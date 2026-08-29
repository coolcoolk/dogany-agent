"""DGN-818 C3: the shipped bridge knows which generation it is.

Before C3, `bridge.__version__` was "1.0.0" with ZERO consumers anywhere in the
framework repo (DGN-818-DESIGN section 5.1), while OSS had just minted "1.1.0".
Two lines, one namespace, no machine able to tell them apart, and a shipped
bridge that could not answer "which generation am I".

C3 splits the identity (`__oss_base__` / `__oss_pin__` / `__vendor_rev__` ->
`__version__`) and gives it two consumers: the boot snapshot and /health.
These tests pin the split, the UPSTREAM.md lockstep, and both consumers.
"""

import json
import re
import unittest
from pathlib import Path

import bridge
from bridge import boot_snapshot, healthcmd

UPSTREAM = Path(bridge.__file__).resolve().parent / "UPSTREAM.md"


class IdentityShapeTest(unittest.TestCase):
    def test_version_is_composed_from_its_two_halves(self):
        self.assertEqual(
            bridge.__version__,
            "%s+dogany.%s" % (bridge.__oss_base__, bridge.__vendor_rev__),
        )

    def test_version_carries_a_local_segment(self):
        # PEP 440 local version: an OSS release can never mint this string.
        self.assertIn("+dogany.", bridge.__version__)

    def test_pin_is_a_full_commit_id(self):
        self.assertRegex(bridge.__oss_pin__, r"^[0-9a-f]{40}$")


class UpstreamLockstepTest(unittest.TestCase):
    """The pin/Vendor-rev prose was PROVENANCE-only. Now a machine reads it."""

    def setUp(self):
        self.assertTrue(UPSTREAM.is_file(), UPSTREAM)  # positive control
        self.text = UPSTREAM.read_text(encoding="utf-8")

    def test_pin_matches_upstream_md(self):
        m = re.search(r"^- Pinned commit: ([0-9a-f]{7,40})\s*$", self.text, re.M)
        self.assertIsNotNone(m, "no anchored 'Pinned commit:' line in UPSTREAM.md")
        self.assertEqual(m.group(1), bridge.__oss_pin__)

    def test_vendor_rev_is_declared_upstream(self):
        # Positive control: the Vendor-rev grep must find SOMETHING first, or a
        # missing marker and a missing file would read the same.
        self.assertTrue(re.search(r"^- Vendor-rev: ", self.text, re.M))
        self.assertIn(bridge.__vendor_rev__, self.text)


class BootSnapshotConsumerTest(unittest.TestCase):
    def test_helper_reports_the_running_package(self):
        self.assertEqual(boot_snapshot._bridge_version(), bridge.__version__)

    def test_snapshot_payload_carries_the_generation(self):
        snap = boot_snapshot._build_snapshot()
        self.assertEqual(snap["bridge_version"], bridge.__version__)
        # Schema number stays 1: the key is additive (see boot_snapshot's
        # module docstring for why a bump would have been the harmful choice).
        self.assertEqual(snap["schema"], 1)
        json.dumps(snap)  # must stay serialisable


class HealthConsumerTest(unittest.TestCase):
    """/health must SAY the generation, and must not fake agreement."""

    def _report(self, self_gen, estate_gens):
        from datetime import datetime, timezone

        def inst(display, gen):
            return {
                "display": display, "root": "/x/" + display, "label": display,
                "pid": 1, "lstart": None, "up": True, "fw_version": "1.43.0",
                "bridge_version": gen, "engine_versions": {},
                "integrity": "snapshot", "issues": [], "tech": [],
            }

        return {
            "now": datetime(2026, 8, 28, tzinfo=timezone.utc),
            "self": inst("host-a", self_gen),
            "estate": [inst(n, g) for n, g in estate_gens],
            "cli": {"segment": None, "issues": [], "tech": []},
            "jobs": {"issues": [], "tech": [], "heartbeat": None, "counts": {}},
        }

    def test_generation_is_reported(self):
        text, _ = healthcmd.compose_health_report(
            self._report(bridge.__version__, [("peer-b", bridge.__version__)]))
        self.assertIn("브릿지 세대", text)
        self.assertIn(bridge.__version__, text)

    def test_unreported_instance_is_named_not_omitted(self):
        # A silent absence would read as "everyone agrees" -- the exact shape
        # DGN-818's discipline forbids.
        text, _ = healthcmd.compose_health_report(
            self._report(bridge.__version__, [("peer-b", None)]))
        self.assertIn("미보고", text)
        self.assertIn("peer-b", text)


if __name__ == "__main__":
    unittest.main()
