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
    # Expired-button tap alert (STALE gate made visible).
    # Confirmed -- owner 2026-10-02 08:17: name the expiry window and point
    # to typing in the chat. {minutes} = the declaring button kind's TTL
    # (CALLBACK_TTL_SECONDS, dec-272). Both keys share the value -- call
    # sites unchanged.
    "stale_callback_expired": "This button expired after {minutes} minutes. Please type what you need in the chat.",
    "stale_callback_expired_nolabel": "This button expired after {minutes} minutes. Please type what you need in the chat.",
    # --- Born-locked ownership / claim flow ---
    # Fallback ONLY: a successful /claim normally opens the agent's own first
    # turn (bot._open_first_contact) and this line is never sent. It goes out
    # when that turn could not be started. Confirmed -- owner 2026-10-02 17:39
    # (first-contact rework 2026-10-02).
    "claim_success": "You're all set. Say hi whenever you like.",
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
    # DGN-1814 r4 /model picker (owner copy final 2026-10-01 09:52): header /
    # "Current:" / note, one line each; one header for the one-vendor and the
    # vendor-step screen.
    "model_select": "Select an LLM model.",
    "model_now": "Current: {model}",
    # Appended to "Current:" when the live version of a configured alias is
    # not the table's. The _batchim twin exists for the ko particle only.
    "model_latest_hint": " (latest is {version}; a restart may switch to it)",
    "model_latest_hint_batchim": " (latest is {version}; a restart may switch to it)",
    "model_switch_note": "(switching starts a new session)",
    # Appended to the current model / vendor button label.
    "model_current_mark": " (current)",
    "model_vendor_not_wired": "{vendor} models can't answer in chat yet.",
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
    # DGN-991: unified with stop_interrupted (2026-09-03 owner approval).
    # This branch fires when the teardown found nothing running and only the
    # pending queue had entries -- but /stop ALWAYS does both (cancel the
    # in-flight turn AND clear the queue); the branch is a post-hoc read of
    # what happened to exist, not a separate mode. From the owner's side a
    # queued instruction is still work he asked for, so "stopped" is true.
    # Four /stop branches collapse to two: something existed -> this line;
    # genuinely nothing -> stop_nothing.
    "stop_paused": "Stopped what was running.",
    "stop_nothing": "Nothing to stop.",
    # DGN-991: owner-approved final copy (2026-09-03, UX gate passed) --
    # this is the ENTIRE /stop reply, one sentence, nothing appended. The
    # prior 3-4 sentence draft made two false claims (background work
    # "always" dies / survives -- DGN-991 measured both outcomes depending
    # on in-session vs detached dispatch, so no blanket claim is true) plus
    # a second-/stop threat the code never needed (bot.py's soft-interrupt
    # except-block already falls back to _hard_stop automatically). The
    # real fact-based signal for an actual kill is bg_task_killed_notice
    # below, automatic interrupts only (DGN-1593: never after /stop).
    "stop_interrupted": "Stopped what was running.",
    # DGN-1015: fact-based, fires only when tracked background work
    # (subagent or background command) actually died in an AUTOMATIC
    # interrupt -- never after the owner's own /stop (DGN-1593 B).
    # Vocabulary rule OWNER-APPROVED 2026-08-24 (commit f67bf57e): no
    # developer terms. DGN-1593 A: count only -- the registry holds only
    # internal tool-call labels. DGN-1593 r2 (owner 2026-10-02 08:47): one
    # bg_task_killed_item bullet per job whose name the owner already knows
    # (START push / workbench row); a nameless job is only counted. Copy
    # OWNER-CONFIRMED 2026-10-02.
    "bg_task_killed_notice": "⚠️ {count} running task(s) stopped too.",
    "bg_task_killed_item": "- {name}",
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
    # DGN-991: unified with stop_interrupted (2026-09-03 owner approval).
    # Measured: the hard-teardown path only kills the CLI subprocess -- the
    # bridge process stays up and never drops user_id from
    # _runtime_active_sessions, so _effective_session_id keeps returning the
    # same session_id and the next message resumes the same conversation.
    # That is the same outcome the owner sees on the soft path, so there is
    # no honest basis for a different sentence here.
    "stop_forced": "Stopped what was running.",
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
    # DGN-1112: /login menu row (on the menu since the dec-266 copy approval).
    "cmd_desc_login": "Log in to Claude Code",  # owner-approved 2026-10-06 17:35 (dec-268)
    # ESTATE:BEGIN DGN-1417 update command
    "cmd_desc_update": "Check and apply updates",
    "update_none": "There are no updates to apply right now.",
    "update_lookup_failed": "Couldn't load the update list. I'll check once more in a few minutes and let you know.",
    "update_lookup_failed_again": "Couldn't load the update list. Please run /update again in a little while.",
    # DGN-1800: bridge-side /update lines, moved out of bot.py.
    "update_check_failed": "Couldn't check for updates. I'll check the connection and let you know.",
    "update_stale": "What can be updated has changed. Please pick again from the new list.",
    "update_consent_prefix": "{labels} will update together. I'll pause my replies briefly and be right back.",
    "update_consent_button": "Pause and update",
    "update_apply_button": "Apply",
    # ESTATE:END
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
        # ESTATE:BEGIN internal ticket ID in shipped copy -- the owner build cites it, the public build must not
        "/authsync has been retired (DGN-1050): its file-to-keychain sync "
        # ESTATE:ELSE
        # "/authsync has been retired: its file-to-keychain sync "
        # ESTATE:END
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
    # new bridge terminal-closes the restart instead. Copy OWNER-APPROVED
    # 2026-08-22 09:12 (DGN-1240/DGN-1249 U1).
    "restart_backstop_notice": "Restart complete",
    # --- /login (DGN-1112) ---
    # Copy OWNER-APPROVED 2026-10-06 16:54 (dec-266), all 16 keys verbatim
    # as reviewed in the DGN-1112 login-relay review record -- do not reword.
    # login_url takes {url} and {minutes}.
    # login_failed / login_no_cli are LEAD lines: messages.py appends the
    # owner-confirmed error_auth_relogin terminal guide after them (dec-257);
    # after login_no_cli the guide drops its first sentence (dec-266).
    "login_preparing": "Preparing a login link -- one moment.",
    "login_url": (
        "Open the link below and sign in to Claude.\n"
        "When you finish, the page shows a code. Copy it and send it here exactly as shown.\n"
        "Please send it within {minutes} minutes. Send /cancel to stop.\n\n"
        "{url}"
    ),
    "login_code_received": "Got the code -- checking the login.",
    "login_wait": "Still working on the login -- one moment.",
    "login_code_invalid": (
        "That doesn't look like a login code. Copy the code from the login page "
        "and send it exactly as shown, or send /cancel to stop."
    ),
    "login_code_mismatch": (
        "That code belongs to a different login link. Sign in with the link I just "
        "sent and send the code it shows."
    ),
    "login_code_attempts": "Too many codes didn't match, so I cancelled the login. Send /login to start again.",
    "login_success": "You're logged in. Your next message will use the new login.",
    "login_failed": "The login didn't go through. Send /login to try again, or log in from Terminal as below.",
    "login_timeout": "No code arrived, so I cancelled the login. Send /login to start again.",
    "login_cancelled": "Login cancelled.",
    "login_already_pending": (
        "A login is already in progress. Sign in with the link I sent and send the code, "
        "or send /cancel to stop."
    ),
    "login_busy": "Another login is already running on this computer. Please try again after it finishes.",
    "login_private_only": "/login only works in a private chat with me.",
    "login_env_token": (
        "Claude credentials set in an environment variable take priority on this computer, "
        "so logging in here would not change anything. Please check that setting on the computer first."
    ),
    "login_no_cli": "I couldn't find the Claude program, so I can't log in from here. Log in from Terminal as below.",
    # --- Usage report (/usage -> routines/claude-usage.sh) ---
    # DGN-1362: public as of OSS 2.0.1 -- the script ships at the instance root.
    "usage_script_missing": "Usage script not found (routines/claude-usage.sh).",
    "usage_timeout": "The usage lookup did not finish in time. Please try again shortly.",
    "usage_failed": "Usage lookup failed: {error}",
    # ESTATE:BEGIN usage-defer catalog keys -- estate routine copy (DGN-835/841)
    # --- DGN-835: usage-defer manual retry (usageretry:<label> / /usageretry) ---
    # usage_retry_not_enough mirrors the owner-approved Korean copy
    # (2026-08-12); the edge/error strings follow the standard i18n register.
    "usage_retry_bad_label": "Invalid retry request (malformed job label).",
    "usage_retry_no_replay": "Nothing to retry for this job (replay expired or already run): {label}",
    "usage_retry_lookup_failed": "Could not check the usage window right now. Please try again shortly.",
    "usage_retry_not_enough": "Not enough headroom yet (currently {pct}%, resets {reset}). Please try again a bit later.",
    "usage_retry_started": "Deferred job started: {label}",
    "usage_retry_usage": "Usage: /usageretry <job label> -- enter the label exactly as shown in the defer notification.",
    # ESTATE:END
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
    # DGN-1934: one live decision menu -- a newer [[OPTIONS]] keyboard
    # retires an older unanswered one; its buttons are dropped and this
    # one-line pointer is appended. Copy owner-confirmed 2026-10-08
    # (dec-321).
    "options_superseded_pointer": "→ continue in the message below",
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
    # DGN-1523: fact only, no button instruction -- this string reaches the
    # user through layers that may or may not have created a button
    # (auto-resume success never shows a button at all). The tap instruction
    # lives solely in timeout_tap_notice, next to the button it describes.
    "timeout_paused": (
        "This is taking a while, so I paused once and am continuing automatically."
    ),
    "timeout_no_resume": (
        "This took a while and paused, but I could not find where to continue. "
        "Please send your request again."
    ),
    "tap_to_continue": "Continue",
    "timeout_tap_notice": "This took a while and paused. Tap to continue.",
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
    # DGN-1914 on-demand voice install. voice_install_prompt owner-confirmed
    # 2026-10-08 (dec-321): {name} = the tier's display name
    # (voice_tier_*_name), {size} = voice_setup.MODEL_SIZES; rendered by
    # voice_setup.install_prompt. The other voice_install_* lines below:
    # {model} = faster-whisper model id, {size} = approx download size.
    "voice_install_prompt": (
        "To understand voice messages I need to install voice recognition first. "
        "Tap the button below and I'll install {name} (about {size}) "
        "and read this message right after."
    ),
    "voice_install_button": "Install voice recognition",
    "voice_installing": "Installing voice recognition ({model}, about {size}). One moment.",
    "voice_install_wait": "Voice recognition is still installing. I'll read this message when it's done.",
    "voice_install_done": "Voice recognition is installed.",
    "voice_install_failed": (
        "Couldn't install voice recognition. Check your internet connection and "
        "free disk space, then tap again."
    ),
    # DGN-1914 follow-up: tier picker (owner decision 2026-10-07 20:40).
    # Copy OWNER-CONFIRMED 2026-10-08 (dec-313; 2-tier + model names: dec-312).
    # Only shown tiers get a line;
    # {speed} is the tier's speed clause, or voice_pick_slow on a slow device.
    "voice_pick_prompt": "To understand voice messages I need to install voice recognition first. Which one should I use?",
    "voice_pick_line": "· {label} ({name}): {desc} (about {size})",
    "voice_pick_recommended": " (recommended)",
    "voice_pick_slow": "Slow on this Mac.",
    "voice_pick_footer": "You can pick again later: just say \"change voice recognition\".",
    "voice_tier_small_label": "Fast",
    "voice_tier_small_name": "OpenAI Whisper small",
    "voice_tier_small_desc": "{speed} Good for clear English.",
    "voice_tier_small_desc_generic": "{speed} Makes more mistakes.",
    "voice_tier_small_speed": "Light and fast.",
    "voice_tier_turbo_label": "Accurate",
    "voice_tier_turbo_name": "OpenAI Whisper large-v3-turbo",
    "voice_tier_turbo_desc": "Catches English more reliably, even with accents or noise. {speed}",
    "voice_tier_turbo_desc_generic": "Much more accurate. {speed}",
    "voice_tier_turbo_speed": "",
    # Whole-message phrases that re-open the picker ("|"-separated; spacing
    # and punctuation are ignored when matching).
    "voice_change_phrases": "change voice recognition|change speech recognition",
    # --- Errors ---
    "internal_error": "Internal error: {error}",
    "processing_failed": "Processing failed: {error}",
    "generic_error": (
        "Sorry, an error occurred while processing your message.\nError: {error}"
    ),
    "network_timeout": "Network connection timed out. Please try again shortly.",
    # --- DGN-686: is_error result notices ---
    "error_transient_retry": "Processing failed temporarily. Try again?",
    # DGN-1857: re-login guide (LOCKED copy, owner-confirmed dec-256 -- do not reword).
    "error_auth_relogin": (
        "Your Claude login has expired. On this Mac, run `claude auth login` "
        "in Terminal, then send /restart. If you are connected remotely (SSH), "
        "first run `security unlock-keychain ~/Library/Keychains/login.keychain-db`."
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
        # ESTATE:BEGIN internal ticket ID in shipped copy -- the owner build cites it, the public build must not
        "[BRIDGE FLAKE RETRY / DGN-670] Your previous reply was a delegation "
        # ESTATE:ELSE
        # "[BRIDGE FLAKE RETRY] Your previous reply was a delegation "
        # ESTATE:END
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
    # DGN-1209: machine-line gate alert -- an UNREGISTERED machine-shaped line
    # (e.g. "SOME_TOKEN k=v") reached an owner-facing rail and was passed
    # through. PLACEHOLDER COPY -- owner confirmation pending (UX gate); the
    # ticket report lists the candidate wordings. {tokens} / {rail} required.
    # DGN-1589/DGN-1591: this alert's reader is the framework OPERATOR, never
    # the end-user (routing: bot.py/machine_gate.py audience axis). The copy
    # must not itself look machine-shaped -- no leading bracket or ALL-CAPS
    # token, or it re-triggers the detector on the operator rail.
    "machine_line_alert": (
        "Framework hygiene notice: unregistered machine-shaped line(s) "
        "passed the user surface unchanged: {tokens} (rail: {rail}). "
        "Nothing was dropped; this fires once per day per token."
    ),
    # Appended when the call site declared no rail (rail=unknown): the missing
    # declaration itself is the work item. PLACEHOLDER COPY (same gate).
    "machine_line_alert_undeclared": (
        " This send path has no rail declaration -- it should be declared."
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
        "- The buttons attach to your reply message itself (no separate "
        "prompt bubble); a reply that is ONLY the choice list gets a short "
        "prompt line to carry them.\n"
        "- Labels are thin tokens; per-option descriptions belong in the body "
        "lines. With 2+ buttons every button shows 'N. label' (any "
        "shape) -- refer to options by number; a single button shows the bare "
        "label. The WHOLE button line must fit "
        "~31 character widths (CJK counts 1.5x), so keep labels within about "
        "27 latin / 18 Korean characters. If ANY one overflows, EVERY "
        "button in that keyboard degrades to a bare number token and the body "
        "keeps the numbered list -- keep the full option text readable there.\n"
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
    "outside_approval_allow_words": "allow|yes|y",
    "outside_approval_deny_words": "deny|no|n",
    "outside_path_deny_no_confirm": (
        "Access to a protected or out-of-root path was denied. This is a "
        "background turn with no user available to confirm it. Skip this path or "
        "ask the user directly in their next message."
    ),
    # ESTATE:BEGIN idrill catalog keys (L-ADAPT STRINGS[idrill_*], DGN-918/939)
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
    # ESTATE:END
}

# ---------------------------------------------------------------------------
# Skill display-name catalog (DGN-102)
#
# Keys = immutable skill folder IDs. Values = user-facing Title Case labels in
# this locale. skill_display_name() in bridge/i18n/__init__.py resolves these;
# fall-back is the raw ID when a key is absent (fail-open, never KeyError).
# ---------------------------------------------------------------------------
SKILL_DISPLAY_NAMES = {
    # ESTATE:BEGIN the dogany-* framework skill labels; the catalog itself stays (lifekit/product skills are not estate)
    # --- Framework / dogany-* skills ---
    "dogany-cron-register":   "Cron Register",
    "dogany-lifekit-setup":   "Life Management Setup",
    "dogany-mailer":          "Mailer",
    "dogany-memory-search":   "Memory Search",
    "dogany-proactive-push":  "Proactive Push",
    "dogany-reminder":        "Reminder",
    "dogany-skill-creator":   "Skill Creator",
    "dogany-user-onboarding": "User Onboarding",
    # ESTATE:END
    # --- Lifekit bundle skills ---
    "diet-log":         "Diet Log",
    "workout-log":      "Workout Log",
    "appointment-log":  "Appointment Manager",
    "relationship":     "Relationship Manager",
    "task-update":      "Task Manager",
    "spending-log":     "Spending Log",
}
