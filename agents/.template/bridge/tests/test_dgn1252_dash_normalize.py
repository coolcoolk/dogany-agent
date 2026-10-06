"""DGN-1252: dash egress normalization -- em dash / ` -- ` -> `:`.

Locked spec (worklog/DGN-1252-dash-egress-normalization.md section 4/5):
  - `A — B`  -> `A: B`   (single, non-repeated U+2014)
  - `A -- B` -> `A: B`   (ASCII, exactly two hyphens, whitespace both sides)
  - Repeats (`---`, `----`, `——`, any run) are UNTOUCHABLE.
  - `--flag` forms (`promote.sh --check`, `--worktree`) never convert.
  - Never inside a complete ``` fence, inline `code`, a markdown link target,
    or a standalone `---` thematic-break line.
  - DASH_NORMALIZE=on|off (instance .telegram_bot/.env via bridge.config),
    default on; off = exact passthrough, no log noise.
  - Applied at ONE egress point: inside markdown_to_telegram_html (see the
    DGN-1252 comment there for the chosen-point rationale).
"""

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import os  # noqa: E402

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge import config as config_mod  # noqa: E402
from bridge.formatting import (  # noqa: E402
    markdown_to_telegram_html as md2html,
    normalize_dashes,
)


# ---------------------------------------------------------------------------
# Core substitution rules (spec section 5, direct on normalize_dashes).
# ---------------------------------------------------------------------------

def test_em_dash_padded_converts_to_colon():
    assert normalize_dashes("A — B") == "A: B"


def test_ascii_double_hyphen_padded_converts_to_colon():
    assert normalize_dashes("A -- B") == "A: B"


def test_thematic_break_line_untouched():
    assert normalize_dashes("---") == "---"
    assert normalize_dashes("line1\n---\nline2") == "line1\n---\nline2"


def test_double_em_dash_repeat_untouched():
    assert normalize_dashes("A —— B") == "A —— B"


def test_longer_em_dash_repeat_untouched():
    assert normalize_dashes("A ——— B") == "A ——— B"


def test_longer_hyphen_repeat_untouched():
    assert normalize_dashes("A ---- B") == "A ---- B"
    assert normalize_dashes("A --- B") == "A --- B"


def test_flag_form_untouched():
    assert normalize_dashes("promote.sh --check") == "promote.sh --check"
    assert normalize_dashes("git merge-base --is-ancestor HEAD") == (
        "git merge-base --is-ancestor HEAD"
    )
    assert normalize_dashes("run --worktree foo") == "run --worktree foo"
    assert normalize_dashes("--worktree foo") == "--worktree foo"


def test_fenced_code_block_untouched():
    text = "before\n```\nA — B\ngit x -- y\n```\nafter — done"
    out = normalize_dashes(text)
    assert "A — B\ngit x -- y" in out  # fenced content byte-identical
    assert "after: done" in out  # prose outside the fence still converts


def test_unterminated_fence_is_scrubbed_as_prose():
    # Module-wide rule (shared with strip_toolcall_markup): an opening ```
    # with no closer is NOT a real code block.
    text = "```\nA — B"
    assert normalize_dashes(text) == "```\nA: B"


def test_inline_code_untouched():
    text = "see `a — b` for detail"
    assert normalize_dashes(text) == "see `a — b` for detail"


def test_inline_code_with_ascii_hyphen_untouched():
    text = "run `foo -- bar` now"
    assert normalize_dashes(text) == "run `foo -- bar` now"


def test_markdown_link_target_untouched():
    text = "see [docs](https://example.com/a--b) for --more"
    out = normalize_dashes(text)
    assert "https://example.com/a--b" in out
    # bare "--more" (no trailing whitespace) never matches the ASCII rule
    # anyway; the assertion pins the link stays literal too.
    assert "[docs](https://example.com/a--b)" in out


def test_ascii_double_hyphen_tab_padded_converts():
    # Grill finding: an inner fast-path guard once checked for the literal
    # " -- " (space-padded) substring, silently skipping the regex walk (and
    # thus the conversion) for tab-padded input even though the regex itself
    # accepts tabs. Pinned so that mismatch can never regress silently.
    assert normalize_dashes("a\t--\tb") == "a: b"


def test_back_to_back_double_hyphen_tokens_untouched():
    # Grill finding: "a -- -- b" converts NEITHER token. The shared trailing
    # whitespace between the two tokens means the first match's mandatory
    # (?!-) lookahead sees the second token's leading hyphen and refuses,
    # and the second token then has no leading whitespace left to match
    # (already consumed as the first attempt's trailing space). This is
    # conservative-by-construction, not a special case to maintain.
    assert normalize_dashes("a -- -- b") == "a -- -- b"


def test_bare_url_double_hyphen_untouched():
    # No literal space in a URL, so the ASCII rule's whitespace-both-sides
    # trigger can never fire inside one.
    text = "curl https://x.test/a--b--c"
    assert normalize_dashes(text) == text


# ---------------------------------------------------------------------------
# Toggle (spec 4.3): DASH_NORMALIZE=on|off via bridge.config, default on.
# ---------------------------------------------------------------------------

def test_default_on_when_unset(monkeypatch):
    monkeypatch.setattr(config_mod, "DASH_NORMALIZE", True)
    assert normalize_dashes("A — B") == "A: B"


def test_off_is_exact_passthrough(monkeypatch):
    monkeypatch.setattr(config_mod, "DASH_NORMALIZE", False)
    text = "A — B and A -- B and --- and `x — y`"
    assert normalize_dashes(text) == text


def test_off_emits_no_log_noise(monkeypatch, caplog):
    monkeypatch.setattr(config_mod, "DASH_NORMALIZE", False)
    with caplog.at_level(logging.DEBUG, logger="bridge.formatting"):
        normalize_dashes("A — B")
    assert caplog.records == []


def test_missing_config_attr_fails_open_to_on(monkeypatch):
    # Mirrors the _i18n() fail-open contract: if bridge.config cannot supply
    # the flag (e.g. the no-venv sanitize hop, or a version-skewed config.py
    # predating this flag), the default is ON, not off.
    monkeypatch.delattr(config_mod, "DASH_NORMALIZE", raising=True)
    assert normalize_dashes("A — B") == "A: B"


# ---------------------------------------------------------------------------
# Egress point: wired into markdown_to_telegram_html (spec 4.4), not a
# second call site per branch.
# ---------------------------------------------------------------------------

def test_egress_point_markdown_to_telegram_html(monkeypatch):
    monkeypatch.setattr(config_mod, "DASH_NORMALIZE", True)
    assert md2html("A — B") == "A: B"
    assert md2html("A -- B") == "A: B"


def test_egress_point_respects_toggle_off(monkeypatch):
    monkeypatch.setattr(config_mod, "DASH_NORMALIZE", False)
    assert md2html("A — B") == "A — B"


def test_egress_point_still_protects_inline_code_and_fences(monkeypatch):
    monkeypatch.setattr(config_mod, "DASH_NORMALIZE", True)
    out = md2html("prose — text with `code -- x` inline")
    assert "<code>code -- x</code>" in out
    assert "prose: text" in out


# ---------------------------------------------------------------------------
# DGN-704/881 regression: OPTIONS button label width vs dash normalization
# (spec section 5 + section 6 open question (a)).
#
# Measured answer: bridge.options never imports/calls markdown_to_telegram_html
# (grep confirms zero references) -- button labels are parsed straight from
# the [[OPTIONS: ...]] marker text and width-checked as-is. The width formula
# itself is also numerically indifferent to this specific substitution: em
# dash (unicodedata.east_asian_width == "A") and ":" (== "Na") both fall into
# _label_width's "everything else" 1.0-weight branch, so even if a label DID
# flow through normalize_dashes first, a single "—" vs ":" would not change
# which side of the 31.0 threshold a borderline label lands on.
# ---------------------------------------------------------------------------

def test_options_label_width_sees_pre_normalization_text():
    from bridge.options import _label_width

    label = "1. 실행—빠르게"
    assert _label_width(label) == _label_width(normalize_dashes(label))


# ---------------------------------------------------------------------------
# Spec section 7 "추가 테스트": R1 (command-signal line skips ASCII only),
# R2 (2+ ASCII dash tokens skips the whole line), R3 (leading dash skips the
# whole line). Ticket-measured defects, pinned verbatim.
# ---------------------------------------------------------------------------

def test_r1_command_line_with_flag_and_separator_untouched():
    text = "git log --all -S'x' -- bridge/"
    assert normalize_dashes(text) == text


def test_r2_two_ascii_dash_tokens_untouched():
    text = "tests/x   --   --   0"
    assert normalize_dashes(text) == text


def test_r3_leading_em_dash_bullet_untouched():
    text = "— 목록 항목"
    assert normalize_dashes(text) == text


def test_r3_leading_single_hyphen_bullet_untouched():
    # Already unmatched by the ASCII rule itself (needs exactly two
    # hyphens); pinned as a regression so R3 never disturbs it either.
    text = "- 목록 항목"
    assert normalize_dashes(text) == text


def test_r1_overskip_is_intended_when_line_merely_mentions_a_script():
    # Owner-approved cost: R1 fires on the .py extension token alone, with
    # no actual command verb on the line. A prose dash is left unconverted.
    text = "bot.py 889줄이라는 숫자 -- 겁먹을 필요 없다"
    assert normalize_dashes(text) == text


def test_ordinary_em_dash_line_still_converts_despite_r1_r2_r3_being_moot():
    text = "1. **stable 직행** — 2.1.0에 한해"
    assert normalize_dashes(text) == "1. **stable 직행**: 2.1.0에 한해"


def test_r1_skip_is_ascii_only_em_dash_on_same_command_line_still_converts():
    # R1's ticket text is explicit: it applies to ASCII dashes only, so an
    # em dash sharing a command-signal line is unaffected by R1.
    text = "git status — check before commit"
    assert normalize_dashes(text) == "git status: check before commit"


def test_r2_skip_does_not_leak_to_neighbouring_line():
    text = "tests/x   --   --   0\nA -- B"
    out = normalize_dashes(text)
    lines = out.split("\n")
    assert lines[0] == "tests/x   --   --   0"
    assert lines[1] == "A: B"


def test_r3_skip_does_not_leak_to_neighbouring_line():
    text = "— bullet line\nA -- B"
    out = normalize_dashes(text)
    lines = out.split("\n")
    assert lines[0] == "— bullet line"
    assert lines[1] == "A: B"


def test_r1_skip_does_not_leak_to_neighbouring_line():
    text = "git status -- check\nA -- B"
    out = normalize_dashes(text)
    lines = out.split("\n")
    assert lines[0] == "git status -- check"
    assert lines[1] == "A: B"


def test_normal_conversion_still_works_on_a_clean_neighbour_before_a_skip_line():
    text = "A -- B\ntests/x   --   --   0"
    out = normalize_dashes(text)
    lines = out.split("\n")
    assert lines[0] == "A: B"
    assert lines[1] == "tests/x   --   --   0"
