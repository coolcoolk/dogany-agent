#!/usr/bin/env python3
"""
SessionStart hook (this repo's canonical copy — .claude/settings.json calls it).

SessionStart hook: 대상 에이전트의 AGENT.md에 ONBOARDING_PENDING 마커가 있으면(또는
AGENT.md가 아예 없으면) "온보딩 필요" 신호를 새 세션 컨텍스트로 주입한다.
에이전트가 AGENT.md 최상단 온보딩 블록 + dogany-user-onboarding 스킬을 따라 스스로 정체성을
채우도록 유도한다. 질문 스크립트는 여기 두지 않는다(단일 소스 = AGENT.md 블록).

Secondary signal (lifekit): onboarding complete AND config/lifekit.conf says
LIFEKIT=pending -> inject a one-shot "lifekit pending" offer context instead.
Onboarding always wins (never both signals in one session). This hook stays
READ-ONLY: the dogany-lifekit-setup skill flips pending -> offered, not us.

stdin(JSON): {session_id, transcript_path, cwd, source, ...}
stdout(JSON): {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": "..."}}
출력 없음 = 온보딩 불필요(마커 없음) 또는 판정 불가.

안전장치: 어떤 에러든 조용히 exit 0. SessionStart를 절대 막지 않는다.
"""
import sys, os, json

MARKER = "<!-- ONBOARDING_PENDING -->"


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

    ctx = (
        "[온보딩 필요] 아직 아무것도 설정되지 않은 새 비서로 처음 깨어났습니다. "
        "자기 이름조차 없으니 첫 응답에서 특정 이름/페르소나로 자칭하지 마세요. "
        "AGENT.md 최상단의 온보딩 블록과 dogany-user-onboarding 스킬을 따라, 질문을 한 번에 "
        "하나씩만(이름 -> 이모지 -> 호칭 -> 톤 -> 유머) 던지고 답을 받은 뒤 다음으로 넘어가세요. "
        "질문은 깔끔하고 공손하게, 짧게 하세요(서론·군더더기 없이 한두 문장). 이모지는 이름을 "
        "정한 뒤 그 이름에 어울리는 후보 3~4개를 짧은 번호 목록(예: '1. 🦊')으로 제시하고, 버튼 "
        "선택 또는 채팅으로 자유 입력이 가능하다고 안내하며 마지막 줄에 [[OPTIONS]] 마커를 답니다 "
        "('쓸까요?'가 아님). 유머는 비유 없이 '유머 수치를 몇 %로 설정할까요?'처럼 바로 묻습니다. "
        "호칭은 미리 전제하지 말고(특히 '__USER_LABEL__'이라 부르지 말 것), 호칭을 듣기 전까진 어떤 "
        "호칭도 쓰지 마세요. 호칭을 물을 땐 '회원님'·'사용자' 같은 라벨 없이 목적어를 빼고 "
        "'제가 어떻게 부르면 좋을까요?'처럼 자연스럽게 물으세요. 답변 형식(RULES Output/notation)만 이미 정해졌으니 묻지 마세요. "
        "받은 답으로 AGENT.md의 정체성 필드를 직접 채우고, 다 끝나면 그 온보딩 "
        "블록과 ONBOARDING_PENDING 마커를 삭제하세요(이게 유일하게 허용된 baseline 자가수정)."
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
        return


if __name__ == "__main__":
    main()
