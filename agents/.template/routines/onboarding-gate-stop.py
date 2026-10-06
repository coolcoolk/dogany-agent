#!/usr/bin/env python3
"""Onboarding gate -- UserPromptSubmit prevention with a Stop backstop,
plus a PostToolUse continuation seat (DGN-1632).

DGN-1715 r2: this is also the shipped Stop seat for the dispatch-return
forcing point (lib/dispatch_return_forcing.py): a denied follow-up must end
on a one-line proposal + [[OPTIONS]] marker; see dispatch_return_stop().

DGN-1632: on the Edit/Write/MultiEdit that fills the LAST of the five
identity fields (identity_fields_filled below, the gate's own completeness
predicate), a domain instance off the migration path gets PostToolUse
additionalContext for THIS turn: the result of the existing post-identity
selector (onboarding-check.py select_offer_context -- continuation present
or not) and the required completion-reply SHAPE (one-line recap ->
transition -> the continuation's first question; no capability list while
a continuation exists).  Shape rule only, no owner copy.  Not-last edits,
later edits, other files and main/migration instances stay silent --
except a main whose kit choice is still open (KIT=pending, life kit
reachable): there the kit-select offer is the continuation and the reply
ends on it instead of the GENERAL menu (v2.6.0 rehearsal 2026-10-03; the
Stop handoff check then also rejects a close on the general menu).
v2.6.0 r2 (rehearsal 2026-10-03 21:21): the last field + block removal
landed via a Bash script, which no Edit-family PostToolUse saw.  The
kit-select close is now keyed on the TRANSITION, not the tool: the Stop that
observes pending->cleared (RELEASED, the arm sentinel this turn's
UserPromptSubmit wrote) on a kit-pending main runs completion_kit_check and
blocks a reply that does not end on the kit-select question once.  Bash
calls naming the persona file also reach the PostToolUse seat (earlier
context; the Stop seat is the guarantee).

DGN-1646: pending owner prompts receive the refusal context before generation.
The original Stop predicate below remains a backstop (DGN-1543 R7).

DGN-1703: the Stop backstop no longer BLOCKS a turn whose text already
reached the owner.  A Stop block cannot retract a streamed reply; the
re-prompt always costs a SECOND owner message, and when the first reply was
already the right onboarding question the owner reads it twice (measured
2026-09-25 09:23, a domain agent: the turn read identity/hot.custom.owner.md via Bash,
asked the correct Q3, was blocked, and re-asked it verbatim).  Such a turn is
logged MISS_VISIBLE and leaves a one-shot miss sentinel; the NEXT
UserPromptSubmit injection carries the reminder before generation, where it
costs nothing.  BLOCK survives only for a turn that produced NO owner-visible
text: there is nothing to duplicate, and the owner, who is waiting on the
onboarding question, has received nothing yet -- the re-prompt is then the
only way the question arrives this turn.

DGN-1632 W3 (owner rehearsal 2026-09-26): two narrow text-surface checks.
(1) While onboarding is pending, a Stop whose owner-visible text used a form
of address before the persona's "Call the user" slot is set (honorific
lexicon + the owner-profile name forms; numbered candidate lists excluded),
or told an origin story, is logged MISS_COPY and the next UserPromptSubmit
injection names the slip -- never a block (the text is already on screen).
(2) The PostToolUse continuation seat arms a one-shot handoff check; the
same turn's Stop BLOCKS once (HANDOFF_BLOCK) when the completion reply ends
on an announcement instead of the continuation's first question, or carries
a filler promise.  A text-less regeneration leaves the on-screen reply
standing (DGN-1651), so a legitimate no-offer close costs nothing.

DGN-1837 (owner rehearsal 2026-10-03): kit-pending first-contact opener.
While the Primary focus slot is still the placeholder (no kit yet), the
opening message -- the turn opened by the bridge's first-contact line, or
the first reply of an unnamed agent whose owner spoke first -- may name no
task, example, domain or capability and use no Korean second-person
pronoun or name guess (term list: routines/lib/first_contact_terms.py, the
single source the injected rule also renders); the bridge-opened opener
also asks no question.  A violating opener is BLOCKED once (FC_BLOCK) with
a regenerate instruction; the bridge retracts the superseded live text when
the regeneration arrives (DGN-1651).  A domain instance (slot filled) is
never checked here -- it introduces its role.
DGN-1837 r2 (rehearsal 2026-10-03 14:33: the owner's hello got the SAME
opener back).  The opener is a ONE-TURN thing, keyed on state: evaluating
the opening turn records the reply (routines/lib/first_contact_state.py,
<logs>/.onboarding-opener-sent).  Once recorded, no later turn is an
opening turn (even in a fresh or resumed session), every owner turn's
UserPromptSubmit context carries the identity-setup phase note (greet back
briefly, ask Q1 in that reply), and a Stop reply that repeats the opener --
or re-introduces itself and asks nothing, or (first owner turn after the
opener only) asks no Q1 name question -- is BLOCKED once (FC_REPEAT_BLOCK).

DGN-1849 (dec-243): the fixed onboarding moments are machine-sent.  The
kit-pending opener is sent by the bridge right after /claim (no model turn;
recorded with source "bridge" on the first-contact state, which arm()
keeps), and Q1-Q5 plus the kit offer are screens the onboarding verb
(routines/onboarding-screen.py) sends itself; the bridge mutes the model's
text of a verb turn (bridge/mint_gate.py).  So the Stop seat no longer
judges the wording of those moments: a turn that ran the verb passes
(VERB_TURN), and the one place the model still decides is checked -- an
owner turn that ends WITHOUT the verb while a step is due is BLOCKED once
(VERB_SKIP_BLOCK).  The kit-select close is satisfied by the verb's kit
screen.  The UserPromptSubmit note names the due step and restates the
verb rule (onboarding-check.py SCREEN_RULE, single source).  The opener
and repeat checks above stay for the paths the verb does not cover (a
domain instance's model-composed opener; a missing verb).

WHY THIS EXISTS (measured, 2026-09-17): a freshly minted agent skipped its
first-contact onboarding entirely.  The ONBOARDING_PENDING marker was present
and the instruction text was correct, but the enforcement surface was a
SessionStart additionalContext injection -- ADVICE -- and it lost to the
scrollback of the previous agent that occupied the same chat.  Two holes:
  1. onboarding-check.py fired only on source startup|clear, and the bridge
     RESUMES sessions (sessions.json / _persist_session_id) -- a resumed
     session got ZERO injection (fixed in onboarding-check.py alongside this).
  2. Even when injected, nothing DENIED a non-onboarding first utterance.
This hook is hole 2's fix: the framework principle "Rule unfollowed ->
design a forcing point" (rules/hot.framework.md Principles) applied to the
onboarding rule at the execution decision-point it governs -- the moment the
model tries to END a user-facing turn.

ENFORCEMENT MODEL (Stop block; receiver is the MODEL, not the owner --
same measured delivery contract as output-gate-stop.py: the bridge builds
the Telegram body only from AssistantMessage text blocks and consumes no
hook payload):
  While identity/hot.custom.agent.md still carries <!-- ONBOARDING_PENDING -->
  AND this is an owner-facing session (PROJECT_ROOT env = live bridge,
  usage-gate.py session predicate), a turn may end ONLY IF at least one of:
    (a) the marker is gone (onboarding completed -- the normal exit);
    (b) the machine completion check fires: all five identity fields
        (Name / Emoji / Call the user / Tone / Humor) no longer carry the
        "(set at onboarding" placeholder -> this hook CLEARS the marker
        line ITSELF, logs AUTO_CLEAR, and lets the turn pass.  A forcing
        point with no exit is a trap; the exit may not depend on the model
        remembering the marker-deletion step;
    (c) the CURRENT turn mechanically touched the onboarding surface
        (DGN-1703 widened it to what onboarding actually reads): the
        resolved identity file, identity/hot.custom.owner.md, or any
        dogany-user-onboarding skill file -- via a file tool's path
        (Read/Edit/Write/MultiEdit/NotebookEdit, Grep/Glob path), the
        Skill tool, or a Bash command whose parsed tokens name one of them;
    (d) (DGN-1703) the turn produced owner-visible text -- not a pass on
        the merits, a no-block: logged MISS_VISIBLE, reminded next turn.
  Otherwise the Stop is BLOCKED once (stop_hook_active short-circuits, the
  output-gate-stop.py precedent: one re-prompt per turn, never a loop) and
  the block reason re-delivers the onboarding contract at maximum recency.

WHY IT CANNOT BE TALKED OUT OF: the release act is a FILE TOUCH, not prose.
A model coasting on old-persona scrollback produces a zero-tool greeting
turn -- exactly the measured defect shape -- and that turn is mechanically
denied.  To pass, the model must pull identity/hot.custom.agent.md (whose
top block IS the onboarding script, single source) into its context THIS
turn; the injected-context race that lost to scrollback cannot recur
because the contract arrives as the newest message either way (tool result
or block reason).  Residual advice (stated honestly): the gate verifies
ENGAGEMENT, not prose quality -- a model that reads the contract and still
defies it in wording is beyond any text-surface machine check.

LIMIT: a blocked reply has already streamed to Telegram.  The bridge
retracts the superseded live text when the regeneration arrives (DGN-1651,
armed on the live stream by the Stop re-prompt since DGN-1703), but any
block on an owner-facing reply still spends a regeneration -- hence (d).

Scope guards (all fail toward silence -- this hook must never wedge a turn):
  - Non-bridge sessions (crons, detached workers, plain CLI: no PROJECT_ROOT
    and no interactive DOGANY_SESSION override) pass through untouched.
  - SubagentStop never gates (hook_event_name filter).
  - Kill switch: routines/gate-switch.py slug `onboarding-gate`
    (GATE_STOP_ONBOARDING / master GATE_STOP_ALL); off -> non-blocking
    systemMessage notice instead of the block, event audit-logged.
  - Any error -> exit 0 silently.

Observability: every event appends one line to
<BOT_DATA_DIR>/logs/onboarding-gate.log (same resolution chain as the
output-gate canary log): BLOCK / PASS_TOUCH / MISS_VISIBLE / AUTO_CLEAR /
RELEASED (+ PRE_GENERATION on UserPromptSubmit; CONTINUATION / SLOT_ABSENT
on the PostToolUse identity-completion seat; MISS_COPY / HANDOFF_OK /
HANDOFF_BLOCK / HANDOFF_YIELD for the DGN-1632 W3 checks; FC_OK / FC_BLOCK
/ FC_SENT / FC_SETUP_OK / FC_REPEAT_BLOCK for the DGN-1837 opener checks).
RELEASED fires once on the pending->cleared transition (arm sentinel file
next to the log), so the marker's clearing is observable no matter who
cleared it (model self-edit or the AUTO_CLEAR path).

stdin(JSON): {session_id, transcript_path, cwd, stop_hook_active, ...}
stdout: UserPromptSubmit additionalContext before generation;
{"decision": "block", "reason": ...} on a denied Stop.
"""
import json
import os
import sys
import time

MARKER = "<!-- ONBOARDING_PENDING -->"
_GATE_SLUG = "onboarding-gate"
_DR_SLUG = "dispatch-return-result-first"
_LOG_FILENAME = "onboarding-gate.log"
_ARM_FILENAME = ".onboarding-gate-armed"
# DGN-1703: one-shot "the last owner turn ended without onboarding
# engagement" flag, written by the Stop seat instead of a block and consumed
# by the next UserPromptSubmit injection.
_MISS_FILENAME = ".onboarding-gate-miss"
# DGN-1632 W3: one-shot copy-rule miss (address term before Q3 / origin
# story), JSON {"kinds": [...], "terms": [...]}, consumed like the miss above.
_COPY_MISS_FILENAME = ".onboarding-gate-copy-miss"
# DGN-1632 W3: armed by the PostToolUse continuation seat when a continuation
# exists; the SAME turn's Stop checks the reply ends on the continuation's
# first question (never on an announcement) and carries no filler promise.
_HANDOFF_DUE_FILENAME = ".onboarding-handoff-due"

# DGN-1703: the onboarding surface a turn may touch to count as engaged.
OWNER_BASENAME = "hot.custom.owner.md"
LEGACY_OWNER_BASENAME = "USER.md"
SKILL_NAME = "dogany-user-onboarding"
_PATH_KEYS = ("file_path", "notebook_path", "path")

# DGN-1606: harness-owned injected-turn mark.  bridge/sdk_bridge.py
# inject_background_turn opens every machine-injected turn (session-inbox /
# cron-inject) with this line; a turn whose boundary message starts with it
# had NO owner utterance.  This gate abstains in such turns (there is no
# owner to onboard), and routines/onboarding-owner-gate.py denies identity
# writes in them.  Lockstep literal with sdk_bridge.py INJECTED_TURN_MARK --
# change both together.
INJECTED_TURN_MARK = "[bridge:injected-turn]"
# DGN-1837: the bridge opens the post-/claim first turn with this line (after
# the injected-turn mark).  Lockstep literal with bridge/bot.py
# FIRST_CONTACT_TURN -- change both together.
FIRST_CONTACT_MARK = "[bridge:first-contact]"
# Belt for the one-update version-skew window (an old bridge injecting
# unmarked spools while the new routines are already live): the harness-
# written self-record spool prefixes (push.sh _record_outbound) also read
# as machine-opened.  A restart-verify spool has no stable prefix -- the
# mark above is the real signal; this tuple is best-effort only.
_INJECTED_BELT_PREFIXES = ("[outbound-record]", "[operator-alert]")

# The five always-asked identity fields (Q1..Q5).  Primary focus (Q6) is
# deliberately EXCLUDED: it is conditional (DGN-227 A3) and stays a
# placeholder forever on a stamped domain mint.
_IDENTITY_FIELD_PREFIXES = (
    "- Name:",
    "- Emoji:",
    "- Call the user",
    "- Tone:",
    "- Humor:",
)
_PLACEHOLDER = "(set at onboarding"
_NAME_PREFIX = "- Name:"
_PRIMARY_FOCUS_PREFIX = "- Primary focus:"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
try:
    import dispatch_return_forcing as _forcing
except Exception:
    _forcing = None
try:
    import first_contact_terms as _fc_terms
except Exception:
    _fc_terms = None
try:
    import first_contact_state as _fc_state
except Exception:
    _fc_state = None

# Model-facing block reason (English per RULES Code=English/ASCII; this is
# hook feedback, never owner copy).  The identity file stays the single
# source of the question script -- this reason points at it, it does not
# fork the wording.
BLOCK_REASON = (
    "Onboarding gate (Stop block, DGN-1543 R7): this instance's identity "
    "file still carries the ONBOARDING_PENDING marker, and this turn showed "
    "no onboarding engagement (no Read/Edit of the identity file, no "
    "dogany-user-onboarding skill use). Whatever persona the chat scrollback "
    "suggests, you are an UNCONFIGURED new agent -- first-contact onboarding "
    "is not optional and precedes every other reply. Regenerate now: "
    "(1) Read identity/hot.custom.agent.md and follow the FIRST-CONTACT "
    "ONBOARDING block at its top (the single source of the question script) "
    "plus the dogany-user-onboarding skill. (2) In the regenerated reply: "
    "use NO form of address until the user sets one, do NOT self-name, "
    "first contact = a short self-introduction that invites the user to "
    "say hello back, with no question yet and no origin story (never say "
    "who created, minted or sent you; 'newly born' is fine) -- that opener is "
    "sent ONCE; once the user has answered, never repeat it: greet back "
    "briefly and lead into the Q1 name ask in that reply, then ONE "
    "question at a time (Q2 emoji -> Q3 address term "
    "-> Q4 tone -> Q5 humor), and never narrate internal state, file names, "
    "or markers to the user. (3) This gate releases when your turn touches "
    "the identity or owner file (a Read counts) or the onboarding skill, or "
    "onboarding completes; once all "
    "five identity fields are filled the machinery clears the marker itself."
)

# DGN-1703: prepended to the next prompt's injection after a MISS_VISIBLE.
MISS_REMINDER = (
    "Onboarding gate (DGN-1703): your PREVIOUS reply ended without any "
    "onboarding engagement while onboarding is still pending. Do not "
    "apologize for it or repeat an earlier question; continue the onboarding "
    "from where the conversation actually stands."
)


# ---------------------------------------------------------------------------
# Sibling module loading (dash filenames -> importlib, the output-gate-stop
# _load_gate_switch idiom).  onboarding-check.py owns the identity-file
# resolver (dual-accept + DGN-1141 relayout promise); loading it keeps this
# gate on the SAME resolution the SessionStart signal uses -- never fork it.
# ---------------------------------------------------------------------------

def _load_sibling(filename, modname):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            modname, os.path.join(SCRIPT_DIR, filename))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def session_is_interactive():
    """True iff this is the owner-driven live bridge session.  Same predicate
    as routines/usage-gate.py session_is_interactive (DGN-925): explicit
    DOGANY_SESSION override wins, else PROJECT_ROOT presence (bridge-only
    env, set in bridge/sdk_bridge.py).  Fail-safe here is the INVERSE of the
    usage gate's: unknown -> NOT interactive -> this gate stays silent, so a
    cron/headless runner on a pending instance is never wedged."""
    override = os.environ.get("DOGANY_SESSION", "").strip().lower()
    if override in ("interactive", "owner", "live"):
        return True
    if override in ("auto", "autonomous", "cron", "headless"):
        return False
    return bool(os.environ.get("PROJECT_ROOT"))


def resolve_identity(data):
    """(path_or_None, marker_present).  Resolution rides onboarding-check.py
    (ONBOARDING_FILE env honored, dual-accept fallback chain).  Loader
    failure -> (None, False): the gate disarms rather than guessing a path
    the SessionStart signal would not have used."""
    oc = _load_sibling("onboarding-check.py", "onboarding_check_for_gate")
    if oc is None:
        return None, False
    try:
        path, _collision = oc.resolve_target(data)
    except Exception:
        return None, False
    if not path or not os.path.isfile(path):
        # Missing identity file reads as onboarding-needed in the
        # SessionStart signal, but this gate cannot demand a touch of a file
        # that does not exist -- treat as disarmed (mint repair territory).
        return path, False
    try:
        with open(path, encoding="utf-8") as f:
            return path, MARKER in f.read()
    except Exception:
        return path, False


# ---------------------------------------------------------------------------
# Machine completion check + marker auto-clear (the gate's guaranteed exit)
# ---------------------------------------------------------------------------

def identity_fields_filled(path):
    """True iff none of the five always-asked identity field lines still
    carries the "(set at onboarding" placeholder.  Lines are matched by
    their template prefixes; a file with no such lines at all reads as
    filled (nothing left to ask)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_IDENTITY_FIELD_PREFIXES) and _PLACEHOLDER in line:
            return False
    return True


def clear_marker(path):
    """Remove the ONBOARDING_PENDING marker from the identity file (atomic
    tmp+replace).  Whole marker-only lines are dropped; an inline occurrence
    (never the shipped shape) falls back to substring removal.  Returns True
    on a verified clear."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        lines = text.splitlines(keepends=True)
        kept = [ln for ln in lines if ln.strip() != MARKER]
        new_text = "".join(kept)
        if MARKER in new_text:
            new_text = new_text.replace(MARKER, "")
        if new_text == text:
            return False
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Current-turn tool touch (transcript scan -- the output-gate-stop.py
# boundary idiom: current turn = entries after the last GENUINE user prompt)
# ---------------------------------------------------------------------------

def _is_genuine_user_boundary(etype, is_injected, msg):
    if etype != "user" or is_injected:
        return False
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return True
    if isinstance(content, list):
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text" for b in content)
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content)
        if has_text and not has_tool_result:
            return True
    return False


def surface_basenames(identity_path):
    """Basenames that count as the identity/owner half of the onboarding
    surface.  Unresolved identity path -> every accepted persona name."""
    names = {OWNER_BASENAME}
    if identity_path:
        base = os.path.basename(identity_path)
        names.add(base)
        if base != "hot.custom.agent.md":
            names.add(LEGACY_OWNER_BASENAME)  # pre-relayout flat root
    else:
        names.update(("hot.custom.agent.md", "PROFILE.md", "AGENT.md",
                      LEGACY_OWNER_BASENAME))
    return names


def path_on_surface(value, basenames):
    """True iff a path-like string names an onboarding-surface file: an
    identity/owner basename, or anything under the onboarding skill."""
    if not isinstance(value, str) or not value.strip():
        return False
    norm = value.strip().rstrip("/")
    if SKILL_NAME in norm.split("/"):
        return True
    return os.path.basename(norm) in basenames


def bash_tokens(command):
    """Shell-parse a Bash command into words, splitting on ; | & ( ) < >.
    Unbalanced quoting falls back to whitespace split -- never raises."""
    try:
        import shlex
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except Exception:
        return command.split()


def tool_touches_surface(name, tool_input, basenames):
    """DGN-1703 typed touch predicate for one tool_use block."""
    if not isinstance(tool_input, dict):
        return False
    if name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return False
        return any(path_on_surface(tok, basenames)
                   for tok in bash_tokens(command))
    if name == "Skill":
        return SKILL_NAME in str(tool_input.get("skill", ""))
    return any(path_on_surface(tool_input.get(k), basenames)
               for k in _PATH_KEYS)


def scan_turn(transcript_path, basenames):
    """(touched, visible_text) for the CURRENT turn.

    touched: some main-agent tool_use touched the onboarding surface
    (tool_touches_surface).  visible_text: some main-agent text block with
    non-whitespace content -- the bridge has already streamed it to the
    owner.  Fail-open to (True, True) on any read/parse trouble or a missing
    turn boundary -- an unreadable transcript must never block."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return True, True
    try:
        entries = []
        with open(transcript_path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype not in ("assistant", "user"):
                    continue
                is_injected = bool(
                    entry.get("isMeta") or entry.get("isSidechain"))
                entries.append((etype, is_injected, entry.get("message") or {}))
    except Exception:
        return True, True
    if not entries:
        return True, True
    boundary = None
    for idx in range(len(entries) - 1, -1, -1):
        etype, is_injected, msg = entries[idx]
        if _is_genuine_user_boundary(etype, is_injected, msg):
            boundary = idx
            break
    if boundary is None:
        return True, True
    touched = False
    visible = False
    for etype, is_injected, msg in entries[boundary + 1:]:
        if etype != "assistant" or is_injected:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            visible = visible or bool(content.strip())
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                if str(block.get("text") or "").strip():
                    visible = True
            elif block.get("type") == "tool_use":
                try:
                    if tool_touches_surface(str(block.get("name", "")),
                                            block.get("input"), basenames):
                        touched = True
                except Exception:
                    continue
    return touched, visible


def current_boundary_text(transcript_path):
    """Text of the CURRENT turn's boundary message (the last genuine user
    prompt), or None when it cannot be determined (missing/unreadable
    transcript, no boundary).  Same entry parsing as scan_turn."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    try:
        boundary_msg = None
        with open(transcript_path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if entry.get("type") != "user":
                    continue
                is_injected = bool(
                    entry.get("isMeta") or entry.get("isSidechain"))
                msg = entry.get("message") or {}
                if _is_genuine_user_boundary("user", is_injected, msg):
                    boundary_msg = msg
    except Exception:
        return None
    if boundary_msg is None:
        return None
    content = boundary_msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text")
    return None


def boundary_is_injected(transcript_path):
    """True iff the current turn was POSITIVELY identified as machine-opened:
    its boundary message starts with the harness injected-turn mark (or a
    belt spool prefix).  Any doubt -> False, so existing behavior holds."""
    text = current_boundary_text(transcript_path)
    if not text:
        return False
    head = text.lstrip()
    return head.startswith((INJECTED_TURN_MARK,) + _INJECTED_BELT_PREFIXES)


# ---------------------------------------------------------------------------
# Observability (log + one-shot release sentinel next to the log)
# ---------------------------------------------------------------------------

def _resolve_log_dir(cwd):
    """BOT_DATA_DIR from config/agent.conf + /logs, else .telegram_bot/logs
    under cwd -- the output-gate canary log's resolution chain."""
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
    try:
        from conf_reader import conf_get
    except Exception:
        def conf_get(key, conf_path=None):
            return ""
    try:
        data_dir = conf_get(
            "BOT_DATA_DIR", os.path.join(cwd, "config", "agent.conf"))
    except Exception:
        data_dir = ""
    if data_dir:
        data_dir = os.path.expanduser(data_dir)
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(cwd, data_dir)
    else:
        data_dir = os.path.join(cwd, ".telegram_bot")
    return os.path.join(data_dir, "logs")


def _log(cwd, event, detail=""):
    try:
        log_dir = _resolve_log_dir(cwd)
        os.makedirs(log_dir, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(os.path.join(log_dir, _LOG_FILENAME), "a",
                  encoding="utf-8") as f:
            f.write("%s  %s  %s\n" % (stamp, event, detail))
    except Exception:
        pass


def _arm_path(cwd):
    return os.path.join(_resolve_log_dir(cwd), _ARM_FILENAME)


def arm(cwd):
    """Record that the gate observed a pending state (write-if-absent), so
    the later pending->cleared transition can be logged exactly once."""
    try:
        path = _arm_path(cwd)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
            # A new onboarding cycle owes a fresh continuation (DGN-1632).
            sent = os.path.join(os.path.dirname(path),
                                _CONTINUATION_SENT_FILENAME)
            if os.path.exists(sent):
                os.remove(sent)
            # ... and a fresh first-contact opener (DGN-1837 r2) -- except
            # the bridge's machine-sent opener (DGN-1849): it is written at
            # /claim while onboarding is pending, so it opens THIS cycle.
            if _fc_state is not None and not _fc_state.opener_from_bridge(
                    _fc_state.read_opener(os.path.dirname(path))):
                _fc_state.clear_opener(os.path.dirname(path))
    except Exception:
        pass


def _miss_path(cwd):
    return os.path.join(_resolve_log_dir(cwd), _MISS_FILENAME)


def record_miss(cwd):
    try:
        path = _miss_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
    except Exception:
        pass


def consume_miss(cwd):
    """True iff a miss was recorded (and is now cleared)."""
    try:
        path = _miss_path(cwd)
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def release_if_armed(cwd, detail):
    consume_miss(cwd)  # a completed onboarding owes no reminder
    consume_copy_miss(cwd)
    if _fc_state is not None:
        _fc_state.clear_opener(_resolve_log_dir(cwd))
    try:
        path = _arm_path(cwd)
        if os.path.exists(path):
            os.remove(path)
            _log(cwd, "RELEASED", detail)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# DGN-1632 W3: owner-facing copy checks on the Stop seat (rehearsal
# 2026-09-26 items B5/B6/C12/C13).  Text-surface checks, deliberately narrow:
# they catch the measured defect shapes, not prose quality in general.
# ---------------------------------------------------------------------------

# Honorific address words a newborn has no right to use before Q3 (the
# rehearsal broke on the first one).  Match corpus for model output, escaped
# to keep this file ASCII.
_ADDRESS_LEXICON = (
    "\ud615\ub2d8",          # hyeong-nim
    "\ub204\ub2d8",          # nu-nim
    "\uc0ac\uc7a5\ub2d8",    # sajang-nim
    "\ub300\ud45c\ub2d8",    # daepyo-nim
    "\ud68c\uc6d0\ub2d8",    # hoewon-nim
    "\uace0\uac1d\ub2d8",    # gogaek-nim
)
# Origin-story shapes.  DGN-1837 r2 (owner copy 2026-10-03 14:34): saying
# the agent is newly born is ALLOWED (the kit-pending opener says it); what
# stays banned is the creator/source story -- who created, minted or sent it.
# So a "born" stem counts only together with a source cue; minting language
# counts on its own.
_ORIGIN_LEXICON = ("\ud0dc\uc5b4\ub0ac", "\ud0dc\uc5b4\ub09c", "\ud0dc\uc5b4\ub098")
_ORIGIN_SOURCE_CUES = (
    "\uc5d0\uac8c\uc11c",            # egeseo (from someone)
    "\ub85c\ubd80\ud130",            # robuteo (from)
    "\uc5d0\uc11c \ud0dc\uc5b4",      # eseo taeeo (born at/from)
)
_ORIGIN_MAKER_STEMS = ("\ubbfc\ud305",)    # minting
_ORIGIN_RE_EN = (r"\b(?:born|created|minted|spawned)\s+(?:by|from)\b"
                 r"|\b(?:was|been|got)\s+(?:just\s+)?minted\b")
# Filler promises in a handoff ("I'll look after it" shapes).
_FILLER_LEXICON = (
    "\ucc59\uaca8 \ubcfc\uac8c", "\ucc59\uaca8\ubcfc\uac8c",
    "\ucc59\uaca8 \ub4dc\ub9b4\uac8c", "\ucc59\uaca8\ub4dc\ub9b4\uac8c",
)
_OPTIONS_MARKER = "[[OPTIONS]]"
_CALL_PREFIX = "- Call the user"

# DGN-1715 r3: a bare-marker literal comparison here missed the labeled shape
# "[[OPTIONS: a | b]]" (DGN-992), same class of bug as the Stop-side forcing
# fix in lib/dispatch_return_forcing.py. Both call sites below (_option_line,
# reply_ends_on_question) now recognize either shape via that module's shared
# predicate; _forcing is None only in the (untested) case its import failed,
# in which case we fall back to the old bare-only comparison rather than
# crash.


def _is_options_marker_line(line):
    if _forcing is not None:
        return _forcing.is_options_marker_line(line)
    return line.strip() == _OPTIONS_MARKER

HANDOFF_BLOCK_REASON = (
    "Onboarding handoff (DGN-1632 W3, Stop block): your identity-completion "
    "reply %s. Regenerate the COMPLETE reply now (it replaces the one on "
    "screen): the itemized settings echo, then ONE transition sentence naming "
    "the concrete next step and why it comes now, then the continuation's "
    "first question as the LAST thing in the message (with its answer options "
    "when the continuation gives them). If the continuation's own check said "
    "stop or no offer, output nothing more -- the reply on screen stands."
)

COPY_MISS_REMINDER = {
    "address": ("your PREVIOUS reply addressed the user with a form of address "
                "(%s) before they answered the address-term question (Q3). "
                "Until Q3 is answered use NO form of address at all -- a "
                "neutral second person only."),
    "origin": ("your PREVIOUS reply told an origin story (who or what created, "
               "minted or sent you). Never do that (saying you are newly born "
               "is fine); greet, then ask."),
}


def _option_line(line):
    import re
    st = line.strip()
    return (not st or _is_options_marker_line(line)
            or re.match(r"^\d+[.)]\s+\S", st) is not None)


def prose_lines(text):
    """Text lines minus numbered option items and the options marker: a
    candidate LIST is not addressing the user (Q3 lists its candidates)."""
    return [ln for ln in (text or "").splitlines() if not _option_line(ln)]


def address_unset(identity_path):
    try:
        with open(identity_path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.lstrip().startswith(_CALL_PREFIX):
                    return _PLACEHOLDER in line
    except Exception:
        return False
    return False


def owner_address_terms(identity_path):
    """Owner-name address forms from the profile (full name, the Q3
    candidate '<given>nim' and its spaced variant).  Name read + candidate
    format come from the DGN-1719 single source (routines/lib/q3_candidates.py)
    -- never re-derived here.  Fail-open -> ()."""
    try:
        sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
        import q3_candidates as q3
        ident_dir = os.path.dirname(os.path.abspath(identity_path))
        root = (os.path.dirname(ident_dir)
                if os.path.basename(ident_dir) == "identity" else ident_dir)
        name = q3.owner_name(root)
        if not name:
            return ()
        terms = {name}
        cand = q3.owner_name_candidate(name, "ko")
        if cand:
            terms.add(cand)
            terms.add(cand[:-1].rstrip() + " " + cand[-1])
        return tuple(sorted(t for t in terms if len(t) >= 2))
    except Exception:
        return ()


def copy_violations(data, identity_path, text):
    """{"kinds": [...], "terms": [...]} for the pending-onboarding copy
    rules this turn's owner-visible text broke; None when clean."""
    import re
    body = "\n".join(prose_lines(text))
    if not body.strip():
        return None
    kinds, terms = [], []
    if identity_path and address_unset(identity_path):
        for term in _ADDRESS_LEXICON + owner_address_terms(identity_path):
            if term in body:
                terms.append(term)
        if terms:
            kinds.append("address")
    born = any(stem in body for stem in _ORIGIN_LEXICON)
    if (born and any(cue in body for cue in _ORIGIN_SOURCE_CUES)) or \
            any(stem in body for stem in _ORIGIN_MAKER_STEMS) or \
            re.search(_ORIGIN_RE_EN, body, re.IGNORECASE):
        kinds.append("origin")
    if not kinds:
        return None
    return {"kinds": kinds, "terms": terms}


def _copy_miss_path(cwd):
    return os.path.join(_resolve_log_dir(cwd), _COPY_MISS_FILENAME)


def record_copy_miss(cwd, violation):
    try:
        path = _copy_miss_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(violation, f, ensure_ascii=False)
    except Exception:
        pass


def consume_copy_miss(cwd):
    """The recorded copy violation (and clear it), or None."""
    try:
        path = _copy_miss_path(cwd)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        finally:
            os.remove(path)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def copy_miss_reminder(rec):
    parts = []
    for kind in rec.get("kinds") or ():
        tmpl = COPY_MISS_REMINDER.get(kind)
        if not tmpl:
            continue
        if kind == "address":
            tmpl = tmpl % (", ".join("'%s'" % t for t in rec.get("terms") or ())
                           or "a title")
        parts.append(tmpl)
    if not parts:
        return ""
    return ("Onboarding gate (DGN-1632 W3): " + " Also, ".join(parts)
            + " Do not apologize for it or mention this notice.")


def turn_text(transcript_path):
    """All owner-visible assistant text of the CURRENT turn (the scan_turn
    boundary rule), or "" when undeterminable."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    entries = []
    try:
        with open(transcript_path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype not in ("assistant", "user"):
                    continue
                entries.append((etype, bool(entry.get("isMeta") or
                                            entry.get("isSidechain")),
                                entry.get("message") or {}))
    except Exception:
        return ""
    boundary = None
    for idx in range(len(entries) - 1, -1, -1):
        if _is_genuine_user_boundary(*entries[idx]):
            boundary = idx
            break
    if boundary is None:
        return ""
    texts = []
    for etype, injected, msg in entries[boundary + 1:]:
        if etype != "assistant" or injected:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return "\n".join(t for t in texts if t.strip())


def final_turn_text(data):
    """Every owner-visible text of the current turn (last message fallback)."""
    text = turn_text(data.get("transcript_path") or "")
    last = data.get("last_assistant_message")
    if isinstance(last, str) and last.strip() and last not in text:
        text = (text + "\n" + last) if text else last
    return text


def final_reply_text(data):
    last = data.get("last_assistant_message")
    if isinstance(last, str) and last.strip():
        return last
    return turn_text(data.get("transcript_path") or "")


def reply_ends_on_question(text):
    """True iff the reply hands the turn to the user: its last prose line
    (after any numbered options and the options marker) asks a question, or
    the reply ends with an options marker (buttons await an answer)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if lines and _is_options_marker_line(lines[-1]):
        return True
    while lines and _option_line(lines[-1]):
        lines.pop()
    if not lines:
        return False
    return "?" in lines[-1] or "\uff1f" in lines[-1]


def handoff_problem(text):
    """None when the handoff reply is well-formed, else the reason clause."""
    if any(f in (text or "") for f in _FILLER_LEXICON):
        return ("used a vague care-taking promise instead of naming the "
                "concrete next step and why")
    if not reply_ends_on_question(text):
        return ("ended on an announcement of what comes next instead of "
                "the continuation's first question")
    return None


def _handoff_due_path(cwd):
    return os.path.join(_resolve_log_dir(cwd), _HANDOFF_DUE_FILENAME)


def arm_handoff_check(cwd, kind=""):
    """kind 'kit': the continuation is the kit-select offer (main, KIT=pending)."""
    try:
        path = _handoff_due_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
            if kind:
                f.write("kind=%s\n" % kind)
    except Exception:
        pass


def _handoff_kind(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("kind="):
                    return line.strip()[5:]
    except Exception:
        pass
    return ""


# The GENERAL menu's fixed first item (ONBOARDING_CTX / persona template,
# ko + en): the measured defect shape when the kit-select close is due.
_GENERAL_MENU_ITEMS = (
    "\uc81c\uac00 \ubb58 \ud574\ub4dc\ub9b4 \uc218 \uc788\ub294\uc9c0",  # jega mwol haedeuril su inneunji
    "see what i can do",
)


def kit_close_problem(text):
    """None when a kit-pending completion reply ends on the kit-select
    question, else the reason clause.  Shape test, no wording: the handoff
    rules, no GENERAL menu item, and the options marker as the last line."""
    problem = handoff_problem(text)
    if problem is not None:
        return problem
    if any(m in (text or "").lower() for m in _GENERAL_MENU_ITEMS):
        return ("closed with the general action menu instead of the "
                "kit-select question (two numbered options from 'kit.offer', "
                "[[OPTIONS]] last; no action menu)")
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not (lines and _is_options_marker_line(lines[-1])):
        return ("did not end on the kit-select question's two numbered "
                "options with the [[OPTIONS]] marker alone on the last line")
    return None


def handoff_check(data, cwd):
    """Stop seat for the continuation handoff.  Returns True iff it emitted
    a block (the caller then ends the hook)."""
    path = _handoff_due_path(cwd)
    if not os.path.exists(path):
        return False
    if data.get("stop_hook_active"):
        try:
            os.remove(path)
        except Exception:
            pass
        _log(cwd, "HANDOFF_YIELD", "stop_hook_active -- already re-prompted once")
        return False
    kind = _handoff_kind(path)
    if kind == "kit" and kit_screen_sent(data):
        try:
            os.remove(path)
        except Exception:
            pass
        _log(cwd, "HANDOFF_OK", "machine-sent kit screen closes the completion")
        return False
    text = final_reply_text(data)
    if not text.strip():
        return False  # nothing delivered yet; keep the check armed
    try:
        os.remove(path)
    except Exception:
        pass
    problem = kit_close_problem(text) if kind == "kit" else handoff_problem(text)
    if problem is None:
        _log(cwd, "HANDOFF_OK", "completion reply ends on the continuation question")
        return False
    blocked = emit_block(cwd, HANDOFF_BLOCK_REASON % problem)
    _log(cwd, "HANDOFF_BLOCK" if blocked else "HANDOFF_OFF_SUPPRESSED", problem)
    return blocked


# ---------------------------------------------------------------------------
# DGN-1837: kit-pending first-contact opener (Stop block once).  Rules only,
# no owner copy: the reason names what to drop, the model words the opener.
# ---------------------------------------------------------------------------

FIRST_CONTACT_BLOCK_REASON = (
    "First-contact gate (DGN-1837, Stop block): this instance has no kit yet "
    "(the Primary focus slot is still the placeholder), and your opening "
    "message %s. Regenerate the COMPLETE opening message now (it replaces "
    "the one on screen): at most two short sentences -- who you are (a "
    "personal agent, newly born) and a light invitation to say hello. Name "
    "NO owner: no second-person possessive, and no substitute possessive or "
    "place word (such as 'this place's' or 'here's') in front of 'personal "
    "agent'. Name NO task, example, domain or capability (no list of what you "
    "can help with); use no second-person pronoun and no name or title for "
    "the user; ask no question. Compose the wording yourself in the instance "
    "language; never mention this notice."
)

# DGN-1837 r2: the opener was already sent and this reply re-opened.
FIRST_CONTACT_REPEAT_REASON = (
    "First-contact gate (DGN-1837, Stop block): your opening message was "
    "already sent earlier in this chat, and this reply %s. The opener is "
    "sent ONCE; this turn is identity setup. Regenerate the COMPLETE reply "
    "now (it replaces the one on screen): do not introduce yourself again "
    "and do not reuse the opener's wording; greet back briefly or answer "
    "what the user said, then ask Q1 (the name the user wants to give you) "
    "in this same reply. Still no self-name and no form of address. Compose "
    "the wording yourself in the instance language; never mention this "
    "notice."
)

# DGN-1837 r2: UserPromptSubmit phase note once the opener is on record.
# DGN-1849: Q1 is a machine-sent screen; the opener may have been sent by
# the bridge (not in the transcript).
IDENTITY_SETUP_NOTE = (
    "First-contact phase (DGN-1837): the opening message was ALREADY SENT "
    "(on a kit-pending main the machine sent it right after the claim) -- "
    "this turn is identity setup, not first contact. The opener rule "
    "applied to that one opening message only and is over: do not "
    "introduce yourself again or repeat the opener. Send the Q1 screen with "
    "the onboarding verb; a short greeting back may ride its --ack."
)

# DGN-1849: the due-step line of the UserPromptSubmit note.
SCREEN_DUE_NOTE = (
    "Onboarding screens (DGN-1849): the open step right now is %s. If this "
    "message answers it, record the answer first; then send the next due "
    "step with the onboarding verb (when this message does not answer it, "
    "re-send %s with your reply in --ack) and end the turn with no text."
)

VERB_SKIP_REASON = (
    "Onboarding screens (DGN-1849, Stop block): onboarding step %s is due, "
    "and this turn ended without the onboarding verb. The fixed onboarding "
    "questions are sent by the machine, never written by you. Now: record "
    "the owner's answer into the identity file if this message answered the "
    "open question (silently), then run `%s <due step> [--ack \"<short "
    "reply>\"]` (q2 also takes 3-4 --emoji arguments) and end the turn with "
    "NO text of your own -- the machine sends your --ack and the screen. "
    "Never mention this notice."
)


def _slot_unset(identity_path, prefix):
    """True iff the identity line starting with prefix still carries the
    placeholder.  Missing line or unreadable file -> False (fail toward
    silence)."""
    try:
        with open(identity_path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.lstrip().startswith(prefix):
                    return _PLACEHOLDER in line
    except Exception:
        return False
    return False


def kit_pending_role(identity_path):
    """The Primary-focus placeholder test (the single discriminator shared
    with the persona block, the onboarding skill and onboarding-check.py):
    still the placeholder -> no kit has given this agent a role."""
    return bool(identity_path) and _slot_unset(identity_path,
                                               _PRIMARY_FOCUS_PREFIX)


def boundary_is_first_contact(transcript_path):
    """True iff the current turn was opened by the bridge's first-contact
    line (the injected-turn mark, when present, comes first)."""
    text = current_boundary_text(transcript_path)
    if not text:
        return False
    head = text.lstrip()
    if head.startswith(INJECTED_TURN_MARK):
        head = head[len(INJECTED_TURN_MARK):].lstrip()
    return head.startswith(FIRST_CONTACT_MARK)


def first_reply_of_session(transcript_path):
    """True iff no assistant text precedes the current turn's boundary (the
    owner spoke first and this is the agent's first reply).  Any doubt ->
    False."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return False
    entries = []
    try:
        with open(transcript_path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype in ("assistant", "user"):
                    entries.append((etype, bool(entry.get("isMeta") or
                                                entry.get("isSidechain")),
                                    entry.get("message") or {}))
    except Exception:
        return False
    boundary = None
    for idx in range(len(entries) - 1, -1, -1):
        if _is_genuine_user_boundary(*entries[idx]):
            boundary = idx
            break
    if boundary is None:
        return False
    for etype, injected, msg in entries[:boundary]:
        if etype != "assistant" or injected:
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return False
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "text"
                and str(b.get("text") or "").strip() for b in content):
            return False
    return True


def opener_problem(text, identity_path, bridge_opened):
    """None when the kit-pending opener is clean, else the reason clause."""
    if _fc_terms is None:
        return None
    found = _fc_terms.opener_violations(
        text, owner_address_terms(identity_path) if identity_path else ())
    parts = []
    if found and found["terms"]:
        parts.append("named tasks/domains/capabilities (%s)" % ", ".join(
            "'%s'" % t for t in found["terms"]))
    possessives = getattr(_fc_terms, "KO_SUBSTITUTE_POSSESSIVE", ())
    pronouns = [t for t in (found or {}).get("pronouns", ())
                if t not in possessives]
    owned = [t for t in (found or {}).get("pronouns", ()) if t in possessives]
    if pronouns:
        parts.append("addressed the user (%s)" % ", ".join(
            "'%s'" % t for t in pronouns))
    if owned:
        parts.append("named an owner for yourself (%s)" % ", ".join(
            "'%s'" % t for t in owned))
    if bridge_opened and ("?" in text or "\uff1f" in text):
        parts.append("asked a question")
    return " and ".join(parts) or None


def opener_on_record(cwd):
    """The recorded first-contact opener (dict), or None when not sent yet
    this onboarding cycle (or the state module is missing)."""
    if _fc_state is None:
        return None
    try:
        return _fc_state.read_opener(_resolve_log_dir(cwd))
    except Exception:
        return None


def is_opening_turn(data, cwd, identity_path, sent=None):
    """The opening turn of first contact: the bridge's first-contact turn,
    or -- no opener on record yet -- the first reply of an unnamed agent
    whose owner spoke first.  Once an opener is on record no other turn is
    the opening one, whatever the transcript shows (a fresh / resumed
    session has no memory of it)."""
    transcript = data.get("transcript_path") or ""
    if boundary_is_first_contact(transcript):
        return True
    if sent is not None:
        return False
    return bool(identity_path) and _slot_unset(identity_path, _NAME_PREFIX) \
        and first_reply_of_session(transcript)


def record_opening_reply(data, cwd):
    """Put the opening reply on record (DGN-1837 r2), keyed to the turn's
    boundary so a regeneration of the same turn refreshes it."""
    if _fc_state is None:
        return
    try:
        boundary = _fc_state.boundary_key(
            current_boundary_text(data.get("transcript_path") or "") or "")
        reply = final_reply_text(data)
        # Owner spoke first and the greeting already led into Q1 -> no Q1
        # is owed by the next turn.
        if _fc_state.record_opener(_resolve_log_dir(cwd), reply, boundary,
                                   q1_due=not _fc_state.asks_name(reply)):
            _log(cwd, "FC_SENT", "first-contact opener on record")
    except Exception:
        pass


def refresh_regenerated_opener(data, cwd):
    """stop_hook_active seat: a regenerated opening reply replaces the
    blocked one on record (same boundary)."""
    if _fc_state is None:
        return
    try:
        sent = _fc_state.read_opener(_resolve_log_dir(cwd))
        if not sent:
            return
        boundary = _fc_state.boundary_key(
            current_boundary_text(data.get("transcript_path") or "") or "")
        if sent.get("boundary") == boundary and final_reply_text(data).strip():
            _fc_state.record_opener(_resolve_log_dir(cwd), final_reply_text(data),
                                    boundary)
    except Exception:
        pass


_SCREEN_VERB_NAME = "onboarding-screen.py"


def _load_screen_verb():
    return _load_sibling(_SCREEN_VERB_NAME, "onboarding_screen_for_gate")


def _json_result(content):
    """The verb's JSON result object from a tool_result content, or None."""
    if isinstance(content, list):
        content = "\n".join(str(b.get("text") or "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    found = None
    for cand in [content.strip()] + [ln.strip() for ln in content.splitlines()]:
        if not cand.startswith("{"):
            continue
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and "note_for_agent" in obj and "step" in obj:
            found = obj
    return found


def verb_results(transcript_path):
    """Results of the onboarding verb calls of the CURRENT turn (the
    scan_turn boundary rule), oldest first; [] when none or undeterminable."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return []
    entries = []
    try:
        with open(transcript_path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype in ("assistant", "user"):
                    entries.append((etype, bool(entry.get("isMeta") or
                                                entry.get("isSidechain")),
                                    entry.get("message") or {}))
    except Exception:
        return []
    boundary = None
    for idx in range(len(entries) - 1, -1, -1):
        if _is_genuine_user_boundary(*entries[idx]):
            boundary = idx
            break
    if boundary is None:
        return []
    calls, out = set(), []
    for etype, injected, msg in entries[boundary + 1:]:
        content = msg.get("content")
        if injected or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if etype == "assistant" and block.get("type") == "tool_use" \
                    and block.get("name") == "Bash":
                command = (block.get("input") or {}).get("command")
                if isinstance(command, str) and _SCREEN_VERB_NAME in command:
                    calls.add(block.get("id"))
            elif etype == "user" and block.get("type") == "tool_result" \
                    and block.get("tool_use_id") in calls:
                res = _json_result(block.get("content"))
                if res is not None:
                    out.append(res)
    return out


def screen_due(cwd, identity_path=None):
    """The onboarding step whose screen is due now, or None."""
    verb = _load_screen_verb()
    if verb is None:
        return None
    try:
        return verb.due_step(cwd, path=identity_path)
    except Exception:
        return None


def _verb_reached_owner(res):
    return res.get("screen_delivered") is True or res.get("note_for_agent") in (
        "SCREEN_SENT_END_TURN", "SCREEN_UNCERTAIN_END_TURN",
        "SCREEN_FAILED_SEND_TEXT")


def verb_check(data, cwd, identity_path):
    """Stop seat for the machine-sent screens (DGN-1849).  Returns "pass"
    (the turn ran the verb -- the machine sent the moment; end the hook),
    "blocked", or None (not decided here; the caller's checks run).

    A turn whose verb call reached the owner (or handed back the fallback
    text) passes; on an opening turn it also puts the opener on record
    (the self-introduction rode --ack).  An owner-opened, non-opening turn
    with a step due and no such call is blocked once."""
    transcript = data.get("transcript_path") or ""
    results = [r for r in verb_results(transcript) if _verb_reached_owner(r)]
    if results:
        if _fc_state is not None and opener_on_record(cwd) is None:
            sent = " ".join(t for r in results for t in (r.get("sent_text") or [])
                            if isinstance(t, str))
            _fc_state.record_opener(_resolve_log_dir(cwd), sent,
                                    _fc_state.boundary_key(
                                        current_boundary_text(transcript) or ""),
                                    q1_due=False, source="verb")
        _log(cwd, "VERB_TURN", "screens %s" % ",".join(
            "%s:%s" % (r.get("step"), r.get("note_for_agent")) for r in results))
        return "pass"
    if current_boundary_text(transcript) is None:
        return None  # unreadable transcript / no boundary: never block
    sent = opener_on_record(cwd)
    if is_opening_turn(data, cwd, identity_path, sent):
        return None
    if boundary_is_injected(transcript):
        return None
    due = screen_due(cwd, identity_path)
    if due is None:
        return None
    blocked = emit_block(cwd, VERB_SKIP_REASON % (due, _screen_verb_cmd()))
    _log(cwd, "VERB_SKIP_BLOCK" if blocked else "VERB_SKIP_OFF_SUPPRESSED",
         "step %s due, no onboarding verb this turn" % due)
    return "blocked" if blocked else None


def _screen_verb_cmd():
    oc = _load_sibling("onboarding-check.py", "onboarding_check_for_gate")
    return getattr(oc, "SCREEN_VERB", None) or "python3 routines/" + _SCREEN_VERB_NAME


def screen_note(cwd, identity_path=None):
    """UserPromptSubmit lines for a pending owner turn: the screen rule
    (single source, onboarding-check.py) plus the open step, or ""."""
    due = screen_due(cwd, identity_path)
    if due is None:
        return ""
    oc = _load_sibling("onboarding-check.py", "onboarding_check_for_gate")
    rule = oc.screen_rule(identity_path) if oc is not None else ""
    return ((rule + " ") if rule else "") + SCREEN_DUE_NOTE % (due, due)


def first_contact_check(data, cwd, identity_path):
    """Stop seat for the first-contact opener.  True iff it emitted a block
    (the caller then ends the hook).  Caller guarantees: Stop event,
    onboarding pending, not stop_hook_active.

    Opening turn: put the reply on record; kit-pending -> check the opener
    content (FC_OK / FC_BLOCK).  Later turn with the opener on record and
    Name still unset (owner-opened): a repeated opener -- or, on the first
    such turn, a reply with no Q1 name ask -- is blocked once
    (FC_REPEAT_BLOCK)."""
    try:
        transcript = data.get("transcript_path") or ""
        sent = opener_on_record(cwd)
        text = final_turn_text(data)
        if is_opening_turn(data, cwd, identity_path, sent):
            if not text.strip():
                return False
            record_opening_reply(data, cwd)
            if not kit_pending_role(identity_path):
                return False
            problem = opener_problem(text, identity_path,
                                     boundary_is_first_contact(transcript))
            if problem is None:
                _log(cwd, "FC_OK", "kit-pending opener clean")
                return False
            blocked = emit_block(cwd, FIRST_CONTACT_BLOCK_REASON % problem)
            _log(cwd, "FC_BLOCK" if blocked else "FC_OFF_SUPPRESSED", problem)
            return blocked
        if sent is None or _fc_state is None or not text.strip():
            return False
        if not (identity_path and _slot_unset(identity_path, _NAME_PREFIX)):
            return False
        if boundary_is_injected(transcript):
            return False
        q1_due = bool(sent.get("q1_due"))
        problem = _fc_state.repeat_problem(final_reply_text(data),
                                           sent.get("text") or "", q1_due)
        if q1_due:
            _fc_state.mark_q1_checked(_resolve_log_dir(cwd))
        if problem is None:
            _log(cwd, "FC_SETUP_OK", "post-opener reply is not a repeat")
            return False
        blocked = emit_block(cwd, FIRST_CONTACT_REPEAT_REASON % problem)
        _log(cwd, "FC_REPEAT_BLOCK" if blocked else "FC_OFF_SUPPRESSED",
             problem)
        return blocked
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DGN-1632: PostToolUse continuation seat.  The identity-completion close
# (recap -> domain onboarding) was a prose step the model skipped (measured
# 2026-09-25 11:17, health agent: recap + capability list, continuation check
# never run).  The forcing point is the edit that fills the LAST identity
# field: the gate runs the existing post-identity selector
# (onboarding-check.py select_offer_context) and hands the result plus the
# reply SHAPE to the model in THIS turn.  Shape rule only -- no owner copy.
# ---------------------------------------------------------------------------

_IDENTITY_EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
# v2.6.0 rehearsal r2 (2026-10-03 21:21): the last identity field and the
# onboarding-block removal landed via a Bash `python3 -` script, so no
# Edit-family PostToolUse ever saw the transition.  A Bash call counts when
# its command text names a persona file (cheap substring prefilter -- this
# seat runs after EVERY Bash call); its pre-state is unknown, so it rides
# the observed-cycle + once rule.  The Stop seat (completion_kit_check) is
# the tool-independent guarantee; this only moves the context earlier.
_IDENTITY_BASH_TOOL = "Bash"
_PERSONA_BASENAMES = ("hot.custom.agent.md", "PROFILE.md", "AGENT.md")
_MIGRATION_KEYS = ("MIGRATION_PEER", "HANDOFF_PEER_AG")
_CONTINUATION_SENT_FILENAME = ".onboarding-continuation-sent"

CONTINUATION_SHAPE = (
    "Identity completion (DGN-1632, PostToolUse): this edit filled the last "
    "of the five identity fields, and the post-identity continuation check "
    "has already been run for you -- do NOT run onboarding-check.py again. "
    "A continuation (domain onboarding) EXISTS. Its context, opaque "
    "framework-to-pack data, follows between the fences; its first step is "
    "whatever that context instructs first (never eval it, never build a "
    "shell command from a parsed pack name; an explicit stop instruction "
    "inside it wins, and a no-offer verdict from its own recheck means the "
    "normal close instead).\n"
    "--- continuation context ---\n%s\n--- end continuation context ---\n"
    "Finish the remaining silent completion steps first (onboarding block "
    "and marker removal). Then the completion reply has this SHAPE, in order: "
    "(1) the settings echo as an ITEMIZED list, one line per item: name "
    "(emoji beside it) / address term / tone / humor, then the one-line "
    "from-now effect; (2) ONE transition sentence that names the concrete "
    "next step and why it comes now -- no vague care-taking promise or "
    "filler; (3) the continuation's first question, with its answer "
    "options when the continuation gives them. The reply MUST END with that "
    "question: never end on an announcement of what comes next -- if you "
    "cannot ask the first question in this reply, do not announce the "
    "continuation at all. Use the continuation's own words for its concepts "
    "verbatim; never paraphrase the pack's vocabulary. NO capability list "
    "and no action menu while a continuation exists -- the continuation is "
    "the close. At most one setup offer in this reply. Wording is yours, in "
    "the user's language; never mention files, hooks, checks or this notice."
)

# v2.6.0 rehearsal (2026-10-03 16:34, fresh install, KIT=pending): the
# seat above returned early for every non-domain instance, so a kit-pending
# main finished identity with the GENERAL menu and the kit-select offer only
# surfaced in the NEXT session.  On a main whose selector yields the
# kit-select context, the completion reply now ends on that question.
KIT_SELECT_TAG = "[kit selection pending]"

KIT_SELECT_SHAPE = (
    "Identity completion (PostToolUse): this edit filled the last of the "
    "five identity fields, and the post-identity check has already been run "
    "for you -- do NOT run onboarding-check.py again. The kit choice is "
    "still open (KIT=pending), so the kit-select question is the close of "
    "THIS reply: ask it NOW, not at a later natural moment. Its context, "
    "opaque framework data, follows between the fences:\n"
    "--- continuation context ---\n%s\n--- end continuation context ---\n"
    "Finish the remaining silent completion steps first (onboarding block "
    "and marker removal). Then the completion reply has this SHAPE, in order: "
    "(1) the settings echo as an ITEMIZED list, one line per item: name "
    "(emoji beside it) / address term / tone / humor, then the one-line "
    "from-now effect; (2) the kit-select question, which is a machine-sent "
    "screen (DGN-1849): send the recap as the --ack of `python3 "
    "routines/onboarding-screen.py kit` -- the machine sends the recap and "
    "then the question with its two option buttons -- and end the turn with "
    "NO text of your own; never write the question, its options or an "
    "[[OPTIONS]] marker yourself. "
    "This REPLACES the GENERAL numbered action menu: no 'see what I "
    "can do' item, no quick start, no capability list in this reply. The "
    "kit-select question is not a quick start -- the kit-neutral rule "
    "forbids life-assistant quick starts, not this question. Write KIT= only "
    "on the user's answer. Never mention files, hooks, checks, KIT= or this "
    "notice."
)

NO_CONTINUATION_SHAPE = (
    "Identity completion (DGN-1632, PostToolUse): this edit filled the last "
    "of the five identity fields, and the post-identity continuation check "
    "has already been run for you -- do NOT run onboarding-check.py again. "
    "NO continuation (no domain onboarding) exists for this instance. "
    "Finish the remaining silent completion steps, then use the normal "
    "close: recap, role line and capability list, with no soft invite and "
    "no domain setup offer. Never mention files, hooks, checks or this "
    "notice."
)


def _edit_pre_text(tool_name, tool_input, tool_response, post_text):
    """Best-effort identity-file text from BEFORE this tool call, or None.
    Prefers the harness's own originalFile; else reverses Edit/MultiEdit
    replacements on the post text.  None = undeterminable."""
    if isinstance(tool_response, dict):
        orig = tool_response.get("originalFile")
        if isinstance(orig, str):
            return orig
    if not isinstance(tool_input, dict) or post_text is None:
        return None
    if tool_name == "Edit":
        edits = [tool_input]
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
    else:
        return None
    text = post_text
    for ed in reversed(edits):
        if not isinstance(ed, dict):
            return None
        old, new = ed.get("old_string"), ed.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        if new not in text:
            return None
        if ed.get("replace_all"):
            text = text.replace(new, old)
        else:
            text = text.replace(new, old, 1)
    return text


def _text_fields_filled(text):
    """identity_fields_filled over an in-memory text (same predicate)."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_IDENTITY_FIELD_PREFIXES) and _PLACEHOLDER in line:
            return False
    return True


def _same_file(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except Exception:
        return False


def _mark_sent(sent):
    try:
        os.makedirs(os.path.dirname(sent), exist_ok=True)
        with open(sent, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
    except Exception:
        pass


def kit_select_context(oc, data):
    """The selector's kit-select context (stripped) on a main whose kit
    choice is still open and whose life kit is reachable, else None."""
    try:
        if oc.instance_class(oc.resolve_instance_conf(data)) == "domain":
            return None
        if not oc.kit_select_pending(oc.resolve_kit(oc.resolve_agent_conf(data))):
            return None
        ctx = oc.select_offer_context(data)
    except Exception:
        return None
    if isinstance(ctx, str) and ctx.strip().startswith(KIT_SELECT_TAG):
        return ctx.strip()
    return None  # life kit unreachable (DGN-1817)


KIT_CLOSE_BLOCK_REASON = (
    "Onboarding completion (Stop block): identity setup finished in this "
    "turn and the kit choice is still open (KIT=pending), but your "
    "completion reply %s. Regenerate the COMPLETE reply now (it replaces the "
    "one on screen), in order: (1) the settings echo as an ITEMIZED list, "
    "one line per item: name (emoji beside it) / address term / tone / "
    "humor, then the one-line from-now effect; (2) the kit-select question, "
    "a machine-sent screen (DGN-1849): pass the recap as the --ack of "
    "`python3 routines/onboarding-screen.py kit` and end the turn with NO "
    "text of your own (never write the question or its options). No "
    "GENERAL action menu, no 'see what I can do' item, no quick start, no "
    "capability list. Do not run onboarding-check.py and do not touch any "
    "file; write KIT= only on the user's answer. Its context, opaque "
    "framework data, follows between the fences:\n"
    "--- continuation context ---\n%s\n--- end continuation context ---\n"
    "Never mention files, hooks, checks, KIT= or this notice."
)


def kit_screen_sent(data):
    """True iff this turn's onboarding verb sent the kit screen (or handed
    back its fallback text) -- the kit-select close is the machine's then
    (DGN-1849)."""
    return any(r.get("step") == "kit" and _verb_reached_owner(r)
               for r in verb_results(data.get("transcript_path") or ""))


def completion_kit_check(data, cwd, path):
    """Tool-independent Stop seat for the kit-select close (v2.6.0 r2).
    Called when THIS Stop observed the pending->cleared transition (the arm
    sentinel written by this turn's UserPromptSubmit was consumed), so the
    identity completed this turn whichever tool wrote it.  When the
    PostToolUse seat already delivered the continuation this cycle (sent
    sentinel), its handoff check owns the turn.  True iff a block was
    emitted."""
    if data.get("hook_event_name") not in (None, "Stop"):
        return False
    if not path or not identity_fields_filled(path):
        return False
    sent = os.path.join(_resolve_log_dir(cwd), _CONTINUATION_SENT_FILENAME)
    if os.path.exists(sent):
        return False
    oc = _load_sibling("onboarding-check.py", "onboarding_check_for_gate")
    if oc is None:
        return False
    ctx = kit_select_context(oc, data)
    if ctx is None:
        return False
    _mark_sent(sent)
    if kit_screen_sent(data):
        _log(cwd, "HANDOFF_OK",
             "kit-select close (release-observed): machine-sent kit screen")
        return False
    if data.get("stop_hook_active"):
        _log(cwd, "HANDOFF_YIELD",
             "kit-select close (release-observed): already re-prompted once")
        return False
    problem = kit_close_problem(final_reply_text(data))
    if problem is None:
        _log(cwd, "HANDOFF_OK",
             "completion reply ends on the kit-select question (release-observed)")
        return False
    blocked = emit_block(cwd, KIT_CLOSE_BLOCK_REASON % (problem, ctx))
    _log(cwd, "HANDOFF_BLOCK" if blocked else "HANDOFF_OFF_SUPPRESSED",
         "kit-select close (release-observed): %s" % problem)
    return blocked


def continuation_context(data):
    """Model-facing additionalContext for a PostToolUse identity edit, or
    None (stay silent).  Fires only on the edit that TRANSITIONS the five
    identity fields to filled, on a domain instance off the migration path
    (that path keeps its own ending) or a kit-pending main.  A Bash call
    counts when its command names the resolved persona file (pre-state
    unknown -> observed-cycle + once rule)."""
    tool_name = data.get("tool_name")
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if tool_name == _IDENTITY_BASH_TOOL:
        command = tool_input.get("command")
        if not isinstance(command, str) or not any(
                b in command for b in _PERSONA_BASENAMES):
            return None
        target = None
    elif tool_name in _IDENTITY_EDIT_TOOLS:
        target = tool_input.get("file_path")
        if not isinstance(target, str) or not target:
            return None
    else:
        return None
    oc = _load_sibling("onboarding-check.py", "onboarding_check_for_gate")
    if oc is None:
        return None
    path, _collision = oc.resolve_target(data)
    if not path or not os.path.isfile(path):
        return None
    cwd = str(data.get("cwd") or os.getcwd())
    if target is None:
        if os.path.basename(path) not in tool_input["command"]:
            return None
    else:
        if not os.path.isabs(target):
            target = os.path.join(cwd, target)
        if not _same_file(target, path):
            return None
    try:
        with open(path, encoding="utf-8") as f:
            post_text = f.read()
    except Exception:
        return None
    if not _text_fields_filled(post_text):
        return None  # not the last field yet
    pre_text = None if target is None else _edit_pre_text(
        tool_name, tool_input, data.get("tool_response"), post_text)
    sent = os.path.join(_resolve_log_dir(cwd), _CONTINUATION_SENT_FILENAME)
    if pre_text is not None:
        if _text_fields_filled(pre_text):
            return None  # fields were already complete: a later edit
    elif not (MARKER in post_text or os.path.exists(_arm_path(cwd))) \
            or os.path.exists(sent):
        # Pre-state unknown: fire only inside an observed onboarding cycle,
        # once.
        return None
    conf = oc.resolve_agent_conf(data)
    if oc.instance_class(oc.resolve_instance_conf(data)) != "domain":
        # A main/general close keeps its menu -- unless the kit choice is
        # still open: then the kit-select offer is the close (v2.6.0).
        ctx = kit_select_context(oc, data)
        if ctx is None:
            return None  # KIT decided / life kit unreachable: menu close stands
        _mark_sent(sent)
        return KIT_SELECT_SHAPE % ctx
    if any((oc.conf_get(k, conf) or "").strip() for k in _MIGRATION_KEYS):
        return None
    ctx = oc.select_offer_context(data)
    # Log-only (no owner output): a domain agent with no pack-declared
    # post_mint_next slot silently gets no continuation -- make it visible.
    try:
        gap = oc.post_mint_slot_gap(data)
        if gap is not None:
            _log(cwd, "SLOT_ABSENT",
                 "domain instance %s: no installed pack declares post_mint_next "
                 "(packs: %s)" % (os.path.basename(os.path.normpath(cwd)),
                                  ", ".join(gap) or "none"))
    except Exception:
        pass
    _mark_sent(sent)
    if isinstance(ctx, str) and ctx.strip():
        return CONTINUATION_SHAPE % ctx.strip()
    return NO_CONTINUATION_SHAPE


def emit_continuation(data):
    cwd = str(data.get("cwd") or os.getcwd())
    try:
        ctx = continuation_context(data)
    except Exception:
        ctx = None
    if not ctx:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": ctx
    }}, ensure_ascii=False), flush=True)
    present = "--- continuation context ---" in ctx
    kit = ctx.startswith(KIT_SELECT_SHAPE.split(":", 1)[0] + ":")
    if present:
        arm_handoff_check(cwd, "kit" if kit else "")
    _log(cwd, "CONTINUATION",
         "identity fields completed by this edit; continuation %s"
         % ("kit-select" if kit else "present" if present else "absent"))


# ---------------------------------------------------------------------------
# Block emission (gate-switch aware, output-gate-stop.py precedent)
# ---------------------------------------------------------------------------

def emit_block(cwd, reason=BLOCK_REASON):
    gs = _load_sibling("gate-switch.py", "gate_switch_for_onboarding")
    if gs is not None:
        try:
            if not gs.is_enabled(_GATE_SLUG, cwd):
                print(json.dumps(gs.off_notice(_GATE_SLUG, reason, cwd),
                                 ensure_ascii=False), flush=True)
                return False
        except Exception:
            pass  # a broken switch never disarms the gate
    print(json.dumps({"decision": "block", "reason": reason},
                     ensure_ascii=False), flush=True)
    return True


def dispatch_return_stop(data, cwd):
    """DGN-1715 r2: a dispatch-return turn whose follow-up was denied must end
    on a one-line proposal + the options marker (marker test, no wording).
    True iff a Stop block was emitted.  Shares this hook's Stop seat and
    MISS_VISIBLE miss sentinel; fails open."""
    if _forcing is None:
        return False
    try:
        verdict = _forcing.stop_verdict(
            data, cwd, turn_text(data.get("transcript_path") or ""))
        if verdict is None:
            return False
        if verdict == "PASS":
            _log(cwd, "DR_PASS", "denied follow-up ended on an options proposal")
            return False
        if verdict in ("MISS_VISIBLE", "YIELD"):
            _forcing.record_miss(_resolve_log_dir(cwd))
            _log(cwd, "DR_" + verdict,
                 "denied follow-up, no options marker; %s -- not blocking, "
                 "next owner prompt carries the reminder" % (
                     "final message already delivered" if verdict == "MISS_VISIBLE"
                     else "already re-prompted once"))
            return False
        gs = _load_sibling("gate-switch.py", "gate_switch_for_dispatch_return")
        if gs is not None and not gs.is_enabled(_DR_SLUG, cwd):
            return False
        print(json.dumps({"decision": "block", "reason": _forcing.STOP_BLOCK_REASON},
                         ensure_ascii=False), flush=True)
        _log(cwd, "DR_BLOCK", "denied follow-up, final message has no options marker")
        return True
    except Exception:
        return False


def dispatch_return_reminder(cwd):
    """One-shot reminder text for the next owner prompt ("" when none)."""
    if _forcing is None:
        return ""
    try:
        return _forcing.MISS_REMINDER if _forcing.consume_miss(_resolve_log_dir(cwd)) else ""
    except Exception:
        return ""


def emit_context(note):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": note
    }}, ensure_ascii=False), flush=True)


def main():
    try:
        try:
            data = json.load(sys.stdin)
        except Exception:
            sys.exit(0)
        if not isinstance(data, dict):
            sys.exit(0)
        # Main-session prompt/Stop only; never gate SubagentStop.
        if data.get("hook_event_name") not in (
                None, "Stop", "UserPromptSubmit", "PostToolUse"):
            sys.exit(0)
        if not session_is_interactive():
            sys.exit(0)

        # DGN-1632: identity-completing edit -> continuation + reply shape.
        # Independent of the marker: the last field may land in the same
        # Write that drops the onboarding block.
        if data.get("hook_event_name") == "PostToolUse":
            emit_continuation(data)
            sys.exit(0)

        cwd = str(data.get("cwd") or os.getcwd())

        # DGN-1715 r2: forcing point for a stalled dispatch-return turn.
        # A block is this hook's only output for the event.
        dr_note = ""
        if data.get("hook_event_name") in (None, "Stop"):
            if dispatch_return_stop(data, cwd):
                sys.exit(0)
        elif data.get("hook_event_name") == "UserPromptSubmit":
            head = str(data.get("prompt") or "").lstrip()
            if not head.startswith((INJECTED_TURN_MARK,) + _INJECTED_BELT_PREFIXES):
                dr_note = dispatch_return_reminder(cwd)

        # DGN-1632 W3: the identity-completion turn armed a handoff check.
        if data.get("hook_event_name") in (None, "Stop"):
            if handoff_check(data, cwd):
                sys.exit(0)

        path, pending = resolve_identity(data)

        if not pending:
            # Steady state (99.9% of this hook's life): one file read, zero
            # output.  Log the pending->cleared transition exactly once.
            released = release_if_armed(
                cwd, "marker cleared (identity=%s)" % (path or "?"))
            # v2.6.0 r2: the kit-select close keyed on the transition, not
            # on the tool that wrote the last field.
            if released and completion_kit_check(data, cwd, path):
                sys.exit(0)
            if dr_note:
                emit_context(dr_note)
            sys.exit(0)

        arm(cwd)

        # Guaranteed exit: onboarding is DONE by field state but the marker
        # was left behind -> the machine clears it (never a permanent trap).
        if path and identity_fields_filled(path):
            if clear_marker(path):
                _log(cwd, "AUTO_CLEAR",
                     "all identity fields filled; marker removed by gate "
                     "(identity=%s)" % path)
                if release_if_armed(cwd, "auto-cleared (identity=%s)" % path) \
                        and completion_kit_check(data, cwd, path):
                    sys.exit(0)
            if dr_note:
                emit_context(dr_note)
            sys.exit(0)

        # DGN-1646: pre-generation context; do not reject the owner's prompt
        # (UserPromptSubmit decision:block would prevent onboarding itself).
        # No tool-touch can yet exist for this turn. Never consult old turns.
        if data.get("hook_event_name") == "UserPromptSubmit":
            prompt = str(data.get("prompt") or "").lstrip()
            if prompt.startswith((INJECTED_TURN_MARK,) + _INJECTED_BELT_PREFIXES):
                sys.exit(0)
            # DGN-1837 r2: the opener is on record -> this owner turn is
            # identity setup; say so first, before any first-contact prose
            # (a state fact, so it rides even with the gate switched off).
            setup_phase = (opener_on_record(cwd) is not None and bool(path)
                           and _slot_unset(path, _NAME_PREFIX))
            # DGN-1849: the due screen step + verb rule (a state fact too).
            screens = screen_note(cwd, path)
            gs = _load_sibling("gate-switch.py", "gate_switch_for_onboarding")
            if gs is not None and not gs.is_enabled(_GATE_SLUG, cwd):
                note = " ".join(n for n in (
                    IDENTITY_SETUP_NOTE if setup_phase else "", screens) if n)
                if note:
                    emit_context(note)
                sys.exit(0)
            reason = BLOCK_REASON.replace(
                "Stop block, DGN-1543 R7", "before generation, DGN-1646").replace(
                "Regenerate now:", "Before answering:").replace(
                "In the regenerated reply:", "In your reply:")
            missed = consume_miss(cwd)
            if missed:
                reason = MISS_REMINDER + " " + reason
            if dr_note:
                reason = dr_note + " " + reason
            copy_rec = consume_copy_miss(cwd)
            copy_note = copy_miss_reminder(copy_rec) if copy_rec else ""
            if copy_note:
                reason = copy_note + " " + reason
            if screens:
                reason = screens + " " + reason
            if setup_phase:
                reason = IDENTITY_SETUP_NOTE + " " + reason
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": reason
            }}, ensure_ascii=False), flush=True)
            _log(cwd, "PRE_GENERATION",
                 "pending marker: onboarding required before reply"
                 + (" (+ miss reminder)" if missed else "")
                 + (" (+ copy reminder)" if copy_note else "")
                 + (" (+ identity-setup phase)" if setup_phase else "")
                 + (" (+ screen step)" if screens else ""))
            sys.exit(0)

        # One block per turn, never a loop (output-gate-stop precedent).
        if data.get("stop_hook_active"):
            refresh_regenerated_opener(data, cwd)
            _log(cwd, "YIELD", "stop_hook_active -- already re-prompted once")
            sys.exit(0)

        # DGN-1849: a turn that ran the onboarding verb passes; an owner
        # turn that skipped it while a step is due is blocked once.
        if verb_check(data, cwd, path) is not None:
            sys.exit(0)

        # DGN-1837: kit-pending opener (the bridge's first-contact turn is
        # machine-opened, so this runs before the injected-turn abstention).
        if first_contact_check(data, cwd, path):
            sys.exit(0)

        # DGN-1606: a machine-opened turn (session-inbox / cron-inject, the
        # bridge marks it) has no owner in it -- demanding onboarding
        # engagement there pressures the model to run the owner's Q&A into
        # the void (measured 2026-09-20: an injected restart-verify turn
        # completed onboarding alone and self-named).  Abstain; the gate
        # re-arms on the next owner-opened turn.
        if boundary_is_injected(data.get("transcript_path") or ""):
            _log(cwd, "SKIP_INJECTED",
                 "turn opened by a machine-injected message -- no owner "
                 "present, onboarding not demanded")
            sys.exit(0)

        # DGN-1632 W3: owner-visible copy rules (address term before Q3,
        # origin story).  The text is already on screen -> never a block;
        # the next prompt's injection carries a specific reminder.
        try:
            violation = copy_violations(data, path, final_turn_text(data))
            if violation:
                record_copy_miss(cwd, violation)
                _log(cwd, "MISS_COPY", "%s (terms: %s)" % (
                    ",".join(violation["kinds"]),
                    ",".join(violation["terms"]) or "-"))
        except Exception:
            pass

        touched, visible = scan_turn(data.get("transcript_path") or "",
                                     surface_basenames(path))
        if touched:
            _log(cwd, "PASS_TOUCH", "onboarding surface touched this turn")
            sys.exit(0)
        last_text = data.get("last_assistant_message")
        if isinstance(last_text, str) and last_text.strip():
            visible = True
        if visible:
            # DGN-1703: the reply is already on the owner's screen; a block
            # would only add a second message.  Remind before the next
            # generation instead.
            record_miss(cwd)
            _log(cwd, "MISS_VISIBLE",
                 "pending marker + no onboarding engagement, reply already "
                 "delivered -- not blocking; next prompt carries the reminder "
                 "(identity=%s)" % (path or "?"))
            sys.exit(0)

        blocked = emit_block(cwd)
        _log(cwd, "BLOCK" if blocked else "OFF_SUPPRESSED",
             "pending marker + zero onboarding engagement and no "
             "owner-visible text this turn (identity=%s)" % (path or "?"))
        sys.exit(0)

    except SystemExit:
        raise
    except BaseException:
        # Catch-all fail-open: never wedge a bridge turn.
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)


if __name__ == "__main__":
    main()
