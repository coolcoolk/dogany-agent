"""Instance settings for one write target and any number of ICS feeds.

No shell evaluation, credential output, driver imports or network on reads.
"""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .errors import BackendUnavailable

NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
CAPABILITY_KEYS = frozenset(("calendar_create_sources", "event_color",
                             "tasks_kind", "change_feed", "offline_write",
                             "attendees_write", "window"))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def read(instance=None):
    from .resolve import instance_root
    root = instance_root(instance)
    path = Path(root) / "config" / "calendar.json" if root else None
    if path is None or not path.exists():
        return {"write_target": None, "backends": {}, "subscriptions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
        if not isinstance(data, dict) or set(data) - {"write_target", "backends", "subscriptions"}:
            raise ValueError()
        target = data.get("write_target")
        if isinstance(target, list):
            raise BackendUnavailable("dual_target")
        backends = data.get("backends", {})
        if not isinstance(backends, dict):
            raise ValueError()
        for name, entry in backends.items():
            if not NAME.fullmatch(name) or name in ("auto", "google") or not isinstance(entry, dict):
                raise ValueError()
            if not isinstance(entry.get("account"), str) or not entry["account"].strip():
                raise ValueError()
            if "driver" in entry and (not isinstance(entry["driver"], str) or not entry["driver"]):
                raise ValueError()
        if target is not None and (not isinstance(target, str) or not NAME.fullmatch(target)):
            raise ValueError()
        feeds = data.get("subscriptions", [])
        if not isinstance(feeds, list):
            raise ValueError()
        names = set()
        for feed in feeds:
            if not isinstance(feed, dict) or set(feed) - {"id", "url"}:
                raise ValueError()
            name = feed["id"]
            if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
                raise ValueError()
            names.add(name)
            if not isinstance(feed["url"], str):
                raise ValueError()
            parsed = urlsplit(feed["url"])
            if parsed.scheme not in ("http", "https", "webcal") or not parsed.hostname:
                raise ValueError()
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError()
        return {"write_target": target, "backends": backends, "subscriptions": feeds}
    except BackendUnavailable:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise BackendUnavailable("invalid_settings") from None
