#!/usr/bin/env python3
"""Owner turns from an instance's bridge-session transcripts (v2.7 AC5).

Source: ~/.claude/projects/<slug>/*.jsonl, where <slug> is the instance root
with every non-alphanumeric character replaced by '-' (the same encoding
routines/session-recap.py uses). Read-only.

An "owner turn" is a plain external user text entry (why_owner_turn
entry_text) the owner typed. The definitions are why_owner_turn's, the
resolver's own (DGN-1967 step 3: no second copy here):
  - entry_text drops tool results, isMeta (hook feedback), compact
    summaries, sidechains and non-external entries
  - is_injected drops machine-opened turns: [bridge:injected-turn]
    (cron-inject, dispatch-return, session-inbox ...), <task-notification>,
    recap / recall blocks, interrupt / hook / command markers
  - owner_words keeps the owner's part of the bridge's wrappers: the
    outbound-context block is cut, the timeout auto-resume prompt is dropped
    whole, the photo / album / document prompt is reduced to the owner's
    caption (no caption -> dropped)
  - sessions that are not owner conversations: entrypoint sdk-cli (cron
    `claude -p` jobs), and one-shot sessions (a single prompt and no bridge
    marker anywhere = a detached worker / scheduled job, not the owner)
Duplicates (a resumed session re-carrying history) collapse on message uuid
and on (timestamp, text).

English/ASCII only. Stdlib only.
"""

import datetime
import glob
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # loaded by path: its sibling module
import why_owner_turn  # noqa: E402

INJECTED_MARK = why_owner_turn.BRIDGE_INJECTED_TURN_MARK
CLI_ENTRYPOINTS = ("sdk-cli",)


def project_dir(instance_root, home=None):
    enc = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(instance_root))
    return os.path.join(home or os.path.expanduser("~"), ".claude", "projects", enc)


def clean(text):
    """Owner-authored part of a user message, or None."""
    return why_owner_turn.owner_words(text)


def _parse_ts(raw):
    from why_net import store
    return store.parse_dt(raw)


def _session_turns(path):
    """-> ([(utc_ts, uuid, text, prev_agent)], saw_bridge_marker) for one
    transcript. prev_agent = text of the last agent message before the turn
    ("" if none); the caller decides whether it is used."""
    turns, marker = [], False
    prev = ""
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    with f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            if o.get("type") == "assistant" and not o.get("isSidechain"):
                t = "\n".join(why_owner_turn.assistant_texts(o)).strip()
                if t:
                    prev = t
                continue
            if o.get("entrypoint") in CLI_ENTRYPOINTS:
                continue
            raw = (why_owner_turn.entry_text(o) or "").strip()
            if raw.startswith(INJECTED_MARK):
                marker = True
            if not raw or why_owner_turn.is_injected(raw):
                continue
            ts = _parse_ts(o.get("timestamp"))
            if ts is None:
                continue
            text = clean(raw)
            if text is None:
                continue
            turns.append((ts, o.get("uuid"), text, prev))
    return turns, marker


def iter_owner_turns(proj_dir, since_utc=None, with_prev=False):
    """Yield (utc_datetime, text) owner turns, oldest first.
    with_prev=True yields (utc_datetime, text, prev_agent_text) instead; the
    previous agent text is raw (the caller must clean / mask it).

    since_utc: skip turns (and whole files last modified) before this aware
    datetime -- transcripts only grow, so an older mtime holds no newer turn.
    """
    seen = set()
    out = []
    for path in glob.glob(os.path.join(proj_dir, "*.jsonl")):
        if since_utc is not None:
            try:
                mtime = datetime.datetime.fromtimestamp(
                    os.path.getmtime(path), datetime.timezone.utc)
            except OSError:
                continue
            if mtime < since_utc:
                continue
        turns, marker = _session_turns(path)
        # one-shot test runs on the WHOLE session, before the time window
        if len(turns) <= 1 and not marker:
            continue
        for ts, uid, text, prev in turns:
            if since_utc is not None and ts < since_utc:
                continue
            key = (ts.isoformat(), text)
            if (uid and uid in seen) or key in seen:
                continue
            if uid:
                seen.add(uid)
            seen.add(key)
            out.append((ts, text, prev) if with_prev else (ts, text))
    out.sort(key=lambda r: r[0])
    return out
