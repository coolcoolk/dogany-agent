#!/bin/bash
# test-mirror-forward.sh -- self-test for DGN-880 mirror-forward publish
# (scripts/lib/mirror-forward.sh + scripts/publish.sh --execute).
#
# Everything runs against THROWAWAY fixture repos in mktemp (local bare repos
# standing in for origin + public). No real remote is ever touched.
#
# Covered safety invariants (DGN-880 LOCKED design v2):
#   - enumeration: plain stable tags only, ancestors of origin/main, ASCENDING
#   - tag verification: local SHA != origin SHA -> die; tag absent on origin
#     (un-released) -> die; tag-tree VERSION != tag -> die
#   - skip predicate: subject+tag => skip; subject-only (tag lost on public)
#     => re-run, deterministic snapshot reproduces the SAME SHA
#   - machinery pinning: a tag-time product.yaml that claimed a secret path
#     public does NOT leak -- the CURRENT main manifest set governs (fail-closed
#     no-owner drop); content still comes from the tag tree
#   - secret sweep runs per published step, 0-hit hard (fail aborts pre-push)
#   - new-exposure guard: newly-exposed pre-existing paths HOLD (exit 3)
#     without --confirm-public-expose; publish proceeds with it
#   - idempotency: re-run publishes nothing and rewrites nothing
#
# Exit 0 = all assertions pass; nonzero = a failure (prints which).

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PUBLISH="$SCRIPT_DIR/../publish.sh"
MIRROR_LIB="$SCRIPT_DIR/../lib/mirror-forward.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

check_eq() {  # $1=desc $2=got $3=expected
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got: '$2', expected: '$3')"; fi
}
check_match() {  # $1=desc $2=string $3=grep -E pattern
  if printf '%s\n' "$2" | grep -qE "$3"; then ok "$1"; else bad "$1 (no match /$3/)"; fi
}
check_not_match() {
  if ! printf '%s\n' "$2" | grep -qE "$3"; then ok "$1"; else bad "$1 (unexpected match /$3/)"; fi
}

echo "== mirror-forward self-test (DGN-880) =="

# shellcheck source=../lib/mirror-forward.sh
. "$MIRROR_LIB"

# ============================================================================
# Fixture: canonical repo C with tags v1.0.0..v1.2.0, bare origin O, bare
# public P pre-seeded with a v1.0.0 snapshot (subject + tag).
# v1.1.0 adds secret/leak.txt owned by a tag-time PUBLIC manifest; that
# manifest is REMOVED at v1.2.0 (current main), so machinery pinning must
# no-owner-drop the leak when the v1.1.0 step is exported.
# ============================================================================
FIX="$(mktemp -d "${TMPDIR:-/tmp}/dgn880-fixture.XXXXXX")"
O="$FIX/origin.git"; P="$FIX/public.git"; C="$FIX/canon"
git init -q --bare "$O"
git init -q --bare "$P"
mkdir -p "$C"
git -C "$C" init -q -b main
git -C "$C" config user.name "Fixture Bot"
git -C "$C" config user.email "fixture@example.invalid"

bump() {  # $1=version -- prepend a CHANGELOG heading + set VERSION
  echo "$1" > "$C/VERSION"
  if [ -f "$C/CHANGELOG.md" ]; then
    printf '## %s\n\n%s' "$1" "$(cat "$C/CHANGELOG.md")" > "$C/CHANGELOG.md"
  else
    printf '## %s\n' "$1" > "$C/CHANGELOG.md"
  fi
}

# v1.0.0
mkdir -p "$C/src" "$C/extra"
cat > "$C/product.yaml" <<'YAML'
id: fixture-core
type: module
visibility: public
status: official
owns:
  - "src/**"
YAML
bump 1.0.0
echo "a-v1.0.0" > "$C/src/a.txt"
echo "old-unowned" > "$C/extra/x.txt"
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm "c-1.0.0"; git -C "$C" tag v1.0.0

# v1.1.0 -- adds secret/ with a TAG-TIME public manifest (the fail-open trap)
mkdir -p "$C/secret"
cat > "$C/secret/product.yaml" <<'YAML'
id: fixture-secret
type: module
visibility: public
status: official
owns:
  - "secret/**"
YAML
echo "TAGTIME-LEAK" > "$C/secret/leak.txt"
echo "a-v1.1.0" > "$C/src/a.txt"
echo "b" > "$C/src/b.txt"
bump 1.1.0
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm "c-1.1.0"; git -C "$C" tag v1.1.0

# v1.2.0 -- secret unit REMOVED (current machinery no longer owns secret/**)
git -C "$C" rm -qr secret >/dev/null
echo "a-v1.2.0" > "$C/src/a.txt"
bump 1.2.0
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm "c-1.2.0"; git -C "$C" tag v1.2.0

git -C "$C" remote add origin "$O"
git -C "$C" push -q origin main --tags
git -C "$C" fetch -q origin
git -C "$C" remote add public "$P"

# Seed public with a v1.0.0-equivalent snapshot (what a v1.0.0 publish ships:
# src/a.txt + injected CHANGELOG.md), subject + tag.
S="$FIX/seed"; mkdir -p "$S/src"
git -C "$S" init -q -b main
echo "a-v1.0.0" > "$S/src/a.txt"
printf '## 1.0.0\n' > "$S/CHANGELOG.md"
git -C "$S" add -A >/dev/null
GIT_AUTHOR_NAME="Dogany Publish Bot" GIT_AUTHOR_EMAIL="publish@dogany.local" \
GIT_COMMITTER_NAME="Dogany Publish Bot" GIT_COMMITTER_EMAIL="publish@dogany.local" \
  git -C "$S" commit -qm "Dogany v1.0.0 -- curated public snapshot"
git -C "$S" tag v1.0.0
git -C "$S" push -q "$P" HEAD:refs/heads/main refs/tags/v1.0.0
git -C "$C" fetch -q public main 2>/dev/null || true

# Sweep stubs (machinery is env-pinned exactly like the real flow).
SWEEP_OK="$FIX/sweep-ok.sh"
SWEEP_LOG="$FIX/sweep.log"
cat > "$SWEEP_OK" <<EOF
#!/bin/bash
echo "sweep \$1" >> "$SWEEP_LOG"
exit 0
EOF
chmod +x "$SWEEP_OK"
SWEEP_FAIL="$FIX/sweep-fail.sh"
printf '#!/bin/bash\nexit 1\n' > "$SWEEP_FAIL"
chmod +x "$SWEEP_FAIL"

run_publish() {  # $@ = extra publish.sh args; uses $RUN_SWEEP (default ok stub)
  DOGANY_CANONICAL_REPO="$C" \
  DOGANY_SWEEP_SCRIPT="${RUN_SWEEP:-$SWEEP_OK}" \
  DOGANY_PUBLISH_STAGE="$(mktemp -d "${TMPDIR:-/tmp}/dgn880-stage.XXXXXX")" \
    bash "$PUBLISH" "$@" </dev/null
}

# ============================================================================
# 1. lib unit tests: version compare + enumeration (ascending, stable-only,
#    ancestor-only, patch tags included)
# ============================================================================
echo "[1] mf_ver_ge + mf_stable_tags + mf_pending_tags"

mf_ver_ge 1.2.0 1.2.0 && ok "ver_ge equal" || bad "ver_ge equal"
mf_ver_ge 1.10.0 1.9.0 && ok "ver_ge 1.10.0 >= 1.9.0 (numeric, not lexical)" || bad "ver_ge 1.10 vs 1.9"
mf_ver_ge 1.2.0 1.10.0 && bad "ver_ge 1.2.0 >= 1.10.0 should be false" || ok "ver_ge 1.2.0 < 1.10.0"

# dev tag + non-ancestor tag must be excluded from enumeration
git -C "$C" tag v1.2.0-dev.1 >/dev/null 2>&1
SIDE="$(git -C "$C" commit-tree "v1.0.0^{tree}" -p "$(git -C "$C" rev-list -n1 v1.0.0)" -m side 2>/dev/null)"
git -C "$C" tag v9.9.9 "$SIDE"
# patch tag ordering probe (points at the v1.1.0 commit; enumeration does not
# tree-check -- per-step verification does)
git -C "$C" tag v1.0.1 "$(git -C "$C" rev-list -n1 v1.1.0)"
TAGS="$(mf_stable_tags "$C" origin/main | tr '\n' ' ')"
check_eq "stable tags ascending, incl patch, excl dev + non-ancestor" \
  "$TAGS" "v1.0.0 v1.0.1 v1.1.0 v1.2.0 "
PEND="$(mf_pending_tags "$C" origin/main 1.1.0 | tr '\n' ' ')"
check_eq "pending >= PUB_LAST includes the floor (tag-loss repair)" "$PEND" "v1.1.0 v1.2.0 "
PEND="$(mf_pending_tags "$C" origin/main '' | tr '\n' ' ')"
check_eq "empty PUB_LAST -> empty pending (caller handles first publish)" "$PEND" ""
git -C "$C" tag -d v1.0.1 v9.9.9 v1.2.0-dev.1 >/dev/null

# ============================================================================
# 2. lib unit tests: mf_verify_tag (blocker 2)
# ============================================================================
echo "[2] mf_verify_tag: SHA match + ancestry + tree VERSION"

R="$(mf_verify_tag "$C" origin origin/main v1.2.0)"; RC=$?
check_eq "good tag verifies ok (rc)" "$RC" "0"
check_eq "good tag verifies ok (msg)" "$R" "ok"

GOOD_SHA="$(git -C "$C" rev-parse v1.2.0)"
git -C "$C" tag -f v1.2.0 "$(git -C "$C" rev-list -n1 v1.1.0)" >/dev/null
R="$(mf_verify_tag "$C" origin origin/main v1.2.0)"; RC=$?
[ "$RC" -ne 0 ] && ok "moved local tag -> verify fails (rc=$RC)" || bad "moved local tag must fail"
check_match "moved local tag -> reason cites SHA mismatch" "$R" "SHA"
git -C "$C" tag -f v1.2.0 "$GOOD_SHA" >/dev/null

git -C "$C" tag v1.6.0 >/dev/null   # local-only tag, never pushed
R="$(mf_verify_tag "$C" origin origin/main v1.6.0)"; RC=$?
[ "$RC" -ne 0 ] && ok "origin-missing tag -> verify fails (un-released)" || bad "origin-missing tag must fail"
check_match "origin-missing tag -> reason says un-released" "$R" "un-released|absent"
git -C "$C" tag -d v1.6.0 >/dev/null

# mislabeled tag: tree VERSION (1.2.0) != tag name v1.9.0
git -C "$C" tag v1.9.0 >/dev/null
git -C "$C" push -q origin refs/tags/v1.9.0
R="$(mf_verify_tag "$C" origin origin/main v1.9.0)"; RC=$?
[ "$RC" -ne 0 ] && ok "mislabeled tag (tree VERSION mismatch) -> verify fails" || bad "mislabeled tag must fail"
check_match "mislabeled tag -> reason cites VERSION" "$R" "VERSION"
git -C "$C" push -q origin :refs/tags/v1.9.0
git -C "$C" tag -d v1.9.0 >/dev/null

# ============================================================================
# 3. lib unit tests: mf_public_action (blocker 3 skip predicate)
# ============================================================================
echo "[3] mf_public_action: skip / rerun / publish"

A="$(mf_public_action "$C" public main "$P" 1.0.0)"
check_eq "subject+tag -> skip" "$A" "skip"
git -C "$P" tag -d v1.0.0 >/dev/null
A="$(mf_public_action "$C" public main "$P" 1.0.0)"
check_eq "subject-only (tag lost) -> rerun" "$A" "rerun"
# restore the seed tag for the e2e climb below
git -C "$S" push -q "$P" refs/tags/v1.0.0
A="$(mf_public_action "$C" public main "$P" 1.2.0)"
check_eq "unpublished version -> publish" "$A" "publish"

# ============================================================================
# 4. e2e climb: --execute publishes v1.1.0 then v1.2.0 ascending; v1.0.0 skips;
#    machinery pin drops the tag-time secret; content comes from the tag.
# ============================================================================
echo "[4] e2e climb v1.0.0(skip) -> v1.1.0 -> v1.2.0"

: > "$SWEEP_LOG"
OUT="$(run_publish --execute 2>&1)"; RC=$?
check_eq "climb exits 0" "$RC" "0"
check_match "v1.0.0 skipped (already fully published)" "$OUT" "v1.0.0 already fully published"
S11="$(printf '%s\n' "$OUT" | grep -n "mirror-forward step v1.1.0" | head -1 | cut -d: -f1)"
S12="$(printf '%s\n' "$OUT" | grep -n "mirror-forward step v1.2.0" | head -1 | cut -d: -f1)"
if [ -n "$S11" ] && [ -n "$S12" ] && [ "$S11" -lt "$S12" ]; then
  ok "steps run ascending (v1.1.0 before v1.2.0)"
else
  bad "steps not ascending (v1.1.0@$S11 v1.2.0@$S12)"
fi
SUBJ="$(git -C "$P" log -1 --format=%s main)"
check_eq "public HEAD subject = v1.2.0 snapshot" "$SUBJ" "Dogany v1.2.0 -- curated public snapshot"
git -C "$P" rev-parse -q --verify refs/tags/v1.1.0 >/dev/null && ok "public has tag v1.1.0" || bad "public missing tag v1.1.0"
git -C "$P" rev-parse -q --verify refs/tags/v1.2.0 >/dev/null && ok "public has tag v1.2.0" || bad "public missing tag v1.2.0"
PARENTS="$(git -C "$P" rev-list --parents -n1 main | awk '{print NF-1}')"
check_eq "public main is a single orphan snapshot (0 parents)" "$PARENTS" "0"
# content-from-tag: v1.1.0 snapshot carries the v1.1.0 file content
check_eq "v1.1.0 snapshot content from TAG tree" "$(git -C "$P" show v1.1.0:src/a.txt)" "a-v1.1.0"
git -C "$P" ls-tree -r --name-only v1.1.0 | grep -q '^src/b.txt$' \
  && ok "v1.1.0 snapshot has tag-time src/b.txt" || bad "v1.1.0 snapshot missing src/b.txt"
# MACHINERY PIN (blocker 1): tag-time public manifest for secret/** must NOT
# leak -- current main machinery no-owner-drops it.
if git -C "$P" ls-tree -r --name-only v1.1.0 | grep -q '^secret/'; then
  bad "SECRET LEAK: tag-time manifest exported secret/ (machinery pin broken)"
else
  ok "machinery pin: tag-time secret/ manifest did NOT export (no-owner drop)"
fi
check_eq "secret sweep ran once per published step" "$(grep -c . "$SWEEP_LOG")" "2"

# ============================================================================
# 5. idempotency + tag-loss repair (blocker 3)
# ============================================================================
echo "[5] idempotent re-run + tag-loss repair"

SHA_BEFORE="$(git -C "$P" rev-parse main)"
OUT="$(run_publish --execute 2>&1)"; RC=$?
check_eq "idempotent re-run exits 0" "$RC" "0"
check_match "idempotent re-run publishes nothing" "$OUT" "already published|up to date"
check_eq "public main SHA unchanged on idempotent re-run" "$(git -C "$P" rev-parse main)" "$SHA_BEFORE"

git -C "$P" tag -d v1.2.0 >/dev/null
OUT="$(run_publish --execute 2>&1)"; RC=$?
check_eq "tag-loss re-run exits 0" "$RC" "0"
check_match "tag-loss detected -> step re-runs (not skip)" "$OUT" "re-running step"
git -C "$P" rev-parse -q --verify refs/tags/v1.2.0 >/dev/null \
  && ok "lost tag v1.2.0 repaired on public" || bad "lost tag v1.2.0 NOT repaired"
check_eq "deterministic snapshot: public main SHA identical after repair" \
  "$(git -C "$P" rev-parse main)" "$SHA_BEFORE"

# ============================================================================
# 6. verification failures abort BEFORE any push (blocker 2)
# ============================================================================
echo "[6] verify failures: moved tag / mislabeled tag / un-pushed tag"

# (a) local tag moved away from origin
git -C "$C" tag -f v1.2.0 "$(git -C "$C" rev-list -n1 v1.1.0)" >/dev/null
OUT="$(run_publish --execute 2>&1)"; RC=$?
[ "$RC" -eq 1 ] && ok "moved local tag -> publish dies (rc=1)" || bad "moved local tag rc=$RC (want 1)"
check_match "moved tag failure names verification" "$OUT" "failed verification"
check_eq "public untouched after verify death" "$(git -C "$P" rev-parse main)" "$SHA_BEFORE"
git -C "$C" tag -f v1.2.0 "$GOOD_SHA" >/dev/null

# (b) mislabeled tag on origin (tree VERSION != tag)
git -C "$C" tag v1.9.0 >/dev/null
git -C "$C" push -q origin refs/tags/v1.9.0
OUT="$(run_publish --execute 2>&1)"; RC=$?
[ "$RC" -eq 1 ] && ok "mislabeled tag -> publish dies (rc=1)" || bad "mislabeled tag rc=$RC (want 1)"
check_match "mislabeled tag failure cites VERSION" "$OUT" "VERSION"
git -C "$C" push -q origin :refs/tags/v1.9.0
git -C "$C" tag -d v1.9.0 >/dev/null

# (c) un-pushed (origin-missing) tag = un-released state: never publish
echo "a-v1.3.0" > "$C/src/a.txt"; bump 1.3.0
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm "c-1.3.0"
git -C "$C" push -q origin main
git -C "$C" tag v1.3.0
OUT="$(run_publish --execute 2>&1)"; RC=$?
[ "$RC" -eq 1 ] && ok "origin-missing tag -> publish dies (rc=1)" || bad "origin-missing tag rc=$RC (want 1)"
check_match "origin-missing failure says un-released" "$OUT" "un-released|absent"
check_eq "public untouched (still v1.2.0 snapshot)" "$(git -C "$P" log -1 --format=%s main)" "Dogany v1.2.0 -- curated public snapshot"
# release the tag properly -> climb succeeds
git -C "$C" push -q origin refs/tags/v1.3.0
OUT="$(run_publish --execute 2>&1)"; RC=$?
check_eq "climb to v1.3.0 after proper release exits 0" "$RC" "0"
check_eq "public at v1.3.0" "$(git -C "$P" log -1 --format=%s main)" "Dogany v1.3.0 -- curated public snapshot"

# ============================================================================
# 7. new-exposure guard (blocker 4): widened manifest over pre-existing path
#    HOLDs with exit 3; --confirm-public-expose proceeds.
# ============================================================================
echo "[7] new-exposure guard: HOLD exit 3 -> confirm proceeds"

cat > "$C/product.yaml" <<'YAML'
id: fixture-core
type: module
visibility: public
status: official
owns:
  - "src/**"
  - "extra/**"
YAML
bump 1.4.0
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm "c-1.4.0 widen owns to extra/"
git -C "$C" push -q origin main
git -C "$C" tag v1.4.0
git -C "$C" push -q origin refs/tags/v1.4.0
OUT="$(run_publish --execute 2>&1)"; RC=$?
check_eq "exposure detected -> HOLD exit 3" "$RC" "3"
check_match "hold output names NEW EXPOSURE" "$OUT" "NEW EXPOSURE"
check_match "hold output lists the newly-exposed pre-existing path" "$OUT" "extra/x.txt"
check_eq "public untouched during hold (still v1.3.0)" "$(git -C "$P" log -1 --format=%s main)" "Dogany v1.3.0 -- curated public snapshot"
git -C "$P" rev-parse -q --verify refs/tags/v1.4.0 >/dev/null \
  && bad "v1.4.0 tag must NOT be on public during hold" || ok "no v1.4.0 tag on public during hold"

OUT="$(run_publish --execute --confirm-public-expose 2>&1)"; RC=$?
check_eq "confirmed exposure publishes (exit 0)" "$RC" "0"
check_match "exposure acknowledged in output" "$OUT" "acknowledged via --confirm-public-expose"
check_eq "public at v1.4.0" "$(git -C "$P" log -1 --format=%s main)" "Dogany v1.4.0 -- curated public snapshot"
git -C "$P" ls-tree -r --name-only main | grep -q '^extra/x.txt$' \
  && ok "confirmed exposure ships extra/x.txt" || bad "extra/x.txt missing after confirmed exposure"

# ============================================================================
# 8. secret sweep fail-closed: sweep exit != 0 aborts before any push
# ============================================================================
echo "[8] sweep fail-closed per step"

echo "a-v1.5.0" > "$C/src/a.txt"; bump 1.5.0
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm "c-1.5.0"
git -C "$C" push -q origin main
git -C "$C" tag v1.5.0
git -C "$C" push -q origin refs/tags/v1.5.0
OUT="$(RUN_SWEEP="$SWEEP_FAIL" run_publish --execute 2>&1)"; RC=$?
[ "$RC" -eq 1 ] && ok "failing sweep -> publish dies (rc=1)" || bad "failing sweep rc=$RC (want 1)"
check_match "sweep failure message present" "$OUT" "secret-sweep FAILED"
check_eq "public untouched after sweep failure (still v1.4.0)" "$(git -C "$P" log -1 --format=%s main)" "Dogany v1.4.0 -- curated public snapshot"
git -C "$P" rev-parse -q --verify refs/tags/v1.5.0 >/dev/null \
  && bad "v1.5.0 must NOT reach public after sweep failure" || ok "no v1.5.0 on public after sweep failure"
OUT="$(run_publish --execute 2>&1)"; RC=$?
check_eq "clean sweep re-run publishes v1.5.0 (exit 0)" "$RC" "0"
check_eq "public at v1.5.0" "$(git -C "$P" log -1 --format=%s main)" "Dogany v1.5.0 -- curated public snapshot"

# ============================================================================
# 9. exposure guard path (a): product.yaml visibility flip private -> public
#    HOLDs; --confirm-public-expose proceeds. (Path (b) covered in [7].)
# ============================================================================
echo "[9] exposure guard: visibility flip private -> public"

# v1.6.0: add a PRIVATE unit -- its paths must be blocked, publish clean.
mkdir -p "$C/priv"
cat > "$C/priv/product.yaml" <<'YAML'
id: fixture-priv
type: module
visibility: private
status: official
owns:
  - "priv/**"
YAML
echo "private-payload" > "$C/priv/p.txt"
bump 1.6.0
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm "c-1.6.0 add private unit"
git -C "$C" push -q origin main
git -C "$C" tag v1.6.0
git -C "$C" push -q origin refs/tags/v1.6.0
OUT="$(run_publish --execute 2>&1)"; RC=$?
check_eq "private unit release publishes clean (exit 0)" "$RC" "0"
git -C "$P" ls-tree -r --name-only main | grep -q '^priv/' \
  && bad "private unit paths leaked to public" || ok "private unit paths blocked from public"

# v1.7.0: FLIP the unit private -> public. Guard must HOLD (exit 3).
cat > "$C/priv/product.yaml" <<'YAML'
id: fixture-priv
type: module
visibility: public
status: official
owns:
  - "priv/**"
YAML
bump 1.7.0
git -C "$C" add -A >/dev/null; git -C "$C" commit -qm "c-1.7.0 flip priv unit public"
git -C "$C" push -q origin main
git -C "$C" tag v1.7.0
git -C "$C" push -q origin refs/tags/v1.7.0
OUT="$(run_publish --execute 2>&1)"; RC=$?
check_eq "visibility flip -> HOLD exit 3" "$RC" "3"
check_match "hold names the visibility flip" "$OUT" "visibility flips"
check_match "hold names the flipped unit" "$OUT" "fixture-priv"
check_eq "public untouched during flip hold (still v1.6.0)" "$(git -C "$P" log -1 --format=%s main)" "Dogany v1.6.0 -- curated public snapshot"
OUT="$(run_publish --execute --confirm-public-expose 2>&1)"; RC=$?
check_eq "confirmed flip publishes (exit 0)" "$RC" "0"
git -C "$P" ls-tree -r --name-only main | grep -q '^priv/p.txt$' \
  && ok "flipped unit ships after explicit confirm" || bad "flipped unit missing after confirm"

# ============================================================================
echo ""
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  rm -rf "$FIX"
  exit 0
fi
echo "fixture kept for inspection: $FIX"
exit 1
