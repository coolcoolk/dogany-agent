"""DGN-1814 (dec-223): one Claude CLI resolver for every launch site.

Measured bug: with CLAUDE_CLI_PATH unset the send path passed no cli_path, so
claude-agent-sdk fell back to its BUNDLED CLI (older, different model alias
map) while --selfcheck and the boot snapshot reported the PATH CLI.

Covers:
  (a) unset key + PATH hit -> send-path SDK options carry that path
  (b) explicit CLAUDE_CLI_PATH wins over PATH
  (c) no PATH hit, no ~/.local/bin -> no cli_path (bundled) + WARNING line
  (d) every launch site (send path, btw fork, options classifier, boot
      snapshot, selfcheck) agrees with bridge.config.resolve_claude_cli_source
  (e) static guard: no bridge module bypasses the resolver (no direct
      shutil.which("claude"), no direct CLAUDE_CLI_PATH read outside config)
"""

import ast
import asyncio
import logging
import stat
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import config as config_mod

BRIDGE_DIR = Path(__file__).resolve().parents[1]


def _fake_cli(dirpath: Path, version: str = "9.9.9 (Claude Code)") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / "claude"
    p.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    """No explicit key, empty PATH, HOME without ~/.local/bin/claude."""
    monkeypatch.setattr(config_mod, "CLAUDE_CLI_PATH", None)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return tmp_path


def _send_path_opts() -> dict:
    from bridge.sdk_bridge import SdkBridge

    captured: dict = {}

    def fake_options(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    client = MagicMock()
    client.connect = AsyncMock()
    with (
        patch("bridge.sdk_bridge.ClaudeAgentOptions", side_effect=fake_options),
        patch("bridge.sdk_bridge.ClaudeSDKClient", return_value=client),
        patch("bridge.sdk_bridge.asyncio.create_task"),
    ):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                SdkBridge()._create_user_stream(user_id=42, model=None)
            )
        except Exception:
            pass  # opts capture is the goal
        finally:
            loop.close()
    assert captured, "ClaudeAgentOptions was never built"
    return captured


def _btw_opts() -> dict:
    from bridge.btw import BtwForkManager

    captured: dict = {}

    def fake_options(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with (
        patch("bridge.btw.ClaudeAgentOptions", side_effect=fake_options),
        patch("bridge.btw.ClaudeSDKClient", return_value=MagicMock()),
    ):
        BtwForkManager()._make_fork_client("sess-1")
    return captured


# --- (a)(b)(c) resolver ladder --------------------------------------------


def test_unset_key_path_hit_send_path_carries_path(clean_env, monkeypatch):
    cli = _fake_cli(clean_env / "pathbin")
    monkeypatch.setenv("PATH", str(cli.parent))
    assert config_mod.resolve_claude_cli_source() == (
        str(cli),
        config_mod.CLI_SOURCE_PATH,
    )
    assert _send_path_opts().get("cli_path") == str(cli)
    assert _btw_opts().get("cli_path") == str(cli)


def test_explicit_key_wins_over_path(clean_env, monkeypatch):
    on_path = _fake_cli(clean_env / "pathbin")
    monkeypatch.setenv("PATH", str(on_path.parent))
    explicit = _fake_cli(clean_env / "explicit")
    monkeypatch.setattr(config_mod, "CLAUDE_CLI_PATH", str(explicit))
    assert config_mod.resolve_claude_cli_source() == (
        str(explicit),
        config_mod.CLI_SOURCE_EXPLICIT,
    )
    assert _send_path_opts().get("cli_path") == str(explicit)


def test_local_bin_fallback_when_path_misses(clean_env):
    local = _fake_cli(clean_env / "home" / ".local" / "bin")
    assert config_mod.resolve_claude_cli_source() == (
        str(local),
        config_mod.CLI_SOURCE_LOCAL_BIN,
    )
    assert _send_path_opts().get("cli_path") == str(local)


def test_no_cli_anywhere_bundled_with_warning(clean_env, caplog):
    assert config_mod.resolve_claude_cli_source() == (
        None,
        config_mod.CLI_SOURCE_BUNDLED,
    )
    assert "cli_path" not in _send_path_opts()
    assert "cli_path" not in _btw_opts()
    with caplog.at_level(logging.INFO, logger="bridge.config"):
        config_mod.log_claude_cli_resolution()
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns and "BUNDLED" in warns[0].getMessage()


def test_startup_line_names_path_and_version(clean_env, monkeypatch, caplog):
    cli = _fake_cli(clean_env / "pathbin", version="2.1.284 (Claude Code)")
    monkeypatch.setenv("PATH", str(cli.parent))
    with caplog.at_level(logging.INFO, logger="bridge.config"):
        config_mod.log_claude_cli_resolution()
    msgs = [r.getMessage() for r in caplog.records]
    assert any(str(cli) in m and "2.1.284" in m and "source=path" in m for m in msgs)


# --- (d) every launch site agrees with the resolver -----------------------


def test_all_launch_sites_use_the_resolver(tmp_path, monkeypatch):
    """Patch ONLY the resolver; a site that bypasses it misses the sentinel."""
    sentinel = _fake_cli(tmp_path / "sentinel-bin")
    # Poison the other inputs so a bypassing site would pick something else.
    monkeypatch.setattr(config_mod, "CLAUDE_CLI_PATH", None)
    decoy = _fake_cli(tmp_path / "decoy-bin")
    monkeypatch.setenv("PATH", str(decoy.parent))
    monkeypatch.setattr(
        config_mod,
        "resolve_claude_cli_source",
        lambda: (str(sentinel), config_mod.CLI_SOURCE_PATH),
    )

    # send path + btw fork
    assert _send_path_opts().get("cli_path") == str(sentinel)
    assert _btw_opts().get("cli_path") == str(sentinel)

    # options classifier (Haiku shell-out) with no cli_path argument
    from bridge import options as options_mod

    argv_seen = []

    def fake_run(argv, **_kw):
        argv_seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="yes", stderr="")

    monkeypatch.setattr(options_mod.subprocess, "run", fake_run)
    assert options_mod.classify_is_choice("q", "1. a\n2. b") is True
    assert argv_seen and argv_seen[0][0] == str(sentinel)

    # ESTATE:BEGIN DGN-1814 the boot snapshot (/health) launch site lives in the estate ext subpackage, which the public tree does not ship
    # boot snapshot / /health
    import bridge.ext.dogany_estate.boot_snapshot as boot_snapshot

    assert boot_snapshot._resolve_claude_cli()["resolved_path"] == str(sentinel)
    # ESTATE:END

    # selfcheck: resolver says "nothing" -> selfcheck must FAIL on it
    from bridge import __main__ as main_mod

    monkeypatch.setattr(
        config_mod,
        "resolve_claude_cli_source",
        lambda: (None, config_mod.CLI_SOURCE_BUNDLED),
    )
    assert main_mod._selfcheck() == 1
    monkeypatch.setattr(
        config_mod,
        "resolve_claude_cli_source",
        lambda: (str(sentinel), config_mod.CLI_SOURCE_PATH),
    )
    assert main_mod._selfcheck() == 0


def test_selfcheck_fails_on_missing_explicit(tmp_path, monkeypatch, capsys):
    from bridge import __main__ as main_mod

    monkeypatch.setattr(config_mod, "CLAUDE_CLI_PATH", str(tmp_path / "nope"))
    assert main_mod._selfcheck() == 1
    assert "CLAUDE_CLI_PATH not found" in capsys.readouterr().out


# --- (e) static guard: no bypass ------------------------------------------


def _bridge_sources():
    for py in sorted(BRIDGE_DIR.rglob("*.py")):
        rel = py.relative_to(BRIDGE_DIR)
        if rel.parts[0] == "tests":
            continue
        yield rel, ast.parse(py.read_text(encoding="utf-8"))


def test_no_site_bypasses_the_resolver():
    offenders = []
    for rel, tree in _bridge_sources():
        is_config = rel.as_posix() == "config.py"
        for node in ast.walk(tree):
            # shutil.which("claude") anywhere but the resolver
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "which"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "claude"
                and not is_config
            ):
                offenders.append(f"{rel}:{node.lineno} shutil.which('claude')")
            # reading CLAUDE_CLI_PATH directly (import or attribute) outside config
            if not is_config:
                if isinstance(node, ast.Name) and node.id == "CLAUDE_CLI_PATH":
                    offenders.append(f"{rel}:{node.lineno} CLAUDE_CLI_PATH")
                if isinstance(node, ast.Attribute) and node.attr == "CLAUDE_CLI_PATH":
                    offenders.append(f"{rel}:{node.lineno} .CLAUDE_CLI_PATH")
                if isinstance(node, ast.ImportFrom) and any(
                    a.name == "CLAUDE_CLI_PATH" for a in node.names
                ):
                    offenders.append(f"{rel}:{node.lineno} import CLAUDE_CLI_PATH")
    assert not offenders, "launch sites bypassing resolve_claude_cli: " + ", ".join(
        offenders
    )
