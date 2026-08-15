# DGN-604 M3 publish.sh -- DRY-RUN report

- canonical: `<canonical-repo-root>`  (branch: auto/dgn604-m3-publish)
- public target: remote `public` branch `main`
- version: v1.16.0 (plain framework train, no prefix tags)
- units: 22 total, 21 public+official selected
- files: export=245, repo-meta=16, f9-blocked=31, priv-blocked=1, third-party-blocked=1, symlink-escape=1, no-owner=56

## Units EXCLUDED from export (not public+official)
- `spending-log` (private/poc) -- manifest: agents/.template/.claude/skills-bundle/spending-log/product.yaml

## Export list -- paths that WOULD go public, per unit

### appointment-log
- agents/.template/.claude/skills-bundle/appointment-log/SKILL.md

### diet-log
- agents/.template/.claude/skills-bundle/diet-log/SKILL.md
- agents/.template/.claude/skills-bundle/diet-log/card.py
- agents/.template/.claude/skills-bundle/diet-log/fonts/ASDGN_ExtraBold.ttf
- agents/.template/.claude/skills-bundle/diet-log/fonts/ASDGN_Medium.ttf
- agents/.template/.claude/skills-bundle/diet-log/lookup.py

### dogany-agent-template
- agents/.template/.claude/.gitkeep
- agents/.template/.claude/agents/baseline-editor.md
- agents/.template/.claude/agents/propagation-editor.md
- agents/.template/.claude/agents/release-closer.md
- agents/.template/.claude/settings.json
- agents/.template/.claude/skills/dogany-cron-register
- agents/.template/.claude/skills/dogany-lifekit-setup
- agents/.template/.claude/skills/dogany-memory-search
- agents/.template/.claude/skills/dogany-portfolio-setup
- agents/.template/.claude/skills/dogany-proactive-push
- agents/.template/.claude/skills/dogany-relogin-rebind
- agents/.template/.claude/skills/dogany-reminder
- agents/.template/.claude/skills/dogany-skill-creator
- agents/.template/.claude/skills/dogany-user-onboarding
- agents/.template/.gitignore
- agents/.template/.telegram_bot/.env.example
- agents/.template/AGENT-OPS.md
- agents/.template/AGENT.md
- agents/.template/CLAUDE.md
- agents/.template/README.md
- agents/.template/USER.md
- agents/.template/bridge/.env.example
- agents/.template/bridge/LICENSE
- agents/.template/bridge/README.md
- agents/.template/bridge/UPSTREAM.md
- agents/.template/bridge/__init__.py
- agents/.template/bridge/__main__.py
- agents/.template/bridge/bot.py
- agents/.template/bridge/com.telegram-skill-bot.telegram-agent.newbridge.plist
- agents/.template/bridge/com.telegram-skill-bot.telegram-agent.watchdog.plist
- agents/.template/bridge/config.py
- agents/.template/bridge/dashboard.py
- agents/.template/bridge/formatting.py
- agents/.template/bridge/health.py
- agents/.template/bridge/heartbeat.py
- agents/.template/bridge/i18n/__init__.py
- agents/.template/bridge/i18n/en.py
- agents/.template/bridge/i18n/ko.py
- agents/.template/bridge/messages.py
- agents/.template/bridge/model_state.py
- agents/.template/bridge/options.py
- agents/.template/bridge/ownership.py
- agents/.template/bridge/permissions.py
- agents/.template/bridge/requirements.txt
- agents/.template/bridge/sdk_bridge.py
- agents/.template/bridge/self_restart.sh
- agents/.template/bridge/session.py
- agents/.template/bridge/start.sh
- agents/.template/bridge/streaming.py
- agents/.template/bridge/tests/__init__.py
- agents/.template/bridge/tests/conftest.py
- agents/.template/bridge/tests/test_dgn325_single_option.py
- agents/.template/bridge/tests/test_dgn372_bracket_escape.py
- agents/.template/bridge/tests/test_dgn376_register_guard.py
- agents/.template/bridge/tests/test_dgn399_stream_bootstrap.py
- agents/.template/bridge/tests/test_dgn426_interim_suppression.py
- agents/.template/bridge/tests/test_dgn494_multi_list.py
- agents/.template/bridge/tests/test_dgn515_callback_edge.py
- agents/.template/bridge/tests/test_dgn517_buffer_overflow.py
- agents/.template/bridge/tests/test_dgn519_empty_final.py
- agents/.template/bridge/tests/test_dgn555_reply_link.py
- agents/.template/bridge/tests/test_model_state.py
- agents/.template/bridge/tests/test_session_inject.py
- agents/.template/bridge/tests/test_skill_display_names.py
- agents/.template/bridge/tests/test_turn_death_safety_net.py
- agents/.template/bridge/voice.py
- agents/.template/bridge/watchdog.sh
- agents/.template/bridge/watchdog_setup.sh
- agents/.template/config/agent.conf
- agents/.template/config/i18n/en.json
- agents/.template/config/i18n/ko.json
- agents/.template/config/lifekit.conf
- agents/.template/config/secret-patterns.conf
- agents/.template/git-hooks/pre-commit
- agents/.template/memories/MEMORY.md
- agents/.template/memory-engine/CONSOLIDATION_TAXONOMY.md
- agents/.template/memory-engine/memory.py
- agents/.template/memory-engine/tests/test_dgn501_sameday_recall.py
- agents/.template/memory-engine/tests/test_retry_queue.py
- agents/.template/routines/browser-auth-open.sh
- agents/.template/routines/bundle/daily-retro.sh
- agents/.template/routines/bundle/morning-brief.sh
- agents/.template/routines/bundle/morning_brief_card.py
- agents/.template/routines/bundle/routine.plist.tpl
- agents/.template/routines/card-followup.py
- agents/.template/routines/cc-memory-write-guard.py
- agents/.template/routines/classify-inbox-check.sh
- agents/.template/routines/claude-usage.sh
- agents/.template/routines/cleanup-files.sh
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.classify-inbox-0500.plist
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.cleanup-files.plist
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.consolidate-0430.plist
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.generic-brief-morning.plist
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.generic-brief-retro.plist
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.generic-brief-weekly.plist
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.mirror-poll.plist
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.mirror-poll.service
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.mirror-poll.timer
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.mirror-reconcile.plist
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.mirror-reconcile.service
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.mirror-reconcile.timer
- agents/.template/routines/com.telegram-skill-bot.telegram-agent.routine-roller.plist
- agents/.template/routines/consolidate-0430.sh
- agents/.template/routines/cron-guard.sh
- agents/.template/routines/generic-brief.sh
- agents/.template/routines/lib/agentlib.sh
- agents/.template/routines/lib/brief-slot-ctl.sh
- agents/.template/routines/lib/design_tokens.py
- agents/.template/routines/lib/handoff-aggregate
- agents/.template/routines/lib/portfolio-core-lint.py
- agents/.template/routines/lib/portfolio-core-parse.sh
- agents/.template/routines/lib/routine-ctl.sh
- agents/.template/routines/lib/run-notify.sh
- agents/.template/routines/mirror-poll.sh
- agents/.template/routines/mirror-reconcile.sh
- agents/.template/routines/mirror-setup-check.sh
- agents/.template/routines/onboarding-check.py
- agents/.template/routines/plists.defer
- agents/.template/routines/portfolio-reconcile.sh
- agents/.template/routines/promote-to-main.sh
- agents/.template/routines/push.sh
- agents/.template/routines/remind.sh
- agents/.template/routines/reminder-fire.sh
- agents/.template/routines/reminder.sh
- agents/.template/routines/routine-roller.sh
- agents/.template/routines/self-update.sh
- agents/.template/routines/session-recap.py
- agents/.template/routines/set-briefing-times.sh
- agents/.template/routines/skill-feedback-gate.py
- agents/.template/routines/status-footer.py
- agents/.template/routines/tests/fixtures/portfolio/manifest_min.md
- agents/.template/routines/tests/fixtures/portfolio/manifest_min_mutant_enum.md
- agents/.template/routines/tests/fixtures/portfolio/manifest_min_mutant_tombstone.md
- agents/.template/routines/tests/fixtures/portfolio/portfolio_c0_legacy.md
- agents/.template/routines/tests/fixtures/portfolio/portfolio_full.md
- agents/.template/routines/tests/test-classify-inbox-chunked.sh
- agents/.template/routines/tests/test-cron-guard-queue.sh
- agents/.template/routines/tests/test-cron-guard.sh
- agents/.template/routines/tests/test-design-tokens-cardparity.py
- agents/.template/routines/tests/test-design-tokens.py
- agents/.template/routines/tests/test-footer-desc.py
- agents/.template/routines/tests/test-footer-liveness.py
- agents/.template/routines/tests/test-portfolio-core.py
- agents/.template/routines/tests/test-push-retry.sh
- agents/.template/routines/tests/test-session-recap-size-log.sh
- agents/.template/routines/tests/test-status-footer.sh
- agents/.template/routines/token-gate.py
- agents/.template/routines/usage-gate.py
- agents/.template/routines/version-check.py
- agents/.template/worklog/_TEMPLATE.md

### dogany-cron-register
- skills/dogany-cron-register/SKILL.md
- skills/dogany-cron-register/template.plist

### dogany-framework
- VERSION
- git-hooks/pre-commit
- install.sh
- mirror/adapter.py
- mirror/http_direct.py
- mirror/mirror_i18n.py
- mirror/mirror_state.sql
- mirror/notify.py
- mirror/reconcile.py
- mirror/sdk_bridge.py
- packs/catalog.json
- packs/dev/.source-sync
- packs/dev/CHANGELOG.md
- packs/dev/checksums.sha
- packs/dev/pack-manifest.json
- packs/health-trainer/lib/daily_job.py
- packs/health-trainer/lib/handoff.py
- packs/health-trainer/lib/handoff_cli.py
- packs/health-trainer/lib/l1gate.py
- packs/health-trainer/lib/ledger.py
- packs/health-trainer/lib/push_gate.py
- packs/health-trainer/lib/section_submit.py
- packs/health-trainer/lib/thresholds.py
- packs/health-trainer/pack-manifest.json
- releases/v1.10.0.md
- releases/v1.11.1.md
- releases/v1.12.0.md
- releases/v1.13.0.md
- releases/v1.13.1.md
- releases/v1.13.2.md
- releases/v1.13.3.md
- releases/v1.13.4.md
- releases/v1.14.0.md
- releases/v1.15.0.md
- releases/v1.16.0.md
- releases/v1.9.0.md
- scripts/DGN-604-M3-dryrun-report.md
- scripts/dogany
- scripts/mint.sh
- scripts/pack/knowledge_selftest.sh
- scripts/pack/lib/extract_section.py
- scripts/pack/mint_run.sh
- scripts/pack/pack_install.sh
- scripts/pack/pack_publish.sh
- scripts/pack/refresh-source-sync.sh
- scripts/publish.sh
- scripts/tests/test-publish-dryrun.sh
- service/README.md
- service/lifekit/__init__.py
- service/lifekit/bundle.conf
- service/mailer/__init__.py
- service/mailer/selftest.py
- tests/agentops/test_t1_t5_agentops.py
- tests/dgn593_selftest.sh
- tests/mirror/test_s1_config_seam.py
- tests/mirror/test_s2_bootstrap_adopt.py
- tests/mirror/test_s3_delivery.py
- tests/mirror/test_s4_onboarding.py
- tests/mirror/test_s5_poll_isolation.py
- tests/mirror/test_s6_mergegate.py
- tests/mirror/test_s7_abandoned_leak.py
- tests/mirror/test_v15_promotion.py
- tests/rehearsal_dgn227.sh
- update.sh
- windows/setup-windows.ps1

### dogany-lifekit-setup
- skills/dogany-lifekit-setup/SKILL.md

### dogany-mailer
- skills/dogany-mailer/SKILL.md

### dogany-memory-search
- skills/dogany-memory-search/SKILL.md

### dogany-portfolio-setup
- skills/dogany-portfolio-setup/SKILL.md
- skills/dogany-portfolio-setup/presets.md

### dogany-proactive-push
- skills/dogany-proactive-push/SKILL.md

### dogany-relogin-rebind
- skills/dogany-relogin-rebind/SKILL.md
- skills/dogany-relogin-rebind/token-sync.sh

### dogany-reminder
- skills/dogany-reminder/SKILL.md

### dogany-routine
- agents/.template/.claude/skills-bundle/dogany-routine/SKILL.md

### dogany-skill-creator
- skills/dogany-skill-creator/SKILL.md

### dogany-upstream-report
- skills/dogany-upstream-report/SKILL.md
- skills/dogany-upstream-report/template.md

### dogany-user-onboarding
- skills/dogany-user-onboarding/SKILL.md

### relationship
- agents/.template/.claude/skills-bundle/relationship/SKILL.md

### relationship-care
- agents/.template/.claude/skills-bundle/relationship-care/SKILL.md

### task-update
- agents/.template/.claude/skills-bundle/task-update/SKILL.md
- agents/.template/.claude/skills-bundle/task-update/task.sh

### workout-log
- agents/.template/.claude/skills-bundle/workout-log/SKILL.md
- agents/.template/.claude/skills-bundle/workout-log/close_task.sh

### youtube-digest
- agents/.template/.claude/skills-bundle/youtube-digest/SKILL.md
- agents/.template/.claude/skills-bundle/youtube-digest/yt_fetch.sh

## REPO-META injected -- public-repo essentials (no unit owns; FIX 1)

Declarative, per-file injection (LICENSE/NOTICE/READMEs/CHANGELOG/
.gitignore + only the docs/img images the READMEs reference). Not a
glob over docs/ or the repo root -- an enumerated allowlist so it
cannot become a default-deny backdoor.
- .gitignore
- CHANGELOG.md
- LICENSE
- NOTICE
- README-ko.md
- README.md
- docs/img/distillation-en.png
- docs/img/distillation-ko.png
- docs/img/golden-circle-en.png
- docs/img/golden-circle-ko.png
- docs/img/pricing-en.png
- docs/img/pricing-ko.png
- docs/img/routing-en.png
- docs/img/routing-ko.png
- docs/img/tiers-en.png
- docs/img/tiers-ko.png

## F9-BLOCKED -- OWNED by a public+official unit but blocked (domain-realdata / PoC-fixture class)
- packs/dev/refdev/AGENT.md.add   (owner: dogany-framework)
- packs/dev/refdev/scripts/claude-usage.sh   (owner: dogany-framework)
- packs/dev/refdev/scripts/secret-sweep.sh   (owner: dogany-framework)
- packs/dev/refdev/scripts/ticket-hygiene.sh   (owner: dogany-framework)
- packs/health-trainer/warg/AGENT.md.add   (owner: dogany-framework)
- packs/health-trainer/warg/config/agent.conf.add   (owner: dogany-framework)
- packs/health-trainer/warg/config/triggers.yaml   (owner: dogany-framework)
- packs/health-trainer/warg/database/migrations/W01_ledger.sql   (owner: dogany-framework)
- packs/health-trainer/warg/routines/com.telegram-skill-bot.warg.daily-0405.plist   (owner: dogany-framework)
- packs/health-trainer/warg/routines/com.telegram-skill-bot.warg.handoff-watch.plist   (owner: dogany-framework)
- packs/health-trainer/warg/routines/com.telegram-skill-bot.warg.section-morning.plist   (owner: dogany-framework)
- packs/health-trainer/warg/routines/com.telegram-skill-bot.warg.section-retro.plist   (owner: dogany-framework)
- packs/health-trainer/warg/routines/com.telegram-skill-bot.warg.section-weekly.plist   (owner: dogany-framework)
- packs/health-trainer/warg/routines/com.telegram-skill-bot.warg.sweep-1230.plist   (owner: dogany-framework)
- packs/health-trainer/warg/routines/daily-0405.sh   (owner: dogany-framework)
- packs/health-trainer/warg/routines/digest-run.sh   (owner: dogany-framework)
- packs/health-trainer/warg/routines/handoff-consume.sh   (owner: dogany-framework)
- packs/health-trainer/warg/routines/handoff-submit.sh   (owner: dogany-framework)
- packs/health-trainer/warg/routines/ledger-inject.py   (owner: dogany-framework)
- packs/health-trainer/warg/routines/plists.defer   (owner: dogany-framework)
- packs/health-trainer/warg/routines/prompts/digest.md   (owner: dogany-framework)
- packs/health-trainer/warg/routines/prompts/redirect-respond.md   (owner: dogany-framework)
- packs/health-trainer/warg/routines/prompts/section-morning.md   (owner: dogany-framework)
- packs/health-trainer/warg/routines/prompts/section-retro.md   (owner: dogany-framework)
- packs/health-trainer/warg/routines/prompts/section-weekly.md   (owner: dogany-framework)
- packs/health-trainer/warg/routines/push-gated.sh   (owner: dogany-framework)
- packs/health-trainer/warg/routines/redirect-respond.sh   (owner: dogany-framework)
- packs/health-trainer/warg/routines/section-morning-gen.sh   (owner: dogany-framework)
- packs/health-trainer/warg/scripts/knowledge-snapshot.sh   (owner: dogany-framework)
- packs/health-trainer/warg/skills/diet-log/SKILL.md   (owner: dogany-framework)
- packs/health-trainer/warg/skills/workout-log/SKILL.md   (owner: dogany-framework)

## PRIVATE-BLOCKED -- claimed by a public parent glob but owned by a private/PoC unit

Nested private/PoC subtrees that a broad public parent glob would
otherwise export (spec 'PoC/private 제외'; M1 spending-log lock).
- agents/.template/.claude/skills-bundle/spending-log/SKILL.md   (private owner: spending-log)

## THIRD-PARTY-BLOCKED -- third-party IP claimed by a public parent glob (FIX 2)

agent-browser (Vercel Labs) rides agent-template's broad glob but is
M1 inventory-excluded (not our IP, uncataloged). Declarative exclusion
class; our own DGN-609 browser-auth wrapper is NOT here (it is ours).
- agents/.template/.claude/skills-bundle/agent-browser/SKILL.md   (would-be owner: dogany-agent-template)

## SYMLINK-ESCAPE -- exported symlinks whose target leaves the export set (dropped)

cp -P never follows a link (no content leak possible); these links are
dropped so no dangling/escaping symlink ships to the public repo.
- agents/.template/RULES.md -> ../../rules/RULES.md   (owner: dogany-agent-template)

## No-owner -- tracked but owned by no public+official unit (default-deny drop)

These are shared/framework-internal paths with no owning unit glob
(rules/, database/, docs/, repo-meta, PoC-private unit subtrees, etc.).
- .env.example/  (1 path(s))
- .gitignore/  (1 path(s))
- .sweepignore/  (1 path(s))
- CHANGELOG.md/  (1 path(s))
- LICENSE/  (1 path(s))
- NOTICE/  (1 path(s))
- README-ko.md/  (1 path(s))
- README.md/  (1 path(s))
- database/  (31 path(s))
- docs/  (14 path(s))
- rules/  (2 path(s))
- skills/  (1 path(s))

## Currently PUBLIC but DROPPED by this curation

Paths present on remote `public/main` today that this
allowlist curation would NOT re-export (informational; requires a
reachable public remote to compute).
- .env.example/  (1 path(s) currently public, now dropped)
- .sweepignore/  (1 path(s) currently public, now dropped)
- agents/  (3 path(s) currently public, now dropped)
- database/  (31 path(s) currently public, now dropped)
- docs/  (3 path(s) currently public, now dropped)
- packs/  (31 path(s) currently public, now dropped)
- rules/  (2 path(s) currently public, now dropped)
- skills/  (1 path(s) currently public, now dropped)
