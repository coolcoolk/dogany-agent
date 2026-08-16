"""Unit tests for DGN-162 model persistence + the resolution chain.

Covers: fresh install (no state file) -> settings chain; a valid persisted
model beating settings; corrupt JSON -> chain + one warning; unknown model id
-> chain + one warning; and that the atomic write leaves no partial file. The
module computes its paths at import time from config, so each test redirects
model_state's path globals to a private temp dir and resets the one-per-start
notice globals for isolation.
"""

import json
import tempfile
import unittest
from pathlib import Path

from bridge import model_state

KNOWN = ["sonnet", "opus", "haiku", "fable"]
FALLBACK = "sonnet"


class _Base(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp(prefix="model-state-test-"))
        self._last = self._dir / "last_model.json"
        self._ws = self._dir / "workspace_settings.json"
        self._global = self._dir / "global_settings.json"
        # Redirect module path globals + reset notice state for isolation.
        self._saved = (
            model_state.LAST_MODEL_PATH,
            model_state.WORKSPACE_SETTINGS_PATH,
            model_state.GLOBAL_SETTINGS_PATH,
            model_state._start_notice_emitted,
            model_state._pending_start_notice,
        )
        model_state.LAST_MODEL_PATH = self._last
        model_state.WORKSPACE_SETTINGS_PATH = self._ws
        model_state.GLOBAL_SETTINGS_PATH = self._global
        model_state._start_notice_emitted = False
        model_state._pending_start_notice = None

    def tearDown(self):
        (
            model_state.LAST_MODEL_PATH,
            model_state.WORKSPACE_SETTINGS_PATH,
            model_state.GLOBAL_SETTINGS_PATH,
            model_state._start_notice_emitted,
            model_state._pending_start_notice,
        ) = self._saved

    def _write_settings(self, path: Path, model):
        path.write_text(json.dumps({"model": model}), encoding="utf-8")

    def _resolve(self, override=None):
        return model_state.resolve_session_model(override, KNOWN, FALLBACK)


class TestFreshInstall(_Base):
    """No state file: resolution walks the settings chain, then fallback."""

    def test_no_state_no_settings_uses_fallback(self):
        self.assertFalse(self._last.exists())
        self.assertEqual(self._resolve(), FALLBACK)
        # The winning fallback is seeded into the state file for next time.
        self.assertEqual(json.loads(self._last.read_text())["model"], FALLBACK)

    def test_no_state_workspace_settings_wins(self):
        self._write_settings(self._ws, "opus")
        self._write_settings(self._global, "haiku")
        self.assertEqual(self._resolve(), "opus")

    def test_no_state_global_settings_when_no_workspace(self):
        self._write_settings(self._global, "haiku")
        self.assertEqual(self._resolve(), "haiku")

    def test_no_warning_on_fresh_install(self):
        self._resolve()
        self.assertIsNone(model_state.take_start_notice())


class TestPersistedWins(_Base):
    """A valid persisted model beats settings.json defaults."""

    def test_persisted_beats_workspace_settings(self):
        model_state.persist_model("opus", KNOWN)
        self._write_settings(self._ws, "haiku")
        self.assertEqual(self._resolve(), "opus")
        self.assertIsNone(model_state.take_start_notice())

    def test_full_claude_id_persists_and_wins(self):
        model_state.persist_model("claude-opus-4-8", KNOWN)
        self.assertEqual(self._resolve(), "claude-opus-4-8")


class TestOverride(_Base):
    """An explicit per-session override beats everything and is persisted."""

    def test_override_wins_and_persists(self):
        model_state.persist_model("opus", KNOWN)
        self.assertEqual(self._resolve(override="haiku"), "haiku")
        self.assertEqual(json.loads(self._last.read_text())["model"], "haiku")


class TestCorruptJson(_Base):
    """Corrupt state file: ignore + chain + exactly one warning."""

    def test_corrupt_json_falls_through_and_warns(self):
        self._last.write_text("{not valid json", encoding="utf-8")
        self._write_settings(self._ws, "opus")
        self.assertEqual(self._resolve(), "opus")
        notice = model_state.take_start_notice()
        self.assertIsNotNone(notice)
        # One-per-start: the second take returns nothing.
        self.assertIsNone(model_state.take_start_notice())

    def test_corrupt_json_no_settings_uses_fallback(self):
        self._last.write_text("\x00\x01broken", encoding="utf-8")
        self.assertEqual(self._resolve(), FALLBACK)
        self.assertIsNotNone(model_state.take_start_notice())


class TestUnknownModel(_Base):
    """Unknown / empty persisted model id: ignore + chain + warning."""

    def test_unknown_model_falls_through_and_warns(self):
        self._last.write_text(json.dumps({"model": "gpt-4"}), encoding="utf-8")
        self._write_settings(self._global, "haiku")
        self.assertEqual(self._resolve(), "haiku")
        self.assertIsNotNone(model_state.take_start_notice())

    def test_empty_model_falls_through(self):
        self._last.write_text(json.dumps({"model": ""}), encoding="utf-8")
        self.assertEqual(self._resolve(), FALLBACK)
        self.assertIsNotNone(model_state.take_start_notice())

    def test_persist_refuses_unknown_model(self):
        model_state.persist_model("gpt-4", KNOWN)
        self.assertFalse(self._last.exists())


class TestAtomicWrite(_Base):
    """Atomic write never leaves a partial/torn file, and no stray .tmp."""

    def test_no_partial_file_left(self):
        model_state.persist_model("opus", KNOWN)
        self.assertTrue(self._last.exists())
        # State file is a complete, parseable JSON object.
        self.assertEqual(json.loads(self._last.read_text())["model"], "opus")
        # No leftover temp sibling.
        tmp = self._last.with_name(self._last.name + ".tmp")
        self.assertFalse(tmp.exists())

    def test_read_never_sees_torn_file(self):
        # A reader either sees the full previous value or the full new value,
        # never a half-written one -- os.replace is atomic within a dir.
        model_state.persist_model("opus", KNOWN)
        model_state.persist_model("haiku", KNOWN)
        loaded, warn = model_state.load_persisted_model(KNOWN)
        self.assertEqual(loaded, "haiku")
        self.assertIsNone(warn)


if __name__ == "__main__":
    unittest.main()
