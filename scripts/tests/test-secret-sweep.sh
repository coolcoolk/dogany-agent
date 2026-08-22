#!/bin/bash
# test-secret-sweep.sh -- regression tests for scripts/secret-sweep.sh
# DGN-533 / DGN-868 (bak-variant gap)

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SWEEP_SCRIPT="$REPO_ROOT/scripts/secret-sweep.sh"

if [ ! -x "$SWEEP_SCRIPT" ]; then
  echo "FAIL: secret-sweep.sh not found or not executable: $SWEEP_SCRIPT"
  exit 1
fi

TOTAL=8
PASS=0
TEMP_DIRS=()

cleanup() {
  for d in "${TEMP_DIRS[@]}"; do
    [ -d "$d" ] && rm -rf "$d"
  done
}
trap cleanup EXIT

run_test() {
  local name="$1"
  local expected_exit="$2"
  local tmpdir="$3"

  echo -n "Test: $name ... "

  if bash "$SWEEP_SCRIPT" "$tmpdir" >/dev/null 2>&1; then
    actual_exit=0
  else
    actual_exit=1
  fi

  if [ "$actual_exit" = "$expected_exit" ]; then
    echo "PASS"
    PASS=$((PASS + 1))
  else
    echo "FAIL (expected exit $expected_exit, got $actual_exit)"
  fi
}

# Test (a): benign README only -> exit 0
TMPDIR_A="$(mktemp -d)"
TEMP_DIRS+=("$TMPDIR_A")
cd "$TMPDIR_A" || exit 1
git init -q
cat > README.md <<'EOF'
# Sample Project
This is a benign test repository.
EOF
git add README.md
git config user.email "test@example.com"
git config user.name "Test User"
git commit -q -m "initial commit"
run_test "benign README only" 0 "$TMPDIR_A"

# Test (b): fake telegram token -> exit 1
TMPDIR_B="$(mktemp -d)"
TEMP_DIRS+=("$TMPDIR_B")
cd "$TMPDIR_B" || exit 1
git init -q
# Assemble token dynamically to avoid literal detection in this test file
TOKEN_PREFIX="1234567890"
TOKEN_SUFFIX="ABCdefGHIjklMNOpqrsTUVwxyzABCDEFGHI"
printf '%s\n' "NOTBOT=${TOKEN_PREFIX}:${TOKEN_SUFFIX}" > config.txt
git add config.txt
git config user.email "test@example.com"
git config user.name "Test User"
git commit -q -m "add config"
run_test "file with fake telegram token" 1 "$TMPDIR_B"

# Test (c): AWS access key example -> exit 1
TMPDIR_C="$(mktemp -d)"
TEMP_DIRS+=("$TMPDIR_C")
cd "$TMPDIR_C" || exit 1
git init -q
# Assemble key dynamically to avoid literal detection
KEY_PREFIX="AKIA"
KEY_SUFFIX="IOSFODNN7EXAMPLE"
printf '%s\n' "${KEY_PREFIX}${KEY_SUFFIX}" > credentials.txt
git add credentials.txt
git config user.email "test@example.com"
git config user.name "Test User"
git commit -q -m "add credentials"
run_test "file with AWS example key" 1 "$TMPDIR_C"

# Test (d): tracked empty .env file -> exit 1
TMPDIR_D="$(mktemp -d)"
TEMP_DIRS+=("$TMPDIR_D")
cd "$TMPDIR_D" || exit 1
git init -q
touch .env
git add .env
git config user.email "test@example.com"
git config user.name "Test User"
git commit -q -m "add .env"
run_test "tracked empty .env file" 1 "$TMPDIR_D"

# Test (e): .env-style API_KEY line committed outside this repo -> exit 1
TMPDIR_E="$(mktemp -d)"
TEMP_DIRS+=("$TMPDIR_E")
cd "$TMPDIR_E" || exit 1
git init -q
# Assemble label and value separately so no literal API_KEY=<value> appears here
KEY_LABEL="API_KEY"
KEY_VAL="Zx7kQ2mR9pLv3nWs"
printf '%s=%s\n' "$KEY_LABEL" "$KEY_VAL" > secrets.env
git add secrets.env
git config user.email "test@example.com"
git config user.name "Test User"
git commit -q -m "add secrets.env"
run_test ".env-style API_KEY line (cat7 hit)" 1 "$TMPDIR_E"

# Test (f): .sweepignore glob suppresses a cat1 telegram-token hit -> exit 0
TMPDIR_F="$(mktemp -d)"
TEMP_DIRS+=("$TMPDIR_F")
cd "$TMPDIR_F" || exit 1
git init -q
# Assemble token parts dynamically to avoid cat1 literal detection in this file
TKN_A="1234567890"
TKN_B="ABCdefGHIjklMNOpqrsTUVwxyzABCDEFGHI"
printf '%s\n' "${TKN_A}:${TKN_B}" > fake-token.txt
printf '%s\n' "fake-token.txt" > .sweepignore
git add fake-token.txt .sweepignore
git config user.email "test@example.com"
git config user.name "Test User"
git commit -q -m "add fake-token with sweepignore"
run_test ".sweepignore suppresses telegram-token hit (cat1 allowlisted)" 0 "$TMPDIR_F"

# Test (g): plain .bak file containing a fake secret must be caught (DGN-868 gap 1)
TMPDIR_G="$(mktemp -d)"
TEMP_DIRS+=("$TMPDIR_G")
cd "$TMPDIR_G" || exit 1
git init -q
# cat8: a plain *.bak file is a forbidden tracked file regardless of content
cat > config.bak <<'SECRETEOF'
# stale backup that should never be committed
some_setting=value
SECRETEOF
git add config.bak
git config user.email "test@example.com"
git config user.name "Test User"
git commit -q -m "accidental bak commit"
run_test "plain .bak file tracked (cat8 gap 1 DGN-868)" 1 "$TMPDIR_G"

# Test (h): DB backup-variant filename (*.db.bak_<tag>_<date>) must be caught (DGN-868 gap 2)
TMPDIR_H="$(mktemp -d)"
TEMP_DIRS+=("$TMPDIR_H")
cd "$TMPDIR_H" || exit 1
git init -q
mkdir -p database
# Create a fake DB bak file; must be caught by cat8 even though it looks like a binary.
# Use a text file so content scan runs; cat8 flags by filename alone before content.
printf 'SQLite format backup\n' > "database/lifekit.db.bak_dgn579_20260726_084041"
git add "database/lifekit.db.bak_dgn579_20260726_084041"
git config user.email "test@example.com"
git config user.name "Test User"
git commit -q -m "accidental db bak commit"
run_test "db-bak-variant file tracked (cat8 gap 2 DGN-868)" 1 "$TMPDIR_H"

# Final report
echo ""
echo "PASS $PASS/$TOTAL"
if [ "$PASS" = "$TOTAL" ]; then
  exit 0
else
  exit 1
fi
