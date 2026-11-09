"""DGN-2041: /update said "changed, pick again" and then applied by itself.

Owner report 2026-10-12 19:41 (a kit update for a two-agent crew): the confirm card
replaced the list in place; an apply callback landed ~1 s after it (the
buttons went at the tap, so the owner saw a card without a button); the
machine (dev, DGN-2027) started the crew run and returned no text, and this
bridge's older adapter turned the missing text into UPDATE_STALE.  The crew
update then completed.  Wanted, bridge side:
  (a)/(b) UPDATE_STALE only for a stale outcome -- a started / busy outcome
      without text says nothing, so stale never stands next to a run;
  (c) an apply tap the machine re-shows as the confirm card frees the card
      (the next tap is a fresh confirm, not a repeat toast);
  (d) no offer / confirm screen without its button;
  and the bridge declares DOGANY_UPDATE_SILENT_START=1 to the machine.

component: agents/.template/bridge/bot.py (/update tap adapter)
promise: a queued outcome edits the offer message in place to the queued line (no new message, no buttons); a repeat tap says the same line
promise: every update-tap branch logs one INFO line carrying the ledger id and the decision
"""

# ESTATE:BEGIN DGN-1417 /update is an estate-only region of bot.py
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages

OWNER_ID = 4242
STALE = "업데이트할 내용이 달라졌어요. 새 목록에서 다시 골라주세요."
TOAST = "이미 업데이트가 진행 중이에요."
APPLY = "upd:p:abcd1234-0000:1:0"
BACK = "upd:b:abcd1234-0000:1"
SYNC = "upd:s:abcd1234-0000:1:0"
CONFIRM = {"action": "confirm", "reason": "unconfirmed", "html": "🛠️ 아래 업데이트를 적용할까요?",
           "buttons": [{"text": "< 뒤로가기", "data": BACK}, {"text": "적용", "data": APPLY}]}


def _bridge():
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    bridge.application.bot.send_message = AsyncMock()
    return bridge


def _card():
    return InlineKeyboardMarkup([[InlineKeyboardButton("< 뒤로가기", callback_data=BACK),
                                  InlineKeyboardButton("적용", callback_data=APPLY)]])


def _update(data, message_id=5, markup=None):
    message = SimpleNamespace(chat=SimpleNamespace(id=OWNER_ID), message_id=message_id,
                              date=datetime.now(timezone.utc), reply_text=AsyncMock(),
                              reply_markup=markup)
    query = MagicMock(data=data, message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return SimpleNamespace(callback_query=query, message=None,
                           effective_user=SimpleNamespace(id=OWNER_ID), effective_chat=message.chat)


def _patched(bridge, tool):
    return (patch.object(bridge, "_check_access", new=AsyncMock(return_value=True)),
            patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/plan.tsv")),
            patch.object(bridge, "_update_tool", new=tool))


async def _tap(bridge, data, results, markup=None):
    calls = []

    async def tool(*args):
        calls.append(args)
        return results.pop(0)
    tap = _update(data, markup=markup)
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._handle_callback(tap, MagicMock())
    return tap, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [APPLY, SYNC])
async def test_textless_crew_start_is_never_answered_stale(data):
    # The incident's reply: started, no text (DGN-2027 crew run).
    bridge = _bridge()
    tap, _ = await _tap(bridge, data, [(0, {"action": "started", "started": True, "ledger": "3c18d7ee"})],
                        markup=_card())
    tap.callback_query.message.reply_text.assert_not_awaited()
    bridge.application.bot.send_message.assert_not_awaited()
    # DGN-2083: the textless start retains its own run, checked on re-tap.
    again, calls = await _tap(bridge, data, [(0, {"active": True})], markup=_card())
    again.callback_query.answer.assert_awaited_once_with(text=TOAST)
    assert calls == [("status", "--ledger", "3c18d7ee")]


@pytest.mark.asyncio
async def test_textless_busy_is_never_answered_stale():
    # cmd_pick maps a lost start claim to action "busy" without text.
    bridge = _bridge()
    tap, _ = await _tap(bridge, APPLY, [(0, {"action": "busy", "started": False, "state": "running"})])
    tap.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_action_is_not_stale_either():
    bridge = _bridge()
    tap, _ = await _tap(bridge, APPLY, [(0, {"action": "picked"})], markup=_card())
    tap.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_real_stale_still_says_stale():
    bridge = _bridge()
    tap, _ = await _tap(bridge, APPLY, [(0, {"action": "stale", "text": STALE})], markup=_card())
    tap.callback_query.message.reply_text.assert_awaited_once_with(STALE)
    bare, _ = await _tap(_bridge(), APPLY, [(0, {"action": "stale"})], markup=_card())
    bare.callback_query.message.reply_text.assert_awaited_once_with(messages.UPDATE_STALE)


@pytest.mark.asyncio
async def test_an_unconfirmed_apply_reshows_the_confirm_card_and_frees_it():
    bridge = _bridge()
    tap, _ = await _tap(bridge, APPLY, [(0, dict(CONFIRM))], markup=_card())
    q = tap.callback_query
    # The buttons came off at the tap, then the confirm card is back with both.
    assert q.edit_message_reply_markup.await_args_list[0].kwargs["reply_markup"] is None
    q.edit_message_text.assert_awaited_once()
    rows = q.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [[b.callback_data for b in row] for row in rows] == [[BACK, APPLY]]
    q.message.reply_text.assert_not_awaited()  # no stale, no start line
    # The next (deliberate) tap on that card reaches the machine again.
    again, calls = await _tap(bridge, APPLY, [(0, {"action": "started", "started": True})], markup=_card())
    assert len(calls) == 1
    again.callback_query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["upd:o:abcd1234-0000:1:0", APPLY])
async def test_a_confirm_without_buttons_is_not_shown(data):
    bridge = _bridge()
    tap, _ = await _tap(bridge, data, [(0, {"action": "confirm", "html": "x", "buttons": []})])
    tap.callback_query.edit_message_text.assert_not_awaited()
    tap.callback_query.message.reply_text.assert_awaited_once_with(messages.UPDATE_LOOKUP_FAILED)


@pytest.mark.asyncio
async def test_an_offer_without_buttons_is_not_sent():
    bridge = _bridge()
    send = AsyncMock()
    assert await bridge._update_send_offer(send, {"html": "<b>x</b>", "buttons": []}) is None
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_update_without_buttons_says_lookup_failed():
    bridge = _bridge()
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=OWNER_ID),
                             effective_chat=SimpleNamespace(id=OWNER_ID))

    async def tool(*args):
        return 0, {"ledger": "x", "items": [{"key": "kit:k:1"}], "html": "<b>x</b>", "buttons": []}
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._cmd_update(update, MagicMock())
    message.reply_text.assert_awaited_once_with(messages.UPDATE_LOOKUP_FAILED)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_fdgn586_slash_update_sends_status_only_offer(fallback):
    bridge = _bridge()
    message = SimpleNamespace(reply_text=AsyncMock())
    if fallback:
        message.reply_text.side_effect = [RuntimeError("HTML refused"), SimpleNamespace(message_id=5)]
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=OWNER_ID),
                             effective_chat=SimpleNamespace(id=OWNER_ID))
    body = ("\U0001f6e0\ufe0f \uc9c0\uae08 \uc801\uc6a9 \uc911\uc778 \uc5c5\ub370\uc774\ud2b8\uac00 \uc788\uc5b4\uc694.\n"
            "<blockquote><b>Health v2.0.0</b>\n\u2514 \uc9c0\uae08 \uc801\uc6a9 \uc911</blockquote>")

    async def tool(*args):
        return 0, {"ledger": "aaaaaaaa", "items": [{"key": "health"}],
                   "html": body, "buttons": [], "in_progress_only": True}

    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._cmd_update(update, MagicMock())
    calls = message.reply_text.await_args_list
    assert calls[0].args == (body,)
    assert calls[0].kwargs == {"parse_mode": "HTML", "reply_markup": None}
    assert len(calls) == (2 if fallback else 1)
    if fallback:
        assert calls[1].kwargs == {"reply_markup": None}
        assert "\u2514 \uc9c0\uae08 \uc801\uc6a9 \uc911" in calls[1].args[0]


@pytest.mark.asyncio
async def test_the_bridge_declares_silent_start_to_the_machine():
    bridge = _bridge()
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="{}")
    with patch.object(bot_mod.subprocess, "run", side_effect=fake_run):
        await bridge._update_tool("versions")
    assert seen.get("DOGANY_UPDATE_SILENT_START") == "1"
    assert seen.get("DOGANY_UPDATE_SILENT_FOLLOW") == "1"
    assert seen.get("DOGANY_UPDATE_RENDER") == "html"
    assert seen.get("DOGANY_UPDATE_QUEUED") == "1"

QUEUED = {"action": "queued", "started": False, "busy": True, "ledger": "feed1234-0000",
          "text": "지금 업데이트가 진행 중이에요. 이 업데이트는 끝나면 자동으로 시작돼요."}


def _tap_lines(caplog):
    return [r.getMessage() for r in caplog.records if "DGN-2135 update tap" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_tap_while_another_update_runs_edits_the_offer_in_place_to_the_queued_line(caplog):
    # DGN-2135: one tap -> the offer message becomes the queued line, no new
    # menu, no buttons, no second message.
    bridge = _bridge()
    with caplog.at_level("INFO"):
        tap, _ = await _tap(bridge, APPLY, [(0, dict(QUEUED))], markup=_card())
    q = tap.callback_query
    q.edit_message_text.assert_awaited_once_with(QUEUED["text"], parse_mode="HTML", reply_markup=None)
    q.message.reply_text.assert_not_awaited()
    bridge.application.bot.send_message.assert_not_awaited()
    assert _tap_lines(caplog) == ["DGN-2135 update tap data=%s ledger=feed1234-0000 decision=queued" % APPLY]


@pytest.mark.asyncio
async def test_a_repeat_tap_says_the_same_line_and_a_not_modified_edit_is_harmless(caplog):
    bridge = _bridge()
    q = _update(APPLY, markup=_card()).callback_query
    q.edit_message_text.side_effect = Exception("Bad Request: message is not modified")
    tap = SimpleNamespace(callback_query=q, message=None, effective_user=SimpleNamespace(id=OWNER_ID),
                          effective_chat=q.message.chat)

    async def tool(*args):
        return 0, dict(QUEUED, repeat=True)
    a, b, c = _patched(bridge, tool)
    with caplog.at_level("INFO"), a, b, c:
        await bridge._handle_callback(tap, MagicMock())
    q.edit_message_text.assert_awaited_once()
    q.message.reply_text.assert_not_awaited()
    assert _tap_lines(caplog) == ["DGN-2135 update tap data=%s ledger=feed1234-0000 decision=queued-repeat" % APPLY]


@pytest.mark.asyncio
@pytest.mark.parametrize("data,result,decision", [
    (APPLY, (0, {"action": "started", "started": True, "ledger": "3c18d7ee"}), "started"),
    (APPLY, (0, dict(CONFIRM)), "reshown"),
    (APPLY, (0, {"action": "stale", "text": STALE}), "open"),
    (BACK, (0, dict(CONFIRM, reason="back")), "handled"),
])
async def test_every_update_tap_branch_logs_one_info_line_with_ledger_and_decision(caplog, data, result, decision):
    bridge = _bridge()
    with caplog.at_level("INFO"):
        await _tap(bridge, data, [result], markup=_card())
    lines = _tap_lines(caplog)
    assert len(lines) == 1 and "ledger=" in lines[0] and lines[0].endswith("decision=" + decision)
    assert ("ledger=" + result[1].get("ledger", "abcd1234-0000")) in lines[0]

# ESTATE:END


# ESTATE:BEGIN DGN-2154 /update adapter + surface regression
_FLOW_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def flow(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "dgn2154_surface", _FLOW_REPO_ROOT / "scripts/pack/update_surface.py")
    surface = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(surface)
    shared = tmp_path / "shared"
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / ".instance.conf").write_text(
        "DOGANY_AGENT_NAME=probe\nDOGANY_FW_VERSION=1.0.0\n")
    sources = tmp_path / "pack-sources.conf"
    sources.write_text("")
    monkeypatch.setenv("DOGANY_SHARED_HOME", str(shared))
    monkeypatch.setenv("DOGANY_PACK_SOURCES", str(sources))
    monkeypatch.setenv("DOGANY_LANG", "ko")
    monkeypatch.setenv("DOGANY_UPDATE_QUEUED", "1")
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", instance)
    monkeypatch.setattr(bot_mod, "_update_machinery_root", lambda: _FLOW_REPO_ROOT)
    monkeypatch.setattr(surface, "CONFIRM_SETTLE_S", 2)
    clock = SimpleNamespace(now=1800000000.25)
    monkeypatch.setattr(surface, "time", SimpleNamespace(time=lambda: clock.now))
    spawns = []
    # Keep subprocess.run (read-only git lookups) real, but never launch the
    # detached supervisor. Real start() still persists the selected plan/run.
    monkeypatch.setattr(surface, "subprocess", SimpleNamespace(
        **dict(vars(subprocess), Popen=lambda *a, **kw: spawns.append(a))))
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    plans, outcomes = [], []

    async def plan():
        clock.now += 6  # The incident's fresh plan costs about six seconds.
        path = tmp_path / ("plan-%d.tsv" % len(plans))
        path.write_text("PLAN-EMIT-V1\nFW\tprobe\t%s\t1.0.0\t2.0.0\n"
                        "PACK\tprobe_pack\tpack\t\t1.0.0\tpack/probe_pack/v2.0.0"
                        "\t2.0.0\tprobe:%s\nPLAN-END\t0\n" % (instance, instance))
        plans.append(path)
        return path

    async def tool(*args):
        monkeypatch.setattr(sys, "argv", [str(spec.origin), *map(str, args)])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = surface.main()
        out = json.loads(stdout.getvalue())
        outcomes.append((args[0], out))
        return rc, out

    monkeypatch.setattr(bridge, "_update_plan", plan)
    monkeypatch.setattr(bridge, "_update_tool", tool)

    async def tap(data, at=None):
        query = SimpleNamespace(edit_message_text=AsyncMock(),
                                message=SimpleNamespace(reply_text=AsyncMock()))
        result = await bridge._handle_update_offer_tap(
            query, data, OWNER_ID, OWNER_ID, int(clock.now if at is None else at))
        return query, result

    return SimpleNamespace(bridge=bridge, surface=surface, clock=clock,
                           tap=tap, plans=plans, outcomes=outcomes, spawns=spawns)


async def open_card(flow, pick="0"):
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()),
                             effective_user=SimpleNamespace(id=OWNER_ID),
                             effective_chat=SimpleNamespace(id=OWNER_ID))
    flow.bridge._check_access = AsyncMock(return_value=True)
    await flow.bridge._cmd_update(update, MagicMock())
    menu = flow.outcomes[-1][1]
    row = pick[1:] if pick.startswith("b") else pick
    callback = next(b["data"] for b in menu["buttons"] if b["data"].endswith(":" + row))
    # Old bundle cards use b<row>; their confirm must retain the same pick.
    callback = callback.rsplit(":", 1)[0] + ":" + pick
    query, _ = await flow.tap(callback)
    command, confirm = flow.outcomes[-1]
    assert command == "offer-view" and confirm["action"] == "confirm"
    apply = query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard[0][-1].callback_data
    ledger = flow.surface.load_ledger(menu["ledger"])
    assert ledger["revision"] == 1 and pick in ledger["confirms"]
    assert apply == "upd:p:%s:1:%s" % (menu["ledger"], pick)
    assert not flow.spawns
    return menu["ledger"], apply


@pytest.mark.asyncio
@pytest.mark.parametrize("pick", ["0", "1", "a", "b0"])
@pytest.mark.parametrize("early_retry", [False, True])
async def test_confirm_tap_starts_after_settle_even_after_slow_early_retry(flow, pick, early_retry):
    ledger_id, apply = await open_card(flow, pick)
    shown = flow.surface.load_ledger(ledger_id)["confirms"][pick]
    if early_retry:
        flow.clock.now += 1
        query, result = await flow.tap(apply)
        assert result == "reshown"
        assert flow.outcomes[-1][1]["action"] == "confirm"
        assert not flow.spawns
        # The identical card is restored after a slow re-plan. Its original
        # appearance, not this retry's completion, owns the settle deadline.
        flow.clock.now += 1
    else:
        flow.clock.now += 3
    _, result = await flow.tap(apply)
    assert flow.outcomes[-1][0] == "pick"
    assert flow.outcomes[-1][1]["action"] == "started"
    assert flow.surface.load_ledger(ledger_id)["confirms"][pick] == shown
    assert result["started"] is True and len(flow.spawns) == 1
    started = flow.surface.load_ledger(result["ledger"])
    assert started["state"] == "running"
    assert started["tapped_at"] >= shown + flow.surface.CONFIRM_SETTLE_S
    assert len(flow.plans) == (4 if early_retry else 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("busy,queued_support", [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize("delay", [None, 8])
async def test_0853_replay_starts_or_queues_once_without_a_fresh_confirm(
        flow, monkeypatch, caplog, busy, queued_support, delay):
    # Live ledger: confirm recorded at 08:53:29.645141; the next plan file
    # was born at 08:53:31.587 and finished at 08:53:37.677. The bridge
    # receipt is integer seconds; replay receipt=31, then the slow re-plan.
    flow.clock.now = 1792367597.645141  # list + open each re-plan for 6 s
    ledger_id, apply = await open_card(flow, "a")
    shown = flow.surface.load_ledger(ledger_id)["confirms"]["a"]
    assert shown == 1792367609.645141
    flow.clock.now = 1792367611.587007 if delay is None else shown + delay
    if not queued_support:
        monkeypatch.delenv("DOGANY_UPDATE_QUEUED")
    if busy:
        # A different agent's live update, outside the selected closure.
        offer = flow.surface.load_ledger(ledger_id)
        other_root = str(Path(offer["instance_root"]).parent / "other")
        other = dict(offer, id="0000bbbb-beef",
                     state="running", selection=["pack:other:3.0.0"],
                     items=[dict(offer["items"][0], key="pack:other:3.0.0",
                                 members=["other"], member_roots=[other_root], labels=["other"])],
                     started_at=int(flow.clock.now))
        flow.surface.atomic_write(flow.surface.ledger_path(other["id"]), other)
    query = _update(apply, markup=_card()).callback_query
    with caplog.at_level("INFO"):
        await flow.bridge._handle_update_callback(
            query, apply, OWNER_ID, OWNER_ID, int(flow.clock.now))
    outcome = flow.outcomes[-1][1]
    expected = "queued" if busy and queued_support else "busy" if busy else "started"
    assert outcome["action"] == expected
    assert not outcome.get("buttons") and not outcome.get("html")
    assert flow.surface.load_ledger(ledger_id)["confirms"]["a"] == shown
    assert len(flow.spawns) == (0 if busy else 1)
    if busy:
        queued = outcome["ledger"]
        assert flow.surface.load_ledger(queued)["state"] == "queued"
        assert "끝나면 자동으로 시작돼요" in outcome["text"]
        # Old bridges render the supported busy action as one plain reply.
        if queued_support:
            query.edit_message_text.assert_awaited_once_with(
                outcome["text"], parse_mode="HTML", reply_markup=None)
            query.message.reply_text.assert_not_awaited()
        else:
            query.edit_message_text.assert_not_awaited()
            query.message.reply_text.assert_awaited_once()
    else:
        started = flow.surface.load_ledger(outcome["ledger"])
        assert started["state"] == "running"
        assert int(started["tapped_at"]) - int(shown) >= 2
    with caplog.at_level("INFO"):
        await flow.bridge._handle_update_callback(
            query, apply, OWNER_ID, OWNER_ID, int(flow.clock.now))
    if busy:
        repeat = flow.outcomes[-1][1]
        assert repeat["action"] == expected and repeat["repeat"]
        assert repeat["ledger"] == queued and repeat["text"] == outcome["text"]
    assert len(flow.spawns) == (0 if busy else 1)
    assert all("decision=reshown" not in line for line in _tap_lines(caplog))
    assert len(_tap_lines(caplog)) == 2


@pytest.mark.asyncio
async def test_back_revokes_confirmation_and_requires_a_new_settle(flow):
    ledger_id, apply = await open_card(flow)
    await flow.tap("upd:b:%s:1" % ledger_id)
    assert flow.outcomes[-1][1]["action"] == "offer"
    assert flow.surface.load_ledger(ledger_id)["confirms"] == {}
    flow.clock.now += 30
    _, result = await flow.tap(apply)
    assert result == "reshown" and not flow.spawns
    assert flow.outcomes[-1][1]["reason"] == "unconfirmed"


@pytest.mark.asyncio
async def test_another_rows_confirm_does_not_authorize_apply(flow):
    ledger_id, _ = await open_card(flow)
    flow.clock.now += 30
    _, result = await flow.tap("upd:p:%s:1:1" % ledger_id)
    assert result == "reshown" and not flow.spawns
    assert flow.outcomes[-1][1]["reason"] == "unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["revision", "plan"])
async def test_a_settled_confirm_still_refuses_a_changed_offer(flow, changed):
    _, apply = await open_card(flow)
    flow.clock.now += 30
    if changed == "revision":
        apply = apply.replace(":1:", ":2:")
    else:
        path = flow.plans[-1]
        path.write_text(path.read_text().replace("2.0.0", "3.0.0"))
        flow.bridge._update_plan = AsyncMock(return_value=path)
    await flow.tap(apply)
    assert flow.outcomes[-1][1]["action"] == "stale"
    assert not flow.spawns
# ESTATE:END


# DGN-2254 (dec-541): no settle wait by default -- a deliberate apply tap in
# the same second the confirm card appeared starts; the consent gate stays.
def test_default_has_no_settle_wait_but_keeps_the_consent_gate(monkeypatch):
    monkeypatch.delenv("DOGANY_UPDATE_CONFIRM_SETTLE_S", raising=False)
    spec = importlib.util.spec_from_file_location(
        "dgn2254_surface", _FLOW_REPO_ROOT / "scripts/pack/update_surface.py")
    surface = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(surface)
    assert surface.CONFIRM_SETTLE_S == 0
    shown = 1792370426.06
    offer = {"confirms": {"a": shown}}
    assert surface.confirmed(offer, "a", int(shown)) is True
    assert surface.confirmed(offer, "a", int(shown) + 1) is True
    assert surface.confirmed(offer, "0", int(shown) + 30) is False
    assert surface.confirmed({"confirms": {}}, "a", int(shown) + 30) is False
