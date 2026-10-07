#!/usr/bin/env python3
# test-lane: fast
"""DGN-1754 -- owner-language ticket titles on owner surfaces.

Owner 2026-09-27: the owner-facing ticket title is a DISPLAY title keyed by
the instance language ("display_title.<lang>", lang = AGENT_LANG base code);
the frontmatter "title" stays the internal key and is never shown in its
place.  Missing display title -> id only; the ticket lint flags it.

  D1  frontmatter parser keeps dotted keys (display_title.ko)
  D2  _owner_lang: env DOGANY_LANG -> conf AGENT_LANG -> en; "ko-KR" -> ko
  D3  _ticket_display_title: instance language picked, quotes unwrapped,
      missing -> "" (never the internal title); exposed on the plugin host
  D4  ticket lint: live ticket without display_title.<lang> -> advisory;
      present / done / dismissed -> silent

Run:  /usr/bin/python3 routines/tests/test-dgn1754-display-title.py
      (DGN1754_ROUTINES=<dir> runs it against another routines checkout)
Exit: 0 all pass, 1 any fail.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.environ.get("DGN1754_ROUTINES") or os.path.dirname(HERE)
FOOTER_PY = os.path.join(ROUTINES, "status-footer.py")
LINT_PY = os.path.join(ROUTINES, "ticket-status-enum-lint.py")

WORK = tempfile.mkdtemp(prefix="dgn1754.")
CONF = os.path.join(WORK, "agent.conf")
os.environ["DOGANY_AGENT_CONF"] = CONF
os.environ["DOGANY_BOT_DATA_DIR"] = os.path.join(WORK, "bot")
os.environ["DOGANY_BOARD_PLUGIN"] = "none"
for k in ("DOGANY_LANG", "TICKET_FILE_GLOB", "TICKET_PREFIX"):
    os.environ.pop(k, None)

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS: " + name)
    else:
        FAIL += 1
        print("  FAIL: " + name + ("  -- " + detail if detail else ""))


def set_conf(text):
    with open(CONF, "w") as fh:
        fh.write(text)


set_conf("")
spec = importlib.util.spec_from_file_location("sf1754", FOOTER_PY)
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)

WL = os.path.join(WORK, "worklog")
os.makedirs(WL)
TICKET = os.path.join(WL, "DGN-9101-x.md")
with open(TICKET, "w") as fh:
    fh.write("---\nid: DGN-9101\ntitle: Workbench shows English titles\n"
             "display_title.ko: \"작업대 티켓 제목 한국어 표시\"\n"
             "display_title.en: Workbench ticket titles\nstatus: open\n---\n")

print("--- D1: dotted frontmatter keys ---")
fm = sf._ticket_frontmatter(TICKET)
check("display_title.ko parsed as its own key",
      fm.get("display_title.ko") == "\"작업대 티켓 제목 한국어 표시\"", repr(fm))
check("plain keys unchanged", fm.get("status") == "open"
      and fm.get("id") == "DGN-9101", repr(fm))

print("--- D2: owner language ---")
ol = getattr(sf, "_owner_lang", None)
check("_owner_lang exists", callable(ol))
if callable(ol):
    set_conf("")
    check("no conf -> en", ol() == "en", repr(ol()))
    set_conf("AGENT_LANG=ko-KR\n")
    check("AGENT_LANG=ko-KR -> ko", ol() == "ko", repr(ol()))
    os.environ["DOGANY_LANG"] = "en"
    check("env DOGANY_LANG wins", ol() == "en", repr(ol()))
    del os.environ["DOGANY_LANG"]

print("--- D3: display title ---")
dt = getattr(sf, "_ticket_display_title", None)
check("_ticket_display_title exists", callable(dt))
if callable(dt):
    set_conf("AGENT_LANG=ko\n")
    check("ko instance -> ko display title, quotes unwrapped",
          dt(fm) == "작업대 티켓 제목 한국어 표시", repr(dt(fm)))
    check("explicit lang -> that language", dt(fm, "en")
          == "Workbench ticket titles", repr(dt(fm, "en")))
    check("missing language -> '' (never the internal title)",
          dt(fm, "ja") == "", repr(dt(fm, "ja")))
    check("no frontmatter -> ''", dt(None) == "" and dt({}) == "")
    check("plugin host exposes the helper",
          sf._HostView()._ticket_display_title is dt)

print("--- D4: ticket lint ---")


def lint(path, lang):
    env = dict(os.environ)
    env["DOGANY_LANG"] = lang
    env["TICKET_FILE_GLOB"] = "DGN-*.md"
    payload = json.dumps({"tool_name": "Edit",
                          "tool_input": {"file_path": path}})
    r = subprocess.run([sys.executable, LINT_PY], input=payload,
                       capture_output=True, text=True, env=env, cwd=WORK)
    return r.stdout


check("ko display title present -> silent", lint(TICKET, "ko") == "",
      lint(TICKET, "ko"))
out = lint(TICKET, "ja")
check("no display_title.ja on a ja instance -> advisory",
      "DISPLAY-TITLE" in out and "display_title.ja" in out, repr(out))
BARE = os.path.join(WL, "DGN-9102-y.md")
with open(BARE, "w") as fh:
    fh.write("---\nid: DGN-9102\ntitle: x\nstatus: wip\n---\n")
check("wip ticket with no display title -> advisory",
      "DISPLAY-TITLE" in lint(BARE, "ko"))
os.environ.pop("DOGANY_DISPLAY_NAMES", None)
out = lint(BARE, "ko")
check("DISPLAY_NAMES unset -> plain-word register",
      "plain everyday words" in out and "original form" not in out,
      repr(out))
os.environ["DOGANY_DISPLAY_NAMES"] = "off"
out = lint(BARE, "ko")
check("DISPLAY_NAMES=off -> original-term register",
      "original form" in out and "plain everyday" not in out, repr(out))
os.environ.pop("DOGANY_DISPLAY_NAMES", None)
with open(BARE, "w") as fh:
    fh.write("---\nid: DGN-9102\ntitle: x\nstatus: dismissed\n---\n")
check("dismissed ticket -> no display-title nag",
      "DISPLAY-TITLE" not in lint(BARE, "ko"))

shutil.rmtree(WORK, ignore_errors=True)
print("\n===========================")
print("Results: %d passed, %d failed" % (PASS, FAIL))
print("===========================")
sys.exit(1 if FAIL else 0)
