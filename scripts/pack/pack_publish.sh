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
# DGN-441: two modes.
#   snapshot (default, back-compat): materialize payload from live source, seal.
#   finalize                       : seal an EXISTING hand-refined payload
#                                    IN PLACE (no materialize, payload immutable).
MODE="snapshot"
SOURCE_ROOT=""; PACK_ID=""; PACK_VERSION=""; REF_SLUG=""; PACKS_DIR=""
MANIFEST_IN=""; CATALOG_FIELDS_IN=""; CHANGELOG_NOTE=""; KNOWLEDGE_WH=""
SECTIONS=(); SKILLS=(); ROUTINES=(); SCRIPTS=()
# track which materialize flags were supplied (for the finalize G-F2 guard)
MATERIALIZE_FLAGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)               MODE="$2"; shift 2 ;;
    --source-root)        SOURCE_ROOT="$2"; shift 2 ;;
    --pack-id)            PACK_ID="$2"; shift 2 ;;
    --pack-version)       PACK_VERSION="$2"; shift 2 ;;
    --reference-slug)     REF_SLUG="$2"; MATERIALIZE_FLAGS+=("--reference-slug"); shift 2 ;;
    --packs-dir)          PACKS_DIR="$2"; shift 2 ;;
    --manifest-in)        MANIFEST_IN="$2"; MATERIALIZE_FLAGS+=("--manifest-in"); shift 2 ;;
    --catalog-fields-in)  CATALOG_FIELDS_IN="$2"; shift 2 ;;
    --changelog-note)     CHANGELOG_NOTE="$2"; shift 2 ;;
    --knowledge-warehouse) KNOWLEDGE_WH="$2"; MATERIALIZE_FLAGS+=("--knowledge-warehouse"); shift 2 ;;
    --section)            SECTIONS+=("$2"); MATERIALIZE_FLAGS+=("--section"); shift 2 ;;
    --skill)              SKILLS+=("$2"); MATERIALIZE_FLAGS+=("--skill"); shift 2 ;;
    --routine)            ROUTINES+=("$2"); MATERIALIZE_FLAGS+=("--routine"); shift 2 ;;
    --script)             SCRIPTS+=("$2"); MATERIALIZE_FLAGS+=("--script"); shift 2 ;;
    *) _die "unknown option: $1" ;;
  esac
done

[[ "$MODE" == "snapshot" || "$MODE" == "finalize" ]] \
  || _die "--mode must be snapshot|finalize: $MODE"

# --- shared required args (both modes) --------------------------------------
[[ -n "$SOURCE_ROOT" ]]  || _die "--source-root required"
[[ -d "$SOURCE_ROOT" ]]  || _die "source root not a directory: $SOURCE_ROOT"
[[ -n "$PACK_ID" ]]      || _die "--pack-id required"
[[ -n "$PACK_VERSION" ]] || _die "--pack-version required"
[[ -n "$PACKS_DIR" ]]    || _die "--packs-dir required"
printf '%s' "$PACK_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
  || _die "--pack-version must be semver (x.y.z): $PACK_VERSION"

LIB_DIR="$SCRIPT_DIR/lib"
PACKAGE_DIR="$PACKS_DIR/$PACK_ID"
CATALOG_FILE="$PACKS_DIR/catalog.json"
TODAY="$(date '+%Y-%m-%d')"

if [[ "$MODE" == "snapshot" ]]; then
  # snapshot resolves the reference slug from --reference-slug (materialize arg).
  [[ -n "$REF_SLUG" ]] || _die "--reference-slug required"
  REF_DIR="$PACKAGE_DIR/$REF_SLUG"
fi

_log "=== publish START mode=$MODE pack=$PACK_ID v$PACK_VERSION source=$SOURCE_ROOT ==="
_log "one-writer invariant: source root is READ-ONLY (no writes) (B4)"

# ===========================================================================
# FINALIZE MODE (DGN-441): F0-F4 -- guards + refslug resolution + drift report.
# Materialization (STEP 2) is SKIPPED: the payload already exists on disk and is
# treated as IMMUTABLE (I1). Only the seal artifacts are (re)written. The shared
# seal steps (STEP 4 manifest+CHANGELOG, STEP 3 gates, STEP 5 checksums,
# catalog upsert) run below with mode-conditional pieces.
# ===========================================================================
if [[ "$MODE" == "finalize" ]]; then
  SRC_AGENT="$SOURCE_ROOT/AGENT.md"

  # -- F0: mode guard G-F2 -- materialize flags are illegal in finalize --------
  # finalize does NOT read live source sections / rebuild the payload. A stray
  # materialize flag is a user error (ignoring it would imply "it copies").
  if [[ ${#MATERIALIZE_FLAGS[@]} -gt 0 ]]; then
    _die "G-F2: finalize does not materialize; remove --${MATERIALIZE_FLAGS[0]#--} (and any other materialize flag)"
  fi

  # -- F1: payload existence gate (G-F1) --------------------------------------
  # FAIL when: (1) package dir absent, (2) refslug unresolved, (3) no sealable
  # content under <refslug>/. checksums.sha presence is NOT a criterion (a
  # pre-GA hand-refined pack legitimately has none yet).
  [[ -d "$PACKAGE_DIR" ]] \
    || _die "G-F1: payload absent -- no directory $PACKAGE_DIR (finalize cannot seal what does not exist)"

  # -- F2: reference-slug resolution -------------------------------------------
  # Prefer pack-manifest.json reference_slug; else infer the SOLE <pack-id>/*/
  # subdirectory. 0 or >1 candidate dirs => FAIL (refslug physically exists in
  # the payload; re-supplying it by hand is an error source).
  REF_SLUG=""
  MANIFEST_FILE="$PACKAGE_DIR/pack-manifest.json"
  if [[ -f "$MANIFEST_FILE" ]]; then
    REF_SLUG="$(python3 -c "import json,sys;
d=json.load(open(sys.argv[1]));
v=d.get('reference_slug','');
print(v if isinstance(v,str) else '')" "$MANIFEST_FILE" 2>/dev/null || echo "")"
  fi
  if [[ -z "$REF_SLUG" ]]; then
    # infer from the sole subdirectory of the package dir (bash 3.2-safe: no
    # mapfile -- read the sorted dir list into a plain array).
    _cand=()
    while IFS= read -r _d; do
      [[ -n "$_d" ]] && _cand+=("$_d")
    done < <(find "$PACKAGE_DIR" -mindepth 1 -maxdepth 1 -type d \
      -exec basename {} \; | LC_ALL=C sort)
    if [[ ${#_cand[@]} -eq 1 ]]; then
      REF_SLUG="${_cand[0]}"
      _log "F2: reference slug inferred from sole payload subdir -> $REF_SLUG"
    elif [[ ${#_cand[@]} -eq 0 ]]; then
      _die "G-F1: no reference-slug directory under $PACKAGE_DIR (payload has no <refslug>/ content)"
    else
      _die "G-F1: cannot infer reference slug -- ${#_cand[@]} candidate dirs under $PACKAGE_DIR (${_cand[*]}); add pack-manifest.json reference_slug"
    fi
  else
    _log "F2: reference slug from pack-manifest.json -> $REF_SLUG"
  fi
  REF_DIR="$PACKAGE_DIR/$REF_SLUG"
  [[ -d "$REF_DIR" ]] \
    || _die "G-F1: resolved reference slug dir absent: $REF_DIR"

  # -- F1 (cont.): real-content check -- at least one sealable artifact --------
  _has_content=""
  [[ -f "$REF_DIR/AGENT.md.add" ]] && _has_content=1
  for _d in skills routines scripts knowledge; do
    if [[ -d "$REF_DIR/$_d" ]] && find "$REF_DIR/$_d" -type f | grep -q .; then
      _has_content=1
    fi
  done
  [[ -n "$_has_content" ]] \
    || _die "G-F1: payload has no sealable content under $REF_DIR (AGENT.md.add / skills / routines / scripts / knowledge all empty)"
  _log "F1: payload existence + real-content check PASS (refslug=$REF_SLUG)"

  # F3 (3 B4 gates) runs in the SHARED gate block below (in-place, detect-only).
  # F4 (drift report) runs immediately below: non-destructive, WARN-only.

  # ===========================================================================
  # F4: drift report (non-destructive) -- G-F3 always emits MATCH/DRIFT/MISSING.
  # Read the EXISTING .source-sync; for each recorded heading, extract the
  # CURRENT source AGENT.md section and compare sha256. NEVER writes .source-sync
  # (I1/F3): finalize does not regenerate the baseline, so intentional
  # divergence stays visible. exit 0 regardless (drift does not block).
  # ===========================================================================
  SYNC_FILE="$PACKAGE_DIR/.source-sync"
  _log "F4: drift report (non-destructive; baseline .source-sync is NOT rewritten)"
  if [[ ! -f "$SYNC_FILE" ]]; then
    _log "  DRIFT no baseline -- skipped (.source-sync absent)"
  elif [[ ! -f "$SRC_AGENT" ]]; then
    _log "  DRIFT SKIPPED -- source AGENT.md absent ($SRC_AGENT); baseline preserved"
  else
    python3 - "$LIB_DIR/extract_section.py" "$SYNC_FILE" "$SRC_AGENT" <<'PYEOF'
import sys, importlib.util
helper_path, sync_file, src_agent = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("extract_section", helper_path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
# read baseline rows: <sha256hex>\t<heading>\t<snapshot_date> (data rows only)
rows = []
for line in open(sync_file, encoding="utf-8"):
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        continue
    parts = line.split("\t")
    if len(parts) < 2:
        continue
    # new format: parts[1] is the heading; tolerate legacy path-first format
    if parts[1].startswith("#"):
        rows.append((parts[0], parts[1]))
    elif len(parts) >= 3 and parts[2].startswith("#"):
        rows.append((parts[0], parts[2]))
content = open(src_agent, encoding="utf-8").read()
n_match = n_drift = n_missing = 0
for base_sha, heading in rows:
    cur_sha, _ = mod.section_sha(content, heading)
    if cur_sha is None:
        print("  DRIFT MISSING  %s (heading absent in current source)" % heading)
        n_missing += 1
    elif cur_sha == base_sha:
        print("  DRIFT MATCH    %s" % heading)
        n_match += 1
    else:
        print("  DRIFT DRIFT    %s (source diverged from baseline)" % heading)
        n_drift += 1
print("  DRIFT summary: %d MATCH, %d DRIFT, %d MISSING (WARN only, not blocking)"
      % (n_match, n_drift, n_missing))
PYEOF
  fi
fi

# ===========================================================================
# STEP 2: EXTRACT -- build the payload tree from the source (read-only source)
# ===========================================================================
# SNAPSHOT ONLY (DGN-441): finalize skips materialization entirely (I1).
if [[ "$MODE" == "snapshot" ]]; then
# F4: the CHANGELOG is VERSION HISTORY, not a source snapshot -- it must survive
# the payload wipe so a re-snapshot PREPENDS onto prior history (STEP 4 below).
_CL_STASH=""
if [[ -f "$PACKAGE_DIR/CHANGELOG.md" ]]; then
  _CL_STASH="$(mktemp)"
  cp "$PACKAGE_DIR/CHANGELOG.md" "$_CL_STASH"
fi
rm -rf "$PACKAGE_DIR"
mkdir -p "$REF_DIR"
if [[ -n "$_CL_STASH" ]]; then
  cp "$_CL_STASH" "$PACKAGE_DIR/CHANGELOG.md"
  rm -f "$_CL_STASH"
fi

# -- 2a. AGENT.md domain sections -> AGENT.md.add (persona fields EXCLUDED) --
# Persona-field blanking gate (B2): the extracted fragment must carry the
# domain Role/Workflows only; identity/persona fields (name/emoji/tone/humor/
# form-of-address) are NEVER copied into the payload. The extractor pulls only
# the requested --section headings and refuses persona headings.
SRC_AGENT="$SOURCE_ROOT/AGENT.md"
if [[ ${#SECTIONS[@]} -gt 0 ]]; then
  [[ -f "$SRC_AGENT" ]] || _die "source AGENT.md missing: $SRC_AGENT"
  python3 - "$LIB_DIR/extract_section.py" "$SRC_AGENT" "$REF_DIR/AGENT.md.add" "$PACK_ID" "${SECTIONS[@]}" <<'PYEOF'
import sys, importlib.util
helper_path, src, out, pack_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
wanted = sys.argv[5:]
# W-A: reuse the single-source extractor helper (same code path as .source-sync
# + finalize drift). The persona-blanking gate (B2) stays local to this step.
spec = importlib.util.spec_from_file_location("extract_section", helper_path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
# persona headings we refuse to extract (blanking gate, B2)
PERSONA_HEADINGS = ("identity", "persona", "relationship", "정체성", "페르소나", "관계")
content = open(src, encoding="utf-8").read()
out_lines = [f"<!-- DOGANY-PACK:{pack_id}:BEGIN -->", ""]
for h in wanted:
    low = h.lower()
    for bad in PERSONA_HEADINGS:
        if bad in low:
            sys.stderr.write("PERSONA_SECTION %s\n" % h); sys.exit(3)
    text = mod.extract_section(content, h)
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
fi  # end snapshot-only materialization (STEP 2)

# ===========================================================================
# STEP 4 (register, part 1): manifest + CHANGELOG (catalog upsert after gates)
# ===========================================================================
# -- pack-manifest.json: from a supplied template, or a minimal default --
# SNAPSHOT ONLY: finalize keeps the EXISTING hand-refined manifest untouched
# (I1 -- payload immutable; the manifest is a payload artifact, not a seal one).
if [[ "$MODE" == "snapshot" ]]; then
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
else
  _log "STEP 4: pack-manifest.json preserved (finalize keeps existing payload manifest)"
fi

# -- pack CHANGELOG (B3 -- pack versioning history file) --
# DGN-441 F4: newest-first PREPEND, not overwrite. A re-publish must NOT erase
# prior version history. Absent CHANGELOG => new file (same result as before).
# Present => split header block from entry blocks; a same-version entry is
# replaced in place (no duplicate), otherwise the new entry is inserted at the
# front of the existing entries. snapshot and finalize share this path.
CHANGELOG="$PACKAGE_DIR/CHANGELOG.md"
# OQ-A: mode-specific default note. finalize is NOT a source snapshot, so the
# snapshot default would mislead; use a re-seal phrasing when none supplied.
if [[ "$MODE" == "finalize" ]]; then
  note="${CHANGELOG_NOTE:-finalize re-seal (payload unchanged)}"
else
  note="${CHANGELOG_NOTE:-published from source snapshot}"
fi
python3 - "$CHANGELOG" "$PACK_ID" "$PACK_VERSION" "$TODAY" "$note" <<'PYEOF'
import os, sys
path, pid, pver, today, note = sys.argv[1:6]
header = ("# %s pack -- CHANGELOG\n"
          "\n"
          "Pack versioning history (B3). Newest first. Version axis is the pack's own\n"
          "semver, independent of the framework release and the knowledge snapshot pin.\n"
          % pid)
new_entry = "## %s -- %s\n- %s\n" % (pver, today, note)
if os.path.isfile(path):
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    # find the first entry heading ("## ") -- everything before it is the header
    first = None
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            first = i; break
    if first is None:
        entries_text = ""
    else:
        entries_text = "\n".join(lines[first:])
    # split existing entries into ("## <ver> ...", body) blocks
    blocks = []
    cur = None
    for ln in entries_text.split("\n"):
        if ln.startswith("## "):
            if cur is not None:
                blocks.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur is not None:
        blocks.append(cur)
    def block_ver(b):
        # "## <ver> -- <date>" -> "<ver>"
        h = b[0][3:].strip()
        return h.split(" ", 1)[0].strip()
    kept = []
    replaced = False
    for b in blocks:
        btext = "\n".join(b).rstrip("\n")
        if not btext.strip():
            continue
        if block_ver(b) == pver:
            # same version: replace with the fresh entry (dedupe)
            kept.append(new_entry.rstrip("\n"))
            replaced = True
        else:
            kept.append(btext)
    if not replaced:
        kept.insert(0, new_entry.rstrip("\n"))
    out = header + "\n" + "\n\n".join(kept) + "\n"
    prior = len(kept) - 1
    print("changelog: %s@%s %s (%d prior entr%s preserved)"
          % (pid, pver, "replaced" if replaced else "prepended",
             prior, "y" if prior == 1 else "ies"))
else:
    out = header + "\n" + new_entry
    print("changelog: %s@%s new file" % (pid, pver))
open(path, "w", encoding="utf-8").write(out)
PYEOF
_log "STEP 4: wrote CHANGELOG.md ($PACK_VERSION, newest-first prepend)"

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
# I3 (DGN-441): exclude .source-sync explicitly. finalize does NOT regenerate
# .source-sync, so the existing file is on disk during checksum generation; the
# STEP 6 ordering that used to keep it out no longer suffices under finalize.
( cd "$PACKAGE_DIR" && find . -type f ! -name checksums.sha ! -name .source-sync \
    | sed 's|^\./||' \
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
#
# SNAPSHOT ONLY (DGN-441 F3/I1): finalize must NOT regenerate .source-sync --
# doing so would reset the drift baseline and hide intentional divergence. The
# finalize drift report (F4 above) reads the existing baseline non-destructively.
SYNC_FILE="$PACKAGE_DIR/.source-sync"
if [[ "$MODE" == "snapshot" ]]; then
if [[ ${#SECTIONS[@]} -gt 0 && -f "$SRC_AGENT" ]]; then
  # W-A: reuse the single-source extractor helper so snapshot baseline hashing
  # and the finalize drift report share ONE extraction code path.
  python3 - "$LIB_DIR/extract_section.py" "$SRC_AGENT" "$SYNC_FILE" "$TODAY" "$PACK_ID" "$PACK_VERSION" "${SECTIONS[@]}" <<'PYEOF'
import sys, importlib.util
helper_path, src, out, today, pid, pver = sys.argv[1:7]
headings = sys.argv[7:]
spec = importlib.util.spec_from_file_location("extract_section", helper_path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
content = open(src, encoding="utf-8").read()
rows = []
for h in headings:
    sha, text = mod.section_sha(content, h)
    if sha is None: continue
    rows.append((sha, h, today))
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
else
  _log "STEP 6: .source-sync preserved (finalize does not rewrite the drift baseline)"
fi

# ===========================================================================
# STEP 4 (register, part 2): catalog.json upsert (AFTER gates pass)
# ===========================================================================
python3 - "$CATALOG_FILE" "$PACK_ID" "$PACK_VERSION" "$MODE" "${CATALOG_FIELDS_IN:-}" <<'PYEOF'
import json, os, sys
catalog, pid, pver, mode, fields_in = sys.argv[1:6]
if os.path.isfile(catalog):
    cat = json.load(open(catalog))
else:
    cat = {"version": 1, "packs": []}
packs = cat.setdefault("packs", [])
# locate an existing row for this pack (status preservation, OQ4/F7)
existing = None; existing_idx = None
for i, p in enumerate(packs):
    if p.get("id") == pid:
        existing = p; existing_idx = i; break
# fields-in fragment (may override name_ko/en, tagline, role prose, status, ...)
fields = {}
if fields_in and os.path.isfile(fields_in):
    fields = json.load(open(fields_in))
# status resolution priority (OQ4/F7 -- NO setdefault official):
#   1. --catalog-fields-in explicit status
#   2. existing catalog row status
#   3. (no existing row) mode default: snapshot=official, finalize=draft
if "status" in fields:
    status = fields["status"]
elif existing is not None and "status" in existing:
    status = existing["status"]
else:
    status = "official" if mode == "snapshot" else "draft"
# build the merged entry: start from the existing row (preserve other fields),
# overlay the fields-in fragment, then force authoritative + resolved values.
entry = dict(existing) if existing is not None else {}
entry.update(fields)
entry["id"] = pid
entry["package_dir"] = pid
entry["pack_version"] = pver
entry["status"] = status
if existing_idx is not None:
    packs[existing_idx] = entry
else:
    packs.append(entry)
json.dump(cat, open(catalog, "w"), ensure_ascii=False, indent=2)
open(catalog, "a").write("\n")
print("catalog: upserted %s@%s (status=%s, mode=%s)" % (pid, pver, status, mode))
PYEOF
_log "STEP 4: catalog.json upserted"

_log "=== publish DONE mode=$MODE pack=$PACK_ID v$PACK_VERSION -> $PACKAGE_DIR ==="
if [[ "$MODE" == "finalize" ]]; then
  # F8: rehearsal guidance (Part B gate) -- log line only, no execution here.
  _log "F8: next (Part B, gated): install rehearsal on the re-sealed payload -- pack_install NM3 round-trip must stay green (payload unchanged, re-seal only)"
else
  _log "next (Part B, gated): sandbox install rehearsal (mint + pack_install full chain) -> release"
fi
