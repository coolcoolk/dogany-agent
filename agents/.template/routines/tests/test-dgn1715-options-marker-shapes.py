#!/usr/bin/env python3
# test-lane: fast
"""DGN-1715 r3: lockstep between the hook-side marker-shape recognizer
(routines/lib/dispatch_return_forcing.py, stdlib-only, mirrors the bridge
grammar rather than importing it) and the bridge's own canonical recognizer
(bridge/formatting.py is_options_marker_line -- "the canonical line-based
recognizer" per sdk_bridge.py DGN-1021, which knows every armable form, bare
AND labeled).

Cause of the incident this locks: dispatch_return_forcing.has_options_marker
used to test the literal substring ``"[[OPTIONS]]" in text``. That misses the
labeled marker shape "[[OPTIONS: a | b]]" (DGN-992) entirely -- the substring
"[[OPTIONS]]" never occurs inside "[[OPTIONS: a | b]]". A dispatch-return
final ending on a labeled marker therefore read as marker-less, and the Stop
seat (onboarding-gate-stop.py dispatch_return_stop) force-blocked the model
into emitting a SECOND [[OPTIONS]] menu on top of the one already on the
owner's screen (measured 2026-09-27 06:22, dev instance on dev.56; owner report
"notification comes twice").

Two things are locked here:
  1. PER-LINE shape lockstep: dispatch_return_forcing.is_options_marker_line
     must agree with bridge.formatting.is_options_marker_line on every line
     of a shared corpus (labeled / bare / malformed-lookalike / mid-sentence
     / wrong-case). This is the actual marker GRAMMAR, and it is the one the
     bridge arms buttons from -- if this ever drifts, the hook's forcing
     verdict can diverge from what the owner's screen actually shows again,
     silently.
  2. WHOLE-TEXT behavior contract for dispatch_return_forcing.has_options_marker
     (labeled, bare, bare + a trailing numbered list, inside a fenced code
     block -> not armed, inline mid-sentence -> not armed). This is the
     forcing point's own fence-aware wrapper around (1); it is deliberately
     STRICTER than bridge.options.has_options_marker (which, as shipped, does
     not exclude fenced code from its line scan at all -- a pre-existing gap
     in the bridge, out of scope for this ticket) because a forcing point
     that force-blocks a turn must never fire on an example/quote of the
     marker rather than a live one.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.normpath(os.path.join(HERE, ".."))
TEMPLATE = os.path.normpath(os.path.join(ROUTINES, ".."))

sys.path.insert(0, os.path.join(ROUTINES, "lib"))
import dispatch_return_forcing as forcing  # noqa: E402

if TEMPLATE not in sys.path:
    sys.path.insert(0, TEMPLATE)
# bridge/formatting.py is deliberately importable without python-telegram-bot
# (see its own module docstring) -- no mock/stub needed, unlike bridge.options.
from bridge.formatting import is_options_marker_line as bridge_is_marker_line  # noqa: E402

_failures = []


def check(name, got, want):
    if got != want:
        _failures.append("%s: got %r, want %r" % (name, got, want))


# (line, expected) -- every shape the bridge grammar accepts or rejects.
LINE_CORPUS = [
    ("[[OPTIONS]]", True),
    ("  [[OPTIONS]]  ", True),
    ("[[OPTIONS: proceed | hold]]", True),
    ("[[OPTIONS: 진행 | 보류]]", True),
    ("[[OPTIONS:진행|보류]]", True),
    ("[[OPTIONS:]]", True),          # empty label list still LOOKS like the shape
    ("[[OPTIONS: ]]", True),
    ("[[options]]", False),          # wrong case
    ("[[OPTIONS]] extra text", False),   # not a standalone bare marker
    ("prefix [[OPTIONS]]", False),       # not standalone (mid-sentence)
    ("[[OPTIONS: a | b", False),         # unterminated labeled form
    ("OPTIONS: a | b]]", False),
    ("", False),
    ("1. proceed", False),
    ("[[IDRILL:0123abcd]]", False),      # a different bracket marker entirely
]


def test_per_line_shape_lockstep_with_bridge():
    for line, expected in LINE_CORPUS:
        hook_result = forcing.is_options_marker_line(line)
        bridge_result = bridge_is_marker_line(line)
        check("hook(%r)" % line, hook_result, expected)
        check("bridge(%r)" % line, bridge_result, expected)
        check("lockstep(%r)" % line, hook_result, bridge_result)


# (text, expected) -- the forcing point's own whole-text, fence-aware wrapper.
TEXT_CORPUS = [
    ("Proceed with the merge?\n[[OPTIONS]]\n1. proceed\n2. hold", True),
    ("Proceed with the merge?\n[[OPTIONS: proceed | hold]]", True),
    ("이어서 병합할까요?\n[[OPTIONS: 진행 | 보류]]", True),
    ("Next conversation I will merge it.", False),
    ("I will do it next time (the [[OPTIONS]] marker only arms a keyboard "
     "on its own line).", False),
    ("I will do it next time.\n```\n[[OPTIONS]]\n```", False),
    ("I will do it next time.\n```\n[[OPTIONS: proceed | hold]]\n```", False),
    ("```\n[[OPTIONS]]\n```\nProceed?\n[[OPTIONS: proceed | hold]]", True),
    ("", False),
]


def test_whole_text_forcing_contract():
    for text, expected in TEXT_CORPUS:
        check("has_options_marker(%r)" % text, forcing.has_options_marker(text), expected)


for _name, _fn in sorted(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        _fn()

if _failures:
    print("FAIL test-dgn1715-options-marker-shapes")
    for f in _failures:
        print("  " + f)
    sys.exit(1)
print("OK test-dgn1715-options-marker-shapes")
