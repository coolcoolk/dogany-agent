#!/usr/bin/env python3
# test-lane: fast
"""DGN-2060: the /usage service card (lib/usage_card.py), exact output.

Owner-confirmed 2026-10-13 17:05. Offline, clock pinned (now = 2026-10-13
16:29 UTC, TZ=UTC). Fixtures: (a) no passes, (b) passes on both services,
(c) Claude-only user (no Codex card at all).
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["TZ"] = "UTC"
# Isolate from the host pass store ($HOME/.dogany/reset-passes, DGN-1983).
os.environ["HOME"] = tempfile.mkdtemp(prefix="dgn2060-home.")
time.tzset()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import usage_card as uc  # noqa: E402

NOW = datetime(2026, 10, 13, 16, 29, tzinfo=timezone.utc)
RULE = "─" * 27
CLAUDE_WINS = [("5시간", 6.0, "2026-10-13T20:40:00Z"),
               ("주간", 73.0, "2026-10-15T11:00:00Z"),
               ("Fable", 0, "2026-10-15T11:00:00Z")]
CODEX_RESET = datetime(2026, 10, 18, 21, 40, tzinfo=timezone.utc).timestamp()
CODEX_WINS = [("주간", 87, CODEX_RESET)]


def P(*days):
    return [{"label": "2026-%s" % d, "expired": False} for d in days]


def render(kind, wins, passes, loc="ko"):
    T = uc.text(loc)
    return "\n".join(uc.card(T[kind], wins, passes, T, now=NOW))


KO_CLAUDE = """Claude 사용 한도
───────────────────────────
5시간
[█░░░░░░░░░░░░░░░░░░░] 6%
리셋 10-13 20:40 (-4h 11m)
주간
[███████████████░░░░░] 73%
리셋 10-15 11:00 (-1d 18h 31m)
Fable
[░░░░░░░░░░░░░░░░░░░░] 0%
리셋 10-15 11:00 (-1d 18h 31m)
@@───────────────────────────"""
KO_CODEX = """Codex 사용 한도
───────────────────────────
주간
[█████████████████░░░] 87%
리셋 10-18 21:40 (-5d 5h 11m)
@@───────────────────────────"""


class Card(unittest.TestCase):
    def test_ultra_omits_absent_five_hour(self):
        from usage_card import claude_windows, text
        data = json.loads((Path(__file__).parent / "fixtures/usage-ultra.json").read_text())
        self.assertEqual([row[:2] for row in claude_windows(data, text("en"))], [("weekly", 51)])

    def test_max5_keeps_five_hour(self):
        from usage_card import claude_windows, text
        data = json.loads((Path(__file__).parent / "fixtures/usage-max-5x.json").read_text())
        self.assertEqual(claude_windows(data, text("en"))[0][:2], ("5h", 90))

    def test_missing_five_hour_is_unknown(self):
        wins = uc.claude_windows({"seven_day": {"utilization": 51}}, uc.text("en"))
        out = render("claude", wins, [], "en")
        self.assertIn("5h\n[" + "?" * 20 + "] ?%", out)

    def test_expired_five_hour_stays_visible_as_unknown(self):
        data = {"five_hour": {"utilization": 10, "resets_at": NOW.timestamp() - 180},
                "seven_day": {"utilization": 51}}
        out = render("claude", uc.claude_windows(data, uc.text("en")), [], "en")
        self.assertIn("5h\n[" + "?" * 20 + "] ?%", out)
        self.assertIn("(+3m)", out)
        self.assertNotIn("] 10%", out)

    def test_b_passes_on_both_exact(self):
        claude = render("claude", CLAUDE_WINS, P("10-23 05:50"))
        codex = render("codex", CODEX_WINS, P("10-23", "10-30", "11-07"))
        self.assertEqual(claude, KO_CLAUDE.replace("@@", "▸ 리셋권 1장\n· 10-23 만료\n"))
        self.assertEqual(codex, KO_CODEX.replace("@@", "▸ 리셋권 3장\n· 10-23 만료\n· 10-30 만료\n· 11-07 만료\n"))
        full = claude + "\n" + codex
        self.assertNotIn("left", full)          # old summary line is gone
        self.assertNotIn("/passes", full)       # hint is gone
        self.assertNotIn("used", full)

    def test_a_no_passes_no_block(self):
        for passes in ([], None, [{"label": "2026-10-01", "expired": True}]):
            self.assertEqual(render("claude", CLAUDE_WINS, passes), KO_CLAUDE.replace("@@", ""))
            self.assertEqual(render("codex", CODEX_WINS, passes), KO_CODEX.replace("@@", ""))

    def test_c_claude_only_user(self):
        # not onboarded -> the lib hands back no rows; the Codex card is never built
        self.assertEqual(uc.passes_for("codex", "/nonexistent/agent.conf"), [])
        out = render("claude", CLAUDE_WINS, P("10-23"))
        self.assertNotIn("Codex", out)

    def test_en_mirror_and_unknown_expiry(self):
        out = render("claude", [("5h", 6, "2026-10-13T20:40:00Z")],
                     P("10-23") + [{"label": "unknown", "expired": False}], "en")
        self.assertEqual(out.splitlines(), [
            "Claude Usage Limits", RULE, "5h",
            "[█░░░░░░░░░░░░░░░░░░░] 6%", "reset 10-13 20:40 (-4h 11m)",
            "▸ Reset passes: 2", "· expires 10-23", "· expiry unknown", RULE])

    def test_bar_is_20_cells_percent_is_used(self):
        for pct, filled in ((0, 0), (6, 1), (50, 10), (100, 20), (250, 20)):
            b = uc.bar(pct)
            self.assertEqual((len(b), b.count("█")), (22, filled))
        self.assertEqual(uc.bar(None), "[" + "?" * 20 + "]")


class ResetBoundary(unittest.TestCase):
    def test_fetch_missing_five_hour_never_invents_zero(self):
        path = Path(__file__).resolve().parents[1] / "usage-gate.py"
        spec = importlib.util.spec_from_file_location("missing_window_gate", path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        data = {"seven_day": {"utilization": 51, "resets_at": "2099-01-01T00:00:00Z"}}
        result = type("Result", (), {"returncode": 0, "stdout": json.dumps(data)})()
        with patch.object(gate.subprocess, "run", return_value=result):
            with self.assertRaises((KeyError, RuntimeError, ValueError)):
                gate.fetch_usage()

    def test_card_loads_without_sibling_import_path(self):
        # The bridge loads this file directly with spec_from_file_location.
        lib = Path(__file__).resolve().parents[1] / "lib"
        without_lib = [p for p in sys.path if Path(p).resolve() != lib]
        spec = importlib.util.spec_from_file_location("standalone_card", lib / "usage_card.py")
        card = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", without_lib), patch.dict(sys.modules):
            sys.modules.pop("usage_summary", None)
            spec.loader.exec_module(card)
            lines = card.card("Codex", [("weekly", 87, NOW.timestamp())], [],
                              card.text("en"), now=NOW)
        self.assertIn("[" + "?" * 20 + "] ?%", lines)

    def test_passed_windows_hide_percent_in_both_services(self):
        past = NOW.timestamp() - 180
        for kind in ("claude", "codex"):
            for reset in (past, past * 1000,
                          datetime.fromtimestamp(past, timezone.utc).isoformat()):
                with self.subTest(kind=kind, reset=reset):
                    out = render(kind, [("window", 27, reset)], [])
                    self.assertIn("[" + "?" * 20 + "] ?%", out)
                    self.assertIn("(+3m)", out)
                    self.assertNotIn("27%", out)
        # Equality is expired; a future reset still shows measured usage.
        self.assertNotIn("27%", render("codex", [("window", 27, NOW.timestamp())], []))
        self.assertIn("27%", render("codex", [("window", 27, NOW.timestamp() + 1)], []))

    def test_any_snapshot_window_expires_ttl_at_boundary(self):
        from usage_summary import snapshot_expired
        for reset in (NOW.timestamp(), NOW.timestamp() * 1000, NOW.isoformat()):
            for payload in ({"five_hour": {"resets_at": reset}},
                            {"seven_day": {"resets_at": reset}},
                            {"limits": [{"resets_at": reset}]}):
                self.assertTrue(snapshot_expired(payload, NOW.timestamp()))
        for reset in (None, "", "unknown", NOW.timestamp() + 1):
            self.assertFalse(snapshot_expired({"five_hour": {"resets_at": reset}},
                                              NOW.timestamp()))

    def test_local_gate_cache_refreshes_and_rejects_expired_stale(self):
        path = Path(__file__).resolve().parents[1] / "usage-gate.py"
        spec = importlib.util.spec_from_file_location("boundary_gate", path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        future = "2099-01-01T00:00:00Z"
        fresh = {"five_hour_util": 1, "seven_day_util": 2,
                 "five_hour_resets_at": future, "seven_day_resets_at": future}
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "STATE_DIR", tmp), \
                patch.object(gate, "book_check"):
            cache = Path(tmp) / gate.CACHE_NAME
            for key in ("five_hour_resets_at", "seven_day_resets_at", "scoped_0_resets_at"):
                cache.write_text(json.dumps({**fresh, key: "2000-01-01T00:00:00Z"}))
                with patch.object(gate, "fetch_usage", return_value=fresh) as fetch:
                    self.assertEqual(gate.get_usage(), fresh)
                    fetch.assert_called_once()
                cache.write_text(json.dumps({**fresh, key: "2000-01-01T00:00:00Z"}))
                with patch.object(gate, "fetch_usage", side_effect=RuntimeError("offline")):
                    self.assertIsNone(gate.get_usage())
            cache.write_text(json.dumps(fresh))
            with patch.object(gate, "fetch_usage") as fetch:
                self.assertEqual(gate.get_usage(), fresh)
                fetch.assert_not_called()
            stale = {"five_hour": {"utilization": 27, "resets_at": "2000-01-01T00:00:00Z"},
                     "seven_day": {"utilization": 20, "resets_at": future}}
            with patch.object(gate.subprocess, "run", return_value=type("Result", (), {
                    "returncode": 0, "stdout": json.dumps(stale)})()):
                with self.assertRaises(RuntimeError):
                    gate.fetch_usage()


if __name__ == "__main__":
    unittest.main()
