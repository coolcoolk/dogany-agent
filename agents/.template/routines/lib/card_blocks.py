#!/usr/bin/env python3
"""card_blocks.py -- R7 information-hierarchy card blocks (DGN-660).

Reusable render primitive: structured card data -> stacked card PNG.
Backend: HTML/CSS assembled by this module, rendered with Chrome headless
in two passes (tall render -> pixel-scan the real content bottom ->
re-render at the exact height, so output height is data-driven).

The R8 Resource-Constraint Target (RCT) gates are enforced in
CardStack.save() -- the single mechanical gate for user-bound card images:
  * render width in [720, 1080] (1080 standard)
  * aspect ratio w:h in [9:16, 4:5] = [0.5625, 0.80], checked at layout
    time (after measuring, before the final render) -- overlong stacks are
    a caller defect (split the stack), never silently squeezed
  * PNG byte target < 1.5MB; on overflow recover in order:
    (1) palette-quantize PNG-8, (2) downscale width toward the 720 floor,
    (3) error -- never a silent oversized send

Type system: 3x3 (three sizes x three weights = nine named tokens, zero
size/weight literals outside the token tables below). Locked 2026-07-31
(DGN-660 owner decision):

    size    LG 76 / MD 42 / SM 30   physical px at 1080 render width
    weight  800 ExtraBold / 600 SemiBold / 400 Regular

Element -> token map (verbatim from the DGN-660 lock):

    hero value (current temp)   LG/800  tt-hero    sole per-stack maximum
    card title                  MD/600  tt-title
    key value (PM max)          MD/800  tt-value   same size as title,
                                                   separated by weight
    condition / quote body      MD/400  tt-body
    bold small numbers          SM/800  tt-num     timeline temp, precip,
                                                   bar values, min values
    supporting emphasis         SM/600  tt-support quote author
    meta / labels               SM/400  tt-meta    date, feels-like, tags,
                                                   times, grades, hi/lo
                                                   labels

Typeface: Pretendard (bundled in fonts/pretendard/ next to this module,
weights 400/600/800 only). The @font-face src is resolved from the bundle
location at runtime -- never a machine-absolute path.

Data fetching is NOT this module's responsibility: consumers assemble
Card objects from their own data and call CardStack.save(). See
card_blocks_demo.py for a consumer example (the DGN-660 weather
reference look, hardcoded sample data).

Runs on stdlib only (PNG decode/scan uses zlib); Pillow is used
opportunistically for the byte-overflow quantize step when installed.
"""

import os
import shutil
import struct
import subprocess
import sys
import zlib

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 3x3 type tokens (DGN-660 LOCK 2026-07-31) -- THE single reference table.
# No font-size / font-weight literal may appear anywhere outside this block;
# CSS consumes the generated --lg/--md/--sm and --w-* custom properties via
# the tt-* element classes.
# ---------------------------------------------------------------------------
SIZES = {"lg": 76, "md": 42, "sm": 30}       # physical px at 1080 width
WEIGHTS = ("800", "600", "400")              # ExtraBold / SemiBold / Regular

HERO_MIN_PX = 70    # invariant: the hero rung stays >= 70
FONT_FLOOR_PX = 28  # invariant: no rung below 28 (RCT legibility floor)

# element class -> (size rung, weight) -- the verbatim lock mapping
ELEMENTS = {
    "hero":    ("lg", "800"),
    "title":   ("md", "600"),
    "value":   ("md", "800"),
    "body":    ("md", "400"),
    "num":     ("sm", "800"),
    "support": ("sm", "600"),
    "meta":    ("sm", "400"),
}

assert SIZES["lg"] >= HERO_MIN_PX, "hero rung violates the >=70px invariant"
assert min(SIZES.values()) >= FONT_FLOOR_PX, "size rung below the 28px floor"
assert all(w in WEIGHTS for _, w in ELEMENTS.values())
assert all(s in SIZES for s, _ in ELEMENTS.values())


def type_css():
    """Generate the :root token variables + tt-* element classes from the
    token tables. This is the only producer of font-size/weight CSS."""
    root_vars = "\n".join(
        "  --%s: %dpx;" % (name, px) for name, px in SIZES.items())
    root_vars += "\n" + "\n".join(
        "  --w-%s: %s;" % (w, w) for w in WEIGHTS)
    classes = "\n".join(
        ".tt-%s { font-size: var(--%s); font-weight: var(--w-%s); }"
        % (el, size, weight) for el, (size, weight) in ELEMENTS.items())
    return ":root {\n%s\n}\n%s" % (root_vars, classes)


# ---------------------------------------------------------------------------
# RCT budget (R8, DGN-660 LOCK) -- consumed by the save() gate.
# ---------------------------------------------------------------------------
RCT = {
    "width_std": 1080,
    "width_min": 720,
    "width_max": 1080,
    "aspect_min": 0.5625,        # 9:16
    "aspect_max": 0.80,          # 4:5
    "byte_target": 1_572_864,    # < 1.5 MB
}

# ---------------------------------------------------------------------------
# Layout tokens (structural, not typographic)
# ---------------------------------------------------------------------------
LAYOUT = {
    "stack_gap": 33,     # px between sibling cards (>= 3% of 1080 rule)
    "stack_pad": 28,     # px stack padding; also the bottom breathing room
    "card_radius": 32,
    "card_pad_v": 40,
    "card_pad_h": 62,    # >= 6% of the card width (R7 inner-padding rule)
}

# ---------------------------------------------------------------------------
# Default palette -- the DGN-660 reference look (lifekit light, teal-pale).
# The palette HOME is design_tokens.py: the Layer B "card-light" theme fills
# the 11 semantic slots and GRADE_LIGHT carries the grade scale; this module
# only ASSEMBLES the render palette from those tokens (zero standalone theme
# hex here). The mechanical derivations pinned in the theme are re-checked
# against the design_tokens derivation chain at self-test (drift guard).
# ---------------------------------------------------------------------------
def load_design_tokens():
    """Load the sibling design_tokens.py module by file path (same walk-up
    convention as the card scripts; here it is a plain sibling)."""
    import importlib.util
    path = os.path.join(MODULE_DIR, "design_tokens.py")
    spec = importlib.util.spec_from_file_location("design_tokens", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _card_light_palette():
    """Assemble the default palette from the token SoT: the registered
    card-light theme (purple/orange are registered slots but unused by
    card renders) plus GRADE_LIGHT mapped onto the grade-* slots."""
    dt = load_design_tokens()
    t = dt.theme("card-light")
    pal = {k: t[k] for k in
           ("bg", "surface", "border", "text", "muted", "accent",
            "green", "yellow", "red")}
    for grade, value in dt.GRADE_LIGHT.items():
        pal["grade-" + grade] = value
    return pal


CARD_LIGHT = _card_light_palette()

# (pinned slot, design_tokens expression) pairs for the drift guard
_MECHANICAL_PROVENANCE = (
    ("bg",      lambda dt: dt.BACKGROUNDS["teal-pale"]["hex"]),
    ("surface", lambda dt: dt.card_surface(dt.BACKGROUNDS["teal-pale"]["hex"])),
    ("accent",  lambda dt: dt.accent_for("teal", "teal-pale")),
    ("green",   lambda dt: dt.tune_accent(dt.BRAND["green"], CARD_LIGHT["surface"])),
    ("grade-ok", lambda dt: dt.tune_accent(dt.BRAND["grade-ok"], CARD_LIGHT["surface"])),
)


def palette_from_theme(theme_name):
    """Build a card palette from a registered Layer B theme (e.g.
    'card-dark'). Grade slots fall back to BRAND grade-* (dark-measured);
    light themes should supply surface-checked values instead."""
    dt = load_design_tokens()
    t = dt.theme(theme_name)
    pal = {k: t[k] for k in
           ("bg", "surface", "border", "text", "muted", "accent",
            "green", "yellow", "red")}
    for g in ("grade-good", "grade-ok", "grade-bad", "grade-vbad"):
        pal[g] = dt.BRAND[g]
    return pal


def check_palette_drift(log=sys.stderr):
    """Drift guard: the mechanical values pinned in the card-light theme
    (assembled into CARD_LIGHT) must equal what the design_tokens
    derivation chain produces today. Returns a list of drift messages
    (empty = clean)."""
    dt = load_design_tokens()
    drifts = []
    for slot, derive in _MECHANICAL_PROVENANCE:
        expected = derive(dt)
        if expected != CARD_LIGHT[slot]:
            drifts.append("palette drift: %s pinned %s != derived %s"
                          % (slot, CARD_LIGHT[slot], expected))
    for msg in drifts:
        log.write("[card_blocks] WARNING %s\n" % msg)
    return drifts


# ---------------------------------------------------------------------------
# Fonts -- bundled Pretendard, resolved relative to this module at runtime.
# ---------------------------------------------------------------------------
FONT_DIR = os.path.join(MODULE_DIR, "fonts", "pretendard")
FONT_FILES = {
    "400": "Pretendard-Regular.otf",
    "600": "Pretendard-SemiBold.otf",
    "800": "Pretendard-ExtraBold.otf",
}
_FONT_FALLBACK = "'Apple SD Gothic Neo', 'Noto Sans KR', sans-serif"


def font_face_css(font_dir=None, log=sys.stderr):
    """Return (font_face_block, font_family). Existence guard: logs whether
    the bundled faces were found; on a partial/missing bundle it falls back
    to system fonts (and says so) instead of silently rendering tofu."""
    fdir = font_dir or FONT_DIR
    paths = {w: os.path.join(fdir, f) for w, f in FONT_FILES.items()}
    missing = [p for p in paths.values() if not os.path.isfile(p)]
    if missing:
        log.write("[card_blocks] WARNING font bundle incomplete (%s); "
                  "falling back to system fonts\n"
                  % ", ".join(os.path.basename(m) for m in missing))
        return "", _FONT_FALLBACK
    log.write("[card_blocks] fonts: bundled Pretendard 400/600/800 "
              "loaded from %s\n" % fdir)
    faces = "\n".join(
        "@font-face {\n"
        "  font-family: 'Pretendard';\n"
        "  src: url('file://%s');\n"
        "  font-weight: %s;\n"
        "}" % (paths[w], w) for w in ("400", "600", "800"))
    return faces, "'Pretendard', " + _FONT_FALLBACK


# ---------------------------------------------------------------------------
# Chrome headless discovery
# ---------------------------------------------------------------------------
_CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_CHROME_NAMES = ("google-chrome", "google-chrome-stable",
                 "chromium", "chromium-browser")


def find_chrome():
    """Locate a Chrome/Chromium binary. Order: CARD_BLOCKS_CHROME env,
    macOS app bundle, PATH lookups (Linux live instances)."""
    env = os.environ.get("CARD_BLOCKS_CHROME", "")
    if env and os.path.isfile(env):
        return env
    if os.path.isfile(_CHROME_MAC):
        return _CHROME_MAC
    for name in _CHROME_NAMES:
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError(
        "no Chrome/Chromium binary found (set CARD_BLOCKS_CHROME to the "
        "browser path)")


# ---------------------------------------------------------------------------
# Icon mini-set (helper-owned inline SVGs, DGN-660 grill OQ7).
# Consumers may also pass any inline <svg> string of their own.
#
# ICON_COLORS is the bounded illustration-pigment palette (R7 carve-out):
# these are drawing pigments for the icon artwork, distinct from the
# semantic theme slots that R1 governs. Adding a pigment extends this
# table; inlining raw hex in the SVG markup is a defect.
# ---------------------------------------------------------------------------
ICON_COLORS = {
    "sun":           "#D4A017",  # sun disc/rays + thunder bolt
    "cloud_light":   "#B0C4BE",  # few-clouds body
    "cloud_pale":    "#A8C0BA",  # cloudy top highlight + snow cloud body
    "cloud_mid":     "#9AB0AA",  # cloudy/drizzle body + unknown disc
    "cloud_soft":    "#8AA8A2",  # overcast top highlight + fog strokes
    "cloud_dark":    "#7A9690",  # overcast body + rain cloud
    "cloud_darker":  "#6A8680",  # shower cloud
    "thunder_cloud": "#5A6870",  # thunder cloud
    "rain":          "#3A8A80",  # rain streaks
    "rain_deep":     "#2A7A70",  # shower streaks (heavier)
    "drizzle":       "#5A9A90",  # drizzle streaks (lighter)
    "snow":          "#E8F4F2",  # snowflakes
    "label_q":       "#286E60",  # unknown-icon '?' glyph
}

# SVG templates reference ICON_COLORS by %(pigment)s key; ICONS below is
# the resolved consumer surface (identical markup to the pre-extraction
# literals).
_ICON_TEMPLATES = {
    "sun": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <circle cx="26" cy="26" r="11" fill="%(sun)s"/>
  <g stroke="%(sun)s" stroke-width="2.5" stroke-linecap="round">
    <line x1="26" y1="4"  x2="26" y2="10"/>
    <line x1="26" y1="42" x2="26" y2="48"/>
    <line x1="4"  y1="26" x2="10" y2="26"/>
    <line x1="42" y1="26" x2="48" y2="26"/>
    <line x1="10.3" y1="10.3" x2="14.6" y2="14.6"/>
    <line x1="37.4" y1="37.4" x2="41.7" y2="41.7"/>
    <line x1="41.7" y1="10.3" x2="37.4" y2="14.6"/>
    <line x1="14.6" y1="37.4" x2="10.3" y2="41.7"/>
  </g>
</svg>""",
    "few": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <circle cx="32" cy="17" r="8" fill="%(sun)s" opacity="0.9"/>
  <g stroke="%(sun)s" stroke-width="2" stroke-linecap="round" opacity="0.7">
    <line x1="32" y1="5"  x2="32" y2="8"/>
    <line x1="44" y1="17" x2="41" y2="17"/>
    <line x1="40.5" y1="8.5" x2="38.4" y2="10.6"/>
  </g>
  <ellipse cx="22" cy="32" rx="14" ry="9" fill="%(cloud_light)s"/>
  <ellipse cx="14" cy="34" rx="9"  ry="7" fill="%(cloud_light)s"/>
  <ellipse cx="30" cy="34" rx="9"  ry="7" fill="%(cloud_light)s"/>
</svg>""",
    "cloudy": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="26" cy="28" rx="18" ry="11" fill="%(cloud_mid)s"/>
  <ellipse cx="16" cy="31" rx="11" ry="9"  fill="%(cloud_mid)s"/>
  <ellipse cx="36" cy="31" rx="11" ry="9"  fill="%(cloud_mid)s"/>
  <ellipse cx="26" cy="22" rx="12" ry="10" fill="%(cloud_pale)s"/>
</svg>""",
    "overcast": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="26" cy="28" rx="18" ry="11" fill="%(cloud_dark)s"/>
  <ellipse cx="16" cy="31" rx="11" ry="9"  fill="%(cloud_dark)s"/>
  <ellipse cx="36" cy="31" rx="11" ry="9"  fill="%(cloud_dark)s"/>
  <ellipse cx="26" cy="22" rx="12" ry="10" fill="%(cloud_soft)s"/>
</svg>""",
    "rain": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="26" cy="22" rx="18" ry="10" fill="%(cloud_dark)s"/>
  <ellipse cx="16" cy="25" rx="11" ry="8"  fill="%(cloud_dark)s"/>
  <ellipse cx="36" cy="25" rx="11" ry="8"  fill="%(cloud_dark)s"/>
  <g stroke="%(rain)s" stroke-width="2.5" stroke-linecap="round">
    <line x1="18" y1="36" x2="15" y2="44"/>
    <line x1="26" y1="36" x2="23" y2="44"/>
    <line x1="34" y1="36" x2="31" y2="44"/>
  </g>
</svg>""",
    "shower": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="26" cy="22" rx="18" ry="10" fill="%(cloud_darker)s"/>
  <ellipse cx="16" cy="25" rx="11" ry="8"  fill="%(cloud_darker)s"/>
  <ellipse cx="36" cy="25" rx="11" ry="8"  fill="%(cloud_darker)s"/>
  <g stroke="%(rain_deep)s" stroke-width="2.5" stroke-linecap="round">
    <line x1="16" y1="36" x2="12" y2="46"/>
    <line x1="24" y1="36" x2="20" y2="46"/>
    <line x1="32" y1="36" x2="28" y2="46"/>
    <line x1="40" y1="36" x2="36" y2="46"/>
  </g>
</svg>""",
    "drizzle": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="26" cy="22" rx="18" ry="10" fill="%(cloud_mid)s"/>
  <ellipse cx="16" cy="25" rx="11" ry="8"  fill="%(cloud_mid)s"/>
  <ellipse cx="36" cy="25" rx="11" ry="8"  fill="%(cloud_mid)s"/>
  <g stroke="%(drizzle)s" stroke-width="1.8" stroke-linecap="round">
    <line x1="18" y1="36" x2="16" y2="42"/>
    <line x1="26" y1="36" x2="24" y2="42"/>
    <line x1="34" y1="36" x2="32" y2="42"/>
  </g>
</svg>""",
    "snow": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="26" cy="22" rx="18" ry="10" fill="%(cloud_pale)s"/>
  <ellipse cx="16" cy="25" rx="11" ry="8"  fill="%(cloud_pale)s"/>
  <ellipse cx="36" cy="25" rx="11" ry="8"  fill="%(cloud_pale)s"/>
  <g fill="%(snow)s">
    <circle cx="16" cy="38" r="3"/>
    <circle cx="26" cy="38" r="3"/>
    <circle cx="36" cy="38" r="3"/>
    <circle cx="21" cy="45" r="3"/>
    <circle cx="31" cy="45" r="3"/>
  </g>
</svg>""",
    "thunder": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="26" cy="20" rx="18" ry="10" fill="%(thunder_cloud)s"/>
  <ellipse cx="16" cy="23" rx="11" ry="8"  fill="%(thunder_cloud)s"/>
  <ellipse cx="36" cy="23" rx="11" ry="8"  fill="%(thunder_cloud)s"/>
  <polygon points="28,30 22,42 27,42 24,52 34,38 29,38" fill="%(sun)s"/>
</svg>""",
    "fog": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <g stroke="%(cloud_soft)s" stroke-width="3" stroke-linecap="round">
    <line x1="8"  y1="18" x2="44" y2="18"/>
    <line x1="10" y1="26" x2="42" y2="26"/>
    <line x1="8"  y1="34" x2="44" y2="34"/>
  </g>
</svg>""",
    "unknown": """<svg width="52" height="52" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <circle cx="26" cy="26" r="18" fill="%(cloud_mid)s" opacity="0.5"/>
  <text x="26" y="32" font-size="22" text-anchor="middle" fill="%(label_q)s">?</text>
</svg>""",
}

ICONS = {name: tpl % ICON_COLORS for name, tpl in _ICON_TEMPLATES.items()}


def icon_svg(name_or_svg):
    """Resolve an icon: a helper-owned name from ICONS, or a raw <svg>
    string passed through unchanged."""
    if name_or_svg is None:
        return ""
    s = str(name_or_svg)
    if s.lstrip().startswith("<svg"):
        return s
    return ICONS.get(s, ICONS["unknown"])


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------
class Card:
    """Base card. Subclasses implement inner_html(palette) and may supply a
    class-level EXTRA_CSS block (collected once per class by the stack).
    A card renders as one .card block -- the single container level (R7:
    no nested background panels inside a card)."""

    EXTRA_CSS = ""
    CARD_CLASS = "card"

    def inner_html(self, palette):
        raise NotImplementedError

    def html(self, palette):
        return '<div class="%s">%s</div>' % (self.CARD_CLASS,
                                             self.inner_html(palette))


class ValueCard(Card):
    """Generic R7 value-card: label head (+optional icon/tag), one dominant
    value (+unit), optional delta line, optional meta line.

    hero=True renders the value on the LG/800 hero rung -- at most one
    hero card per stack (the stack enforces this); all other value-cards
    use the MD/800 key-value rung.
    value_color / delta_color are palette slot names (e.g. "grade-ok",
    "red"); default value color is the ink text color (R7: accent color
    never decorates the big number unless the value IS a status)."""

    EXTRA_CSS = """
.vc-head { display: flex; align-items: baseline; gap: 16px;
           margin-bottom: 14px; }
.vc-head .vc-icon { align-self: center; flex-shrink: 0; }
.vc-label { letter-spacing: -0.01em; }
.vc-tag { margin-left: auto; }
.vc-value-row { display: flex; align-items: baseline; gap: 16px; }
.vc-value { line-height: 1; letter-spacing: -0.03em;
            font-variant-numeric: tabular-nums; }
.vc-hero { line-height: 0.95; }
.vc-delta { margin-top: 12px; font-variant-numeric: tabular-nums; }
.vc-meta { margin-top: 10px; }
"""

    def __init__(self, label, value, unit=None, delta=None, meta=None,
                 icon=None, tag=None, hero=False,
                 value_color=None, delta_color=None):
        self.label = label
        self.value = value
        self.unit = unit
        self.delta = delta
        self.meta = meta
        self.icon = icon
        self.tag = tag
        self.hero = hero
        self.value_color = value_color
        self.delta_color = delta_color

    def inner_html(self, palette):
        p = palette
        icon = icon_svg(self.icon)
        icon_html = '<span class="vc-icon">%s</span>' % icon if icon else ""
        tag_html = ('<span class="vc-tag tt-meta" style="color:%s;">%s</span>'
                    % (p["muted"], self.tag) if self.tag else "")
        value_cls = "tt-hero vc-value vc-hero" if self.hero \
            else "tt-value vc-value"
        value_col = p[self.value_color] if self.value_color else p["text"]
        unit_html = ('<span class="tt-meta" style="color:%s;">%s</span>'
                     % (p["muted"], self.unit) if self.unit else "")
        delta_html = ""
        if self.delta:
            delta_col = p[self.delta_color] if self.delta_color else p["muted"]
            delta_html = ('<div class="vc-delta tt-num" style="color:%s;">%s'
                          '</div>' % (delta_col, self.delta))
        meta_html = ('<div class="vc-meta tt-meta" style="color:%s;">%s</div>'
                     % (p["muted"], self.meta) if self.meta else "")
        return (
            '<div class="vc-head">%s<span class="vc-label tt-title">%s'
            '</span>%s</div>'
            '<div class="vc-value-row"><span class="%s" style="color:%s;">'
            '%s</span>%s</div>%s%s'
            % (icon_html, self.label, tag_html, value_cls, value_col,
               self.value, unit_html, delta_html, meta_html))


class QuoteCard(Card):
    """R7 quote/prose variant: body text on the MD/400 rung, author on the
    SM/600 supporting-emphasis rung in the accent color."""

    CARD_CLASS = "card qc"
    EXTRA_CSS = """
.qc { text-align: center; padding: 40px 48px; }
.qc-text { line-height: 1.5; letter-spacing: -0.005em;
           margin-bottom: 16px; }
"""

    def __init__(self, text, author):
        self.text = text
        self.author = author

    def inner_html(self, palette):
        return (
            '<div class="qc-text tt-body" style="color:%s;">"%s"</div>'
            '<div class="qc-author tt-support" style="color:%s;">'
            '&mdash; %s</div>'
            % (palette["text"], self.text, palette["accent"], self.author))


class Row(Card):
    """Horizontal group of sibling cards sharing one stack row (e.g. the
    PM2.5 / PM10 pair). The row itself paints no background -- the ground
    stays the only grouping surface (R7 single-card principle)."""

    EXTRA_CSS = """
.card-row { display: flex; gap: %(stack_gap)dpx; }
.card-row > .card { flex: 1; }
""" % LAYOUT

    def __init__(self, *cards):
        self.cards = list(cards)

    def html(self, palette):
        return ('<div class="card-row">%s</div>'
                % "".join(c.html(palette) for c in self.cards))

    def child_classes(self):
        return [type(c) for c in self.cards]


# ---------------------------------------------------------------------------
# Chrome render + stdlib PNG measurement (two-pass machinery)
# ---------------------------------------------------------------------------
def render_html(chrome, html_path, png_path, width, height, scale=1):
    """Render html_path -> png_path with Chrome headless."""
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-sandbox",
        "--force-device-scale-factor=%s" % scale,
        "--window-size=%d,%d" % (width, height),
        "--screenshot=%s" % png_path,
        "file://%s" % html_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0 or not os.path.isfile(png_path):
        stderr = result.stderr.decode(errors="replace")
        raise RuntimeError("Chrome headless failed (rc=%s): %s"
                           % (result.returncode, stderr[:400]))


def get_image_size(path):
    """Read PNG width/height from the IHDR header (stdlib only)."""
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            raise ValueError("not a PNG file: %s" % path)
        f.read(4)
        if f.read(4) != b"IHDR":
            raise ValueError("missing IHDR: %s" % path)
        w = int.from_bytes(f.read(4), "big")
        h = int.from_bytes(f.read(4), "big")
    return w, h


def _decode_png_rows(path):
    """Decode a PNG into unfiltered scanlines using stdlib zlib. Supports
    8-bit truecolor (color type 2/6), non-interlaced -- Chrome screenshot
    output. Returns (w, h, channels, rows)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    w = h = bit_depth = color_type = None
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            w, h, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if bit_depth != 8 or color_type not in (2, 6):
        raise ValueError("unsupported PNG format bit_depth=%s color_type=%s"
                         % (bit_depth, color_type))
    channels = 3 if color_type == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    rows = []
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        filt = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if filt == 0:
            pass
        elif filt == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:  # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:  # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pp = a + b - c
                pa = abs(pp - a)
                pb = abs(pp - b)
                pc = abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise ValueError("bad filter %d" % filt)
        rows.append(bytes(line))
        prev = line
    return w, h, channels, rows


def measure_content_height(png_path, bg_hex):
    """Scan a rendered PNG bottom-up for the last row that is not pure
    background; return that content height. This makes the final canvas
    height data-driven (a longer quote extends the stack automatically)."""
    w, h, ch, rows = _decode_png_rows(png_path)
    br, bgc, bb = (int(bg_hex[i:i + 2], 16) for i in (1, 3, 5))
    for y in range(h - 1, -1, -1):
        line = rows[y]
        for x in range(0, w, 4):  # sample every 4th px for speed
            off = x * ch
            r, g, b = line[off], line[off + 1], line[off + 2]
            if abs(r - br) > 6 or abs(g - bgc) > 6 or abs(b - bb) > 6:
                return y + 1
    return h


# ---------------------------------------------------------------------------
# CardStack -- assembly + the R8 gate
# ---------------------------------------------------------------------------
class GateError(RuntimeError):
    """An RCT gate rejected the render (caller defect, not a render bug)."""


class CardStack:
    """Vertical stack of sibling cards on the theme ground.

    save(out_path) is the single mechanical R8 gate: width bounds, aspect
    gate at layout time, byte budget with the locked recovery order."""

    def __init__(self, width=RCT["width_std"], palette=None, font_dir=None,
                 chrome=None, log=sys.stderr):
        if not (RCT["width_min"] <= width <= RCT["width_max"]):
            raise GateError("render width %d outside [%d, %d]"
                            % (width, RCT["width_min"], RCT["width_max"]))
        self.width = width
        self.palette = dict(palette or CARD_LIGHT)
        self.font_dir = font_dir
        self.chrome = chrome
        self.log = log
        self.cards = []

    def add(self, card):
        self.cards.append(card)
        return self

    def _hero_count(self):
        n = 0
        for c in self.cards:
            kids = c.cards if isinstance(c, Row) else [c]
            n += sum(1 for k in kids
                     if isinstance(k, ValueCard) and k.hero)
        return n

    def _collect_css(self):
        seen, blocks = set(), []
        def walk(card):
            cls = type(card)
            if cls not in seen:
                seen.add(cls)
                if cls.EXTRA_CSS:
                    blocks.append(cls.EXTRA_CSS)
            if isinstance(card, Row):
                for k in card.cards:
                    walk(k)
        for c in self.cards:
            walk(c)
        return "\n".join(blocks)

    def html(self):
        if not self.cards:
            raise GateError("empty stack")
        if self._hero_count() > 1:
            raise GateError("more than one hero card in the stack "
                            "(the LG rung is a per-stack monopoly)")
        p = self.palette
        font_face, font_family = font_face_css(self.font_dir, self.log)
        cards_html = "\n".join(c.html(p) for c in self.cards)
        return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<style>
%(font_face)s

/* 3x3 type tokens -- generated from card_blocks.SIZES/WEIGHTS/ELEMENTS */
%(type_css)s

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  width: %(width)dpx;
  background: %(bg)s;
  font-family: %(font_family)s;
  color: %(text)s;
  -webkit-font-smoothing: antialiased;
}

.stack {
  display: flex;
  flex-direction: column;
  gap: %(stack_gap)dpx;
  padding: %(stack_pad)dpx;
  width: %(width)dpx;
}

.card {
  background: %(surface)s;
  border-radius: %(card_radius)dpx;
  padding: %(card_pad_v)dpx %(card_pad_h)dpx;
  position: relative;
}

%(extra_css)s
</style>
</head>
<body>
<div class="stack">
%(cards)s
</div>
</body>
</html>""" % {
            "font_face": font_face,
            "font_family": font_family,
            "type_css": type_css(),
            "width": self.width,
            "bg": p["bg"],
            "text": p["text"],
            "surface": p["surface"],
            "stack_gap": LAYOUT["stack_gap"],
            "stack_pad": LAYOUT["stack_pad"],
            "card_radius": LAYOUT["card_radius"],
            "card_pad_v": LAYOUT["card_pad_v"],
            "card_pad_h": LAYOUT["card_pad_h"],
            "extra_css": self._collect_css(),
            "cards": cards_html,
        }

    def save(self, out_path, keep_html=False):
        """Render the stack to out_path (PNG) through the R8 gate.
        Returns a report dict: width/height/ratio/bytes/path/gates."""
        chrome = self.chrome or find_chrome()
        out_path = os.path.abspath(out_path)
        html_path = out_path + ".html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(self.html())
        try:
            # Pass 1: render tall, measure the real content bottom.
            tall = int(self.width / RCT["aspect_min"]) + 480
            render_html(chrome, html_path, out_path, self.width, tall)
            content_h = measure_content_height(out_path, self.palette["bg"])
            final_h = content_h + LAYOUT["stack_pad"]

            # Aspect gate at layout time (height known, final render not
            # yet done). Out-of-range is a caller defect: split the stack
            # (too tall) or add content / narrow the canvas (too short).
            ratio = self.width / final_h
            if not (RCT["aspect_min"] <= ratio <= RCT["aspect_max"]):
                raise GateError(
                    "aspect gate FAIL: w:h = %d:%d = %.4f outside "
                    "[%.4f, %.4f] -- %s"
                    % (self.width, final_h, ratio,
                       RCT["aspect_min"], RCT["aspect_max"],
                       "split the stack" if ratio < RCT["aspect_min"]
                       else "stack too short for a card image"))

            # Pass 2: exact-height render.
            render_html(chrome, html_path, out_path, self.width, final_h)
            w, h = get_image_size(out_path)
            size = os.path.getsize(out_path)

            # Byte gate + locked recovery order.
            recovery = None
            if size > RCT["byte_target"]:
                size, recovery = self._recover_bytes(
                    chrome, html_path, out_path, final_h, size)
            report = {
                "path": out_path, "width": w, "height": h,
                "ratio": round(w / h, 4), "bytes": size,
                "aspect_gate": "PASS", "byte_gate": "PASS",
                "min_font_px": min(SIZES.values()),
                "recovery": recovery,
            }
            self.log.write(
                "[card_blocks] %dx%d ratio=%.4f (gate [%.4f,%.4f] PASS) "
                "%d KB (target <%d KB) min-font=%dpx (floor %d)\n"
                % (w, h, w / h, RCT["aspect_min"], RCT["aspect_max"],
                   size // 1024, RCT["byte_target"] // 1024,
                   min(SIZES.values()), FONT_FLOOR_PX))
            return report
        finally:
            if not keep_html and os.path.isfile(html_path):
                os.remove(html_path)

    def _recover_bytes(self, chrome, html_path, out_path, final_h, size):
        """Locked recovery order: (1) PNG-8 palette quantize (Pillow, when
        available), (2) downscale width toward the 720 floor via device
        scale factor (layout untouched), (3) error."""
        self.log.write("[card_blocks] byte target exceeded (%d KB); "
                       "recovering\n" % (size // 1024))
        try:
            from PIL import Image
            img = Image.open(out_path).convert("RGB")
            img.quantize(colors=256).save(out_path, optimize=True)
            size = os.path.getsize(out_path)
            if size <= RCT["byte_target"]:
                return size, "quantize-png8"
        except ImportError:
            self.log.write("[card_blocks] Pillow unavailable; skipping "
                           "quantize step\n")
        scale = RCT["width_min"] / self.width
        render_html(chrome, html_path, out_path,
                    RCT["width_min"], int(final_h * scale), scale=scale)
        size = os.path.getsize(out_path)
        if size <= RCT["byte_target"]:
            return size, "downscale-720"
        raise GateError("byte gate FAIL: %d bytes > %d after quantize + "
                        "downscale" % (size, RCT["byte_target"]))


# ---------------------------------------------------------------------------
# Self-test: generic ValueCard/QuoteCard stack (no consumer-specific
# layout; the reference-look reproduction lives in card_blocks_demo.py).
# ---------------------------------------------------------------------------
def _self_test(out_path):
    drifts = check_palette_drift()
    stack = CardStack()
    stack.add(ValueCard(label="Temperature", value="27&deg;", icon="cloudy",
                        meta="feels like 34&deg;", hero=True,
                        delta="+2&deg; vs yesterday", delta_color="red"))
    stack.add(Row(
        ValueCard(label="PM2.5", value="33", tag="max",
                  delta="min 11", delta_color="grade-good",
                  value_color="grade-ok"),
        ValueCard(label="PM10", value="35", tag="max",
                  delta="min 15", delta_color="grade-good",
                  value_color="grade-ok")))
    stack.add(ValueCard(label="Steps", value="8,412", unit="steps",
                        delta="+1,203 vs avg", delta_color="green"))
    stack.add(ValueCard(label="Sleep", value="6h 40m",
                        meta="23:50 - 06:30"))
    stack.add(QuoteCard(
        text="Success happens when preparation meets opportunity.",
        author="Seneca"))
    report = stack.save(out_path)
    print("self-test report: %s" % report)
    return 0 if not drifts else 1


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/card_blocks_selftest.png"
    sys.exit(_self_test(out))
