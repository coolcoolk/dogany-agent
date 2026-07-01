#!/bin/bash
#
# cleanup-files.sh — 에이전트 파일 정리 루틴 (베이스라인)
#
# 동작:
#   1) files/tmp/ 안의 모든 파일·폴더 삭제 (.gitkeep 보존)
#   2) files/outbox/ 와 .telegram_bot/images/ 에서 mtime 7일 초과 파일을
#      trash(있으면) 또는 files/_archive 로 이동
#
# 절대 손대지 않음: files/inbox/, data/, memories/
#
# 사용: cleanup-files.sh [--dry-run]
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

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
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

# ---- 1) tmp 비우기 (.gitkeep 보존) ----
clean_tmp() {
    guard_dir "$TMP_DIR" || return 0
    log "tmp 정리 대상: $TMP_DIR"
    # .gitkeep 제외한 모든 항목(파일·폴더·숨김파일)
    local found=0
    while IFS= read -r -d '' item; do
        found=1
        if [[ "$DRY_RUN" -eq 1 ]]; then
            log "  [DRY] 삭제 예정: $item"
        else
            rm -rf -- "$item"
            log "  삭제: $item"
        fi
    done < <(find "$TMP_DIR" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -print0)
    [[ "$found" -eq 0 ]] && log "  (tmp 비어있음)"
}

# ---- 2) outbox / images 30일 초과 → trash 또는 _archive ----
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
                trash -- "$item" && log "  trash: $item"
            else
                guard_dir "$ARCHIVE_DIR" || { log "  _archive 없음, 건너뜀: $item"; continue; }
                mv -- "$item" "$ARCHIVE_DIR/" && log "  _archive 이동: $item"
            fi
        fi
    done < <(find "$dir" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -mtime "+${RETENTION_DAYS}" -print0)
    [[ "$found" -eq 0 ]] && log "  (대상 없음)"
}

log "시작 $( [[ "$DRY_RUN" -eq 1 ]] && echo '(DRY-RUN)' )"
clean_tmp
archive_old "$OUTBOX_DIR"
archive_old "$IMAGES_DIR"
log "완료"
