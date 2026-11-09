#!/usr/bin/env bash
# Install/uninstall the ONE managed maintenance grant. Run at install time.
# Never touch owner-managed dogany-fseventsd or any other sudoers entry.
# No password pipes, variables or logging; sudo owns its terminal prompt.
set -euo pipefail
if [ "$(uname -s)" != Darwin ]; then
  echo 'maintenance=not-supported platform=non-macOS'
  exit 3
fi
action="${1:-}"
case "$action" in install|uninstall) ;; *) echo 'usage: mac-maintenance-sudoers.sh install|uninstall' >&2; exit 2 ;; esac
account="$(id -un)"
if [ "$account" = root ]; then account="${SUDO_USER:-}"; fi
case "$account" in ALL|''|*[!a-zA-Z0-9_.-]*) echo 'maintenance=invalid-account' >&2; exit 2 ;; esac
dest=/etc/sudoers.d/dogany-maintenance
stage=''
locked=0
local_rule="$(mktemp "${TMPDIR:-/tmp}/dogany-maintenance.XXXXXX")"
cleanup() {
  rm -f "$local_rule"
  if [ -n "$stage" ]; then sudo -n -- /bin/rm -f "$stage" >/dev/null 2>&1 || true; fi
  if [ "$locked" = 1 ]; then sudo -n -- /bin/rmdir /etc/sudoers.d/.dogany-maintenance.lock >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
chmod 600 "$local_rule"
printf '%s ALL=(root) NOPASSWD: /usr/bin/killall fseventsd\n' "$account" > "$local_rule"
# Check the whole configuration before and after publication, as well as the
# isolated candidate. The dot-prefixed staging file is ignored by includedir.
sudo -- /usr/sbin/visudo -c >/dev/null
sudo -- /bin/mkdir -p /etc/sudoers.d
if ! sudo -- /bin/mkdir /etc/sudoers.d/.dogany-maintenance.lock; then
  echo 'maintenance=busy-or-lock-unavailable' >&2
  exit 1
fi
locked=1
if sudo -- /bin/test -e "$dest" || sudo -- /bin/test -L "$dest"; then
  if sudo -- /bin/test -L "$dest" || ! sudo -- /usr/bin/cmp -s "$local_rule" "$dest"; then
    echo 'maintenance=refused-unmanaged-file' >&2
    exit 1
  fi
  if [ "$action" = install ]; then
    sudo -- /usr/sbin/visudo -cf "$dest" >/dev/null
    sudo -- /usr/sbin/chown root:wheel "$dest"
    sudo -- /bin/chmod 0440 "$dest"
    echo 'maintenance=already-installed'
    exit 0
  fi
elif [ "$action" = uninstall ]; then
  echo 'maintenance=already-absent'
  exit 0
fi

stage="$(sudo -- /usr/bin/mktemp /etc/sudoers.d/.dogany-maintenance.XXXXXX)"
if [ "$action" = uninstall ]; then
  sudo -- /bin/mv "$dest" "$stage"
  if ! sudo -- /usr/sbin/visudo -c >/dev/null; then
    sudo -- /bin/mv "$stage" "$dest"
    stage=''
    echo 'maintenance=uninstall-rolled-back' >&2
    exit 1
  fi
  echo 'maintenance=uninstalled'
else
  sudo -- /usr/bin/install -o root -g wheel -m 0440 "$local_rule" "$stage"
  sudo -- /usr/sbin/visudo -cf "$stage" >/dev/null
  sudo -- /bin/mv "$stage" "$dest"
  stage=''
  if ! sudo -- /usr/sbin/visudo -c >/dev/null; then
    sudo -- /bin/rm -f "$dest"
    echo 'maintenance=install-rolled-back' >&2
    exit 1
  fi
  echo 'maintenance=installed'
fi
