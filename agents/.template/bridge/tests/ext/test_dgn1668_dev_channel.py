"""DGN-1668: the dev channel must look where dev tags actually are.

The defect these tests lock out: an instance with DOGANY_UPDATE_CHANNEL=dev
polled the PUBLIC mirror's VERSION file (check 2) and filtered pre-release
tags out of its local lookup (check 1). Dev tags are minted only in the
canonical repository, so both checks returned "" and the hook printed
nothing -- byte-identical to "you are up to date". A live instance sat five
dev tags behind while caching the mirror's stale release number as its
"newest remote".

Network-free by construction: every case builds a real throwaway git repo
with real tags in tmp_path, and DOGANY_VERSION_CHECK=0 keeps check 2 off the
network on the cases that still reach it.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _find_repo_root():
    for cand in Path(__file__).resolve().parents:
        if (cand / "routines" / "version-check.py").is_file():
            return cand
    return None


REPO_ROOT = _find_repo_root()
if REPO_ROOT is None:
    pytest.skip(
        "routines/version-check.py not present in this tree (staged "
        "harness) -- producer tests run in the canonical repo only",
        allow_module_level=True,
    )
SCRIPT_SRC = REPO_ROOT / "routines" / "version-check.py"
LOG_REL = Path(".telegram_bot") / "state" / "version-check.log"
CACHE_REL = Path(".telegram_bot") / "state" / "version-check-cache"


@pytest.fixture(scope="module")
def vc():
    """The hook imported as a module, for unit-level calls."""
    spec = importlib.util.spec_from_file_location("vc_dgn1668", SCRIPT_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                   capture_output=True, text=True)


def _mk_source_repo(tmp_path, tags):
    """A real git checkout carrying `tags` -- the stand-in for the local
    canonical clone. Tag resolution goes through git, so a fake will not do."""
    repo = tmp_path / "canonical"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "VERSION").write_text("2.6.0\n", encoding="utf-8")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-qm", "seed")
    for tag in tags:
        _git(repo, "tag", tag)
    return repo


def _mk_instance(tmp_path, repo_root, channel="dev", built="2.6.0",
                 fw_tag="v2.6.0-dev.33"):
    root = tmp_path / "instance"
    (root / "routines").mkdir(parents=True)
    (root / ".telegram_bot" / "state").mkdir(parents=True)
    shutil.copy2(SCRIPT_SRC, root / "routines" / "version-check.py")
    lines = ["DOGANY_FW_VERSION={}".format(built)]
    if repo_root is not None:
        lines.append("DOGANY_REPO_ROOT={}".format(repo_root))
    if channel is not None:
        lines.append("DOGANY_UPDATE_CHANNEL={}".format(channel))
    if fw_tag is not None:
        lines.append("DOGANY_FW_TAG={}".format(fw_tag))
    (root / ".instance.conf").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
    # Opt out of the network check: the release path must never reach
    # GitHub from a test run.
    (root / ".telegram_bot" / ".env").write_text(
        "DOGANY_VERSION_CHECK=0\n", encoding="utf-8")
    return root


def _run_hook(root, session_id="session-a"):
    env = {k: v for k, v in os.environ.items()
           if k not in ("DOGANY_BRIDGE_SESSION", "DOGANY_LANG",
                        "DOGANY_UPDATE_CHANNEL")}
    env["DOGANY_LANG"] = "en"
    # DGN-1675: the notice path is owner-session (bridge) only.
    env["DOGANY_BRIDGE_SESSION"] = "1"
    proc = subprocess.run(
        [sys.executable, str(root / "routines" / "version-check.py")],
        input=json.dumps({"session_id": session_id}), capture_output=True,
        text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _context(stdout):
    if not stdout.strip():
        return ""
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


def _log(root):
    path = root / LOG_REL
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# --------------------------------------------------------------------------
# the defect itself
# --------------------------------------------------------------------------

def test_dev_channel_sees_a_newer_dev_tag_in_the_local_clone(tmp_path):
    repo = _mk_source_repo(tmp_path, ["v2.5.0", "v2.6.0-dev.33",
                                      "v2.6.0-dev.38"])
    root = _mk_instance(tmp_path, repo)
    ctx = _context(_run_hook(root))
    assert "2.6.0-dev.38" in ctx
    # The source named to the agent is the clone, not the public mirror.
    assert str(repo) in ctx
    assert "advertise(dev): v2.6.0-dev.38" in _log(root)


def test_dev_channel_ranks_dev_numbers_numerically(tmp_path):
    """dev.10 must outrank dev.9 -- a lexical sort would hide it."""
    repo = _mk_source_repo(tmp_path, ["v2.6.0-dev.9", "v2.6.0-dev.10"])
    root = _mk_instance(tmp_path, repo, fw_tag="v2.6.0-dev.9")
    assert "2.6.0-dev.10" in _context(_run_hook(root))


def test_dev_channel_silent_only_when_genuinely_current(tmp_path):
    repo = _mk_source_repo(tmp_path, ["v2.6.0-dev.38"])
    root = _mk_instance(tmp_path, repo, fw_tag="v2.6.0-dev.38")
    assert _run_hook(root).strip() == ""
    # Silence is only acceptable when the log can tell it apart from blindness.
    assert "no-op(dev): nothing newer on the dev line" in _log(root)


def test_dev_channel_never_regresses_to_a_pre_release_below_its_stable(
        tmp_path):
    """A promoted v2.7.0 outranks v2.7.0-dev.1; consuming the stable must
    not then re-advertise the dev tag below it."""
    repo = _mk_source_repo(tmp_path, ["v2.7.0-dev.1", "v2.7.0"])
    root = _mk_instance(tmp_path, repo, built="2.7.0", fw_tag="v2.7.0")
    assert _run_hook(root).strip() == ""


# --------------------------------------------------------------------------
# a host with no clone must SAY so (condition 2)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("repo_root,fragment", [
    (None, "no DOGANY_REPO_ROOT declared"),
    ("/definitely/not/here", "framework repo not found"),
])
def test_dev_channel_without_a_clone_reports_instead_of_falling_silent(
        tmp_path, repo_root, fragment):
    root = _mk_instance(tmp_path, repo_root)
    ctx = _context(_run_hook(root))
    assert ctx, "silence here rebuilds the very defect DGN-1668 fixes"
    assert "dev channel cannot be checked on this host" in ctx
    assert fragment in ctx
    assert "---NOTICE---" in ctx
    assert "unavailable(dev)" in _log(root)


def test_unreadable_checkout_is_reported_not_treated_as_up_to_date(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    root = _mk_instance(tmp_path, plain)
    assert "not a readable git checkout" in _context(_run_hook(root))


def test_clone_with_no_dev_line_tag_is_reported(tmp_path):
    """A readable clone carrying only -rc tags cannot serve this channel --
    self-update.sh would refuse too, so the hook must not imply success."""
    repo = _mk_source_repo(tmp_path, ["v2.6.0-rc.1"])
    root = _mk_instance(tmp_path, repo)
    assert "no dev-line tag" in _context(_run_hook(root))


# --------------------------------------------------------------------------
# the stable path must not move (condition 3)
# --------------------------------------------------------------------------

def test_release_channel_ignores_dev_tags_entirely(tmp_path):
    repo = _mk_source_repo(tmp_path, ["v2.5.0", "v2.6.0-dev.38"])
    root = _mk_instance(tmp_path, repo, channel="release", built="2.5.0")
    assert _run_hook(root).strip() == "", "release must not see a dev tag"


def test_release_channel_still_advertises_a_newer_stable_tag(tmp_path):
    repo = _mk_source_repo(tmp_path, ["v2.5.0", "v2.6.0-dev.38"])
    root = _mk_instance(tmp_path, repo, channel="release", built="2.4.0")
    ctx = _context(_run_hook(root))
    assert "2.5.0" in ctx and "dev.38" not in ctx


def test_missing_clone_on_release_channel_stays_silent(tmp_path):
    """The dev-unavailable notice is scoped to the dev channel. A release
    instance without a clone keeps its old behaviour (the public mirror is
    its source), or DGN-1668 would nag every external user."""
    root = _mk_instance(tmp_path, "/definitely/not/here", channel="release")
    assert _run_hook(root).strip() == ""


def test_undeclared_channel_keeps_the_release_path(tmp_path):
    repo = _mk_source_repo(tmp_path, ["v2.5.0", "v2.6.0-dev.38"])
    root = _mk_instance(tmp_path, repo, channel=None, built="2.5.0")
    assert _run_hook(root).strip() == ""
    assert "channel undeclared" in _log(root)


def test_dev_channel_does_not_touch_the_remote_cache(tmp_path):
    """The stale 2.5.0 in a dev instance's cache came from check 2 polling a
    mirror that cannot carry its channel. Check 2 is skipped now, so the
    cache is neither read nor rewritten on this channel."""
    repo = _mk_source_repo(tmp_path, ["v2.6.0-dev.38"])
    root = _mk_instance(tmp_path, repo, fw_tag="v2.6.0-dev.38")
    (root / CACHE_REL).write_text("1.0 2.5.0\n", encoding="utf-8")
    _run_hook(root)
    assert (root / CACHE_REL).read_text(encoding="utf-8") == "1.0 2.5.0\n"
    assert "no-op(remote): skipped -- channel=dev" in _log(root)


# --------------------------------------------------------------------------
# notice lifecycle: the fw_reference split (a dev pending must survive)
# --------------------------------------------------------------------------

def test_dev_pending_survives_the_next_session_and_resolves_on_consumption(
        tmp_path):
    """.instance.conf carries the BASE version (2.6.0) next to the consumed
    tag (v2.6.0-dev.33), and 2.6.0 semver-outranks 2.6.0-dev.38. Reconciling
    a dev pending against the base would expire it as 'resolved' on the very
    next session -- created once, killed before delivery."""
    repo = _mk_source_repo(tmp_path, ["v2.6.0-dev.33", "v2.6.0-dev.38"])
    root = _mk_instance(tmp_path, repo)
    assert "2.6.0-dev.38" in _context(_run_hook(root, "s1"))
    # A later session still gets it: the pending was not silently resolved.
    assert "2.6.0-dev.38" in _context(_run_hook(root, "s2"))
    # Once the instance actually consumes dev.38, it closes.
    conf = root / ".instance.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8").replace("v2.6.0-dev.33",
                                                 "v2.6.0-dev.38"),
        encoding="utf-8")
    assert _run_hook(root, "s3").strip() == ""
    spool = [json.loads(l) for l
             in (root / ".telegram_bot" / "state"
                 / "notice-spool.jsonl").read_text(
                     encoding="utf-8").splitlines() if l.strip()]
    fw = [r for r in spool if r.get("kind") == "fw-update"]
    assert [(r["version"], r["status"], r["expired_reason"]) for r in fw] == [
        ("2.6.0-dev.38", "expired", "resolved")]


def test_missing_fw_tag_stamp_is_logged_not_silently_assumed(tmp_path):
    repo = _mk_source_repo(tmp_path, ["v2.6.0-dev.38"])
    root = _mk_instance(tmp_path, repo, fw_tag=None)
    _run_hook(root)
    assert "degraded(dev): no DOGANY_FW_TAG stamp" in _log(root)


# --------------------------------------------------------------------------
# version comparison
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cand,cur,want", [
    ("2.6.0-dev.38", "2.6.0-dev.33", True),
    ("2.6.0-dev.33", "2.6.0-dev.38", False),
    ("2.6.0-dev.38", "2.6.0-dev.38", False),
    ("2.6.0", "2.6.0-dev.38", True),
    ("2.6.0-dev.38", "2.6.0", False),
    ("2.7.0-dev.1", "2.6.0-dev.38", True),
    ("2.6.0-dev.10", "2.6.0-dev.9", True),
    ("2.6.0-rc.1", "2.6.0-dev.38", True),
])
def test_prerelease_precedence(vc, cand, cur, want):
    assert vc._is_newer(cand, cur) is want


@pytest.mark.parametrize("cand,cur,want", [
    ("2.6.0", "2.5.0", True),
    ("2.5.0", "2.6.0", False),
    ("2.6.0", "2.6.0", False),
    ("10.0.0", "9.0.0", True),
    ("unknown", "2.6.0", True),
])
def test_suffix_free_comparison_is_unchanged(vc, cand, cur, want):
    assert vc._is_newer(cand, cur) is want
