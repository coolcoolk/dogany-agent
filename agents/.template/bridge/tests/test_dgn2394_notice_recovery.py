# test-component: bridge.live_surface notice recovery and expiry
# test-tier: 0
# test-prereq: tmp-files
import asyncio
import copy
import hashlib
import json

from bridge.live_surface import Coordinator, atomic


def run(value):
    return asyncio.run(value)


def fixture(tmp_path, replies=()):
    sent = []
    replies = iter(replies)
    now = [1000]

    async def transport(payload):
        sent.append(copy.deepcopy(payload))
        reply = next(replies, None)
        if isinstance(reply, Exception):
            raise reply
        return reply or dict(ok=True, result=dict(message_id=100 + len(sent), chat=dict(id=42)))

    return Coordinator(tmp_path, transport=transport, clock=lambda: now[0],
                       notification_policy=lambda _: True), sent, now


def admit(coordinator, count, producer="fixture"):
    for index in range(1, count + 1):
        run(coordinator.enqueue_notice(42, producer, "event-" + str(index),
                                       dict(text="notice-" + str(index), buttons=[])))


def test_uncertain_notice_is_quarantined_and_fifo_continues(tmp_path):
    replies = [dict(ok=True, result=dict(message_id=101, chat=dict(id=42))),
               dict(ok=True, result=dict(message_id=102, chat=dict(id=42))), RuntimeError("network")]
    coordinator, sent, _ = fixture(tmp_path, replies)
    admit(coordinator, 15)
    assert run(coordinator.flush_notices(42)) == dict(ok=True)
    notices = sorted(coordinator.control(42)["notices"].values(), key=lambda item: item["admission_order"])
    assert [item["status"] for item in notices] == ["ok", "ok", "quarantined"] + ["ok"] * 12
    assert [item["frame"]["text"] for item in sent] == ["notice-" + str(i) for i in range(1, 16)]
    assert len([item for item in sent if item["frame"]["text"] == "notice-3"]) == 1


def test_expired_pre_io_claim_retries_once(tmp_path):
    coordinator, sent, now = fixture(tmp_path)
    admit(coordinator, 1)
    key, notice = next(iter(coordinator.control(42)["notices"].items()))
    state, plan = coordinator._maintenance_plan(42, None, key, notice["intended"]["payload"], "system")
    path = coordinator.plan_path(42, state["epoch"], plan["request"]["op"])
    stage = plan["stages"][0]
    stage.update(status="reserved", seq=1)
    stage["attempts"].append(dict(seq=1, started_at_ms=1000, target=None,
                                  lease_fence=plan["coordinator_fence"], io_authorized=False))
    atomic(path, plan)
    control = coordinator.control(42)
    control["notices"][key].update(status="dispatching", epoch=0)
    coordinator.save_control(42, control)
    now[0] += Coordinator.NOTICE_CLAIM_TIMEOUT_MS
    assert run(coordinator.flush_notices(42)) == dict(ok=True)
    assert len(sent) == 1
    attempts = coordinator.plans(42)[0][1]["stages"][0]["attempts"]
    assert [attempt["status"] for attempt in attempts] == ["abandoned_before_io", "ok"]


def test_expire_notices_is_snapshot_bound_idempotent_and_transport_free(tmp_path):
    coordinator, sent, _ = fixture(tmp_path)
    admit(coordinator, 13)
    journal = coordinator.base / "42.transport.json"
    snapshot = hashlib.sha256(journal.read_bytes()).hexdigest()
    args = (42, "fixture", "system", (1, 13), 13, snapshot, "operator expiry")
    dry = run(coordinator.expire_notices(*args, False))
    assert dry["matched"] == 13 and dry["changed"] == 0 and not dry["apply"]
    applied = run(coordinator.expire_notices(*args, True))
    assert applied["matched"] == applied["changed"] == 13
    assert sent == []
    current = hashlib.sha256(journal.read_bytes()).hexdigest()
    again = run(coordinator.expire_notices(42, "fixture", "system", (1, 13), 13,
                                             current, "operator expiry", True))
    assert again["matched"] == 13 and again["changed"] == 0
    assert all(item["status"] == "dropped" for item in coordinator.control(42)["notices"].values())


def test_rate_limit_still_stops_the_chat_fifo(tmp_path):
    coordinator, sent, _ = fixture(tmp_path, [dict(ok=False, error_code=429,
                                                       description="slow down",
                                                       parameters=dict(retry_after=10))])
    admit(coordinator, 2)
    result = run(coordinator.flush_notices(42))
    assert result["pending"] and result["rc"] == 24
    assert len(sent) == 1
    statuses = [item["status"] for item in sorted(coordinator.control(42)["notices"].values(),
                                                  key=lambda notice: notice["admission_order"])]
    assert statuses == ["dispatching", "pending"]


def test_expire_notices_refuses_a_delivered_notice(tmp_path):
    coordinator, sent, _ = fixture(tmp_path)
    admit(coordinator, 3)
    journal = coordinator.base / "42.transport.json"
    control = coordinator.control(42)
    first = min(control["notices"], key=lambda k: control["notices"][k].get("admission_order", 0))
    control["notices"][first]["status"] = "ok"
    coordinator.save_control(42, control)
    snapshot = hashlib.sha256(journal.read_bytes()).hexdigest()
    try:
        run(coordinator.expire_notices(42, "fixture", "system", (1, 3), 3, snapshot, "operator expiry", True))
    except Exception:
        pass
    else:
        raise AssertionError("expiry of a delivered notice must be refused")
    statuses = sorted(item["status"] for item in coordinator.control(42)["notices"].values())
    assert statuses == ["ok", "pending", "pending"]
    assert sent == []
