---
name: dogany-decision-reply-frame
display_name: 결정 답변 정리
description: Apply before asking the user to choose, presenting options or a recommendation, or explaining a structural/design choice. Procedure: prior-answer lookup, Why-distance and proficiency gate, kind-specific material, 3-slot layout, and final send check. NOT for simple one-line factual answers.
---

# dogany-decision-reply-frame

Enforces 3-slot reply structure when reply presents a decision, options, or
structural change. Nudge-tier: auto-fire via description.

## when

Apply if reply contains ANY of:
- decision / recommendation among choices
- numbered options or [[OPTIONS]] list
- structural / design change -> before->after
- comparison of approaches (option A vs option B)

Skip (1-slot only): simple factual answer with no alternatives. Conclusion
line only; do not pad to 3 slots.

## frame

Before drafting:
- Search dogany-memory-search and the decision ledger for an earlier answer to
  the same question. Surface it with its date before asking again. If the issue
  is How (method, plumbing, order, tooling, verification), do not ask: align to
  the prior answer and proceed.
- Gate the question by Why-distance and proficiency: near the user's Why ->
  ask; far -> decide. Greater user proficiency may expose more How. Finish the
  research, comparison, and impact work first; recommend with reasons, while
  leaving the choice to the user.
- Declare the ledger `Kind:`: `shape`, `scope`, or `fork` (Korean aliases
  `모양`, `범위`, `갈림길`). Kits may extend the stance for their surface.

3 slots. Order is fixed: top -> middle -> bottom.

1. conclusion
   - top, 1-2 lines
   - bottom-line answer first, no preamble

2. grounds + mental model
   - middle
   - lead with model/shape, then detail
   - group into 2-3 concepts (never flat-enumerate)
   - if items > 5 -> bucket into concept groups ("really 3 things: A / B / C")
   - structure change: show before -> after
   - complex only: image with core diagram; text stays concise

3. decision
   - bottom
   - numbered list OR one-line next action

## section glyphs (Telegram / SECTION_GLYPHS palette)

Default palette: checkmark pin clipboard (section_glyphs config).
  - slot 1 conclusion  -> checkmark glyph (e.g. check emoji)
  - slot 2 grounds/key -> pin glyph (e.g. pin emoji)
  - slot 3 detail/list -> clipboard glyph (e.g. clipboard emoji)

Apply gate (critical): glyph sections reduce cognitive load -- use ONLY when
content is long/complex and sectioning genuinely helps. Simple/short answers:
plain prose, no glyphs. No glyph spam. Same 1-slot (simple) / 3-slot
(complex/decision) judgment as L0.

Channel rule pointer: the Telegram vendor contract's Markup section
(vendors/telegram.md, injected into bridge sessions by the spawner) is the
authoritative source for glyph identity and out-of-scope surfaces
(workbench brackets, restart-push prefix stay as-is).

## mental-model first

Never open with a flat enumeration ("14 items changed:", "6 files:", etc.).
Group into concept clusters first, then detail inside each cluster.

Example shape (placeholder):
  "Really 3 things: [concept A] / [concept B] / [concept C]."
  Then expand each concept with specifics.

## options contract

Applies when decision slot uses [[OPTIONS]] or a numbered choice list.

- each option: short label + at least one line of substance in body.
  label-only list = violation.
- body reference token == option label token.
  if options are 1 / 2 / 3 -> body refers to 1 / 2 / 3.
  do NOT introduce a separate (a)/(b)/(c) scheme for the same choices.
- mark recommendation on the option's BODY list line (ko "(추천)" / en "(rec)");
  the bridge carries the marker onto that option's button (vendors/telegram.md
  Options rules) -- never hand-add it to a label.
- kind material:
  - shape: render side-by-side detail differences exactly as they will appear
  - scope: state what is in/out, what the excluded part loses, and when
  - fork: state pros and cons for every alternative
- send check: the declared kind's material is present; the user can tell the
  options apart from this text alone; any prior answer to the same question is
  cited with its date. Keep the existing `[[OPTIONS]]` marker/body contract.

### user-visible What -- preview through push first

When asking the user to choose the user-visible What (wording, layout,
buttons, or image), do not describe or approximate candidates in one message.
The real artifact is the choice:

1. Send EACH candidate as its own push, exactly as the recipient would receive
   it. Text and caption must be verbatim. Include that candidate's real
   `--buttons-json`; for an image, send the image rather than a prose summary.
   Use the normal `push.sh --strict` delivery path (and its dry path when the
   task calls for an attached render). Do not add candidate labels or analysis
   inside these preview messages unless the label is part of the proposed What.
2. After every candidate preview, send one short decision message with exactly
   one `[[OPTIONS]]` menu. Its buttons identify the previews briefly; its body
   may recommend one choice, but must not repeat or rewrite the candidates.
3. Never put the pick menu before the previews. Never replace a preview with a
   quoted mockup, screenshot description, or side-by-side paraphrase.

This preview rule specializes `shape`: "exactly as it will appear" means the
candidate itself travels through push, including buttons or image, before the
separate pick menu.

## language

Reply in user's configured language. Do NOT expose internal frame labels
("slot 1:", "mental model:", skill name, doctrine refs) in user-facing text.
Describe structure in the user's own terms.

## tier

BEST-EFFORT. Description auto-fire only. May miss on unusual phrasing.
Not a hook; no guaranteed enforcement.
