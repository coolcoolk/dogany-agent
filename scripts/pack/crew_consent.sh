#!/bin/bash
# crew_consent.sh -- DGN-1506 item 2: the prior-approval verb for the
# crew-pause consent (record / status / revoke).
#
# This is the DGN-1509 SPEC v3 §3.4 (N-d) wiring shape, applied to consent:
# the approval verb runs SEPARATELY, in the owner-conversation context (the
# mint-agent skill runs `record` right after the user's step-2 YES), and the
# crew-scoped apply (update_apply.sh _apply_crew, gate id crew.consent)
# never prompts -- it only verifies the artifact this verb wrote. The
# conversation is the model's lane; the record is the rule lane's.
#
# Verbs:
#   record --kit <kit>   snapshot crew.conf members + timestamp under
#                        $CREW_DIR/<kit>/.consent (atomic, fsync'd).
#                        Run this ONLY after the user's explicit yes to the
#                        pause-consent line -- this verb IS the record of
#                        that yes, not a convenience unlock.
#   status --kit <kit>   rc 0 + "VALID ..." iff a token exists, is
#                        unexpired, and matches the CURRENT crew.conf
#                        member set; otherwise rc 1 + the reason keyword
#                        (MISSING / MALFORMED / FUTURE / EXPIRED /
#                        MEMBER-DRIFT). Read-only.
#   revoke --kit <kit>   remove the token (user changed their mind).
#
# Env: DOGANY_SHARED_HOME / DOGANY_CREW_DIR (layout override, same
# resolution as update_apply.sh), DOGANY_CREW_CONSENT_TTL_S (default 3600).
# There is deliberately NO bypass/force env -- an env var alone is not
# consent (spec §3.4 doctrine; see lib/crew_consent.sh header).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY_RESOLVE_LIB="$SCRIPT_DIR/lib/py_resolve.sh"
[[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
  || { echo "ERROR: python resolver library missing ($PY_RESOLVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/py_resolve.sh
. "$PY_RESOLVE_LIB"
pack_resolve_py3 || exit 1

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"

CONSENT_LIB="$SCRIPT_DIR/lib/crew_consent.sh"
[[ -f "$CONSENT_LIB" && -r "$CONSENT_LIB" ]] \
  || { echo "ERROR: consent library missing ($CONSENT_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/crew_consent.sh
. "$CONSENT_LIB"

usage() {
  echo "usage: crew_consent.sh {record|status|revoke} --kit <kit>" >&2
  exit 2
}

VERB="${1:-}"; shift || true
KIT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kit) KIT="${2:-}"; shift 2 || usage ;;
    *) usage ;;
  esac
done
[[ -n "$KIT" ]] || usage
case "$KIT" in
  */*|.*) echo "ERROR: kit name '$KIT' is not a bare kit id" >&2; exit 2 ;;
esac

# Current crew.conf member set (status verifies against the live registry --
# the same source _apply_crew re-reads at T0-1, so status' verdict predicts
# the gate's).
_conf_members() {
  local conf="$CREW_DIR/$KIT/crew.conf" line out=""
  [[ -f "$conf" ]] || return 0
  while IFS= read -r line; do
    case "$line" in
      member=*) out="${out:+$out
}${line#member=}" ;;
    esac
  done < "$conf"
  printf '%s' "$out"
}

case "$VERB" in
  record)
    if _crew_consent_record "$KIT"; then
      echo "[crew-consent] recorded: kit=$KIT ttl=$(_crew_consent_ttl)s members:"
      sed 's/^/[crew-consent]   /' "$(_crew_consent_dir "$KIT")/members"
      echo "[crew-consent] the crew-scoped apply (update_apply.sh) will verify and consume this token; it is single-use and expires"
    else
      exit 1
    fi
    ;;
  status)
    if why="$(_crew_consent_verify "$KIT" "$(_conf_members)")"; then
      taken="$(sed -n 's/^taken_at=//p' "$(_crew_consent_dir "$KIT")/token" | head -n1)"
      echo "VALID kit=$KIT taken_at=$taken age=$(( $(date +%s) - taken ))s ttl=$(_crew_consent_ttl)s"
    else
      echo "$why"
      exit 1
    fi
    ;;
  revoke)
    _crew_consent_revoke "$KIT"
    echo "[crew-consent] revoked: kit=$KIT (absent token counts as revoked)"
    ;;
  *) usage ;;
esac
