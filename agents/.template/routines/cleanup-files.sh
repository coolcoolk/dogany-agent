#!/bin/bash
#
# cleanup-files.sh — 에이전트 파일 정리 루틴 (베이스라인)
#
# 동작:
#   1) files/tmp/: spec-signature files (*locked* *spec* *contract* *doctrine*)
#      -> quarantine to _archive/tmp-rescue/<YYYYMMDD>/ (DGN-935); remaining
#      scratch older than RETENTION_DAYS -> trash or _archive (.gitkeep 보존)
#   2) files/outbox/ 와 .telegram_bot/images/ 에서 mtime 7일 초과 파일을
#      trash(있으면) 또는 files/_archive 로 이동
#
#   2b) files/_archive/trash/<YYYYMMDD>/ (lib/trash.sh, the D3 rm rewrite
#      target) older than TRASH_RETENTION_DAYS (30) -> spec-signature files
#      quarantined to _archive/tmp-rescue/ first (same rule as 1), then the
#      dated dir goes to the platform trash, else is removed (v2.7 grill F16:
#      nothing else bounds that store)
#
#   3) product/auto-loop-ledger.md: already-merged `pending-review` rows -> merged
#      (routines/ledger-reconcile.sh, DGN-1730; write path = lib/ledger-update.sh)
#
#   5) files/docs/ + files/INDEX.md (DGN-117 / dec-535): lib/filing.py sweep --
#      INDEX self-heal against disk, pending rows 30 days past their ask ->
#      best-guess placement (state auto). Skipped while neither exists.
#
# 절대 손대지 않음: files/inbox/, data/, memories/
#
# Usage: cleanup-files.sh [--dry-run] [--residue-root ROOT ... --residue-trash TRASH]
#
# NOTE: BASE 는 스크립트 위치에서 자동 도출하므로 경로 치환 불필요 — 그대로 상속.
#
set -euo pipefail

# ---- BASE 는 스크립트 위치에서 자동 도출 (routines/ 의 부모) ----
# 절대경로 하드코딩은 디렉토리 이전 시 깨짐 — 자기 위치 기준이라 migration 안전.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="$BASE/files/tmp"
OUTBOX_DIR="$BASE/files/outbox"
# new-bridge layout (default for freshly minted agents)
IMAGES_DIR="$BASE/.telegram_bot/images"
ARCHIVE_DIR="$BASE/files/_archive"

RETENTION_DAYS=7
TRASH_RETENTION_DAYS="${TRASH_RETENTION_DAYS:-30}"

DRY_RUN=0
residue_roots=()
residue_trash=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --residue-root) residue_roots+=(--root "${2:?missing root}"); shift 2 ;;
        --residue-trash) residue_trash=(--trash "${2:?missing trash}"); shift 2 ;;
        *) echo "Unknown cleanup option: $1" >&2; exit 2 ;;
    esac
done
# Root overrides are for isolated rehearsals: never invoke the global host
# worktree reaper from one, and require an explicit isolated Trash as well.
if [[ ${#residue_roots[@]} -gt 0 && ${#residue_trash[@]} -eq 0 ]]; then
    echo "--residue-root requires --residue-trash" >&2
    exit 2
fi

log() { echo "[cleanup-files] $*"; }

# 경로 안전 가드: 변수가 비었거나 디렉토리가 아니면 해당 단계 건너뜀
guard_dir() {
    local d="$1"
    if [[ -z "$d" ]]; then
        log "SKIP: 빈 경로 변수 — 안전상 중단"
        return 1
    fi
    if [[ "$d" == "/" ]]; then
        log "SKIP: 루트 경로 금지 — 안전상 중단"
        return 1
    fi
    if [[ ! -d "$d" ]]; then
        log "SKIP: 디렉토리 없음 ($d)"
        return 1
    fi
    return 0
}

# ---- Signature patterns for locked/spec artifacts (DGN-935) ----
# Bash glob patterns applied to the lowercase filename (basename only).
# On match: quarantine to _archive/tmp-rescue/<YYYYMMDD>/ instead of deleting.
# Fail-safe direction: false-positive (scratch named like a spec) = rescued (harmless).
_RESCUE_DATE="$(date +%Y%m%d)"

_is_spec_signature() {
    # Returns 0 (true) if the basename matches any locked/spec pattern.
    local name
    name="$(basename "$1")"
    local lower   # macOS ships bash 3.2; use tr for lowercase (no ${x,,})
    lower="$(echo "$name" | tr '[:upper:]' '[:lower:]')"
    case "$lower" in
        *locked*|*-locked*|*spec*|*contract*|*doctrine*)
            return 0
            ;;
    esac
    return 1
}

# ---- 1) tmp 정리 (.gitkeep 보존) ----
# Pre-pass: quarantine spec-signature files to _archive/tmp-rescue/<date>/ (DGN-935).
# Then age out remaining scratch via archive_old() -- same reversible
# trash/_archive path and same RETENTION_DAYS as outbox/images (no rm -rf).
clean_tmp() {
    guard_dir "$TMP_DIR" || return 0
    log "tmp 정리 대상: $TMP_DIR"

    # -- 1a) Quarantine gate: rescue spec-signature files first --
    local rescue_dir="$ARCHIVE_DIR/tmp-rescue/$_RESCUE_DATE"
    local found=0
    while IFS= read -r -d '' item; do
        if _is_spec_signature "$item"; then
            found=1
            log "  [WARN] spec signature in tmp (placement violation, DGN-935): $item"
            if [[ "$DRY_RUN" -eq 1 ]]; then
                log "  [DRY] rescue move planned: $item -> $rescue_dir/"
            else
                mkdir -p "$rescue_dir"
                mv -- "$item" "$rescue_dir/"
                log "  rescued: $item -> $rescue_dir/"
            fi
        fi
    done < <(find "$TMP_DIR" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -print0)
    # NOTE: if/fi, not `[[..]] && log` -- under set -e a false test as the
    # function's last statement returns non-zero and aborts the whole script.
    if [[ "$found" -eq 0 ]]; then log "  (no spec signature -- quarantine skipped)"; fi

    # -- 1b) Remaining scratch: reuse the outbox/images path (mtime
    #        +RETENTION_DAYS -> trash or _archive; .gitkeep excluded by find) --
    archive_old "$TMP_DIR"
}

# ---- 2) tmp scratch / outbox / images: mtime +RETENTION_DAYS -> trash 또는 _archive ----
archive_old() {
    local dir="$1"
    guard_dir "$dir" || return 0
    log "오래된 파일 검사(>${RETENTION_DAYS}일): $dir"
    local found=0
    while IFS= read -r -d '' item; do
        found=1
        if [[ "$DRY_RUN" -eq 1 ]]; then
            if command -v trash >/dev/null 2>&1; then
                log "  [DRY] trash 예정: $item"
            else
                log "  [DRY] _archive 이동 예정: $item"
            fi
        else
            if command -v trash >/dev/null 2>&1; then
                # NOTE: no `--` -- /usr/bin/trash treats it as a literal filename
                # (no end-of-options support) -> spurious "-- doesn't exist" errors.
                # Safe: find is given an absolute $dir, so every $item is an
                # absolute path (starts with /), never a dash-leading flag.
                trash "$item" && log "  trash: $item"
            else
                guard_dir "$ARCHIVE_DIR" || { log "  _archive 없음, 건너뜀: $item"; continue; }
                mv -- "$item" "$ARCHIVE_DIR/" && log "  _archive 이동: $item"
            fi
        fi
    done < <(find "$dir" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -mtime "+${RETENTION_DAYS}" -print0)
    # NOTE: if/fi, not `[[..]] && log` -- see clean_tmp() above (set -e footgun).
    if [[ "$found" -eq 0 ]]; then log "  (nothing to remove)"; fi
}

# ---- 2b) trash retention (v2.7 grill F16) ----
# lib/trash.sh moves every rewritten `rm` into _archive/trash/<YYYYMMDD>/;
# without a purge that store grows without bound (worktree node_modules).
# A dated dir older than TRASH_RETENTION_DAYS is judged by its NAME (the day
# it was trashed), never by mtime (mv keeps the file's own mtime).  The
# DGN-935 quarantine rule runs first: a spec-signature file is rescued, not
# purged.
purge_trash() {
    local tdir="$ARCHIVE_DIR/trash"
    [[ -d "$tdir" ]] || return 0
    local cutoff
    cutoff="$(date -v-"${TRASH_RETENTION_DAYS}"d +%Y%m%d 2>/dev/null \
        || date -d "${TRASH_RETENTION_DAYS} days ago" +%Y%m%d)"
    log "trash retention (>${TRASH_RETENTION_DAYS}d, < $cutoff): $tdir"
    local found=0 day item
    for day in "$tdir"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]; do
        [[ -d "$day" ]] || continue
        [[ "$(basename "$day")" < "$cutoff" ]] || continue
        found=1
        while IFS= read -r -d '' item; do
            if _is_spec_signature "$item"; then
                local rescue_dir="$ARCHIVE_DIR/tmp-rescue/$_RESCUE_DATE"
                if [[ "$DRY_RUN" -eq 1 ]]; then
                    log "  [DRY] rescue move planned: $item -> $rescue_dir/"
                else
                    mkdir -p "$rescue_dir"
                    mv -n -- "$item" "$rescue_dir/" && log "  rescued: $item -> $rescue_dir/"
                fi
            fi
        done < <(find "$day" -type f -print0)
        if [[ "$DRY_RUN" -eq 1 ]]; then
            log "  [DRY] purge planned: $day"
        elif command -v trash >/dev/null 2>&1; then
            trash "$day" && log "  trash: $day"
        else
            rm -rf -- "$day" && log "  purged: $day"
        fi
    done
    if [[ "$found" -eq 0 ]]; then log "  (nothing to remove)"; fi
}

# ---- 3) ledger reconcile (DGN-1730) ----
# Daily housekeeping is the right host: it already runs once a day on every
# instance, needs no LLM, and a stale-row sweep is the same kind of chore as
# aging out scratch files. Best-effort -- a reconcile failure must never fail
# the file cleanup, and a missing script/ledger is a silent skip.
reconcile_ledger() {
    local rec="$SCRIPT_DIR/ledger-reconcile.sh"
    [[ -f "$rec" ]] || return 0
    local args=()
    if [[ "$DRY_RUN" -eq 1 ]]; then args+=(--dry-run); fi
    bash "$rec" "${args[@]+"${args[@]}"}" || log "  [WARN] ledger-reconcile 실패 (계속 진행)"
}

# ---- 4) retire this instance's completed job scratch ----
reap_host() {
    local storage="$SCRIPT_DIR/lib/dispatch_storage.py" args=()
    [[ -f "$storage" ]] || return 0
    [[ "$DRY_RUN" -ne 0 ]] || args+=(--apply)
    python3 -B "$storage" sweep --workspace "$BASE" "${args[@]+"${args[@]}"}" \
        || log "  [WARN] job scratch reclaim failed (continuing)"
}

# ---- 5) filing sweep (DGN-117 / dec-535) ----
# Same daily host as 3): no LLM, best-effort, a failure never fails cleanup.
sweep_filing() {
    local fl="$SCRIPT_DIR/lib/filing.py" args=()
    [[ -f "$fl" ]] || return 0
    [[ -f "$BASE/files/INDEX.md" || -d "$BASE/files/docs" ]] || return 0
    if [[ "$DRY_RUN" -eq 1 ]]; then args+=(--dry-run); fi
    # FILING_TODAY (YYYY-MM-DD) pins the clock for rehearsals; unset = today.
    python3 -B "$fl" --base "$BASE" ${FILING_TODAY:+--today "$FILING_TODAY"} sweep "${args[@]+"${args[@]}"}" \
        || log "  [WARN] filing sweep failed (continuing)"
}

log "시작 $( [[ "$DRY_RUN" -eq 1 ]] && echo '(DRY-RUN)' )"
clean_tmp
archive_old "$OUTBOX_DIR"
archive_old "$IMAGES_DIR"
purge_trash
reconcile_ledger
sweep_filing
if [[ ${#residue_roots[@]} -eq 0 ]]; then reap_host; fi
# DGN-2270 / dec-553: daily unattended apply; --dry-run remains a preview.
if [[ -f "$SCRIPT_DIR/cleanup-test-residue.py" ]]; then
    residue_args=()
    if [[ "$DRY_RUN" -eq 0 && "${DGN_TEST_RESIDUE_APPLY:-1}" == 1 ]]; then
        residue_args+=(--apply)
    fi
    python3 "$SCRIPT_DIR/cleanup-test-residue.py" "${residue_args[@]+"${residue_args[@]}"}" \
        "${residue_roots[@]+"${residue_roots[@]}"}" "${residue_trash[@]+"${residue_trash[@]}"}" || log "  [WARN] residue cleanup incomplete; see skip/trash-kept above"
fi
log "완료"
