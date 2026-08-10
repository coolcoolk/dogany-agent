#!/bin/bash
# dgn593_selftest.sh -- DGN-593 self-test harness (T1-T9).
#
# Covers the spec's self-test plan (worklog/DGN-593-spec-v2.md):
#   T1 source invariance      T2 concurrent consumption   T3 channel cohabit
#   T4 classification matrix  T5 substitution channel     T6 dry-run purity
#   T7 temp source lifecycle  T8 main-branch guard        T9 defect regression
#
# SAFETY: every scenario runs against throwaway sandbox repos + instances
# under a private mktemp WORK dir. The real shared repo is never touched;
# no network (origins are local bare clones); no launchd, no tokens.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn593-test.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"   # physical path (macOS /var -> /private/var)
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
CURRENT=""

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL[$CURRENT]: $*"; }
assert() { # assert <desc> <cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}
assert_eq() { # assert_eq <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected [$2] got [$3])"; fi
}

file_sha() { shasum < "$1" 2>/dev/null | awk '{print $1}'; }
str_sha()  { printf '%s\n' "$1" | shasum | awk '{print $1}'; }
tree_digest() {
  ( cd "$1" && find . -type f -print0 2>/dev/null \
      | LC_ALL=C sort -z | xargs -0 shasum 2>/dev/null \
      | shasum | awk '{print $1}' )
}
repo_state() { # HEAD sha + symbolic ref + porcelain, one blob
  {
    git -C "$1" rev-parse HEAD
    git -C "$1" symbolic-ref -q HEAD || echo "detached"
    git -C "$1" status --porcelain
  } 2>/dev/null
}
leftover_src() { # count dogany-src.* leftovers under $1
  find "$1" -maxdepth 1 -name 'dogany-src.*' 2>/dev/null | wc -l | tr -d ' '
}

# ---------------------------------------------------------------------------
# sandbox builders
# ---------------------------------------------------------------------------
build_tree() { # $1 = repo dir; minimal framework tree with the code under test
  local r="$1"
  mkdir -p "$r/agents/.template/routines" "$r/agents/.template/bridge"
  cp "$SANDBOX/update.sh" "$r/update.sh"
  chmod +x "$r/update.sh"
  cp "$SANDBOX/agents/.template/routines/self-update.sh" \
     "$r/agents/.template/routines/self-update.sh"
  chmod +x "$r/agents/.template/routines/self-update.sh"
  printf '9.9.9-test\n' > "$r/VERSION"
  printf '# RULES (test fixture)\n' > "$r/agents/.template/RULES.md"
  printf '# AGENT-OPS (test fixture)\nroot: __PROJECT_ROOT__\n' \
    > "$r/agents/.template/AGENT-OPS.md"
  cat > "$r/agents/.template/bridge/UPSTREAM.md" <<'EOF'
# bridge upstream (test fixture)
- Pinned commit: feca63efc507f820774be6be036aa1695113c950
- Vendor-rev: DGN-399 (base),
  DGN-541 (new marker on a continuation line -- multiline vrev fixture)
EOF
  printf '#!/bin/sh\n# vendor v2 ROOT=__PROJECT_ROOT__ AGENT=__AGENT_NAME__\nexit 0\n' \
    > "$r/agents/.template/bridge/self_restart.sh"
  chmod +x "$r/agents/.template/bridge/self_restart.sh"
  printf '#!/bin/sh\n# vendor v2 W=__PROJECT_ROOT__\nexit 0\n' \
    > "$r/agents/.template/bridge/watchdog_setup.sh"
  chmod +x "$r/agents/.template/bridge/watchdog_setup.sh"
  printf '<plist>ROOT=__PROJECT_ROOT__ NAME=__AGENT_NAME__</plist>\n' \
    > "$r/agents/.template/bridge/com.telegram-skill-bot.telegram-agent.newbridge.plist"
  printf 'DASH = "vendor-v2"\n' > "$r/agents/.template/bridge/dashboard.py"
  printf 'BOT = "vendor-v1"\n'  > "$r/agents/.template/bridge/bot.py"
}

finalize_repo() { # $1 = work dir containing $1/repo; commits, tags, bare origin
  local w="$1"
  git -C "$w/repo" init -q -b main
  git -C "$w/repo" -c user.email=t@t -c user.name=t add -A
  git -C "$w/repo" -c user.email=t@t -c user.name=t commit -qm seed
  git -C "$w/repo" tag v0.0.1
  git clone -q --bare "$w/repo" "$w/origin.git" 2>/dev/null
  git -C "$w/repo" remote add origin "$w/origin.git"
  git -C "$w/repo" fetch -q origin
  git -C "$w/repo" branch -q --set-upstream-to=origin/main main
}

make_repo() { # $1 = work dir
  mkdir -p "$1"
  build_tree "$1/repo"
  finalize_repo "$1"
}

make_instance() { # $1 root, $2 slug, $3 repo_root, $4 channel
  local root="$1" slug="$2" repo="$3" chan="$4"
  mkdir -p "$root/bridge" "$root/config" "$root/routines" "$root/.claude"
  cp "$SANDBOX/agents/.template/routines/self-update.sh" "$root/routines/self-update.sh"
  chmod +x "$root/routines/self-update.sh"
  printf 'AGENT_LANG=en\n' > "$root/config/agent.conf"
  {
    echo "DOGANY_AGENT_NAME=$slug"
    echo "DOGANY_AGENT_LABEL=$slug"
    echo "DOGANY_USER_LABEL=you"
    echo "DOGANY_AGENT_PREFIX=[agent]"
    echo "DOGANY_REPO_ROOT=$repo"
    echo "DOGANY_UPDATE_CHANNEL=$chan"
  } > "$root/.instance.conf"
  # Prior-install state (an old, already-substituted v1 bridge).
  printf 'DASH = "vendor-v1"\n' > "$root/bridge/dashboard.py"
  printf 'BOT = "vendor-v1"\n'  > "$root/bridge/bot.py"
  printf '#!/bin/sh\n# vendor v1 ROOT=%s AGENT=%s\nexit 0\n' "$root" "$slug" \
    > "$root/bridge/self_restart.sh"
  chmod +x "$root/bridge/self_restart.sh"
  printf '#!/bin/sh\n# vendor v1 W=%s\nexit 0\n' "$root" \
    > "$root/bridge/watchdog_setup.sh"
  chmod +x "$root/bridge/watchdog_setup.sh"
  printf '<plist>ROOT=%s NAME=%s</plist>\n' "$root" "$slug" \
    > "$root/bridge/com.telegram-skill-bot.$slug.newbridge.plist"
  cat > "$root/bridge/UPSTREAM.md" <<'EOF'
# bridge upstream (test fixture)
- Pinned commit: feca63efc507f820774be6be036aa1695113c950
- Vendor-rev: DGN-399 (base),
EOF
}

seed_manifest() { # $1 = instance root; seed M for dashboard.py + bot.py
  {
    printf '# seed\n'
    printf 'bridge/dashboard.py  %s\n' "$(file_sha "$1/bridge/dashboard.py")"
    printf 'bridge/bot.py  %s\n'       "$(file_sha "$1/bridge/bot.py")"
  } > "$1/.claude/.dogany-bridge.sha"
}

# ===========================================================================
CURRENT="T1"
say "T1: source invariance (release consumption never moves the shared repo)"
w="$WORK/t1"; make_repo "$w"
inst="$w/inst"; make_instance "$inst" t1agent "$w/repo" release; seed_manifest "$inst"
mkdir -p "$w/tmp"
# Dirty the shared repo working tree (dev session in progress).
printf 'dirty-dev-edit\n' >> "$w/repo/agents/.template/bridge/bot.py"
printf 'untracked\n' > "$w/repo/untracked.txt"
state_before="$(repo_state "$w/repo")"
dirty_before="$(file_sha "$w/repo/agents/.template/bridge/bot.py")"
TMPDIR="$w/tmp" DOGANY_LANG=en sh "$inst/routines/self-update.sh" > "$w/t1.log" 2>&1
rc=$?
assert_eq "self-update (release) exits 0" "0" "$rc"
assert_eq "repo HEAD/branch/status byte-identical" "$state_before" "$(repo_state "$w/repo")"
assert_eq "dirty working-tree file content untouched" \
  "$dirty_before" "$(file_sha "$w/repo/agents/.template/bridge/bot.py")"
assert_eq "lossless drift landed from TAG source (not dirty tree)" \
  "$(str_sha 'DASH = "vendor-v2"')" "$(file_sha "$inst/bridge/dashboard.py")"
assert_eq "no temp source leftover" "0" "$(leftover_src "$w/tmp")"

# ===========================================================================
CURRENT="T2"
say "T2: concurrent consumption (2 instances, same shared repo)"
w="$WORK/t2"; make_repo "$w"
mkdir -p "$w/tmp"
instA="$w/instA"; make_instance "$instA" t2a "$w/repo" release; seed_manifest "$instA"
instB="$w/instB"; make_instance "$instB" t2b "$w/repo" release; seed_manifest "$instB"
state_before="$(repo_state "$w/repo")"
TMPDIR="$w/tmp" DOGANY_LANG=en sh "$instA/routines/self-update.sh" > "$w/a.log" 2>&1 &
pa=$!
TMPDIR="$w/tmp" DOGANY_LANG=en sh "$instB/routines/self-update.sh" > "$w/b.log" 2>&1 &
pb=$!
wait "$pa"; ra=$?
wait "$pb"; rb=$?
assert_eq "instance A update exits 0" "0" "$ra"
assert_eq "instance B update exits 0" "0" "$rb"
assert_eq "shared repo state unchanged after concurrent runs" \
  "$state_before" "$(repo_state "$w/repo")"
assert_eq "A landed" "$(str_sha 'DASH = "vendor-v2"')" "$(file_sha "$instA/bridge/dashboard.py")"
assert_eq "B landed" "$(str_sha 'DASH = "vendor-v2"')" "$(file_sha "$instB/bridge/dashboard.py")"
assert_eq "no shared temp/state leftover" "0" "$(leftover_src "$w/tmp")"

# ===========================================================================
CURRENT="T3"
say "T3: channel cohabitation (release run concurrent with main pull)"
w="$WORK/t3"; make_repo "$w"
mkdir -p "$w/tmp"
inst="$w/inst"; make_instance "$inst" t3agent "$w/repo" release; seed_manifest "$inst"
TMPDIR="$w/tmp" DOGANY_LANG=en sh "$inst/routines/self-update.sh" > "$w/t3.log" 2>&1 &
p3=$!
pull_rc=1
if git -C "$w/repo" pull --ff-only -q >/dev/null 2>&1; then
  pull_rc=0
else
  # one retry absorbs a transient ref-lock collision with the release fetch
  sleep 1
  if git -C "$w/repo" pull --ff-only -q >/dev/null 2>&1; then pull_rc=0; fi
fi
wait "$p3"; r3=$?
assert_eq "main-channel pull --ff-only succeeds (<=1 retry)" "0" "$pull_rc"
assert_eq "release update succeeds alongside" "0" "$r3"

# ===========================================================================
CURRENT="T4"
say "T4: classification matrix (rows 1-10) + accept escape hatches"
w="$WORK/t4"; mkdir -p "$w"
build_tree "$w/repo"   # --no-pull direct run: no git needed
inst="$w/inst"; make_instance "$inst" t4agent "$w/repo" release
T="$w/repo/agents/.template/bridge"
printf 'one\n'       > "$T/f1.py";  printf 'one\n' > "$inst/bridge/f1.py"
printf 'two-new\n'   > "$T/f2.py";  printf 'two-old\n' > "$inst/bridge/f2.py"
printf 'A3v\n'       > "$T/f3.py";  printf 'B3-local\n' > "$inst/bridge/f3.py"
printf 'C4v\n'       > "$T/f4.py";  printf 'B4-local\n' > "$inst/bridge/f4.py"
printf 'A5v\n'       > "$T/f5.py";  printf 'B5-local\n' > "$inst/bridge/f5.py"
printf 'six\n'       > "$T/f6.py"
printf 'seven\n'     > "$T/f7.py"
printf 'eight-new\n' > "$T/f8.py"
printf 'nine\n'      > "$inst/bridge/f9.py"
{
  printf '# seed\n'
  printf 'bridge/f2.py  %s\n'  "$(str_sha 'two-old')"
  printf 'bridge/f3.py  %s\n'  "$(str_sha 'A3v')"
  printf 'bridge/f4.py  %s\n'  "$(str_sha 'A4-ancient')"
  printf 'bridge/f7.py  %s\n'  "$(str_sha 'seven')"
  printf 'bridge/f8.py  %s\n'  "$(str_sha 'eight-old')"
  printf 'bridge/f9.py  %s\n'  "$(str_sha 'nine')"
  printf 'bridge/f10.py  %s\n' "$(str_sha 'ten')"
} > "$inst/.claude/.dogany-bridge.sha"
M="$inst/.claude/.dogany-bridge.sha"
R="$inst/.claude/bridge-reconcile.report"

DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes > "$w/run1.log" 2>&1
rc=$?
assert_eq "run1 exits 0 (conflicts warn, never fail)" "0" "$rc"
assert_eq "#1 in-sync untouched"          "$(str_sha 'one')"     "$(file_sha "$inst/bridge/f1.py")"
assert "  #1 manifest recorded"           grep -q "^bridge/f1.py  $(str_sha 'one')$" "$M"
assert_eq "#2 lossless drift LANDED"      "$(str_sha 'two-new')" "$(file_sha "$inst/bridge/f2.py")"
assert "  #2 manifest updated"            grep -q "^bridge/f2.py  $(str_sha 'two-new')$" "$M"
assert_eq "#3 local edit PRESERVED"       "$(str_sha 'B3-local')" "$(file_sha "$inst/bridge/f3.py")"
assert "  #3 manifest keeps old M"        grep -q "^bridge/f3.py  $(str_sha 'A3v')$" "$M"
assert_eq "#4 conflict PRESERVED"         "$(str_sha 'B4-local')" "$(file_sha "$inst/bridge/f4.py")"
assert "  #4 report CONFLICT"             grep -q '^CONFLICT bridge/f4.py$' "$R"
assert_eq "#5 bootstrap on-disk UNCHANGED" "$(str_sha 'B5-local')" "$(file_sha "$inst/bridge/f5.py")"
assert "  #5 manifest ADOPTED-PROVISIONAL (on-disk sha + tag)" \
  grep -q "^bridge/f5.py  $(str_sha 'B5-local') #adopted-provisional$" "$M"
assert "  #5 report ADOPTED-PROVISIONAL" grep -q '^ADOPTED-PROVISIONAL bridge/f5.py$' "$R"
assert_eq "#6 vendor-new LANDED"          "$(str_sha 'six')"      "$(file_sha "$inst/bridge/f6.py")"
assert "  #6 report NEW"                  grep -q '^NEW bridge/f6.py$' "$R"
assert "#7 deletion respected (absent)"   bash -c "[ ! -e '$inst/bridge/f7.py' ]"
assert "  #7 manifest entry KEPT (deletion memory)" \
  grep -q "^bridge/f7.py  $(str_sha 'seven')$" "$M"
assert "  #7 report DELETED-KEPT"         grep -q '^DELETED-KEPT bridge/f7.py$' "$R"
assert "#8 deletion-conflict not landed"  bash -c "[ ! -e '$inst/bridge/f8.py' ]"
assert "  #8 report CONFLICT"             grep -q '^CONFLICT bridge/f8.py$' "$R"
assert "#9 vendor-removed file kept"      bash -c "[ -f '$inst/bridge/f9.py' ]"
assert "  #9 manifest entry dropped"      bash -c "! grep -q '^bridge/f9.py ' '$M'"
assert "#10 stale manifest entry dropped" bash -c "! grep -q '^bridge/f10.py ' '$M'"
assert "summary line printed"             grep -q 'bridge reconcile: .* landed / .* preserved / .* conflict' "$w/run1.log"

# run2: explicit --bridge-accept on the #4 conflict
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes \
  --bridge-accept bridge/f4.py > "$w/run2.log" 2>&1
rc=$?
assert_eq "run2 (--bridge-accept) exits 0" "0" "$rc"
assert_eq "#4 accepted -> landed" "$(str_sha 'C4v')" "$(file_sha "$inst/bridge/f4.py")"
assert "  #4 manifest seeded"     grep -q "^bridge/f4.py  $(str_sha 'C4v')$" "$M"
assert "  #4 backup created"      bash -c "ls '$inst/.claude/bridge-backups/bridge/'f4.py.user-* >/dev/null 2>&1"

# run3: --bridge-accept-all refused while a conflict (#8) remains
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes \
  --bridge-accept-all > "$w/run3.log" 2>&1
rc=$?
assert "run3 (--bridge-accept-all) REFUSED (non-zero exit)" bash -c "[ $rc -ne 0 ]"
assert "  refusal lists the conflict file" grep -q 'bridge/f8.py' "$w/run3.log"
assert "  #8 still not landed"    bash -c "[ ! -e '$inst/bridge/f8.py' ]"

# run4: --force-accept-all passes, with per-file backups
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes \
  --force-accept-all > "$w/run4.log" 2>&1
rc=$?
assert_eq "run4 (--force-accept-all) exits 0" "0" "$rc"
assert_eq "#8 force-accepted -> landed" "$(str_sha 'eight-new')" "$(file_sha "$inst/bridge/f8.py")"
assert_eq "#3 accepted -> landed"       "$(str_sha 'A3v')"       "$(file_sha "$inst/bridge/f3.py")"
assert_eq "#5 accepted -> landed"       "$(str_sha 'A5v')"       "$(file_sha "$inst/bridge/f5.py")"
assert "  #3 backup created" bash -c "ls '$inst/.claude/bridge-backups/bridge/'f3.py.user-* >/dev/null 2>&1"
assert "  #5 backup created" bash -c "ls '$inst/.claude/bridge-backups/bridge/'f5.py.user-* >/dev/null 2>&1"

# ===========================================================================
CURRENT="T5"
say "T5: substitution channel (unconditional landing + post-subst M record)"
w="$WORK/t5"; mkdir -p "$w"
build_tree "$w/repo"
inst="$w/inst"; make_instance "$inst" t5agent "$w/repo" release; seed_manifest "$inst"
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes > "$w/t5.log" 2>&1
rc=$?
assert_eq "update exits 0" "0" "$rc"
assert "self_restart.sh landed (vendor v2)"  grep -q 'vendor v2' "$inst/bridge/self_restart.sh"
assert "self_restart.sh substituted"         grep -q "ROOT=$inst" "$inst/bridge/self_restart.sh"
assert "self_restart.sh no leftover token"   bash -c "! grep -q '__PROJECT_ROOT__' '$inst/bridge/self_restart.sh'"
assert "watchdog_setup.sh landed+substituted" grep -q "W=$inst" "$inst/bridge/watchdog_setup.sh"
assert "UPSTREAM.md landed unconditionally"  grep -q 'multiline vrev fixture' "$inst/bridge/UPSTREAM.md"
assert "agent-named plist present"  bash -c "[ -f '$inst/bridge/com.telegram-skill-bot.t5agent.newbridge.plist' ]"
assert "generic plist cruft removed" \
  bash -c "[ ! -e '$inst/bridge/com.telegram-skill-bot.telegram-agent.newbridge.plist' ]"
assert "M records self_restart.sh post-subst sha" \
  grep -q "^bridge/self_restart.sh  $(file_sha "$inst/bridge/self_restart.sh")$" \
  "$inst/.claude/.dogany-bridge.sha"
assert "M records renamed plist sha" \
  grep -q "^bridge/com.telegram-skill-bot.t5agent.newbridge.plist  " \
  "$inst/.claude/.dogany-bridge.sha"
# preserve-registered substitution file stays untouched
inst2="$w/inst2"; make_instance "$inst2" t5b "$w/repo" release; seed_manifest "$inst2"
printf 'bridge/self_restart.sh\n' > "$inst2/.claude/.dogany-preserve"
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst2" --no-pull --yes > "$w/t5b.log" 2>&1
rc=$?
assert_eq "preserve-case update exits 0" "0" "$rc"
assert "preserved subst file NOT landed" grep -q 'vendor v1' "$inst2/bridge/self_restart.sh"

# ===========================================================================
CURRENT="T6"
say "T6: dry-run purity (no writes; preview == subsequent real run)"
w="$WORK/t6"; mkdir -p "$w"
build_tree "$w/repo"
printf 'C-vendor\n' > "$w/repo/agents/.template/bridge/fc.py"
finalize_repo "$w"
mkdir -p "$w/tmp"
inst="$w/inst"; make_instance "$inst" t6agent "$w/repo" release
printf 'B-local\n' > "$inst/bridge/fc.py"
{
  printf 'bridge/dashboard.py  %s\n' "$(file_sha "$inst/bridge/dashboard.py")"
  printf 'bridge/bot.py  %s\n'       "$(file_sha "$inst/bridge/bot.py")"
  printf 'bridge/fc.py  %s\n'        "$(str_sha 'A-old')"
} > "$inst/.claude/.dogany-bridge.sha"
digest_before="$(tree_digest "$inst")"
TMPDIR="$w/tmp" DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --dry-run --yes \
  > "$w/dry.log" 2>&1
rc=$?
assert_eq "dry-run exits 0" "0" "$rc"
assert_eq "instance tree byte-identical after dry-run" \
  "$digest_before" "$(tree_digest "$inst")"
assert "no report written"   bash -c "[ ! -e '$inst/.claude/bridge-reconcile.report' ]"
assert "no backups written"  bash -c "[ ! -e '$inst/.claude/bridge-backups' ]"
assert "no temp source leftover after dry-run" bash -c "[ \"$(leftover_src "$w/tmp")\" = 0 ]"
assert "preview: would land dashboard" grep -q 'would land (lossless drift): bridge/dashboard.py' "$w/dry.log"
assert "preview: conflict surfaced for fc.py" grep -q 'bridge/fc.py' "$w/dry.log"
TMPDIR="$w/tmp" DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --yes \
  > "$w/real.log" 2>&1
rc=$?
assert_eq "real run exits 0" "0" "$rc"
assert_eq "real run lands dashboard (matches preview)" \
  "$(str_sha 'DASH = "vendor-v2"')" "$(file_sha "$inst/bridge/dashboard.py")"
assert "real report LANDED dashboard (matches preview)" \
  grep -q '^LANDED bridge/dashboard.py$' "$inst/.claude/bridge-reconcile.report"
assert "real report CONFLICT fc.py (matches preview)" \
  grep -q '^CONFLICT bridge/fc.py$' "$inst/.claude/bridge-reconcile.report"
assert_eq "fc.py preserved in real run too" "$(str_sha 'B-local')" "$(file_sha "$inst/bridge/fc.py")"
assert "no temp source leftover after real run" bash -c "[ \"$(leftover_src "$w/tmp")\" = 0 ]"

# ===========================================================================
CURRENT="T7"
say "T7: temp source lifecycle + no-checkout assertion"
# (a) normal termination: covered above; re-assert explicitly
w="$WORK/t7a"; make_repo "$w"; mkdir -p "$w/tmp"
inst="$w/inst"; make_instance "$inst" t7a "$w/repo" release; seed_manifest "$inst"
TMPDIR="$w/tmp" DOGANY_LANG=en sh "$inst/routines/self-update.sh" > "$w/a.log" 2>&1
rc=$?
assert_eq "(a) normal run exits 0" "0" "$rc"
assert_eq "(a) no leftover after normal run" "0" "$(leftover_src "$w/tmp")"
# (b) verification-gate die on incomplete extraction (tag lacks agents/.template)
w="$WORK/t7b"; mkdir -p "$w"
build_tree "$w/repo"
inst="$w/inst"; make_instance "$inst" t7b "$w/repo" release
rm -rf "$w/repo/agents"          # tag will carry update.sh but no template
finalize_repo "$w"
mkdir -p "$w/tmp"
TMPDIR="$w/tmp" DOGANY_LANG=en sh "$inst/routines/self-update.sh" > "$w/b.log" 2>&1
rc=$?
assert "(b) incomplete extraction -> die (non-zero)" bash -c "[ $rc -ne 0 ]"
assert "(b) die names the incomplete extraction" grep -q 'incomplete' "$w/b.log"
assert_eq "(b) trap cleaned the temp source on die" "0" "$(leftover_src "$w/tmp")"
# (c) INT mid-run: trap cleans the temp source
w="$WORK/t7c"; mkdir -p "$w"
build_tree "$w/repo"
printf '#!/bin/bash\nsleep 3\nexit 0\n' > "$w/repo/update.sh"  # slow child stub
chmod +x "$w/repo/update.sh"
finalize_repo "$w"
inst="$w/inst"; make_instance "$inst" t7c "$w/repo" release
mkdir -p "$w/tmp"
TMPDIR="$w/tmp" DOGANY_LANG=en sh "$inst/routines/self-update.sh" > "$w/c.log" 2>&1 &
pc=$!
sleep 1
kill -INT "$pc" 2>/dev/null
wait "$pc" 2>/dev/null
assert_eq "(c) INT: temp source cleaned" "0" "$(leftover_src "$w/tmp")"
# (d) grep assert: no git checkout/switch/worktree invocation in shipped code
no_git_mutators() {
  local f="$1"
  ! grep -nE '(^|[^A-Za-z._-])git ((-C|--git-dir)[= ][^ ]+ )*(checkout|switch|worktree)([^A-Za-z-]|$)' "$f" \
    | grep -vE '^[0-9]+:[[:space:]]*#'
}
assert "(d) update.sh: zero git checkout/switch/worktree calls" \
  no_git_mutators "$SANDBOX/update.sh"
assert "(d) self-update.sh: zero git checkout/switch/worktree calls" \
  no_git_mutators "$SANDBOX/agents/.template/routines/self-update.sh"

# ===========================================================================
CURRENT="T8"
say "T8: main-branch guard (channel=main on detached / non-main -> die)"
w="$WORK/t8"; make_repo "$w"
inst="$w/inst"; make_instance "$inst" t8agent "$w/repo" main; seed_manifest "$inst"
git -C "$w/repo" checkout -qb dev    # harness-side move, allowed in test code
state_before="$(repo_state "$w/repo")"
DOGANY_LANG=en sh "$inst/routines/self-update.sh" > "$w/dev.log" 2>&1
rc=$?
assert "non-main branch -> die" bash -c "[ $rc -ne 0 ]"
assert "  hint printed" grep -q 'not on main' "$w/dev.log"
assert_eq "  repo unchanged" "$state_before" "$(repo_state "$w/repo")"
git -C "$w/repo" checkout -q --detach
state_before="$(repo_state "$w/repo")"
DOGANY_LANG=en sh "$inst/routines/self-update.sh" > "$w/det.log" 2>&1
rc=$?
assert "detached HEAD -> die" bash -c "[ $rc -ne 0 ]"
assert "  hint printed" grep -q 'not on main' "$w/det.log"
assert_eq "  repo unchanged" "$state_before" "$(repo_state "$w/repo")"
# direct update.sh path (DO_PULL=1, channel=main) hits the same guard
DOGANY_UPDATE_CHANNEL=main DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --yes \
  > "$w/direct.log" 2>&1
rc=$?
assert "update.sh direct: detached + channel=main -> die" bash -c "[ $rc -ne 0 ]"
git -C "$w/repo" checkout -q main
DOGANY_LANG=en sh "$inst/routines/self-update.sh" > "$w/main.log" 2>&1
rc=$?
assert_eq "back on main -> update succeeds" "0" "$rc"

# ===========================================================================
CURRENT="T9"
say "T9: defect regression (multiline vrev + direction-blind lossless drift)"
w="$WORK/t9"; mkdir -p "$w"
build_tree "$w/repo"
# Scenario 1: multiline Vendor-rev -- first lines identical, new marker only
# on a continuation line (the DGN-460/541/555/558 shape). Old code compared
# grep -m1 first lines, saw "equal", then direction-blindly skipped on delta.
inst1="$w/inst1"; make_instance "$inst1" t9a "$w/repo" release; seed_manifest "$inst1"
# (make_instance's instance UPSTREAM.md already carries the identical first
# vrev line without the continuation marker)
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst1" --no-pull --yes > "$w/s1.log" 2>&1
rc=$?
assert_eq "scenario 1 exits 0" "0" "$rc"
assert_eq "scenario 1: lossless drift LANDS despite multiline vrev" \
  "$(str_sha 'DASH = "vendor-v2"')" "$(file_sha "$inst1/bridge/dashboard.py")"
# Scenario 2: pins AND vrev byte-identical (no marker bump at all) + pure
# trailing drift -- the old rsync-delta heuristic labeled this "locally
# ahead" and skipped (direction blindness).
inst2="$w/inst2"; make_instance "$inst2" t9b "$w/repo" release; seed_manifest "$inst2"
cp "$w/repo/agents/.template/bridge/UPSTREAM.md" "$inst2/bridge/UPSTREAM.md"
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst2" --no-pull --yes > "$w/s2.log" 2>&1
rc=$?
assert_eq "scenario 2 exits 0" "0" "$rc"
assert_eq "scenario 2: lossless drift LANDS despite equal pin+vrev" \
  "$(str_sha 'DASH = "vendor-v2"')" "$(file_sha "$inst2/bridge/dashboard.py")"

# ===========================================================================
CURRENT="T10"
say "T10: DGN-677 -- unmanifested bridge code adopts provisionally, never freezes"
# mint-shaped instance: bridge code on disk, NO bridge manifest (Warg shape).
w="$WORK/t10"; mkdir -p "$w"; build_tree "$w/repo"
inst="$w/inst"; make_instance "$inst" t10agent "$w/repo" release
# NO seed_manifest -> .dogany-bridge.sha absent = pristine-but-unmanifested.
# make the on-disk copy differ from template (forward-only drift, not a real
# edit): template dashboard.py = vendor-v2, put OLDER vendor-v1 bytes on disk.
printf 'DASH = "vendor-v1"\n' > "$inst/bridge/dashboard.py"   # older vendor bytes
rm -f "$inst/.claude/.dogany-bridge.sha"
M="$inst/.claude/.dogany-bridge.sha"; R="$inst/.claude/bridge-reconcile.report"

# --- run1: adopt-provisional (M seeded WITH tag, file NOT clobbered) ---
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes >"$w/r1.log" 2>&1
rc=$?
assert_eq "run1 exits 0" "0" "$rc"
assert "run1 adopts dashboard provisionally" \
  grep -q '^bridge/dashboard.py  .* #adopted-provisional$' "$M"
assert "run1 does NOT clobber (still v1 on disk)" \
  grep -q 'vendor-v1' "$inst/bridge/dashboard.py"
assert "run1 reports ADOPTED-PROVISIONAL" grep -q '^ADOPTED-PROVISIONAL bridge/dashboard.py$' "$R"
# [P3] landing-defer detection: if run2 never happens, the file stays OLD.
assert "run1 leaves file on OLD bytes (adopt != land -- next update required)" \
  bash -c "[ \"\$(file_sha '$inst/bridge/dashboard.py')\" = \"\$(str_sha 'DASH = \"vendor-v1\"')\" ]"
assert "run1 summary flags un-landed adopt" grep -qi 'adopt' "$w/r1.log"

# --- run2: I==M + vendor changed -> provisional CONFLICT ESCALATION (NOT land) ---
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes >"$w/r2.log" 2>&1
rc=$?
assert_eq "run2 exits 0 (conflict warns, never fails)" "0" "$rc"
assert "run2 ESCALATES provisional to CONFLICT (no silent land)" \
  grep -q '^CONFLICT bridge/dashboard.py$' "$R"
assert "run2 does NOT auto-land (still v1 -- genuine-edit safety)" \
  grep -q 'vendor-v1' "$inst/bridge/dashboard.py"
assert "run2 keeps provisional tag for re-eval" \
  grep -q '^bridge/dashboard.py  .* #adopted-provisional$' "$M"

# --- run3: explicit --bridge-accept -> landed (freeze broken, user-confirmed) ---
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes \
  --bridge-accept bridge/dashboard.py >"$w/r3.log" 2>&1
rc=$?
assert_eq "run3 exits 0" "0" "$rc"
assert_eq "run3 lands canonical (freeze broken via explicit accept)" \
  "$(str_sha 'DASH = "vendor-v2"')" "$(file_sha "$inst/bridge/dashboard.py")"
assert "run3 drops provisional tag (converged to normal M)" \
  bash -c "! grep -q '#adopted-provisional' '$M'"

# ===========================================================================
CURRENT="T10b"
say "T10b: DGN-677 -- adopt then vendor UNCHANGED -> class 1 converge, tag drops"
w="$WORK/t10b"; mkdir -p "$w"; build_tree "$w/repo"
inst="$w/inst"; make_instance "$inst" t10bagent "$w/repo" release
# on-disk dashboard.py already EQUALS the template (vendor-v2) but M absent ->
# class 5 on run1 (adopt-provisional), class 1 on run2 (converge, tag dropped).
printf 'DASH = "vendor-v2"\n' > "$inst/bridge/dashboard.py"
rm -f "$inst/.claude/.dogany-bridge.sha"
M="$inst/.claude/.dogany-bridge.sha"; R="$inst/.claude/bridge-reconcile.report"
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes >"$w/b1.log" 2>&1
rc=$?
assert_eq "t10b run1 exits 0" "0" "$rc"
# NOTE: on-disk == template means class 1 (in-sync) on run1, NOT class 5 -- the
# adopt path only fires when the file DIFFERS from template. So an in-sync
# unmanifested file records M plainly (no tag) and never freezes either.
assert "t10b run1 records dashboard in M (in-sync, no freeze)" \
  grep -q '^bridge/dashboard.py  ' "$M"
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes >"$w/b2.log" 2>&1
rc=$?
assert_eq "t10b run2 exits 0" "0" "$rc"
assert "t10b run2 has NO provisional tag (clean convergence)" \
  bash -c "! grep -q '#adopted-provisional' '$M'"
assert "t10b run2 no CONFLICT for dashboard" \
  bash -c "! grep -q '^CONFLICT bridge/dashboard.py$' '$R'"

# ===========================================================================
CURRENT="T11"
say "T11: DGN-677 -- mint 3e seeds bridge manifest with CODE entries (origin fix)"
# Direct unit smoke of mint.sh 3e: a minimal PROJECT_ROOT with bridge code +
# the fw_file_checksum helper; assert the produced manifest lists code files.
# (mint.sh has heavy install-time deps; exercise the 3e block in isolation.)
w="$WORK/t11"; mkdir -p "$w/proj/bridge/i18n" "$w/proj/.claude"
printf 'BOT = 1\n'      > "$w/proj/bridge/bot.py"
printf 'KO = 1\n'       > "$w/proj/bridge/i18n/ko.py"
printf 'venv-junk\n'    > "$w/proj/bridge/venv/skip.py" 2>/dev/null || \
  { mkdir -p "$w/proj/bridge/venv"; printf 'venv-junk\n' > "$w/proj/bridge/venv/skip.py"; }
printf 'ignore\n'       > "$w/proj/bridge/x.pyc"
# replicate the 3e block against this PROJECT_ROOT
PROJECT_ROOT="$w/proj"
fw_file_checksum() {
  local f="$1"
  [ -f "$f" ] || { printf '%s\n' "d41d8cd98f00b204e9800998ecf8427e-empty"; return; }
  shasum < "$f" 2>/dev/null | awk '{print $1}'
}
BRIDGE_MANIFEST="$PROJECT_ROOT/.claude/.dogany-bridge.sha"
if [ -d "$PROJECT_ROOT/bridge" ]; then
  mkdir -p "$PROJECT_ROOT/.claude"
  {
    printf '# .dogany-bridge.sha -- per-file checksums of bridge/ as last installed\n'
    printf '# by dogany-agent update.sh (DGN-593). Used for the 3-way per-file\n'
    printf '# reconcile. Format: "<relpath>  <sha>".\n'
    ( cd "$PROJECT_ROOT/bridge" && find . -type f \
        ! -name '.DS_Store' ! -name '*.pyc' ! -name '*.bak.*' \
        ! -name '*.db' ! -name '.env' \
        ! -path './.git/*' ! -path './venv/*' ! -path '*/__pycache__/*' \
        ! -path './runtime/*' ! -path './logs/*' \
        -print | sed 's|^\./||' | LC_ALL=C sort ) | while IFS= read -r _bf; do
      [ -n "$_bf" ] || continue
      printf 'bridge/%s  %s\n' "$_bf" "$(fw_file_checksum "$PROJECT_ROOT/bridge/$_bf")"
    done
  } > "$BRIDGE_MANIFEST"
fi
assert "T11 mint seed includes bridge/bot.py (code entry -- Warg origin fixed)" \
  grep -q '^bridge/bot.py  ' "$BRIDGE_MANIFEST"
assert "T11 mint seed includes bridge/i18n/ko.py (nested code entry)" \
  grep -q '^bridge/i18n/ko.py  ' "$BRIDGE_MANIFEST"
assert "T11 mint seed EXCLUDES venv (filter parity with update.sh)" \
  bash -c "! grep -q 'venv/skip.py' '$BRIDGE_MANIFEST'"
assert "T11 mint seed EXCLUDES *.pyc" \
  bash -c "! grep -q 'x.pyc' '$BRIDGE_MANIFEST'"
# assert the seed digest matches update.sh's file_checksum convention byte-for-byte
assert_eq "T11 seed digest == shasum-<file convention" \
  "$(shasum < "$w/proj/bridge/bot.py" | awk '{print $1}')" \
  "$(awk '$1=="bridge/bot.py"{print $2}' "$BRIDGE_MANIFEST")"

# ===========================================================================
CURRENT="T12"
say "T12: DGN-724 -- STALE-PRESERVE surfacing (preserved local + canonical advanced)"
# Two preserved-local files:
#   fstale: both sides changed, M != template -> canonical advanced -> STALE.
#   ffresh: local edit only, template == M     -> canonical unchanged -> NOT stale.
w="$WORK/t12"; mkdir -p "$w"
build_tree "$w/repo"   # --no-pull direct run
inst="$w/inst"; make_instance "$inst" t12agent "$w/repo" release
T="$w/repo/agents/.template/bridge"
# fstale: template advanced to 'CANON-new', local diverged to 'LOCAL', M = old canon.
printf 'CANON-new\n' > "$T/fstale.py";  printf 'LOCAL-edit\n' > "$inst/bridge/fstale.py"
# ffresh: template == M ('CANON'), local diverged to 'LOCAL' (class 3, canon unchanged).
printf 'CANON\n'     > "$T/ffresh.py";  printf 'LOCAL-edit\n' > "$inst/bridge/ffresh.py"
{
  printf '# seed\n'
  printf 'bridge/fstale.py  %s\n' "$(str_sha 'CANON-old')"
  printf 'bridge/ffresh.py  %s\n' "$(str_sha 'CANON')"
} > "$inst/.claude/.dogany-bridge.sha"
R="$inst/.claude/bridge-reconcile.report"
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes > "$w/t12.log" 2>&1
rc=$?
assert_eq "T12 run exits 0 (conflict warns, never fails)" "0" "$rc"
# fstale: preserved on disk, reported CONFLICT (existing) AND STALE-PRESERVE (new).
assert_eq "T12 fstale preserved on disk" "$(str_sha 'LOCAL-edit')" "$(file_sha "$inst/bridge/fstale.py")"
assert "T12 fstale still reports CONFLICT (existing reader unbroken)" \
  grep -q '^CONFLICT bridge/fstale.py$' "$R"
assert "T12 fstale reports STALE-PRESERVE (canonical advanced)" \
  grep -q '^STALE-PRESERVE bridge/fstale.py$' "$R"
assert "T12 fstale emits STALE-PRESERVE WARN to log" \
  grep -q 'STALE-PRESERVE.*back-land needed: bridge/fstale.py' "$w/t12.log"
# ffresh: preserved, class 3, canonical unchanged -> PRESERVED, NO STALE-PRESERVE.
assert "T12 ffresh reports PRESERVED" grep -q '^PRESERVED bridge/ffresh.py$' "$R"
assert "T12 ffresh has NO STALE-PRESERVE (canonical unchanged)" \
  bash -c "! grep -q '^STALE-PRESERVE bridge/ffresh.py$' '$R'"
assert "T12 ffresh emits NO STALE-PRESERVE WARN" \
  bash -c "! grep -q 'back-land needed: bridge/ffresh.py' '$w/t12.log'"

# ===========================================================================
CURRENT="T13"
say "T13: DGN-757 -- init-era vendor seed (no manifest) auto-lands via ancestry proof"
# Freeze reproduction: bridge files seeded by an init-time commit (DGN-625),
# NO manifest entry. Old behaviour: run1 adopt-provisional (file kept stale),
# run2 CONFLICT held forever -- the render layer never reaches the instance.
# New behaviour: on-disk bytes matching a HISTORICAL vendored blob of the
# same template path PROVE a pristine seed -> the current vendor version
# lands on run1 (backup taken); a genuine local edit (bytes in no vendor
# generation) keeps the adopt-provisional/CONFLICT path -- never clobbered.
w="$WORK/t13"; mkdir -p "$w"; build_tree "$w/repo"
T13T="$w/repo/agents/.template/bridge"
# vendor generation 1 (older release) committed to history ...
printf 'DASH = "vendor-v1"\n' > "$T13T/dashboard.py"
printf 'FC = "vendor-v1"\n'   > "$T13T/fc.py"
printf 'FEDIT = "vendor-v1"\n' > "$T13T/fedit.py"
git -C "$w/repo" init -q -b main
git -C "$w/repo" -c user.email=t@t -c user.name=t add -A
git -C "$w/repo" -c user.email=t@t -c user.name=t commit -qm gen1
# ... then vendor generation 2 (current) committed on top.
printf 'DASH = "vendor-v2"\n' > "$T13T/dashboard.py"
printf 'FC = "vendor-v2"\n'   > "$T13T/fc.py"
printf 'FEDIT = "vendor-v2"\n' > "$T13T/fedit.py"
git -C "$w/repo" -c user.email=t@t -c user.name=t add -A
git -C "$w/repo" -c user.email=t@t -c user.name=t commit -qm gen2
inst="$w/inst"; make_instance "$inst" t13agent "$w/repo" release
# init-era seed on disk = gen1 bytes (make_instance already wrote a
# vendor-v1 dashboard.py); fedit.py = GENUINE local edit (no vendor match);
# fc.py = already-frozen fleet state (Skull shape): adopted-provisional M
# entry from a previous run, on-disk still gen1 vendor bytes.
printf 'FC = "vendor-v1"\n'      > "$inst/bridge/fc.py"
printf 'FEDIT = "local-hack"\n'  > "$inst/bridge/fedit.py"
{
  printf '# seed\n'
  printf 'bridge/fc.py  %s #adopted-provisional\n' "$(str_sha 'FC = "vendor-v1"')"
} > "$inst/.claude/.dogany-bridge.sha"
M="$inst/.claude/.dogany-bridge.sha"; R="$inst/.claude/bridge-reconcile.report"
m_before="$(file_sha "$M")"

# --- dry-run: previews the ancestry landings, writes nothing ---
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes --dry-run \
  > "$w/dry.log" 2>&1
rc=$?
assert_eq "T13 dry-run exits 0" "0" "$rc"
assert "T13 dry-run previews bootstrap ancestry landing (dashboard)" \
  grep -q 'would land (bootstrap + vendor-ancestry verified): bridge/dashboard.py' "$w/dry.log"
assert "T13 dry-run previews provisional ancestry landing (fc)" \
  grep -q 'would land (provisional + vendor-ancestry verified): bridge/fc.py' "$w/dry.log"
assert_eq "T13 dry-run leaves disk untouched (dashboard still gen1)" \
  "$(str_sha 'DASH = "vendor-v1"')" "$(file_sha "$inst/bridge/dashboard.py")"
assert_eq "T13 dry-run leaves manifest untouched" "$m_before" "$(file_sha "$M")"

# --- run1: proven seeds LAND, genuine edit adopts provisionally ---
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes \
  > "$w/r1.log" 2>&1
rc=$?
assert_eq "T13 run1 exits 0" "0" "$rc"
assert_eq "T13 run1 class-5 seed LANDED (render layer reaches instance)" \
  "$(str_sha 'DASH = "vendor-v2"')" "$(file_sha "$inst/bridge/dashboard.py")"
assert "T13 run1 dashboard M seeded WITHOUT provisional tag" \
  grep -q "^bridge/dashboard.py  $(str_sha 'DASH = "vendor-v2"')$" "$M"
assert "T13 run1 dashboard reported LANDED" grep -q '^LANDED bridge/dashboard.py$' "$R"
assert "T13 run1 dashboard backed up before landing" \
  bash -c "ls '$inst/.claude/bridge-backups/bridge/'dashboard.py.user-* >/dev/null 2>&1"
assert_eq "T13 run1 frozen provisional (Skull shape) LANDED (freeze broken)" \
  "$(str_sha 'FC = "vendor-v2"')" "$(file_sha "$inst/bridge/fc.py")"
assert "T13 run1 fc provisional tag dropped (converged)" \
  bash -c "! grep -q '^bridge/fc.py .*#adopted-provisional' '$M'"
assert "T13 run1 fc reported LANDED (not CONFLICT)" \
  bash -c "grep -q '^LANDED bridge/fc.py$' '$R' && ! grep -q '^CONFLICT bridge/fc.py$' '$R'"
assert_eq "T13 run1 genuine local edit NOT clobbered" \
  "$(str_sha 'FEDIT = "local-hack"')" "$(file_sha "$inst/bridge/fedit.py")"
assert "T13 run1 genuine edit adopts provisionally (DGN-677 path kept)" \
  grep -q '^ADOPTED-PROVISIONAL bridge/fedit.py$' "$R"

# --- run2: genuine edit still escalates to CONFLICT, proven files stay in sync ---
DOGANY_LANG=en bash "$w/repo/update.sh" --root "$inst" --no-pull --yes \
  > "$w/r2.log" 2>&1
rc=$?
assert_eq "T13 run2 exits 0" "0" "$rc"
assert "T13 run2 genuine edit escalates to CONFLICT (loss actively blocked)" \
  grep -q '^CONFLICT bridge/fedit.py$' "$R"
assert_eq "T13 run2 genuine edit still on local bytes" \
  "$(str_sha 'FEDIT = "local-hack"')" "$(file_sha "$inst/bridge/fedit.py")"
assert "T13 run2 dashboard stays clean (no CONFLICT / no re-adopt)" \
  bash -c "! grep -Eq '^(CONFLICT|ADOPTED-PROVISIONAL) bridge/dashboard.py$' '$R'"

# ===========================================================================
say ""
say "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
