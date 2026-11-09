#!/usr/bin/env python3
# test-lane: fast
"""DGN-1390 -- owner words in a ticket must reach code (owner-reach).

  P1  predicate: R1 blockquote under an owner-cue line counts; a blockquote
      under a non-owner line does not; the force / suppress markers work
  P2  predicate: R2 quoted span in the SAME sentence as the cue counts; a
      quote in the next sentence, a table row, a code span, a placeholder
      and a single-token ASCII span do not; "worded_by=owner" is no cue
  P3  frontmatter and fenced code are never scanned
  R1  reach: a real file:line meets the quote; a missing file / a line past
      the end is PHANTOM and does not meet it; none: <reason> meets it; a
      reach id naming no quote is an ORPHAN; a clock time is no coordinate
  L1  lint off (default): no owner-reach output
  L2  lint warn: done transition with an unmet quote -> FORCING-POINT,
      no block decision; met -> silent; phantom on a live ticket -> advisory
  L3  lint block: same transition -> decision=block
  C1  count line: unmet / done-unmet / phantom numbers; empty when clean
  S1  session-recap: OWNER_REACH=warn puts the count line in the block

Run:  /usr/bin/python3 routines/tests/test-dgn1390-owner-reach.py
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
ROUTINES = os.path.dirname(HERE)
LINT = os.path.join(ROUTINES, "ticket-status-enum-lint.py")
CLI = os.path.join(ROUTINES, "owner-reach.py")
RECAP = os.path.join(ROUTINES, "session-recap.py")
sys.path.insert(0, os.path.join(ROUTINES, "lib"))
import owner_reach as orx  # noqa: E402

OWNER_KO = "오너"  # the Korean word for owner

WORK = tempfile.mkdtemp(prefix="dgn1390.")
CONF = os.path.join(WORK, "config", "agent.conf")
WL = os.path.join(WORK, "worklog")
os.makedirs(os.path.dirname(CONF))
os.makedirs(WL)
os.makedirs(os.path.join(WORK, "src"))
with open(os.path.join(WORK, "src", "real.py"), "w") as fh:
    fh.write("a = 1\nb = 2\nc = 3\n")
subprocess.run(["git", "-C", WORK, "init", "-q"], check=True)

ENV = dict(os.environ)
ENV["DOGANY_AGENT_CONF"] = CONF
for k in ("DOGANY_OWNER_REACH", "TICKET_FILE_GLOB", "TICKET_PREFIX", "DOGANY_LANG"):
    ENV.pop(k, None)
os.environ.update({"DOGANY_AGENT_CONF": CONF})
os.environ.pop("DOGANY_OWNER_REACH", None)

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
        fh.write("TICKET_PREFIX=TST\n" + text)


def texts(md):
    return [q["text"] for q in orx.owner_quotes(md)]


print("--- P1: R1 blockquote ---")
md = ("---\nstatus: open\n---\n"
      "The " + "owner asked at the end (verbatim):\n\n"
      "> **\"why is none of this fixed yet\"**\n\n"
      "The reviewer's argument is decisive (verbatim):\n\n"
      "> a rule with no counting machine becomes a document again\n")
got = texts(md)
check("blockquote under owner-cue line counts",
      got == ["why is none of this fixed yet"], repr(got))
check("blockquote under a non-owner line does not count",
      not any("counting machine" in t for t in got), repr(got))
md2 = ("<!-- owner-verbatim -->\n> keep the night quiet\n\n"
       "the owner said:\n<!-- not-owner -->\n> sample text only\n")
got = texts(md2)
check("force marker counts, suppress marker drops",
      got == ["keep the night quiet"], repr(got))
check("Korean cue works", texts(OWNER_KO + " 지시: *\"이거 "
                                "올려봐\"* (06:46)\n")
      == ["이거 올려봐"])

print("--- P2: R2 inline ---")
check("same-sentence quote counts",
      texts("the owner decided \"include the forcing point\" yesterday.\n")
      == ["include the forcing point"])
check("quote in the next sentence does not count",
      texts("The owner opened two paths. The reviewer said \"one change "
            "for both\".\n") == [])
check("table row does not count",
      texts("| owner | \"some quoted words\" |\n") == [])
check("code span does not count",
      texts("owner text: `\"inside code here\"`\n") == [])
check("placeholder does not count",
      texts("owner signs: --quote \"<the owner's words verbatim>\"\n") == [])
check("single ASCII token does not count",
      texts("owner field \"global\" stays\n") == [])
check("worded_by=owner is no cue",
      texts("worded_by=owner keeps \"two words\" here\n") == [])

print("--- P3: frontmatter / fences ---")
md3 = ("---\nnote: the owner said \"not scanned here\"\n---\n"
       "```\nthe owner said \"inside a fence\"\n```\n")
check("frontmatter and fences skipped", texts(md3) == [], repr(texts(md3)))

print("--- R1: reach lines + coordinate reality ---")
QA = "why is none of this fixed yet"
QB = "keep the night quiet"
ida, idb = orx.quote_id(QA), orx.quote_id(QB)


def ticket(name, status, reach, extra=""):
    p = os.path.join(WL, name)
    with open(p, "w") as fh:
        fh.write("---\nid: TST-1\ntitle: t\ndisplay_title.en: t\n"
                 "status: %s\n---\n" % status
                 + "the owner asked:\n> \"%s\"\n\n" % QA
                 + "the owner said \"%s\" at night.\n\n" % QB
                 + extra + reach)
    return p


p = ticket("TST-1-a.md", "open",
           "reach: %s src/real.py:2\nreach: %s none: tone only, no code surface\n"
           % (ida, idb))
rep = orx.ticket_report(p, [WORK])
check("real coordinate + none reason -> all met",
      not rep["unmet"] and not rep["phantoms"], json.dumps(rep)[:300])
p = ticket("TST-1-b.md", "open",
           "reach: %s src/real.py:9\nreach: %s src/gone.py:1\n" % (ida, idb))
rep = orx.ticket_report(p, [WORK])
whys = sorted(x["why"] for x in rep["phantoms"])
check("line past end + missing file -> 2 phantoms, both unmet",
      len(rep["phantoms"]) == 2 and len(rep["unmet"]) == 2
      and "no such file" in whys and any("past end" in w for w in whys),
      json.dumps(rep["phantoms"]))
p = ticket("TST-1-c.md", "open",
           "reach: q-000000 src/real.py:1\nreach: %s none: short\n" % ida)
rep = orx.ticket_report(p, [WORK])
check("orphan id reported; too-short none reason does not meet",
      rep["orphans"] and rep["orphans"][0]["id"] == "q-000000"
      and len(rep["unmet"]) == 2, json.dumps(rep)[:300])
check("clock time is no coordinate",
      orx.reach_lines("reach: q-abcdef none: said at 06:46 by voice\n")[0]["coords"] == [])
check("range coordinate parsed",
      orx.reach_lines("reach: q-abcdef src/real.py:1-3\n")[0]["coords"]
      == [("src/real.py", 1, 3)])
for f in os.listdir(WL):
    os.remove(os.path.join(WL, f))


def lint(path):
    payload = {"tool_name": "Write", "tool_input": {"file_path": path},
               "session_id": ""}
    out = subprocess.run([sys.executable, LINT], input=json.dumps(payload),
                         capture_output=True, text=True, env=ENV, timeout=20)
    return json.loads(out.stdout) if out.stdout.strip() else {}


EVID = "merge_sha: abcdef1234\n"
print("--- L1: off by default ---")
set_conf("")
p = ticket("TST-2-x.md", "done", "", EVID)
out = lint(p)
check("OWNER_REACH unset -> no owner-reach output",
      "owner" not in json.dumps(out).lower(), json.dumps(out)[:300])

print("--- L2: warn ---")
set_conf("OWNER_REACH=warn\n")
out = lint(p)
ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
check("done + unmet -> FORCING-POINT", "owner words not reached" in ctx
      and ida in ctx, ctx[:300])
check("warn -> no block decision", "decision" not in out, json.dumps(out)[:200])
p = ticket("TST-2-x.md", "done",
           "reach: %s src/real.py:1\nreach: %s none: tone only, no code surface\n"
           % (ida, idb), EVID)
out = lint(p)
check("done + all met -> silent", out == {}, json.dumps(out)[:300])
p = ticket("TST-3-y.md", "open", "reach: %s src/real.py:99\n" % ida)
ctx = lint(p).get("hookSpecificOutput", {}).get("additionalContext", "")
check("live ticket phantom -> advisory", "phantom" in ctx
      and "src/real.py:99" in ctx, ctx[:300])

print("--- L3: block ---")
set_conf("OWNER_REACH=block\n")
p = ticket("TST-2-x.md", "done", "", EVID)
out = lint(p)
check("block -> decision=block on done transition",
      out.get("decision") == "block" and ida in out.get("reason", ""),
      json.dumps(out)[:300])

print("--- C1: count line ---")
spec = importlib.util.spec_from_file_location("orcli", CLI)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
text, tot = cli.count_line(WL)
check("count: unmet in done ticket counted",
      tot["closed_unmet"] == 2 and tot["phantoms"] == 1 and tot["unmet"] == 4
      and "4 owner quote(s) not reached" in text, text)
for f in os.listdir(WL):
    os.remove(os.path.join(WL, f))
text, tot = cli.count_line(WL)
check("count: clean worklog -> empty line", text == "", text)

print("--- S1: session-recap carries the count ---")
set_conf("OWNER_REACH=warn\n")
ticket("TST-4-z.md", "open", "")
proj = os.path.join(WORK, "proj")
os.makedirs(proj)
with open(os.path.join(proj, "old.jsonl"), "w") as fh:
    fh.write(json.dumps({"type": "user", "message": {"role": "user",
                                                       "content": "hello"}}) + "\n")
open(os.path.join(proj, "cur.jsonl"), "w").close()
os.utime(os.path.join(proj, "old.jsonl"), (1, 1))
payload = {"source": "startup", "cwd": WORK,
           "transcript_path": os.path.join(proj, "cur.jsonl")}
out = subprocess.run([sys.executable, RECAP], input=json.dumps(payload),
                     capture_output=True, text=True, env=ENV, timeout=20)
check("recap block names the unmet count",
      "owner-reach: 2 owner quote(s) not reached" in out.stdout, out.stdout[:400])
set_conf("")
out = subprocess.run([sys.executable, RECAP], input=json.dumps(payload),
                     capture_output=True, text=True, env=ENV, timeout=20)
check("recap off -> no count line", "owner-reach" not in out.stdout,
      out.stdout[:300])

shutil.rmtree(WORK, ignore_errors=True)
print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
