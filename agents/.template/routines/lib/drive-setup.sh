#!/bin/bash
# drive-setup.sh -- DGN-1655 framework-owned instance drive: render, (re)load,
# and verify the ONE scheduled unit that drives terminal-state-ledger.py sweep.
#
# Usage:
#   drive-setup.sh render  <root>   render the unit definition into <root>/routines/
#                                   from the ledger's policy query (no scheduler)
#   drive-setup.sh install <root>   render + (re)load + verify       (fail-closed)
#   drive-setup.sh verify  <root>   verify the LIVE scheduler definition only
#
# Unit: com.telegram-skill-bot.<agent>.instance-drive (launchd StartInterval on
# macOS, a systemd --user .service/.timer pair on Linux). It is not in
# plists.defer and is gated by nothing -- not KIT, LIFEKIT, MIRROR_MODULE,
# credentials or onboarding. The templates live in routines/bundle/
# (instance-drive.{plist,service,timer}.tpl); the rendered copies land in
# routines/ so the default loaders and an auditor see the rendered numbers.
#
# Policy: ONE read of `terminal-state-ledger.py policy` (the instance's own
# engine) gives ttl / period (= TTL/4, the StartInterval) / throttle
# (= period/2, DGN-1655 F1). An invalid policy fails the render (rc 2): no
# guessed or stale interval is ever kept.
#
# Verification reads the LIVE definition, never the file (DGN-1655 F3): an
# unreflected bootout + a swallowed bootstrap failure leaves the OLD job with
# its OLD StartInterval loaded while the plist on disk is new, and a
# presence-only check passes. So verify asserts, from `launchctl print`
# (systemd: `systemctl --user show`):
#   - exactly one loaded *.instance-drive label points at this root, and it is
#     this instance's label (zero / several / foreign-root -> fail)
#   - run interval == rendered period; the sweep argv carries the rendered
#     throttle and beat TTL; the drive marker is in its environment
#   - the scheduler's own run counter shows at least one run (a second,
#     ledger-independent liveness signal) within the grace window
#   - a reserved terminal-state-sweep beat no older than one period exists in
#     the ledger within the grace window (the unit has RunAtLoad /
#     OnActiveSec, so a fresh load beats at once)
#
# Sandbox refusal: without an explicit DRIVE_SETUP_LAUNCHCTL / _SYSTEMCTL
# seam, a fake HOME (unit dir is not this user's real one) or an instance
# root under a temp dir refuses every scheduler call and exits 5 -- the
# DGN-1718 lesson: `launchctl bootstrap gui/<uid>` targets the owner's REAL
# domain whatever HOME says. rc 5 means "nothing loaded, nothing verified";
# callers report it loudly and never count it as a pass.
#
# Test/env seams (never needed live):
#   DRIVE_SETUP_OS          Darwin | Linux (default: uname -s)
#   DRIVE_SETUP_LAUNCHCTL   launchctl binary (a stub in tests)
#   DRIVE_SETUP_SYSTEMCTL   systemctl binary (a stub in tests; called --user)
#   DRIVE_SETUP_LA_DIR      LaunchAgents dir (default DOGANY_LAUNCH_AGENTS_DIR
#                           or ~/Library/LaunchAgents)
#   DRIVE_SETUP_UNIT_DIR    systemd user unit dir (default ~/.config/systemd/user)
#   DRIVE_SETUP_GRACE       seconds to wait for runs/first beat (default 90)
#   DRIVE_SETUP_POLL        poll step seconds (default 3)
#
# Exit: 0 ok / 1 usage / 2 policy invalid or render failed / 3 load or
#       verification failed / 5 sandbox refusal (nothing loaded or verified)
set -u

CMD="${1:-}"; ROOT_ARG="${2:-}"
case "$CMD" in render|install|verify) ;; *)
  echo "usage: drive-setup.sh render|install|verify <instance_root>" >&2; exit 1 ;;
esac
[ -n "$ROOT_ARG" ] && [ -d "$ROOT_ARG" ] || {
  echo "usage: drive-setup.sh $CMD <instance_root> (not a directory: ${ROOT_ARG:-<none>})" >&2; exit 1; }
ROOT="$(cd "$ROOT_ARG" && pwd -P)"

info() { echo "[drive-setup] $*"; }
fail() { echo "[drive-setup][FAIL] $*" >&2; }

LEDGER="$ROOT/routines/terminal-state-ledger.py"
BUNDLE="$ROOT/routines/bundle"
OS="${DRIVE_SETUP_OS:-$(uname -s)}"
GRACE="${DRIVE_SETUP_GRACE:-90}"
POLL="${DRIVE_SETUP_POLL:-3}"
LA_DIR="${DRIVE_SETUP_LA_DIR:-${DOGANY_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}}"
UNIT_DIR="${DRIVE_SETUP_UNIT_DIR:-$HOME/.config/systemd/user}"

# Label: same derivation as routine-ctl.sh (manifest name, else dirname).
AGENT_NAME="$(grep -E '^DOGANY_AGENT_NAME=' "$ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"'"'" || true)"
AGENT_NAME="${AGENT_NAME:-$(basename "$ROOT")}"
LABEL="com.telegram-skill-bot.${AGENT_NAME}.instance-drive"

# ---------- policy (one read) ----------
TTL=""; PERIOD=""; THROTTLE=""
read_policy() {
  [ -f "$LEDGER" ] || { fail "ledger engine missing: $LEDGER"; return 2; }
  local out kv key value
  out="$(python3 "$LEDGER" policy 2>&1)" || {
    fail "policy query refused ($LEDGER policy): $out"; return 2; }
  for kv in $out; do
    case "$kv" in
      ttl=*) TTL="${kv#ttl=}" ;;
      period=*) PERIOD="${kv#period=}" ;;
      throttle=*) THROTTLE="${kv#throttle=}" ;;
    esac
  done
  for key in ttl period throttle; do
    case "$key" in ttl) value="$TTL" ;; period) value="$PERIOD" ;; throttle) value="$THROTTLE" ;; esac
    if [ -z "$value" ]; then
      fail "policy query missing input '$key' ($LEDGER policy): ${out:-<empty output>}"; return 2
    fi
    case "$value" in *[!0-9]*)
      fail "policy query invalid input '$key=$value' ($LEDGER policy)"; return 2 ;;
    esac
  done
  { [ "$TTL" -gt 0 ] && [ $((TTL % 4)) -eq 0 ] && [ "$PERIOD" -eq $((TTL / 4)) ] \
      && [ "$THROTTLE" -eq $((PERIOD / 2)) ]; } || {
    fail "policy numbers inconsistent (ttl=$TTL period=$PERIOD throttle=$THROTTLE)"; return 2; }
  return 0
}

# ---------- render ----------
render_tpl() { # <tpl> <dest>
  local tpl="$1" dest="$2" tmp
  [ -f "$tpl" ] || { fail "template missing: $tpl"; return 2; }
  tmp="${dest}.tmp.$$"
  sed -e "s#__LABEL__#${LABEL}#g" \
      -e "s#__ROOT__#${ROOT}#g" \
      -e "s#__HOMEDIR__#${HOME}#g" \
      -e "s#__PERIOD__#${PERIOD}#g" \
      -e "s#__THROTTLE__#${THROTTLE}#g" \
      -e "s#__TTL__#${TTL}#g" \
      "$tpl" > "$tmp" || { rm -f "$tmp"; fail "render failed: $dest"; return 2; }
  if grep -q '__[A-Z_]*__' "$tmp"; then
    rm -f "$tmp"; fail "unrendered token left in $dest"; return 2
  fi
  mv -f "$tmp" "$dest" || { rm -f "$tmp"; fail "cannot write $dest"; return 2; }
}

do_render() {
  read_policy || return 2
  if [ "$OS" = "Darwin" ]; then
    render_tpl "$BUNDLE/instance-drive.plist.tpl" "$ROOT/routines/${LABEL}.plist" || return 2
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$ROOT/routines/${LABEL}.plist" >/dev/null 2>&1 \
        || { fail "plist lint failed: $ROOT/routines/${LABEL}.plist"; return 2; }
    fi
  else
    render_tpl "$BUNDLE/instance-drive.service.tpl" "$ROOT/routines/${LABEL}.service" || return 2
    render_tpl "$BUNDLE/instance-drive.timer.tpl" "$ROOT/routines/${LABEL}.timer" || return 2
  fi
  info "rendered $LABEL (period=${PERIOD}s throttle=${THROTTLE}s ttl=${TTL}s)"
}

# ---------- sandbox refusal ----------
real_home() {
  local u h
  u="$(id -un 2>/dev/null)" || return 1
  case "$u" in ''|*[!A-Za-z0-9._-]*) return 1 ;; esac
  h="$(eval "printf '%s' ~$u")"
  case "$h" in /*) printf '%s' "$h" ;; *) return 1 ;; esac
}
canon_dir() { ( cd "$1" 2>/dev/null && pwd -P ) || printf '%s' "${1%/}"; }
root_is_temp() {
  local t
  for t in /tmp /private/tmp /var/folders /private/var/folders "${TMPDIR:-}"; do
    [ -n "$t" ] || continue
    t="${t%/}"
    case "$ROOT/" in "$t"/*) return 0 ;; esac
  done
  return 1
}
sandboxed() { # <seam value> <unit dir> <real-relative unit dir>
  [ -n "$1" ] && return 1
  local h; h="$(real_home)" || return 0
  [ "$(canon_dir "$2")" = "$(canon_dir "$h/$3")" ] || return 0
  root_is_temp && return 0
  return 1
}

lc() { "${DRIVE_SETUP_LAUNCHCTL:-launchctl}" "$@"; }
sc() { "${DRIVE_SETUP_SYSTEMCTL:-systemctl}" --user "$@"; }

ledger_last_beat() { python3 "$LEDGER" last-beat 2>/dev/null | tail -n1; }

# wait_until <desc> <fn>: poll fn up to GRACE seconds.
wait_until() {
  local desc="$1" fn="$2" waited=0
  while :; do
    "$fn" && return 0
    [ "$waited" -ge "$GRACE" ] && { fail "$desc (waited ${GRACE}s)"; return 1; }
    sleep "$POLL"; waited=$((waited + POLL))
  done
}

beat_fresh() {
  local last now
  last="$(ledger_last_beat)"
  case "$last" in ''|*[!0-9]*) return 1 ;; esac
  now="$(date +%s)"
  [ $((now - last)) -le "$PERIOD" ]
}

# ---------- macOS ----------
mac_print() { lc print "gui/$(id -u)/$1" 2>/dev/null; }
mac_args() { # print output on stdin -> the arguments block, one per line
  awk '/^[[:space:]]*arguments = \{/{f=1; next} f && /^[[:space:]]*\}/{exit} f {sub(/^[[:space:]]+/, ""); print}'
}
mac_runs_ok() {
  local r
  r="$(mac_print "$LABEL" | sed -n 's/^[[:space:]]*runs = \([0-9][0-9]*\).*/\1/p' | head -n1)"
  case "$r" in ''|*[!0-9]*) return 1 ;; esac
  [ "$r" -ge 1 ]
}
mac_verify() {
  local out ivl args l mine=0 others="" ok=0
  out="$(mac_print "$LABEL")" || { fail "$LABEL is not loaded (launchctl print failed)"; return 3; }
  args="$(printf '%s\n' "$out" | mac_args | tr '\n' ' ')"
  case " $args " in *" $ROOT/routines/terminal-state-ledger.py "*) ;; *)
    fail "$LABEL is loaded but its program does not point at this root ($ROOT): $args"; return 3 ;;
  esac
  ivl="$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*run interval = \([0-9][0-9]*\) seconds.*/\1/p' | head -n1)"
  [ "$ivl" = "$PERIOD" ] || {
    fail "$LABEL live run interval is '${ivl:-none}', rendered period is $PERIOD (stale definition still loaded)"; return 3; }
  case " $args " in *" sweep --throttle $THROTTLE --beat-ttl $TTL "*) ;; *)
    fail "$LABEL live argv does not carry the rendered sweep policy (--throttle $THROTTLE --beat-ttl $TTL): $args"; return 3 ;;
  esac
  printf '%s\n' "$out" | grep -q 'DOGANY_TSL_DRIVE => 1' || {
    fail "$LABEL live environment lacks the drive marker DOGANY_TSL_DRIVE=1"; return 3; }
  # exactly one loaded drive label for this root, and it is ours
  while IFS= read -r l; do
    [ -n "$l" ] || continue
    if mac_print "$l" | mac_args | grep -qxF "$ROOT/routines/terminal-state-ledger.py"; then
      mine=$((mine + 1)); [ "$l" = "$LABEL" ] || others="$others $l"
    fi
  done <<EOF_LABELS
$(lc list 2>/dev/null | awk '{print $NF}' | grep '\.instance-drive$' | LC_ALL=C sort -u)
EOF_LABELS
  [ "$mine" -eq 1 ] && [ -z "$others" ] || {
    fail "expected exactly one loaded drive for $ROOT, found $mine (extra:${others:- none})"; return 3; }
  wait_until "$LABEL shows no scheduler run (runs = 0)" mac_runs_ok || return 3
  wait_until "no terminal-state-sweep beat younger than ${PERIOD}s in the ledger" beat_fresh || return 3
  info "verified $LABEL live: run interval ${PERIOD}s, sweep --throttle $THROTTLE --beat-ttl $TTL, runs >= 1, fresh drive beat"
  return 0
}
mac_install() {
  local dest="$LA_DIR/${LABEL}.plist" uid i
  uid="$(id -u)"
  mkdir -p "$LA_DIR" || { fail "cannot create $LA_DIR"; return 3; }
  cp -f "$ROOT/routines/${LABEL}.plist" "$dest" || { fail "cannot copy to $dest"; return 3; }
  # Reload with CONFIRMED bootout (F3, measured: a first bootout can leave
  # the job listed; only a later one answers "No such process").
  for i in 1 2 3 4 5; do
    lc bootout "gui/$uid/$LABEL" >/dev/null 2>&1 || true
    mac_print "$LABEL" >/dev/null || break
    sleep 1
  done
  if mac_print "$LABEL" >/dev/null; then
    fail "bootout of $LABEL not reflected after 5 tries -- refusing to report a reload that did not happen"; return 3
  fi
  if ! lc bootstrap "gui/$uid" "$dest" >/dev/null 2>&1; then
    sleep 1
    lc bootstrap "gui/$uid" "$dest" >/dev/null 2>&1 || {
      fail "launchctl bootstrap gui/$uid $dest failed"; return 3; }
  fi
  mac_verify
}

# ---------- Linux (systemd --user) ----------
span_secs() { # systemd timespan ("30min", "1h 30min", "1800s", "1800000000us") -> seconds
  python3 - "$1" <<'PY'
import re, sys
mult = {"us": 1e-6, "usec": 1e-6, "ms": 1e-3, "msec": 1e-3, "s": 1, "sec": 1,
        "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400, "w": 604800}
tot, found = 0.0, False
for num, unit in re.findall(r"([0-9.]+)\s*([a-z]*)", sys.argv[1]):
    tot += float(num) * mult.get(unit or "s", 0); found = True
print(int(round(tot)) if found else "")
PY
}
lin_runs_ok() {
  local t
  t="$(sc show "${LABEL}.timer" -p LastTriggerUSecMonotonic --value 2>/dev/null)"
  case "$t" in ''|0|*[!0-9]*) return 1 ;; esac
  return 0
}
lin_verify() {
  local st mono ivl exe envs u mine=0 others=""
  st="$(sc show "${LABEL}.timer" -p ActiveState --value 2>/dev/null)"
  [ "$st" = "active" ] || { fail "${LABEL}.timer is not active (ActiveState=${st:-none})"; return 3; }
  mono="$(sc show "${LABEL}.timer" -p TimersMonotonic --value 2>/dev/null)"
  ivl="$(span_secs "$(printf '%s' "$mono" | sed -n 's/.*OnUnitActiveUSec=\([^;}]*\).*/\1/p')")"
  [ "$ivl" = "$PERIOD" ] || {
    fail "${LABEL}.timer live OnUnitActive interval is '${ivl:-none}', rendered period is $PERIOD (stale definition still loaded)"; return 3; }
  exe="$(sc show "${LABEL}.service" -p ExecStart --value 2>/dev/null)"
  case "$exe" in *"$ROOT/routines/terminal-state-ledger.py sweep --throttle $THROTTLE --beat-ttl $TTL"*) ;; *)
    fail "${LABEL}.service live ExecStart does not run this root's sweep with the rendered policy: $exe"; return 3 ;;
  esac
  envs="$(sc show "${LABEL}.service" -p Environment --value 2>/dev/null)"
  case " $envs " in *" DOGANY_TSL_DRIVE=1 "*) ;; *)
    fail "${LABEL}.service live environment lacks DOGANY_TSL_DRIVE=1"; return 3 ;;
  esac
  while IFS= read -r u; do
    [ -n "$u" ] || continue
    u="${u%.timer}"
    case "$(sc show "${u}.service" -p ExecStart --value 2>/dev/null)" in
      *"$ROOT/routines/terminal-state-ledger.py"*)
        mine=$((mine + 1)); [ "$u" = "$LABEL" ] || others="$others $u" ;;
    esac
  done <<EOF_UNITS
$(sc list-units --all --plain --no-legend --type=timer 2>/dev/null | awk '{print $1}' | grep '\.instance-drive\.timer$' | LC_ALL=C sort -u)
EOF_UNITS
  [ "$mine" -eq 1 ] && [ -z "$others" ] || {
    fail "expected exactly one loaded drive for $ROOT, found $mine (extra:${others:- none})"; return 3; }
  wait_until "${LABEL}.timer never triggered" lin_runs_ok || return 3
  wait_until "no terminal-state-sweep beat younger than ${PERIOD}s in the ledger" beat_fresh || return 3
  info "verified $LABEL live: OnUnitActive ${PERIOD}s, sweep --throttle $THROTTLE --beat-ttl $TTL, triggered, fresh drive beat"
  return 0
}
lin_install() {
  mkdir -p "$UNIT_DIR" || { fail "cannot create $UNIT_DIR"; return 3; }
  cp -f "$ROOT/routines/${LABEL}.service" "$UNIT_DIR/${LABEL}.service" \
    && cp -f "$ROOT/routines/${LABEL}.timer" "$UNIT_DIR/${LABEL}.timer" \
    || { fail "cannot copy units to $UNIT_DIR"; return 3; }
  sc daemon-reload >/dev/null 2>&1 || { fail "systemctl --user daemon-reload failed"; return 3; }
  sc enable "${LABEL}.timer" >/dev/null 2>&1 || { fail "systemctl --user enable ${LABEL}.timer failed"; return 3; }
  # restart, not start: an already-active timer keeps its old schedule
  # until re-armed (same F3 shape as launchd).
  sc restart "${LABEL}.timer" >/dev/null 2>&1 || { fail "systemctl --user restart ${LABEL}.timer failed"; return 3; }
  lin_verify
}

# ---------- main ----------
rc=0
case "$CMD" in
  render)
    do_render || rc=$? ;;
  install|verify)
    if [ "$CMD" = "install" ]; then
      do_render || exit $?
    else
      read_policy || exit 2
    fi
    if [ "$OS" = "Darwin" ]; then
      if sandboxed "${DRIVE_SETUP_LAUNCHCTL:-}" "$LA_DIR" "Library/LaunchAgents"; then
        fail "sandbox (fake HOME or temp instance root: $ROOT) -- refusing every launchctl call; $LABEL NOT loaded and NOT verified (DGN-1718)"
        exit 5
      fi
      if [ "$CMD" = "install" ]; then mac_install || rc=$?; else mac_verify || rc=$?; fi
    else
      if sandboxed "${DRIVE_SETUP_SYSTEMCTL:-}" "$UNIT_DIR" ".config/systemd/user"; then
        fail "sandbox (fake HOME or temp instance root: $ROOT) -- refusing every systemctl call; $LABEL NOT loaded and NOT verified (DGN-1718)"
        exit 5
      fi
      if [ "$CMD" = "install" ]; then lin_install || rc=$?; else lin_verify || rc=$?; fi
    fi
    ;;
esac
exit "$rc"
