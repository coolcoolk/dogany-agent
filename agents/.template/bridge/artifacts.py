"""Shared artifact-render contract: arm-declared keyboard SPEC building.

DGN-966: artifact rendering (marker -> keyboard) used to be reimplemented per
send path, so every new path (fast-path DGN-801, push.sh) silently lost the
keyboard. This module is the SINGLE source for turning an idrill arm file into
a keyboard spec. Consumers:

  * bot.py -- wraps the spec rows into telegram InlineKeyboardMarkup for the
    model-turn, fast-path and proactive send paths;
  * routines/push.sh -- python hop builds the raw Telegram inline_keyboard
    JSON from the same spec (out-of-process rail, same keyboard guaranteed).

INVARIANT (DGN-822 pattern, same as bridge.formatting): this module MUST stay
importable OUTSIDE the bridge venv -- no python-telegram-bot import, no
bridge.config import. Paths are passed in explicitly; a keyboard spec is plain
lists/tuples.

Security invariants preserved from DGN-918/924/939: arm_id shape + path
containment, malformed-declaration drop, declared-value whitelist derivation.
"""

import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# arm_id is an opaque 8-char token used as a filename; constrain to chars safe
# as path components (no traversal, no shell-special chars). Single source --
# bot.py aliases this (was TelegramBot._IDRILL_ARM_ID_RE).
IDRILL_ARM_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# Telegram hard limit for callback_data (bytes, UTF-8). A button whose
# callback_data exceeds this would 400 the WHOLE sendMessage; the spec builder
# drops the offending button (never the message / never the row's siblings).
CALLBACK_DATA_MAX_BYTES = 64

# --- Arm state tree: DECLARED contract surface, not a tuning knob ---------
# ARM_SUBDIR pins the runtime arm-state tree to
#   PROJECT_ROOT/files/program/.idrill-arm/
# exactly as declared in bridge/IDRILL-ARM-CONTRACT.md section 1 ("Arming:
# write an arm file"). That contract file is the SINGLE producer<->consumer
# surface; this tuple is the consumer-side (bridge) declaration of its path,
# and the arm PRODUCER (the set-arm wiring on the other side of the
# contract) pins the same literal path independently of where its own code
# lives.
#
# WHAT IS BOUND TO THIS VALUE (change one, change ALL -- see the contract):
#   * the producer STATE_DIR (consumer-owned set-arm scripts write arms
#     here, pinned independently of their script directory);
#   * every bridge arm path: read_arm / consume_arm below, and bot.py's
#     read/consume/write helpers, all of which route through this module
#     (DGN-1123 collapsed bot.py's duplicate path literals into here);
#   * IDRILL-ARM-CONTRACT.md section 1 path prose (the declared contract);
#   * the depth-2 relative-reference assumption asserted below.
ARM_SUBDIR = ("files", "program", ".idrill-arm")

# Depth-2 assumption, ASSERTED (DGN-1123): the arm state tree hangs off a
# directory exactly TWO levels below PROJECT_ROOT (files/program/ today; the
# planned engine landing service/health/ keeps depth 2). Scripts and argv
# declarations rooted at that directory resolve `../../<sibling>` (e.g.
# ../../database) to PROJECT_ROOT-level siblings ONLY at depth 2 -- one more
# nesting level breaks every such relative reference with NO error (the
# observable symptom is silently dead or silently unconsumed inline
# buttons). This import-time check turns that silent breakage into a loud
# boot failure. To change the depth: update the producer STATE_DIR, the
# contract file, every `../../` consumer reference, and this check TOGETHER.
if len(ARM_SUBDIR) != 3 or ARM_SUBDIR[-1] != ".idrill-arm":
    raise AssertionError(
        "ARM_SUBDIR must be exactly (<depth-2 dir components>, '.idrill-arm')"
        " == 3 components; got %r. This path is a declared contract surface"
        " (bridge/IDRILL-ARM-CONTRACT.md section 1) with a depth-2"
        " relative-reference assumption -- do not change it in isolation."
        % (ARM_SUBDIR,)
    )

# Fallback Back-button label when the caller resolves no localized label
# (push.sh hop; bot.py passes messages.IDRILL_BACK_LABEL instead).
DEFAULT_BACK_LABEL = "« Back"


def arm_dir_for(project_root: Path) -> Path:
    """The idrill arm directory for an instance root."""
    d = Path(project_root)
    for part in ARM_SUBDIR:
        d = d / part
    return d


def arm_path_for(project_root: Path, arm_id: str) -> Optional[Path]:
    """Resolve the contained path of one arm file; None when it escapes.

    Single containment gate shared by read_arm / consume_arm and bot.py's
    write path. arm_id must already match IDRILL_ARM_ID_RE (callers
    validate); the strict containment re-check below keeps any future
    arm_id format change from escaping the arm directory.
    """
    cb_dir = arm_dir_for(project_root)
    arm_path = (cb_dir / arm_id).resolve()
    try:
        arm_path.relative_to(cb_dir.resolve())
    except ValueError:
        logger.warning("idrill: arm_id %r resolved outside arm dir", arm_id)
        return None
    return arm_path


def read_arm(project_root: Path, arm_id: str) -> Optional[dict]:
    """Read + parse an arm file; None on missing/malformed/contained-escape."""
    arm_path = arm_path_for(project_root, arm_id)
    if arm_path is None:
        return None
    try:
        return json.loads(arm_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def consume_arm(project_root: Path, arm_id: str) -> bool:
    """Consume (delete) an arm file -- the destructive twin of read_arm.

    Same path authority and containment discipline as read_arm; contract
    section 3 final-tap step ("CONSUME the file"). Returns True when the
    file existed and THIS call removed it; False when it was already absent
    (consumed earlier or never armed -- genuinely converged).

    Every OTHER failure is LOUD (raises OSError) instead of converging.
    Rationale (DGN-1123): a consume that silently no-ops -- e.g. after a bad
    relocation where the read path still resolves but this path misses --
    leaves the arm alive forever. The button keeps working and state never
    clears, so the failure direction is invisible; only a raise makes it
    observable.
    """
    arm_path = arm_path_for(project_root, arm_id)
    if arm_path is None:
        # A containment escape on a DELETE is never "converged".
        raise OSError(
            "idrill: refusing to consume arm %r: resolved outside arm dir"
            % (arm_id,)
        )
    try:
        arm_path.unlink()
    except FileNotFoundError:
        return False  # already consumed / never existed -- converged
    except OSError:
        logger.error(
            "idrill: arm consume FAILED (NOT converged) for %s", arm_path
        )
        raise
    return True


def _is_pair(entry) -> bool:
    """A flat button declaration: [label, value] -- both strings."""
    return (
        isinstance(entry, (list, tuple))
        and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
    )


def _is_row(entry) -> bool:
    """A grid row declaration: non-empty list of [label, value] pairs."""
    return (
        isinstance(entry, (list, tuple))
        and len(entry) > 0
        and all(_is_pair(b) for b in entry)
    )


def step_button_rows(arm: dict, step: str) -> Optional[List[List[List[str]]]]:
    """Normalize an arm's step_buttons[step] declaration to rows of buttons.

    Returns [[[label, value], ...], ...] (list of rows) or None when the step
    declares no usable buttons. Two declared shapes (DGN-966, shape detection
    is unambiguous because a flat entry's elements are strings while a row
    entry's elements are pairs):

      * legacy flat  [[label, value], ...]        -> ONE row (DGN-918/924/939
        byte-identical; every existing arm file unchanged);
      * grid         [[[label, value], ...], ...] -> declared rows
        (BotFather-style multi-row keyboard).

    Mixed lists: each row entry is a row; each stray pair entry becomes its
    own single-button row, order preserved. Malformed entries are dropped so a
    bad arm cannot inject arbitrary structure (existing discipline).
    """
    decl = arm.get("step_buttons")
    if not isinstance(decl, dict):
        return None
    entries = decl.get(step)
    if not isinstance(entries, list):
        return None
    if all(_is_pair(e) for e in entries) and entries:
        # Legacy flat shape: the whole list is one keyboard row.
        return [[[e[0], e[1]] for e in entries]]
    rows: List[List[List[str]]] = []
    for e in entries:
        if _is_row(e):
            rows.append([[b[0], b[1]] for b in e])
        elif _is_pair(e):
            rows.append([[e[0], e[1]]])
        # else: malformed entry -> dropped
    return rows or None


def declared_step_values(arm: dict, step: str) -> frozenset:
    """Set of tap values the arm declares as buttons for a step (whitelist)."""
    rows = step_button_rows(arm, step)
    if not rows:
        return frozenset()
    return frozenset(value for row in rows for _label, value in row)


def first_declared_step(arm: dict) -> Optional[str]:
    """The lowest-numbered declared step key, or None."""
    decl = arm.get("step_buttons")
    if not isinstance(decl, dict):
        return None
    digit_keys = sorted(
        (k for k in decl.keys() if isinstance(k, str) and k.isdigit()), key=int
    )
    return digit_keys[0] if digit_keys else None


def build_step_keyboard_spec(
    arm: dict,
    arm_id: str,
    step: str,
    back_label: Optional[str] = None,
) -> Tuple[Optional[List[List[Tuple[str, str]]]], Optional[str]]:
    """Build the keyboard SPEC for a step: (rows of (label, callback_data), text).

    Returns (None, None) when the step declares no buttons. The spec is plain
    data -- the caller wraps it for its rail (InlineKeyboardMarkup in bot.py,
    inline_keyboard JSON in the push.sh hop) so the keyboard is IDENTICAL on
    every path (DGN-966 success criterion).

    Back row: appended only when the arm opts in (nav_back: true) and `step`
    is not the first declared step (DGN-939, unchanged). Label priority:
    arm step_back_label > caller back_label > DEFAULT_BACK_LABEL.

    64-byte guard: a button whose callback_data exceeds Telegram's hard limit
    is DROPPED (warning logged); its siblings and the message survive. An
    all-buttons-dropped step returns (None, None) (fail-soft, marker already
    stripped upstream).
    """
    rows = step_button_rows(arm, step)
    if not rows:
        return None, None
    spec_rows: List[List[Tuple[str, str]]] = []
    for row in rows:
        spec_row: List[Tuple[str, str]] = []
        for label, value in row:
            cb = f"idrill:{arm_id}:{step}:{value}"
            if len(cb.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
                logger.warning(
                    "idrill: callback_data over %d bytes dropped "
                    "(arm=%s step=%s label=%r)",
                    CALLBACK_DATA_MAX_BYTES, arm_id, step, label,
                )
                continue
            spec_row.append((label, cb))
        if spec_row:
            spec_rows.append(spec_row)
    if not spec_rows:
        return None, None
    if arm.get("nav_back") is True:
        first = first_declared_step(arm)
        if first is not None and step != first:
            declared_label = arm.get("step_back_label")
            if isinstance(declared_label, str) and declared_label:
                label = declared_label
            elif isinstance(back_label, str) and back_label:
                label = back_label
            else:
                label = DEFAULT_BACK_LABEL
            spec_rows.append([(label, f"idrill:{arm_id}:back")])
    step_text = arm.get("step_text")
    text = step_text.get(step) if isinstance(step_text, dict) else None
    text = text if isinstance(text, str) and text else None
    return spec_rows, text


def initial_keyboard_json(project_root: Path, arm_id: str) -> Optional[dict]:
    """push.sh hop helper: the step-1 keyboard as raw Telegram JSON structures.

    Returns {"text": step_text_1, "reply_markup": {"inline_keyboard": [...]}}
    or None when the arm is invalid/missing/button-less/text-less (fail-soft;
    same gates as bot.py's initial-keyboard render, so path behavior matches).
    """
    if not IDRILL_ARM_ID_RE.match(arm_id):
        logger.warning("idrill: initial marker arm_id failed validation: %r", arm_id)
        return None
    arm = read_arm(project_root, arm_id)
    if arm is None:
        logger.warning("idrill: initial marker arm %s missing/expired", arm_id)
        return None
    spec_rows, text = build_step_keyboard_spec(arm, arm_id, "1")
    if not spec_rows:
        logger.warning("idrill: arm %s declares no step_buttons[1]", arm_id)
        return None
    if not text:
        logger.warning("idrill: arm %s declares no step_text[1]", arm_id)
        return None
    return {
        "text": text,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": lbl, "callback_data": cb} for lbl, cb in row]
                for row in spec_rows
            ]
        },
    }


def step1_renders(arm: Optional[dict], arm_id: str) -> bool:
    """True when the arm will render a step-1 keyboard (buttons AND prompt).

    Same two gates as bot.py's initial-keyboard render and
    initial_keyboard_json, without their warnings (the render logs its own).
    """
    if arm is None or not IDRILL_ARM_ID_RE.match(arm_id or ""):
        return False
    spec_rows, text = build_step_keyboard_spec(arm, arm_id, "1")
    return bool(spec_rows) and bool(text)


# ---------------------------------------------------------------------------
# DGN-983 / DGN-1527: prose count-question beside an armed keyboard.
#
# The RULE lives in routines/lib/output_gate.py (IDRILL_PROSE_Q) -- the
# output-gate rule registry, one source.  This is only the bridge send-seat
# adapter: it loads that lib from the instance, consults the kill switch
# (routines/gate-switch.py slug output-gate-idrill-prose) and appends the
# counter line.  Stage 1 = normalize + count; there is no block here.
#
# Fail direction is UNTOUCHED: lib absent / older lib without the function /
# any error -> the message goes out exactly as the model wrote it.  The one
# exception is the kill switch itself, which fails toward ON like every
# gate-switch caller (a broken switch never disarms a gate).
# ---------------------------------------------------------------------------
IDRILL_PROSE_GATE_SLUG = "output-gate-idrill-prose"
IDRILL_PROSE_RULE_ID = "IDRILL_PROSE_Q"
# Same file the output-gate Stop hook appends to, same line shape, so one
# grep counts the rule across both seats.
OUTPUT_GATE_CANARY_LOG = "output-gate-canary.log"

_path_module_cache: dict = {}


def _load_path_module(name: str, path: Path):
    """Import a module from a file path once per process; None on failure."""
    key = str(path)
    if key in _path_module_cache:
        return _path_module_cache[key]
    mod = None
    try:
        import importlib.util

        if path.is_file():
            spec = importlib.util.spec_from_file_location(name, key)
            if spec is not None and spec.loader is not None:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
    except Exception:
        logger.warning("idrill prose gate: cannot load %s", key, exc_info=True)
        mod = None
    _path_module_cache[key] = mod
    return mod


def _append_canary(log_dir: Optional[Path], metrics: dict, excerpt: str) -> None:
    if log_dir is None:
        return
    try:
        import time

        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / OUTPUT_GATE_CANARY_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s  %s  %s  [%s]\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                IDRILL_PROSE_RULE_ID,
                " ".join("%s=%s" % kv for kv in metrics.items()),
                " ".join(excerpt.split())[:120],
            ))
    except Exception:
        pass  # the counter never wedges a send


def normalize_idrill_prose(
    text: str,
    project_root: Path,
    keyboard_ready: bool,
    log_dir: Optional[Path] = None,
    arm_id: str = "",
    rail: str = "model",
) -> Tuple[str, bool]:
    """Remove prose count-questions from a keyboard-arming message body.

    `text` is the display body AFTER the [[IDRILL:..]] marker was stripped;
    `keyboard_ready` is step1_renders() for that marker's arm -- without a
    keyboard the prose question may be the owner's only prompt, so nothing
    is touched.  Returns (text, changed).
    """
    if not text or not keyboard_ready:
        return text, False
    lib = _load_path_module(
        "output_gate", Path(project_root) / "routines" / "lib" / "output_gate.py")
    fn = getattr(lib, "normalize_idrill_prose_question", None)
    if fn is None:
        return text, False
    try:
        new_text, removed = fn(text, keyboard_armed=True)
    except Exception:
        logger.warning("idrill prose gate: normalizer raised", exc_info=True)
        return text, False
    if not removed:
        return text, False
    enabled = True
    gs = _load_path_module(
        "gate_switch", Path(project_root) / "routines" / "gate-switch.py")
    if gs is not None:
        try:
            enabled = bool(gs.is_enabled(IDRILL_PROSE_GATE_SLUG, str(project_root)))
        except Exception:
            enabled = True
    stage = "normalized" if enabled else "suppressed-off"
    _append_canary(
        log_dir,
        {"stage": stage, "rail": rail, "arm": arm_id or "-",
         "sentences": len(removed)},
        " | ".join(removed),
    )
    if not enabled:
        try:
            # records the would-fire in gate-switch-suppressed.log (status)
            gs.off_notice(IDRILL_PROSE_GATE_SLUG,
                          "would remove: " + " | ".join(removed),
                          str(project_root))
        except Exception:
            pass
        logger.info("idrill prose gate OFF: %d count-question(s) left in "
                    "(arm %s)", len(removed), arm_id)
        return text, False
    logger.info("idrill prose gate: removed %d count-question(s) beside "
                "keyboard (arm %s)", len(removed), arm_id)
    return new_text, True


# ---------------------------------------------------------------------------
# DGN-1500: layered term dictionary (framework / kit / module).
#
# Same seat as the idrill prose gate above: the RULE (matcher) is output_gate
# TERM_LEAK, the DATA is read by routines/lib/term_dict.py from this
# instance's layer files, and this adapter only wires them to the send seat,
# the kill switch (gate-switch slug output-gate-term-dict) and the canary log.
# Count-first: every hit is logged with its sentence; only entries the
# loader marked safe-to-rewrite are rewritten; nothing is ever blocked.
#
# Fail direction is UNTOUCHED: lib absent / no dictionary / a broken layer
# file / any error -> the message goes out exactly as the model wrote it.
# The kill switch fails toward ON like every gate-switch caller.
# ---------------------------------------------------------------------------
TERM_DICT_GATE_SLUG = "output-gate-term-dict"
TERM_LEAK_RULE_ID = "TERM_LEAK"
TERM_DICT_LOAD_RULE_ID = "TERM_DICT"


def _append_canary_line(log_dir: Optional[Path], rule_id: str, metrics: dict,
                        excerpt: str) -> None:
    if log_dir is None:
        return
    try:
        import time

        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / OUTPUT_GATE_CANARY_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s  %s  %s  [%s]\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                rule_id,
                " ".join("%s=%s" % kv for kv in metrics.items()),
                " ".join(excerpt.split())[:160],
            ))
    except Exception:
        pass  # the counter never wedges a send


def normalize_term_leaks(
    text: str,
    project_root: Path,
    log_dir: Optional[Path] = None,
    rail: str = "model",
) -> Tuple[str, bool]:
    """Count dictionary mistranslations in a model body; rewrite safe ones.

    Returns (text, changed). One TERM_LEAK canary line per hit:
    stage=rewritten (safe entry, switch on) | suppressed-off (safe entry,
    switch off -- untouched) | counted (entry not safe to rewrite).
    """
    if not text:
        return text, False
    lib_dir = Path(project_root) / "routines" / "lib"
    td = _load_path_module("term_dict", lib_dir / "term_dict.py")
    og = _load_path_module("output_gate", lib_dir / "output_gate.py")
    load = getattr(td, "load", None)
    find = getattr(og, "find_term_leaks", None)
    apply_rw = getattr(og, "apply_term_rewrites", None)
    if load is None or find is None or apply_rw is None:
        return text, False

    def _load_note(kind: str, detail: str) -> None:
        logger.info("term dictionary %s: %s", kind, detail)
        _append_canary_line(log_dir, TERM_DICT_LOAD_RULE_ID,
                            {"stage": "load", "kind": kind}, detail)

    try:
        rules = load(str(project_root), log=_load_note)
        hits = find(text, rules) if rules else []
    except Exception:
        logger.warning("term dictionary gate: check raised", exc_info=True)
        return text, False
    if not hits:
        return text, False
    enabled = True
    gs = None
    if any(h.get("rewrite") for h in hits):
        gs = _load_path_module(
            "gate_switch", Path(project_root) / "routines" / "gate-switch.py")
        if gs is not None:
            try:
                enabled = bool(gs.is_enabled(TERM_DICT_GATE_SLUG, str(project_root)))
            except Exception:
                enabled = True
    for h in hits:
        if not h.get("rewrite"):
            stage = "counted"
        else:
            stage = "rewritten" if enabled else "suppressed-off"
        _append_canary_line(
            log_dir, TERM_LEAK_RULE_ID,
            {"stage": stage, "rail": rail, "layer": h["layer"],
             "owner": h["owner"], "id": h["id"], "form": h["match"]},
            h["sentence"],
        )
    rewritable = [h for h in hits if h.get("rewrite")]
    if not rewritable:
        return text, False
    if not enabled:
        try:
            gs.off_notice(TERM_DICT_GATE_SLUG,
                          "would rewrite: " + ", ".join(
                              "%s->%s" % (h["match"], h["canonical"])
                              for h in rewritable),
                          str(project_root))
        except Exception:
            pass
        return text, False
    try:
        new_text = apply_rw(text, hits)
    except Exception:
        logger.warning("term dictionary gate: rewrite raised", exc_info=True)
        return text, False
    if new_text == text:
        return text, False
    logger.info("term dictionary gate: rewrote %d term(s)", len(rewritable))
    return new_text, True
