"""Korean string catalog.

One entry per messages.py constant, keyed by the snake_case of the constant
name. Values mirror en.STRINGS but in polite, concise Korean (product tone).
Every {placeholder}, command literal (e.g. /skills, /stop, /claim), and
code-like token (send_file::, PROJECT_ROOT, ffmpeg, faster-whisper) is preserved
verbatim -- only human-readable prose is translated.

Model-facing prompts (system_prompt, ask_user_question_deny, outside_path_deny)
are instructions sent to Claude, not shown to the user, so they stay English on
purpose to preserve model behavior.
"""

STRINGS = {
    # --- Access control ---
    "no_permission": (
        "죄송합니다. 이 봇을 사용할 권한이 없습니다.\n"
        "이용하시려면 관리자에게 문의해 주세요."
    ),
    "no_permission_callback": "이 기능을 사용할 권한이 없습니다",
    # --- Born-locked ownership / claim flow ---
    "claim_success": "이제 이 봇의 소유자가 되셨습니다.",
    "claim_code_log": (
        "CLAIM CODE: {code} -- 소유자가 되려면 텔레그램 계정에서 이 봇에게 "
        "'/claim {code}' 를 보내세요."
    ),
    "owner_lock_missing_log": (
        "owner.lock missing but instance already claimed; reclaim required"
    ),
    # --- Commands ---
    "welcome": (
        "안녕하세요, {name}님! 메시지를 보내 대화를 시작하시거나, /skills 로 사용 "
        "가능한 스킬을 확인하세요."
    ),
    "new_session": (
        "새 세션 모드로 전환했습니다. 다음 메시지부터 새로운 Claude 세션이 "
        "시작됩니다."
    ),
    "model_switched": "{label} (으)로 전환했습니다",
    "model_select": "Claude 모델을 선택하세요:",
    "stop_paused": "일시중지했습니다",
    "stop_nothing": "실행 중인 작업이 없습니다",
    "no_session": "활성 세션이 없습니다. 먼저 대화를 시작해 주세요.",
    # --- Resume (session history) ---
    "no_session_history": "세션 기록을 찾을 수 없습니다.",
    "session_history_header": "세션 기록",
    "resume_hint": "전환할 세션의 번호를 입력해 주세요:",
    "resume_switched": "세션으로 전환했습니다: {msg}",
    "resume_invalid_number": "잘못된 번호입니다. 다시 시도해 주세요.",
    # --- History ---
    "no_history": "이 세션에는 표시할 기록이 없습니다.",
    "history_header": "최근 기록 (최근 5개 메시지)",
    # --- Queue / overflow ---
    "queue_busy": (
        "이전 메시지를 처리하고 있습니다. 잠시 기다리시거나 /stop 으로 중단해 주세요."
    ),
    # --- Options keyboard ---
    "select_prompt": "선택해 주세요:",
    "selected": "선택함: {choice}",
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
    # --- Timeout / resume ---
    "timeout_paused": (
        "{timeout}초가 지나 한 번 끊었습니다. 이어서 진행하려면 아래 버튼을 "
        "누르세요."
    ),
    "timeout_no_resume": (
        "타임아웃으로 작업이 멈췄는데, 이어갈 세션을 찾지 못했습니다. 요청을 다시 "
        "보내주세요."
    ),
    "tap_to_continue": "이어서 진행",
    "timeout_tap_notice": "타임아웃으로 멈췄습니다. 이어서 진행하려면 누르세요.",
    "resume_expired": (
        "이미 처리됐거나 만료된 버튼입니다. 필요하면 다시 요청해 주세요."
    ),
    "resume_continuing": "이어서 진행합니다...",
    "still_working": (
        "시간이 좀 걸리네요 -- 계속 진행 중입니다. 자동으로 이어갈 테니 잠시만요."
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
    # --- Outage / failure notices ---
    "outage_recovered": (
        "약 {minutes}분간 오프라인이었다가 텔레그램에 다시 연결되었습니다. "
        "그동안 보내신 내용이 누락되었을 수 있으니 필요하면 다시 보내주세요."
    ),
    "proactive_turn_failed": (
        "백그라운드 작업이 응답 없이 종료되었습니다 (모델 과부하 또는 재시도 후 "
        "API 오류). 전달된 내용이 없으니 다시 요청해 주세요."
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
        "as documents. After generating a file, always include its send_file:: line."
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
}
