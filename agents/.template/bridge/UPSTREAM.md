# bridge upstream

This `bridge/` directory is a VENDORED (in-tree) copy of the standalone Telegram
<-> Claude bridge so that `dogany-agent` runs immediately after a plain
`git clone` with no extra `--recursive` step.

- Upstream: https://github.com/coolcoolk/claude-code-telegram
- Pinned commit: 2c18f0356070b7cd5725c32f063b5e5bdc88a8d6
- Vendor-rev: v2.0.0 cut -- pin advanced 87ac8ca -> 2c18f0356070b7cd5725c32f063b5e5bdc88a8d6 (3 OSS commits) with
  ZERO code carried across, because the code was already here. Evidence:
  (a) `9d46903` session-inbox UTF-8 poison-pill quarantine -- the fix ORIGINATED
      canonical-side and was published outward. Verified at cut time: the
      poison-pill block in bot.py is byte-identical between the two trees, and
      bridge/tests/test_session_inbox_utf8_poison_pill.py is identical whole-file.
  (b) `e0c5aa3` is that fix's merge commit.
  (c) `2c18f03` is the OSS 1.1.0 version stamp (VERSION + CHANGELOG +
      __version__). It belongs to the OSS product's OWN version line, which
      DGN-818 C3 deliberately split from canonical's -- canonical carries
      `1.0.0+dogany.DGN-818-C3`. Importing it would re-merge two identities
      C3 just separated.
  bot.py differs by ~4,057 lines between the trees; that is the canonical-only
  reverse delta C4 documents, NOT unshipped upstream work.
  ** This pin move is a provenance correction, not a code vendor. ** A future
  pin move that DOES carry code must say so here, or the two cases become
  indistinguishable in this file.
- Vendor-rev: DGN-818-C3 -- ownership declaration + injection contract +
  version identity (canonical-only; no OSS pin move). Three landings share
  this marker because they are one cut surface (DGN-818-DESIGN section 6.1
  C1/C2/C3):
  (C1) the ownership 3-layer table and its machine-readable registry are
  declared BELOW in "Ownership layers"; the unit is the DECLARATION, not the
  file, because the mixing is inside files -- 229 estate-marker code lines in
  108 declarations across 13 of 28 shippable modules. Enforced by
  tests/dgn818_ownership_lint.py.
  (C2) `vendors/custom.telegram.md` -- a name update.sh had reserved as
  instance-owned since DGN-773 T5 with ZERO readers, while the shipped
  contract doc's prose denied any overlay existed -- now has a reader:
  `sdk_bridge._load_vendor_overlay()` (fail-OPEN when absent or blank; the
  canonical contract's fail-CLOSED direction is untouched). The heading
  collision lint went from a hardcoded 2 injectables to all pairs of N. The
  model-facing prompt no longer carries estate ticket IDs: the injected
  headings "## Subagent Task Delegation (DGN-086)" (i18n en/ko) and
  "## Progress Narration vs Final Answer (DGN-699)" (messages.py) lost their
  parentheticals -- measured 2 -> 0 occurrences of `DGN-\d+` in the composed
  system prompt. Section titles are unchanged, so the vendor-doc heading lint
  and the DGN-699 fold tests still match.
  (C3) `__version__` was "1.0.0" with zero consumers while OSS minted
  "1.1.0". It is now composed from `__oss_base__` + `__oss_pin__` +
  `__vendor_rev__` as a PEP 440 local version ("1.0.0+dogany.DGN-818-C3"),
  and it has two consumers: the boot snapshot (`bridge_version`, ADDITIVE at
  schema 1 -- see boot_snapshot's module docstring for why bumping the number
  would have been the harmful choice) and /health's technical fold. The pin
  and Vendor-rev markers, which this file describes as "PROVENANCE
  documentation only", are now read by a machine: `__oss_pin__` must equal
  the Pinned commit line above and `__vendor_rev__` must appear as a marker,
  asserted by bridge/tests/test_dgn818_version_identity.py.
- Vendor-rev: DGN-1141 stage 8 -- comment-only correction + one shipped-doc
  assertion (canonical-only; no OSS pin move; zero behavior change):
  `_load_vendor_contract()`'s docstring justified live re-read with "so an
  owner edit to the contract doc reaches the NEXT session", but
  vendors/telegram.md is framework-owned and update.sh reverts hand edits --
  the stated reason held for one session and then stopped being true (M11
  MAJOR-6). Rewritten to name the refresh that actually reaches a running
  bridge (a self-update landing new doc bytes). Paired with a new test class
  asserting the SHIPPED contract doc declares framework ownership, since
  vendors/ carries no ownership token in its filenames and therefore no
  implicit warning.
- Vendor-rev: DGN-1141 stage 5 -- comment-only sweep for the doc relayout
  (canonical-only; no OSS pin move; zero behavior change): comments that
  cited the retired doc layer (bridge.md deleted; telegram.md rewritten and
  moved to vendors/telegram.md, injection-only) now point at the vendor
  contract or the injected grammar fragment (options.py thin-label note,
  bot.py code+OPTIONS note, i18n option_rec_marker SSOT notes en/ko, en.py
  stage-5 tense fix).
- Vendor-rev: DGN-1141 stage 4 -- vendor contract injection + machine
  grammar fragment (canonical-only; no OSS pin move). (1) i18n
  `system_prompt` (en/ko, identical English by contract) gains a
  '## Choice Buttons ([[OPTIONS]] marker)' section teaching the parser's
  ACTUAL grammar (3 label sources + priority, fence no-arm, code/table
  separation, bundle-level width degrade keyed to the reconstructed
  'N. label' line at 31 weighted units, zero-label fail signal) and the
  send_file section gains the enforced delivery rules (attach-from-disk
  at send time, bare-path ignore, silent >=10MB/absent skip, outside-root
  confirm vs background omit, fence-unguarded detection warning). Every
  sentence is written from code behavior (options.py / formatting.py /
  bot.py), never copied from the doc layer -- the docs carried 9 drift
  findings (DGN-1141-M7 sec. 2); this fragment is the single code-owned
  grammar source the stage-5 doc retirement depends on. A lockstep test
  pins the taught numbers to the parser constants. (2) sdk_bridge grows
  the vendor selector: _load_vendor_contract() reads
  PROJECT_ROOT/vendors/<vendor>.md fresh per compose; vendors/ layer
  absent = inject nothing (fail-open default, pre-stage-5 world);
  declared vendor file missing/empty = VendorContractMissing raised and
  promoted to a bridge BOOT die by a module-level validation call (same
  seam as pydantic config validation; subprocess-proven exit!=0, !=127,
  with positive controls). _compose_system_prompt() fixes the injectable
  order (vendor doc + i18n machine fragment + fold/language) and lints
  '## ' heading collisions between vendor doc and machine fragment
  (log-warn). ESTATE-COUPLING NOTE: the vendors/ contract-doc layer and
  the "spawner declares the vendor" design are estate decisions
  (DGN-1141-M2/M7); an OSS back-land should carry the selector as an
  optional, default-off system-prompt extension file hook.
  move). (O1) The BRIDGE_INFLIGHT_INTERRUPT_NOTICE opt-in now carries a
  DEDICATED auto-interrupt copy (i18n `auto_interrupt_notice`, DRAFT --
  owner confirmation pending) instead of reusing the /stop copy; the
  default stays OFF -- the 2026-08-17 owner decision (silence =
  conversational naturalness) governs the "your message paused the turn"
  notice only, and confirmed subagent deaths already surface
  unconditionally via the DGN-1015 kill notice (copy owner-approved
  2026-08-24), so flipping the default is NOT taken. Stale "DRAFT"
  comments above the approved DGN-1015 copy corrected to record the
  approval. (O2) No mechanism change -- the trigger=stop/auto tag landed
  with the DGN-1016 guard; this rev adds real-judge log assertions
  driving both origins end-to-end. (O3) First use of the SDK's per-task
  stop control request: SdkBridge.stop_task(user_id, task_id) (SDK
  client.py:450 stop_task, present in 0.2.110 and 0.2.119; degrade to
  False + boot-style WARN on older SDKs; registry cleanup rides the
  CLI's terminal "stopped" notification through _track_task_lifecycle,
  never eager). Command-surface exposure (naming, menu seat, list/pick
  copy) is owner-gated UX and deliberately NOT wired in this rev --
  design draft in the dispatch report.
- Vendor-rev: DGN-1139 -- comment-prose only, zero code change: the
  DGN-1123 ARM_SUBDIR contract comment in artifacts.py (lines 44-46) named
  estate topology (instance persona names on both sides of the arm
  contract), which publish.sh gate S (estate-scrub, DGN-1102) correctly
  flags as a NEW public exposure (the file landed after the estate-scrub
  baseline cut, so it has no grandfather entry, and baseline additions for
  new content are forbidden by gate discipline). Rewritten generically as
  producer<->consumer / consumer-side (bridge) with the technical content
  intact: the contract file stays the SINGLE cross-side surface, this tuple
  stays the consumer-side declaration, and the arm producer still pins the
  same literal path independently. No behavior, test, or OSS pin change.
  (canonical-only; no OSS pin change; the Haiku classifier gate is the
  Dogany-side auto-injection surface). Incident (2026-08-27 morning, live
  workout session): a prep step offered exactly ONE action ("1. 완료") and
  the model forgot to hand-author the [[OPTIONS]] marker; has_numbered_list's
  >=2 threshold (kept conservative since DGN-325 -- its test docstring states
  the rationale: "to avoid false positives on incidental single-item lists")
  meant the classifier never ran, ZERO buttons rendered, and the owner had to
  type the answer mid-workout. The marker-authoring convention in the skills
  was prose filling a bridge threshold gap -- prose gets forgotten. Fix:
  options.py -- new has_single_trailing_option(): True ONLY for the one-item
  decision-menu shape (outside code fences: exactly one numbered line, it is
  a "1." item, and it is the LAST non-blank prose line -- a menu awaits the
  pick at the end; an incidental "1." inside running prose is followed by
  more text). sdk_bridge.py -- _maybe_mark_options gate widened to
  `has_numbered_list OR has_single_trailing_option`; the Haiku classifier
  (prompt unchanged, "wrong button is worse than a missing one") remains the
  actual judge -- the predicate is a structural pre-filter, not a new
  judgment heuristic. Scope guards: the has_options OR-arms (proactive flush
  / model-turn finalize) still use the >=2 has_numbered_list, so marker-less
  non-injected content keeps its exact pre-fix force_options behavior;
  authored markers (bare AND labeled) still suppress the classifier (zero
  spend); classifier-injected provenance (DGN-665 body-keep) unchanged.
  H17 boundary preserved by construction: an engine AUTO_ADVANCE seat is
  resolved at the SKILL layer (the token is consumed and the action executes
  immediately -- no numbered line ever reaches the bridge), so the bridge
  only ever classifies seats WITHOUT an engine signal -- exactly the seats
  that need buttons; nothing here re-introduces single-item menus into
  AUTO_ADVANCE seats. Verification (detection-proof-first per DGN-1122
  discipline): pre-fix repro through the REAL finalize path with the
  classifier mocked always-yes produced classifier_calls=0 / keyboards=0
  (the defect is detectable); post-fix the same harness produces
  classifier_calls=1 / one "1. 완료" button. False-positive evidence:
  3 synthetic prose samples with an incidental single numbered line are
  rejected by the DETERMINISTIC gate (zero classifier calls); 3 further
  shape-eligible prose samples (trailing "1." line that is narration, not a
  menu) were judged "no" by the REAL Haiku classifier in a live probe, and
  the incident menu judged "yes" -- 0 false buttons in 6/6 samples.
  Classifier-cost bound: added Haiku calls are confined to replies whose
  final non-blank line is a lone unfenced "1." item with no marker present
  (approximately the single-option-menu class itself -- the calls that
  SHOULD have been happening); a plain threshold->1 change would instead
  have fired on every reply containing any single numbered line anywhere,
  a strictly larger surface, and was rejected. Tests:
  tests/test_dgn1128_single_trailing_option.py (21: predicate unit matrix
  incl. fence/count/ordinal/trailing negatives; classifier-gate spend
  accounting incl. authored bare+labeled suppression and the >=2 path
  unchanged; finalize + proactive end-to-end into the real render seat;
  false-positive samples asserted at zero calls AND zero keyboards).
  Full bridge/tests/ suite: 1552 passed (1531 baseline + 21 new), 1 xfailed,
  same 1 pre-existing unrelated failure as baseline
  (test_dgn682_interim_fold.py D7 reader-loop timeout, reproduced
  identically pre-change this run), zero new failures.
- Vendor-rev: DGN-1123 -- arm path contract from constant to assertion
  (canonical-only; no OSS pin change; the idrill arm surface is a
  Dogany-only primitive, DGN-918/939/966, absent upstream). The arm state
  path existed in FOUR copies with opposite failure directions: reads went
  through artifacts (artifacts.ARM_SUBDIR -> read_arm), but bot.py's
  consume (_idrill_consume_arm) and write (_idrill_write_arm) helpers each
  held their own `PROJECT_ROOT / "files" / "program" / ".idrill-arm"`
  literal, plus a docstring prose copy -- so a relocation that moved the
  artifacts constant would keep buttons WORKING (read path) while consume
  silently no-opped (`except OSError: pass` labeled "converged"), leaving
  every fired arm on disk forever with zero symptoms. Changes: artifacts.py
  -- ARM_SUBDIR promoted from a one-line comment to a declared contract
  surface (what is bound to it, what must move together, pointer to
  bridge/IDRILL-ARM-CONTRACT.md section 1) with an IMPORT-TIME depth-2
  check (3 components ending '.idrill-arm'; a deeper relocation now fails
  at boot instead of silently breaking every `../../` consumer reference);
  new arm_path_for() containment resolver shared by read/consume/write;
  new consume_arm() -- the destructive twin of read_arm: True = removed by
  this call, False = already absent (converged), any OTHER OSError raises
  after an ERROR log (a containment escape on a delete also raises, never
  "converges"). bot.py -- both path literals and the docstring path prose
  replaced by artifacts routing; _idrill_fire_final's consume step now
  three-way: removed -> fire; already-absent -> IDRILL_ARM_EXPIRED, no
  fire (completes the double-tap guard the blanket except left open: the
  concurrent tap that actually consumed owns the fire); unlink failure ->
  logger.exception + IDRILL_ERROR + fire ABORTED (firing with the arm
  still on disk would break the no-double-submission invariant). Tests:
  tests/test_dgn1123_arm_contract.py (11: detection-proof-first ordering
  per DGN-1122 discipline -- the content-based consumption probe is proven
  able to SEE an unconsumed arm before any test claims consumption;
  consume/converge/raise matrix; fire-abort on injected chmod path error;
  race-window expire; no-arm-literal-in-bot.py source lock; ARM_SUBDIR <->
  contract-md prose binding; depth-2 guard presence). Mutation-verified:
  a silent no-op consume_arm mutant fails 6 of 11; a 4-component
  ARM_SUBDIR fails at import. Companion gate outside bridge/:
  scripts/tests/test-arm-state-installer-disjoint.sh turns "pack_install.sh
  never addresses files/program" from an unchecked absence into a checked
  invariant (seeded-violation detection proof first, then zero-hit +
  files/_archive allowlist; static lexical gate, variable-indirection
  limit stated in-file). Full bridge/tests/ suite: 1531 passed + 11 new,
  1 xfailed, same 1 pre-existing unrelated failure as baseline
  (test_dgn682_interim_fold.py D7 reader-loop, reproduced identically on
  pristine HEAD files this run), zero new failures.
- Vendor-rev: DGN-1092 back-land -- button label all-or-nothing degrade
  (canonical-only; no OSS pin change; back-land of live Metal merge, dev-crew
  commit dd56de17). Before this fix, build_option_keyboard's DGN-881 degrade
  decision was PER-BUTTON: only the options whose reconstructed "{i}. {label}"
  exceeded _BUTTON_LABEL_MAX_WIDTH fell back to a localized number handle
  (ko "N번" / en "No.N"); short options in the same keyboard kept their full
  "{i}. {label}" text. That let one keyboard mix full labels and number
  handles, which is confusing (the owner sees "1번" next to "2. 대기" with no
  visible reason the two buttons differ). Canon's options.py was BYTE-IDENTICAL
  to the pre-fix Metal baseline (verified: diff empty before this carry), so
  the same bug was live here too. Fix (verbatim carry, no redesign):
  build_option_keyboard now runs two passes -- (1) reconstructs every
  "{i}. {label}" up front and checks each with the existing
  _overflows_to_handle (unchanged); (2) if ANY one overflows, EVERY button in
  that keyboard renders as a number handle; if NONE overflow, every button
  keeps its full label. The single-label _shorten_button_label API is kept
  for callers needing a per-label decision in isolation (e.g. unit tests) but
  is no longer called by build_option_keyboard's bundle decision; its number-
  handle text generation now shares a new _number_handle_label(number) helper
  with the bundle path instead of duplicating the t("option_number_handle")
  format call. Untouched by design (ticket instruction): callback_data
  generation (still "opt:{i}. {label}" with the "opt:{i}" 64-byte fallback,
  unaffected by which text renders on the button), resolve_choice (index-based
  resolution, never reads button text), and strip_consumed_options (already
  keyed off an any()-bundle check, DGN-992, so it was already consistent with
  the all-or-nothing shape this fix brings to the button text itself). Tests:
  test_dgn881_thin_label.py (mixed-run cases rewritten from "only the
  overflowing option degrades" to "any overflow degrades all"; a new
  test_mixed_run_all_passthrough_when_none_overflow case added), test_
  dgn992_marker_label_buttons.py (test_overflow_label_degrades_to_number_handle
  updated: the second, short-label button now also asserts as a number
  handle instead of "2. 짧은 라벨"), test_dgn704_label_shorten.py
  (test_overflow_option_button_is_handle: second button now "2번" instead of
  "2. 대기") -- carried verbatim from the Metal merge, no local edits beyond
  the copy. Full bridge/tests/ suite: 1520 passed, 1 xfailed, same 1
  pre-existing unrelated failure as baseline (test_dgn682_interim_fold.py D7
  reader-loop timeout, Python 3.14 asyncio-timeout flake, documented
  pre-existing throughout this file's history), zero new failures.
- Vendor-rev: interrupt-fold -- auto-interrupt no longer deletes the
  streamed answer draft (canonical-only; no OSS pin change). On a DGN-911
  debounce auto-interrupt (a new message cut the in-flight turn short),
  `SdkBridge.interrupt()` called `StreamingMessageHandler.finalize_all()`,
  which froze the plain-text draft bubble in its last live form with no
  indication it was cut off -- and `StreamingMessageHandler.cancel()` (used
  by /new and the /stop hard-teardown fallback) deleted it outright with no
  trace. Neither matched the growing dev-agent fold's existing behavior
  (`_fold_finalize` + FOLD_CAPTION_STOPPED already collapses that surface
  with a "중단됨" caption instead of deleting it). Fix: `cancel()` gained an
  optional `fold_caption` param -- when given, it collapses the accumulated
  draft into ONE expandable fold quote via the existing
  render_fold_final/finalize_fold_html swap instead of deleting (an
  empty/placeholder-only draft still falls through to delete, so no empty
  quote is ever left behind); `interrupt()` passes it only for
  `trigger="auto"`. `trigger="stop"` (explicit /stop) keeps calling
  `finalize_all()` unchanged, and the `cancel()` default (`fold_caption=
  None`, used by /new and the hard-teardown fallback) still deletes --
  neither path's UX was in scope. Caption text
  (`formatting.INTERRUPT_FOLD_CAPTION` / i18n `interrupt_fold_caption`) is
  an UNCONFIRMED placeholder pending owner sign-off, not locked copy.
- Vendor-rev: DGN-1059 locale byte-truncation fix (canonical-only; no OSS pin
  change). self_restart.sh's `--verify` path pipes a headless-claude response
  (can be Korean) through `head -c 800`, a byte-count truncation regardless
  of locale, with no UTF-8 locale ever exported in the script -- under
  cron/launchd (LANG/LC_ALL unset -> C locale) a multi-byte Hangul character
  gets sliced in half and the notify body carries invalid UTF-8. Fix: same
  candidate-probe pattern already landed in routines/push.sh and
  routines/self-update.sh for this exact defect class -- probe
  en_US.UTF-8/C.UTF-8, export LC_ALL/LANG to the first whose charmap is
  really UTF-8 (WARN, non-fatal, if neither is available), and replace
  `head -c 800` with `cut -c1-800` (POSIX character count under a UTF-8
  locale; head -c is byte-count unconditionally, so locale alone would not
  have fixed it). Verified under `env -i` (no locale at all): pre-fix
  reproduces UnicodeDecodeError on a Korean verify response, post-fix
  preserves it intact; ASCII verify output unchanged (cut -c adds one
  trailing newline vs head -c on a multi-line-terminated input -- a
  pre-existing side effect of the same cut -c substitution already accepted
  in self-update.sh, not new here). No OSS source touched; canonical-only
  since the upstream project has no Korean-locale caller.
- Vendor-rev: test-only carry -- 2 of the 3 DGN-1016-flagged unbacklanded
  Metal test files (canonical-only; no OSS pin change; no source-code
  change, the fixes under test already landed in canonical). DGN-1016's
  worklog listed three metal-only test files never carried:
  test_dgn919_command_menu_spec.py, test_dgn922_release_gate_fixes.py,
  test_options_code_block.py. Cross-checked before carry: (1)
  test_dgn919_command_menu_spec.py was ALREADY landed and is actively
  NEWER in canonical (DGN-986 [c], same day) -- canonical's copy locks
  the 11-command menu (DGN-986 D1 health insertion + DGN-1050 authsync
  retirement + DGN-997 restart), while Metal's own working copy is still
  on the stale 10-command lock; carrying it back would have been a
  regression, so it was left untouched. (2)
  test_dgn922_release_gate_fixes.py (7 tests: soft-stop ghost-turn
  discard, cdn:done stale-gate bypass, long-btw-response split +
  plain-text degrade, fork-task isolation from _user_run_tasks) -- the
  bot.py fixes under test (_track_btw_fork_task, skip_stale, the FIX
  1-4 DGN-922 comments) were already present in canonical; only the test
  file itself was missing. Carried verbatim, 7 passed. (3)
  test_options_code_block.py (11 tests: extract_options/has_numbered_list
  must ignore fenced-code-block numbering, DGN-085) -- bridge/options.py
  is byte-identical between Metal and canonical (diff empty); only the
  test file was missing. Carried verbatim, 11 passed. Scanned both files
  for metal-local identity/estate/path leakage before carry: zero hits.
- Vendor-rev: DGN-1074 back-land -- DGN-996 owner-session-id pre-persist
  (canonical-only; no OSS pin change; DGN-996 is the last of the three
  items the DGN-911 test-catchup entry below explicitly deferred as "v2.0
  in-flight work, Metal dogfood" -- DGN-1016 and DGN-1010 already landed
  above; this closes DGN-996). Drift measured 2026-08-24: `grep -c
  _persist_session_id` = 5 in the Metal working tree / 0 in canonical.
  Root cause (DGN-996): after a bridge restart the first turns are often
  INJECTED turns (cron-inject / session-inbox) that never produce a
  ChatResponse, so bot._save_session_id (the turn-completion save path)
  never runs and sessions.json keeps the OLD process's session id --
  status-footer's _is_owner_session gate then stays closed and the
  workbench (dashboard.md) never regenerates, hiding live subagents
  indefinitely. Port (verbatim from Metal, hunk-selected against today's
  canonical -- DGN-1076/DGN-986/DGN-773 all landed same-day, diffed line
  by line first, no conflicts, no metal-local extension found riding
  along): sdk_bridge.py -- `from bridge.session import session_manager`
  import (previously bot.py-only); `_UserStreamState.persisted_session_id`
  dedupe marker; new `_persist_session_id()` (writes ONLY the on-disk
  value, never touches bot._runtime_active_sessions -- that cross-process
  resume guard in bot._effective_session_id is deliberately untouched, so
  a fresh process still refuses to resume a previous process's session
  even though the disk sid is now always fresh); three call sites --
  reader-loop init SystemMessage, `_handle_proactive_message` init
  SystemMessage (the restart-critical no-pending-request path), and its
  ResultMessage branch (injected-turn completion analog of
  bot._save_session_id). Tests: tests/test_dgn996_owner_sid_prepersist.py
  carried verbatim (9: init-persist both paths + injected-turn result,
  restart-overwrite + fail-silent-no-dedupe-set, turn-completion path
  intact, double-write no-op, resume-guard-untouched, stale-reader cannot
  resurrect a /new-reset sid) -- 9 passed. Scanned for metal-local
  identity/estate leakage (`rg` for estate slugs, owner identity, metal-only
  paths) before carry: zero hits.
- Vendor-rev: DGN-986 [c] /health command -- estate health surface
  (canonical-only; no OSS pin change): NEW bridge/healthcmd.py (bridge/
  health.py is the PRE-EXISTING polling watchdog, untouched -- spec section 1
  naming warning). SELF block (ps-lstart uptime / .instance.conf fw_version /
  per-kit engine_versions / CLI freshness: resolved real path + version +
  installMethod, live latest lookup best-effort 2s with HONEST
  lookup-failed reporting, DGN-962 no-cache-authority); ESTATE block
  (LaunchAgents *.newbridge.plist enumeration, instance root from
  ProgramArguments --path per R4, PID via one launchctl list parse,
  config-vs-process integrity: runtime-snapshot sha diff assertive /
  mtime-vs-lstart fallback tone-downgraded to check-needed, stale-pid and
  unknown-schema snapshots rejected); JOBS block (health-observer jobs.json:
  warn records listed, info records COUNTED only -- fold carries per-kind/
  per-instance count summaries, never 59 enumerated lines; ongoing fail
  streaks / resident-down / label losses from the persisted jobs map; parse
  errors surfaced per R3; observer heartbeat ALWAYS shown and a missing or
  stale state file is itself a check-needed item -- /health never reports
  all-normal on a dead observer's word); install-choice last record read as
  cross-check only (part [d] contract). Engine DB reads REUSE
  boot_snapshot._read_engine_versions (mode=ro / immutable branching, R5 --
  zero WAL/SHM side effects, zero writes to any other instance). Display
  names via sot/MANAGED-TARGETS.md (path-first, then slug; unregistered ->
  real dir name); all counts computed (F3). CTA: per-issue self-explanatory
  sentence + ONE opt: recommended-action button in the handler (the only
  session-reaching rail; IDRILL fires argv subprocess -- spec section 5
  correction). bot.py -- _cmd_health (asyncio.to_thread per R6; split_text +
  markdown/fold HTML render + plain degrade), CommandHandler("health"),
  COMMAND_MENU_SPEC 10 -> 11 (health after authsync, before help) = the
  OWNER'S DIRECT amendment (DGN-986 D1, 2026-08-21) of the DGN-919
  10-command owner lock (2026-08-17). messages.py CMD_DESC_HEALTH +
  HEALTH_FAILED; i18n ko/en cmd_desc_health + health_failed (copy = dec-094
  UX-gate draft). tests/test_dgn986_healthcmd.py (24, all observation
  fixture-injected -- no launchctl/ps/network) + tests/
  test_dgn919_command_menu_spec.py carried to canonical (previously lived
  only in the Metal instance) and amended to the 11-command lock. Full
  suite 1351 passed; same 7 pre-existing failures (dgn616 x2, dgn682,
  dgn801, dgn902 x2, queue_bundling), zero new.
- Vendor-rev: DGN-986 [b] boot runtime snapshot (canonical-only; no OSS pin
  change): new bridge/boot_snapshot.py writes
  BOT_DATA_DIR/runtime-snapshot.json at bridge boot, right before the
  "Bot is running" marker log (once per process, first_boot-guarded call in
  bot.py _run_async). Locked schema=1 payload: schema / label (DGN-888
  .service_plist marker line 2, XPC_SERVICE_NAME fallback) / pid /
  started_at (ISO8601 local offset) / fw_version (.instance.conf
  DOGANY_FW_VERSION) / engine_versions (per-kit map {db stem: PRAGMA
  user_version} across database/*.db -- Metal decision pre-release: a
  scalar max loses WHICH kit's tier it is; no dbs -> {}; strictly
  read-only: mode=ro&immutable=1 when quiescent --
  measured that plain mode=ro on a WAL db materializes -wal/-shm on open --
  plain mode=ro when a -wal is already present) / claude_cli
  {resolved_path, version, install_method} (SAME resolution rule as
  __main__.py _selfcheck: explicit CLAUDE_CLI_PATH wins with no fallback,
  else PATH, else ~/.local/bin/claude; `claude --version` capped at 2s;
  installMethod from ~/.claude.json) / env_files x3 (instance
  .telegram_bot/.env, shared .rules/.env via config.DOGANY_ENV_PATH, own
  newbridge plist via the marker -- sha256 of file bytes ONLY, never
  contents or key names). Atomic publish: same-dir mkstemp + fsync +
  os.replace, tmp unlinked on failure, a mid-write failure preserves the
  previous snapshot. Boot-path first principle: write_runtime_snapshot()
  absorbs ALL exceptions into one WARNING line -- boot order and existing
  log copy untouched. Readers (part [c] /health, part [e] version-check.py
  third comparison) treat pid mismatch vs launchd as stale and an unknown
  schema as skip. Tests: tests/test_dgn986_boot_snapshot.py (14 new:
  full-field/schema lock, secret-free payload, env-file absence, bare
  instance nulls/empty engine map, sha256 integrity, builder-exception and
  unwritable-target absorption, no-partial-file +
  previous-snapshot-preserved + no-tmp atomicity, WAL/SHM side-effect-free
  quiescent read, hot-WAL read, two-db per-kit map preservation, ro-write
  rejection). Full suite green + 14 new; the 7 failures in
  test_dgn616/682/801/902/queue_bundling pre-exist on clean main
  (verified by stash-rerun; DGN-979 carry in flight) and are untouched.
  Correction (DGN-986 integration merge): the [b] secret-free-payload
  test's fake TELEGRAM_BOT_TOKEN value tripped secret-sweep cat7
  (env-secret-line) once run through publish.sh's export-artifact gate
  3 -- invisible in branch-isolated pytest, only surfaced integrated
  (blocked publish.sh --dry-run, cascading 3 cadence-gate.sh failures).
  Fixed by prefixing the fixture value with the existing TEST-ONLY- form
  filter (DGN-1058) instead of a path allowlist entry.
- Vendor-rev: DGN-1076 idrill followup render-path fix (canonical-only; no
  OSS pin change; idrill is a Dogany-only primitive, DGN-918/939, absent
  upstream -- OSS backport pending): _idrill_post_followup fed the
  consumer-authored followup_cmd STDOUT verbatim into
  markdown_to_telegram_html, which its own docstring (formatting.py:950-954)
  forbids ("Apply only to non-code segments (code fences go through
  code_segment_html)"). A pasted fenced ``` table therefore reached the
  owner as literal ``` text with no <pre>, while the SAME stdout rendered
  correctly through the model-turn send path (_send_text_body_chat), which
  already split fences via split_into_segments before rendering. Diagnosed
  by Skull (real-instance table breakage) + reproduced canonically before
  the fix (Skull handoff decision.notice-FJ6S4WF5 H10; Metal worklog
  DGN-1076-idrill-followup-prose-renderer-eats-fences.md). Fix: the
  segment-splitting logic in _send_text_body_chat was extracted into a new
  shared staticmethod _render_prose_html_segments (fenced code ->
  code_segment_html, prose outside fences -> markdown_to_telegram_html,
  per-segment rebalance_html_chunks) so both send paths render identically
  off one implementation; _send_text_body_chat's own behavior is
  byte-identical (same calls, same order, just routed through the shared
  helper). _idrill_post_followup now renders through that helper and posts
  each part as its own NEW message (query.message.reply_text, never an
  edit -- unchanged contract) with a PER-PART plain-text fallback
  (html_to_plain_text) on an HTML post rejection, so a later part's failure
  never re-sends an earlier part that already succeeded; a render-stage
  exception (nothing posted yet) falls back to one plain-text post of the
  raw source, same as before. Every branch stays wrapped so no exception
  can propagate into _idrill_fire_final (fail-soft preserved -- the fire
  already succeeded). The docstring's prior claim ("standard md->HTML
  render (code-block tables / bold / folds work)") was false (code-block
  tables did not work) and is corrected to describe the actual
  segment-aware pipeline and cite the DGN-1076 defect. Empirically verified
  (pure-function probe, before/after, discarded ad hoc): fenced CJK table
  -> old output has no <pre> and a literal ``` marker, new output has
  <pre> and no literal ```; no-fence prose -> byte-identical old/new
  (no regression, bold still renders); unbalanced (odd) fence count, empty
  string, and mixed code+prose all produce no exception from either
  render path. Self-adversarial grill (mocked query, real code path):
  normal case (2 reply_text calls, both parse_mode="HTML", never
  edit_message_text); HTML-rejected case (per-part plain fallback, 4 calls,
  no duplicate re-send of the first part); HTML+plain both-rejected case
  (dropped, logged, zero exception raised); render-stage exception case
  (1 plain-text call of the raw source, zero exception raised); render
  exception + plain post also failing (dropped, zero exception raised);
  empty text (zero calls, zero exception); unbalanced fence (1 HTML call,
  zero exception) -- all 7 cases confirmed fail-soft holds and
  edit_message_text is never invoked. Scope: set-table/set-arm consumers
  (Warg) are unmodified -- the same stdout that broke on the followup path
  already rendered correctly on the model-turn path, so the defect was
  bridge-side only; _idrill_edit_confirmation (:5188, fold-marker-only edit
  path, does not handle fenced tables) is untouched -- judged a separate
  concern, not this ticket's scope. Tests: existing
  tests/test_dgn939_followup_cmd.py suite (26 tests, incl.
  test_followup_stdout_posted_as_new_message,
  test_followup_html_post_failure_falls_back_plain,
  test_followup_render_failure_does_not_escape) passes unmodified against
  the new implementation -- no test changes were needed, confirming the
  externally observable single-message/no-fence contract those tests pin
  is preserved. Full bridge/tests/ suite: 1438 passed, 1 xfailed, same 1
  pre-existing unrelated failure as baseline (test_dgn682_interim_fold.py
  D7 reader-loop timeout, Python 3.14 asyncio-timeout flake, confirmed
  identical on a clean main worktree), zero new failures.
- Vendor-rev: DGN-1015 copy-approval -- owner-approved wording for the
  interrupt-kill notice (2026-08-24). The subagent's draft read
  "배경 작업이 인터럽트로 함께 종료됐습니다"; the owner rejected it as
  developer vocabulary -- a user who simply typed a message does not know
  what a "background task" or an "interrupt" is. Final: ko
  "⚠️ {names} 작업이 멈췄습니다." / en "⚠️ {names} stopped."
  Owner preferred a follow-up prompt ("이어서 할까요?") but the notice site
  cannot mount buttons; that is tracked separately. Strings only -- no
  behaviour change, no OSS pin move.
- Vendor-rev: DGN-1015 -- subagent silent-death visibility (canonical-only;
  no OSS pin change; builds on the DGN-1016 registry below, which is itself
  Dogany-owned and absent upstream). Source-of-truth for the fix: the
  2026-08-22 09:33 incident's OWN evidence, re-read for this ticket -- the
  killed subagent's jsonl (agent-aa6710a3027cbfd1d.jsonl) and Metal's main
  session transcript both timestamp "[Request interrupted by user]" at
  00:33:23.722Z, and NO task_notification/task_updated for that agentId
  exists anywhere in the main transcript. This confirms the DGN-1016 defect
  writeup precisely: the kill is real but invisible, both to the active_tasks
  registry (would leak the id as phantom "still live" forever -- no lifecycle
  event will ever arrive to clear it) and to the owner (no notice of any
  kind). Reuses the DGN-1016 registry rather than a parallel one (explicit
  ticket instruction): sdk_bridge.interrupt() now clears active_tasks/
  task_descriptions for whatever it kills and stashes their descriptions in
  a new interrupt_killed_descriptions slot; a new pop_interrupt_killed()
  getter reads + clears it. bot.py wires this into BOTH interrupt call
  sites -- /stop and the auto-interrupt defer-cap-exceeded kill -- so a
  confirmed kill sends a new BG_SUBAGENT_KILLED_NOTICE naming what died
  (DRAFT copy, owner confirmation pending, same as DGN-1010's precedent).
  This does NOT touch the existing SILENT-by-default auto-interrupt notice
  (owner decision 2026-08-17): that flag governs a different, older fact
  ("your message caused an interrupt"); this ticket's notice is a narrower,
  new fact ("a background subagent is now confirmed dead") that did not
  exist as an observable signal before DGN-1016 landed.
  routines/status-footer.py Rev 13 (companion fix, dashboard side): a
  launched-but-never-completed agent whose OWN jsonl exists but has gone
  stale past LIVE_STALE_SECS used to be silently dropped from the live
  section -- indistinguishable on screen from a normal completion (exactly
  this incident's shape on the dashboard side). It is now returned with a
  "(무응답 N분+ -- 사망 추정)" suffix instead. A jsonl that was NEVER observed
  at all (e.g. a launch-vs-first-jsonl-write race) stays silently dropped --
  no positive evidence of life or death, so flagging it dead would be a
  guess, not a measurement.
  Limits carried forward, not fixed here (DGN-1006 rule): (a) _hard_stop()
  (the second /stop / stuck-turn hard teardown path) still uses the generic
  STOP_FORCED copy without naming what died -- sdk_bridge.stop() pops the
  whole stream state before a caller could read live_task_count/descriptions,
  so closing this needs a capture-before-teardown change, deliberately left
  as a follow-up rather than folded in here; (b) a task filtered out of the
  SDK's own task_started emission (CLI-side, unread filter -- see the
  DGN-1016 entry below) never enters active_tasks and so cannot be named
  when it dies -- status quo, not a regression; (c) the interrupt-kill notice
  is delivered once, best-effort (fire-and-forget send_message) -- there is
  no backstop retry if the send itself fails (logged, not re-driven).
  Tests: bridge/tests/test_dgn1015_silent_death.py (8: description tracking
  add/clear, interrupt() registry-clear + kill-stash, pop-once semantics,
  no-stream empty, /stop names-the-dead vs stays-silent-when-nothing-died,
  auto-interrupt cap-exceeded kill notice), plus a pop_interrupt_killed
  default-mock fix in test_dgn581_soft_interrupt.py (the existing MagicMock-
  based sdk_bridge stub was truthy-by-default on the new call, which the fix
  make explicit rather than papering over in bot.py) and 2 rewritten +
  2 new cases in routines/tests/test-footer-liveness.py (stale-but-observed
  jsonl now shows STALLED instead of vanishing; never-observed jsonl still
  silently drops; mixed live+stalled ordering). Suite: bridge/tests 1438
  passed (was 1430), 1 pre-existing unrelated failure unchanged (confirmed
  identical on a clean HEAD worktree: test_dgn682_interim_fold D7
  reader-loop chunk, Python 3.14 asyncio-timeout flake). routines/tests
  status-footer scripts: test-status-footer.sh 103 passed, test-footer-
  liveness.py 20 passed (was 16), test-footer-desc.py 21 passed -- all
  unchanged or improved, zero regressions.
- Vendor-rev: DGN-1016 back-land -- auto-interrupt background guard
  (canonical-only; no OSS pin change; the machinery this guards is Dogany-owned
  and absent upstream -- verified against the pinned commit itself:
  `git grep -c <term> 87ac8ca -- '*.py'` returns zero files for
  BRIDGE_INFLIGHT_DEBOUNCE_S, _debounce_expire, live_task_count and DGN-616,
  so there is nothing to backport). Source: the live Metal instance implementation
  (running since 2026-08-22 12:56), carried over WITHOUT redesign.
  Defect: an SDK control-protocol interrupt originates as "remote", and the
  CLI (2.1.238, bundle offset 300208773) gives a remote interrupt NO scope --
  it aborts the SESSION-wide tree, killing every in-session background
  subagent, where the TUI's ESC carries scope "turn-cancel" and only ends the
  turn. The DGN-911 in-flight debounce fires exactly that remote interrupt
  whenever the owner keeps typing mid-turn, so ordinary conversation silently
  killed background agents (9 of 14 confirmed subagent deaths in the 7 days to
  2026-08-24; explicit /stop accounted for 0).
  Port: sdk_bridge grows a passive task registry fed from the reader loop
  (task_started adds; a terminal status from EITHER task_notification or
  task_updated removes -- the SDK documents that a background task's terminal
  state can arrive as task_updated alone) plus live_task_count(), a pure dict
  lookup safe under bot.py's per-user queue lock. bot._debounce_expire skips
  the interrupt while live tasks exist and lets the pre-existing DGN-616
  coalescing drain deliver the buffered messages when the turn ends -- no new
  fallback path, only a new condition on the old one. BRIDGE_INFLIGHT_DEFER_CAP_S
  (default 300s) caps the CUMULATIVE deferral per wait so sustained owner input
  eventually wins and a phantom registry entry cannot gate forever; 0 restores
  pre-DGN-1016 behavior. Explicit /stop never runs this path and stays ungated.
  sdk_bridge.interrupt() gains a `trigger` tag ("stop" / "auto") so the two
  origins are finally distinguishable in the log -- before this, the 2026-08-22
  09:33 subagent-death could not be attributed from bot.log at all.
  Canonical-only addition beyond the Metal port (DGN-1010's lesson -- the
  delivery path is not the verification path): the task-lifecycle SDK symbols
  (TERMINAL_TASK_STATUSES, Task{Started,Notification,Updated}Message) postdate
  this tree's declared floor of claude-agent-sdk>=0.1.72, and sdk_bridge is
  imported at bridge boot, so a hard import would turn "guard unavailable" into
  "bridge dead" on an older estate. They are imported defensively and replaced
  by an isinstance-safe sentinel class when absent (None would raise TypeError
  inside the reader loop); the guard then degrades to exactly the pre-DGN-1016
  behavior and says so once per boot with a WARN line, so an inactive guard is
  never silent (DGN-1012/1015).
  Limits carried forward, not fixed here: the CLI gates task_started emission
  (`if(n&&!i)return; if(rR(e))return;`) and which task shapes it filters is
  unread -- a filtered subagent is not in the registry and dies exactly as it
  does today (status quo, not a regression); a missed terminal event leaves a
  phantom entry that re-defers up to the cap on every subsequent wait; the
  deferral is silent to the owner (SILENT notice policy, owner decision
  2026-08-17 -- owner-facing visibility is DGN-1015's scope).
  Tests: bridge/tests/test_dgn1016_interrupt_guard.py (14: lifecycle add /
  terminal removal via either message / non-terminal keep / reader-loop
  proactive-branch tracking / no-stream zero / defer + zero-loss coalescing
  delivery / probe-failure fail-open / /stop ungated with and without live
  tasks / cap exceeded / cap=0 disables / repeated deferrals keep the first
  clock / SDK-absent degradation / sentinel isinstance-safety), plus the
  trigger="stop" signature update in test_dgn581_soft_interrupt.py and a
  BRIDGE_INFLIGHT_DEFER_CAP_S pin in tests/conftest.py. Suite: 1430 passed
  (was 1416), 1 pre-existing unrelated failure unchanged
  (test_dgn682_interim_fold D7 reader-loop chunk).
- Vendor-rev: DGN-1010 layer-2 back-land -- bot.py unterminated-restart marker
  backstop (canonical-only; no OSS pin change; the OSS bridge has no restart
  helper at all -- self_restart.sh, the marker writer, is a Dogany-owned file
  inside the vendored tree, so there is nothing to backport upstream):
  completes DGN-1010 in this template (layer 1 = the entry below). Source is
  the Metal merge commit 439029d (2026-08-22 landing), NOT the Metal working
  tree (its history was contaminated by the v1.40.0 auto-update revert).
  Port: bot.py _restart_backstop_loop -- the NEW bridge, which every restart
  brings up by construction, polls <bot_data_dir>/state/restart-pending.marker;
  marker present + recorded worker_pid dead -> the self_restart.sh worker can
  never push, so the bridge CLAIMS the marker (atomic rename -- exactly one of
  {worker, backstop} wins; never silence, never a duplicate) and terminal-closes
  the restart toward the owner. Marker path parity with layer 1 verified:
  self_restart.sh RESTART_MARKER = dirname(SPOOL_DIR)/state/restart-pending.marker
  = PROJECT_ROOT/.telegram_bot/state/... and bot.py reads
  config.bot_data_dir/state/... with BOT_DATA_DIR = PROJECT_ROOT/.telegram_bot
  -- same file, both layers. i18n: restart_backstop_notice key in ko/en +
  messages.RESTART_BACKSTOP_NOTICE; copy status 미확정 (형님 확인 대기,
  dec-094) -- provisional wording carried verbatim from the Metal landing.
  Canonical-only addition beyond the 439029d port (second lesson of the
  incident: the two layers hid each other's absence for two days): a
  layer-absence probe -- one static read of self_restart.sh per boot; if the
  script no longer references the marker (the exact regression class v1.40.0
  caused), one WARN line says the backstop is structurally dead. Log-only by
  design: no new owner-facing copy (dec-094), no new defense machinery.
  Tests: bridge/tests/test_dgn1010_restart_backstop.py (7 ported + 2 probe).
- Vendor-rev: DGN-1010 layer-1 back-land -- self_restart.sh real session detach
  (canonical-only; no OSS pin change; self_restart.sh is a Dogany-owned file
  inside the vendored tree -- the OSS bridge has no restart helper at all, so
  there is nothing to backport upstream): real incident -- the DGN-1010 fix
  landed in the Metal instance (78cfe2a, 2026-08-22) but NEVER in this template,
  so the very next framework self-update (Metal commit 2565883, "framework
  update 1.40.0 [auto]") overwrote the live file with this template's copy and
  reverted all 95 lines. The revert was silent and total: line 344 went back to
  `nohup "$SELF" ... & disown` carrying the comment "macOS has no setsid" --
  the exact claim DGN-1010 had disproved (setsid(1) the BINARY is absent on
  macOS; Python's os.setsid() is not). Layer 2 (bot.py unterminated-restart
  marker backstop) survived in the instance, but with no writer for
  restart-pending.marker the backstop could never fire, so the silent restart-CTA
  failure was live again and undetected until a 2.0 carry-back audit found it
  (2026-08-24). Fix: template now carries the double-fork + os.setsid detach and
  the marker write, with a nohup fallback + WARN when no python interpreter is
  found (no silent degradation). Layer 2 is NOT yet in this template -- tracked
  separately; layer 1 alone is safe (it writes a marker nobody reads yet, while
  fixing the detach defect itself). Verified: bash -n, and --dry-run --force on
  the Metal instance printing "detached worker (pid N, own session via setsid)".
- Vendor-rev: DGN-1021 rider -- 4th [[OPTIONS]] gate path (fast-path push)
  (canonical-only; no OSS pin change; the domain fast-path interceptor
  (DGN-801, bot._try_fastpath / _fastpath_push_guaranteed) does not exist
  in the OSS repo at all -- grep for "fastpath" in OSS bridge/bot.py and
  bridge/sdk_bridge.py returns zero hits, so there is nothing to backport;
  back-land of live Metal commit applied 2026-08-23): real incident --
  a fast-path body (a domain handler's exit-0 commit witness, e.g. a
  rendered usage-meter table) that ALSO carried a labeled [[OPTIONS: a | b]]
  marker landed with the table delivered but ZERO buttons: the decision
  never rendered. DGN-1021 enumerated and fixed three [[OPTIONS]] gate
  delivery paths (model-turn finalize / proactive push / classifier) but
  never audited a 4th: bot._fastpath_push_guaranteed called
  `self._send_smart(chat_id, content)` with NO force_options argument at
  all, silently defaulting False. bridge.md already documents "code block
  or table and [[OPTIONS]] must NOT appear in the same message" and the
  DGN-085 has_code branch in _send_smart already implements the hard split
  (delete streamed drafts, re-send the body through the segment-splitting
  HTML renderer, send the keyboard as a separate trailing message) -- but
  that branch only activates when force_options is truthy, so this one
  call site bypassed it entirely: the marker was stripped from the body
  (strip_options_marker runs unconditionally), the DGN-1021 gate-mismatch
  WARNING fired ("options gate is OFF ... upstream recognizer drift?"), and
  the button block in _send_content_artifacts never ran. Root cause
  confirmed by direct reproduction (real bot._fastpath_push_guaranteed call
  against a mocked send channel) before and after the fix. Fix: bot.py --
  _fastpath_push_guaranteed now computes `has_options_marker(content)` (the
  SAME canonical recognizer the other three paths already route through,
  imported at module level) and forwards it as `force_options` into
  _send_smart, closing the gap without adding a new detector -- once
  force_options is correctly set, the pre-existing DGN-085 split mechanism
  handles the rest unchanged. Fenced-example edge case verified unaffected:
  a marker line INSIDE a ``` block still never arms buttons (extraction is
  fence-guarded downstream in extract_marker_labels/options.py, unchanged
  by this fix) and still never triggers a split. No new user-facing copy
  (no message text added or changed; SELECT_PROMPT / DGN-1021 WARNING
  copy untouched) -- dec-094 owner-copy gate does not apply. Tests:
  tests/test_dgn1021_options_gate_paths.py -- new "Path 4: fast-path push"
  section (4 new: incident reproduction asserting an actual keyboard +
  correct body/keyboard ordering, code-only no-regression, marker-only
  no-regression, fenced-marker-example no-buttons/no-split). Full bridge
  suite: 1407 passed (1403 baseline + 4 new), same 1 pre-existing failure
  as baseline (test_dgn682_interim_fold.py D7 reader-loop timeout), zero
  new.
- Vendor-rev: DGN-1050 copy correction (canonical-only; no OSS pin change --
  /authsync never existed in OSS): the `authsync_retired` notice (ko/en) no
  longer offers account-switching guidance. Owner ruling 2026-08-24 -- the
  notice covers re-login only; account switching is out of scope for this
  surface and pointing at it here invited the exact SSH-relogin footgun the
  retirement was meant to close. Text-only change, no code path touched.
- Vendor-rev: DGN-1050 /authsync retirement (canonical-only; no OSS pin
  change; /authsync never existed in OSS -- DGN-759/994 were canonical-only
  vendor-revs, so there is nothing to backport): root cause of the
  estate-wide daily auth deaths was our own sync path -- the CLI keeps ONE
  authoritative credential copy (keychain primary, file fallback) and does
  NOT rewrite ~/.claude/.credentials.json after runtime token rotations, so
  the file is stale BY DESIGN; token-sync.sh `sync` (invoked by /authsync
  on MISMATCH) unconditionally overwrote the keychain from that stale file,
  re-injecting a superseded refresh token whose reuse triggers server-side
  token-family revocation (all instances share the account -> estate-wide
  death; timeline-confirmed 2026-08-23). Owner-approved replacement
  procedure: `claude auth login` in a plain terminal. bot.py --
  _cmd_authsync reduced to a zero-side-effect retirement-notice stub (no
  subprocess, no skill-script resolution); handler stays registered but is
  removed from COMMAND_MENU_SPEC (off-menu) so a typed /authsync is
  answered with the notice instead of being forwarded into the SDK session
  by the catch-all. DGN-994 restart CTA removed with the sync path:
  AUTHSYNC_RESTART_CB constant, the _handle_callback branch, and
  _handle_authsync_restart_callback deleted (a leftover CTA button tap in
  old chat history now falls through all callback branches as a no-op);
  the duplicate-launch latch survives for /restart (DGN-997) as
  RESTART_LATCH_S (renamed from AUTHSYNC_RESTART_LATCH_S, same window/
  attribute). messages.py/i18n: all sync-era authsync strings and
  cmd_desc_authsync removed; single new key authsync_retired (copy status:
  미확정, owner-confirm gate dec-094 before release). Companion change
  outside bridge/: skills/dogany-relogin-rebind/token-sync.sh -- `sync`
  subcommand is a hard-fail stub (exit 2, explains retirement, points to
  `claude auth login`; zero `security add-generic-password` calls remain in
  the file), `status` re-semanticized (MISMATCH verdict renamed DIVERGED,
  exit 0, explicitly reported as the NORMAL post-rotation state -- the old
  exit-1 MISMATCH framing was the misinformation that prompted the
  poisonous sync). Tests: test_dgn759_authsync_command.py and
  test_dgn994_authsync_restart_cta.py rewritten to assert the ABSENCE of
  the retired surface (menu/BotCommand exclusion, stub-only handler with
  zero subprocess at behavior+source level, old message constants/i18n
  keys gone, legacy authsync:restart callback tap is a no-op, latch
  renamed); test_dgn997_restart_command.py cross-path CTA latch test
  removed with the CTA. Full bridge/tests/ suite: 1403 passed, 1 xfailed,
  1 pre-existing unrelated failure (test_dgn682_interim_fold.py D7
  reader-loop timeout, same as baseline), zero new failures.
- Vendor-rev: session-inbox UTF-8 poison-pill quarantine (canonical-only;
  no OSS pin change; OSS backport branch backland/session-inbox-utf8-poison-pill
  prepared in the same run (claude-code-telegram commit 9d46903) -- pin bump
  on the OSS-side merge/re-vendor; back-land of live Metal commits 5ec4f95
  (merged 94ac4d9), bridge portion only): incident (2026-08-23, live Metal
  instance) -- a weekly cron (ticket-hygiene.sh) ran under launchd with no
  LANG/LC_ALL, fell back to the C locale, and bash `${var:0:N}` title
  truncation sliced a Hangul character across a byte boundary, writing an
  invalid-UTF-8 file into session-inbox. `_session_inbox_loop`'s
  `path.read_text(encoding="utf-8")` raised UnicodeDecodeError into the
  generic `except Exception` branch, which never removed the file --
  `files[0]` (sorted glob of `*.md`) kept re-selecting the SAME undecodable
  file every 20s poll forever (52+ identical log errors, report never
  delivered, any healthy file sorted after it permanently starved). No
  dedicated DGN ticket existed for this incident at back-land time. Fix:
  bot.py -- catch UnicodeDecodeError specifically ahead of the generic
  handler, rename the file aside to "<name>.corrupt" (no longer matches
  the "*.md" glob, so it can never be re-picked), log exactly once at
  quarantine time; rename failure (OSError) is caught, logged, loop
  continues; generic `except Exception` retry behavior for other read
  failures is unchanged. The producer-side locale fix (ticket-hygiene.sh
  plist LANG/LC_ALL pin) is instance/routines-local and out of scope for
  this vendored file. Tests:
  tests/test_session_inbox_utf8_poison_pill.py (3 new: poison file
  quarantined exactly once with exactly 1 log call; a healthy file sorted
  behind a poison file is no longer permanently blocked; healthy-file
  baseline unchanged). Full bridge/tests/ suite: 1421 passed, 1
  pre-existing unrelated failure (test_dgn682_interim_fold.py D7
  reader-loop timeout, reproduces identically on pre-change main), zero
  new failures.
- Vendor-rev: DGN-911 test back-land -- debounce-interrupt test suite catch-up
  (canonical-only; no OSS pin change; test-layer only, zero behavior change;
  back-land of live Metal commits 9355eaa + faf1044): the DGN-911 code
  (in-flight debounce-interrupt default + explicit /queue coalescing) was
  merged to canonical at v1.37.0 (carry 1965e88b, merge aa66d1dd) WITHOUT its
  test-suite sync -- canonical kept the pre-DGN-911 expectations, which is why
  test_dgn616_coalescing.py x2, test_dgn801_fastpath.py and
  test_queue_bundling.py have been the standing pre-existing failures since.
  Imported: tests/test_dgn911_inflight_debounce.py (NEW -- pins the
  debounce-interrupt default, /queue copy, drain invariant, /stop discard
  path); tests/test_dgn616_coalescing.py (in-flight cases now pass
  coalesce=True -- coalescing is the explicit /queue path post-DGN-911);
  tests/test_dgn801_fastpath.py (in-flight buffer assertion moved to the
  debounce buffer); tests/test_queue_bundling.py (pending merges via
  coalesce=True). Two post-faf1044 hunks in the dgn911 suite are retained
  deliberately: the interrupt mock takes **kwargs (signature-tolerant, works
  with the canonical no-trigger interrupt()) and the /stop reply assertion
  expects the DGN-991 two-line copy (STOP_INTERRUPTED + STOP_BG_NOTE) which
  canonical bot.py has shipped since v1.38.x -- the older single-line
  assertion would fail against canonical. Explicitly NOT imported (v2.0
  in-flight work, Metal dogfood): DGN-1016 auto-interrupt background guard,
  DGN-1010 restart backstop, DGN-996 sid pre-persist (in-review).
- Vendor-rev: DGN-1021 [[OPTIONS]] gates route through the canonical
  recognizer + fail-loud hoist (canonical-only; no OSS pin change; OSS
  backport branch dgn1021-labeled-marker-gate prepared in the same run --
  pin bump on the OSS-side merge/re-vendor; back-land of live Metal commit
  a6acc00e): the concept "is a marker present?" had FOUR implementations --
  the canonical line-based recognizer (formatting.is_options_marker_line via
  options.has_options_marker, bare AND labeled forms) plus three hand-rolled
  `OPTIONS_MARKER in content` substring checks in sdk_bridge. Two of those
  (proactive flush gate, model-turn finalize gate) never learned the DGN-992
  labeled form: has_options=False -> force_options=False -> the entire
  button block in bot._send_content_artifacts (including the DGN-992
  fail-loud, which sits INSIDE `if force_options:`) skipped -- zero buttons,
  zero warnings (the DGN-1021 incident). sdk_bridge.py -- all three seats
  (classifier marker_present suppression / proactive flush / finalize) now
  route through has_options_marker ONLY; substring recognizers retired
  (deliberate semantic: a MID-LINE prose mention never arms buttons, so it
  no longer suppresses classifier injection -- locked by test). The
  `or has_numbered_list` arms are load-bearing (source-3 / classifier
  injection path) and stay. bot.py -- fail-loud hoisted: new elif branch in
  _send_content_artifacts WARNs when an armable marker line reaches the
  render seat with the gate off (recognizer-drift tripwire; log-only,
  body delivered intact, silent for marker-less sends). Tests:
  tests/test_dgn1021_options_gate_paths.py (20 new, ported from Metal):
  path-level gate suite (finalize / proactive / classifier) run end-to-end
  into the real render seat, 4-shape no-regression fixtures (bare marker,
  bare+numbered, numbered-only source 3, plain), fenced-example suppression,
  mid-line-mention intent lock, AST-level guard that `OPTIONS_MARKER in`
  substring recognizers never return to sdk_bridge, gate-off WARN +
  marker-less silence. Full suite: 1403 passed (1383 baseline + 20 new),
  same 5 pre-existing failures as baseline (test_dgn616_coalescing.py x2,
  test_dgn682_interim_fold.py D7, test_dgn801_fastpath.py,
  test_queue_bundling.py), zero new.
- Vendor-rev: DGN-997 /restart owner command + immediate ack (canonical-only;
  no OSS pin change; OSS backport pending; back-land of live Metal commits
  a994fde + b138a18): bot.py -- new CommandHandler("restart", _cmd_restart):
  the general-purpose "just restart" path had NO slash entry at all (DGN-994
  only wired a CTA button inside the /authsync sync-ok reply), so Metal
  ended up announcing a non-existent /restart to the owner (DGN-997
  incident). _cmd_restart reuses the standard _check_access owner gate and
  launches self_restart.sh --trigger user (idle-guard bypass, immediate, no
  confirmation menu -- an explicit /restart typed by the owner IS the
  explicit restart command, same convention the DGN-994 CTA tap already
  uses), with --reason set and NO --resume-intent (a command handler has no
  visibility into what the live SDK session is doing). Self-grill fix
  (already applied before back-land): the DGN-994 CTA latch and the new
  /restart latch are UNIFIED into one shared attribute
  (_restart_launch_started, renamed from _authsync_restart_started) on
  _handle_authsync_restart_callback -- a CTA tap immediately followed by
  /restart (or vice versa) cannot double-fire self_restart.sh or
  double-push the completion notice; both call sites read/write the same
  attribute name. New cmd_desc_restart (ko "봇 재시작" / en "Restart the
  bot") exposed via COMMAND_MENU_SPEC (drives both /help and the BotCommand
  popup menu, inserted after authsync/before help). RESTART_ERROR (ko
  "재시작 오류: {error}" / en "Restart error: {error}") is the failure-path
  fallback only (script missing / launch exception / non-zero exit), same
  shape as AUTHSYNC_ERROR but restart-scoped. dec-094 (owner-approved
  2026-08-21): RESTART_ACK (ko "재시작합니다. 곧 돌아옵니다." / en
  "Restarting -- back shortly.") is a synchronous immediate ack sent right
  after the launch subprocess returns 0 -- self_restart.sh's default DELAY
  is 6s before it SIGTERMs the bridge pid (script -- verified: the launcher
  forks the worker via nohup+& and exits immediately, so subprocess.run
  returns near-instantly, well inside the 6s window before the worker's
  `sleep "$DELAY"` SIGTERM), so the ack always lands on the still-alive
  process -- distinct from the completion push self_restart.sh owns
  separately (no duplicate notice). Failure branches (script missing /
  launch exception / non-zero exit) and the owner-gate denial all
  early-return before the ack line -- ack fires on the success path only.
  Ported onto a byte-identical base (canonical's pre-change _cmd_authsync /
  _handle_authsync_restart_callback / COMMAND_MENU_SPEC / messages.py /
  i18n regions matched the live Metal source's pre-DGN-997 state exactly,
  verified by direct read before editing -- no divergence to reconcile).
  Tests: tests/test_dgn997_restart_command.py (15 new, byte-identical port
  -- no live-only test infra dependency): access gate, launch argv
  (--trigger user / --reason / no --resume-intent), duplicate-command
  single-launch, cross-path CTA-then-/restart shared-latch (self-grill
  regression), missing-script and launcher-failure error paths (latch
  reset on failure), RESTART_ERROR/RESTART_ACK copy presence + locked ko
  wording, /help + BotCommand menu exposure. No test_dgn919_command_menu_spec.py
  exists in canonical to update (pre-existing gap, never vendored -- the
  live source's DGN-919 diff hunk for that file was skipped; the two
  canonical tests that read COMMAND_MENU_SPEC, test_dgn618_command_output.py
  and test_dgn902_btw_command.py, only assert membership/absence, not a
  fixed count, so they are unaffected by the new "restart" entry). Full
  suite: 1383 passed (1368 baseline + 15 new), same 5 pre-existing failures
  as baseline (test_dgn616_coalescing.py x2, test_dgn682_interim_fold.py
  D7 reader-loop timeout, test_dgn801_fastpath.py, test_queue_bundling.py),
  zero new.
- Vendor-rev: DGN-994 /authsync restart CTA (canonical-only; no OSS pin
  change; OSS backport pending): bot.py -- _cmd_authsync sync-ok branch (and
  ONLY that branch: MATCH / NOT-APPLICABLE / ERROR / SCRIPT_MISSING /
  SYNC_FAILED stay button-free) now attaches a one-button InlineKeyboardMarkup
  restart CTA (label i18n key authsync_restart_button: ko "재시작" / en
  "Restart" -- the only new copy; DGN-990 lexicon). Direct InlineKeyboard +
  dedicated callback token AUTHSYNC_RESTART_CB ("authsync:restart"), NOT the
  DGN-992 [[OPTIONS]] label-marker path: that path runs on the agent-output
  send pipeline and its opt: callback round-trips through the SDK session,
  while /authsync is a session-free command handler. New
  _handle_authsync_restart_callback: owner-gated by the same _check_access
  every callback passes (incl. 20-min stale drop = old-message replay cap);
  an owner tap is the explicit restart command (owner decision 2026-08-21),
  so it launches PACKAGE_DIR/self_restart.sh --trigger user (idle guard
  bypassed, immediate, no confirmation) -- bridge-package-relative resolution,
  no hardcoded absolute path (DGN-929 lesson). Keyboard is cleared before
  launch (replay guard) and a monotonic 120s latch drops duplicate taps
  without going sticky (an aborted worker self-heals); script
  missing/launch failure reuses AUTHSYNC_ERROR with the technical detail
  (no new sentence) and resets the latch so a retry tap works. Tests:
  tests/test_dgn994_authsync_restart_cta.py (15 new: exactly-one-button on
  sync-ok with label+callback_data asserted, no reply_markup on all five
  other branches, ko/en label lexicon + short-token contract, non-owner
  callback no-op, owner callback --trigger user argv, duplicate-tap single
  launch, missing-script error without launch, launcher-failure error +
  latch reset). Full suite: 1361 passed, same 7 pre-existing failures as
  the v1.39.2 baseline (dgn616 x2, dgn682, dgn801, dgn902 x2,
  queue_bundling), zero new.
- Vendor-rev: DGN-992 labeled [[OPTIONS: a | b]] marker + fail-loud zero-button
  net; DGN-991 stopgap /stop copy honesty (canonical-only; no OSS pin change;
  OSS backport pending): formatting.py -- OPTIONS_LABELED_RE +
  is_options_marker_line + parse_options_marker_labels (telegram-free, so the
  push.sh sanitize hop keeps working -- DGN-822 invariant);
  strip_display_markers drops labeled marker lines from streamed drafts.
  options.py -- strip_options_marker strips both forms; new has_options_marker
  + extract_marker_labels (fence-guarded: a syntax example inside ``` never
  arms, DGN-085 guard class); strip_consumed_options gains marker_labels: a
  labeled marker's labels ARE the options (body formatting irrelevant -- the
  DGN-992 silent-evaporation trap closes), and the numbered-run parse is
  demoted to a dedup optimization that strips the body run ONLY on exact
  label match + line adjacency + no DGN-879 overflow-keep (a mismatched run =
  DGN-984 hijack shape stays in the body and never feeds buttons). bot.py --
  _reply_smart/_send_smart/_send_content_artifacts pass marker labels from
  the ORIGINAL content; zero-buildable-buttons with a marker present now logs
  a WARNING and the body is guaranteed intact as the text fallback (no
  dead-end; body strip only ever fires when buttons build). sdk_bridge.py --
  classifier gate uses has_options_marker so a labeled marker suppresses
  Haiku injection (substring check missed it). DGN-991 stopgap (root
  relocation of background work out of the session process stays v2.0,
  untouched): bot.py _cmd_stop soft-success reply appends new stop_forewarn
  (another /stop force-quits the session process; background work inside it
  dies; no task count invented -- no live-task registry exists); _hard_stop
  splits the reply: task_cancelled-or-killed -> new honest stop_forced copy,
  cleared-only -> stop_paused (still true), idle -> stop_nothing. The bare
  stop_interrupted is kept for the DGN-911 auto-interrupt notice. i18n
  ko/en + messages.py: stop_forewarn + stop_forced (DRAFT copy, owner
  confirmation pending dec-094). Tests:
  tests/test_dgn992_marker_label_buttons.py (24: parsing, fence guard,
  dedup/mismatch/overflow, keyboard build incl. DGN-881 number handle,
  seat wiring incl. fail-loud caplog, classifier gate) +
  tests/test_dgn991_stop_copy.py (10: key presence/mirroring/honesty/no-count/
  action, wiring for all four reply branches); test_dgn581_soft_interrupt.py
  hard-path assertions updated to the forced copy.
  rev2 (same ticket, head-directed correction): the bare marker's TRAILING
  lines (contiguous non-blank lines directly under a standalone [[OPTIONS]]
  line, stopping at a blank line / another marker / a code fence) are now
  accepted as labels -- the natural authoring shape from the real incident
  (labels written under the marker, no numbered run anywhere). Priority:
  labeled marker > bare-marker trailing lines > body numbered run (first
  source that yields labels wins). Numbered trailing lines shed their "N. "
  prefix via the EXISTING _OPTION_RE (no new heuristic; prevents
  double-numbered buttons in the DGN-984 marker-above shape). Consumed
  trailing labels are removed from the display via new
  _strip_verbatim_label_block (exact contiguous match only, fence-guarded,
  miss-safe) under the same DGN-879 overflow-keep rule; sentence-looking
  trailing lines still become buttons (evaporation is worse than an ugly
  button) and over-wide ones ride the DGN-881 number handle. +9 tests
  (verbatim incident fixture -> exactly 3 buttons; blank-line prose not
  labels; priority; prefix shed; sentence/overflow keep; foreign-marker
  stop; marker-last regression; seat-level 3-button render).
  rev3 (DGN-991 premise falsified by incident log -- copy redesigned): the
  2026-08-21 loss happened with a SOFT interrupt only (no Force-killed / Bot
  stopped in that window), so the "background dies at the second /stop"
  forewarn was false. Mechanism pinned in code: the bridge soft path sends
  ONE SDK control request and cancels nothing itself (bot.py _cmd_stop soft
  branch; sdk_bridge.interrupt drains bridge-side futures only); the SDK
  writes {"subtype":"interrupt"} to CLI stdin (claude_agent_sdk client.py /
  _internal/query.py); the CLI's stdin message-loop handler for that request
  aborts the in-flight turn's AbortController tree (claude 2.1.238 binary,
  extracted handler: fe.abort(XC("remote-cancel")) + mr.abortController
  ?.abort() + taskRegistry sweep) -- Task-tool subagents run inside that
  tree, so they die at the FIRST stop. stop_forewarn RETIRED; new
  stop_bg_note states the background consequence immediately (no count --
  no live-task registry) plus the true second-/stop escalation fact;
  stop_forced rephrased to "still alive inside it"; sdk_bridge.interrupt
  docstring carries the caveat. All copy remains DRAFT pending owner
  confirmation (dec-094). +1 honesty regression test (immediate-consequence
  phrasing, deferral phrasing banned). Full suite: 1346 passed, same 7
  pre-existing failures as baseline (dgn616 x2, dgn682, dgn801, dgn902 x2,
  queue_bundling), zero new.
- Vendor-rev: DGN-979 btw test-file carry repair (canonical-only; no OSS pin
  change; OSS backport pending; TEST-ONLY -- no runtime file touched):
  DGN-920 and DGN-922 carry commits moved bot.py into the vendored tree but
  did NOT move the matching tests, so
  tests/test_dgn902_btw_command.py stayed frozen at 89bf940a ("fix(919)")
  while bot.py advanced. Running that stale test against the CURRENT bot.py
  reproduces 2 deterministic failures: the old test patches
  `_track_user_task`, but DGN-922 FIX 4 moved fork tasks onto a separate
  `_track_btw_fork_task` / `_btw_fork_tasks` path, so the real code runs
  unstubbed into `_drain_pending_texts` -> `_get_user_queue_lock` and raises
  `AttributeError: 'TelegramBot' object has no attribute
  '_user_queue_locks'`. The defect is in the vendored TEST file only --
  bot.py in the template is byte-identical to the live Metal bridge's
  bot.py (verified by diff), so no runtime behavior is implicated and the
  pin does not move. Repair is a one-way fast-forward: the live Metal
  instance's copy of the test file is a strict superset of the vendored
  copy, so it replaces the vendored copy wholesale -- (a) the two patch
  targets corrected to `_track_btw_fork_task` plus the `_btw_fork_tasks`
  instance stub (DGN-922 FIX 4), (b) the 5 `TestBtwFormatting` cases from
  DGN-920 (fork edit/send paths must carry parse_mode="HTML" and convert
  **bold** -> <b>bold</b>) which had never been vendored at all. Detected by
  the v1.39.3 self-update reconcile report, which classified all three files
  as CONFLICT and preserved them -- that block was the correct defense: had
  they landed, the live instance would have REGRESSED (2 failures
  reintroduced, 5 formatting cases lost). Sibling files in the same conflict
  set (conftest.py, test_countdown.py) were the reverse case -- canonical
  ahead, live stale -- and were adopted live-side, outside this commit.
  Tests: run in the template tree, tests/test_dgn902_btw_command.py = 24
  passed, 6 subtests passed. Live Metal full suite after the paired
  adoption: 1 failed, 1354 passed, 1 xfailed, 18 subtests (was 2 failed);
  the remaining failure is test_dgn682_interim_fold.py's D7 reader-loop
  timeout, out of scope, tracked as DGN-980.
- Vendor-rev: DGN-974 pin (dashboard) surface HTML parse_mode + fail-open
  fallback (canonical-only; no OSS pin change; OSS backport pending):
  dashboard.py's two send sites (_sync's edit_message_text, _recreate's
  send_message) never set parse_mode -- the pinned live-dashboard message
  always rendered plain text, so a Warg 종목표 code block (and bold/inline
  code) could never render on the pin surface even though the same content
  renders fine on the normal chat rail. New DashboardSync._render_pin_html
  routes pin text through the SAME converter the chat path uses
  (formatting.sanitize_message_for_telegram + balance_telegram_html -- no
  second converter); new _edit_with_fallback/_send_with_fallback wrap the two
  send sites: send parse_mode="HTML", and on a Telegram parse-entity
  rejection (new _is_parse_error classifier, narrowly scoped to "can't parse
  entities" so an unrelated BadRequest such as "message is not modified" is
  never swallowed) OR a conversion exception, retry the IDENTICAL operation
  as plain text with no parse_mode -- the pin must never stop updating or
  disappear over a formatting problem in owner/generator-authored content
  (DGN-967 section ownership). Every pre-existing exception path
  (RetryAfter, Forbidden, chat-gone, needs-recreate, NetworkError, generic
  TelegramError, the dirty/synced bookkeeping in _mark_synced) is preserved
  byte-for-byte -- the fallback only intercepts a genuine HTML parse
  rejection at the innermost call. formatting.markdown_to_telegram_html was
  independently verified (real code run, not just read) to already
  html.escape '<' / '&' / '>' in prose segments (step 3 of the conversion
  pipeline), so no converter change was needed for that part of the spec.
  Tests: tests/test_dgn974_pin_parse_mode.py (14 new: fenced code block ->
  <pre> + parse_mode="HTML" at both send sites; raw '<'/'&' in a ticket-title
  style body escapes and still sends; conversion-raises -> plain-text
  fallback with correct dirty/synced bookkeeping at both sites; a Telegram
  parse-error BadRequest -> plain-text retry succeeds at both sites; a
  NON-parse BadRequest ("message is not modified", "chat not found") is NOT
  retried -- exactly one call, pre-existing behavior kept; _is_parse_error
  classifier unit tests). Full suite: 1297 passed (1283 baseline + 14 new),
  same 7 pre-existing failures as baseline, zero new.
- Vendor-rev: DGN-969 de-marked streaming display (canonical-only; no OSS pin
  change; OSS backport pending): the live streaming draft bubble was sent as
  raw plain text the whole turn (formatting.markdown_to_telegram_html only ran
  on the single final edit, and that edit itself falls back to delete+resend
  for any multi-bubble reply) -- **bold**, __bold__, `code` etc. showed
  literally the entire time the owner was actually reading. formatting.py --
  new demark_markdown_for_stream(text) (+ private _demark_prose helper):
  DISPLAY-ONLY transform (never touches the caller's own accumulated text)
  that strips a COMPLETE markdown span (bold-star/under, strike, inline code,
  word-only italic, links) to its bare content using the EXACT SAME regexes as
  markdown_to_telegram_html, so any span with a real closing pair renders
  identically to the eventual final HTML render; a marker left DANGLING by a
  stream-chunk cut or by the bridge's own character-count overflow split (an
  orphaned OPENER with no close yet, or an orphaned CLOSER whose opener sealed
  into a previous already-finalized bubble) is also stripped -- new
  _MD_BOLD_STAR_OPEN_RE/_CLOSE_RE, _MD_BOLD_UNDER_OPEN_RE/_CLOSE_RE,
  _MD_STRIKE_OPEN_RE/_CLOSE_RE mirror the existing full regexes' own
  alnum-adjacency guards, so x**2 / snake_case / *.md glob literals stay
  provably untouched (same guard the complete-pair regex already relies on).
  Single-character emphasis (*word*/_word_) and links are converted ONLY on a
  complete pair -- deliberately NOT dangling-stripped (indistinguishable from
  literal glob/math/snake_case without a closing pair to confirm intent).
  Fenced code blocks (closed or an in-progress/unclosed trailing fence) are
  left fully untouched, fences included. streaming.py -- StreamingMessageHandler
  .create_draft/_send_extra_chunks/update_draft/finalize_draft now send
  demark_markdown_for_stream(...) instead of the raw text; DraftState.text and
  handler.accumulated_text are UNTOUCHED (still the raw original), so
  chunking/overflow decisions and the eventual bot.py
  _edit_streamed_prose_html HTML conversion are byte-identical to before this
  change -- the finalized message is unaffected, only the live in-progress
  bubble's appearance changes. Tests: tests/test_dgn969_demarked_streaming.py
  (30 new: complete-pair conversion per marker type, dangling-open AND
  orphaned-closer cases for **/__/~~/backtick, glob/math/snake_case/x**2
  non-corruption, closed and unclosed fenced-code preservation, a real
  overflow split constructed to bisect a live "**bold**" span, and the
  create_draft/update_draft/finalize_draft raw-vs-displayed invariant). Full
  suite: 1260 passed (1230 baseline + 30 new), same 7 pre-existing failures as
  the v1.39.2 baseline, zero new.
- Vendor-rev: DGN-929 authsync skill-path resolution fix (canonical-only; no
  OSS pin change; OSS backport pending): bot.py -- _cmd_authsync no longer
  hardcodes Path.home()/.claude/skills/dogany-relogin-rebind/token-sync.sh
  (that path never exists on a standard Dogany instance, which installs
  skills under PROJECT_ROOT/.claude/skills -- /authsync always replied
  "script missing" in production). New shared _skill_roots() (workspace
  PROJECT_ROOT/.claude/skills first, ~/.claude/skills legacy/global
  fallback) + _resolve_skill_script(skill_name, script_name) helper; both
  _cmd_authsync and _cmd_skills now call _skill_roots() so the two commands
  cannot diverge on lookup order again (climb-the-ladder: reused/extracted,
  no third copy). tests/test_dgn759_authsync_command.py -- the test suite
  previously asserted the HOME-only path as the correct contract (encoding
  the bug as a passing test); replaced with real (non-mocked) temp-dir
  fixtures covering workspace-only resolution, HOME fallback, workspace-wins-
  when-both-present, and a genuine (non-mocked) missing-script case; new
  test_command_invokes_resolved_workspace_script fails against the pre-fix
  handler (verified: reverting _cmd_authsync to the old hardcoded lookup
  makes this test fail with "subprocess.run was never invoked" while all
  other tests still pass).
- Vendor-rev: DGN-964 watchdog DGN-888 marker self-heal + deterministic plist
  pick (branch dgn964-watchdog-marker-selfheal; canonical-only, no OSS pin
  change; OSS backport pending): watchdog.sh -- backfill_service_marker() on
  the healthy-heartbeat path: while the label IS registered, validate the
  $DATA_DIR/.service_plist marker locally (file exists, placeholder-free,
  Label match); when missing/invalid, resolve the plist launchd ACTUALLY
  loaded (`launchctl print` first `path =` line) and write the marker from
  that verified truth -- never a guess; parse miss / failed guard = no-op
  (2026-08-21 Skull 21-min outage: backstop code present but marker absent,
  watchdog skipped forever). 'No marker = no recovery' preserved for fresh /
  manual installs; all DGN-888 recovery guards untouched. watchdog_setup.sh
  -- expected_bridge_label() (.instance.conf DOGANY_AGENT_NAME) + pick_plist()
  deterministic candidate selection for BOTH the *.watchdog.plist
  registration and write_service_marker: multiple candidates WARN (never
  silent) and prefer the Label matching the instance identity so a legacy
  plist of a renamed agent can no longer win first-glob; identity
  unresolvable falls back to first-glob with a warn naming the winner.
  self_restart.sh -- operator-guard header: raw `launchctl bootout`
  discouraged (unregisters the label; KeepAlive dies with it); restart via
  self_restart.sh, unavoidable bootout must pair with an immediate bootstrap.
- Vendor-rev: DGN-963 start.sh PATH standardize (canonical-only; no OSS pin
  change; OSS backport pending): start.sh -- export PATH reordered so
  $HOME/.local/bin (native Claude CLI installer target, self-updating) wins
  ahead of both Homebrew and /usr/local/bin, closing the DGN-961 root cause
  (a stale root-owned npm-global claude in /usr/local/bin silently winning
  PATH resolution over the fresher user-writable native install).
  com.telegram-skill-bot.telegram-agent.newbridge.plist -- same PATH reorder
  + removal of the dead __HOME__/.npm-global/bin entry (deprecated install
  method). CLAUDE_CLI_PATH untouched and unaffected: it is an explicit
  absolute-path override (config.py -> sdk_bridge.py opts["cli_path"]),
  never PATH-search based.
- Vendor-rev: DGN-966 shared artifact-render contract (canonical-only; no OSS
  pin change; OSS backport pending): NEW artifacts.py -- telegram-free,
  config-free spec layer for arm-declared keyboards (arm read + containment,
  step_buttons row normalization: legacy flat = one row byte-identical /
  nested grid = multi-row BotFather-style, declared-value whitelist, keyboard
  SPEC build with opt-in Back row and a 64-byte callback_data drop guard).
  bot.py -- _send_content_artifacts is now THE shared artifact tail
  (target-polymorphic: telegram message OR bare chat_id via _artifact_send /
  _artifact_chat_id); _send_smart's duplicated partial tail (files + OPTIONS
  keyboard, IDRILL marker silently deleted) replaced by a call into it, so
  the fast-path (DGN-801 _fastpath_push_guaranteed) and proactive pushes
  render the identical keyboard the model turn renders; idrill keyboard
  builders (_idrill_send_initial_keyboard / _idrill_build_step_keyboard /
  _idrill_declared_values / _idrill_read_arm) delegate to bridge.artifacts;
  DGN-966 rider: confirm_fmt/confirm_fmt_skip/confirm_fold {i} tokens render
  from the FULL ordered capture list (_idrill_positionals; fixes the
  captures[0][1] fixed-index gate-tap leak on N-step arms; 2-step arms
  byte-identical). routines/push.sh -- [[IDRILL:<arm_id>]] marker hop through
  the same spec builder (initial_keyboard_json): body bubble + step-1
  keyboard message from ONE invocation (multi-button rows / grids /
  drilldown entry on the push rail; --button DGN-835 single-button contract
  untouched); fail-soft marker strip on hop failure, marker-only push sends
  keyboard alone. IDRILL-ARM-CONTRACT.md documents the grid shape, path
  independence, and {i} render. Tests:
  test_dgn966_shared_artifact_render.py (17: path parity, 6-button row,
  grid drilldown off-model-path, push-hop JSON parity, {i} rider, 64-byte
  drop, partial-marker / expired-arm / stale-tap probes, --button contract
  scan). Full suite 1247 passed; same 7 pre-existing failures as the
  v1.39.2 baseline, zero new.
  Verification round (same ticket, pre-merge): (1) push.sh's keyboard-fail
  branch changed exit 2 -> exit 0 + loud stderr WARN. Evidence: the
  health-trainer redirect-respond.sh call has no `|| true` guard around
  push-gated.sh, so a non-zero push.sh exit bubbles up through
  handoff.consume ("handler crash leaves the message for the next sweep")
  and re-sends the SAME body on the next poll; mirror-reconcile.sh /
  mirror-poll.sh's STAMP-AFTER-PUSH pattern has the same retry-on-nonzero
  shape. No caller in this codebase distinguishes exit codes, so a
  keyboard-only failure (body already delivered) must never look like a
  full-push failure; exit 2 stays reserved for the body sendMessage itself
  failing (nothing delivered, safe to retry). (2) _send_content_artifacts's
  outside-PROJECT_ROOT send_file:: confirm now only raises the interactive
  Allow/Deny keyboard when `target` is a live telegram message (model-turn
  rail); the bare-chat_id rail (fast-path/proactive, no live turn to answer
  it -- measured: dead-end prompt, 20-min STALE-gated button, no owner
  around at an odd-hour cron push) gets a new _notify_outside_file_omitted:
  loud log + a plain no-button chat notice, no session-state write. New
  i18n key external_file_omitted_noninteractive (ko/en) -- copy APPROVED by
  owner 2026-08-21 (dec-094 UX-facing gate cleared). Owner directed DUAL
  notation: plain term first, internal token in parens -- ko "작업
  폴더(PROJECT_ROOT) 바깥 파일이 있었지만 ...", en "A file outside your
  working folder (PROJECT_ROOT) was withheld ...". Rationale: the bare token
  alone does not tell the reader where the boundary is; a plain-word-only
  phrasing does not say WHICH boundary -- so both. Tests:
  +2 in test_dgn966_shared_artifact_render.py (19 total) + new
  routines/tests/test-push-idrill-exitcode.sh (8 assertions, curl-stub
  exit-code contract). Full suite unchanged at 7 pre-existing
  fail / 9 pre-existing error (environment count differs from the 1247
  figure above -- same NAMED set before and after this round, zero new).
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

## Ownership layers -- L-CORE / L-ADAPT / L-DOC (DGN-818 C1)

DGN-818-DESIGN section 1.3 判定: "the bridge" has no single owner. What is one
tree today is three layers:

| layer | what | owner | version line |
|---|---|---|---|
| **L-CORE** | channel transport/parsing machine. Same for any Claude Code it is bolted onto: `[[OPTIONS]]` parser, HTML render/split, width & size limits, streaming, session, permissions | OSS product (`coolcoolk/claude-code-telegram`) | bridge semver (OSS `__version__`) |
| **L-ADAPT** | estate adapter -- code that KNOWS Dogany: idrill engine, `/health`, boot snapshot, btw, fastpath, image_send, the vendor-contract loader/composer, `SKILL_DISPLAY_NAMES` | framework (owned by the template's product manifest) | framework semver (`VERSION`) |
| **L-DOC** | judgment / expression rules that code cannot enforce: `vendors/telegram.md` | framework | framework semver |

DGN-1141 M7 section 3 判定 ("machine grammar = code is canonical") is NOT
overturned by this table -- it is refined. Exactly one M7 row lands in a
different layer than where it physically sits today: the **DGN-086 subagent
delegation preamble** is estate discipline (L-ADAPT), not channel grammar
(L-CORE), yet it lives in the shared `SYSTEM_PROMPT` text on both sides of the
fork. See DGN-818-DESIGN section 1.3 for the full remap.

### The declaration unit is the DECLARATION, not the file

DGN-818-DESIGN section 8-4 flagged its own weakest link: the layer table was
written per FILE, while the real mixing happens INSIDE files. Measured on this
tree (`python3 tests/dgn818_ownership_lint.py --list-units`): **229 estate-marker
code lines spread over 108 declarations in 13 of 28 shippable modules**, and
6 of those 12 modules (`bot.py`, `config.py`, `formatting.py`, `i18n/__init__.py`,
`options.py`, `sdk_bridge.py`, plus the two catalogs) exist on BOTH sides of the
fork. A per-file declaration would therefore have to call `bot.py` (5,605 lines,
180 marker lines) a single layer, which is false in both directions.

So the unit is the **declaration**:

* a top-level `def` / `class`, or a method (`TelegramBot._idrill_advance`)
* a module- or class-level named binding (`ARM_SUBDIR`, `TelegramBot._IDRILL_TOKEN_RE`)
* a dict entry, keyed (`STRINGS[idrill_arm_expired]`, `SKILL_DISPLAY_NAMES[dogany-mailer]`)
* an imported name (`import:strip_idrill_marker`)
* the catch-all remainder of a scope (`<module-body>`)

No line numbers appear in a unit id, so the registry survives code motion.

A third value, **L-SEAM**, exists because declaration granularity does not fully
resolve either: 12 declarations are L-CORE code whose only estate contact is a
*reference* into L-ADAPT (`display, _ = strip_idrill_marker(display)`;
`if data.startswith("idrill:")`). Calling those L-ADAPT would be a lie, and
calling them L-CORE would silence the drift check. L-SEAM declares the exact
reference tokens that are allowed to appear; any OTHER estate line under a seam
declaration is a violation.

### Registry

Enforced by `tests/dgn818_ownership_lint.py` (COVERAGE / NO-DRIFT / NO-PHANTOM),
driven from `tests/dgn818_ownership_selftest.sh`. Columns:
`<layer>  <module>  <declaration>  [seam tokens]`; `*` = the module default;
a trailing `*` on a declaration is a prefix glob.

```dogany-ownership-map
# --- __init__.py: the C3 version identity is framework-minted ---------------
#     __oss_base__ / __oss_pin__ / __vendor_rev__ are pure provenance, but the
#     composed __version__ carries the `+dogany.` local segment -- an estate
#     fact, and the reason this line is L-ADAPT. (Landing C3 tripped the C1
#     lint on exactly this: the gate found it before a human did.)
L-ADAPT  __init__.py  __version__

# --- modules with no OSS counterpart: estate adapters end to end -------------
L-ADAPT  artifacts.py      *
L-ADAPT  boot_snapshot.py  *
L-ADAPT  btw.py            *
L-ADAPT  fastpath.py       *
L-ADAPT  healthcmd.py      *
L-ADAPT  image_send.py     *

# --- modules present on both sides of the fork: L-CORE by default -----------
L-CORE   __init__.py       *
L-CORE   __main__.py       *
L-CORE   bot.py            *
L-CORE   config.py         *
L-CORE   countdown.py      *
L-CORE   dashboard.py      *
L-CORE   edit_guard.py     *
L-CORE   formatting.py     *
L-CORE   health.py         *
L-CORE   heartbeat.py      *
L-CORE   i18n/__init__.py  *
L-CORE   i18n/en.py        *
L-CORE   i18n/ko.py        *
L-CORE   messages.py       *
L-CORE   model_state.py    *
L-CORE   options.py        *
L-CORE   ownership.py      *
L-CORE   permissions.py    *
L-CORE   sdk_bridge.py     *
L-CORE   session.py        *
L-CORE   streaming.py      *
L-CORE   voice.py          *

# --- bot.py: idrill engine + /usage pipeline are estate; the dispatch and
#     render edges that merely call into them are seams ------------------------
L-ADAPT  bot.py  TelegramBot._IDRILL_*
L-ADAPT  bot.py  TelegramBot._idrill_*
L-ADAPT  bot.py  TelegramBot._handle_idrill_callback
L-ADAPT  bot.py  TelegramBot._cmd_usage
L-ADAPT  bot.py  TelegramBot._fetch_usage_json
L-ADAPT  bot.py  TelegramBot._usage_retry_run
L-SEAM   bot.py  import:strip_idrill_marker      strip_idrill_marker
L-SEAM   bot.py  TelegramBot._reply_smart        strip_idrill_marker
L-SEAM   bot.py  TelegramBot._send_smart         strip_idrill_marker
L-SEAM   bot.py  TelegramBot._send_content_artifacts  strip_idrill_marker,idrill_arm_id,_idrill_send_initial_keyboard
L-SEAM   bot.py  TelegramBot._handle_callback    idrill:,_handle_idrill_callback

# --- formatting.py: the idrill marker + its stripper are estate; the generic
#     display-marker sweep only references them ---------------------------------
L-ADAPT  formatting.py  IDRILL_MARKER_RE
L-ADAPT  formatting.py  strip_idrill_marker
L-SEAM   formatting.py  strip_display_markers   IDRILL_MARKER_RE

# --- options.py: re-export + one predicate that consults the estate marker ----
L-SEAM   options.py  import:IDRILL_MARKER_RE     IDRILL_MARKER_RE
L-SEAM   options.py  _is_foreign_marker_line     IDRILL_MARKER_RE

# --- config.py: the shared-.env discovery is Dogany-named. It is present in
#     OSS too (OSS config.py:27) -- an estate leak that already shipped, and a
#     named backport-removal candidate rather than a mislabel ------------------
L-ADAPT  config.py  _find_dogany_env
L-SEAM   config.py  <module-body>   DOGANY_ENV_PATH

# --- sdk_bridge.py: the register-path regex hardcodes the framework layout ----
L-ADAPT  sdk_bridge.py  _REGISTER_PATH_RE

# --- catalogs: estate strings are keyed, so the unit is the key ---------------
L-ADAPT  messages.py  IDRILL_*
L-ADAPT  i18n/en.py   SKILL_DISPLAY_NAMES*
L-ADAPT  i18n/en.py   STRINGS[idrill_*
L-ADAPT  i18n/en.py   STRINGS[usage_script_missing]
L-ADAPT  i18n/ko.py   SKILL_DISPLAY_NAMES*
L-ADAPT  i18n/ko.py   STRINGS[idrill_*
L-ADAPT  i18n/ko.py   STRINGS[usage_script_missing]
L-SEAM   i18n/__init__.py  DISPLAY_NAMES_FOR[en]  SKILL_DISPLAY_NAMES
L-SEAM   i18n/__init__.py  DISPLAY_NAMES_FOR[ko]  SKILL_DISPLAY_NAMES
L-SEAM   i18n/__init__.py  skill_display_name     SKILL_DISPLAY_NAMES
```

What the registry does NOT claim: it classifies estate KNOWLEDGE, not code
authorship or backport eligibility. An L-CORE declaration can still be
canonical-only (the 8,009-line delta of DGN-818-DESIGN section 1.2); the
registry says nothing about that axis -- the release-time bridge-drift
reporter (C4, canonical-repo tooling) measures it.

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
