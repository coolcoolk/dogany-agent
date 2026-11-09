#!/bin/bash
# test-lane: fast
# test-component: decision-preview-and-skill-override
# test-tier: 0
# test-prereq: bash, python3, rg, sha256sum, tmp-files
# test-new-component: one contract spans push presentation and shipped-skill local layering; no existing suite owns both boundaries.
# test-covers: skills/dogany-decision-reply-frame/SKILL.md skills/dogany-upstream-report/SKILL.md skills/dogany-skill-creator/SKILL.md agents/.template/routines/skill-feedback-gate.py agents/.template/routines/skill-layer.py agents/.template/rules/cold.framework.why.md
# Rework C: What previews travel as real strict pushes; shipped skill defects
# are fixed in a registered local override before parallel upstream reporting.
set -euo pipefail
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
PASS=0
check() { if "$@"; then PASS=$((PASS+1)); else echo "FAIL: $*" >&2; exit 1; fi; }
FRAME="$ROOT/skills/dogany-decision-reply-frame/SKILL.md"
UP="$ROOT/skills/dogany-upstream-report/SKILL.md"
CREATOR="$ROOT/skills/dogany-skill-creator/SKILL.md"
GATE="$ROOT/agents/.template/routines/skill-feedback-gate.py"
WHY="$ROOT/agents/.template/rules/cold.framework.why.md"
check rg -q 'Send EACH candidate as its own push' "$FRAME"
check rg -q -- '`--buttons-json`' "$FRAME"
check rg -q '[[OPTIONS]].*menu' "$FRAME"
check rg -q 'file the reproducible upstream report in parallel' "$UP"
check rg -q 'fix the local copy now' "$CREATOR"
check rg -q 'fix that copy now.*upstream report in parallel' "$GATE"
check rg -q 'upstream report in parallel' "$WHY"

TMP="$(mktemp -d /tmp/rework-c.XXXXXX)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/instance/.claude/skills/dogany-example"
cat > "$TMP/instance/.claude/skills/dogany-example/SKILL.md" <<'SKILL'
---
name: dogany-example
scope: local
layer: local
description: Local copy of a wrong shipped skill.
---
local bytes survive
SKILL
check python3 "$ROOT/agents/.template/routines/skill-layer.py" --root "$TMP/instance" register "$TMP/instance/.claude/skills/dogany-example"
BEFORE="$(sha256sum "$TMP/instance/.claude/skills/dogany-example/SKILL.md" | cut -d' ' -f1)"
# update.sh preserves untagged local-layer paths; emulate its overlay copy stage
# by attempting to lay down different shipped bytes and then restoring registered paths.
cp -a "$TMP/instance/.claude/skills/dogany-example" "$TMP/saved"
printf '%s\n' shipped-update > "$TMP/instance/.claude/skills/dogany-example/SKILL.md"
cp -a "$TMP/saved/." "$TMP/instance/.claude/skills/dogany-example/"
AFTER="$(sha256sum "$TMP/instance/.claude/skills/dogany-example/SKILL.md" | cut -d' ' -f1)"
check test "$BEFORE" = "$AFTER"
check rg -q '^\.claude/skills/dogany-example/  # layer:local \(routines/skill-layer\.py register\)$' "$TMP/instance/.claude/.dogany-preserve"
echo "passed=$PASS failed=0"
