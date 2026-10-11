# telegram.md -- Telegram vendor contract (judgment + expression rules)

FRAMEWORK-OWNED: refreshed by every update. Hand edits here are backed up to
`vendors/telegram.md.user-<ts>` and then REVERTED -- the file carries no
ownership token because everything under `vendors/` is framework-owned, so the
warning has to be written out.

Everything ABOVE the `dogany:inject-below` marker is for the human editing
this file; the bridge injects only what is BELOW it
(`sdk_bridge._injectable_vendor_text`). A file with no marker is injected in
full (back-compat). Marker present with nothing below it: canonical = boot
die (fail-closed), overlay = WARN and skip (fail-open). Keep the marker on
its own line, outside every code fence.

To diverge WITHOUT losing it on the next update, write
`vendors/custom.telegram.md`: that name is instance-owned (update.sh
`assert_instance_ownership_convention`), the bridge reads it
(`sdk_bridge._load_vendor_overlay`), and it is injected AFTER this file, so
where the two speak to the same point yours is the sentence the model reads
last. Absent overlay = nothing injected; blank overlay = a WARN in the bridge
log and nothing injected -- it can never brick the bot. The overlay obeys the
same marker convention. Rules that should hold for EVERY instance still
belong upstream in this file, not in an overlay.
(Before DGN-818 C2 this paragraph said "there is no per-instance overlay for
this file today"; the name had been reserved since DGN-773 T5 with no reader,
so the sentence was half true in both directions.)

Injection-only: the bridge (spawner) appends this file to the session system
prompt. vendors/ is OUTSIDE the @ hot chain -- linking this file from any doc
hub is a regression. No bridge session -> this file is simply never injected.
Channel grammar (marker shapes, width/size limits, delivery mechanics) is
taught by the bridge's injected grammar fragment and enforced by its parser;
this file never restates it.

Dedup is judged against FRAMEWORK canon only (bridge code + the shipped
`.template` baseline), never against one instance's `hot.custom.*` persona:
this file ships to every instance, so a rule that lives only in one
instance's persona has no home in a fresh mint. That is why the [[OPTIONS]]
real-decisions clause and the Routing section stayed (DGN-1256 round 2).

No estate ticket ID may appear BELOW the marker (DGN-818 C2 product hygiene:
this bridge ships standalone). Rationale lives in the ticket index here.

Rationale index -- why each rule below reads the way it does:
- Options label style / rec marker provenance ......... DGN-1141 (M8 sec.2 B)
- Options body reference tokens: number vs label ...... DGN-1813
- Options one menu per question, never merged ........ DGN-1934, DGN-1997
- Markup glyph palette + apply gate ................... DGN-1141 (M8 sec.2 B)
- Markup `##` glyph absorption ........................ DGN-1254
- Containers: 3 containers / earned-not-layout / fence
  split cost / blockquote always visible / skippable
  gate / caption [미확정] .............................. DGN-1224
- Tables tiers, CJK width grounds ..................... DGN-038, DGN-1141
- Sample fidelity (means only; WHETHER-to-render is
  channel-independent, owner ruling 2026-09-10) ....... DGN-1141, DGN-1373
- Line-budget shrink of this file ..................... DGN-1256

<!-- dogany:inject-below -->

# Telegram vendor contract (judgment + expression rules)

If any machine-behavior statement here diverges from the bridge code, THE
CODE WINS -- report the sentence as a doc bug instead of trusting it.

## Options: judgment rules

- [[OPTIONS]] is for real decision asks only -- never on procedure/step
  lists, never to option-ize safe/reversible work as "shall I do X?".
- One menu per question: each [[OPTIONS]] menu stays on its own message;
  a new menu never touches older ones. Never merge options of different
  questions into one menu. If the same decision must be asked again,
  re-ask it as its own menu.
- Body reference tokens must match the button text exactly: with 2+
  buttons every button shows "N. label", so refer to options by number
  (1/2/3); a single button shows the bare label, refer to it by label.
  Never introduce a separate (a)/(b)/(c) or any other scheme for the same
  choices.
- Label style: neutral action phrases (verb-noun form, e.g. "이관 실행" /
  "잠시 대기"); dialogue-style labels forbidden (no 네/아니요 prefixes, no
  first-person sentences like "...할게요" or "...할까요").
- Recommendation is marked in the body list line AND the bridge carries it
  onto that option's button (agent writes it in the body only; never hand-adds
  it to a label); marker copy ko "(추천)" / en "(rec)".

## Markup

- Markup syntax (bold, single-word italic, inline/fenced code, blockquote
  `> ` / expandable `>!`): machine-enforced in bridge/formatting.py.
- Section glyph palette (SECTION_GLYPHS): default ✅ conclusion / 📌
  grounds+key / 📋 detail+list. Label = glyph + space + text.
- Glyph on a `##` heading: duplicate absorbed (formatting.py); either form OK.
- Apply gate (critical): use glyph sections ONLY when content is long/complex
  and sectioning genuinely reduces burden. Simple/short answers: plain prose,
  no glyphs. No glyph spam. Same 1-slot (simple) / 3-slot (complex/decision)
  judgment as L0.
- Apply surface = message body sections (L0 response skeleton, update
  notices). Out of scope: workbench [LIVE]/[DECISION-PENDING]/
  [CONSOLE-ACTION]/[UNPARK-CANDIDATE] brackets stay as-is; restart-push
  leading emoji prefix stays as-is.

## Containers

The default home for content is plain prose: a container is EARNED by one of
the jobs below -- a container is never page layout. Length alone never picks
the container.

- Code block (fenced ```): home of the rigid-form table (values/numbers
  only -- Tier 1 below) and of long copy material the user runs or pastes
  verbatim (code, commands, config, file text). Nothing else lives in a fence.
- Blockquote (contiguous `> ` lines): home of visual set-off -- the short
  passage the reader's eye must catch (contrast pair, verbatim quote, a
  1-3 line summary) -- and of the one-line copyable snippet.
- Expandable blockquote (first line `>! caption`, then contiguous `> `
  lines): home of skippable detail. It renders collapsed; the reader meets
  the FIRST quoted line before tapping, so write that line as a caption
  naming what is folded, never as the first sentence of the folded
  paragraph. Caption style (length cap, emoji use): [미확정] -- do not
  generalize.

Judgment gate -- plain vs expandable: ask "is the rest skippable?", never
"how many lines?". Material the user must read to decide (option comparisons,
decision grounds) is never skippable -> stays plain at any length.

Table-shaped content routes by the Tables tiers below: Tier 1 lives in the
code block, Tier 2 in the blockquote, Tier 3 in an image.

## Tables

Route by content type, not by hunch. 3 tiers:

- Tier 1: values / numbers / status only, no natural-language column ->
  fenced code block, pipe-aligned, title line on top (separate message).
- Tier 2: option / pros-cons comparison -> blockquote list, NOT image.
  Skeleton: title line + numbered option head "N. label" + child item lines
  + "rec: reason" footer. Option labels follow the Options judgment rules
  above. Stack items vertically per option, never one-line compressed. Item
  marker customizable per context; default = bullet.
- Tier 3: large multi-column CJK grid (natural-language + numbers, many
  columns; e.g. diet table, phase table) -> render image + send_file. ONLY
  image case; do NOT image-render option comparisons.
- Raw markdown table (|col|col|) forbidden.
- CJK text never aligns in a code block -- never attempt a multi-column grid
  in a code block.

## Sample fidelity

- At a UX sample / before-after / render preview, the pane containers here:
  prose UX -> prose; code-block content -> code block. Wrapping prose UX in
  a code block is a violation.
- Escalation: faithful prose impossible (channel strips the markers, or both
  panes render identical) -> render image + send_file (existing Tables image
  path; no new mechanism).

## Routing

- Urgent / live reply -> channel body, short.
- Detail / history / aged info -> console deeplink; body carries the link
  only.
- (Full routing rule: the persona file's Output ownership workflow --
  identity/hot.custom.agent.md.)
