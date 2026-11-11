#!/usr/bin/env python3
"""DGN-2147: refuse a bot token that another installed agent already uses.

Two agents on one bot token fight over getUpdates (Telegram 409 Conflict) and
neither receives messages reliably. mint.sh calls this before it writes the
new instance's .env.

usage: DOGANY_BOT_TOKEN=<token> token_dup_check.py --root <target-root>
exit 0  token free (or no token given)
exit 3  another agent on this machine already has it; the agent's name and
        root are printed on stdout, one "name<TAB>root" line per holder
The token is read from the environment only and never printed.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mint_token  # noqa: E402


def holders(token, target_root, roots=None):
    me = Path(target_root).resolve()
    out = []
    for root in (roots if roots is not None else mint_token.discover_roots()):
        try:
            if Path(root).resolve() == me:
                continue
            if mint_token.instance_token(root) == token:
                out.append((Path(root).resolve().name, str(Path(root).resolve())))
        except (mint_token.InventoryError, OSError):
            continue
    return out


def main(argv):
    root = argv[argv.index('--root') + 1] if '--root' in argv else ''
    token = os.environ.get('DOGANY_BOT_TOKEN', '')
    if not token or not root:
        return 0
    try:
        found = holders(token, root)
    except mint_token.InventoryError:
        return 0        # an unreadable estate must not block a first install
    for name, path in found:
        print('%s\t%s' % (name, path))
    return 3 if found else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
