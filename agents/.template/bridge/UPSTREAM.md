# bridge upstream

This `bridge/` directory is a VENDORED (in-tree) copy of the standalone Telegram
<-> Claude bridge so that `dogany-agent` runs immediately after a plain
`git clone` with no extra `--recursive` step.

- Upstream: https://github.com/coolcoolk/claude-code-telegram
- Pinned commit: ce3b597615ba15d784f0258f5343ef5060ac860b
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
  Residual known drift vs OSS (deliberate, non-ported): DGN-665 strip sites
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
