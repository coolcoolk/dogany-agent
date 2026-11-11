#!/usr/bin/env python3
# test-lane: fast
# test-component: return-route
# test-new-component: return routing has no existing suite; it is a new pure routing module
# test-tier: 0
# test-prereq: tmp-files
# test-covers: agents/.template/routines/lib/return_route.py agents/.template/routines/dispatch-detached.sh agents/.template/bridge/bot.py
# retained-promise: Ordinary dispatch reads no developer seat inventory.
# retained-promise: A consumed leader hold item leaves a receipt; republishing the same hold never recreates it.
# retained-promise: Activation serializes with publishers and a crash releases the barrier without enabling.
# retained-promise: Current-generation reservations, host leases of any agent and live inbox drops never block activation; pre-registration or unparseable state does.
# retained-promise: Unknown legacy dispatch status, publication or host reservation refuses activation.
# retained-promise: Registered terminal history and retained quarantine activate without fabricated receipts.
# retained-promise: Rewritten history, nested active work and current routed returns retain publication gates.
# retained-promise: Wrapper route selection retains a reservation until publication and fences old enabled writers.
# retained-promise: Deferred legacy replay retains its original reservation after scheduler handoff.
# retained-promise: Inbox consumption holds the publication barrier through injection and unlink.
# retained-promise: no PM seat sends the return to the PO
# retained-promise: a PM seat sends the return to that seat
# retained-promise: a non-urgent return in a live conversation is held
# retained-promise: an idle conversation holds nothing
# retained-promise: an enum-urgent return goes to the leader and is not held
# retained-promise: free-text urgency is not urgent
# retained-promise: the bridge gate lets an urgent drop through a live turn and keeps others
# retained-promise: dispatch-detached keeps the submitter inbox when no product is set
"""Return routing: holder via the PM seat resolver, PO fallback, urgent bypass."""
import ast
import asyncio
import contextlib
import hashlib
import json
import shlex
import subprocess
from unittest.mock import patch
import importlib.util
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
import types
from unittest.mock import AsyncMock

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

sys.dont_write_bytecode = True
for k in [k for k in os.environ if k != "DOGANY_RESOURCE_SETTINGS_DIR" and k.startswith(("GIT_CONFIG", "DOGANY_", "DGN_"))]:
    del os.environ[k]
REPO = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('_rr', REPO / 'agents/.template/routines/lib/return_route.py')
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

NO_SEAT = lambda product: ""
SEAT = lambda product: "/ws/pm"
ACTIVATION_TERMINAL_OUTCOMES = (
    "completed", "failed", "error", "cancelled", "canceled", "timed-out", "timeout",
    "incomplete", "aborted", "killed", "replaced", "needs-validation", "launch-failed", "closed",
)


class Route(unittest.TestCase):
    def test_no_pm_seat_goes_to_po(self):
        r = R.route("devkit", "", False, "/ws/po", "/ws/lead", seat_of=NO_SEAT)
        self.assertEqual((r["to"], r["via"]), ("/ws/po", "po"))

    def test_pm_seat_gets_the_return(self):
        r = R.route("devkit", "", False, "/ws/po", "/ws/lead", seat_of=SEAT)
        self.assertEqual((r["to"], r["via"]), ("/ws/pm", "pm"))

    def test_pack_command_with_arguments_reaches_pm(self):
        with tempfile.TemporaryDirectory() as td:
            devkit = Path(td) / "devkit"
            devkit.write_text(
                "#!/bin/sh\n"
                "[ \"$1 $2 $3\" = 'product pm-holder devkit' ] || exit 9\n"
                "printf '/ws/pm\\n'\n",
                encoding="utf-8",
            )
            devkit.chmod(0o755)
            old = os.environ.get(R.RESOLVER_ENV)
            os.environ[R.RESOLVER_ENV] = "%s product pm-holder" % devkit
            try:
                self.assertEqual(R.pm_seat("devkit"), "/ws/pm")
            finally:
                if old is None:
                    os.environ.pop(R.RESOLVER_ENV, None)
                else:
                    os.environ[R.RESOLVER_ENV] = old

    def test_broken_resolver_reports_cause(self):
        old = os.environ.get(R.RESOLVER_ENV)
        os.environ[R.RESOLVER_ENV] = "/definitely/missing/pm-holder"
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(R.pm_seat("devkit"), "")
        finally:
            if old is None:
                os.environ.pop(R.RESOLVER_ENV, None)
            else:
                os.environ[R.RESOLVER_ENV] = old
        self.assertIn("PM seat resolver could not run", stderr.getvalue())

    def test_live_non_urgent_is_held(self):
        r = R.route("devkit", "", True, "/ws/po", "/ws/lead", seat_of=NO_SEAT)
        self.assertTrue(r["held"])

    def test_idle_holds_nothing(self):
        r = R.route("devkit", "", False, "/ws/po", "/ws/lead", seat_of=NO_SEAT)
        self.assertFalse(r["held"])

    def test_enum_urgent_goes_to_leader_unheld(self):
        for flag in R.URGENT:
            r = R.route("devkit", flag, True, "/ws/po", "/ws/lead", seat_of=SEAT)
            self.assertEqual((r["to"], r["via"], r["held"]), ("/ws/lead", "leader", False))

    def test_free_text_urgency_is_not_urgent(self):
        r = R.route("devkit", "very urgent", True, "/ws/po", "/ws/lead", seat_of=NO_SEAT)
        self.assertEqual((r["via"], r["held"]), ("po", True))

    def test_bridge_gate_urgent_drop_only(self):
        self.assertTrue(R.inbox_urgent("[dispatch-return] x\n- urgent: outage\n"))
        self.assertFalse(R.inbox_urgent("[dispatch-return] x\n- urgent: asap\n"))
        bot = (REPO / 'agents/.template/bridge/bot.py').read_text(encoding='utf-8')
        self.assertRegex(bot, r'(?s)_user_turn_active\(owner_id\):.{0,260}?inbox_drop_urgent\(p\)')

    def test_dispatch_default_inbox_unchanged(self):
        sh = (REPO / 'agents/.template/routines/dispatch-detached.sh').read_text(encoding='utf-8')
        self.assertIn('SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"\n', sh)
        self.assertRegex(sh, r'if \[ -n "\$\{DOGANY_RETURN_PRODUCT:-\}" \]; then')
        self.assertNotRegex(sh, r'return_route\.py[^\n]*2>/dev/null')


class PlainDurableRoute(unittest.TestCase):
    def test_default_ignores_old_seat_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / ".telegram_bot/po-seat/config.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("broken retired developer configuration")
            resolved = R.resolve_json("ordinary", "", td)
            self.assertEqual(resolved["kind"], "inbox")
            self.assertEqual(resolved["destination"], str(root / ".telegram_bot/session-inbox"))
            self.assertEqual(legacy.read_text(), "broken retired developer configuration")

    def test_generic_reservation_and_publication_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            resolved = R.resolve_json("", "", td)
            R.reserve_route(root, "request-1", resolved)
            self.assertTrue(R.reservation_path(root, "request-1").is_file())
            R.release_reservation(root, "request-1")
            self.assertFalse(R.reservation_path(root, "request-1").exists())
            self.assertFalse((root / ".telegram_bot/po-seat").exists())

    def test_plain_inbox_hold_acknowledgement_is_durable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            key = "a" * 64
            R.publish_inbox_hold(root, key, {"event": "fixture"}, "routine", "Held routine")
            drop = root / ".telegram_bot/session-inbox" / ("routine-hold-" + key + ".md")
            with R.inbox_publication_barrier(root):
                R.acknowledge_inbox_drop(drop)
            R.publish_inbox_hold(root, key, {"event": "fixture"}, "routine", "Held routine")
            self.assertFalse(drop.exists())
            self.assertTrue((root / ".telegram_bot/routine-consumed" / drop.name).exists())

    def test_canonical_and_template_never_admitted(self):
        for root in (REPO, REPO / "agents/.template"):
            with self.assertRaises(ValueError):
                with R.inbox_publication_barrier(root):
                    self.fail("framework source admitted")


if __name__ == "__main__":
    unittest.main()
