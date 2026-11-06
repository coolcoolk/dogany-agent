# persona_resolver.sh -- pack-engine persona write-target resolver
# (DGN-773 R5/T4b). Sourced library (no shebang, no side effects).
#
# A 2.0 instance carries PROFILE.md as the real identity file, with AGENT.md
# demoted to a compat symlink (AGENT.md -> PROFILE.md, 1-version window,
# R7). A pre-2.0 instance still carries AGENT.md as the only real file.
# Every pack-engine read/write site resolves through this function instead
# of hardcoding "$ROOT/AGENT.md" -- naive AGENT.md paths silently regress
# to reading/writing a stale symlink shadow on a 2.0 instance (grill G2
# false-FAIL) or clobbering the compat link outright (see below).
#
# Resolving straight to PROFILE.md (the real target) rather than through
# the AGENT.md symlink path is also what keeps a tmp+rename write safe: a
# `mv tmp "$(resolve_persona_md "$ROOT")"` on a 2.0 instance renames onto
# PROFILE.md itself, never touching the AGENT.md symlink path at all, so
# the compat link survives. Renaming onto the symlink path directly
# (`mv tmp "$ROOT/AGENT.md"`) is the T4b bug: rename-over-symlink replaces
# the link with a real file (mode regressed, content diverged from
# PROFILE.md) -- confirmed by measurement in DGN-773's pack_install.sh
# --upgrade excise step. truncate-write (`>`/`>>`/Python write_text)
# preserves the symlink either way, but callers should still resolve
# through this function so pre-2.0 instances (no PROFILE.md) are targeted
# correctly.
resolve_persona_md() { # resolve_persona_md <root>
  # DGN-1141 stage-5 relayout: a relaid-out instance carries the persona as
  # identity/hot.custom.agent.md with PROFILE.md demoted to a compat symlink.
  # Resolve to the REAL file first for the same reason PROFILE.md outranks
  # the AGENT.md symlink above: a tmp+rename write onto a symlink path
  # replaces the link with a diverged real file (the T4b bug). 3-way accept:
  # identity/hot.custom.agent.md (relayout) > PROFILE.md (2.0) > AGENT.md (pre-2.0).
  local root="$1"
  if [[ -f "$root/identity/hot.custom.agent.md" ]]; then
    printf '%s\n' "$root/identity/hot.custom.agent.md"
  elif [[ -f "$root/PROFILE.md" ]]; then
    printf '%s\n' "$root/PROFILE.md"
  else
    printf '%s\n' "$root/AGENT.md"
  fi
}
