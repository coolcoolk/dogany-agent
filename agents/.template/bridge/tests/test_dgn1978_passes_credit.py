"""DGN-1978: /passes shows the month's API credit left and says whether the
figure was read from the console or is an estimate. Pure render; the pool's
summary is a fixture dict (no subprocess, no network)."""

import bridge.tests.conftest  # noqa: F401
from bridge import passes
from bridge.config import config
from bridge.i18n import en, ko

INV = {"claude": {"valid": True, "passes": []}, "codex": {"valid": True, "passes": []}}


def _credit(**kw):
    doc = {"budget": 200.0, "remaining": 95.83, "truth": "console", "truth_age": 300,
           "inflight_n": 0, "inflight_usd": 0.0, "exhausted": False}
    doc.update(kw)
    return doc


def test_keys_in_both_locales():
    keys = {k for k in en.STRINGS if k.startswith("passes_credit_")}
    assert len(keys) == 7
    assert keys == {k for k in ko.STRINGS if k.startswith("passes_credit_")}


def test_console_figure_and_remaining(monkeypatch):
    monkeypatch.setattr(config, "locale", "en")
    text, _ = passes.render_list(dict(INV, api_credit=_credit()))
    assert "API credit\n$95.83 of $200.00 left this month\nRead from the console 5m ago" in text


def test_stale_estimated_inflight_exhausted(monkeypatch):
    monkeypatch.setattr(config, "locale", "en")
    assert "Console figure from 2h ago" in "\n".join(
        passes.credit_lines(_credit(truth="console-stale", truth_age=7300)))
    lines = passes.credit_lines(_credit(truth="estimated", truth_age=None,
                                        inflight_n=2, inflight_usd=12.5))
    assert "Estimated -- not read from the console" in lines
    assert "2 running now (~$12.50 expected)" in lines
    assert "Used up until the 1st" in passes.credit_lines(_credit(exhausted=True))


def test_absent_when_pool_off(monkeypatch):
    monkeypatch.setattr(config, "locale", "en")
    text, _ = passes.render_list(dict(INV, api_credit=None))
    assert "API credit" not in text
    assert passes.credit_lines(None) == []
