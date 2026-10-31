#!/usr/bin/env python3
"""DGN-1683 WP4: pipeline worker, process tree, cleanup.

Implements the WP4 seams of lib/mint_flow.py:

  ProcessWorker(mf.Worker)   start / stop / alive
  FlowCleanup(mf.Cleanup)    run
  adapters(...)              convenience: an mf.Adapters wired with both

and the detached runner (`mint_flow_worker.py run ...`) that ProcessWorker
spawns. Nothing here intercepts an owner message and nothing here speaks to the
owner except through the engine's own funnel (Engine.complete_done /
finish_failed send the terminal screen: grill v4 fix 1).

WORKER (grill v3 fix 5)
  start()  spawns the runner in a DEDICATED SESSION/PROCESS GROUP (pgid == pid).
           The runner prints READY and then blocks on stdin. The parent records
           pid + pgid + process start identity + a nonce in flow['worker'] and
           PERSISTS the flow; only then does it write GO (one JSON line: nonce
           and the secret). EOF or silence before GO -> the runner exits having
           mutated nothing. The secret rides that pipe only: never argv, never
           the flow, never a log (child output is masked on the way to disk).
  stop()   TERM the group and any descendant that left it, bounded wait, KILL,
           bounded wait, then PROVE it: no live process with that pgid and none
           of the recorded descendants. PID reuse is refused (start identity
           mismatch -> blocked). Not provable -> {"kind": "blocked"}.
  The runner runs `mint_run.sh pipeline --no-start --no-state` as a BLACK BOX
  (dry-run first), inside the controller contract of mint_run.sh (env
  DOGANY_MINT_CONTROLLER_FLOW / _NONCE, verified against the flow file), then:
  start units -> VERIFYING (Readiness) -> RECORDING (accept + read-back) ->
  complete_done (the engine sends the DONE screen). Any failure -> report file
  + finish_failed (the engine sends the PROBLEM/ended screen).

FLOW RECEIPT (grill v4 fix 8)   <instance>/.mint-flow/<flow_id>.receipt.json
  Written by the runner at GO, BEFORE the pipeline can create anything: flow id,
  slug, physical target root, preimage (absent|empty). It is the evidence
  teardown accepts (mint_run.sh teardown --flow-receipt) once
  `pipeline --no-start` has closed .mint-incomplete, and the proof that
  everything under the root was created by this flow.

CLEANUP (dec-174 + grill v3 fix 5/6, v4 fix 4)
  run() is idempotent and evidence-driven:
    1. own-newborn stop: only when artifacts.started AND ownership is proved
       (receipt matches flow/slug/root, preimage empty). Units come from the
       root's own plists; bootout + prove unit gone and no process on the root.
    2. archive: mint_run.sh teardown with the flow receipt (reversible
       archive); a partial root without .instance.conf is archived by a
       wrapper-owned move (receipt-proved partial root only).
    3. accept compensation: mint_retire.ledger_plan finds the binding; the
       clear is compare-and-set on slug AND physical path under the ONE shared
       minting-state lock (minting_policy.state_lock, also taken by
       minting_state.py and weekly_eval.py). A different binding is preserved
       and blocks.
    4. read-back: root, unit registrations, LaunchAgents copies, handoff
       mailbox, mint journal, crew rows, ledger binding. Only if all are clean
       -> {"kind": "complete"}; anything left -> blocked (+ report).

Stated limits: (a) a descendant that called setsid AND whose parent died before
stop() cannot be attributed; teardown's own liveness guard (root in a process
command line) is the second net and blocks. (b) The local report file is
always written. The notice goes to the OPERATOR only (DGN-1821): routines/
push.sh --audience operator --no-owner-fallback, resolved from the instance's
STEWARD_ENV_FILE (DGN-1585 / DGN-1808 dec-220). No operator resolvable -> no
send; the owner's path is the engine's own PROBLEM screen, never this report.
No recipient slug is hardcoded here.
"""
import argparse
import importlib
import json
import os
import plistlib
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mint_flow as mf                      # noqa: E402
import mint_flow_guard as guard             # noqa: E402

REPO = HERE.parents[2]
DEFAULT_MINT_RUN = HERE.parent / 'mint_run.sh'
MINTING_STATE = REPO / 'skills/dogany-mint-agent/scripts/minting_state.py'

READY_WAIT_S = 15.0        # parent waits this long for READY
GO_WAIT_S = 30.0           # runner waits this long for GO
TERM_WAIT_S = 5.0
KILL_WAIT_S = 5.0
POLL_S = 0.1
STOP_ATTEMPTS = 3          # 'running' answers before stop() gives up as blocked
UNIT_WAIT_S = 10.0
CLEAN_ATTEMPTS = 3
HOOK_RETRIES = 6
CHANNELS = ('release', 'dev', 'staging', 'main')
MODELS = ('sonnet', 'opus', 'haiku')
_WORD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


# ------------------------------------------------------------------ paths
def flow_dir(instance_root):
    return Path(instance_root) / '.mint-flow'


def receipt_path(instance_root, flow_id):
    return flow_dir(instance_root) / (flow_id + '.receipt.json')


def _ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)


def _real(p):
    return os.path.realpath(str(p))


def _dir_state(root):
    """'absent' | 'empty' | 'nonempty' | 'other' for a target root."""
    if not os.path.lexists(root):
        return 'absent'
    if os.path.islink(root) or not os.path.isdir(root):
        return 'other'
    return 'nonempty' if os.listdir(root) else 'empty'


# ------------------------------------------------------------------- plan
def plan_from_flow(flow):
    """The pipeline request the flow carries. Raises ValueError. Every value is
    checked before it can become an argv element (no option smuggling: nothing
    may start with '-'; paths are absolute and normalized)."""
    t, sel = flow.get('target'), flow.get('selection')
    if not isinstance(t, dict) or not isinstance(sel, dict):
        raise ValueError('flow has no target/selection')
    slug, root, pack, domain = t.get('slug'), t.get('root'), sel.get('pack_id'), sel.get('domain')
    for name, v in (('slug', slug), ('pack_id', pack), ('domain', domain)):
        if not isinstance(v, str) or not _WORD.match(v):
            raise ValueError('bad ' + name)
    if not isinstance(root, str) or not os.path.isabs(root) or os.path.normpath(root) != root \
            or root in ('/', os.path.expanduser('~')):
        raise ValueError('bad root')
    plan = {'slug': slug, 'root': root, 'pack': pack, 'domain': domain, 'extra': []}
    for key, flag in (('catalog', '--catalog'),):
        v = sel.get(key)
        if v is not None:
            if not isinstance(v, str) or not os.path.isabs(v):
                raise ValueError('bad ' + key)
            plan['extra'] += [flag, v]
    for key, flag in (('source_tag', '--source-tag'), ('untagged_local', '--untagged-local'), ('role', '--role')):
        v = sel.get(key)
        if v is not None:
            if not isinstance(v, str) or not v.strip() or v.startswith('-'):
                raise ValueError('bad ' + key)
            plan['extra'] += [flag, v]
    if sel.get('source_tag') is not None and sel.get('untagged_local') is not None:
        raise ValueError('source_tag and untagged_local are contradictory')
    for key, flag, allowed in (('channel', '--channel', CHANNELS), ('model', '--model', MODELS)):
        v = sel.get(key)
        if v is not None:
            if v not in allowed:
                raise ValueError('bad ' + key)
            plan['extra'] += [flag, v]
    if sel.get('no_venv') is True:
        plan['extra'].append('--no-venv')
    return plan


# ----------------------------------------------------------------- tools
class Proc:
    def __init__(self, pid, ppid, pgid, stat, lstart, command=''):
        self.pid, self.ppid, self.pgid, self.stat, self.lstart, self.command = pid, ppid, pgid, stat, lstart, command

    @property
    def alive(self):
        return not self.stat.startswith('Z')


class Tools:
    """Every OS probe/actuator in one place so tests can point them at stubs."""
    def __init__(self, ps='ps', launchctl='launchctl', sleep=time.sleep, clock=time.monotonic,
                 term_wait=TERM_WAIT_S, kill_wait=KILL_WAIT_S, unit_wait=UNIT_WAIT_S, uid=None):
        self.ps, self.launchctl, self.sleep, self.clock = ps, launchctl, sleep, clock
        self.term_wait, self.kill_wait, self.unit_wait = term_wait, kill_wait, unit_wait
        self.uid = os.getuid() if uid is None else uid

    def _run(self, argv, timeout=15):
        env = dict(os.environ, LC_ALL='C')
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)

    def snapshot(self):
        out = self._run([self.ps, '-Ao', 'pid=,ppid=,pgid=,stat=,lstart=']).stdout
        procs = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
                procs.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4].strip()))
        return procs

    def commands(self):
        """pid -> full command line (one ps call)."""
        out = self._run([self.ps, '-Ao', 'pid=,command=']).stdout
        res = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                res[int(parts[0])] = parts[1]
        return res

    def lstart(self, pid):
        for p in self.snapshot():
            if p.pid == pid and p.alive:
                return p.lstart
        return None

    def signal_group(self, pgid, sig):
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

    def signal_pid(self, pid, sig):
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

    def unit_registered(self, label):
        r = self._run([self.launchctl, 'print', 'gui/%d/%s' % (self.uid, label)])
        return r.returncode == 0

    def unit_bootout(self, label):
        return self._run([self.launchctl, 'bootout', 'gui/%d/%s' % (self.uid, label)]).returncode == 0


def tree_pids(snap, seeds):
    """seeds plus every live descendant (ppid walk), whatever its pgid."""
    kids = {}
    for p in snap:
        if p.alive:
            kids.setdefault(p.ppid, []).append(p.pid)
    out, todo = set(seeds), list(seeds)
    while todo:
        for k in kids.get(todo.pop(), ()):
            if k not in out:
                out.add(k)
                todo.append(k)
    return out


def stop_tree(rec, tools):
    """Terminate the worker's whole tree and prove it. -> 'gone'|'running'|'blocked'."""
    pid, pgid = rec['pid'], rec['pgid']
    snap = tools.snapshot()
    by_pid = {p.pid: p for p in snap}
    leader = by_pid.get(pid)
    if leader is not None and leader.alive and (leader.lstart != rec['start'] or leader.pgid != pgid):
        return 'blocked'                          # pid reused / not the process we registered
    members = {p.pid for p in snap if p.pgid == pgid and p.alive}
    reach = tree_pids(snap, members)
    escaped = {q: by_pid[q].lstart for q in reach - members if q in by_pid}   # left the group
    if not members and not escaped:
        return 'gone'

    def alive_now():
        cur = tools.snapshot()
        cur_by = {p.pid: p for p in cur}
        if any(p.pgid == pgid and p.alive for p in cur):
            return True
        return any(q in cur_by and cur_by[q].alive and cur_by[q].lstart == ls for q, ls in escaped.items())

    def deliver(sig):
        ok = True
        if members:
            ok = tools.signal_group(pgid, sig) and ok
        for q in escaped:
            ok = tools.signal_pid(q, sig) and ok
        return ok

    def wait(limit):
        end = tools.clock() + limit
        while tools.clock() < end:
            if not alive_now():
                return True
            tools.sleep(POLL_S)
        return not alive_now()

    if not deliver(signal.SIGTERM):
        return 'blocked'
    if wait(tools.term_wait):
        return 'gone'
    if not deliver(signal.SIGKILL):
        return 'blocked'
    return 'gone' if wait(tools.kill_wait) else 'running'


# -------------------------------------------------------- receipts, reports
def load_receipt(instance_root, flow):
    """The receipt iff it proves THIS flow owns the target root; else None."""
    try:
        rc = json.loads(receipt_path(instance_root, flow['flow_id']).read_text())
        t = flow['target']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(rc, dict) or rc.get('schema') != 1 or rc.get('flow_id') != flow['flow_id'] \
            or not isinstance(t, dict) or rc.get('slug') != t.get('slug') \
            or _real(rc.get('root') or '/nonexistent') != _real(t.get('root') or '/nonexistent2') \
            or rc.get('preimage') not in ('absent', 'empty'):
        return None
    return rc


def write_receipt(instance_root, flow, plan, preimage):
    _ensure_dir(flow_dir(instance_root))
    mf.atomic_write_json(receipt_path(instance_root, flow['flow_id']), {
        'schema': 1, 'flow_id': flow['flow_id'], 'slug': plan['slug'], 'root': _real(plan['root']),
        'preimage': preimage, 'written_at': time.time(), 'newborn_stopped': False})


def operator_text(flow, phase):
    """DGN-1821: the operator notice. Plain words only: push.sh refuses any
    execution identifier (absolute path, hex id, rc=N), so the flow id, the
    detail and the paths stay in the local report file this points at."""
    t = flow.get('target') or {}
    return mf.mask('Mint flow problem (machine generated): step "%s" did not finish for the new agent "%s". '
                   'State %s, reason %s. The full report (flow id, detail, logs) is in the .mint-flow '
                   'folder of the ordering instance.' % (phase, t.get('slug') or 'unknown',
                                                         flow.get('state'), flow.get('reason')))


def notify_operator(instance_root, text, sender=None, timeout=30):
    """Operator-only send through the instance's own push.sh (DGN-1808 dec-220
    route). Returns the outcome line recorded in the local report."""
    cmd = sender if sender is not None else str(Path(instance_root) / 'routines/push.sh')
    if not os.path.isfile(cmd):
        return 'operator: not sent (no push.sh in this instance)'
    try:
        rc = subprocess.run(['bash', cmd, '--text', text, '--audience', 'operator', '--no-owner-fallback'],
                            capture_output=True, text=True, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError) as e:
        return 'operator: not sent (%s)' % type(e).__name__
    if rc == 0:
        return 'operator: sent'
    if rc == 9:
        return 'operator: none resolvable (STEWARD_ENV_FILE); local report only, owner not messaged'
    return 'operator: send failed rc=%d' % rc


def file_report(instance_root, flow, phase, detail='', sender=None, timeout=30):
    """The behind-the-scenes report (PROBLEM path). Facts only; never a token,
    never owner copy. The local file is always written; the operator gets a
    plain notice when one is resolvable (notify_operator), else nobody is
    messaged from here (the owner already has the PROBLEM screen). The send
    outcome is appended to the file. Returns the report path."""
    fid = flow.get('flow_id', 'unknown')
    art = flow.get('artifacts') or {}
    t = flow.get('target') or {}
    body = mf.mask('\n'.join([
        'mint flow problem report (machine generated)',
        'flow_id: %s' % fid, 'phase: %s' % phase, 'state: %s' % flow.get('state'),
        'reason: %s' % flow.get('reason'), 'slug: %s' % t.get('slug'), 'root: %s' % t.get('root'),
        'artifacts: created=%s started=%s owned=%s' % (art.get('created'), art.get('started'), art.get('owned')),
        'detail: %s' % detail,
        'pipeline log: %s' % (flow_dir(instance_root) / (fid + '.pipeline.log')),
        'receipt: %s' % receipt_path(instance_root, fid), '']))
    path = flow_dir(instance_root) / (fid + '.report.txt')
    try:
        _ensure_dir(flow_dir(instance_root))
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(body)
    except OSError:
        return None
    outcome = notify_operator(instance_root, operator_text(flow, phase), sender, timeout)
    try:
        with open(str(path), 'a') as fh:
            fh.write(outcome + '\n')
    except OSError:
        pass
    return str(path)


def _report_once(instance_root, flow, key, phase, detail, sender):
    """Mark on the flow (in place) so a re-driven cleanup does not re-report."""
    holder = flow['cleanup']
    if holder.get('reported', {}).get(key):
        return
    holder.setdefault('reported', {})[key] = file_report(instance_root, flow, phase, detail, sender) or True


# ---------------------------------------------------------------- worker
class ProcessWorker(mf.Worker):
    def __init__(self, instance_root, tools=None, mint_run=None, adapters_factory=None,
                 python=None, report_sender=None, ready_wait=READY_WAIT_S):
        self.ir = str(instance_root)
        self.tools = tools or Tools()
        self.mint_run = str(mint_run or DEFAULT_MINT_RUN)
        self.factory = adapters_factory              # "module:callable(instance_root) -> mf.Adapters"
        self.python = python or sys.executable
        self.sender = report_sender
        self.ready_wait = ready_wait
        self._children = []

    def start(self, flow, secret=None, persist=None):
        if persist is None or flow.get('state') != 'BUILDING':
            return {'kind': 'failed'}
        try:
            plan_from_flow(flow)
        except ValueError:
            return {'kind': 'failed'}
        fdir = flow_dir(self.ir)
        _ensure_dir(fdir)
        cmd = [self.python, str(Path(__file__).resolve()), 'run', '--instance-root', self.ir,
               '--flow-id', flow['flow_id'], '--mint-run', self.mint_run]
        if self.factory:
            cmd += ['--adapters-factory', self.factory]
        env = {k: v for k, v in os.environ.items()
               if k not in ('DOGANY_BOT_TOKEN', 'DOGANY_MINT_CONTROLLER_FLOW', 'DOGANY_MINT_CONTROLLER_NONCE')}
        log_fd = os.open(str(fdir / (flow['flow_id'] + '.runner.log')), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_fd, env=env,
                                    start_new_session=True, close_fds=True)
        except OSError:
            return {'kind': 'failed'}
        finally:
            os.close(log_fd)
        self._children.append(proc)

        def fail():
            self.tools.signal_group(proc.pid, signal.SIGKILL)
            flow['worker'] = None
            return {'kind': 'failed'}

        ready, _, _ = select.select([proc.stdout], [], [], self.ready_wait)
        if not ready or proc.stdout.readline().strip() != b'READY':
            return fail()
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            return fail()
        start = self.tools.lstart(proc.pid)
        if pgid != proc.pid or not start:
            return fail()
        nonce = secrets.token_hex(16)
        flow['worker'] = {'pid': proc.pid, 'pgid': pgid, 'start': start, 'nonce': nonce,
                          'runner': 'mint_flow_worker', 'spawned_at': time.time()}
        try:
            persist()                                # identity durable BEFORE go
            # WP2's Secret intentionally cannot be JSON serialized. Reveal it
            # only at the parent->child pipe boundary; it is never placed in
            # argv, flow state, the runner environment, or a log.
            secret_value = secret.reveal() if hasattr(secret, 'reveal') else secret
            if secret_value is not None and not isinstance(secret_value, str):
                return fail()
            proc.stdin.write((json.dumps({'go': True, 'nonce': nonce, 'secret': secret_value}) + '\n').encode())
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError):
            return fail()
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
        return {'kind': 'ok', 'worker': flow['worker']}

    def alive(self, flow):
        rec = flow.get('worker')
        if not isinstance(rec, dict) or not rec.get('pid'):
            return None
        for p in self.tools.snapshot():
            if p.pid == rec['pid']:
                return p.alive and p.lstart == rec.get('start')
        return False

    def report_dead(self, flow):
        _report_once(self.ir, flow, 'worker_dead', 'worker death',
                     'runner died before completing the mint flow', self.sender)

    def stop(self, flow):
        rec = flow.get('worker')
        if isinstance(rec, dict) and rec.get('pid') and not rec.get('stopped'):
            res = stop_tree(rec, self.tools)
            if res == 'running':
                rec['stop_attempts'] = rec.get('stop_attempts', 0) + 1
                if rec['stop_attempts'] < STOP_ATTEMPTS:
                    return {'kind': 'running'}
                res = 'blocked'
            if res == 'blocked':
                _report_once(self.ir, flow, 'stop', 'worker stop', 'writer tree could not be proven gone', self.sender)
                return {'kind': 'blocked'}
            rec['stopped'] = True
        self._refresh_artifacts(flow)
        art = flow['artifacts']
        if art['started']:
            art['owned'] = self._owned(flow)
            if not art['owned']:
                _report_once(self.ir, flow, 'owner', 'worker stop',
                             'newborn was started but ownership cannot be proven; preserved', self.sender)
        return {'kind': 'ok'}

    def _refresh_artifacts(self, flow):
        """created := the receipt proves an empty/absent preimage AND the root
        now holds something (everything under it is this flow's)."""
        rc = load_receipt(self.ir, flow)
        if rc and _dir_state(flow['target']['root']) == 'nonempty':
            flow['artifacts']['created'] = True

    def owned(self, flow):
        """Read-only ownership proof (flow receipt), no stop (DGN-1698)."""
        return self._owned(flow)

    def _owned(self, flow):
        rc = load_receipt(self.ir, flow)
        root = (flow.get('target') or {}).get('root')
        return bool(rc and root and _dir_state(root) == 'nonempty')


# --------------------------------------------------------------- cleanup
def unit_labels(root):
    """Launchd labels of the root's OWN plists (the same two globs
    `mint_run.sh start` loads from). None when a plist cannot be read."""
    labels = []
    for sub in ('bridge', 'routines'):
        for p in sorted(Path(root, sub).glob('*.plist')):
            try:
                lab = plistlib.loads(p.read_bytes()).get('Label')
            except Exception:
                return None
            if not isinstance(lab, str) or not lab:
                return None
            labels.append(lab)
    return labels


class FlowCleanup(mf.Cleanup):
    def __init__(self, instance_root, tools=None, mint_run=None, archive_root=None, handoff_root=None,
                 report_sender=None, env=None):
        self.ir = str(instance_root)
        self.tools = tools or Tools()
        self.mint_run = str(mint_run or DEFAULT_MINT_RUN)
        home = Path(os.environ.get('HOME', '/'))
        self.archive_root = str(archive_root or home / '.dogany/torn-down')
        self.handoff_root = str(handoff_root or os.environ.get('DOGANY_HANDOFF_ROOT') or home / '.dogany/handoff')
        self.sender = report_sender
        self.env = env

    # -- entry
    def run(self, flow):
        c = flow['cleanup']
        try:
            res = self._run(flow)
        except Exception as exc:                              # never echo: may hold input
            c['remaining'] = ['internal error: ' + type(exc).__name__]
            res = 'blocked'
        if res == 'running':
            c['attempts'] = c.get('attempts', 0) + 1
            if c['attempts'] < CLEAN_ATTEMPTS:
                return {'kind': 'running'}
            c['remaining'] = c.get('remaining') or ['units still registered']
            res = 'blocked'
        if res == 'blocked':
            _report_once(self.ir, flow, 'cleanup', 'cleanup', 'remaining: ' + ', '.join(c.get('remaining') or []),
                         self.sender)
            return {'kind': 'blocked'}
        c['remaining'] = []
        return {'kind': 'complete'}

    def _run(self, flow):
        c, art = flow['cleanup'], flow['artifacts']
        root = (flow.get('target') or {}).get('root')
        c['remaining'] = []
        rc = load_receipt(self.ir, flow)
        if root:
            state = _dir_state(root)
            if state == 'other' or (state == 'nonempty' and rc is None):
                c['remaining'] = ['target root ownership cannot be proven']
                return 'blocked'
            if state == 'nonempty' and c.get('labels') is None:
                c['labels'] = unit_labels(root)              # remembered: the root is about to move
                if c['labels'] is None:
                    c['remaining'] = ['unit plist unreadable']
                    return 'blocked'
            # 1. own-newborn stop (dec-174): only a flow-started, ownership-proved unit
            if art['started']:
                if rc is None:
                    c['remaining'] = ['newborn ownership cannot be proven']
                    return 'blocked'
                res = self._stop_units(flow, rc, root)
                if res != 'ok':
                    return res
            # 2. archive
            if state == 'nonempty':
                res = self._archive(flow, root, rc)
                if res != 'ok':
                    return res
        # 3. accept compensation
        res = self._compensate_accept(flow)
        if res != 'ok':
            return res
        # 4. read-back
        return self._readback(flow, root)

    # -- 1. units
    def _stop_units(self, flow, rc, root):
        c, tools = flow['cleanup'], self.tools
        labels = c.get('labels') or []
        if not c.get('units_stopped'):
            for lab in labels:
                if tools.unit_registered(lab):
                    tools.unit_bootout(lab)
            end = tools.clock() + tools.unit_wait
            while True:
                left = [lab for lab in labels if tools.unit_registered(lab)]
                busy = self._root_processes(root)
                if not left and not busy:
                    break
                if tools.clock() >= end:
                    c['remaining'] = ['unit still registered: ' + x for x in left] + \
                                     ['process still on root: %d' % p for p in busy]
                    return 'running'
                tools.sleep(POLL_S)
            c['units_stopped'] = True
        if not rc.get('newborn_stopped'):
            rc = dict(rc, newborn_stopped=True)
            mf.atomic_write_json(receipt_path(self.ir, flow['flow_id']), rc)
        return 'ok'

    def _root_processes(self, root):
        real, mine = _real(root), os.getpid()
        pids = set()
        for pid, cmd in self.tools.commands().items():
            if pid != mine and (root in cmd or real in cmd):
                pids.add(pid)
        snap = self.tools.snapshot()
        anc, cur, by = set(), mine, {p.pid: p for p in snap}
        while cur in by and cur > 1 and cur not in anc:
            anc.add(cur)
            cur = by[cur].ppid
        return sorted(pids - anc)

    # -- 2. archive
    def _env(self):
        env = dict(self.env if self.env is not None else os.environ)
        env['DOGANY_HANDOFF_ROOT'] = self.handoff_root
        return env

    def _archive(self, flow, root, rc):
        c = flow['cleanup']
        if (Path(root) / '.instance.conf').is_file():
            argv = [self.mint_run, 'teardown', '--root', root, '--instance-root', self.ir,
                    '--archive-dir', self.archive_root, '--flow-receipt', str(receipt_path(self.ir, flow['flow_id']))]
            r = subprocess.run(argv, capture_output=True, text=True, env=self._env(), timeout=300)
            for line in r.stdout.splitlines():
                if line.startswith('/') and os.path.isdir(line.strip()):
                    c['archive'] = line.strip()
            if r.returncode != 0:
                c['remaining'] = ['teardown rc=%d' % r.returncode]
                return 'blocked'                              # incl. rc 6: crew leave failed
            return 'ok'
        # partial root (no .instance.conf): teardown refuses by design; the
        # receipt proves every byte under it is this flow's, so a reversible
        # move is safe. Same archive shape idea: <archive>/<name>.<ts>/instance.
        if rc is None:
            c['remaining'] = ['partial root ownership cannot be proven']
            return 'blocked'
        arch = Path(self.archive_root) / ('%s.partial.%s' % (Path(root).name, time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())))
        n = 0
        while arch.exists():
            n += 1
            arch = Path(str(arch) + '-%d' % n)
        arch.mkdir(parents=True)
        os.chmod(str(arch), 0o700)
        mf.atomic_write_json(arch / 'RECEIPT.json', {
            'schema': 1, 'kind': 'partial-root', 'flow_id': flow['flow_id'], 'slug': rc['slug'],
            'original_root': rc['root'], 'moved': {'instance': 'instance'}})
        os.rename(root, str(arch / 'instance'))
        c['archive'] = str(arch)
        return 'ok'

    # -- 3. accept binding
    def _compensate_accept(self, flow):
        c, acc = flow['cleanup'], flow.get('accept')
        if not isinstance(acc, dict):
            return 'ok'
        ledger = Path(self.ir) / 'config/minting-state.json'
        ml = importlib.import_module('mint_retire')
        try:
            plan = ml.ledger_plan(Path(_real(acc['path'])), acc['slug'], self.ir)
        except ml.Refusal as exc:
            c['remaining'] = ['accept binding points elsewhere: ' + exc.detail[:80]]
            return 'blocked'
        entries = [e for e in plan.get('entries', []) if e['domain'] == acc['domain']]
        row = {}
        if ledger.exists():
            row = json.loads(ledger.read_text()).get('domains', {}).get(acc['domain'], {})
        if not entries:
            if row.get('dedicated_agent') in (None, ''):
                return 'ok'                                   # already clean (idempotent)
            c['remaining'] = ['ledger binding of domain %s belongs to another agent' % acc['domain']]
            return 'blocked'
        r = subprocess.run([sys.executable, str(MINTING_STATE), 'clear', acc['domain'], '--agent', acc['slug'],
                            '--path', acc['path'], '--state', str(ledger)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            c['remaining'] = ['ledger clear refused (compare-and-set)']
            return 'blocked'
        return 'ok'

    # -- 4. read-back
    def _readback(self, flow, root):
        c = flow['cleanup']
        left = []
        t = flow.get('target') or {}
        slug = t.get('slug')
        if root and _dir_state(root) not in ('absent', 'empty'):
            left.append('target root still present')
        for lab in c.get('labels') or []:
            if self.tools.unit_registered(lab):
                left.append('unit registered: ' + lab)
        if root:
            # start copies source plist filenames verbatim; use retire's
            # path/content ownership inventory rather than inventing
            # `<label>.plist` (notably misses `.newbridge.plist`).
            try:
                ml = importlib.import_module('mint_retire')
                for path in ml.installed_plist_paths(root):
                    left.append('launchagent copy: ' + path.name)
            except Exception:
                left.append('launchagent ownership unreadable')
        if slug:
            if (Path(self.handoff_root) / slug).exists():
                left.append('handoff mailbox')
            if (Path(self.ir) / '.mint-journal' / (slug + '.json')).exists():
                left.append('mint journal')
        if root and self._crew_rows(root):
            left.append('crew membership')
        acc = flow.get('accept')
        if isinstance(acc, dict):
            ledger = Path(self.ir) / 'config/minting-state.json'
            if ledger.exists():
                row = json.loads(ledger.read_text()).get('domains', {}).get(acc['domain'], {})
                if row.get('dedicated_agent') == acc['slug']:
                    left.append('ledger binding')
        if left:
            c['remaining'] = left
            return 'blocked'
        return 'ok'

    def _crew_rows(self, root):
        script = 'set -uo pipefail; . "$1/lib/mint_teardown.sh"; _crew_leave_matches "$2"'
        r = subprocess.run(['bash', '-c', script, '_', str(HERE.parent), root], capture_output=True, text=True,
                           env=self._env(), timeout=60)
        return [x for x in r.stdout.splitlines() if x.strip()]


def adapters(instance_root, screens=None, token=None, readiness=None, registration=None, tools=None,
             mint_run=None, adapters_factory=None, archive_root=None, handoff_root=None, report_sender=None):
    """An mf.Adapters with the WP4 seams wired (the integrator's one call).
    `registration` (WP5, optional) has join(flow) -> {"kind": "ok"} and is run by
    the worker after the pipeline and before start."""
    ad = mf.Adapters(screens=screens, token=token, readiness=readiness,
                     ledger=mf.LedgerGate(instance_root),
                     worker=ProcessWorker(instance_root, tools=tools, mint_run=mint_run,
                                          adapters_factory=adapters_factory, report_sender=report_sender),
                     cleanup=FlowCleanup(instance_root, tools=tools, mint_run=mint_run, archive_root=archive_root,
                                         handoff_root=handoff_root, report_sender=report_sender))
    ad.registration = registration
    return ad


def runtime_adapters(instance_root):
    """The canonical production adapter set used by mint_flow.main and runner.

    Kept import-local to avoid mint_flow <-> worker import initialization
    cycles and to retain injectable seams for the hermetic work-package tests.
    """
    from mint_screens import MintScreens
    from mint_token import TokenAdapter
    from mint_ready import CrewRegistration, Readiness
    return adapters(instance_root, screens=MintScreens(instance_root),
                    token=TokenAdapter(instance_root), readiness=Readiness(instance_root),
                    registration=CrewRegistration(), adapters_factory='mint_flow_worker:runtime_adapters')


# ---------------------------------------------------------------- runner
class Aborted(Exception):
    """The flow left the state this step needs (cancelled/expired under us)."""


class Runner:
    def __init__(self, engine, adapters_, mint_run, instance_root, secret, nonce, flow_id, sender=None):
        self.eng, self.ad, self.mint_run = engine, adapters_, str(mint_run)
        self.ir, self.secret, self.nonce, self.flow_id, self.sender = str(instance_root), secret, nonce, flow_id, sender
        self.gate_file = flow_dir(instance_root) / (flow_id + '.gate')
        self.log_path = flow_dir(instance_root) / (flow_id + '.pipeline.log')

    # -- helpers
    def _log(self, msg):
        print(mf.mask(msg), file=sys.stderr, flush=True)

    def _hook(self, fn, *a, **kw):
        for _ in range(HOOK_RETRIES):
            try:
                return fn(*a, **kw)
            except mf.LockBusy:
                time.sleep(0.2)
        raise mf.LockBusy()

    def _flow(self, want):
        """The current flow, or Aborted unless it is still ours and in `want`."""
        try:
            flow = self.eng.load()
        except mf.Corrupt:
            raise Aborted()
        w = (flow or {}).get('worker') or {}
        if flow is None or flow['flow_id'] != self.flow_id or w.get('nonce') != self.nonce \
                or w.get('pid') != os.getpid() or flow['state'] not in want:
            raise Aborted()
        return flow

    def _fail(self, phase, detail):
        flow = self.eng.load() or {}
        flow.setdefault('artifacts', {})
        file_report(self.ir, flow, phase, detail, self.sender)
        self._hook(self.eng.finish_failed)

    def _gate(self):
        try:
            for line in self.gate_file.read_text().splitlines():
                if line.startswith('gate_id='):
                    return line[8:].strip()
        except OSError:
            pass
        return ''

    def _mask(self, text):
        text = mf.mask(text)
        return text.replace(self.secret, '<token>') if self.secret else text

    # -- pipeline as a black box
    def pipeline(self, plan, dry, deadline):
        argv = [self.mint_run, 'pipeline', '--slug', plan['slug'], '--pack', plan['pack'], '--root', plan['root'],
                '--instance-root', self.ir, '--no-start', '--no-state'] + plan['extra'] + (['--dry-run'] if dry else [])
        env = dict(os.environ, DOGANY_MINT_CONTROLLER_FLOW=self.flow_id, DOGANY_MINT_CONTROLLER_NONCE=self.nonce,
                   DOGANY_GATE_STATE_FILE=str(self.gate_file))
        if self.secret:
            env['DOGANY_BOT_TOKEN'] = self.secret
        _ensure_dir(flow_dir(self.ir))
        log_fd = os.open(str(self.log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        def pump():
            with os.fdopen(log_fd, 'w') as log:
                for raw in proc.stdout:
                    log.write(self._mask(raw.decode('utf-8', 'replace')))
                    log.flush()
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        try:
            rc = proc.wait(timeout=max(30.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            self._log('pipeline overran the exec deadline: killing the whole worker group')
            os.killpg(os.getpgrp(), signal.SIGKILL)               # includes this runner; sweep reports it
            raise
        th.join(timeout=5)
        return rc

    def _run_step(self, argv, phase):
        r = subprocess.run(argv, capture_output=True, text=True, timeout=600, env=dict(os.environ))
        if r.returncode != 0:
            self._log('%s rc=%d %s' % (phase, r.returncode, self._mask(r.stderr[-300:])))
        return r.returncode

    # -- accept
    def accept(self, plan):
        ledger = Path(self.ir) / 'config/minting-state.json'
        row = None
        if ledger.exists():
            row = json.loads(ledger.read_text()).get('domains', {}).get(plan['domain'])
        if isinstance(row, dict) and row.get('dedicated_agent'):
            return 'domain already has a dedicated agent'
        rootp = _real(plan['root'])
        acc = {'domain': plan['domain'], 'slug': plan['slug'], 'path': rootp, 'previous': row, 'status': 'intent'}
        if not self._hook(self.eng.set_accept, acc):
            raise Aborted()
        r = subprocess.run([sys.executable, str(MINTING_STATE), 'accept', plan['domain'], '--agent', plan['slug'],
                            '--path', rootp, '--state', str(ledger)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return 'accept refused rc=%d' % r.returncode
        got = json.loads(ledger.read_text()).get('domains', {}).get(plan['domain'], {})
        if got.get('dedicated_agent') != plan['slug'] or _real(got.get('agent_path') or '/nonexistent') != rootp:
            return 'accept read-back mismatch'
        self._hook(self.eng.set_accept, dict(acc, status='done'))
        return None

    # -- the sequence
    def run(self):
        flow = self._flow(('BUILDING',))
        try:
            plan = plan_from_flow(flow)
        except ValueError:
            return self._fail('plan', 'flow carries no valid pipeline request')
        root = plan['root']
        pre = _dir_state(root)
        if pre not in ('absent', 'empty'):
            return self._fail('admission', 'target root is not empty (occupied or not a directory)')
        write_receipt(self.ir, flow, plan, pre)                   # durable, before anything can be created
        deadline = flow.get('exec_deadline') or (time.time() + mf.EXEC_S)

        rc = self.pipeline(plan, True, deadline)
        if rc != 0:
            return self._fail('pipeline dry-run', 'rc=%d gate=%s' % (rc, self._gate()))
        self._flow(('BUILDING',))
        rc = self.pipeline(plan, False, deadline)
        if _dir_state(root) == 'nonempty':
            self._hook(self.eng.mark_artifacts, created=True)
        if rc != 0:
            return self._fail('pipeline', 'rc=%d gate=%s' % (rc, self._gate()))
        self._flow(('BUILDING',))

        reg = getattr(self.ad, 'registration', None)
        if reg is not None:
            res = reg.join(self._flow(('BUILDING',)))
            if res.get('kind') != 'ok':
                return self._fail('crew join', 'kind=%s' % res.get('kind'))
        self._flow(('BUILDING',))
        # Readiness judges a heartbeat against this durable pre-start epoch,
        # not merely against a successful launchctl return.
        worker = dict(self._flow(('BUILDING',)).get('worker') or {})
        worker['started_at'] = time.time()
        worker['start_epoch'] = worker['started_at']
        if not self._hook(self.eng.set_worker, worker):
            raise Aborted()
        if not self._hook(self.eng.mark_artifacts, started=True):    # intent BEFORE the units load
            raise Aborted()
        rc = self._run_step([self.mint_run, 'start', '--root', root], 'start')
        if rc != 0:
            return self._fail('start', 'rc=%d' % rc)

        if not self._hook(self.eng.advance, 'VERIFYING'):
            raise Aborted()
        ready = self.ad.readiness.check(self._flow(('VERIFYING',)), stage='verify')
        if ready.get('kind') != 'ready':
            return self._fail('readiness', 'kind=%s' % ready.get('kind'))

        if not self._hook(self.eng.advance, 'RECORDING'):
            raise Aborted()
        self._flow(('RECORDING',))
        err = self.accept(plan)
        if err:
            return self._fail('accept', err)
        ready = self.ad.readiness.check(self._flow(('RECORDING',)), stage='final')
        if ready.get('kind') != 'ready':
            return self._fail('final readiness', 'kind=%s' % ready.get('kind'))
        if not self._hook(self.eng.complete_done, mf.Ctx()):
            raise Aborted()
        return None


def _factory(spec, instance_root):
    if not spec:
        return mf.Adapters()
    mod, _, fn = spec.partition(':')
    return getattr(importlib.import_module(mod), fn or 'build')(instance_root)


def runner_main(args):
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    out = sys.stdout
    out.write('READY\n')
    out.flush()
    ready, _, _ = select.select([sys.stdin], [], [], float(os.environ.get('DOGANY_MINT_GO_WAIT_S') or GO_WAIT_S))
    line = sys.stdin.readline() if ready else ''
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)                                           # nothing more goes to the READY pipe
    if not line.strip():
        return 0                                                  # EOF or silence before GO: no mutation
    try:
        msg = json.loads(line)
    except ValueError:
        return 0
    if not msg.get('go') or not isinstance(msg.get('nonce'), str):
        return 0
    engine = mf.Engine(args.instance_root, _factory(args.adapters_factory, args.instance_root))
    runner = Runner(engine, engine.ad, args.mint_run, args.instance_root, msg.get('secret'), msg['nonce'], args.flow_id)
    try:
        runner.run()
    except Aborted:
        runner._log('flow left BUILDING under the worker: stopping without further mutation')
    except Exception as exc:                                      # last resort: a report, never silence
        runner._log('worker error: %s' % type(exc).__name__)
        try:
            runner._fail('worker error', type(exc).__name__)
        except Exception:
            pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='mint_flow_worker', add_help=False)
    sub = ap.add_subparsers(dest='cmd')
    r = sub.add_parser('run')
    r.add_argument('--instance-root', required=True)
    r.add_argument('--flow-id', required=True)
    r.add_argument('--mint-run', default=str(DEFAULT_MINT_RUN))
    r.add_argument('--adapters-factory')
    args = ap.parse_args(argv)
    if args.cmd == 'run':
        return runner_main(args)
    print('usage: mint_flow_worker.py run --instance-root R --flow-id F [--mint-run P] [--adapters-factory m:f]',
          file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
