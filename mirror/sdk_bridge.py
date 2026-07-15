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

ALLOWED_USER_VERSIONS = (6, 7)  # 007 rollout window (DGN-273); pin to
                                # (7,) after cutover settles


def get_conn(db_path=None):
    """Hardened connection via the live SDK (WAL/busy_timeout/foreign_keys).
    Version gate: whitelist check here (event_conn's own assert also holds
    since EXPECTED_USER_VERSION == 5)."""
    conn = ec.event_conn(db_path, assert_version=False)
    v = conn.execute("PRAGMA user_version;").fetchone()[0]
    if v not in ALLOWED_USER_VERSIONS:
        conn.close()
        raise ec.MigrationRequired(
            "event schema user_version=%d, expected one of %s. "
            "Run update.sh (framework migrations) before using the DGN-180 "
            "adapter." % (v, set(ALLOWED_USER_VERSIONS)))
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
