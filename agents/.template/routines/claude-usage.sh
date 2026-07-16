#!/usr/bin/env bash
# claude-usage.sh -- Claude Code rate-limit (live) + stats-cache.json parser
# Default: live rate-limit only (short). With --full: also the cache report.
# NOTE: Live section gets credentials from ~/.claude/.credentials.json or macOS Keychain.
#
# Usage: claude-usage.sh [--full]   (default: live-only)
# Exit codes: 0 ok / 1 no data

set -euo pipefail
# NOTE: set -x is FORBIDDEN (would expose token in logs)

# --- arg parse: default live-only, --full appends the cache snapshot report ---
SHOW_FULL=0
for _arg in "$@"; do
  case "$_arg" in
    --full) SHOW_FULL=1 ;;
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

# Try to get token from ~/.claude/.credentials.json first
_creds_file="${HOME}/.claude/.credentials.json"
if [[ -f "$_creds_file" ]] && [[ -r "$_creds_file" ]]; then
  _access_token=$(python3 -c "
import json, sys
try:
    with open('$_creds_file', encoding='utf-8') as f:
        d = json.load(f)
    t = d.get('claudeAiOauth', {}).get('accessToken', '')
    if t:
        print(t, end='')
        sys.exit(0)
    sys.exit(2)
except Exception:
    sys.exit(1)
") || {
    _access_token=""
  }
fi

# If credentials file did not yield a token, fall back to macOS Keychain
if [[ -z "$_access_token" ]]; then
  if ! _live_token=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null); then
    _live_err="Both ~/.claude/.credentials.json and Keychain lookup failed (no token source available)"
  elif [[ -z "$_live_token" ]]; then
    _live_err="Token empty from Keychain"
  else
    # Extract accessToken from JSON via python3 (token value never written to file or echoed)
    _access_token=$(python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    t = d.get('claudeAiOauth', {}).get('accessToken', '')
    if not t:
        sys.exit(2)
    print(t, end='')
except Exception:
    sys.exit(1)
" <<< "$_live_token") || {
      _live_err="Failed to extract accessToken from Keychain JSON"
      _access_token=""
    }
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
  echo "=================================================="
  echo "  [Live Rate-Limit] live lookup failed (${_live_err})"
  echo "=================================================="
  _access_token=""
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
