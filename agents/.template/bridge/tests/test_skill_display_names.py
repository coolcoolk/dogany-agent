"""Tests for DGN-102: SKILL_DISPLAY_NAMES catalog + skill_display_name() resolver.

Covers:
- en/ko key parity (both maps have identical key sets)
- en/ko import sanity (no syntax errors, maps are dicts)
- resolver: known key returns localized label (not the raw id)
- resolver: unknown key falls back to raw id (fail-open, never KeyError)
- resolver: locale switching works correctly
"""

import os
import sys
from pathlib import Path

import pytest

# conftest.py already inserts the package root into sys.path and sets env vars,
# but include a guard here so the test file is also runnable standalone.
_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.i18n import en, ko, skill_display_name  # noqa: E402
from bridge.config import config  # noqa: E402


# ---------------------------------------------------------------------------
# Catalog sanity
# ---------------------------------------------------------------------------

def test_en_display_names_is_dict():
    assert isinstance(en.SKILL_DISPLAY_NAMES, dict)
    assert len(en.SKILL_DISPLAY_NAMES) > 0


def test_ko_display_names_is_dict():
    assert isinstance(ko.SKILL_DISPLAY_NAMES, dict)
    assert len(ko.SKILL_DISPLAY_NAMES) > 0


def test_en_ko_key_parity():
    """en and ko SKILL_DISPLAY_NAMES must have exactly the same key set."""
    en_keys = set(en.SKILL_DISPLAY_NAMES.keys())
    ko_keys = set(ko.SKILL_DISPLAY_NAMES.keys())
    assert en_keys == ko_keys, (
        f"Key mismatch:\n  en only: {en_keys - ko_keys}\n  ko only: {ko_keys - en_keys}"
    )


def test_en_display_names_all_nonempty():
    for skill_id, label in en.SKILL_DISPLAY_NAMES.items():
        assert isinstance(label, str) and label.strip(), (
            f"en.SKILL_DISPLAY_NAMES[{skill_id!r}] is empty"
        )


def test_ko_display_names_all_nonempty():
    for skill_id, label in ko.SKILL_DISPLAY_NAMES.items():
        assert isinstance(label, str) and label.strip(), (
            f"ko.SKILL_DISPLAY_NAMES[{skill_id!r}] is empty"
        )


# ---------------------------------------------------------------------------
# Required skill IDs are present in both catalogs
# ---------------------------------------------------------------------------

REQUIRED_IDS = [
    "dogany-cron-register",
    "dogany-lifekit-setup",
    "dogany-mailer",
    "dogany-memory-search",
    "dogany-proactive-push",
    "dogany-reminder",
    "dogany-skill-creator",
    "dogany-user-onboarding",
    "diet-log",
    "workout-log",
    "appointment-log",
    "relationship",
    "task-update",
]


@pytest.mark.parametrize("skill_id", REQUIRED_IDS)
def test_required_ids_in_en(skill_id):
    assert skill_id in en.SKILL_DISPLAY_NAMES, (
        f"{skill_id!r} missing from en.SKILL_DISPLAY_NAMES"
    )


@pytest.mark.parametrize("skill_id", REQUIRED_IDS)
def test_required_ids_in_ko(skill_id):
    assert skill_id in ko.SKILL_DISPLAY_NAMES, (
        f"{skill_id!r} missing from ko.SKILL_DISPLAY_NAMES"
    )


# ---------------------------------------------------------------------------
# Resolver: en locale
# ---------------------------------------------------------------------------

def test_resolver_known_id_en(monkeypatch):
    monkeypatch.setattr(config, "locale", "en")
    label = skill_display_name("diet-log")
    assert label == en.SKILL_DISPLAY_NAMES["diet-log"]
    assert label != "diet-log"


def test_resolver_known_id_ko(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    label = skill_display_name("diet-log")
    assert label == ko.SKILL_DISPLAY_NAMES["diet-log"]
    assert label != "diet-log"


def test_resolver_ko_diet_log_is_correct_korean(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    assert skill_display_name("diet-log") == "식단 기록"


def test_resolver_ko_workout_log_is_correct_korean(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    assert skill_display_name("workout-log") == "운동 기록"


def test_resolver_ko_reminder(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    assert skill_display_name("dogany-reminder") == "리마인더"


def test_resolver_ko_memory_search(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    assert skill_display_name("dogany-memory-search") == "기억 검색"


# ---------------------------------------------------------------------------
# Resolver: fallback behavior (fail-open)
# ---------------------------------------------------------------------------

def test_resolver_unknown_id_returns_raw_en(monkeypatch):
    monkeypatch.setattr(config, "locale", "en")
    unknown = "some-future-skill-not-yet-mapped"
    assert skill_display_name(unknown) == unknown


def test_resolver_unknown_id_returns_raw_ko(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")
    unknown = "some-future-skill-not-yet-mapped"
    assert skill_display_name(unknown) == unknown


def test_resolver_never_raises_for_unknown(monkeypatch):
    for locale in ("en", "ko", "fr", "xx"):
        monkeypatch.setattr(config, "locale", locale)
        # Must not raise, must return a string
        result = skill_display_name("nonexistent-skill-id")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Resolver: en fallback when locale catalog is missing
# ---------------------------------------------------------------------------

def test_resolver_unknown_locale_falls_back_to_en(monkeypatch):
    """An unrecognized locale string should gracefully fall back to en values."""
    monkeypatch.setattr(config, "locale", "fr")
    # en has the label; fr does not exist in DISPLAY_NAMES_FOR -> falls to en -> label
    label = skill_display_name("diet-log")
    assert label == en.SKILL_DISPLAY_NAMES["diet-log"]


# ---------------------------------------------------------------------------
# /skills formatter integration: display label format
# ---------------------------------------------------------------------------

def test_skills_format_uses_display_label(monkeypatch):
    """Simulate _fmt logic: verify it produces 'Label (/id)' for known IDs."""
    monkeypatch.setattr(config, "locale", "ko")

    def _fmt_sim(entries):
        rows = []
        for name, desc in entries:
            label = skill_display_name(name)
            prefix = f"{label} (/{name})" if label != name else f"/{name}"
            desc = (desc or "").strip()
            if len(desc) > 120:
                desc = desc[:117] + "..."
            rows.append(prefix + (f" - {desc}" if desc else ""))
        return rows

    entries = [("diet-log", "Track meals"), ("some-unknown-skill", "Custom skill")]
    result = _fmt_sim(entries)
    assert result[0] == "식단 기록 (/diet-log) - Track meals"
    # Unknown skill: label == id so falls back to /id format
    assert result[1] == "/some-unknown-skill - Custom skill"
