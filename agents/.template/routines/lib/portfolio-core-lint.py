#!/usr/bin/env python3
# portfolio-core-lint.py -- universal portfolio schema lint (core v1).
# Framework asset. Spec of record: docs/PORTFOLIO-CORE.md (canonical repo,
# dogany-agent). Origin: DGN-350 executable gate, graduated unchanged
# (the shipped test suite routines/tests/test-portfolio-core.py is the
# regression net for the move).
#
# Usage:
#   python3 routines/lib/portfolio-core-lint.py <index-path> [label]
#   python3 routines/lib/portfolio-core-lint.py --enum-config <file.json> <index-path> [label]
#   python3 routines/lib/portfolio-core-lint.py --parse-only <index-path>
#   python3 routines/lib/portfolio-core-lint.py --dump-rows <index-path>
#   python3 routines/lib/portfolio-core-lint.py --dump-exclusions <index-path>
# Exit: 0 = PASS, nonzero = FAIL (parse-or-die class). --parse-only speaks the
# PORTFOLIO-PARSE-OK / PORTFOLIO-PARSE-FAIL output contract (structural parse
# subset only, no header semantics; EDGES optional = CORE profile).
# --enum-config: JSON file {"state_vocab": ["...", ...]} -- the D-18
# lint-config-resident closed state vocabulary for declared state columns.
# md and JSON index substrates are both supported (auto-detected by extension).
#
# Rule index (encoding rules; full text in docs/PORTFOLIO-CORE.md):
#   D-0  header = `# <key>: <value>` lines before first block marker; exact
#        kebab-case key match; closed key + block registries
#   D-1  `core: 0` present = die; C=0 only via marker absence (grandfather)
#   D-2  discovery-marker: comma list, parenthetical annotations allowed
#   D-3  exclusion entry `- item | reason | date`, exactly 3 fields, ISO date
#   D-4  liveness-terminal: exactly one surface identifier
#   D-6  update-authority key line required (tier-1 + tier-2 labels);
#        whole-header prose scanning forbidden
#   D-7  accepted-gap legal only with exactly one enum source
#   D-8/9 class-map key; `col:class` comma pairs; unparseable token = die
#   D-10 every index column mapped; no all-three-classes rule
#   D-12 TOMBSTONE block, EXCLUDE-grammar entries, row/block cross-check
#        keyed on the core `state` column ONLY (extension vocab excluded)
#   D-13 tombstone date lives in the block; last_activity doubling forbidden
#   D-14 `generated` ISO-8601; absent = WARN, malformed = die; not at C0
#   D-16 E5 well-formedness runs at C0; block cross-check from core:1
#   D-17 JSON key set mirrors the md contract; unknown top-level key = die
#   D-18 enum vocab lint-config-resident; retired/frozen/- core-reserved
#   D-19 positional separator (content-validated); silent line-drop = die
#   D-20 id/location/last_activity presence enforced; id non-empty
#   post-lock r3: duplicate header key = die; the positionally-consumed
#        separator line must actually BE a separator
# LINT vs RECONCILE boundary (D-15): E3 reconcile loop, E1 multi-source diff,
#   and E4 checker-liveness (staleness thresholds) are RECONCILE duties (see
#   routines/portfolio-reconcile.sh); this lint checks structural/schema
#   conformance only (header contract, class map, tombstone structure, enum
#   domains, core-column presence).
# Python 3 stdlib only.

import re
import sys
import json
import os

# ============================================================
# SPEC constants
# ============================================================

SPEC_CORE_VERSION = 1   # This lint supports core version 1.
SUPPORT_WINDOW = 1      # Min supported version = SPEC_CORE_VERSION - SUPPORT_WINDOW.
                        # C=0 is the grandfather path (absent marker); window covers
                        # >= 1 with full lint.  Older than the window = structural+WARN.
                        # D-1: with S=1, older-window branch is dormant until S>=2.

CORE_MARKER_RE = re.compile(r'^core\s*:\s*(\S+)\s*$')

PROVENANCE_CLASSES = {'derived', 'declared', 'judgment'}

# E9 item 2: update-authority -- requires a key line (D-6)
# The key line must contain both tier-1 and tier-2 labels.
UPDATE_AUTHORITY_TIER1_RE = re.compile(r'tier.?1', re.IGNORECASE)
UPDATE_AUTHORITY_TIER2_RE = re.compile(r'tier.?2', re.IGNORECASE)

# E9 item 4: enum-sources -- min two, or one + "accepted-gap"
ENUM_SOURCES_ACCEPTED_GAP_RE = re.compile(r'\+\s*accepted-gap', re.IGNORECASE)

# ISO date pattern for exclusion/tombstone entries (D-3 / D-12)
# m2 (r3): month 01-12 / day 01-31 range sanity included.
ISO_DATE_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$')

# ISO-8601 timestamp pattern for generated stamp (D-14)
# We accept date-only or datetime with T separator.
# m2 (r3): fully anchored ($) so prefix-match cannot defeat malformed=die;
# month/day range sanity included.
ISO_8601_RE = re.compile(
    r'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])'
    r'(T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?)?$'
)

# Core-reserved state tokens (D-18); lintable without config
CORE_RESERVED_STATES = {'retired', 'frozen', '-'}

# D-19 / grill-r3 F1: a separator cell is dashes with optional leading/trailing
# colons (:--, ---, --:, :-:). The positionally-consumed separator line must
# actually BE a separator -- all cells matching this -- else die.
SEPARATOR_CELL_RE = re.compile(r'^:?-+:?$')

# Block names in the registered block registry (D-0):
# ROWS and EXCLUDE: exactly once. EDGES: exactly once WHEN the edge layer is
# adopted (D-0/E6) -- absent block = edges not adopted = legal (M1 fix, r3).
# TOMBSTONE is conditional (required iff retired/frozen rows exist, D-12).
REGISTERED_BLOCKS = ['PORTFOLIO:EXCLUDE', 'PORTFOLIO:ROWS', 'PORTFOLIO:EDGES', 'PORTFOLIO:TOMBSTONE']

# ============================================================
# Result accumulator
# ============================================================

class LintResult:
    def __init__(self):
        self.errors = []    # parse-or-die class; lint aborts
        self.warnings = []  # WARN class; lint continues
        self.findings = []  # informational findings
        self.died = False
        self.core_version = None  # None = absent (C0 grandfather)
        self.path_mode = None     # 'c0-grandfather', 'full', 'older-window', 'version-ahead'

    def die(self, msg):
        self.errors.append(msg)
        self.died = True

    def warn(self, msg):
        self.warnings.append(msg)

    def find(self, msg):
        self.findings.append(msg)

    def verdict(self):
        if self.died:
            return 'FAIL'
        if self.errors:
            return 'FAIL'
        return 'PASS'

    def report(self, label=''):
        lines = []
        prefix = f'[{label}] ' if label else ''
        lines.append(f'{prefix}VERDICT: {self.verdict()}')
        lines.append(f'{prefix}PATH: {self.path_mode}')
        if self.errors:
            for e in self.errors:
                lines.append(f'{prefix}  ERROR: {e}')
        if self.warnings:
            for w in self.warnings:
                lines.append(f'{prefix}  WARN: {w}')
        if self.findings:
            for f in self.findings:
                lines.append(f'{prefix}  FIND: {f}')
        return '\n'.join(lines)


# ============================================================
# E9 md substrate: marker-pair block parser
# ============================================================

def _find_marker_indices(lines, block_name):
    """Return list of line indices where BEGIN marker appears and END marker appears."""
    begin_tag = f'<!-- {block_name}:BEGIN -->'
    end_tag = f'<!-- {block_name}:END -->'
    begins = [i for i, l in enumerate(lines) if begin_tag in l]
    ends = [i for i, l in enumerate(lines) if end_tag in l]
    return begins, ends


def parse_md_sections(content, lint_config=None):
    """
    Parse block marker pairs per D-0 block registry:
      PORTFOLIO:EXCLUDE, PORTFOLIO:ROWS (required, exactly once),
      PORTFOLIO:EDGES (exactly once WHEN edge layer adopted; absent = legal, M1),
      PORTFOLIO:TOMBSTONE (conditional -- absent is OK iff no retired/frozen rows).
    Returns dict with substrate data.
    parse-or-die: raises ValueError on structural failure (D-0, D-19).

    D-0: each registered block appears at most once; BEGIN before END;
    duplicate or out-of-order = die.
    """
    lines = content.splitlines()

    def require_block_once(block_name):
        """Returns (begin_idx, end_idx); raises ValueError on violation."""
        begins, ends = _find_marker_indices(lines, block_name)
        if len(begins) != 1:
            raise ValueError(
                f'{block_name}:BEGIN must appear exactly once (found {len(begins)})'
            )
        if len(ends) != 1:
            raise ValueError(
                f'{block_name}:END must appear exactly once (found {len(ends)})'
            )
        b, e = begins[0], ends[0]
        if b >= e:
            raise ValueError(
                f'{block_name}: BEGIN (line {b}) must precede END (line {e})'
            )
        return b, e

    def optional_block(block_name):
        """Returns (begin_idx, end_idx) or None if block is absent.
        Raises ValueError on duplicate or order violation if present."""
        begins, ends = _find_marker_indices(lines, block_name)
        if len(begins) == 0 and len(ends) == 0:
            return None
        if len(begins) != 1:
            raise ValueError(
                f'{block_name}:BEGIN must appear at most once (found {len(begins)})'
            )
        if len(ends) != 1:
            raise ValueError(
                f'{block_name}:END must appear at most once (found {len(ends)})'
            )
        b, e = begins[0], ends[0]
        if b >= e:
            raise ValueError(
                f'{block_name}: BEGIN (line {b}) must precede END (line {e})'
            )
        return b, e

    # Required blocks: ROWS, EXCLUDE (D-0: exactly once).
    # EDGES: exactly once WHEN the edge layer is adopted (D-0/E6/C.3) --
    # optional layer, off by default; absent = edges not adopted = legal.
    # Duplicate or out-of-order EDGES markers still die (optional_block).
    rb, re_ = require_block_once('PORTFOLIO:ROWS')
    xb, xe  = require_block_once('PORTFOLIO:EXCLUDE')
    edges_range = optional_block('PORTFOLIO:EDGES')

    # Block ordering sanity: ROWS before EDGES; EXCLUDE before ROWS is typical
    # Spec does not mandate relative order of EXCLUDE vs ROWS/EDGES, only
    # that each block's BEGIN precedes its END (already verified above).

    # TOMBSTONE: optional at parse time; required if retired/frozen rows exist
    tomb_range = optional_block('PORTFOLIO:TOMBSTONE')

    # header_text = all lines before the first section marker
    # (first of EXCLUDE:BEGIN, ROWS:BEGIN, EDGES:BEGIN, TOMBSTONE:BEGIN)
    first_marker = xb
    # Also check if tombstone comes before rows/exclude
    if tomb_range is not None:
        first_marker = min(first_marker, tomb_range[0])
    if edges_range is not None:
        first_marker = min(first_marker, edges_range[0])
    first_marker = min(first_marker, rb)
    header_text = '\n'.join(lines[:first_marker])

    # Parse exclude block lines (between BEGIN and END, exclusive)
    exclude_lines = lines[xb+1:xe]

    # Parse tombstone block lines
    tombstone_lines = None
    if tomb_range is not None:
        tombstone_lines = lines[tomb_range[0]+1:tomb_range[1]]

    # D-19: Positional separator rule + no-silent-drop.
    # Parse a table section: the separator row is ONLY the one immediately
    # following the header row (position-based, not content-based).
    # Any other all-dash-cells line is DATA.
    # Any non-blank, non-pipe, non-marker line inside a section = die.
    def parse_table(section_lines, section_name):
        """
        Returns (header_cols_list, data_rows_list).
        Raises ValueError on structural violation (D-19).
        """
        header_cols = None
        header_count = None
        separator_consumed = False
        data_rows = []

        for lineno, line in enumerate(section_lines):
            stripped = line.strip()

            # Skip blank lines
            if not stripped:
                continue

            # Die on HTML comment lines inside section (they should not appear here)
            if stripped.startswith('<!--'):
                # This is a begin/end marker leaking in -- structural error
                raise ValueError(
                    f'{section_name} line {lineno+1}: unexpected marker inside section: {stripped!r}'
                )

            # Every non-blank line must be a pipe-delimited table line
            if '|' not in stripped:
                raise ValueError(
                    f'{section_name} line {lineno+1}: non-table line inside section '
                    f'(unconsumed/undropped) -- D-19: {stripped!r}'
                )

            parts = [c.strip() for c in stripped.strip('|').split('|')]

            if header_cols is None:
                # First line is the header row
                header_cols = parts
                header_count = len(parts)
                continue

            if not separator_consumed:
                # D-19: the one line immediately following the header is positionally
                # designated as THE separator. Grill-r3 F1: it must actually BE a
                # separator (every cell all dashes with optional colons) -- otherwise
                # a missing separator would silently eat the first data row. Die.
                if not all(SEPARATOR_CELL_RE.match(c) for c in parts):
                    raise ValueError(
                        f'{section_name} line {lineno+1}: line after header row is not '
                        f'a separator row (all dash/colon cells required) -- '
                        f'D-19/F1 missing or malformed separator: {stripped!r}'
                    )
                separator_consumed = True
                continue

            # All subsequent lines are data rows.
            if len(parts) != header_count:
                raise ValueError(
                    f'{section_name} line {lineno+1}: {len(parts)} columns, '
                    f'expected {header_count} (header: {header_cols})'
                )
            data_rows.append(dict(zip(header_cols, parts)))

        return header_cols, data_rows

    rows_section = lines[rb+1:re_]
    rows_header, rows_data = parse_table(rows_section, 'ROWS')

    # EDGES: parsed only when the layer is adopted (block present, M1)
    edges_header, edges_data = None, []
    if edges_range is not None:
        edges_section = lines[edges_range[0]+1:edges_range[1]]
        edges_header, edges_data = parse_table(edges_section, 'EDGES')

    return {
        'header_text':    header_text,
        'exclude_lines':  exclude_lines,
        'tombstone_lines': tombstone_lines,
        'rows_header':    rows_header,
        'rows_data':      rows_data,
        'edges_header':   edges_header,
        'edges_data':     edges_data,
    }


# ============================================================
# E9 header contract parser
# ============================================================

def _extract_key_line(header_text, exact_key):
    """
    D-0: Keys are exact kebab-case tokens matched at line start (after # strip).
    Loose substring matching is FORBIDDEN.
    Returns the value string after the colon, or None if the key is absent.
    m4 (r3, post-lock clarification): duplicate occurrences of the same header
    key = die (parse-or-die; silent first-wins forbidden) -- raises ValueError.
    """
    values = []
    for line in header_text.splitlines():
        stripped = line.strip()
        # Strip leading # comment markers
        stripped = re.sub(r'^#+\s*', '', stripped)
        # Check for exact key match: key must be at start, followed by ':'
        if stripped.lower().startswith(exact_key.lower() + ':'):
            values.append(stripped[len(exact_key)+1:].strip())
    if len(values) > 1:
        raise ValueError(
            f'duplicate header key "{exact_key}" ({len(values)} occurrences) -- '
            f'parse-or-die, first-wins forbidden (m4 post-lock clarification)'
        )
    if values:
        return values[0]
    return None


def _extract_key_or_die(header_text, exact_key, result):
    """Wrapper: converts duplicate-key ValueError into result.die.
    Returns the value (or None if absent OR died -- check result.died)."""
    try:
        return _extract_key_line(header_text, exact_key)
    except ValueError as e:
        result.die(f'E9/D-0 malformed: {e}')
        return None


def parse_header_contract(header_text, result, lint_version=SPEC_CORE_VERSION):
    """
    Check E9 header contract items 1-7 per D-0 through D-9.
    Populates result with core_version and appropriate errors/warnings.
    Returns dict of found items (may be partial if died).
    lint_version = S, the lint-supported version (E0: instance-local lints may
    pin a local version; parameterized here so the E0 consumer matrix branches
    are testable by simulation -- see m3 tests).
    """
    found = {}

    # --- Item 1: core: <N> ---
    # Absent = C0 grandfather (not an error per E0).
    # Present but value=0 or non-integer = malformed = die.
    # Present value must be positive integer (D-1: core: 0 present = die).
    core_value = _extract_key_or_die(header_text, 'core', result)
    if result.died:
        return found
    if core_value is None:
        result.core_version = 0
        result.find('core: marker absent -- C0 grandfather path')
    else:
        try:
            v = int(core_value)
            if v <= 0:
                result.die(f'E9/E0 item-1 malformed: core value must be positive integer, got "{core_value}"')
                return found
            result.core_version = v
            found['core'] = v
        except ValueError:
            result.die(f'E9/E0 item-1 malformed: core value not parseable as integer: "{core_value}"')
            return found

    # Determine path mode
    S = lint_version
    C = result.core_version if result.core_version is not None else 0

    if C == 0:
        result.path_mode = 'c0-grandfather'
    elif C > S:
        result.path_mode = 'version-ahead'
        result.die(f'CORE-VERSION-AHEAD: index version {C} > lint supported version {S}; consumer must take no-ledger fallback path')
        return found
    elif C == S:
        result.path_mode = 'full'
    else:
        # C < S (dormant at S=1: C is a positive integer, so C < 1 is impossible;
        # reachable only when S >= 2 -- exercised by the m3 simulation tests)
        min_window = S - SUPPORT_WINDOW
        if C >= min_window:
            # Within the support window: apply version-C PUBLISHED rules (E0).
            # D-1: applying current-version rules relabeled as "version-C rules"
            # is FORBIDDEN. The only published ruleset this lint implements is
            # core-1 (SPEC_CORE_VERSION); for C == that version its published
            # rules ARE these full checks, so we proceed. For any other C we
            # have no published ruleset -- fail closed rather than relabel.
            result.path_mode = 'older-window'
            result.warn(f'Index conforms to older core {C}; lint applies version-{C} published rules')
            if C != SPEC_CORE_VERSION:
                result.die(f'D-1: version-{C} published rules not implemented by this lint; applying current rules relabeled is forbidden -- fail closed')
                return found
        else:
            # Older than the support window: structural checks only + WARN (E0).
            # m3 (r3): distinct path mode so lint_md takes the structural-only
            # branch -- no fall-through to full checks / E8 die.
            result.path_mode = 'older-structural'
            result.warn(f'Index core version {C} older than support window (>= {min_window}); structural checks only + WARN "conforms to older core {C}"')
            return found

    # C0 grandfather: structural checks only
    if result.path_mode == 'c0-grandfather':
        result.warn('C0 grandfather mode: structural checks only; header contract items 2-7 NOT checked')
        return found

    if result.path_mode == 'version-ahead':
        return found

    # Full path: check items 2-7

    # --- Item 2: update-authority (D-6) ---
    # Required key line; value must contain tier-1 and tier-2 labels.
    # Whole-header prose scanning FORBIDDEN (D-6).
    ua_value = _extract_key_or_die(header_text, 'update-authority', result)
    if result.died:
        return found
    if ua_value is None:
        result.die('E9 item-2: update-authority key line absent (required; prose scan not a substitute -- D-6)')
        return found
    if not UPDATE_AUTHORITY_TIER1_RE.search(ua_value):
        result.die('E9 item-2 malformed: update-authority line missing tier-1 label')
        return found
    if not UPDATE_AUTHORITY_TIER2_RE.search(ua_value):
        result.die('E9 item-2 malformed: update-authority line missing tier-2 label')
        return found
    found['update_authority'] = ua_value

    # --- Item 3: discovery-marker (D-2) ---
    # Exact key: discovery-marker; value: one or more marker identifiers,
    # comma-separated; a parenthetical annotation may follow each identifier.
    # m1 (r3): split the identifiers; at least one non-empty identifier
    # (after annotation strip) required, else malformed = die (",,," dies).
    dm_value = _extract_key_or_die(header_text, 'discovery-marker', result)
    if result.died:
        return found
    if dm_value is None:
        result.die('E9 item-3: discovery-marker key line absent (required)')
        return found
    dm_idents = [
        re.sub(r'\(.*?\)', '', tok).strip()
        for tok in dm_value.split(',')
    ]
    dm_idents = [i for i in dm_idents if i]
    if not dm_idents:
        result.die('E9 item-3 malformed: discovery-marker present but no non-empty identifiers listed (D-2)')
        return found
    found['discovery_markers'] = dm_idents

    # --- Item 4: enum-sources ---
    # Exact key: enum-sources
    es_value = _extract_key_or_die(header_text, 'enum-sources', result)
    if result.died:
        return found
    if es_value is None:
        result.die('E9 item-4: enum-sources key line absent (required)')
        return found
    if not es_value.strip():
        result.die('E9 item-4 malformed: enum-sources key present but empty')
        return found
    has_gap = bool(ENUM_SOURCES_ACCEPTED_GAP_RE.search(es_value))
    src_text = ENUM_SOURCES_ACCEPTED_GAP_RE.sub('', es_value)
    sources = [s.strip() for s in re.split(r'[,;]', src_text) if s.strip()]
    if len(sources) < 1:
        result.die('E9 item-4 malformed: no sources listed in enum-sources')
        return found
    if len(sources) < 2 and not has_gap:
        result.die('E9 item-4 malformed: fewer than two enum-sources without accepted-gap token')
        return found
    if len(sources) >= 2 and has_gap:
        result.die('E9 item-4 malformed: accepted-gap token present with two or more sources (D-7: token only legal with exactly one source)')
        return found
    found['enum_sources'] = sources

    # --- Item 5: class-map (D-8, D-9) ---
    # Exact key: class-map; aliases dropped.
    # Serialization: col:class pairs comma-separated; = form dropped; unparseable = die.
    cm_value = _extract_key_or_die(header_text, 'class-map', result)
    if result.died:
        return found
    if cm_value is None:
        result.die('E9 item-5: class-map key line absent (required)')
        return found
    classmap = {}
    for pair in re.split(r',', cm_value):
        pair = pair.strip()
        if not pair:
            continue
        # D-9: only col:class (colon form); = form dropped
        m = re.match(r'^(\S+):(\S+)$', pair)
        if not m:
            result.die(f'E9 item-5 malformed: class-map token not parseable as col:class: "{pair}" (D-9: unparseable pair = die)')
            return found
        col_name, cls = m.group(1), m.group(2)
        if cls not in PROVENANCE_CLASSES:
            result.die(f'E9 item-5 malformed: unknown provenance class "{cls}" for column "{col_name}"')
            return found
        classmap[col_name] = cls
    if not classmap:
        result.die('E9 item-5 malformed: class-map present but no valid col:class pairs found')
        return found
    found['class_map'] = classmap

    # --- Item 6: exclusion list -- deferred, checked in check_exclusion_list() ---
    found['exclusion_list_deferred'] = True

    # --- Item 7: liveness-terminal (D-4) ---
    # Exact key: liveness-terminal; exactly ONE identifier (comma/semicolon = malformed).
    lt_value = _extract_key_or_die(header_text, 'liveness-terminal', result)
    if result.died:
        return found
    if lt_value is None:
        result.die('E9 item-7: liveness-terminal key line absent (required)')
        return found
    lt_value = lt_value.strip()
    if not lt_value:
        result.die('E9 item-7 malformed: liveness-terminal key present but no surface identifier listed')
        return found
    # Single token: comma or semicolon means multiple surfaces = malformed (D-4)
    if re.search(r'[,;]', lt_value):
        result.die(f'E9 item-7 malformed: liveness-terminal must be exactly one identifier; comma/semicolon list forbidden (D-4): "{lt_value}"')
        return found
    found['liveness_terminal'] = lt_value

    return found


# ============================================================
# E9 item-6: Exclusion list check (D-3)
# ============================================================

def check_exclusion_list(exclude_lines, result, path_mode):
    """
    D-3: each entry exactly `- <item> | <reason> | <date>`.
    Exactly 3 pipe-separated fields; ISO date; non-dash line inside block = die.
    Empty list is legal (zero entries). Block absence is handled at parse level.
    Returns list of parsed exclusions, or None if died.
    """
    exclusions = []
    for line in exclude_lines:
        raw = line
        line = line.strip()
        if not line:
            continue
        # Comment lines (from markers leaking -- should not happen after parse)
        if line.startswith('<!--'):
            continue
        # D-3: non-dash line (not starting with '-') inside block = die
        if not line.startswith('-'):
            result.die(f'E9 item-6 malformed: non-dash line inside EXCLUDE block -- D-3: "{raw.rstrip()}"')
            return None
        entry = line[1:].strip()
        parts = [p.strip() for p in entry.split('|')]
        # Exactly 3 fields required (D-3)
        if len(parts) != 3:
            result.die(f'E9 item-6 malformed: exclusion entry must have exactly 3 pipe-separated fields (got {len(parts)}): "{line}"')
            return None
        item_name, reason, date = parts[0], parts[1], parts[2]
        if not item_name:
            result.die(f'E9 item-6 malformed: exclusion entry has empty item name: "{line}"')
            return None
        if not reason:
            result.die(f'E9 item-6 malformed: exclusion entry has empty reason: "{line}"')
            return None
        if not date:
            result.die(f'E9 item-6 malformed: exclusion entry has empty date: "{line}"')
            return None
        # ISO date validation (D-3)
        if not ISO_DATE_RE.match(date):
            result.die(f'E9 item-6 malformed: exclusion entry date not ISO YYYY-MM-DD: "{date}" in "{line}"')
            return None
        exclusions.append({'item': item_name, 'reason': reason, 'date': date})
        result.find(f'EXCLUSION: {item_name} | {reason} | {date}')
    result.find(f'E9 item-6: exclusion list OK ({len(exclusions)} entries)')
    return exclusions


# ============================================================
# E8 class-map check
# ============================================================

def check_class_map(rows_header, classmap, result):
    """
    E8: every column in the index header row must appear in the class map.
    """
    if rows_header is None:
        return
    missing = [col for col in rows_header if col not in classmap]
    if missing:
        result.die(f'E8: columns in index not in class map: {missing}')
        return

    for col, cls in classmap.items():
        if cls not in PROVENANCE_CLASSES:
            result.die(f'E8: unknown provenance class "{cls}" for column "{col}"')
            return

    result.find(f'E8: class-map check passed; {len(rows_header)} columns all mapped')
    result.find(f'E8: misclassification detector: existence-enumeration check is a reconcile-pass duty (D-11) -- skipped in lint')


# ============================================================
# D-14: generated stamp check
# ============================================================

def check_generated_stamp(header_text, result):
    """
    D-14: key 'generated'; absent = WARN; malformed (present but unparseable) = die.
    Not checked at C0 (called only on full/older-window paths).
    """
    gen_value = _extract_key_or_die(header_text, 'generated', result)
    if result.died:
        return
    if gen_value is None:
        result.warn('E4/D-14: generated stamp absent -- WARN (not die per D-14; E4 mandate)')
        return
    gen_value = gen_value.strip()
    if not gen_value or not ISO_8601_RE.match(gen_value):
        result.die(f'E4/D-14 malformed: generated stamp present but not parseable as ISO-8601: "{gen_value}"')
        return
    result.find(f'E4/D-14: generated stamp OK: {gen_value}')


# ============================================================
# D-18: State enum check (lint-config-resident)
# ============================================================

def check_state_enum(rows_data, rows_header, result, lint_config=None):
    """
    D-18: enum vocabularies are lint-config-resident.
    Core-reserved tokens: retired, frozen, - (lintable without config).
    Any other value in 'state' when enum config absent = WARN "undeclared state vocabulary".
    When config present: closed-enum check; non-vocab value = die.
    """
    if rows_header is None or 'state' not in rows_header:
        return

    state_vocab = None
    if lint_config is not None:
        state_vocab = lint_config.get('state_vocab')

    for row in rows_data:
        state_val = str(row.get('state', '-')).strip()
        if state_val in CORE_RESERVED_STATES:
            continue
        if state_vocab is not None:
            if state_val not in state_vocab:
                result.die(f'D-18: state value "{state_val}" not in declared state vocabulary {state_vocab}')
                return
        else:
            result.warn(f'D-18: state value "{state_val}" is not a core-reserved token (retired/frozen/-) and no enum config provided -- undeclared state vocabulary')


# ============================================================
# E5 / D-12 / D-13: Tombstone block parser + row/block cross-check
# ============================================================

def _parse_tombstone_block(tombstone_lines, result):
    """
    D-12 tombstone block grammar:
      - <id> | <date> | <reason or ->
    Exactly 3 pipe-separated fields; id non-empty; date ISO.
    Non-dash line inside block = die.
    Duplicate ids = die.
    Returns list of {id, date, reason} or None if died.
    """
    entries = []
    seen_ids = {}
    for line in tombstone_lines:
        raw = line
        line = line.strip()
        if not line:
            continue
        if line.startswith('<!--'):
            continue
        if not line.startswith('-'):
            result.die(f'E5/D-12 malformed: non-dash line inside TOMBSTONE block: "{raw.rstrip()}"')
            return None
        entry = line[1:].strip()
        parts = [p.strip() for p in entry.split('|')]
        if len(parts) != 3:
            result.die(f'E5/D-12 malformed: tombstone entry must have exactly 3 pipe-separated fields (got {len(parts)}): "{line}"')
            return None
        tid, tdate, treason = parts[0], parts[1], parts[2]
        if not tid:
            result.die(f'E5/D-12 malformed: tombstone entry has empty id: "{line}"')
            return None
        if not tdate or not ISO_DATE_RE.match(tdate):
            result.die(f'E5/D-12 malformed: tombstone entry date not ISO YYYY-MM-DD: "{tdate}" in "{line}"')
            return None
        # reason: '-' is legal (optional per D-12)
        if tid in seen_ids:
            result.die(f'E5/D-12: duplicate tombstone id "{tid}" -- die')
            return None
        seen_ids[tid] = True
        entries.append({'id': tid, 'date': tdate, 'reason': treason})
    return entries


def check_tombstones(rows_data, rows_header, tombstone_lines, result, path_mode):
    """
    D-12 / D-13 tombstone checks:
    - Parse the TOMBSTONE block (D-12 grammar).
    - Cross-check: every row with state=retired/frozen MUST have a TOMBSTONE entry
      with matching id (from core:1; block cross-check starts at core:1 per D-16).
    - Block entry without matching row = legal (D-12: post-regeneration trace).
    - E5/D-16: tombstone well-formedness structural check runs at C0 too
      (id-non-empty check on retired/frozen rows).
    - D-12: role=frozen is extension-vocab (C.3 namespace rule); no hardcoded
      core meaning; the core lint keys on state column only.
    """
    if rows_header is None:
        return

    has_state = 'state' in rows_header
    has_id = 'id' in rows_header

    # Find rows that are retired/frozen by state column
    tombstone_state_rows = []
    if has_state and has_id:
        for row in rows_data:
            state_val = row.get('state', '-').strip()
            if state_val in ('retired', 'frozen'):
                id_val = row.get('id', '').strip()
                if not id_val or id_val == '-':
                    result.die(f'E5/D-16: tombstone row (state={state_val}) missing mandatory id field (id must be non-empty)')
                    return
                tombstone_state_rows.append(id_val)

    # C0 grandfather / older-than-window: only the id-non-empty check above
    # (structural checks only per E0; the same phrase governs both paths --
    # D-16 scopes C0 to the id check; the older-structural path mirrors it).
    if path_mode in ('c0-grandfather', 'older-structural'):
        return

    # Parse the tombstone block
    if tombstone_lines is None:
        # Block absent -- check if required
        if tombstone_state_rows:
            result.die(
                f'E5/D-12: PORTFOLIO:TOMBSTONE block absent but {len(tombstone_state_rows)} '
                f'retired/frozen row(s) exist (ids: {tombstone_state_rows}); block required'
            )
        else:
            result.find('E5/D-12: TOMBSTONE block absent -- OK (no retired/frozen rows)')
        return

    # Parse tombstone block entries
    tomb_entries = _parse_tombstone_block(tombstone_lines, result)
    if tomb_entries is None:
        return  # died in parse

    result.find(f'E5/D-12: TOMBSTONE block parsed OK ({len(tomb_entries)} entries)')

    # Cross-check: every retired/frozen row must have a tombstone entry
    tomb_ids = {e['id'] for e in tomb_entries}
    for row_id in tombstone_state_rows:
        if row_id not in tomb_ids:
            result.die(f'E5/D-12: retired/frozen row "{row_id}" has no matching TOMBSTONE entry')
            return

    # Block entry without matching row = legal (D-12)
    for entry in tomb_entries:
        if entry['id'] not in {row.get('id', '').strip() for row in rows_data if has_id}:
            result.find(f'E5/D-12: TOMBSTONE entry "{entry["id"]}" has no matching active row -- legal (D-12: post-regeneration trace)')


# ============================================================
# D-20: Core-column presence check
# ============================================================

def check_core_columns(rows_data, rows_header, result, path_mode):
    """
    D-20: at core:1, columns id/location/last_activity MUST exist; state optional.
    id must be non-empty (not '-') on every row.
    Skipped at C0 (structural checks at C0 don't include D-20 core-column mandate).
    """
    if path_mode == 'c0-grandfather':
        return
    if rows_header is None:
        return

    required_cols = ['id', 'location', 'last_activity']
    for col in required_cols:
        if col not in rows_header:
            result.die(f'D-20: required core column "{col}" absent from index header (id/location/last_activity required at core:1)')
            return

    # id must be non-empty on every row
    for i, row in enumerate(rows_data):
        id_val = row.get('id', '').strip()
        if not id_val or id_val == '-':
            result.die(f'D-20: row {i+1} has empty id (id is the stable grep key; must be non-empty)')
            return

    result.find(f'D-20: core-column presence OK (id/location/last_activity present; all id cells non-empty)')


# ============================================================
# Main lint entry points
# ============================================================

def lint_md(path, label='', lint_version=SPEC_CORE_VERSION, lint_config=None):
    """
    Full md lint entry point. parse-or-die on structural failure.
    lint_config: optional dict with keys:
      'state_vocab': list of allowed state values (D-18 closed enum; if absent, WARN only)
    Returns LintResult.
    """
    result = LintResult()

    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError as e:
        result.die(f'Cannot read file: {e}')
        return result

    # Substrate parse (parse-or-die)
    try:
        parsed = parse_md_sections(content, lint_config=lint_config)
    except ValueError as e:
        result.die(f'Substrate parse failure: {e}')
        return result

    result.find(f'Substrate parse OK: {len(parsed["rows_data"])} rows, {len(parsed["edges_data"])} edges')

    # E9 header contract
    contract = parse_header_contract(parsed['header_text'], result, lint_version=lint_version)
    if result.died:
        return result

    path_mode = result.path_mode

    # C0 grandfather / older-than-window: structural checks only (E0).
    # m3 (r3): older-structural takes this branch too -- previously it fell
    # through to the full checks and died at E8 (class map unavailable).
    if path_mode in ('c0-grandfather', 'older-structural'):
        result.find(f'{path_mode}: structural checks complete')
        # E5 tombstone well-formedness (D-16: structural checks include
        # id-non-empty on retired rows)
        check_tombstones(parsed['rows_data'], parsed['rows_header'], parsed['tombstone_lines'], result, path_mode)
        # D-20: skipped (core-column mandate is a version-1 semantic rule,
        # not a structural check)
        return result

    if path_mode == 'version-ahead':
        return result

    # Full path (or older-window applying version-C published rules):

    # E9 item-6: Exclusion list (EXCLUDE block must have been found at parse level)
    exclusions = check_exclusion_list(parsed['exclude_lines'], result, path_mode)
    if result.died:
        return result

    # D-14: generated stamp
    check_generated_stamp(parsed['header_text'], result)
    if result.died:
        return result

    # E8 class map check
    if 'class_map' in contract:
        check_class_map(parsed['rows_header'], contract['class_map'], result)
        if result.died:
            return result
    else:
        result.die('E8: class map not available (E9 item-5 failed)')
        return result

    # E5 / D-12 / D-13: Tombstone block + cross-check
    check_tombstones(parsed['rows_data'], parsed['rows_header'], parsed['tombstone_lines'], result, path_mode)
    if result.died:
        return result

    # D-18: state enum check (config-resident)
    check_state_enum(parsed['rows_data'], parsed['rows_header'], result, lint_config=lint_config)
    if result.died:
        return result

    # D-20: core-column presence
    check_core_columns(parsed['rows_data'], parsed['rows_header'], result, path_mode)
    if result.died:
        return result

    return result


# ============================================================
# JSON lint -- D-17 full contract mirror
# ============================================================

# Required JSON contract keys (D-17); core_version must be first.
# 'generated' is a KNOWN key but its lint behavior follows D-14 (E4 mandate,
# not one of the E9 7 die-items): absent = WARN, malformed = die -- mirroring
# the md path (M2 r3 fix: semantic parity, not key-presence-only).
_JSON_REQUIRED_KEYS = [
    'core_version', 'update_authority', 'discovery_markers',
    'enum_sources', 'class_map', 'exclusion_list', 'liveness_terminal',
]
# Required data keys
_JSON_DATA_KEYS = ['rows']
# Optional keys (edges: required only when the edge layer is adopted -- absent = legal)
_JSON_OPTIONAL_KEYS = ['generated', 'tombstones', 'edges', 'carry_forward_store']
# All known top-level keys (unknown = die per D-17)
_JSON_KNOWN_KEYS = set(_JSON_REQUIRED_KEYS) | set(_JSON_DATA_KEYS) | set(_JSON_OPTIONAL_KEYS)


def _check_json_e5_structural(data, result):
    """
    E5/D-16 structural check, JSON mirror: id non-empty on retired/frozen rows.
    Runs on ALL paths including C0 grandfather (structural checks include this).
    """
    rows = data.get('rows', [])
    if not isinstance(rows, list):
        return []
    retired_ids = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        state_val = str(row.get('state', '-')).strip()
        if state_val in ('retired', 'frozen'):
            id_val = str(row.get('id', '')).strip()
            if not id_val or id_val == '-':
                result.die(f'E5/D-16: JSON rows[{i}] (state={state_val}) missing mandatory id field (id must be non-empty)')
                return retired_ids
            retired_ids.append(id_val)
    return retired_ids


def _check_json_tombstones(data, retired_ids, result):
    """
    D-12 cross-check, JSON mirror (M2 r3):
    - tombstones absent + retired/frozen rows exist = die (block required).
    - Entry grammar: {id, date, reason?}; id non-empty; date ISO; unknown key = die.
    - Duplicate ids = die. Entry without matching row = legal (post-regen trace).
    - Every retired/frozen row must have a matching tombstone entry.
    """
    tombs = data.get('tombstones', None)
    if tombs is None:
        if retired_ids:
            result.die(
                f'E5/D-12: JSON "tombstones" absent but {len(retired_ids)} '
                f'retired/frozen row(s) exist (ids: {retired_ids}); tombstones required'
            )
        else:
            result.find('E5/D-12: JSON tombstones absent -- OK (no retired/frozen rows)')
        return
    if not isinstance(tombs, list):
        result.die('E5/D-12: JSON tombstones must be an array')
        return
    seen_ids = set()
    for i, entry in enumerate(tombs):
        if not isinstance(entry, dict):
            result.die(f'E5/D-12: JSON tombstones[{i}] must be an object')
            return
        unknown = [k for k in entry.keys() if k not in ('id', 'date', 'reason')]
        if unknown:
            result.die(f'E5/D-12: JSON tombstones[{i}] unknown field(s) {unknown} (grammar: id, date, reason?)')
            return
        tid = str(entry.get('id', '')).strip()
        if not tid or tid == '-':
            result.die(f'E5/D-12: JSON tombstones[{i}] has empty id')
            return
        tdate = str(entry.get('date', '')).strip()
        if not ISO_DATE_RE.match(tdate):
            result.die(f'E5/D-12: JSON tombstones[{i}] date not ISO YYYY-MM-DD: "{tdate}"')
            return
        if tid in seen_ids:
            result.die(f'E5/D-12: duplicate JSON tombstone id "{tid}" -- die')
            return
        seen_ids.add(tid)
    result.find(f'E5/D-12: JSON tombstones parsed OK ({len(tombs)} entries)')
    for rid in retired_ids:
        if rid not in seen_ids:
            result.die(f'E5/D-12: retired/frozen row "{rid}" has no matching JSON tombstone entry')
            return


def lint_json(path, label='', lint_version=SPEC_CORE_VERSION, lint_config=None):
    """
    JSON substrate lint: D-17 contract + E9 structure-equivalence (M2 r3):
    same closed enums and the same D-3/D-12/D-14/D-18/D-20 checks as the md
    path -- mirrored, no new semantics.
    E0 grandfather (M3 r3): core_version marker ABSENT = C0, structural checks
    only + WARN (same as md); marker PRESENT but malformed still dies.
    Returns LintResult.
    """
    result = LintResult()

    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
    except OSError as e:
        result.die(f'Cannot read file: {e}')
        return result

    # JSON parse failure = die
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        result.die(f'JSON parse failure (parse-or-die): {e}')
        return result

    if not isinstance(data, dict):
        result.die('JSON parse-or-die: top-level value must be an object')
        return result

    keys = list(data.keys())

    # E0 / M3 (r3): core_version marker ABSENT = C0 grandfather -- structural
    # checks only + WARN, exactly as the md path. Absence is meaningful, not
    # an error; D-17's key-absent=die applies to the core:1 full path.
    if 'core_version' not in data:
        result.core_version = 0
        result.path_mode = 'c0-grandfather'
        result.find('core_version marker absent -- C0 grandfather path (E0)')
        result.warn('C0 grandfather mode: structural checks only; JSON contract keys NOT checked')
        _check_json_e5_structural(data, result)
        return result

    # Marker PRESENT: must be the FIRST field (D-17, order-significant)
    if keys[0] != 'core_version':
        result.die(f'JSON parse-or-die: first field must be "core_version" (found: {keys[:3]})')
        return result

    # Marker present but malformed = die (E0 M2a; never a silent C0 fallback)
    core_val = data['core_version']
    try:
        C = int(core_val)
        if C <= 0:
            result.die(f'JSON parse-or-die: core_version must be positive integer, got {core_val!r}')
            return result
    except (TypeError, ValueError):
        result.die(f'JSON parse-or-die: core_version not parseable as integer: {core_val!r}')
        return result

    result.core_version = C
    S = lint_version
    if C > S:
        result.path_mode = 'version-ahead'
        result.die(f'CORE-VERSION-AHEAD: JSON index version {C} > lint supported version {S}')
        return result
    elif C == S:
        result.path_mode = 'full'
    else:
        # C < S: same window semantics as md (dormant at S=1)
        min_window = S - SUPPORT_WINDOW
        if C >= min_window:
            result.path_mode = 'older-window'
            result.warn(f'JSON index conforms to older core {C}; lint applies version-{C} published rules')
            if C != SPEC_CORE_VERSION:
                result.die(f'D-1: version-{C} published rules not implemented by this lint; applying current rules relabeled is forbidden -- fail closed')
                return result
        else:
            result.path_mode = 'older-structural'
            result.warn(f'JSON index core_version {C} older than support window (>= {min_window}); structural checks only + WARN')
            _check_json_e5_structural(data, result)
            return result

    # ---- Full path (or older-window applying version-C published rules) ----

    # Unknown top-level key = die (D-17)
    for k in keys:
        if k not in _JSON_KNOWN_KEYS:
            result.die(f'JSON parse-or-die: unknown top-level key "{k}" (D-17)')
            return result

    # Required contract keys absent = die (D-17)
    for req_key in _JSON_REQUIRED_KEYS:
        if req_key not in data:
            result.die(f'JSON parse-or-die: required contract key "{req_key}" absent (D-17)')
            return result

    # Required data key: rows
    for dk in _JSON_DATA_KEYS:
        if dk not in data:
            result.die(f'JSON parse-or-die: required data key "{dk}" absent (D-17)')
            return result

    # D-14 mirror (M2 r3): generated absent = WARN; present but malformed = die.
    if 'generated' not in data:
        result.warn('E4/D-14: generated stamp absent -- WARN (not die per D-14; E4 mandate)')
    else:
        gen_value = data['generated']
        if not isinstance(gen_value, str) or not ISO_8601_RE.match(gen_value.strip()):
            result.die(f'E4/D-14 malformed: JSON generated stamp present but not parseable as ISO-8601: {gen_value!r}')
            return result
        result.find(f'E4/D-14: generated stamp OK: {gen_value}')

    # update_authority: must be object with tier_1 and tier_2 strings
    ua = data.get('update_authority', {})
    if not isinstance(ua, dict) or 'tier_1' not in ua or 'tier_2' not in ua:
        result.die('JSON parse-or-die: update_authority must be object with tier_1 and tier_2 (D-17)')
        return result

    # discovery_markers: non-empty string array; D-2 mirror (m1): at least one
    # non-empty identifier
    dm = data.get('discovery_markers', [])
    if not isinstance(dm, list) or len(dm) == 0:
        result.die('JSON parse-or-die: discovery_markers must be a non-empty string array (D-17)')
        return result
    if not any(isinstance(m, str) and m.strip() for m in dm):
        result.die('JSON parse-or-die: discovery_markers has no non-empty identifier (D-2)')
        return result

    # enum_sources: object with sources (non-empty array) and accepted_gap (bool)
    es = data.get('enum_sources', {})
    if not isinstance(es, dict) or 'sources' not in es or 'accepted_gap' not in es:
        result.die('JSON parse-or-die: enum_sources must be object with sources array and accepted_gap bool (D-17)')
        return result
    sources = es.get('sources', [])
    accepted_gap = es.get('accepted_gap', False)
    if not isinstance(sources, list) or len(sources) == 0:
        result.die('JSON parse-or-die: enum_sources.sources must be a non-empty array (D-17)')
        return result
    # D-7 rule, both directions (same as md):
    if accepted_gap and len(sources) >= 2:
        result.die('JSON parse-or-die: enum_sources accepted_gap=true with 2+ sources (D-7: accepted-gap only legal with exactly one source)')
        return result
    if not accepted_gap and len(sources) < 2:
        result.die('JSON parse-or-die: enum_sources has fewer than 2 sources without accepted_gap=true (D-7)')
        return result

    # class_map: object mapping column -> class (same closed enum as md)
    cm = data.get('class_map', {})
    if not isinstance(cm, dict) or len(cm) == 0:
        result.die('JSON parse-or-die: class_map must be a non-empty object (D-17)')
        return result
    for col, cls in cm.items():
        if cls not in PROVENANCE_CLASSES:
            result.die(f'JSON parse-or-die: class_map unknown class "{cls}" for column "{col}"')
            return result

    # exclusion_list: array (may be empty); D-3 mirror (M2 r3): exactly the
    # fields item/reason/date, all non-empty, ISO date; extra field = die
    # (md: fewer OR more than three fields = die). Printed in full (E9 item 6).
    el = data.get('exclusion_list', None)
    if not isinstance(el, list):
        result.die('JSON parse-or-die: exclusion_list must be an array (may be empty)')
        return result
    for i, entry in enumerate(el):
        if not isinstance(entry, dict):
            result.die(f'E9 item-6 malformed: JSON exclusion_list[{i}] must be an object')
            return result
        if set(entry.keys()) != {'item', 'reason', 'date'}:
            result.die(f'E9 item-6 malformed: JSON exclusion_list[{i}] fields must be exactly item/reason/date (D-3), got {sorted(entry.keys())}')
            return result
        for f in ('item', 'reason', 'date'):
            if not isinstance(entry[f], str) or not entry[f].strip():
                result.die(f'E9 item-6 malformed: JSON exclusion_list[{i}] has empty {f} (D-3)')
                return result
        if not ISO_DATE_RE.match(entry['date'].strip()):
            result.die(f'E9 item-6 malformed: JSON exclusion_list[{i}] date not ISO YYYY-MM-DD: "{entry["date"]}" (D-3)')
            return result
        result.find(f'EXCLUSION: {entry["item"]} | {entry["reason"]} | {entry["date"]}')
    result.find(f'E9 item-6: exclusion list OK ({len(el)} entries)')

    # liveness_terminal: string; D-4 mirror: exactly one surface identifier
    lt = data.get('liveness_terminal', None)
    if not isinstance(lt, str) or not lt.strip():
        result.die('JSON parse-or-die: liveness_terminal must be a non-empty string (D-17)')
        return result
    if re.search(r'[,;]', lt):
        result.die(f'JSON parse-or-die: liveness_terminal must be exactly one identifier; comma/semicolon list forbidden (D-4): "{lt}"')
        return result

    # rows: array of objects; each row key must be in class_map
    rows = data.get('rows', [])
    if not isinstance(rows, list):
        result.die('JSON parse-or-die: rows must be an array')
        return result
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            result.die(f'JSON parse-or-die: rows[{i}] must be an object')
            return result
        for k in row.keys():
            if k not in cm:
                result.die(f'JSON parse-or-die: rows[{i}] key "{k}" not in class_map (D-17)')
                return result

    # edges: optional (edge layer adopted when present); must be array if present
    if 'edges' in data and not isinstance(data['edges'], list):
        result.die('JSON parse-or-die: edges must be an array when present (D-17)')
        return result

    # D-20 mirror (M2 r3): core columns id/location/last_activity must exist in
    # the declared column set (class_map = the JSON header); state optional.
    # id non-empty (not '-') on every row.
    for col in ('id', 'location', 'last_activity'):
        if col not in cm:
            result.die(f'D-20: required core column "{col}" absent from JSON class_map (id/location/last_activity required at core:1)')
            return result
    for i, row in enumerate(rows):
        id_val = str(row.get('id', '')).strip()
        if not id_val or id_val == '-':
            result.die(f'D-20: JSON rows[{i}] has empty id (id is the stable grep key; must be non-empty)')
            return result
    result.find('D-20: JSON core-column presence OK (id/location/last_activity mapped; all id cells non-empty)')

    # E5/D-16 + D-12 mirror (M2 r3): id non-empty on retired/frozen rows;
    # tombstone presence rule + grammar + row/block cross-check.
    retired_ids = _check_json_e5_structural(data, result)
    if result.died:
        return result
    _check_json_tombstones(data, retired_ids, result)
    if result.died:
        return result

    # D-18 mirror (M2 r3): state enum, lint-config-resident; core-reserved
    # tokens pass; undeclared vocabulary = WARN without config, die with config.
    _json_rows_header = list(cm.keys())
    check_state_enum(rows, _json_rows_header, result, lint_config=lint_config)
    if result.died:
        return result

    result.find(f'JSON substrate parse OK; core_version={C}; {len(rows)} rows')
    result.find('JSON: D-17 contract + E9 semantic-parity checks passed')
    return result


def lint_file(path, label='', lint_config=None):
    """Auto-detect md vs JSON and lint accordingly."""
    result = LintResult()
    if not os.path.exists(path):
        result.die(f'File not found: {path}')
        return result
    if path.endswith('.json'):
        return lint_json(path, label=label, lint_config=lint_config)
    else:
        return lint_md(path, label=label, lint_config=lint_config)


# ============================================================
# Structural-parse subset (S3 entrypoint) + section dumps
# ============================================================

def _parse_sections_or_fail(path):
    """Shared substrate-parse front end for --parse-only / --dump-* modes.
    Returns parsed dict (md) or ('json', data) on success.
    On failure prints the PORTFOLIO-PARSE-FAIL contract and exits 1.
    No header semantics -- structural parse only (CORE profile: EDGES optional)."""
    def fail(reason):
        print('PORTFOLIO-PARSE-FAIL')
        print(f'reason: {reason}')
        sys.exit(1)

    if not os.path.exists(path):
        fail(f'index file not found: {path}')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
    except OSError as e:
        fail(f'cannot read file: {e}')

    if path.endswith('.json'):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            fail(f'JSON parse failure (parse-or-die): {e}')
        if not isinstance(data, dict):
            fail('JSON parse-or-die: top-level value must be an object')
        return ('json', data)

    try:
        parsed = parse_md_sections(raw)
    except ValueError as e:
        fail(str(e))
    return ('md', parsed)


def cmd_parse_only(path):
    """PORTFOLIO-PARSE-OK / PORTFOLIO-PARSE-FAIL contract; exit 0/1."""
    _parse_sections_or_fail(path)
    print('PORTFOLIO-PARSE-OK')
    sys.exit(0)


def cmd_dump_rows(path):
    """After a passing structural parse, print one TSV line per data row:
    id<TAB>location<TAB>state<TAB>last_activity ('-' where the column is
    absent -- works at C0 too). Consumer: routines/portfolio-reconcile.sh."""
    kind, parsed = _parse_sections_or_fail(path)
    if kind == 'json':
        rows = parsed.get('rows', [])
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            print('\t'.join(str(row.get(c, '-')).strip() or '-'
                            for c in ('id', 'location', 'state', 'last_activity')))
    else:
        for row in parsed['rows_data']:
            print('\t'.join((row.get(c, '-') or '-')
                            for c in ('id', 'location', 'state', 'last_activity')))
    sys.exit(0)


def cmd_dump_exclusions(path):
    """After a passing structural parse, print one exclusion ITEM name per
    line (grammar violations inside the block are a full-lint duty, not a
    dump duty -- unparseable entries are skipped here)."""
    kind, parsed = _parse_sections_or_fail(path)
    if kind == 'json':
        for entry in parsed.get('exclusion_list', []) or []:
            if isinstance(entry, dict) and str(entry.get('item', '')).strip():
                print(str(entry['item']).strip())
    else:
        for line in parsed['exclude_lines']:
            line = line.strip()
            if not line.startswith('-'):
                continue
            item = line[1:].split('|')[0].strip()
            if item:
                print(item)
    sys.exit(0)


def _usage():
    print('Usage: portfolio-core-lint.py [--parse-only|--dump-rows|--dump-exclusions]')
    print('                              [--enum-config FILE] <index-path> [label]')
    sys.exit(1)


if __name__ == '__main__':
    args = sys.argv[1:]
    mode = 'lint'
    enum_config_path = None
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--parse-only':
            mode = 'parse-only'
        elif a == '--dump-rows':
            mode = 'dump-rows'
        elif a == '--dump-exclusions':
            mode = 'dump-exclusions'
        elif a == '--enum-config':
            i += 1
            if i >= len(args):
                _usage()
            enum_config_path = args[i]
        elif a.startswith('--'):
            _usage()
        else:
            positional.append(a)
        i += 1

    if len(positional) < 1:
        _usage()
    path = positional[0]
    label = positional[1] if len(positional) > 1 else ''

    if mode == 'parse-only':
        cmd_parse_only(path)
    elif mode == 'dump-rows':
        cmd_dump_rows(path)
    elif mode == 'dump-exclusions':
        cmd_dump_exclusions(path)

    lint_config = None
    if enum_config_path is not None:
        try:
            with open(enum_config_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict) or not isinstance(cfg.get('state_vocab'), list):
                print(f'[lint] bad enum-config (need JSON object with "state_vocab" list): {enum_config_path}')
                sys.exit(1)
            lint_config = {'state_vocab': [str(v) for v in cfg['state_vocab']]}
        except (OSError, json.JSONDecodeError) as e:
            print(f'[lint] cannot read enum-config {enum_config_path}: {e}')
            sys.exit(1)

    r = lint_file(path, label=label, lint_config=lint_config)
    print(r.report(label=label))
    if r.verdict() != 'PASS':
        sys.exit(1)
