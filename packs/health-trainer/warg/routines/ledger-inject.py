#!/usr/bin/env python3
"""ledger-inject.py -- UserPromptSubmit hook (v3 section 4, DGN-071
pattern: deterministic, zero LLM, pure reads).

Emits 1-2 context lines to stdout:
  line 1: active three-layer summary + key constraints + supplement
          stack + volatile freshness
  line 2 (when present): unconsumed handoff inbox summary (proposal-path
          session-split mitigation, v3 section 3)

proposed rows never ride the inject line (v3 section 6). Any failure
exits 0 silently -- the hook must never block a user turn.
"""

import json
import os
import sqlite3
import sys

WARG_ROOT = os.environ.get(
    "WARG_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(WARG_ROOT, "database", "lifekit.db")
INBOX = os.path.join(WARG_ROOT, "files", "handoff", "inbox")


def main():
    try:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='ledger_goal'").fetchone():
            return
        layers = []
        for layer in ("long", "mid", "short", "event_overlay"):
            row = conn.execute(
                "SELECT title FROM ledger_goal WHERE layer=? AND "
                "status='active'", (layer,)).fetchone()
            if row:
                layers.append("%s=%s" % (layer, row["title"]))
        cons = conn.execute(
            "SELECT key, value FROM ledger_constraint WHERE status='active' "
            "AND class IN ('nutrition','safety') ORDER BY inviolable DESC "
            "LIMIT 3").fetchall()
        supps = [r[0] for r in conn.execute(
            "SELECT name FROM ledger_resource WHERE kind='supplement' AND "
            "status='active'").fetchall()]
        stale = conn.execute(
            "SELECT COUNT(*) FROM ledger_resource WHERE volatile=1 AND "
            "status='stale'").fetchone()[0]
        parts = []
        if layers:
            parts.append("ledger: " + ", ".join(layers))
        if cons:
            parts.append("constraints: " + ", ".join(
                "%s=%s" % (r["key"], r["value"]) for r in cons))
        if supps:
            parts.append("supplements: " + ", ".join(supps))
        if stale:
            parts.append("stale pantry items: %d" % stale)
        if parts:
            print("[ledger] " + " | ".join(parts))
        if os.path.isdir(INBOX):
            pend = [n for n in os.listdir(INBOX) if n.endswith(".md")]
            if pend:
                print("[handoff] unconsumed inbox: %d message(s): %s"
                      % (len(pend), ", ".join(sorted(pend)[:3])))
    except Exception:
        pass   # hook must never block the turn


if __name__ == "__main__":
    main()
    sys.exit(0)
