#!/usr/bin/env python3
"""terminal-state-ledger.py -- DGN-1012 single terminal-state machine.

Axis: an async/deferred obligation MUST end as done or failed -- never
silence. ONE machine, N surfaces: surfaces REGISTER obligations here with
one-line open/close/beat calls; the expiry wake-up rides the EXISTING hourly
housekeeper launchd job (cron-guard wrapped -- no new watcher, DGN-1013 item-3
discipline); notifications reuse the EXISTING channels only: push.sh (owner,
bridge-independent curl) and the session-inbox (the owner-agent session,
bot.py DGN-217 poller). No new notification channel.

Ledger: append-only JSONL at <AGENT_ROOT>/.telegram_bot/terminal-state-ledger.jsonl
(runtime state, gitignored; chflags uappend best-effort -- DGN-1009 pattern).

Event schema (one JSON object per line; every event carries wall-clock `at`
ISO local time + `at_epoch`; close/expired carry the fulfilment path in
`evidence` -- DGN-1012 success criterion 4: time + path):

  open    {v,event:"open",id,surface,at,at_epoch,ttl_secs,expires_epoch,
           notify:"session"|"owner"|"both",note,evidence}
  close   {v,event:"close",id,at,at_epoch,state:"done"|"failed",note,evidence}
  beat    {v,event:"beat",id,surface,at,at_epoch,rc,ttl_secs,expires_epoch,note}
  expired {v,event:"expired",id,at,at_epoch,opened_epoch,notified:[...],note}

State fold: events replayed in file order per id. `open` after a terminal
event (close/expired) starts a NEW lifecycle (id reuse is legal -- e.g. the
singular restart-pending obligation). `beat` renews itself (last beat wins).
`close` without a live open is recorded as an amendment line (kept for the
audit trail, warned on stderr, never an error).

Surfaces registered as of DGN-1012 landing (grep `terminal-state-ledger.py`
for the live list -- the calls, not this comment, are the truth):
  dispatch     routines/dispatch-detached.sh  open at spawn / close in finalize
  cron         routines/cron-guard.sh         beat per wrapped run (rc recorded)
  restart-cta  bridge/self_restart.sh         open at marker arm / close on push
               bridge/bot.py                  close when the layer-2 backstop
                                              terminal-closes instead

Nag / false-positive posture (DGN-1012 success criterion 5):
  - `expired` is TERMINAL: exactly ONE notification per obligation lifetime.
    A swept obligation never nags again.
  - one sweep bundles ALL due expiries into ONE session-inbox drop and ONE
    owner push (no per-item message storm).
  - notify failure leaves the obligation open -> retried on the next hourly
    sweep, and sweep exits nonzero -> housekeeper propagates -> cron-guard
    fires its (daily-deduped) owner alert. The closure machine's own death is
    loud, not silent.
  - owner-push dedup marker per (id, day) guards the crash window between a
    successful push and the expired-line append (no double push).
  - beats with rc!=0 do NOT alert here -- cron-guard already owns run-failure
    alerts; duplicating them would be nag, not guarantee.

Limits (DGN-1012 ticket L1-L4 inherited + new; keep this list honest --
claiming coverage we do not have is itself the false-forcing-point failure):
  L1 active lying: `close --state done` is self-reported; a false close
     passes. The lie itself stays on the ledger (auditable), not prevented.
  L2 fulfilment quality unguaranteed: closed != correct.
  L3 all closers dead: if housekeeper/launchd itself stops firing, the sweep
     never runs. cron beats record the gap post-hoc, but the push at that
     moment does not happen. External-watch territory (ticket L3).
  L4 unenumerated surfaces: only registered surfaces are covered. Skill-chain
     (DGN-1011) and handoff-reply have NO registration call yet -- they are
     UNGUARANTEED and documented as such.
  L5 (new) registration is fail-open: every registration call is wrapped
     `|| true` at the call site so it can never break its surface. A silently
     failing registration = an obligation that never existed = silence. The
     wrapper's own stderr is the only trace.
  L6 (new) detection latency = sweep cadence: expiry fires on the next hourly
     housekeeper tick, so a TTL of 15min is detected up to ~75min after open.
  L7 (new) cron beats without --ttl are record-only: "didn't run" becomes
     visible on query (status), not by push. Push-on-missed-run requires the
     job to declare its cadence (DOGANY_TSL_BEAT_TTL env in its plist).
  L8 (new) concurrent appends rely on O_APPEND atomicity for short lines
     (single write() per event); pathological >4KB notes could interleave.
  L9 (M2) the drive's regress terminates OUTSIDE this machine. The sweep is
     driven by two independent legs (cron-guard = every periodic job;
     dispatch-detached = every delegation), and its own liveness beat lets a
     RETURNING sweep report a gap it slept through. What no leg can report is
     its own permanent death: if the whole cron fleet AND dispatch stop, the
     ledger stops and says nothing. That state is not silent for other
     reasons -- 3 of the 11 wrapped jobs are the owner's scheduled briefs
     (generic-brief morning/retro/weekly) -- but the tripwire is a human
     noticing an absence, not a machine. Declared 미보장 in the design doc's
     surface table (3d), not papered over. Supersedes L3's narrower form
     (a single housekeeper job).

Env seams (tests only): DOGANY_TSL_LEDGER, DOGANY_TSL_INBOX, DOGANY_TSL_PUSH,
DOGANY_TSL_DEDUP_DIR, DOGANY_TSL_ENV.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.dirname(SCRIPT_DIR)

LEDGER_PATH = os.environ.get(
    "DOGANY_TSL_LEDGER",
    os.path.join(AGENT_ROOT, ".telegram_bot", "terminal-state-ledger.jsonl"),
)
INBOX_DIR = os.environ.get(
    "DOGANY_TSL_INBOX", os.path.join(AGENT_ROOT, ".telegram_bot", "session-inbox")
)
PUSH_SH = os.environ.get("DOGANY_TSL_PUSH", os.path.join(SCRIPT_DIR, "push.sh"))
PUSH_ENV = os.environ.get(
    "DOGANY_TSL_ENV", os.path.join(AGENT_ROOT, ".telegram_bot", ".env")
)
DEDUP_DIR = os.environ.get("DOGANY_TSL_DEDUP_DIR", "/tmp/dogany-tsl")

VALID_NOTIFY = ("session", "owner", "both")
TERMINAL = ("done", "failed", "expired")

# DGN-1012 sweep drive (M2). The sweep is the ONLY recovery path for a
# SIGKILLed obligation (measured: TERM/INT/HUP all run the worker EXIT trap;
# KILL does not), so what wakes it decides whether the whole axis holds.
# The drive is TWO independent legs -- cron-guard.sh (every periodic job) and
# dispatch-detached.sh (launchd-independent) -- and both pass --throttle, so
# riding every job costs one sweep per window, not one per job.
#
# The throttle and the mutual exclusion live HERE, not in the drivers: same
# reason registration lives in the launcher (design R1) -- a driver cannot
# forget what it never had to remember. Adding a third leg later is one line.
SWEEP_BEAT_ID = "terminal-state-sweep"
SWEEP_LOCK = os.path.join(DEDUP_DIR, "sweep.lock")
SWEEP_LOCK_STALE = 900       # a sweep that has held the claim this long is dead
SWEEP_BEAT_TTL_MIN = 7200    # gap alarm floor when --throttle is small


def now_pair():
    epoch = int(time.time())
    iso = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    return iso, epoch


def append_event(obj):
    """Single O_APPEND write per event; best-effort uappend (DGN-1009)."""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    fd = os.open(LEDGER_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    if sys.platform == "darwin":
        subprocess.run(
            ["chflags", "uappend", LEDGER_PATH],
            capture_output=True,
            timeout=5,
            check=False,
        )


def read_events():
    """Yield parsed events in file order; count (not raise on) corrupt lines."""
    events, corrupt = [], 0
    if not os.path.isfile(LEDGER_PATH):
        return events, corrupt
    with open(LEDGER_PATH, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if isinstance(ev, dict) and ev.get("id") and ev.get("event"):
                    events.append(ev)
                else:
                    corrupt += 1
            except ValueError:
                corrupt += 1
    return events, corrupt


def fold(events):
    """Event-sourced fold -> {id: state-dict}. Later events win per id."""
    st = {}
    for ev in events:
        oid = ev["id"]
        kind = ev["event"]
        cur = st.get(oid)
        if kind == "open":
            st[oid] = {
                "id": oid,
                "status": "open",
                "surface": ev.get("surface", "?"),
                "opened_epoch": ev.get("at_epoch"),
                "opened_at": ev.get("at"),
                "expires_epoch": ev.get("expires_epoch"),
                "notify": ev.get("notify", "session"),
                "note": ev.get("note", ""),
                "evidence": ev.get("evidence", ""),
                "last_at": ev.get("at"),
                "last_epoch": ev.get("at_epoch"),
                "rc": None,
            }
        elif kind == "beat":
            st[oid] = {
                "id": oid,
                "status": "beat",
                "surface": ev.get("surface", "cron"),
                "opened_epoch": (cur or {}).get("opened_epoch") or ev.get("at_epoch"),
                "opened_at": (cur or {}).get("opened_at") or ev.get("at"),
                "expires_epoch": ev.get("expires_epoch"),
                "notify": (cur or {}).get("notify", "session"),
                "note": ev.get("note", ""),
                "evidence": "",
                "last_at": ev.get("at"),
                "last_epoch": ev.get("at_epoch"),
                "rc": ev.get("rc"),
            }
        elif kind in ("close", "expired"):
            if cur is None:
                cur = {"id": oid, "surface": "?", "notify": "session"}
            cur = dict(cur)
            cur["status"] = ev.get("state", "expired") if kind == "close" else "expired"
            cur["last_at"] = ev.get("at")
            cur["last_epoch"] = ev.get("at_epoch")
            cur["closed_epoch"] = ev.get("at_epoch")
            if ev.get("evidence"):
                cur["evidence"] = ev["evidence"]
            st[oid] = cur
    return st


def _agent_prefix():
    """Per-agent signature emoji -- SAME source cron-guard uses (never hardcode)."""
    conf = os.path.join(AGENT_ROOT, ".instance.conf")
    try:
        with open(conf, encoding="utf-8") as f:
            for line in f:
                if line.startswith("DOGANY_AGENT_PREFIX="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "[agent]"


def _fmt_item(s, now):
    opened = s.get("opened_at") or "?"
    over = ""
    if s.get("expires_epoch"):
        over = f", 만료 {int(now - s['expires_epoch'])}s 초과"
    ev = f", evidence: {s['evidence']}" if s.get("evidence") else ""
    note = f" -- {s['note']}" if s.get("note") else ""
    return f"- {s['surface']}/{s['id']}{note} (개시 {opened}{over}{ev})"


def notify_session(due, now):
    """ONE bundled session-inbox drop (DGN-217 writer contract: UTF-8,
    dot-prefixed temp + atomic rename). Returns True on success."""
    lines = [
        f"[terminal-state] 종결 백스톱: 만료 의무 {len(due)}건 (DGN-1012 sweep)",
        "",
    ]
    lines += [_fmt_item(s, now) for s in due]
    lines += [
        "",
        "세션 지시: 각 항목의 실제 결말을 확인하라 (evidence 경로부터). 실제로는 "
        "완료된 작업이면 close --state done --note 로 정정 기록을 남기고, 죽은 "
        "작업이면 재발주/수동 마무리를 판단하라.",
        f"ledger: {LEDGER_PATH}",
    ]
    try:
        os.makedirs(INBOX_DIR, exist_ok=True)
        name = f"terminal-state-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        tmp = os.path.join(INBOX_DIR, f".{name}.tmp")
        with open(tmp, "w", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, os.path.join(INBOX_DIR, name))
        return True
    except OSError as e:
        print(f"[terminal-state] session-inbox drop failed: {e}", file=sys.stderr)
        return False


def notify_owner(due, now):
    """ONE bundled owner push via push.sh (bridge-independent). Per-(id,day)
    dedup markers guard the push-then-crash window. Returns True when the
    push succeeded OR every due id was already pushed today (dedup)."""
    day = datetime.now().strftime("%Y%m%d")
    os.makedirs(DEDUP_DIR, exist_ok=True)
    fresh = [s for s in due if not os.path.exists(
        os.path.join(DEDUP_DIR, f"{s['id']}.{day}"))]
    if not fresh:
        return True
    # 잠정 문구 -- 미확정(형님 확인 대기, dec-094 UX gate). 발화 조건 자체가
    # "종결 주체 전원 사망"이라는 예외 상황이므로 잠정본 노출 위험은 그
    # 상황에서만 존재한다 (DGN-1010 백스톱 문구와 같은 지위).
    text = (
        f"{_agent_prefix()} [종결 백스톱] 종결 통보 없이 만료된 비동기 작업 "
        f"{len(fresh)}건:\n"
        + "\n".join(_fmt_item(s, now) for s in fresh)
        + f"\n장부: {LEDGER_PATH}"
    )
    cmd = ["/bin/bash", PUSH_SH, "--text", text]
    if os.path.isfile(PUSH_ENV):
        cmd += ["--env", PUSH_ENV]
    try:
        rc = subprocess.run(cmd, capture_output=True, timeout=60).returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[terminal-state] owner push failed: {e}", file=sys.stderr)
        return False
    if rc != 0:
        print(f"[terminal-state] owner push failed rc={rc}", file=sys.stderr)
        return False
    for s in fresh:
        try:
            with open(os.path.join(DEDUP_DIR, f"{s['id']}.{day}"), "w") as f:
                f.write(str(int(now)))
        except OSError:
            pass
    return True


def cmd_open(a):
    iso, epoch = now_pair()
    append_event({
        "v": 1, "event": "open", "id": a.id, "surface": a.surface,
        "at": iso, "at_epoch": epoch, "ttl_secs": a.ttl,
        "expires_epoch": (epoch + a.ttl) if a.ttl else None,
        "notify": a.notify, "note": a.note, "evidence": a.evidence,
    })
    return 0


def cmd_close(a):
    events, _ = read_events()
    st = fold(events).get(a.id)
    if st is None or st["status"] in TERMINAL:
        print(
            f"[terminal-state] close on non-open id '{a.id}' -- recorded as "
            "amendment (audit trail), no live obligation released",
            file=sys.stderr,
        )
    iso, epoch = now_pair()
    append_event({
        "v": 1, "event": "close", "id": a.id, "at": iso, "at_epoch": epoch,
        "state": a.state, "note": a.note, "evidence": a.evidence,
    })
    return 0


def cmd_beat(a):
    iso, epoch = now_pair()
    append_event({
        "v": 1, "event": "beat", "id": a.id, "surface": a.surface,
        "at": iso, "at_epoch": epoch, "rc": a.rc, "ttl_secs": a.ttl,
        "expires_epoch": (epoch + a.ttl) if a.ttl else None,
        "note": a.note,
    })
    return 0


def _sweep_claim(stale=SWEEP_LOCK_STALE):
    """Atomic single-sweeper claim. mkdir is the atomic primitive (same guard
    `finalize_run` uses for .finalized). Two drivers CAN fire in the same
    instant -- health-observer and mirror-poll both run on a 300s
    StartInterval -- and a double sweep would mean a duplicate session-inbox
    drop, i.e. nag. Fail-OPEN: if the lock dir itself is unusable, sweeping
    twice is strictly better than not sweeping."""
    try:
        os.makedirs(DEDUP_DIR, exist_ok=True)
    except OSError:
        return True
    try:
        os.mkdir(SWEEP_LOCK)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(SWEEP_LOCK) > stale:
                os.rmdir(SWEEP_LOCK)      # reap a sweeper that died holding it
                os.mkdir(SWEEP_LOCK)
                return True
        except OSError:
            pass
        return False
    except OSError:
        return True


def _sweep_release():
    try:
        os.rmdir(SWEEP_LOCK)
    except OSError:
        pass


def _sweep_beat(throttle, beat_ttl):
    """The sweep's OWN liveness row. Written by the sweep itself rather than
    by each driver -- a driver cannot forget it, and a third leg gets it free.

    This is also what makes the drive's own gap observable: the beat carries a
    TTL, so if nothing sweeps for two windows the row goes overdue and the
    NEXT sweep expires it like any other abandoned obligation. That closes the
    regress one step (who watches the watcher) without a new watcher. It does
    NOT close it entirely -- see L9."""
    ttl = beat_ttl
    if ttl is None:
        # An unthrottled (manual/test) sweep is record-only: arming a gap alarm
        # from a one-off invocation would fire on the next scheduled sweep.
        ttl = max(2 * throttle, SWEEP_BEAT_TTL_MIN) if throttle else None
    iso, epoch = now_pair()
    append_event({
        "v": 1, "event": "beat", "id": SWEEP_BEAT_ID, "surface": "sweep",
        "at": iso, "at_epoch": epoch, "rc": 0, "ttl_secs": ttl,
        "expires_epoch": (epoch + ttl) if ttl else None,
        "note": "종결 스윕 구동 비트 -- 이 행이 만료됐다면 그 기간 동안 스윕이 돌지 않았다",
    })


def cmd_sweep(a):
    events, corrupt = read_events()
    st = fold(events)
    now = a.now if a.now else time.time()
    throttle = max(0, a.throttle or 0)
    if throttle:
        prev = st.get(SWEEP_BEAT_ID)
        if (prev and prev.get("status") == "beat" and prev.get("last_epoch")
                and now - prev["last_epoch"] < throttle):
            return 0          # a sweep already ran this window -- silent, cheap
        if not _sweep_claim():
            return 0          # another driver leg holds the claim right now
    try:
        rc = _sweep_body(st, now, corrupt)
    finally:
        # Liveness is recorded for a sweep that RAN, not one that succeeded --
        # a sweep failing to notify is still evidence the drive is alive.
        _sweep_beat(throttle, a.beat_ttl)
        if throttle:
            _sweep_release()
    return rc


def _sweep_body(st, now, corrupt):
    due = [
        s for s in st.values()
        if s["status"] in ("open", "beat")
        and s.get("expires_epoch")
        and now >= s["expires_epoch"]
    ]
    if corrupt:
        print(f"[terminal-state] sweep: {corrupt} corrupt line(s) skipped",
              file=sys.stderr)
    if not due:
        return 0  # clean run = silent (attention-budget discipline)

    need_session = [s for s in due if s["notify"] in ("session", "both")]
    need_owner = [s for s in due if s["notify"] in ("owner", "both")]
    sess_ok = notify_session(need_session, now) if need_session else True
    owner_ok = notify_owner(need_owner, now) if need_owner else True

    notified_any = False
    for s in due:
        chans_ok = (
            (s["notify"] != "session" or sess_ok)
            and (s["notify"] != "owner" or owner_ok)
            and (s["notify"] != "both" or (sess_ok and owner_ok))
        )
        if not chans_ok:
            continue  # stays open -> retried next sweep
        iso, epoch = now_pair()
        append_event({
            "v": 1, "event": "expired", "id": s["id"], "at": iso,
            "at_epoch": epoch, "opened_epoch": s.get("opened_epoch"),
            "notified": [c for c in ("session", "owner")
                         if s["notify"] in (c, "both")],
            "note": s.get("note", ""),
        })
        notified_any = True
    if notified_any:
        print(f"[terminal-state] sweep: {len(due)} obligation(s) expired + notified")
    if sess_ok and owner_ok:
        return 0
    return 1  # notify failure -> housekeeper/cron-guard makes this loud


def cmd_status(a):
    events, corrupt = read_events()
    st = fold(events)
    rows = sorted(st.values(), key=lambda s: s.get("opened_epoch") or 0)
    if a.open_only:
        rows = [s for s in rows if s["status"] in ("open", "beat")]
    if a.as_json:
        print(json.dumps({"corrupt": corrupt, "items": rows}, ensure_ascii=False,
                         indent=2))
        return 0
    now = time.time()
    for s in rows:
        exp = ""
        if s.get("expires_epoch"):
            d = int(s["expires_epoch"] - now)
            exp = f" expires_in={d}s" if d >= 0 else f" OVERDUE={-d}s"
        rc = f" rc={s['rc']}" if s.get("rc") is not None else ""
        print(f"{s['status']:8s} {s['surface']:12s} {s['id']}{rc}{exp} "
              f"last={s.get('last_at', '?')} {s.get('note', '')}")
    if corrupt:
        print(f"(corrupt lines skipped: {corrupt})")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("open", help="register a pending obligation")
    po.add_argument("--id", required=True)
    po.add_argument("--surface", required=True)
    po.add_argument("--ttl", type=int, default=None, help="seconds to expiry")
    po.add_argument("--notify", choices=VALID_NOTIFY, default="session")
    po.add_argument("--note", default="")
    po.add_argument("--evidence", default="")
    po.set_defaults(fn=cmd_open)

    pc = sub.add_parser("close", help="terminal-close an obligation")
    pc.add_argument("--id", required=True)
    pc.add_argument("--state", choices=("done", "failed"), required=True)
    pc.add_argument("--note", default="")
    pc.add_argument("--evidence", default="")
    pc.set_defaults(fn=cmd_close)

    pb = sub.add_parser("beat", help="record a liveness beat (cron surface)")
    pb.add_argument("--id", required=True)
    pb.add_argument("--surface", default="cron")
    pb.add_argument("--rc", type=int, default=0)
    pb.add_argument("--ttl", type=int, default=None,
                    help="arm a missed-next-beat expiry (seconds)")
    pb.add_argument("--note", default="")
    pb.set_defaults(fn=cmd_beat)

    ps = sub.add_parser("sweep", help="expire overdue obligations + notify")
    ps.add_argument("--now", type=int, default=None,
                    help="epoch override (tests only)")
    ps.add_argument("--throttle", type=int, default=0,
                    help="seconds; skip if a sweep already ran this window. "
                         "Drivers pass this so the sweep can ride EVERY "
                         "periodic job without sweeping once per job.")
    ps.add_argument("--beat-ttl", type=int, default=None,
                    help="seconds until the sweep's own liveness beat goes "
                         "overdue (default max(2*throttle, 7200))")
    ps.set_defaults(fn=cmd_sweep)

    pt = sub.add_parser("status", help="fold + print current obligations")
    pt.add_argument("--open", dest="open_only", action="store_true")
    pt.add_argument("--json", dest="as_json", action="store_true")
    pt.set_defaults(fn=cmd_status)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # registration must never crash a surface loudly
        print(f"[terminal-state] internal error: {e}", file=sys.stderr)
        sys.exit(2)
