"""DGN-675: push.sh --text cron rail must render code blocks as HTML <pre>
tags, not emit raw triple-backtick fences to Telegram.

Root cause (DGN-675): before DGN-822, push.sh --text sent raw curl with no
parse_mode, so triple-backtick fences appeared verbatim in Telegram messages.
DGN-822 wired the shell rail through sanitize_message_for_telegram +
parse_mode=HTML (routines/push.sh:391,396 sanitize hop; :488,492
parse_mode=HTML send), resolving the gap. Landed originally as a standalone
script in routines/tests/ (dgn-675-autobatch branch, 2026-08-12); moved here
because routines/tests/ has no test runner reading it (neither the SUITE
class, which only scans <root>/tests/, nor the PYTEST class, which only
scans agents/.template/bridge/tests/, bridge/tests/, and packs/*/tests/ --
git-hooks/run-tests.sh, verified 2026-09-03) while bridge/tests/ does, and
this is exactly the function under test (sanitize_message_for_telegram) that
other bridge/tests/ files (e.g. test_dgn822_formatting_telegram_free.py,
test_dgn974_pin_parse_mode.py) already exercise from this directory.

These tests verify the SANITIZER CONTRACT -- the formatting.py function that
push.sh pipes through. They confirm:
  (a) plain code block -> <pre>...</pre> with HTML-escaped content
  (b) fenced block with language tag -> <pre><code class="language-...">
  (c) inline backtick -> <code>...</code>
  (d) mixed prose + code: prose unaffected, code lifted to <pre>
  (e) raw backtick fence never reaches output (zero-leak invariant)
  (f) multi-block: multiple fenced regions each become individual <pre> segments
  (g) HTML-escape inside code block: & < > are escaped (injection safe)
"""

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-dgn675")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.formatting import sanitize_message_for_telegram


def test_a_plain_code_block_becomes_pre():
    """(a) plain ``` fence -> <pre>...</pre>, no raw backticks in output."""
    body = "```\necho hello\n```"
    result = sanitize_message_for_telegram(body)
    assert "<pre>" in result, "Expected <pre> tag in output, got: {!r}".format(result)
    assert "echo hello" in result, "Expected code content preserved, got: {!r}".format(result)
    assert "```" not in result, "Raw ``` fence must not appear in output, got: {!r}".format(result)


def test_b_fenced_block_with_language_tag():
    """(b) ```python fence -> <pre><code class="language-python">...</code></pre>."""
    body = "```python\nx = 1 + 2\nprint(x)\n```"
    result = sanitize_message_for_telegram(body)
    assert 'class="language-python"' in result, \
        "Expected language class attribute, got: {!r}".format(result)
    assert "<pre>" in result, "Expected <pre> wrapper, got: {!r}".format(result)
    assert "```" not in result, "Raw ``` fence must not appear, got: {!r}".format(result)


def test_c_inline_backtick_becomes_code():
    """(c) inline `code` span -> <code>...</code>."""
    body = "Use `push.sh --text` to send."
    result = sanitize_message_for_telegram(body)
    assert "<code>" in result, "Expected <code> tag, got: {!r}".format(result)
    assert "push.sh --text" in result, "Expected code content, got: {!r}".format(result)


def test_d_mixed_prose_and_code():
    """(d) prose before and after code block: prose preserved, code lifted to <pre>."""
    body = "요약:\n```\nls -la\n```\n완료."
    result = sanitize_message_for_telegram(body)
    assert "요약" in result, "Prose before code must survive, got: {!r}".format(result)
    assert "완료" in result, "Prose after code must survive, got: {!r}".format(result)
    assert "<pre>" in result, "Code block must become <pre>, got: {!r}".format(result)
    assert "ls -la" in result, "Code content must survive, got: {!r}".format(result)
    assert "```" not in result, "Raw ``` fence must not appear, got: {!r}".format(result)


def test_e_zero_raw_fence_leak():
    """(e) zero-leak invariant: no triple-backtick string survives sanitization."""
    bodies = [
        "```\nsingle block\n```",
        "```bash\ngit status\n```",
        "before\n```\ncode\n```\nafter",
        "```\nfirst\n```\n\n```\nsecond\n```",
    ]
    for body in bodies:
        result = sanitize_message_for_telegram(body)
        assert "```" not in result, \
            "Raw ``` leaked in output for input {!r}: got {!r}".format(body, result)


def test_f_multiple_code_blocks():
    """(f) multiple fenced regions each become separate <pre> segments."""
    body = "Block 1:\n```\nalpha\n```\n\nBlock 2:\n```\nbeta\n```"
    result = sanitize_message_for_telegram(body)
    assert result.count("<pre>") >= 2, \
        "Expected at least 2 <pre> tags for 2 code blocks, got: {!r}".format(result)
    assert "alpha" in result, "First block content must survive, got: {!r}".format(result)
    assert "beta" in result, "Second block content must survive, got: {!r}".format(result)
    assert "```" not in result, "Raw ``` must not appear, got: {!r}".format(result)


def test_g_html_escape_inside_code():
    """(g) HTML-special chars inside a code block are escaped (injection safe)."""
    body = "```\nif x < 10 && y > 0:\n    pass\n```"
    result = sanitize_message_for_telegram(body)
    assert "&lt;" in result, "Expected &lt; escape for <, got: {!r}".format(result)
    assert "&gt;" in result, "Expected &gt; escape for >, got: {!r}".format(result)
    inner = result.replace("<pre>", "").replace("</pre>", "")
    assert "<" not in inner or all(
        tag in inner for tag in ["<pre>", "</pre>", "<code", "</code>"]
    ), "Unescaped < inside code block -- injection risk: {!r}".format(result)
