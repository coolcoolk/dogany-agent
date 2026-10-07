"""DGN-1417: the session-start update notice and the /update callbacks.

The ledger (scripts/pack/update_surface.py) is covered by
tests/dgn1417_update_surface_selftest.py; here only the bridge adapter:
the notice goes out as its own message under the reply (the old
framework-only 적용 button on the fw-update carrier is gone), taps bypass
the 20-min gate and enter the same ledger verbs with the callback receipt
time and the tapping user. Requested lookup failures keep their existing
responses and retry; unsolicited failures stay in the log (DGN-1869). The
bridge relays the machine's copy verbatim.
"""

# ESTATE:BEGIN DGN-1417 /update and the notice apply button are estate-only regions of bot.py; the public tree has neither
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import notice_spool

OWNER_ID = 4242
STALE = "업데이트할 내용이 달라졌어요. 새 목록에서 다시 골라주세요."
# DGN-1776: the machine's start text is HTML; DGN-1781: the start line alone.
STARTED = "🛠️ 업데이트를 시작했어요. 잠깐 재시작한 뒤, 제대로 적용됐는지 확인해서 알려드릴게요."
HTML = {"parse_mode": "HTML"}
LOOKUP_FAILED = "업데이트 목록을 불러오지 못했어요. 몇 분 뒤 한 번 더 확인해서 알려드릴게요."
LOOKUP_FAILED_AGAIN = "업데이트 목록을 불러오지 못했어요. 잠시 뒤 /update 를 다시 실행해 주세요."


def _bridge():
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    bridge.application.bot.edit_message_reply_markup = AsyncMock()
    return bridge


async def _settle(kind):
    bridge = _bridge()
    meta = {"id": "n1", "attempt": 1, "kind": kind, "version": "2.6.0-dev.53", "text": "x"}
    carrier = SimpleNamespace(message_id=77)
    with patch.object(bridge, "_promote_notice_delivered", new=AsyncMock()):
        await bridge._settle_notice_receipt(meta, OWNER_ID, [(carrier, "body")], None, "body")
    return bridge.application.bot.edit_message_reply_markup


@pytest.mark.asyncio
async def test_fw_update_notice_carrier_no_longer_gets_the_single_apply_button():
    # Rehearsal item 17: the framework-only [적용] button is replaced by the
    # all-layer notice.
    edit = await _settle(notice_spool.KIND_FW_UPDATE)
    edit.assert_not_awaited()
    assert not hasattr(bot_mod.TelegramBot, "_attach_update_button")


@pytest.mark.asyncio
async def test_pending_restart_notice_gets_no_button():
    edit = await _settle(notice_spool.KIND_PENDING_RESTART)
    edit.assert_not_awaited()


async def _tap(data, tool_results, *, age_s=0):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=OWNER_ID), message_id=5,
        date=datetime.now(timezone.utc), reply_text=AsyncMock(),
    )
    query = MagicMock(data=data, message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = SimpleNamespace(
        callback_query=query, message=None,
        effective_user=SimpleNamespace(id=OWNER_ID),
        effective_chat=message.chat,
    )
    bridge = _bridge()
    calls = []

    async def tool(*args):
        calls.append(args)
        return tool_results.pop(0)
    access = AsyncMock(return_value=True)
    with (
        patch.object(bridge, "_check_access", new=access),
        patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/plan.tsv")),
        patch.object(bridge, "_update_tool", new=tool),
    ):
        await bridge._handle_callback(update, MagicMock())
    return query, calls, access


@pytest.mark.asyncio
async def test_notice_tap_bypasses_stale_gate_and_starts_the_same_executor():
    query, calls, access = await _tap("upd:n:2.6.0-dev.53", [(0, {"action": "started", "started": True, "text": STARTED, **HTML})])
    assert bot_mod._callback_ttl(query.data) is None  # dec-272: the ledger decides staleness
    verb = calls[0]
    assert verb[0] == "notice"
    assert verb[verb.index("--version") + 1] == "2.6.0-dev.53"
    assert int(verb[verb.index("--tapped-at") + 1]) > 0
    assert verb[verb.index("--requester") + 1] == str(OWNER_ID)
    query.message.reply_text.assert_awaited_once_with(STARTED, parse_mode="HTML")
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_stale_notice_tap_is_rejected():
    query, _, _ = await _tap("upd:n:2.6.0-dev.1", [(0, {"action": "stale"})])
    query.message.reply_text.assert_awaited_once_with(STALE)


@pytest.mark.asyncio
async def test_notice_tap_needing_crew_consent_shows_the_consent_card():
    item = {"name": "라이프킷", "current": "1", "target": "2", "members": ["아그", "워그"],
            "needs_consent": True, "includes": [{"name": "ag"}], "sections": []}
    query, _, _ = await _tap("upd:n:2.0.0", [(0, {"action": "consent", "ledger": "abcd1234", "revision": 1, "index": 0, "item": item})])
    text = query.message.reply_text.await_args.args[0]
    markup = query.message.reply_text.await_args.kwargs["reply_markup"]
    assert "아그, 워그이 함께 업데이트돼요. 잠시 답변을 멈췄다가 돌아올게요." in text
    button = markup.inline_keyboard[0][0]
    assert button.text == "잠시 멈추고 업데이트" and button.callback_data == "upd:c:abcd1234:1:0"


@pytest.mark.asyncio
async def test_consent_and_apply_taps_are_not_age_gated():
    # dec-272: the consent/apply card taps were the last upd: kinds behind
    # the 20-min gate; the ledger key/revision already decides staleness.
    query, _, _ = await _tap("upd:a:abcd1234:1:0", [(0, {"raw": "framework:a:2"}), (0, {"started": True})])
    assert bot_mod._callback_ttl(query.data) is None
    assert bot_mod._callback_ttl("upd:c:abcd1234:1:0") is None


@pytest.mark.asyncio
async def test_consent_tap_records_then_applies_with_user_and_tap_time():
    query, calls, _ = await _tap("upd:c:abcd1234:1:0", [
        (0, {"raw": "kit:life:2"}), (0, {"ok": True}), (0, {"started": True, "text": STARTED, **HTML})])
    assert [c[0] for c in calls] == ["key", "consent", "apply"]
    for c in calls[1:]:
        assert c[c.index("--user") + 1] == str(OWNER_ID)
        assert int(c[c.index("--tapped-at") + 1]) > 0
    query.edit_message_text.assert_awaited_once_with(STARTED, parse_mode="HTML")


@pytest.mark.asyncio
async def test_failed_consent_never_applies():
    query, calls, _ = await _tap("upd:c:abcd1234:1:0", [(0, {"raw": "kit:life:2"}), (2, {})])
    assert [c[0] for c in calls] == ["key", "consent"]
    query.edit_message_text.assert_awaited_once_with(STALE)


def test_machinery_root_comes_from_the_instance_conf(tmp_path, monkeypatch):
    canon = tmp_path / "canon"
    (canon / "scripts" / "pack").mkdir(parents=True)
    (canon / "scripts" / "pack" / "update_surface.py").write_text("")
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / ".instance.conf").write_text("DOGANY_AGENT_NAME=x\nDOGANY_REPO_ROOT=%s\n" % canon)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", inst)
    assert bot_mod._update_machinery_root() == canon
    (inst / ".instance.conf").write_text("DOGANY_AGENT_NAME=x\n")
    assert bot_mod._update_machinery_root() == inst


# ---------------------------------------------------------------- round 3
OFFER = {
    "action": "offer", "ledger": "abcd1234-0000", "revision": 1, "signature": "[sig]",
    # DGN-1749 layout, dec-222 item / target hierarchy (the machine's HTML, relayed verbatim).
    "html": "🛠️ 새 업데이트가 2개 있어요.\n"
            "<blockquote><b>[프레임워크] Dogany v2.6.0-dev.53</b>\n└ 적용대상: 김아그·김워그 (현재 v2.6.0-dev.52)</blockquote>\n"
            "<blockquote expandable><b>▸ 업데이트 요약</b>\n요약\n\n<b>▸ 업데이트 상세</b>\n· 상세</blockquote>\n\n"
            "<blockquote><b>[킷] 라이프킷 v2.30.0</b>\n└ 적용대상: 김아그·김워그 (현재 v2.29.0)\n"
            "같이 쓰는 킷이라 두 에이전트가 함께 올라가요.</blockquote>",
    "buttons": [{"text": "\uc804\uccb4 \uc5c5\ub370\uc774\ud2b8", "data": "upd:o:abcd1234-0000:1:a"},
                {"text": "[프레임워크] Dogany v2.6.0-dev.53 \uc5c5\ub370\uc774\ud2b8", "data": "upd:o:abcd1234-0000:1:0"}],
}


def _offer_bridge(tmp_path, monkeypatch, results):
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bot_mod, "UPDATE_OFFER_RETRY_S", 0)
    bridge = _bridge()
    bridge.application.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=501))
    calls = []

    async def tool(*args):
        calls.append(args)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(bridge, "_update_tool", tool)
    return bridge, calls


def _request(tmp_path):
    path = tmp_path / ".telegram_bot" / "state" / "update-offer-request"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


async def _drain():
    for _ in range(20):
        await asyncio.sleep(0)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_notice_goes_out_as_its_own_message_with_all_buttons(tmp_path, monkeypatch):
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [(0, dict(OFFER)), (0, {"ok": True})])
    request = _request(tmp_path)
    with patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/p")):
        bridge._kick_update_offer(OWNER_ID)
        assert not request.exists()  # taken once
        bridge._kick_update_offer(OWNER_ID)  # a second reply takes nothing
        await _drain()
    assert [c[0] for c in calls] == ["offer", "offered"]
    send = bridge.application.bot.send_message
    send.assert_awaited_once()
    assert send.await_args.args == (OWNER_ID, OFFER["html"])
    assert send.await_args.kwargs["parse_mode"] == "HTML"
    rows = send.await_args.kwargs["reply_markup"].inline_keyboard
    assert [(r[0].text, r[0].callback_data) for r in rows] == [
        ("\uc804\uccb4 \uc5c5\ub370\uc774\ud2b8", "upd:o:abcd1234-0000:1:a"), ("[프레임워크] Dogany v2.6.0-dev.53 \uc5c5\ub370\uc774\ud2b8", "upd:o:abcd1234-0000:1:0")]
    offered = calls[1]
    assert offered[offered.index("--signature") + 1] == "[sig]"
    assert offered[offered.index("--message-id") + 1] == "501"
    assert "--force" not in calls[0]


@pytest.mark.asyncio
async def test_no_request_no_notice(tmp_path, monkeypatch):
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [])
    bridge._kick_update_offer(OWNER_ID)
    await _drain()
    assert calls == []
    bridge.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_asked_set_is_quiet(tmp_path, monkeypatch):
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [(0, {"action": "quiet"})])
    _request(tmp_path)
    with patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/p")):
        bridge._kick_update_offer(OWNER_ID)
        await _drain()
    assert [c[0] for c in calls] == ["offer"]
    bridge.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_leader_member_sends_nothing(tmp_path, monkeypatch):
    # DGN-1749: only the crew leader delivers the crew's notice; the machine
    # answers a member "quiet" (not_leader) and the bridge stays silent,
    # records nothing and schedules no re-check.
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [(0, {"action": "quiet", "reason": "not_leader",
                                                               "leader": "/x/ag", "ledger": "", "signature": "",
                                                               "handoff": True})])
    _request(tmp_path)
    with patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/p")):
        bridge._kick_update_offer(OWNER_ID)
        await _drain()
    assert [c[0] for c in calls] == ["offer"]
    bridge.application.bot.send_message.assert_not_awaited()


class _StopInbox(Exception):
    pass


@pytest.mark.asyncio
async def test_member_handover_is_sent_by_the_leader_bridge_without_a_model_turn(tmp_path, monkeypatch):
    # DGN-1749 review (7): a member hands its offer to the leader (request +
    # "[update-offer-handoff]" session-inbox marker).  The leader's inbox loop
    # sends the notice through the session-start seat -- the marker is never
    # injected as a model turn and no other owner text goes out.
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [(0, dict(OFFER)), (0, {"ok": True})])
    request = _request(tmp_path)
    inbox = tmp_path / ".telegram_bot" / "session-inbox"
    inbox.mkdir(parents=True)
    drop = inbox / "update-offer-handoff.md"
    drop.write_text("[update-offer-handoff] from 워그 (DGN-1749): the bridge sends the update notice; "
                    "not a model turn.\n")
    fake_sdk = MagicMock()
    fake_sdk.ensure_owner_stream = AsyncMock(return_value=True)
    fake_sdk.inject_background_turn = AsyncMock(return_value=True)
    fake_config = MagicMock()
    fake_config.bot_data_dir = tmp_path / ".telegram_bot"
    fake_config.allowed_user_ids = [OWNER_ID]
    real_sleep = asyncio.sleep
    ticks = {"n": 0}

    async def sleep(secs, *a, **k):
        if secs != 20:
            return await real_sleep(secs, *a, **k)
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise _StopInbox
    monkeypatch.setattr(bot_mod, "config", fake_config)
    monkeypatch.setattr(bot_mod, "sdk_bridge", fake_sdk)
    monkeypatch.setattr(bridge, "_user_turn_active", lambda uid: False)
    with patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/p")), \
         patch.object(bot_mod.asyncio, "sleep", sleep):
        with pytest.raises(_StopInbox):
            await bridge._session_inbox_loop()
        await _drain()
    assert not drop.exists() and not request.exists()
    fake_sdk.inject_background_turn.assert_not_awaited()
    fake_sdk.ensure_owner_stream.assert_not_awaited()
    assert [c[0] for c in calls] == ["offer", "offered"]
    send = bridge.application.bot.send_message
    send.assert_awaited_once()
    assert send.await_args.args == (OWNER_ID, OFFER["html"])


@pytest.mark.asyncio
async def test_unsent_notice_is_not_recorded_as_asked(tmp_path, monkeypatch):
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [(0, dict(OFFER))])
    bridge.application.bot.send_message = AsyncMock(side_effect=RuntimeError("down"))
    _request(tmp_path)
    with patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/p")):
        bridge._kick_update_offer(OWNER_ID)
        await _drain()
    assert [c[0] for c in calls] == ["offer"]


@pytest.mark.asyncio
async def test_unsolicited_offer_failure_stays_silent(tmp_path, monkeypatch):
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [(2, {})])
    _request(tmp_path)
    with patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/p")):
        bridge._kick_update_offer(OWNER_ID)
        await _drain()
    bridge.application.bot.send_message.assert_not_awaited()
    assert [c[0] for c in calls] == ["offer"]
    assert not (tmp_path / ".telegram_bot/state/update-offer-failure-day").exists()


@pytest.mark.asyncio
async def test_first_contact_planner_failure_stays_silent(tmp_path, monkeypatch):
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [])
    planner = AsyncMock(side_effect=RuntimeError("lookup failed"))
    with patch.object(bridge, "_update_plan", new=planner):
        for _ in range(2):
            _request(tmp_path)
            bridge._kick_update_offer(OWNER_ID)
            await _drain()
    bridge.application.bot.send_message.assert_not_awaited()
    assert planner.await_count == 2
    assert calls == []


@pytest.mark.asyncio
async def test_requested_recheck_failure_still_reports(tmp_path, monkeypatch):
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [(2, {})])
    with patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/p")):
        await bridge._update_offer_run(OWNER_ID, retry=True)
    bridge.application.bot.send_message.assert_awaited_once_with(OWNER_ID, LOOKUP_FAILED_AGAIN)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["none", "offer"])
async def test_requested_recheck_reports_result(tmp_path, monkeypatch, action):
    result = dict(OFFER) if action == "offer" else {"action": "none"}
    bridge, calls = _offer_bridge(tmp_path, monkeypatch, [(0, result), (0, {"ok": True})])
    with patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/p")):
        await bridge._update_offer_run(OWNER_ID, retry=True)
    expected = OFFER["html"] if action == "offer" else bot_mod.messages.UPDATE_NONE
    bridge.application.bot.send_message.assert_awaited_once()
    assert bridge.application.bot.send_message.await_args.args == (OWNER_ID, expected)
    assert "--force" in calls[0]


@pytest.mark.asyncio
async def test_requested_update_planner_failure_keeps_existing_reply():
    bridge = _bridge()
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    with patch.object(bridge, "_check_access", new=AsyncMock(return_value=True)), \
            patch.object(bridge, "_update_plan", new=AsyncMock(side_effect=RuntimeError("lookup failed"))):
        await bridge._cmd_update(update, None)
    update.message.reply_text.assert_awaited_once_with(bot_mod.messages.UPDATE_CHECK_FAILED)


def test_both_reply_paths_hand_off_to_the_notice():
    import inspect
    for fn in (bot_mod.TelegramBot._reply_smart, bot_mod.TelegramBot._send_smart):
        src = inspect.getsource(fn)
        assert src.rstrip().splitlines()[-2].strip().startswith("self._kick_update_offer(")


@pytest.mark.asyncio
async def test_apply_all_tap_enters_pick_and_relays_the_start_copy():
    query, calls, access = await _tap("upd:p:abcd1234-0000:1:a", [(0, {"action": "started", "started": True, "text": STARTED, **HTML})])
    assert bot_mod._callback_ttl(query.data) is None  # dec-272: the ledger decides staleness
    verb = calls[0]
    assert verb[0] == "pick"
    assert verb[verb.index("--ledger") + 1] == "abcd1234-0000"
    assert verb[verb.index("--revision") + 1] == "1"
    assert verb[verb.index("--pick") + 1] == "a"
    assert verb[verb.index("--user") + 1] == str(OWNER_ID)
    assert int(verb[verb.index("--tapped-at") + 1]) > 0
    query.message.reply_text.assert_awaited_once_with(STARTED, parse_mode="HTML")
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_single_item_tap_strips_the_confirm_card():
    query, calls, _ = await _tap("upd:p:abcd1234-0000:1:0", [(0, {"action": "started", "started": True, "text": STARTED, **HTML, "all_rows": False})])
    query.message.reply_text.assert_awaited_once_with(STARTED, parse_mode="HTML")
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)  # DGN-1785


@pytest.mark.asyncio
async def test_the_only_row_tapped_clears_the_buttons():
    # A one-row notice has no separate Update all button.
    query, _, _ = await _tap("upd:p:abcd1234-0000:1:0", [(0, {"action": "started", "started": True, "text": STARTED, **HTML, "all_rows": True})])
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_pack_confirmation_runs_the_existing_pick_path():
    # Item 22: the kit is on the pack's row; the machine starts both at once
    # and the bridge relays the start copy -- no "함께 적용" screen.
    started = STARTED
    query, calls, _ = await _tap("upd:p:abcd1234-0000:1:0", [(0, {"action": "started", "started": True, "text": started, **HTML})])
    assert [c[0] for c in calls] == ["pick"]
    query.message.reply_text.assert_awaited_once_with(started, parse_mode="HTML")
    assert "reply_markup" not in query.message.reply_text.await_args.kwargs
    # A round-3 bundle-confirm button still resolves through pick.
    _, calls, _ = await _tap("upd:p:abcd1234-0000:1:b1", [(0, {"action": "started", "started": True, "text": started, **HTML})])
    assert calls[0][calls[0].index("--pick") + 1] == "b1"


@pytest.mark.asyncio
async def test_start_html_refused_falls_back_to_plain():
    # DGN-1776: a refused HTML send still tells the owner the update started.
    async def reply(text, **kw):
        if kw.get("parse_mode") == "HTML":
            raise RuntimeError("can't parse entities")
    bridge = _bridge()
    sent = AsyncMock(side_effect=reply)
    await bridge._update_send_reply(sent, {"started": True, "text": STARTED, **HTML}, STARTED)
    assert sent.await_args_list[-1].args == (bot_mod.html_to_plain_text(STARTED),)
    assert "<blockquote>" not in sent.await_args_list[-1].args[0]
    plain = AsyncMock()
    await bridge._update_send_reply(plain, {"busy": True}, "busy")
    plain.assert_awaited_once_with("busy")


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["pick", "notice", "apply"])
async def test_every_start_verb_declares_html_render(verb, tmp_path, monkeypatch):
    # DGN-1778: the script emits HTML start text only for a caller that
    # declared it renders HTML; this bridge declares it on every verb that
    # can start an update (pick = offer / update tap, notice = upd:n, apply).
    monkeypatch.setattr(bot_mod, "_update_machinery_root", lambda: tmp_path)
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw.get("env"))
        return SimpleNamespace(returncode=0, stdout='{"started": true}')
    monkeypatch.setattr(bot_mod.subprocess, "run", fake_run)
    rc, out = await _bridge()._update_tool(verb, "--ledger", "x")
    assert (rc, out) == (0, {"started": True})
    assert seen["cmd"][2] == verb
    assert seen["env"]["DOGANY_UPDATE_RENDER"] == "html"


@pytest.mark.asyncio
async def test_plain_start_outcome_is_sent_without_parse_mode():
    # DGN-1778: an undeclared caller's start outcome carries no parse_mode.
    plain_start = bot_mod.html_to_plain_text(STARTED)
    sent = AsyncMock()
    await _bridge()._update_send_reply(sent, {"started": True, "text": plain_start}, plain_start)
    sent.assert_awaited_once_with(plain_start)


PARTIAL_BUTTON = [[{"text": "워그 재시작", "callback_data": "upd:r:abcd1234-0000:0"}]]


@pytest.mark.asyncio
async def test_restart_tap_runs_the_machine_restart_and_relays_the_verdict():
    done = "🛠️ 업데이트를 완료했어요.\n\n[킷] 라이프킷 v2.0.0: 아그와 워그 모두 새 버전으로 적용됐어요."
    query, calls, access = await _tap("upd:r:abcd1234-0000:0", [(0, {"action": "done", "alive": True, "text": done, "buttons": []})])
    assert bot_mod._callback_ttl(query.data) is None  # dec-272: the ledger decides staleness
    assert calls == [("restart", "--ledger", "abcd1234-0000", "--index", "0", "--user", str(OWNER_ID))]
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)  # no double tap mid-restart
    query.message.reply_text.assert_awaited_once_with(done, reply_markup=None)


@pytest.mark.asyncio
async def test_restart_tap_still_stopped_offers_the_button_again():
    failed = "워그를 다시 시작해 봤지만 아직 멈춰 있어요. 잠시 뒤 한 번 더 눌러 주세요."
    query, _, _ = await _tap("upd:r:abcd1234-0000:0", [(0, {"action": "failed", "alive": False, "text": failed, "buttons": PARTIAL_BUTTON})])
    assert query.message.reply_text.await_args.args[0] == failed
    button = query.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert (button.text, button.callback_data) == ("워그 재시작", "upd:r:abcd1234-0000:0")


@pytest.mark.asyncio
async def test_restart_tap_refused_by_the_machine_never_claims_success():
    query, _, _ = await _tap("upd:r:abcd1234-0000:0", [(2, {})])
    query.message.reply_text.assert_awaited_once_with(STALE)


def test_consent_prefix_names_agents_by_label_not_slug():
    # DGN-1744: members are install slugs; the owner reads the labels.
    item = {"members": ["ag", "health"], "labels": ["김아그", "김헬스"]}
    assert bot_mod.TelegramBot._update_consent_prefix(item).startswith("김아그, 김헬스이 함께")


CREW_CAPTION = "[프레임워크] Dogany → v2.6.0-dev.57 (김아그 v2.6.0-dev.55, 김워그 v2.6.0-dev.56)"


def test_update_row_uses_the_machine_caption():
    # DGN-1749: a several-agent row is one line, agents grouped by version, never a "~" range.
    item = {"name": "[프레임워크] Dogany", "current": "2.6.0-dev.55", "target": "2.6.0-dev.57", "caption": CREW_CAPTION}
    assert bot_mod.TelegramBot._update_row(item) == CREW_CAPTION
    assert "~" not in bot_mod.TelegramBot._update_row(item)


async def _update_command(result):
    bot = _bridge()
    bot._check_access = AsyncMock(return_value=True)
    bot._update_plan = AsyncMock(return_value="/tmp/plan.tsv")
    bot._update_tool = AsyncMock(return_value=result)
    update = MagicMock()
    update.effective_user.id = OWNER_ID
    update.effective_chat.id = OWNER_ID
    update.message.reply_text = AsyncMock()
    await bot._cmd_update(update, MagicMock())
    return bot, update.message.reply_text


@pytest.mark.asyncio
async def test_update_command_is_the_notice_screen_byte_for_byte(tmp_path, monkeypatch):
    # DGN-1775: one renderer.  /update relays the machine's offer HTML and
    # buttons exactly as the session-start notice sends them.
    items = [{"name": "[프레임워크] Dogany", "current": "2.6.0-dev.52", "target": "2.6.0-dev.53"}]
    listing = dict(OFFER, items=items)
    listing.pop("action")
    bot, reply = await _update_command((0, listing))
    assert bot._update_tool.await_args.args[0] == "list"
    reply.assert_awaited_once()
    notice, _ = _offer_bridge(tmp_path, monkeypatch, [(0, dict(OFFER)), (0, {"ok": True})])
    _request(tmp_path)
    with patch.object(notice, "_update_plan", new=AsyncMock(return_value="/tmp/p")):
        notice._kick_update_offer(OWNER_ID)
        await _drain()
    sent = notice.application.bot.send_message.await_args
    assert reply.await_args.args == (OFFER["html"],) == sent.args[1:]
    assert reply.await_args.kwargs["parse_mode"] == sent.kwargs["parse_mode"] == "HTML"
    rows = lambda call: [[(b.text, b.callback_data) for b in r] for r in call.kwargs["reply_markup"].inline_keyboard]
    assert rows(reply.await_args) == rows(sent) == [[(b["text"], b["data"])] for b in OFFER["buttons"]]


@pytest.mark.asyncio
async def test_update_command_with_nothing_to_apply_says_so():
    _, reply = await _update_command((0, {"ledger": "abcd1234", "revision": 1, "items": []}))
    reply.assert_awaited_once_with(bot_mod.messages.UPDATE_NONE)


@pytest.mark.asyncio
async def test_update_command_with_nothing_to_apply_relays_installed_versions():
    # DGN-1790: the machine's "nothing to apply" text carries the installed
    # versions as an HTML quote; the bridge relays it verbatim.
    text = "지금 적용할 업데이트가 없어요.\n\n설치된 버전은 아래와 같아요.\n<blockquote>· [프레임워크] Dogany v2.6.0-dev.55</blockquote>"
    _, reply = await _update_command((0, {"ledger": "abcd1234", "revision": 1, "items": [],
                                          "text": text, "parse_mode": "HTML"}))
    reply.assert_awaited_once_with(text, parse_mode="HTML")


@pytest.mark.asyncio
async def test_old_update_list_detail_tap_is_stale():
    # The pre-DGN-1775 list buttons (upd:d) have no screen of their own any more.
    query, calls, _ = await _tap("upd:d:abcd1234:1:0", [(0, {"raw": "framework:a:2"})])
    assert [c[0] for c in calls] == ["key"]
    query.edit_message_text.assert_awaited_once_with(STALE)


# DGN-1794 r2 (dec-210): the busy reply asks whether to carry the apply
# through, with the machine's two buttons.
BUSY_ASK = "지금 [프레임워크] Dogany 업데이트(v2.6.0-dev.52 → v2.6.0-dev.53)가 진행 중이에요. 끝나면 이어서 적용할까요?"
ASK_BUTTONS = [[{"text": "이어서 적용", "callback_data": "upd:q:beef0000-1111:y"},
                {"text": "그만두기", "callback_data": "upd:q:beef0000-1111:n"}]]
BUSY_OUT = {"action": "busy", "busy": True, "started": False, "ledger": "beef0000-1111",
            "text": BUSY_ASK, "buttons": ASK_BUTTONS}


def _rows(call):
    return [[(b.text, b.callback_data) for b in row] for row in call.kwargs["reply_markup"].inline_keyboard]


def _ask_rows():
    return [[(b["text"], b["callback_data"]) for b in row] for row in ASK_BUTTONS]


@pytest.mark.asyncio
async def test_busy_and_stale_taps_relay_the_machine_copy():
    # DGN-1794: busy strips the confirm card (upd:p -- the notice's and the
    # /update screen's, one tap flow): its apply button is dead for the
    # running run.  r2: the reply asks, [이어서 적용][그만두기] in one row.
    for data in ("upd:p:abcd1234-0000:1:a", "upd:p:abcd1234-0000:1:0"):
        query, _, _ = await _tap(data, [(0, dict(BUSY_OUT))])
        query.message.reply_text.assert_awaited_once()
        assert query.message.reply_text.await_args.args == (BUSY_ASK,)
        assert _rows(query.message.reply_text.await_args) == _ask_rows()
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query, _, _ = await _tap("upd:p:abcd1234-0000:1:0", [(0, {"action": "stale", "text": STALE})])
    query.message.reply_text.assert_awaited_once_with(STALE)
    query.edit_message_reply_markup.assert_not_awaited()  # stale is out of DGN-1794 scope


@pytest.mark.asyncio
async def test_list_screen_taps_never_strip_before_the_confirm_card():
    # DGN-1794 scope: a /update (or notice) row tap only opens the confirm
    # card in place; the strip belongs to the card's own apply tap.
    confirm = {"action": "confirm", "html": "<b>x</b>",
               "buttons": [{"text": "< 뒤로가기", "data": "upd:b:abcd1234-0000:1"},
                           {"text": "적용", "data": "upd:p:abcd1234-0000:1:0"}]}
    query, _, _ = await _tap("upd:o:abcd1234-0000:1:0", [(0, confirm)])
    query.edit_message_text.assert_awaited_once()
    query.edit_message_reply_markup.assert_not_awaited()
    query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_round2_notice_busy_strips_the_notice_and_asks():
    # DGN-1794: the upd:n busy branch strips like its started/stale siblings;
    # r2: its reply is the ask with the two buttons.
    query, _, _ = await _tap("upd:n:2.6.0-dev.53", [(0, dict(BUSY_OUT))])
    query.message.reply_text.assert_awaited_once()
    assert query.message.reply_text.await_args.args == (BUSY_ASK,)
    assert _rows(query.message.reply_text.await_args) == _ask_rows()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("data,tools", [
    ("upd:a:abcd1234:1:0", [(0, {"raw": "framework:a:2"})]),
    ("upd:c:abcd1234:1:0", [(0, {"raw": "kit:life:2"}), (0, {"ok": True})]),
])
async def test_detail_card_busy_asks_and_strips_the_card(data, tools):
    # DGN-1794 r2: the detail-card apply (upd:a / upd:c) asks too, as its
    # own reply; the card keeps no dead apply button.
    apply_busy = {k: v for k, v in BUSY_OUT.items() if k != "action"}
    query, calls, _ = await _tap(data, tools + [(0, apply_busy)])
    assert calls[-1][0] == "apply"
    query.edit_message_text.assert_not_awaited()
    assert query.message.reply_text.await_args.args == (BUSY_ASK,)
    assert _rows(query.message.reply_text.await_args) == _ask_rows()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


QUEUED = "알겠어요. 끝나면 이어서 적용할게요."
DECLINED = "적용하지 않았어요."


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,action,text", [("y", "queued", QUEUED), ("n", "declined", DECLINED)])
async def test_follow_tap_replaces_the_ask_with_the_machine_line(choice, action, text):
    query, calls, access = await _tap("upd:q:beef0000-1111:" + choice,
                                      [(0, {"action": action, "text": text, "parse_mode": "HTML"})])
    assert bot_mod._callback_ttl(query.data) is None  # dec-272: the ledger decides staleness
    verb = calls[0]
    assert verb[0] == "follow"
    assert verb[verb.index("--ledger") + 1] == "beef0000-1111"
    assert verb[verb.index("--choice") + 1] == choice
    assert verb[verb.index("--user") + 1] == str(OWNER_ID)
    assert verb[verb.index("--chat") + 1] == str(OWNER_ID)
    query.edit_message_text.assert_awaited_once_with(text, parse_mode="HTML", reply_markup=None)
    query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_follow_tap_that_starts_or_reoffers_renders_the_machine_reply():
    # The run already ended: the tap itself picks up -> the start line, or
    # (plan moved) a fresh /update card with its buttons, in place of the ask.
    query, _, _ = await _tap("upd:q:beef0000-1111:y", [(0, {"action": "picked", "text": STARTED, **HTML})])
    query.edit_message_text.assert_awaited_once_with(STARTED, parse_mode="HTML", reply_markup=None)
    card = {"action": "changed", "parse_mode": "HTML", "text": "내용이 바뀌어서 이어서 적용하지 않았어요. 아래에서 다시 골라주세요.\n\n<b>x</b>",
            "buttons": [[{"text": "[프레임워크] Dogany v2.6.0-dev.54 업데이트", "callback_data": "upd:o:abcd1234-0000:1:0"}]]}
    query, _, _ = await _tap("upd:q:beef0000-1111:y", [(0, card)])
    edit = query.edit_message_text.await_args
    assert edit.args == (card["text"],) and edit.kwargs["parse_mode"] == "HTML"
    assert _rows(edit) == [[("[프레임워크] Dogany v2.6.0-dev.54 업데이트", "upd:o:abcd1234-0000:1:0")]]


@pytest.mark.asyncio
async def test_follow_tap_refused_keeps_the_ask():
    query, _, _ = await _tap("upd:q:beef0000-1111:y", [(2, {})])
    query.message.reply_text.assert_awaited_once_with(STALE)
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_tap_lookup_failure_says_so_and_schedules_the_recheck():
    retried = []

    async def fake_retry(self, chat_id):
        retried.append(chat_id)
    with patch.object(bot_mod.TelegramBot, "_update_offer_retry", new=fake_retry):
        query, _, _ = await _tap("upd:p:abcd1234-0000:1:a", [(2, {})])
        await _drain()
    query.message.reply_text.assert_awaited_once_with(LOOKUP_FAILED)
    assert retried == [OWNER_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["a", "0", "1"])
async def test_offer_tap_edits_same_message_to_confirmation(selection):
    payload = {"action": "confirm", "html": OFFER["html"] + "<blockquote>caution</blockquote>",
               "buttons": [{"text": "\uC801\uC6A9", "data": "upd:p:abcd1234-0000:1:" + selection},
                           {"text": "< \uB4A4\uB85C\uAC00\uAE30", "data": "upd:b:abcd1234-0000:1"}]}
    query, calls, access = await _tap("upd:o:abcd1234-0000:1:" + selection, [(0, payload)])
    assert bot_mod._callback_ttl(query.data) is None  # dec-272: the ledger decides staleness
    assert calls[0][0] == "offer-view"
    assert calls[0][calls[0].index("--pick") + 1] == selection
    query.message.reply_text.assert_not_awaited()
    edit = query.edit_message_text.await_args
    assert edit.args == (payload["html"],)
    assert edit.kwargs["parse_mode"] == "HTML"
    assert [(b.text, b.callback_data) for b in edit.kwargs["reply_markup"].inline_keyboard[0]] == [(b["text"], b["data"]) for b in payload["buttons"]]


@pytest.mark.asyncio
async def test_back_restores_offer_in_same_message():
    query, calls, access = await _tap("upd:b:abcd1234-0000:1", [(0, dict(OFFER))])
    assert bot_mod._callback_ttl(query.data) is None  # dec-272: the ledger decides staleness
    assert calls[0][0] == "offer-view" and "--back" in calls[0]
    query.message.reply_text.assert_not_awaited()
    edit = query.edit_message_text.await_args
    assert edit.args == (OFFER["html"],)
    assert [(r[0].text, r[0].callback_data) for r in edit.kwargs["reply_markup"].inline_keyboard] == [(b["text"], b["data"]) for b in OFFER["buttons"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["upd:o:abcd1234-0000:1:0", "upd:b:abcd1234-0000:1"])
async def test_confirmation_and_back_relay_stale(data):
    query, _, _ = await _tap(data, [(0, {"action": "stale", "text": STALE})])
    query.message.reply_text.assert_awaited_once_with(STALE)
    query.edit_message_text.assert_not_awaited()
# ESTATE:END
