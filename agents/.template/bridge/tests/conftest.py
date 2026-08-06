"""Test bootstrap: satisfy import-time config before any bridge import.

bridge.config reads PROJECT_ROOT from the environment and constructs a Config()
that requires a non-placeholder TELEGRAM_BOT_TOKEN at import time. Point both at
throwaway test values so importing the package never needs a live project or a
real token.
"""

import os
import sys
import tempfile
from pathlib import Path

# Make the package importable (parent of bridge/).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if "PROJECT_ROOT" not in os.environ:
    _tmp = tempfile.mkdtemp(prefix="bridge-test-")
    (Path(_tmp) / ".telegram_bot").mkdir(parents=True, exist_ok=True)
    os.environ["PROJECT_ROOT"] = _tmp

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
