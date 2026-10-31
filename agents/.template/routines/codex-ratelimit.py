#!/usr/bin/env python3
"""codex-ratelimit.py -- read the codex CLI weekly quota via the app-server
JSON-RPC handshake (DGN-1444) and classify read failures for the accumulating
failure ledger.

WHY THIS EXISTS: the owner's condition ("codex first, but keep 10%+ headroom
on the weekly window") is only enforceable if the weekly usedPercent can be
read headlessly. `codex exec` does not expose it; the interactive TUI's
/usage does, and upstream has no CLI flag yet (openai/codex#15281). The
app-server DOES expose it over JSON-RPC. Measured 2026-09-13:

  spawn `codex app-server`, keep stdin OPEN (closing it makes the process
  exit before answering -- a one-shot pipe returns empty), then:
    -> {"id":1,"method":"initialize","params":{"clientInfo":{...}}}
    <- {"id":1,"result":{...}}
    -> {"method":"initialized","params":{}}                (notification, no id)
    -> {"id":2,"method":"account/rateLimits/read",
        "params":{"excludeResetCreditDetails":true}}
    <- {"id":2,"result":{"rateLimits":{"primary":{"usedPercent":N,
                          "windowDurationMins":10080,"resetsAt":...}}}}

  Other notifications (e.g. "remoteControl/status/changed") can arrive
  interleaved and must be skipped, not mistaken for the answer.

SCOPE: this script only PROBES and CLASSIFIES a read. It never decides
proceed/refuse/fallback -- that policy (usedPercent<=90 proceed, >90 refuse
+ owner approval, read-failed -> fall back to claude-code) lives in
dispatch-detached.sh, the one place that also knows the dispatch intent and,
moments later, whether the claude-code fallback itself worked. Recording a
failure is therefore a SEPARATE explicit action (--record-failure) rather
than a side effect of reading: a plain probe (self-test, a human checking by
hand) never writes a half-known record with a guessed intent/fallback.

TAXONOMY (DGN-1444, tagged [assumption] -- built from zero real failures
before this script existed; an unmatched failure lands as "unknown" with the
raw text preserved rather than being force-fit into a wrong bucket):
  spawn_fail    codex app-server failed to start (binary missing/permission)
  no_response   initialize answered, but the process exited before
                account/rateLimits/read ever got a reply
  auth_expired  the response (or exit) carries an auth/token-expiry signature
  parse_fail    a reply arrived but is not valid JSON, or is valid JSON
                missing the expected result.rateLimits.primary shape (CLI
                schema drift)
  timeout       still waiting past the timeout budget (network/backend lag)
  unknown       none of the above -- e.g. a well-formed JSON-RPC error with
                an unrecognized message. If this bucket fills up, the
                taxonomy itself is stale and needs updating from the raw
                evidence, not the other way around.

Exit codes:
  0  quota read OK           (--json prints {"ok": true, ...})
  1  usage/argument error    (bad CLI args, --record-failure without --kind,
                              cannot write the failures ledger, ...)
  2  quota read FAILED       (--json prints {"ok": false, "kind": ..., ...})
     -- the caller must never confuse this with a healthy quota.
"""
import argparse
import json
import os
import queue
import select
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

DEFAULT_TIMEOUT_S = 20.0
RATE_LIMIT_METHOD = "account/rateLimits/read"
FAILURES_LEDGER_NAME = os.path.join(".telegram_bot", "codex-ratelimit-failures.jsonl")

# Substrings checked case-insensitively against a failure response/exit text.
# Kept narrow (real vendor wording, not a guess) -- this is the ONE bucket
# where a false match would misfile a real quota-availability incident as a
# credential problem, so precision matters more than recall here.
AUTH_EXPIRED_PATTERNS = (
    "token_expired",
    "token expired",
    "authentication token is expired",
    "not authenticated",
    "please log in",
    "please login",
    "401 unauthorized",
)


def _looks_like_auth_expired(text):
    low = (text or "").lower()
    return any(p in low for p in AUTH_EXPIRED_PATTERNS)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _reader_thread(pipe, out_q):
    """Feeds every line from pipe into out_q; None on EOF. Runs in a daemon
    thread so the main thread can enforce a real wall-clock deadline via
    queue.get(timeout=...) instead of blocking on readline() directly."""
    try:
        for line in iter(pipe.readline, ""):
            out_q.put(line)
    except Exception:
        pass
    finally:
        out_q.put(None)


def _read_all_nonblocking(pipe, max_bytes=8192):
    """Best-effort, NEVER-BLOCKING drain of whatever is already buffered on
    pipe. Used only to enrich the 'raw' field of a failure record -- must
    never itself hang the classification it is trying to help produce."""
    if pipe is None:
        return ""
    try:
        fd = pipe.fileno()
    except Exception:
        return ""
    chunks = []
    total = 0
    while total < max_bytes:
        try:
            ready, _, _ = select.select([fd], [], [], 0)
        except Exception:
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except Exception:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks).decode("utf-8", "replace").strip()


def _terminate(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def probe_rate_limits(codex_cmd, timeout_s):
    """Runs the handshake once. Returns a dict:
      ok=True  -> {"ok": True, "used_percent", "window_duration_mins",
                   "resets_at", "plan_type", "elapsed_ms"}
      ok=False -> {"ok": False, "kind", "raw", "elapsed_ms"}
    Never raises: every failure mode this function itself can hit is a
    classified result, not an exception (the caller must never see a crash
    where it expects a classification)."""
    start = time.monotonic()

    def elapsed_ms():
        return int((time.monotonic() - start) * 1000)

    try:
        proc = subprocess.Popen(
            codex_cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except OSError as e:
        return {"ok": False, "kind": "spawn_fail", "raw": str(e), "elapsed_ms": elapsed_ms()}

    out_q = queue.Queue()
    reader = threading.Thread(target=_reader_thread, args=(proc.stdout, out_q), daemon=True)
    reader.start()
    budget_end = start + timeout_s

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def read_response(want_id):
        """(doc, eof) for the line whose id == want_id, skipping notifications
        and other ids. doc is {"__unparsed__": <raw line>} on a JSON parse
        failure. (None, False) on timeout, (None, True) on EOF with nothing
        matching."""
        while True:
            remaining = budget_end - time.monotonic()
            if remaining <= 0:
                return None, False
            try:
                item = out_q.get(timeout=remaining)
            except queue.Empty:
                return None, False
            if item is None:
                return None, True
            item = item.strip()
            if not item:
                continue
            try:
                doc = json.loads(item)
            except Exception:
                return {"__unparsed__": item}, False
            if isinstance(doc, dict) and doc.get("id") == want_id:
                return doc, False
            # Notification or a reply to a different id -- keep waiting.
            continue

    def fail_on_no_answer(eof, stage):
        stderr_tail = _read_all_nonblocking(proc.stderr)
        _terminate(proc)
        if eof:
            kind = "auth_expired" if _looks_like_auth_expired(stderr_tail) else "no_response"
            raw = stderr_tail or f"(process exited before answering {stage})"
        else:
            kind = "timeout"
            raw = stderr_tail or f"(timed out waiting for {stage} response, budget={timeout_s}s)"
        return {"ok": False, "kind": kind, "raw": raw, "elapsed_ms": elapsed_ms()}

    try:
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "dogany-codex-ratelimit",
                                         "title": "dogany-codex-ratelimit",
                                         "version": "1"}}})
    except (BrokenPipeError, OSError) as e:
        _terminate(proc)
        return {"ok": False, "kind": "spawn_fail", "raw": str(e), "elapsed_ms": elapsed_ms()}

    init_doc, init_eof = read_response(1)
    if init_doc is None:
        return fail_on_no_answer(init_eof, "initialize")
    if "__unparsed__" in init_doc:
        _terminate(proc)
        return {"ok": False, "kind": "parse_fail", "raw": init_doc["__unparsed__"], "elapsed_ms": elapsed_ms()}
    if "error" in init_doc:
        _terminate(proc)
        msg = json.dumps(init_doc["error"])
        kind = "auth_expired" if _looks_like_auth_expired(msg) else "unknown"
        return {"ok": False, "kind": kind, "raw": msg, "elapsed_ms": elapsed_ms()}

    try:
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": RATE_LIMIT_METHOD,
              "params": {"excludeResetCreditDetails": True}})
    except (BrokenPipeError, OSError) as e:
        # The process answered initialize but died before the real request --
        # a spawn already succeeded, so this is "stopped answering", not a
        # spawn failure.
        _terminate(proc)
        return {"ok": False, "kind": "no_response", "raw": str(e), "elapsed_ms": elapsed_ms()}

    rl_doc, rl_eof = read_response(2)
    if rl_doc is None:
        return fail_on_no_answer(rl_eof, RATE_LIMIT_METHOD)
    _terminate(proc)
    if "__unparsed__" in rl_doc:
        return {"ok": False, "kind": "parse_fail", "raw": rl_doc["__unparsed__"], "elapsed_ms": elapsed_ms()}
    if "error" in rl_doc:
        msg = json.dumps(rl_doc["error"])
        kind = "auth_expired" if _looks_like_auth_expired(msg) else "unknown"
        return {"ok": False, "kind": kind, "raw": msg, "elapsed_ms": elapsed_ms()}
    try:
        rate_limits = rl_doc["result"]["rateLimits"]
        primary = rate_limits["primary"]
        used_percent = primary["usedPercent"]
    except (KeyError, TypeError):
        return {"ok": False, "kind": "parse_fail", "raw": json.dumps(rl_doc), "elapsed_ms": elapsed_ms()}
    return {
        "ok": True,
        "used_percent": used_percent,
        "window_duration_mins": primary.get("windowDurationMins"),
        "resets_at": primary.get("resetsAt"),
        "plan_type": rate_limits.get("planType"),
        "elapsed_ms": elapsed_ms(),
    }


def append_failure_record(path, ts, kind, raw, elapsed_ms, intent, fallback):
    """Sole writer of the append-only, no-rotation failure ledger (DGN-1444:
    'know when/how it fails before reacting to it'). raw is written
    UNTRUNCATED -- a wrong classification needs the full text to re-diagnose,
    and truncation is exactly how DGN-1440-style guessing starts."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "ts": ts,
        "kind": kind,
        "raw": raw,
        "elapsed_ms": elapsed_ms,
        "intent": intent,
        "fallback": fallback,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Self-test fixtures: minimal stdlib-only JSON-RPC stand-ins for `codex
# app-server`, one per taxonomy bucket. Each is written to a temp file and
# run as `python3 <file>` in place of the real binary via probe_rate_limits's
# codex_cmd parameter -- no real codex install or network needed to verify
# the classification logic.
# ---------------------------------------------------------------------------
_FIXTURE_PRELUDE = "import sys, json\n"

_FIXTURE_HEALTHY = _FIXTURE_PRELUDE + """
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        doc = json.loads(line)
    except Exception:
        continue
    method = doc.get("method")
    if method == "initialize":
        print(json.dumps({"id": doc["id"], "result": {}})); sys.stdout.flush()
    elif method == "account/rateLimits/read":
        print(json.dumps({"id": doc["id"], "result": {"rateLimits": {
            "primary": {"usedPercent": 42, "windowDurationMins": 10080,
                        "resetsAt": 1234567890},
            "planType": "plus"}}})); sys.stdout.flush()
"""

_FIXTURE_TIMEOUT = _FIXTURE_PRELUDE + """
import time
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        doc = json.loads(line)
    except Exception:
        continue
    method = doc.get("method")
    if method == "initialize":
        print(json.dumps({"id": doc["id"], "result": {}})); sys.stdout.flush()
    elif method == "account/rateLimits/read":
        time.sleep(60)
"""

_FIXTURE_MALFORMED = _FIXTURE_PRELUDE + """
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        doc = json.loads(line)
    except Exception:
        continue
    method = doc.get("method")
    if method == "initialize":
        print(json.dumps({"id": doc["id"], "result": {}})); sys.stdout.flush()
    elif method == "account/rateLimits/read":
        print("not-json-{{{"); sys.stdout.flush()
"""

_FIXTURE_UNKNOWN = _FIXTURE_PRELUDE + """
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        doc = json.loads(line)
    except Exception:
        continue
    method = doc.get("method")
    if method == "initialize":
        print(json.dumps({"id": doc["id"], "result": {}})); sys.stdout.flush()
    elif method == "account/rateLimits/read":
        print(json.dumps({"id": doc["id"], "error": {
            "code": -32601, "message": "Method not found (server too old)"}}))
        sys.stdout.flush()
"""

_FIXTURE_AUTH_EXPIRED = _FIXTURE_PRELUDE + """
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        doc = json.loads(line)
    except Exception:
        continue
    method = doc.get("method")
    if method == "initialize":
        print(json.dumps({"id": doc["id"], "result": {}})); sys.stdout.flush()
    elif method == "account/rateLimits/read":
        print(json.dumps({"id": doc["id"], "error": {
            "code": 401, "message": "Provided authentication token is expired."}}))
        sys.stdout.flush()
"""

_FIXTURE_NO_RESPONSE = _FIXTURE_PRELUDE + """
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        doc = json.loads(line)
    except Exception:
        continue
    method = doc.get("method")
    if method == "initialize":
        print(json.dumps({"id": doc["id"], "result": {}})); sys.stdout.flush()
    elif method == "account/rateLimits/read":
        sys.exit(0)
"""


def run_self_tests():
    scenarios = {
        "healthy": (_FIXTURE_HEALTHY, "ok", 3.0),
        "timeout": (_FIXTURE_TIMEOUT, "timeout", 1.5),
        "malformed": (_FIXTURE_MALFORMED, "parse_fail", 3.0),
        "unknown": (_FIXTURE_UNKNOWN, "unknown", 3.0),
        "auth_expired": (_FIXTURE_AUTH_EXPIRED, "auth_expired", 3.0),
        "no_response": (_FIXTURE_NO_RESPONSE, "no_response", 3.0),
    }
    all_ok = True
    with tempfile.TemporaryDirectory() as td:
        for name, (src, want_kind, budget) in scenarios.items():
            path = os.path.join(td, name + ".py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            result = probe_rate_limits([sys.executable, path], budget)
            if name == "healthy":
                good = result.get("ok") is True and result.get("used_percent") == 42
            else:
                good = result.get("ok") is False and result.get("kind") == want_kind
            print(f"[self-test] {name}: {'PASS' if good else 'FAIL'} -- {result}")
            all_ok = all_ok and good

        result = probe_rate_limits(["/nonexistent/dogany-codex-fake-binary"], 3.0)
        good = result.get("ok") is False and result.get("kind") == "spawn_fail"
        print(f"[self-test] spawn_fail: {'PASS' if good else 'FAIL'} -- {result}")
        all_ok = all_ok and good

        fpath = os.path.join(td, "failures.jsonl")
        append_failure_record(fpath, _now_iso(), "timeout", "raw text here", 1234,
                               "label=x model=sonnet axis=accuracy", "claude-code")
        with open(fpath, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        good = (len(lines) == 1 and lines[0]["kind"] == "timeout"
                and lines[0]["raw"] == "raw text here"
                and lines[0]["elapsed_ms"] == 1234
                and lines[0]["fallback"] == "claude-code")
        print(f"[self-test] record-failure round-trip: {'PASS' if good else 'FAIL'} -- {lines}")
        all_ok = all_ok and good

    print(f"[self-test] {'ALL PASS' if all_ok else 'SOME FAILED'}")
    return all_ok


def _default_failures_path():
    workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(workspace, FAILURES_LEDGER_NAME)


def build_argparser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   help=f"overall handshake budget in seconds (default {DEFAULT_TIMEOUT_S})")
    p.add_argument("--codex-cmd", default=None,
                   help="override the app-server command line, shell-quoted "
                        "(testing only; default: 'codex app-server')")
    p.add_argument("--failures-path", default=None,
                   help="override the failure ledger path (testing only)")
    p.add_argument("--record-failure", action="store_true",
                   help="append one failure record instead of probing")
    p.add_argument("--kind", default=None)
    p.add_argument("--raw", default="")
    p.add_argument("--elapsed-ms", type=int, default=0)
    p.add_argument("--intent", default="")
    p.add_argument("--fallback", default="")
    p.add_argument("--self-test", action="store_true",
                   help="run built-in classification self-tests and exit")
    return p


def main(argv=None):
    args = build_argparser().parse_args(argv)

    if args.self_test:
        sys.exit(0 if run_self_tests() else 1)

    failures_path = args.failures_path or _default_failures_path()

    if args.record_failure:
        if not args.kind:
            print("[codex-ratelimit] --record-failure requires --kind", file=sys.stderr)
            sys.exit(1)
        try:
            append_failure_record(failures_path, _now_iso(), args.kind, args.raw,
                                   args.elapsed_ms, args.intent, args.fallback)
        except OSError as e:
            print(f"[codex-ratelimit] could not append failure record: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"[codex-ratelimit] recorded failure kind={args.kind} -> {failures_path}")
        sys.exit(0)

    codex_cmd = shlex.split(args.codex_cmd) if args.codex_cmd else ["codex", "app-server"]
    try:
        result = probe_rate_limits(codex_cmd, args.timeout)
    except Exception as e:
        # A bug in this script must still classify, not crash silently --
        # the taxonomy's own "unknown" bucket is the correct landing spot.
        result = {"ok": False, "kind": "unknown", "raw": f"{type(e).__name__}: {e}", "elapsed_ms": 0}

    if args.json:
        print(json.dumps(result))
    elif result.get("ok"):
        print("[codex-ratelimit] usedPercent=%s window=%smin resetsAt=%s elapsed=%sms" % (
            result.get("used_percent"), result.get("window_duration_mins"),
            result.get("resets_at"), result.get("elapsed_ms")))
    else:
        print("[codex-ratelimit] READ FAILED kind=%s elapsed=%sms" % (
            result.get("kind"), result.get("elapsed_ms")))
        print("[codex-ratelimit]   raw: %s" % result.get("raw"), file=sys.stderr)

    sys.exit(0 if result.get("ok") else 2)


if __name__ == "__main__":
    main()
