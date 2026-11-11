#!/usr/bin/env python3
"""Stop gate: an owner ask must follow real attempts (DGN-2032 rev2).

Owner ruling 2026-10-14 14:12: asking the owner for what is needed, at the
right time, is good; coming back after one shallow cycle without trying the
means the agent has is the defect.  After real attempts (fetch / browser
done, still not found), "not found + what was tried + one specific ask" is
correct and passes.

The verdict lives in routines/lib/output_gate.py (owner_ask_verdict).  This
file is plumbing: read the final reply and THIS turn's tool calls from the
transcript, decide whether fetch/browser tools were visible to the agent,
log one canary row per ask, and block once per turn when the verdict says
so.  A cited-means line in the reply never counts; only tool calls do.

Ladder, OWNER_ASK_MODE (env, then config/agent.conf):
  off     nothing runs
  record  every ask -> one OWNER_ASK canary row, no block
  block   (default) the row is written FIRST; a blocking verdict then
          writes an OWNER_ASK_BLOCK row and the Stop block (switch slug
          owner-ask, master GATE_STOP_ALL).
Kit-declared readable facts (DGN-2165) block independently of web means;
the retry includes the kit's read hint and the read-failure instruction.
For source-search asks, record-only paths: tool calls not readable (state
blind), no fetch/browser tool visible to the agent (no_tool), no web search
this turn (no_search).  Owner session only (lib/owner_session.py, the guard
the text seat uses); a re-prompt (stop_hook_active) never blocks again.
Fail-open: any error exits 0 with no stdout.

Canary (<BOT_DATA_DIR>/logs/output-gate-canary.log):
  TS  OWNER_ASK  state=<s> asks=N search=N fetch=N avail=0|1|- mode=<m>  [excerpt]
  TS  OWNER_ASK_BLOCK  state=<s>
"""
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))

SLUG = "owner-ask"
MODE_KEY = "OWNER_ASK_MODE"
MODES = ("off", "record", "block")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mode(cwd, ogs):
    val = os.environ.get(MODE_KEY, "").strip().lower()
    if not val:
        val = (ogs.conf_get(MODE_KEY, os.path.join(cwd, "config", "agent.conf"))
               or "").strip().lower()
    return val if val in MODES else "block"


def fetch_available(path, name_re, turn_tool_blob=""):
    """True when a fetch/browser tool was offered to (deferred-tool list) or
    used in this session; None when the transcript shows neither -- the
    caller then records only, it cannot claim the agent had the means."""
    for line in (turn_tool_blob or "").splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[0] == "TOOL" and name_re.match(parts[1]):
            return True
    try:
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                if ("deferred_tools_delta" not in raw
                        and '"tool_use"' not in raw):
                    continue
                try:
                    e = json.loads(raw)
                except Exception:
                    continue
                names = []
                att = e.get("attachment")
                if isinstance(att, dict) and \
                        att.get("type") == "deferred_tools_delta":
                    names = att.get("addedNames") or []
                content = (e.get("message") or {}).get("content")
                if isinstance(content, list):
                    names = names + [b.get("name") or "" for b in content
                                     if isinstance(b, dict)
                                     and b.get("type") == "tool_use"]
                if any(isinstance(n, str) and name_re.match(n)
                       for n in names):
                    return True
    except Exception:
        return None
    return None


def _row(lp, kind, fields, excerpt=""):
    try:
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        with open(lp, "a", encoding="utf-8") as fh:
            fh.write("%s  %s  %s%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), kind,
                " ".join("%s=%s" % kv for kv in fields),
                ("  [%s]" % excerpt) if excerpt else ""))
    except Exception:
        pass


def decide(data):
    """Return the stdout payload (dict) or None.  Raises on internal error
    (the caller fails open)."""
    if data.get("hook_event_name") not in (None, "Stop"):
        return None
    cwd = str(data.get("cwd") or os.getcwd())
    ogs = _load("output_gate_stop", os.path.join(HERE, "output-gate-stop.py"))
    mode = _mode(cwd, ogs)
    if mode == "off":
        return None
    import owner_session
    if owner_session.hook_status(data, ROOT) != "owner":
        return None
    tp = data.get("transcript_path") or ""
    text = ogs._last_assistant_text(tp)
    if not text:
        return None
    import output_gate as og
    from kit_readable_facts import load_readable_facts
    blob = ogs._current_turn_tool_blob(tp)
    avail = fetch_available(tp, og.MEANS_FETCH_TOOL_NAME_RE, blob or "")
    v = og.owner_ask_verdict(text, blob, avail, load_readable_facts(cwd))
    if v is None:
        return None
    lp = ogs._resolve_log_path(cwd)
    _row(lp, "OWNER_ASK", [
        ("state", v["state"]), ("asks", v["asks"]), ("search", v["search"]),
        ("fetch", v["fetch"]), ("avail", {True: 1, None: "-"}.get(avail, 0)),
        ("mode", mode)], v["excerpt"])
    if not v["block"] or mode != "block" or data.get("stop_hook_active"):
        return None
    _row(lp, "OWNER_ASK_BLOCK", [("state", v["state"])])
    try:
        gs = _load("gate_switch", os.path.join(HERE, "gate-switch.py"))
        if not gs.is_enabled(SLUG, cwd):
            return gs.off_notice(SLUG, v["reason"], cwd)
    except Exception:
        pass  # a broken switch never disarms the gate
    return {"decision": "block", "reason": v["reason"]}


def main():
    # v2.7 stable: retired procedural refusal; accept stale registrations.
    return
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return 0
        out = decide(data)
        if out:
            print(json.dumps(out, ensure_ascii=False), flush=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
