---
name: dogany-decision-reply-frame
display_name: Decision Reply Frame
description: Apply when reply contains: a decision among options, a numbered choice list, [[OPTIONS]] buttons, a recommendation among alternatives, or a structural/design change (before->after explanation). Enforces 3-slot layout (conclusion-first / grounds+mental-model / decision), mental-model-before-details grouping, and [[OPTIONS]] body contract. NOT for simple one-line factual answers (single factual lookup, yes/no with no alternatives) -> 1-slot only (conclusion). Trigger phrases: "which option", "recommend one", "compare approaches", "how should I choose", "what is better", selecting among options, comparing approaches, explaining a redesign.
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

Channel rule pointer: telegram.md Markup section (SECTION_GLYPHS + apply gate)
is the authoritative source for glyph identity, bracket-replacement rule,
and out-of-scope surfaces (workbench brackets, restart-push prefix stay as-is).

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
- mark recommendation on the option label itself
  (e.g. "option 1 (recommended)"), not only in prose.

## language

Reply in user's configured language. Do NOT expose internal frame labels
("slot 1:", "mental model:", skill name, doctrine refs) in user-facing text.
Describe structure in the user's own terms.

## tier

BEST-EFFORT. Description auto-fire only. May miss on unusual phrasing.
Not a hook; no guaranteed enforcement.
