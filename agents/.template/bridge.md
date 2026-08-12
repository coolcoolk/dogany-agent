# bridge.md -- bridge output contract (channel-agnostic)

Applies when an output bridge is active. Direct runtime with no bridge
(e.g. Claude Code CLI): markers are inert; treat output as plain text.
Violations = bugs, not style issues.

## [[OPTIONS]] contract

- [[OPTIONS]] marker must be the LAST line of its message (no trailing content).
- Button labels: plain text only, no markup, no punctuation wrappers.
- Every option = a short label plus at least a one-line description in the message body.
  Labels-only decision list (buttons with no body explanation) is a violation.
- Body reference tokens must match option labels exactly. If options are numbered 1/2/3,
  refer to them as 1/2/3 in the body -- never introduce a separate (a)/(b)/(c) or any
  other scheme for the same choices.
- Mark a recommendation directly on the option label (e.g. label + "(rec)"), not only
  in prose.

## Message structure

- Code block or table and [[OPTIONS]] must NOT appear in the same message --
  bridge parser conflict breaks both. Send code/table first, [[OPTIONS]] next message.

## send_file contract

- Syntax: standalone line `send_file:: <absolute path>` (one per file).
- Bare file path in prose is NOT sent -- bridge ignores it.
- Finalize the file BEFORE the send_file line; bridge attaches disk state at send time.
- File must exist and be under 10MB (bridge-enforced). Path outside PROJECT_ROOT requires confirm.

## Tables

Route by content type, not by hunch. Wide, dense grids -> render image + send_file.
Specific tier forms (code block skeleton, blockquote skeleton, CJK glyph limits) are
defined in the channel layer below.

## Routing

- Urgent / live reply -> channel body, short.
- Detail / history / aged info -> console deeplink; body carries the link only.
- (Full routing rule: AGENT.md Output ownership workflow.)

## Channel dispatch

Active channel defines the channel-specific contract layer.
Channel = Telegram -> telegram.md applies.

@telegram.md
