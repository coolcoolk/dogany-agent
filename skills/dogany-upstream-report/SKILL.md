---
name: dogany-upstream-report
description: Override first -- fix a wrong shipped framework or kit skill now in a registered local shadow, then file its reproducible upstream report in parallel. Other local needs or preferences use a local override and are not reported. User installs (not the maintainer fleet) file framework defects as a public GitHub issue on coolcoolk/dogany-agent after scrub and user confirm. Maintainer instances open an internal worklog ticket. Fires on upstream-report requests or when the agent hits a shipped skill defect. Output: working local override plus worklog ticket, issue URL, or draft path.
---

# dogany-upstream-report -- standard upstream proposal

Agent hits framework defect / procedure deviation / improvement idea -> package
in standard form -> route by repo ownership. One standard pipe, no ad-hoc
reporting.

## when
- agent observes framework bug, wrong/missing procedure, doc drift, improvement
  idea that belongs upstream (not instance-local).
- user says: report upstream / open an issue for this.
- instance-local problems (own config, own data) do NOT go upstream -- fix local.
- ALWAYS run step 0 (override first) before anything below.

## step 0 -- override first (runs before any report)
First make the instance work. If a shipped `dogany-*` framework skill or a
bundled kit skill is wrong, copy that skill into the instance-owned skill
location, change its frontmatter to `layer: local`, register the copied skill
with `python3 routines/skill-layer.py register <copied-skill-dir>`, and fix it
there now. Never edit the shipped source in place. Verify the local copy, then
file the reproducible upstream report in parallel; reporting never delays or
replaces the local fix.

For anything else, ask: is this wrong for EVERYONE, and can it be reproduced
on a clean install? A yes to both moves on to the standard form. A local need
or preference is solved with an override layer and is not reported.

override layers (pick the smallest that works):
1. operating rule / habit / tone / gate tweak -> `rules/hot.custom.md`
   (operating-gate overlay, never shipped, survives updates). Agent drafts
   the exact lines, user confirms, then write.
2. new capability or a different procedure -> an instance custom skill
   (`skills/<name>/`, frontmatter `layer: local`), then
   `python3 routines/skill-layer.py register skills/<name>`.
3. a shipped skill or framework file must behave differently here -> local shadow of that
   file, registered in `.claude/.dogany-preserve` with
   `python3 routines/skill-layer.py register <path>` (survives updates).
   Prefer 1 or 2 when they can do the job.

unsure whether a non-skill issue is a defect -> override, and tell the user one line why
(e.g. "This fits how you work, so I set it up here for you."). Never
report "just in case".

user-facing wording (all steps, in the user's language): plain words only.
Say "change how I work here", "set it up on your side", "tell the Dogany
developers", "post a public bug report". Never show internal terms
(override, layer, upstream, preserve, hot.custom, Layer A/B, PORTFOLIO,
DGN, lane, outbox) to the user; file paths only when the user asks.

## standard form (template.md in this folder)
Fill ALL sections; thin evidence -> gather first, then report.
1. title -- one line, surface prefix. e.g. "update.sh: skills manifest not
   refreshed on dry-run".
2. surface -- which component (skill name / script / engine / bridge module).
3. what happened / deviation -- observed vs expected.
4. evidence -- dates, scrubbed log lines, repro steps.
5. proposed change -- smallest fix that works.
6. severity guess -- fatal / major / minor / idea.

## repo routing

### surface -> repo mapping heuristic
- framework surfaces (skills/, routines, agents/.template, memory-engine,
  mint/update/install scripts, docs) -> `coolcoolk/dogany-agent`
- bridge surfaces (telegram gateway, bridge/ code) -> `coolcoolk/claude-code-telegram`
- unsure -> `coolcoolk/dogany-agent` (framework owns triage).

### Layer A -- universal rule (no ledger required; applies on every instance)
Self-maintained = repos the integration agent directly commits to.

BACKSTOP (hardcoded): `coolcoolk/*` repos NEVER receive a public GitHub
issue via any path -- regardless of ledger existence, ledger parse state, or
any lookup result. This rule cannot be overridden by any ledger row.
ONE exception, nothing else: the user-install lane below (framework defect
that passed step 0, target `coolcoolk/dogany-agent`, user confirms).

### user-install lane (the only public path to a coolcoolk/* repo)
User install = an instance that is NOT part of the maintainer fleet:
no `product/PORTFOLIO.md` ledger, or a ledger that parses clean and has no
row for the target repo. Any ledger row for the target (maintainer,
version-consumer, steward-delegate, semi-managed, frozen) or a ledger parse
failure -> NOT this lane; use Layer B below unchanged.
On a user install the integration agent cannot fix the framework, so the
defect goes to the public repo:
- target: always `coolcoolk/dogany-agent` (framework owns triage; bridge
  defects too).
- steps: dup-check -> compose from template.md -> PII scrub (below) -> show
  the user the final title + body + target repo + which GitHub account
  posts it -> user confirms -> `gh issue create` (submit steps below).
- identity: post under the gh account the user is already logged in with;
  the `coolcoolk` identity gate does NOT apply here. Never switch or log in
  to an account on your own.
- gh missing / unauthed / user declines -> outbox draft (fallback below).
- confirm prompt, plain words, e.g.: "This looks like a bug in Dogany
  itself, not your setup. Here is a public bug report for the Dogany
  developers -- anyone can read it, and it is posted from your GitHub
  account <login>. Post it?"

For self-maintained repos: always internal worklog ticket + direct fix
(upstream-first). No public post. No pre-send confirm.

For third-party repos (repos the integration agent does NOT maintain):
public GitHub issue path (see third-party section below) -- subject to all
guards (identity gate, PII scrub, pre-send confirm, outbox fallback).

### Layer B -- conditional ledger overlay (instances with product/PORTFOLIO.md only)
Instances without the ledger or without a parse entrypoint (instance-local
routines/lib/portfolio-parse.sh, or the framework-shipped
routines/lib/portfolio-core-parse.sh) skip Layer B entirely (file absence =
natural skip). Layer A alone applies; semantics identical to pre-ledger
behavior.

When the ledger is present:

step 1 -- parse check (run BEFORE reading the ledger body). Prefer the
instance-local hardened parser when it exists (stricter profile); else use
the framework core parse:
```bash
if [ -f routines/lib/portfolio-parse.sh ]; then
  bash routines/lib/portfolio-parse.sh
else
  bash routines/lib/portfolio-core-parse.sh
fi
```
Nonzero exit OR output containing `PORTFOLIO-PARSE-FAIL` -> immediately take
the fail-closed path (rule 1 below). Do NOT read the ledger body. This blocks
the LLM from silently tolerating a broken table.

step 2 -- row lookup (only after parse passes):
Look up the repo identifier in the ledger (product/PORTFOLIO.md). Lookup key
matches when it is a suffix of the row's `repo` cell value. Route by the
matched row's `role` and `defect_intake` column per the following lanes:
- role=maintainer -> internal DGN ticket + direct fix. NEVER a public issue.
- role=version-consumer -> local fix if instance-local; promote to maintainer
  lane (upstream-first) if framework defect.
- role=steward-delegate -> smith handoff channel -> Metal triage -> maintainer
  lane.
- role=semi-managed -> owner manual report -> DGN ticket.
- role=frozen -> outbox draft + WARN even on lookup HIT. No public issue.

### fail-closed rules (all three are absolute)
1. Parse failure (nonzero exit or PORTFOLIO-PARSE-FAIL token) -> write outbox
   draft + emit WARN report. Public-issue branch forbidden.
2. Lookup miss (repo not registered in ledger) -> write outbox draft + WARN
   ("needs row registration or third-party confirmation"). Public-issue branch
   forbidden -- public path only after HUMAN third-party confirmation.
   Exception: target `coolcoolk/dogany-agent` on a clean-parsing ledger with
   no row for it = user install -> user-install lane.
3. No ledger result can override the Layer A backstop. Even if a coolcoolk/*
   row were missing or corrupted in the ledger, no public issue is opened
   (a corrupted ledger = parse failure = rule 1, never the user-install lane).

## submit -- self-maintained repos (maintainer instances)
Integration agent handles self-maintained repos internally:
1. compose report from template.md.
2. open internal worklog ticket (next available DGN-# in worklog/).
3. fix defect directly in the upstream (self-maintained) repo -- upstream-first:
   fix lands in the canonical repo (`coolcoolk/dogany-agent` or
   `coolcoolk/claude-code-telegram`), then redistributes to instances via normal
   update/propagation flow.
4. no public post, no pre-send confirm (nothing goes public).
5. report ticket number and fix summary back to __USER_LABEL__.

## submit -- public route (third-party repos, and the user-install lane)
For repos the integration agent does NOT maintain, and for a framework
defect from a user install (target `coolcoolk/dogany-agent`):

### identity gate (HARD, check BEFORE composing anything public)
(third-party route only; the user-install lane uses the user's own logged-in
account, see above)
- run `gh auth status`. active account MUST be `coolcoolk`.
- gh missing, unauthed, or ANY other active account -> DO NOT submit. go to
  fallback. never switch accounts on your own.
- the private owner identity must NEVER appear on public repos -- not in body,
  not as committer, not in screenshots.

### PII scrub (issue body lands on a PUBLIC repo)
- machine paths -> `~` or `<workspace>` placeholders. no absolute /Users/...
- no owner email / real name / handles other than coolcoolk (user-install
  lane: no user email / real name / other people's handles in the body).
- no tokens, bot ids, chat ids, .env contents. log excerpts: trim to the
  relevant lines, scrub first.
- re-read the final body once with only this question: "safe on a public repo?"

### submit steps
1. compose body from template.md, scrubbed.
2. pre-send confirm: show __USER_LABEL__ the final title + body + target repo,
   get OK (public post rule -- always confirm first).
3. label: `bug` for defect/deviation, `enhancement` for improvement. if the
   label is rejected (repo has no such label), retry WITHOUT label -- never
   fail on labels.
   ```bash
   gh issue create -R <owner/repo> --title "<title>" --body-file <tmpfile> --label <bug|enhancement>
   ```
4. report the issue URL back.

### fallback (gh missing / unauthed / wrong account / user declines)
- write the full proposal (same template, same scrub) to
  `files/outbox/upstream-<YYYY-MM-DD>-<slug>.md`, first line = target repo.
- tell __USER_LABEL__ in plain words: the report is saved as a file they can
  send later or paste at github.com/coolcoolk/dogany-agent/issues (and why
  it was not posted).

## bounds
- step 0 first: a shipped skill defect gets a working registered local shadow
  and an upstream report in parallel. A mere local preference is not reported.
- public post (third-party, or user-install lane) -> pre-send confirm is
  mandatory, no autonomous submit.
- one issue per proposal; dup-check open issues first (`gh issue list`) --
  if a matching issue exists, comment there instead of opening a new one.
- self-maintained repos on maintainer instances (any ledger row): NEVER open
  a public GitHub issue. always internal ticket + direct fix.
