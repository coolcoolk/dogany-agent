#!/bin/bash
# kit_first_install.sh -- life kit first install for an already-minted
# instance (DGN-1817). The deterministic half of the dogany-lifekit-setup
# skill's install step 0 and of the kit-choice hook's fail-closed gate.
#
# Measured dead end (DGN-1817 repro): on a fresh main the owner picks "life"
# at the kit-select question, but no framework path installs the kit files
# afterwards (install.sh only seeds LIFEKIT=pending; /update only upgrades
# packs already recorded in DOGANY_PACKS). The setup skill then found
# bundle= empty and stopped -- after it had already written LIFEKIT=offered,
# so the one-shot offer was burned with nothing installed.
#
# This script adds NO installer. It resolves the kit source and hands the
# install to scripts/pack/pack_install.sh (catalog lane):
#   source  -- packs/catalog.json row <kit>.package_dir, through
#              lib/pack_coords.sh (the one coordinate rule). When that
#              sibling checkout is absent (a public install) and the row
#              carries source_url (DGN-1818), the source is that public
#              remote: probe reads its tags with `git ls-remote` (no write),
#              install hands pack_install.sh the same catalog and it clones
#              into <shared home>/pack-src/<id> (pack_coords.sh
#              pack_source_fetch, the one clone writer). Remote unreachable
#              (offline, DNS, auth) = kind=network; reachable but no stable
#              pack/<id>/vX.Y.Z tag = kind=no_version.
#   tag     -- pack_install.sh's own DGN-1489 source-tag gate admits the
#              catalog lane only when pack/<id>/v<version> exists, and the
#              DGN-1698 sealed-tree step then installs the tag's bytes.
#              `probe` checks the same predicate up front (read-only).
#
# Usage:
#   kit_first_install.sh probe   <agent-root> [--kit <id>] [--catalog <file>]
#   kit_first_install.sh install <agent-root> [--catalog <file>]
#
# probe   read-only: can <kit> (default kit_life) be installed from its
#         catalog source? stdout KEY=value lines:
#           source=<dir|url>  tag=<ref>         rc 0 (installable)
#           kind=no_version|network  unreachable=<reason>   rc 3
# install life kit only. bundle= already non-empty -> no install. Else probe,
#         then pack_install.sh <slug> <root> --pack kit_life --no-start, then
#         re-read bundle= (routines/lib/life_engine.py). Only when bundle= is
#         non-empty: kit conf LIFEKIT=pending -> offered (the one-shot offer
#         is spent only by a real install; any other LIFEKIT value is left as
#         it is). Any failure leaves LIFEKIT untouched. stdout:
#           installed=0|1  bundle=<path>  lifekit=<value>   rc 0
#           kind=no_version|network  unreachable=<reason>   rc 3
#           error=<reason>                                  rc 4
#
# Catalog: --catalog, else $DOGANY_PACK_CATALOG, else the instance's default
# (lib/canonical_catalog.sh -- the canonical repo's catalog when the
# instance records another DOGANY_REPO_ROOT). The same file is passed to
# pack_install.sh, so probe and install can never read two catalogs.
#
# rc 2 = usage error. ASCII-only output: the skill speaks to the owner in
# the owner's language; reasons here are machine text.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=lib/py_resolve.sh
. "$SCRIPT_DIR/lib/py_resolve.sh"
pack_resolve_py3 || exit 1
# shellcheck source=lib/pack_coords.sh
. "$SCRIPT_DIR/lib/pack_coords.sh"
# shellcheck source=lib/pack_commit_gate.sh
. "$SCRIPT_DIR/lib/pack_commit_gate.sh"
# shellcheck source=lib/canonical_catalog.sh
. "$SCRIPT_DIR/lib/canonical_catalog.sh"

LIFE_KIT=kit_life

usage() {
  echo "usage: kit_first_install.sh probe <agent-root> [--kit <id>] [--catalog <file>]" >&2
  echo "       kit_first_install.sh install <agent-root> [--catalog <file>]" >&2
  exit 2
}

VERB="${1:-}"
ROOT="${2:-}"
[[ "$VERB" == "probe" || "$VERB" == "install" ]] || usage
[[ -n "$ROOT" && -d "$ROOT" ]] || usage
ROOT="$(cd "$ROOT" && pwd)"
shift 2
KIT="$LIFE_KIT"
CATALOG="${DOGANY_PACK_CATALOG:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kit)     [[ "$VERB" == "probe" && -n "${2:-}" ]] || usage; KIT="$2"; shift 2 ;;
    --catalog) [[ -n "${2:-}" ]] || usage; CATALOG="$2"; shift 2 ;;
    *) usage ;;
  esac
done
if [[ -z "$CATALOG" ]]; then
  CATALOG="$(pack_default_catalog_for_instance "$REPO_DIR" "$ROOT" "kit_first_install" 2>/dev/null)" \
    || { echo "kind=no_version"; echo "unreachable=canonical catalog missing (DOGANY_REPO_ROOT in .instance.conf)"; exit 3; }
fi

# probe_remote <url> -- public-install lane (DGN-1818): read the remote's tags
# without writing anything. The tag rule is pack_source_fetch's (newest
# STABLE pack/<id>/vX.Y.Z, never a pre-release), so probe and the install
# that follows name the same tag. Sets SRC_DIR=<url> / SRC_TAG.
probe_remote() {
  local url="$1" refs
  if ! refs="$(GIT_TERMINAL_PROMPT=0 GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=20 \
                 git ls-remote --tags "$url" 2>&1)"; then
    echo "kind=network"
    echo "unreachable=cannot reach $url ($(printf '%s' "$refs" | tail -n 1 | tr -cd '[:print:]' | cut -c1-160))"
    return 3
  fi
  SRC_TAG="$(printf '%s\n' "$refs" | awk '{print $2}' \
    | sed -n "s|^refs/tags/\(pack/$KIT/v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$|\1|p" | sort -V | tail -n 1)"
  [[ -n "$SRC_TAG" ]] || { echo "kind=no_version"; echo "unreachable=no stable pack/$KIT/vX.Y.Z tag at $url"; return 3; }
  SRC_DIR="$url"
  return 0
}

# probe_source -> sets SRC_DIR / SRC_TAG, or prints unreachable= and returns 3.
probe_source() {
  local row pd mf ver top url
  [[ -f "$CATALOG" ]] || { echo "kind=no_version"; echo "unreachable=no catalog at $CATALOG"; return 3; }
  row="$(pack_catalog_row "$CATALOG" "$KIT" 2>/dev/null)" || row="null"
  [[ -n "$row" && "$row" != "null" ]] || { echo "kind=no_version"; echo "unreachable=no $KIT row in $CATALOG"; return 3; }
  pd="$(pack_row_field "$row" package_dir)"
  [[ -n "$pd" ]] || { echo "kind=no_version"; echo "unreachable=$KIT row has no package_dir"; return 3; }
  SRC_DIR="$(pack_coord_resolve "$CATALOG" "$pd")"
  mf="$SRC_DIR/pack-manifest.json"
  if [[ ! -f "$mf" ]]; then
    url="$(pack_row_field "$row" source_url)"
    [[ -n "$url" ]] || { echo "kind=no_version"; echo "unreachable=package_dir has no pack-manifest.json ($SRC_DIR)"; return 3; }
    probe_remote "$url"
    return $?
  fi
  # Version: catalog row first, manifest fallback (pack_install.sh order).
  ver="$(pack_row_field "$row" pack_version)"
  if [[ -z "$ver" ]]; then
    ver="$("$PACK_PY3" -c "import json,sys; v=json.load(open(sys.argv[1])).get('pack_version'); print(v if isinstance(v,str) else '')" "$mf" 2>/dev/null || true)"
  fi
  [[ -n "$ver" ]] || { echo "kind=no_version"; echo "unreachable=$KIT source declares no pack_version"; return 3; }
  top="$(git -C "$SRC_DIR" rev-parse --show-toplevel 2>/dev/null)" || top=""
  [[ -n "$top" ]] || { echo "kind=no_version"; echo "unreachable=package_dir is not a git work tree ($SRC_DIR)"; return 3; }
  SRC_TAG="$(pack_d1_tag_ref "$KIT" "$ver")"
  git -C "$SRC_DIR" rev-parse -q --verify "refs/tags/$SRC_TAG" >/dev/null 2>&1 \
    || { echo "kind=no_version"; echo "unreachable=tag $SRC_TAG absent in $top"; return 3; }
  return 0
}

life_engine() { "$PACK_PY3" "$ROOT/routines/lib/life_engine.py" "$ROOT"; }
le_field() { life_engine 2>/dev/null | sed -n "s/^$1=//p" | head -1; }

if [[ "$VERB" == "probe" ]]; then
  SRC_DIR="" SRC_TAG=""
  probe_source || exit 3
  printf 'source=%s\ntag=%s\n' "$SRC_DIR" "$SRC_TAG"
  exit 0
fi

# ---------- install (life kit) ------------------------------------------------
[[ -f "$ROOT/routines/lib/life_engine.py" ]] \
  || { echo "error=routines/lib/life_engine.py missing in $ROOT"; exit 4; }
CONF="$(le_field conf)"
[[ -n "$CONF" ]] || { echo "error=life_engine.py resolved no kit conf"; exit 4; }

INSTALLED=0
BUNDLE="$(le_field bundle)"
if [[ -z "$BUNDLE" ]]; then
  SRC_DIR="" SRC_TAG=""
  probe_source || exit 3
  SLUG="$(sed -n 's/^DOGANY_AGENT_NAME=//p' "$ROOT/.instance.conf" 2>/dev/null | head -1 || true)"
  [[ -n "$SLUG" ]] || { echo "error=no DOGANY_AGENT_NAME in $ROOT/.instance.conf"; exit 4; }
  rc=0
  out="$(bash "$SCRIPT_DIR/pack_install.sh" "$SLUG" "$ROOT" --pack "$LIFE_KIT" \
           --catalog "$CATALOG" --no-start 2>&1)" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    printf '%s\n' "$out" | tail -n 15 >&2
    # The public lane's clone/fetch failed after a good probe (connection
    # dropped in between): still a network failure, not an install error.
    if printf '%s\n' "$out" | grep -q "pack source: cannot fetch $LIFE_KIT" \
       && printf '%s\n' "$out" | grep -q "pack_source_fetch: \(clone failed\|tag fetch failed\)"; then
      echo "kind=network"
      echo "unreachable=fetch from $SRC_DIR failed during install"
      exit 3
    fi
    echo "error=pack_install.sh rc=$rc ($SRC_TAG; log: .telegram_bot/logs/pack-install.log)"
    exit 4
  fi
  INSTALLED=1
  BUNDLE="$(le_field bundle)"
  [[ -n "$BUNDLE" ]] || { echo "error=pack_install.sh rc=0 but bundle= still empty"; exit 4; }
fi

# Spend the one-shot offer only now that the kit is really there.
"$PACK_PY3" - "$CONF" <<'PYEOF' || { echo "error=cannot write LIFEKIT=offered to the kit conf"; exit 4; }
import os, sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines(True) if os.path.isfile(path) else []
out, hit = [], False
for line in lines:
    if not hit and line.strip().startswith("LIFEKIT="):
        hit = True
        val = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
        if val == "pending":
            line = "LIFEKIT=offered\n"
    out.append(line)
if hit:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    os.replace(tmp, path)
PYEOF
LIFEKIT="$(sed -n 's/^LIFEKIT=//p' "$CONF" 2>/dev/null | head -1 || true)"
printf 'installed=%s\nbundle=%s\nlifekit=%s\n' "$INSTALLED" "$BUNDLE" "$LIFEKIT"
exit 0
