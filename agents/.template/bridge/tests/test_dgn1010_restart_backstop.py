"""Tests for DGN-1010: restart terminal-state backstop (layer 2).

Incident (2026-08-22 08:00): the owner tapped the DGN-994 restart CTA; the
restart succeeded but the completion push never arrived -- the detached
self_restart.sh worker was reaped together with the old bridge's launchd
process group (AbandonProcessGroup=false kills the whole group), dying
between the SIGTERM and the push. Layer 1 fixes the detach itself (double
fork + os.setsid in self_restart.sh); THIS file covers layer 2: the new
bridge's _restart_backstop_loop, which guarantees every restart tap ends in
exactly ONE terminal notification.

Covers:
  - Marker present + worker pid dead -> backstop claims the marker and sends
    RESTART_BACKSTOP_NOTICE to the owner; marker removed (terminal-closed).
  - Marker present + worker pid ALIVE -> backstop does NOT claim or push
    (the live worker still owns the terminal push); marker stays.
  - No marker (normal boot / dry-run) -> loop exits, no push.
  - Unreadable/garbled marker -> treated as an orphaned restart (fail toward
    "notify the owner", never toward silence).
  - Claim race: marker vanishes between the pid check and the rename
    (worker claimed it in the same instant) -> no push, no error.
  - Marker copy exposure: messages.RESTART_BACKSTOP_NOTICE exists and is
    non-empty in both catalogs (copy itself is owner-approval pending).
  - Layer-absence probe: self_restart.sh without the marker-arming code
    (the exact v1.40.0 regression class) -> one WARN at backstop start;
    intact script -> no WARN.

Same object.__new__ harness as test_dgn997 (no live PTB application).


DGN-2147 part 2 (pytest cases at the end of the file; the self_restart.sh
restart-target resolution and escalation, launchctl stubbed):

DGN-2147 part 2: /restart replaces the bridge process of a minted agent.

Live (owner, 17:34): after a long sleep the in-chat /restart did not clear a
broken bridge. Path traced: bot.py _cmd_restart -> bridge/self_restart.sh
--trigger user -> detached worker -> SIGTERM to the pid launchd lists for
LABEL (mint renders LABEL and the service plist's Label from the same
__AGENT_NAME__ token) -> wait <= 60 s for a new pid + "Bot is running". The
old bridge could outlive that window (a Conflict backoff of up to 300 s did
not watch SIGTERM, fixed in bot.py), and the worker never escalated.

Promises (each case <= 1 s, fixtures only; launchctl is a stub):
  R1 the restart target of a minted agent is the service label its plist
     registers, and the pid launchd lists for it.
  R2 a label still carrying the mint placeholder resolves from the instance
     identity (.instance.conf DOGANY_AGENT_NAME).
  R3 with no launchd row, the target is the pid in the bridge's own instance
     lock -- only while that pid is a live bridge, never a reused pid.
  R4 an old bridge that outlives SIGTERM is replaced: kickstart -k, then the
     worker sees the new pid and sends the completion notice.
"""

import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import messages
from bridge import bot as _bot
from bridge.i18n import en as _en, ko as _ko
import re
import shutil
import subprocess
import sys
import textwrap
import time
import pytest


def _make_bot():
    bot_instance = object.__new__(_bot.TelegramBot)
    bot_instance.application = MagicMock()
    bot_instance.application.bot.send_message = AsyncMock()
    return bot_instance


def _write_marker(state_dir: Path, worker_pid: int, text: str = None) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "restart-pending.marker"
    if text is None:
        text = (
            "ts=1755817242\nreason=test restart\nold_pid=11111\n"
            f"worker_pid={worker_pid}\n"
        )
    marker.write_text(text)
    return marker


class _BackstopHarness(unittest.IsolatedAsyncioTestCase):
    """Shared tmp bot_data_dir + fast loop constants."""

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="dgn1010-")
        self.data_dir = Path(self._tmp.name)
        self.state_dir = self.data_dir / "state"
        self._patches = [
            patch.object(_bot, "RESTART_BACKSTOP_INITIAL_S", 0),
            patch.object(_bot, "RESTART_BACKSTOP_POLL_S", 0.01),
            patch.object(_bot.config, "bot_data_dir", self.data_dir),
            patch.object(_bot.config, "allowed_user_ids", [4242]),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)


class TestBackstopFires(_BackstopHarness):

    async def test_claimed_owner_with_empty_allowlist(self):
        bot = _make_bot()
        _write_marker(self.state_dir, 0, text="not a marker at all\n")
        (self.data_dir / "owner.lock").write_text("42\n", encoding="ascii")
        with patch.object(_bot.config, "allowed_user_ids", []):
            await bot._restart_backstop_loop()
        bot.application.bot.send_message.assert_awaited_once_with(
            chat_id=42, text=messages.RESTART_BACKSTOP_NOTICE
        )

    async def test_no_owner_sends_nothing(self):
        bot = _make_bot()
        _write_marker(self.state_dir, 0, text="not a marker at all\n")
        with patch.object(_bot.config, "allowed_user_ids", []):
            await bot._restart_backstop_loop()
        bot.application.bot.send_message.assert_not_awaited()

    async def test_dead_worker_marker_terminal_closed_with_owner_push(self):
        bot = _make_bot()
        # A pid that is certainly dead: fork a child and reap it.
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        marker = _write_marker(self.state_dir, worker_pid=pid)

        await bot._restart_backstop_loop()

        send = bot.application.bot.send_message
        send.assert_awaited_once()
        kwargs = send.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 4242)
        self.assertEqual(kwargs["text"], messages.RESTART_BACKSTOP_NOTICE)
        self.assertFalse(marker.exists(), "marker must be claimed (removed)")
        # The transient .claimed.<pid> file must not linger either.
        self.assertEqual(list(self.state_dir.iterdir()), [])

    async def test_garbled_marker_still_terminal_closes(self):
        # Unreadable worker_pid -> fail toward notifying, never silence.
        bot = _make_bot()
        marker = _write_marker(self.state_dir, 0, text="not a marker at all\n")

        await bot._restart_backstop_loop()

        bot.application.bot.send_message.assert_awaited_once()
        self.assertFalse(marker.exists())


class TestBackstopHoldsOff(_BackstopHarness):

    async def test_no_marker_no_push(self):
        bot = _make_bot()
        await bot._restart_backstop_loop()
        bot.application.bot.send_message.assert_not_awaited()

    async def test_live_worker_keeps_ownership(self):
        bot = _make_bot()
        # Our own pid is definitely alive.
        marker = _write_marker(self.state_dir, worker_pid=os.getpid())

        task = asyncio.get_running_loop().create_task(
            bot._restart_backstop_loop()
        )
        await asyncio.sleep(0.15)  # several poll rounds
        self.assertFalse(task.done(), "backstop must keep waiting on a live worker")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        bot.application.bot.send_message.assert_not_awaited()
        self.assertTrue(marker.exists(), "live worker's marker must not be claimed")

    async def test_worker_claims_first_no_duplicate(self):
        # Race: marker disappears between the pid check and the rename --
        # exactly what happens when the worker claims in the same instant.
        bot = _make_bot()
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        marker = _write_marker(self.state_dir, worker_pid=pid)

        real_rename = Path.rename

        def _steal_then_rename(self_path, target):
            if self_path == marker:
                marker.unlink(missing_ok=True)  # the worker's mv wins
            return real_rename(self_path, target)

        with patch.object(Path, "rename", _steal_then_rename):
            await bot._restart_backstop_loop()

        bot.application.bot.send_message.assert_not_awaited()


class TestLayerAbsenceProbe(_BackstopHarness):
    """The 2026-08-22 incident recurred for two days because layer 1 (the
    marker writer in self_restart.sh) was reverted by an update while this
    backstop stayed -- neither layer saw the other was gone. The probe is one
    static read per boot: WARN when the sibling script no longer arms the
    marker."""

    async def test_missing_writer_warns_once(self):
        bot = _make_bot()
        fake_pkg = self.data_dir / "pkg"
        fake_pkg.mkdir()
        (fake_pkg / "self_restart.sh").write_text("#!/bin/bash\n# no marker here\n")
        with patch.object(_bot, "PACKAGE_DIR", fake_pkg):
            with self.assertLogs("bridge.bot", level="WARNING") as captured:
                await bot._restart_backstop_loop()
        self.assertTrue(
            any("layer 1 missing" in line for line in captured.output),
            captured.output,
        )

    async def test_intact_writer_no_warn(self):
        bot = _make_bot()
        fake_pkg = self.data_dir / "pkg"
        fake_pkg.mkdir()
        (fake_pkg / "self_restart.sh").write_text(
            '#!/bin/bash\nRESTART_MARKER="x/state/restart-pending.marker"\n'
        )
        with patch.object(_bot, "PACKAGE_DIR", fake_pkg):
            with self.assertNoLogs("bridge.bot", level="WARNING"):
                await bot._restart_backstop_loop()


class TestBackstopCopyExposure(unittest.TestCase):
    """Copy is owner-approval pending (미확정), but the key must exist in both
    catalogs and be exposed through messages.py so the backstop can never
    KeyError into silence."""

    def test_key_in_both_catalogs(self):
        self.assertTrue(_en.STRINGS["restart_backstop_notice"].strip())
        self.assertTrue(_ko.STRINGS["restart_backstop_notice"].strip())

    def test_messages_module_exposes_notice(self):
        self.assertTrue(messages.RESTART_BACKSTOP_NOTICE)


if __name__ == "__main__":
    unittest.main()


BRIDGE_DIR = Path(__file__).resolve().parents[1]
SRC_RESTART = BRIDGE_DIR / "self_restart.sh"
SRC_PLIST = BRIDGE_DIR / "com.telegram-skill-bot.telegram-agent.newbridge.plist"
NAME = "dgn2147t"
needs_posix = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("ps") is None,
                                 reason="bash/ps missing")


def _mint_render(text: str, name: str = NAME) -> str:
    """The token substitution scripts/mint.sh applies (lines without mint:keep)."""
    agent_name_token = "__" "AGENT_NAME" "__"
    return "".join(line if "mint:keep" in line else line.replace(agent_name_token, name)
                   for line in text.splitlines(keepends=True))


def _instance(root: Path, minted: bool = True) -> Path:
    br = root / "bridge"
    (br / "venv" / "bin").mkdir(parents=True)
    for d in ("logs", "state"):
        (root / ".telegram_bot" / d).mkdir(parents=True)
    (root / "routines").mkdir()
    (root / "config").mkdir()
    src = SRC_RESTART.read_text(encoding="utf-8")
    (br / "self_restart.sh").write_text(_mint_render(src) if minted else src, encoding="utf-8")
    (br / "self_restart.sh").chmod(0o755)
    shutil.copy(BRIDGE_DIR / "live_model.py", br / "live_model.py")
    # ESTATE:BEGIN DGN-2147 the instance identity file is estate-only
    (root / ".instance.conf").write_text("DOGANY_AGENT_NAME=%s\n" % NAME, encoding="utf-8")
    # ESTATE:END
    (root / "config" / "agent.conf").write_text("AGENT_LANG=en\n", encoding="utf-8")
    (root / ".telegram_bot" / ".env").write_text("", encoding="utf-8")
    push = root / "routines" / "push.sh"
    push.write_text('#!/bin/bash\nwhile [[ $# -gt 0 ]]; do [[ "$1" == --text ]] && '
                    'printf "%s\\n" "$2" >> "$PUSHLOG"; shift; done\n', encoding="utf-8")
    push.chmod(0o755)
    stub = br / "venv" / "bin" / "python"
    stub.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return root


def _fakebin(tmp: Path, root: Path, old_pid, new_pid=None) -> Path:
    """launchctl stub: lists old_pid until a kickstart, new_pid after it."""
    fb = tmp / "fakebin"
    fb.mkdir(exist_ok=True)
    st = tmp / "lc"
    st.mkdir(exist_ok=True)
    label = _baked_label()
    rows = ("%s\t0\t%s" % (old_pid, label)) if old_pid else ""
    (fb / "launchctl").write_text(textwrap.dedent(f"""\
        #!/bin/bash
        case "$1" in
          list)
            if [[ -f "{st}/kicked" ]]; then echo "{new_pid}	0	{label}"
            elif [[ -n "{rows}" ]]; then echo "{rows}"; fi ;;
          kickstart)
            echo "$*" >> "{st}/kick.log"; touch "{st}/kicked"
            echo "Bot is running" >> "{root}/.telegram_bot/logs/bot.log" ;;
        esac
        """), encoding="utf-8")
    (fb / "launchctl").chmod(0o755)
    return fb


def _env(fb: Path, tmp: Path, **extra) -> dict:
    env = dict(os.environ, PATH="%s:%s" % (fb, os.environ["PATH"]),
               PUSHLOG=str(tmp / "pushes.log"), DGN1814_MODEL_WAIT="0",
               DGN1814_MODEL_FOLLOWUP_WAIT="0", **extra)
    env.pop("CLAUDECODE", None)  # a session-run test must hit the worker, not the F7 gate
    return env


def _target(root: Path, env: dict) -> str:
    out = subprocess.run(["bash", str(root / "bridge" / "self_restart.sh"), "--print-target"],
                         env=env, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _baked_label() -> str:
    """The label this tree's self_restart.sh carries once rendered."""
    return re.search(r'^LABEL="([^"]+)"', _mint_render(SRC_RESTART.read_text(encoding="utf-8")),
                     re.M).group(1)


# ESTATE:BEGIN DGN-2147 R1/R2: the minted service plist and the instance identity are estate-only
def _plist_label(name: str = NAME) -> str:
    m = re.search(r"<key>Label</key>\s*<string>([^<]+)</string>",
                  _mint_render(SRC_PLIST.read_text(encoding="utf-8"), name))
    return m.group(1)


@needs_posix
def test_r1_minted_target_is_the_service_label_and_its_pid(tmp_path):
    # promise: R1 /restart targets the label the minted plist registers
    root = _instance(tmp_path / "inst")
    env = _env(_fakebin(tmp_path, root, 4242), tmp_path)
    assert _target(root, env) == "label=%s pid=4242" % _plist_label()
    assert _plist_label() == "com.telegram-skill-bot.%s" % NAME


@needs_posix
def test_r2_placeholder_label_resolves_from_instance_identity(tmp_path):
    # promise: R2 an unrendered label still finds the agent's service
    root = _instance(tmp_path / "inst", minted=False)
    env = _env(_fakebin(tmp_path, root, 4243), tmp_path)
    assert _target(root, env) == "label=com.telegram-skill-bot.%s pid=4243" % NAME
# ESTATE:END


def _spawn(*argv):
    p = subprocess.Popen([sys.executable, "-c", "import signal,time\n"
                          "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(20)", *argv])
    time.sleep(0.05)
    return p


def _lock(root: Path, pid: int) -> None:
    (root / ".telegram_bot" / "state" / "bridge.lock").write_text(
        '{"pid": %d, "host": "manual", "started_at": 1}' % pid, encoding="utf-8")


@needs_posix
def test_r3_no_launchd_row_targets_the_live_bridge_in_the_lock(tmp_path):
    # promise: R3 the lock pid is used only while it is a live bridge
    root = _instance(tmp_path / "inst")
    env = _env(_fakebin(tmp_path, root, None), tmp_path)
    bridge, stranger = _spawn("-m", "bridge"), _spawn()
    try:
        _lock(root, bridge.pid)
        assert _target(root, env) == "label=%s pid=%d" % (_baked_label(), bridge.pid)
        _lock(root, stranger.pid)
        assert _target(root, env) == "label=%s pid=" % _baked_label()
    finally:
        bridge.kill()
        stranger.kill()


@needs_posix
def test_r4_bridge_outliving_sigterm_is_replaced(tmp_path):
    # promise: R4 an old bridge ignoring SIGTERM is replaced, owner notified
    root = _instance(tmp_path / "inst")
    old = _spawn("-m", "bridge")
    try:
        env = _env(_fakebin(tmp_path, root, old.pid, new_pid=old.pid + 100000), tmp_path,
                   SELF_RESTART_TERM_GRACE="0")
        out = subprocess.run(
            ["bash", str(root / "bridge" / "self_restart.sh"), "--_worker", "--reason", "dgn2147",
             "--label", _baked_label(), "--delay", "0", "--prefix", "",
             "--env", str(root / ".telegram_bot" / ".env")],
            env=env, capture_output=True, text=True, timeout=5)
    finally:
        old.kill()
    kicks = (tmp_path / "lc" / "kick.log").read_text(encoding="utf-8")
    assert "kickstart -k gui/%d/%s" % (os.getuid(), _baked_label()) in kicks, out.stdout
    pushes = (tmp_path / "pushes.log").read_text(encoding="utf-8")
    assert "Restart complete" in pushes, out.stdout + out.stderr
