"""DGN-1854 W5 -- K1 conformance runner (scenario fixtures vs a backend).

Runs fixtures/k1_scenarios.json against a connector backed by the fake
Google (fake_google.FakeGoogle via google_stub). Each scenario is a list of
steps; the Apple annex build reuses the same fixtures against its fake app.

Step ops:
  call    {"args": [svc, res, method], "params": {...}?, "body": ...?}
  delete  {"args": [...], "params": {...}?}
  http    {"method", "url", "body"?, "etag"?}
  health  {}                          -> expect {"value": cls|null}
  fake    {"do": purge|expire_sync|owner_edit|inject|auth, ...}
Expectations (key "expect"):
  {"has": {dotted.path: value}}       result contains these values
  {"count": {dotted.path: n}}         len() of a list in the result
  {"returns": value}                  result equals value (http: [status, json])
  {"raises": {"http_code", "reason", "paused"?}}
  {"sent": n}                         requests reaching the fake in this step
"save": {"var": dotted.path} stores a result value; "${var}" in any string of
a later step is replaced by it.

English/ASCII only.
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fake_google  # noqa: E402
import google_stub  # noqa: E402
from service.calendar import google as cal_google  # noqa: E402
from service.calendar.errors import CalendarPaused, GwsError  # noqa: E402

FIXTURE = os.path.join(_HERE, "fixtures", "k1_scenarios.json")


def load():
    with open(FIXTURE) as f:
        return json.load(f)["scenarios"]


def _sub(obj, env):
    if isinstance(obj, str):
        for k, v in env.items():
            obj = obj.replace("${%s}" % k, str(v))
        return obj
    if isinstance(obj, list):
        return [_sub(x, env) for x in obj]
    if isinstance(obj, dict):
        return {k: _sub(v, env) for k, v in obj.items()}
    return obj


def _get(obj, path):
    for part in path.split("."):
        if isinstance(obj, (list, tuple)):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


def run_scenario(sc, carry_body=True):
    """-> list of failure strings (empty = pass)."""
    fake = fake_google.FakeGoogle()
    mod = google_stub.make_module(fake, carry_body=carry_body)
    conn = cal_google.GoogleConnector(mod, mod.state.get("sub"))
    env = {}
    fails = []
    for i, raw in enumerate(sc["steps"]):
        step = _sub(raw, env)
        where = "%s step %d (%s)" % (sc["id"], i, step["op"])
        before = len(fake.requests)
        result, err = None, None
        try:
            op = step["op"]
            if op in ("call", "delete"):
                args = list(step["args"])
                if "params" in step:
                    args += ["--params", json.dumps(step["params"])]
                if op == "call":
                    result = conn.call(*args, body=step.get("body"))
                else:
                    result = conn.delete(*args)
            elif op == "http":
                result = list(conn.http(step["method"], step["url"],
                                        body=step.get("body"),
                                        etag=step.get("etag")))
            elif op == "health":
                result = conn.health()
            elif op == "fake":
                _fake_op(fake, mod, step)
            else:
                fails.append("%s: unknown op" % where)
                continue
        except GwsError as e:
            err = e
        except Exception as e:  # escaped the seam: always a failure
            fails.append("%s: non-GwsError escaped: %r" % (where, e))
            continue
        exp = step.get("expect", {})
        if err is not None and "raises" not in exp:
            fails.append("%s: unexpected %s %s %s" % (
                where, type(err).__name__, err.http_code, err.reason))
            continue
        if "raises" in exp:
            want = exp["raises"]
            if err is None:
                fails.append("%s: expected raise %s, got %r" % (
                    where, want, result))
            else:
                got = {"http_code": err.http_code, "reason": err.reason,
                       "paused": isinstance(err, CalendarPaused)}
                if any(got[k] != v for k, v in want.items()):
                    fails.append("%s: raised %s, want %s" % (where, got, want))
        if "returns" in exp and result != exp["returns"]:
            fails.append("%s: returned %r, want %r" % (
                where, result, exp["returns"]))
        if "value" in exp and result != exp["value"]:
            fails.append("%s: value %r, want %r" % (where, result,
                                                    exp["value"]))
        for path, val in exp.get("has", {}).items():
            try:
                got = _get(result, path)
            except (KeyError, IndexError, TypeError, ValueError):
                fails.append("%s: missing %s" % (where, path))
                continue
            if got != val:
                fails.append("%s: %s=%r, want %r" % (where, path, got, val))
        for path, n in exp.get("count", {}).items():
            try:
                got = len(_get(result, path))
            except (KeyError, IndexError, TypeError, ValueError):
                fails.append("%s: missing %s" % (where, path))
                continue
            if got != n:
                fails.append("%s: len(%s)=%d, want %d" % (where, path, got, n))
        if "sent" in exp and len(fake.requests) - before != exp["sent"]:
            fails.append("%s: sent %d requests, want %d" % (
                where, len(fake.requests) - before, exp["sent"]))
        for var, path in step.get("save", {}).items():
            try:
                env[var] = _get(result, path)
            except (KeyError, IndexError, TypeError, ValueError):
                fails.append("%s: cannot save %s from %s" % (where, var, path))
    return fails


def _fake_op(fake, mod, step):
    do = step["do"]
    if do == "purge":
        fake.purge(step["cal"], step["id"])
    elif do == "expire_sync":
        fake.expire_sync_tokens()
    elif do == "owner_edit":
        fake.owner_edit_event(step["cal"], step["id"], **step["fields"])
    elif do == "inject":
        fake.inject(step["method"], step["verb"], step["status"],
                    step["reason"], step.get("times", 1))
    elif do == "auth":
        mod.state.clear()
        mod.state.update(step["status"])
    else:
        raise ValueError("unknown fake op %s" % do)


def run_all(carry_body=True):
    out = {}
    for sc in load():
        out[sc["id"]] = run_scenario(sc, carry_body=carry_body)
    return out


if __name__ == "__main__":
    res = run_all()
    bad = {k: v for k, v in res.items() if v}
    for k, v in res.items():
        print("%-28s %s" % (k, "ok" if not v else "FAIL"))
        for f in v:
            print("    " + f)
    sys.exit(1 if bad else 0)
