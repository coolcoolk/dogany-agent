#!/usr/bin/env bash
# setup.sh -- dogany-computer-use backend setup (DGN-1972). macOS only.
#
# DRY BY DEFAULT: every verb prints its plan and changes nothing. Add
# --apply to act. Idempotent: a step already in the wanted state reports
# state=done and is skipped, so re-running is always safe.
#
#   setup.sh [plan]            install plan: app, CLI link, policy, MCP.
#   setup.sh install --apply   run the install plan.
#   --workspace <ROOT>         agent workspace the MCP server is registered
#                              for (Claude Code local scope + Codex project
#                              config). Default: $DOGANY_ROOT, else the
#                              instance root when run from
#                              <ROOT>/.claude/skills/dogany-computer-use.
#                              Never user scope: other agents on the Mac
#                              must not get the tools.
#   setup.sh policy --apps <bundle-id,...> [--apply]
#                              set the app scope (bounded manifest).
#   setup.sh grant [--apply]   ask macOS for Accessibility + Screen Recording
#                              (prompts attributed to CuaDriver.app).
#   setup.sh start [--apply]   (re)start the daemon in bounded mode, then
#                              verify it runs (cua-driver status).
#   setup.sh status            alias of plan (read-only).
#
# Pinned backend: Cua Driver (MIT), release below. Integrity = two checks
# that must both pass before anything lands in the apps folder:
#   1. SHA-256 of the downloaded archive == CU_SHA256 pinned here.
#   2. CuaDriver.app satisfies the code requirement: Apple-anchored
#      Developer ID, identifier com.trycua.driver, team CU_TEAM_ID.
# Bumping the release = edit the four CU_* values in one commit + rerun tests.
# The upstream installer and `cua-driver skills install` are NOT used:
# Dogany owns the skill text and the policy.
#
# Linux: pilot-pending (computer-use-linux inside an isolated virtual
# desktop). This script exits 3 on any non-Darwin host.
#
# Env (tests + unusual layouts): CU_APP_DIR (default /Applications),
# CU_BIN_DIR (default ~/.local/bin), DOGANY_CU_HOME (default
# ~/.dogany/computer-use), CU_START_WAIT (seconds to wait for the daemon,
# default 10).
#
# Exit: 0 ok | 2 usage | 3 not macOS | 4 macOS too old | 5 missing tool |
#       6 integrity fail | 7 identity fail | 8 step failed
set -u

CU_VERSION="0.34.1"
CU_TAG="cua-driver-rs-v0.34.1"
CU_ASSET="cua-driver-rs-0.34.1-darwin-universal.tar.gz"
CU_SHA256="4c7edf1ebbad7e4bc9fac90a4dc4f50d544592e00b35f15b681ccf8b41cb4ff1"
CU_TEAM_ID="YCK386LBJ7"
CU_BUNDLE_ID="com.trycua.driver"
CU_URL="https://github.com/trycua/cua/releases/download/$CU_TAG/$CU_ASSET"
CU_REQ="anchor apple generic and identifier \"$CU_BUNDLE_ID\" and certificate leaf[subject.OU] = \"$CU_TEAM_ID\""
MIN_MACOS=14
DEFAULT_APPS="com.apple.calculator"

APP_DIR="${CU_APP_DIR:-/Applications}"
APP="$APP_DIR/CuaDriver.app"
APP_BIN="$APP/Contents/MacOS/cua-driver"
BIN_DIR="${CU_BIN_DIR:-$HOME/.local/bin}"
BIN="$BIN_DIR/cua-driver"
CU_HOME="${DOGANY_CU_HOME:-$HOME/.dogany/computer-use}"
APPS_CONF="$CU_HOME/apps.conf"
MANIFEST="$CU_HOME/manifest.yaml"

# Reviewed tools. The manifest allow list = this list INTERSECTED with what
# the installed driver reports (`cua-driver list-tools`): the daemon refuses
# to start on any tool it does not know (0.34.1 has no run_actions).
# Left out on purpose: kill_app, clipboard_*, recording, browser_*, page,
# set_config, install_*, replay_trajectory, get_desktop_state (needs
# desktop.display).
REVIEWED_TOOLS="list_apps list_windows get_window_state get_accessibility_tree
verify_state parse_visual_regions get_screen_size get_cursor_position zoom
start_session end_session get_session list_sessions check_permissions
health_report launch_app bring_to_front set_window_frame invoke_menu click
double_click right_click scroll move_cursor drag type_text press_key hotkey
set_value run_actions"

START_WAIT="${CU_START_WAIT:-10}"
MCP_NAME="cua-driver"

VERB="plan"
APPLY=0
APPS_ARG=""
WS_ARG=""
TODO=0

usage() { sed -n '2,28p' "$0" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    plan|install|policy|grant|start|status) VERB="$1" ;;
    --apply) APPLY=1 ;;
    --apps) shift; APPS_ARG="${1:-}" ;;
    --apps=*) APPS_ARG="${1#*=}" ;;
    --workspace) shift; WS_ARG="${1:-}" ;;
    --workspace=*) WS_ARG="${1#*=}" ;;
    -h|--help) usage ;;
    *) echo "error=unknown argument $1" >&2; usage ;;
  esac
  shift
done
[ "$VERB" = "status" ] && VERB="plan"
[ "$VERB" = "plan" ] && [ "$APPLY" = 1 ] && VERB="install"

if [ "$APPLY" = 1 ]; then echo "mode=apply"; else echo "mode=dry"; fi

# ------------------------------------------------------------------ helpers

step() {  # id state detail
  echo "step=$1 state=$2 detail=$3"
  case "$2" in todo) TODO=$((TODO + 1)) ;; esac
}
act() {  # describe + run only under --apply
  echo "action=$*"
  [ "$APPLY" = 1 ] || return 0
  "$@"
}
die() { echo "error=$2" >&2; echo "result=fail code=$1"; exit "$1"; }

app_version() {  # $1 = app path
  plutil -extract CFBundleShortVersionString raw -o - \
    "$1/Contents/Info.plist" 2>/dev/null
}
app_identity_ok() {  # $1 = app path
  codesign --verify --deep --strict -R="$CU_REQ" "$1" >/dev/null 2>&1
}

apps_list() {  # current scope, one bundle id per line
  if [ -f "$APPS_CONF" ]; then
    grep -E '^[A-Za-z0-9][A-Za-z0-9.-]*$' "$APPS_CONF"
  else
    printf '%s\n' $DEFAULT_APPS
  fi
}

driver_tools() {  # tool names the installed driver reports, one per line
  local raw
  raw="$("$APP_BIN" list-tools 2>/dev/null)" || return 1
  if printf '%s\n' "$raw" | grep -q '"name"'; then
    printf '%s\n' "$raw" \
      | grep -oE '"name"[[:space:]]*:[[:space:]]*"[a-z][a-z0-9_]*"' \
      | sed -E 's/.*"([a-z][a-z0-9_]*)"$/\1/'
  else
    printf '%s\n' "$raw" | sed -E 's/^[[:space:]]*([-*][[:space:]]+)?//' \
      | awk '{print $1}' | sed -E 's/[:,]$//' | grep -E '^[a-z][a-z0-9_]*$'
  fi
}

allow_tools() {  # -> ALLOW (reviewed AND reported), DROPPED; 1 = no list
  local have t
  ALLOW=""; DROPPED=""
  have="$(driver_tools)" || return 1
  [ -n "$have" ] || return 1
  for t in $REVIEWED_TOOLS; do
    if printf '%s\n' "$have" | grep -qx "$t"; then
      ALLOW="$ALLOW $t"
    else
      DROPPED="$DROPPED $t"
    fi
  done
  ALLOW="${ALLOW# }"; DROPPED="${DROPPED# }"
  [ -n "$ALLOW" ]
}

render_manifest() {  # stdin = bundle ids, $ALLOW = tools -> manifest yaml
  echo "# generated by dogany-computer-use setup.sh; edit via: setup.sh policy"
  echo "version: 3"
  echo "expires_after: 12h"
  echo "idle_timeout: 30m"
  echo "allow:"
  echo "  tools:"
  for t in $ALLOW; do echo "    - $t"; done
  echo "resources:"
  echo "  apps:"
  while read -r b; do
    [ -n "$b" ] || continue
    echo "    - bundle_id: $b"
    echo "      launch: true"
    echo "      windows: all"
    echo "      terminate: driver_launched"
  done
  echo "  desktop:"
  echo "    display: false"
}

# ------------------------------------------------------------------ gates

os="$(uname -s 2>/dev/null || echo unknown)"
echo "platform=$os"
if [ "$os" != "Darwin" ]; then
  echo "verdict=pilot-pending"
  echo "note=Linux path (computer-use-linux in an isolated virtual desktop) is not shipped yet"
  exit 3
fi
mac="$(sw_vers -productVersion 2>/dev/null || echo 0)"
echo "macos=$mac"
mac_major="${mac%%.*}"
case "$mac_major" in ''|*[!0-9]*) mac_major=0 ;; esac
if [ "$mac_major" -lt "$MIN_MACOS" ]; then
  die 4 "macOS $MIN_MACOS or newer required"
fi
for t in curl shasum tar ditto codesign plutil; do
  command -v "$t" >/dev/null 2>&1 || die 5 "missing tool: $t"
done
echo "release=$CU_VERSION asset=$CU_ASSET team=$CU_TEAM_ID"

# ------------------------------------------------------------------ steps

check_app() {  # -> sets APP_STATE done|todo + APP_WHY
  if [ ! -d "$APP" ]; then APP_STATE=todo; APP_WHY="absent"; return; fi
  local v; v="$(app_version "$APP")"
  if [ "$v" != "$CU_VERSION" ]; then
    APP_STATE=todo; APP_WHY="installed ${v:-unknown}, want $CU_VERSION"; return
  fi
  if ! app_identity_ok "$APP"; then
    APP_STATE=todo; APP_WHY="identity check failed, reinstall"; return
  fi
  APP_STATE=done; APP_WHY="$CU_VERSION identity ok"
}

install_app() {
  CU_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dogany-cu.XXXXXX")" || die 8 "mktemp"
  trap 'rm -rf "$CU_TMP"' EXIT
  local tmp="$CU_TMP"
  curl -fsSL --proto '=https' -o "$tmp/$CU_ASSET" "$CU_URL" \
    || die 8 "download failed"
  local got; got="$(shasum -a 256 "$tmp/$CU_ASSET" | awk '{print $1}')"
  if [ "$got" != "$CU_SHA256" ]; then
    die 6 "sha256 mismatch: got $got want $CU_SHA256"
  fi
  echo "verified=sha256"
  mkdir -p "$tmp/x" && tar -xzf "$tmp/$CU_ASSET" -C "$tmp/x" \
    || die 8 "extract failed"
  local src; src="$(find "$tmp/x" -maxdepth 2 -name CuaDriver.app -type d | head -1)"
  [ -n "$src" ] || die 7 "archive has no CuaDriver.app"
  [ "$(app_version "$src")" = "$CU_VERSION" ] || die 7 "archive app version mismatch"
  app_identity_ok "$src" || die 7 "code requirement not met (team $CU_TEAM_ID)"
  echo "verified=identity"
  ditto "$src" "$APP.dogany-new" || die 8 "copy failed"
  if [ -d "$APP" ]; then
    mkdir -p "$CU_HOME/backup"
    mv "$APP" "$CU_HOME/backup/CuaDriver.app.$(date +%Y%m%d%H%M%S)" \
      || die 8 "could not move old app aside"
    echo "note=replaced app; re-check permissions (grant) after start"
  fi
  mv "$APP.dogany-new" "$APP" || die 8 "install move failed"
}

plan_install() {
  check_app
  step app "$APP_STATE" "$APP_WHY"
  if [ "$APP_STATE" = todo ]; then
    echo "action=download $CU_URL"
    echo "action=verify sha256 $CU_SHA256"
    echo "action=verify code requirement team=$CU_TEAM_ID id=$CU_BUNDLE_ID"
    echo "action=install $APP"
    [ "$APPLY" = 1 ] && install_app
  fi

  if [ -L "$BIN" ] && [ "$(readlink "$BIN")" = "$APP_BIN" ]; then
    step cli done "$BIN -> app"
  else
    step cli todo "link $BIN -> $APP_BIN"
    if [ "$APPLY" = 1 ]; then
      mkdir -p "$BIN_DIR" && ln -sfn "$APP_BIN" "$BIN" || die 8 "link failed"
      echo "action=linked $BIN"
    fi
  fi

  plan_policy

  plan_mcp
}

# MCP: per agent workspace, never user scope (DGN-1972 r2). Claude Code =
# local scope run from the workspace; Codex = workspace .codex/config.toml.
# A user-scope registration from an older setup is removed.
resolve_ws() {
  WS="${WS_ARG:-${DOGANY_ROOT:-}}"
  if [ -z "$WS" ]; then
    case "$0" in
      */.claude/skills/dogany-computer-use/setup.sh)
        WS="${0%/.claude/skills/dogany-computer-use/setup.sh}" ;;
    esac
  fi
  [ -n "$WS" ] || return 0
  [ -d "$WS" ] || die 2 "workspace not a directory: $WS"
  WS="$(cd "$WS" && pwd)"
}

claude_scope() {  # $1 = dir -> scope word of the visible registration
  (cd "$1" && claude mcp get "$MCP_NAME" 2>/dev/null) \
    | sed -nE 's/^[[:space:]]*Scope:[[:space:]]*([A-Za-z]+).*/\1/p' \
    | head -1 | tr 'A-Z' 'a-z'
}

plan_mcp() {
  resolve_ws
  local neutral sc toml
  if command -v claude >/dev/null 2>&1; then
    neutral="$(mktemp -d "${TMPDIR:-/tmp}/dogany-cu-ns.XXXXXX")"
    sc="$(claude_scope "$neutral")"
    rmdir "$neutral" 2>/dev/null
    if [ "$sc" = user ]; then
      step mcp-claude-user todo "remove user-scope $MCP_NAME (every agent saw it)"
      act claude mcp remove --scope user "$MCP_NAME" \
        || die 8 "claude mcp remove failed"
    fi
    if [ -z "$WS" ]; then
      step mcp-claude skip "no agent workspace (pass --workspace <ROOT>)"
    elif [ "$(claude_scope "$WS")" = local ] \
        || [ "$(claude_scope "$WS")" = project ]; then
      step mcp-claude done "$MCP_NAME registered for $WS"
    else
      step mcp-claude todo "register $MCP_NAME -> $BIN mcp (local scope, $WS)"
      echo "action=cd $WS && claude mcp add --scope local --transport stdio $MCP_NAME -- $BIN mcp"
      if [ "$APPLY" = 1 ]; then
        (cd "$WS" && claude mcp add --scope local --transport stdio \
          "$MCP_NAME" -- "$BIN" mcp) || die 8 "claude mcp add failed"
      fi
    fi
  else
    step mcp-claude skip "claude not installed"
  fi

  if command -v codex >/dev/null 2>&1; then
    if codex mcp get "$MCP_NAME" >/dev/null 2>&1; then
      step mcp-codex-user todo "remove user-level $MCP_NAME (every agent saw it)"
      act codex mcp remove "$MCP_NAME" || die 8 "codex mcp remove failed"
    fi
    if [ -z "$WS" ]; then
      step mcp-codex skip "no agent workspace (pass --workspace <ROOT>)"
      return
    fi
    toml="$WS/.codex/config.toml"
    if [ -f "$toml" ] && grep -qE "^\[mcp_servers\.\"?$MCP_NAME\"?\]" "$toml"; then
      step mcp-codex done "$MCP_NAME in $toml"
    else
      step mcp-codex todo "register $MCP_NAME -> $BIN mcp in $toml"
      echo "action=append [mcp_servers.$MCP_NAME] to $toml"
      if [ "$APPLY" = 1 ]; then
        mkdir -p "$WS/.codex" && {
          [ ! -s "$toml" ] || echo
          echo "# >>> dogany-computer-use (setup.sh) >>>"
          echo "[mcp_servers.$MCP_NAME]"
          echo "command = \"$BIN\""
          echo "args = [\"mcp\"]"
          echo "# <<< dogany-computer-use <<<"
        } >> "$toml" || die 8 "write $toml"
      fi
    fi
  else
    step mcp-codex skip "codex not installed"
  fi
}

plan_policy() {
  local want_apps
  if [ -n "$APPS_ARG" ]; then
    want_apps="$(printf '%s\n' "$APPS_ARG" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | sed '/^$/d')"
    if printf '%s\n' "$want_apps" | grep -vqE '^[A-Za-z0-9][A-Za-z0-9.-]*$'; then
      die 2 "bad bundle id in --apps"
    fi
  else
    want_apps="$(apps_list)"
  fi
  if [ ! -x "$APP_BIN" ]; then
    [ "$APPLY" = 1 ] && die 8 "backend not installed; run install first"
    step policy todo "write bounded manifest (tools from installed driver), apps: $(echo $want_apps)"
    return
  fi
  allow_tools || die 8 "cua-driver list-tools reported no tools"
  [ -z "$DROPPED" ] || echo "note=reviewed tools not reported by driver, left out: $DROPPED"
  local want; want="$(printf '%s\n' "$want_apps" | render_manifest)"
  local have=""; [ -f "$MANIFEST" ] && have="$(cat "$MANIFEST")"
  local have_apps=""; [ -f "$APPS_CONF" ] && have_apps="$(cat "$APPS_CONF")"
  if [ "$want" = "$have" ] && [ "$want_apps" = "$have_apps" ]; then
    step policy done "bounded manifest, apps: $(echo $want_apps)"
    return
  fi
  step policy todo "write bounded manifest, apps: $(echo $want_apps)"
  if [ "$APPLY" = 1 ]; then
    mkdir -p "$CU_HOME" || die 8 "mkdir $CU_HOME"
    printf '%s\n' "$want_apps" > "$APPS_CONF.tmp" && mv "$APPS_CONF.tmp" "$APPS_CONF" \
      || die 8 "write apps.conf"
    printf '%s\n' "$want" > "$MANIFEST.tmp" && mv "$MANIFEST.tmp" "$MANIFEST" \
      || die 8 "write manifest"
    echo "action=wrote $MANIFEST"
    echo "note=running daemon keeps the old scope until: setup.sh start --apply"
  fi
}

need_installed() {
  check_app
  [ "$APP_STATE" = done ] || die 8 "backend not installed ($APP_WHY); run install first"
}

case "$VERB" in
  plan|install)
    plan_install
    if [ "$APPLY" = 1 ]; then
      echo "note=First screen-control use will ask for macOS Screen Recording and Accessibility permissions."
    fi ;;
  policy)
    [ -n "$APPS_ARG" ] || die 2 "policy needs --apps"
    plan_policy ;;
  grant)
    need_installed
    step grant todo "macOS asks owner for Accessibility + Screen Recording"
    act "$APP_BIN" permissions grant || die 8 "grant failed" ;;
  start)
    need_installed
    [ -f "$MANIFEST" ] || die 8 "no manifest; run install first"
    step daemon todo "restart bounded, manifest $MANIFEST"
    act "$APP_BIN" stop || true
    act open -n -g -a "$APP" --args serve --permission-mode bounded \
      --capability-manifest "$MANIFEST" --approve-capability-manifest \
      || die 8 "daemon start failed"
    echo "action=verify $APP_BIN status (wait ${START_WAIT}s)"
    if [ "$APPLY" = 1 ]; then
      n=0
      until "$APP_BIN" status >/dev/null 2>&1; do
        n=$((n + 1))
        [ "$n" -lt "$START_WAIT" ] \
          || die 8 "daemon not running after start (check manifest: $MANIFEST)"
        sleep 1
      done
      echo "verified=daemon running"
    fi ;;
esac

echo "todo=$TODO"
echo "result=ok"
exit 0
