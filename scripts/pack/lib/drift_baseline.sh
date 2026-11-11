# drift_baseline.sh -- baseline-tag resolver for the content-drift guard.
# Sourced library (no shebang, no side effects). Requires git.
#
# SINGLE SOURCE (DGN-1034 shape guard): this is the DGN-1201 B1-minimal
# ladder, extracted VERBATIM from update.sh by DGN-1491 so the pack lane can
# call the SAME resolver instead of growing a second one. Two callers:
#   * update.sh content_drift_guard (framework lane): tag space v<VER>;
#     prefix omitted -> "v", behavior byte-identical to the pre-extraction
#     inline function (DGN-1201).
#   * scripts/pack/pack_install.sh content-drift guard (pack lane,
#     DGN-1491): tag space pack/<id>/v<VER> (dec-120 #1 grammar); prefix
#     "pack/<id>/v", version = the instance's DOGANY_PACKS pin.
#
# resolve_drift_baseline_tag <repo> <recorded_tag> <recorded_version> [<tag_prefix>]
#   Sets globals:
#     DRIFT_BL_TAG  -- resolved tag name; EMPTY = unresolvable (fail-closed,
#                      the caller falls back to the strict DGN-1109 signal).
#     DRIFT_BL_WHY  -- one-line reason for an empty DRIFT_BL_TAG (language
#                      picked by msg()), printed by the caller's note.
#   <tag_prefix> defaults to "v" (framework tag space). The pack lane passes
#   "pack/<id>/v" so every rung walks the pack's own tag namespace.
#   Ladder -- stops at the first rung that resolves and NEVER guesses:
#     1. recorded tag (update lane: DGN-626 W1 landing stamp) AND it exists
#        as a tag -> use it. A recorded-but-missing tag is NOT retried
#        through the rungs below: the record is the truth, so substituting a
#        reconstructed name for it would be a silent baseline swap.
#     2. no recorded tag: stable <prefix><VER> exists -> use it (the
#        pre-DGN-1201 fallback, kept). Pre-release landings (-dev.N / -rc.N)
#        recorded only a plain version and have NO stable tag while the base
#        is still unreleased -- that is the DGN-1201 shape. (Pack lane note:
#        a post-t2pin DOGANY_PACKS pin already carries the pre-release
#        suffix, so this rung verifies the exact installed tag directly.)
#     3. no stable: enumerate <prefix><VER>-rc.N + <prefix><VER>-dev.N.
#        EXACTLY ONE candidate -> adopt it (reason printed via msg()).
#        ZERO or TWO+ candidates -> unresolvable, with the count and the
#        list in DRIFT_BL_WHY. The max/newest candidate is NEVER picked: a
#        confidently wrong baseline looks identical to a right one, which is
#        worse than the loud strict-mode fallback (DGN-1201 s7).
#   TODO(DGN-1201 s8): when >=2 candidates actually occur a fingerprint rung
#   is needed; its file set must be DERIVED from `git diff A B` of each
#   candidate pair -- a fixed file list is wrong in principle (files that do
#   not differ between the candidates are not evidence).

# msg() host contract: update.sh defines a ko/en msg() before sourcing this
# lib. A host without one (pack_install.sh: installer logs are English-only
# by the code=English/ASCII rule) gets this English-arm fallback.
if ! declare -F msg >/dev/null 2>&1; then
  msg() { printf '%s\n' "$2"; }
fi

DRIFT_BL_TAG=""
DRIFT_BL_WHY=""
resolve_drift_baseline_tag() {
  local _rb_repo="${1:-}" _rb_tag="${2:-}" _rb_ver="${3:-}" _rb_pfx="${4:-v}"
  local _rb_re="" _rb_pfx_re="" _rb_cands="" _rb_n=0 _rb_list=""
  DRIFT_BL_TAG=""
  DRIFT_BL_WHY=""
  if [ -z "$_rb_repo" ] || [ ! -e "$_rb_repo/.git" ]; then
    DRIFT_BL_WHY="$(msg "repo 부재 (repo=${_rb_repo:-없음})" \
                        "repo unavailable (repo=${_rb_repo:-none})")"
    return 0
  fi
  # 1. recorded tag (DGN-626 W1 stamp): primary, and final either way.
  if [ -n "$_rb_tag" ]; then
    if git -C "$_rb_repo" rev-parse -q --verify "refs/tags/${_rb_tag}^{commit}" >/dev/null 2>&1; then
      DRIFT_BL_TAG="$_rb_tag"
    else
      DRIFT_BL_WHY="$(msg "기록된 태그 ${_rb_tag} 가 repo 에 태그로 없음 (재구성 시도 안 함)" \
                          "recorded tag ${_rb_tag} is not a tag in the repo (no reconstruction attempted)")"
    fi
    return 0
  fi
  if [ -z "$_rb_ver" ]; then
    DRIFT_BL_WHY="$(msg "태그/버전 둘 다 기록 없음" \
                        "neither a tag nor a version is recorded")"
    return 0
  fi
  # 2. stable <prefix><VER> (the pre-DGN-1201 fallback).
  if git -C "$_rb_repo" rev-parse -q --verify "refs/tags/${_rb_pfx}${_rb_ver}^{commit}" >/dev/null 2>&1; then
    DRIFT_BL_TAG="${_rb_pfx}${_rb_ver}"
    return 0
  fi
  # 3. pre-release candidates: <prefix><VER>-rc.N + <prefix><VER>-dev.N,
  #    exact shape only (the glob pre-filters; the anchored grep rejects
  #    e.g. -dev.1x). Regex-escaping: '.' is the only regex metachar either
  #    part can carry -- the framework prefix is the literal "v" and the pack
  #    prefix is pack/<id>/v with <id> constrained to ^[a-z][a-z0-9_-]{0,31}$
  #    (PACK_TOKEN_CORE, lib/pack_token_grammar.sh).
  _rb_re="$(printf '%s' "$_rb_ver" | sed 's/\./\\./g')"
  _rb_pfx_re="$(printf '%s' "$_rb_pfx" | sed 's/\./\\./g')"
  _rb_cands="$(git -C "$_rb_repo" tag --list "${_rb_pfx}${_rb_ver}-rc.*" "${_rb_pfx}${_rb_ver}-dev.*" 2>/dev/null \
                 | grep -E "^${_rb_pfx_re}${_rb_re}-(rc|dev)\.[0-9]+\$" || true)"
  if [ -n "$_rb_cands" ]; then
    _rb_n="$(printf '%s\n' "$_rb_cands" | wc -l | tr -d ' ')"
  fi
  if [ "$_rb_n" -eq 1 ]; then
    DRIFT_BL_TAG="$_rb_cands"
    msg "콘텐츠-드리프트 가드: stable 태그 ${_rb_pfx}${_rb_ver} 부재, 프리릴리스 후보 유일 -> ${_rb_cands} 를 베이스라인으로 채택 (DGN-1201)" \
        "content-drift guard: stable tag ${_rb_pfx}${_rb_ver} absent, single pre-release candidate -> adopting ${_rb_cands} as the baseline (DGN-1201)"
    return 0
  fi
  if [ "$_rb_n" -eq 0 ]; then
    DRIFT_BL_WHY="$(msg "stable 태그 ${_rb_pfx}${_rb_ver} 부재 + 프리릴리스 후보(${_rb_pfx}${_rb_ver}-rc.N/-dev.N) 0건" \
                        "stable tag ${_rb_pfx}${_rb_ver} absent + zero pre-release candidates (${_rb_pfx}${_rb_ver}-rc.N/-dev.N)")"
  else
    _rb_list="$(printf '%s\n' "$_rb_cands" | tr '\n' ' ' | sed 's/ $//')"
    DRIFT_BL_WHY="$(msg "stable 태그 ${_rb_pfx}${_rb_ver} 부재 + 프리릴리스 후보 ${_rb_n}건 -- 추측 채택 거부 (DGN-1201 4단계): ${_rb_list}" \
                        "stable tag ${_rb_pfx}${_rb_ver} absent + ${_rb_n} pre-release candidates -- refusing to guess (DGN-1201 rung 4): ${_rb_list}")"
  fi
  return 0
}
