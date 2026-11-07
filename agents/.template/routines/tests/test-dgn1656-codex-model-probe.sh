#!/usr/bin/env bash
# test-covers: agents/.template/routines/codex-model-probe.py agents/.template/routines/cli-updater.py
# test-lane: fast    # measured 1s (2026-10-12, cut-prep2), fake probe command, no network
# test-dgn1656-codex-model-probe.sh -- selftest: a newer codex generation is
# found from the table alone, classified, applied safely, and rolled back on a
# bad write. Uses a fake probe command; spends no tokens.
set -u
R="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/lib"; cp "$R/lib/model_display.py" "$T/lib/"
printf 'codex\topus\tgpt-6.1-sol\tGPT 6.1 Sol\ncodex\thaiku\tgpt-6-luna\tGPT 6 Luna\ncodex\tsonnet\tgpt-5.6-terra\tGPT 5.6 Terra\n' > "$T/t.tsv"
cat > "$T/fake.sh" <<'F'
#!/bin/sh
case "$1" in
  gpt-6.2-sol) echo "hi" ;;
  gpt-7-sol) echo "The 'gpt-7-sol' model is not supported when using Codex with a ChatGPT account."; exit 1 ;;
  gpt-6.1-luna) echo "model does not exist"; exit 1 ;;
  *) echo "model not found"; exit 1 ;;
esac
F
chmod +x "$T/fake.sh"
P="python3 $R/codex-model-probe.py --tsv $T/t.tsv"
fail=0; chk(){ if ! grep -q -- "$2" "$1"; then echo "FAIL: $3"; fail=1; fi; }
$P > "$T/o1"; chk "$T/o1" "candidate opus gpt-6.2-sol" "derived minor candidate"; chk "$T/o1" "candidate opus gpt-7-sol" "derived major candidate"
chk "$T/o1" "candidate haiku gpt-6.1-luna" "luna minor bump"
CODEX_PROBE_CMD="$T/fake.sh" $P --apply > "$T/o2"
chk "$T/o2" "gpt-7-sol -> tier_blocked" "tier_blocked classified"
chk "$T/o2" "gpt-6.1-luna -> not_found" "not_found classified"
chk "$T/o2" "binding unmeasured" "binding caveat reported"
chk "$T/t.tsv" "gpt-6.2-sol	GPT 6.2 Sol" "table rewritten"
chk "$T/t.tsv.bak" "gpt-6.1-sol" "rollback copy kept"
chk "$T/t.tsv.probe-events.jsonl" "gpt-6.2-sol" "change logged"
# second run inside min-age: blocked candidates are not re-probed
CODEX_PROBE_CMD="$T/fake.sh" $P --probe > "$T/o3"
if grep -q "^probe .*gpt-7-sol" "$T/o3"; then echo "FAIL: re-probed a recent refusal"; fail=1; fi
# validation failure leaves the table untouched
printf 'codex\topus\tgpt-6.1-sol\tGPT 6.1 Sol\n' > "$T/u.tsv"
printf '#!/bin/sh\necho hi\n' > "$T/ok.sh"; chmod +x "$T/ok.sh"
cp "$T/u.tsv" "$T/u.before"
echo 'def tsv_row(*a, **k): return None' > "$T/lib/model_display.py"
cp "$R/codex-model-probe.py" "$T/codex-model-probe.py"
CODEX_PROBE_CMD="$T/ok.sh" python3 "$T/codex-model-probe.py" --tsv "$T/u.tsv" --apply > "$T/o4"
chk "$T/o4" "REFUSED" "bad validation refused"
cmp -s "$T/u.tsv" "$T/u.before" || { echo "FAIL: table changed after refusal"; fail=1; }
# Exercise the actual maintenance entry point with isolated files and only
# CODEX_PROBE_CMD stubs. Empty PATH prevents accidental real CLI calls;
# no alert root means no inbox, worklog, push or schedule is touched.
python3 - "$R" "$T" <<'PYTEST'
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

routines, scratch = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("probe", routines / "codex-model-probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
EPOCH = 1800000000
WINDOW = 14 * 86400
ORIGINAL = ("# table fixture\n"
            "claude-code\topus\topus\tClaude Opus\tchat\n"
            "codex\topus\tgpt-6.1-sol\tGPT 6.1 Sol\tchat\n")


def fixture(name):
    root = scratch / name
    root.mkdir()
    (root / "state").mkdir()
    (root / "state" / "armed").touch()
    (root / "display.tsv").write_text(ORIGINAL)
    (root / "tier.tsv").write_text("codex\tcoverage\topus\tmeasured sentinel\n")
    (root / "exec.tsv").write_text("codex\tcodex\n")
    stub = root / "probe.sh"
    stub.write_text('''#!/bin/sh
printf '%s\n' "$1" >> "$PROBE_CALLS"
case "$PROBE_RESULT:$1" in
  success:gpt-6.2-sol|all_ok:*) echo hi ;;
  success:*) echo "not supported when using Codex with a ChatGPT account"; exit 1 ;;
  error:gpt-6.2-sol) echo hi ;;
  error:*) echo "transport error"; exit 1 ;;
  limit:*) echo "You've hit your usage limit" ;;
  missing:*) echo "unknown model"; exit 1 ;;
  *) exit 2 ;;
esac
''')
    stub.chmod(0o755)
    return root


def environment(root, outcome="success", now=EPOCH):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLI_UPDATER_") and k != "CODEX_PROBE_CMD"}
    env.update(PATH="", CODEX_PROBE_CMD=str(root / "probe.sh"),
               PROBE_CALLS=str(root / "calls"), PROBE_RESULT=outcome,
               CLI_UPDATER_NOW=str(now), CLI_UPDATER_BUDGET_SECS="1000",
               CLI_UPDATER_DISPLAY_TSV=str(root / "display.tsv"),
               CLI_UPDATER_TIER_TSV=str(root / "tier.tsv"),
               CLI_UPDATER_EXEC_TSV=str(root / "exec.tsv"))
    for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_ACTIVATOR", "CLAUDE_VERSIONS",
                "CLAUDE_JSON", "CLAUDE_PROJECTS", "CODEX_HOME", "CODEX_ACTIVATOR"):
        env["CLI_UPDATER_" + key] = str(root / key.lower())
    return env


def tick(root, outcome="success", now=EPOCH, mode="install", **overrides):
    env = environment(root, outcome, now)
    env.update(overrides)
    run = subprocess.run([sys.executable, str(routines / "cli-updater.py"),
                          "--mode", mode, "--state-dir", str(root / "state")],
                         env=env, capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
    reports = list((root / "state" / "runs").glob("*.json"))
    report = json.loads(max(reports, key=lambda p: p.stat().st_mtime_ns).read_text())
    assert (root / "tier.tsv").read_text() == "codex\tcoverage\topus\tmeasured sentinel\n"
    return report


def calls(root):
    p = root / "calls"
    return p.read_text().splitlines() if p.exists() else []


root = fixture("maintenance-success")
report = tick(root)
result = report["probe"]["codex_maintenance"]
assert result["status"] == "applied", report
assert result["binding_status"] == "unmeasured"
assert calls(root) == ["gpt-6.2-sol", "gpt-7-sol"]  # one sweep, no legacy loop
assert (root / "display.tsv").read_text() == ORIGINAL.replace("6.1", "6.2")
assert (root / "display.tsv.bak").read_text() == ORIGINAL
log = root / "display.tsv.probe-events.jsonl"
events = log.read_text().splitlines()
assert len(events) == 1 and json.loads(events[0])["binding_status"] == "unmeasured"
assert report["owner"]["models"] == ["GPT 6.2 Sol"]
for now in (EPOCH, EPOCH + 1, EPOCH + WINDOW - 1):
    assert tick(root, now=now)["probe"]["codex_maintenance"]["status"] == "recent"
assert len(calls(root)) == 2 and log.read_text().splitlines() == events
# At exactly 14 days, new candidates derive from the updated row; refused
# candidates are retried and a successful newer major still keeps its family.
report = tick(root, outcome="all_ok", now=EPOCH + WINDOW)
assert calls(root)[2:] == ["gpt-6.3-sol", "gpt-7-sol"]
assert report["probe"]["codex_maintenance"]["changes"] == {"opus": "gpt-7-sol"}
assert "gpt-7-sol\tGPT 7 Sol\tchat" in (root / "display.tsv").read_text()
assert len(log.read_text().splitlines()) == 2
print("PASS maintenance cadence, derived upgrades, backup/event, untouched bindings")

for mode, armed in (("observe", True), ("install", False), ("agents", True)):
    root = fixture("inactive-%s-%s" % (mode, armed))
    if not armed:
        (root / "state" / "armed").unlink()
    tick(root, mode=mode)
    assert not calls(root) and (root / "display.tsv").read_text() == ORIGINAL
    assert not (root / "display.tsv.probe-state.json").exists()

root = fixture("budget")
assert tick(root, CLI_UPDATER_BUDGET_SECS="-1")["probe"]["codex_maintenance"]["status"] == "budget"
assert not calls(root) and (root / "display.tsv").read_text() == ORIGINAL
assert not (root / "display.tsv.probe-state.json").exists()
assert tick(root)["probe"]["codex_maintenance"]["status"] == "applied"
root = fixture("missing-cli")
assert tick(root, CODEX_PROBE_CMD="")["probe"]["codex_maintenance"]["status"] == "missing-cli"
assert not calls(root) and (root / "display.tsv").read_text() == ORIGINAL
assert not (root / "display.tsv.probe-state.json").exists()
assert tick(root)["probe"]["codex_maintenance"]["status"] == "applied"
print("PASS observe/unarmed/agents, budget refusal, missing CLI, later eligibility")

# Account cache discovery still reports available models without applying or
# probing, and the independent Claude per-version cache still suppresses calls.
root = fixture("cache-only")
(root / "codex_home").mkdir()
(root / "codex_home" / "models_cache.json").write_text(json.dumps({"models": [
    {"slug": "gpt-7-sol"}, {"slug": "gpt-7-nova"},
    {"slug": "gpt-5-sol"}, {"slug": "gpt-8-sol", "visibility": "hide"}]}))
report = tick(root, mode="observe")
assert sorted(report["owner"]["models"]) == ["GPT 7 Nova", "GPT 7 Sol"]
assert not calls(root) and (root / "display.tsv").read_text() == ORIGINAL
root = fixture("claude-cache")
(root / "claude_activator").symlink_to(root / "2.1.284")
(root / "state" / "probe-claude-2.1.284.json").write_text(
    json.dumps({"opus": "claude-opus-5-5"}))
# The activator deliberately does not exist: the cached id needs no CLI turn.
report = tick(root)
assert report["probe"]["claude"] == {"opus": "claude-opus-5-5"}
assert "Claude Opus 5.5" in report["owner"]["models"]
assert not report["probe"]["inconclusive"]
print("PASS account cache-only discovery and Claude version cache preserved")

for outcome in ("error", "limit", "missing"):
    root = fixture("failure-" + outcome)
    report = tick(root, outcome=outcome)
    assert (root / "display.tsv").read_text() == ORIGINAL
    assert not (root / "display.tsv.bak").exists()
    assert not (root / "display.tsv.probe-events.jsonl").exists()
    result = report["probe"]["codex_maintenance"]
    assert result["status"] == ("probed" if outcome == "missing" else "error"), result
    count = len(calls(root))
    tick(root, outcome=outcome, now=EPOCH + 1)
    assert len(calls(root)) == count
# Existing per-candidate history also gates a first maintenance sweep.
root = fixture("candidate-history")
state = {slug: {"at": EPOCH, "result": result} for slug, result in
         (("gpt-6.2-sol", "not_found"), ("gpt-7-sol", "tier_blocked"))}
(root / "display.tsv.probe-state.json").write_text(json.dumps(state))
tick(root, now=EPOCH + WINDOW - 1)
assert not calls(root)
tick(root, now=EPOCH + WINDOW)
assert len(calls(root)) == 2
print("PASS mixed success/error, rc-zero usage limit, refusal cache and exact expiry")

# The shared budget is consulted before every call, including mid-sweep.
root = fixture("mid-budget")
with patch.dict(os.environ, environment(root)):
    checks = iter((True, True, False))
    result = probe.probe_table(str(root / "display.tsv"), apply=True, periodic=True,
                               now=EPOCH, can_probe=lambda: next(checks))
assert result["status"] == "budget" and len(calls(root)) == 1
assert (root / "display.tsv").read_text() == ORIGINAL
assert not (root / "display.tsv.bak").exists()

# Assert the old table is present until a single rename replaces it.
root = fixture("atomic")
replace = os.replace
replacements = []
def observed_replace(src, dst):
    if str(dst) == str(root / "display.tsv"):
        assert (root / "display.tsv").read_text() == ORIGINAL
        assert (root / "display.tsv.bak").read_text() == ORIGINAL
        replacements.append((src, dst))
    return replace(src, dst)
with patch.dict(os.environ, environment(root)), patch.object(probe.os, "replace", observed_replace):
    result = probe.probe_table(str(root / "display.tsv"), apply=True)
assert result["status"] == "applied" and len(replacements) == 1
assert probe.classify(0, "unknown model") == "not_found"
assert probe.classify(0, "") == "error"
assert probe.classify(0, "not authenticated") == "error"
print("PASS per-call budget, atomic table replacement, conservative error classification")
PYTEST
[ $? = 0 ] || fail=1
[ $fail = 0 ] && echo "PASS dgn1656"; exit $fail
