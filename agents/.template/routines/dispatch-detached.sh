#!/usr/bin/env bash
# routines/dispatch-detached.sh -- session-detached background dispatch (DGN-991 rev3, phase 1)
#
# WHY: interactive Task subagents live IN-PROCESS inside the session CLI.
# /stop (soft interrupt or hard teardown SIGKILL) kills them with the turn.
# This wrapper spawns long autonomous work as a claude -p worker in its OWN
# SESSION (python3 fork -> os.setsid -> exec, same escape as
# bridge/self_restart.sh DGN-1010), so /stop only cuts the conversational
# turn and background work survives. bridge/* is untouched by design: the
# hard-teardown invariant ("no ownerless processes") is preserved by giving
# the detached worker a NEW owner = ledger row + run dir.
#
# USAGE:
#   dispatch-detached.sh --task <prompt-file> --model <fable|opus|sonnet|haiku>
#     --bottleneck <coverage|accuracy|judgment>   (DGN-1218: the TASK's
#         bottleneck axis; the model is checked against the measured binding
#         in lib/dispatch-tier-bindings.tsv -- data, never gate logic)
#     --bottleneck-why "<one line>"  (DGN-1485: required task evidence for
#         the declared axis; recorded verbatim for later calibration)
#     [--runtime claude-code|codex|cloud] (flag > env > measured selection;
#         with no runtime given, cloud is tried FIRST for a cloud-eligible
#         run -- see CLOUD-FIRST below -- then codex vs claude-code.
#         DGN-1742: claude-code|codex on a run that cloud-first WOULD take
#         is REFUSED unless --local-why states why it must stay local)
#     [--repo-contained]   (with --worktree or --repo: declares that the
#         work (build and tests too, for a write run) needs only the target
#         repo -- the one thing that makes a WRITE run cloud-eligible; never
#         inferred)
#     [--local-why "<reason, >=10 chars>"]   (DGN-1742: the stated reason a
#         repo run stays local. A --repo or --worktree-repo run (not
#         --runtime cloud) must carry --repo-contained OR --local-why --
#         neither is REFUSED. Also required for an explicit claude-code|codex
#         runtime on a cloud-eligible run, and to run local when cloud-first
#         is blocked only by unpushed base commits (else REFUSED: push the
#         base to origin first). With it, cloud-first never takes the run.
#         Recorded verbatim in run.env/status.json and as local_why= on the
#         ledger running row. No '|' or newline; refused on --runtime cloud
#         and on a workspace-only run, where it would mean nothing)
#     [--why "<one line>"]   (mandatory ONLY when the chosen model is outside
#         the measured binding for that axis; recorded verbatim in the ledger.
#         DGN-1512: this line is for WHY THIS AXIS/MODEL, not for a usage
#         number -- the script attaches its own machine-read usage snapshot
#         (TIER_WHY_USAGE) alongside it automatically, so a usage claim typed
#         here is checked against the live account state, never just trusted;
#         a fable-availability claim that disagrees with the live
#         .fable-exhausted flag REFUSES the spawn instead of proceeding.)
#     [--effort low|medium|high|max]   (DGN-1666: reasoning effort for the
#         worker. claude-code: `claude --effort <v>`. codex (DGN-1770):
#         `codex exec -c model_reasoning_effort=<v>` for low|medium|high; max
#         has no verified codex equivalent and is recorded, NOT passed. cloud:
#         no verified control -- recorded, NOT passed, and a note says so.
#         DGN-1770: OPTIONAL like --model. Omitted -> resolved from
#         lib/dispatch-effort-bindings.tsv for the FINAL (runtime, axis,
#         model): a measured level is passed (EFFORT_SOURCE=table), a cell
#         measured to have no effort effect or never measured passes no flag
#         (EFFORT_SOURCE=no-effect / unmeasured -> CLI default). Explicit
#         --effort wins (EFFORT_SOURCE=explicit). Recorded in run.env,
#         status.json and the spawn banner. sonnet + high on the accuracy axis
#         prints a WARNING (effort bench: 2/5 completed, no accuracy gain))
#     --label <label> [--ticket DGN-NNN] (--read-only | [--write] --worktree <slug>) [--timeout <min, default 60>]
#     --display-name "<text>"   (the name OWNER surfaces render for this run.
#         The label above is machine-shaped (ASCII, lowercase, hyphenated) so
#         it can be a branch/dir/log key; this one is written for a human in
#         the instance's own language. REQUIRED on a non-English instance --
#         see "DISPLAY NAME" below)
#     [--notify silent|push]   (default: push -- the owner-designed END block
#         reaches the owner from THIS shell, not from the model's memory to
#         relay it; silent stays as the explicit opt-out. See END NOTICE below.)
#     [--unguarded-codex "<reason>"]   (codex runtime only: LOUD override of the
#         DGN-1444 hook-trust gate -- spawns the write-capable codex worker even
#         though its hook enforcement is not verified active. Reason >=10 chars,
#         recorded verbatim in run.env/status.json and as hooks=UNGUARDED on the
#         ledger running row. Never use it as a default.)
#     [--repo <path>]   (read-only mode only: worker cwd -> <path>; excludes --worktree.
#                        --runtime cloud: REQUIRED -- the git repo whose GitHub
#                        origin the cloud routine clones and pushes claude/<label> to)
#     [--worktree-repo <path>]   (requires --worktree: provision the worktree in THAT
#                                 git repo instead of the workspace -- WRITE-capable
#                                 target repo; artifacts stay workspace-anchored)
#     [--base <branch>]   (requires --worktree: branch point + freshness reference;
#                          default: dev when the target repo carries .dogany-canonical
#                          AND has a dev branch (DGN-1184 (A)), else main.
#                          --runtime cloud: the branch the routine checks out
#                          first; must be on origin)
#     [--live-target <path>]...   (requires --read-only, DGN-1458 PLAN LANE: a live
#                          path the worker may NOT write but plans changes for in
#                          RUNDIR/plan/plan.json; applied by --apply-plan)
#   dispatch-detached.sh --cancel <runid>
#   dispatch-detached.sh --apply-plan <runid> [--apply]   (dry run without --apply)
#   dispatch-detached.sh --list
#   dispatch-detached.sh --calibration   (recent dispatch history -- the reader)
#   dispatch-detached.sh --backfill-model-observed [--apply]
#     (DGN-1227: retroactively write status.json.model_observed for past runs
#     whose stream.jsonl survived. Default dry-run (reports candidates,
#     writes nothing); --apply patches only the missing field, tagged
#     model_observed_backfilled=true. Never touches runs that already carry
#     the field or that have no stream.jsonl left.)
#
# CONTRACTS (locked spec: worklog/DGN-991-stop-kills-background-work.md rev3):
#   - Gates run SYNCHRONOUSLY before detach: usage-gate.sh (fail-CLOSED),
#     .claude/.fable-exhausted pre-check (fable -> opus demotion, logged),
#     active-count cap 3, prompt size cap 100KB (ARG_MAX).
#   - DGN-1158: the fable-exhausted flag is also WRITTEN from this script --
#     a fable worker that dies on the "requires usage credits" weekly-cap
#     signature records the flag in its own exit path (do_worker) so the
#     NEXT fable spawn's pre-check above demotes automatically. No auto
#     re-dispatch of the run that just died.
#   - BOTTLENECK GATE (DGN-1218): a spawn must declare the task's bottleneck
#     axis (--bottleneck coverage|accuracy|judgment). LAYER 1 (this script's
#     logic) knows only that axis enum -- it is a property of the TASK, not
#     of any model, so it survives every provider change. LAYER 2
#     (lib/dispatch-tier-bindings.tsv) is DATA binding (runtime, axis) ->
#     measured models (DGN-1193 replay measurement). A model outside the
#     binding -> spawn REFUSED unless --why states the exception (recorded
#     verbatim in run.env/status.json). A missing (runtime, axis) row ->
#     pass-through with a notice: an unmeasured runtime is never blocked
#     (3.0 boundary -- "do not build, do not block"). A missing data FILE ->
#     refused, fail-closed. Model names must NEVER appear in the gate logic;
#     a new runtime is new rows, zero code
#     (locked by tests/test-dgn1218-dispatch-gate.sh).
#   - AXIS EVIDENCE GATE (DGN-1485): an enum alone does not establish that it
#     describes this task. Every spawn must give a one-line --bottleneck-why;
#     it is persisted with the axis so later calibration can audit the
#     classification. This asks for evidence but does not invent a brittle
#     brief classifier: deriving an axis from task text remains a separate
#     design problem.
#   - DISPLAY NAME (--display-name): ONE dispatch carries TWO names, and they
#     are not the same kind of thing.
#       * --label is the MACHINE label: it is reused as a ledger key, a run
#         identifier component and a log key, so it is written ASCII /
#         lowercase / hyphenated. Nothing about its uses changes here.
#       * --display-name is the name a HUMAN reads. Owner surfaces render it.
#     Before this field existed, the machine label WAS the owner-facing name:
#     the pinned work board reads the ledger item column, which was the label
#     verbatim, so an English hyphenated dispatch title appeared on the owner's
#     own screen on an instance that does not speak English.
#     SELECTION is language-conditional: the resolved instance language
#     (AGENT_LANG in config/agent.conf, read through the shell rail's one
#     locale reader lib/agentlib.sh -- see resolve_owner_lang) decides. Not
#     English -> owner surfaces render the display name. English -> the
#     machine label already IS readable owner-facing text and is used as
#     before (that is also why the field is not demanded on an English
#     instance: it would be a mandatory argument that renders nowhere).
#     The field is REQUIRED, not encouraged, wherever it is rendered: an
#     optional field is skipped under load and the owner surface silently
#     falls back to the machine label -- the exact defect this closes. Hard
#     refusal at spawn (exit 1, same shape as the --label/--bottleneck
#     refusals); no exemption, because every dispatch lands a ledger row that
#     the work board renders, so there is no dispatch whose name the owner
#     never sees. Locked by tests/test-dispatch-display-name.sh.
#     WHERE IT IS RENDERED: the ledger item column at spawn -- that column is
#     what the pinned work board prints, so the board is wired by the value
#     written into it and needs no renderer change. The raw field is also
#     persisted (run.env DISPLAY_NAME, status.json display_name) so every
#     other consumer selects through owner_facing_name() instead of deriving
#     a name of its own. SECOND RENDER SITE (2026-09-11): the completion PUSH
#     in finalize_run. The first pass left it nameless on the reading that the
#     push is a signal for the agent, not a report for the owner; the owner
#     reversed that -- a notice he can read is one that names the job -- so the
#     push now renders owner_facing_name() too; the completed branch renders
#     the owner-designed END block and the failure branches keep their
#     confirmed/draft one-liners (see END NOTICE below).
#   - CALIBRATION READER (DGN-1218): the spawn path prints the recent
#     dispatch history (model/axis/result/wall/product from status.json --
#     the ledger that already existed and nobody read) BEFORE the gates run,
#     so the tier decision is made against data, not memory. Standalone:
#     --calibration.
#   - active-count (.telegram_bot/dispatch/active-count) is a SEPARATE ledger
#     from autonomous-loop's DAILY_SPAWN_BUDGET / PER_RUN_CAP. The loop budget
#     governs queue juniors; this counter governs interactive detached
#     dispatches only. Never merge the two.
#   - --worktree <slug>: branch dsp/<slug> (namespace-separated from loop's
#     auto/*), freshness gate (the BASE branch must be ancestor). WITHOUT --worktree,
#     an explicit --read-only is required for OBSERVE mode: kernel-enforced read-only (macOS seatbelt,
#     profile rendered to $RUNDIR/observe.sb) with Bash fully open inside --
#     blocks a second writer on the live checkout. See "OBSERVE MODE" below.
#   - WRITE-MODE ISOLATION (DGN-1504): a claude-code --worktree worker runs
#     under a kernel profile rendered per run (RUNDIR/write.sb) that closes
#     every OTHER checkout on the estate roster (adjacent repos + registered
#     shared checkouts, e.g. the canonical DOGANY_REPO_ROOT) and the protected
#     branch refs (main/dev/master/base), keeps the git object stores open, and opens
#     only the run's own worktree. Proven by a self-check before spawn;
#     sandbox-exec absent -> REFUSED (rc 2). The work location is injected
#     into the worker's system prompt, and the shared checkouts are
#     snapshotted at spawn/finalize (status.json checkout_drift). See the
#     "WRITE-MODE ISOLATION" section (DGN-1504).
#   - PLAN LANE (DGN-1458): a live workspace path (instance dir, its
#     database, a crew dir) is no checkout, so --worktree cannot reach it and
#     observe mode denies it. --read-only --live-target <path> keeps the
#     worker the same kernel read-only verifier and opens exactly one extra
#     place, RUNDIR/plan/, where it writes plan.json; --apply-plan <runid>
#     [--apply] runs routines/plan-apply.py OUTSIDE the kernel: declared
#     targets only, preconditions re-checked, backups, row-count guard, once.
#     Design: DGN-1458.
#   - --worktree-repo <path> (requires --worktree, DGN-1012): the repo the
#     worktree is CUT FROM. Default = WORKSPACE, so a bare --worktree call is
#     byte-for-byte the old behaviour (zero regression). With it, a dispatch
#     can WRITE to another repo (canonical, a product repo) through the same
#     worktree isolation -- the combination that did not exist before and
#     forced four hand-carrybacks on 2026-08-24 (--worktree always landed in
#     the dispatching instance's repo; --repo could reach another repo but only READ-ONLY).
#     Kept as its OWN flag rather than overloading --repo: --repo means
#     "read-only cwd redirect" and stays mutually exclusive with --worktree,
#     so one flag never means two things depending on its companions, and
#     write-to-foreign-repo dispatches are grep-able at the call site.
#     Everything except the git repo of the worktree is unchanged: RUNDIR /
#     out.log / status.json / session-inbox drop / ledger / active-count stay
#     WORKSPACE-anchored absolute paths. Freshness gate + cleanup run against
#     the TARGET repo (WT_REPO); the gate compares that repo's own BASE branch.
#   - BASE branch (DGN-1184 (A), 2026-09-01): the canonical framework repo
#     develops on `dev` (main = release line, dev -> main merges only). A
#     worktree cut from main there would build on a stale base and its merge
#     into dev would be judged against the wrong line. Resolution, printed on
#     the spawn line (never silent): --base <branch> if given; else `dev` when
#     the target repo root carries the tracked `.dogany-canonical` marker AND
#     refs/heads/dev exists (same marker scoping as git-hooks/pre-commit --
#     instance repos never carry it, so they keep main); else `main` (bare
#     workspace dispatch = byte-for-byte the old behaviour).
#   - --repo <path> (observe mode only, mutually exclusive with --worktree):
#     sets the worker cwd to <path>. cwd is the ONLY thing that moves:
#     RUNDIR / out.log / status.json / session-inbox drop / ledger /
#     active-count stay WORKSPACE-anchored absolute paths, and <path> joins
#     the sandbox deny list as the worker's WORKDIR. To WRITE in another repo
#     use --worktree <slug> --worktree-repo <path> instead.
#   - OBSERVE MODE (DGN-1191, replaces the --allowedTools whitelist): the
#     read/write boundary is a FILESYSTEM invariant, and the permission engine
#     only sees command-line text -- it cannot see that "git log --output=X",
#     "sqlite3 -readonly ... writefile()" or "python3 -m json.tool in out"
#     write files (all measured, docs/dgn1191-lab/). So the invariant is
#     enforced where it lives: the whole worker process tree runs under
#     sandbox-exec with (deny file-write*) and only /dev, TMPDIR, /tmp and
#     the CLI's own ~/.claude session state re-opened; WORKDIR / WORKSPACE /
#     WORKTREE_BASE are denied LAST (seatbelt: last matching rule wins, so
#     a target living under /tmp is still denied). Tool policy inside:
#     --permission-mode default --allowedTools Bash --disallowedTools
#     Task,Agent,Write,Edit,NotebookEdit. Write/Edit deny is belt only; the
#     kernel is the enforcement. sandbox-exec absent -> spawn REFUSED (rc 2),
#     never a silent fallback to an allowlist. Design + evidence:
#     docs/DGN-1191-observe-mode-design.md. Tests (model-free):
#     routines/tests/test-dispatch-observe.sh.
#   - Ledger writes go through routines/lib/ledger-update.sh ONLY. Running
#     note format mirrors the loop ("dispatch 스폰 (model=$MODEL)") so
#     status-footer.py's existing model= parser reads it unchanged.
#   - End states: commits present -> pending-review; report-only ->
#     deferred --flag dsp-report; timeout/error/cancel -> failed. No new
#     state enum.
#   - Push is issued by THIS shell (worker postprocess), never by the model.
#     Model output arrives as files only ("LLM judges, shell writes").
#   - No instance paths hardcoded: everything resolves from SCRIPT_DIR
#     (push.sh:35-39 pattern) -- canonical-bound.
#   - First-run bootstrap: product/auto-loop-ledger.md is seeded with an
#     EMPTY table skeleton when absent (fresh mints ship no product/ dir and
#     update.sh never syncs product/). Row writes still go through
#     lib/ledger-update.sh only -- the seed never contains a row.
#   - Completion return path (DGN-1012 closure axis): EVERY terminal state
#     (completed/timed-out/canceled/error) drops
#     <workspace>/.telegram_bot/session-inbox/dispatch-<runid>.md for the
#     bridge poller (bot.py _session_inbox_loop, DGN-217) to inject into the
#     dispatching session -- the owner push alone leaves it blind to completion.
#     --notify silent suppresses the OWNER push only; the inbox drop always
#     happens.
#   - END NOTICE: the completion push renders the owner-designed END block
#     (identity Response workflow, "Completion signal"): title
#     `✅ <background-term> 작업 완료` + `- <Model>: <작업명> <HH:MM> (<소요시간>)`.
#     The title term is the INSTANCE's background term (background_term
#     below; owner 2026-09-28, DGN-1769: the same title as the start notice
#     and the workbench section) -- never a fixed string; the canonical
#     default is the neutral 백그라운드. Default ON (--notify push): before this the
#     completion reached only the session inbox and the model relayed the
#     block by hand -- a block the model must remember to type is a block
#     that goes missing (the START NOTICE ruling below, applied to the end
#     side). dec-126's silent default predated that design; silent stays as
#     the explicit opt-out. One run per finalize, so the end title never
#     carries a batch count; completions do not share the start notice's
#     join window because a burst's runs end minutes apart, not seconds
#     (the window exists for spawn bursts, which are one decision;
#     completions are separate events).
#   - START NOTICE (owner ruling 2026-09-11): the owner is told when work
#     BEGINS, not only when it ends. Until now the only start signal was the
#     agent saying so in prose, and prose depends on the agent remembering --
#     on the day of the ruling it was forgotten repeatedly and the owner had
#     to ask whether work had started at all. A machine-emitted notice does
#     not depend on that diligence.
#       * BATCHED, one message per burst (owner's own choice between batched
#         and per-dispatch): N dispatches fired together produce ONE notice.
#         Per-dispatch notices would have doubled the traffic of a day that
#         already carried 20+ dispatches, against a standing complaint about
#         message fragmentation.
#       * The join is a SENDER-SIDE TIME WINDOW (start_notice_arm below plus
#         the --start-notice-flush mode): there is no batch abstraction in
#         this dispatcher -- each invocation is an independent process -- so
#         the first spawn of a burst claims one detached flusher, every spawn
#         appends its owner-facing name to one queue file under the same
#         lock, and the flusher sends a single push START_NOTICE_WINDOW
#         seconds later. A caller-passed batch id was rejected: a lone
#         dispatch is a legitimate call, so an ABSENT id is indistinguishable
#         from a correct one and no refusal can be armed at the spawn gate --
#         the field would silently degrade to per-dispatch notices the first
#         time the caller forgot it. (Contrast DISPLAY NAME above, where a
#         refusal WAS armable and is therefore the mechanism used there.)
#       * Independent of --notify: that flag governs the COMPLETION push
#         only -- gating the start notice on it would tie two different owner
#         signals to one switch. This notice IS the event the owner asked
#         for by name. (When this rail was built the completion default was
#         still silent under dec-126; the END NOTICE above has since flipped
#         it, but the independence stands.)
#       * Owner-facing names only (owner_facing_name), never runid / branch /
#         machine label / commit -- so the notice passes the send rail's
#         execution-identifier guard instead of depending on it.
#       * Emitted only AFTER the detach succeeds: every gate refusal and every
#         provisioning failure exits before the arming call, so a refused or
#         failed dispatch produces no notice. The push can never fail the
#         spawn either -- the sender is a separate detached process that the
#         launcher does not wait on.
#   - SESSION BACKGROUND JOBS (DGN-1691 phase 2, owner 2026-09-24 "다른 백
#     작업들도 동생보내듯이 규약 맞춰줘"): a background Bash / Monitor /
#     Workflow job launched by the owner session gets the SAME two notices
#     as a dispatch, through THIS file's rails -- never a second notice
#     mechanism. Two internal modes, called only by lib/bg_job_notice.py:
#       * --start-notice-enqueue --display-name <name> --model-slot <slot>:
#         start_notice_arm() verbatim, so a shell job joins the same queue,
#         the same flusher claim and the same window as a dispatch spawned
#         next to it (one `▶️ <background-term> 작업 시작 (N건)` message
#         for the burst; DGN-1767: the start title is background_term() for
#         every burst, dispatch or job).
#         --display-name arrives as the owner-visible name: the job's
#         description alone (lib/bg_job_notice.py owner_label; DGN-1820
#         supersedes the DGN-1727 "<slot>: <description>" label), so
#         --model-slot is carried but NEVER rendered by this rail.
#       * --end-notice --display-name <name> --model-slot <slot>
#         --result <completed|failed|killed> [--elapsed-secs N]
#         [--end-clock HH:MM] [--not-before <epoch>]: renders
#         end_notice_text() -- the same function
#         finalize_run uses, so the ✅ block and the ⚠️ lines have one owner.
#     Exactly-once and the owner-session gate live in the caller (it keeps
#     the per-task sent-state); these modes send what they are told.
#   - CLOUD RUNTIME (DGN-1726, owner 2026-09-26 "클라우드 세션도 백그라운드
#     작업 리스트에 똑같이 들어가게해줘. Sonnet 5(Cloud) 이런식으로"): a Claude
#     Code cloud session is ONE MORE RUNTIME of this dispatcher, never a side
#     tool -- same ledger row, same run dir/status.json, same batched START
#     notice, same END notice, same dispatch-return drop, same workbench row.
#     What differs is only where the work runs and how completion is observed:
#       * transport (r2): a one-shot ROUTINE, not `claude --cloud` -- a CLI
#         cloud session starts from a seed bundle with no git remote and its
#         push is refused (403, verified 2026-09-26); a routine whose
#         session_context.sources names the GitHub repo clones it with origin
#         set. The routines API is reachable only through Claude Code's
#         in-process RemoteTrigger tool, so lib/cloud_routine.py spawns a
#         short local `claude -p --model sonnet` (ToolSearch + RemoteTrigger
#         only) for each call: start = create (run_once_at in the future) ->
#         run -> disable, status = list_runs (+ get_run_log when ended),
#         disable. No raw HTTP, no token reads. The call runs INSIDE the
#         detached worker, so the spawn returns at once.
#       * config: CLOUD_ENV_ID in config/agent.conf (env override
#         DOGANY_CLOUD_ENV_ID) names the cloud environment -- absent ->
#         spawn REFUSED. The repo URL is derived from --repo's origin
#         (https://github.com/<owner>/<repo>); a non-GitHub origin -> REFUSED.
#       * base: routines clone the DEFAULT branch, so the task is prefixed
#         with `git fetch origin <base> && git checkout -B work origin/<base>`
#         (base = --base, else --repo's current branch when it is on origin,
#         else main). A machine-appended footer (cloud_task_footer): push only
#         to the NEW branch claude/<label> (routines may push claude/*),
#         never dev/main, no PR, end with branch + sha lines. claude/<label>
#         already on origin at spawn -> REFUSED.
#       * completion: the detached worker polls `git ls-remote origin
#         claude/<label>` (DOGANY_CLOUD_POLL_SECS, default 60) AND the run
#         status (DOGANY_CLOUD_STATUS_SECS, default 300). Branch -> completed
#         with its sha. Run ended without the branch -> failed AT ONCE
#         (owner 2026-09-26 17:07: a silent failure leaves the owner not
#         knowing what happened) -- as are a create/run failure and the
#         --timeout (default 120 for cloud). Every failure goes through the
#         existing finalize: ledger failed row, END notice, dispatch-return
#         with the reason line and the view URL. Trigger id, session id,
#         view URL and reason land in RUNDIR/cloud.env and status.json.
#       * gates: the local usage gate is SKIPPED (cloud spends cloud credit
#         first -- a tight local 5h window is not this lane's resource) and so
#         is the fable demotion (same account concept); the ledger, the
#         prompt-size cap and an active-count cap of its OWN (active-count.cloud,
#         DOGANY_DISPATCH_MAX_ACTIVE_CLOUD, default 3) stay. The decision
#         record carries selected_runtime=cloud + reason cloud_usage_gate_skipped.
#       * display: lib/model_display.py renders the full model name for this
#         runtime (the pinned id gives the version: "Claude Sonnet 5"; no
#         runtime suffix, owner 2026-09-27 13:38) on the
#         notices, the dispatch-return and the workbench row.
#     --cancel stops the local watcher and disables the routine; the remote
#     session itself is not stoppable from here (the dispatch-return says so
#     and carries its URL).
#   - CLOUD-FIRST (DGN-1726, owner 2026-09-26 21:39: put cloud into the
#     dispatch machine like codex, first in line): with no --runtime/DOGANY_DISPATCH_RUNTIME, cloud
#     is the FIRST auto candidate, ahead of the codex/claude-code measured
#     selection. It is chosen only when EVERY check in cloud_first_check
#     holds, conservatively -- any doubt keeps the run local:
#       * window: today <= CLOUD_FIRST_UNTIL (env DOGANY_CLOUD_FIRST_UNTIL >
#         agent.conf > default 2026-11-05, the persona rule's expiry; "off"
#         disables) and no live .claude/.cloud-exhausted flag (same schema
#         as .fable-exhausted: {"expiry": iso8601}; written by hand when the
#         cloud credit is spent);
#       * work kind: --read-only with an explicit --repo, or --worktree with
#         an explicit --worktree-repo AND --repo-contained. A bare --worktree
#         (cut from the live workspace) never goes to cloud;
#       * no live instance: the target repo is not under ~/.dogany, not the
#         workspace (or containing it), not an instance root (config/
#         agent.conf), and the task text names no .dogany path, launchctl/
#         launchd/LaunchAgents, rehearsal or the workspace path;
#       * cloud runnable: GitHub origin, CLOUD_ENV_ID configured, label valid
#         as claude/<label>, (cloud, axis) binding row holding the model,
#         origin readable, claude/<label> not yet on origin;
#       * same code: the base branch (read-only: the checked-out branch, with
#         no uncommitted tracked changes; worktree: --base or the resolved
#         worktree base) is on origin and has NO unpushed local commits --
#         the routine clones origin. Unpushed commits REFUSE the spawn
#         (DGN-1742, below) with the count printed; this script never pushes
#         on the caller's behalf.
#     A worktree run moved to cloud delivers claude/<label> on origin instead
#     of a local dsp/<slug> branch. One line always says why cloud was or was
#     not chosen; selection_reason (cloud_first_eligible) or the not-chosen
#     code (selection_detail "cloud_first=...") lands in the decision record
#     and status.json.
#   - LOCAL-WHY FORCING POINTS (DGN-1742, owner 2026-09-27 08:28): repo work
#     used to skip cloud SILENTLY three ways -- an explicit --runtime
#     claude-code|codex needed no reason, unpushed base commits quietly fell
#     back to local, and a --worktree-repo run was local unless the caller
#     remembered to type --repo-contained. Each is now a synchronous refusal
#     before spawn, like the other gates; the one way past is a stated reason,
#     --local-why "<>=10 chars>", recorded verbatim (run.env LOCAL_WHY,
#     status.json local_why, ledger running row local_why=...):
#       1. a --repo / --worktree-repo run that is not --runtime cloud carries
#          --repo-contained or --local-why -- neither -> refused (exit 1);
#       2. an explicit --runtime claude-code|codex (flag or
#          DOGANY_DISPATCH_RUNTIME) on a run cloud_first_check would pass
#          -> refused without --local-why (exit 2);
#       3. cloud-first blocked by unpushed_commits -> refused with "push
#          <base> to origin first" without --local-why (exit 2); with it the
#          run proceeds local and the note is recorded.
#     With --local-why and no runtime, cloud-first never takes the run (the
#     reason is the caller's decision to stay local; the eligibility verdict
#     is still recorded as cloud_first=local_why: ...). --runtime cloud and
#     workspace-only runs (no --repo / --worktree-repo) are unaffected, and
#     --local-why is refused on them rather than ignored.
#
# EXIT CODES (launcher): 0 spawned/ok, 1 usage/config error, 2 gate refused
#   (usage gate / active cap / observe sandbox unavailable or self-check
#   failed), 3 worktree/freshness failure.

set -euo pipefail

# ---------------------------------------------------------------------------
# Path resolution -- SCRIPT_DIR relative, no instance hardcoding (spec cond. 1)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
DISPATCH_DIR="$WORKSPACE/.telegram_bot/dispatch"
ACTIVE_COUNT_FILE="$DISPATCH_DIR/active-count"
# DGN-1434: claude-code and codex are DIFFERENT accounts with different
# quotas -- a codex dispatch must never consume a claude-code slot (or vice
# versa). claude-code keeps the original filename (backward compat: existing
# tests read this path directly); codex gets its own counter file.
ACTIVE_COUNT_FILE_CODEX="$DISPATCH_DIR/active-count.codex"
# DGN-1726: the cloud runtime spends cloud credit, not either local account --
# its own counter, its own fixed cap (header, CLOUD RUNTIME).
ACTIVE_COUNT_FILE_CLOUD="$DISPATCH_DIR/active-count.cloud"
CLOUD_MAX_ACTIVE="${DOGANY_DISPATCH_MAX_ACTIVE_CLOUD:-3}"
case "$CLOUD_MAX_ACTIVE" in
  ''|*[!0-9]*|0) echo "[dispatch] DOGANY_DISPATCH_MAX_ACTIVE_CLOUD must be a positive integer (got '$CLOUD_MAX_ACTIVE') -- using 3" >&2
                 CLOUD_MAX_ACTIVE=3 ;;
esac
# The routine transport (header, CLOUD RUNTIME). Tests point
# DOGANY_CLOUD_ROUTINE_HELPER at a stub -- never a real cloud call.
CLOUD_ROUTINE="${DOGANY_CLOUD_ROUTINE_HELPER:-$SCRIPT_DIR/lib/cloud_routine.py}"
CLOUD_POLL_SECS="${DOGANY_CLOUD_POLL_SECS:-60}"
[[ "$CLOUD_POLL_SECS" =~ ^[1-9][0-9]*$ ]] || CLOUD_POLL_SECS=60
# Each status poll is one short local `claude -p` turn -- sparser than the
# free ls-remote poll.
CLOUD_STATUS_SECS="${DOGANY_CLOUD_STATUS_SECS:-300}"
[[ "$CLOUD_STATUS_SECS" =~ ^[1-9][0-9]*$ ]] || CLOUD_STATUS_SECS=300
CLOUD_DEFAULT_TIMEOUT_MIN=120
# Resolved at spawn (validate_spawn_args) and carried in run.env.
CLOUD_ENV_ID=""
CLOUD_REPO_URL=""
# Filled by the cloud worker (RUNDIR/cloud.env) and read back at finalize;
# empty defaults keep set -u quiet on every other runtime.
CLOUD_TRIGGER_ID=""
CLOUD_TRIGGER_DISABLED=""
CLOUD_SESSION_ID=""
CLOUD_VIEW_URL=""
CLOUD_SHA=""
CLOUD_FAIL_REASON=""
# CLOUD-FIRST (header): the auto-selection window and credit flag.
CLOUD_FIRST_DEFAULT_UNTIL="2026-11-05"
CLOUD_EXHAUSTED_FLAG="$WORKSPACE/.claude/.cloud-exhausted"
CLOUD_FIRST_SELECTED=0
CLOUD_FIRST_NOTE=""
COUNT_LOCK_DIR="$DISPATCH_DIR/.count.lock"
LEDGER_UPDATE="$SCRIPT_DIR/lib/ledger-update.sh"
USAGE_GATE="$SCRIPT_DIR/usage-gate.sh"
PUSH_SH="$SCRIPT_DIR/push.sh"
FABLE_FLAG="$WORKSPACE/.claude/.fable-exhausted"
LEDGER_FILE="$WORKSPACE/product/auto-loop-ledger.md"
# bot_data_dir of the bridge (config.bot_data_dir = <workspace>/.telegram_bot)
# -- same resolution as review-reminder.sh / decision-actions-watch.sh.
SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"
WORKTREE_BASE="${DOGANY_DISPATCH_WT_BASE:-/tmp/dogany-dispatch/worktrees}"
# DGN-1218 Layer 2: (runtime, axis) -> measured-model binding lives in DATA,
# never in this script's logic. New runtime = new rows there, zero code here.
TIER_BINDINGS="$SCRIPT_DIR/lib/dispatch-tier-bindings.tsv"
# DGN-1444: one shared DATA source for concrete runtime slugs and owner
# display labels. status-footer.py reads the same file.
MODEL_DISPLAY_BINDINGS="$SCRIPT_DIR/lib/dispatch-model-display.tsv"
# DGN-1770: (runtime, axis, model) -> measured effort default, same DATA rule.
EFFORT_BINDINGS="$SCRIPT_DIR/lib/dispatch-effort-bindings.tsv"
# DGN-1592: no runtime request means measured selection at spawn time.
# Keep the inert pre-spawn value for legacy function-level callers; do_spawn
# always resolves it before the tier/usage gates or any provisioning.
RUNTIME="claude-code"
REQUESTED_RUNTIME="${DOGANY_DISPATCH_RUNTIME:-auto}"
[[ "$REQUESTED_RUNTIME" == auto ]] || RUNTIME="$REQUESTED_RUNTIME"
RUNTIME_SELECTION_SOURCE="auto"
[[ "$REQUESTED_RUNTIME" == auto ]] || RUNTIME_SELECTION_SOURCE="env"
RUNTIME_SELECTION_REASON=""
RUNTIME_SELECTION_DETAIL=""

# ---------------------------------------------------------------------------
# DGN-1470 RUNTIME FALLBACK + PRE-SPAWN DECISION RECORD.
#
# Two defects this block closes (design: DGN-1470 runtime-fallback record,
# 2026-09-15):
#   1/2  claude<->codex candidate evaluation was asymmetric and partial --
#        claude's gate refusal dead-ended (return 2) and codex only fell back
#        on a quota READ failure, never on over-ceiling / hook refusal / slot
#        cap. Both directions now evaluate the alternate lane ONCE (never a
#        second switch, never a claude<->codex loop).
#   4    a dispatch refused BEFORE spawn left no run.env/status.json -- the
#        refusal existed only on a terminal nobody kept. Every fallback /
#        refusal / defer decision now writes a JSON record under
#        DISPATCH_DIR/decisions/, OUTSIDE any run dir, so worktree/rundir
#        cleanup can never erase the verdict.
#
# What a fallback candidate must pass is a function of WORK KIND x TARGET
# PATH, never of quota alone (2026-09-14: three codex worktree dispatches
# committed with every hook silently inert):
#   - write-capable (worktree) codex: verified hook trust at the target repo.
#   - observe codex: NO hook requirement -- its protection contract is the
#     outer seatbelt (do_spawn self-check); demanding hook trust there would
#     lose the read-only lane entirely.
#   - either direction: a measured tier-binding row (runtime, axis) that
#     includes the model. Automatic switches never reuse --why or
#     --unguarded-codex (validate_spawn_args already refuses the latter on a
#     non-codex request, so a claude-primary fallback can never inherit it).
# ---------------------------------------------------------------------------
RUNTIME_FALLBACK_DIRECTION=""
RUNTIME_FALLBACK_NOTE=""
# Set by the switch helpers on refusal so the caller's record names the exact
# blocking reason instead of a generic "codex refused".
FALLBACK_BLOCK_REASON=""
# Last claude usage-gate outcome (rc + final line) -- consumed by the defer
# decision and persisted in the decision record.
CLAUDE_GATE_RC=""
CLAUDE_GATE_NOTE=""
# Request-level decision records (defect 4). One JSON file per dispatch
# request; later calls for the same request overwrite with the FINAL verdict
# (the interim steps remain on stderr).
DECISION_DIR="$DISPATCH_DIR/decisions"
DECISION_ID=""
# Bounded-defer policy (design 3: mandatory policy values whose optimum is
# undetermined -- env-overridable; the deadline is NEVER extended on retry,
# it is set once at the first refusal and carried through every replay).
DISPATCH_DEFER_MAX="${DOGANY_DISPATCH_DEFER_MAX:-2}"
DISPATCH_DEFER_DEADLINE_MIN="${DOGANY_DISPATCH_DEFER_DEADLINE_MIN:-360}"
DISPATCH_DEFER_RETRY_MIN="${DOGANY_DISPATCH_DEFER_RETRY_MIN:-30}"
# Owner 2026-09-27 09:11: a usage LOOKUP failure (429 / network) is not a
# quota verdict -- retry after about a minute, not the quota interval.
DISPATCH_LOOKUP_RETRY_SEC="${DOGANY_DISPATCH_LOOKUP_RETRY_SEC:-60}"
DISPATCH_DEFER_ATTEMPT="${DOGANY_DISPATCH_DEFER_ATTEMPT:-0}"
DISPATCH_DEFER_DEADLINE="${DOGANY_DISPATCH_DEFER_DEADLINE:-}"
[[ "$DISPATCH_DEFER_ATTEMPT" =~ ^[0-9]+$ ]] || DISPATCH_DEFER_ATTEMPT=0
[[ "$DISPATCH_DEFER_DEADLINE" =~ ^[0-9]+$ ]] || DISPATCH_DEFER_DEADLINE=""
# Reset-aligned one-shot scheduler (DGN-835 machinery, reused not duplicated):
# the ONLY thing that lets this script report "deferred" -- a deferral without
# a persisted reservation is reported as blocked, never as "waiting".
USAGE_DEFER_RETRY="$SCRIPT_DIR/usage-defer-retry.sh"

# ---------------------------------------------------------------------------
# CODEX RUNTIME (DGN-1444, 2026-09-13 owner: "codex cli 설치하고 인증했다,
# claude code 동생 대신에 우선적으로 쓸 수 있냐 ... 조건은 codex 주간 한도
# 10%이상 남겨놓기").
#
# The axes that differ between claude-code and codex are exactly four
# (invocation, model name, stream format, quota gate); everything else in
# this file -- decomp/in-session gates (outside this script), tier-binding
# gate, ledger/runid/status.json, worktree isolation, notifications, prompt
# cap, display-name -- is SHARED and must never be forked per runtime.
#
# MODEL stays the caller-facing vocabulary (fable|opus|sonnet|haiku)
# regardless of runtime -- no call site changes. The shared model-display DATA
# below owns concrete codex identifiers and owner-facing display labels.
# Measured 2026-09-13 (codex-cli 0.154.0, ChatGPT account): "solar" is NOT a
# valid slug for the opus tier -- it is "sol".
CODEX_RATELIMIT_SCRIPT="$SCRIPT_DIR/codex-ratelimit.py"
CODEX_QUOTA_TIMEOUT="${DOGANY_CODEX_QUOTA_TIMEOUT:-20}"
# DGN-1434: claude's own live-usage reader, reused (not duplicated) for the
# dynamic concurrency cap below. DGN-1512: this used to point at
# claude-usage.sh itself, shelled out to fresh on every single spawn -- N
# dispatches in a fan-out meant N live calls to /api/oauth/usage, and a
# 3-dispatch batch measured 2026-09-16 13:23 tripped a 429 on the third call.
# usage-gate.py already wraps that exact script behind a 300s TTL file cache
# (get_usage(), .claude/.usage-cache.json) built for this precise herd
# problem (DGN-835) -- reused below instead of re-implementing a second one.
USAGE_GATE_PY="$SCRIPT_DIR/usage-gate.py"
# Owner-set live resource ceiling (DGN-1444, verbatim: "10%이하면 유저 승인받고
# 발주하기"). This is NOT a --why-overridable tier-binding judgment call -- it
# is a spend ceiling the owner set by hand, so no override flag exists for it.
#
# 2026-09-18 RAISED 90 -> 98 by owner instruction, 원문: "올려서 한두건 돌리자
# 한 2%정도까지". Context: claude weekly was at 97% with its reset four days
# out (09-23) while codex sat at 91% used with a reset two days out (09-20),
# so the owner spent the nearer window on purpose.
#
# The gate's own refusal text says "owner must explicitly approve dispatching
# over the ceiling" -- but no mechanism accepts that approval, so approving it
# means editing this constant. That asymmetry (a rule whose sanctioned path
# does not exist in code) is recorded in DGN-1576; until it is closed, an
# owner ceiling change lands here and nowhere else.
CODEX_QUOTA_MAX_PERCENT=98
# Populated by codex_quota_gate() on every outcome (proceed or fallback) and
# persisted into run.env/status.json so the decision is visible after the
# fact, never silent (DGN-1444: "폴백은 조용하면 안 된다" -- three prior
# swallowed-signal incidents this estate has already paid for: DGN-1442
# observer, DGN-1415, the standing-audit info channel).
CODEX_QUOTA_NOTE=""
# DGN-1434: the weekly usedPercent codex_quota_gate() just read, cached here
# so the dynamic-cap computation (below) reuses ONE live read per dispatch
# instead of spawning a second `codex app-server` handshake for the same
# number. Empty means "not read this run" (e.g. RUNTIME never was codex).
CODEX_USED_PERCENT=""
# DGN-1874: the same read's reset epoch + window length, kept for the
# Claude-vs-Codex headroom policy (lib/runtime_headroom.py) -- no second read.
CODEX_RESETS_AT=""
CODEX_WINDOW_MINS=""
# DGN-1874: headroom policy (decides only; fed by the existing readers).
RUNTIME_HEADROOM_PY="$SCRIPT_DIR/lib/runtime_headroom.py"

# codex_reset_detail <resets_at> [now_epoch] -- render the one owner-facing
# reset fact shared by the refusal and the runtime-selection record. Empty
# stdout means the probe omitted or malformed the optional timestamp; callers
# retain their established quota wording and never let display parsing affect
# the gate decision.
codex_reset_detail() {
  python3 - "$1" "${2:-}" <<'PY' 2>/dev/null
import sys
import time

try:
    reset_at = int(sys.argv[1])
    now = int(sys.argv[2]) if sys.argv[2] else int(time.time())
    if reset_at <= 0:
        raise ValueError("invalid reset epoch")
    remaining = max(0, reset_at - now)
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes = remaining // 60
    duration = []
    if days:
        duration.append(f"{days}d")
    if hours or days:
        duration.append(f"{hours}h")
    duration.append(f"{minutes}m")
    print("weekly reset: %s (in %s)" % (
        time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(reset_at)),
        " ".join(duration),
    ))
except Exception:
    pass
PY
}
# DGN-1444/DGN-1592: the 2026-09-14 read-only-only interim policy is retired.
# Write eligibility is measured per target by codex-hook-verify.py. Managed
# hooks plus a reachable instance adapter can pass in estate worktrees;
# targets without the adapter fail closed. Observe uses the outer seatbelt.
CODEX_HOOK_VERIFY_SCRIPT="$SCRIPT_DIR/codex-hook-verify.py"
CODEX_HOOK_TIMEOUT="${DOGANY_CODEX_HOOK_TIMEOUT:-20}"
# A foreign --worktree-repo is governed by the instance that dispatched it,
# not by files the target product repo happens to carry.  The managed launcher
# receives this root in the worker environment and delegates to this battery.
# Empty preserves the ordinary cwd-derived adapter lookup.
CODEX_ADAPTER_ROOT=""
# Loud operator override (--unguarded-codex "<reason>"): spawns the codex
# worker WITHOUT verified hooks, on the operator's explicit responsibility.
# Never silent: the reason is recorded verbatim in run.env/status.json AND
# the ledger running-row note carries hooks=UNGUARDED so the owner surface
# shows the run was unguarded while it is still running.
CODEX_UNGUARDED_WHY=""
# Outcome of the hook gate for THIS run ("" on claude-code runs) -- persisted
# like CODEX_QUOTA_NOTE so the decision stays visible after the fact.
CODEX_HOOK_NOTE=""

# model_display_binding <runtime> <model> <column>
# stdout: requested tab-separated column. rc 1 means a missing row/field.
model_display_binding() {
  local runtime="$1" model="$2" column="$3"
  [[ -r "$MODEL_DISPLAY_BINDINGS" ]] || return 1
  awk -F'\t' -v rt="$runtime" -v model="$model" -v col="$column" '
    /^[[:space:]]*#/ || NF < col { next }
    $1 == rt && $2 == model { print $col; found=1; exit }
    END { exit(found ? 0 : 1) }
  ' "$MODEL_DISPLAY_BINDINGS"
}

# codex_slug_for_model reads the single shared model-display DATA source.
# rc 1 + empty stdout on an unknown alias (should be unreachable: MODEL is
# already validated against the same four names in validate_spawn_args()).
codex_slug_for_model() {
  model_display_binding codex "$1" 3
}

# owner_model_display [observed] prints the owner-facing model label for the
# effective runtime: "<Name> <version>", no vendor prefix (DGN-1227, owner
# decision dec-177). ONE parser owns the rule -- lib/model_display.py, shared
# with status-footer.py -- so this is only a call. `observed` is a model id or
# the status.json model_observed JSON list: the claude-code version is decided
# by the CLI at run time, so it is derived from the id the run reported.
# Before any id exists (START notice, spawn banner) the label is the bare name
# ("Claude Opus"); a version is never guessed. Presentation only, FAIL-OPEN (DGN-1227
# follow-up): lib missing, import error, non-zero exit or empty output ->
# the raw alias, verbatim (no casing here: that rule belongs to the lib). A
# display failure must never cost a notice, a drop or a ledger close.
owner_model_display() {
  local out=""
  if [[ -r "$SCRIPT_DIR/lib/model_display.py" ]]; then
    out="$(python3 "$SCRIPT_DIR/lib/model_display.py" --tsv "$MODEL_DISPLAY_BINDINGS" \
             "$RUNTIME" "$MODEL" "${1:-}" 2>/dev/null)" || out=""
  fi
  [[ -n "$out" ]] || out="${MODEL:-}"
  printf '%s' "$out"
}

# The <Model> slot of the owner-facing dispatch blocks (identity Response
# workflow, "Completion signal"): the label from owner_model_display, fed the
# model id the run reported (MODEL_OBSERVED_JSON) so the END notice shows the
# observed version ("Claude Opus 5.5"); casing belongs to lib/model_display.py, not
# to a second pass here. DGN-1691 phase 2: a session background job has no
# model, so this rail adds NO model slot: the caller (lib/bg_job_notice.py
# record_start) hands --display-name as the description alone (DGN-1820: the
# slot is machine plumbing, never owner text; same label as the workbench
# row), so the line renders "- <display-name> <HH:MM>". --model-slot is still
# required and carried through, but never rendered here.
owner_model_display_cap() {
  if [[ -n "$MODEL_SLOT" ]]; then
    return 0
  fi
  owner_model_display "${MODEL_OBSERVED_JSON:-}"
}

# The sibling term (DASHBOARD_SIBLING_TERM, DGN-541) no longer titles any
# notice here: the start and end titles both follow background_term below
# (DGN-1767 / DGN-1769), the same noun as the workbench section.

# background_term -- title noun for EVERY start and END notice, dispatch or
# shell job (owner 2026-09-27 for a shell job's END: a job the agent ran itself
# is not a sibling's work; 2026-09-28 DGN-1767 every start; 2026-09-28 07:08
# DGN-1769 every END -- "완료도 백그라운드로?" "ㅇㅇ 맞춰야지"). Same
# source as the workbench section name (DASHBOARD_LIVE_LABEL, DGN-1691)
# minus its trailing " 작업 중"; neutral default 백그라운드.
background_term() {
  local label="${DOGANY_LIVE_LABEL:-}" lib="$SCRIPT_DIR/lib/agentlib.sh"
  if [[ -z "$label" && -f "$lib" ]]; then
    label="$( ( set +e; AGENT_ROOT="$WORKSPACE"; . "$lib" >/dev/null 2>&1
                printf '%s' "${DASHBOARD_LIVE_LABEL:-}" ) 2>/dev/null )" || label=""
  fi
  label="${label% 작업 중}"
  printf '%s' "${label:-백그라운드}"
}

# codex_quota_gate -- reads the live weekly quota and either clears the
# codex path (rc 0), refuses over the owner ceiling (rc 2, no bypass), or
# reports the read failed (rc 3). DGN-1470: it no longer mutates RUNTIME --
# the switch to claude-code is a recorded decision made by run_gates()
# (codex_to_claude_switch), and everything after run_gates() (usage-gate,
# fable-exhausted precheck, do_worker's invocation branch, render_out_log,
# model_observed) reads RUNTIME fresh, so a recorded switch transparently
# becomes an ordinary claude-code dispatch for the rest of this run.
#
# WHY fail-open here but fail-CLOSED for claude's own usage-gate.sh (see
# run_gates() (a) below): claude's gate protects against killing the ONLY
# account this estate has -- exceeding it is catastrophic, so any read
# failure there must refuse. Codex's gate protects a SPEND CEILING on ONE of
# two available runtimes -- a read failure here still has a safe answer
# (claude-code exists and works), so refusing would be strictly worse than
# falling back. Same words ("read failed"), opposite correct answer, because
# the axis is different: account survival vs. runtime choice. Do not
# "unify for consistency" and make this fail-closed too -- that silently
# reproduces the exact asymmetry this comment exists to prevent.
codex_quota_gate() {
  local py_out="" rc=0 used="" resets_at="" window_mins="" reset_detail="" kind="" raw="" elapsed=""
  py_out="$(python3 "$CODEX_RATELIMIT_SCRIPT" --json --timeout "$CODEX_QUOTA_TIMEOUT" 2>/dev/null)" || rc=$?
  if [[ $rc -eq 0 ]]; then
    IFS=$'\t' read -r used resets_at window_mins <<< "$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    used = d["used_percent"]
    if d.get("ok") is not True or type(used) is not int or not 0 <= used <= 100:
        raise ValueError("invalid quota")
    print("%s\t%s\t%s" % (used, d.get("resets_at") or "", d.get("window_duration_mins") or ""))
except Exception:
    print("")' 2>/dev/null)"
    [[ "$used" =~ ^[0-9]+$ ]] || rc=9   # malformed success payload -- treat as a read failure below
  fi
  if [[ $rc -eq 0 ]]; then
    CODEX_USED_PERCENT="$used"   # DGN-1434: reused by the dynamic-cap computation, no second live read
    CODEX_RESETS_AT="$resets_at"; CODEX_WINDOW_MINS="$window_mins"   # DGN-1874 headroom policy
    if [[ "$used" -gt "$CODEX_QUOTA_MAX_PERCENT" ]]; then
      reset_detail="$(codex_reset_detail "$resets_at")"
      CODEX_QUOTA_NOTE="codex weekly quota ${used}% over owner ceiling ${CODEX_QUOTA_MAX_PERCENT}%${reset_detail:+; $reset_detail}"
      {
        echo "[dispatch] REFUSED: codex weekly quota at ${used}% (owner ceiling: ${CODEX_QUOTA_MAX_PERCENT}%)."
        echo "[dispatch]   this is a live resource ceiling the owner set by hand, not a tier-binding judgment -- no --why bypass exists for it (DGN-1444)."
        echo "[dispatch]   owner must explicitly approve dispatching over the ceiling, or wait for the weekly window to reset.${reset_detail:+ $reset_detail}"
      } >&2
      return 2
    fi
    CODEX_QUOTA_NOTE="codex weekly quota ${used}% (<=${CODEX_QUOTA_MAX_PERCENT}, proceeding on codex)"
    echo "[dispatch]   codex quota gate: $CODEX_QUOTA_NOTE"
    return 0
  fi
  # Read failed -- classify, fall back LOUDLY, and accumulate the case
  # (DGN-1444 owner: "언제어떻게 실패하는지 패턴 알아야 대응하지").
  kind="$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("kind","unknown"))
except Exception:
    print("unknown")' 2>/dev/null)"
  [[ -n "$kind" ]] || kind="unknown"
  raw="$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("raw",""))
except Exception:
    print("")' 2>/dev/null)"
  elapsed="$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("elapsed_ms",0))
except Exception:
    print(0)' 2>/dev/null)"
  [[ "$elapsed" =~ ^[0-9]+$ ]] || elapsed=0

  # DGN-1470: this gate CLASSIFIES only -- it no longer mutates RUNTIME. The
  # claude-code switch a read failure used to perform as a side effect is now
  # a recorded decision in run_gates() (codex_to_claude_switch), which also
  # re-checks the tier binding for the switched runtime. rc=3 distinguishes
  # "could not read" from rc=2 "read fine, over the owner ceiling": the
  # fail-open-vs-fail-closed asymmetry note above still governs what the
  # CALLER does with each (both evaluate the claude lane; neither dead-ends
  # here anymore).
  CODEX_QUOTA_NOTE="codex quota read FAILED (kind=$kind)"
  echo "[dispatch] $CODEX_QUOTA_NOTE" >&2
  echo "[dispatch]   raw: $raw" >&2

  if ! python3 "$CODEX_RATELIMIT_SCRIPT" --record-failure \
        --kind "$kind" --raw "$raw" --elapsed-ms "$elapsed" \
        --intent "label=$LABEL model=$MODEL axis=$BOTTLENECK" \
        --fallback "claude-code" >/dev/null 2>&1; then
    echo "[dispatch]   warning: could not append codex-ratelimit failure record (non-fatal)" >&2
  fi
  return 3
}

# ---------------------------------------------------------------------------
# codex_hook_gate <dir> <stage> -- DGN-1444 hook-trust forcing point (see the
# CODEX HOOK-TRUST GATE block above for the incident this closes).
#
# Asks codex's own app-server (hooks/list) whether the project hook layer at
# <dir> would actually load, enabled and trusted, for a worker running there.
# Two call sites, one policy:
#   stage=repo      run_gates(), against WT_REPO -- the synchronous gate
#                   stage every other refusal uses. Cheap early refusal
#                   before any slot/rundir/worktree exists. It reads the
#                   repo the worktree will be cut from, which is also the
#                   path codex resolves a LINKED worktree's project layer to
#                   (measured 2026-09-14: hooks/list for a worktree cwd
#                   returns sourcePath/keys anchored at the main checkout).
#   stage=worktree  do_spawn(), against the freshly provisioned WT_PATH --
#                   the run-binding proof against the exact cwd the worker
#                   gets. Exists because the repo-stage answer, while
#                   necessary, is evidence about ANOTHER path; if codex's
#                   worktree resolution ever changes, this second read is
#                   the one that stays honest. Cannot run earlier: the path
#                   does not exist before provision_worktree.
# WHAT NEITHER STAGE CAN KNOW: whether the hook commands themselves refuse
# correctly at runtime (a hook that errors fails OPEN in codex, observed
# 2026-09-13) -- that is E2E-test territory (DGN-1444 required-work 5), not
# spawn-time evidence. Verifier read failure rejects this candidate. The
# caller may evaluate Claude at repo stage; worktree-stage refusal closes
# the run because its runtime and slot are already bound.
# ---------------------------------------------------------------------------
codex_hook_gate() {
  local target="$1" stage="$2" out rc=0
  if [[ -n "$CODEX_UNGUARDED_WHY" ]]; then
    CODEX_HOOK_NOTE="UNGUARDED($CODEX_UNGUARDED_WHY)"
    if [[ "$stage" == "repo" ]]; then
      {
        echo "[dispatch] WARNING: --unguarded-codex set -- spawning a WRITE-CAPABLE codex worker with NO verified hooks."
        echo "[dispatch]   reason (recorded verbatim in run.env/status.json/ledger): $CODEX_UNGUARDED_WHY"
      } >&2
    fi
    return 0
  fi
  local -a verify_args=( --cwd "$target" --timeout "$CODEX_HOOK_TIMEOUT" )
  [[ -n "$CODEX_ADAPTER_ROOT" ]] && verify_args+=( --adapter-root "$CODEX_ADAPTER_ROOT" )
  out="$(python3 "$CODEX_HOOK_VERIFY_SCRIPT" "${verify_args[@]}" 2>&1)" || rc=$?
  if [[ $rc -eq 0 ]]; then
    CODEX_HOOK_NOTE="hooks-verified@$stage"
    echo "[dispatch]   codex hook gate ($stage): verified active at $target"
    return 0
  fi
  CODEX_HOOK_NOTE="hooks-unverified@$stage: $out"
  {
    echo "[dispatch] REFUSED: write-capable codex dispatch with UNVERIFIED hook enforcement (DGN-1444)."
    echo "[dispatch]   checked ($stage stage): codex app-server hooks/list for $target"
    echo "[dispatch]   verdict: $out"
    echo "[dispatch]   why this refuses: codex silently skips non-managed hooks without persisted hook trust -- on 2026-09-14 three codex worktree dispatches committed with every gate inert."
    echo "[dispatch]   escape 1 (sanctioned): dispatch write work on the claude runtime (--runtime claude-code)."
    echo "[dispatch]   escape 2 (loud override): --unguarded-codex \"<reason, >=10 chars>\" -- recorded in the run ledger as hooks=UNGUARDED."
    echo "[dispatch]   fix (owner-gated): persist hook trust for the repo's .codex hooks (hooks.state.\"<key>\".trusted_hash in ~/.codex/config.toml -- see worklog/DGN-1444, Part 1 verdict 2026-09-14)."
  } >&2
  return 2
}

# ---------------------------------------------------------------------------
# record_runtime_decision <status> <reason_code> [detail] [next_epoch] [deadline_epoch]
# DGN-1470 defect 4: the persistent verdict for decisions made BEFORE any run
# dir exists. status vocabulary: selected (candidate cleared, spawn imminent)
# / deferred (re-evaluation reservation persisted) / blocked (reasoned
# closure) / expired (deadline or attempt budget exhausted). One file per
# request under DISPATCH_DIR/decisions/ -- outside every RUNDIR on purpose,
# so refused-worktree cleanup can never erase the verdict. Later calls for
# the same request overwrite with the FINAL verdict. rc 1 on a write failure:
# the automatic-switch callers treat that as "no durable record -> no silent
# fallback" and refuse the switch.
# ---------------------------------------------------------------------------
record_runtime_decision() {
  local status="$1" reason="$2" detail="${3:-}" next_epoch="${4:-}" deadline_epoch="${5:-}"
  mkdir -p "$DECISION_DIR" 2>/dev/null || true
  if [[ -z "$DECISION_ID" ]]; then
    DECISION_ID="rtd-$(date +%Y%m%d-%H%M%S)-$$-$(printf '%04d' $(( RANDOM % 10000 )))"
  fi
  local mode="observe"
  [[ -n "$WT_SLUG" ]] && mode="worktree"
  if ! DR_PATH="$DECISION_DIR/$DECISION_ID.json" \
    DR_ID="$DECISION_ID" DR_STATUS="$status" DR_REASON="$reason" DR_DETAIL="$detail" \
    DR_LABEL="${LABEL:-}" DR_TICKET="${TICKET:-}" DR_MODEL="${MODEL:-}" DR_MODEL_SOURCE="${MODEL_SOURCE:-}" \
    DR_REQUESTED_MODEL="${REQUESTED_MODEL:-}" DR_CODEX_MODEL="${CODEX_MODEL:-}" \
    DR_AXIS="${BOTTLENECK:-}" DR_MODE="$mode" \
    DR_WT_REPO="${WT_REPO:-}" DR_REPO="${REPO_PATH:-}" \
    DR_REQ_RT="$REQUESTED_RUNTIME" DR_SEL_RT="$RUNTIME" \
    DR_SELECTION_SOURCE="$RUNTIME_SELECTION_SOURCE" DR_SELECTION_REASON="$RUNTIME_SELECTION_REASON" \
    DR_SELECTION_DETAIL="$RUNTIME_SELECTION_DETAIL" \
    DR_DIRECTION="$RUNTIME_FALLBACK_DIRECTION" DR_FB_NOTE="$RUNTIME_FALLBACK_NOTE" \
    DR_CLAUDE_RC="${CLAUDE_GATE_RC:-}" DR_CLAUDE_NOTE="${CLAUDE_GATE_NOTE:-}" \
    DR_QUOTA_NOTE="${CODEX_QUOTA_NOTE:-}" DR_HOOK_NOTE="${CODEX_HOOK_NOTE:-}" \
    DR_RUNID="${RUNID:-}" DR_ATTEMPT="$DISPATCH_DEFER_ATTEMPT" \
    DR_NEXT="$next_epoch" DR_DEADLINE="$deadline_epoch" \
    DR_ORIGIN="${DOGANY_DISPATCH_DEFER_ORIGIN:-}" \
    python3 - <<'PY'
import json, os, time

def _epoch(name):
    v = os.environ.get(name, "")
    if not v.isdigit():
        return None
    e = int(v)
    return {"epoch": e, "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(e))}

path = os.environ["DR_PATH"]
doc = {
    "decision_id": os.environ.get("DR_ID", ""),
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "status": os.environ.get("DR_STATUS", ""),
    "reason_code": os.environ.get("DR_REASON", ""),
    "detail": os.environ.get("DR_DETAIL", ""),
    "label": os.environ.get("DR_LABEL", ""),
    "ticket": os.environ.get("DR_TICKET", ""),
    "model": os.environ.get("DR_MODEL", ""),
    "model_source": os.environ.get("DR_MODEL_SOURCE", ""),
    "requested_model": os.environ.get("DR_REQUESTED_MODEL", ""),
    "codex_model": os.environ.get("DR_CODEX_MODEL", ""),
    "bottleneck": os.environ.get("DR_AXIS", ""),
    "mode": os.environ.get("DR_MODE", ""),
    "wt_repo": os.environ.get("DR_WT_REPO", ""),
    "repo_path": os.environ.get("DR_REPO", ""),
    "requested_runtime": os.environ.get("DR_REQ_RT", ""),
    "selected_runtime": os.environ.get("DR_SEL_RT", ""),
    "selection_source": os.environ.get("DR_SELECTION_SOURCE", ""),
    "selection_reason": os.environ.get("DR_SELECTION_REASON", ""),
    "selection_detail": os.environ.get("DR_SELECTION_DETAIL", ""),
    # selected != executed: this record proves a SELECTION at decision time;
    # execution truth stays with run.env/status.json, linked via runid below
    # (empty when the refusal happened before any run dir was minted).
    "fallback_direction": os.environ.get("DR_DIRECTION", ""),
    "fallback_note": os.environ.get("DR_FB_NOTE", ""),
    "claude_gate_rc": os.environ.get("DR_CLAUDE_RC", ""),
    "claude_gate_note": os.environ.get("DR_CLAUDE_NOTE", ""),
    "codex_quota_note": os.environ.get("DR_QUOTA_NOTE", ""),
    "codex_hook_note": os.environ.get("DR_HOOK_NOTE", ""),
    "runid": os.environ.get("DR_RUNID", ""),
    "defer_attempt": int(os.environ.get("DR_ATTEMPT", "0") or 0),
    "next_recheck": _epoch("DR_NEXT"),
    "deadline": _epoch("DR_DEADLINE"),
    "origin_decision": os.environ.get("DR_ORIGIN", ""),
}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
  then
    echo "[dispatch]   warning: runtime decision record write FAILED ($DECISION_DIR/$DECISION_ID.json)" >&2
    return 1
  fi
  return 0
}

# claude_usage_snapshot -- stdout: {"usage": <get_usage() dict>, "history":
# [<usage_history() samples>]}; rc 1 when the Claude read failed. DGN-1874:
# the SAME usage-gate.py TTL cache every other reader here uses (DGN-1512),
# plus the samples get_usage() records on each live refresh -- no new reader.
claude_usage_snapshot() {
  [[ -r "$USAGE_GATE_PY" ]] || return 1
  python3 -B - "$USAGE_GATE_PY" <<'PY' 2>/dev/null
import importlib.util, json, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    usage = mod.get_usage()
    if usage is None:
        raise RuntimeError("get_usage() returned None (read failed)")
    hist = mod.usage_history() if hasattr(mod, "usage_history") else []
    print(json.dumps({"usage": usage, "history": hist}))
except Exception:
    sys.exit(1)
PY
}

# runtime_headroom_verdict -- "runtime<TAB>verdict<TAB>detail" from the
# DGN-1874 policy over BOTH lanes' reads. Needs codex_quota_gate() to have
# passed first (CODEX_USED_PERCENT set). A policy crash falls back to the
# pre-DGN-1874 order (codex, gate already passed) and says so.
runtime_headroom_verdict() {
  local snap="" out=""
  snap="$(claude_usage_snapshot)" || snap=""
  out="$(printf '%s' "$snap" | python3 -B "$RUNTIME_HEADROOM_PY" \
           --codex-used "$CODEX_USED_PERCENT" --codex-resets-at "$CODEX_RESETS_AT" \
           --codex-window-mins "${CODEX_WINDOW_MINS:-10080}" 2>/dev/null)" || out=""
  [[ "$out" == codex$'\t'* || "$out" == claude-code$'\t'* ]] \
    || out=$'codex\theadroom_policy_unavailable\theadroom policy produced no verdict; codex quota gate passed'
  printf '%s\n' "$out"
}

# Select only the DEFAULT; explicit requests still pass all existing gates.
# DGN-1874 (dec-254): when the bound Codex candidate clears its quota gate,
# the headroom policy (lib/runtime_headroom.py) weighs Claude 5h+7d against
# Codex 7d -- Claude by default while open and on pace; Codex on a Claude 5h
# velocity spill, a Claude-tight/Codex-open split, or Codex use-it-or-lose-it.
# A Codex pick still needs target-hook evidence for write work; read-only
# needs no hook proof because the outer seatbelt is its write boundary.
# Unknown is never permission.
select_default_runtime() {
  REQUESTED_MODEL="$MODEL"
  if [[ "$CLOUD_FIRST_SELECTED" == 1 ]]; then
    # CLOUD-FIRST already chose and converted the run (cloud_first_select).
    RUNTIME="cloud"
  elif [[ "$REQUESTED_RUNTIME" != auto ]]; then
    RUNTIME="$REQUESTED_RUNTIME"
    RUNTIME_SELECTION_REASON="explicit_runtime"
    RUNTIME_SELECTION_DETAIL="source=$RUNTIME_SELECTION_SOURCE runtime=$RUNTIME"
  else
    RUNTIME="claude-code"
    RUNTIME_SELECTION_REASON="codex_tier_unbound"
    RUNTIME_SELECTION_DETAIL="axis=$BOTTLENECK model=$MODEL target=${REPO_PATH:-${WT_REPO:-$WORKSPACE}}"
    local codex_model="" qrc=0
    # DGN-1685: the axis selects the Codex tier, independent of the caller's
    # Claude model. Keep MODEL unchanged until quota and hooks pass so a
    # Claude fallback retains the caller's choice.
    codex_model="$(tier_binding_lookup codex "$BOTTLENECK" 2>/dev/null || true)"
    codex_model="${codex_model%%,*}"
    RUNTIME_SELECTION_DETAIL+="; codex_candidate_model=${codex_model:-none}"
    if fallback_tier_eligible codex "$codex_model"; then
      codex_quota_gate || qrc=$?
      RUNTIME_SELECTION_DETAIL+="; $CODEX_QUOTA_NOTE"
      local hr_rt="" hr_verdict="" hr_detail=""
      if [[ $qrc -eq 0 ]]; then
        IFS=$'\t' read -r hr_rt hr_verdict hr_detail <<< "$(runtime_headroom_verdict)"
        RUNTIME_SELECTION_DETAIL+="; headroom=$hr_verdict: $hr_detail"
      fi
      if [[ $qrc -ne 0 ]]; then
        RUNTIME_SELECTION_REASON="codex_quota_unavailable"
      elif [[ "$hr_rt" == claude-code ]]; then
        RUNTIME_SELECTION_REASON="$hr_verdict"
      elif [[ -n "$WT_SLUG" ]]; then
        if codex_hook_gate "$WT_REPO" selection; then
          RUNTIME="codex"
          MODEL="$codex_model"; MODEL_SOURCE="binding"; CODEX_MODEL="$codex_model"
          RUNTIME_SELECTION_REASON="write_hooks_and_quota_verified"
        else
          RUNTIME_SELECTION_REASON="codex_hooks_unavailable"
        fi
        RUNTIME_SELECTION_DETAIL+="; $CODEX_HOOK_NOTE"
      else
        RUNTIME="codex"
        MODEL="$codex_model"; MODEL_SOURCE="binding"; CODEX_MODEL="$codex_model"
        RUNTIME_SELECTION_REASON="read_quota_verified"
      fi
    fi
    [[ -n "$CLOUD_FIRST_NOTE" ]] && RUNTIME_SELECTION_DETAIL+="; cloud_first=$CLOUD_FIRST_NOTE"
  fi
  [[ -n "$LOCAL_WHY" ]] && RUNTIME_SELECTION_DETAIL+="; local_why=$LOCAL_WHY"
  echo "[dispatch] runtime selection: $RUNTIME ($RUNTIME_SELECTION_REASON; $RUNTIME_SELECTION_DETAIL)"
  record_runtime_decision selected "$RUNTIME_SELECTION_REASON" "$RUNTIME_SELECTION_DETAIL"
}

# fallback_tier_eligible <runtime> -- an AUTOMATIC switch needs a measured
# (runtime, axis) binding row that includes the current model. Stricter than
# bottleneck_gate on purpose: the unmeasured pass-through and the --why
# override are dispatcher judgments about the REQUESTED runtime; neither is
# reused to justify a runtime the dispatcher never chose. (The full
# final-tier recheck after demotion stays DGN-1470 residual defect 3 -- out
# of this scope.)
fallback_tier_eligible() {
  local rt="$1" candidate_model="${2:-$MODEL}" models="" m
  models="$(tier_binding_lookup "$rt" "$BOTTLENECK")" || models=""
  for m in ${models//,/ }; do
    [[ "$m" == "$candidate_model" ]] && return 0
  done
  echo "[dispatch]   fallback tier check FAILED: (runtime=$rt, axis=$BOTTLENECK) measured=[$models] model=$candidate_model -- automatic fallback requires a measured binding row including the model (no --why reuse)" >&2
  return 1
}

# claude_to_codex_fallback <reason> -- evaluate codex as the alternate lane
# after claude was refused (usage-gate rc=1 tight; never lookup-failed --
# rc=1 is the case the first DGN-1470 proposal missed and the 2026-09-15
# 11:35 incident hit) or claude's slot cap was reached. At most one switch
# per dispatch; a persisted decision record is a PRECONDITION of the switch.
claude_to_codex_fallback() {
  local reason="$1"
  FALLBACK_BLOCK_REASON=""
  if [[ -n "$RUNTIME_FALLBACK_DIRECTION" ]]; then
    FALLBACK_BLOCK_REASON="fallback_already_attempted"
    echo "[dispatch] fallback already attempted ($RUNTIME_FALLBACK_DIRECTION) -- never a second switch" >&2
    return 2
  fi
  RUNTIME_FALLBACK_DIRECTION="claude-code->codex"
  RUNTIME_FALLBACK_NOTE="$reason -> evaluating codex fallback"
  echo "[dispatch] $RUNTIME_FALLBACK_NOTE" >&2
  if ! fallback_tier_eligible codex; then
    FALLBACK_BLOCK_REASON="fallback_tier_unbound"
    return 2
  fi
  # Work kind x target path (see the DGN-1470 block above): write-capable
  # needs verified hooks at the repo the worktree is cut from; observe's
  # contract is the outer seatbelt, so no hook demand there.
  if [[ -n "$WT_SLUG" ]]; then
    if ! codex_hook_gate "$WT_REPO" repo; then
      FALLBACK_BLOCK_REASON="codex_hooks_unavailable"
      return 2
    fi
  fi
  local qrc=0
  codex_quota_gate || qrc=$?
  if [[ $qrc -ne 0 ]]; then
    FALLBACK_BLOCK_REASON="codex_quota_unavailable"
    return 2
  fi
  RUNTIME="codex"
  RUNTIME_FALLBACK_NOTE="$reason -> falling back to codex"
  if ! record_runtime_decision selected claude_to_codex_fallback "$RUNTIME_FALLBACK_NOTE"; then
    echo "[dispatch] REFUSED: fallback decision record could not be persisted -- an unrecorded automatic switch is forbidden (DGN-1470)" >&2
    RUNTIME="claude-code"
    FALLBACK_BLOCK_REASON="decision_record_failed"
    return 2
  fi
  echo "[dispatch]   runtime fallback: $RUNTIME_FALLBACK_NOTE (decision $DECISION_ID)"
  return 0
}

# codex_to_claude_switch <reason> -- evaluate claude-code as the alternate
# lane after codex was refused (quota over ceiling, quota read failure, hook
# trust unverified, slot cap). rc 0 switched, rc 3 claude's own gate refused
# (recoverable -> caller decides defer), rc 2 blocked for a non-gate reason.
codex_to_claude_switch() {
  local reason="$1"
  FALLBACK_BLOCK_REASON=""
  if [[ -n "$RUNTIME_FALLBACK_DIRECTION" ]]; then
    FALLBACK_BLOCK_REASON="fallback_already_attempted"
    echo "[dispatch] fallback already attempted ($RUNTIME_FALLBACK_DIRECTION) -- never a second switch" >&2
    return 2
  fi
  RUNTIME_FALLBACK_DIRECTION="codex->claude-code"
  RUNTIME_FALLBACK_NOTE="$reason -> evaluating claude-code fallback"
  echo "[dispatch] $RUNTIME_FALLBACK_NOTE" >&2
  local claude_model="${REQUESTED_MODEL:-$MODEL}"
  if ! fallback_tier_eligible claude-code "$claude_model"; then
    FALLBACK_BLOCK_REASON="fallback_tier_unbound"
    return 2
  fi
  local gate_out gate_rc=0
  gate_out="$("$USAGE_GATE" 2>&1)" || gate_rc=$?
  CLAUDE_GATE_RC="$gate_rc"
  CLAUDE_GATE_NOTE="$(printf '%s\n' "$gate_out" | tail -n 1)"
  if [[ $gate_rc -ne 0 ]]; then
    echo "[dispatch] refused by usage-gate (rc=$gate_rc):" >&2
    echo "$gate_out" >&2
    FALLBACK_BLOCK_REASON="claude_gate_refused"
    return 3
  fi
  RUNTIME="claude-code"
  local previous_model="$MODEL" previous_source="$MODEL_SOURCE"
  MODEL="$claude_model"
  [[ -z "$REQUESTED_MODEL" ]] || MODEL_SOURCE="explicit"
  # Same demotion contract as the primary claude lane (pre-existing; the
  # post-demotion tier recheck remains DGN-1470 residual defect 3).
  if [[ "$MODEL" == "fable" ]] && fable_exhausted; then
    echo "[dispatch] fable weekly cap exhausted (.claude/.fable-exhausted) -> demoting to opus for this dispatch"
    MODEL="opus"
  fi
  RUNTIME_FALLBACK_NOTE="$reason -> falling back to claude-code"
  if ! record_runtime_decision selected codex_to_claude_fallback "$RUNTIME_FALLBACK_NOTE"; then
    echo "[dispatch] REFUSED: fallback decision record could not be persisted -- an unrecorded automatic switch is forbidden (DGN-1470)" >&2
    RUNTIME="codex"
    MODEL="$previous_model"; MODEL_SOURCE="$previous_source"
    FALLBACK_BLOCK_REASON="decision_record_failed"
    return 2
  fi
  echo "[dispatch]   runtime fallback: $RUNTIME_FALLBACK_NOTE (decision $DECISION_ID)"
  return 0
}

# claude_reset_epoch <gate_out> -- epoch seconds when the claude window that
# tripped the gate resets (trigger=five_hour|seven_day parsed from the gate's
# own output; default five_hour). rc 1 on any read/parse failure -- the
# caller falls back to a fixed retry interval, never a guessed boundary.
#
# DGN-1512: reads usage-gate.py's TTL cache (the same one
# claude_quota_remaining() uses) instead of a second live claude-usage.sh
# call -- the cache carries five_hour_resets_at / seven_day_resets_at (see
# fetch_usage() in usage-gate.py), so every reader in this file shares one
# live read per TTL window.
claude_reset_epoch() {
  local window="five_hour" key iso=""
  [[ "$1" == *"trigger=seven_day"* ]] && window="seven_day"
  key="${window}_resets_at"
  [[ -r "$USAGE_GATE_PY" ]] || return 1
  iso="$(python3 -B - "$USAGE_GATE_PY" "$key" <<'PY' 2>/dev/null
import importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    usage = mod.get_usage()
    if usage is None:
        raise RuntimeError("get_usage() returned None (read failed)")
    v = usage.get(sys.argv[2], "")
    if not v:
        raise RuntimeError("missing resets_at")
    print(v)
except Exception:
    sys.exit(1)
PY
)" || return 1
  python3 -c '
import sys
from datetime import datetime
try:
    dt = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    print(int(dt.timestamp()))
except Exception:
    sys.exit(1)
' "$iso" 2>/dev/null
}

# defer_or_block <claude_rc> <claude_gate_out> -- both lanes are blocked.
# Either register a BOUNDED, reset-aligned re-evaluation (deferred: reservation
# persisted via usage-defer-retry.sh, deadline never extended, attempts
# capped) or close with the reasons on record (blocked/expired). Never an
# unowned "waiting" state, never an owner-authored sleep script. The caller
# always returns 2 after this -- THIS dispatch attempt did not spawn.
defer_or_block() {
  local claude_rc="$1" claude_out="$2"
  local now deadline reset_epoch="" both self snap
  now="$(date +%s)"
  if [[ -n "$DISPATCH_DEFER_DEADLINE" ]]; then
    deadline="$DISPATCH_DEFER_DEADLINE"
  else
    deadline=$(( now + DISPATCH_DEFER_DEADLINE_MIN * 60 ))
  fi
  both="claude: ${CLAUDE_GATE_NOTE:-usage-gate rc=$claude_rc}; codex: ${FALLBACK_BLOCK_REASON:-not_evaluated}${CODEX_QUOTA_NOTE:+ ($CODEX_QUOTA_NOTE)}"
  if [[ "$DISPATCH_DEFER_ATTEMPT" -ge "$DISPATCH_DEFER_MAX" ]]; then
    echo "[dispatch] BLOCKED (expired): re-evaluation budget exhausted (attempt $DISPATCH_DEFER_ATTEMPT/$DISPATCH_DEFER_MAX) -- $both" >&2
    record_runtime_decision expired defer_budget_exhausted "$both" "" "$deadline" || true
    return 0
  fi
  if [[ "$now" -ge "$deadline" ]]; then
    echo "[dispatch] BLOCKED (expired): defer deadline reached -- $both" >&2
    record_runtime_decision expired defer_deadline_reached "$both" "" "$deadline" || true
    return 0
  fi
  case "$claude_rc" in
    1) reset_epoch="$(claude_reset_epoch "$claude_out")" || reset_epoch="" ;;
    2) reset_epoch=$(( now + DISPATCH_LOOKUP_RETRY_SEC )) ;;   # lookup failure (429/network): transient, retry in ~1 min
    *)
      echo "[dispatch] BLOCKED: both lanes blocked and the claude refusal (rc=$claude_rc) is not a quota/lookup shape -- no time boundary recovers it, closing" >&2
      record_runtime_decision blocked both_lanes_blocked "$both" "" "$deadline" || true
      return 0 ;;
  esac
  [[ "$reset_epoch" =~ ^[0-9]+$ ]] || reset_epoch=$(( now + DISPATCH_DEFER_RETRY_MIN * 60 ))
  if [[ "$reset_epoch" -ge "$deadline" ]]; then
    echo "[dispatch] BLOCKED: earliest recovery boundary ($(date -r "$reset_epoch" '+%m-%d %H:%M' 2>/dev/null || echo "epoch $reset_epoch")) is past the defer deadline -- closing instead of waiting past the contract" >&2
    record_runtime_decision blocked recovery_after_deadline "$both" "$reset_epoch" "$deadline" || true
    return 0
  fi
  mkdir -p "$DECISION_DIR" 2>/dev/null || true
  if [[ -z "$DECISION_ID" ]]; then
    DECISION_ID="rtd-$(date +%Y%m%d-%H%M%S)-$$-$(printf '%04d' $(( RANDOM % 10000 )))"
  fi
  # Snapshot the prompt: the caller's task file may be a temp path that is
  # gone by the time the one-shot fires (same reason do_spawn snapshots).
  snap="$DECISION_DIR/$DECISION_ID.prompt.md"
  if ! cp "$TASK_FILE" "$snap" 2>/dev/null; then
    echo "[dispatch] BLOCKED: could not snapshot the prompt for the re-evaluation -- closing" >&2
    record_runtime_decision blocked defer_snapshot_failed "$both" "" "$deadline" || true
    return 0
  fi
  self="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
  # Replay = the SAME request re-entering the full gate path with the defer
  # budget threaded through env. Deliberately absent: --unguarded-codex (a
  # manual override is never re-applied without the operator at the wheel)
  # and DOGANY_DISPATCH_MAX_ACTIVE (the replay re-evaluates live state).
  local replay=( /usr/bin/env
    "DOGANY_DISPATCH_RUNTIME=$REQUESTED_RUNTIME"
    "DOGANY_DISPATCH_DEFER_ATTEMPT=$(( DISPATCH_DEFER_ATTEMPT + 1 ))"
    "DOGANY_DISPATCH_DEFER_DEADLINE=$deadline"
    "DOGANY_DISPATCH_DEFER_ORIGIN=${DOGANY_DISPATCH_DEFER_ORIGIN:-$DECISION_ID}"
    "$self" --task "$snap" --model "${REQUESTED_MODEL:-$MODEL}" --bottleneck "$BOTTLENECK" --bottleneck-why "$BOTTLENECK_WHY"
    --label "$LABEL" --timeout "$TIMEOUT_MIN" --notify "$NOTIFY" )
  [[ -n "$DISPLAY_NAME" ]] && replay+=( --display-name "$DISPLAY_NAME" )
  [[ -n "$TICKET" ]]       && replay+=( --ticket "$TICKET" )
  [[ -n "$TIER_WHY" ]]     && replay+=( --why "$TIER_WHY" )
  [[ "$READ_ONLY" == 1 ]] && replay+=( --read-only )
  [[ "$WRITE_REQUESTED" == 1 ]] && replay+=( --write )
  [[ -n "$WT_SLUG" ]]      && replay+=( --worktree "$WT_SLUG" )
  [[ -n "$WT_REPO_ARG" ]]  && replay+=( --worktree-repo "$WT_REPO_ARG" )
  [[ -n "$WT_BASE_ARG" ]]  && replay+=( --base "$WT_BASE_ARG" )
  [[ -n "$REPO_PATH" ]]    && replay+=( --repo "$REPO_PATH" )
  if "$USAGE_DEFER_RETRY" --label "dispatch-$DECISION_ID" \
       --reset-epoch "$reset_epoch" -- "${replay[@]}" >&2; then
    local next_at=$(( reset_epoch + 120 ))   # scheduler's own post-reset pad
    echo "[dispatch] DEFERRED: both lanes blocked; re-evaluation reserved at $(date -r "$next_at" '+%m-%d %H:%M' 2>/dev/null || echo "epoch $next_at") (attempt $(( DISPATCH_DEFER_ATTEMPT + 1 ))/$DISPATCH_DEFER_MAX, deadline $(date -r "$deadline" '+%m-%d %H:%M' 2>/dev/null || echo "epoch $deadline")) -- $both" >&2
    record_runtime_decision deferred both_lanes_blocked "$both" "$next_at" "$deadline" || true
  else
    echo "[dispatch] BLOCKED: both lanes blocked and the re-evaluation reservation could not be persisted -- closing with reasons on record (a deferral without a persisted reservation is not a deferral)" >&2
    record_runtime_decision blocked defer_reservation_failed "$both" "" "$deadline" || true
  fi
  return 0
}

# runtime_slot_fallback -- the primary runtime passed its gates but its slot
# cap is full: evaluate the alternate lane once (full gates, then ITS slot).
# rc 0 = switched AND slot acquired. Design bound: a slot failure of an
# already-switched candidate never re-cycles (one switch max).
runtime_slot_fallback() {
  local from="$RUNTIME" rc=0
  if [[ "$from" == "claude-code" ]]; then
    claude_to_codex_fallback "claude-code slot cap reached ($(read_active_count claude-code)/$MAX_ACTIVE)" || return 1
  elif [[ "$from" == "codex" ]]; then
    codex_to_claude_switch "codex slot cap reached ($(read_active_count codex)/$MAX_ACTIVE)" || return 1
  else
    return 1
  fi
  if ! active_count_incr; then
    echo "[dispatch]   alternate runtime $RUNTIME slot cap also reached ($(read_active_count)/$MAX_ACTIVE) -- no lane has capacity" >&2
    return 1
  fi
  return 0
}

# 2026-08-26 13:0x: 3 -> 5 (형님 "태워") -> 13:2x: 1 (7일 창 97% critical)
#   -> 13:3x: 3 복귀 (형님 계정 전환, 새 창 7d 1% 실측).
# 교훈(주석으로 남긴다): 상한을 올리기 전에 잔량을 먼저 실측한다. 13:0x 인상 시점에
#   확인 안 했고, 40분 만에 7일 창이 97%까지 갔다. usage-gate 는 임계에서 거부할 뿐
#   임계까지 가는 속도는 이 상수가 정한다 -- "게이트가 있으니 괜찮다"는 틀린 추론.
#   기본값 3 = 설계값. 5 이상은 잔량 실측 + 형님 승인 동반일 때만.
#
# DGN-1262: 그 조건("실측 + 승인")이 충족돼도 올릴 방법이 상수 편집뿐이었다.
#   프레임워크 파일이라 라이브 인스턴스는 하드패치가 금지되고, 그래서 승인이
#   있어도 다음 업데이트까지 못 올렸다 -- 규율이 아니라 배선이 막고 있었다.
#   env override 를 연다: 기본값은 설계값 3 그대로이고, 상향은 호출마다 명시돼
#   호출 이력에 남는다(상수 편집은 아무 흔적도 안 남긴다).
#   올리기 전 잔량 실측 의무는 그대로다 -- 이 노브가 그 의무를 면제하지 않는다.
#
# DGN-1434: the "3" above stood in for a measurement that was never taken --
# it refused a 4th dispatch today (2026-09-14) with claude at 3% used / codex
# at 18% used, both nearly-empty windows, and it stayed exactly 3 the day the
# 7d window burned to 97% in 40 minutes. Same constant, opposite correct
# answer, because it never looked at the thing it exists to protect. Below,
# MAX_ACTIVE becomes a computed value (compute_dynamic_cap(), called from
# active_count_incr() at the moment of the actual spawn attempt, per RUNTIME)
# instead of a constant fixed at parse time. DOGANY_DISPATCH_MAX_ACTIVE still
# overrides it outright -- validated here, early, so a typo'd override still
# fails fast -- but an override is now ANNOUNCED (DYNAMIC_CAP_NOTE) instead of
# looking like an ordinary computed decision, which is how the 08-26 raise
# stayed invisible for 40 minutes.
if [[ -n "${DOGANY_DISPATCH_MAX_ACTIVE:-}" ]]; then
  case "$DOGANY_DISPATCH_MAX_ACTIVE" in
    *[!0-9]*|0) echo "[dispatch] DOGANY_DISPATCH_MAX_ACTIVE must be a positive integer (got '$DOGANY_DISPATCH_MAX_ACTIVE')" >&2; exit 2 ;;
  esac
fi
# Placeholder only -- overwritten by active_count_incr() -> compute_dynamic_cap()
# before it is ever consulted (refusal message, --list/--calibration display).
MAX_ACTIVE=3
# Populated by compute_dynamic_cap() on every call: WHY this number and not
# another (mission requirement -- a refusal must carry the numbers behind it).
DYNAMIC_CAP_NOTE=""
DYNAMIC_CAP_VALUE=3

# cap_from_remaining_claude <remaining%> -- CONSERVATIVE mapping. claude-code
# has TWO windows to protect (5h + 7d) and exhausting the 5h one kills the
# live session mid-work (measured twice, 2026-09-13). Steps chosen so the
# unchanged mid-band (20-39% remaining -> 3) matches today's fixed default,
# and the top band (>=70% remaining) is comfortably above 3 without being
# reckless -- this is a judgment call pending more measurement, not a
# derived constant; revisit with real burn-rate data (DGN-1434 note (c)).
cap_from_remaining_claude() {
  local r="$1"
  if   (( r >= 70 )); then echo 5
  elif (( r >= 40 )); then echo 4
  elif (( r >= 20 )); then echo 3
  elif (( r >= 10 )); then echo 2
  elif (( r >= 3  )); then echo 1
  else                     echo 0
  fi
}

# cap_from_remaining_codex <remaining%> -- PERMISSIVE mapping, deliberately
# NOT the same table as claude (owner, 2026-09-14 07:19): codex has ONE
# window (primary/weekly; secondary is null -- verified against a live
# account/rateLimits/read response, DGN-1434), and resets arrive out-of-band
# roughly weekly per owner report ([추정]: cadence unmeasured). The same
# remaining% therefore buys a HIGHER cap than claude at every step. Do not
# unify this with cap_from_remaining_claude "for consistency" -- that would
# silently make codex as timid as claude, which the owner explicitly did not
# ask for. codex_quota_gate() above already refuses outright past its configured used-percent
# ceiling before this table is ever consulted at the low end -- these rows are
# defense in depth, not the primary guard.
cap_from_remaining_codex() {
  local r="$1"
  if   (( r >= 40 )); then echo 8
  elif (( r >= 20 )); then echo 6
  elif (( r >= 10 )); then echo 4
  elif (( r >= 5  )); then echo 2
  else                     echo 1
  fi
}

# claude_quota_remaining -- stdout on success: "<5h remaining%> <7d remaining%>"
# (two integers, space-separated). rc 1 on ANY read failure (missing script,
# bad JSON, non-numeric fields) -- the caller must fall back to the fixed cap,
# never treat a failure as "unlimited".
#
# DGN-1512: reuses usage-gate.py's get_usage() (300s TTL file cache) instead
# of shelling out to claude-usage.sh --json directly (see USAGE_GATE_PY
# above). Every caller within the same CACHE_TTL_SEC window -- in particular
# every spawn inside one fan-out burst -- now shares ONE live read instead of
# making its own; a cache miss still fetches live exactly like before.
claude_quota_remaining() {
  [[ -r "$USAGE_GATE_PY" ]] || return 1
  python3 -B - "$USAGE_GATE_PY" <<'PY' 2>/dev/null
import importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    usage = mod.get_usage()
    if usage is None:
        raise RuntimeError("get_usage() returned None (read failed)")
    fh = float(usage.get("five_hour_util", 0))
    sd = float(usage.get("seven_day_util", 0))
    if not (0 <= fh <= 100 and 0 <= sd <= 100):
        raise ValueError("invalid utilization")
    print(int(100 - fh), int(100 - sd))
except Exception:
    sys.exit(1)
PY
}

# codex_quota_remaining -- stdout on success: "<weekly remaining%>" (integer).
# rc 1 on read failure. Reuses CODEX_USED_PERCENT if codex_quota_gate() already
# read it this run (the common path -- RUNTIME=="codex" always runs that gate
# first in run_gates()); falls back to its own live read only when called
# standalone (e.g. a future direct caller, or tests).
codex_quota_remaining() {
  if [[ -n "$CODEX_USED_PERCENT" ]]; then
    echo $(( 100 - CODEX_USED_PERCENT ))
    return 0
  fi
  local py_out="" rc=0 used=""
  py_out="$(python3 "$CODEX_RATELIMIT_SCRIPT" --json --timeout "$CODEX_QUOTA_TIMEOUT" 2>/dev/null)" || rc=$?
  [[ $rc -eq 0 ]] || return 1
  used="$(printf '%s' "$py_out" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("used_percent",""))
except Exception:
    print("")' 2>/dev/null)"
  [[ "$used" =~ ^[0-9]+$ ]] || return 1
  echo $(( 100 - used ))
}

# Reuse the usage gate's scope/session policy, including conf/env precedence.
# Only an explicit WAIVE result permits ignoring the weekly cap. Missing or
# broken policy code retains the conservative cap, as usage-gate.sh does.
claude_weekly_cap_waived() {
  local policy=""
  policy="$(python3 -B - "$USAGE_GATE_PY" "$WORKSPACE" <<'PY' 2>/dev/null
import importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    waived = mod.gate_7d_auto_only(sys.argv[2]) and mod.session_is_interactive()
    print("WAIVE" if waived else "APPLY")
except Exception:
    print("APPLY")
PY
)" || return 1
  [[ "$policy" == "WAIVE" ]]
}

# compute_dynamic_cap <runtime> -- sets TWO globals, DYNAMIC_CAP_VALUE (the
# effective MAX_ACTIVE, right now) and DYNAMIC_CAP_NOTE (the reasoning -- an
# ordinary spawn and a refusal both need to show their work). MUST be called
# directly, NEVER via `$(compute_dynamic_cap ...)`: a command-substitution
# subshell would silently discard both globals the instant it exits, and the
# caller would see an empty note with no error -- exactly the kind of silent
# failure this ticket exists to remove. Read failure -> fixed 3, loudly, for
# EITHER runtime: unknown is not permission (same axis-dependent reasoning as
# codex_quota_gate()'s own read-failure path, applied here because a bad cap
# is a shared-resource risk no matter which account's reader broke).
compute_dynamic_cap() {
  local runtime="$1"
  DYNAMIC_CAP_VALUE=3
  DYNAMIC_CAP_NOTE=""

  # DGN-1726: checked BEFORE the generic override -- that knob sizes the
  # local accounts; the cloud lane has its own and reads no local quota.
  if [[ "$runtime" == "cloud" ]]; then
    DYNAMIC_CAP_VALUE="$CLOUD_MAX_ACTIVE"
    DYNAMIC_CAP_NOTE="cloud runtime fixed cap=$CLOUD_MAX_ACTIVE (separate lane; DOGANY_DISPATCH_MAX_ACTIVE_CLOUD, no local quota read)"
    return 0
  fi

  if [[ -n "${DOGANY_DISPATCH_MAX_ACTIVE:-}" ]]; then
    DYNAMIC_CAP_NOTE="OVERRIDE: DOGANY_DISPATCH_MAX_ACTIVE=$DOGANY_DISPATCH_MAX_ACTIVE (manual, ignores live quota -- announced here every time so a raised cap can never again go unnoticed for 40 minutes like 2026-08-26)"
    DYNAMIC_CAP_VALUE="$DOGANY_DISPATCH_MAX_ACTIVE"
    return 0
  fi

  if [[ "$runtime" == "codex" ]]; then
    local rem
    if rem="$(codex_quota_remaining)"; then
      DYNAMIC_CAP_VALUE="$(cap_from_remaining_codex "$rem")"
      DYNAMIC_CAP_NOTE="codex weekly remaining=${rem}% -> cap=$DYNAMIC_CAP_VALUE (permissive mapping: single window, frequent out-of-band resets)"
      return 0
    fi
    DYNAMIC_CAP_NOTE="codex quota read FAILED -> falling back to fixed cap 3 (unknown is not permission)"
    DYNAMIC_CAP_VALUE=3
    return 0
  fi

  local out fh_rem sd_rem cap_5h cap_7d
  if out="$(claude_quota_remaining)"; then
    fh_rem="${out%% *}"; sd_rem="${out##* }"
    cap_5h="$(cap_from_remaining_claude "$fh_rem")"
    if claude_weekly_cap_waived; then
      DYNAMIC_CAP_VALUE="$cap_5h"
      DYNAMIC_CAP_NOTE="claude 5h remaining=${fh_rem}% (cap $cap_5h) -> effective cap=$DYNAMIC_CAP_VALUE; USAGE_GATE_7D=auto-only, owner-driven live session"
      return 0
    fi
    cap_7d="$(cap_from_remaining_claude "$sd_rem")"
    DYNAMIC_CAP_VALUE=$(( cap_5h < cap_7d ? cap_5h : cap_7d ))
    DYNAMIC_CAP_NOTE="claude 5h remaining=${fh_rem}% (cap $cap_5h), 7d remaining=${sd_rem}% (cap $cap_7d) -> effective cap=$DYNAMIC_CAP_VALUE (conservative mapping: the MORE constrained window governs, so a tight 5h throttles even when the 7d window is healthy)"
    return 0
  fi
  DYNAMIC_CAP_NOTE="claude usage read FAILED -> falling back to fixed cap 3 (unknown is not permission)"
  DYNAMIC_CAP_VALUE=3
  return 0
}

PROMPT_MAX_BYTES=102400   # 100KB -- ARG_MAX guard (spec R8)
DEFAULT_TIMEOUT_MIN=60

# ---------------------------------------------------------------------------
# Argument parsing / mode routing
# ---------------------------------------------------------------------------
usage() {
  sed -n '13,85p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

MODE="spawn"
TASK_FILE=""
MODEL=""
REQUESTED_MODEL=""
CODEX_MODEL=""
# DGN-1666: how MODEL got its value -- "explicit" (--model) or "binding"
# (auto-resolved from the measured axis row). Recorded so the ledger can tell
# a hand pick from a table pick when the next calibration pass reads it.
MODEL_SOURCE="explicit"
# DGN-1666: optional reasoning effort (low|medium|high|max). Empty = no
# --effort flag (current behaviour); also the floor for pre-field run.env.
EFFORT=""
# DGN-1770: how EFFORT got its value -- explicit | table | no-effect |
# unmeasured (resolve_effort()). Empty = run.env written before the field.
EFFORT_SOURCE=""
# DGN-1770: one-line spawn-banner warning for a measured-bad combination.
EFFORT_WARNING=""
LABEL=""
TICKET=""
# DGN-1218 gate inputs. Empty defaults double as the backward-compat floor
# when finalize/cancel/list source a run.env written before this gate
# existed (no BOTTLENECK/BOTTLENECK_WHY/TIER_WHY lines; set -u would trip otherwise).
BOTTLENECK=""
BOTTLENECK_WHY=""
TIER_WHY=""
# DGN-1512: the machine-filled usage snapshot recorded alongside a TIER_WHY
# override (bottleneck_gate(), below) -- NOT part of the human sentence, never
# mutates TIER_WHY itself (that stays verbatim, see its own contract). Empty
# default is also the backward-compat floor for run.env written before this
# field existed.
TIER_WHY_USAGE=""
# Owner-facing name (see DISPLAY NAME in the header). The empty default is
# also the backward-compat floor: finalize/cancel/list source a run.env
# written before this field existed (no DISPLAY_NAME line) and set -u would
# trip otherwise. An empty value simply means "render the machine label",
# which is byte-for-byte the pre-field behaviour.
DISPLAY_NAME=""
WT_SLUG=""
# Access declarations are independent of cwd. Old run.env files stay unknown.
READ_ONLY=0
WRITE_REQUESTED=0
# CLOUD-FIRST: --repo-contained (header). Meaningful with --worktree /
# --repo only.
REPO_CONTAINED=0
# DGN-1742 (header, LOCAL-WHY FORCING POINTS): the stated reason a repo run
# stays local. Empty default is also the floor for older run.env files.
LOCAL_WHY=""
ACCESS_MODE=""
REPO_PATH=""
WT_REPO_ARG=""
# Git repo the worktree is cut from. Default = workspace (legacy behaviour).
# Also the fallback when do_cancel sources a run.env written by an older
# version of this script (no WT_REPO line) -- set -u would trip otherwise.
WT_REPO="$WORKSPACE"
# Base branch of the dsp/<slug> worktree (branch point + freshness reference).
# Resolved in provision_worktree (see BASE branch contract in the header).
# Default "main" doubles as the fallback when finalize/cancel source a run.env
# written by an older version of this script (no WT_BASE line; set -u).
WT_BASE="main"
WT_BASE_ARG=""
# Observe-mode sandbox artifacts (empty in worktree mode and in run.env files
# written before DGN-1191 -- set -u would trip otherwise).
SANDBOX_PROFILE=""
SESSION_ID=""
# DGN-1504 write-mode artifacts (empty in observe mode / older run.env).
WRITE_PROFILE=""
# DGN-1458 plan lane (empty = no plan lane / older run.env).
LIVE_TARGETS_ARG=()
LIVE_TARGETS=""
PLAN_DIR=""
PLAN_STATE=""
CHECKOUT_SPAWN_NOTE=""
WRITE_ROSTER_NOTE=""
CHECKOUT_DRIFT=""
TIMEOUT_MIN="$DEFAULT_TIMEOUT_MIN"
TIMEOUT_GIVEN=0
# Default push, not silent (header, END NOTICE): the owner-designed completion
# block reaches the owner from this shell. dec-126 (owner, 2026-08-24) set the
# silent default when the completion push was a bare event line; the designed
# END block postdates it, and a completion signal the model has to relay by
# hand goes missing on exactly the turn it matters (the same rule->forcing-point
# ruling that machine-emitted the START notice). --notify silent remains the
# explicit opt-out for runs whose completion the caller will fold into its own
# report.
NOTIFY="push"
# DGN-1688: set only when the owning bridge session deliberately invokes
# --cancel.  Empty preserves the established behavior for timeouts, worker
# deaths, and cancellations issued outside that session.
CANCEL_BY=""
CANCEL_RUNID=""
WORKER_RUNDIR=""
BACKFILL_APPLY=0
# DGN-1691 phase 2 (header, SESSION BACKGROUND JOBS): inputs of the
# --start-notice-enqueue / --end-notice modes. Empty MODEL_SLOT keeps every
# dispatch path on its model display column.
MODEL_SLOT=""
BG_RESULT=""
BG_ELAPSED_SECS=""
BG_END_CLOCK=""
BG_NOT_BEFORE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task)     TASK_FILE="$2"; shift 2 ;;
    --runtime)  RUNTIME="$2"; REQUESTED_RUNTIME="$2"; RUNTIME_SELECTION_SOURCE="flag"; shift 2 ;;
    --model)    MODEL="$2"; shift 2 ;;
    --effort)   EFFORT="$2"; shift 2 ;;
    --bottleneck) BOTTLENECK="$2"; shift 2 ;;
    --bottleneck-why) BOTTLENECK_WHY="$2"; shift 2 ;;
    --why)      TIER_WHY="$2"; shift 2 ;;
    --unguarded-codex) CODEX_UNGUARDED_WHY="$2"; shift 2 ;;
    --label)    LABEL="$2"; shift 2 ;;
    --display-name) DISPLAY_NAME="$2"; shift 2 ;;
    --ticket)   TICKET="$2"; shift 2 ;;
    --read-only) READ_ONLY=1; shift ;;
    --write)    WRITE_REQUESTED=1; shift ;;
    --repo-contained) REPO_CONTAINED=1; shift ;;
    --local-why) LOCAL_WHY="$2"; shift 2 ;;
    --worktree) WT_SLUG="$2"; shift 2 ;;
    --repo)     REPO_PATH="$2"; shift 2 ;;
    --worktree-repo) WT_REPO_ARG="$2"; shift 2 ;;
    --base)     WT_BASE_ARG="$2"; shift 2 ;;
    --live-target) LIVE_TARGETS_ARG+=("$2"); shift 2 ;;
    --timeout)  TIMEOUT_MIN="$2"; TIMEOUT_GIVEN=1; shift 2 ;;
    --notify)   NOTIFY="$2"; shift 2 ;;
    --cancel)   MODE="cancel"; CANCEL_RUNID="$2"; shift 2 ;;
    --apply-plan) MODE="apply-plan"; CANCEL_RUNID="$2"; shift 2 ;;
    --list)     MODE="list"; shift 1 ;;
    --calibration) MODE="calibration"; shift 1 ;;
    --worker-run) MODE="worker"; WORKER_RUNDIR="$2"; shift 2 ;;
    # Internal, like --worker-run: the detached sender of the batched start
    # notice (see START NOTICE in the header). Never called by hand, so it is
    # deliberately absent from usage() -- it takes no arguments and spawning it
    # twice for one burst would double the owner's message.
    --start-notice-flush) MODE="start-notice-flush"; shift 1 ;;
    # Internal (DGN-1691 phase 2): session background-job notices on the
    # dispatch rails. Called by lib/bg_job_notice.py only; absent from usage().
    --start-notice-enqueue) MODE="bg-start-notice"; shift 1 ;;
    --end-notice) MODE="bg-end-notice"; shift 1 ;;
    --model-slot) MODEL_SLOT="$2"; shift 2 ;;
    --result)   BG_RESULT="$2"; shift 2 ;;
    --elapsed-secs) BG_ELAPSED_SECS="$2"; shift 2 ;;
    --end-clock) BG_END_CLOCK="$2"; shift 2 ;;
    --not-before) BG_NOT_BEFORE="$2"; shift 2 ;;
    --backfill-model-observed) MODE="backfill"; shift 1 ;;
    --apply)    BACKFILL_APPLY=1; shift 1 ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "[dispatch] unknown arg: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# shared-file lock + active-count helpers. The active count is a SEPARATE
# ledger from the loop budget (see header).
# mkdir-based lock: atomic on POSIX, self-expiring via stale-age check.
# ---------------------------------------------------------------------------
# dir_lock <lockdir> -- the lock itself. Parameterized rather than bound to
# one path because a SECOND shared file needs the same discipline (the
# start-notice queue, below): one implementation means a fix to the stale-steal
# logic lands for every caller instead of one of them.
dir_lock() {
  local lock="$1" tries=0 age now mtime
  while ! mkdir "$lock" 2>/dev/null; do
    tries=$(( tries + 1 ))
    if [[ $tries -ge 50 ]]; then
      # Stale lock (holder crashed): steal if older than 30s, else fail.
      now="$(date +%s)"
      mtime="$(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || echo "$now")"
      age=$(( now - mtime ))
      if [[ $age -gt 30 ]]; then
        rmdir "$lock" 2>/dev/null || true
        continue
      fi
      echo "[dispatch] lock timeout: $lock" >&2
      return 1
    fi
    sleep 0.1
  done
  return 0
}

dir_unlock() {
  rmdir "$1" 2>/dev/null || true
}

count_lock() {
  # First-run bootstrap: the lock mkdir fails forever if the parent dir is
  # absent (found by sandbox E2E -- spawn refused with a bogus 0/3 message).
  mkdir -p "$DISPATCH_DIR" 2>/dev/null || true
  dir_lock "$COUNT_LOCK_DIR"
}

count_unlock() {
  dir_unlock "$COUNT_LOCK_DIR"
}

# active_count_file <runtime> -- DGN-1434: claude-code keeps the original path
# (backward compat: tests read it directly); codex counts independently.
active_count_file() {
  if [[ "$1" == "codex" ]]; then
    echo "$ACTIVE_COUNT_FILE_CODEX"
  elif [[ "$1" == "cloud" ]]; then
    echo "$ACTIVE_COUNT_FILE_CLOUD"
  else
    echo "$ACTIVE_COUNT_FILE"
  fi
}

read_active_count() {
  local runtime="${1:-$RUNTIME}" f v="0"
  f="$(active_count_file "$runtime")"
  if [[ -f "$f" ]]; then
    v="$(cat "$f" 2>/dev/null || echo 0)"
    v="${v//[^0-9]/}"
  fi
  echo "${v:-0}"
}

# incr returns 1 (refuse) when the cap is already reached. The cap itself is
# computed HERE, per runtime, at the moment of the actual spawn attempt
# (DGN-1434) -- not a value fixed once at script-parse time. MAX_ACTIVE is
# left set as a side effect purely so the refusal message / --list /
# --calibration can display the number that was just computed.
active_count_incr() {
  local runtime="${1:-$RUNTIME}"
  count_lock || return 1
  local v cap f
  v="$(read_active_count "$runtime")"
  compute_dynamic_cap "$runtime"   # direct call, NOT $(...) -- see its header comment
  cap="$DYNAMIC_CAP_VALUE"
  MAX_ACTIVE="$cap"
  if [[ $v -ge $cap ]]; then
    count_unlock
    return 1
  fi
  f="$(active_count_file "$runtime")"
  printf '%s\n' "$(( v + 1 ))" > "$f"
  count_unlock
  return 0
}

active_count_decr() {
  local runtime="${1:-$RUNTIME}"
  count_lock || return 0   # best-effort on the way down; never block teardown
  local v f
  f="$(active_count_file "$runtime")"
  v="$(read_active_count "$runtime")"
  if [[ $v -gt 0 ]]; then
    printf '%s\n' "$(( v - 1 ))" > "$f"
  else
    printf '0\n' > "$f"
  fi
  count_unlock
}

# ---------------------------------------------------------------------------
# Gate (b): .claude/.fable-exhausted pre-check -> opus demotion (spec R1).
# Flag semantics mirror usage-gate.py: JSON {"expiry": iso8601}; past-expiry
# flag is treated as recovered (we do NOT delete it -- that is usage-gate.py's
# surface; we only read). Unparseable expiry biases to demotion, capped by
# 7d file age (same conservatism as usage-gate.py).
# ---------------------------------------------------------------------------
fable_exhausted() {
  expiry_flag_active "$FABLE_FLAG"
}

# expiry_flag_active <path> -- rc 0 while an {"expiry": iso8601} flag file is
# live (shared by .fable-exhausted and the CLOUD-FIRST .cloud-exhausted).
expiry_flag_active() {
  [[ -f "$1" ]] || return 1
  python3 - "$1" <<'PY'
import json, os, sys, time
from datetime import datetime, timezone
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        flag = json.load(f)
    expiry = flag.get("expiry", "") if isinstance(flag, dict) else ""
    exp = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    sys.exit(0 if exp > datetime.now(timezone.utc) else 1)
except Exception:
    # Corrupt flag: demote (safe) unless the file has aged past one 7d window.
    try:
        sys.exit(0 if (time.time() - os.path.getmtime(path)) <= 7 * 86400 else 1)
    except Exception:
        sys.exit(0)
PY
}

# ---------------------------------------------------------------------------
# DGN-1158: fable_exhausted() above only READS this flag; nothing in the
# estate ever wrote it, so the fable->opus demotion gate was a permanent
# no-op (observed: a fable worker died on "Fable 5 requires usage credits..."
# and the flag did not exist anywhere -- worklog/DGN-1158). This is the
# writer, called from the worker's own exit path (do_worker, below) on that
# exact death signature. It only consumes the failure as a SIGNAL for the
# next spawn's demotion -- it never retries the dispatch that just died; that
# decision stays with whoever reads the finalize/inbox report.
#
# DGN-1350: this pattern must stay in LOCKSTEP with RE_DEATH in the sibling
# writer routines/fable-death-catch.py. It did not, and the divergence was
# silent: the harness reworded the cap message to "You've reached your Fable 5
# limit. Switch to another model, or manage usage credits at ..." -- the
# python writer's `reached your .*limit` alternative still matched, this one
# ('requires usage credits') did not. Two detached fable dispatches died on
# that wording (2026-09-08 22:03 / 22:17), no marker was written either time,
# and the demotion gate stayed a no-op exactly as before DGN-1158 "fixed" it.
# Keep the alternation identical to the python side; the paired test asserts
# both writers accept the same corpus.
# ---------------------------------------------------------------------------
RE_FABLE_CREDITS_DEATH='[Rr]equires usage credits|[Hh]it your (usage )?limit|usage limit reached|weekly limit reached|[Rr]eached your .*limit'

# rc 0 = this run died on the fable weekly-cap signature, 1 = anything else.
# claude-code-only concept: gpt-6-astra (codex's "fable" tier binding) has its
# own quota, entirely unrelated to claude's fable weekly cap -- a codex run
# with --model fable must never write $FABLE_FLAG on death, or it would
# wrongly demote a FUTURE claude-code fable dispatch based on a codex failure.
is_fable_credits_death() {
  [[ "$RUNTIME" == "claude-code" ]] || return 1
  [[ "$MODEL" == "fable" ]] || return 1
  [[ -f "$1" ]] || return 1
  grep -qE "$RE_FABLE_CREDITS_DEATH" "$1"
}

# Same flag schema and same expiry source/fallback as the sibling PostToolUse
# writer (routines/fable-death-catch.py reset_expiry), so either writer
# leaves an interchangeable marker for the one reader (fable_exhausted() /
# usage-gate.py): the weekly cap resets on the 7d window boundary recorded in
# .claude/.usage-cache.json; missing/unparseable -> now+3d so the flag still
# self-clears and fable gets re-probed even without the cache.
write_fable_exhausted_marker() {
  mkdir -p "$(dirname "$FABLE_FLAG")" 2>/dev/null || return 1
  python3 - "$FABLE_FLAG" "$WORKSPACE" <<'PY'
import json, os, sys
from datetime import datetime, timedelta, timezone

flag_path, workspace = sys.argv[1], sys.argv[2]
cache = os.path.join(workspace, ".claude", ".usage-cache.json")
expiry = None
try:
    with open(cache, encoding="utf-8") as f:
        resets = json.load(f).get("seven_day_resets_at")
    if resets:
        datetime.fromisoformat(str(resets).replace("Z", "+00:00"))
        expiry = str(resets)
except Exception:
    expiry = None
if not expiry:
    expiry = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

tmp = flag_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({
        "expiry": expiry,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": "dispatch-detached-worker",
    }, f)
os.replace(tmp, flag_path)
PY
}

# ---------------------------------------------------------------------------
# First-run ledger bootstrap. ledger-update.sh refuses to write when the
# ledger file is absent (loud, by design) -- without this seed the very first
# dispatch on a freshly minted instance dies with "Ledger not found" (the
# caller-present/callee-absent defect class, DGN-1061). Seeds header + table
# head ONLY: an empty table keeps the one-write-path doctrine intact (rows
# are written exclusively by lib/ledger-update.sh), and the table head is
# required -- ledger-update.sh appends after the last pipe row and would
# silently DROP the row into a headless file. noclobber = first writer wins;
# a concurrent loser falls through to the existing file.
# ---------------------------------------------------------------------------
ensure_ledger() {
  if [[ -f "$LEDGER_FILE" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$LEDGER_FILE")"
  ( set -o noclobber
    printf '%s\n' \
      "# auto-loop-ledger.md" \
      "# Current-state ledger -- one row per background work item (branch)." \
      "# Single write path: routines/lib/ledger-update.sh. Never edit rows by hand." \
      "# Seeded by dispatch-detached.sh on first use (DGN-991 backland)." \
      "" \
      "| branch | item | state | attempts | backoff_until | last_ts | flags | note |" \
      "|---|---|---|---|---|---|---|---|" > "$LEDGER_FILE"
  ) 2>/dev/null || true
  if [[ ! -f "$LEDGER_FILE" ]]; then
    echo "[dispatch] ledger bootstrap failed: cannot create $LEDGER_FILE" >&2
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Spawn-side gates, all synchronous, all BEFORE detach (spec step 1).
# ---------------------------------------------------------------------------
run_gates() {
  # (0) codex weekly-quota gate (DGN-1444). Runs FIRST so (a)/(b) below --
  # claude-only concepts (account usage window, fable weekly-cap flag) --
  # apply to whichever runtime is ACTUALLY about to execute. DGN-1470: any
  # non-zero outcome (rc=2 over ceiling, rc=3 read failed) evaluates the
  # claude lane ONCE via codex_to_claude_switch instead of dead-ending (the
  # old code switched only on a read failure, and did it as an unrecorded
  # side effect).
  if [[ "$RUNTIME" == "codex" ]]; then
    local qrc=0 swrc=0
    codex_quota_gate || qrc=$?
    if [[ $qrc -ne 0 ]]; then
      codex_to_claude_switch "$CODEX_QUOTA_NOTE" || swrc=$?
      if [[ $swrc -eq 3 ]]; then
        defer_or_block "$CLAUDE_GATE_RC" "$CLAUDE_GATE_NOTE"
        return 2
      elif [[ $swrc -ne 0 ]]; then
        record_runtime_decision blocked "${FALLBACK_BLOCK_REASON:-codex_quota_refused}" "$CODEX_QUOTA_NOTE" || true
        return 2
      fi
    fi
  fi

  # (0b) codex hook-trust gate (DGN-1444 forcing point), repo stage. AFTER
  # the quota gate on purpose: that gate may have switched the run to
  # claude-code, and a claude worker's hooks are the claude-side ones --
  # this gate only governs a run that will ACTUALLY execute on codex.
  # 2026-09-14: no worktree means no hook gate ONLY because do_spawn
  # self-checks the outer seatbelt, which makes target writes impossible.
  # DGN-1470: an unverified hook layer now also evaluates the claude lane
  # once -- the refusal was correct, the dead end was not.
  if [[ "$RUNTIME" == "codex" && -n "$WT_SLUG" ]]; then
    if ! codex_hook_gate "$WT_REPO" repo; then
      local hkrc=0
      codex_to_claude_switch "codex hook trust unverified at $WT_REPO (repo stage)" || hkrc=$?
      if [[ $hkrc -eq 3 ]]; then
        defer_or_block "$CLAUDE_GATE_RC" "$CLAUDE_GATE_NOTE"
        return 2
      elif [[ $hkrc -ne 0 ]]; then
        record_runtime_decision blocked "${FALLBACK_BLOCK_REASON:-codex_hooks_unavailable}" "repo stage: $WT_REPO" || true
        return 2
      fi
    fi
  fi

  # Guarded against the codex->claude-code switch above: that path already
  # ran the usage gate and the demotion check inside codex_to_claude_switch,
  # and re-running the gate here would double-consume an owner approval
  # sentinel (usage-gate.sh one-shot count semantics).
  if [[ "$RUNTIME" == "claude-code" && "$RUNTIME_FALLBACK_DIRECTION" != "codex->claude-code" ]]; then
    # (a) usage gate -- fail-CLOSED: any non-zero exit refuses THIS lane.
    # DGN-1590: only rc=1 (measured quota tight) evaluates the codex lane.
    # rc=2 (lookup failed) is unknown, never a switch reason. rc=1 is the
    # 2026-09-15 11:35 shape (5h window 95%). Other exit codes are input/
    # environment errors a runtime switch cannot fix (design: never solve an
    # input error by changing runtimes).
    local gate_out gate_rc=0
    gate_out="$("$USAGE_GATE" 2>&1)" || gate_rc=$?
    CLAUDE_GATE_RC="$gate_rc"
    CLAUDE_GATE_NOTE="$(printf '%s\n' "$gate_out" | tail -n 1)"
    if [[ $gate_rc -ne 0 ]]; then
      echo "[dispatch] refused by usage-gate (rc=$gate_rc):" >&2
      echo "$gate_out" >&2
      local fbrc=2
      if [[ $gate_rc -eq 1 ]]; then
        fbrc=0
        claude_to_codex_fallback "claude usage-gate refused (rc=$gate_rc): $CLAUDE_GATE_NOTE" || fbrc=$?
      fi
      if [[ $fbrc -ne 0 ]]; then
        defer_or_block "$gate_rc" "$gate_out"
        return 2
      fi
    fi

    # (b) fable-exhausted pre-check -> opus demotion, explicitly logged.
    # Skipped when the fallback above moved the run off claude-code: the
    # flag is a claude-account concept.
    if [[ "$RUNTIME" == "claude-code" && "$MODEL" == "fable" ]] && fable_exhausted; then
      echo "[dispatch] fable weekly cap exhausted (.claude/.fable-exhausted) -> demoting to opus for this dispatch"
      MODEL="opus"
    fi
  fi

  # (c) concurrent dispatch cap (separate ledger from loop budget), counted
  # PER RUNTIME (DGN-1434: claude-code and codex are different accounts with
  # different quotas -- a codex dispatch must never consume a claude-code
  # slot). The cap is DYNAMIC (compute_dynamic_cap(), inside active_count_incr)
  # -- a bare "cap reached" gives the caller nothing to act on, so a refusal
  # here always carries the runtime, the live remaining quota that produced
  # the cap, and the current active count. DGN-1470: a full PRIMARY lane
  # evaluates the alternate once (capacity is a resource like quota); a full
  # ALTERNATE lane closes -- one switch max, no cycling.
  if ! active_count_incr; then
    {
      echo "[dispatch] refused: active dispatch cap reached for runtime=$RUNTIME ($(read_active_count)/$MAX_ACTIVE)."
      echo "[dispatch]   cap basis: $DYNAMIC_CAP_NOTE"
    } >&2
    if [[ -z "$RUNTIME_FALLBACK_DIRECTION" ]] && runtime_slot_fallback; then
      return 0
    fi
    echo "[dispatch] Cancel or wait, then retry." >&2
    record_runtime_decision blocked slot_cap_reached "runtime=$RUNTIME $DYNAMIC_CAP_NOTE${FALLBACK_BLOCK_REASON:+; alternate: $FALLBACK_BLOCK_REASON}" || true
    return 2
  fi

  # DGN-1726: the cloud lane skipped (a)/(b) above by construction (they run
  # for claude-code only) -- that skip is a decision, so it goes on record.
  if [[ "$RUNTIME" == "cloud" ]]; then
    record_runtime_decision selected cloud_usage_gate_skipped \
      "runtime=cloud: local usage gate (5h-window deny) and fable demotion skipped -- cloud credit is spent first; kept: ledger, prompt-size cap, $DYNAMIC_CAP_NOTE" || true
  fi

  # A replayed (previously deferred) request that cleared every gate closes
  # its defer chain on record; execution truth follows via run.env/status.json.
  if [[ -n "${DOGANY_DISPATCH_DEFER_ORIGIN:-}" && -z "$RUNTIME_FALLBACK_DIRECTION" ]]; then
    record_runtime_decision selected defer_replay_cleared "attempt=$DISPATCH_DEFER_ATTEMPT origin=${DOGANY_DISPATCH_DEFER_ORIGIN}" || true
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Prompt size cap -- claude -p receives the prompt as ONE argv entry; past
# ~100KB we risk E2BIG (ARG_MAX). Non-exec refusal with guidance (spec R8).
# ---------------------------------------------------------------------------
check_prompt_size() {
  local bytes
  bytes="$(wc -c < "$TASK_FILE" | tr -d ' ')"
  # DGN-1726: the cloud task is sent WITH its machine preamble + footer --
  # cap the sum.
  if [[ "$RUNTIME" == "cloud" ]]; then
    bytes=$(( bytes + $( { cloud_task_preamble; cloud_task_footer; } | wc -c | tr -d ' ') ))
  fi
  if [[ $bytes -gt $PROMPT_MAX_BYTES ]]; then
    echo "[dispatch] refused: prompt file is ${bytes}B > ${PROMPT_MAX_BYTES}B (ARG_MAX guard)." >&2
    echo "[dispatch] hint: move bulk material into files and reference their paths from a short prompt." >&2
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# DGN-1218 LAYER 2: (runtime, axis) -> measured-model binding, consulted as
# DATA. These functions must never contain a model name: the moment one
# appears here, a provider swap (3.0) has one more thing to rip out --
# guarded by tests/test-dgn1218-dispatch-gate.sh.
# ---------------------------------------------------------------------------

# tier_binding_lookup <runtime> <axis>
# stdout: comma-separated measured-model list for the row.
# rc 0 = row found, 1 = no row (unmeasured runtime/axis), 2 = data file absent.
tier_binding_lookup() {
  local runtime="$1" axis="$2"
  [[ -f "$TIER_BINDINGS" ]] || return 2
  awk -F'\t' -v rt="$runtime" -v ax="$axis" '
    /^[[:space:]]*(#|$)/ { next }
    $1 == rt && $2 == ax { print $3; found = 1; exit }
    END { exit found ? 0 : 1 }
  ' "$TIER_BINDINGS"
}

# effort_binding_lookup <runtime> <axis> <model>  (DGN-1770)
# stdout: the effort cell (low|medium|high, or "-" = measured, no effect).
# rc 0 = row found, 1 = no row (unmeasured), 2 = data file absent.
effort_binding_lookup() {
  local runtime="$1" axis="$2" model="$3"
  [[ -f "$EFFORT_BINDINGS" ]] || return 2
  awk -F'\t' -v rt="$runtime" -v ax="$axis" -v m="$model" '
    /^[[:space:]]*(#|$)/ { next }
    $1 == rt && $2 == ax && $3 == m { print $4; found = 1; exit }
    END { exit found ? 0 : 1 }
  ' "$EFFORT_BINDINGS"
}

# effort_passed_to_runtime <runtime> <effort> -- rc 0 when the worker hands
# this effort to the runtime CLI, 1 when it is recorded only. The single
# owner of that rule: the spawn banner and the codex spawn site ask here (the
# claude spawn site passes every level validate_spawn_args() admits).
#   claude-code: `claude --effort` takes low|medium|high|max.
#   codex: `-c model_reasoning_effort=` -- verified 2026-09-28 on codex-cli
#     0.156.1 (the value lands in the session turn_context; unset runs record
#     null) and used by the DGN-1750 bench, whose reasoning tokens rose with
#     effort. The CLI does NOT validate the value ("bogus" is accepted), so
#     only the three levels that bench exercised are passed; max is not one.
#   cloud (and any unlisted runtime): no verified control.
effort_passed_to_runtime() {
  case "$1:$2" in
    claude-code:low|claude-code:medium|claude-code:high|claude-code:max) return 0 ;;
    codex:low|codex:medium|codex:high) return 0 ;;
  esac
  return 1
}

# resolve_effort -- DGN-1770: the effort twin of bottleneck_gate()'s model
# auto-resolution. Runs once RUNTIME and MODEL are FINAL (after run_gates(),
# which may flip the runtime or fall back to another model), so the table is
# read with the key that actually executes. Never refuses: a missing table or
# row degrades to the CLI default, said out loud. Sets EFFORT, EFFORT_SOURCE,
# EFFORT_WARNING. Model-name-free like the rest of this section (guarded by
# tests/test-dgn1770-effort-binding.sh D4); the one measured-bad combination
# lives in effort_warning() below, outside the lookup.
resolve_effort() {
  local cell="" rc=0
  if [[ -n "$EFFORT" ]]; then
    EFFORT_SOURCE="explicit"
  else
    cell="$(effort_binding_lookup "$RUNTIME" "$BOTTLENECK" "$MODEL")" || rc=$?
    if [[ $rc -eq 0 && "$cell" == "-" ]]; then
      EFFORT_SOURCE="no-effect"
      echo "[dispatch] effort: no flag (runtime=$RUNTIME axis=$BOTTLENECK model=$MODEL measured with no effort effect -> CLI default)"
    elif [[ $rc -eq 0 && "$cell" =~ ^(low|medium|high)$ ]]; then
      EFFORT="$cell"
      EFFORT_SOURCE="table"
      echo "[dispatch] effort auto-resolved from table: runtime=$RUNTIME axis=$BOTTLENECK model=$MODEL -> $EFFORT  (pass --effort to override)"
    else
      EFFORT_SOURCE="unmeasured"
      if [[ $rc -eq 2 ]]; then
        echo "[dispatch] effort: no flag (effort table missing: $EFFORT_BINDINGS -> CLI default)"
      elif [[ $rc -eq 0 ]]; then
        echo "[dispatch] effort: no flag (invalid table cell '$cell' for runtime=$RUNTIME axis=$BOTTLENECK model=$MODEL -> CLI default)"
      else
        echo "[dispatch] effort: no flag (runtime=$RUNTIME axis=$BOTTLENECK model=$MODEL unmeasured -> CLI default)"
      fi
    fi
  fi
  EFFORT_WARNING="$(effort_warning)"
  return 0
}

# effort_warning -- DGN-1770 item 4 (DGN-1666 follow-up). A SEPARATE,
# model-named rule (like fable_exhausted()), kept out of the lookup above.
# stdout: one warning line, or nothing.
# Evidence (effort bench n=5, a2 large-evidence verify): sonnet high completed
# 2/5 (3 timeouts) vs medium 4/5, low 5/5, for no accuracy gain (.953 on the
# two that finished vs low .930 / medium .948). WARN, not refuse: the
# dispatcher cannot see evidence size, and on small-evidence verify (a1/a3)
# sonnet high completed 5/5 -- a refusal would block runs the same
# measurement shows are fine. Claude runtimes only (cloud runs the same
# model); the codex "sonnet" alias is a different model and was not measured
# this way.
effort_warning() {
  if [[ "$BOTTLENECK" == "accuracy" && "$MODEL" == "sonnet" && "$EFFORT" == "high" \
        && ( "$RUNTIME" == "claude-code" || "$RUNTIME" == "cloud" ) ]]; then
    echo "sonnet + high on accuracy: effort bench large-evidence verify completed 2/5 (3 timeouts) vs low 5/5, no accuracy gain -- prefer low, or opus medium for large evidence"
  fi
}

# ---------------------------------------------------------------------------
# DGN-1512: TIER_WHY override audit. NOT part of the Layer 2 (runtime, axis)
# lookup above -- this is a SEPARATE, already-fable-named subsystem (like
# FABLE_FLAG / fable_exhausted() / write_fable_exhausted_marker() elsewhere in
# this file), so it lives in its own functions rather than inside
# bottleneck_gate() itself (which stays model-name-free, per its own header
# and tests/test-dgn1218-dispatch-gate.sh T9A).
#
# THE DEFECT THIS CLOSES: --why is a hand-written sentence, and the gate only
# ever checked that a sentence was PRESENT, never that it was TRUE. Measured
# 2026-09-16: the same sentence ("fable 주간 92% 소진 -- ... opus 로 demote")
# was copied into seven afternoon dispatches while fable_percent had already
# read 0.0 and .claude/.fable-exhausted did not exist -- the judgment axis's
# top-ranked model was passed over seven times on a stale claim, silently.
#
# TIER_WHY_USAGE is a MACHINE-FILLED, ADDITIVE field, never a mutation of
# TIER_WHY: TIER_WHY keeps recording the human's sentence VERBATIM (the
# existing contract this file already states three times and
# tests/test-dgn1218-dispatch-gate.sh T5E/T5G assert byte-for-byte) -- the
# split this ticket asked for is TIER_WHY = "why this axis" (owned by the
# human) and TIER_WHY_USAGE = "what the account looked like right now"
# (owned by the machine, reusing claude_quota_remaining()'s cached read).
RE_FABLE_USAGE_CLAIM='[Ff]able.*(exhaust|limit|quota|[0-9]+%|소진|한도|초과)|(exhaust|limit|quota|[0-9]+%|소진|한도|초과).*[Ff]able'

# tier_why_usage_snapshot -- one-line machine-filled usage record for a
# tier-binding override. Reuses claude_quota_remaining()'s TTL-cached read
# (no extra live call inside a fan-out window) plus the fable-scoped percent
# the same cache file already carries (DGN-1440's fable_percent field) and
# the SAME fable_exhausted() flag check run_gates() (b) uses to actually
# demote a spawn -- one predicate, read here and there, never two answers to
# "is fable exhausted right now". "?" for any field a read failure leaves
# unfilled: a plumbing hiccup degrades this one audit line, it must never
# fail the whole dispatch (the usage gate in run_gates() already owns that
# fail-closed decision).
tier_why_usage_snapshot() {
  local rem fh="?" sd="?" fp="?" flag="absent"
  if rem="$(claude_quota_remaining)"; then
    fh="${rem%% *}"; sd="${rem##* }"
  fi
  if [[ -r "$USAGE_GATE_PY" ]]; then
    fp="$(python3 -B - "$USAGE_GATE_PY" <<'PY' 2>/dev/null
import importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    usage = mod.get_usage()
    v = usage.get("fable_percent") if usage else None
    print(v if v is not None else "?")
except Exception:
    print("?")
PY
)"
    [[ -n "$fp" ]] || fp="?"
  fi
  fable_exhausted && flag="set"
  printf 'five_hour_remaining=%s%% seven_day_remaining=%s%% fable_used=%s%% fable_flag=%s' \
    "$fh" "$sd" "$fp" "$flag"
}

# tier_why_usage_check <models> -- narrow, structural + textual gate. Runs
# ONLY when BOTH hold: (a) fable was the (or a) measured-bound model for this
# axis and a different one was chosen (the exact shape a fable-availability
# override takes -- see dispatch-tier-bindings.tsv: judgment binds fable
# alone), AND (b) the human's own --why names fable together with a
# usage/exhaustion word or a bare percent (RE_FABLE_USAGE_CLAIM) -- i.e. the
# human is ALREADY claiming this is a usage decision, not e.g. a task-fit or
# output-quality one. Outside that narrow shape this is a no-op: an override
# for any other reason is unaffected, exactly as before this ticket.
#
# Within that shape, fable_exhausted() is authoritative (same flag/predicate
# run_gates() (b) uses to actually demote a live fable spawn) -- if it says
# fable is NOT exhausted, the human's claim disagrees with the account's
# current state and the spawn REFUSES rather than proceeding on it.
#
# WHY REFUSE INSTEAD OF SILENTLY OVERWRITING (the ticket's design question):
# TIER_WHY is calibration evidence of what the human believed -- rewriting it
# would erase that evidence while still letting the spawn land on a model the
# binding data says is wrong for this axis. A refusal surfaces the
# contradiction at the one moment a human is still watching (the spawn call
# itself), matches this file's existing fail-loud culture (every other gate
# here -- codex_quota_gate, the active-count cap, the DGN-1444 hook gate --
# refuses with reasons rather than proceeding with a footnote), and forces a
# fresh, honest --why on retry instead of letting the same stale sentence
# ride into an eighth dispatch.
tier_why_usage_check() {
  local models="$1"
  [[ ",$models," == *,fable,* ]] || return 0
  [[ "$MODEL" != "fable" ]] || return 0
  [[ "$TIER_WHY" =~ $RE_FABLE_USAGE_CLAIM ]] || return 0
  if fable_exhausted; then
    return 0
  fi
  {
    echo "[dispatch] REFUSED: --why claims a fable usage/exhaustion reason, but the live check disagrees (DGN-1512)."
    echo "[dispatch]   --why (verbatim): $TIER_WHY"
    echo "[dispatch]   machine snapshot (just read): $TIER_WHY_USAGE"
    echo "[dispatch]   $FABLE_FLAG is absent or expired -- fable is NOT currently flagged exhausted."
    echo "[dispatch] fable is the bound model for axis=$BOTTLENECK: either drop --model to fable (it is available now), or re-issue --why with the actual (non-usage) reason -- the usage claim is checked here, never just trusted."
  } >&2
  return 2
}

# Refuses (rc 2) when the declared axis and the chosen model disagree with the
# measured binding and no --why was given. The default path is REFUSAL, not a
# warning: warn-and-pass is exactly the 3.4% shape (DGN-1194). The override
# path exists because the binding is measurement over a finite sample, not
# doctrine -- but it must leave a verbatim, auditable record.
bottleneck_gate() {
  local models="" rc=0 m ok=0
  models="$(tier_binding_lookup "$RUNTIME" "$BOTTLENECK")" || rc=$?
  if [[ $rc -eq 2 ]]; then
    echo "[dispatch] REFUSED: tier-binding data file missing: $TIER_BINDINGS (fail-closed -- a lost data file must not silently disarm the gate)" >&2
    return 2
  fi
  if [[ $rc -eq 1 ]]; then
    # Unmeasured runtime (or axis row): never block -- measurement does not
    # exist yet and blocking would freeze new runtimes out (3.0 boundary).
    # The run is still fully recorded, so the future measurement can happen.
    # DGN-1666 x unmeasured: with no row there is nothing to resolve an
    # omitted --model from -- refuse rather than spawn with an empty model.
    if [[ -z "$MODEL" ]]; then
      echo "[dispatch] REFUSED: --model omitted and no tier-binding row for (runtime=$RUNTIME, axis=$BOTTLENECK) to resolve it from -- pass --model explicitly" >&2
      return 2
    fi
    echo "[dispatch] tier-binding: no row for (runtime=$RUNTIME, axis=$BOTTLENECK) -- unmeasured, passing through (run recorded for future measurement)"
    return 0
  fi
  # DGN-1666 (owner 2026-09-23): --model is OPTIONAL. The binding table
  # already decides (runtime, axis) -> models and this gate already refuses
  # anything else, so naming the model by hand re-derives a value the DATA
  # owns -- and gets it wrong (a hand pick refused by this very gate, then a
  # pick the runtime demoted). Empty MODEL resolves to the
  # FIRST measured model for the row. Explicit --model still wins: the owner
  # asks for a specific tier, and the --why override path is untouched.
  if [[ -z "$MODEL" ]]; then
    MODEL="${models%%,*}"
    MODEL_SOURCE="binding"
    echo "[dispatch] model auto-resolved from binding: axis=$BOTTLENECK runtime=$RUNTIME -> $MODEL  (measured=[$models]; pass --model to override)"
    return 0
  fi
  for m in ${models//,/ }; do
    [[ "$m" == "$MODEL" ]] && ok=1
  done
  if [[ $ok -eq 1 ]]; then
    return 0
  fi
  if [[ -n "$TIER_WHY" ]]; then
    TIER_WHY_USAGE="$(tier_why_usage_snapshot)"
    tier_why_usage_check "$models" || return 2
    echo "[dispatch] tier-binding OVERRIDE: axis=$BOTTLENECK measured=[$models] chosen=$MODEL -- accepted with --why (recorded verbatim in run.env/status.json)"
    echo "[dispatch]   usage snapshot (machine-filled, DGN-1512): $TIER_WHY_USAGE"
    return 0
  fi
  {
    echo "[dispatch] REFUSED: axis/model mismatch (DGN-1218 tier-binding gate)."
    echo "[dispatch]   declared bottleneck : $BOTTLENECK"
    echo "[dispatch]   measured binding    : $models  (runtime=$RUNTIME, data: $TIER_BINDINGS)"
    echo "[dispatch]   chosen model        : $MODEL"
    echo "[dispatch] pick a bound model, or state the exception: --why \"<one line, >=10 chars, recorded>\"."
    echo "[dispatch] the binding is measurement (DGN-1193 replay), not preference -- overrides are ledgered and audited."
  } >&2
  return 2
}

# ---------------------------------------------------------------------------
# DGN-1218 calibration reader. The ledger (run.env + status.json) always
# recorded model/wall/result -- what was missing was a READER at the moment
# of decision. Printed on every spawn attempt BEFORE the gates, and on
# demand via --calibration. Deliberately cheap: newest <=20 status.json
# files, nothing else. Shows only decision-relevant columns: what tier ran,
# on which axis, how it ended, how long it took, what it produced.
# ---------------------------------------------------------------------------
print_calibration() {
  if [[ ! -d "$DISPATCH_DIR" ]]; then
    echo "[calibration] no dispatch history yet"
    return 0
  fi
  CAL_DIR="$DISPATCH_DIR" python3 - <<'PY' || echo "[calibration] reader failed (non-fatal)"
import datetime, json, os, sys

d = os.environ["CAL_DIR"]
try:
    names = sorted((n for n in os.listdir(d) if n.startswith("dsp-")), reverse=True)
except OSError:
    sys.exit(0)
rows = []
for n in names:
    if len(rows) >= 20:
        break
    try:
        with open(os.path.join(d, n, "status.json"), encoding="utf-8") as f:
            rows.append(json.load(f))
    except Exception:
        continue   # running or pre-status run: not calibration evidence yet
if not rows:
    print("[calibration] no finalized dispatch history yet")
    sys.exit(0)

def wall(doc):
    try:
        fmt = "%Y-%m-%dT%H:%M:%S%z"
        s = datetime.datetime.strptime(doc.get("started_at", ""), fmt)
        e = datetime.datetime.strptime(doc.get("ended_at", ""), fmt)
        return "%dm" % max(0, round((e - s).total_seconds() / 60))
    except Exception:
        return "?"

def product(doc):
    c = doc.get("commits") or 0
    if c:
        return "commits=%d" % c
    try:
        b = os.path.getsize(doc.get("out_log", ""))
    except OSError:
        b = 0
    return "report=%dKB" % round(b / 1024)

print("[calibration] recent dispatches (newest first):")
for doc in rows[:8]:
    print("  %s  %-6s %-9s %-10s %4s  %-12s %s%s" % (
        (doc.get("started_at") or "?")[5:16],
        doc.get("model") or "?",
        doc.get("bottleneck") or "-",
        doc.get("result") or "?",
        wall(doc),
        product(doc),
        doc.get("label") or "",
        (" [" + doc["ticket"] + "]") if doc.get("ticket") else ""))
hist, runtime_hist, fell_back, axed = {}, {}, 0, 0
for doc in rows:
    m = doc.get("model") or "?"
    hist[m] = hist.get(m, 0) + 1
    # DGN-1470: runtime + fallback aggregation. A pre-runtime record counts
    # as "unknown", never assumed claude-code (what is not measured does not
    # exist -- and must not be invented either).
    rt = doc.get("runtime") or "unknown"
    runtime_hist[rt] = runtime_hist.get(rt, 0) + 1
    if doc.get("runtime_fallback_direction"):
        fell_back += 1
    if doc.get("bottleneck"):
        axed += 1
dist = " / ".join("%s %d" % kv for kv in sorted(hist.items(), key=lambda kv: -kv[1]))
print("[calibration] last %d by model: %s -- bottleneck axis recorded on %d/%d"
      % (len(rows), dist, axed, len(rows)))
runtime_dist = " / ".join("%s %d" % kv for kv in sorted(runtime_hist.items(), key=lambda kv: -kv[1]))
print("[calibration] last %d by runtime: %s -- runtime fallback on %d/%d"
      % (len(rows), runtime_dist, fell_back, len(rows)))
PY
  return 0
}

# ---------------------------------------------------------------------------
# Owner-facing language + name selection (see DISPLAY NAME in the header).
#
# THE KNOB: AGENT_LANG in config/agent.conf. It is the only language value
# this rail can read: DOGANY_LANG belongs to the installer/updater scripts
# (install.sh / update.sh / self-update.sh message helpers, which run before
# or outside an instance), and the bridge's config.locale/LOCALE is a
# pydantic field of the bridge process, unreachable from a shell routine.
# AGENT_LANG is read the way every other shell routine reads it -- by
# sourcing lib/agentlib.sh, which loads config/agent.conf and applies the
# established `AGENT_LANG=${AGENT_LANG:-en}` default. Reading it any other
# way would mean a second parser for one key (the DGN-916 defect class), and
# inventing a dispatch-local knob would mean a fourth spelling of "language".
#
# The source happens in a SUBSHELL on purpose: agent.conf carries unrelated
# keys (PLAN, USAGE_GATE, DASHBOARD_*) that other gates read from the
# ENVIRONMENT, and this reader must not import config values into their
# namespace as a side effect of asking for one string.
# ---------------------------------------------------------------------------
OWNER_LANG="en"

resolve_owner_lang() {
  local lib="$SCRIPT_DIR/lib/agentlib.sh" out=""
  if [[ -f "$lib" ]]; then
    # set +e inside: a conf/lib read problem must degrade to the default, not
    # kill the dispatch. Absent lib (broken install, scratch workspace) -> en.
    out="$( ( set +e; AGENT_ROOT="$WORKSPACE"; . "$lib" >/dev/null 2>&1
              printf '%s' "${AGENT_LANG:-}" ) 2>/dev/null )" || out=""
  fi
  out="$(printf '%s' "$out" | tr 'A-Z' 'a-z' | tr -d '[:space:]')"
  OWNER_LANG="${out:-en}"
}

# rc 0 = this instance's owner-facing language is English. Region/charset
# suffixes count as the same language (en, en_US, en-GB).
owner_lang_is_english() {
  case "${OWNER_LANG%%[-_.]*}" in
    en) return 0 ;;
    *)  return 1 ;;
  esac
}

# The name owner surfaces render for this run: the display name on a
# non-English instance, the machine label otherwise (header, DISPLAY NAME).
owner_facing_name() {
  if ! owner_lang_is_english && [[ -n "$DISPLAY_NAME" ]]; then
    printf '%s' "$DISPLAY_NAME"
  else
    printf '%s' "$LABEL"
  fi
}

# Owner-approved stop copy (2026-09-24). It is deliberately shared by timeout
# and external cancellation/death paths: both are real interruptions the owner
# should see.
# DGN-1688 exempts only an intentional cancellation from this same bridge
# session; it does not alter the notice for any other terminal path.
# $1 = result, $2 = owner-facing name.
draft_result_text() {
  printf '⚠️ %s 작업이 멈췄습니다.' "$2"
}

# end_notice_text <result> <owner-facing name> <HH:MM> [<elapsed secs>]
# The one renderer of the completion push (finalize_run below, and the
# --end-notice mode for session background jobs, DGN-1691 phase 2); the
# <Model> slot is owner_model_display_cap (the label derived from the model
# id the run reported, or --model-slot verbatim for a job that has no model).
#
# COMPLETED: owner-approved block shape (identity Response workflow,
# "Completion signal" -- the designed subagent dispatch END block,
# approved and recorded 2026-09-18; it supersedes the one-line
# "✅ 작업완료: ..." copy the owner had approved 2026-09-11 on this
# surface -- that design postdates the one-liner and IS the fresh owner
# pass its lock comment demanded). Reproduced EXACTLY: title line
# `✅ <background-term> 작업 완료` -- the term is background_term()
# (instance conf; neutral default 백그라운드), never a fixed string, for a
# dispatch run and a shell job alike (DGN-1769); one run per
# finalize, so never a batch count -- then
# `- <Model>: <작업명> <HH:MM> (<소요시간>)` with Model = the
# display label from lib/model_display.py ("Claude Opus 5.5"), 작업명 =
# the owner-facing name, HH:MM = completion time. Unknown elapsed (no
# STARTED_AT on a launcher-side finalize, or a parse failure) drops the
# parenthesis instead of guessing a number.
#
# FAILURE branches: owner-approved copy 2026-09-11 (작업실패,
# owner-authored verbatim; dec-094 UX gate passed) and the two drafts in
# draft_result_text above -- all untouched: the designed block covers
# completion only.
end_notice_text() {
  local result="$1" name="$2" clock="$3" elapsed="${4:-}" end_line
  case "$result" in
    completed)
      local end_label
      end_label="$(owner_model_display_cap)"
      if [[ -n "$end_label" ]]; then
        end_line="$(printf -- '- %s: %s %s' "$end_label" "$name" "$clock")"
      else
        end_line="$(printf -- '- %s %s' "$name" "$clock")"
      fi
      if [[ -n "$elapsed" ]]; then
        end_line="$end_line ($(format_elapsed_ko "$elapsed"))"
      fi
      # owner-approved 2026-09-18: END block title + model line (header above)
      # owner-approved 2026-09-28 07:08: every END notice titles with background_term (DGN-1769)
      printf '%s\n%s' "✅ $(background_term) 작업 완료" "$end_line" ;;
    timed-out|canceled)
      draft_result_text "$result" "$name" ;;
    *)
      printf '⚠️ 작업실패: %s - 원인 확인하고 다시 알려드릴게요.' "$name" ;;
  esac
}

# The <소요시간> slot of the completion block. The BLOCK shape is
# owner-approved (identity Response workflow, "Completion signal"); the
# design names the slot but not the unit rendering, so the unit words below
# are DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (dec-094
# UX gate): "1분 미만" under a minute, "N분" under an hour, "N시간" on the
# hour, "N시간 M분" otherwise. Minutes truncate rather than round: a 59:30
# run reported as "1시간" would claim a boundary it never crossed.
# $1 = elapsed whole seconds.
format_elapsed_ko() {
  local secs="$1" mins hours rem
  case "$secs" in ''|*[!0-9]*) return 1 ;; esac
  if [[ "$secs" -lt 60 ]]; then
    printf '1분 미만'
    return 0
  fi
  mins=$(( secs / 60 ))
  hours=$(( mins / 60 ))
  rem=$(( mins % 60 ))
  if [[ "$hours" -eq 0 ]]; then
    printf '%d분' "$mins"
  elif [[ "$rem" -eq 0 ]]; then
    printf '%d시간' "$hours"
  else
    printf '%d시간 %d분' "$hours" "$rem"
  fi
}

# Whole seconds since STARTED_AT (worker-stamped, %Y-%m-%dT%H:%M:%S%z), or
# rc 1 with empty stdout when it cannot be known (a launcher-side finalize
# has no STARTED_AT; a parse failure must degrade to "no elapsed", never
# guess a number).
elapsed_seconds_since_start() {
  [[ -n "${STARTED_AT:-}" ]] || return 1
  python3 -c '
import datetime, sys
try:
    s = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%S%z")
except Exception:
    sys.exit(1)
d = datetime.datetime.now(datetime.timezone.utc) - s
print(max(0, int(d.total_seconds())))
' "$STARTED_AT" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Spawn path
# ---------------------------------------------------------------------------
# TARGET LAYER (DGN-1226 retroactive declaration, DGN-1194 wiring pass):
# the spawn-arg gates below (--model/--label refusal, usage gate) are
# Layer 2 -- a dev agent gating a dev agent; hard refusal (exit 1) is the
# correct failure shape at this layer.  The layer model is UNSETTLED
# (DGN-1226 sec 4) -- declaration only, no layer branching.  If devkit
# inherits this machinery, it must NOT reach Layer 3 (product users) in
# this form.
# DGN-1430: require a declaration before gates, slots, or worker creation.
# --worktree already explicitly requests writes; do not require it twice.
# --repo is only a cwd selector and never substitutes for --read-only.
validate_access_mode() {
  # DGN-1726: a cloud run writes nothing on this host -- its product is a
  # branch on origin, pushed by the remote session. No local access mode to
  # declare, and a local worktree would be a checkout nobody writes to.
  if [[ "$RUNTIME" == "cloud" ]]; then
    if [[ "$WRITE_REQUESTED" == 1 || -n "$WT_SLUG" ]]; then
      echo "[dispatch] --runtime cloud takes no --write/--worktree: the cloud routine pushes its result to claude/<label> on origin; name the repo with --repo <path>." >&2
      return 1
    fi
    ACCESS_MODE=cloud
    return 0
  fi
  # OWNER-COPY-PENDING: refusal text requires owner review before deployment.
  if [[ "$READ_ONLY" == 1 ]]; then
    if [[ "$WRITE_REQUESTED" == 1 || -n "$WT_SLUG" ]]; then
      echo "[dispatch] --read-only conflicts with --write/--worktree; choose one access mode." >&2
      return 1
    fi
    ACCESS_MODE=read
  elif [[ -n "$WT_SLUG" ]]; then
    ACCESS_MODE=write
  else
    echo "[dispatch] declare --read-only for observation or --worktree <slug> for repository writes; --write requires --worktree <slug>. For live workspace writes (paths that are no git checkout) use the plan lane: --read-only --live-target <path> plans them, then --apply-plan <runid> applies the plan (DGN-1458)." >&2
    return 1
  fi
}

# DGN-1458: --live-target declarations. Sets LIVE_TARGETS (newline-joined
# physical paths). The plan lane is a READ-ONLY lane: the worker itself never
# writes a live target, so the flag requires --read-only.
validate_live_targets() {
  local t real out=""
  LIVE_TARGETS=""
  [[ ${#LIVE_TARGETS_ARG[@]} -gt 0 ]] || return 0
  if [[ "$READ_ONLY" != 1 || -n "$WT_SLUG" || "$RUNTIME" == "cloud" ]]; then
    echo "[dispatch] --live-target requires --read-only (plan lane: the worker plans, --apply-plan applies; a --worktree or cloud run cannot carry it)." >&2
    return 1
  fi
  for t in "${LIVE_TARGETS_ARG[@]}"; do
    if [[ "$t" != /* || ! -e "$t" ]]; then
      echo "[dispatch] --live-target must be an existing absolute path: $t" >&2
      return 1
    fi
    if [[ -d "$t" ]]; then
      real="$(cd "$t" && pwd -P)"
    else
      real="$(cd "$(dirname "$t")" && pwd -P)/$(basename "$t")"
    fi
    if ! sb_path_ok "$real"; then
      echo "[dispatch] --live-target path cannot be quoted safely: $t" >&2
      return 1
    fi
    out="${out:+$out$'\n'}$real"
  done
  LIVE_TARGETS="$out"
  return 0
}

validate_spawn_args() {
  # DGN-1666: --model is no longer required. It is resolved from the measured
  # (runtime, axis) binding in bottleneck_gate(), which runs right after this
  # and already owns that table. --bottleneck stays required -- the axis is the
  # judgment, the model is a lookup, and conflating them is what put a hand
  # value in front of the data.
  if [[ -z "$TASK_FILE" || -z "$LABEL" ]]; then
    echo "[dispatch] --task and --label are required" >&2
    return 1
  fi
  if [[ ! -f "$TASK_FILE" || ! -r "$TASK_FILE" ]]; then
    echo "[dispatch] task file not readable: $TASK_FILE" >&2
    return 1
  fi
  # Empty is legal (auto-resolve); a NAMED model must still be one of the four.
  case "$MODEL" in
    ""|fable|opus|sonnet|haiku) ;;
    *) echo "[dispatch] invalid --model '$MODEL' (fable|opus|sonnet|haiku, or omit to resolve from the axis binding)" >&2; return 1 ;;
  esac
  # DGN-1666: --effort is optional; a given value must be a known level.
  case "$EFFORT" in
    ""|low|medium|high|max) ;;
    *) echo "[dispatch] invalid --effort '$EFFORT' (low|medium|high|max, or omit for the CLI default)" >&2; return 1 ;;
  esac
  # Historical 2026-09-13 exclusion (superseded by the measurement below):
  # DGN-1444: codex observe-mode (no --worktree) is REFUSED, not downgraded.
  # DGN-1191's guarantee for observe mode is not just "no writes" -- it also
  # denies READING ~/.ssh and denies EXEC of the git credential helper, so a
  # read-only worker cannot push or exfiltrate credentials. Empirically
  # tested 2026-09-13: codex's own `--sandbox read-only` blocks writes
  # (including TMPDIR -- stricter than claude's observe profile there) but
  # does NOT deny reading ~/.ssh, and wrapping codex in the SAME sandbox-exec
  # profile render_observe_profile() builds for claude HANGS the codex
  # process (nested seatbelt confinement). Neither path reaches parity, so
  # per DGN-1444 ("say so and refuse rather than downgrade") this refuses
  # the combination instead of running codex under a silently weaker
  # guarantee. Use --worktree <slug> for a codex dispatch.
  # 2026-09-14 remeasurement: both parity grounds above failed (8 probes,
  # zero hangs; outer seatbelt denies SSH reads regardless of internal policy).
  # Accept observe here; do_spawn still requires the real profile self-check.
  # Evidence: files/outbox/2026-09-14-codex-observe-remeasure.md (B2/B3/B4).
  # DGN-1218 LAYER 1: the bottleneck axis is REQUIRED, the same forcing point
  # as --model itself (a written rule runs at 3.4%; a refusing flag ran at
  # 100% -- DGN-1194). It asks "why this tier" in the one machine-checkable
  # form: which property of the TASK bounds the outcome. Axis names are task
  # properties, never model properties -- this block must stay model-name-free.
  if [[ -z "$BOTTLENECK" ]]; then
    {
      echo "[dispatch] --bottleneck is required: declare the task's bottleneck axis before choosing a tier (DGN-1218)."
      echo "[dispatch]   coverage  = exhaustive enumeration; one omission voids the artifact"
      echo "[dispatch]   accuracy  = verifying N given items; being wrong is the risk, not missing"
      echo "[dispatch]   judgment  = the verdict itself is the work (design/security/tradeoff)"
      echo "[dispatch] recent history: $0 --calibration"
    } >&2
    return 1
  fi
  case "$BOTTLENECK" in
    coverage|accuracy|judgment) ;;
    *) echo "[dispatch] invalid --bottleneck '$BOTTLENECK' (coverage|accuracy|judgment)" >&2; return 1 ;;
  esac
  # DGN-1485: the enum prevents invalid labels, not a habitual valid label.
  # The dispatcher cannot safely infer the true bottleneck from arbitrary task
  # text, so require the caller's concise task evidence and preserve it for
  # the calibration reader. This gate deliberately runs before model binding:
  # the task classification must stand on its own.
  local bottleneck_why_trimmed="${BOTTLENECK_WHY//[[:space:]]/}"
  if [[ -z "$bottleneck_why_trimmed" ]]; then
    echo "[dispatch] --bottleneck-why is required: state in one line why '$BOTTLENECK' is this task's bottleneck; it is recorded with the axis for calibration" >&2
    return 1
  fi
  if [[ ${#BOTTLENECK_WHY} -lt 10 ]]; then
    echo "[dispatch] --bottleneck-why too short ('$BOTTLENECK_WHY'): one line of task evidence (>=10 chars) is required" >&2
    return 1
  fi
  case "$BOTTLENECK_WHY" in
    *'|'*|*$'\n'*|*$'\r'*)
      echo "[dispatch] invalid --bottleneck-why: no '|', newline or carriage return (it is written into the pipe-delimited ledger row)" >&2
      return 1 ;;
  esac
  # DISPLAY-NAME GATE (header, DISPLAY NAME). Same forcing shape as
  # --bottleneck above: the rule is not "remember to name it for the owner",
  # it is a refusal at the one decision point that can still be corrected.
  # Armed exactly where the name is rendered -- a non-English instance, where
  # the machine label must never reach an owner surface. On an English
  # instance the label is the owner-facing render, so there is nothing to
  # demand.
  resolve_owner_lang
  # Emptiness test only -- the VALUE itself is never rewritten. Shell-native
  # substitution rather than an external byte-oriented tool: the name is human
  # text in the instance's language, i.e. routinely multi-byte, and the check
  # must not depend on how `tr` behaves under the caller's locale.
  local display_trimmed="${DISPLAY_NAME//[[:space:]]/}"
  # A blank-but-present value is the same silent fallback with an argument in
  # front of it -- checked FIRST so the message names the real problem instead
  # of claiming the field is absent.
  if [[ -n "$DISPLAY_NAME" && -z "$display_trimmed" ]]; then
    echo "[dispatch] --display-name is blank (whitespace only): it would render as an empty name on the owner's work board" >&2
    return 1
  fi
  if ! owner_lang_is_english && [[ -z "$display_trimmed" ]]; then
    {
      echo "[dispatch] --display-name is required: this instance's language is '$OWNER_LANG', so the machine label is never rendered to the owner."
      echo "[dispatch]   --label        = machine label (ledger/run/log key): ASCII, lowercase, hyphenated"
      echo "[dispatch]   --display-name = the same job named for a human, in the instance's language"
      echo "[dispatch] supply both, e.g.: --label ${LABEL:-<label>} --display-name \"<owner-readable name>\""
      echo "[dispatch] it is not optional on purpose: an optional field is the one skipped under load, and the owner surfaces then fall back to the machine label without saying so."
    } >&2
    return 1
  fi
  # The ledger row is pipe-delimited and run.env is line-oriented: a display
  # name carrying either separator would corrupt the row it is written into.
  case "$DISPLAY_NAME" in
    *'|'*|*$'\n'*|*$'\r'*)
      echo "[dispatch] invalid --display-name: no '|', newline or carriage return (the ledger row is pipe-delimited)" >&2
      return 1 ;;
  esac
  # --why is free text ON TOP of the enum, never instead of it. A minimum
  # length blocks the "--why x" reflex; real audit pressure comes from the
  # verbatim ledger record (run.env/status.json), which the reader surfaces.
  if [[ -n "$TIER_WHY" && ${#TIER_WHY} -lt 10 ]]; then
    echo "[dispatch] --why too short ('$TIER_WHY'): one line of real reasoning (>=10 chars); it is recorded verbatim in the run ledger" >&2
    return 1
  fi
  # --unguarded-codex is the LOUD override of the DGN-1444 hook-trust gate.
  # Same discipline as --why: a real reason, recorded verbatim -- and it is
  # refused outright on a non-codex run, because an inert override flag that
  # parses fine is the silent-degradation shape this file keeps paying for.
  if [[ -n "$CODEX_UNGUARDED_WHY" ]]; then
    if [[ "$RUNTIME" != "codex" ]]; then
      echo "[dispatch] --unguarded-codex given but runtime is '$RUNTIME': the flag overrides the codex hook-trust gate and means nothing elsewhere -- refusing rather than ignoring it" >&2
      return 1
    fi
    if [[ ${#CODEX_UNGUARDED_WHY} -lt 10 ]]; then
      echo "[dispatch] --unguarded-codex reason too short ('$CODEX_UNGUARDED_WHY'): one line of real reasoning (>=10 chars); it is recorded verbatim in the run ledger" >&2
      return 1
    fi
    case "$CODEX_UNGUARDED_WHY" in
      *'|'*|*$'\n'*|*$'\r'*)
        echo "[dispatch] invalid --unguarded-codex reason: no '|', newline or carriage return (it is written into the pipe-delimited ledger row)" >&2
        return 1 ;;
    esac
  fi
  if ! [[ "$TIMEOUT_MIN" =~ ^[0-9]+$ ]] || [[ "$TIMEOUT_MIN" -lt 1 ]]; then
    echo "[dispatch] invalid --timeout '$TIMEOUT_MIN' (positive minutes)" >&2
    return 1
  fi
  case "$NOTIFY" in
    push|silent) ;;
    *) echo "[dispatch] invalid --notify '$NOTIFY' (push|silent)" >&2; return 1 ;;
  esac
  if [[ -n "$WT_SLUG" ]] && ! [[ "$WT_SLUG" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "[dispatch] invalid --worktree slug '$WT_SLUG' ([A-Za-z0-9._-] only)" >&2
    return 1
  fi
  if [[ -n "$REPO_PATH" ]]; then
    if [[ -n "$WT_SLUG" ]]; then
      echo "[dispatch] --repo and --worktree are mutually exclusive: worktree mode already owns its cwd ($WORKTREE_BASE/<runid>-<slug>); --repo only redirects the READ-ONLY worker's cwd" >&2
      return 1
    fi
    if [[ ! -d "$REPO_PATH" ]]; then
      echo "[dispatch] --repo path is not a directory: $REPO_PATH" >&2
      return 1
    fi
    # Canonicalize: the worker cd's after detach, long after the caller's cwd
    # is gone -- a relative path must be pinned to an absolute one now.
    REPO_PATH="$(cd "$REPO_PATH" && pwd)"
    # Non-git dirs are ALLOWED (log-dir surveys are legitimate); warn only.
    if ! git -C "$REPO_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "[dispatch] warning: --repo path is not a git repository ($REPO_PATH) -- git commands will fail there; file reads still work"
    fi
  fi
  if [[ -n "$WT_REPO_ARG" ]]; then
    if [[ -z "$WT_SLUG" ]]; then
      echo "[dispatch] --worktree-repo requires --worktree <slug>: it only selects WHICH repo the worktree is cut from. For a read-only worker in another repo use --repo <path>." >&2
      return 1
    fi
    if [[ ! -d "$WT_REPO_ARG" ]]; then
      echo "[dispatch] --worktree-repo path is not a directory: $WT_REPO_ARG" >&2
      return 1
    fi
    # Canonicalize now: the worker resolves it after detach, when the
    # caller's cwd is long gone (same reason as --repo).
    WT_REPO_ARG="$(cd "$WT_REPO_ARG" && pwd)"
    # Unlike --repo, a git repo is MANDATORY here -- there is nothing to cut
    # a worktree from otherwise. Hard refusal, not a warning.
    if ! git -C "$WT_REPO_ARG" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "[dispatch] --worktree-repo path is not a git repository: $WT_REPO_ARG" >&2
      return 1
    fi
    WT_REPO="$WT_REPO_ARG"
  fi
  # For a foreign target repo, the dispatching instance owns the work and its
  # gate battery must govern it. The verifier checks this root via the same
  # installed managed launcher that the worker will invoke.
  if [[ -n "$WT_SLUG" && "$WT_REPO" != "$WORKSPACE" ]]; then
    CODEX_ADAPTER_ROOT="$WORKSPACE"
  fi
  if [[ "$REPO_CONTAINED" == 1 && -z "$WT_SLUG" && -z "$REPO_PATH" ]]; then
    echo "[dispatch] --repo-contained requires --worktree <slug> or --repo <path>: it declares that the work needs only the target repo (CLOUD-FIRST)." >&2
    return 1
  fi
  # DGN-1742 (header, LOCAL-WHY FORCING POINTS). --runtime cloud is exempt:
  # it already goes to cloud.
  if [[ -n "$LOCAL_WHY" ]]; then
    if [[ "$RUNTIME" == "cloud" ]]; then
      echo "[dispatch] --local-why given but runtime is 'cloud': it states why a run stays LOCAL -- refusing rather than ignoring it" >&2
      return 1
    fi
    if [[ -z "$WT_REPO_ARG" && -z "$REPO_PATH" ]]; then
      echo "[dispatch] --local-why given on a workspace-only run: it states why a --repo/--worktree-repo run skips cloud and means nothing here -- refusing rather than ignoring it" >&2
      return 1
    fi
    if [[ ${#LOCAL_WHY} -lt 10 ]]; then
      echo "[dispatch] --local-why reason too short ('$LOCAL_WHY'): one line of real reasoning (>=10 chars); it is recorded verbatim in the run ledger" >&2
      return 1
    fi
    case "$LOCAL_WHY" in
      *'|'*|*$'\n'*|*$'\r'*)
        echo "[dispatch] invalid --local-why reason: no '|', newline or carriage return (it is written into the pipe-delimited ledger row)" >&2
        return 1 ;;
    esac
  fi
  if [[ "$RUNTIME" != "cloud" && ( -n "$WT_REPO_ARG" || -n "$REPO_PATH" ) \
        && "$REPO_CONTAINED" != 1 && -z "$LOCAL_WHY" ]]; then
    echo "[dispatch] REFUSED: a --repo/--worktree-repo run must carry --repo-contained (the work needs only that repo: cloud-eligible) or --local-why \"<reason, >=10 chars>\" (why it must run local)." >&2
    return 1
  fi
  if [[ -n "$WT_BASE_ARG" && -z "$WT_SLUG" && "$RUNTIME" != "cloud" ]]; then
    echo "[dispatch] --base requires --worktree <slug>: it only selects the branch the worktree is cut from (a read-only worker has no branch)." >&2
    return 1
  fi
  if [[ "$RUNTIME" == "cloud" ]]; then
    if [[ -z "$REPO_PATH" ]] || ! git -C "$REPO_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "[dispatch] --runtime cloud requires --repo <git repo>: the cloud routine clones that repo's GitHub origin and pushes claude/<label> back to it." >&2
      return 1
    fi
    # The label becomes a branch name component (claude/<label>).
    if ! [[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || [[ "$LABEL" == *..* || "$LABEL" == *.lock || "$LABEL" == *. ]]; then
      echo "[dispatch] invalid --label '$LABEL' for --runtime cloud: it names the result branch claude/<label> ([A-Za-z0-9._-], no '..')" >&2
      return 1
    fi
    CLOUD_ENV_ID="$(cloud_env_id)"
    if [[ -z "$CLOUD_ENV_ID" ]]; then
      echo "[dispatch] REFUSED: --runtime cloud needs a cloud environment id -- set CLOUD_ENV_ID=env_... in $WORKSPACE/config/agent.conf (or DOGANY_CLOUD_ENV_ID). None is configured, so no routine can be created." >&2
      return 1
    fi
    if ! [[ "$CLOUD_ENV_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
      echo "[dispatch] REFUSED: CLOUD_ENV_ID '$CLOUD_ENV_ID' is not an environment id ([A-Za-z0-9_-])." >&2
      return 1
    fi
    [[ "$TIMEOUT_GIVEN" == 1 ]] || TIMEOUT_MIN="$CLOUD_DEFAULT_TIMEOUT_MIN"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# DGN-1726 cloud runtime helpers (header, CLOUD RUNTIME).
# ---------------------------------------------------------------------------
# The preamble and footer wrapped around every cloud task: the routine gets
# no system-prompt channel, so the base checkout and the return contract ride
# in the task text itself. Routines clone the DEFAULT branch -- the base
# checkout comes first.
cloud_task_preamble() {
  cat <<PREAMBLE
[dispatch setup -- prepended by the dispatcher, not part of the task]
Before anything else, run exactly:
  git fetch origin ${WT_BASE} && git checkout -B work origin/${WT_BASE}
and do all of the work below on top of that checkout.
---

PREAMBLE
}

cloud_task_footer() {
  cat <<FOOTER


---
[dispatch return contract -- appended by the dispatcher, not part of the task]
- Start from origin/${WT_BASE} (the setup line above), not the default branch.
- Deliver the result as commits pushed ONLY to a NEW branch on origin named
  exactly claude/${LABEL}   (git push origin HEAD:refs/heads/claude/${LABEL}).
  If claude/${LABEL} already exists on origin, push nothing and say so.
- Never push to dev or main or any other branch, never force-push or rewrite
  any existing branch, and do not open a pull request -- merging is the
  dispatcher's decision.
- The dispatcher watches origin for that branch; if this session ends
  without it, the run is recorded as failed.
- End your final message with exactly these two lines:
  branch: claude/${LABEL}
  sha: <full commit sha you pushed>
FOOTER
}

# The cloud environment id: env override, else CLOUD_ENV_ID in the instance
# conf through the one conf parser (lib/conf_reader.py). "" when unset.
cloud_env_id() {
  if [[ -n "${DOGANY_CLOUD_ENV_ID:-}" ]]; then
    printf '%s' "$DOGANY_CLOUD_ENV_ID"
    return 0
  fi
  python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from conf_reader import conf_get; print(conf_get("CLOUD_ENV_ID", sys.argv[2]))' \
    "$SCRIPT_DIR/lib" "$WORKSPACE/config/agent.conf" 2>/dev/null | tr -d '[:space:]'
}

# cloud_github_url <origin url> -- https://github.com/<owner>/<repo> for a
# GitHub origin (https, ssh or scp form, optional .git), rc 1 otherwise.
cloud_github_url() {
  python3 -c '
import re, sys
u = sys.argv[1].strip()
m = (re.match(r"^(?:https?|git|ssh)://(?:[^@/]+@)?github\.com(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$", u)
     or re.match(r"^(?:[^@/]+@)?github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$", u))
if not m or not all(re.match(r"^[A-Za-z0-9_.-]+$", g) and g not in (".", "..") for g in m.groups()):
    sys.exit(1)
print("https://github.com/%s/%s" % m.groups())
' "$1"
}

# cloud_remote_sha <repo> <branch> -- the branch head on origin, "" when
# absent. rc 1 when origin cannot be read at all (unknown, not absent).
cloud_remote_sha() {
  local out
  out="$(git -C "$1" ls-remote --heads origin "refs/heads/$2" 2>/dev/null)" || return 1
  printf '%s' "$out" | awk 'NR==1 {print $1}'
}

# Spawn-time preconditions a cloud run cannot recover from later: a GitHub
# origin (the routine's source), a readable origin (the watcher's signal), a
# base branch on origin, and a result branch that does not exist yet (a
# stale one would read as an instant completion). Sets CLOUD_REPO_URL and
# WT_BASE.
cloud_preflight() {
  local sha="" cur="" rc=0 url=""
  # Raw config value: `git remote get-url` would expand insteadOf rewrites.
  url="$(git -C "$REPO_PATH" config --get remote.origin.url 2>/dev/null || true)"
  if [[ -z "$url" ]]; then
    echo "[dispatch] REFUSED: --repo $REPO_PATH has no origin remote -- the cloud routine clones origin and the watcher reads it." >&2
    return 1
  fi
  if ! CLOUD_REPO_URL="$(cloud_github_url "$url")"; then
    echo "[dispatch] REFUSED: origin of $REPO_PATH is not a GitHub repo ($url) -- a cloud routine can only clone and push a GitHub source." >&2
    return 1
  fi
  sha="$(cloud_remote_sha "$REPO_PATH" "claude/$LABEL")" || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "[dispatch] REFUSED: cannot read origin of $REPO_PATH (git ls-remote failed) -- completion would be unobservable." >&2
    return 1
  fi
  if [[ -n "$sha" ]]; then
    echo "[dispatch] REFUSED: claude/$LABEL already exists on origin ($sha) -- pick a new --label; the result branch must be new." >&2
    return 1
  fi
  if [[ -n "$WT_BASE_ARG" ]]; then
    if [[ -z "$(cloud_remote_sha "$REPO_PATH" "$WT_BASE_ARG" 2>/dev/null)" ]]; then
      echo "[dispatch] REFUSED: --base $WT_BASE_ARG is not on origin of $REPO_PATH -- the cloud routine can only check out a pushed branch." >&2
      return 1
    fi
    WT_BASE="$WT_BASE_ARG"
    return 0
  fi
  cur="$(git -C "$REPO_PATH" symbolic-ref --short -q HEAD 2>/dev/null || true)"
  if [[ -n "$cur" && -n "$(cloud_remote_sha "$REPO_PATH" "$cur" 2>/dev/null)" ]]; then
    WT_BASE="$cur"
  else
    WT_BASE="main"
    echo "[dispatch] warning: current branch '${cur:-<detached>}' of $REPO_PATH is not on origin -- the cloud routine starts from origin/main; push it and pass --base if the task depends on it" >&2
  fi
  return 0
}

# CLOUD-FIRST (header). cloud_first_check -> rc 0 when every check holds;
# otherwise CLOUD_FIRST_NOTE="<code>: <detail>" names the first failed check.
# On success CFC_TOP / CFC_BASE carry the resolved target repo and base.
# Read-only with respect to the caller's repos: git reads and ls-remote only.
CLOUD_FIRST_LIVE_RE='(^|[^A-Za-z0-9_-])\.dogany([/"'"'"'`[:space:]]|$)|launchctl|launchd|LaunchAgents|[Rr]ehears'
cloud_first_check() {
  # $1 = ignore_explicit: judge eligibility as if no runtime were requested
  # (DGN-1742 explicit_runtime_cloud_gate).
  local ignore_explicit="${1:-}"
  local until today target top url home_live ws_real sha rc=0 cand models local_sha ahead
  CFC_TOP="" CFC_BASE=""
  if [[ "$REQUESTED_RUNTIME" != auto && -z "$ignore_explicit" ]]; then
    CLOUD_FIRST_NOTE="explicit_runtime: $REQUESTED_RUNTIME (source=$RUNTIME_SELECTION_SOURCE)"; return 1
  fi
  until="${DOGANY_CLOUD_FIRST_UNTIL:-}"
  if [[ -z "$until" ]]; then
    until="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from conf_reader import conf_get; print(conf_get("CLOUD_FIRST_UNTIL", sys.argv[2]))' \
      "$SCRIPT_DIR/lib" "$WORKSPACE/config/agent.conf" 2>/dev/null | tr -d '[:space:]')"
  fi
  [[ -n "$until" ]] || until="$CLOUD_FIRST_DEFAULT_UNTIL"
  if [[ "$until" == off ]]; then
    CLOUD_FIRST_NOTE="cloud_first_off: CLOUD_FIRST_UNTIL=off"; return 1
  fi
  if ! [[ "$until" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    CLOUD_FIRST_NOTE="cloud_first_off: CLOUD_FIRST_UNTIL '$until' is not YYYY-MM-DD"; return 1
  fi
  today="$(date +%F)"
  if [[ "$today" > "$until" ]]; then
    CLOUD_FIRST_NOTE="cloud_first_expired: until=$until today=$today"; return 1
  fi
  if expiry_flag_active "$CLOUD_EXHAUSTED_FLAG"; then
    CLOUD_FIRST_NOTE="cloud_credit_exhausted: $CLOUD_EXHAUSTED_FLAG"; return 1
  fi
  if [[ ${#LIVE_TARGETS_ARG[@]} -gt 0 ]]; then
    CLOUD_FIRST_NOTE="plan_lane: --live-target plans against this host's live paths"; return 1
  fi
  if [[ -n "$WT_SLUG" ]]; then
    if [[ "$REPO_CONTAINED" != 1 ]]; then
      CLOUD_FIRST_NOTE="worktree_not_repo_contained: --worktree without --repo-contained stays local"; return 1
    fi
    if [[ -z "$WT_REPO_ARG" ]]; then
      CLOUD_FIRST_NOTE="live_instance_target: --worktree cut from the workspace (no --worktree-repo)"; return 1
    fi
    target="$WT_REPO"
  else
    if [[ -z "$REPO_PATH" ]]; then
      CLOUD_FIRST_NOTE="no_repo_target: read-only without --repo runs in the workspace"; return 1
    fi
    target="$REPO_PATH"
  fi
  top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || top=""
  if [[ -z "$top" ]]; then
    CLOUD_FIRST_NOTE="no_git_repo: $target"; return 1
  fi
  top="$(cd "$top" && pwd -P)"
  home_live="$(cd "$HOME/.dogany" 2>/dev/null && pwd -P || printf '%s' "$HOME/.dogany")"
  ws_real="$(cd "$WORKSPACE" && pwd -P)"
  if [[ "$top/" == "$home_live/"* || "$top/" == "$ws_real/"* || "$ws_real/" == "$top/"* \
        || -f "$top/config/agent.conf" ]]; then
    CLOUD_FIRST_NOTE="live_instance_target: $top"; return 1
  fi
  if grep -qE "$CLOUD_FIRST_LIVE_RE" "$TASK_FILE" 2>/dev/null \
     || grep -qF -e "$WORKSPACE" -e "$ws_real" "$TASK_FILE" 2>/dev/null; then
    CLOUD_FIRST_NOTE="task_touches_live: task text names ~/.dogany, launchd/launchctl, rehearsal or the workspace"; return 1
  fi
  url="$(git -C "$top" config --get remote.origin.url 2>/dev/null || true)"
  if [[ -z "$url" ]] || ! cloud_github_url "$url" >/dev/null; then
    CLOUD_FIRST_NOTE="no_github_remote: origin='${url:-none}'"; return 1
  fi
  local env_id
  env_id="$(cloud_env_id)"
  if [[ -z "$env_id" ]] || ! [[ "$env_id" =~ ^[A-Za-z0-9_-]+$ ]]; then
    CLOUD_FIRST_NOTE="cloud_env_unconfigured: no valid CLOUD_ENV_ID"; return 1
  fi
  if ! [[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || [[ "$LABEL" == *..* || "$LABEL" == *.lock || "$LABEL" == *. ]]; then
    CLOUD_FIRST_NOTE="label_not_branch_safe: $LABEL"; return 1
  fi
  models="$(tier_binding_lookup cloud "$BOTTLENECK" 2>/dev/null)" || models=""
  cand="${MODEL:-${models%%,*}}"
  if [[ -z "$models" || ",$models," != *",$cand,"* ]]; then
    CLOUD_FIRST_NOTE="cloud_tier_unbound: (cloud, $BOTTLENECK) measured=[${models}] model=${cand:-none}"; return 1
  fi
  if [[ -n "$WT_SLUG" ]]; then
    resolve_wt_base
    CFC_BASE="$WT_BASE"
  else
    CFC_BASE="$(git -C "$top" symbolic-ref --short -q HEAD 2>/dev/null || true)"
    if [[ -z "$CFC_BASE" ]]; then
      CLOUD_FIRST_NOTE="detached_head: $top has no checked-out branch"; return 1
    fi
    if [[ -n "$(git -C "$top" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
      CLOUD_FIRST_NOTE="uncommitted_changes: $top ($CFC_BASE) -- the cloud clone would not see them"; return 1
    fi
  fi
  sha="$(cloud_remote_sha "$top" "claude/$LABEL")" || rc=$?
  if [[ $rc -ne 0 ]]; then
    CLOUD_FIRST_NOTE="origin_unreadable: git ls-remote failed for $top"; return 1
  fi
  if [[ -n "$sha" ]]; then
    CLOUD_FIRST_NOTE="result_branch_exists: claude/$LABEL on origin"; return 1
  fi
  sha="$(cloud_remote_sha "$top" "$CFC_BASE" 2>/dev/null || true)"
  if [[ -z "$sha" ]]; then
    CLOUD_FIRST_NOTE="base_not_on_origin: $CFC_BASE"; return 1
  fi
  local_sha="$(git -C "$top" rev-parse -q --verify "refs/heads/$CFC_BASE^{commit}" 2>/dev/null || true)"
  # Pushed = contained in the origin head; when that object was never
  # fetched, contained in the last-fetched origin/<base> tracking ref.
  if [[ -n "$local_sha" && "$local_sha" != "$sha" ]]; then
    if ! git -C "$top" merge-base --is-ancestor "$local_sha" "$sha" 2>/dev/null; then
      if git -C "$top" cat-file -e "$sha^{commit}" 2>/dev/null; then
        ahead="$(git -C "$top" rev-list --count "$sha..$local_sha" 2>/dev/null || echo '?')"
        CLOUD_FIRST_NOTE="unpushed_commits: $CFC_BASE is $ahead commit(s) ahead of origin -- push it (private remote) or run local"; return 1
      fi
      if ! git -C "$top" merge-base --is-ancestor "$local_sha" "refs/remotes/origin/$CFC_BASE" 2>/dev/null; then
        ahead="$(git -C "$top" rev-list --count "refs/remotes/origin/$CFC_BASE..$local_sha" 2>/dev/null || echo '?')"
        CLOUD_FIRST_NOTE="unpushed_commits: $CFC_BASE is $ahead commit(s) ahead of origin (last fetch) -- push it (private remote) or run local"; return 1
      fi
    fi
  fi
  CFC_TOP="$top"
  CLOUD_FIRST_NOTE="eligible: target=$top base=$CFC_BASE until=$until"
  return 0
}

# DGN-1742 forcing point 2 (header, LOCAL-WHY FORCING POINTS): an explicit
# claude-code|codex runtime on a run cloud-first would take needs a stated
# reason. rc 0 = proceed, rc 2 = refused. Not a repo run -> nothing to judge.
explicit_runtime_cloud_gate() {
  case "$REQUESTED_RUNTIME" in claude-code|codex) ;; *) return 0 ;; esac
  [[ -n "$REPO_PATH" || -n "$WT_REPO_ARG" ]] || return 0
  local note_saved="$CLOUD_FIRST_NOTE" wt_base_saved="$WT_BASE"
  if ! cloud_first_check ignore_explicit; then
    CLOUD_FIRST_NOTE="$note_saved" WT_BASE="$wt_base_saved"
    return 0
  fi
  local eligible="$CLOUD_FIRST_NOTE"
  CLOUD_FIRST_NOTE="$note_saved" WT_BASE="$wt_base_saved"
  if [[ -z "$LOCAL_WHY" ]]; then
    echo "[dispatch] REFUSED: --runtime $REQUESTED_RUNTIME (source=$RUNTIME_SELECTION_SOURCE) on a cloud-eligible run ($eligible) needs --local-why \"<reason, >=10 chars>\" -- or drop the runtime and let cloud-first take it." >&2
    return 2
  fi
  echo "[dispatch] cloud-first: skipped by explicit runtime $REQUESTED_RUNTIME on a cloud-eligible run -- local-why: $LOCAL_WHY"
  return 0
}

# cloud_first_select -- rc 0 when cloud was chosen and the run converted to
# the cloud shape (--repo <top> --base <base>, no worktree); rc 1 otherwise
# (the local selection proceeds); rc 2 = refused (DGN-1742: unpushed base
# commits and no --local-why). Prints one line either way.
cloud_first_select() {
  local mode="read-only"
  [[ -n "$WT_SLUG" ]] && mode="worktree(repo-contained)"
  if ! cloud_first_check; then
    if [[ "$CLOUD_FIRST_NOTE" == unpushed_commits:* ]]; then
      if [[ -z "$LOCAL_WHY" ]]; then
        echo "[dispatch] REFUSED: cloud-first blocked by unpushed commits (${CLOUD_FIRST_NOTE#unpushed_commits: }) -- push $CFC_BASE to origin first, or pass --local-why \"<reason, >=10 chars>\" to run local." >&2
        return 2
      fi
      CLOUD_FIRST_NOTE+="; local_why given"
    fi
    echo "[dispatch] cloud-first: not chosen -- $CLOUD_FIRST_NOTE"
    return 1
  fi
  if [[ -n "$LOCAL_WHY" ]]; then
    CLOUD_FIRST_NOTE="local_why: ${CLOUD_FIRST_NOTE}"
    echo "[dispatch] cloud-first: not chosen -- $CLOUD_FIRST_NOTE (--local-why: $LOCAL_WHY)"
    return 1
  fi
  RUNTIME="cloud"
  CLOUD_FIRST_SELECTED=1
  RUNTIME_SELECTION_SOURCE="auto"
  RUNTIME_SELECTION_REASON="cloud_first_eligible"
  RUNTIME_SELECTION_DETAIL="mode=$mode; $CLOUD_FIRST_NOTE; axis=$BOTTLENECK model=${MODEL:-binding}"
  if [[ -n "$WT_SLUG" ]]; then
    echo "[dispatch] cloud-first: worktree '$WT_SLUG' becomes a cloud run -- result branch claude/$LABEL on origin, no local dsp/$WT_SLUG"
  fi
  REPO_PATH="$CFC_TOP"
  WT_BASE_ARG="$CFC_BASE"
  WT_SLUG="" WT_REPO_ARG="" WT_REPO="$WORKSPACE"
  WRITE_REQUESTED=0 READ_ONLY=0 REPO_CONTAINED=0
  echo "[dispatch] cloud-first: cloud chosen ($mode; $CLOUD_FIRST_NOTE)"
  return 0
}

# Resolve the base branch for the worktree (DGN-1184 (A)); see header. Sets
# WT_BASE. Explicit --base wins; else canonical marker + dev -> dev; else main.
resolve_wt_base() {
  local top
  if [[ -n "$WT_BASE_ARG" ]]; then
    WT_BASE="$WT_BASE_ARG"
    return 0
  fi
  top="$(git -C "$WT_REPO" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$WT_REPO")"
  if [[ -f "$top/.dogany-canonical" ]] \
     && git -C "$WT_REPO" show-ref --verify --quiet refs/heads/dev; then
    WT_BASE="dev"
  else
    WT_BASE="main"
  fi
}

# Provision dsp/<slug> worktree + freshness gate (DGN-556). Cut from WT_REPO
# (= WORKSPACE unless --worktree-repo moved it). Echoes nothing on stdout
# except via globals WT_PATH / BRANCH; returns 3 on failure. On success, the
# gate verdict (fresh / stale-merged) is recorded as one line in
# $RUNDIR/freshness.txt -- the machine's own record of whether the gate ran,
# so a worker or a later audit never has to guess (DGN-1230).
provision_worktree() {
  BRANCH="dsp/$WT_SLUG"
  # runid prefix keeps the path unique across repos as well as across runs.
  WT_PATH="$WORKTREE_BASE/$RUNID-$WT_SLUG"
  mkdir -p "$WORKTREE_BASE"
  resolve_wt_base
  # The base branch is the branch point AND the freshness reference -- a
  # target repo without it would fail deeper with an opaque "worktree add"
  # error.
  if ! git -C "$WT_REPO" show-ref --verify --quiet "refs/heads/$WT_BASE"; then
    echo "[dispatch] target repo has no '$WT_BASE' branch: $WT_REPO" >&2
    return 3
  fi
  if git -C "$WT_REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    if ! git -C "$WT_REPO" worktree add "$WT_PATH" "$BRANCH" >/dev/null 2>&1; then
      echo "[dispatch] worktree add failed (existing branch $BRANCH in $WT_REPO)" >&2
      return 3
    fi
  else
    if ! git -C "$WT_REPO" worktree add "$WT_PATH" -b "$BRANCH" "$WT_BASE" >/dev/null 2>&1; then
      echo "[dispatch] worktree add -b $BRANCH $WT_BASE failed (repo $WT_REPO)" >&2
      return 3
    fi
  fi
  # Freshness gate: the base must be an ancestor; stale -> merge base;
  # conflict -> abort (never build on a stale base).
  if git -C "$WT_PATH" merge-base --is-ancestor "$WT_BASE" HEAD; then
    echo "fresh base=$WT_BASE" > "$RUNDIR/freshness.txt"
  else
    if ! git -C "$WT_PATH" merge --no-edit "$WT_BASE" >/dev/null 2>&1; then
      git -C "$WT_PATH" merge --abort >/dev/null 2>&1 || true
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
      echo "[dispatch] freshness gate FAILED: branch $BRANCH cannot merge $WT_BASE cleanly -- resolve manually first" >&2
      return 3
    fi
    echo "stale-merged base=$WT_BASE" > "$RUNDIR/freshness.txt"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Observe mode (DGN-1191): kernel-enforced read-only for the non-worktree
# worker. Profile is RENDERED per run (paths substituted), SELF-CHECKED
# before spawn, and the worker only ever consumes it.
# ---------------------------------------------------------------------------

# Fail-closed availability gate. No allowlist fallback exists on purpose: an
# engine-level allowlist cannot see what an allowed command's options do
# (git --output, sqlite3 writefile, python3 -m json.tool ...), which is the
# hole this mode replaces.
observe_sandbox_available() {
  hash -r   # never trust a stale command hash for a fail-closed decision
  if ! command -v sandbox-exec >/dev/null 2>&1; then
    echo "[dispatch] observe mode needs sandbox-exec (macOS seatbelt) and it is not available -- REFUSING to spawn (no allowlist fallback: that fallback is the DGN-1191 write hole). Use --worktree <slug> for a write-capable run." >&2
    return 1
  fi
  return 0
}

# Seatbelt string literals: refuse paths the syntax cannot quote safely
# (fail-closed rather than emit a profile that parses differently).
sb_path_ok() {
  case "$1" in
    *'"'*|*'\'*|*$'\n'*|*$'\r'*) return 1 ;;
  esac
  [[ "$1" == /* ]]
}

# One (subpath ...) group covering the given path in every spelling the
# kernel might see: as given, its physical path (seatbelt compares canonical
# paths), and the /private form for a /tmp path that does not exist yet.
sb_subpaths() {
  local p="$1" r=""
  sb_path_ok "$p" || return 1
  printf '(subpath "%s")' "$p"
  if [[ -d "$p" ]]; then
    r="$(cd "$p" && pwd -P)"
    [[ "$r" != "$p" ]] && printf ' (subpath "%s")' "$r"
  elif [[ "$p" == /tmp/* || "$p" == /tmp ]]; then
    printf ' (subpath "/private%s")' "$p"
  fi
  return 0
}

# Escape a literal path for use inside a seatbelt (regex #"...") pattern.
sb_regex_escape() {
  printf '%s' "$1" | sed 's/[][\.*^$()|+?{}\\]/\\&/g'
}

# ~/.claude/projects/<slug> as the CLI derives it from the worker cwd.
sb_project_slug() {
  printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'
}

# render_observe_profile <out> <workdir> <session-id>
# Rule order is the contract: seatbelt applies the LAST matching rule, so the
# targets (WORKDIR / WORKSPACE / WORKTREE_BASE) are denied at the very end and
# beat the /tmp allow when a target lives under /tmp (the case that broke the
# first lab profile, docs/DGN-1191-observe-mode-design.md sec 7.1).
render_observe_profile() {
  local out="$1" workdir="$2" sid="$3"
  local home="$HOME" home_re cc slug slugs="" wd_real="" ws_real=""
  if ! sb_path_ok "$home" || ! sb_path_ok "$workdir" || ! sb_path_ok "$WORKSPACE" \
     || ! sb_path_ok "$WORKTREE_BASE" || ! [[ "$sid" =~ ^[0-9a-f-]{36}$ ]] \
     || { [[ -n "$PLAN_DIR" ]] && ! sb_path_ok "$PLAN_DIR"; }; then
    echo "[dispatch] observe profile: unquotable path or bad session id -- refusing" >&2
    return 1
  fi
  home_re="$(sb_regex_escape "$home")"
  cc="$home/.claude"
  # git's keychain credential helper (https push). Resolved, not hardcoded;
  # absent helper = nothing to deny.
  local cred_helper=""
  cred_helper="$(git --exec-path 2>/dev/null)/git-credential-osxkeychain"
  { [[ -x "$cred_helper" ]] && sb_path_ok "$cred_helper"; } || cred_helper=""
  # Project-state slugs: the cwd as given and as the kernel reports it
  # (process.cwd() is physical) -- both are opened for THIS session only.
  slug="$(sb_project_slug "$workdir")"
  slugs="(literal \"$cc/projects/$slug\") (regex #\"^$home_re/\\.claude/projects/$slug/$sid(\\.|/|\$)\")"
  if [[ -d "$workdir" ]]; then
    wd_real="$(cd "$workdir" && pwd -P)"
    if [[ "$wd_real" != "$workdir" ]]; then
      slug="$(sb_project_slug "$wd_real")"
      slugs="$slugs (literal \"$cc/projects/$slug\") (regex #\"^$home_re/\\.claude/projects/$slug/$sid(\\.|/|\$)\")"
    fi
  fi
  {
    echo '(version 1)'
    echo ";; DGN-1191 observe mode -- rendered by dispatch-detached.sh; rule ORDER is load-bearing (last match wins)."
    echo '(allow default)'
    echo ';; 1. deny every filesystem write ...'
    echo '(deny file-write*)'
    echo ';; 2. ... then re-open only what the worker process itself needs'
    echo '(allow file-write* (subpath "/dev"))'
    echo '(allow file-write* (subpath "/private/var/folders"))   ; TMPDIR (mktemp of dry-run scripts)'
    echo '(allow file-write* (subpath "/private/tmp"))           ; /tmp scratch + CLI cwd tracker'
    echo "(allow file-write* (subpath \"$cc\"))                  ; CLI session state"
    if [[ "$RUNTIME" == "codex" ]]; then
      # Codex needs its own session state outside WORKSPACE and WORKDIR.
      # Open only this runtime's home, not HOME; TARGETS LAST still wins
      # if a target overlaps it. The SSH read deny below remains in force.
      echo "(allow file-write* $(sb_subpaths "$home/.codex")) ; Codex session state"
    fi
    echo ';; 3. inside ~/.claude: configuration, hooks, plugins and skills stay closed'
    echo "(deny file-write* (literal \"$cc/settings.json\") (literal \"$cc/settings.local.json\") (literal \"$cc/CLAUDE.md\") (literal \"$cc/keybindings.json\"))"
    echo "(deny file-write* (subpath \"$cc/plugins\") (subpath \"$cc/skills\") (subpath \"$cc/agents\") (subpath \"$cc/commands\") (subpath \"$cc/hooks\") (subpath \"$cc/scripts\") (subpath \"$cc/rules\"))"
    echo ';;    DGN-1050: the CLI writes ~/.claude/.credentials.json ONLY when a keychain write fails'
    echo ';;    non-transiently -- exactly what rule 1 causes -- and then DELETES the keychain entry.'
    echo ';;    Denying the file (regex: the CLI stages via .credentials.json.tmp.<hex>) makes that'
    echo ';;    fallback fail cleanly: audited CLI behavior is retry x3 -> save_failed, no crash, no'
    echo ';;    keychain delete, process continues on the old token. Reads stay open.'
    echo "(deny file-write* (regex #\"^$home_re/\\.claude/\\.credentials\\.json\"))"
    echo "(deny file-write* (regex #\"^$home_re/\\.claude\\.json\"))   ; global config incl. permission cache"
    echo ';; 4. state that OTHER live sessions source into their unsandboxed shells: shell snapshots'
    echo ';;    (denied outright -- the CLI runs without one), per-session env, transcripts (own session only)'
    echo "(deny file-write* (subpath \"$cc/shell-snapshots\"))"
    echo "(deny file-write* (subpath \"$cc/session-env\"))"
    echo "(allow file-write* (subpath \"$cc/session-env/$sid\"))"
    echo "(deny file-write* (subpath \"$cc/projects\"))"
    echo "(allow file-write* $slugs)"
    echo ';; 5. push credentials cut: ssh keys/known_hosts unreadable; the git credential helper cannot run'
    echo ';;    (denying the gitconfig files instead makes git itself fatal on EPERM -- measured)'
    echo "(deny file-read* (subpath \"$home/.ssh\") (subpath \"$home/.config/gh\"))"
    if [[ -n "$cred_helper" ]]; then
      echo "(deny file-read* (literal \"$cred_helper\"))"
      echo "(deny process-exec (literal \"$cred_helper\"))"
    fi
    echo ';; 6. TARGETS LAST -- these must beat every allow above, wherever the target lives'
    echo "(deny file-write* $(sb_subpaths "$workdir"))        ; WORKDIR (worker cwd)"
    echo "(deny file-write* $(sb_subpaths "$WORKSPACE"))      ; WORKSPACE (live checkout, RUNDIR, ledger)"
    echo "(deny file-write* $(sb_subpaths "$WORKTREE_BASE"))  ; other dispatches' worktrees"
    if [[ -n "$PLAN_DIR" ]]; then
      local lt
      while IFS= read -r lt; do
        [[ -n "$lt" ]] && echo "(deny file-write* $(sb_subpaths "$lt"))  ; live target (plan lane: planned, never written here)"
      done <<<"$LIVE_TARGETS"
      echo ';; 7. PLAN LANE (DGN-1458): the one writable place -- RUNDIR/plan (the profile itself stays denied)'
      echo "(allow file-write* $(sb_subpaths "$PLAN_DIR"))"
    fi
  } > "$out" || return 1
  return 0
}

# observe_profile_selfcheck <profile> <target>...
# Proves the rendered profile before a worker exists: it compiles, scratch
# is open (the CLI cannot run otherwise), and a write inside EACH target is
# refused by the kernel. Any failure = spawn refusal (a render defect must
# never become an open worker).
observe_profile_selfcheck() {
  local prof="$1"; shift
  local t probe
  if ! sandbox-exec -f "$prof" /usr/bin/true 2>/dev/null; then
    echo "[dispatch] observe profile does not compile: $prof -- refusing to spawn" >&2
    return 1
  fi
  if ! sandbox-exec -f "$prof" /bin/sh -c 'd="$(mktemp -d)" && rmdir "$d"' 2>/dev/null; then
    echo "[dispatch] observe profile closed TMPDIR (the worker could not run) -- refusing to spawn" >&2
    return 1
  fi
  for t in "$@"; do
    [[ -d "$t" ]] || continue
    probe="$t/.observe-probe-$$"
    if sandbox-exec -f "$prof" /bin/mkdir "$probe" 2>/dev/null; then
      rmdir "$probe" 2>/dev/null || true
      echo "[dispatch] observe profile self-check FAILED: sandbox allowed a write inside $t -- refusing to spawn" >&2
      return 1
    fi
  done
  return 0
}

# ---------------------------------------------------------------------------
# WRITE-MODE ISOLATION (DGN-1504).
# A --worktree run used to get its own checkout but NO boundary: the claude
# worker ran with bypassPermissions and could (and did, 2026-09-16/19/21)
# write into the SHARED canonical checkout, switch its HEAD, and commit onto
# dev. The worktree was an option the worker could ignore. The boundary now
# lives in the kernel, like observe mode's: the worker runs under a rendered
# seatbelt profile that
#   1. closes EVERY checkout on the estate roster (every worktree of every
#      repo under the estate roots and of every registered shared checkout,
#      e.g. the canonical DOGANY_REPO_ROOT) plus WORKTREE_BASE (other
#      dispatches),
#   2. re-opens the git object stores (commits, `git worktree add` work),
#   3. re-closes the shared state inside them: the main checkout's index /
#      HEAD / config / packed-refs / hooks, and the base-branch refs (main, dev,
#      master, the run's WT_BASE) -- nothing detached may move them,
#   4. opens the run's OWN worktree LAST (last match wins).
# Everything else keeps write-mode's old reach (allow default): the fix is
# scoped to the one resource that is shared, not to "writes" in general.
# The claude-code runtime only: codex already runs its own kernel sandbox
# (workspace-write = cwd + git-common-dir, select_codex_tool_policy).
# Missing sandbox-exec -> REFUSED, never a silent unconfined run.
# ---------------------------------------------------------------------------
# Branch refs no detached run may move, whatever its WT_BASE.
PROTECTED_BRANCHES_DEFAULT="main dev master"

write_sandbox_available() {
  hash -r
  if ! command -v sandbox-exec >/dev/null 2>&1; then
    echo "[dispatch] write mode needs sandbox-exec (macOS seatbelt) to confine the worker to its own worktree, and it is not available -- REFUSING to spawn (an unconfined worker can write the shared checkout: DGN-1504)." >&2
    return 1
  fi
  return 0
}

# Toplevel of a checkout, physical path. Empty on failure.
wt_toplevel() {
  local top=""
  top="$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" || return 1
  (cd "$top" 2>/dev/null && pwd -P)
}

# Absolute, physical git-common-dir of a repo/checkout. Empty on failure.
wt_common_dir() {
  local d=""
  d="$(git -C "$1" rev-parse --git-common-dir 2>/dev/null)" || return 1
  case "$d" in
    /*) : ;;
    *) d="$1/$d" ;;
  esac
  (cd "$d" 2>/dev/null && pwd -P)
}

# Estate roots = directories whose git children form the roster.
# DOGANY_ESTATE_ROOTS (colon-separated) overrides; default = the parent
# directory of the workspace and of the target repo (canonical sits beside
# its sibling repos, an instance beside its sibling instances).
estate_roots() {
  local r
  if [[ -n "${DOGANY_ESTATE_ROOTS:-}" ]]; then
    tr ':' '\n' <<<"$DOGANY_ESTATE_ROOTS" | sed '/^$/d'
    return 0
  fi
  for r in "$WORKSPACE" "$WT_REPO"; do
    r="$(wt_toplevel "$r" 2>/dev/null || printf '%s' "$r")"
    dirname "$r"
  done | awk '!seen[$0]++'
}

# Registered shared checkouts: repos closed by NAME, not by adjacency.
# estate_roots() only sees siblings of the workspace / target repo; an
# instance workspace (e.g. <estate>/agents/<slug>) is not a sibling of the
# canonical framework checkout (~/dogany/dogany-agent), so a --worktree run
# from that workspace left canonical writable -- the exact 09-16/09-19/09-21
# incident path (live verify 2026-09-27: OPEN-APPEND OK on canonical VERSION).
# Sources (all read, union, one path per line; missing = none):
#   - DOGANY_REPO_ROOT in <workspace>/.instance.conf and in the target
#     repo's .instance.conf (the canonical checkout the instance was minted
#     from and updates from),
#   - SHARED_CHECKOUTS in <workspace>/config/agent.conf (colon-separated),
#   - DOGANY_SHARED_CHECKOUTS in the environment (colon-separated).
# Not narrowed by DOGANY_ESTATE_ROOTS: the override replaces the adjacency
# scan, never the named list. When a registered repo IS the target, the
# own-worktree rule (last match) still opens this run's worktree only.
registered_shared_checkouts() {
  local conf top v
  top="$(wt_toplevel "$WT_REPO" 2>/dev/null)" || top="$WT_REPO"
  for conf in "$WORKSPACE/.instance.conf" "$top/.instance.conf"; do
    [[ -f "$conf" ]] || continue
    python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from conf_reader import conf_get; print(conf_get("DOGANY_REPO_ROOT", sys.argv[2]))' \
      "$SCRIPT_DIR/lib" "$conf" 2>/dev/null
  done
  v="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from conf_reader import conf_get; print(conf_get("SHARED_CHECKOUTS", sys.argv[2]))' \
      "$SCRIPT_DIR/lib" "$WORKSPACE/config/agent.conf" 2>/dev/null)"
  tr ':' '\n' <<<"$v"
  tr ':' '\n' <<<"${DOGANY_SHARED_CHECKOUTS:-}"
}

# write_roster -> globals ROSTER_CHECKOUTS (every checkout that is not this
# run's) and ROSTER_GITDIRS (their git common dirs). Repos = workspace +
# target repo + every registered shared checkout + every direct git child of
# an estate root; checkouts = each repo's `git worktree list`. Checkouts
# under WORKTREE_BASE are covered by the WORKTREE_BASE rule and are not
# listed one by one.
write_roster() {
  ROSTER_CHECKOUTS=()
  ROSTER_GITDIRS=()
  local repos=() root child repo gd line path own="" base_real=""
  own="$(cd "$WT_PATH" 2>/dev/null && pwd -P)" || own="$WT_PATH"
  base_real="$(cd "$WORKTREE_BASE" 2>/dev/null && pwd -P)" || base_real="$WORKTREE_BASE"
  repos+=("$WORKSPACE" "$WT_REPO")
  while IFS= read -r repo; do
    repo="$(printf '%s' "$repo" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [[ -n "$repo" ]] && repos+=("${repo/#\~/$HOME}")
  done < <(registered_shared_checkouts)
  while IFS= read -r root; do
    [[ -d "$root" ]] || continue
    for child in "$root"/* "$root"/.[!.]*; do
      [[ -e "$child/.git" ]] && repos+=("$child")
    done
  done < <(estate_roots)
  local seen_gd=" " seen_co=" "
  for repo in "${repos[@]}"; do
    gd="$(wt_common_dir "$repo" 2>/dev/null)" || continue
    [[ -n "$gd" && "$seen_gd" != *" $gd "* ]] || continue
    seen_gd="$seen_gd$gd "
    ROSTER_GITDIRS+=("$gd")
    while IFS= read -r line; do
      [[ "$line" == "worktree "* ]] || continue
      path="${line#worktree }"
      path="$(cd "$path" 2>/dev/null && pwd -P)" || continue
      [[ "$path" == "$own" ]] && continue
      [[ "$path" == "$base_real" || "$path" == "$base_real"/* ]] && continue
      [[ "$seen_co" == *" $path "* ]] && continue
      seen_co="$seen_co$path "
      ROSTER_CHECKOUTS+=("$path")
    done < <(git -C "$gd" worktree list --porcelain 2>/dev/null)
  done
  return 0
}

# render_write_profile <out>. Needs WT_PATH, WT_BASE, WORKTREE_BASE and the
# roster (write_roster). Rule ORDER is the contract (last match wins).
render_write_profile() {
  local out="$1" p gd b bases="" gd_re
  if ! sb_path_ok "$WT_PATH" || ! sb_path_ok "$WORKTREE_BASE"; then
    echo "[dispatch] write profile: unquotable worktree path -- refusing" >&2
    return 1
  fi
  for p in ${ROSTER_CHECKOUTS[@]+"${ROSTER_CHECKOUTS[@]}"} ${ROSTER_GITDIRS[@]+"${ROSTER_GITDIRS[@]}"}; do
    if ! sb_path_ok "$p"; then
      echo "[dispatch] write profile: unquotable roster path '$p' -- refusing" >&2
      return 1
    fi
  done
  for b in $PROTECTED_BRANCHES_DEFAULT ${WT_BASE:-}; do
    [[ "$b" =~ ^[A-Za-z0-9._/-]+$ ]] || continue
    [[ " $bases " == *" $b "* ]] || bases="$bases $b"
  done
  bases="$(printf '%s' "$bases" | sed 's/^ //' | tr ' ' '\n' | while IFS= read -r b; do sb_regex_escape "$b"; printf '|'; done | sed 's/|$//')"
  {
    echo '(version 1)'
    echo ";; DGN-1504 write mode -- rendered by dispatch-detached.sh; rule ORDER is load-bearing (last match wins)."
    echo '(allow default)'
    echo ';; 1. every checkout on the estate roster is closed (shared working trees)'
    for p in ${ROSTER_CHECKOUTS[@]+"${ROSTER_CHECKOUTS[@]}"}; do
      echo "(deny file-write* $(sb_subpaths "$p"))"
    done
    echo "(deny file-write* $(sb_subpaths "$WORKTREE_BASE"))  ; other dispatches' worktrees"
    echo ';; 2. object stores re-opened: commits and `git worktree add` keep working'
    for gd in ${ROSTER_GITDIRS[@]+"${ROSTER_GITDIRS[@]}"}; do
      echo "(allow file-write* $(sb_subpaths "$gd"))"
    done
    echo ';; 3. ... except the shared state inside them: the main checkout index/HEAD/config,'
    echo ';;    packed-refs, hooks, and the protected branch refs (a detached run never moves them)'
    # Roster git dirs are physical paths (pwd -P) = what the kernel compares.
    for gd in ${ROSTER_GITDIRS[@]+"${ROSTER_GITDIRS[@]}"}; do
      gd_re="$(sb_regex_escape "$gd")"
      echo "(deny file-write* (regex #\"^$gd_re/(index|HEAD|config|packed-refs)(\\.lock)?\$\") (regex #\"^$gd_re/refs/heads/($bases)(\\.lock)?\$\") (regex #\"^$gd_re/hooks(/|\$)\"))"
    done
    echo ';; 4. OWN WORKTREE LAST -- the one checkout this run may write'
    echo "(allow file-write* $(sb_subpaths "$WT_PATH"))"
  } > "$out" || return 1
  return 0
}

# write_profile_selfcheck <profile>: proves the rendered profile before a
# worker exists -- it compiles, the own worktree AND the object store are
# writable (else the run could not commit), and a write into EACH roster
# checkout is refused. Any failure = spawn refusal.
write_profile_selfcheck() {
  local prof="$1" t probe gd
  if ! sandbox-exec -f "$prof" /usr/bin/true 2>/dev/null; then
    echo "[dispatch] write profile does not compile: $prof -- refusing to spawn" >&2
    return 1
  fi
  probe="$WT_PATH/.write-probe-$$"
  if ! sandbox-exec -f "$prof" /bin/mkdir "$probe" 2>/dev/null; then
    echo "[dispatch] write profile self-check FAILED: own worktree $WT_PATH is not writable -- refusing to spawn" >&2
    return 1
  fi
  rmdir "$probe" 2>/dev/null || true
  gd="$(wt_common_dir "$WT_PATH" 2>/dev/null)" || gd=""
  if [[ -n "$gd" && -d "$gd/objects" ]]; then
    probe="$gd/objects/.write-probe-$$"
    if ! sandbox-exec -f "$prof" /bin/mkdir "$probe" 2>/dev/null; then
      echo "[dispatch] write profile self-check FAILED: object store $gd/objects is not writable (commits would fail) -- refusing to spawn" >&2
      return 1
    fi
    rmdir "$probe" 2>/dev/null || true
  fi
  for t in ${ROSTER_CHECKOUTS[@]+"${ROSTER_CHECKOUTS[@]}"}; do
    [[ -d "$t" ]] || continue
    probe="$t/.write-probe-$$"
    if sandbox-exec -f "$prof" /bin/mkdir "$probe" 2>/dev/null; then
      rmdir "$probe" 2>/dev/null || true
      echo "[dispatch] write profile self-check FAILED: sandbox allowed a write inside shared checkout $t -- refusing to spawn" >&2
      return 1
    fi
  done
  return 0
}

# Evidence rail (DGN-1504 (c)). The kernel stops THIS worker; it cannot stop
# a sibling session or a human, and the 2026-09-16 incident showed the real
# damage is ATTRIBUTION: a measurement taken while someone else's uncommitted
# edits sat in the shared tree could not be traced to any tree state. So the
# shared checkouts this run touches are snapshotted at spawn and at finalize
# (path, branch, HEAD, base-branch sha, dirty count, dirty digest), and any
# difference is recorded as checkout_drift -- never attributed to the worker,
# whose writes there are kernel-denied.
checkout_snapshot() {
  # args: out-file. One TSV line per shared checkout of WT_REPO / WORKSPACE.
  local out="$1" r top seen=" " br head basesha dirty ndirty digest
  : > "$out" || return 1
  for r in "$WT_REPO" "$WORKSPACE"; do
    # main checkout of that repo = the first `worktree` line
    top="$(git -C "$r" worktree list --porcelain 2>/dev/null | sed -n '1s/^worktree //p')"
    [[ -n "$top" && -d "$top" && "$seen" != *" $top "* ]] || continue
    seen="$seen$top "
    br="$(git -C "$top" symbolic-ref --short -q HEAD 2>/dev/null || echo DETACHED)"
    head="$(git -C "$top" rev-parse --short HEAD 2>/dev/null || echo none)"
    basesha="$(git -C "$top" rev-parse --short "refs/heads/${WT_BASE:-main}" 2>/dev/null || echo none)"
    # The dispatcher's OWN bookkeeping in the workspace (run dirs, ledger,
    # flags) moves on every run and is not drift.
    local spec=(-- . ':(exclude).telegram_bot' ':(exclude)product/auto-loop-ledger.md'
                ':(exclude).claude/.fable-exhausted' ':(exclude).claude/.cloud-exhausted')
    dirty="$(git -C "$top" status --porcelain --untracked-files=all "${spec[@]}" 2>/dev/null)"
    ndirty="$(printf '%s\n' "$dirty" | sed '/^$/d' | wc -l | tr -d ' ')"
    digest="$({ printf '%s\n' "$dirty"; git -C "$top" diff HEAD "${spec[@]}" 2>/dev/null; } | cksum | cut -d' ' -f1)"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$top" "$br" "$head" "$basesha" "$ndirty" "$digest" >> "$out"
  done
  return 0
}

# checkout_drift <start.tsv> <end.tsv> -> one "; "-joined line on stdout
# naming what moved in each shared checkout ("" = nothing moved).
checkout_drift() {
  [[ -f "$1" && -f "$2" ]] || return 0
  awk -F'\t' -v base="${WT_BASE:-main}" '
    NR==FNR { b[$1]=$2; h[$1]=$3; s[$1]=$4; n[$1]=$5; d[$1]=$6; next }
    ($1 in b) {
      m=""
      if (b[$1]!=$2) m=m sprintf(" branch %s->%s", b[$1], $2)
      if (h[$1]!=$3) m=m sprintf(" HEAD %s->%s", h[$1], $3)
      if (s[$1]!=$4) m=m sprintf(" %s %s->%s", base, s[$1], $4)
      if (d[$1]!=$6) m=m sprintf(" uncommitted %s->%s (content changed)", n[$1], $5)
      if (m!="") out=out (out==""?"":"; ") $1 ":" m
    }
    END { printf "%s", out }' "$1" "$2"
}

# One spawn line when the shared checkout of the target repo is NOT on the
# base branch or is dirty. The run itself is unaffected (it is cut from
# refs/heads/<base>, not from HEAD), but anything measured IN that checkout
# right now measures someone else's tree state -- say so, once, on record.
checkout_spawn_note() {
  local line top br ndirty
  line="$(head -1 "$1" 2>/dev/null)" || return 0
  [[ -n "$line" ]] || return 0
  top="$(cut -f1 <<<"$line")"; br="$(cut -f2 <<<"$line")"; ndirty="$(cut -f5 <<<"$line")"
  if [[ "$br" != "${WT_BASE:-main}" || "$ndirty" != "0" ]]; then
    printf 'shared checkout %s is on %s (base %s) with %s uncommitted path(s) -- this run is cut from refs/heads/%s and cannot write there; measurements taken in that checkout are not attributable to this run\n' \
      "$top" "$br" "${WT_BASE:-main}" "$ndirty" "${WT_BASE:-main}"
  fi
}

# plan_dir_selfcheck <profile>: the plan lane's one writable place must be
# writable (else the planner could not deliver). No plan lane -> no-op.
plan_dir_selfcheck() {
  [[ -n "$PLAN_DIR" ]] || return 0
  local probe="$PLAN_DIR/.plan-probe-$$"
  if ! sandbox-exec -f "$1" /bin/mkdir "$probe" 2>/dev/null; then
    echo "[dispatch] observe profile self-check FAILED: plan dir $PLAN_DIR is not writable -- refusing to spawn" >&2
    return 1
  fi
  rmdir "$probe" 2>/dev/null || true
  return 0
}

# select_tool_policy -> globals TOOL_ARGS (claude flags) and WRAP (command
# prefix). Worktree mode keeps the pre-DGN-1191 tool policy and, since
# DGN-1504, runs inside the rendered write profile (WRITE-MODE ISOLATION).
select_tool_policy() {
  TOOL_ARGS=()
  WRAP=()
  if [[ -n "$WT_PATH" ]]; then
    if [[ -z "$WRITE_PROFILE" || ! -f "$WRITE_PROFILE" ]]; then
      echo "[dispatch-worker] write mode without a rendered write profile -- refusing to run unconfined (DGN-1504)" >&2
      return 1
    fi
    write_sandbox_available || return 1
    WRAP=(sandbox-exec -f "$WRITE_PROFILE")
    TOOL_ARGS=(--permission-mode bypassPermissions --disallowedTools "Task,Agent")
    return 0
  fi
  if [[ -z "$SANDBOX_PROFILE" || ! -f "$SANDBOX_PROFILE" || -z "$SESSION_ID" ]]; then
    echo "[dispatch-worker] observe mode without a rendered sandbox profile -- refusing to run" >&2
    return 1
  fi
  observe_sandbox_available || return 1
  WRAP=(sandbox-exec -f "$SANDBOX_PROFILE")
  TOOL_ARGS=(--permission-mode default --allowedTools "Bash" \
    --disallowedTools "Task,Agent,Write,Edit,NotebookEdit" --session-id "$SESSION_ID")
  return 0
}

# select_codex_tool_policy -> globals CODEX_ARGS (codex flags) and WRAP
# Historical policy (2026-09-13; superseded for observe below):
# (command prefix, unused for codex -- empty). Worktree mode ONLY: codex
# observe mode is refused earlier (validate_spawn_args, DGN-1444) because
# parity with DGN-1191's guarantee cannot be established (tested: codex's
# own read-only sandbox does not deny reading ~/.ssh; wrapping it in our
# seatbelt profile hangs the process). --sandbox workspace-write restricts
# codex to its cwd (the worktree) plus TMPDIR -- measured 2026-09-13 to be a
# real kernel-enforced boundary, at least as contained as claude's current
# worktree-mode guarantee (which has no OS sandbox of its own there at all).
#
# GIT-COMMON-DIR CARVE-OUT (found via a real E2E run, 2026-09-13): a `git
# worktree add` checkout's OWN .git is just a pointer file -- the real index,
# HEAD and objects live in the ORIGINATING repo's .git/worktrees/<slug>/,
# which is OUTSIDE the worktree directory codex's workspace-write covers.
# Reproduced directly: `git commit` inside a codex-sandboxed worktree failed
# with "Unable to create .../.git/worktrees/<slug>/index.lock: Operation not
# permitted" until that directory was added via --add-dir. Without this,
# every codex worktree dispatch would silently produce zero commits no
# matter what the model did -- not a model failure, a missing sandbox grant.
select_codex_tool_policy() {
  CODEX_ARGS=()
  WRAP=()
  if [[ -n "$WT_PATH" ]]; then
    CODEX_ARGS=(--sandbox workspace-write --skip-git-repo-check)
    local common_dir=""
    common_dir="$(git -C "$WT_PATH" rev-parse --git-common-dir 2>/dev/null)" || common_dir=""
    if [[ -n "$common_dir" ]]; then
      case "$common_dir" in
        /*) : ;;
        *) common_dir="$WT_PATH/$common_dir" ;;
      esac
      common_dir="$(cd "$common_dir" 2>/dev/null && pwd)" || common_dir=""
    fi
    if [[ -n "$common_dir" ]]; then
      CODEX_ARGS+=(--add-dir "$common_dir")
    else
      echo "[dispatch-worker] warning: could not resolve git-common-dir for $WT_PATH -- codex git commits may fail under workspace-write (non-fatal, continuing)" >&2
    fi
    return 0
  fi
  if [[ -z "$SANDBOX_PROFILE" || ! -f "$SANDBOX_PROFILE" || -z "$SESSION_ID" ]]; then
    echo "[dispatch-worker] codex observe mode without a rendered sandbox profile -- refusing to run" >&2
    return 1
  fi
  observe_sandbox_available || return 1
  WRAP=(sandbox-exec -f "$SANDBOX_PROFILE")
  # The outer kernel seatbelt is the boundary. An internal read-only sandbox
  # nests seatbelts and breaks legitimate reads (2026-09-14 remeasurement B2).
  CODEX_ARGS=(--sandbox danger-full-access --skip-git-repo-check)
  return 0
}

# ---------------------------------------------------------------------------
# START NOTICE -- one owner push when work BEGINS, batched per burst.
# Contract + the owner's ruling: see START NOTICE in the header.
#
# WHY A TIME WINDOW AND NOT A KEY. Three joins were weighed against what this
# code can actually support:
#   (1) caller-passed batch id -- REJECTED. A lone dispatch is a legitimate
#       call, so an absent id reads exactly like a correct one and no refusal
#       can be armed at the spawn gate. An unenforceable field degrades to
#       per-dispatch notices the first time the caller forgets it, silently,
#       which is the failure mode this dispatcher has already paid for once
#       (DISPLAY NAME above: the field had to be made REFUSING to survive).
#   (2) coalescing keyed on the calling session -- REJECTED. Parallel
#       dispatches are independent processes spawned by the calling session,
#       and the only session key available is a runtime-private environment
#       variable of one particular CLI (a provider detail this rail must not
#       bind to -- cf. the RUNTIME seam above). Worse, it does not remove the
#       need for a window: the same session dispatches all day long, so a
#       session key alone would join two bursts hours apart.
#   (3) SENDER-SIDE WINDOW -- chosen. It needs no cooperation from the caller
#       and no runtime identity: the first spawn of a burst claims the flusher,
#       later spawns just find the claim taken and join the queue.
#
# THE WINDOW VALUE is measured, not taste. Gaps between consecutive spawns in
# this estate's own dispatch history (630 runs, run-dir timestamps):
#   0-6s: 63 gaps  |  7-13s: ZERO gaps  |  14s and up: the rest (565 gaps).
# Bursts land inside 6s and unrelated dispatches never arrived closer than
# 14s, so the cut sits in an empty valley; 8s is inside it with margin on both
# sides. Replaying the history at 8s joins 50 of the 52 multi-spawn bursts a
# 20s window would find, at a quarter of the delay.
# COST OF THE DELAY: the notice reaches the owner up to 8s after the work
# actually starts (the window is anchored on the burst's FIRST spawn, so the
# delay is bounded, not sliding). Acceptable on the owner's own terms -- the
# defect he named is asking "did it start?" minutes later with no signal at
# all; 8 seconds is invisible against that, and the alternative doubles the
# day's message count.
# The env override is for tests and for a later re-measurement; the default is
# the measured value.
# ---------------------------------------------------------------------------
START_NOTICE_DIR="$DISPATCH_DIR/start-notice"
START_NOTICE_QUEUE="$START_NOTICE_DIR/queue"
START_NOTICE_LOCK="$START_NOTICE_DIR/.lock"
START_NOTICE_CLAIM="$START_NOTICE_DIR/.flusher"
START_NOTICE_WINDOW="${DOGANY_START_NOTICE_WINDOW:-8}"
# DGN-1727: upper bound (seconds, ONE shared deadline per batch) the flusher
# waits for the claude-code runs in a batch to report their model id before
# rendering, so the START line can show "Claude Opus 5.5" like the END line. Past the
# bound the line goes out with the bare name -- never a guessed version.
START_NOTICE_MODEL_WAIT="${DOGANY_START_NOTICE_MODEL_WAIT:-45}"
case "$START_NOTICE_MODEL_WAIT" in
  ''|*[!0-9]*) START_NOTICE_MODEL_WAIT=45 ;;
esac
case "$START_NOTICE_WINDOW" in
  ''|*[!0-9]*)
    echo "[dispatch] DOGANY_START_NOTICE_WINDOW must be a whole number of seconds (got '$START_NOTICE_WINDOW') -- using 8" >&2
    START_NOTICE_WINDOW=8 ;;
esac

# rc 0 = this name is safe to put in an owner-facing message.
# The send rail (push.sh) refuses any body carrying an execution identifier,
# and refusing THERE would take the whole burst's notice down with one badly
# named job. So the same detector is consulted HERE, at the one moment the
# name can still be corrected, and the offending name is kept out of the
# queue instead. Detector unavailable (no interpreter / no bridge module) ->
# rc 0, the same fail-open push.sh itself takes: a missing detector must not
# cost the owner a notice.
start_notice_name_is_clean() {
  local hits="" rc=0
  hits="$(printf '%s' "$1" | python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from bridge.machine_gate import find_execution_identifiers
for kind, sample in find_execution_identifiers(sys.stdin.read()):
    sys.stdout.write("%s: %s\n" % (kind, sample[:60]))
' "$WORKSPACE" 2>/dev/null)" || rc=$?
  if [[ $rc -ne 0 ]]; then
    return 0
  fi
  if [[ -n "$hits" ]]; then
    START_NOTICE_REJECT="$hits"
    return 1
  fi
  return 0
}

# Detach the flusher into its OWN session (python3 fork -> setsid -> exec, the
# same escape do_spawn uses): a sender that dies with the calling session would
# lose the notice on exactly the /stop that this whole file exists to survive.
start_notice_spawn_flusher() {
  local py="python3" self
  if [[ -n "${BRIDGE_PYTHON:-}" ]]; then
    py="$BRIDGE_PYTHON"
  fi
  command -v "$py" >/dev/null 2>&1 || return 1
  self="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
  "$py" - "$START_NOTICE_DIR/flusher.log" "$self" --start-notice-flush <<'PYEOF' >/dev/null
import os, sys
log_path, target = sys.argv[1], sys.argv[2]
if os.fork() > 0:
    os._exit(0)
os.setsid()
devnull = os.open(os.devnull, os.O_RDONLY)
log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
os.dup2(devnull, 0)
os.dup2(log, 1)
os.dup2(log, 2)
os.close(devnull)
os.close(log)
os.execv(target, [target] + sys.argv[3:])
PYEOF
}

# start_notice_arm <owner-facing name> -- queue this run and make sure exactly
# one flusher is pending. Called ONLY after the worker is detached.
# Never fails the spawn: every failure here is reported and swallowed (the work
# is already running; losing its notice must not look like a failed dispatch).
# Queue line = "<Model>\t<HH:MM>\t<name>": the flusher runs with NO run
# context (it must never source a run.env), so everything the designed item
# line renders is resolved HERE, where RUNTIME/MODEL are still in scope, and
# travels through the queue. Tabs are safe field separators on this line:
# the model display column is ASCII DATA and the name gate below already
# refuses newlines; the name rides in the LAST field so even a pathological
# tab inside it stays inside the name slot.
start_notice_arm() {
  local name="$1" claim_age now mtime spawn=0
  if [[ -z "$name" ]]; then
    echo "[dispatch] start notice skipped: no owner-facing name resolved" >&2
    return 0
  fi
  START_NOTICE_REJECT=""
  if ! start_notice_name_is_clean "$name"; then
    echo "[dispatch] start notice SKIPPED for this run: its owner-facing name carries an execution identifier, which the send rail refuses (and one bad name would take the whole batch's notice with it):" >&2
    printf '%s\n' "$START_NOTICE_REJECT" | sed 's/^/[dispatch]   /' >&2
    echo "[dispatch]   fix: re-dispatch with a --display-name written for a human (no run ids, hashes, paths or exit codes). The run itself is unaffected." >&2
    return 0
  fi
  if ! mkdir -p "$START_NOTICE_DIR" 2>/dev/null; then
    echo "[dispatch] start notice skipped: cannot create $START_NOTICE_DIR" >&2
    return 0
  fi
  if ! dir_lock "$START_NOTICE_LOCK"; then
    echo "[dispatch] start notice skipped: queue lock unavailable" >&2
    return 0
  fi
  # DGN-1727: a claude-code run has no version yet (the id arrives with the
  # first stream event, after this line is queued), so it queues a pointer to
  # its run dir instead of a baked label; the flusher resolves the label after
  # a bounded wait. "@run\t<run dir>\t<alias>\t<HH:MM>\t<name>". Everything
  # else (codex, session background jobs) keeps the baked three-field line.
  if [[ -z "$MODEL_SLOT" && "$RUNTIME" == "claude-code" && -n "${RUNDIR:-}" \
        && "$RUNDIR" != *$'\t'* ]]; then
    printf '@run\t%s\t%s\t%s\t%s\n' "$RUNDIR" "$MODEL" "$(date +%H:%M)" "$name" \
      >> "$START_NOTICE_QUEUE"
  elif [[ -n "$MODEL_SLOT" ]]; then
    # Session background job: description only, no model slot (DGN-1727).
    printf '@bare\t%s\t%s\n' "$(date +%H:%M)" "$name" >> "$START_NOTICE_QUEUE"
  else
    printf '%s\t%s\t%s\n' "$(owner_model_display_cap)" "$(date +%H:%M)" "$name" \
      >> "$START_NOTICE_QUEUE"
  fi
  # A claim left behind by a flusher that never ran (launcher killed between
  # the claim and the detach, host went down mid-window) would suppress every
  # later notice. Steal it once it is older than the window plus a grace --
  # the queue survives, so the stolen burst's names go out with the next one.
  if [[ -d "$START_NOTICE_CLAIM" ]]; then
    now="$(date +%s)"
    # GNU stat first: GNU `stat -f` means filesystem mode and prints a text
    # block with rc 0; BSD stat rejects -c and falls through to -f %m.
    mtime="$(stat -c %Y "$START_NOTICE_CLAIM" 2>/dev/null || stat -f %m "$START_NOTICE_CLAIM" 2>/dev/null || echo "$now")"
    claim_age=$(( now - mtime ))
    if [[ $claim_age -gt $(( START_NOTICE_WINDOW + 60 )) ]]; then
      echo "[dispatch] start notice: stealing a stale flusher claim (${claim_age}s old)" >&2
      rmdir "$START_NOTICE_CLAIM" 2>/dev/null || true
    fi
  fi
  # Claim and queue are decided under ONE lock, and the flusher takes the queue
  # and releases the claim under that same lock. So a spawn either joins a
  # pending flush or starts its own -- it can never append to a queue that
  # nobody will send, and two flushers can never take the same names.
  if mkdir "$START_NOTICE_CLAIM" 2>/dev/null; then
    spawn=1
  fi
  dir_unlock "$START_NOTICE_LOCK"
  if [[ $spawn -eq 1 ]]; then
    if start_notice_spawn_flusher; then
      echo "[dispatch]   start notice: owner push in ${START_NOTICE_WINDOW}s (batched; dispatches spawned within the window share one message)"
    else
      # No sender was armed -> release the claim so the NEXT spawn can arm one,
      # and take this burst's names with it.
      if dir_lock "$START_NOTICE_LOCK"; then
        rmdir "$START_NOTICE_CLAIM" 2>/dev/null || true
        dir_unlock "$START_NOTICE_LOCK"
      fi
      echo "[dispatch] start notice: could not detach the sender (no python3) -- notice deferred to the next dispatch" >&2
    fi
  else
    echo "[dispatch]   start notice: joined the pending batch (one message for this burst)"
  fi
  return 0
}

# The detached sender. Waits out the window, takes the whole queue, sends ONE
# push. Runs with no run context at all: it must never touch a run dir, a
# ledger row or the active count.
do_start_notice_flush() {
  sleep "$START_NOTICE_WINDOW"
  local items="" n=0 text
  mkdir -p "$START_NOTICE_DIR" 2>/dev/null || true
  if ! dir_lock "$START_NOTICE_LOCK"; then
    echo "[dispatch-start-notice] queue lock unavailable -- leaving the queue for the next dispatch" >&2
    return 0
  fi
  if [[ -f "$START_NOTICE_QUEUE" ]]; then
    items="$(cat "$START_NOTICE_QUEUE" 2>/dev/null || true)"
    : > "$START_NOTICE_QUEUE"
  fi
  # Claim released inside the lock, together with the queue take (see the
  # ordering note in start_notice_arm): a spawn that appends after this point
  # finds no claim and arms its own sender.
  rmdir "$START_NOTICE_CLAIM" 2>/dev/null || true
  dir_unlock "$START_NOTICE_LOCK"
  n="$(printf '%s\n' "$items" | grep -c '[^[:space:]]' || true)"
  if [[ -z "$items" || "${n:-0}" -eq 0 ]]; then
    return 0
  fi
  # Owner-approved block shape (identity Response workflow, "Completion
  # signal" -- the designed subagent dispatch START block; dec-094 copy gate
  # satisfied by that design, approved and recorded 2026-09-18, which the
  # head session used to retype by hand on every dispatch until this change
  # machine-emitted it). Reproduced EXACTLY, nothing added: title line
  # `▶️ <background-term> 작업 시작` -- the term is background_term()
  # (instance conf; neutral default 백그라운드), NEVER a fixed string, for
  # EVERY burst: dispatch runs, shell jobs or any mix (owner-locked
  # 2026-09-28, DGN-1767) -- with " (N건)" appended ONLY when N>1; then one line per run
  # `- <Model>: <job name> <HH:MM>` where <Model> is the display from
  # lib/model_display.py: the observed version when the run reported its id
  # inside the bounded wait below (DGN-1727), else the bare name -- a version
  # is never guessed (DGN-1227) -- and <job name> is the owner-facing
  # dispatch name, never the machine label. The glyph stays a THIRD one on
  # purpose: ✅ means finished and ⚠️ means a problem, so reusing either for
  # "started" would overload a symbol the owner already reads as a verdict.
  # Queue line format (written by start_notice_arm):
  # "<Model>\t<HH:MM>\t<name>". A tab-less line is a leftover from the
  # pre-block queue format (name only, mid-update burst): rendered as a bare
  # name rather than dropped -- losing a notice over a format migration
  # would recreate the very silence this rail closes.
  # owner-approved 2026-09-28: every start notice titles with background_term (DGN-1767)
  text="▶️ $(background_term) 작업 시작"
  if [[ "$n" -gt 1 ]]; then
    text="$(printf '%s (%d건)' "$text" "$n")"
  fi
  # DGN-1727: resolve the pending claude-code labels first, ONE bounded wait
  # for the whole batch (the window already elapsed; this only adds time when
  # a run has not reported its model id yet).
  local -a wait_dirs=()
  local qline qrest qdir
  while IFS= read -r qline; do
    [[ "$qline" == "@run"$'\t'* ]] || continue
    qrest="${qline#@run$'\t'}"
    qdir="${qrest%%$'\t'*}"
    [[ -n "$qdir" ]] && wait_dirs+=("$qdir")
  done <<< "$items"
  local observed_map=""
  if [[ ${#wait_dirs[@]} -gt 0 && -r "$SCRIPT_DIR/lib/model_display.py" ]]; then
    observed_map="$(python3 "$SCRIPT_DIR/lib/model_display.py" --wait-observed \
                      "$START_NOTICE_MODEL_WAIT" "${wait_dirs[@]}" 2>/dev/null)" || observed_map=""
  fi
  local qmodel qtime qname qalias qobs label
  while IFS= read -r qline; do
    if [[ "$qline" == "@run"$'\t'* ]]; then
      qrest="${qline#@run$'\t'}"
      IFS=$'\t' read -r qdir qalias qtime qname <<< "$qrest"
      qobs="$(awk -F'\t' -v d="$qdir" '$1 == d { print $2; exit }' <<< "$observed_map")"
      label=""
      if [[ -r "$SCRIPT_DIR/lib/model_display.py" ]]; then
        label="$(python3 "$SCRIPT_DIR/lib/model_display.py" --tsv "$MODEL_DISPLAY_BINDINGS" \
                   claude-code "$qalias" "$qobs" 2>/dev/null)" || label=""
      fi
      [[ -n "$label" ]] || label="$qalias"
      text="${text}"$'\n'"- ${label}: ${qname} ${qtime}"
      continue
    fi
    IFS=$'\t' read -r qmodel qtime qname <<< "$qline"
    [[ -z "${qmodel}${qtime}${qname}" ]] && continue
    if [[ "$qmodel" == "@bare" ]]; then
      text="${text}"$'\n'"- ${qname} ${qtime}"
      continue
    fi
    if [[ -z "$qtime" && -z "$qname" ]]; then
      text="${text}"$'\n'"- ${qmodel}"
    else
      text="${text}"$'\n'"- ${qmodel}: ${qname} ${qtime}"
    fi
  done <<< "$items"
  if "$PUSH_SH" --text "$text" --audience owner; then
    echo "[dispatch-start-notice] sent (${n} item(s))"
  else
    echo "[dispatch-start-notice] push FAILED (rc=$?) -- the dispatches themselves are unaffected; body was: $text" >&2
  fi
  return 0
}

# DGN-1691 phase 2 (header, SESSION BACKGROUND JOBS). Both modes refuse a
# name or slot that would break the tab-separated queue line or the one-line
# item, and otherwise hand straight to the dispatch rails.
bg_notice_args_ok() {
  local v
  for v in "$DISPLAY_NAME" "$MODEL_SLOT"; do
    if [[ -z "${v//[[:space:]]/}" ]]; then
      echo "[dispatch-bg-notice] --display-name and --model-slot are both required" >&2
      return 1
    fi
    case "$v" in
      *$'\t'*|*$'\n'*|*$'\r'*|*'|'*)
        echo "[dispatch-bg-notice] no tab, newline, carriage return or '|' in --display-name / --model-slot" >&2
        return 1 ;;
    esac
  done
  return 0
}

do_bg_start_notice() {
  bg_notice_args_ok || return 1
  start_notice_arm "$DISPLAY_NAME"
}

do_bg_end_notice() {
  bg_notice_args_ok || return 1
  local result text
  # Task status -> the finalize_run result vocabulary: a stopped job takes
  # the interruption line, anything but completed takes the failure line.
  case "$BG_RESULT" in
    completed) result="completed" ;;
    killed|stopped|canceled|cancelled) result="canceled" ;;
    *) result="error" ;;
  esac
  case "$BG_ELAPSED_SECS" in ''|*[!0-9]*) BG_ELAPSED_SECS="" ;; esac
  case "$BG_END_CLOCK" in [0-9][0-9]:[0-9][0-9]) ;; *) BG_END_CLOCK="$(date +%H:%M)" ;; esac
  text="$(end_notice_text "$result" "$DISPLAY_NAME" "$BG_END_CLOCK" "$BG_ELAPSED_SECS")"
  # A job that ends inside the start-notice join window would otherwise
  # reach the owner before its own START (--not-before: epoch; wait capped
  # at one minute so a bad value can never park the sender).
  local wait_s=0
  case "$BG_NOT_BEFORE" in
    ''|*[!0-9]*) ;;
    *) wait_s=$(( BG_NOT_BEFORE - $(date +%s) )) ;;
  esac
  if [[ $wait_s -gt 0 ]]; then
    [[ $wait_s -gt 60 ]] && wait_s=60
    sleep "$wait_s"
  fi
  if "$PUSH_SH" --text "$text" --audience owner; then
    echo "[dispatch-bg-notice] end notice sent"
  else
    echo "[dispatch-bg-notice] end notice push FAILED -- body was: $text" >&2
    return 1
  fi
}

do_spawn() {
  validate_access_mode || exit 1
  validate_spawn_args || exit 1
  # DGN-1742: an explicit local runtime on a cloud-eligible run needs a reason.
  explicit_runtime_cloud_gate || exit 2
  validate_live_targets || exit 1
  # CLOUD-FIRST: a converted run is re-validated as the cloud run it now is.
  local cfs=0
  cloud_first_select || cfs=$?
  if [[ $cfs -eq 0 ]]; then
    validate_access_mode || exit 1
    validate_spawn_args || exit 1
    validate_live_targets || exit 1
  elif [[ $cfs -eq 2 ]]; then
    exit 2
  fi
  check_prompt_size || exit 1
  if [[ "$RUNTIME" == "cloud" ]]; then
    cloud_preflight || exit 2
  fi
  # DGN-1218: history first, verdict second -- when the gate below refuses,
  # the dispatcher re-decides with the calibration data already on screen.
  print_calibration || true
  select_default_runtime || exit 2
  bottleneck_gate || exit 2
  # Observe mode needs the kernel sandbox; refuse BEFORE any slot is held.
  # A cloud run executes nothing locally but the watcher -- no sandbox.
  if [[ -z "$WT_SLUG" && "$RUNTIME" != "cloud" ]]; then
    observe_sandbox_available || exit 2
  fi
  # DGN-1504: a claude-code write run is confined to its own worktree by the
  # kernel; refuse before any slot is held when that cage cannot be built.
  if [[ -n "$WT_SLUG" && "$RUNTIME" == "claude-code" ]]; then
    write_sandbox_available || exit 2
  fi

  local rc=0
  run_gates || rc=$?
  if [[ $rc -ne 0 ]]; then
    exit 2
  fi
  # DGN-1770: RUNTIME/MODEL are final here (run_gates may have flipped them).
  resolve_effort
  # From here on the active-count slot is held: every failure path must decr.

  # Unique runid (self-grill G22: same-second collision would share a rundir).
  local tries=0
  while :; do
    RUNID="dsp-$(date +%Y%m%d-%H%M%S)-$(printf '%04d' $(( RANDOM % 10000 )))"
    RUNDIR="$DISPATCH_DIR/$RUNID"
    if mkdir "$RUNDIR" 2>/dev/null; then
      break
    fi
    tries=$(( tries + 1 ))
    if [[ $tries -ge 5 ]]; then
      echo "[dispatch] could not allocate a unique run dir under $DISPATCH_DIR" >&2
      active_count_decr
      exit 1
    fi
    sleep 1
  done

  WT_PATH=""
  BRANCH=""
  if [[ -n "$WT_SLUG" ]]; then
    local wrc=0
    provision_worktree || wrc=$?
    if [[ $wrc -ne 0 ]]; then
      active_count_decr
      rm -rf "$RUNDIR"
      exit 3
    fi
    LEDGER_BRANCH="$BRANCH"
    WORKDIR="$WT_PATH"
    # DGN-1444 hook-trust gate, worktree stage: the run-binding proof against
    # the exact cwd the codex worker gets (the repo stage in run_gates()
    # could only read WT_REPO -- this path did not exist yet). Refusal here
    # unwinds exactly like a provisioning failure; the verdict is recorded
    # in RUNDIR like the freshness gate's (DGN-1230), so no later audit has
    # to guess whether the gate ran.
    if [[ "$RUNTIME" == "codex" ]]; then
      if ! codex_hook_gate "$WT_PATH" worktree; then
        # DGN-1470 defect 4: the verdict survives the cleanup below -- the
        # decision record lives OUTSIDE the rundir, so removing the refused
        # worktree and rundir no longer erases the only evidence a refusal
        # happened. No runtime switch at this stage (the repo-stage answer
        # already passed; a worktree-stage flip would re-decide tier/slot
        # after provisioning -- blocked, on record, is the honest verdict).
        record_runtime_decision blocked codex_hooks_unavailable_worktree "worktree stage: $WT_PATH (runid=$RUNID)" || true
        active_count_decr
        git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
        rm -rf "$RUNDIR"
        exit 2
      fi
      echo "$CODEX_HOOK_NOTE path=$WT_PATH" > "$RUNDIR/codex-hooks.txt"
    fi
    # DGN-1504 evidence rail: the shared checkouts' state at spawn.
    checkout_snapshot "$RUNDIR/checkouts-start.tsv" || true
    CHECKOUT_SPAWN_NOTE="$(checkout_spawn_note "$RUNDIR/checkouts-start.tsv")"
    # DGN-1504 kernel boundary (claude-code; codex runs its own sandbox).
    # RUNTIME is final here (run_gates may have flipped it), so this is the
    # authoritative check -- a render/self-check defect unwinds like a
    # provisioning failure and never becomes an unconfined worker.
    if [[ "$RUNTIME" == "claude-code" ]]; then
      WRITE_PROFILE="$RUNDIR/write.sb"
      write_roster
      if ! write_sandbox_available \
         || ! render_write_profile "$WRITE_PROFILE" \
         || ! write_profile_selfcheck "$WRITE_PROFILE"; then
        active_count_decr
        git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
        rm -rf "$RUNDIR"
        exit 2
      fi
      WRITE_ROSTER_NOTE="${#ROSTER_CHECKOUTS[@]} shared checkouts / ${#ROSTER_GITDIRS[@]} git dirs closed"
    fi
  elif [[ "$RUNTIME" == "cloud" ]]; then
    # DGN-1726: the ledger key IS the result branch the watcher waits for,
    # so the work board row and the remote product share one name.
    BRANCH="claude/$LABEL"
    LEDGER_BRANCH="$BRANCH"
    WORKDIR="$REPO_PATH"
  else
    LEDGER_BRANCH="dsp/$RUNID"
    # --repo moves ONLY the worker cwd; every artifact path stays
    # WORKSPACE-anchored (see header).
    WORKDIR="${REPO_PATH:-$WORKSPACE}"
    # Observe mode: render the seatbelt profile into RUNDIR (under WORKSPACE,
    # so the sandboxed worker cannot edit its own cage) and prove it holds
    # against THIS run's targets before anything is spawned. A render defect
    # is a spawn refusal, never a silently open worker.
    SANDBOX_PROFILE="$RUNDIR/observe.sb"
    # The worker's CLI session id is minted HERE so the profile can open
    # exactly this session's ~/.claude state and nothing of any other session.
    SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"
    # DGN-1458 plan lane: one writable dir inside RUNDIR, proven writable,
    # while every live target is proven denied like any other target.
    local plan_targets=()
    if [[ -n "$LIVE_TARGETS" ]]; then
      PLAN_DIR="$RUNDIR/plan"
      mkdir -p "$PLAN_DIR"
      while IFS= read -r _lt; do plan_targets+=("$_lt"); done <<<"$LIVE_TARGETS"
    fi
    if ! render_observe_profile "$SANDBOX_PROFILE" "$WORKDIR" "$SESSION_ID" \
       || ! observe_profile_selfcheck "$SANDBOX_PROFILE" "$WORKDIR" "$WORKSPACE" ${plan_targets[@]+"${plan_targets[@]}"} \
       || ! plan_dir_selfcheck "$SANDBOX_PROFILE"; then
      active_count_decr
      rm -rf "$RUNDIR"
      exit 2
    fi
  fi

  # Snapshot the prompt (caller file may move/change after detach). A cloud
  # task carries its base checkout (cloud_task_preamble) and return contract
  # (cloud_task_footer).
  if [[ "$RUNTIME" == "cloud" ]]; then
    { cloud_task_preamble; cat "$TASK_FILE"; cloud_task_footer; } > "$RUNDIR/prompt.md"
  else
    cp "$TASK_FILE" "$RUNDIR/prompt.md"
  fi

  # Serialized run parameters for the worker (printf %q -- quoting-safe).
  {
    printf 'RUNID=%q\n'         "$RUNID"
    printf 'LABEL=%q\n'         "$LABEL"
    printf 'DISPLAY_NAME=%q\n'  "$DISPLAY_NAME"
    printf 'TICKET=%q\n'        "$TICKET"
    printf 'MODEL=%q\n'         "$MODEL"
    printf 'MODEL_SOURCE=%q\n'  "$MODEL_SOURCE"
    printf 'REQUESTED_MODEL=%q\n' "$REQUESTED_MODEL"
    printf 'CODEX_MODEL=%q\n' "$CODEX_MODEL"
    printf 'EFFORT=%q\n'        "$EFFORT"
    printf 'EFFORT_SOURCE=%q\n' "$EFFORT_SOURCE"
    printf 'BOTTLENECK=%q\n'    "$BOTTLENECK"
    printf 'BOTTLENECK_WHY=%q\n' "$BOTTLENECK_WHY"
    printf 'TIER_WHY=%q\n'      "$TIER_WHY"
    printf 'TIER_WHY_USAGE=%q\n' "$TIER_WHY_USAGE"
    printf 'RUNTIME=%q\n'       "$RUNTIME"
    printf 'REQUESTED_RUNTIME=%q\n' "$REQUESTED_RUNTIME"
    printf 'RUNTIME_SELECTION_SOURCE=%q\n' "$RUNTIME_SELECTION_SOURCE"
    printf 'RUNTIME_SELECTION_REASON=%q\n' "$RUNTIME_SELECTION_REASON"
    printf 'RUNTIME_SELECTION_DETAIL=%q\n' "$RUNTIME_SELECTION_DETAIL"
    printf 'RUNTIME_FALLBACK_DIRECTION=%q\n' "$RUNTIME_FALLBACK_DIRECTION"
    printf 'RUNTIME_FALLBACK_NOTE=%q\n' "$RUNTIME_FALLBACK_NOTE"
    printf 'DECISION_ID=%q\n'   "$DECISION_ID"
    printf 'CODEX_QUOTA_NOTE=%q\n' "$CODEX_QUOTA_NOTE"
    printf 'CODEX_HOOK_NOTE=%q\n' "$CODEX_HOOK_NOTE"
    printf 'CODEX_ADAPTER_ROOT=%q\n' "$CODEX_ADAPTER_ROOT"
    printf 'CODEX_UNGUARDED_WHY=%q\n' "$CODEX_UNGUARDED_WHY"
    printf 'LOCAL_WHY=%q\n'     "$LOCAL_WHY"
    printf 'ACCESS_MODE=%q\n'   "$ACCESS_MODE"
    printf 'WT_SLUG=%q\n'       "$WT_SLUG"
    printf 'REPO_PATH=%q\n'     "$REPO_PATH"
    printf 'WT_REPO=%q\n'       "$WT_REPO"
    printf 'WT_BASE=%q\n'       "$WT_BASE"
    printf 'CLOUD_ENV_ID=%q\n'   "$CLOUD_ENV_ID"
    printf 'CLOUD_REPO_URL=%q\n' "$CLOUD_REPO_URL"
    printf 'WT_PATH=%q\n'       "$WT_PATH"
    printf 'BRANCH=%q\n'        "$BRANCH"
    printf 'LEDGER_BRANCH=%q\n' "$LEDGER_BRANCH"
    printf 'TIMEOUT_MIN=%q\n'   "$TIMEOUT_MIN"
    printf 'NOTIFY=%q\n'        "$NOTIFY"
    printf 'CANCEL_BY=%q\n'     "$CANCEL_BY"
    printf 'WORKDIR=%q\n'       "$WORKDIR"
    printf 'SANDBOX_PROFILE=%q\n' "$SANDBOX_PROFILE"
    printf 'WRITE_PROFILE=%q\n' "$WRITE_PROFILE"
    printf 'LIVE_TARGETS=%q\n'  "$LIVE_TARGETS"
    printf 'PLAN_DIR=%q\n'      "$PLAN_DIR"
    printf 'SESSION_ID=%q\n'    "$SESSION_ID"
    # DGN-1726: spawn epoch -- the workbench row's "HH:MM (N분~)" start.
    printf 'SPAWN_EPOCH=%q\n'   "$(date +%s)"
  } > "$RUNDIR/run.env"

  # Ledger running row FIRST (owner registration precedes the process, same
  # ordering as autonomous-loop). Note format is parser-compatible:
  # status-footer.py _LEDGER_MODEL_RE reads "model=<name>" from the note.
  if ! ensure_ledger; then
    active_count_decr
    if [[ -n "$WT_PATH" ]]; then
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
    fi
    rm -rf "$RUNDIR"
    exit 1
  fi
  local item="$LABEL"
  [[ -n "$TICKET" ]] && item="$LABEL [$TICKET]"
  # OWNER SURFACE 1 -- the pinned work board. It renders the ledger's item
  # column verbatim (status-footer.py _collect_ledger_running), so the item
  # column IS an owner-facing string and takes the owner-facing name. `item`
  # above stays the machine-labelled one for the machine-facing readers
  # (terminal-state obligation note -> session, never the owner).
  local board_item
  board_item="$(owner_facing_name)"
  [[ -n "$TICKET" ]] && board_item="$board_item [$TICKET]"
  # DGN-1444: an unguarded codex run must be visible ON the ledger row, not
  # only in run.env -- the ledger is what the owner surface renders while the
  # run is still alive. Verified runs carry the positive note for symmetry.
  local spawn_note="dispatch 스폰 (model=$MODEL runtime=$RUNTIME axis=$BOTTLENECK)"
  if [[ -n "$CODEX_HOOK_NOTE" ]]; then
    spawn_note="dispatch 스폰 (model=$MODEL runtime=$RUNTIME axis=$BOTTLENECK hooks=$CODEX_HOOK_NOTE)"
  fi
  if [[ -n "$CODEX_MODEL" ]]; then
    spawn_note="${spawn_note%)} requested_model=${REQUESTED_MODEL:-binding} codex_model=$CODEX_MODEL)"
  fi
  # DGN-1742: a repo run that skipped cloud carries its reason on the row.
  [[ -n "$LOCAL_WHY" ]] && spawn_note="${spawn_note%)} local_why=$LOCAL_WHY)"
  if ! "$LEDGER_UPDATE" "$LEDGER_BRANCH" running "$spawn_note" \
        --item "$board_item" --ts "$(date '+%Y-%m-%d %H:%M %Z')"; then
    echo "[dispatch] ledger running-row write failed -- refusing to spawn an unowned worker" >&2
    active_count_decr
    if [[ -n "$WT_PATH" ]]; then
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
    fi
    rm -rf "$RUNDIR"
    exit 1
  fi

  # DGN-1012 terminal-state registration: one obligation per run. If this
  # wrapper never reaches finalize (SIGKILLed with its worker, host crash),
  # the hourly sweep expires the row and the owner agent receives the closure via the
  # session-inbox instead of silence. TTL = run timeout + 30min finalize
  # grace. Fail-open: a failed registration must never block a spawn.
  # (stderr NOT swallowed: a failing registration leaves a trace in this
  # wrapper's log instead of dying silently -- self-grill finding, L5.)
  python3 "$SCRIPT_DIR/terminal-state-ledger.py" open --surface dispatch \
    --id "$RUNID" --ttl "$(( (TIMEOUT_MIN + 30) * 60 ))" --notify session \
    --note "$item" --evidence "$RUNDIR/out.log" >/dev/null || true

  # Detach: python3 fork -> os.setsid -> exec (bridge/self_restart.sh:361-399
  # pattern, DGN-1010). Worker lands in its OWN session/pgid: unreachable by
  # the session CLI teardown AND by the launchd group cleanup.
  local detach_py=""
  if [[ -n "${BRIDGE_PYTHON:-}" ]]; then
    detach_py="$BRIDGE_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    detach_py="python3"
  else
    echo "[dispatch] no python3 for setsid detach -- aborting (nohup fallback would die with the caller group)" >&2
    "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed "dispatch spawn failed (no python3)" || true
    active_count_decr
    if [[ -n "$WT_PATH" ]]; then
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
    fi
    exit 1
  fi

  local self worker_log worker_pid
  self="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
  worker_log="$RUNDIR/worker.log"
  worker_pid="$("$detach_py" - "$worker_log" "$self" --worker-run "$RUNDIR" <<'PYEOF'
import os, sys
log_path, target = sys.argv[1], sys.argv[2]
if os.fork() > 0:
    os._exit(0)  # launcher-side parent: return control to the caller now
os.setsid()      # own session: session-CLI/launchd group teardown cannot reach us
print(os.getpid(), flush=True)  # daemon pid -> launcher capture; releases the pipe
devnull = os.open(os.devnull, os.O_RDONLY)
log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
os.dup2(devnull, 0)
os.dup2(log, 1)
os.dup2(log, 2)
os.close(devnull)
os.close(log)
os.execv(target, [target] + sys.argv[3:])
PYEOF
)"
  printf '%s\n' "${worker_pid:-}" > "$RUNDIR/pid"

  echo "[dispatch] spawned: runid=$RUNID pid=${worker_pid:-?} (own session via setsid)"
  echo "[dispatch]   label:  $item"
  # Never silent about which name the owner surfaces got, or which language
  # decided it (same rule as the BASE-branch resolution line).
  echo "[dispatch]   owner-facing name: $board_item (lang=$OWNER_LANG)"
  if [[ -z "$EFFORT" ]]; then
    echo "[dispatch]   effort: CLI default, no flag (source=${EFFORT_SOURCE:-unmeasured})"
  elif ! effort_passed_to_runtime "$RUNTIME" "$EFFORT"; then
    echo "[dispatch]   effort: $EFFORT (source=$EFFORT_SOURCE) recorded but NOT passed -- runtime $RUNTIME has no verified control for this level"
  elif [[ "$RUNTIME" == "codex" ]]; then
    echo "[dispatch]   effort: $EFFORT (source=$EFFORT_SOURCE, passed as codex -c model_reasoning_effort)"
  else
    echo "[dispatch]   effort: $EFFORT (source=$EFFORT_SOURCE, passed as claude --effort)"
  fi
  [[ -n "$EFFORT_WARNING" ]] && echo "[dispatch]   WARNING effort: $EFFORT_WARNING"
  echo "[dispatch]   model:  $(owner_model_display)  bottleneck: $BOTTLENECK  runtime: $RUNTIME  timeout: ${TIMEOUT_MIN}m  notify: $NOTIFY"
  # DGN-1444: runtime here is already the EFFECTIVE one (codex_quota_gate ran
  # inside run_gates() above and may have flipped it) -- this line is the
  # visible record of that decision, never silent.
  [[ -n "$CODEX_QUOTA_NOTE" ]] && echo "[dispatch]   codex quota: $CODEX_QUOTA_NOTE"
  # DGN-1434: the concurrency cap that just admitted this spawn -- shown on
  # every successful spawn too, not just refusals, so a manual override never
  # looks like an ordinary computed decision (the 2026-08-26 failure mode).
  echo "[dispatch]   dispatch cap: $(read_active_count)/$MAX_ACTIVE ($DYNAMIC_CAP_NOTE)"
  echo "[dispatch]   ledger: $LEDGER_BRANCH (running)"
  [[ -n "$WT_PATH" ]] && echo "[dispatch]   worktree: $WT_PATH (branch $BRANCH, base $WT_BASE, repo $WT_REPO)"
  [[ -n "$REPO_PATH" && "$RUNTIME" != "cloud" ]] && echo "[dispatch]   repo:   $REPO_PATH (observe-mode worker cwd)"
  [[ -n "$SANDBOX_PROFILE" ]] && echo "[dispatch]   observe: kernel read-only (seatbelt $SANDBOX_PROFILE)"
  [[ -n "$PLAN_DIR" ]] && echo "[dispatch]   plan lane: live targets $(tr '\n' ' ' <<<"$LIVE_TARGETS")-> plan $PLAN_DIR/plan.json; apply: $SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}") --apply-plan $RUNID [--apply]"
  [[ -n "$WRITE_PROFILE" ]] && echo "[dispatch]   isolation: kernel write boundary = own worktree only ($WRITE_ROSTER_NOTE; seatbelt $WRITE_PROFILE)"
  [[ -n "$CHECKOUT_SPAWN_NOTE" ]] && echo "[dispatch]   note: $CHECKOUT_SPAWN_NOTE"
  [[ "$RUNTIME" == "cloud" ]] && echo "[dispatch]   cloud:  routine on $CLOUD_REPO_URL (env $CLOUD_ENV_ID) from origin/$WT_BASE -> result branch $BRANCH (branch poll ${CLOUD_POLL_SECS}s, run-status poll ${CLOUD_STATUS_SECS}s; trigger/session/view -> $RUNDIR/cloud.env)"
  echo "[dispatch]   logs:   $RUNDIR/out.log"
  echo "[dispatch]   cancel: $self --cancel $RUNID"

  # START NOTICE (header): hooked HERE, at the one point where the worker is
  # really running. Everything that can refuse or fail a dispatch -- the usage
  # gate, the bottleneck gate, the display-name gate, the active cap, the
  # observe sandbox, worktree provisioning, the ledger row, the detach itself
  # -- has already exited the script above this line, so a refused or failed
  # dispatch reaches no notice. The owner-facing name is the same value the
  # work board renders (no runid, no branch, no machine label).
  if [[ -n "${worker_pid:-}" ]]; then
    start_notice_arm "$(owner_facing_name)"
  else
    echo "[dispatch] start notice skipped: detach returned no worker pid" >&2
  fi

  # DGN-1012 M2: terminal-state SWEEP DRIVE (leg 2 of 2).
  # Leg 1 (cron-guard.sh) rides every periodic job; it dies with launchd.
  # This leg rides delegation activity instead, so the two legs share no
  # single cause -- and this is the leg that covers exactly the window where
  # the other one is least trustworthy: the host that just came back up and
  # whose cron fleet has not fired yet, but on which someone is dispatching.
  #
  # Placed AFTER the worker is detached and reported, so the spawn path pays
  # nothing: throttled, this is one file read; unthrottled, the worker is
  # already running while it sweeps. Fail-open, as every registration is.
  python3 "$SCRIPT_DIR/terminal-state-ledger.py" sweep \
    --throttle "${DOGANY_TSL_SWEEP_INTERVAL:-3600}" >/dev/null || true
}

# ---------------------------------------------------------------------------
# Worker (runs detached in its own session; entry: --worker-run <rundir>)
# ---------------------------------------------------------------------------

# Backstop system prompt: autonomous-loop contract promoted (spec step 5).
# NOTE: the model must NEVER notify by itself -- push is issued by this shell
# only ("LLM judges, shell writes", spec R4).
worker_backstop() {
  cat <<'EOF'
You are a detached background worker dispatched by dispatch-detached.sh.
Hard rules (violations void the run):
- NEVER run git push, git tag, or any remote/network-mutating operation.
- NEVER edit baseline docs: AGENT.md, RULES.md, USER.md, rules/hot.framework.md, identity/ (agent + owner files), any SKILL.md, memories/.
- NEVER write to worklog/ and NEVER merge or commit to the main branch, nor
  to dev (the integration branch) -- merging your branch is the dispatcher's act.
- NEVER spawn sub-agents (Task/Agent are disabled).
- NEVER call routines/push.sh or send any user notification yourself.
  All notifications are issued by the dispatch wrapper shell after you exit.
  Your output is files only: in worktree mode, commit your work to the
  current branch; in observe mode, print your final report as plain text
  (the wrapper captures stdout to a log).
- In observe mode (no worktree) every file write outside TMPDIR and /tmp is
  denied by the kernel ("Operation not permitted"). That is the contract,
  not a bug: do not route around it (no launchctl / osascript / open / other
  out-of-sandbox executors). Bash is fully open: git -C, cd, sqlite3,
  python3, --dry-run scripts all work; scratch space is $TMPDIR. If the task
  needs writes, report back and ask the dispatcher to relaunch you with
  --worktree <slug> [--worktree-repo <path>].
- Do not restart/stop/reconfigure bots, services, or the bridge.
EOF
  # DGN-1458 plan lane: the planner's one writable place and the contract the
  # applier enforces, stated where the worker reads it.
  if [[ -n "${PLAN_DIR:-}" ]]; then
    cat <<EOF
- PLAN LANE (DGN-1458): these live paths are the subject of this task and
  are NOT writable by you:
$(sed 's/^/    /' <<<"$LIVE_TARGETS")
  Verify, then write the exact changes to $PLAN_DIR/plan.json -- the one
  place you can write. After you exit, the dispatcher applies it
  mechanically: only inside those paths, only if every precondition still
  holds, all-or-nothing, once. Format (JSON, ops run in order):
    {"version": 1, "summary": "<one line>", "ops": [
      {"op": "write", "path": "<abs>", "content": "<full new file text>",
       "expect_sha256": "<sha256 of the current file, or absent>"},
      {"op": "delete", "path": "<abs>", "expect_sha256": "<sha256>"},
      {"op": "sqlite", "db": "<abs .db>", "sql": "<statements>",
       "expect_changes": <exact number of rows changed>}]}
  Compute every expect_sha256 now (shasum -a 256 <file>) and every
  expect_changes by a read-only count query. One op per path. Anything the
  format cannot express (service restarts, launchctl, chmod): list it in
  your report for the dispatcher instead.
EOF
  fi
  # DGN-1504 (c): the work location is injected by the dispatcher, never left
  # to the brief. 2026-09-19: a brief said "work in the CANONICAL repo only:
  # <path>" and the worker read it literally -- the worktree path lived only
  # in the spawn output, which the worker never sees.
  if [[ -n "${WT_PATH:-}" ]]; then
    local shared=""
    shared="$(git -C "$WT_REPO" worktree list --porcelain 2>/dev/null | sed -n '1s/^worktree //p')"
    cat <<EOF
- WORK LOCATION (set by the dispatcher; it overrides any path in the task):
  your ONLY writable checkout is $WT_PATH
  (branch $BRANCH, cut from $WT_BASE of ${shared:-$WT_REPO}). Start with
  cd $WT_PATH. When the task names ${shared:-$WT_REPO} (or any other
  checkout) as the place to work, it means the same relative path inside
  $WT_PATH. Every other checkout, and the branch refs main/dev/$WT_BASE,
  are closed to this run by the kernel ("Operation not permitted"). That is
  the contract, not a bug: do not route around it.
EOF
  fi
}

# JSON status writer -- python3 for quoting safety; deterministic shell owns
# the values, python only serializes.
write_status_json() {
  # args: result exit_code notify
  ST_RESULT="$1" ST_EXIT="$2" ST_NOTIFY="$3" \
  ST_RUNID="$RUNID" ST_LABEL="$LABEL" ST_TICKET="$TICKET" ST_MODEL="$MODEL" ST_MODEL_SOURCE="${MODEL_SOURCE:-}" ST_EFFORT="${EFFORT:-}" ST_EFFORT_SOURCE="${EFFORT_SOURCE:-}" \
  ST_REQUESTED_MODEL="${REQUESTED_MODEL:-}" ST_CODEX_MODEL="${CODEX_MODEL:-}" \
  ST_DISPLAY_NAME="${DISPLAY_NAME:-}" ST_ACCESS_MODE="${ACCESS_MODE:-}" \
  ST_AXIS="${BOTTLENECK:-}" ST_AXIS_WHY="${BOTTLENECK_WHY:-}" ST_RUNTIME="${RUNTIME:-}" ST_WHY="${TIER_WHY:-}" \
  ST_WHY_USAGE="${TIER_WHY_USAGE:-}" \
  ST_REQUESTED_RUNTIME="${REQUESTED_RUNTIME:-}" \
  ST_SELECTION_SOURCE="${RUNTIME_SELECTION_SOURCE:-}" ST_SELECTION_REASON="${RUNTIME_SELECTION_REASON:-}" \
  ST_SELECTION_DETAIL="${RUNTIME_SELECTION_DETAIL:-}" \
  ST_FALLBACK_DIRECTION="${RUNTIME_FALLBACK_DIRECTION:-}" \
  ST_FALLBACK_NOTE="${RUNTIME_FALLBACK_NOTE:-}" \
  ST_DECISION_ID="${DECISION_ID:-}" \
  ST_CODEX_QUOTA_NOTE="${CODEX_QUOTA_NOTE:-}" \
  ST_CODEX_HOOK_NOTE="${CODEX_HOOK_NOTE:-}" \
  ST_CODEX_UNGUARDED_WHY="${CODEX_UNGUARDED_WHY:-}" \
  ST_LOCAL_WHY="${LOCAL_WHY:-}" \
  ST_MODEL_OBSERVED="${MODEL_OBSERVED_JSON:-[]}" \
  ST_BRANCH="$BRANCH" ST_COMMITS="${COMMIT_COUNT:-0}" ST_HEAD="${COMMIT_HASH:-}" \
  ST_STARTED="${STARTED_AT:-}" ST_TIMEOUT="$TIMEOUT_MIN" \
  ST_OUTLOG="$RUNDIR/out.log" ST_INBOX="${INBOX_STATE:-pending}" \
  ST_PRESERVE="${PRESERVE_STATE:-}" ST_PRESERVE_NOTE="${PRESERVE_NOTE:-}" \
  ST_WT_KEPT="${WT_KEPT_PATH:-}" \
  ST_WRITE_PROFILE="${WRITE_PROFILE:-}" ST_CHECKOUT_DRIFT="${CHECKOUT_DRIFT:-}" \
  ST_LIVE_TARGETS="${LIVE_TARGETS:-}" ST_PLAN_STATE="${PLAN_STATE:-}" \
  ST_CANCEL_BY="${CANCEL_BY:-}" \
  ST_CLOUD_SESSION="${CLOUD_SESSION_ID:-}" ST_CLOUD_VIEW="${CLOUD_VIEW_URL:-}" \
  ST_CLOUD_SHA="${CLOUD_SHA:-}" ST_CLOUD_TRIGGER="${CLOUD_TRIGGER_ID:-}" \
  ST_CLOUD_REASON="${CLOUD_FAIL_REASON:-}" \
  python3 - "$RUNDIR/status.json" <<'PY'
import json, os, sys, time

try:
    model_observed = json.loads(os.environ.get("ST_MODEL_OBSERVED", "[]") or "[]")
    if not isinstance(model_observed, list):
        model_observed = []
except Exception:
    model_observed = []

doc = {
    "runid":      os.environ.get("ST_RUNID", ""),
    "label":      os.environ.get("ST_LABEL", ""),
    # The owner-facing name of this run (header, DISPLAY NAME). Recorded
    # unconditionally and unselected: this field is the RAW --display-name,
    # so a later owner-facing consumer applies the same language selection
    # (owner_facing_name) instead of re-deriving a name of its own. "" on a
    # run dispatched without one (English instance, or a pre-field run.env).
    "display_name": os.environ.get("ST_DISPLAY_NAME", ""),
    "ticket":     os.environ.get("ST_TICKET", ""),
    "model":      os.environ.get("ST_MODEL", ""),
    # DGN-1666: "explicit" (--model) or "binding" (auto-resolved); "" on
    # run.env files that predate the field.
    "model_source": os.environ.get("ST_MODEL_SOURCE", ""),
    "requested_model": os.environ.get("ST_REQUESTED_MODEL", ""),
    "codex_model": os.environ.get("ST_CODEX_MODEL", ""),
    # DGN-1666: requested reasoning effort ("" = flag not passed / old run.env).
    "effort": os.environ.get("ST_EFFORT", ""),
    # DGN-1770: explicit | table | no-effect | unmeasured ("" = old run.env).
    "effort_source": os.environ.get("ST_EFFORT_SOURCE", ""),
    # DGN-1218/DGN-1485 measurement fields: the declared bottleneck axis
    # (Layer 1 answer), its task evidence, the runtime/provider that
    # executed, and the override rationale if any. Without runtime, cross-provider comparison is
    # impossible once a second runtime exists.
    "bottleneck": os.environ.get("ST_AXIS", ""),
    "bottleneck_why": os.environ.get("ST_AXIS_WHY", ""),
    "runtime":    os.environ.get("ST_RUNTIME", ""),
    # DGN-1470: what the caller asked for vs. what actually ran, plus the
    # recorded switch (if any) and the pre-spawn decision record it links to
    # (DISPATCH_DIR/decisions/<decision_id>.json). "" everywhere on an
    # old run.env files predating this field; new normal selections carry it.
    "requested_runtime": os.environ.get("ST_REQUESTED_RUNTIME", ""),
    "selection_source": os.environ.get("ST_SELECTION_SOURCE", ""),
    "selection_reason": os.environ.get("ST_SELECTION_REASON", ""),
    "selection_detail": os.environ.get("ST_SELECTION_DETAIL", ""),
    "runtime_fallback_direction": os.environ.get("ST_FALLBACK_DIRECTION", ""),
    "runtime_fallback_note": os.environ.get("ST_FALLBACK_NOTE", ""),
    "decision_id": os.environ.get("ST_DECISION_ID", ""),
    "tier_why":   os.environ.get("ST_WHY", ""),
    # DGN-1512: the machine-filled usage snapshot taken at override time --
    # ADDITIVE to tier_why above, never a substitute for it. tier_why stays
    # the human's verbatim sentence (calibration evidence of what they
    # believed); this field is what the account actually looked like at that
    # same moment, so a stale human claim is auditable against it later
    # instead of trusted at face value. "" when no override happened (bound
    # model chosen, no --why needed) or on a pre-DGN-1512 run.env.
    "tier_why_usage": os.environ.get("ST_WHY_USAGE", ""),
    # DGN-1444: codex quota-gate outcome for THIS run -- "" on a claude-code
    # run requested as such, a proceed note on a healthy codex read, or a
    # fallback note when the read failed and RUNTIME above was flipped to
    # claude-code. Never silent: this is the after-the-fact record of the
    # same decision dispatch output already printed at spawn time.
    "codex_quota_note": os.environ.get("ST_CODEX_QUOTA_NOTE", ""),
    # DGN-1444: hook-trust gate outcome for THIS run. "" on a claude-code
    # run; "hooks-verified@worktree" when codex confirmed the project hook
    # layer trusted+enabled at the worker's cwd; "UNGUARDED(<reason>)" when
    # the operator overrode the gate -- the ledger row carries the same mark
    # (never a silently unguarded run again: the 2026-09-14 incident).
    "codex_hook_note": os.environ.get("ST_CODEX_HOOK_NOTE", ""),
    "codex_unguarded_why": os.environ.get("ST_CODEX_UNGUARDED_WHY", ""),
    # DGN-1742: the stated reason this repo run skipped cloud ("" = none).
    "local_why": os.environ.get("ST_LOCAL_WHY", ""),
    # DGN-1227: alias's real version(s), observed from stream.jsonl at close
    # time. [] means "not observed" (no stream, unparseable, or genuinely no
    # assistant turn ran) -- never treat [] as "confirmed empty".
    "model_observed": model_observed,
    "access_mode": os.environ.get("ST_ACCESS_MODE", ""),
    "result":     os.environ.get("ST_RESULT", ""),
    "exit_code":  int(os.environ.get("ST_EXIT", "0") or 0),
    "branch":     os.environ.get("ST_BRANCH", ""),
    "commits":    int(os.environ.get("ST_COMMITS", "0") or 0),
    "head":       os.environ.get("ST_HEAD", ""),
    "started_at": os.environ.get("ST_STARTED", ""),
    "ended_at":   time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "timeout_min": int(os.environ.get("ST_TIMEOUT", "0") or 0),
    "notify":     os.environ.get("ST_NOTIFY", ""),
    "out_log":    os.environ.get("ST_OUTLOG", ""),
    # DGN-1012: session-inbox drop trace -- "failed" here is the ONLY
    # surviving evidence when the session return path could not be written
    # (the drop itself is || true so it never breaks finalize).
    "session_inbox": os.environ.get("ST_INBOX", ""),
    # DGN-1688: ``self`` means the dispatching bridge session intentionally
    # canceled this run.  It is not a worker death or timeout notification.
    "cancel_by": os.environ.get("ST_CANCEL_BY", ""),
    # DGN-1433: uncommitted-product preservation verdict. "" = tree was
    # clean (nothing to preserve); "kept-worktree" names the unchanged
    # checkout for manual recovery. No harness commit is created.
    "preserve": os.environ.get("ST_PRESERVE", ""),
    "preserve_note": os.environ.get("ST_PRESERVE_NOTE", ""),
    "worktree_kept": os.environ.get("ST_WT_KEPT", ""),
    # DGN-1504: the kernel write profile this run was confined by ("" =
    # observe/cloud/codex run, or a pre-DGN-1504 run.env), and what moved in
    # the shared checkouts between spawn and finalize ("" = nothing). Drift
    # is never this worker's write -- the profile denies those -- it is the
    # attribution record for anything measured in that checkout meanwhile.
    "write_profile": os.environ.get("ST_WRITE_PROFILE", ""),
    "checkout_drift": os.environ.get("ST_CHECKOUT_DRIFT", ""),
    # DGN-1458 plan lane: declared live targets ([] = not a plan-lane run)
    # and the plan verdict at close ("absent" | "ready: N op(s)" |
    # "invalid: <why>"; "" = not a plan-lane run). Nothing is applied here.
    "live_targets": [t for t in os.environ.get("ST_LIVE_TARGETS", "").split("\n") if t],
    "plan_state": os.environ.get("ST_PLAN_STATE", ""),
}
# DGN-1726: cloud runs only -- the remote session and the result branch head
# the watcher saw on origin ("" sha = never appeared).
if os.environ.get("ST_RUNTIME") == "cloud":
    doc["cloud_session"] = os.environ.get("ST_CLOUD_SESSION", "")
    doc["cloud_view_url"] = os.environ.get("ST_CLOUD_VIEW", "")
    doc["cloud_branch_sha"] = os.environ.get("ST_CLOUD_SHA", "")
    doc["cloud_trigger"] = os.environ.get("ST_CLOUD_TRIGGER", "")
    doc["cloud_fail_reason"] = os.environ.get("ST_CLOUD_REASON", "")
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
os.replace(tmp, sys.argv[1])
PY
}

# DGN-1012 return path: drop a completion file into the bridge session-inbox
# (bot.py _session_inbox_loop, DGN-217 -- same machinery as restart-verify /
# handoff / ticket-hygiene) so the DISPATCHING SESSION itself learns the run
# ended and picks the result up. The owner push below is for the owner's
# awareness only; without this drop, the session stays blind until the owner
# manually asks (2026-08-24 incident: three survey dispatches completed, owner
# was pinged, the session never was).
#
# NOTIFY semantics (do not conflate): --notify silent means "do not push the
# OWNER"; it does NOT mean "leave the session uninformed". The inbox drop therefore
# runs UNCONDITIONALLY on every terminal state.
#
# UTF-8 contract: bot.py quarantines undecodable inbox files (bot.py:644),
# which would blind the session all over again -- so the file is written by python3
# with encoding="utf-8" + errors="replace" (mangled label bytes degrade to
# U+FFFD instead of poisoning the drop). Temp-write + atomic rename per the
# DGN-217 writer contract; the dot-prefixed temp never matches the poller's
# *.md glob, so a half-written file is never injected.
write_session_inbox() {
  # args: result exit_code
  IB_RESULT="$1" IB_EXIT="$2" \
  IB_RUNID="$RUNID" IB_LABEL="$LABEL" IB_TICKET="$TICKET" IB_MODEL="$MODEL" IB_RUNTIME="$RUNTIME" \
  IB_MODEL_DISPLAY_BINDINGS="$MODEL_DISPLAY_BINDINGS" IB_LIB_DIR="$SCRIPT_DIR/lib" \
  IB_MODEL_OBSERVED="${MODEL_OBSERVED_JSON:-}" \
  IB_BRANCH="$BRANCH" IB_COMMITS="${COMMIT_COUNT:-0}" IB_HEAD="${COMMIT_HASH:-}" \
  IB_OUTLOG="$RUNDIR/out.log" IB_INBOX="$SESSION_INBOX_DIR" \
  IB_PRESERVE="${PRESERVE_STATE:-}" IB_PRESERVE_NOTE="${PRESERVE_NOTE:-}" \
  IB_WT_KEPT="${WT_KEPT_PATH:-}" \
  IB_CHECKOUT_DRIFT="${CHECKOUT_DRIFT:-}" \
  IB_PLAN_STATE="${PLAN_STATE:-}" IB_APPLY_CMD="$SCRIPT_DIR/dispatch-detached.sh --apply-plan $RUNID" \
  IB_CANCEL_BY="${CANCEL_BY:-}" \
  IB_CLOUD_SESSION="${CLOUD_SESSION_ID:-}" IB_CLOUD_VIEW="${CLOUD_VIEW_URL:-}" \
  IB_CLOUD_SHA="${CLOUD_SHA:-}" IB_TIMEOUT="${TIMEOUT_MIN:-}" \
  IB_CLOUD_TRIGGER="${CLOUD_TRIGGER_ID:-}" IB_CLOUD_DISABLED="${CLOUD_TRIGGER_DISABLED:-}" \
  IB_CLOUD_REASON="${CLOUD_FAIL_REASON:-}" \
  python3 - <<'PY'
# json is required by _stream_signals() below. It was missing until 2026-09-08
# (DGN-1347): every detached run's return-path drop died with
# "NameError: name 'json' is not defined" AFTER the work had completed, so the
# owning agent never learned the run ended. Do not remove either import.
import json
import os
import sys

def env(k):
    # os.environ decodes argv bytes with surrogateescape; surrogates must not
    # reach the file or bot.py quarantines it. Round-trip with replace so the
    # output ALWAYS decodes as clean UTF-8.
    return os.environ.get(k, "").encode("utf-8", "replace").decode("utf-8", "replace")


def model_label(runtime, model):
    # DGN-1227 follow-up, FAIL-OPEN: the display lib is presentation only.
    # Missing lib, import error, a raise inside it or an empty answer -> the
    # raw alias. A display failure must never cost this drop (it is the
    # terminal signal the ledger close below waits on).
    try:
        sys.path.insert(0, env("IB_LIB_DIR"))
        from model_display import model_display
        label = model_display(runtime, model, env("IB_MODEL_OBSERVED"),
                              env("IB_MODEL_DISPLAY_BINDINGS"))
    except (Exception, SystemExit):
        label = ""
    return label if isinstance(label, str) and label.strip() else model

runid, label, ticket = env("IB_RUNID"), env("IB_LABEL"), env("IB_TICKET")
result, exit_code, model = env("IB_RESULT"), env("IB_EXIT"), env("IB_MODEL")
runtime = env("IB_RUNTIME")
branch, head, outlog = env("IB_BRANCH"), env("IB_HEAD"), env("IB_OUTLOG")
try:
    commits = int(env("IB_COMMITS"))
except ValueError:
    commits = 0
inbox = os.environ["IB_INBOX"]

lines = [
    f"[dispatch-return] 디스패치 종결: {label or runid} -- {result}",
    "",
    f"- runid: {runid}",
    f"- label: {label}",
    f"- ticket: {ticket or '(없음)'}",
    f"- model: {model_label(runtime, model)}",
    f"- result: {result} (exit={exit_code})",
]
cancel_by = env("IB_CANCEL_BY")
if cancel_by:
    lines.append(f"- cancel_by: {cancel_by}")
if branch and commits > 0:
    lines.append(f"- branch: {branch} (commits={commits}, HEAD={head})")
# DGN-1433: preservation verdict is part of the return record -- the reader
# must never have to guess whether uncommitted product survived teardown.
preserve = env("IB_PRESERVE")
preserve_note = env("IB_PRESERVE_NOTE")
wt_kept = env("IB_WT_KEPT")
if preserve == "kept-worktree":
    lines.append(f"- preserve: 워크트리 유지 ({preserve_note}): {wt_kept}")
# DGN-1504: shared-checkout drift during the run (kernel-denied to this
# worker, so never its product -- an attribution record only).
drift = env("IB_CHECKOUT_DRIFT")
if drift:
    lines.append(f"- checkout drift (not this worker: its writes there are kernel-denied): {drift}")
lines += [f"- out.log: {outlog}", ""]
# DGN-1012 state/content split. LIFECYCLE (how the run ended) and PRODUCT
# (what it left behind) are two independent facts and must be measured from
# two independent sources: lifecycle from the runner's exit code, product from
# artifacts on disk. Binding them to one signal is the 2026-08-28 failure --
# the deadline killed a run (exit=124) whose work had already landed, and the
# drop told the session to "diagnose the cause and consider re-dispatching" work
# that was sitting committed on a branch. Never infer product from lifecycle.
try:
    outlog_bytes = os.path.getsize(outlog)
except OSError:
    outlog_bytes = 0
# DGN-1337: byte count is NOT product. Session bootstrap alone writes 1-13KB
# (hook lines, init, thinking) with zero tool_use -- measured on two cancelled
# runs on 2026-09-08. Treating bytes as product told the agent to
# "recover the artifact" from runs that produced nothing, twice. Same defect
# family as DGN-1331 B-branch: text exists -> work happened.
# Product now needs a real trace: a commit, a tool call, or agent speech.
# DGN-1444: recognizes BOTH stream shapes (claude's "assistant"/tool_use
# blocks and codex's item.completed/{command_execution,agent_message}) in the
# SAME pass -- not a runtime branch, just a wider vocabulary for the same two
# concepts, so a real codex report-type completion (real out.log text, zero
# commits) is not misreported as a no-op. Confirmed by a live E2E run
# (2026-09-13): before this fix, a codex run with genuine agent_message text
# in out.log was reported "product: absent ... suspect a no-op" because this
# function only knew claude's vocabulary.
def _stream_signals(run_dir):
    """(tool_use_count, assistant_msg_count) from stream.jsonl. Unreadable -> (0, 0)."""
    tu = am = 0
    path = os.path.join(run_dir, "stream.jsonl")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                blob = json.dumps(obj)
                if '"tool_use"' in blob:
                    tu += 1
                if obj.get("type") == "assistant":
                    am += 1
                if obj.get("type") == "item.completed":
                    item_type = (obj.get("item") or {}).get("type")
                    if item_type == "command_execution":
                        tu += 1
                    elif item_type == "agent_message":
                        am += 1
    except OSError:
        pass
    return tu, am

tool_uses, assistant_msgs = _stream_signals(os.path.dirname(outlog))
plan_state = env("IB_PLAN_STATE")
product = ("present" if (commits > 0 or tool_uses > 0 or assistant_msgs > 0
                          or preserve == "kept-worktree"
                          or plan_state.startswith("ready"))
           else "absent")
lines.insert(-1, f"- product: {product} "
                 f"(commits={commits}, tool_use={tool_uses}, "
                 f"assistant={assistant_msgs}, out.log={outlog_bytes}B)")
if plan_state:
    lines.insert(-1, f"- plan: {plan_state}")

# Session-directive prefix, written ONCE: the four branches below differ in
# their instruction, not in who they address.
D = "Metal 지시: "

# DGN-1726: a cloud run's product is a branch on origin, not local commits or
# a stream -- its record and directive are its own, same drop, same header.
cloud_sha = env("IB_CLOUD_SHA")
if runtime == "cloud":
    del lines[-2]  # the local-stream product line does not describe a cloud run
    view = env("IB_CLOUD_VIEW")
    trigger = env("IB_CLOUD_TRIGGER")
    reason = env("IB_CLOUD_REASON")
    if result == "canceled" and not reason:
        reason = "canceled here: local watcher stopped, routine disabled; the remote session is not stopped from here"
    lines[-1:-1] = [
        f"- cloud session: {env('IB_CLOUD_SESSION') or '(start failed -- no session)'}",
        f"- view: {view or '(none)'}",
        f"- routine: {trigger or '(none)'}"
        + (f" (disabled={env('IB_CLOUD_DISABLED') or 'no'})" if trigger else ""),
        (f"- branch: {branch} (origin, sha={cloud_sha})" if cloud_sha
         else f"- branch: {branch} (not on origin)"),
        f"- product: {'present' if cloud_sha else 'absent'} (remote branch)",
    ]
    if reason:
        lines.insert(-1, f"- reason: {reason}")
    if trigger and env("IB_CLOUD_DISABLED") != "yes":
        lines.insert(-1, f"- WARNING: routine {trigger} was not confirmed disabled -- "
                         "it can fire again at its run_once_at; disable it on claude.ai")
    if cloud_sha:
        lines.append(
            f"{D}cloud 세션이 결과를 origin 의 {branch} (sha={cloud_sha}) 에 푸시했다. "
            f"git fetch origin {branch} 로 회수해 리뷰하고 머지/반려를 결정하라.")
    elif not env("IB_CLOUD_SESSION"):
        lines.append(
            f"{D}cloud 루틴 시작에 실패했다 ({result}, exit={exit_code}): {reason or '?'}. "
            "원인을 오너에게 한 줄로 알리고 재발주 여부를 판단하라.")
    elif result == "timed-out":
        lines.append(
            f"{D}cloud 세션이 제한 {env('IB_TIMEOUT')}분 안에 {branch} 를 origin 에 "
            f"푸시하지 않았다. 원격 세션은 여기서 멈추지 않는다 -- {view} 에서 상태를 "
            "확인하고, 늦게 푸시됐으면 회수하고 아니면 재발주/수동 마무리를 판단하라.")
    else:
        lines.append(
            f"{D}cloud 세션이 {branch} 를 푸시하지 않고 끝났다 ({result}, exit={exit_code}): "
            f"{reason or '?'}. {view} 에서 로그를 확인하고, 원인을 오너에게 한 줄로 알린 뒤 "
            "재발주/수동 마무리를 판단하라.")
    product = "cloud"

if product == "cloud":
    pass
elif product == "present":
    if commits > 0:
        recover = (f"branch {branch} 에 커밋 {commits}건이 남아 있다. "
                   "먼저 회수해 리뷰하고 머지/반려를 결정하라.")
    elif preserve == "kept-worktree":
        recover = f"워크트리 {wt_kept} 의 변경을 먼저 회수해 리뷰하라."
    else:
        recover = "out.log 에 산출물이 남아 있다. 먼저 읽고 반영하라."
    if result == "completed":
        lines.append(f"{D}이 결과를 회수해 후속 작업을 진행하라 -- {recover}")
    else:
        # The kill and the product are BOTH true. Recovery leads; re-dispatch is
        # a decision made AFTER reading what already landed, never before.
        lines.append(
            f"{D}이 디스패치는 {result}(exit={exit_code}) 로 죽었지만 "
            f"산출물은 남아 있다 -- 재발주 판단 전에 회수가 먼저다. {recover} "
            "그 다음에 out.log 로 어디서 끊겼는지 확인하고 잔여분만 재발주하라.")
elif result == "completed":
    lines.append(
        f"{D}정상 종료했으나 산출물이 없다 -- 커밋도 out.log 내용도 "
        "없다. no-op 완료를 의심하고 발주 내용과 대조하라.")
else:
    lines.append(
        f"{D}이 디스패치는 {result}(exit={exit_code}) 로 끝났고 "
        "산출물도 없다. out.log 를 확인해 원인을 파악하고 재발주/수동 마무리 "
        "여부를 판단하라.")

# DGN-1458 plan lane: the plan is the product; applying it is a separate,
# explicit act of the dispatcher (dry run first).
if plan_state:
    if plan_state.startswith("ready") and result == "completed":
        lines.append(
            f"{D}plan-lane run: review the plan, then apply it -- dry run: "
            f"{env('IB_APPLY_CMD')} ; apply: {env('IB_APPLY_CMD')} --apply")
    elif plan_state.startswith("invalid"):
        lines.append(f"{D}plan-lane run produced an unusable plan ({plan_state}); nothing can be applied.")

# DGN-1433 recovery directive: name exactly where the preserved product sits.
if preserve == "kept-worktree":
    lines.append(
        f"{D}증거 보존을 위해 워크트리를 지우지 않았다: {wt_kept} -- "
        "HEAD, index, 미커밋 파일을 그대로 유지했다. staged/unstaged 변경과 "
        "untracked/ignored 파일을 검토해 회수한 뒤 수동으로 정리하라.")

os.makedirs(inbox, exist_ok=True)
name = f"dispatch-{runid}.md"
tmp = os.path.join(inbox, f".{name}.tmp")
with open(tmp, "w", encoding="utf-8", errors="replace") as f:
    f.write("\n".join(lines) + "\n")
os.replace(tmp, os.path.join(inbox, name))
PY
}

# DGN-1227: observe the REAL model identifier(s) claude -p actually ran, read
# from stream.jsonl -- run.env/status.json "model" is only ever the ALIAS
# (fable/opus/sonnet/haiku); we do not pin the alias->version mapping, so it
# can move between two dispatches while the ledger shows the same name both
# times (worklog/DGN-1227). This is an OBSERVATION, never a gate: parsing is
# defensive line-by-line (stream schema is not ours to depend on) and every
# failure mode -- missing file, empty file, malformed line, unexpected shape
# -- degrades to "[]" rather than a non-zero exit, so it can never block
# finalize. Multiple distinct values are ALL kept in first-seen order rather
# than collapsed to one, so a mid-stream version change (or a demotion, see
# fable_exhausted) is visible instead of silently averaged away. Layer 2
# (DGN-1226): a dev-agent ledger row, machine-observed, not user-facing --
# no layer-conditional branching lives here.
observe_model_from_stream() {
  local out=""
  out="$(python3 "$SCRIPT_DIR/lib/model_display.py" --observe-stream "$1" 2>/dev/null)" || out=""
  [[ "$out" == "["* ]] || out='[]'
  printf '%s\n' "$out"
}

# DGN-1433 / DGN-1459: preserve evidence, not a synthetic author signature.
# Reuse the existing kept-worktree recovery path for ANY dirty terminal run.
# Keeping the checkout preserves HEAD, staged/unstaged boundaries, untracked
# and ignored files without add/commit or a lossy reconstruction from patches.
# Unknown status or an off-branch HEAD also blocks destructive cleanup.
PRESERVE_STATE=""   # "" = proven clean | kept-worktree = manual recovery
PRESERVE_NOTE=""
WT_KEPT_PATH=""
preserve_worktree_product() {
  # args: result (preservation is independent of lifecycle)
  PRESERVE_STATE=""
  PRESERVE_NOTE=""
  WT_KEPT_PATH=""
  [[ -n "$WT_PATH" && -d "$WT_PATH" ]] || return 0
  # Teardown deletes the CHECKOUT; only the branch survives. So safety is
  # proven against the branch: a detached or switched HEAD means even
  # COMMITTED work would not survive removal -- same G6 halt.
  local head_ref=""
  head_ref="$(git -C "$WT_PATH" symbolic-ref --short -q HEAD 2>/dev/null || true)"
  if [[ -z "$head_ref" || "$head_ref" != "$BRANCH" ]]; then
    PRESERVE_STATE="kept-worktree"
    PRESERVE_NOTE="HEAD not on $BRANCH (${head_ref:-detached})"
    WT_KEPT_PATH="$WT_PATH"
    return 0
  fi
  # Ignored residue alone is not product. Once triggered, retaining the whole
  # checkout also preserves ignored evidence without changing the index.
  local dirty=""
  if ! dirty="$(git -C "$WT_PATH" status --porcelain --untracked-files=all --ignore-submodules=none 2>/dev/null)"; then
    # Cannot even MEASURE the tree -> assume there is product and keep it.
    PRESERVE_STATE="kept-worktree"
    PRESERVE_NOTE="git status unreadable"
    WT_KEPT_PATH="$WT_PATH"
    return 0
  fi
  [[ -n "$dirty" ]] || return 0
  PRESERVE_STATE="kept-worktree"
  PRESERVE_NOTE="uncommitted product retained with ignored files; index and HEAD unchanged"
  WT_KEPT_PATH="$WT_PATH"
  return 0
}

refresh_cancel_by() {
  # The canceler records this tiny marker before it signals the detached
  # worker.  The worker has already sourced run.env, so finalize must read the
  # marker rather than rely on its inherited shell variable.
  if [[ -f "$RUNDIR/cancel-by" ]] && [[ "$(cat "$RUNDIR/cancel-by" 2>/dev/null)" == "self" ]]; then
    CANCEL_BY="self"
  fi
}

record_self_cancel() {
  # Keep run.env human/audit readable, but use the marker as the cross-process
  # handoff: the detached worker has already sourced its environment.
  local cancel_marker="$RUNDIR/.cancel-by.tmp"
  printf 'self\n' > "$cancel_marker"
  mv -f "$cancel_marker" "$RUNDIR/cancel-by"
  CANCEL_BY="self"
  printf 'CANCEL_BY=%q\n' "$CANCEL_BY" >> "$RUNDIR/run.env"
}

# Finalize: idempotent (atomic .finalized guard -- cancel fallback and worker
# trap may race). Order per spec step 7: status.json -> worktree remove ->
# active-count decr -> ledger end row -> inbox drop (DGN-1012) -> push ->
# notify-failed backfill.
finalize_run() {
  # args: result exit_code
  local result="$1" exit_code="$2"
  mkdir "$RUNDIR/.finalized" 2>/dev/null || return 0

  refresh_cancel_by
  # DGN-1726: the cloud worker's findings (session, view URL, branch sha)
  # live in RUNDIR/cloud.env -- read here so a launcher-side finalize
  # (--cancel of a dead watcher) reports them too.
  if [[ "${RUNTIME:-}" == "cloud" && -f "$RUNDIR/cloud.env" ]]; then
    # shellcheck disable=SC1091
    source "$RUNDIR/cloud.env" || true
  fi

  # Preserve before cleanup; commit counts describe only worker commits.
  preserve_worktree_product "$result"

  COMMIT_COUNT=0
  COMMIT_HASH=""
  if [[ -n "$WT_PATH" && -d "$WT_PATH" ]]; then
    COMMIT_COUNT="$(git -C "$WT_PATH" rev-list --count "${WT_BASE:-main}..HEAD" 2>/dev/null || echo 0)"
    COMMIT_HASH="$(git -C "$WT_PATH" rev-parse --short HEAD 2>/dev/null || echo '')"
  fi
  # DGN-1458: the planner's product is a plan, judged (not applied) here.
  PLAN_STATE=""
  if [[ -n "${LIVE_TARGETS:-}" ]]; then
    local _pt=() _l
    while IFS= read -r _l; do [[ -n "$_l" ]] && _pt+=(--target "$_l"); done <<<"$LIVE_TARGETS"
    PLAN_STATE="$(python3 "$SCRIPT_DIR/plan-apply.py" --rundir "$RUNDIR" "${_pt[@]}" --check 2>/dev/null)" \
      || PLAN_STATE="invalid: plan check did not run"
  fi
  # DGN-1504 evidence rail: what moved in the shared checkouts during the run.
  CHECKOUT_DRIFT=""
  if [[ -n "$WT_PATH" && -f "$RUNDIR/checkouts-start.tsv" ]]; then
    checkout_snapshot "$RUNDIR/checkouts-end.tsv" || true
    CHECKOUT_DRIFT="$(checkout_drift "$RUNDIR/checkouts-start.tsv" "$RUNDIR/checkouts-end.tsv")"
  fi

  # DGN-1227 (b) autofill -- observed at CLOSE time, not spawn time: run.env's
  # MODEL only ever knows the alias asked for; the real version behind it is
  # only knowable once the worker has actually produced output. See
  # observe_model_from_stream for the fail-open contract.
  # DGN-1444: codex needs no stream-based observation -- unlike claude's
  # alias, which can silently resolve to a different snapshot behind the
  # same name, the codex slug passed on -m IS the concrete model identifier
  # (codex_slug_for_model is a fixed, versioned mapping we own).
  # DGN-1726: a cloud run passes the pinned id on --model the same way, and
  # has no local stream to observe from.
  if [[ "$RUNTIME" == "codex" || "$RUNTIME" == "cloud" ]]; then
    local _codex_observed_slug=""
    _codex_observed_slug="$(model_display_binding "$RUNTIME" "$MODEL" 3 2>/dev/null)" || _codex_observed_slug=""
    if [[ -n "$_codex_observed_slug" ]]; then
      MODEL_OBSERVED_JSON="$(python3 -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$_codex_observed_slug")"
    else
      MODEL_OBSERVED_JSON='[]'
    fi
  else
    MODEL_OBSERVED_JSON="$(observe_model_from_stream "$RUNDIR/stream.jsonl" 2>/dev/null)"
  fi
  [[ -n "$MODEL_OBSERVED_JSON" ]] || MODEL_OBSERVED_JSON='[]'
  local notify_state="pending"
  [[ "$NOTIFY" == "silent" ]] && notify_state="skipped-silent"
  [[ "$CANCEL_BY" == "self" ]] && notify_state="skipped-self-cancel"
  write_status_json "$result" "$exit_code" "$notify_state" || true

  if [[ -n "$WT_PATH" ]]; then
    if [[ "$PRESERVE_STATE" == "kept-worktree" ]]; then
      # The checkout is the recovery artifact; cleanup requires manual review.
      echo "[dispatch] DGN-1433: preservation required ($PRESERVE_NOTE) -- keeping worktree $WT_PATH for manual recovery" >&2
    else
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
    fi
  fi

  active_count_decr || true

  # Ledger end row -- existing state enum only (spec R2: no new states).
  local end_ts
  end_ts="$(date '+%Y-%m-%d %H:%M %Z')"
  case "$result" in
    completed)
      if [[ "$RUNTIME" == "cloud" ]]; then
        "$LEDGER_UPDATE" "$LEDGER_BRANCH" pending-review \
          "cloud 완료 (origin $LEDGER_BRANCH sha=${CLOUD_SHA:0:12} session=${CLOUD_SESSION_ID:-?})" --ts "$end_ts" || true
      elif [[ "$COMMIT_COUNT" -gt 0 ]]; then
        "$LEDGER_UPDATE" "$LEDGER_BRANCH" pending-review \
          "dispatch 완료 (commits=$COMMIT_COUNT, HEAD=$COMMIT_HASH)" --ts "$end_ts" || true
      else
        "$LEDGER_UPDATE" "$LEDGER_BRANCH" deferred \
          "dispatch 보고서형 완료 (out.log)" --flag dsp-report --ts "$end_ts" || true
      fi
      ;;
    timed-out)
      local _to_note="dispatch timeout (${TIMEOUT_MIN}m 초과)"
      [[ "$RUNTIME" == "cloud" ]] && _to_note="cloud timeout (${TIMEOUT_MIN}m 내 브랜치 미푸시, session=${CLOUD_SESSION_ID:-?})"
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed "$_to_note" --ts "$end_ts" || true
      ;;
    canceled)
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed \
        "dispatch canceled" --ts "$end_ts" || true
      ;;
    *)
      local _fail_note="dispatch 실패 (exit=$exit_code)"
      # DGN-1726 r2: a cloud failure names its reason on the row. The ledger
      # row is pipe-delimited -- no '|' from a remote reason.
      [[ "$RUNTIME" == "cloud" ]] && _fail_note="cloud 실패 (session=${CLOUD_SESSION_ID:-none}): $(printf '%s' "${CLOUD_FAIL_REASON:-exit=$exit_code}" | tr '|\n' '/ ' | cut -c1-160)"
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed \
        "$_fail_note" --ts "$end_ts" || true
      ;;
  esac

  # DGN-1012: session return path -- unconditional (see write_session_inbox
  # header: silent gates the OWNER push below, never this drop). A failed
  # drop must not break the exit path, but it may not fail silently either:
  # the "failed" trace lands in status.json (session_inbox field).
  INBOX_STATE="dropped"
  if write_session_inbox "$result" "$exit_code"; then
    # DGN-1012 terminal-state registration: the inbox drop IS the terminal
    # signal, so the obligation opened at spawn closes with it. A FAILED drop
    # deliberately leaves the obligation open -- the hourly sweep then expires
    # it and the owner agent still learns the run ended (silence-proof ordering).
    local _tsl_state="failed"
    [[ "$result" == "completed" ]] && _tsl_state="done"
    python3 "$SCRIPT_DIR/terminal-state-ledger.py" close --id "$RUNID" \
      --state "$_tsl_state" --note "dispatch $result (exit=$exit_code)" \
      --evidence "$RUNDIR/out.log" >/dev/null || true
  else
    INBOX_STATE="failed"
  fi
  write_status_json "$result" "$exit_code" "$notify_state" || true

  # Push: issued by THIS shell, never by the model (spec R4).
  # A self-canceled run is deliberately quiet on the owner surface.  Its
  # session-inbox record remains so the dispatching agent can recover any
  # artifacts, but that record is injected as a quiet recovery turn.
  if [[ "$NOTIFY" != "silent" && "$CANCEL_BY" != "self" ]]; then
    # The previous text put "dispatch <result>: <job-label> (runid dsp-...)"
    # plus branch + commit count on the owner's phone: internal mechanics in a
    # language this instance does not speak to him. Every IDENTIFIER is still
    # gone -- runid / machine label / ticket id / branch / commit count all
    # stay in status.json, the ledger row and the session-inbox drop above,
    # which are the surfaces the maintainer and the agent read.
    #
    # What comes BACK is the readable name. The first pass stripped every name
    # from this notice along with the identifiers; the owner then ruled the
    # opposite, and his reason is that a notice carrying a name he can read is
    # a notice he can actually read -- stripping the name while keeping the
    # notice was incoherent. The name rendered here is the display name the
    # spawn path already records (run.env DISPLAY_NAME / status.json
    # display_name), selected through owner_facing_name() so the machine label
    # can never reach a non-English owner surface. resolve_owner_lang must run
    # first: the worker sources run.env, which carries no locale, so without
    # it the selector would sit on its English default and render the label.
    resolve_owner_lang
    local push_name push_text
    push_name="$(owner_facing_name)"
    # Block shapes and their owner-approval records: end_notice_text.
    local end_elapsed=""
    if [[ "$result" == completed ]]; then
      end_elapsed="$(elapsed_seconds_since_start)" || end_elapsed=""
    fi
    push_text="$(end_notice_text "$result" "$push_name" "$(date +%H:%M)" "$end_elapsed")"
    if "$PUSH_SH" --text "$push_text" --audience owner; then
      write_status_json "$result" "$exit_code" "sent" || true
    else
      write_status_json "$result" "$exit_code" "notify-failed" || true
    fi
  fi
  return 0
}

# bash-level timeout fallback (macOS ships no timeout(1)): job control gives
# the child its OWN pgid (set -m), so the watchdog can TERM the claude tree
# without killing this worker. Child pgid is recorded for --cancel.
# Returns child's exit code; 124 on timeout.
# DGN-1192: render out.log from the FULL stream.
# Contract: out.log stays the single artifact every downstream consumer reads
# (status.json out_log, session-inbox note, size check). Only its PRODUCER
# changes -- last-message capture -> concatenation of every assistant text
# block in order. Thinking blocks are excluded (not report content).
# Fail-open by design: any parse problem falls back to the raw stream + stderr
# so a rendering bug can never destroy evidence -- that is the very defect
# this function exists to remove.
# DGN-1444: stream format is one of the 4 axes that differ per runtime --
# this dispatcher is the fork point; both renderers keep the SAME contract
# (DGN-1192: fail-open to the raw stream on any parse problem, thinking/tool
# blocks excluded, out.log is the one artifact every downstream consumer
# reads) so dispatch-return and the owner-facing report stay identical shape
# across runtimes.
render_out_log() {
  # DGN-1726: the cloud worker writes out.log itself (launch output + result);
  # there is no local stream to render.
  if [[ "$RUNTIME" == "cloud" ]]; then
    [[ -f "$RUNDIR/out.log" ]] || : > "$RUNDIR/out.log"
  elif [[ "$RUNTIME" == "codex" ]]; then
    render_out_log_codex
  else
    render_out_log_claude
  fi
}

render_out_log_claude() {
  local stream="$RUNDIR/stream.jsonl" err="$RUNDIR/claude.err" out="$RUNDIR/out.log"
  [[ -f "$stream" ]] || { : > "$out"; [[ -s "$err" ]] && cat "$err" >> "$out"; return 0; }
  if ! RO_STREAM="$stream" RO_OUT="$out" python3 - <<'PY_RENDER'
import json, os, sys
src, dst = os.environ["RO_STREAM"], os.environ["RO_OUT"]
parts, seen_any = [], False
with open(src, "r", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except Exception:
            continue
        if doc.get("type") != "assistant":
            continue
        msg = doc.get("message")
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                if text.strip():
                    parts.append(text)
                    seen_any = True
if not seen_any:
    sys.exit(3)
with open(dst, "w") as fh:
    fh.write("\n\n".join(parts).rstrip() + "\n")
PY_RENDER
  then
    # No assistant text recovered (or renderer failed): keep EVERYTHING.
    {
      echo "[dispatch] out.log render found no assistant text -- raw stream follows (DGN-1192 fallback)"
      cat "$stream"
      [[ -s "$err" ]] && { echo; echo "--- stderr ---"; cat "$err"; }
    } > "$out"
    return 0
  fi
  if [[ -s "$err" ]]; then
    { echo; echo "--- stderr ---"; cat "$err"; } >> "$out"
  fi
  return 0
}

# codex --json emits {"type":"item.completed","item":{"type":"agent_message",
# "text":...}} for final agent-facing text (measured 2026-09-13) -- the
# analog of claude's assistant/text blocks. command_execution / reasoning
# items are excluded, same exclusion as claude's tool_use/thinking blocks.
render_out_log_codex() {
  local stream="$RUNDIR/stream.jsonl" err="$RUNDIR/claude.err" out="$RUNDIR/out.log"
  [[ -f "$stream" ]] || { : > "$out"; [[ -s "$err" ]] && cat "$err" >> "$out"; return 0; }
  if ! RO_STREAM="$stream" RO_OUT="$out" python3 - <<'PY_RENDER'
import json, os, sys
src, dst = os.environ["RO_STREAM"], os.environ["RO_OUT"]
parts, seen_any = [], False
with open(src, "r", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except Exception:
            continue
        if doc.get("type") != "item.completed":
            continue
        item = doc.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text") or ""
        if text.strip():
            parts.append(text)
            seen_any = True
if not seen_any:
    sys.exit(3)
with open(dst, "w") as fh:
    fh.write("\n\n".join(parts).rstrip() + "\n")
PY_RENDER
  then
    {
      echo "[dispatch] out.log render found no agent_message text -- raw stream follows (DGN-1192 fallback)"
      cat "$stream"
      [[ -s "$err" ]] && { echo; echo "--- stderr ---"; cat "$err"; }
    } > "$out"
    return 0
  fi
  if [[ -s "$err" ]]; then
    { echo; echo "--- stderr ---"; cat "$err"; } >> "$out"
  fi
  return 0
}

# DGN-1701: bridge secrets must not reach the worker's runtime. A dispatch
# launched from a live bridge session inherits that session's env, which
# carries the real TELEGRAM_BOT_TOKEN (rehearsal 1: worker saw it and had to
# `env -i` it away itself). Strip LIST, not `env -i`: claude/codex need PATH,
# HOME, TMPDIR and their own auth (ANTHROPIC_*, CLAUDE_CODE_*, OPENAI_*,
# CODEX_*) untouched. Names come from bridge/.env.example:
#   TELEGRAM_BOT_TOKEN  bot credential
#   ALLOWED_USER_IDS    owner Telegram identity (access-control list)
# plus every exported TELEGRAM_* var (future bridge credentials). Add a new
# bridge credential name here when .env.example grows one. push.sh reads the
# token from the instance .env FILE, not the env, so sends are unaffected.
WORKER_ENV_STRIP_NAMES=(TELEGRAM_BOT_TOKEN ALLOWED_USER_IDS)

# worker_env_strip_args -- fills WORKER_ENV_UNSET with `-u NAME` pairs for
# `env`, consumed at BOTH spawn sites in do_worker (claude-code and codex).
worker_env_strip_args() {
  local name
  WORKER_ENV_UNSET=()
  for name in "${WORKER_ENV_STRIP_NAMES[@]}"; do
    WORKER_ENV_UNSET+=(-u "$name")
  done
  while IFS= read -r name; do
    [[ "$name" == TELEGRAM_* ]] || continue
    [[ " ${WORKER_ENV_STRIP_NAMES[*]} " == *" $name "* ]] && continue
    WORKER_ENV_UNSET+=(-u "$name")
  done < <(compgen -e)
}

run_claude_with_deadline() {
  local secs="$1"; shift
  local timeout_bin=""
  if command -v timeout >/dev/null 2>&1; then
    timeout_bin="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then
    timeout_bin="gtimeout"
  fi

  local rc=0
  if [[ -n "$timeout_bin" ]]; then
    ( cd "$WORKDIR" && "$timeout_bin" "${secs}s" "$@" ) \
      < /dev/null > "$RUNDIR/stream.jsonl" 2> "$RUNDIR/claude.err" || rc=$?
    return $rc
  fi

  set -m
  ( cd "$WORKDIR" && "$@" ) < /dev/null > "$RUNDIR/stream.jsonl" 2> "$RUNDIR/claude.err" &
  local cpid=$!
  set +m
  printf '%s\n' "$cpid" > "$RUNDIR/claude.pid"

  local waited=0
  while kill -0 "$cpid" 2>/dev/null; do
    if [[ $waited -ge $secs ]]; then
      kill -TERM -- "-$cpid" 2>/dev/null || true
      sleep 5
      kill -KILL -- "-$cpid" 2>/dev/null || true
      wait "$cpid" 2>/dev/null || true
      rm -f "$RUNDIR/claude.pid"
      return 124
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  wait "$cpid" || rc=$?
  rm -f "$RUNDIR/claude.pid"
  return $rc
}

# EXIT backstop (self-grill G9): if the worker shell dies early (set -e trip,
# missing file, unexpected error) BEFORE the normal finalize, the run would
# leak (running ledger row + held count slot) with no owner. The .finalized
# guard makes this a no-op on every healthy path.
worker_on_exit() {
  if [[ -n "${RUNDIR:-}" && -d "$RUNDIR" && ! -d "$RUNDIR/.finalized" ]]; then
    finalize_run error 1 || true
  fi
}

worker_on_term() {
  # Cancel path: reap the claude subtree (own pgid in fallback mode), then
  # finalize as canceled. Idempotent via finalize guard.
  if [[ -f "$RUNDIR/claude.pid" ]]; then
    local cpid
    cpid="$(cat "$RUNDIR/claude.pid" 2>/dev/null || echo '')"
    if [[ -n "$cpid" ]]; then
      kill -TERM -- "-$cpid" 2>/dev/null || true
      sleep 2
      kill -KILL -- "-$cpid" 2>/dev/null || true
    fi
  fi
  render_out_log || true   # DGN-1192: cancel path must keep partial evidence
  finalize_run canceled 143
  exit 143
}

do_worker() {
  RUNDIR="$WORKER_RUNDIR"
  if [[ ! -d "$RUNDIR" || ! -f "$RUNDIR/run.env" ]]; then
    echo "[dispatch-worker] bad rundir: $RUNDIR" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$RUNDIR/run.env"

  printf '%s\n' "$$" > "$RUNDIR/pid"
  # Spawn-time process identity for --cancel pid-reuse verification (spec R5).
  ps -o lstart=,command= -p "$$" > "$RUNDIR/proc.txt" 2>/dev/null || true
  STARTED_AT="$(date +%Y-%m-%dT%H:%M:%S%z)"

  trap worker_on_exit EXIT
  trap worker_on_term TERM

  local prompt backstop
  prompt="$(cat "$RUNDIR/prompt.md")"
  backstop="$(worker_backstop)"

  # DGN-1444: RUNTIME here is the EFFECTIVE runtime resolved at spawn time
  # (run_gates()/codex_quota_gate() already ran and may have flipped it to
  # claude-code) -- run.env carries the decision, this branch just executes it.
  local claude_rc=0
  worker_env_strip_args   # DGN-1701: bridge secrets out of the child env
  if [[ "$RUNTIME" == "cloud" ]]; then
    do_worker_cloud   # never returns: finalizes and exits
  fi
  if [[ "$RUNTIME" == "codex" ]]; then
    # Worktree: existing internal sandbox. Observe: outer seatbelt only;
    # select_codex_tool_policy refuses a missing profile or sandbox binary.
    if ! select_codex_tool_policy; then
      finalize_run error 2
      exit 2
    fi
    local codex_slug
    if ! codex_slug="$(codex_slug_for_model "$MODEL")"; then
      echo "[dispatch-worker] no codex slug mapping for model '$MODEL' -- refusing" >&2
      finalize_run error 1
      exit 1
    fi
    # DGN-1770: reasoning effort, same rule as the spawn banner.
    local codex_effort_args=()
    if [[ -n "$EFFORT" ]] && effort_passed_to_runtime codex "$EFFORT"; then
      codex_effort_args=(-c "model_reasoning_effort=$EFFORT")
    fi
    # codex exec has no --append-system-prompt equivalent (checked
    # 2026-09-13: no system/developer-instructions flag in `codex exec
    # --help`, and codex's own instruction-file convention is AGENTS.md
    # picked up from cwd -- writing into that file is out of scope here, it
    # is a baseline doc this estate never hand-patches). The backstop is
    # therefore INLINED into the user-turn prompt instead: functionally
    # equivalent delivery (the model sees it in the same turn either way),
    # just not a separate system-role channel.
    local codex_prompt
    codex_prompt="$(printf '%s\n\n---\n\n%s' "$backstop" "$prompt")"
    run_claude_with_deadline "$(( TIMEOUT_MIN * 60 ))" \
      ${WRAP[@]+"${WRAP[@]}"} env -u CLAUDECODE -u PROJECT_ROOT -u DOGANY_BRIDGE_SESSION \
        ${WORKER_ENV_UNSET[@]+"${WORKER_ENV_UNSET[@]}"} \
        DOGANY_CODEX_ADAPTER_ROOT="$CODEX_ADAPTER_ROOT" codex exec \
        -m "$codex_slug" \
        ${codex_effort_args[@]+"${codex_effort_args[@]}"} \
        --json \
        "${CODEX_ARGS[@]}" \
        "$codex_prompt" \
      || claude_rc=$?
  else
    # Tool policy + execution wrapper: worktree mode = full build perms (loop
    # parity, unchanged); observe mode = kernel sandbox + open Bash (header
    # "OBSERVE MODE"). The worker never re-renders the profile: it was
    # rendered and self-checked by the launcher and lives under WORKSPACE,
    # which the sandbox denies.
    if ! select_tool_policy; then
      finalize_run error 2
      exit 2
    fi

    # -u CLAUDECODE: nested claude -p is rejected inside a session env.
    # -u PROJECT_ROOT: child usage-gate misclassification guard (spec step 5).
    # -u DOGANY_BRIDGE_SESSION (DGN-1586 spec 3.3): a detached worker spawned
    # FROM a bridge session would otherwise inherit the bridge surface marker
    # and version-check.py would treat the worker as a bridge surface.
    run_claude_with_deadline "$(( TIMEOUT_MIN * 60 ))" \
      ${WRAP[@]+"${WRAP[@]}"} env -u CLAUDECODE -u PROJECT_ROOT -u DOGANY_BRIDGE_SESSION \
        ${WORKER_ENV_UNSET[@]+"${WORKER_ENV_UNSET[@]}"} claude -p "$prompt" \
        --model "$MODEL" \
        ${EFFORT:+--effort "$EFFORT"} \
        --append-system-prompt "$backstop" \
        --output-format stream-json --verbose \
        "${TOOL_ARGS[@]}" \
      || claude_rc=$?
  fi

  # DGN-1192: out.log is RENDERED from the full stream, not captured as the
  # last message. `claude -p` in text mode prints only the FINAL assistant
  # message; a worker that split its report across two messages lost the
  # first one silently, and a truncated report always reads as "this model
  # was not good enough" -- poisoning DGN-1193 tier measurement upward.
  render_out_log

  trap - TERM

  if [[ $claude_rc -eq 124 ]]; then
    finalize_run timed-out 124
  elif [[ $claude_rc -eq 0 ]]; then
    finalize_run completed 0
  else
    # DGN-1158: this dispatch died -- if it was fable's weekly-cap death,
    # write the flag so the NEXT fable spawn's run_gates() demotes to opus.
    # No auto-retry of THIS run.
    if is_fable_credits_death "$RUNDIR/out.log"; then
      echo "[dispatch-worker] fable weekly cap exhausted (out.log: requires usage credits) -> recording $FABLE_FLAG so the next fable spawn demotes (DGN-1158)"
      write_fable_exhausted_marker || echo "[dispatch-worker] failed to write $FABLE_FLAG (non-fatal)" >&2
    fi
    finalize_run error "$claude_rc"
  fi
}

# DGN-1726 cloud worker (header, CLOUD RUNTIME): start the routine, then
# WATCH origin for the result branch and the run for its end. Same detached
# process, same pid file, same traps and finalize as a local worker -- so
# --cancel, the work-board liveness check and the terminal-state sweep all
# apply unchanged. Every failure finalizes AT ONCE with a reason line
# (CLOUD_FAIL_REASON -> ledger note, dispatch-return, status.json).

# The worker's findings, rewritten whole on every change (finalize and
# --cancel source it).
cloud_env_write() {
  {
    printf 'CLOUD_TRIGGER_ID=%q\n'       "$CLOUD_TRIGGER_ID"
    printf 'CLOUD_TRIGGER_DISABLED=%q\n' "$CLOUD_TRIGGER_DISABLED"
    printf 'CLOUD_SESSION_ID=%q\n'       "$CLOUD_SESSION_ID"
    printf 'CLOUD_VIEW_URL=%q\n'         "$CLOUD_VIEW_URL"
    printf 'CLOUD_SHA=%q\n'              "$CLOUD_SHA"
    printf 'CLOUD_FAIL_REASON=%q\n'      "$CLOUD_FAIL_REASON"
  } > "$RUNDIR/cloud.env.tmp" && mv -f "$RUNDIR/cloud.env.tmp" "$RUNDIR/cloud.env"
}

# cloud_routine_call <action> [args...] -- one transport call; its key=value
# stdout lands in RUNDIR/cloud-routine.out (stderr appended to .err).
cloud_routine_call() {
  env -u CLAUDECODE -u PROJECT_ROOT -u DOGANY_BRIDGE_SESSION \
    ${WORKER_ENV_UNSET[@]+"${WORKER_ENV_UNSET[@]}"} \
    "$CLOUD_ROUTINE" "$@" --cwd "$RUNDIR" \
    < /dev/null > "$RUNDIR/cloud-routine.out" 2>> "$RUNDIR/cloud-routine.err"
}

cloud_routine_kv() {  # <key> -- first value of key= in the last call's output
  sed -n "s/^$1=//p" "$RUNDIR/cloud-routine.out" 2>/dev/null | head -1
}

# Disable the routine unless already confirmed disabled (a still-enabled
# one-shot routine fires a second session at run_once_at). Never fatal.
cloud_disable_routine() {
  [[ -n "$CLOUD_TRIGGER_ID" && "$CLOUD_TRIGGER_DISABLED" != "yes" ]] || return 0
  if cloud_routine_call disable --trigger-id "$CLOUD_TRIGGER_ID" \
     && [[ "$(cloud_routine_kv disabled)" == "yes" ]]; then
    CLOUD_TRIGGER_DISABLED=yes
    echo "[cloud] routine $CLOUD_TRIGGER_ID disabled" >> "$RUNDIR/out.log"
  else
    echo "[cloud] WARNING: could not disable routine $CLOUD_TRIGGER_ID ($(cloud_routine_kv error)) -- it may fire again at its run_once_at; disable it on claude.ai" >> "$RUNDIR/out.log"
  fi
  cloud_env_write
}

do_worker_cloud() {
  local slug="" rc=0 deadline now left sha="" rrc next_status state status reason ended=0
  deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
  : > "$RUNDIR/out.log"
  if ! slug="$(model_display_binding cloud "$MODEL" 3)" || [[ -z "$slug" ]]; then
    CLOUD_FAIL_REASON="no cloud model id for '$MODEL' in $MODEL_DISPLAY_BINDINGS"
    echo "[dispatch-worker] $CLOUD_FAIL_REASON -- refusing" | tee -a "$RUNDIR/out.log" >&2
    cloud_env_write
    finalize_run error 1
    exit 1
  fi
  echo "[cloud] starting routine: $CLOUD_REPO_URL (env $CLOUD_ENV_ID, model $slug, base origin/$WT_BASE, result $BRANCH)" >> "$RUNDIR/out.log"
  cloud_routine_call start --name "dispatch $LABEL ($RUNID)" --repo-url "$CLOUD_REPO_URL" \
    --env-id "$CLOUD_ENV_ID" --model "$slug" --task-file "$RUNDIR/prompt.md" || rc=$?
  CLOUD_TRIGGER_ID="$(cloud_routine_kv trigger_id)"
  CLOUD_TRIGGER_DISABLED="$(cloud_routine_kv disabled)"
  CLOUD_SESSION_ID="$(cloud_routine_kv session_id)"
  CLOUD_VIEW_URL="$(cloud_routine_kv view)"
  if [[ $rc -ne 0 || -z "$CLOUD_SESSION_ID" ]]; then
    # reason= (e.g. "task text mismatch after create (sha a vs b)") is shown
    # verbatim; a routine that failed verification was disabled, never run.
    CLOUD_FAIL_REASON="$(cloud_routine_kv reason)"
    if [[ -z "$CLOUD_FAIL_REASON" ]]; then
      CLOUD_FAIL_REASON="routine create/run failed: $(cloud_routine_kv error)"
      [[ "$CLOUD_FAIL_REASON" == *": " ]] && CLOUD_FAIL_REASON="${CLOUD_FAIL_REASON}rc=$rc, no session id"
    fi
    {
      echo "[cloud] $CLOUD_FAIL_REASON"
      tail -5 "$RUNDIR/cloud-routine.err" 2>/dev/null
    } >> "$RUNDIR/out.log"
    cloud_env_write
    cloud_disable_routine
    finalize_run error "$(( rc == 0 ? 1 : rc ))"
    exit 1
  fi
  cloud_env_write
  {
    echo "[cloud] routine: $CLOUD_TRIGGER_ID (disabled after run: ${CLOUD_TRIGGER_DISABLED:-no})"
    echo "[cloud] session: $CLOUD_SESSION_ID"
    echo "[cloud] view:    $CLOUD_VIEW_URL"
    echo "[cloud] waiting for origin $BRANCH (timeout ${TIMEOUT_MIN}m, branch poll ${CLOUD_POLL_SECS}s, run-status poll ${CLOUD_STATUS_SECS}s)"
  } >> "$RUNDIR/out.log"
  # The running row names the session while it is still running (the model=
  # / runtime= tokens stay first: status-footer parses them).
  "$LEDGER_UPDATE" "$LEDGER_BRANCH" running \
    "dispatch 스폰 (model=$MODEL runtime=cloud axis=$BOTTLENECK session=$CLOUD_SESSION_ID)" || true
  # A failed post-run disable is retried now, not only at the end.
  cloud_disable_routine

  next_status=$(( $(date +%s) + CLOUD_STATUS_SECS ))
  while :; do
    rrc=0
    sha="$(cloud_remote_sha "$WORKDIR" "$BRANCH")" || rrc=$?
    [[ $rrc -eq 0 && -n "$sha" ]] && break
    [[ $rrc -ne 0 ]] && echo "[cloud] $(date +%H:%M:%S) origin read failed -- retrying" >> "$RUNDIR/out.log"
    now="$(date +%s)"
    if [[ $now -ge $next_status ]]; then
      next_status=$(( now + CLOUD_STATUS_SECS ))
      if cloud_routine_call status --trigger-id "$CLOUD_TRIGGER_ID" --session-id "$CLOUD_SESSION_ID"; then
        state="$(cloud_routine_kv state)"; status="$(cloud_routine_kv status)"
        echo "[cloud] $(date +%H:%M:%S) run status: ${status:-?} (${state:-unknown})" >> "$RUNDIR/out.log"
        if [[ "$state" == "ended" ]]; then
          # The push may have landed between the two reads -- look once more.
          sha="$(cloud_remote_sha "$WORKDIR" "$BRANCH" 2>/dev/null)" || sha=""
          [[ -n "$sha" ]] && break
          reason="$(cloud_routine_kv reason)"
          CLOUD_FAIL_REASON="session ended (${status:-?}) without pushing $BRANCH${reason:+: $reason}"
          ended=1
          break
        fi
      else
        echo "[cloud] $(date +%H:%M:%S) run-status read failed ($(cloud_routine_kv error)) -- the branch poll and the timeout still apply" >> "$RUNDIR/out.log"
      fi
    fi
    now="$(date +%s)"
    left=$(( deadline - now ))
    [[ $left -gt 0 ]] || break
    sleep "$(( left < CLOUD_POLL_SECS ? left : CLOUD_POLL_SECS ))"
  done
  trap - TERM
  cloud_disable_routine
  if [[ -n "$sha" ]]; then
    CLOUD_SHA="$sha"
    cloud_env_write
    echo "[cloud] arrived: origin $BRANCH $CLOUD_SHA" >> "$RUNDIR/out.log"
    finalize_run completed 0
    exit 0
  fi
  if [[ $ended -eq 1 ]]; then
    echo "[cloud] FAILED: $CLOUD_FAIL_REASON" >> "$RUNDIR/out.log"
    cloud_env_write
    finalize_run error 1
    exit 1
  fi
  CLOUD_FAIL_REASON="$BRANCH not on origin after ${TIMEOUT_MIN}m (the remote session may still be running)"
  echo "[cloud] timeout: $CLOUD_FAIL_REASON" >> "$RUNDIR/out.log"
  cloud_env_write
  finalize_run timed-out 124
  exit 124
}

# ---------------------------------------------------------------------------
# Cancel: verify process identity (pid-reuse guard, spec R5) -> TERM the
# worker's process group -> 5s grace -> KILL. Worker's TERM trap does the
# orderly finalize; if it never runs, cancel finalizes from this side
# (idempotent via the .finalized guard).
# ---------------------------------------------------------------------------
do_cancel() {
  RUNDIR="$DISPATCH_DIR/$CANCEL_RUNID"
  if [[ ! -d "$RUNDIR" || ! -f "$RUNDIR/run.env" ]]; then
    echo "[dispatch] unknown runid: $CANCEL_RUNID" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$RUNDIR/run.env"
  local self_cancel=0
  [[ "${DOGANY_BRIDGE_SESSION:-}" == "1" ]] && self_cancel=1

  if [[ -d "$RUNDIR/.finalized" ]]; then
    echo "[dispatch] $CANCEL_RUNID already finalized -- nothing to cancel"
    exit 0
  fi

  # DGN-1726 r2: a cloud run's routine is disabled FIRST (so the finalize
  # below reports it) -- the remote session itself cannot be stopped here.
  if [[ "${RUNTIME:-}" == "cloud" && -f "$RUNDIR/cloud.env" ]]; then
    # shellcheck disable=SC1091
    source "$RUNDIR/cloud.env" || true
    cloud_disable_routine
    echo "[dispatch] cloud routine ${CLOUD_TRIGGER_ID:-?} disabled=${CLOUD_TRIGGER_DISABLED:-no}; remote session ${CLOUD_SESSION_ID:-?} keeps running -- ${CLOUD_VIEW_URL:-}"
  fi

  local pid=""
  [[ -f "$RUNDIR/pid" ]] && pid="$(cat "$RUNDIR/pid" 2>/dev/null | tr -d ' \n')"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    [[ "$self_cancel" -eq 1 ]] && record_self_cancel
    echo "[dispatch] worker not running (pid=${pid:-none}) -- finalizing bookkeeping only"
    finalize_run canceled 1
    exit 0
  fi

  # pid-reuse guard: the recorded spawn-time (lstart, command) must match the
  # live process before we send any signal (spec R5 -- never kill a stranger).
  local recorded current
  recorded="$(cat "$RUNDIR/proc.txt" 2>/dev/null | tr -s ' ' | sed 's/^ *//;s/ *$//')"
  current="$(ps -o lstart=,command= -p "$pid" 2>/dev/null | tr -s ' ' | sed 's/^ *//;s/ *$//')"
  if [[ -z "$recorded" || -z "$current" || "$recorded" != "$current" ]]; then
    echo "[dispatch] REFUSED: pid $pid identity mismatch (reused pid?)" >&2
    echo "  recorded: ${recorded:-<none>}" >&2
    echo "  current:  ${current:-<none>}" >&2
    exit 1
  fi

  # DOGANY_BRIDGE_SESSION is injected only into the dispatching owner session
  # by sdk_bridge. Record its intentional cancellation only after the target
  # passed identity verification, then hand the fact to the detached worker.
  [[ "$self_cancel" -eq 1 ]] && record_self_cancel

  # Worker is its own session/group leader (setsid) -> pgid == pid.
  echo "[dispatch] cancel $CANCEL_RUNID: TERM to process group $pid"
  kill -TERM -- "-$pid" 2>/dev/null || true

  local waited=0
  while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 5 ]]; do
    sleep 1
    waited=$(( waited + 1 ))
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "[dispatch] worker survived TERM -- escalating to KILL"
    kill -KILL -- "-$pid" 2>/dev/null || true
    # Fallback-timeout mode keeps claude in a separate pgid -- reap it too.
    if [[ -f "$RUNDIR/claude.pid" ]]; then
      local cpid
      cpid="$(cat "$RUNDIR/claude.pid" 2>/dev/null | tr -d ' \n')"
      if [[ -n "$cpid" ]]; then
        kill -KILL -- "-$cpid" 2>/dev/null || true
      fi
    fi
  fi

  # Worker trap normally finalizes; give it a moment, then backstop from here.
  sleep 2
  if [[ ! -d "$RUNDIR/.finalized" ]]; then
    finalize_run canceled 137
  fi
  echo "[dispatch] canceled: $CANCEL_RUNID"
}

# ---------------------------------------------------------------------------
# DGN-1227 (3) retroactive restore -- past runs whose stream.jsonl survived
# never got model_observed written (the feature did not exist yet). Reports
# candidates and, only with --apply, patches ONLY the missing field into
# status.json via the SAME observe_model_from_stream parser that finalize_run
# uses live. Default is dry-run -- this never runs unattended and never
# touches a status.json that already has the field or that has no
# stream.jsonl left to observe. Backfilled rows are tagged
# model_observed_backfilled=true so a reader can tell "observed live" from
# "reconstructed after the fact" apart (a backfilled run may have been
# canceled/killed mid-stream, so its stream is not necessarily as complete
# as a live-finalized one's).
# ---------------------------------------------------------------------------
# DGN-1458: --apply-plan <runid> [--apply]. The ONLY writer of the plan lane,
# run by the dispatcher outside the kernel. Dry run unless --apply.
do_apply_plan() {
  if ! [[ "$CANCEL_RUNID" =~ ^dsp-[0-9]{8}-[0-9]{6}-[0-9]{4}$ ]]; then
    echo "[dispatch] --apply-plan needs a runid (dsp-YYYYMMDD-HHMMSS-NNNN), got '$CANCEL_RUNID'" >&2
    return 2
  fi
  local rundir="$DISPATCH_DIR/$CANCEL_RUNID" targets="" t args=()
  if [[ ! -f "$rundir/run.env" ]]; then
    echo "[dispatch] --apply-plan: no run $CANCEL_RUNID under $DISPATCH_DIR" >&2
    return 2
  fi
  targets="$( LIVE_TARGETS=""; source "$rundir/run.env" >/dev/null 2>&1; printf '%s' "$LIVE_TARGETS" )"
  if [[ -z "$targets" ]]; then
    echo "[dispatch] --apply-plan: $CANCEL_RUNID is not a plan-lane run (spawned without --live-target)" >&2
    return 2
  fi
  while IFS= read -r t; do [[ -n "$t" ]] && args+=(--target "$t"); done <<<"$targets"
  # --apply is the shared flag (it also arms --backfill-model-observed).
  [[ "$BACKFILL_APPLY" == 1 ]] && args+=(--apply)
  python3 "$SCRIPT_DIR/plan-apply.py" --rundir "$rundir" "${args[@]}"
}

do_backfill() {
  if [[ ! -d "$DISPATCH_DIR" ]]; then
    echo "[backfill] no dispatches yet"
    return 0
  fi
  local total=0 already=0 nostream=0 candidates=0 patched=0 failed=0
  local d runid status_file stream_file observed has_field
  for d in "$DISPATCH_DIR"/dsp-*/; do
    [[ -d "$d" ]] || continue
    status_file="${d}status.json"
    stream_file="${d}stream.jsonl"
    [[ -f "$status_file" ]] || continue
    total=$(( total + 1 ))
    runid="$(basename "$d")"
    has_field="$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print("1" if isinstance(d, dict) and "model_observed" in d else "0")
except Exception:
    print("0")
' "$status_file" 2>/dev/null || echo 0)"
    if [[ "$has_field" == "1" ]]; then
      already=$(( already + 1 ))
      continue
    fi
    if [[ ! -f "$stream_file" ]]; then
      nostream=$(( nostream + 1 ))
      continue
    fi
    candidates=$(( candidates + 1 ))
    observed="$(observe_model_from_stream "$stream_file" 2>/dev/null)"
    [[ -n "$observed" ]] || observed='[]'
    if [[ "$BACKFILL_APPLY" -eq 1 ]]; then
      if RB_OBSERVED="$observed" python3 - "$status_file" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
except Exception as e:
    print(f"[backfill] SKIP unreadable {path}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    observed = json.loads(os.environ.get("RB_OBSERVED", "[]") or "[]")
    if not isinstance(observed, list):
        observed = []
except Exception:
    observed = []
doc["model_observed"] = observed
doc["model_observed_backfilled"] = True
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
      then
        patched=$(( patched + 1 ))
        echo "[backfill] APPLIED $runid -> $observed"
      else
        failed=$(( failed + 1 ))
        echo "[backfill] FAILED  $runid" >&2
      fi
    else
      echo "[backfill] WOULD-APPLY $runid -> $observed"
    fi
  done
  echo "[backfill] total=$total already_has_field=$already no_stream_left=$nostream candidates=$candidates"
  if [[ "$BACKFILL_APPLY" -eq 1 ]]; then
    echo "[backfill] patched=$patched failed=$failed"
  else
    echo "[backfill] dry-run -- pass --apply to write model_observed into these $candidates run(s)"
  fi
}

# ---------------------------------------------------------------------------
# List: one line per run dir -- state from status.json, else live pid check.
# ---------------------------------------------------------------------------
do_list() {
  if [[ ! -d "$DISPATCH_DIR" ]]; then
    echo "[dispatch] no dispatches yet"
    return 0
  fi
  local found=0 d runid state pid label
  for d in "$DISPATCH_DIR"/dsp-*/; do
    [[ -d "$d" ]] || continue
    found=1
    runid="$(basename "$d")"
    label=""
    if [[ -f "$d/run.env" ]]; then
      # run.env values are printf %q quoted -- unquote via subshell source.
      label="$( ( source "$d/run.env" 2>/dev/null && printf '%s' "${LABEL:-}" ) || true )"
    fi
    if [[ -f "$d/status.json" ]]; then
      state="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("result","?"))' "$d/status.json" 2>/dev/null || echo '?')"
    else
      pid="$(cat "$d/pid" 2>/dev/null | tr -d ' \n')"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        state="running (pid $pid)"
      else
        state="dead-unfinalized"
      fi
    fi
    printf '%s  %-22s  %s\n' "$runid" "$state" "$label"
  done
  if [[ $found -eq 0 ]]; then
    echo "[dispatch] no dispatches yet"
  fi
}

main() {
  case "$MODE" in
    spawn)       do_spawn ;;
    cancel)      do_cancel ;;
    list)        do_list ;;
    calibration) print_calibration ;;
    worker)      do_worker ;;
    backfill)    do_backfill ;;
    apply-plan)  do_apply_plan ;;
    start-notice-flush) do_start_notice_flush ;;
    bg-start-notice) do_bg_start_notice ;;
    bg-end-notice) do_bg_end_notice ;;
  esac
}

# Sourcing guard for tests: DISPATCH_TEST=1 loads functions without executing.
if [[ -z "${DISPATCH_TEST:-}" ]]; then
  main
fi
