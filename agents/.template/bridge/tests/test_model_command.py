"""Tests for DGN-099 /model command: model listing and switch logic.

Covers the pure-logic layer that the _cmd_model handler relies on:
  - _model_whitelist() reads BRIDGE_MODELS env (or defaults to ['sonnet'])
  - _known_models() composes from the whitelist + built-in labels, deduped
  - On switch: session["model"] is set, session_id cleared, model persisted
  - Full 'claude-*' ids bypass the whitelist check (same rule as _cmd_model)
  - Unknown short names are rejected

These tests do NOT import TelegramBot (which requires a live PTB application).
They test the helpers and the session-mutation logic in isolation, which is
the load-bearing seam for the /model command.

Import order note: bridge.config calls load_dotenv() at import time, which
injects BRIDGE_MODELS from the project .env into os.environ. Importing at the
top of this module (outside any patch.dict context) ensures load_dotenv runs
exactly once, so subsequent patch.dict calls to override BRIDGE_MODELS are
respected by _model_whitelist().
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Import once here so bridge.config.load_dotenv() fires before any patch.dict
# context. Tests then use patch.dict to override BRIDGE_MODELS for their scope.
import bridge.tests.conftest  # noqa: F401 -- ensures PROJECT_ROOT / TOKEN are set
from bridge import bot as _bot
from bridge import model_state


class TestModelWhitelist(unittest.TestCase):
    """_model_whitelist() is env-driven and evaluated on every call."""

    def test_empty_env_falls_back_to_sonnet(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": ""}):
            result = _bot._model_whitelist()
        self.assertEqual(result, ["sonnet"])

    def test_multi_model_env(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": "sonnet,opus,fable"}):
            result = _bot._model_whitelist()
        self.assertEqual(result, ["sonnet", "opus", "fable"])

    def test_whitespace_stripped(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": " sonnet , haiku "}):
            result = _bot._model_whitelist()
        self.assertEqual(result, ["sonnet", "haiku"])


class TestKnownModels(unittest.TestCase):
    """_known_models() = env whitelist + built-in labels, deduped."""

    def test_contains_all_builtin_labels(self):
        # Built-in labels are always available regardless of env.
        known = _bot._known_models()
        for name in ("sonnet", "opus", "haiku", "fable"):
            self.assertIn(name, known)

    def test_no_duplicates(self):
        known = _bot._known_models()
        self.assertEqual(len(known), len(set(known)))

    def test_whitelist_names_present(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": "sonnet,opus"}):
            known = _bot._known_models()
        self.assertIn("sonnet", known)
        self.assertIn("opus", known)

    def test_model_labels_map_all_builtin_names(self):
        for name in ("sonnet", "opus", "haiku", "fable"):
            self.assertIn(name, _bot._MODEL_LABELS)


class TestModelSwitch(unittest.TestCase):
    """Session mutation on model switch matches what _cmd_model does."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp(prefix="model-cmd-test-"))
        self._saved_path = model_state.LAST_MODEL_PATH
        model_state.LAST_MODEL_PATH = self._tmpdir / "last_model.json"

    def tearDown(self):
        model_state.LAST_MODEL_PATH = self._saved_path

    def _switch(self, name: str, whitelist_env: str = "sonnet,opus") -> dict:
        """Simulate the session-mutation block inside _cmd_model on a valid switch.

        Replicates the guard + session mutation + persist_model call verbatim
        from _cmd_model (bot.py lines 928-943).
        """
        with patch.dict(os.environ, {"BRIDGE_MODELS": whitelist_env}):
            allowed = _bot._model_whitelist()
            if name not in allowed and not name.startswith("claude-"):
                raise ValueError(f"Unknown model: {name}")
            session: dict = {}
            session["model"] = name
            session["session_id"] = None
            session["new_session"] = True
            model_state.persist_model(name, _bot._known_models())
        return session

    def test_switch_sets_model_in_session(self):
        session = self._switch("opus")
        self.assertEqual(session["model"], "opus")

    def test_switch_clears_session_id(self):
        session = self._switch("opus")
        self.assertIsNone(session["session_id"])

    def test_switch_sets_new_session_flag(self):
        session = self._switch("opus")
        self.assertTrue(session["new_session"])

    def test_switch_persists_model(self):
        self._switch("opus")
        persisted_path = self._tmpdir / "last_model.json"
        self.assertTrue(persisted_path.exists())
        data = json.loads(persisted_path.read_text())
        self.assertEqual(data["model"], "opus")

    def test_full_claude_id_accepted(self):
        # Full 'claude-*' ids bypass the whitelist check -- same rule as _cmd_model.
        session = self._switch("claude-opus-4-8", whitelist_env="sonnet")
        self.assertEqual(session["model"], "claude-opus-4-8")

    def test_unknown_short_name_rejected(self):
        with self.assertRaises(ValueError):
            self._switch("gpt-4", whitelist_env="sonnet")

    def test_switch_is_idempotent_same_model(self):
        session = self._switch("sonnet", whitelist_env="sonnet")
        self.assertEqual(session["model"], "sonnet")


class TestModelListOutput(unittest.TestCase):
    """The list shown by /model reflects the BRIDGE_MODELS env."""

    def test_fable_listed_when_in_env(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": "sonnet,fable"}):
            wl = _bot._model_whitelist()
        self.assertIn("fable", wl)

    def test_fable_absent_when_not_in_env(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": "sonnet"}):
            wl = _bot._model_whitelist()
        self.assertNotIn("fable", wl)

    def test_haiku_present_only_when_in_env(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": "sonnet,haiku"}):
            wl = _bot._model_whitelist()
        self.assertIn("haiku", wl)

    def test_list_reflects_all_env_models(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": "sonnet,opus,haiku,fable"}):
            wl = _bot._model_whitelist()
        self.assertEqual(wl, ["sonnet", "opus", "haiku", "fable"])


class TestModelPerfRank(unittest.TestCase):
    """_MODEL_PERF_RANK defines fable > opus > sonnet > haiku ordering."""

    def test_all_builtin_models_ranked(self):
        for name in ("fable", "opus", "sonnet", "haiku"):
            self.assertIn(name, _bot._MODEL_PERF_RANK)

    def test_fable_highest(self):
        self.assertLess(
            _bot._MODEL_PERF_RANK["fable"],
            _bot._MODEL_PERF_RANK["opus"],
        )

    def test_haiku_lowest(self):
        self.assertGreater(
            _bot._MODEL_PERF_RANK["haiku"],
            _bot._MODEL_PERF_RANK["sonnet"],
        )

    def test_sort_order_is_fable_opus_sonnet_haiku(self):
        models = [("sonnet", "S"), ("haiku", "H"), ("fable", "F"), ("opus", "O")]
        models.sort(key=lambda x: (_bot._MODEL_PERF_RANK.get(x[0], 99), x[0]))
        names = [m[0] for m in models]
        self.assertEqual(names, ["fable", "opus", "sonnet", "haiku"])


class TestModelAlreadyActive(unittest.TestCase):
    """Same-model guard: switching to the already-active model is a no-op switch."""

    def _get_current_model(self, model_name: str, whitelist_env: str = "sonnet,opus") -> str:
        with patch.dict(os.environ, {"BRIDGE_MODELS": whitelist_env}):
            from bridge import model_state
            return model_state.resolve_session_model(
                override=model_name,
                known=_bot._known_models(),
                fallback=_bot.DEFAULT_MODEL,
            )

    def test_same_model_resolves_equal(self):
        current = self._get_current_model("sonnet")
        self.assertEqual(current, "sonnet")

    def test_same_model_detection_logic(self):
        with patch.dict(os.environ, {"BRIDGE_MODELS": "sonnet,opus"}):
            from bridge import model_state
            session = {"model": "opus"}
            current = model_state.resolve_session_model(
                override=session.get("model"),
                known=_bot._known_models(),
                fallback=_bot.DEFAULT_MODEL,
            )
            self.assertEqual(current, "opus")
            self.assertTrue(current == session["model"])


if __name__ == "__main__":
    unittest.main()
