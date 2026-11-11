#!/bin/bash
# v2-ctl.sh -- v2 module switch for this crew-leader instance (DGN-238
# decision 4: every v2 piece is a flag/module; off = full v1 restore, one
# switch).
#
# DGN-1513 SPEC v2: the activation link no longer targets a bundle copy of
# this module (root-relocation, v1 was blocked on the workdir-exposure
# grill). It targets $DOGANY_CONSUME_ROOT/skills/dogany-mint-agent -- a dedicated,
# detached `git worktree` of $DOGANY_REPO_ROOT under
# <instance>/.dogany-consume/, advanced ONLY by self-update.sh right after a
# successful framework update (routines/self-update.sh:advance_consume_checkout,
# reusing the already-resolved channel tag -- no new version logic here or
# there). This script's only jobs are: (1) create the worktree ONCE at
# whatever tag this instance JUST landed (bootstrap -- every advance after
# that is self-update's), and (2) flip the activation link.
#
# Runtime truth = the skill symlink .claude/skills/<module> (kit_life
# dormancy pattern, unchanged from v1). config/v2.conf records the declared
# state; this script reconciles declaration -> symlink. The weekly-review
# hook resolves through the same symlink, so removing it disables BOTH the
# conversational skill and the weekly recommendation lane.
#
# Usage: v2-ctl.sh {status|on|off} [module]     (module default: dogany-mint-agent)
#
# NOTE: 'on'/'off' change live agent behavior -- owner approval required
# (DGN-243 gate). DGN-1847 (dec-242): pre-approved for a MAIN instance --
# install.sh and update.sh call 'on' themselves (with V2CTL_AGENT_DIR set). Declaring DOGANY_CONSUME_ROOT in .instance.conf is a
# SEPARATE, prior owner gate (spec SS1.3) -- this script never writes that
# key, it only reads it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# DGN-1847: install.sh / self-update.sh run this script straight from a
# framework tree (no activation link exists yet to run it through), so the
# instance is named explicitly. Unset = the usual path-derived instance
# (script reached through <instance>/.claude/skills/<module>/scripts/).
if [[ -n "${V2CTL_AGENT_DIR:-}" ]]; then
  [[ -f "$V2CTL_AGENT_DIR/.instance.conf" ]] || { echo "ERROR: V2CTL_AGENT_DIR has no .instance.conf: $V2CTL_AGENT_DIR" >&2; exit 1; }
  AGENT_DIR="$(cd "$V2CTL_AGENT_DIR" && pwd)"
else
  AGENT_DIR="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
fi
CMD="${1:-status}"
MODULE="${2:-dogany-mint-agent}"

CONF_INST="$AGENT_DIR/.instance.conf"
LINK="$AGENT_DIR/.claude/skills/$MODULE"
CONF="$AGENT_DIR/config/v2.conf"
KEY="V2_$(printf '%s' "$MODULE" | tr 'a-z-' 'A-Z_')"

_conf_val() { # _conf_val <key> <file> -- first-match wins (self-update.sh convention)
  [[ -f "$2" ]] || return 0
  sed -n "s/^$1=//p" "$2" | head -n1
}

DOGANY_REPO_ROOT="$(_conf_val DOGANY_REPO_ROOT "$CONF_INST")"
DOGANY_CONSUME_ROOT="$(_conf_val DOGANY_CONSUME_ROOT "$CONF_INST")"

conf_set() { # conf_set <key> <value>
  mkdir -p "$AGENT_DIR/config"
  if [[ ! -f "$CONF" ]]; then
    {
      echo "# v2.conf -- per-instance v2 module flags (DGN-238 decision 4)."
      echo "# Managed by v2-ctl.sh; reconcile with: v2-ctl.sh {on|off} <module>."
      echo "# off = module fully inert (v1 behavior restored)."
    } > "$CONF"
  fi
  if grep -qE "^$1=" "$CONF"; then
    tmp="$(mktemp "$CONF.tmp.XXXXXX")"
    sed -E "s/^$1=.*/$1=$2/" "$CONF" > "$tmp" && mv -f "$tmp" "$CONF"
  else
    echo "$1=$2" >> "$CONF"
  fi
}

wr_patched() {
  grep -q "dogany-mint-agent module (DGN-243)" "$AGENT_DIR/routines/weekly-review.sh" 2>/dev/null
}

# _ensure_consume_gitignored -- DGN-1513 STEP-0 finding: this instance may
# itself be a private continuity repo (DGN-357, lead-instance pattern -- backup-data.sh
# / update.sh's own auto-commit both `git status`/`git add` inside $AGENT_DIR).
# A `git worktree` carries a `.git` FILE, not a dir; git treats that as a
# nested-repo boundary and shows it as ONE untracked line forever -- harmless
# by itself, but one unscoped `git add -A`/`git add .` on this repo (manual,
# or a future writer that does not diff against a pre-run baseline the way
# update.sh:_status_paths does) would stage it as a GITLINK (mode 160000),
# landing a phantom submodule reference into the owner's private history.
# Idempotent: only appends when $AGENT_DIR is itself a git repo AND the
# entry is not already present (any form -- exact line, trailing slash, or
# already covered by a broader existing pattern is left alone; this never
# rewrites a line the owner may have hand-edited).
_ensure_consume_gitignored() {
  local rel gi
  [[ -d "$AGENT_DIR/.git" ]] || return 0
  rel="$(basename "$DOGANY_CONSUME_ROOT")"
  gi="$AGENT_DIR/.gitignore"
  if [[ -f "$gi" ]] && grep -qxE "/?${rel}/?" "$gi"; then
    return 0
  fi
  {
    echo ""
    echo "# DGN-1513: consume checkout (git worktree, own .git file) -- never track"
    echo "/$rel/"
  } >> "$gi"
  echo "[v2-ctl] .gitignore: added /$rel/ (private continuity repo, DGN-1513 STEP-0)"
}

# _bootstrap_tag -- the tag/commit this instance JUST landed (update.sh's own
# DOGANY_FW_TAG stamp for the tag channels; the shared checkout's current
# HEAD for channel=main, where no tag exists). No new resolution: both are
# values update.sh / the shared repo already computed for THIS landing.
#
# DGN-1847: mint.sh stamps DOGANY_FW_TAG=unknown when the landed commit has no
# tag (main channel, unreleased line). That is "no tag", not a ref name --
# treat it like an absent stamp instead of handing git a ref that cannot
# resolve.
_bootstrap_tag() {
  local t
  t="$(_conf_val DOGANY_FW_TAG "$CONF_INST")"
  if [[ -n "$t" && "$t" != "unknown" ]]; then
    printf '%s\n' "$t"
    return 0
  fi
  git -C "$DOGANY_REPO_ROOT" rev-parse HEAD
}

case "$CMD" in
  status)
    conf_val="$(grep -E "^$KEY=" "$CONF" 2>/dev/null | head -1 | cut -d= -f2 || true)"
    echo "module         : $MODULE"
    echo "conf           : ${conf_val:-<absent>} ($CONF)"
    echo "consume root   : ${DOGANY_CONSUME_ROOT:-<not declared>}"
    if [[ -n "$DOGANY_CONSUME_ROOT" && -e "$DOGANY_CONSUME_ROOT/.git" ]]; then
      echo "consume tag    : $(git -C "$DOGANY_CONSUME_ROOT" describe --tags --always 2>/dev/null || echo '<unreadable>')"
    elif [[ -n "$DOGANY_CONSUME_ROOT" ]]; then
      echo "consume tag    : <checkout not created yet -- v2-ctl.sh on>"
    fi
    if [[ -L "$LINK" ]]; then echo "symlink        : ACTIVE ($LINK -> $(readlink "$LINK"))"; else echo "symlink        : absent (module inert)"; fi
    if wr_patched; then echo "weekly hook    : patched"; else echo "weekly hook    : NOT patched (see ACTIVATION.md)"; fi
    ;;
  on)
    [[ -n "$DOGANY_CONSUME_ROOT" ]] || { echo "ERROR: DOGANY_CONSUME_ROOT not declared in $CONF_INST -- owner gate (spec SS1.3), declare it first" >&2; exit 1; }
    [[ -n "$DOGANY_REPO_ROOT" ]] || { echo "ERROR: DOGANY_REPO_ROOT missing in $CONF_INST" >&2; exit 1; }
    [[ -d "$DOGANY_REPO_ROOT/.git" ]] || { echo "ERROR: not a git repo: $DOGANY_REPO_ROOT" >&2; exit 1; }
    if [[ -e "$DOGANY_CONSUME_ROOT/.git" ]]; then
      # DGN-1513 F-3: `.git` existing proves nothing -- a worktree's .git is
      # a FILE pointing into <repo>/.git/worktrees/..., and a moved or
      # re-cloned canonical repo leaves it dangling (every git op dies,
      # files stay at the last tag: fail-stale). Health-check before calling
      # it "present"; a MOVED repo is repairable in place (`worktree
      # repair`), a RE-CLONED repo lost the worktrees/ admin dir entirely --
      # only re-creation helps, and that is a delete, so it stays a guided
      # instruction, never automatic (the tree could carry unexpected
      # writes we must not destroy unseen).
      if git -C "$DOGANY_CONSUME_ROOT" status --porcelain >/dev/null 2>&1; then
        echo "[v2-ctl] consume checkout already present (healthy): $DOGANY_CONSUME_ROOT"
      else
        echo "[v2-ctl] consume checkout BROKEN (git status fails -- canonical repo moved or re-cloned?): $DOGANY_CONSUME_ROOT"
        echo "[v2-ctl] attempting 'git worktree repair' (covers the moved-repo case)..."
        if git -C "$DOGANY_REPO_ROOT" worktree repair "$DOGANY_CONSUME_ROOT" \
           && git -C "$DOGANY_CONSUME_ROOT" status --porcelain >/dev/null 2>&1; then
          echo "[v2-ctl] consume checkout repaired: $DOGANY_CONSUME_ROOT"
        else
          echo "ERROR: worktree repair failed -- the canonical repo was likely re-cloned (its .git/worktrees admin dir is gone). Recovery = re-create:" >&2
          echo "  1) inspect $DOGANY_CONSUME_ROOT for unexpected local changes (there should be NONE -- nobody writes there)" >&2
          echo "  2) rm -rf $DOGANY_CONSUME_ROOT" >&2
          echo "  3) re-run this command (v2-ctl.sh on) -- it re-creates the worktree at DOGANY_FW_TAG; the activation link targets the same path, so it stays valid" >&2
          echo "  (see ACTIVATION.md 'recovery' section)" >&2
          exit 1
        fi
      fi
    elif [[ -e "$DOGANY_CONSUME_ROOT" ]]; then
      echo "ERROR: $DOGANY_CONSUME_ROOT exists and is not a git worktree -- refusing to overwrite (contamination signal)" >&2
      exit 1
    else
      tag="$(_bootstrap_tag)"
      echo "[v2-ctl] creating consume checkout @ $tag: $DOGANY_CONSUME_ROOT"
      git -C "$DOGANY_REPO_ROOT" worktree add --detach "$DOGANY_CONSUME_ROOT" "$tag"
    fi
    _ensure_consume_gitignored
    if [[ ! -d "$DOGANY_CONSUME_ROOT/skills/$MODULE" ]]; then
      echo "ERROR: $DOGANY_CONSUME_ROOT/skills/$MODULE not found in the consumed checkout -- refusing to link a broken target" >&2
      exit 1
    fi
    conf_set "$KEY" on
    # DGN-1513 landing defect (2026-09-17): the old test was `! -e "$LINK"`,
    # which treats ANY existing path as done. The whole reason this step
    # exists is the migration case -- a link that is already there and still
    # points at the retired bundle -- so the one case it had to handle was
    # the one it skipped. Measured on the live landing: the link still read
    # ../skills-bundle/dogany-mint-agent while this script printed ON. This file's
    # own header says "Runtime truth = the skill symlink", so declaring ON
    # over a stale target is the script contradicting its own contract.
    # Compare the TARGET, not mere existence, and repoint when it differs.
    _want="$DOGANY_CONSUME_ROOT/skills/$MODULE"
    _have=""
    [[ -L "$LINK" ]] && _have="$(readlink "$LINK" 2>/dev/null || true)"
    if [[ ! -e "$LINK" && ! -L "$LINK" ]]; then
      ln -s "$_want" "$LINK"
      echo "[v2-ctl] symlink created: $LINK -> $_want"
    elif [[ -L "$LINK" && "$_have" == "$_want" ]]; then
      echo "[v2-ctl] symlink already points at the consumed checkout"
    elif [[ -L "$LINK" ]]; then
      # A symlink we can retarget atomically. The old target is named in the
      # log so the operator can see what was replaced.
      ln -sfn "$_want" "$LINK"
      echo "[v2-ctl] symlink repointed: $LINK -> $_want (was: ${_have:-unreadable})"
    else
      # A real directory/file, not a link -- retiring it is (iv)'s job and
      # doing it here would delete an instance surface without a record.
      echo "ERROR: $LINK exists and is not a symlink -- retire it first (step iv), refusing to overwrite an instance surface" >&2
      exit 1
    fi
    wr_patched || echo "[v2-ctl] WARN weekly-review.sh not patched -- recommendation lane off (see ACTIVATION.md)"
    echo "[v2-ctl] $MODULE = ON"
    ;;
  off)
    conf_set "$KEY" off
    if [[ -L "$LINK" ]]; then
      rm "$LINK"
      echo "[v2-ctl] symlink removed: $LINK"
    fi
    echo "[v2-ctl] $MODULE = OFF (v1 behavior restored; weekly hook self-guards on the absent symlink; consume checkout left in place for re-activation)"
    ;;
  *)
    echo "usage: v2-ctl.sh {status|on|off} [module]" >&2; exit 1 ;;
esac
