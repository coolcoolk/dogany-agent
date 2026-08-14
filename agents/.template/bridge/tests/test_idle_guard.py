"""DGN-328: idle guard tests for bridge/self_restart.sh.

The guard refuses restart when the user is mid-session (last jsonl activity
within --idle-mins, default 10) and fails-open (proceeds with warning) when
the transcript dir or jsonl files are absent.

Strategy: shell out to self_restart.sh with a fake HOME that contains (or
lacks) transcript fixtures at the expected encoded path. The script's push.sh
executable check is satisfied either by the real push.sh (tokenized live
instance) or by a stub created at the placeholder-relative path (canonical
template, where PUSH is the literal "__PROJECT_ROOT__/routines/push.sh").
The --dry-run flag prevents any real kill/launchctl invocation, so these
tests are side-effect-free.

The encoded instance root is DERIVED from the script's own location
(DGN-696 fix -- it was hardcoded to an old workspace path): the script
computes `cd "$(dirname "$0")/.." && pwd` then replaces every
non-alphanumeric character with '-'; we mirror that rule here so the test
follows the workspace wherever it lives.
"""

import os
import re
import subprocess
import time
from pathlib import Path

# Absolute path to the script under test.
SCRIPT = Path(__file__).resolve().parents[1] / "self_restart.sh"

# The encoded workspace root -- must match the script's own encoding rule
# (instance_root = script dir's parent; sed 's/[^a-zA-Z0-9]/-/g').
_INSTANCE_ROOT = SCRIPT.parent.parent
ENCODED_ROOT = re.sub(r"[^a-zA-Z0-9]", "-", str(_INSTANCE_ROOT))
TRANSCRIPT_SUBPATH = f".claude/projects/{ENCODED_ROOT}"


def _script_push_path() -> str:
    """Parse the PUSH= assignment from the script.

    Tokenized live instance: an absolute path to the real push.sh.
    Canonical template: the literal '__PROJECT_ROOT__/routines/push.sh'
    placeholder, which bash resolves RELATIVE to the process cwd.
    """
    for line in SCRIPT.read_text().splitlines():
        m = re.match(r'^PUSH="([^"]+)"', line)
        if m:
            return m.group(1)
    raise AssertionError("PUSH= assignment not found in self_restart.sh")


def _make_fake_home(tmp_path: Path) -> Path:
    """Create a fake HOME directory tree."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    return fake_home


def _run(fake_home: Path, extra_args: list[str]) -> subprocess.CompletedProcess:
    """Run self_restart.sh under the fake HOME, capturing output."""
    env = os.environ.copy()
    env["HOME"] = str(fake_home)

    # Working dir for the script. When PUSH is the template placeholder
    # (relative path), drop an executable stub at that cwd-relative location
    # so the [[ -x "$PUSH" ]] check passes without touching anything real.
    workdir = fake_home.parent / "cwd"
    workdir.mkdir(exist_ok=True)
    push = _script_push_path()
    if not os.path.isabs(push):
        stub = workdir / push
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("#!/bin/bash\nexit 0\n")
        stub.chmod(0o755)

    # Pass --env pointing at an empty .env so a real push.sh (live instance)
    # fails gracefully instead of sending a Telegram message (notify()
    # swallows push.sh failures).
    fake_env = fake_home / "fake.env"
    fake_env.write_text("")

    cmd = [
        "bash",
        str(SCRIPT),
        "--reason", "test-idle-guard",
        "--env", str(fake_env),
        "--dry-run",  # never kills anything; also exercises the guard path
        *extra_args,
    ]
    return subprocess.run(
        cmd,
        env=env,
        cwd=workdir,
        capture_output=True,
        text=True,
    )


def _place_jsonl(fake_home: Path, age_seconds: float) -> Path:
    """Create a transcript jsonl file with a specific mtime (age in seconds ago)."""
    transcript_dir = fake_home / TRANSCRIPT_SUBPATH
    transcript_dir.mkdir(parents=True, exist_ok=True)
    jsonl = transcript_dir / "session-abc.jsonl"
    jsonl.write_text('{"type":"text","text":"hello"}\n')
    target_mtime = time.time() - age_seconds
    os.utime(jsonl, (target_mtime, target_mtime))
    return jsonl


# ---------------------------------------------------------------------------
# Case 1: recent activity -> refused (exit 1)
# ---------------------------------------------------------------------------

def test_recent_activity_is_refused(tmp_path):
    fake_home = _make_fake_home(tmp_path)
    # Activity 30 seconds ago; threshold default is 10 minutes = 600s -> refused.
    _place_jsonl(fake_home, age_seconds=30)

    result = _run(fake_home, [])

    assert result.returncode == 1, (
        f"Expected exit 1 (refused), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "REFUSED" in result.stderr
    assert "Use --force to override" in result.stderr


# ---------------------------------------------------------------------------
# Case 2: recent activity + --force -> proceeds (exit 0, dry-run)
# ---------------------------------------------------------------------------

def test_recent_activity_with_force_proceeds(tmp_path):
    fake_home = _make_fake_home(tmp_path)
    _place_jsonl(fake_home, age_seconds=30)

    result = _run(fake_home, ["--force"])

    # dry-run exits 0 after the notify path (guard is skipped by --force)
    assert result.returncode == 0, (
        f"Expected exit 0 (force skips guard), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "--force set; skipping idle guard" in result.stdout or \
           "--force set; skipping idle guard" in result.stderr


# ---------------------------------------------------------------------------
# Case 3: old activity -> proceeds (exit 0, dry-run)
# ---------------------------------------------------------------------------

def test_old_activity_proceeds(tmp_path):
    fake_home = _make_fake_home(tmp_path)
    # Activity 20 minutes ago; default threshold is 10 minutes -> OK.
    _place_jsonl(fake_home, age_seconds=20 * 60)

    result = _run(fake_home, [])

    assert result.returncode == 0, (
        f"Expected exit 0 (idle guard OK), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "idle guard OK" in result.stdout or "idle guard OK" in result.stderr


# ---------------------------------------------------------------------------
# Case 4: missing transcript dir -> proceeds with warning (exit 0, dry-run)
# ---------------------------------------------------------------------------

def test_missing_transcript_dir_proceeds_with_warning(tmp_path):
    fake_home = _make_fake_home(tmp_path)
    # Do NOT create the transcript dir at all.

    result = _run(fake_home, [])

    assert result.returncode == 0, (
        f"Expected exit 0 (fail-open), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "WARN" in result.stderr
    assert "transcript dir not found" in result.stderr


# ---------------------------------------------------------------------------
# Case 5: transcript dir exists but no jsonl -> proceeds with warning (exit 0)
# ---------------------------------------------------------------------------

def test_empty_transcript_dir_proceeds_with_warning(tmp_path):
    fake_home = _make_fake_home(tmp_path)
    # Create the dir but put no jsonl files in it.
    transcript_dir = fake_home / TRANSCRIPT_SUBPATH
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "not-a-jsonl.txt").write_text("noise\n")

    result = _run(fake_home, [])

    assert result.returncode == 0, (
        f"Expected exit 0 (fail-open, no jsonl), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "WARN" in result.stderr
    assert "no jsonl transcripts found" in result.stderr


# ---------------------------------------------------------------------------
# Case 6: custom --idle-mins respected
# ---------------------------------------------------------------------------

def test_custom_idle_mins_threshold(tmp_path):
    fake_home = _make_fake_home(tmp_path)
    # Activity 3 minutes ago. Default threshold (10m) would refuse;
    # with --idle-mins 2 the threshold is 2m and 3m > 2m -> should pass.
    _place_jsonl(fake_home, age_seconds=3 * 60)

    result = _run(fake_home, ["--idle-mins", "2"])

    assert result.returncode == 0, (
        f"Expected exit 0 (3m > 2m threshold), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "idle guard OK" in result.stdout or "idle guard OK" in result.stderr
