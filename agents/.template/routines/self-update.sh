#!/bin/sh
# self-update.sh -- update THIS instance to the latest framework, zero args.
#
# WHAT THIS IS: the "update yourself" entrypoint. An agent told to update
# itself runs THIS script -- it needs no --root and no operator input.
#
# update != release:
#   - self-update.sh / update.sh CONSUME a published framework release into
#     an instance (this is "update yourself").
#   - bumping VERSION + tagging PRODUCES a release (that is release.sh, a
#     separate, maintainer-only act). "Update yourself" is NEVER a release.
#
# Behaviour:
#   1. Resolve THIS instance's own root from the script's own location
#      (routines/ -> instance root), NOT from cwd -- the job survives a
#      workspace move and does not depend on where it is invoked from.
#   2. Refuse if that root has no .instance.conf (mirrors update.sh's gate:
#      a real minted instance always carries one).
#   3. Read DOGANY_REPO_ROOT from .instance.conf and resolve the update
#      SOURCE. Channel semantics (DGN-593 A3):
#        - release (default, DGN-221): the shared checkout is NEVER moved.
#          Read-only against the repo: `git fetch --tags`, then extract the
#          latest v* tag into a private per-run temp dir with `git archive`
#          and hand off to THAT tree's update.sh. Working tree / HEAD /
#          index of the shared repo are untouched -- release never touches
#          the shared checkout.
#        - main (DOGANY_UPDATE_CHANNEL=main, env or .instance.conf): old
#          `git pull --ff-only` dogfood behaviour, guarded: the repo must
#          actually be ON the main branch (detached / dev branch = a dev
#          session in progress -> die, never auto-switch).
#   4. Invoke update.sh --root <self> --no-pull --yes (self-targeted,
#      non-interactive) as a CHILD process -- never exec: exec would kill
#      the EXIT trap that cleans the per-run temp source. The child's exit
#      status is passed through as this script's exit status.
#
# No owner data, no machine paths, no identity placeholders: every path is
# resolved at runtime. Safe to ship generically (template / OSS).
#
# Usage:
#   ./self-update.sh            # update this instance to the latest framework
#   DOGANY_LANG=ko ./self-update.sh   # Korean messages (default: en)
#
# Pass-through: any extra args are forwarded to update.sh (e.g. --dry-run,
# --no-pull) for advanced/debug use; the common case is zero args.
#
# Local flags (consumed here, NEVER forwarded to update.sh):
#   --no-restart   skip the automatic bridge restart after a successful update
#
# DGN-685: after a successful update this script carries the owner's single
# approval through to the bridge restart (bridge/self_restart.sh). Interactive
# runs (CLAUDECODE env present) restart immediately (--trigger user);
# autonomous runs (cron -- no CLAUDECODE) use --trigger auto so the idle guard
# can quietly defer. --dry-run implies --no-restart. Note: a no-op update
# (already up to date, rc 0) still restarts -- deliberate, it flushes stale
# long-running bridge processes (DGN-685 edge m2).
set -eu

# UTF-8 locale regardless of caller env. `cut -c` below (DGN detail-body
# truncation) is a POSIX character count only under a UTF-8 locale; under
# the C locale (e.g. an env with LANG/LC_ALL unset) it would count bytes
# instead and could slice a Korean release-notes character in half.
# Host coverage (DGN-1059): en_US.UTF-8 is not guaranteed (minimal Linux may
# not have it generated), and setting a missing locale falls back to C
# SILENTLY -- the fix would be inert. So probe candidates and take the first
# whose charmap really is UTF-8 (C.UTF-8 is built into glibc >= 2.35,
# Ubuntu 22.04+, and present on macOS). The probe must never kill the script
# under `set -e` -- hence 2>/dev/null + `|| true`.
_utf8_loc=""
for _cand in en_US.UTF-8 C.UTF-8; do
  if [ "$(LC_ALL="$_cand" locale charmap 2>/dev/null || true)" = "UTF-8" ]; then
    _utf8_loc="$_cand"
    break
  fi
done
if [ -n "$_utf8_loc" ]; then
  export LC_ALL="$_utf8_loc"
  export LANG="$_utf8_loc"
else
  # No UTF-8 locale at all. Do NOT abort the update over it (the update
  # itself still works), but never stay silent: silent byte-truncation is
  # the exact defect class this block exists to prevent.
  printf '%s\n' "[self-update.sh] WARN: no UTF-8 locale available (tried en_US.UTF-8, C.UTF-8); truncation may cut bytes, not characters, and can corrupt multi-byte (e.g. Korean) text" >&2
fi

_ENV_LANG="${DOGANY_LANG:-}"
DOGANY_LANG="${DOGANY_LANG:-en}"
msg() { if [ "$DOGANY_LANG" = "ko" ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi; }
die() { msg "[오류] $1" "[ERROR] $1" >&2; exit 1; }

# DGN-685: arg pre-pass. --no-restart is a LOCAL flag (consumed, not
# forwarded); --dry-run implies no restart but IS forwarded to update.sh.
# POSIX "$@" rebuild idiom -- value-carrying args (e.g. --bridge-accept GLOB)
# survive intact because everything is re-appended quoted (grill M2).
RESTART=1
for a in "$@"; do
  shift
  case "$a" in
    --no-restart) RESTART=0 ;;
    --dry-run)    RESTART=0; set -- "$@" "$a" ;;
    *)            set -- "$@" "$a" ;;
  esac
done

# DGN-685 B1: structural restart-trigger gate. Interactive session = the
# harness Bash tool sets CLAUDECODE -> the owner's update approval doubles as
# restart approval (--trigger user, idle guard skipped, immediate). No
# CLAUDECODE (cron / autonomous) -> --trigger auto, idle guard applies and a
# refusal quietly defers -- an autonomous run can never hard-kill an active
# owner session. Structural, not caller-discipline (dec-094).
if [ -n "${CLAUDECODE:-}" ]; then TRIG="user"; else TRIG="auto"; fi

# ---------------------------------------------------------------------------
# resolve_channel_tag <repo_root> <channel>  (DGN-621 v2 phase1, DGN-626 W1)
#   Mirror of update.sh's selector. Channels are distinguished by TAG SUFFIX,
#   and every tag-consuming channel is an explicit WHITELIST -- exclusion is
#   never delegated to versionsort ordering (DGN-626 F1):
#     release (default) : highest STABLE tag  vX.Y.Z  -- pre-release tags
#                         (any 'v*-*', e.g. -dev.N / -rc.N) are excluded.
#     dev               : highest of  vX.Y.Z | vX.Y.Z-dev.N  ONLY (-rc.N
#                         excluded: a live rc would shadow later -dev tags and
#                         be auto-consumed by dev canaries).
#     staging           : highest of  vX.Y.Z | vX.Y.Z-rc.N  ONLY (-dev
#                         excluded) -- the soak ring (DGN-626).
#   versionsort.suffix=- ranks any hyphenated suffix BELOW its stable, so once
#   the stable exists dev/staging subscribers move forward to it and never
#   regress to a pre-release. (Default git version-sort would WRONGLY rank
#   vX.Y.Z-dev.N ABOVE vX.Y.Z -- DGN-621 verified 2026-07-28 -- hence the
#   explicit -c flag; it also makes ordering independent of global git config.)
#   DOGANY_UPDATE_PIN (optional, env): OVERRIDES channel -- select exactly that
#     tag; if the pinned tag is absent, FAIL LOUD (never silent latest).
#   Unknown channel: FAIL LOUD (die) -- never a silent release fallback.
#   Stable-only estate (no PIN, release/unset) == the old one-liner result.
#   Prints the tag on stdout; empty = no eligible tag (callers handle "no tag").
# ---------------------------------------------------------------------------
resolve_channel_tag() {
  _rct_repo="$1"
  _rct_channel="${2:-release}"
  if [ -n "${DOGANY_UPDATE_PIN:-}" ]; then
    if git -C "$_rct_repo" rev-parse -q --verify "refs/tags/${DOGANY_UPDATE_PIN}" >/dev/null 2>&1; then
      printf '%s\n' "$DOGANY_UPDATE_PIN"
      return 0
    fi
    die "DOGANY_UPDATE_PIN='${DOGANY_UPDATE_PIN}' not found as a tag in ${_rct_repo} -- refusing to fall back to latest (fix the pin or unset it)"
  fi
  case "$_rct_channel" in
    dev)
      # Whitelist: stable + -dev.N only (DGN-626 F1 -- -rc.N excluded).
      # versionsort.suffix=- ranks any pre-release below its stable (so a
      # promoted vX.Y.Z outranks vX.Y.Z-dev.N); default git sorts -dev ABOVE.
      git -C "$_rct_repo" -c versionsort.suffix=- tag --list 'v*' --sort=-v:refname \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-dev\.[0-9]+)?$' | head -n1
      ;;
    staging)
      # Whitelist: stable + -rc.N only (-dev excluded). Same ranking flag:
      # the moment a base's stable is cut, staging subscribers advance to it
      # and never regress to an -rc.
      git -C "$_rct_repo" -c versionsort.suffix=- tag --list 'v*' --sort=-v:refname \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$' | head -n1
      ;;
    release|"")
      # release (default): exclude pre-release tags (any tag whose name
      # contains a hyphen, e.g. -dev.N / -rc.N).
      git -C "$_rct_repo" tag --list 'v*' --sort=-v:refname \
        | grep -v -- '-' | head -n1
      ;;
    *)
      # Unknown channel = operator error: fail loud, never a silent release
      # fallback (a typo'd channel quietly consuming stable is DGN-626 F1's
      # root shape). Channel "main" never reaches here (pull path).
      die "unknown DOGANY_UPDATE_CHANNEL '${_rct_channel}' (valid here: release|dev|staging) -- refusing the silent release fallback (fix the channel declaration)"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# resolve_update_pin <instance_conf> <channel>  (DGN-673 B2, owner decision D2)
#   DOGANY_UPDATE_PIN persistence: env wins, .instance.conf fallback -- the
#   exact UPDATE_CHANNEL read pattern above. A pin persisted in the conf
#   survives scheduled self-updates, so a rolled-back instance is never
#   silently re-upgraded to the bad tag on the next run (DGN-673 failure S8).
#   Resolution:
#     1. env DOGANY_UPDATE_PIN non-empty -> env wins, conf not consulted.
#     2. else conf line 'DOGANY_UPDATE_PIN=<tag>' -> adopt the conf pin.
#     3. conf line present but EMPTY value -> pin released: print a notice
#        (repeats every run until the leftover line is deleted -- deliberate
#        cleanup nudge; pin removal is a rollback incident-close item).
#   A resolved pin is EXPORTED so child update.sh runs inherit it. Every
#   pinned run prints a loud PINNED banner; channel 'main' never resolves
#   tags (pull path), so a pin there gets a loud no-effect warning instead.
#   Call at most ONCE per operator-level run, where the update target is
#   about to be resolved -- never in --no-pull child runs (the parent
#   already announced the pin; a second banner would be noise).
# ---------------------------------------------------------------------------
resolve_update_pin() {
  _rup_conf="$1"
  _rup_channel="${2:-release}"
  if [ -z "${DOGANY_UPDATE_PIN:-}" ] && [ -f "$_rup_conf" ] \
     && grep -q '^DOGANY_UPDATE_PIN=' "$_rup_conf" 2>/dev/null; then
    _rup_pin="$(sed -n 's/^DOGANY_UPDATE_PIN=//p' "$_rup_conf" | head -n1)"
    if [ -n "$_rup_pin" ]; then
      DOGANY_UPDATE_PIN="$_rup_pin"
    else
      msg "[update] 핀 해제됨 -- 채널 추종 재개 (.instance.conf의 빈 DOGANY_UPDATE_PIN= 라인은 삭제해도 됩니다)" \
          "[update] pin released -- channel following resumed (the empty DOGANY_UPDATE_PIN= line in .instance.conf can be deleted)"
    fi
  fi
  [ -n "${DOGANY_UPDATE_PIN:-}" ] || return 0
  export DOGANY_UPDATE_PIN
  if [ "$_rup_channel" = "main" ]; then
    msg "[update][경고] DOGANY_UPDATE_PIN='${DOGANY_UPDATE_PIN}' 은 channel=main(pull 경로)에서 아무 효과가 없습니다" \
        "[update][WARN] DOGANY_UPDATE_PIN='${DOGANY_UPDATE_PIN}' has NO effect on channel=main (pull path)" >&2
    return 0
  fi
  printf '%s\n' "============================================================"
  msg "[update] PINNED to ${DOGANY_UPDATE_PIN} -- 채널 추종 중단 (플릿 구제 후 핀 제거 필수)" \
      "[update] PINNED to ${DOGANY_UPDATE_PIN} -- channel following suspended (remove pin after fleet remedy)"
  printf '%s\n' "============================================================"
}

# 1) Resolve this instance's own root: routines/ -> instance root.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 2) Gate: a real minted instance carries a .instance.conf.
CONF="$SELF_ROOT/.instance.conf"
[ -f "$CONF" ] || die "not a minted Dogany instance (no .instance.conf): $SELF_ROOT"

# DGN-685 M3: user-notice language -- env DOGANY_LANG wins, else the
# instance's config/agent.conf AGENT_LANG, else en (msg() reads the result).
if [ -z "$_ENV_LANG" ]; then
  _CONF_LANG="$(sed -n 's/^AGENT_LANG=//p' "$SELF_ROOT/config/agent.conf" 2>/dev/null | head -n1)"
  if [ -n "$_CONF_LANG" ]; then DOGANY_LANG="$_CONF_LANG"; fi
fi

# ---------------------------------------------------------------------------
# DGN-687 helpers: release-note extraction for the restart-complete notice.
# DGN-822: notice bodies are passed RAW (no pre-escaping of & < >) -- push.sh
# routes every text send through the bridge sanitizer, which escapes entities
# itself; pre-escaping here would double-escape (e.g. "->" rendering as
# "-&gt;"). Whitelisted Telegram tags (<blockquote expandable>, ...) stay raw
# and pass the sanitizer verbatim.
# ---------------------------------------------------------------------------

# changelog_section <changelog_file> <version-without-v>
#   Print the CHANGELOG body for that version (lines between its "## [x.y.z]"
#   heading and the next "## [" heading). Empty output = not found (fail-open).
#   DGN-725: strip (DGN-NNN) and bare DGN-NNN ticket refs before output so
#   internal ticket numbers never reach the user-facing notice.
changelog_section() {
  _cs_file="$1"
  _cs_ver="$2"
  [ -f "$_cs_file" ] || return 0
  awk -v ver="## [${_cs_ver}]" '
    index($0, ver) == 1 { grab = 1; next }
    grab && /^## \[/    { exit }
    grab                { print }
  ' "$_cs_file" \
    | sed -e 's/([[:space:]]*DGN-[0-9][0-9]*[[:space:]]*)//g' \
          -e 's/[[:space:]]*DGN-[0-9][0-9]*//g' \
    | sed -e '/./,$!d'
}

# user_summary <releasefile> <lang>  (DGN-699 Spec A)
#   Extract the user-facing, jargon-free summary for <lang> from the
#   machine-readable HTML-comment block a release note carries near the top:
#
#     <!-- user-summary
#     ko: |
#       <Korean summary>
#     en: |
#       <English summary>
#     -->
#
#   This is the NOTICE body source, deliberately kept OUT of CHANGELOG.md
#   (which stays English/model-facing per DGN-210). Robust to absence: a
#   missing file, a missing block, or a missing <lang> key all print nothing
#   (empty stdout) so the caller falls back to changelog_section. YAML block
#   scalars ('key: |') are honoured -- every line indented deeper than the key
#   belongs to that key, and leading indentation is stripped. Matching stops at
#   the next same-or-less-indented 'key:' line or the closing '-->'.
user_summary() {
  _us_file="$1"
  _us_lang="$2"
  [ -f "$_us_file" ] || return 0
  awk -v want="$_us_lang" '
    # Enter the block on the "<!-- user-summary" opener; leave on "-->".
    /<!--[ \t]*user-summary[ \t]*$/ { inblk = 1; next }
    !inblk { next }
    /-->/  { exit }
    # A "key: |" line opens a block scalar; remember the key indent so we can
    # tell continuation lines (deeper) from the next key (same/less indent).
    match($0, /^[ \t]*[A-Za-z0-9_]+:[ \t]*\|[ \t]*$/) {
      line = $0
      sub(/:[ \t]*\|[ \t]*$/, "", line)      # -> "  ko"
      key = line; sub(/^[ \t]+/, "", key)    # -> "ko"
      keyind = match(line, /[^ \t]/) - 1     # indent columns of the key
      grab = (key == want) ? 1 : 0
      contind = -1
      next
    }
    grab {
      # First continuation line fixes the strip width (its own indent).
      curind = match($0, /[^ \t]/) - 1
      if ($0 ~ /^[ \t]*$/) { print ""; next }   # blank line inside the scalar
      if (curind <= keyind) { grab = 0; next }  # dedented -> scalar ended
      if (contind < 0) contind = curind
      print substr($0, contind + 1)
    }
  ' "$_us_file" | sed -e '/./,$!d'
}

# ---------------------------------------------------------------------------
# maybe_restart <rc> <ver> <src_root>  (DGN-685 spec v2 A3)
#   Carry a successful update straight into a bridge restart. Every early
#   return is `return 0` -- under set -e a non-zero return here would clobber
#   the update exit status (grill M1). <src_root> = the tree the update was
#   applied FROM (extracted tag dir or repo checkout); its CHANGELOG.md feeds
#   the DGN-687 restart-complete release-note fold.
# ---------------------------------------------------------------------------
maybe_restart() {
  _rc="$1"
  _ver="$2"
  _src_root="$3"
  [ "$_rc" -eq 0 ] || return 0
  [ "$RESTART" -eq 1 ] || return 0
  _rs="$SELF_ROOT/bridge/self_restart.sh"
  if [ ! -x "$_rs" ]; then
    msg "[self-update] 재시작 스크립트 없음/실행불가 -- 재시작 건너뜀 (수동: bridge/self_restart.sh --trigger user)" \
        "[self-update] restart script missing/not executable -- skipping restart (manual: bridge/self_restart.sh --trigger user)"
    return 0
  fi
  _vplain="${_ver#v}"
  _vdisp="v${_vplain}"
  # DGN-687 notice #3 (restart complete = update complete): pushed by the
  # self_restart worker AFTER the bridge is back up. Korean copy is LOCKED
  # (owner-approved 2026-08-01; DGN-697 re-lock 2026-08-02: header line plain,
  # release-notes fold stays expandable); en mirrors the structure.
  #
  # DGN-699 Spec A: the fold body is the user-facing, jargon-free summary from
  # releases/v<ver>.md's user-summary block (rendered in DOGANY_LANG), NOT the
  # English CHANGELOG scrape (which is model-facing per DGN-210).
  # DGN-784: CHANGELOG fallback removed. The CHANGELOG is English/developer-facing
  # (DGN-210) and contains raw markdown headers, backticks, and internal file/
  # function names that must never reach user-facing output (RULES: no internal
  # mechanics in user-facing text). When the user-summary block is absent (old
  # release or timing gap), the notice shows no fold body -- safe silence is
  # preferable to exposing raw developer content.
  # DGN-785: strip_tickets <text> -- remove DGN-NNN markers so they can never
  #   reach user-facing output. Strips parenthesized form first, then bare form,
  #   then collapses any doubled spaces / dangling " -- " left behind.
  strip_tickets() {
    printf '%s\n' "$1" \
      | sed -e 's/([[:space:]]*DGN-[0-9][0-9]*[^)]*[[:space:]]*)//g' \
            -e 's/[[:space:]]*DGN-[0-9][0-9]*//g' \
            -e 's/  */ /g' \
            -e 's/[[:space:]]*--[[:space:]]*$//' \
            -e 's/^[[:space:]]*//'
  }
  _notes_raw="$(user_summary "$_src_root/releases/${_vdisp}.md" "$DOGANY_LANG")"
  # DGN-785: apply ticket-strip safety net to all section bodies before render.
  _notes="$(strip_tickets "$_notes_raw")"
  # DGN-742: extended detail fold (COMPLETION notice only).
  #   Reads <lang>_detail key from the same user-summary block. Hard-capped at
  #   ~2500 chars; truncated output gets a continuation hint.
  #   If detail is empty, notice is byte-identical to pre-742 output (parity).
  # DGN-785: detail and try sections use the same strip_tickets safety net.
  _detail_raw="$(user_summary "$_src_root/releases/${_vdisp}.md" "${DOGANY_LANG}_detail")"
  _detail="$(strip_tickets "$_detail_raw")"
  if [ -n "$_detail" ]; then
    _detail_body="$_detail"
    _detail_len="${#_detail_body}"
    if [ "$_detail_len" -gt 2500 ]; then
      # NOTE: `head -c` truncates by BYTES regardless of locale and can slice
      # a multi-byte Hangul character in half; `cut -c` is a POSIX character
      # count under the UTF-8 locale exported above -- character-safe.
      _detail_body="$(printf '%s\n' "$_detail_body" | cut -c1-2500)"
      _detail_body="${_detail_body}
…(이하 생략 — 더 궁금하시면 물어봐 주세요)"
    fi
  fi
  # DGN-785: third section -- "try this" (ko_try / en_try key).
  #   Absent -> section omitted entirely (byte-parity with pre-785 when absent).
  _try_raw="$(user_summary "$_src_root/releases/${_vdisp}.md" "${DOGANY_LANG}_try")"
  _try="$(strip_tickets "$_try_raw")"
  # DGN-829: parse DOGANY_SECTION_GLYPHS (space-separated; empty/unset -> default).
  # Positional: 1st=summary, 2nd=detail, 3rd=try. Fewer than 3 -> bracket fallback.
  # Uses a subshell to avoid clobbering the script's own positional parameters ($@).
  _sg_raw="${DOGANY_SECTION_GLYPHS:-}"
  _sg1="" _sg2="" _sg3=""
  if [ -n "$_sg_raw" ]; then
    # subshell: split raw string, print one token per line, grab first three
    _sg_parsed="$(printf '%s\n' "$_sg_raw" | tr -s '[:space:]' '\n' | grep -v '^$' | head -3)"
    _sg1="$(printf '%s\n' "$_sg_parsed" | sed -n '1p')"
    _sg2="$(printf '%s\n' "$_sg_parsed" | sed -n '2p')"
    _sg3="$(printf '%s\n' "$_sg_parsed" | sed -n '3p')"
  fi
  # default palette when env unset or token absent (owner 2026-08-20: notice
  # sections use the neutral left-triangle to unify with the update-available
  # notice's "▸ Update notes" label; L0 reply section glyphs are a SEPARATE
  # default in bridge/config.py and stay checkmark/pin/clipboard).
  # 형님 확정 2026-08-24: 라벨을 <b>로 감싸고 글리프는 굵은 삼각 ▶.
  # 사유 = 기존 ▸ 는 본문 불릿 • 과 무게가 비슷해 섹션 머리와 항목이 헷갈렸다.
  # 채널 계층은 글리프+굵기로 낸다 (vendors/telegram.md Markup).
  # 참고: bridge/formatting.py:769 `_HEADER_GLYPHS = ("▪","◆","▸")` 가 H1/H2/H3+ 를
  # 매핑한다. 통지 라벨이 쓰던 ▸ 는 그 중 가장 깊은 H3 자리였다(계층 역전).
  # ◆(H2)로 올리는 안을 제시했으나 형님이 ▶ 를 선택 -- 형님 픽이 정본이다.
  [ -z "$_sg1" ] && _sg1="▶"
  [ -z "$_sg2" ] && _sg2="▶"
  [ -z "$_sg3" ] && _sg3="▶"
  if [ "$DOGANY_LANG" = "ko" ]; then
    _notice="재시작 완료 · ${_vdisp} 업데이트 완료"
    _label_summary="<b>${_sg1} 업데이트 요약</b>"
    _label_detail="<b>${_sg2} 업데이트 상세</b>"
    _label_try="<b>${_sg3} 새로 해볼 수 있는 것</b>"
  else
    _notice="Restart complete · ${_vdisp} update complete"
    _label_summary="<b>${_sg1} At a glance</b>"
    _label_detail="<b>${_sg2} What changed</b>"
    _label_try="<b>${_sg3} Try this</b>"
  fi
  # DGN-766: all sections render as ONE expandable blockquote (DGN-718 single-
  #   block principle). DGN-785: three sections -- summary / detail / try.
  #   Each section: [label]\n<body>. Sections separated by ONE blank line.
  #   Empty section -> label and body both omitted. All empty -> no blockquote.
  if [ -n "$_notes" ] || [ -n "$_detail" ] || [ -n "$_try" ]; then
    _body=""
    if [ -n "$_notes" ]; then
      _body="${_label_summary}
${_notes}"
    fi
    if [ -n "$_detail" ]; then
      if [ -n "$_body" ]; then
        _body="${_body}

${_label_detail}
${_detail_body}"
      else
        _body="${_label_detail}
${_detail_body}"
      fi
    fi
    if [ -n "$_try" ]; then
      _try_body="$_try"
      if [ -n "$_body" ]; then
        _body="${_body}

${_label_try}
${_try_body}"
      else
        _body="${_label_try}
${_try_body}"
      fi
    fi
    _notice="${_notice}
<blockquote expandable>${_body}</blockquote>"
  fi
  if "$_rs" --trigger "$TRIG" --delay 20 --reason "framework self-update to ${_vdisp}" --notice "$_notice"; then
    # DGN-687 notice #2 (install complete -- no release notes here; the
    # completion claim + notes belong to the restart-complete push).
    # DGN-697: short status lines are plain text (no blockquote); only the
    # release-notes fold stays expandable. Relayed verbatim as plain lines.
    printf '%s\n' "[self-update] relay the user notice below VERBATIM as the final line of this turn (DGN-687; do not translate/rewrite/code-block it):"
    # DGN-851: the install-complete line carries the PRODUCT name, which is
    # config data -- resolved from config/i18n key 'update.installed'
    # ({version} slot; lang -> en fallback, agentlib i18n() order). The
    # in-code copy below stays as the zero-delta fallback (locked ko copy)
    # for instances whose locale files lack the key or have no python3.
    _done_msg="$(/usr/bin/env python3 - "$SELF_ROOT" "$DOGANY_LANG" "$_vdisp" 2>/dev/null <<'PYEOF'
import json, os, sys
root, lang, ver = sys.argv[1], sys.argv[2], sys.argv[3]
for lg in (lang, "en"):
    try:
        path = os.path.join(root, "config", "i18n", lg + ".json")
        with open(path, encoding="utf-8") as fh:
            val = json.load(fh).get("update.installed")
    except Exception:
        continue
    if isinstance(val, str) and val:
        print(val.replace("{version}", ver))
        break
PYEOF
)" || _done_msg=""
    if [ -n "$_done_msg" ]; then
      printf '%s\n' "$_done_msg"
    elif [ "$DOGANY_LANG" = "ko" ]; then
      printf '%s\n' "도가니 업데이트 설치 완료 · ${_vdisp}
잠시 후 재시작합니다."
    else
      printf '%s\n' "Dogany update installed · ${_vdisp}
Restarting shortly."
    fi
  else
    # Launcher-stage failure/refusal only (exit 3 setup error, or idle-guard
    # deferral on --trigger auto). A detached-worker failure is covered by the
    # worker's own warning push, not here (grill m4).
    msg "[self-update] 재시작이 시작되지 않음(유예 또는 실패) -- 필요 시 수동: bridge/self_restart.sh --trigger user" \
        "[self-update] restart not started (deferred or failed) -- manual if needed: bridge/self_restart.sh --trigger user"
  fi
  return 0
}

# 3) Recover the framework repo root from the instance manifest.
#    Read DOGANY_REPO_ROOT without sourcing the whole conf (avoid importing
#    unrelated vars into this shell).
REPO_ROOT="$(sed -n 's/^DOGANY_REPO_ROOT=//p' "$CONF" | head -n1)"
[ -n "$REPO_ROOT" ] || die "DOGANY_REPO_ROOT missing from $CONF"
[ -d "$REPO_ROOT" ] || die "framework repo not found: $REPO_ROOT"
[ -f "$REPO_ROOT/update.sh" ] || die "update.sh not found in repo: $REPO_ROOT"

# 3b) Resolve the update source (DGN-593 A3).
#     Channel "release": per-run private tag extraction -- the shared repo is
#     only READ (ref read via fetch --tags, object read via git archive);
#     its working tree / HEAD / index are never moved.
#     Channel "main": pull --ff-only, guarded to the main branch.
UPDATE_CHANNEL="${DOGANY_UPDATE_CHANNEL:-$(sed -n 's/^DOGANY_UPDATE_CHANNEL=//p' "$CONF" | head -n1)}"
UPDATE_CHANNEL="${UPDATE_CHANNEL:-release}"
if [ -d "$REPO_ROOT/.git" ]; then
  # DGN-673 B2: pin persistence -- env wins, .instance.conf fallback; loud
  # PINNED banner on every pinned run (exported for the child update.sh).
  resolve_update_pin "$CONF" "$UPDATE_CHANNEL"
  if [ "$UPDATE_CHANNEL" = "main" ]; then
    # Main-branch guard (DGN-593 D3): pull only when the shared repo is
    # actually on main. Detached HEAD / dev branch means a dev session is in
    # progress -- die with a hint, never auto-switch.
    CUR_BRANCH="$(git -C "$REPO_ROOT" symbolic-ref -q --short HEAD || true)"
    if [ "$CUR_BRANCH" != "main" ]; then
      die "shared repo not on main (on '${CUR_BRANCH:-detached}') -- finish the dev session / restore main, then retry: $REPO_ROOT"
    fi
    msg "[self-update] 프레임워크 최신화: git pull --ff-only ($REPO_ROOT, channel=main)" \
        "[self-update] fetching latest framework: git pull --ff-only ($REPO_ROOT, channel=main)"
    git -C "$REPO_ROOT" pull --ff-only \
      || die "git pull --ff-only failed in $REPO_ROOT (resolve manually, then re-run)"
  else
    msg "[self-update] 프레임워크 최신화: 최신 릴리스 태그 ($REPO_ROOT)" \
        "[self-update] fetching latest framework: latest release tag ($REPO_ROOT)"
    git -C "$REPO_ROOT" fetch --tags origin \
      || die "git fetch failed in $REPO_ROOT (resolve manually, then re-run)"
    # Channel-aware target tag (DGN-621 v2 phase1, DGN-626 W1): release =
    # stable only; dev = stable|-dev.N whitelist; staging = stable|-rc.N
    # whitelist; unknown channel dies loud; DOGANY_UPDATE_PIN overrides (fails
    # loud if the pinned tag is missing). Stable-only estate on release == old
    # one-liner.
    LATEST_TAG="$(resolve_channel_tag "$REPO_ROOT" "$UPDATE_CHANNEL")"
    [ -n "$LATEST_TAG" ] || die "no release tag (v*) found in $REPO_ROOT"
    # -----------------------------------------------------------------------
    # RUN-LEVEL DOWNGRADE GUARD (DGN-1136) -- wired HERE, right after the
    # target tag is fixed and BEFORE any extraction work. The channel/pin
    # resolver above hands back whatever the channel math says; without this
    # check nothing compares that candidate against what is ALREADY installed,
    # so a channel whose top sits BEHIND this instance (subscriber drift)
    # silently downgrades the whole tree -- and update.sh's REVERSE-DRIFT
    # GUARD (DGN-249) is per-FILE (SKIP+WARN, run continues), which turns a
    # downgrade run into a TORN tree with a lying version stamp. That guard
    # stays; this is the RUN-level layer above it: candidate < installed
    # refuses the WHOLE run. Same verdict as the fleet planner's
    # BLOCKED-DRIFT predicate (scripts/pack/update_plan.sh: _ver_ok
    # "$cand" ">=$fw"), via the SAME single-source comparator
    # (scripts/pack/lib/semver_range.py -- no new semver copy: prefix-parse
    # X.Y.Z, pre-release suffix ignored, exit 0 satisfied / 1 not / 2 parse
    # error). Fires regardless of channel AND pin -- a pin is a target
    # selector, not downgrade consent. Escape hatch: DOGANY_ROLLBACK=1 (the
    # update.sh rollback lane, DGN-673 B3 -- same variable, no second
    # ledger) demotes the refusal to a loud warning and proceeds; update.sh's
    # own per-file consent (DOGANY_ROLLBACK_ACK) still applies downstream.
    # Every unknown is fail-CLOSED (missing DOGANY_FW_VERSION line,
    # unparseable version on either side, missing comparator): "don't know,
    # burn anyway" is the exact defect shape this guard exists to kill.
    # -----------------------------------------------------------------------
    DG_CAND_VER="${LATEST_TAG#v}"
    DG_INST_VER="$(sed -n 's/^DOGANY_FW_VERSION=//p' "$CONF" | head -n1)"
    DG_PIN_DISP="${DOGANY_UPDATE_PIN:-none}"
    if [ -z "$DG_INST_VER" ]; then
      msg "[self-update][오류] 하향 가드 (DGN-1136): $CONF 에 DOGANY_FW_VERSION 라인이 없습니다 -- 후보 ${LATEST_TAG} (채널=${UPDATE_CHANNEL}, 핀=${DG_PIN_DISP}) 가 하향이 아님을 증명할 수 없어 중단합니다 (fail-closed)" \
          "[self-update][ERROR] downgrade guard (DGN-1136): DOGANY_FW_VERSION not found in $CONF -- cannot prove candidate ${LATEST_TAG} (channel=${UPDATE_CHANNEL}, pin=${DG_PIN_DISP}) is not a downgrade, refusing (fail-closed)" >&2
      msg "  조치: .instance.conf 의 DOGANY_FW_VERSION=<X.Y.Z> 라인을 복원한 뒤 재실행하세요." \
          "  action: restore the DOGANY_FW_VERSION=<X.Y.Z> line in .instance.conf, then re-run." >&2
      die "downgrade guard (DGN-1136): DOGANY_FW_VERSION missing -- fail-closed"
    fi
    DG_SEMVER_LIB="$REPO_ROOT/scripts/pack/lib/semver_range.py"
    if [ ! -f "$DG_SEMVER_LIB" ]; then
      die "downgrade guard (DGN-1136): semver comparator not found ($DG_SEMVER_LIB) -- cannot prove candidate ${LATEST_TAG} is not a downgrade of installed v${DG_INST_VER}, refusing (fail-closed; bring the shared checkout forward so scripts/pack/lib/semver_range.py exists, then re-run)"
    fi
    dg_rc=0
    python3 "$DG_SEMVER_LIB" satisfies "$DG_CAND_VER" ">=$DG_INST_VER" >/dev/null || dg_rc=$?
    case "$dg_rc" in
      0)
        # candidate >= installed: forward update or same-version re-apply --
        # both are normal use; proceed untouched.
        ;;
      1)
        if [ "${DOGANY_ROLLBACK:-0}" = "1" ]; then
          printf '%s\n' "============================================================" >&2
          msg "[self-update][경고] ROLLBACK MODE (DOGANY_ROLLBACK=1) -- 하향 진행: 설치본 v${DG_INST_VER} -> 후보 ${LATEST_TAG} (채널=${UPDATE_CHANNEL}, 핀=${DG_PIN_DISP})" \
              "[self-update][WARN] ROLLBACK MODE (DOGANY_ROLLBACK=1) -- proceeding with DOWNGRADE: installed v${DG_INST_VER} -> candidate ${LATEST_TAG} (channel=${UPDATE_CHANNEL}, pin=${DG_PIN_DISP})" >&2
          msg "  update.sh 의 파일 단위 롤백 동의(DOGANY_ROLLBACK_ACK)는 하위 단계에서 계속 적용됩니다." \
              "  update.sh's per-file rollback consent (DOGANY_ROLLBACK_ACK) still applies downstream." >&2
          printf '%s\n' "============================================================" >&2
        else
          printf '%s\n' "============================================================" >&2
          msg "[self-update][오류] 하향 거부 (DGN-1136) -- 업데이트 후보가 설치본보다 낮습니다" \
              "[self-update][ERROR] DOWNGRADE REFUSED (DGN-1136) -- update candidate is OLDER than the installed framework" >&2
          msg "  설치본: v${DG_INST_VER}  |  후보: ${LATEST_TAG}  |  채널: ${UPDATE_CHANNEL}  |  핀: ${DG_PIN_DISP}" \
              "  installed: v${DG_INST_VER}  |  candidate: ${LATEST_TAG}  |  channel: ${UPDATE_CHANNEL}  |  pin: ${DG_PIN_DISP}" >&2
          msg "  원인: 해석된 업데이트 목적지(채널 top 또는 핀)가 설치본 뒤에 있습니다 (구독원 표류)." \
              "  cause: the resolved update target (channel top or pin) sits BEHIND the installed version (subscriber drift)." >&2
          msg "  조치: 채널/핀 선언을 점검하세요. 의도적 롤백이라면 DOGANY_ROLLBACK=1 로 재실행하세요 (update.sh 롤백 레인; 파일별 동의는 별도로 요구됩니다)." \
              "  action: check the channel/pin declaration. For a DELIBERATE rollback re-run with DOGANY_ROLLBACK=1 (update.sh rollback lane; per-file consent is still required downstream)." >&2
          printf '%s\n' "============================================================" >&2
          die "downgrade refused (DGN-1136): candidate ${LATEST_TAG} < installed v${DG_INST_VER} (channel=${UPDATE_CHANNEL}, pin=${DG_PIN_DISP}) -- set DOGANY_ROLLBACK=1 only for a deliberate rollback"
        fi
        ;;
      *)
        # exit 2 = comparator parse error (either side not X.Y.Z-prefixed);
        # anything else (e.g. 127 = no python3) is equally "cannot judge".
        # Deliberately NOT bypassable by DOGANY_ROLLBACK: rollback consents
        # to a KNOWN downgrade, not to an unjudgeable one.
        die "downgrade guard (DGN-1136): cannot compare versions (candidate '${DG_CAND_VER}' vs installed '${DG_INST_VER}', comparator rc=${dg_rc}) -- not provably a forward update, refusing (fail-closed; channel=${UPDATE_CHANNEL}, pin=${DG_PIN_DISP}; both sides must be X.Y.Z semver)"
        ;;
    esac
    # DGN-626 W1: hand the consumed tag NAME to the --no-pull child (the
    # extracted tree carries no .git, so the child cannot re-derive which
    # tag it came from) -- consumed by the DOGANY_FW_TAG landing stamp.
    DOGANY_UPDATE_SRC_TAG="$LATEST_TAG"
    export DOGANY_UPDATE_SRC_TAG
    # Per-run private source (DGN-593 A3): extract the tag into a temp dir.
    # The trap is installed IMMEDIATELY after mktemp, before any die below,
    # so the temp source can never leak (EXIT covers normal exit and die;
    # INT/TERM cover signals).
    SRC="$(mktemp -d "${TMPDIR:-/tmp}/dogany-src.XXXXXX")" \
      || die "mktemp failed for release source extraction"
    trap 'rm -rf "$SRC"' EXIT INT TERM
    # Two-step extraction (sh has no pipefail -> no pipe; check each step).
    git -C "$REPO_ROOT" archive --format=tar "$LATEST_TAG" > "$SRC/src.tar" \
      || die "git archive $LATEST_TAG failed in $REPO_ROOT"
    tar -xf "$SRC/src.tar" -C "$SRC" \
      || die "tar extraction of $LATEST_TAG failed"
    rm -f "$SRC/src.tar"
    # Extraction verification gate: never consume a half-extracted tree.
    if [ ! -f "$SRC/update.sh" ] || [ ! -d "$SRC/agents/.template" ]; then
      die "extracted source incomplete ($LATEST_TAG): $SRC"
    fi
    msg "[self-update] 릴리스 소스 추출: $LATEST_TAG" \
        "[self-update] release source extracted: $LATEST_TAG"
    # 4-release) Self-targeted, non-interactive update from the EXTRACTED
    #    tree. Child call (never exec -- exec would kill the EXIT trap and
    #    leak the temp dir); the child's exit status passes through.
    msg "[self-update] 이 인스턴스 업데이트: $SELF_ROOT" \
        "[self-update] updating this instance: $SELF_ROOT"
    rc=0
    # DGN-603 SF1: invoke via `bash` (not the extracted file's exec bit) so a
    # noexec TMPDIR mount cannot block the extracted update.sh from running.
    bash "$SRC/update.sh" --root "$SELF_ROOT" --no-pull --yes "$@" || rc=$?
    # DGN-685: successful update -> restart wiring (before the EXIT trap
    # cleans $SRC, so the release notes are still readable).
    maybe_restart "$rc" "$LATEST_TAG" "$SRC"
    exit "$rc"
  fi
else
  msg "[self-update] .git 없음 -> pull 건너뜀 (로컬 체크아웃 사용)" \
      "[self-update] no .git -> skipping pull (using local checkout)"
fi

# 4) main channel / no-.git: self-targeted, non-interactive update from the
#    repo checkout. --no-pull because we already synced above. Child call
#    (no exec, see step 4 note in the header); exit status passes through.
msg "[self-update] 이 인스턴스 업데이트: $SELF_ROOT" \
    "[self-update] updating this instance: $SELF_ROOT"
rc=0
"$REPO_ROOT/update.sh" --root "$SELF_ROOT" --no-pull --yes "$@" || rc=$?
# DGN-685: main / no-.git channel -- the just-pulled checkout IS the new
# version, so its VERSION file names what we updated to (grill m1).
MAIN_VER="$(head -n1 "$REPO_ROOT/VERSION" 2>/dev/null || true)"
maybe_restart "$rc" "${MAIN_VER:-unknown}" "$REPO_ROOT"
exit "$rc"
