# telegram.md -- Telegram vendor contract (judgment + expression rules)

FRAMEWORK-OWNED: refreshed by every update. Hand edits here are backed up to
`vendors/telegram.md.user-<ts>` and then REVERTED -- the file carries no
ownership token because everything under `vendors/` is framework-owned, so the
warning has to be written out.

To diverge WITHOUT losing it on the next update, write
`vendors/custom.telegram.md`: that name is instance-owned (update.sh
`assert_instance_ownership_convention`), the bridge reads it
(`sdk_bridge._load_vendor_overlay`), and it is injected AFTER this file, so
where the two speak to the same point yours is the sentence the model reads
last. Absent overlay = nothing injected; blank overlay = a WARN in the bridge
log and nothing injected -- it can never brick the bot. Rules that should hold
for EVERY instance still belong upstream in this file, not in an overlay.
(Before DGN-818 C2 this paragraph said "there is no per-instance overlay for
this file today"; the name had been reserved since DGN-773 T5 with no reader,
so the sentence was half true in both directions.)

Injection-only: the bridge (spawner) appends this file to the session system
prompt. vendors/ is OUTSIDE the @ hot chain -- linking this file from any doc
hub is a regression. No bridge session -> this file is simply never injected.
If any machine-behavior statement here diverges from the bridge code, THE
CODE WINS -- report the sentence as a doc bug instead of trusting it.
Channel grammar (marker shapes, width/size limits, delivery mechanics) is
taught by the bridge's injected grammar fragment and enforced by its parser;
this file never restates it.

## Options: judgment rules

- [[OPTIONS]] is for real decision asks only -- never on procedure/step
  lists, never to option-ize safe/reversible work as "shall I do X?".
- Every option = a thin label plus at least a one-line description in the
  message body. A labels-only decision list (buttons with no body
  explanation) is a violation.
- Body reference tokens must match option labels exactly. If options are
  numbered 1/2/3, refer to them as 1/2/3 in the body -- never introduce a
  separate (a)/(b)/(c) or any other scheme for the same choices.
- Label style: neutral action phrases (verb-noun form, e.g. "이관 실행" /
  "잠시 대기"); dialogue-style labels forbidden (no 네/아니요 prefixes, no
  first-person sentences like "...할게요" or "...할까요"). [owner-locked
  copy, carried verbatim from CONTRACT.md ## Bridge output]
- Recommendation belongs in the body list line only, never in a button
  label; marker copy ko "(추천)" / en "(rec)" (locked in bridge i18n key
  option_rec_marker).

## Markup

- Bold allowed: `**bold**` or `<b>bold</b>`. Multi-word OK.
- Italic: single word only (`*word*` / `_word_`). A multi-word italic span
  renders raw -- use bold for multi-word emphasis.
- Inline code (backtick) and fenced code blocks: allowed.
- Section glyph palette (SECTION_GLYPHS): default ✅ conclusion / 📌
  grounds+key / 📋 detail+list. Label = glyph + space + text.
- Apply gate (critical): glyph sections are a cognitive-load tool, not
  decoration -- use ONLY when content is long/complex and sectioning
  genuinely reduces burden. Simple/short answers: plain prose, no glyphs.
  No glyph spam. Same 1-slot (simple) / 3-slot (complex/decision) judgment
  as L0.
- Apply surface = message body sections (L0 response skeleton, update
  notices). Out of scope: workbench [LIVE]/[DECISION-PENDING]/
  [CONSOLE-ACTION]/[UNPARK-CANDIDATE] brackets stay as-is; restart-push
  leading emoji prefix stays as-is.

## Tables

Route by content type, not by hunch. 3 tiers:

- Tier 1: values / numbers / status only, no natural-language column ->
  fenced code block, pipe-aligned, title line on top (separate message).
- Tier 2: option / pros-cons comparison -> blockquote list, NOT image.
  Skeleton: title line + numbered option head "N. label" + child item lines
  + "rec: reason" footer. Option labels follow the Options judgment rules
  above; over-wide labels degrade on the button (width mechanics: bridge
  grammar). Stack items vertically per option (never one-line compressed --
  cognition collapses). Item marker customizable per context; default =
  bullet (e.g. +/- pair, signal-light emoji as fit).
- Tier 3: large multi-column CJK grid (natural-language + numbers, many
  columns; e.g. diet table, phase table) -> render image + send_file. ONLY
  image case; do NOT image-render option comparisons.
- Raw markdown table (|col|col|) forbidden -- CJK/emoji widths break ASCII
  grids.
- CJK text never aligns in a code block (glyph width != 2 cells) -- never
  attempt a multi-column grid in a code block.

## Sample fidelity

- Forcing point: at the moment of emitting a UX sample / before-after /
  render preview -- present each pane in the form the user will actually see
  it. Prose UX -> prose. Code-block content -> code block. Wrapping prose UX
  in a code block is a violation: literal rendering kills the difference
  being shown.
- Escalation: the markup itself is the compared object and faithful prose is
  impossible (channel strips the markers, or both panes render identical) ->
  render image + send_file (existing Tables image path; no new mechanism).

## Routing

- Urgent / live reply -> channel body, short.
- Detail / history / aged info -> console deeplink; body carries the link
  only.
- (Full routing rule: the persona file's Output ownership workflow --
  identity/hot.custom.agent.md.)
