"""DGN-1984: /usage menu -- two entry buttons (reset passes / API), a back
button on the LEFT of every sub-view instead of close, and every view gated
on what the user onboarded (Codex, API).

Owner 2026-10-10 19:15. Offline: temp HOME (a fixture Codex auth.json signs
Codex in), the real reset-pass lib, a stub claude-usage.sh, a stub
codex-ratelimit.py, a stub `security` CLI on PATH for the keychain (macOS
path, its own case) and the shared file store (other hosts), fake update / callback query.
Covers each onboarding combination (none / Codex / API / both): the main
view's buttons and Codex block, the reset-pass view and its add step, the API
view; back returns to the main view (cached, and re-read after a restart).
"""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bridge.tests.conftest  # noqa: F401
from bridge import bot as bot_mod
from bridge import passes, usage_menu, secret_intake
from bridge.config import config
from bridge.i18n import en

LIB = Path(__file__).resolve().parents[2] / "routines" / "lib" / "reset_passes.py"
needs_lib = pytest.mark.skipif(not LIB.is_file(), reason="reset_passes lib not shipped here")

CHAT = 4242
CREDIT = {"budget": 200.0, "remaining": 95.83, "truth": "console", "truth_age": 300,
          "inflight_n": 0, "inflight_usd": 0.0, "exhausted": False}
COMBOS = [(False, False), (True, False), (False, True), (True, True)]   # (codex, api)
IDS = ["none", "codex", "api", "both"]


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A temp instance root + HOME; returns a setter for the onboarding."""
    root = tmp_path / "inst"
    home = tmp_path / "home"
    home.mkdir()
    conf = root / "config" / "agent.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("PLAN=max\n")
    _exe(root / "routines" / "claude-usage.sh",
         "#!/bin/sh\necho 'Claude 5h  12%'\necho 'Claude 7d  40%'\n")
    calls = tmp_path / "codex-calls"
    _exe(root / "routines" / "codex-ratelimit.py",
         "import json, pathlib\n"
         "p = pathlib.Path(%r); p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
         "print(json.dumps({'ok': True, 'used_percent': 37, 'window_duration_mins': 10080,"
         " 'resets_at': None}))\n" % str(calls))
    bindir = tmp_path / "bin"
    keychain = tmp_path / "keychain-has-item"
    _exe(bindir / "security",
         "#!/bin/sh\n[ -e %s ] && exit 0\nexit 44\n" % keychain)
    monkeypatch.setenv("PATH", "%s%s%s" % (bindir, os.pathsep, os.environ.get("PATH", "")))
    monkeypatch.setenv("HOME", str(home))
    for k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "RESET_PASSES_CLAUDE", "RESET_PASSES_CODEX",
              "ANTHROPIC_API_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", root)
    monkeypatch.setattr(usage_menu, "PROJECT_ROOT", root)
    monkeypatch.setattr(passes, "_lib", lambda: LIB)
    monkeypatch.setattr(passes, "_conf", lambda: conf)
    monkeypatch.setattr(passes, "credit_summary", lambda: dict(CREDIT))
    monkeypatch.setenv("DOGANY_SECRET_BACKEND", "file")
    monkeypatch.setenv("DOGANY_SECRET_DIR", str(home / ".dogany" / "secrets"))
    store = secret_intake.load_lib("secret_store")
    monkeypatch.setattr(config, "locale", "en")

    def onboard(codex: bool, api: bool) -> None:
        if codex:
            (home / ".codex").mkdir(exist_ok=True)
            (home / ".codex" / "auth.json").write_text(
                json.dumps({"tokens": {"account_id": "acct-fixture-1984"}}))
        if api:
            store.set(usage_menu.API_KEYCHAIN_SERVICE, usage_menu.API_KEYCHAIN_ACCOUNT, "sk-ant-fixture")
    onboard.calls = calls
    onboard.keychain = keychain
    return onboard


def _bot():
    b = bot_mod.TelegramBot()
    b._check_access = AsyncMock(return_value=True)
    return b


def _update():
    msg = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=CHAT))


def _query():
    return SimpleNamespace(edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())


def _buttons(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def _sent(mock):
    call = mock.await_args
    return call.args[0], _buttons(call.kwargs["reply_markup"])


MAIN = [[("🎟 Reset passes", passes.CB_OPEN), ("🔑 API", usage_menu.CB_API)]]
BACK = ("‹ Back", usage_menu.CB_MAIN)


@needs_lib
@pytest.mark.asyncio
@pytest.mark.parametrize("codex,api", COMBOS, ids=IDS)
async def test_every_onboarding_combination(env, codex, api):
    env(codex, api)
    b, upd = _bot(), _update()

    # /usage main view: always the two buttons; Codex usage only when onboarded.
    await b._cmd_usage(upd, None)
    body, rows = _sent(upd.message.reply_text)
    assert rows == MAIN
    assert "Claude 7d  40%" in body
    assert ("Codex" in body) is codex
    if codex:
        assert "Codex Usage Limits" in body and "] 37%" in body and "weekly" in body
    assert env.calls.exists() is codex                       # no Codex probe when not onboarded

    # reset-pass view: Codex hidden unless onboarded; back on the LEFT.
    q = _query()
    await b._handle_passes_callback(q, passes.CB_OPEN)
    text, rows = _sent(q.edit_message_text)
    assert ("Codex" in text) is codex
    assert "Claude" in text
    assert rows[-1] == [BACK, (en.STRINGS["passes_btn_add"], passes.CB_ADD)]
    assert passes.CB_CLOSE not in [cb for row in rows for _, cb in row]
    await b._handle_passes_callback(q, passes.CB_ADD)
    _, rows = _sent(q.edit_message_text)
    assert [label for label, _ in rows[0]] == (["Claude", "Codex"] if codex else ["Claude"])

    # API view: onboarding button only, or the console-sourced credit.
    q = _query()
    await b._handle_usage_menu_callback(q, usage_menu.CB_API, CHAT)
    text, rows = _sent(q.edit_message_text)
    if api:
        assert rows == [[BACK]]
        assert "$95.83 of $200.00 left this month" in text
        assert "Read from the console 5m ago" in text
    else:
        assert rows == [[BACK, ("Connect the API", usage_menu.CB_API_SETUP)]]
        assert "left this month" not in text
        assert en.STRINGS["usage_menu_api_not_onboarded"] in text

    # back returns to the main view: same body, same two buttons.
    await b._handle_usage_menu_callback(q, usage_menu.CB_MAIN, CHAT)
    back_body, back_rows = _sent(q.edit_message_text)
    assert back_rows == MAIN and back_body == body
    assert q.edit_message_text.await_args.kwargs["parse_mode"] == "HTML"


@needs_lib
@pytest.mark.asyncio
async def test_api_setup_screen_and_its_back(env):
    env(False, False)
    q = _query()
    await _bot()._handle_usage_menu_callback(q, usage_menu.CB_API_SETUP, CHAT)
    text, rows = _sent(q.edit_message_text)
    assert text == en.STRINGS["usage_menu_api_setup"]
    assert rows == [[("‹ Back", usage_menu.CB_API)]]


@needs_lib
@pytest.mark.asyncio
async def test_back_after_restart_rereads_the_report(env):
    env(True, False)
    q = _query()
    await _bot()._handle_usage_menu_callback(q, usage_menu.CB_MAIN, CHAT)   # nothing cached
    body, rows = _sent(q.edit_message_text)
    assert rows == MAIN
    assert "Claude 5h  12%" in body and "Codex" in body


@pytest.mark.skipif(not usage_menu.API_KEYCHAIN_SERVICE, reason="no keychain item in this build")
def test_api_keychain_on_macos(env, monkeypatch):
    monkeypatch.setenv("DOGANY_SECRET_BACKEND", "keychain")
    monkeypatch.setenv("DOGANY_SECURITY_BIN", "security")
    assert usage_menu.api_onboarded() is False                  # stub `security`: no item
    env.keychain.write_text("")
    assert usage_menu.api_onboarded() is True


@pytest.mark.skipif(not usage_menu.API_KEYCHAIN_SERVICE, reason="no keychain item in this build")
def test_api_store_file_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("DOGANY_SECRET_BACKEND", "file")
    monkeypatch.setenv("DOGANY_SECRET_DIR", str(tmp_path / "secrets"))
    store = secret_intake.load_lib("secret_store")
    assert usage_menu.api_onboarded() is False
    store.set(usage_menu.API_KEYCHAIN_SERVICE, usage_menu.API_KEYCHAIN_ACCOUNT, "sk-ant-fixture")
    assert usage_menu.api_onboarded() is True
    next((tmp_path / "secrets").iterdir()).chmod(0o644)
    assert usage_menu.api_onboarded() is False


def test_api_presence_uses_only_store_has(monkeypatch):
    from unittest.mock import MagicMock
    store = MagicMock()
    store.has.return_value = True
    monkeypatch.setattr(secret_intake, "load_lib", lambda name: store)
    assert usage_menu.api_onboarded() is True
    store.has.assert_called_once_with(usage_menu.API_KEYCHAIN_SERVICE, usage_menu.API_KEYCHAIN_ACCOUNT)
    store.get.assert_not_called()
    store.has.side_effect = RuntimeError("private detail")
    assert usage_menu.api_onboarded() is False


def test_codex_read_failure_is_one_line(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "locale", "en")
    script = _exe(tmp_path / "codex-ratelimit.py",
                  "import json\nprint(json.dumps({'ok': False, 'kind': 'timeout'}))\n")
    rule = "\u2500" * 27
    assert usage_menu.codex_usage_lines(script) == [
        "Codex Usage Limits", rule, en.STRINGS["usage_menu_codex_unavailable"], rule]
    assert usage_menu.codex_usage_lines(tmp_path / "absent.py")[-2] == \
        en.STRINGS["usage_menu_codex_unavailable"]


def test_copy_and_wiring():
    import inspect
    from bridge.i18n import ko
    keys = {k for k in en.STRINGS if k.startswith("usage_menu_")}
    assert len(keys) >= 10 and keys == {k for k in ko.STRINGS if k.startswith("usage_menu_")}
    assert bot_mod._callback_ttl("usage:api") is None
    assert usage_menu.CB == "usage:" and 'data.startswith("usage:")' in inspect.getsource(
        bot_mod.TelegramBot._handle_callback)
    assert usage_menu.parse_callback("usage:evil") == ""
    assert all(len(cb.encode()) <= 64 for cb in (usage_menu.CB_MAIN, usage_menu.CB_API,
                                                  usage_menu.CB_API_SETUP))


def test_codex_card_has_passes_inside_its_block(tmp_path, monkeypatch):
    """DGN-2060: Codex card = Claude layout, weekly window, one line per pass."""
    monkeypatch.setattr(config, "locale", "en")
    script = _exe(tmp_path / "codex-ratelimit.py",
                  "import json\nprint(json.dumps({'ok': True, 'used_percent': 87,"
                  " 'window_duration_mins': 10080, 'resets_at': 4102444800}))\n")
    rows = [{"label": "2099-10-23", "expired": False}, {"label": "2099-10-30", "expired": False}]
    lines = usage_menu.codex_usage_lines(script, rows)
    assert lines[0] == "Codex Usage Limits" and lines[1] == lines[-1] == "─" * 27
    assert lines[2:4] == ["weekly", "[" + "█" * 17 + "░" * 3 + "] 87%"]
    assert lines[4].startswith("reset 2100-") or lines[4].startswith("reset 01-01")
    assert lines[5:8] == ["▸ Reset passes: 2", "· expires 10-23", "· expires 10-30"]
    assert "Reset passes" not in "\n".join(usage_menu.codex_usage_lines(script, []))
