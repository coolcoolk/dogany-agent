#!/usr/bin/env python3
# test-lane: fast
# test-new-component: Far-path urgency routing through engine.submit is a separate integration boundary from handoff storage and delivery.
# test-component: handoff-far-urgency
# test-tier: 0
# test-prereq: tmp-files
"""M1-4 G9 -- far-path urgency gate (handoff submit path).

Promises (one per test):
  route_far_direct_only_for_closed_enum   outage/security/hard_line -> leader_direct
  route_far_free_text_goes_to_inbox       free text / wrong case / non-str -> inbox
  engine_submit_urgent_reaches_leader     a real enum value lands in the LEADER mailbox only
  engine_submit_fake_urgent_lands_inbox   unknown value lands in the far inbox, never the leader
  engine_submit_no_urgency_is_unchanged   no urgency -> far inbox, no route fields
  engine_submit_ignores_caller_urgency    payload urgency cannot smuggle a direct route

Run: python3 routines/tests/test-m14-g9-far-urgency-gate.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib", "handoff")
sys.path.insert(0, LIB)

import engine    # noqa: E402
import handoff   # noqa: E402


class _Boxes(object):
    def __enter__(self):
        for k in list(os.environ):
            if k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
                os.environ.pop(k)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DOGANY_HANDOFF_ROOT"] = self.tmp.name
        for slug in ("far", "leader"):
            for d in ("inbox", "done", "dead", "tmp"):
                os.makedirs(os.path.join(self.tmp.name, slug, d))
        return self

    def count(self, slug):
        return len(os.listdir(handoff.mailbox_inbox(slug)))

    def __exit__(self, *exc):
        os.environ.pop("DOGANY_HANDOFF_ROOT", None)
        self.tmp.cleanup()


def _submit(urgency, payload=None):
    return engine.submit("far", "decision.notice", payload=payload,
                         from_slug="pm", urgency=urgency,
                         leader_slug="leader")


def test_route_far_direct_only_for_closed_enum():
    for v in ("outage", "security", "hard_line"):
        assert handoff.route_far(v)[0] == handoff.ROUTE_DIRECT


def test_route_far_free_text_goes_to_inbox():
    for v in (None, "urgent", "OUTAGE", "outage ", "outage,security",
              "", 1, True, ["outage"], "pm_feels_urgent"):
        assert handoff.route_far(v)[0] == handoff.ROUTE_INBOX, v


def test_engine_submit_urgent_reaches_leader():
    with _Boxes() as b:
        for v in ("outage", "security", "hard_line"):
            _submit(v)
        assert b.count("leader") == 3 and b.count("far") == 0


def test_engine_submit_fake_urgent_lands_inbox():
    with _Boxes() as b:
        _submit("urgent")
        _submit("very important, owner waiting")
        assert b.count("far") == 2 and b.count("leader") == 0


def test_engine_submit_no_urgency_is_unchanged():
    with _Boxes() as b:
        path = engine.submit("far", "decision.notice", from_slug="pm")
        assert b.count("far") == 1 and b.count("leader") == 0
        assert "route" not in open(path).read()


def test_engine_submit_ignores_caller_urgency():
    with _Boxes() as b:
        path = _submit("urgent", payload={"urgency": "outage",
                                          "route": "leader_direct"})
        assert b.count("far") == 1 and b.count("leader") == 0
        meta, _ = handoff.parse_message(open(path).read())
        assert "urgency" not in meta["payload"]
        assert meta["payload"]["route"] == "inbox"


def _main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:   # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(exc))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_main())
