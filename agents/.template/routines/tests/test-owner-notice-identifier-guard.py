#!/usr/bin/env python3
# test-lane: fast
# test-covers: *    # tree scanner, measured 0.20s (DGN-1734): runs on every push
"""Owner-notice identifier guard -- detector contract + DRAFT-copy marker pin.

THE DEFECT this suite exists for. A detached routine sent the owner, verbatim:

    dispatch completed: <job-label> (runid dsp-20260911-110101-1006)

Internal mechanics (a run id, an internal job label) on the owner's phone, in
a language the instance does not speak to him. Eight builder sites carried the
same class of string; three controls all missed it (the machine-shape detector
only matches a line STARTING with an ALL-CAPS token, the JARGON detector in
routines/lib/output_gate.py is commented out, and the output gate runs as a
Stop-hook on assistant messages -- a detached shell push produces none).

Three things are pinned here, in the order they fail:

  A. DETECTOR CONTRACT -- bridge/machine_gate.find_execution_identifiers
     catches the reported string and every identifier shape the eight sites
     carried, and stays silent on copy that is already owner-APPROVED. The
     negative controls are the load-bearing half: a detector with false
     positives on bilingual prose gets switched off (that is exactly what
     happened to JARGON), and a switched-off detector is not a control.

  B. WIRING -- routines/push.sh, the last hop before the messaging API, calls
     the detector and REFUSES the send on a hit. A detector with no call site
     is not a control either.

  C. DRAFT-COPY MARKER -- every replacement string is owner-facing copy, and
     copy is owner-gated. Each one must carry the repo's pending-copy marker
     ("WORDING PENDING OWNER CONFIRMATION") in the comment block above it, so
     draft copy cannot graduate to approved by nobody noticing. Same
     convention, and the same reason, as the A10 block in
     test-footer-stopped-state.py.

  D. CONFIRMED-COPY RECORD -- the owner has since authored and confirmed two
     of these strings (2026-09-11). For those the assertion INVERTS: no
     pending marker, a dated owner-approval record instead. Leaving the
     marker on approved copy is the same class of defect read backwards --
     the next pass re-words what the owner already settled.

  E. ONE VOCABULARY, ONE REGISTER -- the same owner pass ruled out the
     two-words-for-one-object split in the backup copy (백업 only) and the
     formal/casual register mixing across the notice paths.

Run: python3 routines/tests/test-owner-notice-identifier-guard.py
Exit: 0 all pass, 1 any fail.
"""

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)
ROOT = os.path.dirname(ROUTINES)

PASS = 0
FAIL = 0


def ok(desc):
    global PASS
    PASS += 1
    print("  PASS: %s" % desc)


def bad(desc, detail=""):
    global FAIL
    FAIL += 1
    print("  FAIL: %s%s" % (desc, (" -- " + detail) if detail else ""))


def check(desc, cond, detail=""):
    if cond:
        ok(desc)
    else:
        bad(desc, detail)


def load_machine_gate():
    """Import the gate BY PATH: it is stdlib-only and importable outside the
    bridge venv on purpose (the push.sh hop runs it there), so this suite must
    not need the venv either."""
    path = os.path.join(ROOT, "bridge", "machine_gate.py")
    spec = importlib.util.spec_from_file_location("_mg_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


mg = load_machine_gate()
find = mg.find_execution_identifiers

# ---------------------------------------------------------------------------
# A. detector contract
# ---------------------------------------------------------------------------
# Each entry: (description, text, expected kind). Every text is a string this
# codebase really did put, or was one call away from putting, on the owner's
# phone -- the first is the reported incident verbatim.
POSITIVE = [
    ("the reported dispatch notice (run id)",
     "dispatch completed: callcrm-restructure-v2 "
     "(runid dsp-20260911-110101-1006)",
     "IDENT_RUN_ID"),
    ("a short commit hash",
     "dispatch done -- HEAD=4da96905", "IDENT_COMMIT_HASH"),
    ("a full commit hash",
     "9e8e8a6f1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f is the tip",
     "IDENT_COMMIT_HASH"),
    ("an absolute home path (the leaked ledger path)",
     "ledger: /Users/someone/agent/.telegram_bot/terminal-state.jsonl",
     "IDENT_ABS_PATH"),
    ("a tilde path (the leaked evidence path)",
     "evidence: ~/agent/dispatch/out.log", "IDENT_ABS_PATH"),
    ("a log-tail header naming its source file",
     "--- log tail (/tmp/agent/x.stderr.log) ---", "IDENT_ABS_PATH"),
    ("an exit code, colon form (the cron failure alert)",
     "exit  : 1", "IDENT_PROCESS"),
    ("an exit code, paren form (the dead run-notify wrapper's string)",
     "[warn] routine x failed (exit 2), see logs", "IDENT_PROCESS"),
    ("a return code (the terminal-state sweep alert)",
     "sweep failed (rc=3)", "IDENT_PROCESS"),
    ("a process id", "worker pid 12345 is gone", "IDENT_PROCESS"),
]

print("--- A1: identifier shapes are detected ---")
for desc, text, kind_attr in POSITIVE:
    kind = getattr(mg, kind_attr)
    hits = find(text)
    check("detects %s" % desc,
          any(k == kind for k, _ in hits), "hits=%r" % (hits,))

# FALSE-POSITIVE CONTRACT. Every string below is either copy the owner already
# APPROVED (cron-guard usage-defer 2026-08-12, health-observer fail-streak) or
# ordinary text that reaches him. A hit on any of these means the detector is
# on its way to being switched off, which is how the JARGON detector died.
NEGATIVE = [
    ("approved fail-streak copy: exit code under a Korean label in a fold",
     "⚠️ 스케줄 작업 하나가 계속 실패하고 있어요.\n"
     ">! 기술 상세\n> 잡 라벨: com.example.agent.brief\n> 종료코드: 1"),
    ("approved usage-defer copy: slash command plus job label",
     "주간 사용량 한도로 대기 중입니다. 사용량 리셋되면 자동 재개돼요.\n"
     "또는 /usageretry com.example.agent.consolidate"),
    ("a bare slash command", "지속 상태는 /health 에 상시 표시됩니다"),
    ("a console deep link (a URL carries //host/path)",
     "자세한 건 여기: https://console.example.com/a/b/c?run=dsp-1"),
    ("a version line", "Restart complete v1.12.0 update applied"),
    ("prose with slashes, a date and a large plain number",
     "and/or 24/7 2026-09-11 파일 1234567 bytes"),
    ("English words built only from hex letters",
     "he effaced the cabbage facade"),
    ("a ticket id and a decision id", "dec-094 UX gate / DGN-1209 follow-up"),
]

print("\n--- A2: approved copy and ordinary text are NOT flagged ---")
for desc, text in NEGATIVE:
    hits = find(text)
    check("passes %s" % desc, not hits, "hits=%r" % (hits,))

print("\n--- A3: detector is pure and total ---")
check("empty text is clean", find("") == ())
check("clean Korean prose is clean",
      find("✅ 작업완료: 고객 관계 정리 - 정리해서 보고드릴게요.") == ())
check("a hit reports (kind, sample) pairs",
      all(isinstance(h, tuple) and len(h) == 2 for h in find("exit: 1 /a/b/c")))

# ---------------------------------------------------------------------------
# B. wiring at the choke point
# ---------------------------------------------------------------------------
print("\n--- B: the guard is WIRED on the send rail and blocks ---")
push_sh = read("routines/push.sh")
check("push.sh calls the detector", "find_execution_identifiers" in push_sh)
check("push.sh refuses the send on a hit (exit 4)",
      "execution-identifier guard" in push_sh and "exit 4" in push_sh)
check("the refusal tells the caller what to do",
      "fix: delete the identifier" in push_sh)
check("the refusal has no override env", "IDENT_OVERRIDE" not in push_sh)
check("exit code 4 is documented in the usage header",
      "4 실행식별자 가드 거부" in push_sh)

# ---------------------------------------------------------------------------
# C. DRAFT copy carries the pending-owner-confirmation marker
# ---------------------------------------------------------------------------
# The marker must sit in the comment block ABOVE the string. MARKER_WINDOW is
# how many preceding lines are searched: one comment block legitimately covers
# a small case/if block holding several variants of the same notice.
MARKER = "WORDING PENDING OWNER CONFIRMATION"
MARKER_WINDOW = 25

# (file, draft substring) for every owner-facing string replaced at the eight
# builder sites. Adding a site without a marker fails this suite.
DRAFTS = [
    # dispatch-detached: the failed branch is owner-CONFIRMED (2026-09-11)
    # and the completed branch renders the owner-designed END block
    # (2026-09-18) -- both pinned in section D below instead. These two
    # The elapsed unit words inside the END block ("1분 미만" family) are
    # still draft wording: the block shape is
    # approved but the design names the <소요시간> slot, not its unit
    # rendering.
    ("routines/dispatch-detached.sh", "1분 미만"),
    ("routines/routine-roller.sh", "직접 등록해 두신 반복 일정 중"),
    ("routines/mirror-poll.sh", "아직 구글 계정과 연결되지 않았어요"),
    ("routines/mirror-poll.sh", "어떤 캘린더를 쓸지가 아직 정해지지 않았어요"),
    ("routines/mirror-reconcile.sh", "아직 구글 계정과 연결되지 않았어요"),
    ("routines/mirror-reconcile.sh",
     "어떤 캘린더를 쓸지가 아직 정해지지 않았어요"),
    ("routines/backup-data.sh", "백업에 문제가 생겼어요"),
    ("routines/backup-data.sh", "백업을 원격에 올리는 데"),
    ("bridge/watchdog.sh", "스스로 다시 일어설 수 없는 상태예요"),
    ("bridge/watchdog.sh", "여러 번 되살려 봤는데도 계속 멈춰 있어요"),
    ("bridge/watchdog.sh", "자동 복구를 %s번 시도했는데"),
    ("bridge/watchdog.sh", "잠깐 내려가 있었는데 자동으로 되살렸어요"),
    ("bridge/watchdog.sh", "잠깐 멈춰 있었는데 자동으로 되살렸어요"),
    ("routines/terminal-state-ledger.py", "끝났다는 통보 없이 시간이 지난"),
    # DGN-1873 (owner directive 2026-10-06): the retry-scheduled operator
    # notice -- no question, no button. (The dawn failure alarm it shipped
    # with is owner-confirmed since, dec-252 -- section D.)
    ("routines/cron-guard.sh", "작업이 실패했지만 다음 정기 실행 때 자동으로 다시 해요."),
    ("routines/cron-guard.sh", "다시 실패하면 그때 알려드릴게요."),
]

print("\n--- C: every DRAFT string carries the pending-copy marker ---")
_cache = {}
for rel, needle in DRAFTS:
    if rel not in _cache:
        _cache[rel] = read(rel).splitlines()
    lines = _cache[rel]
    idx = [i for i, ln in enumerate(lines) if needle in ln]
    if not idx:
        bad("%s carries the draft string %r" % (rel, needle),
            "string not found -- copy changed without updating this pin")
        continue
    marked = all(
        any(MARKER in ln for ln in lines[max(0, i - MARKER_WINDOW):i])
        for i in idx
    )
    check("%s: draft %r is marked pending" % (rel, needle[:16]), marked,
          "no %r within %d lines above" % (MARKER, MARKER_WINDOW))

# The drafts must themselves be clean under the detector -- otherwise a
# replacement re-opens the very leak the detector would then have to block.
print("\n--- C2: no DRAFT string trips the detector ---")
for rel, needle in DRAFTS:
    hits = find(needle)
    check("%s: draft %r is identifier-free" % (rel, needle[:16]),
          not hits, "hits=%r" % (hits,))

# ---------------------------------------------------------------------------
# D. CONFIRMED copy carries a dated owner-approval record, NOT the marker
# ---------------------------------------------------------------------------
# The marker means "nobody has approved this yet". Once the owner authors and
# confirms a string, leaving the marker on it is the inverse defect of section
# C: approved copy re-read as draft and re-worded by the next pass. So the
# assertion flips for these two -- no pending marker above them, a dated
# owner-approval record instead, in the same form as the approved copy in
# bridge/messages.py (e.g. "owner-approved copy (2026-08-21)").
#
# (file, confirmed substring, approval date). The 작업실패 line is
# owner-authored verbatim (2026-09-11). The start/end block titles are the
# owner-designed dispatch blocks (identity Response workflow, "Completion
# signal"; approved and recorded 2026-09-18) -- the fresh owner pass that
# superseded the 2026-09-11 one-line 작업완료 copy on the completed branch.
# Their title TERM is interpolated ($(background_term) -- start DGN-1767,
# end DGN-1769, owner 2026-09-28), never a literal, so
# the pinned substring is the interpolation itself: replacing it with a
# fixed word is the halfway-override defect the owner caught. Do not
# re-word any of these without a fresh owner pass.
CONFIRMED = [
    ("routines/dispatch-detached.sh",
     '✅ $(background_term) 작업 완료', "2026-09-28"),
    ("routines/dispatch-detached.sh",
     '▶️ $(background_term) 작업 시작', "2026-09-28"),
    ("routines/dispatch-detached.sh",
     "⚠️ 작업실패: %s - 원인 확인하고 다시 알려드릴게요.", "2026-09-11"),
    ("routines/dispatch-detached.sh",
     "⚠️ %s 작업이 멈췄습니다.", "2026-09-24"),
    # cron-guard operator alarms: owner-approved first-person copy
    # (2026-09-27, DGN-1747) replaced the two drafts that sat in section C.
    ("routines/cron-guard.sh",
     "작업이 방금 실패했어요.", "2026-09-27"),
    ("routines/cron-guard.sh",
     "⚠️ 끝나지 않은 작업을 찾는 점검이 실패했어요.", "2026-09-27"),
    ("routines/cron-guard.sh",
     "놓친 작업이 있을 수 있어요. 한 시간 안에 다시 점검해요.", "2026-09-27"),
    # The dawn (unattended) failure alarm, owner-confirmed 2026-10-06
    # (dec-252): headline in cron-guard.sh, the five machine-classified reason
    # lines and the retry-line frame in routines/lib/dawn_alarm.py.
    ("routines/cron-guard.sh",
     "⚠️ 새벽에 ${AGENT_DISP}의 ${JOB_DISP} 작업이 실패했어요.", "2026-10-06"),
    ("routines/lib/dawn_alarm.py",
     "기억 저장소를 다른 작업이 쓰고 있어서 끝까지 정리하지 못했어요.", "2026-10-06"),
    ("routines/lib/dawn_alarm.py", "사용 한도에 걸려서 멈췄어요.", "2026-10-06"),
    ("routines/lib/dawn_alarm.py", "정해진 시간 안에 끝나지 않았어요.", "2026-10-06"),
    ("routines/lib/dawn_alarm.py", "새벽에 Mac이 꺼져 있어서 돌지 못했어요.", "2026-10-06"),
    ("routines/lib/dawn_alarm.py",
     "원인을 아직 가리지 못했어요. 기록은 남겨 뒀어요.", "2026-10-06"),
    ("routines/lib/dawn_alarm.py", 'RETRY_SUFFIX_KO = " 다시 시도할게요."', "2026-10-06"),
    # dec-255 (2026-10-06): the retry line when the next fire cannot be read.
    ("routines/lib/dawn_alarm.py", "다음 정기 실행 때 다시 시도할게요.", "2026-10-06"),
]
# The crew stall alarm (owner-approved 2026-09-27, DGN-1747) moved into the
# i18n table in DGN-1790 r2 (owner 2026-10-01 "i18n하고 디폴트는 다 영어야"):
# its confirmed ko words now live in config/i18n/ko.json and cron-guard.sh
# reads them by key (English in-code default).  JSON carries no approval
# comment, so D3 pins the words in the table and the key read in the code.
CONFIRMED_I18N = [
    ("cron.lease_alarm_body", "분째 끝나지 않고 있어요.", "routines/cron-guard.sh"),
    # dec-255 (2026-10-06): the unattended (dawn) stall alarm, DGN-1873 Q2.
    ("cron.lease_alarm_body_unattended", "그래서 이번", "routines/cron-guard.sh"),
]

print("\n--- D: every CONFIRMED string is recorded as approved, not draft ---")
for rel, needle, date in CONFIRMED:
    if rel not in _cache:
        _cache[rel] = read(rel).splitlines()
    lines = _cache[rel]
    idx = [i for i, ln in enumerate(lines) if needle in ln]
    if not idx:
        bad("%s carries the confirmed string %r" % (rel, needle[:16]),
            "string not found -- owner-authored copy was altered")
        continue
    ok("%s: confirmed %r is present verbatim" % (rel, needle[:16]))
    check("%s: confirmed %r is NOT marked pending" % (rel, needle[:16]),
          all(not any(MARKER in ln
                      for ln in lines[max(0, i - MARKER_WINDOW):i])
              for i in idx),
          "%r still sits within %d lines above approved copy"
          % (MARKER, MARKER_WINDOW))
    check("%s: confirmed %r records the approval date %s"
          % (rel, needle[:16], date),
          all(any(date in ln and "approved" in ln.lower()
                  for ln in lines[max(0, i - MARKER_WINDOW):i])
              for i in idx),
          "no dated owner-approval line within %d lines above"
          % MARKER_WINDOW)

print("\n--- D1b: the dawn alarm's superseded draft lines are gone (dec-252) ---")
_cg = read("routines/cron-guard.sh")
for gone in ("이번 실행 기록은 남아 있어요.", "이번 실행 기록은 남지 않았어요.",
             "다음 정기 실행 때 다시 해요."):
    check("routines/cron-guard.sh: dropped draft %r is gone" % gone[:16],
          gone not in _cg)

print("\n--- D2: no CONFIRMED string trips the detector ---")
for rel, needle, _date in CONFIRMED:
    hits = find(needle)
    check("%s: confirmed %r is identifier-free" % (rel, needle[:16]),
          not hits, "hits=%r" % (hits,))

print("\n--- D3: confirmed copy moved to the i18n table stays verbatim there ---")
_ko = json.loads(read("config/i18n/ko.json"))
for key, needle, rel in CONFIRMED_I18N:
    check("config/i18n/ko.json %s carries the confirmed %r" % (key, needle[:16]),
          needle in (_ko.get(key) or ""), "value=%r" % (_ko.get(key),))
    check("%s reads %s through i18n_or" % (rel, key),
          ("i18n_or %s " % key) in read(rel), "key read not found")
    hits = find(needle)
    check("i18n %s: confirmed %r is identifier-free" % (key, needle[:16]),
          not hits, "hits=%r" % (hits,))

# ---------------------------------------------------------------------------
# E. one vocabulary, one register (owner rulings, 2026-09-11)
# ---------------------------------------------------------------------------
# E1. The backup notices named the same object two ways (기록 보관 / 백업).
#     The owner picked 백업, so the other spelling must not come back.
# E2. Register: the confirmed pair above is 해요체 (알려드릴게요 / 보고드릴게요),
#     so every DRAFT notice matches it. The formal ~합니다/~습니다/~입니다
#     endings are what mixed inside single sentences before. EXEMPT: the
#     usage-defer copy in cron-guard.sh, which is separately owner-approved
#     (2026-08-12) and is not this pass's to re-word -- it is matched out by
#     its own marker string rather than by line number.
NOTICE_FILES = [
    "routines/dispatch-detached.sh",
    "routines/cron-guard.sh",
    "routines/routine-roller.sh",
    "routines/mirror-poll.sh",
    "routines/mirror-reconcile.sh",
    "routines/backup-data.sh",
    "bridge/watchdog.sh",
    "routines/terminal-state-ledger.py",
    "routines/lib/dawn_alarm.py",
]
FORMAL_ENDINGS = ("합니다", "습니다", "입니다")
APPROVED_EXEMPT = ("주간 사용량", "사용량 리셋", "작업이 멈췄습니다")

print("\n--- E1: the backup notices use one word for one thing (백업) ---")
for rel in ("routines/backup-data.sh",):
    body = read(rel)
    check("%s: 기록 보관 is gone from owner copy" % rel,
          "기록 보관" not in body and "기록보관" not in body)
    check("%s: 백업 is the word it uses" % rel, "백업" in body)

print("\n--- E2: one register across the notice paths (해요체) ---")
for rel in NOTICE_FILES:
    if rel not in _cache:
        _cache[rel] = read(rel).splitlines()
    offenders = [
        ln.strip() for ln in _cache[rel]
        if any(e in ln for e in FORMAL_ENDINGS)
        and not any(x in ln for x in APPROVED_EXEMPT)
    ]
    check("%s: no formal-register ending in notice copy" % rel,
          not offenders, "offending lines=%r" % (offenders[:2],))

print("\n=== %d passed, %d failed ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
