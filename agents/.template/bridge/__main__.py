"""Entry point: resolve PROJECT_ROOT, set up logging, run the bot.

PROJECT_ROOT must be exported into the environment BEFORE config/bot are
imported (both read it at import time), so all imports are deferred until after
the argument is resolved.
"""

import argparse
import logging
import os
import sys
from pathlib import Path


def _selfcheck() -> int:
    """Offline instance health check (DGN-674 S6). Network 0, deterministic.

    Runs AFTER PROJECT_ROOT is resolved (config reads it at import time):
      1. import bridge.config  -- parses .telegram_bot/.env
      2. import bridge.bot + bridge.sdk_bridge -- the full runtime surface
      3. cross-module constant resolution (DGN-762) -- every static
         `messages.<NAME>` / `from bridge.messages import <NAME>` reference
         in bridge/*.py must resolve against the actual messages module
      4. resolve the Claude CLI -- explicit CLAUDE_CLI_PATH wins, else PATH
         lookup, else ~/.local/bin/claude (same rule as start.sh / the
         launchd plist PATH, DGN-674 F9)
    Prints "selfcheck ok" on success, "selfcheck FAIL: <reason>" line(s) on
    failure.
    """
    import ast
    import shutil

    try:
        from bridge import config as _config
    except Exception as e:  # noqa: BLE001 -- any import error is the finding
        print(f"selfcheck FAIL: config import ({e})")
        return 1
    try:
        import bridge.bot  # noqa: F401
        import bridge.sdk_bridge  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"selfcheck FAIL: bridge import ({e})")
        return 1

    # DGN-762: a stale/frozen messages.py imports fine yet crashes at runtime
    # when another module reads a constant it does not define (skull incident:
    # bot.py -> messages.CMD_DESC_KILL, AttributeError crash loop). AST-scan
    # every bridge/*.py for static references and resolve each against the
    # imported module. Known blind spot: dynamic getattr(messages, key)
    # references are invisible to AST.
    from bridge import messages as _messages

    refs = {}  # NAME -> referencing file name
    for py in sorted(Path(__file__).resolve().parent.glob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue  # unparsable file is the import step's finding, not ours
        binds_messages = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "bridge" and any(
                    a.name == "messages" and a.asname in (None, "messages")
                    for a in node.names
                ):
                    binds_messages = True
                elif node.module == "bridge.messages":
                    for a in node.names:
                        if a.name != "*":
                            refs.setdefault(a.name, py.name)
            elif isinstance(node, ast.Import) and any(
                a.name == "bridge.messages" and a.asname == "messages"
                for a in node.names
            ):
                binds_messages = True
        if binds_messages:
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "messages"
                ):
                    refs.setdefault(node.attr, py.name)
    missing = sorted(name for name in refs if not hasattr(_messages, name))
    if missing:
        for name in missing:
            print(
                f"selfcheck FAIL: {refs[name]} references messages.{name} "
                "which is undefined"
            )
        return 1
    cli = _config.CLAUDE_CLI_PATH
    if cli:
        if not Path(cli).expanduser().exists():
            print(f"selfcheck FAIL: CLAUDE_CLI_PATH not found ({cli})")
            return 1
    else:
        local_bin = Path.home() / ".local" / "bin" / "claude"
        if not (shutil.which("claude") or local_bin.exists()):
            print("selfcheck FAIL: claude CLI not found (PATH or ~/.local/bin)")
            return 1
    print("selfcheck ok")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="bridge")
    parser.add_argument("path", nargs="?", help="Project path")
    parser.add_argument("--path", dest="path_opt", help="Project path")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="offline instance health check (imports + .env + CLI resolution)",
    )
    args = parser.parse_args()

    if args.debug:
        os.environ["BOT_DEBUG"] = "1"

    path = args.path_opt or args.path
    if path:
        os.environ["PROJECT_ROOT"] = str(Path(path).expanduser().resolve())

    if "PROJECT_ROOT" not in os.environ:
        print(
            "Error: specify the project path via argument or the PROJECT_ROOT "
            "environment variable"
        )
        sys.exit(1)

    if args.selfcheck:
        sys.exit(_selfcheck())

    from bridge.config import setup_logging

    setup_logging()
    logger = logging.getLogger(__name__)

    from bridge.bot import bot

    try:
        bot.run()
    except SystemExit as e:
        if e.code and str(e.code) != "0":
            logger.error(str(e.code))
        sys.exit(1)
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
