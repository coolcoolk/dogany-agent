#!/usr/bin/env python3
# test-lane: fast
# test-dgn768-card-accent.py -- DGN-768 S1+S2: agent.conf AGENT_ACCENT drives
# the card accent; unassigned stays byte-identical; raw-hex lint on card code.
import os, re, sys, tempfile, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.normpath(os.path.join(HERE, "..", "lib"))
sys.path.insert(0, LIB)
spec = importlib.util.spec_from_file_location("card_blocks", os.path.join(LIB, "card_blocks.py"))
cb = importlib.util.module_from_spec(spec); spec.loader.exec_module(cb)
dt = cb.load_design_tokens()
F = []


def check(name, ok):
    print("  %s %s" % ("ok  " if ok else "FAIL", name))
    if not ok:
        F.append(name)


def conf(text):
    fd, path = tempfile.mkstemp(suffix=".conf")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path

L = {"DOGANY_AGENT_CLASS": "main"}
D = {"DOGANY_AGENT_CLASS": "domain"}
missing = "/nonexistent/agent.conf"

check("no conf -> CARD_LIGHT byte-identical", cb.card_palette(missing, D) == cb.CARD_LIGHT)
check("empty field -> unchanged", cb.card_palette(conf("AGENT_ACCENT=\n"), D) == cb.CARD_LIGHT)
check("teal field -> unchanged", cb.card_palette(conf("AGENT_ACCENT=teal\n"), D) == cb.CARD_LIGHT)
p = cb.card_palette(conf("AGENT_ACCENT=coral\n"), D)
check("field set -> accent used", p["accent"] == dt.accent_for("coral", "teal-pale") and p["accent"] != cb.CARD_LIGHT["accent"])
check("only accent slot changes", {k: v for k, v in p.items() if k != "accent"} == {k: v for k, v in cb.CARD_LIGHT.items() if k != "accent"})
c = conf("AGENT_ACCENT=coral\nAGENT_CREW=devkit\n")
check("override beats crew color", cb.resolved_accent_name(c, L) == "coral")
c = conf("AGENT_CREW=devkit\n")
check("leader crew color when no override", cb.resolved_accent_name(c, L) == "cobalt")
check("domain agent ignores crew color", cb.resolved_accent_name(c, D) == "teal")
check("kit domain color", cb.resolved_accent_name(conf("AGENT_KIT_ACCENT=violet\n"), D) == "violet")
check("unknown name -> safe fallback", cb.card_palette(conf("AGENT_ACCENT=notacolor\n"), D) == cb.CARD_LIGHT)
check("garbage/quoted value ok", cb.resolved_accent_name(conf('AGENT_ACCENT="Rose"\n'), D) == "rose")
check("CardStack default reads resolver", cb.CardStack().palette == cb.card_palette())

# Lint: raw hex literals in card code only inside the ICON_COLORS table.
HEX = re.compile(r"#[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b|rgba?\(")
for fn in ("card_blocks.py", "card_blocks_demo.py"):
    lines = open(os.path.join(LIB, fn)).read().splitlines()
    inside, bad = False, []
    for i, ln in enumerate(lines, 1):
        if ln.startswith("ICON_COLORS = {"):
            inside = True
        elif inside and ln.startswith("}"):
            inside = False
            continue
        if not inside and HEX.search(re.sub(r"^\s*#.*$", "", ln)) and not ln.lstrip().startswith("#"):
            bad.append((i, ln.strip()))
    check("no raw hex in %s outside ICON_COLORS %s" % (fn, bad[:3]), not bad)

# lint self-check: the pattern must catch a planted literal
check("lint pattern catches planted hex", bool(HEX.search('color:#13AB9C;')))
print("FAILED: %s" % F if F else "all pass")
sys.exit(1 if F else 0)
