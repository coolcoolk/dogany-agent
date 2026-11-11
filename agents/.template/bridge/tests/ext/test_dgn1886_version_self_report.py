"""DGN-1886: the model's "what version am I on" comes from the instance stamp.

Owner report 2026-10-06 (remote main, installed v2.5.0): asked "is there an
update?", the agent answered "v1.41.0 -> v2.6.0 (799 files)" -- it had read
its own version out of the framework repository CLONE (a checkout parked on
an old tag) -- and the reply listed git / update.sh commands and paths.

Cause: nothing in an owner session told the model its installed version.
routines/version-check.py injects the stamp only inside an update notice
(and on an estate host only drops a request for the bridge), so on every
other session the model had no fact to stand on and went looking in the
clone. The fix composes the stamp (.instance.conf DOGANY_FW_VERSION, full
build via DOGANY_FW_TAG) into the bridge system prompt, together with the
answer route (/update) and the no-internals rule for update answers.

Fixture = the incident shape: stamp 2.5.0, clone checkout at v1.41.0.
"""

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bridge import sdk_bridge

HEADING = "## Framework version and updates"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                        "HOME": str(cwd)})


@pytest.fixture
def clone(tmp_path):
    """A framework clone whose checkout sits on an OLD tag (the trap)."""
    repo = tmp_path / "dogany-agent"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "VERSION").write_text("1.41.0\n", encoding="utf-8")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-q", "-m", "v1.41.0")
    _git(repo, "tag", "v1.41.0")
    return repo


def _instance(tmp_path, conf_lines):
    root = tmp_path / "main"
    (root / ".telegram_bot").mkdir(parents=True, exist_ok=True)
    (root / ".instance.conf").write_text("\n".join(conf_lines) + "\n",
                                         encoding="utf-8")
    return root


def _compose(root):
    absent = Path(str(root)) / "no-vendors"
    with patch.object(sdk_bridge, "PROJECT_ROOT", Path(root)), \
            patch.object(sdk_bridge, "_VENDOR_DIR", absent), \
            patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", False):
        return sdk_bridge._compose_system_prompt()


def _section(prompt):
    assert HEADING in prompt, "no framework-version section in the prompt"
    return prompt[prompt.index(HEADING):]


def test_installed_version_is_the_stamp_not_the_clone(tmp_path, clone):
    root = _instance(tmp_path, ["DOGANY_FW_VERSION=2.5.0",
                                "DOGANY_REPO_ROOT=%s" % clone])
    sec = _section(_compose(root))
    assert "v2.5.0" in sec
    assert "1.41.0" not in sec


def test_section_names_the_clone_as_not_a_version_source(tmp_path, clone):
    root = _instance(tmp_path, ["DOGANY_FW_VERSION=2.5.0",
                                "DOGANY_REPO_ROOT=%s" % clone])
    sec = _section(_compose(root))
    assert "never read your version from the framework repository clone" in sec


def test_update_questions_route_to_the_update_command(tmp_path, clone):
    root = _instance(tmp_path, ["DOGANY_FW_VERSION=2.5.0",
                                "DOGANY_REPO_ROOT=%s" % clone])
    sec = _section(_compose(root))
    assert "/update" in sec
    assert "Do not compare versions yourself" in sec


def test_update_answers_carry_no_internals(tmp_path, clone):
    root = _instance(tmp_path, ["DOGANY_FW_VERSION=2.5.0",
                                "DOGANY_REPO_ROOT=%s" % clone])
    sec = _section(_compose(root))
    for word in ("commands", "script names", "file paths", "git",
                 "file counts"):
        assert word in sec, word


def test_full_build_from_the_tag_stamp(tmp_path):
    # A dev build: the bare X.Y.Z is not what is installed.
    root = _instance(tmp_path, ["DOGANY_FW_VERSION=2.6.0",
                                "DOGANY_FW_TAG=v2.6.0-dev.33"])
    assert "v2.6.0-dev.33" in _section(_compose(root))


def test_stale_tag_stamp_falls_back_to_the_base_version(tmp_path):
    # A tag left over from an older base is not the installed build.
    root = _instance(tmp_path, ["DOGANY_FW_VERSION=2.6.1",
                                "DOGANY_FW_TAG=v2.6.0-dev.33"])
    sec = _section(_compose(root))
    assert "v2.6.1" in sec and "dev.33" not in sec


def test_no_stamp_no_claim(tmp_path):
    # No stamp -> the prompt asserts no version at all (never a guess).
    root = tmp_path / "bare"
    root.mkdir()
    assert HEADING not in _compose(root)
    root2 = _instance(tmp_path, ["DOGANY_REPO_ROOT=/nowhere"])
    assert HEADING not in _compose(root2)


def test_stamp_is_read_at_compose_time(tmp_path):
    # After an update the restarted bridge composes the NEW stamp.
    root = _instance(tmp_path, ["DOGANY_FW_VERSION=2.5.0"])
    assert "v2.5.0" in _section(_compose(root))
    (root / ".instance.conf").write_text("DOGANY_FW_VERSION=2.6.2\n",
                                         encoding="utf-8")
    sec = _section(_compose(root))
    assert "v2.6.2" in sec and "v2.5.0" not in sec


def test_section_carries_no_ticket_ids(tmp_path):
    root = _instance(tmp_path, ["DOGANY_FW_VERSION=2.5.0"])
    assert not re.search(r"DGN-\d+", _section(_compose(root)))
