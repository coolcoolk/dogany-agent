# KNOWLEDGE-WIRING -- domain-agent knowledge warehouse wiring standard

Status: Rule (PM-HIERARCHY: violating this document is judgeable as a wiring
bug, not a style choice). The agent-crafting phase 2 checklist procedure that
executes the gates is a Playbook and lives with that skill.
Origin: DGN-402 (locked spec v2.1), reference incident DGN-398.

Audience: pack authors. A domain agent consumes a publisher-curated knowledge
warehouse (e.g. kimwog: registry.yaml domain registry / items/ graded items /
e/ E-records / tools/ deterministic tools) as a release-pinned snapshot copy.
The DGN-398 incident: the snapshot was delivered correctly, but the runtime
had ZERO consumption paths, so the agent honestly answered "I have no domain
knowledge". This standard defines the wiring in three layers so that gap can
never be reproduced by the minting machinery.

Scope limit: ONE warehouse per agent. One manifest `knowledge` object, one
`KNOWLEDGE_WAREHOUSE` key, one pointer block. Multi-warehouse promotion path
(if ever needed): comma-list key, knowledge array, per-warehouse gate loop --
structurally compatible, deliberately not designed here (zero real demand).

## The three layers

### Layer 1 -- delivery (the warehouse exists on disk)

- `<root>/knowledge/<warehouse>/` full tree plus `.snapshot-pin`
  (warehouse / release / snapshot_date / source fields).
- `<root>/scripts/knowledge-snapshot.sh` shipped by the pack (`scripts`
  category; pack_install STEP 2f installs it, STEP 6 runs it). Idempotent
  rsync copy + pin record; instance user data (instance/touched-set.yaml,
  instance/registry.yaml, instance/e/, instance/proposals/,
  GAPS-instance.md) is excluded so a re-delivery never erases live accretion.
- The snapshot source path resolves in two channels (DGN-227 B5):
  (1) a BUNDLED FROZEN SNAPSHOT at `<package_dir>/<reference_slug>/knowledge/
  <warehouse>/` -- pack_install STEP 6 injects this path when present, so a
  pack ships its warehouse to OTHER machines (customer-machine path; this is
  the frozen-snapshot delivery channel);
  (2) absent -> fall back to the manifest `knowledge.source` publisher-local
  path (`~` expanded; empty = script default) -- the same-machine publisher
  pilot/dev scenario.
  Frozen-snapshot shipping for other machines is IN SCOPE via channel (1);
  same-machine source injection remains as the channel (2) fallback.

### Layer 2 -- discovery (the runtime knows the warehouse exists)

Three canonical artifacts:

1. AGENT.md compact pointer block -- shipped in the pack `AGENT.md.add`
   fragment, idempotency marker `KNOWLEDGE-WIRING-POINTER` (skeleton below).
2. `config/agent.conf` key `KNOWLEDGE_WAREHOUSE=<name>` -- shipped in the
   pack `agent.conf.add` fragment. This key is the mechanical single source
   of truth for "this instance has a warehouse".
   AUTHORING CONVENTION: write the value UNQUOTED with NO trailing
   whitespace (e.g. `KNOWLEDGE_WAREHOUSE=kimwog`). The gate predicates pass
   the extracted value straight to `test -d`; quotes or spaces break the
   directory path.
3. The canonical dogany-memory-search SKILL.md conditional warehouse gate
   line (framework-owned, keyed on `KNOWLEDGE_WAREHOUSE`). Pack authors do
   NOT ship this -- it rides framework releases once, for all instances, and
   self-deactivates on warehouse-less instances (no key = memory search
   alone gates). Instance-local edits of framework-owned skills have no
   update durability; do not attempt them.

Artifacts 1 and 2 are pack-injected (absent when the pack ships no
warehouse); artifact 3 is conf-key-conditional. Nothing inserts an
unconditional warehouse reference anywhere -- warehouse-less packs stay
dangling-free by construction.

### Layer 3 -- refraction (outputs actually consume the warehouse)

- Pack domain skills carry a `## knowledge warehouse consumption` block
  (skeleton below) plus the warehouse `tools/refract_cli.py` (delivered by
  layer 1). Refraction is deterministic -- never model-computed numbers.
- Never recite: raw item text, claim text, and grade letters never reach
  user-facing output; only refracted results do.
- The snapshot is READ-ONLY on the consumer side: new knowledge goes to
  `instance/proposals/`, coverage gaps append one line to
  `GAPS-instance.md`.
- Ownership vs framework updates: pack_install STEP 7 registers each
  consumer skill's `.claude/skills-bundle/<skill>/SKILL.md` in
  `<root>/.claude/.dogany-preserve` with a `# pack-owned: <pack-id>` tag, so
  the framework skills-bundle refresh does not clobber it. Pack reinstall
  still overwrites (the preserve list binds update.sh only). On every
  install, pack-owned entries are reconciled against the current manifest
  consumer set (stale ones removed; hand-written entries untouched).

Layer boundary principle: AGENT.md = pointer only (hot-inject cost minimum),
procedures = skill bodies (on-demand), numeric computation = deterministic
tools.

Spine rule: domain lock-spec inviolable constraints (e.g. no prescription
numbers exposed, weight untouchable, ramp guard, calorie floor) always win
over warehouse refraction output. Warehouse = guidance, spine = constraint.

## Manifest schema (the single source of truth)

The pack manifest declares warehouse wiring. The catalog.json `knowledge_ko`
field is display-only prose; the install path never reads it.

```json
"categories": [ ...,
  {"category": "scripts", "required": true},
  {"category": "knowledge_snapshot", "required": true} ],
"knowledge": {
  "warehouse": "kimwog",
  "source": "~/dogany/Metal/kimwog",
  "consumer_skills": { "diet-log":    ["nutrition", "sleep-recovery"],
                       "workout-log": ["exercise", "sleep-recovery"] },
  "turns": [
    {"type": "T1", "home": ".claude/skills-bundle/diet-log/SKILL.md"},
    {"type": "T1", "home": ".claude/skills-bundle/workout-log/SKILL.md"},
    {"type": "T2", "home": "AGENT.md"},
    {"type": "T3", "home": "routines/prompts/weekly-section.md"} ],
  "smoke_item": "exercise/volume-landmarks-heuristic-015",
  "smoke_args": ""
}
```

Preflight rules (mechanical, enforced by pack_install):

1. `knowledge` object XOR `knowledge_snapshot` category = FAIL (no half
   declaration).
2. knowledge declared -> `scripts` category required (STEP 6 runs the script
   STEP 2f installs).
3. `consumer_skills` empty = FAIL; every consumer skill must exist in the
   pack payload `skills/` AND as an instance `.claude/skills-bundle/<id>/`
   directory.
4. `turns` empty = FAIL; type is T1/T2/T3 only; home is an
   instance-root-relative pack artifact path.

`smoke_item`: prefer an item with NO rescale axes (no measured values
needed) -- a fresh mint has zero records, so measured-value items fail the
smoke by construction. `smoke_args` is a fallback for packs that have no
rescale-free item (fixed dummy args).

## Authoring skeletons (authoring-time constants)

Warehouse name, domains and triggers are PACK-AUTHORING-TIME CONSTANTS --
write them as literals. There are NO install-time tokens for warehouse
parameters (the v1 `__WAREHOUSE_NAME__` / `__SKILL_DOMAINS__` /
`__CONSUMPTION_TRIGGER__` tokens are rejected); manifest<->skill drift is
caught by the G4 cross-grep instead. Mint identity tokens
(`__USER_LABEL__` etc.) elsewhere in the skill body are fine -- the STEP 7
render pipeline substitutes them and G4 hard-FAILs any residue.

### AGENT.md compact pointer block (pack AGENT.md.add fragment)

Write the warehouse name literally (must match `knowledge.warehouse`; the
G2 cross-grep enforces it). kimwog example:

```markdown
<!-- KNOWLEDGE-WIRING-POINTER (pack marker; idempotent append) -->
### Knowledge warehouse
Domain knowledge lives at `knowledge/kimwog` (curated graded items +
owner E-records; snapshot pinned per release -- `.snapshot-pin`).
On domain-knowledge questions consult it BEFORE answering: README.md =
layout, registry.yaml = domains, tools/resolve.py = alias lookup, items/ =
graded items, e/ = E-records. Never claim domain knowledge is absent
without checking BOTH memory search AND the warehouse. The snapshot is
READ-ONLY: new knowledge -> instance/proposals/, gaps -> GAPS-instance.md.
```

(Five prose lines -- one over the 2-4 line pointer philosophy, accepted as
the price of preventing DGN-405-class read-only violations.)

### Skill consumption block skeleton (pack domain skills)

Fill the `<angle-bracket>` slots with literals at authoring time:

```markdown
## knowledge warehouse consumption

when <consumption trigger for this skill>, consult the warehouse FIRST:

warehouse root: `knowledge/<warehouse>` (snapshot pinned; check
`.snapshot-pin` for release).
relevant domains: <domains, comma-separated -- must match the manifest
consumer_skills entry> (via registry.yaml; tools/resolve.py for aliases).

procedure:
1. read candidate items from `items/` for the relevant domains.
2. check `e/` for owner E-records on the same domains.
3. refract deterministically:
   `python3 knowledge/<warehouse>/tools/refract_cli.py <item-id> --measured <axis>=<value> --now <YYYY-MM-DD>`
   measured values come from this skill's own record lookup; never
   compute refraction numbers yourself.
4. speak only the refracted result. never recite raw item text, claim
   text, or grade letters in user-facing output -- refracted, not recited.
5. PROVISIONAL / contested items: phrase as this program's working rule,
   never "science says"; always hedge contested items.
6. no applicable item -> skip silently; if the miss is a real coverage
   gap, append one line to knowledge/<warehouse>/GAPS-instance.md.
7. the snapshot is read-only: proposals -> instance/proposals/, never items/.
```

Domain-specific detail (record sources, constants, footnotes) goes on top of
this skeleton as pack prose; the skeleton standardizes structure and guards.

## Turn types (declared in manifest knowledge.turns)

The physical home of a consumption step is pack discretion; the manifest
declares {type, home} so the gate can check it mechanically. In-file
markers: T1 = the `## knowledge warehouse consumption` heading itself;
T2/T3 = one ASCII marker line `KNOWLEDGE-TURN-T2` / `KNOWLEDGE-TURN-T3`
(comment or body, once). T2/T3 marker checks verify DECLARATION only;
actual consumption behavior is verified by the G5 live probes.

- T1 post-log comment turn: fires right after a successful domain record
  verb, fetching the skill's registered domain items + same-domain
  E-records. Output bounded: refracted result only; silent skip when
  nothing applies.
- T2 program-design turn: program card creation, phase-transition
  proposals, program parameter (volume/frequency/deficit) sizing. Fires
  just before proposing parameters (internal sizing step). Fetch bound:
  shortlist via registry.yaml domain index (resolve.py aliases), read at
  most 5 item bodies -- never full-scan. Output bounded: the warehouse
  informs internal parameter choice only; refraction numbers, grades and
  raw text never reach the user.
- T3 periodic review turn: mandatory for packs that declare a periodic
  review turn (weekly section, ladder review); not applicable otherwise.
  Fetches the flagged axis domain items + unresolved GAPS-instance.md
  entries (the review doubles as the gap-promotion observation point).
  Output bounded like T2, coaching-block phrasing.

## Gates G1-G5

| # | check | layer | executor |
|---|-------|-------|----------|
| G1 | snapshot exists + pin parses; release-drift check only when the publisher source dir is on this machine | delivery | mechanical (knowledge_selftest.sh) |
| G2 | AGENT.md pointer marker + pointer names the manifest warehouse | discovery | mechanical (knowledge_selftest.sh) |
| G3 | conf key cross-checked (conf <-> manifest <-> disk) + canonical memory-search line | discovery | mechanical (knowledge_selftest.sh) |
| G4 | per-consumer-skill consumption block + domain cross-grep + unrendered-token residue (hard FAIL) + turn declarations + refract smoke | refraction | mechanical (knowledge_selftest.sh) |
| G5 | 2 live probes with transcript/tool-log verification | all | manual (agent-crafting phase 2) |

G1-G4 run automatically as pack_install STEP 7c (exit != 0 = install FAIL)
and are re-run by the agent-crafting phase 2 checklist with the SAME script
(`scripts/pack/knowledge_selftest.sh <root> --manifest <pack-manifest>`) to
catch post-install drift. Warehouse-less packs run one inverse check
instead: no `KNOWLEDGE_WAREHOUSE` key, no `KNOWLEDGE-WIRING-POINTER`
marker, no `knowledge/` directory. The inverse check runs only on wholly
warehouse-less instances -- no key, or a stale key whose named
`knowledge/<name>/` directory is missing on disk; on an instance with a
verified warehouse from another pack it is skipped with an explicit
SKIPPED line naming that warehouse.

G3 release-order dependency: the canonical memory-search line ships via a
framework release. Minting on an older framework FAILs that check -- this
is not a false positive; it means "update the framework first". The
agent-crafting checklist pins the minimum framework version.

G5 (manual, 2 probes): (P1) one in-domain knowledge question; (P2) one
absence-bait question about knowledge that IS in the warehouse. PASS
requires, per probe, an actual warehouse file access in the session
transcript/tool log during the probe turn AND no raw claim text or grade
letters in the reply. Echoing warehouse-sounding language without a logged
file access is a FALSE PASS. Any "no domain knowledge" reply = FAIL.

## Interim window (ordering constraint)

The canonical dogany-memory-search warehouse gate line must land in a
framework release BEFORE the Warg stopgap protections are retired
(preserve directory entries for diet-log/ and workout-log/, and the local
memory-search gate line re-apply procedure). Until that release reaches an
instance, a framework update backs up and overwrites the instance-local
gate line -- re-apply it from the skills backup channel (recovery, not
preservation). Retiring the stopgap before the canonical release lands
reopens the DGN-398 gap.
