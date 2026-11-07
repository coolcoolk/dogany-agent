"""DGN-1854 W5 -- recorded verb + error scenarios (shared by the recorder and
the parity suite).

Every VERB scenario is a call shape lifted from the kit payload
(dogany-lifekit/payload: mirror/adapter.py, mirror/reconcile.py,
database/kind_repair.py). The `site` field names the kit line it came from.
ERROR scenarios reproduce the D10+ list with the error bodies Google sends
(Calendar v3 / Tasks v1 JSON error envelope).

A scenario's `respond` is what the fake Google answers. record_gws.py drives
the real gws CLI against that answer and stores what gws sent and printed;
the parity suite replays the same answer into service.calendar.

English/ASCII only.
"""

CAL = "c0ffee@group.calendar.google.com"
TL = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTA6MDow"
EV = "dgn01hx7k2m9q4r8s5t6v7w8y9z0a"
TK = "dGFza18xMjM0NTY3ODkw"
SYNC = "CPDAlvWDx70CEPDAlvWDx70CGAU="
PAGE = "EjgKMDAwMDAwMDAwMDAwMDAwMA=="
WM = "2026-10-01T00:00:00.000Z"

EVENT_BODY = {
    "id": EV, "summary": "Lunch", "status": "confirmed",
    "start": {"dateTime": "2026-10-08T12:00:00+09:00",
              "timeZone": "Asia/Seoul"},
    "end": {"dateTime": "2026-10-08T13:00:00+09:00",
            "timeZone": "Asia/Seoul"},
    "colorId": "5",
    "extendedProperties": {"private": {"ulid": "01HX7K2M9Q4R8S5T6V7W8Y9Z0A",
                                       "version": "3"}},
}
TASK_BODY = {"title": "Buy milk", "notes": "[ulid:01HX7K2M9Q4R8S5T6V7W8Y9Z0B]",
             "status": "needsAction", "due": "2026-10-08T00:00:00.000Z"}


def _ok(body):
    return {"status": 200, "json": body}


def _gerr(code, reason, message, domain="global", status=None):
    err = {"errors": [{"domain": domain, "reason": reason,
                       "message": message}],
           "code": code, "message": message}
    if status:
        err["status"] = status
    return {"status": code, "json": {"error": err}}


EVENT_RESP = dict(EVENT_BODY, etag='"3456789012345678"', kind="calendar#event",
                  htmlLink="https://www.google.com/calendar/event?eid=x")
TASK_RESP = dict(TASK_BODY, id=TK, etag='"LTE2MjQ0NTY3OA"', kind="tasks#task")
CAL_RESP = {"kind": "calendar#calendar", "etag": '"e1"', "id": CAL,
            "summary": "Dogany", "description": "[dogany:m] -- x",
            "timeZone": "Asia/Seoul"}
TL_RESP = {"kind": "tasks#taskList", "id": TL, "etag": '"t1"',
           "title": "Dogany", "updated": "2026-10-01T00:00:00.000Z"}

# kind: "call" = kit gws(); "delete" = kit gws_delete()
VERBS = [
    {"id": "cal.calendars.get", "kind": "call", "site": "adapter.py:1873,1977,2017",
     "args": ["calendar", "calendars", "get", "--params", {"calendarId": CAL}],
     "respond": [_ok(CAL_RESP)]},
    {"id": "cal.calendars.insert", "kind": "call", "site": "adapter.py:1949,2162",
     "args": ["calendar", "calendars", "insert"],
     "body": {"summary": "Dogany", "description": "[dogany:m] -- x",
              "timeZone": "Asia/Seoul"},
     "respond": [_ok(CAL_RESP)]},
    {"id": "cal.calendars.patch", "kind": "call", "site": "adapter.py:1986,2026",
     "args": ["calendar", "calendars", "patch", "--params", {"calendarId": CAL}],
     "body": {"description": "mine\n[dogany:m] -- x"},
     "respond": [_ok(CAL_RESP)]},
    {"id": "cal.calendars.delete", "kind": "delete", "site": "adapter.py:4272",
     "args": ["calendar", "calendars", "delete", "--params", {"calendarId": CAL}],
     "respond": [{"status": 204, "raw": ""}]},
    {"id": "cal.calendarList.list", "kind": "call", "site": "adapter.py:1893,2095",
     "args": ["calendar", "calendarList", "list"],
     "respond": [_ok({"kind": "calendar#calendarList", "items": [CAL_RESP]})]},
    {"id": "cal.events.get", "kind": "call", "site": "adapter.py:2336,2558,2746",
     "args": ["calendar", "events", "get", "--params",
              {"calendarId": CAL, "eventId": EV}],
     "respond": [_ok(EVENT_RESP)]},
    {"id": "cal.events.insert", "kind": "call", "site": "adapter.py:2405,2423",
     "args": ["calendar", "events", "insert", "--params", {"calendarId": CAL}],
     "body": EVENT_BODY, "respond": [_ok(EVENT_RESP)]},
    {"id": "cal.events.update", "kind": "call", "site": "adapter.py:2412,2567,2749",
     "args": ["calendar", "events", "update", "--params",
              {"calendarId": CAL, "eventId": EV}],
     "body": dict(EVENT_BODY, status="cancelled"),
     "respond": [_ok(dict(EVENT_RESP, status="cancelled"))]},
    {"id": "cal.events.patch", "kind": "call", "site": "adapter.py:3236; kind_repair.py:76",
     "args": ["calendar", "events", "patch", "--params",
              {"calendarId": CAL, "eventId": EV}],
     "body": {"extendedProperties": {"private": {
         "ulid": "01HX7K2M9Q4R8S5T6V7W8Y9Z0A", "version": "0"}}},
     "respond": [_ok(EVENT_RESP)]},
    {"id": "cal.events.list.full", "kind": "call", "site": "adapter.py:3301 (no token)",
     "args": ["calendar", "events", "list", "--params",
              {"calendarId": CAL, "showDeleted": True, "singleEvents": True,
               "maxResults": 250}],
     "respond": [_ok({"kind": "calendar#events", "items": [EVENT_RESP],
                      "nextSyncToken": SYNC})]},
    {"id": "cal.events.list.sync", "kind": "call", "site": "adapter.py:3301 (syncToken)",
     "args": ["calendar", "events", "list", "--params",
              {"calendarId": CAL, "syncToken": SYNC, "singleEvents": True,
               "maxResults": 250, "pageToken": PAGE}],
     "respond": [_ok({"kind": "calendar#events", "items": [],
                      "nextPageToken": PAGE})]},
    {"id": "cal.events.list.reconcile", "kind": "call", "site": "reconcile.py:104",
     "args": ["calendar", "events", "list", "--params",
              {"calendarId": CAL, "maxResults": 250, "singleEvents": True,
               "showDeleted": True, "pageToken": PAGE}],
     "respond": [_ok({"kind": "calendar#events", "items": [EVENT_RESP]})]},
    {"id": "tasks.tasklists.get", "kind": "call", "site": "adapter.py:1879",
     "args": ["tasks", "tasklists", "get", "--params", {"tasklist": TL}],
     "respond": [_ok(TL_RESP)]},
    {"id": "tasks.tasklists.list", "kind": "call", "site": "adapter.py:1916,2126",
     "args": ["tasks", "tasklists", "list"],
     "respond": [_ok({"kind": "tasks#taskLists", "items": [TL_RESP]})]},
    {"id": "tasks.tasklists.insert", "kind": "call", "site": "adapter.py:1963,2185",
     "args": ["tasks", "tasklists", "insert"], "body": {"title": "Dogany"},
     "respond": [_ok(TL_RESP)]},
    {"id": "tasks.tasklists.update", "kind": "call", "site": "adapter.py:2176",
     "args": ["tasks", "tasklists", "update", "--params", {"tasklist": TL}],
     "body": {"id": TL, "title": "Dogany checklist"},
     "respond": [_ok(TL_RESP)]},
    {"id": "tasks.tasklists.delete", "kind": "delete", "site": "adapter.py:4279",
     "args": ["tasks", "tasklists", "delete", "--params", {"tasklist": TL}],
     "respond": [{"status": 204, "raw": ""}]},
    {"id": "tasks.tasks.get", "kind": "call", "site": "adapter.py:2475",
     "args": ["tasks", "tasks", "get", "--params", {"tasklist": TL, "task": TK}],
     "respond": [_ok(TASK_RESP)]},
    {"id": "tasks.tasks.insert", "kind": "call", "site": "adapter.py:2508",
     "args": ["tasks", "tasks", "insert", "--params", {"tasklist": TL}],
     "body": TASK_BODY, "respond": [_ok(TASK_RESP)]},
    {"id": "tasks.tasks.update", "kind": "call", "site": "adapter.py:2494,2504",
     "args": ["tasks", "tasks", "update", "--params", {"tasklist": TL, "task": TK}],
     "body": dict(TASK_BODY, id=TK), "respond": [_ok(TASK_RESP)]},
    {"id": "tasks.tasks.delete", "kind": "delete", "site": "adapter.py:2459,2729",
     "args": ["tasks", "tasks", "delete", "--params", {"tasklist": TL, "task": TK}],
     "respond": [{"status": 204, "raw": ""}]},
    {"id": "tasks.tasks.list.sentinel", "kind": "call", "site": "adapter.py:2528",
     "args": ["tasks", "tasks", "list", "--params",
              {"tasklist": TL, "showHidden": True, "showCompleted": True,
               "showDeleted": False, "maxResults": 100, "pageToken": PAGE}],
     "respond": [_ok({"kind": "tasks#tasks", "items": [TASK_RESP]})]},
    {"id": "tasks.tasks.list.pull", "kind": "call", "site": "adapter.py:3614; reconcile.py:118",
     "args": ["tasks", "tasks", "list", "--params",
              {"tasklist": TL, "showHidden": True, "showCompleted": True,
               "showDeleted": True, "maxResults": 100, "updatedMin": WM}],
     "respond": [_ok({"kind": "tasks#tasks", "items": [TASK_RESP],
                      "nextPageToken": PAGE})]},
    # path-encoding probe: gws percent-encodes every non-alphanumeric byte
    {"id": "enc.path.symbols", "kind": "call", "site": "encoding probe",
     "args": ["calendar", "events", "get", "--params",
              {"calendarId": "a.b-c_d~e/f+g h@x", "eventId": "x%y:z"}],
     "respond": [_ok(EVENT_RESP)]},
    {"id": "enc.path.unicode", "kind": "call", "site": "encoding probe",
     "args": ["calendar", "calendars", "get", "--params",
              {"calendarId": "ko\u00e9\uac00@group.calendar.google.com"}],
     "respond": [_ok(CAL_RESP)]},
]

_EV_GET = ["calendar", "events", "get", "--params",
           {"calendarId": CAL, "eventId": EV}]
_EV_LIST_SYNC = ["calendar", "events", "list", "--params",
                 {"calendarId": CAL, "syncToken": SYNC, "singleEvents": True,
                  "maxResults": 250}]
_TASK_DEL = ["tasks", "tasks", "delete", "--params",
             {"tasklist": TL, "task": TK}]

ERRORS = [
    {"id": "err.events.get.410.deleted", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(410, "deleted", "Resource has been deleted")]},
    {"id": "err.calendars.delete.410.deleted", "kind": "delete",
     "args": ["calendar", "calendars", "delete", "--params", {"calendarId": CAL}],
     "respond": [_gerr(410, "deleted", "Resource has been deleted")]},
    {"id": "err.tasks.delete.410.deleted", "kind": "delete", "args": _TASK_DEL,
     "respond": [_gerr(410, "deleted", "Resource has been deleted")]},
    {"id": "err.tasks.delete.404", "kind": "delete", "args": _TASK_DEL,
     "respond": [_gerr(404, "notFound", "Requested entity was not found.",
                       status="NOT_FOUND")]},
    {"id": "err.events.list.410.fullSync", "kind": "call", "args": _EV_LIST_SYNC,
     "respond": [_gerr(410, "fullSyncRequired",
                       "Sync token is no longer valid, a full sync is required.")]},
    {"id": "err.events.list.400.invalid", "kind": "call", "args": _EV_LIST_SYNC,
     "respond": [_gerr(400, "invalid", "Invalid sync token value.")]},
    {"id": "err.events.list.400.badRequest", "kind": "call", "args": _EV_LIST_SYNC,
     "respond": [_gerr(400, "badRequest", "Bad Request")]},
    {"id": "err.events.get.403.rateLimit", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(403, "rateLimitExceeded", "Rate Limit Exceeded",
                       domain="usageLimits")]},
    {"id": "err.events.get.403.forbidden", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(403, "forbidden", "Forbidden")]},
    {"id": "err.events.get.403.insufficient", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(403, "insufficientPermissions",
                       "Request had insufficient authentication scopes.",
                       status="PERMISSION_DENIED")]},
    {"id": "err.events.get.429", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(429, "rateLimitExceeded", "Rate Limit Exceeded",
                       domain="usageLimits", status="RESOURCE_EXHAUSTED")]},
    {"id": "err.events.get.404", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(404, "notFound", "Not Found")]},
    {"id": "err.events.insert.409", "kind": "call",
     "args": ["calendar", "events", "insert", "--params", {"calendarId": CAL}],
     "body": EVENT_BODY,
     "respond": [_gerr(409, "duplicate",
                       "The requested identifier already exists.")]},
    {"id": "err.events.update.412", "kind": "call",
     "args": ["calendar", "events", "update", "--params",
              {"calendarId": CAL, "eventId": EV}],
     "body": EVENT_BODY,
     "respond": [_gerr(412, "conditionNotMet", "Precondition Failed")]},
    {"id": "err.events.get.401", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(401, "authError", "Invalid Credentials",
                       status="UNAUTHENTICATED")]},
    {"id": "err.events.get.500", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(500, "backendError", "Backend Error")]},
    {"id": "err.events.get.503", "kind": "call", "args": _EV_GET,
     "respond": [_gerr(503, "backendError", "Backend Error")]},
    # envelope edge cases: decide how gws derives `reason`
    {"id": "edge.no_errors_array", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 400, "json": {"error": {
         "code": 400, "message": "Invalid argument", "status": "INVALID_ARGUMENT"}}}]},
    {"id": "edge.empty_errors_array", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 400, "json": {"error": {
         "code": 400, "message": "Invalid argument", "errors": [],
         "status": "INVALID_ARGUMENT"}}}]},
    {"id": "edge.errors_no_reason", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 400, "json": {"error": {
         "code": 400, "message": "m", "errors": [{"message": "m"}]}}}]},
    {"id": "edge.code_mismatch", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 404, "json": {"error": {
         "code": 400, "message": "m", "errors": [{"reason": "r1"}, {"reason": "r2"}]}}}]},
    {"id": "edge.non_json_502", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 502, "raw": "<html><body>502 Bad Gateway</body></html>",
                  "ctype": "text/html"}]},
    {"id": "edge.error_string", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 400, "json": {"error": "invalid_request",
                                          "error_description": "x"}}]},
    {"id": "edge.empty_body_500", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 500, "raw": ""}]},
    {"id": "edge.delete.non_json_500", "kind": "delete", "args": _TASK_DEL,
     "respond": [{"status": 500, "raw": "oops", "ctype": "text/plain"}]},
    {"id": "edge.delete.200_json", "kind": "delete", "args": _TASK_DEL,
     "respond": [_ok({})]},
    {"id": "edge.ok_empty_body", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 200, "raw": ""}]},
    {"id": "edge.ok_200_error_key", "kind": "call", "args": _EV_GET,
     "respond": [_ok({"error": {"code": 7, "reason": "x", "message": "m"}})]},
    {"id": "edge.error_no_code", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 409, "json": {"error": {
         "message": "m", "errors": [{"reason": "conflict"}]}}}]},
    {"id": "edge.error_code_string", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 409, "json": {"error": {
         "code": "409", "message": "m", "errors": [{"reason": "conflict"}]}}}]},
    {"id": "edge.error_no_message", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 400, "json": {"error": {
         "code": 400, "errors": [{"reason": "invalid"}]}}}]},
    {"id": "edge.json_no_error_key_500", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 500, "json": {"oops": 1}}]},
    {"id": "edge.json_list_500", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 500, "json": [1, 2]}]},
    {"id": "edge.non_json_long_503", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 503, "raw": "x" * 3000, "ctype": "text/plain"}]},
    {"id": "edge.ok_non_json", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 200, "raw": "hello", "ctype": "text/plain"}]},
    {"id": "edge.ok_json_list", "kind": "call", "args": _EV_GET,
     "respond": [{"status": 200, "json": [1, 2]}]},
]

ALL = VERBS + ERRORS
