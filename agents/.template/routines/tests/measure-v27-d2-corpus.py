#!/usr/bin/env python3
# test-lane: manual    # measurement harness, not a test -- asserts nothing
"""v2.7 D2 -- replay the text-seat rows and the G03 vocabulary variants over
a transcript corpus.

Usage:
  python3 routines/tests/measure-v27-d2-corpus.py [--dump FILE] [--post-block] <transcript.jsonl>...

Scores the PRE-block reply of a turn another Stop gate re-prompted (an
isMeta "Stop hook feedback" entry): that is the reply the seat sees first
and the one its per-turn latch decides on (grill F2).  --post-block scores
the rewritten reply instead (the pre-rework view, for comparison).

Prints, per row: fires over all turns and over user-typed turns, and the
turns where the row fired together with the G03 verdict (arbitration
overlap).  For G03 it prints the verdict count under three vocabularies:
  current   -- output_gate.py as shipped (instance words in the regexes)
  generic   -- framework words only ("the user" words; no instance words)
  generic+i -- generic plus the instance words supplied as config
               (env D2_OWNER_TERMS / D2_DELEGATE_TERMS / D2_DISPATCH_NOUNS,
               comma-separated)
and the turns where current and generic+i disagree (the re-tune must be
zero-diff before it replaces the shipped regexes).

G03 is replayed with armed_earlier=False (no cross-turn carrier lookback),
so its counts are an UPPER bound of live blocks.

READ-ONLY on the corpus; --dump writes one JSON line per fire to FILE.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROUTINES, "lib"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _split(v):
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def main(argv):
    dump = None
    if "--dump" in argv:
        i = argv.index("--dump")
        dump = open(argv[i + 1], "w", encoding="utf-8")
        argv = argv[:i] + argv[i + 2:]
    pre_block = "--post-block" not in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        sys.stderr.write(__doc__)
        return 2
    import text_seat as T
    import text_seat_g03 as G
    seat = _load("text_seat_stop", os.path.join(ROUTINES, "text-seat-stop.py"))
    ogs = _load("output_gate_stop", os.path.join(ROUTINES, "output-gate-stop.py"))
    og_cur = _load("og_cur", os.path.join(ROUTINES, "lib", "output_gate.py"))
    og_gen = G.apply_vocab(
        _load("og_gen", os.path.join(ROUTINES, "lib", "output_gate.py")),
        G.Vocab())
    og_gi = G.apply_vocab(
        _load("og_gi", os.path.join(ROUTINES, "lib", "output_gate.py")),
        G.Vocab(_split(os.environ.get("D2_OWNER_TERMS")),
                _split(os.environ.get("D2_DELEGATE_TERMS")),
                _split(os.environ.get("D2_DISPATCH_NOUNS"))))
    cfg = T.Config()
    rows = {r: {"all": 0, "typed": 0, "with_g03": 0} for r in T.ROW_ORDER}
    g03 = {"current": 0, "generic": 0, "generic+i": 0}
    diffs = []
    turns = typed = 0
    seen = set()
    for p in paths:
        try:
            got = list(seat.iter_turns(p, pre_block=pre_block))
        except Exception:
            continue
        for turn, mid, ts, _uuid in got:
            key = mid or (p, ts)
            if key in seen:
                continue
            seen.add(key)
            turns += 1
            typed += 0 if turn.injected else 1
            blob = G.tool_blob(turn.tools, ogs._strip_heredocs)
            v = {}
            for name, mod in (("current", og_cur), ("generic", og_gen),
                              ("generic+i", og_gi)):
                v[name] = mod.idle_promise_block_verdict(turn.reply, blob, False)
                if v[name]:
                    g03[name] += 1
            if bool(v["current"]) != bool(v["generic+i"]):
                diffs.append((os.path.basename(p), ts))
            for f in T.classify_all(turn, cfg):
                r = rows[f.row]
                r["all"] += 1
                r["typed"] += 0 if turn.injected else 1
                r["with_g03"] += 1 if v["current"] else 0
                if dump:
                    dump.write(json.dumps({
                        "row": f.row, "file": os.path.basename(p), "ts": ts,
                        "mid": f.mid or mid,
                        "injected": turn.injected, "excerpt": f.excerpt,
                        "prompt": turn.prompt[:300], "reply": turn.reply},
                        ensure_ascii=False) + "\n")
    print("turns=%d typed=%d (%s reply)" % (
        turns, typed, "pre-block" if pre_block else "post-block"))
    print("%-6s %6s %6s %9s" % ("row", "all", "typed", "with_G03"))
    for name in T.ROW_ORDER:
        r = rows[name]
        print("%-6s %6d %6d %9d" % (name, r["all"], r["typed"], r["with_g03"]))
    print("G03 verdicts (armed_earlier=False): " + json.dumps(g03))
    print("G03 current vs generic+i disagreements: %d" % len(diffs))
    for d in diffs[:20]:
        print("  %s %s" % d)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
