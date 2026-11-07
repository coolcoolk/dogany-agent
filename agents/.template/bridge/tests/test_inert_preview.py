# test-component: bridge.inert_preview and receipt-gated candidate delivery
# test-tier: 0
# test-prereq: tmp-files, subprocess
# test-new-component: New preview adapter/frame and multi-candidate receipt boundary have no existing owning suite.
"""FDGN-576: native preview fidelity, no action routing, receipt-before-pick."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bridge import bot as bot_mod
from bridge.inert_preview import adapt, prepare, validate_frame
from bridge.live_surface import Coordinator, Refusal, atomic, digest, mac

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = json.loads((Path(__file__).parent / "fixtures/inert_preview_candidates.json").read_text())
spec = importlib.util.spec_from_file_location("preview_candidates", ROOT / "routines/preview-candidates.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


def run(value):
    return asyncio.run(value)


@pytest.mark.parametrize("sample", SAMPLES["candidates"])
def test_golden_body_quote_boundaries_and_native_rows(tmp_path, sample):
    payload = prepare(tmp_path, sample["candidate_id"], sample["text"], sample["buttons"])
    assert payload["text"] == sample["text"]
    assert payload["text"].count("<blockquote>") == 1
    assert payload["text"].count("</blockquote>") == 1
    title, quoted = payload["text"].split("<blockquote>")
    quote, footer = quoted.split("</blockquote>")
    assert len(quote.splitlines()) == 3
    assert "\u2022" not in title + footer
    assert [[b["text"] for b in row] for row in payload["buttons"]] == [
        ["\ub0b4 \uacc4\uc88c\uc5d0\uc11c \uc62e\uae34 \ub3c8"], ["\uc218\uc785"]]
    assert all(b["callback_data"].startswith("preview:") for row in payload["buttons"] for b in row)


def test_escaping_long_labels_and_url_actions(tmp_path):
    label = "\ud55c\uae00" * 80
    buttons = [[dict(text=label, url="https://example.com/?x=1&y=2"),
                dict(text="& < >", callback_data="opt:" + label)]]
    payload = prepare(tmp_path, "escaped", "<b>A & B</b>\n<blockquote><value></blockquote>", buttons)
    assert payload["text"] == "<b>A &amp; B</b>\n<blockquote>&lt;value&gt;</blockquote>"
    assert payload["buttons"] == [[dict(text=label, callback_data="preview:escaped:0:0"),
                                      dict(text="& < >", callback_data="preview:escaped:0:1")]]
    assert buttons[0][0].get("url")


@pytest.mark.parametrize("data", ["preview:A:0:0", "preview:old:0:0", "preview:url:0:0",
                                  "preview:", "preview:opt:1", "preview:\x00malformed"])
def test_reserved_taps_return_before_every_router(tmp_path, monkeypatch, data):
    bridge = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bridge._check_access = AsyncMock(return_value=True)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", tmp_path)
    forbidden = Mock(side_effect=AssertionError("preview escaped to product routing"))
    monkeypatch.setattr(bot_mod.callback_routes, "Router", forbidden)
    # ESTATE:BEGIN pack revocation is excluded from the public bridge
    monkeypatch.setattr(bot_mod.pack_revocation, "callback_owner", forbidden)
    # ESTATE:END
    monkeypatch.setattr(bot_mod.session_manager, "get_session", forbidden)
    monkeypatch.setattr(bot_mod.sdk_bridge, "process_message", forbidden)
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            edit_message_reply_markup=AsyncMock())
    update = SimpleNamespace(callback_query=query)
    before = list(tmp_path.rglob("*"))
    run(bridge._handle_callback(update, None))
    run(bridge._handle_callback(update, None))
    assert query.answer.await_count == 2
    expected = "\ubbf8\ub9ac\ubcf4\uae30\uc608\uc694. \ub20c\ub7ec\ub3c4 \uc2e4\ud589\ub418\uc9c0 \uc54a\uc544\uc694."
    assert query.answer.await_args.kwargs["text"].encode("utf-8") == expected.encode("utf-8")
    query.answer.assert_awaited_with(text=expected, show_alert=False)
    forbidden.assert_not_called()
    query.edit_message_text.assert_not_awaited()
    query.edit_message_reply_markup.assert_not_awaited()
    assert list(tmp_path.rglob("*")) == before


def test_access_first_and_configured_toast_only(tmp_path, monkeypatch):
    bridge = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bridge._check_access = AsyncMock(return_value=False)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", tmp_path)
    query = SimpleNamespace(data="preview:A", answer=AsyncMock())
    update = SimpleNamespace(callback_query=query)
    run(bridge._handle_callback(update, None))
    query.answer.assert_not_awaited()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/agent.conf").write_text('PREVIEW_TOAST="fixture approved copy"\n')
    bridge._check_access.return_value = True
    run(bridge._handle_callback(update, None))
    query.answer.assert_awaited_once_with(text="fixture approved copy", show_alert=False)
    query.answer.side_effect = RuntimeError("expired query")
    run(bridge._handle_callback(update, None))


def coordinator(tmp_path, active=False, responses=None):
    sent = []
    replies = iter(responses or [])
    async def transport(payload):
        sent.append(copy.deepcopy(payload))
        reply = next(replies, dict(ok=True, result=dict(message_id=len(sent) + 100, chat=dict(id=42))))
        if isinstance(reply, Exception):
            raise reply
        return reply
    coord = Coordinator(tmp_path, transport=transport, clock=lambda: 1000, notification_policy=lambda _: True)
    if active:
        atomic(tmp_path / "config/packs/fixture.session_activation.json", dict(generation=1, phase="active"))
        state = dict(schema="live_surface/1", chat_id=42, surface_id="fixture", epoch=1,
                     owner="fixture", generation=1, state="open", action_slot="card", commit_order=1,
                     lease_until_ms=100000, lease_s=100, envelopes={"turn:42:fixture": dict(kind="turn")},
                     slots=dict(card=dict(message_id=55, has_keyboard=True, retired_ids=[],
                         body="Product", buttons=[[dict(text="Act", callback_data="real:act")]])))
        coord.save_state(state)
    return coord, sent


@pytest.mark.parametrize("active", [False, True])
def test_native_delivery_and_retry_keep_product_action(tmp_path, active):
    coord, sent = coordinator(tmp_path, active)
    old = coord.state(42)
    sample = SAMPLES["candidates"][0]
    rc, result = run(coord.push_preview(42, sample["candidate_id"], sample["text"], sample["buttons"]))
    assert rc == 0 and result["delivered"] and result["message_id"] == 101
    assert len(sent) == 1
    frame = sent[0]["frame"]
    assert frame["frame"] == "inert-preview" and frame["slot"] == "preview"
    assert frame["text"] == sample["text"] and len(frame["buttons"]) == 2
    validate_frame(frame)
    if active:
        assert coord.state(42)["slots"] == old["slots"]
        assert coord.state(42)["commit_order"] == old["commit_order"]
    else:
        assert coord.state(42) is None
    # Restart/retry reads the durable receipt and does not send again.
    fresh = Coordinator(tmp_path, transport=coord.transport, clock=lambda: 1000, notification_policy=lambda _: True)
    assert run(fresh.push_preview(42, sample["candidate_id"], sample["text"], sample["buttons"])) == (rc, result)
    assert len(sent) == 1
    ledger = [json.loads(line) for line in (tmp_path / ".telegram_bot/outbound-ledger.jsonl").read_text().splitlines()]
    assert len(ledger) == 1 and ledger[0]["message_id"] == "101"
    changed = copy.deepcopy(sample["buttons"])
    changed[0][0]["callback_data"] = "other:action"
    with pytest.raises(Refusal, match="payload changed"):
        run(fresh.push_preview(42, sample["candidate_id"], sample["text"], changed))
    assert len(sent) == 1


def test_preview_frames_can_precede_real_action_without_using_its_slot(tmp_path):
    coord, _ = coordinator(tmp_path, True)
    state = coord.state(42)
    frame = dict(stage_id="preview_a", frame="inert-preview", slot="preview", mode="send",
                 text="Screen", buttons=adapt("A", [[dict(text="URL", url="https://example.com")]]),
                 silent=True, notification_class="inert_preview", allowed_outcomes=["ok"])
    action = dict(stage_id="action", frame="card", slot="card", mode="edit", target=dict(message_id=55),
                  text="Product", buttons=state["slots"]["card"]["buttons"], silent=True,
                  notification_class="live_card", allowed_outcomes=["ok"])
    request = dict(op="turn:42:fixture", frames=[frame, dict(frame, stage_id="preview_b"), action],
                   branch_manifest=dict(schema="delivery_branches/1", groups=[]))
    coord._preflight(request, state)
    for mutation in [dict(slot="card"), dict(mode="edit"), dict(buttons=action["buttons"]),
                     dict(buttons=[[dict(text="URL", url="https://example.com")]])]:
        with pytest.raises(Refusal):
            coord._preflight(dict(request, frames=[dict(frame, **mutation)]), state)
    with pytest.raises(Refusal, match="action keyboard must be last"):
        coord._preflight(dict(request, frames=[action, frame]), state)


def test_acceptance_and_uncertainty_never_release_pick(tmp_path):
    coord, sent = coordinator(tmp_path, responses=[TimeoutError()])
    def send(candidate):
        return run(coord.push_preview(42, candidate["candidate_id"], candidate["text"], candidate["buttons"]))[1]
    with pytest.raises(Refusal, match="receipt"):
        workflow.deliver_plan(tmp_path, SAMPLES, send)
    assert len(sent) == 1
    with pytest.raises(Refusal):
        workflow.deliver_plan(tmp_path, SAMPLES, send)
    assert len(sent) == 1


def test_partial_permanent_failure_isolated_without_retry(tmp_path):
    coord, sent = coordinator(tmp_path, responses=[
        dict(ok=True, result=dict(message_id=101)), dict(ok=False, error_code=400, description="rejected")])
    def send(candidate):
        return run(coord.push_preview(42, candidate["candidate_id"], candidate["text"], candidate["buttons"]))[1]
    with pytest.raises(Refusal, match="receipt"):
        workflow.deliver_plan(tmp_path, SAMPLES, send)
    assert len(sent) == 2
    with pytest.raises(Refusal, match="receipt"):
        workflow.deliver_plan(tmp_path, SAMPLES, send)
    assert [p["frame"]["text"] for p in sent] == [c["text"] for c in SAMPLES["candidates"]]


def test_accepted_but_undelivered_and_preflight_whole_set(tmp_path):
    send = Mock(return_value=dict(ok=True, accepted=True, delivered=False))
    with pytest.raises(Refusal):
        workflow.deliver_plan(tmp_path, SAMPLES, send)
    assert send.call_count == 1
    broken = copy.deepcopy(SAMPLES)
    broken["candidates"][1]["buttons"] = [[dict(text="missing action")]]
    send.reset_mock()
    with pytest.raises(Refusal):
        workflow.deliver_plan(tmp_path, broken, send)
    send.assert_not_called()


def test_real_pending_notice_is_not_delivery(tmp_path, monkeypatch):
    coord, sent = coordinator(tmp_path, active=True)
    admitted = run(coord.enqueue_notice(42, "fixture", "held", dict(text="Earlier notice", buttons=[])))
    assert admitted["accepted"] and not admitted["delivered"]
    release = coord._notice_released
    monkeypatch.setattr(coord, "_notice_released", lambda state, chat, notice:
                        False if notice["intended"]["producer"] == "fixture" else release(state, chat, notice))
    def send(candidate):
        rc, reply = run(coord.push_preview(42, candidate["candidate_id"], candidate["text"], candidate["buttons"]))
        assert rc == 5 and reply["accepted"] and not reply["delivered"] and reply["message_id"] is None
        return reply
    with pytest.raises(Refusal, match="receipt"):
        workflow.deliver_plan(tmp_path, SAMPLES, send)
    assert sent == []


@pytest.mark.parametrize("resolution", ["ok", "absent"])
def test_signed_reconciliation_before_uncertain_resend(tmp_path, resolution):
    coord, sent = coordinator(tmp_path, responses=[TimeoutError()])
    sample = SAMPLES["candidates"][0]
    assert run(coord.push_preview(42, sample["candidate_id"], sample["text"], sample["buttons"]))[0] == 6
    path, plan = coord.plans(42)[0]
    stage = plan["stages"][0]
    K = stage["receipt"]["K"]
    observed_id = 777 if resolution == "ok" else None
    observation = dict(kind="manual_observation", K=K, seq=1, payload_digest=stage["payload_digest"],
                       target=None, resolution=resolution, message_id=observed_id)
    record = dict(authority_id="fixture", K=K, seq=1, plan_digest=plan["plan_digest"],
                  payload_digest=stage["payload_digest"], target=None, resolution=resolution, message_id=observed_id,
                  evidence_digest=digest(observation), quiescence=True, issued_at_ms=1000)
    key = b"recovery-key-different-from-coordinator" * 2
    authority = coord.base / "recovery-authorities/fixture.key"
    authority.parent.mkdir()
    authority.write_bytes(key)
    authority.chmod(0o600)
    record["auth"] = dict(mac=mac(key, record))
    atomic(coord.base / "recovery/evidence" / (digest(observation).split(":")[1] + ".json"), observation)
    atomic(coord.base / "recovery/observed.json", dict(record=record, evidence=observation))
    run(coord.reconcile(42, 0, K["op"], K["stage_id"], "observed"))
    rc, result = run(coord.push_preview(42, sample["candidate_id"], sample["text"], sample["buttons"]))
    assert rc == 0 and result["message_id"] == (777 if resolution == "ok" else 102)
    assert len(sent) == (1 if resolution == "ok" else 2)


def test_shell_preview_uses_strict_coordinator_not_curl(tmp_path):
    # Execute a sandbox copy of the real shell entry, with actual coordinator
    # persistence and a fake Telegram transport. No token or network is usable.
    instance = tmp_path / "instance"
    (instance / "routines").mkdir(parents=True)
    (instance / "bridge").mkdir()
    (instance / ".telegram_bot").mkdir()
    # conftest supplies the hermetic test token before this module imports.
    (instance / ".telegram_bot/.env").write_text(
        "TELEGRAM_BOT_TOKEN=" + os.environ["TELEGRAM_BOT_TOKEN"] + "\nALLOWED_USER_IDS=42\n")
    entry = instance / "routines/push.sh"
    entry.write_bytes((ROOT / "routines/push.sh").read_bytes())
    (instance / "bridge/live_surface.py").write_text("# wrapper invokes the source module\n")
    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text("#!" + sys.executable + "\n" +
        "import json, pathlib, sys\n" +
        "sys.path.insert(0, " + repr(str(ROOT)) + ")\n" +
        "from bridge import live_surface as live\n" +
        "async def transport(payload):\n" +
        "    pathlib.Path(" + repr(str(tmp_path / "sent.json")) + ").write_text(json.dumps(payload))\n" +
        "    return dict(ok=True, result=dict(message_id=991, chat=dict(id=42)))\n" +
        "live.cli_transport = lambda root: transport\n" +
        "sys.argv.pop(1)\n" +
        "raise SystemExit(live.main())\n")
    wrapper.chmod(0o755)
    sample = SAMPLES["candidates"][0]
    env = dict(os.environ, BRIDGE_PYTHON=str(wrapper), PUSH_GUARD_OVERRIDE="send-anyway")
    proc = subprocess.run(["bash", str(entry), "--preview", sample["candidate_id"],
        "--text", sample["text"], "--buttons-json", json.dumps(sample["buttons"])],
        env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["message_id"] == 991
    sent = json.loads((tmp_path / "sent.json").read_text())
    assert sent["frame"]["text"] == sample["text"]
    assert len(sent["frame"]["buttons"]) == 2
    expected = tmp_path / "expected"
    expected.write_text("different screen")
    proc = subprocess.run(["bash", str(entry), "--preview", "mismatch", "--text", "Screen",
                           "--expect-file", str(expected)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 5
    assert json.loads((tmp_path / "sent.json").read_text()) == sent
