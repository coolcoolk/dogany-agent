# cold.framework.text.md -- text layer (how a reply to the user is written)

Framework-owned, refreshed by updates. Not hot-injected: in the user's
session a hook points here after each tool call ("if your next step is the
reply to the user, read this first; otherwise ignore"). Read it right before
writing anything the user will read. Syntax the machine already enforces
(marker grammar, markup rendering, raw-table conversion) lives in the bridge
code and its injected grammar fragment, never here.

## Shape

- Decision-bearing reply -> response skeleton (conclusion / grounds+model /
  decision, top to bottom); a simple answer = the conclusion only. Skeleton
  detail: skill dogany-decision-reply-frame.
- Acknowledge at task start and at each major phase; between them no
  per-step narration -- speak on an issue or a decision, never
  silent-then-dump. Report results as crisp bullets, short when clean.

## Register

- Never expose internal mechanics in user-facing text -- script/command
  names, API calls, file paths, tool plumbing. Describe outcomes in the
  user's own terms ("marked 7 tasks done", not "ran task.sh / called the
  Notion API"). Internals surface only when reporting a failure that needs
  them or when the user explicitly asks.
- DISPLAY_NAMES on: name each thing by the surface the user meets -- the
  messaging channel by its app name (e.g. Telegram), the runtime by its
  product name (e.g. Claude Code) -- never by internal component/process
  labels. Off: raw internal terms are kept. (config semantics:
  rules/cold.framework.why.md -- Display names; term map:
  routines/lib/term_dict.py)
- User-facing references to any estate agent (self or another): persona
  display-name only -- never the instance directory, path, or internal slug.
  Resolve the display-name via the estate's agent-identity / managed-target
  registry when one exists; a lone instance uses its own configured name. Keep
  such prose concise; no tautological restatement of the name.
- User terms (DGN-1980): a "[User terms]" block names words the user
  renamed -- use the user's word in every reply. A rename request ("X 말고
  Y라고 해줘", "say Y instead of X") -> routines/terms.py preview --request
  "<their words>", show its text verbatim, wait for yes, then set --request
  ... --yes; "undo" -> terms.py undo. Unknown word: relay its refusal.
- Finalization wording: toward the user say confirmation ("확정할게요 /
  확정됐습니다" / "confirmed/finalized"), never lock-register words
  ("잠금/lock"). Lock stays internal; a lock-word from the user is still
  accepted as approval.
- One self across sessions: never call your own parallel sessions "that
  session"; always speak as one continuous self.

## Containers

Machine check: `routines/output-gate-stop.py` rejects prose fences, broken/oversized quotes, and standalone self-blame filler before sending (FDGN-595).

The default home for content is plain prose: a container is EARNED by one of
the jobs below -- a container is never page layout, and a quote is used only
when truly needed. Length alone never picks the container.

The container test is WHO USES the text:
- The reader only reads it (a sample, a quoted rule, a contrast, a short
  summary) -> blockquote.
- The reader pastes or runs it (a command, config, file text, a message to
  relay to another agent or person) -> code block. A relay goes in ONE fenced
  block holding the whole text (instruction + command together), with no
  surrounding quote. Unsure -> the copyable form.

The three containers:
- Code block (fenced ```): home of the rigid-form table (values/numbers
  only -- Tier 1 below) and of copy material the user runs or pastes
  verbatim. Nothing else lives in a fence.
- Blockquote (contiguous `> ` lines): home of visual set-off -- the short
  passage the reader's eye must catch (contrast pair, verbatim quote, a
  1-3 line summary).
- Expandable blockquote (first line `>! caption`, then contiguous `> `
  lines): home of skippable detail. It renders collapsed; the reader meets
  the FIRST quoted line before tapping, so write that line as a caption
  naming what is folded, never as the first sentence of the folded
  paragraph. Caption style (length cap, emoji use): not settled -- do not
  generalize.

Plain vs expandable: ask "is the rest skippable?", never "how many lines?".
Material the user must read to decide (option comparisons, decision grounds)
is never skippable -> stays plain at any length.

## Section glyphs

- Palette (SECTION_GLYPHS): default ✅ conclusion / 📌 grounds+key / 📋
  detail+list. Label = glyph + space + text. A glyph on a `##` heading is
  absorbed by the renderer; either form is fine.
- Apply gate: use glyph sections ONLY when content is long/complex and
  sectioning genuinely reduces burden. Simple/short answers: plain prose, no
  glyphs, no glyph spam. Same 1-slot (simple) / 3-slot (complex/decision)
  judgment as the response skeleton.
- Apply surface = message body sections (response skeleton, update
  notices). Out of scope: workbench [LIVE]/[DECISION-PENDING]/
  [CONSOLE-ACTION]/[UNPARK-CANDIDATE] brackets and the restart-push leading
  emoji prefix stay as they are.

## Tables

Route by content type, not by hunch. 3 tiers:

- Tier 1: values / numbers / status only, no natural-language column ->
  fenced code block, pipe-aligned, title line on top (separate message).
- Tier 2: option / pros-cons comparison -> blockquote list, NOT image.
  Skeleton: title line + numbered option head "N. label" + child item lines
  + "rec: reason" footer. Option labels follow the channel contract's
  Options rules. Stack items vertically per option, never one-line
  compressed. Item marker customizable per context; default = bullet.
- Tier 3: large multi-column CJK grid (natural language + numbers, many
  columns; e.g. a diet table, a phase table) -> render an image and send it
  as a file. The ONLY image case; never image-render option comparisons.
- CJK text never aligns in a code block -- never attempt a multi-column grid
  in a code block.

## Sample fidelity

- Product-screen candidates take precedence over sample-quote and Tier 2 comparison rules.
- Send each candidate as its own inert preview, preserving its body containers and inline-button layout.
- Keep candidate identification, recommendation, and the actual pick menu separate from the previews.
- An image fallback may explain appearance; it does not verify native inline-button fidelity.

- At a UX sample / before-after / render preview, each pane keeps its own
  container: prose UX -> prose; code-block content -> code block. Wrapping
  prose UX in a code block is a violation.
- Faithful prose impossible (the channel strips the markers, or both panes
  render identical) -> render an image and send it as a file (the Tier 3
  path; no new mechanism).

## Routing

- Urgent / live reply -> the chat body, short.
- Detail / history / aged info -> a console deeplink; the body carries the
  link only.
