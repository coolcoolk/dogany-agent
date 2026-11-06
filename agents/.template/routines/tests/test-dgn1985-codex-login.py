#!/usr/bin/env python3
# test-lane: fast
"""DGN-1985: routines/lib/codex_login.py -- the derived "Codex onboarded"
flag and the API-key login.

Hermetic: temp HOME, a stub `codex` on CODEX_BIN that answers `login status`
from a state file and records argv / stdin of `login --with-api-key`, and a
stub DGN-1981 intake (secret_store.py next to a copy of the lib). Checks:
status parse (logged in via ChatGPT / API key, logged out, error, ANSI),
the verbs' JSON and exit codes, and that the key reaches the CLI on stdin
only -- never argv, never the lib's stdout/stderr, never a log.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib", "codex_login.py")
sys.path.insert(0, os.path.dirname(LIB))
import codex_login as cl  # noqa: E402

KEY = "sk-test-DGN1985-not-a-real-key-7f3a9c"

STUB = textwrap.dedent('''\
    #!{py}
    import os, sys
    d = os.environ["STUB_DIR"]
    args = sys.argv[1:]
    with open(os.path.join(d, "argv.log"), "a") as f:
        f.write(" ".join(args) + "\\n")
    state = os.path.join(d, "state")
    if args == ["login", "status"]:
        s = open(state).read().strip() if os.path.exists(state) else "out"
        if s == "chatgpt":
            sys.stderr.write("\\x1b[32mLogged in using ChatGPT\\x1b[0m\\n"); sys.exit(0)
        if s == "api_key":
            sys.stderr.write("Logged in using an API key - sk-tes***9c\\n"); sys.exit(0)
        if s == "error":
            sys.stderr.write("Error checking login status: boom\\n"); sys.exit(1)
        sys.stderr.write("Not logged in\\n"); sys.exit(1)
    if args == ["login", "--with-api-key"]:
        key = sys.stdin.read().strip()
        open(os.path.join(d, "stdin.got"), "w").write(key)
        # Hostile child: echo the key on both streams; the lib must drop it.
        sys.stdout.write("got " + key + "\\n"); sys.stderr.write("got " + key + "\\n")
        if key.startswith("sk-"):
            open(state, "w").write("api_key"); sys.exit(0)
        sys.exit(1)
    sys.exit(2)
''')

INTAKE = textwrap.dedent('''\
    import os
    def get(service, account):
        if (service, account) != ("dogany-codex-api-key", "dogany"):
            return None
        p = os.environ.get("STUB_SECRET_FILE")
        return open(p, "rb").read() if p and os.path.exists(p) else None
''')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dgn1985-")
        self.home = os.path.join(self.tmp, "home")
        self.stub_dir = os.path.join(self.tmp, "stub")
        self.libdir = os.path.join(self.tmp, "lib")
        for d in (self.home, self.stub_dir, self.libdir):
            os.makedirs(d)
        self.codex = os.path.join(self.stub_dir, "codex")
        with open(self.codex, "w") as f:
            f.write(STUB.format(py=sys.executable))
        os.chmod(self.codex, os.stat(self.codex).st_mode | stat.S_IXUSR)
        shutil.copy(LIB, self.libdir)
        self.secret = os.path.join(self.tmp, "secret")
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "CODEX_BIN": self.codex,
                    "STUB_DIR": self.stub_dir, "STUB_SECRET_FILE": self.secret}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_state(self, s):
        with open(os.path.join(self.stub_dir, "state"), "w") as f:
            f.write(s)

    def run_lib(self, *args):
        return subprocess.run([sys.executable, "-B", os.path.join(self.libdir, "codex_login.py"),
                               *args], capture_output=True, text=True, env=self.env, timeout=60)


class ParseStatus(unittest.TestCase):
    def test_logged_in_chatgpt(self):
        self.assertEqual(cl.parse_status(0, "\x1b[32mLogged in using ChatGPT\x1b[0m\n"),
                         {"onboarded": True, "state": "logged_in", "method": "chatgpt"})

    def test_logged_in_api_key(self):
        st = cl.parse_status(0, "Logged in using an API key - sk-abc***xyz\n")
        self.assertEqual(st, {"onboarded": True, "state": "logged_in", "method": "api_key"})
        self.assertNotIn("sk-", json.dumps(st))

    def test_logged_out(self):
        self.assertEqual(cl.parse_status(1, "Not logged in\n")["state"], "logged_out")
        # "Not logged in" wins even on rc 0 (never read as a login).
        self.assertFalse(cl.parse_status(0, "Not logged in\n")["onboarded"])

    def test_error_and_unknown(self):
        for rc, text in ((1, "Error checking login status: x"), (0, ""), (0, "hello"),
                         (1, "Logged in using ChatGPT")):
            st = cl.parse_status(rc, text)
            self.assertEqual((st["onboarded"], st["state"]), (False, "error"), (rc, text))


class StatusVerb(Base):
    def test_status_follows_the_cli(self):
        for state, want in (("out", (False, "logged_out", None)),
                            ("chatgpt", (True, "logged_in", "chatgpt")),
                            ("api_key", (True, "logged_in", "api_key")),
                            ("error", (False, "error", None))):
            self.set_state(state)
            p = self.run_lib("status")
            self.assertEqual(p.returncode, 0)
            st = json.loads(p.stdout)
            self.assertEqual((st["onboarded"], st["state"], st["method"]), want, state)
            self.assertEqual(self.run_lib("onboarded").returncode, 0 if want[0] else 1)

    def test_no_cli(self):
        self.env["CODEX_BIN"] = os.path.join(self.tmp, "missing")
        st = json.loads(self.run_lib("status").stdout)
        self.assertEqual(st, {"onboarded": False, "state": "no_cli", "method": None})
        self.assertEqual(self.run_lib("resolve").returncode, 1)

    def test_resolve(self):
        p = self.run_lib("resolve")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, self.codex))


class ApiKeyPath(Base):
    def _intake(self):
        with open(os.path.join(self.libdir, "secret_store.py"), "w") as f:
            f.write(INTAKE)

    def _assert_key_nowhere(self, p):
        self.assertNotIn(KEY, p.stdout)
        self.assertNotIn(KEY, p.stderr)
        with open(os.path.join(self.stub_dir, "argv.log")) as f:
            self.assertNotIn(KEY, f.read())

    def test_intake_absent(self):
        p = self.run_lib("apikey")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["reason"], "intake_unavailable")

    def test_no_secret_stored(self):
        self._intake()
        p = self.run_lib("apikey")
        self.assertEqual(json.loads(p.stdout)["reason"], "no_secret")

    def test_key_piped_on_stdin_only(self):
        self._intake()
        with open(self.secret, "w") as f:
            f.write(KEY + "\n")
        p = self.run_lib("apikey")
        res = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual((res["ok"], res["reason"], res["method"]), (True, "ok", "api_key"))
        with open(os.path.join(self.stub_dir, "stdin.got")) as f:
            self.assertEqual(f.read(), KEY)
        with open(os.path.join(self.stub_dir, "argv.log")) as f:
            self.assertIn("login --with-api-key", f.read())
        self._assert_key_nowhere(p)

    def test_rejected_key_reports_and_hides(self):
        self._intake()
        bad = "not-a-key-DGN1985-zz"
        with open(self.secret, "w") as f:
            f.write(bad)
        p = self.run_lib("apikey")
        res = json.loads(p.stdout)
        self.assertEqual((p.returncode, res["ok"], res["reason"]), (1, False, "login_failed"))
        self.assertNotIn(bad, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
