#!/usr/bin/env python3
"""Fake third-party kit provider adapter (test fixture, DGN-2028 slice 4).

Speaks provider protocol 1: argv[1] is the operation, the JSON request is on
stdin, the JSON reply on stdout. Behaviour comes from records.json beside
this file; every call is appended to calls.jsonl beside it (so a test can
prove only read operations were invoked).

records.json: {"mode": "ok" | "fail" | "garbage" | "badresult",
               "records": [{"slot", "target", "event_id"?, "match"?,
                            "result", "refs"?, "versions"?, "subject"?,
                            "evidence"?}],
               "changes": [...], "snapshots": [...]}
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    op = sys.argv[1] if len(sys.argv) > 1 else ""
    req = json.loads(sys.stdin.read() or "{}")
    with open(os.path.join(HERE, "records.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    with open(os.path.join(HERE, "calls.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"op": op, "argv": sys.argv[1:], "req": req}) + "\n")
    mode = cfg.get("mode", "ok")
    if mode == "fail":
        sys.stderr.write("kit down\n")
        return 3
    if mode == "garbage":
        sys.stdout.write("not json")
        return 0
    out = {"protocol": 1}
    if op == "lookup":
        if mode == "badresult":
            out["result"] = "maybe"
        else:
            out["result"] = "absent"
            for r in cfg.get("records", []):
                if r.get("slot") != req.get("slot") or r.get("target") != req.get("target"):
                    continue
                if "event_id" in r and r["event_id"] != req.get("event_id"):
                    continue
                if "match" in r and r["match"] not in (req.get("text") or ""):
                    continue
                for k in ("result", "refs", "versions", "subject", "evidence"):
                    if k in r:
                        out[k] = r[k]
                break
    elif op == "changes":
        ch = cfg.get("changes", [])
        start = int(req.get("cursor") or 0)
        out["changes"] = ch[start:]
        out["cursor"] = str(len(ch))
    elif op == "evidence":
        out["snapshots"] = cfg.get("snapshots", [])
    else:
        return 2
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
