"""Test bootstrap: hermetic environment isolation before any bridge import.

bridge.config reads PROJECT_ROOT from the environment at import time and
immediately loads the project .telegram_bot/.env.  If a live agent's
PROJECT_ROOT leaks in from the shell, the real .env is loaded and variables
such as INTERIM_MODE=fold or OUTPUT_LANG_GUARD=0 flip assertions in
test_dgn426 and test_dgn429.

Fix: unconditionally force PROJECT_ROOT to a fresh throwaway directory
(no .env present) and explicitly reset every env var that drives a
module-level constant in bridge.config before any bridge module is imported.
This makes the test suite deterministic regardless of the launching shell.

Affected DGN: DGN-760.
"""

import os
import sys
import tempfile
from pathlib import Path

# Make the bridge package importable (grandparent of tests/).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# --- Hermetic PROJECT_ROOT -----------------------------------------------
# Always override, even when PROJECT_ROOT is already set in the environment.
# The real project directory carries a live .env that pollutes module-level
# constants (INTERIM_MODE, OUTPUT_LANG_GUARD, etc.) and flips assertions.
_hermetic_root = tempfile.mkdtemp(prefix="bridge-test-")
(Path(_hermetic_root) / ".telegram_bot").mkdir(parents=True, exist_ok=True)
os.environ["PROJECT_ROOT"] = _hermetic_root

# --- Hermetic token -------------------------------------------------------
# Override (not setdefault) so a real token exported in the shell does not
# slip through when PROJECT_ROOT is redirected to the throwaway dir.
os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"

# --- Neutralize module-level config constants -----------------------------
# bridge.config reads these from the environment at import time and assigns
# them as module-level constants (INTERIM_MODE, OUTPUT_LANG_GUARD, etc.).
# Pin them to their canonical defaults so test assertions are stable across
# all shell environments.
#
# INTERIM_MODE: default is unset (resolved to "suppress" by _resolve_interim_mode).
os.environ.pop("INTERIM_MODE", None)
# STREAM_INTERIM: default is "0" (False -> suppress path).
os.environ["STREAM_INTERIM"] = "0"
# OUTPUT_LANG_GUARD: default ON.
os.environ["OUTPUT_LANG_GUARD"] = "1"
# BRIDGE_REGISTER_GUARD: default ON.
os.environ["BRIDGE_REGISTER_GUARD"] = "1"
# BRIDGE_SCAFFOLD_GUARD: default ON.
os.environ["BRIDGE_SCAFFOLD_GUARD"] = "1"
# DGN-911: in-flight debounce-interrupt knobs -- pin canonical defaults.
os.environ.pop("BRIDGE_INFLIGHT_DEBOUNCE_S", None)
os.environ["BRIDGE_INFLIGHT_INTERRUPT_NOTICE"] = "0"
# DGN-932: class-wise notification policy -- default is unset (locked class
# defaults apply); a live instance's NOTIFY_POLICY must not leak in.
os.environ.pop("NOTIFY_POLICY", None)
