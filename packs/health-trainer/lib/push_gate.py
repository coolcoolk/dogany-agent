#!/usr/bin/env python3
"""Push gate -- DGN-238 v3 section 7(a). NEW code (the existing budget.py
is the question-budget tool, 191 lines, zero push-gate function -- this is
not a port).

Hard gate at the single physical unsolicited-contact path (Warg push.sh
wrapper): --trigger <id> is REQUIRED, must match the config/triggers.yaml
whitelist, and a per-trigger daily counter (state file) is enforced.
Unregistered trigger or missing argument = refusal. There is NO
self-declared solicited/unsolicited flag: every caller is a script and
the trigger id is baked into code (DGN-071 distrust principle).
Registry additions = user approval (V3).

triggers.yaml is deliberately a flat `id: cap` map (valid YAML, parsed
here with stdlib -- no PyYAML in the launchd context). cap = integer per
day, or 'unlimited'.

English/ASCII only.
"""

import datetime
import json
import os


def load_registry(path):
    """Parse the flat triggers.yaml -> {trigger_id: cap_int_or_None}."""
    reg = {}
    if not os.path.isfile(path):
        return reg
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line or line.endswith(":"):
                continue
            key, _, val = line.partition(":")
            val = val.strip()
            reg[key.strip()] = None if val == "unlimited" else int(val)
    return reg


def check_and_count(state_dir, registry, trigger, now=None):
    """Gate decision. Returns (allowed: bool, reason: str). Counting is
    atomic enough for the single-host launchd context (read-modify-write
    of a per-day json; callers are serialized scripts)."""
    if not trigger:
        return False, "refused: --trigger <id> is required"
    if trigger not in registry:
        return False, ("refused: trigger %r not in whitelist "
                       "(registry additions = user approval)" % trigger)
    cap = registry[trigger]
    day = (now or datetime.datetime.now(datetime.timezone.utc)
           .strftime("%Y-%m-%dT%H:%M:%SZ"))[:10]
    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "push-gate.%s.json"
                              % day.replace("-", ""))
    counts = {}
    if os.path.isfile(state_path):
        with open(state_path) as f:
            counts = json.load(f)
    used = int(counts.get(trigger, 0))
    if cap is not None and used >= cap:
        return False, ("refused: trigger %r daily cap %d exhausted "
                       "(used=%d)" % (trigger, cap, used))
    counts[trigger] = used + 1
    tmp = state_path + ".part"
    with open(tmp, "w") as f:
        json.dump(counts, f)
    os.replace(tmp, state_path)
    return True, "allowed (%s used %d/%s)" % (
        trigger, used + 1, "inf" if cap is None else cap)
