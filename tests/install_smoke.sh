#!/bin/bash
# install_smoke.sh -- DGN-674 tier-1 fresh-install E2E smoke (S1-S11 + meta).
#
# Runs the REAL scripts/mint.sh into a throwaway sandbox (tests/lib/
# mint_fixture.sh) and hard-asserts the minted-instance contract end to end:
# placeholders, .env, lifekit schema/version, venv imports, bridge selfcheck,
# onboarding hook, service artifacts, skill surface, re-mint idempotence and
# the same-version update no-op leg. Fully automatic: fake-shape bot token,
# no launchd bootstrap, no network beyond pip (the venv build).
#
# Run it from the RELEASE checkout under test (tag content / REL_SHA):
# friends never install main HEAD -- install.sh self-pins to the latest v*
# tag (DGN-221) -- so the tree this script sits in is the tree being judged.
#
# Gate wiring (DGN-674 T4): release.sh preflight calls this script; any FAIL
# blocks the tag. promote.sh --check gains the same call when DGN-621 phase 2
# lands. Pass/fail contract: ALL stages green or exit non-zero -- the FIRST
# failing stage prints "SMOKE FAIL [Sn] <reason>" (no partial pass).
#
# Usage:
#   bash tests/install_smoke.sh [--live-connect] [--keep]
#     --live-connect  extra NON-GATE leg: boot the bridge with the fake token
#                     and assert it reaches the network boundary within ~20s.
#                     Never part of the release gate (needs open network).
#     --keep          keep the sandbox on exit (debugging).
#   SMOKE_672=1       enable the DGN-672/673 extension asserts (backup unit +
#                     backup-data.sh). Flips always-on when 672/673 land in
#                     the v1.22 RC (spec 2.2).
set -u

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$TESTS_DIR/.." && pwd)"
# shellcheck disable=SC1091
. "$TESTS_DIR/lib/mint_fixture.sh"

LIVE_CONNECT=0
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --live-connect) LIVE_CONNECT=1; shift ;;
    --keep)         KEEP=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

OS_KIND="$(uname -s)"   # Darwin | Linux
PASS=0
STAGE="S0"

say()   { printf '%s\n' "$*"; }
stage() { STAGE="$1"; say "[$1] $2"; }
ok()    { PASS=$((PASS+1)); say "  ok: $*"; }
fail()  { printf 'SMOKE FAIL [%s] %s\n' "$STAGE" "$*" >&2; exit 1; }
assert() { # assert <desc> <cmd...> -- hard: any failure aborts the smoke
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else fail "$desc"; fi
}
file_sha() { shasum < "$1" 2>/dev/null | awk '{print $1}'; }
env_mode() {
  if [ "$OS_KIND" = "Darwin" ]; then stat -f '%Lp' "$1"; else stat -c '%a' "$1"; fi
}

# Smoke-host prereqs (asserting tools, not instance deps).
for t in python3 sqlite3 rsync git; do
  command -v "$t" >/dev/null 2>&1 || { STAGE=PRE; fail "smoke host missing tool: $t"; }
done

# ---------------------------------------------------------------------------
# S2 scanners -- functions so the meta self-test can re-invoke them.
# ---------------------------------------------------------------------------
# Runtime-template tokens that LEGITIMATELY survive a mint: they are consumed
# at runtime by their own tooling (dogany-cron-register plist templates,
# routines/lib/routine-ctl.sh + bundle routine.plist.tpl slot tokens,
# claude-usage.sh log format), never by mint substitution. EXACT-NAME
# allowlist on purpose: a NEW framework token would not match and is caught.
ALLOW_TOKENS='__(ROOT|MINUTE|HOUR|HOMEDIR|NAME|LOGNAME|WEEKDAY_ENTRY|SCRIPT|LABEL|PROMPT|PATH|HTTP_STATUS)__'

scan_placeholders() { # <inst> ; prints survivors + returns 1 when any exist
  local inst="$1" hits
  hits="$(grep -rIonE '__[A-Z][A-Z_]*__' "$inst" \
            --exclude-dir=venv --exclude-dir=.git 2>/dev/null \
          | grep -vE ":${ALLOW_TOKENS}\$" || true)"
  if [ -n "$hits" ]; then printf '%s\n' "$hits"; return 1; fi
  return 0
}

scan_angle() { # <inst> ; baseline md <UPPER_SNAKE> placeholders
  local inst="$1" f hits=""
  for f in AGENT.md RULES.md USER.md CLAUDE.md; do
    [ -f "$inst/$f" ] || continue
    if grep -qE '<[A-Z][A-Z_]+>' "$inst/$f" 2>/dev/null; then
      hits="$hits $f"
    fi
  done
  if [ -n "$hits" ]; then printf 'angle survivors in:%s\n' "$hits"; return 1; fi
  return 0
}

# ---------------------------------------------------------------------------
# Sandbox + S1: REAL mint exits 0
# ---------------------------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn674-smoke.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
cleanup() {
  if [ "$KEEP" = "1" ]; then
    say "[keep] sandbox preserved at $WORK"
  else
    mint_fixture_cleanup
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

stage S1 "real mint.sh exits 0 (core-only, fake token, sandbox HOME)"
mint_sandbox "$WORK" || fail "mint.sh non-zero (see log above)"
INST="$MINT_INST"
ok "minted at $INST"

# ---------------------------------------------------------------------------
# S2: placeholder survivors = 0 (generic scan, whole instance)
# ---------------------------------------------------------------------------
stage S2 "placeholder survivors (generic __[A-Z_]*__ + baseline angle scan)"
scan_placeholders "$INST" || fail "generic __X__ survivors (listing above)"
ok "no __X__ survivors outside the runtime-template allowlist"
scan_angle "$INST" || fail "angle-bracket survivors in baseline markdown"
ok "no <UPPER_SNAKE> survivors in baseline markdown"

# ---------------------------------------------------------------------------
# S3: .env contract
# ---------------------------------------------------------------------------
stage S3 ".env contract (existence, 0600, identity lines)"
ENV_FILE="$INST/.telegram_bot/.env"
assert ".env exists" test -f "$ENV_FILE"
[ "$(env_mode "$ENV_FILE")" = "600" ] || fail ".env mode is $(env_mode "$ENV_FILE"), want 600"
ok ".env mode 0600"
assert "TELEGRAM_BOT_TOKEN line" grep -q "^TELEGRAM_BOT_TOKEN=$MINT_FAKE_TOKEN\$" "$ENV_FILE"
assert "ALLOWED_USER_IDS born-lock" grep -q '^ALLOWED_USER_IDS=11111111$' "$ENV_FILE"
assert "LOCALE=ko" grep -q '^LOCALE=ko$' "$ENV_FILE"
assert "TZ=Asia/Seoul" grep -q '^TZ=Asia/Seoul$' "$ENV_FILE"

# ---------------------------------------------------------------------------
# S4: lifekit.db + version lockstep
# ---------------------------------------------------------------------------
stage S4 "lifekit.db schema + user_version lockstep"
LIFEKIT_DB="$INST/database/lifekit.db"
assert "lifekit.db exists (sqlite3 skip path = FAIL, F7)" test -f "$LIFEKIT_DB"
sqlite3 "$WORK/ref.db" < "$INST/database/schema.sql" 2>/dev/null \
  || fail "schema.sql does not load into a reference db"
tbl_set() { sqlite3 "$1" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"; }
[ "$(tbl_set "$LIFEKIT_DB")" = "$(tbl_set "$WORK/ref.db")" ] \
  || fail "lifekit.db table set != schema.sql table set"
ok "table set matches schema.sql"
UV="$(sqlite3 "$LIFEKIT_DB" 'PRAGMA user_version;')"
EXP="$(python3 - "$INST/database/lifekit.py" <<'PY'
import re, sys
m = re.search(r'^EXPECTED_USER_VERSION\s*=\s*([0-9]+)', open(sys.argv[1]).read(), re.M)
sys.exit(1) if not m else print(m.group(1))
PY
)" || fail "EXPECTED_USER_VERSION not parseable from database/lifekit.py"
MINV="$(python3 - "$INST/mirror/sdk_bridge.py" <<'PY'
import re, sys
m = re.search(r'^MIN_USER_VERSION\s*=\s*([0-9]+)', open(sys.argv[1]).read(), re.M)
sys.exit(1) if not m else print(m.group(1))
PY
)" || fail "MIN_USER_VERSION not parseable from mirror/sdk_bridge.py"
[ "$UV" = "$EXP" ] || fail "PRAGMA user_version $UV != lifekit.py EXPECTED_USER_VERSION $EXP (schema.sql stamp drift?)"
ok "user_version $UV == EXPECTED_USER_VERSION"
[ "$UV" -ge "$MINV" ] || fail "user_version $UV < mirror MIN_USER_VERSION $MINV"
ok "user_version $UV >= mirror MIN_USER_VERSION $MINV"

# ---------------------------------------------------------------------------
# S5: venv import surface (PROJECT_ROOT env contract, spec 2.3-1)
# ---------------------------------------------------------------------------
stage S5 "venv imports (bridge, bot, sdk_bridge, config)"
VENV_PY="$INST/bridge/venv/bin/python"
assert "venv python exists" test -x "$VENV_PY"
( cd "$INST" && PROJECT_ROOT="$INST" PYTHONPATH="$INST" \
    "$VENV_PY" -c "import bridge, bridge.bot, bridge.sdk_bridge, bridge.config" ) \
  >"$WORK/s5.log" 2>&1 || { tail -15 "$WORK/s5.log" >&2; fail "bridge import failed (dep drift? F2)"; }
ok "all four modules import under the instance venv"

# ---------------------------------------------------------------------------
# S6: bridge --selfcheck (offline, deterministic)
# ---------------------------------------------------------------------------
stage S6 "bridge --selfcheck --path"
S6_OUT="$(PYTHONPATH="$INST" "$VENV_PY" -m bridge --selfcheck --path "$INST" 2>&1)" \
  || { printf '%s\n' "$S6_OUT" >&2; fail "selfcheck exited non-zero"; }
printf '%s' "$S6_OUT" | grep -q 'selfcheck ok' || fail "selfcheck output unexpected: $S6_OUT"
ok "selfcheck ok"

# ---------------------------------------------------------------------------
# S7: onboarding hook (startup ctx, ko) + lifekit offer leg
# ---------------------------------------------------------------------------
stage S7 "SessionStart onboarding hook + lifekit offer"
hook_run() {
  printf '{"source":"startup","cwd":"%s"}' "$INST" \
    | ( cd "$INST" && /usr/bin/python3 routines/onboarding-check.py )
}
H1="$(hook_run)" || fail "onboarding hook crashed"
printf '%s' "$H1" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ctx = d["hookSpecificOutput"]["additionalContext"]
assert ctx.strip(), "empty additionalContext"
assert any("가" <= c <= "힣" for c in ctx), "context not Korean"
' || fail "onboarding ctx missing/empty/not-ko"
ok "onboarding ctx present and Korean (mint --lang ko honored)"
# Leg 2: onboarding done + LIFEKIT=pending -> lifekit offer fires (class
# defaults to main when DOGANY_AGENT_CLASS is absent -- spec 2.3-6, KEEP).
python3 - "$INST/AGENT.md" <<'PY' || fail "ONBOARDING_PENDING marker not found in AGENT.md"
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
m = "<!-- ONBOARDING_PENDING -->"
assert m in s
open(p, "w", encoding="utf-8").write(s.replace(m, ""))
PY
assert "LIFEKIT=pending scaffolded" grep -q '^LIFEKIT=pending$' "$INST/config/lifekit.conf"
H2="$(hook_run)" || fail "onboarding hook crashed on lifekit leg"
printf '%s' "$H2" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ctx = d["hookSpecificOutput"]["additionalContext"]
assert "lifekit" in ctx.lower(), "no lifekit offer in ctx"
' || fail "lifekit offer ctx did not fire (class gate? pending state?)"
ok "lifekit offer ctx fires after onboarding (bare-mint class defaults main)"

# ---------------------------------------------------------------------------
# S8: service/routine artifacts (lint, rename, defer, PATH, watchdog)
# ---------------------------------------------------------------------------
stage S8 "service artifacts (plist lint / rename / defer / PATH)"
plist_lint() {
  if [ "$OS_KIND" = "Darwin" ]; then
    plutil -lint -s "$1" >/dev/null 2>&1
  else
    python3 -c 'import plistlib, sys; plistlib.load(open(sys.argv[1], "rb"))' "$1" >/dev/null 2>&1
  fi
}
PLIST_N=0
for p in "$INST"/bridge/*.plist "$INST"/routines/*.plist; do
  [ -e "$p" ] || continue
  PLIST_N=$((PLIST_N+1))
  plist_lint "$p" || fail "plist lint failed: $p"
done
[ "$PLIST_N" -gt 0 ] || fail "no plists found in instance"
ok "all $PLIST_N plists lint clean"
NEWBRIDGE="$INST/bridge/com.telegram-skill-bot.smokebot.newbridge.plist"
assert "bridge plist renamed to smokebot" test -f "$NEWBRIDGE"
assert "watchdog plist present + renamed" \
  test -f "$INST/bridge/com.telegram-skill-bot.smokebot.watchdog.plist"
if find "$INST/bridge" "$INST/routines" -name '*telegram-agent*' 2>/dev/null | grep -q .; then
  fail "unrenamed telegram-agent unit files remain"
fi
ok "no telegram-agent filenames remain"
while IFS= read -r line; do
  case "$line" in ''|\#*) continue ;; esac
  [ -f "$INST/routines/$line" ] || fail "plists.defer entry has no file: $line"
  case "$line" in
    *smokebot*) : ;;
    *) fail "plists.defer entry not renamed: $line" ;;
  esac
done < "$INST/routines/plists.defer"
ok "plists.defer entries renamed and present"
assert "newbridge PATH carries \$HOME/.local/bin (F9)" \
  grep -q "$HOME/.local/bin" "$NEWBRIDGE"
assert "start.sh PATH carries .local/bin (F9)" \
  grep -q '\$HOME/.local/bin' "$INST/bridge/start.sh"
if [ "$OS_KIND" = "Linux" ]; then
  for u in mirror-poll.service mirror-poll.timer mirror-reconcile.service mirror-reconcile.timer; do
    assert "systemd unit renamed: $u" \
      test -f "$INST/routines/com.telegram-skill-bot.smokebot.$u"
  done
fi
if [ "${SMOKE_672:-0}" = "1" ]; then
  # DGN-672 extension (flips always-on at RC merge): backup schedule unit
  # exists, is renamed, and is NOT deferred.
  BK_UNIT="$(find "$INST/routines" -name '*smokebot*backup*' \( -name '*.plist' -o -name '*.timer' \) 2>/dev/null | head -1)"
  [ -n "$BK_UNIT" ] || fail "672: no backup schedule unit found"
  grep -q "$(basename "$BK_UNIT")" "$INST/routines/plists.defer" \
    && fail "672: backup unit is in plists.defer (must load by default)"
  ok "672: backup unit present, renamed, not deferred"
fi

# ---------------------------------------------------------------------------
# S9: skill surface + hook wiring
# ---------------------------------------------------------------------------
stage S9 "skill surface (deref) + settings.json hook targets"
for sk in dogany-user-onboarding dogany-reminder; do
  d="$INST/.claude/skills/$sk"
  [ -d "$d" ] && [ ! -L "$d" ] || fail "$sk not a real dir (symlink survived deref?)"
  [ -f "$d/SKILL.md" ] && [ ! -L "$d/SKILL.md" ] || fail "$sk/SKILL.md not a real file"
done
ok "framework skills are real (dereferenced) files"
python3 - "$INST" <<'PY' || fail "settings.json hooks reference missing scripts"
import json, os, sys
inst = sys.argv[1]
cfg = json.load(open(os.path.join(inst, ".claude", "settings.json")))
refs, missing = 0, []
for groups in (cfg.get("hooks") or {}).values():
    for g in groups:
        for hk in g.get("hooks", []):
            for tok in hk.get("command", "").split():
                path = tok.split("=", 1)[1] if ("=" in tok and not tok.startswith(inst)) else tok
                if path.startswith(inst):
                    refs += 1
                    if not os.path.exists(path):
                        missing.append(path)
assert refs > 0, "no instance-rooted hook references (substitution failed?)"
if missing:
    print("\n".join(missing), file=sys.stderr)
    sys.exit(1)
PY
ok "every instance-rooted hook command points at a real script"
if [ "${SMOKE_672:-0}" = "1" ]; then
  assert "672: backup-data.sh present + executable" \
    test -x "$INST/routines/backup-data.sh"
fi

# ---------------------------------------------------------------------------
# S10: re-mint idempotence (keep-if-present contract)
# ---------------------------------------------------------------------------
stage S10 "re-mint --force keeps identity/config/db/.env"
KEEP_FILES="AGENT.md USER.md config/agent.conf config/lifekit.conf config/secret-patterns.conf database/lifekit.db .telegram_bot/.env"
BEFORE=""
for f in $KEEP_FILES; do
  [ -f "$INST/$f" ] || fail "keep-list file missing before re-mint: $f"
  BEFORE="$BEFORE $f=$(file_sha "$INST/$f")"
done
mint_remint || fail "re-mint (--force) non-zero"
AFTER=""
for f in $KEEP_FILES; do
  AFTER="$AFTER $f=$(file_sha "$INST/$f")"
done
[ "$BEFORE" = "$AFTER" ] || fail "re-mint changed keep-if-present files: before[$BEFORE] after[$AFTER]"
ok "re-mint left all keep-if-present files byte-identical"

# ---------------------------------------------------------------------------
# S11: same-version update no-op leg
# ---------------------------------------------------------------------------
stage S11 "update.sh --no-pull --yes = clean no-op on a fresh mint"
SHA_AGENT="$(file_sha "$INST/AGENT.md")"
SHA_USER="$(file_sha "$INST/USER.md")"
SHA_ENV="$(file_sha "$ENV_FILE")"
SHA_DB="$(file_sha "$LIFEKIT_DB")"
cp "$ENV_FILE" "$WORK/env.before"
bash "$REPO_ROOT/update.sh" --root "$INST" --no-pull --yes >"$WORK/update.log" 2>&1 \
  || { tail -30 "$WORK/update.log" >&2; fail "update.sh non-zero"; }
ok "update.sh exit 0"
[ "$(file_sha "$INST/AGENT.md")" = "$SHA_AGENT" ] || fail "update changed AGENT.md"
[ "$(file_sha "$INST/USER.md")" = "$SHA_USER" ] || fail "update changed USER.md"
ok "identity files byte-identical"
if [ "$(file_sha "$ENV_FILE")" = "$SHA_ENV" ]; then
  ok ".env byte-identical"
else
  # Designed exception (grounded deviation from the locked spec's "sha
  # unchanged"): update.sh DELIBERATELY backfills BRIDGE_MODELS into a .env
  # that lacks the key (DGN-590 fail-open legacy path; on a probe-less host
  # every fresh core mint hits it). Accept EXACTLY that additive backfill;
  # any other .env mutation (token/owner/locale/tz or deletions) still fails.
  ENV_DIFF="$(diff "$WORK/env.before" "$ENV_FILE" | grep -E '^[<>]' \
                | grep -vE '^> (#|BRIDGE_MODELS=)' || true)"
  [ -z "$ENV_DIFF" ] || { printf '%s\n' "$ENV_DIFF" >&2; fail "update changed .env beyond the BRIDGE_MODELS backfill"; }
  ok ".env changed only by the designed BRIDGE_MODELS backfill (DGN-590)"
fi
[ "$(file_sha "$LIFEKIT_DB")" = "$SHA_DB" ] || fail "update changed lifekit.db"
ok "lifekit.db untouched"
REPO_VER="$(head -n1 "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
STAMP="$(grep -E '^DOGANY_FW_VERSION=' "$INST/.instance.conf" | head -1 | cut -d= -f2)"
[ "$STAMP" = "$REPO_VER" ] || fail ".instance.conf DOGANY_FW_VERSION='$STAMP' != repo VERSION '$REPO_VER'"
ok "DOGANY_FW_VERSION stamped: $STAMP"

# ---------------------------------------------------------------------------
# META: smoke-of-the-smoke -- planted survivor must turn S2 red (1 mutation)
# ---------------------------------------------------------------------------
stage META "planted __PROJECT_ROOT__ survivor is caught by the S2 scanner"
PROBE="$INST/meta-survivor-probe.md"
printf 'meta probe: __PROJECT_ROOT__ must be caught\n' > "$PROBE"
if scan_placeholders "$INST" >/dev/null 2>&1; then
  rm -f "$PROBE"
  fail "meta: planted survivor NOT caught -- scanner is blind"
fi
rm -f "$PROBE"
ok "scanner catches a planted survivor"

# ---------------------------------------------------------------------------
# Optional non-gate leg: --live-connect (fake-token boot-to-network)
# ---------------------------------------------------------------------------
if [ "$LIVE_CONNECT" = "1" ]; then
  stage S6b "live-connect: bridge boots to the network boundary (<=20s)"
  LC_LOG="$WORK/liveconnect.log"
  ( cd "$INST" && PYTHONPATH="$INST" "$VENV_PY" -m bridge --path "$INST" ) \
    >"$LC_LOG" 2>&1 &
  LC_PID=$!
  FOUND=0
  i=0
  while [ "$i" -lt 20 ]; do
    if grep -qiE 'invalid.*token|unauthorized|getme|telegram\.error|httpx|network' \
         "$LC_LOG" "$INST/.telegram_bot/logs/"*.log 2>/dev/null; then
      FOUND=1; break
    fi
    kill -0 "$LC_PID" 2>/dev/null || break
    sleep 1; i=$((i+1))
  done
  kill "$LC_PID" 2>/dev/null; wait "$LC_PID" 2>/dev/null
  if [ "$FOUND" = "1" ]; then
    ok "bridge reached the network/auth boundary with the fake token"
  elif grep -qiE 'invalid.*token|unauthorized|getme|telegram\.error|httpx|network' \
         "$LC_LOG" "$INST/.telegram_bot/logs/"*.log 2>/dev/null; then
    ok "bridge reached the network/auth boundary (post-exit log)"
  else
    tail -20 "$LC_LOG" >&2
    fail "no network-boundary evidence within 20s"
  fi
fi

say ""
say "SMOKE OK: all stages green ($PASS asserts)"
exit 0
