<!-- AGENT-OPS.md -- framework ops reference (framework-owned; installed at
mint, refreshed by update.sh channel 3k2 with edit-detect + backup). COLD
doc: read on demand via the AGENT.md Ops pointer, never hot-injected.
PLACEHOLDER CONTRACT: this file may carry ONLY the project-root path
token (dunder-underscore style; substituted to this instance's absolute
root at install). Identity-gated placeholder tokens (agent name / label /
user label / prefix / language) are FORBIDDEN here, even as literal
examples -- substitution must stay deterministic when the instance
identity manifest is absent. update.sh asserts this contract mechanically;
this comment is documentation, not the enforcement. -->

# AGENT-OPS -- framework ops procedures

## 1. Self-restart (bridge restart with completion notice)
- Command (flagless -- prefix and launchd label are baked in at mint):
  `__PROJECT_ROOT__/bridge/self_restart.sh --reason "<why>"`
- What it does: nohup detach -> delayed SIGTERM (launchd KeepAlive brings the
  bridge back with new code) -> poll the bot log for the "Bot is running"
  marker -> Telegram completion notice. The user should never have to ask
  "did it work?" -- the result arrives first.
- Optional flags:
  - `--verify "<prompt>"`: headless claude check after restart; result
    attached to the notice.
  - `--dry-run`: exercises the wait + notify path without killing the bridge.
  - `--notice "<text>"`: user-facing notice body in persona voice (no pid, no
    dev jargon); `--reason` stays log-only when set. For a version-update
    restart, compose it release-note style (what changed for the user).
  - `--label <label>` / `--prefix <emoji>` / `--env <path>`: overrides only;
    the baked defaults are already correct for this instance.
- Zombie-poll rule: if the polling marker does not appear within 60s, treat
  the restart as zombie-poll and push a warning.
- NOTE: the script has NO --help (unknown args exit 3). Full flag semantics
  live in the script's own usage header comment -- read them there.

## 2. Self-update (consume a framework release)
- Command: `__PROJECT_ROOT__/routines/self-update.sh` (zero-arg; resolves
  this instance's own root, pulls the framework repo to the latest release
  tag, then runs update.sh --root <self> --yes).
- Update != release: this CONSUMES an already-published framework release
  into this instance. Never bump VERSION and never create a tag as part of
  "update yourself" -- that is release.sh, a separate maintainer act.

## 3. Subagent dispatch routing
- baseline-editor (.claude/agents/baseline-editor.md): any edit to AGENT.md
  or any SKILL.md (and other baseline docs); the main session never edits
  these inline.
- propagation-editor (.claude/agents/propagation-editor.md): any baseline
  infra change (bridge / memory-engine / routines / cron / input handlers)
  or multi-repo product work; it owns the full propagation ruleset (all live
  instances + template + OSS sync, product-repo curation, cross-agent DB
  lockstep). Fixing only one copy = incomplete.
- release-closer (.claude/agents/release-closer.md): release-close ledger
  work (CHANGELOG + releases/vX.md + backlog reconcile).
- Release approval gate: actual release.sh run / tag / push stays behind
  user approval and is executed by the main session -- the subagents stop at
  diffs and ledger edits.
- These routings arm only when this instance takes on that stewardship; a
  plain general instance may never invoke them, but the defs stay routable.

## 4. Upstream reporting
- Framework defects, procedure deviations, and improvement ideas ride the
  dogany-upstream-report skill (standard proposal format + repo routing).
  Never hand-patch framework code locally -- report upstream and consume the
  fix via self-update.
