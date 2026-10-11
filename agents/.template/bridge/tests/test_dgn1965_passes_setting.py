"""DGN-1965 follow-up: /passes, the user's reset-pass setting.

The surface lists the passes per runtime (soonest first) with their kind,
adds one through runtime -> date -> kind buttons and removes one per button;
every read and write goes through routines/lib/reset_passes.py against
config/agent.conf. Offline: a temp conf, the real lib, a fake callback query.
Follow-up 2 (dec-340): the ko copy is owner-confirmed and pinned verbatim.
"""

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.tests.conftest  # noqa: F401
from bridge import bot as bot_mod
from bridge import messages, passes
from bridge.config import config
from bridge.i18n import en, ko

LIB = Path(__file__).resolve().parents[2] / "routines" / "lib" / "reset_passes.py"
needs_lib = pytest.mark.skipif(not LIB.is_file(), reason="reset_passes lib not shipped here")


@pytest.fixture()
def conf(tmp_path, monkeypatch):
    path = tmp_path / "agent.conf"
    path.write_text("PLAN=max\n# RESET_PASSES_CODEX='3|'\n")
    monkeypatch.setattr(passes, "_lib", lambda: LIB)
    monkeypatch.setattr(passes, "_conf", lambda: path)
    monkeypatch.delenv("RESET_PASSES_CLAUDE", raising=False)
    monkeypatch.delenv("RESET_PASSES_CODEX", raising=False)
    monkeypatch.setattr(config, "locale", "en")
    return path


def _labels(rows):
    return [label for row in rows for label, _ in row]


def _data(rows):
    return [cb for row in rows for _, cb in row]


def test_copy_in_both_locales():
    keys = [k for k in en.STRINGS if k.startswith("passes_")]
    assert len(keys) >= 20
    assert set(keys) == {k for k in ko.STRINGS if k.startswith("passes_")}


@needs_lib
def test_add_list_remove_roundtrip(conf):
    inv, err = passes.run("list")
    assert err == "" and inv["codex"]["passes"] == []
    text, rows = passes.render_list(inv)
    assert text.count(en.STRINGS["passes_none"]) == 2
    assert passes.CB_ADD in _data(rows)

    for exp, kind in (("2099-11-07T08:43", "full"), ("2099-10-23T05:50", "5h"),
                      ("2099-10-30", "weekly")):
        inv, err = passes.run("add", "codex", exp, kind)
        assert err == "", err
    inv, err = passes.run("add", "claude", "unknown")          # no kind -> unknown
    assert err == ""
    line = [l for l in conf.read_text().splitlines() if l.startswith("RESET_PASSES_CODEX=")]
    assert line == ["RESET_PASSES_CODEX='3|2099-10-23T05:50:5h,2099-10-30:weekly,"
                    "2099-11-07T08:43:full'"]
    assert "# RESET_PASSES_CODEX='3|'" in conf.read_text()        # example untouched

    text, rows = passes.render_list(inv)
    assert "· Oct 23 05:50: 5-hour (used first)" in text
    assert "· Oct 30: weekly\n" in text
    assert "· Nov 7 08:43: full" in text
    assert "· Expiry unknown: kind unknown" in text
    assert "rpass:rm:codex:2099-10-23T05:50:5h" in _data(rows)
    assert "restores" not in text

    inv, err = passes.run("remove", "codex", "2099-10-23T05:50", "5h")
    assert err == ""
    assert [(p["expiry"], p["kind"]) for p in inv["codex"]["passes"]] == [
        ("2099-10-30", "weekly"), ("2099-11-07T08:43", "full")]
    inv, err = passes.run("remove", "claude", "unknown")
    assert "RESET_PASSES_CLAUDE" not in conf.read_text()


OWNER_MAIN_SCREEN = """🎟 리셋권

Claude
· 10월 23일: 전체 (먼저 사용)

Codex
· 10월 23일: 전체 (먼저 사용)
· 10월 30일: 전체
· 11월 7일: 전체

등록된 리셋권에 맞춰 작업을 계획해요. 리셋권이 만료되기 전에 한도를 다 쓰고, 쓸 때가 되면 알려드릴게요. 리셋권 사용은 언제나 직접 하시는 거예요."""


def _p(expiry, kind="full", nxt=False, expired=False):
    return {"expiry": expiry, "kind": kind, "next": nxt, "expired": expired}


def test_main_screen_matches_the_owner_copy(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    inv = {"claude": {"valid": True, "passes": [_p("2026-10-23", nxt=True)]},
           "codex": {"valid": True, "passes": [_p("2026-10-23", nxt=True), _p("2026-10-30"),
                                                _p("2026-11-07")]}}
    text, rows = passes.render_list(inv)
    assert text == OWNER_MAIN_SCREEN
    assert rows[-1] == [("➕ 리셋권 추가", passes.CB_ADD), ("닫기", passes.CB_CLOSE)]
    assert rows[0] == [("✕ Claude 10월 23일: 전체", "rpass:rm:claude:2026-10-23:full")]


def test_pass_line_shapes(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    inv = {"claude": {"valid": True, "passes": [_p("2026-10-01", "5h", expired=True),
                                                 _p("unknown", "full")]},
           "codex": {"valid": True, "passes": [_p("2026-10-23T05:50", "weekly", nxt=True),
                                                _p("2026-12-01", "unknown")]}}
    text, _ = passes.render_list(inv)
    assert "· 10월 1일: 5시간 (만료됨)\n" in text
    assert "· 만료일 모름: 전체\n" in text
    assert "· 10월 23일 05:50: 주간 (먼저 사용)\n" in text
    assert "· 12월 1일: 종류 모름\n" in text
    text, _ = passes.render_list({"claude": {"passes": []}, "codex": {"valid": False}})
    assert "Claude\n등록된 리셋권이 없어요.\n" in text
    assert "Codex\n저장된 목록을 읽지 못해서 리셋권이 없는 것으로 보고 있어요. 새로 추가하면 바로잡혀요." in text


def test_add_steps_copy_ko(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    text, rows = passes.render_pick_runtime()
    assert text == "어떤 리셋권을 받으셨어요?"
    assert _labels(rows) == ["Claude", "Codex", "‹ 뒤로"]
    text, rows = passes.render_pick_day("codex", 0, today=date(2026, 10, 9))
    assert text == "Codex 리셋권 만료일이 언제예요? (10/9~11/5)"
    assert "모름" in _labels(rows)
    text, rows = passes.render_pick_kind("codex", "2026-11-07")
    assert text == "이 리셋권은 어떤 한도를 되살려요?"
    assert rows[0] == [("전체", "rpass:new:codex:2026-11-07:full"),
                       ("5시간", "rpass:new:codex:2026-11-07:5h"),
                       ("주간", "rpass:new:codex:2026-11-07:weekly"),
                       ("모름", "rpass:new:codex:2026-11-07:unknown")]
    assert rows[1] == [("‹ 뒤로", "rpass:days:codex:0")]
    assert ko.STRINGS["passes_added"].format(
        runtime="Codex", date=passes.date_label("2026-11-07"),
        kind=passes.kind_label("full")) == "Codex 리셋권을 추가했어요: 11월 7일, 전체"
    assert ko.STRINGS["passes_removed"].format(
        runtime="Claude", date=passes.date_label("unknown"),
        kind=passes.kind_label("5h")) == "Claude 리셋권을 뺐어요: 만료일 모름, 5시간"
    assert ko.STRINGS["passes_btn_open"] == "🎟 리셋권"


def test_dead_copy_is_gone():
    for strings in (en.STRINGS, ko.STRINGS):
        assert not [k for k in strings if k.startswith(("passes_restores_", "passes_runtime_head"))]


def test_split_entry():
    assert passes.split_entry("2026-10-23T05:50:full") == ("2026-10-23T05:50", "full")
    assert passes.split_entry("2026-10-23T05:50") == ("2026-10-23T05:50", "")
    assert passes.split_entry("unknown:5h") == ("unknown", "5h")
    assert passes.split_entry("2026-10-23") == ("2026-10-23", "")


@needs_lib
def test_refusals_come_back_as_errors(conf):
    inv, err = passes.run("add", "codex", "2001-01-01")
    assert inv is None and "past" in err
    inv, err = passes.run("add", "codex", "next tuesday")
    assert inv is None and "bad expiry" in err
    inv, err = passes.run("remove", "codex", "2099-01-01")
    assert inv is None and "no codex pass" in err


@needs_lib
def test_env_override_is_shown(conf, monkeypatch):
    monkeypatch.setenv("RESET_PASSES_CLAUDE", "1|2099-10-23")
    inv, _ = passes.run("list")
    text, _ = passes.render_list(inv)
    assert en.STRINGS["passes_env_override"] in text


def test_missing_lib_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "locale", "en")
    monkeypatch.setattr(passes, "_lib", lambda: tmp_path / "nope.py")
    inv, err = passes.run("list")
    text, rows = passes.render_list(inv, err)
    assert inv is None and en.STRINGS["passes_unavailable"] in text
    assert _data(rows) == [passes.CB_CLOSE]


def test_day_picker_pages_and_bounds():
    text, rows = passes.render_pick_day("codex", 0, today=date(2026, 10, 9))
    data = _data(rows)
    assert data[0] == "rpass:kind:codex:2026-10-09"           # date -> step 3 (kind)
    assert "rpass:kind:codex:2026-11-05" in data             # 28th day
    assert "rpass:days:codex:1" in data and "rpass:days:codex:-1" not in data
    assert "rpass:kind:codex:unknown" in data
    assert all(len(d.encode()) <= 64 for d in data)
    _, rows = passes.render_pick_day("codex", 99, today=date(2026, 10, 9))
    assert "rpass:days:codex:4" not in _data(rows)            # clamped to the last page


def test_parse_callback():
    assert passes.parse_callback("rpass:rm:codex:2026-10-23T05:50") == ("rm", "codex", "2026-10-23T05:50")
    assert passes.parse_callback("rpass:new:codex:2026-10-23T05:50:full") == (
        "new", "codex", "2026-10-23T05:50:full")
    assert passes.parse_callback("rpass:new:evil:2026-10-23") == ("new", "", "2026-10-23")
    assert passes.parse_callback("rpass:open") == ("open", "", "")


@needs_lib
@pytest.mark.asyncio
async def test_tap_flow_adds_and_rerenders(conf):
    b = bot_mod.TelegramBot()
    query = SimpleNamespace(edit_message_text=AsyncMock(),
                            edit_message_reply_markup=AsyncMock())
    await b._handle_passes_callback(query, "rpass:add")
    await b._handle_passes_callback(query, "rpass:days:claude:0")
    await b._handle_passes_callback(query, "rpass:kind:claude:2099-10-23")
    text = query.edit_message_text.await_args.args[0]
    assert text == en.STRINGS["passes_pick_kind"]
    assert "rpass:new:claude:2099-10-23:full" in str(query.edit_message_text.await_args)
    assert "RESET_PASSES_CLAUDE" not in conf.read_text()        # nothing written yet
    await b._handle_passes_callback(query, "rpass:new:claude:2099-10-23:full")
    text = query.edit_message_text.await_args.args[0]
    # messages.* bind the import-time locale; the rest renders live.
    assert messages.PASSES_ADDED.format(runtime="Claude", date="Oct 23", kind="full") in text
    assert "· Oct 23: full (used first)" in text
    assert "RESET_PASSES_CLAUDE='1|2099-10-23:full'" in conf.read_text()
    await b._handle_passes_callback(query, "rpass:rm:claude:2099-10-23:full")
    assert messages.PASSES_REMOVED.format(runtime="Claude", date="Oct 23", kind="full") in \
        query.edit_message_text.await_args.args[0]
    assert "RESET_PASSES_CLAUDE" not in conf.read_text()
    await b._handle_passes_callback(query, "rpass:new:claude:1999-01-01:full")
    assert messages.PASSES_FAILED.split("{")[0] in query.edit_message_text.await_args.args[0]
    await b._handle_passes_callback(query, "rpass:close")
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_command_and_callback_are_wired():
    import inspect
    src = inspect.getsource(bot_mod.TelegramBot._setup_handlers)
    assert 'CommandHandler("passes", self._cmd_passes)' in src
    assert bot_mod._callback_ttl("rpass:new:codex:2099-01-01") is None
    assert 'data.startswith("rpass:")' in inspect.getsource(bot_mod.TelegramBot._handle_callback)
