"""DGN-1814 r3/r4: /model picker with real versions + layered model table.

Final owner copy (2026-10-01 09:52): one header for every screen ("LLM 모델을
선택해주세요."), "현재:" = the model that really answered (bridge/live_model.py,
this process only) plus a latest-version hint when a configured alias answered
with another version than the table's, the switch note, one button per family
with the full name ("Claude Opus 5.5") and " (현재)" on the current one.
2+ chat vendors in the table -> vendor step first. Versions come from
routines/lib/model_display.py over dispatch-model-display.tsv only.
"""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.tests.conftest  # noqa: F401
from bridge import live_model, model_picker
from bridge.config import config
from bridge.i18n import en, ko

BRIDGE_DIR = Path(__file__).resolve().parents[1]
LIB = BRIDGE_DIR.parent / "routines" / "lib"
TABLE = (LIB / "model_display.py").exists() and (LIB / "dispatch-model-display.tsv").exists()
needs_table = pytest.mark.skipif(not TABLE, reason="routines/lib model table not shipped here")

RANK = {"fable": 0, "opus": 1, "sonnet": 2, "haiku": 3}
ALL = ["sonnet", "opus", "haiku", "fable"]


@pytest.fixture()
def locale(monkeypatch):
    def _set(loc):
        monkeypatch.setattr(config, "locale", loc)
    return _set


def _md():
    spec = importlib.util.spec_from_file_location("md_under_test", str(LIB / "model_display.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _expected_rows():
    """(alias, family, version) of the chat rows, straight from the TSV text."""
    rows = []
    for line in (LIB / "dispatch-model-display.tsv").read_text().splitlines():
        cols = line.split("\t")
        if line.startswith("#") or len(cols) < 5 or "chat" not in cols[4]:
            continue
        words = cols[3].split()
        rows.append((cols[1], words[1], words[2]))
    return rows


# ------------------------------------------------------------- the table ----

@needs_table
def test_table_is_layered_vendor_family_version():
    table = _md().model_table(surface="chat")
    assert list(table) == ["Claude"]
    assert [(e["alias"], fam, e["version"]) for fam, e in table["Claude"].items()] \
        == _expected_rows()
    assert table["Claude"]["Opus"]["display"] == "Claude Opus 5.5"


@needs_table
def test_split_display_shapes():
    md = _md()
    assert md.split_display("Claude Opus 5.5") == ("Claude", "Opus", "5.5")
    assert md.split_display("GPT 6 Astra") == ("GPT", "Astra", "6")
    assert md.split_display("Claude Opus") == ("Claude", "Opus", "")


@needs_table
def test_unmarked_table_has_every_vendor():
    # Without a surface filter the dispatch rows (GPT) are there too: the
    # "chat" mark is what keeps the picker at one vendor.
    assert set(_md().model_table()) == {"Claude", "GPT"}


# ------------------------------------------------- one-vendor picker ----

@needs_table
@pytest.mark.parametrize("loc,head,now,note,mark", [
    ("ko", "LLM 모델을 선택해주세요.", "현재: Claude Sonnet 5.5", "(모델 전환 시 새 세션으로 시작됩니다)", " (현재)"),
    ("en", "Select an LLM model.", "Current: Claude Sonnet 5.5", "(switching starts a new session)", " (current)"),
])
def test_one_vendor_picker_render(locale, loc, head, now, note, mark):
    locale(loc)
    text, buttons = model_picker.render("sonnet", ALL, "claude-sonnet-5-5", RANK)
    assert text == "\n".join([head, now, note])  # live == table: no hint
    assert buttons == [
        ("Claude Fable 5.1", "model:fable"),
        ("Claude Opus 5.5", "model:opus"),
        ("Claude Sonnet 5.5" + mark, "model:sonnet"),
        ("Claude Haiku 4.5", "model:haiku"),
    ]
    assert all("✓" not in label for label, _cb in buttons)


@needs_table
@pytest.mark.parametrize("loc,now", [
    ("ko", "현재: Claude Sonnet 5 (최신은 5.5예요. 재시작하면 바뀔 수 있어요)"),
    ("en", "Current: Claude Sonnet 5 (latest is 5.5; a restart may switch to it)"),
])
def test_latest_hint_when_same_family_answers_with_older_version(locale, loc, now):
    locale(loc)
    text, buttons = model_picker.render("sonnet", ALL, "claude-sonnet-5", RANK)
    assert text.splitlines()[1] == now
    assert buttons[2][0] == ("Claude Sonnet 5.5 (현재)" if loc == "ko"
                             else "Claude Sonnet 5.5 (current)")


@pytest.mark.parametrize("version,word", [
    ("5.5", "5.5예요"), ("4.2", "4.2예요"), ("4.9", "4.9예요"), ("6.4", "6.4예요"),
    ("5.0", "5.0이에요"), ("5.1", "5.1이에요"), ("4.6", "4.6이에요"),
    ("4.3", "4.3이에요"), ("4.7", "4.7이에요"), ("4.8", "4.8이에요"),
])
def test_latest_hint_ko_particle_follows_final_digit(locale, version, word):
    locale("ko")
    table = {"Claude": {"Sonnet": {"alias": "sonnet", "runtime": "claude-code",
                                   "version": version, "display": "Claude Sonnet " + version}}}
    with patch.object(model_picker, "_live_same", return_value="Claude Sonnet 3"), \
            patch.object(model_picker, "_split", return_value=("Claude", "Sonnet", "3")):
        hint = model_picker.latest_hint("sonnet", "claude-sonnet-3", table)
    assert hint == " (최신은 %s. 재시작하면 바뀔 수 있어요)" % word


@needs_table
@pytest.mark.parametrize("configured,live", [
    ("sonnet", "claude-sonnet-5-5"),        # live == table version
    ("sonnet", "claude-opus-4-8"),          # different family
    ("sonnet", ""),                          # no live record yet
    ("sonnet", "not-a-model-id"),            # unknown live id
    ("claude-sonnet-5", "claude-sonnet-5"),  # pinned full id: restart keeps it
])
def test_latest_hint_absent(locale, configured, live):
    for loc in ("ko", "en"):
        locale(loc)
        text, _b = model_picker.render(configured, ALL, live, RANK)
        line = text.splitlines()[1]
        assert "(" not in line, line
        assert model_picker.latest_hint(configured, live, model_picker.chat_table()) == ""


@needs_table
def test_current_marker_follows_configured_model(locale):
    locale("en")
    _t, buttons = model_picker.render("opus", ["sonnet", "opus"], "", RANK)
    assert buttons == [("Claude Opus 5.5 (current)", "model:opus"),
                       ("Claude Sonnet 5.5", "model:sonnet")]


@needs_table
def test_whitelist_limits_buttons_and_current_is_kept(locale):
    locale("en")
    _t, buttons = model_picker.render("claude-opus-5-5", ["sonnet"], "", RANK)
    assert buttons == [("Claude Sonnet 5.5", "model:sonnet"),
                       ("Claude Opus 5.5 (current)", "model:claude-opus-5-5")]


# ------------------------------------------------ "Current:" honesty ----

@needs_table
@pytest.mark.parametrize("live", ["", "claude-opus-5-5", "not-a-model-id"])
def test_unknown_or_other_live_model_falls_back_without_version(locale, live):
    # No live record yet, a live record of a different family (switch just
    # made, no turn yet), or an unparseable id -> configured family, no version.
    locale("ko")
    text, _b = model_picker.render("sonnet", ALL, live, RANK)
    assert text.splitlines()[1] == "현재: Claude Sonnet"


@needs_table
def test_live_version_wins_over_table_when_same_family(locale):
    locale("en")
    text, buttons = model_picker.render("sonnet", ALL, "claude-sonnet-5[1m]", RANK)
    assert text.splitlines()[1].startswith("Current: Claude Sonnet 5 (latest is 5.5")
    assert ("Claude Sonnet 5.5 (current)", "model:sonnet") in buttons  # buttons = table


@needs_table
def test_full_id_configured_needs_exact_live_match(locale):
    locale("en")
    text, _b = model_picker.render("claude-opus-4-8", ALL, "claude-opus-5-5", RANK)
    # Same family, other version: not observed -> no version claimed.
    assert text.splitlines()[1] == "Current: Claude Opus"
    text, _b = model_picker.render("claude-opus-5-5", ALL, "claude-opus-5-5", RANK)
    assert text.splitlines()[1] == "Current: Claude Opus 5.5"


def test_live_record_of_an_earlier_process_is_not_used(tmp_path, monkeypatch):
    path = tmp_path / "state" / "live_model.json"
    monkeypatch.setattr(live_model, "_first_seen", None)
    live_model._write(path, {"model": "claude-sonnet-5", "first_model": "claude-sonnet-5",
                             "prev": "", "boot": 1.0})
    assert live_model.this_process_model(path) == ""
    live_model.observe("claude-sonnet-5-5", path)
    assert live_model.this_process_model(path) == "claude-sonnet-5-5"


# ------------------------------------------- switched / already lines ----

@needs_table
@pytest.mark.parametrize("loc,switched,already", [
    ("ko", "전환 완료: Claude Opus 5.5 · 새 세션으로 시작합니다", "이미 Claude Opus 5.5 모델을 사용 중입니다."),
    ("en", "Switched to Claude Opus 5.5 · new session started", "Already using Claude Opus 5.5."),
])
def test_switched_and_already_lines_carry_version(loc, switched, already):
    cat = ko.STRINGS if loc == "ko" else en.STRINGS
    label = model_picker.full_name("opus", model_picker.chat_table())
    assert cat["model_switched"].format(label=label) == switched
    assert cat["model_already_active"].format(label=label) == already


# ---------------------------------------------- two-vendor (fake) path ----

def _two_vendor_table(tmp_path):
    src = (LIB / "dispatch-model-display.tsv").read_text()
    fake = src + "codex-fake\tsol\tcodex-sol-1\tCodex Sol 1\tchat\n" \
                 "codex-fake\tluna\tcodex-luna-1\tCodex Luna 1\tchat\n"
    tsv = tmp_path / "two.tsv"
    tsv.write_text(fake)
    return _md().model_table(surface="chat", tsv=str(tsv))


@needs_table
@pytest.mark.parametrize("loc,head,now,mark", [
    ("ko", "LLM 모델을 선택해주세요.", "현재: Claude Sonnet 5.5", " (현재)"),
    ("en", "Select an LLM model.", "Current: Claude Sonnet 5.5", " (current)"),
])
def test_two_vendors_turn_the_picker_two_step(tmp_path, locale, loc, head, now, mark):
    locale(loc)
    table = _two_vendor_table(tmp_path)
    assert list(table) == ["Claude", "Codex"]
    text, buttons = model_picker.render("sonnet", ALL, "claude-sonnet-5-5", RANK, table)
    assert text.splitlines()[:2] == [head, now]
    assert buttons == [("Claude" + mark, "modelv:Claude"), ("Codex", "modelv:Codex")]

    text, buttons = model_picker.family_step("Claude", "sonnet", ALL, "claude-sonnet-5-5", RANK, table)
    assert text.splitlines()[:2] == [head, now]
    assert ("Claude Sonnet 5.5" + mark, "model:sonnet") in buttons and len(buttons) == 4

    text, buttons = model_picker.family_step("Codex", "sonnet", ALL, "", RANK, table)
    assert buttons == [("Codex Sol 1", "modelx:Codex:sol"), ("Codex Luna 1", "modelx:Codex:luna")]


# ------------------------------------------------ bot.py /model handler ----

@needs_table
def test_cmd_model_sends_rendered_picker(locale, monkeypatch):
    from bridge import bot as _bot
    locale("ko")
    monkeypatch.setenv("BRIDGE_MODELS", "sonnet,opus,haiku,fable")
    fake = SimpleNamespace(
        _check_access=AsyncMock(return_value=True),
        _get_real_model=lambda session: "sonnet",
        _live_model_id=lambda: "claude-sonnet-5-5",
        _picker_markup=_bot.TelegramBot._picker_markup,
    )
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock()
    ctx = SimpleNamespace(args=[])
    with patch.object(_bot.session_manager, "get_session", AsyncMock(return_value={})):
        asyncio.run(_bot.TelegramBot._cmd_model(fake, update, ctx))
    args, kwargs = update.message.reply_text.call_args
    assert args[0] == "LLM 모델을 선택해주세요.\n현재: Claude Sonnet 5.5\n(모델 전환 시 새 세션으로 시작됩니다)"
    labels = [row[0].text for row in kwargs["reply_markup"].inline_keyboard]
    assert labels == ["Claude Fable 5.1", "Claude Opus 5.5", "Claude Sonnet 5.5 (현재)", "Claude Haiku 4.5"]


# ------------------------------------------------------- code hygiene ----

def test_no_versions_or_non_ascii_in_bridge_picker_code():
    src = (BRIDGE_DIR / "model_picker.py").read_text(encoding="utf-8")
    assert src.isascii()
    bot_src = (BRIDGE_DIR / "bot.py").read_text(encoding="utf-8")
    assert "_MODEL_LABELS" not in bot_src
    assert "(current)" not in bot_src
    for ver in ("5.5", "5.1", "4.5"):
        assert ver not in src
