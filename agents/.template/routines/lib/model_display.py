"""One owner-facing model display rule (DGN-1227, owner decision dec-177).

Format: full model name, vendor + family + version -- Claude Opus 5.5,
Claude Sonnet 5, Claude Fable 5.1, Claude Haiku 4.5, GPT 6 Sol, GPT 6 Astra,
GPT 6 Luna, GPT 5.6 Terra (owner 2026-09-27 13:38, supersedes the dec-177
no-vendor-prefix form).

This module is the ONLY parser of model ids and the ONLY reader of
lib/dispatch-model-display.tsv. dispatch-detached.sh (start/end notices,
session-inbox drop) shells out to the CLI below; status-footer.py imports it.

Where the version comes from:
  * codex: the concrete slug (gpt-6-astra -> "GPT 6 Astra").
  * claude-code: the CLI decides the version at run time, so the display is
    derived from the model id the run itself reported (stream.jsonl, recorded
    as status.json model_observed). Before any id is observed the TSV row's
    display column carries the bare name ("Claude Opus") -- a version is never guessed.
An unknown id falls back to the raw id, never to a guess.

Layered view (DGN-1814 r3, owner 2026-10-01 08:25): model_table() reads the
same rows as vendor -> family -> version (Claude / Opus / 5.5), split out of
the display column. The optional 5th TSV column lists the surfaces a row is
offered on ("chat" = the bridge /model picker); a second chat vendor is
table rows only.

No runtime marker (DGN-1752, owner 2026-09-27 13:38): every runtime shows the
same full model name on every surface (start/end notices, workbench row,
dispatch-return). RUNTIME_SUFFIX carries " (Cloud)" for cloud runs (owner 2026-10-07).

CLI:  model_display.py [--tsv FILE] <runtime> <alias> [observed]
      observed = a raw model id or the JSON list status.json stores (first
      entry wins). Prints the display, always rc 0 (presentation, never a gate).
      model_display.py --wait-observed <seconds> <run_dir>...
      Bounded wait for the model id each live run reports (DGN-1727: the START
      notice is rendered before the run's first stream event). Prints one
      "<run_dir><TAB><id or empty>" line per run; empty = not observed in time.
"""
import json
import os
import re
import sys
import time

DEFAULT_TSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dispatch-model-display.tsv")

# claude-opus-5-5, claude-sonnet-5, claude-haiku-4-5-20251001, and the legacy
# claude-3-5-sonnet-20241022 order. A trailing -YYYYMMDD snapshot is dropped.
_CLAUDE_NEW = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d{1,2})?)(?:-\d{8})?$")
_CLAUDE_OLD = re.compile(r"^claude-(\d+(?:-\d{1,2})?)-([a-z]+)(?:-\d{8})?$")
# gpt-6-astra, gpt-5.6-terra
_CODEX = re.compile(r"^gpt-(\d+(?:\.\d+)?)-([a-z]+)$")


# Owner 2026-09-27 13:38: full model names ("Claude Opus 5.5", "GPT 6 Astra").
# Owner 2026-10-07 19:04: cloud runs carry " (Cloud)" again now that cloud
# dispatch is back (dec-306); local and codex stay suffix-free.
RUNTIME_SUFFIX = {"cloud": " (Cloud)"}


def parse_model_id(model_id):
    """Display for a concrete model id, or None when the id is not recognised."""
    mid = re.sub(r"\[[^\]]*\]$", "", (model_id or "").strip().lower())
    m = _CLAUDE_NEW.match(mid)
    if m:
        return "Claude %s %s" % (m.group(1).capitalize(), m.group(2).replace("-", "."))
    m = _CLAUDE_OLD.match(mid)
    if m:
        return "Claude %s %s" % (m.group(2).capitalize(), m.group(1).replace("-", "."))
    m = _CODEX.match(mid)
    if m:
        return "GPT %s %s" % (m.group(1), m.group(2).capitalize())
    return None


def first_observed(observed):
    """First model id out of a raw id or a status.json model_observed list."""
    if isinstance(observed, str):
        text = observed.strip()
        if text.startswith("["):
            try:
                observed = json.loads(text)
            except ValueError:
                return ""
        else:
            return text
    if isinstance(observed, list):
        for item in observed:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def tsv_row(runtime, alias, tsv=None):
    """(observed_slug, display) columns of the (runtime, alias) row, or None."""
    try:
        with open(tsv or DEFAULT_TSV, newline="", errors="replace") as fh:
            for line in fh:
                if line.lstrip().startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 4 and cols[0] == runtime and cols[1] == alias:
                    return cols[2], cols[3]
    except OSError:
        pass
    return None


_VERSION_WORD = re.compile(r"^\d+(?:\.\d+)*$")


def split_display(display):
    """(vendor, family, version) out of one display name.

    "Claude Opus 5.5" -> ("Claude", "Opus", "5.5"); "GPT 6 Astra" ->
    ("GPT", "Astra", "6"); a bare "Claude Opus" has version "".
    """
    words = (display or "").split()
    if not words:
        return "", "", ""
    rest = words[1:]
    family = " ".join(w for w in rest if not _VERSION_WORD.match(w))
    version = " ".join(w for w in rest if _VERSION_WORD.match(w))
    return words[0], family, version


def table_rows(tsv=None):
    """Every data row as (runtime, alias, observed_slug, display, surfaces)."""
    rows = []
    try:
        with open(tsv or DEFAULT_TSV, newline="", errors="replace") as fh:
            for line in fh:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 4:
                    continue
                surfaces = tuple(x.strip() for x in cols[4].split(",")
                                 if x.strip()) if len(cols) > 4 else ()
                rows.append((cols[0], cols[1], cols[2], cols[3], surfaces))
    except OSError:
        pass
    return rows


def model_table(surface=None, tsv=None):
    """Layered model table: {vendor: {family: entry}} in file order.

    entry = {"version", "display", "runtime", "alias"}. surface keeps only the
    rows offered there (e.g. "chat"); the first row of a vendor/family wins.
    """
    table = {}
    for runtime, alias, _slug, display, surfaces in table_rows(tsv):
        if surface and surface not in surfaces:
            continue
        vendor, family, version = split_display(display)
        if not vendor or not family:
            continue
        table.setdefault(vendor, {}).setdefault(family, {
            "version": version, "display": display,
            "runtime": runtime, "alias": alias})
    return table


def model_display(runtime, alias, observed=None, tsv=None):
    """The owner-facing label for one run.

    observed (raw id or list) wins when present: parsed, else the raw id.
    Without it the TSV display column stands in (bare name for claude-code),
    and a missing row keeps the capitalized alias.
    """
    return _base_display(runtime, alias, observed, tsv) + RUNTIME_SUFFIX.get(runtime, "")


def _base_display(runtime, alias, observed, tsv):
    mid = first_observed(observed)
    if mid:
        return parse_model_id(mid) or mid
    row = tsv_row(runtime, alias, tsv)
    if row and row[1]:
        return row[1]
    return (alias or "")[:1].upper() + (alias or "")[1:]


def observe_from_stream(path, first_only=False):
    """Model ids a claude -p run reported, first-seen order, from stream.jsonl.

    Defensive line-by-line (the stream schema is not ours): every failure
    degrades to []. Assistant turns win; system/init is the fallback when no
    assistant turn landed. first_only stops at the first assistant id (cheap
    enough for a live board rendering a still-growing file).
    """
    seen, init = [], []
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(doc, dict):
                    continue
                if doc.get("type") == "assistant":
                    msg = doc.get("message")
                    mid = msg.get("model") if isinstance(msg, dict) else None
                    if isinstance(mid, str) and mid and mid not in seen:
                        seen.append(mid)
                        if first_only:
                            break
                elif doc.get("type") == "system" and doc.get("subtype") == "init":
                    mid = doc.get("model")
                    if isinstance(mid, str) and mid and mid not in init:
                        init.append(mid)
    except OSError:
        pass
    return seen or init


def run_observed(run_dir):
    """(model id or "", settled) for one run dir.

    The id is the first one the run reported: status.json model_observed once
    the run closed, else the first assistant/init id in stream.jsonl. settled
    means no id can arrive any more (status.json exists, or the worker pid is
    gone), so a waiter stops instead of burning its bound.
    """
    status = os.path.join(run_dir, "status.json")
    try:
        with open(status, encoding="utf-8") as fh:
            doc = json.load(fh)
        mid = first_observed(doc.get("model_observed")) if isinstance(doc, dict) else ""
        return mid, True
    except (OSError, ValueError):
        pass
    ids = observe_from_stream(os.path.join(run_dir, "stream.jsonl"), first_only=True)
    if ids:
        return ids[0], True
    try:
        with open(os.path.join(run_dir, "pid"), encoding="utf-8") as fh:
            os.kill(int(fh.read().strip()), 0)
    except PermissionError:
        pass
    except (OSError, ValueError):
        return "", True
    return "", False


def wait_observed(run_dirs, timeout, interval=1.0):
    """{run_dir: model id or ""}, polling until every run settled or timeout.

    ONE shared deadline for the whole batch (never per run), so a burst of N
    runs can delay its notice by at most `timeout`. Never raises.
    """
    found = {}
    pending = list(run_dirs)
    deadline = time.time() + max(0.0, timeout)
    while True:
        for d in list(pending):
            try:
                mid, settled = run_observed(d)
            except Exception:
                mid, settled = "", True
            if mid or settled:
                found[d] = mid
                pending.remove(d)
        if not pending or time.time() >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - time.time())))
    for d in pending:
        found[d] = ""
    return found


def main(argv):
    args = argv[1:]
    tsv = None
    if len(args) >= 2 and args[0] == "--tsv":
        tsv, args = args[1], args[2:]
    if args[:1] == ["--observe-stream"] and len(args) == 2:
        print(json.dumps(observe_from_stream(args[1])))
        return 0
    if args[:1] == ["--wait-observed"] and len(args) >= 2:
        try:
            timeout = float(args[1])
        except ValueError:
            timeout = 0.0
        found = wait_observed(args[2:], timeout)
        for d in args[2:]:
            print("%s\t%s" % (d, found.get(d, "")))
        return 0
    if len(args) < 2:
        return 0
    print(model_display(args[0], args[1], args[2] if len(args) > 2 else None, tsv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
