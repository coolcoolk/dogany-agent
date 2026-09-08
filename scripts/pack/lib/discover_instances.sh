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
# and sits at one of the estate layout depths:
#   $HOME/.dogany/agents/*      shared-home agent roots
#   $HOME/dogany/*              mint_run.sh default mint root ($HOME/dogany/<Cap>)
#   $HOME/dogany/*/agents/*     crew layout
#   $HOME/dogany/*/poc/*        poc layout
# Archived roots one level deeper (e.g. dogany/poc/_archive/<x>) do not
# match by design. Display name = basename of the PHYSICAL root (pwd -P),
# so a symlink alias (agents/main -> agents/<leader>) collapses onto one
# entry. Output is LC_ALL=C sorted -> deterministic order. Fixed-depth
# globs only: no recursion, so a symlink cycle cannot loop the scan, and an
# unreadable directory just yields no match (callers keep their loud
# 0-count guards). A .instance.conf-less root is NOT an instance; a
# discovered instance WITHOUT DOGANY_PACKS is still judged downstream
# -- discovery never silently excludes.
_discover_instances() { # prints sorted "name:root" lines; rc 1 = HOME unusable
  local d phys name seen="|" out=""
  if [[ -z "${HOME:-}" || ! -d "${HOME:-}" ]]; then return 1; fi
  for d in "$HOME/.dogany/agents"/*/ "$HOME/dogany"/*/ \
           "$HOME/dogany"/*/agents/*/ "$HOME/dogany"/*/poc/*/; do
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
  done
  printf '%s' "$out" | LC_ALL=C sort
}
