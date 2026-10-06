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
notice in ITS OWN chat, once, rendered by routines/self-update.sh
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
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
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
        "confirm_header": "🛠️ 아래 업데이트가 적용돼요.",
        "confirm_header_n": "🛠️ 아래 {count} 업데이트가 적용돼요.",
        "needs": "{dep} 함께 필요해요.",
        "crew_kit": "같이 쓰는 킷이라 {n} 에이전트가 함께 올라가요.",
        "summary": "업데이트 요약",
        "detail": "업데이트 상세",
        "apply_all": "전체 업데이트",
        "apply_one": "{name} {target} 업데이트",
        "offer_apply": "적용",
        "offer_back": "< 뒤로가기",
        "offer_caution": "⚠️ 적용하면 {agents} 잠깐 멈췄다가 다시 켜져요. 그동안 답하던 대화는 끊길 수 있어요.",
        # DGN-1781 (owner 2026-09-28 16:43): the start line alone, sent to
        # every agent the apply touches.
        "start": "🛠️ 업데이트를 시작했어요. 잠깐 재시작한 뒤, 제대로 적용됐는지 확인해서 알려드릴게요.",
        "done": "🛠️ 업데이트를 완료했어요.",
        "done_item": "{name} {target}: 새 버전으로 적용됐어요.",
        "done_crew": "{name} {target}: {all} 모두 새 버전으로 적용됐어요.",
        # DGN-1794 round 2 (dec-210).  owner-confirmed 2026-09-30 (dec-211): the busy
        # ask, its two buttons, the two edits after a tap, and the pickup's
        # own lines (plan changed / nothing left / lookup failed / expired).
        "busy_ask": "지금 {running}가 진행 중이에요. 끝나면 이어서 적용할까요?",
        "follow_yes": "이어서 적용",
        "follow_no": "그만두기",
        "follow_queued": "알겠어요. 끝나면 이어서 적용할게요.",
        "follow_declined": "적용하지 않았어요.",
        "follow_changed": "내용이 바뀌어서 이어서 적용하지 않았어요. 아래에서 다시 골라주세요.",
        "follow_none": "이미 적용돼서 할 게 없어요.",
        "follow_failed": "확인이 안 돼서 이어서 적용하지 않았어요. /update 로 다시 열어주세요.",
        "follow_expired": "하루가 지나서 이어서 적용은 취소했어요. /update 로 다시 열어주세요.",
        "busy_item": "{name} 업데이트({current} → {target})",
        # DGN-1749: agents on the same installed version share one entry.
        "busy_crew": "{name} 업데이트({rows})",
        # /update detail card only; the notice shows the kit on the row (item 22).
        "bundle": "{name} {target}로 업데이트하려면 {dep}도 {dep_target}로 함께 업데이트해야 해요.",
        "stale": "업데이트할 내용이 달라졌어요. 새 목록에서 다시 골라주세요.",
        "truncated": "…(이하 생략 — 더 궁금하시면 물어봐 주세요)",
        # Item 24 (owner-confirmed): applied, but an agent stayed stopped after
        # the automatic restart retry.  {agents} carries its josa (이/가).
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
    },
    "en": {
        "header": "🛠️ A new update is available.",
        "header_n": "🛠️ {n} new updates are available.",
        "confirm_header": "🛠️ The update below will be applied.",
        "confirm_header_n": "🛠️ The {count} updates below will be applied.",
        "needs": "Needs {dep} as well.",
        "crew_kit": "A shared kit, so {n} agents update together.",
        "summary": "At a glance",
        "detail": "What changed",
        "apply_all": "Update all",
        "apply_one": "Update {name} {target}",
        "offer_apply": "Apply",
        "offer_back": "< Back",
        "offer_caution": "⚠️ Applying will briefly stop {agents} before restarting. Conversations in progress may be interrupted.",
        "start": "🛠️ Update started. I'll restart briefly, then check that it applied correctly and let you know.",
        "done": "🛠️ Update complete.",
        "done_item": "{name} {target}: now on the new version.",
        "done_crew": "{name} {target}: {all} are both on the new version.",
        "busy_ask": "{running} is in progress right now. Apply this one right after it finishes?",
        "follow_yes": "Apply after it",
        "follow_no": "Don't apply",
        "follow_queued": "Got it. I'll apply it once this finishes.",
        "follow_declined": "Not applied.",
        "follow_changed": "The contents changed, so I didn't apply it. Please pick again below.",
        "follow_none": "Already applied, nothing to do.",
        "follow_failed": "I couldn't verify, so I didn't apply it. Reopen with /update.",
        "follow_expired": "A day has passed, so the queued apply was cancelled. Reopen with /update.",
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
    "update.layer_framework": "[Framework]",
    "update.layer_kit": "[Kit]",
    "update.layer_pack": "[Agent]",
    "update.item_head": "{product} {target}",
    "update.offer_target": "└ Applies to: {agents} (now {current})",
    "update.result_target": "└ {agents}: {result}",
    "update.result_done": "✅ Update complete.",
    "update.result_partial": "⚠️ The update was only partly applied.",
    "update.result_blocked": "⚠️ The update could not be applied. Nothing changed.",
    "update.result_ok": "Updated to the new version",
    "update.result_preflight": "Unchanged because a preflight check stopped the update",
    "update.result_dependency": "Unchanged because {dep} is needed first",
    "update.result_incomplete": "The update started but did not finish",
    "update.result_untried": "Unchanged because an earlier update did not finish",
    "update.result_unknown": "The update result is not known yet",
    "update.result_down": "{agent} did not come back online",
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
    default in I18N_DEFAULTS when the table has no value."""
    value = ""
    lookup = _i18n_lookup()
    if lookup is not None:
        template = os.path.dirname(os.path.dirname(os.path.dirname(lookup.__file__)))
        try:
            value = lookup.lookup(template, lang or "en", key)
        except Exception:
            value = ""
    return value if isinstance(value, str) and value else I18N_DEFAULTS[key]


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


def vfmt(version):
    """Full semver with v, always (rehearsal item 16)."""
    return "v" + norm(version) if norm(version) else ""


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
    "Dogany" / 도가니 stays on the framework only, so a kit or pack no longer
    reads as the framework.  kind is "framework" | "kit" | "pack" |
    "" (unknown: no tag -- a guessed layer is worse than none).  DGN-1749
    review (6): no auto "팩" / "Pack" suffix -- the catalog name itself.
    tagged=False (dec-222): the bare name, for text that becomes the owner's
    own chat turn (the crew stall alarm's button)."""
    if kind == "framework":
        name = PRODUCT
    else:
        name = (raw or "").strip()
        for prefix in (PRODUCT, "도가니"):
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
        unit["members"] = [{"name": n, "root": r, "label": label} for (n, r), label in zip(meta.get("members") or [], unit["agents"])]
        if meta.get("dependency"):
            unit["dependency_product"] = unit_display("kit", meta["dependency"], "HEAD", [], lang)["product"]
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
    item = {"key": "%s:%s:%s" % (item_kind, pid, target), "kind": item_kind, "id": pid,
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


def plan_items(plan, repo, lang):
    """One item per independently appliable unit: FW line, PACK line, or a
    whole ATOMIC group (a required bundle is never offered piecemeal)."""
    with open(plan, "r") as handle:
        lines = [line.rstrip("\n") for line in handle]
    if not lines or lines[0] != "PLAN-EMIT-V1":
        raise ValueError("untrusted plan format")
    result = []
    index = 1
    while index < len(lines):
        line = lines[index]
        fields = line.split("\t")
        if fields[0] == "FW" and len(fields) == 5:
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
            item["key"] = "bundle:" + item["key"]
            result.append(item)
        index += 1
    return result


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
            agent=agent_label(roots_of.get(name, ""), name, lang), version=vfmt(version))
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
            "key": "framework:%s:%s" % ("+".join(g["id"] for g in group), group[0]["target"]),
            "members": [g["members"][0] for g in group],
            "member_roots": [g["member_roots"][0] for g in group],
            "labels": [g["labels"][0] for g in group],
            "lines": [line for g in group for line in g["lines"]],
            "currents": [[g["members"][0], norm(g["current"])] for g in group],
            "current": low, "current_high": high if norm(high) != norm(low) else "",
            "who": [g["labels"][0] for g in group]})
        out.append(merged)
    return out


def link_requirements(items, repo):
    """A pack whose candidate needs a newer kit than the installed one names
    that kit item: applying the pack alone would fail its requires_kit gate
    (DGN-1682), so the kit rides with it."""
    kits = dict((i["id"], i) for i in items if i["kind"] == "kit" and not i["key"].startswith("bundle:"))
    for item in items:
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


def versions_text(repo, roots, lang, layers=None):
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
    copy_keys = ("update.versions_header", "update.version_row", "update.version_row_agents")
    words = dict((key, vc._i18n_get(lang, key)) for key in copy_keys)
    if not layers or not all(words.values()):
        return ""
    many = len([r for r in roots if os.path.isfile(os.path.join(r, ".instance.conf"))]) > 1
    rows = []
    for layer in layers:
        for group in layer["versions"]:
            version = vfmt(group["version"])
            if not version:
                continue
            if many:
                rows.append(words["update.version_row_agents"].format(
                    product=layer["name"], version=version, agents=AGENT_SEP.join(group["agents"])))
            else:
                rows.append(words["update.version_row"].format(product=layer["name"], version=version))
    if not rows:
        return ""
    return "%s\n<blockquote>%s</blockquote>" % (
        html.escape(words["update.versions_header"]), html.escape("\n".join(rows)))


def new_ledger(args, source, lang, scope=None, persist=True):
    """scope: "offer" = the notice's leader-gated crew (offer_scope), "crew"
    = /update's member set (crew_roots), None = every layer the planner
    found (the round-2 notice button only resolves this instance's row)."""
    items = merge_framework(plan_items(args.plan, args.repo, lang), args.repo)
    if scope:
        # Every layer touching one of the crew's members -- framework, kit,
        # packs (DGN-1749 for the notice, DGN-1784 for /update).  Other
        # crews announce their own.
        if scope == "offer":
            _, roots = offer_scope(args.instance_root, args.repo)
        else:
            roots = crew_roots(args.instance_root, args.repo)
        items = [item for item in items if any(touches(item, root) for root in roots)]
    link_requirements(items, args.repo)
    for item in items:
        item["caption"] = version_line(item)
    ledger_id = str(uuid.uuid4())
    ledger = {"schema": 3, "revision": 1, "id": ledger_id, "created_at": int(time.time()),
              "source": source, "state": "selecting", "requester": str(args.requester),
              "chat": str(args.chat), "repo": os.path.abspath(args.repo),
              "instance_root": os.path.abspath(args.instance_root), "lang": lang,
              "plan": os.path.abspath(args.plan), "items": items, "selection": None,
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
    return vfmt(item["current"])


def version_line(item):
    """One line, the target named once (DGN-1749 -- no "Dogany → vB" head
    plus a repeated line per agent, never a "vA~A2" range):
        Dogany vA → vB                        one agent
        Dogany vA → vB (<agent>)              another agent's row
        Dogany vA → vB (<a1> · <a2>)          a crew row, one installed version
        Dogany → vB (<a1> vA1, <a2> vA2)      a crew row, several versions"""
    target = vfmt(item["target"])
    rows = crew_rows(item)
    if len(rows) > 1:
        return "%s → %s (%s)" % (item["name"], target, ", ".join(
            "%s %s" % (AGENT_SEP.join(labels), vfmt(current)) for labels, current in rows))
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


def start_text(lang, as_html=True):
    """DGN-1781 (owner 2026-09-28 16:43): the start line alone -- the items
    are already on the screen the owner tapped, so no version quote and no
    crew line.  Escaped when the caller renders HTML (DGN-1776/1778)."""
    line = copy(lang)["start"]
    return html.escape(line, quote=False) if as_html else line


def busy_item(item, lang):
    rows = crew_rows(item)
    if rows:
        return copy(lang)["busy_crew"].format(name=item["name"], rows=", ".join(
            "%s %s → %s" % (AGENT_SEP.join(labels), vfmt(current), vfmt(item["target"])) for labels, current in rows))
    return copy(lang)["busy_item"].format(name=item["name"], current=from_version(item),
                                          target=vfmt(item["target"]))


def busy_text(other):
    lang = other.get("lang", "ko")
    running = [busy_item(item, lang) for item in selected(other)]
    return copy(lang)["busy_ask"].format(running=", ".join(running))


def bundle_lines(item, deps, lang):
    out = []
    for dep in deps:
        out.append(copy(lang)["bundle"].format(name=item["name"], target=vfmt(item["target"]),
                                               dep=dep["name"], dep_target=vfmt(dep["target"])))
    for fw in item["includes"]:
        out.append(copy(lang)["bundle"].format(name=item["name"], target=vfmt(item["target"]),
                                               dep=product_name("", "framework", lang), dep_target=vfmt(fw["target"])))
    return out


def done_line(item, lang):
    if len(item["labels"]) > 1 and (item["needs_consent"] or item["kind"] == "framework"):
        return copy(lang)["done_crew"].format(name=item["name"], target=vfmt(item["target"]),
                                              all=join_and(item["labels"], lang))
    return copy(lang)["done_item"].format(name=item["name"], target=vfmt(item["target"]))


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
    needs = ["%s %s" % (dep["name"], vfmt(dep["target"])) for dep in deps]
    needs += ["%s %s" % (product_name("", "framework", lang), vfmt(fw["target"])) for fw in item.get("includes") or []]
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
    head = word(lang, "update.item_head").format(product=item["name"], target=vfmt(item["target"]))
    lines = [word(lang, "update.offer_target").format(agents=AGENT_SEP.join(labels), current=vfmt(current))
             for labels, current in version_groups(item)]
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


def render_offer(items, lang, header=None):
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
            blocks.append(block)
        return header + "\n" + "\n\n".join(blocks)

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


def offer_state_path(instance_root):
    return os.path.join(instance_root, ".telegram_bot", "state", "update-offer.json")


def read_offer_state(instance_root):
    try:
        with open(offer_state_path(instance_root), "r") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


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
    elif ledger["split_rows"]:
        body, buttons = split_view(ledger)
        out.update(html=body, buttons=buttons)
    else:
        # DGN-1790: nothing to apply -> say so and show what is installed.
        # No "text" (copy or records unreadable) = the bridge's own line.
        block = versions_text(args.repo, crew_roots(args.instance_root, args.repo), lang)
        none = version_check(args.repo)._i18n_get(lang, "update.none")
        if block and none:
            out.update(text="%s\n\n%s" % (html.escape(none), block), parse_mode="HTML")
    print(json.dumps(out, ensure_ascii=False))


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
    signature = offer_signature(items)
    if not items:
        action = "none"
    elif read_offer_state(args.instance_root).get("asked") == signature and not args.force:
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
    prefix = "%s:%s" % (ledger["id"], ledger["revision"])
    buttons = []
    if len(shown) > 1:
        buttons.append({"text": c["apply_all"], "data": "upd:o:%s:a" % prefix})
    for index, item in enumerate(shown):
        buttons.append({"text": c["apply_one"].format(name=item["name"], target=vfmt(item["target"])),
                        "data": "upd:o:%s:%d" % (prefix, index)})
    if pick is None:
        body = render_offer(shown, lang)
        if ledger.get("split_rows"):
            rows_html, sync_buttons = split_view(ledger)
            body += "\n\n" + rows_html
            buttons += sync_buttons
    else:
        index = pick[1:] if pick.startswith("b") else pick
        wanted = rows if index == "a" else [rows[int(index)]]
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
    return {"html": body, "buttons": buttons}


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
    state = {"asked": args.signature, "ledger": args.ledger, "message_id": args.message_id,
             "at": int(time.time())}
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
    # The authenticated callback is the only caller.  The tap time is the
    # callback-receipt time the bridge recorded (grill fix 4), and the
    # crew_consent artifact is written before any executor is launched.
    if item["crew_kit"] in consented_kits(ledger):
        return
    script = os.path.join(repo, "scripts", "pack", "crew_consent.sh")
    subprocess.check_call([script, "record", "--kit", item["crew_kit"]], stdout=subprocess.DEVNULL)
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


def hold(ledger, items, running, follow):
    """The busy refusal: the tap's ledger turns "busy" and keeps what this
    tap would have applied -- key + shown state of each item, the key set
    of their closure (a new riding dependency = changed) -- then asks."""
    ledger["state"] = "busy"
    ledger["follow"] = dict(follow, want=[shown_state(item) for item in items],
                            closure=[item["key"] for item in closure(ledger, items)],
                            asked_at=int(time.time()))
    atomic_write(ledger_path(ledger["id"]), ledger)
    return {"started": False, "busy": True, "ledger": ledger["id"], "text": busy_text(running),
            "buttons": follow_buttons(ledger)}


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


def active_run(exclude):
    """busy(), or a run whose state is final but whose worker is still
    delivering (result / restart notices): the next run waits for both."""
    if busy(exclude) is not None:
        return True
    for name in sorted(os.listdir(ledger_dir())):
        if not name.endswith(".json") or name == exclude + ".json":
            continue
        try:
            other = load_ledger(name[:-5])
        except (ValueError, OSError):
            continue
        if (other.get("state") in FINAL_STATES and other.get("stage") == "delivering"
                and pid_alive((other.get("supervisor") or {}).get("pid"))):
            return True
    return False


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
    if any(item["needs_consent"] and item["crew_kit"] not in follow["consent"] for item in full):
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
    if send:
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
            return settle(led, "failed", {"text": html.escape(c["follow_failed"], quote=False)}, send)
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
        # From here the new run's worker owns the start notices of the other
        # agents, the result and the restart notices (a direct tap's path);
        # this chat gets the start line its bridge would have replied.
        return settle(led, "picked", {"text": start_text(lang, as_html=True), "picked": fresh["id"]}, send)
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
        text = start_text(led.get("lang", "ko"), as_html=True)
    elif state in FOLLOW_REPLY:
        text = html.escape(c[FOLLOW_REPLY[state]], quote=False)
    else:
        text = html.escape(c["stale"], quote=False)
    print(json.dumps({"action": state, "text": text, "parse_mode": "HTML"}, ensure_ascii=False))
    return 0


def start(ledger, items, tapped_at, follow=None):
    """Shared by /update and the notice: the same executor.  follow = the
    tap's own facts (via, scope, user, consent kits): a busy refusal then
    asks to carry the apply through (DGN-1794 r2, hold).  The pickup passes
    none -- it already waited for the running run."""
    if any(item["needs_consent"] and item["crew_kit"] not in consented_kits(ledger) for item in items):
        raise ValueError("crew consent required")
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
    ledger.update({"selection": [item["key"] for item in ordered], "selected_plan": selected_plan,
                   "state": "running", "tapped_at": tapped_at, "started_at": int(time.time())})
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


def cmd_pick(args):
    """A tap on the session-start notice or the /update screen (DGN-1775:
    one screen, one tap flow) ("전체 적용" = a, "<name> 적용" = its
    row index; "b<index>" from a round-3 bundle card = the same row).  The
    notice ledger is only what was SHOWN: a fresh ledger from a fresh plan
    decides, every tapped row must still be there with the same installed
    versions AND the same kit riding on it (else stale), and the owner's tap
    is the crew consent for the kit it names -- the kit is on the row, so
    there is no after-tap confirm (item 22)."""
    offer = load_ledger(args.ledger)
    lang = offer.get("lang", "ko")
    check_requester(offer, args.user)
    stale = {"action": "stale", "text": copy(lang)["stale"]}
    index = args.pick[1:] if args.pick.startswith("b") else args.pick
    rows = offer.get("rows") or [{"key": item["key"], "with": []} for item in offer["items"]]
    if offer.get("source") not in ("offer", "update") or offer["revision"] != args.revision:
        print(json.dumps(stale, ensure_ascii=False))
        return 0
    if index == "a" and not args.pick.startswith("b"):
        wanted = list(rows)
    elif index.isdigit() and int(index) < len(rows):
        wanted = [rows[int(index)]]
    else:
        print(json.dumps(stale, ensure_ascii=False))
        return 0
    preview = getattr(args, "preview", False)
    # The fresh plan is read with the tapped screen's own scope: the notice
    # for the crew(s) this instance leads, /update for every crew it is a
    # member of (DGN-1784) -- a wider or narrower re-plan would drift.
    scope = "offer" if offer.get("source") == "offer" else "crew"
    fresh = new_ledger(args, "notice", lang, scope=scope, persist=not preview)
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
        print(json.dumps(out, ensure_ascii=False))
        return 0
    full = closure(fresh, matched)
    # DGN-1794 r2: the tap names every kit on its rows, so those kits are
    # its consent -- now, or at the pickup of a queued follow-through.
    follow = {"via": "pick", "scope": scope, "user": tap_user(offer, args.user),
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
            outcome.update({"action": "started" if outcome.get("started") else "busy", "item": item})
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
    if only:
        return list(units)
    pid = item["id"]
    out = []
    for unit in units:
        name = unit.get("unit", "")
        if name.startswith("atomic group"):
            if item["key"].startswith("bundle:") and pid in name:
                out.append(unit)
        elif item["kind"] == "framework":
            # A crew framework row (item 26) owns every member's unit.
            if name in ["framework " + member for member in item["members"]]:
                out.append(unit)
        elif not item["key"].startswith("bundle:") and (name == "pack " + pid or name.endswith("/" + pid)):
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


def completion_text(results, items, lang):
    """The fixed result surface, including the restart callback completion."""
    return result_text(result_from_verified(results, items, None, lang), lang)


def fallback_text(results, items, lang):
    # Only when the agent cannot hear this (its own bridge is not back).
    # Reuses update_apply's DGN-1709 neutral lines; never "완료".
    c = copy(lang)
    by_key = dict((item["key"], item) for item in items)
    text = c["none_applied"] if all(r["status"] == "blocked" for r in results) else c["not_all"]
    lines = []
    inline = set()
    for result in results:
        item = by_key[result["key"]]
        stuck = [m for m in result["members"] if m["status"] in STOPPED and member_unit(result, m) == "ok"]
        if result["status"] == "complete":
            lines.append(done_line(item, lang))
        elif result["status"] == "blocked":
            lines.append(c["item_blocked"].format(name=item["name"], target=vfmt(item["target"])))
        elif stuck:
            # Item 24 wording: applied, and which agent is stopped.
            inline.update(m["root"] for m in stuck)
            lines.append(c["partial_item"][2:].format(
                name=item["name"], target=vfmt(item["target"]),
                agents=_agents([member_label(m["root"], m["name"], lang) for m in stuck], lang)))
        else:
            lines.append(c["item_unsure"].format(name=item["name"], target=vfmt(item["target"])))
    if len(results) > 1 or results[0]["status"] != "blocked":
        text += "\n\n" + "\n".join(lines)
    members = [m for r in results for m in r["members"]]

    def names(states):
        seen = []
        for m in members:
            label = member_label(m["root"], m["name"], lang)
            if m["status"] in states and label not in seen and m["root"] not in inline:
                seen.append(label)
        return seen
    for label, states in (("left_down", ("left_down", "incomplete")),
                          ("not_back", ("restart_failed", "not_responding")),
                          ("unconfirmed", ("unconfirmed", "unknown"))):
        found = names(states)
        if found:
            text += "\n" + c[label] + ", ".join(found)
    return text + "\n" + c["record_tail"]


def push(ledger, text, buttons=None):
    script = os.path.join(ledger["instance_root"], "routines", "push.sh")
    argv = ["bash", script, "--strict", "--text", text, "--audience", "owner"]
    if ledger.get("chat"):
        argv += ["--chat", ledger["chat"]]
    if buttons:
        argv += ["--buttons-json", json.dumps(buttons, ensure_ascii=False)]
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode == 0


GUIDE_PROBLEM = """The machine has already sent the update notice. Add at most ONE plain
reason line in the owner's language, only when outcome != "done", from the
record detail. Otherwise return NO_PUSH. Never repeat the notice or claim
completion. No field names, paths, ids, counters, or invented names. Names
ONLY from product / agents / *_labels display fields, copied as written."""
GUIDE_COMPLETE = """The machine owns the entire done notice. Return NO_PUSH.
Write zero owner-facing text."""


def result_text(record, lang=None):
    """DGN-1757: the offer's card layout; display fields only.  dec-222: one
    bold "<product> <target>" head per item (dec-234), one "└ <agents>: <result>"
    line per result under it; DGN-1790 r3: one quote per item, items one
    blank line apart."""
    lang = lang or record.get("lang", "ko")
    outcome = record.get("outcome")
    head = word(lang, "update.result_" + (outcome if outcome in ("done", "blocked") else "partial"))
    items = {}
    for unit in record.get("units") or []:
        item = items.setdefault((unit.get("product", ""), unit.get("target", "")), {})
        result = unit.get("result")
        key = {"ok": "ok", "blocked": "preflight", "incomplete": "incomplete", "untried": "untried"}.get(result, "unknown")
        words = word(lang, "update.result_" + key)
        if result == "blocked" and unit.get("dependency_product"):
            words = word(lang, "update.result_dependency").format(dep=unit["dependency_product"])
        agents = item.setdefault(words, [])
        for label in unit.get("agents") or []:
            if label not in agents:
                agents.append(label)
    blocks = [head]
    for (product, target), results in items.items():
        quote = ["<b>%s</b>" % html.escape(word(lang, "update.item_head").format(product=product, target=vfmt(target)))]
        quote += [html.escape(word(lang, "update.result_target").format(agents=AGENT_SEP.join(agents), result=words))
                  for words, agents in results.items()]
        blocks.append("<blockquote>%s</blockquote>" % "\n".join(quote))
    labels = dict.fromkeys((record.get("bridges_left_down_labels") or []) + (record.get("restart_failures_labels") or []))
    blocks.extend(html.escape(word(lang, "update.result_down").format(agent=label)) for label in labels)
    return "\n\n".join(blocks)


def with_versions(text, ledger, lang):
    """DGN-1790: an update result ends with what is installed now.  Read
    after the apply and its verification, so it is the post-update truth;
    unreadable -> the result text alone."""
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
            unit.setdefault("product", item["name"])
            own = [m for m in result["members"] if unit.get("unit") == "framework " + m["name"] or unit.get("unit", "").startswith(m["name"] + "/")]
            unit.setdefault("agents", [m["label"] for m in own or result["members"]])
            unit.setdefault("target", item["target"])
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
    body = """[cron-inject] update result (DGN-1417)

The owner asked for this update from the update notice or the update screen.
The record below is the machine's account of it -- it is for you, not for the
owner.

%s

```json
%s
```
""" % (GUIDE_COMPLETE if complete else GUIDE_PROBLEM, json.dumps(payload, ensure_ascii=False, indent=2))
    tmp = os.path.join(spool, "." + name + ".tmp")
    with open(tmp, "w") as handle:
        handle.write(body)
    os.rename(tmp, os.path.join(spool, name))
    return True


def deliver(ledger, items, results, record):
    """Exactly one delivery per ledger (claim file shared with sweep)."""
    if not claim(ledger_path(ledger["id"]) + ".delivered"):
        return None
    lang = ledger.get("lang", "ko")
    rec = result_from_verified(results, items, record, lang)
    stopped = []
    for result in results:
        for member in result["members"]:
            if member["status"] in STOPPED and member["root"] not in [m["root"] for m in stopped]:
                stopped.append({k: member[k] for k in ("name", "root", "label")})
    led = load_ledger(ledger["id"])
    led["result"] = dict(led.get("result") or {}, stopped=stopped)
    atomic_write(ledger_path(ledger["id"]), led)
    ledger["result"] = led["result"]
    sent = push(ledger, with_versions(result_text(rec, lang), ledger, lang), restart_buttons(ledger, lang))
    if not sent:
        # Never delegate the fixed notice to the model, including send failures.
        os.unlink(ledger_path(ledger["id"]) + ".delivered")
        return {"method": "none", "at": int(time.time())}
    if rec["outcome"] != "done":
        inbox_drop(ledger, {"status": overall(results), "apply_record": rec})
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
            "--text", text, "--audience", "owner"]
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def send_restart_notices(ledger, items, results, record):
    """Rehearsal (DOGANY_LAUNCHD_CAPTURE), a dry run or a lost record sends
    nothing; a blocked / stopped / unconfirmed agent is never a target (the
    result notice owns failures).  Returns {root: sent|render_failed|push_failed}."""
    if os.environ.get("DOGANY_LAUNCHD_CAPTURE") or record is None or record.get("dry_run"):
        return {}
    sent = {}
    for target in restart_notice_targets(ledger, items, results):
        root = target["root"]
        text = render_restart_notice(ledger["repo"], root, target["version"], target["source"], target.get("product", ""))
        if not text:
            sent[root] = "render_failed"
        else:
            sent[root] = "sent" if push_agent_notice(root, text) else "push_failed"
    return sent


# --------------------------------------------------------------------------
# DGN-1781: the start line in every other touched agent's own chat.
# --------------------------------------------------------------------------

def start_notice_targets(ledger, items):
    """Every agent root the selected items touch (framework rows, kit crew
    members, pack targets, a bundle's framework includes), once each, in
    item order -- except the requester's own agent (its bridge already
    replied with the start line)."""
    out, seen = [], set()
    for item in items:
        for root in list(item.get("member_roots") or []) + [fw["root"] for fw in item.get("includes") or []]:
            key = os.path.realpath(root)
            if key in seen or same_root(root, ledger["instance_root"]):
                continue
            seen.add(key)
            out.append(root)
    return out


def send_start_notices(ledger, items):
    """Before the apply stops anyone: each other touched agent hears the
    start line in its own chat and language, through the same sender as the
    restart notice.  A rehearsal (DOGANY_LAUNCHD_CAPTURE) sends nothing.
    Returns {root: sent|push_failed}."""
    if os.environ.get("DOGANY_LAUNCHD_CAPTURE"):
        return {}
    sent = {}
    for root in start_notice_targets(ledger, items):
        text = start_text(notice_lang(root, ledger["repo"]), as_html=False)
        sent[root] = "sent" if push_agent_notice(root, text) else "push_failed"
    return sent


def cmd_result_send(args):
    record = json.load(sys.stdin)
    lang = record.get("lang", "ko")
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
    return 0 if push(ledger, with_versions(result_text(record, lang), ledger, lang), restart_buttons(ledger, lang)) else 1


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
    starts = send_start_notices(ledger, items)
    if starts:
        ledger["start_notices"] = starts
        atomic_write(ledger_path(args.ledger), ledger)
    since = int(time.time())
    result_file = ledger_path(args.ledger) + ".result"
    apply = os.path.join(ledger["repo"], "scripts", "pack", "update_apply.sh")
    # DGN-1746: the record's display names in this ledger's language.
    env = dict(os.environ, DOGANY_UPDATE_LANG=ledger.get("lang", "ko"))
    proc = subprocess.run(["bash", apply, "--plan-file", ledger["selected_plan"], "--result-file", result_file],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    output = proc.stdout.decode("utf-8", "replace")
    try:
        with open(result_file, "r") as handle:
            record = json.load(handle)
    except (IOError, OSError, ValueError):
        record = None
    ledger = load_ledger(args.ledger)
    ledger["state"] = "verifying"
    ledger["stage"] = "verifying"
    ledger["result"] = {"rc": proc.returncode, "output": output[-20000:], "record": record,
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
    ledger["state"] = overall(results)
    ledger["stage"] = "delivering"
    atomic_write(ledger_path(args.ledger), ledger)
    delivery = deliver(ledger, items, results, record)
    notices = send_restart_notices(ledger, items, results, record)
    ledger = load_ledger(args.ledger)
    if delivery is not None:
        ledger["delivery"] = delivery
    if notices:
        ledger["restart_notices"] = notices
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
    if results:
        ledger["state"] = overall(results)
    ledger.setdefault("restarts", []).append({"root": member["root"], "by": str(args.user),
                                              "alive": bool(back), "at": int(time.time())})
    atomic_write(ledger_path(ledger["id"]), ledger)
    if not back:
        out = {"action": "failed", "alive": False,
               "text": c["restart_failed"].format(agent=josa_reul),
               "buttons": restart_buttons(ledger, lang, only_roots=[member["root"]])}
    elif results and ledger["state"] == "complete":
        out = {"action": "done", "alive": True, "text": completion_text(results, items, lang), "buttons": []}
    else:
        left = stopped_members(results, lang)
        text = c["restart_ok"].format(agent=_agents([label], lang))
        if left and partial_only(results):
            text += "\n\n" + partial_text(results, items, lang)
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
            undelivered = (ledger.get("state") in FINAL_STATES and not ledger.get("delivery")
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
    p = sub.add_parser("validate-apply")
    p.add_argument("--plan", required=True); p.add_argument("--result", required=True)
    p.add_argument("--repo", required=True); p.add_argument("--supervisor", type=int, required=True)
    p.set_defaults(fn=cmd_validate_apply)
    p = sub.add_parser("worker"); p.add_argument("--ledger", required=True); p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("restart"); p.add_argument("--ledger", required=True); p.add_argument("--index", type=int, required=True)
    p.add_argument("--user", default=None); p.set_defaults(fn=cmd_restart)
    p = sub.add_parser("sweep"); p.set_defaults(fn=cmd_sweep)
    p = sub.add_parser("follow"); p.add_argument("--ledger", required=True); p.add_argument("--choice", choices=("y", "n"), required=True)
    p.add_argument("--user", default=None); p.add_argument("--chat", required=True); p.set_defaults(fn=cmd_follow)
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
