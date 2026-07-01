#!/usr/bin/env python3
"""
diet-log card generator (dark theme).
Usage: python3 card.py '<json>'

Input JSON (all keys optional; if meals/intake_kcal are absent, the card reads
that date's meals+workouts straight from lifekit.db):
{
  "date": "2026-06-25",              // YYYY-MM-DD for lifekit.db lookup (default: today KST)
  "intake_kcal": {"current": 772},   // omit goal -> auto from body stats (config table)
  "protein":     {"current": 37.0},  // omit goal -> stats.protein_g fixed
  "carbs":       {"current": 72.5},  // omit goal -> auto from total target
  "fat":         {"current": 41.2},  // omit goal -> total x fat_ratio / 9 auto
  "burn_kcal":   {"current": 172, "detail": "..."},  // optional; hidden if absent
  "meals": [
    {"type": "아침", "name": "...", "protein": 14.7, "carbs": 26.2, "fat": 13.3}
  ],
  "output": "files/outbox/diet_card.png"   // optional, default: /tmp/diet_card.png
}

Concept:
  Visualizes "the more you exercise, the larger the recommended intake bar."
  Total track (= eff_goal) = [base_goal segment] + [exercise-added segment],
  with intake (coral) overlaid from left=0.

Output: prints the PNG path to stdout.

Rendering deps: matplotlib. This script is meant to be launched by a python that
has matplotlib installed. The SKILL.md documents resolving that interpreter via
RENDER_PYTHON (env) with fallbacks. If matplotlib is unavailable, this script
exits with a clear message so callers can gracefully skip the card.
"""

import sys
import json
import os
import re
import datetime

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
except ImportError as e:
    # Graceful skip contract: non-zero exit + a clear stderr message. Callers
    # (SKILL.md flow) detect this and skip the card without failing the log.
    sys.stderr.write(
        "card.py: matplotlib not available in this interpreter -- skipping card "
        "render. Install matplotlib or point RENDER_PYTHON at a venv that has it. "
        f"({e})\n")
    sys.exit(3)

# -- lifekit.py core (structured lane) as single source of truth --------------
# The skill lives at <PROJECT_ROOT>/.claude/skills/diet-log/, and lifekit lives
# at <PROJECT_ROOT>/database/. That is three levels up from this file's dir.
# PROJECT_ROOT-relative and path-independent (no absolute/parent-tree paths).
_DATA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'database'))
if _DATA_DIR not in sys.path:
    sys.path.insert(0, _DATA_DIR)
import lifekit as life

# -- Fonts (path-independent, graceful) ---------------------------------------
# Preference order:
#   1) Bundled TTFs shipped with the skill (fonts/*.ttf) -- fully portable.
#   2) Extract from a system TTC found via a candidate list (env DIET_CARD_TTC
#      first, then common macOS locations) -- best effort only.
#   3) Fall back to matplotlib's default font (CJK may not render, but the card
#      still draws). Never hardcode an absolute font path as a hard dependency.
FONT_DIR  = os.path.join(os.path.dirname(__file__), 'fonts')
FONT_MED  = os.path.join(FONT_DIR, 'ASDGN_Medium.ttf')
FONT_XBLD = os.path.join(FONT_DIR, 'ASDGN_ExtraBold.ttf')

# Candidate system TTC files to source the fonts from if the bundled TTFs are
# missing. Env override wins; then common CJK-capable system fonts.
_TTC_CANDIDATES = [
    os.environ.get('DIET_CARD_TTC', ''),
    '/System/Library/Fonts/AppleSDGothicNeo.ttc',
]


def _try_extract_from_ttc():
    """Best-effort: extract Medium/ExtraBold faces from a system TTC into fonts/.
    Returns True on success. Silent no-op if no TTC/fontTools available."""
    ttc_path = next((p for p in _TTC_CANDIDATES if p and os.path.exists(p)), None)
    if not ttc_path:
        return False
    try:
        from fontTools.ttLib import TTCollection
    except ImportError:
        return False
    try:
        os.makedirs(FONT_DIR, exist_ok=True)
        ttc = TTCollection(ttc_path)
        # AppleSDGothicNeo.ttc: index 2 = Medium, 14 = ExtraBold.
        ttc.fonts[2].save(FONT_MED)
        ttc.fonts[14].save(FONT_XBLD)
        return True
    except Exception:
        return False


def _resolve_fonts():
    """Return (prop, bold_prop). Uses bundled TTFs if present, else tries to
    extract from a system TTC, else falls back to matplotlib defaults."""
    have = os.path.exists(FONT_MED) and os.path.exists(FONT_XBLD)
    if not have:
        have = _try_extract_from_ttc()
    if have:
        fm.fontManager.addfont(FONT_MED)
        fm.fontManager.addfont(FONT_XBLD)
        return fm.FontProperties(fname=FONT_MED), fm.FontProperties(fname=FONT_XBLD)
    # Fallback: default font (CJK glyphs may be missing but the card still draws).
    sys.stderr.write(
        "card.py: skill fonts not found and no usable system TTC -- falling back "
        "to matplotlib default font (CJK text may not render).\n")
    return fm.FontProperties(), fm.FontProperties(weight='bold')


prop, bold_prop = _resolve_fonts()

# -- Body stats / targets model (lifekit.py core; no local reimplementation) --
load_body_stats     = life.load_body_stats
compute_targets     = life.compute_targets
compute_macro_goals = life.compute_macro_goals

# Korean weekday (Mon=0 .. Sun=6)
_WDAY_KO = ['월', '화', '수', '목', '금', '토', '일']


def _resolve_date(date_in):
    """From input date build the lifekit.db lookup 'YYYY-MM-DD' and the display
    'YYYY.MM.DD <weekday>'. 'YYYY-MM-DD' -> that date; otherwise -> today."""
    iso = None
    if date_in:
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_in.strip())
        if m:
            iso = date_in.strip()
    if iso is None:
        iso = datetime.date.today().isoformat()
    y, mo, da = iso.split('-')
    wd = datetime.date(int(y), int(mo), int(da)).weekday()
    display = f"{y}.{mo}.{da} {_WDAY_KO[wd]}"
    if date_in and not re.match(r'^\d{4}-\d{2}-\d{2}$', date_in.strip()):
        display = date_in
    return iso, display


def load_from_life_db(iso_date):
    """Read that date's meals+workouts as a card dict fragment (via lifekit)."""
    return life.load_card_data(iso_date)


# -- Colors (dark theme) ------------------------------------------------------
BG          = '#13132B'
INK         = '#FFFFFF'
INK_SOFT    = '#C4D4FF'
INK_VALUE   = '#DDDDEE'
TRACK_BASE  = '#2A2A50'
BAR_EXERCISE_SEG = '#1C1C38'
BURN_ACCENT = '#FF9F43'
INTAKE_FILL = '#FF6B6B'
SEP_LINE    = '#2A2A55'

MACRO_COLORS = {
    '단백질':   '#4ECDC4',
    '탄수화물': '#FFD166',
    '지방':     '#A8E6CF',
}
MEAL_COLORS = {
    '아침': '#FF6B6B',
    '점심': '#4ECDC4',
    '저녁': '#A8E6CF',
    '간식': '#FFD166',
    '운동': '#FF9F43',
}


def draw_card(d: dict, out_path: str):
    date    = d.get('date', '')
    intake  = d.get('intake_kcal', {})
    protein = d.get('protein', {})
    carbs   = d.get('carbs', {})
    fat     = d.get('fat', {})
    burn    = d.get('burn_kcal', None)
    meals   = d.get('meals', [])

    # -- daily total target = BMR + NEAT - deficit + exercise burn --
    has_burn = bool(burn and burn.get('current', 0) > 0)
    burn_amt = burn['current'] if has_burn else 0
    stats    = load_body_stats()
    t        = compute_targets(stats, exercise_kcal=burn_amt)
    if intake.get('goal'):
        base_goal = round(intake['goal'])
        eff_goal  = round(base_goal + burn_amt)
    else:
        base_goal = t['base_goal']
        eff_goal  = t['eff_goal']
    g = compute_macro_goals(eff_goal, stats)

    current_intake = intake.get('current', 0)

    formula = (f"기초 {t['bmr']} + 활동 {t['neat']} - 적자 {t['deficit']}"
               + (f" + 운동 {round(burn_amt)}" if has_burn else ''))

    # -- figure (height sized to content) --
    n_meals     = len(meals)
    fig_w       = 13.0

    _bar_top_in  = 2.05
    _bar_h_in    = 0.62
    _macro_top   = _bar_top_in + _bar_h_in + 1.25
    _mbar_top    = _macro_top + 0.55
    _mbar_h_in   = 0.22
    _sep_top_in  = _mbar_top + _mbar_h_in + 1.05
    _meals_start = _sep_top_in + 0.65
    _meal_row_in = 0.85
    _last_row_in = _meals_start + (max(n_meals, 1) - 1) * _meal_row_in if n_meals else _sep_top_in
    BOTTOM_PAD_IN = 0.55
    fig_h = _last_row_in + BOTTOM_PAD_IN
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=BG)
    fig.patch.set_facecolor(BG)

    L, R = 0.045, 0.955
    H = fig_h

    def y(px_from_top):
        return 1.0 - (px_from_top / H)

    # -- header: title + date --
    fig.text(L, y(0.55), '오늘 식단 현황', color=INK,
             fontsize=30, fontproperties=bold_prop, va='center', ha='left')
    if date:
        fig.text(0.34, y(0.55), date, color=INK_SOFT,
                 fontsize=23, fontproperties=bold_prop, va='center', ha='left')

    # -- recommended intake formula line --
    fig.text(L, y(1.55),
             f"권장 섭취 칼로리: {eff_goal}kcal = {formula}",
             color=INK, fontsize=18, fontproperties=prop, va='center', ha='left')

    # -- intake bar (exercise segment split) --
    bar_left   = L
    bar_right  = R
    bar_width  = bar_right - bar_left
    bar_top_in = 2.05
    bar_h_in   = 0.62
    bar_y0 = y(bar_top_in + bar_h_in)
    bar_h  = bar_h_in / H

    eff = max(eff_goal, 1)
    base_frac = min(base_goal / eff, 1.0)
    burn_frac = max(0.0, 1.0 - base_frac)
    intake_frac = min(current_intake / eff, 1.0)

    fig.patches.append(plt.Rectangle(
        (bar_left, bar_y0), bar_width * base_frac, bar_h,
        transform=fig.transFigure, facecolor=TRACK_BASE, edgecolor='none', zorder=1))
    if burn_frac > 0:
        fig.patches.append(plt.Rectangle(
            (bar_left + bar_width * base_frac, bar_y0), bar_width * burn_frac, bar_h,
            transform=fig.transFigure, facecolor=BAR_EXERCISE_SEG, edgecolor='none', zorder=1))
    fig.patches.append(plt.Rectangle(
        (bar_left, bar_y0), bar_width * intake_frac, bar_h,
        transform=fig.transFigure, facecolor=INTAKE_FILL, edgecolor='none', zorder=2))

    # -- remaining / over label --
    remaining = eff_goal - current_intake
    if remaining > 0:
        remain_txt = f"남은 {remaining:,.0f} kcal"
    else:
        remain_txt = f"초과 {abs(remaining):,.0f} kcal"
    remain_start = bar_left + bar_width * intake_frac
    label_x = (remain_start + bar_right) / 2
    label_x = min(max(label_x, bar_left + 0.03), bar_right - 0.03)
    remain_y = bar_y0 + bar_h / 2
    fig.text(label_x, remain_y, remain_txt, color=INK_VALUE, fontsize=18,
             fontproperties=bold_prop, va='center', ha='center', zorder=3)

    # -- labels under bar: intake (left), burn (right) --
    label_y = y(bar_top_in + bar_h_in + 0.45)
    fig.text(bar_left, label_y, f"섭취 {current_intake:.0f}", color=INK,
             fontsize=22, fontproperties=bold_prop, va='center', ha='left')
    if has_burn:
        detail = burn.get('detail') or '운동'
        fig.text(bar_right, label_y, f"{detail} 소모 {round(burn_amt)}",
                 color=BURN_ACCENT, fontsize=20, fontproperties=bold_prop,
                 va='center', ha='right')

    # -- macro 3 columns --
    macro_top_in = bar_top_in + bar_h_in + 1.25
    macros = [
        ('단백질',   protein.get('current', 0), protein.get('goal') or g['protein'], None),
        ('탄수화물', carbs.get('current', 0),   carbs.get('goal')   or g['carb'], carbs.get('sugar')),
        ('지방',     fat.get('current', 0),     fat.get('goal')     or g['fat'], None),
    ]
    col_w = (R - L) / 3
    centers = [L + col_w * (i + 0.5) for i in range(3)]
    col_bar_w = col_w * 0.72

    name_y = y(macro_top_in)
    mbar_top_in = macro_top_in + 0.55
    mbar_h_in   = 0.22
    mbar_y0 = y(mbar_top_in + mbar_h_in)
    mbar_h  = mbar_h_in / H
    mval_y  = y(mbar_top_in + mbar_h_in + 0.55)
    msug_y  = y(mbar_top_in + mbar_h_in + 0.85)

    for cx, (mname, mcur, mgoal, msugar) in zip(centers, macros):
        mcolor = MACRO_COLORS.get(mname, '#888899')
        fig.text(cx, name_y, mname, color=INK, fontsize=22,
                 fontproperties=bold_prop, va='center', ha='center')
        mb_left = cx - col_bar_w / 2
        frac = min(mcur / mgoal, 1.0) if mgoal else 0.0
        fig.patches.append(plt.Rectangle(
            (mb_left, mbar_y0), col_bar_w, mbar_h,
            transform=fig.transFigure, facecolor=TRACK_BASE, edgecolor='none', zorder=1))
        fig.patches.append(plt.Rectangle(
            (mb_left, mbar_y0), col_bar_w * frac, mbar_h,
            transform=fig.transFigure, facecolor=mcolor, edgecolor='none', zorder=2))
        fig.text(cx, mval_y, f"{mcur:.0f} / {mgoal:.0f} g", color=INK_VALUE,
                 fontsize=21, fontproperties=bold_prop, va='center', ha='center')
        if msugar is not None:
            fig.text(cx, msug_y, f"당 {msugar:.0f}g", color=mcolor,
                     fontsize=16, fontproperties=bold_prop, va='center', ha='center')

    # -- separator --
    sep_top_in = mbar_top_in + mbar_h_in + 1.05
    sep_y = y(sep_top_in)
    fig.add_artist(plt.Line2D([L, R], [sep_y, sep_y],
                   color=SEP_LINE, linewidth=1.5, transform=fig.transFigure))

    # -- meal list --
    meal_row_in = 0.85
    cur_in = sep_top_in + 0.65
    for m in meals:
        mtype = m.get('type', '')
        mname = m.get('name', '')
        mcolor = MEAL_COLORS.get(mtype, '#777788')
        ry = y(cur_in)

        fig.text(L, ry, mtype, color=mcolor, fontsize=21,
                 fontproperties=bold_prop, va='center', ha='left')
        fig.text(L + 0.075, ry, mname, color=INK, fontsize=21,
                 fontproperties=prop, va='center', ha='left')

        has_macro = any(k in m and m[k] is not None for k in ('protein', 'carbs', 'fat'))
        if has_macro:
            p = m.get('protein')
            c = m.get('carbs')
            f_ = m.get('fat')
            parts = []
            if p is not None:
                parts.append(f"단 {p:.0f}")
            if c is not None:
                s = m.get('sugar')
                if s is not None and s > 0:
                    parts.append(f"탄 {c:.0f} (당 {s:.0f})")
                else:
                    parts.append(f"탄 {c:.0f}")
            if f_ is not None:
                parts.append(f"지 {f_:.0f}")
            macro_txt = '   '.join(parts) + ' g'
            fig.text(R, ry, macro_txt, color=INK_VALUE, fontsize=18,
                     fontproperties=bold_prop, va='center', ha='right')

        cur_in += meal_row_in

    plt.savefig(out_path, dpi=150, facecolor=BG, edgecolor='none')
    plt.close()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: card.py \'<json>\'', file=sys.stderr)
        sys.exit(1)

    data     = json.loads(sys.argv[1])
    out_path = data.get('output', '/tmp/diet_card.png')

    # Data source: if meals/intake absent, auto-aggregate from lifekit.db.
    iso_date, display_date = _resolve_date(data.get('date'))
    if 'meals' not in data and 'intake_kcal' not in data:
        loaded = load_from_life_db(iso_date)
        for k, v in loaded.items():
            data.setdefault(k, v)
        data['date'] = display_date

    draw_card(data, out_path)
    print(out_path)
