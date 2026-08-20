# bridge upstream

This `bridge/` directory is a VENDORED (in-tree) copy of the standalone Telegram
<-> Claude bridge so that `dogany-agent` runs immediately after a plain
`git clone` with no extra `--recursive` step.

- Upstream: https://github.com/coolcoolk/claude-code-telegram
- Pinned commit: 87ac8caefd01dcc2efe6a6d0a168ab82ecc4d062
- Vendor-rev: DGN-946+DGN-950+DGN-953+DGN-954 v1.39.2 bridge regression sweep
  back-land (canonical-only; no OSS pin change; OSS backport pending):
  DGN-946 -- sdk_bridge.py flush_folds_for_shutdown() flushes in-flight fold
  bubbles BEFORE HTTP teardown; bot.py calls it stop_event-gated (real stop
  only; transient Conflict/NetworkError restarts keep live folds); streaming.py
  finalize_fold_html logs a bounded html tail on finalize-time HTML 400
  (observability for parser rejects). DGN-950 -- dashboard.py codepoint-safe tail cut +
  torn-read hold (hangul byte-cut render seam); countdown.py + bot.py
  turn_active deferral so a rest-timer send can never race a queue-before-timer
  turn (Warg render seams). DGN-953 -- btw.py cause-signal logging for silent
  empty /btw fork responses (first-turn + continuation fallback paths) and
  fork-CLI leak reap: orphan fork CLI on turn timeout/failure gets
  disconnect + deregister (bot.py handler wiring). DGN-954 -- formatting.py
  ATX heading promotion: #/##/### headings render as glyph+bold line instead
  of raw markdown. Tests: test_dgn946_shutdown_fold_flush.py,
  test_dgn950_render_seams.py, test_dgn953_btw_empty_response_logging.py,
  test_dgn953_btw_fork_leak.py, test_dgn954_atx_heading_glyph.py (59 new, all
  green). Back-land of metal canary branches ec6f75e/8475e7d/0b7dbf4+aa88970/
  c83cc0f; rider bases were metal v1.39.0-state bridge, 3-way applied onto the
  v1.39.1 template (DGN-947/846 inline-seal + notice-finalize regions
  untouched, no hunk collision). Full suite 1230 passed; same 7 pre-existing
  failures as v1.39.1 baseline, zero new.
- Vendor-rev: DGN-947+DGN-846+DGN-939-item2 v1.39.1 live-regression back-land
  (canonical-only; no OSS pin change; OSS backport pending): streaming.py --
  StreamingMessageHandler.seal_segment() edits each interim draft to its
  permanent form and clears the draft list WITHOUT latching the handler, so
  the terminal answer opens a fresh draft and its own finalize_all still runs
  (repairs the v1.39.0 inline-glue regressions: OPTIONS button break + raw
  HTML tag leak). sdk_bridge.py -- reader loop seals at the inline terminal
  boundary (fail-soft, fold mode untouched); FOLD-1/2 budget-drop rescue:
  when compose_interim_fold drops the fold over budget, the surviving interim
  is re-emitted as its own bubble via render_fold_final/send_fold_html above
  the answer; rescue send failure logs ERROR (send_fold_html return value is
  the success signal), background budget-drop logs as loss, and the three
  fold-absence causes (budget rescue / lossless echo / teardown race) log
  separably. bot.py -- idrill followup_cmd primitive (DGN-939 item2): an
  optional post-record argv fired ONLY after the terminal cmd/cmd_skip2 exits
  0, same security invariants (list-form, no shell, 15s timeout), stdout
  posted as a NEW message; shared _idrill_exec_argv gate now enforces an
  absolute argv[0] for cmd/cmd_skip2/followup_cmd; DGN-846 notice-finalize
  skip gate. formatting.py -- contains_telegram_html() (DGN-846 raw-tag-leak
  gate). IDRILL-ARM-CONTRACT.md -- followup_cmd field row + sections 4/4.1/5.
  Tests: test_dgn846_notice_finalize_html.py, test_dgn939_followup_cmd.py,
  test_dgn947_fold_lossless.py, test_dgn947_ux_invariants.py new; dgn877 +
  autosplit harness updates. Back-land of metal canary commits
  aafbb7b/36c01ed/9e189a3/6a2c6c9 (canary suite 1227 passed).
- Vendor-rev: DGN-939 in-place callback engine -- N-step + « Back + fold confirm
  (canonical-only; no OSS pin change; OSS backport pending): bot.py -- the
  idrill primitive generalized from the fixed 2-step (DGN-918/924) to N declared
  steps. `_handle_idrill_callback` now dispatches `idrill:<arm_id>:<step>:<value>`
  for step "1".."N" plus a reserved `idrill:<arm_id>:back` « Back action; a
  `_nav` capture stack (`[[step, value], ...]`) accumulates on each advance and
  pops on Back (underflow at root -> IDRILL_NAV_ROOT, no crash). `step_final`
  arm field selects the firing step (default "2" = byte-identical DGN-918/924
  firing); `nav_back:true` opts a Back row in (default off = unchanged keyboard).
  `_idrill_fire_cmd_n` substitutes positional `{1}..{N}` from the ordered
  captures (2-step `_idrill_fire_cmd` now a compat wrapper); out-of-range token
  or out-of-declaration value aborts the fire. item6: `confirm_fold`
  {summary, body} arm field renders an expandable blockquote below the
  confirmation via the existing compose/render_fold_block (DGN-619/719) path,
  HTML-edited only when a fold is present (plain-text edit otherwise). Security
  invariants preserved (arm_id containment, per-step declared-value whitelist,
  list-form no-shell exec). New IDRILL-ARM-CONTRACT.md documents the full
  file/CLI/exit-code arm contract (the Skull wiring surface). Pin sync (DGN-214)
  + countdown done-affordance (DGN-915) verified robust, unchanged. 22 new
  DGN-939 tests + 93 DGN-918/924 tests all green (backward-compat).
- Vendor-rev: DGN-932 class-wise notification policy -- 2 mechanisms
  (canonical-only; no OSS pin change; OSS backport pending): config.py --
  notify_policy Dict[str,bool] field (env NOTIFY_POLICY, "class=silent" /
  "class=loud" space/comma list; malformed entries skipped, unknown classes
  kept inert = forward-compatible); _parse_notify_policy before-validator;
  module-level NOTIFY_CLASS_DEFAULT_SILENT locked no-config defaults (owner lock
  2026-08-19: draft=loud, fold=silent, countdown=loud [first tick = set-start
  signal, M1], dashboard=silent, options_prompt=silent) + notify_silent(class)
  resolver (config override wins > locked default > unknown=loud). Mechanism A
  (edited-surface bubbles: notification decided ONCE at first send, later
  in-place edits notification-free): streaming.py send_fold_html (class "fold")
  + draft overflow/first send (class "draft"); countdown.py first-tick send
  (class "countdown", loud) -- countdown.py first send now LOUD by default,
  superseding the DGN-594 always-silent send; dashboard.py recreate send + pin
  (class "dashboard", pin follows the same value). Mechanism B (one-shot
  messages, Telegram-default loud, opt-in silence by class tag): bot.py
  [[OPTIONS]] SELECT_PROMPT button appendix (class "options_prompt", silent so
  the button message does not bury the loud body); system/error/timeout/file/
  command replies stay untagged = loud. tests/test_dgn932_notify_policy.py new
  (config parse, default resolution, both mechanisms, M1 first-tick-loud, m2
  dashboard comment); tests/test_countdown.py test_first_send_notifies_and_
  targets_chat replaces the DGN-594 always-silent assertion; conftest.py pops
  NOTIFY_POLICY (live-instance value must not leak into tests). Ported from the
  Metal DGN-932 branch (99d1711); base files byte-identical to canonical
  template -- clean re-vendor, no prerequisite gap. Test-file drift resolved in
  the same port: tests/conftest.py (DGN-911 knobs), tests/test_countdown.py
  (DGN-915 completion-affordance cases), tests/test_autosplit_codeblock_options.py
  (new file) were behind the live source (source files already carried the
  features; only the vendored tests lagged) -- brought to live parity here.
- Vendor-rev: DGN-924 idrill shape-agnostic -- declared step buttons/validate/
  text + [[IDRILL]] marker (canonical-only; no OSS pin change; OSS backport
  pending): bot.py -- hardcoded _IDRILL_STEP2_BUTTONS + step1/step2 validation
  regexes removed; step_buttons / step_validate / step_text now read from the
  arm file; the bridge no longer knows any use-case values. formatting.py --
  [[IDRILL:<arm_id>]] output marker renders the initial keyboard from
  arm.step_buttons["1"] + step_text["1"] (reuses the [[OPTIONS]] marker->keyboard
  path; arm file is the single button source, no control-file reader). Security
  invariants preserved (declared-value whitelist, arm_id containment, list-form
  exec, positional {1}/{2}). 93 tests (incl. non-domain button set = universality
  proof). Ported from the Metal DGN-924 branch (cb9570e).
- Vendor-rev: DGN-918 idrill in-place drilldown capture callback primitive
  (canonical-only; no OSS pin change; OSS backport pending): bot.py --
  idrill:<arm_id>:1:/:2: two-step callback family (_handle_idrill_callback
  dispatched from _handle_callback): step-1 tap swaps the inline keyboard to the
  step-2 stage; step-2 tap consumes a one-shot arm file
  (files/program/.idrill-arm/<arm_id>.json; _IDRILL_ARM_ID_RE traversal guard;
  consume-on-fire so a double tap reports expired) and executes the DECLARED argv
  from the arm file (cmd / cmd_skip2 keys; positional {1}/{2} substitution via
  _IDRILL_TOKEN_1/2; argv exec, no shell -- contract v2), then edits the message
  to a rendered confirmation (confirm_fmt, positional tokens). Fail-soft on
  missing/expired arm, malformed/absent cmd, and nonzero exit. Domain-neutral:
  no domain field knowledge in the bridge (workout-only fallback dropped;
  step2-without-step1 fails soft via the numeric whitelist). messages.py:
  IDRILL_* strings. i18n/en.py + ko.py: idrill_* keys (generic copy).
  tests/test_dgn918_idrill_callback.py (71 tests: traversal, double-tap,
  injection, argv contract, callback-data 64-byte cases, literal scan,
  legacy-token passthrough). Ported from the Metal DGN-918 branch (3d8400c,
  domain-neutral rename of d684d88); base files byte-identical to canonical
  template -- clean re-vendor, no prerequisite gap.
- Vendor-rev: DGN-911 bridge in-flight interrupt-default (5s debounce) + /queue
  (Metal merge d908798; canonical-only, no OSS pin change; OSS backport
  pending): config.py -- BRIDGE_INFLIGHT_DEBOUNCE_S (5.0s default) +
  BRIDGE_INFLIGHT_INTERRUPT_NOTICE (default off). bot.py -- default in-flight
  policy inverted from DGN-616 coalescing to debounce-interrupt (_debounce_texts
  buffer, _debounce_timers, _reset_inflight_debounce, _debounce_expire with
  fail-safe delivery guarantee); /queue command (_cmd_queue) routes through
  legacy coalesce=True path; _clear_inflight_debounce on /stop; FATAL drain-on-
  exit finally blocks on skill/options/resume/retry SDK turns; MAJOR btw fork +
  continuation drain wrappers; chronological merge (pending+debounced) in
  _drain_pending_texts. messages.py: CMD_DESC_QUEUE + QUEUE_USAGE. i18n/en.py +
  ko.py: /queue help line, cmd_desc_queue, queue_usage.
- Vendor-rev: DGN-922 release-gate MAJOR fixes (canonical-only; no OSS pin change;
  OSS backport pending): bot.py -- COMMAND_MENU_SPEC comment corrected (/claim has no
  CommandHandler; intercepted via MODE_CLAIM _check_access path); _btw_fork_tasks
  Dict[int, set] added to __init__ (FIX 4: separate set for fork tasks, out of
  _user_run_tasks so fork does not block main-conversation debounce); _check_access
  gains skip_stale kwarg (False by default; FIX 2: all existing callers unaffected;
  cdn:done: path passes skip_stale=True); _track_btw_fork_task new method (prune +
  add + done-callback that discards and logs; FIX 4); _clear_user_queue extended to
  also cancel/clear _btw_fork_tasks entries (FIX 4: /stop terminates outstanding forks);
  _cmd_stop soft path now discards _user_pending_texts and calls _clear_inflight_debounce
  BEFORE the interrupt call (FIX 1: ghost merged turn after soft stop eliminated);
  run_fork_task send path replaces single edit_message_text with split_text +
  rebalance_html_chunks + per-chunk html_to_plain_text degrade + thread_anchor mutable
  container (FIX 3: >4096-char fork response no longer fails silently); run_fork_task
  now wrapped in _fork_task_with_drain (drain _user_pending_texts on exit; belt-and-
  braces) and tracked via _track_btw_fork_task not _track_user_task (FIX 4);
  run_fork_continuation send path replaces single send_message with split_text +
  rebalance_html_chunks + per-chunk html_to_plain_text degrade (FIX 3); continuation
  wrapped in _fork_continuation_with_drain + tracked via _track_btw_fork_task (FIX 4);
  _handle_callback peeks cdn_done_tap before _check_access and passes skip_stale=True
  for cdn:done: only (FIX 2: countdown done button stays tappable past 20-min stale
  gate, which equals 24h MAX_SECONDS). countdown.py -- CDN_DONE_PREFIX comment
  updated: clarifies message_id suffix is present but not used for anti-stale check;
  stale exemption now via skip_stale=True path in DGN-922 FIX 2. Logic ported from
  Metal live (777c95f); prerequisites confirmed present in canonical: split_text,
  rebalance_html_chunks, html_to_plain_text (formatting.py DGN-891),
  _clear_inflight_debounce (DGN-911 branch), _user_pending_texts (DGN-616),
  CDN_DONE_PREFIX (DGN-915 branch). No prerequisite gap.
- Vendor-rev: DGN-919 test fixup (follow-up to DGN-919 carry-back; canonical-only):
  tests/test_dgn618_command_output.py -- test_no_history_in_bot_command_menu uses
  COMMAND_MENU_SPEC lookup instead of inspecting _set_bot_commands source (now a
  1-liner); test_history_absent_from_help_text_both_locales checks header+footer
  keys via STRINGS.get() instead of removed "help_text" key. tests/test_dgn902_btw_command.py
  -- test_help_text_mentions_btw checks COMMAND_MENU_SPEC membership instead of
  messages.HELP_TEXT (split into header+footer by DGN-919).
- Vendor-rev: DGN-920 btw fork output through shared HTML formatter (canonical-only;
  no OSS pin change; OSS backport pending): bot.py -- sanitize_message_for_telegram
  imported from bridge.formatting; in run_fork_task: marked text routed through
  balance_telegram_html(sanitize_message_for_telegram(marked)) before edit_message_text
  and send_message fallback, both with parse_mode="HTML"; fail-soft: if formatting
  raises, use_html=False and send raw text without parse_mode; in run_fork_continuation:
  same pattern applied to the send_message path. Logic ported from Metal live
  (fbfcfe3); sanitize_message_for_telegram was already present in canonical
  formatting.py (DGN-822), no prerequisite gap.
- Vendor-rev: DGN-919 slash command menu single-source (canonical-only; no OSS
  pin change; OSS backport pending): bot.py -- COMMAND_MENU_SPEC module-level
  list (10-item ordered: new/stop/btw/usage/queue/model/skills/resume/authsync/
  help) as single source of truth for both BotCommand popup menu and /help
  numbered list; _set_bot_commands builds from spec (no hand-listed duplicate);
  _cmd_help generates numbered list from spec with HELP_TEXT_HEADER + _FOOTER.
  messages.py -- HELP_TEXT replaced by HELP_TEXT_HEADER + HELP_TEXT_FOOTER; CMD_DESC_QUEUE
  added (prerequisite for spec; full /queue handler on dgn911 branch).
  i18n/en.py + ko.py -- help_text replaced by help_text_header + help_text_footer;
  cmd_desc_* updated to owner-locked 2026-08-17 copy (stop/skills/resume/btw/
  authsync updated); cmd_desc_queue added. Logic ported from Metal live (7aece15);
  no structural gap vs canonical (spec pattern did not exist in canonical before).
- Vendor-rev: DGN-915 countdown END completion affordance (canonical-only;
  no OSS pin change; OSS backport pending): countdown.py -- CDN_DONE_PREFIX
  constant; _build_done_keyboard() single-button InlineKeyboardMarkup helper;
  final-edit split on self.completed (natural end emits affordance button +
  fail-soft fallback to plain done; cancel path stays plain done, no button);
  imports InlineKeyboardButton, InlineKeyboardMarkup. bot.py -- CDN_DONE_PREFIX
  imported from bridge.countdown; cdn:done: handler branch in _handle_callback
  (after usageretry: branch) calls edit_message_reply_markup(None) to clear the
  keyboard on tap; fail-soft on Telegram error; TODO(DGN-915) notes next-step
  gap. messages.py -- COUNTDOWN_DONE_BUTTON constant. i18n/en.py + i18n/ko.py
  -- countdown_done_button key ("Continue ▶" / "다음 ▶"). Logic ported from
  Metal live source (dec8eac); vendored tree had no prerequisite gap.
- Vendor-rev: DGN-891 tag-safe HTML split/balance + tag-stripped plain
  fallbacks (branch auto/dgn891-tag-balance; carry-back of live Metal
  commits 7119689 + f83fdd9; landed in OSS main 87ac8ca, pin bumped
  e0cb5c3 -> 87ac8ca this release -- DGN-905; DGN-806 countdown 5b492e6
  content already present via DGN-780/780b):
  formatting.py -- new balance_telegram_html / rebalance_html_chunks /
  html_to_plain_text / _scan_html_tags make every emitted Telegram HTML
  chunk independently valid (open tags closed LIFO, straddling spans
  re-opened with attributes across a split boundary, stray closers
  escaped not dropped [C1], plain fallback preserves link URLs [C2]);
  _render_fold_html balances before the 4096 fit check. bot.py --
  _send_text_body / _send_text_body_chat render-then-rebalance the chunk
  list and degrade to html_to_plain_text (never raw markdown / leaked
  tags); help-output + skill-list fallbacks tag-strip; _try_send_linked
  balance-guards the no-op compare. streaming.py -- send/edit/finalize
  fold helpers balance-guard before the HTML send; _fold_html_to_plain
  delegates to the shared helper. Logic ported (not blind-copied) onto
  the template's current files, which carry DGN-719/822/851/372 features
  the live Metal source lacks (all preserved). Tests:
  test_dgn891_tag_balance.py (27) + test_dgn376_html_prose.py fallback
  assertion update.
- Vendor-rev: DGN-759 /authsync command (canonical-only; no OSS pin change; OSS
  backport pending): bot.py -- _cmd_authsync handler added (reuses token-sync.sh
  from dogany-relogin-rebind skill; status exit0=MATCH/1=MISMATCH/2=ERROR/3=NOT-APPLICABLE;
  sync on MISMATCH; graceful no-op on non-macOS); _setup_handlers + _set_bot_commands
  wired. messages.py: CMD_DESC_AUTHSYNC + AUTHSYNC_* 8 constants. i18n/en.py +
  i18n/ko.py: authsync strings + help_text /authsync line. tests/test_dgn759_authsync_command.py:
  new (registration, access gate, all exit paths, timeout paths).
- Vendor-rev: DGN-902 /btw context-fork side conversation (branch
  auto/dgn902-btw-context-fork; canonical-only, no OSS pin change; OSS
  backport pending): btw.py -- new module (BtwForkState dataclass,
  BtwForkManager with LRU eviction cap _BTW_MAX_FORKS_PER_USER=10);
  fork client uses ClaudeAgentOptions(fork_session=True, resume=session_id)
  so fork reads main session history but writes to its own isolated session;
  run_fork_turn / _run_first_turn / _run_continuation_turn; per-fork asyncio
  lock prevents concurrent questions within one fork. bot.py -- _cmd_btw
  handler (access gate, empty-question guard, no-session guard; sends
  BTW_THINKING as reply-to anchor; registers BtwForkState; dispatches fork
  task via _track_user_task); _maybe_route_btw_reply (checks
  reply_to_message.message_id against _btw_forks table; returns True/claims
  when a known anchor is hit, routes continuation into the fork session,
  keeps main history uncontaminated); _btw_forks BtwForkManager wired in
  __init__; CommandHandler("btw") + BotCommand("btw") registered (after
  authsync, before help). messages.py: BTW_MARKER, BTW_NO_QUESTION,
  BTW_NO_SESSION, BTW_FORK_FAILED, BTW_THINKING, CMD_DESC_BTW. i18n/en.py +
  i18n/ko.py: btw marker (en "💭 btw" / ko "💭 (근데 있잖아)"), all btw_*
  strings, cmd_desc_btw, help_text /btw line.
  tests/test_dgn902_btw_command.py: 19 tests (registration x3, handler x5,
  reply-to routing x4, fork manager x4, message constants x3). Green
  19/19; 0 new failures in existing suite.
- Vendor-rev: DGN-888 watchdog vanished-label auto-recovery backstop (branch
  dgn888-watchdog-backstop; canonical-only, no OSS pin change; OSS backport
  pending): watchdog_setup.sh -- write_service_marker declares the registered
  bridge service plist (absolute path + Label, via existing plist_label) into
  $DATA_DIR/.service_plist (placeholder-guarded, non-fatal, launchd only).
  watchdog.sh -- when the launchd label is completely unregistered (bootout
  aftermath, 2026-08-15 18-min outage), a marker-gated recovery replaces the
  unconditional skip: marker plist must exist, carry no mint placeholders,
  and its Label key must equal --label exactly; attempt runs AFTER the
  RATE_MAX/RATE_WINDOW gate and lands in the RESTARTS ledger success or fail
  (record_attempt); sequence = launchctl enable first (bootout can leave
  disabled=true), then bootstrap, no kickstart (RunAtLoad=true would
  double-launch); bootstrap failure retries next cycle with a single
  marker-suppressed notify after 3 consecutive failures. No-marker path keeps
  the original skip; systemd/Linux branch behavior unchanged (functionally
  identical -- the shared clear_strike rm -f of macOS-only marker files is a
  no-op there).
- Vendor-rev: DGN-886 D4 carry-back (branch auto/dgn886-d4-upstream;
  canonical-only, no OSS pin change; OSS backport pending): sdk_bridge.py --
  user_has_streamed_output counts a grown fold bubble (fold_msg_id set) as
  streamed partial output (DGN-699 D4), matching the live Metal bridge; the
  DGN-721 full re-vendor ported the timeout-path D4 handling but missed this
  reader, so a fold-only dead turn showed the "message not processed" notice
  instead of the softer partial-output variant. Last live-only sdk_bridge
  delta blocking the DGN-886 de-preserve.
- Vendor-rev: DGN-881 thin-label passthrough + DGN-877/878 compose-footer
  subtraction (branch dgn-881-877-878-carryback; canonical-only, no OSS pin
  change; OSS backport pending): options.py -- sep-strip parsing layer
  (_LABEL_SEPARATORS split + _label_has_description heuristics) removed;
  labels pass through verbatim; overflow (>31 weighted) falls back to the
  localized number handle via new i18n key option_number_handle (ko "{n}번" /
  en "No.{n}"); i18n also adds option_rec_marker (ko "(추천)" / en "(rec)")
  as the doc-referenced body-side recommendation marker SSOT. bridge.md +
  telegram.md contract inversion: rec marker moves label -> body line;
  labels = bare short token. sdk_bridge.py -- DGN-877: compose-path fold
  subtraction runs against a pre-footer content snapshot (footer "\n" join
  merged the last paragraph boundary, leaking a narrated final paragraph
  into the fold); DGN-878: compose-empty fold log enumerates all 3 causes.
  Tests: test_dgn881_thin_label.py + test_dgn877_compose_footer_subtraction.py
  new; test_dgn704_label_shorten.py + test_dgn879_overflow_handle.py rewritten
  to the passthrough contract. 66 touched-area tests green; full suite: only
  the 3 pre-existing main failures (countdown/dgn429/dgn682), zero new.
- Vendor-rev: DGN-879 option button overflow -- no meaning-split (branch
  dgn-875-md-manifest; canonical-only, no OSS pin change; OSS backport pending):
  options.py _shorten_button_label -- remove _BUTTON_LABEL_HARD_CAP and the
  glyph-by-glyph raw-cut loop; after sep-strip, a label over the 31-weighted
  contract degrades to a pure "N번" handle (no mid-syllable cut, no meaning split);
  strip_consumed_options keeps the full label in the display body whenever any
  option degrades to a handle (body re-statement guarantee). Tests:
  test_dgn879_overflow_handle.py added; test_dgn704_label_shorten.py updated to the
  overflow->handle contract. Verified green in live bridge venv (116 pass). Zero
  new failures.
- Vendor-rev: DGN-876 fold-trim root relocation (branch dgn-876-fold-trim-canonical;
  canonical-only, no OSS pin change): sdk_bridge.py _finalize_result -- remove
  _final_fully_in_interim fast-path from both the grown-bubble path and the
  compose-fallback path; both paths now unify on _subtract_paras (DGN-777).
  Full duplication -> _subtract_paras returns [] -> fold deleted / no fold prepended
  (same clean-final outcome as before). Strict superset -> survivors kept as collapsed
  fold (bug fix: old fast-path deleted the whole fold even when interim had extra
  progress paragraphs). _final_fully_in_interim fully unused after the change; removed.
  Tests: test_dgn699_growing_fold.py -- TestFinalFullyInInterim removed (deleted
  method); TestDgn876FoldTrim added (cases A/B/C + DGN-832 Notification-0; 6 new
  tests). Zero new failures.
- Vendor-rev: v1.31.0 interim default lift suppress->fold (OSS backport
  landed e0cb5c3 / DGN-825): config.py _resolve_interim_mode fallback
  "suppress" -> "fold"; field comment updated;
  tests/test_dgn426_interim_suppression.py TestInterimSuppressionGate.setUp
  pins INTERIM_MODE="suppress" to isolate suppress-path tests from the new
  default; tests/test_dgn682_interim_fold.py test_unset_defaults_to_fold
  (renamed from test_unset_defaults_to_suppress).
- Vendor-rev: DGN-825 /kill complete removal follow-through (OSS backport
  landed e0cb5c3): bot.py _cmd_stop docstring dead /kill reference removed;
  i18n/en.py + i18n/ko.py indentation bug fixed (cmd_desc_model line);
  tests/test_dgn581_soft_interrupt.py test_kill_always_hard_stops removed +
  module/class docstrings updated.
- Vendor-rev: DGN-797 stop_interrupted UX copy + kill dead code (OSS
  backport landed e0cb5c3): i18n/ko.py stop_interrupted -> "진행하던 작업을
  멈췄습니다. 세션과 대화는 그대로입니다."; i18n/en.py stop_interrupted ->
  "Stopped what was running. Your session and conversation are intact.";
  cmd_desc_kill removed from i18n/ko.py + i18n/en.py + messages.py
  (CMD_DESC_KILL constant dropped); bot.py CommandHandler("kill") +
  BotCommand("kill") + _cmd_kill method removed.
- Vendor-rev: DGN-829 section_glyphs config field (canonical-only; no OSS pin
  change): config.py -- section_glyphs List[str] field (DOGANY_SECTION_GLYPHS
  env; default ["✅","📌","📋"]); _parse_section_glyphs validator splits on
  whitespace, non-empty tokens only, empty/all-whitespace -> default palette.
  Positional: 1st=summary, 2nd=detail, 3rd=try; fewer than 3 -> per-position
  default fallback in consumers (self-update.sh).
- Vendor-rev: DGN-816 footer stripper over-deletion fix (branch
  dgn816-footer-strip; vendored-only DGN-531 sidecar subsystem, no OSS pin
  change): sdk_bridge.py _FOOTER_BLOCK_RE tightened from anywhere-match +
  [^\[]* greedy eat (truncated user messages at any mid-body [라이브]/
  [결정대기] literal) to a line-start + message-tail anchored canonical-shape
  match (marker header lines + "- " bullets, legacy one-line form included;
  ^...\Z MULTILINE). Mid-body literals and [[OPTIONS]] preserved. Tests:
  tests/test_dgn816_footer_strip.py (21 cases: mid-body preservation,
  trailing strip, append-once, regex adversarial).
- Vendor-rev: DGN-834 resume-label inline merge (canonical-only; no OSS pin
  change): self_restart.sh -- new --resume-label TEXT flag; when RESUME_INTENT
  is set and no explicit --notice, the single restart-completion push is now
  "PREFIX 재시작 완료 — LABEL 이어서 진행합니다." (spec Option 1, owner-locked
  2026-08-12); RESUME_LABEL derived from explicit flag > first clause of
  RESUME_INTENT > "직전 작업" fallback; --resume-label forwarded through
  launcher ARGS to worker; drop_verify_spool: removed DGN-687 #4 owner-locked
  "↪ [직전 작업] 이어서 시작했어요." instruction (push now carries task name;
  second line would duplicate the signal); replaced with instruction to report
  resumed work directly without a separate resume notice line.
- Vendor-rev: DGN-851 fold-copy i18n decouple + dead outage copy removal
  (branch dgn851-lifekit-decouple; canonical-side, OSS backport pending):
  formatting.py -- FOLD_CAPTION_NORMAL/STOPPED/TIMEOUT, FOLD_TRUNCATION_LINE,
  _FOLD_OMISSION_LINE move to bridge/i18n catalogs (ko carries the LOCKED
  owner copy; en added) via new lazy fail-open _i18n(key, fallback) helper
  (guarded import keeps the module importable outside the venv for the
  push.sh sanitize hop, DGN-822 invariant; missing key/import failure
  degrades to the previous hardcoded ko literals, zero-delta). i18n/ko.py +
  i18n/en.py: +5 fold_* keys; outage_recovered removed (dead since the
  owner disabled the recovery push 2026-06-30). messages.py:
  OUTAGE_RECOVERED constant removed. bot.py: _notify_outage_recovered
  comment documents the removal + re-enable path. Tests:
  test_dgn699_growing_fold.py + test_dgn719_render_parity.py locked-copy
  pins re-anchored to the ko catalog; render parity asserted via the
  constants (locale-independent).
- Vendor-rev: DGN-822 F-1 regression guard (canonical-only test hygiene; no OSS
  pin change): tests/test_dgn822_formatting_telegram_free.py -- sys.meta_path
  blocker (_TelegramBlocker.find_spec) asserts bridge.formatting imports and
  executes sanitize_message_for_telegram without python-telegram-bot; OPTIONS_MARKER
  value pinned at [[OPTIONS]]; xfail contrast test documents that bridge.options
  IS telegram-dependent by design.  Blocker also purges any pre-seeded telegram
  mock from sys.modules for order-independence.
- Vendor-rev: DGN-822 shell-rail sanitize unification (branch
  dgn822-push-sanitize; canonical-side, OSS backport pending): formatting.py --
  _strip_md_thematic_breaks drops standalone --- / *** / ___ lines (3+
  repeats, whole-line only; wired into _prepass_structural, so every rail
  through markdown_to_telegram_html gets it; table separator rows and code
  segments untouched); new public sanitize_message_for_telegram(text) single
  entry point for out-of-band senders (routines/push.sh) -- splits code
  fences via split_into_segments, renders code via code_segment_html and
  prose via markdown_to_telegram_html, joins to one Telegram-safe HTML
  string. No new sanitize logic; reuses the conversation-rail pipeline.
  Follow-up (same branch): OPTIONS_MARKER moved options.py -> formatting.py
  (options.py re-imports it) so formatting.py imports without
  python-telegram-bot -- the push.sh sanitize hop must not silently degrade
  outside the bridge venv; self_restart.sh notify path drops html_esc
  pre-escaping + the deprecated --html flag (sanitizer owns entity escaping;
  whitelisted tags pass raw).
- Vendor-rev: DGN-835 usage-defer manual retry (branch
  dgn-835-usage-gate-v1; canonical-side, OSS backport pending): bot.py --
  shared core _usage_retry_run consumed by BOTH the usageretry:<label>
  callback branch (_handle_usage_retry_callback) and the /usageretry
  <label> CommandHandler (_cmd_usage_retry, DGN-841 A: STALE-gate-free
  slash route; group-0 registration keeps the catch-all skill forwarder
  from swallowing it) + _fetch_usage_json (claude-usage.sh --json
  preflight): label charset guard ([A-Za-z0-9._-]{1,128}), 7d>=99
  preflight blocks execution ("not enough" reply, keyboard kept on the
  button path), sufficient headroom launches
  ~/.dogany/usage-defer/<label>.replay DETACHED (start_new_session; never
  via process_message -- no live turn consumed; replay self-deletes on
  launch, written that way by cron-guard); import json added.
  messages.py + i18n/en.py + i18n/ko.py: 6 usage_retry_* keys
  (usage_retry_not_enough = owner-approved copy 2026-08-12). Tests:
  tests/test_dgn835_usage_retry.py (button + slash cases: traversal
  labels, missing replay, saturation block, lookup failure, detached
  launch, slash arg handling, threshold lockstep).
- Vendor-rev: DGN-805 countdown done-marker (branch dgn805-done-marker; OSS
  commit 752b7f1): Countdown.completed flag (False in __init__; True on the
  two natural-deadline breaks only -- remaining<=0 and next_target<=0;
  cancel/fail-open paths leave it False). CountdownDriver._emit_done(cid)
  writes <cid>.done {"ended_at": ISO} fail-open (OSError -> pass).
  _tick step-1 calls _emit_done before reaping when countdown.completed;
  cancel and fail-open paths emit nothing. Docstring updated. Import added:
  from datetime import datetime, timezone. Tests: 50 passed (11 new cases
  covering completed flag, emit, cancel/fail-open, fail-open OSError).
- Vendor-rev: DGN-801 fast-path interceptor (branch dgn-801-bridge-interceptor;
  canonical-side, OSS backport pending): NEW fastpath.py -- domain-agnostic
  runner for a configured handler executable (`<handler> handle --raw
  "<text>"`; exit0=processed/stdout=rendered body/sole commit witness,
  exit2=FALLBACK, else/timeout/crash=fail-safe SDK turn); handler spawned in
  its own process group, timeout/cancel kills the WHOLE group
  (SIGTERM->grace->SIGKILL) and reaps. bot.py -- _try_fastpath wired in
  _handle_text_message before _enqueue_text_task, fires only under the user
  queue lock when idle and registers in _user_run_tasks (concurrent messages
  buffer via DGN-616 coalescing); _fastpath_push_guaranteed (retry + death
  notice, never model fallback post-commit). config.py -- FASTPATH_HANDLER
  (.telegram_bot/.env layer, PROJECT_ROOT-relative, DEFAULT OFF => fully
  inert) + FASTPATH_TIMEOUT (3s). i18n ko/en + messages.py:
  fastpath_push_failed key. Bridge never writes handler-side markers
  (files/program/.tip-pending read-only). Tests:
  tests/test_dgn801_fastpath.py (30 cases incl. real-subprocess
  process-group reap on timeout AND cancellation).
  Follow-up (same branch, B1+B2): fastpath.py -- timeout path re-checks
  proc.returncode after the process-group kill and RECOVERS a committed exit0
  from the SIGTERM grace window (Skull F3 post-commit exit0 degrade landing
  mid-timeout): processed with no body -> M3 death-notice, never a model
  fallback (mechanically seals the next-slot double-log; no longer relies on
  UPSERT idempotence). Genuine (non-zero) timeouts still FALLBACK.
  .env.example -- FASTPATH_HANDLER + FASTPATH_TIMEOUT documented (default OFF /
  3.0s; env-key<->config-constant relation noted). Tests +3 (grace-window
  exit0 recovery unit + end-to-end no-model-fallback + genuine-timeout still
  falls back).
  Follow-up (same branch, B4 Option 2): bot.py --
  _maybe_capture_outside_approval now returns bool (True while a live,
  non-expired outside-approval prompt was pending at message time), and
  _handle_text_message gates fast-path on "not approval_pending". Seals the
  A1 double-consumption: while an outside-approval prompt pends, a bare
  numeric token ("1") that also grants the approval is NOT additionally parsed
  as a fast-path set (Skull kept bare-numeric a valid fast-path input, so
  Option 1 -- FALLBACK on bare-numeric -- was rejected; ambiguity is resolved
  on the approval side, which only the bridge knows). Callback caller ignores
  the new return (unchanged). Tests +4 (capture-returns-true while pending for
  allow/deny/non-decision; returns-false when no-prompt/expired; full-path
  approval+active-session skips fast-path with no double-consume; no-approval
  bare "1" still fast-paths).
- Vendor-rev: DGN-790 button output integrated fix (branch dgn790-button; OSS
  main pushed): options.py -- 779 part1 already present; part2 removes manual
  "..." truncation (Telegram client handles display-side ellipsis) behind a new
  _BUTTON_LABEL_HARD_CAP (40.0) runaway safeguard, _BUTTON_LABEL_MAX_WIDTH (31.0)
  redeclared as the generation-contract value; colon removed from
  _LABEL_SEPARATORS (Korean labels mis-split on ":"); DGN-720 auto-split keeps a
  description-bearing option run in the display body (visible above the buttons)
  via a shared _label_has_description predicate reused by the button shortener
  (704c head-guard respected). Owner device PII scrubbed from public-bound
  comments. tests/test_dgn704_label_shorten.py rewritten (53 passed).
- Vendor-rev: DGN-780 countdown snap + UI redesign (branch dgn780-countdown;
  canonical-side, OSS backport pending): countdown.py edit loop moves from a
  fixed cadence nap to deadline-anchored boundary snap (_next_boundary) so
  editMessageText latency no longer accumulates into display drift; bar
  redesigned to DRAIN (filled = remaining) with hourglass/check icons and a
  validated glyph allowlist (dot default / block-line / square) behind new
  config knob COUNTDOWN_GLYPH_SET (config.py); i18n countdown_body/
  countdown_done templates updated (ko/en); tests extended
  (tests/test_countdown.py). DGN-780b free-form extension: start_countdown /
  Countdown / render_countdown gain optional icon/done_icon/glyph params with
  a single priority resolver (call > config > default) and silent safe
  fallback on markdown-risk/unsafe values; i18n templates carry {icon}/
  {done_icon} placeholders; control-file schema adds optional icon/done_icon/
  glyph fields (glyph = preset name or [filled, empty] pair). Backward
  compatible: omitting the params reproduces the prior default look.
- Vendor-rev: re-vendor v1.27.3 candidate (branch revendor-1273; OSS main pushed
  8ace92b): 3-way reconcile of the OSS increment ce3b597..8ace92b onto the
  vendored tree. New files added verbatim: countdown.py, edit_guard.py,
  tests/test_countdown.py, tests/test_edit_guard.py, tests/test_dashboard.py.
  Clean-reflect (canonical was byte-identical to pin ce3b597): dashboard.py,
  formatting.py, options.py, tests/test_dgn704_label_shorten.py. Per-file 3-way
  (OSS additive-only, non-overlapping with vendored-only subsystems -- vendored
  drift preserved intact): bot.py (DGN-594 CountdownDriver wiring: import + task
  create + finally cancel), messages.py + i18n/en.py + i18n/ko.py (DGN-594
  countdown_body/countdown_done keys), self_restart.sh (DGN-706 resume-intent
  step-4 branch + DGN-706b version-update auto-notice/html_esc/VER_MARKER;
  vendored-only DGN-712 smoke gate + __AGENT_NAME__/__AGENT_PREFIX__ template
  placeholders + DGN-687/233 Korean persona notices all preserved).
- Vendor-rev: DGN-779 button label width coefficient fix (canonical-only; no OSS
  pin change): options.py -- _label_width whitespace branch (isspace()->0.4,
  was 1.0; on-device ~0.25, overcounted 2.7x); same three-way branch in the
  _shorten_button_label trim loop for consistency; _BUTTON_LABEL_MAX_WIDTH
  30.0->31.0 (on-device 1-line boundary ~31.5, -1.0 ellipsis reserve). Test
  update: budget assertion 30.0->31.0, exact-width fixture corrected (29.4),
  three new whitespace-weight cases added.
- Vendor-rev: DGN-760 hermetic conftest (canonical-only test hygiene; no OSS
  pin change): unconditionally force PROJECT_ROOT to throwaway tmpdir and pin
  INTERIM_MODE/OUTPUT_LANG_GUARD/BRIDGE_REGISTER_GUARD/BRIDGE_SCAFFOLD_GUARD/
  STREAM_INTERIM to canonical defaults in tests/conftest.py -- eliminates
  false-fail from live shell env leak in release preflight.
- Vendor-rev: DGN-721 full re-vendor v1.27.0 candidate (branch
  dgn721-fullrevendor, local -- OSS push pending owner approval): 3-way
  reconcile against pin ce3b597 completing the DGN-719 partial re-vendor.
  Ported from OSS: DGN-682/699/710 growing-fold wiring (sdk_bridge
  fold_msg_id/fold_buf + reader-loop interim capture + finalize lifecycle
  hooks on all termination paths + compose-fallback dedup; config
  INTERIM_MODE + FOLD_UPDATE_INTERVAL; streaming send/edit/finalize_fold_html
  helpers; fold system-prompt fragment gated into _compose_system_prompt),
  DGN-581 soft interrupt (/stop soft-first + /kill hard teardown +
  sdk_bridge.interrupt + discard_results swallow), reader-crash CLI
  force-kill (FixC), tests test_dgn699_growing_fold.py +
  test_dgn581_soft_interrupt.py. Vendored-only subsystems preserved intact
  and merged function-level where they overlap OSS code: DGN-163 turn-death
  probe (user_has_streamed_output), DGN-531 footer sidecar, DGN-616
  coalescing, DGN-670 flake recovery, DGN-686/376/429 register+language
  guards, queue_bundling, turn_death, DGN-665 bot.py strip sites, image_send.
  Residual known drift vs OSS (deliberate, non-ported): DGN-762 _selfcheck
  cross-module constant resolution (__main__.py AST scan of messages.<NAME>
  refs) is vendored-only (OSS backport pending); DGN-665 strip sites
  remain vendored-only (OSS backport pending); DGN-706/706b resume-intent is
  NOT in pin ce3b597 (OSS branch dgn-706-706b-backport, unmerged); OSS
  /history command + model-facing English photo/doc prompt constants +
  DGN-351 split-merge / DGN-330 conflict-backoff bot.py areas stay
  OSS-side-only pending a later reconcile decision.
  Prior Vendor-rev history:
  DGN-719 re-vendor (branch dgn719-revendor-v127): formatting.py to OSS
  parity (DGN-682 interim fold + DGN-372 legacy-markdown bracket escape +
  DGN-719 phase-1 output contract a1-a4 with dec-108 adjustments);
  options.py bidirectional drift reconciled (DGN-665/704/704b),
  DGN-399 (ensure_owner_stream bootstrap; pushed OSS),
  DGN-460 (max_buffer_size env-configurable, CLAUDE_MAX_BUFFER_SIZE default
  16MB, fixes 1MB SDK transport buffer crash on large tool results; pushed
  OSS 01764ee),
  DGN-541 (dashboard.py debounced empty-delete pin state machine; OSS commit
  883841f, local -- OSS push pending owner approval),
  DGN-555 (selective reply-linking; pushed OSS 7dedc41),
  DGN-558 (DGN-426 C-strict interim suppression ported from OSS c2d64a4:
  STREAM_INTERIM config + stop_reason gate pair; resolves the previously
  noted STREAM_INTERIM drift vs OSS HEAD).

## Why vendored instead of a git submodule

A submodule would leave `bridge/` empty on a plain `git clone` (without
`--recursive`), which breaks the "self-contained, runs when cloned standalone"
goal of this repo. The bridge is therefore vendored. Submodule wiring is
deferred; if this repo later wants the bridge as a submodule, remove this
directory and run:

    git submodule add https://github.com/coolcoolk/claude-code-telegram bridge
    cd bridge && git checkout feca63efc507f820774be6be036aa1695113c950

To refresh the vendored copy from upstream, re-copy the upstream tree over this
directory (excluding any real `.env`, `venv/`, `__pycache__/`).

## Pin discipline (DGN-385 MAJOR-1)

Any canonical change to files under `bridge/` in this repo MUST bump the
"Pinned commit" line above (or add a `Vendor-rev: <marker>` line in the same
commit).

Rationale: `update.sh` detects whether an instance's bridge is "locally ahead"
by comparing the pin in the instance's `bridge/UPSTREAM.md` with the pin in
this template file.  If the pins are equal and rsync shows a diff, the script
concludes the instance has local patches and skips the rsync to avoid a silent
downgrade.  An unbumped canonical bridge change therefore makes every instance
misread the update as local drift and silently skip it -- the fix never lands.
Always bump the pin (or add a `Vendor-rev` marker) in the same commit as the
bridge change.

DGN-593 note: the pin/Vendor-rev markers above are PROVENANCE documentation
only -- the update landing gate is now the per-file manifest 3-way reconcile
(`.claude/.dogany-bridge.sha`), not marker comparison.
