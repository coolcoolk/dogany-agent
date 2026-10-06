# update_door.sh -- DGN-1417 / DGN-1816: the ONE update apply door check.
# Sourced (not executed) by routines/self-update.sh and by the framework
# repo's update.sh, so `dogany update` -> update.sh cannot bypass /update.
# One implementation, two entry points; the caller provides msg().
#
# update_door_check <machinery_root> <instance_root> <entry> <dry 0|1> <direct_why>
#   machinery_root  framework checkout whose scripts/pack/update_surface.py
#                   is the /update surface (self-update: DOGANY_REPO_ROOT;
#                   update.sh: its own tree)
#   instance_root   the instance being updated
#   entry           "self-update" | "update" (message tag + script name)
#   dry             1 = preview run (writes nothing) -> door stays open
#   direct_why      operator emergency reason ("" = none)
# Returns 0 = pass, 1 = refused (message printed to stderr, nothing changed).
#
# The door applies when BOTH hold:
#   - the machinery carries scripts/pack/update_surface.py (the /update
#     surface; public trees carry it since dec-203), and
#   - the instance's own bridge registers the /update command. An instance
#     installed before /update existed (e.g. a v2.5 install taking its first
#     v2.6 update via `dogany update` or its old self-update.sh) has no other
#     door, so the direct path stays open for it.
# Inside the door: DOGANY_UPDATE_DOOR=apply (update_apply.sh's framework
# delegation, inherited by the update.sh it runs). A recorded --direct-why
# pass exports DOGANY_UPDATE_DOOR=apply so the same run's child update.sh
# (and update.sh's release-channel re-exec) does not ask again.

update_door_applies() {  # <machinery_root> <instance_root>
  [ -f "$1/scripts/pack/update_surface.py" ] || return 1
  grep -qsF 'CommandHandler("update"' "$2"/bridge/*.py
}

update_door_check() {
  local mroot="$1" iroot="$2" entry="$3" dry="$4" why="$5"
  update_door_applies "$mroot" "$iroot" || return 0
  [ "${DOGANY_UPDATE_DOOR:-}" = "apply" ] && return 0
  [ "$dry" = "1" ] && return 0
  if [ -z "$why" ]; then
    printf '%s\n' "============================================================" >&2
    msg "[$entry][거부] DGN-1417: 이 인스턴스의 업데이트 적용 문은 /update 하나입니다 -- $entry.sh 직접 실행은 크루를 갈라놓거나 킷보다 프레임워크를 먼저 올릴 수 있습니다. 아무것도 바뀌지 않았습니다." \
        "[$entry][REFUSED] DGN-1417: this instance applies updates through /update only -- a direct $entry.sh run can split the crew or land the framework ahead of its kit. Nothing was changed." >&2
    msg "  다음 행동: 오너에게 /update 로 적용할 업데이트를 고르시도록 안내하세요 (순서와 크루 동시 적용은 그 장부가 지킵니다)." \
        "  Next: ask the owner to pick the update in /update (that ledger keeps the order and applies the crew together)." >&2
    msg "  운영자 비상 경로(/update 를 쓸 수 없을 때만): $entry.sh --direct-why='<이유>' -- 이유가 logs/self-update-direct.log 에 남습니다." \
        "  Operator emergency only (/update unreachable): $entry.sh --direct-why='<reason>' -- the reason is recorded in logs/self-update-direct.log." >&2
    printf '%s\n' "============================================================" >&2
    return 1
  fi
  mkdir -p "$iroot/logs"
  printf '%s direct %s outside /update (DGN-1417): %s\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$entry" "$why" >> "$iroot/logs/self-update-direct.log"
  msg "[$entry][경고] DGN-1417: /update 장부 밖 직접 실행 (이유 기록됨: $why) -- 크루 동시 적용과 킷 우선 순서는 이 실행에서 보장되지 않습니다." \
      "[$entry][WARN] DGN-1417: direct run outside the /update ledger (reason recorded: $why) -- crew-together and kit-first order are NOT guaranteed by this run." >&2
  export DOGANY_UPDATE_DOOR=apply
  return 0
}
