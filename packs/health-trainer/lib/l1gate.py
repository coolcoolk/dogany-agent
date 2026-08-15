#!/usr/bin/env python3
"""L1 (Ag event ledger) read gate -- DGN-238 v3 section 1.3.

Physical contract:
  - path + expected version are injected via Warg config/agent.conf:
        L1_DB=/opt/dogany/agents/ag/database/lifekit.db
        L1_EXPECTED_USER_VERSION=5
  - open = plain sqlite open (WAL readers need -shm, so NO ro-URI /
    immutable), busy_timeout=5000, transaction-snapshot reads.
  - write-ban belt: PRAGMA query_only=ON right after open (connection
    property -- zero Ag contact; even a code bug cannot write).
  - version gate is FAIL-CLOSED: missing key OR mismatch -> no
    connection; L1-dependent features skip gracefully, self-DB features
    are unaffected; a Metal-channel flag fires (once per day, state-file
    dedup) on BOTH the missing-key and mismatch cases.

English/ASCII only. No hidden paths: root/conf are explicit parameters.
"""

import datetime
import os
import sqlite3


class L1GateClosed(Exception):
    """L1 unavailable (version skew / config missing). Carries the reason;
    callers degrade to self-data responses, never full-down."""


def read_conf(conf_path):
    """Parse KEY=VALUE lines (shell-style, '#' comments)."""
    conf = {}
    if not os.path.isfile(conf_path):
        return conf
    with open(conf_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            conf[k.strip()] = v.strip().strip('"')
    return conf


def open_l1(conf, flag_dir=None, now=None):
    """Open the shared-read L1 connection or raise L1GateClosed.

    conf: dict (from read_conf). flag_dir: where the daily-dedup Metal
    flag state lives (e.g. <warg_root>/.telegram_bot/state)."""
    db = conf.get("L1_DB")
    expected = conf.get("L1_EXPECTED_USER_VERSION")
    if not db or not expected:
        _metal_flag(flag_dir, "l1-gate: config key missing "
                    "(L1_DB=%r, L1_EXPECTED_USER_VERSION=%r)"
                    % (db, expected), now)
        raise L1GateClosed("config key missing -- fail-closed")
    if not os.path.isfile(db):
        _metal_flag(flag_dir, "l1-gate: L1 db not found at %s" % db, now)
        raise L1GateClosed("L1 db missing -- fail-closed")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA query_only = ON;")   # write-ban belt, conn-local
        v = conn.execute("PRAGMA user_version;").fetchone()[0]
    except sqlite3.OperationalError as e:
        # e.g. an Ag migration holds an EXCLUSIVE lock past busy_timeout
        # (grill-final MINOR-1 / G4): normalize to the gate exception so
        # callers catching L1GateClosed degrade instead of crashing.
        conn.close()
        _metal_flag(flag_dir, "l1-gate: db locked/unreadable (%s)" % e, now)
        raise L1GateClosed("L1 db locked/unreadable (%s) -- fail-closed" % e)
    if v != int(expected):
        conn.close()
        _metal_flag(flag_dir, "l1-gate: version skew (db=%d expected=%s) -- "
                    "propagation-editor lockstep missed (OQ-B)"
                    % (v, expected), now)
        raise L1GateClosed("user_version %d != expected %s -- fail-closed"
                           % (v, expected))
    return conn


def _metal_flag(flag_dir, reason, now=None):
    """Metal-channel flag, once per day (state-file dedup). The physical
    transport (push to the Metal bot) is wired at mint (MANIFEST step);
    in the sandbox this appends to the flag log only -- never silent."""
    if not flag_dir:
        return False
    os.makedirs(flag_dir, exist_ok=True)
    day = (now or datetime.datetime.now(datetime.timezone.utc)
           .strftime("%Y-%m-%dT%H:%M:%SZ"))[:10]
    marker = os.path.join(flag_dir, "l1-gate-flag.%s" % day.replace("-", ""))
    if os.path.exists(marker):
        return False
    with open(marker, "w") as f:
        f.write(reason + "\n")
    with open(os.path.join(flag_dir, "l1-gate-flag.log"), "a") as f:
        f.write("%s %s\n" % (day, reason))
    return True
