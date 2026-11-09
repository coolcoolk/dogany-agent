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
#   - the instance's own bridge registers the /update command, and
#   - the instance's INSTALLED build is not one that predates /update.
#     An instance installed before /update existed (e.g. a v2.5 install
#     taking its first v2.6 update via `dogany update` or its old
#     self-update.sh) has no other door, so the direct path stays open.
#     DGN-1901: "installed build" is read from the landing stamp in
#     .instance.conf (DOGANY_FW_VERSION / DOGANY_FW_TAG), which update.sh
#     writes only at the very end of a successful run. The bridge files on
#     disk are NOT proof: a run that refreshed the tree and then failed
#     (e.g. exit 4 on a missing faster-whisper, before the stamp) leaves a
#     /update-bearing bot.py and the new self-update.sh + this lib on disk
#     while the installed (and running) build is still 2.5 -- the retry was
#     refused and the instance had no door at all. No stamp -> the bridge
#     file check alone decides (the pre-DGN-1901 behavior).
# Inside the door: DOGANY_UPDATE_DOOR=apply (update_apply.sh's framework
# delegation, inherited by the update.sh it runs). A recorded --direct-why
# pass exports DOGANY_UPDATE_DOOR=apply so the same run's child update.sh
# (and update.sh's release-channel re-exec) does not ask again.

# First build that ships the /update command: v2.6.0-dev.44 (every v2.5.x
# and v2.6.0-dev.1..43 bridge lacks it; v2.6.0 stable and later carry it).
UPDATE_DOOR_FIRST_DEV=44

# update_door_stamp_predates <instance_root>
#   0 = the installed build predates /update, 1 = it carries /update,
#   2 = unknown (no or unparseable DOGANY_FW_VERSION).
#   DOGANY_FW_VERSION holds X.Y.Z only (a -dev.N tag's VERSION file is its
#   X.Y.Z); for 2.6.0 the exact build is DOGANY_FW_TAG, used only while its
#   X.Y.Z equals DOGANY_FW_VERSION (update.sh's own staleness rule).
update_door_stamp_predates() {
  local conf="$1/.instance.conf" v t base n
  [ -f "$conf" ] || return 2
  v="$(sed -n 's/^DOGANY_FW_VERSION=//p' "$conf" | head -n1)"
  v="$(printf '%s' "$v" | sed -n 's/^v\{0,1\}\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)\(-.*\)\{0,1\}[[:space:]]*$/\1/p')"
  [ -n "$v" ] || return 2
  set -- $(printf '%s' "$v" | tr '.' ' ')
  if [ "$1" -lt 2 ] || { [ "$1" -eq 2 ] && [ "$2" -lt 6 ]; }; then
    return 0
  fi
  [ "$1" -eq 2 ] && [ "$2" -eq 6 ] && [ "$3" -eq 0 ] || return 1
  t="$(sed -n 's/^DOGANY_FW_TAG=//p' "$conf" | head -n1)"
  base="$(printf '%s' "$t" | sed -n 's/^v\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)-dev\.[0-9][0-9]*$/\1/p')"
  n="$(printf '%s' "$t" | sed -n 's/^v[0-9.]*-dev\.\([0-9][0-9]*\)$/\1/p')"
  [ "$base" = "$v" ] && [ -n "$n" ] && [ "$n" -lt "$UPDATE_DOOR_FIRST_DEV" ] && return 0
  return 1
}

update_door_applies() {  # <machinery_root> <instance_root>
  [ -f "$1/scripts/pack/update_surface.py" ] || return 1
  grep -qsF 'CommandHandler("update"' "$2"/bridge/*.py || return 1
  update_door_stamp_predates "$2" && return 1
  return 0
}

update_door_check() {
  local mroot="$1" iroot="$2" entry="$3" dry="$4" why="$5"
  if ! update_door_applies "$mroot" "$iroot"; then
    # DGN-1901: say why the door stayed open when the files alone would
    # have closed it (an earlier run landed /update but never finished).
    # Once per run: update.sh's release-channel child inherits the marker.
    if [ -z "${DOGANY_DOOR_STAMP_NOTED:-}" ] \
       && grep -qsF 'CommandHandler("update"' "$iroot"/bridge/*.py \
       && update_door_stamp_predates "$iroot"; then
      printf '%s\n' "[$entry] DGN-1901: the installed stamp (.instance.conf) predates /update -- the /update-bearing bridge files on disk are from an unfinished earlier run, so this direct update completes it." >&2
      export DOGANY_DOOR_STAMP_NOTED=1
    fi
    return 0
  fi
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
