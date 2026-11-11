#!/usr/bin/env python3
# test-lane: fast
"""DGN-1966 -- standing watches on the workbench.

Contract under test (routines/lib/watches.py + the status-footer "watches"
section + the composer's dashboard-actions.json):
  * a SKILL.md `watch:` block declares a watch -> a board row appears;
    a malformed block is skipped, a valid neighbour still loads.
  * toggle off -> routine-ctl disable ran, the health observer holds a
    retire marker for the job's label, and the observer's loss detector
    stays silent for it (while an unretired vanish still fires).
  * toggle on -> unretire + routine-ctl enable; enabled=True recorded only
    after the scheduler confirms; a failed arm re-retires and records off.
  * a switched-on watch that is stale / errored triggers the board; healthy
    or switched-off watches never do; unreadable watches.json -> UNKNOWN.
  * the board's keyboard lands in dashboard-actions.json bound to the
    composed text by sha; callback data <= 64 bytes.
  * follow_presence: away -> on, at_mac -> off; a manual toggle wins until
    the next presence CHANGE; presence-sync is a TODO no-op.

Offline: a stub routine-ctl.sh (state in a temp dir), the REAL
health-observer.py on a temp state dir, temp skills + bot data dirs.
"""

import shutil
import atexit
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
WATCHES_PY = os.path.join(ROUTINES, "lib", "watches.py")
FOOTER_PY = os.path.join(ROUTINES, "status-footer.py")
OBSERVER_PY = os.path.join(ROUTINES, "health-observer.py")

WORK = tempfile.mkdtemp(prefix="dgn1966.")
atexit.register(shutil.rmtree, WORK, ignore_errors=True)  # DGN-2090: leave no temp dir
SKILLS = os.path.join(WORK, "skills")
BOT = os.path.join(WORK, "bot")
STUB = os.path.join(WORK, "stub")
OBS_STATE = os.path.join(WORK, "observer")
AGENT = "testagent"
LABEL = "com.telegram-skill-bot.%s.po-reply-watch" % AGENT

os.environ.update({
    "DOGANY_SKILLS_DIR": SKILLS,
    "DOGANY_BOT_DATA_DIR": BOT,
    "DOGANY_WATCH_AGENT_NAME": AGENT,
    "DOGANY_WATCH_ROUTINE_CTL": os.path.join(STUB, "routine-ctl.sh"),
    "DOGANY_WATCH_HEALTH_OBSERVER": OBSERVER_PY,
    "DOGANY_HEALTH_OBSERVER_STATE_DIR": OBS_STATE,
    "DOGANY_WATCH_NO_REFRESH": "1",
    "STUB_DIR": STUB,
    "DOGANY_LANG": "ko",
})
os.environ["DOGANY_AGENT_CONF"] = os.path.join(WORK, "agent.conf")
open(os.environ["DOGANY_AGENT_CONF"], "w").close()

STUB_SH = r"""#!/bin/bash
# routine-ctl.sh stand-in: a scheduled routine is a file in $STUB_DIR/on/.
echo "$*" >> "$STUB_DIR/calls.log"
mkdir -p "$STUB_DIR/on"
[ -f "$STUB_DIR/fail-$1" ] && exit 2
case "$1" in
  enable)  touch "$STUB_DIR/on/$2" ;;
  disable) rm -f "$STUB_DIR/on/$2" ;;
  status)  [ -f "$STUB_DIR/on/$2" ] && exit 0 || exit 3 ;;
esac
exit 0
"""

_failures = []
_count = [0]


def check(name, got, want):
    _count[0] += 1
    if got != want:
        _failures.append("%s: got %r, want %r" % (name, got, want))


def check_true(name, cond, detail=""):
    _count[0] += 1
    if not cond:
        _failures.append("%s %s" % (name, detail))


def write_skill(name, block):
    d = os.path.join(SKILLS, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: %s\ndescription: test skill\n%s---\n\n# %s\n"
                 % (name, block, name))


PO_BLOCK = (
    "watch:\n"
    "  id: po-reply\n"
    "  label: PO 회신 감시\n"
    "  routine: po-reply-watch\n"
    "  script: .claude/skills/po-watch/run.sh\n"
    "  schedule: \"09:00\"\n"
    "  cadence: 1h\n"
    "  follow_presence: true\n")


def cli(*args):
    p = subprocess.run([sys.executable, WATCHES_PY] + list(args),
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


def calls():
    try:
        with open(os.path.join(STUB, "calls.log")) as fh:
            return [l.strip() for l in fh if l.strip()]
    except OSError:
        return []


def reset_calls():
    try:
        os.unlink(os.path.join(STUB, "calls.log"))
    except OSError:
        pass


def observer_entry():
    try:
        with open(os.path.join(OBS_STATE, "jobs.json")) as fh:
            return json.load(fh).get("jobs", {}).get(LABEL) or {}
    except OSError:
        return {}


def state():
    with open(os.path.join(BOT, "watches.json")) as fh:
        return json.load(fh)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


os.makedirs(STUB)
with open(os.environ["DOGANY_WATCH_ROUTINE_CTL"], "w") as fh:
    fh.write(STUB_SH)

sf = load("status_footer_dgn1966", FOOTER_PY)
W = sf._watches
check_true("watches lib imported by status-footer", W is not None)
obs = load("health_observer_dgn1966", OBSERVER_PY)


def board(watches, decisions=None):
    return sf.Board(sf._base_sections(decisions or [], [], [], None, None,
                                      watches=watches))


# --- 1. declare -> row appears ---------------------------------------------
check("no declarations -> no rows", W.board_view(), [])
check("no declarations -> no section", board(sf._collect_watches()).sections
      and [s.lines() for s in board([]).sections if s.key == "watches"], [[]])
write_skill("po-watch", PO_BLOCK)
write_skill("broken-watch", "watch:\n  id: Bad_ID!\n  label: x\n")
rows = W.board_view()
check("declared watch -> one row", [r["id"] for r in rows], ["po-reply"])
check("unchecked before any switch", rows[0]["enabled"], None)
b = board(sf._collect_watches(), decisions=["dec-1 something"])
text = b.render()
check_true("row renders on the board", "PO 회신 감시: 상태 확인 전" in text, text)
check_true("follow_presence marker", "자리 따라" in text, text)
check("section header", "[상시 감시]" in text, True)
check("unchecked row offers on", b.actions(),
      [[{"text": "PO 회신 감시 켜기", "callback_data": "wb:watch:po-reply:on"}]])
check("plain watch list never triggers", board(rows).triggered, False)

# --- 2. toggle on -> unretire + armed ---------------------------------------
reset_calls()
rc, out = cli("toggle", "po-reply", "on", "--by", "test")
check("on rc", rc, 0)
check_true("on armed via routine-ctl enable",
           "enable po-reply-watch .claude/skills/po-watch/run.sh 09:00" in calls(),
           repr(calls()))
check("on recorded enabled", state()["watches"]["po-reply"]["enabled"], True)
check("schedule label", state()["watches"]["po-reply"]["schedule_label"], LABEL)
check("no retire marker while on", observer_entry().get("retired"), None)

# --- 3. toggle off -> disarmed + retire recorded -> observer silent ---------
reset_calls()
rc, out = cli("toggle", "po-reply", "off", "--by", "test")
check("off rc", rc, 0)
check_true("off disarmed via routine-ctl disable",
           "disable po-reply-watch" in calls(), repr(calls()))
check("off recorded disabled", state()["watches"]["po-reply"]["enabled"], False)
retired = observer_entry().get("retired") or {}
check_true("retire marker recorded", "watch off: po-reply" in
           (retired.get("why") or ""), repr(retired))
# The observer's loss path: the label vanished long enough ago to confirm.
old = "2026-10-09T08:00:00+09:00"
later = "2026-10-09T09:00:00+09:00"
jobs = {LABEL: dict(observer_entry(), lost_at=old)}
check("observer silent for a deliberate off",
      obs.detect_losses(jobs, set(), later), [])
control = {LABEL: {"lost_at": old}}
check("control: an unretired vanish still fires",
      [e["kind"] for e in obs.detect_losses(control, set(), later)],
      ["label_lost"])
b = board(W.board_view(), decisions=["dec-1"])
b.render()
check("off row offers on", b.actions()[0][0]["callback_data"],
      "wb:watch:po-reply:on")

# --- 4. on again -> unretire; a failed arm re-retires and records off -------
rc, _ = cli("on", "po-reply")
check("re-on rc", rc, 0)
check("re-on clears the retire marker", observer_entry().get("retired"), None)
cli("off", "po-reply")
open(os.path.join(STUB, "fail-enable"), "w").close()
rc, out = cli("on", "po-reply")
os.unlink(os.path.join(STUB, "fail-enable"))
check("failed arm rc", rc, 1)
check("failed arm records the actual state",
      state()["watches"]["po-reply"]["enabled"], False)
check_true("failed arm leaves a retire marker",
           bool(observer_entry().get("retired")), repr(observer_entry()))

# --- 5. health -> board trigger ---------------------------------------------
cli("on", "po-reply")
rc, _ = cli("record", "po-reply", "ok")
check("record rc", rc, 0)
rows = W.board_view()
check("fresh run -> healthy", rows[0]["health"], "ok")
check("healthy on watch does not trigger", board(rows).triggered, False)
check("stale after 2 cadences + grace",
      W.board_view(now=time.time() + 2 * 3600 + 301)[0]["health"], "stale")
stale_board = board(W.board_view(now=time.time() + 3 * 3600))
check("stale on watch triggers the board", stale_board.triggered, True)
check_true("stale row says so", "멈춘 것 같아요" in stale_board.render())
cli("record", "po-reply", "error")
err_board = board(W.board_view())
check("errored on watch triggers", err_board.triggered, True)
check_true("error row says so", "실패" in err_board.render())
cli("off", "po-reply")
check("switched-off watch never triggers, even errored",
      board(W.board_view(now=time.time() + 9 * 3600)).triggered, False)

# Unreadable file -> UNKNOWN: board not ok, nothing shown, never cleared.
with open(os.path.join(BOT, "watches.json")) as fh:
    good = fh.read()
with open(os.path.join(BOT, "watches.json"), "w") as fh:
    fh.write("{not json")
check("unreadable -> UNKNOWN", sf._collect_watches() is sf.UNKNOWN, True)
ub = board(sf._collect_watches())
check("UNKNOWN board is not ok", ub.ok, False)
check("UNKNOWN does not trigger", ub.triggered, False)
check("status exit 3 on unreadable", cli("status")[0], 3)
with open(os.path.join(BOT, "watches.json"), "w") as fh:
    fh.write(good)

# --- 6. actions file bound to the composed text -----------------------------
cli("on", "po-reply")
b = board(W.board_view(), decisions=["dec-1 pending"])
text = b.render()
check("publish ok", sf._board_publish(text, b.actions()), True)
with open(os.path.join(BOT, "dashboard.md"), encoding="utf-8") as fh:
    composed = fh.read()
with open(os.path.join(BOT, "dashboard-actions.json"), encoding="utf-8") as fh:
    acts = json.load(fh)
check("actions sha binds the composed text", acts["text_sha"],
      sf._sha256(composed))
check("actions rows", acts["rows"],
      [[{"text": "PO 회신 감시 끄기", "callback_data": "wb:watch:po-reply:off"}]])
sf._board_clear()
with open(os.path.join(BOT, "dashboard-actions.json"), encoding="utf-8") as fh:
    check("cleared board -> no rows", json.load(fh)["rows"], [])
check("foreign callback dropped",
      sf._valid_actions([[{"text": "x", "callback_data": "opt:1"}]]), [])
long_id = "a" * 32
check_true("callback fits 64 bytes",
           len(sf.WATCH_CALLBACK_FMT % (long_id, "off")) <= 64)

# --- 7. follow_presence ------------------------------------------------------
cli("off", "po-reply")
rc, _ = cli("presence", "away")
check("away rc", rc, 0)
check("away -> on", state()["watches"]["po-reply"]["enabled"], True)
rc, _ = cli("presence", "at_mac")
check("at_mac -> off", state()["watches"]["po-reply"]["enabled"], False)
cli("toggle", "po-reply", "on", "--by", "owner-tap")
check("manual on at the Mac", state()["watches"]["po-reply"]["enabled"], True)
cli("presence", "at_mac")
check("same presence again: manual wins",
      state()["watches"]["po-reply"]["enabled"], True)
cli("presence", "away")
cli("toggle", "po-reply", "off", "--by", "owner-tap")
cli("presence", "away")
check("manual off while away holds",
      state()["watches"]["po-reply"]["enabled"], False)
cli("presence", "at_mac")
cli("presence", "away")
check("next change re-applies", state()["watches"]["po-reply"]["enabled"], True)
rc, out = cli("presence-sync")
check("presence-sync is the TODO no-op", (rc, "TODO" in out), (0, True))
check("TODO hook returns None", W.read_presence_signal(), None)
check("bad presence", cli("presence", "home")[0], 2)
check("unknown watch", cli("on", "nope")[0], 2)

print("%d checks, %d failed" % (_count[0], len(_failures)))
for f in _failures:
    print("  FAIL: " + f)
sys.exit(1 if _failures else 0)
