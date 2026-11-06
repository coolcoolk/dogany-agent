#!/usr/bin/env python3
# test-lane: fast
"""DGN-1979 phase 2 (dec-359): every BASE resource book re-reads on an
account / plan / policy change instead of aging out on TTL.

Owner 2026-10-10 17:21 (Q3): per account, yes -- but it must follow an
account switch, a subscription change and a policy change.
Offline: temp HOME + DOGANY_SHARED_HOME, fixture credential files, a stub
`codex` (CODEX_BIN), a stub curl for claude-usage.sh, file secret backend.
Covers:
  - relogin to another Claude account: the host usage snapshot moves to
    read-only history and the same call re-reads live; one decision line,
    no raw account id in it; the workspace usage-gate cache + history too;
    the Claude reset-pass store switches with one line;
  - plan change (credential rateLimitTier): snapshot + usage-gate cache
    invalidated at once; usage-gate thresholds follow the read; PLAN=
    overrides a read and stands alone without one;
  - API credit budget from the plan read (PLAN= override / no read);
  - Codex logout / login: login status + account fp lines, the codex pass
    store follows;
  - API key rotation: the old account dir is marked superseded (history),
    current pointer moves, the key never lands anywhere;
  - policy value change (API_CREDIT_MONTHLY_USD): exhausted marker and
    reconcile state to history, ledger rows kept;
  - dispatcher: a queued ticket on a lane whose account changed is moved to
    the new lane (age kept) and granted there, not lost; a ticket queued
    before phase 2 is left alone.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)
LIB = os.path.join(ROUTINES, "lib")
sys.path.insert(0, LIB)
import book_identity as bi  # noqa: E402
import api_credit_pool as pool  # noqa: E402
import host_dispatcher as hd  # noqa: E402
import reset_passes as rp  # noqa: E402

UUID_A = "11111111-aaaa-4aaa-8aaa-000000000001"
UUID_B = "22222222-bbbb-4bbb-8bbb-000000000002"
KEY_1 = "sk-ant-api03-FIXTUREKEYONE-0000000000000000"
KEY_2 = "sk-ant-api03-FIXTUREKEYTWO-1111111111111111"
NOW = 1791600000.0

CURL = """#!/usr/bin/env bash
cat >/dev/null
echo call >> "$STUB_CALLS"
printf '{"five_hour":{"utilization":%s,"resets_at":"2099-01-01T00:00:00Z"},"seven_day":{"utilization":20,"resets_at":"2099-01-02T00:00:00Z"}}' "${STUB_FH:-10}"
printf '\\n__HTTP_STATUS__:200'
"""
CODEX = """#!/usr/bin/env bash
if [[ "$1 $2" == "login status" ]]; then
  if [[ -f "$STUB_CODEX_IN" ]]; then echo "Logged in using ChatGPT"; exit 0; fi
  echo "Not logged in"; exit 1
fi
exit 0
"""


def _rm_fixture(path):
    """DGN-2090: remove THIS test's own fixture dir. The terminal-state ledger
    it wrote is chflags uappend (macOS), which makes a bare rmtree leave the
    dir behind; clear the flags on this path only, then remove it."""
    if shutil.which("chflags"):
        subprocess.run(["chflags", "-R", "nouappend,nouchg", path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(path, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = tempfile.mkdtemp(prefix="dgn1979p2-")
        self.saved = dict(os.environ)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.environ.update(HOME=self.home, DOGANY_SHARED_HOME=os.path.join(self.tmp, "shared"),
                          DOGANY_SECRET_BACKEND="file", DOGANY_BOOK_PLAN_READ="1",
                          DOGANY_SECRET_DIR=os.path.join(self.tmp, "secrets"),
                          DOGANY_DISPATCH_MACHINE="1 8 32000 0", DOGANY_DISPATCH_BOOT="boot-1",
                          DOGANY_DISPATCH_MONO="1000", DOGANY_DISPATCH_NOW=str(NOW))
        for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PLAN", "API_CREDIT_MONTHLY_USD",
                  "DOGANY_USAGE_SNAPSHOT_DIR"):
            os.environ.pop(k, None)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        for name, body in (("curl", CURL), ("codex", CODEX)):
            p = os.path.join(bin_dir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + self.saved.get("PATH", "/usr/bin:/bin")
        os.environ["CODEX_BIN"] = os.path.join(bin_dir, "codex")
        os.environ["STUB_CALLS"] = os.path.join(self.tmp, "curl-calls.log")
        os.environ["STUB_CODEX_IN"] = os.path.join(self.tmp, "codex-in")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            with contextlib.suppress(Exception):
                p.kill()
                p.wait()
        os.environ.clear()
        os.environ.update(self.saved)
        for root, dirs, files in os.walk(self.tmp):
            for n in dirs + files:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(root, n), 0o755)
        _rm_fixture(self.tmp)

    # fixtures ---------------------------------------------------------------
    def login_claude(self, uuid, tier="default_claude_max_20x", sub="max"):
        with open(os.path.join(self.home, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"accountUuid": uuid, "emailAddress": "x@example.com"},
                       "numStartups": 3}, f)
        oauth = {"accessToken": "tok-fixture-SECRET", "expiresAt": 4102444800000,
                 "subscriptionType": sub}
        if tier:
            oauth["rateLimitTier"] = tier
        with open(os.path.join(self.home, ".claude", ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": oauth}, f)
        self.bump_mtime(os.path.join(self.home, ".claude.json"))
        self.bump_mtime(os.path.join(self.home, ".claude", ".credentials.json"))

    def bump_mtime(self, path):
        # stat-keyed cache: make every rewrite visible even within one tick
        st = os.stat(path)
        self._tick = getattr(self, "_tick", 0) + 1
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + self._tick * 1000))

    def login_codex(self, account_id):
        d = os.path.join(self.home, ".codex")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "auth.json"), "w") as f:
            json.dump({"tokens": {"account_id": account_id, "access_token": "codex-SECRET"}}, f)
        open(os.environ["STUB_CODEX_IN"], "w").close()

    def logout_codex(self):
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(self.home, ".codex", "auth.json"))
        with contextlib.suppress(OSError):
            os.unlink(os.environ["STUB_CODEX_IN"])

    def changes(self, book=None):
        rows = hd.read_lines(bi.ledger_path())
        return [r for r in rows if r.get("kind") == "book-change" and (book is None or r["book"] == book)]

    def ledger_text(self):
        try:
            with open(bi.ledger_path()) as f:
                return f.read()
        except OSError:
            return ""

    def assert_no_secrets(self):
        txt = self.ledger_text()
        for s in (UUID_A, UUID_B, KEY_1, KEY_2, "tok-fixture-SECRET", "codex-SECRET", "acct-codex-1"):
            self.assertNotIn(s, txt)

    def usage_json(self):
        r = subprocess.run(["bash", os.path.join(ROUTINES, "claude-usage.sh"), "--json"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def curl_calls(self):
        try:
            with open(os.environ["STUB_CALLS"]) as f:
                return len(f.read().split())
        except OSError:
            return 0


class ClaudeUsageBook(Base):
    def test_relogin_other_account_rereads_at_once(self):
        self.login_claude(UUID_A)
        os.environ["STUB_FH"] = "11"
        d = self.usage_json()
        self.assertEqual(d["_snapshot"]["source"], "live")
        self.assertEqual(self.usage_json()["_snapshot"]["source"], "cache")
        self.assertEqual(self.curl_calls(), 1)
        self.assertEqual(self.changes(), [])            # first identity adopted, no line
        # relogin to account B inside the TTL
        self.login_claude(UUID_B)
        os.environ["STUB_FH"] = "77"
        d = self.usage_json()
        self.assertEqual(d["_snapshot"]["source"], "live")  # not the cached A numbers
        self.assertEqual(d["five_hour"]["utilization"], 77)
        self.assertEqual(self.curl_calls(), 2)
        rows = self.changes("claude-usage")
        self.assertEqual(len(rows), 1)
        self.assertEqual([c["what"] for c in rows[0]["changed"]], ["claude.account"])
        self.assertEqual(rows[0]["changed"][0]["from"], bi.fp(UUID_A))
        self.assertIn("snapshot.json", rows[0]["invalidated"])
        old = os.path.join(rows[0]["history"], "snapshot.json")
        with open(old) as f:
            self.assertEqual(json.load(f)["five_hour"]["utilization"], 11)  # history kept
        self.assertFalse(os.stat(old).st_mode & stat.S_IWUSR)               # read-only
        self.assert_no_secrets()

    def test_plan_change_rereads_at_once(self):
        self.login_claude(UUID_A, tier="default_claude_max_5x")
        self.usage_json()
        self.login_claude(UUID_A, tier="default_claude_max_20x")
        self.assertEqual(self.usage_json()["_snapshot"]["source"], "live")
        rows = self.changes("claude-usage")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["changed"], [{"what": "claude.plan", "from": "max_5x", "to": "ultra"}])

    def test_no_change_no_reread(self):
        self.login_claude(UUID_A)
        self.usage_json()
        # the CLI rewrites ~/.claude.json all the time; same account -> nothing
        self.login_claude(UUID_A)
        self.assertEqual(self.usage_json()["_snapshot"]["source"], "cache")
        self.assertEqual(self.changes(), [])


def load_usage_gate(ws):
    os.makedirs(os.path.join(ws, "routines"), exist_ok=True)
    os.makedirs(os.path.join(ws, "config"), exist_ok=True)
    shutil.copy(os.path.join(ROUTINES, "usage-gate.py"), os.path.join(ws, "routines", "usage-gate.py"))
    shutil.copytree(LIB, os.path.join(ws, "routines", "lib"),
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))
    with open(os.path.join(ws, "routines", "claude-usage.sh"), "w") as f:
        f.write("#!/bin/bash\nexec bash %s \"$@\"\n" % os.path.join(ROUTINES, "claude-usage.sh"))
    os.chmod(os.path.join(ws, "routines", "claude-usage.sh"), 0o755)
    spec = importlib.util.spec_from_file_location("_ug_%d" % id(ws), os.path.join(ws, "routines", "usage-gate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class UsageGateCopy(Base):
    def test_workspace_cache_follows_account_and_plan(self):
        ws = os.path.join(self.tmp, "ws")
        ug = load_usage_gate(ws)
        self.login_claude(UUID_A, tier="default_claude_max_5x")
        os.environ["STUB_FH"] = "12"
        self.assertEqual(ug.get_usage()["five_hour_util"], 12.0)
        self.assertEqual(len(ug.usage_history()), 1)
        os.environ["STUB_FH"] = "13"
        self.assertEqual(ug.get_usage()["five_hour_util"], 12.0)  # cache inside TTL
        self.login_claude(UUID_B, tier="default_claude_max_5x")
        self.assertEqual(ug.get_usage()["five_hour_util"], 13.0)  # B read at once
        self.assertEqual(len(ug.usage_history()), 1)               # A's burn samples not mixed in
        rows = self.changes("usage-gate")
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0]["invalidated"]), sorted([ug.CACHE_NAME, ug.HISTORY_NAME]))
        self.assert_no_secrets()

    def test_plan_read_and_plan_override(self):
        ws = os.path.join(self.tmp, "ws")
        ug = load_usage_gate(ws)
        self.assertEqual(ug.read_plan(ws), "pro")                   # no read, no PLAN= -> pro
        self.login_claude(UUID_A, tier="default_claude_max_20x")
        ug._PLAN_READ.clear()
        self.assertEqual(ug.read_plan(ws), "ultra")               # the read
        with open(os.path.join(ws, "config", "agent.conf"), "w") as f:
            f.write("PLAN=max_5x\n")
        self.assertEqual(ug.read_plan(ws), "max_5x")                # PLAN= overrides the read
        self.login_claude(UUID_A, tier=None, sub="max")             # tier unknown -> no read
        ug._PLAN_READ.clear()
        self.assertEqual(ug.read_plan(ws), "max_5x")                # PLAN= alone, as before


class Budget(unittest.TestCase):
    def test_ultra_tier(self):
        self.assertEqual(bi.plan_from_oauth({"rateLimitTier": "default_claude_ultra"}), "ultra")

    def test_old_tier_is_ultra(self):
        self.assertEqual(bi.plan_from_oauth({"rateLimitTier": "default_claude_max_20x"}), "ultra")

    def test_plan_alias(self):
        self.assertEqual(bi.effective_plan("max_20x", None)[0], "ultra")

    def test_ultra_credit(self):
        self.assertEqual(pool.resolve_budget("", "ultra")[0], 200.0)

    def test_budget_from_plan_read(self):
        self.assertEqual(pool.resolve_budget("", "", plan_read="max_20x")[0], 200.0)
        usd, src = pool.resolve_budget("", "max_5x", plan_read="max_20x")
        self.assertEqual(usd, 100.0)
        self.assertIn("PLAN=max_5x override (plan read ultra)", src)
        usd, src = pool.resolve_budget("", "max_5x")
        self.assertEqual((usd, src), (100.0, "plan max_5x"))        # no read: unchanged
        usd, src = pool.resolve_budget("150", "", plan_read="max_20x")
        self.assertEqual(usd, 150.0)
        self.assertIn("API_CREDIT_MONTHLY_USD override", src)       # still the override
        self.assertEqual(pool.resolve_budget("", "", plan_read="pro")[0], None)


class CodexBook(Base):
    def test_logout_login(self):
        sd = os.path.join(self.tmp, "codex-book")
        self.login_codex("acct-codex-1")
        self.assertEqual(bi.check("codex-quota", sd, bi.codex_components(window_mins=10080))[0], "new")
        self.assertEqual(bi.check("codex-quota", sd, bi.codex_components(window_mins=10080))[0], "same")
        self.logout_codex()
        v, changed, _ = bi.check("codex-quota", sd, bi.codex_components(window_mins=10080))
        self.assertEqual(v, "changed")
        what = {c["what"]: (c["from"], c["to"]) for c in changed}
        self.assertEqual(what["codex.login"], ("logged_in", "logged_out"))
        self.assertEqual(what["codex.account"], (bi.fp("acct-codex-1"), "none"))
        self.login_codex("acct-codex-2")
        v, changed, _ = bi.check("codex-quota", sd, bi.codex_components(window_mins=10080))
        self.assertEqual(v, "changed")
        self.assertEqual(len(self.changes("codex-quota")), 2)
        # published window length is part of the identity
        v, changed, _ = bi.check("codex-quota", sd, bi.codex_components(window_mins=4320))
        self.assertEqual([c["what"] for c in changed], ["codex.window"])
        # the codex pass store follows the account, one line per switch
        rp.resolve_store("codex")
        self.login_codex("acct-codex-3")
        path, fp = rp.resolve_store("codex")
        self.assertEqual(fp, bi.fp("acct-codex-3"))
        self.assertEqual(len(self.changes("reset-passes-codex")), 1)
        self.assert_no_secrets()

    def test_cli_check_codex(self):
        self.login_codex("acct-codex-1")
        r = lambda: subprocess.run([sys.executable, "-B", os.path.join(LIB, "book_identity.py"),
                                    "check-codex", "--window-mins", "10080"],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(r().returncode, 0)
        self.logout_codex()
        out = r()
        self.assertEqual(out.returncode, 10, out.stdout)
        self.assertTrue(out.stdout.startswith("changed\t"))


class ClaudePasses(Base):
    def test_relogin_switches_store_with_one_line(self):
        self.login_claude(UUID_A)
        _, fa = rp.resolve_store("claude")
        rp.resolve_store("claude")
        self.login_claude(UUID_B)
        _, fb = rp.resolve_store("claude")
        self.assertNotEqual(fa, fb)
        rows = self.changes("reset-passes-claude")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["changed"][0]["to"], fb)
        self.assert_no_secrets()


class ApiBook(Base):
    def account(self, key):
        p = subprocess.run([sys.executable, "-B", os.path.join(LIB, "api_credit_pool.py"), "account"],
                           input=key, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.split("\t")[0]

    def test_key_rotation(self):
        d1 = self.account(KEY_1)
        self.assertEqual(bi.current_api_fp(), pool.key_fingerprint(KEY_1))
        self.assertEqual(self.account(KEY_1), d1)
        self.assertEqual(self.changes("api-key"), [])
        d2 = self.account(KEY_2)
        self.assertNotEqual(d1, d2)
        self.assertEqual(bi.current_api_fp(), pool.key_fingerprint(KEY_2))
        with open(os.path.join(d1, bi.API_SUPERSEDED)) as f:
            self.assertEqual(json.load(f)["superseded_by"], pool.key_fingerprint(KEY_2))
        rows = self.changes("api-key")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["changed"][0]["to"], pool.key_fingerprint(KEY_2))
        # rotating back: that dir is current again, not superseded
        self.account(KEY_1)
        self.assertFalse(os.path.exists(os.path.join(d1, bi.API_SUPERSEDED)))
        self.assert_no_secrets()
        for root, _, files in os.walk(os.path.join(self.home, ".dogany")):
            for n in files:
                with open(os.path.join(root, n), errors="replace") as f:
                    self.assertNotIn(KEY_1, f.read())

    def test_policy_change_invalidates_marker_and_reconcile(self):
        adir = self.account(KEY_1)
        conf = os.path.join(self.tmp, "agent.conf")
        with open(conf, "w") as f:
            f.write("PLAN=max_20x\nAPI_CREDIT_MONTHLY_USD=200\n")
        self.assertEqual(bi.check_api_book(adir, conf)[0], "new")
        for name, doc in (("exhausted.json", {"month": "2026-10"}), ("reconcile.json", {"fetched_at": NOW})):
            with open(os.path.join(adir, name), "w") as f:
                json.dump(doc, f)
        with open(os.path.join(adir, "ledger.jsonl"), "w") as f:
            f.write(json.dumps({"runid": "r1", "cost_usd": 3.0, "month": "2026-10"}) + "\n")
        self.assertEqual(bi.check_api_book(adir, conf)[0], "same")
        with open(conf, "w") as f:
            f.write("PLAN=max_20x\nAPI_CREDIT_MONTHLY_USD=350\n")
        v, changed, hist = bi.check_api_book(adir, conf)
        self.assertEqual(v, "changed")
        self.assertEqual(changed, [{"what": "policy.API_CREDIT_MONTHLY_USD", "from": "200", "to": "350"}])
        self.assertFalse(os.path.exists(os.path.join(adir, "exhausted.json")))
        self.assertFalse(os.path.exists(os.path.join(adir, "reconcile.json")))
        self.assertTrue(os.path.exists(os.path.join(hist, "exhausted.json")))
        self.assertTrue(os.path.exists(os.path.join(adir, "ledger.jsonl")))  # spend facts stay
        # env beats conf, same as dispatch's api_pool_conf
        os.environ["API_CREDIT_MONTHLY_USD"] = "400"
        self.assertEqual(bi.check_api_book(adir, conf)[0], "changed")
        self.assertEqual(len(self.changes("api-credit")), 2)
        self.assert_no_secrets()


class DispatcherLanes(Base):
    def run_hd(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hd.main([str(a) for a in args])
        return rc, out.getvalue().rstrip("\n").split("\t")

    def holder(self):
        p = subprocess.Popen(["sleep", "300"])
        self.procs.append(p)
        return p.pid

    def acct(self, rt="claude-code"):
        return hd.account_for(rt)

    def enqueue(self, agent, runtime="claude-code"):
        f = os.path.join(self.tmp, "argv.json")
        with open(f, "w") as fh:
            json.dump({"argv": ["--task", "x"], "env": {}}, fh)
        rc, out = self.run_hd("enqueue", "--agent", agent, "--runtime", runtime, "--cap", 1,
                              "--account", self.acct(runtime), "--argv-file", f, "--script", "/bin/true")
        self.assertEqual(out[0], "queued", out)
        return out[1]

    def test_queued_ticket_rescored_on_new_account_lane(self):
        self.login_claude(UUID_A)
        lane_a = hd.lane_key("claude-code", bi.fp(UUID_A))
        rc, g = self.run_hd("acquire", "--agent", "A", "--runtime", "claude-code", "--cap", 1,
                            "--account", self.acct(), "--pid", self.holder())
        self.assertEqual(g[0], "granted")
        tid = self.enqueue("B")
        self.assertEqual(hd.load_state()["tickets"][tid]["lane"], lane_a)
        age0 = hd.load_state()["tickets"][tid]["submitted_awake"]
        # relogin to B: the ticket follows on the next transaction
        self.login_claude(UUID_B)
        lane_b = hd.lane_key("claude-code", bi.fp(UUID_B))
        os.environ["DOGANY_DISPATCH_MONO"] = "1060"
        self.run_hd("drain")
        tickets = hd.load_state()["tickets"]
        # lane B has room (A's run holds lane A) -> granted there, not lost
        self.assertNotIn(tid, tickets)
        rel = [r for r in hd.read_lines(hd._p("decisions.jsonl")) if r["kind"] == "relane"]
        self.assertEqual([(r["ticket"], r["was"], r["lane"]) for r in rel], [(tid, lane_a, lane_b)])
        grants = [r for r in hd.read_lines(hd._p("decisions.jsonl"))
                  if r["kind"] == "grant" and r.get("ticket") == tid]
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["lane"], lane_b)
        self.assertNotIn(UUID_B, open(hd._p("decisions.jsonl")).read())
        self.assertIsNotNone(age0)

    def test_relane_keeps_age_and_waits_on_full_new_lane(self):
        self.login_claude(UUID_A)
        self.run_hd("acquire", "--agent", "A", "--runtime", "claude-code", "--cap", 1,
                    "--account", self.acct(), "--pid", self.holder())
        tid = self.enqueue("B")
        age0 = hd.load_state()["tickets"][tid]["submitted_awake"]
        # the new lane is full too: agent C (its own config dir) already runs on B
        rc, g = self.run_hd("acquire", "--agent", "C", "--runtime", "claude-code", "--cap", 1,
                            "--account", bi.fp(UUID_B), "--pid", self.holder())
        self.assertEqual(g[0], "granted")
        self.login_claude(UUID_B)
        os.environ["DOGANY_DISPATCH_MONO"] = "1060"
        self.run_hd("drain")
        t = hd.load_state()["tickets"][tid]                          # still queued, not dropped
        self.assertEqual(t["lane"], hd.lane_key("claude-code", bi.fp(UUID_B)))
        self.assertEqual(t["submitted_awake"], age0)                # aging kept
        self.assertEqual(len(t["relaned"]), 1)
        # a fresh request on lane B does not jump the relaned ticket
        rc, g = self.run_hd("acquire", "--agent", "D", "--runtime", "claude-code", "--cap", 2,
                            "--account", self.acct(), "--pid", self.holder())
        self.assertEqual(g[0], "full", g)
        self.assertIn("rank ahead", g[2])

    def test_codex_logout_relanes(self):
        self.login_codex("acct-codex-1")
        self.run_hd("acquire", "--agent", "A", "--runtime", "codex", "--cap", 1,
                    "--account", self.acct("codex"), "--pid", self.holder())
        tid = self.enqueue("B", runtime="codex")
        self.logout_codex()
        self.run_hd("drain")
        rel = [r for r in hd.read_lines(hd._p("decisions.jsonl")) if r["kind"] == "relane"]
        self.assertEqual(rel[0]["lane"], "codex@unknown")
        self.assertEqual(rel[0]["ticket"], tid)

    def test_pre_phase2_ticket_left_alone(self):
        self.login_claude(UUID_A)
        self.run_hd("acquire", "--agent", "A", "--runtime", "claude-code", "--cap", 1,
                    "--account", self.acct(), "--pid", self.holder())
        tid = self.enqueue("B")
        path = hd._p("queue", tid + ".json")
        with open(path) as f:
            doc = json.load(f)
        doc.pop("acct_seen")
        hd.write_json(path, doc)
        self.login_claude(UUID_B)
        self.run_hd("drain")
        rel = [r for r in hd.read_lines(hd._p("decisions.jsonl")) if r["kind"] == "relane"]
        self.assertEqual(rel, [])


if __name__ == "__main__":
    t0 = time.time()
    res = unittest.main(exit=False, verbosity=1).result
    print("elapsed %.1fs" % (time.time() - t0))
    sys.exit(0 if res.wasSuccessful() else 1)
