"""DGN-1854 W5 -- record the real gws CLI against a local fake Google (D10+).

Manual tool, not a suite: needs the gws binary and one public discovery-doc
fetch. It never touches the owner's gws state: HOME and
GOOGLE_WORKSPACE_CLI_CONFIG_DIR point into a fresh temp dir, the token is the
literal string "fake", and the discovery cache is rewritten so gws's rootUrl
is http://127.0.0.1:<port>/. For every scenario in scenarios.py it records
what gws SENT (method, path, query, body) and what it PRINTED (stdout,
returncode), plus the kit-visible outcome via kit_oracle. Output:
fixtures/gws_recorded.json (checked in; the parity suite replays it offline).

Run: python3 service/calendar/conformance/record_gws.py [--gws <bin>]

English/ASCII only.
"""

import argparse
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import kit_oracle  # noqa: E402
import scenarios  # noqa: E402

OUT = os.path.join(_HERE, "fixtures", "gws_recorded.json")


class _State:
    respond = []
    requests = []


class _Handler(http.server.BaseHTTPRequestHandler):
    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        parts = urllib.parse.urlsplit(self.path)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        _State.requests.append({
            "method": self.command,
            "path": parts.path,
            "query": urllib.parse.parse_qsl(parts.query, keep_blank_values=True),
            "body": body,
            "if_match": self.headers.get("If-Match"),
            "content_type": self.headers.get("Content-Type"),
        })
        idx = min(len(_State.requests) - 1, len(_State.respond) - 1)
        r = _State.respond[idx]
        if "json" in r:
            out = json.dumps(r["json"]).encode()
            ctype = "application/json; charset=UTF-8"
        else:
            out = r.get("raw", "").encode()
            ctype = r.get("ctype", "application/json; charset=UTF-8")
        self.send_response(r["status"])
        if out or r["status"] != 204:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if out:
            self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, *a):
        pass


def _argv(args):
    return [json.dumps(a) if isinstance(a, dict) else a for a in args]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gws", default=shutil.which("gws") or "gws")
    ns = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="dgn1854-gwsrec-")
    env = {"PATH": os.environ.get("PATH", ""),
           "HOME": os.path.join(tmp, "home"),
           "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": os.path.join(tmp, "cfg"),
           "GOOGLE_WORKSPACE_CLI_TOKEN": "fake",
           "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file"}
    os.makedirs(env["HOME"])
    cwd = os.path.join(tmp, "cwd")  # gws loads ./.env -- keep cwd empty
    os.makedirs(cwd)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 1. populate the discovery cache (public doc), 2. point it at the fake
    for svc in (["calendar", "calendarList", "list"],
                ["tasks", "tasklists", "list"]):
        subprocess.run([ns.gws] + svc + ["--dry-run"], env=env, cwd=cwd,
                       capture_output=True, text=True, check=True)
    version = subprocess.run([ns.gws, "--version"], env=env, cwd=cwd,
                             capture_output=True, text=True).stdout.strip()
    cache = os.path.join(env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"], "cache")
    discovery = {}
    for name in sorted(os.listdir(cache)):
        p = os.path.join(cache, name)
        doc = json.load(open(p))
        discovery[name] = {"rootUrl": doc["rootUrl"],
                           "servicePath": doc.get("servicePath", "")}
        doc["rootUrl"] = "http://127.0.0.1:%d/" % port
        doc["baseUrl"] = doc["rootUrl"] + doc.get("servicePath", "")
        with open(p, "w") as f:
            json.dump(doc, f)

    records = []
    for sc in scenarios.ALL:
        _State.respond = sc["respond"]
        _State.requests = []
        cmd = [ns.gws] + _argv(sc["args"])
        if sc.get("body") is not None:
            cmd += ["--json", json.dumps(sc["body"])]
        res = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True,
                             text=True, timeout=60)
        records.append({
            "id": sc["id"],
            "kind": sc["kind"],
            "requests": _State.requests,
            "stdout": res.stdout,
            "returncode": res.returncode,
            "kit": kit_oracle.outcome(sc["kind"], res.stdout, res.stderr,
                                      res.returncode),
        })
        print("%-36s rc=%d reqs=%d %s" % (
            sc["id"], res.returncode, len(_State.requests),
            json.dumps(records[-1]["kit"].get("raises", "ok"))[:90]))
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    with open(OUT, "w") as f:
        json.dump({"gws_version": version, "discovery": discovery,
                   "records": records}, f, indent=1, sort_keys=True,
                  ensure_ascii=True)
        f.write("\n")
    print("wrote %s (%d records)" % (OUT, len(records)))


if __name__ == "__main__":
    main()
