#!/usr/bin/env bash
# Shared by install.sh and update.sh (DGN-2039). Bash 3.2 compatible.
# Root routines/ links to agents/.template/routines; mint and update ship this file.
# Callers provide DRY_RUN, ENABLE_BROWSER and ENABLE_COMPUTER_USE.

# At least 1 GiB on the home volume for the ~684 MB Chrome download plus
# extraction headroom. Unknown space is also skipped; never prompt.
default_browser_install() {
  [ "$ENABLE_BROWSER" = 1 ] || return 0
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] Would install agent-browser + Chrome for Testing (~684 MB; requires 1 GiB free)."
    return 0
  fi
  local available
  available="$(df -Pk "$HOME" 2>/dev/null | awk 'NR>1 && $2 ~ /^[0-9]+$/ {print $4; exit}')" || available=""
  case "$available" in ''|*[!0-9]*) available=0 ;; esac
  if [ "$available" -lt 1048576 ]; then
    echo "  [tools] Skipping browser: less than 1 GiB free or space unknown; add later: npm install -g agent-browser && agent-browser install"
    ENABLE_BROWSER=0
    return 0
  fi
  if ! command -v agent-browser >/dev/null 2>&1; then
    if ! command -v npm >/dev/null 2>&1 || ! npm install -g agent-browser; then
      echo "  [tools] Browser CLI install failed; retry: npm install -g agent-browser && agent-browser install" >&2
      ENABLE_BROWSER=0
      return 0
    fi
  fi
  # The CLI's installer reuses its cached Chrome. Ensure it even when the
  # CLI already exists: command presence alone does not mean Chrome exists.
  if ! agent-browser install; then
    echo "  [tools] Chrome install failed; retry: agent-browser install" >&2
    ENABLE_BROWSER=0
  fi
  return 0
}

default_browser_activate() {
  local root="$1" link="$1/.claude/skills/agent-browser"
  [ "$ENABLE_BROWSER" = 1 ] || return 0
  [ "$DRY_RUN" = 0 ] || return 0
  [ -d "$root/.claude/skills-bundle/agent-browser" ] || return 0
  # Preserve local skill directories and already active links.
  [ ! -e "$link" ] && [ ! -L "$link" ] || return 0
  mkdir -p "$root/.claude/skills" && ln -s ../skills-bundle/agent-browser "$link"
}

default_computer_use_install() {
  local root="$1" setup="$2" mac
  [ "$ENABLE_COMPUTER_USE" = 1 ] || return 0
  if [ "$(uname -s)" != Darwin ]; then
    echo "  [tools] Skipping Cua Driver: requires macOS 14+."
    return 0
  fi
  mac="$(sw_vers -productVersion 2>/dev/null)" || mac=""
  mac="${mac%%.*}"
  case "$mac" in ''|*[!0-9]*) mac=0 ;; esac
  if [ "$mac" -lt 14 ]; then
    echo "  [tools] Skipping Cua Driver: requires macOS 14+."
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    echo "  [dry-run] Would install Cua Driver and register screen control for $root."
    return 0
  fi
  # setup retains the pinned SHA-256 and Apple code-sign checks, repairs
  # missing CLI/policy/MCP pieces, and never starts the driver or grants TCC.
  if ! bash "$setup" install --apply --workspace "$root"; then
    echo "  [tools] Cua Driver setup failed; retry: bash \"$setup\" install --apply --workspace \"$root\"" >&2
  fi
  return 0
}
