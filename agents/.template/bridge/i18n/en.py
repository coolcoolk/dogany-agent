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
    "stop_interrupted": "Stopped what was running. Your session and conversation are intact.",
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
        "Background work started by the stopped task ends with it. "
        "To pick it up again, send a new message with the instruction. "
        "One more /stop force-quits the session process."
    ),
    # DGN-1015: fact-based, fires only when a background subagent actually
    # died. Copy OWNER-APPROVED 2026-08-24 (commit f67bf57e): the draft's
    # developer vocabulary was rejected -- the reader just typed a message
    # and does not know an interrupt happened.
    "bg_subagent_killed_notice": "⚠️ {names} stopped.",
    # DGN-1016 O1: dedicated copy for the AUTOMATIC in-flight interrupt
    # notice (BRIDGE_INFLIGHT_INTERRUPT_NOTICE opt-in; default stays OFF =
    # silence, owner decision 2026-08-17). The flag previously reused
    # stop_interrupted -- /stop copy for an action the user never took.
    # Same vocabulary rule as the DGN-1015 approval above: no developer
    # terms. DRAFT copy -- owner confirmation pending; the flag must not
    # ship enabled before this line is approved.
    "auto_interrupt_notice": (
        "Paused what I was doing to handle your new message."
    ),
    # DGN-991 stopgap B: hard-teardown result copy. The stop_paused claim
    # ("session stays") is false on this path -- the CLI subprocess is killed
    # and anything still alive inside it dies. DRAFT copy -- owner
    # confirmation pending (dec-094).
    "stop_forced": (
        "Force-stopped the session process. Any background work still alive "
        "inside it has ended with it. Your conversation history is intact -- "
        "send a new message to continue."
    ),
    "no_session": "No active session. Start a conversation first.",
    "task_terminated": "Task terminated.",
    # --- Help ---
    # DGN-919: command-list body is generated at render time from COMMAND_MENU_SPEC
    # in bot.py so the menu and help can never drift. Only the header and footer
    # remain as static i18n strings.
    "help_text_header": "Available commands:",
    "help_text_footer": (
        "First-time setup: use /start to greet the bot or /claim <code> to become the owner.\n"
        "Any /name runs the matching skill.\n"
        "File access outside PROJECT_ROOT asks for one-time confirmation."
    ),
    # --- Skills listing (read from SKILL.md frontmatter) ---
    "skills_none": "No skills installed.",
    "skills_header_project": "Project skills",
    "skills_header_global": "Global skills",
    # --- BotCommand menu descriptions ---
    # DGN-919: copy locked by owner 2026-08-17. Keep in sync with ko.py keys.
    "cmd_desc_new": "Start a new session",
    "cmd_desc_stop": "Stop the current task",
    "cmd_desc_btw": "Aside question, no disruption to the chat",
    "cmd_desc_usage": "Claude usage & limits",
    "cmd_desc_queue": "Queue a message",
    "cmd_desc_model": "Switch model",
    "cmd_desc_skills": "Installed skills",
    "cmd_desc_resume": "Resume a previous session",
    # DGN-1050: cmd_desc_authsync removed -- /authsync retired, off-menu.
    # DGN-997: owner-only explicit restart command.
    "cmd_desc_restart": "Restart the bot",
    # DGN-986 D1: owner amendment of the 10-command lock -- /health sits
    # right before help (authsync, the D1 anchor, was retired by DGN-1050
    # after D1 was decided -- see COMMAND_MENU_SPEC comment). Copy = dec-094
    # UX-gate item (draft).
    "cmd_desc_health": "Agent & estate health check",
    "cmd_desc_help": "Help",
    # --- /health command (DGN-986 [c]) ---
    "health_failed": "Health check failed: {error}",
    # --- /btw command (DGN-902) ---
    # Fork the current session context into an ephemeral side conversation.
    # The fork reads the full main session history but writes to its own new
    # session, keeping the main thread uncontaminated.
    "btw_marker": "\U0001f4ad btw",
    "btw_no_question": "Usage: /btw <your question>",
    "btw_no_session": "No active session to fork from. Start a conversation first.",
    "btw_fork_failed": "Could not start a side conversation. Please try again.",
    "btw_thinking": "Thinking...",
    # --- /authsync (RETIRED, DGN-1050) ---
    # The DGN-759 sync strings are gone with the poisonous file->keychain
    # sync path (it re-injected superseded refresh tokens after CLI runtime
    # rotations -> server-side token-family revocation -> estate-wide auth
    # death). Only the retirement notice remains; the hidden stub handler
    # replies with it. Copy status: 미확정 (형님 확인 대기, dec-094 gate).
    "authsync_retired": (
        "/authsync has been retired (DGN-1050): its file-to-keychain sync "
        "was what kept revoking the credentials. The CLI manages credential "
        "refresh by itself; a stale credentials file is normal. To re-login, "
        "run `claude auth login` in a terminal, then /restart."
    ),
    # --- /restart command (DGN-997) ---
    # Success path has no new copy (self_restart.sh owns the completion
    # push). This is the failure-path fallback only.
    "restart_error": "Restart error: {error}",
    # DGN-997 dec-094: immediate ack on the success path only -- self_restart.sh
    # still owns the completion push separately (no duplicate notice).
    "restart_ack": "Restarting -- back shortly.",
    # DGN-1010 layer-2 backstop: fires ONLY when the self_restart.sh worker
    # died before its terminal push (marker unclaimed + worker pid dead); the
    # new bridge terminal-closes the restart instead. Copy status: proposed,
    # owner approval pending.
    "restart_backstop_notice": "Restart check: the completion notice was cut off; the bridge is reporting instead. Running normally now.",
    # --- Usage report (/usage -> routines/claude-usage.sh) ---
    "usage_script_missing": "Usage script not found (routines/claude-usage.sh).",
    "usage_timeout": "The usage lookup did not finish in time. Please try again shortly.",
    "usage_failed": "Usage lookup failed: {error}",
    # --- DGN-835: usage-defer manual retry (usageretry:<label> / /usageretry) ---
    # usage_retry_not_enough mirrors the owner-approved Korean copy
    # (2026-08-12); the edge/error strings follow the standard i18n register.
    "usage_retry_bad_label": "Invalid retry request (malformed job label).",
    "usage_retry_no_replay": "Nothing to retry for this job (replay expired or already run): {label}",
    "usage_retry_lookup_failed": "Could not check the usage window right now. Please try again shortly.",
    "usage_retry_not_enough": "Not enough headroom yet (currently {pct}%, resets {reset}). Please try again a bit later.",
    "usage_retry_started": "Deferred job started: {label}",
    "usage_retry_usage": "Usage: /usageretry <job label> -- enter the label exactly as shown in the defer notification.",
    # --- Transient countdown (DGN-594; UI redesign DGN-780; free-form DGN-780b) ---
    # Icon + draining bar carry the "remaining" meaning; no word. The icon is a
    # placeholder (default hourglass/check resolved in bridge/countdown.py; a
    # caller may override it).
    "countdown_body": "{icon} {label}  {remaining}  {bar}",
    "countdown_done": "{done_icon} {label} done",
    # DGN-915: completion-affordance button label (shown on the done message).
    "countdown_done_button": "Continue ▶",
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
    # --- /queue command (DGN-911) ---
    "queue_usage": "Usage: /queue <message>",
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
    # DGN-881: button label overflow fallback -- localized number handle.
    "option_number_handle": "No.{n}",
    # DGN-881: recommendation marker SSOT. No code path reads this key; the
    # vendor contract doc (vendors/telegram.md, DGN-1141 stage 5) references
    # it as the canonical body-side marker next to the recommended option line.
    "option_rec_marker": "(rec)",
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
    "external_file_omitted_noninteractive": (
        "A file outside your working folder (PROJECT_ROOT) was withheld -- "
        "this delivery path has no live turn to confirm it. Ask again in chat "
        "if you need it."
    ),
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
    # --- Growing-fold captions/markers (DGN-699; moved from formatting.py
    # by DGN-851). ko catalog carries the LOCKED owner copy; en mirrors the
    # structure.
    "fold_caption_normal": "Progress log",
    "fold_caption_stopped": "Stopped · Progress log",
    "fold_caption_timeout": "Timed out · Progress log",
    "fold_truncation_line": "…(truncated)",
    "fold_omission_line": "⋯ snip ⋯",
    # UNCONFIRMED (owner gate pending) -- interrupt-fold ticket, mirrors
    # ko.py's interrupt_fold_caption. Candidates:
    #   1. "Stopped"            -- bare, most conservative (current fallback)
    #   2. "Stopped · Answer"   -- parallels "Stopped · Progress log" naming
    #   3. "Answer stopped"
    "interrupt_fold_caption": "Stopped",
    # --- Outage / failure notices ---
    # (outage_recovered removed by DGN-851: the recovery push was disabled
    #  per owner request 2026-06-30 -- bot._notify_outage_recovered only
    #  logs -- so the copy was dead weight. Re-enabling means restoring the
    #  key here + in ko + a real send in the bot callback.)
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
    # DGN-801: fast-path exit0 push failed after retries. State IS committed
    # (exit0 = commit witness), so never claim the input was lost and never
    # re-run it -- only the screen update failed. Domain-neutral wording on
    # purpose (the bridge does not know what the handler recorded).
    "fastpath_push_failed": (
        "Your input was recorded, but the screen update failed. "
        "The next reply will show the current state."
    ),
    # Variant when partial output already streamed before the turn died: do not
    # claim the message was dropped, warn the visible reply may be cut short.
    "turn_incomplete": (
        "That reply may be incomplete - the turn ended early. "
        "Ask me to continue or resend if anything is missing."
    ),
    # --- System prompt fragment (sent to Claude, English on purpose) ---
    # DGN-1141 stage 4: the [[OPTIONS]] syntax block and the send_file delivery
    # rules below are the CODE-owned channel grammar (single source; the
    # bridge.md/telegram.md doc copies retired in stage 5). Every sentence is
    # written from parser behavior (options.py / formatting.py / bot.py), not
    # copied from the docs -- the doc copies carried 9 drift findings
    # (DGN-1141-M7 section 2). Keep this text and the parser in lockstep:
    # they live in the same repo and must change in the same commit.
    "system_prompt": (
        "\n\n## User Questions and Choices\n\n"
        "The AskUserQuestion tool is NOT available in this environment. "
        "When you need to ask the user a question with multiple choice options:\n"
        "1. Output the question and context clearly\n"
        "2. List options with numbers (1., 2., 3., ...)\n"
        "3. STOP and WAIT for the user's response\n"
        "4. Do NOT continue execution or make assumptions\n"
        "5. Do NOT try to use the AskUserQuestion tool\n\n"
        "## Choice Buttons ([[OPTIONS]] marker)\n\n"
        "When a reply asks the user to pick exactly ONE option (a real decision "
        "menu -- not steps, todos, or status), arm tappable buttons by adding an "
        "[[OPTIONS]] marker. Without the marker, buttons depend on a best-effort "
        "classifier and may not appear. Three accepted shapes; the FIRST source "
        "that yields labels wins:\n"
        "1. Labeled marker: a standalone line '[[OPTIONS: label A | label B]]'. "
        "'|' separates labels and cannot appear inside a label.\n"
        "2. Bare marker + trailing lines: a standalone '[[OPTIONS]]' line; each "
        "non-blank line directly below it is one label. Collection stops at a "
        "blank line, another marker line, or a code fence.\n"
        "3. Bare marker + numbered body list: a standalone '[[OPTIONS]]' line "
        "plus a numbered list (1. 2. 3.) in the body; the LAST contiguous "
        "1..N run becomes the labels.\n"
        "Rules the parser enforces:\n"
        "- A marker inside a code fence never builds buttons. Never place the "
        "marker or the choice list inside a code block.\n"
        "- Do not mix a code block or a table with [[OPTIONS]] in one message; "
        "send code/tables first, then the choice message.\n"
        "- Labels are thin tokens; per-option descriptions belong in the body "
        "lines. Each button renders as 'N. label' and that WHOLE line must fit "
        "~31 character widths (CJK counts 1.5x), so keep labels within about "
        "27 latin / 18 Korean characters. If ANY one overflows, EVERY button "
        "in that keyboard degrades to a bare number token -- keep the full "
        "option text readable in the body.\n"
        "- A marker that yields no labels from any source builds ZERO buttons "
        "(the body text is kept). Always provide labels via one of the three "
        "shapes.\n\n"
        "## Sending Images and Files\n\n"
        "When the user asks you to send/show/deliver an image or file, do NOT read it "
        "with the Read tool. Instead, output a line that starts with 'send_file::' "
        "followed by the absolute path. One file per line. The system detects these "
        "lines and sends the files to the user.\n"
        "Example: send_file:: /path/to/image.png\n"
        "Supported image formats: .png, .jpg, .jpeg, .gif, .webp; other files are sent "
        "as documents. After generating a file, always include its send_file:: line.\n"
        "Delivery rules the bridge enforces:\n"
        "- Files are attached from DISK STATE at send time, after your text is "
        "delivered -- finalize the file before the turn ends.\n"
        "- A bare path in prose is never sent; only 'send_file::' lines are.\n"
        "- A file that does not exist or is 10MB or larger is silently skipped; "
        "check the size of large artifacts first.\n"
        "- A path outside the workspace root triggers an Allow/Deny confirmation "
        "on a live turn and is omitted (with a notice) on background turns -- "
        "prefer paths inside the workspace.\n"
        "- send_file:: lines are detected even inside code fences: never write "
        "one with a real existing path unless you mean to send that file.\n\n"
        "## Subagent Task Delegation\n\n"
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
    "idrill_arm_expired": "This button has expired. Please try again.",
    "idrill_error": "Error recording your input. Please try again.",
    "idrill_other_prompt": "Please type the value manually.",
    "idrill_logged_default": "Done: {1}",
    "idrill_logged_default_skip": "Done: {1} (step 2 skipped)",
    # --- DGN-939: N-step back navigation ---
    "idrill_nav_root": "Already at the first step.",
    "idrill_back_label": "« Back",
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
