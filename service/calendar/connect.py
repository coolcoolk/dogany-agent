"""Let the owner choose the framework calendar after a successful Google relay.

Candidates are read-only. Only bind can create a calendar or save a choice.
No kit configuration, tasklists, scheduling or consent is performed here.
"""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

from . import open as open_calendar
from . import settings
from .errors import BackendUnavailable, GwsError
from .resolve import instance_root
from .verbs import CAL_ROOT
from service.google.errors import render


def _context(instance, account):
    root = instance_root(instance)
    if not root:
        raise BackendUnavailable("invalid_settings")
    settings.read(root)
    path = Path(root) / "config" / "calendar.json"
    saved = (json.loads(path.read_text(), object_pairs_hook=settings._unique)
             if path.exists() else {})
    conn = open_calendar(root)
    if conn.backend != "google":
        raise BackendUnavailable("dual_target")
    if conn.account_key != "google:" + account:
        raise BackendUnavailable("account_mismatch")
    health = conn.health()
    if health:
        raise BackendUnavailable(health)
    previous = saved.get("google", {})
    if previous.get("account_key", conn.account_key) != conn.account_key:
        raise BackendUnavailable("account_mismatch")
    return path, saved, conn


def _items(conn, url, params=None):
    params = dict(params or {})
    seen = set()
    while True:
        _, page = conn.http("GET", url, params=params)
        if not isinstance(page, dict) or not isinstance(page.get("items", []), list):
            raise BackendUnavailable("invalid_response")
        for item in page.get("items", []):
            if not isinstance(item, dict):
                raise BackendUnavailable("invalid_response")
            yield item
        token = page.get("nextPageToken")
        if not token:
            break
        if not isinstance(token, str) or token in seen:
            raise BackendUnavailable("invalid_response")
        seen.add(token)
        params["pageToken"] = token


def _writable(conn):
    return [c for c in _items(conn, CAL_ROOT + "users/me/calendarList")
            if c.get("accessRole") in ("owner", "writer") and not c.get("deleted")
            and isinstance(c.get("id"), str) and c["id"]
            and isinstance(c.get("summary"), str)]


def _bound(calendar, locale, created=False):
    return {"ok": True, "bound": True, "calendar_id": calendar["calendar_id"],
            "summary": calendar["summary"], "created": created,
            "copy": render("google.calendar.created" if created else
                           "google.calendar.bound", locale, summary=calendar["summary"])}


def candidates(instance, account, locale="en", all_calendars=False):
    path, saved, conn = _context(instance, account)
    previous = saved.get("google", {})
    if previous.get("calendar_id"):
        return _bound(previous, locale)
    calendars = _writable(conn)
    dogany = next((c for c in calendars if c["summary"] == "Dogany"), None)
    visible = calendars
    shortened = len(calendars) > 6 and not all_calendars
    if shortened:
        now = datetime.now(timezone.utc)
        params = {"timeMin": (now - timedelta(days=30)).isoformat(),
                  "timeMax": now.isoformat(), "singleEvents": True,
                  "showDeleted": False, "maxResults": 2500}
        counts = {}
        for calendar in calendars:
            url = CAL_ROOT + "calendars/" + quote(calendar["id"], safe="") + "/events"
            counts[calendar["id"]] = sum(1 for event in _items(conn, url, params)
                                         if event.get("status") != "cancelled")
        # Python's stable sort preserves calendarList order on count ties.
        visible = sorted(calendars, key=lambda c: -counts[c["id"]])
        if dogany:
            visible = [dogany] + [c for c in visible if c["id"] != dogany["id"]]
        visible = visible[:5]
    options = []
    if dogany:
        options.append({"id": dogany["id"], "label": render("google.calendar.dogany", locale),
                        "kind": "existing"})
    else:
        options.append({"id": "create", "label": render("google.calendar.create", locale),
                        "kind": "create"})
    options.extend({"id": c["id"], "label": c["summary"], "kind": "existing"}
                   for c in visible if not dogany or c["id"] != dogany["id"])
    if shortened:
        options.append({"id": "more", "label": render("google.calendar.more", locale),
                        "kind": "more"})
    return {"ok": True, "bound": False, "copy": render("google.calendar.choose", locale),
            "candidates": options}


def bind(instance, account, *, create=False, calendar_id=None, locale="en"):
    if create == bool(calendar_id):
        raise BackendUnavailable("invalid_settings")
    root = instance_root(instance)
    if not root:
        raise BackendUnavailable("invalid_settings")
    config = Path(root) / "config"
    config.mkdir(parents=True, exist_ok=True)
    # Serialize retries, including the list/create window and config update.
    fd = os.open(config / ".google-calendar.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path, saved, conn = _context(root, account)
        previous = saved.get("google", {})
        if previous.get("calendar_id"):
            return _bound(previous, locale)
        calendars = _writable(conn)
        calendar = next((c for c in calendars if
                         (c["summary"] == "Dogany" if create else c["id"] == calendar_id)), None)
        created = create and calendar is None
        if created:
            calendar = conn.call("calendar", "calendars", "insert", body={"summary": "Dogany"})
        if not calendar:
            raise BackendUnavailable("calendar_not_writable")
        if (not isinstance(calendar, dict) or not isinstance(calendar.get("id"), str)
                or not calendar["id"] or not isinstance(calendar.get("summary"), str)):
            raise BackendUnavailable("invalid_response")
        saved["google"] = {"calendar_id": calendar["id"], "summary": calendar["summary"],
                           "account_key": conn.account_key}
        tmp_fd, tmp = tempfile.mkstemp(prefix=".calendar-", dir=config)
        try:
            with os.fdopen(tmp_fd, "w") as output:
                json.dump(saved, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return _bound(saved["google"], locale, created)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("candidates", "bind"), nargs="?", default="candidates")
    parser.add_argument("--instance")
    parser.add_argument("--account", required=True, help="sub from the success relay")
    parser.add_argument("--locale", choices=("en", "ko"), default=os.environ.get("AGENT_LANG", "en"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--all", action="store_true", help="list all writable calendars")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--create", action="store_true")
    choice.add_argument("--calendar-id")
    args = parser.parse_args(argv)
    if (args.step == "bind" and (args.all or not (args.create or args.calendar_id))
            or args.step == "candidates" and (args.create or args.calendar_id)):
        parser.error("use candidates [--all] or bind (--create | --calendar-id ID)")
    try:
        result = (candidates(args.instance, args.account, args.locale, args.all)
                  if args.step == "candidates" else
                  bind(args.instance, args.account, create=args.create,
                       calendar_id=args.calendar_id, locale=args.locale))
    except GwsError as exc:
        result = {"ok": False, "class": exc.reason}
    except (ValueError, TypeError, KeyError):
        result = {"ok": False, "class": "invalid_settings"}
    except OSError:
        result = {"ok": False, "class": "store_unwritable"}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
