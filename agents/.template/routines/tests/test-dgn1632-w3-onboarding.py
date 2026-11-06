#!/usr/bin/env python3
# test-lane: fast
"""Locks for the DGN-1632 W3 newborn-onboarding + handoff-seam changes
(owner rehearsal 2026-09-26, items B5-B11 / C12-C13).

Contract under test:
  B5  SessionStart context (en/ko) forbids an origin story in the first
      message. The former Stop-seat copy-miss check retired in v2.7.
  B7  the context keeps the canonical Q1 -> Q5 order in both languages.
  B8  Q3 candidates come from the DGN-1719 single source
      (routines/lib/q3_candidates.py): the owner-name candidate is
      '<given>' + the honorific suffix, no space; with the leader's address
      term the injected "[Q3 derived candidates]" block lists both; the
      bridge's button parser turns a two-candidate Q3 list into exactly two
      buttons.
  B9  pack record config/packs/<id>.tone_candidates.json -> the Q4 block
      (order + verbatim); malformed / wrong language -> no block (generic
      four stay); the health-specific example is gone from the context.
  B10 Q5 = plain 10/30/50/70/90 % options -> five buttons, no sample lines.
  B11 completion echo is an itemized list (ko labels present).
  C12/C13 continuation handoff guidance remains in the prompt, independently
      of the Stop checks retired in v2.7.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(_HERE, ".."))
# The instance/template root this file sits in. Inside a live instance
# (.instance.conf present) it is NOT a template (DGN-1734).
TEMPLATE = os.path.normpath(os.path.join(ROOT, ".."))
GATE = os.path.join(ROOT, "onboarding-gate-stop.py")
CHECK = os.path.join(ROOT, "onboarding-check.py")

MARKER = "<!-- ONBOARDING_PENDING -->"
PACK_CTX = "[pack_health] run routines/pack-health-next.sh; entry skill health-onboard"
HYEONGNIM = "고객님"
GIVEN_NIM = "하늘님"
FULL_NAME = "김하늘"
STYLE = "스타일"

_failures = []


def check(name, cond, detail=""):
    if not cond:
        _failures.append("%s %s" % (name, detail))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oc = load(CHECK, "onboarding_check_w3")
q3 = load(os.path.join(ROOT, "lib", "q3_candidates.py"), "q3_candidates_w3")
gate = load(GATE, "onboarding_gate_w3")


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def persona(call_set=False, filled=False, marker=True):
    lines = [MARKER] if marker else []
    lines += ["# AGENT", "## Identity",
              "- Name: **Mint**" if filled else "- Name: **(set at onboarding)**",
              "- Emoji: X" if filled else "- Emoji: (set at onboarding)",
              "## Role", "- Primary focus: health coach",
              "## Relationship",
              "- Call the user **%s**." % (HYEONGNIM if call_set or filled
                                           else "(set at onboarding)"),
              "- Tone: warm" if filled else "- Tone: (set at onboarding)",
              "- Humor: 30%" if filled else "- Humor: (set at onboarding)"]
    return "\n".join(lines) + "\n"


def mk(root, lang="ko", owner_name=FULL_NAME, tone=None, klass="domain",
       pack=True, leader_call=None, **kw):
    write(os.path.join(root, "identity", "hot.custom.agent.md"), persona(**kw))
    conf = "DOGANY_AGENT_CLASS=%s\n" % klass
    if leader_call:
        lead = os.path.join(root, "_leader")
        write(os.path.join(lead, "identity", "hot.custom.agent.md"),
              "# AGENT\n- Call the user: **%s**\n" % leader_call)
        conf += "DOGANY_LEADER_ROOT=%s\n" % lead
    owner = "# USER\n"
    if owner_name:
        owner += "- Name: %s (2026-09-26, owner)\n" % owner_name
    write(os.path.join(root, "identity", "hot.custom.owner.md"), owner)
    write(os.path.join(root, ".instance.conf"), conf)
    write(os.path.join(root, "config", "agent.conf"),
          "AGENT_LANG=%s\nKIT=none\n" % lang)
    if pack:
        write(os.path.join(root, "config", "packs", "pack_health.post_mint_next"),
              PACK_CTX + "\n")
    if tone is not None:
        write(os.path.join(root, "config", "packs",
                           "pack_health.tone_candidates.json"),
              json.dumps(tone, ensure_ascii=False))


def env_for(root, interactive=True):
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "AGENT_CONF_FILE", "INSTANCE_CONF_FILE", "LIFEKIT_FILE",
              "GATE_STOP_ONBOARDING", "GATE_STOP_ALL", "GATE_SWITCH_CONF"):
        env.pop(k, None)
    env["ONBOARDING_FILE"] = root
    if interactive:
        env["PROJECT_ROOT"] = root
    return env


def session_ctx(root):
    proc = subprocess.run([sys.executable, CHECK],
                          input=json.dumps({"cwd": root, "source": "startup"}),
                          capture_output=True, text=True, timeout=15,
                          env=env_for(root, interactive=False))
    out = proc.stdout.strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def run_gate(root, payload):
    payload = dict(payload, cwd=root)
    proc = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=15,
                          env=env_for(root))
    if proc.returncode != 0:
        _failures.append("gate exit %d: %s" % (proc.returncode, proc.stderr))
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def transcript(root, text, touch=True):
    entries = [{"type": "user", "message": {"content": "hi"}}]
    if touch:
        entries.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": os.path.join(
                root, "identity", "hot.custom.agent.md")}}]}})
    entries.append({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}})
    path = os.path.join(root, "t.jsonl")
    write(path, "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")
    return path


def gate_log(root):
    path = os.path.join(root, ".telegram_bot", "logs", "onboarding-gate.log")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def prompt_ctx(root):
    out = run_gate(root, {"hook_event_name": "UserPromptSubmit", "prompt": "ok"})
    return out["hookSpecificOutput"]["additionalContext"] if out else ""


def options_parser():
    """bridge/options.py extract_options, importable without a bot."""
    if importlib.util.find_spec("telegram") is None:
        sys.modules.setdefault("telegram", MagicMock())
    os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
    # Pin THIS tree's bridge: a PYTHONPATH / installed-instance `bridge` ahead of it
    # (or already imported) would silently test a different converter.
    while TEMPLATE in sys.path:
        sys.path.remove(TEMPLATE)
    sys.path.insert(0, TEMPLATE)
    for _m in [m for m in sys.modules if m == "bridge" or m.startswith("bridge.")]:
        del sys.modules[_m]
    # options only needs the translation function; avoid importing the full
    # bridge runtime (and its optional deployment dependencies) in this
    # portable selftest.
    i18n = MagicMock()
    i18n.t.side_effect = lambda key, **_kw: key
    sys.modules["bridge.i18n"] = i18n
    from bridge.options import extract_options
    return extract_options


# ---------------------------------------------------------------- B5 / B7 / B11

def test_context_rules_both_languages():
    en, ko = oc.ONBOARDING_CTX["en"], oc.ONBOARDING_CTX["ko"]
    check("B5 en origin", "NO ORIGIN STORY" in en)
    check("B5 ko origin", "출생 서사 금지" in ko)
    check("B5 gate reason", "no origin story" in gate.BLOCK_REASON)
    check("B7 en order", "Q1(name) -> Q2(emoji) -> Q3(address term) -> "
          "Q4(tone) -> Q5(humor)" in en)
    check("B7 ko order", "Q1(이름) -> Q2(이모지) -> "
          "Q3(호칭)" in ko)
    check("B11 en itemized", "ITEMIZED LIST" in en)
    for label in ("이름", "호칭", "말투", "유머"):
        check("B11 en ko-label %s" % label, "'%s'" % label in en)
    check("B11 ko itemized", "항목별 목록" in ko)
    check("C12 en handoff", "CONTINUATION HANDOFF SHAPE" in en)
    check("C12 ko handoff", "연속성 인계 형태" in ko)


# ---------------------------------------------------------------- B9 / B10 text

def test_tone_and_humor_rules():
    for lang in ("en", "ko"):
        ctx = oc.ONBOARDING_CTX[lang]
        check("B9 %s pack block ref" % lang,
              "[tone candidates -- from the installed pack]" in ctx)
        for gone in ("health-trainer", "No excuses for skipping today",
                     "헬스트레이너",
                     "오늘 훈련 빠질"):
            check("B9 %s health example removed (%s)" % (lang, gone),
                  gone not in ctx)
        for pct in ("'1. 10%'", "'2. 30%'", "'3. 50%'", "'4. 70%'", "'5. 90%'"):
            check("B10 %s %s" % (lang, pct), pct in ctx)
    check("B10 en no samples", "no sample lines" in oc.ONBOARDING_CTX["en"])


# ---------------------------------------------------------------- B8

def test_owner_candidate_format():
    # The DGN-1719 helper is the only Q3 formatter; onboarding-check.py and
    # the Stop seat must not carry a second one.
    for dup in ("owner_address_candidate", "owner_profile_name",
                "q3_owner_candidate_block", "_KO_HONORIFIC"):
        check("B8 no duplicate helper (%s)" % dup, not hasattr(oc, dup))
    f = q3.owner_name_candidate
    check("B8 3-syllable", f(FULL_NAME, "ko") == GIVEN_NIM, f(FULL_NAME, "ko"))
    check("B8 given only", f("하늘", "ko") == GIVEN_NIM)
    check("B8 spaced", f("김 하늘", "ko") == GIVEN_NIM)
    check("B8 no space", " " not in f(FULL_NAME, "ko"))
    check("B8 none", not f("", "ko"))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "identity", "hot.custom.owner.md")
        write(p, "# USER\n- **Name**: %s (2026-09-26, owner)\n" % FULL_NAME)
        check("B8 bold name line", q3.owner_name(d) == FULL_NAME, q3.owner_name(d))
        write(p, "# USER\n- Name: (set at onboarding)\n")
        check("B8 placeholder", not q3.owner_name(d))
        write(p, "# USER\n")
        check("B8 absent", not q3.owner_name(d))


def test_owner_candidate_injected():
    with tempfile.TemporaryDirectory() as d:
        mk(d, leader_call=HYEONGNIM)
        ctx = session_ctx(d)
        check("B8 block", "\n[Q3 derived candidates] Machine-derived" in ctx, ctx[-400:])
        check("B8 value", "1. %s 2. %s." % (GIVEN_NIM, HYEONGNIM) in ctx, ctx[-400:])
        check("B8 single block", ctx.count("[Q3 derived candidates] M") == 1)
        check("B8 no w3 duplicate block", "[q3 owner-name candidate] For" not in ctx)
    with tempfile.TemporaryDirectory() as d:
        mk(d, owner_name=None, leader_call=HYEONGNIM)
        ctx = session_ctx(d)
        check("B8 one source -> one candidate",
              "[Q3 derived candidates] Machine-derived" in ctx
              and "1. %s." % HYEONGNIM in ctx)
    with tempfile.TemporaryDirectory() as d:
        mk(d, owner_name=None)
        ctx = session_ctx(d)
        check("B8 no sources -> bare Q3",
              "[Q3 derived candidates] No address-term candidates" in ctx
              and "[Q3 derived candidates] Machine-derived" not in ctx)


def test_bridge_buttons():
    try:
        extract = options_parser()
    except Exception as e:  # pragma: no cover - environment
        _failures.append("bridge options import failed: %r" % e)
        return
    q3 = ("제가 어떻게 불러드리면 "
          "좋을까요?\n\n1. %s\n2. %s\n[[OPTIONS]]"
          % (GIVEN_NIM, HYEONGNIM))
    check("B8 two buttons", extract(q3) == [GIVEN_NIM, HYEONGNIM], extract(q3))
    q5 = "Q?\n\n" + "\n".join("%d. %d%%" % (i, p) for i, p in
                              enumerate((10, 30, 50, 70, 90), 1)) + "\n[[OPTIONS]]"
    check("B10 five buttons",
          extract(q5) == ["10%", "30%", "50%", "70%", "90%"], extract(q5))


# ---------------------------------------------------------------- B9 records

def tone_set():
    return {"ko": [
        {"label": "힘차고 격려하는 " + STYLE,
         "sample": "좋습니다 {address}, 한 세트만 "
                   "더 가요!"},
        {"label": "강하고 직접적인 " + STYLE, "sample": "a"},
        {"label": "따뜻하고 차분한 " + STYLE, "sample": "b"},
        {"label": "전문적이고 간결한 " + STYLE, "sample": "c"},
    ]}


def test_tone_block():
    with tempfile.TemporaryDirectory() as d:
        mk(d, tone=tone_set())
        ctx = session_ctx(d)
        check("B9 block", "[tone candidates -- from the installed pack pack_health]" in ctx)
        items = tone_set()["ko"]
        rows = ['%d. %s -- "%s"' % (i, it["label"], it["sample"])
                for i, it in enumerate(items, 1)]
        pos = [ctx.find(r) for r in rows]
        check("B9 verbatim rows", all(p >= 0 for p in pos), pos)
        check("B9 order", pos == sorted(pos), pos)
    with tempfile.TemporaryDirectory() as d:
        mk(d, lang="en", tone=tone_set())
        check("B9 lang mismatch -> generic",
              "[tone candidates -- from the installed pack pack_health]"
              not in session_ctx(d))
    bad = tone_set()
    bad["ko"] = bad["ko"][:2]
    with tempfile.TemporaryDirectory() as d:
        mk(d, tone=bad)
        check("B9 two items -> none", oc.resolve_tone_candidates({"cwd": d}, "ko") is None)
    bad = tone_set()
    bad["ko"][0]["extra"] = "x"
    with tempfile.TemporaryDirectory() as d:
        mk(d, tone=bad)
        check("B9 extra key -> none", oc.resolve_tone_candidates({"cwd": d}, "ko") is None)
    with tempfile.TemporaryDirectory() as d:
        mk(d)
        write(os.path.join(d, "config", "packs", "p.tone_candidates.json"), "{not json")
        check("B9 garbage -> none", oc.resolve_tone_candidates({"cwd": d}, "ko") is None)


def test_predicates():
    f = gate.reply_ends_on_question
    check("q mark", f("a\n어때요?"))
    check("options end", f("pick\n1. a\n2. b\n[[OPTIONS]]"))
    check("announcement", not f("같이 잡아 봐요."))
    check("list after question", f("고를까요?\n1. a\n2. b"))


# ---------------------------------------------------------------- baseline docs

def _resolve_template_dir():
    """Unminted template tree or None. DOGANY_TEMPLATE_DIR wins when it names
    a directory; else the tree this file ships in, unless it is a live
    instance (.instance.conf present) whose persona is already onboarded."""
    env = os.environ.get("DOGANY_TEMPLATE_DIR", "")
    if env:
        return os.path.abspath(env) if os.path.isdir(env) else None
    if os.path.exists(os.path.join(TEMPLATE, ".instance.conf")):
        return None
    return TEMPLATE


TEMPLATE_DIR = _resolve_template_dir()
SKILL = PERSONA = None
if TEMPLATE_DIR:
    SKILL = os.path.join(TEMPLATE_DIR, ".claude", "skills",
                         "dogany-user-onboarding", "SKILL.md")
    PERSONA = os.path.join(TEMPLATE_DIR, "identity", "hot.custom.agent.md")


def test_skill_persona_contract():
    """The two baseline instruction surfaces carry the same W3 contract as
    the hook context (applied with the w3-onboarding skill diff). Template
    only: skipped inside a live instance (DGN-1734)."""
    if not (SKILL and os.path.isfile(SKILL) and os.path.isfile(PERSONA)):
        print("SKIP DGN-1734: template SKILL/persona contract unavailable "
              "(live instance without DOGANY_TEMPLATE_DIR) -- NOT a pass")
        return
    for label, path in (("SKILL", SKILL), ("persona", PERSONA)):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for want in ("NO ORIGIN STORY", "[Q3 derived candidates]",
                     "[tone candidates -- from the installed", '"5. 90%"',
                     "ITEMIZED list"):
            check("%s has %r" % (label, want), want in text)
        for gone in ("health-trainer",
                     "\uc624\ub298 \ud6c8\ub828 \ube60\uc9c8"):
            check("%s lost %r" % (label, gone), gone not in text)
    with open(SKILL, encoding="utf-8") as f:
        check("SKILL handoff shape", "HANDOFF SHAPE (DGN-1632 W3)" in f.read())


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    if _failures:
        print("FAIL (%d)" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("OK test-dgn1632-w3-onboarding (%d tests)" % len(tests))


if __name__ == "__main__":
    main()
