#!/usr/bin/env python3
"""Stop check for the per-turn problem frame (DGN-2028 v2.7 merged design s4).

Runs only when TURN_FRAME is on, in the owner's own session
(lib/owner_session.py), and only for a turn whose frame the hook recorded
(lib/turn_frame.current_frame: this turn's prompt hash, recorded after the
previous turn ended). A missing frame is never a violation.

Checks (lib/turn_frame.stop_findings):
  S1  current high-stakes frame without a recorded disposition
  S2  an ask recorded on the frame that the final reply does not carry
      (why_net.ups.ask_carried, the existing carry rule; no frame records
      an ask before AMEND-2, so S2 has nothing to check yet)
  S3  PROBLEM_LINE display on (explicit setting -> installed kit contract
      default -> off) for a problem-solving reply without an approved
      problem-line prefix (no prefix is approved yet: recorded as skipped)
  S4  present-case causal assertion without relevant successful evidence
      in this turn; detect-only in 2.7 whatever the mode

Mode TURN_FRAME_STOP_MODE (env DOGANY_TURN_FRAME_STOP_MODE, then
config/agent.conf): off | detect (default; any other value reads as
detect). This step never blocks and prints nothing to stdout.

Output: one row per run in <bot data dir>/logs/turn-frame.log (frame id,
check codes and counts; never reply text), and one appended `stop` row on
the frame's own JSONL file. Fail-open: any error exits 0.

  turn-frame.py            Stop hook (stdin = Claude Code Stop payload)
  turn-frame.py disposition --frame-id tf-.. --value proceed|deferred
                           [--session S]
      DGN-2028 AMEND-2 2.10 (F9): the TURN_FRAME-owned disposition writer
      (the S1 path while WHY_NET is off).  Validates that the id is an
      issued frame of the session (default: the env's session) and appends
      one `disposition` row to the turn-frame store only.  Exit 0 written,
      2 usage, 3 unknown frame, 4 TURN_FRAME off.
"""
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))

TAIL_BYTES = 4 * 1024 * 1024
MODES = ("off", "detect")


def _epoch(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _entries(path):
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        raw = fh.read()
    lines = raw.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]
    out = []
    for ln in lines:
        try:
            e = json.loads(ln.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _prompt_text(e):
    if e.get("type") != "user" or e.get("isSidechain") or e.get("isMeta"):
        return None
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return c if c.strip() else None
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        t = "\n".join(b.get("text") or "" for b in c
                      if isinstance(b, dict) and b.get("type") == "text")
        return t if t.strip() else None
    return None


def current_turn(entries):
    """(prompt, previous-turn end epoch, final reply, tools) for the last
    turn; tools = [{"name", "command", "ok"}] with ok = a non-error result."""
    start = None
    for i in range(len(entries) - 1, -1, -1):
        if _prompt_text(entries[i]) is not None:
            start = i
            break
    if start is None:
        return None
    prompt = _prompt_text(entries[start])
    prev_end = None
    for e in reversed(entries[:start]):
        prev_end = _epoch(e.get("timestamp"))
        if prev_end is not None:
            break
    uses, results, msgs = {}, {}, []
    for e in entries[start + 1:]:
        if e.get("isSidechain"):
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if e.get("type") == "assistant" and b.get("type") == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                uses[b.get("id")] = {"name": b.get("name"),
                                     "command": inp.get("command") or ""}
            elif e.get("type") == "assistant" and b.get("type") == "text":
                mid = msg.get("id")
                if msgs and msgs[-1][0] == mid:
                    msgs[-1][1].append(b.get("text") or "")
                else:
                    msgs.append([mid, [b.get("text") or ""]])
            elif e.get("type") == "user" and b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = not b.get("is_error")
    tools = [dict(u, ok=results.get(k, False)) for k, u in uses.items()]
    reply = "\n".join(msgs[-1][1]) if msgs else ""
    return prompt, prev_end, reply, tools


def _display_on(tf, root, conf):
    """PROBLEM_LINE: explicit setting -> installed kit contract default -> off."""
    v = tf.flag("PROBLEM_LINE", conf)
    if v in ("on", "off"):
        return v == "on"
    try:
        import base_contract
        for _pid, c in base_contract.load_installed(root):
            if (c.get("defaults") or {}).get("problem_line") == "on":
                return True
    except Exception:
        pass
    return False


def _log(tf, root, fields):
    try:
        d = os.path.join(tf.bot_data_dir(root), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "turn-frame.log"), "a", encoding="utf-8") as fh:
            fh.write("%s  TURN_FRAME_STOP  %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                " ".join("%s=%s" % kv for kv in fields)))
    except Exception:
        pass


def decide(data, root=ROOT):
    """Run the checks; return the findings list (None = not applicable).
    Never blocks in this build step."""
    if data.get("hook_event_name") not in (None, "Stop"):
        return None
    import turn_frame as tf
    conf = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(root, "config", "agent.conf")
    if not tf.frame_enabled(conf):
        return None
    mode = tf.flag("TURN_FRAME_STOP_MODE", conf, "detect")
    if mode == "off":
        return None
    if os.environ.get("DOGANY_DISPATCH_WORKER"):
        return None
    import owner_session
    if owner_session.hook_status(data, root) != "owner":
        return None
    tp = data.get("transcript_path") or ""
    turn = current_turn(_entries(tp))
    if turn is None:
        return None
    prompt, prev_end, reply, tools = turn
    path = tf.store_path(root, data.get("session_id") or owner_session.session_id(tp))
    frame = tf.current_frame(tf.read_rows(path), prompt, prev_end)
    if frame is None:
        _log(tf, root, [("frame", "-"), ("result", "no-frame")])
        return None
    carried = None
    if frame.get("asks"):
        carried = _why_carried()
    findings = tf.stop_findings(frame, reply, tools, prompt,
                                _display_on(tf, root, conf), carried=carried)
    codes = ",".join(f["check"] for f in findings) or "none"
    _log(tf, root, [("frame", frame["frame_id"]), ("mode", "detect"),
                    ("findings", codes), ("tools", len(tools)),
                    ("ok_tools", sum(1 for t in tools if t["ok"]))])
    try:
        tf.append_row(path, {"v": tf.FRAME_VERSION, "ev": "stop",
                             "frame_id": frame["frame_id"], "ts": time.time(),
                             "mode": "detect", "findings": findings})
    except Exception:
        pass
    return findings


def _why_carried():
    """S2's carry rule over the why-net ask a frame row names: the frame
    store holds the ask id only, the ask (labels, printed text) is read
    from the Why net (why_net.ups.ask_carried, the existing rule)."""
    from why_net import budget, store, ups
    asks = budget.fold_asks(store.read_rows(store.ASKS))

    def carried(reply, ref):
        a = asks.get((ref or {}).get("ask"))
        return a is not None and ups.ask_carried(reply, a)
    return carried


DISPOSITIONS = ("proceed", "deferred")


def disposition(argv, root=ROOT, out=None):
    """`turn-frame.py disposition --frame-id tf-.. --value proceed|deferred
    [--session S]`."""
    import argparse
    out = out or sys.stdout
    p = argparse.ArgumentParser(prog="turn-frame.py disposition")
    p.add_argument("--frame-id", required=True)
    p.add_argument("--value", required=True, choices=DISPOSITIONS)
    p.add_argument("--session")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 2
    import machine_signal as ms
    import turn_frame as tf
    conf = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(root, "config", "agent.conf")
    if not tf.frame_enabled(conf):
        out.write(ms.signal("TURN_FRAME_REFUSED", reason="off") + "\n")
        return 4
    sid = a.session or os.environ.get("CLAUDE_CODE_SESSION_ID") \
        or os.environ.get("CLAUDE_SESSION_ID")
    if not sid:
        return 2
    path = tf.store_path(root, sid)
    if not any(r.get("ev") == "issued" and r.get("frame_id") == a.frame_id
               for r in tf.read_rows(path)):
        out.write(ms.signal("TURN_FRAME_REFUSED", reason="unknown-frame") + "\n")
        return 3
    tf.append_row(path, {"v": tf.FRAME_VERSION, "ev": "disposition",
                         "frame_id": a.frame_id, "value": a.value, "ts": time.time()})
    out.write(ms.signal("TURN_FRAME_OK", disposition=a.value, frame=a.frame_id) + "\n")
    return 0


def main():
    if sys.argv[1:2] == ["disposition"]:
        try:
            return disposition(sys.argv[2:])
        except Exception:
            return 1
    try:
        data = json.load(sys.stdin)
        if isinstance(data, dict):
            decide(data)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
