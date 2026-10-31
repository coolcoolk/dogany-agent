#!/usr/bin/env python3
"""DGN-1683 WP3: screen assets + strict sender for the mint verb.

Implements the `Screens` seam of mint_flow.Adapters:

  options(screen_id, flow) -> [{"label", "route"}]   buttons of that screen
  send(screen_id, flow, ctx) -> {"kind": "ok", "message_id": N}
                              | {"kind": "failed"} | {"kind": "uncertain"}
                              | {"kind": "none"}       (screen has no fixed copy)
                              | not_implemented(...)   (catalog unreadable)

Copy lives as bytes in mint_screens/*.txt (no trailing newline, placeholders
are <username> <first_name> <도메인> <표시이름>). The assets are the source of
the pre-approved screens, and the catalog screen is composed by
catalog_brief.compose() itself (one source); tests/dgn1683_mint_screens_selftest.py
pin both. Nothing here rewrites copy.

Delivery goes ONLY through routines/push.sh --strict (grill fix 7): rc 0 plus a
parsed receipt (message_id, chat) is delivery; rc 6, a timeout or an unreadable
receipt is "uncertain"; every other rc, a dropped line, a refused button set is
"failed". Buttons are inline `opt:N. label` (grill fix 5): the bridge's existing
callback path turns a tap into an ordinary "N. label" owner message.

Owner-copy mapping (dogany-dev wp-common, 2026-09-24 11:00):
  N2 token format -> token_format   N3 token not verified -> token_invalid
  N4 token reused -> token_reused   N8/N9 expired on return -> expired_on_return
  N10/N11 problem -> problem        handoff/bot_confirm/token_prompt: SKILL copy
  N5/N6/N7/N12: NO fixed copy (log only / the agent talks) -> kind "none"
  (DGN-1702: new flows never render N6/N7 -- no-fit and early occupancy end
  without a screen and the agent speaks on its own emission code.)
Handover (info only, no buttons): the kit's handover-brief body is sent as its
own message in front of the token prompt of an official pack.
Catalog (DGN-1702 round 3, owner rehearsal 2026-09-26 A1-A4): ONE message --
the agent's lead as the engine stored it (judged once, at record time; never
re-judged here, never omitted: no stored lead -> Unrenderable), the card as
compose() makes it (unchanged copy; a single pack carries no "1." in its title
or button), and, for a single pack, the owner-approved free-text invitation
(catalog_invite.txt). Every part of every screen is required.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSET_DIR = HERE / 'mint_screens'
REPO = HERE.parents[2]
MINT_RUN = HERE.parent / 'mint_run.sh'
PACK_COMMIT_GATE = HERE / 'pack_commit_gate.sh'
PACK_COORDS = HERE / 'pack_coords.sh'
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import catalog_brief as cb                                   # noqa: E402

PILOT_QUERY = '헬스'          # a legacy flow with no owner words keeps the pilot's fixed catalog
AGAIN_LABEL = '처음부터 만들기'
PUSH_TIMEOUT_S = 90
HANDOVER_TIMEOUT_S = 30
REMOTE_TAG_TIMEOUT_S = 30
LEAD_MAX = 200
CALLBACK_MAX = 64             # Telegram callback_data limit, bytes

ASSETS = {
    'token_prompt': 'token_prompt.txt',
    'bot_confirm': 'bot_confirm.txt',
    'handoff': 'handoff.txt',
    'N2': 'token_format.txt',
    'N3': 'token_invalid.txt',
    'N4': 'token_reused.txt',
    'N8': 'expired_on_return.txt',
    'N9': 'expired_on_return.txt',
    'N5': 'problem.txt',
    'N10': 'problem.txt',
    'N11': 'problem.txt',
}
FOLLOW_UP = {'bot_confirm': 'progress.txt'}     # second message of the same screen
# DGN-1702 round 3 (owner rehearsal 2026-09-26 A1): the free-text invitation
# that closes a single-pack confirm catalog, owner-approved sample verbatim.
CATALOG_INVITE = 'catalog_invite.txt'
NO_COPY = ('N6', 'N7', 'N12')
AGAIN_SCREENS = ('N8', 'N9')

_TOKEN_RE = re.compile(r'\d{3,}:[A-Za-z0-9_-]{20,}')
_USER_RE = re.compile(r'^[A-Za-z0-9_]{3,64}$')
_PLACEHOLDER_RE = re.compile(r'<(?:username|first_name|도메인|표시이름)>')


class Unrenderable(Exception):
    """The screen cannot be produced faithfully (missing fact / bad asset)."""


def pack_row_dir(catalog_path, row):
    """A catalog row's pack directory by the one read-side rule
    (pack_coords.sh pack_row_dir, DGN-1818): the sibling checkout named by
    package_dir, else -- on a public install with no sibling -- the
    pack-src clone the kit install made.  Never package_dir alone."""
    sibling = Path(catalog_path).parent / row['package_dir']
    if (sibling / 'pack-manifest.json').is_file() or not row.get('source_url'):
        return sibling
    try:
        out = subprocess.run(['bash', str(PACK_COORDS), 'row-dir', str(catalog_path), row['id']],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return sibling
    return Path(out) if out else sibling


def pack_source_tag(pack_id, version):
    """Ask the installer's shared tag-coordinate rule for a sealed source tag.

    pack_install.sh sources pack_commit_gate.sh and uses pack_d1_tag_ref for
    its automatic source-tag gate.  Keep the mint selection on that exact
    rule instead of maintaining a second spelling of the tag namespace here.
    """
    try:
        proc = subprocess.run(
            ['bash', '-c', '. "$1"; pack_d1_tag_ref "$2" "$3"',
             'mint-source-tag', str(PACK_COMMIT_GATE), pack_id, version],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unrenderable('cannot derive sealed source tag: %s' % type(exc).__name__)
    tag = proc.stdout.strip()
    if proc.returncode != 0 or not tag:
        raise Unrenderable('cannot derive sealed source tag from installer rule')
    return tag


def pack_remote_tag(catalog_path, pack_id):
    """The newest stable tag of a row's source_url remote, read without a
    clone (pack_coords.sh remote-tag, DGN-1844): the tag pack_install.sh's
    pack_source_fetch checks out when no tag is named. None when it cannot be
    read."""
    try:
        proc = subprocess.run(['bash', str(PACK_COORDS), 'remote-tag', str(catalog_path), pack_id],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=REMOTE_TAG_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    tag = proc.stdout.strip()
    return tag if proc.returncode == 0 and tag else None


def pack_build_version(catalog_path, row):
    """(version, source_tag) the mint build will install, by the same owner of
    truth pack_install.sh uses: the pack's own manifest where a pack directory
    is reachable (a dev sibling checkout, or a pack-src clone that already
    exists), else -- on a public install before pack_install has cloned the
    pack (DGN-1844) -- the newest stable tag of the row's source_url, which is
    exactly what pack_install will fetch. Raises Unrenderable."""
    manifest = pack_row_dir(catalog_path, row) / 'pack-manifest.json'
    if manifest.is_file():
        try:
            declared = json.loads(manifest.read_text(encoding='utf-8'))
            if not isinstance(declared, dict):
                raise ValueError('manifest is not an object')
        except (OSError, ValueError):
            raise Unrenderable('pack manifest is unreadable')
        # pack_version is the pack contract.  `version` is retained only
        # for a manifest that predates that field, never as a preference.
        version = (declared.get('pack_version') if 'pack_version' in declared
                   else declared.get('version'))
        if not isinstance(version, str) or not version.strip():
            raise Unrenderable('pack manifest has no pack_version (or legacy version)')
        version = version.strip()
        return version, pack_source_tag(row['id'], version)
    if not row.get('source_url'):
        raise Unrenderable('pack manifest is unreadable')
    tag = pack_remote_tag(catalog_path, row['id'])
    if tag is None:
        raise Unrenderable('pack source has no reachable stable tag')
    # The version is what the installer's own tag rule wraps around it: spell
    # the rule once with a marker and read the tag back through it.
    head, _, tail = pack_source_tag(row['id'], 'X0X').partition('X0X')
    version = tag[len(head):len(tag) - len(tail)] if tag.startswith(head) and tag.endswith(tail) else ''
    if not version or pack_source_tag(row['id'], version) != tag:
        raise Unrenderable('pack source tag does not follow the installer rule')
    return version, tag


def load_asset(name):
    """Exact bytes of an asset as text (no strip, no newline handling)."""
    return (ASSET_DIR / name).read_bytes().decode('utf-8')


def conf_get(path, key):
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            if line.startswith(key + '='):
                return line.split('=', 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return ''


def fill(template, values):
    """Placeholder substitution; refuses to leave any placeholder unfilled and
    never lets a token-shaped value onto a screen."""
    out = template
    for key, val in values.items():
        if val is None:
            continue
        if _TOKEN_RE.search(val):
            raise Unrenderable('token-shaped value')
        out = out.replace('<%s>' % key, val)
    if _PLACEHOLDER_RE.search(out):
        raise Unrenderable('unfilled placeholder')
    return out


def clean_name(value):
    v = ' '.join(str(value or '').split())
    v = ''.join(c for c in v if c.isprintable() and c not in '<>')
    return v[:64].strip()


def button_rows(options):
    """Inline keyboard rows, one button per row, in the bridge's own shape
    (options.build_option_keyboard): text "N. label", callback_data
    "opt:N. label" with the number-only "opt:N" fallback past 64 bytes.
    A single option carries no number (DGN-1702 round 3, rehearsal A3): its
    text is the bare label, which match_option takes as the exact label."""
    rows = []
    numbered = len(options) > 1
    for i, o in enumerate(options, 1):
        text = ('%d. %s' % (i, o['label'])) if numbered else o['label']
        cbd = 'opt:' + text
        if len(cbd.encode('utf-8')) > CALLBACK_MAX:
            cbd = 'opt:%d' % i
        rows.append([{'text': text, 'callback_data': cbd}])
    return rows


class MintScreens:
    def __init__(self, instance_root, catalog=None, push_sh=None, mint_run=None,
                 runner=subprocess.run, push_timeout=PUSH_TIMEOUT_S,
                 handover_timeout=HANDOVER_TIMEOUT_S, log=None,
                 agents_dir=None, handoff_root=None, crew_dir=None):
        self.root = Path(instance_root)
        # DGN-1705 occupancy probes; None -> the same env/defaults the
        # pipeline, retire and cleanup use, resolved at call time.
        self._agents_dir, self._handoff_root, self._crew_dir = agents_dir, handoff_root, crew_dir
        self._catalog = Path(catalog) if catalog else None
        self.push_sh = Path(push_sh) if push_sh else self.root / 'routines' / 'push.sh'
        self.mint_run = Path(mint_run) if mint_run else MINT_RUN
        self.run = runner
        self.push_timeout, self.handover_timeout = push_timeout, handover_timeout
        self.log = log or (lambda *a: None)

    # ------------------------------------------------------------ catalog
    def catalog_path(self):
        if self._catalog:
            return self._catalog
        canon = conf_get(self.root / '.instance.conf', 'DOGANY_REPO_ROOT')
        if canon and (Path(canon) / 'packs/catalog.json').is_file():
            return Path(canon) / 'packs/catalog.json'
        return REPO / 'packs/catalog.json'

    def user_label(self):
        return conf_get(self.root / '.instance.conf', 'DOGANY_USER_LABEL') or '님'

    def _eligible(self):
        try:
            return cb.load_eligible(str(self.catalog_path()))
        except SystemExit:                  # catalog_brief._fail() exits
            return None

    @staticmethod
    def owner_words(flow):
        """Everything the owner said in this flow: the request and every
        consult answer (DGN-1702). Never the agent's questions."""
        parts = [(flow.get('request') or '').strip()]
        parts += [(x.get('answer') or '').strip() for x in (flow.get('context') or [])
                  if isinstance(x, dict)]
        return ' '.join(p for p in parts if p)

    @classmethod
    def query_of(cls, flow):
        return cls.owner_words(flow) or PILOT_QUERY

    def matched(self, flow):
        """The packs the catalog screen shows. The agent's fit (flow.fit,
        DGN-1702 fix round; round 3: one pack, or the candidates when several
        match) names what is shown, in catalog order; a browse shows
        every eligible pack; only a legacy flow (neither) still matches the
        owner's words by substring."""
        rows = self._eligible()
        if rows is None:
            return None
        fit = self.fit_ids(flow)
        if fit:
            return [r for r in rows if r['id'] in fit]
        if flow.get('browse') is True:
            return list(rows)
        q = self.query_of(flow)
        return [r for r in rows if cb.match_query(r, q)]

    @staticmethod
    def fit_ids(flow):
        """The pack ids of the agent's --fit (one, or several candidates
        joined by commas, DGN-1702 round 3); [] when there is none."""
        fit = (flow or {}).get('fit')
        if not isinstance(fit, str):
            return []
        return [x for x in fit.split(',') if x]

    def fit_packs(self, flow):
        """Engine seam (DGN-1702 fix round): what the agent judges --fit
        against -- id, name_ko, tagline_ko of every eligible pack, verbatim."""
        rows = self._eligible()
        if rows is None:
            return {'kind': 'not_implemented', 'wp': 'WP3', 'seam': 'screens.fit_packs'}
        return [{'id': r['id'], 'name_ko': self._name(r), 'tagline_ko': r.get('tagline_ko') or ''}
                for r in rows]

    @staticmethod
    def _name(row):
        return row.get('name_ko') or row.get('name_en') or row['id']

    def chosen_pack(self, flow):
        """The official pack row the owner picked at the catalog (None: no
        pick). Looked up among all eligible rows: a browse pick is followed by
        consult answers that change query_of(), never the picked row."""
        label = ((flow.get('choice') or {}).get('label') or '').strip()
        for r in self._eligible() or []:
            if self._name(r) == label:
                return r
        return None

    # ------------------------------------------------------- occupancy
    def _bound_agent(self, pack):
        """DGN-1702: the dedicated agent the ledger already binds to the
        pack's machine domain (the key the worker's accept checks), or None.
        Read-only; an unreadable ledger answers None -- the DGN-1700 ledger
        gate and the worker's accept remain the authority."""
        try:
            domain = self._machine_domain(pack)
            state = json.loads((self.root / 'config/minting-state.json').read_text(encoding='utf-8'))
            row = (state.get('domains') or {}).get(domain)
        except (Unrenderable, OSError, ValueError, AttributeError):
            return None
        agent = row.get('dedicated_agent') if isinstance(row, dict) else None
        return agent if isinstance(agent, str) and agent else None

    def occupant(self, flow):
        pack = self.chosen_pack(flow)
        return self._bound_agent(pack) if pack is not None else None

    # ------------------------------------------------------------ slug
    def agents_dir(self):
        return Path(self._agents_dir) if self._agents_dir else Path.home() / '.dogany' / 'agents'

    def _occupied(self, slug):
        """DGN-1705: a live instance already holds `slug` -- its instance
        root, its handoff mailbox or a crew membership. Retire and teardown
        move all three into an archive, so a retired agent never counts."""
        home = Path.home()
        root = self.agents_dir() / slug
        handoff = Path(self._handoff_root or os.environ.get('DOGANY_HANDOFF_ROOT')
                       or home / '.dogany/handoff') / slug
        if os.path.lexists(root) or os.path.lexists(handoff):
            return True
        shared = os.environ.get('SHARED_HOME') or os.environ.get('DOGANY_SHARED_HOME') or home / '.dogany'
        crews = Path(self._crew_dir or os.environ.get('CREW_DIR') or os.environ.get('DOGANY_CREW_DIR')
                     or Path(shared) / 'crews')
        for registry in sorted(crews.glob('*/crew.conf')):
            try:
                lines = registry.read_text(encoding='utf-8').splitlines()
            except OSError:
                return True                     # unreadable registry: fail closed
            for line in lines:
                if line.startswith('member='):
                    member = os.path.normpath(line[7:].strip())
                    if member == os.path.normpath(str(root)) or os.path.basename(member) == slug:
                        return True
        return False

    def _free_slug(self, domain):
        """The pack's machine domain is the slug (phase-0 value), never the
        bot username. A live holder gets -2, -3, ... appended."""
        base = domain.replace('_', '-')[:28].rstrip('-')
        if not re.fullmatch(r'[a-z][a-z0-9-]{1,30}', base):
            raise Unrenderable('pack domain cannot form a mint slug')
        for n in range(1, 100):
            slug = base if n == 1 else '%s-%d' % (base, n)
            if not self._occupied(slug):
                return slug
        raise Unrenderable('no free mint slug for the pack domain')

    @staticmethod
    def _machine_domain(pack):
        # The owner-facing domain may be Korean; the pipeline and ledger use a
        # stable ASCII key. Keep those two meanings separate.
        domain = next((d for d in pack.get('domains', [])
                       if isinstance(d, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', d)), None)
        if domain is None:
            raise Unrenderable('pack has no machine domain')
        return domain

    def lock_selection(self, flow):
        """DGN-1705: pin the slug at pack selection, before the token step,
        so which bot the owner later brings can never move it. The lock
        rides in flow.selection (flow.target is rewritten by the N4 screen);
        configure_accepted_flow completes the selection around it."""
        pack = self.chosen_pack(flow)
        if pack is None:
            raise Unrenderable('no chosen official pack')
        domain = self._machine_domain(pack)
        flow['selection'] = {'pack_id': pack['id'], 'domain': domain,
                             'slug': self._free_slug(domain)}

    def configure_accepted_flow(self, flow, bot):
        """Fill the durable, non-secret build plan after getMe accepted a bot.

        This is deliberately owned by the catalog/screen adapter: it is the
        only component that knows which displayed catalog row the owner chose.
        The token adapter supplies identity facts only; the username is kept
        as a bot fact in flow.target and never feeds the slug (DGN-1705).
        """
        pack = self.chosen_pack(flow)
        if pack is None:
            raise Unrenderable('no chosen official pack')
        username = self._username((bot or {}).get('username'))
        domain = self._machine_domain(pack)
        locked = flow.get('selection') if isinstance(flow.get('selection'), dict) else {}
        if locked.get('pack_id') == pack['id'] and isinstance(locked.get('slug'), str):
            slug = locked['slug']
        else:                                   # a flow saved before the lock existed
            slug = self._free_slug(domain)
        root = str(self.agents_dir() / slug)
        package_dir = pack.get('package_dir')
        if not isinstance(package_dir, str) or not package_dir:
            raise Unrenderable('pack has no package directory')
        version, source_tag = pack_build_version(self.catalog_path(), pack)
        flow['target'] = {'slug': slug, 'root': root, 'domain': domain,
                          'username': username,
                          'first_name': clean_name((bot or {}).get('display_name'))}
        flow['selection'] = {'pack_id': pack['id'], 'kit_id': 'kit_life', 'slug': slug,
                             'version': str(version), 'domain': domain,
                             'source_tag': source_tag,
                             'catalog': str(self.catalog_path()),
                             'role': pack.get('role_prose_ko') or pack.get('role_prose_en') or pack['id']}

    def catalog_text(self, flow):
        matched = self.matched(flow)
        if matched is None:
            raise Unrenderable('catalog unavailable')
        body = cb.compose(matched, numbered=len(matched) > 1)
        if cb._judge(body):
            raise Unrenderable('catalog copy violates an owner ruling')
        # The canonical pipeline's catalog-brief gate requires the same
        # instance-resident stamp as mint_run.sh catalog-brief. Input cannot
        # advance until this screen has a delivery receipt.
        cb.write_stamp(str(self.root / 'config/mint-catalog-brief.json'),
                       self.query_of(flow), [row['id'] for row in matched])
        lines = body.split('\n')
        # compose() ends with the [[OPTIONS: ...]] marker: the marker is bridge
        # grammar for model output; a machine screen carries real buttons, so
        # the marker (and the blank line before it) is the only thing removed.
        assert lines[-1].startswith('[[OPTIONS:') and lines[-2] == ''
        return '\n'.join(lines[:-2])

    # ------------------------------------------------------------ options
    def options(self, screen_id, flow):
        if screen_id == 'catalog':
            matched = self.matched(flow)
            if matched is None:
                return {'kind': 'not_implemented', 'wp': 'WP3', 'seam': 'screens.options'}
            # An official pack goes straight to the token prompt (the handover
            # line rides in front of it, no confirm buttons: owner copy
            # 2026-09-24); a pack whose domain already has a dedicated agent
            # ends there (END_N7, DGN-1702). An empty list is the engine's
            # no-fit signal: this screen is never rendered without options.
            return [{'label': self._name(r),
                     'route': 'END_N7' if self._bound_agent(r) else 'WAIT_TOKEN'} for r in matched]
        if screen_id in AGAIN_SCREENS:
            return [{'label': AGAIN_LABEL, 'route': 'OPEN'}]
        return []

    # -------------------------------------------------------------- copy
    def _target(self, flow):
        t = flow.get('target') or {}
        return t if isinstance(t, dict) else {}

    def _domain(self, flow):
        pack = self.chosen_pack(flow)
        if pack is None:
            raise Unrenderable('no chosen pack')
        # domain_term only ever returns a word the pack declares; the
        # request just picks which one (DGN-1702).
        word = clean_name(cb.domain_term([pack], flow.get('request') or ''))
        if not word:
            raise Unrenderable('chosen pack declares no domain')
        return word

    def _username(self, value):
        v = str(value or '').lstrip('@')
        if not _USER_RE.match(v):
            raise Unrenderable('username')
        return v

    def _values(self, screen_id, flow):
        t = self._target(flow)
        if screen_id == 'bot_confirm':
            return {'username': self._username(t.get('username')),
                    'first_name': clean_name(t.get('first_name')) or None,
                    '도메인': self._domain(flow)}
        if screen_id == 'handoff':
            return {'username': self._username(t.get('username')), '도메인': self._domain(flow)}
        if screen_id == 'N4':
            return {'username': self._username(t.get('existing_username')),
                    '표시이름': clean_name(t.get('existing_display_name')) or None}
        return {}

    def handover_text(self, flow):
        """Kit handover-brief body for the chosen official pack, or None."""
        pack = self.chosen_pack(flow)
        if pack is None:
            return None
        cmd = [str(self.mint_run), 'handover-brief', '--pack', pack['id'],
               '--instance-root', str(self.root), '--catalog', str(self.catalog_path())]
        try:
            r = self.run(cmd, capture_output=True, text=True, timeout=self.handover_timeout)
        except (OSError, subprocess.SubprocessError):
            self.log('handover: brief verb did not run')
            return None
        if r.returncode != 0:
            return None
        body = (r.stdout or '').rstrip('\n')
        return body if body.strip() else None

    def unsaid_pack_terms(self, text, flow):
        """DGN-1702 provenance test: the `domains` words of eligible packs
        that `text` contains but the owner never said in this flow (request or
        any consult answer). The 09:19 incident lead carried a purpose taken
        from the pack description; this is what catches it."""
        said = cb._norm(self.owner_words(flow or {}))
        said_tight = said.replace(' ', '')
        t = cb._norm(text or '')
        rows = self._eligible() or []
        # DGN-1702 round 3: a confirm lead checks the agent's reading of the
        # request by naming the pack it judged (--fit), e.g. "헬스 트레이너
        # 에이전트 말씀이신가요?". The words of that pack's own NAME are the
        # confirmation, not a purpose put in the owner's mouth; its domains
        # and tagline stay under the check.
        named = ' '.join(cb._norm(self._name(r)) for r in rows if r['id'] in self.fit_ids(flow))
        out = []
        for row in rows:
            for d in row.get('domains', []):
                if not isinstance(d, str):
                    continue
                k = cb._norm(d)
                if not k or not cb._key_long_enough(k) or k not in t:
                    continue
                if k in said or k.replace(' ', '') in said_tight or k in named:
                    continue
                if k not in out:
                    out.append(k)
        return out

    def lead_problem(self, lead, flow=None):
        """DGN-1674 mechanical judge of the agent's lead (engine seam): None if
        it may go out, else 'missing' | 'rejected'. One line, capped, no
        token, no procedure/instruction/verdict wording (catalog_brief judge).
        With the flow (DGN-1702): no pack domain word the owner did not say."""
        lead = (lead or '').strip()
        if not lead:
            return 'missing'
        if '\n' in lead or len(lead) > LEAD_MAX or _TOKEN_RE.search(lead) or cb._judge(lead):
            return 'rejected'
        if lead.startswith('>') or '[[' in lead:
            return 'rejected'       # bridge quote/marker grammar: the lead shares the card's message
        if flow is not None and self.unsaid_pack_terms(lead, flow):
            return 'rejected'
        return None

    def ask_flags(self, asked, flow):
        """DGN-1702: the same provenance test on the agent's consult question
        (already spoken, so it flags and never blocks)."""
        return ['pack_term_unsaid'] if asked and self.unsaid_pack_terms(asked, flow) else []

    @staticmethod
    def stored_lead(flow):
        """The lead the engine judged and stored with this catalog screen
        (DGN-1702 round 3, A2). Judged ONCE, at record time (mint_flow
        Engine._lead); the render never re-judges and never falls back to
        the call's own --lead. The live 2026-09-26 09:38 catalog went out
        without its accepted lead because the lead was a separate, optional
        message whose loss was silent: no stored lead -> Unrenderable."""
        lead = ((flow or {}).get('screen') or {}).get('lead')
        if not isinstance(lead, str) or not lead.strip():
            raise Unrenderable('catalog lead missing')
        return lead.strip()

    def catalog_message(self, flow):
        """The catalog screen as ONE message (owner rehearsal A1/A2): the
        agent's stored lead, the card, and -- for a single pack -- the
        owner-approved free-text invitation. One strict send: the card can
        never reach the owner without its lead."""
        matched = self.matched(flow)
        text = self.stored_lead(flow) + '\n\n' + self.catalog_text(flow)
        if matched is not None and len(matched) == 1:
            text += '\n\n' + load_asset(CATALOG_INVITE)
        return text

    def parts(self, screen_id, flow, ctx):
        """Ordered [(text, buttons)] the screen is made of. Every part is
        required: no part of a screen is ever dropped quietly (DGN-1702 A2)."""
        if screen_id in NO_COPY:
            return None
        if screen_id == 'catalog':
            return [(self.catalog_message(flow), button_rows(self.options('catalog', flow)))]
        if screen_id not in ASSETS:
            raise Unrenderable('unknown screen')
        out = []
        if screen_id == 'token_prompt':
            hb = self.handover_text(flow)
            if hb:
                out.append((hb, []))
        out.append((fill(load_asset(ASSETS[screen_id]), self._values(screen_id, flow)),
                    button_rows(self.options(screen_id, flow))))
        if screen_id in FOLLOW_UP:
            out.append((load_asset(FOLLOW_UP[screen_id]), []))
        return out

    # -------------------------------------------------------------- send
    def log_push_failure(self, step, rc, stderr):
        """Match onboarding diagnostics without risking the delivery result."""
        try:
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', errors='replace')
            line = 'mint-screen: step=%s push rc=%s stderr=%s\n' % (
                step, rc, json.dumps((stderr or '')[-300:], ensure_ascii=True))
            log_dir = self.root / '.telegram_bot' / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / 'onboarding-gate.log').open('a', encoding='utf-8') as fh:
                fh.write(line)
        except Exception:
            pass

    def push(self, text, buttons, flow, step=None):
        """One strict send. -> {"kind": "ok", "message_id": N} | failed | uncertain."""
        owner = flow.get('owner') or {}
        chat, thread = owner.get('chat_id'), owner.get('thread_id')
        fd, expect = tempfile.mkstemp(prefix='mint-screen-')
        try:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(text.encode('utf-8'))
            cmd = ['bash', str(self.push_sh), '--strict', '--audience', 'owner',
                   '--text', text, '--expect-file', expect]
            if chat is not None:
                cmd += ['--chat', str(chat)]
            if thread is not None:
                cmd += ['--thread', str(thread)]
            if buttons:
                cmd += ['--buttons-json', json.dumps(buttons, ensure_ascii=False)]
            try:
                r = self.run(cmd, capture_output=True, text=True, timeout=self.push_timeout)
            except subprocess.TimeoutExpired as exc:
                self.log_push_failure(step, 'timeout', exc.stderr)
                return {'kind': 'uncertain'}
            except OSError as exc:
                self.log_push_failure(step, 'not-started', str(exc))
                return {'kind': 'failed'}
        finally:
            try:
                os.unlink(expect)
            except OSError:
                pass
        if r.returncode != 0:
            self.log_push_failure(step, r.returncode, r.stderr)
            if r.returncode == 6:
                return {'kind': 'uncertain'}
            # rc 2 includes connect-phase failures, safe for the engine to retry.
            self.log('push rc=%s' % r.returncode)
            return {'kind': 'failed'}
        try:
            rec = json.loads((r.stdout or '').strip().splitlines()[-1])
            mid = rec['message_id']
            ok = rec.get('ok') is True and isinstance(mid, int) and not isinstance(mid, bool)
            if ok and chat is not None and str(rec.get('chat_id')) != str(chat):
                ok = False
        except (ValueError, KeyError, IndexError, TypeError):
            ok = False
        if ok:
            return {'kind': 'ok', 'message_id': mid}
        self.log_push_failure(step, r.returncode, r.stderr)
        return {'kind': 'uncertain'}

    def send(self, screen_id, flow, ctx):
        try:
            parts = self.parts(screen_id, flow, ctx)
        except Unrenderable as exc:
            self.log('unrenderable %s: %s' % (screen_id, exc))
            # Loud even without a logger: a screen the machine cannot render
            # faithfully is a defect, never a quiet partial send.
            print('mint: screen %s not sent: %s' % (screen_id, exc), file=sys.stderr)
            if str(exc) == 'catalog unavailable':
                return {'kind': 'not_implemented', 'wp': 'WP3', 'seam': 'screens.send'}
            return {'kind': 'failed'}
        if parts is None:
            return {'kind': 'none'}
        last = None
        for text, buttons in parts:
            res = self.push(text, buttons, flow, step=screen_id)
            if res['kind'] != 'ok':
                return res
            last = res
        return last or {'kind': 'failed'}
