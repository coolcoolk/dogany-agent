#!/usr/bin/env python3
# test-lane: manual    # measurement harness, not a test -- asserts nothing
"""DGN-2032 rev2 -- replay owner_ask_verdict over a transcript corpus.

Usage:
  python3 routines/tests/measure-dgn2032-owner-ask.py [--since YYYY-MM-DD]
      [--dump FILE] <transcript.jsonl>...

One verdict per turn (a genuine user prompt opens a turn; the turn's last
assistant text is the reply the Stop gate sees).  The turn's tool blob is
built with output-gate-stop._tool_blob_lines, the same lines the live
adapter reads; fetch availability is the adapter's rule applied to the
session PREFIX up to that turn (offered in a deferred-tool list, or used).

Prints one line per turn whose reply makes an owner ask, then a count per
state.  READ-ONLY on the corpus; --dump writes one JSON line per ask turn
(reply text included) for hand judgment.
"""
import importlib.util
import json
import os
import sys
from collections import Counter

ROUTINES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROUTINES, "lib"))
import output_gate as og  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OGS = _load("output_gate_stop", os.path.join(ROUTINES, "output-gate-stop.py"))


def turns(path):
    """Yield (ts, prompt, reply, blob, avail) per turn."""
    avail = None
    cur = None
    with open(path, errors="replace") as fh:
        rows = []
        for raw in fh:
            try:
                rows.append(json.loads(raw))
            except Exception:
                continue

    def close(t):
        return (t["ts"], t["prompt"], t["reply"], "\n".join(t["blob"]),
                t["avail"])

    for e in rows:
        att = e.get("attachment")
        if isinstance(att, dict) and att.get("type") == "deferred_tools_delta":
            if any(og.MEANS_FETCH_TOOL_NAME_RE.match(n or "")
                   for n in att.get("addedNames") or []):
                avail = True
        et = e.get("type")
        if et not in ("user", "assistant"):
            continue
        msg = e.get("message") or {}
        inj = bool(e.get("isMeta") or e.get("isSidechain"))
        if OGS._is_genuine_user_boundary(et, inj, msg):
            if cur and cur["reply"]:
                yield close(cur)
            c = msg.get("content")
            prompt = c if isinstance(c, str) else " ".join(
                b.get("text", "") for b in c if isinstance(b, dict)
                and b.get("type") == "text")
            cur = {"ts": e.get("timestamp") or "", "prompt": prompt,
                   "reply": "", "blob": [], "avail": avail, "mid": None}
            continue
        if cur is None or et != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        texts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                cur["blob"].extend(OGS._tool_blob_lines(b))
                if og.MEANS_FETCH_TOOL_NAME_RE.match(b.get("name") or ""):
                    avail = cur["avail"] = True
            elif b.get("type") == "text" and (b.get("text") or "").strip():
                texts.append(b["text"])
        if texts:
            mid = msg.get("id")
            if mid and mid == cur["mid"]:
                cur["reply"] += "\n" + "\n".join(texts)
            else:
                cur["reply"] = "\n".join(texts)
            cur["mid"] = mid
    if cur and cur["reply"]:
        yield close(cur)


def main(argv):
    since, dump, paths = "", None, []
    it = iter(argv)
    for a in it:
        if a == "--since":
            since = next(it)
        elif a == "--dump":
            dump = open(next(it), "w")
        else:
            paths.append(a)
    states = Counter()
    n = 0
    for p in paths:
        for ts, prompt, reply, blob, avail in turns(p):
            if since and ts[:10] < since:
                continue
            n += 1
            v = og.owner_ask_verdict(reply, blob, avail)
            if v is None:
                continue
            states[v["state"]] += 1
            print("%s  %s  state=%s block=%d asks=%d search=%d fetch=%d "
                  "avail=%s  [%s]" % (ts[:19], os.path.basename(p)[:8],
                                      v["state"], v["block"], v["asks"],
                                      v["search"], v["fetch"], avail,
                                      v["excerpt"]))
            if dump:
                dump.write(json.dumps({"ts": ts, "file": p, "verdict": v,
                                       "prompt": prompt[:400],
                                       "reply": reply}, ensure_ascii=False)
                           + "\n")
    print("turns=%d asks=%d %s" % (n, sum(states.values()), dict(states)),
          file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
