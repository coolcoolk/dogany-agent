# DESIGN-SYSTEM -- Dogany unified user-facing output UX

Status: Doctrine + Rule (PM-HIERARCHY: the Why section is doctrine; the How
section is judgeable rule -- violating it is a defect, not a style choice;
the What section is playbook skeleton). Token canon of record:
`agents/.template/routines/lib/design_tokens.py`. This document is that
module's spec of record.
Origin: DGN-376 locked design (owner-confirmed 2026-07-23, grill round 1
M1-M5 amendments folded in). Absorbs DGN-429 (language guard) and DGN-430
(leak guard) as implementation rows of this system. Cross-ref only (surface
differs): DGN-535 (skill rule examples).

## Why -- doctrine

1. The user meets only the language of results. Internal mechanics --
   script names, file paths, API calls, tool plumbing, framework English --
   never surface in user-facing output. Outcomes are described in the
   user's own terms and configured language. (This is the leak problem that
   founded the ticket.)
2. One estate, one visual identity. Every visual artifact a Dogany agent
   emits -- telegram cards and tables, console UI, README/docs diagrams,
   matplotlib charts -- draws from a single brand core. Surfaces may speak
   different registers (a web console is not a telegram card), but they do
   it through declared theme mappings, never through ad-hoc hex.
3. A rule only holds where it has an enforcement point. Prose rules scatter
   and decay; every rule in this system is wired to a mechanical gate (a
   guard seat, an import replacement, a lint) or it is explicitly marked as
   not-yet-enforced. Unenforced rules are tracked as debt, not assumed.

## How -- rules

### R1. Token canon (two-layer schema, grill M5)

- Single source of truth: `agents/.template/routines/lib/design_tokens.py`
  (Python constants module, stdlib-only). Format rationale: every v1
  consumer is Python (card scripts, matplotlib theme module) or generated
  from Python at runtime (console CSS `:root` f-string, grill M1); the
  console consumes a vendored copy either way (R4), so a language-neutral
  file would add a parse step without gaining a consumer. The module ships
  `to_json()` as the language-neutral export for vendoring and drift lint.
- Layer A `BRAND`: named family hues. Canonicalized by measurement from the
  incumbent card palette (teal `#4ECDC4` / amber `#FFD166` / coral
  `#FF6B6B` on navy `#13132B` ground, plus ink/secondary families) and the
  README diagram set. New brand colors are added here and only here.
- Layer B `THEMES`: per-surface mappings filling 11 semantic slots
  (`bg surface border text muted accent green yellow red purple orange` --
  the measured console `:root` vocabulary, adopted estate-wide). A slot
  value is either a Layer A reference (`@name`) or a declared per-medium
  override (`#RRGGBB`). Themes in v1: `card-dark` (pure brand derivation),
  `console-dark` (deliberate GitHub-dark register, zero hex shared with
  card-dark until the reskin decision), `diagram` (muted brand variants on
  deep-teal ground).
- Hardcoding a hex value in a new user-facing visual artifact instead of
  importing the token module is a defect.

### R2. Text register (logical integration, no physical migration)

- The output-register rules live in RULES (Output section): no internal
  mechanics in user-facing text, user-language speech, no asterisk-bold,
  table rendering rules, [[OPTIONS]] contract. This document does NOT copy
  them; it binds them to enforcement points. RULES stays the prose canon.
- Register guard (T2, DGN-429+430 merged): one guard function, three
  mandatory seats in the bridge (ingestion / proactive / finalize -- the
  proactive seat exists because routine pushes bypass finalize, grill M3).
  Strength is staged: v1 log-warn, v2 block/regenerate (v2 needs draft
  recall design because streaming sends drafts early).
- Exemption: Metal (dev agent) is exempt from the register guard
  (owner decision 2026-07-23).

### R3. Enforcement points map

| # | Point | Surface | Mechanism | Status |
|---|-------|---------|-----------|--------|
| 1 | Token import | card scripts (telegram) | replace module constants with design_tokens import | T3 |
| 2 | Register guard | bridge output (3 seats) | guard fn, v1 log-warn | T2 (first to ship) |
| 3 | Token import | matplotlib chart scripts | same import replacement, 4-way | v2 |
| 4 | Drift lint | console vendored copy / doc hygiene | estate-doc-watch R6 lint + skill-creator hint | v2 |

### R4. Cross-repo consumption (grill M4)

- The console is an independent product repo under separate governance. It
  never references a dogany-agent path at runtime; it carries a VENDORED
  COPY of the token module (or its JSON export) plus a drift lint against
  the canon. Console reskin toward the brand core is a product-direction
  decision (owner dec gate), not a side effect of this system.
- Estate instances receive the module through normal template propagation
  (4-way sync); the canon lives only in the canonical repo.

## What -- playbook (skeleton)

### Using tokens from Python

    from design_tokens import BRAND, THEMES, theme
    t = theme("card-dark")
    ax.set_facecolor(t["bg"]); accent = t["accent"]

Card and chart scripts resolve the module path with the same walk-up
pattern as `_find_font_dir()` (routines/lib from the agent root), then
plain-import or importlib-load it.

### Generating console CSS

    from design_tokens import to_css_root
    css = to_css_root("console-dark")   # ready :root block, 11 vars

### Adding or changing a color

1. Is it a new brand-family hue? Add to Layer A `BRAND` with a name.
2. Is it one surface's local register? Declare a `#RRGGBB` override in that
   theme's Layer B spec -- never inline in the artifact.
3. Run the self-check (`python3 design_tokens.py`) and the test suite
   (`routines/tests/test-design-tokens.py`) before committing.

### Diagrams (v1 manual)

No render pipeline exists yet; diagrams are one-off commits. v1 rule:
regenerate manually using the `diagram` theme slots; a generation script is
a v2 item.

## Rollout

- v1 backbone: T1 this document + token canon (done) -> T2 register guard
  v1 (three seats, log-warn; ships first) -> T3 telegram card adapter
  (token import replacement).
- v2 staged: matplotlib token module 4-way (Ag/Warg) / console vendored
  adapter (+ reskin dec) / README regeneration script / estate-doc-watch R6
  lint + skill-creator hint / guard v2 block (draft recall design).
