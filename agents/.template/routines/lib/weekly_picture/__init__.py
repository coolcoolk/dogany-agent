"""View-only weekly H2 picture renderer (DGN-2359).

Input is the renderer feed: day/ref_day are relative integers 1..28, not
calendar dates. strands.list contains Why nodes (id, label, start, rho,
deg, shade, mark; optional from/into lineage), strands.hows contains
(id, why, day, label), and strands.days maps day strings to touches
(id, state, items=[{kind: done|decision, text, optional how}]). Links must
already be recorded by the feed producer; this module never infers them.

Optional dependencies: numpy, Pillow, scipy, declared in the adjacent
requirements.txt. Full mint installs these in bridge/venv;
launchd's /usr/bin/python3 is a separate interpreter. To provision routine
interpreters with the existing installer (from the canonical checkout):
  bash scripts/pack/pack_deps_provision.sh --root INSTANCE \
    --requirements INSTANCE/routines/lib/weekly_picture/requirements.txt
Or install with the exact rendering interpreter:
  /path/to/python3 -m pip install numpy Pillow scipy
No automatic pip calls or interpreter switching occur during rendering.

Fonts: macOS uses AppleSDGothicNeo.ttc. Other hosts must set DOGANY_FONT_DIR
to a directory containing ASDGN_Medium.ttf and ASDGN_ExtraBold.ttf.
Output is always 1800x2520; optional check output is 390x546. At most nine
Whats/day and eight Hows/Why are drawn; omitted counts appear in muted text.
"""
import copy
import importlib
import importlib.util
import math
import os
from pathlib import Path
import shlex
import sys


class DependencyError(RuntimeError):
    """The selected interpreter cannot render a picture."""


def check_dependencies():
    missing = []
    for module, package in (("numpy", "numpy"), ("PIL", "Pillow"), ("scipy.ndimage", "scipy")):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if missing:
        raise DependencyError(
            "missing render dependencies for {}: {}; install with: {} -m pip install numpy Pillow scipy"
            .format(sys.executable, ", ".join(missing), shlex.quote(sys.executable)))


def font_faces():
    apple = Path('/System/Library/Fonts/AppleSDGothicNeo.ttc')
    if sys.platform == 'darwin' and apple.is_file():
        return ((apple, 0), (apple, 6))
    directory = os.environ.get('DOGANY_FONT_DIR')
    if directory:
        medium = Path(directory) / 'ASDGN_Medium.ttf'
        bold = Path(directory) / 'ASDGN_ExtraBold.ttf'
        if medium.is_file() and bold.is_file():
            return ((medium, 0), (bold, 0))
    raise ValueError('AppleSDGothicNeo unavailable; set DOGANY_FONT_DIR to a directory '
                     'containing ASDGN_Medium.ttf and ASDGN_ExtraBold.ttf')


def _day(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 28:
        raise ValueError('{} must be a relative day integer in 1..28'.format(field))
    return value


def validate_feed(feed):
    """Validate all references before caps, so hidden bad links also fail."""
    try:
        _day(feed['day'], 'day')
        strands = feed['strands']
        _day(strands['ref_day'], 'strands.ref_day')
        whys = {}
        for why in strands['list']:
            wid = why['id']
            if not isinstance(wid, str) or not wid or wid in whys:
                raise ValueError('duplicate or invalid Why id: {!r}'.format(wid))
            whys[wid] = why
            _day(why['start'], 'Why {} start'.format(wid))
            for key in ('rho', 'deg'):
                if not math.isfinite(float(why[key])):
                    raise ValueError('Why {} {} must be finite'.format(wid, key))
            if not isinstance(why['shade'], int) or not 0 <= why['shade'] < 6:
                raise ValueError('Why {} shade must be in 0..5'.format(wid))
            if not isinstance(why['mark'], int) or why['mark'] < 1:
                raise ValueError('Why {} mark must be a positive integer'.format(wid))
        edges = {}
        for wid, why in whys.items():
            refs = ([why['from']] if why.get('from') else [])
            for merge in why.get('into', []):
                _day(merge['day'], 'Why {} merge day'.format(wid))
                refs.append(merge['id'])
            for ref in refs:
                if ref not in whys:
                    raise ValueError('unknown Why id {!r} in lineage of {!r}'.format(ref, wid))
            edges[wid] = refs
        visiting, visited = set(), set()

        def visit(wid):
            if wid in visiting:
                raise ValueError('cyclic Why lineage at {!r}'.format(wid))
            if wid not in visited:
                visiting.add(wid)
                for ref in edges[wid]:
                    visit(ref)
                visiting.remove(wid)
                visited.add(wid)
        for wid in whys:
            visit(wid)
        hows = {}
        for how in strands.get('hows', []):
            hid = how['id']
            if not isinstance(hid, str) or not hid or hid in hows:
                raise ValueError('duplicate or invalid How id: {!r}'.format(hid))
            if how['why'] not in whys:
                raise ValueError('unknown Why id {!r} for How {!r}'.format(how['why'], hid))
            _day(how['day'], 'How {} day'.format(hid))
            hows[hid] = how
        for day, touches in strands.get('days', {}).items():
            _day(int(day), 'touch day')
            for touch in touches:
                wid = touch['id']
                if wid not in whys:
                    raise ValueError('unknown Why id {!r} on day {}'.format(wid, day))
                for item in touch.get('items', []):
                    if item['kind'] not in ('done', 'decision'):
                        raise ValueError('unknown What kind {!r}'.format(item['kind']))
                    if not isinstance(item['text'], str):
                        raise ValueError('What text must be a string')
                    hid = item.get('how')
                    if hid is not None and hid not in hows:
                        raise ValueError('unknown How id {!r} on day {}'.format(hid, day))
                    if hid is not None and hows[hid]['why'] != wid:
                        raise ValueError('How {!r} belongs to a different Why than {!r}'.format(hid, wid))
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError('invalid weekly picture feed: {}'.format(exc)) from exc


def _truncate(text, font, width):
    """Bound labels by measured pixels; leave short labels byte-identical."""
    text = str(text)
    lines = []
    for line in text.split('\n')[:2]:
        if font.getlength(line) > width:
            lo, hi = 0, len(line)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.getlength(line[:mid] + '\u2026') <= width:
                    lo = mid
                else:
                    hi = mid - 1
            line = line[:lo] + '\u2026'
        lines.append(line)
    return '\n'.join(lines)


def _prepare(feed, faces):
    from PIL import ImageFont
    data = copy.deepcopy(feed)
    strands = data['strands']
    # Historical synthetic history is sample-only; real feeds draw recorded touches.
    if not data.get('illustrative'):
        data['history'] = []
    data.setdefault('labels', {})
    strands.setdefault('days', {})
    font = ImageFont.truetype(str(faces[0][0]), 38, index=faces[0][1])
    for node in strands['list'] + strands.get('hows', []):
        node['label'] = _truncate(node.get('label', ''), font, 780)
    for key in ('tag', 'corner'):
        if key in data:
            data[key] = _truncate(data[key], font, 1200)
    kept, counts = [], {}
    dropped_hows = set()
    for how in strands.get('hows', []):
        wid = how['why']
        counts[wid] = counts.get(wid, 0) + 1
        if counts[wid] <= 8:
            kept.append(how)
        else:
            dropped_hows.add(how['id'])
    strands['hows'] = kept
    omitted = {}
    for day, touches in strands['days'].items():
        total = sum(len(t.get('items', [])) for t in touches)
        for touch in touches:
            touch['items'] = [it for it in touch.get('items', []) if it.get('how') not in dropped_hows]
            for item in touch['items']:
                item['text'] = _truncate(item['text'], font, 780)
        visible = min(9, sum(len(t['items']) for t in touches))
        if total > visible and int(day) <= data['day']:
            omitted[int(day)] = total - visible
    overflow = ['What +{} (day {})'.format(n, d) for d, n in sorted(omitted.items())]
    if dropped_hows:
        overflow.append('How +{}'.format(len(dropped_hows)))
    return data, overflow


def render(feed, out, check_out=None, settings=None):
    """Render one H2 picture without modifying feed or process settings."""
    validate_feed(feed)
    check_dependencies()
    faces = font_faces()
    data, overflow = _prepare(feed, faces)
    spec = importlib.util.spec_from_file_location(__name__ + '._h2', Path(__file__).with_name('_h2.py'))
    module = importlib.util.module_from_spec(spec)
    module.DATA, module.FONT_FACES, module.OVERFLOW = data, faces, overflow
    from .settings import H2
    original = H2.copy()
    try:
        H2.update(settings or {})
        spec.loader.exec_module(module)
        img = module.render_image()
    finally:
        H2.clear()
        H2.update(original)
    img.save(out, format='PNG')
    if check_out is not None:
        from PIL import Image
        img.resize((390, 546), Image.Resampling.LANCZOS).save(check_out, format='PNG')
    return {'size': img.size, 'overflow': overflow}
