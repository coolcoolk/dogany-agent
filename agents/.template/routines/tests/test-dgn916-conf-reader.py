#!/usr/bin/env python3
"""DGN-916 acceptance tests: single conf parser helper.

Covers:
  1. Helper normalization: quoted / single-quoted / unquoted / value with
     spaces / empty value / missing key / missing file -> exact contract.
  2. status-footer LIVE_LABEL end-to-end: quoted value in agent.conf is
     rendered without quotes using ONLY the helper (the 6cc9e65 inline
     strip patch is gone from status-footer).
  3. Consumer regression: usage-gate read_plan, onboarding-check
     resolve_kit / portfolio_pending / instance_class / lifekit_pending,
     session-recap _load_recap_config -- quoted and unquoted values give
     the same (correct) result; fail-open on missing file.

Run: python3 routines/tests/test-dgn916-conf-reader.py
Exit 0 = all pass.
"""

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)
LIB = os.path.join(ROUTINES, "lib")
sys.path.insert(0, LIB)

from conf_reader import conf_get  # noqa: E402

FAILURES = []

# The exact production label from the DGN-916 acceptance criterion
# ("dongsaeng at work"), kept as escapes so the source stays ASCII-only.
LIVE_LABEL_KO = "동생 작업 중"


def check(name, got, want):
    if got == want:
        print("PASS  %s" % name)
    else:
        print("FAIL  %s: got %r want %r" % (name, got, want))
        FAILURES.append(name)


def load_script(name, modname):
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(ROUTINES, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with tempfile.TemporaryDirectory() as td:
    conf = os.path.join(td, "agent.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(
            "# comment line\n"
            "\n"
            'DASHBOARD_LIVE_LABEL="%s"\n' % LIVE_LABEL_KO +
            "PLAN=max_20x\n"
            "SINGLEQ='hello world'\n"
            "SPACED=  padded value  \n"
            "EMPTY=\n"
            'EMPTYQ=""\n'
            "KIT=\"pending\"\n"
            "PORTFOLIO=pending\n"
            "AGENT_LANG=KO\n"
            "RECAP_PAIRS=\"3\"\n"
            "RECAP_CHAR_CAP=abc\n"
            "DOGANY_AGENT_CLASS=\"Domain\"\n"
            "LIFEKIT=pending\n"
        )

    # ---- 1. helper normalization contract -------------------------------
    check("quoted value unquoted", conf_get("DASHBOARD_LIVE_LABEL", conf),
          LIVE_LABEL_KO)
    check("unquoted value", conf_get("PLAN", conf), "max_20x")
    check("single-quoted value", conf_get("SINGLEQ", conf), "hello world")
    check("whitespace stripped", conf_get("SPACED", conf), "padded value")
    check("empty value", conf_get("EMPTY", conf), "")
    check("empty quoted value", conf_get("EMPTYQ", conf), "")
    check("missing key", conf_get("NO_SUCH_KEY", conf), "")
    check("missing file", conf_get("PLAN", os.path.join(td, "nope.conf")), "")
    check("comment lines never match", conf_get("# comment", conf), "")

    # ---- 2. status-footer LIVE_LABEL via helper only ---------------------
    os.environ["DOGANY_AGENT_CONF"] = conf
    os.environ.pop("DOGANY_LIVE_LABEL", None)
    sf = load_script("status-footer.py", "status_footer_dgn916")
    check("status-footer LIVE_LABEL no quotes", sf.LIVE_LABEL, LIVE_LABEL_KO)
    check("status-footer _conf_get delegates", sf._conf_get("KIT"), "pending")
    os.environ.pop("DOGANY_AGENT_CONF", None)

    # ---- 3a. usage-gate read_plan ----------------------------------------
    ws = os.path.join(td, "ws")
    os.makedirs(os.path.join(ws, "config"))
    for body, want, tag in (
        ("PLAN=max_20x\n", "max_20x", "unquoted"),
        ('PLAN="max_20x"\n', "max_20x", "quoted"),
        ("PLAN=bogus\n", "pro", "unknown slug"),
    ):
        with open(os.path.join(ws, "config", "agent.conf"), "w") as f:
            f.write(body)
        ug = load_script("usage-gate.py", "usage_gate_dgn916")
        check("read_plan %s" % tag, ug.read_plan(ws), want)
    # Missing key / missing file in candidate 1 falls through to candidate 2
    # (SCRIPT_DIR/../config/agent.conf) -- same chain as the original loop.
    # In a real instance checkout that file exists with a valid plan, so
    # accept either its plan or the default.
    with open(os.path.join(ws, "config", "agent.conf"), "w") as f:
        f.write("OTHER=1\n")
    ug = load_script("usage-gate.py", "usage_gate_dgn916b")
    check("read_plan missing key falls through",
          ug.read_plan(ws) in ug.PLAN_THRESHOLDS or ug.read_plan(ws) == "pro",
          True)
    check("read_plan missing file falls through",
          ug.read_plan(os.path.join(td, "nodir")) in ug.PLAN_THRESHOLDS
          or ug.read_plan(os.path.join(td, "nodir")) == "pro", True)

    # ---- 3b. onboarding-check readers ------------------------------------
    ob = load_script("onboarding-check.py", "onboarding_dgn916")
    check("resolve_kit quoted", ob.resolve_kit(conf), "pending")
    check("portfolio_pending unquoted", ob.portfolio_pending(conf), True)
    check("lifekit_pending", ob.lifekit_pending(conf), True)
    check("instance_class quoted+case", ob.instance_class(conf), "domain")
    check("resolve_kit missing file",
          ob.resolve_kit(os.path.join(td, "nope.conf")), "none")
    check("portfolio_pending missing file",
          ob.portfolio_pending(os.path.join(td, "nope.conf")), False)
    check("instance_class missing file",
          ob.instance_class(os.path.join(td, "nope.conf")), "main")
    check("resolve_lang lower", ob.resolve_lang({"cwd": td}), "en")
    ws2 = os.path.join(td, "ws2")
    os.makedirs(os.path.join(ws2, "config"))
    with open(os.path.join(ws2, "config", "agent.conf"), "w") as f:
        f.write('AGENT_LANG="KO"\n')
    check("resolve_lang quoted+lower", ob.resolve_lang({"cwd": ws2}), "ko")

    # ---- 3c. session-recap config ----------------------------------------
    sr = load_script("session-recap.py", "session_recap_dgn916")
    ws3 = os.path.join(td, "ws3")
    os.makedirs(os.path.join(ws3, "config"))
    with open(os.path.join(ws3, "config", "agent.conf"), "w") as f:
        f.write('RECAP_PAIRS="3"\nRECAP_CHAR_CAP=150\n')
    check("recap quoted int + unquoted int",
          sr._load_recap_config(ws3), (3, 150))
    with open(os.path.join(ws3, "config", "agent.conf"), "w") as f:
        f.write("RECAP_PAIRS=abc\n")
    check("recap non-integer falls back",
          sr._load_recap_config(ws3), (2, 200))
    check("recap missing file falls back",
          sr._load_recap_config(os.path.join(td, "nodir")), (2, 200))

    # ---- 3d. output-gate-stop readers -------------------------------------
    og = load_script("output-gate-stop.py", "output_gate_dgn916")
    ws4 = os.path.join(td, "ws4")
    os.makedirs(os.path.join(ws4, "config"))
    with open(os.path.join(ws4, "config", "agent.conf"), "w") as f:
        f.write('AGENT_ROLE="Dev"\nBOT_DATA_DIR="/tmp/botdata"\n')
    check("agent_role quoted+lower", og._read_agent_role(ws4), "dev")
    os.environ.pop("DOGANY_OUTPUT_GATE_LOG", None)
    check("bot_data_dir quoted",
          og._resolve_log_path(ws4).startswith("/tmp/botdata/logs/"), True)
    check("agent_role missing file",
          og._read_agent_role(os.path.join(td, "nodir")), "dev")

print()
if FAILURES:
    print("FAILED: %d -> %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("ALL PASS")
