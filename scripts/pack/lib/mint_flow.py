#!/usr/bin/env python3
"""DGN-1683 WP1: mint verb contract + single flow state.

Doctrine D11 (agent carries, ledger holds): the agent passes the owner's words
to this verb; the verb owns state, gates and screens. Nothing here intercepts
an owner message.

Verbs (one JSON object on stdout per call, never a token, never screen copy):
  open   [--input <owner request>|-]          make-open: WAIT_CONTEXT, nothing sent
  open   --browse --lead <one line>           browse: every eligible pack first
  next   --input <owner text | "N. label" | token message>|- [--lead <one line>]
         [--asked <the agent's consult question>] [--more] [--fit <pack_id>|none]
  status
  cancel
  sweep                       backstop for cron/bridge: same JSON as status, but
                              also aborts a build flow whose worker died
  table                       publish the state table (data) and exit

Instance context is passed explicitly (--instance-root); request context via
flags or DOGANY_MINT_* env (owner id, chat, thread, source message id, request
id, runtime epoch). Nothing is guessed: no epoch -> flow cannot be used.

State lives in <instance-root>/config/mint-flow.json (0600, atomic replace,
fsync, short exclusive flock on config/mint-flow.lock). It is the ONLY
conversation-state store; it never holds a token (anything token-shaped is
masked before it touches state or logs).

Every emission goes through emit(), which refuses any (state, waiting_for,
note) triple that is not a row of mint_flow_table.json (fail closed to BLOCKED).

Seams left for later work packages (each stub answers "not_implemented", which
the table covers as state NOT_IMPLEMENTED):
  WP2 TokenAdapter  check(raw_input, flow, ctx)
  WP3 Screens       options(screen_id, flow) / send(screen_id, flow, ctx)
  WP4 Worker        start(flow, secret=, persist=) / stop(flow) / alive(flow)
                    Cleanup.run(flow)      (implemented in lib/mint_flow_worker.py)
  WP5 Readiness     check(flow)
WP4 also calls the engine hook points: mark_artifacts, set_worker, set_accept,
advance, complete_done, finish_failed. The `sweep` verb (cron/bridge backstop,
grill v4 fix 1) reconciles an overdue or dead-worker build flow and re-delivers
a pending terminal screen.

DGN-1674 (dec-170): a step that would send an owner choice screen (the
catalog -- from `open`, or from `next` taking a route back to it) needs an
agent-written one-line lead for THAT screen. No usable lead -> nothing is
written or sent; the verb emits BLOCKED/LEAD_REQUIRED plus "lead_problem"
(missing | rejected) and "rerun_with", and exits 3. The lead is kept with the
screen so a re-send (status/sweep) carries it too. Its words are the agent's;
the engine holds no intro copy.

DGN-1702 (dec-182): consult before recommending. A make-open sends nothing:
the flow waits in WAIT_CONTEXT (agent_utterance "consult") while the agent
asks, in its own words, what situation the new agent is for. Each owner
answer is kept verbatim (masked, capped) with the agent's question as
`asked`; `--more` keeps the consult open for one more answer (at most
CONTEXT_MAX). The closing answer matches the catalog against the owner's own
words: a match sends the catalog (needs --lead), no match ends the flow as
CANCELLED/NO_FIT (agent_utterance "no_fit", nothing sent). A pack whose
domain already has a dedicated agent ends as CANCELLED/DOMAIN_OCCUPIED
(agent_utterance "occupied"). The token prompt is never reached without the
owner's recorded context: a browse pick enters WAIT_CONTEXT first.

DGN-1702 fix round (grill BLOCK): the consult close never decides fit by
substring. The closing `next` out of WAIT_CONTEXT carries the agent's
judgement `--fit <pack_id>|none`, made from the pack taglines against the
owner's own words. Missing -> BLOCKED/FIT_REQUIRED with the `packs` fact,
nothing written, exit 3; an id that is not an eligible pack ->
INPUT_NOT_ACCEPTED, nothing written; `none` -> CANCELLED/NO_FIT. The fit is
kept as flow['fit'] and the catalog shows that pack only; the owner's tap is
the confirmation. A browse pick is already the owner's choice, so its
consult answer resumes without --fit. Only the emission that enters or
extends WAIT_CONTEXT grants "consult" (note ASK_OWNER, fact answers_left); a
later status there is WAITING_ON_OWNER/none. During WAIT_CONTEXT, `open
--browse` sends every eligible pack (context kept) and `open` with text is
a consult answer.

DGN-1702 round 3 (owner rehearsal 2026-09-26, A1-A4): a request that
clearly names ONE pack gets no consult interview. The make-open carries the
agent's judgement `--fit` made from the request itself:
  --fit <pack_id>           one pack: the catalog goes out at once (needs
                            --lead, a CONFIRM lead: the agent's reading of
                            the request as a question + "an official pack
                            exists"); a single-pack card closes with the
                            owner-approved free-text invitation
  --fit <id>,<id>[,...]     several packs match: those candidates, the lead
                            asks which
  --fit consult             the request does not tell which: WAIT_CONTEXT,
                            the agent asks the context (the only consult)
  --fit none                nothing fits: CANCELLED/NO_FIT, the agent speaks
Missing -> BLOCKED/FIT_REQUIRED (nothing written, exit 3). A make-open with
no request words and nothing to restart from still consults. The lead is judged ONCE,
when the step is recorded (Engine._lead), stored with the screen and rendered
verbatim in the same message as the card (A2: the card is never sent without
it). Owner free text at a catalog is the owner telling what they meant: it
is kept as a consult answer and the agent re-judges --fit (or asks with
--more). A tap on an expiry/restart button ("처음부터 만들기", with or
without its number) is never kept as a consult answer.

DGN-1787 (2026-09-28, main agent): (1) a restart is judged by the bridge runtime
epoch alone; a boot id that differs under the same runtime epoch is not a
restart (macOS boot id = kern.bootsessionuuid now, not the wall-clock-derived
kern.boottime). (2) "처음부터 만들기" on an expiry screen restarts from what
the owner already said: the expired flow's request, consult answers, fit and
recorded catalog lead (flow['origin']) -> the same card again. A fit never
judged -> FIT_REQUIRED with the fact `request`; answers only -> the consult
continues with them; nothing known -> the plain consult. (3) Every persisted
state change is appended to <instance>/.mint-flow/<flow_id>.transitions.jsonl
(from/to, reason, deadlines, flow and caller epoch, expiry detail; token-masked,
TRANSITIONS_MAX_BYTES per flow, newest TRANSITIONS_KEEP logs kept).

Exit code: 0 whenever a table row was emitted (including BLOCKED), 1 for an
unexpected internal error (a BLOCKED row is still emitted), 2 for a malformed
request, 3 for LEAD_REQUIRED / FIT_REQUIRED (rerun the same command with
--lead / --fit).
"""
import argparse
import copy
import errno
import fcntl
import inspect
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE_PATH = HERE / 'mint_flow_table.json'

SCHEMA = 1
IDLE_S = 1800            # WAIT_* idle timeout (spec 5); only accepted input refreshes
EXEC_S = 1200            # bounded execution deadline for BUILDING..RECORDING
LOCK_S = 5.0             # how long a verb waits for the state lock
SEND_ATTEMPTS = 2        # sends per call before the pending screen is left for the next call
PENDING_CAP = 5          # failed sends after which an undelivered explanation no longer blocks open
REQUEST_RING = 16
INPUT_MAX = 65536
LEAD_RERUN = '--lead "<one line about the owner\'s request and this choice>"'
FIT_RERUN = '--fit <pack_id>|none'
OPEN_FIT_RERUN = '--fit <pack_id>[,<pack_id>...]|consult|none'
FIT_NONE = 'none'
FIT_CONSULT = 'consult'     # make-open only: the request does not say which pack (DGN-1702 round 3)
FIT_MAX = 256
CONTEXT_MAX = 3          # owner answers kept per consult (DGN-1702)
ANSWER_MAX = 1000        # characters kept per answer / question
TRANSITIONS_MAX_BYTES = 64 * 1024   # DGN-1787: one flow's transitions log stops growing here
TRANSITIONS_KEEP = 32               # newest per-flow transitions logs kept in .mint-flow/
TOKEN_MSG_EARLIER_MAX = 4           # older undeleted token messages kept for retry (DGN-1844)

# WAIT_CONFIRM remains a legacy on-disk state so an interrupted pre-v4 flow
# can be expired safely. New routes never enter it (I13).
WAIT_STATES = ('WAIT_CONTEXT', 'WAIT_CHOICE', 'WAIT_CONFIRM', 'WAIT_TOKEN')
EXEC_STATES = ('BUILDING', 'VERIFYING', 'RECORDING')
OPEN_ALLOWED = ('EXPIRED', 'CANCELLED', 'FAILED_EMPTY', 'DONE')
END_SCREENS = ('N6', 'N7', 'N12')
AGAIN_SCREENS = ('N8', 'N9')     # expiry screens whose one button starts over (route OPEN)
ROUTES = ('WAIT_CONFIRM', 'WAIT_TOKEN', 'OPEN') + tuple('END_' + s for s in END_SCREENS)

_TOKEN_RE = re.compile(r'\d{3,}:[A-Za-z0-9_-]{20,}')
_NUM_RE = re.compile(r'^\s*(\d{1,3})\s*[.)]\s*(.*?)\s*$', re.S)


# ---------------------------------------------------------------- masking
def mask(text):
    """Replace anything token-shaped. Applied to all input before it is kept."""
    return _TOKEN_RE.sub('<token>', text)


def _log(event, **fields):
    if os.environ.get('DOGANY_MINT_FLOW_DEBUG'):
        line = json.dumps({'event': event, **fields}, sort_keys=True, default=str)
        print(mask(line), file=sys.stderr)


# ------------------------------------------------------------------ table
def load_table(path=TABLE_PATH):
    table = json.loads(Path(path).read_text())
    rows = {}
    for row in table['rows']:
        key = (row['state'], row['waiting_for'], row['note'])
        if key in rows:
            raise ValueError('duplicate table row %r' % (key,))
        if row['state'] not in table['states'] or row['note'] not in table['notes'] \
                or row['waiting_for'] not in table['waiting_for'] \
                or row['agent_utterance'] not in table['utterances']:
            raise ValueError('table row outside its enums: %r' % (row,))
        if (row['note'] == 'SCREEN_SENT_END_TURN') != row['screen_delivered']:
            raise ValueError('screen_delivered must mirror SCREEN_SENT_END_TURN: %r' % (row,))
        rows[key] = row
    table['_index'] = rows
    return table


TABLE = load_table()


def emit(state, waiting_for, note, table=None):
    """The single emission funnel. Unknown triple -> fail closed."""
    table = table or TABLE
    row = table['_index'].get((state, waiting_for, note))
    if row is None:
        _log('emit.refused', state=state, waiting_for=waiting_for, note=note)
        row = table['_index'][('BLOCKED', 'none', 'STATE_UNVERIFIABLE')]
    return {'screen_delivered': row['screen_delivered'], 'waiting_for': row['waiting_for'],
            'state': row['state'], 'note_for_agent': row['note'],
            'agent_utterance': row['agent_utterance']}


# --------------------------------------------------------------- adapters
def not_implemented(wp, seam):
    return {'kind': 'not_implemented', 'wp': wp, 'seam': seam}


def _screen_facts(facts):
    """Owner-safe strings a screen may interpolate (outcome, display_name, username)."""
    if not isinstance(facts, dict):
        return {}
    return {k: mask(str(facts[k]))[:128] for k in ('outcome', 'display_name', 'username')
            if isinstance(facts.get(k), str)}


class Screens:
    """WP3 seam (screen assets + strict sender). Default = stub.

    options(screen_id, flow) -> list of {"label": str, "route": ROUTES member}
        (buttons of that screen; [] for a screen without buttons)
    send(screen_id, flow, ctx) -> {"kind": "ok", "message_id": <id>}
        (flow['screen'].get('facts') carries display_name/username for N4, outcome for N2-N5)
        | {"kind": "failed"} | {"kind": "uncertain"}
        | {"kind": "none"}  (screen without fixed copy: nothing sent, agent speaks)
        | not_implemented(...)
    """
    def options(self, screen_id, flow):
        return not_implemented('WP3', 'screens.options')

    def send(self, screen_id, flow, ctx):
        return not_implemented('WP3', 'screens.send')

    def lead_problem(self, lead, flow=None):
        """None if `lead` (stripped, non-blank) may open a choice screen, else
        'rejected'. MintScreens applies the owner-copy judge here, and (with
        `flow`) refuses pack words the owner never said (DGN-1702)."""
        return None

    def ask_flags(self, asked, flow):
        """Provenance flags for the agent's consult question (DGN-1702):
        [] or ['pack_term_unsaid']. Logged and kept, never blocking."""
        return []

    def occupant(self, flow):
        """The dedicated agent already bound to the chosen pack's domain, or
        None (DGN-1702 early occupancy fact)."""
        return None

    def unsaid_pack_terms(self, text, flow):
        """The pack words in `text` the owner never said (DGN-1702): the
        machine words behind a rejected lead (lead_unsaid)."""
        return []

    def fit_packs(self, flow):
        """The packs the agent may name with --fit (DGN-1702 fix round):
        [{"id", "name_ko", "tagline_ko"}] per eligible pack, or
        not_implemented(...) when the catalog cannot be read."""
        return not_implemented('WP3', 'screens.fit_packs')


class TokenAdapter:
    """WP2 seam. check() sees the raw owner text in memory only (never stored).

    -> {"kind": "accepted", "token_msg": {"message_id": .., "delete_result": ..}}
       | {"kind": "not_accepted", "screen": "N2"|"N3"|"N4"|"N5", "facts": {..}}
       | not_implemented(...)
    """
    def check(self, raw_input, flow, ctx):
        return not_implemented('WP2', 'token.check')


class Worker:
    """WP4 seam (lib/mint_flow_worker.ProcessWorker). Called under the flow lock.

    start(flow, secret=None, persist=None): spawn the detached pipeline worker
        (own process group, READY/GO handshake). `secret` is the accepted token
        from TokenAdapter (key "secret" of its accepted result; memory only, it
        goes to the child over a pipe, never into flow/argv/log). `persist()`
        saves the flow: start() must set flow['worker'] and persist BEFORE it
        sends GO. -> {"kind": "ok", "worker": {...}} | {"kind": "failed"}
    stop(flow): terminate the writer tree, prove it is gone. May update
        flow['artifacts'] in place (created from disk evidence, owned).
        -> {"kind": "ok"} | {"kind": "running"} (still stopping, call again)
        | {"kind": "blocked"} (cannot prove the tree is gone)
    alive(flow): True | False | None (unknown) -- is the registered worker
        still running? Used by the sweep backstop only.
    owned(flow): True | False | None (unknown) -- read-only: does the flow
        receipt prove this flow owns the newborn? Stops nothing (DGN-1698:
        lets `open` on a preserved failure say what `cancel` would do).
    Each may answer not_implemented(...)."""
    def start(self, flow, secret=None, persist=None):
        return not_implemented('WP4', 'worker.start')

    def stop(self, flow):
        return not_implemented('WP4', 'worker.stop')

    def alive(self, flow):
        return None

    def owned(self, flow):
        return None


class Cleanup:
    """WP4 seam (lib/mint_flow_worker.FlowCleanup). run(flow) ->
    {"kind": "complete"} | {"kind": "running"} (still cleaning, call again)
    | {"kind": "blocked"} | not_implemented(...). Owns all cleanup side
    effects; may update flow['cleanup'] in place (persisted by the engine)."""
    def run(self, flow):
        return not_implemented('WP4', 'cleanup.run')


class Readiness:
    """WP5 seam (called by the WP4 worker at VERIFYING, not by WP1).
    check(flow, stage='final') -> {"kind": "ready"} | {"kind": "not_ready"} | not_implemented(...)
    Implementation: lib/mint_ready.py (predicates, target-scoped registration).
    Bound to the ordering instance root; without one it stays the stub, so a
    default Readiness() can never answer "ready"."""
    def __init__(self, instance_root=None, **kw):
        self.instance_root, self.kw = instance_root, kw

    def check(self, flow, stage='final'):
        if not self.instance_root:
            return not_implemented('WP5', 'readiness.check')
        import importlib.util
        path = Path(__file__).resolve().with_name('mint_ready.py')
        spec = importlib.util.spec_from_file_location('mint_ready', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.Readiness(self.instance_root, **self.kw).check(flow, stage)


class LedgerGate:
    """DGN-1700: the mint.ledger-stale gate, flow seat. stale() -> bool.

    The SAME check mint_run.sh runs before the build (minting_state.py
    validate --state <instance-root>/config/minting-state.json; any non-zero
    exit refuses), asked at open and at pack selection -- before the token
    screen sends the owner to BotFather. The pipeline check stays the
    backstop. Unbound (no instance root) answers "not stale", exactly as the
    pipeline skips the gate without --instance-root; a check that cannot run
    counts as stale (fail closed)."""
    SCRIPT = HERE.parents[2] / 'skills/dogany-mint-agent/scripts/minting_state.py'

    def __init__(self, instance_root=None, runner=subprocess.run, timeout=60):
        self.instance_root, self.run, self.timeout = instance_root, runner, timeout

    def stale(self):
        self.stale_binding_path = None
        if not self.instance_root:
            return False
        state = Path(self.instance_root) / 'config' / 'minting-state.json'
        try:
            r = self.run([sys.executable, str(self.SCRIPT), 'validate', '--state', str(state)],
                         capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
            return True
        if r.returncode != 0:
            try:
                bindings = json.loads(state.read_text(encoding='utf-8')).get('domains', {})
                self.stale_binding_path = next(
                    (row.get('agent_path') for row in bindings.values()
                     if isinstance(row, dict) and row.get('dedicated_agent')
                     and (not isinstance(row.get('agent_path'), str)
                          or not os.path.isabs(row['agent_path'])
                          or not os.path.isdir(row['agent_path']))),
                    None)
            except (OSError, ValueError, AttributeError):
                pass
            _log('ledger_stale', detail=(r.stderr or '').strip()[-300:])
        return r.returncode != 0


class Adapters:
    def __init__(self, screens=None, token=None, worker=None, cleanup=None, readiness=None,
                 ledger=None):
        self.screens = screens or Screens()
        self.token = token or TokenAdapter()
        self.worker = worker or Worker()
        self.cleanup = cleanup or Cleanup()
        self.readiness = readiness or Readiness()
        self.ledger = ledger or LedgerGate()


class Ctx:
    def __init__(self, request_id=None, epoch=None, boot_id=None, owner_id=None, chat_id=None,
                 thread_id=None, message_id=None, lead=None, browse=False, asked=None, more=False,
                 fit=None):
        self.request_id, self.epoch, self.boot_id = request_id, epoch, boot_id
        self.owner_id, self.chat_id, self.thread_id = owner_id, chat_id, thread_id
        self.message_id, self.lead = message_id, lead
        self.browse, self.asked, self.more, self.fit = browse, asked, more, fit

    def provable(self):
        return bool(self.epoch) and bool(self.boot_id)


# ------------------------------------------------ pure state logic (WP1)
def epoch_changed(flow, ctx):
    """True only when the runtime is provably a different bridge process.

    DGN-1787: the runtime epoch (bridge pid + start second) alone decides. A
    reboot always starts a new bridge, so it always changes the runtime
    epoch; a boot id that differs while the runtime epoch is the same is a
    boot-id reading artefact, not a restart. (The 2026-09-28 19:22 defect: a
    WAIT_CHOICE flow 15 min old, same bridge pid 31695, expired as a
    'restart'. macOS kern.boottime -- the old boot id -- is wall clock minus
    uptime and moves when the calendar clock is stepped.) The boot id stays
    in the flow record and the transitions log for forensics.
    """
    if not ctx.provable():
        return False
    return flow['epoch']['runtime_epoch'] != ctx.epoch


def decide_expiry(flow, now, ctx):
    """Pure. None, or the reason ('restart' | 'timeout') this flow is dead.

    Terminal and preserved states (DONE, FAILED_*, CLEANUP_BLOCKED, ...) never
    expire: a completed install is not undone by a restart, an observed failure
    is not forgotten by a timeout. ABORT_PENDING/CLEANING are already ending.
    """
    st = flow['state']
    if st in WAIT_STATES:
        if epoch_changed(flow, ctx):
            return 'restart'
        if now > flow['deadline']:
            return 'timeout'
    elif st in EXEC_STATES:
        if epoch_changed(flow, ctx):
            return 'restart'
        if flow.get('exec_deadline') is not None and now > flow['exec_deadline']:
            return 'timeout'
    return None


def abort_target(flow, reason):
    """Pure. Where an unfinished flow goes when it is cancelled/expired.

    WAIT_* holds nothing created: straight to the terminal state. An exec state
    may have a live writer: ABORT_PENDING (mutation blocked) until WP4 stops it.
    """
    if flow['state'] in WAIT_STATES:
        return 'CANCELLED' if reason == 'cancel' else 'EXPIRED'
    return 'ABORT_PENDING'


def after_stop_target(flow):
    """Pure. Once the writer tree is confirmed gone (grill 3): a started
    newborn goes to CLEANING when the flow PROVED it owns the newborn (owner
    ruling dec-174, 2026-09-24: cancel/expiry may stop and archive ONLY the bot
    this flow started; Worker.stop sets artifacts['owned']), and stays
    FAILED_WITH_ARTIFACTS (report only) when ownership cannot be proven.
    Created-but-not-started goes to cleanup; nothing created ends in the plain
    terminal state."""
    art = flow['artifacts']
    if art['started']:
        return 'CLEANING' if art.get('owned') is True else 'FAILED_WITH_ARTIFACTS'
    if art['created']:
        return 'CLEANING'
    return 'CANCELLED' if flow.get('reason') == 'cancel' else 'EXPIRED'


def terminal_screen(state, flow):
    if state == 'EXPIRED':
        if flow.get('reason') == 'worker_probe_failed':
            return None
        if flow.get('reason') == 'worker_failed':
            return 'N11'
        return 'N9' if flow['cleanup'].get('result') == 'complete' else 'N8'
    if state == 'FAILED_EMPTY':
        # N8 says "you didn't reply for a while": only a real expiry/no-resume
        # reason may show it. A worker/pipeline failure (worker_failed or any
        # other reason) gets the existing problem screen (DGN-1700).
        return 'N8' if flow.get('reason') in ('timeout', 'restart') else 'N11'
    return {'CANCELLED': 'N12', 'FAILED_WITH_ARTIFACTS': 'N11',
            'CLEANUP_BLOCKED': 'N10', 'DONE': 'handoff'}.get(state)


def waiting_for(state):
    return {'WAIT_CONTEXT': 'context', 'WAIT_CHOICE': 'choice', 'WAIT_CONFIRM': 'confirm',
            'WAIT_TOKEN': 'token'}.get(state, 'none')


def _token_msg_unsettled(t):
    """A token message that may still be in the chat and has a known id."""
    return isinstance(t, dict) and t.get('delete_result') != 'deleted' \
        and t.get('message_id') is not None and t.get('chat_id') is not None


def _norm(s):
    return ' '.join(s.split()).casefold().strip(' .!?')


def match_option(text, options):
    """Pure. Map owner text to exactly one option or None.

    Accepts the inline button text form "N. label" (label optional, but if
    present it must agree with N), a bare number, or the exact label as free
    text. Anything fuzzier is NOT a match: no guessing a step.
    """
    t = (text or '').strip()
    if not t or not options:
        return None
    by_n = {i + 1: o for i, o in enumerate(options)}
    m = _NUM_RE.match(t)
    if m:
        opt = by_n.get(int(m.group(1)))
        if opt is None or (m.group(2) and _norm(m.group(2)) != _norm(opt['label'])):
            return None
        return opt
    if t.isdigit():
        return by_n.get(int(t))
    hits = [o for o in options if _norm(o['label']) == _norm(t)]
    return hits[0] if len(hits) == 1 else None


def _button_shaped(text):
    """Pure. "N. label" or a bare number: a tap (or its typed form), never
    free words -- one that matches no current option is a stale tap."""
    t = (text or '').strip()
    return bool(_NUM_RE.match(t)) or t.isdigit()


# ------------------------------------------------------- flow validation
def _isint(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_flow(f):
    """Raise ValueError if f is not a well-formed schema-1 flow record."""
    def need(cond, what):
        if not cond:
            raise ValueError('flow schema: ' + what)
    need(isinstance(f, dict), 'not an object')
    need(f.get('schema') == SCHEMA, 'schema')
    need(_isint(f.get('revision')) and f['revision'] >= 1, 'revision')
    need(isinstance(f.get('flow_id'), str) and f['flow_id'], 'flow_id')
    need(f.get('state') in TABLE['flow_states'], 'state')
    for k in ('created_at', 'updated_at', 'deadline'):
        need(isinstance(f.get(k), (int, float)) and not isinstance(f.get(k), bool), k)
    need(f.get('exec_deadline') is None or isinstance(f['exec_deadline'], (int, float)), 'exec_deadline')
    need(f.get('reason') in (None, 'timeout', 'restart', 'cancel', 'unsupported', 'worker_failed', 'worker_probe_failed',
                                'ledger-stale', 'ledger_stale', 'catalog-no-match',
                                'slug_unavailable', 'no_fit', 'occupied', 'build_plan_failed'), 'reason')
    e = f.get('epoch')
    need(isinstance(e, dict) and isinstance(e.get('runtime_epoch'), str) and isinstance(e.get('boot_id'), str), 'epoch')
    o = f.get('owner')
    need(isinstance(o, dict) and set(o) == {'owner_id', 'chat_id', 'thread_id', 'source_message_id'}, 'owner')
    s = f.get('screen')
    need(isinstance(s, dict), 'screen')
    need(_isint(s.get('generation')) and isinstance(s.get('pending'), bool)
         and isinstance(s.get('uncertain'), bool) and _isint(s.get('attempts')), 'screen fields')
    need(s.get('id') is None or isinstance(s['id'], str), 'screen.id')
    need(s.get('lead') is None or isinstance(s['lead'], str), 'screen.lead')
    need(isinstance(s.get('options'), list) and all(
        isinstance(x, dict) and isinstance(x.get('label'), str) and x.get('route') in ROUTES
        for x in s['options']), 'screen.options')
    a = f.get('artifacts')
    need(isinstance(a, dict) and isinstance(a.get('created'), bool) and isinstance(a.get('started'), bool), 'artifacts')
    c = f.get('cleanup')
    need(isinstance(c, dict) and c.get('result') in (None, 'complete', 'blocked')
         and isinstance(c.get('remaining'), list), 'cleanup')
    for k in ('choice', 'token_msg', 'worker', 'selection', 'target', 'accept'):
        need(k in f and (f[k] is None or isinstance(f[k], dict)), k)
    need(isinstance(f.get('requests'), list) and len(f['requests']) <= REQUEST_RING
         and all(isinstance(r, dict) and isinstance(r.get('id'), str) and isinstance(r.get('verb'), str)
                 and isinstance(r.get('emission'), dict) for r in f['requests']), 'requests')
    need(f.get('last_input') is None or isinstance(f['last_input'], str), 'last_input')
    need(f.get('request') is None or isinstance(f['request'], str), 'request')
    # DGN-1702: the owner's consult answers. A flow written before the
    # consult existed has no key at all -- that is an empty consult, not
    # corruption (else every legacy mint-flow.json blocks the verb).
    cx = f.get('context', [])
    need(isinstance(cx, list) and len(cx) <= CONTEXT_MAX and all(
        isinstance(x, dict)
        and (x.get('asked') is None or (isinstance(x['asked'], str) and len(x['asked']) <= ANSWER_MAX))
        and isinstance(x.get('answer'), str) and len(x['answer']) <= ANSWER_MAX
        and isinstance(x.get('at'), (int, float)) and not isinstance(x.get('at'), bool)
        and (x.get('message_id') is None or _isint(x['message_id']))
        and isinstance(x.get('flags'), list) and all(isinstance(g, str) for g in x['flags'])
        for x in cx), 'context')
    # DGN-1702 fix round: the agent's fit judgement and the browse marker
    # (both absent in older files).
    need(f.get('fit') is None or (isinstance(f['fit'], str) and 0 < len(f['fit']) <= FIT_MAX), 'fit')
    need(isinstance(f.get('browse', False), bool), 'browse')
    # DGN-1787: what the last catalog was sent from (absent in older files).
    og = f.get('origin')
    need(og is None or (isinstance(og, dict) and isinstance(og.get('lead'), str)
                        and (og.get('fit') is None or isinstance(og['fit'], str))
                        and isinstance(og.get('browse'), bool)), 'origin')


# ---------------------------------------------------------- file plumbing
class Corrupt(Exception):
    pass


class LeadRequired(Exception):
    def __init__(self, problem, unsaid=None):
        super().__init__(problem)
        self.problem, self.unsaid = problem, unsaid


class FitRequired(Exception):
    """The consult close came without a usable --fit. Nothing is written
    and the request is not recorded (the corrected rerun reuses its id)."""
    def __init__(self, problem, packs, rerun=FIT_RERUN):
        super().__init__(problem)
        self.problem, self.packs, self.rerun = problem, packs, rerun


class LockBusy(Exception):
    pass


class Locked:
    """Short exclusive flock on a stable lock file (never replaced)."""
    def __init__(self, path, timeout):
        self.path, self.timeout, self.fd = path, timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        end = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    os.close(self.fd)
                    raise
                if time.monotonic() >= end:
                    os.close(self.fd)
                    raise LockBusy()
                time.sleep(0.02)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def atomic_write_json(path, obj):
    """0600, fsync file, atomic replace, fsync dir. Token-shaped text is masked
    a last time on the serialized bytes (defence in depth)."""
    data = mask(json.dumps(obj, sort_keys=True, indent=1)) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.mint-flow.', suffix='.tmp', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def transitions_path(root, flow_id):
    """DGN-1787: <instance>/.mint-flow/<flow_id>.transitions.jsonl (the
    worker's per-flow runtime dir, next to its receipt and pipeline log)."""
    return Path(root) / '.mint-flow' / (flow_id + '.transitions.jsonl')


def append_transition(root, flow_id, entry):
    """Append one JSON line to the flow's transitions log. Append-only and
    bounded: a log stops at TRANSITIONS_MAX_BYTES (one 'log_full' line marks
    it), and a new log prunes all but the newest TRANSITIONS_KEEP. Best
    effort: a log write never changes what the verb does or emits."""
    try:
        path = transitions_path(root, flow_id)
        d = path.parent
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(str(d), 0o700)
        fresh = not path.exists()
        size = 0 if fresh else path.stat().st_size
        full = json.dumps({'event': 'log_full', 'flow_id': flow_id}) + '\n'
        if size:
            with open(str(path), 'rb') as fh:
                fh.seek(max(0, size - len(full)))
                if fh.read().decode('utf-8', 'replace').endswith(full):
                    return                                  # already closed at the cap
        line = mask(json.dumps(entry, sort_keys=True, default=str)) + '\n'
        if size + len(line.encode()) + len(full) > TRANSITIONS_MAX_BYTES:
            line = full
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
        if fresh:
            logs = sorted(d.glob('*.transitions.jsonl'), key=lambda q: q.stat().st_mtime)
            for old in logs[:-TRANSITIONS_KEEP]:
                if old != path:
                    old.unlink()
    except OSError:
        pass


class Stub(Exception):
    """A seam answered not_implemented; carries what was asked."""
    def __init__(self, res):
        super().__init__(res.get('seam'))
        self.res = res


# ----------------------------------------------------------------- engine
class Engine:
    def __init__(self, root, adapters=None, clock=time.time, lock_s=LOCK_S,
                 idle_s=IDLE_S, exec_s=EXEC_S):
        self.root = Path(root)
        self.cfg = self.root / 'config'
        self.path = self.cfg / 'mint-flow.json'
        self.lock_path = self.cfg / 'mint-flow.lock'
        self.ad = adapters or Adapters()
        self.clock, self.lock_s, self.idle_s, self.exec_s = clock, lock_s, idle_s, exec_s
        self._persisted = {}      # flow_id -> state last read from / written to disk
        self._ctx = None          # the verb's request context (transitions log)
        self._why = None          # detail for the next logged transition

    # -- storage
    def load(self):
        if not self.path.exists():
            return None
        try:
            flow = json.loads(self.path.read_text())
            validate_flow(flow)
        except (ValueError, OSError) as exc:
            raise Corrupt(type(exc).__name__)
        self._persisted[flow['flow_id']] = flow['state']
        return flow

    def _save(self, flow):
        flow['revision'] += 1
        flow['updated_at'] = self.clock()
        validate_flow(flow)
        atomic_write_json(self.path, flow)
        self._log_transition(flow)

    def _log_transition(self, flow):
        """DGN-1787: one line per persisted state change of this flow (from,
        to, reason, deadlines, the flow's epoch and the caller's), so an
        expiry can be explained after mint-flow.json moved on."""
        fid, st = flow['flow_id'], flow['state']
        prev = self._persisted.get(fid)
        self._persisted[fid] = st
        if prev == st:
            return
        ctx = self._ctx
        entry = {'ts': self.clock(), 'flow_id': fid, 'from': prev, 'to': st,
                 'reason': flow.get('reason'), 'revision': flow['revision'],
                 'deadline': flow.get('deadline'), 'exec_deadline': flow.get('exec_deadline'),
                 'epoch': flow.get('epoch'), 'screen': flow['screen'].get('id'),
                 'ctx_epoch': None if ctx is None else {'runtime_epoch': ctx.epoch,
                                                        'boot_id': ctx.boot_id},
                 'pid': os.getpid()}
        if self._why:
            entry['detail'], self._why = self._why, None
        tm = flow.get('token_msg')
        if isinstance(tm, dict):
            # DGN-1844: where the owner's token message stands, durably (the
            # adapter's own log is debug-only): a delete that did not land is
            # never silent.
            entry['token_msg'] = {k: tm.get(k) for k in ('delete_result', 'attempts', 'error')}
            if tm.get('earlier'):
                entry['token_msg']['earlier_unsettled'] = len(tm['earlier'])
        append_transition(self.root, fid, entry)

    def _new_flow(self, ctx, request_text, browse=False):
        now = self.clock()
        return {
            'schema': SCHEMA, 'revision': 0, 'flow_id': 'f-' + secrets.token_hex(6),
            'state': 'WAIT_CHOICE' if browse else 'WAIT_CONTEXT',
            'owner': {'owner_id': ctx.owner_id, 'chat_id': ctx.chat_id,
                      'thread_id': ctx.thread_id, 'source_message_id': ctx.message_id},
            'epoch': {'runtime_epoch': ctx.epoch, 'boot_id': ctx.boot_id},
            'created_at': now, 'updated_at': now, 'deadline': now + self.idle_s,
            'exec_deadline': None, 'reason': None,
            'request': mask(request_text)[:200] if request_text else None,
            'last_input': None, 'context': [], 'fit': None, 'browse': bool(browse),
            'screen': {'generation': 0, 'id': None, 'pending': False, 'uncertain': False,
                       'attempts': 0, 'receipt': None, 'options': []},
            'choice': None, 'token_msg': None, 'worker': None,
            'selection': None, 'target': None, 'accept': None,
            'artifacts': {'created': False, 'started': False},
            'cleanup': {'result': None, 'remaining': []},
            'requests': [],
        }

    # -- state movement
    def _enter(self, flow, state, screen=None, refresh=False):
        if state not in TABLE['transitions'].get(flow['state'], []):
            raise ValueError('illegal transition %s -> %s' % (flow['state'], state))
        flow['state'] = state
        if refresh and state in WAIT_STATES:
            flow['deadline'] = self.clock() + self.idle_s
        if state == 'BUILDING':
            flow['exec_deadline'] = self.clock() + self.exec_s
        s = flow['screen']
        s.update({'generation': s['generation'] + 1, 'id': screen, 'pending': screen is not None,
                  'uncertain': False, 'attempts': 0, 'receipt': None, 'options': []})
        s.pop('facts', None)
        s.pop('lead', None)

    def _deliver(self, flow, ctx):
        """Try to send the pending screen. Returns 'sent' | 'pending' | 'none'.
        Raises Stub if a WP3 seam is not implemented (state left as persisted)."""
        s = flow['screen']
        if not s['pending'] or not s['id']:
            return 'none'
        if not s['options']:
            opts = self.ad.screens.options(s['id'], flow)
            if isinstance(opts, dict):
                raise Stub(opts)
            s['options'] = [{'label': mask(str(o['label'])), 'route': o['route']} for o in opts]
            self._save(flow)          # options durable before the owner can answer
        for _ in range(SEND_ATTEMPTS):
            res = self.ad.screens.send(s['id'], flow, ctx)
            kind = res.get('kind')
            if kind == 'ok':
                s.update({'pending': False, 'uncertain': False,
                          'receipt': {'message_id': res.get('message_id')}})
                self._save(flow)
                return 'sent'
            if kind == 'not_implemented':
                raise Stub(res)
            if kind == 'none':        # WP3: a screen with no fixed copy (agent speaks); nothing to send
                s.update({'pending': False, 'uncertain': False, 'receipt': None})
                self._save(flow)
                return 'none'
            s['attempts'] += 1
            s['uncertain'] = (kind == 'uncertain')
            if kind == 'uncertain':
                break                 # never blind-resend after an accepted-maybe
        self._save(flow)
        return 'pending'

    def _end(self, flow, state, ctx, reason=None):
        if reason is not None:
            flow['reason'] = reason
        self._enter(flow, state, terminal_screen(state, flow))
        self._save(flow)
        return self._deliver(flow, ctx)

    def _drive(self, flow, ctx):
        """Advance ABORT_PENDING/CLEANING through the WP4 seams. Returns the
        delivery result of the terminal screen ('none' while still in progress)."""
        if flow['state'] == 'ABORT_PENDING':
            res = self.ad.worker.stop(flow)
            if res.get('kind') == 'not_implemented':
                raise Stub(res)
            if res.get('kind') == 'running':
                self._save(flow)                    # the seam's in-place progress (attempt counters) must survive
                return 'none'                       # still stopping: report CLEANUP_IN_PROGRESS
            if res.get('kind') == 'blocked':
                flow['cleanup']['result'] = 'blocked'
                return self._end(flow, 'CLEANUP_BLOCKED', ctx)
            tgt = after_stop_target(flow)
            if tgt == 'CLEANING':
                self._enter(flow, 'CLEANING')
                self._save(flow)
            else:
                return self._end(flow, tgt, ctx)
        if flow['state'] == 'CLEANING':
            res = self.ad.cleanup.run(flow)
            if res.get('kind') == 'not_implemented':
                raise Stub(res)
            if res.get('kind') == 'running':
                self._save(flow)
                return 'none'
            if res.get('kind') == 'complete':
                flow['cleanup']['result'] = 'complete'
                return self._end(flow, 'CANCELLED' if flow['reason'] == 'cancel' else 'EXPIRED', ctx)
            flow['cleanup']['result'] = 'blocked'
            return self._end(flow, 'CLEANUP_BLOCKED', ctx)
        return 'none'

    def _reconcile(self, flow, ctx):
        """Apply expiry/no-resume. Returns (transitioned, delivery)."""
        reason = decide_expiry(flow, self.clock(), ctx)
        if reason is None:
            return False, 'none'
        _log('expiry', flow_id=flow['flow_id'], state=flow['state'], reason=reason)
        self._why = {'expiry': reason, 'now': self.clock(), 'deadline': flow['deadline'],
                     'exec_deadline': flow.get('exec_deadline'), 'flow_epoch': flow['epoch'],
                     'ctx_epoch': {'runtime_epoch': ctx.epoch, 'boot_id': ctx.boot_id}}
        flow['reason'] = reason
        tgt = abort_target(flow, reason)
        if tgt == 'ABORT_PENDING':
            self._enter(flow, 'ABORT_PENDING')
            self._save(flow)          # mutation blocked before any hook runs
            return True, self._drive(flow, ctx)
        return True, self._end(flow, tgt, ctx)

    # -- emission
    def _view(self, flow, delivery='none', rejected=False):
        if flow is None:
            return emit('IDLE', 'none', 'NO_FLOW')
        st, s = flow['state'], flow['screen']
        if st in ('ABORT_PENDING', 'CLEANING'):
            return emit(st, 'none', 'CLEANUP_IN_PROGRESS')
        if s['pending'] and st not in EXEC_STATES:
            return emit('DELIVERY_PENDING', 'none',
                        'DELIVERY_UNCERTAIN' if s['uncertain'] else 'DELIVERY_FAILED')
        if st in EXEC_STATES:
            note = 'SCREEN_SENT_END_TURN' if delivery == 'sent' and st == 'BUILDING' else 'BUILD_RUNNING'
            return emit(st, 'none', note)
        w = waiting_for(st)
        if delivery == 'sent':
            return emit(st, w, 'SCREEN_SENT_END_TURN')
        if st == 'WAIT_CONTEXT':
            return dict(emit(st, w, 'INPUT_NOT_ACCEPTED' if rejected else 'WAITING_ON_OWNER'),
                        answers_left=self._answers_left(flow))
        if st in WAIT_STATES:
            return emit(st, w, 'INPUT_NOT_ACCEPTED' if rejected else 'WAITING_ON_OWNER')
        steady = {'FAILED_WITH_ARTIFACTS': 'FAILED_ARTIFACTS_PRESERVED',
                  'CLEANUP_BLOCKED': 'CLEANUP_BLOCKED'}.get(st, 'FLOW_ENDED')
        return emit(st, 'none', steady)

    @staticmethod
    def _answers_left(flow):
        return max(0, CONTEXT_MAX - len(flow.get('context') or []))

    def _ask(self, flow):
        """DGN-1702: the one emission that grants "consult" -- entering or
        extending WAIT_CONTEXT. A later status there is WAITING_ON_OWNER/none:
        the question is already out."""
        return dict(emit('WAIT_CONTEXT', 'context', 'ASK_OWNER'),
                    answers_left=self._answers_left(flow))

    @staticmethod
    def _stub(res):
        return emit('NOT_IMPLEMENTED', 'none', 'NOT_IMPLEMENTED')

    @staticmethod
    def _blocked(note):
        return emit('BLOCKED', 'none', note)

    def _lead(self, ctx, flow=None):
        """The agent's lead for the choice screen about to be sent, or LeadRequired.
        The judge sees the flow (the owner's recorded words) when it takes it."""
        lead = (getattr(ctx, 'lead', None) or '').strip()
        if not lead:
            raise LeadRequired('missing')
        judge = getattr(self.ad.screens, 'lead_problem', None)
        problem = None
        if callable(judge):
            takes_flow = len(inspect.signature(judge).parameters) >= 2
            problem = judge(lead, flow) if takes_flow else judge(lead)
        if problem:
            unsaid = None
            if problem == 'rejected':
                probe = getattr(self.ad.screens, 'unsaid_pack_terms', None)
                unsaid = [mask(str(w))[:64] for w in probe(lead, flow)] \
                    if (callable(probe) and flow is not None) else []
            raise LeadRequired(problem, unsaid)
        return mask(lead)

    def _fit(self, ctx, flow, at_open=False):
        """DGN-1702 fix round: the agent's --fit, or FitRequired (nothing
        written). Returns FIT_NONE, FIT_CONSULT (make-open only, round 3) or
        the fit pack ids joined by commas (one pack, or the candidates when
        several match; order kept, duplicates dropped)."""
        fit = (getattr(ctx, 'fit', None) or '').strip()
        if fit.casefold() == FIT_NONE:
            return FIT_NONE
        if at_open and fit.casefold() == FIT_CONSULT:
            return FIT_CONSULT
        packs = self.ad.screens.fit_packs(flow)
        if isinstance(packs, dict):
            raise Stub(packs)
        packs = [{k: mask(str(p.get(k) or ''))[:200] for k in ('id', 'name_ko', 'tagline_ko')}
                 for p in packs]
        rerun = OPEN_FIT_RERUN if at_open else FIT_RERUN
        if not fit:
            raise FitRequired('missing', packs, rerun)
        ids = []
        for x in (i.strip() for i in fit.split(',')):
            if x and x not in ids:
                ids.append(x)
        known = [p['id'] for p in packs]
        if not ids or any(x not in known for x in ids) or len(','.join(ids)) > FIT_MAX:
            raise FitRequired('unknown', packs, rerun)
        return ','.join(ids)

    def _record(self, flow, ctx, verb, emission):
        if flow is not None and ctx.request_id and verb in ('open', 'next', 'cancel') \
                and not any(r['id'] == ctx.request_id for r in flow['requests']):
            flow['requests'] = (flow['requests'] + [{'id': ctx.request_id, 'verb': verb,
                                                     'emission': emission}])[-REQUEST_RING:]
            self._save(flow)
        return emission

    # -- verbs
    def call(self, verb, ctx, text=None):
        self._ctx = ctx
        try:
            with Locked(self.lock_path, self.lock_s):
                return self._call(verb, ctx, text)
        except LockBusy:
            return self._blocked('LOCK_BUSY')
        except Corrupt:
            return self._blocked('STATE_UNVERIFIABLE')

    def _call(self, verb, ctx, text):
        flow = self.load()
        if verb in ('open', 'next') and not ctx.provable():
            return self._blocked('STATE_UNVERIFIABLE')     # no epoch/boot: no flow may run
        if flow is not None and ctx.request_id and verb in ('open', 'next', 'cancel'):
            for r in flow['requests']:
                if r['id'] == ctx.request_id:
                    return r['emission'] if r['verb'] == verb else self._blocked('BAD_REQUEST')
        try:
            if flow is None:
                if verb == 'open':
                    return self._open_new(ctx, text, None)
                return self._view(None)
            changed, delivery = self._reconcile(flow, ctx)
            if changed:                                  # input consumed, nothing installed
                return self._record(flow, ctx, verb, self._view(flow, delivery))
            if not self._bound(flow, ctx):
                return self._view(flow, rejected=True)
            # Deletion is intentionally best-effort and never changes the
            # owner-facing route. Retry it on later verbs while the adapter
            # still has the ordering-bot credential.
            retry = getattr(self.ad.token, 'retry_delete', None)
            if callable(retry) and isinstance(flow.get('token_msg'), dict):
                flow['token_msg'] = retry(flow['token_msg'], ctx)
                self._save(flow)
            handler = getattr(self, '_v_' + verb)
            em = handler(flow, ctx, text)
            return self._record(self.load(), ctx, verb, em)
        except Stub as st:
            return self._stub(st.res)
        except LeadRequired as lr:                   # nothing written, input not consumed
            em = dict(self._blocked('LEAD_REQUIRED'), lead_problem=lr.problem, rerun_with=LEAD_RERUN)
            if lr.problem == 'rejected':
                em['lead_unsaid'] = lr.unsaid or []  # machine words the owner never said
            return em
        except FitRequired as fr:                    # nothing written, request not recorded
            if fr.problem == 'missing':
                em = self._blocked('FIT_REQUIRED')
            else:                                    # an id that is not an eligible pack
                em = self._view(self.load(), rejected=True)   # the on-disk flow, answer not kept
            em = dict(em, fit_problem=fr.problem, packs=fr.packs, rerun_with=fr.rerun)
            if getattr(fr, 'request', None):
                em['request'] = fr.request              # DGN-1787: the restart's original request
            return em

    def _bound(self, flow, ctx):
        o = flow['owner']
        for mine, theirs in ((o['owner_id'], ctx.owner_id), (o['chat_id'], ctx.chat_id),
                             (o['thread_id'], ctx.thread_id)):
            if mine is not None and theirs is not None and str(mine) != str(theirs):
                return False
        return True

    def _ledger_refusal(self, flow, ctx):
        """DGN-1700: mint.ledger-stale before any owner effort. A stale
        ledger ends at the no-copy N6 terminal; its machine-readable reason
        and binding path let the agent explain the actual moment. The next
        open checks again. None when the ledger is clean."""
        ledger = getattr(self.ad, 'ledger', None)
        if ledger is None or not ledger.stale():
            return None
        flow['reason'] = 'ledger-stale'
        self._enter(flow, 'CANCELLED', 'N6')
        flow['screen']['facts'] = {'stale_binding_path': getattr(ledger, 'stale_binding_path', None)}
        self._save(flow)
        return self._view(flow, self._deliver(flow, ctx))

    def _open_new(self, ctx, text, old, context=None):
        browse = bool(getattr(ctx, 'browse', False))
        flow = self._new_flow(ctx, text, browse)
        if context:
            flow['context'] = copy.deepcopy(context)[:CONTEXT_MAX]   # DGN-1787 restart
        opts = self.ad.screens.options('catalog', flow)
        if isinstance(opts, dict):
            raise Stub(opts)                             # dark ship: write nothing
        flow['revision'] = old['revision'] if old else 0
        self._carry_token_msg(flow, old)
        refused = self._ledger_refusal(flow, ctx)        # DGN-1700: before the consult/catalog
        if refused is not None:
            return self._record(flow, ctx, 'open', refused)
        if not browse:
            if not (text or '').strip() and not flow['context']:
                # A make-open without request words (a restart tap): there
                # is nothing to judge, the agent asks the context.
                self._save(flow)
                return self._record(flow, ctx, 'open', self._ask(flow))
            # DGN-1702 round 3: the agent judges the request itself. One pack
            # named -> catalog + confirm lead at once; several -> the
            # candidates; unclear -> consult; nothing fits -> NO_FIT.
            fit = self._fit(ctx, flow, at_open=True)        # raises before anything is saved
            if fit == FIT_CONSULT:
                self._save(flow)
                return self._record(flow, ctx, 'open', self._ask(flow))
            if fit == FIT_NONE:
                return self._record(flow, ctx, 'open', self._end_no_fit(flow))
            flow['fit'] = fit
            return self._record(flow, ctx, 'open', self._send_catalog(flow, ctx))
        if not opts:
            return self._record(flow, ctx, 'open', self._end_no_fit(flow))
        lead = self._lead(ctx, flow)                     # DGN-1674: no lead, no choice screen
        flow['screen'].update({'generation': 1, 'id': 'catalog', 'pending': True, 'lead': lead,
                               'options': [{'label': mask(str(o['label'])), 'route': o['route']}
                                           for o in opts]})
        flow['origin'] = {'lead': lead, 'fit': None, 'browse': True}
        self._save(flow)
        return self._record(flow, ctx, 'open', self._view(flow, self._deliver(flow, ctx)))

    def _restart(self, ctx, old):
        """DGN-1787: the owner tapped "처음부터 만들기" on an expiry screen.
        Start over from what the owner already said -- the expired flow's
        request, consult answers and the agent's fit -- so the same pack card
        goes out again with its recorded lead (judged again). The agent's own
        --fit/--lead on this call win. Nothing known (no request, no answers,
        not a browse) -> the plain restart: a new flow at the consult."""
        request = (old.get('request') or '').strip() or None
        context = old.get('context') or []
        origin = old.get('origin') or {}
        browse = bool(old.get('browse'))
        if not (request or context or browse):
            return self._open_new(ctx, None, old)
        rctx = copy.copy(ctx)
        rctx.browse = browse
        fit = (getattr(ctx, 'fit', None) or '').strip() or old.get('fit')
        if browse:
            rctx.fit = None
        elif fit:
            rctx.fit = fit
        elif context and not request:
            # consult answers but no fit judged yet: the consult close judges
            rctx.fit = FIT_CONSULT
        if not (getattr(ctx, 'lead', None) or '').strip() and origin.get('lead') \
                and origin.get('browse') == browse and (browse or origin.get('fit') == rctx.fit):
            rctx.lead = origin['lead']
        self._why = {'restarted_from': old['flow_id'], 'old_state': old['state'],
                     'old_reason': old.get('reason')}
        if context and not request and rctx.fit == FIT_CONSULT:
            # A restart-tap flow that got its words only from the consult:
            # reopen at the consult with the answers kept.
            flow = self._new_flow(ctx, None, False)
            flow['context'] = copy.deepcopy(context)[:CONTEXT_MAX]
            flow['revision'] = old['revision']
            self._carry_token_msg(flow, old)
            self._save(flow)
            return self._record(flow, ctx, 'open', self._ask(flow))
        try:
            return self._open_new(rctx, request, old, context=context)
        except FitRequired as fr:
            fr.request = mask(request)[:200] if request else None   # judge it from these words
            raise

    def _carry_token_msg(self, flow, old):
        """DGN-1844: a new flow that replaces an ended one inherits the ended
        flow's unsettled token-message deletes, so the retry on later verbs
        keeps going instead of being dropped with the old flow."""
        t = (old or {}).get('token_msg')
        if not isinstance(t, dict):
            return
        pending = [e for e in [{k: v for k, v in t.items() if k != 'earlier'}] + list(t.get('earlier') or [])
                   if _token_msg_unsettled(e)]
        if pending:
            head = dict(pending[-1])
            if pending[:-1]:
                head['earlier'] = pending[:-1][-TOKEN_MSG_EARLIER_MAX:]
            flow['token_msg'] = head

    def _end_no_fit(self, flow):
        """DGN-1702 (dec-183): nothing in the catalog fits the owner's words.
        No screen, no custom offer: the agent says so in its own words
        (NO_FIT/"no_fit"). Emitted directly, not via _view, so a later status
        reads the plain FLOW_ENDED and the agent does not speak twice."""
        _log('no_fit', flow_id=flow['flow_id'], words=self.ad.screens.query_of(flow)
             if hasattr(self.ad.screens, 'query_of') else None)
        flow['reason'] = 'no_fit'
        self._enter(flow, 'CANCELLED', None)
        self._save(flow)
        return emit('CANCELLED', 'none', 'NO_FIT')

    def _end_occupied(self, flow):
        """DGN-1702: the chosen pack's domain already has a dedicated agent.
        Ends before any token screen; the agent speaks (DOMAIN_OCCUPIED /
        "occupied") with the occupant as a fact."""
        probe = getattr(self.ad.screens, 'occupant', None)
        agent = probe(flow) if callable(probe) else None
        flow['reason'] = 'occupied'
        self._enter(flow, 'CANCELLED', None)
        self._save(flow)
        return dict(emit('CANCELLED', 'none', 'DOMAIN_OCCUPIED'),
                    occupied={'agent': mask(str(agent))[:64] if agent else None})

    def _consult(self, flow, ctx, text):
        """DGN-1702: one owner answer to the agent's consult question. The
        answer is kept verbatim (masked, capped); nothing is written when the
        closing step refuses the lead (LEAD_REQUIRED)."""
        cx = list(flow.get('context') or [])
        asked = (getattr(ctx, 'asked', None) or '').strip()
        asked = mask(asked)[:ANSWER_MAX] if asked else None
        flagger = getattr(self.ad.screens, 'ask_flags', None)
        flags = list(flagger(asked, flow)) if (asked and callable(flagger)) else []
        mid = str(ctx.message_id or '')
        cx.append({'asked': asked, 'answer': mask(text.strip())[:ANSWER_MAX], 'at': self.clock(),
                   'message_id': int(mid) if mid.isdigit() else None, 'flags': flags})
        if flags:
            _log('consult_flagged', flow_id=flow['flow_id'], flags=flags)
        flow['context'] = cx
        flow['last_input'] = mask(text.strip())[:120]
        if getattr(ctx, 'more', False) and len(cx) < CONTEXT_MAX:
            flow['deadline'] = self.clock() + self.idle_s    # accepted input refreshes
            self._save(flow)
            return self._ask(flow)
        choice = flow.get('choice') or {}
        if choice.get('route'):
            # Browse order: the owner's tap already chose the pack, so --fit
            # is not consulted; an answer that contradicts the pick is the
            # agent's `cancel` (SKILL rule).
            return self._route(flow, ctx, choice['route'], text)
        # The close never decides fit by substring (grill BLOCK #1): the
        # agent's --fit does, and the owner's tap confirms it.
        fit = self._fit(ctx, flow)                       # raises before anything is saved
        if fit == FIT_NONE:
            return self._end_no_fit(flow)
        flow['fit'] = fit
        return self._send_catalog(flow, ctx)

    def _send_catalog(self, flow, ctx):
        """WAIT_CONTEXT -> WAIT_CHOICE with the catalog (the fit pack, or
        every eligible pack for a browse). The lead is judged before anything
        is saved (LEAD_REQUIRED writes nothing)."""
        opts = self.ad.screens.options('catalog', flow)
        if isinstance(opts, dict):
            raise Stub(opts)
        if not opts:
            return self._end_no_fit(flow)
        lead = self._lead(ctx, flow)
        self._enter(flow, 'WAIT_CHOICE', 'catalog', refresh=True)
        flow['screen'].update({'lead': lead,
                               'options': [{'label': mask(str(o['label'])), 'route': o['route']}
                                           for o in opts]})
        flow['origin'] = {'lead': lead, 'fit': flow.get('fit'), 'browse': bool(flow.get('browse'))}
        self._save(flow)
        return self._view(flow, self._deliver(flow, ctx))

    def _v_open(self, flow, ctx, text):
        st = flow['state']
        if st == 'WAIT_CONTEXT':
            # Grill #2: mid-consult, "what is there?" reaches the list (context
            # kept), and new owner words are a consult answer, not a re-ask.
            if getattr(ctx, 'browse', False):
                flow.update({'browse': True, 'fit': None, 'choice': None})
                return self._send_catalog(flow, ctx)
            if (text or '').strip():
                return self._v_next(flow, ctx, text)       # same restart-tap guard as next
            return self._v_status(flow, ctx, text)
        if st == 'WAIT_CHOICE' and (text or '').strip() and not getattr(ctx, 'browse', False):
            return self._v_next(flow, ctx, text)           # owner words at the catalog (round 3)
        if st in ('CLEANING', 'FAILED_WITH_ARTIFACTS'):
            # DGN-1698: a failed attempt is not a dead end. Its leftovers are
            # cleaned first (same routing as an abort), then the new request
            # opens. The lead is checked before anything moves (LEAD_REQUIRED
            # writes nothing).
            if st == 'FAILED_WITH_ARTIFACTS' and flow['artifacts']['started']:
                return self._explain(flow, 'open')          # dec-174: open never stops a started bot
            if getattr(ctx, 'browse', False):
                self._lead(ctx)
            elif (text or '').strip() and self._fit(ctx, None, at_open=True) not in (FIT_NONE, FIT_CONSULT):
                self._lead(ctx)                             # a make-open to a catalog needs its lead
            if st == 'FAILED_WITH_ARTIFACTS':
                kept, delivery = self._settle_failed(flow, ctx)
                if kept:
                    return self._explain(flow, 'open', delivery)
            else:
                delivery = self._drive(flow, ctx)
            if flow['state'] not in OPEN_ALLOWED:
                return self._view(flow, delivery)
            st = flow['state']
        if st in OPEN_ALLOWED and not (flow['screen']['pending']
                                       and flow['screen']['attempts'] < PENDING_CAP):
            if not getattr(ctx, 'browse', False) and match_option(text or '', flow['screen']['options']) \
                    and self._restart_tap(text, flow):
                return self._restart(ctx, flow)          # DGN-1787: the tap passed with open
            return self._open_new(ctx, text, flow)
        return self._v_status(flow, ctx, text)

    def _settle_failed(self, flow, ctx, cancel=False):
        """DGN-1698: route a FAILED_WITH_ARTIFACTS flow the way an abort is
        routed. worker.stop proves the writer gone and, for a started newborn,
        proves ownership from the flow receipt; after_stop_target then picks
        CLEANING (FlowCleanup -> an open-allowed terminal) or keeps the flow
        preserved. Returns (kept, x): kept False -> x is the terminal
        delivery; kept True -> the flow stays FAILED_WITH_ARTIFACTS and x is
        the stop kind when the writer was not proven gone (caller explains)."""
        res = self.ad.worker.stop(flow)
        if res.get('kind') == 'not_implemented':
            raise Stub(res)
        if res.get('kind') != 'ok':
            self._save(flow)                            # the seam's in-place progress survives
            return True, res.get('kind')
        tgt = after_stop_target(flow)
        if tgt == 'FAILED_WITH_ARTIFACTS':
            self._save(flow)
            return True, None
        if cancel:
            flow['reason'] = 'cancel'
        if tgt == 'CLEANING':
            self._enter(flow, 'CLEANING')
            self._save(flow)
            return False, self._drive(flow, ctx)
        return False, self._end(flow, tgt, ctx)

    def _explain(self, flow, verb, stop=None):
        """DGN-1698: a preserved failure never answers an owner verb with
        silence. No fixed owner copy: the agent says `why` and `next` in its
        own words (agent_utterance EXPLAIN)."""
        owned = flow['artifacts'].get('owned')
        if owned is None:
            probe = getattr(self.ad.worker, 'owned', None)
            owned = probe(flow) if callable(probe) else None
        if stop == 'running':
            why, nxt = 'previous_attempt_still_stopping', 'ask_again_shortly'
        elif stop is not None:
            why, nxt = 'previous_attempt_not_provably_stopped', 'kept_and_reported_to_framework'
        elif owned is True:
            why, nxt = 'previous_attempt_bot_still_running', 'cancel_removes_it_then_open_again'
        else:
            why, nxt = 'previous_attempt_bot_not_provably_ours', 'kept_and_reported_to_framework'
        target = flow.get('target') or {}
        em = emit('FAILED_WITH_ARTIFACTS', 'none', 'FAILED_ARTIFACTS_EXPLAIN')
        em['explain'] = {'say': 'tell the owner why and what happens next, in your own words',
                         'verb': verb, 'why': why, 'next': nxt,
                         'failure': flow.get('reason'),
                         'slug': target.get('slug') if isinstance(target.get('slug'), str) else None}
        return em

    def _v_status(self, flow, ctx, text):
        if flow['state'] in EXEC_STATES:
            health = self._worker_health(flow)
            if health != 'live':
                return self._worker_unavailable(flow, ctx, health)
        delivery = self._drive(flow, ctx) if flow['state'] in ('ABORT_PENDING', 'CLEANING') else 'none'
        if delivery == 'none':
            delivery = self._deliver(flow, ctx)
        return self._view(flow, delivery)

    def _worker_health(self, flow):
        """'live', 'missing', or 'failed'. A probe is a safety boundary: an
        exception is not permission to keep claiming a build is running."""
        rec = flow.get('worker')
        if not isinstance(rec, dict) or not rec.get('pid'):
            return 'missing'
        try:
            return 'live' if self.ad.worker.alive(flow) is True else 'failed'
        except Exception:
            return 'probe_failed'

    def _fail_no_worker(self, flow):
        """End a BUILDING flow that never acquired a runnable worker.

        This deliberately has no fixed screen: the agent receives the same
        EXPLAIN permission used for preserved failures and says what happened
        in its own words.
        """
        flow['reason'] = 'worker_failed'
        flow['worker'] = None
        self._enter(flow, 'FAILED_EMPTY', None)
        self._save(flow)
        em = emit('FAILED_EMPTY', 'none', 'FAILED_EMPTY_EXPLAIN')
        em['explain'] = {'say': 'tell the owner why and what happens next, in your own words',
                         'why': 'mint_worker_unavailable', 'next': 'open_starts_new_attempt'}
        return em

    def _worker_unavailable(self, flow, ctx, health):
        """Use the normal worker-failure route for a dead/unprobeable runner.
        A flow which has no registered worker is safer and clearer as the
        immediate no-worker failure above; there is no process to clean up.
        """
        if health == 'missing':
            return self._fail_no_worker(flow)
        flow['reason'] = 'worker_probe_failed' if health == 'probe_failed' else 'worker_failed'
        report_dead = getattr(self.ad.worker, 'report_dead', None)
        if callable(report_dead):
            try:
                report_dead(flow)
            except Exception:
                pass
        self._enter(flow, 'ABORT_PENDING')
        self._save(flow)
        delivery = self._drive(flow, ctx)
        if health == 'probe_failed' and flow['state'] == 'EXPIRED':
            em = emit('EXPIRED', 'none', 'WORKER_PROBE_EXPLAIN')
            em['explain'] = {'say': 'tell the owner why and what happens next, in your own words',
                             'why': 'mint_worker_liveness_unavailable', 'next': 'open_starts_new_attempt'}
            return em
        return self._view(flow, delivery)

    def _v_sweep(self, flow, ctx, text):
        """Backstop (grill v4 fix 1): the worker sends the terminal screen
        itself; if it dies or overruns, the next sweep is what tells the owner.
        _reconcile already handled the exec deadline; here a dead worker turns
        a running build into an abort, then the same drive/re-deliver as status."""
        if flow['state'] in EXEC_STATES:
            health = self._worker_health(flow)
            if health != 'live':
                return self._worker_unavailable(flow, ctx, health)
        return self._v_status(flow, ctx, text)

    def _v_cancel(self, flow, ctx, text):
        st = flow['state']
        if st == 'FAILED_WITH_ARTIFACTS':                   # DGN-1698: a verb path to cleanup
            kept, delivery = self._settle_failed(flow, ctx, cancel=True)
            if kept:
                return self._explain(flow, 'cancel', delivery)
            return self._view(flow, delivery)
        if st in WAIT_STATES or st in EXEC_STATES:
            flow['reason'] = 'cancel'
            tgt = abort_target(flow, 'cancel')
            if tgt == 'ABORT_PENDING':
                self._enter(flow, 'ABORT_PENDING')
                self._save(flow)
                return self._view(flow, self._drive(flow, ctx))
            return self._view(flow, self._end(flow, tgt, ctx))
        return self._v_status(flow, ctx, text)

    def _v_next(self, flow, ctx, text):
        st = flow['state']
        if not (text or '').strip():
            return self._view(flow, rejected=True)
        if st in EXEC_STATES or st in ('ABORT_PENDING', 'CLEANING'):
            return self._v_status(flow, ctx, text)
        if flow['screen']['pending'] and st not in ('EXPIRED', 'CANCELLED', 'FAILED_EMPTY'):
            return self._view(flow, self._deliver(flow, ctx))   # input predates the screen: discard
        if st == 'WAIT_CONTEXT':
            if self._restart_tap(text, flow):
                # The 2026-09-26 09:37 defect: the owner tapped the expiry
                # screen's "1. 처음부터 만들기" and it became the first consult
                # answer. The flow already starts over: nothing is kept, the
                # agent asks its question (again -- it may never have shown).
                return self._ask(flow)
            return self._consult(flow, ctx, text)
        if st in ('WAIT_CHOICE', 'WAIT_CONFIRM'):
            opt = match_option(text, flow['screen']['options'])
            if opt is None:
                if st == 'WAIT_CHOICE' and flow['screen']['id'] == 'catalog' \
                        and not _button_shaped(text) and not self._restart_tap(text, flow) \
                        and len(flow.get('context') or []) < CONTEXT_MAX:
                    # Round 3: the card invites free words ("생각하신 게 이게
                    # 아니면..."). They are the owner telling what they meant:
                    # a consult answer, and the agent judges --fit again.
                    self._enter(flow, 'WAIT_CONTEXT', None, refresh=True)
                    flow['fit'] = None
                    return self._consult(flow, ctx, text)
                return self._view(flow, rejected=True)
            flow['last_input'] = mask(text.strip())[:120]
            flow['choice'] = {'n': flow['screen']['options'].index(opt) + 1, 'label': opt['label']}
            if opt['route'] == 'WAIT_TOKEN':
                flow['choice']['route'] = opt['route']   # the context gate resumes it (DGN-1702)
            return self._route(flow, ctx, opt['route'], text)
        if st == 'WAIT_TOKEN':
            return self._token(flow, ctx, text)
        if st in ('EXPIRED', 'CANCELLED', 'FAILED_EMPTY') and not flow['screen']['pending']:
            opt = match_option(text, flow['screen']['options'])
            if opt is not None and opt['route'] == 'OPEN':
                return self._restart(ctx, flow)
        return self._v_status(flow, ctx, text)

    def _restart_tap(self, text, flow):
        """True when `text` is a tap on an expiry/restart screen button (the
        label of an OPEN-route option of N8/N9, as "N. label" or the bare
        label). A bare number is not a tap on a screen that is not current."""
        t = (text or '').strip()
        m = _NUM_RE.match(t)
        label = m.group(2) if m else t
        if not label:
            return False
        for sid in AGAIN_SCREENS:
            opts = self.ad.screens.options(sid, flow)
            if isinstance(opts, dict):
                continue
            if any(o.get('route') == 'OPEN' and _norm(str(o.get('label', ''))) == _norm(label)
                   for o in opts):
                return True
        return False

    @staticmethod
    def _owner_words_kept(flow):
        """The token prompt needs the owner's own words (DGN-1702): consult
        answers, or -- round 3 -- the make request the agent judged a single
        pack from (a browse is not a request)."""
        if flow.get('context'):
            return True
        return bool((flow.get('request') or '').strip()) and bool(flow.get('fit')) \
            and not flow.get('browse')

    def _route(self, flow, ctx, route, text):
        if route == 'OPEN':
            return self._restart(ctx, flow)
        if route == 'END_N7':                            # DGN-1702: domain already bound
            return self._end_occupied(flow)
        if route == 'END_N6':                            # legacy on-disk "custom" button (dec-183)
            return self._end_no_fit(flow)
        if route.startswith('END_'):
            self._enter(flow, 'CANCELLED', route[4:])
            flow['reason'] = 'cancel'
            self._save(flow)
            return self._view(flow, self._deliver(flow, ctx))
        if route == 'WAIT_TOKEN':                        # DGN-1700: before the BotFather screen
            refused = self._ledger_refusal(flow, ctx)
            if refused is not None:
                return refused
            if not self._owner_words_kept(flow):
                # DGN-1702: no token prompt without the owner's own words.
                # A browse pick asks the context first, then resumes here.
                self._enter(flow, 'WAIT_CONTEXT', None, refresh=True)
                self._save(flow)
                return self._ask(flow)
            # DGN-1705: the slug is fixed at pack selection, before any bot
            # exists, so the bot the owner brings can never choose or move it.
            lock = getattr(self.ad.screens, 'lock_selection', None)
            if callable(lock):
                try:
                    lock(flow)
                except Exception:
                    flow['reason'] = 'slug_unavailable'
                    self._enter(flow, 'CANCELLED', 'N5')
                    self._save(flow)
                    return self._view(flow, self._deliver(flow, ctx))
        # Compatibility-only WAIT_CONFIRM may still exist in an old flow;
        # MintScreens no longer offers a route to it for new conversations.
        self._enter(flow, route, 'handover' if route == 'WAIT_CONFIRM' else 'token_prompt', refresh=True)
        self._save(flow)
        return self._view(flow, self._deliver(flow, ctx))

    @staticmethod
    def _keep_token_msg(flow, new):
        """DGN-1844: record a token message's delete state on the flow. A
        record that is still unsettled (the message may sit in the chat) is
        never dropped by a newer paste: it moves under `earlier`, which the
        token adapter's retry_delete works through as well."""
        if not isinstance(new, dict):
            return
        new = dict(new)
        old = flow.get('token_msg')
        if isinstance(old, dict):
            earlier = [e for e in (old.get('earlier') or []) if isinstance(e, dict)]
            head = {k: v for k, v in old.items() if k != 'earlier'}
            if _token_msg_unsettled(head):
                earlier.append(head)
            earlier += [e for e in (new.pop('earlier', None) or []) if isinstance(e, dict)]
            if earlier:
                new['earlier'] = earlier[-TOKEN_MSG_EARLIER_MAX:]
        flow['token_msg'] = new

    def _start_worker(self, flow, secret):
        """worker.start(flow[, secret=][, persist=]): the extra keywords are
        passed only when the adapter accepts them (WP1-era fakes take flow)."""
        fn = self.ad.worker.start
        params = inspect.signature(fn).parameters
        wide = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        kw = {}
        if secret is not None and (wide or 'secret' in params):
            kw['secret'] = secret
        if wide or 'persist' in params:
            kw['persist'] = lambda: self._save(flow)
        return fn(flow, **kw)

    def _token(self, flow, ctx, text):
        res = self.ad.token.check(text, flow, ctx)      # raw text: memory only
        flow['last_input'] = '<%d chars>' % len(text)
        kind = res.get('kind')
        if kind == 'not_implemented':
            raise Stub(res)
        if kind == 'not_accepted':
            screen = res.get('screen')
            if screen not in ('N2', 'N3', 'N4', 'N5'):
                raise ValueError('token adapter named an unknown screen')
            s = flow['screen']
            s.update({'generation': s['generation'] + 1, 'id': screen, 'pending': True,
                      'uncertain': False, 'attempts': 0, 'receipt': None, 'options': []})
            s['facts'] = _screen_facts(res.get('facts'))
            self._keep_token_msg(flow, res.get('token_msg'))
            if screen == 'N4':
                # One facts location for the reused-bot identity (I11).
                flow['target'] = {'existing_username': s['facts'].get('username'),
                                  'existing_display_name': s['facts'].get('display_name')}
            self._save(flow)
            return self._view(flow, self._deliver(flow, ctx))
        if kind != 'accepted':
            raise ValueError('token adapter result')
        # DGN-1844: the pasted token is in the chat whatever happens next --
        # its delete record is kept before anything can fail, so a later verb
        # retries a delete that did not land (snap too: a Stub restores it).
        self._keep_token_msg(flow, res.get('token_msg'))
        snap = copy.deepcopy(flow)
        secret = res.get('secret') or res.get('credential')
        configure = getattr(self.ad.screens, 'configure_accepted_flow', None)
        if callable(configure):
            try:
                configure(flow, res.get('bot') or {})
            except Exception as exc:
                # DGN-1844: nothing was created and no worker exists, so the
                # flow ends FAILED_EMPTY straight from WAIT_TOKEN (a table
                # transition) with the problem screen -- never an internal error.
                self._why = {'build_plan': type(exc).__name__, 'why': mask(str(exc))[:120]}
                _log('build_plan_failed', flow_id=flow['flow_id'], **self._why)
                return self._view(flow, self._end(flow, 'FAILED_EMPTY', ctx, reason='build_plan_failed'))
        self._enter(flow, 'BUILDING', 'bot_confirm')
        self._save(flow)                                # intent durable before any start
        try:
            delivery = self._deliver(flow, ctx)
            w = self._start_worker(flow, secret)
            if not isinstance(w, dict):
                raise ValueError('worker.start result')
            if w.get('kind') == 'not_implemented':
                raise Stub(w)
        except Stub:
            snap['revision'] = flow['revision']
            self._save(snap)                            # nothing started: back to WAIT_TOKEN
            raise
        except Exception:
            return self._fail_no_worker(flow)
        if w.get('kind') != 'ok':
            return self._fail_no_worker(flow)
        worker = w.get('worker')
        if not isinstance(worker, dict) or not worker.get('pid'):
            return self._fail_no_worker(flow)
        flow['worker'] = worker
        self._save(flow)
        return self._view(flow, delivery)

    # -- WP4 hook points (worker/cleanup call these; all under the lock)
    def _hook(self, fn):
        with Locked(self.lock_path, self.lock_s):
            flow = self.load()
            if flow is None:
                return False
            return fn(flow)

    def mark_artifacts(self, created=None, started=None):
        """Record truth about what this flow made. Allowed while a writer may
        still run (exec states, ABORT_PENDING, CLEANING)."""
        def f(flow):
            if flow['state'] not in EXEC_STATES + ('ABORT_PENDING', 'CLEANING'):
                return False
            if created:
                flow['artifacts']['created'] = True
            if started:
                flow['artifacts'].update({'created': True, 'started': True})
            self._save(flow)
            return True
        return self._hook(f)

    def set_worker(self, worker):
        def f(flow):
            if flow['state'] not in EXEC_STATES:
                return False
            flow['worker'] = worker
            self._save(flow)
            return True
        return self._hook(f)

    def set_accept(self, accept):
        """Durable accept intent (previous ledger row, slug, path) BEFORE the
        ledger is touched, so compensation can tell "not yet" from "done"."""
        def f(flow):
            if flow['state'] not in EXEC_STATES + ('ABORT_PENDING', 'CLEANING'):
                return False
            flow['accept'] = accept
            self._save(flow)
            return True
        return self._hook(f)

    def advance(self, to):
        """BUILDING -> VERIFYING -> RECORDING only, and only from the exact
        predecessor: a worker cannot move a flow that was aborted under it."""
        want = {'VERIFYING': 'BUILDING', 'RECORDING': 'VERIFYING'}
        def f(flow):
            if to not in want or flow['state'] != want[to]:
                return False
            self._enter(flow, to)
            self._save(flow)
            return True
        return self._hook(f)

    def complete_done(self, ctx=None):
        def f(flow):
            if flow['state'] != 'RECORDING':
                return False
            self._end(flow, 'DONE', ctx or Ctx())
            return True
        try:
            return self._hook(f)
        except Stub:
            return True                                 # DONE is durable; handoff stays pending

    def finish_failed(self, ctx=None):
        def f(flow):
            if flow['state'] not in EXEC_STATES:
                return False
            flow['reason'] = 'worker_failed'
            if flow['state'] == 'BUILDING' and not flow['artifacts']['created']:
                self._end(flow, 'FAILED_EMPTY', ctx or Ctx())
                return True
            # DGN-1698: the failing runner is the writer and has stopped
            # writing, so this is the after-stop point. Created-but-not-started
            # goes to CLEANING and is driven right here -> EXPIRED (N11), which
            # a new `open` replaces. A started newborn stays
            # FAILED_WITH_ARTIFACTS until `cancel` proves ownership (dec-174).
            if after_stop_target(flow) == 'CLEANING':
                self._enter(flow, 'CLEANING')
                self._save(flow)
                self._drive(flow, ctx or Ctx())         # 'running'/Stub: status/sweep/open drive it on
            else:
                self._end(flow, 'FAILED_WITH_ARTIFACTS', ctx or Ctx())
            return True
        try:
            return self._hook(f)
        except Stub:
            return True


# -------------------------------------------------------------------- CLI
class Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


INBOUND_CTX_MAX_AGE_S = 1800      # a context older than this is a crashed turn's leftover


def read_inbound_context(root, now=None):
    """Per-turn facts the bridge publishes for tools (bridge/sdk_bridge.py
    inbound_context_path): the owner's chat / thread / inbound message id and
    the bridge process epoch. {} when there is no live owner turn (no file,
    unknown schema, stale, unreadable): nothing is guessed. Plain context --
    explicit flags and DOGANY_MINT_* env still win over it."""
    try:
        d = json.loads((Path(root) / '.telegram_bot' / 'inbound-context.json').read_text())
        age = (time.time() if now is None else now) - float(d['ts'])
        if d.get('schema') != 1 or age < 0 or age > INBOUND_CTX_MAX_AGE_S:
            return {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    def s(v):
        return None if v is None else str(v)
    return {'request_id': s(d.get('request_id')), 'epoch': s(d.get('runtime_epoch')),
            'owner_id': s(d.get('user_id')), 'chat_id': s(d.get('chat_id')),
            'thread_id': s(d.get('thread_id')), 'message_id': s(d.get('message_id'))}


def boot_identity():
    """This host boot's id. Linux: the kernel boot_id. macOS: the boot
    session UUID (DGN-1787: fixed for the whole boot; kern.boottime is
    derived from the wall clock and is only the last resort)."""
    forced = os.environ.get('DOGANY_BOOT_ID')
    if forced:
        return forced
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        pass
    for name in ('kern.bootsessionuuid', 'kern.boottime'):
        try:
            out = subprocess.run(['/usr/sbin/sysctl', '-n', name], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if name == 'kern.bootsessionuuid':
            if re.fullmatch(r'[0-9A-Fa-f-]{16,64}', out):
                return out
            continue
        m = re.search(r'sec\s*=\s*(\d+)', out)
        if m:
            return m.group(1)
    return None


def build_parser():
    p = _Parser(add_help=False, prog='mint')
    p.add_argument('--instance-root')
    p.add_argument('--request-id')
    p.add_argument('--runtime-epoch')
    p.add_argument('--owner-id')
    p.add_argument('--chat-id')
    p.add_argument('--thread-id')
    p.add_argument('--message-id')
    sub = p.add_subparsers(dest='verb')
    for name in ('open', 'next', 'status', 'cancel', 'sweep', 'table'):
        sp = sub.add_parser(name, add_help=False, parents=[], conflict_handler='resolve')
        if name in ('open', 'next'):
            sp.add_argument('--input')
            sp.add_argument('--lead')
            # open with text during WAIT_CONTEXT is a consult answer (DGN-1702)
            sp.add_argument('--asked')
            sp.add_argument('--more', action='store_true')
            sp.add_argument('--fit')
        if name == 'open':
            sp.add_argument('--browse', action='store_true')
    return p


def read_input(arg):
    if arg is None:
        return None
    if arg == '-':
        return sys.stdin.buffer.read(INPUT_MAX).decode('utf-8', 'replace')
    return arg[:INPUT_MAX]


def main(argv=None, adapters=None, out=None, err=None):
    out = out or sys.stdout
    def finish(em, rc=0):
        out.write(json.dumps(em, sort_keys=True) + '\n')
        return rc
    try:
        args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
        if args.verb is None:
            raise Usage('verb required')
        if args.verb == 'table':
            pub = {k: v for k, v in TABLE.items() if not k.startswith('_')}
            out.write(json.dumps(pub, sort_keys=True, indent=1) + '\n')
            return 0
        if not args.instance_root or not Path(args.instance_root).is_dir():
            raise Usage('--instance-root must be an existing directory')
        env = os.environ.get
        if args.verb == 'next' and args.input is None:
            raise Usage('next requires --input')
        inb = read_inbound_context(args.instance_root)
        ctx = Ctx(request_id=args.request_id or env('DOGANY_MINT_REQUEST_ID') or inb.get('request_id'),
                  epoch=args.runtime_epoch or env('DOGANY_MINT_RUNTIME_EPOCH') or inb.get('epoch'),
                  boot_id=boot_identity(),
                  owner_id=args.owner_id or env('DOGANY_MINT_OWNER_ID') or inb.get('owner_id'),
                  chat_id=args.chat_id or env('DOGANY_MINT_CHAT_ID') or inb.get('chat_id'),
                  thread_id=args.thread_id or env('DOGANY_MINT_THREAD_ID') or inb.get('thread_id'),
                  message_id=args.message_id or env('DOGANY_MINT_MESSAGE_ID') or inb.get('message_id'),
                  lead=getattr(args, 'lead', None), browse=getattr(args, 'browse', False),
                  asked=getattr(args, 'asked', None), more=getattr(args, 'more', False),
                  fit=getattr(args, 'fit', None))
        text = read_input(getattr(args, 'input', None))
        if adapters is None:
            # Runtime wiring lives here, not in the shell wrapper, so direct
            # Python invocation and mint_flow.sh have identical real seams.
            import mint_flow_worker as worker_mod
            adapters = worker_mod.runtime_adapters(args.instance_root)
        eng = Engine(args.instance_root, adapters,
                     idle_s=int(env('DOGANY_MINT_IDLE_S') or IDLE_S),
                     exec_s=int(env('DOGANY_MINT_EXEC_S') or EXEC_S),
                     lock_s=float(env('DOGANY_MINT_LOCK_S') or LOCK_S))
        em = eng.call(args.verb, ctx, text)
        return finish(em, 3 if 'rerun_with' in em else 0)
    except Usage as exc:
        _log('usage', error=str(exc))
        return finish(emit('BLOCKED', 'none', 'BAD_REQUEST'), 2)
    except Exception as exc:                            # never echo a message: it may hold input
        print('mint: internal error (%s)' % type(exc).__name__, file=err or sys.stderr)
        return finish(emit('BLOCKED', 'none', 'STATE_UNVERIFIABLE'), 1)


if __name__ == '__main__':
    sys.exit(main())
