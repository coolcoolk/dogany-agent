# telegram.md -- Telegram channel contract

Scope: applies ONLY when the active bridge channel is Telegram. Other channels / no bridge: this file is inert.

## Markup

- No markdown headers (#, ##, ###) -- rendered raw in Telegram.
- No bold/italic markers (**text**, *text*) -- HTML parse mode does not apply
  to bot messages; raw asterisks leak through.
- Inline code (backtick) and fenced code blocks: allowed.

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
