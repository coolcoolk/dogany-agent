#!/usr/bin/env python3
# test-lane: fast
# test-component: status-footer
# test-tier: 1
# test-prereq: tmp-files
"""Workbench areas in quotes (owner 2026-10-15 18:04) -- end-to-end gate.

The owner's board (fixtures/wb-quote/board-input.json, taken from the pinned
board of 2026-10-15 18:05) is rendered through the REAL composer
(status-footer.py _build_dashboard) and then the REAL pin converter
(bridge.formatting sanitize_message_for_telegram + balance_telegram_html,
exactly bridge/dashboard.py _render_pin_html).  Promises:

  Q1  the title line stays outside every quote
  Q2  [products] (no fold of its own) -> one plain <blockquote>
  Q3  tickets stay expandable; each background job has its own quote
  Q4  the same board with 3 background rows -> one plain quote per job
  Q5  quote:: typed block: unterminated stays literal, a body line that
      looks like a marker cannot close the quote, recognized by the
      containment net
  Q6  board stays under DASHBOARD_MAX_UNITS; the rendered pin text (tags
      stripped) fits Telegram's 4096 UTF-16 units
  Q7  snapshot: rendered HTML == fixtures/wb-quote/rendered-8.html
      (and rendered-3.html); --update rewrites them
  P1-P6  the pinned message layout (DGN-2116, owner dec-470/472/474): top
      line, one quote per background job, the framework agent's approved preview, a kit
      section unchanged, own head recognized, length-only overflow fold
      (fixtures/dgn2116/*)

Run:  /usr/bin/python3 routines/tests/test-wb-quote-areas.py [--update]
Exit: 0 all pass, 1 any fail.
"""

import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
TEMPLATE = os.path.dirname(ROUTINES)
FOOTER_PY = os.path.join(ROUTINES, "status-footer.py")
FIX = os.path.join(HERE, "fixtures", "wb-quote")
UPDATE = "--update" in sys.argv

with open(os.path.join(FIX, "board-input.json"), encoding="utf-8") as fh:
    BOARD = json.load(fh)

WORK = tempfile.mkdtemp(prefix="wb-quote.")
os.environ["DOGANY_AGENT_CONF"] = os.path.join(WORK, "agent.conf")
with open(os.environ["DOGANY_AGENT_CONF"], "w") as fh:
    fh.write("WHY_NET=on\n")
os.environ["DOGANY_BOT_DATA_DIR"] = os.path.join(WORK, "bot")
os.environ["DOGANY_BOARD_PLUGIN"] = "none"
os.environ["DOGANY_BOARD_EMOJI"] = BOARD["emoji"]
for k in ("DOGANY_LIVE_LABEL", "DOGANY_LANG", "DOGANY_LIVE_SHOW_MODEL"):
    os.environ.pop(k, None)

# Pin THIS tree's bridge: a PYTHONPATH / installed-instance `bridge` ahead of it
# (or already imported) would silently test a different converter.
while TEMPLATE in sys.path:
    sys.path.remove(TEMPLATE)
sys.path.insert(0, TEMPLATE)
for _m in [m for m in sys.modules if m == "bridge" or m.startswith("bridge.")]:
    del sys.modules[_m]
from bridge.formatting import (  # noqa: E402
    RECOGNIZED_COLON_MARKERS,
    balance_telegram_html,
    html_to_plain_text,
    render_fold_block,
    sanitize_message_for_telegram,
)
from bridge.machine_gate import RAIL_CONSUMER  # noqa: E402

spec = importlib.util.spec_from_file_location("status_footer", FOOTER_PY)
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)
TITLE = "%s 10/15(목) %s" % (BOARD["emoji"], BOARD["stamp"])
sf._board_title_line = lambda: TITLE

PASS = 0
FAIL = 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS: %s" % desc)
    else:
        FAIL += 1
        print("  FAIL: %s" % desc)
        if detail:
            print("    " + str(detail).replace("\n", "\n    "))


def compose(n_live):
    return sf._build_dashboard(
        [], BOARD["live_rows"][:n_live], [],
        extra_sections=BOARD["plugin_sections"],
        hidden=sf._board_hidden(()), why=BOARD["why"])


def render(text):
    """bridge/dashboard.py _render_pin_html, verbatim."""
    return balance_telegram_html(
        sanitize_message_for_telegram(text, rail=RAIL_CONSUMER))


def quotes(html_text):
    """[(tag, first inner line), ...] for every top-level blockquote."""
    out = []
    pos = 0
    while True:
        i = html_text.find("<blockquote", pos)
        if i < 0:
            return out
        j = html_text.index(">", i)
        k = html_text.index("</blockquote>", j)
        inner = html_text[j + 1:k]
        out.append((html_text[i:j + 1], inner.split("\n", 1)[0]))
        pos = k


def snapshot(name, text):
    path = os.path.join(FIX, name)
    if UPDATE:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("  UPDATED: %s" % name)
        return
    try:
        with open(path, encoding="utf-8") as fh:
            want = fh.read()
    except OSError:
        want = None
    check("Q7 snapshot %s" % name, want == text,
          "golden missing (run --update)" if want is None else text)


LIVE = sf.LIVE_LABEL
PLAIN, FOLD = "<blockquote>", "<blockquote expandable>"

print("== board with 8 background rows ==")
raw8 = compose(8)
html8 = render(raw8)
print(html8)
q8 = quotes(html8)
check("Q1 title line first and unquoted",
      html8.split("\n", 1)[0] == TITLE, html8.split("\n", 1)[0])
check("Q1 nothing before the first quote but the title and the bold area title",
      html8[:html8.index("<blockquote")] == TITLE + "\n\n<b>[%s]</b>\n" % LIVE)
heads = dict((first, tag) for tag, first in q8)
bg_head = sf._board_str("live_fold") % 8
check("Q3 eight jobs each have their own plain quote",
      all(heads.get(job) == PLAIN for job in BOARD["live_rows"]), q8)
check("Q3 Why stays off the workbench with WHY_NET=on",
      "[Why]" not in raw8 and "[Why]" not in html8, q8)
ticket_head = BOARD["plugin_sections"][0]["variants"][0][0][len("fold:: "):]
check("Q3 tickets expandable", heads.get(ticket_head) == FOLD, q8)
prod = BOARD["plugin_sections"][1]["variants"][0]
check("Q2 product status is a plain quote", heads.get(prod[0]) == PLAIN, q8)
check("Q2 product rows stay inside its quote",
      all(r in html8.split(prod[0], 1)[1].split("</blockquote>", 1)[0]
          for r in prod[1:]))
check("quote count = eight jobs plus two areas", len(q8) == 10, q8)
check("no typed marker leaks into the HTML",
      "quote::" not in html8 and "fold::" not in html8)
check("Q6 board under DASHBOARD_MAX_UNITS",
      sf._u16len(raw8) <= sf.DASHBOARD_MAX_UNITS, sf._u16len(raw8))
check("Q6 rendered text under 4096 units",
      sf._u16len(html_to_plain_text(html8)) <= 4096)
snapshot("rendered-8.html", html8)

print("== board with 3 background rows ==")
html3 = render(compose(3))
print(html3)
heads3 = dict((first, tag) for tag, first in quotes(html3))
check("Q4 3 rows -> plain quote under the bare title",
      "<b>[%s]</b>\n<blockquote>" % LIVE in html3
      and len([1 for t, f in quotes(html3) if t == PLAIN]) >= 3, quotes(html3))
check("Q4 3 rows -> no count header", bg_head not in html3)
snapshot("rendered-3.html", html3)

print("== edge: five rows stays plain ==")
html5 = render(compose(5))
check("5 rows -> plain",
      "<b>[%s]</b>\n<blockquote>" % LIVE in html5)

print("== quote:: typed block ==")
check("Q5 quote:: is a recognized marker", "quote::" in RECOGNIZED_COLON_MARKERS)
check("Q5 transpile", render_fold_block("a\nquote::\nx\n\ny\nquote::end\nb")
      == "a\n> x\n>\n> y\nb")
check("Q5 unterminated stays literal",
      render_fold_block("quote::\nx") == "quote::\nx")
wrapped = sf._quote_wrap(["[h]", "quote::end", "- x"])
check("Q5 marker-shaped body line cannot close the quote",
      render_fold_block("\n".join(wrapped)).count("\n") == 2
      and render("\n".join(wrapped)).count("<blockquote>") == 1, wrapped)
check("Q5 a section with its own fold is left unwrapped",
      sf._quote_wrap(["fold:: h", "x", "fold::end"])
      == ["fold:: h", "x", "fold::end"])


# ---------------------------------------------------------------------------
# DGN-2116 framework seat -- the pinned message (owner dec-470, dec-472, dec-474).
# The composer and the real pin converter render two agents from fixtures
# (fixtures/dgn2116/{framework-agent,kit-agent}.json) at a frozen clock, 2026-10-17 17:41 KST.
#   P1  top line = agent emoji + MM/DD(weekday) HH:MM, no title word
#   P2  [백그라운드 작업]: bold title outside, ONE blockquote per job, name line,
#       then "• <type>: <actor>, HH:MM (N분~)"; no middle dot (P2b/P2c: the
#       interpreter is read from the launch, transcript -> board row)
#   P3  the framework agent's rendered HTML equals the approved preview byte for byte
#   P4  a kit section stays pluggable: the kit agent's section renders as it did before
#   P5  our own new top line is recognized as a bare board head
#   P6  seven and twelve jobs stay separate; only length overflow folds
# ---------------------------------------------------------------------------
import time as _real_time  # noqa: E402
import types  # noqa: E402

FIX_P = os.path.join(HERE, "fixtures", "dgn2116")
os.environ["TZ"] = "Asia/Seoul"
_real_time.tzset()
NOW = _real_time.mktime((2026, 10, 17, 17, 41, 0, 0, 0, -1))
os.environ.pop("DOGANY_BOARD_TITLE", None)


def load_p(name):
    with open(os.path.join(FIX_P, name), encoding="utf-8") as fh:
        return json.load(fh)


def load_footer(emoji):
    os.environ["DOGANY_BOARD_EMOJI"] = emoji
    spec = importlib.util.spec_from_file_location(
        "sf_" + str(abs(hash(emoji))), os.path.join(ROUTINES, "status-footer.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    frozen = types.SimpleNamespace(**{k: getattr(_real_time, k)
                                      for k in dir(_real_time)
                                      if not k.startswith("__")})
    frozen.time = lambda: NOW
    frozen.localtime = lambda secs=None: _real_time.localtime(
        NOW if secs is None else secs)
    frozen.strftime = lambda fmt, t=None: _real_time.strftime(
        fmt, _real_time.localtime(NOW) if t is None else t)
    mod.time = frozen
    mod._machine_name = lambda: "Mac mini"
    return mod


def rows(sf, spec):
    out = []
    for r in spec["live"]:
        start = NOW - r["ago_min"] * 60
        if r["kind"] == "model":
            bare, tag = sf._place(r["runtime"], r["alias"]) \
                if r.get("runtime") else (None, "")
            out.append(sf.LiveRow(r["text"], "legacy", model=r["model"],
                                  start_epoch=start, bare=bare, tag=tag))
        else:
            out.append(sf.LiveRow(
                sf._background_row(r["slot"], r["text"], start), "background",
                name=sf._background_label(r["slot"], r["text"]),
                job_start=start,
                interpreter=sf.bg_job_notice.command_interpreter(
                    "Bash", {"command": r.get("command", "")})))
    return out


def render_p(sf, spec, live=None):
    text = sf._build_dashboard(
        [], live if live is not None else rows(sf, spec), [],
        extra_sections=spec["plugin_sections"], hidden=sf._board_hidden(()))
    return balance_telegram_html(
        sanitize_message_for_telegram(text, rail=RAIL_CONSUMER)).rstrip("\n")


metal_spec, ag_spec = load_p("framework-agent.json"), load_p("kit-agent.json")
sf_m, sf_a = load_footer(metal_spec["emoji"]), load_footer(ag_spec["emoji"])
html_m, html_a = render_p(sf_m, metal_spec), render_p(sf_a, ag_spec)
print(html_m + "\n\n----\n\n" + html_a)

print("== DGN-2116 P1 top line ==")
check("framework agent: emoji + date + time", html_m.split("\n", 1)[0] == "⚙️ 10/17(토) 17:41")
check("kit agent: its own emoji, same shape", html_a.split("\n", 1)[0] == "🌿 10/17(토) 17:41")
check("no title word in the pinned message",
      "작업대" not in html_m and "갱신" not in html_m
      and "작업대" not in html_a.split("\n\n<blockquote expandable>")[0])

print("== P2 background section ==")
check("bold bracket title outside the quote",
      "\n\n<b>[백그라운드 작업]</b>\n<blockquote>잠자기" in html_m)
check("model job: sibling term + model + where, no middle dot",
      "·" not in html_m and "잠자기 뒤 텔레그램 연결 스스로 회복하기 17:40 (1분~)\n• 서브에이전트: Claude Opus 5.5 (Cloud)"
      in html_m)
check("script job (bash script): no model, no machine, no middle dot",
      "dev push (검사 수정, 용어 수정) 17:33 (8분~)\n• 스크립트</blockquote>" in html_m)
check("script job, unknown interpreter: same bare line, no placeholder",
      " 17:37 (4분~)\n• 스크립트</blockquote>" in html_a
      and "()" not in html_a and "unknown" not in html_a.lower())

print("== P2b interpreter read from the launch, not guessed ==")
bg = sf_m.bg_job_notice
ci = lambda c: bg.command_interpreter("Bash", {"command": c})
with tempfile.TemporaryDirectory() as td:
    z = os.path.join(td, "z.sh")
    with open(z, "w") as fh:
        fh.write("#!/usr/bin/env zsh\necho\n")
    p = os.path.join(td, "n.sh")
    with open(p, "w") as fh:
        fh.write("echo no shebang\n")
    check("shebang via env", ci(z + " --x") == "zsh", ci(z))
    check("script path without shebang -> unknown", ci(p) is None)
check("interpreter head", ci("cd /x && python3 -u r.py") == "python3")
check("sh / zsh heads", ci("sh a.sh") == "sh" and ci("zsh -c 'x'") == "zsh")
check("bare program -> unknown", ci("make dev6") is None and ci("") is None)
check("non-Bash tool -> unknown",
      bg.command_interpreter("Monitor", {"command": "bash x"}) is None)

print("== P2c end to end: transcript launch -> board row ==")
with tempfile.TemporaryDirectory() as td:
    ts = NOW - 5 * 60
    iso = lambda e: _real_time.strftime("%Y-%m-%dT%H:%M:%SZ", _real_time.gmtime(e))
    tp = os.path.join(td, "s.jsonl")
    lines = [
        {"type": "assistant", "timestamp": iso(ts), "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {
                "command": "zsh build.sh dev.6", "description": "dev.6 판 만들기",
                "run_in_background": True}}]}},
        {"type": "user", "timestamp": iso(ts + 1), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content":
             "Command running in background with ID: bk1"}]}},
    ]
    with open(tp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    live = [r for r in sf_m._collect_subagent_rows(tp) if r.source == "background"]
    check("transcript row carries the interpreter",
          len(live) == 1 and live[0].interpreter == "zsh",
          [(r.text, r.interpreter) for r in live])
    check("script job line carries no interpreter or machine",
          bool(live) and live[0].job_lines()[1] == "• 스크립트",
          live and live[0].job_lines())
check("one blockquote per job, one line break between",
      "</blockquote>\n<blockquote>dev push" in html_m)

print("== P3 approved preview ==")
with open(os.path.join(FIX_P, "framework-agent-approved.html"), encoding="utf-8") as fh:
    approved = fh.read()
check("framework agent HTML == approved preview", html_m == approved,
      "\n".join(__import__("difflib").unified_diff(
          approved.split("\n"), html_m.split("\n"), "approved", "rendered", lineterm="")))

print("== P4 kit section unchanged ==")
kit = ag_spec["plugin_sections"][0]["variants"][0]
kit_html = balance_telegram_html(sanitize_message_for_telegram(
    "\n".join(sf_a._quote_wrap(kit)), rail=RAIL_CONSUMER)).rstrip("\n")
check("lifekit-style section renders as the same bytes under the new top line",
      html_a.endswith(kit_html) and kit_html.startswith("<blockquote expandable>"),
      kit_html[:80])
with open(os.path.join(FIX, "rendered-8.html"),
          encoding="utf-8") as fh:
    old = fh.read()
check("...and as the pre-change golden (wb-quote) rendered it",
      kit_html in old, kit_html[:80])

print("== P5 own head recognized ==")
check("new top line is a bare board head",
      sf_m._is_bare_board_head(sf_m._board_title_line()))
check("a foreign first line is not", not sf_m._is_bare_board_head("hello\nx"))

print("== P6 seven and twelve separate quotes ==")
for count in (7, 12):
    many = [sf_m.LiveRow("Job %02d" % i, "legacy", model="Claude Opus 5.5",
                         start_epoch=NOW - 60 * i)
            for i in range(count)]
    html_f = render_p(sf_m, {"plugin_sections": []}, many)
    check("%d jobs -> %d separate plain quotes" % (count, count),
          len(quotes(html_f)) == count
          and all(tag == PLAIN for tag, _ in quotes(html_f)), html_f)
    snapshot("rendered-%d.html" % count, html_f)

print("== DGN-2275 message-length overflow ==")
many = [sf_m.LiveRow("Job %02d " % i + "\U0001f680" * 100, "legacy",
                     model="Claude Opus 5.5", start_epoch=NOW - 60)
        for i in range(40)]
raw_f = sf_m._build_dashboard([], many, [])
html_f = render(raw_f).rstrip("\n")
qs = quotes(html_f)
plain = sum(tag == PLAIN for tag, _ in qs)
check("overflow keeps individual prefix quotes and one counted fold",
      0 < plain < len(many) and qs[-1] == (
          FOLD, sf_m._board_str("live_fold") % (len(many) - plain)), qs)
check("overflow fits both board and Telegram UTF-16 budgets",
      sf_m._u16len(raw_f) <= sf_m.DASHBOARD_MAX_UNITS
      and sf_m._u16len(html_to_plain_text(html_f)) <= 4096)
check("overflow quotes are balanced with no marker leaks",
      html_f.count("<blockquote") == html_f.count("</blockquote>")
      and "fold::" not in html_f and "quote::" not in html_f)
snapshot("rendered-overflow.html", html_f)

print("== DGN-2275 instance helper term ==")
# Use the actual chat terms writer against a scratch instance, then the
# same board and Telegram converter as the pinned message.
import shutil  # noqa: E402
import subprocess  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    shutil.copytree(os.path.join(TEMPLATE, "config", "i18n"),
                    os.path.join(td, "config", "i18n"))
    subprocess.run([sys.executable, os.path.join(ROUTINES, "terms.py"),
                    "set", "--root", td, "--lang", "ko", "--term",
                    "\uc11c\ube0c\uc5d0\uc774\uc804\ud2b8", "--to",
                    "\ub3d9\uc0dd", "--yes"], check=True, capture_output=True)
    old_root = sf_m._ROOT_DIR
    sf_m._ROOT_DIR = td
    try:
        renamed = render_p(sf_m, metal_spec)
        check("user term appears in the rendered job actor",
              "\u2022 \ub3d9\uc0dd: " in renamed
              and "\uc11c\ube0c\uc5d0\uc774\uc804\ud2b8" not in renamed,
              renamed)
        snapshot("rendered-user-term.html", renamed)
    finally:
        sf_m._ROOT_DIR = old_root


print("== DGN-2229 workbench block per case ==")
sf_m.run_block.local_host = lambda: "Fake Host"
CASES = [
    ("local subscription", "claude-code", "sonnet", "", "• 서브에이전트: Claude Sonnet 5.5"),
    ("claude cloud subscription", "cloud", "sonnet", "", "• 서브에이전트: Claude Sonnet 5.5 (Cloud)"),
    ("claude cloud credit", "cloud", "sonnet", "credit", "• 서브에이전트: Claude Sonnet 5.5 (Cloud credit)"),
    ("codex cloud", "codex-cloud", "environment-default", "", "• 서브에이전트: Codex (Cloud)"),
    ("local api credit", "claude-api", "sonnet", "", "• 서브에이전트: Claude Sonnet 5.5 (API credit)"),
]
for label, rt, alias, billing, want in CASES:
    bare, tag = sf_m._place(rt, alias)
    if billing:
        tag = sf_m.run_block.tag(rt, billing)
    row = sf_m.LiveRow("title", "legacy", model=bare, start_epoch=NOW, bare=bare, tag=tag)
    got = row.job_lines()
    print("[%s]\n%s" % (label, "\n".join(got)))
    check("workbench " + label, got[0] == "title 17:41 (0분~)" and got[1] == want, got)
other = sf_m.run_block.tag("codex", host="Other Host")
row = sf_m.LiveRow("title", "legacy", model="GPT 6.1 Sol", start_epoch=NOW, bare="GPT 6.1 Sol", tag=other)
print("[other device]\n" + "\n".join(row.job_lines()))
check("workbench other device", row.job_lines()[1] == "• 서브에이전트: GPT 6.1 Sol (Other Host)")
print("[script on this device]\n" + "\n".join(sf_m.LiveRow("title", "background", name="title", job_start=NOW).job_lines()))
check("workbench script", sf_m.LiveRow("title", "background", name="title", job_start=NOW).job_lines()[1] == "• 스크립트")

print("\n===========================")
print("Results: %d passed, %d failed" % (PASS, FAIL))
print("===========================")
sys.exit(1 if FAIL else 0)

