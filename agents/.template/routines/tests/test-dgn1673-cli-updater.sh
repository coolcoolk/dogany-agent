#!/bin/bash
# test-lane: slow    # measured 34s (2026-09-30, DGN-1673 r2): the queue cases wait on real subprocess timeouts
# test-dgn1673-cli-updater.sh -- fixture tests for the nightly external CLI
# updater (routines/cli-updater.py) and its dawn-memory-queue pre-step.
#
# Everything runs against fixtures: a fake native versions dir + fake
# activator symlink, fake claude/codex scripts (install / --version / one
# stream-json turn / codex exec), a registry fixture with fixed publish times,
# a scratch alert root (ledger + session-inbox) and a scratch HOME for the
# queue runner. No real binary, network, launchd or bridge is touched.
#
# Cases:
#   U1  newer claude published >= 24h -> installed, smoke ok, symlink moved,
#       previous version file kept, ok alert in ledger + inbox, CHANGED=1
#   U2  smoke fails -> REAL rollback: symlink restored to previous, FAIL alert
#       in the seam, CHANGED=0, rc 1
#   U3  smoke fails AND the installer pruned the previous file -> rollback
#       restores it from the keep hardlink
#   U4  newest release published < 24h -> no install call, symlink unchanged
#   U5  unpublished version (in "time", absent from "versions") never picked
#   U6  probe mismatch (claude alias id + codex upgrade candidate) -> alert
#       only, display table byte-identical
#   U7  not armed -> observe: no install, "observe mode" alert
#   U8  vendor autoUpdates=true -> no install, skip alert, flag untouched
#   U9  registry lookup failure -> no install, lookup-failed alert
#   U10 runtime without executable row -> watch alert (derived target set)
#   U11 bridge CLAUDE_CLI_PATH pinned elsewhere -> watch alert
#   U12 identical watch alert is not re-delivered on the next run (dedupe)
#   U13 codex pin holds; not armed -> observe (r2 superseded r1's
#       "codex observe-only": dec-166 apply path now exists)
#   r2 (DGN-1673 round 2):
#   P1  classifier units: usage/rate/auth text -> inconclusive regardless of
#       rc; rc 0 + ERROR line -> fail; healthy claude stream (allowed
#       rate_limit_event, skill text) -> ok; claude is_error limit -> inconclusive
#   P2  usage-limited probes -> inconclusive, no "NOT available", no model
#       change from a failed turn's init id, zero owner pushes
#   P3  codex account model list (models_cache.json) finds new slugs without
#       spending a turn; Korean owner draft lists them
#   C1  codex armed install: explicit --release, current repointed, smoke ok
#       -> kept, old release + state snapshot kept, auth.json not snapshotted
#   C2  codex smoke fail after a state-migrating install -> REAL rollback:
#       current back, state byte-identical, new file quarantined, auth.json
#       untouched, exactly one Korean owner push
#   C3  codex exec usage-limited -> new version kept + watch alert, no push
#   C4  installer fails before touching anything -> no owner push
#   C5  codex process running -> skipped, nothing touched
#   N1  claude rollback -> one owner push; gated by --owner-push AND the
#       owner-push-armed file (either missing -> draft only)
#   N2  routine success (both CLIs) -> zero owner pushes
#   N3  new claude model -> one push, never repeated
#   A1  agent bridge model opus-5 -> opus-5-5 -> one push naming the agent;
#       unchanged -> none; family switch -> leader queue only
#   A2  restarted bridge with no turn -> bounded wait, watch alert
#   A3  the regular pre-step run also checks agents
#   D1  queue --dry-run prints the pre-step before the instance plan
#   D2  queue run: pre-step runs before the first consolidate; pre-step rc!=0
#       does not block consolidate; verdict OK, prestep=<rc>
#   D3  pre-step timeout-kill -> consolidate still runs
#   D4  pre-step script missing -> consolidate still runs
#   D5  post-step: restart only with --live + restart-armed + CHANGED=1, via
#       self_restart.sh --trigger auto, after every consolidate; then the
#       agent model check (--mode agents --wait-secs), --owner-push on --live
#   D6  end-to-end: queue --live with the REAL updater on fixtures installs,
#       consolidates, then restarts
#
# Run: bash routines/tests/test-dgn1673-cli-updater.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UPDATER="$ROUTINES_DIR/cli-updater.py"
DMQ="$ROUTINES_DIR/dawn-memory-queue.sh"
REAL_DISPLAY="$ROUTINES_DIR/lib/dispatch-model-display.tsv"

PASS=0
FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
assert_eq() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 -- got='$2' want='$3'"; fi
}
assert_has() {   # desc file-or-text needle
  if grep -qF -- "$3" "$2" 2>/dev/null; then ok "$1"; else fail "$1 -- '$3' not in $2"; fi
}
assert_not_has() {
  if grep -qF -- "$3" "$2" 2>/dev/null; then fail "$1 -- '$3' found in $2"; else ok "$1"; fi
}

BASE="$(mktemp -d /tmp/dgn1673-test.XXXXXX)"
trap 'rm -rf "$BASE"' EXIT
REAL_DISPLAY_SHA="$(shasum "$REAL_DISPLAY" | cut -d' ' -f1)"

# Pinned data tables (hermetic): the cases assert on the codex/claude slugs
# below, so they must not follow the live routines/lib tables -- a re-probed
# row there (e.g. the codex opus slot moving to a new slug) would otherwise
# turn into a fake probe mismatch here. The live display table is only read
# for the U6 "repo table byte-identical" guard.
FX_TABLES="$BASE/tables"
mkdir -p "$FX_TABLES"
printf '%s\t%s\t%s\t%s\n' \
  claude-code fable fable 'Claude Fable 5.1' \
  claude-code opus opus 'Claude Opus 5.5' \
  claude-code sonnet sonnet 'Claude Sonnet 5.5' \
  claude-code haiku haiku 'Claude Haiku 4.5' \
  codex fable gpt-6-astra 'GPT 6 Astra' \
  codex opus gpt-6-sol 'GPT 6 Sol' \
  codex sonnet gpt-5.6-terra 'GPT 5.6 Terra' \
  codex haiku gpt-6-luna 'GPT 6 Luna' \
  cloud fable claude-fable-5-1 'Claude Fable 5.1' \
  cloud opus claude-opus-5-5 'Claude Opus 5.5' \
  cloud sonnet claude-sonnet-5 'Claude Sonnet 5' \
  cloud haiku claude-haiku-4-5-20251001 'Claude Haiku 4.5' \
  > "$FX_TABLES/display.tsv"
printf '%s\t%s\t%s\t%s\n' \
  claude-code coverage opus,fable 'fixture row' \
  claude-code accuracy sonnet,opus 'fixture row' \
  claude-code judgment opus,fable 'fixture row' \
  codex coverage fable,opus 'fixture row' \
  codex accuracy fable 'fixture row' \
  cloud coverage opus,fable 'fixture row' \
  cloud accuracy sonnet,opus 'fixture row' \
  cloud judgment opus,fable 'fixture row' \
  > "$FX_TABLES/tier.tsv"
printf '%s\t%s\t%s\n' \
  claude-code claude 'fixture row' \
  codex codex 'fixture row' \
  cloud claude 'fixture row' \
  > "$FX_TABLES/exec.tsv"

# NOW = 2026-09-30T00:00:00Z
NOW=1790726400

# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------
write_fake_claude() {  # dest version versions_dir activator calls_log
  cat > "$1" <<EOF
#!/bin/bash
V="$2"
VERS="$3"
ACT="$4"
CALLS="$5"
EOF
  cat >> "$1" <<'EOF'
echo "$V $*" >> "$CALLS"
case "$1" in
  --version) echo "$V (Claude Code)"; exit 0 ;;
  install)
    new="$2"
    sed "s/^V=.*/V=\"$new\"/" "$VERS/$V" > "$VERS/$new"
    chmod +x "$VERS/$new"
    if [[ "${FAKE_PRUNE_OLD:-0}" == "1" ]]; then rm -f "$VERS/$V"; fi
    ln -sfn "$VERS/$new" "$ACT"
    echo "Claude Code $new installed"
    exit 0 ;;
  -p)
    alias=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "--model" ]]; then alias="$2"; shift; fi
      shift
    done
    if [[ ",${FAKE_SMOKE_FAIL:-}," == *",$V,"* ]]; then echo "fake API error" >&2; exit 1; fi
    if [[ ",${FAKE_CLAUDE_LIMIT_ALIASES:-}," == *",$alias,"* ]]; then
      # real shape: init names a model, the result is an error, rc 1
      echo '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}'
      echo '{"type":"system","subtype":"init","model":"claude-sonnet-9"}'
      echo '{"type":"result","subtype":"success","is_error":true,"result":"You have hit your limit - resets 5pm"}'
      exit 1
    fi
    case "$alias" in
      opus)   id="${FAKE_ID_OPUS:-claude-opus-5-5}" ;;
      sonnet) id="claude-sonnet-5-5" ;;   # measured on 2.1.284 (2026-09-30)
      haiku)  id="claude-haiku-4-5-20251001" ;;
      fable)  id="claude-fable-5-1" ;;
      *)      id="claude-unknown-1" ;;
    esac
    echo '{"type":"system","subtype":"init","model":"'"$id"'"}'
    echo '{"type":"assistant","message":{"model":"'"$id"'","content":[{"type":"text","text":"hi"}]}}'
    echo '{"type":"result","subtype":"success"}'
    exit 0 ;;
esac
exit 0
EOF
  chmod +x "$1"
}

write_fake_codex() {  # dest version calls_log
  cat > "$1" <<EOF
#!/bin/bash
V="$2"
CALLS="$3"
EOF
  cat >> "$1" <<'EOF'
if [[ "$1" == "--version" ]]; then echo "codex-cli $V"; exit 0; fi
echo "$*" >> "$CALLS"
m=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-m" ]]; then m="$2"; shift; fi
  shift
done
if [[ "${FAKE_CODEX_USAGE_LIMIT:-0}" == "1" ]]; then
  # measured 2026-09-30 wording; rc may be 0 through a pipeline
  echo "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Oct 4th, 2026 9:00 AM." >&2
  exit "${FAKE_CODEX_USAGE_RC:-1}"
fi
if [[ -z "$m" ]]; then
  if [[ ",${FAKE_CODEX_SMOKE_FAIL:-}," == *",$V,"* ]]; then echo "ERROR: stream disconnected before completion" >&2; exit 1; fi
  echo hi; exit 0
fi
if [[ ",${FAKE_CODEX_AVAILABLE:-}," == *",$m,"* ]]; then echo hi; exit 0; fi
echo "ERROR: model $m not available" >&2; exit 1
EOF
  chmod +x "$1"
}

write_fake_codex_installer() {  # dest -- emulates install.sh --release V
  cat > "$1" <<'EOF'
#!/bin/sh
echo "argv=$* nonint=$CODEX_NON_INTERACTIVE home=$CODEX_HOME dir=$CODEX_INSTALL_DIR" >> "$FAKE_INSTALL_LOG"
[ "$1" = "--release" ] || exit 1
V="$2"
[ "${FAKE_CODEX_INSTALL_FAIL:-0}" = "1" ] && { echo "Could not download" >&2; exit 1; }
SR="$CODEX_HOME/packages/standalone"
REL="$SR/releases/$V-aarch64-apple-darwin"
mkdir -p "$REL/bin"
sed "s/^V=.*/V=\"$V\"/" "$SR/current/bin/codex" > "$REL/bin/codex"
chmod +x "$REL/bin/codex"
printf '{"version": "%s"}\n' "$V" > "$REL/codex-package.json"
ln -s "$REL" "$SR/.current.$$" && mv -hf "$SR/.current.$$" "$SR/current"
rm -f "$SR/auto-update-version"
ln -sfn "$SR/current/bin/codex" "$CODEX_INSTALL_DIR/codex"
if [ "${FAKE_CODEX_MIGRATE:-0}" = "1" ]; then
  printf 'migrated-by-%s' "$V" >> "$CODEX_HOME/state_5.sqlite"
  printf 'new-schema' > "$CODEX_HOME/state_6.sqlite"
fi
echo "Codex CLI $V installed successfully."
EOF
  chmod +x "$1"
}

# new_fixture NAME INSTALLED_VERSION -> sets F (fixture root) and exports env
new_fixture() {
  F="$BASE/$1"
  local h="$F/home" ver="$2"
  mkdir -p "$h/.local/bin" "$h/.local/share/claude/versions" "$F/state" \
           "$F/alert/worklog" "$F/alert/.telegram_bot/session-inbox" "$F/reg"
  write_fake_claude "$h/.local/share/claude/versions/$ver" "$ver" \
    "$h/.local/share/claude/versions" "$h/.local/bin/claude" "$F/claude-calls.log"
  ln -s "$h/.local/share/claude/versions/$ver" "$h/.local/bin/claude"
  echo '{"autoUpdates": false, "installMethod": "native"}' > "$h/.claude.json"
  # codex standalone layout
  local rel="$h/.codex/packages/standalone/releases/0.156.1-aarch64-apple-darwin"
  mkdir -p "$rel/bin"
  write_fake_codex "$rel/bin/codex" 0.156.1 "$F/codex-calls.log"
  echo '{"version": "0.156.1"}' > "$rel/codex-package.json"
  ln -s "$rel" "$h/.codex/packages/standalone/current"
  ln -s "$h/.codex/packages/standalone/current/bin/codex" "$h/.local/bin/codex"
  printf 'state-v5-original' > "$h/.codex/state_5.sqlite"
  printf 'wal-original' > "$h/.codex/logs_2.sqlite-wal"
  printf 'model = "x"\n' > "$h/.codex/config.toml"
  printf 'AUTH-ORIGINAL-TOKENS' > "$h/.codex/auth.json"
  printf '0.156.1-aarch64-apple-darwin' > "$h/.codex/packages/standalone/auto-update-version"
  write_fake_codex_installer "$F/codex-install.sh"
  mkdir -p "$F/alert/config" "$h/.claude/projects"
  echo "AGENT_LANG=ko" > "$F/alert/config/agent.conf"
  printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s/push.log"\n' "$F" > "$F/fake-push.sh"
  chmod +x "$F/fake-push.sh"
  cp "$FX_TABLES/display.tsv" "$F/display.tsv"
  cp "$FX_TABLES/tier.tsv" "$F/tier.tsv"
  cp "$FX_TABLES/exec.tsv" "$F/exec.tsv"
  # registry: 283 old, 284 48h old (eligible), 285 12h old (cooling),
  # 286 in time but unpublished (absent from versions)
  cat > "$F/reg/claude.json" <<'EOF'
{"time": {"created": "2025-02-24T00:00:00.000Z", "modified": "2026-09-29T12:00:00.000Z",
          "2.1.283": "2026-09-26T00:00:00.000Z",
          "2.1.284": "2026-09-28T00:00:00.000Z",
          "2.1.285": "2026-09-29T12:00:00.000Z",
          "2.1.286": "2026-09-28T06:00:00.000Z",
          "2.2.0-beta.1": "2026-09-20T00:00:00.000Z"},
 "versions": {"2.1.283": {}, "2.1.284": {}, "2.1.285": {}, "2.2.0-beta.1": {}}}
EOF
  cat > "$F/reg/codex.json" <<'EOF'
{"time": {"0.156.1": "2026-09-23T00:00:00.000Z", "0.157.0": "2026-09-27T00:00:00.000Z"},
 "versions": {"0.156.1": {}, "0.157.0": {}}}
EOF
  export CLI_UPDATER_CLAUDE_ACTIVATOR="$h/.local/bin/claude"
  export CLI_UPDATER_CLAUDE_VERSIONS="$h/.local/share/claude/versions"
  export CLI_UPDATER_CLAUDE_JSON="$h/.claude.json"
  export CLI_UPDATER_CODEX_HOME="$h/.codex"
  export CLI_UPDATER_REGISTRY_FIXTURE_DIR="$F/reg"
  export CLI_UPDATER_DISPLAY_TSV="$F/display.tsv"
  export CLI_UPDATER_TIER_TSV="$F/tier.tsv"
  export CLI_UPDATER_EXEC_TSV="$F/exec.tsv"
  export CLI_UPDATER_NOW="$NOW"
  export FAKE_CODEX_AVAILABLE="gpt-6-astra,gpt-6-sol,gpt-5.6-terra,gpt-6-luna"
  export CLI_UPDATER_CODEX_ACTIVATOR="$h/.local/bin/codex"
  export CLI_UPDATER_CODEX_INSTALLER="$F/codex-install.sh"
  export CLI_UPDATER_CODEX_BUSY_CMD="false"
  export CLI_UPDATER_CLAUDE_PROJECTS="$h/.claude/projects"
  export CLI_UPDATER_PUSH="$F/fake-push.sh"
  export FAKE_INSTALL_LOG="$F/codex-install.log"
  unset FAKE_SMOKE_FAIL FAKE_PRUNE_OLD FAKE_ID_OPUS \
        FAKE_CODEX_USAGE_LIMIT FAKE_CODEX_USAGE_RC \
        FAKE_CODEX_SMOKE_FAIL FAKE_CODEX_INSTALL_FAIL FAKE_CODEX_MIGRATE \
        FAKE_CLAUDE_LIMIT_ALIASES DOGANY_LANG 2>/dev/null || true
  FPATH="$h/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
}

run_updater() {  # extra args...
  PATH="$FPATH" python3 "$UPDATER" --state-dir "$F/state" --alert-root "$F/alert" "$@" \
    > "$F/out.log" 2>&1
  echo $?
}

link_of() { readlink "$CLI_UPDATER_CLAUDE_ACTIVATOR"; }
inbox_all() { cat "$F"/alert/.telegram_bot/session-inbox/*.md 2>/dev/null > "$F/inbox.txt"; echo "$F/inbox.txt"; }
ledger() { echo "$F/alert/worklog/_CRON_QUEUE_LEDGER.md"; }

# ---------------------------------------------------------------------------
echo "U1: eligible newer claude -> install, smoke ok, symlink moved, old kept"
new_fixture u1 2.1.283
touch "$F/state/armed"
rc="$(run_updater --mode install)"
V="$CLI_UPDATER_CLAUDE_VERSIONS"
assert_eq "U1 rc" "$rc" "0"
assert_eq "U1 symlink moved to 2.1.284" "$(link_of)" "$V/2.1.284"
[[ -x "$V/2.1.283" ]] && ok "U1 previous version file kept" || fail "U1 previous version file kept"
[[ -f "$F/state/keep/claude/2.1.283" ]] && ok "U1 keep hardlink present" || fail "U1 keep hardlink present"
assert_has "U1 install called with the cooled version" "$F/claude-calls.log" "2.1.283 install 2.1.284"
assert_has "U1 smoke used the fixed prompt + opus" "$F/claude-calls.log" "2.1.284 -p just answer me hi --model opus"
assert_has "U1 ok alert in ledger" "$(ledger)" "claude 2.1.283 -> 2.1.284 installed"
assert_has "U1 ok alert in inbox" "$(inbox_all)" "smoke ok: opus = claude-opus-5-5"
assert_has "U1 last-run CHANGED=1" "$F/state/last-run.env" "CLAUDE_CHANGED=1"
assert_has "U1 last-run TO" "$F/state/last-run.env" "CLAUDE_TO=2.1.284"
assert_not_has "U1 no probe mismatch" "$(inbox_all)" "table NOT edited"
assert_eq "U1 vendor autoUpdates untouched" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["autoUpdates"])' "$CLI_UPDATER_CLAUDE_JSON")" "False"
[[ -n "$(ls "$F/state/runs/"*.json 2>/dev/null)" ]] && ok "U1 run snapshot written" || fail "U1 run snapshot written"
assert_has "U1 snapshot records pre version" "$(ls "$F"/state/runs/*.json | head -1)" '"pre_version": "2.1.283"'

echo "U2: smoke fails -> real rollback on fixture"
new_fixture u2 2.1.283
touch "$F/state/armed"
export FAKE_SMOKE_FAIL=2.1.284
rc="$(run_updater --mode install)"
V="$CLI_UPDATER_CLAUDE_VERSIONS"
assert_eq "U2 rc=1" "$rc" "1"
assert_eq "U2 symlink restored to previous" "$(link_of)" "$V/2.1.283"
assert_eq "U2 activator runs the previous version" "$("$CLI_UPDATER_CLAUDE_ACTIVATOR" --version | cut -d' ' -f1)" "2.1.283"
assert_has "U2 FAIL alert in ledger" "$(ledger)" "[FAIL] claude 2.1.283 -> 2.1.284 smoke FAILED"
assert_has "U2 FAIL alert in inbox with proof" "$(inbox_all)" "readlink == previous: yes"
assert_has "U2 proof: version" "$F/inbox.txt" "--version == 2.1.283: yes"
assert_has "U2 last-run CHANGED=0" "$F/state/last-run.env" "CLAUDE_CHANGED=0"
assert_has "U2 action recorded" "$F/state/last-run.env" "CLAUDE_ACTION=smoke-failed-rolled-back"
unset FAKE_SMOKE_FAIL

echo "U3: smoke fails + installer pruned previous file -> restored from keep"
new_fixture u3 2.1.283
touch "$F/state/armed"
export FAKE_SMOKE_FAIL=2.1.284 FAKE_PRUNE_OLD=1
rc="$(run_updater --mode install)"
V="$CLI_UPDATER_CLAUDE_VERSIONS"
assert_eq "U3 rc=1" "$rc" "1"
assert_eq "U3 symlink restored" "$(link_of)" "$V/2.1.283"
[[ -x "$V/2.1.283" ]] && ok "U3 previous file restored from keep" || fail "U3 previous file restored from keep"
assert_has "U3 proof mentions keep copy" "$(inbox_all)" "restored from keep copy"
unset FAKE_SMOKE_FAIL FAKE_PRUNE_OLD

echo "U4: newest published < 24h -> no install"
new_fixture u4 2.1.284
touch "$F/state/armed"
rc="$(run_updater --mode install)"
assert_eq "U4 rc" "$rc" "0"
assert_eq "U4 symlink unchanged" "$(link_of)" "$CLI_UPDATER_CLAUDE_VERSIONS/2.1.284"
assert_not_has "U4 no install call" "$F/claude-calls.log" " install "
[[ ! -e "$CLI_UPDATER_CLAUDE_VERSIONS/2.1.285" ]] && ok "U4 2.1.285 (12h old) not installed" || fail "U4 2.1.285 not installed"
assert_has "U4 action up-to-date" "$F/state/last-run.env" "CLAUDE_ACTION=up-to-date"

echo "U5: unpublished 2.1.286 never picked (picker unit)"
got="$(cd "$ROUTINES_DIR" && python3 -c '
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("u", "cli-updater.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
doc = json.load(open(sys.argv[1]))
print(m.pick_cooled(doc, int(sys.argv[2]), 86400)[0])
' "$F/reg/claude.json" "$NOW")"
assert_eq "U5 cooled pick skips unpublished + prerelease" "$got" "2.1.284"

echo "U6: probe mismatch -> alert only, table unchanged"
new_fixture u6 2.1.284
touch "$F/state/armed"
export FAKE_ID_OPUS=claude-opus-5-6
export FAKE_CODEX_AVAILABLE="gpt-6-astra,gpt-6-sol,gpt-5.6-terra,gpt-6-luna,gpt-6-terra"
sha_before="$(shasum "$F/display.tsv" | cut -d' ' -f1)"
rc="$(run_updater --mode install)"
assert_eq "U6 rc" "$rc" "0"
assert_has "U6 claude alias mismatch alerted" "$(inbox_all)" "claude alias 'opus' resolves to claude-opus-5-6 (Claude Opus 5.6)"
assert_has "U6 codex upgrade candidate alerted" "$F/inbox.txt" "codex 'gpt-6-terra' is available; table has 'gpt-5.6-terra'"
assert_has "U6 alerts say table not edited" "$F/inbox.txt" "table NOT edited"
assert_eq "U6 fixture table byte-identical" "$(shasum "$F/display.tsv" | cut -d' ' -f1)" "$sha_before"
assert_eq "U6 repo table byte-identical" "$(shasum "$REAL_DISPLAY" | cut -d' ' -f1)" "$REAL_DISPLAY_SHA"
assert_has "U6 codex probe used the fixed prompt" "$F/codex-calls.log" "-m gpt-6-terra just answer me hi"
unset FAKE_ID_OPUS

echo "U7: not armed -> observe only"
new_fixture u7 2.1.283
rc="$(run_updater --mode install)"
assert_eq "U7 rc" "$rc" "0"
assert_eq "U7 symlink unchanged" "$(link_of)" "$CLI_UPDATER_CLAUDE_VERSIONS/2.1.283"
assert_not_has "U7 no install call" "$F/claude-calls.log" " install "
assert_not_has "U7 no API turn in observe" "$F/claude-calls.log" " -p "
assert_has "U7 observe alert names the reason" "$(inbox_all)" "not armed"
[[ ! -e "$F/codex-calls.log" ]] && ok "U7 no codex probe in observe" || fail "U7 no codex probe in observe"

echo "U8: vendor autoUpdates=true -> skip, flag untouched"
new_fixture u8 2.1.283
touch "$F/state/armed"
echo '{"autoUpdates": true}' > "$CLI_UPDATER_CLAUDE_JSON"
rc="$(run_updater --mode install)"
assert_eq "U8 symlink unchanged" "$(link_of)" "$CLI_UPDATER_CLAUDE_VERSIONS/2.1.283"
assert_has "U8 skip alert" "$(inbox_all)" "autoUpdates is ON"
assert_eq "U8 flag untouched" "$(cat "$CLI_UPDATER_CLAUDE_JSON")" '{"autoUpdates": true}'

echo "U9: registry lookup failure -> no install"
new_fixture u9 2.1.283
touch "$F/state/armed"
rm -f "$F/reg/claude.json"
rc="$(run_updater --mode install)"
assert_eq "U9 symlink unchanged" "$(link_of)" "$CLI_UPDATER_CLAUDE_VERSIONS/2.1.283"
assert_has "U9 lookup-failed alert" "$(inbox_all)" "registry lookup failed for claude"
assert_not_has "U9 no install call" "$F/claude-calls.log" " install "

echo "U10: runtime without executable row -> watch alert"
new_fixture u10 2.1.284
printf 'antigravity\tcoverage\topus\tfixture row\n' >> "$F/tier.tsv"
rc="$(run_updater --mode observe)"
assert_has "U10 unmapped runtime alerted" "$(inbox_all)" "runtime 'antigravity' is in dispatch-tier-bindings.tsv but has no row"
assert_has "U10 snapshot targets derived (claude runtimes)" "$(ls "$F"/state/runs/*.json | head -1)" '"claude-code"'

echo "U11: bridge CLAUDE_CLI_PATH pinned elsewhere -> watch alert"
new_fixture u11 2.1.284
mkdir -p "$F/inst/.telegram_bot"
echo "CLAUDE_CLI_PATH=/opt/other/claude" > "$F/inst/.telegram_bot/.env"
rc="$(run_updater --mode observe --instance-root "$F/inst")"
assert_has "U11 bridge pin alerted" "$(inbox_all)" "bridge bridge:inst pins CLAUDE_CLI_PATH=/opt/other/claude"

echo "U12: identical watch alert not re-delivered"
n1="$(ls "$F"/alert/.telegram_bot/session-inbox/*.md | wc -l | tr -d ' ')"
rc="$(run_updater --mode observe --instance-root "$F/inst")"
n2="$(ls "$F"/alert/.telegram_bot/session-inbox/*.md | wc -l | tr -d ' ')"
assert_eq "U12 no new inbox drop on repeat" "$n2" "$n1"
assert_has "U12 run reports 0 alerts" "$F/out.log" "alerts=0"

echo "U13: codex pin holds; not armed = observe (r2: codex apply exists, dec-166)"
new_fixture u13 2.1.284
rc="$(run_updater --mode install)"
assert_has "U13 not armed: codex eligible reported as observe" "$(inbox_all)" "codex 0.156.1 -> 0.157.0 is eligible"
[[ ! -e "$FAKE_INSTALL_LOG" ]] && ok "U13 not armed: installer never called" || fail "U13 not armed: installer never called"
touch "$F/state/armed"
echo "codex=0.156.1" > "$F/state/pins.conf"
rm -f "$F/state/alerts-seen.json"
rc="$(run_updater --mode install)"
assert_has "U13 pin satisfied -> snapshot pin" "$(ls -t "$F"/state/runs/*.json | head -1)" '"pin": "0.156.1"'
[[ ! -e "$FAKE_INSTALL_LOG" ]] && ok "U13 pin == installed: no install even armed" || fail "U13 pin == installed: no install"

# ---------------------------------------------------------------------------
# r2: classification, codex apply, owner notices, agent model check
# ---------------------------------------------------------------------------
sha() { shasum "$1" 2>/dev/null | cut -d' ' -f1; }
codex_current() { readlink "$CLI_UPDATER_CODEX_HOME/packages/standalone/current"; }
push_count() { [[ -f "$F/push.log" ]] && grep -c -- "--audience owner" "$F/push.log" || echo 0; }

echo "P1: classifier units (text, not rc)"
got="$(cd "$ROUTINES_DIR" && python3 -c '
import importlib.util
spec = importlib.util.spec_from_file_location("u", "cli-updater.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
lim = "ERROR: You have hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Oct 4th."
print(m.classify_turn(0, "", lim)[0])
print(m.classify_turn(1, "", lim)[1])
print(m.classify_turn(1, "", "ERROR: model gpt-9-x not available")[0])
print(m.classify_turn(0, "", "ERROR: stream disconnected")[0])
print(m.classify_turn(0, "hi", "")[0])
print(m.classify_turn(1, "", "Error: 429 Too Many Requests")[0])
print(m.classify_turn(1, "", "401 Unauthorized: please log in")[0])
ok = "\n".join([
 "{\"type\":\"rate_limit_event\",\"rate_limit_info\":{\"status\":\"allowed\"}}",
 "{\"type\":\"system\",\"subtype\":\"init\",\"model\":\"claude-opus-5-5\",\"skills\":\"login to a site; usage limit docs\"}",
 "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"hi\"}"])
t, clean = m.reduce_claude_stream(ok)
print(m.classify_turn(0, t, "", need_answer=False)[0], clean)
bad = "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":true,\"result\":\"Claude AI usage limit reached\"}"
t, clean = m.reduce_claude_stream(bad)
print(m.classify_turn(1, t, "", need_answer=False)[0], clean)
')"
assert_eq "P1 codex usage-limit text with rc 0 -> inconclusive" "$(sed -n 1p <<< "$got")" "inconclusive"
assert_eq "P1 reason carries the retry hint" "$(sed -n 2p <<< "$got")" "usage limit, try again at Oct 4th"
assert_eq "P1 model-not-available text -> unavailable" "$(sed -n 3p <<< "$got")" "unavailable"
assert_eq "P1 rc 0 + ERROR line -> fail (rc alone not trusted)" "$(sed -n 4p <<< "$got")" "fail"
assert_eq "P1 rc 0 + answer -> ok" "$(sed -n 5p <<< "$got")" "ok"
assert_eq "P1 429 -> inconclusive" "$(sed -n 6p <<< "$got")" "inconclusive"
assert_eq "P1 401 -> inconclusive" "$(sed -n 7p <<< "$got")" "inconclusive"
assert_eq "P1 healthy claude stream (allowed rate_limit_event, skill text) -> ok" "$(sed -n 8p <<< "$got")" "ok True"
assert_eq "P1 claude is_error usage limit -> inconclusive" "$(sed -n 9p <<< "$got")" "inconclusive False"

echo "P2: usage-limited probes -> inconclusive, never 'not available', no model change, no push"
new_fixture p2 2.1.284
touch "$F/state/armed" "$F/state/owner-push-armed" "$F/state/pins.conf"
echo "codex=0.156.1" > "$F/state/pins.conf"
export FAKE_CODEX_USAGE_LIMIT=1 FAKE_CODEX_USAGE_RC=0 FAKE_CLAUDE_LIMIT_ALIASES=sonnet
rc="$(run_updater --mode install --owner-push)"
inbox_all > /dev/null
assert_not_has "P2 no 'NOT available' anywhere" "$(ledger)" "NOT available"
assert_has "P2 codex table slug reported inconclusive" "$(ledger)" "codex 'gpt-6-sol' (table slug for opus) probe inconclusive (usage limit, try again at Oct 4th"
assert_has "P2 claude alias reported inconclusive" "$(ledger)" "claude alias 'sonnet' probe inconclusive (usage limit"
assert_not_has "P2 no alias mismatch from the failed turn's init id" "$(ledger)" "claude-sonnet-9"
assert_eq "P2 zero owner pushes" "$(push_count)" "0"
assert_not_has "P2 no owner draft either" "$(ledger)" "owner-draft"
unset FAKE_CODEX_USAGE_LIMIT FAKE_CODEX_USAGE_RC FAKE_CLAUDE_LIMIT_ALIASES

echo "P3: codex account model list (no turn spent) finds a new family slug"
new_fixture p3 2.1.284
cat > "$CLI_UPDATER_CODEX_HOME/models_cache.json" <<'EOF'
{"fetched_at": "2026-09-30T00:18:52Z", "client_version": "0.156.1", "models": [
 {"slug": "gpt-6-sol", "visibility": "list"}, {"slug": "gpt-5.6-sol", "visibility": "list"},
 {"slug": "gpt-6-terra", "visibility": "list"}, {"slug": "gpt-reserve", "visibility": "hide"},
 {"slug": "gpt-6-nova", "visibility": "list"}]}
EOF
rc="$(run_updater --mode observe)"
assert_has "P3 newer gen of a table family found from the list" "$(inbox_all)" "codex 'gpt-6-terra' is available; table has 'gpt-5.6-terra' for sonnet (offered to the account)"
assert_has "P3 new family found" "$F/inbox.txt" "codex 'gpt-6-nova' is available (offered to the account): a family the table does not have"
assert_not_has "P3 older gen of a table family is not news" "$F/inbox.txt" "gpt-5.6-sol"
assert_not_has "P3 hidden slug ignored" "$F/inbox.txt" "gpt-reserve"
[[ ! -e "$F/codex-calls.log" ]] && ok "P3 observe: no exec turn spent" || fail "P3 observe: no exec turn spent"
assert_has "P3 owner draft names both new models (list, Korean)" "$(ledger)" "새 모델이 나왔어요. / • GPT 6 Nova / • GPT 6 Terra"

echo "C1: codex armed install -> pinned version, current repointed, smoke ok -> kept"
new_fixture c1 2.1.284
touch "$F/state/armed"
CXH="$CLI_UPDATER_CODEX_HOME"
auth_before="$(sha "$CXH/auth.json")"
rc="$(run_updater --mode install)"
assert_eq "C1 rc" "$rc" "0"
assert_eq "C1 current -> releases/0.157.0" "$(codex_current)" "$CXH/packages/standalone/releases/0.157.0-aarch64-apple-darwin"
assert_eq "C1 activator --version" "$("$CLI_UPDATER_CODEX_ACTIVATOR" --version)" "codex-cli 0.157.0"
[[ -x "$CXH/packages/standalone/releases/0.156.1-aarch64-apple-darwin/bin/codex" ]] && ok "C1 previous release kept" || fail "C1 previous release kept"
assert_has "C1 installer got an explicit version, non-interactive" "$FAKE_INSTALL_LOG" "argv=--release 0.157.0 nonint=1 home=$CXH dir=$F/home/.local/bin"
assert_has "C1 smoke exec used the fixed prompt, default model" "$F/codex-calls.log" "exec --skip-git-repo-check --ephemeral -s read-only just answer me hi"
assert_eq "C1 auth.json untouched" "$(sha "$CXH/auth.json")" "$auth_before"
[[ -f "$F/state/codex-state-snapshot/files/state_5.sqlite" ]] && ok "C1 state snapshot taken" || fail "C1 state snapshot taken"
[[ ! -e "$F/state/codex-state-snapshot/files/auth.json" ]] && ok "C1 auth.json NOT in the snapshot" || fail "C1 auth.json NOT in the snapshot"
assert_has "C1 ok alert" "$(ledger)" "codex 0.156.1 -> 0.157.0 installed (published >= 24h ago), smoke ok"
assert_has "C1 last-run CODEX_ACTION" "$F/state/last-run.env" "CODEX_ACTION=installed"

echo "C2: codex smoke fails after a state-migrating install -> REAL rollback, state byte-identical"
new_fixture c2 2.1.284
touch "$F/state/armed" "$F/state/owner-push-armed"
CXH="$CLI_UPDATER_CODEX_HOME"
auth_before="$(sha "$CXH/auth.json")"; auth_mtime="$(stat -f %m "$CXH/auth.json")"
s5_before="$(sha "$CXH/state_5.sqlite")"; wal_before="$(sha "$CXH/logs_2.sqlite-wal")"
auv_before="$(cat "$CXH/packages/standalone/auto-update-version")"
pre_link="$(codex_current)"
export FAKE_CODEX_SMOKE_FAIL=0.157.0 FAKE_CODEX_MIGRATE=1
rc="$(run_updater --mode install --owner-push)"
assert_eq "C2 rc=1" "$rc" "1"
assert_eq "C2 current back to the old release" "$(codex_current)" "$pre_link"
assert_eq "C2 activator runs the old version" "$("$CLI_UPDATER_CODEX_ACTIVATOR" --version)" "codex-cli 0.156.1"
assert_eq "C2 state_5.sqlite byte-identical" "$(sha "$CXH/state_5.sqlite")" "$s5_before"
assert_eq "C2 wal byte-identical" "$(sha "$CXH/logs_2.sqlite-wal")" "$wal_before"
[[ ! -e "$CXH/state_6.sqlite" ]] && ok "C2 post-install state file removed from codex home" || fail "C2 state_6 removed"
[[ -f "$F/state/codex-state-snapshot/post-install-extra/state_6.sqlite" ]] && ok "C2 ...quarantined, not deleted" || fail "C2 state_6 quarantined"
assert_eq "C2 auto-update-version restored" "$(cat "$CXH/packages/standalone/auto-update-version" 2>/dev/null)" "$auv_before"
assert_eq "C2 auth.json untouched (content)" "$(sha "$CXH/auth.json")" "$auth_before"
assert_eq "C2 auth.json untouched (mtime)" "$(stat -f %m "$CXH/auth.json")" "$auth_mtime"
assert_has "C2 FAIL alert with proof" "$(ledger)" "codex 0.156.1 -> 0.157.0 smoke FAILED"
assert_has "C2 proof: state" "$(ledger)" "state files restored byte-identical: yes"
assert_has "C2 failure reason is the error line, not rc" "$(ledger)" "exec=fail ERROR: stream disconnected before completion"
assert_has "C2 proof: auth" "$(ledger)" "auth.json untouched: yes"
assert_eq "C2 exactly one owner push" "$(push_count)" "1"
assert_has "C2 push is the Korean rollback copy" "$F/push.log" "Codex 0.157.0 업데이트를 되돌렸어요."
assert_has "C2 push names the previous version" "$F/push.log" "이전 버전(0.156.1)으로 돌려놨어요."
assert_not_has "C2 push carries no internal path" "$F/push.log" "releases/"
assert_not_has "C2 push carries no script name" "$F/push.log" "cli-updater"
unset FAKE_CODEX_SMOKE_FAIL FAKE_CODEX_MIGRATE

echo "C3: codex exec usage-limited after install -> kept (--version matches) + watch alert, no push"
new_fixture c3 2.1.284
touch "$F/state/armed" "$F/state/owner-push-armed"
export FAKE_CODEX_USAGE_LIMIT=1 FAKE_CODEX_USAGE_RC=0
rc="$(run_updater --mode install --owner-push)"
assert_eq "C3 rc" "$rc" "0"
assert_eq "C3 new version kept" "$(codex_current)" "$CLI_UPDATER_CODEX_HOME/packages/standalone/releases/0.157.0-aarch64-apple-darwin"
assert_has "C3 watch alert: smoke inconclusive with reason" "$(ledger)" "--version matches but smoke inconclusive: usage limit, try again at Oct 4th"
assert_not_has "C3 no rollback" "$(ledger)" "rolled back"
assert_not_has "C3 no 'NOT available'" "$(ledger)" "NOT available"
assert_eq "C3 zero owner pushes" "$(push_count)" "0"
unset FAKE_CODEX_USAGE_LIMIT FAKE_CODEX_USAGE_RC

echo "C4: codex installer fails before touching anything -> nothing to roll back, no owner push"
new_fixture c4 2.1.284
touch "$F/state/armed" "$F/state/owner-push-armed"
pre_link="$(codex_current)"
export FAKE_CODEX_INSTALL_FAIL=1
rc="$(run_updater --mode install --owner-push)"
assert_eq "C4 rc=1" "$rc" "1"
assert_eq "C4 current unchanged" "$(codex_current)" "$pre_link"
assert_has "C4 FAIL alert (leader queue)" "$(ledger)" "codex install 0.157.0 failed"
assert_eq "C4 zero owner pushes (no change happened)" "$(push_count)" "0"
unset FAKE_CODEX_INSTALL_FAIL

echo "C5: a codex process is running -> skipped tonight, nothing touched"
new_fixture c5 2.1.284
touch "$F/state/armed"
export CLI_UPDATER_CODEX_BUSY_CMD="true"
pre_link="$(codex_current)"
rc="$(run_updater --mode install)"
assert_eq "C5 current unchanged" "$(codex_current)" "$pre_link"
[[ ! -e "$FAKE_INSTALL_LOG" ]] && ok "C5 installer not called" || fail "C5 installer not called"
assert_has "C5 skip alert" "$(inbox_all)" "a codex process is running"
export CLI_UPDATER_CODEX_BUSY_CMD="false"

echo "N1: claude rollback -> exactly one owner push (Korean); gated by flag AND file"
new_fixture n1 2.1.283
touch "$F/state/armed"
echo "codex=0.156.1" > "$F/state/pins.conf"
export FAKE_SMOKE_FAIL=2.1.284
rc="$(run_updater --mode install --owner-push)"
assert_eq "N1a push file absent -> zero pushes" "$(push_count)" "0"
assert_has "N1a draft in the ledger instead" "$(ledger)" "owner push not armed"
ls "$F"/state/owner-notices/*.txt > /dev/null 2>&1 && ok "N1a draft kept in state" || fail "N1a draft kept in state"
new_fixture n1b 2.1.283
touch "$F/state/armed" "$F/state/owner-push-armed"
echo "codex=0.156.1" > "$F/state/pins.conf"
export FAKE_SMOKE_FAIL=2.1.284
rc="$(run_updater --mode install)"
assert_eq "N1b armed file but no --owner-push -> zero pushes" "$(push_count)" "0"
new_fixture n1c 2.1.283
touch "$F/state/armed" "$F/state/owner-push-armed"
echo "codex=0.156.1" > "$F/state/pins.conf"
export FAKE_SMOKE_FAIL=2.1.284
rc="$(run_updater --mode install --owner-push)"
assert_eq "N1c rc=1" "$rc" "1"
assert_eq "N1c exactly one owner push" "$(push_count)" "1"
assert_has "N1c Korean rollback copy" "$F/push.log" "Claude Code 2.1.284 업데이트를 되돌렸어요."
assert_has "N1c via the owner audience" "$F/push.log" "--audience owner --env $F/alert/.telegram_bot/.env"
unset FAKE_SMOKE_FAIL

echo "N2: routine success (both CLIs installed, no new model) -> zero owner pushes"
new_fixture n2 2.1.283
touch "$F/state/armed" "$F/state/owner-push-armed"
rc="$(run_updater --mode install --owner-push)"
assert_eq "N2 rc" "$rc" "0"
assert_eq "N2 claude installed" "$(link_of)" "$CLI_UPDATER_CLAUDE_VERSIONS/2.1.284"
assert_has "N2 codex installed" "$F/state/last-run.env" "CODEX_ACTION=installed"
assert_eq "N2 zero owner pushes" "$(push_count)" "0"

echo "N3: new claude model (alias -> id not in the table) -> one push, once"
new_fixture n3 2.1.284
touch "$F/state/armed" "$F/state/owner-push-armed"
echo "codex=0.156.1" > "$F/state/pins.conf"
export FAKE_ID_OPUS=claude-opus-5-6
rc="$(run_updater --mode install --owner-push)"
assert_eq "N3 one push" "$(push_count)" "1"
assert_has "N3 Korean new-model copy" "$F/push.log" "새 모델이 나왔어요 · Claude Opus 5.6"
rm -f "$F"/state/probe-claude-*.json
rc="$(run_updater --mode install --owner-push)"
assert_eq "N3 same model is not pushed twice" "$(push_count)" "1"
unset FAKE_ID_OPUS

# agent fixture: a registered instance with a bridge session transcript
new_agent() {  # NAME -> AG (instance root) with identity name 김메탈
  AG="$F/inst-$1"
  mkdir -p "$AG/.telegram_bot" "$AG/identity"
  printf '# AGENT\n\n## Identity\n- Name: **김메탈**\n' > "$AG/identity/hot.custom.agent.md"
  echo '{"telegram_session:1": {"session_id": "sess-1", "model": "opus"}}' > "$AG/.telegram_bot/sessions.json"
  AGT="$CLI_UPDATER_CLAUDE_PROJECTS/$(printf '%s' "$AG" | sed 's/[^A-Za-z0-9]/-/g')"
  mkdir -p "$AGT"
  # a headless run from the same workspace (NOT the bridge session)
  echo '{"type":"assistant","message":{"model":"claude-haiku-4-5-20251001"}}' > "$AGT/headless.jsonl"
}
turn() {  # model -> append an assistant turn to the bridge session
  echo '{"type":"assistant","message":{"model":"'"$1"'","content":[]}}' >> "$AGT/sess-1.jsonl"
  echo '{"type":"assistant","message":{"model":"<synthetic>","content":[]}}' >> "$AGT/sess-1.jsonl"
}

echo "A1: running agent model changes opus-5 -> opus-5-5 -> one push naming the agent"
new_fixture a1 2.1.284
touch "$F/state/owner-push-armed"
new_agent m
turn claude-opus-5
rc="$(run_updater --mode agents --owner-push --instance-root "$AG")"
assert_eq "A1 first sighting = baseline, no push" "$(push_count)" "0"
assert_has "A1 baseline recorded from the bridge session only" "$F/state/agent-models.json" '"id": "claude-opus-5"'
rc="$(run_updater --mode agents --owner-push --instance-root "$AG")"
assert_eq "A1 unchanged -> no push" "$(push_count)" "0"
turn claude-opus-5-5
rc="$(run_updater --mode agents --owner-push --instance-root "$AG")"
assert_eq "A1 changed -> exactly one push" "$(push_count)" "1"
assert_has "A1 push names the agent and both displays" "$F/push.log" "김메탈 모델이 바뀌었어요 · Claude Opus 5 → Claude Opus 5.5"
assert_not_has "A1 push carries no workspace path" "$F/push.log" "inst-m"
rc="$(run_updater --mode agents --owner-push --instance-root "$AG")"
assert_eq "A1 unchanged afterwards -> still one push" "$(push_count)" "1"
turn claude-sonnet-5-5
rc="$(run_updater --mode agents --owner-push --instance-root "$AG")"
assert_eq "A1 family switch (/model) -> no push" "$(push_count)" "1"
assert_has "A1 family switch lands in the leader queue" "$(ledger)" "switched model claude-opus-5-5 -> claude-sonnet-5-5 (family change"

echo "A2: restarted bridge with no turn -> bounded wait, then watch alert"
new_fixture a2 2.1.284
new_agent w
turn claude-opus-5-5
touch -t "$(date -v-1H +%Y%m%d%H%M)" "$AGT/sess-1.jsonl"
since="$(date +%s)"
echo '{"schema": 1, "started_at": "'"$(date -r $((since + 1)) +%Y-%m-%dT%H:%M:%S%z | sed 's/\(..\)$/:\1/')"'"}' > "$AG/.telegram_bot/runtime-snapshot.json"
t0="$(date +%s)"
rc="$(run_updater --mode agents --instance-root "$AG" --wait-secs 2 --since "$since")"
t1="$(date +%s)"
[[ $((t1 - t0)) -ge 2 && $((t1 - t0)) -le 15 ]] && ok "A2 waited ~2s then gave up ($((t1 - t0))s)" || fail "A2 wait bound ($((t1 - t0))s)"
assert_has "A2 watch alert names the agent" "$(inbox_all)" "agent 김메탈 was restarted but reported no turn within 2s"

echo "A3: pre-step run also checks agents (daytime changes caught next night)"
new_fixture a3 2.1.284
touch "$F/state/owner-push-armed"
new_agent p
turn claude-opus-5
rc="$(run_updater --mode observe --owner-push --instance-root "$AG")"
turn claude-opus-5-5
rc="$(run_updater --mode observe --owner-push --instance-root "$AG")"
assert_eq "A3 one push from the regular run" "$(push_count)" "1"

# ---------------------------------------------------------------------------
# queue runner fixtures
# ---------------------------------------------------------------------------
new_queue() {  # NAME -> QH (home), ORDER file, two instances a(prio 10), b(prio 20)
  QH="$BASE/$1/home"
  ORDER="$BASE/$1/order.log"
  mkdir -p "$QH/.dogany/memory-queue/instances.d"
  : > "$ORDER"
  local s prio=10
  for s in a b; do
    local r="$BASE/$1/inst-$s"
    mkdir -p "$r/routines" "$r/.telegram_bot/logs" "$r/bridge"
    touch "$r/.instance.conf" "$r/.telegram_bot/.env"
    cat > "$r/routines/cron-guard.sh" <<'EOF'
#!/bin/bash
while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
shift
exec "$@"
EOF
    printf '#!/bin/bash\necho "consolidate %s" >> "%s"\n' "$s" "$ORDER" > "$r/routines/consolidate-0430.sh"
    printf '#!/bin/bash\necho "classify %s" >> "%s"\n' "$s" "$ORDER" > "$r/routines/classify-inbox-check.sh"
    printf '#!/bin/bash\necho "restart %s $*" >> "%s"\n' "$s" "$ORDER" > "$r/bridge/self_restart.sh"
    chmod +x "$r/routines/"*.sh "$r/bridge/self_restart.sh"
    printf 'ROOT=%s\nENABLED=1\nPRIORITY=%s\n' "$r" "$prio" > "$QH/.dogany/memory-queue/instances.d/$s.conf"
    prio=$((prio + 10))
  done
  FAKE_UPD="$BASE/$1/fake-updater.py"
}
fake_updater() {  # rc sleep changed
  cat > "$FAKE_UPD" <<EOF
import os, sys, time
open("$ORDER", "a").write("prestep %s\n" % " ".join(sys.argv[1:3]))
open("$ORDER.argv", "a").write(" ".join(sys.argv[1:]) + "\n")
time.sleep($2)
st = sys.argv[sys.argv.index("--state-dir") + 1]
os.makedirs(st, exist_ok=True)
open(os.path.join(st, "last-run.env"), "w").write(
    "RUN_TS=%d\nCLAUDE_CHANGED=$3\nCLAUDE_FROM=2.1.283\nCLAUDE_TO=2.1.284\n" % int(time.time()))
sys.exit($1)
EOF
}
run_dmq() {  # args...
  HOME="$QH" DMQ_CLI_UPDATER="$FAKE_UPD" DMQ_ALERT_ROOT="" /bin/bash "$DMQ" "$@" > "$QH/../dmq.out" 2>&1
  echo $?
}
qlog() { cat "$QH"/.dogany/logs/dawn-memory-queue/queue-*.log; }

echo "D1: dry-run shows the pre-step before the instance plan"
new_queue d1
fake_updater 0 0 0
HOME="$QH" DMQ_CLI_UPDATER="$FAKE_UPD" /bin/bash "$DMQ" --dry-run > "$BASE/d1/dry.out" 2>&1
pre_line="$(grep -n 'pre-step (runs BEFORE' "$BASE/d1/dry.out" | cut -d: -f1)"
plan_line="$(grep -n 'execution plan' "$BASE/d1/dry.out" | cut -d: -f1)"
[[ -n "$pre_line" && -n "$plan_line" && $pre_line -lt $plan_line ]] \
  && ok "D1 pre-step listed before execution plan ($pre_line < $plan_line)" \
  || fail "D1 pre-step before plan (pre=$pre_line plan=$plan_line)"
assert_has "D1 dry-run mode observe without --live" "$BASE/d1/dry.out" "--mode observe"
assert_has "D1 post-step listed" "$BASE/d1/dry.out" "post-step: bridge restart"
assert_has "D1 agent check post-step listed" "$BASE/d1/dry.out" "post-step 2: agent model check"
[[ ! -s "$ORDER" ]] && ok "D1 dry-run executed nothing" || fail "D1 dry-run executed nothing"

echo "D2: pre-step rc=3 -> runs first, consolidate not blocked"
new_queue d2
fake_updater 3 0 0
rc="$(run_dmq)"
assert_eq "D2 queue exit 0 (pre-step fail-open)" "$rc" "0"
assert_eq "D2 order: prestep first" "$(sed -n 1p "$ORDER")" "prestep --mode observe"
assert_eq "D2 order: consolidate a second" "$(sed -n 2p "$ORDER")" "consolidate a"
assert_has "D2 consolidate b ran" "$ORDER" "consolidate b"
qlog > "$BASE/d2/q.log"
assert_not_has "D2 no --owner-push without --live" "$ORDER.argv" "--owner-push"
assert_has "D2 pre-step rc logged" "$BASE/d2/q.log" "pre-step cli-update rc=3 -- queue continues"
assert_has "D2 summary verdict OK + prestep=3" "$BASE/d2/q.log" "verdict=OK instances=2 ok=2 fail=0 skip=0 timeout_kills=0"
assert_has "D2 summary carries prestep" "$BASE/d2/q.log" "prestep=3 restarts=0"
first_pre="$(grep -n 'pre-step cli-update:' "$BASE/d2/q.log" | head -1 | cut -d: -f1)"
first_a="$(grep -n '\[a\] step A' "$BASE/d2/q.log" | head -1 | cut -d: -f1)"
[[ $first_pre -lt $first_a ]] && ok "D2 log: pre-step before first step A" || fail "D2 log order"

echo "D3: pre-step timeout-kill -> consolidate still runs"
new_queue d3
fake_updater 0 20 0
rc="$(HOME="$QH" DMQ_CLI_UPDATER="$FAKE_UPD" DMQ_CLI_UPDATE_TIMEOUT=2 /bin/bash "$DMQ" > "$QH/../dmq.out" 2>&1; echo $?)"
assert_eq "D3 queue exit 0" "$rc" "0"
assert_has "D3 consolidate a ran" "$ORDER" "consolidate a"
qlog > "$BASE/d3/q.log"
assert_has "D3 timeout logged" "$BASE/d3/q.log" "pre-step cli-update TIMEOUT-KILL"
assert_has "D3 summary prestep=124" "$BASE/d3/q.log" "prestep=124"

echo "D4: pre-step script missing -> consolidate still runs"
new_queue d4
rc="$(HOME="$QH" DMQ_CLI_UPDATER="$BASE/d4/nope.py" /bin/bash "$DMQ" > "$QH/../dmq.out" 2>&1; echo $?)"
assert_eq "D4 queue exit 0" "$rc" "0"
assert_has "D4 consolidate a ran" "$ORDER" "consolidate a"
assert_has "D4 summary prestep=missing" "$(qlog > "$BASE/d4/q.log"; echo "$BASE/d4/q.log")" "prestep=missing"

echo "D5: post-step restart gating"
new_queue d5
fake_updater 0 0 1
rc="$(run_dmq)"
assert_not_has "D5a no restart without --live" "$ORDER" "restart"
: > "$ORDER"
rc="$(run_dmq --live)"
assert_not_has "D5b no restart without restart-armed" "$ORDER" "restart"
qlog > "$BASE/d5/q.log"
assert_has "D5b skip reason logged" "$BASE/d5/q.log" "restart-armed absent"
: > "$ORDER"
mkdir -p "$QH/.dogany/cli-updates"; touch "$QH/.dogany/cli-updates/restart-armed"
rc="$(run_dmq --live)"
assert_eq "D5c prestep got install mode on --live" "$(sed -n 1p "$ORDER")" "prestep --mode install"
assert_has "D5c restart a via --trigger auto" "$ORDER" "restart a --trigger auto --reason DGN-1673 nightly claude CLI 2.1.283 -> 2.1.284"
assert_has "D5c restart b" "$ORDER" "restart b --trigger auto"
assert_eq "D5c agent model check runs after the restarts" "$(tail -1 "$ORDER")" "prestep --mode agents"
assert_has "D5c agent check waits for restarted bridges" "$ORDER.argv" "--wait-secs 600 --since"
grep -- '--mode agents' "$ORDER.argv" | grep -q -- '--owner-push' \
  && ok "D5c --live passes --owner-push to the agent check" || fail "D5c --owner-push to the agent check"
grep -- '--mode install' "$ORDER.argv" | tail -1 | grep -q -- '--owner-push' \
  && ok "D5c --live passes --owner-push to the pre-step" || fail "D5c --owner-push to the pre-step"
last_cons="$(grep -n '^consolidate' "$ORDER" | tail -1 | cut -d: -f1)"
first_rs="$(grep -n '^restart' "$ORDER" | head -1 | cut -d: -f1)"
[[ $last_cons -lt $first_rs ]] && ok "D5c restarts after every consolidate" || fail "D5c restart order"
: > "$ORDER"
fake_updater 0 0 0
rc="$(run_dmq --live)"
assert_not_has "D5d no restart when CHANGED=0" "$ORDER" "restart"
assert_not_has "D5d no agent check without a restart" "$ORDER" "--mode agents"

echo "D6: end-to-end queue --live with the real updater on fixtures"
new_queue d6
new_fixture d6u 2.1.283
mkdir -p "$QH/.dogany/cli-updates"
touch "$QH/.dogany/cli-updates/armed" "$QH/.dogany/cli-updates/restart-armed"
ALERT="$BASE/d6u/alert"
rc="$(HOME="$QH" PATH="$FPATH" DMQ_CLI_UPDATER="$UPDATER" DMQ_ALERT_ROOT="$ALERT" \
      /bin/bash "$DMQ" --live > "$QH/../dmq.out" 2>&1; echo $?)"
assert_eq "D6 queue exit 0" "$rc" "0"
assert_eq "D6 claude installed via pre-step" "$(link_of)" "$CLI_UPDATER_CLAUDE_VERSIONS/2.1.284"
assert_has "D6 consolidate ran" "$ORDER" "consolidate a"
assert_has "D6 restart ran after install" "$ORDER" "restart a --trigger auto --reason DGN-1673 nightly claude CLI 2.1.283 -> 2.1.284"
assert_has "D6 ok alert reached the alert-root ledger" "$ALERT/worklog/_CRON_QUEUE_LEDGER.md" "claude 2.1.283 -> 2.1.284 installed"
qlog > "$BASE/d6/q.log"
assert_has "D6 summary" "$BASE/d6/q.log" "prestep=0 restarts=2"

echo ""
echo "RESULT: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
