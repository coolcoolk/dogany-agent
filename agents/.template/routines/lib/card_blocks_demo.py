#!/usr/bin/env python3
"""card_blocks_demo.py -- consumer example for card_blocks (DGN-660).

Reproduces the locked DGN-660 reference look (weather briefing stack,
reference-3x3.png) from HARDCODED sample data -- no network fetch. This
file demonstrates the intended consumer split:

  * card_blocks.py owns the primitive: type tokens, palette, stack
    assembly, Chrome two-pass render, R8 gates.
  * the consumer (here: a weather briefing) owns its domain layout as
    Card subclasses and its data acquisition (replaced by sample data).

Layout note: the subclasses below compose their type entirely from the
tt-* element classes -- their CSS carries zero font-size/weight literals
(3x3 token rule). Sample data strings are Korean because the reference
artifact is Korean user-facing content (same convention as
morning_brief_card.py).

Run:  python3 card_blocks_demo.py [out.png]
"""

import sys

from card_blocks import Card, CardStack, QuoteCard, Row, icon_svg

# ---------------------------------------------------------------------------
# Sample data -- verbatim from the locked reference render (reference-3x3)
# ---------------------------------------------------------------------------
SAMPLE = {
    "city": "서울 날씨",
    "date": "2026.07.31 (금)",
    "temp": "27°",
    "icon": "cloudy",
    "condition": "구름 많음",
    "feels": "체감 34°",
    "hi": "31°",
    "lo": "23°",
    "pm25": {"label": "초미세먼지", "tag": "PM2.5",
             "max": 33, "min": 11},
    "pm10": {"label": "미세먼지", "tag": "PM10",
             "max": 35, "min": 15},
    # (time, temp, icon, precip %)
    "slots": [
        ("06:00", "23°", "overcast", 0),
        ("09:00", "26°", "cloudy", 0),
        ("12:00", "30°", "few", 20),
        ("15:00", "31°", "few", 51),
        ("18:00", "30°", "sun", 10),
        ("21:00", "27°", "cloudy", 0),
    ],
    "pm25_series": [14, 16, 15, 17, 23, 29],
    "pm10_series": [16, 20, 18, 19, 26, 32],
    "quote": "성공은 준비가 기회를 만났을 때 일어납니다.",
    "quote_author": "세네카",
}


# ---------------------------------------------------------------------------
# Air-quality grading (official Korean PM bands) -> palette grade slots
# ---------------------------------------------------------------------------
def pm25_grade(v):
    if v < 15:
        return "좋음", "grade-good"
    if v < 35:
        return "보통", "grade-ok"
    if v < 75:
        return "나쁨", "grade-bad"
    return "매우나쁨", "grade-vbad"


def pm10_grade(v):
    if v < 30:
        return "좋음", "grade-good"
    if v < 80:
        return "보통", "grade-ok"
    if v < 150:
        return "나쁨", "grade-bad"
    return "매우나쁨", "grade-vbad"


# ---------------------------------------------------------------------------
# Domain cards (consumer-owned layout; type via tt-* classes only)
# ---------------------------------------------------------------------------
class WeatherHeroCard(Card):
    """Stack-top weather card: the sole hero (LG/800) element."""

    EXTRA_CSS = """
.weather-head { display: flex; justify-content: space-between;
                align-items: baseline; margin-bottom: 20px; }
.weather-city { letter-spacing: -0.01em; }
.weather-body { display: flex; align-items: flex-start; gap: 16px; }
.weather-temp { line-height: 0.95; letter-spacing: -0.03em;
                font-variant-numeric: tabular-nums; }
.weather-icon-wrap { padding-top: 10px; flex-shrink: 0; }
.weather-icon-wrap svg { width: 88px; height: 88px; }
.weather-mid { flex: 1; padding-top: 8px; }
.weather-condition { letter-spacing: -0.01em; }
.weather-feels { margin-top: 6px; }
.weather-hilow { display: flex; flex-direction: column; gap: 8px;
                 padding-top: 10px; text-align: right; margin-left: auto; }
.weather-hl { font-variant-numeric: tabular-nums; }
"""

    def __init__(self, d):
        self.d = d

    def inner_html(self, p):
        d = self.d
        return (
            '<div class="weather-head">'
            '<span class="weather-city tt-title">%(city)s</span>'
            '<span class="weather-date tt-meta" style="color:%(muted)s;">'
            '%(date)s</span></div>'
            '<div class="weather-body">'
            '<span class="weather-temp tt-hero">%(temp)s</span>'
            '<span class="weather-icon-wrap">%(icon)s</span>'
            '<div class="weather-mid">'
            '<div class="weather-condition tt-body">%(condition)s</div>'
            '<div class="weather-feels tt-meta" style="color:%(muted)s;">'
            '%(feels)s</div></div>'
            '<div class="weather-hilow">'
            '<span class="weather-hl tt-meta" style="color:%(red)s;">'
            '최고 <span class="tt-num">%(hi)s</span></span>'
            '<span class="weather-hl tt-meta" style="color:%(accent)s;">'
            '최저 <span class="tt-num">%(lo)s</span></span>'
            '</div></div>'
            % {"city": d["city"], "date": d["date"], "temp": d["temp"],
               "icon": icon_svg(d["icon"]), "condition": d["condition"],
               "feels": d["feels"], "hi": d["hi"], "lo": d["lo"],
               "muted": p["muted"], "red": p["red"], "accent": p["accent"]})


class PmCard(Card):
    """Air-quality card: key value on the MD/800 rung (weight separates it
    from the MD/600 title), min row on the SM rungs."""

    CARD_CLASS = "card pm-card"
    EXTRA_CSS = """
.pm-card { padding: 34px 40px; }
.pm-label-row { display: flex; justify-content: space-between;
                align-items: baseline; margin-bottom: 12px; }
.pm-label { letter-spacing: -0.01em; }
.pm-max-row { display: flex; align-items: baseline; gap: 16px;
              margin-bottom: 14px; }
.pm-value { line-height: 1; letter-spacing: -0.02em;
            font-variant-numeric: tabular-nums; }
.pm-min-row { display: flex; align-items: baseline; gap: 16px; }
.pm-min-val { font-variant-numeric: tabular-nums; }
"""

    def __init__(self, spec, grade_fn):
        self.spec = spec
        self.grade_fn = grade_fn

    def inner_html(self, p):
        s = self.spec
        gmax_txt, gmax_slot = self.grade_fn(s["max"])
        gmin_txt, gmin_slot = self.grade_fn(s["min"])
        return (
            '<div class="pm-label-row">'
            '<span class="pm-label tt-title">%(label)s</span>'
            '<span class="tt-meta" style="color:%(muted)s;">%(tag)s</span>'
            '</div>'
            '<div class="pm-max-row">'
            '<span class="tt-meta" style="color:%(muted)s;">최고</span>'
            '<span class="pm-value tt-value">%(max)d</span>'
            '<span class="tt-meta" style="color:%(gmax_c)s;">%(gmax)s</span>'
            '</div>'
            '<div class="pm-min-row">'
            '<span class="tt-meta" style="color:%(muted)s;">최저</span>'
            '<span class="pm-min-val tt-num">%(min)d</span>'
            '<span class="tt-meta" style="color:%(gmin_c)s;">%(gmin)s</span>'
            '</div>'
            % {"label": s["label"], "tag": s["tag"], "max": s["max"],
               "min": s["min"], "muted": p["muted"],
               "gmax": gmax_txt, "gmax_c": p[gmax_slot],
               "gmin": gmin_txt, "gmin_c": p[gmin_slot]})


class TimelineCard(Card):
    """Hourly forecast: icon column per slot + PM mini-bar rows."""

    EXTRA_CSS = """
.tl-head { margin-bottom: 24px; letter-spacing: -0.01em; }
.tl-grid { display: flex; justify-content: space-between; gap: 8px; }
.tl-slot { flex: 1; display: flex; flex-direction: column;
           align-items: center; gap: 10px; }
.tl-time { white-space: nowrap; }
.tl-icon svg { width: 72px; height: 72px; }
.tl-temp { letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.tl-prec { font-variant-numeric: tabular-nums; }
.tl-pm-row { display: flex; align-items: flex-end; gap: 8px;
             margin-top: 26px; }
.tl-pm-label { width: 92px; flex-shrink: 0; padding-bottom: 8px;
               white-space: nowrap; }
.tl-pm-slots { display: flex; flex: 1; justify-content: space-between;
               gap: 4px; }
.tl-bar-slot { flex: 1; display: flex; flex-direction: column;
               align-items: center; gap: 6px; }
.tl-bar-val { font-variant-numeric: tabular-nums; }
.tl-bar-track { width: 50px; height: 68px; border-radius: 8px;
                display: flex; align-items: flex-end; overflow: hidden; }
.tl-bar-fill { width: 100%; border-radius: 8px 8px 0 0; }
"""

    def __init__(self, title, slots, pm25_series, pm10_series):
        self.title = title
        self.slots = slots
        self.pm25_series = pm25_series
        self.pm10_series = pm10_series

    def _precip_color(self, p, precip):
        if precip <= 10:
            return p["muted"]
        if precip <= 30:
            return p["yellow"]
        return p["red"]

    def _bar_row(self, p, label, series, grade_fn, floor, threshold):
        # Scale bars against the actual series so low values stay visible:
        # 1.5x the series max, floored at the official "normal" band edge.
        scale = max(float(floor), max(series) * 1.5)
        slots_html = ""
        for v in series:
            _, slot = grade_fn(v)
            color = p["red"] if v > threshold else p[slot]
            pct = min(100.0, v / scale * 100.0)
            slots_html += (
                '<div class="tl-bar-slot">'
                '<span class="tl-bar-val tt-num" style="color:%(c)s;">'
                '%(v)d</span>'
                '<div class="tl-bar-track" style="background:%(bg)s;">'
                '<div class="tl-bar-fill" '
                'style="height:%(pct).1f%%;background:%(c)s;"></div>'
                '</div></div>'
                % {"c": color, "v": v, "pct": pct, "bg": p["bg"]})
        return (
            '<div class="tl-pm-row">'
            '<span class="tl-pm-label tt-meta" style="color:%s;">%s</span>'
            '<div class="tl-pm-slots">%s</div></div>'
            % (p["muted"], label, slots_html))

    def inner_html(self, p):
        slots_html = ""
        for t, temp, icon, precip in self.slots:
            slots_html += (
                '<div class="tl-slot">'
                '<span class="tl-time tt-meta" style="color:%(muted)s;">'
                '%(t)s</span>'
                '<span class="tl-icon">%(icon)s</span>'
                '<span class="tl-temp tt-num">%(temp)s</span>'
                '<span class="tl-prec tt-num" style="color:%(pc)s;">'
                '%(precip)d%%</span></div>'
                % {"t": t, "icon": icon_svg(icon), "temp": temp,
                   "precip": precip, "muted": p["muted"],
                   "pc": self._precip_color(p, precip)})
        return (
            '<div class="tl-head tt-title">%s</div>'
            '<div class="tl-grid">%s</div>%s%s'
            % (self.title, slots_html,
               self._bar_row(p, "초미세", self.pm25_series, pm25_grade,
                             35, 35),
               self._bar_row(p, "미세", self.pm10_series, pm10_grade,
                             50, 80)))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_stack(data=SAMPLE):
    stack = CardStack()
    stack.add(WeatherHeroCard(data))
    stack.add(Row(PmCard(data["pm25"], pm25_grade),
                  PmCard(data["pm10"], pm10_grade)))
    stack.add(TimelineCard("시간대 예보", data["slots"],
                           data["pm25_series"], data["pm10_series"]))
    stack.add(QuoteCard(data["quote"], data["quote_author"]))
    return stack


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/card_blocks_demo.png"
    report = build_stack().save(out)
    print("demo report: %s" % report)


if __name__ == "__main__":
    main()
