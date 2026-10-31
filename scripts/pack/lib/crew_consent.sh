# crew_consent.sh -- DGN-1506 item 2: the crew-pause consent token.
# Sourced (no shebang -- a library, not a runnable script), same contract as
# its sibling lib/crew_lease.sh: requires $CREW_DIR and $PACK_PY3 to already
# be set by the caller.
#
# WHY THIS EXISTS (DGN-1506 item 2, owner decision). The mint-agent skill
# takes the pause consent ONCE, up front (SKILL.md step 2), and step 6c is
# told "consent was taken at step 2 -> do NOT stop and ask again". Without a
# record, "was it taken?" is answered by the model recalling its own
# conversation -- exactly the shape the DGN-1506 doctrine forbids:
#   (a) a forcing point's INPUT must be a machine fact
#       ("the model believes it asked" is not a fact), and
#   (b) its OUTPUT must be machine-performed.
# This file is the (a) leg: the conversation stays the model's lane, but the
# YES it obtains is recorded on disk by a verb, and the crew-scoped apply
# (scripts/pack/update_apply.sh _apply_crew, gate id crew.consent) VERIFIES
# that record -- a record comparison, never a recollection. Same wiring
# shape as DGN-1509 SPEC v3 §3.4 (M4/N-d): a prior approval verb, run in the
# owner-conversation context, creates an artifact; the apply never prompts
# and only verifies -- absence/mismatch = refusal. An environment variable
# alone is NOT consent (spec §3.4 "환경변수 단독 무효") -- there is no bypass
# knob in this file or in the gate, deliberately: a bypass env would be set
# by the model, which is the recollection lane wearing a different hat.
#
# Layout: $CREW_DIR/<kit>/.consent/     (SIBLING of .lease/, never inside it:
#   members   one instance root per line  every _crew_lease_active reader --
#   token     version/kit/taken_at/       update_seed.sh M3, cron-guard --
#             pid/host                    treats .lease dir EXISTENCE as an
#                                         active transaction, so a consent
#                                         artifact stored inside .lease would
#                                         wrongly lock the whole kit out
#                                         between consent and apply)
#
# WHAT MAKES REUSE IMPOSSIBLE (the binding, DGN-1506 item 2 "a consent for
# one run must not authorize a later different run"):
#   1. single use  -- _crew_consent_consume deletes the token immediately
#      BEFORE the consented pause (update_apply T0-7 bridge stop). Even a
#      transaction that dies mid-run cannot leave a token behind that
#      authorizes a SECOND pause.
#   2. member-set binding -- the token snapshots crew.conf at consent time;
#      verify compares that set against the members the apply resolved. A
#      crew that changed in between is a DIFFERENT pause than the one the
#      user said yes to -> refuse (re-ask lane, see below). Strict equality
#      today: the only landed crew-scoped apply is the update lane, whose
#      own T0-1 already ABORTs on member drift. When the crew-JOIN
#      transaction lands (DGN-1509 S5+), its joiner set rides the lease
#      joiners artifact (spec v3 §3.4) and the comparison there must become
#      consented-members UNION lease-joiners -- recorded here so equality is
#      not read as the final contract.
#   3. expiry -- taken_at older than DOGANY_CREW_CONSENT_TTL_S (default
#      3600s) is invalid. Single use stops a second pause; the TTL stops a
#      never-consumed stale token from authorizing a pause days later.
# A refusal is NOT a dead end: the skill's own copy already defines the
# recovery ("step-2 consent not on record (resumed / late-entered flow) ->
# ask ONCE with the step-2 copy, then continue") -- the model asks once,
# runs the record verb again, and the run proceeds.

_crew_consent_dir() { # <kit>
  printf '%s/%s/.consent' "$CREW_DIR" "$1"
}

_crew_consent_ttl() {
  printf '%s' "${DOGANY_CREW_CONSENT_TTL_S:-3600}"
}

# _crew_consent_record <kit> -- snapshot crew.conf members + timestamp,
# atomically (tmp write + rename + fsync of files AND directory, token file
# LAST so its presence implies members is already durable -- same discipline
# as crew_lease.sh). Overwrites an existing token: a fresh YES from the user
# supersedes the old one (latest consent wins; there is nothing to protect
# in the old token -- unlike the lease, it records permission, not state).
# Refuses (rc 1, loud) when crew.conf is missing or lists zero members:
# consent to pause NOBODY is not a fact worth recording, and a crew apply
# for that kit would fail its own crew.conf gate anyway.
_crew_consent_record() {
  local kit="$1" conf dir members=""
  conf="$CREW_DIR/$kit/crew.conf"
  dir="$(_crew_consent_dir "$kit")"
  if [[ ! -f "$conf" ]]; then
    echo "[crew-consent] kit '$kit': crew.conf absent ($conf) -- nothing to consent to; seed the crew first (update_seed.sh)" >&2
    return 1
  fi
  local line
  while IFS= read -r line; do
    case "$line" in
      member=*) members="${members:+$members
}${line#member=}" ;;
      ''|'#'*) : ;;
      *) echo "[crew-consent] kit '$kit': crew.conf line unparseable: '$line' -- refusing to record over a suspect registry (fail-closed)" >&2
         return 1 ;;
    esac
  done < "$conf"
  if [[ -z "$members" ]]; then
    echo "[crew-consent] kit '$kit': crew.conf has zero members -- refusing to record (0 is suspicion, not consent)" >&2
    return 1
  fi
  "$PACK_PY3" - "$dir" "$kit" "$(date +%s)" "$$" "$(hostname 2>/dev/null || echo unknown)" "$members" <<'PYEOF'
import os, sys, tempfile
dir_, kit, taken_at, pid, host, members = sys.argv[1:7]
os.makedirs(dir_, exist_ok=True)


def atomic_write(name, data):
    fd, tmp = tempfile.mkstemp(prefix="." + name + ".tmp.", dir=dir_)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, os.path.join(dir_, name))


mtext = "\n".join(sorted(set(members.splitlines()))) + "\n"
atomic_write("members", mtext)
# token written LAST -- readers treat its presence as the verdict.
atomic_write("token", "version=1\nkit=%s\ntaken_at=%s\npid=%s\nhost=%s\n"
             % (kit, taken_at, pid, host))

dfd = os.open(dir_, os.O_RDONLY)
try:
    os.fsync(dfd)
finally:
    os.close(dfd)
PYEOF
}

# _crew_consent_verify <kit> <expected-members-newline-list>
# rc 0 = a valid, unexpired token exists whose member snapshot equals the
# expected set. Nonzero: prints ONE reason line on stdout beginning with a
# fixed keyword (MISSING / MALFORMED / FUTURE / EXPIRED / MEMBER-DRIFT) so
# callers can log a refusal that names exactly what is missing. Any
# unreadable/garbled token = MALFORMED (fail-closed, never fail-open).
_crew_consent_verify() {
  local kit="$1" expected="$2" dir taken_at now age ttl
  dir="$(_crew_consent_dir "$kit")"
  if [[ ! -f "$dir/token" || ! -f "$dir/members" ]]; then
    printf 'MISSING no consent token on record at %s\n' "$dir"
    return 1
  fi
  taken_at="$(sed -n 's/^taken_at=//p' "$dir/token" 2>/dev/null | head -n1)"
  case "$taken_at" in
    ''|*[!0-9]*)
      printf 'MALFORMED token has no numeric taken_at (%s/token)\n' "$dir"
      return 1 ;;
  esac
  now="$(date +%s)"
  # Small negative age = clock skew tolerance; beyond it the token claims to
  # be from the future -- a clock this wrong cannot support an expiry check.
  if (( taken_at > now + 60 )); then
    printf 'FUTURE token taken_at=%s is ahead of now=%s (clock suspect, fail-closed)\n' "$taken_at" "$now"
    return 1
  fi
  age=$(( now - taken_at ))
  ttl="$(_crew_consent_ttl)"
  if (( age > ttl )); then
    printf 'EXPIRED token age=%ss exceeds ttl=%ss (taken_at=%s)\n' "$age" "$ttl" "$taken_at"
    return 1
  fi
  local got want
  got="$(sort -u "$dir/members" 2>/dev/null | grep -v '^$')"
  want="$(printf '%s\n' "$expected" | sort -u | grep -v '^$')"
  if [[ -z "$got" ]]; then
    printf 'MALFORMED token members file is empty (%s/members)\n' "$dir"
    return 1
  fi
  if [[ "$got" != "$want" ]]; then
    printf 'MEMBER-DRIFT consented member set != apply-time member set (consented: %s | apply: %s)\n' \
      "$(printf '%s' "$got" | tr '\n' ',')" "$(printf '%s' "$want" | tr '\n' ',')"
    return 1
  fi
  return 0
}

# _crew_consent_consume <kit> <expected-members-newline-list>
# The single authorization point: re-verify (the verify at the top of the
# apply is a fail-fast courtesy; THIS one is the rule-lane check at the
# forcing point), then delete the token BEFORE the caller performs the
# consented pause. rc 0 = consumed (caller may stop bridges). Nonzero: the
# verify reason line is printed and NOTHING is removed -- an invalid token
# is left in place as evidence, never silently cleaned.
_crew_consent_consume() {
  local kit="$1" expected="$2" why
  if ! why="$(_crew_consent_verify "$kit" "$expected")"; then
    printf '%s\n' "$why"
    return 1
  fi
  rm -rf "$(_crew_consent_dir "$kit")"
}

# _crew_consent_revoke <kit> -- remove the token unconditionally (user said
# no after saying yes, or an operator resets state). Absent token = rc 0
# (revoking nothing is not an error).
_crew_consent_revoke() {
  rm -rf "$(_crew_consent_dir "$1")"
}
