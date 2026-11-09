"""DGN-1926 (dec-315): the recommended option carries its marker onto the button."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pytest

from bridge.config import config
from bridge.options import (
    build_option_keyboard,
    extract_marker_labels,
    strip_consumed_options,
)


def _opts(text):
    return strip_consumed_options(text, marker_labels=extract_marker_labels(text))[1]


def _texts(kb):
    return [row[0].text for row in kb.inline_keyboard]


@pytest.mark.parametrize("loc,mk", [("ko", "(추천)"), ("en", "(rec)")])
def test_labeled_marker_gets_body_marker(monkeypatch, loc, mk):
    monkeypatch.setattr(config, "locale", loc)
    text = f"1. 실행 {mk}\n2. 대기\n\n[[OPTIONS: 실행 | 대기]]"
    opts = _opts(text)
    assert opts == [f"실행 {mk}", "대기"]
    assert _texts(build_option_keyboard(opts)) == [f"1. 실행 {mk}", "2. 대기"]


def test_no_double_marker_when_label_has_it(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    text = "1. 실행 (추천)\n2. 대기\n\n[[OPTIONS: 실행 (추천) | 대기]]"
    assert _opts(text) == ["실행 (추천)", "대기"]


def test_numbered_run_keeps_marker(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    text = "1. 실행 (추천)\n2. 대기\n[[OPTIONS]]"
    _, opts = strip_consumed_options(text)
    assert _texts(build_option_keyboard(opts))[0] == "1. 실행 (추천)"


def test_unrelated_run_lends_no_marker(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    text = "1. 다른 (추천)\n2. 것\n\n[[OPTIONS: 실행 | 대기]]"
    assert _opts(text) == ["실행", "대기"]


def test_overflow_pinned_falls_back_to_handles(monkeypatch):
    """Marker counts in width; tipping over degrades the whole keyboard (no trimming)."""
    monkeypatch.setattr(config, "locale", "ko")
    text = "1. 가나다라마바사아자차카타파하가나 (추천)\n2. 대기\n\n[[OPTIONS: 가나다라마바사아자차카타파하가나 | 대기]]"
    opts = _opts(text)
    assert opts[0].endswith("(추천)")
    assert _texts(build_option_keyboard(opts)) == ["1번", "2번"]
