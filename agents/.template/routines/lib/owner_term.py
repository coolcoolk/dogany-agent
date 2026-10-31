"""Shared owner-term read (extracted from session-recap, DGN-1652)."""
import os
from conf_reader import conf_get

# Existing recap fallback; also recognized as legacy input by the role gate.
DEFAULT_OWNER_TERM = "형님"


def owner_term(cwd):
    """Use the existing env -> agent.conf -> fallback contract."""
    conf_path = os.path.join(cwd, "config", "agent.conf")
    return (os.environ.get("DOGANY_OWNER_TERM")
            or conf_get("DASHBOARD_OWNER_TERM", conf_path) or DEFAULT_OWNER_TERM)
