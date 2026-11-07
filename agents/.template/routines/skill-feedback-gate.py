#!/usr/bin/env python3
"""Skill-feedback gate: three-mode hook so skill feedback deterministically
enters a propose-edit loop.

Mode A  --record  (PostToolUse, matcher "Skill"): after a Skill runs, write a
  marker file <project_root>/.claude/.last-skill.json =
  {"skill": <name>, "ts": <epoch int>, "session_id": <id>, "injected": 0,
   "stop_nudged": false}, overwritten each time. Meta skills (skill-creator,
  memory-search) are NOT recorded: the propose-edit procedure itself invokes
  them, and stamping them would overwrite the marker mid-feedback-loop with
  the wrong skill.

  The SAME --record call also appends one line to the skill-usage ledger
  (~/.dogany/skill-usage/<slug>-<year>.jsonl, one file per instance per
  year) -- ALL skills, including the ones the marker denylist skips. The
  denylist is a feedback-loop-only concern (do not clobber the marker
  mid-loop); the usage ledger has no such conflict and would be skewed by
  excluding meta skills (memory-search is plausibly the most-used skill).
  Ledger event = {v, ts, skill, slug, actor, session_id, source}. NEVER the
  tool_input args -- those can carry full task text (privacy gate). Append
  is its own try/except so a ledger failure never affects the marker write
  or the denylist-skip return. See routines/skill-usage.py for the reader.

DGN-1446 (O2): the same --record call also checks whether the loaded
  SKILL.md body declares itself a discovery stub ("discovery stub" or "not
  the usage guide"). If so it emits a PostToolUse additionalContext telling
  the model to run the pointed command now and then do the user's request,
  not to end the turn on the stub. Skill bodies are untouched; a normal body
  emits nothing. Fail-open like the rest of the hook.

Mode B  --inject  (UserPromptSubmit): read the marker; if it is missing,
  corrupt, stale (older than the window, default 1800s, env override
  DOGANY_SKILL_FEEDBACK_WINDOW), from another session (cron / subagent
  poisoning guard), or already injected MAX_INJECTIONS times, exit silently
  with no output. Otherwise emit an additionalContext note (same JSON/stdout
  format as the memory recall hook) reminding the model to run the
  propose-edit loop IF the current message is feedback about that skill;
  semantic judgment stays with the model. Each injection increments the
  marker's counter so the note appears at most MAX_INJECTIONS times per
  skill run instead of on every message in the window.

Mode C  --stop  (Stop): proactive propose-at-completion. v1 is reactive (only
  fires when the user sends feedback). v2 closes the gap where the model works
  around a skill's missing step mid-task and never offers to fold the fix back
  in. On Stop, if a skill ran THIS session and the marker is fresh (separate,
  wider STOP window -- a task legitimately runs long after the skill fires),
  BLOCK the stop ONCE with a reason telling the model to self-check for
  deviation and, if it deviated, propose a skill update now (approval-gated,
  same v1 rules). The block fires at most once per marker (stop_nudged flag),
  and never when stop_hook_active is already true (Claude Code's native
  loop guard). Any second Stop passes through unconditionally.

Fail-open contract (mirrors token-gate.py / card-followup.py): ANY exception
exits 0 with no output. The hook must never block or materially delay a turn.
The one deliberate exception is --stop, which emits a single {"decision":
"block"} at most once per marker; every failure path there still allows.
"""
import json
import os
import sys
import time
import hashlib
import re
import shlex

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from hook_paths import instance_root
try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""

MARKER_REL = os.path.join(".claude", ".last-skill.json")
USAGE_DIR_NAME = "skill-usage"
USAGE_SCHEMA_V = 1
DEFAULT_WINDOW_SEC = 1800
# Stop nudge uses a wider window: a task can legitimately run long after the
# skill fired within the same session, and the session guard already prevents
# cross-session poisoning. Env override DOGANY_SKILL_STOP_WINDOW.
DEFAULT_STOP_WINDOW_SEC = 10800
MAX_INJECTIONS = 2
MAX_SKILL_NAME = 100
PROPOSAL_STATE_REL = os.path.join(".claude", ".skill-proposals.json")
DECLINE_DAYS = 30

# Transcript language is deliberately narrow. These are agent self-reports,
# not guesses about owner sentiment: the hook only proposes when the trace
# records a workaround/manual repair, or repeats the same four-step tool run.
TOOL_TYPES = {"tool_use", "tool"}

# Meta skills invoked BY the propose-edit procedure (or routinely before
# answering). Recording them would clobber the marker mid-loop (grill M2).
RECORD_DENYLIST = {
    "skill-creator",
    "dogany-skill-creator",
    "memory-search",
    "dogany-memory-search",
}


def _window_sec():
    try:
        v = int(os.environ.get("DOGANY_SKILL_FEEDBACK_WINDOW", ""))
        if v > 0:
            return v
    except Exception:
        pass
    return DEFAULT_WINDOW_SEC


def _stop_window_sec():
    try:
        v = int(os.environ.get("DOGANY_SKILL_STOP_WINDOW", ""))
        if v > 0:
            return v
    except Exception:
        pass
    return DEFAULT_STOP_WINDOW_SEC


def _marker_path(cwd):
    return os.path.join(instance_root(), MARKER_REL)


def _write_marker(path, payload):
    """Atomic write: temp file + os.replace, so readers never see a torn file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _usage_slug(cwd):
    """Instance slug for the ledger filename: env DOGANY_SLUG override ->
    config/agent.conf SLUG= (resolved against the enrolled/installed instance,
    regardless of the tool call's working directory) -> the
    neutral fallback "unknown" (template ships no SLUG= by design, see
    config/agent.conf comments)."""
    override = os.environ.get("DOGANY_SLUG")
    if override:
        return override
    conf_path = os.path.join(instance_root(), "config", "agent.conf")
    return conf_get("SLUG", conf_path) or "unknown"


def _append_usage(skill, data, source="hook"):
    """Append one skill-usage ledger line. Fail-open (own try/except): a
    ledger write must never affect the marker write or block the turn.
    Name + timestamp only -- tool_input/args are NEVER read here."""
    try:
        home = os.environ.get("DOGANY_HOME") or os.path.expanduser("~/.dogany")
        usage_dir = os.path.join(home, USAGE_DIR_NAME)
        os.makedirs(usage_dir, exist_ok=True)
        cwd = data.get("cwd") or os.getcwd()
        slug = _usage_slug(cwd)
        ts = int(time.time())
        record = {
            "v": USAGE_SCHEMA_V,
            "ts": ts,
            "skill": skill,
            "slug": slug,
            "actor": os.environ.get("DOGANY_ACTOR", "unknown"),
            "session_id": data.get("session_id") or "",
            "source": source,
        }
        year = time.strftime("%Y", time.gmtime(ts))
        path = os.path.join(usage_dir, "{}-{}.jsonl".format(slug, year))
        # Single write, O_APPEND-opened file -> effectively atomic across
        # concurrent writers (same contract note as the design skeleton).
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


STUB_MARKERS = ("discovery stub", "not the usage guide")

STUB_NOTE = (
    "[skill-stub gate] Skill '{skill}' loaded a discovery stub, not the usage "
    "guide. Loading it did NOT do the user's request. Run the command the "
    "stub points to now, then carry out the user's original request with "
    "real tool calls. Do not end the turn on the stub text."
)


def _flatten_text(obj, depth=0):
    """Collect every string inside a tool_response (str / dict / list)."""
    if depth > 6:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(_flatten_text(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return "\n".join(_flatten_text(v, depth + 1) for v in obj)
    return ""


def _skill_body(data, skill):
    """Loaded skill body: the tool_response text, else SKILL.md on disk."""
    # The real Skill tool_response is metadata only ({"success": true,
    # "commandName": "<name>"}), never the body -- so the on-disk SKILL.md
    # is always read too, not only when the response is empty.
    parts = [_flatten_text(data.get("tool_response"))]
    for base in (os.path.join(instance_root(), ".claude", "skills"),
                 os.path.expanduser("~/.claude/skills")):
        try:
            with open(os.path.join(base, skill, "SKILL.md"), "r",
                      errors="replace") as fh:
                parts.append(fh.read(65536))
                break
        except Exception:
            continue
    return "\n".join(parts)


def _is_stub(body):
    low = (body or "").lower()
    return any(m in low for m in STUB_MARKERS)


def _stub_notice(data, skill):
    """PostToolUse: additionalContext when the loaded body is a stub."""
    try:
        if not _is_stub(_skill_body(data, skill)):
            return
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": STUB_NOTE.format(skill=skill),
            }
        }), flush=True)
    except Exception:
        pass


def _record(data):
    """PostToolUse (matcher Skill): stamp the marker with skill name + epoch."""
    if data.get("hook_event_name") not in (None, "PostToolUse"):
        return
    if data.get("tool_name") != "Skill":
        return

    tool_input = data.get("tool_input") or {}
    skill = tool_input.get("skill") or ""
    if not isinstance(skill, str) or not skill.strip():
        return
    skill = skill.strip()[:MAX_SKILL_NAME]

    # Usage ledger: ALL skills, denylist does not apply (design report --
    # the denylist is a feedback-loop-only concern, not a usage-accounting
    # one; excluding meta skills would skew the ledger, memory-search is
    # plausibly the single most-used skill).
    _append_usage(skill, data)

    _stub_notice(data, skill)

    if skill in RECORD_DENYLIST:
        return

    cwd = data.get("cwd") or os.getcwd()
    path = _marker_path(cwd)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    payload = {
        "skill": skill,
        "ts": int(time.time()),
        "session_id": data.get("session_id") or "",
        "injected": 0,
        "stop_nudged": False,
    }
    _write_marker(path, payload)


NOTE = (
    "[skill update contract] Recurring behavior must be persisted or offered as a concrete save-as-skill question. "
    "Already authorized changes need no new approval. Existing skill edits capture a u1 section/old/new mapping "
    "with skill-layer.py update-begin; update-check verifies the candidate before writing and --applied after writing. "
    "u4/s8 must recheck the actual disk hash. Session memory and appended replacement rulings do not count. "
    "[skill-feedback gate] Skill '{skill}' ran {mins} min ago. If the user's "
    "current message is feedback on that skill's behavior (complaint, "
    "correction, improvement request): do NOT hot-patch or silently adjust; "
    "(1) reuse existing user authorization, or propose a concrete skill edit and wait for approval; (2) read the "
    "skill-creator skill (dogany-skill-creator in product instances) before "
    "editing; (3) edit, verify, then log one line to memory. If it is a "
    "dogany-* framework skill OR a bundled lifekit skill (diet-log, "
    "workout-log, appointment-log, relationship, task-update), do not edit its shipped source "
    "in place: copy it to the instance-owned skill location, set layer: local, register it with "
    "skill-layer.py register, and fix that copy now; file the upstream report in parallel. If the root "
    "cause is in service/product code (bridge, runtime, memory engine, "
    "lifekit core), do not edit locally: say so and draft a product issue "
    "report. If this message is not feedback about the skill, ignore this "
    "note silently."
)

STOP_REASON = (
    "[skill update contract] Recurring behavior must be persisted or offered as a concrete save-as-skill question. "
    "Already authorized changes need no new approval. Existing skill edits capture a u1 section/old/new mapping "
    "with skill-layer.py update-begin; update-check verifies the candidate before writing and --applied after writing. "
    "u4/s8 must recheck the actual disk hash. Session memory and appended replacement rulings do not count. "
    "[skill-feedback gate] Skill '{skill}' ran this session. Before you "
    "finish: did the flow deviate from, work around, or manually supplement "
    "that skill's documented procedure (e.g. you did a step the skill omits, "
    "or fixed the skill's output by hand)? If YES -> reuse existing authorization, or propose the concrete "
    "skill update to the user NOW, approval-gated: state the exact edit, wait "
    "for approval, read the skill-creator skill (dogany-skill-creator in "
    "product instances) before editing. If it is a dogany-* framework skill "
    "OR a bundled lifekit skill (diet-log, workout-log, appointment-log, "
    "relationship, task-update), do NOT edit its shipped source in place -- copy it to the "
    "instance-owned skill location, set layer: local, register it with skill-layer.py register, "
    "fix the copy now, and file the upstream report in parallel. If root cause is in service/product "
    "code (bridge, runtime, memory engine, lifekit core), draft a product "
    "issue report rather than editing locally. If there was NO deviation, "
    "just finish normally -- this check will not fire again this turn."
)


def _inject(data):
    """UserPromptSubmit: emit the note if the marker is fresh, else silent."""
    cwd = data.get("cwd") or os.getcwd()
    path = _marker_path(cwd)
    try:
        with open(path, "r") as fh:
            marker = json.load(fh)
    except Exception:
        return  # missing / corrupt -> silent

    if not isinstance(marker, dict):
        return
    skill = marker.get("skill")
    ts = marker.get("ts")
    if not isinstance(skill, str) or not skill.strip():
        return
    try:
        ts = int(ts)
    except Exception:
        return

    age = int(time.time()) - ts
    if age < 0 or age > _window_sec():
        return  # stale -> silent

    # Session guard (grill M3): only inject into the session that ran the
    # skill. Kills cron-headless and subagent marker poisoning.
    marker_sid = marker.get("session_id") or ""
    my_sid = data.get("session_id") or ""
    if not marker_sid or not my_sid or marker_sid != my_sid:
        return

    # Injection cap (grill M1): at most MAX_INJECTIONS notes per skill run.
    try:
        injected = int(marker.get("injected", 0))
    except Exception:
        injected = 0
    if injected >= MAX_INJECTIONS:
        return
    marker["injected"] = injected + 1
    _write_marker(path, marker)

    mins = max(0, age // 60)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": NOTE.format(
                skill=skill.strip()[:MAX_SKILL_NAME], mins=mins
            ),
        }
    }
    print(json.dumps(out), flush=True)



def _transcript_rows(path):
    rows = []
    if not path:
        return rows
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def _message_content(row):
    msg = row.get("message") if isinstance(row.get("message"), dict) else row
    return msg.get("content", "") if isinstance(msg, dict) else ""


def _tool_signature(item):
    """Stable signature: operation plus command head or file pattern."""
    name = item.get("name") or item.get("tool_name")
    if not isinstance(name, str) or name == "Skill":
        return ""
    inp = item.get("input") or item.get("tool_input") or {}
    if not isinstance(inp, dict):
        inp = {}
    if name == "Bash":
        command = inp.get("command") or inp.get("cmd") or ""
        try:
            words = shlex.split(command)
        except Exception:
            words = str(command).split()
        shape = " ".join(words[:2]) if words else "?"
    else:
        path = inp.get("file_path") or inp.get("path") or inp.get("pattern") or "?"
        shape = os.path.basename(str(path)) or "?"
    return name + ":" + shape


def _transcript_evidence(rows):
    """Return per-turn signatures and structural skill/write evidence."""
    turns, skill_seen, post_skill_writes = [], False, []
    for row in rows:
        content = _message_content(row)
        if not isinstance(content, list):
            continue
        turn = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") not in TOOL_TYPES:
                continue
            name = item.get("name") or item.get("tool_name")
            if name == "Skill":
                skill_seen = True
                continue
            sig = _tool_signature(item)
            if sig:
                turn.append(sig)
            if skill_seen and name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                post_skill_writes.append(sig)
        if turn:
            turns.append(turn)
    return turns, post_skill_writes


def _repeated_four_step(turns):
    """Require the same normalized 4+ step procedure in distinct turns."""
    for left in range(len(turns)):
        for right in range(left + 1, len(turns)):
            limit = min(8, len(turns[left]), len(turns[right]))
            for width in range(limit, 3, -1):
                for a in range(len(turns[left]) - width + 1):
                    first = turns[left][a:a + width]
                    for b in range(len(turns[right]) - width + 1):
                        if first == turns[right][b:b + width]:
                            return first
    return []


def _proposal_state_path(cwd):
    return os.path.join(instance_root(), PROPOSAL_STATE_REL)


def _load_proposal_state(path):
    try:
        with open(path, "r") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_proposal_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_marker(path, state)
    except Exception:
        pass


def _proposal_signal(data, marker):
    turns, post_skill_writes = _transcript_evidence(
        _transcript_rows(data.get("transcript_path")))
    skill = marker.get("skill", "") if isinstance(marker, dict) else ""
    if skill and post_skill_writes:
        return "edit", skill, "post-skill write " + post_skill_writes[-1]
    repeated = _repeated_four_step(turns)
    if repeated and not skill:
        return "new", "repeated-procedure", "repeated normalized steps " + " -> ".join(repeated)
    return None


def _proposal_reason(kind, target, change):
    action = "Update skill" if kind == "edit" else "Create skill"
    return (
        "[in-flow skill proposal] Structural evidence exists ({change}). Perform "
        "one bounded self-check: did the work actually supplement/repair the skill, "
        "or is this repeated procedure genuinely reusable? If no, finish silently. "
        "If yes, inspect the target and draft the ACTUAL unified diff for SKILL.md "
        "(for a new skill, the complete proposed SKILL.md) before asking anything. "
        "Classify once: near the owner's Why or changing owner-visible What -> show "
        "that diff with exactly `1. {action}` and `2. Keep current behavior`, then "
        "wait; otherwise apply it through dogany-skill-creator now and report the "
        "exact diff afterward. Never defer to consolidation. Target: `{target}`."
    ).format(action=action, target=target, change=change)


def _in_flow_stop(data):
    """Emit at most one topic proposal per session; consolidation never calls it."""
    if data.get("stop_hook_active") is True:
        return False
    cwd = data.get("cwd") or os.getcwd()
    try:
        with open(_marker_path(cwd), "r") as fh:
            marker = json.load(fh)
    except Exception:
        marker = {}
    signal = _proposal_signal(data, marker)
    if not signal:
        return False
    kind, target, change = signal
    sid = data.get("session_id") or ""
    topic = hashlib.sha256((kind + "\0" + target + "\0" + change).encode()).hexdigest()[:16]
    path = _proposal_state_path(cwd)
    state = _load_proposal_state(path)
    now = int(time.time())
    key = sid + ":" + topic
    decline_until = state.get("declined", {}).get(topic, 0)
    try:
        if int(decline_until) > now:
            return False
    except Exception:
        pass
    # Only an owner's exact selection of this topic's second menu option is a
    # decline. Words elsewhere in the transcript have no effect.
    pending = state.get("pending", {}).get(sid)
    answers = []
    for row in _transcript_rows(data.get("transcript_path")):
        msg = row.get("message") if isinstance(row.get("message"), dict) else row
        if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(msg.get("content"), str):
            answers.append(msg["content"].strip().casefold())
    if pending == topic and answers and answers[-1] in ("2", "keep current behavior"):
        state.setdefault("declined", {})[topic] = now + DECLINE_DAYS * 86400
        state.setdefault("pending", {}).pop(sid, None)
        _save_proposal_state(path, state)
        return True
    if key in state.get("offered", {}):
        return True  # handled topic: suppress the legacy generic nudge
    state.setdefault("offered", {})[key] = now
    state.setdefault("pending", {})[sid] = topic
    _save_proposal_state(path, state)
    out = {"decision": "block", "reason": _proposal_reason(kind, target, change)}
    _block_or_notice(out, cwd)
    return True


# --- central switch (DGN-1231) ---------------------------------------------

_GATE_SLUG = "skill-feedback-stop"
_GATE_SWITCH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "gate-switch.py")


def _load_gate_switch():
    """Central gate on/off switch (DGN-1231).  None -> switch machinery
    absent/broken: the gate stays armed (fail toward protection)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gate_switch", _GATE_SWITCH_PATH)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _block_or_notice(out, cwd=None):
    """Consulted only at the would-block moment: switched off -> emit a
    non-blocking off-notice instead of the Stop block (DGN-1231)."""
    gs = _load_gate_switch()
    if gs is not None:
        try:
            if not gs.is_enabled(_GATE_SLUG, cwd):
                print(json.dumps(
                    gs.off_notice(_GATE_SLUG, out.get("reason", ""), cwd),
                    ensure_ascii=False), flush=True)
                return
        except Exception:
            pass  # broken switch never disarms the gate
    print(json.dumps(out), flush=True)


def _stop(data):
    """Stop: proactively nudge a propose-at-completion, at most ONCE per marker.

    Anti-loop, two independent belts:
      1. Claude Code's native stop_hook_active flag -- if a prior Stop hook
         already blocked this turn, it is true; we pass through immediately.
      2. Our own stop_nudged marker flag -- set the first time we block, so a
         later Stop event (even a fresh turn, or after stop_hook_active clears)
         never blocks on the same marker again.
    Every other path (missing/corrupt/stale marker, wrong session, no skill)
    allows the stop with no output.
    """
    # In-flow detection owns the first Stop when a transcript contains a
    # concrete signal. The legacy skill-run self-check remains as fallback.
    if _in_flow_stop(data):
        return

    # Belt 1: never block if a Stop hook already ran this turn.
    if data.get("stop_hook_active") is True:
        return

    cwd = data.get("cwd") or os.getcwd()
    path = _marker_path(cwd)
    try:
        with open(path, "r") as fh:
            marker = json.load(fh)
    except Exception:
        return  # missing / corrupt -> allow

    if not isinstance(marker, dict):
        return
    skill = marker.get("skill")
    ts = marker.get("ts")
    if not isinstance(skill, str) or not skill.strip():
        return
    try:
        ts = int(ts)
    except Exception:
        return

    # Belt 2: already nudged for this marker -> allow unconditionally.
    if marker.get("stop_nudged") is True:
        return

    age = int(time.time()) - ts
    if age < 0 or age > _stop_window_sec():
        return  # stale -> allow

    # Session guard: only nudge the session that ran the skill. A marker from
    # a different session (cron / subagent / earlier session) must not block.
    marker_sid = marker.get("session_id") or ""
    my_sid = data.get("session_id") or ""
    if not marker_sid or not my_sid or marker_sid != my_sid:
        return

    # Mark fired BEFORE emitting the block, so a torn write still fails safe
    # (worst case: we did not block, never: we block twice).
    marker["stop_nudged"] = True
    _write_marker(path, marker)

    out = {
        "decision": "block",
        "reason": STOP_REASON.format(skill=skill.strip()[:MAX_SKILL_NAME]),
    }
    _block_or_notice(out, cwd)


def main():
    try:
        mode = ""
        for arg in sys.argv[1:]:
            if arg in ("--record", "--inject", "--stop", "--consolidate"):
                mode = arg
        if not mode:
            sys.exit(0)  # no explicit mode -> no-op (wiring-typo guard)
        try:
            data = json.load(sys.stdin)
        except Exception:
            sys.exit(0)  # fail open: bad input -> silent no-op
        if not isinstance(data, dict):
            sys.exit(0)

        if mode == "--record":
            _record(data)
        elif mode == "--stop":
            pass  # Retired answer-end seat; recording/injection remain.
        elif mode == "--inject":
            _inject(data)
        # --consolidate is intentionally silent: skill matter belongs in-flow.
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException:
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)


if __name__ == "__main__":
    main()
