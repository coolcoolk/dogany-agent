#!/usr/bin/env python3
"""DGN-654 gate: forward-tolerant mirror version gate (sdk_bridge.get_conn).

Incident: live Ag event schema ran ahead to user_version=15 via ADDITIVE
Ag-local relationship-module migrations (012_persons_gender /
013_persons_relations / 014_relation_type_unify) while the mirror pin was
the exact whitelist ALLOWED_USER_VERSIONS = (11,) -- every mirror poll
crashed with MigrationRequired for ~2 days. The fix replaces the exact
whitelist with MIN_USER_VERSION floor + capability probe
(REQUIRED_CAPABILITIES: the tables/columns the mirror actually touches).

Live-landing fix (Option A): the v11 floor pre-empted the probe for a
VALID v10 lineage (Warg core: full probe pass, user_version=10). The
floor is now PROBE_FLOOR_USER_VERSION = 10 and the probe is the true
gate; MIN_USER_VERSION stays 11 as the canonical baseline / reverse-drift
guard marker (lowering it would make instance copies look "ahead" and
block propagation).

Asserts:
  (a) a v15 schema (v11 baseline + additive relationship-module deltas
      mimicking Ag 012-014) is ACCEPTED -- no MigrationRequired -- and the
      bridge works for real (lookup roundtrip on an inserted event);
  (b) the v11 baseline is still accepted; MIN_USER_VERSION stays 11 and
      PROBE_FLOOR_USER_VERSION is 10;
  (c) a below-floor schema (v9 stamp) is rejected with a clear
      minimum-version message;
  (d) the capability probe fails LOUDLY when a genuinely required
      table/column is missing, even though user_version claims >= floor
      (diverged/destructive lineage);
  (e) the probe is a no-op (empty missing list) on the clean baseline --
      i.e. every REQUIRED_CAPABILITIES entry exists in v11, so any DB
      reached from v11 by additive migration passes by construction;
  (f) Warg lineage: a probe-passing v10 DB is ACCEPTED (bridge functional),
      and a v10 DB that FAILS the probe is still rejected.

Real modules -- no stubs: mirror/sdk_bridge.py + database/lifekit.py are
imported for real; DBs are scratch sqlite files built from
database/schema.sql. No network, no live instance touched.

Run: python3 tests/mirror/test_dgn654_version_tolerance.py  (exit 0 = pass)
Also runs under pytest if installed (plain test_* functions).
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

# DGN-803 LS-5: database/lifekit.py + schema.sql left the framework tree
# (independent lifekit pack). The mirror engine stays in-tree but its lifekit
# import only ever runs on instances WITH the kit installed (mirror-poll gates
# on MIRROR_MODULE=on in the kit-installed lifekit.conf). To test the real
# seam, build an instance-shaped scratch tree: repo mirror/ + pack payload
# database/. Payload absent -> loud SKIP (exit 0).
LIFEKIT_PAYLOAD = os.environ.get(
    "DOGANY_LIFEKIT_PAYLOAD",
    os.path.join(os.path.dirname(REPO_ROOT), "dogany-lifekit", "payload"))
if not os.path.isfile(os.path.join(LIFEKIT_PAYLOAD, "database", "lifekit.py")):
    if os.environ.get("DGN803_RELEASE_GATE", "0") == "1":
        print("FAIL: lifekit pack payload not found at %s (DGN803_RELEASE_GATE=1 -- payload required)" % LIFEKIT_PAYLOAD, file=sys.stderr)
        print("      clone dogany-lifekit next to this repo (or set DOGANY_LIFEKIT_PAYLOAD)", file=sys.stderr)
        sys.exit(1)
    print("SKIP: lifekit pack payload not found at %s (DGN-803 LS-5)" % LIFEKIT_PAYLOAD)
    print("      clone dogany-lifekit next to this repo (or set DOGANY_LIFEKIT_PAYLOAD)")
    print("      set DGN803_RELEASE_GATE=1 to require payload presence (release gate mode)")
    sys.exit(0)

_SCRATCH_ROOT = tempfile.mkdtemp(prefix="dgn654-tree-")
shutil.copytree(os.path.join(REPO_ROOT, "mirror"),
                os.path.join(_SCRATCH_ROOT, "mirror"),
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db*"))
os.makedirs(os.path.join(_SCRATCH_ROOT, "database"), exist_ok=True)
for _f in ("lifekit.py", "schema.sql"):
    shutil.copy(os.path.join(LIFEKIT_PAYLOAD, "database", _f),
                os.path.join(_SCRATCH_ROOT, "database", _f))

SRC_MIRROR = os.path.join(_SCRATCH_ROOT, "mirror")
SCHEMA_SQL = os.path.join(_SCRATCH_ROOT, "database", "schema.sql")

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


def _import_bridge():
    """Import the REAL mirror/sdk_bridge.py (which imports the real
    database/lifekit.py via its self-locating sys.path insert)."""
    spec = importlib.util.spec_from_file_location(
        "sdk_bridge_dgn654", os.path.join(SRC_MIRROR, "sdk_bridge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _import_bridge()


def _mk_db(path):
    """Fresh baseline DB from the pack payload schema.sql (DGN-803 LS-5:
    the baseline pin is whatever the pack ships -- v11 historically, v19+
    post-split; the gate contract under test is baseline-relative)."""
    conn = sqlite3.connect(path)
    with open(SCHEMA_SQL) as fh:
        conn.executescript(fh.read())
    conn.commit()
    conn.close()


def _baseline_ver():
    """PRAGMA user_version the payload schema.sql stamps."""
    d = tempfile.mkdtemp(prefix="dgn654-base-")
    db = os.path.join(d, "base.db")
    _mk_db(db)
    conn = sqlite3.connect(db)
    v = conn.execute("PRAGMA user_version;").fetchone()[0]
    conn.close()
    return v


BASE_VER = _baseline_ver()
AHEAD_VER = BASE_VER + 4  # mimics the historical v11->v15 additive run-ahead


def _apply_ahead_additive(path):
    """Synthetic ADDITIVE delta mimicking the Ag 012-014 incident class
    (domain tables the mirror never reads), stamped baseline+4 -- the
    baseline-relative equivalent of the original v11->v15 fixture
    (DGN-803 LS-5: the payload baseline moved past the historical pins,
    so the fixture is now derived, not hardcoded)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE dgn654_domain_extra (
            id INTEGER PRIMARY KEY,
            note TEXT
        );
        ALTER TABLE dgn654_domain_extra ADD COLUMN extra_canonical TEXT;
        PRAGMA user_version = %d;
    """ % AHEAD_VER)
    conn.commit()
    conn.close()


def _insert_event(path, ulid):
    """Minimal CHECK-satisfying event row (untimed floating task)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO event (ulid, kind, title, schedule_kind, "
        "slot_exclusive, owning_agent, created_by, time_mode, "
        "created_at, updated_at) VALUES (?, 'task', 'probe row', "
        "'untimed', 0, 'test', 'test', 'floating', "
        "'2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z')", (ulid,))
    conn.commit()
    conn.close()


def test_v15_accepted():
    print("(a) ahead additive schema (base+4) accepted + bridge functional:")
    d = tempfile.mkdtemp(prefix="dgn654-")
    db = os.path.join(d, "lifekit.db")
    _mk_db(db)
    _apply_ahead_additive(db)
    _insert_event(db, "01TESTULID654A")
    try:
        conn = B.get_conn(db)
    except B.ec.MigrationRequired as e:
        _check("ahead schema accepted (no MigrationRequired)", False, e)
        return
    v = conn.execute("PRAGMA user_version;").fetchone()[0]
    _check("fixture really carries user_version=%d" % AHEAD_VER,
           v == AHEAD_VER, v)
    found = B.lookup(conn, "01TESTULID654A")
    _check("lookup roundtrip works on ahead schema", found is not None
           and found[1] == 0, found)
    _check("row_factory set (dict-style access)",
           conn.execute("SELECT ulid FROM event LIMIT 1")
           .fetchone()["ulid"] == "01TESTULID654A", "row_factory missing")
    conn.close()


def test_v11_baseline_still_accepted():
    print("(b) v11 baseline still accepted + constants:")
    d = tempfile.mkdtemp(prefix="dgn654-")
    db = os.path.join(d, "lifekit.db")
    _mk_db(db)
    try:
        conn = B.get_conn(db)
        conn.close()
        ok, detail = True, ""
    except B.ec.MigrationRequired as e:
        ok, detail = False, e
    _check("v11 accepted", ok, detail)
    _check("MIN_USER_VERSION stays 11 (baseline / drift-guard marker)",
           B.MIN_USER_VERSION == 11, B.MIN_USER_VERSION)
    _check("PROBE_FLOOR_USER_VERSION is 10 (hard reject floor)",
           B.PROBE_FLOOR_USER_VERSION == 10, B.PROBE_FLOOR_USER_VERSION)


def test_below_floor_rejected():
    print("(c) below-floor schema (v9) rejected with clear message:")
    d = tempfile.mkdtemp(prefix="dgn654-")
    db = os.path.join(d, "lifekit.db")
    _mk_db(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 9;")
    conn.commit()
    conn.close()
    try:
        B.get_conn(db)
        _check("v9 rejected", False, "no exception raised")
    except B.ec.MigrationRequired as e:
        msg = str(e)
        _check("v9 rejected (MigrationRequired)", True)
        _check("message names the floor", "minimum supported is 10" in msg,
               msg)
        _check("message carries the actionable update.sh hint",
               "update.sh" in msg, msg)


def test_capability_probe_fails_loudly():
    print("(d) capability probe: missing table/column -> loud reject:")
    # Missing TABLE: ahead-stamped DB whose sub_event was renamed away.
    d = tempfile.mkdtemp(prefix="dgn654-")
    db = os.path.join(d, "lifekit.db")
    _mk_db(db)
    _apply_ahead_additive(db)
    conn = sqlite3.connect(db)
    conn.executescript("ALTER TABLE sub_event RENAME TO sub_event_gone;")
    conn.commit()
    conn.close()
    try:
        B.get_conn(db)
        _check("missing sub_event table rejected", False, "no exception")
    except B.ec.MigrationRequired as e:
        msg = str(e)
        _check("missing sub_event table rejected", True)
        _check("message names the missing table", "sub_event" in msg, msg)
        _check("message forbids bypass / points at lineage",
               "lineage" in msg, msg)

    # Missing COLUMN: place.name renamed away, version still claims ahead.
    d2 = tempfile.mkdtemp(prefix="dgn654-")
    db2 = os.path.join(d2, "lifekit.db")
    _mk_db(db2)
    _apply_ahead_additive(db2)
    conn = sqlite3.connect(db2)
    conn.executescript("ALTER TABLE place RENAME COLUMN name TO label;")
    conn.commit()
    conn.close()
    try:
        B.get_conn(db2)
        _check("missing place.name column rejected", False, "no exception")
    except B.ec.MigrationRequired as e:
        msg = str(e)
        _check("missing place.name column rejected", True)
        _check("message names the missing column", "place.name" in msg, msg)


def test_probe_clean_on_baseline():
    print("(e) probe empty on clean v11 (capabilities all v11-present):")
    d = tempfile.mkdtemp(prefix="dgn654-")
    db = os.path.join(d, "lifekit.db")
    _mk_db(db)
    conn = sqlite3.connect(db)
    missing = B.missing_capabilities(conn)
    conn.close()
    _check("missing_capabilities(v11) == []", missing == [], missing)
    _check("REQUIRED_CAPABILITIES covers the mirror's tables",
           set(B.REQUIRED_CAPABILITIES) ==
           {"event", "sub_event", "place", "persons", "event_persons",
            "roller_log"},
           sorted(B.REQUIRED_CAPABILITIES))


def test_v10_warg_lineage():
    print("(f) Warg lineage: probe-passing v10 accepted, probe-fail v10 "
          "rejected:")
    # Probe-passing v10: full baseline schema content, stamped
    # user_version=10 exactly as Warg core carries (all probe
    # tables/columns present; only the version stamp is pre-baseline).
    d = tempfile.mkdtemp(prefix="dgn654-")
    db = os.path.join(d, "lifekit.db")
    _mk_db(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 10;")
    conn.commit()
    conn.close()
    _insert_event(db, "01TESTULID654W")
    try:
        conn = B.get_conn(db)
    except B.ec.MigrationRequired as e:
        _check("probe-passing v10 accepted", False, e)
        conn = None
    if conn is not None:
        v = conn.execute("PRAGMA user_version;").fetchone()[0]
        _check("fixture really carries user_version=10", v == 10, v)
        found = B.lookup(conn, "01TESTULID654W")
        _check("lookup roundtrip works on v10", found is not None
               and found[1] == 0, found)
        conn.close()

    # Probe-failing v10: same stamp, but a required column renamed away --
    # the probe (the true gate) must still reject.
    d2 = tempfile.mkdtemp(prefix="dgn654-")
    db2 = os.path.join(d2, "lifekit.db")
    _mk_db(db2)
    conn = sqlite3.connect(db2)
    conn.executescript("""
        ALTER TABLE place RENAME COLUMN name TO label;
        PRAGMA user_version = 10;
    """)
    conn.commit()
    conn.close()
    try:
        B.get_conn(db2)
        _check("probe-failing v10 rejected", False, "no exception raised")
    except B.ec.MigrationRequired as e:
        msg = str(e)
        _check("probe-failing v10 rejected (MigrationRequired)", True)
        _check("message names the missing column", "place.name" in msg, msg)


def main():
    print("DGN-654 forward-tolerant version gate:")
    test_v15_accepted()
    test_v11_baseline_still_accepted()
    test_below_floor_rejected()
    test_capability_probe_fails_loudly()
    test_probe_clean_on_baseline()
    test_v10_warg_lineage()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures),
                                           ", ".join(_failures)))
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
