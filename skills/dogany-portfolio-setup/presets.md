# presets.md -- portfolio core v1: option matrix, presets, starter templates,
# migration playbooks (working copy for dogany-portfolio-setup).
# Registry of record: docs/PORTFOLIO-CORE.md in the framework repo
# (dogany-agent canonical). On any conflict, that doc wins.

## Option matrix (the schema IS the matrix; presets are named points in it)

| option | values |
|:--|:--|
| index persistence | persisted / live-derived |
| existence writer | scanner / curated-with-recon (mix per row class) |
| state machinery | closed-enum columns / none (`-`) |
| edges | on (E6 trigger) / off |
| retirement | core-minimal / full lifecycle (extension) |
| extension columns | free, header-declared |

## Presets (onboarding conveniences, NOT a taxonomy)

| preset | matches you when ... | configuration |
|:--|:--|:--|
| derived-full | a scanner can derive everything from ground truth (state files, git, tickets) | persisted scanner-written index, full derivation, ticket enums + lint (grandfather mode for legacy), edges off until trigger |
| live-derived | the index is rebuilt per query and only you read it | live index (persist on first non-interactive consumer), full derivation, closed enums + reconcile cron, minimal persisted retired-list, edges off |
| manifest-min | narrative records are primary; you just need a tiny fleet/unit manifest | persisted minimal manifest, ls/conf-derived where possible, NO state machinery (state `-`), narrative worklog untouched, edges off until trigger |
| curated-full | judgment cells (roles, lanes, health calls) matter as much as derived facts | persisted curated+lint index, derive discoverable rows, full enums + tags + reconcile, edges on, full retirement lifecycle |

Rules that always apply:
- STRADDLE: matching multiple presets = additive union of options; where two
  presets imply different disciplines for one column class, the STRICTER
  discipline applies.
- NAMESPACE: preset names never appear in row cells (row-level role enums are
  instance-local extension vocabulary, disjoint by construction).
- Empty scaffolds are never pre-installed. "None" is a legal answer.
- Escalation by trigger, not calendar: persist when a non-interactive
  consumer appears; edges on the E6 trigger; reconcile cron the moment any
  declared-state column exists; retirements recorded from day one (E5 is not
  optional).

## Starter index template (md, persisted presets)

Replace every <...> placeholder; delete the TOMBSTONE block if no
retired/frozen row exists yet; delete the EDGES block unless the edge layer
is adopted. The header keys and block markers are a CLOSED registry -- do not
invent new ones.

    # PORTFOLIO.md -- <estate one-liner>
    # core: 1
    # generated: <YYYY-MM-DDTHH:MM>
    # update-authority: tier-1 = owner gate (schema/column changes); tier-2 = agent autonomous (row content)
    # discovery-marker: <marker(s) in force, comma-separated, annotations in parens>
    # enum-sources: <source-a>, <source-b>            (or: <only-source> + accepted-gap)
    # class-map: id:declared, location:derived, last_activity:derived<, col:class ...>
    # liveness-terminal: <one surface id, e.g. weekly-report-line>

    <!-- PORTFOLIO:EXCLUDE:BEGIN -->
    <!-- PORTFOLIO:EXCLUDE:END -->

    <!-- PORTFOLIO:ROWS:BEGIN -->
    | id | location | last_activity |
    |:--|:--|:--|
    <!-- PORTFOLIO:ROWS:END -->

Column contract (core v1): `id` (non-empty, kebab-case, stable grep key),
`location` (path/URL or `-`), `last_activity` (derived where an activity
source exists, else declared+reconciled); `state` column optional -- when
present it is a closed enum (declare the vocabulary in the lint config;
`retired`/`frozen`/`-` are core-reserved). Every column MUST appear in
class-map with one class from {derived, declared, judgment}. Extension
columns append to the RIGHT; consumers bind by header NAME, never position.
Exclusion entries: `- <item> | <one-line reason> | <YYYY-MM-DD>`.
Tombstone entries (required iff a retired/frozen STATE row exists):
`- <id> | <YYYY-MM-DD> | <reason or ->` inside PORTFOLIO:TOMBSTONE markers.

JSON substrate is legal too (structure-equivalent; `core_version` first
field, same 7 contract items as snake_case keys) -- see docs/PORTFOLIO-CORE.md
for the exact key set.

## Migration playbooks (soft, per preset -- existing artifacts stay
## authoritative until the dated cutover flip)

### derived-full (scanner-derived estate with a legacy status corpus)
1. Header contract + `core:` marker land on the INDEX only; the legacy
   ground-truth corpus is never parse-or-die material (grandfather mode).
2. Declare ONE closed status enum + a mapping-at-read table (legacy value ->
   core token). Enum lint runs REPORT-ONLY over legacy artifacts; rows are
   kept; the burn-down count surfaces on the liveness terminal.
3. New writes use the closed enum from day one; cutover to enforcing lint is
   an explicit, dated flip after burn-down or bulk-map.
4. Checker liveness: the scanner/cron surfaces its own last-run line on a
   surface the owner actually reads (a missing output file is never the only
   death signal).
5. Second enumeration source: diff the marker/dir convention against the
   state-file glob; unreachable units become exclusion-list entries with
   reasons instead of prose TODOs.
6. Regeneration-survivor fields (tombstones, reviewed stamps, exclusions) go
   into a carry-forward store the scanner reads before regenerating
   (`carry-forward-store:` header key, or in-index preserved blocks).

### live-derived (index rebuilt per query)
1. The header contract lives in the DERIVING TOOL's config, not as a new
   file over the data (E9 live-derived clause).
2. Register the weekly reconcile cron: declared backlog/state items vs
   git-log / PR-merge reality (instance-extension logic -- the shipped
   skeleton does lint/staleness/existence/disappearance; the state-vs-git
   half is implemented locally), plus the E1 multi-source diff (config glob
   vs session registry vs prefix map vs backlog-file set).
3. Create the minimal persisted retired-list (E5) -- the one new persisted
   fragment this profile requires.
4. Persist a snapshot only when a non-interactive consumer appears.

### manifest-min (narrative-first estate, tiny manifest)
1. Create the manifest: core 4 columns + chosen extensions (e.g. bot, domain,
   tier, fw_version, modules), FULL E9 header from day one -- no grandfather
   needed when no legacy index exists.
2. Existence derived from the unit-directory listing; second source =
   scheduler labels (launchd/systemd). VERIFY at setup time that per-unit
   conf files actually yield the derived cells; where they do not, those
   cells are declared-class, not derived (a preset-internal knob).
3. Day-one E5: any already-retired unit enters as a retired row + TOMBSTONE
   entry (last_activity never doubles as the tombstone date).
4. Register the weekly reconcile cron (the shipped skeleton covers this
   profile fully: lint + staleness + dir/label diff + disappearance report).
5. Explicitly NOT adopted: ticket status machinery (state `-`), worklog
   stays narrative, message lanes stay prose+scripts (below the E6 trigger).

### curated-full (judgment-heavy estate)
1. Scanner-feed the locally-globbable rows; exclusion list absorbs debris.
2. Explicit header class map for every column (E9 item 5).
3. `core: 1` + discovery-marker + enum-sources + liveness-terminal +
   update-authority key lines + `generated` stamp.
4. `reviewed: <date>` stamps on judgment cells (staleness threshold applies
   to them; fiction between reviews is not machine-preventable, STALE review
   is machine-visible).
5. Column reconciliation to the core contract (`location`, `last_activity`
   present; rename or add -- a schema-tier act behind the tier-1 gate).
6. If retired/frozen STATE rows exist: TOMBSTONE block. Extension vocab like
   role=frozen is NOT core-keyed; tying it to tombstones is an
   instance-extension lint rule.

## Reconcile duties: shipped vs instance-extension

Shipped skeleton (routines/portfolio-reconcile.sh): structural lint,
generated-stamp staleness, E1 existence diff (dir-glob / file-glob /
launchd-prefix), E5 disappearance REPORT, exclusion full print.
Instance-extension (documented here, implemented locally where needed):
E3 declared-state vs ground truth (git/PR/process), E8 misclassification
detector, E2 grandfather burn-down surfacing. Tombstone COMMIT always stays
with the index's designated writer.
