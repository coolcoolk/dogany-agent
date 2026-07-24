#!/usr/bin/env python3
"""Dev-agent live status footer -- Stop hook.

Appends a live-status footer to the agent's response when background
subagents are still running.  Fires on the Stop event; Claude Code
automatically promotes it to SubagentStop inside subagent contexts, so a
single Stop registration covers both.

Footer format (Korean surface text for the user):
  [라이브] <desc1>, <desc2> (진행중 N개) / [결정대기] N건: <item1>; <item2>

Rev 3 (DGN-214): the [티켓] segment is replaced by [결정대기] -- the list of
decisions currently awaiting the user, read from worklog/_DECISIONS.md
(written by the main session when it poses a question, cleared on answer).
Rationale: the user reads messages stack-wise (latest first) while work is
reported queue-wise, so the LAST message must always carry the full set of
pending decisions ("latest message is sufficient" invariant).

Rev 4 (DGN-214 work B): every Stop from the OWNER session also regenerates
<BOT_DATA_DIR>/dashboard.md, the content source of the pinned live-dashboard
message that bridge/dashboard.py mirrors into Telegram.  The session ownership
guard is its SOLE gate, fail-closed (no confirmed owner -> no write; one
writer per shared surface, DGN-198).  Atomic write: tmp + os.replace.

Rev 5 (DGN-531 sole-author model): blocking-error path REMOVED.  The hook
writes the canonical footer to a sidecar file
(<BOT_DATA_DIR>/footer-sidecar.json) and exits 0.  The bridge
(_consume_footer_sidecar in sdk_bridge.py) reads the sidecar at finalize
time, strips any model-written [라이브]/[결정대기] blocks from the content,
and appends the canonical footer exactly once.  This makes the hook the sole
author of the footer with no LLM involvement, eliminating the duplicate
that arose when the model appended instead of replacing (DGN-531 root cause).

Rev 6 (DGN-453, direction A: pinned dashboard single-sourcing):
The footer is now suppressed entirely.  The pinned dashboard.md is the
sole surface for [결정대기]/[라이브] display (always at the top, persistent).
The hook still regenerates dashboard.md on every owner Stop (freshness gate),
but writes an empty sidecar for all turns (no message-level footer appended).
This eliminates message-stack noise while keeping live status visible.

Rev 7 (DGN-541 S1, conditional display): the dashboard is filled ONLY when
something is actionable -- (pending decisions >= 1) OR (working subagents
>= 1).  When the board is CONFIRMED empty (all collectors succeeded and
returned nothing) an EMPTY dashboard.md is written; the bridge's debounced
delete state machine then takes the pinned message down.  A collector
FAILURE never produces an empty write (fail-open empty-ban): the previous
content is preserved so a false-empty cannot escalate into a pin delete.
Recent-completed history is extra info when the board is up, never a
standalone trigger (D2).

Rev 8 (owner spec 2026-07-24): empty-section suppression + tighter
recent-completed cap.  An empty 결정대기 / live section is OMITTED entirely
(no "- 없음" placeholder line) rather than printed as a blank stub -- the
board only ever renders when at least one of them has content anyway, so
the placeholder was pure noise.  Recent-completed is capped at 2 items
(was 5): the board is a live-status surface, not a history log.

Rev 9 (DGN-541 framework promotion): this file is framework-canonical and
byte-identical across instances.  All instance display tokens resolve
  env (DOGANY_CONSOLE_BASE / DOGANY_LIVE_LABEL / DOGANY_BOARD_EMOJI)
  -> config/agent.conf (DASHBOARD_CONSOLE_BASE / DASHBOARD_LIVE_LABEL /
     DASHBOARD_EMOJI)
  -> neutral default.
The env layer keeps hook-command overrides and test isolation working; the
conf layer survives framework updates (the .claude/settings.json Stop-hook
registration is framework-owned and carries no per-instance env).

Behaviour (rev 6):
  - Not owner session  -> exit 0 (no sidecar written).
  - NO_PUSH sentinel turn -> exit 0 (no sidecar written).
  - Owner session, any other turn -> write empty sidecar + exit 0.
    (Dashboard is regenerated before this; footer is always suppressed.)

Fail-open: any exception -> exit 0 (never block or delay a turn).

=== Liveness source (rev 2, corrected against real session data) ===
The FIRST design read each subagent's own jsonl last line and treated
stop_reason=='end_turn' as DONE.  That is WRONG: real completed background
subagents leave a last line of type=assistant with stop_reason in
{None, 'end_turn', 'tool_use'} indiscriminately -- the subagent jsonl
carries no reliable completion marker.  Verified on live session
a317096e: 4 finished agents (spike / GCal grill / cron wrapper / DGN-180
draft) had last-line stop_reason None or end_turn with no way to tell them
apart from a running one.

Correct source = the MAIN session transcript (hook input transcript_path).
For each background agentId the transcript records, in time order:
  LAUNCH     (type=user tool_result):
             "Async agent launched successfully.\nagentId: <id>"
  COMPLETION (type=user task-notification):
             "<task-notification> ... <task-id><id></task-id> ...
              <status>completed</status> ... came to rest ..."
An agent is ACTIVE iff its LAST launch line index > its LAST completion
line index (a launch with no completion after it).  SendMessage-resume
emits a fresh launch, so a resumed-but-not-yet-finished agent flips back
to ACTIVE automatically.  Descriptions come from the launch's neighbouring
meta or, more robustly, from
  ~/.claude/projects/<enc>/<sess>/subagents/agent-<id>.meta.json
  -> {"agentType","description","toolUseId"}

The meta.json "description" is frozen at spawn (the Agent tool's description
param); a SendMessage-resume never rewrites it, so a mid-flight redirect left
the label stuck at the spawn value (DGN-211).  Fix: each SendMessage the
harness asks the caller to write a 5-10 word `summary` recap; we track the
LATEST such recap per agentId and prefer it over the frozen description when
it post-dates the launch, so the label follows the current instruction.

The transcript can be large; we stream it line by line and keep only two
small dicts (agentId -> last launch idx, agentId -> last completion idx),
so memory stays bounded regardless of transcript size.
"""

import json
import os
import re
import sys
import time

FOOTER_MARKER = "[라이브]"
# A model-written footer may carry only the [결정대기] block (no [라이브]) when
# the turn poses a decision -- the dedup guard must detect EITHER marker, else
# it misreads "no existing footer" and appends the canonical on top, producing
# a duplicate [결정대기] block (observed 2026-07-20, DGN-450).
DECISION_MARKER = "[결정대기]"
MAX_LIVE_DISPLAY = 5     # cap live agent descriptions shown in footer
MAX_DECISION_DISPLAY = 3 # how many pending-decision items to show in footer
DECISIONS_FILE = "worklog/_DECISIONS.md"
LEDGER_FILE = "product/auto-loop-ledger.md"

# Worklog lives at <workspace_root>/worklog; this script is at
# <workspace_root>/routines/status-footer.py.  Anchor to the script's own
# location so the ticket count does not depend on the Stop-hook cwd (which is
# not guaranteed to be the workspace root -- observed count 0 · 0 when it was
# not).
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _conf_get(key):
    """Read KEY=value from config/agent.conf (usage-gate PLAN precedent).

    Instance display tokens resolve env -> config/agent.conf -> neutral
    default (Rev 9).  DOGANY_AGENT_CONF overrides the conf path (test
    isolation, DOGANY_DECISIONS_FILE convention).  Missing file / missing
    key -> "" (falls through to the neutral default).
    """
    path = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
        _ROOT_DIR, "config", "agent.conf")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


# Console base for decision deep links.  Instance-specific: env
# DOGANY_CONSOLE_BASE -> conf DASHBOARD_CONSOLE_BASE -> empty.  Empty ->
# decision items render WITHOUT links (instances without a console degrade
# gracefully, DGN-541).
CONSOLE_BASE = (os.environ.get("DOGANY_CONSOLE_BASE")
                or _conf_get("DASHBOARD_CONSOLE_BASE") or "")
# Live-section label on the dashboard.  Instance-specific: env
# DOGANY_LIVE_LABEL -> conf DASHBOARD_LIVE_LABEL -> neutral default for
# general users; instances inject their own sibling term via config/env
# (DGN-541 S3).
LIVE_LABEL = (os.environ.get("DOGANY_LIVE_LABEL")
              or _conf_get("DASHBOARD_LIVE_LABEL") or "서브에이전트 작업 중")
# Board title = fixed product name "작업대" (owner naming 2026-07-24) with an
# optional per-instance emoji prefix (the agent's identity emoji): env
# DOGANY_BOARD_EMOJI -> conf DASHBOARD_EMOJI -> none.
BOARD_EMOJI = (os.environ.get("DOGANY_BOARD_EMOJI")
               or _conf_get("DASHBOARD_EMOJI") or "")
BOARD_TITLE = (BOARD_EMOJI + " " if BOARD_EMOJI else "") + "작업대"

# Staleness ceiling for agents tracked as launched-but-not-completed.
#
# Two confirmed blindness cases (DGN-540, 2026-07-24 session transcript evidence):
#
# Case A (12:15): agent a71716aad82404371 launched at transcript line 386,
#   completed at line 393 (task-notification), then SendMessage-resumed at
#   line 414. The resume result is a tool_result JSON:
#   {"success":true,"message":"Agent \"<id>\" had no active task; resumed from
#   transcript in the background..."}.  The old code only scanned for
#   "Async agent launched successfully" as a launch signal; the resume was
#   invisible, so last_compl[id]=393 > last_launch[id]=386 -> agent wrongly
#   treated as completed.  Fix: _RESUME_RE detects the resume result and
#   updates last_launch[], making the resume a first-class launch event.
#
# Case B (12:23): the same agent was live but mid long-tool-call (3+ minutes
#   of jsonl silence).  LIVE_STALE_SECS=45 dropped it even though the
#   transcript still showed launch > completion.  Fix: raise the ceiling to
#   1200s (20 min).  The original concern (ghost agents from missed completion
#   events, observed agent aba030a8) is addressed by the transcript completion-
#   event detection; the jsonl gate is a fallback for completions that never
#   reach the transcript.  20 min is generous enough for long tool calls while
#   still expiring truly abandoned agents within a reasonable horizon.
LIVE_STALE_SECS = 1200

# Matches the launch tool_result the harness injects when Agent is run in
# the background: "Async agent launched successfully.\nagentId: <hex>".
_LAUNCH_RE = re.compile(
    r"Async agent launched successfully\.\s*\n?\s*agentId:\s*([0-9a-fA-F]+)"
)
_TASKID_RE = re.compile(r"<task-id>([0-9a-fA-F]+)</task-id>")
# Matches a SendMessage tool_result that resumed a completed agent.  The
# harness returns a JSON body (embedded inside the outer transcript JSON, so
# quotes are backslash-escaped):
#   {"success":true,"message":"Agent \"<hex-id>\" had no active task;
#    resumed from transcript in the background ..."}
# _user_text() returns the raw JSON string as-is (no inner parse), so the
# agent id is surrounded by escaped quotes (\").  The pattern allows both
# raw " and escaped \" so it matches whether or not the caller has
# pre-parsed the inner JSON.
# This resume counts as a new launch (DGN-540 Case A): the agent is back
# in flight and must appear in the [진행 중] section.
_RESUME_RE = re.compile(
    r'Agent\s+\\?"([0-9a-fA-F]+)\\?"\s+had no active task.*resumed from transcript'
)


# ---------------------------------------------------------------------------
# Session ownership guard (DGN-214 grill FATAL-1)
# ---------------------------------------------------------------------------
# The Stop hook fires in EVERY Claude session under this project config:
# headless crons (consolidate / queue-populator / autonomous-loop), junior
# worktree sessions (their settings carry absolute hook paths back into this
# repo), subagent contexts (Stop is auto-promoted to SubagentStop), and any
# terminal session.  Only the bridge's live owner session may emit the footer
# -- one writer per shared surface (DGN-198 doctrine).  The bridge persists
# the owner session id in .telegram_bot/sessions.json via _save_session_id;
# match against it and no-op everywhere else.  Fail-closed: unreadable state
# -> no footer (a missing footer is safer than a wrong-session footer).
# Benign residual: the first turn of a brand-new session mismatches until the
# bridge saves the new sid; the next turn catches up.

SESSIONS_FILE = ".telegram_bot/sessions.json"

# Sidecar file: hook writes the canonical footer here; bridge reads it at
# finalize time and appends it to the outgoing content (DGN-531 sole-author
# model).  Atomic write: tmp + os.replace.  Bridge clears the file after
# consuming it so stale footers never bleed across turns.
FOOTER_SIDECAR = ".telegram_bot/footer-sidecar.json"


def _is_owner_session(transcript_path):
    try:
        if not transcript_path:
            return False
        sess = os.path.basename(transcript_path)
        if sess.endswith(".jsonl"):
            sess = sess[:-6]
        sessions_path = (
            os.environ.get("DOGANY_SESSIONS_FILE")
            or os.path.join(_ROOT_DIR, SESSIONS_FILE)
        )
        with open(sessions_path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return False
        for val in data.values():
            if isinstance(val, dict) and val.get("session_id") == sess:
                return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session directory derivation (only needed for meta descriptions)
# ---------------------------------------------------------------------------

def _subagents_dir(transcript_path):
    """Return ~/.claude/projects/<enc>/<sess>/subagents or None."""
    try:
        if not transcript_path:
            return None
        enc = os.path.basename(os.path.dirname(transcript_path))
        sess_file = os.path.basename(transcript_path)
        sess = sess_file[:-6] if sess_file.endswith(".jsonl") else sess_file
        home = os.path.expanduser("~")
        return os.path.join(home, ".claude", "projects", enc, sess, "subagents")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Transcript parsing: active subagents
# ---------------------------------------------------------------------------

def _user_text(entry):
    """Flatten the text/tool_result content of a type=user transcript entry."""
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text") or "")
        elif btype == "tool_result":
            tc = block.get("content")
            if isinstance(tc, str):
                parts.append(tc)
            elif isinstance(tc, list):
                for x in tc:
                    if isinstance(x, dict):
                        parts.append(x.get("text") or "")
    return "\n".join(parts)


def _jsonl_fresh(agent_id, subagents_dir):
    """True if the subagent's jsonl was written within LIVE_STALE_SECS.

    Conservative: missing dir / missing file / unreadable mtime -> False, so
    an agent is only ever shown active with positive freshness evidence.
    """
    try:
        if not subagents_dir or not agent_id:
            return False
        p = os.path.join(subagents_dir, "agent-%s.jsonl" % agent_id)
        if not os.path.isfile(p):
            return False
        return (time.time() - os.path.getmtime(p)) < LIVE_STALE_SECS
    except Exception:
        return False


def _sendmessage_recap(entry):
    """Return (to_agent_id, summary) for a SendMessage tool_use in an assistant
    entry, else (None, None).

    `summary` is the 5-10 word recap the harness asks the caller to write on
    every SendMessage, so it reflects the LATEST instruction to the agent --
    unlike meta.json's spawn-frozen description (DGN-211).
    """
    try:
        msg = entry.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return None, None
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "SendMessage":
                inp = block.get("input") or {}
                to_id = (inp.get("to") or "").strip()
                summary = (inp.get("summary") or "").strip()
                if to_id and summary:
                    return to_id, summary
    except Exception:
        pass
    return None, None


def _scan_transcript(transcript_path):
    """Stream the MAIN transcript once and return three agentId-keyed dicts:
      last_launch  -> line idx of the most recent launch OR resume
      last_compl   -> line idx of the most recent completion notice
      last_summary -> (line idx, summary) of the most recent SendMessage recap

    Launch events (type=user tool_result): "Async agent launched successfully."
    Resume events (type=user tool_result): the SendMessage tool returns a JSON
      body {"message": "Agent \"<id>\" had no active task; resumed from
      transcript in the background ..."}.  A resume means the agent is back in
      flight; it is treated as a new launch to flip last_launch[id] past any
      preceding completion (DGN-540 Case A).

    The SendMessage recap (DGN-211) tracks a mid-flight redirect that the
    spawn-frozen meta.json description cannot.  Memory-bounded: only small
    dicts are kept, regardless of transcript size.

    I/O errors PROPAGATE to the caller (DGN-541 fail-open empty-ban): a
    swallowed read failure would masquerade as "no agents" and escalate into
    a false-empty dashboard write.  Per-line parse errors are still skipped.
    """
    last_launch = {}   # agentId -> line index of most recent launch or resume
    last_compl = {}    # agentId -> line index of most recent completion
    last_summary = {}  # agentId -> (line index, summary) of most recent recap
    if not transcript_path or not os.path.isfile(transcript_path):
        return last_launch, last_compl, last_summary
    with open(transcript_path, "r", errors="replace") as fh:
            for idx, raw in enumerate(fh):
                raw = raw.strip()
                if not raw:
                    continue
                # Cheap pre-filter before JSON parse: launch/completion ride on
                # type=user events; the SendMessage recap rides on an assistant
                # tool_use, so also admit any line mentioning SendMessage.
                # Resume results contain "resumed from transcript" inside a
                # tool_result of a type=user line, so the is_user gate catches
                # them without a separate filter token.
                is_user = '"type":"user"' in raw or '"type": "user"' in raw
                has_send = "SendMessage" in raw
                if not is_user and not has_send:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype == "user":
                    txt = _user_text(entry)
                    if not txt:
                        continue
                    # Standard background-agent launch.
                    m = _LAUNCH_RE.search(txt)
                    if m:
                        last_launch[m.group(1)] = idx
                    # SendMessage-resume: agent had completed but was resumed
                    # via SendMessage; harness returns a success JSON body.
                    # Treat as a new launch so last_launch[id] > last_compl[id]
                    # (DGN-540 Case A).
                    r = _RESUME_RE.search(txt)
                    if r:
                        last_launch[r.group(1)] = idx
                    if "<task-notification>" in txt and (
                        "<status>completed</status>" in txt or "came to rest" in txt
                    ):
                        for tid in _TASKID_RE.findall(txt):
                            last_compl[tid] = idx
                elif etype == "assistant":
                    to_id, summary = _sendmessage_recap(entry)
                    if to_id and summary:
                        last_summary[to_id] = (idx, summary)
    return last_launch, last_compl, last_summary


def _resolve_description(agent_id, launch_idx, summary, subagents_dir):
    """Live label for an active agent.

    Prefer the latest SendMessage recap when it post-dates the launch: it
    reflects a mid-flight redirect (DGN-211).  A recap that predates the
    current launch is ignored.  Fall back to the spawn meta.json description,
    then the short id.
    """
    if summary and summary[0] > launch_idx:
        text = (summary[1] or "").strip()
        if text:
            return text
    return _agent_description(agent_id, subagents_dir)


def _agent_description(agent_id, subagents_dir):
    """Return a human-readable description for an agentId.

    Prefers subagents_dir/agent-<id>.meta.json's "description" field; falls
    back to the short agent id.
    """
    try:
        if subagents_dir:
            meta_path = os.path.join(subagents_dir, "agent-%s.meta.json" % agent_id)
            if os.path.isfile(meta_path):
                with open(meta_path) as fh:
                    meta = json.load(fh)
                desc = (meta.get("description") or "").strip()
                if desc:
                    return desc
    except Exception:
        pass
    return agent_id[:8] if agent_id else "unknown"


def _collect_active_subagents(transcript_path):
    """Return ordered list of description strings for active subagents.

    Active iff the most recent launch/resume post-dates the most recent
    completion AND the subagent jsonl is still fresh (see LIVE_STALE_SECS).
    LIVE_STALE_SECS is now 1200s (20 min) to survive long tool-call silence
    (DGN-540 Case B); the transcript completion-event detection guards against
    ghost agents from missed completions.  The label prefers a post-launch
    SendMessage recap over the spawn-frozen meta.json description (DGN-211).

    Returns None on FAILURE (missing/unreadable transcript): None means
    "cannot confirm empty", so the caller must never treat it as a genuinely
    empty board (DGN-541 fail-open empty-ban).  An empty list [] means
    CONFIRMED no active subagents.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    try:
        subagents_dir = _subagents_dir(transcript_path)
        last_launch, last_compl, last_summary = _scan_transcript(transcript_path)
        active = []
        for aid, launch_idx in last_launch.items():
            if launch_idx <= last_compl.get(aid, -1):
                continue      # a completion notice was seen after the launch
            if not _jsonl_fresh(aid, subagents_dir):
                continue      # no recent jsonl write -> finished (or unverifiable)
            active.append(aid)
        descs = []
        for aid in sorted(active):
            descs.append(_resolve_description(
                aid, last_launch[aid], last_summary.get(aid), subagents_dir))
        return descs
    except Exception:
        return None


def _collect_ledger_running():
    """Return descriptions of junior tasks in 'running' state from the ledger.

    Reads product/auto-loop-ledger.md and returns one label per row where
    state == 'running'.  These are juniors spawned by the auto-loop as
    separate claude processes -- they never appear in the owner session
    transcript, so transcript-only detection leaves the dashboard blind to
    them (DGN-540).  Instances without an auto-loop fleet have no ledger
    file at all; a MISSING file therefore means CONFIRMED no juniors -> [].
    Any other read failure returns None ("cannot confirm empty", DGN-541
    fail-open empty-ban).  Env override mirrors DOGANY_DECISIONS_FILE
    (test isolation).
    """
    path = os.environ.get("DOGANY_LEDGER_FILE") or os.path.join(
        _ROOT_DIR, LEDGER_FILE)
    items = []
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("|"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                # | branch | item | state | attempts | backoff_until | last_ts | flags | note |
                # split gives: ['', branch, item, state, ...]
                if len(parts) < 5:
                    continue
                state = parts[3]
                if state != "running":
                    continue
                item = parts[2]
                branch = parts[1]
                desc = item if (item and item != "-") else branch
                if desc and desc != "-":
                    items.append("junior: " + desc)
    except FileNotFoundError:
        return []
    except Exception:
        return None
    return items


# ---------------------------------------------------------------------------
# Pending decisions (worklog/_DECISIONS.md, DGN-214)
# ---------------------------------------------------------------------------

# Ledger line: "- [YYYY-MM-DD] [dec-NNN] <summary>".  The [dec-NNN] id is
# optional for backward compatibility but standard going forward (DGN-215:
# stable ids make console deep links immune to ledger-line churn).
_DECISION_RE = re.compile(
    r"^-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(?:\[(dec-\d+)\]\s*)?(.+\S)\s*$")


def _collect_decisions():
    """Return the list of pending-decision summaries from _DECISIONS.md.

    Only lines of the form "- [YYYY-MM-DD] <summary>" count; everything else
    (header comments, blanks) is ignored.  A MISSING file means CONFIRMED no
    pending decisions -> [].  Any other read failure returns None ("cannot
    confirm empty", DGN-541 fail-open empty-ban).

    When a decision line carries a dec id, append a console deep link so the
    item reads: "<dec-id> <summary> -> <CONSOLE_BASE>/decision/<dec-id>".
    Items without a dec id are returned unchanged.
    """
    path = os.environ.get("DOGANY_DECISIONS_FILE") or os.path.join(
        _ROOT_DIR, DECISIONS_FILE)
    items = []
    try:
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                m = _DECISION_RE.match(raw.strip())
                if not m:
                    continue
                dec_id, summary = m.group(2), m.group(3)
                if dec_id and CONSOLE_BASE:
                    link = "%s/decision/%s" % (CONSOLE_BASE, dec_id)
                    item = "%s %s -> %s" % (dec_id, summary, link)
                elif dec_id:
                    # No console on this instance: keep the stable id, no link.
                    item = "%s %s" % (dec_id, summary)
                else:
                    item = summary
                items.append(item)
    except FileNotFoundError:
        return []
    except Exception:
        return None
    return items


# ---------------------------------------------------------------------------
# Live dashboard generation (DGN-214 work B, spec item 2)
# ---------------------------------------------------------------------------

DASHBOARD_FILE = "dashboard.md"
MAX_DONE_DISPLAY = 2  # recent completed tickets shown on the dashboard (Rev 8: owner spec, was 5)
# Telegram's 4096 message limit counts UTF-16 code units; the generator owns
# the section-aware length cut (결정대기 preserved first), the bridge keeps
# only a dumb tail-cut safeguard.  Stay under the bridge's 3900 margin.
DASHBOARD_MAX_UNITS = 3800
# Per-decision-item cap (UTF-16 units).  Decision items are never dropped by
# the section cut (only trimmed from the tail when >1), so a single runaway
# line could otherwise exceed DASHBOARD_MAX_UNITS and push the bridge into
# its dumb tail-cut, chopping the last-line freshness stamp.  Decisions are
# one-line ledger summaries; 300 units is generous.
DASHBOARD_ITEM_MAX_UNITS = 300

# Ticket frontmatter line: "key: value" between the two '---' fences.
_FRONTMATTER_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*\S)?\s*$")


def _bot_data_dir():
    """BOT_DATA_DIR = <workspace_root>/.telegram_bot, same derivation as the
    bridge (config.py: PROJECT_ROOT / ".telegram_bot").  Env override mirrors
    the DOGANY_DECISIONS_FILE test convention."""
    return os.environ.get("DOGANY_BOT_DATA_DIR") or os.path.join(
        _ROOT_DIR, ".telegram_bot")


def _u16len(s):
    """Length in UTF-16 code units (Telegram's counting unit)."""
    return len(s.encode("utf-16-le")) // 2


def _cap_item(s, limit=DASHBOARD_ITEM_MAX_UNITS):
    """Cap one dashboard item to `limit` UTF-16 units (ellipsis-terminated).

    If the item contains a link (detects " -> http"), cap the prefix portion
    before the link and append the full intact link; otherwise cap the full
    string.  Cutting on a raw UTF-16 boundary may split a surrogate pair;
    decode with errors="ignore" drops the orphan half.
    """
    if _u16len(s) <= limit:
        return s

    # Check if this item has a console link (format: "... -> http://...").
    link_match = re.search(r'\s*->\s*(https?://\S+)$', s)
    if link_match:
        link = link_match.group(1)
        link_units = _u16len(link)
        prefix = s[:link_match.start()]

        # Reserve space for " -> " (4 units on ASCII), link, and ellipsis (1 unit).
        available = limit - link_units - 5
        if available > 0:
            prefix_units = _u16len(prefix)
            if prefix_units > available:
                # Truncate prefix to fit.
                encoded = prefix.encode("utf-16-le")[: available * 2]
                prefix = encoded.decode("utf-16-le", errors="ignore")
            return prefix + " -> " + link
        else:
            # Link alone exceeds limit; cap the link itself and return it.
            encoded = link.encode("utf-16-le")[: (limit - 1) * 2]
            return encoded.decode("utf-16-le", errors="ignore") + "…"

    # No link detected; cap the full string as before.
    encoded = s.encode("utf-16-le")[: (limit - 1) * 2]
    return encoded.decode("utf-16-le", errors="ignore") + "…"


def _ticket_frontmatter(path):
    """Parse the frontmatter of one worklog ticket into a small dict.

    Reads at most the fenced frontmatter block ('---' ... '---') and only the
    keys we need.  Anything malformed -> {} (ticket simply not shown).
    """
    fm = {}
    try:
        with open(path, "r", errors="replace") as fh:
            first = fh.readline().strip()
            if first != "---":
                return {}
            for _ in range(20):  # frontmatter is short; hard bound the scan
                line = fh.readline()
                if not line:
                    return {}
                line = line.strip()
                if line == "---":
                    return fm
                m = _FRONTMATTER_KV_RE.match(line)
                if m:
                    fm[m.group(1)] = (m.group(2) or "").strip()
    except Exception:
        return {}
    return {}


def _collect_recent_done(limit=MAX_DONE_DISPLAY):
    """Recent completed work = worklog tickets with status done, newest first.

    The footer has no "recent completed" source of its own, so the dashboard
    reads the worklog frontmatter (id / title / status / updated) and sorts
    done tickets by updated date descending, ticket id as tiebreaker.
    Missing/unreadable worklog -> [] (section renders empty, fail-open).
    """
    worklog_dir = os.path.join(_ROOT_DIR, "worklog")
    rows = []
    try:
        names = os.listdir(worklog_dir)
    except OSError:
        return []
    for name in names:
        if not (name.startswith("DGN-") and name.endswith(".md")):
            continue
        fm = _ticket_frontmatter(os.path.join(worklog_dir, name))
        if fm.get("status") != "done":
            continue
        tid = fm.get("id") or name[:-3]
        rows.append((fm.get("updated") or "", tid, fm.get("title") or ""))
    rows.sort(reverse=True)
    out = []
    for updated, tid, title in rows[:limit]:
        line = "%s %s" % (tid, title) if title else tid
        if updated:
            line += " (%s)" % updated
        out.append(line)
    return out


def _build_dashboard(decisions, active_agents, recent_done):
    """Assemble the dashboard.md content string.

    Header: BOARD_TITLE line -- fixed name "작업대" (owner naming 2026-07-24;
    renamed from 상황판) with an optional per-instance emoji prefix (Rev 9).
    Sections: 결정대기 / <live label> / 최근 완료.  The live section title
    comes from LIVE_LABEL (DGN-541 S2/S3: short form, per-instance via
    config/env, neutral default).
    Last line is the freshness
    stamp "갱신 HH:MM" -- written by the GENERATOR only; the bridge never
    touches it, so a dead generator shows up as a visibly stale timestamp.
    Length cut: drop tail items lowest-priority-first (최근 완료 -> 진행
    갈래 -> 결정대기) until the text fits DASHBOARD_MAX_UNITS.
    """
    # Cap every decision item: the section cut never drops the last decision,
    # so an uncapped runaway line would break the 3800-unit contract and let
    # the bridge tail-cut chop the freshness stamp.
    decisions = [_cap_item(d) for d in decisions]
    live = list(active_agents)
    done = list(recent_done)[:MAX_DONE_DISPLAY]
    stamp = "갱신 " + time.strftime("%H:%M")

    def _sec(title, items):
        # Rev 8 (owner spec 2026-07-24): omit an empty section entirely
        # (no "- 없음" placeholder). The board only renders when 결정대기 or
        # live has content, so a blank stub was pure noise.
        if not items:
            return []
        return [title] + ["- " + x for x in items]

    def _render():
        lines = [BOARD_TITLE, ""]
        for block in (_sec("[결정대기]", decisions),
                      _sec("[%s]" % LIVE_LABEL, live),
                      _sec("[최근 완료]", done)):
            if block:
                lines += block
                lines.append("")
        lines.append(stamp)
        return "\n".join(lines) + "\n"

    text = _render()
    while _u16len(text) > DASHBOARD_MAX_UNITS:
        if done:
            done.pop()
        elif live:
            live.pop()
        elif len(decisions) > 1:
            decisions.pop()
        else:
            # Single decision left; per-item cap guarantees it fits, so this
            # break is unreachable-over-budget in practice (kept as a
            # termination guard).
            break
        text = _render()
    return text


def _write_dashboard_text(text):
    """Write text to <BOT_DATA_DIR>/dashboard.md atomically (tmp + os.replace,
    heartbeat.touch precedent).  An EMPTY string is the S1 hide signal: the
    bridge's delete state machine takes the pinned message down (DGN-541)."""
    data_dir = _bot_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, DASHBOARD_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write_dashboard(active_agents, decisions):
    """Regenerate <BOT_DATA_DIR>/dashboard.md.  Caller has already passed the
    ownership gate -- this function does no gating of its own."""
    _write_dashboard_text(
        _build_dashboard(decisions, active_agents, _collect_recent_done()))


# ---------------------------------------------------------------------------
# Footer sidecar write (DGN-531 sole-author model)
# ---------------------------------------------------------------------------

def _write_footer_sidecar(footer):
    """Write (or clear) the footer sidecar file atomically.

    When footer is a non-empty string: write
      {"footer": "<footer text>", "ts": <epoch>}
    so the bridge can append it to the outgoing Telegram content.

    When footer is empty/None (noise-suppression path): write
      {"footer": ""}
    so the bridge knows there is nothing to append this turn.

    Atomic write (tmp + os.replace) mirrors _write_dashboard.  Fail-silent:
    any exception leaves the old sidecar in place; the bridge falls back to
    whatever is there (or nothing if absent).
    """
    try:
        data_dir = _bot_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, os.path.basename(FOOTER_SIDECAR))
        tmp = path + ".tmp"
        payload = {"footer": footer or "", "ts": int(time.time())}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# NO_PUSH sentinel turn detection
# ---------------------------------------------------------------------------

def _turn_is_no_push(transcript_path):
    """True when the turn's last assistant text is the bare NO_PUSH sentinel.

    DGN-217/226: injected background turns end in bare NO_PUSH so the bridge
    suppresses the owner push.  Demanding a footer on such a turn appends
    text AFTER the sentinel, the bridge equality match fails, and the raw
    NO_PUSH body leaks to the owner chat (observed 2026-07-09).  Suppressed
    turns get no footer.

    DGN-234: a turn may also carry a report body and END with the sentinel
    line.  That is still a suppressed turn -- appending a footer would make
    the footer the last line and break the bridge's trailing-line match, so
    treat a trailing NO_PUSH line as a sentinel turn too.
    """
    try:
        if not transcript_path or not os.path.isfile(transcript_path):
            return False
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = min(size, 131072)
            fh.seek(max(0, size - chunk))
            tail = fh.read().decode("utf-8", errors="replace")

        lines = [l.strip() for l in tail.splitlines() if l.strip()]
        if size > chunk and lines:
            lines = lines[1:]  # first line of a mid-file window is partial
        for raw in reversed(lines):
            try:
                entry = json.loads(raw)
            except Exception:
                continue
            if entry.get("type") != "assistant":
                continue
            msg = entry.get("message") or {}
            content = msg.get("content") or []
            parts = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block.get("text") or "")
                    elif isinstance(block, str):
                        parts.append(block)
            text = "".join(parts).strip()
            if text == "NO_PUSH":
                return True
            tail_lines = [l.strip() for l in text.splitlines() if l.strip()]
            return bool(tail_lines) and tail_lines[-1] == "NO_PUSH"
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Duplicate footer detection
# ---------------------------------------------------------------------------

def _all_tool_results(content):
    """True when every block in a content list is a tool_result.

    Used to distinguish tool-round user entries (mid-turn intermediaries,
    should be skipped) from genuine user turn boundaries.  Agent launch
    notifications also arrive as tool_result content and are likewise skipped.
    """
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _collect_current_turn_text(transcript_path):
    """Return concatenated assistant-text for all entries in the current turn.

    Scans backward from the transcript tail through all assistant entries in
    the current turn, accumulates their text blocks, and returns them joined
    in chronological order.  Stops at the first genuine user-text entry (turn
    boundary); tool-result-only user entries are skipped (mid-turn).

    Uses a 128KB tail window (same as the old _footer_already_present) so
    that long assistant entries are not truncated mid-JSON.  Preserves the
    87ac225 tool-round scan behaviour: footers appended before a tool-call
    round are still included in the returned text.

    Returns "" on any error (fail-open).
    """
    parts_rev = []
    try:
        if not transcript_path or not os.path.isfile(transcript_path):
            return ""
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = min(size, 131072)
            fh.seek(max(0, size - chunk))
            tail = fh.read().decode("utf-8", errors="replace")

        lines = [l.strip() for l in tail.splitlines() if l.strip()]
        if size > chunk and lines:
            lines = lines[1:]  # first line of a mid-file window is partial
        for raw in reversed(lines):
            try:
                entry = json.loads(raw)
            except Exception:
                continue
            etype = entry.get("type")
            if etype == "user":
                msg = entry.get("message") or {}
                content = msg.get("content")
                if _all_tool_results(content):
                    continue  # mid-turn tool result; keep scanning
                break  # genuine user-text turn boundary
            if etype != "assistant":
                continue
            msg = entry.get("message") or {}
            content = msg.get("content") or []
            text_parts = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text") or "")
                    elif isinstance(block, str):
                        text_parts.append(block)
            parts_rev.append("".join(text_parts))
    except Exception:
        return ""
    return "\n".join(reversed(parts_rev))


def _ends_with_canonical_footer_once(text, footer):
    """True iff text ends with footer exactly once (spec case b).

    Both sides are stripped of trailing newlines before comparison.

    The check answers: "is the canonical footer already correctly in place?"
    It returns True only when:
      1. text ends with footer (endswith).
      2. No duplicate of the SAME footer exists earlier in the text:
         if FOOTER_MARKER appears in the 'before' portion, the block
         starting at that position must NOT equal the current footer.
         A different-content footer in 'before' is acceptable -- it is a
         stale residue from an earlier block-cycle assistant entry in the
         same turn (DGN-412 root cause: the old code returned False for
         this case, causing an infinite re-block loop).

    A duplicate is defined as the same footer content appearing at another
    FOOTER_MARKER position before the tail.  A stale/different-content
    footer at an earlier position is NOT a duplicate and does not fail
    the check.

    Returns False when footer is empty or text does not end with footer.
    """
    t = text.rstrip("\n")
    f = footer.rstrip("\n")
    if not f:
        return False
    if not t.endswith(f):
        return False
    footer_start = len(t) - len(f)
    before = t[:footer_start]
    # Fast path: no FOOTER_MARKER in 'before' -> canonical single footer.
    if FOOTER_MARKER not in before and DECISION_MARKER not in before:
        return True
    # FOOTER_MARKER or DECISION_MARKER present in 'before'.
    # Check whether ANY occurrence of f in 'before' is a self-contained block
    # (not a prefix of a larger footer block).  A true duplicate means the
    # tail footer is redundant -> return False.  A stale/different-content
    # block at an earlier position (e.g. from a prior block-cycle entry in the
    # same turn) is NOT a duplicate and we should return True.
    #
    # "Self-contained" means nothing that extends the footer block follows
    # immediately after f at that position: neither an extra bullet ("\n- ")
    # nor an extra section marker ("\n[라이브]" / "\n[결정대기]").  If any of
    # those continuation tokens follow, f is a prefix of a larger footer block
    # and is not an isolated duplicate.
    _CONTINUATION = ("\n- ", "\n" + FOOTER_MARKER, "\n" + DECISION_MARKER)
    search_pos = 0
    while True:
        idx = before.find(f, search_pos)
        if idx == -1:
            break
        remainder = before[idx + len(f):]
        # If remainder is empty (f ends exactly at before boundary) or starts
        # with a non-continuation token, this occurrence is an isolated block
        # -> true duplicate of the canonical footer.
        if not remainder or not any(remainder.startswith(c) for c in _CONTINUATION):
            return False
        search_pos = idx + 1
    return True


# ---------------------------------------------------------------------------
# Footer assembly
# ---------------------------------------------------------------------------

def _build_footer(active_agents, decisions):
    """Assemble the itemized multi-line footer string.

    Produces a multi-section block:
      [라이브]
      - <desc 1>
      - <desc 2>
      [결정대기]
      - dec-NNN <summary> -> <link>
      - dec-NNN ...

    [라이브] = running background subagents, capped at MAX_LIVE_DISPLAY with a
    "+ N개" overflow bullet. When no active agents but decisions exist, show
    "- 없음" under the [라이브] header (FOOTER_MARKER must remain the FIRST line
    for dedup detection).

    [결정대기] = the full set of decisions awaiting the user, capped at
    MAX_DECISION_DISPLAY with a "+ N건" overflow bullet. Omitted entirely when
    no decisions (user directive 2026-07-08: no-pending needs no section).

    Returns multi-line string with sections but NO blank line between them.
    """
    lines = []

    # [라이브] section: always present (header + bullets or "없음")
    lines.append("[라이브]")
    if active_agents:
        shown = active_agents[:MAX_LIVE_DISPLAY]
        for desc in shown:
            lines.append("- " + desc)
        extra = len(active_agents) - len(shown)
        if extra > 0:
            lines.append("- 외 %d개" % extra)
    else:
        # Only show "없음" if decisions exist; otherwise the footer IS empty.
        if decisions:
            lines.append("- 없음")

    # [결정대기] section: only if decisions exist
    if decisions:
        lines.append("[결정대기]")
        shown = decisions[:MAX_DECISION_DISPLAY]
        for item in shown:
            lines.append("- " + item)
        extra = len(decisions) - len(shown)
        if extra > 0:
            lines.append("- 외 %d건" % extra)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        try:
            data = json.load(sys.stdin)
        except Exception:
            sys.exit(0)  # fail open: bad stdin -> no-op

        if not isinstance(data, dict):
            sys.exit(0)

        transcript_path = data.get("transcript_path") or ""

        # Session ownership: computed once, gates BOTH the dashboard write
        # and the footer (crons / juniors / subagents / terminals no-op).
        is_owner = _is_owner_session(transcript_path)

        # Dashboard regeneration (DGN-214 spec 2b): runs unconditionally on
        # every owner Stop so the pinned dashboard content never lags.  Sole
        # gate = ownership (spec 2a, fail-closed).  Never blocks the sidecar
        # write path.
        if is_owner:
            # Active subagents: transcript-based (Agent tool launches from this
            # session) merged with ledger-running juniors (spawned by the
            # auto-loop as separate claude processes -- not in this transcript).
            # DGN-540: ledger source fills the blind spot.
            # Each collector returns None on FAILURE and a list on success;
            # an empty list means CONFIRMED empty (DGN-541 fail-open empty-ban).
            transcript_agents = _collect_active_subagents(transcript_path)
            ledger_agents = _collect_ledger_running()
            raw_decisions = _collect_decisions()
            collect_ok = (
                transcript_agents is not None
                and ledger_agents is not None
                and raw_decisions is not None
            )
            active_agents = (transcript_agents or []) + (ledger_agents or [])
            decisions = raw_decisions or []
            try:
                if active_agents or decisions:
                    # Display trigger (DGN-541 S1): >=1 pending decision OR
                    # >=1 working subagent -> fill the board.  Recent-completed
                    # history is extra info only, never a standalone trigger
                    # (D2).
                    _write_dashboard(active_agents, decisions)
                elif collect_ok:
                    # CONFIRMED empty board -> empty write; the bridge's
                    # debounced delete state machine unpins the message.
                    _write_dashboard_text("")
                # else: a collector failed with nothing to show -> do NOT
                # write empty (a false-empty would escalate into a visible
                # pin delete); the previous dashboard.md content stays.
            except Exception:
                pass

        # Guard 1: only the bridge's live owner session writes the sidecar.
        if not is_owner:
            sys.exit(0)

        # Guard 2: bare NO_PUSH sentinel turn -- bridge suppresses this push;
        # do not write a sidecar that would append text after the sentinel.
        if _turn_is_no_push(transcript_path):
            sys.exit(0)

        # DGN-453 direction A: footer is always suppressed.
        # The pinned dashboard.md is the sole surface for [결정대기]/[라이브].
        # The hook still regenerates dashboard.md (freshness gate is above),
        # but writes an empty sidecar for all owner turns (no message-level footer).
        # This eliminates message-stack noise while keeping live status visible
        # at the top of the chat (dashboard persistence).
        _write_footer_sidecar("")
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
