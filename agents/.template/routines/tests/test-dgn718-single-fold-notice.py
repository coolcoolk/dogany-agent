#!/usr/bin/env python3
"""DGN-718 / DGN-846 assembly guard: the update-available notice renders as
PLAIN header + action lines, with ONLY the fold label + release-note summary
inside ONE <blockquote expandable> (with notes) -- or plain header + action,
2 lines, no blockquote (without notes). Never the old 3-block shape
(header / fold / tail).

History:
  - This ticket regressed once already (DGN-738/742 rebuilt on the old
    3-block assembly; no test guarded the ASSEMBLY, only note extraction).
  - dec-117 (DGN-788) plaintexted the notice to paper over a raw-tag leak;
    DGN-846 fixed that leak bridge-side and reverted to a real fold.
  - Owner-confirmed 2026-08-12 (DGN-846): header + action are PLAIN and sit
    OUTSIDE the fold; ONLY the notes (fold label + summary) collapse inside
    the <blockquote expandable>. These asserts reflect that final form."""
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

    lines = with_notes.split("\n")

    # (a) header + action are PLAIN lines outside the fold (no blockquote tag).
    check("{}-header-plain".format(lang), "blockquote" not in lines[0])
    check("{}-action-plain".format(lang), "blockquote" not in lines[1])

    # (b) with notes -> exactly ONE blockquote, and it is expandable; it opens
    #     on line 3 (right after the two plain lines) with the fold label.
    check("{}-single-block".format(lang),
          len(re.findall(r"<blockquote", with_notes)) == 1)
    check("{}-expandable".format(lang),
          "<blockquote expandable>" in with_notes
          and with_notes.endswith("</blockquote>"))
    check("{}-fold-opens-line3".format(lang),
          lines[2].startswith("<blockquote expandable>▸"))

    # (c) notes body sits inside the fold, below the fold label.
    inner = with_notes.split("<blockquote expandable>", 1)[1]
    inner = inner[: -len("</blockquote>")]
    inner_lines = inner.split("\n")
    check("{}-fold-label-first".format(lang), inner_lines[0].startswith("▸"))
    check("{}-notes-below-fold-label".format(lang),
          "\n".join(inner_lines[1:]) == NOTES)

    # (d) no notes -> plain text, 2 lines, NO blockquote at all, no fold.
    check("{}-no-notes-2-plain-lines".format(lang),
          "blockquote" not in no_notes
          and len(no_notes.split("\n")) == 2)
    check("{}-no-notes-not-expandable".format(lang),
          "expandable" not in no_notes)

    # (e) old 3-block shape must never reappear.
    check("{}-no-3-block".format(lang),
          "</blockquote>\n<blockquote" not in with_notes)

if _fails:
    print("\n{} test(s) FAILED: {}".format(len(_fails), ", ".join(_fails)))
    sys.exit(1)
print("\nAll DGN-718/846 assembly tests PASSED")
