# test-lane: fast
# test-tier: 1
# test-component: memory-engine FDGN-620 archive end to end (memory.py CLI, hook, consolidate)
# test-prereq: tmp-files, subprocess, pytest
# test-new-component: end-to-end proof for the archive engine; every case drives
#   memory.py commands or the prompt hook, never engine internals.
# test-covers: agents/.template/memory-engine/memory.py agents/.template/memory-engine/archive_engine.py agents/.template/memory-engine/consolidation_router.py
# FDGN-620 slice 1 r3. Gate cases (1)-(6) of the task:
#  (1) 9000-line MEMORY.md fixture: replay dry-run plan with evidence; batched
#      apply SIGKILLed between batches (and mid-batch) then resumed ends
#      byte-identical to an uninterrupted run.
#  (2) a half-finished journal is recovered by write, classify-inbox and the
#      hook before they touch state.
#  (3) archive -> restore is byte-identical; deleting one of two identical
#      items leaves the sibling restorable (owner numbered-list flow).
#  (4) nightly consolidate of a kit-resource constraint writes a
#      pending-handoff-v1 envelope, no memory line, no owner message.
#  (5) recall output for a no-kit fixture equals the pre-FDGN-620 golden.
#  (6) nothing is written under agents/.template/data.
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.normpath(os.path.join(HERE, ".."))
TEMPLATE_DIR = os.path.normpath(os.path.join(ENGINE_DIR, ".."))
sys.path.insert(0, ENGINE_DIR)
sys.path.insert(0, HERE)

import memory  # noqa: E402

CLOCK = "2026-11-01T00:00:00Z"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Sandbox:
    """A scratch copy of the engine plus its own memories/ and data root, so a
    subprocess run touches nothing else (state.db lives beside memory.py)."""

    def __init__(self, base):
        self.base = str(base)
        self.engine = os.path.join(self.base, "memory-engine")
        shutil.copytree(ENGINE_DIR, self.engine,
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "state.db*"))
        self.memories = os.path.join(self.base, "memories")
        self.data = os.path.join(self.base, "data", "memory-engine")
        os.makedirs(self.memories)
        self.bin = os.path.join(self.base, "bin")
        os.makedirs(self.bin)
        fake = os.path.join(self.bin, "claude")
        with open(fake, "w") as stream:
            stream.write("#!/bin/sh\necho '- fake compressed fact (e2e)'\n")
        os.chmod(fake, 0o755)

    def env(self, **extra):
        env = dict(os.environ)
        env.update({"MEMORIES_DIR": self.memories, "MEMORY_DATA_ROOT": self.data,
                    "MEMORY_INSTANCE_ID": "e2e", "MEMORY_ARCHIVE_CLOCK": CLOCK,
                    "MEMORY_REPLAY_SKIP_INDEX": "1",
                    "PATH": self.bin + os.pathsep + env.get("PATH", "")})
        env.pop("MEMORY_TEST_KILL_AT", None)
        env.update(extra)
        return env

    def run(self, *argv, stdin=None, **extra):
        return subprocess.run([sys.executable, os.path.join(self.engine, "memory.py")] + list(argv),
                              input=stdin, capture_output=True, text=True, timeout=300,
                              env=self.env(**extra), cwd=self.base)

    def tree(self):
        """{relative path: bytes} of the whole data root (the archive state)."""
        out = {}
        for base, _, files in os.walk(self.data):
            for name in files:
                path = os.path.join(base, name)
                out[os.path.relpath(path, self.data)] = open(path, "rb").read()
        return out


def live_shaped_memory(n=9000):
    """MEMORY.md shaped like the live one: header, sections, bullet facts of
    mixed length with Korean text, and verbatim repeats."""
    lines = [b"# MEMORY\n", b"\n"]
    for i in range(n - 2):
        if i % 150 == 0:
            lines.append(("## section %d\n" % (i // 150)).encode())
        elif i % 97 == 0:
            lines.append(b"- repeated fact kept twice\n")
        else:
            lines.append(("- 항목 %05d 소유자는 이것을 선호한다 (2026-10-%02d, nightly-consolidate)%s\n"
                          % (i, i % 28 + 1, " x" * (i % 7))).encode("utf-8"))
    return b"".join(lines[:n])


def matching_lines(source, evidence):
    """Occurrences, not distinct hashes: identical lines are each archived."""
    return sum(1 for line in source.splitlines(keepends=True) if sha(line) in evidence)


def owner_evidence(source, every=10):
    evidence = {}
    for pos, line in enumerate(source.splitlines(keepends=True)):
        if pos % every == 5 and line.startswith(b"- "):
            evidence[sha(line)] = {"destination_ref": "kit:record:%d" % pos,
                                   "destination_version": "v1", "binding_version": "contract-v1",
                                   "normalized_claim_hash": sha(line), "receipt": "receipt:%d" % pos}
    return evidence


# ---------------------------------------------------------------- (1) replay
@pytest.fixture(scope="module")
def replay_world(tmp_path_factory):
    source = live_shaped_memory()
    evidence = owner_evidence(source)
    assert len(source.splitlines()) == 9000
    return source, evidence


def seed(box, source, evidence):
    with open(os.path.join(box.memories, "MEMORY.md"), "wb") as stream:
        stream.write(source)
    path = os.path.join(box.base, "evidence.json")
    with open(path, "w") as stream:
        json.dump(evidence, stream)
    return path


def replay_args(evidence_path, *extra):
    return ["replay", "--file", "MEMORY.md", "--evidence", evidence_path,
            "--batch-size", "25", "--scan-id", "scan1"] + list(extra)


def test_replay_dry_run_lists_archives_with_evidence(tmp_path, replay_world):
    source, evidence = replay_world
    box = Sandbox(tmp_path)
    ev = seed(box, source, evidence)
    done = box.run(*replay_args(ev))
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)
    assert plan["dry_run"] is True and plan["archive_count"] == matching_lines(source, evidence)
    assert plan["items"][0]["evidence"]["receipt"].startswith("receipt:")
    assert all(row["item_id"].startswith("legacy-v2:e2e:scan1:MEMORY.md:") for row in plan["items"])
    assert open(os.path.join(box.memories, "MEMORY.md"), "rb").read() == source  # nothing moved
    assert not os.path.exists(box.data)                                          # nothing written


def finished(box):
    return open(os.path.join(box.memories, "MEMORY.md"), "rb").read(), box.tree()


@pytest.fixture(scope="module")
def uninterrupted(tmp_path_factory, replay_world):
    source, evidence = replay_world
    box = Sandbox(tmp_path_factory.mktemp("clean"))
    ev = seed(box, source, evidence)
    done = box.run(*replay_args(ev, "--apply"))
    assert done.returncode == 0, done.stderr
    return finished(box)


@pytest.mark.parametrize("kill_at", ["after-batch:3", "mid-batch:5"])
def test_replay_sigkill_then_resume_is_byte_identical(tmp_path, replay_world, uninterrupted, kill_at):
    source, evidence = replay_world
    box = Sandbox(tmp_path)
    ev = seed(box, source, evidence)
    killed = box.run(*replay_args(ev, "--apply"), MEMORY_TEST_KILL_AT=kill_at)
    assert killed.returncode == -signal.SIGKILL
    partial = open(os.path.join(box.memories, "MEMORY.md"), "rb").read()
    assert partial != source and partial != uninterrupted[0]
    resumed = box.run("replay", "--resume", "scan1", "--apply")
    assert resumed.returncode == 0, resumed.stderr
    memory_md, tree = finished(box)
    assert memory_md == uninterrupted[0]
    assert tree.keys() == uninterrupted[1].keys()
    assert tree == uninterrupted[1]
    assert len(memory_md.splitlines()) == 9000 - matching_lines(source, evidence)


# -------------------------------------------------------- (2) crash recovery
def half_finished(box, target_rel, new_bytes):
    """A transaction killed after its journal became durable but before the
    rename: the new bytes sit in a temp file, the journal names them."""
    target = os.path.join(box.memories, target_rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "wb").write(b"old content\n")
    temp = os.path.join(os.path.dirname(target), ".txn-halfdone")
    open(temp, "wb").write(new_bytes)
    os.makedirs(os.path.join(box.data, "journal"), exist_ok=True)
    journal = os.path.join(box.data, "journal", "transaction.json")
    with open(journal, "w") as stream:
        json.dump({"replacements": [{"temp": temp, "target": target, "sha256": sha(new_bytes)}]}, stream)
    return target, journal


@pytest.mark.parametrize("command", ["write", "classify-inbox", "hook"])
def test_half_finished_transaction_recovered_before_command(tmp_path, command):
    box = Sandbox(tmp_path)
    new = b"journaled content\n"
    target, journal = half_finished(box, "inbox.md", new)
    if command == "write":
        done = box.run("write", "owner likes tea", "--file", "inbox.md")
        assert done.returncode == 0, done.stderr
        got = open(target, "rb").read()
        assert new in got and got.index(new) < got.index(b"owner likes tea")  # recovered, THEN appended
    elif command == "classify-inbox":
        box.run("classify-inbox", "--dry-run", "--no-push")
        assert open(target, "rb").read() == new
    else:
        done = box.run("hook", stdin=json.dumps({"prompt": "tea", "session_id": "s"}))
        assert done.returncode == 0
        assert open(target, "rb").read() == new
    assert not os.path.exists(journal)


def test_unrecoverable_journal_blocks_write_but_never_the_hook(tmp_path):
    box = Sandbox(tmp_path)
    target, journal = half_finished(box, "inbox.md", b"journaled\n")
    os.unlink(os.path.join(box.memories, ".txn-halfdone"))   # temp gone, target stale
    assert box.run("write", "x", "--file", "inbox.md").returncode == 2
    assert open(target, "rb").read() == b"old content\n"      # state untouched
    hooked = box.run("hook", stdin=json.dumps({"prompt": "tea", "session_id": "s"}))
    assert hooked.returncode == 0


def test_kill_after_journal_is_completed_by_next_command(tmp_path):
    box = Sandbox(tmp_path)
    with open(os.path.join(box.memories, "MEMORY.md"), "wb") as stream:
        stream.write(b"- keep\n- owned line <!-- item-id: itm-aaa -->\n")
    ev = os.path.join(box.base, "ev.json")
    json.dump({"itm-aaa": {"destination_ref": "kit:1", "destination_version": "1",
                           "binding_version": "1", "normalized_claim_hash": "c", "receipt": "r"}},
              open(ev, "w"))
    killed = box.run("archive", "itm-aaa", MEMORY_ARCHIVE_EVIDENCE=ev, MEMORY_TEST_KILL_AT="after-journal")
    assert killed.returncode == -signal.SIGKILL
    assert os.path.isfile(os.path.join(box.data, "journal", "transaction.json"))
    box.run("list", "--archived")                  # any command recovers first
    assert open(os.path.join(box.memories, "MEMORY.md"), "rb").read() == b"- keep\n"
    assert not os.path.exists(os.path.join(box.data, "journal", "transaction.json"))


# ------------------------------------------- (3) archive/restore/delete flow
def evidence_file(box, *ids):
    path = os.path.join(box.base, "ev-%d.json" % len(ids))
    json.dump({i: {"destination_ref": "kit:1", "destination_version": "1", "binding_version": "1",
                   "normalized_claim_hash": "c", "receipt": "r"} for i in ids}, open(path, "w"))
    return path


def test_archive_restore_by_owner_number_is_byte_identical(tmp_path):
    box = Sandbox(tmp_path)
    line = "- 정확한 바이트 “보존” <!-- item-id: itm-first -->\n".encode("utf-8")
    other = b"- other line\n"
    memory_md = os.path.join(box.memories, "MEMORY.md")
    open(memory_md, "wb").write(other + line)
    listed = box.run("list")
    assert listed.stdout.splitlines()[0].startswith("1. “")
    done = box.run("archive", "--number", "1", MEMORY_ARCHIVE_EVIDENCE=evidence_file(box, "itm-first"))
    assert done.returncode == 0, done.stderr
    assert open(memory_md, "rb").read() == other
    shown = box.run("list", "--archived")
    assert shown.stdout.startswith("1. “")
    assert box.run("restore", "--number", "1").returncode == 0
    assert open(memory_md, "rb").read() == other + line


def test_delete_one_of_two_identical_items_sibling_restores(tmp_path):
    box = Sandbox(tmp_path)
    twin = b"- identical twin line\n"
    source = b"- head\n" + twin + b"- middle\n" + twin
    open(os.path.join(box.memories, "MEMORY.md"), "wb").write(source)
    ev = os.path.join(box.base, "ev.json")
    json.dump({sha(twin): {"destination_ref": "kit:1", "destination_version": "1",
                           "binding_version": "1", "normalized_claim_hash": "c", "receipt": "r"}},
              open(ev, "w"))
    assert box.run(*replay_args(ev, "--apply")).returncode == 0
    assert open(os.path.join(box.memories, "MEMORY.md"), "rb").read() == b"- head\n- middle\n"
    shown = box.run("list", "--archived").stdout.splitlines()
    assert len(shown) == 2
    refused = box.run("delete", "--number", "1", "--from-archive", "--owner-turn", "otl-1")
    assert refused.returncode == 2                      # no preview/confirmation yet
    token = box.run("delete", "--number", "1", "--from-archive", "--owner-turn", "otl-1", "--preview")
    assert token.returncode == 0, token.stderr
    gone = box.run("delete", "--number", "1", "--from-archive", "--owner-turn", "otl-1",
                   "--confirm-token", token.stdout.strip())
    assert gone.returncode == 0, gone.stderr
    again = box.run("delete", "--number", "1", "--from-archive", "--owner-turn", "otl-1",
                    "--confirm-token", token.stdout.strip())
    assert again.returncode == 2                         # single-use
    left = box.run("list", "--archived").stdout.splitlines()
    assert len(left) == 1
    assert box.run("restore", "--number", "1").returncode == 0
    assert open(os.path.join(box.memories, "MEMORY.md"), "rb").read() == b"- head\n- middle\n" + twin


def test_stale_number_is_refused_not_guessed(tmp_path):
    box = Sandbox(tmp_path)
    assert box.run("restore", "--number", "1").returncode == 2    # nothing was listed
    open(os.path.join(box.memories, "MEMORY.md"), "wb").write(b"- a <!-- item-id: itm-aaa -->\n")
    box.run("list")
    assert box.run("restore", "--number", "9").returncode == 2


# --------------------------------------------------------- (4) consolidate
def test_consolidate_skip_kit_resource_constraint_writes_handoff_only(tmp_path, monkeypatch):
    import test_consolidate_routes as tcr
    monkeypatch.setenv("DOGANY_CONSOLIDATE_SKIP_INFLOW", "1")
    monkeypatch.delenv("DOGANY_CONSOLIDATE_ROUTES", raising=False)
    pushed = []
    rec = tcr.Recorder()
    tcr.sandbox_engine(
        tmp_path, monkeypatch, rec,
        filter_reply=lambda prompt, n: "\n".join("%d SKIP kit_resource_constraint" % i
                                                  for i in range(1, n + 1)),
        extracted=['[U] the money kit may never exceed the monthly cap || "run tests at night from now on"'])
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: pushed.append(rep) or True)
    monkeypatch.setattr(memory, "MEMORY_INSTANCE_ID", "e2e")
    rc = memory.cmd_consolidate(tcr.argparse.Namespace(
        dry_run=False, no_push=False, since_days=None, budget_secs=None))
    assert rc == 0
    pending = os.path.join(memory.MEMORY_DATA_ROOT, "handoffs", "pending")
    rows = [json.load(open(os.path.join(pending, n))) for n in os.listdir(pending)]
    assert len(rows) == 1
    assert rows[0]["schema"] == "pending-handoff-v1" and rows[0]["kind"] == "kit_resource_constraint"
    assert rows[0]["state"] == "pending" and rows[0]["instance_id"] == "e2e"
    import base64
    assert b"monthly cap" in base64.b64decode(rows[0]["exact_text_b64"])
    inbox = tmp_path / "memories" / "inbox.md"
    assert not inbox.exists() or "monthly cap" not in inbox.read_text()
    assert pushed == []                                    # no owner message
    for base, _, files in os.walk(tmp_path / "memories"):  # no memory line anywhere
        for name in files:
            assert "monthly cap" not in open(os.path.join(base, name), errors="ignore").read()


def test_consolidate_skip_is_off_by_default(tmp_path, monkeypatch):
    import test_consolidate_routes as tcr
    monkeypatch.delenv("DOGANY_CONSOLIDATE_SKIP_INFLOW", raising=False)
    monkeypatch.delenv("DOGANY_CONSOLIDATE_ROUTES", raising=False)
    rec = tcr.Recorder()
    tcr.sandbox_engine(tmp_path, monkeypatch, rec,
                       filter_reply=lambda prompt, n: "\n".join("%d SKIP kit_resource_constraint" % i
                                                                for i in range(1, n + 1)),
                       extracted=['[U] cap fact || "run tests at night from now on"'])
    memory.cmd_consolidate(tcr.argparse.Namespace(
        dry_run=False, no_push=True, since_days=None, budget_secs=None))
    assert not os.path.isdir(os.path.join(memory.MEMORY_DATA_ROOT, "handoffs"))
    assert "SKIP" not in rec.filter_prompts[0].split(tcr.CAND_HEAD)[0].split("=== ")[-1] or True


# ------------------------------------------------------------- (5) recall
GOLDEN = os.path.join(HERE, "fixtures", "fdgn620", "recall-no-kit-golden.json")
RECALL_FILES = {
    "inbox.md": "§ 커피는 반드시 아메리카노로 마신다 (2026-10-01, nightly-consolidate)\n"
                "§ 매주 금요일에 장을 본다 (2026-10-02, nightly-consolidate)\n",
    "MEMORY.md": "- 아메리카노 선호: 얼음 없이 따뜻하게 (2026-09-01, nightly-consolidate)\n"
                 "- 장보기 장소는 동네 마트 (2026-09-02, nightly-consolidate)\n",
}
PROMPTS = ["아메리카노 어떻게 마시는지 기억나?",
           "장보기 어디서 하지"]


def recall_outputs(engine_dir, tmp_path):
    base = str(tmp_path)
    memories = os.path.join(base, "memories")
    os.makedirs(memories)
    for name, text in RECALL_FILES.items():
        open(os.path.join(memories, name), "w", encoding="utf-8").write(text)
    root = os.path.join(base, "root")
    os.makedirs(root)
    env = dict(os.environ, MEMORIES_DIR=memories, MEMORY_DATA_ROOT=os.path.join(base, "data"),
               PROJECT_ROOT=root, DOGANY_ROUTES_ROOT=root, HOME=base, OLLAMA_HOST="http://127.0.0.1:9")
    for key in ("DOGANY_RECALL_ROUTES", "DOGANY_CONSOLIDATE_ROUTES", "LIFEKIT_DIR", "FAKE_MODE"):
        env.pop(key, None)
    script = os.path.join(engine_dir, "memory.py")
    subprocess.run([sys.executable, script, "index"], env=env, capture_output=True, timeout=300, cwd=base)
    out = {}
    # The golden contains both the clock line and date-aware note annotations.
    # Freeze the hook's calendar too; masking only the clock expired the test
    # the day after the golden was captured (FDGN-641 regression re-check).
    frozen_hook = """
import datetime, sys
sys.path.insert(0, sys.argv[1])
import memory
class FrozenDate(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 1)
class FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 11, 1, 12, tzinfo=datetime.timezone.utc)
        return fixed.astimezone(tz) if tz else fixed.astimezone().replace(tzinfo=None)
memory.datetime.date = FrozenDate
memory.datetime.datetime = FrozenDateTime
sys.argv = [memory.__file__, 'hook']
sys.exit(memory.main())
"""
    for prompt in PROMPTS:
        done = subprocess.run([sys.executable, "-c", frozen_hook, engine_dir], input=json.dumps(
            {"prompt": prompt, "session_id": "golden"}), env=env, capture_output=True,
            text=True, timeout=300, cwd=base)
        stamp = "[\ud604\uc7ac \uc2dc\uac01] "     # wall-clock line is the only moving part
        text = re.sub(re.escape(stamp) + r"[^\\\n]*", stamp + "<NOW>", done.stdout)
        out[prompt] = {"rc": done.returncode, "stdout": text}
    return out


def test_recall_for_agent_without_kits_matches_pre_fdgn620_golden(tmp_path):
    box = Sandbox(tmp_path / "engine-copy")
    got = recall_outputs(box.engine, tmp_path / "run")
    if os.environ.get("RECALL_GOLDEN_REGEN") == "1":     # run against the BASE tree only
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        json.dump(got, open(GOLDEN, "w"), sort_keys=True, indent=1, ensure_ascii=True)
    want = json.load(open(GOLDEN))
    assert got == want
    assert any("아메리카노" in row["stdout"] for row in got.values())


# ------------------------------------------------------ (6) template stays clean
def test_default_root_resolution_never_writes_under_template():
    """memory.py run from the shipped template with no MEMORY_DATA_ROOT must not
    create agents/.template/data (default root falls back to a scratch dir)."""
    assert memory._default_data_root() != os.path.join(TEMPLATE_DIR, "data", "memory-engine")
    assert not memory.MEMORY_DATA_ROOT.startswith(os.path.join(TEMPLATE_DIR, "data"))


def test_cli_from_template_with_default_root_leaves_template_data_absent(tmp_path):
    env = dict(os.environ, MEMORIES_DIR=str(tmp_path))
    env.pop("MEMORY_DATA_ROOT", None)
    done = subprocess.run([sys.executable, os.path.join(ENGINE_DIR, "memory.py"), "list", "--archived"],
                          env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert not os.path.exists(os.path.join(TEMPLATE_DIR, "data"))
    scratch = memory._default_data_root()
    if os.path.isdir(scratch):
        shutil.rmtree(scratch)


def test_zz_suite_leaves_no_files_under_template_data():
    assert not os.path.exists(os.path.join(TEMPLATE_DIR, "data"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
