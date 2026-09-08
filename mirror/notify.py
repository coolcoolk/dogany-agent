"""
DGN-180 notification interface (grill-3 finding 8 / D180-2).
SANDBOX MODE: writes to notify_outbox table instead of pushing to Telegram.
Live cutover swaps deliver() to the routines/push.sh pattern; the interface
and the user-facing templates below are FINAL.

Templates are Korean by requirement (user-facing message content, not code).
Code itself is English/ASCII.
"""

from datetime import datetime, timezone

import mirror_i18n

# Final user-facing templates. The ko literals below are the FALLBACK (and the
# byte-identical zero-delta default when no locale file carries the key). At
# send time each kind is resolved through mirror_i18n by AGENT_LANG under the
# i18n key 'mirror.<kind>' (config/i18n/<lang>.json); the ko value here is used
# verbatim when the key/file is absent (DGN-268 S4).
TEMPLATES = {
    "outbox_exhausted": (
        u"동기화 보류: '{title}' 항목이 "
        u"반복 실패로 대기 상태입니다. "
        u"확인이 필요합니다."),
    "circuit_breaker": (
        u"캘린더에서 취소 {count}건이 "
        u"한꺼번에 감지되어 자동 반영을 "
        u"중단했습니다. 캘린더 상태 "
        u"확인이 필요합니다."),
    "inbound_cancel": (
        u"캘린더에서 '{title}' 일정이 "
        u"삭제되어 취소 처리했습니다. "
        u"되돌리시려면 알려주세요."),
    "task_deleted": (
        u"할 일 '{title}' 항목이 삭제되어 "
        u"취소 처리했습니다. 되돌리시려면 "
        u"알려주세요."),
    "inbound_adopted": (
        u"캘린더에 직접 등록하신 '{title}' "
        u"일정을 가져와 등록했습니다."),
    "recurring_skipped": (
        u"반복 일정 '{title}'은 아직 자동 "
        u"연동 대상이 아니라 캘린더에만 "
        u"둡니다. (반복 일정 지원 예정)"),
    "outbox_exhausted_agg": (
        u"동기화 보류 {count}건: 반복 실패로 "
        u"대기 중인 항목이 여러 건입니다. "
        u"확인이 필요합니다."),
    "overlap_notice": (
        u"'{title}' 일정을 적용한 자리에 "
        u"기존 일정과 겹침이 있습니다: "
        u"{detail}"),
    "repeated_failure": (
        u"동기화 실패 반복: '{title}' 항목이 "
        u"재시도 한도에 도달했습니다. "
        u"수동 확인이 필요합니다."),
    "reconcile_report": (
        u"주간 동기화 점검: 총 {checked}건 "
        u"점검, 누락 {missing}건 재전송, "
        u"불일치 {mismatch}건, 관리 밖 항목 "
        u"{orphan}건, 삭제 감지 보류 "
        u"{deleted_held}건. {verdict}"),
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def notify(state_conn, kind, event_ulid=None, dedup=True, **kwargs):
    """Queue a user notification. SANDBOX: insert into notify_outbox
    (delivered=0). Dedup: skip when an undelivered row for the same
    (kind, event_ulid) already exists (poll-repeat spam guard).
    Returns True if queued, False if deduped."""
    if kind not in TEMPLATES:
        raise ValueError("unknown notify kind: %r" % kind)
    # i18n resolve: locale bundle by AGENT_LANG, else the ko literal fallback
    # (zero-delta when no locale file carries 'mirror.<kind>').
    message = mirror_i18n.t("mirror.%s" % kind, TEMPLATES[kind], **kwargs)
    if dedup:
        row = state_conn.execute(
            "SELECT 1 FROM notify_outbox WHERE kind=? AND delivered=0 AND "
            "COALESCE(event_ulid,'')=COALESCE(?,'')",
            (kind, event_ulid)).fetchone()
        if row:
            return False
    state_conn.execute(
        "INSERT INTO notify_outbox(ts, kind, event_ulid, message, delivered) "
        "VALUES(?,?,?,?,0)", (_now(), kind, event_ulid, message))
    state_conn.commit()
    return True


# Operator-facing report kinds (DGN-306 fold, DGN-364 2.8; owner directive
# 2026-07-15): system/ops reports route to the OPERATOR channel, never the
# persona channel -- end users must not receive sync-ops noise. The operator
# channel is config-derived (conf key MIRROR_OPS_ENV_FILE); unset or missing
# file -> ALL kinds deliver to the persona channel (zero-delta for instances
# without an operator channel).
OPS_KINDS = frozenset(["reconcile_report"])

# Ops prefix: i18n key mirror.ops.prefix parameterized with the agent label.
# The label SOURCE is the S1 conf key DOGANY_AGENT_LABEL (R2-2: the named
# parameter source -- no other conf key resolves the label). The ko fallback
# below is unicode-escaped (source stays ASCII, A7):
# "[<label> calendar sync] ".
_OPS_PREFIX_FALLBACK = u"[{label} \uce98\ub9b0\ub354 \ub3d9\uae30\ud654] "


def _notify_conf():
    """Minimal shell-style conf reader (DGN-364 2.8: must stay
    circular-import-free -- adapter imports notify, so notify never imports
    adapter). Same sources + precedence as adapter._load_conf:
    ../config/lifekit.conf first, then ../.instance.conf; earlier source
    wins; missing files are no-ops."""
    import os
    conf = {}
    base = os.path.dirname(os.path.abspath(__file__))
    for parts in (("..", "config", "lifekit.conf"), ("..", ".instance.conf")):
        path = os.path.normpath(os.path.join(base, *parts))
        try:
            with open(path, "r") as fh:
                lines = fh.readlines()
        except (OSError, IOError):
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if not key or key in conf:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            conf[key] = val
    return conf


def _ops_prefix(conf):
    """Resolved ops prefix: i18n mirror.ops.prefix with {label} from the
    DOGANY_AGENT_LABEL conf value ('agent' keeps the string well-formed on a
    bare checkout)."""
    label = conf.get("DOGANY_AGENT_LABEL") or "agent"
    return mirror_i18n.t("mirror.ops.prefix", _OPS_PREFIX_FALLBACK,
                         label=label)


def push_sh_deliver(message, kind=None):
    """PRODUCTION delivery (patch 03): route through the instance's own
    push.sh (self-locating: mirror/ -> ../routines/push.sh). Raises on
    non-zero exit so the row stays undelivered and retries next cycle.
    Ops-report kinds (OPS_KINDS) route to the operator channel via
    push.sh --env when MIRROR_OPS_ENV_FILE resolves to an existing file,
    with the agent-labeled ops prefix; otherwise fall back to the persona
    channel (report must not be lost)."""
    import os
    import subprocess
    push = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "routines", "push.sh"))
    cmd = [push, "--text", message]
    if kind in OPS_KINDS:
        conf = _notify_conf()
        ops_env = conf.get("MIRROR_OPS_ENV_FILE") or ""
        if ops_env and os.path.isfile(ops_env):
            cmd = [push, "--env", ops_env, "--text",
                   _ops_prefix(conf) + message]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("push.sh exit %d: %s" % (r.returncode,
                                                    r.stderr[:200]))


def deliver_pending(state_conn, deliver_fn=None):
    """Cutover seam: drain undelivered notifications through deliver_fn
    (live = routines/push.sh wrapper). SANDBOX: no-op unless a deliver_fn
    is supplied. DGN-306/DGN-364 2.8: the row's kind is passed to a 2-arg
    deliver_fn so ops kinds can route to the operator channel; a 1-arg
    deliver_fn stays backward compatible (sandbox unaffected)."""
    if deliver_fn is None:
        return 0
    import inspect
    try:
        multi_arg = len(inspect.signature(deliver_fn).parameters) >= 2
    except (TypeError, ValueError):
        multi_arg = False
    rows = state_conn.execute(
        "SELECT id, kind, message FROM notify_outbox WHERE delivered=0 ORDER BY id"
    ).fetchall()
    n = 0
    for r in rows:
        if multi_arg:
            deliver_fn(r["message"], r["kind"])
        else:
            deliver_fn(r["message"])
        # g10: commit PER ROW -- a failure at row k must not redeliver
        # rows 1..k-1 on the next cycle (duplicate flood guard).
        state_conn.execute(
            "UPDATE notify_outbox SET delivered=1 WHERE id=?", (r["id"],))
        state_conn.commit()
        n += 1
    return n
