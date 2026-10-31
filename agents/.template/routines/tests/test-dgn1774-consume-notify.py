#!/usr/bin/env python3
# test-lane: fast
"""DGN-1774 -- `handoff-engine.sh consume` must deliver, never silently leave.

Live defect (2026-09-27..28): one instance's handoff-watch plist was
refreshed from the template, which runs `handoff-engine.sh consume`. It has no
config/handoff.capabilities, so every decision.notice was counted "left"
and nothing reached the session. These tests configure an instance the
same way -- the template plist's ProgramArguments, no capabilities file
-- and lock:

  1  a decision.notice dropped in the slug inbox is injected into the
     bridge session-inbox (one *.md turn naming the inbox file), stays in
     the inbox (inbox = un-triaged), and .notify.idx records it
  2  a second run does NOT inject it again (notify-once)
  3  after the session moves it to done/, the idx entry is pruned
  4  session-inbox missing -> rc 1, a loud handoff.log line, NOT recorded
     in the idx; the next run with the session-inbox present injects it
  5  a pre-existing .notify.idx written by the instance-local
     handoff-consume.sh (same path format) is honoured -- no re-inject
     when an instance moves from that notifier to the engine path
  6  a present-but-malformed capabilities file still stops consumption
     (rc != 0, message not moved) but the message is still injected
  7  a message a builtin handler consumed (moved to done/) is not injected
  8  legacy <root>/files/handoff/inbox leftovers are injected too

DOGANY_HANDOFF_ROOT points at a per-test tempdir -- never the real
~/.dogany/handoff. The session-inbox is the fixture root's own
.telegram_bot/session-inbox. No launchd contact.

Run: python3 routines/tests/test-dgn1774-consume-notify.py
     (or: pytest routines/tests/test-dgn1774-consume-notify.py)
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)
LIB = os.path.join(ROUTINES, "lib", "handoff")
PLIST_TEMPLATE = os.path.join(
    ROUTINES, "com.telegram-skill-bot.telegram-agent.handoff-watch.plist")

sys.path.insert(0, LIB)

import handoff                                                   # noqa: E402

SLUG = "skull"


def _mk_instance(tmp, capabilities_text=None, session_inbox=True):
    root = os.path.join(tmp, "skull")
    shutil.copytree(ROUTINES, os.path.join(root, "routines"),
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))
    os.makedirs(os.path.join(root, "config"))
    with open(os.path.join(root, "config", "agent.conf"), "w") as fh:
        fh.write("SLUG=%s\n" % SLUG)
    if capabilities_text is not None:
        with open(os.path.join(root, "config", "handoff.capabilities"),
                  "w") as fh:
            fh.write(capabilities_text)
    if session_inbox:
        os.makedirs(os.path.join(root, ".telegram_bot", "session-inbox"))
    base = os.path.join(tmp, "mailboxes")
    for d in ("inbox", "done", "dead", "tmp"):
        os.makedirs(os.path.join(base, SLUG, d))
    return root, base


def _watch_argv(root):
    """ProgramArguments of the template watcher plist, rendered for this
    fixture root -- i.e. exactly what launchd runs for such an instance."""
    with open(PLIST_TEMPLATE, "rb") as fh:
        pl = plistlib.load(fh)
    return [a.replace("__PROJECT_ROOT__", root)
            for a in pl["ProgramArguments"]]


def _run_watch(root, base):
    env = dict(os.environ)
    env["DOGANY_HANDOFF_ROOT"] = base
    env.pop("HANDOFF_SESSION_INBOX", None)
    return subprocess.run(_watch_argv(root), capture_output=True, text=True,
                          env=env, cwd=root)


def _submit(base, msg_type="decision.notice", subject="re-run now"):
    prev = os.environ.get("DOGANY_HANDOFF_ROOT")
    os.environ["DOGANY_HANDOFF_ROOT"] = base
    try:
        return handoff.submit_to_slug(
            SLUG, {"from": "metal", "type": msg_type,
                   "payload": {"subject": subject}}, body="please re-run")
    finally:
        if prev is None:
            os.environ.pop("DOGANY_HANDOFF_ROOT", None)
        else:
            os.environ["DOGANY_HANDOFF_ROOT"] = prev


def _session_files(root):
    d = os.path.join(root, ".telegram_bot", "session-inbox")
    return sorted(n for n in os.listdir(d) if n.endswith(".md"))


def _idx(base):
    p = os.path.join(base, SLUG, ".notify.idx")
    if not os.path.isfile(p):
        return []
    with open(p) as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def _handoff_log(root):
    p = os.path.join(root, ".telegram_bot", "logs", "handoff.log")
    if not os.path.isfile(p):
        return ""
    with open(p) as fh:
        return fh.read()


def test_0_fixture_is_configured_the_skull_way():
    with tempfile.TemporaryDirectory() as tmp:
        root, _base = _mk_instance(tmp)
        argv = _watch_argv(root)
        assert argv[-2:] == [
            os.path.join(root, "routines", "handoff-engine.sh"), "consume"], argv
        assert not os.path.exists(
            os.path.join(root, "config", "handoff.capabilities"))


def test_1_decision_notice_is_injected_not_silently_left():
    with tempfile.TemporaryDirectory() as tmp:
        root, base = _mk_instance(tmp)
        path = _submit(base)
        out = _run_watch(root, base)
        assert out.returncode == 0, out.stderr
        stats = json.loads(out.stdout.strip())
        assert stats["left"] == 1 and stats["errors"] == 0, stats
        files = _session_files(root)
        assert len(files) == 1, files
        with open(os.path.join(root, ".telegram_bot", "session-inbox",
                               files[0])) as fh:
            text = fh.read()
        assert text.startswith("[cron-inject] handoff arrived -- from metal,"
                               " type decision.notice, subject: re-run now"), text
        assert path in text, text
        assert os.path.isfile(path), "inbox file must stay (inbox = un-triaged)"
        assert _idx(base) == [path]
        assert "notify (inject)" in _handoff_log(root)
        assert '"notified": 1' in out.stderr, out.stderr


def test_2_second_run_does_not_reinject():
    with tempfile.TemporaryDirectory() as tmp:
        root, base = _mk_instance(tmp)
        _submit(base)
        assert _run_watch(root, base).returncode == 0
        out = _run_watch(root, base)
        assert out.returncode == 0, out.stderr
        assert len(_session_files(root)) == 1


def test_3_idx_pruned_after_triage_move():
    with tempfile.TemporaryDirectory() as tmp:
        root, base = _mk_instance(tmp)
        path = _submit(base)
        assert _run_watch(root, base).returncode == 0
        shutil.move(path, os.path.join(base, SLUG, "done"))
        assert _run_watch(root, base).returncode == 0
        assert _idx(base) == []


def test_4_inject_failure_is_loud_and_retried():
    with tempfile.TemporaryDirectory() as tmp:
        root, base = _mk_instance(tmp, session_inbox=False)
        path = _submit(base)
        out = _run_watch(root, base)
        assert out.returncode == 1, (out.returncode, out.stderr)
        assert "inject FAILED" in out.stderr, out.stderr
        assert "inject FAILED" in _handoff_log(root)
        assert _idx(base) == [], "a failed inject must not be marked notified"
        os.makedirs(os.path.join(root, ".telegram_bot", "session-inbox"))
        out = _run_watch(root, base)
        assert out.returncode == 0, out.stderr
        assert len(_session_files(root)) == 1
        assert _idx(base) == [path]


def test_5_existing_notifier_idx_is_honoured():
    with tempfile.TemporaryDirectory() as tmp:
        root, base = _mk_instance(tmp)
        path = _submit(base)
        with open(os.path.join(base, SLUG, ".notify.idx"), "w") as fh:
            fh.write(path + "\n")
        out = _run_watch(root, base)
        assert out.returncode == 0, out.stderr
        assert _session_files(root) == []
        assert _idx(base) == [path]


def test_6_broken_capabilities_still_notifies():
    with tempfile.TemporaryDirectory() as tmp:
        root, base = _mk_instance(tmp,
                                  capabilities_text="bogus.type nope\n")
        path = _submit(base)
        out = _run_watch(root, base)
        assert out.returncode != 0, out.stdout
        assert "capabilities file invalid" in out.stderr, out.stderr
        assert os.path.isfile(path)
        assert len(_session_files(root)) == 1


def test_7_builtin_consumed_message_is_not_injected():
    with tempfile.TemporaryDirectory() as tmp:
        root, base = _mk_instance(
            tmp, capabilities_text=(
                "decision.notice builtin:notice-append idem=ulid\n"))
        _submit(base)
        out = _run_watch(root, base)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout.strip())["processed"] == 1
        assert _session_files(root) == []


def test_8_legacy_inbox_leftover_is_injected():
    with tempfile.TemporaryDirectory() as tmp:
        root, base = _mk_instance(tmp)
        legacy = handoff.inbox_dir(root)
        os.makedirs(legacy)
        lp = os.path.join(legacy, "20260927-decision.notice-LEGACY01.md")
        with open(lp, "w") as fh:
            fh.write(handoff.dump_message(
                {"id": "01LEGACY0000000000000000", "from": "ag",
                 "type": "decision.notice"}, "legacy body"))
        out = _run_watch(root, base)
        assert out.returncode == 0, out.stderr
        files = _session_files(root)
        assert len(files) == 1, files
        with open(os.path.join(root, ".telegram_bot", "session-inbox",
                               files[0])) as fh:
            text = fh.read()
        assert lp in text and handoff.archive_dir(root) in text, text


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS %s" % name)
            except AssertionError as e:
                failed += 1
                print("FAIL %s: %s" % (name, e))
    sys.exit(1 if failed else 0)
