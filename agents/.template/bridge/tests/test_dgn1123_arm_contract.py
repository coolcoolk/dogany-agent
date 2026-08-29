"""DGN-1123: arm path contract -- constants collapsed, consume made loud.

What this file locks in:

  (0) POSITIVE SELF-VERIFICATION ORDER (DGN-1122 discipline): before any
      test claims "the arm was consumed", test (a) proves the probe used
      for that claim can DETECT a not-consumed arm. Deletion is asserted
      by CONTENT (file readable / not readable), never by unlink's return
      (unlink returns nothing).
  (a) detection proof: an armed-but-unconsumed file is visible to the probe.
  (b) consume_arm removes the file -- asserted with the SAME probe as (a).
  (c) absence is converged: consume of a never-armed id -> False, no raise.
  (d) any other unlink failure is LOUD: consume_arm RAISES (no silent
      no-op -- the DGN-1123 failure direction is "button works, state
      never clears", so quiet convergence on a real error is the bug).
  (e) bot fire path: a consume FAILURE aborts the fire with IDRILL_ERROR
      (loud), the declared cmd is NOT fired, the arm file survives.
  (f) bot fire path: consume finding the file already absent (double-tap
      race window) -> IDRILL_ARM_EXPIRED, no fire.
  (g) bot fire path: normal consume -> fire proceeds, file gone (zero
      false positives on the current layout).
  (h) single path authority: bot.py holds NO arm path literal; the
      declared ARM_SUBDIR matches the path prose in
      bridge/IDRILL-ARM-CONTRACT.md section 1 (code<->contract binding).
  (i) depth-2 assumption is asserted in artifacts (3-component subdir).
"""

import asyncio
import inspect
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import bridge.artifacts as artifacts
import bridge.bot as bot_mod
from bridge.bot import TelegramBot
from bridge import messages


def run(coro):
    return asyncio.run(coro)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.text_edits = []

    async def edit_message_text(self, text, **kwargs):
        self.text_edits.append(text)

    async def edit_message_reply_markup(self, **kwargs):
        pass


@pytest.fixture
def bot():
    return TelegramBot.__new__(TelegramBot)


@pytest.fixture
def arm_dir(tmp_path):
    cb = artifacts.arm_dir_for(tmp_path)
    cb.mkdir(parents=True)
    with patch.object(bot_mod, "PROJECT_ROOT", tmp_path):
        yield cb


ARM_ID = "abcd1234"
ARM_CONTENT = {
    "cmd": ["/bin/true", "{1}"],
    "step_buttons": {"1": [["a", "a"], ["b", "b"]],
                     "2": [["1", "1"], ["2", "2"]]},
    "step_validate": {"1": r"^[ab]$", "2": r"^[12]$"},
}


def write_arm(root, arm_id=ARM_ID):
    path = artifacts.arm_dir_for(root) / arm_id
    path.write_text(json.dumps(ARM_CONTENT), encoding="utf-8")
    return path


def arm_present(root, arm_id=ARM_ID):
    """The consumption probe: CONTENT-based, not unlink-return-based.

    True iff the arm file exists AND parses back to the written content.
    Every "was consumed" assertion in this file is the negation of THIS
    probe, so test (a) proving the probe detects an unconsumed arm is the
    positive self-verification for all of them.
    """
    path = artifacts.arm_dir_for(root) / arm_id
    if not path.exists():
        return False
    return json.loads(path.read_text(encoding="utf-8")) == ARM_CONTENT


# ---------------------------------------------------------------------------
# (a) DETECTION PROOF -- must precede every deletion claim
# ---------------------------------------------------------------------------

def test_a_probe_detects_unconsumed_arm(tmp_path, arm_dir):
    """An armed file that nobody consumed IS visible to the probe."""
    write_arm(tmp_path)
    assert arm_present(tmp_path), (
        "probe failed to see an unconsumed arm -- every consumption "
        "assertion below would be vacuous"
    )


# ---------------------------------------------------------------------------
# (b) consume removes -- asserted with the proven probe
# ---------------------------------------------------------------------------

def test_b_consume_arm_removes_file(tmp_path, arm_dir):
    write_arm(tmp_path)
    assert arm_present(tmp_path)  # precondition via the (a)-proven probe
    assert artifacts.consume_arm(tmp_path, ARM_ID) is True
    assert not arm_present(tmp_path)


# ---------------------------------------------------------------------------
# (c) absence is converged (False, quiet)
# ---------------------------------------------------------------------------

def test_c_consume_absent_is_converged_false(tmp_path, arm_dir):
    assert artifacts.consume_arm(tmp_path, "beefbeef") is False


# ---------------------------------------------------------------------------
# (d) non-absence unlink failure is LOUD (raises)
# ---------------------------------------------------------------------------

def test_d_consume_error_raises_not_converges(tmp_path, arm_dir):
    """Path error injected: the arm 'file' is a non-empty directory, so
    unlink fails with an OSError that is NOT FileNotFoundError. The old
    blanket `except OSError: pass` called this 'converged'; now it raises.
    """
    bad = artifacts.arm_dir_for(tmp_path) / ARM_ID
    bad.mkdir()
    (bad / "child").write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        artifacts.consume_arm(tmp_path, ARM_ID)
    assert bad.exists()  # nothing was quietly half-done


def test_d2_consume_containment_escape_raises(tmp_path, arm_dir):
    """A containment escape on a DELETE is never 'converged'."""
    with pytest.raises(OSError):
        artifacts.consume_arm(tmp_path, "../escape")


# ---------------------------------------------------------------------------
# (e) bot fire path: consume failure -> LOUD abort, no fire, file survives
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores dir permissions")
def test_e_fire_aborts_loudly_when_consume_fails(bot, tmp_path, arm_dir):
    write_arm(tmp_path)
    fire = AsyncMock(return_value=True)
    arm_dir.chmod(0o500)  # read OK, unlink blocked -> consume-only breakage
    try:
        with patch.object(bot, "_idrill_fire_cmd_n", fire):
            q = FakeQuery(f"idrill:{ARM_ID}:2:1")
            run(bot._handle_idrill_callback(q, q.data))
    finally:
        arm_dir.chmod(0o700)
    fire.assert_not_awaited()  # no fire on a broken consume
    assert q.text_edits == [messages.IDRILL_ERROR]  # loud, user-visible
    assert arm_present(tmp_path)  # arm untouched -- probe from (a)


# ---------------------------------------------------------------------------
# (f) bot fire path: already-absent at consume time -> EXPIRED, no fire
# ---------------------------------------------------------------------------

def test_f_absent_at_consume_time_expires_without_fire(bot, tmp_path, arm_dir):
    """Race window: the arm vanishes between this tap's re-read and its
    consume (the concurrent tap consumed it and owns the fire)."""
    fire = AsyncMock(return_value=True)
    with patch.object(bot, "_idrill_read_arm", lambda arm_id: dict(ARM_CONTENT)):
        with patch.object(bot, "_idrill_fire_cmd_n", fire):
            q = FakeQuery(f"idrill:{ARM_ID}:2:1")
            run(bot._handle_idrill_callback(q, q.data))
    fire.assert_not_awaited()
    assert q.text_edits == [messages.IDRILL_ARM_EXPIRED]


# ---------------------------------------------------------------------------
# (g) normal consume: fire proceeds, file gone -- zero false positives
# ---------------------------------------------------------------------------

def test_g_normal_consume_fires_and_clears(bot, tmp_path, arm_dir):
    write_arm(tmp_path)
    assert arm_present(tmp_path)
    fire = AsyncMock(return_value=True)
    with patch.object(bot, "_idrill_fire_cmd_n", fire):
        q = FakeQuery(f"idrill:{ARM_ID}:2:1")
        run(bot._handle_idrill_callback(q, q.data))
    fire.assert_awaited_once()
    assert not arm_present(tmp_path)  # consumed -- probe from (a)
    assert messages.IDRILL_ERROR not in q.text_edits


# ---------------------------------------------------------------------------
# (h) single path authority + code<->contract binding
# ---------------------------------------------------------------------------

def test_h_bot_holds_no_arm_path_literal():
    src = inspect.getsource(bot_mod)
    assert ".idrill-arm" not in src, (
        "bot.py regrew an arm path literal; the single authority is "
        "bridge.artifacts.ARM_SUBDIR (see IDRILL-ARM-CONTRACT.md)"
    )


def test_h2_arm_subdir_matches_contract_prose():
    contract = (
        Path(artifacts.__file__).resolve().parent / "IDRILL-ARM-CONTRACT.md"
    )
    text = contract.read_text(encoding="utf-8")
    declared = "PROJECT_ROOT/" + "/".join(artifacts.ARM_SUBDIR)
    assert declared in text, (
        "artifacts.ARM_SUBDIR no longer matches the path declared in "
        "IDRILL-ARM-CONTRACT.md section 1 -- one side moved without the "
        "other"
    )


# ---------------------------------------------------------------------------
# (i) depth-2 assumption stays asserted
# ---------------------------------------------------------------------------

def test_i_depth2_assumption_asserted():
    assert len(artifacts.ARM_SUBDIR) == 3
    assert artifacts.ARM_SUBDIR[-1] == ".idrill-arm"
    # The guard must live in the module source as an import-time check, not
    # only in this test (a broken relocation must fail at BOOT).
    src = inspect.getsource(artifacts)
    assert re.search(r"if len\(ARM_SUBDIR\) != 3", src)
