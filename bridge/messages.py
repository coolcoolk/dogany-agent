"""Centralized user-facing strings.

All text shown to the end user lives here: intentionally-Korean UX strings and
English notices alike. Code identifiers and comments stay English; only the
string VALUES are user-facing. Keeping them in one module makes wording and
localization a single-file concern.
"""

# --- Access control ---
NO_PERMISSION = (
    "Sorry, you don't have permission to use this bot.\n"
    "Please contact the admin for access."
)
NO_PERMISSION_CALLBACK = "No permission to use this feature"

# --- Commands ---
WELCOME = "Hello, {name}! Send a message to start chatting, or use /skills to view available skills."
NEW_SESSION = (
    "Switched to new session mode. Your next message will start a new Claude session."
)
MODEL_SWITCHED = "Switched to {label}"
MODEL_SELECT = "Select Claude model:"
STOP_PAUSED = "Paused"
STOP_NOTHING = "Nothing running"
NO_SESSION = "No active session. Start a conversation first."

# --- Resume (session history) ---
NO_SESSION_HISTORY = "No session history found."
SESSION_HISTORY_HEADER = "Session History"
RESUME_HINT = "Reply with a number to switch to that session:"
RESUME_SWITCHED = "Switched to session: {msg}"
RESUME_INVALID_NUMBER = "Invalid number, please try again."

# --- History ---
NO_HISTORY = "No history available for this session."
HISTORY_HEADER = "Recent History (last 5 messages)"

# --- Queue / overflow ---
QUEUE_BUSY = "Processing previous messages, please wait or send /stop to terminate."

# --- Options keyboard ---
SELECT_PROMPT = "Please select:"
SELECTED = "Selected: {choice}"

# --- External file confirmation ---
EXTERNAL_FILE_PROMPT = (
    "File paths outside PROJECT_ROOT detected. Confirmation required before sending."
)
EXTERNAL_FILE_SEND = "Send external files"
EXTERNAL_FILE_CANCEL = "Cancel"
EXTERNAL_FILE_CANCELLED = "External file sending cancelled."
EXTERNAL_FILE_NONE = "No pending external files."
EXTERNAL_FILE_CONFIRMED = "Confirmed. Sending external files..."

# --- Timeout / resume (A4) ---
TIMEOUT_PAUSED = (
    "{timeout}초가 지나 한 번 끊었습니다. 이어서 진행하려면 아래 버튼을 누르세요."
)
TIMEOUT_NO_RESUME = (
    "타임아웃으로 작업이 멈췄는데, 이어갈 세션을 찾지 못했습니다. 요청을 다시 보내주세요."
)
TAP_TO_CONTINUE = "이어서 진행"
TIMEOUT_TAP_NOTICE = "타임아웃으로 멈췄습니다. 이어서 진행하려면 누르세요."
RESUME_EXPIRED = "이미 처리됐거나 만료된 버튼입니다. 필요하면 다시 요청해 주세요."
RESUME_CONTINUING = "이어서 진행합니다..."
STILL_WORKING = "시간이 좀 걸리네요 — 계속 진행 중입니다. 자동으로 이어갈 테니 잠시만요."
RESUME_FAILED = "이어가기 실패: {error}"

# A4 continuation prompt re-issued to Claude on resume (sent to the model, but it
# is Korean and user-influenced, so it lives here for consistency).
RESUME_CONTINUATION_PROMPT = (
    "직전 작업이 시간 제한으로 한 번 끊겼습니다. "
    "끊긴 지점부터 이어서 계속 진행해줘. "
    "처음부터 다시 하지 말고, 이미 끝낸 부분은 건너뛰고 남은 작업만 마무리해줘."
)

# --- Voice ---
VOICE_TOO_LONG = "Voice message is too long. Max duration is {seconds} seconds."
VOICE_DOWNLOAD_FAILED = "Failed to download your voice message. Please retry."
PHOTO_DOWNLOAD_FAILED = "사진을 받지 못했습니다. 다시 보내주세요."
DOC_DOWNLOAD_FAILED = "파일을 받지 못했습니다. 다시 보내주세요."
VOICE_CONVERT_FAILED = (
    "Failed to convert audio for transcription. "
    "Please ensure ffmpeg is installed and try again."
)
VOICE_UNAVAILABLE = (
    "Voice transcription is not configured (local whisper unavailable). "
    "Install faster-whisper."
)
VOICE_EMPTY = "No speech was detected in your voice message. Please try again."
VOICE_TRANSCRIBE_FAILED = "Failed to transcribe your voice message. Please try again later."

# --- Errors ---
INTERNAL_ERROR = "Internal error: {error}"
PROCESSING_FAILED = "Processing failed: {error}"
GENERIC_ERROR = "Sorry, an error occurred while processing your message.\nError: {error}"

# --- Outage / failure notices (DGN-045) ---
# Pushed when the polling watchdog reconnects after a network outage.
OUTAGE_RECOVERED = (
    "Reconnected to Telegram after about {minutes} min offline. "
    "Anything you sent during that window may have been missed - please resend if needed."
)
# Pushed when a no-pending (background/proactive) turn ends in an error
# (e.g. model overloaded / api_error after retries) and would otherwise be
# dropped silently because no assistant text was produced.
PROACTIVE_TURN_FAILED = (
    "A background turn ended without a reply (model overloaded or an API error after retries). "
    "Nothing was delivered - please ask again."
)

# --- System prompt fragment (sent to Claude, English on purpose) ---
SYSTEM_PROMPT = (
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
)

# Denial message returned to Claude when it tries AskUserQuestion.
ASK_USER_QUESTION_DENY = (
    "AskUserQuestion is not available in this environment. "
    "Do NOT mention this to the user. Instead, output the question followed by "
    "numbered options (1., 2., 3., ...), then STOP and WAIT for the user's choice. "
    "The system converts the numbered options into clickable buttons."
)

# Denial message returned to Claude when an out-of-root path is detected.
OUTSIDE_PATH_DENY = (
    "Detected access to paths outside PROJECT_ROOT. Requires confirmation.\n"
    "{preview}\n"
    "Output these two options to the user and wait for a reply:\n"
    "1. {allow_token} (Allow this external path access)\n"
    "2. {deny_token} (Deny)"
)
