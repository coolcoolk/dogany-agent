#!/usr/bin/env python3
"""probe.py -- one tiny API call with the credit key (check.sh probe).

Key arrives on STDIN only (never argv, never env of a child, never printed).
1) GET /v1/models    -- proves the key is accepted (free).
2) POST /v1/messages -- 1 output token on the cheapest listed model, proves
   the credit balance can pay (the API answers "credit balance too low" here).
Prints one verdict line: ok | key_rejected <why> | credit_low <why> |
failed <why>. Exit 0 = ok, 1 = anything else. stdlib only.
DOGANY_API_PROBE_BASE_URL overrides the endpoint (fixtures only).
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("DOGANY_API_PROBE_BASE_URL", "https://api.anthropic.com").rstrip("/")
VERSION = "2023-06-01"
TIMEOUT = 60


def scrub(text):
    return re.sub(r"sk-ant-[A-Za-z0-9_-]+", "[redacted]", text)[:160]


def call(key, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", VERSION)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload


def err_text(payload):
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return "%s: %s" % (err.get("type", ""), err.get("message", ""))
    return ""


def verdict(code, payload):
    msg = err_text(payload)
    if re.search(r"credit balance|billing", msg, re.I):
        return "credit_low " + scrub(msg)
    if code in (401, 403):
        return "key_rejected http=%d %s" % (code, scrub(msg))
    return "failed http=%d %s" % (code, scrub(msg))


def main():
    key = sys.stdin.readline().strip()
    if not key:
        print("missing key unreadable at call time")
        return 1
    try:
        code, payload = call(key, "GET", "/v1/models?limit=100")
        if code != 200:
            print(verdict(code, payload))
            return 1
        ids = [m.get("id", "") for m in payload.get("data", []) if isinstance(m, dict)]
        cheap = [i for i in ids if "haiku" in i] or ids
        if not cheap:
            print("failed no model listed for this key")
            return 1
        code, payload = call(key, "POST", "/v1/messages", {
            "model": cheap[0], "max_tokens": 1,
            "messages": [{"role": "user", "content": "ok"}]})
        if code != 200:
            print(verdict(code, payload))
            return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("failed network " + scrub(str(e)))
        return 1
    finally:
        key = ""
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
