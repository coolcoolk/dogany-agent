#!/bin/bash
# knowledge_selftest.sh -- DGN-402 knowledge wiring gates (mechanical,
# zero-model, deterministic).
#
# Single home for the gate logic: pack_install.sh runs it as STEP 7c at
# install time; the agent-crafting phase 2 checklist re-runs the same script
# to catch post-install drift.
#
# Usage: knowledge_selftest.sh <root> --manifest <pack-manifest.json>
#
# With a manifest 'knowledge' object: runs gates G1-G4.
#   G1 delivery   -- snapshot dir + .snapshot-pin parse; release-drift check
#                    only when the publisher source dir exists on this
#                    machine (same-machine publisher).
#   G2 discovery  -- AGENT.md KNOWLEDGE-WIRING-POINTER marker + the pointer
#                    names the manifest warehouse path.
#   G3 discovery  -- config/agent.conf KNOWLEDGE_WAREHOUSE key cross-checked
#                    against manifest + disk, and the canonical
#                    dogany-memory-search conditional warehouse line.
#                    NOTE: a canonical-line FAIL on a framework older than
#                    the release carrying that line means "self-update
#                    first", not a wiring defect.
#   G4 refraction -- per-consumer-skill consumption block + manifest<->skill
#                    domain cross-grep + unrendered mint-token residue (hard
#                    FAIL) + per-turn declaration markers + one deterministic
#                    refract_cli.py smoke run.
# Without a 'knowledge' object (warehouse-less pack): runs the inverse check
# instead -- zero warehouse artifacts (no KNOWLEDGE_WAREHOUSE key, no
# KNOWLEDGE-WIRING-POINTER marker, no knowledge/ directory).
#
# G5 (live probes) is MANUAL -- this script only prints a reminder line.
#
# Exit 0 = all applicable mechanical checks PASS; nonzero = at least one FAIL.
set -uo pipefail

usage() { echo "usage: knowledge_selftest.sh <root> --manifest <pack-manifest.json>" >&2; exit 2; }

ROOT="${1:-}"
[[ -n "$ROOT" ]] || usage
shift
MANIFEST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$MANIFEST" ]] || usage
[[ -d "$ROOT" ]] || { echo "ERROR: root not found: $ROOT" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "ERROR: manifest not found: $MANIFEST" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 2; }

FAILED=0
_pass() { echo "PASS: $1"; }
_fail() { echo "FAIL: $1"; FAILED=1; }

# ---------- manifest knowledge object ---------------------------------------
KNOWLEDGE_DECL="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if isinstance(d.get('knowledge'), dict) else 0)" "$MANIFEST")"

# ---------- inverse check (warehouse-less pack) ------------------------------
if [[ "$KNOWLEDGE_DECL" -eq 0 ]]; then
  # Scope guard (DGN-402 OQ-2 ruling): the inverse check asserts a wholly
  # warehouse-less INSTANCE. When another pack already owns a warehouse
  # (KNOWLEDGE_WAREHOUSE key set in config/agent.conf AND the named
  # knowledge/<name>/ directory verified on disk), a warehouse-less pack
  # install must not false-FAIL on that pack's artifacts -- skip explicitly,
  # naming the verified warehouse. Key present but directory missing =
  # stale key -> do NOT skip; the inverse check runs and flags it.
  W_EXISTING="$(sed -n 's/^KNOWLEDGE_WAREHOUSE=//p' "$ROOT/config/agent.conf" 2>/dev/null | head -1)"
  if [[ -n "$W_EXISTING" && -d "$ROOT/knowledge/$W_EXISTING" ]]; then
    echo "SKIPPED: inverse check -- instance already has warehouse '$W_EXISTING' (verified on disk, owned by another pack); warehouse-less pack adds no wiring to verify"
    echo "== result: PASS =="
    exit 0
  fi
  echo "== inverse check (no manifest knowledge object -- warehouse-less pack) =="
  ok=1
  if grep -q '^KNOWLEDGE_WAREHOUSE=' "$ROOT/config/agent.conf" 2>/dev/null; then
    _fail "inverse: KNOWLEDGE_WAREHOUSE key present in config/agent.conf"
    ok=0
  fi
  if grep -q 'KNOWLEDGE-WIRING-POINTER' "$ROOT/AGENT.md" 2>/dev/null; then
    _fail "inverse: KNOWLEDGE-WIRING-POINTER marker present in AGENT.md"
    ok=0
  fi
  if [[ -d "$ROOT/knowledge" ]]; then
    _fail "inverse: knowledge/ directory present"
    ok=0
  fi
  [[ "$ok" -eq 1 ]] && _pass "inverse check: zero warehouse artifacts"
  echo "== result: $([[ "$FAILED" -eq 0 ]] && echo PASS || echo FAIL) =="
  exit "$FAILED"
fi

# ---------- knowledge fields -------------------------------------------------
_know_str() {
  python3 -c "import json,sys; k=json.load(open(sys.argv[1])).get('knowledge') or {}; v=k.get(sys.argv[2]); print(v if isinstance(v,str) else '')" "$MANIFEST" "$1"
}
W="$(_know_str warehouse)"
KNOW_SOURCE="$(_know_str source)"
KNOW_SOURCE="${KNOW_SOURCE/#\~/$HOME}"
SMOKE_ITEM="$(_know_str smoke_item)"
SMOKE_ARGS="$(_know_str smoke_args)"
CONSUMER_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
k = json.load(open(sys.argv[1])).get("knowledge") or {}
for skill, domains in (k.get("consumer_skills") or {}).items():
    if not isinstance(domains, list):
        domains = []
    print("%s\t%s" % (skill, " ".join(str(d) for d in domains)))
PYEOF
)"
TURN_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
k = json.load(open(sys.argv[1])).get("knowledge") or {}
for t in k.get("turns") or []:
    if isinstance(t, dict):
        print("%s\t%s" % (t.get("type", ""), t.get("home", "")))
PYEOF
)"

[[ -n "$W" ]] || { echo "ERROR: manifest knowledge.warehouse is empty" >&2; exit 2; }
echo "== knowledge wiring gates G1-G4 (warehouse: $W) =="

# ---------- G1: delivery -----------------------------------------------------
PIN="$ROOT/knowledge/$W/.snapshot-pin"
if [[ -d "$ROOT/knowledge/$W" ]]; then
  _pass "G1: knowledge/$W/ exists"
else
  _fail "G1: knowledge/$W/ missing"
fi
if [[ -f "$PIN" ]] && grep -q '^release:' "$PIN"; then
  _pass "G1: .snapshot-pin present and release parses"
else
  _fail "G1: .snapshot-pin missing or no 'release:' field"
fi
# drift check -- CONDITIONAL on same-machine publisher: knowledge.source dir
# exists on this machine -> pin release must equal publisher CHANGELOG.yaml
# head; source dir absent -> pin-parse only.
if [[ -n "$KNOW_SOURCE" && -d "$KNOW_SOURCE" && -f "$KNOW_SOURCE/CHANGELOG.yaml" ]]; then
  PIN_RELEASE="$(sed -n 's/^release:[[:space:]]*//p' "$PIN" 2>/dev/null | head -1)"
  PUB_RELEASE="$(grep -m1 -E '^[[:space:]]*-[[:space:]]*version:' "$KNOW_SOURCE/CHANGELOG.yaml" \
    | sed -E 's/^[[:space:]]*-[[:space:]]*version:[[:space:]]*//' | tr -d '"' | tr -d "'")"
  if [[ -n "$PIN_RELEASE" && "$PIN_RELEASE" == "$PUB_RELEASE" ]]; then
    _pass "G1: pin release ($PIN_RELEASE) matches publisher CHANGELOG head"
  else
    _fail "G1: pin release ('$PIN_RELEASE') != publisher CHANGELOG head ('$PUB_RELEASE') -- drift"
  fi
else
  echo "note: G1 drift check skipped (publisher source dir not on this machine) -- pin-parse only"
fi

# ---------- G2/G3: discovery (4-way cross: conf <-> AGENT.md <-> disk <-> manifest)
W_CONF="$(sed -n 's/^KNOWLEDGE_WAREHOUSE=//p' "$ROOT/config/agent.conf" 2>/dev/null | head -1)"

if grep -q 'KNOWLEDGE-WIRING-POINTER' "$ROOT/AGENT.md" 2>/dev/null; then
  _pass "G2: AGENT.md carries the KNOWLEDGE-WIRING-POINTER marker"
else
  _fail "G2: KNOWLEDGE-WIRING-POINTER marker missing in AGENT.md"
fi
if grep -q "knowledge/$W" "$ROOT/AGENT.md" 2>/dev/null; then
  _pass "G2: AGENT.md pointer names knowledge/$W"
else
  _fail "G2: AGENT.md does not reference knowledge/$W"
fi

if [[ -n "$W_CONF" ]]; then
  _pass "G3: KNOWLEDGE_WAREHOUSE key set (value: $W_CONF)"
else
  _fail "G3: KNOWLEDGE_WAREHOUSE key missing/empty in config/agent.conf"
fi
if [[ "$W_CONF" == "$W" ]]; then
  _pass "G3: conf value matches manifest warehouse"
else
  _fail "G3: conf value ('$W_CONF') != manifest warehouse ('$W')"
fi
if [[ -n "$W_CONF" && -d "$ROOT/knowledge/$W_CONF" ]]; then
  _pass "G3: conf value resolves on disk (knowledge/$W_CONF/)"
else
  _fail "G3: conf value does not resolve to a knowledge/ dir on disk"
fi
if grep -q 'KNOWLEDGE_WAREHOUSE' "$ROOT/.claude/skills/dogany-memory-search/SKILL.md" 2>/dev/null; then
  _pass "G3: canonical dogany-memory-search carries the conditional warehouse line"
else
  _fail "G3: canonical warehouse line missing in dogany-memory-search (on an old framework this means 'self-update first', not a wiring defect)"
fi

# ---------- G4: refraction ---------------------------------------------------
if [[ -z "$CONSUMER_LINES" ]]; then
  _fail "G4: manifest knowledge.consumer_skills is empty"
fi
while IFS=$'\t' read -r skill domains; do
  [[ -n "$skill" ]] || continue
  S="$ROOT/.claude/skills-bundle/$skill/SKILL.md"
  if [[ ! -f "$S" ]]; then
    _fail "G4: $skill -- SKILL.md missing at .claude/skills-bundle/$skill/"
    continue
  fi
  if grep -q '^## knowledge warehouse consumption' "$S"; then
    _pass "G4: $skill -- consumption block present"
  else
    _fail "G4: $skill -- '## knowledge warehouse consumption' block missing"
  fi
  for d in $domains; do
    if grep -q "$d" "$S"; then
      _pass "G4: $skill -- names manifest domain '$d'"
    else
      _fail "G4: $skill -- manifest domain '$d' not named (manifest<->skill drift)"
    fi
  done
  # unrendered-token check: narrow 7-token alternation, hard FAIL.
  # false-positive risk = 0 (these 7 are the only tokens the render pipeline
  # substitutes; no legitimate __TOKEN__ literals exist in pack skill files).
  # CROSS-REF: token list appears in four places that must stay in sync:
  #   (1) mint.sh sanity check (~L504 alternation)
  #   (2) update.sh subst_one (~L1053-1066)
  #   (3) pack_install.sh _subst_mint_tokens
  #   (4) this check (G4, knowledge_selftest.sh)
  # When adding a token, update all four sites and their cross-ref comments.
  if grep -qE '__(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|AGENT_PREFIX|HOME|AGENT_LANG)__' "$S"; then
    _fail "G4: $skill -- unrendered mint token residue in SKILL.md"
  else
    _pass "G4: $skill -- no unrendered mint token residue"
  fi
done <<< "$CONSUMER_LINES"

# per-turn declaration checks (T2/T3 markers verify DECLARATION only; actual
# consumption behavior is verified by the manual G5 live probes)
if [[ -z "$TURN_LINES" ]]; then
  _fail "G4: manifest knowledge.turns is empty"
fi
while IFS=$'\t' read -r ttype thome; do
  [[ -n "$ttype$thome" ]] || continue
  H="$ROOT/$thome"
  if [[ ! -f "$H" ]]; then
    _fail "G4: turn $ttype -- home file missing: $thome"
    continue
  fi
  case "$ttype" in
    T1)
      if grep -q '^## knowledge warehouse consumption' "$H"; then
        _pass "G4: turn T1 declared in $thome"
      else
        _fail "G4: turn T1 -- consumption heading missing in $thome"
      fi
      ;;
    T2|T3)
      if grep -q "KNOWLEDGE-TURN-$ttype" "$H"; then
        _pass "G4: turn $ttype declared in $thome (marker)"
      else
        _fail "G4: turn $ttype -- KNOWLEDGE-TURN-$ttype marker missing in $thome"
      fi
      ;;
    *)
      _fail "G4: unknown turn type '$ttype' (manifest error)"
      ;;
  esac
done <<< "$TURN_LINES"

# smoke (deterministic, zero-model; rescale-free item preferred -- items
# needing measured values fail on a fresh mint and must not be smoke_item)
REFRACT_CLI="$ROOT/knowledge/$W/tools/refract_cli.py"
if [[ -z "$SMOKE_ITEM" ]]; then
  _fail "G4: manifest knowledge.smoke_item is empty"
elif [[ ! -f "$REFRACT_CLI" ]]; then
  _fail "G4: refract_cli.py missing at knowledge/$W/tools/"
else
  # SMOKE_ARGS is word-split on purpose (manifest-authored extra args).
  # shellcheck disable=SC2086
  if python3 "$REFRACT_CLI" "$SMOKE_ITEM" $SMOKE_ARGS --now "$(date +%F)" >/dev/null 2>&1; then
    _pass "G4: refract smoke ($SMOKE_ITEM) exit 0"
  else
    _fail "G4: refract smoke ($SMOKE_ITEM) failed"
  fi
fi

# ---------- G5 reminder (manual) --------------------------------------------
echo "note: G5 live probes are MANUAL -- 2 probes with transcript/tool-log verification, agent-crafting phase 2 checklist (not covered by this script)"

echo "== result: $([[ "$FAILED" -eq 0 ]] && echo PASS || echo FAIL) =="
exit "$FAILED"
