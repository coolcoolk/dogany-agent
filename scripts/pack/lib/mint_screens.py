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

DGN-1910 (locked spec DGN-1910-LOCKED, dec-288..294): the custom staff
agent. custom_ready() (section 4.1 facts b-d), the kit menu and its i18n
names, existing_custom (this crew's custom agents), the reserved-name file
.mint-flow/<flow_id>.reserved (section 3.8), design_problem() (section 4.2:
role rules a-f, menu skills, custom_skill.py validate, totals),
design_facts() (custom_skill.py facts), the ONE owner screen design_confirm
(section 12 S-DESIGN, DRAFT copy kept in mint_flow_table.json copy_draft),
the date slug custom-YYMMDD-n with tombstones, and the custom branch of
lock_selection / configure_accepted_flow producing the section 3.1
selection. custom_skill.py and secret_scan.py are called only through their
frozen CLI/API.
"""
import contextlib
import datetime
import hashlib
import io
import types
import unicodedata
import json
import os
import re
import subprocess
import sys
import tempfile
import time
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
import mint_single_agent
import crew_paths                                            # noqa: E402
import custom_skill as cs                                    # noqa: E402

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

# DGN-1910
TABLE_PATH = HERE / 'mint_flow_table.json'
CUSTOM_SKILL = HERE / 'custom_skill.py'
CREW_KIT_VERSION = HERE / 'crew_kit_version.sh'
TEMPLATE_LIB = REPO / 'agents' / '.template' / 'routines' / 'lib'
ROLE_MAX = 120
CUSTOM_MAX = cs.MAX_CUSTOM
KST = datetime.timezone(datetime.timedelta(hours=9))
NON_RECORD_MODULES = ('module_core', 'module_brief')
SCHEDULE_MODULE = 'module_schedule'          # the one module with approved overlap copy
DESIGN_CODES = ('secret', 'charset', 'grammar', 'cite', 'trigger_not_owner', 'collision', 'limit',
                'uses_kit', 'decoy', 'role_official', 'prose')
NEWBORN_PLACEHOLDER = '/nonexistent/dgn1910-newborn'
_DESIGN_PH_RE = re.compile(r'<(role|kit_names|display_name|purpose|facts|leader|other)>')

_TOKEN_RE = re.compile(r'\d{3,}:[A-Za-z0-9_-]{20,}')
_USER_RE = re.compile(r'^[A-Za-z0-9_]{3,64}$')
_PLACEHOLDER_RE = re.compile(r'<(?:username|first_name|도메인|표시이름)>')


class Unrenderable(Exception):
    """The screen cannot be produced faithfully (missing fact / bad asset)."""


def pack_row_dir(catalog_path, row):
    """A catalog row's pack directory by the one coordinate lookup
    (pack_coords.sh pack_row_dir, DGN-1818 / DGN-1883 WP2): a local checkout
    (legacy package_dir, pack-sources .repo), else the pack-src clone the kit
    install made, else the would-be location.  None when the row has no
    coordinate at all."""
    try:
        out = subprocess.run(['bash', str(PACK_COORDS), 'row-dir', str(catalog_path), row['id']],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out) if out else None


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
    pkg = pack_row_dir(catalog_path, row)
    manifest = pkg / 'pack-manifest.json' if pkg is not None else None
    if manifest is not None and manifest.is_file():
        try:
            declared = json.loads(manifest.read_text(encoding='utf-8'))
            if not isinstance(declared, dict):
                raise ValueError('manifest is not an object')
        except (OSError, ValueError):
            raise Unrenderable('pack manifest is unreadable')
        # pack_version is the agent pack contract.  `version` is retained only
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


def charset_problem(text):
    """DGN-1910 section 3.2 F4 on the role line: custom_skill's one rule
    (field_charset_ok), and the role is one line. True = refused."""
    return '\n' in text or not cs.field_charset_ok(text)


def _load_lib(name):
    """A shared routines/lib module (role_prose, secret_scan), or None."""
    path = TEMPLATE_LIB / (name + '.py')
    if not path.is_file():
        return None
    if str(TEMPLATE_LIB) not in sys.path:
        sys.path.insert(0, str(TEMPLATE_LIB))
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LIFE_STEMS = []


def life_stems():
    """life_engine.KNOWN_STEMS (the life kit's two stems), else the literal."""
    if not _LIFE_STEMS:
        try:
            lib = _load_lib('life_engine')
            stems = tuple(lib.KNOWN_STEMS) if lib is not None else ()
        except Exception:
            stems = ()
        _LIFE_STEMS.append(stems or ('kit_life', 'lifekit'))
    return _LIFE_STEMS[0]


def secret_hits(text, mode='strict'):
    """secret_scan.scan(text, mode) (section 3.5), the lib custom_skill
    scans with. Without it the screen cannot judge: fail closed."""
    lib = cs.secret_scan_lib()
    if lib is None:
        raise Unrenderable('secret_scan lib missing')
    return bool(lib.scan(text, mode))


def table_copy():
    """DRAFT owner copy (DGN-1910 section 12), kept in the state table, and
    the section 12 internal-word list."""
    t = json.loads(TABLE_PATH.read_text(encoding='utf-8'))
    return t.get('copy_draft') or {}, t.get('role_banned_words') or []


def _nfkc_norm(text):
    return cb._norm(unicodedata.normalize('NFKC', str(text or '')))


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
                 agents_dir=None, handoff_root=None, crew_dir=None,
                 kit_root=None, custom_skill=None, crew_kit_lib=None, consume_root=None):
        self.root = Path(instance_root)
        # DGN-1910 seams (None -> the installed defaults, resolved at call time)
        self._kit_root, self._crew_kit_lib, self._consume_root = kit_root, crew_kit_lib, consume_root
        self._resolved_kit_roots = {}
        self.custom_skill = Path(custom_skill) if custom_skill else CUSTOM_SKILL
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
        try:
            return Path(subprocess.run(
                ["bash", str(REPO / "scripts/pack/lib/canonical_catalog.sh"),
                 str(REPO), str(self.root), "mint_screens"],
                check=True, capture_output=True, text=True, timeout=30).stdout.strip())
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unrenderable("catalog resolution failed") from exc

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
        # DGN-1910: choice_words and design_words are owner words too
        for key in ('context', 'choice_words', 'design_words'):
            parts += [(x.get('answer') or '').strip() for x in (flow.get(key) or [])
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
        for registry in sorted(self.crews_dir().glob('*/crew.conf')):
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
        configure_accepted_flow completes the selection around it.
        DGN-1910: a custom fit pins the date slug and the whole section 3.1
        selection at the design accept."""
        if flow.get('fit') == 'custom':
            return self._lock_custom(flow)
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
        if flow.get('fit') == 'custom':
            return self._configure_custom(flow, bot)
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
        if pack_row_dir(self.catalog_path(), pack) is None:
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
            # An official agent pack goes straight to the token prompt (the handover
            # line rides in front of it, no confirm buttons: owner copy
            # 2026-09-24); an agent pack whose domain already has a dedicated agent
            # ends there (END_N7, DGN-1702). An empty list is the engine's
            # no-fit signal: this screen is never rendered without options.
            return [{'label': self._name(r),
                     'route': 'INSTALL_MAIN' if flow.get('main_agent') else
                     'END_N7' if self._bound_agent(r) else 'WAIT_TOKEN'} for r in matched]
        if screen_id in AGAIN_SCREENS:
            return [{'label': AGAIN_LABEL, 'route': 'OPEN'}]
        if screen_id == 'design_confirm':
            # DGN-1910: one button; the engine takes a tap on it as the accept
            return [{'label': table_copy()[0]['design_button'], 'route': 'WAIT_TOKEN'}]
        return []

    # -------------------------------------------------------------- copy
    def _target(self, flow):
        t = flow.get('target') or {}
        return t if isinstance(t, dict) else {}

    @staticmethod
    def _custom_sel(flow):
        sel = (flow or {}).get('selection')
        return sel if isinstance(sel, dict) and sel.get('kind') == 'custom' else None

    def _custom_role(self, sel):
        """dec-295: a custom flow has no domain word; the owner-confirmed role
        line takes its place, byte-equal to the confirm (never shortened)."""
        role = sel.get('role')
        if not isinstance(role, str) or not role.strip() \
                or role != (sel.get('design_confirm') or {}).get('text') \
                or ' '.join(role.split()) != role or len(role) > ROLE_MAX \
                or any(not c.isprintable() or c in '<>' for c in role):
            raise Unrenderable('custom: no clean confirmed role line')
        return role

    def _domain(self, flow):
        sel = self._custom_sel(flow)
        if sel is not None:
            return self._custom_role(sel)
        pack = self.chosen_pack(flow)
        if pack is None:
            raise Unrenderable('no chosen pack')
        # domain_term only ever returns a word the agent pack declares; the
        # request just picks which one (DGN-1702).
        word = clean_name(cb.domain_term([pack], flow.get('request') or ''))
        if not word:
            raise Unrenderable('chosen pack declares no domain')
        return word

    def custom_handoff(self, text, flow):
        """Section 12 handoff (custom), DRAFT copy_draft.handoff_custom: after
        the opening paragraph, the shared-records line and -- only when custom
        skills were made -- the grow-by-talking line. Passes cb._judge."""
        sel, t = self._custom_sel(flow), self._target(flow)
        lines = table_copy()[0].get('handoff_custom') or []
        if len(lines) != 2:
            raise Unrenderable('custom handoff copy missing')
        leader = self.leader_name()
        new_agent = clean_name(t.get('first_name')) or '@' + self._username(t.get('username'))
        if not leader or _TOKEN_RE.search(leader + new_agent):
            raise Unrenderable('custom handoff names')
        add = [lines[0].replace('<leader>', leader)]
        if sel.get('custom_skills'):
            add.append(lines[1].replace('<new_agent>', new_agent))
        head, sep, rest = text.partition('\n\n')
        out = head + '\n\n' + '\n'.join(add) + (sep + rest if sep else '')
        if cb._judge(out):
            raise Unrenderable('custom handoff fails the copy rulings')
        return out

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
        # request by naming the agent pack it judged (--fit), e.g. "헬스 트레이너
        # 에이전트 말씀이신가요?". The words of that agent pack's own NAME are the
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
        if flow.get('main_agent'):
            # The same agent pack buttons confirm installation here. There is
            # never a second button offering a single/split architecture.
            names = ', '.join(self._name(r) for r in matched or [])
            return ('\uc694\uccad\ud558\uc2e0 \ub300\ub85c \uc9c0\uae08 \uc800\uc640 \ud568\uaed8 \ud558\ub098\ub85c \uc9c4\ud589\ud560\uac8c\uc694.\n\n'
                    + names + '\n\n\uc120\ud0dd\ud558\uba74 \uc81c\uac00 \uc774 \uc5ed\ud560\uacfc \ud544\uc694\ud55c \uae30\ub2a5\uc744 \ub9e1\uac8c \ub429\ub2c8\ub2e4.')
        text = self.stored_lead(flow) + '\n\n' + self.catalog_text(flow)
        if matched is not None and len(matched) == 1:
            text += '\n\n' + load_asset(CATALOG_INVITE)
        return text

    def prepare_main_agent(self, flow):
        flow['main_agent'] = (mint_single_agent.request_basis(flow)
                              if self._conf('.instance.conf', 'DOGANY_AGENT_CLASS') == 'main'
                              else None)

    def install_main_agent(self, flow):
        return mint_single_agent.install(self, flow)

    def parts(self, screen_id, flow, ctx):
        """Ordered [(text, buttons)] the screen is made of. Every part is
        required: no part of a screen is ever dropped quietly (DGN-1702 A2)."""
        if screen_id in NO_COPY:
            return None
        if screen_id == 'catalog':
            return [(self.catalog_message(flow), button_rows(self.options('catalog', flow)))]
        if screen_id == 'design_confirm':
            return [(self.design_text(flow), button_rows(self.options('design_confirm', flow)))]
        if screen_id not in ASSETS:
            raise Unrenderable('unknown screen')
        out = []
        if screen_id == 'token_prompt':
            hb = self.handover_text(flow)
            if hb:
                out.append((hb, []))
        text = fill(load_asset(ASSETS[screen_id]), self._values(screen_id, flow))
        if screen_id == 'handoff' and self._custom_sel(flow) is not None:
            text = self.custom_handoff(text, flow)
        out.append((text, button_rows(self.options(screen_id, flow))))
        if screen_id in FOLLOW_UP:
            out.append((load_asset(FOLLOW_UP[screen_id]), []))
        return out

    # ---------------------------------------- DGN-1910 custom staff agent
    def _conf(self, name, key):
        return conf_get(self.root / name, key)

    def leader_kit(self):
        """The leader's kit as a catalog id. The shipped kit-select writes the
        life kit as KIT=lifekit (onboarding-check.py tristate) while its
        catalog row is kit_life (DGN-1238: both stems are live); a life stem
        with no catalog row of its own resolves to the life stem that has one."""
        kit = conf_get(self.root / 'config' / 'agent.conf', 'KIT')
        stems = life_stems()
        if kit in stems and self.kit_row(kit) is None:
            return next((s for s in stems if self.kit_row(s) is not None), kit)
        return kit

    def leader_name(self):
        return clean_name(self._conf('.instance.conf', 'DOGANY_AGENT_LABEL')
                          or self._conf('.instance.conf', 'DOGANY_AGENT_NAME') or self.root.name)

    def _catalog_rows(self):
        try:
            rows = json.loads(Path(self.catalog_path()).read_text(encoding='utf-8')).get('packs')
        except (OSError, ValueError, AttributeError):
            return []
        return [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get('id'), str)]

    def kit_row(self, kit):
        return next((r for r in self._catalog_rows() if r['id'] == kit and r.get('kind') == 'kit'), None)

    def _design_kit(self, flow):
        return ((flow or {}).get('design') or {}).get('kit') or self.leader_kit()

    def kit_root(self, kit=None):
        """The kit payload (service/<kit>/bundle.conf, config/i18n, skills-bundle):
        <package dir>/<manifest payload_root, default "payload"> -- the same
        resolution as mint_run.sh _custom_kit_root, so the design screen and
        the build judge one payload."""
        if self._kit_root:
            return Path(self._kit_root)
        selected = kit or self.leader_kit()
        if not selected:
            return self.root
        if selected in self._resolved_kit_roots:
            return self._resolved_kit_roots[selected]
        row = self.kit_row(selected)
        pkg = pack_row_dir(self.catalog_path(), row) if row is not None else None
        if pkg is None:
            raise Unrenderable('no kit payload')
        try:
            sub = json.loads((pkg / 'pack-manifest.json').read_text(encoding='utf-8')).get('payload_root')
        except (OSError, ValueError, AttributeError):
            sub = None
        root = pkg / (sub if isinstance(sub, str) and sub else 'payload')
        self._resolved_kit_roots[selected] = root
        return root

    @staticmethod
    def _kit_i18n(kit_root):
        try:
            d = json.loads((kit_root / 'config' / 'i18n' / 'ko.bundle.json').read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _bundle_rows(kit, kit_root):
        try:
            text = (kit_root / 'service' / kit / 'bundle.conf').read_text(encoding='utf-8')
        except OSError:
            raise Unrenderable('kit bundle.conf unreadable')
        return cs.bundle_rows(text)

    def kit_menu(self, kit=None):
        """The kit menu (section 0 K18): kind=skill rows of bundle.conf, with
        the i18n display name, module and module name. record = may be a
        uses_kit target (module not core/brief, section 3.2)."""
        kit = kit or self.leader_kit()
        root = self.kit_root(kit)
        i18n = self._kit_i18n(root)
        out = []
        for f in self._bundle_rows(kit, root):
            if len(f) < 5 or f[0].startswith('@') or f[1] != 'skill':
                continue
            key = f[3] if f[3] and f[3] != '-' else 'bundle.' + f[0].replace('-', '_')
            out.append({'id': f[0], 'name': i18n.get(key + '.name') or f[0],
                        'tagline': i18n.get(key) or '', 'module': f[4],
                        'module_name': i18n.get('module.%s.name' % f[4]) or '',
                        'record': f[4] not in NON_RECORD_MODULES})
        return out

    def menu_fact(self, flow):
        """The menu the agent drafts from (DESIGN_DRAFT fact)."""
        if not ((flow or {}).get('design') or {}).get('kit_menu_available'):
            return []
        try:
            return [{k: m[k] for k in ('id', 'name', 'module_name', 'record')}
                    for m in self.kit_menu(self._design_kit(flow))]
        except Unrenderable:
            return []

    def crews_dir(self):
        return Path(self._crew_dir) if self._crew_dir else crew_paths.crews_dir()

    def crew_lists_root(self, kit):
        try:
            lines = (self.crews_dir() / kit / 'crew.conf').read_text(encoding='utf-8').splitlines()
        except OSError:
            return False
        me = os.path.realpath(str(self.root))
        return any(line.startswith('member=') and os.path.realpath(line[7:].strip()) == me
                   for line in lines)

    def crew_kit_version(self, kit):
        """(state, version) from lib/crew_kit_version.sh (_crew_kit_version,
        moved out of pack_install.sh). Scanned as the newborn would see the
        crew: no member is excluded, so the leader counts. No lib -> not
        ready (fail closed)."""
        lib = Path(self._crew_kit_lib) if self._crew_kit_lib else CREW_KIT_VERSION
        if not lib.is_file():
            return 'unavailable', None
        script = ('ROOT="$3"; SCRIPT_DIR="$4"; . "$1" || exit 2; _crew_kit_version "$2" || exit 1; '
                  'printf "%s|%s\\n" "$CREW_KIT_STATE" "$CREW_KIT_V"')
        try:
            r = self.run(['bash', '-c', script, 'crew-kit-version', str(lib), kit, NEWBORN_PLACEHOLDER,
                          str(HERE.parent)], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return 'unavailable', None
        if r.returncode != 0:
            return 'unavailable', None
        state, _, version = (((r.stdout or '').strip().splitlines() or [''])[-1]).partition('|')
        return state, (version or None)

    def tag_exists(self, row, tag):
        pkg = pack_row_dir(self.catalog_path(), row)
        if pkg is None:
            return False
        try:
            r = self.run(['git', '-C', str(pkg), 'rev-parse', '-q',
                          '--verify', 'refs/tags/' + tag], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0

    def custom_ready(self, flow=None):
        """Section 4.1 facts (b)-(d); the engine adds (a), its constant, and
        logs every fact. (b) class=main, KIT= names a catalog kind=kit row and
        that kit's crew.conf lists this root; (c) the crew kit version is
        uniform and its tag exists; (d) >=1 kit menu skill."""
        f = {'a_ordering': self._conf('.instance.conf', 'DOGANY_AGENT_CLASS') == 'main',
             'b_leader': False, 'c_crew_kit': False, 'd_menu': False, 'kit': None, 'version': None,
             'source_tag': None, 'crew_state': None, 'menu_count': 0}
        kit = self.leader_kit()
        row = self.kit_row(kit) if kit else None
        f['kit'] = kit or None
        f['b_leader'] = bool(row) and self._conf('.instance.conf', 'DOGANY_AGENT_CLASS') == 'main' \
            and self.crew_lists_root(kit)
        if row is not None:
            state, version = self.crew_kit_version(kit)
            f['crew_state'] = state
            if state == 'uniform' and version:
                try:
                    tag = pack_source_tag(kit, version)
                except Unrenderable:
                    tag = None
                f.update({'version': version, 'source_tag': tag})
                f['c_crew_kit'] = bool(tag) and self.tag_exists(row, tag)
            try:
                f['menu_count'] = len(self.kit_menu(kit))
            except Unrenderable:
                f['menu_count'] = 0
            f['d_menu'] = f['menu_count'] >= 1
        f['kit_menu_available'] = bool(f['b_leader'] and f['c_crew_kit'] and f['d_menu'])
        f['ready'] = f['a_ordering']
        return f

    # -- the crew's custom agents (minting-state rows + tombstones)
    def _ledger(self):
        try:
            d = json.loads((self.root / 'config' / 'minting-state.json').read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _custom_rows(state):
        """[(slug, row)] of the ledger's kind=custom rows. ONE shape, the
        one minting_state.py writes (accept --custom-json): slug-keyed
        `domains` rows with kind=custom; retired ones keep the row and add a
        top-level `retired_custom` tombstone (_tombstones)."""
        dom = state.get('domains')
        if not isinstance(dom, dict):
            return []
        return [(k, v) for k, v in dom.items()
                if isinstance(k, str) and k and isinstance(v, dict) and v.get('kind') == 'custom']

    @staticmethod
    def _tombstones(state):
        return [t.get('slug') for t in state.get('retired_custom') or []
                if isinstance(t, dict) and isinstance(t.get('slug'), str)]

    def _custom_display_names(self, slug, row):
        """Display names of a custom agent's skills: the ledger row's own
        dicts, else the newborn's why-seed.json custom[], else the rendered
        SKILL.md frontmatter, else the name."""
        names = row.get('custom_skills') or []
        if names and all(isinstance(n, dict) for n in names):
            return [clean_name(n.get('display_name') or n.get('name')) for n in names]
        agent = Path(row['agent_path']) if isinstance(row.get('agent_path'), str) else self.agents_dir() / slug
        seed = {}
        try:
            seed = {c.get('name'): c.get('display_name')
                    for c in json.loads((agent / 'config' / 'why-seed.json').read_text(encoding='utf-8'))
                    .get('custom') or [] if isinstance(c, dict)}
        except (OSError, ValueError, AttributeError):
            pass
        out = []
        for n in (str(x) for x in names):
            disp = seed.get(n)
            if not disp:
                try:
                    m = re.search(r'^display_name:\s*(.+)$',
                                  (agent / '.claude' / 'skills' / n / 'SKILL.md').read_text(encoding='utf-8'),
                                  re.M)
                    disp = m.group(1).strip().strip('"\'') if m else None
                except OSError:
                    disp = None
            out.append(clean_name(disp or n))
        return out

    def existing_custom(self, flow):
        """This crew's live custom staff agents (section 2 Duplicates):
        [{slug, role, kit[], custom[display_name]}]; tombstones excluded."""
        state = self._ledger()
        kit = self._design_kit(flow)
        dead = set(self._tombstones(state))
        out = []
        for slug, row in self._custom_rows(state):
            if slug in dead or row.get('crew') not in (None, kit):
                continue
            out.append({'slug': slug, 'role': str(row.get('role') or '')[:ROLE_MAX],
                        'kit': [x for x in row.get('skills') or [] if isinstance(x, str)],
                        'custom': self._custom_display_names(slug, row)})
        return out

    # -- reserved names (section 3.8)
    def reserved_path(self, flow):
        return self.root / '.mint-flow' / (flow['flow_id'] + '.reserved')

    def reserved_names(self, kit=None):
        """Every dir under the consume root's skills/, every kit payload
        skill name (menu + always-on + skills-bundle), every name linked in
        this instance's .claude/skills*/."""
        kit = kit or self.leader_kit()
        names = set()
        consume = Path(self._consume_root or self._conf('.instance.conf', 'DOGANY_CONSUME_ROOT') or REPO)
        if (consume / 'skills').is_dir():
            names.update(p.name for p in (consume / 'skills').iterdir() if p.is_dir())
        try:
            root = self.kit_root(kit)
            if (root / 'skills-bundle').is_dir():
                names.update(p.name for p in (root / 'skills-bundle').iterdir() if p.is_dir())
            for f in self._bundle_rows(kit, root):
                names.add(f[1] if f[0] == '@always-on' and len(f) > 1 else f[0])
        except Unrenderable:
            pass
        for d in sorted(self.root.glob('.claude/skills*')):
            if d.is_dir():
                names.update(p.name for p in d.iterdir())
        return sorted(n for n in names if n and not n.startswith(('@', '.')))

    def write_reserved(self, flow):
        """.mint-flow/<flow_id>.reserved for custom_skill.py validate
        --reserved: one name per line; the first line `dogany-*` stands for
        the reserved prefix. 0600, atomic replace."""
        path = self.reserved_path(flow)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.reserved.', dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(['dogany-*'] + self.reserved_names(self._design_kit(flow))) + '\n')
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    # -- section 4.2 design_problem
    def closed_vocabulary(self, kit=None):
        """Words the role may carry only when the owner said them: catalog
        domains, kit skill names, kit bundle taglines, kit module words."""
        words = []
        for r in self._catalog_rows():
            words += [d for d in r.get('domains') or [] if isinstance(d, str)]
        try:
            for m in self.kit_menu(kit):
                words += [m['id'], m['name'], m['tagline'], m['module_name']]
        except Unrenderable:
            pass
        out = []
        for w in words:
            k = _nfkc_norm(w)
            if k and cb._key_long_enough(k) and k not in out:
                out.append(k)
        return out

    def _official_claim(self, role, slug):
        """(f) the card-first haystack gate: catalog_brief gate --haystack
        "<slug> <role>" against this instance's catalog stamp; rc 20 = an
        official pack claims it and its card was not shown."""
        args = types.SimpleNamespace(catalog=str(self.catalog_path()), ttl=21600, pack='',
                                     stamp=str(self.root / 'config' / 'mint-catalog-brief.json'),
                                     haystack='%s %s' % (slug, role))
        rc = 0
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                cb.cmd_gate(args)
            except SystemExit as exc:
                rc = exc.code
        if rc not in (0, 20):
            raise Unrenderable('catalog gate could not run')
        return rc == 20

    def role_problem(self, flow, role, own=False):
        """Section 4.2 role rules, first finding wins: (a) decoy, (b)
        validate_role_prose, (c) one line <=120 + the mechanical copy
        rulings + the section 12 internal words, (d) secret_scan strict,
        (e) the section 3.2 charset, (f) the official haystack gate. The
        owner's own line (own=True) skips (a) and the internal words."""
        if not isinstance(role, str) or not role.strip():
            return 'grammar'
        if not own:
            said = _nfkc_norm(self.owner_words(flow or {}))
            tight = said.replace(' ', '')
            r = _nfkc_norm(role)
            if any(k in r and k not in said and k.replace(' ', '') not in tight
                   for k in self.closed_vocabulary(self._design_kit(flow))):
                return 'decoy'
        prose = _load_lib('role_prose')
        if prose is None:
            raise Unrenderable('role_prose lib missing')
        try:
            prose.validate_role_prose(role, str(self.root))
        except ValueError:
            return 'prose'
        if '\n' in role or '\r' in role or len(role) > ROLE_MAX:
            return 'limit'
        banned = [] if own else table_copy()[1]
        if cb._judge(role) or any(w in role for w in banned):
            return 'prose'
        if secret_hits(role, 'strict'):
            return 'secret'
        if charset_problem(role):
            return 'charset'
        # The official-card haystack only needs the custom namespace and date;
        # probing every estate holder here made each draft validation pay the
        # full occupancy scan.  The real, never-reused suffix is still chosen
        # by custom_slug() when the confirmed design is locked.
        day = datetime.datetime.now(KST).strftime('%y%m%d')
        if self._official_claim(role, 'custom-%s' % day):
            return 'role_official'
        return None

    def _custom_skill(self, verb, *args):
        try:
            return self.run([sys.executable, str(self.custom_skill), verb] + [str(a) for a in args],
                            capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unrenderable('custom_skill.py %s did not run: %s' % (verb, type(exc).__name__))

    def design_problem(self, flow, design, path, own_role=False):
        """Section 4.2 on the canonical design bytes at `path`: first finding
        -> {code, skill, field}, else None."""
        def p(code, field, skill=None):
            return {'code': code, 'skill': skill, 'field': field}
        code = self.role_problem(flow, design.get('role'), own=own_role)
        if code:
            return p(code, 'role')
        skills, customs = design.get('skills'), design.get('custom_skills')
        if not isinstance(skills, list) or not all(isinstance(x, str) for x in skills):
            return p('grammar', 'skills')
        if not isinstance(customs, list) or not all(isinstance(x, dict) for x in customs):
            return p('grammar', 'custom_skills')
        kit = self._design_kit(flow)
        kit_menu_available = bool(((flow or {}).get('design') or {}).get('kit_menu_available'))
        menu = [m['id'] for m in self.kit_menu(kit)] if kit_menu_available else []
        if len(set(skills)) != len(skills):
            return p('limit', 'skills')
        if any(x not in menu for x in skills):
            return p('uses_kit', 'skills')
        if len(customs) > CUSTOM_MAX:
            return p('limit', 'custom_skills')
        if len(skills) + len(customs) < 1:
            return p('limit', 'design')
        if customs:
            reserved = self.write_reserved(flow)
            r = self._custom_skill('validate', '--design', path,
                                   '--flow', self.root / 'config' / 'mint-flow.json',
                                   '--kit-root', self.kit_root(kit), '--instance-root', self.root,
                                   '--reserved', reserved, *(['--no-kit'] if not kit else []))
            if r.returncode == 1:
                try:
                    hit = json.loads(((r.stdout or '').strip().splitlines() or ['{}'])[0])
                except ValueError:
                    hit = {}
                hit = hit if isinstance(hit, dict) else {}
                return p(hit.get('code') if hit.get('code') in DESIGN_CODES else 'grammar',
                         hit.get('field') if isinstance(hit.get('field'), str) else None,
                         hit.get('skill') if isinstance(hit.get('skill'), str) else None)
            if r.returncode != 0:
                raise Unrenderable('custom_skill.py validate rc %s' % r.returncode)
        return None

    def design_facts(self, flow, design, path):
        """The design screen's facts -- the ONLY source of what it shows and
        of design_confirm at the accept: role text, kit skills (+ display
        names), custom skills {name, display_name, purpose, facts} with facts
        from custom_skill.py facts, the leader, the overlap names."""
        kit = self._design_kit(flow)
        menu = self.kit_menu(kit) if ((flow or {}).get('design') or {}).get('kit_menu_available') else []
        names = {m['id']: m['name'] for m in menu}
        customs = design.get('custom_skills') or []
        fmap = {}
        if customs:
            r = self._custom_skill('facts', '--design', path, '--kit-root', self.kit_root(kit),
                                   '--lang', 'ko', *(['--no-kit'] if not kit else []))
            try:
                fmap = json.loads((r.stdout or '').strip()) if r.returncode == 0 else None
            except ValueError:
                fmap = None
            if not isinstance(fmap, dict):
                raise Unrenderable('custom_skill.py facts gave no facts')
        custom = []
        for c in customs:
            if not isinstance(fmap.get(c.get('name')), str):
                raise Unrenderable('custom_skill.py facts missed a skill')
            custom.append({'name': c['name'], 'display_name': c.get('display_name'),
                           'purpose': c.get('purpose'), 'facts': fmap[c['name']]})
        schedule = {m['id'] for m in menu if m['module'] == SCHEDULE_MODULE}
        overlap = []
        for e in (flow or {}).get('existing_custom') or []:
            if isinstance(e, dict) and set(e.get('kit') or []) & set(design['skills']) & schedule:
                label = conf_get(self.agents_dir() / str(e.get('slug')) / '.instance.conf',
                                 'DOGANY_AGENT_LABEL')
                overlap.append(clean_name(label or e.get('slug')))
        return {'text': design['role'], 'skills': list(design['skills']),
                'kit_names': [names[s] for s in design['skills']], 'custom': custom,
                'leader': self.leader_name(), 'overlap': overlap}

    def design_text(self, flow):
        """S-DESIGN (section 12) -- DRAFT owner copy from copy_draft, filled
        only from the facts stored with the screen. The kit line is left out
        when no kit skill is used, the new-feature header when no custom
        skill is made; one overlap line per agent sharing the schedule."""
        f = ((flow or {}).get('design') or {}).get('facts')
        if not isinstance(f, dict) or not isinstance(f.get('text'), str):
            raise Unrenderable('design facts missing')
        copy, _ = table_copy()
        lines = copy['design_confirm']

        def sub(template, **vals):
            if any(_TOKEN_RE.search(str(v)) for v in vals.values()):
                raise Unrenderable('token-shaped value')
            return _DESIGN_PH_RE.sub(lambda m: str(vals[m.group(1)]), template)
        out = [lines[0], sub(lines[1], role=f['text'])]
        if f.get('kit_names'):
            out.append(sub(lines[2], kit_names=' \u00b7 '.join(f['kit_names'])))
        if f.get('custom'):
            out.append(lines[3])
            out += [sub(lines[4], display_name=c['display_name'], purpose=c['purpose'], facts=c['facts'])
                    for c in f['custom']]
        out.append(sub(lines[5], leader=f.get('leader') or ''))
        out += [sub(copy['overlap'], other=o) for o in f.get('overlap') or []]
        out.append(lines[6])
        text = '\n'.join(out)
        if cb._judge(text):               # dec-295: the feature line passes the copy rulings
            raise Unrenderable('design screen fails the copy rulings')
        return text

    # -- section 3.8 slug
    def custom_slug(self, now=None):
        """custom-<YYMMDD>-<n>, KST date: n = 1 + the max n over this
        ledger's custom rows AND tombstones of that date and the dirs under
        the agents dir, its _archive* / .retired* dirs and the teardown
        archive (<agents dir>/../torn-down); a live holder (handoff, crew)
        still moves it on. A slug is never reused. The one implementation
        (DGN-1967 step 12; minting_state.py's CLI copy is gone)."""
        day = datetime.datetime.fromtimestamp(time.time() if now is None else now, KST).strftime('%y%m%d')
        rx = re.compile(r'^custom-%s-([1-9]\d{0,5})(?:$|[.-])' % day)   # bare, or with an archive suffix
        state = self._ledger()
        seen = [slug for slug, _ in self._custom_rows(state)] + self._tombstones(state)
        base = self.agents_dir()
        dirs = [base, base.parent / 'torn-down']
        if base.is_dir():
            dirs += sorted(d for d in base.iterdir()
                           if d.name.startswith(('_archive', '.retired')) and d.is_dir())
        for d in dirs:
            try:
                seen += [p.name for p in d.iterdir()]
            except OSError:
                pass
        n = 1 + max([int(m.group(1)) for m in (rx.match(x) for x in seen) if m] or [0])
        for _ in range(100):
            slug = 'custom-%s-%d' % (day, n)
            if not self._occupied(slug):
                return slug
            n += 1
        raise Unrenderable('no free custom slug')

    def _design_bytes(self, flow):
        try:
            data = (self.root / '.mint-flow' / (flow['flow_id'] + '.design.json')).read_bytes()
        except OSError:
            raise Unrenderable('design file unreadable')
        return data, hashlib.sha256(data).hexdigest()

    def _lock_custom(self, flow):
        """The section 3.1 selection, pinned at the design accept. Refuses
        unless the canonical design file is the confirmed one, invariant 1
        (the confirm came in another request than the draft) and invariant
        2 (confirm == screen facts == selection) hold."""
        d = flow.get('design') or {}
        conf = d.get('confirm')
        if not isinstance(conf, dict) or not d.get('sha'):
            raise Unrenderable('custom: no confirmed design')
        data, sha = self._design_bytes(flow)
        if not (sha == d['sha'] == conf.get('design_sha')):
            raise Unrenderable('custom: design changed after confirm')
        if not conf.get('request_id') or conf['request_id'] == d.get('request_id'):
            raise Unrenderable('custom: self-confirm')
        obj = json.loads(data.decode('utf-8'))
        keys = ('name', 'display_name', 'purpose')
        seen = [{k: c.get(k) for k in keys} for c in conf.get('custom') or []]
        spec = [{k: c.get(k) for k in keys} for c in obj['custom_skills']]
        facts = d.get('facts') or {}
        if conf.get('text') != obj['role'] or conf.get('skills') != obj['skills'] or seen != spec \
                or [conf.get(k) for k in ('text', 'skills', 'custom')] \
                != [facts.get(k) for k in ('text', 'skills', 'custom')]:
            raise Unrenderable('custom: confirm, screen facts and design disagree')
        kit_menu_available = bool(d.get('kit_menu_available'))
        kit = (d.get('kit') or self.leader_kit()) if kit_menu_available else None
        if kit_menu_available and (not kit or not d.get('source_tag')):
            raise Unrenderable('custom: no crew kit version')
        slug = self.custom_slug(conf.get('at'))
        newborn = self.agents_dir() / slug
        flow['selection'] = {
            'kind': 'custom', 'pack_id': kit, 'kit_id': kit, 'no_kit': not kit_menu_available,
            'slug': slug, 'domain': slug,
            'role': obj['role'], 'skills': list(obj['skills']), 'custom_skills': obj['custom_skills'],
            'custom_sha': sha, 'design_file': '.mint-flow/%s.design.json' % flow['flow_id'],
            'source_tag': d.get('source_tag'), 'version': d.get('version'),
            'design_confirm': {'text': conf['text'], 'skills': list(conf['skills']),
                               'custom': [dict(c) for c in conf.get('custom') or []],
                               'request_id': conf['request_id'], 'message_id': conf.get('message_id'),
                               'confirm_kind': conf.get('confirm_kind'),
                               'confirm_input': conf.get('confirm_input'),
                               'confirmed_at': conf.get('at')},
            'existing_custom': list(flow.get('existing_custom') or []),
            'why_seed': str(newborn / 'config' / 'why-seed.json')}

    def _configure_custom(self, flow, bot):
        """The bot identity around the locked custom selection; the design
        file must still be the confirmed bytes."""
        sel = flow.get('selection') if isinstance(flow.get('selection'), dict) else {}
        if sel.get('kind') != 'custom' or not isinstance(sel.get('slug'), str):
            raise Unrenderable('custom: selection not locked')
        if self._design_bytes(flow)[1] != sel.get('custom_sha'):
            raise Unrenderable('custom: design changed after confirm')
        flow['target'] = {'slug': sel['slug'], 'root': str(self.agents_dir() / sel['slug']),
                          'domain': sel['slug'], 'username': self._username((bot or {}).get('username')),
                          'first_name': clean_name((bot or {}).get('display_name'))}

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
