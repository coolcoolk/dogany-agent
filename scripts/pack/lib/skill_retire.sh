# skill_retire.sh -- declaration-vs-reality skill removal (DGN-1422).
# Sourced library (no shebang for the sourced half; a direct-execution guard
# at the bottom adds an operator restore CLI, prev_backup_prune.sh
# precedent). Requires python3.
#
# THE GAP THIS CLOSES: DGN-1409 made a NEWLY declared skill activate
# (payload skills-bundle/ -> instance skills-bundle/ -> .claude/skills/
# symlink). Nothing on the removal side ever ran: a kit/pack upgrade whose
# new pack-manifest.json simply stops shipping a skills-bundle/<name>/
# directory leaves every trace of the OLD skill exactly as active as before
# -- pack_install.sh has no memory of what it installed LAST time, so it has
# nothing to compare the new payload against and nothing ever prunes.
#
# THE FIX SHAPE: pack_install.sh persists the skill-bundle names it installs
# on every run to config/packs/<pack-id>.skills (skill_retire_write_declared,
# same precedent as _reqfw_record's .requires_framework file). The NEXT
# install reads that file BEFORE overwriting it -- names present there but
# absent from the new payload are "declared removed" (skill_retire_diff).
# Each is retired (skill_retire_apply): NEVER a bare rm -- everything that
# carries bytes moves under
#   <root>/.claude/skills-retired/<name>.<ts>/
# and everything that is a symlink has its target recorded in a
# ".retired-manifest" file in that same archive dir BEFORE the link is
# removed, so skill_retire_restore can recreate the exact symlink (not a
# copy) as well as move real directories back -- "심링크는 타입까지
# 왕복 가능하게" (design constraint 1).
#
# SURFACES ONE RETIRE SWEEPS (checklist cross-reference, DGN-1422 ticket):
#   1. instance activation link   <root>/.claude/skills/<name>
#   2. instance skills-bundle body/link <root>/.claude/skills-bundle/<name>
#   3. crew-shared canonical      <shared_root>/<name>            (kit only)
#   4. crew-shared .prev-* backups tied to that canonical         (kit only;
#      DGN-1395's writer-prunes-its-own-backups doctrine only ever runs from
#      inside _kit_share_skill, which never runs again for a retired name --
#      without this sweep those backups become permanently unreachable)
#   5. instance .pre-share-* conversion backups tied to the bundle body
#
# WHY THIS NEVER ABORTS THE INSTALL: a stale skill left slightly longer is a
# nuisance; an install that dies half-way through PRUNING is a much worse
# failure mode for something that already finished the real work. Same
# "cleanup never fails the caller" doctrine as pack_deps_provision.sh and
# prev_backup_prune.sh. Every mutating step below is guarded so a single
# permission error WARNs and moves on instead of propagating under the
# caller's `set -e`.

# DGN-1424: shared python3 resolver (PATH-independent -- see the lib file
# header for why a bare `python3` call is unsafe on a human's own terminal).
_PY_RESOLVE_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/py_resolve.sh"
# shellcheck source=py_resolve.sh
. "$_PY_RESOLVE_LIB"

# skill_retire_diff <declared_file> <new_names_newline_separated>
# Prints names present in <declared_file> (one name per line; '#'-prefixed
# lines are comments) but ABSENT from <new_names_newline_separated>, sorted,
# one per line. A missing <declared_file> means "nothing declared before" --
# empty output (a first-ever install retires nothing, correctly).
skill_retire_diff() {
  local declared_file="$1" new_names="$2"
  local tmp
  # WHY A TEMP FILE, NOT A PIPE: `python3 - <<'PYEOF'` already uses stdin to
  # feed the SCRIPT SOURCE to the interpreter -- a `printf ... | python3 -`
  # pipe on the same command line is silently discarded (the heredoc
  # redirection wins the fd, and by the time the script body runs, stdin is
  # already at EOF from being fully consumed as source). Measured: this
  # made `new` deterministically empty on every call, so a byte-identical
  # re-install retired everything it had ever declared. A second argv path
  # sidesteps the conflict entirely.
  tmp="$(mktemp)"
  printf '%s\n' "$new_names" > "$tmp"
  pack_resolve_py3 || return 1
  "$PACK_PY3" - "$declared_file" "$tmp" <<'PYEOF'
import sys
declared_file, new_file = sys.argv[1], sys.argv[2]
old = set()
try:
    with open(declared_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            old.add(line)
except FileNotFoundError:
    pass
new = set()
with open(new_file, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            new.add(line)
for name in sorted(old - new):
    print(name)
PYEOF
  rm -f "$tmp"
}

# skill_retire_write_declared <declared_file> <new_names_newline_separated>
# Atomic (tmp-in-same-dir + mv) overwrite of the declared-skills record with
# the set this install just installed. Called ONLY on a real (non-dry-run)
# run, and only AFTER retirement of the diff has been attempted -- a
# dry-run must leave this file untouched (dry-run doctrine, design
# constraint 5).
skill_retire_write_declared() {
  local file="$1" new_names="$2"
  local dir tmp
  dir="$(dirname "$file")"
  mkdir -p "$dir"
  tmp="$file.tmp.$$"
  {
    printf '# <pack-id>.skills -- skill-bundle names this pack declared at its most\n'
    printf '# recent install (DGN-1422). Read by the NEXT install to find names\n'
    printf '# dropped from the manifest; not consumed anywhere else.\n'
    printf '%s\n' "$new_names" | awk 'NF' | LC_ALL=C sort -u
  } > "$tmp"
  mv "$tmp" "$file"
}

# skill_retire_other_owner <root> <name> <self_pack_id>
# DGN-1678: ownership guard for the retire lane. A name dropped from ONE
# pack's declaration is only really orphaned when nobody else declares it:
# the kit (or a sibling pack) may have started shipping the same skill
# name, and the instance dir <root>/.claude/skills*/<name> is then THEIR
# live asset, not this pack's leftover. Reuses the existing ownership
# records -- no new registry:
#   <root>/config/packs/<id>.skills    every installed kit/pack (DGN-1422)
#   <root>/.claude/.dogany-bundle-skills.sha   framework lifekit bundle
#                                               skills ("<name>  <sha>")
# <self_pack_id>'s own record is skipped (it is the one dropping the name).
# Prints the first other owner's id and returns 0 when one holds <name>;
# prints nothing and returns 1 when no other owner remains (safe to archive).
skill_retire_other_owner() {
  local root="$1" name="$2" self="$3"
  local f id
  for f in "$root/config/packs"/*.skills; do
    [[ -f "$f" ]] || continue
    id="$(basename "$f" .skills)"
    [[ "$id" == "$self" ]] && continue
    if grep -qxF -- "$name" "$f" 2>/dev/null; then
      printf '%s\n' "$id"
      return 0
    fi
  done
  f="$root/.claude/.dogany-bundle-skills.sha"
  if [[ -f "$f" ]] && awk -v n="$name" '$1 == n { found = 1 } END { exit !found }' "$f" 2>/dev/null; then
    printf 'framework-bundle\n'
    return 0
  fi
  return 1
}

# _skill_retire_inspect <root> <shared_root> <name>
# THE single predicate skill_retire_plan (dry-run) and skill_retire_apply
# (real) both read -- so a dry-run's plan and the real run's actions can
# never diverge (same doctrine as pack_install.sh's _rk_compute_verdict
# dry-run/real split). <shared_root> may be "" (class=pack has none).
# Emits KEY=VALUE lines.
_skill_retire_inspect() {
  local root="$1" shared_root="$2" name="$3"
  local bundle="$root/.claude/skills-bundle/$name"
  local link="$root/.claude/skills/$name"
  local p n

  if [[ -L "$bundle" ]]; then
    echo "bundle_kind=link"
    echo "bundle_target=$(readlink "$bundle")"
  elif [[ -d "$bundle" ]]; then
    echo "bundle_kind=dir"
  else
    echo "bundle_kind=absent"
  fi

  if [[ -L "$link" ]]; then
    echo "link_kind=link"
    echo "link_target=$(readlink "$link")"
  elif [[ -e "$link" ]]; then
    echo "link_kind=other"
  else
    echo "link_kind=absent"
  fi

  n=0
  for p in "$root/.claude/skills-bundle/$name".pre-share-*; do
    [[ -e "$p" ]] || continue
    n=$((n + 1))
  done
  echo "pre_share_backups=$n"

  if [[ -n "$shared_root" ]]; then
    if [[ -d "$shared_root/$name" ]]; then
      echo "shared_kind=dir"
    else
      echo "shared_kind=absent"
    fi
    n=0
    for p in "$shared_root/$name".prev-*; do
      [[ -e "$p" ]] || continue
      n=$((n + 1))
    done
    echo "shared_prev_backups=$n"
  else
    echo "shared_kind=na"
    echo "shared_prev_backups=0"
  fi
}

_skill_retire_field() { # _skill_retire_field <info> <key>
  printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1
}

# skill_retire_plan <root> <shared_root> <name>
# Dry-run: prints what WOULD happen, touches nothing.
skill_retire_plan() {
  local root="$1" shared_root="$2" name="$3"
  local info bundle_kind link_kind pre_n shared_kind shared_prev_n
  info="$(_skill_retire_inspect "$root" "$shared_root" "$name")"
  bundle_kind="$(_skill_retire_field "$info" bundle_kind)"
  link_kind="$(_skill_retire_field "$info" link_kind)"
  pre_n="$(_skill_retire_field "$info" pre_share_backups)"
  shared_kind="$(_skill_retire_field "$info" shared_kind)"
  shared_prev_n="$(_skill_retire_field "$info" shared_prev_backups)"

  echo "$name: RETIRE PLAN (dropped from this pack's manifest)"
  case "$link_kind" in
    link)   echo "$name: would remove activation link .claude/skills/$name (target recorded first)" ;;
    other)  echo "$name: WARN .claude/skills/$name exists but is not our symlink -- would leave untouched" ;;
    absent) echo "$name: activation link already absent -- no-op" ;;
  esac
  case "$bundle_kind" in
    link)   echo "$name: would archive+remove instance skills-bundle/ symlink (share mode; target recorded first)" ;;
    dir)    echo "$name: would archive instance skills-bundle/ real dir (own mode)" ;;
    absent) echo "$name: instance skills-bundle/ already absent -- no-op" ;;
  esac
  [[ "${pre_n:-0}" -gt 0 ]] && echo "$name: would sweep $pre_n .pre-share-* backup(s) into the archive"
  [[ "$shared_kind" == "dir" ]] && echo "$name: would archive crew-shared canonical at \$SHARED_ROOT/$name"
  [[ "${shared_prev_n:-0}" -gt 0 ]] && echo "$name: would sweep $shared_prev_n crew-shared .prev-* backup(s) into the archive"
}

# skill_retire_apply <root> <shared_root> <name> <ts>
# Real run: archives (never bare-deletes) then removes the live surfaces.
# Prints a log line per action taken; the caller (_log) owns formatting.
# Never returns non-zero -- every mutating step is best-effort with a WARN
# on failure (cleanup must not fail an otherwise-successful install).
skill_retire_apply() {
  local root="$1" shared_root="$2" name="$3" ts="$4"
  local bundle="$root/.claude/skills-bundle/$name"
  local link="$root/.claude/skills/$name"
  local archive="$root/.claude/skills-retired/$name.$ts"
  local did=0 p

  echo "$name: RETIRE (dropped from this pack's manifest)"

  if [[ -L "$link" ]]; then
    if mkdir -p "$archive" \
        && printf 'activation_link_target=%s\n' "$(readlink "$link")" >> "$archive/.retired-manifest" \
        && rm -f "$link"; then
      did=1
      echo "$name: removed activation link .claude/skills/$name (target recorded)"
    else
      echo "$name: WARN failed to archive/remove activation link .claude/skills/$name -- left in place"
    fi
  elif [[ -e "$link" ]]; then
    echo "$name: WARN .claude/skills/$name exists but is not our symlink -- left untouched"
  fi

  if [[ -L "$bundle" ]]; then
    if mkdir -p "$archive" \
        && printf 'skills_bundle_link_target=%s\n' "$(readlink "$bundle")" >> "$archive/.retired-manifest" \
        && rm -f "$bundle"; then
      did=1
      echo "$name: removed instance skills-bundle/ symlink (share mode; target recorded)"
    else
      echo "$name: WARN failed to archive/remove instance skills-bundle/ symlink -- left in place"
    fi
  elif [[ -d "$bundle" ]]; then
    if mkdir -p "$archive" && mv "$bundle" "$archive/skills-bundle-body"; then
      did=1
      echo "$name: archived instance skills-bundle/ -> $archive/skills-bundle-body"
    else
      echo "$name: WARN failed to archive instance skills-bundle/ -- left in place"
    fi
  fi

  for p in "$root/.claude/skills-bundle/$name".pre-share-*; do
    [[ -e "$p" ]] || continue
    if mkdir -p "$archive/pre-share-backups" && mv "$p" "$archive/pre-share-backups/$(basename "$p")"; then
      did=1
      echo "$name: swept $(basename "$p") -> $archive/pre-share-backups/"
    else
      echo "$name: WARN failed to sweep $(basename "$p") -- left in place"
    fi
  done

  if [[ -n "$shared_root" ]]; then
    if [[ -d "$shared_root/$name" ]]; then
      if mkdir -p "$archive" && mv "$shared_root/$name" "$archive/crew-shared-canonical"; then
        did=1
        echo "$name: archived crew-shared canonical -> $archive/crew-shared-canonical"
      else
        echo "$name: WARN failed to archive crew-shared canonical -- left in place"
      fi
    fi
    for p in "$shared_root/$name".prev-*; do
      [[ -e "$p" ]] || continue
      if mkdir -p "$archive/crew-shared-prev-backups" && mv "$p" "$archive/crew-shared-prev-backups/$(basename "$p")"; then
        did=1
        echo "$name: swept crew-shared $(basename "$p") -> $archive/crew-shared-prev-backups/"
      else
        echo "$name: WARN failed to sweep crew-shared $(basename "$p") -- left in place"
      fi
    done
  fi

  if [[ "$did" -eq 0 ]]; then
    echo "$name: nothing on disk to retire (already clean) -- declaration-only drop"
  else
    echo "$name: retire archive at $archive"
  fi
}

# skill_retire_restore <root> <shared_root> <name> <archive_dir>
# Operator command (NOT wired into any install path -- prev_backup_prune.sh
# --reconcile precedent): reverses one skill_retire_apply archive. Symlinks
# are recreated from the recorded target (never a copy of the archive's
# metadata file), real directories are moved back verbatim.
skill_retire_restore() {
  local root="$1" shared_root="$2" name="$3" archive="$4"
  local manifest="$archive/.retired-manifest" tgt p

  [[ -d "$archive" ]] || { echo "$name: RESTORE FAILED -- archive not found: $archive"; return 1; }

  if [[ -f "$manifest" ]]; then
    tgt="$(sed -n 's/^skills_bundle_link_target=//p' "$manifest" | head -1)"
    if [[ -n "$tgt" ]]; then
      mkdir -p "$root/.claude/skills-bundle"
      ln -sfn "$tgt" "$root/.claude/skills-bundle/$name"
      echo "$name: restored skills-bundle/ symlink -> $tgt"
    fi
    tgt="$(sed -n 's/^activation_link_target=//p' "$manifest" | head -1)"
    if [[ -n "$tgt" ]]; then
      mkdir -p "$root/.claude/skills"
      ln -sfn "$tgt" "$root/.claude/skills/$name"
      echo "$name: restored activation link -> $tgt"
    fi
  fi

  if [[ -d "$archive/skills-bundle-body" ]]; then
    mkdir -p "$root/.claude/skills-bundle"
    mv "$archive/skills-bundle-body" "$root/.claude/skills-bundle/$name"
    echo "$name: restored instance skills-bundle/ real dir"
  fi

  if [[ -d "$archive/crew-shared-canonical" ]]; then
    [[ -n "$shared_root" ]] || { echo "$name: RESTORE WARN -- crew-shared-canonical archived but no shared_root given"; }
    if [[ -n "$shared_root" ]]; then
      mkdir -p "$shared_root"
      mv "$archive/crew-shared-canonical" "$shared_root/$name"
      echo "$name: restored crew-shared canonical"
    fi
  fi

  if [[ -d "$archive/pre-share-backups" ]]; then
    for p in "$archive/pre-share-backups"/*; do
      [[ -e "$p" ]] || continue
      mv "$p" "$root/.claude/skills-bundle/$(basename "$p")"
    done
    echo "$name: restored .pre-share-* backup(s)"
  fi

  if [[ -d "$archive/crew-shared-prev-backups" && -n "$shared_root" ]]; then
    for p in "$archive/crew-shared-prev-backups"/*; do
      [[ -e "$p" ]] || continue
      mv "$p" "$shared_root/$(basename "$p")"
    done
    echo "$name: restored crew-shared .prev-* backup(s)"
  fi

  echo "$name: restore complete from $archive"
}

# Direct-execution guard: operator restore CLI.
#   skill_retire.sh --restore <root> <shared_root|-> <name> <archive_dir>
# <shared_root> as a literal "-" means "no crew-shared root" (class=pack).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  if [[ "${1:-}" != "--restore" || $# -ne 5 ]]; then
    echo "usage: skill_retire.sh --restore <root> <shared_root|-> <name> <archive_dir>" >&2
    exit 1
  fi
  _r_root="$2" _r_shared="$3" _r_name="$4" _r_archive="$5"
  [[ "$_r_shared" == "-" ]] && _r_shared=""
  skill_retire_restore "$_r_root" "$_r_shared" "$_r_name" "$_r_archive"
fi
