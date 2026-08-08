# bridge upstream

This `bridge/` directory is a VENDORED (in-tree) copy of the standalone Telegram
<-> Claude bridge so that `dogany-agent` runs immediately after a plain
`git clone` with no extra `--recursive` step.

- Upstream: https://github.com/coolcoolk/claude-code-telegram
- Pinned commit: ad56e243f384f54867dd5191e955a76e6127e2ce
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
