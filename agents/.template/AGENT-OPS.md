<!-- AGENT-OPS.md -- framework ops reference (cold: read on demand, never hot-injected).
PLACEHOLDER CONTRACT: this file may carry ONLY the PROJECT_ROOT (and HOME) mint
placeholders. NO identity-gated placeholders (agent name / prefix / label / user
label / lang): substitution must be deterministic even when IDENTITY_OK=0.
update.sh section 3k2 mechanically asserts this on every refresh. -->

# AGENT-OPS -- framework operations reference

Cold reference for framework procedures. Read this when you need the full
mechanics; the hot pointer in AGENT.md carries only the entry points and the
emergency breadcrumbs.

## 1. Self-restart

Base command (flagless -- LABEL and PREFIX are baked as instance defaults at mint):

    __PROJECT_ROOT__/bridge/self_restart.sh --reason "<why>"

What it does: nohup detach -> delayed SIGTERM (KeepAlive relaunches the new code)
-> poll for the "Bot is running" marker -> Telegram completion notice. The result
arrives on its own; the user should not have to ask "did it work?".

Reference flags: `--verify "<prompt>"` (headless self-check attached to the notice),
`--dry-run` (test the notification path without killing the process), `--notice
"<body>"` (persona-voice user-facing text; --reason stays log-only when set),
`--label <launchd-label>` and `--prefix <emoji>` (override the baked defaults only
if ever needed), `--env <file>` (notification env).

Zombie-poll rule: if the marker does not appear within 60s, treat as zombie-poll
and push a warning. NOTE: the script has NO --help; its usage lives in the header
comment of self_restart.sh -- read that if you need semantics beyond this doc.

## 2. Self-update (framework version)

    __PROJECT_ROOT__/routines/self-update.sh

Zero-arg. Resolves this instance's own root, pulls the framework repo to the latest
release tag, then runs update.sh --root <self> --yes. This CONSUMES an already
published framework release into this instance. It is NOT a release: never a VERSION
bump, never a tag (that is release.sh, a separate maintainer act).

## 3. Moving / relocating an instance

When an instance directory is moved to a new path (relocate / rename the install
root), the launchd watchdog LaunchAgent keeps the OLD path baked into its plist
(`ProgramArguments` + log paths). Left unfixed it runs the dead old `watchdog.sh`
against the stale heartbeat and force-restarts the live bot on false stalls
(DGN-480). After ANY move, re-register the watchdog from the NEW location:

    __PROJECT_ROOT__/bridge/watchdog_setup.sh

Zero-arg, idempotent, non-fatal. It derives the current root from its own on-disk
location, boots out the old LaunchAgent, and bootstraps a plist repointed at the
new path. A plain `self-update` (section 2) also runs this step, so an update from
the new location fixes it too; run it directly when you move without updating.

## 4. Subagent dispatch routing

- baseline-editor -- any edit to AGENT.md or a SKILL.md (and other baseline docs).
  The main session never edits these inline.
- propagation-editor -- any baseline infra change (bridge / memory-engine / routines
  / cron / input handlers) or multi-repo product work. Owns the full propagation
  ruleset; fixing one copy only is incomplete.
- release-closer -- release-close ledger work (CHANGELOG + releases/vX.md + backlog
  reconcile).
- release.sh run / tag / push always stays behind user approval and is executed by
  the main session -- never a subagent, never autonomous.

These pointers arm dev-agent / product-steward workflows; a plain general instance
may never invoke them, but they must exist so the subagent defs are routable.

## 5. Upstream reporting

Framework defects / improvement proposals go through the dogany-upstream-report skill
(standard gh-issue form, routed to the correct upstream repo). Internal handling;
never open a public issue by hand.
