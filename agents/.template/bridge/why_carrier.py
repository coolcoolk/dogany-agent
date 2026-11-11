"""FDGN-594 live carrier. Semantic intake stays in the common core writer.

Only authenticated ingress, actual tool arguments and Telegram send receipts
cross this adapter. No keyword approval or second work classifier lives here.
"""
import json
from pathlib import Path
import sys
import time


def core(root):
    lib = Path(root) / "routines/lib"
    if not lib.is_dir():
        return None
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    from conf_reader import conf_get
    if conf_get("WHY_NET", str(Path(root) / "config/agent.conf")).lower() != "on":
        return None
    from why_net import runtime
    return runtime


def capture(bot, message):
    """Capture arrival before debounce/queue waits; bounded, process-local."""
    orders = getattr(bot, "_why_owner_arrivals", {})
    key = (message.chat_id, message.message_id)
    orders.setdefault(key, time.time_ns() // 1000)
    while len(orders) > 256:
        orders.pop(next(iter(orders)))
    bot._why_owner_arrivals = orders


def custody(bot, message):
    reply = bot._reply_to_info(message)
    return dict(received_order=getattr(bot, "_why_owner_arrivals", {}).pop(
        (message.chat_id, message.message_id), 0),
        reply_to_carrier_id=("telegram:%s:%s" % (message.chat_id, reply["message_id"])
            if reply and reply.get("from_bot") else None))


def publish_turn(root, data):
    """Retired owner publication hook; stored Why ledgers remain readable."""
    return {}


def publish_tool(root, data):
    """Retired tool gate. Missing or pending Why evidence never denies work."""
    return {}


def deny():
    """Compatibility for older adapters; Why no longer grants permissions."""
    return {}


def pending_readback(root, session, request_id):
    """Stored readbacks remain readable, but never hold ordinary delivery."""
    return False


async def deliver_readback(root, session, transport, chat_id):
    """Send exact recorded projection once; ACK only the real send result."""
    runtime = core(root)
    if runtime is None or not session:
        return False
    from why_net.delivery_guard import validate_delivery
    from why_net.intake import digest
    path = runtime.snapshot_path(root, session)
    if not path.exists():
        return False
    snapshot = json.loads(path.read_text())
    expected = snapshot.get("readback")
    if not expected:
        return False
    if runtime.enrolment(root).get("chat_id") != chat_id:
        raise ValueError("readback-chat-mismatch")
    envelope = snapshot["delivery_envelope"]
    frame, resolver = runtime.load_runtime(root, dict(session_id=session), envelope=envelope)
    body = snapshot["intake_projection"]
    if validate_delivery(frame, envelope, body, resolver)["decision"] != "allow":
        raise ValueError("readback-held")
    receipt = runtime.state_dir(root) / (digest(expected["ref"]) + ".receipt.json")
    if receipt.exists():
        # Approved turns resume their ordinary final delivery. Pending retries
        # do not resend an already acknowledged read-back.
        return any(not resolver.read_work_record(u).get("direction_approval") for u in frame["units"])
    sent = await transport.send_message(chat_id=chat_id, text=body, parse_mode=None)
    mid = getattr(sent, "message_id", None)
    if (not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0
            or getattr(sent, "chat_id", None) != chat_id):
        raise ValueError("readback-send-uncertain")
    if not runtime.record_readback_ack(root, session, body,
            "telegram:%s:%s" % (chat_id, mid), turn_ended=True):
        raise ValueError("readback-ack-held")
    return True
