---
name: dogany-mac-load-check
display_name: 맥 부하 점검
scope: framework
description: Mac load check for ANY agent (project, domain, or main) on a Mac running several parallel Claude Code sessions, macOS only. Covers pre-flight check BEFORE a heavy job (xcodebuild test, next build, vitest run, gradle, flutter build, simulator boot), read-only triage when the machine is slow, AND status query. Korean triggers - "맥 느려", "빌드가 너무 느려", "테스트가 안 끝나", "디스크 꽉 찼어", "시뮬레이터 여러 개 떠 있어", "메모리 스왑", "부하 확인해줘", "지금 무거운 거 뭐야". English triggers - "mac is slow", "load is high", "disk full", "too many simulators", "hung xcodebuild", "check machine load". Also fires when a machine-health alert is mentioned. Cleans only this agent's own footprint (own processes, own build output inside its own project dir, after owner yes, to Trash); machine-wide cleanup is escalated to the crew representative. Non-macOS host -> says not supported and stops. Output = short status: what is heavy, whose it is, what was done, what the owner must decide. Tier BEST-EFFORT.
---

# dogany-mac-load-check -- per-agent Mac load check (limited)

Reader: agent. Tier: BEST-EFFORT (description auto-fire, no hook).
Placeholders: `<ROOT>` = this instance root, `<PID>`, `<PROJECT_DIR>`,
`<PATH_X>` = fill at run time.
Script: `<ROOT>/.claude/skills/dogany-mac-load-check/loadcheck.sh` (read-only).

## 0. Platform guard (run FIRST)
- `uname -s` != Darwin (script exit 3) -> tell owner: Mac only, not supported
  on this machine. STOP.

## 1. Role
- You = one agent among several. NOT machine owner.
- Machine-wide cleanup + machine routines belong to the crew representative
  (Mac optimize skill). You: diagnose, protect other sessions, clean own
  footprint only, escalate.

## 2. Pre-flight (before any heavy job)
- `loadcheck.sh preflight` -> load5 vs ncpu, swap used, disk used %, running
  counts per heavy family.
- Same family already running elsewhere -> wait or tell owner. Never stack a
  duplicate.
- Limits when you start one:
  - xcodebuild test: `-parallel-testing-enabled NO`
  - vitest: `--maxWorkers=2`
  - next build: one at a time across all worktrees
  - simulator: boot at most one
- load5 above ncpu or swap climbing -> defer heavy job, tell owner.

## 3. Triage when slow (READ-ONLY)
- `loadcheck.sh preflight` for numbers.
- Top RSS + top CPU: `ps -Ao pid,ppid,rss,%cpu,etime,comm` (sort both ways).
- Process count grouped by command -> runaway families.
- Booted simulators: `xcrun simctl list devices booted`.
- Hung job = same command running unusually long (tests: 15+ min).
- Machine thrashing -> `du` / `xcrun` crawl. Run in background, read output
  file. Never chain sleeps.

## 4. Ownership rule
- `loadcheck.sh owner <PID>` -> `mine=yes | no | unsure`.
- yes = descends from YOUR own session. no = another `claude` process or no
  session. unsure = not yours.
- Not yours -> never kill, never delete its build dirs / worktrees /
  simulators / DerivedData.

## 5. Own footprint only
- Stop dev servers + test runs YOU started, once done.
- Hung job: kill only if `mine=yes`. SIGTERM first, verify gone.
- Simulator: shut down only if you booted it AND no other `xcodebuild` runs.
- Build output: only inside `<PROJECT_DIR>`, only when not in use. List items
  + sizes, owner yes, then `trash <PATH_X>` (no `trash` -> `mv <PATH_X>
  ~/.Trash/`). Never `rm -rf`.
- Global caches = hands off (package caches, DerivedData, XCTestDevices,
  DeviceSupport, Application Support, Library). Representative owns them.

## 6. Escalate
- Machine-wide problem = swap high with no job of yours, disk > 85%, others'
  hung runs, cache bloat, incompatible apps.
- Tell owner: numbers + owner of each heavy item. Recommend asking the crew
  representative agent for a Mac optimize run.
- If you ARE the representative (Mac optimize gate passes) -> offer that
  skill directly.
- Do NOT run machine-wide cleanup. Do NOT register or edit launchd routines.

## 7. Hard nevers
- delete outside `<PROJECT_DIR>`
- kill other sessions' processes
- recursive delete on shared caches; `rm -rf` anywhere
- sudo -> hand owner exact command lines for their own terminal instead
- delete anything unsure
- denied delete / move -> STOP, report. Never retry another way.
- restart / stop bridge or main bot

## 8. Output (owner-facing)
- Short. Owner's language.
- Order: conclusion (what is heavy + whose) -> what you did -> what owner
  must decide.
- Outcomes only. No script, file-path, or command names in owner text.

## test
- canonical repo selftest `dgn1712_mac_skills_selftest` (not shipped to
  instances): frontmatter, section 0 platform check, non-Darwin exit 3,
  preflight keys, zero writes.
- live: `loadcheck.sh preflight` prints every key without error.
