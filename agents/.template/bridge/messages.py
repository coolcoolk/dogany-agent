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
STOP_PAUSED = t("stop_paused")
STOP_NOTHING = t("stop_nothing")
NO_SESSION = t("no_session")

# --- Resume (session history) ---
NO_SESSION_HISTORY = t("no_session_history")
SESSION_HISTORY_HEADER = t("session_history_header")
RESUME_HINT = t("resume_hint")
RESUME_SWITCHED = t("resume_switched")
RESUME_INVALID_NUMBER = t("resume_invalid_number")

# --- History ---
NO_HISTORY = t("no_history")
HISTORY_HEADER = t("history_header")

# --- Queue / overflow ---
QUEUE_BUSY = t("queue_busy")

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

# --- Outage / failure notices (DGN-045) ---
OUTAGE_RECOVERED = t("outage_recovered")
PROACTIVE_TURN_FAILED = t("proactive_turn_failed")

# --- System prompt fragment (sent to Claude, English on purpose) ---
SYSTEM_PROMPT = t("system_prompt")

# Denial message returned to Claude when it tries AskUserQuestion.
ASK_USER_QUESTION_DENY = t("ask_user_question_deny")

# Denial message returned to Claude when an out-of-root path is detected.
OUTSIDE_PATH_DENY = t("outside_path_deny")
