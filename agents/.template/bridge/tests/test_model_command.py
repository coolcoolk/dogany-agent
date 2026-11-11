"""Tests for DGN-099 /model command: model listing and switch logic.

Covers the pure-logic layer that the _cmd_model handler relies on:
  - _model_whitelist() reads BRIDGE_MODELS env (or defaults to ['sonnet'])
  - _known_models() composes from the whitelist + built-in labels, deduped
  - On switch: session["model"] is set, session_id cleared, model persisted
  - Full 'claude-*' ids bypass the whitelist check (same rule as _cmd_model)
  - Unknown short names are rejected

The helper tests cover session mutation in isolation. DGN-2152 also drives
the real command, picker callback, session reload and injected bootstrap
with local session stores and a fake SDK client.

Import order note: bridge.config calls load_dotenv() at import time, which
injects BRIDGE_MODELS from the project .env into os.environ. Importing at the
top of this module (outside any patch.dict context) ensures load_dotenv runs
exactly once, so subsequent patch.dict calls to override BRIDGE_MODELS are
respected by _model_whitelist().
"""

import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Import once here so bridge.config.load_dotenv() fires before any patch.dict
# context. Tests then use patch.dict to override BRIDGE_MODELS for their scope.
import bridge.tests.conftest  # noqa: F401 -- ensures PROJECT_ROOT / TOKEN are set
from bridge import bot as _bot
from bridge import model_state, sdk_bridge
from bridge.session import SessionManager


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
        # DGN-1814 r3: labels are derived from the model table (no bridge map).
        from bridge import model_picker
        table = model_picker.chat_table()
        for name in ("sonnet", "opus", "haiku", "fable"):
            self.assertIn(name.capitalize(), model_picker.full_name(name, table))
        self.assertFalse(hasattr(_bot, "_MODEL_LABELS"))


class TestModelSwitch(unittest.TestCase):
    """Session mutation on model switch matches what _cmd_model does."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="model-cmd-test-")
        self.addCleanup(tmp.cleanup)
        self._tmpdir = Path(tmp.name)
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
            model_state.persist_model(name, _bot._known_models(), source="cmd_model")
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


# DGN-2152: exercise real owner selections and restart/injection boundaries.
@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(model_state, "_start_notice_emitted", False)
    monkeypatch.setattr(model_state, "_pending_start_notice", None)
    monkeypatch.setattr(model_state, "LAST_MODEL_PATH", tmp_path / "last_model.json")
    monkeypatch.setattr(model_state, "WORKSPACE_SETTINGS_PATH", tmp_path / "workspace.json")
    monkeypatch.setattr(model_state, "GLOBAL_SETTINGS_PATH", tmp_path / "global.json")
    monkeypatch.setattr(_bot.config, "session_store_path", tmp_path / "sessions.json")
    monkeypatch.setattr(_bot, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("BRIDGE_MODELS", "sonnet,haiku,opus")
    model_state.WORKSPACE_SETTINGS_PATH.write_text('{"model": "sonnet"}')
    os.utime(model_state.WORKSPACE_SETTINGS_PATH, ns=(10, 10))
    return tmp_path


def make_bot():
    instance = _bot.TelegramBot.__new__(_bot.TelegramBot)
    instance._runtime_active_sessions = set()
    instance._check_access = AsyncMock(return_value=True)
    instance._reply_guaranteed = AsyncMock()
    instance.application = SimpleNamespace(bot=None)
    return instance


@pytest.mark.parametrize("cached", [False, True])
def test_newer_settings_beat_stale_legacy_state_after_restart(state, monkeypatch, cached):
    model_state.LAST_MODEL_PATH.write_text('{"model": "haiku"}')
    os.utime(model_state.LAST_MODEL_PATH, ns=(1, 1))
    manager = SessionManager()
    asyncio.run(manager.update_session(1, {"model": "haiku"} if cached else {}))
    restarted = SessionManager()
    session = asyncio.run(restarted.get_session(1))
    assert make_bot()._get_real_model(session) == "sonnet"
    assert json.loads(model_state.LAST_MODEL_PATH.read_text()) == {"model": "haiku"}


@pytest.mark.parametrize("selection", ["command", "callback"])
@pytest.mark.parametrize("already_active", [False, True])
def test_owner_selection_survives_restart_and_logs_source(
    state, monkeypatch, caplog, selection, already_active
):
    if already_active:
        model_state.WORKSPACE_SETTINGS_PATH.write_text('{"model": "haiku"}')
        os.utime(model_state.WORKSPACE_SETTINGS_PATH, ns=(10, 10))
    manager = SessionManager()
    asyncio.run(manager.update_session(1, {"session_id": "keep-if-active"}))
    monkeypatch.setattr(_bot, "session_manager", manager)
    instance = make_bot()
    query = SimpleNamespace(data="model:haiku", answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=1),
        message=SimpleNamespace(reply_text=AsyncMock()), callback_query=query,
    )
    caplog.set_level(logging.INFO, logger="bridge.model_state")
    if selection == "command":
        asyncio.run(instance._cmd_model(update, SimpleNamespace(args=["haiku"])))
        source = "cmd_model"
    else:
        asyncio.run(instance._handle_callback(update, SimpleNamespace()))
        source = "model_callback"
    assert json.loads(model_state.LAST_MODEL_PATH.read_text()) == {
        "model": "haiku",
        "source": source,
        "settings_seen": {
            "workspace": "haiku" if already_active else "sonnet",
            "global": None,
        },
    }
    assert f"last_model persisted: source={source} model=haiku" in caplog.text
    monkeypatch.setattr(_bot, "_MODEL_OVERRIDE_BOOT", "restarted-process")
    restarted = SessionManager()
    session = asyncio.run(restarted.get_session(1))
    assert make_bot()._get_real_model(session) == "haiku"
    assert make_bot()._get_real_model({}) == "haiku"
    assert session["session_id"] == ("keep-if-active" if already_active else None)
    assert bool(session.get("new_session")) == (not already_active)


@pytest.mark.parametrize("seed", [False, True])
def test_background_resolution_and_real_injection_never_persist(state, monkeypatch, seed):
    if seed:
        model_state.persist_model("sonnet", _bot._known_models(), source="cmd_model")
    before = model_state.LAST_MODEL_PATH.read_bytes() if seed else None
    before_mtime = model_state.LAST_MODEL_PATH.stat().st_mtime_ns if seed else None
    client = SimpleNamespace(query=AsyncMock())
    bridge = sdk_bridge.SdkBridge()
    # A background-only override must never become the restart default.
    resolved = model_state.resolve_session_model("haiku", _bot._known_models(), "sonnet")
    bridge._streams[1] = sdk_bridge._UserStreamState(client=client, model=resolved)
    assert asyncio.run(bridge.inject_background_turn(1, "background note", quiet=True))
    client.query.assert_awaited_once()
    assert make_bot()._get_real_model({}) == "sonnet"
    if seed:
        assert model_state.LAST_MODEL_PATH.read_bytes() == before
        assert model_state.LAST_MODEL_PATH.stat().st_mtime_ns == before_mtime
    else:
        assert not model_state.LAST_MODEL_PATH.exists()


def test_injected_bootstrap_uses_settings_instead_of_cached_model(state, monkeypatch):
    model_state.LAST_MODEL_PATH.write_text('{"model": "haiku"}')
    os.utime(model_state.LAST_MODEL_PATH, ns=(1, 1))
    before = model_state.LAST_MODEL_PATH.read_bytes()
    manager = SessionManager()
    asyncio.run(manager.update_session(1, {"model": "haiku"}))
    monkeypatch.setattr(_bot, "session_manager", SessionManager())
    monkeypatch.setattr(_bot, "machine_opener_text", lambda: None)
    bridge = sdk_bridge.SdkBridge()
    client = SimpleNamespace(query=AsyncMock())
    stream = sdk_bridge._UserStreamState(client=client, model="sonnet")
    bridge._streams[1] = stream
    bridge._get_or_create_stream = AsyncMock(return_value=stream)
    monkeypatch.setattr(_bot, "sdk_bridge", bridge)
    instance = make_bot()
    instance._proactive_push = AsyncMock()
    message = SimpleNamespace(reply_text=AsyncMock())
    asyncio.run(instance._open_first_contact(1, 1, message))
    bridge._get_or_create_stream.assert_awaited_once_with(1, "sonnet", new_session=False)
    client.query.assert_awaited_once()
    assert model_state.LAST_MODEL_PATH.read_bytes() == before
    assert model_state.LAST_MODEL_PATH.stat().st_mtime_ns == 1
    message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("source", ["resolved", "background", "injected"])
def test_non_owner_persist_is_refused(state, source):
    model_state.persist_model("haiku", _bot._known_models(), source=source)
    assert not model_state.LAST_MODEL_PATH.exists()


@pytest.mark.parametrize("settings_path", ["WORKSPACE_SETTINGS_PATH", "GLOBAL_SETTINGS_PATH"])
def test_changed_settings_supersede_preference(state, settings_path):
    model_state.persist_model("haiku", _bot._known_models(), source="cmd_model")
    model_state.WORKSPACE_SETTINGS_PATH.unlink()
    path = getattr(model_state, settings_path)
    path.write_text('{"model": "opus"}')
    newer = model_state.LAST_MODEL_PATH.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(newer, newer))
    assert make_bot()._get_real_model({"model": "haiku"}) == "opus"


def test_workspace_precedence_is_kept_when_global_is_newer(state):
    model_state.persist_model("haiku", _bot._known_models(), source="cmd_model")
    model_state.GLOBAL_SETTINGS_PATH.write_text('{"model": "opus"}')
    newer = model_state.LAST_MODEL_PATH.stat().st_mtime_ns + 1_000_000_000
    os.utime(model_state.GLOBAL_SETTINGS_PATH, ns=(newer, newer))
    assert make_bot()._get_real_model({}) == "haiku"


def test_live_owner_override_survives_persist_failure_but_not_restart(state, monkeypatch):
    # Preserve the current conversation's explicit selection even if disk I/O
    # fails; stale cached overrides still cannot outrank settings on restart.
    model_state.LAST_MODEL_PATH.mkdir()
    instance = make_bot()
    manager = SessionManager()
    monkeypatch.setattr(_bot, "session_manager", manager)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    asyncio.run(instance._cmd_model(update, SimpleNamespace(args=["haiku"])))
    session = asyncio.run(manager.get_session(1))
    assert instance._get_real_model(session) == "haiku"
    monkeypatch.setattr(_bot, "_MODEL_OVERRIDE_BOOT", "restarted-process")
    assert make_bot()._get_real_model(session) == "sonnet"


if __name__ == "__main__":
    unittest.main()
