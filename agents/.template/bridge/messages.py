"""Centralized user-facing strings (i18n shim).

Truly-additive shim over bridge/i18n: each constant below is bound at import
time to the active-locale template via t("<key>"). Call sites keep using these
constants exactly as before (as module attributes, often with .format(...)) --
NOTHING at a call site changes. The actual catalogs live in bridge/i18n/en.py
and bridge/i18n/ko.py; the active locale comes from config.config.locale
(env LOCALE, ko/en, default en). t() returns the RAW template with any
{placeholders} intact, so call sites still do their own .format(...).

To add or change a string: edit the i18n catalogs (add the same key to both
en.py and ko.py), then add a matching constant here. Constant names and count
must stay in parity with the catalog keys.
"""

from bridge.i18n import t

# --- Access control ---
NO_PERMISSION = t("no_permission")
NO_PERMISSION_CALLBACK = t("no_permission_callback")

# --- Born-locked ownership / claim flow (see bridge/ownership.py) ---
CLAIM_SUCCESS = t("claim_success")
CLAIM_CODE_LOG = t("claim_code_log")
OWNER_LOCK_MISSING_LOG = t("owner_lock_missing_log")

# --- Commands ---
WELCOME = t("welcome")
NEW_SESSION = t("new_session")
MODEL_SWITCHED = t("model_switched")
MODEL_SELECT = t("model_select")
MODEL_SWITCH_WARNING = t("model_switch_warning")
MODEL_UNKNOWN = t("model_unknown")
MODEL_STATE_FALLBACK = t("model_state_fallback")
MODEL_ALREADY_ACTIVE = t("model_already_active")
STOP_PAUSED = t("stop_paused")
STOP_NOTHING = t("stop_nothing")
STOP_INTERRUPTED = t("stop_interrupted")
NO_SESSION = t("no_session")
TASK_TERMINATED = t("task_terminated")

# --- Help ---
# DGN-919: help_text is no longer a static string. The command-list body is
# generated at render time in TelegramBot._cmd_help from COMMAND_MENU_SPEC.
# Only the header and footer remain as i18n-bound constants.
HELP_TEXT_HEADER = t("help_text_header")
HELP_TEXT_FOOTER = t("help_text_footer")

# --- Skills listing ---
SKILLS_NONE = t("skills_none")
SKILLS_HEADER_PROJECT = t("skills_header_project")
SKILLS_HEADER_GLOBAL = t("skills_header_global")

# --- BotCommand menu descriptions ---
CMD_DESC_NEW = t("cmd_desc_new")
CMD_DESC_STOP = t("cmd_desc_stop")
CMD_DESC_MODEL = t("cmd_desc_model")
CMD_DESC_RESUME = t("cmd_desc_resume")
CMD_DESC_SKILLS = t("cmd_desc_skills")
CMD_DESC_USAGE = t("cmd_desc_usage")
CMD_DESC_HELP = t("cmd_desc_help")
CMD_DESC_AUTHSYNC = t("cmd_desc_authsync")
CMD_DESC_BTW = t("cmd_desc_btw")
CMD_DESC_QUEUE = t("cmd_desc_queue")

# --- /btw command (DGN-902) ---
BTW_MARKER = t("btw_marker")
BTW_NO_QUESTION = t("btw_no_question")
BTW_NO_SESSION = t("btw_no_session")
BTW_FORK_FAILED = t("btw_fork_failed")
BTW_THINKING = t("btw_thinking")

# --- /authsync command (DGN-759) ---
AUTHSYNC_RUNNING = t("authsync_running")
AUTHSYNC_MATCH = t("authsync_match")
AUTHSYNC_MISMATCH_SYNCING = t("authsync_mismatch_syncing")
AUTHSYNC_SYNC_OK = t("authsync_sync_ok")
AUTHSYNC_SYNC_FAILED = t("authsync_sync_failed")
AUTHSYNC_NOT_APPLICABLE = t("authsync_not_applicable")
AUTHSYNC_SCRIPT_MISSING = t("authsync_script_missing")
AUTHSYNC_ERROR = t("authsync_error")

# --- Usage report (/usage -> routines/claude-usage.sh) ---
USAGE_SCRIPT_MISSING = t("usage_script_missing")
USAGE_TIMEOUT = t("usage_timeout")
USAGE_FAILED = t("usage_failed")

# --- DGN-835: usage-defer manual retry (button + /usageretry slash) ---
# usage_retry_not_enough = owner-approved copy (2026-08-12).
USAGE_RETRY_BAD_LABEL = t("usage_retry_bad_label")
USAGE_RETRY_NO_REPLAY = t("usage_retry_no_replay")
USAGE_RETRY_LOOKUP_FAILED = t("usage_retry_lookup_failed")
USAGE_RETRY_NOT_ENOUGH = t("usage_retry_not_enough")
USAGE_RETRY_STARTED = t("usage_retry_started")
USAGE_RETRY_USAGE = t("usage_retry_usage")

# --- Transient countdown (DGN-594) ---
COUNTDOWN_BODY = t("countdown_body")
COUNTDOWN_DONE = t("countdown_done")
# DGN-915: button label on the completion affordance (inline keyboard).
COUNTDOWN_DONE_BUTTON = t("countdown_done_button")

# --- Slash command usage ---
USAGE_SKILL = t("usage_skill")
USAGE_COMMAND = t("usage_command")

# --- Inbound photo / document prompts (sent to Claude) ---
PHOTO_PROMPT_SINGLE = t("photo_prompt_single")
PHOTO_PROMPT_PATH = t("photo_prompt_path")
PHOTO_PROMPT_ALBUM = t("photo_prompt_album")
PHOTO_PROMPT_ALBUM_PATH = t("photo_prompt_album_path")
DOC_PROMPT = t("doc_prompt")
DOC_PROMPT_PATH = t("doc_prompt_path")
USER_CAPTION = t("user_caption")

# --- Resume (session history) ---
NO_SESSION_HISTORY = t("no_session_history")
SESSION_HISTORY_HEADER = t("session_history_header")
RESUME_HINT = t("resume_hint")
RESUME_SWITCHED = t("resume_switched")
RESUME_INVALID_NUMBER = t("resume_invalid_number")

# --- Queue / overflow ---
QUEUE_BUSY = t("queue_busy")

# --- /queue command (DGN-911) ---
QUEUE_USAGE = t("queue_usage")

# --- Options keyboard ---
SELECT_PROMPT = t("select_prompt")
SELECTED = t("selected")

# --- External file confirmation ---
EXTERNAL_FILE_PROMPT = t("external_file_prompt")
EXTERNAL_FILE_SEND = t("external_file_send")
EXTERNAL_FILE_CANCEL = t("external_file_cancel")
EXTERNAL_FILE_CANCELLED = t("external_file_cancelled")
EXTERNAL_FILE_NONE = t("external_file_none")
EXTERNAL_FILE_CONFIRMED = t("external_file_confirmed")

# --- Timeout / resume (A4) ---
TIMEOUT_PAUSED = t("timeout_paused")
TIMEOUT_NO_RESUME = t("timeout_no_resume")
TAP_TO_CONTINUE = t("tap_to_continue")
TIMEOUT_TAP_NOTICE = t("timeout_tap_notice")
RESUME_EXPIRED = t("resume_expired")
RESUME_CONTINUING = t("resume_continuing")
STILL_WORKING = t("still_working")
RESUME_FAILED = t("resume_failed")

# A4 continuation prompt re-issued to Claude on resume.
RESUME_CONTINUATION_PROMPT = t("resume_continuation_prompt")

# --- Voice ---
VOICE_TOO_LONG = t("voice_too_long")
VOICE_DOWNLOAD_FAILED = t("voice_download_failed")
PHOTO_DOWNLOAD_FAILED = t("photo_download_failed")
DOC_DOWNLOAD_FAILED = t("doc_download_failed")
VOICE_CONVERT_FAILED = t("voice_convert_failed")
VOICE_UNAVAILABLE = t("voice_unavailable")
VOICE_EMPTY = t("voice_empty")
VOICE_TRANSCRIBE_FAILED = t("voice_transcribe_failed")

# --- Errors ---
INTERNAL_ERROR = t("internal_error")
PROCESSING_FAILED = t("processing_failed")
GENERIC_ERROR = t("generic_error")
NETWORK_TIMEOUT = t("network_timeout")

# --- DGN-686: is_error result notices (LOCKED user-facing copy) ---
# Shown INSTEAD of the raw English failure detail (which goes to stderr only).
# TRANSIENT/GENERIC pair with a [retry] button; AUTH offers no retry.
ERROR_TRANSIENT_RETRY = t("error_transient_retry")
ERROR_AUTH_RELOGIN = t("error_auth_relogin")
ERROR_GENERIC_RETRY = t("error_generic_retry")
ERROR_RETRY_BUTTON = t("error_retry_button")
ERROR_RETRYING = t("error_retrying")
ERROR_RETRY_EXPIRED = t("error_retry_expired")

# --- File send failure (send_file:: retry exhausted) ---
# DGN-649: reason-specific variants -- SEND_FILE_FAILED keeps the network
# wording and now fires only for network-classified failures.
SEND_FILE_FAILED = t("send_file_failed")
SEND_FILE_FAILED_DIMENSIONS = t("send_file_failed_dimensions")
SEND_FILE_FAILED_TOO_LARGE = t("send_file_failed_too_large")
SEND_FILE_FAILED_API = t("send_file_failed_api")

# --- Outage / failure notices ---
# (OUTAGE_RECOVERED removed by DGN-851 -- the recovery push was disabled per
#  owner request 2026-06-30, so the constant was dead weight. See
#  bot._notify_outage_recovered.)
PROACTIVE_TURN_FAILED = t("proactive_turn_failed")

# --- Subagent placeholder-flake recovery (DGN-670) ---
# FLAKE_RETRY_PREFIX is model-facing (English on purpose); the bridge appends
# the original user message verbatim. FLAKE_RECOVERY_FAILED is the user-facing
# notice when the single retry also failed.
FLAKE_RETRY_PREFIX = t("flake_retry_prefix")
FLAKE_RECOVERY_FAILED = t("flake_recovery_failed")

# --- Turn-death safety net (DGN-163) ---
# A consumed inbound update must never yield zero user-visible output: any
# exception between "update accepted" and the first reply routes through these.
TURN_FAILED = t("turn_failed")
TURN_FAILED_PHOTO = t("turn_failed_photo")
TURN_FAILED_DOCUMENT = t("turn_failed_document")
TURN_FAILED_VOICE = t("turn_failed_voice")
TURN_INCOMPLETE = t("turn_incomplete")

# --- Fast-path interceptor (DGN-801) ---
# Death-notice when an exit-0 handler push failed after retries: state is
# committed, only the screen update was lost -- never re-fed to the model.
FASTPATH_PUSH_FAILED = t("fastpath_push_failed")

# --- System prompt fragment (sent to Claude, English on purpose) ---
SYSTEM_PROMPT = t("system_prompt")

# DGN-699 FATAL-1: fold-mode register split, appended to the system prompt by
# sdk_bridge._compose_system_prompt() ONLY when the effective interim mode is
# "fold" -- other modes must never receive the "user already saw your
# progress" premise. Model-facing, English on purpose, not i18n.
SYSTEM_PROMPT_FOLD_FRAGMENT = (
    "\n\n## Progress Narration vs Final Answer (DGN-699)\n\n"
    "The interim text you emit BETWEEN tool calls is shown to the user "
    "live as a progress log (a collapsible quote bubble), and your final "
    "end-of-turn message is delivered separately below it. Treat them as "
    "two distinct registers: interim narration carries PROGRESS ONLY "
    "(what you are doing right now); the final message carries the "
    "SUBSTANTIVE RESULT ONLY. Do NOT restate or re-summarize in the "
    "final message what you already narrated as progress -- the user has "
    "already seen it."
)

# DGN-429 hybrid leg 1: output-language rule template appended to the system
# prompt by sdk_bridge._compose_system_prompt(). Carries a {language}
# placeholder (human locale name), formatted at compose time so the rule
# always reflects the live config.locale value.
OUTPUT_LANG_PROMPT_TEMPLATE = t("output_lang_prompt")

# Denial message returned to Claude when it tries AskUserQuestion.
ASK_USER_QUESTION_DENY = t("ask_user_question_deny")

# Denial message returned to Claude when an out-of-root path is detected.
OUTSIDE_PATH_DENY = t("outside_path_deny")

# Denial returned to Claude for a protected/out-of-root path on a no-pending
# (background/proactive) turn, where no interactive confirm is possible.
OUTSIDE_PATH_DENY_NO_CONFIRM = t("outside_path_deny_no_confirm")

# --- DGN-918: idrill in-place drilldown callback ---
IDRILL_ARM_EXPIRED = t("idrill_arm_expired")
IDRILL_ERROR = t("idrill_error")
IDRILL_OTHER_PROMPT = t("idrill_other_prompt")
IDRILL_LOGGED_DEFAULT = t("idrill_logged_default")
IDRILL_LOGGED_DEFAULT_SKIP = t("idrill_logged_default_skip")
