#!/bin/bash
# DGN-238 OQ-F (RESOLVED 2026-07-13): kimwog knowledge warehouse consumption =
# SNAPSHOT COPY @ RELEASE PIN. The warehouse is portable by design (all tools
# take --root), so the snapshot is a plain directory copy plus a pin record.
#
# Copies <warehouse-src> -> <warg-root>/knowledge/kimwog and writes
# <warg-root>/knowledge/kimwog/.snapshot-pin (latest CHANGELOG release id +
# snapshot date + source). Idempotent: re-run overwrites the snapshot and
# refreshes the pin.
#
# COPIED  : registry.yaml lanes.yaml axes.yaml CHANGELOG.yaml REFRACTION.md
#           README.md GAPS.md items/ e/ tools/ instance/tools/ (+ instance
#           README/.gitignore -- instance-side CODE and docs).
# EXCLUDED: tests/, __pycache__/, *.pyc, .git* (build/dev artifacts) AND the
#           instance-side USER DATA (instance/touched-set.yaml,
#           instance/registry.yaml, instance/e/, instance/proposals/,
#           instance/.instance.conf). DGN-120: conversational memory is
#           user-owned; a fresh mint starts with an EMPTY instance side (the
#           instance tools create their files on first use). The excluded
#           instance paths are also PROTECTED from --delete on re-run, so a
#           live Warg's own accreted instance data survives a snapshot refresh.
# PROTECTED (never overwritten/deleted, seeded if absent):
#           GAPS-instance.md -- the instance-local gap log the design-layer
#           skills append to on a warehouse miss (warehouse accretion queue).
#
# Usage: knowledge-snapshot.sh <warg-root> [<warehouse-src>]
#        <warehouse-src> must be supplied explicitly. The kimwog warehouse
#        lives on the publisher instance; pack_install.sh resolves it from
#        the manifest 'knowledge.source' field (instance-local path configured
#        at mint time -- see KNOWLEDGE-WIRING.md for the design proposal).
set -euo pipefail

WARG_ROOT="${1:?usage: knowledge-snapshot.sh <warg-root> <warehouse-src>}"
SRC="${2:?usage: knowledge-snapshot.sh <warg-root> <warehouse-src> (source must be supplied)}"
DEST="$WARG_ROOT/knowledge/kimwog"

[ -d "$SRC" ] || { echo "ERROR: warehouse source not found: $SRC" >&2; exit 1; }
[ -f "$SRC/CHANGELOG.yaml" ] || { echo "ERROR: not a kimwog warehouse (no CHANGELOG.yaml): $SRC" >&2; exit 1; }
[ -d "$WARG_ROOT" ] || { echo "ERROR: warg root not found: $WARG_ROOT" >&2; exit 1; }

mkdir -p "$DEST"

# rsync --delete keeps re-runs clean; excluded patterns are NOT deleted on the
# receiver (no --delete-excluded), which protects instance user data and the
# gap log across refreshes.
rsync -a --delete \
  --exclude='/tests/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='.git*' \
  --exclude='/.snapshot-pin' \
  --exclude='/GAPS-instance.md' \
  --exclude='/instance/touched-set.yaml' \
  --exclude='/instance/registry.yaml' \
  --exclude='/instance/e/' \
  --exclude='/instance/proposals/' \
  --exclude='/instance/.instance.conf' \
  "$SRC/" "$DEST/"

# fresh (empty) instance-side data dirs -- the tools expect the root to exist
mkdir -p "$DEST/instance/e" "$DEST/instance/proposals"

# seed the instance gap log once (append target for the skills' absence path)
if [ ! -f "$DEST/GAPS-instance.md" ]; then
  {
    echo "# Kimwog instance gap log (Warg)"
    echo "# Design-layer skills append one line per warehouse miss."
    echo "# Feeds the next warehouse collection sprint (see GAPS.md for the publisher-side queue)."
  } > "$DEST/GAPS-instance.md"
fi

# pin: latest release id = first 'version:' entry in CHANGELOG.yaml (newest first)
RELEASE="$(grep -m1 -E '^[[:space:]]*-[[:space:]]*version:' "$SRC/CHANGELOG.yaml" \
  | sed -E 's/^[[:space:]]*-[[:space:]]*version:[[:space:]]*//' | tr -d '"' | tr -d "'")"
[ -n "$RELEASE" ] || { echo "ERROR: could not parse latest release id from CHANGELOG.yaml" >&2; exit 1; }

{
  echo "warehouse: kimwog"
  echo "release: $RELEASE"
  echo "snapshot_date: $(date +%F)"
  echo "source: $SRC"
} > "$DEST/.snapshot-pin"

echo "kimwog snapshot -> $DEST (release $RELEASE, $(date +%F))"
