#!/usr/bin/env python3
# test-lane: fast
"""DGN-1227 (owner decision dec-177): one model display rule, "<Name> <version>".

Locks: the id -> display table, the raw-id fallback for unknown ids, the TSV
rows agreeing with the parser, the shell notices (start = bare name, end =
observed version) and the status-footer workbench rows sharing that parser.

Run:  python3 routines/tests/test-dgn1227-model-display.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
LIB = os.path.join(ROUTINES, "lib")
TSV = os.path.join(LIB, "dispatch-model-display.tsv")
sys.path.insert(0, LIB)
import model_display as md  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "status_footer", os.path.join(ROUTINES, "status-footer.py"))
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)

_probe_scratch = tempfile.TemporaryDirectory(prefix="model-display-test-")
os.environ["MODEL_DISPLAY_CLAUDE_PROBE_DIR"] = os.path.join(_probe_scratch.name, "absent")

PASS = FAIL = 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS: %s" % desc)
    else:
        FAIL += 1
        print("  FAIL: %s %s" % (desc, detail))


print("== id -> display table ==")
TABLE = [
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
    ("claude-sonnet-5-20260101", "Claude Sonnet 5"),
    ("claude-3-5-sonnet-20241022", "Claude Sonnet 3.5"),
    ("claude-opus-5-5[1m]", "Claude Opus 5.5"),
    ("gpt-6-sol", "GPT 6 Sol"),
    ("gpt-6.1-sol", "GPT 6.1 Sol"),
    ("gpt-6-astra", "GPT 6 Astra"),
    ("gpt-6-luna", "GPT 6 Luna"),
    ("gpt-5.6-terra", "GPT 5.6 Terra"),
]
for mid, want in TABLE:
    got = md.parse_model_id(mid)
    check("%s -> %s" % (mid, want), got == want, "(got %r)" % got)
for mid in ["totally-new-model-x", "claude-opus", "gpt-6", "", "claude-opus-5-5-latest"]:
    check("unknown %r parses to None" % mid, md.parse_model_id(mid) is None)
    if mid:
        check("unknown %r displays as the raw id" % mid,
              md.model_display("claude-code", "opus", mid, TSV) == mid)
check("synthetic provider marker is absence, not an owner-facing model",
      md.model_display("claude-api", "sonnet", "<synthetic>", TSV)
      == "Claude Sonnet 5.5 (API)")
check("sentinel match is exact; similar unknown real ids stay raw",
      md.model_display("claude-api", "sonnet", "<synthetic>-real", TSV)
      == "<synthetic>-real (API)")

print("== observed input shapes ==")
check("JSON list -> first id wins",
      md.model_display("claude-code", "opus",
                       '["claude-opus-5-5","claude-sonnet-5"]', TSV) == "Claude Opus 5.5")
check("empty list -> bare name, no guessed version",
      md.model_display("claude-code", "opus", "[]", TSV) == "Claude Opus 5.5")
check("garbage json -> bare name",
      md.model_display("claude-code", "opus", "[oops", TSV) == "Claude Opus 5.5")

print("== TSV rows: before/after ==")
BARE = {"fable": "Claude Fable 5.2", "opus": "Claude Opus 5.5", "sonnet": "Claude Sonnet 5.5", "haiku": "Claude Haiku 5.5"}
CODEX = {"fable": "GPT 6 Astra", "opus": "GPT 6.1 Sol", "sonnet": "GPT 5.6 Terra", "haiku": "GPT 6 Luna"}
for alias, want in BARE.items():
    check("claude-code %s, nothing observed -> %s" % (alias, want),
          md.model_display("claude-code", alias, None, TSV) == want)
for alias, want in CODEX.items():
    slug = md.tsv_row("codex", alias, TSV)[0]
    check("codex %s (%s) -> %s" % (alias, slug, want),
          md.model_display("codex", alias, None, TSV) == want + "")
    check("codex %s TSV display == parser(slug): no drift" % alias,
          md.parse_model_id(slug) == want)
check("missing TSV row keeps the capitalized alias",
      md.model_display("claude-code", "mystery", None, TSV) == "Mystery")
with tempfile.TemporaryDirectory() as d:
    os.makedirs(os.path.join(d, "lib"))
    core = os.path.join(d, "lib", "dispatch-model-display.tsv")
    pack_dir = os.path.join(d, "bundle", "dispatch-model-display.d")
    os.makedirs(pack_dir)
    with open(core, "w") as fh:
        fh.write("# runtime\talias\tobserved\tdisplay\n")
    with open(os.path.join(pack_dir, "devkit.tsv"), "w") as fh:
        fh.write("pack-runtime\topus\t\tPack Model\n")
        fh.write("cloud\tsonnet\tclaude-sonnet-5-5\tClaude Sonnet 5.5 (Cloud)\n")
        fh.write("codex-cloud\topus\t\tCodex (Cloud)\n")
    check("installed pack display row is used",
          md.model_display("pack-runtime", "opus", None, core) == "Pack Model")
    check("Claude cloud overlay restores the full tier and place",
          md.model_display("cloud", "sonnet", None, core)
          == "Claude Sonnet 5.5 (Cloud)")
    check("Codex cloud overlay ignores a misleading Claude-family alias",
          md.model_display("codex-cloud", "opus", None, core) == "Codex (Cloud)")
    check("core rows win over an overlay conflict",
          md.model_display("pack-runtime", "opus", None, core) == "Pack Model")
    check("unknown runtime without a row names the runtime, not a model family",
          md.model_display("other-runtime", "opus", None, core) == "Other-runtime")

print("== stream observation ==")
with tempfile.TemporaryDirectory() as d:
    stream = os.path.join(d, "stream.jsonl")
    with open(stream, "w") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init",
                             "model": "claude-init-1"}) + "\n")
        fh.write("not json\n")
        fh.write(json.dumps({"type": "assistant",
                             "message": {"model": "claude-opus-5-5"}}) + "\n")
        fh.write(json.dumps({"type": "assistant",
                             "message": {"model": "claude-sonnet-5"}}) + "\n")
    check("assistant ids win, first-seen order",
          md.observe_from_stream(stream) == ["claude-opus-5-5", "claude-sonnet-5"])
    check("first_only stops at the first assistant id",
          md.observe_from_stream(stream, first_only=True) == ["claude-opus-5-5"])
    init_only = os.path.join(d, "init.jsonl")
    with open(init_only, "w") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init",
                             "model": "claude-fable-5-1"}) + "\n")
    check("system/init is the fallback",
          md.observe_from_stream(init_only) == ["claude-fable-5-1"])
    check("missing stream -> []", md.observe_from_stream(os.path.join(d, "nope")) == [])

print("== shell notices share the parser ==")
def shell_display(runtime, model, observed=""):
    script = ('DISPATCH_TEST=1 source "%s/dispatch-detached.sh"; '
              'RUNTIME=%s MODEL=%s; owner_model_display "$OBS"'
              % (ROUTINES, runtime, model))
    p = subprocess.run(["bash", "-c", script],
                       env=dict(os.environ, OBS=observed), capture_output=True, text=True, timeout=30)
    return p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""

check("START notice (nothing observed): bare name, no guessed version",
      shell_display("claude-code", "opus") == "Claude Opus 5.5")
check("END notice (observed id): Claude Opus 5.5",
      shell_display("claude-code", "opus", '["claude-opus-5-5"]') == "Claude Opus 5.5")
check("END notice (observed haiku snapshot id): Claude Haiku 4.5",
      shell_display("claude-code", "haiku", '["claude-haiku-4-5-20251001"]') == "Claude Haiku 4.5")
check("END notice unknown id: raw id",
      shell_display("claude-code", "sonnet", '["mystery-9"]') == "mystery-9")
check("END notice with empty observed list: bare name",
      shell_display("claude-code", "fable", "[]") == "Claude Fable 5.2")
check("codex notice: GPT 6 Astra",
      shell_display("codex", "fable") == "GPT 6 Astra")

print("== status-footer workbench rows share the parser ==")
with tempfile.TemporaryDirectory() as d:
    dispatch = os.path.join(d, "dispatch")
    run = os.path.join(dispatch, "dsp-20260924-000000-1")
    os.makedirs(run)
    with open(os.path.join(run, "run.env"), "w") as fh:
        fh.write("LEDGER_BRANCH=dsp/x1\n")
    ledger = os.path.join(d, "ledger.md")
    os.environ["DOGANY_DISPATCH_DIR"] = dispatch
    os.environ["DOGANY_LEDGER_FILE"] = ledger
    sf.LIVE_SHOW_MODEL = True
    installed_lib = os.path.join(d, "routines", "lib")
    installed_overlay = os.path.join(
        d, "routines", "bundle", "dispatch-model-display.d")
    os.makedirs(installed_lib)
    os.makedirs(installed_overlay)
    with open(TSV) as source, open(os.path.join(
            installed_lib, "dispatch-model-display.tsv"), "w") as target:
        target.write(source.read())
    with open(os.path.join(installed_overlay, "kit_dev.tsv"), "w") as fh:
        fh.write("cloud\tsonnet\tclaude-sonnet-5-5\tClaude Sonnet 5.5 (Cloud)\n")
        fh.write("codex-cloud\topus\t\tCodex (Cloud)\n")
    sf._ROOT_DIR = d

    def rows(branch, item, note):
        with open(ledger, "w") as fh:
            fh.write("| %s | %s | running | 1 | - | 2026-09-24 | - | %s |\n"
                     % (branch, item, note))
        return sf._collect_ledger_running()

    note = "dispatch 스폰 (model=opus runtime=claude-code axis=judgment)"
    check("no id reported yet -> bare name",
          rows("dsp/x1", "정리 작업", note) == ["Claude Opus 5.5: 정리 작업"])
    with open(os.path.join(run, "stream.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "assistant",
                             "message": {"model": "claude-opus-5-5"}}) + "\n")
    check("stream reported claude-opus-5-5 -> Claude Opus 5.5",
          rows("dsp/x1", "정리 작업", note) == ["Claude Opus 5.5: 정리 작업"])
    check("row with no matching rundir -> bare name, never guessed",
          rows("dsp/other", "다른 작업", note) == ["Claude Opus 5.5: 다른 작업"])
    sf.LIVE_SHOW_MODEL = False
    check("codex row -> GPT 6.1 Sol, even with the generic prefix opt-out",
          rows("dsp/x1", "코덱스", "dispatch 스폰 (model=opus runtime=codex axis=x)")
          == ["GPT 6.1 Sol: 코덱스"])
    sf.SIBLING_TERM = "동생"
    cases = [
        ("codex-cloud", "opus", "Codex (Cloud)"),
        ("cloud", "sonnet", "Claude Sonnet 5.5 (Cloud)"),
        ("codex", "opus", "GPT 6.1 Sol"),
        ("claude-code", "opus", "Claude Opus 5.5"),
        ("unknown-runtime", "opus", "Unknown-runtime"),
    ]
    for runtime, alias, want in cases:
        note = "dispatch 스폰 (model=%s runtime=%s axis=x)" % (alias, runtime)
        labels = rows("dsp/other", "작업", note)
        block = sf.LiveRow("작업", "ledger", model=labels[0].split(": ", 1)[0])
        line = block.job_lines()[1]
        check("board line for %s is owner-safe" % runtime,
              line == "• 동생: " + want and line.count(want) == 1, line)
print("== DGN-2116 job blocks retain dispatched models (DGN-1752) ==")
# The old label view above retained the model, but the two-line board view
# accidentally reapplied the generic subagent opt-out. Cover the actual
# job block for every dispatch runtime, with and without recorded starts.
epoch = sf.time.mktime((2026, 9, 24, 12, 0, 0, 0, 0, -1))
# Pin both views of the board language; the real shared renderer now receives
# BOARD_LANG, while the board's clock text reads _BOARD_STR. Host language
# settings must not turn this Korean fixture into a mixed-language board.
with patch.object(sf, "BOARD_LANG", "ko"), \
     patch.object(sf, "_BOARD_STR", sf.BOARD_STRINGS["ko"]), \
     patch.object(sf, "SIBLING_TERM", "HOST-TERM"), \
     patch.object(sf.time, "time", return_value=epoch):
    for show in (True, False):
        with patch.object(sf, "LIVE_SHOW_MODEL", show):
            for model in ("Claude Sonnet 5.5", "GPT 6 Sol",
                          "Claude Sonnet 5.5 (Cloud)",
                          "Claude Sonnet 5.5 (API)"):
                for start, suffix in ((epoch, " 12:00 (0분~)"), (None, "")):
                    row = sf.LiveRow("job", "ledger", model=model,
                                     job_start=start)
                    check("dispatch job model=%s show=%s start=%s" %
                          (model, show, start is not None),
                          row.job_lines() == ["job" + suffix, "• HOST-TERM: "
                                              + model], row.job_lines())
            row = sf.LiveRow("job", "subagent", model="Claude Sonnet 5.5",
                             job_start=epoch)
            check("transcript subagent retains its model opt-out: %s" % show,
                  row.job_lines() == ["job 12:00 (0분~)", "• HOST-TERM"
                                      + (": Claude Sonnet 5.5" if show else "")],
                  row.job_lines())

print("\n== DGN-1727: bounded wait for the observed id (start notice) ==")
import time  # noqa: E402
with tempfile.TemporaryDirectory() as tmp:
    live, closed, silent, dead = (os.path.join(tmp, n) for n in
                                  ("live", "closed", "silent", "dead"))
    for d in (live, closed, silent, dead):
        os.makedirs(d)
    for d in (live, silent):  # this test process stands in for a live worker
        with open(os.path.join(d, "pid"), "w") as fh:
            fh.write(str(os.getpid()))
    with open(os.path.join(live, "stream.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init",
                             "model": "claude-opus-5-5"}) + "\n")
    with open(os.path.join(closed, "status.json"), "w") as fh:
        json.dump({"model_observed": ["claude-sonnet-5"]}, fh)
    with open(os.path.join(dead, "pid"), "w") as fh:
        fh.write("999999999")
    t0 = time.time()
    got = md.wait_observed([live, closed, dead], 30, interval=0.1)
    check("observed / closed / dead-worker runs settle at once",
          time.time() - t0 < 5 and got == {live: "claude-opus-5-5",
                                           closed: "claude-sonnet-5", dead: ""},
          got)
    t0 = time.time()
    got = md.wait_observed([live, silent], 1.0, interval=0.2)
    check("a silent live run costs the bound, once, for the whole batch",
          0.8 <= time.time() - t0 < 3 and got == {live: "claude-opus-5-5",
                                                  silent: ""}, got)
    out = subprocess.run([sys.executable, os.path.join(LIB, "model_display.py"),
                          "--wait-observed", "1", live, silent],
                         capture_output=True, text=True, timeout=30).stdout
    check("CLI prints one '<dir><TAB><id>' line per run, empty when unseen",
          out.splitlines() == ["%s\tclaude-opus-5-5" % live, "%s\t" % silent],
          out)
    check("observed id renders versioned, none renders bare (rule kept)",
          md.model_display("claude-code", "opus", got[live]) == "Claude Opus 5.5"
          and md.model_display("claude-code", "opus", got[silent]) == "Claude Opus 5.5")

print("\nRESULT: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
