"""
DGN-180 W12 weekly reconcile job (X10b items 5+6) -- promoted from
fulldiff.py.

- Tombstone-aware: a cancelled calendar item whose SoT row is routed to
  tasks (routing-flip residue) or settled abandoned (our cancel push) is
  EXPECTED, not an orphan.
- Auto-repair safe classes only: missing-on-surface -> outbox re-push
  (idempotent sync). Field mismatches and orphans -> report only (a blind
  re-push could clobber an unseen owner edit; orphans need owner policy
  dec-009).
- Failed-row retry (item 6): failed outbox rows get bounded re-attempts
  (requeues < MAX_REQUEUES -> back to queued with reset attempts); at the
  cap -> repeated_failure notification. No infinite silent dead rows.
- Summary lands on the notification interface (sandbox notify_outbox).
- DGN-294/DGN-364 (V15): run_reconcile takes the cal_ids DICT (string shim
  accepted); the scan set covers ALL engraved category calendars; per-ulid
  LISTS + calendar_dup detection; wrong-calendar drift attention.

Usage: python3 reconcile.py [--state <state_db>] [--repair] [--drain]
English/ASCII only.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
import adapter as A
import mirror_i18n
import notify as notify_mod
import sdk_bridge

MAX_REQUEUES = 3

# DGN-268 S1 (H7) -> S4: user-facing strings route through a single seam. The
# ko literal is the fallback; the live value is resolved by AGENT_LANG through
# mirror_i18n (i18n key 'mirror.reconcile_verdict_attention'). notify.py owns
# its own TEMPLATES dict; this covers the one literal composed here.
TEXT = {
    "verdict_attention": u"확인 필요",
}


def _fetch_all_calendar(cal_id):
    items, tok = [], None
    while True:
        params = {"calendarId": cal_id, "maxResults": 250,
                  "singleEvents": True, "showDeleted": True}
        if tok:
            params["pageToken"] = tok
        resp = A.gws("calendar", "events", "list", "--params", json.dumps(params))
        items.extend(resp.get("items", []))
        tok = resp.get("nextPageToken")
        if not tok:
            return items


def _fetch_all_tasks(tl_id):
    items, tok = [], None
    while True:
        params = {"tasklist": tl_id, "maxResults": 100, "showHidden": True,
                  "showCompleted": True, "showDeleted": True}
        if tok:
            params["pageToken"] = tok
        resp = A.gws("tasks", "tasks", "list", "--params", json.dumps(params))
        items.extend(resp.get("items", []))
        tok = resp.get("nextPageToken")
        if not tok:
            return items


def retry_failed(state_conn, src_conn, max_requeues=MAX_REQUEUES):
    """Bounded re-attempts for failed outbox rows. At the requeue cap the
    row gets ONE repeated_failure notification and then goes terminal
    (dead=1, g5-4): no infinite silent dead rows, no weekly re-alerts."""
    requeued, capped, dead_skipped = 0, 0, 0
    rows = state_conn.execute(
        "SELECT * FROM mirror_outbox WHERE status='failed'").fetchall()
    for r in rows:
        if r["dead"]:
            dead_skipped += 1
            continue
        if r["requeues"] < max_requeues:
            try:
                state_conn.execute(
                    "UPDATE mirror_outbox SET status='queued', attempts=0, "
                    "requeues=requeues+1, updated_at=? WHERE id=?",
                    (A._now_iso(), r["id"]))
                state_conn.commit()
                requeued += 1
            except sqlite3.IntegrityError:
                state_conn.rollback()  # another pending row exists -> skip
        else:
            capped += 1
            notify_mod.notify(
                state_conn, "repeated_failure", r["event_ulid"],
                title=A._title_of(src_conn, r["event_ulid"]))
            state_conn.execute(
                "UPDATE mirror_outbox SET dead=1, updated_at=? WHERE id=?",
                (A._now_iso(), r["id"]))
            state_conn.commit()
    return {"requeued": requeued, "capped": capped,
            "dead_skipped": dead_skipped}


def run_reconcile(state_conn, src_conn, cal_ids, tl_id, repair=True,
                  scope_ulids=None):
    """scope_ulids: when provided, only these SoT ulids are reconciled
    (targeted reconcile / sample-scale test). None = full DB (cutover job).
    g6-12: classification runs under the single mirror lock (poll/drain
    excluded while we read surfaces + apply dec-011).
    cal_ids (V15): the category dict; a legacy single id string is accepted
    via the _cal_ids_dict shim."""
    if not A._acquire_drain_lock(state_conn):
        return {"verdict": "LOCKED", "status": "locked"}
    try:
        return _run_reconcile_locked(state_conn, src_conn, cal_ids, tl_id,
                                     repair, scope_ulids)
    finally:
        A._release_drain_lock(state_conn)


def _run_reconcile_locked(state_conn, src_conn, cal_ids, tl_id, repair,
                          scope_ulids):
    # DGN-294/DGN-364 (V15): reconcile = full scan of ALL engraved category
    # calendars. The scan set comes from the same 2.2 dict-shape rule as
    # poll_cycle (_calendar_scan_set): a legacy/all-identical dict collapses
    # to ONE fetch with no category attribution (no drift attention in
    # legacy mode). Track WHICH calendar each item came from for drift
    # attention (multi mode only).
    cal_ids = A._cal_ids_dict(cal_ids)
    cal_items = []
    item_cal_key = {}
    for cid, key in A._calendar_scan_set(cal_ids):
        for it in _fetch_all_calendar(cid):
            cal_items.append(it)
            if key is not None and it.get("id"):
                item_cal_key[it["id"]] = key
    task_items = _fetch_all_tasks(tl_id)

    # grill m1: per-ulid LISTS -- a dict would silently collapse the same
    # ulid appearing on two calendars (exactly the drift/duplication case
    # reconcile exists to catch).
    cal_by_ulid, foreign_cal = {}, 0
    for it in cal_items:
        u = A._extract_ulid(it)
        if u is None:
            foreign_cal += 1
        else:
            cal_by_ulid.setdefault(u, []).append(it)
    # grill-5 finding 2: keep the deleted-tombstone id set (matched by the
    # gtask_id bookkeeping column, NOT by sentinel -- tombstone notes
    # preservation is unproven, finding 8).
    task_by_ulid, foreign_task = {}, 0
    deleted_task_ids = set()
    for it in task_items:
        if it.get("deleted"):
            if it.get("id"):
                deleted_task_ids.add(it["id"])
            continue  # tombstone = not a live surface object
        m = A._SENTINEL_RE.search(it.get("notes", "") or "")
        if m:
            task_by_ulid[m.group(1)] = it
        else:
            foreign_task += 1

    missing, mismatched, expected_tombstones = [], [], []
    # DGN-302: abandoned rows whose gcal event is still confirmed (the cancel
    # push was never enqueued). Tracked separately so repair can enqueue a
    # sync via the normal outbox path (push_calendar projects abandoned ->
    # cancelled). Foreign-item guard is unchanged: we only reach here via
    # cal_by_ulid, which only contains our-ulid items.
    abandoned_gcal_drift = []
    owner_deleted = []
    checked = 0
    sot_rows = {}
    for r in src_conn.execute("SELECT * FROM event").fetchall():
        ev = dict(r)
        if not A.in_mirror_scope(ev):
            continue
        if scope_ulids is not None and ev["ulid"] not in scope_ulids:
            continue
        sot_rows[ev["ulid"]] = ev
        checked += 1
        target = A.route_surface(ev)
        abandoned = (ev.get("settled_outcome") == "abandoned"
                     or ev.get("status") == "abandoned")

        cal_list = cal_by_ulid.pop(ev["ulid"], [])
        if len(cal_list) > 1:
            # m1: same ulid on >1 calendar = duplication drift -> report,
            # never silently pick-and-hide. Classification continues with
            # the copy on the EXPECTED calendar when present.
            homes = sorted(item_cal_key.get(i.get("id"), "?")
                           for i in cal_list)
            A.mirror_log(state_conn, "calendar_dup", ev["ulid"],
                         "copies=%s" % ",".join(homes))
            mismatched.append(("calendar_dup", ev["ulid"],
                               {"copies": homes}))
        want_key = A.route_cal_key(ev)
        cal_it = None
        for i in cal_list:
            if item_cal_key.get(i.get("id")) == want_key:
                cal_it = i
                break
        if cal_it is None and cal_list:
            cal_it = cal_list[0]
        task_it = task_by_ulid.pop(ev["ulid"], None)

        if abandoned:
            # Expected: calendar tombstone-or-absent, task absent.
            # DGN-302 (V15 re-graft): consider ALL calendar copies for this
            # ulid -- if ANY copy is still non-cancelled the cancel push
            # never landed = drift. With repair=True we enqueue a sync so
            # the drain pushes status=cancelled (ONE entry per ulid, never
            # per copy; classified separately from mismatched so the repair
            # path is targeted and the report is informative). All copies
            # cancelled stays the expected-tombstone case.
            if cal_list:
                if any(i.get("status") != "cancelled" for i in cal_list):
                    abandoned_gcal_drift.append(ev["ulid"])
                else:
                    expected_tombstones.append(("abandoned-cal", ev["ulid"]))
            if task_it is not None and not task_it.get("deleted"):
                mismatched.append(("tasks", ev["ulid"],
                                   {"expected": "deleted",
                                    "got": task_it.get("status")}))
            continue

        if target == "calendar":
            if task_it is not None and not task_it.get("deleted"):
                mismatched.append(("tasks", ev["ulid"],
                                   {"expected": "absent (calendar-routed)",
                                    "got": task_it.get("status")}))
            if cal_it is None:
                missing.append(("calendar", ev["ulid"]))
                continue
            # V15 drift attention: found in a calendar route() does not map
            # this row to -> ONE report line, no silent move.
            found_key = item_cal_key.get(cal_it.get("id"))
            if found_key is not None and found_key != want_key:
                A.mirror_log(state_conn, "calendar_drift", ev["ulid"],
                             "found_in=%s expected=%s"
                             % (found_key, want_key))
                mismatched.append(("calendar_drift", ev["ulid"],
                                   {"found_in": found_key,
                                    "expected": want_key}))
                continue
            if cal_it.get("status") == "cancelled":
                # live row but tombstoned surface = drift (owner cancel
                # awaiting decision, or W3 revive pending) -> report only.
                mismatched.append(("calendar", ev["ulid"],
                                   {"expected": "confirmed",
                                    "got": "cancelled (owner decision or "
                                           "revive pending)"}))
                continue
            p_ev = A.calendar_projection_from_event(ev)
            p_it = A.calendar_projection_from_item(cal_it)
            if p_ev != p_it:
                diffs = {k: (p_ev.get(k), p_it.get(k)) for k in p_ev
                         if p_ev.get(k) != p_it.get(k)}
                # Owner decision 2026-07-14: cosmetic surface appearance
                # (color markers) quietly accepts the Google side as truth.
                # color_id drift is NOT a mismatch and never repaired.
                diffs.pop("color_id", None)
                if not diffs:
                    continue
                mismatched.append(("calendar", ev["ulid"], diffs))
        else:
            # Tasks-routed: a cancelled calendar leftover = flip tombstone.
            if cal_it is not None:
                if cal_it.get("status") == "cancelled":
                    expected_tombstones.append(("flip-tombstone", ev["ulid"]))
                else:
                    mismatched.append(("calendar", ev["ulid"],
                                       {"expected": "cancelled flip tombstone",
                                        "got": cal_it.get("status")}))
            if task_it is None:
                # dec-011 (cutover round): a live SoT row whose surface task
                # was DELETED by the owner -> cancel verb (abandoned) +
                # bookkeeping clear + one confirm notification. Never
                # resurrected by auto-repair.
                gt = A.bk_get(state_conn, src_conn, ev["ulid"], "gtask_id")
                if gt and gt in deleted_task_ids:
                    res = A._run_verb(lambda: sdk_bridge.cancel_abandoned(
                        src_conn, ev["ulid"], "gtasks-owner-delete"))
                    if res in A.VERB_OK:
                        A.bk_set(state_conn, src_conn, ev["ulid"],
                                 "gtask_id", "")
                        A.bk_set(state_conn, src_conn, ev["ulid"],
                                 "gtask_etag", "")
                        state_conn.execute(
                            "DELETE FROM push_snapshot WHERE event_ulid=?",
                            (ev["ulid"],))
                        state_conn.commit()
                        notify_mod.notify(
                            state_conn, "task_deleted", ev["ulid"],
                            title=ev.get("title") or ev["ulid"])
                    owner_deleted.append(ev["ulid"])
                    continue
                missing.append(("tasks", ev["ulid"]))
                continue
            p_ev = A.tasks_projection_from_event(ev)
            p_it = A.tasks_projection_from_item(task_it)
            if p_ev != p_it:
                diffs = {k: (p_ev.get(k), p_it.get(k)) for k in p_ev
                         if p_ev.get(k) != p_it.get(k)}
                mismatched.append(("tasks", ev["ulid"], diffs))

    # Leftovers: ours-marked surface items with no live SoT row. In scoped
    # mode (grill-5 finding 7), out-of-scope leftovers are simply rows we
    # did not check this run -- not orphans.
    leftovers = ([("calendar", u) for u in cal_by_ulid]
                 + [("tasks", u) for u in task_by_ulid])
    if scope_ulids is not None:
        leftovers = [(sfc, u) for sfc, u in leftovers if u in scope_ulids]
    orphans = leftovers

    # g6-8: a ulid with a terminal-dead outbox row must NOT be re-enqueued
    # by repair (dead-revival loop). Reported once as its own class.
    dead_ulids = {r["event_ulid"] for r in state_conn.execute(
        "SELECT DISTINCT event_ulid FROM mirror_outbox WHERE dead=1"
    ).fetchall()}
    dead_held = [(sfc, u) for sfc, u in missing if u in dead_ulids]
    missing = [(sfc, u) for sfc, u in missing if u not in dead_ulids]
    repaired = 0
    if repair:
        for _surface, ulid in missing:
            if A.outbox_enqueue(state_conn, ulid):
                repaired += 1

    # DGN-302: enqueue a sync for each abandoned-gcal-drift row so the drain
    # pushes the cancel. Dead-row guard applies here too (no revival loop).
    abandoned_cancel_enqueued = 0
    if repair:
        for ulid in abandoned_gcal_drift:
            if ulid not in dead_ulids:
                if A.outbox_enqueue(state_conn, ulid):
                    abandoned_cancel_enqueued += 1

    retry = retry_failed(state_conn, src_conn)

    # owner_deleted is auto-handled (dec-011 applied + notified) -> it does
    # not require attention by itself.
    verdict = ("CLEAN" if not missing and not mismatched and not orphans
               and not abandoned_gcal_drift
               else "ATTENTION")
    summary = {
        "checked": checked,
        "missing": len(missing),
        "repaired_enqueued": repaired,
        "mismatched": len(mismatched),
        "orphans": len(orphans),
        "owner_deleted": len(owner_deleted),
        "dead_held": len(dead_held),
        "dead_held_detail": dead_held[:20],
        "expected_tombstones": len(expected_tombstones),
        "abandoned_gcal_drift": len(abandoned_gcal_drift),
        "abandoned_cancel_enqueued": abandoned_cancel_enqueued,
        "abandoned_gcal_drift_detail": abandoned_gcal_drift[:20],
        "foreign": {"calendar": foreign_cal, "tasks": foreign_task},
        "retry": retry,
        "verdict": verdict,
        "missing_detail": missing[:20],
        "mismatch_detail": mismatched[:20],
        "orphan_detail": orphans[:20],
        "owner_deleted_detail": owner_deleted[:20],
    }
    # g6-13 (Metal ruling): CLEAN = silent. The weekly report reaches the
    # owner ONLY when something needs attention. Korean only (finding 6).
    if verdict != "CLEAN":
        notify_mod.notify(
            state_conn, "reconcile_report", None, dedup=False,
            checked=checked, missing=len(missing), mismatch=len(mismatched),
            orphan=len(orphans), deleted_held=len(owner_deleted),
            verdict=mirror_i18n.t("mirror.reconcile_verdict_attention",
                                  TEXT["verdict_attention"]))
    return summary


if __name__ == "__main__":
    # DGN-364: target resolution goes through the single resolver -- no raw
    # state-key reads in entry points (the DGN-363 anti-pattern is dead).
    args = sys.argv[1:]
    if "--state" in args:
        A.STATE_DB_PATH = args[args.index("--state") + 1]
    state = A.open_state_db()
    src = A.get_src_conn()
    targets = A.get_mirror_targets(state)
    summary = run_reconcile(state, src, targets["cal_ids"],
                            targets["checklist_id"],
                            repair="--repair" in args)
    if "--drain" in args:
        summary["drain"] = A.outbox_drain(state, src, targets["cal_ids"],
                                          targets["checklist_id"])
    for k, v in summary.items():
        print("%s: %s" % (k, v))
    src.close()
    state.close()
