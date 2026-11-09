#!/usr/bin/env python3
"""recorrection-count.py -- daily owner re-correction counter (v2.7 AC5).

Counts, per local day, how many owner turns are a "direction re-correction /
re-explanation" signal (predicate: routines/lib/recorrection.py + its pattern
data), and appends one row per completed day to the instance ledger:

  <root>/.telegram_bot/state/recorrection-ledger.tsv
  date <TAB> owner_turns <TAB> hits <TAB> rate <TAB> predicate

The ledger holds COUNTS ONLY -- never owner text. It is append-only: a run
writes every completed day after the last recorded one, up to yesterday, so
the first run backfills (--backfill-days, default 30, clamped to retention)
and a missed night is filled by the next run. Today is never written (it is
not complete).

Day boundary = the instance UTC offset from routines/lib/tz_offset.sh (the one
resolver). Days older than the Claude Code transcript retention horizon
(~/.claude/settings.json cleanupPeriodDays, default 30) are not written:
their transcripts may be gone, and a 0 there would be invented.

Modes
  (default)          append missing completed days to the ledger
  --dry-run          print the rows that would be appended; write nothing
  --mode hybrid      strong pattern cues decide; uncertain turns (weak cue, or
                     no cue on a turn of 30+ chars) go to `claude -p --model
                     sonnet`, one turn per call, with up to 160 chars of the
                     previous agent message. Rubric v2: only C2 counts. A
                     reply that does not parse is "unresolved", never C2.
                     Writes a SEPARATE ledger (recorrection-hybrid-ledger.tsv:
                     counts + cue names only, never text). This is the
                     scheduled mode (dec-291, nightly). Model calls per run
                     are capped (--max-model-calls): when the cap is hit,
                     the days from that one on wait for the next night;
                     only the run's first day is written anyway, its
                     over-cap turns counted as unresolved. A run in which
                     every model call failed writes nothing (exit 1), so
                     an outage is retried instead of recorded.
                     With --dry-run it calls no model and prints, per day,
                     how many turns WOULD go to it.
  --eval FILE        score the predicate on a labelled JSONL sample
                     ({"text", "label": C|F|N|X, "split"}) -> hit rate
                     (recall), precision, false-positive rate per split.
                     C = re-correction (positive); F/N = negative;
                     X = machine text the reader must drop.
  --rubric v2        with --eval: score on the rubric-v2 `label2` field
                     (C2 positive; O/F/N negative) instead of `label`.
  --eval FILE --mode hybrid
                     score the hybrid offline on the rubric-v2 rows: strong
                     cues decide, uncertain rows go to the model (same mask
                     and prompt as the nightly run). --repeat N runs the
                     model N times and reports run-to-run agreement on the
                     called rows. The total call count (uncertain rows x N)
                     must fit --max-model-calls, else it exits 2 before any
                     call. With --dry-run it calls no model and prints the
                     routing share per split.

Read-only on transcripts. English/ASCII only. Stdlib only.
"""

import argparse
import collections
import datetime
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

import owner_turns  # noqa: E402
import recorrection  # noqa: E402

LEDGER_REL = os.path.join(".telegram_bot", "state", "recorrection-ledger.tsv")
HEADER = (
    "# recorrection-ledger -- v2.7 AC5 owner re-correction / re-explanation, "
    "one row per completed local day. Append-only. Counts only.\n"
    "# date\towner_turns\thits\trate\tpredicate\n"
)
DEFAULT_RETENTION_DAYS = 30

HYBRID_LEDGER_REL = os.path.join(".telegram_bot", "state",
                                 "recorrection-hybrid-ledger.tsv")
HYBRID_HEADER = (
    "# recorrection-hybrid-ledger -- v2.7 AC5 hybrid (patterns + model), one "
    "row per completed local day. Append-only. Counts and cue names only.\n"
    "# date\towner_turns\thits\trate\tpredicate\tstrong\tmodel_calls\t"
    "model_c2\tunresolved\tcues\n"
)
MODEL_TIMEOUT_S = 90
# Per-run model call cap. One live instance, 2026-09-08..10-07: mean 37 uncertain turns a
# day, max 90; the cap clears a normal day and spreads a 30-day backfill over
# about ten nights.
DEFAULT_MAX_MODEL_CALLS = 120
CLAUDE_BIN_ENV = "RECORRECTION_CLAUDE_BIN"
_PATH_RX = re.compile(r"(?:~|/)[\w.\-]*(?:/[\w.\-]+)+")


def utc_offset_hours(root):
    try:
        out = subprocess.run(
            ["bash", os.path.join(HERE, "lib", "tz_offset.sh"), "resolve", root],
            capture_output=True, text=True, timeout=20)
        return int(out.stdout.strip())
    except Exception:
        off = datetime.datetime.now().astimezone().utcoffset()
        return int(off.total_seconds() // 3600)


def retention_days(home):
    try:
        with open(os.path.join(home, ".claude", "settings.json")) as f:
            v = int(json.load(f).get("cleanupPeriodDays", DEFAULT_RETENTION_DAYS))
            return v if v > 0 else DEFAULT_RETENTION_DAYS
    except Exception:
        return DEFAULT_RETENTION_DAYS


def last_ledger_date(path):
    last = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                last = line.split("\t", 1)[0].strip()
    except FileNotFoundError:
        return None
    return datetime.date.fromisoformat(last) if last else None


def day_rows(proj_dir, tz, first_day, last_day):
    """[(date, owner_turns, hits)] for every day in [first_day, last_day]."""
    since = datetime.datetime.combine(first_day, datetime.time(), tz)
    counts = {}
    d = first_day
    while d <= last_day:
        counts[d] = [0, 0]
        d += datetime.timedelta(days=1)
    for ts, text in owner_turns.iter_owner_turns(
            proj_dir, since_utc=since.astimezone(datetime.timezone.utc)):
        day = ts.astimezone(tz).date()
        if day not in counts:
            continue
        counts[day][0] += 1
        if recorrection.is_recorrection(text):
            counts[day][1] += 1
    return [(d, n, h) for d, (n, h) in sorted(counts.items())]


def load_redactor():
    """The memory engine's redact_secrets (the repo's one secret mask), or None
    when it cannot be loaded -- hybrid then refuses to send anything."""
    path = os.path.join(os.path.dirname(HERE), "memory-engine", "memory.py")
    try:
        spec = importlib.util.spec_from_file_location("_recorr_memory", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.redact_secrets
    except Exception:
        return None


def mask_for_model(text, redact):
    return _PATH_RX.sub("<path>", redact(text or ""))


def prev_for_model(prev, redact):
    """Previous agent text -> reader-cleaned, secret-masked, path-free, last
    PREV_AGENT_CHARS chars. Masking runs on the whole text before the cut so a
    secret cannot be split by it."""
    cleaned = owner_turns.clean(prev)
    if not cleaned:
        return ""
    return mask_for_model(cleaned, redact).strip()[-recorrection.PREV_AGENT_CHARS:]


def call_sonnet(prompt):
    """One `claude -p` call. Returns stdout, or None on any failure. No tools,
    no session file, no user settings / hooks; run from the temp dir so no
    instance CLAUDE.md / .mcp.json rides along with the turn."""
    cmd = [os.environ.get(CLAUDE_BIN_ENV) or "claude", "-p", "--model", "sonnet",
           "--tools", "", "--no-session-persistence", "--disable-slash-commands",
           "--setting-sources", "", "--output-format", "text"]
    try:
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                             timeout=MODEL_TIMEOUT_S, cwd=tempfile.gettempdir())
    except Exception:
        return None
    return out.stdout if out.returncode == 0 else None


def hybrid_day_rows(proj_dir, tz, first_day, last_day, model_fn, redact,
                    dry_run=False, max_calls=None):
    """{date: Counter} per day in [first_day, last_day]. Counter keys: turns,
    strong, uncertain (would go / went to the model), model_c2, unresolved,
    plus "cue:<name>" for every cue that decided a C2 or triggered a call.
    dry_run: routing only, model_fn is never called.
    max_calls: model call cap. Hitting it on a day after first_day drops that
    day and every later one (the next run picks them up); on first_day itself
    the rest of its uncertain turns count as unresolved, so a run always
    makes progress."""
    since = datetime.datetime.combine(first_day, datetime.time(), tz)
    days = {}
    calls = 0
    d = first_day
    while d <= last_day:
        days[d] = collections.Counter()
        d += datetime.timedelta(days=1)
    for ts, text, prev in owner_turns.iter_owner_turns(
            proj_dir, since_utc=since.astimezone(datetime.timezone.utc),
            with_prev=True):
        day = ts.astimezone(tz).date()
        c = days.get(day)
        if c is None:
            continue
        c["turns"] += 1
        route, cue = recorrection.routing(text)
        if route == "decided_n":
            continue
        if route == "strong":
            c["strong"] += 1
            c["cue:" + cue] += 1
            continue
        c["uncertain"] += 1
        c["cue:" + (cue or "nocue")] += 1
        if dry_run:
            continue
        if max_calls is not None and calls >= max_calls:
            if day > first_day:
                # turns come oldest first: nothing later belongs to this run
                return {d: v for d, v in days.items() if d < day}
            c["unresolved"] += 1
            continue
        calls += 1
        c["called"] += 1
        label, _, _ = recorrection.classify_hybrid(
            mask_for_model(text, redact), prev_for_model(prev, redact),
            model_fn)
        if label == "C2":
            c["model_c2"] += 1
        elif label == recorrection.UNRESOLVED:
            c["unresolved"] += 1
            c["failed"] += 1
    return days


def fmt_hybrid_row(day, c):
    n = c["turns"]
    h = c["strong"] + c["model_c2"]
    rate = ("%.3f" % (float(h) / n)) if n else "-"
    cues = ",".join("%s:%d" % (k[4:], v) for k, v in sorted(c.items())
                    if k.startswith("cue:")) or "-"
    return "%s\t%d\t%d\t%s\t%s+%s\t%d\t%d\t%d\t%d\t%s\n" % (
        day.isoformat(), n, h, rate, recorrection.PREDICATE_VERSION,
        recorrection.HYBRID_VERSION, c["strong"], c["called"],
        c["model_c2"], c["unresolved"], cues)


def fmt_row(day, n, h):
    rate = ("%.3f" % (float(h) / n)) if n else "-"
    return "%s\t%d\t%d\t%s\t%s\n" % (day.isoformat(), n, h, rate,
                                     recorrection.PREDICATE_VERSION)


def run_count(args):
    root = os.path.abspath(args.root)
    home = args.home or os.path.expanduser("~")
    tz = datetime.timezone(datetime.timedelta(hours=utc_offset_hours(root)))
    today = datetime.datetime.now(tz).date()
    yesterday = today - datetime.timedelta(days=1)
    hybrid = args.mode == "hybrid"
    ledger = args.ledger or os.path.join(
        root, HYBRID_LEDGER_REL if hybrid else LEDGER_REL)

    last = last_ledger_date(ledger)
    first = (last + datetime.timedelta(days=1)) if last else \
        today - datetime.timedelta(days=args.backfill_days)
    horizon = today - datetime.timedelta(days=retention_days(home) - 1)
    if first < horizon:
        sys.stderr.write("recorrection-count: %s..%s is past transcript "
                         "retention; starting at %s\n"
                         % (first, horizon - datetime.timedelta(days=1), horizon))
        first = horizon
    if first > yesterday:
        print("recorrection-count: ledger current through %s" % (last or "-"))
        return 0

    proj = owner_turns.project_dir(root, home)
    if not os.path.isdir(proj):
        sys.stderr.write("recorrection-count: no transcripts at %s\n" % proj)
        return 1
    if hybrid:
        redact = load_redactor()
        if redact is None and not args.dry_run:
            sys.stderr.write("recorrection-count: secret mask unavailable; "
                             "hybrid sends nothing\n")
            return 1
        days = hybrid_day_rows(proj, tz, first, yesterday, call_sonnet, redact,
                               dry_run=args.dry_run,
                               max_calls=max(args.max_model_calls, 0))
        if args.dry_run:
            return print_hybrid_dry_run(days)
        tot = collections.Counter()
        for c in days.values():
            tot.update(c)
        if tot["called"] and tot["failed"] == tot["called"]:
            sys.stderr.write("recorrection-count: every model call failed; "
                             "nothing written, the next run retries\n")
            return 1
        rows = [fmt_hybrid_row(d, c) for d, c in sorted(days.items())]
        yesterday = max(days) if days else first - datetime.timedelta(days=1)
    else:
        rows = [fmt_row(*r) for r in day_rows(proj, tz, first, yesterday)]

    if args.dry_run:
        sys.stdout.write(HEADER + "".join(rows))
        return 0
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    with open(ledger, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        # re-read under the lock: a concurrent run may have appended already
        cur = last_ledger_date(ledger)
        fresh = [r for r in rows
                 if cur is None or datetime.date.fromisoformat(r[:10]) > cur]
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(HYBRID_HEADER if hybrid else HEADER)
        f.write("".join(fresh))
    print("recorrection-count: appended %d day(s) %s..%s"
          % (len(fresh), first, yesterday))
    return 0


def print_hybrid_dry_run(days):
    """Routing only: how many turns would go to the model. No model call."""
    print("# hybrid dry-run: routing only, no model call, nothing written")
    print("date\towner_turns\tstrong\twould_call\tshare")
    tot = collections.Counter()
    for d, c in sorted(days.items()):
        tot.update(c)
        print("%s\t%d\t%d\t%d\t%s" % (
            d.isoformat(), c["turns"], c["strong"], c["uncertain"],
            "%.3f" % (float(c["uncertain"]) / c["turns"]) if c["turns"] else "-"))
    n = len(days) or 1
    print("total\t%d\t%d\t%d\t%s" % (
        tot["turns"], tot["strong"], tot["uncertain"],
        "%.3f" % (float(tot["uncertain"]) / tot["turns"]) if tot["turns"] else "-"))
    print("projected model calls/day (mean over %d day(s)): %.1f"
          % (len(days), float(tot["uncertain"]) / n))
    return 0


def run_eval(path, rubric="v1"):
    stats = {}
    leaked = 0
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    for r in rows:
        text = owner_turns.clean(r["text"])
        if r["label"] == "X":
            leaked += text is not None
            continue
        if rubric == "v2":
            if "label2" not in r:
                continue
            pos = r["label2"] == "C2"
        else:
            pos = r["label"] == "C"
        hit = text is not None and recorrection.is_recorrection(text)
        for split in (r.get("split", "all"), "ALL"):
            s = stats.setdefault(split, {"tp": 0, "fn": 0, "fp": 0, "tn": 0})
            s[("tp" if hit else "fn") if pos else ("fp" if hit else "tn")] += 1
    print("predicate %s on %s" % (recorrection.PREDICATE_VERSION, path))
    print("split\tn\tpos\tTP\tFN\tFP\tTN\thit_rate\tprecision\tfp_rate")
    for split in sorted(stats, key=lambda k: (k == "ALL", k)):
        s = stats[split]
        pos, neg = s["tp"] + s["fn"], s["fp"] + s["tn"]
        print("%s\t%d\t%d\t%d\t%d\t%d\t%d\t%s\t%s\t%s" % (
            split, pos + neg, pos, s["tp"], s["fn"], s["fp"], s["tn"],
            "%.3f" % (float(s["tp"]) / pos) if pos else "-",
            "%.3f" % (float(s["tp"]) / (s["tp"] + s["fp"])) if s["tp"] + s["fp"] else "-",
            "%.3f" % (float(s["fp"]) / neg) if neg else "-"))
    print("machine rows (X) not dropped by the reader: %d" % leaked)
    return 0 if leaked == 0 else 1


def _rates(s):
    pos, neg = s["tp"] + s["fn"], s["fp"] + s["tn"]
    return (pos + neg, pos, s["tp"], s["fn"], s["fp"], s["tn"],
            "%.3f" % (float(s["tp"]) / pos) if pos else "-",
            "%.3f" % (float(s["tp"]) / (s["tp"] + s["fp"]))
            if s["tp"] + s["fp"] else "-",
            "%.3f" % (float(s["fp"]) / neg) if neg else "-")


def run_eval_hybrid(path, model_fn, redact, repeat=1, dry_run=False,
                    max_calls=DEFAULT_MAX_MODEL_CALLS):
    """Offline hybrid score on the rubric-v2 rows (`label2`). Prints routing
    share, then per model run hit rate / precision / FP rate per split, the
    unresolved count, and run-to-run agreement on the called rows. Text is
    never printed. -> exit code."""
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    items = []  # (split, pos, route, masked_text, masked_prev)
    for r in rows:
        if r.get("label") == "X" or "label2" not in r:
            continue
        text = owner_turns.clean(r["text"])
        route = recorrection.routing(text)[0] if text is not None else "decided_n"
        masked = prev = ""
        if route == "uncertain" and not dry_run:
            masked = mask_for_model(text, redact)
            prev = prev_for_model(r.get("prev") or "", redact)
        items.append((r.get("split", "all"), r["label2"] == "C2", route,
                      masked, prev))
    repeat = max(repeat, 1)
    routed = collections.Counter()
    for split, _, route, _, _ in items:
        for k in (split, "ALL"):
            routed[(k, "n")] += 1
            routed[(k, route)] += 1
    n_unc = routed[("ALL", "uncertain")]
    print("hybrid %s+%s on %s (rubric v2)" % (
        recorrection.PREDICATE_VERSION, recorrection.HYBRID_VERSION, path))
    print("split\tn\tstrong\tuncertain\tshare")
    splits = sorted({k for k, _ in routed}, key=lambda k: (k == "ALL", k))
    for k in splits:
        n = routed[(k, "n")]
        print("%s\t%d\t%d\t%d\t%s" % (
            k, n, routed[(k, "strong")], routed[(k, "uncertain")],
            "%.3f" % (float(routed[(k, "uncertain")]) / n) if n else "-"))
    need = n_unc * repeat
    print("model calls needed: %d (%d uncertain x %d run(s))"
          % (need, n_unc, repeat))
    if dry_run:
        print("# dry-run: no model call")
        return 0
    if need > max_calls:
        sys.stderr.write("recorrection-count: %d model calls exceed "
                         "--max-model-calls %d; nothing sent\n"
                         % (need, max_calls))
        return 2
    labels = []  # per run: list aligned with items (None = not called)
    for _ in range(repeat):
        run = []
        for _, _, route, masked, prev in items:
            if route != "uncertain":
                run.append(None)
                continue
            label, _, _ = recorrection.classify_hybrid(masked, prev, model_fn)
            run.append(label)
        labels.append(run)
    print("run\tsplit\tn\tpos\tTP\tFN\tFP\tTN\thit_rate\tprecision\t"
          "fp_rate\tunresolved")
    failed_all = True
    for i, run in enumerate(labels, 1):
        stats = {}
        unres = 0
        for (split, pos, route, _, _), lab in zip(items, run):
            if lab == recorrection.UNRESOLVED:
                unres += 1
            elif lab is not None:
                failed_all = False
            hit = route == "strong" or lab == "C2"
            for k in (split, "ALL"):
                s = stats.setdefault(k, {"tp": 0, "fn": 0, "fp": 0, "tn": 0})
                s[("tp" if hit else "fn") if pos else
                  ("fp" if hit else "tn")] += 1
        for k in sorted(stats, key=lambda k: (k == "ALL", k)):
            print("%d\t%s\t%d\t%d\t%d\t%d\t%d\t%d\t%s\t%s\t%s\t%s" % (
                (i, k) + _rates(stats[k]) + (unres if k == "ALL" else "",)))
    if n_unc and repeat > 1:
        agree = sum(1 for j, it in enumerate(items) if it[2] == "uncertain"
                    and len({run[j] for run in labels}) == 1)
        print("run-to-run agreement on called rows: %d/%d (%.3f)"
              % (agree, n_unc, float(agree) / n_unc))
    if n_unc and failed_all:
        sys.stderr.write("recorrection-count: every model call failed\n")
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--root", default=os.path.dirname(HERE),
                    help="instance root (default: this script's parent)")
    ap.add_argument("--home", default=None, help="home holding ~/.claude")
    ap.add_argument("--ledger", default=None, help="ledger path override")
    ap.add_argument("--backfill-days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--eval", metavar="FILE")
    ap.add_argument("--rubric", choices=("v1", "v2"), default="v1")
    ap.add_argument("--mode", choices=("patterns", "hybrid"), default="patterns")
    ap.add_argument("--max-model-calls", type=int,
                    default=DEFAULT_MAX_MODEL_CALLS,
                    help="hybrid: model call cap per run (default %(default)s)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="--eval --mode hybrid: model runs (agreement)")
    args = ap.parse_args(argv)
    if args.eval and args.mode == "hybrid":
        redact = load_redactor()
        if redact is None and not args.dry_run:
            sys.stderr.write("recorrection-count: secret mask unavailable; "
                             "hybrid sends nothing\n")
            return 1
        return run_eval_hybrid(args.eval, call_sonnet, redact,
                               repeat=args.repeat, dry_run=args.dry_run,
                               max_calls=max(args.max_model_calls, 0))
    if args.eval:
        return run_eval(args.eval, args.rubric)
    return run_count(args)


if __name__ == "__main__":
    sys.exit(main())
