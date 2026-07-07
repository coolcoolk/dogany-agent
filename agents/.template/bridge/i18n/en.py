"""English string catalog.

One entry per messages.py constant, keyed by the snake_case of the constant
name. Values are RAW templates: any {placeholder}, command literal (e.g.
/skills), or code-like token is preserved verbatim so call sites can .format().
This is the canonical fallback catalog: every key MUST exist here.
"""

STRINGS = {
    # --- Access control ---
    "no_permission": (
        "Sorry, you don't have permission to use this bot.\n"
        "Please contact the admin for access."
    ),
    "no_permission_callback": "No permission to use this feature",
    # --- Born-locked ownership / claim flow ---
    "claim_success": "You are now the owner of this bot.",
    "claim_code_log": (
        "CLAIM CODE: {code} -- send '/claim {code}' to this bot from your Telegram "
        "account to become the owner."
    ),
    "owner_lock_missing_log": (
        "owner.lock missing but instance already claimed; reclaim required"
    ),
    # --- Commands ---
    "welcome": (
        "Hello, {name}! Send a message to start chatting, or use /skills to view "
        "available skills."
    ),
    "new_session": (
        "Switched to new session mode. Your next message will start a new Claude "
        "session."
    ),
    "model_switched": "Switched to {label}",
    "model_select": "Select Claude model:",
    "model_switch_warning": (
        "Note: switching the model starts a fresh conversation."
    ),
    "model_unknown": (
        "Unknown model '{name}'. Allowed models: {allowed}"
    ),
    "model_state_fallback": (
        "Saved model preference was unreadable; using the default instead."
    ),
    "stop_paused": "Paused",
    "stop_nothing": "Nothing running",
    "no_session": "No active session. Start a conversation first.",
    "task_terminated": "Task terminated.",
    # --- Help ---
    "help_text": (
        "Available commands:\n"
        "/start - Start / greeting\n"
        "/new - Start a new session\n"
        "/stop - Stop the current run\n"
        "/model - Switch model (restarts the conversation)\n"
        "/resume - Resume a previous session\n"
        "/history - Show recent history\n"
        "/skills - List installed skills\n"
        "/usage - Claude usage / limits\n"
        "/help - Show this help\n\n"
        "Any /name runs the matching skill.\n"
        "First-time setup: send /claim <code> to become the owner. "
        "File access outside PROJECT_ROOT asks for one-time confirmation."
    ),
    # --- Skills listing (read from SKILL.md frontmatter) ---
    "skills_none": "No skills installed.",
    "skills_header_project": "Project skills",
    "skills_header_global": "Global skills",
    # --- BotCommand menu descriptions ---
    "cmd_desc_new": "New session",
    "cmd_desc_stop": "Stop execution",
    "cmd_desc_model": "Switch model",
    "cmd_desc_resume": "Resume session",
    "cmd_desc_history": "View message history",
    "cmd_desc_skills": "List skills",
    "cmd_desc_usage": "Claude usage / limits",
    "cmd_desc_help": "Show help",
    # --- Usage report (/usage -> routines/claude-usage.sh) ---
    "usage_script_missing": "Usage script not found (routines/claude-usage.sh).",
    "usage_timeout": "The usage lookup did not finish in time. Please try again shortly.",
    "usage_failed": "Usage lookup failed: {error}",
    # --- Resume (session history) ---
    "no_session_history": "No session history found.",
    "session_history_header": "Session History",
    "resume_hint": "Reply with a number to switch to that session:",
    "resume_switched": "Switched to session: {msg}",
    "resume_invalid_number": "Invalid number, please try again.",
    # --- History ---
    "no_history": "No history available for this session.",
    "history_header": "Recent History (last 5 messages)",
    # --- Queue / overflow ---
    "queue_busy": "Processing previous messages, please wait or send /stop to terminate.",
    # --- Slash command usage ---
    "usage_skill": "Usage: /skill <name> [args]",
    "usage_command": "Usage: /command <name> [args]",
    # --- Inbound photo / document prompts (sent to Claude) ---
    "photo_prompt_single": (
        "The user sent a photo. Open the image file at the path below with the Read "
        "tool, review it, and respond."
    ),
    "photo_prompt_path": "Image path: {path}",
    "photo_prompt_album": (
        "The user sent {count} photos at once (an album). Open all image files at "
        "the paths below with the Read tool, review them together, and answer with a "
        "single response."
    ),
    "photo_prompt_album_path": "Image {index} path: {path}",
    "doc_prompt": (
        "The user sent a file. Open the file at the path below with the Read tool, "
        "review it, and respond."
    ),
    "doc_prompt_path": "File path: {path}",
    "user_caption": "User caption: {caption}",
    # --- Options keyboard ---
    "select_prompt": "Please select:",
    "selected": "Selected: {choice}",
    # --- External file confirmation ---
    "external_file_prompt": (
        "File paths outside PROJECT_ROOT detected. Confirmation required before "
        "sending."
    ),
    "external_file_send": "Send external files",
    "external_file_cancel": "Cancel",
    "external_file_cancelled": "External file sending cancelled.",
    "external_file_none": "No pending external files.",
    "external_file_confirmed": "Confirmed. Sending external files...",
    # --- Timeout / resume ---
    "timeout_paused": (
        "Paused after {timeout} seconds. Tap the button below to continue."
    ),
    "timeout_no_resume": (
        "Work stopped on timeout, but no session was found to resume. "
        "Please send your request again."
    ),
    "tap_to_continue": "Continue",
    "timeout_tap_notice": "Stopped on timeout. Tap to continue.",
    "resume_expired": (
        "This button was already handled or has expired. Please request again if "
        "needed."
    ),
    "resume_continuing": "Continuing...",
    "still_working": (
        "This is taking a little while -- still working. I'll continue "
        "automatically, one moment."
    ),
    "resume_failed": "Resume failed: {error}",
    "resume_continuation_prompt": (
        "The previous task was interrupted once by a time limit. "
        "Continue from where it stopped. "
        "Do not start over; skip what is already done and finish only the "
        "remaining work."
    ),
    # --- Voice ---
    "voice_too_long": "Voice message is too long. Max duration is {seconds} seconds.",
    "voice_download_failed": "Failed to download your voice message. Please retry.",
    "photo_download_failed": "Failed to receive the photo. Please send it again.",
    "doc_download_failed": "Failed to receive the file. Please send it again.",
    "voice_convert_failed": (
        "Failed to convert audio for transcription. "
        "Please ensure ffmpeg is installed and try again."
    ),
    "voice_unavailable": (
        "Voice transcription is not configured (local whisper unavailable). "
        "Install faster-whisper."
    ),
    "voice_empty": "No speech was detected in your voice message. Please try again.",
    "voice_transcribe_failed": (
        "Failed to transcribe your voice message. Please try again later."
    ),
    # --- Errors ---
    "internal_error": "Internal error: {error}",
    "processing_failed": "Processing failed: {error}",
    "generic_error": (
        "Sorry, an error occurred while processing your message.\nError: {error}"
    ),
    # --- Outage / failure notices ---
    "outage_recovered": (
        "Reconnected to Telegram after about {minutes} min offline. "
        "Anything you sent during that window may have been missed - please resend "
        "if needed."
    ),
    "proactive_turn_failed": (
        "A background turn ended without a reply (model overloaded or an API error "
        "after retries). Nothing was delivered - please ask again."
    ),
    # --- Turn-death safety net (DGN-163) ---
    # Fired when a consumed inbound update would otherwise produce zero output:
    # any exception between "update accepted" and the first user-visible reply.
    # Bounded prose, never a raw traceback.
    "turn_failed": (
        "Something went wrong handling that message - it was not processed. "
        "Please resend or try again."
    ),
    "turn_failed_photo": (
        "Couldn't download the photo, so the message was not processed. "
        "Please send it again."
    ),
    "turn_failed_document": (
        "Couldn't download the file, so the message was not processed. "
        "Please send it again."
    ),
    "turn_failed_voice": (
        "Couldn't download the voice message, so it was not processed. "
        "Please send it again."
    ),
    # Variant when partial output already streamed before the turn died: do not
    # claim the message was dropped, warn the visible reply may be cut short.
    "turn_incomplete": (
        "That reply may be incomplete - the turn ended early. "
        "Ask me to continue or resend if anything is missing."
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
    "outside_path_deny_no_confirm": (
        "Access to a protected or out-of-root path was denied. This is a "
        "background turn with no user available to confirm it. Skip this path or "
        "ask the user directly in their next message."
    ),
}

# ---------------------------------------------------------------------------
# Skill display-name catalog (DGN-102)
#
# Keys = immutable skill folder IDs. Values = user-facing Title Case labels in
# this locale. skill_display_name() in bridge/i18n/__init__.py resolves these;
# fall-back is the raw ID when a key is absent (fail-open, never KeyError).
# ---------------------------------------------------------------------------
SKILL_DISPLAY_NAMES = {
    # --- Framework / dogany-* skills ---
    "dogany-cron-register":   "Cron Register",
    "dogany-lifekit-setup":   "Life Management Setup",
    "dogany-mailer":          "Mailer",
    "dogany-memory-search":   "Memory Search",
    "dogany-proactive-push":  "Proactive Push",
    "dogany-reminder":        "Reminder",
    "dogany-skill-creator":   "Skill Creator",
    "dogany-user-onboarding": "User Onboarding",
    # --- Lifekit bundle skills ---
    "diet-log":         "Diet Log",
    "workout-log":      "Workout Log",
    "appointment-log":  "Appointment Manager",
    "relationship":     "Relationship Manager",
    "task-update":      "Task Manager",
}
