"""DGN-1854 W5 -- stateful fake Google Calendar v3 + Tasks v1 (K1).

In-memory server for google_stub.make_module(server=FakeGoogle()). It models
only what the kit's mirror depends on (adapter.py branches):

  * events: live / cancelled (tombstone) / purged. insert on an existing id
    (live, cancelled or purged) -> 409 duplicate; update on purged -> 404
    (the M10 409 -> update -> 404 -> regenerate path, adapter.py:2404-2425);
    get on purged -> 404, on cancelled -> the cancelled resource.
  * etags change on every write; If-Match mismatch -> 412 (conn.http lane).
  * events.list: maxResults/pageToken paging, nextSyncToken on the last
    page, syncToken incremental feed (changed items incl. cancelled),
    expired token -> 410 fullSyncRequired, malformed token -> 400 invalid.
  * PATCH merges objects key by key (extendedProperties.private, spec A-R4).
  * tasks: updatedMin / showDeleted / showCompleted / showHidden filters,
    paging; delete of a missing task -> 404 notFound.
  * inject(method, verb_re, status, reason, times) queues an error answer.

Error bodies use Google's JSON envelope so the gws reason rule applies.

English/ASCII only.
"""

import copy
import re
import urllib.parse

CAL_ROOT = "https://www.googleapis.com/calendar/v3/"
TASKS_ROOT = "https://tasks.googleapis.com/tasks/v1/"


def gerr(code, reason, message=None):
    message = message or reason
    return code, {"error": {"errors": [{"domain": "global", "reason": reason,
                                        "message": message}],
                            "code": code, "message": message}}


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


def _truthy(v):
    return v in (True, "true")


class FakeGoogle(object):
    def __init__(self):
        self.seq = 0
        self.calendars = {}
        self.events = {}        # cal -> {id: event}
        self.purged = {}        # cal -> set(id)
        self.tasklists = {}
        self.tasks = {}         # tl -> {id: task}
        self.expired_tokens = set()
        self.injected = []
        self.requests = []
        self._ids = 0

    # -- helpers ------------------------------------------------------------
    def _tick(self):
        self.seq += 1
        return self.seq

    def _new_id(self, prefix):
        self._ids += 1
        return "%s%04d" % (prefix, self._ids)

    def _stamp(self, obj):
        s = self._tick()
        obj["etag"] = '"%016d"' % s
        obj["_seq"] = s
        obj["updated"] = "2026-10-07T%02d:%02d:%02d.000Z" % (
            s // 3600, (s // 60) % 60, s % 60)
        return obj

    @staticmethod
    def _public(obj):
        return {k: copy.deepcopy(v) for k, v in obj.items()
                if not k.startswith("_")}

    def inject(self, method, verb_re, status, reason, times=1, message=None):
        self.injected.append([method, re.compile(verb_re), status, reason,
                              times, message])

    def purge(self, cal, eid):
        self.events.get(cal, {}).pop(eid, None)
        self.purged.setdefault(cal, set()).add(eid)

    def expire_sync_tokens(self):
        self.expired_tokens.add("*")

    def owner_edit_event(self, cal, eid, **fields):
        ev = self.events[cal][eid]
        ev.update(fields)
        self._stamp(ev)

    # -- dispatch -----------------------------------------------------------
    def __call__(self, method, url, params, body, etag):
        params = dict(params or {})
        self.requests.append((method, url, params, copy.deepcopy(body), etag))
        if url.startswith(CAL_ROOT):
            api, rest = "calendar", url[len(CAL_ROOT):]
        elif url.startswith(TASKS_ROOT):
            api, rest = "tasks", url[len(TASKS_ROOT):]
        else:
            return gerr(404, "notFound")
        segs = [urllib.parse.unquote(s) for s in rest.split("/")]
        verb_path = "%s %s" % (api, "/".join(segs))
        for inj in self.injected:
            if inj[4] > 0 and inj[0] == method and inj[1].search(verb_path):
                inj[4] -= 1
                return gerr(inj[2], inj[3], inj[5])
        if api == "calendar":
            return self._calendar(method, segs, params, body, etag)
        return self._tasks(method, segs, params, body)

    # -- calendar -----------------------------------------------------------
    def _calendar(self, method, segs, params, body, etag):
        if segs == ["users", "me", "calendarList"] and method == "GET":
            return 200, {"kind": "calendar#calendarList",
                         "items": [self._public(c)
                                   for c in self.calendars.values()]}
        if segs == ["calendars"] and method == "POST":
            cid = self._new_id("cal") + "@group.calendar.google.com"
            cal = dict(body, id=cid, kind="calendar#calendar")
            self.calendars[cid] = self._stamp(cal)
            self.events[cid] = {}
            return 200, self._public(cal)
        if len(segs) == 2 and segs[0] == "calendars":
            cal = self.calendars.get(segs[1])
            if cal is None:
                return gerr(404, "notFound", "Not Found")
            if method == "GET":
                return 200, self._public(cal)
            if method == "PATCH":
                _merge(cal, body)
                return 200, self._public(self._stamp(cal))
            if method == "DELETE":
                del self.calendars[segs[1]]
                self.events.pop(segs[1], None)
                return 204, None
        if len(segs) >= 3 and segs[0] == "calendars" and segs[2] == "events":
            cid = segs[1]
            if cid not in self.calendars:
                return gerr(404, "notFound", "Not Found")
            evs = self.events.setdefault(cid, {})
            purged = self.purged.setdefault(cid, set())
            if len(segs) == 3 and method == "GET":
                return self._events_list(cid, evs, params)
            if len(segs) == 3 and method == "POST":
                eid = body.get("id") or self._new_id("ev")
                if eid in evs or eid in purged:
                    return gerr(409, "duplicate",
                                "The requested identifier already exists.")
                ev = dict(copy.deepcopy(body), id=eid, kind="calendar#event")
                ev.setdefault("status", "confirmed")
                evs[eid] = self._stamp(ev)
                return 200, self._public(ev)
            if len(segs) == 4:
                eid = segs[3]
                ev = evs.get(eid)
                if ev is None:
                    return gerr(404, "notFound", "Not Found")
                if etag and etag != ev["etag"]:
                    return 412, gerr(412, "conditionNotMet",
                                     "Precondition Failed")[1]
                if method == "GET":
                    return 200, self._public(ev)
                if method == "PUT":
                    new = dict(copy.deepcopy(body), id=eid,
                               kind="calendar#event")
                    new.setdefault("status", "confirmed")
                    evs[eid] = self._stamp(new)
                    return 200, self._public(new)
                if method == "PATCH":
                    _merge(ev, body)
                    return 200, self._public(self._stamp(ev))
        return gerr(400, "badRequest", "Bad Request")

    def _events_list(self, cid, evs, params):
        token = params.get("syncToken")
        since = 0
        if token:
            m = re.match(r"^sync-(\d+)$", token)
            if not m:
                return gerr(400, "invalid", "Invalid sync token value.")
            if "*" in self.expired_tokens or token in self.expired_tokens:
                return gerr(410, "fullSyncRequired",
                            "Sync token is no longer valid, a full sync is "
                            "required.")
            since = int(m.group(1))
        items = sorted(evs.values(), key=lambda e: (e["_seq"], e["id"]))
        if token:
            items = [e for e in items if e["_seq"] > since]
        elif not _truthy(params.get("showDeleted")):
            items = [e for e in items if e.get("status") != "cancelled"]
        return self._page(items, params, "calendar#events", "nextSyncToken",
                          "sync-%d" % self.seq, 250)

    def _page(self, items, params, kind, final_key, final_val, default_max):
        try:
            size = int(params.get("maxResults") or default_max)
        except ValueError:
            return gerr(400, "invalid", "Invalid maxResults")
        start = 0
        if params.get("pageToken"):
            m = re.match(r"^page-(\d+)$", params["pageToken"])
            if not m:
                return gerr(400, "invalid", "Invalid page token")
            start = int(m.group(1))
        chunk = items[start:start + size]
        out = {"kind": kind, "items": [self._public(i) for i in chunk]}
        if start + size < len(items):
            out["nextPageToken"] = "page-%d" % (start + size)
        elif final_key:
            out[final_key] = final_val
        return 200, out

    # -- tasks --------------------------------------------------------------
    def _tasks(self, method, segs, params, body):
        if segs[:3] == ["users", "@me", "lists"]:
            if len(segs) == 3:
                if method == "GET":
                    return 200, {"kind": "tasks#taskLists",
                                 "items": [self._public(t) for t in
                                           self.tasklists.values()]}
                if method == "POST":
                    tid = self._new_id("TL")
                    tl = dict(body, id=tid, kind="tasks#taskList")
                    self.tasklists[tid] = self._stamp(tl)
                    self.tasks[tid] = {}
                    return 200, self._public(tl)
            if len(segs) == 4:
                tl = self.tasklists.get(segs[3])
                if tl is None:
                    return gerr(404, "notFound",
                                "Requested entity was not found.")
                if method == "GET":
                    return 200, self._public(tl)
                if method == "PUT":
                    new = dict(body, id=segs[3], kind="tasks#taskList")
                    self.tasklists[segs[3]] = self._stamp(new)
                    return 200, self._public(new)
                if method == "DELETE":
                    del self.tasklists[segs[3]]
                    self.tasks.pop(segs[3], None)
                    return 204, None
        if len(segs) >= 3 and segs[0] == "lists" and segs[2] == "tasks":
            tl = segs[1]
            if tl not in self.tasklists:
                return gerr(404, "notFound", "Requested entity was not found.")
            ts = self.tasks.setdefault(tl, {})
            if len(segs) == 3 and method == "GET":
                items = sorted(ts.values(), key=lambda t: (t["_seq"], t["id"]))
                if params.get("updatedMin"):
                    items = [t for t in items
                             if t["updated"] >= params["updatedMin"]]
                if not _truthy(params.get("showDeleted")):
                    items = [t for t in items if not t.get("deleted")]
                if params.get("showCompleted") in (False, "false"):
                    items = [t for t in items
                             if t.get("status") != "completed"]
                return self._page(items, params, "tasks#tasks", None, None,
                                  100)
            if len(segs) == 3 and method == "POST":
                tid = self._new_id("T")
                t = dict(copy.deepcopy(body), id=tid, kind="tasks#task")
                ts[tid] = self._stamp(t)
                return 200, self._public(t)
            if len(segs) == 4:
                t = ts.get(segs[3])
                if t is None or (t.get("deleted") and method != "GET"):
                    return gerr(404, "notFound",
                                "Requested entity was not found.")
                if method == "GET":
                    return 200, self._public(t)
                if method == "PUT":
                    new = dict(copy.deepcopy(body), id=segs[3],
                               kind="tasks#task")
                    ts[segs[3]] = self._stamp(new)
                    return 200, self._public(new)
                if method == "DELETE":
                    t["deleted"] = True
                    self._stamp(t)
                    return 204, None
        return gerr(400, "badRequest", "Bad Request")
