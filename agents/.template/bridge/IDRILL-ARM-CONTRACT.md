# idrill arm contract (in-place callback drilldown engine)

Refs: DGN-918 (2-step seed), DGN-924 (shape-agnostic), DGN-939 (N-step + « Back
+ expandable-blockquote confirmation fold). This file is the SINGLE contract
surface between the bridge engine (Metal-owned) and the arm CONSUMER
(Skull-owned flow wiring). The bridge holds no domain meaning; every value,
button, step count, copy string, and command is declared by the arm file.

Boundary: Metal owns the mechanism below (callback routing, in-place edits,
keyboard swaps, nav stack, argv substitution, security gates, fold render).
Skull owns everything the arm file DECLARES (button values, step count, prompt
copy, cmd argv, fold content, arm/attach timing). The contract is file + CLI +
exit-code; there is no new contract_version surface.

## 1. Arming: write an arm file

Path: `PROJECT_ROOT/files/program/.idrill-arm/<arm_id>`
`<arm_id>` MUST match `^[0-9a-f]{8}$` (8 lowercase hex chars). The consumer
mints it (e.g. `secrets.token_hex(4)`). The bridge rejects any other shape
(path-traversal guard).

The file is JSON. Fields:

| field            | type            | required | meaning |
|------------------|-----------------|----------|---------|
| `step_buttons`   | dict step->rows | yes      | per-step keyboard. Key = step "1".."N". Value accepts TWO shapes (DGN-966): legacy flat `[[label, value], ...]` = ONE row (DGN-918/924/939 byte-identical), or grid `[[[label, value], ...], ...]` = list of rows (BotFather-style multi-row keyboard). Shape detection is unambiguous (flat entry elements are strings; grid entry elements are pairs). Mixed lists: a row entry is a row, a stray pair entry becomes its own single-button row, order preserved. |
| `step_text`      | dict step->str  | rec.     | per-step prompt copy. Missing on a non-first step -> markup-only swap. |
| `step_validate`  | dict step->rgx  | rec.     | per-step value gate (regex). When present it is the source of truth; else the declared button values are the whitelist. |
| `step_final`     | str digit       | no       | the step whose tap FIRES the cmd. Default `"2"` (DGN-918/924 two-step). |
| `nav_back`       | bool            | no       | `true` -> append a « Back button on every step after the first. Default off (byte-identical to DGN-918/924). |
| `step_back_label`| str             | no       | override the Back button label (default localized `« 뒤로` / `« Back`). |
| `cmd`            | list[str]       | yes      | argv fired on the final tap. `argv[0]` = absolute path. `{i}` elements are substituted with the i-th captured step value. Every other element is verbatim. |
| `cmd_skip2`      | list[str]       | no       | argv fired when the final tap value == `skip`. Must NOT reference the skipped step's `{i}` token. |
| `followup_cmd`   | list[str]       | no       | OPTIONAL follow-up argv (DGN-939 item2). Fired ONLY after the terminal argv (`cmd`/`cmd_skip2`) exits 0; same security invariants and same `{i}` substitution/capture list as `cmd`. Its STDOUT is posted to the owner chat as a NEW inline message (never an edit). Absent/empty -> byte-identical to today (no follow-up post). |
| `confirm_fmt`    | str             | no       | confirmation headline. Positional `{i}` renders the i-th value of the FULL ordered capture list (DGN-966; skip path appends the literal `skip` as the final positional). 2-step arms: `{1}`/`{2}` byte-identical to DGN-918/924. `{sec}` named extra for hold arms. Fallback: bridge default. |
| `confirm_fmt_skip`| str            | no       | headline for the skip path. |
| `confirm_fold`   | dict            | no       | DGN-939 item6 expandable blockquote appended below the headline. `{"summary": str, "body": str}`. Both may carry positional `{i}` tokens (full capture list, DGN-966). Empty/blank summary -> no fold. |
| `is_hold`        | bool            | no       | hold-style arm (timer). Selects the `{sec}` confirm render path. |
| `hold_sec`       | int             | no       | hold seconds for the hold confirm render. |
| `session_id`     | str             | no       | opaque; logged only. |

Bridge-reserved runtime fields (the consumer never writes these; the bridge
manages them): `_nav` (the capture stack `[[step, value], ...]`),
`_pending_step1` (kept in sync for the 2-step confirm render).

Special step values (bridge-level meaning; the arm still declares them as
button values so they pass the gate):
- `skip` -> fires `cmd_skip2` instead of `cmd` (final step only).
- `other` -> emits a free-text prompt, NO fire, arm left for TTL cleanup.
- `back` is NOT a declarable value; it is the reserved Back callback action.

## 2. Rendering the first keyboard: the output marker

Emit `[[IDRILL:<arm_id>]]` on its own line in the output body. The bridge
strips the marker and renders `step_buttons["1"]` + `step_text["1"]` as an
inline keyboard on that message (homomorphic to `[[OPTIONS]]`; no file
watcher). The entry step is always `"1"`.

Path independence (DGN-966): the marker renders through ONE shared
artifact-render contract (`bridge/artifacts.py` spec builder) on EVERY send
path -- model turn (`_reply_smart`), fast-path handler stdout (DGN-801,
`_send_smart`), proactive push (`_proactive_push`), and the out-of-process
`routines/push.sh` rail (python hop, same spec builder). The same arm renders
the same keyboard regardless of which path carried the content; later step
taps are always handled by the live bridge callback engine (same bot token),
so drilldown works on push-sent keyboards too. A button whose callback_data
would exceed Telegram's 64-byte limit is dropped with a warning (the message
and its sibling buttons survive).

## 3. Callback grammar (bridge -> engine, no model turn)

```
idrill:<arm_id>:<step>:<value>   step tap (step = "1".."N")
idrill:<arm_id>:back             « Back: pop one capture, re-render prior step
```

Engine behavior (all in-place edits, model turn 0, outbound 0):
- non-final step tap -> gate value, push `(step, value)` on `_nav`, swap the
  message to the NEXT declared step's keyboard (`editMessageText` when the next
  step declares `step_text`, else `editMessageReplyMarkup`).
- final step tap -> re-read arm, CONSUME the file (double-tap safe), fire the
  declared `cmd`/`cmd_skip2` with `{i}` substituted from the full capture list,
  edit the confirmation (HTML render when `confirm_fold` is present).
- `:back` -> pop the last `_nav` capture, re-render the step it came from.
  Underflow (empty nav) -> `IDRILL_NAV_ROOT` message, no crash.
- `:<step>:other` -> free-text prompt, no fire, arm kept.

Ordering: `step_buttons` keys are walked in numeric order; step K advances to
the next declared numeric key, until `step_final` fires.

## 4. Command execution (fire) -- security invariants

- List-form subprocess, NEVER a shell. No PATH lookup, no cwd change, no env
  injection. 15s timeout. Exit 0 == success.
- `argv[0]` MUST be an absolute path. The bridge ENFORCES this: a relative
  (or absent) `argv[0]` -- checked on the post-substitution argv -- is refused
  before any subprocess (a relative argv[0] would let the OS fall back to a
  PATH search, defeating the no-PATH-lookup invariant). Applies to `cmd`,
  `cmd_skip2`, and `followup_cmd` alike.
- Only exact `{i}` elements are substituted; substrings (`x{1}y`) are verbatim.
- Every substituted value is whitelisted against its step's own
  `step_validate` regex (or the declared button values). An out-of-declaration
  value ABORTS the fire (no subprocess) -> `IDRILL_ERROR`.
- A `{i}` token with no i-th capture (e.g. `{2}` in `cmd_skip2`, or `{3}` on a
  2-step arm) is a malformed declaration -> no fire, `IDRILL_ERROR`.
- The arm file is consumed regardless of fire outcome (no double-submission).

### 4.1 followup_cmd (optional inline table refresh)

- Fired ONLY when the terminal argv exited 0. Terminal fire failed / aborted
  -> no followup subprocess, no post.
- Same execution invariants as `cmd` (list-form, no shell, no PATH, 15s
  timeout) and the same exact-match `{i}` substitution over the SAME capture
  list that fired the terminal argv (skip path: the skipped step's capture is
  absent -- a followup token referencing it is malformed).
- On exit 0, its non-empty STDOUT is posted to the owner chat as a NEW inline
  message (md->HTML render; never an edit of the confirmation). Empty STDOUT
  -> no post.
- All followup failures (malformed declaration, bad token, timeout, nonzero
  exit) are FAIL-SOFT: logged, no post, the confirmation stands -- a followup
  problem never turns a succeeded fire into an error.
- Mutual exclusion (double-fire guard): the followup post exists ONLY on the
  zero-model-turn callback fire path. The model-turn set-add path keeps its
  existing PostToolUse hook as the sole table-refresh author there; the two
  paths never overlap.
- The bridge stays verb/domain agnostic: WHAT the followup prints (e.g. the
  set-progress table) is entirely the consumer's argv declaration.

## 5. Deterministic log -> table refresh (item2)

The engine fires ONE declared argv on the final tap (plus the optional
`followup_cmd`, section 4.1) and nothing else. Logging
and any table/dashboard refresh are the CONSUMER's responsibility inside that
argv: `cmd` should both record the log entry AND (re)write the dashboard file
that DGN-214 mirrors into the pinned message. The pin then updates in place via
the existing dashboard sync (no model turn, no bridge call). The engine gives a
deterministic single-fire hook; the refresh is a side effect of the declared
command, keeping the bridge domain-agnostic.

## 6. Completion-push / hold-timer attach (item5)

Two deterministic attach points, both consumer-selected:
- Working-set style: arm the `[[IDRILL:..]]` marker onto whichever message the
  consumer wants to carry the buttons -- including a completion/"workout done"
  proactive push. The marker renders the keyboard on ANY message it rides.
- Hold-timer style: the countdown primitive (DGN-915) already attaches its own
  single done-button on the timer-completion message (`cdn:done:` callback,
  reply_markup cleared on tap). That is the single source for hold completion.

The bridge exposes both attach points; WHICH push carries WHICH affordance, and
the arm/attach TIMING, are arm-declaration / consumer decisions (Skull wires
Warg values). The bridge holds no attach policy.

## 7. Pin sync (item5) -- reliability

`dashboard.py` (DGN-214) mirrors the agent-authored dashboard file into one
pinned owner-chat message by editing it in place: rate-guarded (>=3s), deferred
while a turn is in flight, recreated on "message can't be edited" / "not found",
empty-state debounced before unpin+delete. Verified robust (168 dashboard +
countdown tests green); no change needed for DGN-939. To refresh the pin,
the consumer's `cmd` rewrites the dashboard file -- the sync task does the rest.

## 8. Backward compatibility

DGN-918/924 two-step arms (no `step_final`, no `nav_back`, `{1}`/`{2}` tokens)
run byte-identically: `step_final` defaults to `"2"`, step 1 advances to step 2,
step 2 fires. All 93 DGN-918/924 tests pass unchanged.
