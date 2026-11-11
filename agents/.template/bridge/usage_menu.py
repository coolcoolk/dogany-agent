"""/usage menu (DGN-1984): two entry buttons, back navigation, and every view
gated on what the user onboarded.

Owner 2026-10-10 19:15: /usage gets two buttons -- reset passes and API; each
sub-view has a back button on the LEFT instead of close; a user who
onboarded Codex also sees Codex usage, one who did not never sees Codex
(the reset-pass view included); a user who did not onboard the API sees only
an onboarding button in the API view.

Onboarded means, on this host:
  Codex  a Codex account is signed in (routines/lib/reset_passes.py
         `onboarded`, reported per row by its list verb).
  API    the API credit key is stored where the dispatcher reads it
         (the shared secret store on every platform).

Pure render layer like passes.py: bot.py sends what render_* return and runs
the blocking probes (codex_usage_lines, api_onboarded, passes.run) off the
loop. Copy: bridge/i18n usage_menu_* keys (PENDING_OWNER_CONFIRM).
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bridge import passes, secret_intake
from bridge.config import PROJECT_ROOT, config
from bridge.i18n import t

logger = logging.getLogger(__name__)

CB = "usage:"
CB_MAIN = "usage:main"        # back: the /usage main view
CB_API = "usage:api"          # the API view
CB_API_SETUP = "usage:apisetup"  # the API onboarding screen

# ESTATE:BEGIN the API credit key's keychain item is the estate dispatcher's (dispatch-detached.sh api_key_present); the public build has no keychain item and reads only ANTHROPIC_API_KEY_FILE
API_KEYCHAIN_SERVICE = "dogany-anthropic-api-credit"
API_KEYCHAIN_ACCOUNT = "dogany"
# ESTATE:ELSE
# API_KEYCHAIN_SERVICE = ""
# API_KEYCHAIN_ACCOUNT = ""
# ESTATE:END
CODEX_PROBE_TIMEOUT = 8       # seconds, codex-ratelimit.py's own budget

Button = Tuple[str, str]
Rows = List[List[Button]]


def api_onboarded() -> bool:
    """Presence through the same secret store the dispatcher reads. Blocking."""
    try:
        store = secret_intake.load_lib("secret_store")
        return store.has(API_KEYCHAIN_SERVICE, API_KEYCHAIN_ACCOUNT)
    except Exception:
        return False


def codex_onboarded(inv: Optional[Dict[str, Any]]) -> bool:
    """From the reset-pass lib's list answer (passes.run("list"))."""
    return "codex" in passes.shown_runtimes(inv)


def _window_label(mins: Any) -> str:
    try:
        m = int(mins)
    except (TypeError, ValueError):
        return ""
    if m == 10080:
        return t("usage_menu_codex_week")
    if m % 60 == 0:
        return t("usage_menu_codex_hours").format(h=m // 60)
    return t("usage_menu_codex_mins").format(m=m)


def codex_usage_lines(script: Optional[Path] = None,
                      codex_passes: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """The Codex card of the /usage main view (DGN-2060: same layout as the
    Claude card, its one quota window, the Codex reset passes inside): read by
    routines/codex-ratelimit.py, or one line saying it could not be read.
    `codex_passes` = the reset-pass list's codex rows. Blocking (spawns the
    codex app-server); run off the loop."""
    script = script or Path(PROJECT_ROOT) / "routines" / "codex-ratelimit.py"
    doc: Dict[str, Any] = {}
    if script.is_file():
        try:
            proc = subprocess.run(
                [sys.executable, "-B", str(script), "--json",
                 "--timeout", str(CODEX_PROBE_TIMEOUT)],
                capture_output=True, text=True, timeout=CODEX_PROBE_TIMEOUT + 4)
            doc = json.loads(proc.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            logger.warning("codex usage read failed: %s", e)
            doc = {}
    card = secret_intake.load_lib("usage_card")
    T = card.text(config.locale)
    used = doc.get("used_percent") if isinstance(doc, dict) else None
    if not (isinstance(doc, dict) and doc.get("ok") is True and isinstance(used, (int, float))):
        return [T["codex"], card.RULE, t("usage_menu_codex_unavailable"), card.RULE]
    window = _window_label(doc.get("window_duration_mins")) or T["hw"]
    return card.card(T["codex"], [(window, used, doc.get("resets_at"))], codex_passes, T)


def main_rows() -> Rows:
    """The /usage main view's buttons: reset passes, API."""
    return [[(t("usage_menu_btn_passes"), passes.CB_OPEN),
             (t("usage_menu_btn_api"), CB_API)]]


def _back() -> Button:
    return (t("passes_btn_back"), CB_MAIN)


def render_api(onboarded: bool, credit: Optional[Dict[str, Any]]) -> Tuple[str, Rows]:
    """API view. Not onboarded -> only the onboarding button (back on its
    left); onboarded -> the month's API credit (console-sourced, DGN-1978)."""
    lines = [t("usage_menu_api_title")]
    if not onboarded:
        lines += ["", t("usage_menu_api_not_onboarded")]
        return "\n".join(lines), [[_back(), (t("usage_menu_btn_api_setup"), CB_API_SETUP)]]
    block = passes.credit_lines(credit)
    if credit and credit.get("owner_off"):
        lines += ["", t("usage_menu_api_off")]
        return "\n".join(lines), [[_back()]]
    lines += block[2:] if block else ["", t("usage_menu_api_no_credit")]
    if block:
        lines.insert(1, "")
    return "\n".join(lines), [[_back()]]


def render_api_setup() -> Tuple[str, Rows]:
    """The API onboarding screen: the steps; back to the API view."""
    return t("usage_menu_api_setup"), [[(t("passes_btn_back"), CB_API)]]


def parse_callback(data: str) -> str:
    """'usage:<verb>' -> verb ('' for anything unknown)."""
    verb = data[len(CB):] if data.startswith(CB) else ""
    return verb if verb in ("main", "api", "apisetup") else ""
