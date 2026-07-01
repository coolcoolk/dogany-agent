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


def main() -> None:
    parser = argparse.ArgumentParser(prog="bridge")
    parser.add_argument("path", nargs="?", help="Project path")
    parser.add_argument("--path", dest="path_opt", help="Project path")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
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
