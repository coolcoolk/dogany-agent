# discover_instances.sh -- HOME-relative estate instance discovery (DGN-1037).
# Sourced library (no shebang, no side effects). Consumers: update_plan.sh
# (plan-target discovery) and update_apply.sh (fixed-sender resolution for
# the completion/abort notice). Extracted from update_plan.sh so both sides
# share ONE truth source instead of two drifting scans.
#
# The former hardcoded owner-estate table leaked the owner's account name +
# private estate topology into the public mirror AND could never match
# another user's disk. Replaced by a HOME-relative scan (mint_run.sh
# precedent: every estate path there is already derived from $HOME).
#
# Discovery rule -- a directory is an instance iff it carries .instance.conf
# and is named by one of these sources (union, authoritative first):
#   $HOME/.dogany/instances     install registry, "<class>\t<root>" lines --
#                               install.sh registry_upsert + mint_run.sh
#                               g3b_align_domain write it (the record of
#                               every root the installer made, wherever
#                               --root put it)
#   $HOME/.dogany/lite_instance install.sh primary-instance pointer (always
#                               written, even when the registry is not)
#   $DOGANY_DISCOVER_SELF_ROOT  the asking instance's own root (the bridge
#                               sets it for its /update lookup: an instance
#                               asking about updates is by definition one,
#                               wherever mint.sh --root put it)
#   fixed-depth estate scan (fallback for roots no record names):
#   $HOME/.dogany/main          install.sh default root (DGN-1483)
#   $HOME/.dogany/agents/*      shared-home agent roots (mint_run.sh default)
#   $HOME/dogany/*              legacy mint root ($HOME/dogany/<Cap>)
#   $HOME/dogany/*/agents/*     crew layout
#   $HOME/dogany/*/poc/*        poc layout
# Before the record sources were read (rehearsal 2026-10-02) every
# install.sh install -- root ~/.dogany/main -- was invisible here: the
# planner aborted on "0 instances" and the bot told a fresh owner the
# update list could not be loaded. A registry line whose root is gone or
# has no .instance.conf just drops (stale record != instance).
# Archived roots one level deeper (e.g. dogany/poc/_archive/<x>) do not
# match by design. Display name = basename of the PHYSICAL root (pwd -P),
# so a symlink alias (agents/main -> agents/<leader>) collapses onto one
# entry. Output is LC_ALL=C sorted -> deterministic order. Fixed-depth
# globs only: no recursion, so a symlink cycle cannot loop the scan, and an
# unreadable directory just yields no match (callers keep their loud
# 0-count guards). A .instance.conf-less root is NOT an instance; a
# discovered instance WITHOUT DOGANY_PACKS is still judged downstream
# -- discovery never silently excludes.
#
# Test injection (DGN-1517): DOGANY_DISCOVER_INSTANCES_OVERRIDE, when set,
# replaces the scan below with its own "name:root,..." value (same format
# this function prints, comma-separated instead of newline-separated). This
# is a DIFFERENT variable from DOGANY_PLAN_INSTANCES (update_plan.sh /
# update_apply.sh): those two callers already intercept DOGANY_PLAN_INSTANCES
# themselves and never call into this function when it is set, so adding the
# override here does not touch them. It exists because pack_install.sh's
# crew lockstep guard (_kit_crew_lockstep_guard) calls _discover_instances
# directly with no seam of its own -- before this, a 2+ member crew scenario
# could not be expressed in a test at all (the guard always saw the real
# $HOME, which a tmp fixture is never part of). Reusing DOGANY_PLAN_INSTANCES
# for this would leak into every existing DOGANY_PLAN_INSTANCES-driven crew
# fixture (it is inherited by the pack_install.sh subprocess update_apply.sh
# spawns) and silently change already-passing tests' verdicts as a side
# effect of opening this seam. A dedicated variable keeps "nothing injected"
# byte-identical to today for every caller that does not know it exists.
_discover_instances() { # prints sorted "name:root" lines; rc 1 = HOME unusable
  if [[ -n "${DOGANY_DISCOVER_INSTANCES_OVERRIDE:-}" ]]; then
    printf '%s\n' "${DOGANY_DISCOVER_INSTANCES_OVERRIDE//,/$'\n'}"
    return 0
  fi
  local d phys name seen="|" out=""
  if [[ -z "${HOME:-}" || ! -d "${HOME:-}" ]]; then return 1; fi
  while IFS= read -r d; do
    if [[ -z "$d" ]]; then continue; fi
    d="${d%/}/"
    if [[ ! -d "$d" || ! -f "${d}.instance.conf" ]]; then continue; fi
    phys="$(cd "$d" 2>/dev/null && pwd -P)" || continue
    if [[ -z "$phys" ]]; then continue; fi
    case "$seen" in *"|$phys|"*) continue ;; esac
    seen="$seen$phys|"
    name="$(basename "$phys")"
    case "$name" in
      *[:,]*)
        echo "[discover] SKIP: instance dirname contains ':' or ',' (name:root format reserved): $phys" >&2
        continue ;;
    esac
    out="$out$name:$phys"$'\n'
  done < <(_discover_instance_candidates)
  printf '%s' "$out" | LC_ALL=C sort
}

# _discover_instance_candidates -- every root the sources above name, one per
# line, unfiltered (the caller applies the .instance.conf + dedupe rule).
_discover_instance_candidates() {
  local h="$HOME/.dogany" d
  if [[ -f "$h/instances" ]]; then
    awk -F'\t' 'NF >= 2 && $2 ~ /^\// { print $2 }' "$h/instances" 2>/dev/null || true
  fi
  if [[ -f "$h/lite_instance" ]]; then
    d="$(sed -n '1{s/[[:space:]]*$//;p;}' "$h/lite_instance" 2>/dev/null || true)"
    case "$d" in /*) printf '%s\n' "$d" ;; esac
  fi
  case "${DOGANY_DISCOVER_SELF_ROOT:-}" in /*) printf '%s\n' "$DOGANY_DISCOVER_SELF_ROOT" ;; esac
  for d in "$h/main/" "$h/agents"/*/ "$HOME/dogany"/*/ \
           "$HOME/dogany"/*/agents/*/ "$HOME/dogany"/*/poc/*/; do
    printf '%s\n' "$d"
  done
}
