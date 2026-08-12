# telegram.md -- Telegram channel contract

Scope: applies ONLY when the active bridge channel is Telegram. Other channels / no bridge: this file is inert.

## Markup

- Headers (#/##/###) still forbidden -- rendered raw. Visual hierarchy via emoji+label line instead.
- Bold allowed (correction): `**bold**` or `<b>bold</b>` -> formatting.py renders as <b>. Multi-word OK.
  (Old "no bold" rule was misinformation.)
- Italic: single word only `*word*`/`_word_` -> <i>. Multi-word `*span*` renders raw -> use bold for multi-word emphasis.
- Inline code (backtick) and fenced code blocks: allowed (unchanged).
- Section glyph palette (SECTION_GLYPHS): default `checkmark pin clipboard` (✅ conclusion / 📌 grounds+key / 📋 detail+list).
  Label = glyph + space + text. Bracket ([]) section markers replaced by palette glyphs.
- User custom: config `SECTION_GLYPHS` accepts any set (emoji OR unicode symbols e.g. `◆ ▸ ●`, count 1-N).
- Apply gate (critical): glyph sections are a cognitive-load tool, not decoration -- use ONLY when content
  is long/complex and sectioning genuinely reduces burden. Simple/short answers: plain prose, no glyphs.
  No glyph spam. Same 1-slot (simple) / 3-slot (complex/decision) judgment as L0.
- Apply surface = message body sections (L0 response skeleton, update notices).
  Out of scope: workbench [LIVE]/[DECISION-PENDING]/[CONSOLE-ACTION]/[UNPARK-CANDIDATE] brackets stay as-is;
  restart-push leading emoji prefix stays as-is.

## Tables

Route by content type, not by hunch. 3 tiers:

- Tier 1: values / numbers / status only, no natural-language column -> fenced code
  block, pipe-aligned, title line on top (separate message).
- Tier 2: option / pros-cons comparison -> blockquote list, NOT image.
  Skeleton: title line + numbered option head "N. label (rec)" + child item lines +
  "rec: reason" footer. Stack items vertically per option (never one-line compressed --
  cognition collapses).
  Item marker customizable per context; default = bullet (e.g. +/- pair, signal-light
  emoji as fit).
- Tier 3: large multi-column CJK grid (natural-language + numbers, many columns;
  e.g. diet table, phase table) -> render image + send_file (see Files contract below).
  ONLY image case; do NOT image-render option comparisons.
- Raw markdown table (|col|col|) forbidden -- CJK/emoji widths break ASCII grids.
- CJK text never aligns in a code block (glyph width != 2 cells) -- never attempt a
  multi-column grid in a code block.
