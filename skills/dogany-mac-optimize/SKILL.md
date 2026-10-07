---
name: dogany-mac-optimize
display_name: 맥 최적화
scope: framework
description: Machine-wide Mac slowness fix plus process and storage cleanup, run by the crew representative agent only, on macOS only. Covers run (diagnose + cleanup after owner yes) AND query (machine status, disk / memory / swap / load, what is eating resources, what can be freed). Korean triggers - "맥 느려", "맥 최적화해줘", "저장공간 정리해줘", "디스크 꽉 찼어", "용량 확보해줘", "프로세스 정리", "메모리 스왑 많아", "시뮬레이터 정리", "캐시 정리해줘", "맥 상태 어때", "왜 이렇게 버벅여", "주간 정리 걸어줘". English triggers - "optimize my Mac", "Mac is slow", "free up disk space", "clean caches", "disk full", "swap is high", "what is eating my Mac". Also fires when another agent's load report recommends a machine-wide cleanup. Non-representative agent or non-macOS host -> says so and stops (project agents use the Mac load check skill instead). Output = diagnosis (conclusion / grounds / decision), list of cleanable items with sizes for owner approval, then result with space moved to Trash and what is left for the owner. Deletes go to Trash only. Tier BEST-EFFORT.
---

# dogany-mac-optimize -- machine-wide Mac process + storage cleanup

Reader: agent. Tier: BEST-EFFORT (description auto-fire, no hook). Safety =
gates below, not the trigger.
Placeholders: `<ROOT>` = this instance root, `<PID>`, `<PATH_X>`, `<N>` =
fill at run time.

## 0. Gates (run FIRST, stop on any fail)
- `bash <ROOT>/.claude/skills/dogany-mac-optimize/gate.sh check <ROOT>`
  - exit 3 (`uname -s` != Darwin) -> tell owner: Mac only, not supported on
    this machine. STOP.
  - exit 4 (not representative) -> tell owner: machine-wide cleanup belongs
    to the crew's representative agent; offer the Mac load check instead.
    STOP.
  - exit 5 (root not an instance) -> cannot confirm role. STOP, report.
  - exit 0 -> continue.
- Representative = `DOGANY_AGENT_CLASS` in `<ROOT>/.instance.conf`:
  `domain` = no; `main` or absent = yes (same rule as install.sh). crew.conf
  has no leader field. One host can carry several main instances -> machine
  routine (section 6) stays single per host.

## 1. Ownership gate (before ANY kill / delete)
- Live Claude Code session = `claude` process. Its descendants, cwd, build
  dirs, worktrees, dev servers, simulators, DerivedData in use = OFF LIMITS.
- Check one process: `gate.sh owner <PID>` -> `offlimits=yes` = hands off.
- Session cwd: `lsof -a -p <PID> -d cwd`.
- Also off limits: bridge / bot processes and any loaded launchd job of this
  estate. Never restart or stop them.
- Heavy or duplicate item under a session -> REPORT only. Suggest owner check
  that session for hung / duplicate runs.
- Unsure of ownership -> off limits.

## 2. Diagnose (READ-ONLY, in order)
1. disk: `df -h /System/Volumes/Data`
2. memory: `vm_stat`, `sysctl vm.swapusage`
3. load: `uptime`, `sysctl -n hw.ncpu`
4. top processes by RSS and by CPU: `ps -Ao pid,ppid,rss,%cpu,etime,comm`.
5. process count grouped by command -> runaway families (e.g. simulator
   helpers).
6. node / build process tree + cwd per PID (`lsof`).
7. Claude session PIDs + cwd each.
8. `launchctl list` -> failing status.
9. running `xcodebuild`; booted simulators: `xcrun simctl list devices
   booted`, also `--set testing`.
10. sizes: `du -sh -x` on HOME top level, `~/Library/Developer/*`,
    `~/Library/Application Support`, `~/Library/Caches`, `~/.cache`, `~/.npm`.
- Machine thrashing -> `du` / `xcrun` crawl. Run them in background, read
  output file. Never chain sleeps.
- Tag each finding: session-owned (gate) / regenerable / personal.

## 3. Cleanup tiers
Delete = move to Trash: `trash <PATH_X>`. No `trash` command -> `mv
<PATH_X> ~/.Trash/`. Never `rm -rf`. Move refused (permission / privacy
block) -> STOP, report, ask owner. Never retry another way.

TIER A -- regenerable. Only after owner yes (section 5).
- package caches (npm, uv, pip, Homebrew download cache) ONLY when no install
  running. Trash the cache dir contents.
- DerivedData subdirs not modified 7+ days. Keep shared module / symbol
  caches.
- old iOS DeviceSupport versions. Keep 2 most recent.
- XCTestDevices clone simulators: ONLY when NO `xcodebuild` running.
  `xcrun simctl --set testing shutdown all`, then trash the clones.
- `xcrun simctl delete unavailable`: tool-native, not recoverable from Trash
  -> mark so in the approval list.

TIER B -- NEVER automatic.
- Application Support, game libs, model dirs, user media, git clones.
- Owner calls a dir "cache" -> open it, list contents first.
  - git repo inside -> check dirty files + branch pushed (`git ls-remote`).
  - unpushed / dirty work -> do NOT delete. Report.

TIER C -- personal data. Only on explicit owner request, item by item.
- back up tiny irreplaceable parts (small config / voice dirs) to
  `<ROOT>/files/_archive/` first.

## 4. Incompatible apps + root-owned items
- `system_profiler SPApplicationsDataType -json` -> `arch_kind ==
  arch_i64` (Intel-only), skip `/System`.
- failing launchd helper daemons: `launchctl list` status 78.
- vendored copies inside project dirs = leave.
- games under Rosetta = ask, never delete.
- root needed (`/Library`, root-owned `/Applications`) -> NO sudo. Hand owner
  exact command lines to run in their own terminal. Prefer move-to-Trash forms.

## 5. Approval + execution
- Destructive step = list every item with size + tier, get owner yes, then
  run. Yes covers the listed items only.
- Record `df` before and after.
- Trash holds the space until emptied -> report "moved to Trash: <N> GB".
  Emptying Trash = owner's call; say so.
- Auto-mode / permission classifier denies a move -> STOP, report, ask owner
  for explicit go or a permission rule. No workaround (no smaller pieces, no
  other tool).

## 6. Prevention (no new watchdog)
- Job health (silent death, lost jobs) = existing per-instance health
  observer. Do not build or register another watchdog.
- Swap / load / disk alerts: health observer does not watch these yet ->
  diagnose on owner ask only; mention the gap if owner wants alerts.
- Weekly check routine: ONLY when owner asks, ONLY on this representative,
  ONLY via dogany-cron-register.
  - first check one host-wide copy: `launchctl list | grep -i mac-weekly`.
    Found (any instance) -> do not add a second.
  - job body = section 2 read-only + push reclaimable Tier A list to owner.
    Never deletes unattended; cleanup waits for owner yes in session.
- Parallel-run limits (one `xcodebuild` per scheme, one simulator, vitest
  workers 2, one next build) live in the Mac load check skill.

## 7. Output (owner-facing)
- Owner's language. Address per persona.
- Skeleton: conclusion / grounds / decision. Short bullets.
- Outcomes only. No script, file-path, or command names in owner text --
  except command lines the owner must run themselves (section 4).
- After cleanup: space moved to Trash, space freed now, what remains for the
  owner (root-only items, Trash to empty, session-owned heavy items).

## 8. Bounds
- Never restart / stop bridge or main bot.
- Framework code untouched.
- No external actions.

## test
- canonical repo selftest `dgn1712_mac_skills_selftest` (not shipped to
  instances): frontmatter, section 0 platform check, exit codes per class
  (not-mac / domain / main / absent / unknown root), zero writes.
- live: `gate.sh check <ROOT>` then section 2 commands read-only; all print
  without error.
