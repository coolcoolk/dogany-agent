"""DGN-1843: a complete plan with blocked items is a plan, not a lookup
failure.

The planner exits 2 both for "an item is blocked, the rest was planned"
and for "aborted, no plan"; only the former ends its emit with PLAN-END.
Measured 2026-10-04: one instance's pack receipt read EVIDENCE-MISMATCH and
every agent's session-start notice said the update list could not be
loaded.  Here the planner is a stub that writes exactly those two emits.
"""

# ESTATE:BEGIN DGN-1417 /update and the notice are estate-only regions of bot.py
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod

OWNER_ID = 4242

COMPLETE_BLOCKED = (
    "PLAN-EMIT-V1\n"
    "BLOCKED\tPACK\tkit_x\tFAIL-EVIDENCE\thealth:/x/health\n"
    "PLAN-END\t0\t1\n"
)
ABORTED = "PLAN-EMIT-V1\n"


def _machinery(tmp_path, emit_body, rc):
    root = tmp_path / "machinery"
    script = root / "scripts" / "pack" / "update_plan.sh"
    script.parent.mkdir(parents=True)
    body = tmp_path / "emit-body"
    body.write_text(emit_body)
    script.write_text(
        "#!/bin/bash\n"
        "cp '%s' \"$DOGANY_PLAN_EMIT\"\n"
        "echo '[plan] FAIL: kit_x: health evidence' >&2\n"
        "exit %d\n" % (body, rc))
    script.chmod(0o755)
    return root


def _bridge(tmp_path, monkeypatch, emit_body, rc, tool_out):
    inst = tmp_path / "inst"
    inst.mkdir()
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", inst)
    monkeypatch.setattr(bot_mod, "UPDATE_OFFER_RETRY_S", 0)
    root = _machinery(tmp_path, emit_body, rc)
    monkeypatch.setattr(bot_mod, "_update_machinery_root", lambda: root)
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    bridge.application.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    calls = []

    async def tool(*args):
        calls.append(args)
        return tool_out
    monkeypatch.setattr(bridge, "_update_tool", tool)
    return bridge, calls


async def _drain():
    for _ in range(20):
        await asyncio.sleep(0)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_complete_plan_with_blocked_item_is_a_plan(tmp_path, monkeypatch):
    bridge, _ = _bridge(tmp_path, monkeypatch, COMPLETE_BLOCKED, 2, (0, {}))
    plan = await bridge._update_plan()
    assert plan.read_text() == COMPLETE_BLOCKED


@pytest.mark.asyncio
async def test_aborted_planner_is_still_a_lookup_failure(tmp_path, monkeypatch):
    bridge, _ = _bridge(tmp_path, monkeypatch, ABORTED, 2, (0, {}))
    with pytest.raises(RuntimeError):
        await bridge._update_plan()


@pytest.mark.asyncio
async def test_other_nonzero_rc_is_a_lookup_failure_even_with_plan_end(tmp_path, monkeypatch):
    bridge, _ = _bridge(tmp_path, monkeypatch, COMPLETE_BLOCKED, 1, (0, {}))
    with pytest.raises(RuntimeError):
        await bridge._update_plan()


@pytest.mark.asyncio
async def test_notice_stays_quiet_when_only_another_member_is_blocked(tmp_path, monkeypatch):
    # Replay of 2026-10-04: nothing to apply, one member blocked -> no
    # notice at all on this agent (never the lookup-failure line).
    bridge, calls = _bridge(tmp_path, monkeypatch, COMPLETE_BLOCKED, 2,
                            (0, {"action": "none", "ledger": "l", "revision": 1, "signature": "", "blocked": []}))
    await bridge._update_offer_run(OWNER_ID)
    await _drain()
    assert [c[0] for c in calls] == ["offer"]
    bridge.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_blocked_item_is_logged_not_sent(tmp_path, monkeypatch, caplog):
    blocked = [{"kind": "PACK", "id": "kit_x", "status": "FAIL-EVIDENCE"}]
    bridge, _ = _bridge(tmp_path, monkeypatch, COMPLETE_BLOCKED, 2,
                        (0, {"action": "none", "ledger": "l", "revision": 1, "signature": "", "blocked": blocked}))
    with caplog.at_level("WARNING"):
        await bridge._update_offer_run(OWNER_ID)
        await _drain()
    bridge.application.bot.send_message.assert_not_awaited()
    assert "kit_x" in caplog.text and "FAIL-EVIDENCE" in caplog.text


@pytest.mark.asyncio
async def test_unsolicited_aborted_planner_logs_without_notice(tmp_path, monkeypatch, caplog):
    bridge, calls = _bridge(tmp_path, monkeypatch, ABORTED, 2, (0, {"action": "none"}))
    await bridge._update_offer_run(OWNER_ID)
    await _drain()
    assert calls == []
    bridge.application.bot.send_message.assert_not_awaited()
    assert "update notice lookup failed" in caplog.text
# ESTATE:END
