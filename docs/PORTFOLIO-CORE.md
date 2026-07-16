# PORTFOLIO-CORE.md -- universal portfolio schema, core v1

Registry of record for the portfolio core spec. This document IS the product
home of the core version counter: `core: 1` is defined here, and core
revisions (E0 bumps) ride this repository's release machinery from the
release that first ships this file. Home ratified by owner decision dec-035
(2026-07-16). Provenance (non-normative): the spec was designed and locked as
DGN-350 v2 after two adversarial grill rounds plus a real-code round against
an executable gate; this document restates that locked content as the
self-contained normative text.

Shipped tooling (this repo):

| surface | path |
|:--|:--|
| structural/schema lint | `agents/.template/routines/lib/portfolio-core-lint.py` |
| generic parse entrypoint | `agents/.template/routines/lib/portfolio-core-parse.sh` |
| weekly reconcile skeleton | `agents/.template/routines/portfolio-reconcile.sh` |
| conversational setup + soft migration | `skills/dogany-portfolio-setup` |
| self-test suite + fixtures | `agents/.template/routines/tests/test-portfolio-core.py` |

## 0. Scope

One human owner, one agent writer per artifact. Multi-user / multi-writer
estates are out of scope; the single-writer convention is load-bearing in
every element below.

An "index" is the estate's portfolio artifact: one row per managed unit
(project, repo, instance, ...). It may be a persisted md table, a
structure-equivalent JSON object, or a live-derived view whose defining tool
carries the contract.

## 1. Core elements

Common core = E0 core versioning, E1 existence layer, E2 declared-state
discipline, E3 reconciliation loop, E4 freshness + checker liveness, E5
retirement, E7 governance pointer, E8 column provenance rule, E9 storage +
header contract -- plus one OPTIONAL layer, E6 edges, with a defined adoption
trigger. Adoption is opt-in via presets (section 4). Everything else is
extension (section 3).

### E0. Core versioning

- The core spec is versioned by a single integer. This document = core 1.
- Every index carries the marker: persisted md index -> header line
  `core: <N>`; JSON index -> first field `core_version`; live-derived index
  -> the marker lives in the deriving tool's config (the header contract
  lives wherever the index is DEFINED, not necessarily where it is rendered).
- Marker semantics (two distinct cases):
  - Marker ABSENT: treated as C=0 -- the pre-versioning legacy grandfather
    state. Structural checks only + WARN. The absence is meaningful and
    intentional; it is the soft-migration entry state.
  - Marker PRESENT but malformed (unparseable integer, invalid field name,
    wrong syntax): parse-or-die. A malformed marker is NEVER a silent C=0
    fallback.
- Consumer behavior for cross-estate shared tooling, given lint-supported
  version S and index version C (instance-local lints may pin a local S in
  their config, isolating the instance from version heterogeneity until it
  opts in):
  - C > S: fail-closed. Error token `CORE-VERSION-AHEAD`, parse-or-die class;
    the consumer takes its no-index fallback path. Never guess forward.
  - C = S: full lint.
  - C < S: lint applies version-C PUBLISHED rules; support window = at least
    S-1. Older than the window: structural checks only + WARN "conforms to
    older core C". Visible heterogeneity, never silent. Applying
    current-version rules relabeled as "version-C rules" is FORBIDDEN.
- Ownership: this repository owns core revisions and bumps N through its
  release machinery. Revision protocol: additive changes preferred; any
  change to core columns, the header contract, or lint rules = version bump +
  migration note in the release notes. Sovereign instances upgrade opt-in at
  their own gate -- the marker makes heterogeneity DETECTABLE; it never
  forces migration.

### E1. Existence layer: derived where discoverable, verified multi-source

- WRITER: a unit that is machine-discoverable (declared marker file / dir
  convention) MUST enter the index via the scanner and MUST NOT be
  hand-listed; the scanner is the single writer of derived existence rows.
- VERIFIER: existence is never trusted to one enumeration source. The
  reconcile loop (E3) diffs the index against AT LEAST TWO independent
  enumeration sources, of which the marker scan is one. Eligible second
  sources: process/scheduler labels, registry/config files, directory
  listings, session registries, backlog/state file globs.
- Why the second source is mandatory: marker-based derivation covers MARKED
  existence only, and marker application is itself a curated act with no
  reconciliation. A unit registered in one surface but missing its marker is
  structurally invisible to a single-glob check; a registry-vs-glob diff
  catches it mechanically (live incident evidence behind this rule).
- Diff semantics: identifier present in any source but absent from index AND
  exclusion list = FAIL (no silent enrollment). Row whose location is absent
  on disk = FAIL, routed to retirement (E5). The exclusion list absorbs false
  positives: item + one-line reason + date, printed IN FULL on every run
  (visibility is the check on the list's own autonomy).
- Non-discoverable units (remote machines, no reachable marker): curated rows
  are allowed but MUST carry reconciliation coverage or an explicit
  accepted-gap token.
- Estates with genuinely ONE enumeration surface declare
  `enum-sources: <src> + accepted-gap` in the header -- the softness is
  visible, never silent.
- Discovery markers are per-instance DECLARED, not framework-standardized:
  the header names the marker(s) in force; shared tooling reads the
  declaration.

### E2. Declared state: closed enum + machine lint, scoped to columns that exist

- Any state column that exists has a closed kebab-case vocabulary and a lint;
  open vocabularies are forbidden. ABSENT state columns are legal
  (narrative-first estates run state = `-`). The core does not mandate a
  status column.
- GRANDFATHER MODE (adoption over a legacy corpus): parse-or-die applies to
  the INDEX artifact only, never to the legacy ground-truth corpus.
  Sequencing: (1) declare the closed enum + a mapping-at-read table (legacy
  value -> core token); (2) enum lint runs REPORT-ONLY over legacy artifacts
  -- violations are listed, rows are KEPT, and the burn-down count MUST
  surface on the designated E4 liveness terminal; (3) new writes use the
  closed enum from day one; (4) cutover to enforcing lint is an explicit,
  dated flip after burn-down or bulk-map.

### E3. Reconciliation loop: periodic diff of declared state vs ground truth

- Contents of the pass: (a) declared-state cells vs ground truth (git log /
  PR merge state / process / file presence / mtime); (b) cross-artifact
  consistency (all registration surfaces agree); (c) existence multi-source
  diff (E1); (d) disappearance detection (E5).
- Derivation covers EXISTENCE, not STATE. Derive what you can; reconcile what
  you declare; judgment cells are reviewed, not diffed (E8).
- Cadence: weekly default -- stated as the guarantee it actually is:
  "declared state may be wrong for up to 7 days." Estates with tighter
  tolerance shorten the cadence.
- The loop is itself a checker and inherits E4 liveness obligations.

### E4. Freshness + checker liveness

- Every persisted artifact carries a generated-at stamp; a routine checks
  staleness thresholds (default >30d WARN).
- CHECKER LIVENESS: "derived cells cannot lie" is false when the scanner is
  dead -- the cells then lie by omission. Every scanner / checker /
  reconciler MUST surface its own last-run timestamp at a designated
  HUMAN-VISIBLE terminal surface (console badge, report line the owner
  routinely sees). Absence of output is never the only failure signal.
- The who-watches-the-watcher regress terminates BY DECLARATION at that one
  designated surface: the header (E9 item 7) names it. Beyond it = accepted,
  documented residual.
- Live-derived indexes are exempt at index level (fresh by construction) but
  their declared INPUTS need per-record timestamps.

### E5. Retirement: disappearance is a recorded transition, never a silent drop

- Derived indexes lose deleted units silently BY CONSTRUCTION (dir gone ->
  row gone on next regeneration, zero trace). The core minimum: (a) the
  reconcile diff REPORTS every disappearance (E1 reverse check); (b) the
  index RECORDS it -- either a row state token (`retired`/`frozen`) or a
  tombstone entry (id + date mandatory; reason optional, curated) that
  regeneration preserves. The tombstone core fields are written by the
  index's DESIGNATED WRITER off the reconcile diff, never by the scanner
  directly: the scanner surfaces the disappearance, the writer commits the
  record.
- Live-derived profiles keep a minimal persisted retired-list -- the one
  persisted fragment such a profile requires.
- Full lifecycle machinery (frozen role transitions, renamed-pointers with
  grace cycles, consumer cache draining) stays extension territory, justified
  only where machine consumers cache row ids.
- REGENERATION-SURVIVOR FIELDS: tombstone entries, reviewed stamps, and the
  exclusion list are curated/judgment-class fields a scanner cannot
  re-derive. In the derived-full profile, the carry-forward store (persisted
  sidecar or preserved in-index blocks) holding them across regeneration is a
  REGISTERED SCANNER INPUT: the scanner reads it before regenerating, merges
  the preserved blocks into the fresh output, and treats its absence as WARN.
  The store is declared in the header contract.

### E6. Relationships: OPTIONAL edge layer, off by default, with counting rules

- Layer shape when adopted: typed rows (type / direction / mechanism /
  discipline_ref / health); endpoints are row ids plus a closed exception
  list; new exception = schema change.
- Counting rules (the trigger runs on these):
  - R1 managed unit = an actual or would-be row of THIS instance's index.
  - R2 cross-unit invariant = standing state held at two or more managed
    units that must co-vary for correctness (copy + SHA pin, version pair,
    copy-set equality, schema-version lockstep). Message lanes, event and
    delegation flows, and one-shot handoffs hold no standing co-varying state
    and are NOT invariants. QUALIFIER: skew that is versioned and
    tolerance-managed with its own detection mechanism (schema_version field,
    content_hash check) is NOT a silent-break invariant -- it counts toward
    the trigger only via clause (b).
  - R3 declared state ABOUT a unit vs that unit's own ground truth is
    reconciliation (E3), never an edge -- regardless of which artifact holds
    the declaration.
  - R4 distinct type = distinct invariant CLASS; N instantiations of one
    class count once.
  - R5 endpoints outside the managed set (external trackers, owner, console)
    do not count toward the trigger. The trigger governs ADOPTION only; once
    adopted, the layer MAY additionally index non-invariant relations
    (gates, flows, views) as post-adoption, profile-local content.
- Trigger: adopt edges when EITHER (a) two or more distinct cross-unit
  invariant classes exist between managed units, OR (b) post-incident: a
  cross-unit invariant broke with no signal at the consuming end -- clause
  (b) is explicitly a post-mortem rule; edge adoption is the corrective act.
- A layer is adopted whole with its discipline, never cherry-picked.

### E7. Governance: prose, with a machine-readable pointer only

- Core requirement is limited to: (a) gate-bearing cells may carry a
  `gate=<actor>` token that lint checks for PRESENCE, never semantics;
  (b) the index header states its own update authority in tiers (schema
  change = owner gate; content = agent autonomous). The gate itself lives in
  the estate's governance prose (RULES/AGENT class docs) and changes via
  those docs' procedures.

### E8. Column provenance rule (the schema's central claim)

- Every column is classified `derived | declared | judgment`; the class is
  DECLARED PER COLUMN in the header contract and dictates the discipline:
  - derived: scanner-owned, hand-edit forbidden; subject to checker liveness
    (E4).
  - declared: closed enum + lint (E2) + reconciliation (E3).
  - judgment: curated; changes follow governance tiers (E7); NOT diffed by
    the reconcile loop -- REVIEWED instead: each row carries a
    `reviewed: <date>` stamp covering its judgment cells (per-cell stamp
    tokens allowed), and the freshness routine applies the staleness
    threshold to it. Fiction between reviews cannot be machine-prevented, but
    STALE review is machine-visible.
- ARBITER: provenance class assignment and change are SCHEMA-tier acts --
  tier-1 (owner-gate) authority, never content-tier autonomy. Misclassifying
  a column to dodge discipline is a gate violation, not a judgment call.
- MISCLASSIFICATION DETECTOR (heuristic, labeled as such): the weekly
  reconcile-coupled lint flags any declared- or judgment-class column whose
  observed value set is reproducible by existence enumeration alone -> WARN
  "derivable column is hand-maintained -- reclassify or justify". Scoped to
  existence-enumeration reproducibility; full value-derivation checking would
  need a per-column derivation registry, which is NOT core (extension).
- This rule dissolves the derive-vs-curate contradiction: derive what is
  derivable, curate what is judgment -- with the class assignment itself
  gated and partially machine-checked.

### E9. Storage + header contract

- Format conventions: stable BEGIN/END section markers; one row = one line;
  no pipes in cells; empty = `-`; multi-value separator `;` only;
  parse-or-die on every read (marker pairs + column count == header count).
  Freshness stamp per E4.
- COLUMN BINDING: consumers bind columns by HEADER NAME, never by position.
  Extension-column appends are non-breaking; core column renames/removals are
  core-version bumps (E0).
- HEADER CONTRACT -- the index header (or defining config) MUST declare the
  following 7 items. For each: malformed (present but unparseable per
  grammar) = parse-or-die; absent = parse-or-die EXCEPT where noted.
  1. `core: <N>` -- positive integer; absent = C=0 grandfather (NOT an
     error); malformed = die.
  2. Update authority tiers -- tier-1 (owner gate) and tier-2 (agent
     autonomous) scope; absent or tier labels missing = die.
  3. Discovery marker(s) in force -- one or more named marker identifiers;
     absent = die; malformed = die.
  4. Enumeration sources -- source list; minimum two sources, or exactly one
     source with `+ accepted-gap`; absent = die; malformed = die.
  5. Per-column provenance class map -- every index column maps to one of
     `derived | declared | judgment`; absent = die; unknown class token or
     unmapped column = die.
  6. Exclusion list -- zero or more entries `item + one-line reason + date`;
     empty list legal; absent block = die; malformed entry = die. Printed in
     full on every lint run.
  7. Designated liveness terminal surface -- exactly one named surface
     identifier; absent = die; malformed = die.
- Substrate: md table is the DEFAULT; structure-equivalent JSON is LEGAL --
  record = object, keys = header names, header contract as leading fields,
  same closed enums and single writer. Parse-or-die maps to JSON as: JSON
  parse failure OR key-set mismatch (required contract key absent, or
  unknown key not declared in the column class map) = die; no silent
  fallback, no partial parse.

### Minimal row contract

Each core column has an ALLOWED CLASS SET; the instance picks ONE class per
column and declares it in the header map:

| column | allowed classes | notes |
|:--|:--|:--|
| id | declared OR derived | scanner-minted ids = derived; hand-assigned = declared. Stable grep key, kebab-case, non-empty on every row |
| location | derived (discoverable) OR declared (remote, with E1 curated-row duties) | path or URL or `-` |
| state | declared OR absent (`-`) | closed enum when present (E2) |
| last_activity | derived (activity source exists) OR declared (then reconciled per E3) | git/mtime-derived where possible |

Everything beyond these four (version, phase, role, tier, deploy, counts,
health, ...) is extension territory. Version facts live in their derived
home; the index holds a pointer or derived cell only -- never a hand-typed
literal (one fact, one home).

## 2. Encodings (core v1, normative)

### 2.1 Canonical header encoding (md persisted index)

- Header contract items are encoded as comment lines in the region before the
  first section marker: `# <key>: <value>`, one item per line. Writers MUST
  emit the leading `#`; lint accepts an optional missing `#` (strict write,
  tolerant read).
- Keys are exact kebab-case tokens matched at line start (after `#` strip).
  Loose substring matching is FORBIDDEN.
- Canonical key registry (closed for core v1): `core`, `generated`,
  `update-authority`, `discovery-marker`, `enum-sources`, `class-map`,
  `liveness-terminal`, `carry-forward-store` (optional). The exclusion list
  and the tombstone store are marker-pair BLOCKS, not key lines.
- Block marker registry (closed for core v1): `PORTFOLIO:EXCLUDE`,
  `PORTFOLIO:TOMBSTONE`, `PORTFOLIO:ROWS`, `PORTFOLIO:EDGES` -- each
  `<!-- <NAME>:BEGIN -->` / `<!-- <NAME>:END -->`. EXCLUDE and ROWS: exactly
  once. EDGES: exactly once WHEN the edge layer is adopted (absent = not
  adopted = legal). TOMBSTONE: conditional (2.4). BEGIN before END; duplicate
  or out-of-order markers of ANY registered block = die.
- Duplicate occurrences of one registered header key = malformed = die.
  Silent first-wins is forbidden -- a second `core:` or `class-map:` line is
  a contradiction, not a fallback.

### 2.2 Key grammars

- `core`: positive integer. `core: 0` PRESENT = die; C=0 is reachable ONLY
  via marker absence. At S=1 the C<S older-window branch is dormant; when a
  core-2 spec exists, the lint MUST apply the older version's PUBLISHED
  rules, never current rules relabeled.
- `update-authority`: REQUIRED key line whose one-line value contains both a
  tier-1 and a tier-2 label (case-insensitive). Whole-header prose scanning
  is FORBIDDEN. Longer prose tier blocks remain legal as documentation but do
  not substitute for the key line.
- `discovery-marker`: one or more identifiers, comma-separated; a
  parenthetical annotation may follow each identifier. At least one non-empty
  identifier, else die.
- `enum-sources`: comma- or semicolon-separated source list, optionally
  followed by `+ accepted-gap`. `+ accepted-gap` is legal ONLY with exactly
  one source; two or more sources plus the token = die (fail-closed: an
  estate with two sources has no single-surface gap to accept).
- `class-map`: single header line; pairs `col:class`, comma-separated; class
  from {derived, declared, judgment}. The `=` pair form is illegal. ANY
  non-empty token that does not parse as `<col>:<class>` = die -- silent
  pair-skipping is forbidden. Every index column appears in the map; there is
  NO requirement that all three classes each appear (an all-derived index is
  legal). The map's physical grammar is encoding-tier; its CONTENT (which
  class each column gets) is schema-tier and changes only through tier-1.
- `liveness-terminal`: exactly ONE surface identifier (kebab-case token). A
  comma/semicolon list = die (two surfaces = nobody's surface).
- `generated`: ISO-8601 timestamp. ABSENT = WARN (E4 mandate, not an E9
  die-item; enforcement teeth are the reconcile staleness check); MALFORMED =
  die. Not checked at C0.
- Exclusion entry: `- <item> | <reason> | <date>` -- exactly three
  pipe-separated fields, all non-empty, date ISO `YYYY-MM-DD`. Fewer or more
  fields, empty field, non-ISO date, or ANY non-blank non-comment line inside
  the EXCLUDE block not starting with `-` = die (no warn-and-skip).

### 2.3 Table encoding (D-19 class)

Inside a marked section, every non-blank line MUST be consumed as the header
row, THE separator row, or a data row -- anything else = die. Separator
recognition is POSITIONAL: exactly the one line immediately following the
header row is the separator; content-based separator sniffing on any other
line is FORBIDDEN. The positionally-consumed separator line must actually BE
a separator (every cell dashes with optional colons) -- anything else = die,
so a table missing its separator row can never silently eat its first data
row. Prose junk inside a section is die, not skip. An all-dash line anywhere
after the separator is DATA.

### 2.4 Tombstones (E5 encoding)

    <!-- PORTFOLIO:TOMBSTONE:BEGIN -->
    - <id> | <date> | <reason or ->
    <!-- PORTFOLIO:TOMBSTONE:END -->

- Entry grammar: exactly three pipe-separated fields; id non-empty; date ISO
  `YYYY-MM-DD`; reason optional (`-` when absent).
- Cross-check (lint duty): every row whose `state` cell is `retired` or
  `frozen` MUST have a TOMBSTONE entry with matching id; duplicate ids within
  the block = die. A block entry WITHOUT a matching row is legal -- that is
  the post-regeneration trace the derived-full profile needs; the in-index
  TOMBSTONE block IS the in-index variant of the E5 carry-forward store. A
  sidecar variant is declared via the optional `carry-forward-store:
  <relpath>` header key.
- Block presence: required iff at least one retired/frozen STATE row exists;
  otherwise absent or empty is legal. It is E5 machinery, NOT an eighth
  header item -- the 7-item list is closed.
- The core lint keys the cross-check on the core `state` column ONLY.
  Extension columns (e.g. a `role` column with a `frozen` value) are
  instance-local vocabulary; tying them to tombstone duties is an
  instance-extension lint rule, not core.
- The tombstone date lives in the block. There is NO core tombstone-date
  column, and `last_activity` NEVER doubles as the tombstone date -- the two
  facts are distinct, and doubling recreates one-fact-two-homes drift.
- E5 at C0: tombstone WELL-FORMEDNESS is structural and runs (and may die)
  at C0 -- scoped to the id-non-empty check on retired/frozen-state rows;
  the block cross-check applies from core:1.

### 2.5 JSON encoding

Structure-equivalent JSON index = one top-level object with:

- Leading contract fields (kebab keys with `_`): `core_version` (FIRST field
  -- order-significant), `generated`, `update_authority` (object: `tier_1`,
  `tier_2`, both strings), `discovery_markers` (non-empty string array),
  `enum_sources` (object: `sources` string array + `accepted_gap` bool; the
  accepted-gap rule applies in both directions), `class_map` (object: column
  -> class), `exclusion_list` (array of {item, reason, date}; may be empty,
  must be present), `liveness_terminal` (string, one identifier).
- Data fields: `tombstones` (array of {id, date, reason?}; presence rule per
  2.4), `rows` (array of objects; keys = column names, all keys in
  class_map), `edges` (array; required only when the edge layer is adopted),
  optional `carry_forward_store`.
- Die conditions: JSON syntax failure; any required contract key absent; any
  unknown top-level key; any row key not in class_map. Only
  core_version-first is order-significant.
- `core_version` ABSENT = C0 grandfather (structural checks only + WARN),
  same as the md path; PRESENT but malformed = die.

### 2.6 State enum residence

Enum vocabularies are LINT-CONFIG-RESIDENT, not header items -- each
instance's lint / deriving-tool config declares the closed vocabulary per
declared-class column (registry-of-record pattern). The header stays 7 items;
cross-estate shared tooling without the config performs structural checks and
skips enum domains. Core-reserved exception: the E5 state tokens `retired` /
`frozen` and the empty sentinel `-` are lint-checkable without config; any
OTHER value in a `state` column whose enum config is absent = WARN
"undeclared state vocabulary" (visible softness, never silent).

## 3. Extension mechanism

1. Extension columns: appended to the RIGHT of core columns (md) /
   additional keys (JSON), order stable within an instance and irrelevant to
   consumers (name binding). Core lint validates core columns (presence +
   domain); extension columns are checked structurally plus each MUST appear
   in the header class map (E8 discipline applies).
2. Extension artifacts: anything beyond the index (per-project status DBs,
   backlog JSONs, deploy runbooks, dashboards, narrative worklogs) stays
   local, reachable via pointer grammar `see:<relpath>` / `pin:<relpath>`.
   One fact, one home -- the index never duplicates a fact that has a home
   elsewhere.
3. Optional layers: edges (E6). A layer is adopted whole with its discipline,
   never cherry-picked.
4. Presets: section 4.

## 4. Adoption model -- opt-in, option matrix + presets

- ADOPTION GATE (normative): a product home for the core spec MUST exist
  before any second instance adopts. THIS DOCUMENT is that home; from the
  release that ships it, the gate is satisfied for every adopter.
- Evidence a mandate fails: templates shipped without an integrated structure
  around them go unused, and unused scaffolds create the ILLUSION of coverage
  while the real state store drifts unchecked. Hence: conversational
  integrated offer at onboarding (skills/dogany-portfolio-setup), never an
  orphaned pre-created file.
- The SCHEMA is the option matrix; presets are named points in it:

| option | values |
|:--|:--|
| index persistence | persisted / live-derived |
| existence writer | scanner / curated-with-recon (mix per row class) |
| state machinery | closed-enum columns / none (`-`) |
| edges | on (E6 trigger) / off |
| retirement | core-minimal / full lifecycle (extension) |
| extension columns | free, header-declared |

- PRESETS = four observed working configurations, offered as onboarding
  conveniences -- explicitly NOT a taxonomy (new estates compose options
  directly):

| preset | configuration |
|:--|:--|
| derived-full | persisted scanner-written index, full derivation, ticket enums + lint (grandfather mode for legacy), edges off until trigger |
| live-derived | live index (persist on first non-interactive consumer), full derivation, closed enums + reconcile cron, minimal persisted retired-list (E5), edges off |
| manifest-min | persisted minimal manifest, ls/conf-derived where possible, NO state machinery (state `-`), narrative worklog untouched, edges off until trigger |
| curated-full | persisted curated+lint index, derive discoverable rows, full enums + tags + reconcile, edges on, full retirement lifecycle |

- NAMESPACE RULE: preset names never appear in row cells. Row-level role
  enums are instance-local extension vocabulary, disjoint from preset names
  by construction.
- STRADDLE RULE: an estate matching multiple presets takes the ADDITIVE UNION
  of enabled options; where two presets imply different disciplines for the
  same column class, the STRICTER discipline applies.
- Empty scaffolds are never pre-installed. "None" is a legal answer that
  costs nothing. Narrative-first is first-class, not degenerate: manifest-min
  obligates only a tiny manifest plus weekly freshness; narrative practice is
  preserved untouched.
- Escalation by trigger, not calendar: persist the index when a
  non-interactive consumer appears; adopt edges on the E6 trigger; adopt the
  reconcile cron the moment any declared-state column exists; record
  retirements from day one (E5 is not optional).

## 5. Migration playbooks (soft, generic)

Common frame: adoption is NEVER a forced rewrite. Tooling and the setup skill
arrive via a framework update; every EXISTING artifact stays authoritative
and untouched; a pre-existing index without a `core:` marker reads as C=0
grandfather (structural checks + WARN -- meaningful, intentional); cutover to
`core: 1` is an explicit, dated, per-instance opt-in executed by that
instance's agent with its operator. Nothing phones home: sovereign instances,
no rollup, no cross-estate reader.

The per-preset playbooks (derived-full / live-derived / manifest-min /
curated-full step lists) ship with the setup skill:
`skills/dogany-portfolio-setup/presets.md`. That file is the working copy;
on conflict this document wins.

## 6. Lint vs reconcile boundary (duty assignment)

- LINT (every read, parse-or-die -- `portfolio-core-lint.py`): substrate
  parse (blocks, tables, column counts, no-silent-drop); header contract
  items 1-7 with the section-2 grammars; E8 class-map coverage; E5 tombstone
  block grammar + row/block cross-check; exclusion list full print;
  `generated` stamp presence/parse; core-column presence.
- RECONCILE (weekly pass -- `portfolio-reconcile.sh` skeleton +
  instance extensions): E1 multi-source existence diff; E3 declared-state vs
  ground truth; E4 staleness thresholds + checker liveness; E5 disappearance
  DETECTION and tombstone COMMIT (writer role); E8 misclassification
  detector; E2 grandfather burn-down surfacing.
- Shipped v1 skeleton covers the generic subset: lint run, staleness,
  existence diff over three source types (dir-glob, file-glob,
  launchd-prefix), disappearance report, exclusion full print. E3
  state-vs-truth diffs and the E8 detector need instance knowledge and are
  instance-extension duties (playbooks name them per preset). The framework
  never pre-registers the cron; the setup skill wires the trigger.

## 7. Known residuals (accepted, documented)

- E0 fail-closed on CORE-VERSION-AHEAD can degrade every consumer at an
  instance that upgrades its index before its tooling -- designed fail-closed
  (degraded-visible-reversible), upgrade ordering is discipline.
- E2 grandfather mode has no automatic cutover mechanism; the burn-down count
  on the liveness terminal makes progress visible, the flip stays discipline.
- E4 ends the watcher regress by declaring one human-visible surface; whether
  the owner actually looks is not machine-verifiable.
- E5 forces one persisted fragment (retired-list) into the live-derived
  profile -- a small contradiction of that profile's premise, accepted for
  trace durability.
- Single-surface estates carry a visible accepted-gap token; the blindness
  itself remains.
- `reviewed` stamps measure review OCCURRENCE, not quality.
- E6 counting rules were calibrated on four estates; a fifth estate that
  misclassifies revises the rules, not the estate.
- Enum unification across estates (one framework-wide vocabulary vs
  per-instance closed enums) is OPEN -- undecidable at current sample size;
  per-instance closed enums stand.
