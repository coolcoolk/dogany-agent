#!/usr/bin/env python3
"""
SessionStart hook (this repo's canonical copy -- .claude/settings.json calls it).

SessionStart hook: if the target agent's AGENT.md has the ONBOARDING_PENDING
marker (or AGENT.md is missing entirely), inject an "onboarding needed" signal
into the new session context. This nudges the agent to fill its own identity by
following the onboarding block at the top of AGENT.md plus the
dogany-user-onboarding skill. The question script is NOT kept here (single
source = the AGENT.md block).

Secondary signal (lifekit): onboarding complete AND config/lifekit.conf says
LIFEKIT=pending -> inject a one-shot "lifekit pending" offer context instead.
Onboarding always wins (never both signals in one session). This hook stays
READ-ONLY: the dogany-lifekit-setup skill flips pending -> offered, not us.

stdin(JSON): {session_id, transcript_path, cwd, source, ...}
stdout(JSON): {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": "..."}}
No output = onboarding not needed (no marker) or undecidable.

Safety: on any error, exit 0 silently. Never block SessionStart.
"""
import sys, os, json

MARKER = "<!-- ONBOARDING_PENDING -->"


def resolve_lang(data):
    """Read AGENT_LANG from config/agent.conf (en default). Any error -> 'en'."""
    try:
        cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
        conf = os.path.join(cwd, "config", "agent.conf")
        with open(conf, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("AGENT_LANG="):
                    return (line.split("=", 1)[1].strip() or "en").lower()
    except Exception:
        return "en"
    return "en"


# First-contact onboarding instruction, chosen by AGENT_LANG (en default; ko
# preserved). Same intent in both: ask one question at a time (name -> emoji ->
# form of address -> tone -> humor), do not self-name, do not presume any form
# of address. Add more locales by adding a key here.
ONBOARDING_CTX = {
    "en": (
        "[onboarding needed] You have just woken up as a brand-new assistant "
        "with nothing configured. You do not even have a name yet, so in your "
        "first reply do NOT refer to yourself by any specific name or persona. "
        "Follow the onboarding block at the top of AGENT.md and the "
        "dogany-user-onboarding skill: ask ONE question at a time (name -> emoji "
        "-> form of address -> tone -> humor -> role), waiting for each answer before "
        "the next. Keep every question clean and polite, short (no preamble, one "
        "or two sentences). For the emoji, AFTER the name is set, propose 3-4 "
        "candidate signature emojis that fit that name as a short numbered list "
        "(e.g. '1. \U0001F98A'), say the user can tap a button or send any emoji "
        "in chat, and end that message with the [[OPTIONS]] marker on its own "
        "last line (not 'should I use one?'). For humor, ask plainly like 'What "
        "percent should I set the humor to?' with no metaphors. Do NOT presume "
        "any form of address or preset label/title; until the user tells you, use no form of address at "
        "all. When asking, drop the object and phrase it naturally, e.g. 'What "
        "would you like me to call you?'. The LAST question (role) asks what "
        "role you are taking on, as a short numbered list ending with the "
        "[[OPTIONS]] marker (same pattern as the emoji question): '1. life "
        "assistant (schedule, appointments, career, general life management)', "
        "'2. an agent for a specific role'. Option 1 -> fill the Role "
        "section's Primary-focus slot with a life-assistant prose line; "
        "option 2 -> ask ONE follow-up ('What role should that be?') and fill "
        "the slot with the answer as ONE prose line. NEVER install skills, "
        "routines, or crons from this answer. Do NOT ask about answer format (already "
        "set by RULES Output). Fill the received answers directly into "
        "the identity fields of AGENT.md, and when done delete that onboarding "
        "block and the ONBOARDING_PENDING marker (the one-time unprompted "
        "baseline self-edit; later identity/Role edits happen only on the "
        "user's explicit request, per RULES)."
    ),
    "ko": (
        "[온보딩 필요] 아직 아무것도 설정되지 않은 새 비서로 처음 깨어났습니다. "
        "자기 이름조차 없으니 첫 응답에서 특정 이름/페르소나로 자칭하지 마세요. "
        "AGENT.md 최상단의 온보딩 블록과 dogany-user-onboarding 스킬을 따라, 질문을 한 번에 "
        "하나씩만(이름 -> 이모지 -> 호칭 -> 톤 -> 유머 -> 역할) 던지고 답을 받은 뒤 다음으로 넘어가세요. "
        "질문은 깔끔하고 공손하게, 짧게 하세요(서론·군더더기 없이 한두 문장). 이모지는 이름을 "
        "정한 뒤 그 이름에 어울리는 후보 3~4개를 짧은 번호 목록(예: '1. \U0001F98A')으로 제시하고, 버튼 "
        "선택 또는 채팅으로 자유 입력이 가능하다고 안내하며 마지막 줄에 [[OPTIONS]] 마커를 답니다 "
        "('쓸까요?'가 아님). 유머는 비유 없이 '유머 수치를 몇 %로 설정할까요?'처럼 바로 묻습니다. "
        "호칭은 미리 전제하지 말고(어떤 기본 라벨·호칭도 금지), 호칭을 듣기 전까진 어떤 "
        "호칭도 쓰지 마세요. 호칭을 물을 땐 '회원님'·'사용자' 같은 라벨 없이 목적어를 빼고 "
        "'제가 어떻게 부르면 좋을까요?'처럼 자연스럽게 물으세요. 마지막 질문(역할)은 '제가 맡을 역할이 "
        "뭘까요?'를 짧은 번호 목록으로 제시합니다(이모지 질문과 같은 패턴, 마지막 줄 [[OPTIONS]] 마커): "
        "'1. 생활 비서(일정·약속·커리어·기타 생활 관리)', '2. 특정 역할을 위한 에이전트'. "
        "1번이면 Role 섹션의 Primary-focus 슬롯에 생활 비서 프로즈 한 줄을 채우고, 2번이면 "
        "'어떤 역할일까요?' 후속 질문 하나를 던져 그 답을 프로즈 한 줄로 채웁니다. 이 답으로 스킬/루틴/크론을 "
        "절대 설치하지 마세요. 답변 형식(RULES Output)만 이미 정해졌으니 묻지 마세요. "
        "받은 답으로 AGENT.md의 정체성 필드를 직접 채우고, 다 끝나면 그 온보딩 "
        "블록과 ONBOARDING_PENDING 마커를 삭제하세요(1회성 자발 baseline 자가수정 -- 이후 정체성/Role 수정은 사용자가 명시 요청할 때만, RULES 참조)."
    ),
}


def resolve_target(data):
    env_path = os.environ.get("ONBOARDING_FILE")
    if env_path:
        return os.path.expanduser(env_path)
    cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    return os.path.join(cwd, "AGENT.md")


def needs_onboarding(path):
    if not os.path.isfile(path):
        return True
    try:
        with open(path, encoding="utf-8") as f:
            return MARKER in f.read()
    except Exception:
        return False


def resolve_lifekit_conf(data):
    env_path = os.environ.get("LIFEKIT_FILE")
    if env_path:
        return os.path.expanduser(env_path)
    cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    return os.path.join(cwd, "config", "lifekit.conf")


def lifekit_pending(path):
    """True iff lifekit.conf exists and LIFEKIT=pending (missing/other -> False)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("LIFEKIT="):
                    return line.split("=", 1)[1].strip() == "pending"
    except Exception:
        return False
    return False


def resolve_instance_conf(data):
    env_path = os.environ.get("INSTANCE_CONF_FILE")
    if env_path:
        return os.path.expanduser(env_path)
    cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    return os.path.join(cwd, ".instance.conf")


def instance_tier(path):
    """Tier from .instance.conf DOGANY_TIER. Missing file/field -> 'lite'
    (fail-closed to the free tier). Gates NEW activation offers only; it never
    touches an already-active lifekit."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DOGANY_TIER="):
                    return line.split("=", 1)[1].strip().lower() or "lite"
    except Exception:
        return "lite"
    return "lite"


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    if data.get("source") not in ("startup", "clear"):
        return

    try:
        path = resolve_target(data)
    except Exception:
        return

    try:
        onboarding = needs_onboarding(path)
    except Exception:
        return

    if not onboarding:
        # Onboarding done -> check the one-shot lifekit offer signal.
        try:
            if not lifekit_pending(resolve_lifekit_conf(data)):
                return
            # Tier gate: the lifekit bundle lives in the basic (CRAFT) tier
            # and up. On lite (HAND) never inject the offer. Activation-time
            # gate only -- an instance with LIFEKIT already on is untouched.
            if instance_tier(resolve_instance_conf(data)) == "lite":
                return
        except Exception:
            return
        ctx = (
            "[lifekit pending] User onboarding is complete but the lifekit "
            "(life-management) default bundle has not been offered yet "
            "(config/lifekit.conf LIFEKIT=pending). Once this session, at a "
            "natural moment (greeting or idle turn, never mid-task), offer the "
            "lifekit walkthrough via the dogany-lifekit-setup skill. Base the "
            "offer wording on the i18n key 'lifekit.offer' in "
            "config/i18n/<lang>.json. BEFORE presenting the offer, set "
            "LIFEKIT=offered in config/lifekit.conf so this signal never fires "
            "again (one-shot; the user can start anytime by asking). If the "
            "user declines for now, leave it as offered; if they say never, "
            "set LIFEKIT=off."
        )
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": ctx,
            }
        }
        try:
            print(json.dumps(out, ensure_ascii=False))
        except Exception:
            pass
        return

    lang = resolve_lang(data)
    ctx = ONBOARDING_CTX.get(lang, ONBOARDING_CTX["en"])
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }
    try:
        print(json.dumps(out, ensure_ascii=False))
    except Exception:
        return


if __name__ == "__main__":
    main()
