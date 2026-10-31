#!/usr/bin/env python3
# test-lane: fast
"""Locks for DGN-1837: a kit-pending main's first-contact opener must not
sound like the life kit (owner rehearsal 2026-10-03: "라이프킷처럼 안내하는
게 문제").

Contract under test:
  F1  single source: routines/lib/first_contact_terms.py flags the observed
      opener (domain terms + "dangsin") and passes a compliant one (ko + en);
      common words that merely contain a pronoun stem stay clean.
  F2  Stop gate, kit-pending (Primary focus placeholder), bridge-opened
      first-contact turn: the observed opener is blocked once with a
      regenerate reason naming the terms; a compliant 2-sentence opener
      passes (ko + en); an opener asking a question is blocked;
      stop_hook_active yields; gate switch off -> no block.
  F3  owner spoke first (no bridge line), Name still unset: a first reply
      listing capabilities is blocked; a clean greeting that leads into Q1
      (a question) passes; a later turn is never checked.
  F4  a domain-role agent (Primary focus filled) may describe its role --
      never blocked by this check.
  F5  onboarding-check.py: the en/ko first-contact rule carries the concrete
      prohibitions and the GENERAL menu offers no life-assistant quick start;
      a kit-pending main gets the term list rendered from the same source,
      a role-stamped one does not.
  r2 (rehearsal 2026-10-03 14:33: the owner's hello got the same opener
  back; owner copy 14:34 "newly born"):
  F6  rehearsal replay, state-keyed: claim -> bridge opener (FC_OK, put on
      record) -> owner hello: the UserPromptSubmit context says identity
      setup + Q1; the same opener again is BLOCKED (fresh session and same
      session); a re-intro without a question is blocked; a reply that does
      not ask the name on that first turn is blocked; a greet-back + name ask
      passes; the opener checks no longer apply to that turn (a question is
      fine); a later turn (Q2) is not held to a name ask; a SessionStart
      after the opener injects the identity-setup line, not the term list;
      a regenerated opener refreshes the record; a new onboarding cycle
      clears it; the owner-spoke-first greeting that already asked Q1 owes
      no second name ask.
      DGN-1849: Q1 is a machine-sent screen now, so the model-written
      repeat / Q1 checks run only when the onboarding verb is missing; F6
      drives them against a routines copy without routines/onboarding-
      screen.py (GATE_NO_VERB).  With the verb present a model-written
      reply is a VERB_SKIP_BLOCK (tests/dgn1849_onboarding_screens_selftest.py).
      The SessionStart identity-setup line is English for both locales.
  F7  wording: en/ko rules say newly born (never "newly connected"), apply
      the opener to the opening turn only, carry the sent-once clause, and
      the origin clause allows "newly born" while banning the creator story;
      the gate logs no origin copy-miss for "newly born".
  DGN-1848 (rehearsal 2026-10-04 10:09: "i gos-ui gaein eijeonteu"):
  F8  the opener identity carries no owner possessive in any opener rule
      text (bridge turn, en/ko rule, gate reason, term line); the rules ban
      the substitute possessive; the gate blocks the observed substitute
      possessives with a reason that names them, and passes the plain
      opener and ordinary place words.
"""
import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(_HERE, ".."))
GATE = os.path.join(ROOT, "onboarding-gate-stop.py")
CHECK = os.path.join(ROOT, "onboarding-check.py")
TERMS = os.path.join(ROOT, "lib", "first_contact_terms.py")

MARKER = "<!-- ONBOARDING_PENDING -->"
PLACEHOLDER_FOCUS = ("- Primary focus: (set at onboarding -- one prose line "
                     "naming the main hat;\n  the general front-door bullet "
                     "above still applies)")
FIRST_CONTACT = ("[bridge:injected-turn]\n[bridge:first-contact] The owner has "
                 "just claimed this bot (/claim succeeded) and is in the chat now.")
# Ticket DGN-1837 "Observed" opener, verbatim.
OBSERVED = ("안녕하세요! 저는 당신의 개인 에이전트예요. 일정이나 할 일 정리부터 "
            "궁금한 것 찾아보기, 이런저런 고민 같이 풀어보는 일까지, 생활 속 "
            "여러 일을 한자리에서 도와드려요. 편하게 인사 한마디 건네 주세요.")
COMPLIANT_KO = "안녕하세요, 새로 태어난 개인 에이전트예요. 편하게 인사 한마디 건네 주세요."
COMPLIANT_EN = ("Hi, I'm a personal agent, newly born. "
                "Say hello whenever you like.")

_failures = []
_tmp_roots = []


def _gate_without_verb():
    """A copy of the routines dir minus the onboarding verb (DGN-1849): the
    gate's model-written fallback checks."""
    dst = tempfile.mkdtemp(prefix="dgn1837-noverb-")
    _tmp_roots.append(dst)
    shutil.copytree(ROOT, os.path.join(dst, "routines"), ignore=shutil.ignore_patterns(
        "onboarding-screen.py", "tests", "__pycache__", "retire", "bundle"))
    return os.path.join(dst, "routines", "onboarding-gate-stop.py")


GATE_NO_VERB = None


def check(name, cond, detail=""):
    if not cond:
        _failures.append("%s %s" % (name, detail))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


terms = load(TERMS, "first_contact_terms_t1837")
oc = load(CHECK, "onboarding_check_t1837")


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def mk(lang="ko", role=None, name_set=False):
    root = tempfile.mkdtemp(prefix="dgn1837-")
    _tmp_roots.append(root)
    focus = ("- Primary focus: %s" % role) if role else PLACEHOLDER_FOCUS
    write(os.path.join(root, "identity", "hot.custom.agent.md"), "\n".join([
        MARKER, "# AGENT", "## Identity",
        "- Name: **Mint**" if name_set else "- Name: **(set at onboarding)**",
        "- Emoji: (set at onboarding)", "## Role",
        "- General personal agent: the user's single front door.", focus,
        "## Relationship", "- Call the user **(set at onboarding)**.",
        "- Tone: (set at onboarding)", "- Humor: (set at onboarding)"]) + "\n")
    write(os.path.join(root, "identity", "hot.custom.owner.md"),
          "# USER\n- Name: 김하늘 (2026-10-03, owner)\n")
    write(os.path.join(root, "config", "agent.conf"),
          "AGENT_LANG=%s\nKIT=%s\n" % (lang, "none" if role else "pending"))
    return root


def env_for(root, interactive=True, gate_off=False):
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "AGENT_CONF_FILE", "INSTANCE_CONF_FILE", "LIFEKIT_FILE",
              "GATE_STOP_ONBOARDING", "GATE_STOP_ALL", "GATE_SWITCH_CONF"):
        env.pop(k, None)
    env["ONBOARDING_FILE"] = root
    if interactive:
        env["PROJECT_ROOT"] = root
    if gate_off:
        env["GATE_STOP_ONBOARDING"] = "0"
    return env


def transcript(root, reply, boundary=FIRST_CONTACT, earlier=None,
               earlier_boundary="hi"):
    entries = []
    if earlier:
        entries += [{"type": "user", "message": {"content": earlier_boundary}},
                    {"type": "assistant", "message": {"content": [
                        {"type": "text", "text": earlier}]}}]
    entries += [{"type": "user", "message": {"content": boundary}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": reply}]}}]
    path = os.path.join(root, "t.jsonl")
    write(path, "\n".join(json.dumps(e, ensure_ascii=False)
                          for e in entries) + "\n")
    return path


def stop(root, reply, active=False, gate_off=False, gate=None, **kw):
    payload = {"hook_event_name": "Stop", "cwd": root,
               "transcript_path": transcript(root, reply, **kw),
               "last_assistant_message": reply, "stop_hook_active": active}
    proc = subprocess.run([sys.executable, gate or GATE], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=15,
                          env=env_for(root, gate_off=gate_off))
    if proc.returncode != 0:
        _failures.append("gate exit %d: %s" % (proc.returncode, proc.stderr))
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def blocked(out):
    return bool(out) and out.get("decision") == "block" \
        and "DGN-1837" in out.get("reason", "")


def repeat_blocked(out):
    return blocked(out) and "already sent" in out.get("reason", "")


def prompt_ctx(root, prompt):
    proc = subprocess.run([sys.executable, GATE], input=json.dumps(
        {"hook_event_name": "UserPromptSubmit", "cwd": root,
         "prompt": prompt}), capture_output=True, text=True, timeout=15,
        env=env_for(root))
    out = proc.stdout.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] \
        if out else ""


def gate_log(root):
    try:
        with open(os.path.join(root, ".telegram_bot", "logs",
                               "onboarding-gate.log"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def opener_record(root):
    path = os.path.join(root, ".telegram_bot", "logs",
                        ".onboarding-opener-sent")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None


def session_ctx(root):
    proc = subprocess.run([sys.executable, CHECK],
                          input=json.dumps({"cwd": root, "source": "startup"}),
                          capture_output=True, text=True, timeout=15,
                          env=env_for(root, interactive=False))
    out = proc.stdout.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


# ---------------------------------------------------------------- F1

def test_f1_term_source():
    v = terms.opener_violations(OBSERVED)
    check("F1 observed flagged", v is not None)
    if v:
        for t in ("일정", "할 일", "정리", "찾아", "고민", "생활"):
            check("F1 term %s" % t, t in v["terms"], str(v))
        check("F1 dangsin", "당신" in v["pronouns"], str(v))
    check("F1 compliant ko", terms.opener_violations(COMPLIANT_KO) is None)
    check("F1 compliant en", terms.opener_violations(COMPLIANT_EN) is None)
    check("F1 stems in common words", terms.opener_violations(
        "그대로 너무 반가워요.") is None)
    en = terms.opener_violations("I can help with your schedule, email and "
                                 "looking things up.")
    check("F1 en terms", en and {"schedule", "email", "look up"}
          <= set(en["terms"]), str(en))
    check("F1 name guess", terms.opener_violations(
        "하늘님 반가워요.", ("하늘님",))["pronouns"] == ["하늘님"])


# ---------------------------------------------------------------- F2

def test_f2_bridge_opener():
    root = mk()
    out = stop(root, OBSERVED)
    check("F2 observed blocked", blocked(out), str(out))
    if out:
        check("F2 reason names term", "'일정'" in out["reason"], out["reason"])
        check("F2 reason names pronoun", "'당신'" in out["reason"])
        check("F2 reason english", "Regenerate the COMPLETE opening message"
              in out["reason"])
    check("F2 compliant ko passes", not blocked(stop(root, COMPLIANT_KO)))
    check("F2 question blocked", blocked(stop(
        root, "안녕하세요, 새로 태어난 개인 에이전트예요. 이름을 정해 주실래요?")))
    check("F2 stop_hook_active yields", not blocked(stop(root, OBSERVED,
                                                         active=True)))
    check("F2 switch off", not blocked(stop(root, OBSERVED, gate_off=True)))
    en = mk(lang="en")
    check("F2 compliant en passes", not blocked(stop(en, COMPLIANT_EN)))
    check("F2 en capability list blocked", blocked(stop(
        en, "Hi, I'm a personal agent. I can help with your schedule, "
            "to-dos and reminders. Say hello!")))


# ---------------------------------------------------------------- F3

def test_f3_owner_spoke_first():
    root = mk()
    check("F3 first reply listing blocked", blocked(stop(
        root, OBSERVED, boundary="안녕")))
    check("F3 clean greeting + Q1 passes", not blocked(stop(
        root, "반가워요, 새로 태어난 개인 에이전트예요. 제 이름을 정해 주실래요?",
        boundary="안녕")))
    check("F3 later turn unchecked", not blocked(stop(
        root, "일정 정리도 도와드릴게요.", boundary="좋아",
        earlier=COMPLIANT_KO)))
    named = mk(name_set=True)
    check("F3 name set -> not an opener", not blocked(stop(
        named, OBSERVED, boundary="안녕")))


# ---------------------------------------------------------------- F4

def test_f4_domain_role_may_describe():
    root = mk(role="health coach -- exercise, diet and sleep")
    out = stop(root, "안녕하세요, 운동과 식단 관리를 돕는 건강 에이전트예요. "
                     "편하게 인사 건네 주세요.")
    check("F4 domain role not blocked", not blocked(out), str(out))


# ---------------------------------------------------------------- F5

def test_f5_injected_rule():
    en, ko = oc.ONBOARDING_CTX["en"], oc.ONBOARDING_CTX["ko"]
    check("F5 en opener rule", "KIT-PENDING OPENER" in en
          and "name NO task, example" in en and "NO second-person pronoun" in en
          and "ask NO question" in en)
    check("F5 ko opener rule", "킷 대기 첫 인사" in ko and "2인칭 대명사" in ko)
    check("F5 en menu", "schedule briefing" not in en
          and "item 2 must be kit-neutral" in en)
    check("F5 ko menu", "일정 브리핑 받아보기" not in ko
          and "2번도 킷 중립 항목만" in ko)
    for lang in ("ko", "en"):
        ctx = session_ctx(mk(lang=lang))
        check("F5 %s term list rendered" % lang,
              terms.prohibited_terms_line(lang) in ctx)
        role_ctx = session_ctx(mk(lang=lang, role="health coach"))
        check("F5 %s role-stamped no term list" % lang,
              "[kit-pending opener]" not in role_ctx and role_ctx)


# ---------------------------------------------------------------- F6

# Owner's replies in the replay.
HELLO = "안녕"
GREET_Q1 = "반가워요! 먼저 제 이름을 정해 주실래요?"


def test_f6_rehearsal_replay():
    global GATE_NO_VERB
    GATE_NO_VERB = GATE_NO_VERB or _gate_without_verb()
    nv = GATE_NO_VERB
    root = mk()
    # 1. claim -> bridge-opened opener: clean, put on record.
    check("F6 opener passes", not blocked(stop(root, COMPLIANT_KO)))
    rec = opener_record(root)
    check("F6 opener on record", rec is not None and rec.get("text")
          == COMPLIANT_KO and rec.get("q1_due") is True, str(rec))
    check("F6 FC_OK + FC_SENT", "FC_OK" in gate_log(root)
          and "FC_SENT" in gate_log(root), gate_log(root))
    # 2. owner says hello -> the turn's context is identity setup.
    ctx = prompt_ctx(root, HELLO)
    check("F6 hello ctx identity setup", ctx.startswith(
        "First-contact phase (DGN-1837)") and "ALREADY SENT" in ctx
        and "Q1" in ctx, ctx[:200])
    # 3a. the measured defect: the same opener again, fresh session (the
    # transcript has no trace of the opener) -> blocked.
    out = stop(root, COMPLIANT_KO, boundary=HELLO, gate=nv)
    check("F6 repeat blocked (fresh session)", repeat_blocked(out), str(out))
    if out:
        check("F6 repeat reason asks Q1", "ask Q1" in out["reason"]
              and "do not introduce yourself again" in out["reason"])
    check("F6 FC_REPEAT_BLOCK logged", "FC_REPEAT_BLOCK" in gate_log(root))

    def fresh():
        r = mk()
        stop(r, COMPLIANT_KO)
        return r
    # 3b. same session (opener visible in the transcript) -> blocked too.
    r = fresh()
    check("F6 repeat blocked (same session)", repeat_blocked(stop(
        r, COMPLIANT_KO, boundary=HELLO, earlier=COMPLIANT_KO,
        earlier_boundary=FIRST_CONTACT, gate=nv)))
    # 3c. rephrased re-intro, no question -> blocked.
    r = fresh()
    check("F6 re-intro without question blocked", repeat_blocked(stop(
        r, "네, 반가워요. 저는 갓 깨어난 개인 에이전트입니다.",
        boundary=HELLO, gate=nv)))
    # 3d. greets back but asks no name -> blocked on that first turn.
    r = fresh()
    check("F6 no name ask blocked", repeat_blocked(stop(
        r, "반가워요! 오늘 기분은 어떠세요?", boundary=HELLO, gate=nv)))
    # 4. the required reply: greet back + name ask -> passes, and the
    # opener checks (no question) do not apply to this turn.
    r = fresh()
    out = stop(r, GREET_Q1, boundary=HELLO, gate=nv)
    check("F6 greet + Q1 passes", not blocked(out), str(out))
    check("F6 FC_SETUP_OK logged", "FC_SETUP_OK" in gate_log(r), gate_log(r))
    check("F6 q1 no longer due", (opener_record(r) or {}).get("q1_due") is False)
    # 5. a later turn (Q2) owes no name ask, still may not repeat.
    check("F6 Q2 turn passes", not blocked(stop(
        r, "좋아요. 저를 나타낼 이모지 하나 골라 주실래요?", boundary="민트",
        gate=nv)))
    check("F6 later repeat still blocked", repeat_blocked(stop(
        r, COMPLIANT_KO, boundary="민트", gate=nv)))
    # 6. a session started after the opener: identity-setup line, no
    # opener term list.
    ctx = session_ctx(r)
    check("F6 SessionStart identity setup", "[first-contact phase: identity setup]" in ctx
          and "[kit-pending opener]" not in ctx, ctx[-400:])
    en = mk(lang="en")
    stop(en, COMPLIANT_EN)
    ctx = session_ctx(en)
    check("F6 en SessionStart identity setup",
          "[first-contact phase: identity setup]" in ctx
          and "[kit-pending opener]" not in ctx)
    check("F6 en repeat blocked", repeat_blocked(stop(
        en, COMPLIANT_EN, boundary="hello", gate=nv)))
    # 7. a blocked opener's regeneration replaces the record.
    r = mk()
    check("F6 observed blocked", blocked(stop(r, OBSERVED)))
    stop(r, COMPLIANT_KO, active=True)
    check("F6 regenerated opener on record",
          (opener_record(r) or {}).get("text") == COMPLIANT_KO, str(opener_record(r)))
    # 8. a new onboarding cycle (arm sentinel gone) clears the record.
    os.remove(os.path.join(r, ".telegram_bot", "logs",
                           ".onboarding-gate-armed"))
    prompt_ctx(r, HELLO)
    check("F6 new cycle clears record", opener_record(r) is None)
    # 9. owner spoke first, greeting already asked Q1 -> nothing more owed.
    r = mk()
    check("F6 owner-first greet + Q1 passes", not blocked(stop(
        r, "반가워요, 새로 태어난 개인 에이전트예요. 제 이름을 정해 주실래요?",
        boundary=HELLO, gate=nv)))
    check("F6 owner-first q1 not due", (opener_record(r) or {}).get("q1_due") is False)
    check("F6 owner-first next turn free", not blocked(stop(
        r, "좋은 이름이에요. 이모지도 하나 골라 주실래요?", boundary="민트",
        gate=nv)))


# ---------------------------------------------------------------- F7

def test_f7_wording():
    en, ko = oc.ONBOARDING_CTX["en"], oc.ONBOARDING_CTX["ko"]
    check("F7 en newly born", "personal agent, newly born" in en
          and "newly connected" not in en)
    check("F7 ko newly born", "새로 태어난" in ko and "새로 연결된" not in ko)
    check("F7 en opening turn only", "the OPENING TURN ONLY" in en)
    check("F7 ko opening turn only", "첫 인사 턴에만 적용" in ko)
    check("F7 en sent once", "THE OPENER IS SENT ONCE" in en)
    check("F7 ko sent once", "첫 인사는 한 번만" in ko)
    check("F7 en origin relaxed", "saying you are newly born is allowed" in en
          and "who created, minted, sent" in en and "just born" not in en)
    check("F7 ko origin relaxed", "새로 태어났다는 말은 괜찮지만" in ko
          and "방금 태어났다는 말은 절대" not in ko)
    gate = load(GATE, "onboarding_gate_t1837")
    check("F7 gate reason newly born",
          "newly born" in gate.FIRST_CONTACT_BLOCK_REASON
          and "newly connected" not in gate.FIRST_CONTACT_BLOCK_REASON)
    check("F7 gate copy rule", gate.copy_violations(
        {}, None, "새로 태어난 개인 에이전트예요.") is None)
    check("F7 creator story still flagged", (gate.copy_violations(
        {}, None, "메인 에이전트에게서 태어났어요.") or {}).get("kinds")
        == ["origin"])
    check("F7 en creator story flagged", (gate.copy_violations(
        {}, None, "I was born from the main agent.") or {}).get("kinds")
        == ["origin"])


# ---------------------------------------------------------------- F8

# Rehearsal 2026-10-04 10:09 opener, verbatim.
OBSERVED_1848 = ("안녕하세요, 막 태어난 이 곳의 개인 에이전트예요. "
                 "편하게 인사 한마디 건네 주세요.")
OWNER_POSSESSIVE = ("user's personal agent", "personal agent of the user",
                    "유저의 개인 에이전트", "사용자의 개인 에이전트")


def test_f8_no_owner_possessive():
    gate = load(GATE, "onboarding_gate_t1848")
    bot_src = open(os.path.join(ROOT, "..", "bridge", "bot.py"),
                   encoding="utf-8").read()
    turn = bot_src[bot_src.index("FIRST_CONTACT_TURN = (") + 21:]
    turn = ast.literal_eval(turn[:turn.index("\n)\n") + 2])
    texts = {"bridge turn": turn,
             "en rule": oc.ONBOARDING_CTX["en"],
             "ko rule": oc.ONBOARDING_CTX["ko"],
             "gate reason": gate.FIRST_CONTACT_BLOCK_REASON,
             "en term line": terms.prohibited_terms_line("en"),
             "ko term line": terms.prohibited_terms_line("ko")}
    for label, text in texts.items():
        for bad in OWNER_POSSESSIVE:
            check("F8 %s has no %r" % (label, bad), bad not in text)
    check("F8 en identity", "who you are (a personal agent, newly born)"
          in oc.ONBOARDING_CTX["en"])
    check("F8 ko identity", "내가 누구인지(새로 태어난 개인 에이전트)"
          in oc.ONBOARDING_CTX["ko"])
    check("F8 bridge identity", "you are a personal agent, newly born" in turn
          and "names no owner at all" in turn)
    check("F8 en bans substitute", "name NO owner" in oc.ONBOARDING_CTX["en"]
          and "this place's" in oc.ONBOARDING_CTX["en"])
    check("F8 ko bans substitute", "대체 소유격" in oc.ONBOARDING_CTX["ko"]
          and "이곳의" in oc.ONBOARDING_CTX["ko"])
    check("F8 gate reason bans substitute", "Name NO owner"
          in gate.FIRST_CONTACT_BLOCK_REASON
          and "this place's" in gate.FIRST_CONTACT_BLOCK_REASON)
    check("F8 en term line not your", "not even 'your personal agent'"
          in terms.prohibited_terms_line("en"))
    for form in terms.KO_SUBSTITUTE_POSSESSIVE:
        check("F8 ko term line %s" % form,
              form in terms.prohibited_terms_line("ko"))
    v = terms.opener_violations(OBSERVED_1848)
    check("F8 observed flagged", v is not None
          and v["pronouns"] == ["이 곳의"] and not v["terms"], str(v))
    for form in ("이곳의", "여기의"):
        check("F8 %s flagged" % form, terms.opener_violations(
            "안녕하세요, %s 개인 에이전트예요." % form) is not None)
    check("F8 place word alone clean", terms.opener_violations(
        "이곳에서 처음 인사드려요. 여기 있어요.") is None)
    root = mk()
    out = stop(root, OBSERVED_1848)
    check("F8 gate blocks observed", blocked(out), str(out))
    if out:
        check("F8 reason names owner possessive",
              "named an owner for yourself ('이 곳의')" in out["reason"]
              and "addressed the user" not in out["reason"], out["reason"])
    check("F8 plain opener passes", not blocked(stop(root, COMPLIANT_KO)))


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
    finally:
        for root in _tmp_roots:
            shutil.rmtree(root, ignore_errors=True)
    if _failures:
        print("FAIL (%d)" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("OK test-dgn1837-first-contact-opener (%d tests)" % len(tests))


if __name__ == "__main__":
    main()
