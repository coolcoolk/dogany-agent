"""/passes: the user's own reset-pass setting (DGN-1965 follow-ups).

Owner 2026-10-09 11:29: the reset-pass inventory is a USER SETTING, not a
config line the agent edits. This is its owner surface: list the passes per
runtime with their expiry and kind, add one (runtime -> expiry date button ->
kind), remove one -- buttons only, no config keys typed. Reached by /passes
and by a button under /usage.

Owner 2026-10-09 12:48-13:44 (dec-340): what a pass restores differs PER PASS
(full = the 5-hour and the weekly limit, 5h, weekly, or not known), so each
pass carries a kind and the add flow asks for it as its third step.

Storage stays the instance's config/agent.conf lines RESET_PASSES_CLAUDE /
RESET_PASSES_CODEX, read by the dispatch selector. Every read and write goes
through routines/lib/reset_passes.py (list / add / remove) so the format has
one owner; this module only renders and calls it. When the host does not
ship that lib, the surface says the setting is unavailable.

Pure render layer like model_picker.py: bot.py sends what render_* return.
Copy: bridge/i18n passes_* keys (owner-confirmed, dec-340).

DGN-1978: the list also shows the month's API credit left (the pool's own
summary, routines/lib/api_credit_pool.py) and whether that figure was read
from the console or is an estimate. Read-only; absent when the pool is off
or the lib is missing.
"""

import json
import logging
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bridge.config import PROJECT_ROOT
from bridge.i18n import t

logger = logging.getLogger(__name__)

CB = "rpass:"               # every callback of this surface
CB_OPEN = "rpass:open"      # main list
CB_ADD = "rpass:add"        # add step 1: pick the runtime
CB_DAYS = "rpass:days:"     # rpass:days:<rt>:<page>      add step 2: expiry
CB_KIND = "rpass:kind:"     # rpass:kind:<rt>:<date|unknown>   add step 3: kind
CB_NEW = "rpass:new:"       # rpass:new:<rt>:<date|unknown>:<kind>
CB_RM = "rpass:rm:"         # rpass:rm:<rt>:<expiry>:<kind>
CB_CLOSE = "rpass:close"

RUNTIMES = ("claude", "codex")
RUNTIME_NAMES = {"claude": "Claude", "codex": "Codex"}
KINDS = ("full", "5h", "weekly", "unknown")      # lib order; unknown = weekly
MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAYS_PER_PAGE = 28          # 4 rows x 7
PAGES = 4                   # ~16 weeks ahead

Button = Tuple[str, str]    # (label, callback_data)
Rows = List[List[Button]]


def _lib() -> Path:
    return Path(PROJECT_ROOT) / "routines" / "lib" / "reset_passes.py"


def _conf() -> Path:
    return Path(PROJECT_ROOT) / "config" / "agent.conf"


def credit_summary() -> Optional[Dict[str, Any]]:
    """The API credit pool's state for this workspace, or None (off / no lib
    / unreadable). Blocking: run off the loop (run("list") calls it)."""
    lib = Path(PROJECT_ROOT) / "routines" / "lib" / "api_credit_pool.py"
    if not lib.is_file():
        return None
    try:
        proc = subprocess.run([sys.executable, "-B", str(lib), "summary",
                               "--workspace", str(PROJECT_ROOT)],
                              capture_output=True, text=True, timeout=10)
        doc = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.warning("api credit summary failed: %s", e)
        return None
    if not isinstance(doc, dict) or "off" in doc or "budget" not in doc:
        return None
    return doc


def _usd(v: Any) -> str:
    try:
        return "$%.2f" % float(v)
    except (TypeError, ValueError):
        return "$?"


def _age_label(secs: Any) -> str:
    try:
        s = max(0, int(secs))
    except (TypeError, ValueError):
        return "?"
    if s < 3600:
        return "%dm" % max(1, s // 60)
    if s < 86400:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def credit_lines(c: Optional[Dict[str, Any]]) -> List[str]:
    """The API credit block of the list, [] when there is none."""
    if not c:
        return []
    lines = ["", t("passes_credit_title")]
    if c.get("exhausted"):
        lines.append(t("passes_credit_exhausted"))
    else:
        lines.append(t("passes_credit_left").format(
            left=_usd(c.get("remaining")), budget=_usd(c.get("budget"))))
    truth = c.get("truth")
    if truth == "console":
        lines.append(t("passes_credit_console").format(age=_age_label(c.get("truth_age"))))
    elif truth == "console-stale":
        lines.append(t("passes_credit_stale").format(age=_age_label(c.get("truth_age"))))
    else:
        lines.append(t("passes_credit_estimated"))
    if c.get("inflight_n"):
        lines.append(t("passes_credit_inflight").format(
            n=c.get("inflight_n"), usd=_usd(c.get("inflight_usd"))))
    return lines


def run(verb: str, runtime: str = "", expiry: str = "",
        kind: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
    """Call the lib verb -> (inventory, error). Blocking: run off the loop."""
    lib = _lib()
    if not lib.is_file():
        return None, "unavailable"
    args = [sys.executable, "-B", str(lib), verb, "--conf", str(_conf())]
    if verb != "list":
        args += ["--runtime", runtime, "--expiry", expiry]
        if kind:
            args += ["--kind", kind]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if proc.returncode != 0:
        return None, (proc.stderr or "").strip() or "rc=%d" % proc.returncode
    try:
        inv = json.loads(proc.stdout)
    except ValueError:
        return None, "unreadable answer"
    if verb == "list" and isinstance(inv, dict):
        inv["api_credit"] = credit_summary()
    return inv, ""


def split_entry(arg: str) -> Tuple[str, str]:
    """'<expiry>[:<kind>]' (a callback arg) -> (expiry, kind or ''). The kind
    is the part after the last ':' only when it names one, so the minute of
    '2026-10-23T05:50' is never read as a kind."""
    if ":" in arg:
        head, tail = arg.rsplit(":", 1)
        if tail in KINDS:
            return head, tail
    return arg, ""


def date_label(expiry: str) -> str:
    """'2026-10-23' -> the passes_date copy ('Oct 23' in en); a time is kept
    ('... 05:50'); 'unknown' -> the unknown-expiry label."""
    try:
        d = date.fromisoformat(expiry[:10])
    except (TypeError, ValueError):
        return t("passes_unknown")
    label = t("passes_date").format(month=d.month, mon=MON[d.month - 1], day=d.day)
    if "T" in expiry:
        label += " " + expiry.split("T", 1)[1]
    return label


def kind_label(kind: str) -> str:
    return t("passes_kind_%s" % (kind if kind in KINDS else "unknown"))


def pass_label(expiry: str, kind: str) -> str:
    """'<date>: <kind>' ('Oct 23: full' in en) -- one pass, without the marks."""
    return "%s: %s" % (date_label(expiry), kind_label(kind))


def _pass_line(p: Dict[str, Any]) -> str:
    label = pass_label(p.get("expiry", "unknown"), p.get("kind", "unknown"))
    if p.get("expired"):
        label += " " + t("passes_expired_mark")
    elif p.get("next"):
        label += " " + t("passes_next_mark")
    return "· " + label


def render_list(inv: Optional[Dict[str, Any]], error: str = "",
                notice: str = "") -> Tuple[str, Rows]:
    """Main view: per runtime its name, then one line per pass (soonest
    first); a remove button per pass, then Add / Close."""
    if inv is None:
        lines = [t("passes_title"), "", t("passes_unavailable")]
        if error and error != "unavailable":
            logger.warning("reset passes read failed: %s", error)
        return "\n".join(lines), [[(t("passes_btn_close"), CB_CLOSE)]]
    lines = [t("passes_title")]
    if notice:
        lines += ["", notice]
    rows: Rows = []
    for rt in RUNTIMES:
        row = inv.get(rt) or {}
        name = RUNTIME_NAMES[rt]
        lines += ["", name]
        if not row.get("valid", True):
            lines.append(t("passes_invalid"))
        passes = row.get("passes") or []
        if not passes:
            lines.append(t("passes_none"))
        for p in passes:
            exp, kind = p.get("expiry", "unknown"), p.get("kind", "unknown")
            lines.append(_pass_line(p))
            rows.append([(t("passes_btn_remove").format(runtime=name, label=pass_label(exp, kind)),
                          "%s%s:%s:%s" % (CB_RM, rt, exp, kind))])
        if row.get("env_override"):
            lines.append(t("passes_env_override"))
    lines += credit_lines(inv.get("api_credit"))
    lines += ["", t("passes_footer")]
    rows.append([(t("passes_btn_add"), CB_ADD), (t("passes_btn_close"), CB_CLOSE)])
    return "\n".join(lines), rows


def render_pick_runtime() -> Tuple[str, Rows]:
    rows: Rows = [[(RUNTIME_NAMES[rt], "%s%s:0" % (CB_DAYS, rt)) for rt in RUNTIMES]]
    rows.append([(t("passes_btn_back"), CB_OPEN)])
    return t("passes_pick_runtime"), rows


def render_pick_day(runtime: str, page: int, today: Optional[date] = None) -> Tuple[str, Rows]:
    """A 4x7 grid of dates (today onward), paged, plus "not sure" and back."""
    today = today or date.fromtimestamp(time.time())
    page = max(0, min(PAGES - 1, page))
    start = today + timedelta(days=page * DAYS_PER_PAGE)
    rows: Rows = []
    for w in range(DAYS_PER_PAGE // 7):
        row = []
        for d in range(7):
            day = start + timedelta(days=w * 7 + d)
            row.append(("%d/%d" % (day.month, day.day),
                        "%s%s:%s" % (CB_KIND, runtime, day.isoformat())))
        rows.append(row)
    nav: List[Button] = []
    if page > 0:
        nav.append(("◀", "%s%s:%d" % (CB_DAYS, runtime, page - 1)))
    if page < PAGES - 1:
        nav.append(("▶", "%s%s:%d" % (CB_DAYS, runtime, page + 1)))
    if nav:
        rows.append(nav)
    rows.append([(t("passes_btn_unknown"), "%s%s:unknown" % (CB_KIND, runtime)),
                 (t("passes_btn_back"), CB_ADD)])
    end = start + timedelta(days=DAYS_PER_PAGE - 1)
    text = t("passes_pick_day").format(
        runtime=RUNTIME_NAMES.get(runtime, runtime),
        first="%d/%d" % (start.month, start.day), last="%d/%d" % (end.month, end.day))
    return text, rows


def render_pick_kind(runtime: str, expiry: str) -> Tuple[str, Rows]:
    """Add step 3: what this pass restores (full / 5h / weekly / not sure)."""
    base = "%s%s:%s:" % (CB_NEW, runtime, expiry)
    rows: Rows = [[(t("passes_btn_kind_%s" % k), base + k) for k in ("full", "5h", "weekly")]
                  + [(t("passes_btn_unknown"), base + "unknown")]]
    rows.append([(t("passes_btn_back"), "%s%s:0" % (CB_DAYS, runtime))])
    return t("passes_pick_kind"), rows


def parse_callback(data: str) -> Tuple[str, str, str]:
    """'rpass:<verb>[:<rt>[:<arg>]]' -> (verb, runtime, arg); runtime is
    validated (unknown -> ''), arg is passed to the lib, which validates it."""
    parts = data[len(CB):].split(":", 2)
    verb = parts[0]
    rt = parts[1] if len(parts) > 1 and parts[1] in RUNTIMES else ""
    arg = parts[2] if len(parts) > 2 else ""
    return verb, rt, arg
