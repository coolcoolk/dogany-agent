# bridge.md -- bridge output contract (channel-agnostic)

Applies when an output bridge is active. Direct runtime with no bridge
(e.g. Claude Code CLI): markers are inert; treat output as plain text.
Violations = bugs, not style issues.

## [[OPTIONS]] contract

- Label source priority (first source that yields labels wins; later ones unseen):
  1. Labeled marker `[[OPTIONS: a | b | c]]` -- labels ride the marker, `|`-separated;
     `|` cannot appear inside a label.
  2. Bare `[[OPTIONS]]` trailing lines -- the non-blank lines directly under the
     marker, one line = one label; collection stops at a blank line, another marker,
     or a code fence.
  3. Body numbered list -- the LAST contiguous 1..N run in the body.
- Examples (one per preferred source):

  ```
  Proceed with the merge?
  [[OPTIONS: proceed | hold]]
  ```

  ```
  Proceed with the merge?
  [[OPTIONS]]
  proceed
  hold
  ```

- Sources 1/2: label lines consumed by the buttons are auto-removed from the display
  (no duplicate list). Exception: a label overflowing to a number-handle button keeps
  the body text intact so the choice stays readable.
- "Marker must be the LAST line of its message (no trailing content)" applies to
  source 3 only (bare marker + body numbered list). Source 2 puts label lines AFTER
  the marker by design.
- Markers inside fenced code blocks never arm buttons -- fencing an example (as above)
  is safe in any doc or message.
- Fail signal: marker present but no label found from any source -> ZERO buttons,
  body kept intact, bridge logs a WARNING ("no buttons could be built").
- Button labels: plain text only, no markup, no punctuation wrappers.
- Every option = a short label plus at least a one-line description in the message body.
  Labels-only decision list (buttons with no body explanation) is a violation.
- Body reference tokens must match option labels exactly. If options are numbered 1/2/3,
  refer to them as 1/2/3 in the body -- never introduce a separate (a)/(b)/(c) or any
  other scheme for the same choices.
- Button labels: short token only -- no separators (-- / em-dash / " - "), no description
  phrases, no recommendation markers. Recommendation belongs in the body list line only;
  localized marker: ko "(추천)" / en "(rec)". An over-wide label degrades the button to
  a number token (width limit: channel layer).

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
