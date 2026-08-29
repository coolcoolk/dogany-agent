"""DGN-986 [c]: /health bridge command -- the estate health surface.

NAMING: bridge/health.py is the PRE-EXISTING network polling watchdog
(getUpdates conflict/restart plumbing) and has nothing to do with this
module. The /health command logic lives here, in healthcmd.py, by spec
(DGN-986 section 1 naming warning). Never merge the two.

What /health shows (spec section 3 + 7(na), owner-approved copy skeleton):
  - SELF block: uptime (ps lstart), framework version, per-kit engine
    versions, Claude CLI freshness (resolved real path + version + install
    method, best-effort live latest-version lookup -- 2s timeout, failure is
    reported HONESTLY as a failed lookup, never papered over with a cache;
    DGN-962).
  - ESTATE block: every com.telegram-skill-bot.*.newbridge.plist under
    ~/Library/LaunchAgents, instance root resolved from ProgramArguments
    "--path" (R4 -- WorkingDirectory is useless: all instances share
    /Users/<user>). Per instance: PID/up, framework version, per-kit engine
    versions, config-vs-process integrity:
      * primary: runtime-snapshot.json sha256 diff (assertive tone), part [b]
      * fallback (snapshot absent/stale): config mtime vs process lstart --
        over-detection is possible, so the copy DOWNGRADES to "check needed",
        never asserts (spec section 3 axis 3 limit).
  - JOBS block: health-observer state (part [a], ~/.dogany/health-observer/
    jobs.json). warn-level records summarized, info-level records counted
    only. The observer's own heartbeat is ALWAYS shown (the observer's
    observer, DGN-985): a missing or stale jobs.json is itself a "check
    needed" item -- /health must never say "all normal" while the thing
    that watches for problems is dead (the ticket's core failure mode).
  - INSTALL choice: last record of ~/.dogany/claude-install-choice (part [d]
    contract) -- informational cross-check only, never authoritative over
    the live resolution (DGN-962 cache-first ban).

Hard rules honored here:
  - READ-ONLY everywhere: no writes to any other instance's files. Engine DB
    access reuses boot_snapshot._read_engine_versions (mode=ro / immutable
    URI branching, R5) -- no WAL/SHM side effects, no duplicate implementation.
  - Display names resolve via sot/MANAGED-TARGETS.md (slug/path -> display
    name); unregistered instances fall back to their real directory name --
    never a raw label or a full path in user-facing lines.
  - Counts are computed, never constants (grill F3).
  - No session, no model call (_cmd_usage pattern); the handler in bot.py
    runs build_health_report() via asyncio.to_thread (R6: worst case ~3s of
    subprocess + network must not stall the event loop).
  - CTA: every problem line carries a self-explanatory sentence CTA; the
    recommended action is also wired as ONE "opt:" callback button in the
    handler (the only session-reaching button rail -- IDRILL taps fire an
    argv subprocess and cannot reach the session; spec section 5 correction).
  - Telegram contract: no # headers, no raw md tables, no CJK code-block
    grids; technical numbers are folded via formatting.compose_fold_block.

User-facing copy note: the report copy follows the owner-approved section
7(na) skeleton. Strings that had no approved skeleton line (observer-down,
lookup-failed, etc.) keep the same tone and are dec-094 UX-gate items,
flagged in the build handoff.
"""

import hashlib
import json
import os
import plistlib
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from bridge import config as config_mod
from bridge import boot_snapshot
from bridge.formatting import compose_fold_block

LABEL_PREFIX = "com.telegram-skill-bot."
NEWBRIDGE_GLOB = LABEL_PREFIX + "*.newbridge.plist"

# Observer cadence: the shipped plist runs health-observer.py every 300s.
# Heartbeat older than ~3 missed polls (+ 1 min slack) = the observer is not
# running -- surfaced as a "check needed" item, never silently ignored.
OBSERVER_POLL_INTERVAL_S = 300
OBSERVER_STALE_AFTER_S = OBSERVER_POLL_INTERVAL_S * 3 + 60

# Mirrors health-observer.py notify thresholds (routines side; the bridge must
# not import routines, so the two small constants are duplicated knowingly).
STRUCTURAL_EXITS = frozenset((126, 127, 78))
GENERAL_STREAK_NOTIFY = 3
STRUCTURAL_STREAK_NOTIFY = 2

# Live latest-version lookup for the Claude CLI (best-effort, 2s budget).
# DGN-962: a failed lookup is reported as a failed lookup -- no cached value
# is ever presented as the live answer.
LATEST_CLI_URL = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"
LATEST_CLI_TIMEOUT_S = 2.0

SUBPROCESS_TIMEOUT_S = 5.0

DEFAULT_REGISTRY_PATH = Path(
    os.environ.get(
        "DOGANY_MANAGED_TARGETS",
        os.path.expanduser("~/dogany/dev-crew/sot/MANAGED-TARGETS.md"),
    )
)
# Same env override the observer itself honors, so /health and the observer
# always read the same state file.
DEFAULT_OBSERVER_STATE_PATH = Path(
    os.environ.get(
        "DOGANY_HEALTH_OBSERVER_STATE_DIR",
        os.path.expanduser("~/.dogany/health-observer"),
    )
) / "jobs.json"
DEFAULT_INSTALL_CHOICE_PATH = Path(
    os.path.expanduser("~/.dogany/claude-install-choice")
)


# ---------------------------------------------------------------------------
# probes (subprocess / network layer -- NOT exercised by unit tests)
# ---------------------------------------------------------------------------

def _probe_launchctl_list() -> str:
    proc = subprocess.run(
        ["launchctl", "list"],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _probe_ps_lstart(pids: List[int]) -> str:
    if not pids:
        return ""
    proc = subprocess.run(
        ["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S,
    )
    return proc.stdout or ""


def _probe_latest_cli_version() -> Optional[str]:
    """Live latest CLI version from the npm registry. Raises on any failure
    (caller reports the lookup as failed -- honestly)."""
    with urllib.request.urlopen(LATEST_CLI_URL, timeout=LATEST_CLI_TIMEOUT_S) as r:
        data = json.loads(r.read().decode("utf-8"))
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("no version field in registry response")
    return version.strip()


# ---------------------------------------------------------------------------
# pure parsers
# ---------------------------------------------------------------------------

def parse_launchctl_pids(text: str) -> Dict[str, Optional[int]]:
    """``launchctl list`` -> {label: pid or None} for our prefix.

    Tiny twin of health-observer.parse_launchctl_list (the bridge does not
    import routines/). Format: PID<TAB>Status<TAB>Label, "-" when not running.
    """
    out: Dict[str, Optional[int]] = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_raw, _status, label = parts[0].strip(), parts[1], parts[2].strip()
        if not label.startswith(LABEL_PREFIX):
            continue
        try:
            out[label] = int(pid_raw)
        except ValueError:
            out[label] = None
    return out


def parse_ps_lstart(text: str) -> Dict[int, datetime]:
    """``ps -o pid=,lstart=`` -> {pid: start datetime}. Unparseable lines are
    skipped (a dead pid simply does not appear)."""
    out: Dict[int, datetime] = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            out[pid] = datetime.strptime(parts[1].strip(), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            continue
    return out


def parse_newbridge_plist(path: Path) -> Optional[Dict[str, Any]]:
    """One newbridge plist -> {label, root}. Root comes from ProgramArguments
    "--path" ONLY (R4: WorkingDirectory is identical across every instance and
    resolves nothing). Unreadable / no --path -> None."""
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return None
    label = data.get("Label")
    args = data.get("ProgramArguments") or []
    root: Optional[str] = None
    for i, a in enumerate(args):
        if a == "--path" and i + 1 < len(args):
            root = args[i + 1]
            break
    if not label or not root:
        return None
    return {"label": str(label), "root": str(root), "plist_path": str(path)}


def parse_registry(text: str) -> Dict[str, str]:
    """MANAGED-TARGETS.md tables -> resolution map.

    Keys: "slug:<slug lowercased>" and "path:<realpath>" -> short display
    name (text before " (" so "Kim Metal (Metal Kim)" collapses to the
    persona name). Table rows look like:
        | slug | display | emoji | crew | role | `path` | endpoint | state |
    Header/separator rows are skipped structurally (no format guessing).
    """
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        slug, display = cells[0], cells[1]
        if not slug or slug.lower() == "slug" or set(slug) <= set(":- "):
            continue
        short = display.split(" (")[0].strip()
        if not short:
            continue
        out["slug:" + slug.lower()] = short
        path_cell = cells[5]
        if "`" in path_cell:
            try:
                raw = path_cell.split("`")[1].strip()
                if raw.startswith("/"):
                    out["path:" + os.path.realpath(raw)] = short
            except IndexError:
                pass
    return out


def resolve_display_name(registry: Dict[str, str], root: str, label: str) -> str:
    """Registry-first display-name resolution; unregistered -> the real
    directory name (spec: 실명 폴백) -- never a slug-path or full path."""
    hit = registry.get("path:" + os.path.realpath(root))
    if hit:
        return hit
    slug = label[len(LABEL_PREFIX):] if label.startswith(LABEL_PREFIX) else label
    hit = registry.get("slug:" + slug.lower())
    if hit:
        return hit
    return Path(root).name


def parse_install_choice(text: str) -> Optional[Dict[str, str]]:
    """Last record of the append-only ~/.dogany/claude-install-choice file
    (part [d] contract: key=value lines, blank-line separated records).
    Informational only -- NEVER authoritative over the live resolution."""
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in (text or "").splitlines() + [""]:
        line = line.strip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            current[k.strip()] = v.strip()
    return records[-1] if records else None


# ---------------------------------------------------------------------------
# collection helpers (filesystem-pure -- tmp-dir testable)
# ---------------------------------------------------------------------------

def read_runtime_snapshot(root: Path, launchd_pid: Optional[int]) -> Tuple[Optional[Dict], Optional[str]]:
    """(snapshot, invalid_reason). snapshot is returned only when USABLE:
    readable, known schema, and pid matches the current launchd pid (a stale
    pid means the file describes a dead process -- crash-restart cover).
    Unusable -> (None, reason) and the caller falls back to mtime."""
    path = Path(root) / ".telegram_bot" / boot_snapshot.SNAPSHOT_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "absent"
    except Exception:
        return None, "unreadable"
    if not isinstance(data, dict):
        return None, "unreadable"
    schema = data.get("schema")
    if schema != boot_snapshot.SNAPSHOT_SCHEMA:
        # Unknown (newer) schema: quietly step aside (forward compat).
        return None, "unknown-schema"
    if launchd_pid is not None and data.get("pid") != launchd_pid:
        return None, "stale-pid"
    return data, None


def _sha256_file(path: str) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%-m/%-d %H:%M") if dt else "?"


def _uptime_text(lstart: Optional[datetime], now: datetime) -> str:
    if lstart is None:
        return "가동시간 미확인"
    delta = now - lstart
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 60:
        span = "%d분" % minutes
    elif minutes < 48 * 60:
        span = "%d시간" % (minutes // 60)
    else:
        span = "%d일" % (minutes // (24 * 60))
    return "가동 %s (%s부터)" % (span, _fmt_dt(lstart))


def _engine_segment(engine_versions: Dict[str, int]) -> Optional[str]:
    """Per-kit engine tiers, NEVER collapsed (grill F8 / snapshot schema
    correction: a scalar loses which kit's tier it is). Empty map -> None
    (instances without engine DBs omit the segment entirely, spec 7(na))."""
    if not engine_versions:
        return None
    parts = ["%s v%s" % (stem, ver) for stem, ver in sorted(engine_versions.items())]
    return "엔진 " + ", ".join(parts)


def assess_instance(
    *,
    display: str,
    root: str,
    label: str,
    launchd_pid: Optional[int],
    lstart: Optional[datetime],
    shared_env_path: Optional[str],
    plist_path: Optional[str],
    now: datetime,
) -> Dict[str, Any]:
    """One instance -> {display, up, fw_version, engine_versions, integrity,
    issues[], tech[]}. All reads; every failure degrades to an honest note."""
    inst: Dict[str, Any] = {
        "display": display,
        "root": root,
        "label": label,
        "pid": launchd_pid,
        "lstart": lstart,
        "up": launchd_pid is not None,
        "fw_version": None,
        # DGN-818 C3: which bridge GENERATION that instance is running. Only a
        # live boot snapshot can answer it for a remote instance (its bridge
        # package is not importable from here), so this stays None until the
        # snapshot is read below -- "not reported" and "same as mine" must not
        # look alike.
        "bridge_version": None,
        "engine_versions": {},
        "integrity": "unknown",   # snapshot | mtime-ok | mtime-drift | snapshot-drift | unknown
        "issues": [],
        "tech": [],
    }
    root_p = Path(root)
    try:
        inst["fw_version"] = boot_snapshot._read_fw_version(root_p)
    except Exception:
        pass
    try:
        # Reused from part [b]: mode=ro / immutable URI branching -- strictly
        # read-only, creates no WAL/SHM files on another agent's live DB (R5).
        inst["engine_versions"] = boot_snapshot._read_engine_versions(root_p)
    except Exception:
        inst["engine_versions"] = {}

    if not inst["up"]:
        inst["issues"].append({
            "severity": "warn",
            "short": "%s 브릿지 다운" % display,
            "explain": "%s 브릿지가 지금 떠 있지 않아요. 재기동이 필요합니다." % display,
            "note": "브릿지가 떠 있지 않아요 -- 재기동 필요",
            "cta": "%s 재기동해줘" % display,
            "cta_desc": "브릿지를 다시 띄웁니다.",
            "tech": ["%s: launchd pid 없음 (label %s)" % (display, label)],
        })
        return inst

    snapshot, snap_reason = read_runtime_snapshot(root_p, launchd_pid)
    if snapshot is not None:
        inst["integrity"] = "snapshot"
        snap_bridge = snapshot.get("bridge_version")
        if isinstance(snap_bridge, str) and snap_bridge.strip():
            inst["bridge_version"] = snap_bridge.strip()
        changed: List[str] = []
        for entry in snapshot.get("env_files") or []:
            path, recorded = entry.get("path"), entry.get("sha256")
            if not path or not recorded:
                continue
            current = _sha256_file(path)
            if current != recorded:
                changed.append(path)
        snap_fw = snapshot.get("fw_version")
        if changed:
            # Primary detection (precise) -- assertive tone is allowed.
            inst["integrity"] = "snapshot-drift"
            inst["issues"].append({
                "severity": "warn",
                "short": "%s 재기동 필요" % display,
                "explain": ("%s 설정이 바뀌었는데 프로세스가 아직 옛 설정으로 돌고 "
                            "있어요. 재기동해야 새 설정을 씁니다." % display),
                "note": "설정 변경됨 · 프로세스 미반영 -- 재기동 필요",
                "cta": "%s 재기동해줘" % display,
                "cta_desc": "새 설정으로 다시 띄웁니다.",
                "tech": ["%s: 스냅샷 sha 불일치 -- %s" % (display, ", ".join(changed))],
            })
        if snap_fw and inst["fw_version"] and snap_fw != inst["fw_version"]:
            inst["integrity"] = "snapshot-drift"
            inst["issues"].append({
                "severity": "warn",
                "short": "%s 업데이트 미반영" % display,
                "explain": ("%s가 업데이트(v%s)는 받았는데 아직 v%s로 돌고 있어요. "
                            "재기동하면 반영됩니다." % (display, inst["fw_version"], snap_fw)),
                "note": "업데이트 미반영 (파일 v%s / 프로세스 v%s) -- 재기동 필요"
                        % (inst["fw_version"], snap_fw),
                "cta": "%s 재기동해줘" % display,
                "cta_desc": "새 버전으로 다시 띄웁니다.",
                "tech": ["%s: fw 파일 v%s vs 프로세스 v%s"
                         % (display, inst["fw_version"], snap_fw)],
            })
        inst["tech"].append("%s: 정합검사 = runtime-snapshot sha 대조" % display)
        return inst

    # --- mtime fallback (secondary, over-detection possible -> tone DOWN,
    # "check needed", never an assertion; spec section 3 axis 3 limit) ---
    inst["tech"].append(
        "%s: 스냅샷 %s -> mtime 폴백 (부트 스냅샷 이전 세대)" % (display, snap_reason))
    if lstart is None:
        inst["integrity"] = "unknown"
        inst["tech"].append("%s: lstart 수집 실패 -- 정합검사 보류" % display)
        return inst
    candidates = [
        str(Path(root) / ".telegram_bot" / ".env"),
        shared_env_path,
        plist_path,
    ]
    newer: List[Tuple[str, datetime]] = []
    for c in candidates:
        if not c:
            continue
        try:
            mtime = datetime.fromtimestamp(Path(c).stat().st_mtime)
        except OSError:
            continue
        if mtime > lstart:
            newer.append((c, mtime))
    if newer:
        inst["integrity"] = "mtime-drift"
        latest = max(m for _, m in newer)
        inst["issues"].append({
            "severity": "check",
            "short": "%s 확인 필요" % display,
            "explain": ("%s는 설정이 바뀐 뒤(%s) 재기동 이력이 없어요. 확인이 "
                        "필요합니다." % (display, _fmt_dt(latest))),
            "note": "설정이 바뀐 뒤(%s) 재기동 이력이 없어요" % _fmt_dt(latest),
            "cta": "%s 재기동해줘" % display,
            "cta_desc": "확인 후 새 설정으로 다시 띄웁니다.",
            "tech": ["%s: mtime > lstart(%s) -- %s"
                     % (display, _fmt_dt(lstart),
                        ", ".join("%s(%s)" % (p, _fmt_dt(m)) for p, m in newer))],
        })
    else:
        inst["integrity"] = "mtime-ok"
    return inst


def assess_jobs(
    state: Optional[Dict[str, Any]],
    state_error: Optional[str],
    now: datetime,
    display_by_slug: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Observer state -> jobs block. The observer's own liveness is part of
    the assessment (the observer's observer): a missing or stale heartbeat is
    a first-class "check needed" item -- /health must never report all-normal
    on the word of a dead observer."""
    block: Dict[str, Any] = {
        "present": state is not None,
        "heartbeat": None,
        "stale": False,
        "warn_items": [],
        "info_count": 0,
        "counts": {},
        "issues": [],
        "tech": [],
    }
    if state is None:
        block["issues"].append({
            "severity": "check",
            "short": "잡 관측자 미가동",
            "explain": ("스케줄 작업을 지키는 관측자가 안 돌고 있어요(%s). 잡 "
                        "상태는 지금 아무도 보고 있지 않습니다."
                        % (state_error or "상태 파일 없음")),
            "cta": "헬스 관측자 상태 봐줘",
            "cta_desc": "관측자가 왜 안 도는지 확인해 보고드립니다.",
            "tech": ["observer: jobs.json %s" % (state_error or "absent")],
        })
        return block

    hb: Optional[datetime] = None
    hb_raw = state.get("heartbeat_at")
    if isinstance(hb_raw, str):
        try:
            hb = datetime.fromisoformat(hb_raw).replace(tzinfo=None)
        except ValueError:
            hb = None
    block["heartbeat"] = hb
    if hb is None or (now - hb).total_seconds() > OBSERVER_STALE_AFTER_S:
        block["stale"] = True
        block["issues"].append({
            "severity": "check",
            "short": "잡 관측자 침묵",
            "explain": ("잡 관측자의 마지막 확인이 %s에 멈춰 있어요. 관측자가 "
                        "죽었을 수 있습니다 -- 아래 잡 상태는 그 시각 기준입니다."
                        % (_fmt_dt(hb) if hb else "알 수 없는 시각")),
            "cta": "헬스 관측자 상태 봐줘",
            "cta_desc": "관측자가 왜 멈췄는지 확인해 보고드립니다.",
            "tech": ["observer: heartbeat %s, 허용 %ds 초과"
                     % (hb_raw, OBSERVER_STALE_AFTER_S)],
        })

    counts = state.get("counts") or {}
    block["counts"] = counts

    # Info-level records are COUNTED, never enumerated (spec: info = count
    # only) -- 59 live not_loaded lines would blow the fold past Telegram's
    # message limit and drown the signal. The fold gets per-kind/per-instance
    # count summaries instead.
    info_counts: Dict[str, Dict[str, int]] = {}
    for w in state.get("warnings") or []:
        if w.get("level") == "warn":
            block["warn_items"].append(w)
        else:
            block["info_count"] += 1
            kind = w.get("kind") or "info"
            label = w.get("label") or "-"
            rest = (label[len(LABEL_PREFIX):]
                    if label.startswith(LABEL_PREFIX) else label)
            inst = rest.split(".")[0]
            info_counts.setdefault(kind, {})
            info_counts[kind][inst] = info_counts[kind].get(inst, 0) + 1
    for kind, by_inst in sorted(info_counts.items()):
        total = sum(by_inst.values())
        parts = ", ".join("%s %d" % (i, c) for i, c in sorted(by_inst.items()))
        block["tech"].append("info %s: %d건 (%s)" % (kind, total, parts))

    # Ongoing fail streaks / resident-down / losses persist in the jobs map;
    # /health is the always-on surface for them (push fires once per episode).
    for label, entry in sorted((state.get("jobs") or {}).items()):
        if entry.get("retired"):
            continue
        if entry.get("lost_at"):
            block["warn_items"].append({
                "kind": "label_lost", "label": label,
                "detail": "적재돼 있던 잡이 사라졌어요 (%s)" % entry.get("lost_at"),
            })
            continue
        if entry.get("down_since"):
            block["warn_items"].append({
                "kind": "resident_down", "label": label,
                "detail": "상주 서비스가 %s부터 떠 있지 않아요" % entry.get("down_since"),
            })
            continue
        streak = entry.get("fail_streak") or 0
        if streak <= 0:
            continue
        structural = entry.get("last_exit") in STRUCTURAL_EXITS
        threshold = STRUCTURAL_STREAK_NOTIFY if structural else GENERAL_STREAK_NOTIFY
        if streak >= threshold:
            block["warn_items"].append({
                "kind": "fail_streak", "label": label,
                "detail": "연속 %d회 실패 (exit %s)" % (streak, entry.get("last_exit")),
            })
        else:
            block["info_count"] += 1
            block["tech"].append("info: %s 실패 %d회 (임계 미만)" % (label, streak))

    parse_errors = state.get("parse_errors") or []
    if parse_errors:
        # R3: a parse failure is itself surfaced -- the observer must not be
        # able to die quietly inside /health either.
        block["warn_items"].append({
            "kind": "parse_errors", "label": "-",
            "detail": "관측 파싱 실패 %d건 (기술 상세 참고)" % len(parse_errors),
        })
        for pe in parse_errors:
            block["tech"].append("parse_error: %s -- %s" % (pe.get("label"), pe.get("error")))

    _JOB_WARN_COPY = {
        "reboot_nonsurvivor": "재부팅하면 사라져요 (자동 적재 설정 없음)",
        "plist_divergence": "재부팅하면 지금과 다른 설정으로 떠요",
    }
    _JOB_WARN_KIND = {
        "reboot_nonsurvivor": "재부팅 비생존",
        "plist_divergence": "설정 갈라짐",
        "fail_streak": "반복 실패",
        "resident_down": "상주 다운",
        "label_lost": "소실",
        "parse_errors": "관측 파싱 실패",
    }
    for w in block["warn_items"]:
        label = w.get("label", "-")
        friendly = _friendly_job(label, display_by_slug)
        rest = label[len(LABEL_PREFIX):] if label.startswith(LABEL_PREFIX) else label
        # Suffix-less labels are the resident bridges themselves (R2 bridge
        # bucket) -- calling them a "job" reads wrong to the owner.
        unit = "작업" if "." in rest else "브릿지"
        subject = "%s %s" % (friendly, unit)
        copy = _JOB_WARN_COPY.get(w.get("kind"))
        detail = w.get("detail") or ""
        block["issues"].append({
            "severity": "warn",
            "short": "%s %s" % (subject, _JOB_WARN_KIND.get(
                w.get("kind"), w.get("kind", "이상"))),
            "explain": copy or detail or "이상이 관측됐어요",
            "cta": "%s 상태 봐줘" % subject,
            "cta_desc": "원인을 확인해 조치 경로까지 보고드립니다.",
            "tech": ["%s: %s -- %s" % (w.get("kind"), label, detail)],
        })
    return block


def _friendly_job(label: str, display_by_slug: Optional[Dict[str, str]] = None) -> str:
    """User-facing name for a launchd label: the instance part resolves to
    its registry display name when known (never a raw slug for a registered
    instance), the job suffix stays as-is."""
    if not label.startswith(LABEL_PREFIX):
        return label
    rest = label[len(LABEL_PREFIX):]
    parts = rest.split(".")
    inst = parts[0]
    if display_by_slug:
        inst = display_by_slug.get(parts[0].lower(), inst)
    if len(parts) > 1:
        return "%s %s" % (inst, " ".join(parts[1:]))
    return inst


def assess_self_cli(
    cli: Dict[str, Optional[str]],
    auto_updates: Optional[bool],
    install_choice: Optional[Dict[str, str]],
    latest: Optional[str],
    latest_error: Optional[str],
) -> Dict[str, Any]:
    """CLI freshness (963-B axis). Live resolution is authoritative; the
    recorded install choice is a cross-check only (contract lock, DGN-962)."""
    out: Dict[str, Any] = {"cli": cli, "issues": [], "tech": [], "segment": None}
    resolved = cli.get("resolved_path")
    version = (cli.get("version") or "").split()[0] if cli.get("version") else None
    method = cli.get("install_method")

    path_looks_npm = bool(resolved) and (
        "node_modules" in resolved or "/npm" in resolved or "npm-global" in resolved)

    if not resolved:
        out["segment"] = "CLI 해소 실패"
        out["issues"].append({
            "severity": "warn",
            "short": "CLI 해소 실패",
            "explain": "claude CLI 실행 경로를 찾지 못했어요. 브릿지 발화가 곧 실패합니다.",
            "cta": "CLI 경로 점검해줘",
            "cta_desc": "CLI 설치 상태를 확인해 수리안을 보고드립니다.",
            "tech": ["cli: 해소 실패 (CLAUDE_CLI_PATH/PATH/~/.local/bin 전부 부재)"],
        })
    elif method == "native" and not path_looks_npm:
        seg = "CLI 네이티브"
        if version:
            seg += " v" + version
        if auto_updates is True:
            seg += " (자동업데이트 켜짐)"
        elif auto_updates is False:
            seg += " (자동업데이트 꺼짐)"
        out["segment"] = seg
    else:
        label = "npm" if (method in ("global", "local") or path_looks_npm) else (
            method or "미확인")
        out["segment"] = "CLI %s 방식%s ⚠️" % (label, (" v" + version) if version else "")
        out["issues"].append({
            "severity": "warn",
            "short": "CLI 설치 방식 확인 필요",
            "explain": ("claude CLI가 %s 방식이라 자동 업데이트가 안 돼요. 시간이 "
                        "지나면 CLI만 조용히 구버전에 뒤처집니다." % label),
            "cta": "CLI 네이티브로 이관해줘",
            "cta_desc": "공식 네이티브 설치로 옮기고 연결까지 확인합니다.",
            "tech": ["cli: method=%s path=%s" % (method, resolved)],
        })

    out["tech"].append("CLI 해소: %s (%s)" % (resolved or "없음", method or "미확인"))
    if latest is not None:
        if version and latest != version:
            out["tech"].append("CLI 최신 조회: v%s (현재 v%s)" % (latest, version))
        else:
            out["tech"].append("CLI 최신 조회: v%s = 최신" % latest)
    else:
        # DGN-962: honest failure -- never substitute a cached answer.
        out["tech"].append("CLI 최신버전 조회 실패 (%s)" % (latest_error or "네트워크"))

    if install_choice:
        cls = install_choice.get("classify")
        dec = install_choice.get("decision")
        # unknown classify was recorded without asking -> info, not a warning
        # (part [d] contract). The live judgment above already decided warn/ok.
        out["tech"].append("설치 판별 기록: classify=%s decision=%s (참고용 -- 실시간 판정 우선)"
                           % (cls, dec))
    else:
        out["tech"].append("설치 판별 기록 없음 -> 실시간 판정 사용")
    return out


# ---------------------------------------------------------------------------
# collection orchestration
# ---------------------------------------------------------------------------

def collect_report(
    *,
    now: Optional[datetime] = None,
    launch_agents_dir: Optional[Path] = None,
    launchctl_list_output: Optional[str] = None,
    ps_lstart_output: Optional[str] = None,
    self_root: Optional[Path] = None,
    registry_path: Optional[Path] = None,
    observer_state_path: Optional[Path] = None,
    install_choice_path: Optional[Path] = None,
    shared_env_path: Optional[str] = None,
    cli_info: Optional[Dict[str, Optional[str]]] = None,
    auto_updates: Optional[bool] = None,
    latest_cli_fetcher: Optional[Callable[[], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Gather everything (blocking; the handler runs this in a worker thread).
    Every argument is injectable for tests; defaults read the live system.
    READ-ONLY: this function writes nothing anywhere."""
    now = now or datetime.now()
    launch_agents_dir = Path(launch_agents_dir or
                             (Path.home() / "Library" / "LaunchAgents"))
    self_root = Path(self_root or config_mod.PROJECT_ROOT)
    registry_path = Path(registry_path or DEFAULT_REGISTRY_PATH)
    observer_state_path = Path(observer_state_path or DEFAULT_OBSERVER_STATE_PATH)
    install_choice_path = Path(install_choice_path or DEFAULT_INSTALL_CHOICE_PATH)
    if shared_env_path is None and config_mod.DOGANY_ENV_PATH is not None:
        shared_env_path = str(config_mod.DOGANY_ENV_PATH)

    # --- enumeration (R4) ---
    plists: List[Dict[str, Any]] = []
    try:
        for p in sorted(launch_agents_dir.glob(NEWBRIDGE_GLOB)):
            info = parse_newbridge_plist(p)
            if info:
                plists.append(info)
    except OSError:
        pass

    if launchctl_list_output is None:
        try:
            launchctl_list_output = _probe_launchctl_list()
        except Exception:
            launchctl_list_output = ""
    pids_by_label = parse_launchctl_pids(launchctl_list_output)

    pids = sorted({p for p in pids_by_label.values() if p} | {os.getpid()})
    if ps_lstart_output is None:
        try:
            ps_lstart_output = _probe_ps_lstart(pids)
        except Exception:
            ps_lstart_output = ""
    lstart_by_pid = parse_ps_lstart(ps_lstart_output)

    registry: Dict[str, str] = {}
    try:
        registry = parse_registry(registry_path.read_text(encoding="utf-8"))
    except OSError:
        registry = {}

    # --- instances (self split out of the estate) ---
    self_real = os.path.realpath(str(self_root))
    self_entry: Optional[Dict[str, Any]] = None
    estate: List[Dict[str, Any]] = []
    for info in plists:
        pid = pids_by_label.get(info["label"])
        inst = None
        try:
            inst = assess_instance(
                display=resolve_display_name(registry, info["root"], info["label"]),
                root=info["root"],
                label=info["label"],
                launchd_pid=pid,
                lstart=lstart_by_pid.get(pid) if pid else None,
                shared_env_path=shared_env_path,
                plist_path=info["plist_path"],
                now=now,
            )
        except Exception as e:  # one broken instance must not sink the report
            inst = {
                "display": resolve_display_name(registry, info["root"], info["label"]),
                "root": info["root"], "label": info["label"], "pid": pid,
                "lstart": None, "up": pid is not None, "fw_version": None,
                "bridge_version": None,
                "engine_versions": {}, "integrity": "unknown",
                "issues": [], "tech": ["%s: 수집 실패 (%s)" % (info["label"], e)],
            }
        if os.path.realpath(info["root"]) == self_real:
            self_entry = inst
        else:
            estate.append(inst)

    if self_entry is None:
        # Not in the plist enumeration (dev run): assess self directly.
        pid = os.getpid()
        self_entry = assess_instance(
            display=resolve_display_name(registry, str(self_root), ""),
            root=str(self_root), label="(self)", launchd_pid=pid,
            lstart=lstart_by_pid.get(pid), shared_env_path=shared_env_path,
            plist_path=None, now=now,
        )

    # DGN-818 C3: for SELF (and only self) the running package is importable,
    # so a missing/stale snapshot must not report "generation unknown" about
    # the very process answering the question.
    if not self_entry.get("bridge_version"):
        self_entry["bridge_version"] = boot_snapshot._bridge_version()

    # --- self CLI freshness ---
    if cli_info is None:
        cli_info = boot_snapshot._resolve_claude_cli()
        if auto_updates is None:
            try:
                info = json.loads(
                    boot_snapshot.CLAUDE_INSTALL_INFO_PATH.read_text(encoding="utf-8"))
                raw = info.get("autoUpdates")
                auto_updates = raw if isinstance(raw, bool) else None
            except (OSError, ValueError):
                auto_updates = None

    install_choice = None
    try:
        install_choice = parse_install_choice(
            install_choice_path.read_text(encoding="utf-8"))
    except OSError:
        install_choice = None

    latest = None
    latest_error: Optional[str] = None
    fetch = latest_cli_fetcher or _probe_latest_cli_version
    try:
        latest = fetch()
    except Exception as e:
        latest, latest_error = None, e.__class__.__name__

    cli_block = assess_self_cli(cli_info, auto_updates, install_choice,
                                latest, latest_error)

    # --- observer state ---
    state: Optional[Dict[str, Any]] = None
    state_error: Optional[str] = None
    try:
        raw = json.loads(observer_state_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            state = raw
        else:
            state_error = "상태 파일 형식 이상"
    except FileNotFoundError:
        state_error = "상태 파일 없음"
    except Exception:
        state_error = "상태 파일 읽기 실패"
    display_by_slug: Dict[str, str] = {}
    for inst in [self_entry] + estate:
        lbl = inst.get("label") or ""
        if lbl.startswith(LABEL_PREFIX):
            display_by_slug[lbl[len(LABEL_PREFIX):].lower()] = inst["display"]
    jobs_block = assess_jobs(state, state_error, now, display_by_slug)

    return {
        "now": now,
        "self": self_entry,
        "cli": cli_block,
        "estate": estate,
        "jobs": jobs_block,
    }


# ---------------------------------------------------------------------------
# compose (pure -- report dict in, (text, ctas) out)
# ---------------------------------------------------------------------------

def _instance_line(inst: Dict[str, Any]) -> str:
    segs: List[str] = []
    issue = inst["issues"][0] if inst.get("issues") else None
    if issue is not None:
        marker = "⚠️ " + ("재기동 필요" if issue["severity"] == "warn" and inst["up"]
                          else ("다운" if not inst["up"] else "확인 필요"))
        segs.append(marker)
        segs.append(issue.get("note") or issue["explain"])
    else:
        segs.append("정상")
        if inst.get("fw_version"):
            segs.append("v%s" % inst["fw_version"])
        eng = _engine_segment(inst.get("engine_versions") or {})
        if eng:
            segs.append(eng)
        if inst.get("lstart"):
            segs.append("%s부터" % _fmt_dt(inst["lstart"]))
    return "· %s -- %s" % (inst["display"], " · ".join(segs))


def compose_health_report(report: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Owner-approved section 7(na) layout: verdict on top, self block,
    estate block (issues sorted first), jobs block, sentence CTAs, fold.
    All-normal collapses to the short form. Every count is computed (F3)."""
    now = report["now"]
    self_e = report["self"]
    cli = report["cli"]
    estate = report["estate"]
    jobs = report["jobs"]

    issues: List[Dict[str, Any]] = []
    issues += self_e.get("issues") or []
    issues += cli.get("issues") or []
    for inst in estate:
        issues += inst.get("issues") or []
    issues += jobs.get("issues") or []

    tech: List[str] = []
    for src in ([self_e] + estate):
        tech += src.get("tech") or []
    tech += cli.get("tech") or []
    tech += jobs.get("tech") or []
    for iss in issues:
        tech += iss.get("tech") or []

    # engine tier note (kits differ across instances by design)
    tiers = []
    for inst in [self_e] + estate:
        eng = _engine_segment(inst.get("engine_versions") or {})
        if eng:
            tiers.append("%s %s" % (inst["display"], eng))
    if tiers:
        tech.append("엔진 티어는 킷 구성별로 달라요 (킷별 상이 = 정상): " + " / ".join(tiers))

    # bridge generation (DGN-818 C3). Before C3 the shipped bridge could not
    # answer "which generation am I" at all -- __version__ had zero consumers.
    # Instances with no live snapshot are named as UNREPORTED rather than
    # omitted: a silent absence would read as agreement.
    gens, unreported = [], []
    for inst in [self_e] + estate:
        gen = inst.get("bridge_version")
        if gen:
            gens.append("%s %s" % (inst["display"], gen))
        else:
            unreported.append(inst["display"])
    if gens or unreported:
        seg = "브릿지 세대: " + (" / ".join(gens) if gens else "보고 없음")
        if unreported:
            seg += " (미보고: %s)" % ", ".join(unreported)
        tech.append(seg)

    hb = jobs.get("heartbeat")
    hb_text = ("관측자 최종 확인 %s" % hb.strftime("%H:%M")) if hb else "관측자 확인 이력 없음"

    ctas: List[str] = []
    cta_lines: List[str] = []
    for iss in issues:
        c = iss.get("cta")
        if c and c not in ctas:
            ctas.append(c)
            cta_lines.append("· \"%s\" -- %s" % (c, iss.get("cta_desc") or ""))

    # --- self line ---
    self_segs = [_uptime_text(self_e.get("lstart"), now)]
    if self_e.get("fw_version"):
        self_segs.append("프레임워크 v%s" % self_e["fw_version"])
    eng = _engine_segment(self_e.get("engine_versions") or {})
    if eng:
        self_segs.append(eng)
    if cli.get("segment"):
        self_segs.append(cli["segment"])
    self_line = " · ".join(self_segs)

    fold = compose_fold_block("기술 상세", "\n".join(tech))

    # --- all-normal short form ---
    if not issues:
        n = len(estate)
        fw_set = sorted({i.get("fw_version") for i in estate if i.get("fw_version")})
        if n and len(fw_set) == 1 and all(i.get("fw_version") for i in estate):
            estate_txt = "에스테이트 %d기 전부 정상 (전원 v%s)" % (n, fw_set[0])
        elif n:
            estate_txt = "에스테이트 %d기 전부 정상" % n
        else:
            estate_txt = "에스테이트 관측 대상 없음"
        sched = (jobs.get("counts") or {}).get("scheduled")
        jobs_txt = ("스케줄 작업 %d개 전부 정상" % sched) if sched else "스케줄 작업 이상 없음"
        lines = [
            "✅ 전부 정상",
            "%s %s" % (self_e["display"], self_line),
            " · ".join([estate_txt, jobs_txt, hb_text]),
        ]
        if fold:
            lines.append(fold)
        return "\n".join(lines), []

    # --- issues present ---
    lines = []
    if len(issues) == 1:
        lines.append("⚠️ 확인 필요 1건 -- %s" % issues[0]["short"])
        lines.append(issues[0]["explain"])
    else:
        lines.append("⚠️ 확인 필요 %d건" % len(issues))
        for iss in issues:
            lines.append("· %s -- %s" % (iss["short"], iss["explain"]))
    lines.append("")
    lines.append("📌 %s -- %s" % (self_e["display"],
                                  "정상" if not (self_e.get("issues") or cli.get("issues"))
                                  else "확인 필요"))
    lines.append(self_line)
    lines.append("")

    n = len(estate)
    n_bad = sum(1 for i in estate if i.get("issues"))
    if n:
        head = "📋 에스테이트 %d기 -- " % n
        head += ("%d기 정상 · %d기 확인 필요" % (n - n_bad, n_bad)) if n_bad else "전부 정상"
        lines.append(head)
        ordered = sorted(estate, key=lambda i: (0 if i.get("issues") else 1,
                                                i["display"]))
        for inst in ordered:
            lines.append(_instance_line(inst))
        lines.append("")

    n_job_warn = sum(1 for i in jobs.get("issues") or [] if i["severity"] == "warn")
    sched = (jobs.get("counts") or {}).get("scheduled")
    if not jobs.get("present") or jobs.get("stale"):
        state_bit = "⚠️ 관측자가 돌고 있지 않아요"
    elif n_job_warn:
        state_bit = "이상 %d건" % n_job_warn
    elif sched:
        state_bit = "스케줄 %d개 전부 정상" % sched
    else:
        state_bit = "이상 없음"
    lines.append("📋 스케줄 작업 -- %s · %s" % (state_bit, hb_text))
    if jobs.get("info_count"):
        lines.append("· 참고 항목 %d건 (기술 상세 참고)" % jobs["info_count"])
    lines.append("")

    if cta_lines:
        lines.append("조치하려면 이렇게 말씀해 주세요 (그대로 보내면 제가 알아듣습니다):")
        lines += cta_lines
    if fold:
        lines.append(fold)
    return "\n".join(lines), ctas


def build_health_report() -> Tuple[str, List[str]]:
    """Entry point for the /health handler. Blocking (subprocess + 2s network
    budget); MUST be called via asyncio.to_thread (R6)."""
    return compose_health_report(collect_report())
