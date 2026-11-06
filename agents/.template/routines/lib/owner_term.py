"""Shared owner-term read (extracted from session-recap, DGN-1652)."""
import os
from conf_reader import conf_get

# Rendered when no owner term is configured: framework text names the user
# generically, never a hardcoded form of address (hot.framework.md Output,
# v2.7 D5). The legacy fallback below is INPUT vocabulary only.
DEFAULT_OWNER_TERM = "the user"
# The pre-D5 hardcoded fallback. Old copied prose may still carry it, so the
# role gate keeps recognizing it (role_prose.py); nothing renders it.
LEGACY_OWNER_TERM = "\ud615\ub2d8"
# Korean input vocabulary retained for older instances. These are matching
# defaults only; rendered text must always use owner_term().
KO_OWNER_TERM_DEFAULTS = (LEGACY_OWNER_TERM, "\uc0ac\uc6a9\uc790\ub2d8", "\uc624\ub108")


def _chat_choice(cwd):
    """The user's own term.owner choice (DGN-1980 chat path, written to the
    DGN-1791 instance layer config/i18n.local/<lang>.json); '' when none.
    Only the instance layer: the framework/kit default of term.owner is NOT
    used here, so an instance with no choice keeps rendering exactly what it
    did before (the neutral DEFAULT_OWNER_TERM)."""
    try:
        import term_registry
        return term_registry.instance_value(
            cwd, term_registry.conf_lang(cwd), "term.owner")
    except Exception:
        return ""


def owner_term(cwd):
    """env -> the user's chat choice (DGN-1980) -> agent.conf (legacy knob)
    -> neutral fallback.  One mechanism with every other term: a rename from
    chat lands in the i18n instance layer and wins over the legacy knob."""
    conf_path = os.path.join(cwd, "config", "agent.conf")
    return (os.environ.get("DOGANY_OWNER_TERM") or _chat_choice(cwd)
            or conf_get("DASHBOARD_OWNER_TERM", conf_path) or DEFAULT_OWNER_TERM)


def owner_terms(cwd):
    """Configured owner term plus stable Korean input-matching defaults."""
    configured = owner_term(cwd)
    return tuple(dict.fromkeys((configured,) + KO_OWNER_TERM_DEFAULTS))
