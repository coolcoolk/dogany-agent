#!/usr/bin/env bash
# steps.sh -- rule-lane carrier for dogany-skill-creator (lanes: in product.yaml).
# Thin dispatcher: real logic lives in routines/skill-landing-gate.py and
# routines/skill-layer.py; this file only gives lane-lint an in-unit carrier.
#
#   steps.sh declare-check SKILL_DIR  s2 gate: probe the landing gate with the
#                                     would-be SKILL.md write (exit 1 = deny)
#   steps.sh register SKILL_DIR       s6: register a layer=local skill
#   steps.sh final-lint SKILL_DIR     s8: layer lint + real-data shape scan
#
# ROOT = instance root: $DOGANY_ROOT, else cwd. Exit 0 ok / 1 finding / 2 usage.
set -uo pipefail

ROOT="${DOGANY_ROOT:-$PWD}"
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")")" && pwd)"

routine() {  # $1 = routine file name; instance copy first, then walk up from here
  local d="$HERE"
  [ -f "$ROOT/routines/$1" ] && { echo "$ROOT/routines/$1"; return 0; }
  while [ "$d" != "/" ]; do
    [ -f "$d/routines/$1" ] && { echo "$d/routines/$1"; return 0; }
    d="$(dirname "$d")"
  done
  echo "steps.sh: routines/$1 not found (set DOGANY_ROOT)" >&2
  return 2
}

fm() {  # $1 = SKILL.md, $2 = frontmatter key
  awk -v k="$2" 'NR==1 && $0!="---"{exit} NR>1 && $0=="---"{exit}
    NR>1 && index($0, k":")==1 {sub(/^[^:]*:[ \t]*/, ""); print; exit}' "$1" 2>/dev/null
}

cmd="${1:-}"; dir="${2:-}"
[ -n "$cmd" ] && [ -n "$dir" ] || { sed -n '6,9p' "$0"; exit 2; }
dir="$(cd "$dir" 2>/dev/null && pwd || echo "$dir")"

case "$cmd" in
  declare-check)
    gate="$(routine skill-landing-gate.py)" || exit 2
    sentinel="$ROOT/.claude/.skill-landing-declared"
    loc="$(sed -n 's/^location:[ \t]*//p' "$sentinel" 2>/dev/null | head -n1)"
    out="$(python3 - "$dir/SKILL.md" "$loc" "$ROOT" <<'PY' | python3 "$gate"
import json, sys
path, loc, root = sys.argv[1:4]
body = "---\nname: x\nscope: %s\n---\n" % loc
print(json.dumps({"tool_name": "Write", "cwd": root,
                  "tool_input": {"file_path": path, "content": body}}))
PY
)"
    if printf '%s' "$out" | grep -q '"permissionDecision": "deny"'; then
      echo "declare-check: DENY"
      printf '%s\n' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["permissionDecisionReason"])'
      exit 1
    fi
    [ -n "$out" ] && printf '%s\n' "$out"
    echo "declare-check: allow ($dir, location=${loc:-n/a})"
    ;;
  register)
    layer="$(fm "$dir/SKILL.md" layer)"
    [ "$layer" = "local" ] || { echo "register: refused -- layer is '${layer:-missing}', register runs for layer: local only"; exit 2; }
    python3 "$(routine skill-layer.py)" --root "$ROOT" register "$dir"
    ;;
  final-lint)
    rc=0
    python3 "$(routine skill-layer.py)" --root "$ROOT" lint --skill "$dir" || rc=1
    # Real-data SHAPE scan: e-mail, phone number, home path with a real user
    # name. Mechanical floor only; names and case content stay review work.
    hits="$(grep -rnIE \
      -e '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' \
      -e '(\+[0-9]{1,3}[ -]?)?\b0?1[016789][ -.]?[0-9]{3,4}[ -.]?[0-9]{4}\b' \
      -e '/(Users|home)/[A-Za-z0-9._-]+/' \
      --exclude=steps.sh "$dir" 2>/dev/null || true)"
    if [ -n "$hits" ]; then
      echo "final-lint: FAIL real-data shape (use placeholders like <USER_NAME>):"
      printf '%s\n' "$hits" | sed 's/^/  /'
      rc=1
    fi
    [ "$rc" = 0 ] && echo "final-lint: clean ($dir)"
    exit "$rc"
    ;;
  *) sed -n '6,9p' "$0"; exit 2 ;;
esac
