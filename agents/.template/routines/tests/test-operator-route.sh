#!/bin/bash
# test-lane: fast
# test-tier: 1
# test-component: operator-route
# test-prereq: tmp-files, subprocess
# test-new-component: shared operator destination resolver
# promise: claim markers and unresolved steward directories never authorize owner fallback
# promise: configured, crew and maintainer operators resolve to usable destinations
set -euo pipefail
LIB="$(cd "$(dirname "$0")/../lib" && pwd)"
env -i PATH="$PATH" python3 -B - "$LIB" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

LIB = sys.argv.pop()


class OperatorRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="operator-route-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "instance"
        self.bot = self.root / ".telegram_bot"
        self.bot.mkdir(parents=True)
        self.conf = self.root / "agent.conf"
        self.env = self.bot / ".env"
        self.env.write_text("TELEGRAM_BOT_TOKEN=TEST-ONLY-owner\nALLOWED_USER_IDS=111\n")
        self.marker = Path(self.tmp.name) / "marker"

    def route(self):
        env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name,
               "DOGANY_MACHINE_STEWARD_FILE": str(self.marker)}
        return subprocess.check_output(
            ["bash", "-c", '. "$1/steward_env.sh"; . "$1/operator_route.sh"; '
             'operator_route "$2" "$3" "$4"', "route", LIB,
             str(self.root), str(self.conf), str(self.env)],
            env=env, text=True).strip().split("\t")

    def test_owner_lock_is_not_an_operator(self):
        (self.bot / "owner.lock").write_text("111\n")
        self.assertEqual(self.route()[0], "session")

    def test_claim_is_not_an_operator(self):
        (self.bot / ".claimed").touch()
        self.assertEqual(self.route()[0], "session")

    def test_bare_steward_directory_is_not_a_destination(self):
        steward = Path(self.tmp.name) / "steward"
        steward.mkdir()
        self.marker.write_text(str(steward))
        self.assertEqual(self.route()[0], "session")

    def test_missing_steward_is_standalone(self):
        self.conf.write_text("STEWARD_ENV_FILE=/missing/operator.env\n")
        self.assertEqual(self.route()[0], "session")

    def test_empty_configured_destination_fails_closed(self):
        self.env.write_text("")
        self.conf.write_text("STEWARD_ENV_FILE=" + str(self.env))
        self.assertEqual(self.route()[0], "invalid")

    def test_configured_destination(self):
        self.conf.write_text("STEWARD_ENV_FILE=" + str(self.env))
        self.assertEqual(self.route(), ["steward", str(self.env)])

    def test_crew_destination(self):
        steward = Path(self.tmp.name) / "steward"
        (steward / ".telegram_bot").mkdir(parents=True)
        env = steward / ".telegram_bot/.env"
        env.write_text(self.env.read_text())
        self.marker.write_text(str(steward))
        self.assertEqual(self.route(), ["steward", str(env.resolve())])

    def test_maintainer_destination(self):
        self.marker.write_text(str(self.root))
        self.assertEqual(self.route(), ["steward", str(self.env)])

    def test_declared_operator(self):
        self.conf.write_text("OWNER_KIND=operator\n")
        self.assertEqual(self.route()[0], "owner")


unittest.main()
PY
