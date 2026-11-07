#!/usr/bin/env bash
# General detached jobs: local model/script execution, cancellation and durable returns.
# Usage: --task FILE --label LABEL --read-only [--runtime claude-code|codex]
#        --script --label LABEL [--workdir PATH] -- COMMAND ARG...
# Repository/cloud policy is supplied only by an explicit DOGANY_JOB_DRIVER executable.
set -euo pipefail

if [[ -n "${DOGANY_JOB_DRIVER:-}" && -z "${DISPATCH_TEST:-}" ]]; then
  [[ -x "$DOGANY_JOB_DRIVER" ]] || { echo "[dispatch] configured job driver is not executable" >&2; exit 2; }
  exec "$DOGANY_JOB_DRIVER" "$@"
fi

# Optional installed provider. The pack resolver owns receipt/seal admission.
# No binding retains the core dispatcher; explicit DOGANY_JOB_DRIVER wins above.
if [[ -z "${DISPATCH_TEST:-}" ]]; then
  _driver_root="${DOGANY_INSTANCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  _driver_pack="$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from conf_reader import conf_get; print(conf_get("DISPATCH_DRIVER_PACK", sys.argv[2]))' \
    "$_driver_root/routines/lib" "$_driver_root/config/agent.conf")" || exit 2
  if [[ -n "$_driver_pack" ]]; then
    exec bash "$_driver_root/routines/driver.sh" --root "$_driver_root" \
      --pack "$_driver_pack" --entry dispatch -- "$@"
  fi
fi

WHY_DISPATCH_ORIGINAL_ARGV=("$@")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTANCE_ROOT="${DOGANY_INSTANCE_ROOT:-$WORKSPACE}"
DISPATCH_DIR="$WORKSPACE/.telegram_bot/dispatch"
ACTIVE_COUNT_FILE="$DISPATCH_DIR/active-count"

ACTIVE_COUNT_FILE_CODEX="$DISPATCH_DIR/active-count.codex"

MACHINE_PRESSURE_ARG=""
MACHINE_PRESSURE_READ=0

ATTACH_FILES=()
ATTACH_NOTE=""
ATTACH_TMP=""

ATTACH_BRIEF_FILE=""
COUNT_LOCK_DIR="$DISPATCH_DIR/.count.lock"
LEDGER_UPDATE="$SCRIPT_DIR/lib/ledger-update.sh"
USAGE_GATE="$SCRIPT_DIR/usage-gate.sh"
PUSH_SH="$SCRIPT_DIR/push.sh"
FABLE_FLAG="$WORKSPACE/.claude/.fable-exhausted"
LEDGER_FILE="$WORKSPACE/product/auto-loop-ledger.md"

SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"

RETURN_ROUTE_JSON=""
RETURN_RESERVATION=""
RETURN_RESERVATION_DURABLE=0
RETURN_ROUTE_KIND="inbox"
RETURN_JOB_KIND="work"
RETURN_PARENT_EVENT_ID=""
RETURN_RUNTIME_POLICY="default"
REQUESTED_NOTIFY=""

return_route_release_unadmitted() {
  if [[ -n "$RETURN_RESERVATION" && "$RETURN_RESERVATION_DURABLE" == 0 ]]; then
    python3 -B "$SCRIPT_DIR/lib/return_route.py" --release-reservation "$WORKSPACE" "$RETURN_RESERVATION"
  fi
}

resolve_return_route() {

  if [[ -z "$RETURN_RESERVATION" ]]; then
    RETURN_RESERVATION="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
  fi
  trap 'return_route_release_unadmitted' EXIT
  local resolved fields
  local product_args=()
  if [ -n "${DOGANY_RETURN_PRODUCT:-}" ]; then
    product_args=(--product "$DOGANY_RETURN_PRODUCT")
  fi
  resolved="$(python3 -B "$SCRIPT_DIR/lib/return_route.py" --return-route-json \
    --workspace "$WORKSPACE" ${product_args[@]+"${product_args[@]}"} \
    --urgent "${DOGANY_RETURN_URGENT:-}" --leader "${DOGANY_RETURN_LEADER_WS:-}" \
    --job-kind "$RETURN_JOB_KIND" --parent-event-id "$RETURN_PARENT_EVENT_ID" \
    --legacy-inbox "$SESSION_INBOX_DIR" \
    --snapshot "$RETURN_ROUTE_JSON" --reservation "$RETURN_RESERVATION")" || {
    python3 -B - "$SCRIPT_DIR/lib" "$WORKSPACE" ${ORIG_ARGV[@]+"${ORIG_ARGV[@]}"} <<'PYRETAIN'
import sys
sys.path.insert(0, sys.argv[1])
from return_route import retain_request
print("return-route: request retained at " + retain_request(sys.argv[2], sys.argv[3:]), file=sys.stderr)
PYRETAIN
    return 2
  }
  RETURN_ROUTE_JSON="$resolved"
  fields="$(python3 -c 'import json,sys; r=json.loads(sys.argv[1]); print("\t".join([r["kind"], r["destination"], r["job_kind"], r["parent_event_id"] or "-", r["runtime_policy"], r["urgency"] or "-"]))' "$resolved")" || return 2
  local kind dest parent urgent
  IFS=$'\t' read -r kind dest RETURN_JOB_KIND parent RETURN_RUNTIME_POLICY urgent <<< "$fields"
  RETURN_ROUTE_KIND="$kind"
  RETURN_PARENT_EVENT_ID="${parent#-}"
  if [[ "$kind" == inbox ]]; then SESSION_INBOX_DIR="$dest"; fi
  DOGANY_RETURN_URGENT="${urgent#-}"
  REQUESTED_NOTIFY="${REQUESTED_NOTIFY:-$NOTIFY}"
  if [[ "$kind" != inbox ]]; then NOTIFY="silent"; fi
  if [[ "$RETURN_RUNTIME_POLICY" == codex-only ]]; then
    [[ "$SCRIPT_JOB" != 1 && -z "$LIVE_TARGETS" ]] || {
      echo "[dispatch] seat-review requires a read-only model job" >&2; return 2;
    }
    [[ "$REQUESTED_RUNTIME" == auto || "$REQUESTED_RUNTIME" == codex ]] || {
      echo "[dispatch] seat-review requires codex; no alternate runtime" >&2; return 2;
    }
    RUNTIME=codex REQUESTED_RUNTIME=codex READ_ONLY=1
    TIMEOUT_MIN=5
  fi
}

restore_legacy_return_route() {

  if [[ -z "${RETURN_ROUTE_JSON:-}" && -n "${DOGANY_RETURN_PRODUCT:-}" ]]; then
    local holder
    holder="$(python3 "$SCRIPT_DIR/lib/return_route.py" "$DOGANY_RETURN_PRODUCT" \
      "${DOGANY_RETURN_URGENT:--}" "$WORKSPACE" "${DOGANY_RETURN_LEADER_WS:-}")" || holder=""
    [[ -z "$holder" ]] || SESSION_INBOX_DIR="$holder/.telegram_bot/session-inbox"
  fi
}

return_route_env_write() {
  RETURN_RESERVATION_DURABLE=1
  run_env_write RETURN_RESERVATION "$RETURN_RESERVATION"
  run_env_write RETURN_RESERVATION_DURABLE 1
  if [[ -n "$RETURN_ROUTE_JSON" ]]; then
    python3 -B - "$SCRIPT_DIR/lib" "$RUNDIR" "$RETURN_ROUTE_JSON" <<'PYPIN'
import json, sys
sys.path.insert(0, sys.argv[1])
from return_route import immutable_json, validate_route
immutable_json(sys.argv[2] + "/return.route.json", validate_route(json.loads(sys.argv[3])))
PYPIN
  fi
  run_env_write RETURN_ROUTE_JSON "$RETURN_ROUTE_JSON"
  run_env_write RETURN_ROUTE_KIND "$RETURN_ROUTE_KIND"
  run_env_write SESSION_INBOX_DIR "$SESSION_INBOX_DIR"
  run_env_write RETURN_JOB_KIND "$RETURN_JOB_KIND"
  run_env_write RETURN_PARENT_EVENT_ID "$RETURN_PARENT_EVENT_ID"
  run_env_write RETURN_RUNTIME_POLICY "$RETURN_RUNTIME_POLICY"
  run_env_write REQUESTED_NOTIFY "$REQUESTED_NOTIFY"
  run_env_write DOGANY_RETURN_URGENT "${DOGANY_RETURN_URGENT:-}"
}

return_route_request_json() {
  RETURN_RESERVATION_DURABLE=1
  RETURN_RESERVATION_EXPORT="$RETURN_RESERVATION" python3 - "$RETURN_ROUTE_JSON" "$@" <<'PYROUTE'
import json, os, sys
route = json.loads(sys.argv[1]) if sys.argv[1] else None
argv = sys.argv[3:]
if route:
    at = argv.index("--") if "--" in argv else len(argv)
    argv[at:at] = ["--return-route-json", sys.argv[1], "--return-reservation", os.environ["RETURN_RESERVATION_EXPORT"]]
with open(sys.argv[2], "w") as stream:
    json.dump(dict(argv=argv, return_route=route, return_reservation=os.environ["RETURN_RESERVATION_EXPORT"],
                   job_kind=route["job_kind"] if route else "work",
                   parent_event_id=route["parent_event_id"] if route else None,
                   runtime_policy=route["runtime_policy"] if route else "default"), stream)
PYROUTE
}
DISPATCH_ISOLATION_ROOT="${DOGANY_DISPATCH_ISOLATION_ROOT:-/tmp/dogany-dispatch}"
MODEL_DISPLAY_BINDINGS="$SCRIPT_DIR/lib/dispatch-model-display.tsv"


RUNTIME="claude-code"
REQUESTED_RUNTIME="${DOGANY_DISPATCH_RUNTIME:-auto}"
[[ "$REQUESTED_RUNTIME" == auto ]] || RUNTIME="$REQUESTED_RUNTIME"
RUNTIME_SELECTION_SOURCE="auto"
[[ "$REQUESTED_RUNTIME" == auto ]] || RUNTIME_SELECTION_SOURCE="env"
RUNTIME_SELECTION_REASON=""
RUNTIME_SELECTION_DETAIL=""
RUNTIME_SELECTED=""

RUNTIME_FALLBACK_DIRECTION=""
RUNTIME_FALLBACK_NOTE=""

FALLBACK_BLOCK_REASON=""

CLAUDE_GATE_RC=""
CLAUDE_GATE_NOTE=""

DECISION_DIR="$DISPATCH_DIR/decisions"
DECISION_ID=""

DISPATCH_DEFER_MAX="${DOGANY_DISPATCH_DEFER_MAX:-2}"
DISPATCH_DEFER_DEADLINE_MIN="${DOGANY_DISPATCH_DEFER_DEADLINE_MIN:-360}"
DISPATCH_DEFER_RETRY_MIN="${DOGANY_DISPATCH_DEFER_RETRY_MIN:-30}"

DISPATCH_LOOKUP_RETRY_SEC="${DOGANY_DISPATCH_LOOKUP_RETRY_SEC:-60}"
DISPATCH_DEFER_ATTEMPT="${DOGANY_DISPATCH_DEFER_ATTEMPT:-0}"
DISPATCH_DEFER_DEADLINE="${DOGANY_DISPATCH_DEFER_DEADLINE:-}"
[[ "$DISPATCH_DEFER_ATTEMPT" =~ ^[0-9]+$ ]] || DISPATCH_DEFER_ATTEMPT=0
[[ "$DISPATCH_DEFER_DEADLINE" =~ ^[0-9]+$ ]] || DISPATCH_DEFER_DEADLINE=""

USAGE_DEFER_RETRY="$SCRIPT_DIR/usage-defer-retry.sh"

CODEX_RATELIMIT_SCRIPT="$SCRIPT_DIR/codex-ratelimit.py"
CODEX_QUOTA_TIMEOUT="${DOGANY_CODEX_QUOTA_TIMEOUT:-20}"

USAGE_GATE_PY="$SCRIPT_DIR/usage-gate.py"

CODEX_QUOTA_MAX_PERCENT=98

CODEX_QUOTA_NOTE=""

CODEX_USED_PERCENT=""

CODEX_RESETS_AT=""
CODEX_WINDOW_MINS=""

RUNTIME_HEADROOM_PY="$SCRIPT_DIR/lib/runtime_headroom.py"

RUNTIME_POLICY_PY="$SCRIPT_DIR/lib/runtime_policy.py"
RUNTIME_TRAITS_TSV="$SCRIPT_DIR/lib/runtime-traits.tsv"

API_CREDIT_POOL_PY="$SCRIPT_DIR/lib/api_credit_pool.py"

BOOK_IDENTITY_PY="$SCRIPT_DIR/lib/book_identity.py"
API_CREDIT_LEDGER="$DISPATCH_DIR/api-credit-ledger.jsonl"
API_CREDIT_KEYCHAIN_SERVICE="dogany-anthropic-api-credit"
API_CREDIT_KEYCHAIN_ACCOUNT="dogany"

API_CREDIT_EXHAUSTED_MARKER="$DISPATCH_DIR/api-credit-exhausted.json"
API_CREDIT_RECONCILE_STATE="$DISPATCH_DIR/api-credit-reconcile.json"
API_CREDIT_INFLIGHT_DIR="$DISPATCH_DIR/api-inflight"
API_CREDIT_PROBE_STATE="$DISPATCH_DIR/api-credit-probe.json"

API_CREDIT_LEGACY_LEDGER="$API_CREDIT_LEDGER"
API_CREDIT_LEGACY_MARKER="$API_CREDIT_EXHAUSTED_MARKER"
API_CREDIT_LEGACY_RECONCILE="$API_CREDIT_RECONCILE_STATE"
API_CREDIT_LEGACY_INFLIGHT="$API_CREDIT_INFLIGHT_DIR"
API_CREDIT_ACCOUNT_DIR=""
API_POOL_ACCOUNT_READ=""
API_ADMIN_KEY_PRESENT=""
API_ADMIN_KEYCHAIN_SERVICE="dogany-anthropic-admin"
API_ADMIN_KEYCHAIN_ACCOUNT="dogany"
API_ADMIN_BASE_URL="${API_ADMIN_BASE_URL:-https://api.anthropic.com}"

RESET_PASSES_PY="$SCRIPT_DIR/lib/reset_passes.py"
RESET_PASS_PROMPT_STAMP="$DISPATCH_DIR/reset-pass-prompts"
RESET_PASS_BOOST_CODEX=8        
RESET_PASS_BOOST_CLAUDE=8       
RESET_PASS_SPECS_READ=""
RESET_PASS_SPEC_CLAUDE=""
RESET_PASS_SPEC_CODEX=""

codex_reset_detail() {
  python3 - "$1" "${2:-}" <<'PY' 2>/dev/null
import sys
import time

try:
    reset_at = int(sys.argv[1])
    now = int(sys.argv[2]) if sys.argv[2] else int(time.time())
    if reset_at <= 0:
        raise ValueError("invalid reset epoch")
    remaining = max(0, reset_at - now)
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes = remaining // 60
    duration = []
    if days:
        duration.append(f"{days}d")
    if hours or days:
        duration.append(f"{hours}h")
    duration.append(f"{minutes}m")
    print("weekly reset: %s (in %s)" % (
        time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(reset_at)),
        " ".join(duration),
    ))
except Exception:
    pass
PY
}

CODEX_HOOK_VERIFY_SCRIPT="$SCRIPT_DIR/codex-hook-verify.py"
CODEX_HOOK_TIMEOUT="${DOGANY_CODEX_HOOK_TIMEOUT:-20}"

CODEX_ADAPTER_ROOT=""

CODEX_UNGUARDED_WHY=""

CODEX_HOOK_NOTE=""

model_display_binding() {
  local runtime="$1" model="$2" column="$3"
  [[ -r "$MODEL_DISPLAY_BINDINGS" ]] || return 1
  awk -F'\t' -v rt="$runtime" -v model="$model" -v col="$column" '
    /^[[:space:]]*#/ || NF < col { next }
    $1 == rt && $2 == model { print $col; found=1; exit }
    END { exit(found ? 0 : 1) }
  ' "$MODEL_DISPLAY_BINDINGS"
}

codex_slug_for_model() {
  if [[ "$1" == gpt-* ]]; then printf '%s\n' "$1"; else model_display_binding codex "$1" 3; fi
}

owner_model_display() {
  local out=""
  if [[ -r "$SCRIPT_DIR/lib/model_display.py" ]]; then
    out="$(python3 "$SCRIPT_DIR/lib/model_display.py" --tsv "$MODEL_DISPLAY_BINDINGS" \
             "$RUNTIME" "$MODEL" "${1:-}" 2>/dev/null)" || out=""
  fi
  [[ -n "$out" ]] || out="${MODEL:-}"
  printf '%s' "$out"
}

owner_model_display_cap() {
  if [[ -n "$MODEL_SLOT" ]]; then
    return 0
  fi
  owner_model_display "${MODEL_OBSERVED_JSON:-}"
}

owner_model_display_bare() {
  local out=""
  if [[ -r "$SCRIPT_DIR/lib/model_display.py" ]]; then
    out="$(python3 "$SCRIPT_DIR/lib/model_display.py" --bare --tsv "$MODEL_DISPLAY_BINDINGS" \
             "$RUNTIME" "$MODEL" "${1:-}" 2>/dev/null)" || out=""
  fi
  [[ -n "$out" ]] || out="${MODEL:-}"
  printf '%s' "$out"
}

run_block() {
  local out=""
  if [[ -r "$SCRIPT_DIR/lib/run_block.py" ]]; then
    out="$(python3 "$SCRIPT_DIR/lib/run_block.py" --quote --kind "$1" --model "$2" \
             --runtime "$3" --billing "${RUN_BILLING:-}" --host "${RUN_HOST:-}" --title "$4" --clock "$5" --elapsed "${6:-}" 2>/dev/null)" || out=""
  fi
  if [[ -z "$out" ]]; then
    out="$(python3 - "$1" "$2" "$4" "$5" "${6:-}" <<'PY'
import html
import sys
kind, model, title, clock, elapsed = sys.argv[1:]
actor = kind + (": " + model if model else "")
line = "• " + actor + (", " + clock if clock else "")
if clock and elapsed:
    line += " (" + elapsed + ")"
print("<blockquote>" + html.escape(title, quote=False) + "\n" +
      html.escape(line, quote=False) + "</blockquote>", end="")
PY
)"
  fi
  printf '%s' "$out"
}

background_term() {
  local label="${DOGANY_LIVE_LABEL:-}" lib="$SCRIPT_DIR/lib/agentlib.sh"
  if [[ -z "$label" && -f "$lib" ]]; then
    label="$( ( set +e; AGENT_ROOT="$WORKSPACE"; . "$lib" >/dev/null 2>&1
                printf '%s' "${DASHBOARD_LIVE_LABEL:-}" ) 2>/dev/null )" || label=""
  fi
  label="${label% 작업 중}"
  [[ -n "$label" ]] || label="$(notice_i18n dispatch.notice.background)"
  printf '%s' "${label:-백그라운드}"
}

CODEX_BOOK_CHECKED=""
codex_book_check() {
  [[ -z "$CODEX_BOOK_CHECKED" && -r "$BOOK_IDENTITY_PY" ]] || return 0
  CODEX_BOOK_CHECKED=1
  python3 -B "$BOOK_IDENTITY_PY" check-codex --window-mins "${1:-}" >/dev/null 2>&1 || true
}

codex_quota_gate() {
  local py_out="" rc=0 used="" resets_at="" window_mins="" reset_detail="" kind="" raw="" elapsed=""
  py_out="$(python3 "$CODEX_RATELIMIT_SCRIPT" --json --timeout "$CODEX_QUOTA_TIMEOUT" 2>/dev/null)" || rc=$?
  if [[ $rc -eq 0 ]]; then
    IFS=$'\t' read -r used resets_at window_mins <<< "$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    used = d["used_percent"]
    if d.get("ok") is not True or type(used) is not int or not 0 <= used <= 100:
        raise ValueError("invalid quota")
    print("%s\t%s\t%s" % (used, d.get("resets_at") or "", d.get("window_duration_mins") or ""))
except Exception:
    print("")' 2>/dev/null)"
    [[ "$used" =~ ^[0-9]+$ ]] || rc=9   
  fi
  if [[ $rc -eq 0 ]]; then
    CODEX_USED_PERCENT="$used"   
    CODEX_RESETS_AT="$resets_at"; CODEX_WINDOW_MINS="$window_mins"   
    codex_book_check "$window_mins"
    if [[ "$used" -gt "$CODEX_QUOTA_MAX_PERCENT" ]]; then
      reset_detail="$(codex_reset_detail "$resets_at")"
      CODEX_QUOTA_NOTE="codex weekly quota ${used}% over owner ceiling ${CODEX_QUOTA_MAX_PERCENT}%${reset_detail:+; $reset_detail}"
      {
        echo "[dispatch] REFUSED: codex weekly quota at ${used}% (owner ceiling: ${CODEX_QUOTA_MAX_PERCENT}%)."
        echo "[dispatch]   this is a live resource ceiling the owner set by hand, not a tier-binding judgment -- no --why bypass exists for it (DGN-1444)."
        echo "[dispatch]   owner must explicitly approve dispatching over the ceiling, or wait for the weekly window to reset.${reset_detail:+ $reset_detail}"
      } >&2
      return 2
    fi
    CODEX_QUOTA_NOTE="codex weekly quota ${used}% (<=${CODEX_QUOTA_MAX_PERCENT}, quota gate passed)"
    echo "[dispatch]   codex quota gate: $CODEX_QUOTA_NOTE"
    return 0
  fi

  kind="$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("kind","unknown"))
except Exception:
    print("unknown")' 2>/dev/null)"
  [[ -n "$kind" ]] || kind="unknown"
  raw="$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("raw",""))
except Exception:
    print("")' 2>/dev/null)"
  elapsed="$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("elapsed_ms",0))
except Exception:
    print(0)' 2>/dev/null)"
  [[ "$elapsed" =~ ^[0-9]+$ ]] || elapsed=0

  CODEX_QUOTA_NOTE="codex quota read FAILED (kind=$kind)"
  echo "[dispatch] $CODEX_QUOTA_NOTE" >&2
  echo "[dispatch]   raw: $raw" >&2

  if ! python3 "$CODEX_RATELIMIT_SCRIPT" --record-failure \
        --kind "$kind" --raw "$raw" --elapsed-ms "$elapsed" \
        --intent "label=$LABEL model=$MODEL" \
        --fallback "claude-code" >/dev/null 2>&1; then
    echo "[dispatch]   warning: could not append codex-ratelimit failure record (non-fatal)" >&2
  fi
  return 3
}

write_runtime_decision_file() {
  python3 - <<'PY'
import json, os, time

def _epoch(name):
    v = os.environ.get(name, "")
    if not v.isdigit():
        return None
    e = int(v)
    return {"epoch": e, "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(e))}

path = os.environ["DR_PATH"]
doc = {
    "decision_id": os.environ.get("DR_ID", ""),
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "status": os.environ.get("DR_STATUS", ""),
    "reason_code": os.environ.get("DR_REASON", ""),
    "detail": os.environ.get("DR_DETAIL", ""),
    "label": os.environ.get("DR_LABEL", ""),
    "ticket": os.environ.get("DR_TICKET", ""),
    "model": os.environ.get("DR_MODEL", ""),
    "model_source": os.environ.get("DR_MODEL_SOURCE", ""),
    "requested_model": os.environ.get("DR_REQUESTED_MODEL", ""),
    "codex_model": os.environ.get("DR_CODEX_MODEL", ""),
    "mode": os.environ.get("DR_MODE", ""),
    "workdir": os.environ.get("DR_WORKDIR", ""),
    "requested_runtime": os.environ.get("DR_REQ_RT", ""),
    "selected_runtime": os.environ.get("DR_SEL_RT", ""),
    "selection_source": os.environ.get("DR_SELECTION_SOURCE", ""),
    "selection_reason": os.environ.get("DR_SELECTION_REASON", ""),
    "selection_detail": os.environ.get("DR_SELECTION_DETAIL", ""),
    # selected != executed: this record proves a SELECTION at decision time;
    # execution truth stays with run.env/status.json, linked via runid below
    # (empty when the refusal happened before any run dir was minted).
    "fallback_direction": os.environ.get("DR_DIRECTION", ""),
    "fallback_note": os.environ.get("DR_FB_NOTE", ""),
    "claude_gate_rc": os.environ.get("DR_CLAUDE_RC", ""),
    "claude_gate_note": os.environ.get("DR_CLAUDE_NOTE", ""),
    "codex_quota_note": os.environ.get("DR_QUOTA_NOTE", ""),
    "codex_hook_note": os.environ.get("DR_HOOK_NOTE", ""),
    "runid": os.environ.get("DR_RUNID", ""),
    "defer_attempt": int(os.environ.get("DR_ATTEMPT", "0") or 0),
    "next_recheck": _epoch("DR_NEXT"),
    "deadline": _epoch("DR_DEADLINE"),
    "origin_decision": os.environ.get("DR_ORIGIN", ""),
    # DGN-2144: the stated reason for an explicit pin and this agent's pin
    # count for the day (0 = no pin on this request).
}
# Owner 2026-11-02: name the pool actually chosen and why each other pool
# was skipped (last reason per pool wins; the chosen pool is never listed).
doc["chosen_pool"] = doc["selected_runtime"]
_skips = {}
for _item in os.environ.get("DR_POOL_SKIPS", "").split("|"):
    _pool, _sep, _why = _item.partition("=")
    if _sep and _pool and _pool != doc["chosen_pool"]:
        _skips[_pool] = _why
doc["skipped_pools"] = _skips
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
}

record_runtime_decision() {
  local status="$1" reason="$2" detail="${3:-}" next_epoch="${4:-}" deadline_epoch="${5:-}"
  mkdir -p "$DECISION_DIR" 2>/dev/null || true
  if [[ -z "$DECISION_ID" ]]; then
    DECISION_ID="rtd-$(date +%Y%m%d-%H%M%S)-$$-$(printf '%04d' $(( RANDOM % 10000 )))"
  fi
  local mode="observe"
  if ! DR_PATH="$DECISION_DIR/$DECISION_ID.json" \
    DR_ID="$DECISION_ID" DR_STATUS="$status" DR_REASON="$reason" DR_DETAIL="$detail" \
    DR_LABEL="${LABEL:-}" DR_TICKET="${TICKET:-}" DR_MODEL="${MODEL:-}" DR_MODEL_SOURCE="${MODEL_SOURCE:-}" \
    DR_REQUESTED_MODEL="${REQUESTED_MODEL:-}" DR_CODEX_MODEL="${CODEX_MODEL:-}" \
    DR_MODE="$mode" \
    DR_WORKDIR="${JOB_WORKDIR:-}" \
    DR_REQ_RT="$REQUESTED_RUNTIME" DR_SEL_RT="$RUNTIME" \
    DR_SELECTION_SOURCE="$RUNTIME_SELECTION_SOURCE" DR_SELECTION_REASON="$RUNTIME_SELECTION_REASON" \
    DR_SELECTION_DETAIL="$RUNTIME_SELECTION_DETAIL" \
    DR_DIRECTION="$RUNTIME_FALLBACK_DIRECTION" DR_FB_NOTE="$RUNTIME_FALLBACK_NOTE" \
    DR_CLAUDE_RC="${CLAUDE_GATE_RC:-}" DR_CLAUDE_NOTE="${CLAUDE_GATE_NOTE:-}" \
    DR_QUOTA_NOTE="${CODEX_QUOTA_NOTE:-}" DR_HOOK_NOTE="${CODEX_HOOK_NOTE:-}" \
    DR_RUNID="${RUNID:-}" DR_ATTEMPT="$DISPATCH_DEFER_ATTEMPT" \
    DR_NEXT="$next_epoch" DR_DEADLINE="$deadline_epoch" \
    DR_ORIGIN="${DOGANY_DISPATCH_DEFER_ORIGIN:-}" \
    DR_POOL_SKIPS="${POOL_SKIPS:-}" \
    write_runtime_decision_file
  then
    echo "[dispatch]   warning: runtime decision record write FAILED ($DECISION_DIR/$DECISION_ID.json)" >&2
    return 1
  fi
  return 0
}

runtime_selection_consistency() {
  [[ -n "$RUNTIME_SELECTED" && "$RUNTIME_SELECTED" != "$RUNTIME" ]] || return 0
  if [[ "${1:-admission}" == worker || -z "$RUNTIME_FALLBACK_NOTE" ]]; then
    echo "[dispatch] REFUSED: selected $RUNTIME_SELECTED but would execute $RUNTIME without a fallback note" >&2
    return 2
  fi
  RUNTIME_SELECTION_REASON=runtime_fallback
  RUNTIME_SELECTION_DETAIL+="; selected_runtime=$RUNTIME; fallback=$RUNTIME_FALLBACK_NOTE"
  record_runtime_decision selected "$RUNTIME_SELECTION_REASON" "$RUNTIME_SELECTION_DETAIL" || return 2
  RUNTIME_SELECTED="$RUNTIME"
}

claude_usage_snapshot() {
  [[ -r "$USAGE_GATE_PY" ]] || return 1
  python3 -B - "$USAGE_GATE_PY" <<'PY' 2>/dev/null
import importlib.util, json, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    usage = mod.get_usage()
    if usage is None:
        raise RuntimeError("get_usage() returned None (read failed)")
    hist = mod.usage_history() if hasattr(mod, "usage_history") else []
    print(json.dumps({"usage": usage, "history": hist}))
except Exception:
    sys.exit(1)
PY
}

runtime_headroom_verdict() {
  local snap="" out=""
  reset_pass_specs
  snap="$(claude_usage_snapshot)" || snap=""
  out="$(printf '%s' "$snap" | python3 -B "$RUNTIME_HEADROOM_PY" \
           --codex-used "$CODEX_USED_PERCENT" --codex-resets-at "$CODEX_RESETS_AT" \
           --codex-window-mins "${CODEX_WINDOW_MINS:-10080}" \
           --claude-pass "$(reset_pass_spec claude)" \
           --codex-pass "$(reset_pass_spec codex)" \
           --claude-live-burn "$DISPATCH_LIVE_BURN_7D" 2>/dev/null)" || out=""
  [[ "$out" == codex$'\t'* || "$out" == claude-code$'\t'* ]] \
    || out=$'codex\theadroom_policy_unavailable\theadroom policy produced no verdict; codex quota gate passed'
  printf '%s\n' "$out"
}

reset_pass_specs() {
  local out c x
  [[ -n "$RESET_PASS_SPECS_READ" ]] && return 0
  RESET_PASS_SPECS_READ=1

  [[ "${DISPATCH_TEST:-}" == 1 && "${DISPATCH_TEST_PASS_STORE:-}" != 1 ]] && return 0
  out="$(python3 -B "$RESET_PASSES_PY" specs --conf "$WORKSPACE/config/agent.conf" 2>/dev/null)" || out=""
  IFS=$'\t' read -r c x <<< "$out" || true
  [[ "$c" == - ]] && c=""
  [[ "$x" == - ]] && x=""
  RESET_PASS_SPEC_CLAUDE="$(printf '%s' "$c" | tr -d '[:space:]')"
  RESET_PASS_SPEC_CODEX="$(printf '%s' "$x" | tr -d '[:space:]')"
  return 0
}

reset_pass_spec() {
  local v=""
  case "$1" in
    claude) v="${RESET_PASSES_CLAUDE:-}" ;;
    codex)  v="${RESET_PASSES_CODEX:-}" ;;
  esac
  if [[ -n "$v" ]]; then
    printf '%s' "$v" | tr -d '[:space:]'
    return 0
  fi
  reset_pass_specs
  case "$1" in
    claude) printf '%s' "$RESET_PASS_SPEC_CLAUDE" ;;
    codex)  printf '%s' "$RESET_PASS_SPEC_CODEX" ;;
  esac
}

API_PLAN_READ="" API_PLAN_READ_DONE=""

reset_pass_mode() {
  local rt="$1" used="$2" reset="${3:-}" spec="" out=""
  reset_pass_specs
  case "$rt" in
    claude) spec="$(reset_pass_spec claude)" ;;
    codex)  spec="$(reset_pass_spec codex)" ;;
  esac
  if [[ -z "$spec" ]]; then
    printf 'none\t-\t-\t%s pass none\n' "$rt"
    return 0
  fi
  out="$(python3 -B "$RESET_PASSES_PY" mode --runtime "$rt" --spec "$spec" \
           --used "$used" --resets-at "$reset" --window "${4:-7d}" 2>/dev/null)" || out=""
  [[ "$out" == *$'\t'* ]] || out="none"$'\t-\t-\t'"$rt pass policy unavailable; treated as no pass"
  printf '%s\n' "$out"
}

reset_pass_slack() {
  local rt="$1" used="$2" reset="${3:-}" spec=""
  reset_pass_specs
  spec="$(reset_pass_spec "$rt")"
  python3 -B "$RESET_PASSES_PY" slack --runtime "$rt" --spec "$spec" \
    --used "$used" --resets-at "$reset" --window "${4:-7d}"
}

needs_connectors_infer() {
  if [[ "$NEEDS_CONNECTORS" != 1 && -r "${TASK_FILE:-}" ]] \
     && grep -q 'mcp__claude_ai_' "$TASK_FILE" 2>/dev/null; then
    NEEDS_CONNECTORS=1
  fi
  return 0
}

auto_codex_model() {
  printf '%s\t%s\n' "${REQUESTED_MODEL:-${DOGANY_DISPATCH_CODEX_MODEL:-opus}}" "local-default"
}

test_slots_held() {
  local q="${DOGANY_SHARED_HOME:-${HOME:-}/.dogany}/cron-queue" s pid n=0 w ncpu
  [[ -d "$q" ]] || return 1
  for s in "$q"/worker-test/slot-* "$q"/heavy-test/slot-1; do
    [[ -d "$s" ]] || continue
    pid="$(cat "$s/pid" 2>/dev/null)" || pid=""
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    if kill -0 "$pid" 2>/dev/null || [[ "$(kill -0 "$pid" 2>&1)" == *ermitted* ]]; then
      n=$(( n + 1 ))
    fi
  done
  w="${DGN_WORKER_TEST_SLOTS:-}"
  if ! [[ "$w" =~ ^[1-9][0-9]*$ ]]; then
    ncpu="$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1)"
    [[ "$ncpu" =~ ^[0-9]+$ ]] || ncpu=1
    w=$(( ncpu / 4 )); (( w < 1 )) && w=1
  fi
  echo "$n $w"
}

machine_pressure_arg() {
  if [[ -n "${DOGANY_DISPATCH_MACHINE_PRESSURE:-}" ]]; then
    [[ "$DOGANY_DISPATCH_MACHINE_PRESSURE" == off ]] || printf '%s' "$DOGANY_DISPATCH_MACHINE_PRESSURE"
    return 0
  fi
  if [[ "$MACHINE_PRESSURE_READ" != 1 ]]; then
    local snap="" load5="" ncpu="" avail="" held="" width="" ts=""
    if snap="$(machine_snapshot)"; then
      read -r load5 ncpu avail <<< "$snap"
    fi
    if ts="$(test_slots_held)"; then
      read -r held width <<< "$ts"
    fi
    MACHINE_PRESSURE_ARG="$load5,$ncpu,$avail,$held,$width"
    MACHINE_PRESSURE_READ=1
  fi
  printf '%s' "$MACHINE_PRESSURE_ARG"
}

reset_pass_i18n() {
  local lib="$SCRIPT_DIR/lib/agentlib.sh" out=""
  [[ -f "$lib" ]] || return 0
  out="$( ( set +e; AGENT_ROOT="$WORKSPACE"; . "$lib" >/dev/null 2>&1
            i18n "$1" ) 2>/dev/null )" || out=""
  [[ "$out" == "$1" ]] && out=""
  printf '%s' "$out"
}

reset_pass_text() {
  local kind="$1" name="" tpl="" win="${4:-7d}" wname=""
  case "$2" in claude) name="Claude" ;; codex) name="Codex" ;; *) name="$2" ;; esac
  tpl="$(reset_pass_i18n "dispatch.reset_pass.$kind")"
  if [[ -z "$tpl" ]]; then
    case "$kind" in
      expiring) tpl="{runtime} reset pass expires {expiry}. Use up what's left first, then redeem it to run again right away." ;;
      *)        tpl="{runtime} {window} usage is nearly full. Redeem a reset pass and I can run again right away." ;;
    esac
  fi
  [[ "$win" == 5h ]] || win=7d
  wname="$(reset_pass_i18n "dispatch.reset_pass.window_$win")"
  [[ -n "$wname" ]] || { [[ "$win" == 5h ]] && wname="5-hour" || wname="weekly"; }
  tpl="${tpl//\{runtime\}/$name}"
  tpl="${tpl//\{window\}/$wname}"
  printf '%s' "${tpl//\{expiry\}/$3}"
}

reset_pass_review() {

  [[ "${RETURN_ROUTE_KIND:-inbox}" == inbox ]] || return 0
  local cs xs snap="" out="" first="" tag rt kind key exp win text
  cs="$(reset_pass_spec claude)"; xs="$(reset_pass_spec codex)"
  if [[ -z "$cs" && -z "$xs" ]]; then
    RUNTIME_SELECTION_DETAIL+="; passes=none"
    return 0
  fi
  snap="$(claude_usage_snapshot)" || snap=""
  out="$(printf '%s' "$snap" | python3 -B "$RESET_PASSES_PY" review \
           --claude-spec "$cs" --codex-spec "$xs" \
           --codex-used "$CODEX_USED_PERCENT" --codex-resets-at "$CODEX_RESETS_AT" \
           --codex-window-mins "${CODEX_WINDOW_MINS:-10080}" 2>/dev/null)" || out=""
  if [[ -z "$out" ]]; then
    RUNTIME_SELECTION_DETAIL+="; passes: policy unavailable (treated as no pass)"
    return 0
  fi
  first="${out%%$'\n'*}"
  RUNTIME_SELECTION_DETAIL+="; passes: $first"
  while IFS=$'\t' read -r tag rt kind key exp win; do
    [[ "$tag" == PROMPT ]] || continue
    python3 -B "$RESET_PASSES_PY" once --stamp "$RESET_PASS_PROMPT_STAMP" --key "$key" 2>/dev/null || continue
    [[ "$exp" == - ]] && exp=""
    text="$(reset_pass_text "$kind" "$rt" "$exp" "$win")"
    if "$PUSH_SH" --framework-notice --text "$text" --audience owner >/dev/null 2>&1; then
      RUNTIME_SELECTION_DETAIL+="; pass_prompt=$kind:$rt sent"
    else
      RUNTIME_SELECTION_DETAIL+="; pass_prompt=$kind:$rt push FAILED (stamped; not retried this window)"
    fi
  done <<< "$out"
}

select_work_class_runtime() {
  [[ -n "$WORK_CLASS" ]] || return 1

  local pinned=""
  [[ -n "$REQUESTED_MODEL" ]] && pinned="$MODEL"
  [[ -n "$WORK_CLASS_DIFFICULTY" ]] || {
    echo "[dispatch] REFUSED: --work-class requires --difficulty" >&2; return 2;
  }
  local receipt candidate model place note
  receipt="$(python3 -B "$SCRIPT_DIR/work-class-resolve.py" --class "$WORK_CLASS" \
    --difficulty "$WORK_CLASS_DIFFICULTY" --root "$INSTANCE_ROOT")" || return 2
  WORK_CLASS_RECEIPT="$(printf '%s' "$receipt" | python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt_sha256"])')" || return 2
  WORK_CLASS_FIELDS="$(printf '%s' "$receipt" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["fields"],sort_keys=True,separators=(",",":")))')" || return 2
  local msrc=work-class
  while IFS=$'\t' read -r model place; do
    [[ -n "$model" && -n "$place" ]] || continue
    if [[ -n "$pinned" ]]; then
      
      if [[ "$pinned" == gpt-* || "$pinned" == *codex* ]]; then
        [[ "$place" == codex ]] || continue
      else
        [[ "$place" != codex ]] || continue
      fi
      model="$pinned" msrc=explicit
    fi
    RUNTIME="$place" MODEL="$model" REQUESTED_RUNTIME="$place"
    MODEL_SOURCE="$msrc" RUNTIME_SELECTION_SOURCE=work-class
    if slot_candidate_gate "$place"; then

      RUNTIME="$place" MODEL="$model" MODEL_SOURCE="$msrc"
      [[ "$place" == codex ]] && CODEX_MODEL="$model" || CODEX_MODEL=""
      WORK_CLASS_REASON="$WORK_CLASS/$WORK_CLASS_DIFFICULTY: first passing candidate $model@$place"
      [[ -z "$pinned" ]] || WORK_CLASS_REASON+=" (model selected)"
      RUNTIME_SELECTION_REASON=work_class_table
      RUNTIME_SELECTION_DETAIL="receipt=$WORK_CLASS_RECEIPT; reason=$WORK_CLASS_REASON; fields=$WORK_CLASS_FIELDS"
      RUNTIME_SELECTED="$RUNTIME"
      echo "[dispatch] work-class route: $WORK_CLASS_REASON"
      return 0
    fi
    note="${SLOT_CAND_NOTE:-hard guard refused}"

    echo "[dispatch] work-class candidate skipped: $model@$place: $note" >&2
  done < <(printf '%s' "$receipt" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(c["model"]+"\t"+c["place"]) for c in d["candidates"]]')
  if [[ -n "$pinned" ]]; then
    echo "[dispatch] REFUSED: no work-class place for pinned model $pinned passed hard guards; no automatic local fallback" >&2
    return 2
  fi
  echo "[dispatch] REFUSED: no work-class candidate passed hard guards; no automatic local fallback" >&2
  return 2
}

select_default_runtime() {
  if [[ -n "$WORK_CLASS" ]]; then select_work_class_runtime; return $?; fi
  case "$REQUESTED_RUNTIME" in
    auto) RUNTIME=claude-code ;;
    claude-code|codex) RUNTIME="$REQUESTED_RUNTIME" ;;
    *) echo "[dispatch] runtime needs an explicitly configured job driver" >&2; return 2 ;;
  esac
  if [[ "$RUNTIME" == codex ]]; then
    MODEL="${MODEL:-${DOGANY_DISPATCH_CODEX_MODEL:-opus}}"
    CODEX_MODEL="$MODEL"
  else
    MODEL="${MODEL:-${DOGANY_DISPATCH_MODEL:-sonnet}}"
  fi
  RUNTIME_SELECTED="$RUNTIME"
  RUNTIME_SELECTION_REASON=local_runtime
  RUNTIME_SELECTION_DETAIL="selected_runtime=$RUNTIME"
  record_runtime_decision selected "$RUNTIME_SELECTION_REASON" "$RUNTIME_SELECTION_DETAIL"
}

runtime_account_pace() {
  local pool="$1" used="" resets="" snap=""
  case "$pool" in
    codex)
      used="$CODEX_USED_PERCENT"; resets="$CODEX_RESETS_AT" ;;
    *)
      snap="$(claude_usage_snapshot)" || snap=""
      { read -r used; read -r resets; } < <(printf '%s' "$snap" | python3 -B -c '
import json, sys
try:
    u = json.load(sys.stdin)["usage"]
    print(u["seven_day_util"]); print(u["seven_day_resets_at"])
except Exception:
    print(""); print("")' 2>/dev/null)
      ;;
  esac
  [[ -n "$used" && -n "$resets" ]] || { echo unknown; return 0; }
  python3 -B "$RUNTIME_POLICY_PY" pace --traits "$RUNTIME_TRAITS_TSV" --pool "$pool" \
    --used "$used" --resets-at "$resets" 2>/dev/null || echo unknown
}

runtime_account_elapsed() {
  local pool="$1" resets="" snap=""
  case "$pool" in
    codex) resets="$CODEX_RESETS_AT" ;;
    *)
      snap="$(claude_usage_snapshot)" || snap=""
      resets="$(printf '%s' "$snap" | python3 -B -c '
import json, sys
try: print(json.load(sys.stdin)["usage"]["seven_day_resets_at"])
except Exception: print("")' 2>/dev/null)" ;;
  esac
  [[ -n "$resets" ]] || { echo unknown; return 0; }
  python3 -B - "$resets" <<'PY' 2>/dev/null || echo unknown
import datetime, sys, time
try:
    raw = sys.argv[1]
    try: reset = float(raw)
    except ValueError: reset = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    left = reset - time.time()
    if left < 0: left = 0
    if left > 7 * 86400: raise ValueError
    print(f"{(1 - left / (7 * 86400)) * 100:.2f}")
except (TypeError, ValueError):
    print("unknown")
PY
}

RUNTIME_POLICY_NOTE=""
RUNTIME_PREFERRED_WAIT=0
worker_claude_pace_gate() {

  WORKER_PACE_NOTE="$(runtime_account_pace claude-code)"
  local elapsed
  elapsed="$(runtime_account_elapsed claude-code)"
  python3 -B -c 'import sys
sys.path.insert(0, sys.argv[2])
try:
    from runtime_policy import under_pace
    ok = under_pace(float(sys.argv[1]), float(sys.argv[3]))
except (ValueError, ImportError):
    ok = False
sys.exit(0 if ok else 1)' "$WORKER_PACE_NOTE" "$SCRIPT_DIR/lib" "$elapsed" 2>/dev/null
}

runtime_move_allowed() {
  if [[ "${RETURN_RUNTIME_POLICY:-default}" == codex-only && "$2" != codex ]]; then
    RUNTIME_POLICY_NOTE="codex-only: seat-review cannot switch runtime"; return 1
  fi
  local from="$1" to="$2" cls="${3:-capacity}" pace=unknown elapsed=unknown out="" verdict="" rest=""
  local args=()
  pace="$(runtime_account_pace "$to")"
  elapsed="$(runtime_account_elapsed "$to")"
  out="$(python3 -B "$RUNTIME_POLICY_PY" check --traits "$RUNTIME_TRAITS_TSV" \
           --from "$from" --to "$to" --reason-class "$cls" --pace "$pace" --elapsed "$elapsed" \
           ${args[@]+"${args[@]}"} 2>/dev/null)" || out=""
  verdict="${out%%$'\t'*}"; rest="${out#*$'\t'}"
  if [[ "$verdict" != allow && "$verdict" != deny ]]; then
    RUNTIME_POLICY_NOTE="policy_unavailable: $from->$to not moved (fail closed)"
    return 1
  fi
  RUNTIME_POLICY_NOTE="${rest//$'\t'/: }"
  [[ "$verdict" == allow ]]
}

claude_to_codex_fallback() {
  local reason="$1"
  [[ -z "$RUNTIME_FALLBACK_DIRECTION" ]] || return 2
  runtime_move_allowed claude-code codex quota || return 2
  codex_quota_gate || return 2
  RUNTIME=codex
  MODEL="${CODEX_MODEL:-${DOGANY_DISPATCH_CODEX_MODEL:-opus}}"
  RUNTIME_FALLBACK_DIRECTION="claude-code->codex"
  RUNTIME_FALLBACK_NOTE="$reason"
  record_runtime_decision selected local_fallback "$reason"
}

codex_to_claude_switch() {
  local reason="$1"
  [[ -z "$RUNTIME_FALLBACK_DIRECTION" ]] || return 2
  runtime_move_allowed codex claude-code "${2:-quota}" || return 2
  local rc=0
  "$USAGE_GATE" || rc=$?
  if [[ $rc != 0 ]]; then CLAUDE_GATE_RC="$rc"; CLAUDE_GATE_NOTE="$reason"; return 3; fi
  RUNTIME=claude-code MODEL="${DOGANY_DISPATCH_MODEL:-sonnet}"
  RUNTIME_FALLBACK_DIRECTION="codex->claude-code"
  RUNTIME_FALLBACK_NOTE="$reason"
  record_runtime_decision selected local_fallback "$reason"
}

claude_reset_epoch() {
  local window="five_hour" key iso=""
  [[ "$1" == *"trigger=seven_day"* ]] && window="seven_day"
  key="${window}_resets_at"
  [[ -r "$USAGE_GATE_PY" ]] || return 1
  iso="$(python3 -B - "$USAGE_GATE_PY" "$key" <<'PY' 2>/dev/null
import importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    usage = mod.get_usage()
    if usage is None:
        raise RuntimeError("get_usage() returned None (read failed)")
    v = usage.get(sys.argv[2], "")
    if not v:
        raise RuntimeError("missing resets_at")
    print(v)
except Exception:
    sys.exit(1)
PY
)" || return 1
  python3 -c '
import sys
from datetime import datetime
try:
    dt = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    print(int(dt.timestamp()))
except Exception:
    sys.exit(1)
' "$iso" 2>/dev/null
}

defer_or_block() {
  local claude_rc="$1" claude_out="$2"
  local now deadline reset_epoch="" both self snap
  now="$(date +%s)"
  if [[ -n "$DISPATCH_DEFER_DEADLINE" ]]; then
    deadline="$DISPATCH_DEFER_DEADLINE"
  else
    deadline=$(( now + DISPATCH_DEFER_DEADLINE_MIN * 60 ))
  fi
  both="claude: ${CLAUDE_GATE_NOTE:-usage-gate rc=$claude_rc}; codex: ${FALLBACK_BLOCK_REASON:-not_evaluated}${CODEX_QUOTA_NOTE:+ ($CODEX_QUOTA_NOTE)}"

  [[ "$claude_rc" == slot ]] && both="all runtimes at cap: $claude_out"
  if [[ "$DISPATCH_DEFER_ATTEMPT" -ge "$DISPATCH_DEFER_MAX" ]]; then
    echo "[dispatch] BLOCKED (expired): re-evaluation budget exhausted (attempt $DISPATCH_DEFER_ATTEMPT/$DISPATCH_DEFER_MAX) -- $both Accepted path: resubmit after the reported resource boundary with a new DOGANY_DISPATCH_DEFER_DEADLINE_MIN window." >&2
    record_runtime_decision expired defer_budget_exhausted "$both" "" "$deadline" || true
    return 0
  fi
  if [[ "$now" -ge "$deadline" ]]; then
    echo "[dispatch] BLOCKED (expired): defer deadline reached -- $both Accepted path: resubmit after the reported resource boundary with a new DOGANY_DISPATCH_DEFER_DEADLINE_MIN window." >&2
    record_runtime_decision expired defer_deadline_reached "$both" "" "$deadline" || true
    return 0
  fi
  case "$claude_rc" in
    1) reset_epoch="$(claude_reset_epoch "$claude_out")" || reset_epoch="" ;;
    2) reset_epoch=$(( now + DISPATCH_LOOKUP_RETRY_SEC )) ;;   
    slot) reset_epoch=$(( now + DISPATCH_DEFER_RETRY_MIN * 60 )) ;;   

    *)
      echo "[dispatch] BLOCKED: both lanes blocked and the claude refusal (rc=$claude_rc) is not a quota/lookup shape -- no time boundary recovers it, closing Accepted path: resolve the named underlying configuration refusal, then resubmit the original request." >&2
      record_runtime_decision blocked both_lanes_blocked "$both" "" "$deadline" || true
      return 0 ;;
  esac
  [[ "$reset_epoch" =~ ^[0-9]+$ ]] || reset_epoch=$(( now + DISPATCH_DEFER_RETRY_MIN * 60 ))
  if [[ "$reset_epoch" -ge "$deadline" ]]; then
    echo "[dispatch] BLOCKED: earliest recovery boundary ($(date -r "$reset_epoch" '+%m-%d %H:%M' 2>/dev/null || echo "epoch $reset_epoch")) is past the defer deadline -- closing instead of waiting past the contract Accepted path: resubmit after the reported resource boundary with a new DOGANY_DISPATCH_DEFER_DEADLINE_MIN window." >&2
    record_runtime_decision blocked recovery_after_deadline "$both" "$reset_epoch" "$deadline" || true
    return 0
  fi
  mkdir -p "$DECISION_DIR" 2>/dev/null || true
  if [[ -z "$DECISION_ID" ]]; then
    DECISION_ID="rtd-$(date +%Y%m%d-%H%M%S)-$$-$(printf '%04d' $(( RANDOM % 10000 )))"
  fi

  snap="$DECISION_DIR/$DECISION_ID.prompt.md"
  if ! snapshot_dispatch_inputs "$snap"; then
    echo "[dispatch] BLOCKED: could not snapshot the prompt for the re-evaluation -- closing Accepted path: restore access to $DECISION_DIR and the re-evaluation reservation store, then resubmit the original request." >&2
    record_runtime_decision blocked defer_snapshot_failed "$both" "" "$deadline" || true
    return 0
  fi
  self="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"

  local replay=( /usr/bin/env
    "DOGANY_DISPATCH_RUNTIME=$REQUESTED_RUNTIME"
    "DOGANY_DISPATCH_DEFER_ATTEMPT=$(( DISPATCH_DEFER_ATTEMPT + 1 ))"
    "DOGANY_DISPATCH_DEFER_DEADLINE=$deadline"
    "DOGANY_DISPATCH_DEFER_ORIGIN=${DOGANY_DISPATCH_DEFER_ORIGIN:-$DECISION_ID}"
    "$self" --task "$SNAP_TASK_FILE"
    --label "$LABEL" --timeout "$TIMEOUT_MIN" --notify "${REQUESTED_NOTIFY:-$NOTIFY}" )
  [[ -z "$RETURN_ROUTE_JSON" ]] || replay+=( --return-route-json "$RETURN_ROUTE_JSON" --return-reservation "$RETURN_RESERVATION" )
  [[ -n "$DISPLAY_NAME" ]] && replay+=( --display-name "$DISPLAY_NAME" )
  [[ -n "$TICKET" ]]       && replay+=( --ticket "$TICKET" )
  [[ -n "$WHY_ID" ]] && replay+=( --why "$WHY_ID" )
  [[ "$READ_ONLY" == 1 ]] && replay+=( --read-only )
  [[ "$WRITE_REQUESTED" == 1 ]] && replay+=( --write )
  [[ -n "$JOB_WORKDIR" ]] && replay+=( --workdir "$JOB_WORKDIR" )
  local snapshot_attach
  for snapshot_attach in ${SNAP_ATTACH_FILES[@]+"${SNAP_ATTACH_FILES[@]}"}; do
    replay+=(--attach "$snapshot_attach")
  done
  [[ -n "$REQUESTED_MODEL" ]] && replay+=( --model "$REQUESTED_MODEL" )
  if "$USAGE_DEFER_RETRY" --label "dispatch-$DECISION_ID" \
       --reset-epoch "$reset_epoch" -- "${replay[@]}" >&2; then
    RETURN_RESERVATION_DURABLE=1  
    local next_at=$(( reset_epoch + 120 ))   
    echo "[dispatch] DEFERRED: $([[ "$claude_rc" == slot ]] && echo "no runtime has room" || echo "both lanes blocked"); re-evaluation reserved at $(date -r "$next_at" '+%m-%d %H:%M' 2>/dev/null || echo "epoch $next_at") (attempt $(( DISPATCH_DEFER_ATTEMPT + 1 ))/$DISPATCH_DEFER_MAX, deadline $(date -r "$deadline" '+%m-%d %H:%M' 2>/dev/null || echo "epoch $deadline")) -- $both" >&2
    record_runtime_decision deferred "$([[ "$claude_rc" == slot ]] && echo all_runtimes_at_cap || echo both_lanes_blocked)" "$both" "$next_at" "$deadline" || true
  else
    echo "[dispatch] BLOCKED: both lanes blocked and the re-evaluation reservation could not be persisted -- closing with reasons on record (a deferral without a persisted reservation is not a deferral) Accepted path: restore access to $DECISION_DIR and the re-evaluation reservation store, then resubmit the original request." >&2
    record_runtime_decision blocked defer_reservation_failed "$both" "" "$deadline" || true
  fi
  return 0
}

slot_candidate_gate() {
  local candidate="$1"
  case "$candidate" in
    claude-code) "$USAGE_GATE" || { SLOT_CAND_NOTE="usage gate refused"; return 1; } ;;
    codex) codex_quota_gate || { SLOT_CAND_NOTE="$CODEX_QUOTA_NOTE"; return 1; } ;;
    *) SLOT_CAND_NOTE="runtime requires configured job driver"; return 1 ;;
  esac
  compute_dynamic_cap "$candidate"
  [[ "$DYNAMIC_CAP_VALUE" -gt 0 ]] || { SLOT_CAND_NOTE="$DYNAMIC_CAP_NOTE"; return 1; }
}

cap_note_brief() {
  local n="$1"
  if [[ "$n" == *"; machine "* ]]; then
    n="machine ${n##*; machine }"
    n="${n% (tightest headroom governs, DGN-1835)}"
  fi
  printf '%s' "$n"
}

if [[ -n "${DOGANY_DISPATCH_MAX_ACTIVE:-}" ]]; then
  case "$DOGANY_DISPATCH_MAX_ACTIVE" in
    *[!0-9]*|0) echo "[dispatch] DOGANY_DISPATCH_MAX_ACTIVE must be a positive integer (got '$DOGANY_DISPATCH_MAX_ACTIVE')" >&2; exit 2 ;;
  esac
fi

MAX_ACTIVE=3

DYNAMIC_CAP_NOTE=""
DYNAMIC_CAP_VALUE=3

HOST_CAP_VALUE=3

DISPATCH_HORIZON_H=0.4          
DISPATCH_QUOTA_RESERVE_PCT=10   
DISPATCH_LIVE_BURN_5H=7.1       
DISPATCH_LIVE_BURN_7D=1.2       
DISPATCH_CODEX_BURN=15          
DISPATCH_LOAD_PER_NCPU=1.5      
DISPATCH_LOAD_PER_WORKER=1.0    
DISPATCH_MEM_PER_WORKER_MB=512  
DISPATCH_MEM_RESERVE_MB=1024    

dispatch_worker_burn() {
  case "$1" in
    opus)   echo "6.3 1.1 measured" ;;
    sonnet) echo "10.7 1.7 measured" ;;
    fable)  echo "33.6 4.4 measured" ;;
    *)      echo "33.6 4.4 unmeasured" ;;
  esac
}

quota_workers() {
  python3 - "$@" <<'PY'
import math, sys
rem, reserve, live, burn, left = sys.argv[1:6]
h = float(sys.argv[6])
try:
    h = min(h, max(float(left), 0.05))
except ValueError:
    pass  # reset time unknown -> the full horizon (the conservative side)
room = float(rem) - float(reserve) - float(live) * h
print(max(0, math.floor(room / (float(burn) * h))), "%.2f" % h)
PY
}

claude_hours_left() {
  [[ -r "$USAGE_GATE_PY" ]] || return 1
  python3 -B - "$USAGE_GATE_PY" <<'PY' 2>/dev/null
import datetime, importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    usage = mod.get_usage() or {}
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for key in ("five_hour_resets_at", "seven_day_resets_at"):
        if key == "five_hour_resets_at" and usage.get("five_hour_absent"):
            out.append("absent")
            continue
        at = datetime.datetime.fromisoformat(usage[key].replace("Z", "+00:00"))
        out.append("%.2f" % ((at - now).total_seconds() / 3600))
    print(*out)
except Exception:
    sys.exit(1)
PY
}

machine_snapshot() {
  if [[ -n "${DOGANY_DISPATCH_MACHINE_SNAPSHOT:-}" ]]; then
    [[ "$DOGANY_DISPATCH_MACHINE_SNAPSHOT" =~ ^[0-9.]+\ [1-9][0-9]*\ [0-9]+$ ]] || return 1
    printf '%s\n' "$DOGANY_DISPATCH_MACHINE_SNAPSHOT"
    return 0
  fi
  python3 - <<'PY' 2>/dev/null
import os, subprocess, sys
try:
    load5 = os.getloadavg()[1]
    ncpu = os.cpu_count()
    if sys.platform == "darwin":
        sysctl = lambda k: int(subprocess.check_output(["sysctl", "-n", k], text=True))
        avail = sysctl("hw.memsize") * sysctl("kern.memorystatus_level") / 100
    else:
        with open("/proc/meminfo") as f:
            avail = next(int(l.split()[1]) * 1024 for l in f if l.startswith("MemAvailable:"))
    print("%.2f" % load5, ncpu, int(avail // 2**20))
except Exception:
    sys.exit(1)
PY
}

MACHINE_CAP_READ=0
MACHINE_CAP=""
MACHINE_NOTE=""
local_active_count() {
  echo "$(( $(read_active_count claude-code) + $(read_active_count codex) + $(read_active_count claude-api) ))"
}

machine_workers() {
  [[ "$MACHINE_CAP_READ" == 1 ]] && return 0
  MACHINE_CAP_READ=1
  local snap load5 ncpu avail active held=0 width ts
  MACHINE_CAP=""
  if ! snap="$(machine_snapshot)"; then
    MACHINE_NOTE="machine read FAILED (no load/memory bound applied)"
    return 0
  fi
  read -r load5 ncpu avail <<<"$snap"
  active="$(local_active_count)"

  if ts="$(test_slots_held)"; then read -r held width <<< "$ts"; fi
  [[ "$held" =~ ^[0-9]+$ ]] || held=0
  read -r MACHINE_CAP MACHINE_NOTE < <(python3 - "$load5" "$ncpu" "$avail" "$active" "$held" \
    "$DISPATCH_LOAD_PER_NCPU" "$DISPATCH_LOAD_PER_WORKER" \
    "$DISPATCH_MEM_RESERVE_MB" "$DISPATCH_MEM_PER_WORKER_MB" <<'PYM'
import math, sys
load5, ncpu, avail, active, held, per_ncpu, per_w, mres, mper = map(float, sys.argv[1:10])
limit = ncpu * per_ncpu
by_load = max(0, math.floor((limit - load5) / per_w))
by_mem = max(0, math.floor((avail - mres) / mper))
room = max(0, min(by_load, by_mem) - int(held))
cap = int(active + room)
print(cap, "machine load5=%.1f/%g (+%d) mem_avail=%dMB (+%d) ncpu=%d test_slots_held=%d local_active=%d -> shared cap %d"
      % (load5, limit, by_load, avail, by_mem, ncpu, held, active, cap))
PYM
)
}

claude_quota_remaining() {
  [[ -r "$USAGE_GATE_PY" ]] || return 1
  python3 -B - "$USAGE_GATE_PY" <<'PY' 2>/dev/null
import importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    usage = mod.get_usage()
    if usage is None:
        raise RuntimeError("get_usage() returned None (read failed)")
    fh = float(usage.get("five_hour_util", 0))
    sd = float(usage.get("seven_day_util", 0))
    if not (0 <= fh <= 100 and 0 <= sd <= 100):
        raise ValueError("invalid utilization")
    print("absent" if usage.get("five_hour_absent") else int(100 - fh), int(100 - sd))
except Exception:
    sys.exit(1)
PY
}

codex_quota_remaining() {
  if [[ -n "$CODEX_USED_PERCENT" ]]; then
    echo $(( 100 - CODEX_USED_PERCENT ))
    return 0
  fi
  local py_out="" rc=0 used=""
  py_out="$(python3 "$CODEX_RATELIMIT_SCRIPT" --json --timeout "$CODEX_QUOTA_TIMEOUT" 2>/dev/null)" || rc=$?
  [[ $rc -eq 0 ]] || return 1
  used="$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("used_percent",""))
except Exception:
    print("")' 2>/dev/null)"
  [[ "$used" =~ ^[0-9]+$ ]] || return 1
  echo $(( 100 - used ))
}

claude_weekly_cap_waived() { return 1; }

compute_dynamic_cap() {
  local runtime="$1"
  DYNAMIC_CAP_VALUE=3
  HOST_CAP_VALUE=3
  DYNAMIC_CAP_NOTE=""
  case "$runtime" in
    claude-code|codex|script) ;;
    *) DYNAMIC_CAP_VALUE=0 HOST_CAP_VALUE=0; DYNAMIC_CAP_NOTE="requires configured job driver"; return 0 ;;
  esac

  if [[ "$runtime" == "script" ]]; then
    DYNAMIC_CAP_VALUE="${DOGANY_SCRIPT_JOB_MAX_ACTIVE:-2}"
    [[ "$DYNAMIC_CAP_VALUE" =~ ^[0-9]+$ && "$DYNAMIC_CAP_VALUE" -ge 1 ]] || DYNAMIC_CAP_VALUE=2
    HOST_CAP_VALUE="$DYNAMIC_CAP_VALUE"
    DYNAMIC_CAP_NOTE="script lane cap $DYNAMIC_CAP_VALUE (DOGANY_SCRIPT_JOB_MAX_ACTIVE; no quota term)"
    return 0
  fi

  local override_cap=""
  [[ -n "${DOGANY_DISPATCH_MAX_ACTIVE:-}" ]] && override_cap="$DOGANY_DISPATCH_MAX_ACTIVE"

  local quota_cap quota_note h
  if [[ -n "$override_cap" ]]; then
    quota_cap="$override_cap"
    quota_note="OVERRIDE: DOGANY_DISPATCH_MAX_ACTIVE=$override_cap replaces the quota-derived cap only (manual, ignores live quota; the machine ceiling still applies -- DGN-2029; announced every time so a raised cap can never go unnoticed like 2026-08-26)"
  elif [[ "$runtime" == "claude-api" ]]; then

    quota_cap=3
    quota_note="claude-api: monthly API credit pool (${API_POOL_NOTE:-pool checked at selection}), no 5h/7d quota term -> design default 3"
  elif [[ "$runtime" == "codex" ]]; then
    local rem
    if ! rem="$(codex_quota_remaining)"; then
      DYNAMIC_CAP_NOTE="codex quota read FAILED -> falling back to fixed cap 3 (unknown is not permission)"
      DYNAMIC_CAP_VALUE=3
      return 0
    fi

    local creserve=$(( 100 - CODEX_QUOTA_MAX_PERCENT )) pm pk pkey pdetail pout cap_rem="$rem" slack_out
    local cwin=7d
    [[ "${CODEX_WINDOW_MINS:-10080}" =~ ^[0-9]+$ && "${CODEX_WINDOW_MINS:-10080}" -lt 1440 ]] && cwin=5h
    pout="$(reset_pass_mode codex "$(( 100 - rem ))" "$CODEX_RESETS_AT" "$cwin")"
    IFS=$'\t' read -r pm pk pkey pdetail <<< "$pout"
    if [[ "$pm" == expiry_boost ]]; then
      quota_cap="$RESET_PASS_BOOST_CODEX"
      quota_note="codex weekly remaining=${rem}% -- $pdetail -> boost $quota_cap"
    else
      if [[ "$pm" != none ]]; then
        creserve=0

        slack_out="$(reset_pass_slack codex "$(( 100 - rem ))" "$CODEX_RESETS_AT" "$cwin")" || slack_out=""
        [[ -n "$slack_out" ]] && read -r cap_rem _ <<< "$slack_out"
      fi
      read -r quota_cap h <<<"$(quota_workers "$cap_rem" "$creserve" 0 \
        "$DISPATCH_CODEX_BURN" "?" "$DISPATCH_HORIZON_H")"
      quota_note="codex weekly remaining=${rem}% reserve=${creserve}% worker=${DISPATCH_CODEX_BURN}%/h H=${h}h -> $quota_cap"
      [[ "$pm" != none ]] && quota_note+=" ($pdetail)"
    fi
  else
    local out fh_rem sd_rem left fh_left="?" sd_left="?" burn5 burn7 measured cap_5h cap_7d
    if ! out="$(claude_quota_remaining)"; then
      DYNAMIC_CAP_NOTE="claude usage read FAILED -> falling back to fixed cap 3 (unknown is not permission)"
      DYNAMIC_CAP_VALUE=3
      return 0
    fi
    fh_rem="${out%% *}"; sd_rem="${out##* }"
    if left="$(claude_hours_left)"; then
      fh_left="${left%% *}"; sd_left="${left##* }"
    fi
    read -r burn5 burn7 measured <<<"$(dispatch_worker_burn "${MODEL:-}")"
    quota_note="claude model=${MODEL:-?} ($measured"
    [[ "$measured" == "unmeasured" ]] && quota_note+=", priced as fable, the worst measured"

    local pm pk pkey pdetail pout freserve="$DISPATCH_QUOTA_RESERVE_PCT" fh_drop=""
    if [[ "$fh_rem" == absent ]]; then
      cap_5h="$RESET_PASS_BOOST_CLAUDE"; fh_drop=1
      quota_note+="): weekly window only"
    else
      pout="$(reset_pass_mode claude "$(( 100 - fh_rem ))" "" 5h)"
      IFS=$'\t' read -r pm pk pkey pdetail <<< "$pout"
      if [[ "$pm" == expiry_boost ]]; then
        cap_5h="$RESET_PASS_BOOST_CLAUDE"; fh_drop=1
        quota_note+="): 5h remaining=${fh_rem}% dropped -- $pdetail"
      else
        [[ "$pm" != none ]] && freserve=0
        read -r cap_5h h <<<"$(quota_workers "$fh_rem" "$freserve" \
          "$DISPATCH_LIVE_BURN_5H" "$burn5" "$fh_left" "$DISPATCH_HORIZON_H")"
        quota_note+="): 5h remaining=${fh_rem}% reserve=${freserve}%"
        [[ "$pm" != none ]] && quota_note+=" ($pdetail)"
        quota_note+=" live=${DISPATCH_LIVE_BURN_5H}%/h worker=${burn5}%/h H=${h}h -> $cap_5h"
      fi
    fi
    if claude_weekly_cap_waived; then
      quota_cap="$cap_5h"
      quota_note+="; 7d waived (USAGE_GATE_7D=auto-only, owner-driven live session)"
    else
      local sreserve="$DISPATCH_QUOTA_RESERVE_PCT"
      pout="$(reset_pass_mode claude "$(( 100 - sd_rem ))" "" 7d)"
      IFS=$'\t' read -r pm pk pkey pdetail <<< "$pout"
      if [[ "$pm" == expiry_boost ]]; then
        quota_cap="$cap_5h"
        quota_note+="; 7d remaining=${sd_rem}% dropped -- $pdetail"
        [[ -n "$fh_drop" ]] && quota_note+="; both windows dropped -> boost $quota_cap"
      else
        [[ "$pm" != none ]] && sreserve=0
        read -r cap_7d h <<<"$(quota_workers "$sd_rem" "$sreserve" \
          "$DISPATCH_LIVE_BURN_7D" "$burn7" "$sd_left" "$DISPATCH_HORIZON_H")"
        if [[ -n "$fh_drop" ]]; then
          quota_cap="$cap_7d"
        else
          quota_cap=$(( cap_5h < cap_7d ? cap_5h : cap_7d ))
        fi
        quota_note+="; 7d remaining=${sd_rem}%"
        [[ "$pm" != none ]] && quota_note+=" reserve=0% ($pdetail)"
        quota_note+=" live=${DISPATCH_LIVE_BURN_7D}%/h worker=${burn7}%/h H=${h}h -> $cap_7d"
      fi
    fi
  fi

  HOST_CAP_VALUE="$quota_cap"
  machine_workers "$runtime"
  DYNAMIC_CAP_VALUE="$quota_cap"
  if [[ -n "$MACHINE_CAP" ]] && (( MACHINE_CAP < quota_cap )); then
    DYNAMIC_CAP_VALUE="$MACHINE_CAP"
  fi
  DYNAMIC_CAP_NOTE="$quota_note; $MACHINE_NOTE => cap=$DYNAMIC_CAP_VALUE (tightest headroom governs, DGN-1835)"
  return 0
}

PROMPT_MAX_BYTES=102400   
DEFAULT_TIMEOUT_MIN=60

usage() { printf '%s\n' 'dispatch --task FILE --label LABEL --read-only [--runtime claude-code|codex]' 'dispatch --script --label LABEL [--workdir PATH] -- COMMAND ARG...' 'dispatch --list | --cancel RUNID'; }

MODE="spawn"
TASK_FILE=""
MODEL=""
REQUESTED_MODEL=""
CODEX_MODEL=""

MODEL_SOURCE="explicit"

EFFORT=""

EFFORT_SOURCE=""

EFFORT_WARNING=""
LABEL=""
TICKET=""

WHY_ID=""
DISPATCH_WHY=""

BRIEF_WHY_WHAT=""
BRIEF_WHY=""
BRIEF_WHAT=""
BRIEF_SAMPLE=""

BRIEF_UNIT_GATE=""
BRIEF_UNIT=""
BRIEF_GATE=""

WORK_CLASS=""
WORK_CLASS_DIFFICULTY=""
WORK_CLASS_RECEIPT=""
WORK_CLASS_REASON=""
WORK_CLASS_FIELDS=""


DISPLAY_NAME=""
DISPATCH_KIND=""

SCRIPT_JOB=0
NO_QUEUE=0
SCRIPT_WORKDIR=""
SCRIPT_ARGV=()
SCRIPT_USAGE_FILE=""
HOST_FORCE=0

READ_ONLY=0
WRITE_REQUESTED=0


NEEDS_CONNECTORS="${DOGANY_DISPATCH_NEEDS_CONNECTORS:-0}"


ACCESS_MODE=""
JOB_WORKDIR=""

SANDBOX_PROFILE=""

SESSION_ID=""


LIVE_TARGETS_ARG=()
LIVE_TARGETS=""
PLAN_DIR=""
PLAN_STATE=""
TIMEOUT_MIN="$DEFAULT_TIMEOUT_MIN"
TIMEOUT_GIVEN=0

NOTIFY="push"

CANCEL_BY=""
CANCEL_RUNID=""
WORKER_RUNDIR=""
BACKFILL_APPLY=0

MODEL_SLOT=""
BG_RESULT=""
BG_ELAPSED_SECS=""
BG_END_CLOCK=""
BG_NOT_BEFORE=""

ORIG_ARGV=("$@")
HOST_CANCEL_TICKET=""

HOST_ADOPT_LEASE="${DOGANY_HOST_LEASE:-}"
unset DOGANY_HOST_LEASE

REPLACED_FROM="${DOGANY_DISPATCH_REPLACED_FROM:-}"
REPLACE_REASON="${DOGANY_DISPATCH_REPLACE_REASON:-}"
unset DOGANY_DISPATCH_REPLACED_FROM DOGANY_DISPATCH_REPLACE_REASON

while [[ $# -gt 0 ]]; do
  case "$1" in
    
    --return-route-json) RETURN_ROUTE_JSON="$2"; shift 2 ;;
    --return-reservation) RETURN_RESERVATION="$2"; shift 2 ;;
    --job-kind) RETURN_JOB_KIND="$2"; shift 2 ;;
    --parent-event-id) RETURN_PARENT_EVENT_ID="$2"; shift 2 ;;
    --no-queue) NO_QUEUE=1; shift ;;  
    --script)   SCRIPT_JOB=1; shift ;;
    --workdir)  SCRIPT_WORKDIR="$2"; JOB_WORKDIR="$2"; shift 2 ;;
    --)         shift; SCRIPT_ARGV=("$@"); break ;;   

    --cancel-ticket) MODE="host-cancel"; HOST_CANCEL_TICKET="$2"; shift 2 ;;
    --task)     TASK_FILE="$2"; shift 2 ;;

    --runtime)  RUNTIME="$2"; REQUESTED_RUNTIME="$2"; RUNTIME_SELECTION_SOURCE="flag"; shift 2 ;;
    --model)    MODEL="$2"; REQUESTED_MODEL="$2"; shift 2 ;;

    --effort)   EFFORT="$2"; shift 2 ;;

    --work-class) WORK_CLASS="$2"; shift 2 ;;
    --difficulty) WORK_CLASS_DIFFICULTY="$2"; shift 2 ;;
    --why) WHY_ID="$2"; shift 2 ;;
    --unguarded-codex) CODEX_UNGUARDED_WHY="$2"; shift 2 ;;
    --label)    LABEL="$2"; shift 2 ;;
    --display-name) DISPLAY_NAME="$2"; shift 2 ;;
    --kind) DISPATCH_KIND="${2:-}"; [[ $# -ge 2 ]] || exit 2; shift 2 ;;
    --ticket)   TICKET="$2"; shift 2 ;;
    --read-only) READ_ONLY=1; shift ;;

    --attach)   ATTACH_FILES+=("$2"); shift 2 ;;
    

    --live-target) LIVE_TARGETS_ARG+=("$2"); shift 2 ;;
    --timeout)  TIMEOUT_MIN="$2"; TIMEOUT_GIVEN=1; shift 2 ;;
    --notify)   NOTIFY="$2"; shift 2 ;;
    --reclaim) MODE="reclaim"; shift ;;
    --cancel)   MODE="cancel"; CANCEL_RUNID="$2"; shift 2 ;;
    --apply-plan) MODE="apply-plan"; CANCEL_RUNID="$2"; shift 2 ;;
    --list)     MODE="list"; shift 1 ;;
    
    --worker-run) MODE="worker"; WORKER_RUNDIR="$2"; shift 2 ;;
    --watchdog-run) MODE="watchdog"; WORKER_RUNDIR="$2"; shift 2 ;;

    --queue-probe) MODE="queue-probe"; shift ;;
    --start-notice-flush) MODE="start-notice-flush"; shift 1 ;;

    --start-notice-enqueue) MODE="bg-start-notice"; shift 1 ;;
    --end-notice) MODE="bg-end-notice"; shift 1 ;;
    --model-slot) MODEL_SLOT="$2"; shift 2 ;;
    --result)   BG_RESULT="$2"; shift 2 ;;
    --elapsed-secs) BG_ELAPSED_SECS="$2"; shift 2 ;;
    --end-clock) BG_END_CLOCK="$2"; shift 2 ;;
    --not-before) BG_NOT_BEFORE="$2"; shift 2 ;;
    --backfill-model-observed) MODE="backfill"; shift 1 ;;
    --apply)    BACKFILL_APPLY=1; shift 1 ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "[dispatch] unknown arg: $1" >&2; usage >&2; exit 1 ;;
  esac
done

dir_lock() {
  local lock="$1" tries=0 age now mtime
  while ! mkdir "$lock" 2>/dev/null; do
    tries=$(( tries + 1 ))
    if [[ $tries -ge 50 ]]; then
      
      now="$(date +%s)"
      mtime="$(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || echo "$now")"
      age=$(( now - mtime ))
      if [[ $age -gt 30 ]]; then
        rmdir "$lock" 2>/dev/null || true
        continue
      fi
      echo "[dispatch] lock timeout: $lock Accepted path: inspect the holder of $lock and retry after that operation completes." >&2
      return 1
    fi
    sleep 0.1
  done
  return 0
}

dir_unlock() {
  rmdir "$1" 2>/dev/null || true
}

count_lock() {

  mkdir -p "$DISPATCH_DIR" 2>/dev/null || true
  dir_lock "$COUNT_LOCK_DIR"
}

count_unlock() {
  dir_unlock "$COUNT_LOCK_DIR"
}

active_count_file() {
  if [[ "$1" == "codex" ]]; then
    echo "$ACTIVE_COUNT_FILE_CODEX"
  elif [[ "$1" == "claude-api" ]]; then
    echo "$ACTIVE_COUNT_FILE_API"
  elif [[ "$1" == "script" ]]; then
    echo "$ACTIVE_COUNT_FILE_SCRIPT"
  else
    echo "$ACTIVE_COUNT_FILE"
  fi
}

read_active_count() {
  local runtime="${1:-$RUNTIME}" f v="0"
  f="$(active_count_file "$runtime")"
  if [[ -f "$f" ]]; then
    v="$(cat "$f" 2>/dev/null || echo 0)"
    v="${v//[^0-9]/}"
  fi
  echo "${v:-0}"
}

HOST_DISPATCHER_PY="$SCRIPT_DIR/lib/host_dispatcher.py"
HOST_LEASE_ID=""
HOST_FULL_NOTE=""
HOST_TICKET_ID=""
HOST_QUEUE_POSITION=""
HOST_REFUSAL_ARG=()
HOST_AGENT_ID=""
HOST_DEGRADED_FILE="$DISPATCH_DIR/degraded.lease"

host_dispatch_on() {
  [[ -r "$HOST_DISPATCHER_PY" ]]
}

host_agent_id() {
  if [[ -z "$HOST_AGENT_ID" ]]; then
    HOST_AGENT_ID="a$(cd "$WORKSPACE" 2>/dev/null && pwd -P | cksum | cut -d' ' -f1)"
  fi
  printf '%s' "$HOST_AGENT_ID"
}

host_account() {
  python3 -B "$HOST_DISPATCHER_PY" account --runtime "$1" \
    --api-dir "${API_CREDIT_ACCOUNT_DIR:-}" 2>/dev/null || echo unknown
}

host_call() {
  local out rc=0
  out="$(python3 -B "$HOST_DISPATCHER_PY" "$@" 2>/dev/null)" || rc=$?
  IFS=$'\t' read -r HOST_VERDICT HOST_IDENT HOST_NOTE <<< "$out"
  if [[ $rc -eq 3 || -z "$HOST_VERDICT" || "$HOST_VERDICT" == unavailable ]]; then
    HOST_NOTE="${HOST_NOTE:-no output (rc=$rc)}"
    return 3
  fi
  return 0
}


host_run_kind() {
  case "${DOGANY_DISPATCH_RUN_KIND:-}" in
    llm|build) printf '%s' "$DOGANY_DISPATCH_RUN_KIND"; return ;;
  esac
  if [[ "${SCRIPT_JOB:-0}" == 1 ]]; then printf build; else printf llm; fi
}

host_common_args() {
  local runtime="$1" cap="$2"
  HOST_ARGS=(--agent "$(host_agent_id)" --workspace "$WORKSPACE"
    --script "$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
    --display "$(owner_facing_name 2>/dev/null || printf '%s' "$LABEL")"
    --runtime "$runtime" --account "$(host_account "$runtime")"
    --cap "$cap" --cap-note "$DYNAMIC_CAP_NOTE"
    --label "$LABEL" --model "${MODEL:-}" --kind "$(host_run_kind)" --runid "${RUNID:-}")
}

host_refresh_cap() {  
  local runtime="$1" cap="$2"
  [[ "${DOGANY_DISPATCH_QUEUE_PROBE:-0}" != 1 ]] || return 0
  host_dispatch_on || return 0
  host_call refresh --runtime "$runtime" --account "$(host_account "$runtime")" \
    --cap "$cap" --cap-note "$DYNAMIC_CAP_NOTE" >/dev/null || true
}

host_acquire() {
  if [[ "${RETURN_RUNTIME_POLICY:-default}" == codex-only && "$1" != codex ]]; then
    HOST_FULL_NOTE="seat-review requires codex"; return 1
  fi
  local runtime="$1" cap="$2"
  HOST_LEASE_ID=""
  host_dispatch_on || return 0
  host_common_args "$runtime" "$cap"
  local force_arg=()
  [[ "$HOST_FORCE" == 1 ]] && force_arg=(--force)
  if host_call acquire "${HOST_ARGS[@]}" --pid "$$" --lease "$HOST_ADOPT_LEASE" ${force_arg[@]+"${force_arg[@]}"}; then
    HOST_ADOPT_LEASE=""
    if [[ "$HOST_VERDICT" == blocked ]]; then
      echo "[dispatch] REFUSED: $HOST_NOTE" >&2
      return 1
    fi
    if [[ "$HOST_VERDICT" == duplicate ]]; then
      echo "[dispatch] REFUSED: $HOST_NOTE; live runid=$HOST_IDENT Accepted path: follow the existing run/request $HOST_IDENT in dispatch-detached.sh --list instead of launching another." >&2
      exit 2
    fi
    if [[ "$HOST_VERDICT" == granted ]]; then
      HOST_LEASE_ID="$HOST_IDENT"
      return 0
    fi
    HOST_FULL_NOTE="host: $HOST_NOTE"
    DYNAMIC_CAP_NOTE="$DYNAMIC_CAP_NOTE; $HOST_FULL_NOTE"
    return 1
  fi
  host_degraded_acquire "$runtime"
}

host_degraded_acquire() {
  local pid
  if [[ "$HOST_FORCE" == 1 ]]; then
    
    echo "[dispatch] host dispatcher UNAVAILABLE ($HOST_NOTE) -- --no-queue script job starts without a host lease" >&2
    HOST_LEASE_ID=""
    return 0
  fi
  echo "[dispatch] host dispatcher UNAVAILABLE ($HOST_NOTE) -- degraded mode: one local run of this agent, paid lanes closed (DGN-1979)" >&2
  if [[ "$1" == claude-api ]]; then
    HOST_FULL_NOTE="host dispatcher unavailable: $1 is a paid lane, closed in degraded mode"
    DYNAMIC_CAP_NOTE="$DYNAMIC_CAP_NOTE; $HOST_FULL_NOTE"
    return 1
  fi
  pid="$(cat "$HOST_DEGRADED_FILE" 2>/dev/null | tr -dc 0-9)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    HOST_FULL_NOTE="host dispatcher unavailable: degraded mode allows one local run and pid $pid holds it"
    DYNAMIC_CAP_NOTE="$DYNAMIC_CAP_NOTE; $HOST_FULL_NOTE"
    return 1
  fi
  mkdir -p "$DISPATCH_DIR" 2>/dev/null || true
  printf '%s\n' "$$" > "$HOST_DEGRADED_FILE"
  HOST_LEASE_ID="degraded"
  return 0
}

host_bind() {
  [[ -n "$HOST_LEASE_ID" ]] || return 0
  if [[ "$HOST_LEASE_ID" == degraded ]]; then
    [[ -n "$1" ]] && printf '%s\n' "$1" > "$HOST_DEGRADED_FILE"
    return 0
  fi
  [[ -n "$1" ]] || return 0
  host_call bind --lease "$HOST_LEASE_ID" --pid "$1" --runid "${RUNID:-}" --marker "${RUNDIR:-}" || true
}

host_release() {
  [[ -n "${HOST_LEASE_ID:-}" ]] || return 0
  if [[ "$HOST_LEASE_ID" == degraded ]]; then
    rm -f "$HOST_DEGRADED_FILE" 2>/dev/null || true
  else
    host_call release --lease "$HOST_LEASE_ID" || true
  fi
  HOST_LEASE_ID=""
}

snapshot_dispatch_inputs() {  
  local snap="$1" f dest i=0
  SNAP_TASK_FILE="$snap"; SNAP_ATTACH_FILES=()
  cp "$TASK_FILE" "$snap" 2>/dev/null || return 1
  [[ -n "${ATTACH_BRIEF_FILE:-}" && ${#ATTACH_FILES[@]} -gt 0 ]] || return 0
  mkdir -p "$snap.inputs" || return 1
  SNAP_TASK_FILE="$snap.inputs/brief.md"
  cp "$ATTACH_BRIEF_FILE" "$SNAP_TASK_FILE" 2>/dev/null || return 1
  for f in "${ATTACH_FILES[@]}"; do
    i=$(( i + 1 ))
    mkdir -p "$snap.inputs/$i" || return 1
    dest="$snap.inputs/$i/$(basename "$f")"
    cp "$f" "$dest" 2>/dev/null || return 1
    SNAP_ATTACH_FILES+=("$dest")
  done
}

host_enqueue() {
  local snap="$DECISION_DIR/queue-$(date +%s)-$$-$RANDOM"
  mkdir -p "$DECISION_DIR"
  if [[ "$SCRIPT_JOB" == 1 ]]; then
    return_route_request_json "$snap.json" "${ORIG_ARGV[@]}" || return 1
  else
    snapshot_dispatch_inputs "$snap.md" || return 1
    local replay=(--task "$SNAP_TASK_FILE" --label "$LABEL" --read-only --notify "${REQUESTED_NOTIFY:-$NOTIFY}" --timeout "$TIMEOUT_MIN")
    [[ -z "$DISPLAY_NAME" ]] || replay+=(--display-name "$DISPLAY_NAME")
    [[ "$REQUESTED_RUNTIME" == auto ]] || replay+=(--runtime "$REQUESTED_RUNTIME")
    [[ -z "$REQUESTED_MODEL" ]] || replay+=(--model "$REQUESTED_MODEL")
    [[ -z "$EFFORT" ]] || replay+=(--effort "$EFFORT")
    [[ -z "$JOB_WORKDIR" ]] || replay+=(--workdir "$JOB_WORKDIR")
    [[ -z "$WORK_CLASS" ]] || replay+=(--work-class "$WORK_CLASS" --difficulty "$WORK_CLASS_DIFFICULTY")
    local attached
    for attached in ${SNAP_ATTACH_FILES[@]+"${SNAP_ATTACH_FILES[@]}"}; do replay+=(--attach "$attached"); done
    return_route_request_json "$snap.json" "${replay[@]}" || return 1
  fi
  host_common_args "$RUNTIME" "$HOST_CAP_VALUE"
  host_call enqueue "${HOST_ARGS[@]}" --argv-file "$snap.json" --logdir "$DISPATCH_DIR" --why "$HOST_FULL_NOTE" --not-before "${HOST_NOT_BEFORE:-0}" || return 1
  [[ "$HOST_VERDICT" == queued ]] || return 1
  HOST_TICKET_ID="$HOST_IDENT"
  HOST_QUEUE_POSITION="$HOST_NOTE"
  RETURN_RESERVATION_DURABLE=1
}

host_queue_routes() { printf '%s\n' "$RUNTIME"; }

queue_refusal() {
  HOST_FULL_NOTE="$1"
  HOST_NOT_BEFORE=$(( $(date +%s) + 1800 ))
  HOST_REFUSAL_ARG=(--refusal)
  if host_enqueue; then
    echo "[dispatch] queued #$HOST_QUEUE_POSITION (ticket ${TICKET:-$HOST_TICKET_ID}, request $HOST_TICKET_ID): $HOST_FULL_NOTE; retry after prerequisite re-check."
    record_runtime_decision queued host_queue "$HOST_FULL_NOTE; request=$HOST_TICKET_ID" || true
    exit 0
  fi
  echo "[dispatch] REFUSED: request not accepted by host queue (${HOST_NOTE:-$HOST_FULL_NOTE}) Accepted path: restore host queue availability, then resubmit the same request." >&2
  record_runtime_decision blocked host_queue_unavailable "$HOST_FULL_NOTE; ${HOST_NOTE:-unavailable}" || true
  exit 2
}


do_host_cancel() {
  host_dispatch_on || exit 2
  host_call cancel --agent "$(host_agent_id)" --ticket "$HOST_CANCEL_TICKET" || exit 2
  echo "[dispatch] ticket $HOST_CANCEL_TICKET: $HOST_VERDICT${HOST_NOTE:+ -- $HOST_NOTE}"
  [[ "$HOST_VERDICT" == cancelled ]] || exit 2
}

active_count_incr() {
  local runtime="${1:-$RUNTIME}"
  count_lock || return 1
  local v cap f
  v="$(read_active_count "$runtime")"
  local quota_active="$v"
  compute_dynamic_cap "$runtime"   
  cap="$DYNAMIC_CAP_VALUE"
  MAX_ACTIVE="$cap"
  HOST_FULL_NOTE=""

  if ! host_dispatch_on && [[ $quota_active -ge $cap && -z "$HOST_ADOPT_LEASE" && "$HOST_FORCE" != 1 ]]; then
    count_unlock
    return 1
  fi

  if [[ -z "$HOST_ADOPT_LEASE" && "$HOST_FORCE" != 1 ]] \
     && ! host_dispatch_on && [[ -n "$MACHINE_CAP" ]] \
     && (( $(local_active_count) >= MACHINE_CAP )); then
    DYNAMIC_CAP_NOTE+="; shared machine full: $(local_active_count)/$MACHINE_CAP"
    count_unlock
    return 1
  fi

  if host_dispatch_on; then
    count_unlock
    
    host_acquire "$runtime" "$HOST_CAP_VALUE" || return 1
    count_lock || { host_release; return 1; }
    v="$(read_active_count "$runtime")"
  fi
  f="$(active_count_file "$runtime")"
  printf '%s\n' "$(( v + 1 ))" > "$f"
  count_unlock
  return 0
}

active_count_decr() {
  local runtime="${1:-$RUNTIME}"
  host_release
  count_lock || return 0   
  local v f
  f="$(active_count_file "$runtime")"
  v="$(read_active_count "$runtime")"
  if [[ $v -gt 0 ]]; then
    printf '%s\n' "$(( v - 1 ))" > "$f"
  else
    printf '0\n' > "$f"
  fi
  count_unlock
}

fable_exhausted() {
  expiry_flag_active "$FABLE_FLAG"
}

expiry_flag_active() {
  [[ -f "$1" ]] || return 1
  python3 - "$1" <<'PY'
import json, os, sys, time
from datetime import datetime, timezone
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        flag = json.load(f)
    expiry = flag.get("expiry", "") if isinstance(flag, dict) else ""
    exp = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    sys.exit(0 if exp > datetime.now(timezone.utc) else 1)
except Exception:
    # Corrupt flag: demote (safe) unless the file has aged past one 7d window.
    try:
        sys.exit(0 if (time.time() - os.path.getmtime(path)) <= 7 * 86400 else 1)
    except Exception:
        sys.exit(0)
PY
}

RE_FABLE_CREDITS_DEATH='[Rr]equires usage credits|[Hh]it your (usage )?limit|usage limit reached|weekly limit reached|[Rr]eached your .*limit'

is_fable_credits_death() {
  [[ "$RUNTIME" == "claude-code" ]] || return 1
  [[ "$MODEL" == "fable" ]] || return 1
  [[ -f "$1" ]] || return 1
  grep -qE "$RE_FABLE_CREDITS_DEATH" "$1"
}

OVERLOAD_FALLBACK_ORIGINS="opus fable"
OVERLOAD_FALLBACK_CHAIN="fable: sonnet:max"
MODEL_FALLBACK_FROM=""
MODEL_FALLBACK_NOTE=""

stream_tool_use_seen() {
  python3 - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    fh = open(sys.argv[1], errors="replace")
except Exception:
    sys.exit(1)
for line in fh:
    try:
        doc = json.loads(line)
    except Exception:
        continue
    if not isinstance(doc, dict) or doc.get("type") != "assistant":
        continue
    msg = doc.get("message")
    if not isinstance(msg, dict):
        continue
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") in ("tool_use", "server_tool_use"):
            sys.exit(0)
sys.exit(1)
PY
}

overload_death_kind() {
  stream_tool_use_seen "$1" && return 0
  python3 - "$1" <<'PY' 2>/dev/null || true
import json, re, sys
pat = re.compile(r"\b529\b|overloaded", re.I)
res, last_text = None, ""
try:
    fh = open(sys.argv[1], errors="replace")
except Exception:
    sys.exit(0)
for line in fh:
    try:
        doc = json.loads(line)
    except Exception:
        continue
    if not isinstance(doc, dict):
        continue
    if doc.get("type") == "result":
        res = doc
    elif doc.get("type") == "assistant" and isinstance(doc.get("message"), dict):
        for block in doc["message"].get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                last_text = block.get("text") or ""
if res is not None:
    if res.get("is_error") and (res.get("api_error_status") == 529
                                or pat.search(str(res.get("result") or ""))):
        print("overloaded")
elif re.search(r"API Error: 529", last_text):
    print("overloaded")
PY
  return 0
}

overload_fallback_reason() {
  [[ "$RUNTIME" == "claude-code" || "$RUNTIME" == "claude-api" ]] || return 1
  if [[ -z "$MODEL_FALLBACK_FROM" && " $OVERLOAD_FALLBACK_ORIGINS " != *" $MODEL "* ]]; then
    return 1
  fi
  if [[ "$(overload_death_kind "$RUNDIR/stream.jsonl")" == overloaded ]]; then
    echo "529-overloaded"
    return 0
  fi
  if [[ -n "$MODEL_FALLBACK_FROM" ]] && ! stream_tool_use_seen "$RUNDIR/stream.jsonl"; then
    render_out_log
    if is_fable_credits_death "$RUNDIR/out.log"; then
      write_fable_exhausted_marker || echo "[dispatch-worker] failed to write $FABLE_FLAG (non-fatal)" >&2
      echo "fable-weekly-cap"
      return 0
    fi
  fi
  return 1
}

overload_fallback_next() {
  local entry m e
  for entry in $OVERLOAD_FALLBACK_CHAIN; do
    m="${entry%%:*}" e="${entry#*:}"
    [[ " $1 " == *" $m "* ]] && continue
    if [[ "$m" == fable ]] && fable_exhausted; then
      continue
    fi
    printf '%s %s\n' "$m" "$e"
    return 0
  done
  return 1
}

overload_fallback_record() {
  local n="$1" reason="$2" next="$3" effort="$4" from="$MODEL" hop binding f
  
  if [[ -n "$next" ]]; then
    for f in stream.jsonl claude.err out.log; do
      [[ -f "$RUNDIR/$f" ]] && mv -f "$RUNDIR/$f" "$RUNDIR/${f%%.*}.attempt$n-$from.${f#*.}"
    done
  fi
  [[ -n "$MODEL_FALLBACK_FROM" ]] || MODEL_FALLBACK_FROM="$from"
  if [[ -z "$next" ]]; then
    hop="$from $reason, 0 tool use -> end of fallback chain"
  else
    hop="$from $reason, 0 tool use -> $next${effort:+ effort=$effort}"
    MODEL="$next"
    if [[ -n "$effort" ]]; then
      EFFORT="$effort"
      EFFORT_SOURCE="fallback"
    fi
  fi
  MODEL_FALLBACK_NOTE="${MODEL_FALLBACK_NOTE:+$MODEL_FALLBACK_NOTE; }$hop"
  echo "[dispatch-worker] DGN-1898 model fallback: $hop"
  {
    run_env_write MODEL "$MODEL"
    run_env_write EFFORT "$EFFORT"
    run_env_write EFFORT_SOURCE "$EFFORT_SOURCE"
    run_env_write MODEL_FALLBACK_FROM "$MODEL_FALLBACK_FROM"
    run_env_write MODEL_FALLBACK_NOTE "$MODEL_FALLBACK_NOTE"
  } >> "$RUNDIR/run.env"
  [[ -n "$next" ]] || return 0
  
  "$LEDGER_UPDATE" "$LEDGER_BRANCH" running \
    "dispatch model fallback (model=$MODEL runtime=$RUNTIME fallback_from=$MODEL_FALLBACK_FROM reason=$reason)" \
    --ts "$(date '+%Y-%m-%d %H:%M %Z')" \
    || echo "[dispatch-worker] ledger fallback note failed (status.json/run.env still carry it)" >&2
}

session_transcript_exists() {
  local wd="$1" sid="$2" real=""
  [[ -f "$HOME/.claude/projects/$(sb_project_slug "$wd")/$sid.jsonl" ]] && return 0
  [[ -d "$wd" ]] && real="$(cd "$wd" && pwd -P)"
  [[ -n "$real" && -f "$HOME/.claude/projects/$(sb_project_slug "$real")/$sid.jsonl" ]]
}

write_fable_exhausted_marker() {
  mkdir -p "$(dirname "$FABLE_FLAG")" 2>/dev/null || return 1
  python3 - "$FABLE_FLAG" "$WORKSPACE" <<'PY'
import json, os, sys
from datetime import datetime, timedelta, timezone

flag_path, workspace = sys.argv[1], sys.argv[2]
cache = os.path.join(workspace, ".claude", ".usage-cache.json")
expiry = None
try:
    with open(cache, encoding="utf-8") as f:
        resets = json.load(f).get("seven_day_resets_at")
    if resets:
        datetime.fromisoformat(str(resets).replace("Z", "+00:00"))
        expiry = str(resets)
except Exception:
    expiry = None
if not expiry:
    expiry = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

tmp = flag_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({
        "expiry": expiry,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": "dispatch-detached-worker",
    }, f)
os.replace(tmp, flag_path)
PY
}

ensure_ledger() {
  if [[ -f "$LEDGER_FILE" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$LEDGER_FILE")"
  ( set -o noclobber
    printf '%s\n' \
      "# auto-loop-ledger.md" \
      "# Current-state ledger -- one row per background work item (branch)." \
      "# Single write path: routines/lib/ledger-update.sh. Never edit rows by hand." \
      "# Seeded by dispatch-detached.sh on first use (DGN-991 backland)." \
      "" \
      "| branch | item | state | attempts | backoff_until | last_ts | flags | note |" \
      "|---|---|---|---|---|---|---|---|" > "$LEDGER_FILE"
  ) 2>/dev/null || true
  if [[ ! -f "$LEDGER_FILE" ]]; then
    echo "[dispatch] ledger bootstrap failed: cannot create $LEDGER_FILE Accepted path: restore ledger access at $LEDGER_FILE, then retry the original request before launching a worker." >&2
    return 1
  fi
  return 0
}

run_gates() {
  if [[ "$RUNTIME" == claude-code ]]; then worker_claude_pace_gate || return 2; fi

  if [[ "$RUNTIME" == "codex" ]]; then
    local qrc=0 swrc=0
    codex_quota_gate || qrc=$?
    if [[ $qrc -ne 0 ]]; then
      codex_to_claude_switch "$CODEX_QUOTA_NOTE" || swrc=$?
      if [[ $swrc -eq 3 ]]; then
        defer_or_block "$CLAUDE_GATE_RC" "$CLAUDE_GATE_NOTE"
        return 2
      elif [[ $swrc -ne 0 ]]; then
        record_runtime_decision blocked "${FALLBACK_BLOCK_REASON:-codex_quota_refused}" "$CODEX_QUOTA_NOTE" || true
        return 2
      fi
    fi
  fi

  if [[ "$RUNTIME" == "claude-code" && "$RUNTIME_FALLBACK_DIRECTION" != "codex->claude-code" ]]; then

    local gate_out gate_rc=0
    gate_out="$("$USAGE_GATE" 2>&1)" || gate_rc=$?
    CLAUDE_GATE_RC="$gate_rc"
    CLAUDE_GATE_NOTE="$(printf '%s\n' "$gate_out" | tail -n 1)"
    if [[ $gate_rc -ne 0 ]]; then
      echo "[dispatch] refused by usage-gate (rc=$gate_rc):" >&2
      echo "$gate_out" >&2
      local fbrc=2
      if [[ $gate_rc -eq 1 ]]; then
        fbrc=0
        claude_to_codex_fallback "claude usage-gate refused (rc=$gate_rc): $CLAUDE_GATE_NOTE" || fbrc=$?
      fi
      if [[ $fbrc -ne 0 ]]; then
        defer_or_block "$gate_rc" "$gate_out"
        return 2
      fi
    fi

    if [[ "$RUNTIME" == "claude-code" && "$MODEL" == "fable" ]] && fable_exhausted; then
      echo "[dispatch] fable weekly cap exhausted (.claude/.fable-exhausted) -> demoting to opus for this dispatch"
      MODEL="opus"
    fi
  fi

  if ! active_count_incr; then
    {

      if [[ -n "$HOST_FULL_NOTE" ]]; then
        echo "[dispatch] dispatch refused for runtime=$RUNTIME (own $(read_active_count)/$MAX_ACTIVE; $HOST_FULL_NOTE) -- trying the next runtime with room."
      else
        echo "[dispatch] active dispatch cap reached for runtime=$RUNTIME ($(read_active_count)/$MAX_ACTIVE) -- trying the next runtime with room."
      fi
      echo "[dispatch]   cap basis: $DYNAMIC_CAP_NOTE"
    } >&2
    SLOT_WALK_NOTE="$RUNTIME at cap $(read_active_count)/$MAX_ACTIVE ($(cap_note_brief "$DYNAMIC_CAP_NOTE"))"

    [[ -n "$HOST_FULL_NOTE" ]] || HOST_FULL_NOTE="own cap $(read_active_count)/$MAX_ACTIVE"
    if host_enqueue; then
      if [[ "$RUNTIME_PREFERRED_WAIT" == 1 ]]; then
        echo "[dispatch] queued #$HOST_QUEUE_POSITION (ticket $HOST_TICKET_ID): $(owner_model_display_bare) 자리 대기 중; starts automatically when its preferred slot frees."
        record_runtime_decision queued preferred_runtime_wait "ticket=$HOST_TICKET_ID runtime=$RUNTIME $DYNAMIC_CAP_NOTE; $SLOT_WALK_NOTE" || true
      else
        echo "[dispatch] queued #$HOST_QUEUE_POSITION (ticket $HOST_TICKET_ID): starts automatically when a slot frees."
        record_runtime_decision queued host_queue "ticket=$HOST_TICKET_ID runtime=$RUNTIME $DYNAMIC_CAP_NOTE; $SLOT_WALK_NOTE" || true
      fi
      return 3
    fi

    if ! host_dispatch_on; then
      echo "[dispatch] refused: active dispatch cap reached for runtime=$RUNTIME and no runtime has room -- $SLOT_WALK_NOTE" >&2
      defer_or_block slot "$SLOT_WALK_NOTE"
      return 2
    fi
    echo "[dispatch] host queue unavailable: request was not accepted (${HOST_NOTE:-enqueue not attempted}) Accepted path: restore host queue availability, then resubmit the same request." >&2
    record_runtime_decision blocked host_queue_unavailable "${HOST_NOTE:-enqueue not attempted}; $SLOT_WALK_NOTE" || true
    return 2
  fi

  if [[ -n "${DOGANY_DISPATCH_DEFER_ORIGIN:-}" && -z "$RUNTIME_FALLBACK_DIRECTION" ]]; then
    record_runtime_decision selected defer_replay_cleared "attempt=$DISPATCH_DEFER_ATTEMPT origin=${DOGANY_DISPATCH_DEFER_ORIGIN}" || true
  fi
  return 0
}

attach_inline() {
  [[ ${#ATTACH_FILES[@]} -gt 0 ]] || return 0
  local f b n all cap overhead merged
  for f in "${ATTACH_FILES[@]}"; do
    if [[ ! -f "$f" || ! -r "$f" ]]; then
      echo "[dispatch] --attach: not a readable regular file: $f" >&2
      return 1
    fi
    n="$(wc -c < "$f" | tr -d ' ')"
    if [[ "$(LC_ALL=C tr -d '\000' < "$f" | wc -c | tr -d ' ')" != "$n" ]]; then
      echo "[dispatch] --attach: $(basename "$f") is binary (NUL bytes) -- only text is inlined into a brief" >&2
      return 1
    fi
  done
  merged="$(mktemp "${TMPDIR:-/tmp}/dsp-attach.XXXXXX")" || {
    echo "[dispatch] --attach: could not create the merged brief" >&2; return 1; }
  ATTACH_TMP="$merged"
  ATTACH_BRIEF_FILE="$TASK_FILE"
  trap 'return_route_release_unadmitted; rm -f "$ATTACH_TMP"' EXIT
  cat "$TASK_FILE" > "$merged"
  ATTACH_NOTE=""
  for f in "${ATTACH_FILES[@]}"; do
    b="$(basename "$f")"; n="$(wc -c < "$f" | tr -d ' ')"
    {
      printf '\n\n--- attached file: %s (%s bytes; inlined at dispatch -- the original path is not readable from the worker) ---\n' "$b" "$n"
      cat "$f"
      printf '\n--- end attached file: %s ---\n' "$b"
    } >> "$merged"
    ATTACH_NOTE+="${ATTACH_NOTE:+, }$b ${n}B"
  done
  cap="$PROMPT_MAX_BYTES"
  all="$(wc -c < "$merged" | tr -d ' ')"
  if (( all > cap )); then
    echo "[dispatch] refused: --attach brief + attachments = ${all}B > ${cap}B -- attach only the sections the worker needs" >&2
    return 1
  fi
  TASK_FILE="$merged"
  echo "[dispatch] attach: inlined ${#ATTACH_FILES[@]} file(s) into the brief ($ATTACH_NOTE; total ${all}B of ${cap}B prompt cap)"
  return 0
}

design_prompt_rules() {
  [[ "$DISPATCH_KIND" == design ]] || return 0
  resolve_owner_lang
  printf '\n[Design task rules: dispatching instance]\n'
  cat "$WORKSPACE/.claude/agents/dogany-designer.md"
  printf '\nImage text language: AGENT_LANG=%s. Use this language for all text inside images, including labels.\n' "$OWNER_LANG"
}

render_dispatch_prompt() { cat "$TASK_FILE"; design_prompt_rules; }

check_prompt_size() {
  local bytes
  bytes="$(wc -c < "$TASK_FILE" | tr -d ' ')"
  if [[ "$DISPATCH_KIND" == design ]]; then
    bytes=$(( bytes + $(design_prompt_rules | wc -c) ))
  fi

  if [[ $bytes -gt $PROMPT_MAX_BYTES ]]; then
    echo "[dispatch] refused: prompt file is ${bytes}B > ${PROMPT_MAX_BYTES}B (ARG_MAX guard)." >&2
    echo "[dispatch] hint: move bulk material into files and reference their paths from a short prompt." >&2
    return 1
  fi
  return 0
}

effort_passed_to_runtime() {
  case "$1:$2" in
    claude-code:low|claude-code:medium|claude-code:high|claude-code:max) return 0 ;;
    claude-api:low|claude-api:medium|claude-api:high|claude-api:max) return 0 ;;
    codex:low|codex:medium|codex:high) return 0 ;;
  esac
  return 1
}

RE_FABLE_USAGE_CLAIM='[Ff]able.*(exhaust|limit|quota|[0-9]+%|소진|한도|초과)|(exhaust|limit|quota|[0-9]+%|소진|한도|초과).*[Ff]able'

OWNER_LANG="en"

resolve_owner_lang() {
  local lib="$SCRIPT_DIR/lib/agentlib.sh" out=""
  if [[ -f "$lib" ]]; then

    out="$( ( set +e; AGENT_ROOT="$WORKSPACE"; . "$lib" >/dev/null 2>&1
              printf '%s' "${AGENT_LANG:-}" ) 2>/dev/null )" || out=""
  fi
  out="$(printf '%s' "$out" | tr 'A-Z' 'a-z' | tr -d '[:space:]')"
  OWNER_LANG="${out:-en}"
}

owner_lang_is_english() {
  case "${OWNER_LANG%%[-_.]*}" in
    en) return 0 ;;
    *)  return 1 ;;
  esac
}

owner_facing_name() {
  local name
  if [[ $# -gt 0 ]]; then
    name="$1"
  elif ! owner_lang_is_english && [[ -n "$DISPLAY_NAME" ]]; then
    name="$DISPLAY_NAME"
  else
    name="$LABEL"
  fi
  if [[ -n "${TICKET:-}" && "$name" != *"$TICKET"* ]]; then
    printf '[%s] %s' "$TICKET" "$name"
  else
    printf '%s' "$name"
  fi
}

# Owner-approved session-job stop copy (2026-09-24), retained by DGN-2072.
draft_result_text() {
  printf '⚠️ %s 작업이 멈췄습니다.' "$2"
}

legacy_end_notice_text() {
  local result="$1" name="$2" clock="$3" elapsed="${4:-}" end_line
  case "$result" in
    completed)
      local end_kind=subagent end_model="" end_elapsed=""
      if [[ -n "$MODEL_SLOT" || "${RUNTIME:-}" == script ]]; then
        end_kind=script
      else
        end_model="$(owner_model_display_bare "${MODEL_OBSERVED_JSON:-}")"
      fi
      if [[ -n "$elapsed" ]]; then
        end_elapsed="$(format_elapsed_ko "$elapsed")" || end_elapsed=""
      fi
      end_line="$(run_block "$end_kind" "$end_model" "${RUNTIME:-}" "$name" "$clock" "$end_elapsed")"

      # owner-approved 2026-09-28 07:08: every END notice titles with background_term (DGN-1769)
      printf '%s\n%s' "✅ $(background_term) 작업 완료" "$end_line" ;;
    timed-out|canceled)
      draft_result_text "$result" "$name" ;;
    replaced)
      local tpl
      tpl="$(reset_pass_i18n dispatch.replaced_credit)"
      [[ -n "$tpl" ]] || tpl="{name}: the API credit ran out, so I moved the work over and it keeps going."
      printf '%s' "${tpl//\{name\}/$name}" ;;
    *)
      
      # owner-approved 2026-09-11: job-failed line, owner-authored verbatim
      printf '⚠️ 작업실패: %s - 원인 확인하고 다시 알려드릴게요.' "$name" ;;
  esac

}

notice_i18n() {
  local out="" catalog_root=""
  out="$(reset_pass_i18n "$1")"
  if [[ -z "$out" && -r "$SCRIPT_DIR/lib/i18n_lookup.py" ]]; then
    catalog_root="$(cd -P "$SCRIPT_DIR" && cd .. && pwd)"
    resolve_owner_lang
    out="$(python3 -B "$SCRIPT_DIR/lib/i18n_lookup.py" "$catalog_root" "$OWNER_LANG" "$1" 2>/dev/null)" || out=""
  fi

  if [[ -z "$out" ]]; then
    case "$1" in
      dispatch.notice.background) out=Background ;;
      dispatch.notice.started) out='▶️ {background} job started' ;;
      dispatch.notice.completed) out='✅ {background} job completed' ;;
      dispatch.notice.stopped) out='⚠️ {background} job stopped' ;;
      dispatch.notice.failed) out='⚠️ {background} job failed' ;;
      dispatch.notice.row) out='• {model}: {name} {clock}' ;;
      dispatch.notice.batch) out='{title} ({count} jobs)' ;;
      dispatch.notice.elapsed) out='{row} ({duration})' ;;
      dispatch.notice.duration.less_minute) out='<1 min' ;;
      dispatch.notice.duration.minutes) out='{minutes} min' ;;
      dispatch.notice.duration.hours) out='{hours} h' ;;
      dispatch.notice.duration.hours_minutes) out='{hours} h {minutes} min' ;;
    esac
  fi
  printf '%s' "$out"
}

notice_title() {
  local tpl
  tpl="$(notice_i18n "dispatch.notice.$1")"
  printf '%s' "${tpl//\{background\}/$(background_term)}"
}

notice_row() {
  local tpl duration="" secs="${4:-}" minutes hours key
  tpl="$(notice_i18n dispatch.notice.row)"

  shopt -u patsub_replacement 2>/dev/null || true
  local notice_model="$1" notice_name="$2"
  tpl="${tpl//\{model\}/$notice_model}"
  tpl="${tpl//\{clock\}/$3}"
  case "$secs" in ''|*[!0-9]*) printf '%s' "${tpl//\{name\}/$notice_name}"; return ;; esac
  minutes=$(( secs / 60 ))
  hours=$(( minutes / 60 ))
  if [[ "$secs" -lt 60 ]]; then key=less_minute
  elif [[ "$hours" -eq 0 ]]; then key=minutes
  elif [[ $((minutes % 60)) -eq 0 ]]; then key=hours
  else key=hours_minutes; minutes=$(( minutes % 60 )); fi
  duration="$(notice_i18n "dispatch.notice.duration.$key")"
  duration="${duration//\{hours\}/$hours}"
  duration="${duration//\{minutes\}/$minutes}"
  local elapsed_tpl
  elapsed_tpl="$(notice_i18n dispatch.notice.elapsed)"
  elapsed_tpl="${elapsed_tpl//\{row\}/$tpl}"
  elapsed_tpl="${elapsed_tpl//\{duration\}/$duration}"
  printf '%s' "${elapsed_tpl//\{name\}/$notice_name}"
}

end_notice_text() {
  local state
  if [[ -n "$MODEL_SLOT" || "${RUNTIME:-}" == script || "$1" == replaced ]]; then
    legacy_end_notice_text "$@"
    return
  fi
  case "$1" in
    completed) state=completed ;;
    timed-out|canceled) state=stopped ;;
    *) state=failed ;;
  esac
  printf '%s\n%s' "$(notice_title "$state")" \
    "$(notice_row "$(owner_model_display_cap)" "$2" "$3" "${4:-}")"

}

# The <elapsed> slot of the completion block. The BLOCK shape is
# owner-approved (identity Response workflow, "Completion signal"); the
# design names the slot but not the unit rendering, so the unit words below
# are DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (dec-094
# UX gate): "<1 min" under a minute, "N min" under an hour, "N h" on the
# hour, "N h M min" otherwise (rendered in the owner's language by the printf
# literals below). Minutes truncate rather than round: a 59:30 run reported
# as "1 h" would claim a boundary it never crossed.
# $1 = elapsed whole seconds.
format_elapsed_ko() {
  local secs="$1" mins hours rem
  case "$secs" in ''|*[!0-9]*) return 1 ;; esac
  if [[ "$secs" -lt 60 ]]; then
    printf '1분 미만'
    return 0
  fi
  mins=$(( secs / 60 ))
  hours=$(( mins / 60 ))
  rem=$(( mins % 60 ))
  if [[ "$hours" -eq 0 ]]; then
    printf '%d분' "$mins"
  elif [[ "$rem" -eq 0 ]]; then
    printf '%d시간' "$hours"
  else
    printf '%d시간 %d분' "$hours" "$rem"
  fi
}

elapsed_seconds_since_start() {
  [[ -n "${STARTED_AT:-}" ]] || return 1
  python3 -c '
import datetime, sys
try:
    s = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%S%z")
except Exception:
    sys.exit(1)
d = datetime.datetime.now(datetime.timezone.utc) - s
print(max(0, int(d.total_seconds())))
' "$STARTED_AT" 2>/dev/null
}

validate_access_mode() {
  [[ "$READ_ONLY" == 1 ]] || { echo '[dispatch] --read-only is required for model jobs' >&2; return 1; }
  ACCESS_MODE=read
}

validate_live_targets() {
  local t real out=""
  LIVE_TARGETS=""
  [[ ${#LIVE_TARGETS_ARG[@]} -gt 0 ]] || return 0
  if [[ "$READ_ONLY" != 1 ]]; then
    echo "[dispatch] --live-target requires --read-only (the worker plans; --apply-plan applies)." >&2
    return 1
  fi
  for t in "${LIVE_TARGETS_ARG[@]}"; do
    if [[ "$t" != /* || ! -e "$t" ]]; then
      echo "[dispatch] --live-target must be an existing absolute path: $t" >&2
      return 1
    fi
    if [[ -d "$t" ]]; then
      real="$(cd "$t" && pwd -P)"
    else
      real="$(cd "$(dirname "$t")" && pwd -P)/$(basename "$t")"
    fi
    if ! sb_path_ok "$real"; then
      echo "[dispatch] --live-target path cannot be quoted safely: $t Accepted path: use an absolute path without double quotes, backslashes, newline or carriage return and a UUID session identifier, then retry the original request." >&2
      return 1
    fi
    out="${out:+$out$'\n'}$real"
  done
  LIVE_TARGETS="$out"
  return 0
}

validate_display_name() {

  resolve_owner_lang

  local display_trimmed="${DISPLAY_NAME//[[:space:]]/}"

  if [[ -n "$DISPLAY_NAME" && -z "$display_trimmed" ]]; then
    echo "[dispatch] --display-name is blank (whitespace only): it would render as an empty name on the owner's work board. Accepted path: supply a nonblank human-readable --display-name." >&2
    return 1
  fi
  if ! owner_lang_is_english && [[ -z "$display_trimmed" ]]; then
    {
      echo "[dispatch] --display-name is required: this instance's language is '$OWNER_LANG', so the machine label is never rendered to the owner."
      echo "[dispatch]   --label        = machine label (ledger/run/log key): ASCII, lowercase, hyphenated"
      echo "[dispatch]   --display-name = the same job named for a human, in the instance's language"
      echo "[dispatch] supply both, e.g.: --label ${LABEL:-<label>} --display-name \"<owner-readable name>\""
      echo "[dispatch] it is not optional on purpose: an optional field is the one skipped under load, and the owner surfaces then fall back to the machine label without saying so."
    } >&2
    return 1
  fi

  case "$DISPLAY_NAME" in
    *'|'*|*$'\n'*|*$'\r'*)
      echo "[dispatch] invalid --display-name: no '|', newline or carriage return (the ledger row is pipe-delimited)" >&2
      return 1 ;;
  esac
  return 0
}

validate_spawn_args() {
  [[ -n "$TASK_FILE" && -r "$TASK_FILE" && -f "$TASK_FILE" && -n "$LABEL" ]] || {
    echo "[dispatch] --task <readable file> and --label are required" >&2; return 1;
  }
  case "$REQUESTED_RUNTIME" in auto|claude-code|codex) ;; *)
    echo "[dispatch] this runtime requires an explicitly configured DOGANY_JOB_DRIVER" >&2; return 1 ;; esac
  if [[ -n "$JOB_WORKDIR" ]]; then
    [[ -d "$JOB_WORKDIR" ]] || { echo "[dispatch] work directory does not exist" >&2; return 1; }
    JOB_WORKDIR="$(cd "$JOB_WORKDIR" && pwd -P)"
  fi
  [[ "$TIMEOUT_MIN" =~ ^[0-9]+$ && "$TIMEOUT_MIN" -ge 1 ]] || return 1
  case "$EFFORT" in ""|low|medium|high|max) ;; *) return 1 ;; esac
  case "$NOTIFY" in push|silent) ;; *) return 1 ;; esac
  case "$LABEL" in *'|'*|*$'\n'*|*$'\r'*|*/*) echo "[dispatch] invalid job label" >&2; return 1 ;; esac
  validate_display_name || return 1
  return 0
}

run_env_write() {

  local key="$1" value="$2"

  local LC_ALL=C
  printf '%s=' "$key"
  printf '%q' "$value"
  printf '\n'
}

observe_sandbox_available() {
  hash -r   
  if ! command -v sandbox-exec >/dev/null 2>&1; then
    echo "[dispatch] observe mode needs sandbox-exec (macOS seatbelt) and it is not available -- REFUSING to spawn (no allowlist fallback: that fallback is the DGN-1191 write hole). Accepted path: use a supported macOS dispatch host with sandbox-exec available." >&2
    return 1
  fi
  return 0
}

sb_path_ok() {
  case "$1" in
    *'"'*|*'\'*|*$'\n'*|*$'\r'*) return 1 ;;
  esac
  [[ "$1" == /* ]]
}

sb_subpaths() {
  local p="$1" r=""
  sb_path_ok "$p" || return 1
  printf '(subpath "%s")' "$p"
  if [[ -d "$p" ]]; then
    r="$(cd "$p" && pwd -P)"
    [[ "$r" != "$p" ]] && printf ' (subpath "%s")' "$r"
  elif [[ "$p" == /tmp/* || "$p" == /tmp ]]; then
    printf ' (subpath "/private%s")' "$p"
  fi
  return 0
}

sb_regex_escape() {
  printf '%s' "$1" | sed 's/[][\.*^$()|+?{}\\]/\\&/g'
}

sb_project_slug() {
  printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'
}

sb_signal_rules() {
  echo ';; DGN-1937: signals -- the worker may signal only its own process tree (same sandbox)'
  echo '(deny signal)'
  echo '(allow signal (target same-sandbox))'
}

signal_guard_selfcheck() {
  local prof="$1" label="$2" outside rc
  if ! sandbox-exec -f "$prof" /bin/sh -c 'sleep 30 & p=$!; kill "$p" || exit 1; wait "$p"; [ $? -ne 0 ]' 2>/dev/null; then
    echo "[dispatch] $label profile self-check FAILED: a sandboxed shell cannot kill its own child (DGN-1937 signal guard too tight) -- refusing to spawn Accepted path: repair the sandbox renderer and rerun its profile self-check before retrying the original request." >&2
    return 1
  fi
  sleep 30 &
  outside=$!
  rc=0
  sandbox-exec -f "$prof" /bin/kill -CONT "$outside" 2>/dev/null || rc=$?
  kill "$outside" 2>/dev/null || true
  wait "$outside" 2>/dev/null || true
  if [[ "$rc" -eq 0 ]]; then
    echo "[dispatch] $label profile self-check FAILED: sandbox allowed a signal to a process outside its own tree (DGN-1937) -- refusing to spawn Accepted path: repair the sandbox renderer and rerun its profile self-check before retrying the original request." >&2
    return 1
  fi
  return 0
}

render_observe_profile() {
  local out="$1" workdir="$2" sid="$3"
  local home="$HOME" home_re cc slug slugs="" wd_real="" ws_real=""
  if ! sb_path_ok "$home" || ! sb_path_ok "$workdir" || ! sb_path_ok "$WORKSPACE" \
     || ! sb_path_ok "$DISPATCH_ISOLATION_ROOT" || ! [[ "$sid" =~ ^[0-9a-f-]{36}$ ]] \
     || { [[ -n "$PLAN_DIR" ]] && ! sb_path_ok "$PLAN_DIR"; }; then
    echo "[dispatch] observe profile: unquotable path or bad session id -- refusing Accepted path: use an absolute path without double quotes, backslashes, newline or carriage return and a UUID session identifier, then retry the original request." >&2
    return 1
  fi
  home_re="$(sb_regex_escape "$home")"
  cc="$home/.claude"

  local cred_helper=""
  cred_helper="$(git --exec-path 2>/dev/null)/git-credential-osxkeychain"
  { [[ -x "$cred_helper" ]] && sb_path_ok "$cred_helper"; } || cred_helper=""

  slug="$(sb_project_slug "$workdir")"
  slugs="(literal \"$cc/projects/$slug\") (regex #\"^$home_re/\\.claude/projects/$slug/$sid(\\.|/|\$)\")"
  if [[ -d "$workdir" ]]; then
    wd_real="$(cd "$workdir" && pwd -P)"
    if [[ "$wd_real" != "$workdir" ]]; then
      slug="$(sb_project_slug "$wd_real")"
      slugs="$slugs (literal \"$cc/projects/$slug\") (regex #\"^$home_re/\\.claude/projects/$slug/$sid(\\.|/|\$)\")"
    fi
  fi
  {
    echo '(version 1)'
    echo ";; DGN-1191 observe mode -- rendered by dispatch-detached.sh; rule ORDER is load-bearing (last match wins)."
    echo '(allow default)'
    echo ';; 1. deny every filesystem write ...'
    echo '(deny file-write*)'
    echo ';; 2. ... then re-open only what the worker process itself needs'
    echo '(allow file-write* (subpath "/dev"))'
    echo '(allow file-write* (subpath "/private/var/folders"))   ; TMPDIR (mktemp of dry-run scripts)'
    echo '(allow file-write* (subpath "/private/tmp"))           ; /tmp scratch + CLI cwd tracker'
    echo "(allow file-write* (subpath \"$cc\"))                  ; CLI session state"
    if [[ "$RUNTIME" == "codex" ]]; then

      echo "(allow file-write* $(sb_subpaths "$home/.codex")) ; Codex session state"
    fi
    echo ';; 3. inside ~/.claude: configuration, hooks, plugins and skills stay closed'
    echo "(deny file-write* (literal \"$cc/settings.json\") (literal \"$cc/settings.local.json\") (literal \"$cc/CLAUDE.md\") (literal \"$cc/keybindings.json\"))"
    echo "(deny file-write* (subpath \"$cc/plugins\") (subpath \"$cc/skills\") (subpath \"$cc/agents\") (subpath \"$cc/commands\") (subpath \"$cc/hooks\") (subpath \"$cc/scripts\") (subpath \"$cc/rules\"))"
    echo ';;    DGN-1050: the CLI writes ~/.claude/.credentials.json ONLY when a keychain write fails'
    echo ';;    non-transiently -- exactly what rule 1 causes -- and then DELETES the keychain entry.'
    echo ';;    Denying the file (regex: the CLI stages via .credentials.json.tmp.<hex>) makes that'
    echo ';;    fallback fail cleanly: audited CLI behavior is retry x3 -> save_failed, no crash, no'
    echo ';;    keychain delete, process continues on the old token. Reads stay open.'
    echo "(deny file-write* (regex #\"^$home_re/\\.claude/\\.credentials\\.json\"))"
    echo "(deny file-write* (regex #\"^$home_re/\\.claude\\.json\"))   ; global config incl. permission cache"
    echo ';; 4. state that OTHER live sessions source into their unsandboxed shells: shell snapshots'
    echo ';;    (denied outright -- the CLI runs without one), per-session env, transcripts (own session only)'
    echo "(deny file-write* (subpath \"$cc/shell-snapshots\"))"
    echo "(deny file-write* (subpath \"$cc/session-env\"))"
    echo "(allow file-write* (subpath \"$cc/session-env/$sid\"))"
    echo "(deny file-write* (subpath \"$cc/projects\"))"
    echo "(allow file-write* $slugs)"
    echo ';; 5. push credentials cut: ssh keys/known_hosts unreadable; the git credential helper cannot run'
    echo ';;    (denying the gitconfig files instead makes git itself fatal on EPERM -- measured)'
    echo "(deny file-read* (subpath \"$home/.ssh\") (subpath \"$home/.config/gh\"))"
    if [[ -n "$cred_helper" ]]; then
      echo "(deny file-read* (literal \"$cred_helper\"))"
      echo "(deny process-exec (literal \"$cred_helper\"))"
    fi
    sb_signal_rules
    echo ';; 6. TARGETS LAST -- these must beat every allow above, wherever the target lives'
    echo "(deny file-write* $(sb_subpaths "$workdir"))        ; WORKDIR (worker cwd)"
    echo "(deny file-write* $(sb_subpaths "$WORKSPACE"))      ; WORKSPACE (live checkout, RUNDIR, ledger)"
    echo "(deny file-write* $(sb_subpaths "$DISPATCH_ISOLATION_ROOT"))  ; other dispatches"
    if [[ -n "$PLAN_DIR" ]]; then
      local lt
      while IFS= read -r lt; do
        [[ -n "$lt" ]] && echo "(deny file-write* $(sb_subpaths "$lt"))  ; live target (plan lane: planned, never written here)"
      done <<<"$LIVE_TARGETS"
      echo ';; 7. PLAN LANE (DGN-1458): the one writable place -- RUNDIR/plan (the profile itself stays denied)'
      echo "(allow file-write* $(sb_subpaths "$PLAN_DIR"))"
    fi
    if [[ -n "${RUNDIR:-}" ]]; then
      echo "(allow file-write* $(sb_subpaths "$RUNDIR/tmp")) ; per-run scratch"
    fi
  } > "$out" || return 1
  return 0
}

observe_profile_selfcheck() {
  local prof="$1"; shift
  local t probe
  if ! sandbox-exec -f "$prof" /usr/bin/true 2>/dev/null; then
    echo "[dispatch] observe profile does not compile: $prof -- refusing to spawn Accepted path: repair the sandbox renderer and rerun its profile self-check before retrying the original request." >&2
    return 1
  fi
  if ! sandbox-exec -f "$prof" /bin/sh -c 'd="$(mktemp -d)" && rmdir "$d"' 2>/dev/null; then
    echo "[dispatch] observe profile closed TMPDIR (the worker could not run) -- refusing to spawn Accepted path: repair the sandbox renderer and rerun its profile self-check before retrying the original request." >&2
    return 1
  fi
  signal_guard_selfcheck "$prof" observe || return 1
  for t in "$@"; do
    [[ -d "$t" ]] || continue
    probe="$t/.observe-probe-$$"
    if sandbox-exec -f "$prof" /bin/mkdir "$probe" 2>/dev/null; then
      rmdir "$probe" 2>/dev/null || true
      echo "[dispatch] observe profile self-check FAILED: sandbox allowed a write inside $t -- refusing to spawn Accepted path: repair the sandbox renderer and rerun its profile self-check before retrying the original request." >&2
      return 1
    fi
  done
  return 0
}


plan_dir_selfcheck() {
  [[ -n "$PLAN_DIR" ]] || return 0
  local probe="$PLAN_DIR/.plan-probe-$$"
  if ! sandbox-exec -f "$1" /bin/mkdir "$probe" 2>/dev/null; then
    echo "[dispatch] observe profile self-check FAILED: plan dir $PLAN_DIR is not writable -- refusing to spawn Accepted path: repair the named sandbox profile/path boundary and rerun its self-check before retrying the original request." >&2
    return 1
  fi
  rmdir "$probe" 2>/dev/null || true
  return 0
}

select_tool_policy() {
  TOOL_ARGS=()
  WRAP=()
  if [[ -z "$SANDBOX_PROFILE" || ! -f "$SANDBOX_PROFILE" || -z "$SESSION_ID" ]]; then
    echo "[dispatch-worker] observe mode without a rendered sandbox profile -- refusing to run Accepted path: relaunch through dispatch provisioning after the confinement profile renders and passes its self-check." >&2
    return 1
  fi
  observe_sandbox_available || return 1
  WRAP=(sandbox-exec -f "$SANDBOX_PROFILE")

  TOOL_ARGS=(--permission-mode default --allowedTools "Bash,WebSearch,WebFetch" \
    --disallowedTools "Task,Agent,Write,Edit,NotebookEdit" --session-id "$SESSION_ID")
  return 0
}

select_codex_tool_policy() {
  CODEX_ARGS=()
  WRAP=()
  if [[ -z "$SANDBOX_PROFILE" || ! -f "$SANDBOX_PROFILE" || -z "$SESSION_ID" ]]; then
    echo "[dispatch-worker] codex observe mode without a rendered sandbox profile -- refusing to run Accepted path: relaunch through dispatch provisioning after the confinement profile renders and passes its self-check." >&2
    return 1
  fi
  observe_sandbox_available || return 1
  WRAP=(sandbox-exec -f "$SANDBOX_PROFILE")

  CODEX_ARGS=(--sandbox danger-full-access --skip-git-repo-check)
  return 0
}

START_NOTICE_DIR="$DISPATCH_DIR/start-notice"
START_NOTICE_QUEUE="$START_NOTICE_DIR/queue"
START_NOTICE_LOCK="$START_NOTICE_DIR/.lock"
START_NOTICE_CLAIM="$START_NOTICE_DIR/.flusher"
START_NOTICE_WINDOW="${DOGANY_START_NOTICE_WINDOW:-8}"

START_NOTICE_MODEL_WAIT="${DOGANY_START_NOTICE_MODEL_WAIT:-45}"
case "$START_NOTICE_MODEL_WAIT" in
  ''|*[!0-9]*) START_NOTICE_MODEL_WAIT=45 ;;
esac
case "$START_NOTICE_WINDOW" in
  ''|*[!0-9]*)
    echo "[dispatch] DOGANY_START_NOTICE_WINDOW must be a whole number of seconds (got '$START_NOTICE_WINDOW') -- using 8" >&2
    START_NOTICE_WINDOW=8 ;;
esac

start_notice_name_is_clean() {
  local hits="" rc=0
  hits="$(printf '%s' "$1" | python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from bridge.machine_gate import find_execution_identifiers
for kind, sample in find_execution_identifiers(sys.stdin.read()):
    sys.stdout.write("%s: %s\n" % (kind, sample[:60]))
' "$WORKSPACE" 2>/dev/null)" || rc=$?
  if [[ $rc -ne 0 ]]; then
    return 0
  fi
  if [[ -n "$hits" ]]; then
    START_NOTICE_REJECT="$hits"
    return 1
  fi
  return 0
}

start_notice_spawn_flusher() {
  local py="python3" self
  if [[ -n "${BRIDGE_PYTHON:-}" ]]; then
    py="$BRIDGE_PYTHON"
  fi
  command -v "$py" >/dev/null 2>&1 || return 1
  self="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
  "$py" - "$START_NOTICE_DIR/flusher.log" "$self" --start-notice-flush <<'PYEOF' >/dev/null
import os, sys
log_path, target = sys.argv[1], sys.argv[2]
if os.fork() > 0:
    os._exit(0)
os.setsid()
devnull = os.open(os.devnull, os.O_RDONLY)
log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
os.dup2(devnull, 0)
os.dup2(log, 1)
os.dup2(log, 2)
os.close(devnull)
os.close(log)
os.execv(target, [target] + sys.argv[3:])
PYEOF
}

detach_watchdog() {
  "$1" - "$2" "$3" --watchdog-run "$4" <<'PYEOF'
import os, sys
log_path, target = sys.argv[1], sys.argv[2]
if os.fork() > 0:
    os._exit(0)
os.setsid()
print(os.getpid(), flush=True)
devnull = os.open(os.devnull, os.O_RDONLY)
log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
os.dup2(devnull, 0); os.dup2(log, 1); os.dup2(log, 2)
os.close(devnull); os.close(log)
os.chdir("/")
os.execv(target, [target] + sys.argv[3:])
PYEOF
}

start_run_watchdog() {
  local py="$1" self="$2" watchdog_pid
  watchdog_pid="$(detach_watchdog "$py" "$RUNDIR/watchdog.log" "$self" "$RUNDIR")"
  printf '%s\n' "${watchdog_pid:-}" > "$RUNDIR/watchdog.pid"
}

defer_stop_notice() {
  python3 - "$RUNDIR" "$LABEL" "$1" <<'PY_STOP'
import json, os, sys, time
root, label, text = sys.argv[1:]
try:
    since = os.stat(os.path.join(root, "cancel-by")).st_mtime
except OSError:
    since = time.time()
with open(os.path.join(root, "pending-stop.json"), "w") as fh:
    json.dump({"label": label, "text": text, "since": since, "queued": time.time()}, fh)
PY_STOP
  [[ $? -eq 0 ]] || return 1
  STOP_NOTICE_QUEUED=0
  start_notice_arm "$(owner_facing_name)" "$RUNDIR"
  if [[ "$STOP_NOTICE_QUEUED" == 1 ]]; then
    return 0
  fi
  
  rm -f "$RUNDIR/pending-stop.json"
  return 1
}

flush_stop_notice() {
  local run_dir="$1" body state push_output=""
  body="$(python3 - "$run_dir" "$DISPATCH_DIR" "$START_NOTICE_WINDOW" <<'PY_STOP'
import json, pathlib, sys, time
run, dispatch = map(pathlib.Path, sys.argv[1:3])
try:
    pending = json.loads((run / "pending-stop.json").read_text())
except (OSError, ValueError):
    sys.exit(1)
# Joining a nearly expired START batch still grants a full restart window.
time.sleep(max(0, min(float(sys.argv[3]),
                     pending["queued"] + float(sys.argv[3]) - time.time())))
for marker in dispatch.glob("dsp-*/notice-label"):
    try:
        if (marker.parent != run and marker.stat().st_mtime >= pending["since"]
                and marker.read_text().rstrip("\n") == pending["label"]):
            break
    except OSError:
        continue
else:
    print(pending["text"])
PY_STOP
)" || return 1
  state=suppressed-redispatch
  if [[ -n "$body" ]]; then
    state=sent
    if push_output="$("$PUSH_SH" --framework-notice --notice-id "dispatch:$(basename "$run_dir"):end" --text "$body" --audience owner)"; then
      [[ -z "$push_output" ]] || printf '%s\n' "$push_output"
      state="$(printf '%s' "$push_output" | dispatch_notice_state)"
    else
      state=notify-failed
    fi
  fi
  python3 - "$run_dir/status.json" "$state" <<'PY_STOP'
import json, os, sys
path, state = sys.argv[1:]
try:
    with open(path) as fh:
        doc = json.load(fh)
    doc["notify"] = state
    tmp = path + ".stop.tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
except (OSError, ValueError):
    pass
PY_STOP
}

start_notice_arm() {
  [[ "${RETURN_ROUTE_KIND:-inbox}" == inbox ]] || return 0
  local name="$1" claim_age now mtime spawn=0
  if [[ -z "$name" ]]; then
    echo "[dispatch] start notice skipped: no owner-facing name resolved" >&2
    return 0
  fi
  START_NOTICE_REJECT=""
  if ! start_notice_name_is_clean "$name"; then
    echo "[dispatch] start notice SKIPPED for this run: its owner-facing name carries an execution identifier, which the send rail refuses (and one bad name would take the whole batch's notice with it):" >&2
    printf '%s\n' "$START_NOTICE_REJECT" | sed 's/^/[dispatch]   /' >&2
    echo "[dispatch]   fix: re-dispatch with a --display-name written for a human (no run ids, hashes, paths or exit codes). The run itself is unaffected." >&2
    return 0
  fi
  if ! mkdir -p "$START_NOTICE_DIR" 2>/dev/null; then
    echo "[dispatch] start notice skipped: cannot create $START_NOTICE_DIR" >&2
    return 0
  fi
  if ! dir_lock "$START_NOTICE_LOCK"; then
    echo "[dispatch] start notice skipped: queue lock unavailable" >&2
    return 0
  fi

  if [[ -n "${2:-}" ]]; then
    printf '@stop\t%s\n' "$2" >> "$START_NOTICE_QUEUE"
    STOP_NOTICE_QUEUED=1
  elif [[ -z "$MODEL_SLOT" && ( "$RUNTIME" == "claude-code" || "$RUNTIME" == "claude-api" ) && -n "${RUNDIR:-}" \
        && "$RUNDIR" != *$'\t'* ]]; then
    printf '@run\t%s\t%s\t%s\t%s\t%s\n' "$RUNDIR" "$MODEL" "$(date +%H:%M)" "$name" "$RUNTIME" \
      >> "$START_NOTICE_QUEUE"
  elif [[ -n "$MODEL_SLOT" || "${RUNTIME:-}" == script ]]; then
    
    printf '@bare\t%s\t%s\n' "$(date +%H:%M)" "$name" >> "$START_NOTICE_QUEUE"
  else
    printf '%s\t%s\t%s\t%s\n' "$(owner_model_display_cap)" "$(date +%H:%M)" "$name" "$RUNTIME" \
      >> "$START_NOTICE_QUEUE"
  fi

  if [[ -d "$START_NOTICE_CLAIM" ]]; then
    now="$(date +%s)"

    mtime="$(stat -c %Y "$START_NOTICE_CLAIM" 2>/dev/null || stat -f %m "$START_NOTICE_CLAIM" 2>/dev/null || echo "$now")"
    claim_age=$(( now - mtime ))
    if [[ $claim_age -gt $(( START_NOTICE_WINDOW + 60 )) ]]; then
      echo "[dispatch] start notice: stealing a stale flusher claim (${claim_age}s old)" >&2
      rmdir "$START_NOTICE_CLAIM" 2>/dev/null || true
    fi
  fi

  if mkdir "$START_NOTICE_CLAIM" 2>/dev/null; then
    spawn=1
  fi
  dir_unlock "$START_NOTICE_LOCK"
  if [[ $spawn -eq 1 ]]; then
    if start_notice_spawn_flusher; then
      echo "[dispatch]   start notice: owner push in ${START_NOTICE_WINDOW}s (batched; dispatches spawned within the window share one message)"
    else

      if dir_lock "$START_NOTICE_LOCK"; then
        rmdir "$START_NOTICE_CLAIM" 2>/dev/null || true
        dir_unlock "$START_NOTICE_LOCK"
      fi
      [[ -z "${2:-}" ]] || STOP_NOTICE_QUEUED=0
      echo "[dispatch] start notice: could not detach the sender (no python3) -- notice deferred to the next dispatch" >&2
    fi
  else
    echo "[dispatch]   start notice: joined the pending batch (one message for this burst)"
  fi
  return 0
}

do_start_notice_flush() {
  sleep "$START_NOTICE_WINDOW"
  local items="" n=0 text
  mkdir -p "$START_NOTICE_DIR" 2>/dev/null || true
  if ! dir_lock "$START_NOTICE_LOCK"; then
    echo "[dispatch-start-notice] queue lock unavailable -- leaving the queue for the next dispatch" >&2
    return 0
  fi
  if [[ -f "$START_NOTICE_QUEUE" ]]; then
    items="$(cat "$START_NOTICE_QUEUE" 2>/dev/null || true)"
    : > "$START_NOTICE_QUEUE"
  fi

  rmdir "$START_NOTICE_CLAIM" 2>/dev/null || true
  dir_unlock "$START_NOTICE_LOCK"
  local queued remaining=""
  while IFS= read -r queued; do
    if [[ "$queued" == "@stop"$'\t'* ]]; then
      flush_stop_notice "${queued#@stop$'\t'}" || true
    elif [[ -n "$queued" ]]; then
      remaining="${remaining}${remaining:+$'\n'}${queued}"
    fi
  done <<< "$items"
  items="$remaining"
  n="$(printf '%s\n' "$items" | grep -c '[^[:space:]]' || true)"
  if [[ -z "$items" || "${n:-0}" -eq 0 ]]; then
    return 0
  fi

  # owner-approved 2026-09-28: every start notice titles with background_term (DGN-1767)
  text="$(notice_title started)"
  if ! printf '%s\n' "$items" | grep -qv '^@bare'; then
    text="▶️ $(background_term) 작업 시작"
  fi
  if [[ "$n" -gt 1 ]]; then
    local batch_tpl
    batch_tpl="$(notice_i18n dispatch.notice.batch)"
    batch_tpl="${batch_tpl//\{title\}/$text}"
    text="${batch_tpl//\{count\}/$n}"
  fi

  local -a wait_dirs=()
  local qline qrest qdir
  while IFS= read -r qline; do
    [[ "$qline" == "@run"$'\t'* ]] || continue
    qrest="${qline#@run$'\t'}"
    qdir="${qrest%%$'\t'*}"
    [[ -n "$qdir" ]] && wait_dirs+=("$qdir")
  done <<< "$items"
  local observed_map=""
  if [[ ${#wait_dirs[@]} -gt 0 && -r "$SCRIPT_DIR/lib/model_display.py" ]]; then
    observed_map="$(python3 "$SCRIPT_DIR/lib/model_display.py" --wait-observed \
                      "$START_NOTICE_MODEL_WAIT" "${wait_dirs[@]}" 2>/dev/null)" || observed_map=""
  fi
  local qmodel qtime qname qalias qobs label qrt
  while IFS= read -r qline; do
    if [[ "$qline" == "@run"$'\t'* ]]; then
      qrest="${qline#@run$'\t'}"
      IFS=$'\t' read -r qdir qalias qtime qname qrt <<< "$qrest"
      qobs="$(awk -F'\t' -v d="$qdir" '$1 == d { print $2; exit }' <<< "$observed_map")"
      label=""
      if [[ -r "$SCRIPT_DIR/lib/model_display.py" ]]; then
        label="$(python3 "$SCRIPT_DIR/lib/model_display.py" --tsv "$MODEL_DISPLAY_BINDINGS" \
                   "${qrt:-claude-code}" "$qalias" "$qobs" 2>/dev/null)" || label=""
      fi
      [[ -n "$label" ]] || label="$qalias"
      text="${text}"$'\n'"$(run_block subagent "$label" "${qrt:-claude-code}" "$qname" "$qtime")"
      continue
    fi
    IFS=$'\t' read -r qmodel qtime qname qrt <<< "$qline"
    [[ -z "${qmodel}${qtime}${qname}" ]] && continue
    if [[ -z "${qtime}${qname}" ]]; then
      
      text="${text}"$'\n'"<blockquote>${qmodel}</blockquote>"
      continue
    fi
    if [[ "$qmodel" == "@bare" ]]; then
      text="${text}"$'\n'"$(run_block script "" "" "$qname" "$qtime")"
      continue
    fi
    text="${text}"$'\n'"$(run_block subagent "$qmodel" "${qrt:-claude-code}" "$qname" "$qtime")"
  done <<< "$items"
  if "$PUSH_SH" --framework-notice --text "$text" --audience owner; then
    echo "[dispatch-start-notice] sent (${n} item(s))"
  else
    echo "[dispatch-start-notice] push FAILED (rc=$?) -- the dispatches themselves are unaffected; body was: $text" >&2
  fi
  return 0
}

bg_notice_args_ok() {
  local v
  for v in "$DISPLAY_NAME" "$MODEL_SLOT"; do
    if [[ -z "${v//[[:space:]]/}" ]]; then
      echo "[dispatch-bg-notice] --display-name and --model-slot are both required" >&2
      return 1
    fi
    case "$v" in
      *$'\t'*|*$'\n'*|*$'\r'*|*'|'*)
        echo "[dispatch-bg-notice] no tab, newline, carriage return or '|' in --display-name / --model-slot" >&2
        return 1 ;;
    esac
  done
  return 0
}

do_bg_start_notice() {
  bg_notice_args_ok || return 1
  start_notice_arm "$(owner_facing_name "$DISPLAY_NAME")"
}

do_bg_end_notice() {
  bg_notice_args_ok || return 1
  local result text

  case "$BG_RESULT" in
    completed) result="completed" ;;
    killed|stopped|canceled|cancelled) result="canceled" ;;
    *) result="error" ;;
  esac
  case "$BG_ELAPSED_SECS" in ''|*[!0-9]*) BG_ELAPSED_SECS="" ;; esac
  case "$BG_END_CLOCK" in [0-9][0-9]:[0-9][0-9]) ;; *) BG_END_CLOCK="$(date +%H:%M)" ;; esac
  text="$(end_notice_text "$result" "$(owner_facing_name "$DISPLAY_NAME")" "$BG_END_CLOCK" "$BG_ELAPSED_SECS")"

  local wait_s=0
  case "$BG_NOT_BEFORE" in
    ''|*[!0-9]*) ;;
    *) wait_s=$(( BG_NOT_BEFORE - $(date +%s) )) ;;
  esac
  if [[ $wait_s -gt 0 ]]; then
    [[ $wait_s -gt 60 ]] && wait_s=60
    sleep "$wait_s"
  fi
  if "$PUSH_SH" --framework-notice --text "$text" --audience owner; then
    echo "[dispatch-bg-notice] end notice sent"
  else
    echo "[dispatch-bg-notice] end notice push FAILED -- body was: $text" >&2
    return 1
  fi
}

spawn_helper_error() {
  local rc="$1" helper="$2" line="$3"
  if [[ "$BASH_SUBSHELL" -eq "$_spawn_error_subshell" ]]; then
    printf '[dispatch] ERROR: helper %s failed (rc=%s, line=%s)\n' \
      "$helper" "$rc" "$line" >&2
  fi
  return "$rc"
}

detach_worker() {
  "$1" - "$2" "$3" --worker-run "$4" <<'PYEOF'
import os, sys
log_path, target = sys.argv[1], sys.argv[2]
if os.fork() > 0:
    os._exit(0)  # launcher-side parent: return control to the caller now
os.setsid()      # own session: session-CLI/launchd group teardown cannot reach us
print(os.getpid(), flush=True)  # daemon pid -> launcher capture; releases the pipe
devnull = os.open(os.devnull, os.O_RDONLY)
log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
os.dup2(devnull, 0)
os.dup2(log, 1)
os.dup2(log, 2)
os.close(devnull)
os.close(log)
os.execv(target, [target] + sys.argv[3:])
PYEOF
}

do_spawn() {
  resolve_return_route || exit 2
  local _spawn_error_subshell="$BASH_SUBSHELL"
  
  set -E
  trap 'spawn_helper_error "$?" "${FUNCNAME[0]:-do_spawn}" "$LINENO"' ERR
  RUNID="${DOGANY_HOST_RUNID:-dsp-$(date +%Y%m%d-%H%M%S)-$$-$RANDOM}"
  validate_access_mode || exit 1
  validate_spawn_args || exit 1
  attach_inline || exit 1
  validate_live_targets || exit 1
  check_prompt_size || exit 1
  select_default_runtime || exit 2
  dispatch_disk_preflight || exit 2
  check_prompt_size || exit 1

  observe_sandbox_available || exit 2
  local rc=0
  run_gates || rc=$?
  if [[ $rc -eq 3 ]]; then
    exit 0   
  fi
  if [[ $rc -ne 0 ]]; then
    exit 2
  fi
  if ! runtime_selection_consistency; then
    active_count_decr
    exit 2
  fi
  
  if ! dispatch_disk_preflight; then
    active_count_decr
    exit 2
  fi

  local tries=0
  while :; do
    [[ $tries -eq 0 && -n "${RUNID:-}" ]] || RUNID="dsp-$(date +%Y%m%d-%H%M%S)-$(printf '%04d' $(( RANDOM % 10000 )))"
    RUNDIR="$DISPATCH_DIR/$RUNID"
    if mkdir "$RUNDIR" 2>/dev/null; then
      break
    fi
    tries=$(( tries + 1 ))
    if [[ $tries -ge 5 ]]; then
      echo "[dispatch] could not allocate a unique run dir under $DISPATCH_DIR" >&2
      active_count_decr
      exit 1
    fi
    sleep 1
  done

  worker_scratch_prepare || { active_count_decr; exit 1; }
  BRANCH=""
    LEDGER_BRANCH="dsp/$RUNID"

    WORKDIR="${JOB_WORKDIR:-$WORKSPACE}"

    SANDBOX_PROFILE="$RUNDIR/observe.sb"

    SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"

    local plan_targets=()
    if [[ -n "$LIVE_TARGETS" ]]; then
      PLAN_DIR="$RUNDIR/plan"
      mkdir -p "$PLAN_DIR"
      while IFS= read -r _lt; do plan_targets+=("$_lt"); done <<<"$LIVE_TARGETS"
    fi
    if ! render_observe_profile "$SANDBOX_PROFILE" "$WORKDIR" "$SESSION_ID" \
       || ! observe_profile_selfcheck "$SANDBOX_PROFILE" "$WORKDIR" "$WORKSPACE" ${plan_targets[@]+"${plan_targets[@]}"} \
       || ! plan_dir_selfcheck "$SANDBOX_PROFILE"; then
      active_count_decr
      rm -rf "$RUNDIR"
      exit 2
    fi

  render_dispatch_prompt > "$RUNDIR/prompt.md"
  resolve_dispatch_why
  brief_why_what_check
  brief_unit_gate_check

  {
    run_env_write DOGANY_RUN_SCRATCH "$DOGANY_RUN_SCRATCH"
    return_route_env_write
    run_env_write RUNID "$RUNID"
    run_env_write LABEL "$LABEL"
    run_env_write DISPLAY_NAME "$DISPLAY_NAME"
    run_env_write DISPATCH_KIND "$DISPATCH_KIND"
    run_env_write TICKET "$TICKET"
    run_env_write DISPATCH_WHY "$DISPATCH_WHY"
    run_env_write BRIEF_WHY_WHAT "$BRIEF_WHY_WHAT"
    run_env_write BRIEF_WHY "$BRIEF_WHY"
    run_env_write BRIEF_WHAT "$BRIEF_WHAT"
    run_env_write BRIEF_SAMPLE "$BRIEF_SAMPLE"
    run_env_write BRIEF_UNIT_GATE "$BRIEF_UNIT_GATE"
    run_env_write BRIEF_UNIT "$BRIEF_UNIT"
    run_env_write BRIEF_GATE "$BRIEF_GATE"
    run_env_write MODEL "$MODEL"
    run_env_write MODEL_SOURCE "$MODEL_SOURCE"
    run_env_write REQUESTED_MODEL "$REQUESTED_MODEL"
    run_env_write CODEX_MODEL "$CODEX_MODEL"
    run_env_write EFFORT "$EFFORT"
    run_env_write EFFORT_SOURCE "$EFFORT_SOURCE"
    run_env_write WORK_CLASS "$WORK_CLASS"
    run_env_write WORK_CLASS_DIFFICULTY "$WORK_CLASS_DIFFICULTY"
    run_env_write WORK_CLASS_RECEIPT "$WORK_CLASS_RECEIPT"
    run_env_write WORK_CLASS_REASON "$WORK_CLASS_REASON"
    run_env_write WORK_CLASS_FIELDS "$WORK_CLASS_FIELDS"
    run_env_write RUNTIME "$RUNTIME"
    run_env_write RUNTIME_SELECTED "$RUNTIME_SELECTED"
    run_env_write REQUESTED_RUNTIME "$REQUESTED_RUNTIME"
    run_env_write RUNTIME_SELECTION_SOURCE "$RUNTIME_SELECTION_SOURCE"
    run_env_write RUNTIME_SELECTION_REASON "$RUNTIME_SELECTION_REASON"
    run_env_write RUNTIME_SELECTION_DETAIL "$RUNTIME_SELECTION_DETAIL"
    run_env_write RUNTIME_FALLBACK_DIRECTION "$RUNTIME_FALLBACK_DIRECTION"
    run_env_write RUNTIME_FALLBACK_NOTE "$RUNTIME_FALLBACK_NOTE"
    run_env_write DECISION_ID "$DECISION_ID"
    run_env_write CODEX_QUOTA_NOTE "$CODEX_QUOTA_NOTE"
    run_env_write CODEX_HOOK_NOTE "$CODEX_HOOK_NOTE"
    run_env_write CODEX_ADAPTER_ROOT "$CODEX_ADAPTER_ROOT"
    run_env_write CODEX_UNGUARDED_WHY "$CODEX_UNGUARDED_WHY"
    run_env_write ACCESS_MODE "$ACCESS_MODE"
    run_env_write JOB_WORKDIR "$JOB_WORKDIR"
    run_env_write BRANCH "$BRANCH"
    run_env_write LEDGER_BRANCH "$LEDGER_BRANCH"
    run_env_write TIMEOUT_MIN "$TIMEOUT_MIN"
    run_env_write NOTIFY "$NOTIFY"
    run_env_write CANCEL_BY "$CANCEL_BY"
    run_env_write WORKDIR "$WORKDIR"
    run_env_write SANDBOX_PROFILE "$SANDBOX_PROFILE"
    run_env_write LIVE_TARGETS "$LIVE_TARGETS"
    run_env_write PLAN_DIR "$PLAN_DIR"
    run_env_write SESSION_ID "$SESSION_ID"
    
    run_env_write SPAWN_EPOCH "$(date +%s)"
    run_env_write HOST_LEASE_ID "$HOST_LEASE_ID"
    run_env_write REPLACED_FROM "$REPLACED_FROM"
    run_env_write REPLACE_REASON "$REPLACE_REASON"
  } > "$RUNDIR/run.env"

  return_route_request_json "$RUNDIR/argv.json" \
    ${ORIG_ARGV[@]+"${ORIG_ARGV[@]}"} || exit 2

  if ! ensure_ledger; then
    active_count_decr
    rm -rf "$RUNDIR"
    exit 1
  fi
  local item="$LABEL"
  [[ -n "$TICKET" ]] && item="$LABEL [$TICKET]"

  local board_item
  board_item="$(owner_facing_name)"

  local spawn_note="dispatch 스폰 (model=$MODEL runtime=$RUNTIME)"
  if [[ -n "$CODEX_HOOK_NOTE" ]]; then
    spawn_note="dispatch 스폰 (model=$MODEL runtime=$RUNTIME hooks=$CODEX_HOOK_NOTE)"
  fi
  if [[ -n "$CODEX_MODEL" ]]; then
    spawn_note="${spawn_note%)} requested_model=${REQUESTED_MODEL:-binding} codex_model=$CODEX_MODEL)"
  fi
  
  [[ -n "$WORK_CLASS_REASON" ]] && spawn_note="${spawn_note%)} work_class_reason=$WORK_CLASS_REASON receipt=$WORK_CLASS_RECEIPT)"
  if ! "$LEDGER_UPDATE" "$LEDGER_BRANCH" running "$spawn_note" \
        --item "$board_item" --ts "$(date '+%Y-%m-%d %H:%M %Z')"; then
    echo "[dispatch] ledger running-row write failed -- refusing to spawn an unowned worker Accepted path: restore ledger access at $LEDGER_FILE, then retry the original request before launching a worker." >&2
    active_count_decr
    rm -rf "$RUNDIR"
    exit 1
  fi

  python3 "$SCRIPT_DIR/terminal-state-ledger.py" open --surface dispatch \
    --id "$RUNID" --ttl "$(( (TIMEOUT_MIN + 30) * 60 ))" --notify session \
    --note "$item" --evidence "$RUNDIR/out.log" >/dev/null || true

  local detach_py=""
  if [[ -n "${BRIDGE_PYTHON:-}" ]]; then
    detach_py="$BRIDGE_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    detach_py="python3"
  else
    echo "[dispatch] no python3 for setsid detach -- aborting (nohup fallback would die with the caller group)" >&2
    "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed "dispatch spawn failed (no python3)" || true
    active_count_decr
    exit 1
  fi

  local self worker_pid
  self="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
  worker_pid="$(detach_worker "$detach_py" "$RUNDIR/worker.log" "$self" "$RUNDIR")"
  printf '%s\n' "${worker_pid:-}" > "$RUNDIR/pid"
  host_bind "${worker_pid:-}"
  start_run_watchdog "$detach_py" "$self"

  echo "[dispatch] spawned: runid=$RUNID pid=${worker_pid:-?} (own session via setsid)"
  echo "[dispatch]   label:  $item"

  echo "[dispatch]   owner-facing name: $board_item (lang=$OWNER_LANG)"
  if [[ -z "$EFFORT" ]]; then
    echo "[dispatch]   effort: CLI default, no flag (source=${EFFORT_SOURCE:-unmeasured})"
  elif ! effort_passed_to_runtime "$RUNTIME" "$EFFORT"; then
    echo "[dispatch]   effort: $EFFORT (source=$EFFORT_SOURCE) recorded but NOT passed -- runtime $RUNTIME has no verified control for this level"
  elif [[ "$RUNTIME" == "codex" ]]; then
    echo "[dispatch]   effort: $EFFORT (source=$EFFORT_SOURCE, passed as codex -c model_reasoning_effort)"
  else
    echo "[dispatch]   effort: $EFFORT (source=$EFFORT_SOURCE, passed as claude --effort)"
  fi
  [[ -n "$EFFORT_WARNING" ]] && echo "[dispatch]   WARNING effort: $EFFORT_WARNING"
  echo "[dispatch]   model:  $(owner_model_display)  runtime: $RUNTIME  timeout: ${TIMEOUT_MIN}m  notify: $NOTIFY"

  [[ -n "$CODEX_QUOTA_NOTE" ]] && echo "[dispatch]   codex quota: $CODEX_QUOTA_NOTE"

  echo "[dispatch]   dispatch cap: $(read_active_count)/$MAX_ACTIVE ($DYNAMIC_CAP_NOTE)"
  echo "[dispatch]   ledger: $LEDGER_BRANCH (running)"
  [[ -n "$JOB_WORKDIR" ]] && echo "[dispatch]   workdir: $JOB_WORKDIR"
  [[ -n "$SANDBOX_PROFILE" ]] && echo "[dispatch]   observe: kernel read-only (seatbelt $SANDBOX_PROFILE)"
  [[ -n "$PLAN_DIR" ]] && echo "[dispatch]   plan lane: live targets $(tr '\n' ' ' <<<"$LIVE_TARGETS")-> plan $PLAN_DIR/plan.json; apply: $SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}") --apply-plan $RUNID [--apply]"
  echo "[dispatch]   logs:   $RUNDIR/out.log"
  echo "[dispatch]   cancel: $self --cancel $RUNID"

  if [[ -n "${worker_pid:-}" ]]; then
    printf '%s\n' "$LABEL" > "$RUNDIR/notice-label"
  fi
  if [[ "$NOTIFY" == "silent" ]]; then
    echo "[dispatch]   start notice: none (--notify silent: no owner start/end pair; the caller reports this run)"
  elif [[ -n "${worker_pid:-}" ]]; then
    start_notice_arm "$(owner_facing_name)"
  else
    echo "[dispatch] start notice skipped: detach returned no worker pid" >&2
  fi

  trap - ERR
}

validate_script_job() {
  local bad=""
  [[ -z "$TASK_FILE" ]] || bad="--task"
  [[ -z "$DISPATCH_KIND" ]] || bad="${bad:+$bad }--kind"
  [[ -z "$MODEL" ]] || bad="${bad:+$bad }--model"
  [[ "$READ_ONLY" != 1 ]] || bad="${bad:+$bad }--read-only/--write"
  [[ "$RUNTIME_SELECTION_SOURCE" != flag ]] || bad="${bad:+$bad }--runtime"
  if [[ -n "$bad" ]]; then
    echo "[dispatch] --script runs a command, not a model: $bad does not apply (put the command after --)" >&2
    return 1
  fi
  if [[ -z "$LABEL" ]]; then
    echo "[dispatch] --label is required" >&2
    return 1
  fi
  case "$LABEL" in
    *'|'*|*$'\n'*|*$'\r'*|*/*) echo "[dispatch] invalid --label: no '|', '/', newline or carriage return" >&2; return 1 ;;
  esac
  if [[ ${#SCRIPT_ARGV[@]} -eq 0 ]]; then
    echo "[dispatch] --script needs the command after --: $0 --script --label L ... -- <argv...>" >&2
    return 1
  fi
  [[ -n "$SCRIPT_WORKDIR" ]] || SCRIPT_WORKDIR="$WORKSPACE"
  if [[ "$SCRIPT_WORKDIR" != /* || ! -d "$SCRIPT_WORKDIR" ]]; then
    echo "[dispatch] --workdir must be an existing absolute directory: $SCRIPT_WORKDIR" >&2
    return 1
  fi
  local cmd="${SCRIPT_ARGV[0]}"
  if [[ "$cmd" == */* ]]; then
    [[ "$cmd" == /* ]] || cmd="$SCRIPT_WORKDIR/$cmd"
    [[ -x "$cmd" && -f "$cmd" ]] || { echo "[dispatch] command not executable: ${SCRIPT_ARGV[0]} Accepted path: supply an executable command resolvable from the declared --workdir/PATH." >&2; return 1; }
  else
    command -v "$cmd" >/dev/null 2>&1 || { echo "[dispatch] command not found: $cmd Accepted path: supply an executable command resolvable from the declared --workdir/PATH." >&2; return 1; }
  fi
  case "$NOTIFY" in
    push|silent) ;;
    *) echo "[dispatch] invalid --notify '$NOTIFY' (push|silent)" >&2; return 1 ;;
  esac
  if ! [[ "$TIMEOUT_MIN" =~ ^[0-9]+$ ]] || [[ "$TIMEOUT_MIN" -lt 1 ]]; then
    echo "[dispatch] invalid --timeout '$TIMEOUT_MIN' (whole minutes >= 1)" >&2
    return 1
  fi
  validate_display_name || return 1
  return 0
}

script_admission_line() {  
  printf '%s\trunid=%s\tlabel=%s\tadmission=%s\tlease=%s\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$RUNID" "$LABEL" "$1" "${HOST_LEASE_ID:-none}" \
    >> "$DISPATCH_DIR/script-admissions.log" 2>/dev/null || true
}

do_spawn_script() {
  resolve_dispatch_why
  resolve_return_route || exit 2
  local _spawn_error_subshell="$BASH_SUBSHELL"
  set -E
  trap 'spawn_helper_error "$?" "${FUNCNAME[0]:-do_spawn_script}" "$LINENO"' ERR
  validate_script_job || exit 1
  RUNTIME="script"
  dispatch_disk_preflight || exit 2
  RUNID="${DOGANY_HOST_RUNID:-dsp-$(date +%Y%m%d-%H%M%S)-$$-$RANDOM}"
  mkdir -p "$DISPATCH_DIR"

  [[ "$NO_QUEUE" == 1 ]] && HOST_FORCE=1
  if ! active_count_incr script; then
    [[ -n "$HOST_FULL_NOTE" ]] || HOST_FULL_NOTE="script lane cap $(read_active_count script)/$MAX_ACTIVE"
    if host_enqueue; then
      echo "[dispatch] queued #$HOST_QUEUE_POSITION (ticket $HOST_TICKET_ID): starts automatically when a slot frees."
      exit 0
    fi
    echo "[dispatch] refused: script lane full and the host queue did not accept it (${HOST_NOTE:-$HOST_FULL_NOTE}); retry, or use --no-queue" >&2
    exit 2
  fi

  local tries=0
  while :; do
    [[ $tries -eq 0 && -n "${RUNID:-}" ]] || RUNID="dsp-$(date +%Y%m%d-%H%M%S)-$(printf '%04d' $(( RANDOM % 10000 )))"
    RUNDIR="$DISPATCH_DIR/$RUNID"
    mkdir "$RUNDIR" 2>/dev/null && break
    tries=$(( tries + 1 ))
    if [[ $tries -ge 5 ]]; then
      echo "[dispatch] could not allocate a unique run dir under $DISPATCH_DIR" >&2
      active_count_decr
      exit 1
    fi
    sleep 1
  done
  worker_scratch_prepare || { active_count_decr; exit 1; }
  BRANCH=""
  LEDGER_BRANCH="dsp/$RUNID"
  WORKDIR="$SCRIPT_WORKDIR"
  SCRIPT_USAGE_FILE="$RUNDIR/usage.jsonl"
  : > "$SCRIPT_USAGE_FILE"
  local a
  for a in "${SCRIPT_ARGV[@]}"; do printf '%s\0' "$a"; done > "$RUNDIR/job.argv"
  { printf 'script job (DGN-2115)\nworkdir: %s\nargv:' "$WORKDIR"; printf ' %q' "${SCRIPT_ARGV[@]}"; printf '\n'; } > "$RUNDIR/prompt.md"

  {
    run_env_write DOGANY_RUN_SCRATCH "$DOGANY_RUN_SCRATCH"
    return_route_env_write
    run_env_write RUNID "$RUNID"
    run_env_write LABEL "$LABEL"
    run_env_write DISPLAY_NAME "$DISPLAY_NAME"
    run_env_write TICKET "$TICKET"
    run_env_write DISPATCH_WHY "$DISPATCH_WHY"
    run_env_write MODEL ""
    run_env_write RUNTIME "script"
    run_env_write ACCESS_MODE "script"
    run_env_write SCRIPT_JOB "1"
    run_env_write SCRIPT_USAGE_FILE "$SCRIPT_USAGE_FILE"
    run_env_write JOB_WORKDIR ""
    run_env_write BRANCH ""
    run_env_write LEDGER_BRANCH "$LEDGER_BRANCH"
    run_env_write TIMEOUT_MIN "$TIMEOUT_MIN"
    run_env_write NOTIFY "$NOTIFY"
    run_env_write CANCEL_BY "$CANCEL_BY"
    run_env_write WORKDIR "$WORKDIR"
    run_env_write SPAWN_EPOCH "$(date +%s)"
    run_env_write HOST_LEASE_ID "$HOST_LEASE_ID"
  } > "$RUNDIR/run.env"
  return_route_request_json "$RUNDIR/argv.json" \
    ${ORIG_ARGV[@]+"${ORIG_ARGV[@]}"} || exit 2

  if ! ensure_ledger; then
    active_count_decr
    script_discard_rundir
    exit 1
  fi
  local item="$LABEL" board_item
  [[ -n "$TICKET" ]] && item="$LABEL [$TICKET]"
  board_item="$(owner_facing_name)"
  if ! "$LEDGER_UPDATE" "$LEDGER_BRANCH" running "dispatch 스폰 (model=script runtime=script kind=script-job)" \
        --item "$board_item" --ts "$(date '+%Y-%m-%d %H:%M %Z')"; then
    echo "[dispatch] ledger running-row write failed -- refusing to spawn an unowned job Accepted path: restore ledger access at $LEDGER_FILE, then retry the original request before launching a worker." >&2
    active_count_decr
    script_discard_rundir
    exit 1
  fi
  python3 "$SCRIPT_DIR/terminal-state-ledger.py" open --surface dispatch \
    --id "$RUNID" --ttl "$(( (TIMEOUT_MIN + 30) * 60 ))" --notify session \
    --note "$item" --evidence "$RUNDIR/out.log" >/dev/null || true

  local detach_py=""
  if [[ -n "${BRIDGE_PYTHON:-}" ]]; then detach_py="$BRIDGE_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then detach_py="python3"
  else
    echo "[dispatch] no python3 for setsid detach -- aborting" >&2
    "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed "dispatch spawn failed (no python3)" || true
    active_count_decr
    exit 1
  fi
  local self worker_pid
  self="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
  worker_pid="$(detach_worker "$detach_py" "$RUNDIR/worker.log" "$self" "$RUNDIR")"
  printf '%s\n' "${worker_pid:-}" > "$RUNDIR/pid"
  host_bind "${worker_pid:-}"
  start_run_watchdog "$detach_py" "$self"
  if [[ "$NO_QUEUE" == 1 ]]; then script_admission_line no-queue; else script_admission_line granted; fi

  echo "[dispatch] spawned: runid=$RUNID pid=${worker_pid:-?} (own session via setsid)"
  echo "[dispatch]   script job: $(printf '%q ' "${SCRIPT_ARGV[@]}")"
  echo "[dispatch]   label:  $item"
  echo "[dispatch]   owner-facing name: $board_item (lang=$OWNER_LANG)"
  echo "[dispatch]   workdir: $WORKDIR  timeout: ${TIMEOUT_MIN}m  notify: $NOTIFY"
  echo "[dispatch]   admission: $([[ "$NO_QUEUE" == 1 ]] && echo 'no-queue (started at once; admission line logged)' || echo 'granted by the host queue') ($DYNAMIC_CAP_NOTE)"
  echo "[dispatch]   ledger: $LEDGER_BRANCH (running)"
  echo "[dispatch]   usage:  append JSON lines to \$DOGANY_SCRIPT_USAGE_FILE ($SCRIPT_USAGE_FILE)"
  echo "[dispatch]   logs:   $RUNDIR/out.log"
  echo "[dispatch]   cancel: $self --cancel $RUNID"
  if [[ -n "${worker_pid:-}" ]]; then
    printf '%s\n' "$LABEL" > "$RUNDIR/notice-label"
  fi
  if [[ "$NOTIFY" == "silent" ]]; then
    echo "[dispatch]   start notice: none (--notify silent: no owner start/end pair)"
  elif [[ -n "${worker_pid:-}" ]]; then
    start_notice_arm "$(owner_facing_name)"
  else
    echo "[dispatch] start notice skipped: detach returned no worker pid" >&2
  fi
  trap - ERR
}

script_discard_rundir() {
  [[ -n "${RUNID:-}" && -d "$DISPATCH_DIR/$RUNID" ]] && rm -rf "$DISPATCH_DIR/$RUNID"
  return 0
}

worker_backstop() {
  cat <<'EOF'
You are a detached worker carrying out the supplied task for the requesting agent.
Run commands in the foreground and finish each command before returning.
Use DOGANY_RUN_SCRATCH (also TMPDIR) for scratch files and temporary evidence.
Create DOGANY_RUN_SCRATCH/.keep only for failed or blocked runs; it expires
after 24 hours, and completed-run scratch is always cleaned.
The wrapper owns status, cancellation, completion delivery and owner notices.
Do not send a separate notification or leave a background command unjoined.
Return the concrete result and identify anything that remains unfinished.
Your final report must include both labeled blocks:
Open items: what remains unfinished, or none
Re-checked: the previously working behaviors you exercised and how
Say done only when Open items is none. Never finish with a waiting placeholder.
EOF
  if [[ -n "${BRIEF_UNIT:-}" || -n "${BRIEF_GATE:-}" ]]; then
    printf '\nUnit of work: %s\nE2E gate: %s\n' "${BRIEF_UNIT:-}" "${BRIEF_GATE:-}"
  fi
}

resolve_dispatch_why() {
  local cli="$SCRIPT_DIR/why-net.py" v=""
  if [[ -n "$WHY_ID" ]]; then
    DISPATCH_WHY="$WHY_ID"
    return 0
  fi
  if [[ -n "$TICKET" && -f "$cli" ]]; then
    v="$(DOGANY_ROOT="$WORKSPACE" DOGANY_BOT_DATA_DIR="$WORKSPACE/.telegram_bot" python3 "$cli" lint-ticket --resolve "$TICKET" 2>/dev/null | tail -1)"
  fi
  case "$v" in
    why-[0-9]*|hypothesis) DISPATCH_WHY="$v" ;;
    *) DISPATCH_WHY="none" ;;
  esac
  return 0
}

brief_why_what_check() {
  local lib="$SCRIPT_DIR/lib/brief_why_what.py" line=""
  BRIEF_WHY_WHAT="unread"
  if [[ -f "$lib" && -f "${TASK_FILE:-}" ]]; then
    line="$(python3 "$lib" "$TASK_FILE" 2>/dev/null | head -n 1)" || line=""
  fi
  if [[ -n "$line" ]]; then

    BRIEF_WHY_WHAT="${line%%$'\t'*}"; line="${line#*$'\t'}"
    BRIEF_WHY="${line%%$'\t'*}";      line="${line#*$'\t'}"
    BRIEF_WHAT="${line%%$'\t'*}";     line="${line#*$'\t'}"
    BRIEF_SAMPLE="$line"
    [[ -n "$BRIEF_WHY_WHAT" ]] || BRIEF_WHY_WHAT="unread"
  fi
  case "$BRIEF_WHY_WHAT" in
    ok) ;;
    missing:*)
      echo "[dispatch] advisory (DGN-1751): brief has no ${BRIEF_WHY_WHAT#missing:} line -- pin \"Why: <owner intent>\" and \"What: <artifact + how the owner judges it>\" before build (not refused; recorded as brief_why_what=$BRIEF_WHY_WHAT; docs/DISPATCH-BRIEF-CONTRACT.md)" >&2
      ;;
  esac
  return 0
}

brief_unit_gate_check() {
  local lib="$SCRIPT_DIR/lib/done_blocks.py" line=""
  BRIEF_UNIT_GATE="unread"
  if [[ -f "$lib" && -f "${TASK_FILE:-}" ]]; then
    line="$(python3 "$lib" brief "$TASK_FILE" 2>/dev/null | head -n 1)" || line=""
  fi
  if [[ -n "$line" ]]; then
    BRIEF_UNIT_GATE="${line%%$'\t'*}"; line="${line#*$'\t'}"
    BRIEF_UNIT="${line%%$'\t'*}";      line="${line#*$'\t'}"
    BRIEF_GATE="$line"
    [[ -n "$BRIEF_UNIT_GATE" ]] || BRIEF_UNIT_GATE="unread"
  fi
  case "$BRIEF_UNIT_GATE" in
    missing:*)
      echo "[dispatch] advisory (DGN-2048): brief has no ${BRIEF_UNIT_GATE#missing:} line -- pin \"Unit of work: <the whole thing the user experiences>\" and \"E2E gate: <the end-to-end, user-screen check>\" before build (not refused; recorded as brief_unit_gate=$BRIEF_UNIT_GATE)" >&2
      ;;
  esac
  return 0
}

write_status_json() {
  JOB_RUNID="${RUNID:-}" \
  JOB_LABEL="${LABEL:-}" \
  JOB_DISPLAY_NAME="${DISPLAY_NAME:-}" \
  JOB_TICKET="${TICKET:-}" \
  JOB_DISPATCH_WHY="${DISPATCH_WHY:-}" \
  JOB_BRIEF_WHY_WHAT="${BRIEF_WHY_WHAT:-}" \
  JOB_BRIEF_WHY="${BRIEF_WHY:-}" \
  JOB_BRIEF_WHAT="${BRIEF_WHAT:-}" \
  JOB_BRIEF_UNIT_GATE="${BRIEF_UNIT_GATE:-}" \
  JOB_BRIEF_UNIT="${BRIEF_UNIT:-}" \
  JOB_BRIEF_GATE="${BRIEF_GATE:-}" \
  JOB_MODEL="${MODEL:-}" \
  JOB_MODEL_SOURCE="${MODEL_SOURCE:-}" \
  JOB_EFFORT="${EFFORT:-}" \
  JOB_EFFORT_SOURCE="${EFFORT_SOURCE:-}" \
  JOB_RUNTIME="${RUNTIME:-}" \
  JOB_REQUESTED_RUNTIME="${REQUESTED_RUNTIME:-}" \
  JOB_RUNTIME_SELECTION_REASON="${RUNTIME_SELECTION_REASON:-}" \
  JOB_WORK_CLASS="${WORK_CLASS:-}" \
  JOB_WORK_CLASS_DIFFICULTY="${WORK_CLASS_DIFFICULTY:-}" \
  JOB_WORK_CLASS_RECEIPT="${WORK_CLASS_RECEIPT:-}" \
  JOB_WORK_CLASS_REASON="${WORK_CLASS_REASON:-}" \
  JOB_MODEL_OBSERVED="${MODEL_OBSERVED_JSON:-}" \
  JOB_STARTED_AT="${STARTED_AT:-}" \
  JOB_TIMEOUT_MIN="${TIMEOUT_MIN:-}" \
  JOB_CANCEL_BY="${CANCEL_BY:-}" \
  JOB_SCRIPT_JOB="${SCRIPT_JOB:-}" \
  JOB_SCRIPT_USAGE="${SCRIPT_USAGE_JSON:-}" \
  JOB_SCRIPT_USAGE_FILE="${SCRIPT_USAGE_FILE:-}" \
  JOB_WORKDIR="${WORKDIR:-}" \
  JOB_PLAN_STATE="${PLAN_STATE:-}" \
  JOB_INBOX_STATE="${INBOX_STATE:-}" \
  JOB_ACCESS_MODE="${ACCESS_MODE:-}" \
  JOB_REQUESTED_NOTIFY="${REQUESTED_NOTIFY:-}" \
  python3 -B "$SCRIPT_DIR/lib/dispatch_report.py" status "$RUNDIR" "$1" "$2" "${3:-}"
}

write_session_inbox() {
  JOB_RUNID="${RUNID:-}" \
  JOB_LABEL="${LABEL:-}" \
  JOB_DISPLAY_NAME="${DISPLAY_NAME:-}" \
  JOB_TICKET="${TICKET:-}" \
  JOB_DISPATCH_WHY="${DISPATCH_WHY:-}" \
  JOB_BRIEF_WHY_WHAT="${BRIEF_WHY_WHAT:-}" \
  JOB_BRIEF_WHY="${BRIEF_WHY:-}" \
  JOB_BRIEF_WHAT="${BRIEF_WHAT:-}" \
  JOB_BRIEF_UNIT_GATE="${BRIEF_UNIT_GATE:-}" \
  JOB_BRIEF_UNIT="${BRIEF_UNIT:-}" \
  JOB_BRIEF_GATE="${BRIEF_GATE:-}" \
  JOB_MODEL="${MODEL:-}" \
  JOB_MODEL_SOURCE="${MODEL_SOURCE:-}" \
  JOB_EFFORT="${EFFORT:-}" \
  JOB_EFFORT_SOURCE="${EFFORT_SOURCE:-}" \
  JOB_RUNTIME="${RUNTIME:-}" \
  JOB_REQUESTED_RUNTIME="${REQUESTED_RUNTIME:-}" \
  JOB_RUNTIME_SELECTION_REASON="${RUNTIME_SELECTION_REASON:-}" \
  JOB_WORK_CLASS="${WORK_CLASS:-}" \
  JOB_WORK_CLASS_DIFFICULTY="${WORK_CLASS_DIFFICULTY:-}" \
  JOB_WORK_CLASS_RECEIPT="${WORK_CLASS_RECEIPT:-}" \
  JOB_WORK_CLASS_REASON="${WORK_CLASS_REASON:-}" \
  JOB_MODEL_OBSERVED="${MODEL_OBSERVED_JSON:-}" \
  JOB_STARTED_AT="${STARTED_AT:-}" \
  JOB_TIMEOUT_MIN="${TIMEOUT_MIN:-}" \
  JOB_CANCEL_BY="${CANCEL_BY:-}" \
  JOB_SCRIPT_JOB="${SCRIPT_JOB:-}" \
  JOB_SCRIPT_USAGE="${SCRIPT_USAGE_JSON:-}" \
  JOB_SCRIPT_USAGE_FILE="${SCRIPT_USAGE_FILE:-}" \
  JOB_WORKDIR="${WORKDIR:-}" \
  JOB_PLAN_STATE="${PLAN_STATE:-}" \
  JOB_INBOX_STATE="${INBOX_STATE:-}" \
  JOB_ACCESS_MODE="${ACCESS_MODE:-}" \
  JOB_REQUESTED_NOTIFY="${REQUESTED_NOTIFY:-}" \
  JOB_WORKSPACE="${WORKSPACE:-}" \
  JOB_RETURN_ROUTE_JSON="${RETURN_ROUTE_JSON:-}" \
  JOB_RETURN_RESERVATION="${RETURN_RESERVATION:-}" \
  JOB_SESSION_INBOX_DIR="${SESSION_INBOX_DIR:-}" \
  python3 -B "$SCRIPT_DIR/lib/dispatch_report.py" inbox "$RUNDIR" "$1" "$2" "${3:-}"
}

observe_model_from_stream() {
  local out=""
  out="$(python3 "$SCRIPT_DIR/lib/model_display.py" --observe-stream "$1" 2>/dev/null)" || out=""
  [[ "$out" == "["* ]] || out='[]'
  printf '%s\n' "$out"
}


refresh_cancel_by() {

  if [[ -f "$RUNDIR/cancel-by" ]] && [[ "$(cat "$RUNDIR/cancel-by" 2>/dev/null)" == "self" ]]; then
    CANCEL_BY="self"
  fi
}

record_self_cancel() {

  local cancel_marker="$RUNDIR/.cancel-by.tmp"
  printf 'self\n' > "$cancel_marker"
  mv -f "$cancel_marker" "$RUNDIR/cancel-by"
  CANCEL_BY="self"
  run_env_write CANCEL_BY "$CANCEL_BY" >> "$RUNDIR/run.env"
}

REPLACE_VERDICT="" REPLACE_TO="" REPLACE_RUNTIME="" REPLACE_NOTE=""

dispatch_finalize_hook() {
  if ! python3 -B - "$SCRIPT_DIR/lib" "$WORKSPACE" "$RUNDIR/worker.log" \
    "${RUNID:-}" "${LABEL:-}" "${TICKET:-}" "$1" "${BRANCH:-}" <<'PYHOOK' 2>/dev/null
import os
import signal
import subprocess
import sys

lib, workspace, log, *args = sys.argv[1:]
sys.path.insert(0, lib)
from conf_reader import conf_get

hook = conf_get("DISPATCH_FINALIZE_HOOK", os.path.join(workspace, "config/agent.conf"))
if not hook:
    sys.exit(0)
if not os.path.isabs(hook):
    hook = os.path.join(workspace, hook)
if os.fork():
    sys.exit(0)

# Same detach mechanism as the dispatch worker; no inherited pipe may keep
# the finalizer or its caller alive while the hook runs.
try:
    os.setsid()
    with open(os.devnull, "r+b", buffering=0) as null:
        for fd in (0, 1, 2):
            os.dup2(null.fileno(), fd)
    os.closerange(3, max(256, os.sysconf("SC_OPEN_MAX")))
    child = subprocess.Popen([hook, *args], cwd=workspace, start_new_session=True)
    try:
        rc = child.wait(timeout=30)
        failure = "exit={}".format(rc) if rc else ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        failure = "timeout=30s"
except Exception as exc:
    failure = type(exc).__name__
if failure:
    try:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write("[dispatch] finalize hook failed: {}\n".format(failure))
    except OSError:
        pass
os._exit(0)
PYHOOK
  then
    printf '%s\n' '[dispatch] finalize hook failed: launcher error' >> "$RUNDIR/worker.log" || true
  fi
}

dispatch_notice_state() {
  python3 -B -c '
import json, sys
try:
    value = json.loads(sys.stdin.read())
except (ValueError, TypeError):
    value = {}
print("queued" if isinstance(value, dict) and value.get("accepted") is True and value.get("delivered") is False else "sent")
'
}

worker_scratch_prepare() {
  [[ ! -L "$RUNDIR/tmp" ]] || return 1
  mkdir -p "$RUNDIR/tmp" || return 1
  TMPDIR="$(cd "$RUNDIR/tmp" && pwd -P)" || return 1
  printf 'dogany-test-scratch-v1\n' > "$TMPDIR/.dogany-test-scratch" || return 1
  printf '%s\n' "$$" > "$TMPDIR/owner.pid" || return 1
  DOGANY_RUN_SCRATCH="$TMPDIR"
  
  DOGANY_TEST_SCRATCH="$DOGANY_RUN_SCRATCH/tests"
  export TMPDIR DOGANY_RUN_SCRATCH DOGANY_TEST_SCRATCH
}

dispatch_storage_sweep() {
  python3 -B "$SCRIPT_DIR/lib/dispatch_storage.py" sweep --workspace "$WORKSPACE" --apply
}

dispatch_disk_preflight() {
  python3 -B "$SCRIPT_DIR/lib/dispatch_storage.py" disk-check --workspace "$WORKSPACE" --path "$WORKSPACE"
}

worker_scratch_finalize() {
  python3 -B "$SCRIPT_DIR/lib/dispatch_storage.py" retire --rundir "$RUNDIR" --result "$1"
}

finalize_run() {
  
  local result="$1" exit_code="$2"
  [[ ! -f "$RUNDIR/.finalized/complete" ]] || return 0
  mkdir "$RUNDIR/.finalizing" 2>/dev/null || return 0

  worker_browser_teardown   
  refresh_cancel_by

  if [[ "$RUNTIME" == "script" ]]; then
    MODEL_OBSERVED_JSON='[]'   
  elif [[ "$RUNTIME" == "codex" ]]; then
    local _codex_observed_slug=""
    _codex_observed_slug="$(codex_slug_for_model "$MODEL" 2>/dev/null)" || _codex_observed_slug=""
    if [[ -n "$_codex_observed_slug" ]]; then
      MODEL_OBSERVED_JSON="$(python3 -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$_codex_observed_slug")"
    else
      MODEL_OBSERVED_JSON='[]'
    fi
  else
    MODEL_OBSERVED_JSON="$(observe_model_from_stream "$RUNDIR/stream.jsonl" 2>/dev/null)"
  fi
  [[ -n "$MODEL_OBSERVED_JSON" ]] || MODEL_OBSERVED_JSON='[]'
  local notify_state="pending"
  [[ "$NOTIFY" == "silent" ]] && notify_state="skipped-silent"
  
  SCRIPT_USAGE_JSON=""
  if [[ "${SCRIPT_JOB:-0}" == 1 ]]; then
    SCRIPT_USAGE_JSON="$(python3 -B "$SCRIPT_DIR/lib/script_usage.py" summarize "${SCRIPT_USAGE_FILE:-$RUNDIR/usage.jsonl}" 2>/dev/null)" || SCRIPT_USAGE_JSON=""
  fi
  write_status_json "$result" "$exit_code" "$notify_state" || true

  active_count_decr || true
  
  local end_ts
  end_ts="$(date '+%Y-%m-%d %H:%M %Z')"
  
  local fb_sfx=""
  [[ -n "${MODEL_FALLBACK_FROM:-}" ]] && fb_sfx=" model_fallback=$MODEL_FALLBACK_FROM->$MODEL"
  case "$result" in
    completed)
      if [[ "${SCRIPT_JOB:-0}" == 1 ]]; then
        "$LEDGER_UPDATE" "$LEDGER_BRANCH" deferred \
          "script job 완료 (exit=0, out.log)$fb_sfx" --flag dsp-report --ts "$end_ts" || true
      else
        "$LEDGER_UPDATE" "$LEDGER_BRANCH" deferred \
          "dispatch 보고서형 완료 (out.log)$fb_sfx" --flag dsp-report --ts "$end_ts" || true
      fi
      ;;
    incomplete)
      local _incomplete_note="dispatch 미완 (대기 문구로 종료, 최종 보고 없음 -- DGN-1916)"
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed \
        "$_incomplete_note$fb_sfx" --ts "$end_ts" || true
      ;;
    timed-out)
      local _to_note="dispatch timeout (${TIMEOUT_MIN}m 초과)"
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed "$_to_note$fb_sfx" --ts "$end_ts" || true
      ;;
    canceled)
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed \
        "dispatch canceled$fb_sfx" --ts "$end_ts" || true
      ;;
    *)
      local _fail_note="dispatch 실패 (exit=$exit_code)"
      [[ "${SCRIPT_JOB:-0}" == 1 ]] && _fail_note="script job 실패 (exit=$exit_code)"

      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed \
        "$_fail_note$fb_sfx" --ts "$end_ts" || true
      ;;
  esac

  INBOX_STATE="dropped"
  if write_session_inbox "$result" "$exit_code"; then

    local _tsl_state="failed"
    [[ "$result" == "completed" ]] && _tsl_state="done"
    local _return_evidence="$RUNDIR/out.log"
    [[ -z "${RETURN_ROUTE_JSON:-}" ]] || _return_evidence="$RUNDIR/return.event.json"
    python3 "$SCRIPT_DIR/terminal-state-ledger.py" close --id "$RUNID" \
      --state "$_tsl_state" --note "dispatch $result (exit=$exit_code)" \
      --evidence "$_return_evidence" >/dev/null || true
  else
    INBOX_STATE="failed"
  fi
  write_status_json "$result" "$exit_code" "$notify_state" || true

  if [[ "$NOTIFY" != "silent" ]]; then

    resolve_owner_lang
    local push_name push_text
    push_name="$(owner_facing_name)"
    
    local end_elapsed=""
    end_elapsed="$(elapsed_seconds_since_start)" || end_elapsed=""
    push_text="$(end_notice_text "$result" "$push_name" "$(date +%H:%M)" "$end_elapsed")"
    local push_output="" delivery_state=""
    if [[ "$result" == canceled && "$CANCEL_BY" == self ]]; then
      write_status_json "$result" "$exit_code" "queued-stop" || true
    fi
    if [[ "$result" == canceled && "$CANCEL_BY" == self ]] && defer_stop_notice "$push_text"; then
      : 
    elif push_output="$("$PUSH_SH" --framework-notice --notice-id "dispatch:${RUNID:-$RUNDIR}:end" --text "$push_text" --audience owner)"; then
      [[ -z "$push_output" ]] || printf '%s\n' "$push_output"
      delivery_state="$(printf '%s' "$push_output" | dispatch_notice_state)"
      write_status_json "$result" "$exit_code" "$delivery_state" || true
    else
      [[ -z "$push_output" ]] || printf '%s\n' "$push_output"
      write_status_json "$result" "$exit_code" "notify-failed" || true
    fi
  fi

  dispatch_finalize_hook "$result" || true
  worker_scratch_finalize "$result" || true
  touch "$RUNDIR/.finalizing/complete" || return 1
  mv "$RUNDIR/.finalizing" "$RUNDIR/.finalized" || return 1
  dispatch_storage_sweep || echo "[dispatch] post-finalize reclaim sweep incomplete" >&2
  return 0
}

out_log_waiting_tail() {
  [[ -s "$1" ]] || return 1
  python3 - "$1" <<'PY_WAIT'
import re, sys
try:
    text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except OSError:
    sys.exit(1)
text = text.split("\n--- stderr ---\n", 1)[0]
lines = [l.strip() for l in text.splitlines() if l.strip()]
if not lines:
    sys.exit(1)
last = lines[-1]
PATTERNS = (
    r"\bwaiting (for|on|until)\b",
    r"\b(i'll|i will|i'm going to|we'll|will) (be |get )?notified\b",
    r"\bnotified (when|once)\b",
    r"\b(let me|i'll|i will) (wait|check back|report back|follow up)\b",
    r"\b(running|started|launched|kicked off|continues?|left)\b.*\bin the background\b",
    r"\b(still|now) running\b.*\b(when|once|until)\b",
    r"\b(once|when|after) (it|that|this|they|those|the [\w-]+( [\w-]+)?) "
    r"(finish|finishes|completes?|is done|are done|returns?|lands?)\b.*"
    r"\b(i'll|i will|will|then)\b",
    r"기다리(는|고|겠|며|면서|는 중)",
    r"대기 ?중",
    r"(끝나|완료되|종료되)면 .*(알려|보고|확인|이어)",
    r"알림이 (오면|올 때)",
    r"백그라운드(에서|로) .*(실행|돌)",
)
if any(re.search(p, last, re.IGNORECASE) for p in PATTERNS):
    print(last)
    sys.exit(0)
sys.exit(1)
PY_WAIT
}

render_out_log() {
  if [[ "$RUNTIME" == codex ]]; then render_out_log_codex; else render_out_log_claude; fi
}

render_out_log_claude() {
  local stream="$RUNDIR/stream.jsonl" err="$RUNDIR/claude.err" out="$RUNDIR/out.log"
  [[ -f "$stream" ]] || { : > "$out"; [[ -s "$err" ]] && cat "$err" >> "$out"; return 0; }
  if ! RO_STREAM="$stream" RO_OUT="$out" python3 - <<'PY_RENDER'
import json, os, sys
src, dst = os.environ["RO_STREAM"], os.environ["RO_OUT"]
parts, seen_any = [], False
with open(src, "r", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except Exception:
            continue
        if doc.get("type") != "assistant":
            continue
        msg = doc.get("message")
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                if text.strip():
                    parts.append(text)
                    seen_any = True
if not seen_any:
    sys.exit(3)
with open(dst, "w") as fh:
    fh.write("\n\n".join(parts).rstrip() + "\n")
PY_RENDER
  then
    
    {
      echo "[dispatch] out.log render found no assistant text -- raw stream follows (DGN-1192 fallback)"
      cat "$stream"
      [[ -s "$err" ]] && { echo; echo "--- stderr ---"; cat "$err"; }
    } > "$out"
    return 0
  fi
  if [[ -s "$err" ]]; then
    { echo; echo "--- stderr ---"; cat "$err"; } >> "$out"
  fi
  return 0
}

render_out_log_codex() {
  local stream="$RUNDIR/stream.jsonl" err="$RUNDIR/claude.err" out="$RUNDIR/out.log"
  [[ -f "$stream" ]] || { : > "$out"; [[ -s "$err" ]] && cat "$err" >> "$out"; return 0; }
  if ! RO_STREAM="$stream" RO_OUT="$out" python3 - <<'PY_RENDER'
import json, os, sys
src, dst = os.environ["RO_STREAM"], os.environ["RO_OUT"]
parts, seen_any = [], False
with open(src, "r", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except Exception:
            continue
        if doc.get("type") != "item.completed":
            continue
        item = doc.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text") or ""
        if text.strip():
            parts.append(text)
            seen_any = True
if not seen_any:
    sys.exit(3)
with open(dst, "w") as fh:
    fh.write("\n\n".join(parts).rstrip() + "\n")
PY_RENDER
  then
    {
      echo "[dispatch] out.log render found no agent_message text -- raw stream follows (DGN-1192 fallback)"
      cat "$stream"
      [[ -s "$err" ]] && { echo; echo "--- stderr ---"; cat "$err"; }
    } > "$out"
    return 0
  fi
  if [[ -s "$err" ]]; then
    { echo; echo "--- stderr ---"; cat "$err"; } >> "$out"
  fi
  return 0
}

WORKER_ENV_STRIP_NAMES=(TELEGRAM_BOT_TOKEN ALLOWED_USER_IDS)

worker_env_strip_args() {
  local name
  WORKER_ENV_UNSET=()
  for name in "${WORKER_ENV_STRIP_NAMES[@]}"; do
    WORKER_ENV_UNSET+=(-u "$name")
  done
  while IFS= read -r name; do
    [[ "$name" == TELEGRAM_* ]] || continue
    [[ " ${WORKER_ENV_STRIP_NAMES[*]} " == *" $name "* ]] && continue
    WORKER_ENV_UNSET+=(-u "$name")
  done < <(compgen -e)
}

worker_browser_socket_dir() {
  printf '/tmp/dogany-ab-%s' "$RUNID"
}

worker_browser_env_args() {
  local dir
  dir="$(worker_browser_socket_dir)"
  WORKER_BROWSER_ENV=()
  mkdir -p -m 700 "$dir" 2>/dev/null || return 0
  WORKER_BROWSER_ENV=(
    "AGENT_BROWSER_SOCKET_DIR=$dir"
    "AGENT_BROWSER_ARGS=${AGENT_BROWSER_ARGS:+$AGENT_BROWSER_ARGS,}--no-sandbox"
  )
}

worker_browser_teardown() {
  [[ -n "${RUNID:-}" ]] || return 0
  local dir
  dir="$(worker_browser_socket_dir)"
  [[ -d "$dir" ]] || return 0
  if ls "$dir"/*.sock >/dev/null 2>&1 && command -v agent-browser >/dev/null 2>&1; then
    local t=()
    command -v timeout >/dev/null 2>&1 && t=(timeout 30s)
    AGENT_BROWSER_SOCKET_DIR="$dir" ${t[@]+"${t[@]}"} agent-browser close --all >/dev/null 2>&1 || true
  fi
  rm -rf "$dir" 2>/dev/null || true
}

run_claude_with_deadline() {
  local secs="$1"; shift
  local timeout_bin=""
  if command -v timeout >/dev/null 2>&1; then
    timeout_bin="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then
    timeout_bin="gtimeout"
  fi

  local rc=0
  if [[ -n "$timeout_bin" ]]; then
    ( cd "$WORKDIR" && "$timeout_bin" "${secs}s" "$@" ) \
      < /dev/null > "$RUNDIR/stream.jsonl" 2> "$RUNDIR/claude.err" || rc=$?
    return $rc
  fi

  set -m
  ( cd "$WORKDIR" && "$@" ) < /dev/null > "$RUNDIR/stream.jsonl" 2> "$RUNDIR/claude.err" &
  local cpid=$!
  set +m
  printf '%s\n' "$cpid" > "$RUNDIR/claude.pid"

  local waited=0
  while kill -0 "$cpid" 2>/dev/null; do
    if [[ $waited -ge $secs ]]; then
      kill -TERM -- "-$cpid" 2>/dev/null || true
      sleep 5
      kill -KILL -- "-$cpid" 2>/dev/null || true
      wait "$cpid" 2>/dev/null || true
      rm -f "$RUNDIR/claude.pid"
      return 124
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  wait "$cpid" || rc=$?
  rm -f "$RUNDIR/claude.pid"
  return $rc
}

do_worker_script() {
  local argv=() a rc=0 cpid deadline
  while IFS= read -r -d '' a; do argv+=("$a"); done < "$RUNDIR/job.argv"
  worker_env_strip_args
  : >> "$RUNDIR/out.log"
  [[ -n "${SCRIPT_USAGE_FILE:-}" ]] || SCRIPT_USAGE_FILE="$RUNDIR/usage.jsonl"
  deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
  set -m
  ( cd "$WORKDIR" && exec env -u CLAUDECODE -u PROJECT_ROOT -u DOGANY_BRIDGE_SESSION \
      ${WORKER_ENV_UNSET[@]+"${WORKER_ENV_UNSET[@]}"} \
      DOGANY_DISPATCH_WORKER="$RUNID" DOGANY_SCRIPT_JOB=1 DOGANY_SCRIPT_RUNID="$RUNID" \
      DOGANY_SCRIPT_RUNDIR="$RUNDIR" DOGANY_SCRIPT_USAGE_FILE="$SCRIPT_USAGE_FILE" \
      "${argv[@]}" ) < /dev/null >> "$RUNDIR/out.log" 2>&1 &
  cpid=$!
  set +m
  printf '%s\n' "$cpid" > "$RUNDIR/claude.pid"
  while kill -0 "$cpid" 2>/dev/null; do
    if [[ $(date +%s) -ge $deadline ]]; then
      kill -TERM -- "-$cpid" 2>/dev/null || true
      sleep 5
      kill -KILL -- "-$cpid" 2>/dev/null || true
      wait "$cpid" 2>/dev/null || true
      rc=124
      break
    fi
    sleep 1
  done
  [[ $rc -eq 124 ]] || wait "$cpid" || rc=$?
  rm -f "$RUNDIR/claude.pid"
  trap - TERM
  if [[ $rc -eq 0 ]]; then finalize_run completed 0
  elif [[ $rc -eq 124 ]]; then finalize_run timed-out 124
  else finalize_run error "$rc"
  fi
  exit 0
}

worker_on_exit() {
  if [[ -n "${RUNDIR:-}" && -d "$RUNDIR" && ! -d "$RUNDIR/.finalized" ]]; then
    if [[ "${RUNTIME:-}" == "claude-api" && "${API_POOL_IN_ATTEMPT:-0}" == 1 ]]; then
      api_pool_record worker-exit || true
    fi
    finalize_run error 1 || true
  fi

  worker_scratch_finalize error || true
}

worker_on_term() {

  if [[ -f "$RUNDIR/claude.pid" ]]; then
    local cpid
    cpid="$(cat "$RUNDIR/claude.pid" 2>/dev/null || echo '')"
    if [[ -n "$cpid" ]]; then
      kill -TERM -- "-$cpid" 2>/dev/null || true
      sleep 2
      kill -KILL -- "-$cpid" 2>/dev/null || true
    fi
  fi

  [[ "${SCRIPT_JOB:-0}" == 1 ]] || render_out_log || true
  
  if [[ "${RUNTIME:-}" == "claude-api" && "${API_POOL_IN_ATTEMPT:-0}" == 1 ]]; then
    api_pool_record canceled || true
  fi
  finalize_run canceled 143
  exit 143
}

do_worker() {
  RUNDIR="$WORKER_RUNDIR"
  if [[ ! -d "$RUNDIR" || ! -f "$RUNDIR/run.env" ]]; then
    echo "[dispatch-worker] bad rundir: $RUNDIR" >&2
    exit 1
  fi
  
  source "$RUNDIR/run.env"
  restore_legacy_return_route
  trap worker_on_exit EXIT
  trap worker_on_term TERM INT
  runtime_selection_consistency worker || exit 2
  worker_scratch_prepare || exit 1

  printf '%s\n' "$$" > "$RUNDIR/pid"
  
  ps -o lstart=,command= -p "$$" > "$RUNDIR/proc.txt" 2>/dev/null || true
  STARTED_AT="$(date +%Y-%m-%dT%H:%M:%S%z)"

  if [[ "${SCRIPT_JOB:-0}" == 1 ]]; then
    do_worker_script   
  fi

  local prompt backstop
  prompt="$(cat "$RUNDIR/prompt.md")"
  backstop="$(worker_backstop)"

  local claude_rc=0

  worker_env_strip_args
  worker_browser_env_args
  if [[ "$RUNTIME" == "codex" ]]; then

    if ! select_codex_tool_policy; then
      finalize_run error 2
      exit 2
    fi
    local codex_slug
    if ! codex_slug="$(codex_slug_for_model "$MODEL")"; then
      echo "[dispatch-worker] no codex slug mapping for model '$MODEL' -- refusing Accepted path: restore the canonical model-display slug mapping for $MODEL at $MODEL_DISPLAY_BINDINGS, then retry the original request." >&2
      finalize_run error 1
      exit 1
    fi
    
    local codex_effort_args=()
    if [[ -n "$EFFORT" ]] && effort_passed_to_runtime codex "$EFFORT"; then
      codex_effort_args=(-c "model_reasoning_effort=$EFFORT")
    fi

    local codex_prompt
    codex_prompt="$(printf '%s\n\n---\n\n%s' "$backstop" "$prompt")"
    run_claude_with_deadline "$(( TIMEOUT_MIN * 60 ))" \
      ${WRAP[@]+"${WRAP[@]}"} env -u CLAUDECODE -u PROJECT_ROOT -u DOGANY_BRIDGE_SESSION \
        ${WORKER_ENV_UNSET[@]+"${WORKER_ENV_UNSET[@]}"} DOGANY_DISPATCH_WORKER="$RUNID" \
         \
        ${WORKER_BROWSER_ENV[@]+"${WORKER_BROWSER_ENV[@]}"} \
         DOGANY_CODEX_ADAPTER_ROOT="$CODEX_ADAPTER_ROOT" codex exec \
        -m "$codex_slug" \
        ${codex_effort_args[@]+"${codex_effort_args[@]}"} \
        --json \
        "${CODEX_ARGS[@]}" \
        "$codex_prompt" \
      || claude_rc=$?
  else

    if ! select_tool_policy; then
      finalize_run error 2
      exit 2
    fi

    local attempt_prompt="$prompt" fb_tried="$MODEL" fb_n=1 fb_reason fb_next
    local deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 )) secs_left i

    local api_stream_args=()

    API_POOL_ATTEMPT=0
    while :; do
      claude_rc=0
      API_POOL_ATTEMPT=$(( API_POOL_ATTEMPT + 1 ))
      API_POOL_IN_ATTEMPT=1
      secs_left=$(( deadline - $(date +%s) ))
      [[ $secs_left -ge 60 ]] || secs_left=60
      run_claude_with_deadline "$secs_left" \
        ${WRAP[@]+"${WRAP[@]}"} env -u CLAUDECODE -u PROJECT_ROOT -u DOGANY_BRIDGE_SESSION \
          ${WORKER_ENV_UNSET[@]+"${WORKER_ENV_UNSET[@]}"} DOGANY_DISPATCH_WORKER="$RUNID" \
           \
          ${WORKER_BROWSER_ENV[@]+"${WORKER_BROWSER_ENV[@]}"}  claude -p "$attempt_prompt" \
          --model "$MODEL" \
          ${EFFORT:+--effort "$EFFORT"} \
          --append-system-prompt "$backstop" \
          --output-format stream-json --verbose \
          ${api_stream_args[@]+"${api_stream_args[@]}"} \
          "${TOOL_ARGS[@]}" \
        || claude_rc=$?

      API_POOL_IN_ATTEMPT=0
      [[ $claude_rc -ne 0 && $claude_rc -ne 124 ]] || break
      fb_reason="$(overload_fallback_reason)" || break
      if ! fb_next="$(overload_fallback_next "$fb_tried")"; then
        overload_fallback_record "$fb_n" "$fb_reason" "" ""
        break
      fi
      overload_fallback_record "$fb_n" "$fb_reason" "${fb_next%% *}" "${fb_next#* }"
      fb_tried="$fb_tried $MODEL"
      fb_n=$(( fb_n + 1 ))
      if [[ -n "$SESSION_ID" ]] && session_transcript_exists "$WORKDIR" "$SESSION_ID"; then
        for i in "${!TOOL_ARGS[@]}"; do
          [[ "${TOOL_ARGS[$i]}" == --session-id ]] && TOOL_ARGS[$i]="--resume"
        done
        attempt_prompt="The previous attempt at the task in the first message of this session died on API 529 (overloaded) before any tool call, so nothing has been done yet. The model was switched ($MODEL_FALLBACK_NOTE). Carry out that task now, from the start."
      fi
    done

  fi

  render_out_log

  trap - TERM

  local waiting_line=""
  if [[ $claude_rc -eq 124 ]]; then
    finalize_run timed-out 124
  elif [[ $claude_rc -eq 0 ]] && waiting_line="$(out_log_waiting_tail "$RUNDIR/out.log")"; then

    echo "[dispatch-worker] DGN-1916: run ended on a waiting line, no final report -> incomplete: $waiting_line"
    printf '\n[dispatch] DGN-1916: incomplete -- the run exited 0 on a waiting line, with no final report (a backgrounded task'"'"'s notice never re-enters a detached run).\n' >> "$RUNDIR/out.log"
    finalize_run incomplete 0
  elif [[ $claude_rc -eq 0 ]]; then
    finalize_run completed 0
  else

    if is_fable_credits_death "$RUNDIR/out.log"; then
      echo "[dispatch-worker] fable weekly cap exhausted (out.log: requires usage credits) -> recording $FABLE_FLAG so the next fable spawn demotes (DGN-1158)"
      write_fable_exhausted_marker || echo "[dispatch-worker] failed to write $FABLE_FLAG (non-fatal)" >&2
    fi

    finalize_run error "$claude_rc"
  fi
}

run_tree_members() {
  local seeds="$1" wt="${2:-}"
  python3 - "$seeds" "$wt" <<'PY'
import os, subprocess, sys
seeds = {int(x) for x in sys.argv[1].split(",") if x.strip().isdigit()}
wt = os.path.realpath(sys.argv[2]) if sys.argv[2] else ""
rows = {}
out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,state=,command="], capture_output=True, text=True).stdout
for ln in out.splitlines():
    f = ln.split(None, 4)
    if len(f) >= 3 and all(x.isdigit() for x in f[:3]):
        if len(f) > 3 and f[3].startswith("Z"):
            continue
        rows[int(f[0])] = (int(f[1]), int(f[2]), f[4] if len(f) > 4 else "")
safe = set(); q = os.getpid()
while q and q not in safe:
    safe.add(q); q = rows.get(q, (0,))[0]
members = {p for p in seeds if p in rows}
members |= {p for p, (pp, pg, _) in rows.items() if pg in seeds and pg > 1}
if wt and wt not in ("/", "/tmp", "/private/tmp"):
    r = subprocess.run(["lsof", "-d", "cwd", "-Fpn"], capture_output=True, text=True)
    cur = None
    for ln in r.stdout.splitlines():
        if ln.startswith("p"):
            cur = int(ln[1:]) if ln[1:].isdigit() else None
        elif ln.startswith("n") and cur and (ln[1:] == wt or ln[1:].startswith(wt + "/")):
            if cur in rows:
                members.add(cur)
changed = True
while changed:
    changed = False
    for p, (pp, pg, _) in rows.items():
        if p not in members and pp in members and pp > 1:
            members.add(p); changed = True
for p in sorted(members - safe):
    print(f"{p}\t{rows[p][2][:120]}")
PY
}

run_tree_reap() {
  local seeds="$1" wt="${2:-}" snap pids i
  snap="$(run_tree_members "$seeds" "$wt")"
  pids="$(printf '%s\n' "$snap" | cut -f1 | grep -E '^[0-9]+$' | paste -sd, -)"
  [[ -z "$pids" ]] && return 0
  seeds="$seeds,$pids"

  kill -CONT ${pids//,/ } 2>/dev/null || true
  local sig
  for sig in TERM KILL; do
    snap="$(run_tree_members "$seeds" "$wt")"
    pids="$(printf '%s\n' "$snap" | cut -f1 | grep -E '^[0-9]+$' | paste -sd, -)"
    [[ -z "$pids" ]] && return 0
    seeds="$seeds,$pids"
    
    kill -"$sig" ${pids//,/ } 2>/dev/null || true
    for (( i = 0; i < 10; i++ )); do
      sleep 0.5
      [[ -z "$(run_tree_members "$seeds" "$wt")" ]] && return 0
    done
  done
  echo "[dispatch] survivors still alive after KILL:" >&2
  run_tree_members "$seeds" "$wt" | sed 's/^/  pid /' >&2
  return 1
}

do_watchdog() {
  RUNDIR="$WORKER_RUNDIR"
  [[ -d "$RUNDIR" && -f "$RUNDIR/run.env" ]] || exit 1
  
  source "$RUNDIR/run.env"
  restore_legacy_return_route
  local started deadline now stopped_since=0 stopped_limit poll worker seeds snap states
  started="$(python3 -c 'import os,sys; print(int(os.stat(sys.argv[1]).st_mtime))' "$RUNDIR/run.env" 2>/dev/null || date +%s)"

  local grace
  grace="${DOGANY_DISPATCH_WATCHDOG_GRACE_SECS:-1800}"
  [[ "$grace" =~ ^[0-9]+$ ]] || grace=1800
  deadline=$(( started + TIMEOUT_MIN * 60 + grace ))
  stopped_limit="${DOGANY_DISPATCH_STOPPED_SECS:-300}"
  poll="${DOGANY_DISPATCH_WATCHDOG_POLL_SECS:-2}"
  [[ "$stopped_limit" =~ ^[0-9]+$ && "$stopped_limit" -gt 0 ]] || stopped_limit=300
  [[ "$poll" =~ ^[0-9]+$ && "$poll" -gt 0 ]] || poll=2
  while [[ ! -f "$RUNDIR/.finalized/complete" ]]; do
    worker="$(cat "$RUNDIR/pid" 2>/dev/null || true)"
    seeds="$worker"
    [[ ! -f "$RUNDIR/claude.pid" ]] || seeds="$seeds,$(cat "$RUNDIR/claude.pid" 2>/dev/null || true)"
    snap="$(run_tree_members "$seeds" "" 2>/dev/null || true)"
    states=""
    if [[ -n "$snap" ]]; then
      states="$(printf '%s\n' "$snap" | cut -f1 | while read -r p; do ps -o state= -p "$p" 2>/dev/null; done)"
    fi
    now="$(date +%s)"
    if printf '%s\n' "$states" | grep -q '^[[:space:]]*T'; then
      [[ "$stopped_since" -ne 0 ]] || stopped_since="$now"
    else
      stopped_since=0
    fi
    if [[ "$now" -ge "$deadline" || ( "$stopped_since" -ne 0 && $(( now - stopped_since )) -ge "$stopped_limit" ) ]]; then
      local why="deadline"
      [[ "$now" -ge "$deadline" ]] || why="stopped-process>${stopped_limit}s"
      printf '[dispatch-watchdog] timeout: %s\n' "$why" >> "$RUNDIR/out.log"
      if ! run_tree_reap "$seeds" ""; then
        
        printf '[dispatch-watchdog] reap incomplete; retrying\n' >> "$RUNDIR/out.log"
        sleep "$poll"
        continue
      fi
      
      rm -rf "$RUNDIR/.finalizing"
      finalize_run timed-out 124
      exit 0
    fi
    sleep "$poll"
  done
}

do_cancel() {
  RUNDIR="$DISPATCH_DIR/$CANCEL_RUNID"
  if [[ ! -d "$RUNDIR" || ! -f "$RUNDIR/run.env" ]]; then
    echo "[dispatch] unknown runid: $CANCEL_RUNID" >&2
    exit 1
  fi
  
  source "$RUNDIR/run.env"
  restore_legacy_return_route
  local self_cancel=0
  [[ "${DOGANY_BRIDGE_SESSION:-}" == "1" ]] && self_cancel=1

  if [[ -f "$RUNDIR/.finalized/complete" ]]; then
    echo "[dispatch] $CANCEL_RUNID already finalized -- nothing to cancel"
    exit 0
  fi

  local pid=""
  [[ -f "$RUNDIR/pid" ]] && pid="$(cat "$RUNDIR/pid" 2>/dev/null | tr -d ' \n')"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    [[ "$self_cancel" -eq 1 ]] && record_self_cancel
    echo "[dispatch] worker not running (pid=${pid:-none}) -- finalizing bookkeeping only"
    local orphan_seeds=""
    [[ -f "$RUNDIR/claude.pid" ]] && orphan_seeds="$(tr -d ' \n' < "$RUNDIR/claude.pid" 2>/dev/null)"
    if ! run_tree_reap "$orphan_seeds" ""; then
      finalize_run canceled 1
      echo "[dispatch] NOT fully canceled: $CANCEL_RUNID has surviving processes (listed above)" >&2
      exit 1
    fi

    rm -rf "$RUNDIR/.finalizing"
    [[ -f "$RUNDIR/.finalized/complete" ]] || rm -rf "$RUNDIR/.finalized"
    finalize_run canceled 1
    exit 0
  fi

  local recorded current
  recorded="$(cat "$RUNDIR/proc.txt" 2>/dev/null | tr -s ' ' | sed 's/^ *//;s/ *$//')"
  current="$(ps -o lstart=,command= -p "$pid" 2>/dev/null | tr -s ' ' | sed 's/^ *//;s/ *$//')"
  if [[ -z "$recorded" || -z "$current" || "$recorded" != "$current" ]]; then
    echo "[dispatch] REFUSED: pid $pid identity mismatch (reused pid?) Accepted path: inspect dispatch-detached.sh --list for the current run before cancelling; leave the mismatching PID untouched." >&2
    echo "  recorded: ${recorded:-<none>}" >&2
    echo "  current:  ${current:-<none>}" >&2
    exit 1
  fi

  [[ "$self_cancel" -eq 1 ]] && record_self_cancel

  local seeds="$pid" cpid=""
  [[ -f "$RUNDIR/claude.pid" ]] && cpid="$(tr -d ' \n' < "$RUNDIR/claude.pid" 2>/dev/null)"
  [[ -n "$cpid" ]] && seeds="$seeds,$cpid"
  local pre
  pre="$(run_tree_members "$seeds" "" | cut -f1 | paste -sd, -)"
  [[ -n "$pre" ]] && seeds="$seeds,$pre"
  echo "[dispatch] cancel $CANCEL_RUNID: TERM to process group $pid and its process tree"
  kill -TERM -- "-$pid" 2>/dev/null || true

  local waited=0
  while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 5 ]]; do
    sleep 1
    waited=$(( waited + 1 ))
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "[dispatch] worker survived TERM -- escalating to KILL"
    kill -KILL -- "-$pid" 2>/dev/null || true
  fi
  local reap_rc=0
  run_tree_reap "$seeds" "" || reap_rc=1

  sleep 2
  if [[ ! -f "$RUNDIR/.finalized/complete" ]]; then
    rm -rf "$RUNDIR/.finalizing" "$RUNDIR/.finalized"
    finalize_run canceled 137
  fi
  if [[ $reap_rc -ne 0 ]]; then
    echo "[dispatch] NOT fully canceled: $CANCEL_RUNID has surviving processes (listed above)" >&2
    exit 1
  fi
  echo "[dispatch] canceled: $CANCEL_RUNID"
}

do_apply_plan() {

  if ! [[ "$CANCEL_RUNID" =~ ^dsp-[0-9]{8}-[0-9]{6}-([0-9]{4}|[0-9]+-[0-9]+)$ ]]; then
    echo "[dispatch] --apply-plan needs a runid (dsp-YYYYMMDD-HHMMSS-NNNN or dsp-YYYYMMDD-HHMMSS-PID-RANDOM), got '$CANCEL_RUNID'" >&2
    return 2
  fi
  local rundir="$DISPATCH_DIR/$CANCEL_RUNID" targets="" t args=()
  if [[ ! -f "$rundir/run.env" ]]; then
    echo "[dispatch] --apply-plan: no run $CANCEL_RUNID under $DISPATCH_DIR" >&2
    return 2
  fi
  targets="$( LIVE_TARGETS=""; source "$rundir/run.env" >/dev/null 2>&1; printf '%s' "$LIVE_TARGETS" )"
  if [[ -z "$targets" ]]; then
    echo "[dispatch] --apply-plan: $CANCEL_RUNID is not a plan-lane run (spawned without --live-target)" >&2
    return 2
  fi
  while IFS= read -r t; do [[ -n "$t" ]] && args+=(--target "$t"); done <<<"$targets"
  
  [[ "$BACKFILL_APPLY" == 1 ]] && args+=(--apply)
  python3 "$SCRIPT_DIR/plan-apply.py" --rundir "$rundir" "${args[@]}"
}

do_backfill() {
  if [[ ! -d "$DISPATCH_DIR" ]]; then
    echo "[backfill] no dispatches yet"
    return 0
  fi
  local total=0 already=0 nostream=0 candidates=0 patched=0 failed=0
  local d runid status_file stream_file observed has_field
  for d in "$DISPATCH_DIR"/dsp-*/; do
    [[ -d "$d" ]] || continue
    status_file="${d}status.json"
    stream_file="${d}stream.jsonl"
    [[ -f "$status_file" ]] || continue
    total=$(( total + 1 ))
    runid="$(basename "$d")"
    has_field="$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print("1" if isinstance(d, dict) and "model_observed" in d else "0")
except Exception:
    print("0")
' "$status_file" 2>/dev/null || echo 0)"
    if [[ "$has_field" == "1" ]]; then
      already=$(( already + 1 ))
      continue
    fi
    if [[ ! -f "$stream_file" ]]; then
      nostream=$(( nostream + 1 ))
      continue
    fi
    candidates=$(( candidates + 1 ))
    observed="$(observe_model_from_stream "$stream_file" 2>/dev/null)"
    [[ -n "$observed" ]] || observed='[]'
    if [[ "$BACKFILL_APPLY" -eq 1 ]]; then
      if RB_OBSERVED="$observed" python3 - "$status_file" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
except Exception as e:
    print(f"[backfill] SKIP unreadable {path}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    observed = json.loads(os.environ.get("RB_OBSERVED", "[]") or "[]")
    if not isinstance(observed, list):
        observed = []
except Exception:
    observed = []
doc["model_observed"] = observed
doc["model_observed_backfilled"] = True
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
      then
        patched=$(( patched + 1 ))
        echo "[backfill] APPLIED $runid -> $observed"
      else
        failed=$(( failed + 1 ))
        echo "[backfill] FAILED  $runid" >&2
      fi
    else
      echo "[backfill] WOULD-APPLY $runid -> $observed"
    fi
  done
  echo "[backfill] total=$total already_has_field=$already no_stream_left=$nostream candidates=$candidates"
  if [[ "$BACKFILL_APPLY" -eq 1 ]]; then
    echo "[backfill] patched=$patched failed=$failed"
  else
    echo "[backfill] dry-run -- pass --apply to write model_observed into these $candidates run(s)"
  fi
}

do_list() {
  if [[ ! -d "$DISPATCH_DIR" ]]; then
    echo "[dispatch] no dispatches yet"
    return 0
  fi
  local found=0 d runid state pid label
  for d in "$DISPATCH_DIR"/dsp-*/; do
    [[ -d "$d" ]] || continue
    found=1
    runid="$(basename "$d")"
    label=""
    if [[ -f "$d/run.env" ]]; then
      
      label="$( ( source "$d/run.env" 2>/dev/null && printf '%s' "${LABEL:-}" ) || true )"
    fi
    if [[ -f "$d/status.json" ]]; then
      state="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("result","?"))' "$d/status.json" 2>/dev/null || echo '?')"
    else
      pid="$(cat "$d/pid" 2>/dev/null | tr -d ' \n')"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        state="running (pid $pid)"
      else
        state="dead-unfinalized"
      fi
    fi
    printf '%s  %-22s  %s\n' "$runid" "$state" "$label"
  done
  if [[ $found -eq 0 ]]; then
    echo "[dispatch] no dispatches yet"
  fi
  
  if host_dispatch_on; then
    python3 -B "$HOST_DISPATCHER_PY" list 2>/dev/null || echo "[dispatch] host dispatcher unavailable"
  fi
}

do_queue_probe() {
  validate_spawn_args || return 2
  select_default_runtime || return 2
  slot_candidate_gate "$RUNTIME" || return 2
  printf 'QUEUE-ROUTES\t[{"lane":"%s@%s","cap":%s}]\n' "$RUNTIME" "$(host_account "$RUNTIME")" "$HOST_CAP_VALUE"
}

main() {

  case "$MODE" in
    spawn|worker|apply-plan)
      if [[ -f "$INSTANCE_ROOT/config/agent.conf" ]]; then
        local why_intake_on
        why_intake_on="$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from conf_reader import conf_get; print(conf_get("WHY_NET", sys.argv[2]).lower())' \
          "$SCRIPT_DIR/lib" "$INSTANCE_ROOT/config/agent.conf")" || return 3
        if [[ "$why_intake_on" == on ]]; then
          python3 -B "$SCRIPT_DIR/why-frame-gate.py" --dispatch "$INSTANCE_ROOT" ${WHY_DISPATCH_ORIGINAL_ARGV[@]+"${WHY_DISPATCH_ORIGINAL_ARGV[@]}"} || return $?
        fi
      fi ;;
  esac
  case "$MODE" in
    spawn|list|cancel) dispatch_storage_sweep || echo "[dispatch] reclaim sweep incomplete; see retained paths above" >&2 ;;
  esac
  case "$MODE" in
    reclaim)     dispatch_storage_sweep ;;
    spawn)       if [[ "$SCRIPT_JOB" == 1 ]]; then do_spawn_script; else do_spawn; fi ;;
    queue-probe) do_queue_probe ;;
    cancel)      do_cancel ;;
    list)        do_list ;;
    worker)      do_worker ;;
    watchdog)    do_watchdog ;;
    backfill)    do_backfill ;;
    apply-plan)  do_apply_plan ;;
    start-notice-flush) do_start_notice_flush ;;
    bg-start-notice) do_bg_start_notice ;;
    bg-end-notice) do_bg_end_notice ;;
    host-cancel) do_host_cancel ;;
  esac
}

if [[ -z "${DISPATCH_TEST:-}" ]]; then
  main
fi
