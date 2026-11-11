#!/usr/bin/env python3
# test-lane: fast
# test-tier: 0
# test-component: retro slot and Why-net concept copy
# promise: ko concept copy retains Why while retro block and weekday contracts hold
"""DGN-1961 -- the framework daily retro slot (lib/retro_slot.py).

  RS1  off (default): nothing sent, the morning Why push untouched
  RS2  on + no content: nothing sent, the day is done (no resend)
  RS3  kit sections appear in order (registered and dropped-in files);
       local modules win over bundle duplicates; broken files never block others
  RS4  a deferred section: left out tonight, its own short message next
       morning (not before the morning time, once)
  RS5  the title carries the year: "2026-10-08(<Thu>)"
  RS6  weekly recap replaces the daily on Sunday; month end adds the
       monthly sections; the picture hook ships as a no-op
  RS7  on: the morning Why push is off (no double ask)
  RW1-RW7 (DGN-2028 AMEND-2 P3.4, replacing the interactive DGN-1961
       cases): the Why section is a view-only picture -- confirmed
       purposes, then unconfirmed assumptions; no buttons, no next item;
       placeholder copy never ships; readings hand out and settle nothing;
       rendering mutates no store file; a typed message after it binds
       nothing; an empty span sends no wrapper; open retro asks are closed
       once (retro-view-only)
  RC1  copy: one pending block, ko + en, original Why concept wording
  RC2  the CLI: off prints the off signal and sends nothing
  RM1  the morning brief nudge is due once; consume is idempotent; completed
       retros are suppressed and deferred sections remain independently due
  CW1  weekly cadence: nothing on the other days; exactly one message on
       the weekly day with all 7 days (kit days; the Why section is
       view-only since DGN-2028 AMEND-2: no walk, none settled); no resend
  CW2  unknown cadence / weekday -> daily / Sunday + a logged signal
  CW3  weekly: the morning defer and a custom weekday still apply
  CW4  weekly: a month end inside the week brings the monthly sections
  CP1  low activity while daily -> one proposal line + buttons (alone on
       an empty day); nothing before a full window; conf untouched
  CP2  a normal-activity fixture proposes nothing
  CP3  cooldown: not again inside the window, again after it
  CP4  the conf changes only on the tap (wrong message / other words /
       "keep" change nothing); then the slot runs weekly
  CP5  Why net off: a typed button label answers the proposal too
  CP6  view-only retro (DGN-2028 AMEND-2): no unanswered item, no skip
       proposal
  CP7  high activity while weekly -> proposes daily

Runs on the Why net rev 3 harness (test-why-net-grill-final.Base).
Synthetic data only.

Run: python3 routines/tests/test-retro-slot.py
"""

import datetime
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(ROUTINES)))
# Trim B moved developer-only receipt machinery out of the shipped payload.
# This repository harness still loads it while composing the historical base.
DEVKIT_LIB = os.path.join(REPO, "extracted-devkit", "payload", "routines", "bundle", "lib")
if os.path.isdir(DEVKIT_LIB):
    sys.path.append(DEVKIT_LIB)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GF = _load("_slot_grill_final", os.path.join(HERE, "test-why-net-grill-final.py"))
FU, AM = GF.FU, GF.AM

import retro_slot as RS  # noqa: E402
from why_net import ask, events, morning, retro, store, ups  # noqa: E402

T0, CHAT = AM.T0, AM.CHAT              # 2026-10-08 (Thu) 10:00 KST
NOT, STOP, ESC, COST, SPEED, STABLE = GF.NOT, GF.STOP, GF.ESC, GF.COST, GF.SPEED, GF.STABLE
DAY = 86400
H = 3600
EVENING = T0 + 11 * H                  # 21:00 KST
BEFORE = T0 + 10 * H                   # 20:00 KST
MORNING = T0 + 22 * H                  # 08:00 KST next day
EARLY = T0 + 21 * H                    # 07:00 KST next day
SUNDAY = EVENING + 3 * DAY             # 2026-10-11 (Sun)
MONTH_END = EVENING + 23 * DAY         # 2026-10-31 (Sat)


class Base(GF.Base):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.nexts = []   # DGN-2028 AMEND-2 P3.4: RETRO_NEXT is gone (view-only)

    def slot_on(self, extra=""):
        self.conf_on("RETRO_SLOT=on\n" + extra)

    def send(self, ctx, text, buttons):
        self.sent.append({"text": text, "buttons": buttons})
        return 0, 7000 + len(self.sent), CHAT

    def tick_slot(self, slot=None):
        out = io.StringIO()
        did = RS.tick(self.ctx, sender=self.send, slot=slot, out=out)
        return did, out.getvalue()

    def kit_slot(self):
        slot = RS.build(self.ctx, with_kits=False)
        return slot

    def tap(self, label, mid):
        self.tick()
        self.ups(label, source="callback", message_id=mid)

    def next_item(self):
        return RS.next_item(self.ctx, sender=self.send, out=io.StringIO())

    def labels(self, msg):
        return [r[0]["text"] for r in msg["buttons"] or []]

    def readings(self, n, work="DGN-1301"):
        """n corrections on `work`, each read by the agent as one Why."""
        w = self.why(COST, work=work)
        self.why(SPEED, work=work)
        self.why(STABLE, work=work)
        self.turn()
        for i in range(1, n + 1):
            self.tick(60)
            self.correction("cor-%04d" % i, "\uc2e0\ud638 %d \uc544\ub2c8\uc57c" % i, work)
            self.assertEqual(self.cli("read", "--signal", "cor-%04d" % i, "--why", w)[0], 0)
        return w




# ---------------------------------------------------------------------------
# the slot contract
# ---------------------------------------------------------------------------

class Contract(Base):
    def test_RS1_off_sends_nothing(self):
        self.readings(2)
        slot = self.kit_slot()
        slot.register("kit", 10, lambda day, ctx: "\uc624\ub298 \ud55c \uc77c")
        self.goto(EVENING)
        did, out = self.tick_slot(slot)
        self.assertEqual((did, self.sent), ([], []))
        self.assertIn("signal::RETRO_SLOT off", out)
        if os.path.exists(RS.state_path(self.ctx)):
            self.assertEqual(RS.load_state(self.ctx).get("days"), {})
        self.assertIsNone(self.next_item())
        # the shipped default is off
        self.assertEqual(RS.DEFAULTS["RETRO_SLOT"], "off")
        self.assertFalse(RS.slot_on(self.ctx))

    def test_RS2_on_with_no_content_sends_nothing_once(self):
        self.slot_on()
        slot = self.kit_slot()
        slot.register("kit", 10, lambda day, ctx: None)
        slot.register("blank", 20, lambda day, ctx: "  \n")
        self.goto(BEFORE)
        self.assertEqual(self.tick_slot(slot)[0], [])          # not time yet
        self.goto(EVENING)
        did, out = self.tick_slot(slot)
        self.assertEqual(self.sent, [])
        self.assertIn("sent=none", out)
        calls = []
        slot.register("late", 30, lambda day, ctx: calls.append(1) or "x")
        self.goto(EVENING + 600)
        self.assertEqual(self.tick_slot(slot)[0], [])          # the day is done
        self.assertEqual((self.sent, calls), ([], []))





    def test_RS3_kit_sections_in_order(self):
        self.slot_on()
        slot = self.kit_slot()
        slot.register("diet", 20, lambda day, ctx: "\uc2dd\ub2e8: \uc810\uc2ec \uac70\ub984")
        slot.register("sched", 10, lambda day, ctx: "\uc77c\uc815: 3\uac74 \ub05d")
        slot.register("boom", 15, lambda day, ctx: 1 / 0)
        with self.assertRaises(ValueError):
            slot.register("diet", 30, lambda day, ctx: "x")
        self.goto(EVENING)
        self.tick_slot(slot)
        [m] = self.sent
        body = m["text"].split("\n\n")
        self.assertEqual(body[1:], ["\uc77c\uc815: 3\uac74 \ub05d",
                                    "\uc2dd\ub2e8: \uc810\uc2ec \uac70\ub984"])
        self.assertIsNone(m["buttons"])
        # an instance-local file in routines/retro_sections/ is loaded
        d = os.path.join(self.root, RS.SECTIONS_DIR)
        os.makedirs(d)
        with open(os.path.join(d, "mood.py"), "w") as fh:
            fh.write("def render(day, ctx):\n    return 'mood ' + day.iso\n\n"
                     "def register(slot):\n    slot.register('mood', 5, render)\n")
        with open(os.path.join(d, "broken.py"), "w") as fh:
            fh.write("def register(slot):\n    raise RuntimeError('kit bug')\n")
        built = RS.build(self.ctx)
        self.assertEqual([s.name for s in built.ordered()],
                         ["mood", RS.WHY_SECTION, RS.LINK_REVIEW_SECTION])
        self.assertEqual(built.errors, ["broken:RuntimeError"])
        self.goto(EVENING + DAY)
        self.tick_slot()
        self.assertEqual(self.sent[-1]["text"].split("\n\n")[1], "mood 2026-10-09")

    def test_RS3_bundle_only_section_loads(self):
        self.slot_on()
        d = os.path.join(self.root, "routines", "bundle", "retro_sections")
        os.makedirs(d)
        with open(os.path.join(d, "mood.py"), "w") as fh:
            fh.write("def register(slot):\n"
                     "    slot.register('mood', 5, lambda day, ctx: 'bundle mood')\n")
        built = RS.build(self.ctx)
        self.assertEqual([s.name for s in built.ordered()],
                         ["mood", RS.WHY_SECTION, RS.LINK_REVIEW_SECTION])
        self.assertEqual(built.errors, [])
        self.goto(EVENING)
        self.tick_slot(built)
        self.assertEqual(self.sent[-1]["text"].split("\n\n")[1], "bundle mood")

    def test_RS3_local_module_wins_before_bundle_import(self):
        for directory, source in (
            (RS.SECTIONS_DIR,
             "def register(slot):\n"
             "    slot.register('mood', 5, lambda day, ctx: 'local mood')\n"),
            (os.path.join("routines", "bundle", "retro_sections"),
             "raise AssertionError('duplicate must not even be imported')\n"),
        ):
            d = os.path.join(self.root, directory)
            os.makedirs(d)
            with open(os.path.join(d, "mood.py"), "w") as fh:
                fh.write(source)
        built = RS.build(self.ctx)
        self.assertEqual([s.name for s in built.ordered()],
                         ["mood", RS.WHY_SECTION, RS.LINK_REVIEW_SECTION])
        self.assertEqual(built.sections["mood"].render(None, self.ctx), "local mood")
        self.assertEqual(built.errors, ["mood:duplicate-module:instance-local-wins"])

    def test_RS3_broken_bundle_modules_do_not_block_other_sections(self):
        d = os.path.join(self.root, "routines", "bundle", "retro_sections")
        os.makedirs(d)
        for name, source in (
            ("broken_import", "raise ImportError('kit dependency missing')\n"),
            ("broken_register", "def register(slot):\n    raise RuntimeError('kit bug')\n"),
            ("mood", "def register(slot):\n"
             "    slot.register('mood', 5, lambda day, ctx: 'bundle mood')\n"),
            ("_private", "raise AssertionError('private module must be ignored')\n"),
        ):
            with open(os.path.join(d, name + ".py"), "w") as fh:
                fh.write(source)
        built = RS.build(self.ctx)
        self.assertEqual([s.name for s in built.ordered()],
                         ["mood", RS.WHY_SECTION, RS.LINK_REVIEW_SECTION])
        self.assertEqual(built.errors,
                         ["broken_import:ImportError", "broken_register:RuntimeError"])

    def test_RS4_deferred_section_goes_out_next_morning(self):
        self.slot_on()
        slot = self.kit_slot()
        late = {"ready": False}

        def appts(day, ctx):
            if not late["ready"]:
                return RS.DEFER           # the last appointment ends at 23:00
            return "\ub9c8\uc9c0\ub9c9 \uc57d\uc18d: \uc798 \ub05d\ub0a8 (%s)" % day.iso

        slot.register("appts", 10, appts, defer=True)
        slot.register("notes", 20, lambda day, ctx: "\uba54\ubaa8 \ud558\ub098")
        # a section without the defer flag cannot defer itself
        slot.register("rude", 30, lambda day, ctx: RS.DEFER)
        self.goto(EVENING)
        self.tick_slot(slot)
        [ev] = self.sent
        self.assertNotIn("\uc57d\uc18d", ev["text"])
        self.assertIn("\uba54\ubaa8 \ud558\ub098", ev["text"])
        late["ready"] = True
        self.goto(EARLY)                          # 07:00: not yet
        self.tick_slot(slot)
        self.assertEqual(len(self.sent), 1)
        self.goto(MORNING)                        # 08:00: its own short message
        self.tick_slot(slot)
        self.assertEqual(len(self.sent), 2)
        mo = self.sent[1]["text"]
        title, body = mo.split("\n\n")
        self.assertEqual(title, "2026-10-08(\ubaa9) \ud68c\uace0 (\uc774\uc5b4\uc11c)")
        self.assertEqual(body, "\ub9c8\uc9c0\ub9c9 \uc57d\uc18d: \uc798 \ub05d\ub0a8 "
                         "(2026-10-08)")      # rendered for the deferred day
        self.goto(MORNING + 900)
        self.tick_slot(slot)
        self.assertEqual(len(self.sent), 2)       # once

    def test_RS5_title_carries_the_year(self):
        self.slot_on()
        slot = self.kit_slot()
        slot.register("kit", 10, lambda day, ctx: "x")
        self.goto(EVENING)
        self.tick_slot(slot)
        self.assertEqual(self.sent[0]["text"].split("\n")[0],
                         "2026-10-08(\ubaa9) \ud68c\uace0")
        with open(os.path.join(self.root, "config", "agent.conf")) as fh:
            conf = fh.read().replace("AGENT_LANG=ko", "AGENT_LANG=en")
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write(conf)
        self.assertEqual(RS.Day(datetime.date(2026, 10, 7)).title(self.ctx),
                         "2026-10-07(Wed) retro")

    def test_RS6_weekly_replaces_daily_and_monthly_adds(self):
        self.slot_on()
        slot = self.kit_slot()
        slot.register("daily", 10, lambda day, ctx: "D")
        slot.register("week", 20, lambda day, ctx: "W", cadences=("weekly",))
        slot.register("month", 30, lambda day, ctx: "M", cadences=("monthly",))
        self.goto(SUNDAY)
        self.tick_slot(slot)
        title, *body = self.sent[-1]["text"].split("\n\n")
        self.assertEqual(body, ["W"])                         # no daily on Sunday
        self.assertEqual(title, "2026-10-05(\uc6d4) ~ 2026-10-11(\uc77c) "
                                "\ud55c \uc8fc \ud68c\uace0")
        self.goto(MONTH_END)                                  # Sat 10-31
        did, _ = self.tick_slot(slot)
        self.assertEqual(self.sent[-1]["text"].split("\n\n")[1:], ["D", "M"])
        self.assertIsNone(did[-1]["plan"]["picture"])        # the no-op hook
        # the hook is pluggable; the plan carries what it returned, no image sent
        slot.set_picture(lambda day, ctx: "/tmp/pic-%s.png" % day.iso)
        plan = RS.compose(self.ctx, slot, RS.Day(datetime.date(2026, 11, 30)))
        self.assertIsNone(plan["picture"])
        plan = RS.compose(self.ctx, slot, RS.Day(datetime.date(2026, 11, 29)))
        self.assertIsNone(plan["picture"])

    def test_RS7_on_turns_the_morning_why_push_off(self):
        w = self.why(COST)
        ask.enqueue(self.ctx, "why", "ripe", w, [w], shape="confirm")
        self.conf_on("WHY_MORNING_FALLBACK=on\nRETRO_SLOT=on\n")
        sent, send = self.sender()
        self.goto(GF.AT_0700 + DAY)
        out = io.StringIO()
        morning.run(self.ctx, sender=send, out=out)
        self.assertEqual(sent, [])
        self.assertIn("reason=retro-slot", out.getvalue())

    def test_RM1_missed_retro_brief_contract(self):
        self.slot_on()
        yesterday = datetime.date(2026, 10, 8)
        today = yesterday + datetime.timedelta(days=1)
        st = {"days": {yesterday.isoformat(): {
            "evening": "sent", "deferred": ["appointments"]}}}
        RS.save_state(self.ctx, st, today)

        self.assertEqual(RS.morning_due(self.ctx, today),
                         ("어제 회고가 남아 있어요. 지금 할까요?", "회고 시작"))
        RS.morning_consume(self.ctx, today)
        RS.morning_consume(self.ctx, today)
        self.assertIsNone(RS.morning_due(self.ctx, today))
        rec = RS.load_state(self.ctx)["days"][yesterday.isoformat()]
        self.assertEqual(rec["morning_consumed"], today.isoformat())
        self.assertEqual(rec["deferred"], ["appointments"])

        rec.pop("morning_consumed")
        rec["done"] = True
        RS.save_state(self.ctx, {"days": {yesterday.isoformat(): rec}}, today)
        self.assertIsNone(RS.morning_due(self.ctx, today))


# ---------------------------------------------------------------------------
# the morning brief button
# ---------------------------------------------------------------------------

class MorningCatchup(Base):
    def prepare(self):
        self.slot_on()
        modules = os.path.join(self.root, RS.SECTIONS_DIR)
        os.makedirs(modules, exist_ok=True)
        with open(os.path.join(modules, "catchup.py"), "w") as fh:
            fh.write('def register(slot):\n'
                     '    slot.register("catchup", 10, render, next=next_item)\n'
                     'def render(day, ctx):\n'
                     '    return {"text": day.iso, "buttons": [[{"text": "answer"}]]}\n'
                     'def next_item(ctx):\n'
                     '    return {"text": "continue yesterday", "buttons": None}\n')
        self.goto(EVENING)
        self.tick_slot()
        self.goto(MORNING)
        self.today = RS._local(self.ctx.now).date()
        self.yesterday = self.today - datetime.timedelta(days=1)
        self.label = RS.t(self.ctx, "retro.slot.missed.start")
        RS.morning_consume(self.ctx, self.today)
        self.sent.clear()

    def start(self, sender=None):
        return RS.missed_answer(self.ctx, self.label, sender=sender or self.send,
                                out=io.StringIO())

    def test_morning_button_resumes_yesterday_once(self):
        self.prepare()
        self.assertTrue(self.start())
        self.assertEqual([m["text"] for m in self.sent], ["continue yesterday"])
        self.assertFalse(self.start())
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(RS.load_state(self.ctx)["days"][self.yesterday.isoformat()]
                        ["morning_started"])

    def test_failed_start_can_retry(self):
        self.prepare()
        self.assertFalse(self.start(sender=lambda *args: (1, None, None)))
        self.assertFalse(RS.load_state(self.ctx)["days"][self.yesterday.isoformat()]
                         .get("morning_started"))
        self.assertTrue(self.start())
        self.assertEqual(len(self.sent), 1)

    def test_off_or_quiet_does_not_use_the_button(self):
        self.prepare()
        self.conf_off()
        self.assertFalse(self.start())
        self.slot_on("RETRO_SLOT_QUIET=07:00-09:00\n")
        self.assertFalse(self.start())
        self.slot_on()
        self.assertTrue(self.start())
        self.assertEqual(len(self.sent), 1)

    def test_stale_button_does_not_start_an_old_retro(self):
        self.prepare()
        self.goto(MORNING + DAY)
        self.assertFalse(self.start())
        self.assertEqual(self.sent, [])

    def test_completed_retro_does_not_start(self):
        self.prepare()
        st = RS.load_state(self.ctx)
        st["days"][self.yesterday.isoformat()]["completed"] = True
        RS.save_state(self.ctx, st, self.today)
        self.assertFalse(self.start())
        self.assertEqual(self.sent, [])

    def test_view_only_retro_can_be_reopened_from_the_button(self):
        self.prepare()
        modules = os.path.join(self.root, RS.SECTIONS_DIR)
        with open(os.path.join(modules, "catchup.py"), "w") as fh:
            fh.write('def register(slot):\n'
                     '    slot.register("catchup", 10, lambda day, ctx: day.iso)\n')
        st = RS.load_state(self.ctx)
        st["days"][self.yesterday.isoformat()]["interactive"] = None
        RS.save_state(self.ctx, st, self.today)
        self.assertTrue(self.start())
        self.assertIn(self.yesterday.isoformat(), self.sent[0]["text"])
        self.assertIsNone(self.sent[0]["buttons"])
        self.assertFalse(self.start())

    def test_verified_button_with_outbound_prefix_and_why_off(self):
        self.prepare()
        self.conf_off("RETRO_SLOT=on\n")
        block = "[bridge:outbound-context]\nbrief\n[/bridge:outbound-context]\n"
        with patch.object(RS, "missed_answer", return_value=True) as start:
            self.ups(block + self.label)
            start.assert_called_once_with(self.ctx, self.label)

    def test_unverified_or_forwarded_turn_cannot_start(self):
        self.prepare()
        with patch.object(RS, "missed_answer") as start:
            ups.run(self.ctx, {"prompt": self.label, "session_id": "no-inbound"})
            self.ups(self.label, forwarded=True)
            start.assert_not_called()

    def test_cli_root_overrides_the_callers_bot_directory(self):
        self.prepare()
        st = RS.load_state(self.ctx)
        st["days"][self.yesterday.isoformat()].pop("morning_consumed")
        RS.save_state(self.ctx, st, self.today)
        foreign = os.path.join(self.root, "foreign-bot")
        os.makedirs(foreign)
        foreign_state = os.path.join(foreign, RS.STATE_FILE)
        sentinel = '{"days": {"2026-10-08": {"evening": "empty"}}}'
        with open(foreign_state, "w") as fh:
            fh.write(sentinel)
        foreign_conf = os.path.join(self.root, "foreign.conf")
        with open(foreign_conf, "w") as fh:
            fh.write("RETRO_SLOT=off\n")

        def cli(verb):
            return subprocess.run(
                [sys.executable, RS.__file__, verb, "--root", self.root,
                 "--today", self.today.isoformat()],
                env=dict(os.environ, DOGANY_BOT_DATA_DIR=foreign,
                         DOGANY_AGENT_CONF=foreign_conf),
                capture_output=True, text=True, timeout=10)

        due = cli("morning-due")
        self.assertEqual(due.returncode, 0, due.stderr)
        self.assertEqual(due.stdout.splitlines(), [
            "DUE", "LINE\t" + RS.t(self.ctx, "retro.slot.missed.morning"),
            "BUTTON\t" + self.label])
        self.assertNotIn("morning_consumed", RS.load_state(self.ctx)["days"]
                         [self.yesterday.isoformat()])
        self.assertEqual(cli("morning-consume").returncode, 0)
        self.assertEqual(cli("morning-consume").returncode, 0)
        self.assertEqual(cli("morning-due").stdout, "NO\n")
        self.assertEqual(RS.load_state(self.ctx)["days"][self.yesterday.isoformat()]
                         ["morning_consumed"], self.today.isoformat())
        with open(foreign_state) as fh:
            self.assertEqual(fh.read(), sentinel)


# ---------------------------------------------------------------------------
# the Why section
# ---------------------------------------------------------------------------

# DGN-2028 AMEND-2 P3.4: MD 14 Q4 A, pinned as PENDING_OWNER_CONFIRM -- the
# test lands it in the instance layer the way the owner's pick would; the
# framework catalog ships a placeholder that never reaches the owner.
Q4 = {"why.retro.view.head": "\uc774\ubc88 \uc8fc \ubc29\ud5a5",
      "why.retro.view.confirmed": "\ud655\uc778\ud55c \ubaa9\uc801: {text}",
      "why.retro.view.assumed": "\uc81c\uac00 \uc9d0\uc791\ud55c \uac83: {text}"}


class WhySection(Base):
    """DGN-2028 AMEND-2 P3.4 (merged design 9): the Why section is a
    view-only picture -- confirmed purposes, then the agent's unconfirmed
    assumptions.  It hands out no item, binds no answer, settles nothing
    and spends no budget.  (Replaces the DGN-1961 interactive RW1-RW7.)"""

    def q4(self):
        d = os.path.join(self.root, "config", "i18n.local")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ko.json"), "w", encoding="utf-8") as fh:
            json.dump(Q4, fh, ensure_ascii=False)

    def evening(self):
        self.slot_on()
        self.goto(EVENING)
        self.tick_slot()
        return self.sent[-1] if self.sent else None

    def assume(self, text):
        # v4 section 2 retires frame writes; section 3.3 retains history readers.
        self.turn()
        self.assert_retired("frame", "--mode", "proceed", "--assume", text)
        store.append_row(store.FRAMES, dict(ev="proceed", otl=self.last_otl(),
            assumption=text, at=store.iso_at(self.ctx.now), slot="why"))

    def content(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)                    # the owner tapped SPEED
        self.assume("\uc624\ub298 \uc6b4\ub3d9 \ubcf5\uad6c\uac00 \uba3c\uc800")
        return w

    def store_bytes(self):
        out = {}
        for name in (store.EVENTS, store.ASKS, store.PROBES, store.FRAMES):
            try:
                with open(store.path(name), "rb") as fh:
                    out[name] = fh.read()
            except FileNotFoundError:
                out[name] = None
        return out

    def test_RW1_view_only_picture_no_buttons_no_next(self):
        self.q4()
        self.content()
        m = self.evening()
        self.assertIsNone(m["buttons"])
        lines = m["text"].split("\n\n")[-1].split("\n")
        self.assertEqual(lines[0], Q4["why.retro.view.head"])
        self.assertIn(Q4["why.retro.view.confirmed"].replace("{text}", SPEED), lines)
        self.assertIn(Q4["why.retro.view.assumed"].replace(
            "{text}", "\uc624\ub298 \uc6b4\ub3d9 \ubcf5\uad6c\uac00 \uba3c\uc800"), lines)
        # confirmed first, assumptions after: never the reverse
        self.assertLess(lines.index(Q4["why.retro.view.confirmed"].replace("{text}", SPEED)),
                        [i for i, l in enumerate(lines) if l.startswith("\uc81c\uac00")][0])
        self.assertIsNone(self.next_item())       # no follow-up item, ever

    def test_RW2_placeholder_copy_never_ships(self):
        self.content()                            # no Q4 copy landed
        self.assertTrue(ask.t(self.ctx, "why.retro.view.head").startswith(ask.PENDING_PREFIX))
        self.assertIsNone(self.evening())
        self.assertEqual(self.sent, [])

    def test_RW3_readings_hand_out_nothing_and_settle_nothing(self):
        self.q4()
        self.readings(3)
        before = len(store.read_rows(store.ASKS))
        self.assertIsNone(self.evening())         # no confirmed purpose, no assumption
        self.assertEqual(len(store.read_rows(store.ASKS)), before)
        self.assertEqual([r for r in store.read_rows(store.PROBES)
                          if r.get("reading_settled")], [])

    def test_RW4_rendering_mutates_nothing(self):
        self.q4()
        self.content()
        w = self.why("\uc5f4\ub9b0 \uc77d\uae30")
        self.correction("cor-0009", "\uc544\ub2c8\uc57c", "DGN-1301")
        self.cli("read", "--signal", "cor-0009", "--why", w)
        retro.readings_bubble(self.ctx)           # an open retro ask stays open
        before = self.store_bytes()
        self.assertIsNotNone(self.evening())
        self.assertEqual(self.store_bytes(), before)

    def test_RW5_typed_text_after_the_retro_binds_nothing(self):
        self.q4()
        self.content()
        self.evening()
        n = len(self.probes())
        self.tick()
        self.ups("\uadf8\uac74 \uadf8\ub0e5 \ubc24\uc5d0 \uc9c0\uccd0\uc11c")
        self.assertEqual(len(self.probes()), n)   # no retro binding any more
        self.assertEqual(self.nexts, [])

    def test_RW6_empty_span_sends_no_blank_wrapper(self):
        self.q4()
        self.content()
        self.goto(EVENING + 3 * DAY)              # daily span: three days later
        self.slot_on()
        self.tick_slot()
        self.assertEqual(self.sent, [])

    def test_RW7_open_retro_asks_closed_once(self):
        w = self.readings(2)
        self.assertIsNone(retro.readings_bubble(self.ctx))
        rid = GF.historical_queue(self.ctx, "why", "readings", "DGN-1301", [], shape="scatter")[0]
        self.assertEqual(self.asks()[rid]["state"], "queued")
        self.q4()
        self.assertEqual(retro.migrate_view_only(self.ctx), [rid])
        a = self.asks()[rid]
        self.assertEqual((a["state"], a.get("reason")), ("ignored", "retro-view-only"))
        self.assertEqual(retro.migrate_view_only(self.ctx), [])   # one-shot
        self.assertEqual(self.node(w)["status"], "hypothesis")

    def test_RW_net_off_means_no_why_section(self):
        self.readings(2)
        self.conf_off("RETRO_SLOT=on\n")
        self.goto(EVENING)
        self.tick_slot()
        self.assertEqual(self.sent, [])


# ---------------------------------------------------------------------------
# copy + CLI
# ---------------------------------------------------------------------------

class CopyAndCli(Base):
    def catalog(self, lang):
        p = os.path.join(os.path.dirname(ROUTINES), "config", "i18n", lang + ".json")
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def test_RC1_one_pending_block_and_original_why_concept(self):
        keys = (RS.COPY_KEYS_PENDING_OWNER_CONFIRM + RS.COPY_KEYS_CONFIRMED
                + ask.COPY_KEYS_PENDING_OWNER_CONFIRM)
        self.assertFalse(set(RS.COPY_KEYS_CONFIRMED) & set(RS.COPY_KEYS_PENDING_OWNER_CONFIRM))
        for lang in ("ko", "en"):
            cat = self.catalog(lang)
            order = list(cat)
            idx = [order.index(k) for k in RS.COPY_KEYS_PENDING_OWNER_CONFIRM]
            self.assertEqual(idx, list(range(idx[0], idx[0] + len(idx))), lang)  # one block
            for k in keys:
                self.assertTrue(cat.get(k), (lang, k))
        self.assertEqual(self.catalog("ko")["why.ask.subject.framework"],
                         "\ub3c4\uac00\ub2c8 \ud070 Why")
        ko = self.catalog("ko")
        for key in ("subject.framework", "stem.contrast", "stem.set", "free_hint",
                    "stem.act_confirm", "stem.split", "stem.mixed"):
            self.assertIn("Why", ko["why.ask." + key])
            self.assertNotIn("\uc774\uc720", ko["why.ask." + key])
        self.assertEqual(len(self.catalog("ko")["retro.slot.weekdays"].split(",")), 7)

    def test_RC2_cli_off_and_usage(self):
        cli = _load("_retro_slot_cli", os.path.join(ROUTINES, "retro-slot.py"))
        sent = []
        self.goto(EVENING)
        self.assertEqual(cli.main(["tick"], ctx=self.ctx,
                                  sender=lambda *a: sent.append(a) or (0, 1, CHAT)), 0)
        self.assertEqual(cli.main(["next"], ctx=self.ctx,
                                  sender=lambda *a: sent.append(a) or (0, 1, CHAT)), 0)
        self.assertEqual(sent, [])
        self.assertEqual(cli.main([], ctx=self.ctx), 2)
        with open(os.path.join(ROUTINES, "plists.defer"), encoding="utf-8") as fh:
            self.assertNotIn("telegram-agent.retro-slot.plist", fh.read())






# ---------------------------------------------------------------------------
# cadence (DGN-1961, owner 2026-10-11)
# ---------------------------------------------------------------------------

YES_W, YES_D, KEEP = "\uc77c\uc8fc\uc77c\uc5d0 \ud55c \ubc88", "\ub9e4\uc77c", "\uc9c0\uae08\ucc98\ub7fc"


class Cadence(Base):
    def signals(self, ids, w, work="DGN-1301"):
        for cid in ids:
            self.tick(60)
            self.correction(cid, "\uc2e0\ud638 %s \uc544\ub2c8\uc57c" % cid, work)
            self.assertEqual(self.cli("read", "--signal", cid, "--why", w)[0], 0)

    def walk(self):
        """Typed answers until the slot has nothing more; -> items sent."""
        n = 1
        while True:
            self.tick()
            self.ups("\uadf8\uac74 \ub2e4\ub978 \uc774\uc720\uc57c %d" % n)
            if self.next_item() is None:
                return n
            n += 1

    def settled(self):
        return [r for r in store.read_rows(store.PROBES) if r.get("reading_settled")]

    def test_CW1_weekly_once_with_the_whole_week(self):
        w = self.readings(0)
        self.slot_on("RETRO_SLOT_CADENCE=weekly\n")
        slot = self.kit_slot()
        slot.register("diary", 10, lambda day, ctx: "diary " + day.iso)
        slot.register("week", 20, lambda day, ctx: "week %s..%s" % (day.days[0], day.days[-1]),
                      cadences=("weekly",))
        self.assertEqual(retro.cap(self.ctx), 21)            # 7 daily budgets
        for k in range(3):                                   # Thu, Fri, Sat
            self.goto(EVENING + k * DAY - 3 * H)
            self.signals(["cor-%d%d" % (k, i) for i in range(2)], w)
            self.goto(EVENING + k * DAY)
            self.assertEqual(self.tick_slot(slot)[0], [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.settled(), [])                 # nothing given back yet
        self.assertFalse([r for r in store.read_rows(store.ASKS) if r.get("ev") == "shown"])
        self.goto(SUNDAY)
        self.tick_slot(slot)
        [m] = self.sent
        title, *body = m["text"].split("\n\n")
        self.assertEqual(title, "2026-10-05(\uc6d4) ~ 2026-10-11(\uc77c) "
                                "\ud55c \uc8fc \ud68c\uace0")
        names = "\uc6d4\ud654\uc218\ubaa9\uae08\ud1a0\uc77c"
        for i in range(7):
            d = datetime.date(2026, 10, 5) + datetime.timedelta(days=i)
            self.assertIn("%s(%s)\ndiary %s" % (d, names[i], d), body)
        self.assertIn("week 2026-10-05..2026-10-11", body)
        # DGN-2028 AMEND-2 P3.4: the Why section is view-only -- no buttons,
        # no walk, nothing settled, no ask shown
        self.assertEqual(self.labels(m), [])
        self.assertIsNone(self.next_item())
        self.assertEqual(len(self.sent), 1)
        self.assertFalse([r for r in store.read_rows(store.ASKS) if r.get("ev") == "shown"])
        self.assertEqual(self.settled(), [])
        self.goto(SUNDAY + 900)
        self.assertEqual(self.tick_slot(slot)[0], [])        # once
        for k in range(1, 7):                                # Mon..Sat: nothing
            self.goto(SUNDAY + k * DAY)
            self.assertEqual(self.tick_slot(slot)[0], [])
        self.assertEqual(len(self.sent), 1)

    def test_CW2_unknown_value_runs_daily_and_says_so(self):
        self.slot_on("RETRO_SLOT_CADENCE=fortnightly\nRETRO_SLOT_WEEKDAY=someday\n")
        slot = self.kit_slot()
        slot.register("kit", 10, lambda day, ctx: "x", cadences=("daily", "weekly"))
        self.goto(EVENING)                                   # Thursday
        did, out = self.tick_slot(slot)
        self.assertEqual(len(self.sent), 1)                  # daily: never silent-off
        self.assertIn("signal::RETRO_SLOT_CONF_UNKNOWN key=RETRO_SLOT_CADENCE "
                      "value=fortnightly fallback=daily", out)
        self.assertIn("key=RETRO_SLOT_WEEKDAY value=someday fallback=sun", out)
        self.goto(SUNDAY)
        self.tick_slot(slot)
        self.assertIn("\ud55c \uc8fc \ud68c\uace0", self.sent[-1]["text"].split("\n")[0])
        self.assertEqual(RS.cadence(self.ctx)[:2], ("daily", 6))
        self.slot_on("RETRO_SLOT_WEEKDAY=wednesday\n")
        self.assertEqual(RS.cadence(self.ctx), ("daily", 2, []))
        self.slot_on("RETRO_SLOT_WEEKDAY=5\n")
        self.assertEqual(RS.cadence(self.ctx), ("daily", 4, []))

    def test_CW3_weekly_keeps_the_morning_defer_on_its_weekday(self):
        self.slot_on("RETRO_SLOT_CADENCE=weekly\nRETRO_SLOT_WEEKDAY=Fri\n")
        slot = self.kit_slot()
        late = {"ready": False}
        fri = datetime.date(2026, 10, 9)

        def appts(day, ctx):
            if day.date == fri and not late["ready"]:
                return RS.DEFER
            return "appt " + day.iso

        slot.register("appts", 10, appts, defer=True)
        self.goto(EVENING)                                   # Thu: not the day
        self.assertEqual(self.tick_slot(slot)[0], [])
        self.goto(EVENING + DAY)                             # Fri 21:00
        did, _ = self.tick_slot(slot)
        [ev] = self.sent
        self.assertEqual(did[0]["plan"]["deferred"], ["appts"])
        self.assertIn("appt 2026-10-03", ev["text"])
        self.assertIn("appt 2026-10-08", ev["text"])
        self.assertNotIn("appt 2026-10-09", ev["text"])
        late["ready"] = True
        self.goto(MORNING + DAY)                             # Sat 08:00
        self.tick_slot(slot)
        self.assertEqual(self.sent[1]["text"].split("\n\n")[1], "appt 2026-10-09")
        self.goto(MORNING + DAY + 900)
        self.tick_slot(slot)
        self.assertEqual(len(self.sent), 2)

    def test_CW4_month_end_inside_the_week(self):
        self.slot_on("RETRO_SLOT_CADENCE=weekly\n")
        slot = self.kit_slot()
        slot.register("month", 30, lambda day, ctx: "M " + day.iso, cadences=("monthly",))
        slot.set_picture(lambda day, ctx: "/tmp/pic-%s.png" % day.iso)
        plan = RS.compose(self.ctx, slot, RS.Day(datetime.date(2026, 11, 1), 6, "weekly"))
        self.assertIn("M 2026-10-31", plan["text"])
        self.assertIsNone(plan["picture"])
        plan = RS.compose(self.ctx, slot, RS.Day(datetime.date(2026, 10, 25), 6, "weekly"))
        self.assertIsNone(plan["text"])

    # -- the proposal ---------------------------------------------------------

    def conf_text(self):
        with open(os.path.join(self.root, "config", "agent.conf"), encoding="utf-8") as fh:
            return fh.read()

    def quiet_days(self, first, last, slot=None):
        for k in range(first, last + 1):
            self.goto(EVENING + k * DAY)
            self.tick_slot(slot)

    def proposal_line(self, key, **kv):
        line = ask.t(self.ctx, key)
        for k, v in kv.items():
            line = line.replace("{%s}" % k, str(v))
        return line

    def test_CP1_low_activity_proposes_weekly(self):
        self.slot_on()
        before = self.conf_text()
        self.quiet_days(0, 13)
        self.assertEqual(self.sent, [])                      # not before a full window
        self.goto(EVENING + 14 * DAY)
        did, out = self.tick_slot()
        [m] = self.sent
        title, line = m["text"].split("\n\n")
        self.assertEqual(title, "2026-10-22(\ubaa9) \ud68c\uace0")
        self.assertEqual(line, self.proposal_line("retro.slot.cadence.weekly.low",
                                                  days=14, n=0, weekday="\uc77c"))
        self.assertEqual(self.labels(m), [YES_W, KEEP])
        self.assertIn("signal::RETRO_SLOT proposal to=weekly reason=low", out)
        self.assertEqual(self.conf_text(), before)           # proposing changes nothing
        self.assertEqual(RS.cadence(self.ctx)[0], "daily")

    def test_CP2_normal_activity_proposes_nothing(self):
        w = self.readings(0)
        self.slot_on()
        self.quiet_days(0, 0)
        self.goto(EVENING + 10 * DAY - 3 * H)
        self.signals(["cor-%02d" % i for i in range(15)], w)
        self.goto(EVENING + 14 * DAY)
        self.tick_slot()
        # DGN-2028 AMEND-2 P3.4: readings are no retro items (view-only), so
        # nothing is sent -- and the activity still proposes nothing
        self.assertEqual(self.sent, [])
        self.assertIsNone(RS.load_state(self.ctx).get("proposal"))

    def test_CP3_cooldown_one_window(self):
        self.slot_on()
        self.quiet_days(0, 14)
        self.assertEqual(len(self.sent), 1)
        self.quiet_days(15, 27)                              # ignored: not again
        self.assertEqual(len(self.sent), 1)
        self.quiet_days(28, 28)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.labels(self.sent[1]), [YES_W, KEEP])

    def test_CP4_conf_changes_only_on_the_tap(self):
        self.slot_on()
        self.quiet_days(0, 14)
        before = self.conf_text()
        kb = dict(keyboard=[YES_W, KEEP], option_index=1)
        self.tick()
        self.ups("\uc624\ub298 \uc880 \ubc14\ube74\uc5b4")           # other words
        self.tick()
        self.ups(YES_W, source="callback", message_id=9999, **kb)  # another message
        self.assertEqual(self.conf_text(), before)
        self.assertIsNone(RS.load_state(self.ctx)["proposal"]["answer"])
        self.tick()
        self.ups(YES_W, source="callback", message_id=7001, **kb)
        self.assertIn("RETRO_SLOT_CADENCE=weekly", self.conf_text())
        self.assertEqual(RS.load_state(self.ctx)["proposal"]["answer"], "yes")
        self.assertFalse(RS.proposal_open(self.ctx))
        after = self.conf_text()
        self.tick()
        self.ups(KEEP, source="callback", message_id=7001,
                 keyboard=[YES_W, KEEP], option_index=2)    # answered once
        self.assertEqual(self.conf_text(), after)
        self.quiet_days(15, 17)                              # Thu..Sat: weekly now
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(RS.load_state(self.ctx)["cadence"]["mode"], "weekly")

    def test_CP5_net_off_typed_keep(self):
        self.conf_off("RETRO_SLOT=on\n")
        self.quiet_days(0, 14)
        [m] = self.sent
        self.assertEqual(self.labels(m), [YES_W, KEEP])
        before = self.conf_text()
        self.tick()
        self.ups(KEEP)                                       # typed, exact label
        self.assertEqual(self.conf_text(), before)
        self.assertEqual(RS.load_state(self.ctx)["proposal"]["answer"], "keep")

    def test_CP6_view_only_retro_has_no_skip_proposal(self):
        """DGN-2028 AMEND-2 P3.4: the Why section hands out no item, so no
        retro day can go unanswered: the "skip" proposal (answered days)
        has nothing to measure and is never raised."""
        w = self.readings(0)
        self.slot_on("RETRO_SLOT_PROPOSE_WINDOW_DAYS=7\n")
        self.quiet_days(0, 0)
        for k in range(1, 8):
            self.goto(EVENING + k * DAY - 3 * H)
            self.signals(["cor-%d%d" % (k, i) for i in range(3)], w)
            self.goto(EVENING + k * DAY)
            self.tick_slot()
        self.assertEqual(self.sent, [])
        self.assertEqual(RS.why_engagement(self.ctx, datetime.date(2026, 10, 8),
                                           datetime.date(2026, 10, 15)), (0, 0))
        self.assertIsNone(self.next_item())

    def test_CP7_high_activity_while_weekly_proposes_daily(self):
        w = self.readings(0)
        self.slot_on("RETRO_SLOT_CADENCE=weekly\n")
        self.quiet_days(0, 0)
        self.goto(SUNDAY + 7 * DAY)
        self.signals(["cor-%02d" % i for i in range(42)], w)
        self.goto(SUNDAY + 14 * DAY)                         # Sun 10-25
        self.tick_slot()
        [m] = self.sent
        # DGN-2028 AMEND-2 P3.4: no Why item holds it back -- the proposal
        # is the message itself (shown, not held for the end of a walk)
        prop = RS.load_state(self.ctx)["proposal"]
        self.assertEqual((prop["to"], prop["reason"], prop["shown"]),
                         ("daily", "high", "2026-10-25"))
        self.assertEqual(self.labels(m), [YES_D, KEEP])
        self.assertIn("RETRO_SLOT_CADENCE=weekly", self.conf_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
