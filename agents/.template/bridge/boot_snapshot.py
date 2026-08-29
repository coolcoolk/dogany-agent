"""DGN-986 [b]: boot-time runtime snapshot.

At bridge boot (right before the "Bot is running" marker log) write
BOT_DATA_DIR/runtime-snapshot.json describing what THIS process actually
loaded: pid, boot time, framework version, per-kit engine versions, the resolved
Claude CLI, and the sha256 of the three config surfaces the process consumed
(instance .env / shared .rules/.env / newbridge launchd plist).

Readers (the /health command and version-check.py's third comparison) diff
these hashes against the current on-disk files to detect "config changed but
the process was never restarted" (the Ag incident class) and "framework
updated but not consumed".

Locked schema (DGN-986 spec, schema=1; engine field amended to a per-kit map
by Metal decision pre-release -- a scalar max loses WHICH kit's tier it is):
  schema / label / pid / started_at / fw_version / bridge_version /
  engine_versions {db_stem: user_version} /
  claude_cli {resolved_path, version, install_method} /
  env_files [{path, sha256}] x3

The lock is on the MEANING of a field and on the fixed env_files order, not on
the key count: `bridge_version` was ADDED at schema 1 (DGN-818 C3). Both
readers -- healthcmd.read_runtime_snapshot and routines/version-check.py
_read_snapshot_fw_version -- address fields by name and reject an UNKNOWN
schema number outright, so bumping the number for a purely additive key would
have made every not-yet-updated reader in the fleet step aside from a snapshot
it can read perfectly well. A key that changes meaning still requires a bump.

Invariants (boot-path first principles):
  - ALL failures are absorbed: a snapshot problem must never block the boot.
    write_runtime_snapshot() logs ONE WARNING line and returns None on any
    error.
  - Secrets safety: only paths and sha256 digests are recorded -- never file
    contents, never key names.
  - Atomic publish: tmp file + os.replace, so a reader can never observe a
    partially written snapshot.
  - Engine DB access is strictly read-only (sqlite URI mode=ro, R5): no
    WAL/SHM side effects on live databases.
"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bridge import config as config_mod

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA = 1
SNAPSHOT_FILENAME = "runtime-snapshot.json"
# DGN-888/DGN-964 service-plist marker (written by watchdog_setup.sh, verified
# and self-healed by watchdog.sh from launchd truth): line 1 = absolute path
# of the loaded launchd plist, line 2 = launchd Label.
SERVICE_PLIST_MARKER_NAME = ".service_plist"
CLI_VERSION_TIMEOUT_S = 2.0
CLAUDE_INSTALL_INFO_PATH = Path.home() / ".claude.json"


def _read_service_marker(data_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    """(plist_path, label) from the DGN-888 marker; (None, None) when absent."""
    marker = data_dir / SERVICE_PLIST_MARKER_NAME
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    plist_path = lines[0].strip() if len(lines) >= 1 and lines[0].strip() else None
    label = lines[1].strip() if len(lines) >= 2 and lines[1].strip() else None
    return plist_path, label


def _resolve_label(data_dir: Path) -> Optional[str]:
    """launchd label: marker line 2 first, then the launchd-provided env."""
    _, label = _read_service_marker(data_dir)
    if label:
        return label
    # launchd exports XPC_SERVICE_NAME to its children on macOS; "0" appears
    # in some non-launchd contexts and is not a label.
    env_label = os.environ.get("XPC_SERVICE_NAME", "").strip()
    if env_label and env_label != "0":
        return env_label
    return None


def _read_fw_version(project_root: Path) -> Optional[str]:
    """DOGANY_FW_VERSION from <root>/.instance.conf (same key self_restart.sh
    reads). Quotes are tolerated; absent file/key -> None."""
    conf = project_root / ".instance.conf"
    try:
        text = conf.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("DOGANY_FW_VERSION="):
            value = line.split("=", 1)[1].strip().strip("\"'").strip()
            return value or None
    return None


def _bridge_version() -> Optional[str]:
    """The running bridge's generation string (DGN-818 C3).

    Absorbs everything: a snapshot problem must never block the boot, and a
    pre-C3 bridge tree simply has no such attribute.
    """
    try:
        import bridge as _bridge_pkg

        value = getattr(_bridge_pkg, "__version__", None)
        return value if isinstance(value, str) and value.strip() else None
    except Exception:  # pragma: no cover - boot-path absorption
        return None


def _read_engine_versions(project_root: Path) -> Dict[str, int]:
    """{db stem: PRAGMA user_version} across <root>/database/*.db, read-only.

    Per-kit map, NOT a scalar: the consumer is /health's engine-tier line,
    which reports tiers per kit (e.g. lifekit v29) -- collapsing to one
    number would lose which kit's tier it is. No databases -> {}.

    R5: the instance's engine DBs live under database/ (a root-level
    lifekit.db may be a stale legacy copy -- never read it). Access is a
    read-only sqlite URI, and MUST NOT create WAL/SHM side-effect files on a
    live database. Measured: plain mode=ro on a WAL-mode db still creates
    -wal/-shm on open, so the URI is branched:
      - no -wal present (quiescent db): mode=ro&immutable=1 -- creates
        nothing at all; user_version lives in the header page, safe.
      - -wal present (hot db): plain mode=ro -- the side files already
        exist, so the reader creates nothing new and sees WAL state.
    """
    versions: Dict[str, int] = {}
    db_dir = project_root / "database"
    try:
        candidates = sorted(db_dir.glob("*.db"))
    except OSError:
        return versions
    for db in candidates:
        try:
            base_uri = db.resolve().as_uri()  # percent-encodes odd path chars
            if Path(f"{db}-wal").exists():
                uri = f"{base_uri}?mode=ro"
            else:
                uri = f"{base_uri}?mode=ro&immutable=1"
            con = sqlite3.connect(uri, uri=True, timeout=1.0)
            try:
                row = con.execute("PRAGMA user_version").fetchone()
            finally:
                con.close()
            if row is not None:
                versions[db.stem] = int(row[0])
        except (sqlite3.Error, OSError, ValueError, TypeError):
            continue  # one unreadable db must not spoil the field
    return versions


def _resolve_claude_cli() -> Dict[str, Optional[str]]:
    """Resolve the effective Claude CLI -- SAME rule as bridge/__main__.py
    _selfcheck (DGN-674 F9): explicit CLAUDE_CLI_PATH wins (no fallback when
    it is configured but missing, mirroring selfcheck's FAIL), else PATH
    lookup, else ~/.local/bin/claude."""
    resolved: Optional[str] = None
    cli = config_mod.CLAUDE_CLI_PATH
    if cli:
        p = Path(cli).expanduser()
        resolved = str(p) if p.exists() else None
    else:
        which = shutil.which("claude")
        if which:
            resolved = which
        else:
            local_bin = Path.home() / ".local" / "bin" / "claude"
            if local_bin.exists():
                resolved = str(local_bin)

    version: Optional[str] = None
    if resolved:
        try:
            proc = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=CLI_VERSION_TIMEOUT_S,
            )
            if proc.returncode == 0:
                version = proc.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            version = None

    install_method: Optional[str] = None
    try:
        info = json.loads(CLAUDE_INSTALL_INFO_PATH.read_text(encoding="utf-8"))
        raw = info.get("installMethod")
        if isinstance(raw, str) and raw.strip():
            install_method = raw.strip()
    except (OSError, ValueError):
        install_method = None

    return {
        "resolved_path": resolved,
        "version": version,
        "install_method": install_method,
    }


def _env_file_entry(path: Optional[Path]) -> Dict[str, Optional[str]]:
    """{path, sha256} for one config surface. sha256 of the file BYTES only --
    no contents, no key names (secrets safety). Missing/unreadable -> null
    sha256; unresolvable path -> null path."""
    if path is None:
        return {"path": None, "sha256": None}
    digest: Optional[str] = None
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        digest = None
    return {"path": str(path), "sha256": digest}


def _build_snapshot() -> Dict[str, Any]:
    project_root = config_mod.PROJECT_ROOT
    data_dir = config_mod.BOT_DATA_DIR
    plist_path, _ = _read_service_marker(data_dir)

    # env_files order is FIXED (schema=1): instance .env / shared .rules/.env /
    # own newbridge plist. Paths follow config.py's actual load order
    # (PROJECT_ENV_PATH / DOGANY_ENV_PATH) and the DGN-888 marker.
    env_files = [
        _env_file_entry(config_mod.PROJECT_ENV_PATH),
        _env_file_entry(config_mod.DOGANY_ENV_PATH),
        _env_file_entry(Path(plist_path) if plist_path else None),
    ]

    return {
        "schema": SNAPSHOT_SCHEMA,
        "label": _resolve_label(data_dir),
        "pid": os.getpid(),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fw_version": _read_fw_version(project_root),
        # DGN-818 C3: which bridge generation this process actually loaded.
        # Read from the imported package, not from a file, so it describes the
        # RUNNING code rather than what happens to be on disk right now.
        "bridge_version": _bridge_version(),
        "engine_versions": _read_engine_versions(project_root),
        "claude_cli": _resolve_claude_cli(),
        "env_files": env_files,
    }


def _atomic_write_json(target: Path, payload: Dict[str, Any]) -> None:
    """Publish atomically: tmp file in the SAME directory + os.replace, so a
    reader never sees a partial snapshot. The tmp file is removed on failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{SNAPSHOT_FILENAME}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_runtime_snapshot() -> Optional[Path]:
    """Write BOT_DATA_DIR/runtime-snapshot.json. NEVER raises.

    Boot-path first principle: any failure here is absorbed and reported as a
    single WARNING log line; the bridge boot proceeds untouched. Returns the
    snapshot path on success, None on failure.
    """
    try:
        target = config_mod.BOT_DATA_DIR / SNAPSHOT_FILENAME
        _atomic_write_json(target, _build_snapshot())
        return target
    except Exception as e:  # noqa: BLE001 -- absorb everything (boot path)
        logger.warning("runtime snapshot skipped: %s", e)
        return None
