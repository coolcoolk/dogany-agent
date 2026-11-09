"""DGN-2060: the /usage service card (Claude and Codex share one layout).

    <Service> usage limits
    ---------------------------
    <window label>
    [20-cell bar] NN%              (percent = USED)
    reset MM-DD HH:MM (-Xd Xh Xm)
    ...
    > reset passes N               (only when the service has live passes)
    . MM-DD expires                (one line per pass)
    ---------------------------

Pure formatting; callers pass `now` for deterministic output. Used by
routines/claude-usage.sh (Claude card) and bridge/usage_menu.py (Codex card).
"""
import time
from datetime import datetime, timezone

RULE = "─" * 27
BAR_WIDTH = 20

TEXT = {
    "ko": {"claude": "Claude 사용 한도", "codex": "Codex 사용 한도",
           "h5": "5시간", "hw": "주간", "reset": "리셋",
           "passes": "▸ 리셋권 {n}장",
           "expires": "· {date} 만료",
           "expiry_unknown": "· 만료일 모름"},
    "en": {"claude": "Claude Usage Limits", "codex": "Codex Usage Limits",
           "h5": "5h", "hw": "weekly", "reset": "reset",
           "passes": "▸ Reset passes: {n}",
           "expires": "· expires {date}",
           "expiry_unknown": "· expiry unknown"},
}


def text(locale):
    return TEXT.get(str(locale or "").strip().lower(), TEXT["en"])


def pctstr(v):
    try:
        return str(int(round(float(v))))
    except Exception:
        return "?"


def bar(pct, width=BAR_WIDTH):
    # narrow enough that Telegram mobile <pre> does not wrap (DGN-205)
    try:
        p = max(0.0, min(100.0, float(pct)))
    except Exception:
        return "[" + ("?" * width) + "]"
    filled = int(round(p / 100.0 * width))
    return "[" + ("█" * filled) + ("░" * (width - filled)) + "]"


def remain(dt, now=None):
    """Countdown to reset, "-{d}d {h}h {m}m" (a leading "-" = still ahead,
    "+" = the reset already passed)."""
    now = now or datetime.now(dt.tzinfo)
    secs = int((dt - now).total_seconds())
    sign = "-" if secs >= 0 else "+"
    d, rem = divmod(abs(secs), 86400)
    h, rem = divmod(rem, 3600)
    parts = []
    if d:
        parts.append("%dd" % d)
    if h or d:
        parts.append("%dh" % h)
    parts.append("%dm" % (rem // 60))
    return "%s%s" % (sign, " ".join(parts))


def to_datetime(v):
    """ISO string or epoch (s or ms) -> aware datetime, None when unreadable."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ts = float(v)
            return datetime.fromtimestamp(ts / 1000.0 if ts > 1e12 else ts, timezone.utc)
        s = str(v)
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            ts = float(s)
            return datetime.fromtimestamp(ts / 1000.0 if ts > 1e12 else ts, timezone.utc)
    except Exception:
        return None


def reset_text(v, now=None):
    if v in (None, ""):
        return "?"
    dt = to_datetime(v)
    if dt is None:
        return str(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone()
    return "%s (%s)" % (dt.strftime("%m-%d %H:%M"), remain(dt, now))


def window_lines(label, pct, reset, T, now=None):
    # A pre-reset measurement is unknown after its window ends, even live.
    dt = to_datetime(reset)
    now_ts = now.timestamp() if now is not None else time.time()
    if dt is not None and dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp() <= now_ts:
        pct = None
    return [label, "%s %s%%" % (bar(pct), pctstr(pct)),
            "%s %s" % (T["reset"], reset_text(reset, now))]


def pass_lines(passes, T):
    """Live passes of one service -> block lines; [] when it has none.
    `passes` = reset_passes.inventory()[runtime]["passes"] rows."""
    live = [p for p in passes or [] if not p.get("expired")]
    if not live:
        return []
    out = [T["passes"].format(n=len(live))]
    for p in live:
        label = str(p.get("label") or "")
        if len(label) >= 10 and label[4] == "-" and label[7] == "-":
            out.append(T["expires"].format(date=label[5:10]))
        else:
            out.append(T["expiry_unknown"])
    return out


def card(title, windows, passes, T, now=None, notes=()):
    """windows: [(label, used_pct, reset)]. Title, rule, windows, pass block,
    rule. `notes` (snapshot-age lines) sit right under the title."""
    lines = [title, *notes, RULE]
    for label, pct, reset in windows:
        lines += window_lines(label, pct, reset, T, now)
    lines += pass_lines(passes, T)
    lines.append(RULE)
    return lines


def passes_for(runtime, conf):
    """Live-pass rows of one runtime from the shared store; [] when the lib
    is absent (public build), the runtime is not onboarded, or on any error."""
    try:
        import reset_passes as rp
        row = (rp.inventory(conf or None, time.time()) or {}).get(runtime) or {}
        if row.get("onboarded") is False:
            return []
        return row.get("passes") or []
    except Exception:
        return []


def claude_windows(data, labels):
    """Omit windows explicitly null in the API response (owner 2026-11-01).
    Missing/unreadable windows stay visible as unknown, not plan absence."""
    keys = [("five_hour", "h5"), ("seven_day", "hw")]
    windows = []
    for key, label in keys:
        if key in data and data[key] is None:
            continue
        row = data.get(key)
        row = row if isinstance(row, dict) else {}
        windows.append((labels[label], row.get("utilization"), row.get("resets_at", "")))
    return windows
