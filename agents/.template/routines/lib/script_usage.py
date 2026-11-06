"""DGN-2115: usage lines of a script job (tokens / cost per model).

A script job (dispatch-detached.sh --script) is not a model run, but it may
drive models (a bench, a merge with a reviewer). It reports what it spent by
appending one JSON object per line to the file named by $DOGANY_SCRIPT_USAGE_FILE
(<rundir>/usage.jsonl). finalize_run folds that file into the run's status.json
under "script_usage"; that is the per-run record the usage accounting reads.

Line format (UTF-8 JSON object, one per line; unknown keys are ignored):

    {"model": "claude-opus-5-5",       # required, non-empty string
     "input_tokens": 1200,             # optional, integer >= 0
     "output_tokens": 340,             # optional, integer >= 0
     "cache_read_tokens": 0,           # optional, integer >= 0
     "cache_write_tokens": 0,          # optional, integer >= 0
     "cost_usd": 0.12,                 # optional, number >= 0
     "runtime": "claude-code"}         # optional, free label (account lane)

A line needs "model" and at least one numeric field. Anything else (blank, not
JSON, wrong types, negative numbers) is counted in "bad_lines" and skipped --
a malformed line never fails the job or the finalize.

CLI (for shell scripts):
    script_usage.py append --model M [--input-tokens N] [--output-tokens N]
        [--cache-read-tokens N] [--cache-write-tokens N] [--cost-usd X]
        [--runtime R] [--file F]       (F defaults to $DOGANY_SCRIPT_USAGE_FILE)
    script_usage.py summarize FILE     (prints the JSON summary)
"""
import argparse
import json
import math
import os
import sys

INT_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
NUM_FIELDS = INT_FIELDS + ("cost_usd",)


def _num(v, integer):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    if integer:
        return int(v) if float(v).is_integer() else None
    return float(v)


def parse_line(line):
    """The validated record of one line, or None."""
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    model = doc.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    rec = {"model": model.strip()}
    for k in NUM_FIELDS:
        if k in doc:
            v = _num(doc[k], k in INT_FIELDS)
            if v is None:
                return None
            rec[k] = v
    if len(rec) == 1:
        return None
    return rec


def summarize(path):
    """{"lines": n, "bad_lines": n, "models": {model: sums}, "total": sums}."""
    models, total = {}, {k: 0 for k in NUM_FIELDS}
    total["cost_usd"] = 0.0
    lines = bad = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                rec = parse_line(raw)
                if rec is None:
                    bad += 1
                    continue
                lines += 1
                row = models.setdefault(rec["model"], {k: 0 for k in NUM_FIELDS})
                for k in NUM_FIELDS:
                    row[k] += rec.get(k, 0)
                    total[k] += rec.get(k, 0)
    except OSError:
        pass
    for row in list(models.values()) + [total]:
        row["cost_usd"] = round(row["cost_usd"], 6)
    return {"lines": lines, "bad_lines": bad, "models": models, "total": total}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="script_usage.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("append")
    p.add_argument("--model", required=True)
    p.add_argument("--input-tokens", type=int)
    p.add_argument("--output-tokens", type=int)
    p.add_argument("--cache-read-tokens", type=int)
    p.add_argument("--cache-write-tokens", type=int)
    p.add_argument("--cost-usd", type=float)
    p.add_argument("--runtime", default="")
    p.add_argument("--file", default="")
    p = sub.add_parser("summarize")
    p.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "summarize":
        json.dump(summarize(a.file), sys.stdout, sort_keys=True)
        print()
        return 0
    path = a.file or os.environ.get("DOGANY_SCRIPT_USAGE_FILE", "")
    if not path:
        sys.stderr.write("script_usage: no --file and no DOGANY_SCRIPT_USAGE_FILE\n")
        return 2
    rec = {"model": a.model}
    for k in NUM_FIELDS:
        v = getattr(a, k)
        if v is not None:
            rec[k] = v
    if a.runtime:
        rec["runtime"] = a.runtime
    if parse_line(json.dumps(rec)) is None:
        sys.stderr.write("script_usage: need a model and at least one non-negative number\n")
        return 2
    # One write() of one short line: O_APPEND keeps concurrent appenders whole.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
