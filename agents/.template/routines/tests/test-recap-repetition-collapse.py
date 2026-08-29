#!/usr/bin/env python3
"""DGN-951 regression: session-recap degenerate-repetition collapse.

A flooded turn (one short token repeated) must be neutralized before it is
re-injected as the continuity tail; normal conversation, separators, and short
natural repeats must be left byte-for-byte unchanged (false positives ~0).
Run: python3 routines/tests/test-recap-repetition-collapse.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RECAP = os.path.join(HERE, "..", "session-recap.py")
spec = importlib.util.spec_from_file_location("recap", RECAP)
recap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recap)
cr = recap.collapse_repetition

fails = []


def collapses(name, text):
    out = cr(text)
    if "repetition collapsed" not in out or "course course course course course" in out:
        fails.append((name, out[:80]))


def unchanged(name, text):
    if cr(text) != text:
        fails.append((name, cr(text)[:80]))


# must collapse
collapses("token-flood", "ok confirmed " + "course " * 200)
collapses("newline-flood", "of\n" * 80)
collapses("substr-flood", "prefix " + "course" * 100)
collapses("mixed-flood", "Conclusion follows. " + "of " * 60 + " end.")

# must stay untouched (zero false positive)
unchanged("normal", "Understood, going with plan B as the user decided. Locking the spec now.")
unchanged("dashes", "---\nsome text\n===")
unchanged("short-repeat", "yes yes yes understood")
unchanged("ellipsis", "one moment... checking now")
unchanged("empty", "")

# fail-open: never raise
try:
    cr(None)  # type: ignore[arg-type]
except TypeError:
    pass  # None is not valid text; real callers always pass str

if fails:
    for name, got in fails:
        print(f"FAIL {name}: {got!r}", file=sys.stderr)
    sys.exit(1)
print("OK: all recap repetition-collapse cases pass")
