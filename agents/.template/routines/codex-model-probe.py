#!/usr/bin/env python3
"""codex-model-probe.py -- DGN-1656: probe for newer codex model generations.

codex has no model alias layer, so the codex rows of lib/dispatch-model-display.tsv
are hand-pinned slugs. This probe finds a newer generation without a hand-kept
candidate list: candidates are DERIVED from the slug already in the table
(gpt-6.1-sol -> gpt-6.2-sol, gpt-7-sol), so the table is the only source.

Cost control: one real codex call (~6k tokens) per candidate. A candidate that
was not-released / tier-blocked is not re-probed before --min-age-days (default
14) have passed since its last probe (state file). Default is a dry run that
lists candidates; --probe spends tokens; --apply also rewrites the table.
The armed CLI maintenance path calls probe_table(periodic=True), sharing its
run budget and suppressing the entire sweep for 14 days after a spent tick.

Outcomes per candidate (classified from the codex output):
  ok            the model answered
  tier_blocked  "not supported when using Codex with a ChatGPT account": exists,
                this account tier cannot use it (re-probe after an upgrade)
  not_found     the model does not exist / is unknown
  error         anything else (never written to the table)

Safe apply: only `ok` candidates strictly newer than the pinned slug, same tier
name; the new TSV is validated through model_display.py before replacing the
old file, the old file is kept as a .bak rollback, and every change is appended
to the events log. Tier BINDINGS (dispatch-tier-bindings.tsv) are NOT touched:
a swapped slug is reported as "binding unmeasured" for a human to re-measure.

Env: CODEX_PROBE_CMD overrides the probe command (selftest); it gets the slug
as $1 and prints the codex output.
"""
import argparse, json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
TSV = os.path.join(HERE, "lib", "dispatch-model-display.tsv")
SLUG_RE = re.compile(r"^gpt-(\d+)(?:\.(\d+))?-([a-z]+)$")


def parse_slug(slug):
    m = SLUG_RE.match(slug)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), m.group(3)


def candidates(slug):
    p = parse_slug(slug)
    if not p:
        return []
    major, minor, name = p
    out = ["gpt-%d.%d-%s" % (major, minor + 1, name), "gpt-%d-%s" % (major + 1, name)]
    return out


def display_for(slug):
    major, minor, name = parse_slug(slug)
    ver = "%d.%d" % (major, minor) if minor else str(major)
    return "GPT %s %s" % (ver, name.capitalize())


def classify(rc, text):
    low = text.lower()
    if "not supported when using codex with a chatgpt account" in low:
        return "tier_blocked"
    if re.search(r"usage limit|hit your (?:usage )?limit|rate[ _-]?limit|quota|"
                 r"not authenticated|not logged in|unauthorized|token.*expired|"
                 r"please log ?in|invalid api key|sign in again|\b(?:401|429)\b", low):
        return "error"
    if re.search(r"does not exist|unknown model|model_not_found|not found", low):
        return "not_found"
    if rc == 0 and text.strip() and "error" not in low and "not supported" not in low:
        return "ok"
    return "error"


def run_probe(slug, executable="codex"):
    cmd = os.environ.get("CODEX_PROBE_CMD")
    argv = [cmd, slug] if cmd else [executable, "exec", "--skip-git-repo-check",
                                    "--ephemeral", "-s", "read-only", "-m", slug,
                                    "just answer me hi"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL)
        return classify(r.returncode, r.stdout + r.stderr)
    except (OSError, subprocess.TimeoutExpired):
        return "error"


def read_rows(tsv):
    with open(tsv) as fh:
        return fh.read().split("\n")


def codex_rows(lines):
    for i, ln in enumerate(lines):
        f = ln.split("\t")
        if len(f) >= 4 and f[0] == "codex":
            yield i, f


def load_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def validate(tsv_path, expect):
    sys.path.insert(0, os.path.join(HERE, "lib"))
    import model_display
    for alias, slug in expect.items():
        row = model_display.tsv_row("codex", alias, tsv_path)
        if row != (slug, display_for(slug)):
            return False
    return True


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def apply_display_table(tsv, lines, validator, event, log=None, preserve_backup=False):
    """Validate, back up and atomically replace a probed display table."""
    tmp = tsv + ".new"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines))
    if not validator(tmp):
        os.unlink(tmp)
        print("REFUSED: new table failed validation; table untouched")
        return False
    # Keep the table present throughout, with a byte-exact rollback copy.
    # A locked maintenance run may apply Claude and Codex in sequence. Keep
    # its first snapshot so rollback restores BOTH runtimes' previous rows.
    if not preserve_backup or not os.path.exists(tsv + ".bak"):
        shutil.copy2(tsv, tsv + ".bak.new")
        os.replace(tsv + ".bak.new", tsv + ".bak")
    os.replace(tmp, tsv)
    with open(log or tsv + ".probe-events.jsonl", "a") as fh:
        fh.write(json.dumps(event) + "\n")
    return True


def probe_table(tsv=TSV, state=None, log=None, min_age_days=14, probe=False,
                apply=False, periodic=False, can_probe=None, executable="codex",
                now=None, preserve_backup=False):
    """Shared CLI/maintenance entry point; the caller owns its run lock.

    Periodic callers sweep at most once per retry window, including after an
    upgrade derives fresh candidates. The budget is checked before EVERY call.
    A refused budget or missing executable does not consume an eligible tick.
    """
    state_p = state or tsv + ".probe-state.json"
    log_p = log or tsv + ".probe-events.jsonl"
    result = {"status": "dry-run", "results": {}, "changes": {}, "rc": 0}
    state = load_state(state_p)
    lines = read_rows(tsv)
    now = time.time() if now is None else now
    if periodic and now - state.get("_periodic_at", -float("inf")) < min_age_days * 86400:
        result["status"] = "recent"
        return result
    spending = probe or apply
    if spending and not shutil.which(os.environ.get("CODEX_PROBE_CMD") or executable):
        result["status"] = "missing-cli"
        return result
    if spending and can_probe is not None and not can_probe():
        result["status"] = "budget"
        return result
    if spending:
        result["status"] = "probed"
    changes = {}
    for i, f in codex_rows(lines):
        alias, slug = f[1], f[2]
        for cand in candidates(slug):
            last = state.get(cand)
            if last and now - last["at"] < min_age_days * 86400 and last["result"] != "ok":
                print("skip %s %s (probed %s, %s)" % (alias, cand, last["result"], "recent"))
                continue
            if not spending:
                print("candidate %s %s (dry run)" % (alias, cand))
                continue
            if can_probe is not None and not can_probe():
                result["status"] = "budget"
                continue
            # Persist before spending: a killed maintenance run must not spend
            # again on the next tick. A completed response replaces "error".
            if periodic:
                state["_periodic_at"] = now
            state[cand] = {"at": now, "result": "error"}
            save_state(state_p, state)
            res = run_probe(cand, executable)
            result["results"][cand] = res
            state[cand] = {"at": now, "result": res}
            save_state(state_p, state)
            print("probe %s %s -> %s" % (alias, cand, res))
            if res == "ok" and parse_slug(cand) > parse_slug(changes.get(alias, slug)):
                changes[alias] = cand
    # A partial sweep with a budget refusal or transport/auth error cannot
    # install even a successful candidate from earlier in the same sweep.
    if result["status"] == "budget" or "error" in result["results"].values():
        if result["status"] != "budget":
            result["status"] = "error"
        return result
    if not changes:
        return result
    for alias, cand in changes.items():
        print("NEWER %s -> %s (tier binding unmeasured: re-measure by hand)" % (alias, cand))
    if not apply:
        return result
    new = list(lines)
    for i, f in codex_rows(lines):
        if f[1] in changes:
            f[2], f[3] = changes[f[1]], display_for(changes[f[1]])
            new[i] = "\t".join(f)
    if not apply_display_table(tsv, new, lambda path: validate(path, changes),
                               {"at": now, "changes": changes,
                                "binding_status": "unmeasured"}, log_p,
                               preserve_backup=preserve_backup):
        result.update(status="validation-failed", rc=1)
        return result
    print("APPLIED %s (rollback: %s.bak)" % (changes, tsv))
    result.update(status="applied", changes=changes, binding_status="unmeasured")
    return result


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsv", default=TSV)
    ap.add_argument("--state", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--min-age-days", type=float, default=14)
    ap.add_argument("--probe", action="store_true", help="spend tokens on real probes")
    ap.add_argument("--apply", action="store_true", help="probe and rewrite the table")
    return probe_table(**vars(ap.parse_args(argv)))["rc"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
