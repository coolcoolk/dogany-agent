"""Korean string catalog.

One entry per messages.py constant, keyed by the snake_case of the constant
name. Values mirror en.STRINGS but in polite, concise Korean (product tone).
Every {placeholder}, command literal (e.g. /skills, /stop, /claim), and
code-like token (send_file::, PROJECT_ROOT, ffmpeg, faster-whisper) is preserved
verbatim -- only human-readable prose is translated.

Model-facing prompts (system_prompt, output_lang_prompt, ask_user_question_deny,
outside_path_deny) are instructions sent to Claude, not shown to the user, so
they stay English on purpose to preserve model behavior.
"""

STRINGS = {
    # --- Access control ---
    "no_permission": (
        "죄송합니다. 이 봇을 사용할 권한이 없습니다.\n"
        "이용하시려면 관리자에게 문의해 주세요."
    ),
    "no_permission_callback": "이 기능을 사용할 권한이 없습니다",
    # --- Born-locked ownership / claim flow ---
    "claim_success": "이 봇의 소유자가 되셨습니다.",
    "claim_code_log": (
        "CLAIM CODE: {code} -- 소유자가 되려면 텔레그램 계정에서 이 봇에게 "
        "'/claim {code}' 를 보내세요."
    ),
    "owner_lock_missing_log": (
        "owner.lock missing but instance already claimed; reclaim required"
    ),
    # --- Commands ---
    "welcome": (
        "안녕하세요, {name}님! 메시지를 보내 대화를 시작해주세요."
    ),
    "new_session": (
        "새 세션으로 전환했습니다."
    ),
    "model_switched": "전환 완료: {label} · 새 세션으로 시작합니다",
    "model_select": (
         "Claude 모델을 선택하세요:\n"
         "(모델 전환 시 새 세션으로 시작됩니다)"
                     ),
    "model_switch_warning": "주의: 모델을 전환하면 새 세션이 시작됩니다.",
    "model_unknown": "알 수 없는 모델 '{name}'. 사용 가능한 모델: {allowed}",
    "model_state_fallback": "저장된 모델 설정을 읽지 못해 기본값으로 시작합니다.",
    # DGN-192: same-model guard -- switching to the already-active model is a
    # no-op (no session reset).
    "model_already_active": "이미 {label} 모델을 사용 중입니다.",
    "stop_paused": "마지막 작업을 멈췄습니다. 세션과 대화는 그대로입니다.",
    "stop_nothing": "멈출 작업이 없습니다.",
    "stop_interrupted": "진행하던 작업을 멈췄습니다. 세션과 대화는 그대로입니다.",
    # DGN-991 stopgap A (rev3): appended to stop_interrupted ONLY on the
    # /stop soft-success reply (the DGN-911 auto-interrupt notice reuses the
    # bare stop_interrupted and must not carry this). Measured mechanism: the
    # SDK interrupt control request makes the CLI abort the in-flight turn's
    # abort tree, so subagents running inside that turn die WITH the soft
    # stop -- the damage lands at the FIRST stop, not the second (2026-08-21
    # incident log: soft interrupt only, no hard kill, subagents dead). The
    # copy therefore states the background consequence NOW, plus the true
    # second-/stop escalation fact. No live-task registry exists, so no
    # count is stated. DRAFT copy -- owner confirmation pending (dec-094).
    "stop_bg_note": (
        "멈춘 작업이 띄운 백그라운드 작업도 함께 중단됩니다. "
        "이어가려면 새 메시지로 다시 지시해 주세요. "
        "/stop을 한 번 더 보내면 세션 프로세스를 강제 종료합니다."
    ),
    # DGN-991 stopgap B: hard-teardown result copy. The stop_paused claim
    # ("세션과 대화는 그대로") is false on this path -- the CLI subprocess is
    # killed and anything still alive inside it dies. DRAFT copy -- owner
    # confirmation pending (dec-094).
    "stop_forced": (
        "세션 프로세스를 강제 종료했습니다. "
        "그 안에 남아 있던 백그라운드 작업이 있다면 함께 종료됐습니다. "
        "대화 기록은 남아 있으니 새 메시지를 보내면 이어서 진행됩니다."
    ),
    "no_session": "활성 세션이 없습니다. 먼저 대화를 시작해 주세요.",
    "task_terminated": "작업을 종료했습니다.",
    # --- Help ---
    # DGN-919: command-list body is generated at render time from COMMAND_MENU_SPEC
    # in bot.py so the menu and help can never drift. Only the header and footer
    # remain as static i18n strings.
    "help_text_header": "사용 가능한 명령:",
    "help_text_footer": (
        "최초 설정: /start 로 봇을 시작하거나 /claim <code> 로 소유자가 되세요.\n"
        "임의의 /이름 을 보내면 해당 스킬이 실행됩니다.\n"
        "PROJECT_ROOT 바깥 파일 접근은 일회성 확인을 요청합니다."
    ),
    # --- Skills listing (read from SKILL.md frontmatter) ---
    "skills_none": "설치된 스킬이 없습니다.",
    "skills_header_project": "프로젝트 스킬",
    "skills_header_global": "전역 스킬",
    # --- BotCommand menu descriptions ---
    # DGN-919: copy locked by owner 2026-08-17. Keep in sync with en.py keys.
    "cmd_desc_new": "새 세션 시작",
    "cmd_desc_stop": "현재 작업 중단",
    "cmd_desc_btw": "대화 방해 없는 곁다리 질문",
    "cmd_desc_usage": "Claude 사용량·한도",
    "cmd_desc_queue": "메시지 큐에 넣기",
    "cmd_desc_model": "모델 전환",
    "cmd_desc_skills": "설치된 스킬 목록",
    "cmd_desc_resume": "이전 세션 이어가기",
    # DGN-759 + DGN-919: macOS-only; Linux uses a graceful no-op message.
    "cmd_desc_authsync": "자격증명 재동기화 (macOS)",
    "cmd_desc_help": "도움말",
    # --- /btw command (DGN-902) ---
    # 현재 세션 컨텍스트를 포크해 곁다리 단발 대화를 엽니다. 포크는 메인 세션
    # 히스토리를 읽지만 자체 새 세션에 기록하므로 메인 스레드가 오염되지 않습니다.
    "btw_marker": "\U0001f4ad (근데 있잖아)",
    "btw_no_question": "사용법: /btw <질문 내용>",
    "btw_no_session": "포크할 활성 세션이 없습니다. 먼저 대화를 시작해 주세요.",
    "btw_fork_failed": "곁다리 대화를 시작하지 못했습니다. 다시 시도해 주세요.",
    "btw_thinking": "생각 중...",
    # --- /authsync command (DGN-759) ---
    "authsync_running": "자격증명 동기화 상태를 확인합니다...",
    "authsync_match": "자격증명이 일치합니다 (MATCH). 동기화 불필요.",
    "authsync_mismatch_syncing": "자격증명 불일치 (MISMATCH). 키체인 동기화 중...",
    "authsync_sync_ok": (
        "동기화 완료 (MATCH 확인). 새 자격증명을 반영하려면 봇을 재시작해야 합니다."
    ),
    "authsync_sync_failed": "동기화 실패. 키체인을 직접 확인해 주세요.",
    "authsync_not_applicable": (
        "자격증명 동기화는 macOS 전용(키체인)입니다. "
        "이 호스트에서는 동작하지 않습니다."
    ),
    "authsync_script_missing": (
        "token-sync.sh 를 찾을 수 없습니다 "
        "(.claude/skills/dogany-relogin-rebind/ 에 있어야 합니다). "
        "dogany-relogin-rebind 스킬을 먼저 설치해 주세요."
    ),
    "authsync_error": "자격증명 동기화 오류: {error}",
    # DGN-994: restart CTA button on the sync-ok message. Short token only
    # (bridge.md label contract); user-facing lexicon fixed by DGN-990.
    "authsync_restart_button": "재시작",
    # --- Usage report (/usage -> routines/claude-usage.sh) ---
    "usage_script_missing": "사용량 스크립트를 찾을 수 없습니다 (routines/claude-usage.sh).",
    "usage_timeout": "사용량 조회가 시간 내에 끝나지 않았습니다. 잠시 후 다시 시도해 주세요.",
    "usage_failed": "사용량 조회 실패: {error}",
    # --- DGN-835: usage-defer manual retry (usageretry:<label> / /usageretry) ---
    # usage_retry_not_enough = 형님 승인 확정 문구 (2026-08-12). 나머지 엣지/오류
    # 문구(bad_label/no_replay/lookup_failed/usage/started)는 표준 i18n 오류
    # 레지스터를 따르며 잠금된 defer UX 본문에는 포함되지 않았다.
    "usage_retry_bad_label": "잘못된 재실행 요청입니다 (작업 라벨 형식 오류).",
    "usage_retry_no_replay": "재실행할 작업이 없습니다 (만료됐거나 이미 실행됨): {label}",
    "usage_retry_lookup_failed": "사용량 확인에 실패했습니다. 잠시 후 다시 시도해 주세요.",
    "usage_retry_not_enough": "아직 사용량이 부족해요 (현재 {pct}%, 리셋 {reset}). 잠시 뒤 다시 눌러 주세요.",
    "usage_retry_started": "미뤄둔 작업을 실행했습니다: {label}",
    "usage_retry_usage": "사용법: /usageretry <작업 라벨> -- 대기 알림에 적힌 라벨을 그대로 입력해 주세요.",
    # --- Transient countdown (DGN-594; UI redesign DGN-780; free-form DGN-780b) ---
    # Icon + draining bar carry the "remaining" meaning; no word. The icon is a
    # placeholder (default hourglass/check resolved in bridge/countdown.py; a
    # caller may override it).
    "countdown_body": "{icon} {label}  {remaining}  {bar}",
    "countdown_done": "{done_icon} {label} 완료",
    # DGN-915: completion-affordance button label (shown on the done message).
    "countdown_done_button": "다음 ▶",
    # --- Resume (session history) ---
    "no_session_history": "세션 기록을 찾을 수 없습니다.",
    "session_history_header": "세션 기록",
    "resume_hint": "전환할 세션의 번호를 입력해 주세요:",
    "resume_switched": "세션으로 전환했습니다: {msg}",
    "resume_invalid_number": "잘못된 번호입니다. 다시 시도해 주세요.",
    # --- Queue / overflow (DGN-616) ---
    # In-flight messages now COALESCE into the running turn; this notice fires
    # only at the memory-safety cap (a runaway flood of buffered messages).
    "queue_busy": (
        "대기 중인 메시지가 너무 많이 쌓였습니다. 잠시 기다리시거나 /stop 으로 중단해 주세요."
    ),
    # --- /queue command (DGN-911) ---
    "queue_usage": "사용법: /queue <메시지 내용>",
    # --- Slash command usage ---
    "usage_skill": "사용법: /skill <name> [args]",
    "usage_command": "사용법: /command <name> [args]",
    # --- Inbound photo / document prompts (sent to Claude) ---
    "photo_prompt_single": (
        "사용자가 사진을 보냈습니다. 아래 경로의 이미지 파일을 Read 도구로 열어서 "
        "내용을 확인하고 응답하세요."
    ),
    "photo_prompt_path": "이미지 경로: {path}",
    "photo_prompt_album": (
        "사용자가 사진 {count}장을 한 번에(앨범) 보냈습니다. 아래 경로의 이미지 파일들을 "
        "모두 Read 도구로 열어 함께 보고 하나의 응답으로 답하세요."
    ),
    "photo_prompt_album_path": "이미지 {index} 경로: {path}",
    "doc_prompt": (
        "사용자가 파일을 보냈습니다. 아래 경로의 파일을 Read 도구로 열어서 "
        "내용을 확인하고 응답하세요."
    ),
    "doc_prompt_path": "파일 경로: {path}",
    "user_caption": "사용자 캡션: {caption}",
    # --- Options keyboard ---
    "select_prompt": "선택해 주세요:",
    "selected": "선택: {choice}",
    # DGN-881: button label overflow fallback -- localized number handle.
    "option_number_handle": "{n}번",
    # DGN-881: recommendation marker SSOT. No code path reads this key; the
    # output contract docs (bridge.md / telegram.md) reference it as the
    # canonical body-side marker next to the recommended option line.
    "option_rec_marker": "(추천)",
    # --- External file confirmation ---
    "external_file_prompt": (
        "PROJECT_ROOT 바깥의 파일 경로가 감지되었습니다. 전송하려면 확인이 "
        "필요합니다."
    ),
    "external_file_send": "외부 파일 전송",
    "external_file_cancel": "취소",
    "external_file_cancelled": "외부 파일 전송을 취소했습니다.",
    "external_file_none": "대기 중인 외부 파일이 없습니다.",
    "external_file_confirmed": "확인했습니다. 외부 파일을 전송합니다...",
    "external_file_omitted_noninteractive": (
        "작업 폴더(PROJECT_ROOT) 바깥 파일이 있었지만, 확인을 받을 수 없는 "
        "발송 경로라서 보내지 않았습니다. 필요하시면 채팅으로 다시 요청해주세요."
    ),
    # --- Timeout / resume ---
    "timeout_paused": (
        "{timeout}초가 지나 한 번 끊었습니다. 이어서 진행하려면 아래 버튼을 "
        "누르세요."
    ),
    "timeout_no_resume": (
        "타임아웃으로 작업이 멈췄는데, 이어갈 세션을 찾지 못했습니다. 요청을 다시 "
        "보내주세요."
    ),
    "tap_to_continue": "이어서 진행하기",
    "timeout_tap_notice": "타임아웃으로 멈췄습니다. 이어서 진행하려면 누르세요.",
    "resume_expired": (
        "이미 처리됐거나 만료된 버튼입니다. 다시 요청해 주세요."
    ),
    "resume_continuing": "이어서 진행합니다...",
    "still_working": (
        "시간이 좀 걸리고 있습니다. 자동으로 계속 진행 중입니다."
    ),
    "resume_failed": "이어가기 실패: {error}",
    "resume_continuation_prompt": (
        "직전 작업이 시간 제한으로 한 번 끊겼습니다. "
        "끊긴 지점부터 이어서 계속 진행해줘. "
        "처음부터 다시 하지 말고, 이미 끝낸 부분은 건너뛰고 남은 작업만 마무리해줘."
    ),
    # --- Voice ---
    "voice_too_long": "음성 메시지가 너무 깁니다. 최대 길이는 {seconds}초입니다.",
    "voice_download_failed": "음성 메시지를 받지 못했습니다. 다시 시도해 주세요.",
    "photo_download_failed": "사진을 받지 못했습니다. 다시 보내주세요.",
    "doc_download_failed": "파일을 받지 못했습니다. 다시 보내주세요.",
    "voice_convert_failed": (
        "음성 변환에 실패했습니다. ffmpeg 가 설치되어 있는지 확인한 뒤 다시 "
        "시도해 주세요."
    ),
    "voice_unavailable": (
        "음성 인식이 설정되어 있지 않습니다 (로컬 whisper 를 사용할 수 없습니다). "
        "faster-whisper 를 설치해 주세요."
    ),
    "voice_empty": "음성 메시지에서 말소리를 인식하지 못했습니다. 다시 시도해 주세요.",
    "voice_transcribe_failed": (
        "음성 메시지를 텍스트로 변환하지 못했습니다. 잠시 후 다시 시도해 주세요."
    ),
    # --- Errors ---
    "internal_error": "내부 오류: {error}",
    "processing_failed": "처리 실패: {error}",
    "generic_error": (
        "죄송합니다. 메시지를 처리하는 중 오류가 발생했습니다.\n오류: {error}"
    ),
    "network_timeout": "네트워크 연결이 잠시 불안정했습니다. 잠시 후 다시 시도해 주세요.",
    # --- DGN-686: is_error result notices (LOCKED copy -- do not reword) ---
    "error_transient_retry": "일시적으로 처리에 실패했어요. 다시 시도할까요?",
    "error_auth_relogin": "클로드에 다시 로그인 하신 후 알려주시면 복구하겠습니다.",
    "error_generic_retry": "처리 실패했어요. 다시 시도할까요?",
    "error_retry_button": "다시 시도",
    "error_retrying": "다시 시도하고 있어요...",
    "error_retry_expired": "다시 시도 요청이 만료됐어요. 메시지를 다시 보내주세요.",
    # --- File send failure (send_file:: retry exhausted) ---
    # DGN-649: reason-specific variants; "send_file_failed" fires only for
    # network-classified failures now.
    "send_file_failed": (
        "파일 전송에 실패했습니다: '{filename}' (네트워크 오류). "
        "잠시 후 다시 요청해 주세요."
    ),
    "send_file_failed_dimensions": (
        "이미지 전송에 실패했습니다: '{filename}' (이미지 치수가 텔레그램 "
        "사진 한도 초과). 다시 요청해 주세요."
    ),
    "send_file_failed_too_large": (
        "파일 전송에 실패했습니다: '{filename}' (파일 용량이 전송 한도 초과)."
    ),
    "send_file_failed_api": (
        "파일 전송에 실패했습니다: '{filename}' (텔레그램 전송 오류). "
        "잠시 후 다시 요청해 주세요."
    ),
    # --- Growing-fold captions/markers (DGN-699; moved from formatting.py
    # by DGN-851). LOCKED copy (owner A-case 2026-08-02 14:27) -- do not
    # edit without an owner gate.
    "fold_caption_normal": "진행 기록",
    "fold_caption_stopped": "중단됨 · 진행 기록",
    "fold_caption_timeout": "시간 초과 · 진행 기록",
    "fold_truncation_line": "…(생략)",
    "fold_omission_line": "⋯ 중략 ⋯",
    # --- Outage / failure notices ---
    # (outage_recovered removed by DGN-851: the recovery push was disabled
    #  per owner request 2026-06-30 -- bot._notify_outage_recovered only
    #  logs -- so the copy was dead weight. Re-enabling means restoring the
    #  key here + in en + a real send in the bot callback.)
    "proactive_turn_failed": (
        "백그라운드 작업이 응답 없이 종료되었습니다 (모델 과부하 또는 재시도 후 "
        "API 오류). 전달된 내용이 없으니 다시 요청해 주세요."
    ),
    # --- Subagent placeholder-flake recovery (DGN-670) ---
    # Model-facing retry preamble (English on purpose, identical to en catalog).
    # Must contain no Korean flake vocabulary (never self-trigger the
    # placeholder-flake regex on the retry turn).
    "flake_retry_prefix": (
        "[BRIDGE FLAKE RETRY / DGN-670] Your previous reply was a delegation "
        "placeholder ('subagent working / waiting for completion'), not a "
        "result. You are the direct executor. Do not delegate and do not wait "
        "for another agent. If the work was in fact already completed, report "
        "the actual result now; do NOT redo side-effectful steps. The original "
        "request follows -- complete it and output the real result:\n\n"
    ),
    # User-facing notice when the single flake retry also failed.
    "flake_recovery_failed": (
        "작업 결과를 받지 못했습니다 (실행 확인 2회 실패). "
        "같은 요청을 다시 보내 주시면 재시도하겠습니다."
    ),
    # --- Turn-death safety net (DGN-163) ---
    # 소비된 인바운드 업데이트가 아무 출력도 못 내는 경우를 막는 안전망:
    # 업데이트 접수와 첫 사용자 응답 사이의 모든 예외에서 발화. 원시 트레이스백
    # 없이 짧게.
    "turn_failed": (
        "메시지를 처리하는 중 문제가 발생해 처리되지 않았습니다. "
        "다시 보내거나 재시도해 주세요."
    ),
    "turn_failed_photo": (
        "사진을 받지 못해 메시지가 처리되지 않았습니다. 다시 보내주세요."
    ),
    "turn_failed_document": (
        "파일을 받지 못해 메시지가 처리되지 않았습니다. 다시 보내주세요."
    ),
    "turn_failed_voice": (
        "음성 메시지를 받지 못해 처리되지 않았습니다. 다시 보내주세요."
    ),
    # DGN-801: fast-path exit0 push failed after retries. State IS committed
    # (exit0 = commit witness), so never claim the input was lost and never
    # re-run it -- only the screen update failed. Domain-neutral wording on
    # purpose (the bridge does not know what the handler recorded).
    "fastpath_push_failed": (
        "입력은 기록됐지만 화면 갱신에 실패했습니다. "
        "다음 응답에서 최신 상태를 확인해 주세요."
    ),
    # 일부 출력이 이미 스트리밍된 뒤 턴이 죽은 경우의 변형: 누락됐다고 하지 말고
    # 응답이 잘렸을 수 있음을 알린다.
    "turn_incomplete": (
        "이 응답은 중간에 끊겼을 수 있습니다. 이어서 진행을 요청하거나 "
        "빠진 부분이 있으면 다시 보내주세요."
    ),
    # --- System prompt fragment (sent to Claude, English on purpose) ---
    "system_prompt": (
        "\n\n## User Questions and Choices\n\n"
        "The AskUserQuestion tool is NOT available in this environment. "
        "When you need to ask the user a question with multiple choice options:\n"
        "1. Output the question and context clearly\n"
        "2. List options with numbers (1., 2., 3., ...)\n"
        "3. STOP and WAIT for the user's response\n"
        "4. Do NOT continue execution or make assumptions\n"
        "5. Do NOT try to use the AskUserQuestion tool\n\n"
        "## Sending Images and Files\n\n"
        "When the user asks you to send/show/deliver an image or file, do NOT read it "
        "with the Read tool. Instead, output a line that starts with 'send_file::' "
        "followed by the absolute path. One file per line. The system detects these "
        "lines and sends the files to the user.\n"
        "Example: send_file:: /path/to/image.png\n"
        "Supported image formats: .png, .jpg, .jpeg, .gif, .webp; other files are sent "
        "as documents. After generating a file, always include its send_file:: line.\n\n"
        "## Subagent Task Delegation (DGN-086)\n\n"
        "When you delegate work using the Task tool, the subagent prompt MUST include "
        "this line verbatim at the top:\n"
        "\"You are the direct executor of this task. You MUST perform the work "
        "yourself using the available tools. Do NOT delegate, defer, or report that "
        "you are waiting for another agent. Do NOT output placeholder messages like "
        "'working in background' or 'waiting for completion'. Complete the task "
        "directly and output the result.\"\n"
        "If a subagent returns a placeholder response (e.g. 'still working', "
        "'waiting for completion notice', 'background agent running') instead of "
        "actual results, that is a flake. Send a follow-up message telling it: "
        "\"You are the executor. Do not delegate. Execute the task directly now "
        "and output the result.\""
    ),
    # --- Output-language rule appended to the system prompt (DGN-429 hybrid
    # leg 1; sent to Claude, English on purpose). {language} = the human name
    # of the configured locale (Korean/English). ---
    "output_lang_prompt": (
        "\n\n## Output Language\n\n"
        "The user's configured language is {language}. Every user-facing "
        "reply, including the final answer of a long tool-using turn, MUST be "
        "written in {language}. Instructions, skill documents, and tool output "
        "you work with are internal working material in English -- never let "
        "that working register leak into the reply you send the user. Code, "
        "commands, identifiers, and proper nouns may stay as-is."
    ),
    # --- Denials returned to Claude (English on purpose) ---
    "ask_user_question_deny": (
        "AskUserQuestion is not available in this environment. "
        "Do NOT mention this to the user. Instead, output the question followed by "
        "numbered options (1., 2., 3., ...), then STOP and WAIT for the user's choice. "
        "The system converts the numbered options into clickable buttons."
    ),
    "outside_path_deny": (
        "Detected access to paths outside PROJECT_ROOT. Requires confirmation.\n"
        "{preview}\n"
        "Output these two options to the user and wait for a reply:\n"
        "1. {allow_token} (Allow this external path access)\n"
        "2. {deny_token} (Deny)"
    ),
    "outside_path_deny_no_confirm": (
        "Access to a protected or out-of-root path was denied. This is a "
        "background turn with no user available to confirm it. Skip this path or "
        "ask the user directly in their next message."
    ),
    # --- DGN-918: idrill in-place drilldown callback ---
    # Positional tokens on purpose: {1} = step1 value (the bridge knows no
    # domain meaning). Rendered via .format("", val1) so {1} resolves.
    "idrill_arm_expired": "만료된 버튼입니다. 다시 시도해 주세요.",
    "idrill_error": "기록 중 오류가 발생했습니다. 다시 시도해 주세요.",
    "idrill_other_prompt": "값을 직접 입력해 주세요.",
    "idrill_logged_default": "✅ {1} 기록됨",
    "idrill_logged_default_skip": "✅ {1} 기록됨 (2단계 생략)",
    # --- DGN-939: N-step back navigation ---
    # Shown when a Back tap underflows past the first step (no crash).
    "idrill_nav_root": "처음 단계입니다.",
    # Default label for the auto-injected Back button (arm may override via
    # step_back_label). Kept short for the inline button width.
    "idrill_back_label": "« 뒤로",
}

# ---------------------------------------------------------------------------
# Skill display-name catalog (DGN-102)
#
# Keys = immutable skill folder IDs (same keys as en.SKILL_DISPLAY_NAMES).
# Values = user-facing Korean labels. skill_display_name() in
# bridge/i18n/__init__.py resolves these with en fallback.
# ---------------------------------------------------------------------------
SKILL_DISPLAY_NAMES = {
    # --- Framework / dogany-* skills ---
    "dogany-cron-register":   "반복 일정 등록",
    "dogany-lifekit-setup":   "생활 관리 설정",
    "dogany-mailer":          "메일러",
    "dogany-memory-search":   "기억 검색",
    "dogany-proactive-push":  "선제적 알림",
    "dogany-reminder":        "리마인더",
    "dogany-skill-creator":   "스킬 제작",
    "dogany-user-onboarding": "사용자 온보딩",
    # --- Lifekit bundle skills ---
    "diet-log":         "식단 기록",
    "workout-log":      "운동 기록",
    "appointment-log":  "약속 관리",
    "relationship":     "관계 관리",
    "task-update":      "할 일 관리",
    "spending-log":     "소비 기록",
}
