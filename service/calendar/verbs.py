"""service.calendar.verbs -- the CLOSED verb table (DGN-1854 W5, D10+).

Recorded from every gws() / gws_delete() call shape in the kit payload
(dogany-lifekit/payload mirror/adapter.py, mirror/reconcile.py,
database/kind_repair.py; site list in conformance/scenarios.py) and checked
against the REST call the gws CLI 0.13.2 actually sends
(conformance/fixtures/gws_recorded.json):

  * path parameters: every byte that is not an ASCII letter or digit is
    percent-encoded (gws encodes '.', '-', '_', '~' and '/' too);
  * query values: booleans as "true"/"false", integers in decimal;
  * the body is sent as given.

A verb or parameter outside this table fails closed with
GwsError(code 0, reason "unsupported_verb") and nothing is sent. Adding a
verb means recording it first (record_gws.py), then adding it here.

English/ASCII only.
"""

import json

from .errors import GwsError

CAL_ROOT = "https://www.googleapis.com/calendar/v3/"
TASKS_ROOT = "https://tasks.googleapis.com/tasks/v1/"

UNSUPPORTED = "unsupported_verb"

_EV_LIST_Q = ("syncToken", "showDeleted", "singleEvents", "maxResults",
              "pageToken")
_TASK_LIST_Q = ("showHidden", "showCompleted", "showDeleted", "maxResults",
                "updatedMin", "pageToken")

# (service, resource, method) ->
#   kind   "call" (kit gws) | "delete" (kit gws_delete)
#   http   HTTP method
#   root   API root
#   path   template; {name} = path parameter
#   query  allowed query parameters (exactly the ones the kit passes)
#   body   True = the kit always sends a body; False = never
#   scope  short scope the access token must carry
VERBS = {
    ("calendar", "calendars", "get"): dict(
        kind="call", http="GET", root=CAL_ROOT, path="calendars/{calendarId}",
        query=(), body=False, scope="calendar"),
    ("calendar", "calendars", "insert"): dict(
        kind="call", http="POST", root=CAL_ROOT, path="calendars",
        query=(), body=True, scope="calendar"),
    ("calendar", "calendars", "patch"): dict(
        kind="call", http="PATCH", root=CAL_ROOT, path="calendars/{calendarId}",
        query=(), body=True, scope="calendar"),
    ("calendar", "calendars", "delete"): dict(
        kind="delete", http="DELETE", root=CAL_ROOT,
        path="calendars/{calendarId}", query=(), body=False, scope="calendar"),
    ("calendar", "calendarList", "list"): dict(
        kind="call", http="GET", root=CAL_ROOT, path="users/me/calendarList",
        query=(), body=False, scope="calendar"),
    ("calendar", "events", "get"): dict(
        kind="call", http="GET", root=CAL_ROOT,
        path="calendars/{calendarId}/events/{eventId}", query=(), body=False,
        scope="calendar"),
    ("calendar", "events", "insert"): dict(
        kind="call", http="POST", root=CAL_ROOT,
        path="calendars/{calendarId}/events", query=(), body=True,
        scope="calendar"),
    ("calendar", "events", "update"): dict(
        kind="call", http="PUT", root=CAL_ROOT,
        path="calendars/{calendarId}/events/{eventId}", query=(), body=True,
        scope="calendar"),
    ("calendar", "events", "patch"): dict(
        kind="call", http="PATCH", root=CAL_ROOT,
        path="calendars/{calendarId}/events/{eventId}", query=(), body=True,
        scope="calendar"),
    ("calendar", "events", "list"): dict(
        kind="call", http="GET", root=CAL_ROOT,
        path="calendars/{calendarId}/events", query=_EV_LIST_Q, body=False,
        scope="calendar"),
    ("tasks", "tasklists", "get"): dict(
        kind="call", http="GET", root=TASKS_ROOT,
        path="users/@me/lists/{tasklist}", query=(), body=False, scope="tasks"),
    ("tasks", "tasklists", "list"): dict(
        kind="call", http="GET", root=TASKS_ROOT, path="users/@me/lists",
        query=(), body=False, scope="tasks"),
    ("tasks", "tasklists", "insert"): dict(
        kind="call", http="POST", root=TASKS_ROOT, path="users/@me/lists",
        query=(), body=True, scope="tasks"),
    ("tasks", "tasklists", "update"): dict(
        kind="call", http="PUT", root=TASKS_ROOT,
        path="users/@me/lists/{tasklist}", query=(), body=True, scope="tasks"),
    ("tasks", "tasklists", "delete"): dict(
        kind="delete", http="DELETE", root=TASKS_ROOT,
        path="users/@me/lists/{tasklist}", query=(), body=False, scope="tasks"),
    ("tasks", "tasks", "get"): dict(
        kind="call", http="GET", root=TASKS_ROOT,
        path="lists/{tasklist}/tasks/{task}", query=(), body=False,
        scope="tasks"),
    ("tasks", "tasks", "insert"): dict(
        kind="call", http="POST", root=TASKS_ROOT, path="lists/{tasklist}/tasks",
        query=(), body=True, scope="tasks"),
    ("tasks", "tasks", "update"): dict(
        kind="call", http="PUT", root=TASKS_ROOT,
        path="lists/{tasklist}/tasks/{task}", query=(), body=True,
        scope="tasks"),
    ("tasks", "tasks", "delete"): dict(
        kind="delete", http="DELETE", root=TASKS_ROOT,
        path="lists/{tasklist}/tasks/{task}", query=(), body=False,
        scope="tasks"),
    ("tasks", "tasks", "list"): dict(
        kind="call", http="GET", root=TASKS_ROOT, path="lists/{tasklist}/tasks",
        query=_TASK_LIST_Q, body=False, scope="tasks"),
}

# conn.http (== http_direct._call) is the If-Match lane: calendar events only.
HTTP_METHODS = ("GET", "PUT", "PATCH")
HTTP_ROOTS = (CAL_ROOT,)


def _unsupported(detail):
    return GwsError("unsupported verb: %s" % detail, code=0,
                    reason=UNSUPPORTED)


def encode_path_param(value):
    """gws path encoding: keep ASCII letters and digits, %XX every other
    UTF-8 byte (uppercase hex)."""
    out = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if byte < 128 and ch.isalnum():
            out.append(ch)
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _query_value(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, str):
        return value
    raise _unsupported("query value type %s" % type(value).__name__)


def parse(args, body=None, kind="call"):
    """gws argv -> request dict {method, url, params, body, scope, verb}.

    `args` is the kit's positional gws() argv: service, resource, method,
    then optionally "--params" <json object>. `kind` is "call" or "delete"
    (which kit wrapper is being emulated). Raises GwsError(unsupported_verb)
    for anything outside the table."""
    if not all(isinstance(a, str) for a in args):
        raise _unsupported("non-string argument")
    if len(args) < 3:
        raise _unsupported(" ".join(args) or "(empty)")
    key = tuple(args[:3])
    spec = VERBS.get(key)
    if spec is None:
        raise _unsupported(" ".join(key))
    if spec["kind"] != kind:
        raise _unsupported("%s via %s" % (" ".join(key), kind))
    rest = list(args[3:])
    params = {}
    if rest:
        if len(rest) != 2 or rest[0] != "--params":
            raise _unsupported("%s flags %s" % (" ".join(key), rest))
        try:
            params = json.loads(rest[1])
        except ValueError:
            raise _unsupported("%s bad --params JSON" % " ".join(key))
        if not isinstance(params, dict):
            raise _unsupported("%s --params not an object" % " ".join(key))
    if spec["body"] and body is None:
        raise _unsupported("%s needs a body" % " ".join(key))
    if not spec["body"] and body is not None:
        raise _unsupported("%s takes no body" % " ".join(key))

    path = spec["path"]
    path_names = []
    i = 0
    while True:
        a = path.find("{", i)
        if a < 0:
            break
        b = path.index("}", a)
        path_names.append(path[a + 1:b])
        i = b + 1
    for name in path_names:
        val = params.get(name)
        if not isinstance(val, str) or not val:
            raise _unsupported("%s missing %s" % (" ".join(key), name))
        path = path.replace("{%s}" % name, encode_path_param(val))
    query = {}
    for name, val in params.items():
        if name in path_names:
            continue
        if name not in spec["query"]:
            raise _unsupported("%s param %s" % (" ".join(key), name))
        query[name] = _query_value(val)
    return {"method": spec["http"], "url": spec["root"] + path,
            "params": query or None, "body": body, "scope": spec["scope"],
            "verb": ".".join(key)}


def check_http(method, url):
    """conn.http guard: the If-Match lane may only reach calendar v3 with
    GET/PUT/PATCH (the token is never attached to another host)."""
    if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
        raise _unsupported("http method %r" % (method,))
    if not isinstance(url, str) or not url.startswith(HTTP_ROOTS):
        raise _unsupported("http url outside calendar v3")
    return method.upper()
