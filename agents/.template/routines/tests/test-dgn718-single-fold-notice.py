#!/usr/bin/env python3
"""DGN-718 regression guard: the update-available notice must render as ONE
plain-text block (with notes) or ONE plain 2-line block (without notes) --
never the old 3-block shape (header / fold / tail).

This ticket regressed once already: the single-fold merge was written on a
branch, never released, and later notice work (DGN-738/742) rebuilt on the old
3-block assembly. There was no test guarding the ASSEMBLY, only the note
extraction. This file is that missing guard.

dec-117 (DGN-788): HTML/blockquote tags removed; output is now plain text.
Layout: header / action / fold_label / notes (4 lines when notes present),
header / action (2 lines when no notes). These asserts reflect the current
plain-text format."""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VC = os.path.join(_HERE, os.pardir, "version-check.py")

spec = importlib.util.spec_from_file_location("vc_dgn718", _VC)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

_fails = []


def check(name, cond):
    if cond:
        print("PASS ({}): ok".format(name))
    else:
        print("FAIL ({}): assertion failed".format(name))
        _fails.append(name)


NOTES = "- root skew fix\n- notice merged to one fold"

for lang in ("ko", "en"):
    with_notes = vc._build_user_notice("1.27.1", NOTES, lang)
    no_notes = vc._build_user_notice("1.27.1", "", lang)

    # (a) with notes -> plain text, no blockquote tags (dec-117)
    check("{}-single-block".format(lang),
          "<blockquote" not in with_notes)
    # expandable check: plain text must not contain any blockquote markup
    check("{}-expandable".format(lang),
          "blockquote" not in with_notes)

    # (b) collapsed preview = first 3 lines are header / action / fold label,
    #     notes body sits below (line 4+)
    lines = with_notes.split("\n")
    check("{}-preview-3-lines".format(lang), len(lines) >= 4)
    check("{}-fold-label-line3".format(lang), lines[2].startswith("▸"))
    check("{}-notes-below-fold".format(lang), "\n".join(lines[3:]) == NOTES)

    # (c) no notes -> plain text, 2 lines, no blockquote, no expandable
    check("{}-downgrade-single-block".format(lang),
          "<blockquote" not in no_notes and len(no_notes.split("\n")) == 2)
    check("{}-downgrade-not-expandable".format(lang),
          "expandable" not in no_notes)

    # (d) old 3-block shape must never reappear
    check("{}-no-3-block".format(lang),
          "</blockquote>\n<blockquote" not in with_notes)

if _fails:
    print("\n{} test(s) FAILED: {}".format(len(_fails), ", ".join(_fails)))
    sys.exit(1)
print("\nAll DGN-718 single-fold tests PASSED")
