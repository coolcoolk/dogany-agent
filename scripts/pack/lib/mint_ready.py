#!/usr/bin/env python3
"""DGN-1683 WP5: readiness + target-scoped crew registration for the mint flow.

Seam: mint_flow.Readiness.check(flow) (called by the WP4 worker at VERIFYING
and again before complete_done). Honest success = EVERY predicate below is
'pass'; unknown, skipped, deferred and errors all count as not ready
(spec v4 6, grill v4 fix 8). `pipeline --no-start` rc=0 is never evidence.

Predicates (each collected independently -- a dead bridge never hides a
channel/source defect):
  install_receipt   install_evidence.sh reader says VERIFIED, and the receipt's
                    pack / version / source equal what the flow selected
  viability         `mint_run.sh verdict --check viability --fresh --process`:
                    heartbeat at/after the pre-start epoch AND fresh now, and
                    a live launchd bridge pid whose command line carries
                    `--path <target root>` (the existing verdict, extended)
  config            `mint_run.sh verdict --check config --strict`: framework
                    stamps present; update channel equals the resolved
                    expectation (explicit / ordering declaration / release
                    default); "not judged" fails
  crew_member       crew.conf of the selected kit lists this root
  shared_db         target database/<kit>.db resolves to the crew's physical
                    DB and is readable; ordering DB, if present, agrees
  onboarding_wiring newborn settings register the SessionStart onboarding
                    check + Stop onboarding gate under THIS root, every hook
                    script under the root exists, no template placeholder is
                    left, and any pack-declared hooks are wired
  onboarding_state  identity file exists and still carries ONBOARDING_PENDING
                    (a newborn that has not met the owner yet is NORMAL; the
                    owner having said hello is never a precondition)
  accept_readback   (final stage only) ordering config/minting-state.json holds
                    dedicated_agent == slug and agent_path == target root

Flow fields read (WP4 fills them; anything missing -> that predicate 'unknown'):
  flow['target']    {'slug': str, 'root': abs path, 'domain': str (optional)}
  flow['selection'] {'pack_id': str, 'kit_id': str, 'version': str,
                     'source_tag': str (optional), 'source': str (optional),
                     'domain': str (optional), 'hooks': [{'event','script'}]
                     (optional; pack-declared hooks)}
  flow['worker']    {'started_at' | 'start_epoch': epoch recorded BEFORE start}
  flow['accept']    {'domain': str} once the accept intent exists

Never writes anything in the ordering instance or the target: every probe is
read-only. Nothing returned or logged carries a token or screen copy.

Registration (pipeline step 7): CrewRegistration.register(flow) runs
`update_seed.sh --target <root> --kit <kit>` -- the estate seed's own lease +
atomic upsert, scoped to this one root, no estate scan. Then (DGN-1877) every
other id the newborn mounts gets its update source registered with
`update_seed.sh --target <root> --pack <id>` (pack-sources.conf only). A
failure there is loud on stderr (the mint runner log) but never undoes the
mint: the kit join already succeeded and the next framework update's planner
self-heals an unregistered pack whose source clone exists.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK_DIR = HERE.parent
MINT_RUN = PACK_DIR / 'mint_run.sh'
UPDATE_SEED = PACK_DIR / 'update_seed.sh'
EVIDENCE = HERE / 'install_evidence.sh'

PASS, FAIL, UNKNOWN, DEFERRED = 'pass', 'fail', 'unknown', 'deferred'
ID_RE = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
PENDING_MARKER = '<!-- ONBOARDING_PENDING -->'
ONBOARDING_HOOKS = (('SessionStart', 'routines/onboarding-check.py'),
                    ('Stop', 'routines/onboarding-gate-stop.py'))
SCOPE_ENV_DROP = ('DOGANY_PLAN_INSTANCES', 'DOGANY_DISCOVER_INSTANCES_OVERRIDE')


def _dict(v):
    return v if isinstance(v, dict) else {}


def _str(v):
    return v if isinstance(v, str) and v else None


def _conf_get(path, key):
    try:
        for ln in Path(path).read_text(encoding='utf-8').splitlines():
            if ln.startswith(key + '='):
                return ln[len(key) + 1:].strip()
    except OSError:
        pass
    return None


def _real(p):
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return None


def _root_of(flow):
    root = _str(_dict(flow.get('target')).get('root'))
    return root.rstrip('/') if root and os.path.isabs(root) else None


class Readiness:
    """check(flow, stage='final') -> {'kind': 'ready'|'not_ready', ...}.

    stage 'final' (the default: a bare check(flow) can never over-claim) needs
    every predicate incl. accept read-back. stage 'verify' is for VERIFYING,
    before accept exists: accept_readback is reported 'deferred', the result is
    marked final=False and must NOT be used as DONE evidence.
    """

    def __init__(self, instance_root, run=None, env=None, shared_home=None,
                 verdict_timeout=None, clock=time.time, mint_run=None, evidence=None):
        self.instance_root = os.path.realpath(instance_root) if instance_root else None
        self.run = run or subprocess.run
        self.env = env
        self.shared_home = shared_home
        self.verdict_timeout = verdict_timeout
        self.clock = clock
        self.mint_run = str(mint_run or MINT_RUN)
        self.evidence = str(evidence or EVIDENCE)

    # -- helpers
    def _env(self):
        e = dict(os.environ if self.env is None else self.env)
        for k in SCOPE_ENV_DROP:
            e.pop(k, None)
        return e

    def _shared_home(self):
        return self.shared_home or self._env().get('DOGANY_SHARED_HOME') or \
            os.path.join(os.path.expanduser('~'), '.dogany')

    def _crew_dir(self):
        return self._env().get('DOGANY_CREW_DIR') or os.path.join(self._shared_home(), 'crews')

    def _exec(self, argv, timeout):
        try:
            p = self.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=self._env(), timeout=timeout, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, type(exc).__name__
        return p.returncode, (p.stdout or '') + (p.stderr or '')

    # -- predicates: each returns (verdict, short reason)
    def install_receipt(self, flow, root):
        sel = _dict(flow.get('selection'))
        pid, ver = _str(sel.get('pack_id')), _str(sel.get('version'))
        if not root or not pid or not ID_RE.match(pid) or not ver:
            return UNKNOWN, 'flow lacks target root / selection pack_id / version'
        ledger = None
        packs = _conf_get(os.path.join(root, '.instance.conf'), 'DOGANY_PACKS') or ''
        for item in packs.split(','):
            if item.startswith(pid + '@'):
                ledger = item[len(pid) + 1:]
        if ledger != ver:
            return FAIL, 'ledger carries %r, selection says %r' % (ledger, ver)
        rc, out = self._exec(['bash', self.evidence, 'read', '--root', root, '--pack', pid,
                              '--ledger-version', ledger], 60)
        lines = [ln for ln in (out or '').splitlines() if ln.startswith('EVIDENCE ')]
        if rc != 0 or len(lines) != 1 or 'verdict=VERIFIED' not in lines[0]:
            return FAIL, 'install evidence not VERIFIED (rc=%s)' % rc
        recv = {}
        try:
            for ln in Path(root, 'config/packs', pid + '.receipt').read_text(encoding='utf-8').splitlines():
                if '=' in ln and not ln.startswith('anchor='):
                    k, v = ln.split('=', 1)
                    recv[k] = v
        except OSError:
            return FAIL, 'receipt unreadable'
        if recv.get('pack') != pid or recv.get('version') != ver:
            return FAIL, 'receipt pack/version differ from the selection'
        want_src = _str(sel.get('source'))
        tag = _str(sel.get('source_tag'))
        got = recv.get('source')
        if want_src is None and tag is None:
            return UNKNOWN, 'selection carries no source identity to compare'
        if want_src is not None and got != want_src:
            return FAIL, 'receipt source differs from the selection'
        if tag is not None and got not in ('tag:' + tag, tag):
            return FAIL, 'receipt source differs from the selected source tag'
        return PASS, 'receipt VERIFIED, pack/version/source match'

    def _verdict(self, argv_extra):
        cmd = ['bash', self.mint_run, 'verdict'] + argv_extra
        wait = self.verdict_timeout
        rc, out = self._exec(cmd, (wait if wait is not None else 180) + 90)
        return rc, out

    def viability(self, flow, root):
        w = _dict(flow.get('worker'))
        since = w.get('started_at', w.get('start_epoch'))
        if not root or isinstance(since, bool) or not isinstance(since, (int, float)) or since <= 0:
            return UNKNOWN, 'flow lacks the pre-start epoch (worker.started_at)'
        argv = ['--root', root, '--check', 'viability', '--fresh', '--process',
                '--since', str(int(since))]
        if self.verdict_timeout is not None:
            argv += ['--timeout', str(int(self.verdict_timeout))]
        rc, out = self._verdict(argv)
        if rc == 0:
            return PASS, 'live bridge process of this root, heartbeat fresh'
        if rc == 30:
            why = [ln for ln in (out or '').splitlines() if 'process identity FAILED' in ln]
            return FAIL, (why[0].split('] ', 1)[-1] if why else 'no fresh heartbeat from a live process')
        return UNKNOWN, 'verdict could not run (rc=%s)' % rc

    def config(self, flow, root):
        if not root or not self.instance_root:
            return UNKNOWN, 'no target root / ordering instance'
        argv = ['--root', root, '--instance-root', self.instance_root, '--check', 'config', '--strict']
        rc, out = self._verdict(argv)
        if rc == 0:
            return PASS, 'framework stamps present, channel judged and equal'
        if rc == 31:
            return FAIL, 'birth-defect check failed (stamps / channel)'
        return UNKNOWN, 'verdict could not run (rc=%s)' % rc

    def _kit(self, flow):
        kit = _str(_dict(flow.get('selection')).get('kit_id'))
        return kit if kit and ID_RE.match(kit) else None

    def crew_member(self, flow, root):
        kit = self._kit(flow)
        if not root or not kit:
            return UNKNOWN, 'flow lacks target root / selection kit_id'
        conf = os.path.join(self._crew_dir(), kit, 'crew.conf')
        try:
            lines = Path(conf).read_text(encoding='utf-8').splitlines()
        except OSError:
            return FAIL, 'crew.conf of %s absent or unreadable' % kit
        me = _real(root)
        for ln in lines:
            if ln.startswith('member=') and (ln[7:] == root or _real(ln[7:]) == me):
                return PASS, 'crew %s lists this root' % kit
        return FAIL, 'crew %s does not list this root' % kit

    def shared_db(self, flow, root):
        kit = self._kit(flow)
        if not root or not kit or not self.instance_root:
            return UNKNOWN, 'flow lacks target root / kit_id / ordering instance'
        mine = os.path.join(root, 'database', kit + '.db')
        theirs = os.path.join(self.instance_root, 'database', kit + '.db')
        crew = os.path.join(self._crew_dir(), kit, kit + '.db')
        rm, rc = _real(mine), _real(crew)
        if not rm or not os.path.isfile(rm) or not os.access(rm, os.R_OK):
            return FAIL, 'target database/%s.db missing or unreadable' % kit
        if rm != rc:
            return FAIL, 'target database/%s.db is not the crew\'s physical file' % kit
        # The ordering instance may not have this kit; a dangling link is
        # still a declared DB path and must not bypass the consistency check.
        if os.path.lexists(theirs) and _real(theirs) != rc:
            return FAIL, 'ordering database/%s.db is not the crew\'s physical file' % kit
        return PASS, 'same physical %s.db, readable' % kit

    def _hook_commands(self, root):
        cmds = {}
        for name in ('settings.json', 'settings.local.json'):
            p = Path(root, '.claude', name)
            if not p.exists():
                continue
            try:
                doc = json.loads(p.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None, '%s is unreadable or not JSON' % name
            hooks = _dict(_dict(doc).get('hooks'))
            for ev, entries in hooks.items():
                for entry in entries if isinstance(entries, list) else []:
                    items = entry.get('hooks') if isinstance(entry, dict) and 'hooks' in entry else [entry]
                    for it in items or []:
                        if isinstance(it, dict) and isinstance(it.get('command'), str):
                            cmds.setdefault(ev, []).append(it['command'])
        return cmds, None

    def onboarding_wiring(self, flow, root):
        if not root:
            return UNKNOWN, 'flow lacks target root'
        cmds, err = self._hook_commands(root)
        if cmds is None:
            return FAIL, err
        wanted = list(ONBOARDING_HOOKS)
        for h in _dict(flow.get('selection')).get('hooks') or []:
            if isinstance(h, dict) and _str(h.get('event')) and _str(h.get('script')):
                wanted.append((h['event'], h['script']))
        for ev, script in wanted:
            path = os.path.join(root, script)
            if not any(path in c for c in cmds.get(ev, [])):
                return FAIL, '%s hook for %s is not registered under this root' % (ev, script)
            if not os.path.isfile(path):
                return FAIL, 'hook script %s is registered but absent' % script
        for ev, lst in cmds.items():
            for c in lst:
                if '__PROJECT_ROOT__' in c or '__HOME__' in c:
                    return FAIL, 'template placeholder left in a %s hook' % ev
                for m in re.finditer(re.escape(root) + r'/\S+', c):
                    if not os.path.isfile(m.group(0)):
                        return FAIL, 'a %s hook points at an absent file under the root' % ev
        return PASS, 'onboarding + declared hooks registered, scripts present'

    def onboarding_state(self, flow, root):
        if not root:
            return UNKNOWN, 'flow lacks target root'
        try:
            text = Path(root, 'identity', 'hot.custom.agent.md').read_text(encoding='utf-8')
        except OSError:
            return FAIL, 'identity file absent or unreadable'
        if PENDING_MARKER not in text:
            return FAIL, 'newborn identity lost its pending marker before the owner met it'
        return PASS, 'identity present, onboarding pending (normal)'

    def accept_readback(self, flow, root):
        acc = flow.get('accept')
        tgt, sel = _dict(flow.get('target')), _dict(flow.get('selection'))
        slug = _str(tgt.get('slug'))
        domain = _str(_dict(acc).get('domain')) or _str(tgt.get('domain')) or _str(sel.get('domain'))
        if not isinstance(acc, dict) or not root or not slug or not domain or not self.instance_root:
            return UNKNOWN, 'no accept intent / slug / domain / ordering instance in the flow'
        try:
            state = json.loads(Path(self.instance_root, 'config', 'minting-state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return FAIL, 'ordering minting-state.json unreadable'
        row = _dict(_dict(_dict(state).get('domains')).get(domain))
        if row.get('dedicated_agent') != slug:
            return FAIL, 'ledger dedicated_agent differs from the slug'
        path = _str(row.get('agent_path'))
        if not path or _real(path) != _real(root):
            return FAIL, 'ledger agent_path differs from the target root'
        return PASS, 'ledger read-back: slug and path match'

    # -- the seam
    def check(self, flow, stage='final'):
        if not self.instance_root:
            return {'kind': 'not_implemented', 'wp': 'WP5', 'seam': 'readiness.check'}
        if stage not in ('final', 'verify'):
            raise ValueError('stage')
        root = _root_of(flow)
        names = ['install_receipt', 'viability', 'config', 'crew_member', 'shared_db',
                 'onboarding_wiring', 'onboarding_state', 'accept_readback']
        preds, details = {}, {}
        for n in names:
            if n == 'accept_readback' and stage == 'verify':
                preds[n], details[n] = DEFERRED, 'accept has not happened yet (verify stage)'
                continue
            try:
                v, why = getattr(self, n)(flow, root)
            except Exception as exc:                     # a broken probe is never a pass
                v, why = UNKNOWN, 'probe error: %s' % type(exc).__name__
            preds[n], details[n] = v, why
        needed = [n for n in names if not (stage == 'verify' and n == 'accept_readback')]
        failed = [n for n in needed if preds[n] != PASS]
        return {'kind': 'not_ready' if failed else 'ready', 'stage': stage,
                'final': stage == 'final' and not failed,
                'predicates': preds, 'failed': failed, 'details': details}


class CrewRegistration:
    """Pipeline step 7. register(flow) ->
    {'kind': 'ok', 'result': 'added'|'present'} | {'kind': 'failed', 'rc': int}.
    'added' means THIS call created the membership (WP4 compensation may then
    remove exactly that row); 'present' means it was already there."""

    def __init__(self, run=None, env=None, update_seed=None, dry_run=False):
        self.run = run or subprocess.run
        self.env = env
        self.update_seed = str(update_seed or UPDATE_SEED)
        self.dry_run = dry_run
        self.pack_results = {}

    def _own_packs(self, flow, root, kit):
        # Every id the newborn mounts besides its kit; the selected pack when
        # the conf has no readable mount line.
        line = _conf_get(os.path.join(root, '.instance.conf'), 'DOGANY_PACKS') or ''
        ids = [it.split('@', 1)[0] for it in line.split(',') if '@' in it]
        if not ids:
            ids = [_str(_dict(flow.get('selection')).get('pack_id')) or '']
        out = []
        for pid in ids:
            if pid != kit and ID_RE.match(pid) and pid not in out:
                out.append(pid)
        return out

    def _register_pack(self, root, pid, env):
        argv = ['bash', self.update_seed, '--target', root, '--pack', pid]
        if self.dry_run:
            argv.append('--dry-run')
        why = None
        try:
            p = self.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         timeout=120, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            p, why = None, type(exc).__name__
        if p is not None:
            m = re.search(r'target-source pack=%s root=%s result=(added|present|preserved)'
                          % (re.escape(pid), re.escape(root)), p.stdout or '')
            if p.returncode == 0 and m:
                return m.group(1)
            why = 'rc=%s' % p.returncode
            tail = [ln for ln in ((p.stdout or '') + (p.stderr or '')).splitlines()
                    if 'FAIL' in ln or 'ERROR' in ln][-3:]
            if tail:
                why += ': ' + ' | '.join(tail)
        sys.stderr.write('[mint-register] FAIL: pack %s update source NOT registered for %s (%s); '
                         'the mint stands; /update shows it BLOCKED-UNREGISTERED until '
                         'update_seed.sh --target %s --pack %s is run (the next update plan '
                         'self-heals it when its pack-src clone exists; DGN-1877)\n' % (pid, root, why, root, pid))
        sys.stderr.flush()
        return 'failed'

    def register(self, flow):
        root = _root_of(flow)
        kit = _str(_dict(flow.get('selection')).get('kit_id'))
        if not root or not kit or not ID_RE.match(kit):
            return {'kind': 'failed', 'rc': None}
        env = dict(os.environ if self.env is None else self.env)
        for k in SCOPE_ENV_DROP:
            env.pop(k, None)
        argv = ['bash', self.update_seed, '--target', root, '--kit', kit]
        if self.dry_run:
            argv.append('--dry-run')
        try:
            p = self.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         timeout=120, text=True)
        except (OSError, subprocess.SubprocessError):
            return {'kind': 'failed', 'rc': None}
        m = re.search(r'target-registered kit=%s root=%s result=(added|present)'
                      % (re.escape(kit), re.escape(root)), p.stdout or '')
        if p.returncode != 0 or not m:
            return {'kind': 'failed', 'rc': p.returncode}
        try:
            self.pack_results = {pid: self._register_pack(root, pid, env)
                                 for pid in self._own_packs(flow, root, kit)}
        except Exception as exc:                 # never undoes the kit join
            sys.stderr.write('[mint-register] FAIL: pack source registration error for %s: %s '
                             '(the mint stands; DGN-1877)\n' % (root, type(exc).__name__))
        return {'kind': 'ok', 'result': m.group(1)}

    # Worker integration uses a common registration seam named join(). Keep
    # register() as the public WP5 API while binding that seam directly.
    def join(self, flow):
        return self.register(flow)


if __name__ == '__main__':
    sys.stderr.write('mint_ready is a library (mint_flow.Readiness seam)\n')
    sys.exit(2)
