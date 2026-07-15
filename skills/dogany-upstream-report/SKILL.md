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
- framework surfaces (skills/, routines, agents/.template, memory-engine,
  mint/update/install scripts, docs) -> `coolcoolk/dogany-agent` (SELF-MAINTAINED)
- bridge surfaces (telegram gateway, bridge/ code) -> `coolcoolk/claude-code-telegram` (SELF-MAINTAINED)
- unsure -> `coolcoolk/dogany-agent` (framework owns triage).
- genuinely third-party repos (projects the integration agent does NOT maintain):
  public GitHub issue path (see third-party section below).

Self-maintained = coolcoolk/* repos that the integration agent directly commits
to. Do NOT open public GitHub issues on self-maintained repos -- the integration
agent is the maintainer; a public issue is pointless ceremony against your own
repo.

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
