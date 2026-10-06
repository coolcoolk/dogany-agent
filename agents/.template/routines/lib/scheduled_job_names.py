#!/usr/bin/env python3
"""Owner-facing scheduled-job names (DGN-1747).

This is the sole data seat for names shown in scheduled-job alarms.  Plists
keep their stable machine keys; callers resolve those keys here at render time.
"""

# Approved by the owner, 2026-09-27.
JOB_DISPLAY_NAMES = {
    "generic-brief-morning": "모닝 브리핑",
    "generic-brief-retro": "하루 회고",
    "generic-brief-weekly": "주간 회고",
    "mirror-poll": "캘린더 동기화",
    "mirror-reconcile": "캘린더 대조",
    "routine-roller": "루틴 일정 갱신",
    "classify-inbox": "기억 분류",
    "memory-consolidate": "기억 정리",
    "lifekit-backup": "라이프킷 백업",
    "cleanup-files": "임시 파일 정리",
    "health-observer": "상태 점검",
    "pack-lag": "팩 버전 점검",
}

# launchd labels retain historical schedule suffixes while their plist
# --friendly-name values are stable machine keys.  Keep that translation next
# to the owner vocabulary, rather than teaching each alarm surface about it.
LABEL_SUFFIX_KEYS = {
    "backup-data": "lifekit-backup",
    "classify-inbox-0500": "classify-inbox",
    "consolidate-0430": "memory-consolidate",
    # DGN-1582 (3): the dawn memory queue wraps the same two jobs under its
    # own labels (<ns>.<slug>.dmq-consolidate / .dmq-classify); without these
    # rows its alarms named the job only as the generic fallback.
    "dmq-consolidate": "memory-consolidate",
    "dmq-classify": "classify-inbox",
    "pack-lag-0317": "pack-lag",
}

UNKNOWN_JOB_DISPLAY = "예약"  # every template appends " 작업" -> "예약 작업"


def display_name(machine_key="", label=""):
    """Return approved Korean copy; never expose an unknown slug to an owner."""
    key = machine_key or ""
    if not key and label:
        suffix = label.rsplit(".", 1)[-1]
        key = LABEL_SUFFIX_KEYS.get(suffix, suffix)
    return JOB_DISPLAY_NAMES.get(key, UNKNOWN_JOB_DISPLAY)


if __name__ == "__main__":
    import sys
    print(display_name(*(sys.argv[1:3])))
