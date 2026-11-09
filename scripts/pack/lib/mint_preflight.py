#!/usr/bin/env python3
"""mint_preflight.py -- DGN-1644: the pre-mint predicates as a call site.

The pre-mint gate used to be prose a model executed (mint-agent SKILL
predicates a/b/c): no function, no exit code, no proof it fired. This module
is the seat. It judges every predicate whose inputs were supplied and emits
the COMPLETE fired set -- never "the predicate that blocked" (first-wins
reproduces the 7/7 tally that named the wrong predicate in four cases).

Exit contract (one JSON object on stdout, human lines on stderr):
  {"schema": "mint.preflight/1",
   "fired": [<predicate id>, ...],        sorted, complete; [] on pass
   "measured": {<predicate id>: <measured value>, ...},   one per fired id
   "not_judged": {<predicate family>: <reason>, ...},     stated, never silent
   "state_only": true|false}
  rc 0 = fired set empty, rc 40 = fired set non-empty, rc 2 = usage.

Predicate ids (each distinction the corpus needs has its own id):
  bridge.heartbeat_missing         newborn has no .telegram_bot/poll_heartbeat
  bridge.heartbeat_unattributable  heartbeat present, but not at/after --since
                                   (since>0) or not inside STALE_S (since=0)
  conf.instance_conf_missing       newborn root exists, .instance.conf absent
  conf.fw_tag_absent               DOGANY_FW_TAG absent/empty
  conf.fw_repo_sha_absent          DOGANY_FW_REPO_SHA absent/empty
  conf.fw_stamps_unknown           both stamps are the literal 'unknown'
  channel.unmeasurable             no --channel and no ordering .instance.conf
                                   (ordering root absent or manifest missing)
  channel.invalid                  expected channel outside release|dev|staging|main
  channel.divergent                newborn channel != expected channel
  pack.manifest_unreadable         package dir resolved, pack-manifest.json
                                   missing or not JSON
  pack.manifest_id_absent          manifest has no `id` -- the tag namespace is
                                   never derived from repo/catalog/legacy names
  pack.version_absent              manifest has no `pack_version`
  pack.release_tag_absent          pack/<manifest id>/v<pack_version> (or the
                                   --source-tag ref) is not a tag in the pack
                                   work tree
  ledger.stale                     a dedicated_agent binding whose agent_path is
                                   absent (legacy key missing), relative or
                                   gone; an unreadable ledger is stale too
                                   (unverifiable = stale, never pass)
  ledger.occupied                  the pack's machine domain is bound to a
                                   dedicated agent whose agent_path resolves
  ledger.unjudged                  --require-ledger (the pipeline) and no
                                   --instance-root: the ledger family cannot be
                                   judged, and inside the pipeline "not
                                   judged" is never a pass (DGN-1644 gap 2)

--state-only skips the bridge family: configuration is read WITHOUT a
liveness precondition (a dead instance's conf is reachable, so no case needs
a forged heartbeat to reach the state it tests).

Read-only. Never writes, repairs, starts or deletes anything.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
MINTING_STATE = REPO / 'skills/dogany-mint-agent/scripts/minting_state.py'
CHANNELS = ('release', 'dev', 'staging', 'main')
RC_FIRED = 40


def conf_get(path, key):
    """Same reading as mint_run.sh conf_get: first KEY= line, whitespace
    stripped; '' when the key or file is absent."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith(key + '='):
                    return re.sub(r'\s', '', line[len(key) + 1:])
    except OSError:
        pass
    return ''


class Judge:
    def __init__(self, args):
        self.a = args
        self.fired = {}
        self.not_judged = {}

    def fire(self, pid, measured):
        self.fired[pid] = measured
        print('[preflight] FIRED %s -- %s' % (pid, measured), file=sys.stderr)

    def skip(self, family, reason):
        self.not_judged[family] = reason
        print('[preflight] NOT JUDGED %s -- %s' % (family, reason), file=sys.stderr)

    # ---- newborn instance (conf + bridge) ---------------------------------
    def newborn(self):
        root = self.a.root
        if not root:
            self.skip('conf', 'no --root')
            self.skip('bridge', 'no --root')
            return
        if not os.path.isdir(root):
            self.skip('conf', 'no instance at --root (pre-mint target)')
            self.skip('bridge', 'no instance at --root (pre-mint target)')
            return
        conf = os.path.join(root, '.instance.conf')
        if not os.path.isfile(conf):
            self.fire('conf.instance_conf_missing', conf)
        else:
            tag, sha = conf_get(conf, 'DOGANY_FW_TAG'), conf_get(conf, 'DOGANY_FW_REPO_SHA')
            if not tag:
                self.fire('conf.fw_tag_absent', 'DOGANY_FW_TAG=<absent> (DOGANY_FW_REPO_SHA=%s)' % (sha or '<absent>'))
            if not sha:
                self.fire('conf.fw_repo_sha_absent', 'DOGANY_FW_REPO_SHA=<absent> (DOGANY_FW_TAG=%s)' % (tag or '<absent>'))
            if tag == 'unknown' and sha == 'unknown':
                self.fire('conf.fw_stamps_unknown', 'DOGANY_FW_TAG=unknown DOGANY_FW_REPO_SHA=unknown')
        if self.a.state_only:
            self.skip('bridge', '--state-only (configuration read without a liveness precondition)')
            return
        hb = os.path.join(root, '.telegram_bot', 'poll_heartbeat')
        if not os.path.isfile(hb):
            self.fire('bridge.heartbeat_missing', hb)
            return
        mtime = int(os.stat(hb).st_mtime)
        now = int(time.time())
        if self.a.since > 0:
            if mtime < self.a.since:
                self.fire('bridge.heartbeat_unattributable',
                          'mtime=%d < since=%d (a previous life wrote it)' % (mtime, self.a.since))
        elif now - mtime >= self.a.stale_s:
            self.fire('bridge.heartbeat_unattributable',
                      'age=%ds >= stale-window=%ds' % (now - mtime, self.a.stale_s))

    # ---- update channel ----------------------------------------------------
    def channel(self):
        expected, basis = self.a.channel, '--channel'
        if not expected:
            iconf = os.path.join(self.a.instance_root, '.instance.conf') if self.a.instance_root else ''
            if not iconf or not os.path.isfile(iconf):
                self.fire('channel.unmeasurable',
                          'no --channel and no ordering manifest (%s)' % (iconf or '--instance-root not supplied'))
                return
            # Undeclared orderers resolve to release (DGN-1621 owner ruling):
            # a measurement, not an unmeasurable.
            expected = conf_get(iconf, 'DOGANY_UPDATE_CHANNEL') or 'release'
            basis = 'ordering manifest %s' % iconf
        if expected not in CHANNELS:
            self.fire('channel.invalid', '%r from %s (valid: %s)' % (expected, basis, '|'.join(CHANNELS)))
            return
        root = self.a.root
        conf = os.path.join(root, '.instance.conf') if root else ''
        if not conf or not os.path.isfile(conf):
            return  # pre-mint: nothing born to diverge yet
        got = conf_get(conf, 'DOGANY_UPDATE_CHANNEL')
        if got != expected:
            self.fire('channel.divergent', 'newborn %r != expected %r (%s)' % (got or '<absent>', expected, basis))

    # ---- pack release tag (predicate b) -------------------------------------
    def _row(self):
        try:
            with open(self.a.catalog, encoding='utf-8') as f:
                packs = json.load(f).get('packs', [])
        except (OSError, ValueError, AttributeError):
            return None
        return next((p for p in packs if isinstance(p, dict) and p.get('id') == self.a.pack), None)

    def _row_dir(self):
        coords = HERE / 'pack_coords.sh'
        try:
            r = subprocess.run(['bash', str(coords), 'row-dir', self.a.catalog, self.a.pack],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ''
        return r.stdout.strip() if r.returncode == 0 else ''

    def pack(self, row):
        if not self.a.pack:
            self.skip('pack', 'no --pack')
            return
        if row is None:
            self.skip('pack', 'pack %r has no row in %s (pack_install owns that refusal)' % (self.a.pack, self.a.catalog))
            return
        if self.a.untagged_local:
            self.skip('pack', '--untagged-local declared (%s)' % self.a.untagged_local)
            return
        pdir = self._row_dir()
        if not pdir or not os.path.isdir(pdir):
            self.skip('pack', 'package dir not on disk (%s): the source_url lane fetches at the tag' % (pdir or '<unresolved>'))
            return
        top = subprocess.run(['git', '-C', pdir, 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True)
        if top.returncode != 0 or not top.stdout.strip():
            self.skip('pack', 'package dir is not a git work tree (%s): same narrowing as pack_install DGN-1489' % pdir)
            return
        mpath = os.path.join(pdir, 'pack-manifest.json')
        try:
            with open(mpath, encoding='utf-8') as f:
                manifest = json.load(f)
            if not isinstance(manifest, dict):
                raise ValueError('not an object')
        except (OSError, ValueError) as e:
            self.fire('pack.manifest_unreadable', '%s (%s)' % (mpath, e.__class__.__name__))
            return
        mid, ver = manifest.get('id'), manifest.get('pack_version')
        if not isinstance(mid, str) or not mid:
            self.fire('pack.manifest_id_absent', '%s carries no id' % mpath)
        if not isinstance(ver, str) or not ver:
            self.fire('pack.version_absent', '%s carries no pack_version' % mpath)
        if 'pack.manifest_id_absent' in self.fired or 'pack.version_absent' in self.fired:
            return
        # The namespace is READ from the manifest, never derived (2026-09-23:
        # a hand-typed legacy name returned a plausible wrong answer).
        tag = self.a.source_tag or 'pack/%s/v%s' % (mid, ver)
        ok = subprocess.run(['git', '-C', pdir, 'rev-parse', '-q', '--verify', 'refs/tags/' + tag],
                            capture_output=True, text=True).returncode == 0
        if not ok:
            self.fire('pack.release_tag_absent', '%s not in %s (manifest id=%s pack_version=%s)'
                      % (tag, top.stdout.strip(), mid, ver))

    # ---- dedicated-agent ledger --------------------------------------------
    def ledger(self, row):
        if not self.a.instance_root:
            if self.a.require_ledger:
                self.fire('ledger.unjudged', '--instance-root not supplied and no ordering instance derivable '
                          '(the ledger is instance-resident; not judged is never a pass here)')
                return
            self.skip('ledger', '--instance-root not supplied (the ledger is instance-resident)')
            return
        path = os.path.join(self.a.instance_root, 'config', 'minting-state.json')
        try:
            spec = importlib.util.spec_from_file_location('minting_state', MINTING_STATE)
            sys.path.insert(0, str(MINTING_STATE.parent))
            ms = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ms)
            state = ms.pol.load_state(path)
            domains = state.get('domains', {})
            stale = ms.stale_bindings(state)
        except Exception as e:  # unverifiable = stale, never pass
            self.fire('ledger.stale', '%s unreadable (%s)' % (path, e.__class__.__name__))
            return
        if stale:
            self.fire('ledger.stale', 'binding(s) without an existing absolute agent_path: %s' % ', '.join(sorted(stale)))
        if self.a.custom:
            self.skip('ledger.occupied', 'custom staff agent: its ledger key is the flow selection, not a catalog domain')
            return
        if not self.a.pack or row is None:
            self.skip('ledger.occupied', 'no catalog row for --pack (no machine domain to look up)')
            return
        domain = next((d for d in row.get('domains', []) or []
                       if isinstance(d, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', d)), None)
        if domain is None:
            self.skip('ledger.occupied', 'pack %r has no machine domain' % self.a.pack)
            return
        bound = domains.get(domain) if isinstance(domains, dict) else None
        if isinstance(bound, dict) and bound.get('dedicated_agent') and domain not in stale:
            self.fire('ledger.occupied', 'domain %r bound to %r at %s'
                      % (domain, bound.get('dedicated_agent'), bound.get('agent_path')))

    def run(self):
        row = self._row() if self.a.pack and self.a.catalog else None
        if self.a.pack and not self.a.catalog:
            self.skip('pack', 'no --catalog')
        else:
            self.pack(row)
        self.ledger(row)
        self.channel()
        self.newborn()
        out = {'schema': 'mint.preflight/1',
               'fired': sorted(self.fired),
               'measured': {k: self.fired[k] for k in sorted(self.fired)},
               'not_judged': self.not_judged,
               'state_only': bool(self.a.state_only)}
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return RC_FIRED if self.fired else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='DGN-1644 mint preflight predicates')
    ap.add_argument('--root', default='')
    ap.add_argument('--instance-root', default='')
    ap.add_argument('--channel', default='')
    ap.add_argument('--pack', default='')
    ap.add_argument('--catalog', default='')
    ap.add_argument('--source-tag', default='')
    ap.add_argument('--untagged-local', default='')
    ap.add_argument('--custom', action='store_true')
    ap.add_argument('--since', type=int, default=0)
    ap.add_argument('--stale-s', type=int, default=180)
    ap.add_argument('--state-only', action='store_true')
    ap.add_argument('--require-ledger', action='store_true')
    a = ap.parse_args(argv)
    return Judge(a).run()


if __name__ == '__main__':
    sys.exit(main())
