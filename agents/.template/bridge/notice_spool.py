"""DGN-1586 notice spool -- consumer/receipt side of the owner-notice
delivery accounting.

CONTRACT (DGN-1586 hardened spec, sections 3.1/3.4/3.5/3.6/5):

- The spool is the instance file .telegram_bot/state/notice-spool.jsonl,
  one JSON record per line. The PRODUCER side (routines/version-check.py,
  a standalone stdlib-only SessionStart hook that must not import bridge/)
  implements the SAME record schema and locking protocol independently --
  the shared contract is the FILE + LOCK protocol, not shared code (the
  established footer-sidecar precedent: status-footer.py writes, the
  bridge reads, no import either way). Any schema change here must land
  in version-check.py in the same change, and vice versa.

- Locking (spec MINOR-3): every read-modify-write runs under an exclusive
  flock on the SEPARATE fixed lock file .telegram_bot/state/
  notice-spool.lock; the jsonl itself is replaced atomically
  (tmp + os.replace) so a reader never sees a torn file. Network calls
  never happen inside the lock.

- Record statuses: pending -> delivered (real send receipt) or
  expired (reason: resolved / superseded / attempts_exhausted).
  "delivered" is owned EXCLUSIVELY by the send/edit success of the
  message that actually carried the notice text (spec 3.6) -- never by
  ChatResponse completion, first-segment success, or a stream draft.

- Corrupt lines are preserved verbatim on rewrite and logged (spec 5:
  valid rows survive, parse failures become diagnostics; repair is
  operator-owned).

This module is deliberately dependency-light: stdlib + lazy bridge
imports only, and every path parameter is explicit so tests can point it
at a scratch instance root.
"""

import asyncio
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Relative paths inside an instance root. Mirrored in
# routines/version-check.py (_SPOOL_RELATIVE_PATH / _SPOOL_LOCK_RELATIVE_PATH).
SPOOL_RELATIVE_PATH = os.path.join(".telegram_bot", "state", "notice-spool.jsonl")
SPOOL_LOCK_RELATIVE_PATH = os.path.join(".telegram_bot", "state", "notice-spool.lock")
LEDGER_RELATIVE_PATH = os.path.join(".telegram_bot", "outbound-ledger.jsonl")
SNAPSHOT_RELATIVE_PATH = os.path.join(".telegram_bot", "runtime-snapshot.json")
_SNAPSHOT_SCHEMA_KNOWN = 1

KIND_FW_UPDATE = "fw-update"
KIND_PENDING_RESTART = "pending-restart"

# DGN-735: delivered receipts retain context_injected_session_id, which the
# producer uses to suppress only that SessionStart session.  The consumer
# never treats elapsed time as an announcement gate.  Spec 5 MINOR-1: at most
# 3 synthesis reservations per round, then
# expired(attempts_exhausted) + operator alarm.
MAX_ATTEMPTS = 3


def spool_path(root: Path) -> Path:
    return Path(root) / SPOOL_RELATIVE_PATH


def _lock_path(root: Path) -> Path:
    return Path(root) / SPOOL_LOCK_RELATIVE_PATH


@contextmanager
def spool_lock(root: Path):
    """Exclusive flock on the fixed lock file (spec MINOR-3). The lock file
    is separate from the jsonl because the jsonl is replaced (os.replace)
    while held -- locking the replaced inode would drop mutual exclusion."""
    lp = _lock_path(root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lp, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def load_records(root: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Return (parsed records, unparseable raw lines). Caller holds the lock
    when this feeds a mutation. Missing file -> ([], [])."""
    records: List[Dict[str, Any]] = []
    bad: List[str] = []
    try:
        with open(spool_path(root), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        records.append(rec)
                    else:
                        bad.append(line)
                except Exception:
                    bad.append(line)
    except FileNotFoundError:
        return [], []
    if bad:
        logger.warning(
            "notice spool: %d unparseable line(s) preserved for "
            "operator repair", len(bad)
        )
    return records, bad


def dump_records(root: Path, records: List[Dict[str, Any]], bad: List[str]) -> None:
    """Atomic rewrite. Corrupt raw lines ride along verbatim at the tail so
    no operator-recoverable data is destroyed by a rewrite (spec 5)."""
    path = spool_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for line in bad:
            fh.write(line + "\n")
    os.replace(tmp, str(path))


# --- local state readers (mirrors of routines/version-check.py) ------------

def _read_conf(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip()
    except Exception:
        return {}
    return out


def built_version(root: Path) -> str:
    # ESTATE:BEGIN DGN-1586 the built-version source is the estate instance layout (.instance.conf DOGANY_FW_VERSION); the public build has no such file, and an empty value fails every notice judgment toward silence
    return _read_conf(Path(root) / ".instance.conf").get("DOGANY_FW_VERSION", "")
    # ESTATE:ELSE
    # return ""
    # ESTATE:END


def fw_reference(root: Path) -> str:
    """DGN-1668: the version a fw-update pending is judged AGAINST.

    built_version() returns the BASE version (DOGANY_FW_VERSION=2.6.0), which
    semver-outranks every 2.6.0-dev.N. Judging a dev-channel notice against
    it marks the notice 'resolved' at the first finalize pass, so the owner
    never receives it -- the producer creates, the consumer silently kills.
    A dev instance's real consumption point is the TAG it installed
    (DOGANY_FW_TAG=v2.6.0-dev.33), which update.sh stamps into the same
    manifest. Falls back to the base version when no tag stamp exists
    (pre-stamp instances), which is the pre-DGN-1668 behaviour exactly.

    Scoped to KIND_FW_UPDATE on purpose: pending-restart compares against
    the BOOT SNAPSHOT, which records DOGANY_FW_VERSION, so that predicate
    must keep using built_version."""
    # ESTATE:BEGIN DGN-1668 the consumed-tag stamp is the estate instance layout (.instance.conf DOGANY_FW_TAG); the public build has no such file and falls back to the built version
    tag = _read_conf(Path(root) / ".instance.conf").get("DOGANY_FW_TAG", "")
    return _norm(tag) or built_version(root)
    # ESTATE:ELSE
    # return built_version(root)
    # ESTATE:END


def _pid_alive(pid) -> Optional[bool]:
    try:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None
    return True


def snapshot_fw_version(root: Path) -> Optional[str]:
    """Boot snapshot fw_version, or None when the comparison must be skipped
    (absent / malformed / unknown schema / stale pid) -- the version-check
    reader's exact judgment, mirrored (spec 3.2: snapshot absence is never
    resolution evidence)."""
    try:
        with open(Path(root) / SNAPSHOT_RELATIVE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    try:
        if not isinstance(data, dict):
            return None
        if data.get("schema") != _SNAPSHOT_SCHEMA_KNOWN:
            return None
        if _pid_alive(data.get("pid")) is not True:
            return None
        fw = data.get("fw_version")
        if isinstance(fw, str) and fw.strip():
            return fw.strip()
        return None
    except Exception:
        return None


def _version_tuple(v: str):
    core = v.strip().split("-")[0].split("+")[0]
    parts = []
    for chunk in core.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == "":
            return None
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _prerelease_key(v: str):
    """DGN-1668: SemVer 2.0.0 rule 11.4 key, used only when the release
    cores tie. Mirror of version-check _prerelease_key -- the two sides
    judge the SAME spool records, so a divergence here means the producer
    creates a notice the consumer refuses (or the reverse)."""
    core = v.strip().lstrip("v").split("+")[0]
    _, sep, pre = core.partition("-")
    if not sep or not pre:
        return (1,)
    ids = []
    for ident in pre.split("."):
        if ident.isdigit():
            ids.append((0, int(ident)))
        else:
            ids.append((1, ident))
    return tuple([0] + ids)


def is_newer(candidate: str, current: str) -> bool:
    """Strictly-newer semver-ish comparison (version-check _is_newer mirror).

    DGN-1668: pre-release identifiers break a tie on the release core, so
    2.6.0-dev.38 outranks 2.6.0-dev.33. Suffix-free inputs key to (1,) on
    both sides and are unaffected."""
    ct = _version_tuple(candidate)
    cur = _version_tuple(current)
    if ct is None or cur is None:
        return candidate != current
    n = max(len(ct), len(cur))
    ct = ct + (0,) * (n - len(ct))
    cur = cur + (0,) * (n - len(cur))
    if ct != cur:
        return ct > cur
    return _prerelease_key(candidate) > _prerelease_key(current)


def _norm(version: str) -> str:
    return (version or "").strip().lstrip("v")


# --- consumer: finalize-seam selection + attempt reservation ----------------

def _kind_valid_now(rec: Dict[str, Any], built: str, snapshot: Optional[str],
                    fw_ref: Optional[str] = None) -> str:
    """Per-kind validity of a pending record against the freshest local
    state (spec 3.2). Returns one of:
      "valid"    -- eligible for synthesis this turn
      "resolved" -- the resolution condition is met -> expire(resolved)
      "hold"     -- neither (e.g. snapshot unknown, or a stale
                    pending-restart target awaiting producer supersede):
                    preserve the pending, skip synthesis this turn."""
    ver = _norm(rec.get("version") or "")
    if rec.get("kind") == KIND_FW_UPDATE:
        # DGN-1668: judged against the consumed TAG, not the base version.
        if not is_newer(ver, fw_ref or built):
            return "resolved"
        return "valid"
    if rec.get("kind") == KIND_PENDING_RESTART:
        # Resolution predicate is EXACTLY snapshot_fw == built (spec 3.2) --
        # never the fw-update "version <= built" predicate, which would
        # expire this kind at its very first synthesis (grill MAJOR-1).
        if snapshot is None:
            return "hold"
        if snapshot == built:
            return "resolved"
        if ver == _norm(built) and is_newer(built, snapshot):
            return "valid"
        # built moved since this record was created: the producer owns the
        # supersede; finalize holds the stale body (spec 3.2).
        return "hold"
    return "hold"


def reserve_for_synthesis(root: Path) -> Optional[Dict[str, Any]]:
    """Owner-turn finalize consumer (spec 3.5). Under the lock: re-read the
    freshest spool + local state, expire records whose kind-specific
    resolution condition is met, detect attempt exhaustion, pick the single
    oldest valid pending (first_pending_ts), and PRE-PERSIST its attempt
    increment before the caller appends anything to the reply. Returns

        {"record": <copy or None>, "attempt": <int or None>,
         "exhausted": [<records newly expired as attempts_exhausted>]}

    or None when nothing was done (no spool / unreadable built version /
    persist failure -- spec 5: a failed pre-record holds this turn's
    synthesis and keeps the pending for the next opportunity)."""
    root = Path(root)
    if not spool_path(root).is_file():
        return None
    built = built_version(root)
    if not built:
        # Cannot judge validity -> fail toward silence, mutate nothing.
        return None
    with spool_lock(root):
        records, bad = load_records(root)
        snapshot = snapshot_fw_version(root)
        fw_ref = fw_reference(root)
        now = time.time()
        changed = False
        exhausted: List[Dict[str, Any]] = []
        candidates: List[Dict[str, Any]] = []
        for rec in records:
            if rec.get("status") != "pending":
                continue
            verdict = _kind_valid_now(rec, built, snapshot, fw_ref)
            if verdict == "resolved":
                rec["status"] = "expired"
                rec["expired_reason"] = "resolved"
                rec["expired_ts"] = now
                changed = True
                continue
            if verdict != "valid":
                continue
            attempts = rec.get("attempts") or 0
            if attempts >= MAX_ATTEMPTS:
                # Spec 5 MINOR-1: exhaustion converts to an operator alarm;
                # the terminal record blocks auto re-creation of the key.
                rec["status"] = "expired"
                rec["expired_reason"] = "attempts_exhausted"
                rec["expired_ts"] = now
                changed = True
                exhausted.append(dict(rec))
                continue
            if not ((rec.get("body_fold") or rec.get("body_oneline") or "").strip()):
                continue  # nothing to append; leave for diagnostics
            candidates.append(rec)
        # Spec 3.4: a strictly newer same-kind pending shelves the older
        # body (oldest-first must not resurrect a superseded-in-waiting
        # target before the producer closes it).
        selectable = [
            rec for rec in candidates
            if not any(
                other is not rec
                and other.get("kind") == rec.get("kind")
                and is_newer(_norm(other.get("version") or ""),
                             _norm(rec.get("version") or ""))
                for other in candidates
            )
        ]
        picked: Optional[Dict[str, Any]] = None
        if selectable:
            picked = min(selectable, key=lambda r: r.get("first_pending_ts") or 0.0)
            picked["attempts"] = (picked.get("attempts") or 0) + 1
            picked["last_attempt_ts"] = now
            changed = True
        if changed:
            try:
                dump_records(root, records, bad)
            except Exception:
                # Pre-record failed: the notice must NOT ride this turn
                # (spec 5 row 2). Expiries are lost too -- next pass redoes
                # them from the same predicates.
                logger.exception("notice spool pre-record failed")
                return None
        if picked is None and not exhausted:
            return None
        return {
            "record": dict(picked) if picked else None,
            "attempt": picked.get("attempts") if picked else None,
            "exhausted": exhausted,
        }


def promote_delivered(
    root: Path,
    notice_id: str,
    chat_id: int,
    message_id: int,
    method: str,
    attempt: Optional[int] = None,
    related_message_ids: Optional[List[int]] = None,
) -> bool:
    """Receipt processor (spec 3.6): re-read the spool by id and record the
    REAL carrier receipt. Idempotent on re-processing; a late valid receipt
    after exhaustion still records the real delivery fact; a receipt never
    promotes any other round's id (id match only)."""
    root = Path(root)
    with spool_lock(root):
        records, bad = load_records(root)
        target = None
        for rec in records:
            if rec.get("id") == notice_id:
                target = rec
                break
        if target is None:
            logger.error(
                "receipt for unknown notice id %s (chat %s msg %s)",
                notice_id, chat_id, message_id,
            )
            return False
        if target.get("status") == "delivered":
            return True  # idempotent: first receipt wins, duplicates no-op
        if target.get("status") == "expired":
            # Late receipt after exhaustion processing: the send DID happen.
            target["late_receipt"] = True
        target["status"] = "delivered"
        target["delivered_ts"] = time.time()
        target["chat_id"] = chat_id
        target["message_id"] = message_id
        target["delivery_method"] = method
        if attempt is not None:
            target["delivered_attempt"] = attempt
        if related_message_ids:
            target["related_message_ids"] = list(related_message_ids)
        dump_records(root, records, bad)
        return True


def append_ledger(root: Path, row: Dict[str, Any]) -> None:
    """Append one outbound-ledger row (push.sh _record_outbound mirror for
    the bot response path, spec 3.6). Fail-soft: a lost record must never
    turn a delivered notice into a failure."""
    try:
        path = Path(root) / LEDGER_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("outbound-ledger append failed")


REARM_PROCEDURE = (
    "check the outbound ledger and the target chat for the partial sends -> "
    "fix the failure cause -> under the same notice-spool.lock flock keep "
    "the exhausted round closed (set rearmed=true on it) and append ONE new "
    "pending record for the same kind/version with a new id and attempts=0"
)


async def send_exhausted_alarm(root: Path, rec: Dict[str, Any]) -> None:
    """Spec 5 MINOR-1: attempts exhaustion converts to an OPERATOR alarm
    (DGN-1585 audience axis), never an owner-channel message. Routed like
    bot._send_machine_line_alert: steward env resolved -> push.sh
    --audience operator; no steward -> log only. Network runs outside the
    spool lock by construction (caller schedules this as a task)."""
    try:
        root = Path(root)
        receipt = (
            "message_id=%s" % rec.get("message_id")
            if rec.get("message_id") else "none"
        )
        text = (
            "[notice-spool] delivery attempts exhausted\n"
            "id=%s kind=%s version=%s attempts=%s receipt=%s\n"
            "auto re-creation for this key is blocked. manual re-arm: %s"
            % (
                rec.get("id"), rec.get("kind"), rec.get("version"),
                rec.get("attempts"), receipt, REARM_PROCEDURE,
            )
        )
        # ESTATE:BEGIN DGN-1586 operator-audience alarm routing resolves the estate steward topology and push.sh audience axis (DGN-1585, same delivery path as bot._send_machine_line_alert); the public build logs the exhaustion only
        from bridge.machine_gate import steward_env_file

        steward = steward_env_file(root / "config" / "agent.conf")
        if steward is None:
            logger.error(
                "DGN-1586 attempts exhausted, NO steward configured -- "
                "log only: %s", text.replace("\n", " | "),
            )
            return
        env = dict(os.environ)
        # Suppresses the recursive alert hop inside push.sh (the alarm body
        # itself may look machine-shaped) -- same guard as the bot layer's
        # machine-line alert.
        env["DOGANY_MACHINE_ALERT_HOP"] = "1"
        proc = await asyncio.create_subprocess_exec(
            "bash", str(root / "routines" / "push.sh"),
            "--audience", "operator", "--env", str(steward),
            "--text", text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        if proc.returncode != 0:
            logger.error(
                "DGN-1586 exhausted-alarm operator push failed rc=%s: %s",
                proc.returncode,
                (err or b"")[-300:].decode("utf-8", "replace"),
            )
        # ESTATE:ELSE
        # logger.error(
        #     "notice delivery attempts exhausted: %s",
        #     text.replace("\n", " | "),
        # )
        # ESTATE:END
    except Exception:
        logger.exception("exhausted-alarm dispatch failed")
