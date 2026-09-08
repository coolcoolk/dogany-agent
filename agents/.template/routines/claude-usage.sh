#!/usr/bin/env bash
# claude-usage.sh -- Claude Code rate-limit (live) + stats-cache.json parser
# Default: live rate-limit only (short). With --full: also the cache report.
# NOTE: Live section gets credentials from ~/.claude/.credentials.json or
# macOS Keychain. The file is stale by design (the CLI only rotates the
# Keychain after a runtime refresh) -- an expired file next to a valid
# Keychain entry is normal and produces no message at all.
#
# Usage: claude-usage.sh [--full]   (default: live-only)
# Exit codes: 0 ok / 1 live lookup failed or no cache data (with --full)

set -euo pipefail
# NOTE: set -x is FORBIDDEN (would expose token in logs)

# --- arg parse: default live-only, --full appends the cache snapshot report,
# --json prints the raw /api/oauth/usage response body and exits (machine
# consumers, e.g. routines/usage-gate.py; DGN-546) ---
SHOW_FULL=0
JSON_OUT=0
for _arg in "$@"; do
  case "$_arg" in
    --full) SHOW_FULL=1 ;;
    --json) JSON_OUT=1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# workspace root = one level up from routines/
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
: "${WORKSPACE}"  # suppress unused var warning if -u strict

CACHE_FILE="${HOME}/.claude/stats-cache.json"

# ============================================================
# SECTION 1: LIVE RATE-LIMIT (from Anthropic API)
# ============================================================

_live_token=""
_live_err=""
_access_token=""

# _read_creds_file <path> -> prints accessToken to stdout; exits nonzero if
# the file is missing, unreadable, the token is absent, or the token is
# expired (expiresAt in the past). Never echoes the token value in errors.
_read_creds_file() {
  local _f="$1"
  [[ -f "$_f" && -r "$_f" ]] || return 1
  python3 -c "
import json, sys
from datetime import datetime, timezone
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        d = json.load(f)
    oauth = d.get('claudeAiOauth', {})
    t = oauth.get('accessToken', '')
    if not t:
        sys.exit(2)
    exp = oauth.get('expiresAt', '')
    if exp:
        try:
            # expiresAt is epoch-milliseconds (integer) or an ISO string
            if isinstance(exp, (int, float)):
                exp_dt = datetime.fromtimestamp(exp / 1000.0, tz=timezone.utc)
            else:
                exp_dt = datetime.fromisoformat(str(exp).replace('Z', '+00:00'))
            if exp_dt <= datetime.now(timezone.utc):
                sys.exit(3)  # expired
        except Exception:
            pass  # unparseable expiry: treat as non-expired (best effort)
    print(t, end='')
except Exception:
    sys.exit(1)
" "$_f"
}

# _read_keychain -> prints accessToken to stdout; exits nonzero if the
# Keychain entry is absent, empty, unparseable, or expired.
_read_keychain() {
  local _raw
  _raw=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null) || return 1
  [[ -n "$_raw" ]] || return 1
  python3 -c "
import json, sys
from datetime import datetime, timezone
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    oauth = d.get('claudeAiOauth', {})
    t = oauth.get('accessToken', '')
    if not t:
        sys.exit(2)
    exp = oauth.get('expiresAt', '')
    if exp:
        try:
            if isinstance(exp, (int, float)):
                exp_dt = datetime.fromtimestamp(exp / 1000.0, tz=timezone.utc)
            else:
                exp_dt = datetime.fromisoformat(str(exp).replace('Z', '+00:00'))
            if exp_dt <= datetime.now(timezone.utc):
                sys.exit(3)  # expired
        except Exception:
            pass
    print(t, end='')
except Exception:
    sys.exit(1)
" <<< "$_raw"
}

# _refresh_token_state -> prints "valid" / "expired" / "unknown" for the
# refresh token backing whichever store still has one. The Keychain is
# checked first -- it's the copy the CLI actually rotates (DGN-1050) -- and
# falls back to the file. Never echoes the raw JSON or any token value.
_refresh_token_state() {
  local _raw=""
  _raw=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null) || _raw=""
  if [[ -z "$_raw" && -f "$_creds_file" && -r "$_creds_file" ]]; then
    _raw=$(cat "$_creds_file" 2>/dev/null) || _raw=""
  fi
  [[ -n "$_raw" ]] || { echo "unknown"; return; }
  python3 -c "
import json, sys
from datetime import datetime, timezone
try:
    d = json.loads(sys.argv[1])
    exp = d.get('claudeAiOauth', {}).get('refreshTokenExpiresAt', '')
    if not exp:
        print('unknown'); sys.exit(0)
    exp_dt = (datetime.fromtimestamp(exp / 1000.0, tz=timezone.utc)
              if isinstance(exp, (int, float))
              else datetime.fromisoformat(str(exp).replace('Z', '+00:00')))
    print('expired' if exp_dt <= datetime.now(timezone.utc) else 'valid')
except Exception:
    print('unknown')
" "$_raw"
}

_file_state_desc() {
  case "$_file_rc" in
    3) echo "expired (normal -- the file is stale by design, the CLI only rotates the Keychain)" ;;
    2) echo "present, accessToken field empty" ;;
    0) echo "valid" ;;
    *) echo "missing/unreadable" ;;
  esac
}

# Expiry-aware token selection: try credentials file first; fall through to
# Keychain if the file token is absent or expired.
#
# IMPORTANT: the Claude CLI only ever rewrites the macOS Keychain after a
# runtime token rotation -- it never rewrites ~/.claude/.credentials.json
# (see the dogany-relogin-rebind skill's token-sync.sh DIVERGED verdict). An
# expired FILE next to a valid Keychain entry is therefore the normal steady
# state, not a fault, and is never reported below. Only a Keychain that
# itself cannot serve a token is diagnosed -- and even then, an expired
# Keychain access token is routinely self-healing as long as the refresh
# token behind it is still valid.
_creds_file="${HOME}/.claude/.credentials.json"
_file_rc=0
_live_calm=0
if _access_token=$(_read_creds_file "$_creds_file"); then
  : # file token is valid -- use it (rare; the file is normally stale)
else
  _file_rc=$?
  # Fall through to Keychain
  if _access_token=$(_read_keychain); then
    : # Keychain token is valid -- the expected steady state
  else
    _kc_rc=$?
    case "$_kc_rc" in
      3)
        # Keychain access token expired. Whether this self-heals depends
        # entirely on the refresh token, not the access token.
        case "$(_refresh_token_state)" in
          valid)
            _live_calm=1
            _live_err="credentials.json: $(_file_state_desc); Keychain access token expired but its refresh token is still valid -- waiting for the CLI's automatic refresh. No action needed."
            ;;
          expired)
            _live_err="RE-LOGIN REQUIRED: credentials.json: $(_file_state_desc); Keychain access token AND refresh token are both expired -- automatic renewal is no longer possible. Run: claude auth login"
            ;;
          *)
            _live_err="credentials.json: $(_file_state_desc); Keychain access token expired and refresh-token validity could not be determined. Run: claude auth login"
            ;;
        esac
        ;;
      1)
        _live_err="credentials.json: $(_file_state_desc); Keychain lookup returned no \"Claude Code-credentials\" entry (absent, or Keychain unavailable on this platform) -- not an expiry. Run: claude auth login"
        ;;
      *)
        _live_err="credentials.json: $(_file_state_desc); Keychain entry present but malformed (no accessToken field). Run: claude auth login"
        ;;
    esac
  fi
fi

if [[ -z "$_live_err" && -z "${_access_token:-}" ]]; then
  _live_err="accessToken is empty"
fi

if [[ -z "$_live_err" ]]; then
  # curl with timeout=10s; -sS = silent but show errors; token in header only
  _resp_body=""
  _resp_code=""
  if ! _curl_out=$(curl -sS --max-time 10 \
      -H "Authorization: Bearer ${_access_token}" \
      -H "anthropic-version: 2023-06-01" \
      -w "\n__HTTP_STATUS__:%{http_code}" \
      "https://api.anthropic.com/api/oauth/usage" 2>&1); then
    _live_err="curl failed: ${_curl_out}"
  else
    _resp_body=$(printf '%s' "$_curl_out" | sed '$d')
    _resp_code=$(printf '%s' "$_curl_out" | grep '__HTTP_STATUS__' | cut -d: -f2)
    if [[ "$_resp_code" != "200" ]]; then
      _live_err="API returned HTTP ${_resp_code}"
    fi
  fi
fi

# --json: emit the raw usage JSON for machine consumers and stop (DGN-546).
if [[ "$JSON_OUT" == "1" ]]; then
  _access_token=""  # clear from memory before any output
  if [[ -n "$_live_err" ]]; then
    echo "[claude-usage] live lookup unavailable (${_live_err})" >&2
    exit 1
  fi
  printf '%s\n' "$_resp_body"
  exit 0
fi

# Parse and print live section via python3
if [[ -z "$_live_err" ]]; then
  # Pass the response body to python via a tmpfile. Embedding it directly in an
  # unquoted heredoc is fragile -- the shell would expand $()/backticks, or the
  # python string could break on quotes in the body.
  _resp_file="$(mktemp /tmp/claude-usage-resp.XXXXXX)"
  printf '%s' "$_resp_body" > "$_resp_file"
  python3 - "$_resp_file" <<'PYEOF2'
import json, sys
from datetime import datetime, timezone, timedelta

raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
try:
    data = json.loads(raw)
except Exception as e:
    print(f"[Live Rate-Limit] JSON parse failed: {e}")
    sys.exit(0)

import os
_loc = os.environ.get("LOCALE", "en").strip().lower()
if _loc not in ("ko", "en"):
    _loc = "en"
_T = {
    "ko": {"title": "Claude 사용 한도", "h5": "5시간", "hw": "주간", "reset": "리셋"},
    "en": {"title": "Claude Usage Limits", "h5": "5h", "hw": "weekly", "reset": "reset"},
}[_loc]

def pctstr(v):
    try:
        return str(int(round(float(v))))
    except Exception:
        return "?"

def bar(pct, width=20):
    # short unicode block bar -- narrow enough that Telegram mobile <pre> does
    # not wrap the line (wrapping breaks bar/line alignment; DGN-205).
    try:
        p = max(0.0, min(100.0, float(pct)))
    except Exception:
        return "[" + ("?" * width) + "]"
    filled = int(round(p / 100.0 * width))
    return "[" + ("█" * filled) + ("░" * (width - filled)) + "]"

def _remain(dt):
    # Countdown from now to reset, compact "-{d}d {h}h {m}m" (drop zero heads).
    # Sign: leading "-" = time still counting down until reset (future).
    delta = dt - datetime.now(dt.tzinfo)
    secs = int(delta.total_seconds())
    sign = "-" if secs >= 0 else "+"  # + = reset already passed
    secs = abs(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append("%dd" % d)
    if h or d:
        parts.append("%dh" % h)
    parts.append("%dm" % m)
    return "%s%s" % (sign, " ".join(parts))

def _reset(iso_str):
    # OS-local tz, short MM-DD HH:MM (no year/zone -- width matters on mobile),
    # plus a live countdown to the reset in parens (e.g. "(-1h 33m)").
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
        return "%s (%s)" % (dt.strftime("%m-%d %H:%M"), _remain(dt))
    except Exception:
        return iso_str

def _row(label, pct, reset_iso):
    # label / bar+pct / reset on separate lines so the bar always starts at
    # the line head -> consistent width regardless of label length (form req).
    print(label)
    print("%s %s%%" % (bar(pct), pctstr(pct)))
    print("%s %s" % (_T["reset"], _reset(reset_iso)))

print(_T["title"])
print("─" * 27)

five_hour = data.get("five_hour", {})
seven_day = data.get("seven_day", {})
_row(_T["h5"], five_hour.get("utilization", 0), five_hour.get("resets_at", ""))
_row(_T["hw"], seven_day.get("utilization", 0), seven_day.get("resets_at", ""))

# per-model / scoped limits (e.g. weekly Fable) -- no severity tag (DGN-205).
for lim in data.get("limits", []):
    if lim.get("kind") in ("session", "weekly_all"):
        continue  # already shown as 5-hour / weekly above
    scope = lim.get("scope") or {}
    model = ""
    if isinstance(scope, dict) and isinstance(scope.get("model"), dict):
        model = scope["model"].get("display_name", "") or ""
    name = model or lim.get("kind", "?")
    _row(name, lim.get("percent", 0), lim.get("resets_at", ""))

print("─" * 27)
PYEOF2
  rm -f "$_resp_file"
  _access_token=""  # clear from memory after use
else
  if [[ "$_live_calm" == "1" ]]; then
    echo "[Live Rate-Limit] ${_live_err}"
  else
    echo "=================================================="
    echo "  [Live Rate-Limit] ${_live_err}"
    echo "=================================================="
  fi
  _access_token=""
  exit 1
fi

# ============================================================
# SECTION 2: CACHE REPORT (stats-cache.json) -- only with --full
# ============================================================

# Default is live-only (keeps /usage short in Telegram). The detailed cache
# snapshot report is opt-in via --full.
if [[ "$SHOW_FULL" != "1" ]]; then
  exit 0
fi

# --- guard: file must exist and be non-empty ---
if [[ ! -f "$CACHE_FILE" ]]; then
  echo "[Claude Usage] stats-cache.json not found: ${CACHE_FILE}"
  exit 1
fi
if [[ ! -s "$CACHE_FILE" ]]; then
  echo "[Claude Usage] stats-cache.json is empty."
  exit 1
fi

# --- parse with python3 (stdlib only) ---
python3 - "$CACHE_FILE" <<'PYEOF'
import json
import sys
from datetime import datetime, timezone

path = sys.argv[1]

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except (OSError, json.JSONDecodeError) as e:
    print(f"[Claude Usage] Failed to parse cache: {e}")
    sys.exit(1)

def _get(d, *keys, default=None):
    """Safe nested get."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur

# ---- header ----
last_date = data.get("lastComputedDate", "unknown")
print("=" * 50)
print("  Claude Code Usage Report")
print(f"  (cache snapshot -- not live rate-limit)")
print(f"  Last computed: {last_date}")
print("=" * 50)

# ---- totals ----
total_sessions = data.get("totalSessions", 0)
total_messages = data.get("totalMessages", 0)
first_date = data.get("firstSessionDate", "")
if first_date:
    try:
        first_date = first_date[:10]  # just the date part
    except Exception:
        pass

print("\n[TOTALS]")
print(f"  Sessions : {total_sessions:,}")
print(f"  Messages : {total_messages:,}")
if first_date:
    print(f"  Since    : {first_date}")

# ---- recent daily activity (last 7 entries) ----
daily = data.get("dailyActivity", [])
if daily:
    recent = daily[-7:]
    print("\n[RECENT DAILY ACTIVITY (last 7 days with usage)]")
    print(f"  {'Date':<12} {'Messages':>9} {'Sessions':>9} {'ToolCalls':>10}")
    print(f"  {'-'*12} {'-'*9} {'-'*9} {'-'*10}")
    for entry in reversed(recent):
        d  = entry.get("date", "?")
        mc = entry.get("messageCount", 0)
        sc = entry.get("sessionCount", 0)
        tc = entry.get("toolCallCount", 0)
        print(f"  {d:<12} {mc:>9,} {sc:>9,} {tc:>10,}")
else:
    print("\n[RECENT DAILY ACTIVITY] no data")

# ---- model usage ----
model_usage = data.get("modelUsage", {})
if model_usage:
    print("\n[MODEL TOKEN USAGE (cumulative)]")
    print(f"  {'Model':<32} {'Input':>12} {'Output':>10} {'CacheRead':>12} {'CacheWrite':>12}")
    print(f"  {'-'*32} {'-'*12} {'-'*10} {'-'*12} {'-'*12}")
    for model, stats in sorted(model_usage.items()):
        if not isinstance(stats, dict):
            continue
        inp   = stats.get("inputTokens", 0) or 0
        out   = stats.get("outputTokens", 0) or 0
        cr    = stats.get("cacheReadInputTokens", 0) or 0
        cw    = stats.get("cacheCreationInputTokens", 0) or 0
        # skip models with zero activity
        if inp == 0 and out == 0 and cr == 0 and cw == 0:
            continue
        # shorten model name if too long
        short = model if len(model) <= 32 else model[:29] + "..."
        print(f"  {short:<32} {inp:>12,} {out:>10,} {cr:>12,} {cw:>12,}")
else:
    print("\n[MODEL USAGE] no data")

# ---- longest session highlight ----
ls = data.get("longestSession", {})
if ls and isinstance(ls, dict):
    dur_ms = ls.get("duration", 0) or 0
    dur_min = dur_ms // 60000
    msgs = ls.get("messageCount", 0) or 0
    ts = ls.get("timestamp", "")
    if ts:
        try:
            ts = ts[:10]
        except Exception:
            pass
    print(f"\n[LONGEST SESSION]  {dur_min:,} min / {msgs:,} messages  ({ts})")

print("\n[NOTE] stats-cache.json is updated by Claude Code periodically.")
print("       Live 5h / weekly limits are NOT available from this source.")
print("=" * 50)
PYEOF
