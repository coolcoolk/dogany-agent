"""
DGN-180 SDK bridge -- PRODUCTION (mirror/). Thin seam over the live
lifekit SDK: result-string mapping for the adapter's retry classifier.
The DGN-180 verbs (unsettle / bypass_schedule_apply / bypass_event_add /
recompute / force_settle settled_at param) live in lifekit.py itself
(canonical c451aa8); this module only wraps and re-exports.
English/ASCII only.
"""

import os
import sys
import sqlite3

# Live SDK: mirror/ -> ../database on sys.path (self-locating, no
# absolute home paths).
_DB_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "database"))
if _DB_DIR not in sys.path:
    sys.path.insert(0, _DB_DIR)

import lifekit as ec  # noqa: E402  (adapter reaches ec.* through this alias)

# DGN-654: forward-tolerant version gate. The exact-whitelist pin
# (ALLOWED_USER_VERSIONS = (11,)) broke the mirror whenever a live
# instance ran ahead via ADDITIVE domain migrations (v11-vs-v15 incident:
# Ag relationship-module migrations 012-014 added tables the mirror never
# reads, yet the whitelist hard-stopped every poll for ~2 days -- same
# defect pattern as the DGN-364-era (6,7,8) pin). Contract:
#   (1) hard floor: user_version >= PROBE_FLOOR_USER_VERSION. Lineages at
#       or below v9 predate the mirror surface and are rejected outright.
#   (2) capability probe -- the TRUE gate: the tables/columns the mirror
#       ACTUALLY depends on must exist (REQUIRED_CAPABILITIES below).
#       Trust capabilities, not version numbers -- a future additive
#       migration passes without a lockstep re-pin; a diverged/destructive
#       lineage that dropped a needed column still fails loudly at open
#       time.
# Live-landing fix (DGN-654 Option A): the original v11 floor pre-empted
# the probe for a VALID pre-baseline lineage (Warg core, user_version=10,
# full probe pass), defeating "the probe is the true gate". The floor is
# therefore PROBE_FLOOR_USER_VERSION = 10 (oldest live lineage); accept
# iff user_version >= 10 AND the probe passes.
# Every column listed below exists in the v11 baseline schema, so any DB
# reached from v11 by additive migration satisfies the probe by
# construction; the v10 Warg lineage passes by direct probe.
#
# MIN_USER_VERSION stays 11 = canonical baseline (schema.sql stamp). It is
# ALSO the monotonic lineage marker parsed by update.sh's reverse-drift
# guard: NEVER lower it -- instance copies carrying the old value would
# look "ahead" and the guard would block propagation of this very file.
# Widen acceptance downward only via PROBE_FLOOR_USER_VERSION.
MIN_USER_VERSION = 11
PROBE_FLOOR_USER_VERSION = 10

# Tables + columns the mirror path reads/writes by name in the lifekit DB
# (adapter.py projections/pull-apply/bookkeeping + this bridge's lookup /
# settle / cancel / content_update verbs). Mirror-state tables
# (mirror_state / mirror_outbox / push_snapshot / ...) live in the
# separate mirror_state.db and are NOT gated here.
REQUIRED_CAPABILITIES = {
    "event": (
        "id", "ulid", "version", "kind", "title", "note",
        "schedule_kind", "start_at", "end_at", "display_tz",
        "open_ended", "slot_exclusive", "status", "completion_rule",
        "settled_at", "settled_by", "settled_outcome", "created_by",
        "is_routine", "location", "location_url", "purpose", "summary",
        "recurrence_id", "rec_date", "block_class", "place_id",
        "gcal_event_id", "gtask_id", "gcal_etag", "gtask_etag",
        # DGN-654 grill: adapter+settle/drag/adopt write-paths also touch
        # these; missing them green-lit a lineage that crashes the dec-011
        # settle path (the incident path). All v11-present -> additive.
        "updated_at", "created_at", "owning_agent", "time_mode",
        "rec_exception", "derived_from", "derived_role", "derived_pinned",
    ),
    "sub_event": ("event_id", "done", "tombstone"),
    "place": ("id", "name"),
    "persons": ("id", "name"),
    "event_persons": ("event_id", "person_id"),
    # Written on the lifekit conn (NOT the state db) by _travel_log on each
    # cleared travel-skip -- looks state-db-ish but is gated here (DGN-654 grill).
    "roller_log": ("ts", "recurrence_id", "category", "detail"),
}


def missing_capabilities(conn):
    """Probe the open DB for the mirror's required tables/columns.
    Returns a sorted list of 'table' / 'table.column' strings that are
    absent (empty list = capability-compatible)."""
    missing = []
    for table, cols in REQUIRED_CAPABILITIES.items():
        try:
            have = {r[1] for r in
                    conn.execute("PRAGMA table_info(%s)" % table)}
        except sqlite3.Error:
            have = set()
        if not have:
            missing.append(table)
            continue
        missing.extend("%s.%s" % (table, c) for c in cols
                       if c not in have)
    return sorted(missing)


def get_conn(db_path=None):
    """Hardened connection via the live SDK (WAL/busy_timeout/foreign_keys).
    Version gate: the PROBE_FLOOR_USER_VERSION floor + capability probe
    here is the SOLE mirror-side gate (event_conn is opened with
    assert_version=False; its own EXPECTED_USER_VERSION assert applies to
    CLI lanes only)."""
    conn = ec.event_conn(db_path, assert_version=False)
    v = conn.execute("PRAGMA user_version;").fetchone()[0]
    if v < PROBE_FLOOR_USER_VERSION:
        conn.close()
        raise ec.MigrationRequired(
            "event schema user_version=%d, minimum supported is %d. "
            "Run update.sh (framework migrations) before using the DGN-180 "
            "adapter." % (v, PROBE_FLOOR_USER_VERSION))
    missing = missing_capabilities(conn)
    if missing:
        conn.close()
        raise ec.MigrationRequired(
            "event schema user_version=%d meets the version floor (>=%d) "
            "but lacks required mirror capabilities: %s. The schema lineage "
            "is incompatible with the DGN-180 adapter -- do NOT bypass this "
            "check; reconcile the migration lineage first."
            % (v, PROBE_FLOOR_USER_VERSION, ", ".join(missing)))
    conn.row_factory = sqlite3.Row
    return conn


def lookup(conn, ulid):
    """(eid, version) for a ulid or None."""
    row = conn.execute(
        "SELECT id, version FROM event WHERE ulid=?;", (ulid,)).fetchone()
    if row is None:
        return None
    return row["id"], row["version"]


def settle_done(conn, ulid, settled_by, settled_at_ts=None):
    """Inbound completed -> force_settle('done') with the surface completion
    instant (lifekit settled_at_ts param, grill-4 6(v)).
    Returns 'applied' | 'already_done' | 'cas_fail' | 'not_found'."""
    found = lookup(conn, ulid)
    if found is None:
        return "not_found"
    eid, version = found

    def run():
        return ec.force_settle(conn, eid, version, settled_by,
                               settled_at_ts=settled_at_ts)

    res = ec.with_retry(run)
    if res == ec.MutationResult.APPLIED:
        return "applied"
    if res == ec.MutationResult.CAS_FAIL:
        row = conn.execute(
            "SELECT settled_outcome FROM event WHERE id=?;", (eid,)).fetchone()
        if row is None:
            return "not_found"
        if row["settled_outcome"] == "done":
            return "already_done"
        return "cas_fail"
    return "not_found"


def cancel_abandoned(conn, ulid, by):
    """dec-011: owner-deleted surface object -> cancel verb (abandoned).
    Returns 'applied'|'already_abandoned'|'cas_fail'|'not_found'."""
    found = lookup(conn, ulid)
    if found is None:
        return "not_found"
    eid, version = found

    def run():
        return ec.cancel(conn, eid, version, by)

    res = ec.with_retry(run)
    if res == ec.MutationResult.APPLIED:
        return "applied"
    if res == ec.MutationResult.CAS_FAIL:
        row = conn.execute(
            "SELECT settled_outcome FROM event WHERE id=?;", (eid,)).fetchone()
        if row and row["settled_outcome"] == "abandoned":
            return "already_abandoned"
        return "cas_fail"
    return "not_found"


def content_update(conn, ulid, fields):
    """Inbound content edits (title/note/location) -> event_set_meta (D1:
    CAS version bump + updated_at; meta allowlist -- no schedule fields,
    no settle state reachable). The cutover patch dropped the sandbox
    content_update verb in favor of this routing, but the bridge wrapper
    was never added (DGN-242 fix). DGN-240 (M-B ruling): content edits do
    NOT stamp rec_exception -- the CAS version bump already makes the row
    regen-untouchable, and the row stays in rule governance + lapse.
    Stamping is owned by bypass_schedule_apply alone.
    Returns 'applied' | 'noop' | 'cas_fail' | 'not_found' | 'rejected'."""
    updates = {k: v for k, v in fields.items()
               if k in ("title", "note", "location")}
    if not updates:
        return "noop"
    found = lookup(conn, ulid)
    if found is None:
        return "not_found"
    eid, version = found

    def run():
        return ec.event_set_meta(conn, eid, version, updates)

    try:
        # MutationResult values ARE the adapter's classifier strings
        # (applied / cas_fail / not_found); cas_fail retries via _run_verb,
        # which re-enters here and re-reads the fresh version.
        return ec.with_retry(run)
    except ValueError:
        # event_set_meta contract violation (empty title / kind-illegal
        # column): classified failure for the adapter's verb_failed audit
        # path, never a crash path (g14 stance).
        return "rejected"


# DGN-180 verbs: live in lifekit.py -- re-exported through the seam.
unsettle = ec.unsettle
bypass_schedule_apply = ec.bypass_schedule_apply
bypass_event_add = ec.bypass_event_add
recompute = ec.recompute
# DGN-333 (MAJOR-5 rev): batch-end overlap recheck for deferred notices.
mirror_overlap_recheck = ec.mirror_overlap_recheck
