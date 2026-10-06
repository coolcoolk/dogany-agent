#!/usr/bin/env python3
"""weekly_eval.py -- weekly recommendation evaluation for the mint-agent module.

Called from weekly-review.sh (guarded block, see ACTIVATION.md). The path
resolves through .claude/skills/dogany-mint-agent, so an absent symlink (module off)
means this never runs -- one switch.

Behavior:
- gathers metrics per domain (domains.json):
  * conversation share, last N days (default 14): keyword-matched user
    messages / total user messages, from the instance's Claude transcripts
    (~/.claude/projects/<derived>/*.jsonl; sidechains + system lines skipped).
  * accumulated records: all-time row counts of the domain's kit_life tables
    (read-only sqlite; no writes ever).
- applies minting_policy (decision 10 discipline) against
  config/minting-state.json.
- self-heals orphaned state first (DGN-1480, existence check fixed by
  DGN-1505): a domain's dedicated_agent whose recorded landing path
  (minting_state.py accept --path) no longer exists is cleared before
  evaluation, so a deleted agent does not permanently block its domain's
  recommendations. Skipped under --dry-run (no state write), but still
  applied to the in-memory decision so the printed JSON reflects it.
- on recommend: prints the Korean recommendation block to STDOUT (weekly
  review appends it verbatim) and records the recommendation date in state.
- otherwise prints NOTHING and exits 0.
- --dry-run: prints the decision JSON to stdout, never writes state.

Exit codes: 0 ok (with or without recommendation), 1 config/data error.
"""

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import minting_policy as pol  # noqa: E402

# User-facing cost wording -- FIXED by DGN-238 decision 10, verbatim. Do not edit.
COST_WORDING = (
    "단기적으로 토큰 소모량이 다소 늘 수 있으나, 컨텍스트가 분리되어 "
    "인지 비용이 줄고 장기적·구조적으로 토큰 소모량이 줄도록 설계되어 있습니다."
)


def default_agent_dir():
    """__file__-derived LAST-RESORT guess: scripts -> mint-agent -> skills ->
    .claude -> <agent root> (root-relocation home), OR scripts -> mint-agent
    -> skills -> consume root -> <agent root> when reached through the
    activation symlink into $DOGANY_CONSUME_ROOT (DGN-1513) -- realpath()
    resolves the symlink, so this climb only lands on the real instance root
    if DOGANY_CONSUME_ROOT happens to sit exactly one path segment below it
    (the spec's own default, `<instance>/.dogany-consume`, but not a
    structural guarantee). Never call this directly -- go through
    resolve_agent_dir(), which validates it before trusting it (w1).
    """
    return os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))


def resolve_agent_dir(explicit):
    """Instance-root resolution ladder (DGN-1513 w1): explicit --agent-dir >
    $DOGANY_INSTANCE_ROOT > cwd's own .instance.conf > the __file__-derived
    guess above, validated (.instance.conf must exist there) before use.
    v1 had the same defect shape (symlink-derived root guess, unvalidated)
    against a different wrong root; this closes it structurally instead of
    trusting every future call site to always pass --agent-dir.
    """
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("DOGANY_INSTANCE_ROOT")
    if env:
        return os.path.abspath(env)
    cwd = os.getcwd()
    if os.path.isfile(os.path.join(cwd, ".instance.conf")):
        return cwd
    guess = default_agent_dir()
    if not os.path.isfile(os.path.join(guess, ".instance.conf")):
        print("[weekly_eval] ERROR: could not resolve the instance root "
              "(no --agent-dir, no DOGANY_INSTANCE_ROOT, cwd is not an "
              f"instance root, and the derived guess {guess!r} has no "
              ".instance.conf) -- refusing to guess (DGN-1513 w1)",
              file=sys.stderr)
        sys.exit(1)
    return guess


def agent_exists(dstate):
    """Does a minted agent still live at its recorded landing path?

    DGN-1505: accept (minting_state.py) writes the landing path at mint time
    (agent_path), so this reads a recorded fact back instead of guessing one.
    The prior version guessed the agent sits as a sibling of THIS instance's
    own directory (DGN-1483 convention) -- wrong the moment an agent lands
    under a different root (estate has 2+: ~/.dogany/agents/,
    a second operator-chosen root, and mint_run.sh --root has no default), which
    silently and permanently reaped a live agent's minting record.

    A record with no agent_path predates this change (accept used to write
    the slug only) -- treated as existing (never reaped): erasing a live
    agent's minting record is the irreversible direction, so an unknown
    location defaults to "assume alive" rather than "assume gone".
    """
    path = dstate.get("agent_path")
    if not path:
        return True
    return os.path.isdir(path)


def reap_orphans(state):
    """Self-heal DGN-1480: drop a dedicated_agent whose recorded path is gone.

    Mirrors minting_state.py's `clear` command (dedicated_agent -> None,
    minted_at + agent_path removed, recs/declines untouched) so the ghost
    left by a deleted agent does not depend on a human remembering to run it.
    Never fires on a legacy no-path record (see agent_exists). Returns the
    list of (domain, slug) pairs that were healed.
    """
    healed = []
    for domain, dstate in state.get("domains", {}).items():
        slug = dstate.get("dedicated_agent")
        if slug and not agent_exists(dstate):
            dstate["dedicated_agent"] = None
            dstate.pop("minted_at", None)
            dstate.pop("agent_path", None)
            healed.append((domain, slug))
    return healed


def transcripts_dir_for(agent_dir):
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(agent_dir))
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", slug)


def iter_user_texts(tdir, cutoff_utc):
    """Yield user message texts newer than cutoff from session jsonl files."""
    if not os.path.isdir(tdir):
        return
    for name in os.listdir(tdir):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(tdir, name)
        try:
            if dt.datetime.fromtimestamp(
                os.path.getmtime(path), dt.timezone.utc
            ) < cutoff_utc:
                continue
        except OSError:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("type") != "user" or obj.get("isSidechain"):
                        continue
                    ts = obj.get("timestamp")
                    if ts:
                        try:
                            when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if when < cutoff_utc:
                                continue
                        except ValueError:
                            pass
                    content = (obj.get("message") or {}).get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            it.get("text", "")
                            for it in content
                            if isinstance(it, dict) and it.get("type") == "text"
                        )
                    if not isinstance(content, str):
                        continue
                    text = content.strip()
                    # Skip injected/system-ish lines, keep real user talk.
                    if not text or text.startswith("<") or text.startswith("Caveat:"):
                        continue
                    yield text
        except OSError:
            continue


def count_records(db_path, tables):
    total = 0
    if not tables or not os.path.exists(db_path):
        return total
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error as e:
        # loud on stderr: a silent 0 under-counts and can suppress
        # record-threshold recommendations (2026-07-10 live lesson)
        print(f"[weekly_eval] db open failed ({e}); records=0", file=sys.stderr)
        return total
    try:
        for t in tables:
            if not str(t).replace("_", "").isalnum():
                continue
            try:
                total += con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error as e:
                print(f"[weekly_eval] count {t} failed ({e})", file=sys.stderr)
                continue
    finally:
        con.close()
    return total


def gather_metrics(agent_dir, domains, days, tdir=None, db_path=None):
    tdir = tdir or transcripts_dir_for(agent_dir)
    db_path = db_path or os.path.join(agent_dir, "database", "kit_life.db")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    texts = list(iter_user_texts(tdir, cutoff))
    total = len(texts)
    metrics = {}
    for domain, spec in domains.items():
        if domain.startswith("_"):
            continue
        kws = [k.lower() for k in spec.get("keywords", [])]
        hits = sum(1 for t in texts if any(k in t.lower() for k in kws))
        share = (hits / total) if total else 0.0
        metrics[domain] = {
            "share": share,
            "records": count_records(db_path, spec.get("kit_life_tables", [])),
            "msgs": hits,
            "total_msgs": total,
        }
    return metrics


def resolve_user_label(agent_dir):
    """Machine-resolved owner honorific -- same source scripts/pack/mint_run.sh
    reads for its {USER_LABEL} hint substitution (.instance.conf
    DOGANY_USER_LABEL), same "님" fallback (mint_run.sh:772) when unset.
    Reuses routines/lib/conf_reader.py, the one KEY=value parser python
    consumers share, instead of hand-rolling another (DGN-1464 shape).
    """
    sys.path.insert(0, os.path.join(agent_dir, "routines", "lib"))
    try:
        from conf_reader import conf_get
    except ImportError:
        def conf_get(key, conf_path=None):
            return ""
    label = conf_get("DOGANY_USER_LABEL", os.path.join(agent_dir, ".instance.conf"))
    return label or "님"


def render_block(winner, domains, user_label):
    ko = domains.get(winner["domain"], {}).get("ko", winner["domain"])
    share_pct = round(winner["share"] * 100)
    lines = ["[전담 에이전트 추천]"]
    if winner["rec_no"] == 2:
        lead = (
            f"{user_label}, 지난번에 보류하셨던 {ko} 전담 에이전트 건입니다. "
            f"이후에도 비중이 더 커져 마지막으로 한 번 더 추천드립니다."
        )
    else:
        lead = f"{user_label}, {ko} 전담 에이전트를 하나 만드는 것을 추천드립니다."
    evidence = (
        f"최근 2주 {ko} 관련 대화 비중이 약 {share_pct}%"
        f"{', 누적 기록이 ' + str(winner['records']) + '건' if winner['records'] else ''}인데 "
        f"아직 전담이 없습니다."
    )
    tail = '원하시면 "새 에이전트 만들어줘"라고 말씀해 주세요. 부담되시면 "됐어" 한마디면 접어두겠습니다.'
    lines.append(f"{lead} {evidence} {COST_WORDING} {tail}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="mint-agent weekly recommendation eval")
    ap.add_argument("--agent-dir", default=None,
                    help="instance root override (DGN-1513 w1: default is a "
                         "validated resolution ladder, not a bare guess)")
    ap.add_argument("--transcripts-dir", default=None)
    ap.add_argument("--db", default=None, help="kit_life.db path override (tests)")
    ap.add_argument("--domains", default=None, help="domains.json path override")
    ap.add_argument("--state", default=None, help="state json path override")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--today", default=None, help="YYYY-MM-DD override (tests)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print decision JSON, write nothing")
    args = ap.parse_args()

    agent_dir = resolve_agent_dir(args.agent_dir)
    domains_path = args.domains or os.path.join(SCRIPT_DIR, "..", "domains.json")
    state_path = args.state or os.path.join(agent_dir, "config", "minting-state.json")
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    try:
        with open(domains_path, "r", encoding="utf-8") as f:
            domains = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except (OSError, ValueError) as e:
        print(f"[weekly_eval] domains.json unreadable: {e}", file=sys.stderr)
        return 1

    metrics = gather_metrics(agent_dir, domains, args.days,
                             tdir=args.transcripts_dir, db_path=args.db)
    # DGN-1683 grill 4: the whole load -> reap/record -> save section runs
    # under the shared state lock (minting_state.py and the mint flow's accept
    # compensation take the same one); a dry run writes nothing, so it does
    # not lock.
    with (contextlib.nullcontext() if args.dry_run else pol.state_lock(state_path)):
        state = pol.load_state(state_path)

        healed = reap_orphans(state)
        for domain, slug in healed:
            print(f"[weekly_eval] cleared ghost dedicated_agent={slug!r} "
                  f"for domain={domain!r} (agent directory not found)",
                  file=sys.stderr)
        if healed and not args.dry_run:
            pol.save_state(state_path, state)

        winner, decisions = pol.evaluate_all(
            {d: {"share": m["share"], "records": m["records"]} for d, m in metrics.items()},
            state, today,
        )

        if args.dry_run:
            print(json.dumps({"winner": winner, "decisions": decisions,
                              "metrics": metrics}, ensure_ascii=False, indent=2))
            return 0

        if not winner:
            return 0

        # Record the fired recommendation (skip when same-day refire).
        if winner["reason"] != "refire-same-day":
            ds = pol.domain_state(state, winner["domain"])
            ds["recommendations"].append(today.isoformat())
            pol.save_state(state_path, state)

    print(render_block(winner, domains, resolve_user_label(agent_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
