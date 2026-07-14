---
name: dogany-upstream-report
description: Package a framework defect, procedure deviation, or improvement idea as a standard upstream proposal and submit it as a GitHub issue on the public upstream repo (framework surfaces -> coolcoolk/dogany-agent, bridge surfaces -> coolcoolk/claude-code-telegram). Fires on "업스트림 올려줘", "업스트림 제안해줘", "이거 이슈로 올려", "프레임워크 버그 제보", "업스트림에 보고", "report this upstream", "file a GitHub issue", "open an upstream issue", or when the agent itself hits a framework bug / deviation worth reporting. Requires gh authed as the public identity; otherwise writes a draft to files/outbox/ and asks __USER_LABEL__ to relay manually. Output: issue URL (or draft file path).
---

# dogany-upstream-report -- standard upstream proposal (GitHub issue)

Agent hits framework defect / procedure deviation / improvement idea -> package
in standard form -> submit as GitHub issue on the public upstream repo. One
standard pipe, no ad-hoc reporting.

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
  mint/update/install scripts, docs) -> `coolcoolk/dogany-agent`
- bridge surfaces (telegram gateway, bridge/ code) -> `coolcoolk/claude-code-telegram`
- unsure -> `coolcoolk/dogany-agent` (framework owns triage).

## identity gate (HARD, check BEFORE composing anything public)
- run `gh auth status`. active account MUST be `coolcoolk`.
- gh missing, unauthed, or ANY other active account -> DO NOT submit. go to
  fallback. never switch accounts on your own.
- the private owner identity must NEVER appear on public repos -- not in body,
  not as committer, not in screenshots.

## PII scrub (issue body lands on a PUBLIC repo)
- machine paths -> `~` or `<workspace>` placeholders. no absolute /Users/...
- no owner email / real name / handles other than coolcoolk.
- no tokens, bot ids, chat ids, .env contents. log excerpts: trim to the
  relevant lines, scrub first.
- re-read the final body once with only this question: "safe on a public repo?"

## submit (primary route)
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

## fallback (gh missing / unauthed / wrong account)
- write the full proposal (same template, same scrub) to
  `files/outbox/upstream-<YYYY-MM-DD>-<slug>.md`, first line = target repo.
- tell __USER_LABEL__: draft is in outbox, needs manual relay (and why gh
  route failed).

## bounds
- public post -> pre-send confirm is mandatory, no autonomous submit.
- one issue per proposal; dup-check open issues first (`gh issue list`) --
  if a matching issue exists, comment there instead of opening a new one.
