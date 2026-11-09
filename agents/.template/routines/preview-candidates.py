#!/usr/bin/env python3
"""Deliver an ordered preview set; release the existing OPTIONS text only on receipts.

The live turn publishes stdout through its normal interim/final reply path.
Candidate identities, order and instructions belong in a separate announcement.
This command never publishes the pick itself or calls product answer handling.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.inert_preview import prepare
from bridge.live_surface import Refusal, load_request, positive, require


def deliver_plan(root, plan, send=None):
    from bridge.options import strip_consumed_options
    require(isinstance(plan, dict) and frozenset(plan) == {"v", "candidates", "pick"} and
            type(plan["v"]) is int and plan["v"] == 1 and
            isinstance(plan["candidates"], list) and 2 <= len(plan["candidates"]) <= 7 and
            isinstance(plan["pick"], str), 5, "invalid preview plan")
    _, choices = strip_consumed_options(plan["pick"])
    require("[[OPTIONS" in plan["pick"] and len(choices) == len(plan["candidates"]),
            5, "separate pick must contain one OPTIONS choice per candidate")
    ids = []
    # Preflight the entire set before sending its first screen.
    for candidate in plan["candidates"]:
        require(isinstance(candidate, dict) and frozenset(candidate) ==
                {"v", "candidate_id", "text", "buttons"} and
                type(candidate["v"]) is int and candidate["v"] == 1, 5, "invalid producer export")
        prepare(root, candidate["candidate_id"], candidate["text"], candidate["buttons"])
        require(candidate["candidate_id"] not in ids, 5, "duplicate candidate id")
        ids.append(candidate["candidate_id"])
    if send is None:
        def send(candidate):
            process = subprocess.run(["bash", str(Path(root) / "routines/push.sh"),
                "--audience", "owner", "--preview", candidate["candidate_id"],
                "--text", candidate["text"], "--buttons-json", json.dumps(candidate["buttons"])],
                stdout=subprocess.PIPE, text=True, timeout=90)
            require(process.returncode == 0, process.returncode or 5, "candidate delivery not confirmed")
            return json.loads(process.stdout)
    receipts, chat = [], None
    for candidate in plan["candidates"]:
        reply = send(candidate)
        require(isinstance(reply, dict) and reply.get("ok") is True and reply.get("delivered") is True and
                reply.get("candidate_id") == candidate["candidate_id"] and
                positive(reply.get("message_id")) and type(reply.get("chat_id")) is int and
                isinstance(reply.get("receipt"), dict), 5, "candidate lacks delivery receipt")
        results = reply["receipt"].get("results", [])
        require(reply["receipt"].get("complete") is True and len(results) == 1 and
                results[0].get("result") == "ok" and
                results[0].get("message_id") == reply["message_id"], 5, "candidate receipt incomplete")
        require(chat is None or chat == reply["chat_id"], 5, "candidate destination changed")
        chat = reply["chat_id"]
        require(reply["message_id"] not in receipts, 5, "candidate message id reused")
        receipts.append(reply["message_id"])
    # Preserve the authored menu verbatim: promotion/selection/retirement stay
    # with the existing bridge OPTIONS implementation.
    return plan["pick"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        with args.plan.open() as stream:
            plan = load_request(stream)
        pick = deliver_plan(args.root, plan)
    except (Refusal, OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
        print("Preview set withheld: " + str(error), file=sys.stderr)
        return getattr(error, "rc", 5)
    print(pick)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
