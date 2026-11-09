"""Load the active kit's optional READABLE_FACTS conf declaration (DGN-2165).

KIT in config/agent.conf selects config/<kit>.conf. READABLE_FACTS is a
single-quoted JSON array of {kind, read_hint}; no commands are executed.
Domain nouns live in the kit i18n key owner_ask.fact.<kind>. The framework
owns request/private-clause phrase tables. Both ko/en are consulted so a
mixed-language reply receives the same check regardless of AGENT_LANG.
Missing/invalid entries or phrase tables are skipped independently; an
unconfigured instance keeps the original source-search gate behavior.
"""
import json
import os
import re

from conf_reader import conf_get
from i18n_lookup import merged_bundle


def load_readable_facts(root):
    kit = conf_get("KIT", os.path.join(root, "config", "agent.conf"))
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", kit or ""):
        return []
    raw = conf_get("READABLE_FACTS", os.path.join(root, "config", kit + ".conf"))
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    bundles = [merged_bundle(root, lang) for lang in ("ko", "en")]
    facts = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind, hint = entry.get("kind"), entry.get("read_hint")
        if (not isinstance(kind, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind)
                or not isinstance(hint, str) or not hint.strip()):
            continue
        phrases = []
        for bundle in bundles:
            table = {"surface": bundle.get("owner_ask.fact." + kind),
                     "request": bundle.get("owner_ask.fact_request"),
                     "private": bundle.get("owner_ask.fact_private")}
            try:
                if not all(isinstance(v, str) and v.strip()
                           for v in table.values()):
                    continue
                for pattern in table.values():
                    re.compile(pattern, re.IGNORECASE)
            except re.error:
                continue
            phrases.append(table)
        if phrases:
            facts.append({"kind": kind, "read_hint": hint.strip(),
                          "phrases": phrases})
    return facts
