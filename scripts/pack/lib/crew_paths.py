#!/usr/bin/env python3
"""crew_paths.py -- where the crew registry (crew.conf, staff.d/) lives, for
the mint flow's Python side (DGN-1967 step 12: one helper instead of the
screens / worker / readiness copies).

Order, the same as the shell side (pack_install.sh, crew_*.sh,
update_*.sh: CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}") plus the
caller-exported CREW_DIR / SHARED_HOME that mint_screens and mint_retire
already honoured:
    CREW_DIR > DOGANY_CREW_DIR > <shared home>/crews
    shared home = SHARED_HOME > DOGANY_SHARED_HOME > $HOME/.dogany

DOGANY_CREW_DIR moves only the registry. The crew DB symlink ground truth
stays <shared home>/crews/<kit>/<kit>.db (the update_plan.sh / crew_leave.sh
doctrine), which is why crew_kit_version.sh does not read it.
"""
import os
from pathlib import Path


def shared_home(env=None):
    e = os.environ if env is None else env
    return Path(e.get('SHARED_HOME') or e.get('DOGANY_SHARED_HOME')
                or os.path.join(e.get('HOME') or os.path.expanduser('~'), '.dogany'))


def crews_dir(env=None, shared=None):
    """The crew registry dir. `shared` overrides the shared home (a caller's
    own seam); CREW_DIR / DOGANY_CREW_DIR still win, as on the shell side."""
    e = os.environ if env is None else env
    explicit = e.get('CREW_DIR') or e.get('DOGANY_CREW_DIR')
    if explicit:
        return Path(explicit)
    return Path(shared or shared_home(e)) / 'crews'


def _conf_last(conf, key):
    """Last KEY= value of an .instance.conf, quotes stripped ('' if none)."""
    val = ''
    try:
        with open(conf, encoding='utf-8') as fh:
            for ln in fh:
                if ln.startswith(key + '='):
                    val = ln.rstrip('\n').split('=', 1)[1]
    except OSError:
        return ''
    return val.replace('"', '').replace("'", '')


def crew_role(root):
    """DGN-2040: 'leader' when the root's DOGANY_CREW_ROLE says so, else ''.
    Last value wins, quotes stripped, lowercased (pack_install block (1))."""
    return _conf_last(os.path.join(str(root), '.instance.conf'),
                      'DOGANY_CREW_ROLE').lower()


def seat_of(root):
    """DGN-2040: the root's DOGANY_SEAT_OF value ('' when not a seat)."""
    return _conf_last(os.path.join(str(root), '.instance.conf'), 'DOGANY_SEAT_OF')


def seat_owner(root):
    """DGN-2040: the leader root that owns this seat, or the root itself.
    A seat whose owner is gone or is not a leader resolves to itself, so a
    dead leader falls back to the DGN-1745 rule. Shell twin: crew_seat.sh
    _seat_owner. Returns a str."""
    root = str(root)
    owner = seat_of(root)
    if owner and os.path.isdir(owner) and crew_role(owner) == 'leader':
        return owner
    return root
