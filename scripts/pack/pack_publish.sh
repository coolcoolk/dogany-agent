#!/bin/bash
# pack_publish.sh -- DGN-227 B4 publish pipeline (Part A infrastructure).
#
# Turns a VALIDATED live domain agent into a refined, personal-data-free
# catalog snapshot payload + catalog entry. This is the publish (write) side
# of the pack lifecycle; pack_install.sh is the install (read) side.
#
# SCOPE (DGN-419 Part A): this is REUSABLE TOOLING. It takes a SOURCE agent
# root and a spec of what to publish, extracts + refines the payload, runs the
# three B4 refinement gates (each loud-FAIL on violation), generates the
# publish-side checksums.sha (exact DGN-418 NM3 install-gate format), writes
# .source-sync provenance + a pack CHANGELOG, and upserts the catalog entry.
#
# DATA BOUNDARY: this script only READS the source root (one-writer invariant,
# B4 "라이브 소스 인스턴스에는 어떤 쓰기도 하지 않는다"). It NEVER writes to
# the source. It is exercised in the harness against a SYNTHETIC fixture agent
# with FAKE personal data -- never against a live agent.
#
# The B4 pipeline steps this script implements (spec B4, lines 275-298):
#   1. source select     -- caller supplies --source-root + --sections/--skills
#   2. extract           -- AGENT.md section -> AGENT.md.add (persona excluded),
#                           skill dirs, domain routines/plists, knowledge frozen
#                           snapshot (B5; release pin)
#   3. refine + 3 gates  -- (a) secret-sweep class (personal-data / conversation
#                           memory removal), (b) persona-token residue grep,
#                           (c) personal-data exclusion list check
#                           ALSO: persona-field blanking (B2), knowledge
#                           release-pin (B5) verified as a gate.
#   4. register          -- pack-manifest.json + catalog.json upsert +
#                           pack_version + CHANGELOG
#   5. NM3 gate          -- checksums.sha (publish-side, DGN-418 format)
#   6. source-sync       -- .source-sync provenance baseline
#
# Usage:
#   pack_publish.sh --source-root <live-agent-root> \
#                   --pack-id <id> --pack-version <semver> \
#                   --reference-slug <slug> \
#                   --packs-dir <dogany-agent/packs> \
#                   [--manifest-in <manifest.json template>] \
#                   [--catalog-fields-in <json fragment for catalog entry>] \
#                   [--changelog-note "<one line>"] \
#                   [--knowledge-warehouse <name>] \
#                   [--section <AGENT.md heading> ...] \
#                   [--skill <skill-dir-name> ...] \
#                   [--routine <basename> ...] \
#                   [--script <basename> ...]
#
# The output payload lands in <packs-dir>/<pack-id>/ (package_dir = pack-id).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- persona tokens forbidden in payload (B2 "페르소나 토큰 금지 규약") -------
# Any file in the payload carrying one of these render tokens FAILs gate (b).
PERSONA_TOKENS_RE='__AGENT_LABEL__|__USER_LABEL__|__AGENT_NAME__|__USER_NAME__'

# --- personal-data exclusion list (B2 "제외 -- 절대 포함 금지") --------------
# Directory / file names that must NOT appear anywhere in the payload.
# (memories/, transcripts, USER.md content, env/tokens, real DBs.)
EXCLUDE_NAMES=(
  "memories" "USER.md" ".env" ".telegram_bot"
)
EXCLUDE_SUFFIXES=( ".db" ".sqlite" ".sqlite3" )
# transcript / conversation-memory directory names (B4-3a: conversation memory)
EXCLUDE_TRANSCRIPT_DIRS=( "transcripts" "conversations" "sessions" "chatlog" )

_die() { echo "[pack_publish] FATAL: $*" >&2; exit 1; }
_log() { echo "[pack_publish] $*"; }

# --- argument parse ---------------------------------------------------------
SOURCE_ROOT=""; PACK_ID=""; PACK_VERSION=""; REF_SLUG=""; PACKS_DIR=""
MANIFEST_IN=""; CATALOG_FIELDS_IN=""; CHANGELOG_NOTE=""; KNOWLEDGE_WH=""
SECTIONS=(); SKILLS=(); ROUTINES=(); SCRIPTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-root)        SOURCE_ROOT="$2"; shift 2 ;;
    --pack-id)            PACK_ID="$2"; shift 2 ;;
    --pack-version)       PACK_VERSION="$2"; shift 2 ;;
    --reference-slug)     REF_SLUG="$2"; shift 2 ;;
    --packs-dir)          PACKS_DIR="$2"; shift 2 ;;
    --manifest-in)        MANIFEST_IN="$2"; shift 2 ;;
    --catalog-fields-in)  CATALOG_FIELDS_IN="$2"; shift 2 ;;
    --changelog-note)     CHANGELOG_NOTE="$2"; shift 2 ;;
    --knowledge-warehouse) KNOWLEDGE_WH="$2"; shift 2 ;;
    --section)            SECTIONS+=("$2"); shift 2 ;;
    --skill)              SKILLS+=("$2"); shift 2 ;;
    --routine)            ROUTINES+=("$2"); shift 2 ;;
    --script)             SCRIPTS+=("$2"); shift 2 ;;
    *) _die "unknown option: $1" ;;
  esac
done

[[ -n "$SOURCE_ROOT" ]]  || _die "--source-root required"
[[ -d "$SOURCE_ROOT" ]]  || _die "source root not a directory: $SOURCE_ROOT"
[[ -n "$PACK_ID" ]]      || _die "--pack-id required"
[[ -n "$PACK_VERSION" ]] || _die "--pack-version required"
[[ -n "$REF_SLUG" ]]     || _die "--reference-slug required"
[[ -n "$PACKS_DIR" ]]    || _die "--packs-dir required"
printf '%s' "$PACK_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
  || _die "--pack-version must be semver (x.y.z): $PACK_VERSION"

PACKAGE_DIR="$PACKS_DIR/$PACK_ID"
REF_DIR="$PACKAGE_DIR/$REF_SLUG"
CATALOG_FILE="$PACKS_DIR/catalog.json"
TODAY="$(date '+%Y-%m-%d')"

_log "=== publish START pack=$PACK_ID v$PACK_VERSION source=$SOURCE_ROOT ==="
_log "one-writer invariant: source root is READ-ONLY (no writes) (B4)"

# ===========================================================================
# STEP 2: EXTRACT -- build the payload tree from the source (read-only source)
# ===========================================================================
rm -rf "$PACKAGE_DIR"
mkdir -p "$REF_DIR"

# -- 2a. AGENT.md domain sections -> AGENT.md.add (persona fields EXCLUDED) --
# Persona-field blanking gate (B2): the extracted fragment must carry the
# domain Role/Workflows only; identity/persona fields (name/emoji/tone/humor/
# form-of-address) are NEVER copied into the payload. The extractor pulls only
# the requested --section headings and refuses persona headings.
SRC_AGENT="$SOURCE_ROOT/AGENT.md"
if [[ ${#SECTIONS[@]} -gt 0 ]]; then
  [[ -f "$SRC_AGENT" ]] || _die "source AGENT.md missing: $SRC_AGENT"
  python3 - "$SRC_AGENT" "$REF_DIR/AGENT.md.add" "$PACK_ID" "${SECTIONS[@]}" <<'PYEOF'
import sys
src, out, pack_id = sys.argv[1], sys.argv[2], sys.argv[3]
wanted = sys.argv[4:]
# persona headings we refuse to extract (blanking gate, B2)
PERSONA_HEADINGS = ("identity", "persona", "relationship", "정체성", "페르소나", "관계")
def hlevel(h): return len(h) - len(h.lstrip("#"))
lines = open(src, encoding="utf-8").read().split("\n")
def extract(heading):
    lvl = hlevel(heading); start = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == heading: start = i; break
    if start is None: return None
    end = len(lines)
    for i in range(start+1, len(lines)):
        if lines[i].startswith("#") and hlevel(lines[i]) <= lvl:
            end = i; break
    body = lines[start:end]
    while body and body[-1].strip() == "": body.pop()
    return "\n".join(body)
out_lines = [f"<!-- DOGANY-PACK:{pack_id}:BEGIN -->", ""]
for h in wanted:
    low = h.lower()
    for bad in PERSONA_HEADINGS:
        if bad in low:
            sys.stderr.write("PERSONA_SECTION %s\n" % h); sys.exit(3)
    text = extract(h)
    if text is None:
        sys.stderr.write("SECTION_NOT_FOUND %s\n" % h); sys.exit(4)
    out_lines.append(text); out_lines.append("")
out_lines.append(f"<!-- DOGANY-PACK:{pack_id}:END -->")
open(out, "w", encoding="utf-8").write("\n".join(out_lines) + "\n")
print("extracted %d section(s) -> AGENT.md.add" % len(wanted))
PYEOF
  rc=$?
  [[ $rc -eq 0 ]] || _die "AGENT.md section extraction failed (rc=$rc -- persona section or missing heading)"
  _log "STEP 2a: extracted ${#SECTIONS[@]} domain section(s) -> AGENT.md.add (persona fields excluded)"
fi

# -- 2b. skill directories (full multi-file dirs -> payload skills/) --
if [[ ${#SKILLS[@]} -gt 0 ]]; then
  mkdir -p "$REF_DIR/skills"
  for sk in "${SKILLS[@]}"; do
    local_src="$SOURCE_ROOT/.claude/skills-bundle/$sk"
    [[ -d "$local_src" ]] || local_src="$SOURCE_ROOT/.claude/skills/$sk"
    [[ -d "$local_src" ]] || _die "skill dir not found in source: $sk"
    rsync -a --exclude '.git' "$local_src/" "$REF_DIR/skills/$sk/"
    _log "STEP 2b: extracted skill dir -> skills/$sk"
  done
fi

# -- 2c. domain routines + plists --
if [[ ${#ROUTINES[@]} -gt 0 ]]; then
  mkdir -p "$REF_DIR/routines"
  for rt in "${ROUTINES[@]}"; do
    src_rt="$SOURCE_ROOT/routines/$rt"
    [[ -e "$src_rt" ]] || _die "routine not found in source: $rt"
    cp "$src_rt" "$REF_DIR/routines/$rt"
    _log "STEP 2c: extracted routine -> routines/$rt"
  done
fi

# -- 2c2. scripts (e.g. knowledge-snapshot.sh; STEP 6 install runs it) --
if [[ ${#SCRIPTS[@]} -gt 0 ]]; then
  mkdir -p "$REF_DIR/scripts"
  for sc in "${SCRIPTS[@]}"; do
    src_sc="$SOURCE_ROOT/scripts/$sc"
    [[ -e "$src_sc" ]] || _die "script not found in source: $sc"
    cp "$src_sc" "$REF_DIR/scripts/$sc"
    _log "STEP 2c2: extracted script -> scripts/$sc"
  done
fi

# -- 2d. knowledge frozen snapshot (B5) + release-pin --
if [[ -n "$KNOWLEDGE_WH" ]]; then
  WH_SRC="$SOURCE_ROOT/knowledge/$KNOWLEDGE_WH"
  [[ -d "$WH_SRC" ]] || _die "knowledge warehouse not found in source: $KNOWLEDGE_WH"
  WH_DST="$REF_DIR/knowledge/$KNOWLEDGE_WH"
  mkdir -p "$WH_DST"
  # frozen snapshot: instance-accumulated dirs are excluded (B5 규약)
  rsync -a \
    --exclude 'instance/' --exclude 'GAPS-instance.md' \
    --exclude '.git' \
    "$WH_SRC/" "$WH_DST/"
  # release-pin: the snapshot MUST be pinned to a release (gate c, B5).
  # If the source warehouse ships a .snapshot-pin we carry its release; else
  # the pin's release is derived from --pack-version (the publish release).
  pin_release="$PACK_VERSION"
  if [[ -f "$WH_SRC/.snapshot-pin" ]]; then
    src_rel="$(grep -E '^release:' "$WH_SRC/.snapshot-pin" | head -1 | sed 's/^release:[[:space:]]*//')"
    [[ -n "$src_rel" ]] && pin_release="$src_rel"
  fi
  cat > "$WH_DST/.snapshot-pin" <<PIN
warehouse: $KNOWLEDGE_WH
release: $pin_release
snapshot_date: $TODAY
source: bundled-frozen
PIN
  _log "STEP 2d: froze knowledge/$KNOWLEDGE_WH (release pin $pin_release, instance accumulations excluded)"
fi

# ===========================================================================
# STEP 4 (register, part 1): manifest + CHANGELOG (catalog upsert after gates)
# ===========================================================================
# -- pack-manifest.json: from a supplied template, or a minimal default --
if [[ -n "$MANIFEST_IN" ]]; then
  [[ -f "$MANIFEST_IN" ]] || _die "manifest template not found: $MANIFEST_IN"
  cp "$MANIFEST_IN" "$PACKAGE_DIR/pack-manifest.json"
else
  python3 - "$PACKAGE_DIR/pack-manifest.json" "$PACK_ID" "$REF_SLUG" "$KNOWLEDGE_WH" <<'PYEOF'
import json, sys
out, pid, ref, wh = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
m = {"name": pid, "reference_slug": ref,
     "reference_root": "/opt/dogany/agents/%s" % ref,
     "categories": []}
if wh:
    m["knowledge"] = {"warehouse": wh}
open(out, "w").write(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
PYEOF
fi
_log "STEP 4: wrote pack-manifest.json"

# -- pack CHANGELOG (B3 -- pack versioning history file) --
CHANGELOG="$PACKAGE_DIR/CHANGELOG.md"
note="${CHANGELOG_NOTE:-published from source snapshot}"
cat > "$CHANGELOG" <<CL
# $PACK_ID pack -- CHANGELOG

Pack versioning history (B3). Newest first. Version axis is the pack's own
semver, independent of the framework release and the knowledge snapshot pin.

## $PACK_VERSION -- $TODAY
- $note
CL
_log "STEP 4: wrote CHANGELOG.md ($PACK_VERSION)"

# ===========================================================================
# STEP 3: REFINE + THREE B4 GATES (each loud-FAIL on violation)
# ===========================================================================
# Run gates against the assembled payload (REF_DIR) + manifest/CHANGELOG.
_log "STEP 3: running 3 refinement gates on payload $REF_DIR"

# -- GATE (a): personal-data / conversation-memory removal (secret-sweep class) --
# Spec B4-3a: "팩-창고 secret-sweep 클래스". The refined snapshot must contain
# ZERO personal data / conversation memory. We detect excluded names / suffixes
# / transcript dirs anywhere in the payload and loud-FAIL if any survive.
gate_a_fail=""
while IFS= read -r f; do
  base="$(basename "$f")"
  for ex in "${EXCLUDE_NAMES[@]}"; do
    [[ "$base" == "$ex" ]] && gate_a_fail+="  personal-data file: ${f#$PACKAGE_DIR/}"$'\n'
  done
  for sfx in "${EXCLUDE_SUFFIXES[@]}"; do
    [[ "$base" == *"$sfx" ]] && gate_a_fail+="  real-data file: ${f#$PACKAGE_DIR/}"$'\n'
  done
done < <(find "$PACKAGE_DIR" -type f)
while IFS= read -r d; do
  base="$(basename "$d")"
  for ex in "${EXCLUDE_TRANSCRIPT_DIRS[@]}" "${EXCLUDE_NAMES[@]}"; do
    [[ "$base" == "$ex" ]] && gate_a_fail+="  personal/transcript dir: ${d#$PACKAGE_DIR/}"$'\n'
  done
done < <(find "$PACKAGE_DIR" -type d)
if [[ -n "$gate_a_fail" ]]; then
  echo "[pack_publish] GATE (a) personal-data/conversation-memory removal FAILED:" >&2
  printf '%s' "$gate_a_fail" >&2
  _die "GATE (a): payload carries personal data / conversation memory (B4-3a) -- publish aborted"
fi
_log "  GATE (a) personal-data/conversation-memory removal: PASS"

# -- GATE (b): persona-token residue (B2 페르소나 토큰 금지 규약) --
# Spec B4-3b: grep for __AGENT_LABEL__ / __USER_LABEL__ class tokens; residue
# -> FAIL. install renders the payload BEFORE onboarding fixes persona, so a
# persona token would be soaked to the mint default permanently.
if grep -rlE "$PERSONA_TOKENS_RE" "$PACKAGE_DIR" 2>/dev/null | grep -q .; then
  echo "[pack_publish] GATE (b) persona-token residue FAILED:" >&2
  grep -rlE "$PERSONA_TOKENS_RE" "$PACKAGE_DIR" 2>/dev/null \
    | sed "s|^$PACKAGE_DIR/|  |" >&2
  _die "GATE (b): persona render token(s) survive in payload (B2/B4-3b) -- publish aborted"
fi
_log "  GATE (b) persona-token residue: PASS"

# -- GATE (c): knowledge-warehouse release-pin (B5) --
# Spec B4 step 2 + B5: the bundled knowledge snapshot is pinned to a RELEASE,
# not a floating ref. If the payload ships a knowledge/ warehouse, its
# .snapshot-pin MUST carry a concrete release. A floating pin -> FAIL.
if [[ -d "$REF_DIR/knowledge" ]]; then
  while IFS= read -r pin; do
    rel="$(grep -E '^release:' "$pin" | head -1 | sed 's/^release:[[:space:]]*//' | tr -d '[:space:]')"
    if [[ -z "$rel" ]]; then
      _die "GATE (c): knowledge snapshot pin has no release: ${pin#$PACKAGE_DIR/} (B5) -- publish aborted"
    fi
    # floating refs are not a release pin (loud-FAIL, B5 "릴리스 핀, 부동 참조 금지")
    case "$rel" in
      HEAD|head|latest|main|master|floating|"*")
        _die "GATE (c): knowledge snapshot pinned to a FLOATING ref '$rel': ${pin#$PACKAGE_DIR/} (B5) -- publish aborted" ;;
    esac
    _log "  GATE (c): knowledge pin release='$rel' (${pin#$PACKAGE_DIR/})"
  done < <(find "$REF_DIR/knowledge" -name '.snapshot-pin')
  # a knowledge/ dir with no pin at all is also a floating-ref failure
  if ! find "$REF_DIR/knowledge" -name '.snapshot-pin' | grep -q .; then
    _die "GATE (c): knowledge/ shipped with NO .snapshot-pin (floating) (B5) -- publish aborted"
  fi
fi
_log "  GATE (c) knowledge release-pin: PASS"
_log "STEP 3: all 3 gates PASS"

# ===========================================================================
# STEP 5: NM3 GATE -- checksums.sha (publish-side, DGN-418 install-gate format)
# ===========================================================================
# Format (must match pack_install.sh NM3 gate exactly, closes DGN-418 OQ3):
#   '<sha256hex>  <relpath>'  -- two-space separator, relpath relative to
#   package_dir, python3-hashable, one line per file, checksums.sha excluded.
CHECKSUMS="$PACKAGE_DIR/checksums.sha"
( cd "$PACKAGE_DIR" && find . -type f ! -name checksums.sha | sed 's|^\./||' \
    | LC_ALL=C sort \
    | while IFS= read -r rel; do
        hex="$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$rel")"
        printf '%s  %s\n' "$hex" "$rel"
      done ) > "$CHECKSUMS"
n_sums="$(grep -vc '^#' "$CHECKSUMS" 2>/dev/null || echo 0)"
_log "STEP 5: NM3 checksums.sha generated over $n_sums payload file(s) (DGN-418 format: '<hex>  <relpath>')"

# ===========================================================================
# STEP 6: .source-sync provenance baseline
# ===========================================================================
# Records the snapshot<->source-live sync provenance (B4 step 6). release-
# preflight later reads this to warn when the source live agent drifts.
# Section headings are private per-source data; recorded from --section only.
SYNC_FILE="$PACKAGE_DIR/.source-sync"
if [[ ${#SECTIONS[@]} -gt 0 && -f "$SRC_AGENT" ]]; then
  python3 - "$SRC_AGENT" "$SYNC_FILE" "$TODAY" "$PACK_ID" "$PACK_VERSION" "${SECTIONS[@]}" <<'PYEOF'
import sys, hashlib
src, out, today, pid, pver = sys.argv[1:6]
headings = sys.argv[6:]
def hlevel(h): return len(h) - len(h.lstrip("#"))
lines = open(src, encoding="utf-8").read().split("\n")
def extract(heading):
    lvl = hlevel(heading); start = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == heading: start = i; break
    if start is None: return None
    end = len(lines)
    for i in range(start+1, len(lines)):
        if lines[i].startswith("#") and hlevel(lines[i]) <= lvl:
            end = i; break
    body = lines[start:end]
    while body and body[-1].strip() == "": body.pop()
    return "\n".join(body)
rows = []
for h in headings:
    t = extract(h)
    if t is None: continue
    norm = "\n".join(l.rstrip() for l in t.split("\n"))
    rows.append((hashlib.sha256(norm.encode("utf-8")).hexdigest(), h, today))
hdr = ("# %s pack source conformance baseline\n"
       "# Generated by: scripts/pack/pack_publish.sh\n"
       "# pack: %s  pack_version: %s  snapshot_date: %s\n"
       "#\n"
       "# Each row: sha256 of a pack-mirrored source section (heading to next\n"
       "# same-or-higher heading, trailing blanks + per-line trailing ws stripped).\n"
       "# release-preflight warns when a listed section drifts from this snapshot.\n"
       "# Format:  <sha256hex>\\t<section_heading>\\t<snapshot_date>\n"
       "# Source file path is machine-specific private data, NOT stored here.\n"
       % (pid, pid, pver, today))
with open(out, "w", encoding="utf-8") as fh:
    fh.write(hdr + "\n")
    for h, heading, d in rows:
        fh.write("%s\t%s\t%s\n" % (h, heading, d))
print("source-sync: %d section baseline(s) recorded" % len(rows))
PYEOF
  _log "STEP 6: .source-sync provenance baseline written (${#SECTIONS[@]} section(s))"
else
  # No AGENT.md sections published: still record a provenance stub so the
  # snapshot<->source link is not silently absent.
  cat > "$SYNC_FILE" <<SS
# $PACK_ID pack source conformance baseline
# Generated by: scripts/pack/pack_publish.sh
# pack: $PACK_ID  pack_version: $PACK_VERSION  snapshot_date: $TODAY
#
# No AGENT.md sections published in this pack -- no section drift baseline.
SS
  _log "STEP 6: .source-sync provenance stub written (no AGENT.md sections)"
fi
# NB: checksums.sha is generated in STEP 5 BEFORE .source-sync so the sync file
# is not itself checksummed (it is publish provenance, not installed payload).

# ===========================================================================
# STEP 4 (register, part 2): catalog.json upsert (AFTER gates pass)
# ===========================================================================
python3 - "$CATALOG_FILE" "$PACK_ID" "$PACK_VERSION" "${CATALOG_FIELDS_IN:-}" <<'PYEOF'
import json, os, sys
catalog, pid, pver, fields_in = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
if os.path.isfile(catalog):
    cat = json.load(open(catalog))
else:
    cat = {"version": 1, "packs": []}
entry = {"id": pid, "package_dir": pid, "status": "official", "pack_version": pver}
if fields_in and os.path.isfile(fields_in):
    entry.update(json.load(open(fields_in)))
# force id/package_dir/pack_version to authoritative values
entry["id"] = pid; entry["package_dir"] = pid; entry["pack_version"] = pver
entry.setdefault("status", "official")
packs = cat.setdefault("packs", [])
for i, p in enumerate(packs):
    if p.get("id") == pid:
        packs[i] = entry; break
else:
    packs.append(entry)
json.dump(cat, open(catalog, "w"), ensure_ascii=False, indent=2)
open(catalog, "a").write("\n")
print("catalog: upserted %s@%s (status=%s)" % (pid, pver, entry.get("status")))
PYEOF
_log "STEP 4: catalog.json upserted"

_log "=== publish DONE pack=$PACK_ID v$PACK_VERSION -> $PACKAGE_DIR ==="
_log "next (Part B, gated): sandbox install rehearsal (mint + pack_install full chain) -> release"
