#!/usr/bin/env python3
"""skill-usage.py -- CRUD reader/backfill CLI for the skill-usage ledger.

Ledger written by routines/skill-feedback-gate.py --record (PostToolUse,
matcher "Skill"): one line per Skill-tool invocation, appended to
~/.dogany/skill-usage/<slug>-<year>.jsonl (append-only, never rewritten).
Event schema: {v, ts, skill, slug, actor, session_id, source}. source is
"hook" (real-time) or "backfill" (seeded from a transcript, see `backfill`
below); actor is one of owner-session|detached|cron|junior|subagent|unknown.

Design doc: previous dispatch report (skill-usage-ledger design, 2026-08-25).
CRUD:
  C (skill appears)   -- no registration store. A skill's mere existence
                          under .claude/skills*/ or ~/.dogany/shared-skills/
                          makes it "live"; count starts at 0 automatically.
  R (read)            -- `report` subcommand below.
  U (rename/migrate)  -- ~/.dogany/skill-usage/aliases.tsv, "<old>\\t<new>"
                          per line, append-only. `report` folds old names
                          into new ones; history is never rewritten.
  D (skill removed)   -- no tombstone is ever stored. "live" is recomputed
                          on every read from the current skills directory,
                          never cached -- so a stale cached tombstone can
                          never misjudge a skill that came back (the exact
                          failure mode a tombstone-based design would risk).

No rotation/rollup is implemented (measured volume: tens of KB/year). Escape
hatch only, not built: when a <slug>-<year>.jsonl segment exceeds a few MB,
compress it to {month, skill, actor, count} rollup rows and archive the
original -- not needed for years at current volume, so not built yet.

Nature axis (2026-08-25, owner-ratified; spec: docs/SKILL-NATURE.md):
a skill declares its own nature in SKILL.md frontmatter, `nature:` key.
  capability  -- provides a feature. usage count = promotion signal
                 (DEPLOY-DISCIPLINE.md section 7, L1->L2).
  remediation -- restores broken state. usage count = DEFECT signal
                 ("needed repair N times over M days"); never a promotion
                 signal -- high count files a ticket instead.
Absent/invalid value = unknown -> EXCLUDED from promotion judgment entirely
(never guessed from the count). `report --by skill` splits the live section
along this axis; --by slug/actor output is unchanged (nature is per-skill).
"""
import argparse
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""

USAGE_DIR_NAME = "skill-usage"
MAX_SKILL_NAME = 100


def _home():
    return os.environ.get("DOGANY_HOME") or os.path.expanduser("~/.dogany")


def _usage_dir():
    return os.path.join(_home(), USAGE_DIR_NAME)


# ---------------------------------------------------------------------------
# shared: alias table (U) + ledger scan
# ---------------------------------------------------------------------------

def _load_aliases():
    """old-name -> new-name map from aliases.tsv. Missing file = empty map.
    A chain (a->b, b->c) folds transitively; a cycle is broken defensively
    by capping the walk at the map's own size."""
    path = os.path.join(_usage_dir(), "aliases.tsv")
    raw = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or "\t" not in line:
                    continue
                old, new = line.split("\t", 1)
                old, new = old.strip(), new.strip()
                if old and new:
                    raw[old] = new
    except OSError:
        return {}

    def resolve(name):
        seen = 0
        while name in raw and seen <= len(raw):
            name = raw[name]
            seen += 1
        return name

    return {old: resolve(old) for old in raw}


def _iter_events(slug_filter=None):
    """Yield every event dict from every <slug>-<year>.jsonl ledger file.
    Malformed lines are skipped (fail-open reader, never crashes a report)."""
    pattern = os.path.join(_usage_dir(), "*.jsonl")
    for path in sorted(glob.glob(pattern)):
        base = os.path.basename(path)
        if base == "aliases.tsv":
            continue
        slug = base.rsplit("-", 1)[0] if "-" in base else base[:-len(".jsonl")]
        if slug_filter and slug != slug_filter:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        yield obj
        except OSError:
            continue


def _live_skill_set(root):
    """Union of skill names currently installed under `root`: .claude/skills/,
    .claude/skills-bundle/, and the machine-global ~/.dogany/shared-skills/.
    Directory listing only -- no registration store (design C decision)."""
    return {name for name, _ in _skill_dirs(root)}


NATURE_VALUES = ("capability", "remediation")


def _skill_dirs(root):
    """Yield (skill-name, skill-dir) for every installed skill under `root`
    -- same three surfaces as _live_skill_set (single source of 'installed')."""
    dirs = [os.path.join(root, ".claude/skills"),
            os.path.join(root, ".claude/skills-bundle"),
            os.path.join(_home(), "shared-skills")]
    for d in dirs:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for entry in entries:
            full = os.path.join(d, entry)
            if os.path.isdir(full):
                yield entry, full


def _read_nature(skill_dir):
    """Parse `nature:` out of SKILL.md frontmatter. Returns 'capability' or
    'remediation', else None (absent file / no frontmatter / unknown value --
    all fail-open to None; an unknown nature is never guessed)."""
    path = os.path.join(skill_dir, "SKILL.md")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return None
            for _ in range(100):  # frontmatter is small; cap the scan
                line = fh.readline()
                if not line or line.strip() == "---":
                    return None
                if line.startswith("nature:"):
                    val = line.split(":", 1)[1].strip().strip("\"'").lower()
                    return val if val in NATURE_VALUES else None
    except OSError:
        return None
    return None


def _nature_map(root):
    """skill-name -> declared nature for every installed skill. A name
    installed on two surfaces with conflicting declarations resolves to None
    (unknown): a conflict is not evidence either way."""
    natures = {}
    for name, full in _skill_dirs(root):
        nature = _read_nature(full)
        if name in natures and natures[name] != nature:
            natures[name] = None
        else:
            natures[name] = nature
    return natures


def _parse_since(spec):
    """'<N>d' / '<N>h' / a raw epoch int / None -> epoch cutoff or None."""
    if not spec:
        return None
    spec = spec.strip()
    try:
        if spec.endswith("d"):
            return int(datetime.datetime.now(datetime.timezone.utc).timestamp()) - int(spec[:-1]) * 86400
        if spec.endswith("h"):
            return int(datetime.datetime.now(datetime.timezone.utc).timestamp()) - int(spec[:-1]) * 3600
        return int(spec)
    except ValueError:
        raise SystemExit("--since: expected '<N>d', '<N>h', or an epoch int, got {!r}".format(spec))


# ---------------------------------------------------------------------------
# R -- report
# ---------------------------------------------------------------------------

def cmd_report(args):
    cutoff = _parse_since(args.since)
    aliases = _load_aliases()
    root = os.path.abspath(args.root or os.getcwd())
    live_set = _live_skill_set(root)

    by = args.by
    live_counts = {}
    gone_counts = {}
    live_days = {}
    total_events = 0
    for ev in _iter_events(slug_filter=args.slug):
        ts = ev.get("ts")
        if not isinstance(ts, int):
            continue
        if cutoff is not None and ts < cutoff:
            continue
        skill = aliases.get(ev.get("skill"), ev.get("skill"))
        if not isinstance(skill, str) or not skill:
            continue
        if args.skill and skill != args.skill:
            continue

        key = {"skill": skill, "slug": ev.get("slug") or "unknown",
               "actor": ev.get("actor") or "unknown"}.get(by)
        if key is None:
            continue

        total_events += 1
        bucket = live_counts if skill in live_set else gone_counts
        bucket[key] = bucket.get(key, 0) + 1
        if bucket is live_counts:
            day = datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime("%Y-%m-%d")
            live_days.setdefault(key, set()).add(day)

    def _print_section(title, counts, tail=None):
        print(title)
        if not counts:
            print("  (none)")
            return
        width = max(len(k) for k in counts)
        for k, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            line = "  {}  {}".format(k.ljust(width), c)
            if tail:
                line += tail(k, c)
            print(line)

    print("skill-usage report -- by={} since={} root={}".format(
        by, args.since or "all-time", root))
    print("total events scanned (post-filter): {}".format(total_events))
    print()
    if by == "skill":
        # Nature split (spec: docs/SKILL-NATURE.md). Promotion candidates =
        # capability ONLY; unknown is held out, never guessed into a bucket.
        natures = _nature_map(root)
        split = {"capability": {}, "remediation": {}, None: {}}
        for k, c in live_counts.items():
            split[natures.get(k)][k] = c
        days = lambda k: len(live_days.get(k, ()))
        _print_section(
            "[live / capability -- promotion candidates (usage = useful)]",
            split["capability"],
            tail=lambda k, c: "  ({}d active)".format(days(k)))
        print()
        _print_section(
            "[live / remediation -- DEFECT signal (usage = breakage; "
            "ticket, not promotion)]",
            split["remediation"],
            tail=lambda k, c: "  -- {} repairs across {}d".format(c, days(k)))
        print()
        _print_section(
            "[live / unknown nature -- judgment withheld (no nature: in "
            "SKILL.md; excluded from promotion)]",
            split[None],
            tail=lambda k, c: "  ({}d active)".format(days(k)))
    else:
        _print_section("[live]", live_counts)
    print()
    _print_section("[gone -- skill no longer installed at --root]", gone_counts)
    return 0


# ---------------------------------------------------------------------------
# C source: transcript backfill (one-time historical seed)
# ---------------------------------------------------------------------------

def _parse_transcript_ts(raw):
    """'2026-07-29T08:22:17.984Z' (or without fractional seconds) -> epoch."""
    if not isinstance(raw, str) or not raw:
        return None
    s = raw[:-1] if raw.endswith("Z") else raw
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def _iter_transcript_skill_calls(transcripts_dir):
    """Yield (event_dict) for every Skill tool_use found under
    transcripts_dir, main-session files first, then subagents/agent-*.jsonl
    (path-distinguished -- actor differs, see caller)."""
    main_files = sorted(glob.glob(os.path.join(transcripts_dir, "*.jsonl")))
    sub_files = sorted(glob.glob(os.path.join(transcripts_dir, "*", "subagents", "agent-*.jsonl")))

    def _scan(path, is_subagent):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    message = obj.get("message") or {}
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if not (isinstance(c, dict) and c.get("name") == "Skill"):
                            continue
                        tool_input = c.get("input") or {}
                        skill = tool_input.get("skill")
                        if not isinstance(skill, str) or not skill.strip():
                            continue
                        ts = _parse_transcript_ts(obj.get("timestamp"))
                        if ts is None:
                            continue
                        yield {
                            "uid": obj.get("uuid") or "",
                            "ts": ts,
                            "skill": skill.strip()[:MAX_SKILL_NAME],
                            "cwd": obj.get("cwd") or "",
                            "session_id": obj.get("sessionId") or "",
                            "is_subagent": is_subagent,
                        }
        except OSError:
            return

    for path in main_files:
        yield from _scan(path, is_subagent=False)
    for path in sub_files:
        yield from _scan(path, is_subagent=True)


def cmd_backfill(args):
    transcripts_dir = os.path.abspath(args.transcripts_dir)
    if not os.path.isdir(transcripts_dir):
        raise SystemExit("--transcripts-dir not found: {}".format(transcripts_dir))

    usage_dir = _usage_dir()
    os.makedirs(usage_dir, exist_ok=True)

    # Idempotency: read every existing "uid" already on disk (backfill-sourced
    # events only) so a re-run never double-counts (grill: "백필이 중복 계상하나?").
    seen_uids = set()
    for ev in _iter_events():
        uid = ev.get("uid")
        if uid:
            seen_uids.add(uid)

    written = 0
    skipped_dup = 0
    skipped_bad = 0
    by_year_fh = {}
    try:
        for call in _iter_transcript_skill_calls(transcripts_dir):
            uid = call["uid"]
            if not uid or uid in seen_uids:
                skipped_dup += 1
                continue
            seen_uids.add(uid)

            slug = args.slug or conf_get("SLUG", os.path.join(call["cwd"], "config", "agent.conf")) or "unknown"
            actor = "subagent" if call["is_subagent"] else "unknown"
            record = {
                "v": 1,
                "ts": call["ts"],
                "skill": call["skill"],
                "slug": slug,
                "actor": actor,
                "session_id": call["session_id"],
                "source": "backfill",
                "uid": uid,
            }
            if not record["skill"]:
                skipped_bad += 1
                continue

            year = datetime.datetime.fromtimestamp(call["ts"], datetime.timezone.utc).strftime("%Y")
            path = os.path.join(usage_dir, "{}-{}.jsonl".format(slug, year))
            if args.dry_run:
                written += 1
                continue
            fh = by_year_fh.get(path)
            if fh is None:
                fh = open(path, "a", encoding="utf-8")
                by_year_fh[path] = fh
            fh.write(json.dumps(record) + "\n")
            written += 1
    finally:
        for fh in by_year_fh.values():
            fh.close()

    print("backfill: written={} skipped_dup={} skipped_bad_skill={} dry_run={}".format(
        written, skipped_dup, skipped_bad, args.dry_run))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_report = sub.add_parser("report", help="read the ledger")
    p_report.add_argument("--since", default=None, help="'<N>d', '<N>h', or an epoch int")
    p_report.add_argument("--by", choices=["skill", "slug", "actor"], default="skill")
    p_report.add_argument("--skill", default=None, help="filter to one skill name (post-alias)")
    p_report.add_argument("--slug", default=None, help="filter to one instance slug (ledger file)")
    p_report.add_argument("--root", default=None,
                           help="instance root for the live-skill filter (default: cwd)")
    p_report.set_defaults(func=cmd_report)

    p_backfill = sub.add_parser("backfill", help="seed the ledger from Claude Code transcripts (one-time)")
    p_backfill.add_argument("--transcripts-dir", required=True,
                             help="e.g. ~/.claude/projects/-Users-<u>-dogany-dev-crew-agents-<slug>")
    p_backfill.add_argument("--slug", default=None,
                             help="override slug for every written event (default: read per-event cwd's config/agent.conf SLUG=)")
    p_backfill.add_argument("--dry-run", action="store_true")
    p_backfill.set_defaults(func=cmd_backfill)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
