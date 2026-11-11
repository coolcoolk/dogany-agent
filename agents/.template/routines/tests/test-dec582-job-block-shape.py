#!/usr/bin/env python3
# test-lane: fast
# test-component: status-footer
# test-tier: 1
# test-prereq: tmp-files
# test-new-component: the dec-582 job-block row shape is a new board contract; test-wb-quote-areas owns quote layout, not row wording
"""dec-582: workbench background job block shape.

  [<ticket>] <name> <HH:MM> (<N>m~)
  • <actor>: <model>[ (<place tag>)]

Covers ticket present / absent / no start, the sibling term from env and
conf, script jobs, stalled rows, English, and prints a sample board.
Run:  python3 routines/tests/test-dec582-job-block-shape.py
"""
import importlib.util
import os
import shutil
import sys
import atexit
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
TEMPLATE = os.path.dirname(ROUTINES)

WORK = tempfile.mkdtemp(prefix="dec582.")
atexit.register(shutil.rmtree, WORK, True)
os.environ["DOGANY_AGENT_CONF"] = os.path.join(WORK, "agent.conf")
open(os.environ["DOGANY_AGENT_CONF"], "w").close()
os.environ["DOGANY_BOT_DATA_DIR"] = os.path.join(WORK, "bot")
os.environ["DOGANY_BOARD_PLUGIN"] = "none"
for k in ("DOGANY_SIBLING_TERM", "DOGANY_LANG", "DOGANY_LIVE_SHOW_MODEL"):
    os.environ.pop(k, None)

spec = importlib.util.spec_from_file_location(
    "status_footer", os.path.join(ROUTINES, "status-footer.py"))
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)
rb = sf.run_block

PASS = FAIL = 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS: " + desc)
    else:
        FAIL += 1
        print("  FAIL: %s %r" % (desc, detail))


NOW = 1_000_000_000
T0 = NOW - 6 * 60


def clock(epoch):
    import time
    return time.strftime("%H:%M", time.localtime(epoch))


def row(name, source="ledger", start=T0, state="live", tag="(unused)"):
    return sf.LiveRow(name, source, state=state, model="Claude Opus 5.5",
                      job_start=start, name=name, bare="Claude Opus 5.5",
                      tag="Cloud credit")


from unittest.mock import patch  # noqa: E402

with patch.object(sf.time, "time", return_value=NOW), \
     patch.object(sf, "BOARD_LANG", "ko"), \
     patch.object(sf, "_BOARD_STR", sf.BOARD_STRINGS["ko"]):
    c = clock(T0)
    print("== ticket present: bracket moves to the front, no time on line 2 ==")
    got = row("시험 방식 이전 설계 검증 [KDEV-56]").job_lines()
    check("ticket first", got[0] == "[KDEV-56] 시험 방식 이전 설계 검증 %s (6분~)" % c, got)
    check("actor line has model + tag, no time",
          got[1] == "• 서브에이전트: Claude Opus 5.5 (Cloud credit)", got)

    print("== ticket already leading stays put ==")
    got = row("[DGN-1726] 위젯 파서 정리").job_lines()
    check("leading ticket unchanged", got[0] == "[DGN-1726] 위젯 파서 정리 %s (6분~)" % c, got)

    print("== no ticket ==")
    got = row("시험 방식 이전 설계 검증").job_lines()
    check("no bracket", got[0] == "시험 방식 이전 설계 검증 %s (6분~)" % c, got)

    print("== no recorded start: time dropped ==")
    got = row("작업 [KDEV-56]", start=None).job_lines()
    check("no time", got[0] == "[KDEV-56] 작업", got)
    check("actor line same", got[1] == "• 서브에이전트: Claude Opus 5.5 (Cloud credit)", got)

    print("== long name: the ticket survives the cap ==")
    got = row("긴 이름 " * 80 + "[KDEV-56]").job_lines()
    check("long name keeps ticket in front", got[0].startswith("[KDEV-56] ") and "…" in got[0], got[0][:40])

    print("== script job keeps its own actor word ==")
    with patch.object(sf, "SIBLING_TERM", "서브에이전트"):
        got = sf.LiveRow("테스트 [KDEV-9]", "background", name="테스트 [KDEV-9]",
                         job_start=T0).job_lines()
    check("script line", got == ["[KDEV-9] 테스트 %s (6분~)" % c, "• 스크립트"], got)

    print("== stalled / dead rows unchanged ==")
    got = row("멈춘 작업 [KDEV-1] (무응답 20분+ -- 사망 추정)", state="dead").job_lines()
    check("dead row is one raw line", got == ["멈춘 작업 [KDEV-1] (무응답 20분+ -- 사망 추정)"], got)

    print("== sibling term: env > conf > catalog default ==")
    check("catalog default", row("a").job_lines()[1].startswith("• 서브에이전트: "))
    root = os.path.join(WORK, "root")
    shutil.copytree(os.path.join(TEMPLATE, "config", "i18n"),
                    os.path.join(root, "config", "i18n"))
    with open(os.path.join(root, "config", "agent.conf"), "w") as fh:
        fh.write("DASHBOARD_SIBLING_TERM=서브에이전트\n")
    with patch.object(sf, "_ROOT_DIR", root), patch.dict(
            os.environ, {"DOGANY_AGENT_CONF": os.path.join(root, "config", "agent.conf")}):
        got = row("a [KDEV-56]").job_lines()
        check("conf DASHBOARD_SIBLING_TERM", got[1] == "• 서브에이전트: Claude Opus 5.5 (Cloud credit)", got)
        with patch.dict(os.environ, {"DOGANY_SIBLING_TERM": "형제"}):
            check("env beats conf", row("a").job_lines()[1].startswith("• 형제: "))

    print("== English layout mirrors the same order ==")
    with patch.object(sf, "BOARD_LANG", "en"), \
         patch.object(sf, "_BOARD_STR", sf.BOARD_STRINGS["en"]):
        got = row("verify design [KDEV-56]").job_lines()
        check("en", got == ["[KDEV-56] verify design %s (6m~)" % c, "• subagent: Claude Opus 5.5 (Cloud credit)"]
              or (got[0].startswith("[KDEV-56] verify design " + c) and got[1].startswith("• subagent: ")), got)

    print("== sample board text ==")
    sample = (row("시험 방식 이전 설계 검증 [KDEV-56]").job_lines()
              + row("킷 정리").job_lines()
              + row("시작 모름 [KDEV-7]", start=None).job_lines()
              + sf.LiveRow("전체 테스트", "background", name="전체 테스트",
                           job_start=T0).job_lines())
    print("\n".join(sample))

print("RESULT: PASS=%d FAIL=%d" % (PASS, FAIL))
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAIL else 0)
