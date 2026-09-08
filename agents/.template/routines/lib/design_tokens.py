#!/usr/bin/env python3
# design_tokens.py -- Dogany design-system style token canon (v1, T1).
# Framework asset. Spec of record: docs/DESIGN-SYSTEM.md (canonical repo,
# dogany-agent). Origin: DGN-376 locked design (grill round 1, M5 two-layer
# schema).
#
# This module is the SINGLE SOURCE OF TRUTH for estate-wide visual style
# tokens. Every user-facing visual artifact (telegram cards, console CSS,
# README/docs diagrams, matplotlib chart themes) derives its palette from
# here instead of hardcoding hex values.
#
# Two-layer schema (grill M5):
#   Layer A  BRAND      -- brand-core family hues (named colors, measured
#                          from the incumbent card palette + README diagram
#                          set; see docs/DESIGN-SYSTEM.md for provenance).
#   Layer B  THEMES     -- per-surface theme mappings. Each theme fills the
#                          same 11 semantic slots (SLOTS below) either by
#                          referencing a Layer A name ("@name") or by a
#                          per-medium literal override ("#RRGGBB").
#
# Consumption:
#   Python:  from design_tokens import THEMES; THEMES["card-dark"]["accent"]
#            (or importlib.util from a walked-up routines/lib path, same
#            resolution pattern as the card scripts' _find_font_dir()).
#   CSS:     to_css_root("console-dark") -> ":root { --bg: ...; }" string
#            (console repo consumes a VENDORED COPY of this module plus a
#            drift lint, never a live cross-repo path -- grill M4).
#   JSON:    to_json() -> language-neutral export for lint/diff tooling.
#
# Python 3 stdlib only. No third-party imports.
#
# Run self-check: python3 design_tokens.py          (exit 0 = pass)
# Dump JSON:      python3 design_tokens.py --json
# Anchor table:   python3 design_tokens.py --anchors  (generated reference table)
# Pick gate:      python3 design_tokens.py --gate --accent <name> \
#                   --bg <name|#RRGGBB> [--crew <crew>] [--leader] \
#                   [--taken a,b]      (exit 0 = allowed, 1 = rejected)

import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Layer A -- brand core (family hues)
#
# Canonicalized by measurement, not invention: values are the incumbent
# palette of the shipped card scripts (morning_brief_card.py /
# diet-log/card.py) plus the README diagram set (docs/img/*.png pixel
# sample). The three signature hues (teal / amber / coral) on a navy dark
# ground are the Dogany brand identity.
# ---------------------------------------------------------------------------

BRAND = {
    # signature hues
    "teal":        "#4ECDC4",  # primary accent (cards ACCENT)
    "amber":       "#FFD166",  # warm highlight (cards WARM)
    "coral":       "#FF6B6B",  # alert / intake / negative accent
    # secondary hues
    "mint":        "#A8E6CF",  # soft green (macro fat / dinner)
    "orange":      "#FF9F43",  # burn / exercise accent
    "green":       "#2ECC71",  # positive state
    "purple":      "#C9A7EB",  # lavender (README routing diagram incumbent)
    # navy dark ground family
    "navy":        "#13132B",  # card background
    "navy-panel":  "#1A1A35",  # raised panel
    "navy-quote":  "#1C1C3A",  # quote / inset block
    "navy-seg":    "#1C1C38",  # inset bar segment (diet exercise track)
    "navy-track":  "#2A2A50",  # progress track base
    "navy-line":   "#2A2A55",  # separator / panel edge
    # ink family (text on navy)
    "ink":         "#FFFFFF",  # primary text
    "ink-soft":    "#C4D4FF",  # secondary text / labels
    "ink-value":   "#DDDDEE",  # value text
    "grey":        "#888899",  # fallback / unknown-category grey
    # air-quality / status grade scale (measured from the brief card)
    "grade-good":  "#2ECC71",  # good (== green)
    "grade-ok":    "#A8D86E",  # normal
    "grade-bad":   "#95A5A6",  # bad
    "grade-vbad":  "#636E72",  # very bad
}

# Font tokens (measured from card scripts; files ship in
# .claude/skills-bundle/diet-log/fonts/, resolved via the _find_font_dir()
# walk-up pattern).
FONTS = {
    "family":    "ASDGN",
    "medium":    "ASDGN_Medium.ttf",
    "extrabold": "ASDGN_ExtraBold.ttf",
}

# ---------------------------------------------------------------------------
# Layer B -- surface theme mappings
#
# Semantic slot contract: every theme fills exactly these 11 slots. The
# slot list is the measured console :root variable set (views.py), adopted
# estate-wide as the semantic vocabulary.
# ---------------------------------------------------------------------------

SLOTS = (
    "bg", "surface", "border", "text", "muted",
    "accent", "green", "yellow", "red", "purple", "orange",
)

# Spec values: "@name" = Layer A reference, "#RRGGBB" = per-medium override.
_THEME_SPECS = {
    # Telegram card artifacts (matplotlib-rendered PNG cards). Pure brand
    # derivation -- this is the brand-core surface.
    "card-dark": {
        "bg":      "@navy",
        "surface": "@navy-panel",
        "border":  "@navy-line",
        "text":    "@ink",
        "muted":   "@ink-soft",
        "accent":  "@teal",
        "green":   "@green",
        "yellow":  "@amber",
        "red":     "@coral",
        "purple":  "@purple",
        "orange":  "@orange",
    },
    # Console web UI themes (console-dark / console-light /
    # console-dark-minimal) are registered BELOW, after the Layer C tuning
    # machinery they derive from (DGN-376 v2 -- the reskin decision: the old
    # alien GitHub-dark register is retired, the console joins the brand
    # surfaces).
    # README / docs diagrams (docs/img/*.png). Same family hues as the
    # brand core, muted print-like variants on a deep teal ground (measured
    # from the shipped diagram set; v1 = palette compliance + manual
    # regeneration, no render pipeline yet).
    "diagram": {
        "bg":      "#074A5A",
        "surface": "#07333F",
        "border":  "#F4F1E9",  # cream stroke is the diagram border language
        "text":    "#F4F1E9",
        "muted":   "#85A2A6",
        "accent":  "#6FD0C0",
        "green":   "@mint",    # no measured incumbent; brand-derived
        "yellow":  "#E8B15A",
        "red":     "#E8735A",
        "purple":  "#C9A7EB",
        "orange":  "@orange",  # no measured incumbent; brand-derived
    },
    # Card-block image artifacts on the lifekit light ground (DGN-660
    # reference look; consumed by card_blocks.CARD_LIGHT). Mechanical slots
    # pin the output of this module's own derivation chain as literals
    # (console-light convention); the AA-tuned declared overrides carry the
    # DGN-660 measured values. card_blocks re-derives the mechanical pins at
    # self-test (drift guard).
    "card-light": {
        "bg":      "#DBF1EC",  # BACKGROUNDS["teal-pale"]
        "surface": "#D3E9E4",  # card_surface(bg)
        "border":  "#C0D8D2",  # declared override (subtle border)
        "text":    "@navy",    # ink-on-light (~13:1 vs surface)
        "muted":   "#286E60",  # declared override (~5.2:1 vs surface)
        "accent":  "#176A60",  # accent_for("teal", "teal-pale")
        "green":   "#19703E",  # tune_accent(BRAND green, surface)
        "yellow":  "#8A5A00",  # declared override, warning amber (AA-checked)
        "red":     "#B03030",  # declared override, alert red (AA-checked)
        # purple/orange: brand-derived; card renders do not consume them.
        "purple":  "@purple",
        "orange":  "@orange",
    },
}

_HEX_RE = re.compile(r"^#[0-9A-F]{6}$")


def _resolve_value(value):
    """Resolve one spec value: '@name' -> BRAND lookup, '#RRGGBB' -> as-is."""
    if value.startswith("@"):
        name = value[1:]
        if name not in BRAND:
            raise KeyError("unknown brand token reference: %s" % value)
        return BRAND[name]
    return value


def _resolve_theme(spec):
    return {slot: _resolve_value(spec[slot]) for slot in SLOTS}


# Resolved themes THEMES ({theme_name: {slot: "#RRGGBB"}}) are built AFTER
# the console theme registration below -- the console-dark accent derivation
# needs the Layer C tuning machinery (tune_accent). Consumers read THEMES
# (or theme()), never _THEME_SPECS.


def theme(name):
    """Return a copy of a resolved theme dict (safe to mutate)."""
    return dict(THEMES[name])


# ---------------------------------------------------------------------------
# Layer C -- crew / agent color identity (DGN-376, locked 2026-07-28)
#
# A DISTINCT namespace from the Layer A BRAND card palette above. These are the
# estate IDENTITY colors: crew background surfaces + per-agent accents, governed
# by one rule -- "hue fixed + per-surface lightness anchor". An accent's shade
# is COMPUTED per chosen surface (tune_accent), never hardcoded, so any accent
# on any allowed background either clears WCAG AA (4.5:1) on its real card or is
# an explicit exclusion (never a sub-threshold shade). Contrast uses real WCAG
# sRGB-gamma luminance. Mode (dark/light) is derived once from the CARD's
# luminance (derive card first, then decide), not a page-vs-card straddle.
# Spec of record: color-system-spec.md (Metal files/docs) + docs/DESIGN-SYSTEM.md.
# ---------------------------------------------------------------------------

# 15 allowed agent accent base hues. Intentionally separate from the BRAND card
# hues: a user picks identity from this validated set; the card render palette
# is a different concern (do not unify without a deliberate decision).
ACCENT_PALETTE = {
    "teal":    "#2DD4BF",
    "aqua":    "#22D3EE",
    "sky":     "#38BDF8",
    "cobalt":  "#3B82F6",
    "indigo":  "#6366F1",
    "violet":  "#8B5CF6",
    "orchid":  "#C084FC",
    "magenta": "#E879F9",
    "rose":    "#FB7185",
    "coral":   "#FB923C",
    "amber":   "#FBBF24",
    "lime":    "#A3E635",
    "emerald": "#34D399",
    "pink":    "#F472B6",
    "peri":    "#A5B4FC",
}

# 8 allowed background surfaces. All are clearly dark OR clearly light; mid-
# luminance (~50% grey) is FORBIDDEN -- it is a contrast dead zone where no
# accent can clear threshold.
BACKGROUNDS = {
    "black":     {"hex": "#000000", "tier": "dark",  "role": None},
    "ink":       {"hex": "#0F1117", "tier": "dark",  "role": "neutral"},
    "navy":      {"hex": "#13132B", "tier": "dark",  "role": "crew:devkit"},
    "teal-deep": {"hex": "#0C2926", "tier": "dark",  "role": "crew:lifekit"},
    "white":     {"hex": "#FFFFFF", "tier": "light", "role": None},
    "cloud":     {"hex": "#EDEFF3", "tier": "light", "role": "neutral"},
    "navy-pale": {"hex": "#E3E9F6", "tier": "light", "role": "crew:devkit"},
    "teal-pale": {"hex": "#DBF1EC", "tier": "light", "role": "crew:lifekit"},
}

# Air-quality grade scale measured on the card-light surface (DGN-660).
# NOT part of the 11-slot theme contract: card_blocks assembles these onto
# its grade-* palette slots next to the card-light theme.
GRADE_LIGHT = {
    "good": "#19703E",  # == card-light green
    "ok":   "#546C37",  # tune_accent(BRAND grade-ok, card-light surface)
    "bad":  "#706B5A",  # declared override (AA-checked)
    "vbad": "#5C5450",  # declared override (AA-checked)
}

# Crew default background pair (owner-overridable to any BACKGROUNDS entry).
CREW_BG_DEFAULT = {
    "devkit":  {"dark": "navy",      "light": "navy-pale"},
    "lifekit": {"dark": "teal-deep", "light": "teal-pale"},
}

# Crew identity accent: the palette hue a crew's LEADER wears (also the crew's
# accent identity). Navy is a background hue, not a legible accent and not in the
# palette, so devkit's accent identity is cobalt (its blue-family palette
# representative); lifekit's is teal (same family as its teal ground).
# DGN-376 leader/domain reconciliation (2026-07-28).
CREW_ACCENT = {
    "devkit": "cobalt",
    "lifekit": "teal",
}

SYSTEM_DEFAULT_ACCENT = "teal"

# --- Tuning / legibility constants (all empirically picked; see color-system-
# spec.md "Numeric thresholds" for the justification of each) -----------------
#
# WCAG AA normal-text threshold, applied to BOTH tiers. The earlier build used
# 4.0 on dark, which is not real AA; with proper sRGB-gamma luminance (below)
# the curated dark cards sit near-black so bright accents clear 4.5 easily, so
# there is no reason to keep the softer dark target.
AA_CONTRAST = 4.5

# Over-tuning cap: a tuned shade may never land within this Euclidean RGB
# distance of pure white / pure black. On tinted grounds an unbounded tune can
# march an accent all the way to near-white, collapsing distinct identities into
# the same near-white. If the AA target cannot be met before the cap is hit, the
# pair is EXCLUDED (accent_for returns None). On the 8 curated backgrounds the
# nearest a tuned shade gets to its toward-color is ~117 units, so the cap never
# actually excludes a curated pair -- it is a guard for custom / tinted grounds.
OVER_TUNE_CAP = 40.0

# Perceptual distinctiveness gate (CIELAB CIE76 dE). A candidate accent is
# rejected if its dE to a taken accent is below this. Chosen just under the
# smallest inter-hue distance in the 15-palette (indigo<->violet = 14.5) so all
# 15 base hues remain mutually pickable, while any near-duplicate is rejected;
# adjacent families (cobalt/indigo/sky, 22+) stay comfortably distinct. The
# tightest tuned same-background pair observed is 10.3 dE, so 10 also holds as
# the same-surface non-collapse floor.
DISTINCT_DE = 10.0

# Background validity dead-band (real-WCAG luminance). A custom background whose
# luminance falls inside [lo, hi] is rejected: it is neither clearly dark nor
# clearly light, so no accent can be reliably tuned to AA on it. All 8 curated
# backgrounds sit outside this band (darkest light = navy-pale 0.813, brightest
# dark = teal-deep 0.018); a mid-grey like #808080 (0.216) is correctly rejected.
BG_DEADBAND = (0.06, 0.55)


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return "#%02X%02X%02X" % tuple(int(round(c)) for c in rgb)


def _linearize(c):
    """sRGB gamma expansion of one 0..255 channel to linear 0..1."""
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb):
    """Real WCAG relative luminance (0..1): sRGB-gamma linearize each channel,
    then 0.2126 R + 0.7152 G + 0.0722 B. This replaces the earlier plain
    normalized-channel approximation (which was mislabeled 'WCAG')."""
    r, g, b = (_linearize(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    la = relative_luminance(a) + 0.05
    lb = relative_luminance(b) + 0.05
    return max(la, lb) / min(la, lb)


def is_dark_surface(hex_color):
    return relative_luminance(_hex_to_rgb(hex_color)) < 0.5


def _rgb_to_lab(rgb):
    """sRGB (0..255) -> CIELAB (D65). Used for perceptual distinctiveness."""
    r, g, b = (_linearize(c) for c in rgb)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    x, y, z = x / 0.95047, y / 1.0, z / 1.08883

    def f(t):
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t + 16.0 / 116.0)
    fx, fy, fz = f(x), f(y), f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def delta_e(hex_a, hex_b):
    """CIE76 perceptual color difference (dE) between two hex colors."""
    la = _rgb_to_lab(_hex_to_rgb(hex_a))
    lb = _rgb_to_lab(_hex_to_rgb(hex_b))
    return sum((la[i] - lb[i]) ** 2 for i in range(3)) ** 0.5


def _blend(c, t, f):
    return tuple(round(c[i] + (t[i] - c[i]) * f) for i in range(3))


def tune_accent(base_hex, card_hex):
    """Tuning algorithm: shift the base accent toward white (on a dark card) or
    black (on a light card) in 0.05 steps until text contrast vs the card clears
    AA (4.5:1), returning the first passing shade AS HEX. Two guards:
      - over-tuning cap: a shade that would land within OVER_TUNE_CAP of pure
        white/black is rejected (identity would collapse to near-white/black);
      - if AA cannot be reached before the cap, the pair is EXCLUDED -> returns
        None (the caller / picker must drop that accent x background pair).
    Real WCAG luminance is used throughout."""
    base = _hex_to_rgb(base_hex)
    dark = is_dark_surface(card_hex)
    toward = (255, 255, 255) if dark else (0, 0, 0)
    card = _hex_to_rgb(card_hex)
    f = 0.0
    while f <= 0.95 + 1e-9:
        tuned = _blend(base, toward, f) if f > 0 else base
        dist_to_toward = sum((tuned[i] - toward[i]) ** 2 for i in range(3)) ** 0.5
        if dist_to_toward < OVER_TUNE_CAP:
            return None  # would collapse into near-white/black -> exclude
        if contrast_ratio(tuned, card) >= AA_CONTRAST:
            return _rgb_to_hex(tuned)
        f += 0.05
    return None  # AA unreachable within cap -> excluded pair


def card_surface(bg_hex):
    """Derive the raised card surface from a page background. Dark bg: lighten
    +16/chan (cap 255); light bg: darken -8/chan (floor 0); pure white -> a
    faint grey card. THE single card definition -- reference anchors are a
    generated artifact of accent_for over this card, never a separate fixed
    tile (the old _REF_CARD_* fixed-card table contradicted the real render
    card and is removed)."""
    if bg_hex.upper() == "#FFFFFF":
        return "#F5F6F9"
    rgb = _hex_to_rgb(bg_hex)
    if is_dark_surface(bg_hex):
        rgb = tuple(min(255, c + 16) for c in rgb)
    else:
        rgb = tuple(max(0, c - 8) for c in rgb)
    return _rgb_to_hex(rgb)


def accent_for(accent_name, bg_name):
    """Tuned accent hex for an accent on a given allowed background, or None if
    the pair is excluded (cannot reach AA within the over-tuning cap). Raises a
    loud KeyError on an unknown accent or background name -- never silently
    resolves to a default."""
    if accent_name not in ACCENT_PALETTE:
        raise KeyError("unknown accent: %r (not in ACCENT_PALETTE)" % (accent_name,))
    if bg_name not in BACKGROUNDS:
        raise KeyError("unknown background: %r (not in BACKGROUNDS)" % (bg_name,))
    return tune_accent(ACCENT_PALETTE[accent_name],
                       card_surface(BACKGROUNDS[bg_name]["hex"]))


# Default crew surface per tier, used to GENERATE the published reference anchor
# table (dark default = navy, light default = navy-pale). One source of truth =
# accent_for over card_surface(bg); no hand-copied hex.
_DEFAULT_BG = {"dark": "navy", "light": "navy-pale"}


def chip_needs_outline(base_hex, card_hex):
    """A chip is the vivid BASE hue drawn as a filled block on the card. On many
    light cards the base hue is near-invisible (~1.1:1). Return True when the
    chip fails the WCAG non-text / graphical-object threshold (3:1) vs the card,
    so the render must draw a 1px outline ring (in the tuned anchor color) to
    guarantee the shape reads while keeping the vivid identity fill."""
    return contrast_ratio(_hex_to_rgb(base_hex), _hex_to_rgb(card_hex)) < 3.0


def validate_background(hex_color):
    """Reject a custom background whose real-WCAG luminance falls in the mid
    dead-band [BG_DEADBAND] -- neither clearly dark nor light, so no accent can
    be reliably tuned to AA. Raises ValueError on a bad hex or a dead-band
    color; returns the tier ('dark' | 'light') on success."""
    if not _HEX_RE.match(hex_color.upper()):
        raise ValueError("bad background hex: %r" % (hex_color,))
    lum = relative_luminance(_hex_to_rgb(hex_color))
    lo, hi = BG_DEADBAND
    if lo <= lum <= hi:
        raise ValueError(
            "background luminance %.3f in forbidden dead-band [%.2f, %.2f]: %s"
            % (lum, lo, hi, hex_color))
    return "dark" if lum < 0.5 else "light"


def accent_distinct(candidate_name, taken_names, crew=None):
    """Perceptual distinctiveness gate (replaces the old exact-name collision).
    Reject the candidate accent if its base-hue dE (CIE76) to ANY already-taken
    domain accent OR to the crew LEADER accent is below DISTINCT_DE. Enforces
    both leader-vs-domain and domain-vs-domain separation. Returns True if the
    candidate is distinct enough to allow. Raises KeyError on unknown names."""
    if candidate_name not in ACCENT_PALETTE:
        raise KeyError("unknown accent: %r" % (candidate_name,))
    cand_hex = ACCENT_PALETTE[candidate_name]
    compare = list(taken_names)
    if crew is not None:
        leader = crew_accent(crew)
        if leader is not None:
            compare.append(leader)
    for other in compare:
        if other == candidate_name:
            return False  # exact reuse is never distinct
        if other not in ACCENT_PALETTE:
            raise KeyError("unknown taken accent: %r" % (other,))
        if delta_e(cand_hex, ACCENT_PALETTE[other]) < DISTINCT_DE:
            return False
    return True


def crew_accent(crew):
    """Accent palette-name a crew's leader wears; None for an unknown crew."""
    return CREW_ACCENT.get(crew)


def resolve_accent(owner_override=None, own_color=None, is_leader=False,
                   crew=None, kit_domain_color=None):
    """Resolve an agent's effective accent palette-name by the locked precedence:
    owner override > agent's own color > (leader: crew accent / domain: kit domain
    color) > system default (teal). Unknown/None candidates are skipped; only
    ACCENT_PALETTE names are honored."""
    role_default = crew_accent(crew) if is_leader else kit_domain_color
    for cand in (owner_override, own_color, role_default, SYSTEM_DEFAULT_ACCENT):
        if cand and cand in ACCENT_PALETTE:
            return cand
    return SYSTEM_DEFAULT_ACCENT


def reference_anchor(accent_name, tier):
    """Published reference anchor for the DEFAULT crew surface of a tier
    ('dark' -> navy background, 'light' -> navy-pale background). GENERATED from
    accent_for over the default background's card -- the same code path the
    render uses -- so the published table can never diverge from the real card.
    Returns the tuned hex, or None if the pair is excluded at AA."""
    if tier not in _DEFAULT_BG:
        raise ValueError("unknown tier: %r (expected 'dark'|'light')" % (tier,))
    return accent_for(accent_name, _DEFAULT_BG[tier])


def accent_conflicts_with_crew(accent_name, crew, is_leader=False):
    """Distinctiveness rule (NOT legibility -- tune_accent already guarantees
    contrast on any surface, so a leader wearing the crew hue still reads). A crew
    LEADER is meant to wear the crew accent, so a leader never conflicts. A DOMAIN
    agent must not sit too close to its crew's leader accent. Delegates to the
    perceptual gate (accent_distinct): a domain accent conflicts when it is within
    DISTINCT_DE of the crew leader accent (which includes the exact-hue case).
    DGN-376 leader/domain reconciliation; supersedes the earlier name-equality
    rule. Raises KeyError on an unknown accent name (loud, not silent)."""
    if accent_name not in ACCENT_PALETTE:
        raise KeyError("unknown accent: %r" % (accent_name,))
    if is_leader:
        return False
    if crew not in CREW_ACCENT:
        raise KeyError("unknown crew: %r" % (crew,))
    # conflict == NOT distinct from the crew leader accent
    return not accent_distinct(accent_name, [], crew=crew)


def _valid_accents(bg_hex, crew=None, is_leader=False, taken=()):
    """Accent names still pickable on a given background under the full gate
    (crew distinctiveness + peer distance + AA-exclusion). Feeds the FAIL
    feedback so a rejected pick immediately carries its re-selection list."""
    card = card_surface(bg_hex)
    out = []
    for name in ACCENT_PALETTE:
        if crew is not None and accent_conflicts_with_crew(name, crew, is_leader):
            continue
        if taken and not accent_distinct(name, list(taken)):
            continue
        if tune_accent(ACCENT_PALETTE[name], card) is None:
            continue
        out.append(name)
    return out


def gate_pick(accent_name, bg, crew=None, is_leader=False, taken=()):
    """Onboarding / identity-change color pick gate (DGN-650, DESIGN-SYSTEM R3
    row 5). The ONE mechanical enforcement point for confirming an agent
    accent + background pair. Checks, in order:
      1. background: validate_background (bad hex / mid-luminance dead-band);
         `bg` is a curated BACKGROUNDS name or a custom #RRGGBB hex.
      2. crew distinctiveness: accent_conflicts_with_crew (leader exempt).
      3. peer distance: accent_distinct vs already-taken peer accents.
      4. AA exclusion: tune_accent must reach AA within the over-tune cap.
    Returns (ok, lines): ok bool, lines = printable feedback. On FAIL the
    lines carry the reason AND the remaining valid choices (instant
    re-selection feedback, D6). User-driven inputs (unknown accent, bad or
    dead-band background) FAIL with guidance; caller-config inputs (unknown
    crew / unknown taken name) raise KeyError -- loud, matching the helper
    contract."""
    bg_name = bg if bg in BACKGROUNDS else None
    bg_hex = BACKGROUNDS[bg_name]["hex"] if bg_name else bg
    try:
        tier = validate_background(bg_hex)
    except ValueError as exc:
        return False, [
            "FAIL BACKGROUND: %s" % exc,
            "allowed backgrounds: %s" % ", ".join(sorted(BACKGROUNDS)),
        ]
    if accent_name not in ACCENT_PALETTE:
        return False, [
            "FAIL ACCENT-UNKNOWN: %r is not in the accent palette"
            % (accent_name,),
            "valid accents: %s"
            % ", ".join(_valid_accents(bg_hex, crew, is_leader, taken)),
        ]
    if crew is not None and accent_conflicts_with_crew(accent_name, crew,
                                                       is_leader):
        return False, [
            "FAIL ACCENT-CREW: %r is within dE %.0f of crew %r leader "
            "accent %r" % (accent_name, DISTINCT_DE, crew, crew_accent(crew)),
            "valid accents: %s"
            % ", ".join(_valid_accents(bg_hex, crew, is_leader, taken)),
        ]
    if taken and not accent_distinct(accent_name, list(taken)):
        return False, [
            "FAIL ACCENT-PEER: %r is within dE %.0f of a taken peer accent "
            "(taken: %s)" % (accent_name, DISTINCT_DE, ", ".join(taken)),
            "valid accents: %s"
            % ", ".join(_valid_accents(bg_hex, crew, is_leader, taken)),
        ]
    card = card_surface(bg_hex)
    tuned = tune_accent(ACCENT_PALETTE[accent_name], card)
    if tuned is None:
        return False, [
            "FAIL EXCLUDED: %r on %s cannot reach AA %.1f within the "
            "over-tune cap" % (accent_name, bg_name or bg_hex, AA_CONTRAST),
            "valid accents: %s"
            % ", ".join(_valid_accents(bg_hex, crew, is_leader, taken)),
        ]
    outline = chip_needs_outline(ACCENT_PALETTE[accent_name], card)
    return True, [
        "PASS: accent=%s bg=%s tier=%s anchor=%s chip_outline=%s"
        % (accent_name, bg_name or bg_hex, tier, tuned,
           "yes" if outline else "no"),
    ]


# ---------------------------------------------------------------------------
# Layer B (continued) -- console themes + design resolver (DGN-376 v2,
# locked 2026-07-31)
#
# The console reskin decision is MADE: the console web UI leaves the alien
# GitHub-dark register and joins the brand surfaces. Three registered themes:
#
#   console-dark          brand navy card family (like card-dark) with the
#                         devkit crew accent COBALT instead of teal (D2).
#                         The accent shade is COMPUTED by tune_accent() on
#                         the navy-panel surface (AA 4.5), never hardcoded.
#   console-light         devkit navy-pale ground (candidate C literals).
#   console-dark-minimal  neutral ink ground, low chroma (candidate B
#                         literals). Registered but NOT mapped from any user
#                         setting value (D1).
#
# User-facing selection is a coarse tier setting resolved by resolve_design()
# (D1): "dark" -> console-dark (default), "light" -> console-light.
# ---------------------------------------------------------------------------

_THEME_SPECS["console-dark"] = {
    "bg":      "@navy",
    "surface": "@navy-panel",
    "border":  "@navy-line",
    "text":    "@ink",
    "muted":   "@ink-soft",
    # Devkit crew accent on the console: cobalt tuned on the navy-panel
    # surface (D2). Computed at import -- never a hand-pinned hex. (The
    # cobalt base already clears AA 4.5 here, so the tune returns the base.)
    "accent":  tune_accent(ACCENT_PALETTE[CREW_ACCENT["devkit"]],
                           BRAND["navy-panel"]),
    "green":   "@green",
    "yellow":  "@amber",
    "red":     "@coral",
    "purple":  "@purple",
    "orange":  "@orange",
}

# console-light: candidate C literals (dgn376 v2 candidates doc). Provenance:
# every value is the output of this module's own derivation chain over
# BACKGROUNDS["navy-pale"] (#E3E9F6) -- surface = card_surface(bg); accent +
# status hues = ACCENT_PALETTE entries darkened by tune_accent() vs that
# surface (light-tier rule); text = brand @navy as ink-on-light. Pinned as
# literals; the self-tests re-derive them to guard drift.
_THEME_SPECS["console-light"] = {
    "bg":      "#E3E9F6",  # BACKGROUNDS["navy-pale"]
    "surface": "#DBE1EE",  # card_surface(bg)
    "border":  "#B8C2D9",
    "text":    "@navy",    # ink-on-light            13.86:1 vs surface
    "muted":   "#5B6178",  #                          4.68:1
    "accent":  "#2C62B8",  # tune_accent(cobalt)      4.52:1
    "green":   "#1A6A4D",  # tune_accent(emerald)     4.99:1
    "yellow":  "#715610",  # tune_accent(amber)       5.27:1
    "red":     "#974450",  # tune_accent(rose)        4.92:1
    "purple":  "#6F4AC5",  # tune_accent(violet)      4.63:1
    "orange":  "#8A5021",  # tune_accent(coral)       4.93:1
}

# console-dark-minimal: candidate B literals (dgn376 v2 candidates doc).
# Provenance: bg = BACKGROUNDS["ink"] (#0F1117); surface = card_surface(bg);
# accent = peri (lowest-chroma palette hue); status hues = ACCENT_PALETTE
# entries via tune_accent() vs the surface (all pass at base shade);
# text/muted = neutral greys. Registered, not user-mapped (D1).
_THEME_SPECS["console-dark-minimal"] = {
    "bg":      "#0F1117",  # BACKGROUNDS["ink"]
    "surface": "#1F2127",  # card_surface(bg)
    "border":  "#2A2E36",
    "text":    "#D7DAE0",  # 11.49:1 vs surface
    "muted":   "#8B9098",  #  5.01:1
    "accent":  "#A5B4FC",  # peri     8.07:1
    "green":   "#34D399",  # emerald  8.37:1
    "yellow":  "#FBBF24",  # amber    9.64:1
    "red":     "#FB7185",  # rose     5.98:1
    "purple":  "#C084FC",  # orchid   6.09:1
    "orange":  "#FB923C",  # coral    7.11:1
}

# Resolved themes: {theme_name: {slot: "#RRGGBB"}}. This is the consumer
# surface -- consumers read THEMES (or theme()), never _THEME_SPECS.
THEMES = {name: _resolve_theme(spec) for name, spec in _THEME_SPECS.items()}

# Console values that move with the theme but are NOT slots (the SLOTS
# contract stays at 11): the inset ground (pre.log / pre.doc / textarea /
# input / code.hash), the translucent wash alpha levels (hex alpha suffixes
# applied to accent-tinted badges/washes), and a low-emphasis "wip" hue.
# wip is COMPUTED from the theme accent at import -- blended 0.30 toward the
# theme's own ground pole (black on dark, white on light) so it reads as a
# faded accent -- never a hand-picked hex.
_WASH_ALPHAS = {
    "dark":  {"wash_alpha": "33", "soft_alpha": "22",
              "chip_alpha": "1C", "faint_alpha": "11"},
    "light": {"wash_alpha": "22", "soft_alpha": "1A",
              "chip_alpha": "14", "faint_alpha": "0D"},
}

_CONSOLE_INSETS = {
    "console-dark":         "#0E0E22",
    "console-light":        "#FFFFFF",
    "console-dark-minimal": "#0A0C10",
}

_WIP_BLEND = 0.30


def _console_extras(theme_name):
    slots = THEMES[theme_name]
    dark = is_dark_surface(slots["surface"])
    pole = (0, 0, 0) if dark else (255, 255, 255)
    wip = _rgb_to_hex(_blend(_hex_to_rgb(slots["accent"]), pole, _WIP_BLEND))
    extras = {"inset": _CONSOLE_INSETS[theme_name], "wip": wip}
    extras.update(_WASH_ALPHAS["dark" if dark else "light"])
    return extras


CONSOLE_EXTRAS = {name: _console_extras(name) for name in _CONSOLE_INSETS}

# User-setting resolver (D1): the user stores a coarse tier; the resolver
# maps it to a registered console theme. console-dark-minimal is deliberately
# absent from the map (registered but unreachable from user values).
DESIGN_SETTINGS = {"dark": "console-dark", "light": "console-light"}
DESIGN_DEFAULT = "dark"


def resolve_design(setting=None):
    """Map a user-facing design setting (coarse tier) to a registered console
    theme: {"name", "slots" (the 11 resolved slots), "extras" (the theme's
    CONSOLE_EXTRAS entry)}. The input is USER data: normalized via
    strip().lower(); unknown / empty / None (or any non-string) falls back to
    DESIGN_DEFAULT. MUST NOT raise on user input (unlike the programmer-facing
    lookups in this module, which raise loudly)."""
    key = setting.strip().lower() if isinstance(setting, str) else ""
    name = DESIGN_SETTINGS.get(key, DESIGN_SETTINGS[DESIGN_DEFAULT])
    return {"name": name, "slots": theme(name),
            "extras": dict(CONSOLE_EXTRAS[name])}


def to_css_root(name="console-dark", indent="    "):
    """Render a theme as a CSS :root block (console views.py pattern)."""
    t = THEMES[name]
    lines = ["%s--%s: %s;" % (indent, slot, t[slot].lower()) for slot in SLOTS]
    return ":root {\n%s\n}" % "\n".join(lines)


def to_json():
    """Language-neutral export (vendoring / drift-lint surface)."""
    return json.dumps(
        {"brand": BRAND, "fonts": FONTS, "themes": THEMES,
         "accents": ACCENT_PALETTE, "backgrounds": BACKGROUNDS,
         "crew_bg_default": CREW_BG_DEFAULT, "crew_accent": CREW_ACCENT},
        indent=2, sort_keys=True,
    )


# The bundled TTFs live under the diet-log skill bundle; every card script
# resolved them by walking up to <agent_root>/.claude/skills-bundle/diet-log/
# fonts/. Centralize that walk here so card scripts consume FONTS through one
# token-owned resolver instead of each re-deriving the path (DGN-376 T3).
_FONT_BUNDLE_REL = os.path.join(
    ".claude", "skills-bundle", "diet-log", "fonts"
)


def find_font_dir(start=None):
    """Locate the bundled font dir, or None. AGENT_ROOT env first, then a
    walk up from `start` (default: this module's dir). Same lookup shape as
    the card scripts' former _find_font_dir()."""
    agent_root = os.environ.get("AGENT_ROOT", "")
    if agent_root:
        cand = os.path.join(agent_root, _FONT_BUNDLE_REL)
        if os.path.isdir(cand):
            return cand
    base = start or os.path.dirname(os.path.abspath(__file__))
    parent = base
    for _ in range(6):
        cand = os.path.join(parent, _FONT_BUNDLE_REL)
        if os.path.isdir(cand):
            return cand
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    return None


def font_paths(start=None):
    """Return (medium_ttf, extrabold_ttf) absolute paths, or ('', '') if the
    bundle is not found. Names come from the FONTS token block."""
    fdir = find_font_dir(start)
    if not fdir:
        return "", ""
    return (
        os.path.join(fdir, FONTS["medium"]),
        os.path.join(fdir, FONTS["extrabold"]),
    )


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def _self_check():
    errors = []
    # Layer A: every brand value is canonical uppercase hex.
    for name, value in BRAND.items():
        if not _HEX_RE.match(value):
            errors.append("brand %s: bad hex %r" % (name, value))
    # Layer B: exact slot coverage, all values resolve to valid hex.
    for tname, spec in _THEME_SPECS.items():
        missing = set(SLOTS) - set(spec)
        extra = set(spec) - set(SLOTS)
        if missing:
            errors.append("theme %s: missing slots %s" % (tname, sorted(missing)))
        if extra:
            errors.append("theme %s: extra slots %s" % (tname, sorted(extra)))
        for slot, value in spec.items():
            try:
                resolved = _resolve_value(value)
            except KeyError as exc:
                errors.append("theme %s.%s: %s" % (tname, slot, exc))
                continue
            if not _HEX_RE.match(resolved):
                errors.append("theme %s.%s: bad hex %r" % (tname, slot, resolved))
    # DGN-376 v2 invariant (RETIRES the M5 card/console zero-overlap net,
    # which was explicitly scoped "while the console reskin decision is
    # pending" -- v2 IS that decision): every console-* theme must clear AA
    # (4.5:1) for text/muted/accent and all 5 status slots vs its surface,
    # and its text must clear AA vs the theme's inset ground.
    for tname, tvals in THEMES.items():
        if not tname.startswith("console-"):
            continue
        surface = _hex_to_rgb(tvals["surface"])
        for slot in ("text", "muted", "accent",
                     "green", "yellow", "red", "purple", "orange"):
            if contrast_ratio(_hex_to_rgb(tvals[slot]), surface) < AA_CONTRAST:
                errors.append("console theme %s.%s below AA %.1f vs surface"
                              % (tname, slot, AA_CONTRAST))
        inset = _hex_to_rgb(CONSOLE_EXTRAS[tname]["inset"])
        if contrast_ratio(_hex_to_rgb(tvals["text"]), inset) < AA_CONTRAST:
            errors.append("console theme %s: text below AA %.1f vs inset"
                          % (tname, AA_CONTRAST))
    # Resolver sanity: every mapped setting targets a registered console
    # theme that also carries extras.
    for setting, tname in DESIGN_SETTINGS.items():
        if tname not in THEMES or tname not in CONSOLE_EXTRAS:
            errors.append("DESIGN_SETTINGS %r -> unregistered theme %r"
                          % (setting, tname))
    # Diagram theme shares the brand family (at least one direct brand ref).
    diagram_refs = [v for v in _THEME_SPECS["diagram"].values() if v.startswith("@")]
    if not diagram_refs:
        errors.append("diagram theme lost all brand references")
    # CSS export shape: one declaration per slot.
    css = to_css_root("console-dark")
    if css.count("--") != len(SLOTS):
        errors.append("to_css_root declaration count mismatch")
    # Layer C -- color identity. Mechanical legibility guarantee: every accent,
    # tuned for every allowed background, either clears AA (4.5) on the real
    # card OR is a documented exclusion (accent_for -> None). No sub-threshold
    # shade may ship.
    for aname in ACCENT_PALETTE:
        for bgname, bg in BACKGROUNDS.items():
            card = card_surface(bg["hex"])
            tuned_hex = accent_for(aname, bgname)
            if tuned_hex is None:
                continue  # excluded pair -- picker must drop it
            if contrast_ratio(_hex_to_rgb(tuned_hex),
                              _hex_to_rgb(card)) < AA_CONTRAST:
                errors.append("accent %s on %s below AA %.1f"
                              % (aname, bgname, AA_CONTRAST))
    # Backgrounds well-formed; declared tier agrees with measured luminance.
    for bgname, bg in BACKGROUNDS.items():
        if not _HEX_RE.match(bg["hex"]):
            errors.append("background %s: bad hex %r" % (bgname, bg["hex"]))
        elif bg["tier"] not in ("dark", "light"):
            errors.append("background %s: bad tier %r" % (bgname, bg["tier"]))
        elif is_dark_surface(bg["hex"]) != (bg["tier"] == "dark"):
            errors.append("background %s: tier/luminance disagree" % bgname)
    # Accent palette hex validity + expected counts.
    for aname, ahex in ACCENT_PALETTE.items():
        if not _HEX_RE.match(ahex):
            errors.append("accent %s: bad hex %r" % (aname, ahex))
    if len(ACCENT_PALETTE) != 15:
        errors.append("accent palette count %d != 15" % len(ACCENT_PALETTE))
    if len(BACKGROUNDS) != 8:
        errors.append("background count %d != 8" % len(BACKGROUNDS))
    # Layer C leader/domain: crew accent names valid; leader exempt from the
    # distinctiveness rule; domain default to the crew accent conflicts.
    for crew, aname in CREW_ACCENT.items():
        if aname not in ACCENT_PALETTE:
            errors.append("crew_accent %s: unknown accent %r" % (crew, aname))
        if accent_conflicts_with_crew(aname, crew, is_leader=True):
            errors.append("leader wrongly flagged for crew %s" % crew)
        if not accent_conflicts_with_crew(aname, crew, is_leader=False):
            errors.append("domain crew-accent collision not flagged for %s" % crew)
    if resolve_accent(owner_override="rose", own_color="lime",
                      is_leader=True, crew="devkit") != "rose":
        errors.append("resolve_accent owner override precedence broken")
    if resolve_accent(is_leader=True, crew="lifekit") != "teal":
        errors.append("resolve_accent leader default broken")
    if resolve_accent(is_leader=False, crew="devkit",
                      kit_domain_color="violet") != "violet":
        errors.append("resolve_accent domain default broken")
    if resolve_accent() != "teal":
        errors.append("resolve_accent system fallback broken")
    # reference anchors are a generated artifact of accent_for over the default
    # background card -- never a separately-tuned fixed table.
    for aname in ACCENT_PALETTE:
        for tier, bgname in _DEFAULT_BG.items():
            if reference_anchor(aname, tier) != accent_for(aname, bgname):
                errors.append("reference_anchor %s/%s != accent_for" % (aname, tier))
    # validate_background: all 8 curated pass, a mid-grey is rejected.
    for bgname, bg in BACKGROUNDS.items():
        try:
            validate_background(bg["hex"])
        except ValueError:
            errors.append("validate_background rejected curated bg %s" % bgname)
    try:
        validate_background("#808080")
        errors.append("validate_background accepted mid-grey #808080")
    except ValueError:
        pass
    # loud errors on invalid input (never silent-resolve to teal/False).
    for bad_call in (lambda: accent_for("nope", "navy"),
                     lambda: accent_for("teal", "nope"),
                     lambda: accent_conflicts_with_crew("nope", "devkit"),
                     lambda: accent_distinct("nope", [])):
        try:
            bad_call()
            errors.append("invalid input did not raise")
        except KeyError:
            pass
    # perceptual distinctiveness gate: exact reuse and near-duplicate rejected,
    # a distant hue allowed; domain vs crew-leader enforced.
    if accent_distinct("cobalt", ["cobalt"]):
        errors.append("accent_distinct allowed exact reuse")
    if accent_distinct("indigo", ["violet"]):  # dE 14.5 base... check < gate?
        pass  # 14.5 >= DISTINCT_DE(10) so this is allowed; not an error
    if not accent_distinct("rose", ["teal", "amber"]):
        errors.append("accent_distinct wrongly rejected a distant hue")
    if accent_distinct("cobalt", [], crew="devkit"):
        errors.append("accent_distinct allowed domain == crew leader accent")
    return errors


def anchors_table():
    """Generated reference anchor table (single source of truth = accent_for
    over the default crew backgrounds). Returns a printable string; excluded
    pairs (should be none on the curated set) render as 'EXCLUDED'."""
    lines = ["accent   | base    | dark(navy) | light(navy-pale)"]
    for aname in ACCENT_PALETTE:
        dk = reference_anchor(aname, "dark") or "EXCLUDED"
        lt = reference_anchor(aname, "light") or "EXCLUDED"
        lines.append("%-8s | %s | %s   | %s"
                     % (aname, ACCENT_PALETTE[aname], dk, lt))
    return "\n".join(lines)


def _gate_cli(argv):
    """CLI for the color pick gate (DGN-650). Exit 0 = allowed (PASS line with
    tier + tuned anchor + chip flag), 1 = rejected (FAIL line with reason +
    remaining valid choices), 2 = caller error (bad flags / unknown crew or
    taken name)."""
    parser = argparse.ArgumentParser(
        prog="design_tokens.py --gate",
        description="Onboarding / identity-change color pick gate.")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--accent", required=True,
                        help="accent palette name (15-color set)")
    parser.add_argument("--bg", required=True,
                        help="background name (8 curated) or custom #RRGGBB")
    parser.add_argument("--crew", default=None,
                        help="crew for the distinctiveness rule")
    parser.add_argument("--leader", action="store_true",
                        help="candidate is the crew leader (exempt from the "
                             "crew-accent conflict rule)")
    parser.add_argument("--taken", default="",
                        help="comma-separated peer accents already worn")
    args = parser.parse_args(argv)
    taken = [t.strip() for t in args.taken.split(",") if t.strip()]
    try:
        ok, lines = gate_pick(args.accent, args.bg, crew=args.crew,
                              is_leader=args.leader, taken=taken)
    except KeyError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0 if ok else 1


def main(argv):
    if "--gate" in argv:
        return _gate_cli(argv)
    if "--json" in argv:
        print(to_json())
        return 0
    if "--anchors" in argv:
        print(anchors_table())
        return 0
    errors = _self_check()
    if errors:
        for err in errors:
            print("FAIL: %s" % err, file=sys.stderr)
        return 1
    print("design_tokens self-check OK: %d brand tokens, %d themes x %d slots"
          % (len(BRAND), len(THEMES), len(SLOTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
