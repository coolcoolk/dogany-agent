#!/usr/bin/env python3
"""DGN-718 regression guard: the update-available notice must render as ONE
Telegram <blockquote expandable> bubble (with notes) or ONE plain <blockquote>
(without notes) -- never the old 3-block shape (header / fold / tail).

This ticket regressed once already: the single-fold merge was written on a
branch, never released, and later notice work (DGN-738/742) rebuilt on the old
3-block assembly. There was no test guarding the ASSEMBLY, only the note
extraction. This file is that missing guard."""
import importlib.util
import os
import re
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

    # (a) with notes -> exactly ONE blockquote, and it is expandable
    check("{}-single-block".format(lang),
          len(re.findall(r"<blockquote", with_notes)) == 1)
    check("{}-expandable".format(lang),
          with_notes.startswith("<blockquote expandable>")
          and with_notes.endswith("</blockquote>"))

    # (b) collapsed preview = first 3 lines are header / action / fold label,
    #     notes body sits below (line 4+)
    inner = with_notes[len("<blockquote expandable>"):-len("</blockquote>")]
    lines = inner.split("\n")
    check("{}-preview-3-lines".format(lang), len(lines) >= 4)
    check("{}-fold-label-line3".format(lang), lines[2].startswith("▸"))
    check("{}-notes-below-fold".format(lang), "\n".join(lines[3:]) == NOTES)

    # (c) no notes -> plain single blockquote, NOT expandable, 2 lines
    check("{}-downgrade-single-block".format(lang),
          len(re.findall(r"<blockquote", no_notes)) == 1)
    check("{}-downgrade-not-expandable".format(lang),
          "expandable" not in no_notes)

    # (d) old 3-block shape must never reappear
    check("{}-no-3-block".format(lang),
          "</blockquote>\n<blockquote" not in with_notes)

if _fails:
    print("\n{} test(s) FAILED: {}".format(len(_fails), ", ".join(_fails)))
    sys.exit(1)
print("\nAll DGN-718 single-fold tests PASSED")
