#!/usr/bin/env python3
"""DGN-1417 owner-facing update surface and durable update ledger.

This is deliberately a small adapter around update_plan.sh and update_apply.sh.
It does not contain a second planner or a second apply engine.  The bridge
uses its JSON output for /update and for the session-start update notice;
the detached supervisor applies exactly the plan lines that were shown.

Roots.  --repo is the canonical machinery root (scripts/pack, releases,
the framework git tags) -- an instance reads it from .instance.conf
DOGANY_REPO_ROOT.  --instance-root is the requesting instance (language,
owner push, session inbox).  They are different directories on a live host.

Release-note source contract (grill fix 6), one contract for every layer:
  framework  repo = --repo,                         commit = tag v<target>
  pack/kit   repo = pack-sources.conf <id>.repo,    commit = the plan's cand_tag
  path       releases/v<target>.md at that commit (git show, never the tree)
  locale     the requester instance's notice language
The note is parsed by version-check.py's own user-summary reader, so the list
row, the detail card and the notice are the same text.

Session-start notice (round 3, owner rehearsal 2026-09-26 section D).  `offer`
turns ONE planner product into ONE message covering every layer that touches
this instance (framework + kits + packs), asked once per version set
(`offered` records the set after a real send).  A tap (`pick`) never trusts
the message: it re-plans, matches the tapped items by key and installed
version (anything else is stale), pulls in required kits, records crew
consent (the owner's tap names the kit), and starts the same supervisor as
/update.

Round 4 (owner review 2026-09-26 section E, supersedes D where different).
A pack that needs a newer kit is ONE notice row whose caption names the kit
("<pack> vA → vB (<kit> vC → vD과 함께)") and whose "<pack> 적용" applies both
-- no after-tap confirm.  Framework rows are one per crew, and (DGN-1744,
owner-approved) such a row lists each agent on its own line:
"Dogany → vB" / "<agent A> vA1 → vB" / "<agent B> vA2 → vB" -- never a "vA1~A2" range.  One row
-> no "전체 적용".  An agent left stopped after an applied update gets ONE
automatic restart (the update run's own restart step, else the agent's
/restart script) before the owner hears anything; still stopped -> the fixed
partial message with a "<agent> 재시작" button per agent, whose tap (`restart`)
runs the same restart and reports success only after the liveness verdict.

DGN-1749 (owner-locked layout 2026-09-27, supersedes the notice shapes
above).  Rows go Dogany, kit, packs, a blank line apart.  Each row is a
plain quote -- "▸ <product>: <target>", "▸ 적용대상", one
"· <a1>·<a2>: 현재 <version>" line per distinct installed version, the
dependency / crew-kit line -- and, on the very next line, ONE expandable
quote with the note's summary, a blank line, its detail (no notes, no
fold).  A notice covers one crew and only its leader (the DGN-1745 rule)
sends it; another member stays quiet and hands what it found to the leader
(its update-offer-request + a session-inbox marker drop, which the leader's
bridge turns into the notice without a model turn), so an idle leader still
sends it.  The one-line version_line (start / busy / /update caption) names
the target once and groups agents by installed version the same way.

Supervisor (grill fix 3).  `worker` is forked into its own session, so the
bridge stops of update_apply do not reach it.  It owns the ledger through
apply -> final restart -> per-member verification, then sends the per-item
result itself.  update_apply is told (--result-file) to hand its record over
instead of delivering it, so a run is still reported exactly once.  An item
is "complete" only when its unit applied AND every affected member is back
and on the target version (grill fix 5); the run says "완료" only when every
item is complete.  A lost supervisor is closed by the next bridge's `sweep`
(claim file = mutex, exactly one delivery).

DGN-1758 (owner decision 2026-09-27, option 2).  After delivery, every agent
the run brought back verified gets the old self-update restart-complete
notice in ITS OWN chat, once (DGN-1939: preceded by the confirmed
"update.result_done" completion line, so every crew member's chat gets its
own completion, not only the requester's), rendered by routines/self-update.sh
--restart-notice (the one copy) and sent through its own push.sh.  The
requester's agent is skipped (the result notice covers that chat); a
blocked, stopped or unconfirmed agent gets nothing; a rehearsal
(DOGANY_LAUNCHD_CAPTURE) sends nothing.

DGN-1781 (owner 2026-09-28 16:43).  The start reply is the start line alone
("🛠️ 업데이트를 시작했어요. ..."), and the worker sends the same line, before
the apply, to every OTHER agent the run touches, in its own chat through the
DGN-1758 sender (the requester's bridge already replied; a rehearsal sends
nothing).  The offer / confirm quote is lighter: "· <product>: <target>",
then one "· 적용대상: <a1>·<a2> - 현재 <version>" line per installed version,
no bold; the confirm header carries the house emoji.

DGN-2125 supersedes DGN-2047: every stopping member gets a strict start
notice in its own chat before teardown, and single-member items deliver
their result card there, while shared items stay in the requester's
combined card.

dec-454 (owner 2026-10-16 12:07: update notes that change only one member go to
that member's chat; supersedes DGN-2081 where they differ).  A non-requester
member's chat gets, per run: its start line; ONE return line
("update.member_return") once it is verified back, with the pointer
("update.member_return_pointer") only when the requester's shared card was
delivered and carries an item of that member; then the result card of the
items that change ONLY that member (DGN-2081's member_result_record
narrowing, partial / blocked reasons kept).  Shared items are never copied
into a member chat.  A member that heard the start line but is not back
gets the pointer alone (no "back" line).  Order: the requester's card,
then each member's return, then the member-only cards (deliver()).

DGN-2157 supersedes the crew routing above: a led crew run has one start
message in the requester's chat, edited through apply and verification
(DGN-2157 part 2, owner-approved Korean progress shape), then one result card carrying every selected item, including member-only packs. Members
send no separate pause / return pair of their own.
FDGN-584 dec-576 supersedes the dec-564 member start/end pair: every other
member receives the leader's full live progress card, edited by the supervisor
through that member's own sender. Per-chat receipts survive retries.
A run touching only one other member keeps its existing own-chat result.

DGN-1784 (owner 2026-09-28 17:25).  /update is crew-scoped, not estate-wide:
it lists the layers touching a member of every crew the requesting instance
belongs to (itself when it has none), with no leader gate -- any member may
open it.  The notice's leader rule and /update's member set come from one
crew read (crew_scope); a /update tap re-plans with the same member set.

DGN-1790 r2 / dec-222 (owner 2026-10-01 06:54, supersedes the DGN-1781
quote rows above).  Offer and result share one card hierarchy: a bold
"[layer] <product> <target>" item head (dec-234: no leading triangle), then "└ 적용대상: <a1>·<a2>
(현재 <version>)" (offer) or "└ <a1>·<a2>: <result>" (result) under it,
one line per distinct installed version / result.  r3 (owner 07:11 "항목마다
칸 분리"): every item gets its own quote, offers and results alike, items
one blank line apart; an item's note fold sits right under its quote,
before the next item.  Every reshaped
line is an i18n key (config/i18n/<lang>.json, I18N_DEFAULTS = the English
fallback; owner 06:29 "i18n하고 디폴트는 다 영어야").

DGN-1794 (owner 2026-09-30).  A busy refusal on the notice, the confirm card
or /update strips the tapped card (the bridge).  Round 2 (dec-210, owner
12:20, supersedes r1's "run /update again" line): the refusal ASKS whether
to carry the apply through ("busy_ask" + follow_yes / follow_no, upd:q).  The
busy ledger keeps what the tap would have applied (`follow`: the item keys
and shown states of its closure, the scope it was planned with, the kits the
tap consented to).  Yes (`follow --choice y`) marks it "queued"
with the tapping chat; no, or no answer, runs nothing.  Pickup
(`follow_through`) runs when a run is delivered (the worker, after its
restart notices), on every bridge-boot sweep, and on the queue tap itself
when nothing is running any more: it re-plans at that moment and starts the
queued items through the same start() only when every key, installed
version, riding dependency and consent is still what was tapped -- the
start line, result and restart notices then go exactly as after a direct
tap, to the tapping chat.  Anything else sends that chat a fresh /update
card instead (never an auto-run); another run still active leaves it
queued; a queue older than FOLLOW_MAX_AGE_S is dropped with one line.  A
rehearsal (DOGANY_LAUNCHD_CAPTURE) picks up nothing.

DGN-1908 (owner 2026-10-07).  A busy tap whose closure the running run
already applies (every key in its selection) is the same button tapped
twice, not another pending update: the ledger turns "duplicate" and the
reply is the running status ("busy_same", owner-confirmed dec-321) -- no ask, nothing
held to carry through, so no follow-through ever settles for it.

DGN-1795 (owner 2026-09-30).  The per-agent sender adds the persona prefix
only to a line that does not already open with a glyph: the DGN-1781 start
line (house emoji first) reaches every other touched agent exactly as the
requester got it, while the DGN-1758 restart notice (plain header, no
glyph) keeps its one self-update prefix.

dec-263 (a) (owner-confirmed 2026-10-06).  A kit crew split across
versions (planner BLOCKED-CREW-LOCKSTEP + its CREW-SPLIT line) is a row of
its own on /update: the kit, each agent's version, and -- when every member
sits on a stable tag and the newest stable is not below any of them -- a
[버전 맞추기] button; a pack that needs that kit says it waits for the
button.  The tap (`sync`) re-checks the split against a fresh plan, plans
THAT sync (update_plan.sh DOGANY_PLAN_CREW_SYNC: the kit at the newest
stable tag for every member + its dependents), records the crew consent the
row named, and starts the same supervisor -> update_apply crew transaction
as any other tap.  A split with an untagged / trial member is the
operator's: the row only says the maintainer is aligning it, no button.
"""
from __future__ import print_function

import argparse
import functools
import hashlib
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid

ACTIVE_STATES = ("running", "verifying")
FINAL_STATES = ("complete", "blocked", "partial", "recovery_required")
# Owner-confirmed IA (DGN-1417 12:43): summary / detail / try, empty omitted.
SECTION_KEYS = (("", "업데이트 요약", "At a glance"),
                ("_detail", "업데이트 상세", "What changed"),
                ("_try", "새로 해볼 수 있는 것", "Try this"))
# The product name in owner copy (rehearsal 2026-09-26 item 15): never 도가니.
PRODUCT = "Dogany"
AGENT_SEP = "·"  # DGN-1749: agents sharing one installed version ("<agent A>·<agent B>")
COUNT_KO = {2: "두", 3: "세", 4: "네", 5: "다섯", 6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉", 10: "열"}
DETAIL_MAX_CHARS = 2500
NOTICE_MAX_CHARS = 3900  # Telegram 4096, visible characters, with headroom

# Owner-confirmed copy, rehearsal 2026-09-26 section D items 18-21 (ko final;
# en mirrors the existing en labels).  Every owner-facing machine line of
# this surface lives here.
COPY = {
    "ko": {
        # DGN-1749 (owner-locked 2026-09-27): one item / several items.
        "header": "🛠️ 새 업데이트가 있어요.",
        "header_n": "🛠️ 새 업데이트가 {n}개 있어요.",
        # DGN-1776 (owner 2026-09-28 12:26): the confirm screen's header counts
        # what THIS apply applies.  Owner wrote n=2; n=1/3/4/5+ extrapolated.
        # DGN-1781 (owner 2026-09-28 16:43): the house emoji in front.
        "confirm_header": "🛠️ 아래 업데이트를 적용할까요?",
        "confirm_header_n": "🛠️ 아래 {count} 업데이트를 적용할까요?",
        "needs": "{dep} 함께 필요해요.",
        "crew_kit": "같이 쓰는 킷이라 {n} 에이전트가 함께 올라가요.",
        "summary": "업데이트 요약",
        "detail": "업데이트 상세",
        "apply_all": "전체 업데이트",
        "apply_one": "{name} {target} 업데이트",
        "offer_apply": "적용",
        "offer_back": "< 뒤로가기",
        "offer_caution": "⚠️ 적용하는 동안 {agents} 잠깐 멈춰요. 하던 대화는 끊길 수 있어요.",
        # DGN-1781 (owner 2026-09-28 16:43): the start line alone, sent to
        # every agent the apply touches.
        "start": "🛠️ 업데이트를 시작했어요. 잠깐 재시작한 뒤, 제대로 적용됐는지 확인해서 알려드릴게요.",
        "done": "🛠️ 업데이트를 완료했어요.",
        "migrate_offer": "{old_name} {new_name} 바뀌어요. 하던 기록·설정은 그대로 옮겨져요.",
        "migrate_done": "{new_name} {target}: 새 제품으로 옮겼어요. 이전 팩 기록은 보관했어요.",
        "migrate_join": " 데브킷 설치·크루 참여를 함께 진행하고 기존 작업 기록도 읽을 수 있어요.",
        "done_item": "{name} {target}: 새 버전으로 적용됐어요.",
        "done_crew": "{name} {target}: {all} 모두 새 버전으로 적용됐어요.",
        # DGN-1794 round 2 (dec-210).  owner-confirmed 2026-09-30 (dec-211): the busy
        # ask, its two buttons, the two edits after a tap, and the pickup's
        # own lines (plan changed / nothing left / lookup failed / expired).
        "busy_ask": "지금 {running}가 진행 중이에요. 끝나면 이어서 적용할까요?",
        "follow_yes": "이어서 적용",
        "follow_no": "그만두기",
        "follow_queued": "알겠어요. 끝나면 이어서 적용할게요.",
        # DGN-2135: an apply tap while another update runs queues itself --
        # one tap, this line edits the offer in place.
        "queued_tap": "지금 {running}가 진행 중이에요. 이 업데이트는 끝나면 자동으로 시작돼요.",
        "follow_declined": "적용하지 않았어요.",
        "follow_changed": "내용이 바뀌어서 이어서 적용하지 않았어요. 아래에서 다시 골라주세요.",
        "follow_none": "이미 적용돼서 할 게 없어요.",
        "follow_failed": "확인이 안 돼서 이어서 적용하지 않았어요. /update 로 다시 열어주세요.",
        "follow_expired": "하루가 지나서 이어서 적용은 취소했어요. /update 로 다시 열어주세요.",
        # DGN-1908, owner-confirmed 2026-10-08 (dec-321): a second tap on what
        # the running run already applies -- its status, no ask.  {running}
        # is busy_item / busy_crew (the "{name} update ({from} -> {to})" row
        # below), rendered the same way busy_ask renders it.
        "busy_same": "지금 {running}가 진행 중이에요. 끝나면 결과를 알려드릴게요.",
        "busy_item": "{name} 업데이트({current} → {target})",
        # DGN-1749: agents on the same installed version share one entry.
        "busy_crew": "{name} 업데이트({rows})",
        # /update detail card only; the notice shows the kit on the row (item 22).
        "bundle": "{name} {target}로 업데이트하려면 {dep}도 {dep_target}로 함께 업데이트해야 해요.",
        "stale": "업데이트할 내용이 달라졌어요. 새 목록에서 다시 골라주세요.",
        "truncated": "…(이하 생략 — 더 궁금하시면 물어봐 주세요)",
        # Item 24 (owner-confirmed): applied, but an agent stayed stopped after
        # the automatic restart retry.  {agents} carries its subject particle (josa).
        "partial_head": "🛠️ 업데이트를 적용했는데, {agents} 멈춰 있어요.",
        "partial_done": "- {line}",
        "partial_item": "- {name} {target}: 적용은 됐는데, {agents} 멈춰 있어요.",
        "restart_button": "{agent} 재시작",
        # COPY PENDING OWNER CONFIRM: the restart tap's own replies.
        "restart_running": "{agent} 다시 시작하고 있어요. 돌아오면 알려드릴게요.",
        "restart_ok": "{agent} 다시 돌아왔어요.",
        "restart_failed": "{agent} 다시 시작해 봤지만 아직 멈춰 있어요. 잠시 뒤 한 번 더 눌러 주세요.",
        # DGN-1709 neutral lines, reused per item when the agent cannot hear.
        "not_all": "업데이트가 전부 적용되지는 않았어요.",
        "none_applied": "이번 업데이트는 적용되지 않았어요. 바뀐 것은 없어요.",
        "item_blocked": "{name} {target}: 적용되지 않았어요. 바뀐 것은 없어요.",
        # COPY PENDING OWNER CONFIRM: an item whose state is not known, and the
        # agent lists (item 24: never "확인이 필요해요", never 켜다/응답/프로세스).
        "item_unsure": "{name} {target}: 적용 결과를 아직 알 수 없어요.",
        "left_down": "업데이트 전에 멈춘 채로 남아 있는 에이전트: ",
        "not_back": "다시 시작하지 못한 에이전트: ",
        "unconfirmed": "지금 상태를 알 수 없는 에이전트: ",
        "record_tail": "자세한 내용은 업데이트 실행 기록에 있어요.",
        # DGN-2095 (dec-426): one start line after the confirm-card warning.
        # The crew transaction sends it before stopping the bridges.
        "stop_notice": "🛠️ 적용을 시작했어요. 끝나면 결과를 알려드릴게요.",
    },
    "en": {
        "header": "🛠️ A new update is available.",
        "header_n": "🛠️ {n} new updates are available.",
        "confirm_header": "🛠️ Apply the update below?",
        "confirm_header_n": "🛠️ Apply the {count} updates below?",
        "needs": "Needs {dep} as well.",
        "crew_kit": "A shared kit, so {n} agents update together.",
        "summary": "At a glance",
        "detail": "What changed",
        "apply_all": "Update all",
        "apply_one": "Update {name} {target}",
        "offer_apply": "Apply",
        "offer_back": "< Back",
        "offer_caution": "⚠️ While the update is being applied, {agents} will pause briefly. Conversations in progress may be interrupted.",
        "start": "🛠️ Update started. I'll restart briefly, then check that it applied correctly and let you know.",
        "done": "🛠️ Update complete.",
        "migrate_offer": "{old_name} is now {new_name}; records and settings carry over.",
        "migrate_done": "{new_name} {target}: moved to the new product. The old pack's records are archived.",
        "migrate_join": " Includes Devkit installation and crew membership; existing worklog records remain readable.",
        "done_item": "{name} {target}: now on the new version.",
        "done_crew": "{name} {target}: {all} are both on the new version.",
        "busy_ask": "{running} is in progress right now. Apply this one right after it finishes?",
        "follow_yes": "Apply after it",
        "follow_no": "Don't apply",
        "follow_queued": "Got it. I'll apply it once this finishes.",
        "queued_tap": "{running} is in progress right now. This update starts automatically after it.",
        "follow_declined": "Not applied.",
        "follow_changed": "The contents changed, so I didn't apply it. Please pick again below.",
        "follow_none": "Already applied, nothing to do.",
        "follow_failed": "I couldn't verify, so I didn't apply it. Reopen with /update.",
        "follow_expired": "A day has passed, so the queued apply was cancelled. Reopen with /update.",
        # DGN-1908, owner-confirmed 2026-10-08 (dec-321).
        "busy_same": "{running} is in progress right now. I'll let you know the result when it finishes.",
        "busy_item": "The {name} update ({current} → {target})",
        "busy_crew": "The {name} update ({rows})",
        "bundle": "To update {name} to {target}, {dep} must be updated to {dep_target} as well.",
        "stale": "What can be updated has changed. Please pick again from the new list.",
        "truncated": "…(truncated — ask me if you want more)",
        "partial_head": "🛠️ The update was applied, but {agents} stopped.",
        "partial_done": "- {line}",
        "partial_item": "- {name} {target}: applied, but {agents} stopped.",
        "restart_button": "Restart {agent}",
        "restart_running": "Restarting {agent}. I'll let you know when it's back.",
        "restart_ok": "{agent} is back.",
        "restart_failed": "I tried restarting {agent}, but it is still stopped. Please tap again in a little while.",
        "not_all": "Not everything in this update was applied.",
        "none_applied": "This update was not applied. Nothing changed.",
        "item_blocked": "{name} {target}: not applied. Nothing changed.",
        "item_unsure": "{name} {target}: the result is not known yet.",
        "left_down": "Agents still stopped from before the update: ",
        "not_back": "Agents that could not be restarted: ",
        "unconfirmed": "Agents whose state is not known right now: ",
        "record_tail": "The details are in the update run record.",
        # DGN-2095 (dec-426): English mirror of the approved Korean copy.
        "stop_notice": "🛠️ I've started applying the update. I'll let you know the result when it's done.",
    },

}


def copy(lang):
    return COPY["ko" if lang == "ko" else "en"]


# DGN-1790 r2 (owner 2026-10-01 06:29 "i18n하고 디폴트는 다 영어야"): the
# lines of the update card this surface reshaped -- layer tag, item head,
# target / result lines (dec-221, dec-222) -- live in config/i18n/<lang>.json
# under these keys.  The value here is only the English fallback for a
# missing key or locale, never a second copy of the owner's words.
I18N_DEFAULTS = {
    # dec-594: English equivalent is DRAFT, pending owner review.
    "update.refit_preflight": "[DRAFT] The update was not applied.\n"
        "\u00b7 Reason: A problem occurred while checking which files would change. Nothing changed, "
        "and you can keep using {agent_topic} on the current version.\n"
        "\u00b7 Alternative: Would you like to tell the developers about this? "
        "You'll receive another update notification when a fixed version is available.",
    "update.refit_report_button": "[DRAFT] Tell the developers",
    "update.layer_framework": "[Framework]",
    "update.layer_kit": "[Kit]",
    "update.layer_pack": "[Agent]",
    "update.item_head": "{product} {target}",
    # WORDING PENDING OWNER CONFIRMATION (DGN-2081): result rows name both versions.
    "update.result_item_head": "{product} {current} \u2192 {target}",
    "update.offer_target": "└ Applies to: {agents} (now {current})",
    # FDGN-586 / dec-580: English copy pending owner confirmation.
    "update.offer_applying_header": "\u23f3 Updates being applied",
    "update.apply_agent": "Update {agent}: {name} {target}",
    "update.offer_migration_target": "└ Applies to: {agents} ({current} → {target})",
    "update.crew_registration_pending": "The crew registration must be converted before this update can be applied.",
    "update.result_target": "└ {agents}: {result}",
    "update.result_done": "✅ Update complete.",
    "update.member_start": "\U0001f6e0\ufe0f I'll pause briefly to apply the update.",
    "update.member_return": "\u2705 I'm back.",
    "update.member_return_pointer": "Details are in {requester}'s chat.",
    "update.result_partial": "⚠️ The update was only partly applied.",
    # Owner-approved 2026-10-21 (dec-514/515): result, reason, alternative.
    # Unknown causes stay unconfirmed, including refused-before-write runs.
    "update.failure": "The update could not be applied.",
    "update.failure_unknown": "The cause has not been confirmed yet.",
    "update.failure_agent_handoff": "This failure has been handed to the agent. You'll receive another update notification when a fixed version is available.",
    "update.failure_retry": "Try again with /update.",
    "update.failure_reason": "\u00b7 Reason: {reason}",
    "update.failure_alternative": "\u00b7 Alternative: {alternative}",
    "update.failure_channel": "The required {kit} version ({version}) has not been released on the stable channel yet.",
    "update.failure_channel_next": "I'll let you know when that {kit} version is released.",
    "update.failure_dependency": "{dep} is needed first.",
    "update.failure_dependency_next": "Update the required kit together with this update in /update.",
    "update.failure_dependency_blocked": "The required kit could not be updated in this run.",
    "update.failure_dependency_blocked_next": "Resolve the kit update failure above, then retry with /update.",
    "update.failure_lease": "A previous kit update was cut off partway and is still pending.",
    "update.failure_lease_next": "Repair the update with the button below, then retry with /update.",
    "update.repair_button": "Repair update",
    "update.failure_untried": "An earlier update did not finish, so this update could not be applied.",
    "update.failure_restart": "Could not restart {agents}.",
    "update.failure_restart_next": "Try again with the restart button below.",
    "update.failure_network": "An internet connection problem prevented the update download.",
    "update.failure_network_next": "Check the internet connection, then try again with /update.",
    "update.failure_no_version": "No installable kit version was found.",
    "update.failure_post_install_refused": "{kit_product} {version} refused its post-install step because this agent does not meet its requirements.",
    "update.failure_post_install_failed": "{kit_product} {version} failed during its post-install step.",
    "update.failure_install": "An error occurred during installation.",
    "update.drift_check_button": "Check differences",
    "update.failure_content_drift": "Stopped because the update would overwrite files changed directly on this agent.",
    "update.failure_content_drift_next": "Hand the difference check to the agent with the button below. You'll receive another update notification when a fixed version is available.",
    "update.result_blocked": "The update could not be applied.",
    "update.result_ok": "Updated to the new version",
    "update.result_preflight": "The cause has not been confirmed yet.",
    "update.result_dependency": "{dep} is needed first.",
    "update.result_incomplete": "The update started but did not finish",
    "update.result_untried": "An earlier update did not finish, so this update could not be applied.",
    "update.result_unknown": "The cause has not been confirmed yet.",
    "update.result_down": "{agent} did not come back online",
    "update.result_refused_restarted": "The cause has not been confirmed yet.",
    # dec-263 (a): the crew version-split rows on /update and their button.
    # owner-approved 2026-10-06 (ko verbatim in config/i18n/ko.json; en mirror).
    "update.crew_split": "{kit}: the agents that share it are on different versions ({versions}). "
                         "Tap [Align versions] to bring them all to the latest stable release, then update. "
                         "The other updates can still be applied as they are.",
    # owner-approved 2026-10-06 (the "{agent} {ver}" pair of the row above).
    "update.crew_split_version": "{agent} {version}",
    # owner-approved 2026-10-06
    "update.crew_split_dependent": "{pack}: {kit} has to be aligned first. "
                                   "Tapping [Align versions] on {kit} updates this as well.",
    # owner-approved 2026-10-06 (the button the two rows above name).
    "update.crew_sync_button": "Align versions",
    # owner-approved 2026-10-06 (dec-264): a split with an untagged / trial
    # member is the operator's -- no button, one line.
    "update.crew_split_operator": "{kit}: the maintainer is aligning the versions of the agents that share it.",
    # owner-approved 2026-10-06 (dec-264)
    "update.crew_split_dependent_operator": "{pack}: {kit} has to be aligned first. The maintainer is aligning it.",
    # WORDING PENDING OWNER CONFIRMATION (dec-263 (a) draft): the button when
    # one /update screen carries two or more alignable kits.
    "update.crew_sync_button_named": "Align {kit} versions",
    "update.progress_head": "\U0001f6e0\ufe0f Updating",
    "update.precheck_start": "Checking before the update started.",
    # dec-579: English progress copy.
    "update.progress_complete": "\u2705 Update complete",
    "update.progress_stopped": "Update stopped",
    "update.progress_restart": "Restart agents",
    "update.progress_minutes": "{minutes}m {seconds}s elapsed",
    "update.progress_seconds": "{seconds}s elapsed",
    "update.progress_pack_suffix": "pack",
    "update.progress_update": "Update {name}",
    "update.progress_install": "Install {name}",
}

# Keep Korean progress copy available without the lookup module; shared
# defaults remain English.
PROGRESS_KO_DEFAULTS = {
    "update.precheck_start": "업데이트 전 점검을 시작했어요.",
    "update.progress_stopped": "업데이트 중단",
    "update.progress_head": "\U0001f6e0\ufe0f \uc5c5\ub370\uc774\ud2b8 \uc911",
    "update.progress_complete": "\u2705 \uc5c5\ub370\uc774\ud2b8 \uc644\ub8cc",
    "update.progress_restart": "\uc5d0\uc774\uc804\ud2b8 \uc7ac\uc2dc\uc791",
    "update.progress_minutes": "{minutes}\ubd84 {seconds}\ucd08",
    "update.progress_seconds": "{seconds}\ucd08",
    "update.progress_pack_suffix": "\ud329",
    # dec-502 (owner 2026-10-19 "2. \uc5c5\ub370\uc774\ud2b8\ub9cc"): the row names the action, no version.
    "update.progress_update": "{name} \uc5c5\ub370\uc774\ud2b8",
    "update.progress_install": "{name} \uc124\uce58",
}


@functools.lru_cache(maxsize=1)
def _i18n_lookup():
    """agents/.template/routines/lib/i18n_lookup.py of this machinery repo
    (DGN-1789, the layered lookup every framework surface reads); None when
    absent."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(here, "agents", ".template", "routines", "lib", "i18n_lookup.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("dogany_i18n_lookup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def word(lang, key):
    """The i18n value of `key` (active language, then en); the English
    default when absent, except for the Korean-only progress fallback."""
    value = ""
    lookup = _i18n_lookup()
    if lookup is not None:
        template = os.path.dirname(os.path.dirname(os.path.dirname(lookup.__file__)))
        try:
            value = lookup.lookup(template, lang or "en", key)
        except Exception:
            value = ""
    if isinstance(value, str) and value:
        return value
    if lang == "ko" and key in PROGRESS_KO_DEFAULTS:
        return PROGRESS_KO_DEFAULTS[key]
    return I18N_DEFAULTS[key]


def atomic_write(path, value):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp.%s" % os.getpid()
    with open(tmp, "w") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp, path)


def shared_home():
    return os.environ.get("DOGANY_SHARED_HOME", os.path.join(os.path.expanduser("~"), ".dogany"))


def ledger_dir():
    return os.path.join(shared_home(), "update-ledger")


def ledger_path(ledger_id):
    if not re.match(r"^[a-f0-9-]{8,64}$", ledger_id):
        raise ValueError("invalid ledger id")
    return os.path.join(ledger_dir(), ledger_id + ".json")


def load_ledger(ledger_id):
    with open(ledger_path(ledger_id), "r") as handle:
        return json.load(handle)


def claim(path):
    """O_EXCL claim file: exactly one caller wins (apply start, delivery)."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.write(fd, ("%d %d\n" % (os.getpid(), int(time.time()))).encode())
    os.close(fd)
    return True


def read_conf(path):
    conf = {}
    try:
        with open(path, "r") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    conf.setdefault(key.strip(), value.strip())
    except (IOError, OSError):
        pass
    return conf


def norm(version):
    return (version or "").strip().lstrip("v")


@functools.lru_cache(maxsize=1)
def _own_semver():
    """This machinery repo's scripts/pack/lib/semver_range.py; None when it
    cannot be loaded (vfmt then keeps the plain "v<version>")."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "semver_range.py")
    try:
        spec = importlib.util.spec_from_file_location("dogany_semver_range_own", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def vfmt(version, pid=None):
    """Full semver with v, always (rehearsal item 16).  dec-461: one raw
    semver format for every row, framework included; a dev-tail product's
    legacy stamp shows the dev tail it maps to (semver_range.display_version:
    kit_dev 0.1.2 -> "v0.1.0-dev.2"), never "v0.1.2"."""
    if pid and norm(version):
        module = _own_semver()
        shown = module.display_version(pid, norm(version)) if module else None
        if shown:
            return shown
    return "v" + norm(version) if norm(version) else ""


def _pid(item):
    """The pack id of a kit / pack offer item (vfmt's dev-tail display);
    None for the framework row."""
    return item.get("id") if item.get("kind") in ("kit", "pack") else None


def same_root(a, b):
    return os.path.realpath(a) == os.path.realpath(b)


def version_check(repo):
    """The notice's own module: one parser for list, detail and notice."""
    path = os.path.join(repo, "agents", ".template", "routines", "version-check.py")
    spec = importlib.util.spec_from_file_location("dogany_version_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def notice_lang(instance_root, repo):
    try:
        return version_check(repo)._resolve_lang(instance_root)
    except Exception:
        return "en"


def git_show(repo, commit, path):
    """File content at a fixed commit, or None (absent / unreadable)."""
    if not repo or not commit or not os.path.isdir(repo):
        return None
    proc = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (commit, path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def pack_repo(pack_id):
    registry = os.environ.get("DOGANY_PACK_SOURCES", os.path.join(shared_home(), "pack-sources.conf"))
    return read_conf(registry).get(pack_id + ".repo", "")


def note_source(kind, pid, target, cand_tag, repo, lang):
    version = norm(target)
    if kind == "framework":
        return {"repo": repo, "commit": "v" + version, "path": "releases/v%s.md" % version, "locale": lang}
    return {"repo": pack_repo(pid), "commit": cand_tag, "path": "releases/v%s.md" % version, "locale": lang}


@functools.lru_cache(maxsize=1)
def candidate_reader():
    """Reuse the cut producer's receipt reader; this never verifies or applies."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                        "scripts", "dev-candidate.py")
    spec = importlib.util.spec_from_file_location("update_dev_candidate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def framework_verification(repo, target, lang):
    """Display evidence for this exact dev tag; absence is not verification."""
    if "-dev." not in norm(target):
        return ""
    state = "unknown"
    try:
        reader = candidate_reader()
        sha = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--verify", "refs/tags/v%s^{commit}" % norm(target)],
            text=True, stderr=subprocess.DEVNULL).strip()
        for row in reader.receipts(repo):
            if (isinstance(row, dict) and isinstance(row.get("candidate"), dict)
                    and row["candidate"].get("sha") == sha):
                state = row.get("state", "unknown")
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if state == "verified":
        return ""
    key = "update.verification_pending" if state == "slow-pending" else "update.verification_unknown"
    return word(lang, key)


def framework_suffix(targets, repo, lang):
    """Read each (target, member) source, also for mixed-version atomic groups."""
    labels = []
    for target, root in targets:
        if "-dev." not in norm(target):
            continue
        source = read_conf(os.path.join(root, ".instance.conf")).get("DOGANY_REPO_ROOT") or repo
        label = framework_verification(source, target, lang)
        if label and label not in labels:
            labels.append(label)
    return " [%s]" % ", ".join(labels) if labels else ""


def target_text(item, lang):
    text = vfmt(item["target"], _pid(item))
    # Compact version-line rows may omit planner metadata.
    if item.get("kind") == "framework":
        text += framework_suffix([(item["target"], root) for root in item["member_roots"]],
                                 (item.get("note") or {}).get("repo", ""), lang)
    return text


def included_framework_target(fw, lang):
    # A bundled pack's note repo is its pack source, not the framework source.
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return vfmt(fw["target"]) + framework_suffix([(fw["target"], fw["root"])], repo, lang)


def cmd_verification(args):
    machinery = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    label = framework_verification(args.repo, args.version, notice_lang(args.instance_root, machinery))
    print(" [%s]" % label if label else "", end="")


def read_note(source, repo):
    """Sections in the confirmed IA, empty sections omitted.  No note = []."""
    text = git_show(source["repo"], source["commit"], source["path"])
    if text is None:
        source["status"] = "absent"
        return []
    vc = version_check(repo)
    lang = source["locale"] if source["locale"] in ("ko", "en") else "en"
    found = []
    for suffix, title_ko, title_en in SECTION_KEYS:
        body = vc._strip_tickets(vc._user_summary_from_note(text, lang + suffix, max_lines=None)).strip()
        if body:
            found.append({"key": suffix or "_summary", "title": title_ko if lang == "ko" else title_en, "body": body})
    source["status"] = "ok"
    return found


def summary(sections, lang):
    # The list row reuses the [업데이트 요약] body verbatim (owner 12:44) --
    # never a second, separately written summary.
    title = SECTION_KEYS[0][1] if lang == "ko" else SECTION_KEYS[0][2]
    for section in sections:
        if section["title"] == title:
            return section["body"]
    return ""


def pack_manifest(pid, cand_tag):
    text = git_show(pack_repo(pid), cand_tag, "pack-manifest.json")
    try:
        manifest = json.loads(text) if text else {}
    except ValueError:
        manifest = {}
    return manifest if isinstance(manifest, dict) else {}


def layer_kind(kind, provides_kit=None):
    """DGN-1790 / dec-221: the layer a pack manifest declares -> "kit" |
    "pack" | "" (unknown).  Read from the manifest's own kind / provides_kit,
    never from the name.  Contract packs can only say kit or pack (C-KIND,
    install gate fails closed), so "" is a legacy or unreadable manifest."""
    if kind == "kit" or (isinstance(provides_kit, str) and provides_kit):
        return "kit"
    if kind == "pack":
        return "pack"
    return ""


def product_name(raw, kind, lang, tagged=True):
    """Owner-facing component name, the ONE seat every update line names a
    layer through (menu, offer, busy, result, restart notice, installed
    versions).  DGN-1790 / dec-221 (owner 2026-10-01): the layer tag first --
    "[프레임워크] Dogany", "[킷] 라이프킷", "[에이전트] 헬스 트레이너" -- and
    Legacy English "Dogany" prefixes are shortened; Korean manifest names
    (including "도가니") stay intact (DGN-2128). kind is "framework" | "kit" | "pack" |
    "" (unknown: no tag -- a guessed layer is worse than none).  DGN-1749
    review (6): no auto "팩" / "Pack" suffix -- the catalog name itself.
    tagged=False (dec-222): the bare name, for text that becomes the owner's
    own chat turn (the crew stall alarm's button)."""
    if kind == "framework":
        name = PRODUCT
    else:
        name = (raw or "").strip()
        for prefix in (PRODUCT,):
            rest = name[len(prefix):]
            if name.startswith(prefix) and rest[:1].isspace() and rest.strip():
                name = rest.strip()
    key = "update.layer_" + (kind or "")
    tag = word(lang, key) if tagged and key in I18N_DEFAULTS else ""
    return ("%s %s" % (tag, name)).strip() if name else ""


@functools.lru_cache(maxsize=1)
def _agent_name():
    """agents/.template/routines/lib/agent_name.py of this machinery repo --
    the single owner-facing agent name resolver (DGN-1744); None when absent."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(here, "agents", ".template", "routines", "lib", "agent_name.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("dogany_agent_name", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def member_label(root, name, lang):
    """DGN-1744: the onboarding name (identity "- Name:") wins over the
    mint-time .instance.conf label, which stays the slug nobody updates."""
    resolver = _agent_name()
    if resolver is not None:
        return resolver.display_name(root, name, label=(lang == "ko"))
    conf = read_conf(os.path.join(root, ".instance.conf"))
    if lang == "ko":
        return conf.get("DOGANY_AGENT_LABEL") or conf.get("DOGANY_AGENT_NAME") or name
    return conf.get("DOGANY_AGENT_NAME") or name


def resolved_product_name(kind, pid, cand_tag, lang, tagged=True):
    """Owner-facing product name for one update_apply unit (framework | kit |
    pack), through the SAME seat the /update menu uses (product_name over the
    manifest name_ko / name_en) -- never a second name map.  "" when the
    manifest carries no name; the caller picks its own no-name fallback
    (DGN-1758 pack-name: never the pid slug for a restart-notice header)."""
    if kind == "framework":
        return product_name("", "framework", lang, tagged)
    raw, manifest = "", {}
    try:
        manifest = pack_manifest(pid, cand_tag)
        raw = manifest.get("name_ko" if lang == "ko" else "name_en") or ""
    except Exception:
        raw = ""
    raw = raw if isinstance(raw, str) else ""
    # The unit's kind is the planner's own manifest read (update_apply
    # UNIT_META), the fallback when this tag's manifest carries no kind.
    return product_name(raw, layer_kind(manifest.get("kind") or kind, manifest.get("provides_kit")), lang, tagged) if raw.strip() else ""


def unit_display(kind, pid, cand_tag, members, lang, tagged=True):
    """DGN-1746: owner-facing names for one update_apply unit.  kind is
    framework | kit | pack; members = [(name, root), ...].  An unresolvable
    product name falls back to the slug, never blank."""
    product = (resolved_product_name(kind, pid, cand_tag, lang, tagged)
               or product_name(pid or "", layer_kind(kind), lang, tagged))
    return {"product": product, "agents": [agent_label(root, name, lang) for name, root in members]}


def agent_label(root, name, lang):
    """member_label with the slug fallback guaranteed non-blank (DGN-1746)."""
    try:
        label = member_label(root, name, lang) if root else name
    except Exception:
        label = name
    return (label or "").strip() or name or os.path.basename(root or "")


def annotate_record(record, unit_meta, roots, lang):
    """DGN-1746: add display fields to an update_apply_result record in place.
    unit_meta[i] = {"kind", "id", "cand_tag", "members": [(name, root)]} for
    record["units"][i]; roots = {record list key: [root, ...]} for the agent
    lists.  "unit" and the slug lists stay the machine keys; "product" /
    "agents" and the *_labels lists are what the agent names things by."""
    for unit, meta in zip(record.get("units") or [], unit_meta):
        unit.update(unit_display(meta.get("kind", ""), meta.get("id", ""), meta.get("cand_tag", ""),
                                 meta.get("members") or [], lang))
        unit["target"] = meta.get("target") or meta.get("cand_tag", "").rsplit("/v", 1)[-1]
        # DGN-2119: the owner-facing target (dev-tail display for an
        # unreleased kit/pack); result_text prints it as written.
        unit["target_shown"] = vfmt(unit["target"], meta.get("id") if meta.get("kind") in ("kit", "pack") else None)
        unit["members"] = [{"name": n, "root": r, "label": label} for (n, r), label in zip(meta.get("members") or [], unit["agents"])]
        if meta.get("dependency"):
            unit["dependency_product"] = unit_display("kit", meta["dependency"], "HEAD", [], lang)["product"]
        payload = unit.get("reason_payload") or {}
        if unit.get("reason_code") == "kit.channel-unavailable" and payload.get("kit"):
            manifest = pack_manifest(payload["kit"], "HEAD")
            raw = manifest.get("name_" + lang) or manifest.get("name_en") or payload["kit"]
            unit["reason_payload"] = dict(payload, kit_product=product_name(raw, "kit", lang, tagged=False))
        if unit.get("reason_code") in ("install.post-install-refused", "install.post-install-failed"):
            unit["reason_payload"] = dict(payload, kit_product=unit["product"],
                                           version=payload.get("version") or unit["target"])
        if not unit["product"]:
            unit["product"] = unit.get("unit", "")
    products = {meta.get("id"): unit["product"] for unit, meta in zip(record.get("units") or [], unit_meta)}
    for unit, meta in zip(record.get("units") or [], unit_meta):
        if meta.get("kind") == "kit":
            provider = pack_manifest(meta.get("id", ""), meta.get("cand_tag", "")).get("provides_kit")
            if isinstance(provider, str) and provider:
                products[provider] = unit["product"]
    for unit, meta in zip(record.get("units") or [], unit_meta):
        if meta.get("dependency") in products:
            unit["dependency_product"] = products[meta["dependency"]]
    for key, rlist in roots.items():
        seen, labels = set(), []
        for root in rlist:  # same dedup-by-basename as update_apply _names_of
            base = os.path.basename(root.rstrip("/"))
            if base and base not in seen:
                seen.add(base)
                labels.append(agent_label(root, base, lang))
        record[key + "_labels"] = labels
        record[key + "_members"] = [{"name": os.path.basename(r), "root": r, "label": agent_label(r, os.path.basename(r), lang)} for r in rlist]
    record["lang"] = lang
    return record


def members_of(blob):
    out = []
    for entry in blob.split():
        name, _, root = entry.partition(":")
        out.append({"name": name, "root": root})
    return out


def item_key(kind, pid, target, roots):
    """Identity includes the unordered instance set; names alone can collide."""
    return "%s:%s:%s:%s" % (kind, pid, target,
                                json.dumps(sorted(set(os.path.realpath(r) for r in roots)),
                                           separators=(",", ":")))


def item_from(kind_field, fields, lines, repo, lang, includes=None):
    requires_kit = {}
    if kind_field == "FW":
        _, name, root, current, target = fields[:5]
        item_kind, members, pid, cand_tag, crew = "framework", [{"name": name, "root": root}], name, "", ""
        display = product_name("", "framework", lang)
    else:
        _, pid, pack_kind, provides, current, cand_tag, target, member_blob = fields[:8]
        item_kind = "kit" if (pack_kind == "kit" or provides) else "pack"
        members = members_of(member_blob)
        crew = (provides or pid) if item_kind == "kit" else ""
        manifest = pack_manifest(pid, cand_tag)
        display = product_name(manifest.get("name_ko" if lang == "ko" else "name_en") or pid,
                               layer_kind(manifest.get("kind") or pack_kind, manifest.get("provides_kit") or provides), lang)
        if isinstance(manifest.get("requires_kit"), dict):
            requires_kit = manifest["requires_kit"]
    source = note_source(item_kind, pid, target, cand_tag, repo, lang)
    sections = read_note(source, repo)
    sync = []
    if kind_field == "PACK" and len(fields) >= 9 and fields[8]:
        # dec-263: the planned crew sync -- every member's own installed
        # version, in member order (version_groups shows one entry each).
        declared = dict(entry.partition("=")[::2] for entry in fields[8].split())
        sync = [[m["name"], norm(declared.get(m["name"], ""))] for m in members]
    item = {"key": item_key(item_kind, pid, target, [m["root"] for m in members]), "kind": item_kind, "id": pid,
            "name": display, "current": current, "target": target, "cand_tag": cand_tag,
            "members": [m["name"] for m in members], "member_roots": [m["root"] for m in members],
            "labels": [member_label(m["root"], m["name"], lang) for m in members],
            "pack_roots": [m["root"] for m in members] if kind_field == "PACK" else [],
            "crew_kit": crew, "needs_consent": bool(crew), "lines": lines,
            "summary": summary(sections, lang), "sections": sections, "note": source,
            "includes": includes or [], "requires_kit": requires_kit, "requires": []}
    if sync:
        item["sync"] = sync
        item["currents"] = sync
    return item


def migration_source(repo, root, pid, tag):
    """The replacement may not have a source-registry row until first install.
    Prefer the source registry; use the instance catalog only when unregistered."""
    source = pack_repo(pid)
    if source:
        text = git_show(source, tag, "pack-manifest.json")
        return source, json.loads(text) if text else {}
    lib = os.path.join(repo, "scripts", "pack", "lib")
    catalog = subprocess.check_output(["bash", os.path.join(lib, "canonical_catalog.sh"),
                                       repo, root, "update_surface"], stderr=subprocess.DEVNULL).decode().strip()
    source = subprocess.check_output(["bash", "-c", '. "$1"; pack_coord_for "$3" "$2"',
                                      "update_surface", os.path.join(lib, "pack_coords.sh"), catalog, pid],
                                     stderr=subprocess.DEVNULL).decode().strip()
    text = git_show(source, tag, "pack-manifest.json")
    return source, json.loads(text) if text else {}


def plan_items(plan, repo, lang):
    """One item per independently appliable unit: FW line, PACK line, or a
    whole ATOMIC group (a required bundle is never offered piecemeal)."""
    with open(plan, "r") as handle:
        lines = [line.rstrip("\n") for line in handle]
    if not lines or lines[0] != "PLAN-EMIT-V1":
        raise ValueError("untrusted plan format")
    result = []
    retires = []
    index = 1
    while index < len(lines):
        line = lines[index]
        fields = line.split("\t")
        if fields[0] == "MIGRATE" and len(fields) in (5, 6):
            _, old, new, tag, member = fields[:5]
            root = members_of(member)[0]["root"]
            installed = dict(x.split("@", 1) for x in read_conf(os.path.join(root, ".instance.conf")).get("DOGANY_PACKS", "").split(",") if "@" in x)
            current = installed.get(old, "")
            target = tag.split("/v", 1)[1]
            item = item_from("PACK", ["PACK", new, "pack", "", current, tag, target, member], [line], repo, lang)
            _, old_manifest = migration_source(repo, root, old, "pack/%s/v%s" % (old, current))
            new_source, new_manifest = migration_source(repo, root, new, tag)
            item["note"]["repo"] = new_source
            item["sections"] = read_note(item["note"], repo)
            item["summary"] = summary(item["sections"], lang)
            raw_name = new_manifest.get("name_ko" if lang == "ko" else "name_en") or new
            item["name"] = product_name(raw_name, "pack", lang)
            item["migration_name"] = raw_name
            item["requires_kit"] = new_manifest.get("requires_kit") or {}
            if len(fields) == 6:
                item["join_kit_tag"] = fields[5]
            item.update(migrate_from=old, old_name=old_manifest.get("name_ko" if lang == "ko" else "name_en") or old, lang=lang)
            result.append(item)
        elif fields[0] == "RETIRE" and len(fields) == 6:
            retires.append((line, fields))
        elif fields[0] == "FW" and len(fields) == 5:
            result.append(item_from("FW", fields, [line], repo, lang))
        elif fields[0] == "PACK" and len(fields) in (8, 9):
            result.append(item_from("PACK", fields, [line], repo, lang))
        elif fields[0] == "ATOMIC":
            group = [line]
            index += 1
            while index < len(lines) and not lines[index].startswith("ATOMIC-END\t"):
                group.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ValueError("truncated atomic group")
            group.append(lines[index])
            packs = [g.split("\t") for g in group if g.startswith("ATOMIC-PACK\t")]
            fws = [g.split("\t") for g in group if g.startswith("ATOMIC-FW\t")]
            if len(packs) != 1:
                raise ValueError("unsupported atomic group")
            includes = [{"name": f[1], "root": f[2], "current": f[3], "target": f[4]} for f in fws]
            item = item_from("PACK", ["PACK"] + packs[0][1:], group, repo, lang, includes)
            for fw in includes:
                if fw["root"] not in item["member_roots"]:
                    item["members"].append(fw["name"])
                    item["member_roots"].append(fw["root"])
                    item["labels"].append(member_label(fw["root"], fw["name"], lang))
            item["key"] = "bundle:" + item_key(item["kind"], item["id"], item["target"], item["member_roots"])
            result.append(item)
        index += 1
    attach_retires(result, retires)
    return result


def held_items(plan, repo, lang):
    """Informational rows, never selectable or passed to the apply engine."""
    result = []
    with open(plan) as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 9 or fields[0] != "HELD-PACK" or fields[8] != "BLOCKED-CREW-DECLARED":
                continue
            item = item_from("PACK", ["PACK"] + fields[1:8], [], repo, lang)
            item["held_reason"] = word(lang, "update.crew_registration_pending")
            result.append(item)
    return result


def attach_retires(items, retires):
    """DGN-1922: a RETIRE line (a retired pack leaving one agent) is not an
    owner choice of its own -- it rides with the item that makes it safe.
    Carrier = the absorbing kit's item covering that agent (the line goes
    AFTER the kit's lines: the kit lands first), else that agent's framework
    item (the line goes FIRST: the framework end state already excludes the
    pack).  No carrier = not offered in this /update (it stays planned and
    rides the next one that touches the agent)."""
    for line, fields in retires:
        kit = fields[2]
        root = os.path.realpath(fields[5].partition(":")[2])
        holds = lambda item: any(os.path.realpath(r) == root for r in item["member_roots"])
        carrier = next((i for i in items if i["kind"] == "kit" and kit in (i["id"], i["crew_kit"]) and holds(i)), None)
        if carrier is not None:
            carrier["lines"].append(line)
            continue
        carrier = next((i for i in items if i["kind"] == "framework" and holds(i)), None)
        if carrier is not None:
            carrier["lines"].insert(0, line)


def plan_usable(rc, plan):
    """DGN-1843: is this planner run a plan?  rc 0 = yes.  rc 2 is BOTH "an
    item is blocked, the rest was planned" (_failline) and "aborted, no
    plan" (_abort); only the former ends its emit with PLAN-END.  One
    member's blocked pack must not read as a lookup failure for every
    agent on the machine; an aborted run still must."""
    if not os.path.isfile(plan):
        return False
    if rc == 0:
        return True
    if rc != 2:
        return False
    try:
        with open(plan, "r") as handle:
            lines = handle.read().splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return False
    return bool(lines) and lines[0] == "PLAN-EMIT-V1" and lines[-1].startswith("PLAN-END\t")


def plan_blocked(plan, root):
    """DGN-1843: the planner's BLOCKED lines attributable to `root` (an
    evidence failure names only the failing member).  Data only -- the
    caller decides whether anyone is told."""
    out = []
    try:
        with open(plan, "r") as handle:
            lines = handle.read().splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return out
    for line in lines[1:]:
        fields = line.split("\t")
        if fields[0] != "BLOCKED" or len(fields) != 5:
            continue
        if any(same_root(m["root"], root) for m in members_of(fields[4]) if m["root"]):
            out.append({"kind": fields[1], "id": fields[2], "status": fields[3]})
    return out


def plan_splits(plan):
    """dec-263 (a): the planner's CREW-SPLIT lines -- a kit crew split
    across versions: [{"id", "mode" (sync | operator), "cand_tag",
    "versions": [[name, version]], "members": [{"name", "root"}],
    "dependents": [pack id], "dependent_owners": {pack id: [root]}}]."""
    try:
        with open(plan, "r") as handle:
            lines = handle.read().splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return []
    owners = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if fields[0] == "BLOCKED" and len(fields) == 5 and fields[1] == "PACK" \
                and fields[3] == "BLOCKED-BY-DEPENDENCY":
            owners.setdefault(fields[2], []).extend(m["root"] for m in members_of(fields[4]))
    out = []
    for line in lines[1:]:
        fields = line.split("\t")
        if fields[0] != "CREW-SPLIT" or len(fields) != 7:
            continue
        deps = fields[6].split()
        out.append({"id": fields[1], "mode": fields[2], "cand_tag": fields[3],
                    "versions": [list(entry.partition("=")[::2]) for entry in fields[4].split()],
                    "members": members_of(fields[5]), "dependents": deps,
                    "dependent_owners": dict((d, owners.get(d, [])) for d in deps)})
    return out


def split_rows(plan, roots, lang):
    """dec-263 (a): the /update rows for every crew split touching `roots`
    -> (rows, splits).  rows = [{"text", "sync": index | None}] in kit then
    dependent order; splits = the alignable splits a [버전 맞추기] tap
    resolves against (the index in a row)."""
    rows, splits = [], []
    found = [s for s in plan_splits(plan)
             if any(same_root(m["root"], root) for m in s["members"] for root in roots)]
    alignable = [s for s in found if s["mode"] == "sync"]
    for split in found:
        tag = split["cand_tag"] or "HEAD"
        kit = unit_display("kit", split["id"], tag, [], lang)["product"]
        kit_bare = unit_display("kit", split["id"], tag, [], lang, tagged=False)["product"]
        roots_of = dict((m["name"], m["root"]) for m in split["members"])
        versions = ", ".join(word(lang, "update.crew_split_version").format(
            agent=agent_label(roots_of.get(name, ""), name, lang), version=vfmt(version, split["id"]))
            for name, version in split["versions"])
        sync = split["mode"] == "sync"
        if sync:
            index = len(splits)
            splits.append({"id": split["id"], "cand_tag": split["cand_tag"], "versions": split["versions"],
                           "dependents": split["dependents"], "name": kit_bare})
            rows.append({"text": word(lang, "update.crew_split").format(kit=kit, versions=versions),
                         "sync": index,
                         "button": (word(lang, "update.crew_sync_button") if len(alignable) == 1 else
                                    word(lang, "update.crew_sync_button_named").format(kit=kit_bare))})
        else:
            rows.append({"text": word(lang, "update.crew_split_operator").format(kit=kit), "sync": None})
        dep_line = (word(lang, "update.crew_split_dependent") if sync
                    else word(lang, "update.crew_split_dependent_operator"))
        for dep in split["dependents"]:
            if not any(same_root(r, root) for r in split["dependent_owners"].get(dep, []) for root in roots):
                continue
            pack = unit_display("pack", dep, "HEAD", [], lang)["product"]
            rows.append({"text": dep_line.format(pack=pack, kit=kit_bare), "sync": None})
    return rows, splits


def split_view(ledger):
    """The split rows' HTML (one quote each, a blank line apart) and their
    [버전 맞추기] buttons for one /update ledger."""
    prefix = "%s:%s" % (ledger["id"], ledger["revision"])
    rows = ledger.get("split_rows") or []
    body = "\n\n".join("<blockquote>%s</blockquote>" % html.escape(row["text"], quote=False) for row in rows)
    buttons = [{"text": row["button"], "data": "upd:s:%s:%d" % (prefix, row["sync"])}
               for row in rows if row.get("sync") is not None]
    return body, buttons


def semver(repo):
    """scripts/pack/lib/semver_range.py -- the single semver source."""
    path = os.path.join(repo, "scripts", "pack", "lib", "semver_range.py")
    spec = importlib.util.spec_from_file_location("dogany_semver_range", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def satisfies(repo, version, rng):
    """None when it cannot be evaluated (the caller treats that as 'required')."""
    try:
        return semver(repo).satisfies(norm(version), rng)
    except Exception:
        return None


def version_span(repo, versions):
    """(lowest, highest) by semver precedence; unparseable -> text order."""
    try:
        cmp = semver(repo).cmp_precedence
    except Exception:
        cmp = None

    def order(a, b):
        c = cmp(norm(a), norm(b)) if cmp else None
        if c is None:
            c = (norm(a) > norm(b)) - (norm(a) < norm(b))
        return c
    ranked = sorted(versions, key=functools.cmp_to_key(order))
    return ranked[0], ranked[-1]


def crew_dir():
    return os.environ.get("DOGANY_CREW_DIR", os.path.join(shared_home(), "crews"))


def crew_groups(items):
    """Agents that form a crew: the members of every crew.conf (the planner's
    own crew discovery source) plus the members of any crew kit in this plan;
    an agent in two crews joins them."""
    groups = []
    directory = crew_dir()
    if os.path.isdir(directory):
        for kit in sorted(os.listdir(directory)):
            try:
                with open(os.path.join(directory, kit, "crew.conf"), "r") as handle:
                    roots = set(os.path.realpath(line.strip()[len("member="):])
                                for line in handle if line.strip().startswith("member="))
            except (IOError, OSError):
                continue
            if roots:
                groups.append(roots)
    for item in items:
        if item["needs_consent"] and not item["key"].startswith("bundle:"):
            groups.append(set(os.path.realpath(r) for r in item["member_roots"]))
    joined = []
    for group in groups:
        group = set(group)
        rest = []
        for other in joined:
            if other & group:
                group |= other
            else:
                rest.append(other)
        joined = rest + [group]
    return joined


def merge_framework(items, repo):
    """Item 26: ONE framework row per crew at the same target, applied to
    every member.  `currents` keeps each member's installed version (the row
    shows one line per agent, DGN-1744); `current` is the lowest and
    `current_high` the highest, for the ledger."""
    groups = crew_groups(items)

    def group_of(item):
        real = os.path.realpath(item["member_roots"][0])
        for index, group in enumerate(groups):
            if real in group:
                return ("crew", index, norm(item["target"]))
        return ("solo", real, norm(item["target"]))
    buckets = {}
    for item in items:
        if item["kind"] == "framework":
            buckets.setdefault(group_of(item), []).append(item)
    out = []
    done = set()
    for item in items:
        if item["kind"] != "framework":
            out.append(item)
            continue
        gid = group_of(item)
        if gid in done:
            continue
        done.add(gid)
        group = buckets[gid]
        if len(group) == 1:
            out.append(item)
            continue
        merged = dict(group[0])
        low, high = version_span(repo, [g["current"] for g in group])
        merged.update({
            "key": item_key("framework", "+".join(sorted(g["id"] for g in group)), group[0]["target"], [g["member_roots"][0] for g in group]),
            "members": [g["members"][0] for g in group],
            "member_roots": [g["member_roots"][0] for g in group],
            "labels": [g["labels"][0] for g in group],
            "lines": [line for g in group for line in g["lines"]],
            "currents": [[g["members"][0], norm(g["current"])] for g in group],
            "current": low, "current_high": high if norm(high) != norm(low) else "",
            "who": [g["labels"][0] for g in group]})
        out.append(merged)
    return out


def merge_framework_targets(items, repo):
    """dec-465 (owner 2026-10-17): the framework is ONE card per target for
    every agent in scope -- crew or not.  Runs on the scope-cut items (after
    merge_framework's crew pass), so a scoped /update or notice never folds in
    an agent outside its scope.  Kits stay per crew, agent packs per agent."""
    buckets = {}
    for item in items:
        if item["kind"] == "framework":
            buckets.setdefault(norm(item["target"]), []).append(item)
    out, done = [], set()
    for item in items:
        if item["kind"] != "framework":
            out.append(item)
            continue
        target = norm(item["target"])
        if target in done:
            continue
        done.add(target)
        group = buckets[target]
        if len(group) == 1:
            out.append(item)
            continue
        members = [m for g in group for m in g["members"]]
        roots = [r for g in group for r in g["member_roots"]]
        # one (member, installed version) pair per member, in member order
        currents = [pair for g in group for pair in
                    (g.get("currents") or [[g["members"][0], norm(g["current"])]])]
        low, high = version_span(repo, [version for _, version in currents])
        merged = dict(group[0])
        merged.update({
            "key": item_key("framework", "+".join(sorted(members)), item["target"], roots),
            "members": members, "member_roots": roots,
            "labels": [l for g in group for l in g["labels"]],
            "lines": [line for g in group for line in g["lines"]],
            "currents": currents,
            "current": low, "current_high": high if norm(high) != norm(low) else "",
            "who": [l for g in group for l in g["labels"]]})
        out.append(merged)
    return out


def link_requirements(items, repo):
    """A pack whose candidate needs a newer kit than the installed one names
    that kit item: applying the pack alone would fail its requires_kit gate
    (DGN-1682), so the kit rides with it."""
    kits = dict((i["id"], i) for i in items if i["kind"] == "kit" and not i["key"].startswith("bundle:"))
    for item in items:
        if item.get("join_kit_tag"):
            continue  # this member switch already carries its exact kit join
        need = item.get("requires_kit") or {}
        kit = kits.get(need.get("kit"))
        if kit is None or kit is item:
            continue
        if satisfies(repo, kit["current"], need.get("range", "")) is not True:
            item["requires"].append(kit["key"])


def touches(item, root):
    roots = list(item["member_roots"]) + [fw["root"] for fw in item["includes"]]
    return any(same_root(r, root) for r in roots)


def health_observer(repo):
    """routines/health-observer.py of the machinery repo: the DGN-1745 crew
    leader rule (read_crews / crew_leader) lives there, once."""
    path = os.path.join(repo, "agents", ".template", "routines", "health-observer.py")
    spec = importlib.util.spec_from_file_location("dogany_health_observer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def crew_scope(instance_root, repo):
    """(me, [(leader_root, [member roots]), ...]) for every crew.conf that
    lists this instance, in crew order -- the ONE crew read behind both the
    notice's leader rule (offer_scope) and /update's member set
    (crew_roots).  The leader is the DGN-1745 rule: the first member, in
    crew.conf order, whose class is not domain (a crew with no such member:
    its first member).  No crew (or an unreadable crew dir): []."""
    me = os.path.realpath(instance_root)
    try:
        observer = health_observer(repo)
        crews = observer.read_crews(crew_dir())
    except Exception:
        return me, []
    mine = []
    for kit in sorted(crews):
        members = [os.path.realpath(m) for m in crews[kit]]
        # Pending declarations still belong to the owner's update view. They
        # never enter the planner's kit membership or the apply transaction.
        pending = os.path.join(crew_dir(), kit, "crew.declared-pending")
        try:
            with open(pending) as handle:
                rows = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
            if all(row.startswith("member=/") for row in rows):
                members += [os.path.realpath(row[len("member="):]) for row in rows
                            if os.path.realpath(row[len("member="):]) not in members]
        except OSError:
            pass
        if me in members:
            leader = observer.crew_leader(crews[kit]) or members[0]
            mine.append((os.path.realpath(leader), members))
    return me, mine


def offer_scope(instance_root, repo):
    """DGN-1749: (leader_root, [crew member roots]) for the notice.  An offer
    covers one crew and only that crew's leader delivers it.  A standalone
    agent is its own leader.  An agent leading several crews speaks for
    each of them."""
    me, mine = crew_scope(instance_root, repo)
    if not mine:
        return me, [me]
    roots, first_leader = [], None
    for leader, members in mine:
        first_leader = first_leader or leader
        if leader == me:
            roots += [m for m in members if m not in roots]
    return (me, roots) if roots else (first_leader, [])


def crew_roots(instance_root, repo):
    """DGN-1784: /update's member set -- the members of every crew this
    instance belongs to, leader or not (a standalone agent: itself)."""
    me, mine = crew_scope(instance_root, repo)
    roots = []
    for _, members in mine:
        roots += [m for m in members if m not in roots]
    return roots or [me]


def installed_layers(repo, roots, lang):
    """DGN-1790: the installed version of every layer on `roots`, read from
    the records the planner itself reads (update_plan.sh), never invented:
      framework  .instance.conf DOGANY_FW_VERSION, shown as the exact build
                 (DOGANY_FW_TAG, DGN-1723 -- version-check._installed_build)
      kit / pack .instance.conf DOGANY_PACKS <id>@<version> (pack_install
                 records the installed tag's full version); kind and name from
                 the pack manifest at that tag, the slug when unreadable
      modules    no record of their own: a module ships inside the framework
                 or a kit/pack and moves with it -> one fixed row, no number
    A root without a readable .instance.conf contributes nothing.  Returns
    [{"kind", "id", "name", "versions": [{"version", "agents"}]}], framework
    first, then kits, then packs; a missing version is "" (never a guess)."""
    vc = version_check(repo)
    layers = {}
    for root in roots:
        conf_path = os.path.join(root, ".instance.conf")
        if not os.path.isfile(conf_path):
            continue
        conf = read_conf(conf_path)
        label = agent_label(root, conf.get("DOGANY_AGENT_NAME") or os.path.basename(root), lang)
        found = [("framework", "framework", product_name("", "framework", lang),
                  norm(vc._installed_build(conf.get("DOGANY_FW_VERSION", ""), conf.get("DOGANY_FW_TAG", ""))))]
        for entry in conf.get("DOGANY_PACKS", "").replace(",", " ").split():
            pid, _, version = entry.partition("@")
            if not pid:
                continue
            manifest = pack_manifest(pid, "pack/%s/v%s" % (pid, norm(version))) if norm(version) else {}
            kind = "kit" if (manifest.get("kind") == "kit" or manifest.get("provides_kit")) else "pack"
            raw = manifest.get("name_ko" if lang == "ko" else "name_en") or ""
            raw = raw if isinstance(raw, str) and raw.strip() else pid
            name = product_name(raw, layer_kind(manifest.get("kind"), manifest.get("provides_kit")), lang)
            found.append((kind, pid, name, norm(version)))
        for kind, pid, name, version in found:
            layer = layers.setdefault((kind, pid), {"kind": kind, "id": pid, "name": name, "versions": []})
            group = next((g for g in layer["versions"] if g["version"] == version), None)
            if group is None:
                group = {"version": version, "agents": []}
                layer["versions"].append(group)
            if label not in group["agents"]:
                group["agents"].append(label)
    order = {"framework": 0, "kit": 1, "pack": 2}
    return sorted(layers.values(), key=lambda layer: order[layer["kind"]])


def versions_text(repo, roots, lang, layers=None, include_header=True):
    """DGN-1790: the "installed versions" block both /update moments show
    (nothing to apply; after an apply).  HTML, the result quote's layout;
    agent names only when more than one agent is in scope.  "" when nothing
    is readable or the framework i18n copy is missing (the caller then says
    what it said before)."""
    vc = version_check(repo)
    layers = installed_layers(repo, roots, lang) if layers is None else layers
    # Owner 2026-09-29: show only layers that carry a version -- no
    # "no version" row and no modules row (modules/skills have no version
    # source of their own; they ride the framework or a kit/pack).
    copy_keys = ("update.version_row", "update.version_row_agents")
    if include_header:
        copy_keys += ("update.versions_header",)
    words = dict((key, vc._i18n_get(lang, key)) for key in copy_keys)
    if not layers or not all(words.values()):
        return ""
    many = len([r for r in roots if os.path.isfile(os.path.join(r, ".instance.conf"))]) > 1
    rows = []
    for layer in layers:
        for group in layer["versions"]:
            version = vfmt(group["version"], layer["id"] if layer["kind"] != "framework" else None)
            if not version:
                continue
            if many:
                rows.append(words["update.version_row_agents"].format(
                    product=layer["name"], version=version, agents=AGENT_SEP.join(group["agents"])))
            else:
                rows.append(words["update.version_row"].format(product=layer["name"], version=version))
    if not rows:
        return ""
    block = "<blockquote>%s</blockquote>" % html.escape("\n".join(rows))
    return html.escape(words["update.versions_header"]) + "\n" + block if include_header else block


def automatic_offer_items(items):
    """T17: unsolicited offers cannot recommend dev framework installs.

    An atomic bundle or a dependent pack must not smuggle a dev install
    into a notice. Owner-requested listings keep the complete plan.
    """
    blocked = {item["key"] for item in items
               if (item["kind"] == "framework" and "-dev." in item["target"])
               or any("-dev." in fw["target"] for fw in item.get("includes", []))}
    while True:
        expanded = blocked | {item["key"] for item in items
                              if blocked.intersection(item.get("requires", []))}
        if expanded == blocked:
            return [item for item in items if item["key"] not in blocked]
        blocked = expanded


def new_ledger(args, source, lang, scope=None, persist=True):
    """scope: "offer" = the notice's leader-gated crew (offer_scope), "crew"
    = /update's member set (crew_roots), None = every layer the planner
    found (the round-2 notice button only resolves this instance's row)."""
    items = merge_framework(plan_items(args.plan, args.repo, lang), args.repo)
    held = held_items(args.plan, args.repo, lang)
    if scope:
        # Every layer touching one of the crew's members -- framework, kit,
        # packs (DGN-1749 for the notice, DGN-1784 for /update).  Other
        # crews announce their own.
        if scope == "offer":
            _, roots = offer_scope(args.instance_root, args.repo)
        else:
            roots = crew_roots(args.instance_root, args.repo)
        items = [item for item in items if any(touches(item, root) for root in roots)]
        held = [item for item in held if any(touches(item, root) for root in roots)]
    # dec-465: merge AFTER the scope cut, so a scoped /update or notice
    # never folds in an agent outside its scope.
    items = merge_framework_targets(items, args.repo)
    link_requirements(items, args.repo)
    if source == "offer":
        items = automatic_offer_items(items)
        held = automatic_offer_items(held)
    for item in items:
        item["caption"] = version_line(item, lang)
    ledger_id = str(uuid.uuid4())
    ledger = {"schema": 3, "revision": 1, "id": ledger_id, "created_at": int(time.time()),
              "source": source, "state": "selecting", "requester": str(args.requester),
              "chat": str(args.chat), "repo": os.path.abspath(args.repo),
              "instance_root": os.path.abspath(args.instance_root), "lang": lang,
              "plan": os.path.abspath(args.plan), "items": items, "held_items": held, "selection": None,
              "consent": [], "tapped_at": None, "supervisor": None, "result": None,
              "delivery": None}
    if persist:
        atomic_write(ledger_path(ledger_id), ledger)
    return ledger


def item_for(ledger, key):
    return next((item for item in ledger["items"] if item["key"] == key), None)


def selection_keys(ledger):
    sel = ledger.get("selection")
    if not sel:
        return []
    return [sel] if isinstance(sel, str) else list(sel)


def selected(ledger):
    return [item for item in (item_for(ledger, key) for key in selection_keys(ledger)) if item]


def closure(ledger, picked):
    """The picked items plus every item they require, in plan order (the
    planner already orders kit -> pack -> framework)."""
    want = set(item["key"] for item in picked)
    grew = True
    while grew:
        grew = False
        for item in ledger["items"]:
            if item["key"] in want:
                for req in item.get("requires") or []:
                    if req not in want and item_for(ledger, req):
                        want.add(req)
                        grew = True
    return [item for item in ledger["items"] if item["key"] in want]


# --------------------------------------------------------------------------
# Owner copy builders (ko final, en mirrored).
# --------------------------------------------------------------------------

def _batchim(word):
    ch = word[-1:] if word else ""
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    return ch in "0136789LMNRlmnr"


def with_josa(word, after_consonant, after_vowel):
    return word + (after_consonant if _batchim(word) else after_vowel)


def join_and(names, lang):
    names = list(names)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if lang == "ko":
        return ", ".join(names[:-2] + [with_josa(names[-2], "과", "와") + " " + names[-1]])
    return ", ".join(names[:-1]) + " and " + names[-1]


def version_groups(item):
    """DGN-1749 (owner-locked): [(labels, current)] -- the agents a row
    covers, those on the same installed version on ONE entry, a separate
    entry only per distinct version, member order kept.  A crew framework
    row carries each member's own version (`currents`); any other row has
    one installed version for all its agents."""
    currents = item.get("currents") or []
    labels = list(item.get("labels") or [])
    if len(currents) >= 2:
        pairs = [(label, norm(current)) for label, (_, current) in zip(labels, currents)]
    else:
        pairs = [(label, norm(item["current"])) for label in labels]
    groups = []
    for label, current in pairs:
        for names, version in groups:
            if version == current:
                names.append(label)
                break
        else:
            groups.append(([label], current))
    return groups


def crew_rows(item):
    """The version groups of a several-agent framework row, [] otherwise."""
    return version_groups(item) if len(item.get("currents") or []) >= 2 else []


def from_version(item):
    """vA -- one agent's installed version.  A several-agent row never
    squeezes its versions into a range; see version_groups."""
    return vfmt(item["current"], _pid(item))


def version_line(item, lang="en"):
    """One line, the target named once (DGN-1749 -- no "Dogany → vB" head
    plus a repeated line per agent, never a "vA~A2" range):
        Dogany vA → vB                        one agent
        Dogany vA → vB (<agent>)              another agent's row
        Dogany vA → vB (<a1> · <a2>)          a crew row, one installed version
        Dogany → vB (<a1> vA1, <a2> vA2)      a crew row, several versions"""
    target = target_text(item, lang)
    rows = crew_rows(item)
    if len(rows) > 1:
        return "%s → %s (%s)" % (item["name"], target, ", ".join(
            "%s %s" % (AGENT_SEP.join(labels), vfmt(current, _pid(item))) for labels, current in rows))
    line = "%s %s → %s" % (item["name"], from_version(item), target)
    if rows:
        line += " (%s)" % AGENT_SEP.join(rows[0][0])
    elif item.get("who"):
        line += " (%s)" % ", ".join(item["who"])
    return line


# DGN-1778: the start text is sent by the PRE-update bridge, which can be
# older than this script (it runs from the canonical checkout).  A bridge
# that renders Telegram HTML declares it in this env var; without the
# declaration the start text is plain (same content, no tags) so an older
# bridge that ignores parse_mode never shows raw markup.
RENDER_ENV = "DOGANY_UPDATE_RENDER"


def caller_renders_html():
    return os.environ.get(RENDER_ENV, "").strip().lower() == "html"


# DGN-2041: a bridge declares that it reads a started outcome without text
# as "started, nothing to say" (DGN-2027).  The machinery runs from the repo
# checkout while each instance's bridge can lag a release behind; an older
# bridge turns a missing text into UPDATE_STALE -- a "changed, pick again"
# line next to an apply that really started (owner report 2026-10-12).  So
# the start line is left out only for a caller that declared it.
SILENT_START_ENV = "DOGANY_UPDATE_SILENT_START"

# DGN-2154: update_surface runs from the canonical checkout even when the
# calling bridge is one release behind.  dev.5 does not know the `queued`
# action, but it already renders `busy` text.  Only callers that explicitly
# advertise queued support receive the newer action name.
QUEUED_ACTION_ENV = "DOGANY_UPDATE_QUEUED"


def caller_takes_silent_start():
    return os.environ.get(SILENT_START_ENV, "").strip() == "1"


def queued_reply(outcome):
    outcome["action"] = "queued" if os.environ.get(QUEUED_ACTION_ENV, "").strip() == "1" else "busy"
    if outcome["action"] == "busy":
        # Old adapters treat busy=True without ask buttons as a duplicate
        # and toast instead of displaying the queued line. Their busy
        # action still renders text when that duplicate-status flag is absent.
        outcome.pop("busy", None)
    return outcome


def start_text(lang, as_html=True):
    """DGN-1781 (owner 2026-09-28 16:43): the start line alone -- the items
    are already on the screen the owner tapped, so no version quote and no
    crew line.  Escaped when the caller renders HTML (DGN-1776/1778)."""
    line = copy(lang)["start"]
    return html.escape(line, quote=False) if as_html else line


def migration_line(item, lang, done=False):
    old, new = item.get("old_name", ""), item.get("migration_name", item["name"])
    if lang == "ko" and not done:
        old = with_josa(old, "이", "가")
        # Directional particle: no final or final rieul takes -ro, other finals -euro.
        code = ord(new[-1]) - 0xAC00 if new else -1
        final = code % 28 if 0 <= code < 11172 else 0
        new += "으로" if final not in (0, 8) else "로"
    text = copy(lang)["migrate_done" if done else "migrate_offer"].format(
        old_name=old, new_name=new, target=norm(item["target"]))
    if item.get("join_kit_tag"):
        text += copy(lang)["migrate_join"]
    return text


def busy_item(item, lang):
    if item.get("migrate_from"):
        return migration_line(item, lang)
    rows = crew_rows(item)
    if rows:
        return copy(lang)["busy_crew"].format(name=item["name"], rows=", ".join(
            "%s %s → %s" % (AGENT_SEP.join(labels), vfmt(current, _pid(item)), target_text(item, lang)) for labels, current in rows))
    return copy(lang)["busy_item"].format(name=item["name"], current=from_version(item),
                                          target=target_text(item, lang))


def busy_text(other, key="busy_ask"):
    lang = other.get("lang", "ko")
    running = [busy_item(item, lang) for item in selected(other)]
    return copy(lang)[key].format(running=", ".join(running))


def bundle_lines(item, deps, lang):
    out = []
    for dep in deps:
        out.append(copy(lang)["bundle"].format(name=item["name"], target=target_text(item, lang),
                                               dep=dep["name"], dep_target=target_text(dep, lang)))
    for fw in item["includes"]:
        out.append(copy(lang)["bundle"].format(name=item["name"], target=target_text(item, lang),
                                               dep=product_name("", "framework", lang), dep_target=included_framework_target(fw, lang)))
    return out


def done_line(item, lang):
    if item.get("migrate_from"):
        return migration_line(item, lang, done=True)
    if len(item["labels"]) > 1 and (item["needs_consent"] or item["kind"] == "framework"):
        return copy(lang)["done_crew"].format(name=item["name"], target=target_text(item, lang),
                                              all=join_and(item["labels"], lang))
    return copy(lang)["done_item"].format(name=item["name"], target=target_text(item, lang))


def _visible_len(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def count_word(n, lang):
    if lang == "ko":
        return COUNT_KO.get(n, "%d명의" % n)
    return "both" if n == 2 else "all %d" % n


def offer_notes(item, lang):
    """The dependency lines under 적용대상: every layer riding on the row
    ("<kit> vD이 함께 필요해요."), then the crew-kit line when the row IS a
    kit shared by several agents (the needed kit has its own row, which
    carries that line)."""
    c = copy(lang)
    deps = item.get("offer_deps") or []
    needs = ["%s %s" % (dep["name"], target_text(dep, lang)) for dep in deps]
    needs += ["%s %s" % (product_name("", "framework", lang), included_framework_target(fw, lang)) for fw in item.get("includes") or []]
    out = [c["needs"].format(dep=with_josa(need, "이", "가") if lang == "ko" else need) for need in needs]
    if item.get("needs_consent") and len(item["labels"]) >= 2:
        out.append(c["crew_kit"].format(n=count_word(len(item["labels"]), lang)))
    return out


def offer_lines(item, lang):
    """The HTML lines of one offer item (dec-222, owner 2026-10-01 06:54,
    supersedes the DGN-1781 "· <product>: <target>" row):
        <b><product> <target></b>   (dec-234: no leading triangle)
        └ 적용대상: <a1>·<a2> (현재 <current>)    one line per distinct version
        <dependency / crew-kit lines>"""
    head = word(lang, "update.item_head").format(product=item["name"], target=target_text(item, lang))
    target_key = "update.offer_migration_target" if item.get("migrate_from") else "update.offer_target"
    lines = [word(lang, target_key).format(agents=AGENT_SEP.join(labels), current=vfmt(current, _pid(item)),
                                         target=target_text(item, lang))
             for labels, current in version_groups(item)]
    if item.get("migrate_from"):
        lines.append(migration_line(item, lang))
    if item.get("held_reason"):
        lines.append(item["held_reason"])
    else:
        lines += offer_notes(item, lang)
    return ["<b>%s</b>" % html.escape(head, quote=False)] + [html.escape(line, quote=False) for line in lines]


def confirm_header(n, lang):
    """DGN-1776 confirm-screen header: the count word for n=2/3/4 (ko),
    "{n}" + counter from 5 on, singular form for 1; the house emoji in
    front (DGN-1781)."""
    c = copy(lang)
    if n == 1:
        return c["confirm_header"]
    if lang == "ko":
        count = {2: "두", 3: "세", 4: "네"}.get(n, "%d개" % n)
    else:
        count = str(n)
    return c["confirm_header_n"].format(count=count)


def render_offer(items, lang, header=None, group_in_progress=False):
    """DGN-1749 (owner-locked 2026-09-27): header line, then per item the
    plain quote (product + target, 적용대상) followed on the very next line
    -- one newline, no blank line -- by one expandable quote holding the
    bold ▸ summary / ▸ detail sections from that layer's note, a blank line
    between them (empty omitted, never the try-section at availability time;
    no notes -> no expandable quote).  Items are one blank line apart."""
    c = copy(lang)
    detail_cap = DETAIL_MAX_CHARS

    def parts_for(item, keep):
        parts = []
        for section in item["sections"]:
            if section.get("key") == "_summary" or section["title"] == c["summary"]:
                label, body = c["summary"], section["body"]
            elif section.get("key") == "_detail" or section["title"] == c["detail"]:
                if keep < 2:
                    continue
                label, body = c["detail"], section["body"]
                if len(body) > detail_cap:
                    body = body[:detail_cap] + "\n" + c["truncated"]
            else:
                continue  # "새로 해볼 수 있는 것" is completion-only
            if keep < 1:
                continue
            parts.append("<b>▸ %s</b>\n%s" % (html.escape(label, quote=False), html.escape(body, quote=False)))
        return parts

    if header is None:
        header = c["header"] if len(items) == 1 else c["header_n"].format(n=len(items))

    def build(levels):
        # DGN-1790 r3: one quote per item, items one blank line apart; an
        # item's note fold sits right under that item's quote.
        blocks = []
        for item, keep in zip(items, levels):
            block = "<blockquote>%s</blockquote>" % "\n".join(offer_lines(item, lang))
            parts = parts_for(item, keep)
            if parts:
                # ONE "\n" between the two quotes: with none, Telegram
                # merges the fold into the plain quote; a blank line would
                # open a gap the owner rejected (DGN-1749 review).
                block += "\n<blockquote expandable>%s</blockquote>" % "\n\n".join(parts)
            if group_in_progress:
                applying = bool(item.get("offer_in_progress"))
                if not blocks or applying != bool(items[len(blocks) - 1].get("offer_in_progress")):
                    group_header = word(lang, "update.offer_applying_header") if applying else c["header"]
                    block = group_header + "\n" + block
            blocks.append(block)
        return ("" if group_in_progress else header + "\n") + "\n\n".join(blocks)

    # Too long for one message: drop details from the last item upwards,
    # then summaries -- the plain quotes always stay.
    levels = [2] * len(items)
    text = build(levels)
    for floor in (1, 0):
        for index in reversed(range(len(items))):
            if _visible_len(text) <= NOTICE_MAX_CHARS:
                return text
            levels[index] = min(levels[index], floor)
            text = build(levels)
    return text


def shown_state(item):
    """What a row showed: key + installed version(s).  A tap is honoured only
    while the fresh plan still says the same."""
    entry = [item["key"], norm(item["current"])]
    if item.get("currents"):
        entry.append(sorted(item["currents"]))
    return entry


def offer_signature(items):
    return json.dumps(sorted(shown_state(item) for item in items))


ROW_ORDER = {"framework": 0, "kit": 1, "pack": 2}


def offer_rows(ledger):
    """DGN-1749 (owner-locked): one row per item, Dogany first, then the
    kit, then the packs.  A kit a pack needs keeps its own row (it carries
    the crew-kit line) and still rides on the pack's row: the pack's quote
    names it ("<kit> vD이 함께 필요해요.") and the pack's tap applies both
    (item 22, no after-tap confirm)."""
    rows = []
    for item in sorted(ledger["items"], key=lambda item: ROW_ORDER.get(item["kind"], len(ROW_ORDER))):
        deps = [other for other in closure(ledger, [item]) if other["key"] != item["key"]]
        rows.append({"key": item["key"], "with": [dep["key"] for dep in deps]})
    return rows


def offer_pick_rows(rows, pick):
    """Resolve legacy all/row taps and explicit all-new row indices."""
    index = pick[1:] if pick.startswith("b") else pick
    if pick == "a":
        return list(rows)
    if pick.startswith("a"):
        indices = pick[1:].split(",")
    else:
        indices = [index]
    if (not all(value.isdigit() and int(value) < len(rows) for value in indices)
            or len(set(indices)) != len(indices)):
        raise ValueError("stale offer pick")
    return [rows[int(value)] for value in indices]


def offer_state_path(instance_root):
    return os.path.join(instance_root, ".telegram_bot", "state", "update-offer.json")


def read_offer_state(instance_root):
    try:
        with open(offer_state_path(instance_root), "r") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def remember_channel_failure(ledger, record):
    """Keep the release wait across bridge restarts and ask-once dedup."""
    pending = [u["reason_payload"] for u in record.get("units", [])
               if u.get("reason_code") == "kit.channel-unavailable"
               and (u.get("reason_payload") or {}).get("required_version")]
    if not pending:
        return
    root, _ = offer_scope(ledger["instance_root"], ledger["repo"])
    state = read_offer_state(root)
    state["deferred"] = list({json.dumps(p, sort_keys=True): p
                              for p in state.get("deferred", []) + pending}.values())
    atomic_write(offer_state_path(root), state)


def released_waits(repo, root):
    """Read the same channel gate again; an unreadable source never releases a wait."""
    pending = read_offer_state(root).get("deferred") or []
    if not pending:
        return []
    lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from kit_channel import check
    try:
        catalog = subprocess.check_output(["bash", os.path.join(lib, "canonical_catalog.sh"),
                                           repo, root, "update_surface"], stderr=subprocess.DEVNULL).decode().strip()
        return [p for p in pending if not check({"contract_version": 1, "requires_kit": {
            "kit": p["kit"], "range": p["range"]}}, catalog, p.get("channel", "stable"),
            os.path.join(root, ".instance.conf"))]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return []


def deferred_ready(repo, root):
    return bool(released_waits(repo, root))


# --------------------------------------------------------------------------
# Verbs
# --------------------------------------------------------------------------

def cmd_list(args):
    """/update (DGN-1775): the SAME screen as the session-start notice --
    render_offer text, offer_view buttons, the same tap flow (upd:o/b/p ->
    offer-view / pick).  One renderer for both doors.  What stays /update's
    own: no leader gate and no ask-once record.  Its item set (DGN-1784,
    owner 2026-09-28) is the crew's -- every layer touching a member of any
    crew this instance belongs to, leader or not; a standalone agent: only
    itself.  The ledger
    stays "selecting", so the item verbs (apply/consent) still resolve."""
    lang = notice_lang(args.instance_root, args.repo)
    ledger = new_ledger(args, "update", lang, scope="crew", persist=False)
    if ledger["items"]:
        ledger["rows"] = offer_rows(ledger)
    # dec-263 (a): a kit crew split across versions is a row of its own.
    ledger["split_rows"], ledger["splits"] = split_rows(
        args.plan, crew_roots(args.instance_root, args.repo), lang)
    atomic_write(ledger_path(ledger["id"]), ledger)
    out = {"ledger": ledger["id"], "revision": 1, "items": ledger["items"],
           "blocked": plan_blocked(args.plan, args.instance_root)}
    if ledger["items"]:
        out.update(offer_view(ledger))
    elif ledger.get("held_items") or ledger["split_rows"]:
        blocks, buttons = [], []
        if ledger.get("held_items"):
            blocks.append(render_offer(ledger["held_items"], lang))
        if ledger["split_rows"]:
            body, buttons = split_view(ledger)
            blocks.append(body)
        out.update(html="\n\n".join(blocks), buttons=buttons)
    else:
        # DGN-1790: nothing to apply -> say so and show what is installed.
        # No "text" (copy or records unreadable) = the bridge's own line.
        # DGN-1947: "nothing to apply" is only said after a fresh remote
        # read; the planner marks a framework it could not check on origin
        # (UNCHECKED-REMOTE) and that is worded as "could not check".
        roots = crew_roots(args.instance_root, args.repo)
        unchecked = any(b["kind"] == "FW" and b["status"] == "UNCHECKED-REMOTE"
                        for r in roots for b in plan_blocked(args.plan, r))
        block = versions_text(args.repo, roots, lang, include_header=False)
        none = version_check(args.repo)._i18n_get(lang, "update.none_unchecked" if unchecked else "update.none")
        if none and (block or unchecked):
            text = html.escape(none)
            if block and not unchecked:
                text += "\n\n" + block
            out.update(text=text, parse_mode="HTML")
    print(json.dumps(out, ensure_ascii=False))


# FDGN-467 / DGN-2053: after an update the agent itself offers the "try it"
# items of every version in (previous installed, new installed].
TRY_LIMIT = 3
TRY_COPY = {
    "ko": {"head": "이번 업데이트에서 이런 걸 해볼 수 있어요. 해보실래요?",
           "more": "그 밖의 항목은 릴리즈 노트에서 볼 수 있어요."},
    "en": {"head": "This update adds things you can try. Want to try them?",
           "more": "More in the notes."},
}


def _words(text):
    return set(re.findall(r"[0-9A-Za-z\u3131-\u318e\uac00-\ud7a3]{2,}", (text or "").lower()))


def try_items_since(repo, commit, previous, new, lang, machinery=None):
    """The "try it" bullet lines of every release note in (previous, new],
    oldest version first.  A missing or empty note contributes nothing."""
    root = machinery or repo
    # The try offer is an extra line on the result notice; a machinery tree
    # without the semver lib (old trees, test fixtures) offers nothing rather
    # than breaking the notice it rides on.
    if not os.path.isfile(os.path.join(root, "scripts", "pack", "lib", "semver_range.py")):
        return []
    vc = version_check(root)
    cmp = semver(root).cmp_precedence
    listing = subprocess.run(["git", "-C", repo, "ls-tree", "--name-only", commit, "releases/"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    versions = []
    for name in listing.stdout.decode("utf-8", "replace").split():
        match = re.fullmatch(r"releases/v(.+)\.md", name)
        if match:
            versions.append(match.group(1))
    prev, last = norm(previous), norm(new)
    chosen = [v for v in versions
              if (not prev or cmp(v, prev) == 1) and cmp(v, last) in (-1, 0)]
    chosen.sort(key=functools.cmp_to_key(lambda a, b: cmp(a, b) or 0))
    items = []
    for version in chosen:
        text = git_show(repo, commit, "releases/v%s.md" % version)
        body = vc._strip_tickets(vc._user_summary_from_note(text or "", lang + "_try", max_lines=None)).strip()
        for line in body.splitlines():
            line = line.strip().lstrip("\u00b7-* ").strip()
            if line:
                items.append({"version": version, "text": line})
    return items


def rank_try_items(items, profile, limit=TRY_LIMIT):
    """<= limit: all, in version order.  More: rank by fit to this user's
    needs (word overlap with profile = owner file + recent use), then by
    improvement size (longer described change first), then newer version."""
    if len(items) <= limit:
        return list(items), []
    need = _words(profile)
    order = {id(item): index for index, item in enumerate(items)}
    ranked = sorted(items, key=lambda i: (-len(_words(i["text"]) & need), -len(i["text"]), -order[id(i)]))
    top = ranked[:limit]
    top.sort(key=lambda i: order[id(i)])
    return top, [i for i in items if i not in top]


def render_try_offer(items, lang, profile="", limit=TRY_LIMIT):
    """One message; "" when no version brought a try item."""
    if not items:
        return ""
    c = TRY_COPY["ko" if lang == "ko" else "en"]
    top, rest = rank_try_items(items, profile, limit)
    lines = [c["head"]] + ["\u00b7 " + i["text"] for i in top]
    if rest:
        lines.append(c["more"])
    return "\n".join(lines)


def cmd_try_offer(args):
    lang = notice_lang(args.instance_root, args.repo) if args.instance_root else args.lang
    items = try_items_since(args.repo, args.commit, args.previous, args.new, lang)
    profile = open(args.profile).read() if args.profile and os.path.isfile(args.profile) else ""
    print(json.dumps({"text": render_try_offer(items, lang, profile), "count": len(items)}, ensure_ascii=False))


def post_update_try_offer(ledger, items, results, root, lang):
    """Use the saved before/after versions, never the updated instance config.

    Render once across all applied layers. The result card's existing claim
    and per-route receipt also cover this offer, including bridge boot sweeps.
    """
    collected, seen = [], set()
    for item, result in zip(items, results):
        if item["key"].startswith("previous:"):
            continue
        currents = dict(item.get("currents") or [])
        for member in result["members"]:
            if member_unit(result, member) != "ok" or member["status"] != "verified":
                continue
            # Older result items may lack layer metadata. Use a saved note
            # when available; an optional offer must not break their notice.
            source = item.get("note") or {}
            if not source and item.get("kind") and "id" in item:
                source = note_source(item["kind"], item["id"], item["target"],
                                     item.get("cand_tag", ""), ledger["repo"], lang)
            ranges = [(source, currents.get(member["name"], item.get("current", "")), item["target"])]
            for fw in item.get("includes") or []:
                if same_root(fw["root"], member["root"]):
                    ranges.append((note_source("framework", fw["name"], fw["target"], "", ledger["repo"], lang),
                                   fw["current"], fw["target"]))
            for source, previous, new in ranges:
                if not previous or not source.get("repo") or not source.get("commit"):
                    continue
                for entry in try_items_since(source["repo"], source["commit"], previous, new, lang,
                                             machinery=ledger["repo"]):
                    key = (os.path.realpath(source["repo"]), entry["version"], entry["text"])
                    if key not in seen:
                        seen.add(key)
                        collected.append(entry)
    profile_path = os.path.join(root, "identity", "hot.custom.owner.md")
    try:
        with open(profile_path, encoding="utf-8") as handle:
            profile = handle.read()
    except OSError:
        profile = ""
    return render_try_offer(collected, lang, profile)


def cmd_versions(args):
    """DGN-1790: read-only -- the installed version of every layer in
    /update's scope (crew_roots), as data plus the owner-facing block."""
    lang = notice_lang(args.instance_root, args.repo)
    roots = crew_roots(args.instance_root, args.repo)
    layers = installed_layers(args.repo, roots, lang)
    print(json.dumps({"layers": layers, "modules": {"version": None, "source": "none"},
                      "text": versions_text(args.repo, roots, lang, layers), "parse_mode": "HTML"},
                     ensure_ascii=False))


def cmd_offer(args):
    """Session-start notice: one message for every layer touching this
    instance, from the same planner product /update uses.  Asked once per
    version set: a set already shown is quiet (unless --force: the promised
    re-check after a lookup failure)."""
    lang = notice_lang(args.instance_root, args.repo)
    leader, _ = offer_scope(args.instance_root, args.repo)
    if not same_root(leader, args.instance_root):
        # DGN-1749: a crew member that is not its leader stays silent -- the
        # leader's bridge sends the one notice for the whole crew.  What
        # this member found is handed to the leader, so it is not lost
        # while the leader is idle.
        print(json.dumps({"action": "quiet", "reason": "not_leader", "leader": leader, "ledger": "",
                          "revision": 1, "signature": "", "handoff": offer_handoff(args, leader),
                          "blocked": plan_blocked(args.plan, args.instance_root)},
                         ensure_ascii=False))
        return
    # Only a shown notice gets a durable ledger (taps resolve against it);
    # a quiet or empty check leaves nothing behind.
    ledger = new_ledger(args, "offer", lang, scope="offer", persist=False)
    items = ledger["items"]
    signature = offer_signature(items + ledger.get("held_items", []))
    if not items:
        action = "none"
    elif (read_offer_state(args.instance_root).get("asked") == signature and not args.force
          and not deferred_ready(args.repo, args.instance_root)):
        action = "quiet"
    else:
        action = "offer"
    if action == "offer":
        ledger["state"] = "offered"
        ledger["rows"] = offer_rows(ledger)
        atomic_write(ledger_path(ledger["id"]), ledger)
    out = {"action": action, "ledger": ledger["id"], "revision": 1, "signature": signature,
           "blocked": plan_blocked(args.plan, args.instance_root)}
    if action == "offer":
        out.update(offer_view(ledger))
    print(json.dumps(out, ensure_ascii=False))


def offer_view(ledger, pick=None):
    """Rebuild both notice screens only from the persisted offer."""
    lang = ledger["lang"]
    c = copy(lang)
    rows = ledger["rows"]
    shown = [dict(item_for(ledger, row["key"]),
                  offer_deps=[item_for(ledger, key) for key in row["with"]]) for row in rows]
    # Match only the selected item and target, never every item in a run's
    # original offer or every update touching the same member.
    if pick is None:
        running = {(item["key"], item["target"])
                   for run in active_ledgers(ledger["id"]) for item in selected(run)}
        for item in shown:
            item["offer_in_progress"] = (item["key"], item["target"]) in running
    in_progress = any(item.get("offer_in_progress") for item in shown)
    prefix = "%s:%s" % (ledger["id"], ledger["revision"])
    buttons = []
    # A mixed offer names its new rows explicitly, so the selection stays
    # fixed through confirmation even after the active run finishes.
    new_indices = [str(index) for index, item in enumerate(shown) if not item.get("offer_in_progress")]
    if new_indices and len(shown) > 1:
        all_pick = "a" + ",".join(new_indices) if in_progress else "a"
        buttons.append({"text": c["apply_all"], "data": "upd:o:%s:%s" % (prefix, all_pick)})
    for index, item in enumerate(shown):
        if item.get("offer_in_progress"):
            continue
        label = (word(lang, "update.apply_agent").format(agent=AGENT_SEP.join(item["labels"]),
                 name=item["name"], target=target_text(item, lang)) if item["kind"] == "pack"
                 else c["apply_one"].format(name=item["name"], target=target_text(item, lang)))
        buttons.append({"text": label,
                        "data": "upd:o:%s:%d" % (prefix, index)})
    if pick is None:
        cards = sorted(shown + ledger.get("held_items", []), key=lambda item: (
            bool(item.get("offer_in_progress")), ROW_ORDER.get(item["kind"], len(ROW_ORDER))))
        body = render_offer(cards, lang, header=c["header"], group_in_progress=in_progress)
        if ledger.get("split_rows"):
            rows_html, sync_buttons = split_view(ledger)
            body += "\n\n" + rows_html
            buttons += sync_buttons
    else:
        wanted = offer_pick_rows(rows, pick)
        applied = closure(ledger, [item_for(ledger, row["key"]) for row in wanted])
        # DGN-1776: the confirm screen shows exactly what this apply applies
        # (the picked rows + their dependency closure), counted in its header.
        keys = set(item["key"] for item in applied)
        body = render_offer([item for item in shown if item["key"] in keys], lang,
                            header=confirm_header(len(applied), lang))
        members, labels = set(), []
        for item in applied:
            for member, label in zip(item["members"], item["labels"]):
                if member not in members:
                    members.add(member)
                    labels.append(label)
        agents = AGENT_SEP.join(labels)
        if lang == "ko":
            agents = with_josa(agents, "\uC774", "\uAC00")
        # Owner 2026-09-28 06:25: the caution is plain text, not a quote.
        body += "\n\n%s" % html.escape(c["offer_caution"].format(agents=agents), quote=False)
        # Owner 2026-09-28 06:20: back on the left, apply on the right.
        buttons = [{"text": c["offer_back"], "data": "upd:b:%s" % prefix},
                   {"text": c["offer_apply"], "data": "upd:p:%s:%s" % (prefix, pick)}]
    # FDGN-586 permits a status-only offer when every shown item is running.
    # Other offers and confirmations retain the DGN-2041 button guard.
    if (not buttons and not (pick is None and in_progress)) or (pick is not None and not any(b["data"].startswith("upd:p:") for b in buttons)):
        raise ValueError("offer without its button")
    view = {"html": body, "buttons": buttons}
    if pick is None and in_progress and not buttons:
        view["in_progress_only"] = True
    return view


HANDOFF_NAME = "update-offer-handoff.md"
# DGN-1749 review (7): the drop is a machine marker, not a prompt.  The
# leader's bridge (bot.UPDATE_OFFER_HANDOFF_PREFIX) recognises it and sends
# the notice through its session-start seat -- no model turn, no owner text
# besides the notice.
HANDOFF_PREFIX = "[update-offer-handoff]"
HANDOFF_BODY = HANDOFF_PREFIX + " from %s (DGN-1749): the bridge sends the update notice; not a model turn.\n"


def offer_handoff(args, leader):
    """DGN-1749 review (5): a member that finds an offer hands it to its
    crew leader through the seats that already exist -- the leader's
    update-offer-request (what version-check / pack-lag leave) plus one
    session-inbox marker drop, which the leader's bridge answers by sending
    the notice itself (review (7): no model turn).  Nothing is handed over when the
    leader's notice would be empty or already asked; one drop at a time
    (fixed name, replaced).  Returns whether the leader was handed it."""
    try:
        state = os.path.join(leader, ".telegram_bot")
        if not os.path.isdir(state):
            return False
        lang = notice_lang(leader, args.repo)
        as_leader = argparse.Namespace(**dict(vars(args), instance_root=leader))
        items = new_ledger(as_leader, "offer", lang, scope="offer", persist=False)["items"]
        if not items or read_offer_state(leader).get("asked") == offer_signature(items):
            return False
        request = os.path.join(state, "state", "update-offer-request")
        atomic_write(request, {"source": "handoff", "from": os.path.realpath(args.instance_root),
                               "ts": int(time.time())})
        me = os.path.realpath(args.instance_root)
        who = member_label(me, read_conf(os.path.join(me, ".instance.conf")).get("DOGANY_AGENT_NAME")
                           or os.path.basename(me), lang)
        spool = os.path.join(state, "session-inbox")
        if not os.path.isdir(spool):
            os.makedirs(spool)
        tmp = os.path.join(spool, "." + HANDOFF_NAME + ".tmp")
        with open(tmp, "w") as handle:
            handle.write(HANDOFF_BODY % who)
        os.rename(tmp, os.path.join(spool, HANDOFF_NAME))
        return True
    except Exception:
        return False


def cmd_offered(args):
    """Ask-once record, written only after the notice really went out."""
    state = read_offer_state(args.instance_root)
    if state.get("deferred"):
        released = released_waits(load_ledger(args.ledger)["repo"], args.instance_root)
        state["deferred"] = [p for p in state["deferred"] if p not in released]
    state.update(asked=args.signature, ledger=args.ledger, message_id=args.message_id,
                 at=int(time.time()))
    atomic_write(offer_state_path(args.instance_root), state)
    print(json.dumps({"ok": True}))


def cmd_detail(args):
    ledger = load_ledger(args.ledger)
    item = item_for(ledger, args.key)
    if not item or ledger["revision"] != args.revision or ledger["state"] != "selecting":
        raise ValueError("stale selection")
    full = closure(ledger, [item])
    deps = [other for other in full if other["key"] != item["key"]]
    crew, crew_labels = [], []
    for other in full:
        if other["needs_consent"]:
            for member, label in zip(other["members"], other["labels"]):
                if member not in crew:
                    crew.append(member)
                    crew_labels.append(label)
    view = dict(item, bundle=bundle_lines(item, deps, ledger["lang"]),
                needs_consent=bool(crew), members=crew or item["members"],
                labels=crew_labels or item["labels"])
    print(json.dumps({"ledger": ledger["id"], "revision": ledger["revision"], "item": view}, ensure_ascii=False))


def cmd_key(args):
    ledger = load_ledger(args.ledger)
    if ledger["revision"] != args.revision or args.index < 0 or args.index >= len(ledger["items"]):
        raise ValueError("stale selection")
    print(ledger["items"][args.index]["key"])


def check_requester(ledger, user):
    if user is not None and str(user) != ledger["requester"]:
        raise ValueError("not the requester")


def consented_kits(ledger):
    consent = ledger.get("consent") or []
    if isinstance(consent, dict):
        consent = [consent]
    return set(entry.get("kit") for entry in consent)


def record_consent(ledger, item, user, tapped_at, repo):
    # Keep the selected crew as a receipt, without an approval artifact.
    if item["crew_kit"] in consented_kits(ledger):
        return
    consent = ledger.get("consent") or []
    if isinstance(consent, dict):
        consent = [consent]
    consent.append({"key": item["key"], "kit": item["crew_kit"], "members": item["members"],
                    "tapped_at": tapped_at, "recorded_at": int(time.time()), "user": str(user)})
    ledger["consent"] = consent


def cmd_consent(args):
    ledger = load_ledger(args.ledger)
    check_requester(ledger, args.user)
    item = item_for(ledger, args.key)
    if not item or ledger["revision"] != args.revision or ledger["state"] != "selecting":
        raise ValueError("stale consent")
    full = closure(ledger, [item])
    if not any(other["needs_consent"] for other in full):
        raise ValueError("stale consent")
    for other in full:
        if other["needs_consent"]:
            record_consent(ledger, other, args.user, args.tapped_at, ledger["repo"])
    atomic_write(ledger_path(args.ledger), ledger)
    print(json.dumps({"ok": True, "members": item["members"]}, ensure_ascii=False))


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def supervisor_alive(ledger):
    sup = ledger.get("supervisor") or {}
    if sup.get("pid"):
        return pid_alive(sup["pid"])
    # Launched but the child has not stamped itself yet: short grace.
    return int(time.time()) - int(ledger.get("started_at") or 0) < 120


def cmd_status(args):
    """Read-only liveness for this card's run, using the supervisor policy."""
    ledger = load_ledger(args.ledger)
    print(json.dumps({"ledger": ledger["id"],
                      "active": ledger.get("state") in ACTIVE_STATES and supervisor_alive(ledger)}))


def busy(exclude):
    """Another ledger mid-run (one update run on the machine at a time):
    that ledger, or None."""
    if not os.path.isdir(ledger_dir()):
        return None
    for name in sorted(os.listdir(ledger_dir())):
        if not name.endswith(".json") or name == exclude + ".json":
            continue
        try:
            other = load_ledger(name[:-5])
        except (ValueError, OSError):
            continue
        if other.get("state") in ACTIVE_STATES and supervisor_alive(other):
            return other
    return None


# --------------------------------------------------------------------------
# DGN-1794 round 2 (dec-210): a busy tap asks whether to carry the apply
# through; yes queues it on its busy ledger and the next pickup
# (run delivered / boot sweep / the queue tap itself) applies it -- or, when
# the plan moved, offers a fresh card.
# --------------------------------------------------------------------------

# A live run is bounded (a verdict timeout per member, one automatic
# restart, a lost supervisor closed by the next boot sweep), so no honest
# run holds a queue for a day.  Older means the trigger was lost or the
# owner's moment has passed: drop it with one line, never apply it.  One day
# is also the notice's own cadence (asked / failure notice once a day).
FOLLOW_MAX_AGE_S = 24 * 3600
SOURCE_SCOPE = {"offer": "offer", "update": "crew"}


def tap_user(ledger, user):
    return str(user) if user is not None else ledger["requester"]


def follow_buttons(ledger):
    c = copy(ledger.get("lang", "ko"))
    prefix = "upd:q:%s:" % ledger["id"]
    return [[{"text": c["follow_yes"], "callback_data": prefix + "y"},
             {"text": c["follow_no"], "callback_data": prefix + "n"}]]


def already_running(ledger, items, running):
    """DGN-1908: every key this tap would apply (its closure) is already in
    the running run's selection -- a second tap on the same button, not a
    different pending update."""
    keys = set(item["key"] for item in closure(ledger, items))
    return bool(keys) and keys <= set(selection_keys(running))


def hold(ledger, items, running, follow):
    """The busy refusal: the tap's ledger turns "busy" and keeps what this
    tap would have applied -- key + shown state of each item, the key set
    of their closure (a new riding dependency = changed) -- then asks.
    DGN-1908: a tap the running run already covers is a duplicate -- the
    running status once, no ask, nothing held to carry through."""
    if already_running(ledger, items, running):
        ledger["state"] = "duplicate"
        ledger["duplicate_of"] = running["id"]
        atomic_write(ledger_path(ledger["id"]), ledger)
        return {"started": False, "busy": True, "duplicate": True, "ledger": ledger["id"],
                "text": busy_text(running, "busy_same")}
    ledger["state"] = "busy"
    ledger["follow"] = dict(follow, want=[shown_state(item) for item in items],
                            closure=[item["key"] for item in closure(ledger, items)],
                            asked_at=int(time.time()))
    atomic_write(ledger_path(ledger["id"]), ledger)
    return {"started": False, "busy": True, "ledger": ledger["id"], "text": busy_text(running),
            "buttons": follow_buttons(ledger)}


def queue_tap(ledger, items, running, follow):
    """DGN-2135: the offer's apply tap while another run holds the lock.  The
    tap IS the owner's yes: the ledger goes straight to "queued" (hold's
    follow-through, picked up by the same pickup) and the answer is one line
    that edits the offer in place -- no second ask.  A tap the running run
    already covers stays hold's duplicate status.  Fails closed: queued,
    never started."""
    if already_running(ledger, items, running):
        return hold(ledger, items, running, follow)
    now = int(time.time())
    ledger["state"] = "queued"
    ledger["follow"] = dict(follow, want=[shown_state(item) for item in items],
                            closure=[item["key"] for item in closure(ledger, items)],
                            asked_at=now, queued_at=now, queued_by=str(follow["user"]),
                            chat=str(ledger["chat"]))
    atomic_write(ledger_path(ledger["id"]), ledger)
    return {"started": False, "busy": True, "queued": True, "ledger": ledger["id"],
            "text": queued_text(running)}


def queued_text(running):
    lang = running.get("lang", "ko")
    names = ", ".join(busy_item(item, lang) for item in selected(running))
    return html.escape(copy(lang)["queued_tap"].format(running=names), quote=False)


def queued_for(keys, user):
    """DGN-2135: the live queued follow-through of this requester that already
    carries every key of this tap (any offer copy), or None."""
    if not os.path.isdir(ledger_dir()):
        return None
    for name in sorted(os.listdir(ledger_dir())):
        if not name.endswith(".json"):
            continue
        try:
            other = load_ledger(name[:-5])
        except (ValueError, OSError):
            continue
        follow = other.get("follow") or {}
        if (other.get("state") == "queued" and str(follow.get("user")) == str(user)
                and set(keys) <= set(follow.get("closure") or [])
                and int(time.time()) - int(follow.get("queued_at") or 0) <= FOLLOW_MAX_AGE_S):
            return other
    return None


def close_superseded(keys, by):
    """DGN-2135: a pick's scratch ledger (source "notice") still `selecting`
    for keys a follow / run now carries is closed, never left behind.  The
    offer menus (source offer / update) stay: other rows of the same card
    are still tappable."""
    if not os.path.isdir(ledger_dir()):
        return []
    closed = []
    for name in sorted(os.listdir(ledger_dir())):
        if not name.endswith(".json") or name == by + ".json":
            continue
        try:
            other = load_ledger(name[:-5])
        except (ValueError, OSError):
            continue
        if (other.get("state") == "selecting" and other.get("source") == "notice"
                and set(keys) <= set(item["key"] for item in other.get("items") or [])):
            other["state"] = "superseded"
            other["superseded_by"] = by
            atomic_write(ledger_path(other["id"]), other)
            closed.append(other["id"])
    return closed


def claim_live(path):
    """claim(), taking over a claim whose holder died (its pid is in the file)."""
    if claim(path):
        return True
    try:
        with open(path, "r") as handle:
            pid = int(handle.read().split()[0])
    except (IOError, OSError, ValueError, IndexError):
        return False
    if pid_alive(pid):
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return claim(path)


def follow_lock(ledger_id, wait_s=0):
    """The one mutex over a follow-through's state (tap vs pickup)."""
    path = ledger_path(ledger_id) + ".follow"
    deadline = time.time() + wait_s
    while not claim_live(path):
        if time.time() >= deadline:
            return None
        time.sleep(0.2)
    return path


def active_ledgers(exclude):
    """Runs still applying, verifying or delivering with a live supervisor."""
    if not os.path.isdir(ledger_dir()):
        return
    for name in sorted(os.listdir(ledger_dir())):
        if not name.endswith(".json") or name == exclude + ".json":
            continue
        try:
            other = load_ledger(name[:-5])
        except (ValueError, OSError):
            continue
        if ((other.get("state") in ACTIVE_STATES and supervisor_alive(other))
                or (other.get("state") in FINAL_STATES and other.get("stage") == "delivering"
                    and pid_alive((other.get("supervisor") or {}).get("pid")))):
            yield other


def active_run(exclude):
    """Applying or still delivering: the next run waits for both."""
    return next(active_ledgers(exclude), None) is not None


def fresh_plan(ledger):
    """The same planner product the bridge takes for a tap (_update_plan)."""
    state = os.path.join(ledger["instance_root"], ".telegram_bot", "state", "update-plans")
    if not os.path.isdir(state):
        os.makedirs(state)
    plan = os.path.join(state, "%s.tsv" % uuid.uuid4().hex)
    env = dict(os.environ, DOGANY_PLAN_EMIT=plan)
    if ledger.get("sync_kit"):
        # dec-263: a [버전 맞추기] ledger re-plans the same crew sync.
        env["DOGANY_PLAN_CREW_SYNC"] = ledger["sync_kit"]
    proc = subprocess.run([os.path.join(ledger["repo"], "scripts", "pack", "update_plan.sh")],
                          env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return plan if plan_usable(proc.returncode, plan) else None


def follow_match(fresh, follow):
    """The queued items in the fresh plan, or None when anything the tap
    saw moved: a key gone, another installed version, another riding
    dependency, or a crew consent the tap did not give."""
    items = []
    for shown in follow["want"]:
        now = item_for(fresh, shown[0])
        if now is None or json.loads(json.dumps(shown_state(now))) != shown:
            return None
        items.append(now)
    full = closure(fresh, items)
    if [item["key"] for item in full] != follow["closure"]:
        return None
    return items


def fresh_offer(origin, lang):
    """The plan moved: the /update card as it stands now, for the tapping
    chat to pick again (never an auto-run)."""
    c = copy(lang)
    offer = new_ledger(origin, "update", lang, scope="crew", persist=False)
    if not offer["items"]:
        block = versions_text(origin.repo, crew_roots(origin.instance_root, origin.repo), lang)
        return {"text": html.escape(c["follow_none"], quote=False) + ("\n\n" + block if block else "")}
    offer["rows"] = offer_rows(offer)
    atomic_write(ledger_path(offer["id"]), offer)
    view = offer_view(offer)
    return {"text": html.escape(c["follow_changed"], quote=False) + "\n\n" + view["html"], "offer": offer["id"],
            "buttons": [[{"text": b["text"], "callback_data": b["data"]}] for b in view["buttons"]]}


def settle(ledger, state, payload, send):
    """Close a follow-through once: record the state first (never a second
    run), then tell the tapping chat -- pushed from a pickup, or handed back
    to the bridge (send=False, the queue tap edits its own ask)."""
    ledger["state"] = state
    ledger["follow"].update(settled_at=int(time.time()), **{k: v for k, v in payload.items()
                                                            if k in ("offer", "picked")})
    atomic_write(ledger_path(ledger["id"]), ledger)
    if send and payload.get("text"):
        ledger["follow"]["sent"] = push(ledger, payload["text"], payload.get("buttons"))
        atomic_write(ledger_path(ledger["id"]), ledger)
    return dict(payload, state=state, parse_mode="HTML")


def follow_one(ledger_id, send=True):
    """Pick up one queued follow-through.  None = still queued (another run
    active, its pickup elsewhere, a race lost to a new run, a rehearsal)."""
    if os.environ.get("DOGANY_LAUNCHD_CAPTURE"):
        return None
    lock = follow_lock(ledger_id)
    if lock is None:
        return None
    try:
        led = load_ledger(ledger_id)
        follow = led.get("follow") or {}
        if led.get("state") != "queued":
            return None
        lang = led.get("lang", "ko")
        c = copy(lang)
        if int(time.time()) - int(follow.get("queued_at") or 0) > FOLLOW_MAX_AGE_S:
            return settle(led, "expired", {"text": html.escape(c["follow_expired"], quote=False)}, send)
        if active_run(ledger_id):
            return None
        plan = fresh_plan(led)
        if plan is None:
            return settle(led, "failed", {"text": html.escape(failure_text({}, lang), quote=False)}, send)
        origin = argparse.Namespace(plan=plan, repo=led["repo"], instance_root=led["instance_root"],
                                    requester=led["requester"], chat=led["chat"])
        fresh = new_ledger(origin, "follow", lang, scope=follow.get("scope"))
        items = follow_match(fresh, follow)
        if items is None:
            fresh["state"] = "stale"
            atomic_write(ledger_path(fresh["id"]), fresh)
            return settle(led, "changed", fresh_offer(origin, lang), send)
        for item in items:
            if item["needs_consent"]:
                record_consent(fresh, item, follow.get("user"), follow.get("tapped_at"), fresh["repo"])
        outcome = start(fresh, items, follow.get("tapped_at"))
        if not outcome.get("started"):
            fresh["state"] = "stale"
            atomic_write(ledger_path(fresh["id"]), fresh)
            return None
        # Crew pickup uses the same strict start sender as a direct tap.
        # It must not add the old follow-through start push as well.
        payload = {"picked": fresh["id"]}
        if not silent_follow(items):
            payload["text"] = start_text(lang, as_html=True)
        return settle(led, "picked", payload, send)
    finally:
        os.unlink(lock)


def follow_through():
    """Every queued follow-through, oldest queue first; the first that
    starts makes the rest wait for its own delivery."""
    if os.environ.get("DOGANY_LAUNCHD_CAPTURE") or not os.path.isdir(ledger_dir()):
        return []
    queued = []
    for name in sorted(os.listdir(ledger_dir())):
        if not name.endswith(".json"):
            continue
        try:
            led = load_ledger(name[:-5])
        except (ValueError, OSError):
            continue
        if led.get("state") == "queued":
            queued.append(led)
    done = []
    for led in sorted(queued, key=lambda led: int((led.get("follow") or {}).get("queued_at") or 0)):
        outcome = follow_one(led["id"])
        if outcome is not None:
            done.append({"ledger": led["id"], "state": outcome["state"]})
    return done


FOLLOW_REPLY = {"queued": "follow_queued", "declined": "follow_declined", "changed": "follow_changed",
                "failed": "follow_failed", "expired": "follow_expired"}


def cmd_follow(args):
    """The yes / no tap under a busy ask (upd:q), the
    requester only.  y queues (idempotent: one queued request per ledger)
    and, when nothing is running any more, picks it up at once; n (or no
    answer) runs nothing.  The reply replaces the ask."""
    led = load_ledger(args.ledger)
    check_requester(led, args.user)
    c = copy(led.get("lang", "ko"))
    if not led.get("follow"):
        print(json.dumps({"action": "stale", "text": c["stale"]}, ensure_ascii=False))
        return 0
    # A pickup holding the lock only ever holds a ledger already queued, so
    # a tap that cannot get it answers from the state as it stands.
    lock = follow_lock(led["id"], wait_s=15)
    try:
        led = load_ledger(led["id"])
        state = led["state"]
        if lock is None:
            pass
        elif state == "busy" and args.choice == "y":
            if int(time.time()) - int(led["follow"].get("asked_at") or 0) > FOLLOW_MAX_AGE_S:
                state = "expired"
            else:
                state = "queued"
                led["chat"] = str(args.chat)
                led["follow"].update(chat=str(args.chat), queued_by=str(args.user), queued_at=int(time.time()))
            led["state"] = state
            atomic_write(ledger_path(led["id"]), led)
        elif state in ("busy", "queued") and args.choice == "n":
            state = led["state"] = "declined"
            led["follow"]["declined_at"] = int(time.time())
            atomic_write(ledger_path(led["id"]), led)
    finally:
        if lock is not None:
            os.unlink(lock)
    if lock is not None and state == "queued" and not active_run(led["id"]):
        # The running run already ended before this tap: nothing else will
        # pick it up, so this tap does.
        outcome = follow_one(led["id"], send=False)
        if outcome is not None:
            out = dict(outcome, action=outcome["state"])
            print(json.dumps(out, ensure_ascii=False))
            return 0
    if state == "picked":
        picked = load_ledger(led["follow"]["picked"])
        if silent_follow(selected(picked)):
            print(json.dumps({"action": state}))
            return 0
        text = start_text(led.get("lang", "ko"), as_html=True)
    elif state == "failed":
        text = html.escape(failure_text({}, led.get("lang", "ko")), quote=False)
    elif state in FOLLOW_REPLY:
        text = html.escape(c[FOLLOW_REPLY[state]], quote=False)
    else:
        text = html.escape(c["stale"], quote=False)
    print(json.dumps({"action": state, "text": text, "parse_mode": "HTML"}, ensure_ascii=False))
    return 0


def crew_run(items):
    """A run touching several agents, including independent framework units."""
    touched = set()
    for item in items:
        roots = item.get("member_roots")
        if roots is None:
            # Ledger-door callers may supply only the executable plan lines.
            # Derive their scope rather than treating an unknown crew as solo.
            roots = []
            for line in item.get("lines", []):
                fields = line.split("\t")
                if fields[0] in ("FW", "ATOMIC-FW") and len(fields) >= 5:
                    roots.append(fields[2])
                elif fields[0] in ("PACK", "ATOMIC-PACK") and len(fields) >= 8:
                    roots.extend(member["root"] for member in members_of(fields[7]))
        touched.update(os.path.realpath(root) for root in roots)
    return len(touched) > 1


def item_intents(item):
    """Stable per-member identity across a re-plan that drops applied members."""
    includes = tuple(sorted((os.path.realpath(f["root"]), norm(f["target"]))
                            for f in item.get("includes") or []))
    # Framework item IDs encode the remaining members and change on retry.
    pid = "" if item["kind"] == "framework" else item["id"]
    return {(item["kind"], pid, norm(item["target"]), os.path.realpath(root),
             tuple(f for f in includes if f[0] == os.path.realpath(root)))
            for root in item["member_roots"]}


def retry_context(ledger, items):
    """Resume only the latest unfinished story in this owner/chat/repo scope.

    Every outstanding member must be retried, and no new product/target may
    enter the selection. A completed or different update ends the story.
    Snapshot the facts before apply; never add earlier units to the plan.
    """
    candidates = []
    for name in os.listdir(ledger_dir()):
        if not name.endswith(".json") or name == ledger["id"] + ".json":
            continue
        try:
            old = load_ledger(name[:-5])
            modified = os.stat(ledger_path(name[:-5])).st_mtime_ns
        except (ValueError, OSError):
            continue
        if (not old.get("started_at") or not old.get("result")
                or not all(old.get(k) == ledger.get(k) for k in ("requester", "chat"))
                or not all(same_root(old.get(k, ""), ledger.get(k, ""))
                           for k in ("repo", "instance_root"))):
            continue
        old["_ledger_mtime"] = modified
        candidates.append(old)
    if not candidates:
        return None
    old = max(candidates, key=lambda entry: (entry["started_at"], entry["_ledger_mtime"], entry["id"]))
    if old.get("state") not in ("partial", "blocked", "recovery_required"):
        return None
    result = old["result"]
    story = result.get("story") or dict(items=selected(old), results=result.get("items") or [],
                                       record=result.get("record"))
    if not story.get("record") or len(story["items"]) != len(story["results"]):
        return None
    wanted = set().union(*(item_intents(item) for item in items))
    previous, outstanding = set(), set()
    for item, verdict in zip(story["items"], story["results"]):
        intents = item_intents(item)
        previous.update(intents)
        verified = {os.path.realpath(m["root"]) for m in verdict["members"]
                    if m["status"] == "verified" and member_unit(verdict, m) == "ok"}
        outstanding.update(i for i in intents if i[3] not in verified)
    if not outstanding or not outstanding <= wanted or not wanted <= previous:
        return None
    return dict(story, ledger=old["id"])


def report_story(ledger, items, results, record):
    """Add earlier successes to the result only, with fresh version/life checks."""
    context = ledger.get("retry_context")
    if not context:
        return items, results, record
    wanted = set().union(*(item_intents(item) for item in items))
    carried_items, carried_results, carried_units = [], [], []
    cache = {}
    for old_item in context["items"]:
        keep = {i[3] for i in item_intents(old_item) - wanted}
        if not keep:
            continue
        item = dict(old_item)
        item["key"] = "previous:" + old_item["key"].removeprefix("previous:")
        pairs = [(n, r) for n, r in zip(item["members"], item["member_roots"])
                 if os.path.realpath(r) in keep]
        item.update(members=[n for n, r in pairs], member_roots=[r for n, r in pairs])
        item["pack_roots"] = [r for r in item.get("pack_roots") or [] if os.path.realpath(r) in keep]
        item["includes"] = [f for f in item.get("includes") or [] if os.path.realpath(f["root"]) in keep]
        units = []
        for old_unit in units_for(old_item, context["record"].get("units") or [], len(context["items"]) == 1):
            own = [(n, r) for n, r in pairs if old_unit.get("unit") == "framework " + n
                   or old_unit.get("unit", "").startswith(n + "/")]
            if item["kind"] == "framework" and not own:
                continue
            unit = dict(old_unit, story_key=item["key"],
                        result=old_unit.get("applied_result", old_unit.get("result")))
            unit["agents"] = [agent_label(r, n, ledger["lang"]) for n, r in own or pairs]
            unit["members"] = [dict(name=n, root=r) for n, r in own or pairs]
            units.append(unit)
        verdict = judge(ledger, item, dict(outcome="done", units=units), 0,
                        only=False, alive_cache=cache)
        # A formerly verified member can go down or change version before retry.
        # Keep it visible, but never print a successful result for stale evidence.
        if verdict["status"] != "complete":
            for unit in units:
                unit.update(applied_result=unit["result"], result="incomplete", reason_code="")
        carried_items.append(item)
        carried_results.append(verdict)
        carried_units.extend(units)
    current_units = []
    for item in items:
        current_units.extend(dict(unit, story_key=item["key"]) for unit in
                             units_for(item, (record or {}).get("units") or [], len(items) == 1))
    return (carried_items + items, carried_results + results,
            dict(record or {}, units=carried_units + current_units))


def silent_start(items):
    """The strict stop sender owns crew starts; older bridges keep their reply."""
    return caller_takes_silent_start() and (crew_run(items) or any(item.get("needs_consent") for item in items))


def silent_follow(items):
    # Pre-update bridges may understand a silent apply but not a silent pickup.
    return os.environ.get("DOGANY_UPDATE_SILENT_FOLLOW") == "1" and silent_start(items)


def start(ledger, items, tapped_at, follow=None):
    """Shared by /update and the notice: the same executor.  follow = the
    tap's own facts (via, scope, user, consent kits): a busy refusal then
    asks to carry the apply through (DGN-1794 r2, hold).  The pickup passes
    none -- it already waited for the running run."""
    for item in items:
        if item["needs_consent"]:
            record_consent(ledger, item, (follow or {}).get("user"), tapped_at, ledger["repo"])
    running = busy(ledger["id"])
    if running is not None:
        if follow is None:
            return {"started": False, "busy": True}
        return hold(ledger, items, running, dict(follow, tapped_at=tapped_at))
    if not claim(ledger_path(ledger["id"]) + ".start"):
        return {"started": False, "state": load_ledger(ledger["id"])["state"]}
    keys = [item["key"] for item in items]
    ordered = [item for item in ledger["items"] if item["key"] in keys]
    selected_plan = ledger_path(ledger["id"]) + ".plan"
    with open(selected_plan, "w") as handle:
        handle.write("PLAN-EMIT-V1\n")
        for item in ordered:
            for line in item["lines"]:
                handle.write(line + "\n")
    context = retry_context(ledger, ordered)
    if context:
        ledger["retry_context"] = context
        ledger["retry_of"] = context["ledger"]
    story_items = ordered + (context["items"] if context else [])
    ledger["crew_update"] = crew_run(story_items)
    # A silent-start bridge delegates the first owner-facing event to this
    # supervisor.  Enable the editable receipt in every language so that the
    # pre-check line is delivered before the apply process (and therefore
    # before its lint/dry-run gates), then becomes the existing progress card.
    progress_enabled = caller_takes_silent_start()
    ledger.update({"selection": [item["key"] for item in ordered], "selected_plan": selected_plan,
                   "state": "running", "tapped_at": tapped_at, "started_at": int(time.time()),
                   "progress_enabled": progress_enabled})
    atomic_write(ledger_path(ledger["id"]), ledger)
    worker = os.path.abspath(__file__)
    log = ledger_path(ledger["id"]) + ".log"
    with open(log, "a") as handle:
        # Own session: the launchd group teardown of the stopped bridge can
        # no longer reach the supervisor (DGN-1010 shape).
        subprocess.Popen([sys.executable, worker, "worker", "--ledger", ledger["id"]],
                         stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                         close_fds=True, start_new_session=True)
    # DGN-1776: the start text is HTML; the busy/stale texts stay plain, so
    # the bridge keys parse_mode off this.  The other agents this run
    # touches get the same line from the worker (DGN-1781).
    # DGN-1778: HTML only when the calling bridge declared it renders HTML.
    as_html = caller_renders_html()
    # DGN-2157: framework-only crews need the same single start sender as
    # kit crews. Consent is unrelated to how many agents the run touches.
    # The strict stop notice owns the line; DGN-2041 callers declare that
    # they understand a textless start. Keep the older bridge fallback.
    if silent_start(story_items) or progress_enabled:
        return {"started": True}
    outcome = {"started": True, "text": start_text(ledger["lang"], as_html)}
    if as_html:
        outcome["parse_mode"] = "HTML"
    return outcome


def cmd_apply(args):
    ledger = load_ledger(args.ledger)
    check_requester(ledger, args.user)
    if ledger["state"] in ACTIVE_STATES + FINAL_STATES:
        # Duplicate tap: the recorded outcome, never a second run.
        print(json.dumps({"started": False, "state": ledger["state"]}, ensure_ascii=False))
        return 0
    item = item_for(ledger, args.key)
    if not item or ledger["revision"] != args.revision or ledger["state"] != "selecting":
        raise ValueError("stale apply")
    # DGN-1794 r2: the detail-card apply (upd:a / upd:c) asks on busy too;
    # the kits consented on this card (upd:c) are the tap's consent.
    follow = {"via": "apply", "scope": SOURCE_SCOPE.get(ledger.get("source")),
              "user": tap_user(ledger, args.user), "consent": sorted(consented_kits(ledger))}
    print(json.dumps(start(ledger, closure(ledger, [item]), args.tapped_at, follow=follow), ensure_ascii=False))
    return 0


# DGN-2041 consent gate.  The confirm card replaces the list in place, so a
# second tap meant for the list (the owner waited ~4 s on a re-plan with no
# feedback) can land on the apply button that just appeared under the finger --
# owner report 2026-10-12: confirm rendered ~19:41:53.4, apply callback 19:41:54, the
# tap took the buttons off at once and the crew update ran.  An apply tap is
# the owner's confirm only when this ledger rendered THAT confirm card and
# the tap came CONFIRM_SETTLE_S clock seconds after it; anything else re-shows
# the confirm card (never stale, never a run).
# DGN-2254 (dec-541, owner 2026-10-23): the settle wait is removed by default --
# it also ate the owner's deliberate apply tap ~1 s after the card appeared and
# re-showed the same card. The consent gate (a confirm card this ledger
# rendered for this pick) stays; the env knob remains for opt-in only.
CONFIRM_SETTLE_S = float(os.environ.get("DOGANY_UPDATE_CONFIRM_SETTLE_S", "0") or 0)


def record_confirm(offer, pick):
    """The confirm card for `pick` was rendered now (offer-view)."""
    shown = offer.get("confirms") if isinstance(offer.get("confirms"), dict) else {}
    shown[pick] = time.time()
    offer["confirms"] = shown
    atomic_write(ledger_path(offer["id"]), offer)


def clear_confirms(offer):
    """Back to the list: no confirm card is on screen any more."""
    if offer.get("confirms"):
        offer["confirms"] = {}
        atomic_write(ledger_path(offer["id"]), offer)


def confirmed(offer, pick, tapped_at):
    """Did the owner confirm `pick` on a card this ledger showed?"""
    shown = (offer.get("confirms") or {}).get(pick)
    if shown is None or tapped_at is None:
        return False
    # The bridge and CLI carry integer receipt seconds. Compare the shown
    # time at that same resolution, including ledgers written by older code.
    # DGN-2154: shown=08:53:29.645, receipt=08:53:31 used to look like only
    # 1.355 s, re-showing the card after another six-second plan. The two
    # clock ticks have elapsed; the preceding tick still fails closed.
    return int(tapped_at) - int(shown) >= CONFIRM_SETTLE_S


def cmd_pick(args):
    """A tap on the session-start notice or the /update screen (DGN-1775:
    one screen, one tap flow) ("전체 적용" = a, "<name> 적용" = its
    row index; "b<index>" from a round-3 bundle card = the same row).  The
    Scoped all-new taps carry "a<index>,<index>" through confirmation. The
    notice ledger is only what was SHOWN: a fresh ledger from a fresh plan
    decides, every tapped row must still be there with the same installed
    versions AND the same kit riding on it (else stale), and the owner's tap
    is the crew consent for the kit it names -- the kit is on the row, so
    there is no after-tap confirm (item 22)."""
    offer = load_ledger(args.ledger)
    lang = offer.get("lang", "ko")
    check_requester(offer, args.user)
    stale = {"action": "stale", "text": copy(lang)["stale"]}
    rows = offer.get("rows") or [{"key": item["key"], "with": []} for item in offer["items"]]
    if offer.get("source") not in ("offer", "update") or offer["revision"] != args.revision:
        print(json.dumps(stale, ensure_ascii=False))
        return 0
    try:
        wanted = offer_pick_rows(rows, args.pick)
    except ValueError:
        print(json.dumps(stale, ensure_ascii=False))
        return 0
    preview = getattr(args, "preview", False)
    # The fresh plan is read with the tapped screen's own scope: the notice
    # for the crew(s) this instance leads, /update for every crew it is a
    # member of (DGN-1784) -- a wider or narrower re-plan would drift.
    scope = "offer" if offer.get("source") == "offer" else "crew"
    fresh = new_ledger(args, "notice", lang, scope=scope, persist=False)
    matched = []
    for row in wanted:
        was = item_for(offer, row["key"])
        now = item_for(fresh, row["key"])
        ok = was is not None and now is not None and shown_state(now) == shown_state(was)
        if ok:
            riding = [item for item in closure(fresh, [now]) if item["key"] != now["key"]]
            ok = [item["key"] for item in riding] == list(row["with"]) and all(
                item_for(offer, item["key"]) is not None
                and shown_state(item) == shown_state(item_for(offer, item["key"])) for item in riding)
        if not ok:
            fresh["state"] = "stale"
            if not preview:
                atomic_write(ledger_path(fresh["id"]), fresh)
            print(json.dumps(stale, ensure_ascii=False))
            return 0
        matched.append(now)
    if preview:
        out = offer_view(offer, None if args.back else args.pick)
        out["action"] = "offer" if args.back else "confirm"
        if args.back:
            clear_confirms(offer)
        else:
            record_confirm(offer, args.pick)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not confirmed(offer, args.pick, args.tapped_at):
        # DGN-2041: no apply without the owner's confirm in this run -- the
        # same confirm card again, its buttons back, nothing started.
        out = offer_view(offer, args.pick)
        # DGN-2154: restoring this same card must not restart its settle
        # window after every slow re-plan. Otherwise taps just after each
        # restore can keep postponing consent forever. A new card (preview)
        # records its appearance; Back still revokes it via clear_confirms.
        if args.pick not in (offer.get("confirms") or {}):
            record_confirm(offer, args.pick)
        out.update(action="confirm", reason="unconfirmed")
        print(json.dumps(out, ensure_ascii=False))
        return 0
    full = closure(fresh, matched)
    # DGN-1794 r2: the tap names every kit on its rows, so those kits are
    # its consent -- now, or at the pickup of a queued follow-through.
    follow = {"via": "pick", "scope": scope, "user": tap_user(offer, args.user),
              "consent": sorted(set(item["crew_kit"] for item in full if item["needs_consent"]))}
    # DGN-2135: the scratch ledger reaches the disk only when this tap queues
    # or starts (an unconfirmed / repeated tap leaves no `selecting` orphan);
    # check + queue / start are one critical section across the crews' bots.
    keys = [item["key"] for item in full]
    mutex = os.path.join(ledger_dir(), "pick.tap")
    give_up = time.time() + 15
    while not claim_live(mutex):
        if time.time() >= give_up:
            print(json.dumps(stale, ensure_ascii=False))  # fail closed: nothing started
            return 0
        time.sleep(0.05)
    try:
        queued = queued_for(keys, follow["user"])
        if queued is not None:
            running = busy(queued["id"])
            if running is not None:
                print(json.dumps(queued_reply({"queued": True, "repeat": True, "started": False, "busy": True,
                                  "ledger": queued["id"], "text": queued_text(running)}), ensure_ascii=False))
                return 0
            # Nothing runs any more and the queue was not picked up (its
            # trigger was lost): this tap supersedes it and goes the normal way.
            queued.update(state="superseded", superseded_by=fresh["id"])
            atomic_write(ledger_path(queued["id"]), queued)
        atomic_write(ledger_path(fresh["id"]), fresh)
        running = busy(fresh["id"])
        if running is not None:
            outcome = queue_tap(fresh, full, running, dict(follow, tapped_at=args.tapped_at))
            if outcome.get("queued"):
                outcome = queued_reply(outcome)
            else:
                outcome["action"] = "busy"
            if outcome.get("queued"):
                close_superseded(keys, fresh["id"])
            print(json.dumps(outcome, ensure_ascii=False))
            return 0
        for item in full:
            if item["needs_consent"]:
                record_consent(fresh, item, args.user, args.tapped_at, fresh["repo"])
        outcome = start(fresh, full, args.tapped_at, follow=follow)
        if outcome.get("started"):
            close_superseded(keys, fresh["id"])
    finally:
        os.unlink(mutex)
    outcome.update({"action": "started" if outcome.get("started") else "busy", "ledger": fresh["id"],
                    # Every row of the notice is covered (a pack's tap also
                    # applies the kit row riding on it, DGN-1749).
                    "all_rows": set(row["key"] for row in rows) <= set(item["key"] for item in full)})
    print(json.dumps(outcome, ensure_ascii=False))
    return 0


def cmd_sync(args):
    """dec-263 (a): the [버전 맞추기] tap on a /update split row (upd:s), the
    requester only.  The row is only what was SHOWN: the fresh plan must
    still carry the same alignable split (same versions, same newest stable
    tag), else stale.  Then the planner plans exactly that sync -- the kit
    at the newest stable tag for every member, its dependents applicable --
    and the kit item plus the dependents the row named start through the
    same start() / supervisor / update_apply crew transaction as any tap.
    The row named the kit and its agents, so the tap is the crew consent."""
    offer = load_ledger(args.ledger)
    lang = offer.get("lang", "ko")
    check_requester(offer, args.user)
    stale = {"action": "stale", "text": copy(lang)["stale"]}
    splits = offer.get("splits") or []
    if (offer.get("source") != "update" or offer["revision"] != args.revision
            or not args.pick.isdigit() or int(args.pick) >= len(splits)):
        print(json.dumps(stale, ensure_ascii=False))
        return 0
    shown = splits[int(args.pick)]
    now = [s for s in plan_splits(args.plan) if s["id"] == shown["id"]]
    if (not now or now[0]["mode"] != "sync" or now[0]["cand_tag"] != shown["cand_tag"]
            or now[0]["versions"] != shown["versions"]):
        print(json.dumps(stale, ensure_ascii=False))
        return 0
    plan = fresh_plan({"repo": offer["repo"], "instance_root": offer["instance_root"],
                       "sync_kit": shown["id"]})
    if plan is None:
        raise ValueError("crew sync plan failed")
    origin = argparse.Namespace(**dict(vars(args), plan=plan))
    fresh = new_ledger(origin, "update", lang, scope="crew", persist=False)
    fresh["sync_kit"] = shown["id"]
    want = sorted([name, norm(version)] for name, version in shown["versions"])
    kit = next((item for item in fresh["items"] if item["id"] == shown["id"] and item.get("sync")
                and not item["key"].startswith("bundle:")), None)
    if kit is None or kit["cand_tag"] != shown["cand_tag"] or sorted(kit["sync"]) != want:
        fresh["state"] = "stale"
        atomic_write(ledger_path(fresh["id"]), fresh)
        print(json.dumps(stale, ensure_ascii=False))
        return 0
    deps = [item for item in fresh["items"] if item["kind"] == "pack" and item["id"] in shown["dependents"]
            and (item.get("requires_kit") or {}).get("kit") in (kit["id"], kit["crew_kit"])]
    full = closure(fresh, [kit] + deps)
    atomic_write(ledger_path(fresh["id"]), fresh)
    follow = {"via": "sync", "scope": "crew", "user": tap_user(offer, args.user),
              "consent": sorted(set(item["crew_kit"] for item in full if item["needs_consent"]))}
    running = busy(fresh["id"])
    if running is not None:
        outcome = hold(fresh, full, running, dict(follow, tapped_at=args.tapped_at))
        outcome["action"] = "busy"
        print(json.dumps(outcome, ensure_ascii=False))
        return 0
    for item in full:
        if item["needs_consent"]:
            record_consent(fresh, item, args.user, args.tapped_at, fresh["repo"])
    outcome = start(fresh, full, args.tapped_at, follow=follow)
    outcome.update({"action": "started" if outcome.get("started") else "busy", "ledger": fresh["id"]})
    print(json.dumps(outcome, ensure_ascii=False))
    return 0


def cmd_notice(args):
    """Round-2 notice button (upd:n:<version>), kept only so a tap on an
    already-delivered old notice resolves: a fresh ledger from the same
    planner, the framework item of THIS instance at exactly the announced
    version.  Anything else (already applied, newer out, gone) is stale."""
    lang = notice_lang(args.instance_root, args.repo)
    ledger = new_ledger(args, "notice", lang)
    root = os.path.realpath(args.instance_root)
    for index, item in enumerate(ledger["items"]):
        fws = [{"root": r, "target": item["target"]} for r in item["member_roots"]] if item["kind"] == "framework" else item["includes"]
        if any(os.path.realpath(fw["root"]) == root and norm(fw["target"]) == norm(args.version) for fw in fws):
            if item["needs_consent"]:
                print(json.dumps({"action": "consent", "ledger": ledger["id"], "revision": 1,
                                  "index": index, "item": item}, ensure_ascii=False))
                return 0
            follow = {"via": "notice", "scope": None, "user": tap_user(ledger, None), "consent": []}
            outcome = start(ledger, [item], args.tapped_at, follow=follow)  # DGN-1794 r2
            outcome.update({"action": "started" if outcome.get("started") else "busy", "item": item,
                            "ledger": ledger["id"]})
            print(json.dumps(outcome, ensure_ascii=False))
            return 0
    ledger["state"] = "stale"
    atomic_write(ledger_path(ledger["id"]), ledger)
    print(json.dumps({"action": "stale"}, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# Supervisor: apply -> final restart -> verify -> per-item result -> deliver.
# --------------------------------------------------------------------------

def on_target(item, root):
    conf = read_conf(os.path.join(root, ".instance.conf"))

    def fw_at(target):
        return norm(conf.get("DOGANY_FW_TAG")) == norm(target) or norm(conf.get("DOGANY_FW_VERSION")) == norm(target)
    if item["kind"] == "framework":
        return fw_at(item["target"])
    real = os.path.realpath(root)
    if any(os.path.realpath(r) == real for r in item["pack_roots"]):
        packs = conf.get("DOGANY_PACKS", "").replace(",", " ").split()
        if item.get("migrate_from") and any(p.startswith(item["migrate_from"] + "@") for p in packs):
            return False
        if "%s@%s" % (item["id"], norm(item["target"])) not in packs:
            return False
    return all(fw_at(fw["target"]) for fw in item["includes"] if os.path.realpath(fw["root"]) == real)


def alive(repo, root, since, timeout=None):
    timeout = timeout or os.environ.get("DOGANY_UPDATE_VERDICT_TIMEOUT", "180")
    mint_run = os.path.join(repo, "scripts", "pack", "mint_run.sh")
    proc = subprocess.run(["bash", mint_run, "verdict", "--root", root, "--process",
                           "--since", str(since), "--timeout", str(timeout)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


STOPPED = ("not_responding", "restart_failed", "left_down")


def restart_member(repo, root):
    """Restart ONE agent through the paths that already exist, then judge it
    with the same liveness verdict the update run uses (item 24).  Units
    that are not loaded are loaded by the update run's own restart step
    (mint_run.sh start); when everything was already loaded, the agent's
    own /restart script (bridge/self_restart.sh --trigger user) restarts the
    stuck bridge.  True only when the verdict saw this life come up."""
    since = int(time.time())
    mint_run = os.path.join(repo, "scripts", "pack", "mint_run.sh")
    proc = subprocess.run(["bash", mint_run, "start", "--root", root], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    loaded = proc.returncode == 0 and "[start] loaded " in proc.stdout.decode("utf-8", "replace")
    script = os.path.join(root, "bridge", "self_restart.sh")
    if not loaded and os.access(script, os.X_OK):
        try:
            subprocess.run([script, "--trigger", "user", "--reason", "update: the agent stayed stopped after the update"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
    return alive(repo, root, since)


def units_for(item, units, only):
    """update_apply unit records that belong to this item (unit names:
    "framework <name>", "crew <kit>/<id>", "<member>/<id>", "pack <id>",
    "atomic group [...]").  A single-item run owns every unit."""
    # DGN-1922: a riding retire unit is housekeeping, not this item's own
    # result -- its outcome reaches the record, never this item's verdict.
    units = [u for u in units if not u.get("unit", "").startswith("retire ")]
    if any("story_key" in unit for unit in units):
        return [unit for unit in units if unit.get("story_key") == item["key"]]
    if only:
        return list(units)
    pid = item["id"]
    members = item.get("members") or [os.path.basename(r.rstrip('/')) for r in item.get("member_roots", [])]
    out = []
    for unit in units:
        name = unit.get("unit", "")
        if name.startswith("atomic group"):
            if item["key"].startswith("bundle:") and pid in name:
                out.append(unit)
        elif item["kind"] == "framework":
            # A crew framework row (item 26) owns every member's unit.
            if name in ["framework " + member for member in members]:
                out.append(unit)
        elif not item["key"].startswith("bundle:") and (
                name == "pack " + pid or name == "crew %s/%s" % (item.get("crew_kit") or pid, pid)
                or name in [member + "/" + pid for member in members]):
            out.append(unit)
    return out


def unit_class(units):
    """ok / blocked / incomplete for a set of update_apply unit records."""
    results = set(u.get("result") for u in units)
    if not units:
        return "blocked"
    if results == {"ok"}:
        return "ok"
    if results <= {"blocked", "untried"}:
        return "blocked"
    return "incomplete"


def item_state(unit, outcome, members):
    """complete / blocked / partial / recovery_required for one item.
    DGN-1417 grill 5: results split per member -- a crew framework row whose
    members' own units differ (one landed and verified, one stopped before any
    write) is "partial", not "recovery_required": nothing needs recovering,
    and the landed member is not reported as untouched."""
    states = [m["status"] for m in members]
    if unit == "ok" and outcome in ("done", "partial") and all(s == "verified" for s in states):
        # outcome "partial": this item's own unit landed and verified; another
        # item in the same run did not.  The run as a whole is still never done.
        return "complete"
    if unit == "blocked" and all(s == "not_changed" for s in states):
        return "blocked"
    if outcome in ("done", "partial") and set(states) == {"verified", "not_changed"}:
        return "partial"
    return "recovery_required"


def judge(ledger, item, record, since, only=True, alive_cache=None, revived=None):
    """Per-member and per-item status.  complete ONLY when the unit applied
    and every member came back on the target version.  `revived` = roots a
    restart retry brought back ({root: verdict}): a revived member of an
    applied unit counts as back.  A framework member is judged by ITS OWN
    unit ("framework <name>") when the record has one (DGN-1417: the
    09-27 two-member row marked the landed member "incomplete" because the
    other member's unit was blocked)."""
    alive_cache = {} if alive_cache is None else alive_cache
    revived = revived or {}
    members = []
    units = units_for(item, (record or {}).get("units") or [], only)
    unit = "unknown" if record is None else unit_class(units)
    down = set((record or {}).get("bridges_left_down") or [])
    rfail = set((record or {}).get("restart_failures") or [])
    unconfirmed = set((record or {}).get("bridges_state_unconfirmed") or [])
    for name, root in zip(item["members"], item["member_roots"]):
        base = os.path.basename(root)
        own = [u for u in units if u.get("unit") == "framework " + name] if item["kind"] == "framework" else []
        munit = unit_class(own) if own and unit != "unknown" else unit
        if munit == "unknown":
            status = "unknown"
        elif base in down:
            status = "left_down"
        elif base in rfail:
            status = "restart_failed"
        elif base in unconfirmed:
            status = "unconfirmed"
        elif munit == "blocked":
            status = "not_changed"
        elif munit != "ok":
            status = "incomplete"
        elif not on_target(item, root):
            status = "version_mismatch"
        else:
            if root not in alive_cache:
                alive_cache[root] = alive(ledger["repo"], root, since)
            status = "verified" if alive_cache[root] else "not_responding"
        if munit == "ok" and status in STOPPED and revived.get(root):
            status = "verified" if on_target(item, root) else "version_mismatch"
        members.append({"name": name, "root": root, "status": status, "unit": munit,
                        "label": agent_label(root, name, ledger.get("lang", "ko"))})
    state = item_state(unit, (record or {}).get("outcome"), members)
    return {"key": item["key"], "name": item["name"], "current": item["current"], "target": item["target"],
            "unit": unit, "reasons": [u.get("reason", "") for u in units if u.get("result") != "ok"],
            "status": state, "members": members}


def member_unit(result, member):
    """The member's own unit class (judge), else the item's (older ledgers)."""
    return member.get("unit") or result["unit"]


def overall(results):
    states = set(r["status"] for r in results)
    if states == {"complete"}:
        return "complete"
    if states == {"blocked"}:
        return "blocked"
    if "recovery_required" in states:
        return "recovery_required"
    return "partial"


def stopped_members(results, lang):
    """Agents left stopped after an applied unit, once each, in result order."""
    out = []
    for result in results:
        for member in result["members"]:
            if member_unit(result, member) != "ok":
                continue
            if member["status"] in STOPPED and member["root"] not in [m["root"] for m in out]:
                out.append({"name": member["name"], "root": member["root"],
                            "label": member_label(member["root"], member["name"], lang)})
    return out


def partial_only(results):
    """Item 24's case: everything applied on disk, and the only thing wrong
    is that some agent did not come back."""
    stuck = False
    for result in results:
        if result["status"] == "complete":
            continue
        if result["unit"] != "ok" or any(m["status"] not in ("verified",) + STOPPED for m in result["members"]):
            return False
        stuck = stuck or any(m["status"] in STOPPED for m in result["members"])
    return stuck


def _agents(labels, lang):
    joined = join_and(labels, lang)
    return with_josa(joined, "이", "가") if lang == "ko" else joined


def partial_text(results, items, lang):
    """The fixed result surface after a restart callback."""
    return result_text(result_from_verified(results, items, None, lang), lang)


def restart_buttons(ledger, lang, only_roots=None):
    """One "<agent> 재시작" button per agent still stopped.  The index is
    into the ledger's recorded `stopped` list, which never shrinks, so an
    older message's button still names the same agent."""
    rows = []
    for index, member in enumerate((ledger.get("result") or {}).get("stopped") or []):
        if only_roots is not None and member["root"] not in only_roots:
            continue
        rows.append([{"text": copy(lang)["restart_button"].format(agent=member["label"]),
                      "callback_data": "upd:r:%s:%d" % (ledger["id"], index)}])
    return rows


def result_buttons(record, ledger, lang, only_roots=None):
    """Lease repair and drift checks ask the agent through opt:, never act on tap.

    Keep the held kit ID in the callback: the confirmed short button label
    cannot identify it, and the failed unit may be a dependent agent pack.
    The shared option resolver appends this ID to the owner's chat turn.
    """
    rows = []
    kits, agents = set(), set()
    for unit in record.get("units") or []:
        if unit.get("result") == "blocked" and unit.get("reason_code") == "update.content-drift":
            for member in unit.get("members") or []:
                slug = member.get("name", "")
                if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", slug) or slug in agents:
                    continue
                agents.add(slug)
                rows.append([{"text": word(lang, "update.drift_check_button"),
                              "callback_data": "opt:drift-check:" + slug}])
        if unit.get("result") == "ok" or unit.get("reason_code") != "crew.lease-active":
            continue
        kit = (unit.get("reason_payload") or {}).get("kit", "")
        if not isinstance(kit, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", kit) or kit in kits:
            continue
        kits.add(kit)
        rows.append([{"text": word(lang, "update.repair_button"),
                      "callback_data": "opt:repair-update:" + kit}])
    failures = [u for u in record.get("units") or []
                if u.get("result") != "ok" and u.get("reason_code") == "update.refit-preflight"]
    if failures:
        # Local evidence only. An opt: tap asks for a draft, never submits it.
        context = {"failures": [{"reason_code": u["reason_code"], "writes": u.get("writes"),
                    "current": (u.get("reason_payload") or {}).get("current", u.get("current", "")),
                    "target": (u.get("reason_payload") or {}).get("target", u.get("target", "")),
                    "members": u.get("members") or [(u.get("reason_payload") or {}).get("member", {})],
                    "log_path": (u.get("reason_payload") or {}).get("log_path", ""),
                    "stage": (u.get("reason_payload") or {}).get("stage", "")}
                    for u in failures]}
        token = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:32]
        path = os.path.join(ledger["instance_root"], ".telegram_bot", "update-reports", token + ".json")
        atomic_write(path, context)
        rows.append([{"text": word(lang, "update.refit_report_button"),
                      "callback_data": "opt:report-update:" + token}])
    return rows + restart_buttons(ledger, lang, only_roots)


def completion_text(results, items, lang):
    """The fixed result surface, including the restart callback completion."""
    return result_text(result_from_verified(results, items, None, lang), lang)


def fallback_text(results, items, lang):
    return result_text(result_from_verified(results, items, None, lang), lang)


def push(ledger, text, buttons=None, receipt=False, edit=None):
    script = os.path.join(ledger["instance_root"], "routines", "push.sh")
    argv = ["bash", script, "--strict", "--text", text, "--audience", "owner"]
    if ledger.get("chat"):
        argv += ["--chat", ledger["chat"]]
    if buttons:
        argv += ["--buttons-json", json.dumps(buttons, ensure_ascii=False)]
    if edit is not None:
        argv += ["--edit", str(edit)]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10 if edit is not None else None)
    except (OSError, subprocess.TimeoutExpired):
        return {} if receipt else False
    if receipt:
        if proc.returncode not in ((0, 8) if edit is not None else (0,)):
            return {"returncode": proc.returncode,
                    "stderr_tail": proc.stderr.decode("utf-8", "replace")[-2000:]}
        try:
            return dict(json.loads(proc.stdout), delivered=True)
        except (ValueError, TypeError):
            # Older senders can confirm delivery without an editable receipt.
            return {"delivered": True}
    return proc.returncode in (0, 8) if edit is not None else proc.returncode == 0


def push_notice(ledger, text):
    """Send a safety notice, retrying only the two transient push outcomes."""
    receipt = push(ledger, text, receipt=True)
    if receipt.get("returncode") in (2, 6):
        receipt = push(ledger, text, receipt=True)
    return receipt


GUIDE_PROBLEM = """The machine owns the entire failure notice, including the reason and
alternative. Return NO_PUSH. Write zero owner-facing text. Never infer a cause
from raw logs or add a second explanation."""
GUIDE_FAILURE_HANDOFF = """The machine owns the entire failure notice, including the reason and
alternative. Return NO_PUSH. Write zero owner-facing text now. Never infer a cause
from raw logs or add a second explanation.
Treat this failure as your own work item: read the unit's
recorded stderr or refusal line and inspect the installed pack, find the cause,
then fix it in the owning repository or file a ticket. Tell the owner only when
a fixed version is ready or an owner decision is needed."""
GUIDE_COMPLETE = """The machine owns the entire done notice. Return NO_PUSH.
Write zero owner-facing text."""


def failure_uses_agent_handoff(unit):
    """Whether failure_text renders update.failure_agent_handoff for a unit."""
    code = unit.get("reason_code", "")
    payload = unit.get("reason_payload") or {}
    if code == "kit.channel-unavailable" and payload.get("kit_product") and payload.get("required_version"):
        return False
    if code in ("network", "install.network", "update.content-drift",
                "crew.lease-active", "kit.dependency-blocked", "update.restart-failed"):
        return False
    if not code and unit.get("dependency_product"):
        return False
    if code in ("update.notice-not-connected", "update.notice-uncertain",
                "update.notice-send-failed", "update.refit-preflight"):
        return False
    return True


def failure_text(unit, lang, agent=None):
    """dec-514: confirmed reason codes only; raw logs never become causes."""
    code = unit.get("reason_code", "")
    reason = word(lang, "update.failure_unknown")
    alternative = word(lang, "update.failure_agent_handoff")
    payload = unit.get("reason_payload") or {}
    if code == "kit.channel-unavailable" and payload.get("kit_product") and payload.get("required_version"):
        reason = word(lang, "update.failure_channel").format(
            kit=payload["kit_product"], version=vfmt(payload["required_version"]))
        alternative = word(lang, "update.failure_channel_next").format(kit=payload["kit_product"])
    elif code in ("network", "install.network"):
        reason = word(lang, "update.failure_network")
        alternative = word(lang, "update.failure_network_next")
    elif code in ("no_version", "kit.no-version"):
        reason = word(lang, "update.failure_no_version")
    elif code in ("install.post-install-refused", "install.post-install-failed"):
        key = ("update.failure_post_install_refused" if code.endswith("refused")
               else "update.failure_post_install_failed")
        reason = word(lang, key).format(
            kit_product=payload.get("kit_product") or unit.get("product") or "",
            version=vfmt(payload.get("version") or unit.get("target") or ""))
    elif code in ("error", "install.error"):
        reason = word(lang, "update.failure_install")
    elif code in ("update.notice-not-connected", "update.notice-uncertain",
                  "update.notice-send-failed"):
        # The push result is a confirmed failure class, not an inference from
        # free-form stderr. Keep the established retry action for the owner.
        reason = ("업데이트 시작 알림을 전달하지 못했어요."
                  if lang == "ko" else "The update start notice could not be delivered.")
    elif code == "update.refit-preflight":
        agent = agent or AGENT_SEP.join(unit.get("agents") or [])
        if not agent:
            member = payload.get("member") or next(iter(unit.get("members") or []), {})
            agent = member_label(member.get("root", ""), member.get("name", ""), lang)
        return word(lang, "update.refit_preflight").format(
            agent=agent,
            agent_topic=with_josa(agent, "은", "는") if lang == "ko" else agent)
    elif code == "update.content-drift":
        reason = word(lang, "update.failure_content_drift")
        alternative = word(lang, "update.failure_content_drift_next")
    elif code == "crew.lease-active":
        reason = word(lang, "update.failure_lease")
        alternative = word(lang, "update.failure_lease_next")
    elif code == "kit.dependency-blocked":
        reason = word(lang, "update.failure_dependency_blocked")
        alternative = word(lang, "update.failure_dependency_blocked_next")
    elif not code and unit.get("dependency_product"):
        dep = unit["dependency_product"]
        if dep.startswith("[") and "] " in dep:
            dep = dep.split("] ", 1)[1]
        if lang == "ko":
            dep = with_josa(dep, "\uC744", "\uB97C")
        reason = word(lang, "update.failure_dependency").format(dep=dep)
        alternative = word(lang, "update.failure_dependency_next")
    elif not code and unit.get("result") == "untried":
        reason = word(lang, "update.failure_untried")
    elif code == "update.restart-failed":
        reason = word(lang, "update.failure_restart").format(agents=payload["agents"])
        alternative = word(lang, "update.failure_restart_next")
    return "\n".join((word(lang, "update.failure"),
                      word(lang, "update.failure_reason").format(reason=reason),
                      word(lang, "update.failure_alternative").format(alternative=alternative)))


def result_text(record, lang=None):
    """Keep successful cards; render each failure in the dec-514 shape."""
    lang = lang or record.get("lang", "ko")
    units = record.get("units") or []
    failures = [u for u in units if u.get("result") != "ok"]
    down = list(dict.fromkeys((record.get("bridges_left_down_labels") or []) +
                             (record.get("restart_failures_labels") or [])))
    if down:
        failures.append({"reason_code": "update.restart-failed", "reason_payload": {"agents": _agents(down, lang)}})
    if not failures and record.get("outcome") != "done":
        failures.append({})
    agent = record.get("notice_agent")
    notices = list(dict.fromkeys(failure_text(u, lang, agent) for u in failures))
    if len(failures) == 1 and not any(u.get("result") == "ok" for u in units):
        return html.escape(notices[0])
    blocks = [word(lang, "update.result_done")] if not failures else []
    items = {}
    for unit in units:
        if unit.get("result") != "ok":
            continue
        key = (unit.get("product", ""), unit.get("target", ""))
        item = items.setdefault(key, {"current": unit.get("current", ""), "shown": unit.get("target_shown"), "results": {}})
        words = unit.get("migration_done") or word(lang, "update.result_ok")
        agents = item["results"].setdefault(words, [])
        for label in unit.get("agents") or []:
            if label not in agents:
                agents.append(label)
    for (product, target), item in items.items():
        head_key = "update.result_item_head" if item["current"] else "update.item_head"
        quote = ["<b>%s</b>" % html.escape(word(lang, head_key).format(
            product=product, current=vfmt(item["current"]), target=item["shown"] or vfmt(target)))]
        quote += [html.escape(word(lang, "update.result_target").format(agents=AGENT_SEP.join(agents), result=words))
                  for words, agents in item["results"].items()]
        blocks.append("<blockquote>%s</blockquote>" % "\n".join(quote))
    # dec-514/515 failure notices remain plain, with product/agent context
    # above each distinct reason. DGN-2252 / dec-540 changes drift copy
    # and its action button, not this layout.
    for notice in notices:
        matching = [u for u in failures if failure_text(u, lang, agent) == notice]
        if len(failures) > 1 or items:
            for unit in matching:
                if not unit.get("product"):
                    continue
                head_key = "update.result_item_head" if unit.get("current") else "update.item_head"
                title = word(lang, head_key).format(product=unit["product"],
                    current=vfmt(unit.get("current", "")), target=unit.get("target_shown") or vfmt(unit.get("target", "")))
                blocks.append("<b>%s</b>\n%s" % (html.escape(title), html.escape(AGENT_SEP.join(unit.get("agents") or []))))
        blocks.append(html.escape(notice))
    return "\n\n".join(blocks)


def with_versions(text, ledger, lang):
    """DGN-1790: an update result ends with what is installed now.  Read
    after the apply and its verification, so it is the post-update truth;
    unreadable -> the result text alone."""
    if word(lang, "update.failure") in text or word(lang, "update.refit_preflight").split("\n", 1)[0] in text:
        return text
    try:
        block = versions_text(ledger["repo"], crew_roots(ledger["instance_root"], ledger["repo"]), lang)
    except Exception:
        block = ""
    return text + "\n\n" + block if block else text


def result_from_verified(results, items, record, lang):
    """Keep apply's per-unit display fields; incorporate final restart verification."""
    rec = dict(record or {}, lang=lang, outcome={"complete": "done", "blocked": "blocked"}.get(overall(results), "partial"))
    units = [dict(u) for u in rec.get("units") or []]
    # Older records may lack display metadata. Resolve through the existing seats.
    for result, item in zip(results, items):
        matches = units_for(item, units, len(items) == 1)
        if not matches:
            matches = [{"result": result["unit"]}]; units.extend(matches)
        for unit in matches:
            if item.get("migrate_from") and result["status"] == "complete":
                unit["migration_done"] = migration_line(item, lang, done=True)
                unit["product"] = item["name"]
            unit.setdefault("product", item["name"])
            own = [m for m in result["members"] if unit.get("unit") == "framework " + m["name"] or unit.get("unit", "").startswith(m["name"] + "/")]
            unit.setdefault("agents", [m["label"] for m in own or result["members"]])
            unit.setdefault("target", item["target"])
            unit.setdefault("current", item.get("current", ""))
    rec["units"] = units
    rec["bridges_left_down_labels"] = []
    rec["restart_failures_labels"] = list(dict.fromkeys(m["label"] for r in results for m in r["members"] if m["status"] in STOPPED))
    return rec


def inbox_drop(ledger, payload, complete=False):
    spool = os.path.join(ledger["instance_root"], ".telegram_bot", "session-inbox")
    if not os.path.isdir(os.path.join(ledger["instance_root"], ".telegram_bot")):
        return False
    if not os.path.isdir(spool):
        os.makedirs(spool)
    name = "update-ledger-%s.md" % ledger["id"]
    record = payload.get("apply_record") or {}
    failures = [unit for unit in record.get("units") or [] if unit.get("result") != "ok"]
    if not failures and record.get("outcome") != "done":
        failures.append({})
    guide = (GUIDE_COMPLETE if complete else
             GUIDE_FAILURE_HANDOFF if any(failure_uses_agent_handoff(unit) for unit in failures)
             else GUIDE_PROBLEM)
    body = """[cron-inject] update result (DGN-1417)

The owner asked for this update from the update notice or the update screen.
The record below is the machine's account of it -- it is for you, not for the
owner.

%s

```json
%s
```
""" % (guide, json.dumps(payload, ensure_ascii=False, indent=2))
    tmp = os.path.join(spool, "." + name + ".tmp")
    with open(tmp, "w") as handle:
        handle.write(body)
    os.rename(tmp, os.path.join(spool, name))
    return True


def result_routes(ledger, items, results, record):
    """Every notified crew chat receives the requester's complete result.

    A run touching just one other agent keeps its existing own-chat route.
    """
    crew = ledger.get("crew_update") or crew_run(items)
    routes = {}
    for item, result in zip(items, results):
        roots = list(dict.fromkeys(os.path.realpath(r) for r in item["member_roots"]))
        root = roots[0] if len(roots) == 1 and not crew else ledger["instance_root"]
        key = os.path.realpath(root)
        route = routes.setdefault(key, {"root": root, "items": [], "results": [], "units": []})
        route["items"].append(item)
        route["results"].append(result)
        route["units"].extend(units_for(item, (record or {}).get("units") or [], len(items) == 1))
    if crew and routes:
        requester = routes[os.path.realpath(ledger["instance_root"])]
        for root, status in (ledger.get("start_notices") or {}).items():
            if status == "sent":
                key = os.path.realpath(root)
                routes.setdefault(key, dict(requester, root=key))
    return list(routes.values())


def deliver(ledger, items, results, record):
    """One result card per destination, with successful routes kept on retry."""
    lock = ledger_path(ledger["id"]) + ".delivered"
    if not claim(lock):
        return None
    story = (ledger.get("result") or {}).get("story")
    if story:
        items, results, record = story["items"], story["results"], story["record"]
    else:
        items, results, record = report_story(ledger, items, results, record)
    stopped = stopped_members(results, ledger.get("lang", "ko"))
    led = load_ledger(ledger["id"])
    led["result"] = dict(led.get("result") or {}, stopped=stopped)
    if ledger.get("retry_context"):
        led["result"]["story"] = dict(items=items, results=results, record=record)
        led["state"] = overall(results)
    atomic_write(ledger_path(ledger["id"]), led)
    ledger["result"] = led["result"]
    sent = dict(led.get("result_cards") or {})
    failed = False
    crew = ledger.get("crew_update") or crew_run(items)
    # Stop-send persists member receipts after the supervisor loaded its ledger.
    routing = dict(ledger, start_notices=led.get("start_notices") or {})
    routes = sorted(result_routes(routing, items, results, record),
                    key=lambda route: not same_root(route["root"], ledger["instance_root"]))
    for route in routes:
        root = route["root"]
        requester = same_root(root, ledger["instance_root"])
        if sent.get(os.path.realpath(root)) == "sent":
            continue
        lang = ledger.get("lang", "ko") if requester else notice_lang(root, ledger["repo"])
        dest = dict(ledger, instance_root=root, chat=ledger.get("chat", "") if requester else "")
        # Restart buttons in any chat still reference the original ledger.
        sub = dict(record or {}, units=route["units"])
        rec = (result_from_verified(route["results"], route["items"], sub, lang) if requester or crew
               else member_result_record(ledger, route["items"], route["results"], sub, root, lang))
        rec["notice_agent"] = member_label(root, os.path.basename(root), lang)
        remember_channel_failure(dest, rec)
        button_rec = (member_result_record(ledger, route["items"], route["results"], sub, root, lang)
                      if crew and not requester else rec)
        only_roots = ([root] if not requester else
                      [r for item in route["items"] for r in item["member_roots"]])
        text = with_versions(result_text(rec, lang), ledger if crew else dest, lang)
        offer = post_update_try_offer(ledger, route["items"], route["results"], root, lang)
        if offer:
            text += "\n\n" + html.escape(offer)
        ok = push(dest, text,
                  result_buttons(button_rec, dest, lang, only_roots=only_roots))
        sent[os.path.realpath(root)] = "sent" if ok else "push_failed"
        failed = failed or not ok
        led = load_ledger(ledger["id"])
        led["result_cards"] = sent
        atomic_write(ledger_path(ledger["id"]), led)
        if ok and rec["outcome"] != "done":
            inbox_drop(dest, {"status": overall(route["results"]), "apply_record": rec})
    ledger["result_cards"] = sent
    if failed:
        os.unlink(lock)
        return {"method": "none", "at": int(time.time())}
    return {"method": "push-partial" if stopped else "push", "at": int(time.time())}


# --------------------------------------------------------------------------
# DGN-1758: restart-complete notice in each restarted agent's own chat.
# --------------------------------------------------------------------------

def restart_notice_targets(ledger, items, results):
    """[{root, version, source, product}] for every agent this run brought
    back verified (unit applied, live since the run, on target), except the
    requester's own agent (its chat has the result notice).  One entry per
    agent: its framework version wins, else the first item that touched it
    (a pack/kit carries its own version and note).  `product` is "" for a
    framework pick (header stays byte-identical) and, for a pack/kit-only
    pick, the resolved display name or "" when unresolvable (DGN-1758
    pack-name: the renderer falls back to the framework-style header, never
    a slug)."""
    lang = ledger.get("lang", "ko")
    picked = {}
    for item, result in zip(items, results):
        for member in result["members"]:
            root = member["root"]
            if member_unit(result, member) != "ok":
                continue
            if member["status"] != "verified" or same_root(root, ledger["instance_root"]):
                continue
            fw = None
            if item["kind"] == "framework":
                fw = (item["target"], item.get("note") or note_source("framework", item["id"], item["target"], "", ledger["repo"], lang))
            for inc in item.get("includes") or []:
                if same_root(inc["root"], root):
                    fw = (inc["target"], note_source("framework", inc["name"], inc["target"], "", ledger["repo"], lang))
            key = os.path.realpath(root)
            if fw is not None and not picked.get(key, {}).get("fw"):
                picked[key] = {"root": root, "version": fw[0], "source": fw[1], "fw": True, "product": ""}
            elif key not in picked:
                own = item.get("note") or note_source(item["kind"], item["id"], item["target"], item.get("cand_tag", ""), ledger["repo"], lang)
                product = resolved_product_name(item["kind"], item["id"], item.get("cand_tag", ""), lang)
                picked[key] = {"root": root, "version": item["target"], "source": own, "fw": False, "product": product}
    return list(picked.values())


def render_restart_notice(repo, root, version, source, product=""):
    """The self-update.sh renderer (--restart-notice), in this agent's own
    language.  No note -> the header line only (DGN-784 safe silence); no
    product -> the framework-style header (DGN-1758 pack-name)."""
    script = os.path.join(repo, "agents", ".template", "routines", "self-update.sh")
    if not os.path.isfile(script):
        return ""
    text = git_show(source.get("repo"), source.get("commit"), source.get("path")) if source else None
    fd, note = tempfile.mkstemp(prefix="dgn1758-note-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text or "")
        env = dict(os.environ, DOGANY_LANG=notice_lang(root, repo))
        argv = ["sh", script, "--restart-notice=" + norm(version), "--notice-file=" + note]
        if product:
            argv.append("--product=" + product)
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    finally:
        os.unlink(note)
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""


def leads_with_glyph(text):
    """The line opens with a symbol glyph (emoji / pictograph), not a word."""
    return bool(text) and unicodedata.category(text[0]) == "So"


def push_agent_notice(root, text):
    """bridge/self_restart.sh's notify step for this agent: the persona prefix
    from the one resolver (applied once), then its own push.sh.  The one
    per-agent sender of this surface (restart notice, DGN-1781 start line)."""
    prefix = ""
    resolver = os.path.join(root, "routines", "lib", "agent_prefix.py")
    if os.path.isfile(resolver):
        proc = subprocess.run([sys.executable, resolver, root], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        prefix = proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""
    # DGN-1795: a machine line that already opens with its own glyph (the
    # DGN-1781 house-emoji start line) goes out as the requester saw it -- no
    # second, persona glyph in front.
    if prefix and not text.startswith(prefix) and not leads_with_glyph(text):
        text = prefix + " " + text
    argv = ["bash", os.path.join(root, "routines", "push.sh"), "--env", os.path.join(root, ".telegram_bot", ".env"),
            "--strict", "--text", text, "--audience", "owner"]
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def send_restart_notices(ledger, items, results, record):
    """DGN-2157: a led update's result owns completion, including members.

    Keep the entry point for recovery callers from older supervisors, but
    send no independent member restart/return notice for a led run.
    Standalone self-update and /restart use their own existing senders.
    """
    return {}


def member_result_record(ledger, items, results, record, root, lang):
    """Narrow units and outcomes for solo member cards and crew actions.

    Crew result text uses the full run; only its actions use this scope.
    """
    scoped_items, scoped_results, scoped_units = [], [], []
    for item, result in zip(items, results):
        member = next((m for m in result["members"] if same_root(m["root"], root)), None)
        if member is None:
            continue
        scoped_items.append(item)
        unit = member_unit(result, member)
        status = item_state(unit, (record or {}).get("outcome"), [member])
        scoped_results.append(dict(result, unit=unit, status=status, members=[member]))
        matches = units_for(item, (record or {}).get("units") or [], len(items) == 1)
        if item["kind"] == "framework":
            own = [u for u in matches if u.get("unit") == "framework " + member["name"]]
            matches = own or matches
        for source in matches:
            rendered = dict(source)
            rendered["agents"] = [member["label"]]
            if rendered.get("members"):
                rendered["members"] = [m for m in rendered["members"] if same_root(m.get("root", ""), root)]
            scoped_units.append(rendered)
    scoped_record = dict(record or {}, units=scoped_units)
    return result_from_verified(scoped_results, scoped_items, scoped_record, lang)


def cmd_result_send(args):
    record = json.load(sys.stdin)
    lang = record.get("lang", "ko")
    record["notice_agent"] = member_label(args.instance_root, os.path.basename(args.instance_root), lang)
    owner = ""
    for path in (os.path.join(args.instance_root, ".telegram_bot", ".env"),
                 os.path.join(args.instance_root, "runtime", ".env"), os.path.expanduser("~/telegram_bot/.env")):
        owner = read_conf(path).get("ALLOWED_USER_IDS", "").strip("\"'").split(",")[0].strip()
        if owner:
            break
    if not owner and (record.get("bridges_left_down_members") or record.get("restart_failures_members")):
        raise ValueError("restart buttons require the configured owner")
    stopped = []
    for key in ("bridges_left_down_members", "restart_failures_members"):
        for member in record.get(key) or []:
            if member["root"] not in [m["root"] for m in stopped]:
                stopped.append(member)
    ledger = {"id": str(uuid.uuid4()), "repo": args.repo, "instance_root": args.instance_root,
              "requester": owner, "chat": "", "lang": lang, "items": [], "selection": [],
              "state": "partial", "result": {"stopped": stopped}}
    if stopped:
        atomic_write(ledger_path(ledger["id"]), ledger)
    remember_channel_failure(ledger, record)
    return 0 if push(ledger, with_versions(result_text(record, lang), ledger, lang), result_buttons(record, ledger, lang)) else 1


# DGN-2157: the progress shape is shared; each owner-facing word is resolved
# through the requester's language bundle.
def progress_stages(ledger):
    lang = ledger.get("lang", "ko")
    stages = []
    for item in selected(ledger):
        lines = [line.split("\t") for line in item["lines"]]
        pack = next((f for f in lines if f[0] in ("PACK", "ATOMIC-PACK")), None)
        kind = item.get("kind") or ("pack" if pack is not None else "framework")
        if pack is not None or kind != "framework":
            roots = [m.partition(":")[2] for m in pack[7].split()] if pack else item["member_roots"]
            # Ledgers issued before the display-name field was added (and
            # recovery fixtures that faithfully model them) still carry a
            # stable id/key.  Progress delivery must not block the update's
            # stop/re-arm safety path merely because presentation metadata is
            # absent.
            name = re.sub(r"^\[[^]]+\]\s*", "", item.get("name") or item.get("id") or item["key"])
            if kind == "pack" and not name.endswith(word(lang, "update.progress_pack_suffix")):
                name += " " + word(lang, "update.progress_pack_suffix")
            action = "update.progress_update" if item.get("current") else "update.progress_install"
            stages.append({"kind": kind, "id": item.get("id") or (pack[1] if pack else ""), "key": item["key"],
                           "name": word(lang, action).format(name=name), "roots": roots, "labels": dict(zip(item.get("member_roots") or [], item.get("labels") or []))})
        fws = [f for f in lines if f[0] in ("FW", "ATOMIC-FW")]
        if fws:
            labels = list(dict.fromkeys(member_label(f[2], f[1], lang) for f in fws))
            stages.append({"kind": "framework", "id": "", "key": item["key"],
                           "name": word(lang, "update.progress_update").format(name=AGENT_SEP.join(labels) + " " + word(lang, "update.layer_framework").strip("[]"))
                           + framework_suffix([(f[4], f[2]) for f in fws], ledger["repo"], lang),
                           "roots": [f[2] for f in fws]})
    # The same pack can be selected for several agents; tell those stages apart.
    names = [stage["name"] for stage in stages]
    for stage in stages:
        labels = [stage.get("labels", {}).get(root) for root in stage["roots"]]
        if names.count(stage["name"]) > 1 and all(labels):
            stage["name"] += " (%s)" % AGENT_SEP.join(dict.fromkeys(labels))
    stages.append({"kind": "restart", "id": "", "key": "", "name": word(lang, "update.progress_restart"), "roots": []})
    return stages


PROGRESS_IDLE_REFRESH = 5


def progress_text(ledger, stages, states=None, now=None, member=False):
    states = states or ["running"] + ["pending"] * (len(stages) - 1)
    done = states.count("done")
    filled = 10 * done // len(stages)
    seconds = max(0, int((time.time() if now is None else now) - ledger["started_at"]))
    minutes, seconds = divmod(seconds, 60)
    lang = ledger.get("lang", "ko")
    elapsed = word(lang, "update.progress_minutes" if minutes else "update.progress_seconds").format(minutes=minutes, seconds=seconds)
    head = word(lang, "update.progress_head")
    if not any(s in ("pending", "running") for s in states) and any(s != "done" for s in states):
        head = word(lang, "update.progress_stopped")
    elif member and len(states) == len(stages) and all(s == "done" for s in states):
        head = word(lang, "update.progress_complete")
    bar = "%s %d/%d" % ("\u2593" * filled + "\u2591" * (10 - filled), done, len(stages))
    mark = {"done": "[v]", "running": "[>]", "pending": "[ ]", "blocked": "[x]", "incomplete": "[!]"}
    rows = [mark[state] + " " + stage["name"] for stage, state in zip(stages, states)]
    body = "\n".join([head, bar, elapsed, ""] + rows)
    # push.sh's sanitizer escapes & < > itself; escaping here would show "&gt;" literally.
    return "<pre>%s</pre>" % body


class UpdateProgress:
    """Supervisor-owned edits survive bridge teardown; display failures are inert.

    Apply emits facts to a private append-only stream, never Telegram calls.
    Only this monitor edits the receipt from the strict pre-stop sender. It
    never writes the ledger (stop-send and the supervisor own those writes).
    """
    def __init__(self, ledger):
        self.ledger = ledger
        self.stages = progress_stages(ledger)
        self.states = ["running"] + ["pending"] * (len(self.stages) - 1)
        self.done_roots = [set() for _ in self.stages]
        self.events = ledger_path(ledger["id"]) + ".progress"
        self.offset = 0
        self.pending = []
        self.last_edit = time.monotonic()
        self.receipt = None
        self.disabled = False
        self.member_disabled = set()
        self.backoff = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.watch, name="update-progress", daemon=True)

    def read_events(self):
        try:
            with open(self.events) as handle:
                handle.seek(self.offset)
                lines = handle.readlines()
                # A half-written event is retried on the next poll.
                for line in lines:
                    if not line.endswith("\n"):
                        break
                    self.offset += len(line.encode("utf-8"))
                    fields = line.rstrip("\n").split("\t")
                    if len(fields) != 4:
                        continue
                    kind, pid, members, status = fields
                    before = list(self.states)
                    for i, stage in enumerate(self.stages):
                        if stage["kind"] != kind or (kind != "framework" and stage["id"] != pid):
                            continue
                        roots = {os.path.realpath(m.partition(":")[2]) for m in members.split()}
                        expected = {os.path.realpath(r) for r in stage["roots"]}
                        if kind != "restart" and not roots.intersection(expected):
                            continue
                        if status == "ok":
                            self.done_roots[i].update(roots)
                            if expected <= self.done_roots[i]:
                                self.states[i] = "done"
                        elif status == "running":
                            self.states[i] = "running"
                        elif status in ("blocked", "incomplete", "untried"):
                            self.states[i] = "incomplete" if status == "incomplete" else "blocked"
                    if self.states != before:
                        self.pending.append(list(self.states))
        except OSError:
            pass

    def destinations(self):
        progress = load_ledger(self.ledger["id"]).get("progress") or {}
        self.receipt = progress.get("receipt")
        targets = [(self.ledger["instance_root"], self.receipt, True)]
        targets.extend((root, entry.get("receipt"), False)
                       for root, entry in (progress.get("members") or {}).items())
        return [(root, receipt, leader) for root, receipt, leader in targets
                if receipt and receipt.get("message_id") is not None]

    @staticmethod
    def log_edit_failure(outcome):
        tail = str(outcome.get("stderr_tail") or "").replace("\r", " ").replace("\n", " ")[-500:]
        print("progress edit failed: rc=%s stderr=%s" %
              (outcome.get("returncode", "unknown"), tail), flush=True)

    def edit(self, states, verified=False, final=False, leader_only=False):
        # The verified terminal frame gives every destination, including the
        # requester, the completion head when (and only when) all stages are done.
        # Share the timestamp so elapsed, bar and rows still match all chats.
        now = time.time()
        text = progress_text(self.ledger, self.stages, states, now=now)
        verified_text = progress_text(self.ledger, self.stages, states, now=now, member=True) if verified else text
        for root, receipt, leader in self.destinations():
            if leader_only and not leader:
                continue
            if (self.disabled if leader else root in self.member_disabled):
                continue
            if not final and self.backoff.get(root, 0) > time.monotonic():
                continue
            dest = dict(self.ledger, instance_root=root,
                        chat=str(receipt.get("chat_id") or (self.ledger.get("chat", "") if leader else "")))
            # Never create a replacement, including after a bridge restart.
            # A final frame bypasses transient backoff and gets its own bounded
            # retry loop; no later monitor tick exists to repair it.
            for attempt in range(5 if final else 1):
                try:
                    # The terminal edit is backed by the completed live
                    # verification, so the requester is entitled to the same
                    # verified completion head as member cards.  Using the
                    # ordinary in-flight text for the leader made a successful
                    # retry land without the final completion notice.
                    body = verified_text if verified else (text if leader else verified_text)
                    outcome = push(dest, body, receipt=True, edit=receipt["message_id"])
                except Exception as error:
                    outcome = {"stderr_tail": str(error)}
                if outcome.get("delivered"):
                    self.backoff[root] = 0
                    break
                self.log_edit_failure(outcome)
                if outcome.get("returncode") == 7:
                    if leader:
                        self.disabled = True
                    else:
                        self.member_disabled.add(root)
                    break
                self.backoff[root] = time.monotonic() + 20
                if final and attempt < 4:
                    time.sleep(6)
        self.last_edit = time.monotonic()

    def refresh(self):
        self.read_events()
        if time.monotonic() - self.last_edit >= (2 if self.pending else PROGRESS_IDLE_REFRESH):
            self.edit(self.pending.pop(0) if self.pending else self.states)

    def watch(self):
        while not self.stop.wait(1):
            try:
                self.refresh()
            except Exception as error:
                # A broken display must never change the update/verdict path.
                print("progress edit failed: %s" % error, flush=True)

    def finish(self, results):
        self.stop.set()
        self.thread.join()
        terminal = {"ok": "done", "blocked": "blocked", "untried": "blocked", "incomplete": "incomplete"}
        final = [next((terminal.get(r["unit"], "blocked") for r in results if r["key"] == stage["key"]),
                      "blocked") for stage in self.stages]
        final[-1] = "done" if overall(results) == "complete" else "blocked"
        try:
            if not self.destinations():
                return
            self.read_events()
            # The terminal frame supersedes queued transitions. Dropping those
            # avoids delaying the only edit that must land.
            self.states = final
            time.sleep(max(0, 2 - (time.monotonic() - self.last_edit)))
            self.edit(self.states, verified=True, final=True)
        except Exception as error:
            print("progress finish failed: %s" % error, flush=True)
        finally:
            self.states = final


# DGN-2296 fix 3: the out-of-process outage watch.  It lives in the update
# supervisor, which survives the bridge/watchdog teardown that silenced every
# in-instance alarm during the 2026-10-25 outage.  It only ever REPORTS: no
# code path here restarts, stops or edits an agent.
#
# DRAFT COPY (owner gate): the wording below is data awaiting the owner's
# approval.  Until OUTAGE_ALERT_COPY_APPROVED is flipped by that approval an
# alert is spooled for the record and rendered to nobody.
OUTAGE_ALERT_COPY_APPROVED = False
OUTAGE_ALERT_DRAFT = {
    "down": {
        "ko": "DRAFT: {agent}\uAC00 {minutes}\uBD84\uC9F8 \uBA48\uCDB0 \uC788\uC2B5\uB2C8\uB2E4. \uC5C5\uB370\uC774\uD2B8\uB294 \uACC4\uC18D \uC9C4\uD589 \uC911\uC774\uBA70 \uC790\uB3D9\uC73C\uB85C \uC7AC\uC2DC\uC791\uD558\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.",
        "en": "DRAFT: {agent} has been down for {minutes} min during the update. The update is still running; nothing was restarted automatically.",
    },
    "stall": {
        "ko": "DRAFT: \uC5C5\uB370\uC774\uD2B8\uAC00 {stall_minutes}\uBD84\uC9F8 \uC9C4\uC804\uC774 \uC5C6\uACE0 {agent}\uAC00 \uBA48\uCDB0 \uC788\uC2B5\uB2C8\uB2E4. \uC790\uB3D9\uC73C\uB85C \uC7AC\uC2DC\uC791\uD558\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.",
        "en": "DRAFT: the update made no progress for {stall_minutes} min and {agent} is down. Nothing was restarted automatically.",
    },
}


def _env_seconds(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return float(default)


class OutageWatch:
    """Alert once per down agent when it stays down past a limit, or when the
    apply stops making progress while an agent is down.

    Facts come from the apply's private event stream (`agent-down` /
    `agent-up`, plus every other event as proof of progress).  Alerts are
    deduplicated per agent per outage and are never an action on the agent.
    Limits: DOGANY_UPDATE_OUTAGE_DOWN_S (180), DOGANY_UPDATE_OUTAGE_STALL_S
    (120); DOGANY_UPDATE_OUTAGE_ALERT=0 turns the watch off.
    """

    def __init__(self, ledger, events, clock=time.monotonic, down_limit=None, stall_limit=None):
        self.ledger = ledger
        self.events = events
        self.clock = clock
        self.down_limit = _env_seconds("DOGANY_UPDATE_OUTAGE_DOWN_S", 180) if down_limit is None else down_limit
        self.stall_limit = _env_seconds("DOGANY_UPDATE_OUTAGE_STALL_S", 120) if stall_limit is None else stall_limit
        self.enabled = os.environ.get("DOGANY_UPDATE_OUTAGE_ALERT", "1") != "0"
        self.offset = 0
        self.down = {}      # root -> (agent, since)
        self.alerted = set()
        self.last_progress = clock()
        self.spool = ledger_path(ledger["id"])[:-len(".json")] + ".alerts"
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.watch, name="update-outage-watch", daemon=True)

    def read_events(self):
        try:
            with open(self.events) as handle:
                handle.seek(self.offset)
                for line in handle.readlines():
                    if not line.endswith("\n"):
                        break
                    self.offset += len(line.encode("utf-8"))
                    fields = line.rstrip("\n").split("\t")
                    if len(fields) != 4:
                        continue
                    kind, _pid, members, _status = fields
                    self.last_progress = self.clock()
                    if kind not in ("agent-down", "agent-up"):
                        continue
                    agent, _sep, root = members.partition(":")
                    if kind == "agent-down":
                        self.down.setdefault(root, (agent, self.clock()))
                    else:
                        self.down.pop(root, None)
                        self.alerted.discard(root)
        except OSError:
            pass

    def check(self):
        """Read new facts, spool the alerts that are due; returns their paths."""
        self.read_events()
        now = self.clock()
        stalled = now - self.last_progress > self.stall_limit
        sent = []
        for root, (agent, since) in sorted(self.down.items()):
            if root in self.alerted:
                continue
            kind = "down" if now - since > self.down_limit else "stall" if stalled else None
            if kind is None:
                continue
            self.alerted.add(root)
            sent.append(self.alert(kind, agent, root, now - since, now - self.last_progress))
        return sent

    def alert(self, kind, agent, root, down_s, idle_s):
        lang = self.ledger.get("lang", "ko")
        draft = OUTAGE_ALERT_DRAFT[kind]
        text = draft.get(lang, draft["en"]).format(
            agent=agent, minutes=int(down_s // 60), stall_minutes=int(idle_s // 60))
        record = {"kind": kind, "agent": agent, "root": root, "ledger": self.ledger["id"],
                  "down_s": int(down_s), "idle_s": int(idle_s), "at": int(time.time()),
                  "draft": not OUTAGE_ALERT_COPY_APPROVED, "text": text, "delivered": False}
        if OUTAGE_ALERT_COPY_APPROVED:
            try:
                record["delivered"] = bool(push_notice(self.ledger, text).get("delivered"))
            except Exception as error:  # noqa: BLE001 -- an alert failure never touches the update
                record["delivery_error"] = str(error)
        path = os.path.join(self.spool, "%d-%s.json" % (len(self.alerted), re.sub(r"[^A-Za-z0-9_.-]", "_", agent)))
        atomic_write(path, record)
        return path

    def watch(self):
        while not self.stop.wait(1 if self.down_limit >= 10 else 0.05):
            try:
                self.check()
            except Exception as error:  # noqa: BLE001
                print("outage watch failed: %s" % error, flush=True)

    def finish(self):
        self.stop.set()
        self.thread.join()


def start_progress(ledger):
    """Claim the sole start send before preflight; stop-send reuses its verdict.

    Persist the attempt first: a lost/uncertain send can never produce a
    second start bubble. Missing delivery still blocks the stop boundary.
    """
    if "progress" in ledger:
        return bool(ledger["progress"].get("delivered"))
    text = progress_text(ledger, progress_stages(ledger))
    precheck = word(ledger.get("lang", "ko"), "update.precheck_start")
    ledger["progress"] = {"attempted": True, "text": text, "precheck_text": precheck}
    atomic_write(ledger_path(ledger["id"]), ledger)
    # This is deliberately the first owner-facing event of apply.  The apply
    # subprocess is not invoked until cmd_worker returns from this function.
    # UpdateProgress reuses the receipt and edits it into `text` in place.
    receipt = push_notice(ledger, precheck)
    # rc 2 is a confirmed transport rejection/not-connected result and rc 6
    # means delivery is uncertain. Both may be transient, but neither is
    # permission to stop an agent. Retry exactly once before failing closed.
    rc = receipt.get("returncode")
    if rc is not None:
        reason_code = "update.notice-not-connected" if rc == 2 else (
            "update.notice-uncertain" if rc == 6 else "update.notice-send-failed")
        receipt["reason_code"] = reason_code
        print("start notice delivery failed: reason_code=%s push_rc=%s stderr_tail=%r" %
              (reason_code, rc, receipt.get("stderr_tail", "")), flush=True)
    ledger["progress"].update(delivered=bool(receipt.get("delivered")),
        receipt=receipt if receipt.get("delivered") or rc is not None else None,
        reason_code=receipt.get("reason_code"))
    if receipt.get("delivered"):
        ledger.setdefault("start_notices", {})[os.path.realpath(ledger["instance_root"])] = "sent"
    atomic_write(ledger_path(ledger["id"]), ledger)
    return bool(receipt.get("delivered"))


def start_member_progress(ledger, roots):
    """Persist one card attempt per member; inherit existing cards on retry.

    The supervisor is the sole editor. A stop boundary only reads/reuses these
    receipts, so bridge teardown and repeated units cannot post another card.
    """
    progress = ledger.setdefault("progress", {})
    members = progress.setdefault("members", {})
    inherited, old = {}, ledger
    for _ in range(8):
        if not old.get("retry_of"):
            break
        try:
            old = load_ledger(old["retry_of"])
        except (ValueError, OSError):
            break
        for root, entry in ((old.get("progress") or {}).get("members") or {}).items():
            inherited.setdefault(root, entry)
    # Members receive the normal progress card immediately; the requester's
    # receipt alone owns the short pre-check line that precedes the apply.
    text = progress.get("text") or progress_text(ledger, progress_stages(ledger))
    ok = True
    for root in dict.fromkeys(os.path.realpath(r) for r in roots):
        if same_root(root, ledger["instance_root"]):
            continue
        if root not in members:
            if root in inherited:
                members[root] = dict(inherited[root])
            else:
                members[root] = {"attempted": True}
                atomic_write(ledger_path(ledger["id"]), ledger)
                dest = dict(ledger, instance_root=root, chat="")
                receipt = push_notice(dest, text)
                members[root].update(delivered=bool(receipt.get("delivered")), receipt=receipt)
        if members[root].get("delivered"):
            ledger.setdefault("start_notices", {})[root] = "sent"
        else:
            ok = False
        atomic_write(ledger_path(ledger["id"]), ledger)
    return ok


def stop_text(members, lang):
    """DGN-1506 item 4: the stop notice for the agents the crew transaction
    is about to stop ("name:root" tokens, display names resolved here)."""
    names = []
    for token in members:
        name, _, root = token.partition(":")
        label = member_label(root, name, lang) if root else name
        if label not in names:
            names.append(label)
    agents = join_and(names, lang)
    if lang == "ko":
        agents = with_josa(agents, "이", "가")
    return copy(lang)["stop_notice"].format(agents=agents)


def cmd_stop_send(args):
    """DGN-1506 item 4: send the stop notice through the result notice's own
    sender and report CONFIRMED delivery (push.sh --strict) as rc 0 --
    update_apply stops nothing unless this returns 0.  --ledger-result is
    the /update run's result file: the notice then goes to the ledger's
    instance and chat, exactly where the worker delivers the outcome.
    --instance-root is the operator / rehearsal path (result-send's sender).
    Prints the body so the run log carries what the owner was told."""
    if os.environ.get("DOGANY_LAUNCHD_CAPTURE"):
        return 0
    if args.ledger_result:
        result = os.path.abspath(args.ledger_result)
        if not result.endswith(".json.result"):
            raise ValueError("not a ledger result path")
        ledger_id = os.path.basename(result)[:-len(".json.result")]
        if result != os.path.abspath(ledger_path(ledger_id) + ".result"):
            raise ValueError("result outside the update ledger")
        ledger = load_ledger(ledger_id)
        lang = ledger.get("lang", "ko")
    else:
        ledger = {"instance_root": args.instance_root, "chat": ""}
        lang = os.environ.get("DOGANY_UPDATE_LANG") or notice_lang(args.instance_root, args.repo)
    # The apply process invokes this at each real stop boundary. Persist
    # successful sends so later units and retries do not duplicate them.
    sent = dict(ledger.get("start_notices") or {})
    requester = os.path.realpath(ledger["instance_root"])
    def save():
        if args.ledger_result:
            ledger["start_notices"] = sent
            atomic_write(ledger_path(ledger["id"]), ledger)
    if ledger.get("progress_enabled"):
        if not start_progress(ledger):
            return 1
        sent[requester] = "sent"
    elif sent.get(requester) != "sent" and not args.requester_notified:
        text = stop_text(args.member, lang)
        print(text)
        receipt = push_notice(ledger, text)
        if not receipt.get("delivered"):
            print("stop notice delivery failed: push_rc=%s stderr_tail=%r" %
                  (receipt.get("returncode"), receipt.get("stderr_tail", "")), flush=True)
            return 1
        sent[requester] = "sent"
        save()
    # FDGN-584: member starts are the same card, never separate lines.
    if ledger.get("crew_update") or (ledger.get("items") and crew_run(selected(ledger))):
        return 0 if start_member_progress(ledger, [token.partition(":")[2] for token in args.member]) else 1
    for token in args.member:
        name, _, root = token.partition(":")
        key = os.path.realpath(root)
        if key == requester or sent.get(key) == "sent":
            continue
        text = word(notice_lang(root, args.repo), "update.member_start")
        print(text)
        if not push_agent_notice(root, text):
            return 1
        sent[key] = "sent"
        save()
    return 0


def cmd_validate_apply(args):
    """Read-only handoff check; emit the exact checked bytes for the walker.

    This enforces the supervisor protocol, not a security boundary against
    an actor able to rewrite the ledger or the framework on disk.
    """
    try:
        plan = os.path.abspath(args.plan)
        if not plan.endswith(".json.plan"):
            raise ValueError("not a ledger plan")
        ledger_id = os.path.basename(plan)[:-10]
        path = ledger_path(ledger_id)
        if plan != os.path.abspath(path + ".plan"):
            raise ValueError("plan outside the update ledger")
        ledger = load_ledger(ledger_id)
        if (ledger.get("id") != ledger_id or ledger.get("state") != "running"
                or ledger.get("stage") != "applying"
                or not ledger.get("tapped_at")
                or not os.path.isfile(path + ".start")
                or ledger.get("selected_plan") != args.plan
                or os.path.realpath(ledger.get("repo", "")) != os.path.realpath(args.repo)
                or os.path.abspath(args.result) != os.path.abspath(path + ".result")
                or (ledger.get("supervisor") or {}).get("pid") != args.supervisor):
            raise ValueError("no active owner-picked supervisor handoff")
        keys = selection_keys(ledger)
        items = selected(ledger)
        if not keys or keys != [item["key"] for item in items] or len(set(keys)) != len(keys):
            raise ValueError("invalid ledger selection")
        expected = "PLAN-EMIT-V1\n" + "".join(
            line + "\n" for item in items for line in item["lines"])
        with open(plan, "rb") as handle:
            content = handle.read()
        if content != expected.encode("utf-8"):
            raise ValueError("plan differs from owner-picked ledger selection")
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as error:
        raise ValueError("/update handoff refused: %s" % error) from error
    sys.stdout.buffer.write(content)


def cmd_worker(args):
    ledger = load_ledger(args.ledger)
    ledger["supervisor"] = {"pid": os.getpid(), "started_at": int(time.time())}
    ledger["stage"] = "applying"
    atomic_write(ledger_path(args.ledger), ledger)
    items = selected(ledger)
    # Start delivery belongs to the apply process's real stop boundary.
    since = int(time.time())
    result_file = ledger_path(args.ledger) + ".result"
    apply = os.path.join(ledger["repo"], "scripts", "pack", "update_apply.sh")
    # DGN-1746: the record's display names in this ledger's language.
    env = dict(os.environ, DOGANY_UPDATE_LANG=ledger.get("lang", "ko"))
    progress = UpdateProgress(ledger) if (ledger.get("progress_enabled") or ledger.get("crew_update") or crew_run(items)) and not os.environ.get("DOGANY_LAUNCHD_CAPTURE") else None
    if progress:
        if ledger.get("progress_enabled"):
            start_progress(ledger)
        story_items = items + (ledger.get("retry_context") or {}).get("items", [])
        start_member_progress(ledger, [root for item in story_items for root in item["member_roots"]])
        # Replace the requester's short pre-check line with the normal 0/N
        # card. Members already received that card, so do not edit them twice.
        progress.edit(progress.states, leader_only=True)
        progress.last_edit = time.monotonic()
        env["DOGANY_UPDATE_PROGRESS_FILE"] = progress.events
        progress.thread.start()
    # DGN-2296: the outage watch reads the same private event stream.
    watch = None
    if not os.environ.get("DOGANY_LAUNCHD_CAPTURE"):
        env.setdefault("DOGANY_UPDATE_PROGRESS_FILE", ledger_path(args.ledger) + ".progress")
        watch = OutageWatch(ledger, env["DOGANY_UPDATE_PROGRESS_FILE"])
        if watch.enabled:
            watch.thread.start()
    proc = subprocess.run(["bash", apply, "--plan-file", ledger["selected_plan"], "--result-file", result_file],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    output = proc.stdout.decode("utf-8", "replace")
    if watch is not None and watch.enabled:
        watch.finish()
    try:
        with open(result_file, "r") as handle:
            record = json.load(handle)
    except (IOError, OSError, ValueError):
        record = None
    ledger = load_ledger(args.ledger)
    progress_reason = (ledger.get("progress") or {}).get("reason_code")
    if progress_reason and record:
        for unit in record.get("units") or []:
            if unit.get("result") != "ok" and not unit.get("reason_code"):
                unit["reason_code"] = progress_reason
    ledger["state"] = "verifying"
    ledger["stage"] = "verifying"
    ledger["result"] = {"rc": proc.returncode, "output": output, "record": record,
                        "finished_at": int(time.time())}
    atomic_write(ledger_path(args.ledger), ledger)
    cache = {}
    results = [judge(ledger, item, record, since, only=len(items) == 1, alive_cache=cache) for item in items]
    # Item 24: an agent that did not come back after an applied unit gets ONE
    # automatic restart before the owner hears anything.
    retry = {}
    for member in stopped_members(results, ledger.get("lang", "ko")):
        retry[member["root"]] = restart_member(ledger["repo"], member["root"])
    if retry:
        results = [judge(ledger, item, record, since, only=len(items) == 1, alive_cache=cache, revived=retry)
                   for item in items]
    ledger = load_ledger(args.ledger)
    if retry:
        ledger["result"]["restart_retry"] = retry
    ledger["result"]["items"] = results
    story_items, story_results, story_record = report_story(ledger, items, results, record)
    ledger["state"] = overall(story_results)
    if ledger.get("retry_context"):
        ledger["result"]["story"] = dict(items=story_items, results=story_results, record=story_record)
    ledger["stage"] = "delivering"
    atomic_write(ledger_path(args.ledger), ledger)
    if progress:
        progress.finish(story_results)
        ledger["progress"]["states"] = progress.states
        atomic_write(ledger_path(args.ledger), ledger)
    delivery = deliver(ledger, items, results, record)
    ledger = load_ledger(args.ledger)
    if delivery is not None:
        ledger["delivery"] = delivery
    ledger["stage"] = "done"
    atomic_write(ledger_path(args.ledger), ledger)
    # DGN-1794 r2: this run is delivered -> carry through what a busy tap
    # queued behind it.  Best effort: a failure leaves it queued for the
    # next boot sweep and never changes this run's outcome.
    try:
        print(json.dumps({"follow": follow_through()}))
    except Exception as error:  # noqa: BLE001 -- the run itself is already delivered
        print("follow-through failed: %s" % error)
    return 0 if ledger["state"] == "complete" else 1


def cmd_restart(args):
    """The "<agent> 재시작" tap (item 24): owner-initiated, the requester only.
    Restart that one agent the same way the automatic retry does and report
    success only after the liveness verdict.  An agent that is already back
    is judged by the same verdict and not restarted again."""
    ledger = load_ledger(args.ledger)
    check_requester(ledger, args.user)
    lang = ledger.get("lang", "ko")
    c = copy(lang)
    stopped = (ledger.get("result") or {}).get("stopped") or []
    if args.index < 0 or args.index >= len(stopped):
        print(json.dumps({"action": "stale", "text": c["stale"]}, ensure_ascii=False))
        return 0
    member = stopped[args.index]
    label = member["label"]
    josa_reul = with_josa(label, "을", "를") if lang == "ko" else label
    lock = ledger_path(ledger["id"]) + ".restart"
    if not claim(lock):
        print(json.dumps({"action": "running", "text": c["restart_running"].format(agent=josa_reul)},
                         ensure_ascii=False))
        return 0
    try:
        back = alive(ledger["repo"], member["root"], 0, timeout=1) or restart_member(ledger["repo"], member["root"])
    finally:
        os.unlink(lock)
    ledger = load_ledger(ledger["id"])
    items = selected(ledger)
    by_key = dict((item["key"], item) for item in items)
    results = (ledger.get("result") or {}).get("items") or []
    for result in results:
        item = by_key.get(result["key"])
        for m in result["members"]:
            if back and m["root"] == member["root"] and m["status"] in STOPPED and item and member_unit(result, m) == "ok":
                m["status"] = "verified" if on_target(item, m["root"]) else "version_mismatch"
        if result["status"] not in ("complete", "partial"):
            # Promote only: a revived member can finish an item (complete, or
            # partial when a sibling member's own unit never ran).
            promoted = item_state(result["unit"], "partial", result["members"])
            if promoted in ("complete", "partial"):
                result["status"] = promoted
    story_items, story_results, story_record = report_story(
        ledger, items, results, (ledger.get("result") or {}).get("record"))
    if results:
        ledger["state"] = overall(story_results)
        if ledger.get("retry_context"):
            ledger["result"]["story"] = dict(items=story_items, results=story_results, record=story_record)
    ledger.setdefault("restarts", []).append({"root": member["root"], "by": str(args.user),
                                              "alive": bool(back), "at": int(time.time())})
    atomic_write(ledger_path(ledger["id"]), ledger)
    if not back:
        out = {"action": "failed", "alive": False,
               "text": html.escape(failure_text({"reason_code": "update.restart-failed",
                       "reason_payload": {"agents": _agents([label], lang)}}, lang)),
               "buttons": restart_buttons(ledger, lang, only_roots=[member["root"]])}
    elif results and ledger["state"] == "complete":
        out = {"action": "done", "alive": True, "text": completion_text(story_results, story_items, lang), "buttons": []}
    else:
        left = stopped_members(story_results, lang)
        text = c["restart_ok"].format(agent=_agents([label], lang))
        if left and partial_only(story_results):
            text += "\n\n" + partial_text(story_results, story_items, lang)
        out = {"action": "back", "alive": True, "text": text,
               "buttons": restart_buttons(ledger, lang, only_roots=[m["root"] for m in left])}
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_sweep(args):
    """Bridge-boot backstop: a ledger whose supervisor died mid-run is closed
    as recovery_required and reported once (the claim file is the mutex)."""
    pending = 0
    closed = 0
    if os.path.isdir(ledger_dir()):
        for name in sorted(os.listdir(ledger_dir())):
            if not name.endswith(".json"):
                continue
            try:
                ledger = load_ledger(name[:-5])
            except (ValueError, OSError):
                continue
            lost = ledger.get("state") in ACTIVE_STATES and not supervisor_alive(ledger)
            undelivered = (ledger.get("state") in FINAL_STATES and (not ledger.get("delivery") or ledger["delivery"].get("method") == "none")
                           and not (ledger.get("supervisor") and pid_alive(ledger["supervisor"].get("pid"))))
            if ledger.get("state") in ACTIVE_STATES and not lost:
                pending += 1
                continue
            if not (lost or undelivered):
                continue
            items = selected(ledger)
            if not items:
                continue
            record = (ledger.get("result") or {}).get("record")
            results = (ledger.get("result") or {}).get("items")
            if not results:
                results = [judge(ledger, item, None, 0) for item in items]
                ledger["result"] = dict(ledger.get("result") or {}, items=results)
                ledger["state"] = "recovery_required"
            ledger["stage"] = "supervisor_lost"
            atomic_write(ledger_path(ledger["id"]), ledger)
            delivery = deliver(ledger, items, results, record)
            if delivery is not None:
                ledger = load_ledger(ledger["id"])
                ledger["delivery"] = dict(delivery, by="sweep")
                atomic_write(ledger_path(ledger["id"]), ledger)
                closed += 1
    # DGN-1794 r2: a queued follow-through whose trigger was lost (the
    # worker died, the bridges restarted mid-wait) is picked up here.
    try:
        follow = follow_through()
    except Exception as error:  # noqa: BLE001 -- the sweep's own close is done
        follow = [{"error": str(error)}]
    print(json.dumps({"pending": pending, "closed": closed, "follow": follow}))
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    def origin(p):
        p.add_argument("--plan", required=True); p.add_argument("--repo", required=True)
        p.add_argument("--instance-root", required=True); p.add_argument("--requester", required=True)
        p.add_argument("--chat", required=True)
    p = sub.add_parser("verification")
    p.add_argument("--repo", required=True); p.add_argument("--instance-root", required=True)
    p.add_argument("--version", required=True); p.set_defaults(fn=cmd_verification)
    p = sub.add_parser("list"); origin(p); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("offer"); origin(p); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_offer)
    p = sub.add_parser("offered"); p.add_argument("--instance-root", required=True); p.add_argument("--signature", required=True)
    p.add_argument("--ledger", required=True); p.add_argument("--message-id", type=int, default=None); p.set_defaults(fn=cmd_offered)
    p = sub.add_parser("pick"); origin(p); p.add_argument("--ledger", required=True); p.add_argument("--revision", type=int, required=True)
    p.add_argument("--pick", required=True); p.add_argument("--user", default=None); p.add_argument("--tapped-at", type=int, default=None)
    p.set_defaults(fn=cmd_pick)
    p = sub.add_parser("offer-view"); origin(p); p.add_argument("--ledger", required=True)
    p.add_argument("--revision", type=int, required=True); p.add_argument("--pick", required=True)
    p.add_argument("--user", default=None); p.add_argument("--back", action="store_true")
    p.set_defaults(fn=cmd_pick, preview=True)
    p = sub.add_parser("sync"); origin(p); p.add_argument("--ledger", required=True); p.add_argument("--revision", type=int, required=True)
    p.add_argument("--pick", required=True); p.add_argument("--user", default=None); p.add_argument("--tapped-at", type=int, default=None)
    p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("notice"); origin(p); p.add_argument("--version", required=True); p.add_argument("--tapped-at", type=int, default=None); p.set_defaults(fn=cmd_notice)
    p = sub.add_parser("detail"); p.add_argument("--ledger", required=True); p.add_argument("--revision", type=int, required=True); p.add_argument("--key", required=True); p.set_defaults(fn=cmd_detail)
    p = sub.add_parser("key"); p.add_argument("--ledger", required=True); p.add_argument("--revision", type=int, required=True); p.add_argument("--index", type=int, required=True); p.set_defaults(fn=cmd_key)
    for verb, fn in (("consent", cmd_consent), ("apply", cmd_apply)):
        p = sub.add_parser(verb); p.add_argument("--ledger", required=True); p.add_argument("--revision", type=int, required=True)
        p.add_argument("--key", required=True); p.add_argument("--user", default=None); p.add_argument("--tapped-at", type=int, default=None)
        p.set_defaults(fn=fn)
    p = sub.add_parser("result-send"); p.add_argument("--repo", required=True); p.add_argument("--instance-root", required=True); p.set_defaults(fn=cmd_result_send)
    p = sub.add_parser("stop-send"); p.add_argument("--repo", required=True)
    dest = p.add_mutually_exclusive_group(required=True)
    dest.add_argument("--ledger-result"); dest.add_argument("--instance-root")
    p.add_argument("--requester-notified", action="store_true")
    p.add_argument("--member", action="append", required=True); p.set_defaults(fn=cmd_stop_send)
    p = sub.add_parser("validate-apply")
    p.add_argument("--plan", required=True); p.add_argument("--result", required=True)
    p.add_argument("--repo", required=True); p.add_argument("--supervisor", type=int, required=True)
    p.set_defaults(fn=cmd_validate_apply)
    p = sub.add_parser("worker"); p.add_argument("--ledger", required=True); p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("restart"); p.add_argument("--ledger", required=True); p.add_argument("--index", type=int, required=True)
    p.add_argument("--user", default=None); p.set_defaults(fn=cmd_restart)
    p = sub.add_parser("status"); p.add_argument("--ledger", required=True); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("sweep"); p.set_defaults(fn=cmd_sweep)
    p = sub.add_parser("follow"); p.add_argument("--ledger", required=True); p.add_argument("--choice", choices=("y", "n"), required=True)
    p.add_argument("--user", default=None); p.add_argument("--chat", required=True); p.set_defaults(fn=cmd_follow)
    p = sub.add_parser("try-offer"); p.add_argument("--repo", required=True); p.add_argument("--commit", required=True)
    p.add_argument("--previous", default=""); p.add_argument("--new", required=True); p.add_argument("--instance-root", default="")
    p.add_argument("--lang", default="ko"); p.add_argument("--profile", default=""); p.set_defaults(fn=cmd_try_offer)
    p = sub.add_parser("versions"); p.add_argument("--repo", required=True); p.add_argument("--instance-root", required=True)
    p.set_defaults(fn=cmd_versions)
    args = parser.parse_args()
    if not getattr(args, "fn", None): parser.error("command required")
    try:
        return args.fn(args) or 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
