"""DGN-1985: /login asks Claude or Codex; Codex onboarding from chat.

Covers bridge/codex_onboard.py: the picker and Codex screen (scope note,
derived status line, path buttons), the device-auth prompt parser, the
device-auth relay against a stub `codex` (URL + code relayed, approval picked
up by polling the status reader or by the child's exit, timeout, cancel, an
existing login never read as success), and the API-key path through the
DGN-1981 intake seam (unavailable by default; a stub intake + the real host
lib + a stub CLI never put the key in a log). Temp dirs only; no network.
"""

import asyncio
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge.tests.conftest  # noqa: F401
from bridge import codex_onboard as co
from bridge.i18n import ko

URL = "https://auth.openai.com/codex/device"
CODE = "QX7P-4KD2M"

# Prints the 0.161.0-shaped device prompt (ANSI-coloured, to stderr), then
# waits for the test to drop an "approve" or "deny" file.
DEVICE_STUB = textwrap.dedent('''\
    #!{py}
    import os, sys, time
    d = os.environ["STUB_DIR"]
    open(os.path.join(d, "argv.log"), "a").write(" ".join(sys.argv[1:]) + "\\n")
    url = os.environ.get("STUB_URL", "{url}")
    sys.stderr.write("\\nWelcome to Codex\\n\\nFollow these steps to sign in with ChatGPT "
                     "using device code authorization:\\n\\n"
                     "1. Open this link in your browser and sign in to your account\\n"
                     "   \\x1b[94m" + url + "\\x1b[0m\\n\\n"
                     "2. Enter this one-time code \\x1b[90m(expires in 15 minutes)\\x1b[0m\\n"
                     "   \\x1b[94m{code}\\x1b[0m\\n\\n"
                     "Device codes are a common phishing target. Never share this code.\\n")
    sys.stderr.flush()
    if os.environ.get("STUB_SILENT_EXIT"):
        sys.exit(0)
    while True:
        if os.path.exists(os.path.join(d, "approve")):
            open(os.path.join(d, "state"), "w").write("in")
            if os.environ.get("STUB_HANG_AFTER_APPROVE"):
                time.sleep(3600)
            sys.exit(0)
        if os.path.exists(os.path.join(d, "deny")):
            sys.stderr.write("Error: device code was denied\\n")
            sys.exit(1)
        time.sleep(0.02)
''')


def run(coro):
    return asyncio.run(coro)


class Render(unittest.TestCase):
    def test_pick_has_claude_and_codex(self):
        text, rows = co.render_pick()
        self.assertEqual(text, ko.STRINGS["login_pick"])
        self.assertEqual([cb for _, cb in rows[0]], [co.CB_CLAUDE, co.CB_CODEX])

    def test_codex_screen_states_scope_and_status(self):
        for st, line in (
            ({"onboarded": True, "state": "logged_in", "method": "chatgpt"}, "codex_status_in_chatgpt"),
            ({"onboarded": True, "state": "logged_in", "method": "api_key"}, "codex_status_in_apikey"),
            ({"onboarded": False, "state": "logged_out", "method": None}, "codex_status_out"),
            ({"onboarded": False, "state": "error", "method": None}, "codex_status_unknown"),
        ):
            text, rows = co.render_codex(st)
            self.assertTrue(text.startswith(ko.STRINGS["codex_scope"]))
            self.assertIn(ko.STRINGS[line], text)
            self.assertEqual([r[0][1] for r in rows], [co.CB_CX_SUB, co.CB_CX_KEY])

    def test_no_path_buttons_without_cli_or_lib(self):
        for state in ("no_cli", "unavailable"):
            _, rows = co.render_codex({"onboarded": False, "state": state, "method": None})
            self.assertEqual(rows, [])

    def test_callbacks_fit_telegram(self):
        for cb in (co.CB_CLAUDE, co.CB_CODEX, co.CB_CX_SUB, co.CB_CX_KEY):
            self.assertTrue(cb.startswith(co.CB))
            self.assertLessEqual(len(cb.encode()), 64)

    def test_status_without_lib_is_unavailable(self):
        with patch.object(co, "_lib", return_value=Path("/nonexistent/codex_login.py")):
            self.assertEqual(co.read_status()["state"], "unavailable")


class PromptParse(unittest.TestCase):
    PROMPT = ("Follow these steps:\n1. Open this link\n   \x1b[94m" + URL + "\x1b[0m\n\n"
              "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n"
              "   \x1b[94m" + CODE + "\x1b[0m\n\nNever share this code.\n")

    def test_url_and_code(self):
        self.assertEqual(co.extract_device_prompt(self.PROMPT), (URL, CODE))

    def test_incomplete_line_waits(self):
        cut = self.PROMPT.index(CODE) + 4
        self.assertIsNone(co.extract_device_prompt(self.PROMPT[:cut]))

    def test_url_allowlist(self):
        self.assertTrue(co.validate_device_url(URL))
        self.assertTrue(co.validate_device_url(URL + "/"))
        for bad in ("http://auth.openai.com/codex/device", "https://evil.example/codex/device",
                    "https://auth.openai.com:8443/codex/device",
                    "https://auth.openai.com/codex/device?x=1",
                    "https://auth.openai.com/oauth/authorize"):
            self.assertFalse(co.validate_device_url(bad), bad)


class DeviceFlowBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dgn1985-bridge-")
        self.stub_dir = os.path.join(self.tmp, "stub")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.stub_dir)
        os.makedirs(self.home)
        self.codex = os.path.join(self.stub_dir, "codex")
        with open(self.codex, "w") as f:
            f.write(DEVICE_STUB.format(py=sys.executable, url=URL, code=CODE))
        os.chmod(self.codex, os.stat(self.codex).st_mode | stat.S_IXUSR)
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "STUB_DIR": self.stub_dir}
        self.sent = []
        self.status_calls = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self):
        self.status_calls += 1
        on = os.path.exists(os.path.join(self.stub_dir, "state"))
        return {"onboarded": on, "state": "logged_in" if on else "logged_out",
                "method": "chatgpt" if on else None}

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def manager(self, **kw):
        kw.setdefault("status_reader", self.status)
        kw.setdefault("poll_interval", 0.05)
        kw.setdefault("prompt_timeout", 10.0)
        kw.setdefault("device_timeout", 10.0)
        return co.CodexDeviceLogin(send=self.send, cli_resolver=lambda: self.codex,
                                   lock_dir=self.tmp, env=self.env, **kw)

    async def wait_relay(self):
        for _ in range(400):
            if any(CODE in s for _, s in self.sent):
                return
            await asyncio.sleep(0.01)
        self.fail(f"no relay: {self.sent}")

    def touch(self, name):
        open(os.path.join(self.stub_dir, name), "w").close()

    async def drive(self, m, after_relay):
        self.assertEqual(await m.start(7, 70), "started")
        task = m.pending_for(7).task
        await self.wait_relay()
        await after_relay()
        await asyncio.wait_for(task, 10)


class DeviceFlow(DeviceFlowBase):
    def test_relay_then_status_poll_succeeds(self):
        self.env["STUB_HANG_AFTER_APPROVE"] = "1"
        m = self.manager()

        async def approve():
            self.touch("approve")

        with self.assertLogs("bridge.codex_onboard", level=logging.INFO) as logs:
            run(self.drive(m, approve))
        texts = [s for _, s in self.sent]
        relay = ko.STRINGS["codex_device_relay"].format(url=URL, code=CODE, minutes=1)
        self.assertEqual(texts, [ko.STRINGS["codex_device_preparing"], relay,
                                 ko.STRINGS["codex_success"]])
        self.assertTrue(all(cid == 70 for cid, _ in self.sent))
        self.assertTrue(any("success_polled" in r for r in logs.output))
        self.assertFalse(any(CODE in r for r in logs.output), logs.output)
        with open(os.path.join(self.stub_dir, "argv.log")) as f:
            self.assertEqual(f.read().split(), ["login", "--device-auth"])
        self.assertIsNone(m.pending_for(7))

    def test_child_exit_confirmed_by_status(self):
        m = self.manager(poll_interval=5.0)

        async def approve():
            self.touch("approve")

        run(self.drive(m, approve))
        self.assertEqual(self.sent[-1][1], ko.STRINGS["codex_success"])

    def test_denied(self):
        m = self.manager()

        async def deny():
            self.touch("deny")

        run(self.drive(m, deny))
        self.assertEqual(self.sent[-1][1], ko.STRINGS["codex_failed"])

    def test_existing_login_not_read_as_success(self):
        # Logged in before the flow: polling is off, only the child's exit
        # counts -- so a deny still fails even though status says logged in.
        self.touch("state")
        m = self.manager()

        async def deny():
            await asyncio.sleep(0.3)
            self.touch("deny")

        run(self.drive(m, deny))
        self.assertEqual(self.sent[-1][1], ko.STRINGS["codex_failed"])

    def test_timeout(self):
        m = self.manager(device_timeout=0.3)

        async def nothing():
            pass

        run(self.drive(m, nothing))
        self.assertEqual(self.sent[-1][1], ko.STRINGS["codex_timeout"])

    def test_cancel_and_duplicate(self):
        m = self.manager()

        async def go():
            self.assertEqual(await m.start(7, 70), "started")
            await self.wait_relay()
            self.assertEqual(await m.start(7, 70), "duplicate")
            self.assertTrue(await m.cancel(7))
            self.assertFalse(await m.cancel(7))

        run(go())
        texts = [s for _, s in self.sent]
        self.assertIn(ko.STRINGS["codex_already_pending"], texts)
        self.assertEqual(texts[-1], ko.STRINGS["codex_cancelled"])

    def test_bad_url_fails_closed(self):
        self.env["STUB_URL"] = "https://evil.example/codex/device"
        m = self.manager()

        async def go():
            await m.start(7, 70)
            await asyncio.wait_for(m.pending_for(7).task, 10)

        run(go())
        self.assertFalse(any(CODE in s for _, s in self.sent))
        self.assertEqual(self.sent[-1][1], ko.STRINGS["codex_failed"])

    def test_no_cli(self):
        m = co.CodexDeviceLogin(send=self.send, cli_resolver=lambda: None,
                                status_reader=self.status, lock_dir=self.tmp, env=self.env)
        self.assertEqual(run(m.start(7, 70)), "no_cli")
        self.assertEqual(self.sent, [(70, ko.STRINGS["codex_no_cli"])])


class KeyPath(unittest.TestCase):
    KEY = "sk-test-DGN1985-bridge-key-91b2"

    def setUp(self):
        self.sent = []

    async def send(self, chat_id, text):
        self.sent.append(text)

    def test_default_intake_unavailable(self):
        out = run(co.start_key_path(co.UnavailableIntake(), 7, 70, self.send,
                                    key_login=lambda name: self.fail("must not run")))
        self.assertEqual(out, "intake_unavailable")
        self.assertEqual(self.sent, [ko.STRINGS["codex_key_unavailable"]])

    def test_end_to_end_key_never_logged(self):
        """Stub intake -> real host lib (copied to a temp lib dir with a stub
        secret_store.py) -> stub codex. The key reaches the CLI's stdin and
        nothing else."""
        tmp = tempfile.mkdtemp(prefix="dgn1985-key-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root = Path(tmp) / "root"
        lib = root / "routines" / "lib"
        lib.mkdir(parents=True)
        src = Path(__file__).resolve().parents[2] / "routines" / "lib" / "codex_login.py"
        shutil.copy(src, lib)
        vault = Path(tmp) / "vault"
        (lib / "secret_store.py").write_text(
            "def get(service, account):\n"
            f"    return open({str(vault)!r}, 'rb').read()\n")
        stub_dir = Path(tmp) / "stub"
        stub_dir.mkdir()
        codex = stub_dir / "codex"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            f"d = {str(stub_dir)!r}\n"
            "open(os.path.join(d, 'argv.log'), 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:] == ['login', 'status']:\n"
            "    ok = os.path.exists(os.path.join(d, 'state'))\n"
            "    sys.stderr.write('Logged in using an API key - sk-***\\n' if ok else 'Not logged in\\n')\n"
            "    sys.exit(0 if ok else 1)\n"
            "key = sys.stdin.read().strip()\n"
            "open(os.path.join(d, 'stdin.got'), 'w').write(key)\n"
            "sys.stderr.write('echo ' + key + '\\n')\n"
            "open(os.path.join(d, 'state'), 'w').write('k')\n")
        codex.chmod(0o755)
        key = self.KEY

        class StubIntake(co.KeyIntake):
            async def request(self, user_id, chat_id, name, on_stored):
                assert name == co.SECRET_NAME
                vault.write_text(key)  # what DGN-1981 would put in the keychain
                await on_stored()
                return True

        with patch.object(co, "_lib", return_value=lib / "codex_login.py"), \
             patch.dict(os.environ, {"CODEX_BIN": str(codex)}), \
             self.assertLogs(level=logging.DEBUG) as logs:
            out = run(co.start_key_path(StubIntake(None, None), 7, 70, self.send))
        self.assertEqual(out, "asked")
        self.assertEqual(self.sent, [ko.STRINGS["codex_key_checking"], ko.STRINGS["codex_success"]])
        self.assertEqual((stub_dir / "stdin.got").read_text(), key)
        self.assertNotIn(key, (stub_dir / "argv.log").read_text())
        self.assertFalse(any(key in r for r in logs.output))
        self.assertFalse(any(key in s for s in self.sent))

    def test_failed_key_login(self):
        async def go():
            class I(co.KeyIntake):
                async def request(self, u, c, n, on_stored):
                    await on_stored()
                    return True
            return await co.start_key_path(I(None, None), 7, 70, self.send,
                                           key_login=lambda n: {"ok": False, "reason": "login_failed"})
        self.assertEqual(run(go()), "asked")
        self.assertEqual(self.sent[-1], ko.STRINGS["codex_key_failed"])


if __name__ == "__main__":
    unittest.main()
