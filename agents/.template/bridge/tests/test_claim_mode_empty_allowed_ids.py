"""A claim-mode install exports an empty ALLOWED_USER_IDS; boot must not crash.

2026-10-02 rehearsal: the bridge died at config load with
SettingsError(allowed_user_ids) because pydantic-settings JSON-decoded "".
"""
import pytest


@pytest.mark.parametrize("raw,expected", [
    ("", []),
    ("   ", []),
    ("123", [123]),
    ("123,456", [123, 456]),
])
def test_allowed_user_ids_env_forms(monkeypatch, raw, expected):
    monkeypatch.setenv("ALLOWED_USER_IDS", raw)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    # Config reads the environment at instantiation; never reload the module
    # (that rebinds module globals other tests rely on).
    from bridge.config import Config
    assert Config().allowed_user_ids == expected
