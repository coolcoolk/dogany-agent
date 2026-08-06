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
    "model_switched": "Switched to {label} · new session started",
    "model_select": (
        "Select Claude model:\n"
        "(switching starts a new session)"
    ),
    "model_switch_warning": (
        "Note: switching the model starts a fresh conversation."
    ),
    "model_unknown": (
        "Unknown model '{name}'. Allowed models: {allowed}"
    ),
    "model_state_fallback": (
        "Saved model preference was unreadable; using the default instead."
    ),
    # DGN-192: same-model guard -- switching to the already-active model is a
    # no-op (no session reset).
    "model_already_active": "Already using {label}.",
    "stop_paused": "Stopped the current task -- your session stays.",
    "stop_nothing": "Nothing to stop.",
    "stop_interrupted": (
        "Interrupted. The current turn was stopped in place; the session "
        "stays alive and queued messages were dropped."
    ),
    "no_session": "No active session. Start a conversation first.",
    "task_terminated": "Task terminated.",
    # --- Help ---
    "help_text": (
        "Available commands:\n"
        "/start - Start / greeting\n"
        "/new - Start a new session\n"
        "/stop - Stop the current run\n"
        "/model - Switch model\n"
        "/resume - Resume a previous session\n"
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
    "cmd_desc_stop": "Stop the current task",
    "cmd_desc_kill": "Kill session (hard stop)",
    "cmd_desc_model": "Switch model",
    "cmd_desc_resume": "Resume session",
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
    # --- Queue / overflow (DGN-616) ---
    # In-flight messages now COALESCE into the running turn; this notice fires
    # only at the memory-safety cap (a runaway flood of buffered messages).
    "queue_busy": "Too many messages are queued up. Please wait or send /stop to terminate.",
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
    "network_timeout": "Network connection timed out. Please try again shortly.",
    # --- DGN-686: is_error result notices ---
    "error_transient_retry": "Processing failed temporarily. Try again?",
    "error_auth_relogin": (
        "Please log in to Claude again, then let me know and I'll recover."
    ),
    "error_generic_retry": "Processing failed. Try again?",
    "error_retry_button": "Retry",
    "error_retrying": "Retrying...",
    "error_retry_expired": "The retry request expired. Please send your message again.",
    # --- File send failure (send_file:: retry exhausted) ---
    # DGN-649: reason-specific variants; "send_file_failed" fires only for
    # network-classified failures now.
    "send_file_failed": (
        "Warning: failed to send file '{filename}' (network error). "
        "Please try again in a moment."
    ),
    "send_file_failed_dimensions": (
        "Warning: failed to send image '{filename}' (image dimensions exceed "
        "Telegram's photo limit). Please ask again."
    ),
    "send_file_failed_too_large": (
        "Warning: failed to send file '{filename}' (file size exceeds the "
        "transfer limit)."
    ),
    "send_file_failed_api": (
        "Warning: failed to send file '{filename}' (Telegram delivery error). "
        "Please try again in a moment."
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
    # --- Subagent placeholder-flake recovery (DGN-670) ---
    # Model-facing retry preamble (English on purpose, identical in ko catalog).
    # The bridge appends the ORIGINAL user message verbatim after this prefix.
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
    # User-facing notice when the single flake retry also failed. Honest
    # failure wording, no internals (no placeholder/flake jargon).
    "flake_recovery_failed": (
        "The task ended without a usable result (execution check failed "
        "twice). Please resend the request to retry."
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
    "spending-log":     "Spending Log",
}
