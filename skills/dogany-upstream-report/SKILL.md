---
name: dogany-upstream-report
description: Package a framework defect, procedure deviation, or improvement idea as a standard upstream proposal. For self-maintained repos (coolcoolk/dogany-agent, coolcoolk/claude-code-telegram): integration agent opens internal worklog ticket and fixes directly in the upstream repo (upstream-first). For genuinely third-party repos: submit as a GitHub issue. Fires on "업스트림 올려줘", "업스트림 제안해줘", "이거 이슈로 올려", "프레임워크 버그 제보", "업스트림에 보고", "report this upstream", "file a GitHub issue", "open an upstream issue", or when the agent itself hits a framework bug / deviation worth reporting. Output: worklog ticket (self-maintained) or issue URL / draft file path (third-party).
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

BACKSTOP (hardcoded, unconditional): `coolcoolk/*` repos NEVER receive a
public GitHub issue via any path -- regardless of ledger existence, ledger
parse state, or any lookup result. This rule cannot be overridden by any
ledger row.

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
3. No ledger result can override the Layer A backstop. Even if a coolcoolk/*
   row were missing or corrupted in the ledger, no public issue is opened.

## submit -- self-maintained repos (primary route)
Integration agent handles self-maintained repos internally:
1. compose report from template.md.
2. open internal worklog ticket (next available DGN-# in worklog/).
3. fix defect directly in the upstream (self-maintained) repo -- upstream-first:
   fix lands in the canonical repo (`coolcoolk/dogany-agent` or
   `coolcoolk/claude-code-telegram`), then redistributes to instances via normal
   update/propagation flow.
4. no public post, no pre-send confirm (nothing goes public).
5. report ticket number and fix summary back to __USER_LABEL__.

## submit -- third-party upstream (public route)
For repos the integration agent does NOT maintain:

### identity gate (HARD, check BEFORE composing anything public)
- run `gh auth status`. active account MUST be `coolcoolk`.
- gh missing, unauthed, or ANY other active account -> DO NOT submit. go to
  fallback. never switch accounts on your own.
- the private owner identity must NEVER appear on public repos -- not in body,
  not as committer, not in screenshots.

### PII scrub (issue body lands on a PUBLIC repo)
- machine paths -> `~` or `<workspace>` placeholders. no absolute /Users/...
- no owner email / real name / handles other than coolcoolk.
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

### fallback (gh missing / unauthed / wrong account)
- write the full proposal (same template, same scrub) to
  `files/outbox/upstream-<YYYY-MM-DD>-<slug>.md`, first line = target repo.
- tell __USER_LABEL__: draft is in outbox, needs manual relay (and why gh
  route failed).

## bounds
- public post (third-party only) -> pre-send confirm is mandatory, no autonomous submit.
- one issue per proposal; dup-check open issues first (`gh issue list`) --
  if a matching issue exists, comment there instead of opening a new one.
- self-maintained repos: NEVER open a public GitHub issue. always internal ticket + direct fix.
