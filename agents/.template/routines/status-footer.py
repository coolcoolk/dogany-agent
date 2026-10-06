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
  env (DOGANY_CONSOLE_BASE / DOGANY_LIVE_LABEL / DOGANY_SIBLING_TERM /
       DOGANY_BOARD_EMOJI / DOGANY_LIVE_SHOW_MODEL)
  -> config/agent.conf (DASHBOARD_CONSOLE_BASE / DASHBOARD_LIVE_LABEL /
     DASHBOARD_SIBLING_TERM / DASHBOARD_EMOJI / DASHBOARD_LIVE_SHOW_MODEL)
  -> neutral default.
The env layer keeps hook-command overrides and test isolation working; the
conf layer survives framework updates (the .claude/settings.json Stop-hook
registration is framework-owned and carries no per-instance env).

Rev 10 (DGN-534 T3): the dashboard gains a [언파크 후보] section -- a
read-only DERIVED view of the unpark-candidate ledger worklog/_UNPARK.md
(sole writer = the ticket-hygiene gate scan, M4).  Canonical no-op:
instances WITHOUT the ledger file (no gate-scan machinery, e.g. a main agent) render
zero change.  When the ledger exists, the section header carries the
last-scan stamp; Rev 8 empty-section suppression applies only while the
signal is healthy (0 candidates AND fresh scan) -- a stale (>26h) or
unreadable ledger FORCES the header with a staleness flag so a
silently-stopped scan stays visible (M5 silent-stop detector).  The length
cut drops unpark items after 최근 완료 but before the live section, and
never drops the header.  The FOOTER is unchanged -- unpark candidates ride
the pinned dashboard only.

Rev 11 (DGN-536 T3b): the dashboard gains a [콘솔액션] section -- a
read-only DERIVED view of the console decision-action journal
product/decision-actions.md (sole writer = the console, DGN-536 design).
Canonical no-op: instances WITHOUT the journal file (every instance except
the dev leader today) render zero change -- byte-identical to Rev 10
behaviour.  When the journal exists, pending (non-terminal) acts are
rendered as compact one-liners: dec id, verb, age.  A zero-pending journal
suppresses the section (Rev 8 empty-section suppression).  failed/conflict
acts force the header visible even when pending count is zero (M5 analog:
a silently-stuck failed act must not become invisible).  The section is
ordered BETWEEN [결정대기] and [언파크 후보] in the drop-priority chain;
its items are dropped before live agents but after 언파크 items.

Rev 12 (DGN-967, shared pin surface -- section ownership): dashboard.md is
no longer this hook's sole property.  The SAME file is the framework's only
pin surface (DGN-214), so domain agents (e.g. a live workout card) must be
able to share it -- and the Rev 7 confirmed-empty write ("" = bridge HIDE
signal) was deleting THEIR content on every owner Stop.  dashboard.md is now
a DERIVED artifact composed from a section store
(<BOT_DATA_DIR>/dashboard-sections.json): each owner slug holds exactly one
section; the workbench board writes only the reserved "board" section; the
composer (sole physical writer of dashboard.md) joins all non-empty sections
-- domain sections first (owner-slug order), board LAST (superseded by
Rev 16 below: the board now leads).  The bridge is untouched: an
empty COMPOSED output (ALL sections empty) still writes "" and still drives
the debounced unpin+delete; any non-empty section keeps the pin alive.
Owner API (file/CLI/exit-code contract):
    status-footer.py --pin-set <owner> [--file PATH] [--ttl SECS]
                     [--fresh [--wait SECS]]
    status-footer.py --pin-clear <owner>
--fresh (DGN-1768 r2): after the section write, drop the rotate-request
marker <BOT_DATA_DIR>/dashboard_rotate.request (sibling of the bridge's
dashboard_state.json); the bridge's next tick sends the board as a NEW
silent pinned message, deletes the previous one, and removes the marker.
--wait SECS blocks until the marker is consumed: exit 0 when consumed, exit
2 on timeout (the marker is withdrawn, so no late rotation surprises a
producer that already took its fallback path).  Conf DASHBOARD_ROTATE
(bridge side): auto (default: also rotate on the first real change of a new
day) | explicit (rotate ONLY on --fresh; every other change edits).
Foreign DIRECT writes to dashboard.md (a domain agent that never adopts the
API) are detected at compose time via a sha mismatch against the last
composed text and adopted as the reserved "legacy" section (24h TTL backstop
so a dead direct writer cannot pin stale content forever).  All store
mutations run under an flock'd read-modify-write; the Stop-hook path stays
fail-open (lock miss -> skip the write, previous content preserved).

Rev 13 (DGN-1015, silent-death visibility): a launched-but-never-completed
agent whose own jsonl goes stale (age >= LIVE_STALE_SECS) used to be dropped
from _collect_active_subagents SILENTLY -- identical on screen to an agent
that finished cleanly. Root cause of the 2026-08-22 09:33 incident (measured
via the killed agent's own jsonl + the leader's main transcript, both timestamping
"[Request interrupted by user]" with ZERO task-notification anywhere): an
in-session interrupt kills a background subagent's abort tree without ever
emitting a completion event, so the launch-without-completion state persists
until the jsonl ages past the ceiling -- and then, before this fix, the agent
just vanished with no record. Fixed: such an agent is no longer dropped: it
is returned with a distinct "(무응답 N분+ -- 사망 추정)" suffix so 0 live agents
and "was live, went silent" are never visually identical (ticket success
criterion 2/3). A jsonl that was NEVER observed at all (missing file, e.g. a
launch-vs-first-write race) stays silently dropped as before -- that case has
no positive evidence of life OR death, so flagging it dead would be a guess,
not a measurement.

Rev 14 (deliberate stop leaves the board): Rev 13 gave a launched-but-never-
completed agent exactly two fates -- live, or presumed-dead -- and
presumed-dead was an ABSORBING state. The only exit from the live section is a
completion event in the main transcript (last_compl[id] >= last_launch[id]),
and an agent that is killed on purpose never emits one; its jsonl simply
stops, so it stays stale forever and no amount of elapsed time releases it
(measured: a launch with a 30-day-old jsonl still renders with STALLED_LABEL).
Consequence measured on the owner's pinned board: an agent stopped
deliberately sat under "presumed dead" for hours and the owner asked about it
twice -- the alarm that is supposed to mark a real silent death was being
spent on a known, intentional stop.

Fixed by making a deliberate stop REMOVE the agent from the live section at
once: whoever performs the stop writes a stop record at stop time (the
subagent-stop hook / --agent-stopped CLI below), and a launch carrying a stop
record is excluded from the live section entirely -- not relabelled, not
dimmed, not held for a grace window.  Owner decision 2026-09-11, verbatim
reasoning: a stop is something the agent itself ordered, so the owner should
never have been looking at it; this board is HIS screen and it carries what he
needs to know -- what is running, and what actually died.  A deliberate stop
is the agent's own housekeeping and costs him attention for nothing, so any
retention window (30 minutes or 3) is just noise with a timer on it.  There is
therefore NO retention constant and NO stopped label: the state is
"not on the board", which needs neither.

The record itself is kept on disk -- this is about the owner's VIEW, not about
discarding data; the agent still has the stop history for its own bookkeeping.
No scanner and no sweeper is introduced: the record is a write-time artifact
of the stop itself, and the exclusion is a read-time filter inside the
dashboard pass that already runs.  A genuine death is untouched: with no stop
record the Rev 13 path applies verbatim, so the alarm still fires for every
unexplained silence.

Rev 15 (DGN-1020, one workbench for every agent -- owner decision 2026-09-26
"공통 기능은 정본으로"): the improvements two hand forks of this file had
grown separately (two dev-crew instances) come back here,
because every agent should have them; only dev-only sections stay outside.
  - Live rows carry the owner-locked row shape (2026-09-17)
      "<Model>: <name> · HH:MM (N분~)"
    for EVERY live source: subagents (model from the agent's own records,
    start = its spawn), dispatched/ledger rows (model from the note, start =
    the row's last_ts) and background jobs (unchanged).  The model prefix is
    omitted ONLY when no model is recorded; the time is "MM/DD HH:MM" when
    the start is not today.  LIVE_SHOW_MODEL now defaults ON (owner
    2026-09-26); "off" in conf/env remains the per-instance opt-out.
  - Event collection: a subagent completion that lands only as an
    attachment / queue-operation entry, and the "resumedAgentId" resume
    shape, are recognised (both carried from a dev fork).
  - The freshness stamp rides the title line: "<title> (갱신 HH:MM)" -- the
    first line can never be chopped by the bridge's tail-cut -- and the
    adoption rule recognises that stamped title as our own board.
  - Worklog tickets: one resolver (_iter_worklog_tickets / _ticket_id)
    honours TICKET_FILE_GLOB (the knob done-claim-gate already reads) and
    rejects a prose `id:` value; recent-done shows today's completions only.
  - [결정대기]: a decision whose worklog/decisions/<dec-id>.md carries a
    question / recommendation / options section renders as one expandable
    fold (bridge fold:: block); without a detail file it is the same bullet
    as before.  Bodies are the first thing the length cut drops.
  - Board sections plugin (the ONE extension seam, no more forks): an
    instance may carry routines/bundle/workbench_sections.py (delivered by
    a pack; conf DASHBOARD_BOARD_PLUGIN / env DOGANY_BOARD_PLUGIN override
    the path, "none" disables).  It contributes extra board sections from
    a context this hook already collected (see _plugin_ctx) and may hide
    base sections it supersedes.  Fail-open: a missing, invalid or raising
    plugin leaves the base board exactly as without one.  The plugin never
    writes -- this file keeps the DGN-967 compose and every write rule.

Rev 16 (DGN-1020, board FIRST in the composed pin -- owner 2026-09-26
15:14): the Rev 12 compose order is flipped -- the workbench board now
renders FIRST, domain sections (e.g. lifekit's "lifekit-day") AFTER it.
Rationale: the owner's pinned dashboard is read top-down like any other
message, and the board (what is running right now, what needs a decision)
is what he actually watches; a domain card he set up once and rarely
revisits does not belong ahead of it.  The Rev 12 reason for the OLD order
-- board last so its "갱신 HH:MM" freshness stamp is the file's last line
-- no longer holds: Rev 15 already moved that stamp onto the board's own
title line ("<title> (갱신 HH:MM)"), so the stamp survives regardless of
where the board sits in the file.  Nothing else about the compose model
changes: an empty board still yields domain-sections-only output with no
blank lead line, all-sections-empty is still "" (bridge HIDE signal),
domain sections keep owner-slug order among themselves, and exactly one
blank line still separates the board block from the domain block.
The one real consequence is the foreign-write adoption rule (DGN-967 spec
5): it used to strip "everything from the board's title line to EOF" and
treat the untouched remainder as domain -- safe only because board was
provably last.  With domain now last instead, adoption anchors the OTHER
end: `_is_own_domain_tail` recognises our own crash residue by checking
whether the text's TAIL is an exact render of the current domain sections
(whatever precedes that tail -- old board, new board -- is discarded
unseen, mirroring the old guarantee at the opposite end), and
`_is_bare_board_head` recognises a board-only file (old pre-DGN-967 style
or a fresh board with no domain sections yet) so it migrates to "nothing
to adopt" instead of duplicating itself as legacy.

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

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# flock serializes section-store read-modify-writes (DGN-967).  Windows has
# no fcntl; degrade to lock-free best-effort there (atomic os.replace writes
# keep individual files consistent either way).
try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

# Single conf parser (DGN-916): quote/whitespace normalization lives in
# routines/lib/conf_reader.py only.  Guarded import keeps the Stop-hook
# fail-open contract even if the lib file is ever missing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""
# Owner START/END notices for session background jobs (DGN-1691 phase 2).
# Guarded like conf_reader: without it the board still renders, the notices
# are simply not sent.
try:
    import bg_job_notice
except Exception:
    bg_job_notice = None

# Owner-facing model display (DGN-1227): one parser shared with
# dispatch-detached.sh, same guarded import.
try:
    from model_display import model_display as _shared_model_display
    from model_display import observe_from_stream as _observe_from_stream
except Exception:
    _shared_model_display = None
    _observe_from_stream = None

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
    """Read KEY=value from config/agent.conf via the single conf parser
    (routines/lib/conf_reader.py, DGN-916 -- quote/whitespace normalization
    lives there, not here).

    Instance display tokens resolve env -> config/agent.conf -> neutral
    default (Rev 9).  DOGANY_AGENT_CONF overrides the conf path (test
    isolation, DOGANY_DECISIONS_FILE convention).  Missing file / missing
    key -> "" (falls through to the neutral default).
    """
    path = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
        _ROOT_DIR, "config", "agent.conf")
    return conf_get(key, path)


# Console base for decision deep links.  Instance-specific: env
# DOGANY_CONSOLE_BASE -> conf DASHBOARD_CONSOLE_BASE -> empty.  Empty ->
# decision items render WITHOUT links (instances without a console degrade
# gracefully, DGN-541).
CONSOLE_BASE = (os.environ.get("DOGANY_CONSOLE_BASE")
                or _conf_get("DASHBOARD_CONSOLE_BASE") or "")
# Sibling TERM -- the noun naming what a background worker IS on this
# instance's owner surfaces.  DGN-541 shipped the override as a WHOLE PHRASE
# (DASHBOARD_LIVE_LABEL = "<term> 작업 중"), which only this one surface
# could use: when the dispatch start/end notices needed the same term, a
# phrase-shaped override could not feed them and the owner caught the two
# surfaces disagreeing on one screen (2026-09-18).  So the override unit
# moves DOWN to the term: one instance-supplied value feeds every
# `<term> 작업 ...` surface it still feeds (the dispatch start/end titles
# moved to background_term, the LIVE_LABEL noun: DGN-1767 / DGN-1769).  Same
# resolution order as every display token: env DOGANY_SIBLING_TERM -> conf
# DASHBOARD_SIBLING_TERM -> neutral default for general users.
SIBLING_TERM = (os.environ.get("DOGANY_SIBLING_TERM")
                or _conf_get("DASHBOARD_SIBLING_TERM") or "서브에이전트")
# Live-section label on the dashboard.  Instance-specific (DGN-541 S3).
# PRECEDENCE: the surface-specific phrase key, wherever it is set, beats the
# term-built default -- env DOGANY_LIVE_LABEL -> conf DASHBOARD_LIVE_LABEL
# -> SIBLING_TERM + " 작업 중".  Backward compatibility is the reason the
# phrase key survives at this rank: live instances set only
# DASHBOARD_LIVE_LABEL today and must keep rendering byte-identically, and
# a whole-phrase override also remains the only way to give THIS surface a
# wording that is not "<term> 작업 중".  An instance that wants one value to
# move every surface sets the TERM key and leaves this one unset.
LIVE_LABEL = (os.environ.get("DOGANY_LIVE_LABEL")
              or _conf_get("DASHBOARD_LIVE_LABEL")
              or "백그라운드 작업 중")
# 2026-09-25 owner decision (DGN-1691): this section lists EVERY background
# job (siblings, test/upload shell jobs, fan-outs, watchers) -- not only
# siblings -- so its neutral default names the section by what it holds,
# independent of the sibling term.  The dispatch start/end titles follow
# this label's noun too (dispatch-detached.sh background_term, DGN-1769).
# Per-item prefix inside the live section. Was the hardcoded English word
# "junior:" -- an internal term that split the language of a single rendered
# line ("서브에이전트 작업 중 ... junior: <slug>") on the first screen an
# external user meets. Same resolution order as the labels above so an
# instance can inject its own sibling term.
LIVE_ITEM_PREFIX = (os.environ.get("DOGANY_LIVE_ITEM_PREFIX")
                    or _conf_get("DASHBOARD_LIVE_ITEM_PREFIX") or "작업")
# Model display on the live line (DGN-1365 item 3).  Shipped default OFF
# (the value to an outside user was unconfirmed); Rev 15 flips the default
# to ON by owner decision 2026-09-26 ("<Model>: <name> · HH:MM (N분~)" is the
# row shape for every agent, the prefix omitted only when no model is
# recorded).  Same resolution order as every display token: env
# DOGANY_LIVE_SHOW_MODEL -> conf DASHBOARD_LIVE_SHOW_MODEL -> on.  Only an
# explicit "off" (or 0/false/no, case-insensitive) opts generic live rows
# out; dispatch rows still name their recorded effective model so no runtime
# becomes blank. Any other value keeps generic rows on. The prefix is the same
# "<Model>: <작업명>" order as dispatch start/end blocks (identity Response
# workflow, "Completion signal").
LIVE_SHOW_MODEL = ((os.environ.get("DOGANY_LIVE_SHOW_MODEL")
                    or _conf_get("DASHBOARD_LIVE_SHOW_MODEL")
                    or "on").strip().lower()
                   not in ("off", "0", "false", "no"))
# Board title: product default "작업대" (owner naming 2026-07-24); the title
# TEXT is instance/product-tunable (DGN-851): env DOGANY_BOARD_TITLE -> conf
# DASHBOARD_BOARD_TITLE -> default. Optional per-instance emoji prefix (the
# agent's identity emoji): env DOGANY_BOARD_EMOJI -> conf DASHBOARD_EMOJI ->
# none.
BOARD_EMOJI = (os.environ.get("DOGANY_BOARD_EMOJI")
               or _conf_get("DASHBOARD_EMOJI") or "")
BOARD_TITLE_TEXT = (os.environ.get("DOGANY_BOARD_TITLE")
                    or _conf_get("DASHBOARD_BOARD_TITLE") or "작업대")
BOARD_TITLE = (BOARD_EMOJI + " " if BOARD_EMOJI else "") + BOARD_TITLE_TEXT

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
# DGN-1769: a background job the owner was told STARTED (bg-notice-state
# start_sent, no end_sent) and that the transcript still shows open stays on
# the board past LIVE_STALE_SECS -- a release run takes 40 min and the owner
# saw it vanish at minute 20.  Bounded so a job whose end notice never
# reaches the transcript (a crashed then resumed session) cannot pin a row
# for the state file's 14-day retention.
BG_NOTICED_MAX_SECS = 6 * 3600

# DGN-1015: suffix appended to a launched-but-never-completed agent whose
# jsonl went silent past LIVE_STALE_SECS -- see Rev 13 above. Never used for
# a jsonl that was never observed at all (kept silently dropped, ambiguous).
STALLED_LABEL = "무응답 %d분+ -- 사망 추정" % (LIVE_STALE_SECS // 60)

# A deliberately stopped agent has NO label and NO retention constant here on
# purpose (Rev 14, owner decision 2026-09-11): it is excluded from the live
# section the moment the stop is recorded, so there is no owner-facing text to
# word and no window to tune.  Do not reintroduce either without the owner --
# a visible stopped row is a UX decision, not an implementation detail.

# Matches the launch tool_result the harness injects when Agent is run in
# the background: "Async agent launched successfully.\nagentId: <hex>".
#
# 2026-09-12 FIX (dev fork 96873af carry-back) -- the old pattern required `agentId:`
# to follow the sentence with nothing but whitespace between.  The harness
# can emit a parenthetical between the two:
#     Async agent launched successfully. (This tool result is internal
#     metadata - never quote or paste any part of it, including the agentId
#     below, into a user-facing reply.)
#     agentId: <hex>
# which broke the match and made the board blind to the launch.  Widened to
# a bounded [\s\S] scan.  400 chars is deliberately small -- a launch line
# and its agentId are adjacent; a larger window risks pairing one launch
# with a different agent's id.
_LAUNCH_RE = re.compile(
    r"Async agent launched successfully\.[\s\S]{0,400}?agentId:\s*([0-9a-fA-F]+)"
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
# Second resume shape (Rev 15, carried from a dev fork Rev 15b): newer
# harness builds report a SendMessage resume as a JSON body carrying
# "resumedAgentId": "<hex>" instead of the prose sentence above.  Same
# semantics -- the agent is back in flight -- so it is a launch event too.
_RESUME2_RE = re.compile(
    r'\\?"resumedAgentId\\?"\s*:\s*\\?"([0-9a-fA-F]+)\\?"'
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


def _async_notification_text(entry, etype):
    """Task-notification text from an async-delivery transcript entry, or "".

    Rev 15 (carried from a dev fork, DGN-995 root cause 2): a subagent
    completion notice that arrives WHILE the main turn is still running is
    never written as a type=user entry.  It lands only as
      - type=queue-operation (text at top-level "content"), and/or
      - type=attachment (attachment.commandMode == "task-notification",
        text at attachment.prompt).
    Without this the agent's completion is invisible and its row stays live
    until the jsonl ages out.

    Conservative on purpose (a live agent shown completed is the worse
    error): an attachment must declare commandMode task-notification (a
    queued USER prompt merely quoting a notice does not count), and a
    queue-operation payload must START with <task-notification> (a genuine
    notice is the whole payload; a quoted one has preamble).
    """
    try:
        if etype == "queue-operation":
            txt = entry.get("content")
            if isinstance(txt, str) and txt.lstrip().startswith(
                    "<task-notification>"):
                return txt
            return ""
        if etype == "attachment":
            att = entry.get("attachment")
            if not isinstance(att, dict):
                return ""
            if att.get("commandMode") != "task-notification":
                return ""
            txt = att.get("prompt")
            return txt if isinstance(txt, str) else ""
    except Exception:
        pass
    return ""


# Harness acknowledgements only; an input flag alone does not prove launch.
_BACKGROUND_RE = re.compile(
    r"(?:Command running in background with ID: |"
    r"Command was manually backgrounded by user with ID: |"
    r"was moved to the background \(ID: )([A-Za-z0-9_-]+)"
)
_MONITOR_RE = re.compile(r"Monitor started \(task ([A-Za-z0-9_-]+)[,)]")
_WORKFLOW_RE = re.compile(
    r"Workflow launched in (?:background|a remote CCR session)\. Task ID: "
    r"([A-Za-z0-9_-]+)"
)
_BACKGROUND_TASKID_RE = re.compile(r"<task-id>\s*([A-Za-z0-9_-]+)\s*</task-id>")


def _live_timestamp(stamp):
    """The shared ceiling for positive evidence of live work."""
    return stamp is not None and time.time() - stamp < LIVE_STALE_SECS


def _entry_timestamp(entry):
    try:
        return datetime.fromisoformat(
            entry["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


_NOTICE_BLOCK_RE = re.compile(
    r"<task-notification>([\s\S]*?)(?:</task-notification>|$)")
_NOTICE_STATUS_RE = re.compile(r"<status>\s*([A-Za-z_-]+)\s*</status>")
# Statuses that report progress, not an end: such a notice closes nothing.
_NONTERMINAL_STATUSES = ("running", "pending")


def _close_job(jobs, closed, tid, status, stamp):
    """A terminal notice for `tid`: leave the live section; record the end
    (status + time) for the owner END notice when a `closed` map is given."""
    if not tid or (status or "").lower() in _NONTERMINAL_STATUSES:
        return
    jobs.pop(tid, None)
    if closed is not None:
        closed[tid] = ((status or "").lower(), stamp)


def _close_from_text(txt, jobs, closed, stamp):
    """Close every job a task-notification text reports as ended.  A Monitor
    EVENT rides the same envelope (<event>, no <status>) and is not an end."""
    for block in _NOTICE_BLOCK_RE.findall(txt):
        m = _BACKGROUND_TASKID_RE.search(block)
        if not m or "<event>" in block:
            continue
        status = _NOTICE_STATUS_RE.search(block)
        _close_job(jobs, closed, m.group(1),
                   status.group(1) if status else "", stamp)


def _scan_background(entry, idx, pending, jobs, closed=None):
    """Pair session launches by tool_use_id; consume terminal task notices.

    Uses the existing transcript pass and freshness ceiling. Scheduled tools
    are deliberately not candidates, even if their results mention task IDs.
    jobs: task id -> (idx, description, launch epoch, tool).  closed (phase
    2, optional): task id -> (status, notice epoch) for every job that ended.
    """
    etype = entry.get("type")
    content = (entry.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else []
    stamp = _entry_timestamp(entry)
    if etype == "assistant":
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if name not in ("Bash", "Monitor", "Workflow"):
                continue
            inp = block.get("input") or {}
            slot = name
            if bg_job_notice is not None:
                slot = bg_job_notice.model_slot(name, inp)
            # Description stays empty when the launch gave none, so the
            # workbench row can fall back to the command name (DGN-1727).
            pending[block.get("id")] = (
                name, inp.get("description") or inp.get("summary") or "",
                stamp, slot)
    elif etype == "user":
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            launch = pending.pop(block.get("tool_use_id"), None)
            if not launch or block.get("is_error"):
                continue
            txt = _user_text({"message": {"content": [block]}})
            pattern = {"Bash": _BACKGROUND_RE, "Monitor": _MONITOR_RE,
                       "Workflow": _WORKFLOW_RE}[launch[0]]
            # Only the harness acknowledgement itself counts: it is the first
            # line of the result. Output that merely QUOTES an ack (cat of a
            # fixture, a grep of a log) must never open a phantom job.
            head = txt.lstrip().split("\n", 1)[0]
            match = None
            if head.startswith(("Command ", "Monitor started", "Workflow launched")):
                match = pattern.search(head)
            if match:
                jobs[match.group(1)] = (idx, launch[1], launch[2], launch[0], launch[3])
        txt = _user_text(entry)
        if "<task-notification>" in txt:
            _close_from_text(txt, jobs, closed, stamp)
    elif etype in ("attachment", "queue-operation"):
        # Real runtime shapes (Claude Code 2.1.281): the notice rides as
        # attachment.type=queued_command with the XML in attachment.prompt,
        # and as a queue-operation line with the XML in content.
        attachment = entry.get("attachment") or {}
        if not isinstance(attachment, dict):
            attachment = {}
        if attachment.get("type") in ("task_notification", "task-notification"):
            _close_job(jobs, closed,
                       attachment.get("taskId") or attachment.get("task_id"),
                       attachment.get("status") or "", stamp)
        for txt in (attachment.get("prompt"), attachment.get("content"),
                    entry.get("content")):
            if isinstance(txt, str) and "<task-notification>" in txt:
                _close_from_text(txt, jobs, closed, stamp)
    elif etype == "system":
        if entry.get("subtype") == "task_notification":
            _close_job(jobs, closed, entry.get("task_id"),
                       entry.get("status") or "", stamp)
        elif entry.get("subtype") == "task_started":
            launch = pending.get(entry.get("tool_use_id"))
            tid = entry.get("task_id")
            if launch and tid and entry.get("is_backgrounded") is not False:
                jobs[tid] = (idx, launch[1], launch[2], launch[0], launch[3])


def _background_row(slot, description, started):
    """Workbench row for a session background job:
    "<description> . <HH:MM> (<N> min~)".  DGN-1820 (owner 2026-10-01,
    supersedes the DGN-1727 "<slot>: " prefix): the slot -- the CLI name for
    Bash, the tool name otherwise -- is machine plumbing and is never shown
    next to a description; a launch with NO description shows the slot
    alone.  "~" marks the minutes as a floor: the value is frozen at render
    time while the board stays on screen."""
    if bg_job_notice is not None:
        label = bg_job_notice.owner_label(slot, description)
    else:
        label = (description or "").strip() or (slot or "").strip()
    mins = max(int((time.time() - started) // 60), 0)
    return "%s \u00b7 %s (%d\ubd84~)" % (
        label, time.strftime("%H:%M", time.localtime(started)), mins)


def _sync_background_notices(background):
    """Stop-hook half of the background-job notices (DGN-1691 phase 2): END
    for every started job that closed, START catch-up for a fresh open job
    the PostToolUse hook missed.  Exactly-once lives in the lib's state.
    Fail-open: a notice problem never touches the board or the turn."""
    if bg_job_notice is None or not background:
        return
    try:
        bg_job_notice.sync_from_scan(
            background.get("open") or {}, background.get("closed") or {},
            LIVE_STALE_SECS)
    except Exception:
        pass


def _jsonl_state(agent_id, subagents_dir):
    """DGN-1015: liveness classification for a launched-but-not-completed
    agent's own jsonl.

    Returns one of:
      "fresh"  -- written within LIVE_STALE_SECS: positive proof of life.
      "stale"  -- the file EXISTS but has gone silent past the ceiling: the
                  agent was once alive and is now presumed dead (the exact
                  silent-death signature -- an interrupt-killed subagent's
                  jsonl simply stops, with no completion event ever written).
      "absent" -- the file was never observed at all (e.g. a launch-vs-
                  first-write race just after spawn). Ambiguous: no positive
                  evidence of life OR death, so this is NOT flagged dead.
    Any read error degrades to "absent" (the pre-existing conservative
    default: never claim life without positive evidence).
    """
    try:
        if not subagents_dir or not agent_id:
            return "absent"
        p = os.path.join(subagents_dir, "agent-%s.jsonl" % agent_id)
        if not os.path.isfile(p):
            return "absent"
        if _live_timestamp(os.path.getmtime(p)):
            return "fresh"
        return "stale"
    except Exception:
        return "absent"


# ---------------------------------------------------------------------------
# Deliberate-stop records (Rev 14)
# ---------------------------------------------------------------------------
# Written by whoever performs the stop, at the moment of the stop (see
# _record_agent_stop, the subagent-stop PostToolUse hook and the
# --agent-stopped CLI).  The record OUTLIVES its effect on the board on
# purpose: the board drops the agent the instant the record exists, while the
# file stays on disk as the session's own stop history.  Deliberately NOT
# derived by scanning or sweeping anything: the standing decision is that
# enforcement and observation wiring is either a blocking hook or a
# write-time record, never a watcher that goes looking for state after the
# fact.  A stop that nobody records is therefore indistinguishable from a
# crash -- which is correct, and is the conservative direction: an unrecorded
# stop still raises the presumed-dead alarm rather than being silently
# excused.
_STOP_ID_RE = re.compile(r"^[0-9a-fA-F]{1,64}$")


def _stops_dir():
    """Directory holding one stop record per stopped agent.  Env override
    DOGANY_SUBAGENT_STOPS_DIR mirrors the DOGANY_DISPATCH_DIR /
    DOGANY_LEDGER_FILE test-isolation convention."""
    return os.environ.get("DOGANY_SUBAGENT_STOPS_DIR") or os.path.join(
        _ROOT_DIR, ".telegram_bot", "subagent-stops")


def _stop_record_path(agent_id):
    """Path for an agent's stop record, or None if the id is not a plain hex
    agent id (the id reaches this from a transcript and from argv, so it is
    never interpolated into a path unvalidated)."""
    if not agent_id or not _STOP_ID_RE.match(agent_id):
        return None
    return os.path.join(_stops_dir(), "%s.json" % agent_id)


def _stop_record(agent_id):
    """Return {"reason": str, "ts": int} if this agent was deliberately
    stopped, else None.

    None means "no stop was recorded", which routes the agent back through the
    unchanged Rev 13 live/presumed-dead classification.  Every failure mode
    (missing file, unreadable file, malformed JSON, missing/!int timestamp)
    degrades to None on purpose: a broken stop record must never be able to
    suppress the death alarm.
    """
    try:
        path = _stop_record_path(agent_id)
        if not path or not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            rec = json.load(fh)
        if not isinstance(rec, dict):
            return None
        ts = int(rec.get("ts"))
        reason = (rec.get("reason") or "").strip()
        return {"reason": reason, "ts": ts}
    except Exception:
        return None


def _record_agent_stop(agent_id, reason=""):
    """Write the stop record for `agent_id`.  Returns True on success.

    Called by the stopper as part of performing the stop.  Rewriting an
    existing record is intentional: a stop re-issued on an agent that did not
    die the first time refreshes the reason and the timestamp, and the newest
    stop is the one the history should show.
    """
    path = _stop_record_path(agent_id)
    if not path:
        return False
    try:
        _atomic_write(path, json.dumps(
            {"agent_id": agent_id,
             "reason": (reason or "").strip(),
             "ts": int(time.time())},
            ensure_ascii=False) + "\n")
        return True
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


def _scan_transcript(transcript_path, background_jobs=None,
                     background_closed=None):
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
    # Optional output keeps the existing three-dict scanner API intact.
    pending = {}
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
                # Rev 15: mid-turn completion notices land only as
                # attachment/queue-operation entries; admit any line carrying
                # a task-notification body so those are scanned too.
                has_notif = "<task-notification>" in raw
                # Background launches/closures (DGN-1691): assistant tool_use
                # for Bash/Monitor/Workflow, system task lifecycle events, and
                # task-notification attachments. Everything else stays unparsed
                # so large transcripts do not pay a full JSON pass.
                has_bg = background_jobs is not None and (
                    '"task_' in raw or "task-notification" in raw
                    or '"name":"Bash"' in raw or '"name": "Bash"' in raw
                    or '"name":"Monitor"' in raw or '"name": "Monitor"' in raw
                    or '"name":"Workflow"' in raw or '"name": "Workflow"' in raw)
                if (not is_user and not has_send and not has_notif
                        and not has_bg):
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                if background_jobs is not None:
                    _scan_background(entry, idx, pending, background_jobs,
                                     background_closed)
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
                    r2 = _RESUME2_RE.search(txt)
                    if r2:
                        last_launch[r2.group(1)] = idx
                    if "<task-notification>" in txt and (
                        "<status>completed</status>" in txt or "came to rest" in txt
                    ):
                        for tid in _TASKID_RE.findall(txt):
                            last_compl[tid] = idx
                elif etype in ("attachment", "queue-operation"):
                    # Rev 15: COMPLETION scan only.  Launch/resume regexes
                    # are deliberately NOT applied here: a queued prompt
                    # quoting a launch string would mint a ghost live agent,
                    # and launch/resume results are synchronous tool_results
                    # that always land as type=user anyway.  A notice seen
                    # twice (here and as type=user) is harmless: last_compl
                    # is agentId-keyed.
                    txt = _async_notification_text(entry, etype)
                    if txt and (
                        "<status>completed</status>" in txt
                        or "came to rest" in txt
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


# Completed-agent description cap for the Rev 15 detail output: each entry
# costs one small meta.json read, and a long owner session accumulates many.
MAX_COMPLETED_DESCS = 20


def _agent_meta_model_and_spawn(agent_id, subagents_dir):
    """(model, runtime, spawn_epoch) from an agent's meta.json; None per miss.

    model/runtime: what the harness recorded at spawn (absent -> None, never
    guessed).  spawn_epoch: the meta.json mtime -- the file is written once at
    spawn and never rewritten (DGN-211: the whole file is frozen), so its
    mtime is the start time.  Never raises.
    """
    model = runtime = spawn_epoch = None
    try:
        if subagents_dir and agent_id:
            meta_path = os.path.join(subagents_dir,
                                     "agent-%s.meta.json" % agent_id)
            if os.path.isfile(meta_path):
                with open(meta_path) as fh:
                    meta = json.load(fh)
                model = (meta.get("model") or "").strip() or None
                runtime = (meta.get("runtime") or "").strip() or None
                try:
                    spawn_epoch = os.path.getmtime(meta_path)
                except OSError:
                    spawn_epoch = None
    except Exception:
        pass
    return model, runtime, spawn_epoch


def _agent_observed_model(agent_id, subagents_dir):
    """The model id a live subagent's own jsonl reports ("" when none yet).

    Same observation the dispatch rows use (model_display.observe_from_stream
    reads type=assistant message.model), pointed at the agent's transcript:
    it is what turns the spawn alias "opus" into "Claude Opus 5.5".  first_only keeps
    the read to the first assistant turn.  Never raises."""
    if _observe_from_stream is None or not subagents_dir or not agent_id:
        return ""
    try:
        ids = _observe_from_stream(
            os.path.join(subagents_dir, "agent-%s.jsonl" % agent_id),
            first_only=True)
        return ids[0] if ids and isinstance(ids[0], str) else ""
    except Exception:
        return ""


def _collect_active_subagents(transcript_path, background_out=None,
                              detail_out=None):
    """Return ordered descriptions for subagents and session background work.

    Active iff the most recent launch/resume post-dates the most recent
    completion AND the subagent jsonl is still fresh (see LIVE_STALE_SECS).
    LIVE_STALE_SECS is now 1200s (20 min) to survive long tool-call silence
    (DGN-540 Case B); the transcript completion-event detection guards against
    ghost agents from missed completions.  The label prefers a post-launch
    SendMessage recap over the spawn-frozen meta.json description (DGN-211).

    DGN-1015 (Rev 13): a launch-without-completion whose jsonl EXISTS but has
    gone stale is no longer dropped -- it is appended with a STALLED_LABEL
    suffix (sorted after the genuinely live entries) so a silent death is
    visually distinct from both "0 active" and a normally-completed agent. A
    jsonl that was never observed at all stays silently dropped (ambiguous,
    not a measurement of death).

    Rev 14: an agent carrying a stop record (see _stop_record) is EXCLUDED
    from this list -- immediately and permanently, with no label and no grace
    window.  The owner's board answers "what is running / what actually died";
    an agent the session stopped on purpose is neither.  Freshness still wins
    over the stop record, so an agent that was stopped and later resumed (its
    jsonl starts moving again) comes back as a plain live row.

    Background Bash/Monitor/Workflow jobs share this scan and ceiling. Their
    launch timestamp is the available evidence; no output-file mtime is
    treated as proof that a process is still alive.  DGN-1769: an open job
    whose owner START was sent and END not yet (bg_job_notice
    started_open_ids) outlives the ceiling, up to BG_NOTICED_MAX_SECS. Their rows use the
    owner-locked workbench row format (2026-09-17)
    "<name> . <HH:MM> (<N> min~)" -- no model, and no slot either (DGN-1820,
    _background_row); subagent labels remain unchanged.

    background_out (optional dict) receives the scan's background state for
    the owner notices (DGN-1691 phase 2): "open" -> the jobs map, "closed"
    -> task id -> (status, notice epoch).

    detail_out (optional dict, Rev 15) receives, from the SAME scan:
      "records"   -> a list ALIGNED 1:1 with the returned descriptions: one
                     {"desc", "model_display", "start_epoch"} dict per
                     genuinely live agent (model_display = the owner-facing
                     label, "" when no model is recorded or observed) and
                     None for a stalled or background row (a presumed death
                     has no live runtime to stamp; a background row is
                     already stamped by _background_row);
      "completed" -> descriptions of the MAX_COMPLETED_DESCS most recently
                     completed agents (a board plugin's review signal).
    Best-effort: a failure there shortens the lists, never the rows.

    Returns None on FAILURE (missing/unreadable transcript): None means
    "cannot confirm empty", so the caller must never treat it as a genuinely
    empty board (DGN-541 fail-open empty-ban). An empty list [] means
    CONFIRMED no active subagents.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    records = []
    if detail_out is not None:
        detail_out["records"] = records
        detail_out["completed"] = []
    try:
        subagents_dir = _subagents_dir(transcript_path)
        background_jobs = {}
        background_closed = {}
        last_launch, last_compl, last_summary = _scan_transcript(
            transcript_path, background_jobs, background_closed)
        if background_out is not None:
            background_out["open"] = background_jobs
            background_out["closed"] = background_closed
        live, stalled = [], []
        for aid, launch_idx in last_launch.items():
            if launch_idx <= last_compl.get(aid, -1):
                continue      # a completion notice was seen after the launch
            state = _jsonl_state(aid, subagents_dir)
            if state == "fresh":
                live.append(aid)
                continue      # proof of life outranks any older stop record
            if _stop_record(aid) is not None:
                continue      # stopped on purpose -> off the owner's board
            if state == "stale":
                stalled.append(aid)
            # "absent": no positive evidence either way -- silently dropped,
            # same as the pre-DGN-1015 behavior for this specific case.
        descs = []
        for aid in sorted(live):
            desc = _resolve_description(
                aid, last_launch[aid], last_summary.get(aid), subagents_dir)
            descs.append(desc)
            if detail_out is not None:
                rec = None
                try:
                    model, runtime, spawn = _agent_meta_model_and_spawn(
                        aid, subagents_dir)
                    observed = _agent_observed_model(aid, subagents_dir)
                    rec = {"desc": desc,
                           "model_display": _model_label(runtime, model,
                                                         observed),
                           "start_epoch": spawn}
                except Exception:
                    rec = None
                records.append(rec)
        for aid in sorted(stalled):
            base = _resolve_description(
                aid, last_launch[aid], last_summary.get(aid), subagents_dir)
            descs.append("%s (%s)" % (base, STALLED_LABEL))
            records.append(None)
        noticed = set()
        if background_jobs and bg_job_notice is not None:
            try:
                noticed = bg_job_notice.started_open_ids()
            except Exception:
                noticed = set()
        for tid, job in sorted(background_jobs.items(), key=lambda kv: kv[1]):
            _, description, started, _tool, slot = job
            if _live_timestamp(started) or (
                    tid in noticed and started is not None
                    and time.time() - started < BG_NOTICED_MAX_SECS):
                descs.append(_background_row(slot, description, started))
                records.append(None)
        if detail_out is not None:
            try:
                done = [aid for aid, cidx in last_compl.items()
                        if cidx >= last_launch.get(aid, -1)]
                done.sort(key=lambda a: -last_compl[a])  # most recent first
                detail_out["completed"] = [
                    _resolve_description(aid, last_launch.get(aid, -1),
                                         last_summary.get(aid), subagents_dir)
                    for aid in done[:MAX_COMPLETED_DESCS]]
            except Exception:
                detail_out["completed"] = []
        return descs
    except Exception:
        if detail_out is not None:
            detail_out["records"] = []
            detail_out["completed"] = []
        return None


# DGN-995 (ledger path): a 'running' row had NO staleness/pid check at all
# -- unlike the transcript path's 20-minute LIVE_STALE_SECS ceiling, a
# ledger junior whose process died (crash, kill, host reboot) with no
# terminal ledger transition stayed "running" FOREVER (worklog measured a
# 3-day-old dead row still rendered live).  Dispatch-detached.sh rows
# (branch prefix "dsp/*") already leave a discoverable, pid-reuse-safe
# liveness record: run.env's LEDGER_BRANCH field is written from the exact
# same value passed to ledger-update.sh as the branch column (dispatch-
# detached.sh do_spawn), so it is a byte-exact correlation key back to this
# row -- not a guess. The rundir's pid file + proc.txt (lstart+command,
# stamped at worker spawn) mirror dispatch-detached.sh's own --cancel
# pid-reuse guard (spec R5) so this reader never mistakes a recycled pid
# for the original junior. Rows this cannot correlate (autonomous-loop
# 'auto/*' rows -- that dispatcher writes no run.env; or a 'dsp/*' row in
# the brief window between the ledger write and the worker's own pid-file
# write) yield NO EVIDENCE, which is NOT treated as death -- same
# absent-vs-stale split _collect_active_subagents already uses for a jsonl
# that was never observed (silently kept, never guessed dead).
LEDGER_DEAD_LABEL = "pid 사망 확인 -- 종결 이벤트 없음"


def _dispatch_rundir_for_branch(branch):
    """Best-effort: the dispatch-detached.sh RUNDIR whose run.env
    LEDGER_BRANCH matches the given ledger row branch, or None if nothing
    correlates (e.g. an autonomous-loop 'auto/*' row, or no dispatch
    directory at all).  A miss is NOT evidence of anything -- it just means
    this reader has no pid to check for that row.  Env override
    DOGANY_DISPATCH_DIR mirrors the DOGANY_LEDGER_FILE test-isolation
    convention.
    """
    dispatch_dir = os.environ.get("DOGANY_DISPATCH_DIR") or os.path.join(
        _ROOT_DIR, ".telegram_bot", "dispatch")
    try:
        if not os.path.isdir(dispatch_dir):
            return None
        for name in os.listdir(dispatch_dir):
            if not name.startswith("dsp-"):
                continue
            run_env = os.path.join(dispatch_dir, name, "run.env")
            try:
                with open(run_env, "r", errors="replace") as fh:
                    text = fh.read()
            except Exception:
                continue
            m = re.search(r"(?m)^LEDGER_BRANCH=(.*)$", text)
            if not m:
                continue
            if m.group(1).strip().strip("'\"") == branch:
                return os.path.join(dispatch_dir, name)
    except Exception:
        return None
    return None


def _ps_identity(pid):
    """Return the ps(1) 'lstart= command=' identity line for a live pid, or
    None on any failure.  Same ps flags dispatch-detached.sh's do_cancel
    uses for its pid-reuse comparison, so the two readers agree."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=,command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3)
        if out.returncode != 0:
            return None
        return " ".join(out.stdout.split()) or None
    except Exception:
        return None


def _dispatch_junior_confirmed_dead(branch):
    """True only with POSITIVE evidence the dispatch owning this ledger
    branch is dead: a resolvable rundir whose recorded pid is gone, or
    alive but under a DIFFERENT process (pid recycled -- identity mismatch
    against the rundir's own proc.txt, the same spec-R5 guard
    dispatch-detached.sh's --cancel path checks before ever signaling a
    pid). False for every other case, INCLUDING "cannot determine" --
    absence of evidence is never treated as evidence of death.
    """
    try:
        rundir = _dispatch_rundir_for_branch(branch)
        if not rundir:
            return False
        with open(os.path.join(rundir, "pid")) as fh:
            pid = int(fh.read().strip())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True  # pid gone -- confirmed dead
        except PermissionError:
            pass  # process exists (owned by someone else) -- alive
        except Exception:
            return False  # can't tell -- no death claim
        try:
            with open(os.path.join(rundir, "proc.txt")) as fh:
                recorded = " ".join(fh.read().split())
        except Exception:
            recorded = None
        if recorded:
            current = _ps_identity(pid)
            if current is not None and current != recorded:
                return True  # pid recycled by an unrelated process
        return False
    except Exception:
        return False


# The running-row note is written by its spawner in a parseable shape
# ("dispatch 스폰 (model=<alias> runtime=<runtime> ...)" -- the contract
# named in dispatch-detached.sh's ledger section).  runtime= is absent from
# rows written before it existed; those were all claude-code spawns, so that
# is the fallback, not a guess.
_NOTE_MODEL_RE = re.compile(r"model=([A-Za-z0-9_.\-]+)")
_NOTE_RUNTIME_RE = re.compile(r"runtime=([A-Za-z0-9_.\-]+)")


def _observed_model_for_branch(branch, runtime):
    """The model id a still-running dispatch has reported so far, or "".

    Read from the run's own stream.jsonl (the same observation the run's
    status.json model_observed is written from at close). codex runs need no
    stream: their slug is pinned by the TSV row.  No rundir / no stream / no id
    yet -> "" (the row then shows the bare name, never a guessed version).
    """
    if not branch or runtime in ("codex", "cloud") or _observe_from_stream is None:
        return ""
    rundir = _dispatch_rundir_for_branch(branch)
    if not rundir:
        return ""
    try:
        ids = _observe_from_stream(os.path.join(rundir, "stream.jsonl"),
                                   first_only=True)
        return ids[0] if ids and isinstance(ids[0], str) else ""
    except Exception:
        return ""


def _model_display_from_note(note, branch=None):
    """Owner-facing model label for a running row's note, or "" when the
    note names no model (a row this reader cannot label is rendered without
    one, never guessed).

    Format "<Name> <version>" (DGN-1227): the version comes from the model id
    the run reported (see _observed_model_for_branch); before one exists the
    label is the bare name from routines/lib/dispatch-model-display.tsv.  The
    parsing and the TSV read live in routines/lib/model_display.py, shared with
    dispatch-detached.sh -- there is no second copy of the rule here.
    FAIL-OPEN (DGN-1227 follow-up): a missing lib, a raise inside it or an
    empty answer falls back to the raw alias verbatim; the row still renders.
    """
    m = _NOTE_MODEL_RE.search(note or "")
    if not m:
        return ""
    model = m.group(1)
    rt = _NOTE_RUNTIME_RE.search(note or "")
    runtime = rt.group(1) if rt else "claude-code"
    if _shared_model_display is None:
        return model
    tsv = os.path.join(_ROOT_DIR, "routines", "lib",
                       "dispatch-model-display.tsv")
    try:
        label = _shared_model_display(
            runtime, model, _observed_model_for_branch(branch, runtime), tsv)
    except Exception:
        label = ""
    return label if isinstance(label, str) and label.strip() else model


def _dispatch_row(model_display, branch, label, fallback_start):
    """Prefix every dispatch row from its single effective-model display seat.

    When the dispatch records SPAWN_EPOCH, use it for the row's stamp.  This
    is runtime-neutral: Codex, Cloud, and Claude-code all follow the same
    path; a pre-field run falls back to the ledger timestamp instead.
    """
    row = (model_display + ": " + label) if model_display else label
    started = None
    rundir = _dispatch_rundir_for_branch(branch)
    if rundir:
        try:
            with open(os.path.join(rundir, "run.env"), "r",
                      errors="replace") as fh:
                m = re.search(r"(?m)^SPAWN_EPOCH=['\"]?(\d+)", fh.read())
            started = int(m.group(1)) if m else None
        except Exception:
            started = None
    if started is None:
        return row, fallback_start
    mins = max(int((time.time() - started) // 60), 0)
    return "%s \u00b7 %s (%d\ubd84~)" % (
        row, time.strftime("%H:%M", time.localtime(started)), mins), None


def _model_label(runtime, model, observed=""):
    """Owner-facing label for a (runtime, alias[, observed id]) triple -- the
    DGN-1227 rule for callers that hold the pieces rather than a ledger note
    (a subagent's meta.json + its own jsonl).  An observed id alone is enough
    ("claude-opus-5-5" -> "Claude Opus 5.5"); nothing recorded -> "" (the row then
    carries no prefix, never a guess).  Lib absent / raising / empty answer
    -> the alias with its first letter capitalized (same fail-open as
    _model_display_from_note)."""
    if not model and not observed:
        return ""
    fallback = (model or "")[:1].upper() + (model or "")[1:]
    if _shared_model_display is None:
        return fallback
    tsv = os.path.join(_ROOT_DIR, "routines", "lib",
                       "dispatch-model-display.tsv")
    try:
        label = _shared_model_display(runtime or "claude-code", model or "",
                                      observed or "", tsv)
    except Exception:
        label = ""
    return label if isinstance(label, str) and label.strip() else fallback


def _ledger_ts_epoch(ts_text):
    """Local epoch of a ledger last_ts field ('YYYY-MM-DD HH:MM[:SS] TZ'), or
    None.  The zone label is ignored on purpose: the stamp is written by the
    local `date` at dispatch time on this machine, the same clock the board's
    own "갱신 HH:MM" stamp reads.  A date-only field ('YYYY-MM-DD') carries no
    start time and yields None (the row then renders without a time).
    Never raises."""
    try:
        m = re.match(
            r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?",
            (ts_text or "").strip())
        if not m:
            return None
        y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
        s = int(m.group(6)) if m.group(6) else 0
        return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
    except Exception:
        return None


def _collect_ledger_running(detail_out=None):
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

    DGN-995: a row whose dispatch is CONFIRMED dead (see
    _dispatch_junior_confirmed_dead) is not silently dropped -- it is kept
    with a LEDGER_DEAD_LABEL suffix, sorted after the still-live rows, so a
    ledger junior that died without a terminal ledger transition is
    visually distinct from both "0 running" and a genuinely live one
    (same visibility invariant as DGN-1015's STALLED_LABEL on the
    transcript path). A row with no correlatable evidence either way is
    rendered exactly as before (unflagged) -- see the module note above.

    detail_out (optional dict, Rev 15) receives, from the SAME pass:
      "records" -> a list ALIGNED 1:1 with the returned labels: one
                   {"desc", "branch", "model_display", "start_epoch"} dict
                   per live running row (model_display = the prefix already
                   on a dispatched label when its note names a model; generic
                   rows may be empty when LIVE_SHOW_MODEL is off; start_epoch
                   from last_ts) and None for a
                   confirmed-dead row (no live runtime to stamp);
      "review"  -> "branch item" text per pending-review row still
                   unmerged (DGN-1730 _filter_review_rows; a board
                   plugin's review signal).
    The returned labels are unchanged by it.
    """
    path = os.environ.get("DOGANY_LEDGER_FILE") or os.path.join(
        _ROOT_DIR, LEDGER_FILE)
    items = []
    dead_items = []
    records = []
    review = []
    review_rows = []
    if detail_out is not None:
        detail_out["records"] = records
        detail_out["review"] = review
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
                if state == "pending-review":
                    text = " ".join(x for x in (parts[1], parts[2])
                                    if x and x != "-")
                    if text:
                        review_rows.append((
                            parts[1], parts[8] if len(parts) > 8 else "",
                            text))
                    continue
                if state != "running":
                    continue
                item = parts[2]
                branch = parts[1]
                named = bool(item and item != "-")
                desc = item if named else branch
                if desc and desc != "-":
                    # Owner ruling 2026-09-12 (DGN-1429): the row already leads
                    # with the model ("Sonnet: ..."), so a generic noun in front
                    # of a real display name is pure noise. The prefix exists for
                    # the BRANCH fallback, where a bare token ("auto/dgn-541") is
                    # not a sentence and needs a noun to hang on.
                    label = desc if named else (LIVE_ITEM_PREFIX + ": " + desc)
                    note = parts[8] if len(parts) > 8 else ""
                    start_epoch = _ledger_ts_epoch(
                        parts[6] if len(parts) > 6 else "")
                    # DGN-1752: dispatched work has one effective-model
                    # display seat irrespective of runtime.  In particular,
                    # Codex must not vanish merely because Cloud previously
                    # had a renderer-only exception.  The shared parser owns
                    # both the concrete model and any runtime marker.
                    model_disp = _model_display_from_note(note, branch)
                    label, start_epoch = _dispatch_row(
                        model_disp, branch, label, start_epoch)
                    if _dispatch_junior_confirmed_dead(branch):
                        dead_items.append("%s (%s)" % (label, LEDGER_DEAD_LABEL))
                    else:
                        items.append(label)
                        records.append({
                            "desc": label,
                            "branch": branch,
                            # Already on the label when non-empty (the
                            # stamp never adds it twice); kept here so a
                            # plugin can stamp a ticket row with it.
                            "model_display": model_disp,
                            "start_epoch": start_epoch,
                        })
    except FileNotFoundError:
        del records[:], review[:]
        return []
    except Exception:
        del records[:], review[:]
        return None
    records.extend([None] * len(dead_items))
    # DGN-1730: a pending-review row is review load only while its work is
    # not in base -- same filter as _collect_ledger_review.
    if detail_out is not None and review_rows:
        keep = _filter_review_rows([(b, n) for b, n, _ in review_rows], path)
        review.extend(t for (_, _, t), k in zip(review_rows, keep) if k)
    return items + dead_items


# ---------------------------------------------------------------------------
# DGN-1730: pending-review rows are review load ONLY while their work is not
# yet in base.  A dsp/* branch merged by hand never flips its ledger row
# (only loop-merge.sh does), so the raw 'pending-review' state overstates the
# review queue.  Forcing point on the READ side: a row counts as review iff
# its branch tip -- or, when the branch ref is gone, the HEAD=<sha> in its
# note -- is NOT an ancestor of base.  Repos checked: the instance repo
# (DOGANY_INSTANCE_ROOT, base main) and the canonical repo (DOGANY_REPO_ROOT,
# falling back to .instance.conf, base dev), each falling back to its
# default branch.  Unresolvable (no ref, no sha) -> not review (cannot be
# acted on; routines/ledger-reconcile.sh reports it).
#
# Runs inside the every-turn Stop hook, so cost is bounded: <= 3 git calls
# per repo (for-each-ref, cat-file --batch-check, rev-list --stdin), one
# shared time budget, and a disk cache keyed on ledger mtime + base HEADs so
# a steady-state turn costs one for-each-ref per repo.  ANY failure degrades
# to the pre-DGN-1730 behaviour (every pending-review row counts) -- never a
# crash, never a silently emptied queue.
# ---------------------------------------------------------------------------
LEDGER_REVIEW_CACHE = ".telegram_bot/ledger-review-cache.json"
LEDGER_REVIEW_BUDGET_S = 4.0
_NOTE_HEAD_RE = re.compile(r"HEAD=([0-9a-fA-F]{7,40})")


def _review_key(branch, note):
    m = _NOTE_HEAD_RE.search(note or "")
    return "%s\t%s" % (branch, m.group(1) if m else "")


def _review_repos():
    """[(repo_path, preferred_base_branch)], de-duplicated by realpath."""
    inst = os.environ.get("DOGANY_INSTANCE_ROOT") or _ROOT_DIR
    repos = [(inst, "main")]
    canon = os.environ.get("DOGANY_REPO_ROOT") or ""
    if not canon:
        try:
            canon = conf_get("DOGANY_REPO_ROOT",
                             os.path.join(_ROOT_DIR, ".instance.conf")) or ""
        except Exception:
            canon = ""
    if canon:
        repos.append((canon, "dev"))
    seen, out = set(), []
    for path, base in repos:
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            out.append((path, base))
    return out


def _git_run(repo, args, deadline, stdin=None):
    """stdout of `git -C repo args`, or None on any failure / budget spent."""
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    try:
        out = subprocess.run(["git", "-C", repo] + list(args), input=stdin,
                             capture_output=True, text=True, timeout=left)
    except Exception:
        return None
    return out.stdout if out.returncode == 0 else None


def _review_repo_facts(repo, pref, deadline):
    """(base_sha, tips) for one repo, or None when it cannot be read.
    tips maps branch name -> sha (local heads win over origin/<branch>)."""
    out = _git_run(repo, ["for-each-ref",
                          "--format=%(refname) %(objectname) %(symref)",
                          "refs/heads", "refs/remotes/origin"], deadline)
    if out is None:
        return None
    heads, remotes, origin_head = {}, {}, None
    for line in out.splitlines():
        f = line.split()
        if len(f) < 2:
            continue
        ref, sha = f[0], f[1]
        if ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):]] = sha
        elif ref == "refs/remotes/origin/HEAD":
            origin_head = sha
        elif ref.startswith("refs/remotes/origin/"):
            remotes[ref[len("refs/remotes/origin/"):]] = sha
    base = heads.get(pref) or origin_head or heads.get("main") \
        or heads.get("master")
    if not base:
        return None
    tips = dict(remotes)
    tips.update(heads)
    return base, tips


def _review_repo_status(repo, facts, rows, deadline):
    """Per-row 'merged' / 'unmerged' / None (unresolved here) for one repo,
    or None on git failure.  `rows` = [(branch, note)]."""
    base, tips = facts
    cand = []                      # per row: full sha or a note sha to check
    need_check = []
    for i, (branch, note) in enumerate(rows):
        if branch in tips:
            cand.append(tips[branch])
            continue
        m = _NOTE_HEAD_RE.search(note or "")
        cand.append(None)
        if m:
            need_check.append((i, m.group(1)))
    if need_check:
        out = _git_run(repo, ["cat-file", "--batch-check"], deadline,
                       stdin="".join("%s^{commit}\n" % s
                                     for _, s in need_check))
        if out is None:
            return None
        lines = out.splitlines()
        if len(lines) != len(need_check):
            return None
        for (i, _), line in zip(need_check, lines):
            f = line.split()
            if len(f) >= 2 and f[1] == "commit":
                cand[i] = f[0]
    shas = sorted({c for c in cand if c})
    unmerged = set()
    if shas:
        out = _git_run(repo, ["rev-list", "--stdin"], deadline,
                       stdin="".join(s + "\n" for s in shas)
                       + "--not\n%s\n" % base)
        if out is None:
            return None
        unmerged = set(out.split())
    return [None if not c else ("unmerged" if c in unmerged else "merged")
            for c in cand]


def _review_cache_read(key):
    try:
        path = os.environ.get("DOGANY_LEDGER_REVIEW_CACHE") or os.path.join(
            _ROOT_DIR, LEDGER_REVIEW_CACHE)
        with open(path) as fh:
            data = json.load(fh)
        if data.get("key") == key and isinstance(data.get("rows"), dict):
            return data["rows"]
    except Exception:
        pass
    return None


def _review_cache_write(key, rows):
    try:
        path = os.environ.get("DOGANY_LEDGER_REVIEW_CACHE") or os.path.join(
            _ROOT_DIR, LEDGER_REVIEW_CACHE)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump({"key": key, "rows": rows}, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def _filter_review_rows(rows, ledger_path=None):
    """Aligned list of bools: True = the pending-review row is still review
    load (its work is NOT in base).  `rows` = [(branch, note)].  Any failure
    -> all True (degrade to the raw ledger state); see the DGN-1730 note."""
    all_review = [True] * len(rows)
    if not rows:
        return all_review
    try:
        deadline = time.monotonic() + LEDGER_REVIEW_BUDGET_S
        facts = []                       # [(repo, facts-or-None)]
        for repo, pref in _review_repos():
            facts.append((repo, _review_repo_facts(repo, pref, deadline)))
        if not any(f for _, f in facts):
            return all_review
        try:
            st = os.stat(ledger_path)
            stamp = [st.st_mtime_ns, st.st_size]
        except Exception:
            stamp = None
        key = [stamp] + [[os.path.realpath(r), f[0] if f else None]
                         for r, f in facts]
        cached = _review_cache_read(key) if stamp else None
        decided = {}
        if cached is not None:
            decided = cached
        # rows the cache does not know (or every row on a miss)
        todo = [(b, n) for b, n in rows if _review_key(b, n) not in decided]
        if todo:
            per_repo = []
            for repo, f in facts:
                per_repo.append(None if f is None else
                                _review_repo_status(repo, f, todo, deadline))
            if all(p is None for p in per_repo):
                return all_review
            unknown_repo = any(p is None for p in per_repo)
            for idx, (b, n) in enumerate(todo):
                sts = [p[idx] for p in per_repo if p is not None]
                if "merged" in sts:
                    review = False
                elif "unmerged" in sts:
                    review = True
                else:
                    # unresolved everywhere we could look; a repo we could
                    # not read may still hold it -> keep counting it.
                    review = unknown_repo
                decided[_review_key(b, n)] = review
            if stamp and not unknown_repo:
                _review_cache_write(key, decided)
        return [bool(decided.get(_review_key(b, n), True)) for b, n in rows]
    except Exception:
        return all_review


def _collect_ledger_review():
    """"<branch> <item>" text per pending-review row that is still review
    load (DGN-1730: not merged into base).  Missing ledger -> []; read
    failure -> [] (best-effort per-ticket signal, same contract as the
    workbench review source)."""
    path = os.environ.get("DOGANY_LEDGER_FILE") or os.path.join(
        _ROOT_DIR, LEDGER_FILE)
    rows = []
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("|"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 5 or parts[3] != "pending-review":
                    continue
                note = parts[8] if len(parts) > 8 else ""
                rows.append((parts[1], parts[2], note))
    except Exception:
        return []
    keep = _filter_review_rows([(b, n) for b, _, n in rows], path)
    return [" ".join(x for x in (b, i) if x and x != "-")
            for (b, i, _), k in zip(rows, keep) if k and (b or i)]


# ---------------------------------------------------------------------------
# Live-row stamp (Rev 15): "<Model>: <name> · HH:MM (N분~)" for every source
# ---------------------------------------------------------------------------

def _format_start_and_date(epoch):
    """'HH:MM' for a local epoch today, 'MM/DD HH:MM' for another day, or None.

    Bare "HH:MM" reads as "started this morning"; a run that is actually from
    a previous day must not be misread as today (carried from a dev fork,
    owner spec 2026-08-21).  Plain time.localtime -- the same clock as the
    board's own freshness stamp.  None/invalid -> None."""
    try:
        if epoch is None:
            return None
        lt = time.localtime(epoch)
        if lt[:3] == time.localtime()[:3]:
            return time.strftime("%H:%M", lt)
        return time.strftime("%m/%d %H:%M", lt)
    except Exception:
        return None


def _format_elapsed(epoch):
    """Minutes since `epoch` as 'N분~', or None (missing / clock skew).

    "~" marks the value as a floor: it is frozen at render time while the
    board stays on screen, so a bare "N분" would read as "now"."""
    try:
        if epoch is None:
            return None
        mins = int((time.time() - epoch) // 60)
        if mins < 0:
            return None
        return "%d\ubd84~" % mins
    except Exception:
        return None


def _live_stamp(model_display, start_epoch):
    """(prefix, suffix) for one live row, owner-locked shape 2026-09-17:
    "<Model>: <name> · HH:MM (N분~)".

    prefix: the model label ("" when none is recorded -- the row then leads
    with the name, never a placeholder; "" as well when LIVE_SHOW_MODEL is
    off).  A lowercase routing token gets its first letter capitalized
    (owner ruling 2026-09-11: models are proper nouns).  suffix: " · <time>"
    or "" when the start is unknown.  Never raises."""
    try:
        prefix = (model_display or "").strip() if LIVE_SHOW_MODEL else ""
        if prefix[:1].islower():
            prefix = prefix[:1].upper() + prefix[1:]
        suffix = ""
        stamp = _format_start_and_date(start_epoch)
        if stamp:
            elapsed = _format_elapsed(start_epoch)
            suffix = " \u00b7 " + (
                "%s (%s)" % (stamp, elapsed) if elapsed else stamp)
        return prefix, suffix
    except Exception:
        return "", ""


def _cap_with_suffix(line, suffix, limit=None):
    """Cap one row to `limit` UTF-16 units while keeping `suffix` whole.

    Without this a long name would make _cap_item's tail-ellipsis chop
    exactly the start-time stamp; the name degrades instead.  No suffix ->
    plain _cap_item.  A suffix wider than the cap degrades to the capped
    bare line (never an unbounded row)."""
    cap = DASHBOARD_ITEM_MAX_UNITS if limit is None else limit
    if not suffix:
        return _cap_item(line, cap)
    try:
        budget = cap - _u16len(suffix)
        if budget <= 0:
            return _cap_item(line, cap)
        return _cap_item(line, budget) + suffix
    except Exception:
        return _cap_item(line, cap)


def _format_live_items(active_agents, live_records):
    """Stamp the live-section rows (Rev 15).

    `live_records` is the concatenation of the collectors' detail records,
    aligned 1:1 with `active_agents` (transcript rows, then ledger rows --
    the same order main() concatenates the labels).  Equal lengths -> index
    pairing; otherwise pairing falls back to exact-desc lookup, and a row
    with no confident record renders unstamped: a wrong time on the wrong
    row is worse than none.  A row that already leads with its model (the
    ledger path applies it) never gets a second prefix.  Fail-open: any
    exception returns the plain capped rows.
    """
    descs = list(active_agents or [])
    if not descs:
        return []
    try:
        recs = list(live_records or [])
        if len(recs) == len(descs):
            paired = recs
        else:
            by_desc = {}
            for r in recs:
                if isinstance(r, dict) and r.get("desc") not in by_desc:
                    by_desc[r.get("desc")] = r
            paired = [by_desc.get(d) for d in descs]
        out = []
        for d, rec in zip(descs, paired):
            text, suffix = d, ""
            if isinstance(rec, dict):
                prefix, suffix = _live_stamp(rec.get("model_display"),
                                             rec.get("start_epoch"))
                if prefix and not d.startswith(prefix + ": "):
                    text = prefix + ": " + d
            out.append(_cap_with_suffix(text, suffix))
        return out
    except Exception:
        return [_cap_item(d) for d in descs]


# ---------------------------------------------------------------------------
# Pending decisions (worklog/_DECISIONS.md, DGN-214)
# ---------------------------------------------------------------------------

# Ledger line: "- [YYYY-MM-DD] [dec-NNN] <summary>".  The [dec-NNN] id is
# optional for backward compatibility but standard going forward (DGN-215:
# stable ids make console deep links immune to ledger-line churn).
_DECISION_RE = re.compile(
    r"^-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(?:\[(dec-\d+)\]\s*)?(.+\S)\s*$")


# Decision detail files (Rev 15, carried from a dev fork Rev 21): a
# ledger entry "[dec-NNN]" may have worklog/decisions/dec-NNN.md whose H2
# sections carry the question, the agent's recommendation and the options.
# The board shows exactly those three (never the whole file -- the board must
# stay light) inside one expandable fold per decision.  Env override
# DOGANY_DECISIONS_DIR mirrors DOGANY_DECISIONS_FILE (test isolation).
DECISIONS_DIR = "worklog/decisions"
# Read caps: both files are hand-kept documents; the caps only bound a
# runaway file on this every-turn hook.  The ledger cap is generous on
# purpose -- a live ledger measured 61 KB (2026-09-26) and an entry past the
# cap would silently vanish from the board.
DECISIONS_READ_CAP = 1048576
DEC_DETAIL_READ_CAP = 65536
# Expanded body cap per decision (UTF-16 units): one runaway detail must not
# eat the whole DASHBOARD_MAX_UNITS budget.
DEC_BODY_MAX_UNITS = 700
# H2 heading -> slot.  The corpus spells the same three things several ways
# (measured over a live decisions dir 2026-09-23: 선택지/옵션, 물음/질문, and
# "<agent> 권고" / "<agent> 의견"); aliases here rather than rewriting the
# owner's reading surface to suit a parser.
_DEC_HEAD_ALIASES = {
    "물음": "물음", "질문": "물음", "무엇을 정하나": "물음",
    "선택지": "선택지", "옵션": "선택지",
    "권고": "권고", "의견": "권고",
}
_DEC_HEAD_AGENT_RE = re.compile(r"^\S+\s+(권고|의견)$")
_FENCE = "`" * 3


def _sanitize_fold_body_line(line):
    """Make one foreign text line safe inside a fold:: body.

    Three hazards neutralized so the bridge transpile can never be corrupted
    by file content: a leading '>' (nested blockquote -- Telegram rejects
    it) is stripped; a code fence (would split the message mid-fold) becomes
    three single quotes; a fold:: marker line is ZWSP-prefixed so
    render_fold_block ignores it (same downgrade formatting.py uses for
    unknown markers)."""
    line = re.sub(r"^\s*>+\s?", "", (line or "").rstrip())
    line = line.replace(_FENCE, "'" * 3)
    if line.lstrip().startswith("fold::"):
        line = "​" + line
    return line


def _dec_detail_body(dec_id):
    """물음 / 권고 / 선택지 body of worklog/decisions/<dec_id>.md, or None.

    The recommendation sits BEFORE the options on purpose (owner ruling of
    2026-09-23: it must be visible on the board, and the cap drops trailing
    lines first).  Missing file, none of the sections, or any parse failure
    -> None: the decision renders as its plain bullet (a missing detail never
    hides the decision itself).  Never raises."""
    try:
        if not dec_id:
            return None
        base = (os.environ.get("DOGANY_DECISIONS_DIR")
                or os.path.join(_ROOT_DIR, DECISIONS_DIR))
        with open(os.path.join(base, dec_id + ".md"), "r",
                  errors="replace") as fh:
            data = fh.read(DEC_DETAIL_READ_CAP)
        sections = {}
        current = None
        for raw in data.splitlines():
            m = re.match(r"^##\s+(\S.*?)\s*$", raw)
            if m:
                head = m.group(1)
                current = _DEC_HEAD_ALIASES.get(head)
                if current is None and _DEC_HEAD_AGENT_RE.match(head):
                    current = "권고"
                continue
            if current is not None:
                sections.setdefault(current, []).append(
                    _sanitize_fold_body_line(raw))
        parts = []
        q = "\n".join(sections.get("물음", [])).strip("\n")
        if q.strip():
            parts.append("물음: " + q)
        rec = "\n".join(sections.get("권고", [])).strip("\n")
        if rec.strip():
            parts.append("권고: " + rec)
        opts = "\n".join(sections.get("선택지", [])).strip("\n")
        if opts.strip():
            parts.append("선택지:\n" + opts)
        if not parts:
            return None
        lines = "\n".join(parts).split("\n")
        while len(lines) > 1 and _u16len("\n".join(lines)) > DEC_BODY_MAX_UNITS:
            lines.pop()
        body = "\n".join(lines)
        if _u16len(body) > DEC_BODY_MAX_UNITS:
            body = _cap_item(body, DEC_BODY_MAX_UNITS)
        return body
    except Exception:
        return None


def _collect_decision_items():
    """Pending decisions as dicts {"text", "dec_id", "body"}, ledger order.

    "text" is exactly the pre-Rev-15 item string (see _collect_decisions);
    "body" is the detail fold body (_dec_detail_body) or None.  Only lines
    of the form "- [YYYY-MM-DD] <summary>" count.  A MISSING ledger means
    CONFIRMED no pending decisions -> [].  Any other read failure returns
    None ("cannot confirm empty", DGN-541 fail-open empty-ban)."""
    path = os.environ.get("DOGANY_DECISIONS_FILE") or os.path.join(
        _ROOT_DIR, DECISIONS_FILE)
    items = []
    try:
        with open(path, "r", errors="replace") as fh:
            data = fh.read(DECISIONS_READ_CAP)
        for raw in data.splitlines():
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
            items.append({"text": item, "dec_id": dec_id,
                          "body": _dec_detail_body(dec_id)})
    except FileNotFoundError:
        return []
    except Exception:
        return None
    return items


def _collect_decisions():
    """Return the list of pending-decision summaries from _DECISIONS.md.

    Only lines of the form "- [YYYY-MM-DD] <summary>" count; everything else
    (header comments, blanks) is ignored.  A MISSING file means CONFIRMED no
    pending decisions -> [].  Any other read failure returns None ("cannot
    confirm empty", DGN-541 fail-open empty-ban).

    When a decision line carries a dec id, append a console deep link so the
    item reads: "<dec-id> <summary> -> <CONSOLE_BASE>/decision/<dec-id>".
    Items without a dec id are returned unchanged.  (Rev 15: the text view of
    _collect_decision_items -- same strings, same failure semantics.)
    """
    items = _collect_decision_items()
    if items is None:
        return None
    return [d["text"] for d in items]


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
# its dumb tail-cut, chopping the board tail.  Decisions are
# one-line ledger summaries; 300 units is generous.
DASHBOARD_ITEM_MAX_UNITS = 300
# Unpark-candidate ledger (DGN-534 T3): worklog/_UNPARK.md is regenerated
# idempotently by the ticket-hygiene gate scan (its SOLE writer, M4); the
# dashboard only DERIVES a view from it -- never recomputes gate state and
# never writes the ledger.  A MISSING ledger means this instance has no
# gate-scan machinery -> the section is omitted entirely (framework-
# canonical no-op for instances without it).
UNPARK_FILE = "worklog/_UNPARK.md"
# Freshness threshold (M5): the gate scan runs daily at 06:00, so a
# last-scan stamp older than 26h means the scan silently stopped --
# surface it.
UNPARK_STALE_SECS = 26 * 3600

# Console decision-action journal (DGN-536 T3b, Rev 11).  Sole writer = the
# console (product/decision-actions.md).  This script only READS it to derive
# a compact dashboard section.  A MISSING file means this instance has no
# console decision-action machinery -> the section is omitted entirely
# (framework-canonical no-op for instances without it).
DECISION_ACTIONS_FILE = "product/decision-actions.md"

# Journal block status markers (DGN-536 spec 2.3).
#   pending   -- awaiting leader reconcile (non-terminal, show in section).
#   terminal  -- applied / superseded* / failed (trigger forced header).
# The bracket form "## act-<id> [pending]" is the canonical write; other
# status values appear in decision-actions-status.md overlay (leader-owned),
# not in the journal itself.  Any act header WITHOUT "[pending]" is treated
# as terminal for section-rendering purposes.
_ACT_HEADER_RE = re.compile(
    r"^##\s+(act-[^\s\[]+)\s*(?:\[([^\]]*)\])?\s*$")
_ACT_FIELD_RE = re.compile(r"^([a-z_]+):\s*(.+?)\s*$")

# Ticket frontmatter line: "key: value" between the two '---' fences.
# Keys may be dotted ("display_title.ko", DGN-1754) so a locale-keyed
# display field parses as its own key.
_FRONTMATTER_KV_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_-]+)*):\s*(.*\S)?\s*$")

# Hard bound on frontmatter scan depth (DGN-1013 item D).  A plain key:value
# block is well under 20 lines, but a YAML block scalar (">" / "|") or a
# multi-line list value (grounded_on: / superseded_parts: etc.) pushes the
# closing fence further down -- confirmed instance cases close at line 28
# (7 lines past the old 20-line bound), silently dropping an otherwise-valid
# ticket from every frontmatter-driven count (e.g. _collect_recent_done).
# 200 gives a 7x margin over the observed overflow while still bounding the
# scan against a corrupt/fenceless file (this runs on every Stop hook).
_FRONTMATTER_SCAN_LINES = 200


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
    keys we need.  Anything malformed -> {} (ticket simply not shown).  The
    scan is bounded at _FRONTMATTER_SCAN_LINES (DGN-1013 item D) -- long
    enough for real block-scalar/list frontmatter, still bounded against a
    corrupt or fenceless file.
    """
    fm = {}
    try:
        with open(path, "r", errors="replace") as fh:
            first = fh.readline().strip()
            if first != "---":
                return {}
            for _ in range(_FRONTMATTER_SCAN_LINES):
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


# Worklog ticket filenames (Rev 15): the SAME knob done-claim-gate.py reads
# -- env TICKET_FILE_GLOB -> conf TICKET_FILE_GLOB -> "DGN-*.md" -- so one
# instance setting moves every ticket reader together.  An instance whose
# tickets are date-slug files (2026-08-25-live-push-incident.md) sets "*.md";
# the unset default keeps the pre-Rev-15 DGN-only scan byte-identical.
DEFAULT_TICKET_GLOB = "DGN-*.md"
# Top-level worklog files that are ledgers / conventions, never tickets.  Any
# "_"-prefixed name is reserved the same way (_DECISIONS.md, _UNPARK.md,
# _CONVENTION.md, _TEMPLATE.md ...).
_WORKLOG_RESERVED_PREFIX = "_"
# A frontmatter id that is a single token.  A value with whitespace or
# parentheses is prose ("(미부여 -- ...)", measured on a live worklog
# 2026-09-15: 13 open tickets carried such an id) and must never be shown as
# the ticket's identity -- the filename stem stands in.
_TICKET_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _ticket_glob():
    """The instance ticket-filename glob (see DEFAULT_TICKET_GLOB)."""
    return ((os.environ.get("TICKET_FILE_GLOB") or "").strip()
            or _conf_get("TICKET_FILE_GLOB").strip() or DEFAULT_TICKET_GLOB)


def _worklog_dir():
    """<root>/worklog; env DOGANY_WORKLOG_DIR overrides (test isolation)."""
    return os.environ.get("DOGANY_WORKLOG_DIR") or os.path.join(
        _ROOT_DIR, "worklog")


def _iter_worklog_tickets(with_frontmatter=True):
    """Yield (name, path, frontmatter) for every top-level worklog ticket.

    Top-level only (os.listdir does not recurse, so companion dirs such as
    worklog/decisions/ are naturally excluded), filtered by _ticket_glob()
    and the reserved "_" prefix.  with_frontmatter=False yields {} instead of
    parsing -- a caller that first filters by mtime or name opens only the
    files it keeps.  Raises OSError when the directory exists but cannot be
    listed (the caller decides fail-open vs "cannot confirm empty"); a
    MISSING directory yields nothing.
    """
    import fnmatch
    wdir = _worklog_dir()
    try:
        names = sorted(os.listdir(wdir))
    except FileNotFoundError:
        return
    pattern = _ticket_glob()
    for name in names:
        if name.startswith(_WORKLOG_RESERVED_PREFIX) or not name.endswith(".md"):
            continue
        if not fnmatch.fnmatchcase(name, pattern):
            continue
        path = os.path.join(wdir, name)
        if not os.path.isfile(path):
            continue
        yield name, path, (_ticket_frontmatter(path) if with_frontmatter
                           else {})


def _ticket_id(name, fm):
    """Canonical ticket id: the frontmatter id when it is a single token,
    else the filename stem.  Date-slug stems are identities in their own
    right (never rewritten into another namespace)."""
    tid = (fm or {}).get("id")
    if isinstance(tid, str):
        tid = tid.strip().strip("\"'")
        if _TICKET_ID_TOKEN_RE.match(tid):
            return tid
    return name[:-3] if name.endswith(".md") else name


def _mentions_ticket(texts, tid):
    """True iff any text mentions `tid` as a whole token (case-insensitive).

    Left boundary: not preceded by a letter or digit.  Right boundary: an id
    ending in a digit ("DGN-95") must not continue with a digit ("DGN-955")
    but may continue with a slug ("dgn-95-fix" in a branch name); any other
    id (a date slug) must not continue with a word character or hyphen, so
    "...-incident" never matches inside "...-incident-extra".  Never raises.
    """
    try:
        if not tid:
            return False
        tail = r"(?!\d)" if tid[-1:].isdigit() else r"(?![\w-])"
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(tid) + tail,
                         re.IGNORECASE)
        for t in texts or []:
            if t and pat.search(t):
                return True
    except Exception:
        return False
    return False


# Owner-facing ticket title (DGN-1754, owner 2026-09-27): the frontmatter
# "title" is the internal key (any language); what an owner surface shows is
# a DISPLAY title keyed by the instance language -- "display_title.<lang>",
# <lang> = the AGENT_LANG base code ("ko-KR" -> "ko").  No hardcoded
# language field.  Missing -> "" and the caller shows the id alone; the
# internal title is never substituted.
DISPLAY_TITLE_KEY = "display_title"


def _owner_lang():
    """AGENT_LANG base code (env DOGANY_LANG -> conf AGENT_LANG -> "en")."""
    lang = ((os.environ.get("DOGANY_LANG") or "").strip()
            or _conf_get("AGENT_LANG").strip() or "en")
    return re.split(r"[-_.]", lang.lower(), 1)[0] or "en"


def _ticket_display_title(fm, lang=None):
    """The ticket's display title for `lang` (default: _owner_lang()), one
    matching quote pair unwrapped; "" when absent.  Never raises."""
    try:
        key = "%s.%s" % (DISPLAY_TITLE_KEY, lang or _owner_lang())
        t = ((fm or {}).get(key) or "").strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1].strip()
        return t
    except Exception:
        return ""


def _collect_recent_done(limit=MAX_DONE_DISPLAY):
    """Recent completed work = worklog tickets with status done, newest first.

    The footer has no "recent completed" source of its own, so the dashboard
    reads the worklog frontmatter (id / title / status / updated) and sorts
    done tickets by updated date descending, ticket id as tiebreaker.
    Missing/unreadable worklog -> [] (section renders empty, fail-open).

    Rev 15: tickets come from _iter_worklog_tickets (TICKET_FILE_GLOB, prose
    ids rejected) and only files touched TODAY count (local calendar date,
    the convention both dev-crew forks had converged on) -- the board is a
    live-status surface, not a history log.
    """
    today = time.localtime()[:3]
    rows = []
    try:
        for name, path, _fm in _iter_worklog_tickets(with_frontmatter=False):
            try:
                if time.localtime(os.path.getmtime(path))[:3] != today:
                    continue
            except OSError:
                continue
            fm = _ticket_frontmatter(path)
            if fm.get("status") != "done":
                continue
            rows.append((fm.get("updated") or "", _ticket_id(name, fm),
                         _ticket_display_title(fm)))
    except Exception:
        return []
    rows.sort(reverse=True)
    out = []
    for updated, tid, title in rows[:limit]:
        line = "%s %s" % (tid, title) if title else tid
        if updated:
            line += " (%s)" % updated
        out.append(line)
    return out


# Ledger stamp line: "last-scan: 2026-07-24 06:00:12 KST".  The trailing
# zone label is display-only; the stamp is written in local time by the scan
# on this same machine, so epoch math against time.time() is sound.
_UNPARK_STAMP_RE = re.compile(
    r"^last-scan:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?::(\d{2}))?")


def _collect_unpark():
    """Derived view of the unpark-candidate ledger (DGN-534 T3, read-only).

    Returns None when the ledger file is ABSENT: this instance has no
    gate-scan machinery, so the dashboard renders no unpark section at all
    (framework-canonical no-op).

    Otherwise returns (stamp_epoch, stamp_display, items):
      stamp_epoch   -- last-scan time as epoch seconds, or None if unparsed.
      stamp_display -- "YYYY-MM-DD HH:MM" string, or None if absent.
      items         -- candidate lines, whitespace-squeezed, ledger order.
    An EXISTING but unreadable ledger returns (None, None, []): no stamp ->
    the section is FORCED with "(스캔 기록 없음)" so a broken ledger is
    visible, never silent (M5).  Env override DOGANY_UNPARK_FILE mirrors
    the DOGANY_DECISIONS_FILE test convention.
    """
    path = os.environ.get("DOGANY_UNPARK_FILE") or os.path.join(
        _ROOT_DIR, UNPARK_FILE)
    stamp_epoch = None
    stamp_display = None
    items = []
    zero = False
    try:
        fh = open(path, "r", errors="replace")
    except FileNotFoundError:
        return None            # no ledger -> no section (canonical no-op)
    except Exception:
        return None, None, []  # exists but unreadable -> forced visibility
    try:
        with fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = _UNPARK_STAMP_RE.match(line)
                if m:
                    stamp_display = "%s %s" % (m.group(1), m.group(2))
                    try:
                        stamp_epoch = time.mktime(time.strptime(
                            "%s %s:%s" % (m.group(1), m.group(2),
                                          m.group(3) or "00"),
                            "%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        stamp_epoch = None
                    continue
                if line.startswith("candidates:"):
                    # Count header; the item list speaks for itself -- except
                    # "candidates: 0" (Rev 15): the scan then writes a
                    # placeholder line ("(후보 없음)") that is NOT a candidate
                    # and must not hold the section (and the board) up.
                    if line.split(":", 1)[1].strip() == "0":
                        zero = True
                    continue
                items.append(re.sub(r"\s+", " ", line))
    except Exception:
        return None, None, []
    if zero:
        items = []
    return stamp_epoch, stamp_display, items


def _collect_console_actions():
    """Derived view of the console decision-action journal (DGN-536 T3b, Rev 11).

    Returns None when the journal file is ABSENT: this instance has no
    console decision-action machinery, so the dashboard renders no
    [콘솔액션] section at all (framework-canonical no-op).

    Otherwise returns (pending, has_failed):
      pending    -- list of compact one-liner strings for pending acts
                    (non-terminal, i.e. "[pending]" status), each formatted
                    as "<act-id> <action> <target> <age>d".
      has_failed -- True if any act header is present with a non-pending
                    status (failed / applied / superseded*).  When True the
                    section header is forced even if pending is empty (M5
                    analog: a stuck failed act must not become invisible).

    Journal format (DGN-536 spec 2.3):
      ## act-<epoch>[-bump] [pending]
      target: dec-NNN
      action: approve|hold|reject
      decided_at: YYYY-MM-DD HH:MM KST
      note: <text>

    Only blocks whose header bracket is exactly "[pending]" are counted as
    pending.  Any other bracket value (or no bracket) is treated as terminal.
    Env override DOGANY_DECISION_ACTIONS_FILE mirrors the test-isolation
    convention used by other file overrides.
    """
    path = os.environ.get("DOGANY_DECISION_ACTIONS_FILE") or os.path.join(
        _ROOT_DIR, DECISION_ACTIONS_FILE)
    try:
        fh = open(path, "r", errors="replace")
    except FileNotFoundError:
        return None          # no journal -> no section (canonical no-op)
    except Exception:
        return [], True      # exists but unreadable -> force header visible

    pending = []
    has_failed = False
    try:
        with fh:
            current_id = None
            current_status = None
            fields = {}
            def _flush_block():
                nonlocal has_failed
                if current_id is None:
                    return
                if current_status == "pending":
                    target = fields.get("target", "?")
                    action = fields.get("action", "?")
                    # Age from decided_at field (when the act was recorded).
                    age_str = ""
                    decided_at = fields.get("decided_at", "")
                    if decided_at:
                        try:
                            # Format: YYYY-MM-DD HH:MM KST (trailing zone ignored)
                            dt = time.strptime(decided_at[:16], "%Y-%m-%d %H:%M")
                            age_days = int(
                                (time.time() - time.mktime(dt)) / 86400)
                            age_str = " %dd" % age_days
                        except Exception:
                            pass
                    pending.append("%s %s %s%s" % (
                        current_id, action, target, age_str))
                else:
                    # Only flag has_failed for genuinely bad terminal states
                    # (failed / conflict).  Normal terminal states (applied,
                    # superseded*) indicate successful processing and should
                    # not force the header visible.
                    if (current_status.startswith("failed")
                            or "conflict" in current_status):
                        has_failed = True

            for raw in fh:
                line = raw.strip()
                m = _ACT_HEADER_RE.match(line)
                if m:
                    _flush_block()
                    current_id = m.group(1)
                    current_status = (m.group(2) or "").strip().lower() or "terminal"
                    fields = {}
                    continue
                if current_id is not None:
                    fm = _ACT_FIELD_RE.match(line)
                    if fm:
                        fields[fm.group(1)] = fm.group(2)
            _flush_block()
    except Exception:
        return [], True

    return pending, has_failed


def _console_actions_visible(console_actions):
    """Whether the [콘솔액션] section renders at all.

    None (journal absent) -> never (canonical no-op).
    has_failed True       -> always (force for stuck failed acts, M5 analog).
    pending non-empty     -> always.
    Zero pending + no failed -> suppressed (Rev 8 empty-section suppression).
    """
    if console_actions is None:
        return False
    pending, has_failed = console_actions
    return bool(pending) or has_failed


def _console_actions_header(console_actions):
    """Section header for [콘솔액션].

    Shows pending count.  When has_failed but no pending, appends a flag
    so a stuck failed act is visible.
    """
    if console_actions is None:
        return "[콘솔액션]"
    pending, has_failed = console_actions
    n = len(pending)
    if n == 0 and has_failed:
        return "[콘솔액션] (실패 act 있음)"
    return "[콘솔액션] %d건" % n


def _unpark_fresh(stamp_epoch):
    """True iff the last-scan stamp parsed AND is within UNPARK_STALE_SECS.

    An unparseable stamp counts as NOT fresh: a stamp we cannot verify gets
    the same forced visibility as a stale one (safe direction, M5).
    """
    return (stamp_epoch is not None
            and (time.time() - stamp_epoch) <= UNPARK_STALE_SECS)


def _unpark_visible(unpark):
    """Whether the unpark section renders at all (Rev 8 reconcile).

    None (ledger absent) -> never (canonical no-op).  Candidates present ->
    always.  Zero candidates: suppressed while the scan is FRESH (Rev 8
    empty-section suppression, owner spec) but FORCED when stale/unreadable
    so the silent-stop signal survives suppression (M5).
    """
    if unpark is None:
        return False
    stamp_epoch, _stamp_display, items = unpark
    return bool(items) or not _unpark_fresh(stamp_epoch)


def _unpark_header(stamp_epoch, stamp_display):
    """Section header carrying the scan freshness (M5).

    The header is never dropped by the length cut, so freshness stays
    visible even when every candidate item has been cut.  Not fresh ->
    explicit staleness flag (silent-stop detector).
    """
    if not stamp_display:
        return "[언파크 후보] (스캔 기록 없음)"
    if not _unpark_fresh(stamp_epoch):
        return "[언파크 후보] (스캔 %s -- 오래됨)" % stamp_display
    return "[언파크 후보] (스캔 %s)" % stamp_display


# Base sections a board plugin may hide because it supersedes them (Rev 15).
# The pending-decision and live sections are never hideable: they are the
# owner-facing signal every agent's board exists for.
HIDEABLE_BASE_SECTIONS = ("recent_done", "unpark", "console_actions")
# Marker the length cut stamps on a decision whose fold body it dropped, so
# the cut is visible (the decision itself stays).
DEC_BODY_DROPPED_MARK = " (상세 생략)"


def _board_title_line():
    """"<BOARD_TITLE> (갱신 HH:MM)" -- Rev 15: the freshness stamp rides the
    FIRST line (it can never be chopped by the bridge's dumb tail-cut, and a
    dead generator still shows up as a visibly stale time)."""
    return "%s (갱신 %s)" % (BOARD_TITLE, time.strftime("%H:%M"))


def _decision_block(decisions):
    """[결정대기] lines.  A str item (or a dict without a body) is the
    pre-Rev-15 "- <item>" bullet; a dict with a body is one fold:: block
    (collapsed = the item text, expanded = the detail body), each fold
    followed by a blank line so it transpiles to its OWN expandable quote."""
    if not decisions:
        return []
    lines = ["[결정대기]"]
    for d in decisions:
        if isinstance(d, dict) and d.get("body"):
            lines.append("fold:: " + _sanitize_fold_body_line(d["text"]))
            lines.extend(d["body"].split("\n"))
            lines.append("fold::end")
            lines.append("")
        else:
            lines.append("- " + (d["text"] if isinstance(d, dict) else d))
    if lines[-1] == "":
        lines.pop()
    return lines


def _build_dashboard(decisions, active_agents, recent_done, unpark=None,
                     console_actions=None, extra_sections=None, hidden=()):
    """Assemble the dashboard.md content string.

    Header: BOARD_TITLE line -- fixed name "작업대" (owner naming 2026-07-24;
    renamed from 상황판) with an optional per-instance emoji prefix (Rev 9)
    -- carrying the freshness stamp inline, "<title> (갱신 HH:MM)" (Rev 15;
    written by the GENERATOR only, the bridge never touches it, so a dead
    generator shows up as a visibly stale timestamp).
    Sections: 결정대기 / 콘솔액션 / <live label> / <plugin sections> /
    언파크 후보 / 최근 완료.
    `decisions`: str items, or Rev 15 dicts {"text", "body"} -- a body
    renders as one expandable fold (see _decision_block).
    The live section title comes from LIVE_LABEL (DGN-541 S2/S3: short form,
    per-instance via config/env, neutral default).  `unpark` is None (ledger
    absent -> no section) or the (stamp_epoch, stamp_display, items) triple
    from _collect_unpark(); visibility follows _unpark_visible (Rev 8
    empty-suppression + M5 forced staleness).  `console_actions` is None
    (journal absent -> no section, canonical no-op) or the (pending, has_failed)
    pair from _collect_console_actions(); visibility follows
    _console_actions_visible (Rev 8 + M5-analog forced header for failed acts).
    `extra_sections` (Rev 15): validated board-plugin sections, each
    {"key", "variants": [[line, ...], ...] (largest first), "pinned"}; they
    render after the live section in the given order.  `hidden`: base
    section keys a plugin supersedes (HIDEABLE_BASE_SECTIONS only).
    Length cut, lowest priority first, every step visible or reversible:
    decision fold bodies (oldest first, stamped DEC_BODY_DROPPED_MARK) ->
    최근 완료 -> 언파크 후보 items -> 콘솔액션 items -> plugin sections (last
    first: each steps down through its variants, then leaves; a "pinned"
    section is never cut -- its plugin bounds it) -> live -> 결정대기 tail
    (the last decision always stays).  The unpark HEADER and the 콘솔액션
    HEADER are never dropped: the freshness/failure signal must stay
    visible (DGN-534 T3 + DGN-536 T3b).
    """
    hidden = set(hidden or ()) & set(HIDEABLE_BASE_SECTIONS)
    # Cap every decision item: the section cut never drops the last decision,
    # so an uncapped runaway line would break the 3800-unit contract and let
    # the bridge tail-cut chop the freshness stamp.
    decs = []
    for d in decisions:
        if isinstance(d, dict):
            decs.append({"text": _cap_item(d.get("text") or ""),
                         "body": d.get("body") or None})
        else:
            decs.append({"text": _cap_item(d), "body": None})
    live = list(active_agents)
    done = ([] if "recent_done" in hidden
            else list(recent_done)[:MAX_DONE_DISPLAY])
    unpark_show = "unpark" not in hidden and _unpark_visible(unpark)
    unpark_items = []
    unpark_title = ""
    if unpark_show:
        stamp_epoch, stamp_display, unpark_items = unpark
        # Per-item cap: a runaway gate-prose line must not blow the size
        # budget in one item (same rationale as the decision-item cap).
        unpark_items = [_cap_item(x) for x in unpark_items]
        unpark_title = _unpark_header(stamp_epoch, stamp_display)
    ca_show = ("console_actions" not in hidden
               and _console_actions_visible(console_actions))
    ca_items = []
    ca_title = ""
    if ca_show:
        ca_pending, _ca_failed = console_actions
        ca_items = [_cap_item(x) for x in list(ca_pending)]
        ca_title = _console_actions_header(console_actions)
    # Plugin sections: [key, variant index, variants, pinned]; mutated by the
    # length cut below (index steps down, then the entry is removed).
    extras = [[sec["key"], 0, sec["variants"], bool(sec.get("pinned"))]
              for sec in (extra_sections or [])]
    title = _board_title_line()

    def _sec(header, items):
        # Rev 8 (owner spec 2026-07-24): omit an empty section entirely
        # (no "- 없음" placeholder). The board only renders when 결정대기 or
        # live has content, so a blank stub was pure noise.
        if not items:
            return []
        return [header] + ["- " + x for x in items]

    def _render():
        # The unpark block and the console-actions block bypass _sec: when
        # shown, the header stands even with zero items (forced-failure / forced-
        # staleness case, or all items length-cut).
        unpark_block = ([unpark_title] + ["- " + x for x in unpark_items]
                        if unpark_show else [])
        ca_block = ([ca_title] + ["- " + x for x in ca_items]
                    if ca_show else [])
        blocks = [_decision_block(decs), ca_block,
                  _sec("[%s]" % LIVE_LABEL, live)]
        blocks += [list(variants[idx]) for _k, idx, variants, _p in extras]
        blocks += [unpark_block, _sec("[최근 완료]", done)]
        lines = [title, ""]
        for block in blocks:
            if block:
                lines += block
                lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines) + "\n"

    def _shrink_extra():
        # Last cuttable plugin section first: next variant, else remove it.
        for i in range(len(extras) - 1, -1, -1):
            entry = extras[i]
            if entry[3]:
                continue
            if entry[1] + 1 < len(entry[2]):
                entry[1] += 1
            else:
                del extras[i]
            return True
        return False

    text = _render()
    while _u16len(text) > DASHBOARD_MAX_UNITS:
        bodied = [d for d in decs if d["body"]]
        if bodied:
            bodied[0]["body"] = None
            bodied[0]["text"] += DEC_BODY_DROPPED_MARK
        elif done:
            done.pop()
        elif unpark_items:
            unpark_items.pop()
        elif ca_items:
            ca_items.pop()
        elif _shrink_extra():
            pass
        elif live:
            live.pop()
        elif len(decs) > 1:
            decs.pop()
        else:
            # Single decision left; per-item cap guarantees it fits, so this
            # break is unreachable-over-budget in practice (kept as a
            # termination guard).
            break
        text = _render()
    return text


def _write_dashboard(active_agents, decisions, unpark=None,
                     console_actions=None, extra_sections=None, hidden=()):
    """Publish the workbench board into its OWN section of the shared pin
    surface (DGN-967).  Caller has already passed the ownership gate -- this
    function does no gating of its own."""
    # Owner 2026-10-01 15:34: the recent-done section took board space and
    # carried little meaning -> off by default on every board. The section
    # code stays (a plugin can no longer re-enable it; dispatch END notices
    # and the ticket ledger carry completion).
    hidden = set(hidden or ()) | {"recent_done"}
    done = []
    _board_publish(
        _build_dashboard(decisions, active_agents, done, unpark,
                         console_actions, extra_sections, hidden))


# ---------------------------------------------------------------------------
# Dashboard section store (DGN-967 shared-ownership compose model)
# ---------------------------------------------------------------------------
# dashboard.md is a DERIVED artifact: compose(dashboard-sections.json).
# One section per owner slug; the composer below is the file's sole physical
# writer.  The bridge contract is untouched -- "" (all sections empty) is
# still the HIDE signal that drives the debounced unpin+delete.

SECTIONS_FILE = "dashboard-sections.json"
SECTIONS_LOCK = ".dashboard-sections.lock"
# DGN-1768 r2 fresh-pin request marker; must match bridge/dashboard.py
# ROTATE_REQUEST_NAME (sibling of dashboard_state.json in BOT_DATA_DIR).
ROTATE_REQUEST_FILE = "dashboard_rotate.request"
FRESH_WAIT_STEP = 0.25  # seconds between marker polls under --wait
# Reserved owner slugs: the workbench board (Stop-hook path only) and the
# adopted foreign-direct-write section (adoption path only).
BOARD_OWNER = "board"
LEGACY_OWNER = "legacy"
RESERVED_OWNERS = (BOARD_OWNER, LEGACY_OWNER)
_OWNER_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# Adopted direct writes expire after 24h: a foreign writer that died (e.g. a
# workout card with no session-closeout clear) must not pin stale content
# forever.  Every fresh direct write re-adopts and refreshes the clock.
# API sections default ttl=0 (no expiry) -- lifecycle is the owner's call,
# --ttl is the opt-in backstop.
LEGACY_TTL_SECS = 24 * 3600
# Stop hook must never block a turn: bounded non-blocking lock retry, then
# fail open (skip the write; previous composed content stays on disk).
LOCK_RETRY_SECS = 2.0
LOCK_RETRY_STEP = 0.05


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path, text):
    """tmp + os.replace (heartbeat.touch precedent); creates parent dirs."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _load_sections(manifest_path):
    """Read the section store.  Returns (sections, composed_sha).

    Fail-open: an absent or corrupt manifest reads as ({}, None) -- the
    compose cycle then re-migrates from whatever dashboard.md currently
    holds (adoption rule), so a damaged store can never brick the surface.
    Malformed individual entries are dropped, valid siblings kept.
    """
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    raw = data.get("sections")
    composed_sha = data.get("composed_sha")
    if not isinstance(composed_sha, str):
        composed_sha = None
    sections = {}
    if isinstance(raw, dict):
        for owner, meta in raw.items():
            if not isinstance(owner, str) or not _OWNER_RE.match(owner):
                continue
            if not isinstance(meta, dict):
                continue
            text = meta.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            ts = meta.get("ts")
            ttl = meta.get("ttl")
            sections[owner] = {
                "text": text,
                "ts": ts if type(ts) is int else 0,
                "ttl": ttl if type(ttl) is int and ttl > 0 else 0,
            }
    return sections, composed_sha


def _render_domain(sections, skip=()):
    """Deterministic render of all NON-board sections (owner-slug order)."""
    parts = []
    for owner in sorted(sections):
        if owner == BOARD_OWNER or owner in skip:
            continue
        text = (sections[owner].get("text") or "").strip("\n")
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)


def _compose_text(sections, skip=()):
    """Compose dashboard.md from the store: board FIRST (Rev 16, DGN-1020
    owner 2026-09-26 -- the workbench is what the owner actually watches, so
    it must lead; its 갱신 stamp already rides the board's own title line
    since Rev 15, so it stays visible regardless of position), domain
    sections after (owner-slug order).  All sections empty -> "" (the bridge
    HIDE signal, unchanged semantics).  Domain-only output is byte-identical
    to the pre-DGN-967 board-absent write; board-only output is unchanged
    too (still just the board's own text)."""
    domain = _render_domain(sections, skip)
    board = sections.get(BOARD_OWNER, {}).get("text") or ""
    if not board.strip():
        board = ""
    if not domain and not board:
        return ""
    if not board:
        return domain + "\n"
    if not domain:
        return board
    return board + "\n" + domain + "\n"


def _is_own_domain_tail(sections, text):
    """True if `text` ends in exactly the domain content `sections` would
    currently render -- the crash-residue / no-foreign-write signature now
    that the board sits at the FRONT (Rev 16) instead of the back.

    Before Rev 16 the board was always last, so "everything from its title
    line to EOF" could be discarded sight unseen and whatever remained was,
    unambiguously, domain content.  With domain now last instead, the same
    trick anchors the other end: our own domain sections are exactly
    reproducible from `sections` (_render_domain), so if that exact text is
    the tail of `text`, whatever precedes it -- an old board, a freshly
    re-stamped one, anything -- is by elimination the board, and it does
    not matter which: it gets discarded unseen either way.
    """
    domain = _render_domain(sections).rstrip("\n")
    if not domain:
        return False
    text = text.rstrip("\n")
    return text == domain or text.endswith("\n\n" + domain)


def _is_bare_board_head(text):
    """True if `text` opens with our board title (bare or 갱신-stamped) and
    nothing else needs to be kept -- the old pre-DGN-967 board-only
    dashboard.md, or a fresh board with no domain sections yet.  Used by the
    adoption rule so such a file migrates to "nothing to adopt" instead of
    duplicating itself as a legacy section.  Both the decorated title (emoji
    prefix) and the bare title text are matched: a conf change between
    writes must not turn our own stale board into adopted foreign content.
    """
    text = text.strip("\n")
    if not text:
        return True
    head = text.splitlines()[0].strip()
    # Rev 15: the board title carries its freshness stamp inline
    # ("<title> (갱신 HH:MM)"); bare equality alone would miss our own
    # stamped board and re-adopt it as foreign "legacy" content (the board
    # would then render twice).  Guard carried from a dev fork Rev 12c.
    return (head == BOARD_TITLE or head == BOARD_TITLE_TEXT
            or head.startswith(BOARD_TITLE + " (갱신 ")
            or head.startswith(BOARD_TITLE_TEXT + " (갱신 "))


def _adopt_foreign(sections, composed_sha, current_text, now):
    """Adoption rule (DGN-967 spec 5): a dashboard.md that no longer matches
    the last composed text was replaced by a foreign DIRECT writer (a domain
    agent that has not adopted the section API).  Capture its content as the
    reserved "legacy" section so the next compose preserves instead of
    clobbers it.  A foreign EMPTY write is that writer's hide signal ->
    clear the legacy section.  Our own composed artifact (crash residue
    between the two atomic writes) is recognized and skipped."""
    if composed_sha is not None and _sha256(current_text) == composed_sha:
        return  # no foreign write since our last compose
    if _is_own_domain_tail(sections, current_text):
        return  # our own artifact (crash between dashboard/manifest writes)
    core = current_text.rstrip("\n")
    if not core or _is_bare_board_head(core):
        # Board-only remains (first-run migration of an old board file) or a
        # foreign empty write: nothing foreign to keep.
        sections.pop(LEGACY_OWNER, None)
        return
    sections[LEGACY_OWNER] = {"text": core, "ts": now, "ttl": LEGACY_TTL_SECS}


def _sweep_expired(sections, now):
    for owner in list(sections):
        meta = sections[owner]
        ttl = meta.get("ttl") or 0
        if ttl > 0 and (now - (meta.get("ts") or 0)) > ttl:
            del sections[owner]


def _budget_skip(sections):
    """Render-time exclusion set keeping the composed output inside
    DASHBOARD_MAX_UNITS.  Drop whole sections from the OUTPUT only (the
    store keeps the owner's data): legacy first, then domain oldest-ts
    first.  The board is never dropped (it self-cuts to the same budget).
    A single oversized remaining section is left for the bridge's dumb
    tail-cut safeguard -- dropping it would flip the file to the HIDE
    signal and delete the pin over a length problem."""
    skip = set()
    order = sorted(
        (o for o in sections if o != BOARD_OWNER),
        key=lambda o: (0 if o == LEGACY_OWNER else 1,
                       sections[o].get("ts") or 0),
    )
    board_has = bool((sections.get(BOARD_OWNER, {}).get("text") or "").strip())
    keep_min = 0 if board_has else 1
    while len(order) > keep_min:
        if _u16len(_compose_text(sections, skip)) <= DASHBOARD_MAX_UNITS:
            break
        skip.add(order.pop(0))
    return skip


def _lock_acquire(fh):
    """Bounded non-blocking flock.  True = acquired (or no fcntl platform)."""
    if fcntl is None:
        return True
    deadline = time.monotonic() + LOCK_RETRY_SECS
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_RETRY_STEP)


def _compose_locked(mutate=None):
    """One serialized read-adopt-mutate-compose-write cycle.

    Order inside the lock: load store -> adopt foreign direct writes ->
    TTL sweep -> apply the caller's mutation -> drop empty sections ->
    budget cut -> atomic dashboard.md write -> atomic manifest write
    (composed_sha = sha of exactly what was written).  Returns True on a
    completed write, False on lock/IO failure (caller decides fail-open
    vs exit code; the previous composed content stays on disk either way).
    """
    data_dir = _bot_data_dir()
    dash_path = os.path.join(data_dir, DASHBOARD_FILE)
    manifest_path = os.path.join(data_dir, SECTIONS_FILE)
    lock_path = os.path.join(data_dir, SECTIONS_LOCK)
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lf:
            if not _lock_acquire(lf):
                return False
            sections, composed_sha = _load_sections(manifest_path)
            now = int(time.time())
            try:
                with open(dash_path, "r", encoding="utf-8") as fh:
                    current = fh.read()
            except OSError:
                current = ""
            _adopt_foreign(sections, composed_sha, current, now)
            _sweep_expired(sections, now)
            if mutate is not None:
                mutate(sections)
            for owner in list(sections):
                if not (sections[owner].get("text") or "").strip():
                    del sections[owner]  # empty section == cleared
            text = _compose_text(sections, _budget_skip(sections))
            _atomic_write(dash_path, text)
            _atomic_write(manifest_path, json.dumps(
                {"version": 1, "composed_sha": _sha256(text),
                 "sections": sections},
                ensure_ascii=False))
            return True
    except Exception:
        return False


def _board_publish(text):
    """Set the reserved board section and recompose (Stop-hook path)."""
    def mutate(sections):
        sections[BOARD_OWNER] = {"text": text, "ts": int(time.time()),
                                 "ttl": 0}
    return _compose_locked(mutate)


def _board_clear():
    """Confirmed-empty board: clear ONLY the board section and recompose.
    With no other sections the composed output is "" (bridge HIDE signal,
    exactly the pre-DGN-967 behavior); with a domain section present the
    pin now survives -- the whole point of DGN-967."""
    return _compose_locked(lambda sections: sections.pop(BOARD_OWNER, None))


def _fresh_request(owner, wait):
    """DGN-1768 r2: drop the rotate-request marker for the bridge; with
    wait (seconds, may be None) block until the bridge consumed it.
    0 = requested (and consumed, when waiting), 1 = write failed,
    2 = not consumed in time (marker withdrawn)."""
    path = os.path.join(_bot_data_dir(), ROTATE_REQUEST_FILE)
    try:
        _atomic_write(path, json.dumps({"owner": owner,
                                        "ts": int(time.time())}))
    except OSError as e:
        sys.stderr.write("fresh request write failed: %s\n" % e)
        return 1
    if wait is None:
        return 0
    deadline = time.monotonic() + wait
    while os.path.exists(path):
        if time.monotonic() >= deadline:
            try:
                os.unlink(path)
            except FileNotFoundError:
                return 0  # consumed right at the deadline
            except OSError:
                pass
            sys.stderr.write("fresh pin not confirmed within %ss\n" % wait)
            return 2
        time.sleep(FRESH_WAIT_STEP)
    return 0


def _pin_cli(argv):
    """Owner API: --pin-set <owner> [--file PATH] [--ttl SECS]
    [--fresh [--wait SECS]] (content from --file or stdin; empty content ==
    clear) / --pin-clear <owner>.
    Deliberate local callers (domain scripts), so no session-ownership gate;
    meaningful exit codes instead of the hook's fail-open silence."""
    mode = argv[0]
    rest = list(argv[1:])
    if not rest:
        sys.stderr.write("usage: status-footer.py %s <owner> "
                         "[--file PATH] [--ttl SECS] "
                         "[--fresh [--wait SECS]]\n" % mode)
        return 1
    owner = rest.pop(0)
    if not _OWNER_RE.match(owner) or owner in RESERVED_OWNERS:
        sys.stderr.write("invalid owner slug: %r (grammar [a-z0-9_-]{1,32}, "
                         "reserved: %s)\n" % (owner, "/".join(RESERVED_OWNERS)))
        return 1
    if mode == "--pin-clear":
        if rest:
            sys.stderr.write("--pin-clear takes no extra arguments\n")
            return 1
        return 0 if _compose_locked(
            lambda sections: sections.pop(owner, None)) else 1
    file_path = None
    ttl = 0
    fresh = False
    wait = None
    while rest:
        flag = rest.pop(0)
        if flag == "--file" and rest:
            file_path = rest.pop(0)
        elif flag == "--ttl" and rest:
            try:
                ttl = max(0, int(rest.pop(0)))
            except ValueError:
                sys.stderr.write("--ttl expects an integer (seconds)\n")
                return 1
        elif flag == "--fresh":
            fresh = True
        elif flag == "--wait" and rest:
            try:
                wait = max(0.0, float(rest.pop(0)))
            except ValueError:
                sys.stderr.write("--wait expects a number (seconds)\n")
                return 1
        else:
            sys.stderr.write("unknown/incomplete argument: %s\n" % flag)
            return 1
    if wait is not None and not fresh:
        sys.stderr.write("--wait requires --fresh\n")
        return 1
    try:
        if file_path is not None:
            with open(file_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = sys.stdin.read()
    except OSError as e:
        sys.stderr.write("content read failed: %s\n" % e)
        return 1
    text = text.rstrip()
    if not text:
        ok = _compose_locked(lambda sections: sections.pop(owner, None))
    else:
        def mutate(sections):
            sections[owner] = {"text": text, "ts": int(time.time()),
                               "ttl": ttl}
        ok = _compose_locked(mutate)
    if not ok:
        return 1
    return _fresh_request(owner, wait) if fresh else 0


def _agent_stopped_cli(argv):
    """Stopper API: --agent-stopped <agent-id> [--reason TEXT].

    Called BY the actor performing the stop, as part of the stop -- that is
    the whole design: the stop is the only moment at which the difference
    between "killed on purpose" and "died" is known, so it is the only honest
    place to record it.  Same shape as _pin_cli: deliberate local caller, no
    session-ownership gate, meaningful exit codes instead of the Stop hook's
    fail-open silence.
    """
    rest = list(argv[1:])
    if not rest:
        sys.stderr.write("usage: status-footer.py --agent-stopped <agent-id> "
                         "[--reason TEXT]\n")
        return 1
    agent_id = rest.pop(0)
    if not _STOP_ID_RE.match(agent_id):
        sys.stderr.write("invalid agent id: %r (grammar [0-9a-fA-F]{1,64})\n"
                         % agent_id)
        return 1
    reason = ""
    while rest:
        flag = rest.pop(0)
        if flag == "--reason" and rest:
            reason = rest.pop(0)
        else:
            sys.stderr.write("unknown/incomplete argument: %s\n" % flag)
            return 1
    if not _record_agent_stop(agent_id, reason):
        sys.stderr.write("stop record write failed for %s\n" % agent_id)
        return 1
    return 0


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
# Board sections plugin (Rev 15, DGN-1020) -- the one extension seam
# ---------------------------------------------------------------------------
# Default location = the pack payload path (packs/CONTRACT.md: a pack's
# payload/routines/bundle/* lands in <instance>/routines/bundle/*, and the
# framework update never deletes it).  env DOGANY_BOARD_PLUGIN -> conf
# DASHBOARD_BOARD_PLUGIN override the path (relative = from the instance
# root); "none" (or "off") disables.
BOARD_PLUGIN_FILE = os.path.join("routines", "bundle", "workbench_sections.py")
BOARD_PLUGIN_API = 1
# A "pinned" plugin section is exempt from the length cut, so it must bound
# itself; one over this size is treated as an ordinary (cuttable) section.
PLUGIN_PINNED_MAX_UNITS = 1200
_PLUGIN_KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _plugin_debug(msg):
    """One stderr line when DOGANY_DASHBOARD_DEBUG is set; silent otherwise
    (the Stop hook never talks on a normal turn)."""
    if os.environ.get("DOGANY_DASHBOARD_DEBUG"):
        try:
            sys.stderr.write("board-plugin: %s\n" % msg)
        except Exception:
            pass


def _board_plugin_path():
    """The configured plugin path, or "" when disabled."""
    raw = os.environ.get("DOGANY_BOARD_PLUGIN")
    if raw is None:
        raw = _conf_get("DASHBOARD_BOARD_PLUGIN") or BOARD_PLUGIN_FILE
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("none", "off"):
        return ""
    raw = os.path.expanduser(raw)
    return raw if os.path.isabs(raw) else os.path.join(_ROOT_DIR, raw)


def _load_board_plugin():
    """(module or None, ok).

    No plugin file / disabled -> (None, True): the base board, nothing to
    wait for.  A file that exists but fails to import, declares another
    PLUGIN_API or has no callable build_sections -> (None, False): the base
    board renders, and ok=False keeps a confirmed-empty clear from hiding
    whatever the broken plugin would have shown (DGN-541 empty-ban).  Loaded
    only from main(): importing this file as a library (bg-job-notice.py,
    subagent-stop-record.py) never executes plugin code.  Never raises."""
    path = _board_plugin_path()
    if not path or not os.path.isfile(path):
        return None, True
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dogany_board_plugin", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        _plugin_debug("import failed: %s" % type(exc).__name__)
        return None, False
    if getattr(mod, "PLUGIN_API", None) != BOARD_PLUGIN_API:
        _plugin_debug("PLUGIN_API mismatch")
        return None, False
    if not callable(getattr(mod, "build_sections", None)):
        _plugin_debug("no build_sections")
        return None, False
    return mod, True


class _HostView(object):
    """Attribute view of THIS module's globals -- the plugin's "host".

    sys.modules[__name__] is not always this module (a loader that exec's
    the file via spec_from_file_location without registering it, as the
    test suites and bg-job-notice.py do); the live globals dict always is."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name)


def _plugin_ctx(live_rows, active_agents, live_records, completed_descs,
                review_descs, raw_decisions):
    """The v1 context a plugin renders from -- everything already collected
    this turn; the plugin reads, it never re-scans the transcript/ledger.

      host             this module: reuse its helpers (_cap_item, _u16len,
                       _conf_get, _iter_worklog_tickets, _ticket_id,
                       _mentions_ticket, _ticket_display_title,
                       _owner_lang, _live_stamp, _cap_with_suffix,
                       _bot_data_dir, _ROOT_DIR ...) instead of copying them
      live_descs       the live labels, unstamped (transcript then ledger)
      live_rows        the same rows as rendered (stamped)
      live_records     aligned 1:1 with live_descs: {"desc",
                       "model_display", "start_epoch"} or None
      completed_descs  recently completed subagent descriptions
      review_descs     ledger pending-review rows ("branch item")
      decisions        pending-decision item strings, None = read FAILED
    """
    return {
        "api": BOARD_PLUGIN_API,
        "host": _HostView(),
        "live_descs": list(active_agents or []),
        "live_rows": list(live_rows or []),
        "live_records": list(live_records or []),
        "completed_descs": list(completed_descs or []),
        "review_descs": list(review_descs or []),
        "decisions": (None if raw_decisions is None
                      else list(raw_decisions)),
    }


def _plugin_sections(plugin, ctx):
    """Validated plugin output -> (sections, hidden, triggered, ok).

    Contract v1: build_sections(ctx) returns a dict
      {"sections": [section, ...], "ok": bool, "hide": [base key, ...]}
    section = {"key": slug, "lines": [str, ...]  or  "variants":
               [[str, ...], ...] (largest first, the length cut steps down),
               "trigger": bool (may alone put the board up),
               "pinned": bool (exempt from the length cut; must stay under
               PLUGIN_PINNED_MAX_UNITS)}
    "ok" False = one of the plugin's own collectors FAILED, so an empty
    result must not be read as confirmed empty.  An invalid section is
    dropped on its own; an exception or a non-dict result drops them all and
    reports ok=False.  The plugin can degrade the board, never blank it."""
    if plugin is None:
        return [], (), False, True
    try:
        result = plugin.build_sections(ctx)
    except Exception as exc:
        _plugin_debug("build_sections raised: %s" % type(exc).__name__)
        return [], (), False, False
    if not isinstance(result, dict):
        _plugin_debug("result is not a dict")
        return [], (), False, False
    ok = result.get("ok", True) is not False
    hidden = tuple(k for k in (result.get("hide") or ())
                   if k in HIDEABLE_BASE_SECTIONS)
    sections = []
    triggered = False
    seen = set()
    for sec in result.get("sections") or ():
        try:
            key = sec.get("key")
            if not (isinstance(key, str) and _PLUGIN_KEY_RE.match(key)) \
                    or key in seen:
                continue
            raw = sec.get("variants")
            if raw is None:
                raw = [sec.get("lines")]
            variants = []
            for v in raw:
                if (isinstance(v, (list, tuple)) and v
                        and all(isinstance(x, str) for x in v)):
                    body = []
                    for x in v:
                        body.extend(x.split("\n"))
                    if _u16len("\n".join(body)) <= DASHBOARD_MAX_UNITS:
                        variants.append(body)
            if not variants:
                continue
            pinned = bool(sec.get("pinned")) and _u16len(
                "\n".join(variants[0])) <= PLUGIN_PINNED_MAX_UNITS
            seen.add(key)
            sections.append({"key": key, "variants": variants,
                             "pinned": pinned})
            triggered = triggered or bool(sec.get("trigger"))
        except Exception:
            continue
    return sections, hidden, triggered, ok


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
            background = {}
            # Rev 15: the collectors also hand back per-row detail (model,
            # start time, completed/review signals) from the SAME pass.
            sub_detail = {}
            transcript_agents = _collect_active_subagents(
                transcript_path, background, sub_detail)
            _sync_background_notices(background)
            ledger_detail = {}
            ledger_agents = _collect_ledger_running(ledger_detail)
            decision_items = _collect_decision_items()
            raw_decisions = (None if decision_items is None
                             else [d["text"] for d in decision_items])
            # Unpark collector has NO failure state (absent ledger = canonical
            # no-op, unreadable ledger = forced-visible section), so it does
            # not participate in collect_ok.
            unpark = _collect_unpark()
            # Console-actions collector has NO failure state (absent journal =
            # canonical no-op, unreadable journal = forced-visible section),
            # so it does not participate in collect_ok either.
            console_actions = _collect_console_actions()
            collect_ok = (
                transcript_agents is not None
                and ledger_agents is not None
                and raw_decisions is not None
            )
            active_agents = (transcript_agents or []) + (ledger_agents or [])
            decisions = decision_items or []
            # Rev 15: every live row gets "<Model>: <name> · HH:MM (N분~)".
            live_records = (
                (list(sub_detail.get("records") or [])
                 if transcript_agents is not None else [])
                + (list(ledger_detail.get("records") or [])
                   if ledger_agents is not None else []))
            live_rows = _format_live_items(active_agents, live_records)
            # Rev 15: board sections plugin (fail-open, see _plugin_sections).
            plugin, plugin_ok = _load_board_plugin()
            extra, hidden, extra_trigger, sections_ok = _plugin_sections(
                plugin, _plugin_ctx(
                    live_rows, active_agents, live_records,
                    sub_detail.get("completed"), ledger_detail.get("review"),
                    raw_decisions))
            collect_ok = collect_ok and plugin_ok and sections_ok
            try:
                if (active_agents or decisions or extra_trigger
                        or ("unpark" not in hidden
                            and _unpark_visible(unpark))
                        or ("console_actions" not in hidden
                            and _console_actions_visible(console_actions))):
                    # Display trigger (DGN-541 S1 + DGN-534 T3 + DGN-536 T3b):
                    # >=1 pending decision OR >=1 working subagent OR a visible
                    # unpark section OR a visible console-actions section ->
                    # fill the board.  Unpark candidates and pending console
                    # acts are actionable; a stale scan or a stuck failed act
                    # is a health signal that must not vanish with the board
                    # (M4/M5); recent-completed history stays extra info only,
                    # never a standalone trigger (D2).  Rev 15: a plugin
                    # section that declares trigger=True is actionable too.
                    _write_dashboard(live_rows, decisions, unpark,
                                     console_actions, extra, hidden)
                elif collect_ok:
                    # CONFIRMED empty board -> clear ONLY the board section
                    # (DGN-967).  With no domain sections the composed file
                    # is empty and the bridge's debounced delete state
                    # machine unpins the message, exactly as before; with a
                    # domain section present the pin survives intact.
                    _board_clear()
                # else: a collector failed with nothing to show -> do NOT
                # touch the surface at all (a false-empty would escalate
                # into a visible pin delete); the previous composed
                # dashboard.md content stays.
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
    # DGN-967 owner API: explicit CLI modes bypass the Stop-hook path (and
    # its fail-open silence) entirely.
    if len(sys.argv) > 1 and sys.argv[1] in ("--pin-set", "--pin-clear"):
        sys.exit(_pin_cli(sys.argv[1:]))
    # Rev 14 stopper API: records a deliberate stop so the agent leaves the
    # live section instead of being reported as a presumed death.  The
    # routine wiring is routines/subagent-stop-record.py (PostToolUse); this
    # flag is for a caller that performs a stop some other way.
    if len(sys.argv) > 1 and sys.argv[1] == "--agent-stopped":
        sys.exit(_agent_stopped_cli(sys.argv[1:]))
    main()
