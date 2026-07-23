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

import json
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
    "navy-track":  "#2A2A50",  # progress track base
    "navy-line":   "#2A2A55",  # separator / panel edge
    # ink family (text on navy)
    "ink":         "#FFFFFF",  # primary text
    "ink-soft":    "#C4D4FF",  # secondary text / labels
    "ink-value":   "#DDDDEE",  # value text
    "grey":        "#888899",  # fallback / unknown-category grey
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
    # Console web UI (separate product repo, GitHub-dark register).
    # Deliberately alien to the brand navy ground (measured views.py :root,
    # shared hex with card-dark = 0 -- kept as a per-medium override block,
    # grill M5). Reskin toward brand = product decision, out of v1 scope.
    "console-dark": {
        "bg":      "#0D1117",
        "surface": "#161B22",
        "border":  "#30363D",
        "text":    "#C9D1D9",
        "muted":   "#8B949E",
        "accent":  "#58A6FF",
        "green":   "#3FB950",
        "yellow":  "#D29922",
        "red":     "#F85149",
        "purple":  "#BC8CFF",
        "orange":  "#E3B341",
    },
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


# Resolved themes: {theme_name: {slot: "#RRGGBB"}}. This is the consumer
# surface -- consumers read THEMES (or theme()), never _THEME_SPECS.
THEMES = {name: _resolve_theme(spec) for name, spec in _THEME_SPECS.items()}


def theme(name):
    """Return a copy of a resolved theme dict (safe to mutate)."""
    return dict(THEMES[name])


def to_css_root(name="console-dark", indent="    "):
    """Render a theme as a CSS :root block (console views.py pattern)."""
    t = THEMES[name]
    lines = ["%s--%s: %s;" % (indent, slot, t[slot].lower()) for slot in SLOTS]
    return ":root {\n%s\n}" % "\n".join(lines)


def to_json():
    """Language-neutral export (vendoring / drift-lint surface)."""
    return json.dumps(
        {"brand": BRAND, "fonts": FONTS, "themes": THEMES},
        indent=2, sort_keys=True,
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
    # M5 invariant: console-dark is a deliberate per-medium register --
    # zero hex overlap with card-dark (regression net for accidental
    # cross-bleed while the console reskin decision is pending).
    if THEMES:
        card = set(THEMES["card-dark"].values())
        console = set(THEMES["console-dark"].values())
        overlap = card & console
        if overlap:
            errors.append("card-dark/console-dark hex overlap: %s" % sorted(overlap))
    # Diagram theme shares the brand family (at least one direct brand ref).
    diagram_refs = [v for v in _THEME_SPECS["diagram"].values() if v.startswith("@")]
    if not diagram_refs:
        errors.append("diagram theme lost all brand references")
    # CSS export shape: one declaration per slot.
    css = to_css_root("console-dark")
    if css.count("--") != len(SLOTS):
        errors.append("to_css_root declaration count mismatch")
    return errors


def main(argv):
    if "--json" in argv:
        print(to_json())
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
