# bridge upstream

This `bridge/` directory is a VENDORED (in-tree) copy of the standalone Telegram
<-> Claude bridge so that `dogany-agent` runs immediately after a plain
`git clone` with no extra `--recursive` step.

- Upstream: https://github.com/coolcoolk/claude-code-telegram
- Pinned commit: e0cb5c3fa7f405b29ab2a33e509994af5fe9870a
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
