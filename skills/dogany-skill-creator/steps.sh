#!/usr/bin/env bash
# steps.sh -- rule-lane carrier for dogany-skill-creator (lanes: in product.yaml).
# Thin dispatcher: real logic lives in routines/skill-landing-gate.py and
# routines/skill-layer.py; this file only gives lane-lint an in-unit carrier.
#
#   steps.sh declare-check SKILL_DIR  s2 gate: probe the landing gate with the
#                                     would-be SKILL.md write (exit 1 = deny)
#   steps.sh register SKILL_DIR       s6: register a layer=local skill
#   steps.sh final-lint SKILL_DIR     s8: layer lint + real-data shape scan
#                                     + no-user-data marker (advisory WARN)
#                                     + secret shapes + charset + mint-custom grammar
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
    out="$(python3 - "$dir/SKILL.md" "$loc" "$ROOT" <<'PY' | python3 "$gate" --declaration-only
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
  update-begin|update-check)
    shift 2
    python3 "$(routine skill-layer.py)" --root "$ROOT" "$cmd" --target "$dir/SKILL.md" "$@"
    exit "$?"
    ;;
  final-lint)
    rc=0
    # u4/s8: when a turn is supplied, verify the captured update against disk.
    # Existing new-skill callers retain all prior s8 checks.
    if [ -n "${3:-}" ] || [ -n "${4:-}" ]; then
      python3 "$(routine skill-layer.py)" --root "$ROOT" update-check \
        --target "$dir/SKILL.md" --session "${3:-}" --turn "${4:-}" --applied || rc=1
    fi
    python3 "$(routine skill-layer.py)" --root "$ROOT" update-audit --target "$dir/SKILL.md" || rc=1
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
    # DGN-667 option C: the author confirms the body-conventions review
    # (no real names / real case content in rule text) with frontmatter
    # 'lint: no-user-data'. Advisory: a missing marker warns, rc unchanged.
    if [ "$(fm "$dir/SKILL.md" lint)" != "no-user-data" ]; then
      echo "final-lint: WARN no 'lint: no-user-data' in SKILL.md frontmatter --" \
        "review rule/step examples (body conventions: placeholders only), then add it"
    fi
    # DGN-1910 spec 6 checks 3-5: ONE floor for a mint render and every later
    # edit. 3 = secret shapes (secret_scan body mode), 4 = charset (no Cc but
    # tab/newline, no Cf/Zl/Zp/Co/Cs), both over every text file in the dir;
    # 5 = only for frontmatter 'origin: mint-custom': the Steps grammar and a
    # Bounds section. Hand-made skills are not held to 5. Findings name the
    # file:line and the class, never the matched text.
    ss="$(routine lib/secret_scan.py)" || exit 2
    cs=""
    d="$HERE"
    while [ "$d" != "/" ]; do
      [ -f "$d/scripts/pack/lib/custom_skill.py" ] && { cs="$d/scripts/pack/lib/custom_skill.py"; break; }
      d="$(dirname "$d")"
    done
    # A deployed copy (a staff agent's own skill) is not under the framework
    # tree: its consume checkout, then DOGANY_CONSUME_ROOT, then the
    # framework repo every instance declares (.instance.conf DOGANY_REPO_ROOT).
    repo="$(sed -n 's/^DOGANY_REPO_ROOT=//p' "$ROOT/.instance.conf" 2>/dev/null | head -n1)"
    for c in "$ROOT/.dogany-consume" "${DOGANY_CONSUME_ROOT:-}" "$repo"; do
      [ -z "$cs" ] && [ -n "$c" ] && [ -f "$c/scripts/pack/lib/custom_skill.py" ] \
        && cs="$c/scripts/pack/lib/custom_skill.py"
    done
    python3 - "$dir" "$ss" "$cs" "$ROOT" <<'PY' || rc=1
import importlib.util, os, sys
skill_dir, ss_path, cs_path, root = sys.argv[1:5]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

ss = load("secret_scan", ss_path)
bad = []
for base, dirs, files in os.walk(skill_dir):
    dirs[:] = sorted(x for x in dirs if x != "__pycache__")
    for fn in sorted(files):
        p = os.path.join(base, fn)
        if os.path.islink(p):
            continue
        text, skip = ss.read_text(p)
        if skip:
            continue
        rel = os.path.relpath(p, skill_dir)
        seen = set()
        for h in ss.scan(text, "body"):
            k = ("secret shape " + h.cat, ss.line_of(text, h.start))
            if k not in seen:
                seen.add(k)
                bad.append("%s %s:%d" % (k[0], rel, k[1]))
        for off, cat in ss.bad_chars(text):
            k = ("charset " + cat, ss.line_of(text, off))
            if k not in seen:
                seen.add(k)
                bad.append("%s %s:%d" % (k[0], rel, k[1]))
md = os.path.join(skill_dir, "SKILL.md")
text, _ = ss.read_text(md) if os.path.isfile(md) else (None, None)
origin = None
if text:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for ln in lines[1:]:
            if ln.strip() == "---":
                break
            if ln.startswith("origin:"):
                origin = ln.split(":", 1)[1].strip().strip("'\"")
if origin == "mint-custom":
    if not cs_path:
        bad.append("mint-custom: scripts/pack/lib/custom_skill.py not found -- cannot hold the steps grammar")
    else:
        cs = load("custom_skill", cs_path)
        bad += ["mint-custom: " + f for f in cs.lint_rendered(text, cs.kit_names_of(root))]
if bad:
    print("final-lint: FAIL secret / charset / mint-custom floor (DGN-1910):")
    for b in bad:
        print("  " + b)
    sys.exit(1)
PY
    [ "$rc" = 0 ] && echo "final-lint: clean ($dir)"
    exit "$rc"
    ;;
  *) sed -n '6,9p' "$0"; exit 2 ;;
esac
