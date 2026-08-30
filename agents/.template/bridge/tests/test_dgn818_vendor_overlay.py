"""DGN-818 C2: per-instance vendor overlay wiring + shipped-doc truth.

`vendors/custom.telegram.md` was RESERVED as an instance-owned name by
update.sh long before anything read it, and the shipped contract doc said in
prose that no per-instance overlay existed. Name reserved + zero readers +
prose denying it is the "존재 != 배선" shape DGN-1141 named (class 8): the
reservation and the documentation were each half true and disagreed with each
other.

C2 judged: KEEP the reservation, ADD the reader. These tests pin all three
points of that decision -- the loader's failure directions, the composition
order (canonical first, overlay second), and the SHIPPED doc naming the exact
file the loader computes. The last one is the answer to DGN-818-DESIGN
section 4 correction 3: the stage-8 test asserts the doc SAYS something about
ownership; nothing asserted that what it says is TRUE.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import sdk_bridge

CONTRACT_TEXT = "# telegram.md -- vendor contract\n\n## Sample fidelity\n\n- rule\n"
OVERLAY_TEXT = "# custom overlay\n\n## Local judgment\n\n- instance rule\n"


def _vendors(contract=CONTRACT_TEXT, overlay=None):
    d = Path(tempfile.mkdtemp(prefix="dgn818-vendors-"))
    if contract is not None:
        (d / "telegram.md").write_text(contract, encoding="utf-8")
    if overlay is not None:
        (d / "custom.telegram.md").write_text(overlay, encoding="utf-8")
    return d


class OverlayLoaderTest(unittest.TestCase):
    def test_overlay_filename_is_the_reserved_name(self):
        # The name is a CONTRACT with update.sh, not a local convention.
        self.assertEqual(
            f"{sdk_bridge._VENDOR_OVERLAY_PREFIX}{sdk_bridge._VENDOR_NAME}.md",
            "custom.telegram.md",
        )

    def test_absent_overlay_injects_nothing(self):
        with patch.object(sdk_bridge, "_VENDOR_DIR", _vendors()):
            self.assertEqual(sdk_bridge._load_vendor_overlay(), "")

    def test_present_overlay_is_loaded(self):
        with patch.object(sdk_bridge, "_VENDOR_DIR", _vendors(overlay=OVERLAY_TEXT)):
            self.assertEqual(sdk_bridge._load_vendor_overlay(), OVERLAY_TEXT)

    def test_blank_overlay_warns_and_injects_nothing(self):
        # fail-OPEN: an instance-owned file must never be able to brick the bot.
        with patch.object(sdk_bridge, "_VENDOR_DIR", _vendors(overlay="   \n\n")):
            with self.assertLogs("bridge.sdk_bridge", level="WARNING") as cm:
                self.assertEqual(sdk_bridge._load_vendor_overlay(), "")
            self.assertIn("blank", "\n".join(cm.output))

    def test_unreadable_overlay_warns_and_injects_nothing(self):
        d = _vendors()
        (d / "custom.telegram.md").mkdir()  # present but not a readable file
        with patch.object(sdk_bridge, "_VENDOR_DIR", d):
            with self.assertLogs("bridge.sdk_bridge", level="WARNING") as cm:
                self.assertEqual(sdk_bridge._load_vendor_overlay(), "")
            self.assertIn("unreadable", "\n".join(cm.output))

    def test_no_vendors_dir_injects_nothing(self):
        d = Path(tempfile.mkdtemp(prefix="dgn818-none-")) / "absent"
        with patch.object(sdk_bridge, "_VENDOR_DIR", d):
            self.assertEqual(sdk_bridge._load_vendor_overlay(), "")


class CompositionOrderTest(unittest.TestCase):
    def _compose(self, **kw):
        with patch.object(sdk_bridge, "_VENDOR_DIR", _vendors(**kw)):
            with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", False):
                return sdk_bridge._compose_system_prompt()

    def test_overlay_text_reaches_the_prompt(self):
        out = self._compose(overlay=OVERLAY_TEXT)
        self.assertIn("- instance rule", out)

    def test_canonical_first_overlay_second(self):
        # DGN-1141 section 3.4: 정본 먼저, 덮개 나중. Where both speak to the
        # same point, the instance's sentence is the later one.
        out = self._compose(overlay=OVERLAY_TEXT)
        self.assertLess(out.index("## Sample fidelity"), out.index("## Local judgment"))

    def test_overlay_precedes_the_machine_fragment(self):
        out = self._compose(overlay=OVERLAY_TEXT)
        self.assertLess(
            out.index("## Local judgment"),
            out.index("## Choice Buttons ([[OPTIONS]] marker)"),
        )

    def test_absent_overlay_is_byte_identical_to_the_pre_c2_shape(self):
        # Zero behavior change for every instance that has no overlay.
        self.assertEqual(
            self._compose(),
            CONTRACT_TEXT.rstrip() + self._compose_bare(),
        )

    def _compose_bare(self):
        d = Path(tempfile.mkdtemp(prefix="dgn818-bare-")) / "absent"
        with patch.object(sdk_bridge, "_VENDOR_DIR", d):
            with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", False):
                return sdk_bridge._compose_system_prompt()

    def test_overlay_colliding_heading_warns(self):
        with patch.object(
            sdk_bridge, "_VENDOR_DIR",
            _vendors(overlay="# o\n\n## Sample fidelity\n\n- dup\n"),
        ):
            with self.assertLogs("bridge.sdk_bridge", level="WARNING") as cm:
                sdk_bridge._compose_system_prompt()
            joined = "\n".join(cm.output)
            self.assertIn("injection overlap", joined)
            self.assertIn("custom.telegram.md", joined)


class InjectedSurfaceHygieneTest(unittest.TestCase):
    """No estate ticket ID may ride the model-facing prompt (DGN-818 C2)."""

    def test_composed_prompt_carries_no_ticket_ids(self):
        import re

        d = Path(tempfile.mkdtemp(prefix="dgn818-clean-")) / "absent"
        with patch.object(sdk_bridge, "_VENDOR_DIR", d):
            out = sdk_bridge._compose_system_prompt()
        # Positive control first: the detector must be able to see one.
        self.assertTrue(
            re.search(r"DGN-\d+", out + "DGN-000"),
            "ticket-id regex is dead -- a zero below would be meaningless",
        )
        self.assertEqual(re.findall(r"DGN-\d+", out), [])


class ShippedContractOverlayProseTest(unittest.TestCase):
    """The doc must name the file the loader actually reads (3-point match)."""

    CONTRACT = Path(__file__).resolve().parents[2] / "vendors" / "telegram.md"

    def test_contract_file_exists(self):
        # Existence asserted separately: a missing file must never read as a
        # passing CONTENT check.
        self.assertTrue(self.CONTRACT.is_file(), self.CONTRACT)

    def test_prose_names_the_overlay_the_loader_computes(self):
        text = self.CONTRACT.read_text(encoding="utf-8")
        computed = (
            f"{sdk_bridge._VENDOR_OVERLAY_PREFIX}{sdk_bridge._VENDOR_NAME}.md"
        )
        self.assertIn(computed, text)

    def test_prose_no_longer_denies_the_overlay(self):
        text = self.CONTRACT.read_text(encoding="utf-8")
        # The retired sentence may only survive as a quoted history note.
        for line in text.splitlines():
            if "no per-instance overlay for this file today" in line:
                self.assertIn('"', line, f"undeclared denial still live: {line}")


if __name__ == "__main__":
    unittest.main()
