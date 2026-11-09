#!/usr/bin/env python3
# test-lane: fast
"""DGN-1891: nightly persona/skill drift audit (routines/persona-audit.py)
and its dawn-queue step C.

  A. audit checks: missing template workflow, stale history line, custom
     skill overlapping a shipped one, empty owner profile with memory facts;
     a clean instance yields zero proposals; nothing under the instance is
     edited except the output json.
  T. template resolution: DOGANY_REPO_ROOT from .instance.conf; unresolved
     template skips the template checks with a note (other checks still run).
  R. --render prints a numbered DRAFT message; empty when nothing found.
  Q. dawn-memory-queue step C: runs after consolidate, summary carries
     audit=<ran>/<proposals>; a failing or missing audit never counts into
     fail= and the queue exits 0; DMQ_PERSONA_AUDIT=0 disables it.
  C. canon review (DGN-2052): after an update changed the canon, a custom
     line the added canon covers -> canon_covered (overlay / persona /
     local skill, apply via the owner-confirmed path, persona via
     baseline-editor); a custom line echoing removed canon ->
     canon_retired; nothing is edited; no snapshot -> no canon check; no
     overlap -> the snapshot is consumed.
  X. framework docs never point at a persona workflow the template lacks
     (the vendors/telegram.md "Output ownership" dangling-reference class).

Run: python3 routines/tests/test-dgn1891-persona-audit.py
Exit: 0 all pass, 1 any fail.
"""

import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(TEMPLATE, "routines", "persona-audit.py")
DMQ = os.path.join(TEMPLATE, "routines", "dawn-memory-queue.sh")
spec = importlib.util.spec_from_file_location("persona_audit", SCRIPT)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS: " + name)
    else:
        FAIL += 1
        print("  FAIL: %s %s" % (name, detail))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


TMPL_PERSONA = open(os.path.join(TEMPLATE, "identity", "hot.custom.agent.md"),
                    encoding="utf-8").read()


def make_instance(base, persona=None, owner="# USER\n\n<!-- guide -->\n",
                  skills=None, memories=None, repo_root=None):
    root = os.path.join(base, "inst")
    write(os.path.join(root, ".instance.conf"),
          "DOGANY_REPO_ROOT=%s\n" % (repo_root or ""))
    write(os.path.join(root, "identity", "hot.custom.agent.md"),
          TMPL_PERSONA if persona is None else persona)
    write(os.path.join(root, "identity", "hot.custom.owner.md"), owner)
    for name, desc in (skills or {}).items():
        write(os.path.join(root, ".claude", "skills", name, "SKILL.md"),
              "---\nname: %s\ndescription: %s\n---\nbody\n" % (name, desc))
    for name, text in (memories or {}).items():
        write(os.path.join(root, "memories", name), text)
    return root


def run(root, *extra):
    p = subprocess.run([sys.executable, SCRIPT, "--root", root,
                        "--template", TEMPLATE] + list(extra),
                       capture_output=True, text=True)
    data = None
    out = os.path.join(root, pa.OUT_REL)
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
    return p, data


def snapshot(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            path = os.path.join(dp, f)
            if path.endswith(pa.OUT_REL):
                continue
            with open(path, "rb") as fh:
                out[path] = fh.read()
    return out


def drop_workflow(text, heading):
    lines, out, skip = text.splitlines(), [], False
    for line in lines:
        if line.startswith("### "):
            skip = line[4:].startswith(heading)
        elif line.startswith("## "):
            skip = False
        if not skip:
            out.append(line)
    return "\n".join(out) + "\n"


def kinds(data):
    return sorted(p["kind"] for p in data["proposals"])


def test_audit(base):
    print("A: audit checks")
    clean = make_instance(os.path.join(base, "clean"),
                          skills={"dogany-mailer": "Send mail through SMTP"})
    p, d = run(clean)
    check("A0 clean instance: rc 0", p.returncode == 0, p.stderr)
    check("A0 clean instance: zero proposals", d and d["proposals"] == [], d)
    check("A0 summary line", p.stdout.startswith("persona-audit: proposals=0"), p.stdout)

    persona = drop_workflow(TMPL_PERSONA, "Response skeleton")
    persona = drop_workflow(persona, "Default parallel autonomy")
    persona = persona.replace(
        "## Workflows\n",
        "## Workflows\n- 2026-08-01 switched the morning brief to 7am (history)\n", 1)
    drift = make_instance(
        os.path.join(base, "drift"), persona=persona,
        skills={
            "dogany-computer-use": "Computer use - see the screen, click, type in desktop apps on this Mac",
            "mac-screen-click": "click buttons on the screen of desktop apps",
            "recipe-box": "store cooking recipes",
        },
        memories={"inbox.md": "- owner timezone is Asia/Seoul (2026-09-01)\n- likes tea\n"})
    before = snapshot(drift)
    p, d = run(drift)
    check("A1 drift: rc 0", p.returncode == 0, p.stderr)
    got = kinds(d)
    check("A1 two missing workflows",
          got.count("missing_workflow") == 2, got)
    mw = " ".join(x["summary"] for x in d["proposals"] if x["kind"] == "missing_workflow")
    check("A1 names Response skeleton", "Response skeleton" in mw, mw)
    check("A2 stale history line flagged", got.count("stale_history") == 1, got)
    sh = [x for x in d["proposals"] if x["kind"] == "stale_history"][0]
    check("A2 evidence quotes the line", "2026-08-01" in sh["evidence"][0], sh)
    check("A2 template lines with DGN refs not flagged",
          len(sh["evidence"]) == 1, sh["evidence"])
    dup = [x for x in d["proposals"] if x["kind"] == "duplicate_skill"]
    check("A3 one overlapping skill", len(dup) == 1, dup)
    check("A3 pairs the custom with the shipped skill",
          dup and "mac-screen-click" in dup[0]["summary"]
          and "dogany-computer-use" in dup[0]["summary"], dup)
    op = [x for x in d["proposals"] if x["kind"] == "owner_profile_empty"]
    check("A4 empty owner profile with memory fact", len(op) == 1, got)
    check("A4 evidence cites memory", op and "Asia/Seoul" in op[0]["evidence"][0], op)
    check("A5 ids are 1..n", [x["id"] for x in d["proposals"]]
          == list(range(1, len(d["proposals"]) + 1)), d["proposals"])
    check("A6 nothing else edited", snapshot(drift) == before)

    filled = make_instance(os.path.join(base, "filled"),
                           owner="# USER\n- Timezone: Asia/Seoul (2026-09-01, owner)\n",
                           memories={"inbox.md": "- timezone Asia/Seoul\n"})
    _, d = run(filled)
    check("A7 owner profile with facts: no proposal", d["proposals"] == [], d)
    nomem = make_instance(os.path.join(base, "nomem"))
    _, d = run(nomem)
    check("A8 empty profile, no memory facts: no proposal", d["proposals"] == [], d)


def test_template(base):
    print("T: template resolution")
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, "agents"))
    os.symlink(TEMPLATE, os.path.join(repo, "agents", ".template"))
    root = make_instance(os.path.join(base, "t1"), repo_root=repo,
                         persona=drop_workflow(TMPL_PERSONA, "Ops"))
    p = subprocess.run([sys.executable, SCRIPT, "--root", root],
                       capture_output=True, text=True)
    d = json.load(open(os.path.join(root, pa.OUT_REL)))
    check("T1 DOGANY_REPO_ROOT resolves the template",
          d["template"].endswith(os.path.join("agents", ".template")), d["template"])
    check("T1 missing Ops found", kinds(d) == ["missing_workflow"], kinds(d))
    root2 = make_instance(os.path.join(base, "t2"), repo_root="",
                          persona="# AGENT\n",
                          memories={"a.md": "- tz: Asia/Seoul\n"})
    p = subprocess.run([sys.executable, SCRIPT, "--root", root2],
                       capture_output=True, text=True)
    d = json.load(open(os.path.join(root2, pa.OUT_REL)))
    check("T2 no template: rc 0", p.returncode == 0, p.stderr)
    check("T2 no template: note recorded", d["notes"] and "template" in d["notes"][0], d)
    check("T2 no template: other checks still run",
          kinds(d) == ["owner_profile_empty"], kinds(d))
    p = subprocess.run([sys.executable, SCRIPT, "--root", os.path.join(base, "nope")],
                       capture_output=True, text=True)
    check("T3 missing root: rc 2", p.returncode == 2, p.returncode)


def test_render(base):
    print("R: render")
    root = make_instance(os.path.join(base, "r"),
                         persona=drop_workflow(TMPL_PERSONA, "Ops"))
    p, _ = run(root, "--render")
    lines = p.stdout.strip().splitlines()
    check("R1 DRAFT header", lines and lines[0].startswith("[DRAFT]"), p.stdout)
    check("R1 numbered item", len(lines) > 1 and lines[1].startswith("1. "), p.stdout)
    clean = make_instance(os.path.join(base, "r2"))
    p, _ = run(clean, "--render")
    check("R2 nothing found: no text", p.stdout == "", p.stdout)


def new_queue(base, name, audit_body):
    qh = os.path.join(base, name, "home")
    inst = os.path.join(base, name, "inst-a")
    os.makedirs(os.path.join(qh, ".dogany", "memory-queue", "instances.d"))
    os.makedirs(os.path.join(inst, "routines"))
    os.makedirs(os.path.join(inst, ".telegram_bot"))
    write(os.path.join(inst, ".instance.conf"), "")
    write(os.path.join(inst, ".telegram_bot", ".env"), "")
    write(os.path.join(inst, "routines", "cron-guard.sh"),
          '#!/bin/bash\nwhile [[ $# -gt 0 && "$1" != "--" ]]; do shift; done\nshift\nexec "$@"\n')
    write(os.path.join(inst, "routines", "consolidate-0430.sh"), "#!/bin/bash\nexit 0\n")
    write(os.path.join(inst, "routines", "classify-inbox-check.sh"), "#!/bin/bash\nexit 0\n")
    if audit_body is not None:
        write(os.path.join(inst, "routines", "persona-audit.py"), audit_body)
    for f in glob.glob(os.path.join(inst, "routines", "*")):
        os.chmod(f, 0o755)
    write(os.path.join(qh, ".dogany", "memory-queue", "instances.d", "a.conf"),
          "ROOT=%s\nENABLED=1\nPRIORITY=10\n" % inst)
    return qh


def run_dmq(qh, **env):
    e = dict(os.environ, HOME=qh, DMQ_CLI_UPDATE="0", DMQ_ALERT_ROOT="")
    e.update(env)
    p = subprocess.run(["/bin/bash", DMQ], capture_output=True, text=True, env=e)
    log = ""
    for f in glob.glob(os.path.join(qh, ".dogany", "logs", "dawn-memory-queue", "queue-*.log")):
        log += open(f, encoding="utf-8").read()
    return p.returncode, log


def test_queue(base):
    print("Q: dawn-memory-queue step C")
    ok = 'import sys\nprint("persona-audit: proposals=3 missing_workflow=3")\n'
    rc, log = run_dmq(new_queue(base, "q1", ok))
    check("Q1 queue exit 0", rc == 0, log[-800:])
    check("Q1 step C ran after step A",
          log.find("step A ok") < log.find("step C: persona audit ok"), log[-800:])
    check("Q1 summary audit=1/3", "audit=1/3" in log, log[-400:])
    rc, log = run_dmq(new_queue(base, "q2", "import sys\nsys.exit(5)\n"))
    check("Q2 failing audit: queue exit 0", rc == 0, log[-800:])
    check("Q2 failing audit: fail=0 audit=0/0",
          "fail=0" in log and "audit=0/0" in log, log[-400:])
    rc, log = run_dmq(new_queue(base, "q3", None))
    check("Q3 missing audit: skipped, exit 0",
          rc == 0 and "persona audit SKIPPED" in log, log[-800:])
    rc, log = run_dmq(new_queue(base, "q4", ok), DMQ_PERSONA_AUDIT="0")
    check("Q4 disabled: not run",
          rc == 0 and "DISABLED" in log and "audit=0/0" in log, log[-800:])


OLD_CANON = """# hot.framework.md

## Problem solving
- Cheapest check first. Search before any absence claim.
- Solve plus ask: weigh every alternative by total cost spent so far before asking.
"""
NEW_CANON = """# hot.framework.md

## Problem solving
- Cheapest check first. Search before any absence claim.
- Compare alternatives, doing nothing included, only by cost and gain from now on; spent cost never counts.
"""


def test_canon(base):
    print("C: canon review after an update")
    persona = TMPL_PERSONA.replace(
        "## Workflows\n",
        "## Workflows\n- Compare alternatives including doing nothing by cost and gain from now on.\n", 1)
    root = make_instance(os.path.join(base, "canon"), persona=persona,
                         skills={"trip-plan": "plan trips"})
    write(os.path.join(root, "rules", "hot.framework.md"), NEW_CANON)
    write(os.path.join(root, pa.CANON_PREV_REL), OLD_CANON)
    write(os.path.join(root, "rules", "hot.custom.md"),
          "# hot.custom.md\n"
          "- Spent cost never counts: compare alternatives by cost and gain from now on.\n"
          "- Weigh every alternative by total cost spent so far before asking.\n"
          "- Morning brief at 7am in Korean.\n")
    write(os.path.join(root, ".claude", "skills", "trip-plan", "SKILL.md"),
          "---\nname: trip-plan\ndescription: plan trips\n---\n"
          "- Cheapest check first and search before any absence claim.\n")
    before = snapshot(root)
    p, d = run(root)
    got = [(x["kind"], x["evidence"][0].split(":")[0]) for x in d["proposals"]] if d else []
    check("C1 overlay line the new canon covers -> canon_covered",
          ("canon_covered", "rules/hot.custom.md") in got, got)
    check("C2 overlay line echoing removed canon -> canon_retired",
          ("canon_retired", "rules/hot.custom.md") in got, got)
    check("C3 persona workflow line covered -> canon_covered via baseline-editor",
          any(x["kind"] == "canon_covered" and x["evidence"][0].startswith("identity/")
              and "baseline-editor" in x["apply"] for x in d["proposals"]), d)
    check("C4 unrelated line and line matching unchanged canon -> no proposal",
          len(got) == 3 and not any("Morning" in e or "trip-plan" in e
                                    for x in d["proposals"] for e in x["evidence"]), got)
    check("C5 nothing under the instance edited (snapshot kept while items open)",
          snapshot(root) == before)
    check("C6 summary counts canon kinds",
          "canon_covered=2" in p.stdout and "canon_retired=1" in p.stdout, p.stdout)
    write(os.path.join(root, ".claude", "skills", "trip-plan", "SKILL.md"),
          "---\nname: trip-plan\ndescription: plan trips\n---\n"
          "- Weigh every alternative by total cost spent so far before asking.\n")
    _, d = run(root)
    check("C7 local skill echoing removed canon -> canon_retired",
          any(x["kind"] == "canon_retired" and "trip-plan" in x["evidence"][0]
              for x in d["proposals"]), d)

    clean = make_instance(os.path.join(base, "canon-clean"))
    write(os.path.join(clean, "rules", "hot.framework.md"), NEW_CANON)
    write(os.path.join(clean, "rules", "hot.custom.md"), "# hot.custom.md\n- Morning brief at 7am in Korean.\n")
    _, d = run(clean)
    check("C8 no snapshot -> no canon check", d and d["proposals"] == [], d)
    write(os.path.join(clean, pa.CANON_PREV_REL), OLD_CANON)
    _, d = run(clean)
    check("C9 no overlap -> zero proposals, snapshot consumed",
          d and d["proposals"] == [] and not os.path.exists(os.path.join(clean, pa.CANON_PREV_REL)),
          d)


def test_xref():
    print("X: framework docs name only template workflows")
    have = {k for k, _ in pa.workflows(TMPL_PERSONA)}
    repo = os.path.dirname(os.path.dirname(TEMPLATE))
    files = []
    for sub in ("rules", "vendors", "playbooks"):
        files += glob.glob(os.path.join(TEMPLATE, sub, "**", "*.md"), recursive=True)
    files += glob.glob(os.path.join(repo, "rules", "*.md"))
    pat = re.compile(r"(?:persona(?: file)?'s|AGENT)\s+['\"]?([A-Z][A-Za-z -]+?)['\"]?\s+workflow")
    bad = []
    for f in files:
        for n, line in enumerate(open(f, encoding="utf-8"), 1):
            for m in pat.finditer(line):
                if pa.workflow_key(m.group(1)) not in have:
                    bad.append("%s:%d %s" % (os.path.relpath(f, repo), n, m.group(1)))
    check("X1 scanned some files", len(files) > 3, files)
    check("X1 no dangling workflow reference", not bad, bad)
    check("X2 the matcher catches the old telegram.md shape",
          pat.search("(Full routing rule: the persona file's Output ownership workflow --")
          is not None)


def main():
    base = tempfile.mkdtemp(prefix="dgn1891-")
    try:
        test_audit(base)
        test_template(base)
        test_render(base)
        test_queue(base)
        test_canon(base)
        test_xref()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
