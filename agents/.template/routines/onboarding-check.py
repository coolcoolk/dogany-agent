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

Tertiary signal (portfolio): onboarding complete AND the lifekit signal did
NOT fire this session AND config/agent.conf says PORTFOLIO=pending -> inject
a one-shot "portfolio pending" offer context. At most ONE signal per session
(onboarding > lifekit > portfolio) so a fresh mint never chains two offers at
first contact. Portfolio is TIER-FREE (owner ruling dec-035): no tier gate --
a lite instance whose lifekit offer is tier-suppressed still gets the
portfolio offer. READ-ONLY here too: dogany-portfolio-setup flips the state.

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
        "YOUR FIRST MESSAGE must be a greeting + one-line self-intro + the Q1 name ask -- "
        "ALL IN ONE MESSAGE. Never send the greeting alone and wait silently. "
        "Check own AGENT.md Role section 'Primary focus' slot: if it already holds a real "
        "domain role (not the placeholder text starting with '(set at onboarding'), introduce "
        "yourself using that role: e.g. 'Hi, I'm your new <role> agent. Nice to meet you!' "
        "If the slot is still the placeholder (general agent), use a generic intro: "
        "e.g. 'Hi, I'm your new assistant. Nice to meet you!' Keep the intro to 1-2 sentences. "
        "Do NOT name yourself -- you have no name yet. Do NOT use any form of address -- "
        "none until the user tells you. "
        "NO INTERNAL NARRATION: user-facing output during onboarding is ONLY greeting + "
        "self-intro + questions + short confirmations. NEVER narrate internal state, file "
        "names, markers, or checklists to the user (e.g. 'Checked AGENT.md', "
        "'ONBOARDING_PENDING marker found', status dumps). All internal checks are silent. "
        "ALWAYS ASK ALL 5 IDENTITY QUESTIONS in order: Q1(name) -> Q2(emoji) -> "
        "Q3(address term) -> Q4(tone) -> Q5(humor). A field that appears to carry a "
        "pre-set value does NOT skip its question -- confirm it via the normal question "
        "instead. "
        "Q6 (role) IS CONDITIONAL, not one of the always-5 (DGN-227 A3 conditional "
        "retention). Discriminator = the Role 'Primary focus' slot placeholder text "
        "'(set at onboarding -- one prose line naming the main hat...'. Placeholder ABSENT "
        "-> role was already stamped at install (A3 fills the slot AND excises Q6 on all "
        "three paths); DO NOT ask Q6, DO NOT re-ask it. Placeholder PRESENT -> an old "
        "un-stamped instance; ask Q6 so the slot gets filled. This same placeholder test "
        "is the SINGLE discriminator across the AGENT.md onboarding block, the "
        "dogany-user-onboarding SKILL.md, and this hook -- they must agree. "
        "Immediately after the intro (still in the same first message), ask Q1: what would "
        "you like to name me? Then follow the onboarding block at the top of AGENT.md and the "
        "dogany-user-onboarding skill: ask ONE question at a time starting from Q2 (emoji "
        "-> form of address -> tone -> humor -> role), waiting for each answer before "
        "the next. Keep every question clean and polite, short (no preamble, one "
        "or two sentences). For the emoji (Q2), AFTER the name is set: if this is a "
        "DOMAIN agent (Primary focus holds a real role), propose 4 candidates -- "
        "2 role-related and 2 name-related -- as a short numbered list "
        "(e.g. '1. \U0001F98A'). If this is a GENERAL agent (Primary focus still "
        "placeholder), propose 3-4 name-related candidates (unchanged). In both cases, "
        "say the user can tap a button or send any emoji in chat, and end that message "
        "with the [[OPTIONS]] marker on its own last line (not 'should I use one?'). "
        "For the tone question (Q4): LABEL FORMAT RULE -- tone candidate labels must use "
        "'<adjective> style' phrasing in the instance language; NEVER use the Korean '-형' "
        "suffix form (e.g. NOT '간결형 / 친근형'; YES '간결한 스타일' / '따뜻하고 친근한 스타일'). "
        "UI: present 3-5 numbered tone-style candidates as a short numbered list; put the "
        "[[OPTIONS]] marker on its own LAST LINE (same pattern as Q2). Always state that "
        "free-text input is also accepted. Candidates must span a useful range and be few "
        "and distinct. DOMAIN agent (Primary focus holds a real role): 3-4 candidates "
        "tailored to that role. Example for a health-trainer agent: 'strong and direct style', "
        "'warm and encouraging style', 'professional and concise style'. GENERAL agent "
        "(Primary focus still placeholder): 4 generic candidates: "
        "'1. concise and dry style', '2. warm and friendly style', "
        "'3. polite and formal style', '4. casual and playful style'. "
        "For humor, ask plainly like 'What percent should I set the humor to?' with no "
        "metaphors. Do NOT presume any form of address or preset label/title; until the "
        "user tells you, use no form of address at all. When asking (Q3), follow the "
        "AGENT.md onboarding block wording (single source): ONE short natural sentence, "
        "omitting the object label entirely. Never use generic labels (member/user). The LAST "
        "question (role, Q6) is CONDITIONAL -- ask it ONLY IF the Primary-focus placeholder "
        "is still present (see the Q6 conditional rule above; a stamped slot means install "
        "already set the role at A3 -> skip Q6, complete with the 5 filled). When asked, "
        "the role question asks what role you are taking on, as a short numbered list "
        "ending with the [[OPTIONS]] marker (same pattern as the emoji question): "
        "'1. life assistant (schedule, appointments, career, general life management)', "
        "'2. an agent for a specific role'. Option 1 -> fill the Role "
        "section's Primary-focus slot with a life-assistant prose line; "
        "option 2 -> ask ONE follow-up ('What role should that be?') and fill "
        "the slot with the answer as ONE prose line. NEVER install skills, "
        "routines, or crons from this answer. Do NOT ask about answer format (already "
        "set by RULES Output). Fill the received answers directly into "
        "the identity fields of AGENT.md, and when done delete that onboarding "
        "block and the ONBOARDING_PENDING marker (the one-time unprompted "
        "baseline self-edit; later identity/Role edits happen only on the "
        "user's explicit request, per RULES). "
        "BRIEFING-TIME STEP (DGN-227 A3 / DGN-420 seam) -- AFTER deleting the block, "
        "BEFORE the completion message, and ONLY IF this instance has generic-brief units "
        "(test: routines/*generic-brief-morning.plist exists -- a domain standalone agent; "
        "main agents keep their lifekit briefing and skip this): (1) ask ONE combined "
        "question for the three briefing times, stating the defaults and that they may skip "
        "to accept: morning (default 07:00), retro (default 22:00), weekly (default Sunday "
        "20:00); free-text HH:MM (24h), weekly accepts '<Day> HH:MM'. (2) apply "
        "deterministically (no model math): run "
        "bash <own-root>/routines/set-briefing-times.sh --root <own-root> "
        "[--morning HH:MM] [--retro HH:MM] [--weekly \"<Day> HH:MM\"] -- pass only the "
        "slots the user changed; skip = run with no time flags (writes defaults). This ONE "
        "script writes BRIEF_TIME_MORNING/RETRO/WEEKLY into config/agent.conf AND "
        "regenerates the generic-brief plist StartCalendarInterval; never hand-edit config "
        "or plists. (3) confirm in one line; on script error, log silently and continue. "
        "COMPLETION MESSAGE (send immediately after the briefing-time step, or right after "
        "deleting the block when that step's gate is false): "
        "(1) Echo the confirmed settings (name, emoji, address term, tone, humor) in 1-2 lines. "
        "(2) Declare immediate effect: say you will interact this way FROM NOW -- NEVER say "
        "'from the next session' (identity is injected every turn; that framing is false). "
        "(3) Branch by agent type: "
        "DOMAIN agent (Primary focus filled with a real role): decide the sub-path from "
        "the ACTUAL integration config, never from the role stamp alone (DGN-284 #3) -- "
        "silently read own config/agent.conf for MIGRATION_PEER; when MIGRATION_PEER is "
        "absent, fall back to the legacy HANDOFF_PEER_AG key (pre-DGN-227 mints). "
        "NEVER read HANDOFF_PEER_MAIN for this decision -- it is a briefing-topology "
        "key, not a migration key (DGN-227 E2-1). "
        "DOMAIN agent MIGRATION path (the migration key IS set -- minted from a main agent "
        "with existing records to migrate): "
        "NO [[OPTIONS]] menu. Instead: "
        "(a) ATTEMPT the migration request (deterministic, no model): derive own slug "
        "from the workspace directory name. If "
        "routines/lib/handoff/handoff_cli.py exists in own root, run: "
        "  python3 <own-root>/routines/lib/handoff/handoff_cli.py submit "
        "  --to-root <MIGRATION_PEER value (legacy: HANDOFF_PEER_AG)> --from <own-slug> --to ag "
        "  --type migration.request "
        "  --payload-json '{\"domain\":\"<role domain>\",\"target_root\":\"<own root>\"}' "
        "where <role domain> is the single-word domain keyword for this agent's Primary "
        "focus (e.g. 'health' for a health/fitness agent). Fire-and-forget only -- do "
        "NOT block the completion message on the result. Log any error silently. "
        "(b) Tell the user: 'Migration has been requested from the main agent -- once "
        "the setup is done, I will proactively offer the first consultation.' "
        "FALLBACK (migration key set but handoff_cli.py absent -- package partially "
        "applied): skip step (a); send instead: "
        "'Go back to the main agent room and the migration of existing records will "
        "continue.' (use the main agent's name if known, else generic 'main agent') "
        "DOMAIN agent FRESH path (no migration key: MIGRATION_PEER absent AND legacy "
        "HANDOFF_PEER_AG absent -- standalone/direct mint, no "
        "data to migrate; NEVER mention migration or a main agent on this path): "
        "numbered list ending with [[OPTIONS]] on its own last line: "
        "'1. See what I can do for you' "
        "'2. Start recording right now' "
        "'3. Start a goal consultation'. "
        "GENERAL agent (life assistant or placeholder): "
        "numbered list ending with [[OPTIONS]] on its own last line: "
        "'1. See what I can do for you' "
        "'2. (domain-appropriate quick start, e.g. Get today's schedule briefing for a "
        "life-assistant agent; adapt to the filled role if known)'. "
        "FORBIDDEN: closing with 'Just tell me anything' alone -- applies to all branches "
        "except DOMAIN migration-path, which ends with the two guidance lines above."
    ),
    "ko": (
        "[온보딩 필요] 아직 아무것도 설정되지 않은 새 에이전트로 처음 깨어났습니다. "
        "자기 이름조차 없으니 첫 응답에서 특정 이름/페르소나로 자칭하지 마세요. "
        "첫 메시지는 인사 + 한 줄 자기소개 + Q1 이름 질문을 하나의 메시지에 모두 담아야 합니다. "
        "인사만 보내고 기다리는 것은 금지입니다. "
        "본인 AGENT.md의 Role 섹션 'Primary focus' 슬롯을 확인하세요: "
        "'(set at onboarding...'으로 시작하는 플레이스홀더가 아닌 실제 도메인 역할이 이미 채워져 있으면, "
        "그 역할을 사용해 자기소개하세요: 예) '안녕하세요, 새로 온 <역할> 에이전트입니다. 잘 부탁드립니다!' "
        "슬롯이 아직 플레이스홀더(일반 에이전트)이면 일반 소개를 쓰세요: "
        "예) '안녕하세요, 새로 온 에이전트입니다. 잘 부탁드립니다!' "
        "1~2문장만, 이름 없이(아직 없음), 호칭 없이(아직 없음). "
        "내부 상태 서술 금지: 온보딩 중 유저에게 보내는 출력은 오직 인사 + 자기소개 + 질문 + 짧은 확인 "
        "뿐입니다. AGENT.md 확인 완료, ONBOARDING_PENDING 마커 있음, 현재 상태: ... 같은 내부 상태·파일명·"
        "마커·체크리스트를 절대 서술하지 마세요. 모든 내부 점검은 조용히 수행합니다. "
        "정체성 5개 질문은 반드시 모두 순서대로 물어봐야 합니다: Q1(이름) -> Q2(이모지) -> Q3(호칭) -> "
        "Q4(톤) -> Q5(유머). 필드에 미리 채워진 값처럼 보이는 것이 있어도 질문을 건너뛰지 마세요 -- "
        "해당 질문을 그대로 물어서 사용자에게 확인받으세요. "
        "Q6(역할)은 조건부입니다 -- 5개 고정 질문에 포함되지 않습니다 (DGN-227 A3 조건부 유지). "
        "판별자 = Role 'Primary focus' 슬롯의 플레이스홀더 '(set at onboarding -- one prose line "
        "naming the main hat...'. 플레이스홀더 부재 -> 설치가 A3에서 이미 역할을 스탬프함 (세 경로 "
        "모두 슬롯을 채우고 Q6를 절제); Q6를 묻지도 다시 묻지도 마세요. 플레이스홀더 존재 -> 구 "
        "미스탬프 인스턴스; 슬롯을 채우도록 Q6를 물으세요. 이 플레이스홀더 테스트는 AGENT.md "
        "온보딩 블록, dogany-user-onboarding SKILL.md, 이 훅 3사본의 단일 판별자 -- 서로 일치해야 합니다. "
        "자기소개 직후(같은 첫 메시지 안에서) Q1을 바로 물어보세요: 저를 어떻게 부를까요? (이름 질문). "
        "그 다음부터 AGENT.md 최상단의 온보딩 블록과 dogany-user-onboarding 스킬을 따라, Q2부터 질문을 한 번에 "
        "하나씩만(이모지 -> 호칭 -> 톤 -> 유머 -> 역할) 던지고 답을 받은 뒤 다음으로 넘어가세요. "
        "질문은 깔끔하고 공손하게, 짧게 하세요(서론·군더더기 없이 한두 문장). "
        "이모지(Q2)는 이름을 정한 뒤: 도메인 에이전트(Primary focus에 실제 역할이 있음)이면 후보 4개 -- "
        "역할 연관 2개 + 이름 연관 2개 -- 를 짧은 번호 목록(예: '1. \U0001F98A')으로 제시하세요. "
        "일반 에이전트(Primary focus가 아직 플레이스홀더)이면 이름 연관 후보 3~4개를 제시합니다(기존 동작 유지). "
        "두 경우 모두, 버튼 선택 또는 채팅으로 자유 입력이 가능하다고 안내하며 마지막 줄에 [[OPTIONS]] 마커를 답니다 "
        "('쓸까요?'가 아님). 톤 질문(Q4): 라벨 형식 규칙 -- 톤 후보 라벨은 '<형용사> 스타일' 형식을 써야 합니다; "
        "절대 '-형' 접미어 형식을 쓰지 마세요 (예: '간결형 / 친근형' 금지; '간결한 스타일' / "
        "'따뜻하고 친근한 스타일' 사용). UI: 3~5개 번호 목록으로 후보를 제시하고 마지막 줄에 "
        "[[OPTIONS]] 마커를 붙입니다 (Q2와 같은 패턴). 자유 입력도 가능하다고 항상 안내합니다. "
        "후보는 범위가 넓고 서로 뚜렷하게 구분돼야 합니다. 도메인 에이전트(Primary focus에 "
        "실제 역할 있음): 해당 역할에 맞는 후보 3~4개. 헬스트레이너 에이전트 예시: "
        "'강하고 직접적인 스타일', '따뜻하고 격려하는 스타일', '전문적이고 간결한 스타일'. "
        "일반 에이전트(Primary focus가 아직 플레이스홀더): 일반 후보 4개: "
        "'1. 간결하고 담백한 스타일', '2. 따뜻하고 친근한 스타일', "
        "'3. 공손하고 격식 있는 스타일', '4. 편안하고 유쾌한 스타일'. "
        "유머는 비유 없이 '유머 수치를 몇 %로 설정할까요?'처럼 바로 묻습니다. "
        "호칭은 미리 전제하지 말고(어떤 기본 라벨·호칭도 금지), 호칭을 듣기 전까진 어떤 "
        "호칭도 쓰지 마세요. 호칭 질문(Q3)은 AGENT.md 온보딩 블록의 문안(단일 소스)을 따르세요: "
        "목적어 라벨 없이 자연스러운 한 문장으로만 묻습니다. "
        "'회원님'·'사용자' 같은 라벨은 절대 쓰지 마세요. 마지막 질문(역할, Q6)은 조건부입니다 -- "
        "Primary-focus 플레이스홀더가 아직 남아 있을 때만 물으세요 (위 Q6 조건부 규칙 참조; 슬롯이 "
        "이미 스탬프됐으면 설치가 A3에서 역할을 정한 것 -> Q6 생략, 5개 채우면 완료). 물을 때는 "
        "'제가 맡을 역할이 뭘까요?'를 짧은 번호 목록으로 제시합니다(이모지 질문과 같은 패턴, 마지막 줄 [[OPTIONS]] 마커): "
        "'1. 생활 비서(일정·약속·커리어·기타 생활 관리)', '2. 특정 역할을 위한 에이전트'. "
        "1번이면 Role 섹션의 Primary-focus 슬롯에 생활 비서 프로즈 한 줄을 채우고, 2번이면 "
        "'어떤 역할일까요?' 후속 질문 하나를 던져 그 답을 프로즈 한 줄로 채웁니다. 이 답으로 스킬/루틴/크론을 "
        "절대 설치하지 마세요. 답변 형식(RULES Output)만 이미 정해졌으니 묻지 마세요. "
        "받은 답으로 AGENT.md의 정체성 필드를 직접 채우고, 다 끝나면 그 온보딩 "
        "블록과 ONBOARDING_PENDING 마커를 삭제하세요(1회성 자발 baseline 자가수정 -- 이후 정체성/Role 수정은 사용자가 명시 요청할 때만, RULES 참조). "
        "브리핑 시각 스텝 (DGN-227 A3 / DGN-420 seam) -- 블록 삭제 후, 완료 메시지 전에, "
        "generic-brief 유닛이 있을 때만 (테스트: routines/*generic-brief-morning.plist 존재 -- "
        "도메인 standalone 에이전트; 메인 에이전트는 lifekit 브리핑을 유지하므로 이 스텝을 건너뜀): "
        "(1) 세 브리핑 시각을 한 질문으로 묻되, 기본값과 건너뛰면 기본값 적용됨을 안내: "
        "모닝(기본 07:00), 회고(기본 22:00), 주간(기본 일요일 20:00); 자유 입력 HH:MM(24시간), "
        "주간은 '<요일> HH:MM' 형식. (2) 결정론적으로 적용(모델 계산 없음): "
        "bash <own-root>/routines/set-briefing-times.sh --root <own-root> "
        "[--morning HH:MM] [--retro HH:MM] [--weekly \"<Day> HH:MM\"] -- 유저가 바꾼 슬롯만 "
        "전달; 건너뜀 = 시각 플래그 없이 실행(기본값 기록). 이 스크립트 하나가 "
        "BRIEF_TIME_MORNING/RETRO/WEEKLY를 config/agent.conf에 기록하고 generic-brief plist의 "
        "StartCalendarInterval을 재생성함; config나 plist를 손으로 편집하지 마세요. "
        "(3) 설정 결과를 한 줄로 확인; 스크립트 오류는 조용히 로그하고 계속 진행. "
        "완료 메시지 (브리핑 시각 스텝 직후, 그 스텝 게이트가 거짓이면 블록 삭제 직후 즉시 발송): "
        "(1) 확정된 설정(이름, 이모지, 호칭, 톤, 유머)을 1~2줄로 에코. "
        "(2) 즉시 적용 선언: '지금부터 이렇게 대화하겠습니다.' 절대 '다음 세션부터'라고 쓰지 마세요 "
        "(정체성은 매 턴 주입됨 -- 다음 세션부터 프레이밍은 거짓). "
        "(3) 에이전트 유형별 분기: "
        "도메인 에이전트(Primary focus에 실제 역할이 채워짐): 하위 경로는 역할 스탬프가 아니라 "
        "실제 통합 config로 판정한다(DGN-284 #3) -- 본인 config/agent.conf의 MIGRATION_PEER를 "
        "조용히 확인 (MIGRATION_PEER 부재 시 레거시 HANDOFF_PEER_AG로 폴백 -- DGN-227 이전 민팅 보호). "
        "HANDOFF_PEER_MAIN은 절대 이 판정에 쓰지 않는다 -- 브리핑 라우팅 키이지 이관 키가 아니다 (DGN-227 E2-1). "
        "도메인 에이전트 -- 마이그레이션 경로(이관 키가 설정됨 -- 메인 에이전트에서 "
        "민팅됐고 이관할 기존 기록이 있음): "
        "[[OPTIONS]] 메뉴 없음. 대신: "
        "(a) 마이그레이션 요청 시도 (결정론적, 모델 없음): 워크스페이스 디렉토리 이름으로 "
        "슬러그를 도출. routines/lib/handoff/handoff_cli.py가 있으면 실행: "
        "  python3 <own-root>/routines/lib/handoff/handoff_cli.py submit "
        "  --to-root <MIGRATION_PEER value (legacy: HANDOFF_PEER_AG)> --from <own-slug> --to ag "
        "  --type migration.request "
        "  --payload-json '{\"domain\":\"<role domain>\",\"target_root\":\"<own root>\"}' "
        "여기서 <role domain>은 이 에이전트의 Primary focus 도메인 키워드 한 단어 "
        "(예: 헬스/피트니스 에이전트는 'health'). 결과를 기다리지 말 것 -- 완료 메시지를 "
        "블로킹하지 않는다. 오류는 조용히 로그. "
        "(b) 유저에게 전달: '이관을 메인 에이전트에게 요청해뒀어요 -- 정리가 끝나면 "
        "제가 먼저 첫 상담을 제안드릴게요' "
        "폴백 (이관 키는 있으나 handoff_cli.py 없음 -- 패키지 부분 적용): "
        "단계 (a) 건너뜀, 대신 안내: "
        "'아그(메인 에이전트) 방으로 돌아가면 기존 기록 이관이 이어집니다.' "
        "(메인 에이전트 이름을 아는 경우 사용, 모르면 '메인 에이전트') "
        "도메인 에이전트 -- 신규 직접 민팅 경로(이관 키 없음: MIGRATION_PEER도 레거시 HANDOFF_PEER_AG도 부재 -- 독립 인스턴스, 이관할 "
        "데이터 없음; 이 경로에서는 이관·메인 에이전트를 절대 언급하지 않는다): "
        "마지막 줄에 [[OPTIONS]] 마커를 단 번호 목록: "
        "'1. 제가 뭘 해드릴 수 있는지 보기' "
        "'2. 바로 기록 시작하기' "
        "'3. 목표 상담 시작하기'. "
        "일반 에이전트(생활 비서 또는 플레이스홀더): "
        "마지막 줄에 [[OPTIONS]] 마커를 단 번호 목록: "
        "'1. 제가 뭘 해드릴 수 있는지 보기' "
        "'2. (역할 맞춤 빠른 시작 -- 예: 생활 비서이면 \"오늘 일정 브리핑 받아보기\"; 채워진 역할에 맞게 조정)'. "
        "금지: '무엇이든 말씀해 주세요'만으로 마무리 금지 -- 도메인 마이그레이션 경로(위 두 줄로 마무리)를 제외한 "
        "모든 분기에 적용."
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


def resolve_agent_conf(data):
    env_path = os.environ.get("AGENT_CONF_FILE")
    if env_path:
        return os.path.expanduser(env_path)
    cwd = (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    return os.path.join(cwd, "config", "agent.conf")


def portfolio_pending(path):
    """True iff agent.conf exists and PORTFOLIO=pending (missing/other -> False).
    Key absent (pre-portfolio instance) -> False: existing estates are never
    auto-offered; soft migration starts only when the user asks."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PORTFOLIO="):
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
        # Onboarding done -> at most ONE offer signal per session, in fixed
        # order: lifekit first, portfolio second (never both -- a fresh mint
        # must not chain two offers at first contact).
        ctx = None
        try:
            # Lifekit: pending AND tier gate passes (the bundle lives in the
            # basic/CRAFT tier and up; lite never gets the lifekit offer).
            # Activation-time gate only -- LIFEKIT already on is untouched.
            if (lifekit_pending(resolve_lifekit_conf(data))
                    and instance_tier(resolve_instance_conf(data)) != "lite"):
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
            # Portfolio: TIER-FREE (owner ruling dec-035). Fires only when the
            # lifekit signal did not fire this session.
            elif portfolio_pending(resolve_agent_conf(data)):
                ctx = (
                    "[portfolio pending] User onboarding is complete but the "
                    "portfolio index offer has not been made yet "
                    "(config/agent.conf PORTFOLIO=pending). Once this session, at a "
                    "natural moment (greeting or idle turn, never mid-task), offer the "
                    "portfolio walkthrough via the dogany-portfolio-setup skill. Base "
                    "the offer wording on the i18n key 'portfolio.offer' in "
                    "config/i18n/<lang>.json. BEFORE presenting the offer, set "
                    "PORTFOLIO=offered in config/agent.conf so this signal never fires "
                    "again (one-shot; the user can start anytime by asking). If the "
                    "user declines for now, leave it as offered; if they say never, "
                    "set PORTFOLIO=off. NEVER pre-create an index file -- the file is "
                    "written only on explicit opt-in with a chosen profile."
                )
        except Exception:
            return
        if ctx is None:
            return
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
