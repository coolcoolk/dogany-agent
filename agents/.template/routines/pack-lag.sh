#!/bin/bash
# pack-lag.sh -- "am I behind on packs?", answered INSIDE the instance.
#
# DGN-1493 (instance-side half) / DGN-1417 (consumption-surface boundary).
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS AND WHY IT DOES NOT INSTALL ANYTHING
# ---------------------------------------------------------------------------
# The framework lifts itself: routines/self-update.sh is IN the instance, so
# an agent told to update itself just runs it. The pack lane had no such
# thing -- every installer (pack_install.sh / update_plan.sh / update_apply.sh)
# lives in the canonical repo only, so a pack falling behind was invisible
# from inside the instance and had to be noticed by a human at canonical.
# Measured consequence (DGN-1493): a main agent sat three cuts behind kit_life and
# nobody knew; a 2.20.1 landed untagged and nobody knew.
#
# This script closes the MEASUREMENT half and deliberately not the APPLY half.
# The boundary is write authority, not layer count:
#
#   measure  per-instance, read-only     -> here (an instance may always ask)
#   apply    crew-scoped, writes         -> update_plan.sh / update_apply.sh
#
# An instance must never self-apply a kit upgrade: a kit crew shares ONE
# physical DB and one skills tree, so update_plan.sh aborts the whole plan on
# member version divergence (scripts/pack/update_plan.sh:19-22, "crew
# discovery ... member version divergence (half-updated crew) = ABORT").
# A per-instance apply would BE that divergence -- the DGN-1407 lockstep break.
# So this script answers, loudly, and stops. Applying stays one estate-wide
# door. Adding a second apply door is the next drift, which is exactly what
# DGN-1493's work list warned about.
#
# ---------------------------------------------------------------------------
# WHAT IS READ, AND FROM WHERE (DGN-1493 work item 2)
# ---------------------------------------------------------------------------
#   1. <root>/config/packs/<id>.receipt   -- source=tag:pack/<id>/vX.Y.Z[-pre]
#      AUTHORITATIVE installed coordinate. The installer writes the receipt
#      BEFORE the ledger (pack_install.sh:4050-4051) and records the tag the
#      payload actually came from, pre-release suffix intact (DGN-1469,
#      DGN-1489). This is the only source that survives DGN-1469's defect
#      class, so it is tried FIRST.
#   2. <root>/.instance.conf DOGANY_PACKS=<id>@<ver>[,...]  -- the ledger
#      PROJECTION (install_evidence.sh header: "the receipt is the
#      authoritative record"). Used to ENUMERATE what this instance mounts,
#      and as the fallback coordinate for pre-receipt legacy installs
#      (measured: one instance carries dev@1.3.0 with no dev.receipt).
#   3. <root>/.instance.conf DOGANY_PACK_PINS=<id>=<tag>@<commit>  -- an exact
#      per-instance pin (DGN-1461). A pinned pack is measured against the PIN,
#      never against the channel head: silently following the channel past a
#      declared pin is the same silence this script exists to kill.
#   4. $DOGANY_SHARED_HOME/pack-sources.conf -- <id>.repo / <id>.channel /
#      <id>.pin. Where the published tags live and which channel this estate
#      subscribes to. NOT the framework canonical: pack tags live in each
#      pack's OWN repo, which is why this measurement needs no canonical at all.
#   5. git -C <repo> -c versionsort.suffix=- tag --list 'pack/<id>/v*'
#      --sort=-v:refname -- the published ladder.
#
# RANK BY LADDER POSITION, NEVER BY A NEW SEMVER COMPARATOR. update_plan.sh:28
# records that re-copying semver logic is a defect (it is single-sourced in
# scripts/pack/lib/semver_range.py, which is NOT shipped to instances). Both
# the installed coordinate and the candidate are TAGS in the same namespace,
# so their order is already decided by git's own version sort. Measured
# (2026-09-16, scratch repo): with -c versionsort.suffix=- the order is
# v2.21.0 > v2.21.0-dev.2 > v2.21.0-dev.1 > v2.20.3; WITHOUT the flag git
# ranks -dev.2 ABOVE the stable v2.21.0, i.e. the flag is load-bearing, not
# decoration (same finding as self-update.sh resolve_channel_tag, DGN-621).
#
# ---------------------------------------------------------------------------
# WHY A PRE-RELEASE IS NORMAL HERE (DGN-1493 acceptance criterion)
# ---------------------------------------------------------------------------
# Dogfooding is the declared procedure (dec-151), so an instance holding
# pack_health@0.11.5-dev.1 is CORRECT, not broken. Two rules make that hold:
#   - a pre-release installed coordinate is a first-class tag, ranked by the
#     same ladder (no "not semver x.y.z" rejection);
#   - an installed version that outranks its channel candidate is only DRIFT
#     when the installed coordinate is STABLE. A pre-release sitting above the
#     newest stable is PRERELEASE-AHEAD -- the expected dogfooding shape.
# The publisher-side 3-point lint's pre-release blindness is a DIFFERENT
# defect on a DIFFERENT input (it reads pack manifests, never instance
# receipts) and is owned by the publisher side -- not duplicated here. See the ticket.
#
# ---------------------------------------------------------------------------
# ANTI-SILENCE (DGN-1493 work item 3)
# ---------------------------------------------------------------------------
#   1. The footer is ALWAYS printed, on clean and on dirty runs.
#   2. An instance with mounts whose examined count is 0 is a FAILURE, not a
#      pass -- zero examined means the reader broke, not that all is well.
#      An instance with NO mounts prints an explicit fw-only line instead of
#      saying nothing (the two cases can never be confused).
#   3. Every pack prints its own verdict line; MISSING/UNREGISTERED verdicts
#      are worded so they cannot be skimmed as OK.
#   4. Any non-clean verdict NOTIFIES through routines/push.sh. A lag that
#      passes with no alert is the defect this script was written for, so
#      the notify call is on the success path of the measurement, not in an
#      error handler. WHO is notified depends on what was found:
#        BEHIND   -> the owner (update offer, or the approved owner push)
#        evidence -> the OPERATOR ONLY (DRIFT / UNTAGGED / UNREGISTERED /
#                    NO-REPO / NO-TAGS / PIN-MISSING). dec-220 (owner,
#                    2026-10-01): the owner cannot act on version-record
#                    problems, so they never reach the owner -- not as their
#                    own push, not riding along in the BEHIND push, and not
#                    via push.sh's operator-unresolved owner fallback
#                    (--no-owner-fallback). No operator resolvable (a normal
#                    user install) = the verdict lines in this run's log only.
#
# Transport seam (test only): DOGANY_PACKLAG_SINK, when set, captures the
# owner-bound body to that file and the operator-only body to
# "$DOGANY_PACKLAG_SINK.operator" instead of touching push.sh or a live
# channel -- same seam shape as generic-brief.sh's DOGANY_BRIEF_SINK. This is
# what makes the silence-reproduction test possible; live runs go through
# push.sh.
#
# Usage:  pack-lag.sh            # measure this instance, zero args
#         DOGANY_LANG=ko pack-lag.sh
#         pack-lag.sh --no-notify    # measure and print only
#
# Exit codes:
#   0  every mounted pack is CURRENT / PINNED-OK / PRERELEASE-AHEAD -- or, in
#      notify mode (the scheduled run), every finding was HANDED to its
#      audience (update-offer request left and/or pack-lag's own push
#      delivered), or an evidence-only finding was LOGGED because no
#      operator is resolvable (dec-220: log-only is its designed route there)
#   1  at least one pack is BEHIND / DRIFT / UNTAGGED / evidence-broken
#      (--no-notify, the lint use: always), or in notify mode a finding
#      whose delivery failed -- cron-guard's failure alarm is then the carrier
#   2  cannot measure (no .instance.conf, unreadable registry, zero examined
#      with mounts present) -- fail-closed, never reported as clean
# DGN-1808: the scheduled run went through cron-guard, which reads any
# nonzero as "the routine FAILED" and pushed "...작업이 방금 실패했어요" on
# every pack cut although the run measured, found the lag and raised the
# owner's update offer as designed. The finding already has its own channel
# (offer / own push), so a delivered finding is a successful RUN; a second,
# failure-worded alarm for it was the defect. Exit 2 is never softened.
# DGN-1808 r2 (dec-220): an evidence-only finding with NO operator is exit 0,
# not 1. cron-guard's failure alarm is itself --audience operator WITH the
# owner fallback, so exit 1 there would carry the very finding dec-220 keeps
# off the owner's screen back to it as "작업이 실패했어요". Logged = handled.
set -euo pipefail

LINT="pack-lag"

_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$_ROOT/.instance.conf"

DOGANY_LANG="${DOGANY_LANG:-en}"
msg() { if [ "$DOGANY_LANG" = "ko" ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi; }

_info() { echo "[$LINT] INFO: $*"; }
_fail() { echo "[$LINT] FAIL: $*" >&2; }
_warn() { echo "[$LINT] WARN: $*" >&2; }
# Verdict lines are column-aligned on purpose: a BEHIND must never be
# skimmable as a CURRENT (pack-version-lint.sh's _point idiom).
_verdict() { printf '[%s]   %-14s %-18s %s\n' "$LINT" "$1" "$2" "$3"; }

NOTIFY=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-notify) NOTIFY=0; shift ;;
    -h|--help)   sed -n '1,136p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) _fail "unknown option: $1"; exit 2 ;;
  esac
done

[[ -f "$CONF" ]] || {
  _fail "no .instance.conf at $_ROOT -- this is not a minted instance root (same gate as self-update.sh)"
  exit 2
}

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
SOURCES_CONF="${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}"

_conf_val() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | head -n1; }
# DGN-1744: the owner-facing agent name (identity "- Name:" first) from the
# single resolver; the mint-time conf keys only when it cannot run.
_agent_disp() {
  local v
  v="$(python3 "$_ROOT/routines/lib/agent_name.py" "$_ROOT" "$@" 2>/dev/null || true)"
  [[ -n "$v" ]] || { [[ "${1:-}" == "--no-label" ]] || v="$(_conf_val DOGANY_AGENT_LABEL)"; }
  [[ -n "$v" ]] || v="$(_conf_val DOGANY_AGENT_NAME)"
  printf '%s' "$v"
}

# ---------------------------------------------------------------------------
# Registry parse. Grammar and the duplicate-key rule are taken verbatim from
# scripts/pack/update_plan.sh:361-408 -- one registry, one reading of it.
# Duplicate key = FAIL, never a silent first-line-wins.
# ---------------------------------------------------------------------------
REG_IDS=() REG_REPOS=() REG_CHANNELS=() REG_PINS=()

_reg_idx() { # <id> -- prints index or -1
  local pid="$1" i=0
  while [[ "$i" -lt "${#REG_IDS[@]}" ]]; do
    [[ "${REG_IDS[$i]}" == "$pid" ]] && { echo "$i"; return 0; }
    i=$((i + 1))
  done
  echo "-1"
}

REGISTRY_PRESENT=0
if [[ -f "$SOURCES_CONF" ]]; then
  [[ -r "$SOURCES_CONF" ]] || { _fail "registry unreadable: $SOURCES_CONF -- refusing to report clean off an unread registry"; exit 2; }
  REGISTRY_PRESENT=1
  _id_re='^[a-z][a-z0-9_-]{0,31}$'
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    pid="${key%.*}";  field="${key##*.}"
    if [[ "$key" == "$pid" || ! "$pid" =~ $_id_re ]]; then
      _fail "registry line unparseable: '$line' ($SOURCES_CONF)"; exit 2
    fi
    case "$field" in repo|channel|pin) : ;;
      *) _fail "registry key unsupported: '$key' ($SOURCES_CONF)"; exit 2 ;; esac
    idx="$(_reg_idx "$pid")"
    if [[ "$idx" -lt 0 ]]; then
      REG_IDS+=("$pid"); REG_REPOS+=(""); REG_CHANNELS+=(""); REG_PINS+=("")
      idx=$(( ${#REG_IDS[@]} - 1 ))
    fi
    case "$field" in
      repo)    [[ -z "${REG_REPOS[$idx]}"    ]] || { _fail "registry duplicate key: $pid.repo (a duplicate is a FAIL, not a quiet first-line-wins)"; exit 2; }; REG_REPOS[$idx]="$val" ;;
      channel) [[ -z "${REG_CHANNELS[$idx]}" ]] || { _fail "registry duplicate key: $pid.channel"; exit 2; }; REG_CHANNELS[$idx]="$val" ;;
      pin)     [[ -z "${REG_PINS[$idx]}"     ]] || { _fail "registry duplicate key: $pid.pin"; exit 2; }; REG_PINS[$idx]="$val" ;;
    esac
  done < "$SOURCES_CONF"
  _info "registry: $SOURCES_CONF (${#REG_IDS[@]} pack source(s) declared)"
else
  _info "registry absent: $SOURCES_CONF (update_seed.sh has not run on this machine)"
fi

# ---------------------------------------------------------------------------
# Mount set. A MISSING DOGANY_PACKS line and an EMPTY one both mean "no packs"
# here, but the line is reported either way -- "0 packs" is a statement, never
# a silence.
# ---------------------------------------------------------------------------
PACKS_LINE="$(_conf_val DOGANY_PACKS)"
PPINS_LINE="$(_conf_val DOGANY_PACK_PINS)"

MOUNT_IDS=() MOUNT_VERS=()
if [[ -n "$PACKS_LINE" ]]; then
  IFS=',' read -r -a _items <<< "$PACKS_LINE"
  for it in ${_items[@]+"${_items[@]}"}; do
    [[ -n "$it" ]] || continue
    if [[ "$it" != *"@"* ]]; then
      _fail "DOGANY_PACKS entry '$it' is not <id>@<version> ($CONF)"; exit 2
    fi
    MOUNT_IDS+=("${it%%@*}"); MOUNT_VERS+=("${it#*@}")
  done
fi

_inst_pin() { # <pack_id> -- prints "<tag>" from DOGANY_PACK_PINS, or ""
  local pid="$1" e
  [[ -n "$PPINS_LINE" ]] || { printf ''; return 0; }
  local _pp=(); IFS=',' read -r -a _pp <<< "$PPINS_LINE"
  for e in ${_pp[@]+"${_pp[@]}"}; do
    [[ -n "$e" ]] || continue
    if [[ "${e%%=*}" == "$pid" ]]; then
      e="${e#*=}"; printf '%s' "${e%%@*}"; return 0   # drop the @<commit> half
    fi
  done
  printf ''
}

if [[ "${#MOUNT_IDS[@]}" -eq 0 ]]; then
  echo ""
  _info "examined 0 pack(s): this instance mounts NO packs (DOGANY_PACKS absent or empty in $CONF)"
  _info "framework-only instance -- pack lag is not applicable here, and that is a measured statement, not a skipped check"
  exit 0
fi

# ---------------------------------------------------------------------------
# Installed coordinate: receipt first, ledger second.
# ---------------------------------------------------------------------------
_receipt_tag() { # <pack_id> -- prints the tag ref from the receipt, or ""
  local pid="$1" r="$_ROOT/config/packs/$1.receipt" src
  [[ -f "$r" && -r "$r" ]] || { printf ''; return 0; }
  # An in-progress marker masks the receipt (install_evidence.sh report
  # 4.4.2): an unfinished install must never be measured as installed.
  [[ -f "$_ROOT/config/packs/$pid.receipt.inprogress" ]] && { printf ''; return 0; }
  src="$(sed -n 's/^source=tag://p' "$r" 2>/dev/null | head -n1)"
  printf '%s' "$src"
}

# ---------------------------------------------------------------------------
# Candidate: the same channel rule as update_plan.sh:243-265 (_pack_tag).
# stable excludes pre-releases by inspecting ONLY the version segment after
# the FINAL 'v' -- a hyphenated pack id like health-trainer must survive.
# ---------------------------------------------------------------------------
TAGS="" TAG_COUNT=0 CAND=""
_resolve() { # <repo> <id> <channel>
  local repo="$1" pid="$2" channel="$3" t seg
  CAND=""
  TAGS="$(git -C "$repo" -c versionsort.suffix=- tag --list "pack/$pid/v*" --sort=-v:refname 2>/dev/null || true)"
  if [[ -z "$TAGS" ]]; then TAG_COUNT=0; return 0; fi
  TAG_COUNT="$(printf '%s\n' "$TAGS" | grep -c . | tr -d ' ')"
  if [[ "$channel" == "dev" ]]; then
    CAND="$(printf '%s\n' "$TAGS" | head -n1)"
  else
    while IFS= read -r t; do
      seg="${t#pack/$pid/v}"
      [[ "$seg" == *-* ]] || { CAND="$t"; break; }
    done <<< "$TAGS"
  fi
}

_rank() { # <tag> -- prints 1-based position in TAGS (1 = newest), or "" if absent
  # `|| true` is load-bearing: a tag that is NOT on the ladder is the whole
  # point of the UNTAGGED verdict, and grep's no-match rc 1 would otherwise
  # kill the run under `set -e` -- turning the loudest finding into a silent
  # half-printed report. Measured 2026-09-16: without it the V6b/V8 cases
  # died right after the per-pack header with rc 1 and no verdict line.
  printf '%s\n' "$TAGS" | grep -nxF "$1" 2>/dev/null | head -n1 | cut -d: -f1 || true
}

PASS=0 BEHIND=0 BROKEN=0 EXAMINED=0
ALERT_LINES=""
BEHIND_LINES=""
_alert() { ALERT_LINES="${ALERT_LINES}$1"$'\n'; }
# DGN-1417 round 3: BEHIND is an owner-facing "an update exists" fact; on an
# estate host it rides the all-layer update notice instead of its own push.
_alert_behind() { BEHIND_LINES="${BEHIND_LINES}$1"$'\n'; }

i=0
while [[ "$i" -lt "${#MOUNT_IDS[@]}" ]]; do
  pid="${MOUNT_IDS[$i]}"; lver="${MOUNT_VERS[$i]}"; i=$((i + 1))
  EXAMINED=$((EXAMINED + 1))
  echo ""
  echo "[$LINT] ---- pack: $pid (ledger $lver) ----"

  ridx="$(_reg_idx "$pid")"
  if [[ "$ridx" -lt 0 ]]; then
    _verdict "UNREGISTERED" "-" "mounted but absent from $SOURCES_CONF -- no source to compare against, so lag CANNOT be measured (this is a failure, not a skip)"
    _alert "$pid: UNREGISTERED -- no source row in pack-sources.conf"
    BROKEN=$((BROKEN + 1)); continue
  fi
  repo="${REG_REPOS[$ridx]}"; channel="${REG_CHANNELS[$ridx]:-stable}"
  reg_pin="${REG_PINS[$ridx]}"; inst_pin="$(_inst_pin "$pid")"
  [[ -n "$channel" ]] || channel="stable"

  if [[ -z "$repo" || ! -d "$repo" ]]; then
    _verdict "NO-REPO" "-" "registry declares repo='$repo' which is not a directory -- source unreachable"
    _alert "$pid: NO-REPO -- ${repo:-<unset>} unreachable"
    BROKEN=$((BROKEN + 1)); continue
  fi

  rtag="$(_receipt_tag "$pid")"
  if [[ -n "$rtag" ]]; then
    inst_tag="$rtag"; coord_src="receipt"
  else
    inst_tag="pack/$pid/v$lver"; coord_src="ledger(no receipt)"
  fi

  _resolve "$repo" "$pid" "$channel"
  # A declared pin overrides the channel head. Instance pin wins over the
  # registry pin (the instance is the narrower declaration).
  pin="${inst_pin:-$reg_pin}"
  if [[ -n "$pin" ]]; then
    if printf '%s\n' "$TAGS" | grep -qxF "$pin"; then
      CAND="$pin"
    else
      _verdict "PIN-MISSING" "$pin" "declared pin is not a tag in $repo -- refusing to fall back to the channel head"
      _alert "$pid: PIN-MISSING -- $pin absent in $repo"
      BROKEN=$((BROKEN + 1)); continue
    fi
  fi

  if [[ "$TAG_COUNT" -eq 0 ]]; then
    _verdict "NO-TAGS" "-" "namespace pack/$pid/v* is EMPTY in $repo -- no release has ever been published for this pack"
    _alert "$pid: NO-TAGS -- nothing published in $repo"
    BROKEN=$((BROKEN + 1)); continue
  fi

  inst_rank="$(_rank "$inst_tag")"
  if [[ -z "$inst_rank" ]]; then
    # DGN-1489 shape: installed from something that was never tagged.
    _verdict "UNTAGGED" "$inst_tag" "installed coordinate ($coord_src) is not a published tag in $repo -- cannot be placed on the ladder ($TAG_COUNT tag(s))"
    _alert "$pid: UNTAGGED -- $inst_tag has no published tag"
    BROKEN=$((BROKEN + 1)); continue
  fi

  if [[ -z "$CAND" ]]; then
    # Channel has no eligible tag (e.g. stable channel, only pre-releases cut).
    # Holding a pre-release here is the DECLARED dogfooding state, not a break.
    _verdict "NO-CHANNEL-TAG" "channel=$channel" "no tag eligible for channel '$channel' among $TAG_COUNT tag(s); installed $inst_tag ($coord_src) stands -- nothing to be behind of"
    PASS=$((PASS + 1)); continue
  fi

  cand_rank="$(_rank "$CAND")"
  inst_seg="${inst_tag#pack/$pid/v}"

  if [[ "$inst_tag" == "$CAND" ]]; then
    if [[ -n "$pin" ]]; then
      _verdict "PINNED-OK" "$inst_tag" "declared pin satisfied ($coord_src, channel following suspended)"
    else
      _verdict "CURRENT" "$inst_tag" "newest on channel '$channel' ($coord_src, $TAG_COUNT tag(s))"
    fi
    PASS=$((PASS + 1))
  elif [[ "$inst_rank" -gt "$cand_rank" ]]; then
    _verdict "BEHIND" "$inst_tag" "-> $CAND (channel '$channel', $((inst_rank - cand_rank)) cut(s) back, $coord_src)"
    _alert_behind "$pid: BEHIND $((inst_rank - cand_rank)) cut(s) -- $inst_seg -> ${CAND#pack/$pid/v}"
    BEHIND=$((BEHIND + 1))
  elif [[ "$inst_seg" == *-* ]]; then
    # Installed coordinate is a PRE-RELEASE ranked above the channel
    # candidate. This is the declared dogfooding shape (dec-151), not a
    # break -- DGN-1493's acceptance criterion names exactly this case.
    _verdict "PRERELEASE-AHEAD" "$inst_tag" "pre-release ahead of channel '$channel' head $CAND -- dogfooding, NORMAL (not broken)"
    PASS=$((PASS + 1))
  else
    # A STABLE installed coordinate above the candidate is the drift shape
    # update_plan.sh:7 refuses to adopt ("구독원이 설치본보다 뒤").
    _verdict "DRIFT" "$inst_tag" "installed STABLE outranks channel '$channel' head $CAND -- the subscription source is behind the install"
    _alert "$pid: DRIFT -- installed $inst_seg outranks source head ${CAND#pack/$pid/v}"
    BROKEN=$((BROKEN + 1))
  fi
done

# ---------------------------------------------------------------------------
# Zero-examined gate. Mounts were present, so 0 examined means this reader
# broke -- never a clean result (pack-version-lint.sh's core rule, same shape).
# ---------------------------------------------------------------------------
if [[ "$EXAMINED" -eq 0 ]]; then
  echo ""
  _fail "examined 0 pack(s) while ${#MOUNT_IDS[@]} mount(s) are declared -- the reader is broken, this is NOT a clean run"
  exit 2
fi

echo ""
FOOTER="[$LINT] examined $EXAMINED pack(s): $PASS ok, $BEHIND behind, $BROKEN broken (registry: $([[ "$REGISTRY_PRESENT" -eq 1 ]] && echo present || echo ABSENT))"

# ---------------------------------------------------------------------------
# No-silence path. A non-clean measurement must reach the owner; a lag that
# passes with no alert IS the defect (DGN-1493). Transport is seam-injectable
# so the silence-reproduction test can assert the alert was raised.
# ---------------------------------------------------------------------------
_notify() { # <body>
  local body="$1" sink="${DOGANY_PACKLAG_SINK:-}"
  # Returns nonzero when the body was NOT delivered: the caller then keeps
  # exit 1 so cron-guard's alarm carries it instead (DGN-1808, never silent).
  if [[ -n "$sink" ]]; then printf '%s\n' "$body" >> "$sink" || return 1; return 0; fi
  local push="$_ROOT/routines/push.sh"
  if [[ ! -x "$push" && ! -f "$push" ]]; then
    _warn "cannot notify: $push not found -- printing only (the alert must never be lost in silence)"
    return 1
  fi
  bash "$push" --text "$body" --audience operator >/dev/null 2>&1 && return 0
  _warn "notify failed: push.sh returned nonzero -- the finding stands, only its delivery failed"
  return 1
}

# DGN-1808 r2 (dec-220): operator-ONLY send for evidence findings. Returns
# 0 delivered to the operator / 9 no operator resolvable, nothing sent, log
# line printed (by design -- never the owner) / 1 not delivered.
_notify_operator() { # <body>
  local body="$1" sink="${DOGANY_PACKLAG_SINK:-}" rc=0
  if [[ -n "$sink" ]]; then printf '%s\n' "$body" >> "$sink.operator" || return 1; return 0; fi
  local push="$_ROOT/routines/push.sh"
  if [[ ! -x "$push" && ! -f "$push" ]]; then
    _warn "cannot notify the operator: $push not found -- printing only"
    return 1
  fi
  bash "$push" --text "$body" --audience operator --no-owner-fallback >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) return 0 ;;
    9) _warn "evidence finding(s): no operator resolvable (STEWARD_ENV_FILE) -- logged here only, never sent to the owner (dec-220)"
       return 9 ;;
  esac
  _warn "operator notify failed: push.sh rc=$rc -- the finding stands, only its delivery failed"
  return 1
}

# DGN-1417 round 3 (owner rehearsal 2026-09-26 item 17): the daily pack-lag
# owner push is ABSORBED into the session-start update notice on an estate
# host (DOGANY_REPO_ROOT carries scripts/pack/update_surface.py). This script
# keeps measuring and printing; for BEHIND it leaves the same request the
# session-start hook leaves, and the bridge builds ONE notice for every layer
# from the planner after the owner's next reply (asked once per version set,
# so a lag already offered is not repeated). The notice is the owner's signal
# -- no second push for the same fact. Evidence problems (DRIFT / UNTAGGED /
# UNREGISTERED / ...) are not updates and keep their own push. A host without
# that machinery keeps the old push for BEHIND too (never silent).
_request_update_offer() {
  local repo req tmp
  repo="$(_conf_val DOGANY_REPO_ROOT)"
  [[ -n "$repo" && -f "$repo/scripts/pack/update_surface.py" ]] || return 1
  req="$_ROOT/.telegram_bot/state/update-offer-request"
  mkdir -p "$(dirname "$req")" 2>/dev/null || return 1
  tmp="$req.tmp.$$"
  printf '{"source": "pack-lag", "ts": %s}\n' "$(date +%s)" > "$tmp" 2>/dev/null \
    && mv -f "$tmp" "$req" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 1; }
}

if [[ $(( BEHIND + BROKEN )) -gt 0 ]]; then
  echo "$FOOTER" >&2
  echo "[$LINT] $(( BEHIND + BROKEN )) pack(s) need attention -- raise an update offer for the owner to pick in /update; pack updates are applied estate-wide" >&2
  if [[ "$NOTIFY" -eq 1 ]]; then
    _delivered=1
    _push_behind="$BEHIND"
    if [[ "$BEHIND" -gt 0 ]] && _request_update_offer; then
      echo "[$LINT] $BEHIND behind -> the owner's update notice (request left for the bridge; no separate push)" >&2
      _push_behind=0
    fi
    if [[ "$_push_behind" -gt 0 ]]; then
      # Owner-approved copy (2026-09-27, DGN-1747), verbatim: the agent
      # speaks in first person; the per-pack BEHIND lines (ids, versions)
      # move into the folded detail block. Evidence lines never ride along
      # here (dec-220) -- they take the operator-only send below.
      _hdr="$(msg "📦 $(_agent_disp)의 팩 ${_push_behind}개가 최신 버전보다 뒤처져 있어요."$'\n'"/update로 올릴 수 있어요." \
                  "📦 $(_agent_disp --no-label): ${_push_behind} pack(s) behind the latest version."$'\n'"/update can bring them up.")"
      _notify "$_hdr"$'\n'"$(msg ">! 기술 상세" ">! Technical detail")"$'\n'"$(printf '%s' "$BEHIND_LINES" | sed 's/^/> /')" \
        || _delivered=0
    fi
    if [[ "$BROKEN" -gt 0 ]]; then
      # Evidence problems (DRIFT / UNTAGGED / ...): operator only, never the
      # owner (dec-220). Technical text on purpose -- the operator reads it.
      _orc=0
      _notify_operator "[pack-lag] $(_agent_disp --no-label): ${BROKEN} pack evidence problem(s) -- version records do not match the published tags (operator-only, dec-220)"$'\n'"$ALERT_LINES" \
        || _orc=$?
      case "$_orc" in
        0) echo "[$LINT] $BROKEN evidence finding(s) -> the operator (owner not notified, dec-220)" >&2 ;;
        9) echo "[$LINT] $BROKEN evidence finding(s) logged only: no operator on this instance (dec-220; exit 0 by design)" >&2 ;;
        *) _delivered=0 ;;
      esac
    fi
    # DGN-1808: measured, and every finding reached its audience (or, for
    # evidence with no operator, was logged by design) -> the routine did
    # its job. Exit 0 so cron-guard stays silent instead of re-announcing the
    # finding as "작업이 실패했어요". --no-notify keeps 1.
    if [[ "$_delivered" -eq 1 ]]; then
      echo "[$LINT] findings handled -- the scheduled run succeeded (exit 0; --no-notify reports 1)" >&2
      exit 0
    fi
  fi
  exit 1
fi

echo "$FOOTER"
echo "[$LINT] every mounted pack is current for its channel (pre-releases counted as normal, not broken)"
exit 0
