#!/usr/bin/env python3
"""terminal-state-ledger.py -- DGN-1012 single terminal-state machine.

Axis: an async/deferred obligation MUST end as done or failed -- never
silence. ONE machine, N surfaces: surfaces REGISTER obligations here with
one-line open/close/beat calls; the expiry wake-up is the framework-owned
per-instance drive unit (DGN-1655: com.telegram-skill-bot.<agent>.instance-drive,
cron-guard wrapped, rendered from the TTL policy below by
routines/lib/drive-setup.sh); notifications reuse the EXISTING channels only: push.sh (owner,
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
  beat    {v,event:"beat",id,surface,at,at_epoch,mono,rc,ttl_secs,expires_epoch,note}
          (`mono` = time.monotonic() at write, DGN-1655 F5: recorded as DATA
          only -- wall_delta >> mono_delta would mean the host slept. No
          criterion is built on it; the sleep question ships OPEN.)
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
  sweep        <agent>.instance-drive unit    the ONLY producer of the reserved
                                              (terminal-state-sweep, sweep) row
                                              (DGN-1655 F2, enforced below)
  restart-cta  bridge/self_restart.sh         open at marker arm / close on push
               bridge/bot.py                  close when the layer-2 backstop
                                              terminal-closes instead
  mint         scripts/pack/mint_run.sh       open at ordered pipeline entry;
                                              closed ONLY by a 3-value verdict
                                              (clean/stillborn/born-alive-and-
                                              wrong); address STATED via the
                                              env seams below (DGN-1608)

Nag / false-positive posture (DGN-1012 success criterion 5):
  - `expired` is TERMINAL: exactly ONE notification per obligation lifetime.
    A swept obligation never nags again.
  - one sweep bundles ALL due expiries into ONE session-inbox drop and ONE
    owner push (no per-item message storm).
  - notify failure leaves the obligation open -> retried on the next drive
    sweep, and sweep exits nonzero -> the drive unit's cron-guard fires its
    (daily-deduped) owner alert. The closure machine's own death is
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
  L3 all closers dead: if the drive unit / launchd itself stops firing, the
     sweep never runs. The drive beat records the gap post-hoc, but the push at
     that moment does not happen. External-watch territory (ticket L3).
  L4 unenumerated surfaces: only registered surfaces are covered. Skill-chain
     (DGN-1011) and handoff-reply have NO registration call yet -- they are
     UNGUARANTEED and documented as such.
  L5 (new) registration is fail-open: every registration call is wrapped
     `|| true` at the call site so it can never break its surface. A silently
     failing registration = an obligation that never existed = silence. The
     wrapper's own stderr is the only trace.
  L6 (new) detection latency = sweep cadence: expiry fires on the next drive
     firing (period = TTL/4, today 1800s), so a TTL of 15min is detected up to
     ~45min after open.
  L7 (new) cron beats without --ttl are record-only: "didn't run" becomes
     visible on query (status), not by push. Push-on-missed-run requires the
     job to declare its cadence (DOGANY_TSL_BEAT_TTL env in its plist).
  L8 (new) concurrent appends rely on O_APPEND atomicity for short lines
     (single write() per event); pathological >4KB notes could interleave.
  L9 (DGN-1655) the drive's regress terminates OUTSIDE this machine. The
     sweep is driven by ONE framework-owned per-instance unit
     (<agent>.instance-drive, StartInterval = TTL/4), and its own liveness
     beat lets a RETURNING sweep report a gap it slept through; the health
     observer warns at TTL/2 while the label stays loaded. What no in-host
     unit can report is the death of the per-user scheduler itself (both the
     drive and the observer die with it). That needs an external deadman --
     a separate owned ticket, not covered here. The earlier two-leg drive
     (cron-guard after every wrapped job + dispatch-detached after every
     spawn) is GONE: whichever job ran often enough became the drive, and a
     feature timer masked a missing driver for months (DGN-1655).
  L10 (DGN-1655 F5) host sleep: a laptop that sleeps through a firing makes
     a wall-clock gap that the criterion counts red. `mono` is recorded per
     beat so a later analysis can tell sleep from a stalled drive; nothing
     here consumes it (owner F4: the sleep question ships open).

Env seams (tests only): DOGANY_TSL_LEDGER, DOGANY_TSL_INBOX, DOGANY_TSL_PUSH,
DOGANY_TSL_DEDUP_DIR, DOGANY_TSL_ENV.

Drive marker (NOT a test seam): DOGANY_TSL_DRIVE=1 is set ONLY in the
EnvironmentVariables of the rendered instance-drive unit. A sweep without it
still sweeps, but records under the diagnostic row instead of the reserved one.
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

# DGN-1655: the ONE drive policy value. Everything cadence-shaped is derived
# from it in drive_policy() -- the unit's StartInterval (period), the sweep
# throttle, the beat TTL and the health-observer warning age. There is no
# second editable 7200 anywhere; the unit renderer (routines/lib/drive-setup.sh)
# reads this through `terminal-state-ledger.py policy`, and a drive sweep whose
# rendered argv disagrees with it refuses to write the reserved beat.
TERMINAL_STATE_SWEEP_TTL_SECONDS = 7200

# The sweep is the ONLY recovery path for a SIGKILLed obligation (measured:
# TERM/INT/HUP all run the worker EXIT trap; KILL does not), so what wakes it
# decides whether the whole axis holds. Since DGN-1655 that is one owned
# per-instance unit, and its liveness row is RESERVED here (F2): cmd_beat /
# cmd_open / cmd_close refuse the id, and cmd_sweep writes it only under the
# drive marker. Every other sweep records under SWEEP_DIAG_ID, so the
# reserved row's owner is a property of this file, not of a call-site census.
SWEEP_BEAT_ID = "terminal-state-sweep"
SWEEP_BEAT_SURFACE = "sweep"
SWEEP_DIAG_ID = "terminal-state-sweep-diag"
SWEEP_DIAG_SURFACE = "sweep-diag"
DRIVE_ENV = "DOGANY_TSL_DRIVE"
DRIVE_NAME = "instance-drive"
SWEEP_LOCK = os.path.join(DEDUP_DIR, "sweep.lock")
SWEEP_LOCK_STALE = 900       # a sweep that has held the claim this long is dead

RC_RESERVED = 3              # a write to the reserved row was refused
RC_STALE_DRIVE = 4           # drive argv disagrees with the current policy


class PolicyError(ValueError):
    pass


def drive_policy(ttl=None):
    """Derive every drive number from ONE TTL read (DGN-1655 design + F1).

      period   = TTL/4   the unit's StartInterval: three nominal firings fit
                         between the last good beat and expiry
      throttle = period/2  F1: a throttle equal to the period silently
                         doubles the cadence (beat-at-end + scheduler jitter
                         lands the next firing inside the window); half a
                         period swallows only a genuine double-fire
      warn_age = TTL/2   health-observer early warning (two missed periods)

    Refuses (PolicyError) a TTL that is not a positive integer divisible by
    four -- the renderer must fail closed rather than guess a period."""
    if ttl is None:
        ttl = TERMINAL_STATE_SWEEP_TTL_SECONDS
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0 or ttl % 4:
        raise PolicyError(
            "TERMINAL_STATE_SWEEP_TTL_SECONDS must be a positive integer "
            "divisible by 4 (got %r)" % (ttl,))
    period = ttl // 4
    return {"ttl": ttl, "period": period, "throttle": period // 2,
            "warn_age": ttl // 2}


def drive_marker_set():
    return os.environ.get(DRIVE_ENV) == "1"


def is_reserved(oid, surface=None):
    return oid == SWEEP_BEAT_ID or surface == SWEEP_BEAT_SURFACE


def _refuse_reserved(verb):
    print(
        f"[terminal-state] {verb} refused: ({SWEEP_BEAT_ID}, "
        f"{SWEEP_BEAT_SURFACE}) is reserved for the instance-drive unit's "
        "sweep (DGN-1655 F2). Use another id/surface; a manual sweep records "
        f"under {SWEEP_DIAG_ID}.",
        file=sys.stderr,
    )
    return RC_RESERVED


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
    """Per-agent signature prefix, PADDED ('<emoji> ' or '') -- THE one
    resolver every notify reader uses (routines/lib/agent_prefix.py:
    .instance.conf DOGANY_AGENT_PREFIX -> persona '- Emoji:' -> empty).
    Never hardcode an emoji, never fall back to a placeholder (DGN-828);
    an unresolved prefix renders as NO prefix, with no stray spacing."""
    try:
        sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
        import agent_prefix
        return agent_prefix.padded(AGENT_ROOT)
    except Exception:
        return ""


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
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (dec-094
    # UX gate, same standing as the restart-backstop copy). Do not treat this
    # wording as final; a test pins this marker to the string.
    #
    # The itemized list and the ledger path are gone from the OWNER text. An
    # item line is nothing but identifiers (surface + run id, an evidence file
    # path, an overdue second count, and a note carrying an exit code) and the
    # ledger path is absolute. None of it is lost: notify_session above drops
    # the full itemization on the surface whose reader can act on it, and the
    # stderr line below keeps it in the maintainer's log.
    print(
        "[terminal-state] owner push carries a count only; items: "
        + " | ".join(_fmt_item(s, now) for s in fresh)
        + f" | ledger: {LEDGER_PATH}",
        file=sys.stderr,
    )
    text = (
        f"{_agent_prefix()}[종결 백스톱] 끝났다는 통보 없이 시간이 지난 비동기 "
        f"작업이 {len(fresh)}건 있어요. 제가 실제 결말을 확인하고 정리해서 "
        f"알려드릴게요."
    )
    cmd = ["/bin/bash", PUSH_SH, "--text", text, "--audience", "owner"]
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
    if is_reserved(a.id, a.surface):
        return _refuse_reserved("open")
    iso, epoch = now_pair()
    append_event({
        "v": 1, "event": "open", "id": a.id, "surface": a.surface,
        "at": iso, "at_epoch": epoch, "ttl_secs": a.ttl,
        "expires_epoch": (epoch + a.ttl) if a.ttl else None,
        "notify": a.notify, "note": a.note, "evidence": a.evidence,
    })
    return 0


def cmd_close(a):
    if is_reserved(a.id):
        return _refuse_reserved("close")
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
    if is_reserved(a.id, a.surface):
        return _refuse_reserved("beat")
    iso, epoch = now_pair()
    append_event({
        "v": 1, "event": "beat", "id": a.id, "surface": a.surface,
        "at": iso, "at_epoch": epoch, "mono": time.monotonic(),
        "rc": a.rc, "ttl_secs": a.ttl,
        "expires_epoch": (epoch + a.ttl) if a.ttl else None,
        "note": a.note,
    })
    return 0


def _sweep_claim(stale=SWEEP_LOCK_STALE):
    """Atomic single-sweeper claim. mkdir is the atomic primitive (same guard
    `finalize_run` uses for .finalized). The drive unit and a manual /
    diagnostic sweep CAN overlap, and a double sweep would mean a duplicate
    session-inbox drop, i.e. nag. Fail-OPEN: if the lock dir itself is unusable, sweeping
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


def _sweep_beat(drive, beat_ttl, at_epoch, policy=None):
    """The sweep's OWN liveness row, stamped at sweep START (DGN-1655 F1):
    stamping after _sweep_body put the notify round-trip into the beat time,
    so a slow sweep drifted the next firing into the throttle window.

    drive=True  -> the reserved (terminal-state-sweep, sweep) row, armed with
                   the policy TTL: if nothing sweeps for TTL the row goes
                   overdue and the NEXT sweep expires it like any other
                   abandoned obligation (the drive's own gap is observable).
    drive=False -> the record-only diagnostic row: a manual / test sweep is
                   evidence that someone swept, never production liveness.

    An append failure raises -> exit 2 through main(): no synthetic success."""
    iso = datetime.fromtimestamp(at_epoch).astimezone().strftime(
        "%Y-%m-%dT%H:%M:%S%z")
    if drive:
        ev = {
            "v": 1, "event": "beat", "id": SWEEP_BEAT_ID,
            "surface": SWEEP_BEAT_SURFACE, "at": iso, "at_epoch": at_epoch,
            "mono": time.monotonic(), "rc": 0, "ttl_secs": beat_ttl,
            "expires_epoch": at_epoch + beat_ttl,
            "period": policy["period"], "throttle": policy["throttle"],
            "note": "종결 스윕 구동 비트 -- 이 행이 만료됐다면 그 기간 동안 스윕이 돌지 않았다",
        }
    else:
        ev = {
            "v": 1, "event": "beat", "id": SWEEP_DIAG_ID,
            "surface": SWEEP_DIAG_SURFACE, "at": iso, "at_epoch": at_epoch,
            "mono": time.monotonic(), "rc": 0, "ttl_secs": None,
            "expires_epoch": None,
            "note": "diagnostic sweep (not the instance drive) -- record only",
        }
    append_event(ev)


def cmd_sweep(a):
    events, corrupt = read_events()
    st = fold(events)
    now = a.now if a.now else time.time()
    throttle = max(0, a.throttle or 0)
    drive = drive_marker_set()
    rc_extra = 0
    policy = None
    if drive:
        # F1/F2: the reserved row is written only when the rendered argv
        # still matches the ONE policy read. A stale unit (TTL changed, unit
        # not re-rendered/reloaded) still sweeps -- obligations must keep
        # expiring -- but records under the diagnostic row and exits nonzero
        # so cron-guard makes the stale drive loud.
        try:
            policy = drive_policy()
        except PolicyError as e:
            print(f"[terminal-state] drive sweep: {e}", file=sys.stderr)
            drive, rc_extra = False, RC_STALE_DRIVE
        else:
            if throttle != policy["throttle"] or a.beat_ttl != policy["ttl"]:
                print(
                    "[terminal-state] drive sweep: rendered argv "
                    f"(--throttle {throttle} --beat-ttl {a.beat_ttl}) does not "
                    f"match policy (throttle {policy['throttle']}, ttl "
                    f"{policy['ttl']}) -- stale unit; re-render with "
                    "routines/lib/drive-setup.sh install",
                    file=sys.stderr,
                )
                drive, rc_extra = False, RC_STALE_DRIVE
    row_id = SWEEP_BEAT_ID if drive else SWEEP_DIAG_ID
    if throttle:
        prev = st.get(row_id)
        if (prev and prev.get("status") == "beat" and prev.get("last_epoch")
                and now - prev["last_epoch"] < throttle):
            return rc_extra   # a sweep already ran this window -- no beat
        if not _sweep_claim():
            return rc_extra   # another sweeper holds the claim right now
    try:
        # Liveness is recorded for a sweep that STARTED, not one that
        # succeeded -- a sweep failing to notify is still evidence the drive
        # is alive; that failure is reported by the nonzero rc below.
        _sweep_beat(drive, a.beat_ttl, int(now), policy)
        rc = _sweep_body(st, now, corrupt)
    finally:
        if throttle:
            _sweep_release()
    return rc or rc_extra


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


def cmd_policy(a):
    """The policy-query CLI the unit renderer reads (DGN-1655 design step 3).
    rc 2 on an invalid TTL, so a renderer cannot proceed on a guess."""
    try:
        pol = drive_policy()
    except PolicyError as e:
        print(f"[terminal-state] policy: {e}", file=sys.stderr)
        return 2
    if a.as_json:
        print(json.dumps(pol, sort_keys=True))
    else:
        print(" ".join(f"{k}={pol[k]}" for k in
                       ("ttl", "period", "throttle", "warn_age")))
    return 0


def drive_label(root=None):
    """com.telegram-skill-bot.<agent>.instance-drive -- agent name from the
    instance manifest (mint.sh writes it), dirname fallback (routine-ctl.sh
    derivation)."""
    root = root or AGENT_ROOT
    name = ""
    try:
        with open(os.path.join(root, ".instance.conf"), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line.startswith("DOGANY_AGENT_NAME="):
                    name = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        pass
    name = name or os.path.basename(os.path.realpath(root))
    return f"com.telegram-skill-bot.{name}.{DRIVE_NAME}"


def drive_beats(events):
    """Reserved drive beats, in at_epoch order."""
    out = [ev for ev in events
           if ev.get("event") == "beat" and ev.get("id") == SWEEP_BEAT_ID
           and ev.get("surface") == SWEEP_BEAT_SURFACE]
    return sorted(out, key=lambda ev: ev.get("at_epoch") or 0)


def last_drive_beat_epoch(events=None):
    if events is None:
        events, _ = read_events()
    beats = drive_beats(events)
    return beats[-1].get("at_epoch") if beats else None


def check_ledger(events, since, until, label, qualify=False):
    """Ledger-only acceptance check (design "Ledger-only verification" + F7).

    Reads the reserved sweep row AND the drive unit's own cron row
    (surface=cron, id=<agent>.instance-drive, written by cron-guard). The
    sweep row decides green/red: every adjacent gap, and the trailing gap up
    to `until`, must be <= that row's ttl_secs. The cron row only diagnoses
    a red gap: no firing inside it = "the unit did not fire"; firings inside
    it = "the unit fired but the sweep was throttled / skipped / failed"."""
    problems = []
    beats = [b for b in drive_beats(events)
             if since <= (b.get("at_epoch") or 0) <= until]
    fires = sorted(
        (ev for ev in events
         if ev.get("event") == "beat" and ev.get("surface") == "cron"
         and ev.get("id") == label
         and since <= (ev.get("at_epoch") or 0) <= until),
        key=lambda ev: ev.get("at_epoch") or 0)

    def fires_between(lo, hi):
        return [f for f in fires if lo < (f.get("at_epoch") or 0) <= hi]

    def diagnose(lo, hi, lo_is_beat=True):
        # cron-guard writes the unit's cron row AFTER the wrapped sweep, so
        # the first cron row after a drive beat is that same firing's
        # completion, not a separate firing inside the gap.
        inside = fires_between(lo, hi)
        if lo_is_beat and inside:
            inside = inside[1:]
        if not inside:
            return "unit did not fire in the gap"
        rcs = sorted({f.get("rc") for f in inside}, key=str)
        return (f"unit fired {len(inside)}x in the gap (rc {rcs}) but no "
                "drive beat: sweep throttled, skipped or failed")

    gaps = []
    for b in beats:
        ttl = b.get("ttl_secs")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            problems.append(f"malformed ttl_secs {ttl!r} at {b.get('at')}")
    for prev, cur in zip(beats, beats[1:]):
        gap = cur["at_epoch"] - prev["at_epoch"]
        ttl = prev.get("ttl_secs")
        row = {"from": prev["at_epoch"], "to": cur["at_epoch"], "gap": gap,
               "ttl": ttl}
        if isinstance(ttl, int) and ttl > 0 and gap > ttl:
            row["red"] = diagnose(prev["at_epoch"], cur["at_epoch"])
            problems.append(f"gap {gap}s > ttl {ttl}s between {prev.get('at')} "
                            f"and {cur.get('at')}: {row['red']}")
        ct = cur.get("ttl_secs")
        if ct != ttl and isinstance(ct, int) and ct > 0:
            # A TTL transition is legitimate only when the new row carries
            # the rendered policy for ITS ttl (re-rendered unit, F1 shape).
            if (cur.get("period") != ct // 4
                    or cur.get("throttle") != (ct // 4) // 2):
                problems.append(
                    f"ttl changed {ttl}->{ct} at {cur.get('at')} without the "
                    "rendered policy transition (period/throttle)")
        gaps.append(row)
    if not beats:
        problems.append("no drive beat in the window: "
                        + diagnose(since, until, lo_is_beat=False))
    else:
        last = beats[-1]
        trail = until - last["at_epoch"]
        ttl = last.get("ttl_secs")
        if isinstance(ttl, int) and ttl > 0 and trail > ttl:
            problems.append(f"open gap {trail}s > ttl {ttl}s since "
                            f"{last.get('at')}: "
                            + diagnose(last["at_epoch"], until))
    window = until - since
    try:
        pol = drive_policy()
    except PolicyError as e:
        pol = None
        problems.append(f"policy: {e}")
    dispatch_opens = sum(
        1 for ev in events
        if ev.get("event") == "open" and ev.get("surface") == "dispatch"
        and since <= (ev.get("at_epoch") or 0) <= until)
    report = {
        "window": {"since": since, "until": until, "secs": window},
        "label": label, "drive_beats": len(beats), "unit_fires": len(fires),
        "max_gap": max([g["gap"] for g in gaps], default=None),
        "dispatch_opens": dispatch_opens, "gaps_red": [g for g in gaps
                                                       if "red" in g],
        "problems": problems,
    }
    if qualify and pol:
        opportunities = window // pol["period"] + 1
        report["opportunities"] = opportunities
        if window < 2 * pol["ttl"] or opportunities < 9:
            problems.append(f"window {window}s too short to qualify (need "
                            f">= 2*TTL = {2 * pol['ttl']}s, >= 9 firings)")
        if dispatch_opens:
            problems.append(f"{dispatch_opens} own-agent dispatch(es) inside "
                            "the window (criterion requires none)")
    report["green"] = not problems
    return report


def cmd_last_beat(a):
    """Epoch of the newest reserved drive beat (empty when none). Read by
    drive-setup.sh verify for the first-beat check."""
    last = last_drive_beat_epoch()
    if last is not None:
        print(int(last))
    return 0


def cmd_check(a):
    events, corrupt = read_events()
    until = a.until if a.until is not None else int(time.time())
    since = a.since if a.since is not None else 0
    label = a.label or drive_label()
    rep = check_ledger(events, since, until, label, qualify=a.qualify)
    if corrupt:
        rep["corrupt"] = corrupt
    if a.as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"drive check {'GREEN' if rep['green'] else 'RED'}: "
              f"{rep['drive_beats']} drive beat(s), {rep['unit_fires']} unit "
              f"firing(s) ({label}), max gap "
              f"{'n/a' if rep['max_gap'] is None else str(rep['max_gap']) + 's'}, window "
              f"{rep['window']['secs']}s")
        for prob in rep["problems"]:
            print(f"  - {prob}")
    return 0 if rep["green"] else 1


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

    pb = sub.add_parser("beat", help="record a liveness beat (cron surface; "
                        "the terminal-state-sweep row is reserved)")
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
                         "The drive unit passes the policy throttle "
                         "(period/2).")
    ps.add_argument("--beat-ttl", type=int, default=None,
                    help="seconds until the drive's liveness beat goes "
                         "overdue; the drive unit passes the policy TTL and "
                         "must match it (see `policy`)")
    ps.set_defaults(fn=cmd_sweep)

    pp = sub.add_parser("policy", help="print the drive policy derived from "
                        "TERMINAL_STATE_SWEEP_TTL_SECONDS (rc 2 if invalid)")
    pp.add_argument("--json", dest="as_json", action="store_true")
    pp.set_defaults(fn=cmd_policy)

    pk = sub.add_parser("check", help="ledger-only drive acceptance check: "
                        "every drive beat gap <= TTL (reads the sweep row and "
                        "the unit's cron row)")
    pk.add_argument("--since", type=int, default=None, help="window start epoch")
    pk.add_argument("--until", type=int, default=None,
                    help="window end epoch (default now; the trailing gap "
                         "counts)")
    pk.add_argument("--label", default=None,
                    help="drive unit label (default from .instance.conf)")
    pk.add_argument("--qualify", action="store_true",
                    help="also require a qualifying window (>= 2*TTL, >= 9 "
                         "firings, no own-agent dispatch)")
    pk.add_argument("--json", dest="as_json", action="store_true")
    pk.set_defaults(fn=cmd_check)

    pl = sub.add_parser("last-beat", help="epoch of the newest drive beat")
    pl.set_defaults(fn=cmd_last_beat)

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
