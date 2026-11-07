"""DGN-1683: turn mute after the mint verb already delivered the owner screen.

THE DEFECT (live 2026-09-24 16:23, dev.44). Owner asked for a new agent; the
agent called the mint verb (`open`), the verb pushed the catalog screen to
Telegram itself and returned `screen_delivered: true` /
`note_for_agent: "SCREEN_SENT_END_TURN"`. The agent then ALSO ended the turn
with prose ("No active flow, so open a new one.\n(...)") -- English internal
reasoning plus a narration of the rule. The mint SKILL says "add nothing";
that is a rule the MODEL executes, and the first live use proved the model
does not reliably execute it. This module is the forcing point.

THE CONTRACT. The verdict is read from the verb's own tool_result JSON --
never from prose -- and only for a main-agent Bash call whose command runs
the verb (`mint_flow.sh` / `mint_flow.py`):

    screen_delivered true OR note SCREEN_SENT_END_TURN  -> MUTE_SCREEN
        the owner-bound text of the turn is suppressed (nothing is sent)
    agent_utterance "N13"                               -> MUTE_N13
        the owner-bound text is exactly N13_SENTENCE, nothing else
    anything else / no verb call                        -> no verdict,
        the turn is untouched

A verdict latches for the rest of the turn; MUTE_SCREEN outranks MUTE_N13
(once a screen is on the owner's phone, the fallback sentence would
contradict it).

N13 policy: when the agent produced ANY owner-bound text in an N13 turn, the
owner receives exactly N13_SENTENCE -- extra text is cut, a paraphrase is
replaced. Suppressing instead would leave the owner with neither a screen
nor a notice after a delivery failure, the one case the fallback exists for;
the sentence is fixed approved copy, so emitting it can never leak. When the
agent said nothing, nothing is sent: the bridge never speaks on its own.

DGN-1849 (dec-243): the onboarding verb (routines/onboarding-screen.py)
follows the same contract -- it sends the fixed onboarding screens (Q1-Q5,
kit offer) itself and returns the same result keys, so a call to it mutes
the turn exactly like a mint verb call. It also returns
SCREEN_UNCERTAIN_END_TURN when Telegram may already hold the screen: that
mutes too (a second, model-written copy of the question is worse than none).
It never emits N13.

PROVISIONAL HOLD (pre-verb gap, live 2026-10-04). The CLI streams each
content block of one API message as its own assistant event, so narration
written BEFORE the verb call ("Run open without --fit first ...") arrives in
a text-only event and the same-message rule cannot see the call yet. A turn
that loads the mint skill or calls a verb (arms_hold) therefore holds all
later owner-bound text until the turn ends: a verdict discards it, no verdict
releases it unchanged (sdk_bridge._settle_mint_held).

N13_SENTENCE is a lockstep copy of the OWNER-SAY block in the mint-agent
SKILL.md (tests/test_dgn1683_mint_screen_mute.py pins the two together).
Stdlib-only; no telegram / SDK imports.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

MUTE_SCREEN = "screen"
MUTE_N13 = "n13"

# ASCII source: the Korean OWNER-SAY sentence as \u escapes.
N13_SENTENCE = (
    "\ud654\uba74 \uc804\ub2ec \uc0c1\ud0dc\ub97c \ud655\uc778\ud558\uc9c0 \ubabb\ud588\uc5b4\uc694. \uc7a0\uc2dc \ud6c4 \ub2e4\uc2dc \uc694\uccad\ud574 \uc8fc\uc138\uc694."
)

# The verb entry point (scripts/pack/mint_flow.sh) and the library it execs,
# plus the onboarding verb (DGN-1849).
_VERB_CALL_RE = re.compile(
    r"(?:^|[\s/'\"])(?:mint_flow\.(?:sh|py)|onboarding-screen\.py)(?=$|[\s'\";|&)])")
_MUTE_NOTES = ("SCREEN_SENT_END_TURN", "SCREEN_UNCERTAIN_END_TURN")

# Keys every verb emission carries (mint_flow.emit); a JSON object without
# them is not a verb result, whatever else it says.
_RESULT_KEYS = ("screen_delivered", "note_for_agent", "agent_utterance")

_RANK = {None: 0, MUTE_N13: 1, MUTE_SCREEN: 2}


def is_verb_call(tool_name: Any, tool_input: Any) -> bool:
    """True for a Bash tool call that runs the mint or onboarding verb."""
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    return isinstance(command, str) and bool(_VERB_CALL_RE.search(command))


# The mint skill (any namespaced form). The skill name is estate-only; the
# public build carries an empty name, so only verb calls arm the hold there.
# ESTATE:BEGIN the mint skill name is an estate skill id (DGN-818 C3); the public build matches no skill
MINT_SKILL = "dogany-mint-agent"
# ESTATE:ELSE
# MINT_SKILL = ""
# ESTATE:END


def is_mint_skill_call(tool_name: Any, tool_input: Any) -> bool:
    """True for a Skill tool call that loads the mint skill."""
    if tool_name != "Skill" or not isinstance(tool_input, dict):
        return False
    skill = tool_input.get("skill")
    return (bool(MINT_SKILL) and isinstance(skill, str)
            and skill.strip().rsplit(":", 1)[-1] == MINT_SKILL)


def arms_hold(tool_name: Any, tool_input: Any) -> bool:
    """True for a call that opens a mint turn: the mint skill or a verb call.

    From that call on, owner-bound text is held until the turn ends (see
    sdk_bridge._PendingRequest.mint_armed)."""
    return is_mint_skill_call(tool_name, tool_input) or is_verb_call(tool_name, tool_input)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _verb_result(content: Any) -> Optional[dict]:
    """The LAST verb-shaped JSON object in a tool_result, or None."""
    text = _result_text(content)
    found = None
    candidates = [text.strip()] + [ln.strip() for ln in text.splitlines()]
    for cand in candidates:
        if not cand.startswith("{"):
            continue
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict) and all(k in obj for k in _RESULT_KEYS):
            found = obj
    return found


def verdict_from_result(content: Any) -> Optional[str]:
    """Map a verb tool_result to MUTE_SCREEN / MUTE_N13 / None."""
    obj = _verb_result(content)
    if obj is None:
        return None
    if obj.get("screen_delivered") is True or obj.get("note_for_agent") in _MUTE_NOTES:
        return MUTE_SCREEN
    if obj.get("agent_utterance") == "N13":
        return MUTE_N13
    return None


def merge(current: Optional[str], new: Optional[str]) -> Optional[str]:
    """Latch: a verdict never weakens within a turn (screen > n13 > none)."""
    return new if _RANK.get(new, 0) > _RANK.get(current, 0) else current


def gate_text(verdict: Optional[str], text: str) -> str:
    """The owner-bound text a turn with `verdict` may carry."""
    if verdict == MUTE_SCREEN:
        return ""
    if verdict == MUTE_N13:
        return N13_SENTENCE if (text or "").strip() else ""
    return text
