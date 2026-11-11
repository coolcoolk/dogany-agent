"""JSON CLI. Only the explicit token command prints an access token."""
import argparse
import json
import os

from . import disconnect, _access_token, set_default, status, store
from .errors import GoogleAuthError, render
from . import flow
from .migration import import_gws


def parser():
    p = argparse.ArgumentParser()
    commands = p.add_subparsers(dest='command', required=True)
    for name in ('status', 'disconnect', 'token'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--account')
        cmd.add_argument('--json', action='store_true')
        if name == 'token':
            cmd.add_argument('--scopes', required=True)
    connect = commands.add_parser('connect').add_subparsers(dest='action', required=True)
    for name in ('start', 'finish'):
        cmd = connect.add_parser(name)
        cmd.add_argument('--origin', required=True)
        cmd.add_argument('--json', action='store_true')
        if name == 'start':
            cmd.add_argument('--scopes', required=True)
            cmd.add_argument('--account')
            cmd.add_argument('--locale', choices=('en', 'ko'),
                             default=os.environ.get('AGENT_LANG', 'en'))
        else:
            cmd.add_argument('--url', required=True)
    cmd = commands.add_parser('import-gws')
    cmd.add_argument('--dry-run', action='store_true')
    cmd = commands.add_parser('set-default')
    cmd.add_argument('sub')
    cmd = commands.add_parser('_listen')
    cmd.add_argument('state')
    cmd.add_argument('fd', type=int)
    # Internal bridge integration hooks; do not change the frozen public verbs.
    cmd = commands.add_parser('_expired-prompt')
    cmd.add_argument('--origin', required=True)
    cmd = commands.add_parser('_blocked-screen')
    cmd.add_argument('--origin', required=True)
    cmd.add_argument('--answer', choices=('block', 'missed'), required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'status':
            result = status(args.account)
        elif args.command == 'token':
            cache = _access_token(args.scopes.split(','), args.account)
            result = {key: cache[key] for key in ('access_token', 'expires_at')}
        elif args.command == 'disconnect':
            result = disconnect(args.account)
        elif args.command == 'set-default':
            result = set_default(args.sub)
        elif args.command == 'import-gws':
            result = import_gws(args.dry_run)
        elif args.command == '_listen':
            flow.listen(args.state, args.fd)
            return 0
        elif args.command == '_expired-prompt':
            result = {'key': flow.expired_prompt(args.origin)}
        elif args.command == '_blocked-screen':
            result = flow.record_blocked_screen(args.origin, args.answer == 'block')
        elif args.action == 'start':
            result = flow.start(args.scopes.split(','), args.origin, args.account)
            result.update(ok=True, waiting_for='google-connect-result',
                          copy=[render('google.login.' + key, args.locale)
                                for key in ('start', 'unverified_hint', 'paste_hint')])
        else:
            result = flow.finish(args.url, args.origin)
    except GoogleAuthError as exc:
        result = {'ok': False, 'class': exc.cls}
    except (OSError, ValueError, KeyError, TypeError):
        result = {'ok': False, 'class': 'auth_unknown'}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get('ok', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
