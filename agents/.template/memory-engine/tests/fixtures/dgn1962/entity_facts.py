"""DGN-1962 fake entity-facts provider (fixture).

Implements the framework contract (memory.py: get_entity_facts) the way a
kit would: it matches names in the turn against its own records and returns
structured facts.  Records mirror the incident shape -- person 108 (the
mother) and person 117 (her daughter, born 2026-10-02) -- with fictional
names.  FAKE_MODE env switches failure shapes for the lane-isolation tests.
"""
import os
import time

PEOPLE = {
    108: {"name": "\uc774\uc740\uc194", "aliases": ["\uc740\uc194"],
          "birthday": "1991-04-17"},
    117: {"name": "\uc815\ub2e4\uc628", "born": "2026-10-02",
          "parent": 108, "relation": "daughter"},
}


def get_entity_facts(prompt, today):
    mode = os.environ.get("FAKE_MODE", "")
    if mode == "raise":
        raise RuntimeError("kit db locked")
    if mode == "hang":
        time.sleep(5)
    if mode == "garbage":
        return {"not": "a list"}
    out = []
    for pid, p in PEOPLE.items():
        if "parent" in p:
            continue
        if not any(n in prompt for n in [p["name"]] + p.get("aliases", [])):
            continue
        facts = [{"text": "birthday %s" % p["birthday"][5:],
                  "source": "person#%d" % pid, "topics": ["birthday"]}]
        for cid, c in PEOPLE.items():
            if c.get("parent") == pid:
                facts.append({
                    "text": "%s %s (person#%d) born %s"
                            % (c["relation"], c["name"], cid, c["born"]),
                    "date": c["born"], "as_of": "2026-10-03",
                    "source": "person#%d" % cid})
        out.append({"entity": p["name"], "aliases": p.get("aliases", []),
                    "facts": facts})
    return out
