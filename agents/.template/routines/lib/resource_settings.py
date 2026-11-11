#!/usr/bin/env python3
"""Account resource settings shared by every agent on this host.

Stores: $DOGANY_RESOURCE_SETTINGS_DIR/<api-credit|codex>/<account fp>/settings.json.
The root defaults to ~/.dogany/resource-settings; tests must override it.
Only fingerprints are stored. API fingerprints match api-credit/<fp>; Codex
fingerprints match reset-passes/codex/<fp>. No account means no shared write.

CLI examples (run from an installed instance):
  python3 routines/lib/resource_settings.py set api-credit --reset-day 6 --workspace .
  python3 routines/lib/resource_settings.py set codex --max-used-percent 90
  python3 routines/lib/resource_settings.py show api-credit --workspace .
  python3 routines/lib/resource_settings.py show codex
Use --account <12-hex-fingerprint> to select an account explicitly. No command
prints or stores API secrets. --api-key-stdin accepts a privately piped key
for fresh onboarding before a book pointer exists. Other reads use only public
account metadata. The API account comes from the workspace book pointer, or
from api-credit/current.json when no workspace was supplied. A Codex CLI read
uses auth.json; passive callers must pass their cached account explicitly.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

SPECS = {"api-credit": ("reset_day", 1, 28, 1),
         "codex": ("max_used_percent", 50, 100, 98)}


def valid_value(runtime, value):
    _, low, high, _ = SPECS[runtime]
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        value = int(value.strip())
    return value if type(value) is int and low <= value <= high else None


def valid_account(account):
    return isinstance(account, str) and re.fullmatch(r"[0-9a-f]{12}", account) is not None


def load_json(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        return (doc, None) if isinstance(doc, dict) else ({}, "invalid")
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError):
        return {}, "invalid"


def read_json(path):
    return load_json(path)[0]


def resolve_account(runtime, workspace=None, home=None):
    explicit_home = home is not None
    home = Path(home or Path.home())
    if runtime == "api-credit":
        if workspace is not None:
            doc = read_json(Path(workspace) / ".telegram_bot/dispatch/api-credit-account.json")
            account = doc.get("fingerprint")
            if not valid_account(account):
                account = Path(str(doc.get("dir") or "")).name
        else:
            account = read_json(home / ".dogany/api-credit/current.json").get("fp")
    elif runtime == "codex":
        # An explicit home isolates tests from the calling user's CODEX_HOME.
        folder = Path(os.environ.get("CODEX_HOME") or home / ".codex") if not explicit_home else home / ".codex"
        tokens = read_json(folder / "auth.json").get("tokens")
        aid = tokens.get("account_id") if isinstance(tokens, dict) else None
        account = hashlib.sha256(str(aid).strip().encode("utf-8")).hexdigest()[:12] if aid else None
    else:
        raise ValueError("unknown resource runtime")
    return account if valid_account(account) else None


def account_from_book(path):
    """Resolve an account only from a shared API book, never a legacy file."""
    parent = Path(path).parent if path else None
    return parent.name if parent and parent.parent.name == "api-credit" and valid_account(parent.name) else None


def store_path(runtime, account, home=None):
    if runtime not in SPECS or not valid_account(account):
        raise ValueError("a known runtime and a 12-hex account fingerprint are required")
    root = os.environ.get("DOGANY_RESOURCE_SETTINGS_DIR")
    root = Path(root).expanduser() if root else Path(home or Path.home()) / ".dogany/resource-settings"
    return root / runtime / account / "settings.json"


def get(runtime, account, home=None):
    """Read without locks, writes or credentials; return effective value + note."""
    field, _, _, default = SPECS[runtime]
    doc, error = load_json(store_path(runtime, account, home)) if valid_account(account) else ({}, "missing")
    raw = doc.get(field)
    value = valid_value(runtime, raw)
    if value is None:
        reason = error or ("missing" if raw is None else "invalid")
        return {field: default, "account": account, "source": "default",
                "note": f"{runtime} {field} {reason}; using default {default}"}
    return {field: value, "account": account, "source": "account", "note": "account resource setting"}


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save(path, doc):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            json.dump(doc, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def set_value(runtime, account, value, home=None):
    field, low, high, _ = SPECS[runtime]
    value = valid_value(runtime, value)
    if value is None:
        raise ValueError(f"{field} must be an integer from {low} to {high}")
    path = store_path(runtime, account, home)
    with locked(path):
        doc = read_json(path)
        doc.update({"version": 1, field: value})
        save(path, doc)
    return get(runtime, account, home)


def credit_reset_day(account, legacy=None, home=None, migrate=True):
    """Shared value wins. Import valid legacy input once, only if field absent.

    Passive views use migrate=False: they can display the pending legacy
    value until an active reader migrates it, without violating read-only hooks.
    An invalid stored value falls back to day 1; it is never overwritten by an
    old instance's legacy setting. The owner can repair it with the set verb.
    """
    result = get("api-credit", account, home)
    day = valid_value("api-credit", legacy)
    if day is None or result["source"] == "account":
        return result
    if not valid_account(account):
        return dict(result, reset_day=day, source="legacy", note="legacy reset day; account unresolved")
    path = store_path("api-credit", account, home)
    doc, error = load_json(path)
    if error == "invalid" or "reset_day" in doc:
        return result
    if not migrate:
        return dict(result, reset_day=day, source="legacy", note="legacy reset day pending migration")
    try:
        with locked(path):
            doc, error = load_json(path)
            if error != "invalid" and "reset_day" not in doc:
                doc.update(version=1, reset_day=day)
                save(path, doc)
    except OSError:
        return dict(result, reset_day=day, source="legacy", note="legacy reset day; migration unavailable")
    return get("api-credit", account, home)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("show", "set"))
    parser.add_argument("runtime", choices=tuple(SPECS))
    parser.add_argument("--account")
    parser.add_argument("--workspace")
    parser.add_argument("--api-key-stdin", action="store_true", help="resolve the credit account from a privately piped key")
    parser.add_argument("--reset-day")
    parser.add_argument("--max-used-percent")
    parser.add_argument("--tsv", action="store_true", help="effective value and diagnostic note")
    args = parser.parse_args(argv)
    try:
        if args.api_key_stdin:
            if args.runtime != "api-credit" or args.account or args.workspace:
                raise ValueError("--api-key-stdin requires api-credit without --account/--workspace")
            key = sys.stdin.read().strip()
            if not key:
                raise ValueError("no credit key on stdin")
            account = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            del key
        else:
            account = args.account or resolve_account(args.runtime, args.workspace)
        field = SPECS[args.runtime][0]
        value = getattr(args, field)
        other = args.max_used_percent if args.runtime == "api-credit" else args.reset_day
        if other is not None or (args.command == "show" and value is not None):
            raise ValueError("setting option does not match command/runtime")
        if args.account and not valid_account(args.account):
            raise ValueError("--account must be a 12-hex fingerprint")
        if args.command == "set":
            if not account:
                raise ValueError("account unresolved; connect the account first or supply --account")
            result = set_value(args.runtime, account, value)
        else:
            result = get(args.runtime, account)
        if args.tsv:
            print(f"{result[field]}\t{result['note']}")
        else:
            print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
