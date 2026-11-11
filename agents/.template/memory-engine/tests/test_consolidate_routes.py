# test-lane: fast
# test-component: memory-engine consolidation routing (contract_routing.py + cmd_consolidate)
# test-tier: 0
# test-prereq: tmp-files, subprocess
# test-new-component: contract_routing.py is a new component (write-side slot
#   classification, base_contract home resolution, provider read protocol,
#   reconciliation queue, intent outbox); the slice-1 router suite pins the
#   frozen lifekit payload builder and stays as is.
# test-covers: agents/.template/memory-engine/contract_routing.py agents/.template/memory-engine/memory.py docs/design/DGN-2028-v27-merged-design-20261015.md
# DGN-2028 / DGN-2010 slice 4 (dec-441 item 2): nightly consolidation classifies
# each kept item into a base slot and routes kit-contract facts to the kit that
# declares them (merged design section 6). Acceptance cases: no kit, one kit,
# both kits, fake third-party kit, crew same event from two members, destination
# change, unavailable provider, malformed classification; plus the flags-off
# byte-identical golden on the consolidate dry-run.
#
# Fully sandboxed: temp state.db, temp memories dir, temp instance root holding
# installed base_contract records and a fake third-party kit adapter (a local
# python script, the only subprocess). Model calls, embedding, index and push
# are stubbed. Nothing under a live instance is read or written.
#
# Regenerate the golden ONLY from a pre-slice-4 tree: ROUTES_GOLDEN_REGEN=1.
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "contract-routing")
GOLDEN = os.path.join(FIX, "consolidate-dry-run-off-golden.json")

ROWS = [
    ("2026-10-14T01:00:00.000000Z", "__USER_LABEL__", "cpu was at 90 percent, mostly tests. run tests at night from now on", "s1"),
    ("2026-10-14T01:01:00.000000Z", "__AGENT_LABEL__", "noted, I will move the test runs", "s1"),
    ("2026-10-14T01:02:00.000000Z", "__USER_LABEL__", "I paid 4500 won for coffee by card today", "s1"),
    ("2026-10-14T01:03:00.000000Z", "__USER_LABEL__", "I prefer short replies", "s1"),
]
EXTRACTED = [
    '[U] tests run at night from now on || "run tests at night from now on"',
    '[A] cpu load was mostly tests (agent reading)',
    '[U] paid 4500 won for coffee by card || "I paid 4500 won for coffee by card today"',
    '[U] prefers short replies || "I prefer short replies"',
]


CAND_HEAD = "\ud6c4\ubcf4 \ubaa9\ub85d:\n"   # the filter prompt's candidate list head


class Recorder:
    def __init__(self):
        self.compress_prompts = []
        self.filter_prompts = []


def _norm_today(s):
    return re.sub(r"\d{4}-\d{2}-\d{2}(?=, by=)", "<TODAY>", s)


def sandbox_engine(tmp_path, monkeypatch, rec, filter_reply=None, extracted=None):
    """Point memory at temp state and stub every model / embedding / push."""
    mem_dir = tmp_path / "memories"
    raw_dir = mem_dir / "_raw"
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "MEMORIES_DIR", str(mem_dir))
    monkeypatch.setattr(memory, "TARGET_MEMORY_MD", str(mem_dir / "inbox.md"))
    monkeypatch.setattr(memory, "RAW_ARCHIVE_DIR", str(raw_dir))
    monkeypatch.setattr(memory, "CONSOLIDATE_DEADLETTER_PATH",
                        str(raw_dir / "consolidate-dead-letter.jsonl"))
    monkeypatch.setattr(memory, "CONSOLIDATE_LAST_SUCCESS", str(tmp_path / ".last-success"))
    monkeypatch.setattr(memory, "CONSOLIDATE_DEFER_MARKER", str(tmp_path / ".defer.json"))
    monkeypatch.setattr(memory, "CONSOLIDATE_BUDGET_BOOST_FILE",
                        str(tmp_path / ".budget-boost.json"), raising=False)
    monkeypatch.delenv("DOGANY_USAGE_DEFER_MARKER", raising=False)
    monkeypatch.delenv("DOGANY_BACKLOG_MARKER", raising=False)
    monkeypatch.setattr(memory, "_query_usage_state", lambda: {
        "five_hour_util": 10.0, "seven_day_util": 10.0,
        "five_hour_resets_at": "", "seven_day_resets_at": ""})
    monkeypatch.setattr(memory, "embed", _no_ollama)
    monkeypatch.setattr(memory, "cmd_index", lambda a: None)
    monkeypatch.setattr(memory, "_reindex_locked_with_retry", lambda *a, **k: True)
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: False)
    monkeypatch.setattr(memory, "archive_raw_transcript", lambda wm, now, until_iso=None: 0)
    monkeypatch.setattr(memory, "_owner_words", lambda text: text)
    monkeypatch.setattr(memory, "_iter_transcript_rows",
                        lambda wm, now, raw=False: [r for r in ROWS if not wm or r[0] > wm])
    items = list(EXTRACTED if extracted is None else extracted)

    def fake_compress(text, prompt_prefix=None, model=None):
        rec.compress_prompts.append(prompt_prefix)
        return list(items)

    monkeypatch.setattr(memory, "compress_with_haiku", fake_compress)
    real_run = subprocess.run

    def fake_run(argv, *a, **k):
        if argv[:2] == ["claude", "-p"]:
            prompt = argv[-1]
            rec.filter_prompts.append(prompt)
            n = len(re.findall(r"^\d+\. ", prompt.split(CAND_HEAD)[-1], re.M))
            out = filter_reply(prompt, n) if filter_reply else "\n".join(
                "%d KEEP" % i for i in range(1, n + 1))
            return subprocess.CompletedProcess(argv, 0, out, "")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(memory.subprocess, "run", fake_run)
    return tmp_path


def _no_ollama(text):
    raise OSError("no ollama in test sandbox")


def run_consolidate(dry_run=True):
    args = argparse.Namespace(dry_run=dry_run, no_push=True, since_days=None, budget_secs=0)
    buf = io.StringIO()
    errbuf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(errbuf):
        rc = memory.cmd_consolidate(args)
    out = buf.getvalue()
    sys.stdout.write(out)
    sys.stderr.write(errbuf.getvalue())
    return rc, out


def _snapshot(tmp_path, rec, rc, out):
    files = sorted(os.path.relpath(os.path.join(d, f), str(tmp_path))
                   for d, _, fs in os.walk(str(tmp_path)) for f in fs)
    tables = []
    if os.path.exists(str(tmp_path / "state.db")):
        c = sqlite3.connect(str(tmp_path / "state.db"))
        tables = sorted(r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        c.close()
    return {"rc": rc, "stdout": _norm_today(out),
            "compress_prompts": rec.compress_prompts,
            "filter_prompts": rec.filter_prompts,
            "files": files, "tables": tables}


# ---------------------------------------------------------------------------
# flags-off golden: consolidation output, prompts and side effects unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, "off", "0"])
def test_routes_off_dry_run_is_byte_identical_golden(tmp_path, monkeypatch, flag):
    monkeypatch.delenv("DOGANY_CONSOLIDATE_ROUTES", raising=False)
    conf = tmp_path / "agent.conf"
    conf.write_text("" if flag is None else "CONSOLIDATE_ROUTES=%s\n" % flag)
    monkeypatch.setenv("DOGANY_AGENT_CONF", str(conf))
    rec = Recorder()
    sb = tmp_path / "sb"
    sb.mkdir()
    sandbox_engine(sb, monkeypatch, rec)
    rc, out = run_consolidate(dry_run=True)
    snap = _snapshot(sb, rec, rc, out)
    if os.environ.get("ROUTES_GOLDEN_REGEN") == "1":
        with open(GOLDEN, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=1, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
    with open(GOLDEN, encoding="utf-8") as fh:
        golden = json.load(fh)
    assert snap == golden


# ---------------------------------------------------------------------------
# flags-on harness: a temp instance root with installed base_contract records
# ---------------------------------------------------------------------------

TEMPLATE = os.path.normpath(os.path.join(ENGINE_DIR, ".."))
sys.path.insert(0, ENGINE_DIR)
import contract_routing as cr  # noqa: E402

TESTS_AT = "tests run at night"
CPU = "cpu load was mostly tests"
PAID = "paid 4500 won"
PREF = "prefers short replies"
CLS_KITS = {
    TESTS_AT: "slot=decision target=dev-ops time=now about=host",
    CPU: "slot=explanation target=- time=past about=host",
    PAID: "slot=resource target=spend time=past about=owner",
    PREF: "slot=preference target=- time=now about=owner",
}
CLS_NOKIT = dict(CLS_KITS, **{
    TESTS_AT: "slot=decision target=- time=now about=host",
    PAID: "slot=resource target=- time=past about=owner"})


def make_root(tmp_path):
    root = tmp_path / "inst"
    (root / "routines" / "lib").mkdir(parents=True)
    shutil.copy(os.path.join(TEMPLATE, "routines", "lib", "base_contract.py"),
                str(root / "routines" / "lib" / "base_contract.py"))
    (root / "config" / "packs").mkdir(parents=True)
    return root


def install_kit(root, pack, targets, bindings, records=None, ops=("lookup",), write=False):
    """Install one fake kit: adapter copy + records.json + a recorded contract
    (through base_contract.record, the install path's own validator)."""
    kdir = root / "kits" / pack
    kdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(os.path.join(FIX, "fake_kit_adapter.py"), str(kdir / "adapter.py"))
    (kdir / "records.json").write_text(json.dumps(records or {"mode": "ok", "records": []}))
    prov = {"adapter": "kits/%s/adapter.py" % pack, "operations": list(ops), "protocol": 1}
    if write:
        for k in ("write_door", "capability_probe", "receipt_lookup"):
            prov[k] = "kits/%s/adapter.py" % pack
    contract = {"version": 1, "targets": [{"id": t} for t in targets],
                "bindings": bindings, "providers": {"p": prov}}
    man = root / ("%s.manifest.json" % pack)
    man.write_text(json.dumps({"base_contract": contract}))
    bc = cr.base_contract(str(root))
    assert bc.record(str(root), pack, str(man)) == 0
    return kdir


def set_records(kdir, records):
    (kdir / "records.json").write_text(json.dumps(records))


def calls(kdir):
    p = kdir / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def bind(slot, target, mode="reference", **kw):
    b = {"slot": slot, "target": target, "mode": mode}
    if mode != "none":
        b.update({"ownership": "kit", "provider": "p"})
    b.update(kw)
    return b


def classifier(table):
    def reply(prompt, n):
        lines = []
        body = prompt.split(CAND_HEAD)[-1]
        for m in re.finditer(r"^(\d+)\. (.*)$", body, re.M):
            tail = next((v for k, v in table.items() if k in m.group(2)), None)
            lines.append("%s KEEP %s" % (m.group(1), tail) if tail is not None
                         else "%s KEEP" % m.group(1))
        return "\n".join(lines)
    return reply


def routes_on(tmp_path, monkeypatch, root, table, instance="m1", extracted=None, sb_name="sb"):
    monkeypatch.setenv("DOGANY_CONSOLIDATE_ROUTES", "1")
    monkeypatch.setenv("DOGANY_ROUTES_ROOT", str(root))
    monkeypatch.setenv("DOGANY_INSTANCE_ID", instance)
    rec = Recorder()
    sb = tmp_path / sb_name
    sb.mkdir(exist_ok=True)
    sandbox_engine(sb, monkeypatch, rec, filter_reply=classifier(table), extracted=extracted)
    return sb, rec


def read(p):
    return p.read_text() if p.exists() else ""


def outbox(sb):
    c = sqlite3.connect(str(sb / "state.db"))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM routing_outbox ORDER BY created_at")]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def recon_rows(sb):
    c = sqlite3.connect(str(sb / "state.db"))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM reconciliation_queue")]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def watermark(sb):
    c = sqlite3.connect(str(sb / "state.db"))
    row = c.execute("SELECT value FROM consolidation_state WHERE key='last_ts'").fetchone()
    c.close()
    return row[0] if row else None


@pytest.fixture(autouse=True)
def _routes_ctx_reset():
    yield
    memory._ROUTES[:] = []


# ---------------------------------------------------------------------------
# acceptance: no kit
# ---------------------------------------------------------------------------

def test_no_kit_every_item_lands_in_base_memory_slot_tagged(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    sb, rec = routes_on(tmp_path, monkeypatch, root, CLS_NOKIT)
    rc, out = run_consolidate(dry_run=False)
    assert rc == 0
    inbox = read(sb / "memories" / "inbox.md")
    for key, slot in ((TESTS_AT, "decision"), (CPU, "explanation"), (PAID, "resource"),
                      (PREF, "preference")):
        line = next(ln for ln in inbox.splitlines() if key in ln)
        assert "slot=%s" % slot in line and "ev=ev-" in line, line
        assert re.search(r"\(\d{4}-\d{2}-\d{2},[^()]*\)\s*$", line), "one tag group stays"
    assert outbox(sb) == [] and recon_rows(sb) == []
    assert not (sb / "memories" / cr.PENDING_MD).exists()
    assert watermark(sb) == ROWS[-1][0]
    # no extra model call: one extraction call, one filter batch call
    assert len(rec.compress_prompts) == 1 and len(rec.filter_prompts) == 1
    assert cr.EXTRACT_ADDENDUM in rec.compress_prompts[0]
    assert "Routing mode (CONSOLIDATE_ROUTES)" in rec.filter_prompts[0]
    assert "(none installed)" in rec.filter_prompts[0]


def test_reserved_and_why_slots_stay_base(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    table = {TESTS_AT: "slot=why target=spend time=now about=owner",
             CPU: "slot=state target=spend time=now about=owner",
             PAID: "slot=preference target=spend time=now about=owner",
             PREF: "slot=context target=- time=now about=owner"}
    sb, _ = routes_on(tmp_path, monkeypatch, root, table)
    rc, _out = run_consolidate(dry_run=False)
    assert rc == 0
    inbox = read(sb / "memories" / "inbox.md")
    assert "slot=why" in inbox and "slot=state" in inbox and "slot=preference" in inbox
    assert outbox(sb) == []
    assert calls(root / "kits" / "acme") == [], "no provider call for base-homed slots"


@pytest.mark.parametrize("about", ["host", "agent", "product"])
def test_runtime_state_never_reaches_memory_or_index(tmp_path, monkeypatch, about):
    root = make_root(tmp_path)
    text = "Set buttons are disabled in the current runtime"
    source = '[U] %s || "Set buttons are disabled"' % text
    table = {text: "slot=state target=- time=now about=" + about}
    real_index = memory.cmd_index
    sb, _ = routes_on(tmp_path, monkeypatch, root, table, extracted=[source])
    (sb / "memories").mkdir()
    (sb / "memories" / "inbox.md").write_text("<!-- dogany-format: section -->\n")
    assert run_consolidate(dry_run=False)[0] == 0
    row = outbox(sb)[0]
    assert (row["destination"], row["state"], row["reason"]) == (
        "runtime", "no_consolidate", "transient_runtime")
    assert row["expires_at"] and row["verification"] == "verify_current_runtime_before_use"
    assert text not in read(sb / "memories" / "inbox.md")
    assert not (sb / "memories" / cr.PENDING_MD).exists()
    assert real_index(argparse.Namespace()) == 0
    with sqlite3.connect(str(sb / "state.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    assert watermark(sb) == ROWS[-1][0]


def test_runtime_disposition_retry_keeps_original_expiry(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    item = {"event_id": "proof-transient-1", "text": "Set buttons are disabled",
            "subject": cr.SUBJECT_OWNER, "quote": "Set buttons are disabled",
            "cls": {"slot": "state", "target": "runtime", "time": "now", "about": "host"}}
    plan = cr.plan_item(item, [], None)
    cr.persist(conn, [plan], "first", "2026-10-21T00:00:00Z")
    expires = plan["expires_at"]
    plan["expires_at"] = "2099-01-01T00:00:00Z"
    cr.persist(conn, [plan], "retry", "2026-10-22T00:00:00Z")
    assert conn.execute("SELECT expires_at FROM routing_outbox").fetchone()[0] == expires
    conn.close()


def test_nightly_correction_supersedes_before_reindex(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    text = "Correction: Momo is a cat"
    real_index = memory.cmd_index
    sb, _ = routes_on(tmp_path, monkeypatch, root,
                      {text: "slot=context target=- time=now about=owner"},
                      extracted=['[U] %s || "%s"' % (text, text)])
    monkeypatch.setattr(memory, "_iter_transcript_rows", lambda *a, **k: [
        (ROWS[0][0], "__USER_LABEL__", text, "s1")])
    monkeypatch.setattr(memory, "_supersession_entity_terms", lambda texts: [])
    monkeypatch.setattr(memory, "_reindex_locked_with_retry",
                        lambda: real_index(argparse.Namespace()) == 0)
    (sb / "memories").mkdir()
    inbox = sb / "memories" / "inbox.md"
    inbox.write_text("<!-- dogany-format: section -->\n\u00a7\n"
                     "Momo is a dog (2026-10-14, nightly-consolidate)\n")
    assert run_consolidate(dry_run=False)[0] == 0
    assert "[SUPERSEDED] Momo is a dog" in inbox.read_text()
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", str(sb / "none" / "*.jsonl"))
    monkeypatch.setattr(memory, "_ledger_log", lambda line: None)
    recalled = memory.search_core("Momo", k=5, log=False)
    assert recalled and all("dog" not in hit["text"] for hit in recalled)
    assert any("cat" in hit["text"] for hit in recalled)


# ---------------------------------------------------------------------------
# acceptance: one kit (a fake third-party kit)
# ---------------------------------------------------------------------------

def test_fake_third_party_kit_absent_is_a_declared_handoff(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    kdir = install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    sb, rec = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    rc, out = run_consolidate(dry_run=False)
    assert rc == 0
    inbox = read(sb / "memories" / "inbox.md")
    pend = read(sb / "memories" / cr.PENDING_MD)
    assert PAID not in inbox, "a kit-homed fact is not stored as memory"
    assert PAID in pend and "route=handoff:absent" in pend, "never a silent disappearance"
    rows = outbox(sb)
    assert [(r["destination"], r["state"], r["slot"], r["target_id"]) for r in rows] == [
        ("kit:acme", "handoff", "resource", "spend")]
    assert rows[0]["intent_id"].startswith("int-") and rows[0]["source_event_id"].startswith("ev-")
    # read-only protocol, argv form, owner span passed through
    cs = calls(kdir)
    assert [c["op"] for c in cs] == ["lookup"]
    assert cs[0]["req"]["event_id"] == rows[0]["source_event_id"]
    assert cs[0]["req"]["delivery_key"] == rows[0]["delivery_key"]
    assert "spend" in rec.filter_prompts[0]
    assert watermark(sb) == ROWS[-1][0]


def test_equivalent_record_is_referenced_not_stored(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")], records={
        "mode": "ok", "records": [{"slot": "resource", "target": "spend", "match": "4500",
                                   "result": "equivalent", "refs": ["blk-7"],
                                   "versions": {"blk-7": "3"}}]})
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    assert run_consolidate(dry_run=False)[0] == 0
    assert PAID not in read(sb / "memories" / "inbox.md")
    assert PAID not in read(sb / "memories" / cr.PENDING_MD)
    r = outbox(sb)[0]
    assert (r["state"], json.loads(r["refs"])) == ("referenced", ["blk-7"])


def test_kit_conflict_opens_one_reconciliation_row(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")], records={
        "mode": "ok", "records": [{"slot": "resource", "target": "spend", "match": "4500",
                                   "result": "conflict", "refs": ["blk-7"],
                                   "versions": {"blk-7": "3"}, "subject": "coffee-1014",
                                   "evidence": ["blk-7 says 5000"]}]})
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    assert run_consolidate(dry_run=False)[0] == 0
    rq = recon_rows(sb)
    assert len(rq) == 1
    q = rq[0]
    assert (q["kind"], q["state"], q["subject_ref"], q["target_id"]) == (
        "kit", "open", "coffee-1014", "spend")
    assert json.loads(q["old_refs"]) == ["blk-7"]
    ob = outbox(sb)[0]
    assert (ob["state"], ob["recon_id"]) == ("conflict", q["id"])
    assert json.loads(q["new_refs"]) == [ob["intent_id"]]
    assert PAID in read(sb / "memories" / cr.PENDING_MD), "conflict stays retrievable"


def test_write_binding_waits_for_the_held_door_and_runs_nothing(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    kdir = install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend", mode="write")],
                       write=True)
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    assert run_consolidate(dry_run=False)[0] == 0
    assert outbox(sb)[0]["state"] == "door_pending"
    assert [c["op"] for c in calls(kdir)] == ["lookup"], "no write door in slice 4"


def test_decision_needs_an_owner_quote_and_unverified_never_routes(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    kdir = install_kit(root, "forge", ["dev-ops"], [bind("decision", "dev-ops"),
                                                   bind("explanation", "dev-ops")])
    table = {TESTS_AT: "slot=decision target=dev-ops time=now about=host",
             CPU: "slot=decision target=dev-ops time=past about=host",
             PAID: "slot=explanation target=dev-ops time=past about=host",
             PREF: "slot=context target=- time=now about=owner"}
    extracted = list(EXTRACTED)
    extracted[2] = "paid 4500 won for coffee by card"     # no mark -> unverified
    sb, _ = routes_on(tmp_path, monkeypatch, root, table, extracted=extracted)
    assert run_consolidate(dry_run=False)[0] == 0
    st = {r["text"].split()[0]: (r["state"], r["reason"]) for r in outbox(sb)}
    assert st["tests"] == ("handoff", "absent")
    assert st["cpu"] == ("hold", "decision_not_owner_quoted")
    assert st["paid"] == ("hold", "unverified_attribution")
    assert len(calls(kdir)) == 1, "held items never reach the provider"


# ---------------------------------------------------------------------------
# acceptance: both kits, overlap, `none`
# ---------------------------------------------------------------------------

def test_both_kits_route_each_item_to_its_own_home(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    life = install_kit(root, "lifekit", ["spend"], [bind("resource", "spend")])
    dev = install_kit(root, "devkit", ["dev-ops"], [bind("decision", "dev-ops")])
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    assert run_consolidate(dry_run=False)[0] == 0
    dest = {r["text"].split()[0]: r["destination"] for r in outbox(sb)}
    assert dest == {"tests": "kit:devkit", "paid": "kit:lifekit"}
    assert len(calls(life)) == 1 and len(calls(dev)) == 1
    inbox = read(sb / "memories" / "inbox.md")
    assert CPU in inbox and PREF in inbox and PAID not in inbox and TESTS_AT not in inbox


def test_two_homes_for_one_slot_target_is_an_agent_owned_hold(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    a = install_kit(root, "akit", ["spend", "dev-ops"], [bind("resource", "spend")])
    b = install_kit(root, "bkit", ["spend", "dev-ops"], [bind("resource", "spend")])
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    assert run_consolidate(dry_run=False)[0] == 0
    r = next(x for x in outbox(sb) if PAID in x["text"])
    assert (r["destination"], r["state"], r["reason"]) == ("hold", "hold", "contract_overlap")
    assert calls(a) == [] and calls(b) == [], "no alphabetical winner, no lookup"
    assert recon_rows(sb) == [], "never an owner routing question"


def test_none_binding_withdraws_a_claim_before_counting():
    contracts = [("akit", {"version": 1, "targets": [{"id": "spend"}],
                           "bindings": [{"slot": "resource", "target": "spend", "mode": "none"}],
                           "providers": {}}),
                 ("bkit", {"version": 1, "targets": [{"id": "spend"}],
                           "bindings": [bind("resource", "spend")],
                           "providers": {"p": {"adapter": "x.py", "operations": ["lookup"],
                                               "protocol": 1}}})]
    h = cr.resolve_home("resource", "spend", contracts)
    assert (h["home"], h["pack"]) == ("kit", "bkit")
    assert cr.resolve_home("resource", None, contracts)["home"] == "base"
    assert cr.resolve_home("goal", "spend", contracts)["home"] == "base"


# ---------------------------------------------------------------------------
# acceptance: crew -- the same event from two members; identical payments
# ---------------------------------------------------------------------------

def test_crew_same_event_from_two_members_is_one_fact(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    kdir = install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    sb1, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS, instance="crew-a", sb_name="a")
    assert run_consolidate(dry_run=False)[0] == 0
    a = next(r for r in outbox(sb1) if PAID in r["text"])
    # the shared kit took member A's handoff and now holds the event
    set_records(kdir, {"mode": "ok", "records": [{
        "slot": "resource", "target": "spend", "event_id": a["source_event_id"],
        "result": "equivalent", "refs": ["blk-9"], "versions": {"blk-9": "1"}}]})
    sb2, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS, instance="crew-b", sb_name="b")
    assert run_consolidate(dry_run=False)[0] == 0
    b = next(r for r in outbox(sb2) if PAID in r["text"])
    assert b["intent_id"] == a["intent_id"] and b["source_event_id"] == a["source_event_id"]
    assert b["state"] == "referenced", "member B links to the one authoritative fact"
    assert PAID not in read(sb2 / "memories" / cr.PENDING_MD)
    # a shared outbox keeps one intent with both observers
    conn = sqlite3.connect(":memory:")
    p = dict(a, event_id=a["source_event_id"], target=a["target_id"], refs=[], recon=None,
             binding_version=a["binding_version"])
    cr.persist(conn, [p], "crew-a", "t1")
    cr.persist(conn, [p], "crew-b", "t2")
    rows = conn.execute("SELECT observers FROM routing_outbox").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0]) == ["crew-a", "crew-b"]


def test_two_real_identical_payments_stay_distinct_events():
    e1 = cr.owner_event_id("coffee 4500", "2026-10-14T01:00:00Z", 1)
    e2 = cr.owner_event_id("coffee 4500", "2026-10-14T05:00:00Z", 2)
    e1_crew = cr.owner_event_id("coffee  4500", "2026-10-14T01:00:40Z", 1)
    assert e1 != e2 and e1 == e1_crew
    ctx = {"cr": cr, "instance": "m1", "rows": [
        ("2026-10-14T01:00:00Z", "__USER_LABEL__", "coffee 4500", "s"),
        ("2026-10-14T05:00:00Z", "__USER_LABEL__", "coffee 4500", "s")]}
    memory._ITEM_SUBJECTS.clear()
    memory._ITEM_SUBJECTS["paid coffee"] = (memory.SUBJECT_OWNER, "coffee 4500")
    try:
        assert memory._routes_event_id(ctx, "paid coffee") == e1
    finally:
        memory._ITEM_SUBJECTS.clear()
    assert cr.item_ids(e1, "x", "resource", "spend") != cr.item_ids(e2, "x", "resource", "spend")


# ---------------------------------------------------------------------------
# acceptance: destination change migrates one intent
# ---------------------------------------------------------------------------

def test_destination_change_migrates_the_intent(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    install_kit(root, "akit", ["spend", "dev-ops"], [bind("resource", "spend")])
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    assert run_consolidate(dry_run=False)[0] == 0
    first = next(r for r in outbox(sb) if PAID in r["text"])
    # the owner swaps kits: akit's record goes, bkit now declares the home
    os.remove(str(root / "config" / "packs" / "akit.base_contract.json"))
    install_kit(root, "bkit", ["spend", "dev-ops"], [bind("resource", "spend")])
    c = sqlite3.connect(str(sb / "state.db"))
    c.execute("DELETE FROM consolidation_state WHERE key='last_ts'")   # replay the span
    c.commit()
    c.close()
    assert run_consolidate(dry_run=False)[0] == 0
    rows = [r for r in outbox(sb) if PAID in r["text"]]
    assert len(rows) == 1, "never two active deliveries"
    r = rows[0]
    assert r["intent_id"] == first["intent_id"] and r["destination"] == "kit:bkit"
    assert r["delivery_key"] != first["delivery_key"]
    hist = json.loads(r["history"])
    assert [h["from"] for h in hist] == ["kit:akit"]
    assert read(sb / "memories" / cr.PENDING_MD).count(r["intent_id"]) == 1


# ---------------------------------------------------------------------------
# acceptance: unavailable provider is unknown, never "no record"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,reason", [
    ("fail", "provider_unavailable:rc=3"),
    ("garbage", "provider_reply_not_json"),
    ("badresult", "provider_bad_result"),
])
def test_unavailable_provider_holds(tmp_path, monkeypatch, mode, reason):
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"],
                [bind("resource", "spend"), bind("decision", "dev-ops")],
                records={"mode": mode})
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    assert run_consolidate(dry_run=False)[0] == 0
    rows = outbox(sb)
    assert {(r["state"], r["reason"]) for r in rows} == {("hold", reason)}
    assert all(r["state"] != "handoff" for r in rows)
    pend = read(sb / "memories" / cr.PENDING_MD)
    assert PAID in pend and TESTS_AT in pend


def test_dead_adapter_costs_one_call_and_missing_adapter_holds(tmp_path):
    root = make_root(tmp_path)
    kdir = install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")],
                       records={"mode": "fail"})
    contracts = cr.installed_contracts(str(root))
    client = cr.ProviderClient(str(root), contracts)
    for _ in range(3):
        assert client.lookup("acme", "p", {"slot": "resource"})["result"] == "unknown"
    assert len(calls(kdir)) == 1
    os.remove(str(kdir / "adapter.py"))
    client2 = cr.ProviderClient(str(root), contracts)
    lk = client2.lookup("acme", "p", {})
    assert (lk["result"], lk["why"]) == ("unknown", "adapter_missing")


# ---------------------------------------------------------------------------
# acceptance: malformed classification -> evidence hold
# ---------------------------------------------------------------------------

def test_malformed_classification_is_an_evidence_hold(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    kdir = install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    table = {TESTS_AT: "slot=resource",                                   # keys missing
             CPU: "because it matters",                                   # free text
             PAID: "slot=resource target=nowhere time=past about=owner",  # unknown target
             PREF: "slot=wish target=- time=now about=owner"}             # unknown slot
    sb, _ = routes_on(tmp_path, monkeypatch, root, table)
    assert run_consolidate(dry_run=False)[0] == 0
    st = {r["text"].split()[0]: (r["destination"], r["state"], r["reason"]) for r in outbox(sb)}
    assert st == {"tests": ("hold", "hold", "malformed:keys"),
                  "cpu": ("hold", "hold", "malformed:token"),
                  "paid": ("hold", "hold", "malformed:target"),
                  "prefers": ("hold", "hold", "malformed:slot")}
    assert calls(kdir) == [], "a malformed classification never authorizes a kit call"
    assert read(sb / "memories" / "inbox.md") == ""
    pend = read(sb / "memories" / cr.PENDING_MD)
    assert all(k in pend for k in (TESTS_AT, CPU, PAID, PREF))


def test_filter_failure_keeps_items_as_unclassified_holds(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    real = memory.subprocess.run

    def boom(argv, *a, **k):
        if argv[:2] == ["claude", "-p"]:
            raise subprocess.TimeoutExpired(argv, 180)
        return real(argv, *a, **k)

    monkeypatch.setattr(memory.subprocess, "run", boom)
    assert run_consolidate(dry_run=False)[0] == 0
    assert {r["reason"] for r in outbox(sb)} == {"unclassified:filter_failed"}


@pytest.mark.parametrize("tail,why", [
    ("", "malformed:empty"),
    ("slot=goal target=- time=now about=owner about=owner", "malformed:duplicate-about"),
    ("slot=goal target=- time=soon about=owner", "malformed:time"),
    ("slot=goal target=- time=now about=cat", "malformed:about"),
    ("slot=goal target=- time=now about=owner extra=1", "malformed:keys"),
])
def test_keep_parser_validates_structure(tail, why):
    slots = cr.slot_names(TEMPLATE)
    assert cr.parse_classification(tail, slots, {"spend"}) == (None, why)
    ok, err = cr.parse_classification("slot=goal target=spend time=prediction about=owner",
                                      slots, {"spend"})
    assert err is None and ok == {"slot": "goal", "target": "spend", "time": "prediction",
                                  "about": "owner"}


# ---------------------------------------------------------------------------
# dry-run boundary and watermark durability
# ---------------------------------------------------------------------------

def test_dry_run_on_plans_but_writes_nothing(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)
    rc, out = run_consolidate(dry_run=True)
    assert rc == 0
    assert "== routed (1) ==" in out and "kit:acme handoff:absent" in out
    assert not (sb / "memories").exists()
    c = sqlite3.connect(str(sb / "state.db"))
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert not names & {"routing_outbox", "reconciliation_queue"}
    assert watermark(sb) is None


def test_watermark_waits_until_every_item_is_durable(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    sb, _ = routes_on(tmp_path, monkeypatch, root, CLS_KITS)

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cr, "persist", broken)
    rc, _ = run_consolidate(dry_run=False)
    assert rc == 1
    assert watermark(sb) is None
    assert read(sb / "memories" / "inbox.md") == "", "memory untouched before intents are durable"
    monkeypatch.setattr(cr, "persist", persist_real)
    assert run_consolidate(dry_run=False)[0] == 0
    assert watermark(sb) == ROWS[-1][0]
    assert len(outbox(sb)) == 1


persist_real = cr.persist


def test_flags_off_writes_no_routing_state(tmp_path, monkeypatch):
    monkeypatch.delenv("DOGANY_CONSOLIDATE_ROUTES", raising=False)
    conf = tmp_path / "agent.conf"
    conf.write_text("")
    monkeypatch.setenv("DOGANY_AGENT_CONF", str(conf))
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    monkeypatch.setenv("DOGANY_ROUTES_ROOT", str(root))
    rec = Recorder()
    sb = tmp_path / "sb"
    sb.mkdir()
    sandbox_engine(sb, monkeypatch, rec)
    assert run_consolidate(dry_run=False)[0] == 0
    assert outbox(sb) == [] and recon_rows(sb) == []
    assert not (sb / "memories" / cr.PENDING_MD).exists()
    assert "slot=" not in read(sb / "memories" / "inbox.md")
    assert calls(root / "kits" / "acme") == []


# ---------------------------------------------------------------------------
# one reconciliation queue: owner-turn-checked, version-checked, never aged
# ---------------------------------------------------------------------------

def test_reconciliation_queue_lifecycle():
    conn = sqlite3.connect(":memory:")
    cr.init_tables(conn)
    rid = cr.recon_enqueue(conn, "coffee-1014", "spend", ["blk-7"], ["int-x"], "kit",
                           ["blk-7 says 5000"], {"blk-7": "3"}, "2026-10-14T00:00:00Z")
    again = cr.recon_enqueue(conn, "coffee-1014", "spend", ["blk-7"], ["int-x"], "kit",
                             [], {"blk-7": "3"}, "2026-10-21T00:00:00Z")
    assert again == rid, "a replay a week later neither duplicates nor reopens"
    assert [r["state"] for r in cr.recon_pending(conn)] == ["open"]
    assert cr.recon_mark_asked(conn, rid, "ask-1", "t")
    assert not cr.recon_mark_asked(conn, rid, "ask-2", "t"), "never silently re-asked"
    assert cr.recon_defer(conn, rid, "t")
    # visible to the owning agent even off-topic
    assert [r["id"] for r in cr.recon_pending(conn)] == [rid]
    assert cr.recon_pending(conn, target_id="other") == []
    yes = lambda ref: ref == "otl-5"  # noqa: E731
    assert cr.recon_resolve(conn, rid, "keep 5000", "otl-9", {"blk-7": "3"}, "t",
                            owner_turn_ok=yes) == (False, "owner_turn_unverified")
    assert cr.recon_resolve(conn, rid, "keep 5000", None, {"blk-7": "3"}, "t",
                            owner_turn_ok=yes) == (False, "owner_turn_unverified")
    assert cr.recon_resolve(conn, rid, "keep 5000", "otl-5", {"blk-7": "4"}, "t",
                            owner_turn_ok=yes) == (False, "stale_record_version")
    assert cr.recon_pending(conn)[0]["state"] == "deferred"
    assert cr.recon_resolve(conn, rid, "keep 5000", "otl-5", {"blk-7": "3"}, "t",
                            owner_turn_ok=yes) == (True, None)
    assert cr.recon_pending(conn) == []
    with pytest.raises(ValueError):
        cr.recon_enqueue(conn, "s", None, [], [], "router", [], {}, "t")


def test_default_owner_turn_check_fails_closed():
    assert cr.default_owner_turn_ok("otl-1", root=str(ENGINE_DIR)) is False


def test_legacy_outbox_is_detected_and_migrated_once():
    import consolidation_router as legacy
    conn = sqlite3.connect(":memory:")
    legacy.route_items(conn, [{"id": "m-1", "text": "fact", "kind": "context",
                               "evidence_verified": True}], {"memory"}, None, "t0")
    cr.init_tables(conn)
    cr.init_tables(conn)
    rows = conn.execute("SELECT intent_id,destination,state FROM routing_outbox").fetchall()
    assert rows == [("legacy-outbox:1", "memory", "queued")]
    assert conn.execute("SELECT COUNT(*) FROM consolidation_outbox").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# provider protocol: read-only, bounded, argv without a shell
# ---------------------------------------------------------------------------

def test_provider_protocol_changes_and_evidence_are_bounded_reads(tmp_path):
    root = make_root(tmp_path)
    kdir = install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")],
                       records={"mode": "ok", "changes": [{"n": i} for i in range(5)],
                                "snapshots": [{"s": i} for i in range(20)]},
                       ops=("lookup", "changes", "evidence"))
    client = cr.ProviderClient(str(root), cr.installed_contracts(str(root)))
    ch = client.changes("acme", "p", cursor=None, limit=3)
    assert ch["ok"] and len(ch["changes"]) == 3 and ch["truncated"] and ch["cursor"] == "5"
    nxt = client.changes("acme", "p", cursor=ch["cursor"])
    assert nxt["ok"] and nxt["changes"] == []
    ev = client.evidence("acme", "p", "spend")
    assert ev["ok"] and len(ev["snapshots"]) == cr.MAX_EVIDENCE
    assert client.call("acme", "p", "write", {}) == (False, "not_a_read_operation")
    assert {c["op"] for c in calls(kdir)} == {"changes", "evidence"}


def test_provider_undeclared_operation_and_containment(tmp_path):
    root = make_root(tmp_path)
    install_kit(root, "acme", ["spend", "dev-ops"], [bind("resource", "spend")])
    contracts = cr.installed_contracts(str(root))
    client = cr.ProviderClient(str(root), contracts)
    assert client.call("acme", "p", "changes", {}) == (False, "operation_not_declared")
    assert client.changes("acme", "p", cursor="7")["cursor"] == "7", "old cursor kept"
    seen = []

    def spy(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"protocol": 1, "result": "absent"}), "")

    escaped = [("acme", json.loads(json.dumps(contracts[0][1])))]
    escaped[0][1]["providers"]["p"]["adapter"] = "../outside.py"
    (tmp_path / "outside.py").write_text("")
    bad = cr.ProviderClient(str(root), escaped, runner=spy)
    assert bad.lookup("acme", "p", {})["why"] == "adapter_missing" and seen == []
    good = cr.ProviderClient(str(root), contracts, runner=spy)
    assert good.lookup("acme", "p", {"text": "$(rm -rf ~); `x`"})["result"] == "absent"
    argv, kw = seen[0]
    assert kw["shell"] is False and argv[-1] == "lookup" and isinstance(argv, list)
    assert "$(rm" in kw["input"] and all("$(rm" not in a for a in argv)
