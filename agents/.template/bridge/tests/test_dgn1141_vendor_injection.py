"""DGN-1141 stage 4: vendor contract injection + machine-grammar fragment.

Two injection surfaces land here:

(a) i18n machine fragment -- the [[OPTIONS]] syntax block and the extended
    send_file delivery rules are CODE-owned channel grammar living in the
    i18n catalogs (same repo/commit as the parser: lockstep). Tests assert
    the fragment actually reaches the composed system prompt on BOTH
    locales, that en == ko (the model-facing block is locale-independent by
    contract), and that the numbers/tokens the fragment teaches are the ones
    the parser enforces (drift tripwire).

(b) vendor selector -- _compose_system_prompt() prepends vendors/telegram.md
    when the vendors/ layer exists. Failure directions (DGN-1141-M2 sec. 3):
    vendors/ absent = legitimate default, inject nothing (fail-open);
    vendors/ present but the declared vendor's file missing/empty =
    VendorContractMissing (fail-closed), promoted to a bridge BOOT die by the
    module-level validation call (subprocess-proven below, with a positive
    control so a nonzero exit is never mistaken for an interpreter/env
    failure).
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import messages, options, sdk_bridge
from bridge.formatting import MAX_FILE_BYTES, OPTIONS_MARKER
from bridge.i18n import en, ko

VENDOR_TEXT = "# telegram.md -- vendor contract\n\n## Sample fidelity\n\n- rule\n"


def _tmp_vendors(create_file=True, text=VENDOR_TEXT):
    """Fresh temp dir standing in for PROJECT_ROOT/vendors."""
    d = Path(tempfile.mkdtemp(prefix="dgn1141-vendors-"))
    if create_file:
        (d / "telegram.md").write_text(text, encoding="utf-8")
    return d


class MachineFragmentTest(unittest.TestCase):
    """(a) the grammar fragment reaches the composed prompt, both locales."""

    FRAGMENT_PROBES = (
        "## Choice Buttons ([[OPTIONS]] marker)",
        "[[OPTIONS: label A | label B]]",
        "the FIRST source that yields labels wins",
        "degrades to a bare number token",
        "builds ZERO buttons",
        "10MB or larger is silently skipped",
        "detected even inside code fences",
        "Allow/Deny confirmation",
    )

    def _compose(self, locale):
        with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", True):
            with patch.object(sdk_bridge.config, "locale", locale):
                return sdk_bridge._compose_system_prompt()

    def test_fragment_present_ko(self):
        prompt = self._compose("ko")
        for probe in self.FRAGMENT_PROBES:
            self.assertIn(probe, prompt)

    def test_fragment_present_en(self):
        prompt = self._compose("en")
        for probe in self.FRAGMENT_PROBES:
            self.assertIn(probe, prompt)

    def test_en_ko_system_prompt_identical(self):
        # The model-facing grammar block is locale-independent by contract
        # (DGN-1141-M7 measured this equality on the pre-change catalogs;
        # the stage-4 fragment must keep it).
        self.assertEqual(en.STRINGS["system_prompt"], ko.STRINGS["system_prompt"])

    def test_fragment_ordering_question_then_options_then_files(self):
        sp = en.STRINGS["system_prompt"]
        q = sp.index("## User Questions and Choices")
        o = sp.index("## Choice Buttons")
        f = sp.index("## Sending Images and Files")
        d = sp.index("## Subagent Task Delegation")
        self.assertTrue(q < o < f < d)

    def test_fragment_numbers_match_parser_constants(self):
        # Drift tripwire: the taught numbers ARE the enforced numbers.
        sp = en.STRINGS["system_prompt"]
        self.assertEqual(options._BUTTON_LABEL_MAX_WIDTH, 31.0)
        self.assertIn("~31 character widths", sp)
        # The taught budget applies to the WHOLE reconstructed button line
        # "N. label" -- the same string _overflows_to_handle width-checks.
        self.assertIn("'N. label'", sp)
        self.assertEqual(MAX_FILE_BYTES, 10 * 1024 * 1024)
        self.assertIn("10MB", sp)
        self.assertIn(OPTIONS_MARKER, sp)


class VendorSelectorTest(unittest.TestCase):
    """(b) fail-open without the layer, fail-closed for a declared vendor."""

    def test_no_vendors_dir_injects_nothing(self):
        missing = Path(tempfile.mkdtemp(prefix="dgn1141-root-")) / "vendors"
        with patch.object(sdk_bridge, "_VENDOR_DIR", missing):
            self.assertEqual("", sdk_bridge._load_vendor_contract())
            with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", False):
                self.assertEqual(
                    messages.SYSTEM_PROMPT, sdk_bridge._compose_system_prompt()
                )

    def test_vendor_file_present_prepends_contract(self):
        vendors = _tmp_vendors()
        with patch.object(sdk_bridge, "_VENDOR_DIR", vendors):
            with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", False):
                prompt = sdk_bridge._compose_system_prompt()
        # Fixed composition order: vendor doc, then the i18n machine fragment.
        self.assertTrue(prompt.startswith(VENDOR_TEXT.rstrip()))
        self.assertIn(messages.SYSTEM_PROMPT, prompt)
        self.assertLess(
            prompt.index("## Sample fidelity"), prompt.index("## Choice Buttons")
        )

    def test_vendor_file_missing_raises(self):
        vendors = _tmp_vendors(create_file=False)
        with patch.object(sdk_bridge, "_VENDOR_DIR", vendors):
            with self.assertRaises(sdk_bridge.VendorContractMissing):
                sdk_bridge._load_vendor_contract()
            with self.assertRaises(sdk_bridge.VendorContractMissing):
                sdk_bridge._compose_system_prompt()

    def test_vendor_file_empty_raises(self):
        vendors = _tmp_vendors(text="   \n\n")
        with patch.object(sdk_bridge, "_VENDOR_DIR", vendors):
            with self.assertRaises(sdk_bridge.VendorContractMissing):
                sdk_bridge._load_vendor_contract()

    def test_info_line_logged_on_load(self):
        vendors = _tmp_vendors()
        with patch.object(sdk_bridge, "_VENDOR_DIR", vendors):
            with self.assertLogs("bridge.sdk_bridge", level="INFO") as cm:
                sdk_bridge._load_vendor_contract()
        joined = "\n".join(cm.output)
        self.assertIn("vendor=telegram", joined)
        self.assertIn("bytes=", joined)
        self.assertIn("sha=", joined)

    def test_heading_collision_warns(self):
        # A vendor doc restating a machine-fragment section trips the
        # compose-time duplicate lint (log-warn, never a failure).
        colliding = "# v\n\n## Sending Images and Files\n\n- dup rule\n"
        vendors = _tmp_vendors(text=colliding)
        with patch.object(sdk_bridge, "_VENDOR_DIR", vendors):
            with self.assertLogs("bridge.sdk_bridge", level="WARNING") as cm:
                sdk_bridge._compose_system_prompt()
        # DGN-818 C2 widened the lint to N injectables, so the message names
        # the two colliding items instead of assuming "vendor vs machine".
        joined = "\n".join(cm.output)
        self.assertIn("injection overlap", joined)
        self.assertIn("Sending Images and Files", joined)

    def test_disjoint_headings_do_not_warn(self):
        vendors = _tmp_vendors()
        with patch.object(sdk_bridge, "_VENDOR_DIR", vendors):
            with self.assertNoLogs("bridge.sdk_bridge", level="WARNING"):
                sdk_bridge._compose_system_prompt()


class BootDieSubprocessTest(unittest.TestCase):
    """Import-time validation = the boot-die seam, proven end to end."""

    def _import_bridge(self, project_root):
        bridge_parent = Path(sdk_bridge.__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, "-c", "import bridge.sdk_bridge"],
            capture_output=True,
            text=True,
            timeout=60,
            env={
                "PATH": "/usr/bin:/bin",
                "PROJECT_ROOT": str(project_root),
                "TELEGRAM_BOT_TOKEN": "test:token",
                "PYTHONPATH": str(bridge_parent),
            },
        )

    def _fresh_root(self):
        root = Path(tempfile.mkdtemp(prefix="dgn1141-boot-"))
        (root / ".telegram_bot").mkdir()
        return root

    def test_declared_vendor_missing_dies_at_import(self):
        root = self._fresh_root()
        (root / "vendors").mkdir()  # layer shipped, contract absent
        proc = self._import_bridge(root)
        self.assertNotEqual(0, proc.returncode)
        # Not an interpreter/launcher failure: the die must be OUR exception.
        self.assertNotEqual(127, proc.returncode)
        self.assertIn("VendorContractMissing", proc.stderr)

    def test_no_vendors_layer_boots(self):
        # Positive control for the test above: same interpreter, same env
        # shape, only the vendors/ state differs -- so the nonzero exit in
        # the failing case is attributable to the vendor gate alone.
        root = self._fresh_root()
        proc = self._import_bridge(root)
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_declared_vendor_present_boots(self):
        root = self._fresh_root()
        (root / "vendors").mkdir()
        (root / "vendors" / "telegram.md").write_text(
            VENDOR_TEXT, encoding="utf-8"
        )
        proc = self._import_bridge(root)
        self.assertEqual(0, proc.returncode, proc.stderr)


class ShippedVendorContractTest(unittest.TestCase):
    """The shipped contract doc must warn that it is framework-owned.

    DGN-1141 stage 8 (M11 MAJOR-6). vendors/ deliberately carries NO ownership
    token in its filenames (section 3.5: everything under it is framework), and
    section 3.6 relies on the `framework` token itself reading as "do not
    hand-edit". A file with no token therefore carries no warning at all -- and
    this one invites the edit twice over: update.sh reverts hand edits (backing
    them up), while the loader reads it fresh on every compose. The warning has
    to be spelled out in the prose instead.
    """

    CONTRACT = (
        Path(__file__).resolve().parents[2] / "vendors" / "telegram.md"
    )

    def test_shipped_contract_exists(self):
        # Positive control: the assertion below is about CONTENT, so the file's
        # existence is asserted separately -- a missing file must not read as
        # a passing content check.
        self.assertTrue(self.CONTRACT.is_file(), self.CONTRACT)

    def test_shipped_contract_declares_framework_ownership(self):
        head = self.CONTRACT.read_text(encoding="utf-8")[:800].upper()
        self.assertIn("FRAMEWORK-OWNED", head)
        self.assertIn("REVERT", head)


if __name__ == "__main__":
    unittest.main()
