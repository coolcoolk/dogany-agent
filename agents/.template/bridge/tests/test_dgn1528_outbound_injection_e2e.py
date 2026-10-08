"""DGN-1528/DGN-1642 end-to-end proof: push.sh's own outbound-record leaves
ONLY a ledger row now -- no session-inbox drop, no turn -- while the
session-inbox injection lane itself is unaffected and still carries real
report-bearing turns to the model.

DGN-1642 deleted push.sh's leg 2: the session-inbox drop that used to hand
the SENDING session a free follow-up turn on its own outbound-record. This
file used to prove that drop round-tripped through the REAL bridge/bot.py
TelegramBot._session_inbox_loop; that drop no longer exists, so this file
now proves two things instead, both against the REAL loop (same harness
shape as test_session_inbox_utf8_poison_pill.py):
  1. push.sh's real send appends exactly one ledger row and drops ZERO
     session-inbox files -- proof the RECORD lane is gone.
  2. a hand-authored LOUD injection (representative of the many writers
     that still create real report-bearing turns -- dispatch-return,
     cron-inject, handoff, etc., table 1.2 of the DGN-1642 design) still
     round-trips through the same real loop, injected loud (quiet=False),
     content intact -- proof the lane that CARRIES WORK was not touched.

The ticket's own constraint is explicit: verify the injection lane before
building on it -- "the same class of assumption that produced this ticket"
(two agents assumed visibility into each other's outbound that never
existed). It would be exactly that same mistake to assert push.sh's new
behavior against an ASSUMED contract and unit-test only push.sh's side.

DGN-1566: the real push.sh runs against a SANDBOX INSTANCE, not the host tree.
This test used to drive push.sh in place and then snapshot/restore the host
tree's .telegram_bot around it. Snapshot/restore is the wrong shape twice
over: it still WRITES the host tree (a crash or a hard kill between setUp and
tearDown leaves the drop behind), and it never covered
push-guard-override.log at all, which push.sh appends on every guarded run --
that log grew to 481K inside agents/.template, where mint.sh copied it into
every newborn. push.sh resolves everything it writes from its own location
("$SCRIPT_DIR/../.telegram_bot"), so the fix is to run a push.sh whose own
location is inside a sandbox; nothing then needs restoring because nothing in
the host tree is touched.

The sandbox is built the same way, and for the same reason, as the one in
routines/tests/test-push-idrill-exitcode.sh: routines/ and bridge/ as REAL
dirs holding one symlink per entry. A plain `routines -> .../routines` symlink
does not work -- push.sh's "$SCRIPT_DIR/.." is resolved physically by the
kernel and lands back in the host tree (measured). tests/lib/mint_fixture.sh
is not used here either: this file ships to instances, which carry
bridge/tests/ but no scripts/mint.sh and no tests/lib/.
"""

import asyncio
import json
import os
import shutil
import subprocess
import types
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import AsyncMock, MagicMock, patch

from bridge.tests._hostroot import skip_without_host


class _StopLoop(Exception):
    """Sentinel to break the infinite poll loop after a bounded tick count."""


def _fake_self():
    ns = types.SimpleNamespace()
    ns._proactive_push = AsyncMock()
    ns._user_turn_active = lambda _uid: False
    return ns


@unittest.skipUnless(*skip_without_host("routines/push.sh"))
class TestOutboundInjectionEndToEnd(unittest.TestCase):
    OWNER_ID = 1

    def setUp(self):
        self.host_root = Path(__file__).resolve().parents[2]

        # DGN-1566 sandbox instance: routines/ and bridge/ as REAL dirs holding
        # one symlink per entry, so push.sh under test is the byte-identical
        # host file while "$SCRIPT_DIR/.." -- everything it writes -- resolves
        # inside the sandbox. __pycache__ is deliberately NOT linked: through a
        # link the hop's bytecode writes would land back in the host tree.
        self.instance_root = Path(mkdtemp(prefix="dgn1528-inst-"))
        self.addCleanup(shutil.rmtree, self.instance_root, ignore_errors=True)
        for name in ("routines", "bridge"):
            linked = self.instance_root / name
            linked.mkdir()
            for entry in (self.host_root / name).iterdir():
                if entry.name in ("__pycache__", ".pytest_cache"):
                    continue
                (linked / entry.name).symlink_to(entry)

        self.push_sh = self.instance_root / "routines" / "push.sh"
        self.instance_dir = self.instance_root / ".telegram_bot"
        self.ledger = self.instance_dir / "outbound-ledger.jsonl"
        self.inbox = self.instance_dir / "session-inbox"
        self.inbox.mkdir(parents=True)

    def _real_push(self, tmp, text):
        """Drive the REAL push.sh (stubbed curl only). DGN-1642: push.sh
        drops ZERO session-inbox files now -- asserted here -- and this
        returns None (there is no drop file to hand back anymore).
        """
        stub_bin = Path(tmp) / "bin"
        stub_bin.mkdir()
        curl_stub = stub_bin / "curl"
        curl_stub.write_text(
            "#!/bin/bash\n"
            "OUT=\"\"\n"
            "ARGS=(\"$@\")\n"
            # DGN-1591: log every telegram call so tests can count how many
            # messages actually left for the owner surface.
            "printf '%s\\n' \"$*\" >> \"$(dirname \"$0\")/../curl-calls.log\"\n"
            "for ((i=0;i<${#ARGS[@]};i++)); do\n"
            "  if [[ \"${ARGS[$i]}\" == \"-o\" ]]; then OUT=\"${ARGS[$((i+1))]}\"; fi\n"
            "done\n"
            "echo '{\"ok\":true,\"result\":{\"message_id\":1}}' > \"$OUT\"\n"
            "printf '200'\n"
        )
        curl_stub.chmod(0o755)

        fake_env = Path(tmp) / "fake.env"
        fake_env.write_text(
            "TELEGRAM_BOT_TOKEN=TEST-ONLY-token123\nALLOWED_USER_IDS=12345\n"
        )

        before = set(self.inbox.glob("outbound-*.md"))
        env = dict(os.environ)
        env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
        env["PUSH_GUARD_OVERRIDE"] = "send-anyway"
        result = subprocess.run(
            ["bash", str(self.push_sh), "--env", str(fake_env), "--text", text],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        after = set(self.inbox.glob("outbound-*.md"))
        new_files = after - before
        # DGN-1642: leg 2 is deleted -- push.sh must never drop a
        # session-inbox file, on any send path.
        self.assertEqual(len(new_files), 0, f"expected zero new drops, got {new_files}")

    def test_push_sh_appends_ledger_and_drops_nothing(self):
        """DGN-1642: a real push.sh send appends exactly one outbound-ledger
        row and drops ZERO session-inbox files -- proof the RECORD lane
        (the turn push.sh used to manufacture for its own outbound-record)
        is gone, exercised against the real subprocess, not a unit stub."""
        tmp = mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        self.assertFalse(self.ledger.exists())
        self._real_push(tmp, "이 문장이 자기 기록으로 남아야 한다")
        ledger_lines = self.ledger.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ledger_lines), 1, ledger_lines)
        row = json.loads(ledger_lines[0])
        self.assertEqual(row["body"], "이 문장이 자기 기록으로 남아야 한다")
        # DGN-1566: push.sh's guard-override receipt is the write the old
        # snapshot/restore tearDown never covered at all (it grew to 481K
        # inside agents/.template). It must land under the sandbox too.
        self.assertTrue(
            (self.instance_dir / "push-guard-override.log").exists(),
            "guard receipt did not land in the sandbox instance dir",
        )

    def test_real_loud_injection_still_reaches_the_model(self):
        """DGN-1642: deleting push.sh's RECORD lane must not touch the lane
        that CARRIES WORK. A hand-authored loud injection -- representative
        of the many still-living writers in table 1.2 of the design
        (dispatch-return, cron-inject, handoff, etc. -- none of which are
        [outbound-record]/[operator-alert]) -- must still round-trip through
        the REAL bridge/bot.py TelegramBot._session_inbox_loop, injected
        LOUD (quiet=False), content intact."""
        from bridge import bot as bot_mod

        iso_dir = Path(mkdtemp())
        self.addCleanup(shutil.rmtree, iso_dir, ignore_errors=True)
        iso_inbox = iso_dir / "session-inbox"
        iso_inbox.mkdir()
        report_text = (
            "[dispatch-return] job 9999 finished: 3 commits, 1 merge -- "
            "review and decide next step."
        )
        (iso_inbox / "dispatch-return-0001.md").write_text(report_text, encoding="utf-8")

        results = {"injected": []}

        async def ensure_owner_stream(uid, model, chat_id, push):
            return True

        async def inject_background_turn(uid, text, quiet=False):
            results["injected"].append((uid, text, quiet))
            return True

        fake_bridge = MagicMock()
        fake_bridge.ensure_owner_stream = AsyncMock(side_effect=ensure_owner_stream)
        fake_bridge.inject_background_turn = AsyncMock(side_effect=inject_background_turn)

        fake_config = MagicMock()
        fake_config.bot_data_dir = iso_dir
        fake_config.allowed_user_ids = [self.OWNER_ID]

        fake_sessmgr = MagicMock()
        fake_sessmgr.get_session = AsyncMock(return_value={"model": "sonnet"})

        ticks = {"n": 0}

        async def fake_sleep(_secs):
            ticks["n"] += 1
            if ticks["n"] > 2:
                raise _StopLoop

        fake_logger = MagicMock()
        fake = _fake_self()

        with patch.object(bot_mod, "config", fake_config), \
             patch.object(bot_mod, "sdk_bridge", fake_bridge), \
             patch.object(bot_mod, "session_manager", fake_sessmgr), \
             patch.object(bot_mod, "logger", fake_logger), \
             patch.object(bot_mod.asyncio, "sleep", fake_sleep):
            try:
                asyncio.run(bot_mod.TelegramBot._session_inbox_loop(fake))
            except _StopLoop:
                pass

        self.assertEqual(len(results["injected"]), 1, results)
        injected_uid, injected_text, injected_quiet = results["injected"][0]
        self.assertEqual(injected_uid, self.OWNER_ID)
        self.assertEqual(injected_text, report_text)
        # DGN-1642: this is the class of turn the fix must NOT touch -- no
        # [outbound-record]/[operator-alert] prefix, so bot.py's quiet
        # classifier leaves it loud (quiet=False): it reaches the model as
        # a normal turn, default-delivered.
        self.assertFalse(injected_quiet, "a report-bearing injection must stay loud")
        # Consumed: real bot.py unlinks an injected file (mirrors production).
        self.assertFalse((iso_inbox / "dispatch-return-0001.md").exists())
        fake_logger.error.assert_not_called()

    def test_dgn1591_restart_total_owner_surface_is_one_message(self):
        """DGN-1591 umbrella verification, unit-chained: one restart-completion
        push -> the owner surface receives EXACTLY that one message.

        Chain under test (each link the real component):
          1. push.sh sends the restart notice     -> 1 telegram sendMessage
          2. DGN-1642: push.sh drops NO session-inbox file at all anymore --
             there is no turn left for a quiet classifier to act on.
          3. belt check (R2 untouched): the quiet-suppression machinery
             itself, exercised directly, still suppresses a quiet turn's
             default output unless the model opts in with PUSH -- it stays
             live for the skew window (4.3 of the design) and until R2.
        """
        tmp = mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        self._real_push(tmp, "☠️ 재시작 완료")

        # 1. exactly one telegram send left the instance.
        calls = (Path(tmp) / "curl-calls.log").read_text(encoding="utf-8").splitlines()
        sends = [c for c in calls if "sendMessage" in c or "sendPhoto" in c]
        self.assertEqual(len(sends), 1, sends)

        # 2. _real_push already asserts zero new session-inbox files; the
        #    old wire-prefix check is gone because there is no drop to check.

        # 3. the injected turn's output is suppressed by the REAL flush
        #    unless the model opts in with the PUSH sentinel.
        from bridge.sdk_bridge import SdkBridge, _UserStreamState
        st = _UserStreamState(client=MagicMock(), model=None)
        st.last_chat_id = 111
        st.proactive_push = AsyncMock()
        st.injected_turn_mode = "quiet"
        st.proactive_texts = ["기록만 확인했습니다: 별도 조치 없습니다."]
        asyncio.run(SdkBridge()._flush_proactive(1, st))
        st.proactive_push.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
