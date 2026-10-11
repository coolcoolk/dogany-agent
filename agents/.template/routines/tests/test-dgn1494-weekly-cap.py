#!/usr/bin/env python3
# test-lane: fast    # measured 3.1s (2026-09-16 carryback verify, 15 tests / 64 subtests)
"""Offline cap/usage-gate scope parity; no dispatch, API, or live writes."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


# DGN-1835 formula, opus, quota "90 15", full horizon, machine stub roomy
# enough never to bind: 5h floor((90-10-7.1*0.4)/(6.3*0.4)) = 30,
# 7d floor((15-10-1.2*0.4)/(1.1*0.4)) = 10.
WAIVED, APPLIED = 30, 10


class WeeklyCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dgn1494-")
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.routines = self.ws / "routines"
        (self.routines / "lib").mkdir(parents=True)
        (self.ws / "config").mkdir()
        for name in ("dispatch-detached.sh", "usage-gate.py"):
            shutil.copy(ROOT / "routines" / name, self.routines / name)
        shutil.copy(ROOT / "routines/lib/conf_reader.py", self.routines / "lib")
        self.env = {k: v for k, v in os.environ.items() if not (
            k.startswith(("DOGANY_", "USAGE_GATE", "STUB_")) or
            k in ("PROJECT_ROOT", "DISPATCH_TEST"))}
        self.env.update(DISPATCH_TEST="1", PYTHONDONTWRITEBYTECODE="1")
        self.conf("full")

    def conf(self, value):
        (self.ws / "config/agent.conf").write_text('USAGE_GATE_7D="' + value + '"\n')

    def cap(self, scope=None, session=None, quota="90 15", runtime="claude-code", **extra):
        env = dict(self.env, **extra)
        if scope is not None:
            env["USAGE_GATE_7D"] = scope
        if session is not None:
            env["DOGANY_SESSION"] = session
        # Positional data avoids interpolating shell code or owner settings.
        script = '''wrapper="$1"
set --
source "$wrapper"
claude_quota_remaining() { if [[ "$TEST_QUOTA" == fail ]]; then return 1; fi; echo "$TEST_QUOTA"; }
codex_quota_remaining() { echo 31; }
claude_hours_left() { return 1; }
machine_snapshot() { echo "0.5 64 64000"; }
MODEL=opus
compute_dynamic_cap "$TEST_RUNTIME"
printf '%s\\n' "$DYNAMIC_CAP_VALUE"
'''
        env.update(TEST_QUOTA=quota, TEST_RUNTIME=runtime)
        result = subprocess.run(["bash", "-c", script, "test", str(self.routines / "dispatch-detached.sh")],
                                cwd=self.ws, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout.strip())

    def test_live_auto_only_waives_weekly_cap(self):
        self.assertEqual(self.cap("auto-only", "live"), WAIVED)

    def test_quoted_conf_used_without_env(self):
        self.conf("auto-only")
        self.assertEqual(self.cap(session="owner"), WAIVED)

    def test_env_full_overrides_conf(self):
        self.conf("auto-only")
        self.assertEqual(self.cap("full", "live"), APPLIED)

    def test_env_auto_only_overrides_full_conf(self):
        self.assertEqual(self.cap("auto-only", "interactive"), WAIVED)

    def test_bridge_presence_matches_hook(self):
        self.assertEqual(self.cap("auto-only", PROJECT_ROOT=str(self.ws)), WAIVED)

    def test_cron_overrides_bridge_presence(self):
        self.assertEqual(self.cap("auto-only", "cron", PROJECT_ROOT=str(self.ws)), APPLIED)

    def test_unknown_session_is_conservative(self):
        self.assertEqual(self.cap("auto-only"), APPLIED)

    def test_full_keeps_weekly_cap(self):
        self.assertEqual(self.cap("full", "live"), APPLIED)

    def test_five_hour_cap_never_waived(self):
        self.assertEqual(self.cap("auto-only", "live", quota="2 90"), 0)

    def test_codex_weekly_cap_unchanged(self):
        # codex: floor((31-2)/(15*0.4)) = 4, untouched by the claude weekly policy
        self.assertEqual(self.cap("auto-only", "live", runtime="codex"), 4)

    def test_read_failure_keeps_fixed_cap(self):
        self.assertEqual(self.cap("auto-only", "live", quota="fail"), 3)

    def test_missing_policy_module_is_conservative(self):
        (self.routines / "usage-gate.py").unlink()
        self.assertEqual(self.cap("auto-only", "live"), APPLIED)

    def test_broken_policy_module_is_conservative(self):
        (self.routines / "usage-gate.py").write_text('raise RuntimeError("fixture")\n')
        self.assertEqual(self.cap("auto-only", "live"), APPLIED)

    def test_manual_cap_override_unchanged(self):
        self.assertEqual(self.cap("full", "auto", DOGANY_DISPATCH_MAX_ACTIVE="7"), 7)

    def test_scope_matrix_matches_existing_hook_policy(self):
        spec = importlib.util.spec_from_file_location("usage_gate_fixture", self.routines / "usage-gate.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for scope in ("auto-only", "auto_only", "auto", "owner-exempt", "interactive-exempt", "full", "off", "unknown"):
            for session in ("interactive", "owner", "live", "auto", "autonomous", "cron", "headless", ""):
                with self.subTest(scope=scope, session=session):
                    with patch.dict(os.environ, dict(self.env, USAGE_GATE_7D=scope, DOGANY_SESSION=session), clear=True):
                        waived = mod.gate_7d_auto_only(str(self.ws)) and mod.session_is_interactive()
                    self.assertEqual(self.cap(scope, session), WAIVED if waived else APPLIED)


if __name__ == "__main__":
    unittest.main()
