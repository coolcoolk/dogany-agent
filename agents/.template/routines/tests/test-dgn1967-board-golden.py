#!/usr/bin/env python3
# test-lane: fast
# test-component: status-footer
# test-tier: 0
# test-prereq: tmp-files, subprocess
"""DGN-1967 S0 -- characterization golden for the workbench board.

The workbench refactor (docs/reviews/dgn1967-workbench-review.md, section 3)
is behaviour-preserving step by step.  This suite freezes what the board
generator produces TODAY so every later step can prove it changed nothing
the owner sees (or changed exactly the bytes it meant to).

Frozen inputs, frozen clock (2026-10-09 00:30 Asia/Seoul -- half an hour past
midnight, so the cross-day rows are exercised), two layers:

  B-*  _build_dashboard(...) called directly: full board, forced headers,
       hidden sections, and over-budget boards that walk the whole length-
       cut ladder (fold bodies -> recent done -> unpark items -> console
       items -> plugin variants/removal -> live -> decision tail).
  E-*  _refresh_board(...) end to end over fixture files: transcript with
       live / stalled / stopped / completed / redirected subagents and
       background jobs (fresh, noticed past the ceiling across midnight,
       closed), ledger running / branch-fallback / dead / review rows,
       decisions with and without folds, unpark fresh / stale, console
       pending / failed, a plugin with trigger / pinned / variants / hide.
       Snapshot = the write / clear / no-touch decision + the published
       text, and the v1 plugin ctx (minus host) when a plugin ran.

Golden files live in routines/tests/fixtures/dgn1967-board-golden/.  A
deliberate output change regenerates them with --update and the diff is the
review record.

Run:  /usr/bin/python3 routines/tests/test-dgn1967-board-golden.py [--update]
Exit: 0 all match, 1 any mismatch.
"""

import difflib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time as _real_time

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
FOOTER_PY = os.path.join(ROUTINES, "status-footer.py")
GOLDEN_DIR = os.path.join(HERE, "fixtures", "dgn1967-board-golden")
UPDATE = "--update" in sys.argv[1:]

os.environ["TZ"] = "Asia/Seoul"
_real_time.tzset()
NOW = _real_time.mktime((2026, 10, 9, 0, 30, 0, 0, 0, -1))
MIN = 60

WORK = tempfile.mkdtemp(prefix="dgn1967-golden.")
os.environ["DOGANY_AGENT_CONF"] = os.path.join(WORK, "agent.conf")
open(os.environ["DOGANY_AGENT_CONF"], "w").close()
os.environ["HOME"] = os.path.join(WORK, "home")
os.environ["DOGANY_BOT_DATA_DIR"] = os.path.join(WORK, "bot")
os.environ["DOGANY_SESSIONS_FILE"] = os.path.join(WORK, "bot", "sessions.json")
os.environ["DOGANY_SUBAGENT_STOPS_DIR"] = os.path.join(WORK, "stops")
os.environ["DOGANY_BG_NOTICE_STATE"] = os.path.join(WORK, "bg-state.json")
os.environ["DOGANY_LEDGER_FILE"] = os.path.join(WORK, "ledger.md")
os.environ["DOGANY_DISPATCH_DIR"] = os.path.join(WORK, "dispatch")
os.environ["DOGANY_DECISIONS_FILE"] = os.path.join(WORK, "decisions.md")
os.environ["DOGANY_DECISIONS_DIR"] = os.path.join(WORK, "decisions")
os.environ["DOGANY_UNPARK_FILE"] = os.path.join(WORK, "unpark.md")
os.environ["DOGANY_DECISION_ACTIONS_FILE"] = os.path.join(WORK, "actions.md")
os.environ["DOGANY_WORKLOG_DIR"] = os.path.join(WORK, "worklog")
# Review rows: no readable repo -> every pending-review row counts (the
# DGN-1730 degrade path), so the golden does not depend on git state.
os.environ["DOGANY_INSTANCE_ROOT"] = os.path.join(WORK, "no-repo")
os.environ["DOGANY_REPO_ROOT"] = os.path.join(WORK, "no-repo")
os.environ["DOGANY_LEDGER_REVIEW_CACHE"] = os.path.join(WORK, "review.json")
os.environ["DOGANY_BOARD_PLUGIN"] = "none"
os.environ["GOLDEN_CTX_OUT"] = os.path.join(WORK, "ctx.json")
for _k in ("DOGANY_LIVE_SHOW_MODEL", "DOGANY_LIVE_LABEL", "DOGANY_BOARD_EMOJI",
           "DOGANY_BOARD_TITLE", "DOGANY_CONSOLE_BASE", "DOGANY_SIBLING_TERM",
           "DOGANY_LIVE_ITEM_PREFIX", "DOGANY_LANG", "TICKET_FILE_GLOB",
           "DOGANY_DASHBOARD_DEBUG"):
    os.environ.pop(_k, None)


class _FrozenTime(object):
    """The time module with the wall clock pinned to NOW (monotonic/sleep
    stay real: they only bound budgets)."""

    def __getattr__(self, name):
        return getattr(_real_time, name)

    @staticmethod
    def time():
        return NOW

    @staticmethod
    def time_ns():
        return int(NOW * 1e9)

    @staticmethod
    def localtime(secs=None):
        return _real_time.localtime(NOW if secs is None else secs)

    @staticmethod
    def strftime(fmt, t=None):
        return _real_time.strftime(
            fmt, _real_time.localtime(NOW) if t is None else t)


def load(lang=None):
    if lang is None:
        os.environ.pop("DOGANY_LANG", None)
    else:
        os.environ["DOGANY_LANG"] = lang
    spec = importlib.util.spec_from_file_location("status_footer", FOOTER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time = _FrozenTime()
    mod._machine_name = lambda: "Mac mini"  # DGN-2116: hostname-independent goldens
    bgn = getattr(mod, "bg_job_notice", None)
    if bgn is not None and hasattr(bgn, "time"):
        bgn.time = _FrozenTime()
    return mod


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.utime(path, (NOW if mtime is None else mtime,) * 2)


def iso(epoch):
    return _real_time.strftime("%Y-%m-%dT%H:%M:%S",
                               _real_time.gmtime(epoch)) + ".000Z"


def local_ts(epoch):
    return _real_time.strftime("%Y-%m-%d %H:%M KST",
                               _real_time.localtime(epoch))


FAILS = []
SEEN = []


def golden(name, text):
    """Compare `text` to the golden file `name` (or rewrite it on --update)."""
    SEEN.append(name)
    path = os.path.join(GOLDEN_DIR, name)
    if UPDATE:
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("  UPDATED: %s" % name)
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            want = fh.read()
    except OSError:
        want = None
    if want == text:
        print("  PASS: %s" % name)
        return
    FAILS.append(name)
    print("  FAIL: %s" % name)
    if want is None:
        print("    (golden file missing; run with --update)")
        return
    for line in difflib.unified_diff(want.splitlines(), text.splitlines(),
                                     "golden/" + name, "actual/" + name,
                                     lineterm="", n=2):
        print("    " + line)


# ---------------------------------------------------------------------------
# B: _build_dashboard, direct inputs
# ---------------------------------------------------------------------------
print("\n--- B: _build_dashboard (direct) ---")
sf = load()

FOLD_BODY = "물음: 어느 쪽 먼저?\n권고: 알파 먼저\n선택지:\n1. 알파\n2. 베타"
DECS = [{"text": "dec-901 배포 순서 -> https://c.example/decision/dec-901",
         "dec_id": "dec-901", "body": FOLD_BODY},
        {"text": "아이디 없는 결정", "dec_id": None, "body": None},
        "옛 형식 문자열 결정"]
LIVE = ["Claude Opus 5.5: 보고서 초안 · 00:18 (12분~)",
        "모델 없는 조사 · 00:20 (10분~)",
        "멈춘 작업 (무응답 20분+ -- 사망 추정)",
        "테스트 실행 · 00:26 (4분~)"]
DONE = ["DGN-9001 끝난 일 (2026-10-09)", "DGN-9002 또 끝난 일", "DGN-9003 세번째"]
UNPARK_FRESH = (NOW - 3600, "2026-10-08 23:30",
                ["DGN-1  P2  UNPARK  gate: ok", "DGN-2 P3 UNPARK gate: ok"])
UNPARK_STALE = (NOW - 30 * 3600, "2026-10-07 18:30", [])
UNPARK_NOSTAMP = (None, None, [])
CA_PENDING = (["act-1 approve dec-901 1d", "act-2 hold dec-902 0d"], False)
CA_FAILED = ([], True)
EXTRAS = [
    {"key": "tickets", "pinned": False, "variants": [
        ["[진행중 티켓] 3건", "- T1 하나", "- T2 둘", "- T3 셋"],
        ["[진행중 티켓] 3건", "- T1 하나", "- 외 2건"],
        ["[진행중 티켓] 3건"]]},
    {"key": "products", "pinned": True, "variants": [["[제품]", "- p: v1"]]},
    {"key": "notes", "pinned": False, "variants": [["[메모]", "- 한 줄"]]},
]


def b(name, *args, **kw):
    golden("B-%s.txt" % name, sf._build_dashboard(*args, **kw))


b("full", DECS, LIVE, DONE, UNPARK_FRESH, CA_PENDING, EXTRAS, ())
b("forced-headers", ["d"], [], [], UNPARK_STALE, CA_FAILED)
b("unpark-nostamp", [], ["live"], [], UNPARK_NOSTAMP, None)
b("unpark-fresh-empty", ["d"], [], [], (NOW - 60, "2026-10-09 00:29", []),
  ([], False))
b("hidden", DECS, LIVE, DONE, UNPARK_FRESH, CA_PENDING, EXTRAS,
  ("recent_done", "unpark", "console_actions", "decisions", "live"))
b("empty", [], [], [])

# DGN-2116 / dec-464: legacy Why inputs never contribute board lines.
# These cases preserve the rest of the board, including length cuts.
WHY = {"none": 2, "hypothesis": 1, "provisional": 1, "contested": 0,
       "asks": 1, "fold": ["DGN-101", "DGN-103", "DGN-105", "DGN-107"]}
b("why", DECS, LIVE, DONE, UNPARK_FRESH, CA_PENDING, EXTRAS, (), why=WHY)
b("why-nofold", ["d"], ["live"], [],
  why={"none": 1, "hypothesis": 0, "asks": 0})
b("why-zero", ["d"], ["live"], [],
  why={"none": 0, "hypothesis": 0, "provisional": 0, "contested": 0,
       "asks": 0, "fold": ["DGN-1"]})
b("why-hidden", ["d"], ["live"], [], hidden=("why",), why=WHY)


def overbudget(n_live, n_decs, bodies=True, why=None):
    decs = [{"text": "dec-%03d %s" % (i, "결정 " * 20), "dec_id": "dec-%03d" % i,
             "body": ("상세 " * 150) if bodies else None}
            for i in range(n_decs)]
    live = ["작업 %02d %s · 00:10 (20분~)" % (i, "x" * 120)
            for i in range(n_live)]
    done = ["DGN-%d %s" % (9000 + i, "완료 " * 30) for i in range(3)]
    unp = (NOW - 3600, "2026-10-08 23:30",
           ["DGN-%d P2 UNPARK %s" % (i, "y" * 150) for i in range(6)])
    ca = (["act-%d approve dec-%d %s 1d" % (i, i, "z" * 100)
           for i in range(5)], True)
    extras = [
        {"key": "big", "pinned": False, "variants": [
            ["[큰 섹션]"] + ["- row %d %s" % (i, "w" * 200) for i in range(6)],
            ["[큰 섹션]", "- row 0", "- 외 5건"]]},
        {"key": "pin", "pinned": True, "variants": [
            ["[고정]"] + ["- keep %d" % i for i in range(3)]]},
        {"key": "tail", "pinned": False, "variants": [
            ["[꼬리]"] + ["- t %d %s" % (i, "v" * 150) for i in range(4)]]},
    ]
    return sf._build_dashboard(decs, live, done, unp, ca, extras, (), why=why)


# Each size stops the ladder at a different rung; together they pin the
# whole cut order.
for label, n_live, n_decs in (("bodies", 2, 4), ("unpark", 4, 3),
                              ("extras", 8, 3), ("live", 22, 3),
                              ("dectail", 0, 40)):
    golden("B-cut-%s.txt" % label, overbudget(n_live, n_decs))
golden("B-cut-nobody-tail.txt", overbudget(30, 25, bodies=False))
golden("B-cut-dectail-nobody.txt", overbudget(0, 80, bodies=False))


def signature(text):
    """Block structure of a board: '<first line[:24]> x<line count>' per
    blank-line-separated block -- compact, and it moves on every cut."""
    out = []
    for block in text.rstrip("\n").split("\n\n"):
        lines = block.split("\n")
        out.append("%s x%d" % (lines[0][:24], len(lines)))
    return " | ".join(out)


# The fine-grained walk: one more live row per step pushes the cut one rung
# further, so every rung boundary of the ladder shows up as a change here.
golden("B-cut-ladder.txt", "".join(
    "n_live=%02d units=%d :: %s\n" % (
        n, sf._u16len(t), signature(t))
    for n in range(0, 41)
    for t in [overbudget(n, 3)]))
# The same walk with legacy Why data must match the walk without it.
golden("B-cut-ladder-why.txt", "".join(
    "n_live=%02d units=%d :: %s\n" % (
        n, sf._u16len(t), signature(t))
    for n in range(0, 41)
    for t in [overbudget(n, 3, why=WHY)]))
_orig_max = sf.DASHBOARD_MAX_UNITS
sf.DASHBOARD_MAX_UNITS = 900
golden("B-cut-small-budget.txt",
       sf._build_dashboard(DECS, LIVE, DONE, UNPARK_FRESH, CA_PENDING,
                           EXTRAS, ()))
sf.DASHBOARD_MAX_UNITS = _orig_max

# DGN-1967 S8: an English-language owner gets English board copy (the
# table's "en" column); an unset or unknown language keeps the Korean copy
# above byte for byte.
sf_en = load("en")
golden("B-en-full.txt", sf_en._build_dashboard(
    DECS, LIVE, DONE, UNPARK_FRESH, CA_PENDING, EXTRAS, ()))
golden("B-en-forced-headers.txt", sf_en._build_dashboard(
    ["d"], [], [], UNPARK_STALE, CA_FAILED))
golden("B-en-unpark-nostamp.txt", sf_en._build_dashboard(
    [], ["live"], [], UNPARK_NOSTAMP, None))
golden("B-en-cut-bodies.txt", sf_en._build_dashboard(
    [{"text": "dec-%d" % i, "body": "x" * 600} for i in range(8)], ["live"],
    []))
golden("B-en-why.txt", sf_en._build_dashboard(
    ["d"], ["live"], [], why=WHY))
golden("B-unknown-lang-is-ko.txt", load("ja")._build_dashboard(
    DECS, LIVE, DONE, UNPARK_FRESH, CA_PENDING, EXTRAS, ()))
os.environ.pop("DOGANY_LANG", None)

# ---------------------------------------------------------------------------
# E: _refresh_board, fixture tree
# ---------------------------------------------------------------------------
print("\n--- E: _refresh_board (fixture tree) ---")
SESS = "sess-1967"
PROJ = os.path.join(os.environ["HOME"], ".claude", "projects", "enc")
SUBDIR = os.path.join(PROJ, SESS, "subagents")
TRANSCRIPT = os.path.join(PROJ, SESS + ".jsonl")
PLUGIN = os.path.join(WORK, "plugin.py")


def _launch(aid):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text":
         "Async agent launched successfully.\nagentId: %s (x)" % aid}]}]}}


def _complete(aid):
    return {"type": "user", "message": {"content": [
        {"type": "text", "text": "<task-notification>\n<task-id>%s</task-id>"
         "\n<status>completed</status>\n</task-notification>" % aid}]}}


def _bg(tool_id, task_id, start, desc, tool="Bash", ack=None):
    inp = {"command": "run %s" % task_id, "description": desc,
           "run_in_background": True}
    if tool == "Monitor":
        inp = {"command": "tail -f x", "description": desc, "timeout_ms": 3600000}
    use = {"type": "assistant", "timestamp": iso(start), "message": {
        "content": [{"type": "tool_use", "id": tool_id, "name": tool,
                     "input": inp}]}}
    if ack is None:
        ack = ("Command running in background with ID: %s" % task_id
               if tool == "Bash" else "Monitor started (task %s, x)" % task_id)
    res = {"type": "user", "timestamp": iso(start), "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": ack}]}}
    return [use, res]


def _agent(aid, desc, model=None, spawn=NOW - 12 * MIN, jsonl_mtime=NOW - MIN,
           observed=None):
    meta = {"description": desc}
    if model:
        meta["model"] = model
    write(os.path.join(SUBDIR, "agent-%s.meta.json" % aid), json.dumps(meta),
          spawn)
    msg = {"model": observed} if observed else {}
    write(os.path.join(SUBDIR, "agent-%s.jsonl" % aid),
          json.dumps({"type": "assistant", "message": msg}) + "\n",
          jsonl_mtime)


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def base_tree():
    """The full fixture tree every E case starts from."""
    for d in ("home", "bot", "stops", "dispatch", "decisions", "worklog"):
        shutil.rmtree(os.path.join(WORK, d), ignore_errors=True)
    for f in ("bg-state.json", "ledger.md", "decisions.md", "unpark.md",
              "actions.md", "review.json", "ctx.json", "plugin.py"):
        path = os.path.join(WORK, f)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.unlink(path)
    os.environ["DOGANY_BOARD_PLUGIN"] = "none"
    write(os.environ["DOGANY_SESSIONS_FILE"],
          json.dumps({"main": {"session_id": SESS}}))
    # Subagents.
    _agent("aa01", "보고서 초안", "opus", observed="claude-opus-5-5")
    _agent("aa02", "모델 없는 조사", spawn=NOW - 10 * MIN)
    _agent("aa03", "멈춘 작업", "sonnet", jsonl_mtime=NOW - 30 * MIN)
    _agent("aa04", "멈춰 세운 작업", "sonnet", jsonl_mtime=NOW - 30 * MIN)
    _agent("aa05", "끝난 작업", "haiku")
    _agent("aa06", "처음 설명", "sonnet", spawn=NOW - 5 * MIN)
    write(os.path.join(os.environ["DOGANY_SUBAGENT_STOPS_DIR"], "aa04.json"),
          json.dumps({"agent_id": "aa04", "reason": "stop", "ts": NOW - 60}))
    entries = [_launch("aa01"), _launch("aa02"), _launch("aa03"),
               _launch("aa04"), _launch("aa05"), _complete("aa05"),
               _launch("aa06"),
               {"type": "assistant", "message": {"content": [
                   {"type": "tool_use", "name": "SendMessage", "id": "sm1",
                    "input": {"to": "aa06", "summary": "방향 바꾼 작업",
                              "message": "x"}}]}}]
    entries += _bg("tu1", "bgfresh", NOW - 4 * MIN, "테스트 실행")
    # Launched 23:50 yesterday, START noticed -> kept past the 20 min
    # ceiling, across midnight.
    entries += _bg("tu2", "bgnight", NOW - 40 * MIN, "릴리스 빌드")
    entries += _bg("tu3", "bgdone", NOW - 6 * MIN, "끝난 감시", tool="Monitor")
    entries.append({"type": "user", "timestamp": iso(NOW - 2 * MIN),
                    "message": {"content": [{"type": "text", "text":
                     "<task-notification>\n<task-id>bgdone</task-id>\n"
                     "<status>completed</status>\n</task-notification>"}]}})
    entries += _bg("tu4", "bgold", NOW - 50 * MIN, "잊힌 작업")
    write(TRANSCRIPT, "\n".join(json.dumps(e) for e in entries) + "\n")
    write(os.environ["DOGANY_BG_NOTICE_STATE"], json.dumps({"bgnight": {
        "start_sent": True, "end_sent": False, "name": "릴리스 빌드",
        "slot": "bash", "tool": "Bash", "start": NOW - 40 * MIN,
        "queued": NOW - 40 * MIN, "recorded": NOW - 40 * MIN}}))
    # Ledger + dispatch run dirs.
    spawn_alpha = NOW - 140 * MIN          # 22:10 yesterday
    write(os.environ["DOGANY_LEDGER_FILE"],
          "| branch | item | state | attempts | backoff_until | last_ts | flags | note |\n"
          "|---|---|---|---|---|---|---|---|\n"
          "| dsp/alpha | 알파 배포 준비 | running | 0 | - | %s | - | "
          "dispatch 스폰 (model=opus runtime=claude-code axis=judgment) |\n"
          "| dsp/beta | - | running | 0 | - | %s | - | hand note |\n"
          "| dsp/gamma | 죽은 작업 | running | 0 | - | %s | - | "
          "dispatch 스폰 (model=sonnet runtime=claude-code) |\n"
          "| dsp/delta | 검토 대기 [DGN-9010] | pending-review | 0 | - | %s | - | "
          "done HEAD=abcdef1 |\n"
          "| dsp/eps | 끝 | done | 0 | - | %s | - | x |\n"
          % (local_ts(spawn_alpha), local_ts(NOW - 10 * MIN),
             local_ts(NOW - 25 * MIN), local_ts(NOW - 60 * MIN),
             local_ts(NOW - 90 * MIN)))
    dsp = os.environ["DOGANY_DISPATCH_DIR"]
    write(os.path.join(dsp, "dsp-a", "run.env"),
          "LEDGER_BRANCH=dsp/alpha\nSPAWN_EPOCH=%d\n" % spawn_alpha)
    write(os.path.join(dsp, "dsp-a", "pid"), "%d\n" % os.getpid())
    write(os.path.join(dsp, "dsp-a", "stream.jsonl"),
          json.dumps({"type": "assistant",
                      "message": {"model": "claude-opus-5-5"}}) + "\n")
    write(os.path.join(dsp, "dsp-g", "run.env"), "LEDGER_BRANCH=dsp/gamma\n")
    write(os.path.join(dsp, "dsp-g", "pid"), "%d\n" % _dead_pid())
    # Decisions.
    write(os.environ["DOGANY_DECISIONS_FILE"],
          "# pending decisions\n"
          "- [2026-10-08] [dec-901] 배포 순서 결정\n"
          "- [2026-10-08] 아이디 없는 결정\n"
          "- [2026-10-07] [dec-902] 상세 없는 결정\n")
    write(os.path.join(os.environ["DOGANY_DECISIONS_DIR"], "dec-901.md"),
          "# dec-901\n## 질문\n어느 쪽 먼저?\n## 에이전트 권고\n알파 먼저\n"
          "## 옵션\n1. 알파\n2. 베타\n## 배경\n무시\n")
    # Unpark (fresh, two candidates) + console journal (pending + failed).
    write(os.environ["DOGANY_UNPARK_FILE"],
          "# unpark\nlast-scan: 2026-10-08 23:30:00 KST\ncandidates: 2\n"
          "DGN-1  P2  UNPARK  gate: ok\nDGN-2  P3  UNPARK  gate: ok\n")
    write(os.environ["DOGANY_DECISION_ACTIONS_FILE"],
          "## act-100 [pending]\ntarget: dec-901\naction: approve\n"
          "decided_at: 2026-10-07 20:00 KST\n\n"
          "## act-101 [failed]\ntarget: dec-900\naction: reject\n"
          "decided_at: 2026-10-06 10:00 KST\n\n"
          "## act-102 [applied]\ntarget: dec-899\naction: hold\n")


PLUGIN_FULL = r'''
import json, os
PLUGIN_API = 1

def build_sections(ctx):
    host = ctx["host"]
    snap = dict((k, v) for k, v in ctx.items() if k != "host")
    with open(os.environ["GOLDEN_CTX_OUT"], "w") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1, sort_keys=True)
    pre, suf = host._live_stamp("opus", host.time.time() - 300)
    return {"ok": True, "hide": %(hide)r, "sections": [
        {"key": "tickets", "trigger": %(trigger)r, "variants": [
            ["[진행중 티켓] %%d건" %% len(ctx["live_descs"])]
            + ["- " + host._cap_item(d, 40) for d in ctx["live_descs"]],
            ["[진행중 티켓] %%d건" %% len(ctx["live_descs"])]]},
        {"key": "stamp", "pinned": True,
         "lines": ["[스탬프] " + pre + suf]},
        {"key": "review", "lines": ["[검토]"]
         + ["- " + r for r in ctx["review_descs"]]
         + ["- 완료: " + c for c in ctx["completed_descs"]]},
    ]}
'''


def plugin(trigger=True, hide=(), body=None):
    write(PLUGIN, body if body is not None else
          PLUGIN_FULL % {"trigger": trigger, "hide": list(hide)})
    os.environ["DOGANY_BOARD_PLUGIN"] = PLUGIN


def run_case(name, setup=None, lang=None, why=None):
    base_tree()
    if setup:
        setup()
    mod = load(lang)
    if why is not None:
        write(ENV["DOGANY_AGENT_CONF"], "WHY_NET=on\n")
        # A legacy collector must never be read, including on refresh.
        def unused_why():
            raise AssertionError("workbench must not collect Why data")
        mod._collect_why = unused_why
    calls = []
    mod._board_publish = (lambda text, actions=None:
                          calls.append(("publish", text)) or True)
    mod._board_clear = lambda: calls.append(("clear", None)) or True
    mod._refresh_board(TRANSCRIPT, sync_notices=False)
    if not calls:
        out = "decision: no-touch\n"
    else:
        out = "".join("decision: %s\n%s" % (kind, ("" if text is None else
                                                    "-----\n" + text))
                      for kind, text in calls)
    golden("E-%s.txt" % name, out)
    ctx_path = os.environ["GOLDEN_CTX_OUT"]
    if os.path.isfile(ctx_path):
        with open(ctx_path, encoding="utf-8") as fh:
            golden("E-%s.ctx.json" % name, fh.read() + "\n")


def _rm(*paths):
    def setup():
        for p in paths:
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.unlink(p)
    return setup


def _seq(*fns):
    def setup():
        for f in fns:
            f()
    return setup


ENV = os.environ
EMPTY_TRANSCRIPT = lambda: write(TRANSCRIPT, "")
NO_DECISIONS = lambda: write(ENV["DOGANY_DECISIONS_FILE"], "# none\n")
NOTHING = _seq(EMPTY_TRANSCRIPT, NO_DECISIONS,
               _rm(ENV["DOGANY_LEDGER_FILE"], ENV["DOGANY_UNPARK_FILE"],
                   ENV["DOGANY_DECISION_ACTIONS_FILE"]))

run_case("full-no-plugin")
run_case("full-plugin", plugin)
run_case("plugin-hides", lambda: plugin(hide=("unpark", "console_actions",
                                              "recent_done", "live")))
run_case("confirmed-empty", NOTHING)
run_case("confirmed-empty-plugin-quiet",
         _seq(NOTHING, lambda: plugin(trigger=False)))
run_case("plugin-trigger-only", _seq(NOTHING, lambda: plugin(trigger=True)))
run_case("transcript-missing-nothing-else",
         _seq(NOTHING, _rm(TRANSCRIPT)))
run_case("transcript-missing-ledger-live", _rm(TRANSCRIPT))
run_case("decisions-unreadable",
         _seq(_rm(ENV["DOGANY_DECISIONS_FILE"]),
              lambda: os.makedirs(ENV["DOGANY_DECISIONS_FILE"]), plugin))
run_case("ledger-unreadable-nothing-else",
         _seq(NOTHING, lambda: os.makedirs(ENV["DOGANY_LEDGER_FILE"])))
run_case("plugin-raises-nothing-else",
         _seq(NOTHING, lambda: plugin(body="PLUGIN_API = 1\n"
                                      "def build_sections(ctx):\n"
                                      "    raise ValueError('x')\n")))
run_case("plugin-not-ok-nothing-else",
         _seq(NOTHING, lambda: plugin(body="PLUGIN_API = 1\n"
                                      "def build_sections(ctx):\n"
                                      "    return {'ok': False, 'sections': []}\n")))
run_case("plugin-wrong-api-nothing-else",
         _seq(NOTHING, lambda: plugin(body="PLUGIN_API = 9\n")))
run_case("unpark-stale-only",
         _seq(NOTHING, lambda: write(ENV["DOGANY_UNPARK_FILE"],
                                     "last-scan: 2026-10-07 06:00:00 KST\n"
                                     "candidates: 0\n(후보 없음)\n")))
run_case("unpark-stale-hidden-by-plugin",
         _seq(NOTHING, lambda: write(ENV["DOGANY_UNPARK_FILE"],
                                     "last-scan: 2026-10-07 06:00:00 KST\n"
                                     "candidates: 0\n"),
              lambda: plugin(trigger=False, hide=("unpark",))))
run_case("unpark-fresh-zero",
         _seq(NOTHING, lambda: write(ENV["DOGANY_UNPARK_FILE"],
                                     "last-scan: 2026-10-08 23:00:00 KST\n"
                                     "candidates: 0\n(후보 없음)\n")))
run_case("console-failed-only",
         _seq(NOTHING, lambda: write(ENV["DOGANY_DECISION_ACTIONS_FILE"],
                                     "## act-5 [failed]\ntarget: dec-1\n")))
run_case("console-applied-only",
         _seq(NOTHING, lambda: write(ENV["DOGANY_DECISION_ACTIONS_FILE"],
                                     "## act-5 [applied]\ntarget: dec-1\n")))
run_case("en-full-plugin", plugin, lang="en")
run_case("en-ledger-only", _seq(EMPTY_TRANSCRIPT, NO_DECISIONS), lang="en-US")
run_case("why-with-board", why=WHY)
run_case("why-alone-is-no-trigger", NOTHING, why=WHY)
run_case("decisions-only", _seq(EMPTY_TRANSCRIPT,
                                _rm(ENV["DOGANY_LEDGER_FILE"])))

# Every golden file must still be produced by some case.
if not UPDATE:
    stale = sorted(set(os.listdir(GOLDEN_DIR)) - set(SEEN))
    for name in stale:
        FAILS.append(name)
        print("  FAIL: golden file no case produces: %s" % name)

shutil.rmtree(WORK, ignore_errors=True)
print("\n===========================")
print("Results: %d golden files, %d mismatched" % (len(SEEN), len(FAILS)))
print("===========================")
sys.exit(1 if FAILS else 0)
