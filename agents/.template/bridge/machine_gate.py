"""DGN-1209: outbound machine-line gate -- the ONE choke point for every rail.

THE DEFECT. Consumer stdout (idrill followup_cmd, fastpath body, push.sh
--text, dashboard pin) is posted to the owner VERBATIM with no model turn in
between. set-table printed its machine signal lines (`DAY_SOURCE ...`,
`RAMP_DECISION ...`) to the same stdout, outside the fence, and the owner read
them on his phone. SKILL.md's "machine markers never reach the owner" is a
rule the MODEL executes; on a zero-model-turn rail there is no model, so the
rule never runs. _scaffold_guard (sdk_bridge) is the same shape of defense,
but it is wired ONLY on model-turn paths.

THE CONTRACT (ticket "확정 방향", grill LOCK-WITH-FIX 2026-09-01):

    registered token + pure machine line   -> DROP  + WARNING log
    registered token + owner sentence      -> STRIP prefix, sentence passes
    unregistered + machine SHAPE           -> PASS  + loud alert (dedup'd)
    everything else                        -> PASS

Two halves that close each other's hole -- neither needs to be complete:

  * REGISTRATION is structural, not remembered. The emitter helper
    (routines/lib/machine_signal.py, agentlib.sh machine_signal) is the only
    sanctioned way to print a signal, and it prints the in-band sigil
    `signal::TOKEN ...`. Emitting THROUGH the helper IS the registration; no
    kit<->pack token list has to travel anywhere, and `2>&1` cannot splice
    the signal back in because the decision rides in the TEXT, not the fd.
    A seed of the DGN-1209 incident tokens (SEED_MACHINE_TOKENS) plus the
    BRIDGE_MACHINE_TOKENS .env knob covers emitters not yet on the helper.

  * DETECTION is a heuristic with NO censorship power. The machine-shape
    regex (`^[A-Z][A-Z0-9_]{2,}( |$)`, "P1") only ever ALERTS; it never
    drops. False positives ("API 문서 갱신했습니다") cost one dedup'd notice,
    not a deleted sentence. A raw `print("SOME_TOKEN k=v")` that bypasses the
    helper leaks ONCE, loudly, and that alert is the discovery mechanism that
    leads to registration. set-table:2771 (a direct print bypassing its own
    collection point BEFORE any helper contract existed) is the standing
    counter-example: the detector is a PERMANENT component, not scaffolding.
    tests/test_dgn1209_machine_line_gate.py pins that with a test named for
    the counter-example -- delete the detector and the test fails.

RAIL is a DECLARED call-site argument, never inferred. Ancestry/env sniffing
classified 53/1 wrong on the live tree (both consumers run inside the bridge
process tree). Three values:

    consumer  zero-model-turn rails (followup / fastpath / push.sh / pin)
              -> drop/strip + ALERT on unregistered machine shape
    model     model-turn prose -> drop/strip; unregistered shape LOGS only
              (model prose has ~0.3% legitimate P1 hits: RIR, NO_PUSH, ...)
    unknown   undeclared call site -> alert, and the alert SAYS it is an
              undeclared rail, so the missing declaration is a work item
              that drains itself (declare -> alert stops)

ALERT READER. A log line is not a reader (DGN-1208: a control with no reader
is the defect this ticket is about). The alert sink is set by the host: the
bridge (bot.py) sends a Telegram notice to the owner chat and records a
durable marker AFTER the send succeeds; the push.sh hop appends the notice to
the outgoing body. Dedup key is (rail, token, day) -- token-only would bury
the first appearance of a known token on a NEWLY opened rail, which is
exactly the event the detector exists for.

This module is telegram-free and importable outside the bridge venv (the
push.sh sanitize hop runs it there, DGN-822): stdlib only; bridge.config is
imported lazily and guarded (defaults apply when unavailable).
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

# In-band sigil the emitter helper prints. Same `word::` family as send_file::
# / fold:: / link_preview:: (formatting.RECOGNIZED_COLON_MARKERS). MUST stay
# in sync with routines/lib/machine_signal.SIGIL (pinned by test).
MACHINE_SIGNAL_MARKER = "signal::"

RAIL_CONSUMER = "consumer"
RAIL_MODEL = "model"
RAIL_UNKNOWN = "unknown"
RAILS = (RAIL_CONSUMER, RAIL_MODEL, RAIL_UNKNOWN)

ACTION_DROP = "drop"
ACTION_STRIP = "strip"

# Seed registry: the DGN-1209 incident tokens (set-table's own SIGNAL_POLICY,
# a live instance, 2026-09-01). This is the regression pin for the leak that opened
# the ticket, NOT a growing list -- new emitters register by emitting through
# the helper (sigil), or per-instance via BRIDGE_MACHINE_TOKENS in
# .telegram_bot/.env (the only config layer the bridge reads).
SEED_MACHINE_TOKENS: Dict[str, str] = {
    "DAY_SOURCE": ACTION_DROP,
    "SESSION_UNTAGGED": ACTION_DROP,
    "RAMP_DECISION": ACTION_DROP,
    "PREP_PLANNED": ACTION_DROP,
    "WT_UNKNOWN": ACTION_DROP,
    # NEVER drop: the payload after "LOAD_DECISION: " is the Korean sentence
    # explaining why the load changed -- the one thing the owner asked for.
    "LOAD_DECISION": ACTION_STRIP,
}

_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# P1 (grill 깨진 전제 4): kept as-is -- recall over precision, detection only.
_MACHINE_SHAPE_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,})(?: |$)")
# Line-anchored fence toggle (same shape as sdk_bridge._CODE_FENCE_RE). NOT
# `text.find("```")`: an inline ``` in prose must not open a code region and
# hide the machine line behind it (grill 깨진 전제 3).
_FENCE_LINE_RE = re.compile(r"^\s*```")
# Sigil payload: TOKEN alone / TOKEN + space payload (kv form -> DROP), or
# TOKEN: + owner sentence (colon form -> STRIP to the sentence).
_SIGIL_BODY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)(?::(?:[ \t](.*))?|[ \t].*)?$")


class GateResult(NamedTuple):
    text: str
    rail: str
    dropped: Tuple[Tuple[str, str], ...]      # (token, line)
    stripped: Tuple[Tuple[str, str], ...]     # (token, kept sentence)
    unregistered: Tuple[str, ...]             # machine-shape tokens that PASSED


# --------------------------------------------------------------------------
# configuration (lazy, guarded -- push.sh hop may run without the venv)
# --------------------------------------------------------------------------

def _config_attr(name: str, default):
    try:
        from bridge import config as _cfg  # noqa: WPS433 (lazy on purpose)
    except Exception:
        return default
    return getattr(_cfg, name, default)


def gate_enabled() -> bool:
    return bool(_config_attr("BRIDGE_MACHINE_LINE_GATE", True))


def parse_token_spec(spec: str) -> Dict[str, str]:
    """Parse the BRIDGE_MACHINE_TOKENS knob: `TOKEN,TOKEN:strip,TOKEN:drop`.

    Malformed entries are skipped with a WARNING (never raise on config)."""
    out: Dict[str, str] = {}
    for raw in (spec or "").split(","):
        item = raw.strip()
        if not item:
            continue
        token, _, action = item.partition(":")
        token = token.strip()
        action = (action or ACTION_DROP).strip().lower()
        if not _TOKEN_RE.match(token) or action not in (ACTION_DROP, ACTION_STRIP):
            logger.warning("machine-line gate: ignoring malformed token spec %r", item)
            continue
        out[token] = action
    return out


def registered_tokens() -> Dict[str, str]:
    """Seed registry + per-instance BRIDGE_MACHINE_TOKENS (env wins on clash)."""
    merged = dict(SEED_MACHINE_TOKENS)
    merged.update(parse_token_spec(str(_config_attr("BRIDGE_MACHINE_TOKENS", "") or "")))
    return merged


# --------------------------------------------------------------------------
# the pure gate
# --------------------------------------------------------------------------

def _classify_line(
    stripped: str, registry: Dict[str, str]
) -> Tuple[str, Optional[str], Optional[str]]:
    """(verdict, token, kept_text) for ONE out-of-fence line.

    verdict: "drop" | "strip" | "unregistered" | "pass"
    """
    if stripped.startswith(MACHINE_SIGNAL_MARKER):
        body = stripped[len(MACHINE_SIGNAL_MARKER):].strip()
        m = _SIGIL_BODY_RE.match(body)
        if m is None:
            # Declared machine, malformed payload -> still machine. Drop.
            return ACTION_DROP, body.split(" ", 1)[0] or "?", None
        token, sentence = m.group(1), m.group(2)
        if sentence is not None and sentence.strip():
            return ACTION_STRIP, token, sentence.strip()
        return ACTION_DROP, token, None
    for token, action in registry.items():
        if stripped == token or stripped.startswith(token + " "):
            return ACTION_DROP, token, None          # kv form: payload is machine
        if stripped == token + ":":
            return ACTION_DROP, token, None
        if stripped.startswith(token + ": "):
            if action == ACTION_STRIP:
                sentence = stripped[len(token) + 2:].strip()
                if sentence:
                    return ACTION_STRIP, token, sentence
            return ACTION_DROP, token, None
    m = _MACHINE_SHAPE_RE.match(stripped)
    if m is not None:
        return "unregistered", m.group(1), None
    return "pass", None, None


def gate_machine_lines(text: str, rail: str = RAIL_UNKNOWN) -> GateResult:
    """Pure: apply the contract to raw PRE-RENDER text. No I/O, no logging.

    Fence handling is line-anchored; an UNCLOSED fence makes the whole text
    prose (conservative: a stray ``` must not hide machine lines). CRLF is
    tolerated (`\\r` is stripped before matching, line endings preserved).
    """
    if not text or not gate_enabled():
        return GateResult(text, rail, (), (), ())
    registry = registered_tokens()
    lines = text.splitlines(keepends=True)
    fence_count = sum(1 for ln in lines if _FENCE_LINE_RE.match(ln.rstrip("\r\n")))
    honor_fences = fence_count % 2 == 0
    kept: List[str] = []
    dropped: List[Tuple[str, str]] = []
    stripped_out: List[Tuple[str, str]] = []
    unregistered: List[str] = []
    in_code = False
    for ln in lines:
        core = ln.rstrip("\r\n")
        ending = ln[len(core):]
        if honor_fences and _FENCE_LINE_RE.match(core):
            in_code = not in_code
            kept.append(ln)
            continue
        if in_code:
            kept.append(ln)
            continue
        verdict, token, sentence = _classify_line(core.strip(), registry)
        if verdict == ACTION_DROP:
            dropped.append((token or "?", core))
            continue
        if verdict == ACTION_STRIP:
            stripped_out.append((token or "?", sentence or ""))
            kept.append((sentence or "") + ending)
            continue
        if verdict == "unregistered" and token and token not in unregistered:
            unregistered.append(token)
        kept.append(ln)
    return GateResult(
        "".join(kept), rail, tuple(dropped), tuple(stripped_out), tuple(unregistered)
    )


# --------------------------------------------------------------------------
# alert sink (host-provided reader) + the apply wrapper the choke points call
# --------------------------------------------------------------------------

AlertSink = Callable[[str, Tuple[str, ...], str], None]


def _default_alert_sink(rail: str, tokens: Tuple[str, ...], sample: str) -> None:
    # No reader registered: this is the DGN-1208 "control without a reader"
    # state and is only acceptable in tests / standalone tooling. Say so.
    logger.warning(
        "machine-line gate: unregistered machine-shape line(s) PASSED on rail=%s "
        "tokens=%s (NO ALERT SINK REGISTERED -- log only): %r",
        rail, ",".join(tokens), sample[:120],
    )


_alert_sink: AlertSink = _default_alert_sink


def set_machine_line_alert_sink(sink: Optional[AlertSink]) -> None:
    """Install the host's reader (bot.py: owner-chat notice; push.sh hop:
    body trailer). None restores the log-only default."""
    global _alert_sink
    _alert_sink = sink or _default_alert_sink


def apply_machine_line_gate(text: str, rail: str = RAIL_UNKNOWN) -> str:
    """The ONE call every outbound text render makes, right before rendering.

    Called from formatting.sanitize_message_for_telegram (push.sh / pin /
    btw) and bot.TelegramBot._render_prose_html_segments (followup / fastpath
    / model sends). Logs every DROP/STRIP (no silent drops -- a false positive
    must be visible to the owner and to the maintainer), and hands unregistered
    machine-shape hits to the alert sink on non-model rails.
    """
    if rail not in RAILS:
        logger.warning("machine-line gate: unknown rail %r -> treated as %s", rail, RAIL_UNKNOWN)
        rail = RAIL_UNKNOWN
    result = gate_machine_lines(text, rail)
    for token, line in result.dropped:
        logger.warning(
            "machine-line gate DROP rail=%s token=%s chars=%d line=%r",
            rail, token, len(line), line[:160],
        )
    for token, sentence in result.stripped:
        logger.warning(
            "machine-line gate STRIP rail=%s token=%s kept=%r", rail, token, sentence[:120]
        )
    if result.dropped and not result.text.strip():
        logger.warning(
            "machine-line gate: rail=%s body was machine-only (%d line(s)); nothing owner-facing remains",
            rail, len(result.dropped),
        )
    if result.unregistered:
        sample = next(
            (ln for ln in text.splitlines() if _MACHINE_SHAPE_RE.match(ln.strip())), ""
        )
        if rail == RAIL_MODEL:
            logger.info(
                "machine-line gate: model rail machine-shape tokens (log only): %s",
                ",".join(result.unregistered),
            )
        else:
            try:
                _alert_sink(rail, result.unregistered, sample)
            except Exception:  # noqa: BLE001 -- the alert must never break a send
                logger.exception("machine-line gate: alert sink raised")
    return result.text


# --------------------------------------------------------------------------
# dedup ledger: durable (rail, token, day) markers
# --------------------------------------------------------------------------

def today_key(now: Optional[_dt.date] = None) -> str:
    return (now or _dt.date.today()).strftime("%Y%m%d")


_MARKER_TTL_DAYS = 14


class MachineLineAlertLedger:
    """One marker file per (rail, token, day) under `<BOT_DATA_DIR>/machine-line-alerts`.

    The marker is written AFTER the alert reached its reader (bot.py) -- a
    failed send leaves no marker, so the next hit retries. It is also the
    durable trace the maintainer's audits read (DGN-1210 "durable marker" shape)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, rail: str, token: str, day: str) -> Path:
        safe_token = re.sub(r"[^A-Za-z0-9_]", "_", token)[:80] or "_"
        return self.directory / f"{rail}.{safe_token}.{day}"

    def unseen(self, rail: str, tokens: Iterable[str], day: Optional[str] = None) -> List[str]:
        day = day or today_key()
        out: List[str] = []
        for tok in tokens:
            if tok in out:
                continue
            try:
                if self._path(rail, tok, day).exists():
                    continue
            except OSError:
                pass
            out.append(tok)
        return out

    def mark(self, rail: str, tokens: Iterable[str], day: Optional[str] = None) -> None:
        day = day or today_key()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("machine-line gate: cannot create ledger dir %s: %s", self.directory, e)
            return
        for tok in tokens:
            try:
                self._path(rail, tok, day).write_text(day + "\n", encoding="utf-8")
            except OSError as e:
                logger.warning("machine-line gate: ledger mark failed for %s/%s: %s", rail, tok, e)
        self._purge(day)

    def _purge(self, day: str) -> None:
        try:
            cutoff = _dt.datetime.strptime(day, "%Y%m%d").date() - _dt.timedelta(days=_MARKER_TTL_DAYS)
        except ValueError:
            return
        try:
            for p in self.directory.iterdir():
                stamp = p.name.rsplit(".", 1)[-1]
                try:
                    if _dt.datetime.strptime(stamp, "%Y%m%d").date() < cutoff:
                        p.unlink()
                except (ValueError, OSError):
                    continue
        except OSError:
            return


# --------------------------------------------------------------------------
# alert copy (owner-facing -- WORDING PENDING OWNER CONFIRMATION, DGN-1209)
# --------------------------------------------------------------------------

# Placeholder copy. The final wording is a UX gate (owner confirmation
# pending); the i18n catalogs carry the same placeholder under
# machine_line_alert / machine_line_alert_undeclared. Do not treat this text
# as approved.
_ALERT_FALLBACK = (
    "[bridge] machine-shaped line(s) passed to the owner surface without "
    "registration: {tokens} (rail={rail})"
)
_ALERT_UNDECLARED_FALLBACK = (
    " -- this rail is UNDECLARED (call site passes no rail); declare it."
)


def format_alert_text(rail: str, tokens: Iterable[str]) -> str:
    """Owner-facing notice text for the sink. Guarded i18n (hop-safe)."""
    try:
        from bridge.formatting import _i18n  # noqa: WPS433
        template = _i18n("machine_line_alert", _ALERT_FALLBACK)
        undeclared = _i18n("machine_line_alert_undeclared", _ALERT_UNDECLARED_FALLBACK)
    except Exception:
        template, undeclared = _ALERT_FALLBACK, _ALERT_UNDECLARED_FALLBACK
    try:
        text = template.format(rail=rail, tokens=", ".join(tokens))
    except (KeyError, IndexError):
        text = _ALERT_FALLBACK.format(rail=rail, tokens=", ".join(tokens))
    if rail == RAIL_UNKNOWN:
        text += undeclared
    return text
