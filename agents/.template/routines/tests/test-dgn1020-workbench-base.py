#!/usr/bin/env python3
# test-lane: fast
# test-component: status-footer
# test-tier: 0
# promise: a local plugin named by conf DASHBOARD_BOARD_PLUGIN loads and renders a bold title with its rows inside one quote
# promise: a local plugin that wraps the kit plugin keeps the kit sections, hide list and ok, and survives a failing kit
# promise: the workbench-plugin skill states the real default path, key rule and hideable keys, and is wired into layout.md and the template
"""DGN-1020 Rev 15 -- the canonical workbench base every agent runs.

Owner decision 2026-09-26 ("공통 기능은 정본으로"): improvements two hand forks
of status-footer.py had grown separately live HERE; only dev-only sections
ride a board plugin.  This suite pins the base contract, one group per
promise:

  W1  live-row shape for a dispatched (ledger) row -- the owner's complaint:
      "<Model>: <name> · HH:MM (N분~)", model from the note, version from
      the run's own stream, start from last_ts
  W2  no model recorded -> no prefix (never a placeholder), time kept
  W3  subagent rows: model from meta.json + the agent's own jsonl, start =
      spawn; background rows keep their own stamp (never doubled)
  W4  LIVE_SHOW_MODEL: default on, "off" opts out (time still shown)
  W5  another day's start -> "MM/DD HH:MM"; a long name never eats the stamp
  W6  event collection: attachment / queue-operation completion, quoted
      notices ignored, resumedAgentId resume
  W7  ticket resolver: TICKET_FILE_GLOB, reserved "_" files, prose ids,
      token-bounded mentions, recent-done = today only
  W8  decision folds: detail file -> fold; none -> bullet; the length cut
      drops bodies first, visibly
  W9  inline freshness stamp + stamped-title adoption (no duplicate board)
  W10 board plugin seam: absent / valid / hide / trigger / pinned /
      variants / raising / wrong API / bad sections
  W11 main() end-to-end with a plugin: the board is written, a failing
      plugin never lets a confirmed-empty clear hide the pin
  W13 compose order (Rev 16, DGN-1020 owner 2026-09-26): board FIRST,
      domain sections after; board-empty, domain-empty, both-empty
  W12 library API that bg-job-notice.py / subagent-stop-record.py import
  W14 _machine_name(): hardware model on macOS, short hostname elsewhere,
      cached, never raises (DGN-2116 r3)

Run:  /usr/bin/python3 routines/tests/test-dgn1020-workbench-base.py
Exit: 0 all pass, 1 any fail.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Inherited config would leak into the tmp-state replay.
for _k in [k for k in os.environ
           if k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))]:
    del os.environ[_k]

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
FOOTER_PY = os.path.join(ROUTINES, "status-footer.py")

WORK = tempfile.mkdtemp(prefix="dgn1020-base.")
os.environ["DOGANY_AGENT_CONF"] = os.path.join(WORK, "agent.conf")
open(os.environ["DOGANY_AGENT_CONF"], "w").close()
os.environ["DOGANY_BOT_DATA_DIR"] = os.path.join(WORK, "bot")
os.environ["DOGANY_BOARD_PLUGIN"] = "none"
for k in ("DOGANY_LIVE_SHOW_MODEL", "DOGANY_LIVE_LABEL", "DOGANY_BOARD_EMOJI",
          "DOGANY_CONSOLE_BASE", "TICKET_FILE_GLOB", "DOGANY_WORKLOG_DIR"):
    os.environ.pop(k, None)


def load():
    spec = importlib.util.spec_from_file_location("status_footer", FOOTER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sf = load()

PASS = 0
FAIL = 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS: %s" % desc)
    else:
        FAIL += 1
        print("  FAIL: %s%s" % (desc, (" -- " + detail) if detail else ""))


def hm(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    os.utime(path, (NOW if mtime is None else mtime,) * 2)


# DGN-1953: pin the clock to local noon so START (NOW-15min) never crosses
# midnight; the product reads time.time(), so patch it. The cross-day case
# stays covered by W5 (yday = NOW-26h).
_lt = time.localtime()
NOW = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 12, 0, 0, 0, 0, -1))
time.time = lambda: NOW
START = NOW - 15 * 60
START_TS = time.strftime("%Y-%m-%d %H:%M KST", time.localtime(START))

# ---------------------------------------------------------------------------
print("\n--- W1: dispatched (ledger) row -- the owner's complaint ---")
ledger = os.path.join(WORK, "ledger.md")
dispatch = os.path.join(WORK, "dispatch")
os.environ["DOGANY_LEDGER_FILE"] = ledger
os.environ["DOGANY_DISPATCH_DIR"] = dispatch
# DGN-1730: the review signal counts a pending-review row only while its
# branch is unmerged -- a scratch instance repo holds one of each.
_repo = os.path.join(WORK, "inst")
_genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
for _args in (["init", "-q", "-b", "main"],
              ["commit", "-q", "--allow-empty", "-m", "root"],
              ["branch", "dsp/merged1"],
              ["checkout", "-q", "-b", "dsp/review1"],
              ["commit", "-q", "--allow-empty", "-m", "review1"],
              ["checkout", "-q", "main"]):
    os.makedirs(_repo, exist_ok=True)
    subprocess.run(["git", "-C", _repo] + _args, check=True,
                   capture_output=True, env=_genv)
os.environ["DOGANY_INSTANCE_ROOT"] = _repo
os.environ["DOGANY_REPO_ROOT"] = _repo
os.environ["DOGANY_LEDGER_REVIEW_CACHE"] = os.path.join(WORK, "review.json")
write(ledger,
      "| branch | item | state | attempts | backoff_until | last_ts | flags | note |\n"
      "|---|---|---|---|---|---|---|---|\n"
      "| dsp/rehearsal | 리허설 원칙 반영 + 남은 정리 | running | 0 | - | %s | - | "
      "dispatch 스폰 (model=opus runtime=claude-code axis=judgment) |\n"
      "| dsp/nomodel | 모델 없는 행 | running | 0 | - | %s | - | hand note |\n"
      "| dsp/review1 | 검토 대기 작업 [DGN-9001] | pending-review | 0 | - | %s | - | done |\n"
      "| dsp/merged1 | 이미 병합된 작업 [DGN-9002] | pending-review | 0 | - | %s | - | done |\n"
      % (START_TS, START_TS, START_TS, START_TS))
# The run reported its model id in its own stream (DGN-1227 observation).
rundir = os.path.join(dispatch, "dsp-20260926-134700-0001")
write(os.path.join(rundir, "run.env"), "LEDGER_BRANCH=dsp/rehearsal\n")
write(os.path.join(rundir, "stream.jsonl"),
      json.dumps({"type": "assistant",
                  "message": {"model": "claude-opus-5-5"}}) + "\n")
detail = {}
labels = sf._collect_ledger_running(detail)
rows = sf._format_live_items(labels, detail["records"])
want = "Claude Opus 5.5: 리허설 원칙 반영 + 남은 정리: %s (15분~)" % hm(START)
check("ledger row renders '<Model>: <name> · HH:MM (N분~)'",
      rows[:1] == [want], repr(rows))
check("records align 1:1 with the labels",
      len(detail["records"]) == len(labels))
check("unmerged pending-review rows are exposed for a plugin (never live "
      "rows; merged ones dropped, DGN-1730)",
      detail["review"] == ["dsp/review1 검토 대기 작업 [DGN-9001]"]
      and all("검토 대기" not in l for l in labels), repr(detail["review"]))

print("\n--- W2: no model recorded -> no prefix, never a placeholder ---")
check("model-less row keeps the time, no prefix",
      rows[1:2] == ["모델 없는 행: %s (15분~)" % hm(START)], repr(rows))
check("no placeholder word anywhere", all("모델미상" not in r for r in rows))

# ---------------------------------------------------------------------------
print("\n--- W3: subagent rows (meta + own jsonl), background rows ---")
home = os.path.join(WORK, "home")
proj = os.path.join(home, ".claude", "projects", "enc")
sess = "sess-0001"
subdir = os.path.join(proj, sess, "subagents")
transcript = os.path.join(proj, sess + ".jsonl")
os.makedirs(subdir, exist_ok=True)
os.environ["HOME"] = home
sf = load()  # _subagents_dir derives from HOME


def launch(aid):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text":
         "Async agent launched successfully.\nagentId: %s (x)" % aid}]}]}})


def compl_user(aid):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "text", "text": "<task-notification>\n<task-id>%s</task-id>"
         "\n<status>completed</status>" % aid}]}})


spawn = NOW - 7 * 60
write(os.path.join(subdir, "agent-aaa111.meta.json"),
      json.dumps({"description": "제목 정리", "model": "sonnet"}), spawn)
write(os.path.join(subdir, "agent-aaa111.jsonl"),
      json.dumps({"type": "assistant",
                  "message": {"model": "claude-sonnet-5"}}) + "\n")
write(os.path.join(subdir, "agent-bbb222.meta.json"),
      json.dumps({"description": "모델 기록 없음"}), spawn)
write(os.path.join(subdir, "agent-bbb222.jsonl"),
      json.dumps({"type": "assistant", "message": {}}) + "\n")
bg_start = NOW - 3 * 60
bg_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(bg_start)) + "Z"
bash_use = json.dumps({"type": "assistant", "timestamp": bg_iso, "message": {
    "content": [{"type": "tool_use", "id": "toolu_bg", "name": "Bash",
                 "input": {"command": "pytest -q", "description": "테스트 실행",
                           "run_in_background": True}}]}})
bash_ack = json.dumps({"type": "user", "timestamp": bg_iso, "message": {
    "content": [{"type": "tool_result", "tool_use_id": "toolu_bg",
                 "content": "Command running in background with ID: bgjob1"}]}})
write(transcript, "\n".join([launch("aaa111"), launch("bbb222"), bash_use,
                             bash_ack]) + "\n")
sd = {}
descs = sf._collect_active_subagents(transcript, {}, sd)
live = sf._format_live_items(descs, sd["records"])
check("subagent row: observed id wins -> 'Claude Sonnet 5: ... · HH:MM (7분~)'",
      "Claude Sonnet 5: 제목 정리: %s (7분~)" % hm(spawn) in live, repr(live))
check("subagent without any model record: name + time, no prefix",
      "모델 기록 없음: %s (7분~)" % hm(spawn) in live, repr(live))
bg_rows = [r for r in live if "테스트 실행" in r]
check("background row keeps its own single stamp",
      len(bg_rows) == 1 and bg_rows[0].count(": ") == 1
      and bg_rows[0].endswith("(3분~)"), repr(bg_rows))
check("detail records align with descs (None for the background row)",
      len(sd["records"]) == len(descs) and sd["records"][-1] is None)

# ---------------------------------------------------------------------------
print("\n--- W4: LIVE_SHOW_MODEL default on; dispatch rows retain their model ---")
check("default is on (owner 2026-09-26)", sf.LIVE_SHOW_MODEL is True)
write(os.environ["DOGANY_AGENT_CONF"], "DASHBOARD_LIVE_SHOW_MODEL=off\n")
sf_off = load()
d2 = {}
off_rows = sf_off._format_live_items(sf_off._collect_ledger_running(d2),
                                     d2["records"])
check("conf off -> dispatched model remains, time still shown",
      off_rows[0] == "Claude Opus 5.5: 리허설 원칙 반영 + 남은 정리: %s (15분~)" % hm(START),
      repr(off_rows))
open(os.environ["DOGANY_AGENT_CONF"], "w").close()

# ---------------------------------------------------------------------------
print("\n--- W5: other-day start, long names ---")
yday = NOW - 26 * 3600
pre, suf = sf._live_stamp("opus", yday)
check("lowercase alias capitalized", pre == "Opus")
check("another day's start carries the date",
      suf.startswith(": " + time.strftime("%m/%d %H:%M",
                                                time.localtime(yday))), suf)
# DGN-1967 S2b: background and dispatch rows follow the same cross-day rule
# (they used to print a bare HH:MM, so a job kept across midnight read as
# if it had started tonight).
ymd = time.strftime("%m/%d %H:%M", time.localtime(yday))
bg_y = sf._background_row("bash", "밤샘 작업", yday)
check("background row started on another day reads 'MM/DD HH:MM'",
      bg_y == "밤샘 작업: %s (1560분~)" % ymd, bg_y)
bg_t = sf._background_row("bash", "오늘 작업", START)
check("... and today's background row keeps the bare time",
      bg_t == "오늘 작업: %s (15분~)" % hm(START), bg_t)
_ydir = os.path.join(dispatch, "dsp-20260925-000000-0002")
write(os.path.join(_ydir, "run.env"),
      "LEDGER_BRANCH=dsp/yday\nSPAWN_EPOCH=%d\n" % int(yday))
d_row, d_start = sf._dispatch_row("Claude Opus 5.5", "dsp/yday", "어제 시작",
                                  None)
check("dispatch row spawned on another day reads 'MM/DD HH:MM'",
      d_row == "Claude Opus 5.5: 어제 시작: %s (1560분~)"
      % time.strftime("%m/%d %H:%M", time.localtime(int(yday)))
      and d_start is None, d_row)
shutil.rmtree(_ydir)
long_rows = sf._format_live_items(
    ["가" * 400], [{"desc": "가" * 400, "model_display": "Claude Opus 5.5",
                    "start_epoch": START}])
check("a long name is cut, the stamp survives whole",
      long_rows[0].endswith(": %s (15분~)" % hm(START))
      and sf._u16len(long_rows[0]) <= sf.DASHBOARD_ITEM_MAX_UNITS,
      long_rows[0][-30:])
check("record/desc count mismatch never stamps the wrong row",
      sf._format_live_items(["a", "b"], [{"desc": "zzz", "model_display": "X",
                                          "start_epoch": START}])
      == ["a", "b"])

# ---------------------------------------------------------------------------
print("\n--- W6: event collection ---")
att = json.dumps({"type": "attachment", "attachment": {
    "commandMode": "task-notification",
    "prompt": "<task-notification>\n<task-id>aaa111</task-id>\n"
              "<status>completed</status>"}})
quoted = json.dumps({"type": "queue-operation", "content":
                     "please look: <task-notification><task-id>bbb222"
                     "</task-id><status>completed</status>"})
write(transcript, "\n".join([launch("aaa111"), launch("bbb222"), att,
                             quoted]) + "\n")
d = sf._collect_active_subagents(transcript)
check("attachment completion ends the agent", "제목 정리" not in d, repr(d))
check("a queued prompt QUOTING a notice ends nothing", "모델 기록 없음" in d,
      repr(d))
resume2 = json.dumps({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": json.dumps(
        {"success": True, "resumedAgentId": "aaa111"})}]}})
write(transcript, "\n".join([launch("aaa111"), compl_user("aaa111"),
                             resume2]) + "\n")
check("resumedAgentId brings a completed agent back",
      "제목 정리" in (sf._collect_active_subagents(transcript) or []))

# ---------------------------------------------------------------------------
print("\n--- W7: ticket resolver ---")
wl = os.path.join(WORK, "worklog")
os.environ["DOGANY_WORKLOG_DIR"] = wl
write(os.path.join(wl, "DGN-9001-fix.md"),
      "---\nid: DGN-9001\ntitle: fix it\ndisplay_title.en: Fix\n"
      "status: done\n---\n")
write(os.path.join(wl, "2026-09-15-ag-mirror.md"),
      "---\nid: (미부여 -- 날짜형이 식별자)\ntitle: 미러\nstatus: done\n---\n")
write(os.path.join(wl, "_DECISIONS.md"), "---\nstatus: done\n---\n")
write(os.path.join(wl, "DGN-9002-old.md"),
      "---\nid: DGN-9002\ntitle: 옛일\nstatus: done\n---\n", NOW - 3 * 86400)
names = [n for n, _p, _f in sf._iter_worklog_tickets(False)]
check("default glob = DGN-*.md (pre-Rev-15 scan unchanged)",
      names == ["DGN-9001-fix.md", "DGN-9002-old.md"], repr(names))
os.environ["TICKET_FILE_GLOB"] = "*.md"
names = [n for n, _p, _f in sf._iter_worklog_tickets(False)]
check("TICKET_FILE_GLOB=*.md admits date slugs, never '_' ledgers",
      "2026-09-15-ag-mirror.md" in names and "_DECISIONS.md" not in names,
      repr(names))
check("a prose id falls back to the filename stem",
      sf._ticket_id("2026-09-15-ag-mirror.md",
                    {"id": "(미부여 -- 날짜형이 식별자)"})
      == "2026-09-15-ag-mirror")
check("a token id is kept", sf._ticket_id("x.md", {"id": "DGN-9001"})
      == "DGN-9001")
done = sf._collect_recent_done(limit=5)
check("recent done = today's completions only (date slug included)",
      any(r == "DGN-9001 Fix" for r in done)
      and "2026-09-15-ag-mirror" in done
      and not any("DGN-9002" in r for r in done), repr(done))
del os.environ["TICKET_FILE_GLOB"]
mt = sf._mentions_ticket
check("DGN-95 never matches inside DGN-955", not mt(["DGN-955 x"], "DGN-95"))
check("a numeric id matches a branch slug", mt(["auto/dgn-541-fix"], "DGN-541"))
check("a date slug never matches a longer slug",
      not mt(["2026-08-25-incident-extra"], "2026-08-25-incident"))
check("a date slug matches as a token",
      mt(["[2026-08-25-incident] 작업"], "2026-08-25-incident"))

# ---------------------------------------------------------------------------
print("\n--- W8: decision folds ---")
decf = os.path.join(WORK, "decisions.md")
decdir = os.path.join(WORK, "decisions")
os.environ["DOGANY_DECISIONS_FILE"] = decf
os.environ["DOGANY_DECISIONS_DIR"] = decdir
write(decf, "- [2026-09-26] [dec-900] 착지 순서\n- [2026-09-26] 아이디 없는 결정\n")
write(os.path.join(decdir, "dec-900.md"),
      "# dec-900\n## 질문\n어느 쪽 먼저?\n## Skull 권고\n검토 에이전트 먼저\n"
      "## 옵션\n1. 검토 에이전트\n2. 샘플 에이전트\n```\n## 배경\n무시되는 절\n")
items = sf._collect_decision_items()
check("detail body = question + recommendation + options, aliases honoured",
      items[0]["body"] == "물음: 어느 쪽 먼저?\n권고: 검토 에이전트 먼저\n선택지:\n"
      "1. 검토 에이전트\n2. 샘플 에이전트\n'''", repr(items[0]["body"]))
check("text view is the pre-Rev-15 string list",
      sf._collect_decisions() == ["dec-900 착지 순서", "아이디 없는 결정"])
board = sf._build_dashboard(items, [], [])
check("a decision with a body renders as one fold",
      "fold:: dec-900 착지 순서\n물음: 어느 쪽 먼저?" in board
      and "fold::end" in board, board)
check("a decision without a body stays a bullet", "- 아이디 없는 결정" in board)
big = [{"text": "d%d" % i, "body": "x" * 600} for i in range(8)]
cut = sf._build_dashboard(big, ["live"], [])
check("length cut drops fold bodies first, visibly",
      sf._u16len(cut) <= sf.DASHBOARD_MAX_UNITS
      and "d0" + sf.DEC_BODY_DROPPED_MARK in cut
      and "quote::\nlive\nquote::end" in cut)

# ---------------------------------------------------------------------------
print("\n--- W9: inline stamp + stamped-title adoption ---")
b = sf._build_dashboard(["x"], [], [])
first = b.splitlines()[0]
check("stamp rides the title line (DGN-2116: emoji? + MM/DD(요일) HH:MM)",
      sf._TOP_LINE_RE.match(first) and "작업대" not in first, first)
check("no trailing stamp line any more",
      not b.rstrip("\n").splitlines()[-1].startswith("갱신 "))
domain_secs = {"d": {"text": "DOMAIN"}}
check("_is_own_domain_tail recognises our own domain as the tail of a "
      "board-first crash residue",
      sf._is_own_domain_tail(domain_secs, b + "\nDOMAIN"))
check("... but not when the trailing text differs from the current domain",
      not sf._is_own_domain_tail(domain_secs, b + "\nSOMETHING-ELSE"))
check("_is_bare_board_head recognises the stamped title with nothing after",
      sf._is_bare_board_head(b))
check("... and the legacy bare (unstamped) title too",
      sf._is_bare_board_head(sf.BOARD_TITLE_TEXT + "\n\nx"))
check("... but not genuine foreign content",
      not sf._is_bare_board_head("DOMAIN\n- x"))
# DGN-1967 S8: board copy follows AGENT_LANG; a board written in the other
# language (an en instance's pre-S8 "작업대" board) is still ours.
os.environ["DOGANY_LANG"] = "en"
sf_en = load()
del os.environ["DOGANY_LANG"]
check("en board top line carries an English weekday, no title word",
      sf_en._TOP_LINE_RE.match(sf_en._build_dashboard(["x"], [], []).splitlines()[0])
      and "Workbench" not in sf_en._build_dashboard(["x"], [], [])
      .splitlines()[0], sf_en.BOARD_TITLE)
check("en: a stale Korean-stamped board is ours, never adopted",
      sf_en._is_bare_board_head("작업대 (갱신 09:41)\n\n[라이브]\n- a")
      and sf_en._is_bare_board_head("Workbench (updated 09:41)\n\n- a"))
check("ko: an English-stamped board is ours too",
      sf._is_bare_board_head("Workbench (updated 09:41)\n\n- a"))

unp = os.path.join(WORK, "unpark.md")
write(unp, "# header\nlast-scan: %s\ncandidates: 0\n\n(후보 없음)\n"
      % time.strftime("%Y-%m-%d %H:%M:%S"))
os.environ["DOGANY_UNPARK_FILE"] = unp
u = sf._collect_unpark()
check("unpark 'candidates: 0' placeholder is not a candidate (section stays"
      " hidden while fresh)", u[2] == [] and not sf._unpark_visible(u),
      repr(u))
write(unp, "last-scan: %s\ncandidates: 1\nDGN-1  P2  UNPARK  gate: ok\n"
      % time.strftime("%Y-%m-%d %H:%M:%S"))
check("a real candidate still shows", sf._collect_unpark()[2] ==
      ["DGN-1 P2 UNPARK gate: ok"])

# ---------------------------------------------------------------------------
print("\n--- W10: board plugin seam ---")
plug = os.path.join(WORK, "plugin.py")


def plugin_src(body, api=1):
    write(plug, "PLUGIN_API = %d\n%s\n" % (api, body))


os.environ["DOGANY_BOARD_PLUGIN"] = plug
check("configured but missing file -> no plugin, ok",
      sf._load_board_plugin() == (None, True))
os.environ["DOGANY_BOARD_PLUGIN"] = "none"
check("'none' disables", sf._load_board_plugin() == (None, True))
os.environ["DOGANY_BOARD_PLUGIN"] = plug

plugin_src("def build_sections(ctx):\n"
           "    assert ctx['host']._cap_item\n"
           "    return {'ok': True, 'hide': ['recent_done', 'bogus'], 'sections': [\n"
           "      {'key': 'tickets', 'trigger': True, 'variants': [\n"
           "         ['[T] 3건', 'r1', 'r2', 'r3'], ['[T] 3건', 'r1', '외 2건']]},\n"
           "      {'key': 'products', 'pinned': True, 'lines': ['[P]', '- p: v1\\n  line2']},\n"
           "      {'key': 'BAD KEY', 'lines': ['x']},\n"
           "      {'key': 'empty', 'lines': []},\n"
           "      {'key': 'tickets', 'lines': ['dup']}]}\n")
mod, ok = sf._load_board_plugin()
check("valid plugin loads", mod is not None and ok)
ctx = sf._plugin_ctx(["L"], ["L"], [None], [], [], [])
secs, hidden, trig, sok = sf._plugin_sections(mod, ctx)
check("invalid / empty / duplicate sections dropped, valid kept",
      [s["key"] for s in secs] == ["tickets", "products"], repr(secs))
check("hide keeps only hideable base keys", hidden == ("recent_done",))
check("trigger and ok reported", trig is True and sok is True)
check("an embedded newline becomes its own line",
      secs[1]["variants"][0] == ["[P]", "- p: v1", "  line2"])
board = sf._build_dashboard(["dec"], ["- live row"], ["DONE-1"], None, None,
                            secs, hidden)
order = [board.find(x) for x in ("[결정대기]", "live row", "[T] 3건", "[P]")]
check("plugin sections render after the live section, in order",
      order == sorted(order) and -1 not in order, board)
check("hidden base section is gone", "DONE-1" not in board)
big_rows = [{"key": "tickets", "pinned": False, "variants": [
    ["[T] 3건"] + ["r%d%s" % (i, "." * 298) for i in (1, 2, 3)],
    ["[T] 3건", "r1", "외 2건"]]}]
base_len = sf._u16len(sf._build_dashboard(["dec"], [], []))
n = (sf.DASHBOARD_MAX_UNITS - base_len - 200) // 283
filler = ["x" * 280 for _ in range(n)]
tight = sf._build_dashboard(["dec"], filler, [], None, None, big_rows, ())
check("length cut steps a section down its variants before live rows",
      "외 2건" in tight and "r3." not in tight
      and len([l for l in tight.splitlines() if l.startswith("xxx")]) == n
      and sf._u16len(tight) <= sf.DASHBOARD_MAX_UNITS, tight[-200:])
huge = ["y" * 290 for _ in range(14)]
tighter = sf._build_dashboard(["dec"], huge, [], None, None, secs, hidden)
check("pinned section survives the cut; unpinned one leaves",
      "[P]" in tighter and "[T] 3건" not in tighter
      and sf._u16len(tighter) <= sf.DASHBOARD_MAX_UNITS)

plugin_src("def build_sections(ctx):\n    raise RuntimeError('boom')\n")
mod, ok = sf._load_board_plugin()
check("raising plugin -> no sections, ok False",
      sf._plugin_sections(mod, ctx)[0] == [] and
      sf._plugin_sections(mod, ctx)[3] is False)
plugin_src("def build_sections(ctx):\n    return []\n", api=2)
check("wrong PLUGIN_API -> not loaded, ok False",
      sf._load_board_plugin() == (None, False))
write(plug, "this is not python(\n")
check("import error -> not loaded, ok False",
      sf._load_board_plugin() == (None, False))
plugin_src("def build_sections(ctx):\n    return ['not a dict']\n")
mod, _ok = sf._load_board_plugin()
check("non-dict result -> ok False", sf._plugin_sections(mod, ctx)[3] is False)
plugin_src("def build_sections(ctx):\n    return {'ok': False, 'sections': []}\n")
mod, _ok = sf._load_board_plugin()
check("plugin-declared collector failure propagates",
      sf._plugin_sections(mod, ctx)[3] is False)

# ---------------------------------------------------------------------------
print("\n--- W11: main() end-to-end with a plugin ---")
botdir = os.environ["DOGANY_BOT_DATA_DIR"]
os.makedirs(botdir, exist_ok=True)
sessions = os.path.join(botdir, "sessions.json")
write(sessions, json.dumps({"main": {"session_id": sess}}))
os.environ["DOGANY_SESSIONS_FILE"] = sessions
write(decf, "")
os.environ["DOGANY_LEDGER_FILE"] = os.path.join(WORK, "no-ledger.md")
os.environ["DOGANY_UNPARK_FILE"] = os.path.join(WORK, "no-unpark.md")
write(transcript, "")
dash = os.path.join(botdir, "dashboard.md")


def run_main():
    inp = json.dumps({"session_id": sess, "transcript_path": transcript,
                      "hook_event_name": "Stop"})
    r = subprocess.run([sys.executable, FOOTER_PY], input=inp, text=True,
                       capture_output=True, env=dict(os.environ))
    try:
        return r.returncode, open(dash).read()
    except OSError:
        return r.returncode, None


plugin_src("def build_sections(ctx):\n"
           "    return {'ok': True, 'sections': [{'key': 'tickets', "
           "'trigger': True, 'lines': ['[진행중 티켓]', '- A']}]}\n")
rc, text = run_main()
check("plugin trigger alone puts the board up",
      rc == 0 and text and "[진행중 티켓]" in text, repr(text))
plugin_src("def build_sections(ctx):\n    raise ValueError('x')\n")
rc, text = run_main()
check("raising plugin + nothing else: board NOT cleared (empty-ban)",
      rc == 0 and text and "[진행중 티켓]" in text, repr(text))
plugin_src("def build_sections(ctx):\n    return {'ok': True, 'sections': []}\n")
rc, text = run_main()
check("healthy plugin with nothing to show: confirmed empty -> cleared",
      rc == 0 and text == "", repr(text))
os.environ["DOGANY_BOARD_PLUGIN"] = "none"

# ---------------------------------------------------------------------------
print("\n--- W13: compose order -- board FIRST, domain after (Rev 16) ---")
board_only_secs = {"board": {"text": "TITLE\n\n[live]\n- x\n"}}
domain_only_secs = {"a": {"text": "DOMAIN-A"}, "b": {"text": "DOMAIN-B"}}
both_secs = dict(board_only_secs); both_secs.update(domain_only_secs)

composed_both = sf._compose_text(both_secs)
check("board+domain: board renders first", composed_both.startswith("TITLE"),
      repr(composed_both))
check("board+domain: domain follows the board, owner-slug ordered",
      composed_both.index("TITLE") < composed_both.index("DOMAIN-A")
      < composed_both.index("DOMAIN-B"), repr(composed_both))
check("board+domain: exactly one blank line separates the two blocks",
      "- x\n\nDOMAIN-A" in composed_both, repr(composed_both))

composed_board_only = sf._compose_text(board_only_secs)
check("board-only (domain empty): unchanged, no blank lead line",
      composed_board_only == "TITLE\n\n[live]\n- x\n", repr(composed_board_only))

composed_domain_only = sf._compose_text(domain_only_secs)
check("domain-only (board empty): unchanged, no board tail",
      composed_domain_only == "DOMAIN-A\n\nDOMAIN-B\n", repr(composed_domain_only))

check("both-empty -> '' (bridge hide signal)", sf._compose_text({}) == "")
check("both-empty with blank-text sections -> '' too",
      sf._compose_text({"board": {"text": ""}, "a": {"text": "  "}}) == "")

# ---------------------------------------------------------------------------
print("\n--- W12: library API kept for importers ---")
for name in ("_is_owner_session", "_record_agent_stop", "_stop_record",
             "_stop_record_path", "_collect_active_subagents",
             "_collect_ledger_running", "_scan_transcript",
             "_resolve_description", "_board_publish", "_board_clear",
             "_model_display_from_note", "_build_dashboard", "_conf_get",
             "_ticket_frontmatter", "STALLED_LABEL", "LIVE_STALE_SECS"):
    check("status-footer exposes %s" % name, hasattr(sf, name))
check("plugin code never runs on a library import (no module-level load)",
      "dogany_board_plugin" not in sys.modules)

# ---------------------------------------------------------------------------
print("\n--- W14 (DGN-2116 r3): _machine_name() -- hardware model on macOS, "
      "short hostname on Linux/unknown, cached, never raises ---")
# Sources (system_profiler, hostname, platform) are stubbed on fresh module
# instances; the shared env is restored afterwards.
_mn_env = os.environ.get("DOGANY_BOT_DATA_DIR")


def load_mn(data_dir):
    os.environ["DOGANY_BOT_DATA_DIR"] = data_dir
    return load()


def check_mn(label, got, want):
    check("W14 " + label, got == want, "%r != %r" % (got, want))


class Run:
    def __init__(self, out="", boom=False):
        self.stdout, self.boom = out, boom


SP = "Hardware:\n\n    Hardware Overview:\n\n      Model Name: Mac mini\n      Chip: M2\n"


def stub_run(mn, out=SP, boom=False):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        if boom:
            raise OSError("no system_profiler")
        return Run(out)
    mn.subprocess.run = fake
    return calls


with tempfile.TemporaryDirectory() as d:
    # macOS branch: model name, cached on disk + in process
    mn = load_mn(os.path.join(d, "a"))
    mn.sys = type("S", (), {"platform": "darwin"})
    mn.socket.gethostname = lambda: "Siwons-Mac-mini.local"
    calls = stub_run(mn)
    check_mn("darwin", mn._machine_name(), "Mac mini")
    check_mn("darwin cached", mn._machine_name(), "Mac mini")
    check_mn("one probe", len(calls), 1)
    mn2 = load_mn(os.path.join(d, "a"))
    mn2.sys = mn.sys
    calls2 = stub_run(mn2, boom=True)
    check_mn("disk cache reused", mn2._machine_name(), "Mac mini")
    check_mn("no probe on disk hit", len(calls2), 0)

    # macOS probe failure -> short hostname (never the long form)
    mn = load_mn(os.path.join(d, "b"))
    mn.sys = type("S", (), {"platform": "darwin"})
    mn.socket.gethostname = lambda: "Host-1.local"
    stub_run(mn, boom=True)
    check_mn("darwin fail", mn._machine_name(), "Host-1")
    mn = load_mn(os.path.join(d, "c"))
    mn.sys = type("S", (), {"platform": "darwin"})
    mn.socket.gethostname = lambda: "Host-2.local"
    stub_run(mn, out="nothing useful")
    check_mn("darwin empty", mn._machine_name(), "Host-2")

    # Linux branch: short hostname, no probe
    mn = load_mn(os.path.join(d, "e"))
    mn.sys = type("S", (), {"platform": "linux"})
    mn.socket.gethostname = lambda: "box.example.com"
    calls = stub_run(mn)
    check_mn("linux", mn._machine_name(), "box")
    check_mn("linux no probe", len(calls), 0)

    # hostname itself raising -> ""
    mn = load_mn(os.path.join(d, "f"))
    mn.sys = type("S", (), {"platform": "freebsd"})

    def bad():
        raise OSError("x")
    mn.socket.gethostname = bad
    check_mn("never raises", mn._machine_name(), "")

if _mn_env is None:
    os.environ.pop("DOGANY_BOT_DATA_DIR", None)
else:
    os.environ["DOGANY_BOT_DATA_DIR"] = _mn_env

# ---------------------------------------------------------------------------
print("\n--- W15 (DGN-2221): local board plugin -- the documented how-to ---")
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(ROUTINES)))
SKILL = os.path.join(REPO, "skills", "dogany-workbench-plugin", "SKILL.md")
skill_text = open(SKILL).read()
snippets = re.findall(r"```python\n(.*?)```", skill_text, re.S)
check("skill carries the two documented plugin examples", len(snippets) == 2,
      repr(len(snippets)))
local_src, wrap_src = snippets[0], snippets[1]

# P1: the plugin sits in the instance-owned home, named by the conf key only.
inst = os.path.join(WORK, "w15-inst")
home_rel = os.path.join("data", "board-plugin", "local_sections.py")
write(os.path.join(inst, home_rel), local_src)
conf = os.environ["DOGANY_AGENT_CONF"]
saved_env = os.environ.pop("DOGANY_BOARD_PLUGIN", None)
write(conf, "DASHBOARD_BOARD_PLUGIN=%s\n" % home_rel)
saved_root = sf._ROOT_DIR
sf._ROOT_DIR = inst
mod, ok = sf._load_board_plugin()
check("conf key (relative to the instance root) loads the local plugin",
      mod is not None and ok)
ctx = sf._plugin_ctx([], [], [], [], [], [])
secs, hidden, _t, sok = sf._plugin_sections(mod, ctx)
check("the example section is accepted", [x["key"] for x in secs] ==
      ["master_plan"] and sok is True, repr(secs))
board = sf._build_dashboard(["dec"], ["- live row"], [], None, None, secs,
                            hidden)
lines = board.splitlines()
i = lines.index("**[Master plan]**")
check("bold title line, then quote:: rows quote::end (not re-wrapped)",
      lines[i + 1:i + 5] == ["quote::", "rolling plan A", "rolling plan B",
                             "quote::end"], board)

# P2: wrapping the kit plugin (the pack default path) instead of replacing it.
kit = os.path.join(inst, sf.BOARD_PLUGIN_FILE)
write(kit, "PLUGIN_API = 1\n"
      "def build_sections(ctx):\n"
      "    return {'ok': True, 'hide': ['recent_done'], 'sections': [\n"
      "      {'key': 'kit_tickets', 'lines': ['[KIT]', '- k1']}]}\n")
write(os.path.join(inst, home_rel), wrap_src)
mod, ok = sf._load_board_plugin()
secs, hidden, _t, sok = sf._plugin_sections(mod, ctx)
check("wrapper: kit section first, ours after, kit hide kept, ok kept",
      [x["key"] for x in secs] == ["kit_tickets", "master_plan"]
      and hidden == ("recent_done",) and sok is True, repr((secs, hidden)))
write(kit, "PLUGIN_API = 1\n"
      "def build_sections(ctx):\n    raise RuntimeError('kit down')\n")
secs, hidden, _t, sok = sf._plugin_sections(mod, ctx)
check("wrapper: a raising kit still shows our section, ok False",
      [x["key"] for x in secs] == ["master_plan"] and sok is False,
      repr((secs, sok)))
os.remove(kit)
secs, hidden, _t, sok = sf._plugin_sections(mod, ctx)
check("wrapper: no kit file is not a failure",
      [x["key"] for x in secs] == ["master_plan"] and sok is True)
write(os.path.join(inst, home_rel), local_src)
write(kit, "PLUGIN_API = 1\n"
      "def build_sections(ctx):\n"
      "    return {'sections': [{'key': 'kit_tickets', 'lines': ['[KIT]']}]}\n")
secs, _h, _t, _o = sf._plugin_sections(sf._load_board_plugin()[0], ctx)
check("pointing the conf key at a plain local plugin REPLACES the kit's "
      "(why the skill says to wrap)", [x["key"] for x in secs] ==
      ["master_plan"])

# P3: the skill's facts are the loader's facts.
check("skill names the pack default path", sf.BOARD_PLUGIN_FILE.replace(
    os.sep, "/") in skill_text)
check("skill states the section key rule", sf._PLUGIN_KEY_RE.pattern in
      skill_text)
check("skill lists exactly the hideable base keys",
      all("`%s`" % k in skill_text for k in sf.HIDEABLE_BASE_SECTIONS))
check("skill names the conf key and the pinned bound",
      "DASHBOARD_BOARD_PLUGIN" in skill_text
      and str(sf.PLUGIN_PINNED_MAX_UNITS) in skill_text)
layout = open(os.path.join(os.path.dirname(ROUTINES), "playbooks",
                           "layout.md")).read()
check("layout.md lists the instance-owned plugin home",
      "`data/board-plugin/` -- your" in layout
      and "dogany-workbench-plugin" in layout)
check("the template symlinks the skill (shipped to every install)",
      os.path.isfile(os.path.join(os.path.dirname(ROUTINES), ".claude",
                                  "skills", "dogany-workbench-plugin",
                                  "SKILL.md")))
sf._ROOT_DIR = saved_root
if saved_env is not None:
    os.environ["DOGANY_BOARD_PLUGIN"] = saved_env
write(conf, "")

shutil.rmtree(WORK, ignore_errors=True)
print("\n===========================")
print("Results: %d passed, %d failed" % (PASS, FAIL))
print("===========================")
sys.exit(1 if FAIL else 0)
