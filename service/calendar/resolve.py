"""service.calendar.resolve -- backend selection (DGN-1854 spec 5, K5, K6).

v2.7 rules:
  * offered: "google" always, unless status says cap_suspected (spec 6.1:
    then Google is not offered for NEW connects; an existing mirror keeps
    running). "apple" is never offered: unavailable "not_built" until the
    annex build ships (dec-302).
  * requested: kit conf CALENDAR_BACKEND = auto | google | apple (default
    auto). "apple" is accepted and resolves unavailable with its reason; the
    choice never switches by itself.
  * engraved: the kit's mirror_target stamp (mirror_state KV, "<backend>:
    <account>", spec 7 M3). In auto mode an engraved backend is kept.
  * one target (K6): CALENDAR_BACKEND naming two backends, two conflicting
    CALENDAR_BACKEND lines across the conf files, or an explicit backend
    that disagrees with the engraved stamp -> class "dual_target".

Instance = the instance root (holds config/kit_life.conf and
.instance.conf, parsed the way the kit's adapter._load_conf does). The state
DB is $MIRROR_STATE_DB or <instance>/mirror/mirror_state.db, opened
read-only (never created).

English/ASCII only.
"""

import os
import re
import sqlite3
import urllib.parse

from . import settings
from .errors import BackendUnavailable

BACKENDS = ("google", "apple")
CHOICES = ("auto",) + BACKENDS
UNAVAILABLE = {"apple": "not_built"}
CONF_KEY = "CALENDAR_BACKEND"


def instance_root(instance=None):
    if instance:
        return os.fspath(instance)
    return os.environ.get("PROJECT_ROOT") or None


def _conf_values(path, key):
    """Every value assigned to `key` in a shell-style conf file (kit
    adapter._parse_conf_file quoting rules)."""
    out = []
    try:
        with open(path, "r") as fh:
            lines = fh.readlines()
    except (OSError, IOError, UnicodeDecodeError):
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, val = line.partition("=")
        if k.strip() != key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out.append(val)
    return out


def read_conf(instance=None):
    """All CALENDAR_BACKEND assignments, kit_life.conf first."""
    root = instance_root(instance)
    if not root:
        return []
    vals = []
    for rel in (os.path.join("config", "kit_life.conf"), ".instance.conf"):
        vals.extend(_conf_values(os.path.join(root, rel), CONF_KEY))
    return vals


def state_db_path(instance=None):
    explicit = os.environ.get("MIRROR_STATE_DB")
    if explicit:
        return explicit
    root = instance_root(instance)
    if not root:
        return None
    return os.path.join(root, "mirror", "mirror_state.db")


def read_target(instance=None):
    """The engraved mirror_target stamp, or None. Read-only; a missing or
    unreadable DB is 'no stamp'."""
    path = state_db_path(instance)
    if not path or not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path),
                               uri=True, timeout=2)
        try:
            row = conn.execute("SELECT value FROM mirror_state WHERE key=?",
                               ("mirror_target",)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row and isinstance(row[0], str) and row[0].strip():
        return row[0].strip()
    return None


def _tokens(value):
    return [t for t in re.split(r"[\s,;+]+", (value or "").strip().lower()) if t]


def resolve(instance=None, google_status=None, conf_values=None,
            target=None, _read_state=True, configuration=None):
    """-> {"backend", "class", "requested", "target", "offered",
           "unavailable"}. backend is None exactly when class is set."""
    if conf_values is None:
        conf_values = read_conf(instance)
    if target is None and _read_state:
        target = read_target(instance)
    st = google_status if isinstance(google_status, dict) else {}
    cap = st.get("paused") == "cap_suspected" or st.get("cap_suspected") is True
    offered = [] if cap else ["google"]
    unavailable = dict(UNAVAILABLE)
    if cap:
        unavailable["google"] = "cap_suspected"
    out = {"backend": None, "class": None, "requested": None,
           "target": target, "offered": offered, "unavailable": unavailable}

    try:
        cfg = settings.read(instance) if configuration is None else configuration
    except BackendUnavailable as exc:
        out["class"] = exc.cls
        return out
    custom = cfg["backends"]
    # Offered services remain Google-only. Configured drivers are selected
    # explicitly and are never advertised as Dogany-provided services.
    out["subscriptions"] = [{"id": feed["id"], "backend": "ics", "read_only": True}
                            for feed in cfg["subscriptions"]]
    configured = cfg["write_target"]
    if configured:
        # auto permits the JSON settings to select the one target. Explicit
        # legacy assignments must agree, including across both conf files.
        if any(_tokens(v) not in ([], ["auto"], [configured]) for v in conf_values):
            out["requested"] = configured
            out["class"] = "dual_target"
            return out
        conf_values = [configured]

    distinct = []
    for v in conf_values:
        norm = " ".join(_tokens(v)) or "auto"
        if norm not in distinct:
            distinct.append(norm)
    if len(distinct) > 1:
        out["requested"] = distinct
        out["class"] = "dual_target"
        return out
    toks = _tokens(distinct[0]) if distinct else ["auto"]
    uniq = []
    for t in toks:
        if t not in uniq:
            uniq.append(t)
    out["requested"] = " ".join(uniq)
    if len(uniq) > 1:
        out["class"] = "dual_target"
        return out
    requested = uniq[0]
    if requested not in CHOICES and requested not in custom:
        out["class"] = "unknown_backend"
        return out

    engraved = None
    if target:
        engraved = target.split(":", 1)[0].strip().lower()
        if engraved not in BACKENDS and engraved not in custom:
            out["class"] = "unknown_backend"
            return out

    if requested == "auto":
        chosen = engraved or "google"
    else:
        if engraved and engraved != requested:
            out["class"] = "dual_target"
            return out
        chosen = requested
    if chosen in UNAVAILABLE and chosen not in custom:
        out["class"] = UNAVAILABLE[chosen]
        return out
    if chosen in custom:
        key = "%s:%s" % (chosen, custom[chosen]["account"])
        if target and key != target:
            out["class"] = "target_mismatch"
            return out
        if not custom[chosen].get("driver"):
            out["class"] = "backend_not_available"
            out["unavailable"][chosen] = "backend_not_available"
            return out
    elif target and st.get("sub") and target != "google:" + st["sub"]:
        out["class"] = "target_mismatch"
        return out
    out["backend"] = chosen
    return out
