#!/usr/bin/env python3
# test-lane: fast
"""v2.7 D3/D4 rework: the script-side devices that replaced hook rows
(docs/v27-D3-D4-grill.md section D, docs/v27-D3-D4-spec.md section 6).

The Bash hook sees argv only, so the rows it could not hold moved to the
place that gives the action its meaning:
  (a) bridge/self_restart.sh refuses its owner-only flags (--trigger user,
      --force, --idle-mins below the default, --_worker without its
      launcher) from an agent tool call without a resolved owner quote
      (grill F7: fixtures bash-wrapped-self-restart, self-restart-idle-mins-0,
      self-restart-_worker); its idle guard reads mtimes portably (F29);
  (b) scripts/release-approval.py binds the owner's words to {tag, sha};
      release.sh gate 5bf and the --execute publishers check it (F17:
      bash-x-release);
  (c) git-hooks/pre-push judges the real remote URL against an allowlist and
      every refs/tags/* ref against the release record (F18: tag-after-m,
      push to a public URL);
  (d) routines/lib/owner_quote_check.py, the one CLI door those three
      share, answers with the D3 hook's own binding (lib/user_yes.device_yes)
      in the live caller's session (lib/device_gate.live_caller).
Grill round 2 (docs/v27-D3-D4-grill-r2.md), class AgentBinding and the
device_gate units: an unrelated owner sentence approves nothing (R2-6), a
cron session cannot borrow the bridge session's words (R2-7), and env can
neither loosen a mode nor name a resolver, allowlist or trust root for an
agent caller (R2-8); pre-push judges the push before the missing-sweep exit.
Release confirmation always blocks when absent; other devices default to
detect. The tests also exercise explicit block mode.  The stub-resolver tests run in TEST MODE (DOGANY_TEST_MODE=1,
no CLAUDECODE): the only mode in which the devices honour a stub.  These
devices are ACCIDENT-grade, not adversary-grade (spec section 6).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUTINES = HERE.parent
LIB = ROUTINES / "lib"
TEMPLATE = ROUTINES.parent
REPO = TEMPLATE.parent.parent
SELF_RESTART = TEMPLATE / "bridge" / "self_restart.sh"
RELEASE_SCRIPTS = REPO / "extracted-devkit" / "framework" / "scripts"
RELEASE_APPROVAL = RELEASE_SCRIPTS / "release-approval.py"
PRE_PUSH = TEMPLATE / "git-hooks" / "pre-push"

STUB_RESOLVER = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
q = args[args.index("--quote") + 1]
if q in ("브릿지 재시작해줘", "v2.7 릴리스 진행해", "공개 미러에 올려"):
    print(json.dumps({"runtime": "claude-code", "session_id": "s1",
                      "message_uuid": "u1", "said_at": "2026-10-08T00:00:00Z"}))
    sys.exit(0)
print("refused quote-not-owner-turn")
sys.exit(1)
'''


def _block(text, begin, end):
    i = text.index(begin)
    j = text.index(end, i)
    return text[i:j]


def _test_env(**kw):
    """A non-agent caller in test mode: the only env in which the devices
    honour the stub seams (DOGANY_OWNER_QUOTE_CHECK, DOGANY_PRIVATE_REMOTE_URLS)."""
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")) and k not in ("CLAUDECODE", "SELF_RESTART_OWNER_GATE",
                      "SELF_RESTART_LAUNCH_TOKEN", "RELEASE_APPROVAL_MODE",
                      "DOGANY_PUSH_GATE_MODE", "DOGANY_PUSH_APPROVAL_QUOTE",
                      "DOGANY_PRIVATE_REMOTE_URLS", "DOGANY_OWNER_QUOTE_CHECK",
                      "DOGANY_RELEASE_APPROVALS", "DOGANY_DISPATCH_WORKER")}
    for key in ("DGN_WORKER_TEST_HELD", "DGN_HEAVY_LOCK_HELD"):
        if key in os.environ:
            e[key] = os.environ[key]
    e["DOGANY_TEST_MODE"] = "1"
    e.update(kw)
    return e


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v27-devices-")
        self.stub = os.path.join(self.tmp, "owner_quote_check.py")
        Path(self.stub).write_text(STUB_RESOLVER, encoding="utf-8")
        os.chmod(self.stub, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SelfRestart(Base):
    """(a) the owner-flag gate, run verbatim from self_restart.sh."""

    def _gate(self, flags, env=None, token=False, conf=None):
        root = os.path.join(self.tmp, "inst")
        os.makedirs(os.path.join(root, "routines", "lib"), exist_ok=True)
        os.makedirs(os.path.join(root, ".telegram_bot", "state",
                                 "self-restart-launch"), exist_ok=True)
        shutil.copy(self.stub, os.path.join(root, "routines", "lib",
                                            "owner_quote_check.py"))
        shutil.copy(LIB / "device_gate.py",
                    os.path.join(root, "routines", "lib", "device_gate.py"))
        os.makedirs(os.path.join(root, "config"), exist_ok=True)
        if conf is not None:
            Path(root, "config", "agent.conf").write_text(conf)
        block = _block(SELF_RESTART.read_text(encoding="utf-8"),
                       "# V27-OWNER-GATE-BEGIN", "# V27-OWNER-GATE-END")
        v = {"FORCE": "", "TRIGGER": "auto", "IDLE_MINS": "10", "WORKER": "",
             "OWNER_QUOTE": ""}
        v.update(flags)
        pre = "".join("%s=%s\n" % (k, json.dumps(x, ensure_ascii=False))
                      for k, x in v.items())
        pre += "INSTANCE_ROOT=%s\nV27_DEFAULT_IDLE_MINS=10\n" % json.dumps(root)
        e = _test_env(CLAUDECODE="1", SELF_RESTART_OWNER_GATE="block")
        if token:
            Path(root, ".telegram_bot", "state", "self-restart-launch",
                 "tok123").write_text("")
            e["SELF_RESTART_LAUNCH_TOKEN"] = "tok123"
        e.update(env or {})
        p = subprocess.run(["bash", "-c", pre + block +
                            "\nv27_owner_flag_gate\necho PASSED"],
                           capture_output=True, text=True, env=e)
        return p.returncode, p.stdout + p.stderr, root









    def test_selected_restart_flags_need_no_owner_quote_even_with_old_block_setting(self):
        for flags in ({'TRIGGER': 'user'}, {'FORCE': '1'}, {'IDLE_MINS': '0'}, {'WORKER': '1'}):
            rc, out, _ = self._gate(flags)
            self.assertEqual(0, rc, out)
            self.assertNotIn('REFUSED', out)

    def test_auto_restart_with_reason_passes(self):
        rc, out, _ = self._gate({})
        self.assertEqual(0, rc, out)

    def test_gate_runs_before_anything_else(self):
        """The gate sits right after the parser, before the idle guard and
        the detach, and --owner-quote is a parsed flag."""
        src = SELF_RESTART.read_text(encoding="utf-8")
        self.assertLess(src.index("\nv27_owner_flag_gate\n"),
                        src.index("\n  check_idle_guard\n"))
        self.assertIn('--owner-quote) OWNER_QUOTE="$2"', src)

    def test_f29_idle_guard_reads_mtime_portably(self):
        """On Linux `stat -f %m` read 0 and the guard always passed."""
        src = SELF_RESTART.read_text(encoding="utf-8")
        fn = _block(src, "check_idle_guard() {", "\nSPOOL_DIR=")
        self.assertNotIn("-exec stat -f", fn)
        home = os.path.join(self.tmp, "home")
        root = os.path.join(self.tmp, "inst2")
        os.makedirs(root)
        enc = re.sub(r"[^a-zA-Z0-9]", "-", root)
        proj = os.path.join(home, ".claude", "projects", enc)
        os.makedirs(proj)
        Path(proj, "live.jsonl").write_text("{}\n")  # touched just now
        pre = ('TRIGGER=auto\nFORCE=""\nIDLE_MINS=10\nINSTANCE_ROOT=%s\n'
               % json.dumps(root))
        p = subprocess.run(["bash", "-c", pre + fn + "\ncheck_idle_guard\n"
                            "echo PASSED"], capture_output=True, text=True,
                           env=dict(os.environ, HOME=home))
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)
        self.assertIn("session active", p.stderr)
        old = time.time() - 3600
        os.utime(os.path.join(proj, "live.jsonl"), (old, old))
        p = subprocess.run(["bash", "-c", pre + fn + "\ncheck_idle_guard\n"
                            "echo PASSED"], capture_output=True, text=True,
                           env=dict(os.environ, HOME=home))
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)


def _git(repo, *args, **kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_CONFIG_", "DGN_", "DOGANY_"))}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    env.update(kw.pop("env", {}))
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                          text=True, env=env, check=kw.pop("check", True))


class Repo(Base):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "scripts"))
        shutil.copy(RELEASE_APPROVAL, os.path.join(self.repo, "scripts"))
        shutil.copy(RELEASE_SCRIPTS / "release-quote-check.py",
                    os.path.join(self.repo, "scripts"))
        _git(self.repo, "init", "-q", "-b", "fixture")
        Path(self.repo, "README").write_text("x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")
        self.sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        # the copied release-approval.py (pre-push's tag check) finds its
        # device_gate the way the canonical checkout does; kept out of
        # `git status` so "recording never changes the tree" still holds
        tpl = os.path.join(self.repo, "agents", ".template", "routines")
        os.makedirs(tpl)
        os.symlink(str(LIB), os.path.join(tpl, "lib"))
        with open(os.path.join(self.repo, ".git", "info", "exclude"),
                  "a") as fh:
            fh.write("agents/\n")
        self.env = _test_env(DOGANY_OWNER_QUOTE_CHECK=self.stub,
                             GIT_CONFIG_GLOBAL="/dev/null")

    def ra(self, *args, env=None):
        e = dict(self.env, **(env or {}))
        return subprocess.run([sys.executable, str(RELEASE_APPROVAL),
                               "--repo", self.repo] + list(args),
                              capture_output=True, text=True, env=e,
                              stdin=subprocess.DEVNULL)


class ReleaseApproval(Repo):
    """(b) bash-x-release: whatever ran release.sh, the script checks."""

    def test_record_and_check(self):
        p = self.ra("--check", "--tag", "v2.7.0", "--sha", self.sha,
                    "--mode", "block")
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)
        p = self.ra("--record", "--tag", "v2.7.0", "--sha", self.sha,
                    "--quote", "응")
        self.assertEqual(1, p.returncode, "a quote the resolver refuses")
        p = self.ra("--record", "--tag", "v2.7.0", "--sha", self.sha,
                    "--quote", "v2.7 릴리스 진행해")
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        p = self.ra("--check", "--tag", "v2.7.0", "--sha", self.sha,
                    "--mode", "block")
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        # bound to the exact tag and commit
        p = self.ra("--check", "--tag", "v2.7.1", "--sha", self.sha,
                    "--mode", "block")
        self.assertEqual(1, p.returncode)
        p = self.ra("--check", "--tag", "v2.7.0", "--sha", "0" * 40,
                    "--mode", "block")
        self.assertEqual(1, p.returncode)
        # recording never changes the tree (the store lives in .git)
        self.assertEqual("", _git(self.repo, "status", "--porcelain").stdout)

    def test_hand_written_record_fails_audit(self):
        store = Path(self.repo, ".git", "dogany-release-approvals.jsonl")
        store.write_text(json.dumps({"tag": "v2.7.0", "sha": self.sha,
                                     "quote": "ship it"}) + "\n")
        p = self.ra("--check", "--tag", "v2.7.0", "--sha", self.sha,
                    "--mode", "block")
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)
        self.assertIn("no longer resolves", p.stderr)

    def test_default_release_mode_blocks(self):
        p = self.ra("--check", "--tag", "v2.7.0", "--sha", self.sha)
        self.assertEqual(1, p.returncode)
        self.assertIn("not approved", p.stderr)

    def test_bash_x_release_wiring(self):
        """release.sh gate 5bf and the --execute publishers call the check
        (the hook no longer judges these scripts)."""
        rel = (REPO / "scripts" / "release.sh").read_text(encoding="utf-8")
        # DGN-1967 step 13c: the checker is the one beside release.sh
        self.assertIn('RELEASE_APPROVAL="$SELF_DIR/release-approval.py"', rel)
        self.assertIn('"$RELEASE_APPROVAL" --check --repo "$REPO" --tag "$TAG" '
                      '--sha "$REL_SHA" --mode block', rel)
        self.assertLess(rel.index('gate "5bf'), rel.index('gate "6:'))
        for name in ("publish.sh", "kit-publish.sh", "publish-bridge.sh"):
            src = (REPO / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("release-approval.py\" --check", src, name)


class PrePush(Repo):
    """(c) the real remote URL and every tag ref, whatever ran the push."""

    def setUp(self):
        super().setUp()
        hooks = os.path.join(self.tmp, "hookroot", "git-hooks")
        os.makedirs(hooks)
        os.makedirs(os.path.join(self.tmp, "hookroot", "routines"))
        os.symlink(str(LIB), os.path.join(self.tmp, "hookroot", "routines",
                                          "lib"))
        shutil.copy(PRE_PUSH, hooks)  # no run-tests.sh beside it: lane skipped
        self.hook = os.path.join(hooks, "pre-push")
        sweep = os.path.join(self.tmp, "sweep.sh")
        Path(sweep).write_text("#!/bin/sh\necho 'RESULT: clean'\nexit 0\n")
        os.chmod(sweep, 0o755)
        self.env.update({"DOGANY_SWEEP_SCRIPT": sweep,
                         "DOGANY_PUSH_GATE_MODE": "block"})

    def push(self, url, lines, **env):
        e = dict(self.env, **env)
        return subprocess.run(["bash", self.hook, "origin", url],
                              input="".join(l + "\n" for l in lines),
                              capture_output=True, text=True, env=e,
                              cwd=self.repo)

    def branch_line(self):
        return "refs/heads/main %s refs/heads/main %s" % (self.sha, "0" * 40)

    def test_public_url_needs_owner_words(self):
        url = "https://github.com/x/public.git"
        p = self.push(url, [self.branch_line()])
        self.assertEqual(1, p.returncode, p.stderr)
        self.assertIn("not on the private allowlist", p.stderr)
        p = self.push(url, [self.branch_line()],
                      DOGANY_PUSH_APPROVAL_QUOTE="공개 미러에 올려")
        self.assertEqual(0, p.returncode, p.stderr)
        p = self.push(url, [self.branch_line()],
                      DOGANY_PUSH_APPROVAL_QUOTE="push it")
        self.assertEqual(1, p.returncode, p.stderr)

    def test_allowlisted_url_passes(self):
        _git(self.repo, "config", "--add", "dogany.privateRemote",
             "https://github.com/me/*")
        p = self.push("https://github.com/me/agent.git", [self.branch_line()])
        self.assertEqual(0, p.returncode, p.stderr)
        p = self.push("https://github.com/x/public.git", [self.branch_line()],
                      DOGANY_PRIVATE_REMOTE_URLS="https://github.com/x/*")
        self.assertEqual(0, p.returncode, p.stderr)

    def test_tag_after_m_needs_release_record(self):
        _git(self.repo, "config", "--add", "dogany.privateRemote", "*")
        _git(self.repo, "tag", "-m", "notes", "v2.7.0")
        tag_obj = _git(self.repo, "rev-parse", "v2.7.0").stdout.strip()
        line = "refs/tags/v2.7.0 %s refs/tags/v2.7.0 %s" % (tag_obj, "0" * 40)
        p = self.push("git@github.com:me/agent.git", [line])
        self.assertEqual(1, p.returncode, p.stderr)
        self.assertIn("tag v2.7.0 has no owner release approval", p.stderr)
        r = self.ra("--record", "--tag", "v2.7.0", "--sha", self.sha,
                    "--quote", "v2.7 릴리스 진행해")
        self.assertEqual(0, r.returncode, r.stderr)
        p = self.push("git@github.com:me/agent.git", [line])
        self.assertEqual(0, p.returncode, p.stderr)

    def test_detect_mode_warns_only(self):
        p = self.push("https://github.com/x/public.git", [self.branch_line()],
                      DOGANY_PUSH_GATE_MODE="detect")
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn("WARN (v2.7 push gate", p.stderr)

    def test_hook_copies_identical(self):
        self.assertEqual((REPO / "git-hooks" / "pre-push").read_bytes(),
                         PRE_PUSH.read_bytes())

    def test_pre_push_gate_before_sweep_exit(self):
        """R2-8: a non-instance repo without the sweep (the canonical
        checkout, where release tags are pushed) used to exit 0 before the
        v2.7 gate ran."""
        src = PRE_PUSH.read_text(encoding="utf-8")
        self.assertLess(src.index("push-gate --repo"),
                        src.index('if [ ! -x "$SWEEP" ]; then'))
        Path(self.repo, "config").mkdir()
        Path(self.repo, "config", "agent.conf").write_text(
            "DOGANY_PUSH_GATE_MODE=block\n")
        p = self.push("https://github.com/x/public.git", [self.branch_line()],
                      DOGANY_SWEEP_SCRIPT="/nonexistent",
                      DOGANY_PUSH_GATE_MODE="")
        self.assertEqual(1, p.returncode, p.stderr)
        self.assertIn("PUSH BLOCKED (v2.7 push gate)", p.stderr)

    def test_env_allowlist_and_mode_refused_for_agent(self):
        """R2-8: an agent caller cannot widen the allowlist or loosen the
        mode from env (the suite's own allowlist seam is test-mode only)."""
        Path(self.repo, "config").mkdir()
        Path(self.repo, "config", "agent.conf").write_text(
            "DOGANY_PUSH_GATE_MODE=block\n")
        p = self.push("https://github.com/x/public.git", [self.branch_line()],
                      CLAUDECODE="1", DOGANY_PRIVATE_REMOTE_URLS="*",
                      DOGANY_PUSH_GATE_MODE="detect")
        self.assertEqual(1, p.returncode, p.stderr)
        p = self.push("https://github.com/x/public.git", [self.branch_line()],
                      CLAUDECODE="1",
                      DOGANY_PUSH_APPROVAL_QUOTE="공개 미러에 올려")
        self.assertEqual(1, p.returncode, "the stub resolver is test-mode "
                         "only: " + p.stderr)


D3T = None


def _d3():
    """Load maintained fixture support without importing a retired test."""
    global D3T
    if D3T is None:
        import script_devices_fixtures as fixtures
        D3T = fixtures
    return D3T


RESTART_Q = "Restart now, skipping the idle guard?"


class AgentBinding(unittest.TestCase):
    """R2-6 / R2-7 / R2-8 with the REAL resolver, as the agent calls it
    (CLAUDECODE=1: no stub, no env trust root).  The instance's
    routines/lib is the shipped one (symlinked), so the device reads the
    instance's own data dir: ledger, live record, transcript."""

    def setUp(self):
        d3 = _d3()
        self.s = d3.Session()
        os.makedirs(os.path.join(self.s.root, "routines"))
        os.symlink(str(LIB), os.path.join(self.s.root, "routines", "lib"))
        self.gate = d3.GATE
        self.oqc = os.path.join(self.s.root, "routines", "lib",
                                "owner_quote_check.py")
        self.env = _test_env(CLAUDECODE="1")
        self.env.pop("DOGANY_TEST_MODE")

    def tearDown(self):
        self.s.close()

    def live(self, command, sid=None, transcript=None):
        data = self.s.payload("Bash", {"command": command})
        if sid:
            data["session_id"] = sid
        if transcript:
            data["transcript_path"] = transcript
        self.assertTrue(self.gate.record_live(data, self.s.root))

    def check(self, device, quote, questions=(), objects=(), intent=None,
              **env):
        args = [sys.executable, self.oqc, "--device", device, "--quote", quote]
        for q in questions:
            args += ["--question", q]
        for o in objects:
            args += ["--object", o]
        if intent:
            args += ["--intent", intent]
        p = subprocess.run(args, capture_output=True, text=True,
                           env=dict(self.env, **env))
        return p.returncode, p.stdout.strip()

    def test_unrelated_owner_sentence_not_owner_quote(self):
        """R2-6: 'thanks.' unlocked self_restart's owner-only flags."""
        self.s.add([{"owner": "오늘 날씨 좋다."}, {"say": "그러네요"},
                    {"owner": "고마워."}])
        self.live("bridge/self_restart.sh --reason x --trigger user "
                  "--owner-quote '고마워.'")
        rc, out = self.check("self_restart", "고마워.", [RESTART_Q],
                             intent="service/restart")
        self.assertEqual(1, rc, out)
        self.assertIn("not-bound", out)
        rc, out = self.check("self_restart", "오늘 날씨 좋다.", [RESTART_Q],
                             intent="service/restart")
        self.assertEqual(1, rc, out)  # not in Y, the newest owner turn
        self.assertIn("quote-not-in-yes", out)
        self.s.add([{"owner": "브릿지 재시작해줘"}])
        rc, out = self.check("self_restart", "브릿지 재시작해줘", [RESTART_Q],
                             intent="service/restart")
        self.assertEqual(0, rc, out)
        self.assertEqual("device-object", json.loads(out)["grade"])
        self.s.add([{"say": "지금 바로 재시작할게요.\n" + RESTART_Q},
                    {"owner": "응"}])
        rc, out = self.check("self_restart", "응", [RESTART_Q],
                             intent="service/restart")
        self.assertEqual(0, rc, out)
        self.assertEqual("device-question", json.loads(out)["grade"])

    def test_mid_turn_prompt_is_owner_for_real_quote_resolver(self):
        # promise: a real mid-turn prompt is ledgered and resolved as the newest owner action.
        question = DeviceTapBinding.Q
        self.s.add([{"injected": "[bridge:injected-turn]\nwork"}, {"say": question},
                    {"queued_owner": "yes"}])
        self.live("scripts/release-approval.py --record --tag v2.7.0-dev.4")
        rc, out = self.check("release", "yes", [question])
        self.assertEqual(0, rc, out)
        self.assertEqual(self.s.last, json.loads(out)["message_uuid"])

    def test_voice_thanks_and_unrelated_device_replies_refused(self):
        # promise: voice, thanks, and unrelated typed text still approve nothing.
        for text, source, why in (("yes", "voice", "voice"),
                                  ("thanks.", "message", "not-bound"),
                                  ("nice weather.", "message", "not-bound")):
            with self.subTest(text=text, source=source):
                self.s.add([{"say": DeviceTapBinding.Q}, {"owner": text, "source": source}])
                self.live("scripts/release-approval.py --record --tag v2.7.0-dev.4")
                rc, out = self.check("release", text, [DeviceTapBinding.Q], ["v2.7.0-dev.4"])
                self.assertEqual(1, rc, out)
                self.assertIn(why, out)

    def test_cron_session_borrows_owner_quote(self):
        """R2-7: a session that never had the owner's turn (a claude -p
        cron) cannot resolve the bridge session's words; env cannot name the
        session; two live sessions refuse."""
        self.s.add([{"owner": "브릿지 재시작해줘"}])
        cron_t = os.path.join(self.s.tmp, "cron-1.jsonl")
        Path(cron_t).write_text("")
        self.live("bridge/self_restart.sh --reason x --trigger user",
                  sid="cron-1", transcript=cron_t)
        rc, out = self.check("self_restart", "브릿지 재시작해줘", [RESTART_Q],
                             intent="service/restart",
                             CLAUDE_CODE_SESSION_ID=self.s.sid)
        self.assertEqual(1, rc, out)
        self.live("bridge/self_restart.sh --reason x --trigger user")
        rc, out = self.check("self_restart", "브릿지 재시작해줘", [RESTART_Q],
                             intent="service/restart")
        self.assertEqual(1, rc, out)
        self.assertIn("two-sessions", out)
        live_dir = Path(self.s.data, "state", "bash-gate", "live")
        (live_dir / "cron-1.json").unlink()
        rc, out = self.check("self_restart", "브릿지 재시작해줘", [RESTART_Q],
                             intent="service/restart")
        self.assertEqual(0, rc, out)

    def test_no_live_call_refuses(self):
        self.s.add([{"owner": "브릿지 재시작해줘"}])
        rc, out = self.check("self_restart", "브릿지 재시작해줘", [RESTART_Q],
                             intent="service/restart")
        self.assertEqual(1, rc, out)
        self.assertIn("no-live-call", out)

    def _canon(self):
        repo = os.path.join(self.s.tmp, "canon")
        os.makedirs(os.path.join(repo, "scripts"))
        shutil.copy(RELEASE_APPROVAL, os.path.join(repo, "scripts"))
        _git(repo, "init", "-q", "-b", "main")
        Path(repo, "README").write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        _git(repo, "config", "dogany.ownerInstance", self.s.root)
        return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()

    def ra(self, repo, *args, **env):
        return subprocess.run([sys.executable,
                               os.path.join(repo, "scripts",
                                            "release-approval.py"),
                               "--repo", repo] + list(args),
                              capture_output=True, text=True,
                              env=dict(self.env, **env),
                              stdin=subprocess.DEVNULL)

    def test_unrelated_owner_sentence_not_release_approval(self):
        """R2-6: `--record --quote "thanks."` recorded a release approval."""
        repo, sha = self._canon()
        self.s.add([{"owner": "오늘 날씨 좋다."}, {"say": "네"},
                    {"owner": "고마워."}])
        self.live("python3 scripts/release-approval.py --record --tag v2.7.0")
        p = self.ra(repo, "--record", "--tag", "v2.7.0", "--sha", sha,
                    "--quote", "고마워.")
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)
        q = "Release `v2.7.0` @ `%s`?" % sha[:12]
        self.s.add([{"say": "릴리스 준비 끝났어요.\n" + q}, {"owner": "응"}])
        self.live("python3 scripts/release-approval.py --record --tag v2.7.0")
        p = self.ra(repo, "--record", "--tag", "v2.7.0", "--sha", sha,
                    "--quote", "응")
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        p = self.ra(repo, "--check", "--tag", "v2.7.0", "--sha", sha,
                    "--mode", "block")
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        p = self.ra(repo, "--check", "--tag", "v2.7.1", "--sha", sha,
                    "--mode", "block")
        self.assertEqual(1, p.returncode)

    def test_release_object_named_by_owner(self):
        repo, sha = self._canon()
        self.s.add([{"owner": "v2.7.0 릴리스 진행해"}])
        self.live("scripts/release-approval.py --record")
        p = self.ra(repo, "--record", "--tag", "v2.7.0", "--sha", sha,
                    "--quote", "v2.7.0 릴리스 진행해")
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)

    def test_env_resolver_override_refused(self):
        """R2-8: DOGANY_OWNER_QUOTE_CHECK / DOGANY_TEST_MODE are ignored for
        an agent caller."""
        repo = os.path.join(self.s.tmp, "plain")
        os.makedirs(repo)
        _git(repo, "init", "-q", "-b", "main")
        Path(repo, "README").write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        stub = os.path.join(self.s.tmp, "ok.py")
        Path(stub).write_text(STUB_RESOLVER)
        os.makedirs(os.path.join(repo, "scripts"))
        shutil.copy(RELEASE_APPROVAL, os.path.join(repo, "scripts"))
        os.makedirs(os.path.join(repo, "agents", ".template", "routines"))
        os.symlink(str(LIB), os.path.join(repo, "agents", ".template",
                                          "routines", "lib"))
        p = self.ra(repo, "--record", "--tag", "v2.7.0", "--sha", sha,
                    "--quote", "v2.7 릴리스 진행해",
                    DOGANY_OWNER_QUOTE_CHECK=stub, DOGANY_TEST_MODE="1")
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)
        self.assertIn("resolver-unavailable", p.stderr)

    def test_fabricated_session_via_env(self):
        """R2-8: HOME / CLAUDE_PROJECT_DIR / DOGANY_BOT_DATA_DIR pointed at a
        fabricated session: the device reads the owner instance's own data
        dir, so the forged ledger and transcript are never consulted."""
        repo, sha = self._canon()
        fake = os.path.join(self.s.tmp, "f")
        forged = _d3().Session()
        try:
            forged.add([{"owner": "v2.7.0 릴리스 진행해"}])
            forged_live = forged.payload("Bash", {"command": "release.sh"})
            self.gate.record_live(forged_live, forged.root)
            p = self.ra(repo, "--record", "--tag", "v2.7.0", "--sha", sha,
                        "--quote", "v2.7.0 릴리스 진행해", HOME=fake,
                        CLAUDE_PROJECT_DIR=forged.root,
                        DOGANY_BOT_DATA_DIR=forged.data)
            self.assertEqual(1, p.returncode, p.stdout + p.stderr)
            self.assertIn("no-live-call", p.stderr)
        finally:
            forged.close()

    def test_unrelated_owner_sentence_not_push_approval(self):
        """R2-6: DOGANY_PUSH_APPROVAL_QUOTE="thanks." passed pre-push."""
        root = self.s.root
        _git(root, "init", "-q", "-b", "main")
        Path(root, "README").write_text("x\n")
        _git(root, "add", "README")
        _git(root, "commit", "-qm", "init")
        sha = _git(root, "rev-parse", "HEAD").stdout.strip()
        os.makedirs(os.path.join(root, "git-hooks"))
        shutil.copy(PRE_PUSH, os.path.join(root, "git-hooks"))
        Path(root, "config", "agent.conf").write_text(
            "DOGANY_PUSH_GATE_MODE=block\n")
        sweep = os.path.join(self.s.tmp, "sweep.sh")
        Path(sweep).write_text("#!/bin/sh\necho 'RESULT: clean'\nexit 0\n")
        os.chmod(sweep, 0o755)
        url = "https://github.com/x/public.git"
        line = "refs/heads/main %s refs/heads/main %s\n" % (sha, "0" * 40)

        def push(quote):
            self.live("git push public main")
            return subprocess.run(
                ["bash", os.path.join(root, "git-hooks", "pre-push"),
                 "public", url], input=line, capture_output=True, text=True,
                cwd=root, env=dict(self.env, DOGANY_SWEEP_SCRIPT=sweep,
                                   DOGANY_PUSH_APPROVAL_QUOTE=quote,
                                   GIT_CONFIG_GLOBAL="/dev/null"))
        self.s.add([{"owner": "고마워."}])
        p = push("고마워.")
        self.assertEqual(1, p.returncode, p.stderr)
        self.assertIn("not-bound", p.stderr)
        self.s.add([{"owner": "github.com/x/public 에 올려줘"}])
        p = push("github.com/x/public 에 올려줘")
        self.assertEqual(0, p.returncode, p.stderr)


def _bridge_functions(path, names, namespace):
    """Exercise stdlib bridge seams without importing the Telegram/SDK runtime."""
    import ast
    tree = ast.parse(path.read_text())
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in funcs:
        node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0)] + funcs, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


class DeviceTapBinding(unittest.TestCase):
    # promise: only an explicitly approving slot on the exact question bubble is a device yes.
    Q = "Release `v2.7.0-dev.4` @ `152dbb5d46a4`?"
    LABELS = ["Release", "View list"]
    KB = ["1. Release", "2. View list"]

    def setUp(self):
        import logging
        from unittest.mock import patch
        self.s = _d3().Session()
        sys.path.insert(0, str(TEMPLATE))
        sys.path.insert(0, str(LIB))
        from bridge import formatting, outbound_context
        from why_net import ups, budget
        self.fmt, self.ups, self.budget = formatting, ups, budget
        self.envpatch = patch.dict(os.environ, _test_env(
            DOGANY_ROOT=self.s.root, DOGANY_BOT_DATA_DIR=self.s.data,
            DOGANY_AGENT_CONF=os.path.join(self.s.root, "config", "agent.conf")), clear=True)
        self.envpatch.start()
        Path(self.s.root, "config", "agent.conf").write_text("WHY_NET=off\n")
        # Keep the owner-turn ledger sentinel active without depending on the
        # current template's hook inventory (the old D3 suite that supplied
        # this fixture has been retired).
        Path(self.s.root, ".claude").mkdir()
        Path(self.s.root, ".claude", "settings.json").write_text(json.dumps({
            "hooks": {"PreToolUse": [{"hooks": [{
                "command": "BASH_GATE_MODE=detect routines/bash-safety-gate.py"
            }]}]}
        }))
        self.bridge = _bridge_functions(TEMPLATE / "bridge" / "sdk_bridge.py", {
            "options_bubbles_path", "bubble_text_sha", "_read_options_bubbles",
            "_write_options_bubbles", "publish_options_bubble"}, {
                "PROJECT_ROOT": Path(self.s.root), "Path": Path, "json": json,
                "time": time, "outbound_context": outbound_context,
                "OPTIONS_BUBBLES_FILENAME": "options-bubbles.json",
                "OPTIONS_BUBBLES_MAX": 50, "logger": logging.getLogger(__name__)})
        self.content = self.Q + "\n[[OPTIONS: [approve] Release | View list]]"
        self.body = self.Q.replace("`", "")

    def tearDown(self):
        self.envpatch.stop()
        self.s.close()
        sys.path.remove(str(LIB))
        sys.path.remove(str(TEMPLATE))

    def publish(self, mid=70, content=None, body=None, labels=None):
        approval = self.fmt.device_approval_menu(
            self.content if content is None else content,
            self.body if body is None else body, labels or self.LABELS, self.KB)
        self.bridge["publish_options_bubble"](42, mid, self.KB, device_approval=approval)

    def tap(self, idx=1, wrap=False, **changes):
        text = self.KB[idx - 1]
        said = text
        if wrap:
            # The bridge prefixes a reply-to-push tap with its outbound-context
            # block (DGN-1951); the owner's words are only the label after it.
            said = ("[bridge:outbound-context]\nThe user is replying to this message you sent "
                    "outside this conversation (full text):\n" + self.body +
                    "\n[/bridge:outbound-context]\n" + text)
        inbound = {"schema": 1, "request_id": str(time.time_ns()), "ts": time.time(),
                   "source": "callback", "chat_id": 42, "message_id": 70,
                   "keyboard": self.KB, "option_index": idx,
                   "bubble_text_sha": self.bridge["bubble_text_sha"](self.body),
                   "callback_data": "opt:" + text,
                   "text_sha": self.s.uy._wot().turn_sha(said)}
        inbound.update(changes)
        Path(self.s.data, "inbound-context.json").write_text(json.dumps(inbound))
        self.ups.run(self.budget.Ctx(root=self.s.root), {
            "session_id": self.s.sid, "prompt": said,
            "transcript_path": self.s.transcript})
        self.s.add([{"owner": said, "unledgered": True}])
        return self.s.uy.device_yes(self.s.transcript, self.s.sid, self.s.ledger,
                                   [self.Q], ["v2.7.0-dev.4"], quote=text)

    def test_approving_tap_resolves_and_audits_after_menu_pruned(self):
        self.publish()
        verdict = self.tap()
        self.assertEqual((True, "device-question-tap"), (verdict.ok, verdict.grade))
        Path(self.s.data, "options-bubbles.json").unlink()
        self.s.add([{"owner": "thanks."}])
        audit = self.s.uy.device_audit(self.s.transcript, self.s.sid, self.s.ledger,
                                     verdict.turn_uuid, [self.Q], quote=self.KB[0])
        self.assertTrue(audit.ok, audit.reason)
        for q in (self.Q.replace("dev.4", "dev.5"), self.Q.replace("152db", "252db")):
            self.assertFalse(self.s.uy.device_audit(
                self.s.transcript, self.s.sid, self.s.ledger, verdict.turn_uuid,
                [q], ["v2.7.0-dev.4"], quote=self.KB[0]).ok)

    def test_tap_wrapped_in_outbound_context_still_binds(self):
        # Skull 2026-11-09: every approval tap on a pushed menu came wrapped and
        # was refused, blocking kit_life 2.46.2 publish.
        self.publish()
        wrapped = ("[bridge:outbound-context]\npushed copy\n"
                   "[/bridge:outbound-context]\n" + self.KB[0])
        turn = self.s.uy.Turn("wrapped", time.time(), wrapped, wrapped, True)
        row = {"source": "callback", "keyboard": self.KB,
               "option_index": 1, "device_question": self.Q}
        direct = self.s.uy._device_binding(
            turn, "", row, [self.Q], [], None, self.s.uy.vocab())
        self.assertEqual((True, "device-question-tap"),
                         (direct.ok, direct.grade))
        verdict = self.tap(wrap=True)
        self.assertEqual((True, "device-question-tap"), (verdict.ok, verdict.grade))

    def test_real_device_gate_uses_verified_tap(self):
        self.publish()
        self.assertTrue(self.tap().ok)
        d3 = _d3()
        self.assertTrue(d3.GATE.record_live(self.s.payload(
            "Bash", {"command": "scripts/release-approval.py --record"}), self.s.root))
        gate = d3._load("real_device_gate_tap", LIB / "device_gate.py")
        ok, got = gate.resolve("release", self.KB[0], [self.Q],
                               ["v2.7.0-dev.4"], root=self.s.root, env={})
        self.assertTrue(ok, got)
        self.assertEqual("device-question-tap", got["grade"])

    def test_release_record_accepts_only_verified_tap(self):
        # Exercise the real release CLI, resolver and durable approval audit.
        repo = Path(self.s.tmp, "release-repo")
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(RELEASE_APPROVAL, repo / "scripts")
        shutil.copy(RELEASE_SCRIPTS / "release-quote-check.py", repo / "scripts")
        _git(str(repo), "init", "-q", "-b", "release-fixture")
        _git(str(repo), "add", "scripts/release-approval.py")
        _git(str(repo), "commit", "-qm", "release fixture")
        sha = _git(str(repo), "rev-parse", "HEAD").stdout.strip()
        _git(str(repo), "config", "dogany.ownerInstance", self.s.root)
        Path(self.s.root, "routines").mkdir()
        os.symlink(str(LIB), Path(self.s.root, "routines", "lib"))
        tag = "v2.7.0-dev.4"
        self.Q = "Release `%s` @ `%s`?" % (tag, sha[:12])
        self.content = self.Q + "\n[[OPTIONS: [approve] Release | View list]]"
        self.body = self.Q.replace("`", "")
        env = _test_env(CLAUDECODE="1")
        env.pop("DOGANY_TEST_MODE")
        command = "scripts/release-approval.py --record"

        def cli(*args):
            return subprocess.run(
                [sys.executable, str(repo / "scripts" / "release-approval.py"),
                 "--repo", str(repo)] + list(args),
                capture_output=True, text=True, env=env,
                stdin=subprocess.DEVNULL)

        def record():
            self.assertTrue(_d3().GATE.record_live(
                self.s.payload("Bash", {"command": command}), self.s.root))
            return cli("--record", "--tag", tag, "--sha", sha,
                       "--quote", self.KB[0])

        store = repo / ".git" / "dogany-release-approvals.jsonl"
        for ordinary, voice in ((True, False), (False, True)):
            with self.subTest(ordinary=ordinary, voice=voice):
                self.publish(content=self.content.replace("[approve] ", "")
                             if ordinary else self.content)
                self.assertFalse(self.tap(voice=voice).ok)
                result = record()
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertFalse(store.exists())

        self.publish()
        self.assertTrue(self.tap().ok)
        result = record()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        row = json.loads(store.read_text())
        self.assertEqual((tag, sha, "device-question-tap"),
                         (row["tag"], row["sha"], row["grade"]))
        Path(self.s.data, "options-bubbles.json").unlink()
        self.s.add([{"owner": "thanks."}])
        result = cli("--check", "--tag", tag, "--sha", sha, "--mode", "block")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for changed_tag, changed_sha in ((tag + "1", sha), (tag, "0" * 40)):
            result = cli("--check", "--tag", changed_tag, "--sha", changed_sha,
                         "--mode", "block")
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)

    def test_other_slot_and_older_bubble_refused(self):
        self.publish()
        self.assertEqual("tap", self.tap(idx=2).reason)
        self.publish(mid=71)
        self.assertEqual("tap", self.tap().reason)

    def test_unmarked_or_nonquestion_menu_refused(self):
        cases = [(self.content.replace("[approve] ", ""), self.body),
                 (self.content, "Choose an action"),
                 (self.content.replace(self.Q, self.Q + "\nOne more thing"), self.body),
                 (self.content.replace("View list", "[approve] View list"), self.body)]
        for content, body in cases:
            with self.subTest(content=content, body=body):
                self.publish(content=content, body=body)
                self.assertEqual("tap", self.tap().reason)

    def test_unedited_matching_keyboard_required(self):
        self.publish()
        for changes in ({"bubble_text_sha": "edited"}, {"keyboard": ["yes", "no"]},
                        {"option_index": True}, {"message_id": 71}):
            with self.subTest(changes=changes):
                self.assertEqual("tap", self.tap(**changes).reason)

    def test_current_owner_action_and_fresh_ledger_required(self):
        self.publish()
        v = self.tap()
        self.assertTrue(v.ok, v.reason)
        for now in (time.time() + 3600, time.time() - 3600):
            self.assertEqual("yes-expired", self.s.uy.device_yes(
                self.s.transcript, self.s.sid, self.s.ledger, [self.Q], now=now).reason)
        self.s.add([{"owner": "thanks."}])
        self.assertEqual("not-bound", self.s.uy.device_yes(
            self.s.transcript, self.s.sid, self.s.ledger, [self.Q], quote="thanks.").reason)
        Path(self.s.ledger).unlink()
        self.assertEqual("not-in-ledger", self.s.uy.device_yes(
            self.s.transcript, self.s.sid, self.s.ledger, [self.Q]).reason)

    def test_carrier_result_must_end_with_question(self):
        import asyncio
        from types import SimpleNamespace
        ns = _bridge_functions(TEMPLATE / "bridge" / "bot.py", {"_attach_keyboard"}, {})
        async def edit(**kwargs):
            return SimpleNamespace(text="Only the last split chunk\n" + self.body)
        fake = SimpleNamespace(_artifact_chat_id=lambda target: 42,
                               application=SimpleNamespace(bot=SimpleNamespace(
                                   edit_message_reply_markup=edit)))
        approval = self.fmt.device_approval_menu(self.content, self.body, self.LABELS, self.KB)
        self.assertTrue(asyncio.run(ns["_attach_keyboard"](fake, 42, 70, None, approval)))
        self.assertEqual("Only the last split chunk\n" + self.body, approval["body"])
        self.body = "Different question?"
        asyncio.run(ns["_attach_keyboard"](fake, 42, 70, None, approval))
        self.assertEqual({}, approval)

    def test_approve_flag_is_hidden_and_ordinary_labels_unchanged(self):
        self.assertIsNone(self.fmt.device_approval_menu(
            self.content, self.body, self.LABELS, [self.KB[0]]))
        self.assertEqual(self.LABELS, self.fmt.parse_options_marker_labels(
            "[[OPTIONS: [approve] Release | View list]]"))
        self.assertEqual(self.LABELS, self.fmt.parse_options_marker_labels(
            "[[OPTIONS: Release | View list]]"))


class DeviceGateUnits(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "device_gate_units", str(LIB / "device_gate.py"))
        self.dg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dg)
        self.tmp = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmp, "agent.conf")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_internal_dev_artifact_needs_no_per_cut_owner_record(self):
        self.dg.private_patterns = lambda *args: ["https://example.invalid/private/*"]
        self.assertEqual([], self.dg.push_gate(self.tmp, "https://example.invalid/private/repo",
                         [("refs/tags/v9.0.0-dev.1", "a" * 40)], {}))

    def test_public_dev_artifact_keeps_owner_release_gate(self):
        self.dg.private_patterns = lambda *args: ["https://example.invalid/private/*"]
        self.dg._git = lambda *args: "a" * 40
        self.dg.resolver_for = lambda *args: None
        problems = self.dg.push_gate(self.tmp, "https://example.invalid/public/repo",
                                    [("refs/tags/v9.0.0-dev.1", "a" * 40)], {})
        self.assertTrue(any("no owner release approval" in problem for problem in problems))

    def test_stable_keeps_per_cut_owner_record(self):
        self.dg.private_patterns = lambda *args: ["https://example.invalid/private/*"]
        self.dg._git = lambda *args: "a" * 40
        problems = self.dg.push_gate(self.tmp, "https://example.invalid/private/repo",
                                    [("refs/tags/v9.0.0", "a" * 40)], {})
        self.assertTrue(any("no owner release approval" in problem for problem in problems))

    def test_stable_tag_gate_blocks_in_detect_mode(self):
        import contextlib
        import io
        from unittest.mock import patch
        self.dg.private_patterns = lambda *args: ["https://example.invalid/private/*"]
        self.dg._git = lambda *args: "a" * 40
        # Invoke the actual tag-gate CLI boundary; no push or tag is made.
        with patch.object(sys, "stdin", io.StringIO("refs/tags/v9.0.0 " + "a" * 40)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, self.dg.main(['push-gate', '--repo', self.tmp,
                              '--url', 'https://example.invalid/private/repo']))

    def test_release_cli_defaults_block_without_instance(self):
        env = _test_env(RELEASE_APPROVAL_MODE='detect')
        p = subprocess.run([sys.executable, str(RELEASE_APPROVAL), '--check',
                            '--repo', self.tmp, '--tag', 'v9.0.0', '--sha', 'a' * 40],
                           env=env, capture_output=True, text=True, timeout=1)
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)
        self.assertIn('not approved by the owner', p.stderr)

    def test_explicit_detect_cannot_weaken_release_cli(self):
        p = subprocess.run([sys.executable, str(RELEASE_APPROVAL), '--check',
                            '--repo', self.tmp, '--tag', 'v9.0.0', '--sha', 'a' * 40,
                            '--mode', 'detect'], env=_test_env(),
                           capture_output=True, text=True, timeout=1)
        self.assertEqual(1, p.returncode, p.stdout + p.stderr)

    def test_env_mode_cannot_loosen(self):
        m = self.dg.device_mode
        k = "RELEASE_APPROVAL_MODE"
        self.assertEqual("block", m(k, self.conf, {}))
        Path(self.conf).write_text("RELEASE_APPROVAL_MODE=block\n")
        for e in ("detect", "DETECT", "", "off"):
            self.assertEqual("block", m(k, self.conf, {k: e}), e)
        Path(self.conf).write_text("RELEASE_APPROVAL_MODE=detect\n")
        self.assertEqual("block", m(k, self.conf, {k: "detect"}))
        for e in ("block", "Block", "garbage"):
            self.assertEqual("block", m(k, self.conf, {k: e}), e)
        Path(self.conf).write_text("RELEASE_APPROVAL_MODE=sometimes\n")
        self.assertEqual("block", m(k, self.conf, {}))

    def test_agent_caller_ancestry(self):
        """Q-R6: CLAUDECODE or a claude CLI ancestor; test mode skips the
        probe only without CLAUDECODE."""
        chain = {100: (50, "bash", "bash -c x"),
                 50: (10, "claude", "claude --resume"),
                 10: (1, "launchd", "/sbin/launchd")}
        self.dg._ps = lambda pid: chain.get(pid)
        self.assertTrue(self.dg.claude_ancestor(100))
        chain[50] = (10, "node", "node /x/@anthropic-ai/claude-code/cli.js")
        self.assertTrue(self.dg.claude_ancestor(100))
        chain[50] = (10, "python3", "python3 bridge/bot.py")
        self.assertFalse(self.dg.claude_ancestor(100))
        self.dg.claude_ancestor = lambda pid=None: True
        self.assertTrue(self.dg.agent_caller({}))
        self.assertFalse(self.dg.agent_caller({"DOGANY_TEST_MODE": "1"}))
        self.assertTrue(self.dg.agent_caller({"DOGANY_TEST_MODE": "1",
                                              "CLAUDECODE": "1"}))

    def test_test_seams_need_test_mode(self):
        self.assertIsNone(self.dg.resolver_for(self.tmp, {
            "DOGANY_OWNER_QUOTE_CHECK": "/x/ok.py", "CLAUDECODE": "1",
            "DOGANY_TEST_MODE": "1"}))
        self.assertEqual("/x/ok.py", self.dg.resolver_for(self.tmp, {
            "DOGANY_OWNER_QUOTE_CHECK": "/x/ok.py", "DOGANY_TEST_MODE": "1"}))

    def test_terminal_confirm_types_the_tag(self):
        """Q-R8: a human at a terminal confirms by typing the tag."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "release_approval_units", str(RELEASE_APPROVAL))
        ra = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ra)

        class TTY(object):
            def __init__(self, line):
                self.line = line

            def isatty(self):
                return True

            def readline(self):
                return self.line
        saved = sys.stdin
        try:
            self.dg.agent_caller = lambda env=None: False
            sys.stdin = TTY("v2.7.0\n")
            self.assertTrue(ra.terminal_confirm("v2.7.0", self.dg))
            sys.stdin = TTY("v2.7.1\n")
            self.assertFalse(ra.terminal_confirm("v2.7.0", self.dg))
            self.dg.agent_caller = lambda env=None: True
            sys.stdin = TTY("v2.7.0\n")
            self.assertFalse(ra.terminal_confirm("v2.7.0", self.dg))
        finally:
            sys.stdin = saved


class WrappedRelease(unittest.TestCase):
    """DGN-2251: wrapped owner taps record exact releases without typed repeats."""

    TAG = "v2.7.0-dev.9"
    SHA = "e3f76182a2b0edeb545071cbbc5eee1b3dd08d52"

    def setUp(self):
        self.h = DeviceTapBinding()
        self.h.setUp()
        self.addCleanup(self.h.tearDown)
        self.s = self.h.s
        self.repo = Path(self.s.tmp, "release-repo")
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(RELEASE_APPROVAL, self.repo / "scripts")
        shutil.copy(RELEASE_SCRIPTS / "release-quote-check.py", self.repo / "scripts")
        _git(str(self.repo), "init", "-q", "-b", "release-fixture")
        _git(str(self.repo), "add", "scripts/release-approval.py")
        _git(str(self.repo), "commit", "-qm", "fixture")
        _git(str(self.repo), "config", "dogany.ownerInstance", self.s.root)
        Path(self.s.root, "routines").mkdir()
        os.symlink(str(LIB), Path(self.s.root, "routines", "lib"))
        self.store = self.repo / ".git" / "dogany-release-approvals.jsonl"
        self.h.Q = "Release `%s` @ `%s`?" % (self.TAG, self.SHA[:12])
        self.menu("출시 승인")

    def menu(self, label):
        self.h.LABELS = [label, "변경 목록 보기"]
        self.h.KB = ["1. " + label, "2. 변경 목록 보기"]
        self.h.content = ("아래 버튼을 누르시면 이 출시를 승인하신 것으로 기록합니다.\n\n"
                          + self.h.Q + "\n[[OPTIONS: [approve] " + label
                          + " | 변경 목록 보기]]")
        self.h.body = self.h.content.split("\n[[OPTIONS:")[0].replace("`", "")

    def wrapped(self, text):
        return ("[bridge:outbound-context]\n"
                "The user is replying to this message you sent outside this "
                "conversation (full text):\n" + self.h.body + "\n"
                "The user is answering: " + self.h.Q.replace("`", "") + "\n"
                "[/bridge:outbound-context]\n" + text)

    def replay(self, text=None, **changes):
        text = self.wrapped(self.h.KB[0]) if text is None else text
        inbound = {"schema": 1, "request_id": str(time.time_ns()), "ts": time.time(),
                   "source": "callback", "chat_id": 42, "message_id": 70,
                   "keyboard": self.h.KB, "option_index": 1,
                   "bubble_text_sha": self.h.bridge["bubble_text_sha"](self.h.body),
                   "callback_data": "opt:" + self.h.KB[0],
                   "text_sha": self.s.uy._wot().turn_sha(text)}
        inbound.update(changes)
        Path(self.s.data, "inbound-context.json").write_text(json.dumps(inbound))
        self.h.ups.run(self.h.budget.Ctx(root=self.s.root), {
            "session_id": self.s.sid, "prompt": text,
            "transcript_path": self.s.transcript})
        self.s.add([{"owner": text, "unledgered": True}])
        return self.s.uy.device_yes(self.s.transcript, self.s.sid, self.s.ledger,
                                   [self.h.Q], [self.TAG], quote=self.h.KB[0])

    def cli(self, operation, tag=None, sha=None, quote=None):
        self.assertTrue(_d3().GATE.record_live(self.s.payload(
            "Bash", {"command": "python3 scripts/release-approval.py " + operation}),
            self.s.root))
        env = _test_env(CLAUDECODE="1")
        env.pop("DOGANY_TEST_MODE")
        args = [sys.executable, str(self.repo / "scripts/release-approval.py"),
                operation, "--repo", str(self.repo), "--tag", tag or self.TAG,
                "--sha", sha or self.SHA]
        if quote is not None:
            args += ["--quote", quote]
        return subprocess.run(args, capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL)

    def assert_recorded(self, quote, grade):
        result = self.cli("--record", quote=quote)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        row = json.loads(self.store.read_text())
        self.assertEqual((self.TAG, self.SHA, grade, self.s.last),
                         (row["tag"], row["sha"], row["grade"], row["message_uuid"]))
        return row

    def test_real_wrapped_tap_records_and_later_audits_exact_release(self):
        self.h.publish()
        verdict = self.replay()
        ledger = json.loads(Path(self.s.ledger).read_text().splitlines()[-1])
        self.assertEqual(("callback", self.h.KB, 1, self.h.Q),
                         (ledger["source"], ledger["keyboard"],
                          ledger["option_index"], ledger["device_question"]))
        self.assertNotEqual(ledger["sha"], ledger["owner_sha"])
        self.assert_recorded(self.h.KB[0], "device-question-tap")
        self.assertTrue(verdict.ok, verdict.reason)
        Path(self.s.data, "options-bubbles.json").unlink()
        self.s.add([{"owner": "thanks."}])
        result = self.cli("--check")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for tag, sha in ((self.TAG + "0", self.SHA), (self.TAG, "0" * 40)):
            self.assertEqual(1, self.cli("--check", tag=tag, sha=sha).returncode)

    def test_recommended_label_records_with_verified_release_question(self):
        self.menu("버튼 포함 출시 (추천)")
        self.h.publish()
        verdict = self.replay()
        self.assert_recorded(self.h.KB[0], "device-question-tap")
        self.assertTrue(verdict.ok, verdict.reason)

    def test_earlier_menu_without_release_question_remains_unbound(self):
        # The actual 03:11 menu chose release contents; it had no exact
        # tag/SHA question and its ledger has no device_question. Fix its
        # owner classification without inventing missing approval evidence.
        self.menu("버튼 포함 출시 (추천)")
        self.h.body = "dev.9에 무엇을 넣을지\n누르시는 즉시 출시를 시작합니다."
        self.h.publish(content="어떤 내용을 넣을까요?\n"
                       "[[OPTIONS: 버튼 포함 출시 (추천) | 변경 목록 보기]]")
        verdict = self.replay()
        self.assertEqual("tap", verdict.reason)
        result = self.cli("--record", quote=self.h.KB[0])
        self.assertEqual(1, result.returncode)
        self.assertIn("refused tap", result.stderr)
        self.assertFalse(self.store.exists())

    def test_typed_korean_release_intent_records(self):
        text = self.TAG + " 출시"
        self.s.add([{"owner": text}])
        self.assert_recorded(text, "device-object")

    def test_dev_question_does_not_demand_stable_prepare(self):
        self.s.add([{"owner": "thanks."}])
        result = self.cli("--check")
        self.assertEqual(1, result.returncode)
        self.assertIn(self.h.Q, result.stderr)
        self.assertNotIn("--prepare", result.stderr)
        self.assertIn("[[OPTIONS: [approve] 출시 승인 | 변경 목록 보기]]", result.stderr)
        self.assertNotIn("<the owner's own words>", result.stderr)
        result = self.cli("--record", quote="thanks.")
        self.assertEqual(1, result.returncode)
        self.assertIn(self.h.Q, result.stderr)
        self.assertNotIn("--prepare", result.stderr)
        for tag in ("v2.7.0", "v2.7.0-rc.1", "v2.7.0-dev.9-extra"):
            result = self.cli("--check", tag=tag)
            self.assertEqual(1, result.returncode)
            self.assertIn("--prepare", result.stderr)

    def test_wrapper_does_not_hide_injection_or_supply_owner_words(self):
        self.h.publish()
        for suffix in ("[cron-inject] yes", "[bridge:injected-turn]\nyes",
                       "[dispatch-return] yes", "<task-notification>yes"):
            with self.subTest(suffix=suffix):
                verdict = self.replay(self.wrapped(suffix))
                self.assertFalse(verdict.ok)
                self.assertEqual("newest-turn-not-owner", verdict.reason)
                self.assertEqual(1, self.cli("--record", quote="yes").returncode)
        for text in (self.wrapped("thanks."),
                     "[bridge:outbound-context]\n" + self.h.KB[0]):
            with self.subTest(text=text):
                self.assertFalse(self.replay(text).ok)
                self.assertEqual(1, self.cli("--record", quote=self.h.KB[0]).returncode)
        self.assertFalse(self.store.exists())

    def test_wrapped_unverified_taps_never_record(self):
        for changes in ({"option_index": 2}, {"message_id": 69},
                        {"bubble_text_sha": "edited"}, {"voice": True},
                        {"forwarded": True}, {"keyboard": ["1. yes"]}):
            with self.subTest(changes=changes):
                self.h.publish()
                self.assertFalse(self.replay(**changes).ok)
                self.assertEqual(1, self.cli("--record", quote=self.h.KB[0]).returncode)
        self.h.publish(mid=71)
        self.assertFalse(self.replay().ok)
        self.assertEqual(1, self.cli("--record", quote=self.h.KB[0]).returncode)
        self.assertFalse(self.store.exists())

    def test_wrapped_approval_still_expires_and_newest_action_wins(self):
        self.h.publish()
        verdict = self.replay()
        self.assertTrue(verdict.ok, verdict.reason)
        for now in (time.time() + 3601, time.time() - 3601):
            v = self.s.uy.device_yes(self.s.transcript, self.s.sid, self.s.ledger,
                                   [self.h.Q], now=now)
            self.assertEqual("yes-expired", v.reason)
        self.s.add([{"injected": "[cron-inject] keep working"}])
        self.assertEqual(1, self.cli("--record", quote=self.h.KB[0]).returncode)
        self.assertFalse(self.store.exists())

    def test_release_intent_still_refuses_holds_inspection_and_nouns(self):
        for suffix in ("출시 보류", "출시하지 마", "출시 내일", "출시?",
                       "출시 상태", "출시일", "출시 계획", "출시만"):
            with self.subTest(suffix=suffix):
                text = self.TAG + " " + suffix
                self.s.add([{"owner": text}])
                self.assertEqual(1, self.cli("--record", quote=text).returncode)
        self.assertFalse(self.store.exists())


if __name__ == "__main__":
    unittest.main()
