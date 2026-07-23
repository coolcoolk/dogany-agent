#!/usr/bin/env python3
"""
Morning brief weather card generator (dark theme).
Fetches live weather + air quality from Open-Meteo (no API key required) and
renders a 2-block PNG card: unified weather+air-quality / daily quote.

Usage:
    morning_brief_card.py --output <path> [--lat <lat> --lon <lon>]
    morning_brief_card.py --output <path>  # defaults to Seoul coords

Exit codes:
    0  success -- PNG written to --output path
    1  fatal error (missing matplotlib, network failure, render error)
"""

import sys
import os
import argparse
import datetime
import json
import math
import urllib.request

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import matplotlib.font_manager as fm
import numpy as np

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser(description='Morning brief weather card renderer')
_parser.add_argument('--output', required=True, help='Output PNG path (required)')
_parser.add_argument('--lat', type=float, default=37.5665, help='Latitude (default: Seoul)')
_parser.add_argument('--lon', type=float, default=126.9780, help='Longitude (default: Seoul)')
_args = _parser.parse_args()

LAT = _args.lat
LON = _args.lon
OUT_PATH = _args.output

# ---------------------------------------------------------------------------
# Design tokens (DGN-376 T3): palette + fonts come from the single token canon
# (routines/lib/design_tokens.py), not hardcoded here. This script sits in
# routines/bundle/, so routines/lib/ is a sibling -- resolve it by walking up
# and importing the module by file path (no package install assumed).
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_design_tokens():
    import importlib.util
    parent = _SCRIPT_DIR
    for _ in range(6):
        cand = os.path.join(parent, 'routines', 'lib', 'design_tokens.py')
        if os.path.isfile(cand):
            spec = importlib.util.spec_from_file_location('design_tokens', cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    raise ImportError('design_tokens.py not found from %s' % _SCRIPT_DIR)

_tokens = _load_design_tokens()
_T = _tokens.theme('card-dark')
_BRAND = _tokens.BRAND

# ---------------------------------------------------------------------------
# Font resolution (via token FONTS + shared walk-up resolver)
# ---------------------------------------------------------------------------
FONT_MED, FONT_XBLD = _tokens.font_paths(_SCRIPT_DIR)

def _resolve_fonts():
    if FONT_MED and FONT_XBLD and os.path.exists(FONT_MED) and os.path.exists(FONT_XBLD):
        fm.fontManager.addfont(FONT_MED)
        fm.fontManager.addfont(FONT_XBLD)
        return fm.FontProperties(fname=FONT_MED), fm.FontProperties(fname=FONT_XBLD)
    sys.stderr.write("morning_brief_card: custom fonts not found, falling back to default.\n")
    return fm.FontProperties(), fm.FontProperties(weight='bold')

prop, bold_prop = _resolve_fonts()

# ---------------------------------------------------------------------------
# Color palette (dark theme) -- derived from the card-dark token theme.
# Values are pixel-identical to the former hardcoded block by construction
# (Layer A was canonicalized from this very palette, DGN-376 T1).
# ---------------------------------------------------------------------------
BG         = _T['bg']
INK        = _T['text']
INK_SOFT   = _T['muted']
INK_VALUE  = _BRAND['ink-value']
TRACK_BASE = _BRAND['navy-track']
SEP_LINE   = _BRAND['navy-line']
ACCENT     = _T['accent']
WARM       = _T['yellow']
QUOTE_BG   = _BRAND['navy-quote']
PANEL_BG   = _T['surface']
PANEL_EDGE = _BRAND['navy-line']

# Air quality grade display colors (grade scale tokens)
AQ_COLORS = {
    '좋음':     _BRAND['grade-good'],   # good
    '보통':     _BRAND['grade-ok'],     # normal
    '나쁨':     _BRAND['grade-bad'],    # bad
    '매우나쁨': _BRAND['grade-vbad'],   # very bad
}
# Readable mapping for internal use
_AQ_GOOD    = '좋음'
_AQ_NORMAL  = '보통'
_AQ_BAD     = '나쁨'
_AQ_VBAD    = '매우나쁨'

# ---------------------------------------------------------------------------
# WMO weather code -> display text + short symbol
# Display text is Korean (user-facing content).
# ---------------------------------------------------------------------------
WMO_KO = {
    0:  ('맑음',        'SUN'),
    1:  ('구름 조금', 'FEW'),
    2:  ('구름 많음', 'CLD'),
    3:  ('흐림',        'OVC'),
    45: ('안개',        'FOG'),
    48: ('안개',        'FOG'),
    51: ('이슬비',  'DZL'),
    53: ('이슬비',  'DZL'),
    55: ('이슬비',  'DZL'),
    61: ('비',              'RAN'),
    63: ('비',              'RAN'),
    65: ('강한 비', 'HVY'),
    71: ('눈',              'SNW'),
    73: ('눈',              'SNW'),
    75: ('강한 눈', 'SNW'),
    80: ('소나기',  'SHW'),
    81: ('소나기',  'SHW'),
    82: ('강한 소나기', 'SHW'),
    95: ('뇌우',       'THN'),
    96: ('우박 뇌우', 'THN'),
    99: ('우박 뇌우', 'THN'),
}

def wmo_info(code):
    """Return (display_text, short_symbol) for a WMO weather code."""
    return WMO_KO.get(code, (f'코드{code}', '???'))

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'morning-brief-card/1.0'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def fetch_weather():
    """Fetch current + hourly + daily weather from Open-Meteo."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&current=temperature_2m,weather_code,apparent_temperature"
        f"&hourly=temperature_2m,weather_code,precipitation_probability"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&timezone=Asia%2FSeoul&forecast_days=1"
    )
    return fetch_json(url)

def fetch_air_quality():
    """Fetch current + hourly PM10 / PM2.5 from Open-Meteo Air Quality API."""
    url = (
        f"https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={LAT}&longitude={LON}"
        f"&current=pm10,pm2_5"
        f"&hourly=pm10,pm2_5"
        f"&timezone=Asia%2FSeoul&forecast_days=1"
    )
    return fetch_json(url)

def pm10_grade(v):
    """Korean air quality grade for PM10 (ug/m3)."""
    if v < 30:  return _AQ_GOOD
    if v < 80:  return _AQ_NORMAL
    if v < 150: return _AQ_BAD
    return _AQ_VBAD

def pm25_grade(v):
    """Korean air quality grade for PM2.5 (ug/m3)."""
    if v < 15:  return _AQ_GOOD
    if v < 35:  return _AQ_NORMAL
    if v < 75:  return _AQ_BAD
    return _AQ_VBAD

def parse_weather(data):
    """Extract values needed from the Open-Meteo weather response."""
    cur = data['current']
    daily = data['daily']
    hourly = data['hourly']

    temp_now   = cur['temperature_2m']
    feels_like = cur.get('apparent_temperature', temp_now)
    code_now   = cur['weather_code']
    temp_max   = daily['temperature_2m_max'][0]
    temp_min   = daily['temperature_2m_min'][0]

    # Fixed display time slots
    TARGET_HOURS = [6, 9, 12, 15, 18, 21]
    times = hourly['time']  # list of "YYYY-MM-DDTHH:00"
    slots = []
    for target_h in TARGET_HOURS:
        idx = None
        for i, t in enumerate(times):
            if int(t[11:13]) == target_h:
                idx = i
                break
        if idx is not None:
            slots.append({
                'time':   f"{target_h:02d}:00",
                'temp':   hourly['temperature_2m'][idx],
                'code':   hourly['weather_code'][idx],
                'precip': hourly['precipitation_probability'][idx],
            })

    return {
        'temp_now':   temp_now,
        'feels_like': feels_like,
        'code_now':   code_now,
        'temp_max':   temp_max,
        'temp_min':   temp_min,
        'slots':      slots,
    }

def parse_air_quality(aq_data):
    """Extract current + 6-slot PM10/PM2.5 from Open-Meteo air quality response."""
    current  = aq_data.get('current', {})
    pm10_now = current.get('pm10',  0.0) or 0.0
    pm25_now = current.get('pm2_5', 0.0) or 0.0

    hourly  = aq_data.get('hourly', {})
    times_h = hourly.get('time', [])
    pm10_h  = hourly.get('pm10', [])
    pm25_h  = hourly.get('pm2_5', [])

    TARGET_HOURS = [6, 9, 12, 15, 18, 21]
    slots = []
    for target_h in TARGET_HOURS:
        idx = None
        for i, t in enumerate(times_h):
            if int(t[11:13]) == target_h:
                idx = i
                break
        if idx is not None:
            pm10_val = pm10_h[idx] if idx < len(pm10_h) else 0.0
            pm25_val = pm25_h[idx] if idx < len(pm25_h) else 0.0
            pm10_val = pm10_val or 0.0
            pm25_val = pm25_val or 0.0
            slots.append({
                'time': f"{target_h:02d}:00",
                'pm10': pm10_val,
                'pm25': pm25_val,
            })

    return {
        'pm10_now': pm10_now,
        'pm25_now': pm25_now,
        'slots':    slots,
    }

# ---------------------------------------------------------------------------
# Daily quotes (Korean motivational quotes, date-cycled)
# Content strings -- Korean is intentional for user display.
# ---------------------------------------------------------------------------
QUOTES = [
    ('오늘 할 수 있는 일을 내일로 미루지 마라.',
     '벤저민 프랭클린'),
    ('뭔가를 잃는 것은 더 나은 것이 예정되어 있기 때문입니다.',
     '맨디 헤일'),
    ('성공은 준비가 기회를 만났을 때 일어납니다.',
     '세네카'),
    ('가장 어두운 밤도 결국 끝나고 해는 떠오릅니다.',
     '빅토르 위고'),
    ('당신이 할 수 있다고 믿으면, 이미 반은 된 것입니다.',
     '시어도어 루스벨트'),
    ('작은 일에도 최선을 다하라. 그것이 큰 일을 이루는 길이다.',
     '공자'),
    ('우리가 두려워해야 할 유일한 것은 두려움 그 자체입니다.',
     '프랭클린 루스벨트'),
    ('천 리 길도 한 걸음부터.',
     '노자'),
    ('시작이 반이다.',
     '아리스토텔레스'),
    ('더 나은 세상을 만들고 싶다면 먼저 자기 자신을 바꿔라.',
     '마하트마 간디'),
]

def get_quote():
    """Return (quote_text, author) for today (date-cycled)."""
    today = datetime.date.today()
    idx = today.timetuple().tm_yday % len(QUOTES)
    return QUOTES[idx]

# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------
# Korean weekday abbreviations for the card header (user-facing content)
WDAY_KO = ['월', '화', '수', '목', '금', '토', '일']

def draw_weather_symbol(fig, cx, cy, code, size=0.022):
    """Draw a weather icon at figure-fraction position (cx, cy)."""
    ko, sym = wmo_info(code)

    def add_circle(x, y, r, color, z=5):
        fig.add_artist(mpatches.Circle((x, y), r,
            transform=fig.transFigure, facecolor=color, edgecolor='none', zorder=z))

    def add_ellipse(x, y, w, h, color, angle=0, z=5):
        fig.add_artist(mpatches.Ellipse((x, y), w, h, angle=angle,
            transform=fig.transFigure, facecolor=color, edgecolor='none', zorder=z))

    def draw_sun(x, y, s, color='#FFD166', z=5):
        add_circle(x, y, s * 0.45, color, z)
        for a in range(0, 360, 45):
            rad = math.radians(a)
            spike_cx = x + math.cos(rad) * s * 0.75
            spike_cy = y + math.sin(rad) * s * 0.75
            add_ellipse(spike_cx, spike_cy, s * 0.12, s * 0.30, color, angle=a, z=z)

    def draw_cloud(x, y, s, color='#C4D4FF', z=6):
        add_ellipse(x,          y,          s * 1.6, s * 0.9, color, z=z)
        add_ellipse(x - s*0.5,  y - s*0.1,  s * 1.0, s * 0.7, color, z=z)
        add_ellipse(x + s*0.45, y - s*0.1,  s * 1.0, s * 0.7, color, z=z)

    def draw_rain_drops(x, y, s, color='#4ECDC4', n=3, z=6):
        offsets = ([-s*0.5, 0, s*0.5] if n == 3
                   else [-s*0.6, -s*0.2, s*0.2, s*0.6])
        for ox in offsets[:n]:
            add_ellipse(x + ox, y - s*0.75, s*0.12, s*0.28, color, z=z)

    if sym == 'SUN':
        draw_sun(cx, cy, size)
    elif sym == 'FEW':
        draw_sun(cx + size*0.25, cy + size*0.3, size*0.55, z=5)
        draw_cloud(cx - size*0.1, cy - size*0.1, size*0.65, color='#DDDDEE', z=6)
    elif sym == 'CLD':
        draw_cloud(cx, cy, size*0.85, color='#AAAACC')
    elif sym == 'OVC':
        draw_cloud(cx, cy, size*0.85, color='#777799')
    elif sym in ('RAN', 'HVY'):
        draw_cloud(cx, cy + size*0.2, size*0.75, color='#AAAACC')
        draw_rain_drops(cx, cy, size, color='#4ECDC4', n=3)
    elif sym == 'SHW':
        draw_cloud(cx, cy + size*0.2, size*0.75, color='#888899')
        draw_rain_drops(cx, cy, size, color='#4ECDC4', n=4)
    elif sym == 'DZL':
        draw_cloud(cx, cy + size*0.2, size*0.75, color='#AAAACC')
        draw_rain_drops(cx, cy, size, color='#88CCDD', n=3)
    elif sym == 'SNW':
        draw_cloud(cx, cy + size*0.2, size*0.75, color='#AAAACC')
        for ox in [-size*0.45, 0, size*0.45]:
            add_circle(cx + ox, cy - size*0.72, size*0.10, '#E8F0FF')
    elif sym == 'FOG':
        for dy in [-size*0.25, 0, size*0.25]:
            add_ellipse(cx, cy + dy, size*1.6, size*0.22, '#777799')
    elif sym == 'THN':
        draw_cloud(cx, cy + size*0.25, size*0.75, color='#666688')
        add_ellipse(cx + size*0.05, cy - size*0.45, size*0.22, size*0.60, '#FFD166', angle=15)
    else:
        add_circle(cx, cy, size*0.5, '#666688')


def add_panel(fig, x, y, w, h, facecolor=PANEL_BG, edgecolor=PANEL_EDGE, zorder=0):
    """Add a rounded rectangle background panel in figure-fraction coordinates."""
    panel = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.008",
        transform=fig.transFigure,
        facecolor=facecolor, edgecolor=edgecolor,
        linewidth=1, zorder=zorder, clip_on=False
    )
    fig.add_artist(panel)


def draw_sep_line(fig, y_frac, L, R):
    """Draw a horizontal separator line at figure-fraction y."""
    fig.add_artist(plt.Line2D([L, R], [y_frac, y_frac],
                   color=SEP_LINE, linewidth=1.0, transform=fig.transFigure))


def draw_aq_bar_row(fig, yf, row_bottom_in, row_height_in, slots, max_val, threshold,
                    label, slot_centers, slot_w, L, R, H):
    """
    Render a mini bar chart row for air quality (PM10 or PM2.5).

    Bars are split at the threshold: safe portion uses the grade color,
    overflow above threshold is rendered red (#FF4D4D). A dashed threshold
    line is drawn across all slots.

    row_bottom_in  -- row bottom edge, inches from figure top
    row_height_in  -- total row height in inches
    slots          -- list of {'val': float, 'grade': str}
    max_val        -- scale ceiling (PM10=200, PM2.5=75)
    threshold      -- good/normal -> bad boundary (PM10=80, PM2.5=35)
    """
    row_top_in = row_bottom_in - row_height_in

    chart_top_in = row_top_in + 0.30       # space above bars for value text
    chart_bot_in = row_bottom_in - 0.15    # small bottom margin
    chart_h_in   = chart_bot_in - chart_top_in

    bar_w = slot_w * 0.45

    # Row label centered vertically
    label_y = yf((row_top_in + row_bottom_in) / 2)
    fig.text(L, label_y, label, color=INK_SOFT, fontsize=18,
             fontproperties=prop, va='center', ha='left')

    for i, slot in enumerate(slots):
        cx    = slot_centers[i]
        val   = slot['val']
        grade = slot['grade']
        color = AQ_COLORS[grade]

        safe_val  = min(val, threshold)
        safe_frac = safe_val / max_val
        safe_h_in = chart_h_in * safe_frac

        over_val  = max(0, val - threshold)
        over_frac = over_val / max_val
        over_h_in = chart_h_in * over_frac

        bar_bot_y = yf(chart_bot_in)

        if safe_h_in > 0:
            bh_safe = safe_h_in / H
            fig.patches.append(plt.Rectangle(
                (cx - bar_w / 2, bar_bot_y), bar_w, bh_safe,
                transform=fig.transFigure,
                facecolor=color, edgecolor='none', zorder=3))

        if over_h_in > 0:
            over_bot_y = bar_bot_y + safe_h_in / H
            bh_over = over_h_in / H
            fig.patches.append(plt.Rectangle(
                (cx - bar_w / 2, over_bot_y), bar_w, bh_over,
                transform=fig.transFigure,
                facecolor='#FF4D4D', edgecolor='none', zorder=3))

        total_h_in = safe_h_in + over_h_in
        val_y = yf(chart_bot_in - total_h_in - 0.15)
        fig.text(cx, val_y, f"{val:.0f}",
                 color='#FF4D4D' if over_val > 0 else INK,
                 fontsize=16, fontproperties=bold_prop,
                 va='center', ha='center', zorder=4)

    # Dashed threshold reference line
    thresh_frac = threshold / max_val
    thresh_h_in = chart_h_in * thresh_frac
    thresh_y    = yf(chart_bot_in - thresh_h_in)
    x_start = slot_centers[0]  - slot_w * 0.3
    x_end   = slot_centers[-1] + slot_w * 0.3
    fig.add_artist(plt.Line2D(
        [x_start, x_end], [thresh_y, thresh_y],
        color='#FF6B6B', linewidth=1.0, linestyle='--', alpha=0.6,
        transform=fig.transFigure, zorder=5))


# ---------------------------------------------------------------------------
# Main card render
# ---------------------------------------------------------------------------
def draw_card(weather, aq_parsed, out_path):
    today    = datetime.date.today()
    wday     = WDAY_KO[today.weekday()]
    date_str = f"{today.year}.{today.month:02d}.{today.day:02d} ({wday})"

    temp_now   = weather['temp_now']
    feels_like = weather['feels_like']
    code_now   = weather['code_now']
    temp_max   = weather['temp_max']
    temp_min   = weather['temp_min']
    slots      = weather['slots']

    ko_now, _ = wmo_info(code_now)

    pm10     = aq_parsed['pm10_now']
    pm25     = aq_parsed['pm25_now']
    g10      = pm10_grade(pm10)
    g25      = pm25_grade(pm25)
    aq_slots = aq_parsed['slots']

    # Align both slot lists to 6 entries
    n_slots  = min(len(slots), len(aq_slots), 6)
    slots    = slots[:n_slots]
    aq_slots = aq_slots[:n_slots]

    quote_text, quote_author = get_quote()

    # Figure layout
    FIG_W = 11.0
    FIG_H = 13.0
    DPI   = 180

    fig = plt.figure(figsize=(FIG_W, FIG_H), facecolor=BG)
    fig.patch.set_facecolor(BG)

    H = FIG_H

    def yf(inches_from_top):
        """Convert inches-from-top to figure fraction (0=bottom, 1=top)."""
        return 1.0 - (inches_from_top / H)

    L = 0.07
    R = 0.93

    ROW_LABEL_W = 0.08
    SLOT_L = L + ROW_LABEL_W

    slot_w       = (R - SLOT_L) / max(n_slots, 1)
    slot_centers = [SLOT_L + slot_w * (i + 0.5) for i in range(n_slots)]

    # -----------------------------------------------------------------------
    # Block 1: unified weather + air quality panel
    # -----------------------------------------------------------------------
    panel_top = yf(0.22)
    panel_bot = yf(11.40)
    add_panel(fig, L - 0.01, panel_bot, (R - L + 0.02),
              panel_top - panel_bot, zorder=0)

    # Header: city label + date (city display label uses unicode escape for Seoul)
    # 서울 = Korean for Seoul
    fig.text(L, yf(0.55), '서울', color=INK, fontsize=36,
             fontproperties=bold_prop, va='center', ha='left')
    fig.text(L + 0.092, yf(0.55), date_str, color=INK_SOFT, fontsize=26,
             fontproperties=bold_prop, va='center', ha='left')

    # Current temperature (large, left)
    fig.text(L, yf(1.65), f"{temp_now:.0f}°", color=INK, fontsize=80,
             fontproperties=bold_prop, va='center', ha='left')

    # Condition + feels-like
    # 체감 = Korean for "feels like"
    fig.text(L, yf(2.90), ko_now, color=INK_SOFT, fontsize=28,
             fontproperties=bold_prop, va='center', ha='left')
    fig.text(L, yf(3.45), f"체감 {feels_like:.0f}°", color=INK_VALUE, fontsize=22,
             fontproperties=prop, va='center', ha='left')

    # PM10 / PM2.5 current summary line
    pm10_color = AQ_COLORS[g10]
    pm25_color = AQ_COLORS[g25]
    summary_y  = yf(3.95)
    _renderer  = fig.canvas.get_renderer()
    _dpi       = fig.get_dpi()
    _fig_w_px  = fig.get_figwidth() * _dpi

    def _draw_seq(x_frac, text, color):
        """Draw text at x_frac (figure fraction), return next x after the text."""
        t = fig.text(x_frac, summary_y, text,
                     color=color, fontsize=20, fontproperties=bold_prop,
                     va='center', ha='left')
        fig.canvas.draw()
        bb = t.get_window_extent(renderer=_renderer)
        return x_frac + bb.width / _fig_w_px

    # 미세 = fine dust (PM10); 초미세 = ultra-fine dust (PM2.5)
    _x = L
    _x = _draw_seq(_x, f"미세 {pm10:.0f} ",  INK_SOFT)
    _x = _draw_seq(_x, g10, pm10_color)
    _x = _draw_seq(_x, "   초미세 ", INK_SOFT)
    _x = _draw_seq(_x, f"{pm25:.0f} ", INK_SOFT)
    _draw_seq(_x, g25, pm25_color)

    # Max / min temperatures (right-aligned)
    # 최고 = max; 최저 = min
    fig.text(R, yf(1.65), f"최고 {temp_max:.0f}°", color='#FF6B6B', fontsize=28,
             fontproperties=bold_prop, va='center', ha='right')
    fig.text(R, yf(2.35), f"최저 {temp_min:.0f}°", color=ACCENT, fontsize=28,
             fontproperties=bold_prop, va='center', ha='right')

    draw_sep_line(fig, yf(4.20), L, R)

    # -----------------------------------------------------------------------
    # Unified table: time header / weather icons+temps / precipitation / PM10 / PM2.5
    # -----------------------------------------------------------------------
    HEADER_Y_IN     = 4.50
    ICON_Y_IN       = 5.00
    TEMP_Y_IN       = 5.65
    SEP_PREC_TOP_IN = 6.00
    PREC_Y_IN       = 6.35
    SEP_PREC_BOT_IN = 6.70
    SEP1_Y_IN       = 6.70
    PM10_BAR_BOT_IN = 8.20
    PM10_BAR_H_IN   = 1.50
    SEP2_Y_IN       = 8.35
    PM25_BAR_BOT_IN = 9.85
    PM25_BAR_H_IN   = 1.50

    LABEL_X = L
    # 시간 = time; 날씨 = weather; 강수 = precipitation
    fig.text(LABEL_X, yf(HEADER_Y_IN), '시간', color=INK_SOFT, fontsize=18,
             fontproperties=bold_prop, va='center', ha='left')

    weather_row_mid = (ICON_Y_IN + TEMP_Y_IN) / 2
    fig.text(LABEL_X, yf(weather_row_mid), '날씨', color=INK_SOFT, fontsize=18,
             fontproperties=bold_prop, va='center', ha='left')

    fig.text(LABEL_X, yf(PREC_Y_IN), '강수', color=INK_SOFT, fontsize=18,
             fontproperties=bold_prop, va='center', ha='left')

    draw_sep_line(fig, yf(4.35), SLOT_L, R)
    draw_sep_line(fig, yf(SEP_PREC_TOP_IN), SLOT_L, R)
    draw_sep_line(fig, yf(SEP_PREC_BOT_IN), SLOT_L, R)
    draw_sep_line(fig, yf(SEP2_Y_IN), SLOT_L, R)

    for i in range(n_slots):
        cx     = slot_centers[i]
        w_slot = slots[i]

        fig.text(cx, yf(HEADER_Y_IN), w_slot['time'], color=INK_SOFT, fontsize=19,
                 fontproperties=prop, va='center', ha='center')

        draw_weather_symbol(fig, cx, yf(ICON_Y_IN), w_slot['code'], size=0.022)

        fig.text(cx, yf(TEMP_Y_IN), f"{w_slot['temp']:.0f}°", color=INK, fontsize=22,
                 fontproperties=bold_prop, va='center', ha='center')

        prec = w_slot['precip']
        if prec <= 10:
            prec_color = INK_SOFT
        elif prec <= 30:
            prec_color = WARM
        else:
            prec_color = ACCENT

        fig.text(cx, yf(PREC_Y_IN), f"{int(prec)}%", color=prec_color, fontsize=18,
                 fontproperties=bold_prop, va='center', ha='center')

    # PM10 bar row
    pm10_slots = [{'val': s['pm10'], 'grade': pm10_grade(s['pm10'])} for s in aq_slots]
    pm10_max_scale = max(200, max((s['pm10'] for s in aq_slots), default=0) * 1.2)
    draw_aq_bar_row(fig, yf, PM10_BAR_BOT_IN, PM10_BAR_H_IN,
                    pm10_slots, pm10_max_scale, 80,
                    '미세', slot_centers, slot_w, L, R, FIG_H)

    # PM2.5 bar row
    pm25_slots = [{'val': s['pm25'], 'grade': pm25_grade(s['pm25'])} for s in aq_slots]
    pm25_max_scale = max(75, max((s['pm25'] for s in aq_slots), default=0) * 1.2)
    draw_aq_bar_row(fig, yf, PM25_BAR_BOT_IN, PM25_BAR_H_IN,
                    pm25_slots, pm25_max_scale, 35,
                    '초미세', slot_centers, slot_w, L, R, FIG_H)

    sep_after_block_y = yf(10.10)
    fig.add_artist(plt.Line2D([L, R], [sep_after_block_y, sep_after_block_y],
                   color=SEP_LINE, linewidth=1.5, transform=fig.transFigure))

    # -----------------------------------------------------------------------
    # Block 2: daily quote
    # -----------------------------------------------------------------------
    quote_top_in = 10.50
    quote_bot_in = 12.70
    quote_h_frac = (quote_bot_in - quote_top_in) / FIG_H

    fig.patches.append(plt.Rectangle(
        (L, yf(quote_bot_in)), R - L, quote_h_frac,
        transform=fig.transFigure, facecolor=QUOTE_BG, edgecolor=PANEL_EDGE,
        linewidth=1, zorder=1))

    quote_mid_y = yf((quote_top_in + quote_bot_in) / 2 - 0.30)
    author_y    = yf((quote_top_in + quote_bot_in) / 2 + 0.38)

    fig.text(0.50, author_y, f"— {quote_author}",
             color=WARM, fontsize=20, fontproperties=bold_prop,
             va='center', ha='center')

    fig.text(0.50, quote_mid_y, f'"{quote_text}"',
             color=INK, fontsize=22, fontproperties=prop,
             va='center', ha='center',
             bbox=dict(boxstyle='square,pad=0', facecolor='none', edgecolor='none'))

    # -----------------------------------------------------------------------
    # Save PNG
    # -----------------------------------------------------------------------
    plt.savefig(out_path, dpi=DPI, facecolor=BG, edgecolor='none',
                bbox_inches='tight')
    plt.close()
    sys.stderr.write(f"morning_brief_card: wrote {out_path}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.stderr.write(f"morning_brief_card: fetching weather (lat={LAT}, lon={LON})...\n")
    weather_raw = fetch_weather()
    weather     = parse_weather(weather_raw)

    sys.stderr.write("morning_brief_card: fetching air quality...\n")
    aq_raw    = fetch_air_quality()
    aq_parsed = parse_air_quality(aq_raw)

    pm10 = aq_parsed['pm10_now']
    pm25 = aq_parsed['pm25_now']
    sys.stderr.write(
        f"morning_brief_card: temp={weather['temp_now']:.0f}C "
        f"hi={weather['temp_max']:.0f} lo={weather['temp_min']:.0f} "
        f"pm10={pm10:.0f}({pm10_grade(pm10)}) pm25={pm25:.0f}({pm25_grade(pm25)})\n"
    )

    sys.stderr.write("morning_brief_card: rendering...\n")
    draw_card(weather, aq_parsed, OUT_PATH)
    print(OUT_PATH)
