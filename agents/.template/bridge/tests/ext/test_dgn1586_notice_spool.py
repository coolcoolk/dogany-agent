"""DGN-1586: shown-is-recorded-at-emission -> delivery accounting.

Covers the consumer half of the hardened spec:
  - spec 3.2 kind-split validity (pending-restart resolves ONLY on
    snapshot_fw == built; fw-update on version <= built; unknown snapshot
    holds, never resolves).
  - spec 3.4/3.5 finalize synthesis: at most ONE oldest pending, attempt
    pre-persisted, original response preserved on failure.
  - spec 3.6 promotion: delivered comes ONLY from the send/edit success of
    the message that carried the notice text (last body segment / forced
    stream edit / delete+resend carrier); a first-segment success or a
    turn with no carrier never promotes.
  - spec 5 attempts cap -> expired(attempts_exhausted).
  - field verification steps 1-3 (spec 6) in synthetic form: owner-turn
    positive, automatic-turn negative (the owner-protocol machine proof),
    self-invalidation.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import telegram.error

import bridge.bot as bot_mod
import bridge.sdk_bridge as sdk_mod
from bridge import notice_spool
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState
from claude_agent_sdk import ResultMessage

NOTICE_BODY = "notice header line\nnotice action line"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / ".telegram_bot" / "state").mkdir(parents=True)
    monkeypatch.setattr(sdk_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _write_conf(root: Path, built: str) -> None:
    (root / ".instance.conf").write_text(
        "DOGANY_FW_VERSION={}\n".format(built), encoding="utf-8")


def _write_snapshot(root: Path, fw: str) -> None:
    (root / ".telegram_bot" / "runtime-snapshot.json").write_text(
        json.dumps({"schema": 1, "pid": os.getpid(), "fw_version": fw}),
        encoding="utf-8")


def _rec(kind, version, status="pending", attempts=0, body=NOTICE_BODY,
         first_pending_ts=None, **over):
    rec = {
        "id": "ntc-test-{}-{}".format(kind, version),
        "kind": kind,
        "version": version,
        "audience": "owner",
        "first_pending_ts": first_pending_ts or (time.time() - 60),
        "attempts": attempts,
        "status": status,
        "expired_reason": None,
        "delivered_ts": None,
        "chat_id": None,
        "message_id": None,
        "body_oneline": body,
        "body_fold": "",
        "context_injected_ts": None,
        "origin": "version-check",
    }
    rec.update(over)
    return rec


def _seed(root: Path, records) -> None:
    path = notice_spool.spool_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _load(root: Path):
    records, _bad = notice_spool.load_records(root)
    return records


def _by_id(root: Path, notice_id: str):
    for rec in _load(root):
        if rec.get("id") == notice_id:
            return rec
    return None


# ---------------------------------------------------------------------------
# reserve_for_synthesis: kind validity + attempt reservation
# ---------------------------------------------------------------------------

def test_reserve_picks_pending_and_prepersists_attempt(root):
    _write_conf(root, "2.5.0")
    _seed(root, [_rec("fw-update", "2.6.0")])
    res = notice_spool.reserve_for_synthesis(root)
    assert res is not None and res["record"] is not None
    assert res["attempt"] == 1
    stored = _by_id(root, res["record"]["id"])
    assert stored["attempts"] == 1          # pre-persisted BEFORE any append
    assert stored["status"] == "pending"    # delivered only via real receipt


def test_reserve_self_invalidation_expires_resolved(root):
    # Verification step 3 (spec 6): pending version <= built -> no notice,
    # record expired(resolved).
    _write_conf(root, "2.6.0")
    _seed(root, [_rec("fw-update", "2.6.0"), _rec("fw-update", "2.4.0")])
    res = notice_spool.reserve_for_synthesis(root)
    assert res is None or res["record"] is None
    for rec in _load(root):
        assert rec["status"] == "expired"
        assert rec["expired_reason"] == "resolved"


def test_pending_restart_survives_fw_update_predicate(root):
    # Grill MAJOR-1 / spec 3.2: kind=pending-restart carries version ==
    # built by construction; the fw-update expiry predicate would kill it
    # at first synthesis. It must synthesize while snapshot_fw < built.
    _write_conf(root, "2.6.0")
    _write_snapshot(root, "2.5.0")
    _seed(root, [_rec("pending-restart", "2.6.0")])
    res = notice_spool.reserve_for_synthesis(root)
    assert res is not None and res["record"] is not None
    assert res["record"]["kind"] == "pending-restart"


def test_pending_restart_resolves_only_on_snapshot_equal(root):
    # Verification step 5 (spec 6): resolution is EXACTLY
    # snapshot_fw == built.
    _write_conf(root, "2.6.0")
    _write_snapshot(root, "2.6.0")
    _seed(root, [_rec("pending-restart", "2.6.0")])
    res = notice_spool.reserve_for_synthesis(root)
    assert res is None or res["record"] is None
    rec = _load(root)[0]
    assert rec["status"] == "expired" and rec["expired_reason"] == "resolved"


def test_pending_restart_unknown_snapshot_holds(root):
    # spec 3.2: snapshot absent/corrupt/stale is NOT resolution evidence --
    # pending preserved, this turn synthesizes nothing.
    _write_conf(root, "2.6.0")  # no snapshot file at all
    _seed(root, [_rec("pending-restart", "2.6.0")])
    res = notice_spool.reserve_for_synthesis(root)
    assert res is None or res["record"] is None
    rec = _load(root)[0]
    assert rec["status"] == "pending" and rec["attempts"] == 0


def test_reserve_oldest_first_single_pick(root):
    _write_conf(root, "2.5.0")
    old = _rec("fw-update", "2.6.0", first_pending_ts=100.0, id="ntc-old")
    new = _rec("pending-restart", "2.5.0", first_pending_ts=200.0,
               id="ntc-new")
    _write_snapshot(root, "2.4.0")
    _seed(root, [new, old])
    res = notice_spool.reserve_for_synthesis(root)
    assert res["record"]["id"] == "ntc-old"
    assert _by_id(root, "ntc-new")["attempts"] == 0  # max ONE per turn


def test_reserve_newer_pending_shelves_lower_version(root):
    # spec 3.4: oldest-first must not resurrect a lower fw-update target
    # while a strictly newer same-kind pending exists.
    _write_conf(root, "2.5.0")
    low = _rec("fw-update", "2.6.0", first_pending_ts=100.0, id="ntc-low")
    high = _rec("fw-update", "2.7.0", first_pending_ts=200.0, id="ntc-high")
    _seed(root, [low, high])
    res = notice_spool.reserve_for_synthesis(root)
    assert res["record"]["id"] == "ntc-high"


def test_reserve_exhaustion_expires_and_reports(root):
    _write_conf(root, "2.5.0")
    _seed(root, [_rec("fw-update", "2.6.0", attempts=3, id="ntc-worn")])
    res = notice_spool.reserve_for_synthesis(root)
    assert res is not None and res["record"] is None
    assert [r["id"] for r in res["exhausted"]] == ["ntc-worn"]
    rec = _by_id(root, "ntc-worn")
    assert rec["status"] == "expired"
    assert rec["expired_reason"] == "attempts_exhausted"


# ---------------------------------------------------------------------------
# promote_delivered: receipt semantics
# ---------------------------------------------------------------------------

def test_promote_records_receipt_and_is_idempotent(root):
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-p")])
    assert notice_spool.promote_delivered(root, "ntc-p", 111, 555, "send",
                                          attempt=1, related_message_ids=[554, 555])
    rec = _by_id(root, "ntc-p")
    assert rec["status"] == "delivered"
    assert rec["chat_id"] == 111 and rec["message_id"] == 555
    assert rec["delivery_method"] == "send"
    first_ts = rec["delivered_ts"]
    # Re-processing the same receipt is a no-op; a second receipt never
    # overwrites the first.
    assert notice_spool.promote_delivered(root, "ntc-p", 111, 999, "send")
    rec = _by_id(root, "ntc-p")
    assert rec["message_id"] == 555 and rec["delivered_ts"] == first_ts


def test_late_receipt_after_exhaustion_records_real_delivery(root):
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-late", status="expired",
                      expired_reason="attempts_exhausted")])
    assert notice_spool.promote_delivered(root, "ntc-late", 111, 42, "send")
    rec = _by_id(root, "ntc-late")
    assert rec["status"] == "delivered" and rec["late_receipt"] is True


def test_receipt_for_unknown_id_promotes_nothing(root):
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-a")])
    assert not notice_spool.promote_delivered(root, "ntc-other", 111, 42, "send")
    assert _by_id(root, "ntc-a")["status"] == "pending"


# ---------------------------------------------------------------------------
# finalize seam: owner-turn synthesis (verification step 1, bridge half)
# ---------------------------------------------------------------------------

def _make_state():
    client = MagicMock()
    client.query = AsyncMock()
    st = _UserStreamState(client=client, model=None)
    st.last_chat_id = 111
    st.proactive_push = AsyncMock()
    st.last_session_id = "sess-1586"
    return st


def _make_req(last_texts):
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    loop.close()
    req = _PendingRequest(
        user_id=42, chat_id=111, model=None, requested_session_id=None,
        permission_callback=None, typing_callback=None, future=fut,
        user_message="hello",
    )
    req.last_assistant_texts = last_texts
    return req


def _result_msg(is_error=False, result_text=None):
    return ResultMessage(
        subtype="success", duration_ms=5, duration_api_ms=5,
        is_error=is_error, num_turns=1, session_id="sess-1586",
        result=result_text,
    )


def _finalize(bridge, state, req, msg):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            bridge._finalize_result(req.user_id, state, req, msg))
    finally:
        loop.close()


def test_owner_finalize_appends_notice_with_metadata(root):
    _write_conf(root, "2.5.0")
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-fin")])
    bridge = SdkBridge()
    state = _make_state()
    req = _make_req(["model answer body"])
    _finalize(bridge, state, req, _result_msg())
    resp = req.future.result()
    assert resp.success is True
    assert resp.content.endswith(NOTICE_BODY)     # tail position
    assert resp.content.startswith("model answer body")
    assert resp.notice_id == "ntc-fin"
    assert resp.notice_attempt == 1
    assert resp.notice_kind == "fw-update"
    assert resp.notice_version == "2.6.0"
    assert resp.notice_text == NOTICE_BODY
    stored = _by_id(root, "ntc-fin")
    assert stored["attempts"] == 1 and stored["status"] == "pending"


def test_error_finalize_reserves_nothing(root):
    _write_conf(root, "2.5.0")
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-err")])
    bridge = SdkBridge()
    state = _make_state()
    req = _make_req([])
    _finalize(bridge, state, req, _result_msg(is_error=True,
                                              result_text="boom"))
    resp = req.future.result()
    assert resp.success is False and resp.notice_id is None
    assert _by_id(root, "ntc-err")["attempts"] == 0


def test_spool_failure_keeps_original_response(root, monkeypatch):
    # spec 5 row 1: spool read/synthesis failure -> original response kept.
    _write_conf(root, "2.5.0")
    _seed(root, [_rec("fw-update", "2.6.0")])

    def _boom(_root):
        raise RuntimeError("spool exploded")

    monkeypatch.setattr(sdk_mod.notice_spool, "reserve_for_synthesis", _boom)
    bridge = SdkBridge()
    state = _make_state()
    req = _make_req(["the answer"])
    _finalize(bridge, state, req, _result_msg())
    resp = req.future.result()
    assert resp.success is True
    assert resp.content == "the answer"
    assert resp.notice_id is None


# ---------------------------------------------------------------------------
# automatic turns (verification step 2 -- the owner-protocol machine proof)
# ---------------------------------------------------------------------------

def test_injected_turn_output_never_touches_the_spool(root):
    # A session-inbox injected turn's output flows through _flush_proactive
    # (no pending request). It must synthesize nothing, consume no attempt,
    # promote nothing -- the seeded pending stays byte-identical.
    _write_conf(root, "2.5.0")
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-auto")])
    before = (notice_spool.spool_path(root)).read_text(encoding="utf-8")
    bridge = SdkBridge()
    state = _make_state()
    state.proactive_texts = ["background result for the injected turn"]
    pushed = []

    async def _push(chat_id, content, has_options=False,
                    classifier_injected=False):
        pushed.append(content)

    state.proactive_push = _push
    asyncio.run(bridge._flush_proactive(42, state))
    assert len(pushed) == 1
    assert NOTICE_BODY not in pushed[0]
    after = (notice_spool.spool_path(root)).read_text(encoding="utf-8")
    assert after == before


def test_send_smart_without_notice_meta_promotes_nothing(root):
    # The cron/proactive rail calls _send_smart with NO notice metadata --
    # delivery accounting must not move even though a pending exists.
    _write_conf(root, "2.5.0")
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-cron")])
    before = (notice_spool.spool_path(root)).read_text(encoding="utf-8")
    bot = _make_tbot()
    asyncio.run(bot._send_smart(111, "a cron push body",
                                rail=bot_mod.RAIL_MODEL))
    after = (notice_spool.spool_path(root)).read_text(encoding="utf-8")
    assert after == before
    assert not (root / ".telegram_bot" / "outbound-ledger.jsonl").exists()


# ---------------------------------------------------------------------------
# bot seat: carrier receipt promotion (spec 3.6)
# ---------------------------------------------------------------------------

def _make_tbot():
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    tg = MagicMock()
    bot.application.bot = tg
    tg.delete_message = AsyncMock()
    tg.edit_message_text = AsyncMock()
    bot._last_incoming_mid = {}
    _mid = {"n": 900}

    async def _send_message(chat_id, text, parse_mode=None,
                            link_preview_options=None, reply_markup=None):
        _mid["n"] += 1
        return SimpleNamespace(message_id=_mid["n"], text=text)

    tg.send_message = AsyncMock(side_effect=_send_message)
    return bot


def _meta(notice_id="ntc-carrier", attempt=1):
    return {"id": notice_id, "attempt": attempt, "kind": "fw-update",
            "version": "2.6.0", "text": NOTICE_BODY}


def test_send_smart_promotes_last_segment_as_carrier(root):
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-carrier", attempts=1)])
    bot = _make_tbot()
    body = "the model answer\n" + NOTICE_BODY
    asyncio.run(bot._send_smart(111, body, rail=bot_mod.RAIL_MODEL,
                                notice_meta=_meta()))
    rec = _by_id(root, "ntc-carrier")
    assert rec["status"] == "delivered"
    assert rec["chat_id"] == 111
    assert isinstance(rec["message_id"], int)
    assert rec["delivery_method"] == "send"
    ledger = (root / ".telegram_bot" / "outbound-ledger.jsonl").read_text(
        encoding="utf-8")
    row = json.loads(ledger.strip().splitlines()[-1])
    assert row["notice_id"] == "ntc-carrier"
    assert row["message_id"] == rec["message_id"]
    assert row["audience"] == "owner"
    assert row["kind"] == "fw-update" and row["version"] == "2.6.0"


def test_send_failure_leaves_no_delivered(root):
    # Verification "split send" analogue: the send that would carry the
    # notice fails (HTML and plain fallback both) -> delivered stays 0,
    # pending preserved for the next owner turn.
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-fail", attempts=1)])
    bot = _make_tbot()
    bot.application.bot.send_message = AsyncMock(
        side_effect=RuntimeError("network down"))
    with pytest.raises(RuntimeError):
        asyncio.run(bot._send_smart(111, "body\n" + NOTICE_BODY,
                                    rail=bot_mod.RAIL_MODEL,
                                    notice_meta=_meta("ntc-fail")))
    rec = _by_id(root, "ntc-fail")
    assert rec["status"] == "pending" and rec["message_id"] is None


def test_streamed_notice_forces_real_edit_and_promotes(root):
    # spec 3.6 stream row: plain-text display would normally take the
    # no-call `True` skip; with a notice the edit MUST actually run and the
    # edited draft is the receipt.
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-edit", attempts=1)])
    bot = _make_tbot()
    display = "plain answer\n" + NOTICE_BODY
    asyncio.run(bot._send_smart(
        111, display, streamed=True, draft_message_ids=[77],
        rail=bot_mod.RAIL_MODEL, notice_meta=_meta("ntc-edit")))
    bot.application.bot.edit_message_text.assert_awaited_once()
    rec = _by_id(root, "ntc-edit")
    assert rec["status"] == "delivered"
    assert rec["message_id"] == 77 and rec["delivery_method"] == "edit"


def test_streamed_not_modified_counts_as_receipt(root):
    # `message is not modified` against the ACTUAL full-payload edit
    # request is same-content confirmation (spec 3.6) -- and it only
    # arises here because force_edit made the request happen.
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-nm", attempts=1)])
    bot = _make_tbot()
    bot.application.bot.edit_message_text = AsyncMock(
        side_effect=telegram.error.BadRequest("Message is not modified"))
    asyncio.run(bot._send_smart(
        111, "plain answer\n" + NOTICE_BODY, streamed=True,
        draft_message_ids=[78], rail=bot_mod.RAIL_MODEL,
        notice_meta=_meta("ntc-nm")))
    rec = _by_id(root, "ntc-nm")
    assert rec["status"] == "delivered" and rec["message_id"] == 78


def test_edit_failure_promotes_via_resend_carrier(root):
    # spec 3.6: edit failure falls to delete+resend; the NEW carrier's
    # success is the delivery evidence.
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-rs", attempts=1)])
    bot = _make_tbot()
    bot.application.bot.edit_message_text = AsyncMock(
        side_effect=telegram.error.BadRequest("some other failure"))
    asyncio.run(bot._send_smart(
        111, "plain answer\n" + NOTICE_BODY, streamed=True,
        draft_message_ids=[79], rail=bot_mod.RAIL_MODEL,
        notice_meta=_meta("ntc-rs")))
    bot.application.bot.delete_message.assert_awaited()
    rec = _by_id(root, "ntc-rs")
    assert rec["status"] == "delivered"
    assert rec["delivery_method"] == "send"
    assert rec["message_id"] != 79


def test_reply_smart_promotes_on_reply_rail(root):
    # Both rails share the receipt contract (spec 3.6 rail row).
    _seed(root, [_rec("fw-update", "2.6.0", id="ntc-reply", attempts=1)])
    bot = _make_tbot()
    sent_mid = {"n": 300}
    msg = MagicMock()
    msg.chat.id = 111
    msg.message_id = 100
    msg.date = None

    async def _reply_text(text, parse_mode=None, link_preview_options=None,
                          reply_parameters=None, reply_markup=None):
        sent_mid["n"] += 1
        return SimpleNamespace(message_id=sent_mid["n"], text=text)

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    msg.get_bot.return_value = bot.application.bot
    bot._reply_link_id = MagicMock(return_value=None)
    asyncio.run(bot._reply_smart(msg, "answer\n" + NOTICE_BODY,
                                 notice_meta=_meta("ntc-reply")))
    rec = _by_id(root, "ntc-reply")
    assert rec["status"] == "delivered"
    assert rec["message_id"] == sent_mid["n"]
