#!/usr/bin/env python3
"""Record native-bound ticketless intake through its owning writer.

Example request keys: current={session,ledger_id,message_uuid},
unit={work_key,scope_revision,scope_digest,targets}, expected_revision,
symptom, problem, why, direction; optional symptom_source, why_source,
approval. Source locators always identify actual bound native owner turns.
Ticket provider integration and carrier send/ACK belong to their owning
executors; this command cannot claim either of them happened.
"""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from why_net.runtime import instance_root
from why_net.runtime_intake import write_intake


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="One JSON intake request; no proof facts or paths")
    args = parser.parse_args()
    try:
        result = write_intake(instance_root(Path.cwd()), **json.loads(args.request))
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Native owner text and held body bytes must not leak through errors.
        print(json.dumps(dict(error=type(exc).__name__, reason=str(exc), state="intake-held")))
        return 3


if __name__ == "__main__":
    sys.exit(main())
