"""Locked H2 raster implementation, ported from DGN-2359 retro-weekly.py.

Drawing math and order are retained from v7. DATA, FONT_FACES and OVERFLOW
are injected into a fresh module namespace by weekly_picture.render so calls
share neither state nor environment overrides. Only H2 is exposed.
"""
import math
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import gaussian_filter

from .settings import H2

E = lambda key, default: float(H2.get(key, default))
PREVIEW = bool(DATA.get('illustrative'))

D = dict(bg='#052C30', ink='#F4F1E9', muted='#ADCAC0', line='#739E91',
         accent='#6FD0C0', alert='#E8B15A', danger='#F2A397', surface='#124248')
hexc = lambda h: np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], float)
P_ = {k: hexc(v) for k, v in D.items()}
mix = lambda a, b, t: a + (b - a) * t
UNLIT = mix(P_['surface'], P_['bg'], 0.6)   # an LCD cell that is off
WHY_SHADE = [hexc('#A6F0DC'), hexc('#36B3C9'), hexc('#7FD98F'), hexc('#8FB4F0'), hexc('#D6E79A'), hexc('#C9A8F0')]
# 1 pale mint, 2 blue teal, 3 leaf green, 4 soft blue, 5 lime, 6 lavender (born today on the close card)
# v25: What dots by kind -- done coral, decision amber (both What)
KIND_C = dict(done=P_['danger'], decision=P_['alert'])
KIND_LIGHT = dict(done=mix(P_['danger'], P_['ink'], 0.3), decision=mix(P_['alert'], P_['ink'], 0.35))
QUIET_C = mix(P_['accent'], P_['bg'], 0.36)         # background strands
CASE_C = mix(P_['bg'], UNLIT, 0.3)                  # casing between crossing strands
GLASS_EDGE = mix(P_['accent'], P_['bg'], 0.45)      # v15 pillar edge colour

# ---- canvas: 600 x 500 css px landscape, LCD cell = 2 css px (300 x 250 cells at any scale) ----
W, H = 600, 840   # real card 420 css (v24), preview 500
CW, CH = W // 2, H // 2
def _tt(size, index=0):
    path, face = FONT_FACES[index in (4, 6)]
    return ImageFont.truetype(str(path), size, index=index if path.suffix == '.ttc' else face)


# ---- geometry (world units, y up): v17 spiral, fixed in the world ----
DPW = 7
WPT = 3
WEEKS = 4
DAYS = DPW * WEEKS
RP = 0.80                        # glass tube radius
RI, RO = RP + 0.06, 1.75
RM = RI + (RO - RI) * E('LANE', 0.54)   # v26: inner lane RI..RM = How, outer lane RM..RO = What
PITCH = 5.0
SP = 0.075                       # dot pitch along today's spokes
THICK = 0.17
BOARD = 0.10
WA = 2 * math.pi / WPT
WGAP = 0.07
DW = (WA - 2 * WGAP) / DPW
Y0 = BOARD
TOP = Y0 + THICK + PITCH * WEEKS / WPT + 0.35
REF = DATA['strands']['ref_day']  # the day the strand angles were authored against

# ---- the camera: v17's distance, field of view and framing, turned to today's cell (the cylinder turns with the date) ----
# round 6 (owner 2026-10-13 12:18 "How\uac00 \ub0a9\uc791\ud558\uac8c \ubcbd\uc744 \ud0c0\uace0 \uc62c\ub77c\uac00\uac8c \ud574\uc918"): FLAT = 1 turns the band into a flat ribbon
# lying on the tube wall -- How lane RI..RM only HDEP deep, starting STANDOFF off the glass (0 = flush on the wall),
# the What rim RM..RO a thinner WDEP strip on its outer edge. The camera fit and the base board keep v27's radii.
WK_CORNER = DATA.get('corner', '')          # weekly B: date range text for the corner
WK_CX, WK_CY = E('CX', 22), E('CY', 20)
WK_FADE = E('WKFADE', 0)          # weekly C: fade of the previous week (older weeks: WKFADE2)
WK_FADE2 = E('WKFADE2', WK_FADE)
WK_HI = E('WKHI', 0)              # weekly C: lift of this week's past cells toward accent
# weekly C rev 2 (owner 2026-10-15 18:12 "\uae30\uc874 \uc5f0\uacb0\ub4e4\uc744 \uc5c6\uc564\uac8c \uc544\uc26c\uc6b4\ub370"): WKLINK > 0 decouples the earlier weeks'
# connections (rungs, How tracks, What dots) from the slab fade -- an earlier week's connection is drawn at WKLINK x the
# strength the same connection has in this week's band (today's: rung 1.0, How track 0.95 / 0.55), not x the dimmer
# past-day style (that read as removed at 390 px).
WK_LINK = E('WKLINK', 0)


def wk_early(wk):
    return WK_LINK > 0 and N is not None and wk < week_of(N)


def wk_link(wk, base=1.0, past=1.0):
    """Alpha factor of a connection on a day of week wk; base = this week's alpha, past = its past-day alpha."""
    if wk_early(wk):
        return WK_LINK * base / past
    return 1 - 0.85 * wk_fade(wk)


def wk_fade(wk):
    if WK_FADE <= 0 or N is None:
        return 0.0
    back = week_of(N) - wk
    return 0.0 if back <= 0 else WK_FADE if back == 1 else WK_FADE2


RI0, RO0 = RI, RO
FLAT = bool(E('FLAT', 0))
if FLAT:
    RI = RP + E('STANDOFF', 0.08)
    RM = RI + E('HDEP', 0.16)
    RO = RM + E('WDEP', 0.06)
# round 7 (owner 2026-10-13 13:15 "B / What\uc740 \uc774\uc81c \uae30\uc874\ucc98\ub7fc"): WHAT27 = 1 keeps the flat How ribbon RI..RM and puts What
# back as v27's own element -- the outer day band WM..WO (v27's RM..RO) with its THICK slab, colours and \ub05d\ub0b8 \uc77c / \uacb0\uc815
# dots, solid in front like v27. The How ribbon's walls then end at RM (RO = RM); no rim.
WHAT27 = FLAT and bool(E('WHAT27', 0))
WM, WO = (RI0 + (RO0 - RI0) * E('LANE', 0.54), RO0) if WHAT27 else (RM, RO)
RO = RM
DIST = 26.0                            # v17
YC = TOP / 2 - 0.15                    # v17 look-at height (tube middle): the whole tube stays framed
ELEV0 = math.radians(E('ELEV', 38))    # v19 30; a little higher so today's cell top (the rung ends) opens up
ELEV_K = math.radians(E('ELK', 0.8))   # small tweak: + this per unit of today's height below the tube middle
CAM_OFF = math.radians(E('OFF', -55))  # v19 -20; owner: turn clockwise seen from above (more negative)
FIT_X, FIT_Y = (E('FX0', 330), E('FX1', 592)), (E('FY0', 14), H - 10 + E('FYB', 70))   # v24: FYB moves the fit box down   # the spiral envelope fits the right part of the card (css px)
WOFF = int(E('WOFF', 0))          # weekly: shift the day slots so one 7-cell block = one Sun..Sat week


def rel(day, u):
    w, k = divmod(day - 1 + WOFF, DPW)
    return w * WA + WGAP + (k + u) * DW


def rel_c(t):
    """rel at a continuous day position t (day d, u = t - d); used to move the camera between days."""
    d = min(DAYS, max(1, int(math.floor(t))))
    return rel(d, t - d)


th = rel                         # the world is fixed: a day's cell angle is its own


def y_at(day, u):
    return Y0 + THICK + rel(day, u) / (2 * math.pi) * PITCH


def cyl(r, a, y):
    return np.array([r * math.cos(a), y, r * math.sin(a)])


cam = fw = rt = up = None
_GX, _GY = np.meshgrid(np.arange(CW) + 0.5, np.arange(CH) + 0.5)
_DIR = None


def _fit(c, f_, r_, u_):
    """v17's fit of the spiral's rotation-invariant envelope (radius RO, floor to tube top) into FIT_X x FIT_Y."""
    pts = []
    for k in range(72):
        a = 2 * math.pi * k / 72
        for y in (0.0, FIT_TOP):
            d = np.array([RO0 * math.cos(a), y, RO0 * math.sin(a)]) - c
            z = d @ f_
            pts.append(((d @ r_) / z, -(d @ u_) / z))
    P = np.array(pts)
    sx = (FIT_X[1] - FIT_X[0]) / 2 / (P[:, 0].max() - P[:, 0].min())
    sy = (FIT_Y[1] - FIT_Y[0]) / 2 / (P[:, 1].max() - P[:, 1].min())
    f = min(sx, sy)
    ox = (FIT_X[0] + FIT_X[1]) / 4 - f * (P[:, 0].max() + P[:, 0].min()) / 2
    oy = (FIT_Y[0] + FIT_Y[1]) / 4 - f * (P[:, 1].max() + P[:, 1].min()) / 2
    return f, ox, oy


def _orbit(az, el):
    c = np.array([DIST * math.cos(el) * math.cos(az), YC + DIST * math.sin(el), DIST * math.cos(el) * math.sin(az)])
    f_ = (np.array([0, YC, 0.0]) - c) / np.linalg.norm(np.array([0, YC, 0.0]) - c)
    r_ = np.cross(f_, [0, 1, 0]); r_ /= np.linalg.norm(r_)
    return c, f_, r_, np.cross(r_, f_)


# v24: the fit can stop a little above today (V25_FITUP world units) so a low 'today' is not lost in an empty tube;
# the tube then runs out of the card's top edge (the turns still to come). FITUP < 0 = v23's whole-tube fit. v24 card: 600 x 420 css (v23 500).
FIT_UP = E('FITUP', 0.6)
FIT_TOP = TOP if FIT_UP < 0 else min(TOP, y_at(DATA['day'], 1) + FIT_UP)
# the envelope is rotation-invariant, so one fit (at v17's 32 deg) serves every azimuth: same size, same place
FOCAL, SCR_X, SCR_Y = _fit(*_orbit(math.radians(90), math.radians(32)))
ELEV = ELEV0


def set_camera(tc):
    """Camera for a continuous day position tc (today's cell centre = N + 0.5): v17's orbit, azimuth turned so that
    day's cell faces the viewer (front, a little left), elevation raised a little as today sits lower on the tube."""
    global cam, fw, rt, up, _DIR, ELEV
    a = rel_c(tc)
    y = Y0 + THICK + a / (2 * math.pi) * PITCH
    ELEV = ELEV0 + ELEV_K * (YC - y)
    cam, fw, rt, up = _orbit(a + CAM_OFF, ELEV)
    _DIR = fw[None, None, :] + ((_GX - SCR_X) / FOCAL)[..., None] * rt + (-(_GY - SCR_Y) / FOCAL)[..., None] * up


def proj(p):
    d = np.asarray(p, float) - cam
    z = d @ fw
    return np.array([SCR_X + FOCAL * (d @ rt) / z, SCR_Y - FOCAL * (d @ up) / z, z])


# ---- per-day state (set_day): today N, the strands' height curve, the day's touches, the records ----
SL = DATA['strands']['list']
for _s in SL:                    # layout overrides for the sweep: V25_D<mark> / V25_R<mark>
    _s['deg'] = E(f"D{_s.get('mark', 0)}", _s['deg'])
    _s['rho'] = E(f"R{_s.get('mark', 0)}", _s['rho'])
SBY = {s['id']: s for s in SL}
TOUCH = {int(k): v for k, v in DATA['strands']['days'].items()}
RUNG_U = (E('U0', 0.08), E('U1', 0.92))
# H2: the What slab is a fixed 3x3 grid of cubes per day.
CUBE_S, CUBE_G = E('CUBES', 0.10), E('CUBEG', 0.025)
CUBE_ROWS, CUBE_COLS = 3, 3
CUBE_E = E('CUBEE', 0.25)         # H2: an empty cube's face, the band's own tone lifted this far toward 'line'
CUBE_EA = E('CUBEEA', 0.8)        # H2: an empty cube's alpha (filled cubes are opaque)


def cube_slots(n):
    """H2 cube centres in fill order, outer row first, equal world-space gaps."""
    step, rc = CUBE_S + CUBE_G, (WM + WO) / 2
    rows, per = CUBE_ROWS, [CUBE_COLS] * CUBE_ROWS
    out = []
    for i, m in enumerate(per):
        r = rc + ((rows - 1) / 2 - i) * step
        out += [(r, 0.5 + (j - (m - 1) / 2) * step / (r * DW)) for j in range(m)]
    return out


def rungs_for(day, touches):
    """v25: a day's Whats spread evenly along its cell like base pairs, grouped by where they hang: first the Whats
    straight on a Why, then one group per How (so a How's rungs sit side by side). Earlier items before tonight's
    ('new'). Position along the cell carries no meaning; the radius alternates so neighbouring ends do not touch."""
    flat = [dict(s=SBY[tc['id']], kind=it['kind'], text=it['text'], day=day, new=bool(it.get('new')), how=it.get('how'))
            for tc in touches for it in tc.get('items', [])]
    hord = {h['id']: k for k, h in enumerate(DATA['strands'].get('hows', []))}
    # a How's Whats first in the day: the How forks off low in the cell and its sub-strand runs up the rest of it
    flat.sort(key=lambda r: (r['how'] is None, hord.get(r['how'], 99), r['new'], r['s']['mark']))
    flat = flat[:9]
    n = len(flat)
    for k, r in enumerate(flat):
        r['u'] = (RUNG_U[0] + RUNG_U[1]) / 2 if n == 1 else RUNG_U[0] + (RUNG_U[1] - RUNG_U[0]) * k / (n - 1)
        r['r'] = WM + (WO - WM) * (0.3, 0.7, 0.5)[k % 3]     # v26: Whats live in the outer lane
        r['r'], r['u'] = cube_slots(n)[k]
    return flat


ALL_RUNGS = {d: rungs_for(d, TOUCH[d]) for d in sorted(TOUCH)}   # world-fixed: independent of which day is today
# v26: the Hows -- tracks in the inner lane of the ramp. A How is born just before its first What on its birth day
# and runs along the ramp through the last day it is used. 'attach' = the day-times of every What hung on it.
# Each How keeps one radius slot for its whole life (greedy over [born, last use], so concurrent Hows never share a
# slot); its letter is A, B, ... in birth order among the Hows of its Why.
# round 8 (owner 2026-10-13 13:32 "how\uc758 \uc904\uae30\ub294 \uc606\uba74\uc744 \ub530\ub974\uac8c \ud574\uc904\ub798? \uc704 \uc881\uc796\uc544"): HSIDE = 1 routes the How tracks
# down the flat ribbon's outer side face (radius RO, HOW_DEPTH tall) instead of its narrow top; slot k sits a fraction
# HS_LO..HS_HI of the face height below the top edge (0.45: every today track stays in view).
HSIDE = FLAT and bool(E('HSIDE', 0))
HS_LO, HS_HI = E('HSLO', 0.08), E('HSHI', 0.45)   # deeper slots hide behind v27's What band on the front
# round 9 (owner 2026-10-13 13:50 "\uc606\uba74 \ub192\uc774\uae30\ub9cc \ud574\ubd10"): only the side face grows -- render with HDEPTH=1.3 (r8 0.7,
# ~1.86x); HS_LO..HS_HI stay fractions of that taller face, so the How tracks spread ~1.86x further apart.
# weekly D2 (owner 2026-10-15 18:27 "how\uac00 \ud310 \uc5c6\uc774 \uac01\uc790 \ud0c0\uace0 \uc62c\ub77c\uac00\ub294 \uac78\ub85c"): NOSLAB = 1 drops the How ribbon (its top
# and side walls); each How track then climbs the tube wall on its own helix at radius HRAD (default RO), its height
# slot spread over HDEPTH below the band top as before. What band (WHAT27) and the Why net unchanged.
NOSLAB = HSIDE and bool(E('NOSLAB', 0))
HOW_RAD = E('HRAD', 0)
HOW_RSPREAD = E('HRSPREAD', 0)    # weekly D: slot k also sits HRSPREAD x (k + 0.5) / NSLOT further out (side-by-side at the silhouette)
# RSTAPER = 1: that radial step only near today (it fades in over the HTAPER days), so earlier days stay on the face
# weekly D: HTAPER > 0 days -- the slots spread over HSLO..HSHI on earlier days narrow to HSLO..HSHI_T over the last
# HTAPER days into today's cell, where the earlier days' What band hides the deeper part of the face (this week first).
HOW_TAPER, HS_HI_T = E('HTAPER', 0), E('HSHI_T', 0.4)
# weekly E1 (D1 polish): HSLO_T moves the top of today's slot range down too -- yesterday's What slab sits one day's
# rise lower and juts out past today's How face, covering its upper band from the earlier-days side (default = HSLO)
HS_LO_T = E('HSLO_T', HS_LO)
# weekly E2 (D1 polish): TODAYUP > 0 lifts today's How face over the earlier days' What band -- where that band sits in
# front of today's face it is mixed TODAYUP toward the face behind it (lower opacity), and today's How tracks are
# depth-tested without it, so the whole face carries today's strands (camera unchanged)
TODAY_UP = E('TODAYUP', 0)
HOW_TK = E('HWTK', 1)             # How track width factor on today's cell (tapers like the slots)


def how_drop(h, t):
    if HOW_TAPER <= 0 or N is None:
        return h['drop']
    w = SMOOTH((N - t) / HOW_TAPER)
    hi = HS_HI_T + (HS_HI - HS_HI_T) * w
    lo = HS_LO_T + (HS_LO - HS_LO_T) * w
    return lo + (hi - lo) * (h['slot'] + 0.5) / NSLOT
HOW_LEAD = E('HLEAD', 0.06)       # day fraction a How starts before its first What
LETTERS = 'ABCDEFGH'               # internal ids only (check json); never drawn since v27
HOW_DASH = [None, (E('DASH_ON', 3.2), E('DASH_OFF', 2.2)), (E('DOT_ON', 1.0), E('DOT_OFF', 2.0))]   # v27: css px
HL = []
for _h in DATA['strands'].get('hows', []):
    w = SBY[_h['why']]
    at = sorted(r['day'] + r['u'] for d, rs in ALL_RUNGS.items() for r in rs if r['how'] == _h['id'])
    first = [x for x in at if int(x) == _h['day']]
    birth = max(_h['day'] + 0.01, (first[0] if first else _h['day'] + 0.3) - HOW_LEAD)
    HL.append(dict(id=_h['id'], label=_h['label'], why=_h['why'], start=birth, born=_h['day'], new=bool(_h.get('new')),
                   shade=w['shade'], mark=w['mark'], attach=at, how=True,
                   last=max([int(x) for x in at] + [_h['day']])))
for _w in SL:
    for k, h in enumerate(sorted([h for h in HL if h['why'] == _w['id']], key=lambda h: h['start'])):
        h['letter'] = LETTERS[k]
        h['dash'] = HOW_DASH[min(k, len(HOW_DASH) - 1)]
_slots = []                       # per slot: the last day it is taken up to
for h in sorted(HL, key=lambda h: h['start']):
    free = [k for k, e in enumerate(_slots) if e < h['born']]
    k = free[0] if free else len(_slots)
    if k == len(_slots):
        _slots.append(0)
    _slots[k] = h['last']
    h['slot'] = k
NSLOT = max(1, len(_slots))
for h in HL:
    h['r'] = RI + (RM - RI) * (0.14 + 0.72 * (h['slot'] + 0.5) / NSLOT)
    h['drop'] = HS_LO + (HS_HI - HS_LO) * (h['slot'] + 0.5) / NSLOT   # round 8: height slot down the side face
# Consecutive height slots keep each Why's How lines together, oldest below.
HOW_ROW_H = E('HTILE_H', 0.060)
HOW_ROW_GAP = E('HTILE_SEAM', 0.008)
_how_rows = []
for _w in SL:
    group = sorted((h for h in HL if h['why'] == _w['id'] and h['attach']),
                   key=lambda h: h['start'])
    for row, h in enumerate(group):
        h['row'] = len(_how_rows)
        h['why_row'] = row
        _how_rows.append(h)
HOW_ROW_COUNT = len(_how_rows)
# Equal-brightness families belong to How rows, independent of the Why hue.
HOW_FAMILY = [hexc(c) for c in ('#65BCCF', '#E0AF44', '#89A6EF',
                                '#D09C98', '#82C170')]
WHAT_NEUTRAL = hexc('#B8BDB8')
for row, h in enumerate(_how_rows):
    h['colour'] = HOW_FAMILY[row % len(HOW_FAMILY)]
HBY = {h['id']: h for h in HL}
N = None
_KN = _SL = None
Y_TODAY = None
REC = {}
TODAY = {}                       # Why id -> today's touch (state, items)
TOUCHED, LABELLED = [], []
PHASE = 'after'
RUNGS = []                       # today's Whats: dict(s, kind, text, u, r, day, how) -- one rung each
PAST_RUNGS = []                  # the same for every earlier day


def set_day(n, phase):
    """Make day n 'today'. Returns nothing; every module-level function reads these globals."""
    global N, _KN, _SL, Y_TODAY, REC, TODAY, TOUCHED, LABELLED, PHASE, RUNGS, PAST_RUNGS
    N, PHASE = n, phase
    # height of day-time t on the strands: cubic Hermite through every day start (C1, so the week gaps make no kink);
    # inside today it is exactly today's helical cell top, so a strand tip and its spoke share one height
    _KN = [y_at(d, 0) for d in range(1, N + 1)]
    _SL = []
    for i in range(N):
        if i == N - 1:
            _SL.append(y_at(N, 1) - y_at(N, 0))
        elif i == 0:
            _SL.append(_KN[1] - _KN[0])
        else:
            _SL.append((_KN[i + 1] - _KN[i - 1]) / 2)
    Y_TODAY = y_at(N, 1)          # the top of today's band: nothing is drawn above this
    # records of the past days: a day with a touch list shows exactly its Whats; else the v13 rehearsal rule
    rng = random.Random(20261009)
    REC = {}
    for i, lv in enumerate(DATA.get('history', [])):
        if lv > 0:
            REC[i + 1] = dict(why=lv + rng.randint(-1, 0) or 1,
                              decision=2 * lv + rng.randint(0, 1), done=2 * lv + 1 + rng.randint(0, 1))
    for d, lst in TOUCH.items():
        if d <= N:
            its = [it for x in lst for it in x.get('items', [])]
            REC[d] = dict(why=len(lst), decision=sum(it['kind'] == 'decision' for it in its),
                          done=sum(it['kind'] == 'done' for it in its))
    TODAY = {x['id']: x for x in TOUCH.get(N, [])} if phase == 'after' else {}
    TOUCHED = sorted([SBY[k] for k in TODAY], key=lambda s: s['mark'])
    LABELLED = sorted([s for s in SL if s.get('label') and alive(s)], key=lambda s: s['mark'])
    RUNGS = ALL_RUNGS.get(N, []) if phase == 'after' else []
    PAST_RUNGS = [r for d in sorted(ALL_RUNGS) if d < N for r in ALL_RUNGS[d]]


def yd(t):
    if t >= N:
        return y_at(N, min(1.0, t - N))
    t = max(1.0, t)
    i = min(int(math.floor(t)) - 1, N - 2)
    s = t - (i + 1)
    h00, h10, h01, h11 = 2 * s ** 3 - 3 * s ** 2 + 1, s ** 3 - 2 * s ** 2 + s, -2 * s ** 3 + 3 * s ** 2, s ** 3 - s ** 2
    return h00 * _KN[i] + h10 * _SL[i] + h01 * _KN[i + 1] + h11 * _SL[i + 1]


DAY_LABEL = lambda: DATA['labels']['day'].format(d=N)


def state_of(day, phase):
    if day == N:
        return 'today' if phase == 'after' else 'wait'
    if day > N:
        return 'future'
    return 'filled' if day in REC else 'empty'


def week_of(day):
    return (day - 1 + WOFF) // DPW


# ======================= the Why net: strand paths in the tube's own (x, z) frame =======================
SMOOTH = lambda x: 0.0 if x <= 0 else 1.0 if x >= 1 else x * x * (3 - 2 * x)   # C1: Y-joins stay tangent
GROW = 5.0                       # days a past branch / merge takes to leave / reach its parent (slow: no hooks)
STEP = 0.03                      # sample step in days
TWIST = 9.0                      # degrees per day: fibres of a rope


def home_xz(s, t):
    a = math.radians(s['deg'] + TWIST * (REF + 0.5 - t))
    return np.array([s['rho'] * RP * math.cos(a), s['rho'] * RP * math.sin(a)])


def alive(s, phase=None):
    phase = phase or PHASE
    st = int(math.floor(s['start']))           # a How starts at a day-time inside its birth day
    return st < N or (st == N and phase == 'after')


def end_t(s):
    """Where the strand is drawn up to on today's card: its merge (if already merged), today's spoke (if touched),
    the middle of today (alive, untouched) or today's floor (before the retro)."""
    into = [i for i in s.get('into', []) if i['day'] <= N]
    if into:
        return max(i['day'] for i in into)
    if s['id'] in TODAY:
        return N + 1.0                    # touched today: the strand runs the whole day (its rungs sit along it)
    return N + (0.5 if PHASE == 'after' else 0.0)


def grow_of(s):
    return s.get('grow', GROW)


def xz_at(s, t):
    """World-fixed path: does not depend on which day is today."""
    p = home_xz(s, t)
    if s.get('from'):
        w = SMOOTH((t - s['start']) / grow_of(s))       # tangent to its parent at the fork
        p = xz_at(SBY[s['from']], t) * (1 - w) + p * w
    into = s.get('into', [])
    if into:
        last = max(into, key=lambda i: i['day'])
        w = SMOOTH((t - (last['day'] - GROW)) / GROW)
        p = p * (1 - w) + xz_at(SBY[last['id']], t) * w
    return p


def strand_threads(phase):
    out = []
    for s in SL:                         # v26: Hows are tracks on the ramp (how_tracks), not strands
        if not alive(s, phase):
            continue
        t0, t1 = s['start'], end_t(s)
        if t1 <= t0:
            continue
        ts = list(np.arange(t0, t1, STEP)) + [t1]
        out.append(dict(id=s['id'], kind='main', ts=ts, xz=[xz_at(s, t) for t in ts], s=s,
                        touched=s['id'] in TODAY))
        into = s.get('into', [])
        if len(into) > 1:
            last = max(into, key=lambda i: i['day'])
            for i in into:
                if i is last:
                    continue
                t2 = min(i['day'], end_t(s))
                ts2 = list(np.arange(i['day'] - GROW, t2, STEP)) + [t2]
                xz2 = []
                for t in ts2:
                    w = SMOOTH((t - (i['day'] - GROW)) / GROW)
                    xz2.append(xz_at(s, t) * (1 - w) + xz_at(SBY[i['id']], t) * w)
                out.append(dict(id=s['id'] + '>' + i['id'], kind='thread', ts=ts2, xz=xz2, s=s, touched=False))
    return out


def events(phase):
    """Branch / merge moments already visible on today's card (their middle is not above today)."""
    ev = []
    for s in SL:
        if not alive(s, phase):
            continue
        if s.get('from'):
            t = s['start'] + 0.5 * min(grow_of(s), GROW)
            if t <= end_t(s):
                ev.append(('branch', s['id'], t, s['from']))
        for i in s.get('into', []):
            if i['day'] <= N:
                ev.append(('merge', s['id'], i['day'] - 0.5 * GROW, i['id']))
    return ev


def world(xz, t):
    a0 = rel(REF, 0.5)
    c, s_ = math.cos(a0), math.sin(a0)
    x, z = xz
    return np.array([x * c - z * s_, yd(t), x * s_ + z * c])


STRAND_W = E('SW', 2.8)          # css px (today's Why strands)
RUNG_PX = E('RPX', 1.5)          # v22: rung width in px on the full-size (3x) card; drawn anti-aliased after the LCD snap
RUNG_PAST_A = E('RPA', 0.42)     # v23: past rungs clearly quieter than today's (owner: the full net is a bit much)
RUNG_PAST_MIX = E('RPM', 0.3)     # ... and their colour pulled this far toward the background
RUNG_OLD_A = E('ROA', 0.5)       # v24: today's earlier rungs (before tonight's links): dimmer than tonight's
RUNG_OLD_MIX = E('ROM', 0.3)
ANY_NEW = any(it.get('new') for v in TOUCH.values() for x in v for it in x.get('items', []))
HOW_W = E('HW', 1.5)              # v27: How track width (css px) on the days it is used (Why strand 2.8) ...
HOW_W_OFF = E('HWOFF', 0.9)       # ... and on the days in between (the track persists, thin and faint)
# round 10 (owner 2026-10-13 13:56 "\ub108\ube44\ub294 \ucda9\ubd84\ud574 how \uac1c\ubcc4 \uc120\uc744 \uc880 \ub113\uac8c \ud574\uc918"): render with HW=2.5 HWOFF=1.7 (r9 1.5 / 0.9,
# ~1.7-1.9x) so each How reads as its own wide line; capped below the Why strand (SW 2.8) so Why stays the boldest.


def strand_style(th_, t):
    """(colour, width css px, alpha, glow) -- width is constant per strand."""
    s = th_['s']
    if th_['touched']:
        return WHY_SHADE[s['shade']], STRAND_W, 1.0, 1.0      # born today: thick, bright, glowing
    if s.get('label'):
        # v22: every labelled Why wears its own colour (the past rungs leave from it), a little dimmer than today's
        return mix(WHY_SHADE[s['shade']], P_['bg'], 0.3), 2.8, 1.0, 0.0
    return QUIET_C, 2.2, 1.0, 0.0


# ======================= pass 1: slabs, z-buffer, LCD grid, glass tube =======================
HOW_DEPTH = E('HDEPTH', 1.6)       # v31: the How flight's depth below its top surface (v27: THICK = 0.17)
HOW_BANDS = int(E('HBANDS', 4))    # v31: wall strips (same id, no outline) for a soft top-to-bottom shade
HOW_DARK = E('HDARK', 0.2)         # v31: darkening at the underside edge of the walls
HOW_LO, HOW_HI = E('HLO', 0.05), E('HHI', 0.2)   # v31: wall cells lit from UNLIT toward the band tone, by light
RW = RI if FLAT else RP if E('HATTACH', 1) else RI  # round 6: the ribbon's own inner face; v31: inner wall radius -- on the tube wall
# round 5 (owner 2026-10-13 11:46, thickness compare): WTHICK >= 0 splits the band into two layers -- the How lane
# RW..RO is a slab HOW_DEPTH thick, the What lane RM..RO a thin rim WTHICK tall riding its outer edge, its top
# WLIFT above the How top (0 = flush hairline rim, = WTHICK = a lip sitting on the slab). WTHICK < 0 = v31.
W_THICK = -1.0 if WHAT27 else E('WTHICK', -1)
W_LIFT = E('WLIFT', 0) if W_THICK >= 0 else 0.0
W_LO, W_HI = E('WLO', 0.45), E('WHI', 0.3)       # rim walls: lit well above the How walls, a crisp light edge


def how_point(h, day, u):
    """Shared world position for the voxel run and its existing connections."""
    if not HSIDE:
        return cyl(h['r'], th(day, u), y_at(day, u) + 0.006)
    # Fixed row pitch avoids the old slot taper squeezing separate Hows together.
    radius = (HOW_RAD or RO) + 0.012
    down = E('HTILE_DROP', 0.0) + (HOW_ROW_COUNT - 1 - h.get('row', 0)) * (HOW_ROW_H + HOW_ROW_GAP)
    height = y_at(day, u) - down - HOW_ROW_H / 2
    return cyl(radius, th(day, u), height)


def render_grid(phase):
    zb = np.full((CH, CW), np.inf)
    zs = np.full((CH, CW), np.inf)
    zw = np.full((CH, CW), np.inf)    # v31: the How flight's walls / underside -- seen-through, not hiding the net
    zsn = np.full((CH, CW), np.inf)   # weekly E2: zs without the earlier days' What band (today's tracks test this)
    zf = np.full((CH, CW), np.inf)    # weekly E2: today's How volume (part 2), depth + colour, drawn or hidden
    cf = np.zeros((CH, CW, 3))
    idb = np.full((CH, CW), -1, int)
    col = np.zeros((CH, CW, 3))
    meta = {}

    def tri(a, b, c, color, sid):
        zonly = meta.get(sid, {}).get('zonly')     # weekly H: a cube face -- depth only, drawn later as a vector overlay
        pa, pb, pc = proj(a), proj(b), proj(c)
        x0 = max(0, int(math.floor(min(pa[0], pb[0], pc[0]))))
        x1 = min(CW - 1, int(math.ceil(max(pa[0], pb[0], pc[0]))))
        y0 = max(0, int(math.floor(min(pa[1], pb[1], pc[1]))))
        y1 = min(CH - 1, int(math.ceil(max(pa[1], pb[1], pc[1]))))
        if x0 > x1 or y0 > y1:
            return
        den = (pb[1] - pc[1]) * (pa[0] - pc[0]) + (pc[0] - pb[0]) * (pa[1] - pc[1])
        if abs(den) < 1e-9:
            return
        X, Y = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        l1 = ((pb[1] - pc[1]) * (X - pc[0]) + (pc[0] - pb[0]) * (Y - pc[1])) / den
        l2 = ((pc[1] - pa[1]) * (X - pc[0]) + (pa[0] - pc[0]) * (Y - pc[1])) / den
        l3 = 1 - l1 - l2
        m = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        z = l1 * pa[2] + l2 * pb[2] + l3 * pc[2]
        if zonly:
            for zk in (zs, zsn):
                zsub = zk[y0:y1 + 1, x0:x1 + 1]
                ms = m & (z < zsub)
                zsub[ms] = z[ms]
            return
        sub = zb[y0:y1 + 1, x0:x1 + 1]
        if meta.get(sid, {}).get('st') != 'future':
            zsub = (zw if meta.get(sid, {}).get('part') in (2, 3) else zs)[y0:y1 + 1, x0:x1 + 1]
            ms = m & (z < zsub)
            zsub[ms] = z[ms]
            if TODAY_UP > 0:
                md_ = meta.get(sid, {})
                if md_.get('day') == N and md_.get('part') == 2:
                    fs = zf[y0:y1 + 1, x0:x1 + 1]
                    mf = m & (z < fs)
                    fs[mf] = z[mf]
                    cf[y0:y1 + 1, x0:x1 + 1][mf] = color
                if md_.get('part') not in (2, 3) and not (md_.get('part') in (1, 4) and md_.get('day', N) < N):
                    nsub = zsn[y0:y1 + 1, x0:x1 + 1]
                    mn = m & (z < nsub)
                    nsub[mn] = z[mn]
        else:
            m &= np.isinf(zs[y0:y1 + 1, x0:x1 + 1])
        m &= z < sub
        sub[m] = z[m]
        idb[y0:y1 + 1, x0:x1 + 1][m] = sid
        col[y0:y1 + 1, x0:x1 + 1][m] = color

    def quad(a, b, c, d, color, sid):
        tri(a, b, c, color, sid); tri(a, c, d, color, sid)

    shade = lambda a: 0.55 + 0.45 * max(0.0, math.cos(a - (math.atan2(cam[2], cam[0]) + math.radians(35))))
    bgc = P_['bg']

    rings = [(0, RP, 'accent'), (RI0 - 0.06, RO0, 'line')]
    if E('FL_SPLIT', 0):
        r_mid = RP + (RO0 - RP) * E('FL_T', 0.5)
        rings = [(0, RP, 'accent'), (RI0 - 0.06, r_mid, 'line'), (r_mid, RO0, 'line')]
    for k, (r0, r1, key) in enumerate(rings):
        sid = 2000 + k
        c = mix(UNLIT, P_[key], 0.12)
        meta[sid] = dict(kind='board', edge=mix(P_[key], bgc, 0.62), div=None)
        n = 72
        for i in range(n):
            a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
            quad(cyl(r0, a0, BOARD), cyl(r1, a0, BOARD), cyl(r1, a1, BOARD), cyl(r0, a1, BOARD), c, sid)
            if k == len(rings) - 1:
                quad(cyl(r1, a0, 0), cyl(r1, a1, 0), cyl(r1, a1, BOARD), cyl(r1, a0, BOARD),
                     c * shade((a0 + a1) / 2) * 0.8, sid)

    cells = {}
    cubes = []
    for day in range(1, N + 1):    # v22: no slabs above today (the future turns are dotted ghosts, see ghost_dots)
        st = state_of(day, phase)
        if st == 'filled':
            r = REC[day]
            dens = min(1.0, (r['done'] + r['decision']) / 8)
            # v26: two lanes -- inner How lane a little darker and cooler, outer What lane as v25's band
            top = [mix(UNLIT, P_['accent'], 0.1 + 0.05 * dens), mix(UNLIT, P_['muted'], 0.16 + 0.12 * dens)]
            edge, div = mix(P_['line'], P_['muted'], 0.3), mix(P_['line'], bgc, 0.15)
            dd = mix(P_['line'], bgc, 0.38)
        elif st == 'today':
            top = [mix(UNLIT, P_['accent'], 0.2), mix(UNLIT, P_['ink'], 0.2)]
            edge, div, dd = P_['ink'], mix(P_['ink'], bgc, 0.3), P_['ink']
        elif st == 'wait':
            top = [mix(UNLIT, P_['ink'], 0.07)] * 2      # v23: freshly opened -- lit outline, empty, a faint lift
            edge, div, dd = P_['ink'], None, P_['ink']
        elif st == 'empty':
            top = [UNLIT] * 2
            edge, div = mix(P_['line'], P_['muted'], 0.5), mix(P_['line'], bgc, 0.5)
            dd = mix(P_['line'], bgc, 0.2)
        else:
            top = [UNLIT] * 2
            edge, div, dd = mix(P_['line'], bgc, 0.4), None, mix(P_['line'], bgc, 0.62)
        wall = mix(top[1], bgc, 0.45) if st in ('filled', 'today') else mix(UNLIT, bgc, 0.2)
        # v31: the deep walls are LCD cells lit a little above 'off', in the band's own tone, by how much light they face
        tone = P_['ink'] if st == 'today' else mix(P_['line'], P_['accent'], 0.3) if st == 'filled' else P_['line']
        wl = lambda light, tone=tone: mix(UNLIT, tone, HOW_LO + HOW_HI * light)
        rtone = P_['ink'] if st == 'today' else mix(P_['muted'], P_['ink'], 0.3) if st == 'filled' else P_['line']
        ids = [day * 10 + p for p in range(5 if WHAT27 else 4 if W_THICK >= 0 else 3)]   # round 5: part 3 = the What rim's sides; round 7: part 4 = v27's What slab sides
        for p, sid in enumerate(ids):
            meta[sid] = dict(kind='cell', day=day, week=week_of(day), part=p, edge=edge, div=div, dd=dd, st=st,
                             rim=P_['ink'] if st == 'today' else mix(P_['muted'], P_['ink'], 0.45) if st == 'filled' else mix(P_['line'], P_['muted'], 0.6))
        cells[day] = dict(st=st, ids=ids)
        n = 3
        for i in range(n):
            u0, u1 = i / n, (i + 1) / n
            a0, a1 = th(day, u0), th(day, u1)
            y0, y1 = y_at(day, u0), y_at(day, u1)
            if not NOSLAB:
                quad(cyl(RI, a0, y0), cyl(RM, a0, y0), cyl(RM, a1, y1), cyl(RI, a1, y1), top[0], ids[0])   # How lane
            L = W_LIFT
            if W_THICK >= 0:
                # round 5: the How slab (RW..RO, HOW_DEPTH deep below the How top) and, riding its outer edge, the thin
                # What rim (RM..RO): top W_LIFT above the How top, W_THICK tall; its outer face continues the slab's
                b0, b1 = max(BOARD, y0 - HOW_DEPTH), max(BOARD, y1 - HOW_DEPTH)
                c0, c1 = min(y0, y0 + L - W_THICK), min(y1, y1 + L - W_THICK)     # where the rim's face ends
                if RI > RW:
                    quad(cyl(RW, a0, y0), cyl(RI, a0, y0), cyl(RI, a1, y1), cyl(RW, a1, y1), top[0], ids[0])
                lo = (shade((a0 + a1) / 2) - 0.55) / 0.45
                for j in range(HOW_BANDS):
                    f0, f1 = j / HOW_BANDS, (j + 1) / HOW_BANDS
                    dk = 1 - HOW_DARK * (f0 + f1) / 2
                    ya0, ya1 = c0 + (b0 - c0) * f0, c1 + (b1 - c1) * f0
                    yb0, yb1 = c0 + (b0 - c0) * f1, c1 + (b1 - c1) * f1
                    quad(cyl(RO, a0, yb0), cyl(RO, a1, yb1), cyl(RO, a1, ya1), cyl(RO, a0, ya0),
                         wl((0.35 + 0.65 * lo) * dk), ids[2])
                    ya0, ya1 = y0 + (b0 - y0) * f0, y1 + (b1 - y1) * f0
                    yb0, yb1 = y0 + (b0 - y0) * f1, y1 + (b1 - y1) * f1
                    quad(cyl(RW, a0, yb0), cyl(RW, a1, yb1), cyl(RW, a1, ya1), cyl(RW, a0, ya0), wl(0.3 * dk), ids[2])
                quad(cyl(RW, a0, b0), cyl(RO, a0, b0), cyl(RO, a1, b1), cyl(RW, a1, b1), wl(0.1), ids[2])
                wr = lambda light: mix(UNLIT, rtone, W_LO + W_HI * light)
                if L > 0:          # the lip's step up from the How top
                    quad(cyl(RM, a0, y0), cyl(RM, a1, y1), cyl(RM, a1, y1 + L), cyl(RM, a0, y0 + L), wr(0.5), ids[3])
                quad(cyl(RO, a0, c0), cyl(RO, a1, c1), cyl(RO, a1, y1 + L), cyl(RO, a0, y0 + L),
                     wr(0.35 + 0.65 * lo), ids[3])
                continue
            if NOSLAB:
                continue
            # v31: the flight's volume -- walls HOW_DEPTH deep (down to the board at the foot), inner wall on the tube
            b0, b1 = max(BOARD, y0 - HOW_DEPTH), max(BOARD, y1 - HOW_DEPTH)
            if RI > RW:
                quad(cyl(RW, a0, y0), cyl(RI, a0, y0), cyl(RI, a1, y1), cyl(RW, a1, y1), top[0], ids[0])
            for j in range(HOW_BANDS):
                f0, f1 = j / HOW_BANDS, (j + 1) / HOW_BANDS
                dk = 1 - HOW_DARK * (f0 + f1) / 2                  # a little darker toward the underside
                ya0, ya1 = y0 + (b0 - y0) * f0, y1 + (b1 - y1) * f0
                yb0, yb1 = y0 + (b0 - y0) * f1, y1 + (b1 - y1) * f1
                lo = (shade((a0 + a1) / 2) - 0.55) / 0.45
                quad(cyl(RO, a0, yb0), cyl(RO, a1, yb1), cyl(RO, a1, ya1), cyl(RO, a0, ya0),
                     wl((0.35 + 0.65 * lo) * dk), ids[2])
                quad(cyl(RW, a0, yb0), cyl(RW, a1, yb1), cyl(RW, a1, ya1), cyl(RW, a0, ya0), wl(0.3 * dk), ids[2])
            quad(cyl(RW, a0, b0), cyl(RO, a0, b0), cyl(RO, a1, b1), cyl(RW, a1, b1), wl(0.1), ids[2])
        k = (day - 1 + WOFF) % DPW
        for u, end in ((0, k == 0), (1, k == DPW - 1)):
            if end:
                a, y = th(day, u), y_at(day, u)
                b = max(BOARD, y - HOW_DEPTH)
                if W_THICK >= 0:
                    c = min(y, y + W_LIFT - W_THICK)
                    quad(cyl(RW, a, b), cyl(RO, a, b), cyl(RO, a, y), cyl(RW, a, y), wl(0.6), ids[2])
                    quad(cyl(RM, a, c), cyl(RO, a, c), cyl(RO, a, y + W_LIFT), cyl(RM, a, y + W_LIFT),
                         mix(UNLIT, rtone, W_LO + W_HI * 0.6), ids[3])
                    continue
                if not NOSLAB:
                    quad(cyl(RW, a, b), cyl(RO, a, b), cyl(RO, a, y), cyl(RW, a, y), wl(0.6), ids[2])
        records = ALL_RUNGS.get(day, []) if st in ('filled', 'today') else []
        kinds = [r['kind'] for r in records]
        new = [bool(r.get('new')) for r in records]
        for j, (rc_, uc) in enumerate(cube_slots(len(kinds))):
            kd = kinds[j] if j < len(kinds) else None
            if kd is not None:
                how = HBY.get(records[j]['how'])
                c = mix(how['colour'], P_['ink'], 0.18) if how else WHAT_NEUTRAL
                c = (c if (new[j] or not ANY_NEW) else mix(c, bgc, 0.15)) if day == N else mix(c, bgc, 0.3)
                edge_c = mix(c, P_['ink'], 0.4)
            else:
                c = mix(top[1], P_['line'], CUBE_E)
                edge_c = mix(P_['ink'], bgc, 0.5) if st == 'today' else mix(edge, bgc, 0.3)
            sid = 100000 + day * 100 + j
            meta[sid] = dict(kind='cube', zonly=True, day=day)
            h, du = CUBE_S / 2, CUBE_S / 2 / (rc_ * DW)
            r0, r1, u0, u1 = rc_ - h, rc_ + h, uc - du, uc + du
            a0, a1, t0, t1 = th(day, u0), th(day, u1), y_at(day, u0), y_at(day, u1)
            b0, b1 = t0 - CUBE_S, t1 - CUBE_S
            side = lambda k: mix(c, UNLIT, k)
            lo = (shade((a0 + a1) / 2) - 0.55) / 0.45
            faces = [([cyl(r0, a0, t0), cyl(r1, a0, t0), cyl(r1, a1, t1), cyl(r0, a1, t1)], c),             # top
                     ([cyl(r1, a0, b0), cyl(r1, a1, b1), cyl(r1, a1, t1), cyl(r1, a0, t0)], side(0.5 - 0.2 * lo)),   # outer
                     ([cyl(r0, a0, b0), cyl(r1, a0, b0), cyl(r1, a0, t0), cyl(r0, a0, t0)], side(0.32)),     # ends
                     ([cyl(r0, a1, b1), cyl(r1, a1, b1), cyl(r1, a1, t1), cyl(r0, a1, t1)], side(0.32)),
                     ([cyl(r0, a0, b0), cyl(r0, a1, b1), cyl(r0, a1, t1), cyl(r0, a0, t0)], side(0.6)),      # inner
                     ([cyl(r0, a0, b0), cyl(r1, a0, b0), cyl(r1, a1, b1), cyl(r0, a1, b1)], side(0.7))]      # bottom
            for q, _ in faces:
                quad(*q, None, sid)
            cubes.append(dict(day=day, filled=kd is not None, faces=faces, edge=edge_c,
                              z=proj(cyl(rc_, th(day, uc), y_at(day, uc) - h))[2]))

    # The How runs are the geometry: no carrier wall or vector track overlay.
    for hi, how in enumerate(HL):
        used = sorted({int(t) for t in how['attach']
                       if t < N + (1 if phase == 'after' else 0)})
        if not used or how['born'] > N:
            continue
        radial, length, height = E('HTILE_R', 0.052), E('HTILE_L', 0.10), HOW_ROW_H
        gap = E('HTILE_GAP', 0.006)
        for day in range(how['born'], used[-1] + 1):
            start = how['start'] - day if day == how['born'] else 0.0
            radius = float(np.linalg.norm(how_point(how, day, start)[[0, 2]]))
            count = max(1, int(math.ceil((1 - start) * radius * DW / (length + gap))))
            pitch = (1 - start) / count
            du = min(length / (radius * DW), pitch * 0.94)
            colour = how['colour']
            # owner 2026-10-31 08:39 (FDGN-600 How pick 2): How rows lit harder, past days not dimmed.
            colour = mix(colour, P_['ink'], E('HLIT', 0.10)) if day in used else mix(colour, UNLIT, E('HDIM', 0.38))
            if day < N:
                colour = mix(colour, UNLIT, E('HPAST', 0.18))
            for tile in range(count):
                u0 = start + tile * pitch + (pitch - du) / 2
                u1 = u0 + du
                a0, a1 = th(day, u0), th(day, u1)
                p0, p1 = how_point(how, day, u0), how_point(how, day, u1)
                t0, t1 = p0[1] + height / 2, p1[1] + height / 2
                b0, b1 = max(BOARD, t0 - height), max(BOARD, t1 - height)
                if min(t0, t1) <= BOARD:
                    continue
                r0, r1 = radius - radial / 2, radius + radial / 2
                side = lambda amount: mix(colour, UNLIT, amount * E('HSIDE_DARK', 1.0))
                faces = [
                    ([cyl(r0, a0, t0), cyl(r1, a0, t0), cyl(r1, a1, t1), cyl(r0, a1, t1)], colour),
                    ([cyl(r1, a0, b0), cyl(r1, a1, b1), cyl(r1, a1, t1), cyl(r1, a0, t0)], side(0.25)),
                    ([cyl(r0, a0, b0), cyl(r1, a0, b0), cyl(r1, a0, t0), cyl(r0, a0, t0)], side(0.32)),
                    ([cyl(r0, a1, b1), cyl(r1, a1, b1), cyl(r1, a1, t1), cyl(r0, a1, t1)], side(0.32)),
                    ([cyl(r0, a0, b0), cyl(r0, a1, b1), cyl(r0, a1, t1), cyl(r0, a0, t0)], side(0.48)),
                    ([cyl(r0, a0, b0), cyl(r1, a0, b0), cyl(r1, a1, b1), cyl(r0, a1, b1)], side(0.60)),
                ]
                sid = 200000 + hi * 10000 + day * 100 + tile
                meta[sid] = dict(kind='how', zonly=True, day=day)
                for face, _ in faces:
                    quad(*face, None, sid)
                centre = (p0 + p1) / 2
                cubes.append(dict(day=day, filled=True, faces=faces,
                                  edge=mix(colour, UNLIT, 0.72), z=proj(centre)[2],
                                  how=how['id'], size=(radial, length, height)))

    # ---- outlines ----
    out = col.copy()
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)):
        nb_id = np.full_like(idb, -1); nb_z = np.full_like(zb, np.inf)
        ys = slice(max(0, dy), CH + min(0, dy)); yd_ = slice(max(0, -dy), CH + min(0, -dy))
        xs = slice(max(0, dx), CW + min(0, dx)); xd = slice(max(0, -dx), CW + min(0, -dx))
        nb_id[yd_, xd] = idb[ys, xs]; nb_z[yd_, xd] = zb[ys, xs]
        diff = (idb >= 0) & (nb_id != idb) & (zb <= nb_z + 1e-6)
        for y, x in zip(*np.nonzero(diff)):
            m, mb = meta[idb[y, x]], meta.get(nb_id[y, x])
            md, nd = m.get('day'), (mb or {}).get('day')
            same_day = md is not None and nd == md
            same_week = md is not None and nd is not None and nd != md and mb['week'] == m['week']
            if WHAT27 and md is not None:
                what = lambda q: q.get('part') in (1, 4)
                if nd is not None and what(m) != what(mb):          # round 7: How ribbon vs What band -- two layers
                    mb, nd, same_day, same_week = None, None, False, False
                if what(m):                                        # round 7: v27's outline rules, part 4 = v27's 2
                    pm, pb = (2 if m['part'] == 4 else 1), (None if mb is None else 2 if mb.get('part') == 4 else mb.get('part'))
                    if same_day and {pm, pb} == {0, 1}:
                        if m['div'] is not None:
                            out[y, x] = m['div']
                    elif same_day and pm == 2 and pb < 2:
                        continue
                    elif same_week:
                        if m['st'] in ('today', 'wait'):
                            out[y, x] = m['edge']
                        elif mb['st'] in ('today', 'wait'):
                            continue
                        elif pm < 2 and pb < 2 and md < nd:
                            out[y, x] = m['dd']
                    else:
                        out[y, x] = m['edge']
                    continue
            if m.get('part') == 3 and not (mb and mb.get('part') == 3 and mb['week'] == m['week']):
                out[y, x] = m['rim']                  # round 5: the What rim's outline is its own light line
            elif same_day and m['part'] == 2 and mb['part'] == 3:
                continue
            elif same_day and {m['part'], mb['part']} == {0, 1}:
                if m['div'] is not None:
                    out[y, x] = m['div']
            elif same_day and m['part'] >= 2 and mb['part'] < 2:
                continue
            elif md is not None and nd is not None and m['part'] >= 2 and mb['part'] >= 2:
                if nd != md and mb['week'] == m['week'] and md < nd:   # v31: day divider down the How wall
                    out[y, x] = m['dd']
                elif mb['week'] != m['week']:
                    out[y, x] = m['edge']
            elif same_week:
                if m['st'] in ('today', 'wait'):
                    out[y, x] = m['edge']
                elif mb['st'] in ('today', 'wait'):
                    continue
                elif m['part'] < 2 and mb['part'] < 2 and md < nd:
                    if m['st'] == 'future' and mb['st'] == 'future' and (x + y) % 2:
                        continue
                    out[y, x] = m['dd'] if m['st'] != 'future' or mb['st'] == 'future' else mb['dd']
            elif m.get('st') == 'future' and (x + y) % 2:
                continue
            else:
                out[y, x] = m['edge']
    if WK_FADE > 0 or WK_HI > 0:      # weekly C: this week's band lit, earlier weeks pushed toward 'off'
        wk_of_id = {sid: m['week'] for sid, m in meta.items() if m.get('kind') == 'cell'}
        cur = week_of(N)
        for sid, wk in wk_of_id.items():
            m_ = idb == sid
            if not m_.any():
                continue
            if wk < cur:
                out[m_] = mix(out[m_], UNLIT, wk_fade(wk))
            elif WK_HI > 0 and meta[sid]['st'] == 'filled':
                out[m_] = mix(out[m_], P_['accent'], WK_HI)
    if TODAY_UP > 0:                  # weekly E2: the earlier days' What band over today's How face, see-through
        early_w = np.isin(idb, [sid for sid, m in meta.items() if m.get('kind') == 'cell' and m['day'] < N and m['part'] in (1, 4)])
        up = early_w & np.isfinite(zf) & (zb < zf)
        out[up] = mix(out[up], cf[up], TODAY_UP)
    lit = idb >= 0
    grid = np.where(lit[..., None], out, UNLIT)
    gz = np.full((CH, CW), np.inf)
    ghosts = ghost_dots(zb)
    for x, y, z, c in ghosts:
        grid[y, x] = c
        gz[y, x] = min(gz[y, x], z)
    # ghosts behind the tube are frosted like everything else seen through two walls; they never hide the glass
    tube = glass(grid, np.minimum(np.minimum(zs, zw), zb) if E('HOW_TUBE', 0) else np.minimum(zs, zw), np.minimum(zb, gz), np.isin(idb, cells[N]['ids']))
    if E('HOW_TUBE', 0):
        glass(grid, np.minimum(np.minimum(zs, zw), zb), np.minimum(zb, gz), np.isin(idb, cells[N]['ids']), R=E('HOW_TUBE_R', 0.95), second=True)
    grid[0:2, :] = P_['accent']  # v12 status line

    # ---- dots: past How / What, today's spokes ----
    marks = []

    def vis_q(p, sids, eps=0.05):
        q = proj(p)
        x, y = int(q[0]), int(q[1])
        ok = 0 <= x < CW and 0 <= y < CH and idb[y, x] in sids and q[2] <= zb[y, x] + eps
        return q, bool(ok)

    def strip_pts(day, n, cols):
        r0, r1 = WM, WO                  # v26: What dots in the outer lane
        rows = max(1, math.ceil(n / cols))
        pts = []
        for j in range(n):
            ci, ri = j % cols, j // cols
            m = min(cols, n - ri * cols)
            u = 0.5 if m == 1 else 0.2 + 0.6 * (ci + (cols - m) / 2) / (cols - 1)
            v = (ri + 0.5) / rows
            r = r0 + (r1 - r0) * (0.14 + 0.72 * v)
            pts.append(cyl(r, th(day, u), y_at(day, u) + 0.004 + W_LIFT))
        return pts

    rung_days = {r['day'] for r in PAST_RUNGS}
    for day, ci in cells.items():
        if ci['st'] != 'filled' or day in rung_days:     # a day with rungs shows its rung ends instead
            continue
        r = REC[day]
        ks = ['decision'] * r['decision'] + ['done'] * r['done']
        for p, kd in zip(strip_pts(day, len(ks), 3), ks):
            q, ok = vis_q(p, ci['ids'][:2])
            if ok:
                marks.append((q[0], q[1], 1, mix(KIND_C[kd] * 0.9, UNLIT, (1 - WK_LINK) if wk_early(week_of(day)) else wk_fade(week_of(day)))))

    anc = dict(spokes={})
    anc['cell_px'] = int(np.isin(idb, cells[N]['ids'][:2]).sum())
    cm = np.isin(idb, cells[N]['ids'])
    if cm.any():
        ys_, xs_ = np.nonzero(cm)
        anc['cell_box'] = (xs_.min() * 2, ys_.min() * 2, xs_.max() * 2 + 2, ys_.max() * 2 + 2)
    for (mx, my, sz, c) in marks:
        cx, cy = int(mx), int(my)
        for yy in range(sz):
            for xx in range(sz):
                if 0 <= cy + yy < CH and 0 <= cx + xx < CW:
                    grid[cy + yy, cx + xx] = c
    return dict(grid=grid, idb=idb, zb=zb, zs=zs, zw=zw, zsn=zsn if TODAY_UP > 0 else zs, cells=cells, anc=anc, phase=phase,
                tube=tube, ghosts=ghosts, cubes=cubes)


# ======================= the future turns: faint dotted cell outlines on the same helix =======================
GHOST_STEP = E('GSTEP', 2.0)     # LCD cells between ghost dots along every outline (1 lit, 1 dark)
GHOST_C = mix(P_['line'], P_['bg'], 0.42)
GHOST_DAYS = E('GDAYS', 0)       # v23: none by default (v22: 4); even one day floated left of the tube as a box       # future days with a ghost: the camera follows today, so these are always the next
# steps up the front-left of the tube; from the 5th day on the helix passes behind the glass, where the band seen
# through two walls (inner and outer edges crossing in projection) read as a fold, and the end of the spiral at the
# last day poked out of the tube as a stub -- the owner's 'odd upper part' (19:13 crop)


def ghost_dots(zb):
    """Each future day = the top of its cell only: inner edge (RI), outer edge (RO) and the divider at its start
    (plus the closing divider at a week's end and at the last day), sampled on the very helix the filled cells
    use (same y_at / th, so same pitch and radius). Dots are evenly spaced in screen length along each outline,
    and a dot only shows where no solid slab is in front of it. No walls, no undersides: nothing folds."""
    curves = []
    last = min(DAYS, N + int(GHOST_DAYS))
    for day in range(N + 1, last + 1):
        k = (day - 1) % DPW
        c = mix(GHOST_C, UNLIT, 0.55 * (day - N - 1) / max(1, last - N - 1))    # fades as it climbs away
        for r in (RI, RO):
            curves.append(([cyl(r, th(day, u), y_at(day, u)) for u in np.linspace(0, 1, 40)], c))
        ends = [0.0] + ([1.0] if k == DPW - 1 or day == last else [])
        for u in ends:
            curves.append(([cyl(r, th(day, u), y_at(day, u)) for r in np.linspace(RI, RO, 16)], c))
    out, seen = [], set()
    for cv, c in curves:
        Q = np.array([proj(p) for p in cv])
        seg = np.hypot(*np.diff(Q[:, :2], axis=0).T)
        acc = np.concatenate([[0], np.cumsum(seg)])
        for L in np.arange(0, acc[-1] + 1e-6, GHOST_STEP):
            j = min(len(Q) - 2, int(np.searchsorted(acc, L, side='right') - 1))
            f = (L - acc[j]) / max(seg[j], 1e-9)
            q = Q[j] + (Q[j + 1] - Q[j]) * f
            x, y = int(q[0]), int(q[1])
            if not (0 <= x < CW and 2 <= y < CH) or (x, y) in seen:
                continue
            if q[2] > zb[y, x] + 0.05:              # a filled or today's slab is in front
                continue
            seen.add((x, y))
            out.append((x, y, q[2], c))
    return out


# ======================= the glass tube (ray-cast per LCD cell; never hides anything) =======================
TUBE_TOP = TOP
GLASS_RAY = {}
HOW_EDGE = {}


def glass(grid, zb, zall, keep, R=None, second=False):
    R = RP if R is None else R
    dx, dy, dz = _DIR[..., 0], _DIR[..., 1], _DIR[..., 2]
    a = dx * dx + dz * dz
    b = 2 * (cam[0] * dx + cam[2] * dz)
    c = cam[0] ** 2 + cam[2] ** 2 - R ** 2
    disc = b * b - 4 * a * c
    hit = disc > 0
    sq = np.sqrt(np.where(hit, disc, 0))
    z1, z2 = (-b - sq) / (2 * a), (-b + sq) / (2 * a)
    # the eye is near today's height: rays go both up and down, so clip the cylinder chord by the floor / top slab
    dys = np.where(np.abs(dy) < 1e-9, 1e-9, dy)
    ta, tb = (BOARD - cam[1]) / dys, (TUBE_TOP - cam[1]) / dys
    t_in, t_out = np.maximum(z1, np.minimum(ta, tb)), np.minimum(z2, np.maximum(ta, tb))
    inside = hit & (t_in < t_out) & (t_out > 0)
    ya, yb = cam[1] + t_in * dy, cam[1] + t_out * dy
    lo = np.minimum(ya, yb)
    y1, y2 = cam[1] + z1 * dy, cam[1] + z2 * dy
    if not second:
        GLASS_RAY.update(inside=inside, z2=np.where(inside, t_out, np.inf))
    zin = t_in                                             # depth where the ray enters the tube volume (|dir.fw| = 1)
    seen = inside & (zin < zb + 0.05)
    filled = seen & (lo < Y_TODAY)                          # the ray passes through the part up to today
    ratio = np.clip(sq / (a * 2 * R / np.sqrt(np.where(hit, a, 1))), 0, 1)   # chord / diameter
    fres = (1 - ratio) ** 2.2
    g = grid
    # frosted glass: whatever lies behind the tube's far wall is seen through two walls -- pushed toward bg
    frost = seen & np.isfinite(zall) & (zall > z2) & ~keep & (not second or E('HOW_FROST', 0) > 0)
    g[frost] = mix(g[frost], P_['bg'], 0.5)
    g[seen] = mix(g[seen], P_['accent'], 0.02 if second else 0.05)
    g[filled] = mix(g[filled], P_['accent'], 0.07)
    g[seen] = mix(g[seen], P_['accent'], ((0.04 if second else 0.12) * fres[seen])[:, None])
    # outline: the silhouette (cells inside whose neighbour is outside) + the top rim ellipse
    edge = np.zeros_like(inside)
    for oy, ox in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        sh = np.zeros_like(inside)
        sh[max(0, oy):CH + min(0, oy), max(0, ox):CW + min(0, ox)] = \
            inside[max(0, -oy):CH + min(0, -oy), max(0, -ox):CW + min(0, -ox)]
        edge |= inside & ~sh
    # the board hides the tube's foot: no outline along the bottom arc (the board ring is there)
    edge &= (np.minimum(y1, y2) > BOARD + 0.02) | (np.maximum(y1, y2) > TUBE_TOP - 0.05)
    side = edge & seen
    g[side] = GLASS_EDGE
    front = side.copy()
    rim = 0
    axis_z = proj(np.array([0, TUBE_TOP, 0]))[2]
    for k in range(480):
        a_ = 2 * math.pi * k / 480
        q = proj(cyl(R, a_, TUBE_TOP))
        x, y = int(q[0]), int(q[1])
        if 0 <= x < CW and 0 <= y < CH and q[2] < zb[y, x] + 0.05:
            g[y, x] = GLASS_EDGE if q[2] < axis_z else mix(GLASS_EDGE, P_['bg'], 0.35)
            front[y, x] = True
            rim += 1
    if second or E('HOW_TUBE', 0):
        HOW_EDGE['mask'] = front if HOW_EDGE.get('mask') is None or not second else (HOW_EDGE['mask'] | front)
    return dict(outline_cells=int(side.sum()), rim_hits=rim, tint_cells=int(seen.sum()), filled_cells=int(filled.sum()))


# ======================= the net: project, rasterise supersampled, snap to the LCD grid =======================
K = 6                             # supersamples per LCD cell side
EPS_CASE = 0.18                   # casing only shows against strands this much (world) further away


def net_screen(fr, eps=0.06):
    zs = fr['zs']
    res = []
    for th_ in strand_threads(fr['phase']):
        pts = []
        for t, xz in zip(th_['ts'], th_['xz']):
            p = world(xz, t)
            q = proj(p)
            x, y = int(q[0]), int(q[1])
            ok = 0 <= x < CW and 0 <= y < CH and q[2] <= zs[y, x] + eps
            pts.append((q[0] * 2, q[1] * 2, q[2], ok, p, t))       # css px
        res.append(dict(th_, pts=pts))
    return res


def tip_of(net, sid):
    for th_ in net:
        if th_['id'] == sid:
            return th_['pts'][-1]
    return None


THRU_A = E('THRUA', 0.6)         # v31: alpha factor for the net seen through a How wall


class Raster:
    """Supersampled depth-tested capsule raster in LCD-cell units (K samples per cell)."""

    def __init__(self, zs, zw=None):
        self.Z = np.full((CH * K, CW * K), np.inf)
        self.C = np.zeros((CH * K, CW * K, 3))
        self.T = np.zeros((CH * K, CW * K), np.int8)      # 0 none, 1 casing, 2 body
        self.A = np.zeros((CH * K, CW * K))
        self.G = np.zeros((CH * K, CW * K))
        self.ZS = np.repeat(np.repeat(zs, K, axis=0), K, axis=1)
        self.ZW = np.repeat(np.repeat(np.full_like(zs, np.inf) if zw is None else zw, K, axis=0), K, axis=1)

    def capsule(self, a, b, r, color, alpha, glow, case=0.0, eps=0.06):
        """a, b = (x, y, z) in cell units; r = radius in cells; case = casing width in cells."""
        R = r + case
        x0 = max(0, int((min(a[0], b[0]) - R) * K)); x1 = min(CW * K, int((max(a[0], b[0]) + R) * K) + 1)
        y0 = max(0, int((min(a[1], b[1]) - R) * K)); y1 = min(CH * K, int((max(a[1], b[1]) + R) * K) + 1)
        if x0 >= x1 or y0 >= y1:
            return
        X, Y = np.meshgrid((np.arange(x0, x1) + 0.5) / K, (np.arange(y0, y1) + 0.5) / K)
        vx, vy = b[0] - a[0], b[1] - a[1]
        L2 = vx * vx + vy * vy
        s = np.clip(((X - a[0]) * vx + (Y - a[1]) * vy) / L2, 0, 1) if L2 > 1e-12 else np.zeros_like(X)
        d = np.hypot(X - (a[0] + s * vx), Y - (a[1] + s * vy))
        z = a[2] + s * (b[2] - a[2])
        Zs, Z, T = self.ZS[y0:y1, x0:x1], self.Z[y0:y1, x0:x1], self.T[y0:y1, x0:x1]
        ok = z <= Zs + eps
        thru = z > self.ZW[y0:y1, x0:x1] + eps       # v31: behind a How wall -- seen through it, dimmed
        body = ok & (d <= r) & (z < Z)
        if case > 0:
            cz = z + EPS_CASE
            cm = ok & (d > r) & (d <= R) & (cz < Z) & ~body & ~thru
            Z[cm] = cz[cm]; T[cm] = 1
            self.C[y0:y1, x0:x1][cm] = CASE_C
            self.A[y0:y1, x0:x1][cm] = 0.85
            self.G[y0:y1, x0:x1][cm] = 0
        Z[body] = z[body]; T[body] = 2
        self.C[y0:y1, x0:x1][body] = color
        self.A[y0:y1, x0:x1][body] = np.where(thru, alpha * THRU_A, alpha)[body]
        self.G[y0:y1, x0:x1][body] = np.where(thru, 0.0, glow)[body]

    def composite(self, grid):
        """Per-cell coverage -> LCD cells (the snap happens here, after the smooth geometry)."""
        sh = (CH, K, CW, K)
        body = (self.T == 2).astype(float)
        case = (self.T == 1).astype(float)
        cov = body.reshape(sh).mean(axis=(1, 3))
        ccov = case.reshape(sh).mean(axis=(1, 3))
        wsum = (body * self.A).reshape(sh).sum(axis=(1, 3))
        csum = (self.C * (body * self.A)[..., None]).reshape(CH, K, CW, K, 3).sum(axis=(1, 3))
        ccol = csum / np.maximum(wsum, 1e-9)[..., None]
        aval = wsum / np.maximum(body.reshape(sh).sum(axis=(1, 3)), 1e-9)
        glow = (body * self.G).reshape(sh).mean(axis=(1, 3))
        gcol = (self.C * (body * self.G)[..., None]).reshape(CH, K, CW, K, 3).sum(axis=(1, 3))
        gw = (body * self.G).reshape(sh).sum(axis=(1, 3))
        g = grid.copy()
        g = mix(g, CASE_C, (0.85 * ccov)[..., None])
        if gw.sum() > 0:
            gb = gaussian_filter(glow, 1.4) * 2.0
            gc_ = np.stack([gaussian_filter(gcol[..., i], 1.6) for i in range(3)], -1) / \
                np.maximum(gaussian_filter(gw, 1.6), 1e-9)[..., None]
            g = mix(g, gc_, np.clip(gb, 0, 0.38)[..., None])
        a = np.clip(cov, 0, 1) ** 0.7 * aval
        return mix(g, ccol, a[..., None]), cov


def draw_net(fr, net):
    R = Raster(fr['zs'], fr['zw'])
    for th_ in net:
        pts = th_['pts']
        for i in range(len(pts) - 1):
            p, q = pts[i], pts[i + 1]
            c, w, a, gl = strand_style(th_, p[5])
            R.capsule((p[0] / 2, p[1] / 2, p[2]), (q[0] / 2, q[1] / 2, q[2]), w / 4, c, a, gl,
                      case=0.55)
        # first-confirmed seed: a slightly larger round start (not for branches, which grow out of a parent)
        if th_['kind'] == 'main' and not th_['s'].get('from') and th_['s']['start'] <= N:   # v23: born today too
            p = pts[0]
            c, w, a, gl = strand_style(th_, p[5])
            R.capsule((p[0] / 2, p[1] / 2, p[2]), (p[0] / 2, p[1] / 2, p[2]), w / 4 * 1.45, c, a, gl, case=0.4)
    # depth of the strands per LCD cell: a rung passing behind another strand hides there
    SZ = np.where(R.T == 2, R.Z, np.inf).reshape(CH, K, CW, K).min(axis=(1, 3))
    links, lines, tracks = {}, [], []
    rungs = (RUNGS if fr['phase'] == 'after' else []) + PAST_RUNGS
    on_ramp = lambda r_, d, u: cyl(r_, th(d, u), y_at(d, u) + 0.006)

    how_at = how_point
    foot = lambda r: on_ramp(r['r'], r['day'], r['u']) + np.array([0, W_LIFT, 0])   # round 5: on the lifted rim
    gin, gz2 = GLASS_RAY['inside'], GLASS_RAY['z2']

    def vis_of(pts, free=0, eps=0.07, zk='zs'):
        out, back = [], []
        for j, p in enumerate(pts):
            x, y = int(p[0]), int(p[1])
            ok = 0 <= x < CW and 0 <= y < CH and p[2] <= fr[zk][y, x] + eps
            out.append(bool(ok and (j < free or p[2] <= SZ[y, x] + 0.03)))
            back.append(bool(ok and ((gin[y, x] and p[2] > gz2[y, x]) or p[2] > fr['zw'][y, x] + eps)))   # glass / How wall
        return out, back

    def style(c, d, new):
        if d == N and (new or not ANY_NEW):
            return c, 1.0
        if d == N:
            return mix(c, P_['bg'], RUNG_OLD_MIX), RUNG_OLD_A        # the day's earlier rungs, dimmer
        return mix(c, P_['bg'], RUNG_PAST_MIX), RUNG_PAST_A

    # How tiles were depth-tested with the What cubes in render_grid.
    # rungs: What -> its How track (across the ramp) or straight to its Why strand
    for k, r in enumerate(rungs):
        s = r['s']
        d = r['day']
        t = d + r['u']
        hw = HBY.get(r['how']) if r.get('how') else None
        b3 = foot(r)
        a3 = how_at(hw, d, r['u']) if hw else world(xz_at(s, t), t)
        pts = [proj(a3 + (b3 - a3) * j / 48) for j in range(49)]
        vis, back = vis_of(pts, free=0 if hw else 6, zk='zsn' if d == N else 'zs')   # weekly E2: today's over the earlier band
        colour, alpha = style(hw['colour'] if hw else WHY_SHADE[s['shade']], d, r.get('new'))
        alpha *= wk_link(week_of(d), 1.0, RUNG_PAST_A)
        lines.append(dict(pts=pts, vis=vis, back=back, colour=colour, alpha=alpha))
        links[k] = dict(seg='what-how' if hw else 'what-why', why=s['mark'], kind=r['kind'], day=d,
                        vis=sum(vis) / len(vis), end_vis=vis[-1], colour=[int(v) for v in (hw['colour'] if hw else WHY_SHADE[s['shade']])],
                        today=d == N, new=bool(r.get('new')), how=r.get('how'), text=r['text'])
    # ... and one rung per How per day: How track -> its Why strand, from the middle of that day's Whats on it
    byhd = {}
    for r in rungs:
        if r.get('how'):
            byhd.setdefault((r['how'], r['day']), []).append(r)
    for (hid, d), rs in sorted(byhd.items()):
        h, s = HBY[hid], rs[0]['s']
        u = sum(r['u'] for r in rs) / len(rs)
        t = d + u
        a3, b3 = world(xz_at(s, t), t), how_at(h, d, u)
        pts = [proj(a3 + (b3 - a3) * j / 48) for j in range(49)]
        vis, back = vis_of(pts, free=6, zk='zsn' if d == N else 'zs')
        new = any(r.get('new') for r in rs)
        colour, alpha = style(WHY_SHADE[s['shade']], d, new)
        alpha *= wk_link(week_of(d), 1.0, RUNG_PAST_A)
        lines.append(dict(pts=pts, vis=vis, back=back, colour=colour, alpha=alpha))
        links[len(links)] = dict(seg='how-why', why=s['mark'], kind='how', day=d, vis=sum(vis) / len(vis),
                                 end_vis=vis[-1], colour=[int(v) for v in WHY_SHADE[s['shade']]], today=d == N,
                                 new=new, how=hid, text=h['label'])
    grid, cov = R.composite(fr['grid'])
    fr['rung_lines'] = lines
    fr['tracks'] = tracks
    fr['rungs'] = rungs
    return grid, cov, links


# ======================= pass 2: paint, markers, legend =======================
def paint(grid, S):
    C = 2 * S
    big = np.repeat(np.repeat(grid, C, axis=0), C, axis=1)
    mask = np.zeros((C,), bool); mask[C - 1:] = True
    big[np.tile(mask, CH), :] = P_['bg']
    big[:, np.tile(mask, CW)] = P_['bg']
    return Image.fromarray(np.clip(big, 0, 255).astype(np.uint8), 'RGB')


PILL = E('PILL', 12.0)            # marker diameter, css px (weekly: larger, the picture is the whole card)


def mark_color(s):
    return WHY_SHADE[s['shade']] if s['id'] in TODAY else mix(WHY_SHADE[s['shade']], P_['bg'], 0.3)


def cap_markers(net):
    """Each of today's Why strands wears its number as a cap just above its top tip (screen space), so the pill does
    not cover the strand. If the two caps would touch, the later one moves sideways away from the other."""
    out = {}
    for th_ in net:
        s_ = th_['s']
        live = PHASE == 'before' and s_.get('label') and not [i for i in s_.get('into', []) if i['day'] <= N]
        if th_['kind'] != 'main' or not (th_['touched'] or live):    # v23: the open card numbers every live Why
            continue
        pts = th_['pts']
        a, b = pts[-1], pts[max(0, len(pts) - 6)]
        dx, dy = a[0] - b[0], a[1] - b[1]
        L = math.hypot(dx, dy) or 1.0
        dx, dy = (dx / L, dy / L) if dy < 0 else (0.0, -1.0)
        out[th_['id']] = dict(shown=True, x=a[0] + dx * (PILL / 2 + 2.5), y=a[1] + dy * (PILL / 2 + 2.5), t=a[5],
                              slid=0.0, tip=(a[0], a[1]))
    ks = list(out)
    for i, k in enumerate(ks):
        for j in ks[:i]:
            m, o = out[k], out[j]
            d = math.hypot(m['x'] - o['x'], m['y'] - o['y'])
            if d < PILL + 2:
                sx = 1.0 if m['x'] >= o['x'] else -1.0
                m['x'] = o['x'] + sx * (PILL + 2)
    return out


def draw_pill(g, x, y, s, S, fonts, alpha=1.0):
    r = PILL / 2 * S
    c = mark_color(s)
    rgba = lambda c_, a: tuple(int(v) for v in c_) + (int(255 * max(0.0, min(1.0, a))),)
    g.ellipse([x * S - r - S, y * S - r - S, x * S + r + S, y * S + r + S], fill=rgba(P_['bg'], 0.9 * alpha))
    g.ellipse([x * S - r, y * S - r, x * S + r, y * S + r], fill=rgba(c, alpha))
    if TODAY.get(s['id'], {}).get('state') == 'corrected':
        rr = r + 1.6 * S
        g.ellipse([x * S - rr, y * S - rr, x * S + rr, y * S + rr], outline=rgba(mix(P_['ink'], P_['bg'], 0.1), alpha),
                  width=max(1, int(round(1.1 * S))))
    g.text((x * S, y * S + 0.4 * S), str(s['mark']), font=fonts['mark'], fill=rgba(P_['bg'], alpha), anchor='mm')


# ---- the text column (left): date, each Why with its pill + colour tick, its items (How / What dot first) ----
TX = E('TX', 30)                  # pill left edge, css px
TY = E('TY', 46)                  # date heading baseline-ish top
WHY_GAP, ITEM_LH = 26, 19


def fonts_at(S):
    return dict(head=_tt(int(20 * S), 6), why=_tt(int(14 * S), 6),
                item=_tt(int(12.5 * S), 2), since=_tt(int(11 * S), 2),
                mark=_tt(int(9.5 * PILL / 12 * S), 6),
                how=_tt(int(12.5 * S), 2), what=_tt(int(11.5 * S), 0),
                old=_tt(int(11 * S), 0), tag=_tt(int(10 * S), 4),
                close=_tt(int(14 * S), 4))


def rgba(c, a=1.0):
    return tuple(int(v) for v in c) + (int(255 * max(0.0, min(1.0, a))),)


def draw_rungs(img, lines, S, SS=4):
    """Fine rungs: anti-aliased polylines over the painted LCD image (supersampled SS x, then reduced), only along
    their visible runs. Width RUNG_PX at the 3x card, scaled with S; v26: a line may carry its own width 'w' (css px,
    the How tracks), and the part seen through the glass tube is drawn at BACK_A of its alpha."""
    allp = [(p[0] * 2 * S, p[1] * 2 * S) for ln in lines for p in ln['pts']]
    if not allp:
        return img
    P = np.array(allp)
    pad = 4
    x0, y0 = max(0, int(P[:, 0].min()) - pad), max(0, int(P[:, 1].min()) - pad)
    x1, y1 = min(img.size[0], int(P[:, 0].max()) + pad), min(img.size[1], int(P[:, 1].max()) + pad)
    lay = Image.new('RGBA', ((x1 - x0) * SS, (y1 - y0) * SS), (0, 0, 0, 0))
    g = ImageDraw.Draw(lay)
    for ln in sorted(lines, key=lambda l: (l.get('layer', 1), l['alpha'])):   # v24: the brightest (tonight's) on top
        w = max(1, int(round((ln['w'] * S if ln.get('w') else RUNG_PX * S / 3) * SS)))
        back = ln.get('back') or [False] * len(ln['pts'])
        run, rb = [], None
        for p, v, b in list(zip(ln['pts'], ln['vis'], back)) + [(None, False, None)]:
            if v and (rb is None or b == rb or not run):
                run.append(((p[0] * 2 * S - x0) * SS, (p[1] * 2 * S - y0) * SS))
                rb = b
                continue
            if len(run) > 1:
                for piece in dash_runs(run, ln.get('dash'), S * SS):
                    g.line(piece, fill=rgba(ln['colour'], ln['alpha'] * (BACK_A if rb else 1.0)), width=w,
                           joint='curve')
            run, rb = [], None
            if v:
                run.append(((p[0] * 2 * S - x0) * SS, (p[1] * 2 * S - y0) * SS))
                rb = b
    lay = lay.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    img.alpha_composite(lay, (x0, y0))
    return img


def draw_cubes(img, fr, S, SS=4):
    """weekly H: the What cubes, far to near, each face an anti-aliased polygon (far faces first so the near three
    cover them), the outline in the cube's edge colour; masked where a nearer solid surface hides the cube (LCD depth,
    the cube's own faces included) and frosted behind the glass tube like the hex tiles. Earlier weeks fade toward
    'off' as the band does, but stay opaque (a see-through cube reads as wire)."""
    gin, gz2 = GLASS_RAY['inside'], GLASS_RAY['z2']
    C = 2 * S
    for cb in sorted(fr['cubes'], key=lambda t: -t['z']):
        fs = [([proj(p) for p in q], col) for q, col in cb['faces']]
        P = np.array([(p[0], p[1]) for q, _ in fs for p in q])
        x0, y0 = max(0, int(P[:, 0].min()) - 1), max(0, int(P[:, 1].min()) - 1)
        x1, y1 = min(CW, int(P[:, 0].max()) + 2), min(CH, int(P[:, 1].max()) + 2)
        if x0 >= x1 or y0 >= y1:
            continue
        zz = fr['zsn' if cb['day'] == N else 'zs'][y0:y1, x0:x1]
        vis = zz >= cb['z'] - CUBE_S * 1.5
        if not vis.any():
            continue
        back = gin[y0:y1, x0:x1] & (cb['z'] > gz2[y0:y1, x0:x1])
        f = wk_fade(week_of(cb['day'])) if cb['day'] < N else 0.0     # earlier weeks: toward 'off', still solid
        fs = [(q, mix(col, UNLIT, f)) for q, col in fs]
        edge = mix(cb['edge'], UNLIT, f)
        a = np.where(vis, np.where(back, BACK_A, 1.0), 0.0) * (1.0 if cb['filled'] else CUBE_EA)
        lay = Image.new('RGBA', ((x1 - x0) * C * SS, (y1 - y0) * C * SS), (0, 0, 0, 0))
        g = ImageDraw.Draw(lay)
        ew = max(1, int(round((0.55 if cb.get('how') else 0.35) * S * SS)))
        for q, col in sorted(fs, key=lambda f: -sum(p[2] for p in f[0])):
            qq = [((p[0] - x0) * C * SS, (p[1] - y0) * C * SS) for p in q]
            g.polygon(qq, fill=rgba(col))
            g.line(qq + [qq[0]], fill=rgba(edge), width=ew, joint='curve')
        lay = lay.resize(((x1 - x0) * C, (y1 - y0) * C), Image.LANCZOS)
        A = np.asarray(lay).astype(float)
        A[..., 3] *= np.repeat(np.repeat(a, C, axis=0), C, axis=1)
        img.alpha_composite(Image.fromarray(A.astype(np.uint8), 'RGBA'), (x0 * C, y0 * C))
    return img


BACK_A = E('BACKA', 0.4)          # v26: alpha factor for track / rung parts seen through the glass tube


def how_style(h, used, today):
    """(colour, alpha, width css) of a How line -- one source for the ramp track and the tree swatch (v27).
    The Why's hue, lighter than the Why strand: a little toward ink on used days, toward bg on the days between."""
    c = WHY_SHADE[h['shade']]
    if used:
        return (mix(c, P_['ink'], 0.12), 0.95, HOW_W) if today else (mix(c, P_['bg'], 0.15), 0.85, HOW_W)
    return mix(c, P_['bg'], 0.35), 0.55, HOW_W_OFF


def dash_runs(run, dash, scale):
    """Split a polyline (device px) into dash pieces; dash = (on, off) in css px, scale = device px per css px."""
    if not dash or len(run) < 2:
        return [run]
    on, off = dash[0] * scale, dash[1] * scale
    out, cur, pos = [], [run[0]], 0.0          # pos = distance into the current on+off period
    for a, b in zip(run, run[1:]):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        t = 0.0
        while t < L:
            lim = on if pos < on else on + off
            step = min(L - t, lim - pos)
            t += step; pos += step
            q = (a[0] + (b[0] - a[0]) * t / L, a[1] + (b[1] - a[1]) * t / L) if L else b
            if cur is not None:
                cur.append(q)
            if pos >= on + off - 1e-9:
                pos = 0.0; cur = [q]
            elif pos >= on - 1e-9 and cur is not None:
                out.append(cur); cur = None
    if cur and len(cur) > 1:
        out.append(cur)
    return [r for r in out if len(r) > 1]


def draw_tag(img, S, text):
    """Small corner tag (top right) marking an illustrative card."""
    g = ImageDraw.Draw(img)
    f = _tt(int(E('TAGF', 11) * S), 6)
    bb = g.textbbox((0, 0), text, font=f)
    tw, th_ = bb[2] - bb[0], bb[3] - bb[1]
    x1, y0 = W * S - 14 * S, 12 * S
    x0, y1 = x1 - tw - 14 * S, y0 + th_ + 10 * S
    g.rounded_rectangle([x0, y0, x1, y1], radius=5 * S, fill=rgba(P_['bg'], 0.85), outline=rgba(P_['alert']),
                        width=max(1, int(S)))
    g.text(((x0 + x1) / 2, (y0 + y1) / 2), text, font=f, fill=rgba(P_['alert']), anchor='mm')
    return [v / S for v in (x0, y0, x1, y1)]


def draw_frame(fr, S, check=None):
    grid, cov, links = draw_net(fr, fr['net'])
    img = paint(grid, S).convert('RGBA')
    tr = [dict(t, layer=0) for t in fr['tracks']]
    if tr:  # Tracks below the cube faces; rungs land on the tops.
        img = draw_rungs(img, tr, S)
    img = draw_rungs(draw_cubes(img, fr, S), fr['rung_lines'], S)
    if HOW_EDGE.get('mask') is not None:
        eg = ImageDraw.Draw(img)
        cs = W / CW * S
        for y, x in zip(*np.nonzero(HOW_EDGE['mask'])):
            eg.rectangle([x * cs, y * cs, (x + 1) * cs - 1, (y + 1) * cs - 1],
                         fill=rgba(GLASS_EDGE))
    over = Image.new('RGBA', img.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(over)
    boxes = []
    markers = cap_markers(fr['net'])
    for s in LABELLED:
        m = markers.get(s['id'])
        if m and m['shown']:
            draw_pill(g, m['x'], m['y'], s, S, fonts_at(S))
            boxes.append(('mark' + str(s['mark']), [m['x'] - PILL / 2, m['y'] - PILL / 2, m['x'] + PILL / 2,
                                                     m['y'] + PILL / 2]))
    img = Image.alpha_composite(img, over)
    if E('FIGONLY', 0):           # round 5 comparison crops: the cylinder figure only
        if check is not None:
            check.update(boxes=boxes + [('txt-bottom', [0, 0, 0, 0])], markers=markers, links=links, cov=cov)
        return img.convert('RGB')
    boxes.append(('txt-bottom', [0, 0, 0, 0]))
    if WK_CORNER:
        boxes += draw_corner(img, S)
    if DATA.get('tag'):
        boxes.append(('tag', draw_tag(img, S, DATA['tag'])))
    draw_overflow(img, S)
    img = img.convert('RGB')
    if check is not None:
        check.update(boxes=boxes, markers=markers, links=links, cov=cov)
    return img


def draw_corner(img, S):
    """Weekly B: the week's date range (top left, small) and one row of the Why colour dots with their numbers."""
    g = ImageDraw.Draw(img)
    f = _tt(int(15 * S), 2)
    x, y = WK_CX, WK_CY
    g.text((x * S, y * S), WK_CORNER, font=f, fill=rgba(P_['muted']), anchor='lt')
    bb = g.textbbox((x * S, y * S), WK_CORNER, font=f, anchor='lt')
    out = [('txt-range', [v / S for v in bb])]
    r_ = 9.0
    fm = _tt(int(11.5 * S), 6)
    cy = bb[3] / S + 10 + r_
    cx = x + r_
    for s in sorted([s for s in SL if s.get('label') and alive(s)], key=lambda s: s['mark']):
        c = mark_color(s)
        g.ellipse([(cx - r_) * S, (cy - r_) * S, (cx + r_) * S, (cy + r_) * S], fill=rgba(c))
        g.text((cx * S, (cy + 0.4) * S), str(s['mark']), font=fm, fill=rgba(P_['bg']), anchor='mm')
        out.append(('txt-legend%d' % s['mark'], [cx - r_, cy - r_, cx + r_, cy + r_]))
        cx += 2 * r_ + 7
    return out


def frame(n, phase, tc=None):
    set_day(n, phase)
    set_camera(n + 0.5 if tc is None else tc)
    fr = render_grid(phase)
    fr['net'] = net_screen(fr)
    return fr


def draw_overflow(img, scale):
    if not OVERFLOW:
        return
    g = ImageDraw.Draw(img)
    font = _tt(11 * scale, 2)
    for row, text in enumerate(OVERFLOW):
        g.text((22 * scale, (20 + row * 15) * scale), text,
               font=font, fill=rgba(P_['muted']), anchor='lt')


def render_image():
    return draw_frame(frame(DATA['day'], 'after'), 3)
