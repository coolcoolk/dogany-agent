#!/bin/bash
# test-lane: fast
# test-dgn1020-pin-ownership.sh -- DGN-967 pin ownership contract, moved here
# from the devkit stage-0 suite (DGN-1020, audit dsp-20260915-171250-6254 D
# step 0 + "핀 fixture 는 정본 쪽으로").  The devkit no longer ships a copy of
# this footer; the board plugin only returns text and this file owns every
# write, so the contract that "a board with fewer features must never
# destroy another owner's pin" is pinned on the canonical template itself.
#
#   P1 reserved-owner rejection (board / legacy) writes nothing
#   P2 domain pin -> board publish -> board clear -> the domain pin SURVIVES
#      (board first, domain last while both live -- Rev 16 owner order)
#   P3 clearing the last domain section restores the HIDE signal ("")
#   P4 TTL sweep expires only the opted-in section
#   P5 a foreign direct write is adopted as legacy; a foreign EMPTY write
#      clears it
#   P6 a stale board whose title carries the inline "(갱신 HH:MM)" stamp is
#      OURS -- never re-adopted as legacy (Rev 15 inline stamp)
#   P7 concurrent distinct-owner writers all land (flock)
#
# SAFETY: every call runs against a private mktemp DOGANY_BOT_DATA_DIR; no
# git, no network, nothing outside the sandbox.
set -u

FOOTER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/status-footer.py"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn1020-pin.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); }
bad()  { FAIL=$((FAIL + 1)); echo "FAIL: $*" >&2; }

[[ -f "$FOOTER" ]] || { echo "FAIL: footer missing: $FOOTER" >&2; exit 1; }

# Board publish/clear are the Stop-hook writer path (no CLI flag); drive
# them through the module surface exactly as the hook does.
board() {  # board publish|clear [text]
  DOGANY_BOT_DATA_DIR="$1" python3 - "$FOOTER" "$2" "${3:-}" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sf", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if sys.argv[2] == "publish":
    ok = mod._board_publish(sys.argv[3])
else:
    ok = mod._board_clear()
sys.exit(0 if ok else 1)
PY
}

# --- P1: reserved owners rejected, store untouched -------------------------
for args in "--pin-set board" "--pin-set legacy" "--pin-clear board"; do
  out="$(DOGANY_BOT_DATA_DIR="$WORK" python3 "$FOOTER" $args </dev/null 2>&1)"
  rc=$?
  if [[ $rc -eq 1 && "$out" == *"invalid owner slug"* ]]; then
    ok
  else
    bad "P1 reserved-owner call '$args' not rejected (rc=$rc out=$out)"
  fi
done
if [[ -e "$WORK/dashboard-sections.json" || -e "$WORK/dashboard.md" ]]; then
  bad "P1 rejected pin call wrote to the section store"
else
  ok
fi

# --- P2: domain pin -> board publish -> board clear -> domain survives -----
if printf '헬스 라이브 카드\n- 세트 3/5' \
     | DOGANY_BOT_DATA_DIR="$WORK" python3 "$FOOTER" --pin-set health \
       >/dev/null 2>&1 \
   && grep -q '헬스 라이브 카드' "$WORK/dashboard.md" 2>/dev/null; then
  ok
else
  bad "P2 --pin-set health did not compose the domain section"
fi
BOARD_TEXT='작업대 (갱신 11:22)

[라이브]
- 작업 dgn1020-stage0'
if board "$WORK" publish "$BOARD_TEXT" \
   && grep -q '작업대 (갱신 11:22)' "$WORK/dashboard.md" \
   && grep -q '헬스 라이브 카드' "$WORK/dashboard.md"; then
  ok
else
  bad "P2 board publish lost the domain section or its own content"
fi
# Ordering contract: board FIRST, domain LAST (Rev 16 owner order; the
# board's stamp rides its own title line, so it is visible either way).
if python3 - "$WORK/dashboard.md" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read()
title = text.find("작업대 (갱신")
sys.exit(0 if title >= 0 and title < text.find("헬스 라이브 카드") else 1)
PY
then
  ok
else
  bad "P2 compose order wrong (board must render before domain sections)"
fi
if board "$WORK" clear \
   && grep -q '헬스 라이브 카드' "$WORK/dashboard.md" \
   && ! grep -q '작업대' "$WORK/dashboard.md"; then
  ok
else
  bad "P2 CORE: board clear destroyed the domain pin (or kept the board)"
fi

# --- P3: clearing the last domain section restores the HIDE signal ---------
if DOGANY_BOT_DATA_DIR="$WORK" python3 "$FOOTER" --pin-clear health \
     >/dev/null 2>&1 \
   && [[ -f "$WORK/dashboard.md" && ! -s "$WORK/dashboard.md" ]]; then
  ok
else
  bad "P3 --pin-clear health did not compose back to the empty HIDE signal"
fi

# --- P4: TTL sweep expires only the opted-in section -----------------------
W4="$WORK/p4"; mkdir -p "$W4"
printf '만료 카드' | DOGANY_BOT_DATA_DIR="$W4" python3 "$FOOTER" \
  --pin-set ephemeral --ttl 1 >/dev/null 2>&1
printf '상주 카드' | DOGANY_BOT_DATA_DIR="$W4" python3 "$FOOTER" \
  --pin-set resident >/dev/null 2>&1
sleep 2
# Any compose cycle sweeps; use a third owner's write as the trigger.
printf '트리거' | DOGANY_BOT_DATA_DIR="$W4" python3 "$FOOTER" \
  --pin-set trigger >/dev/null 2>&1
if ! grep -q '만료 카드' "$W4/dashboard.md" \
   && grep -q '상주 카드' "$W4/dashboard.md"; then
  ok
else
  bad "P4 TTL sweep wrong (expired kept, or no-ttl sibling lost)"
fi

# --- P5: foreign direct write adopted, foreign empty write clears ----------
W5="$WORK/p5"; mkdir -p "$W5"
board "$W5" publish "$BOARD_TEXT" >/dev/null 2>&1
printf '외부 직접 카드\n- 외부 내용\n' > "$W5/dashboard.md"
if board "$W5" publish "$BOARD_TEXT" \
   && grep -q '외부 직접 카드' "$W5/dashboard.md" \
   && grep -q '"legacy"' "$W5/dashboard-sections.json"; then
  ok
else
  bad "P5 foreign direct write was clobbered instead of adopted as legacy"
fi
: > "$W5/dashboard.md"
if board "$W5" publish "$BOARD_TEXT" \
   && ! grep -q '외부 직접 카드' "$W5/dashboard.md" \
   && ! grep -q '"legacy"' "$W5/dashboard-sections.json"; then
  ok
else
  bad "P5 foreign EMPTY write (hide signal) did not clear the legacy section"
fi

# --- P6: stamped board title is OURS -- never re-adopted as legacy ---------
# Without the stamped-title guard (dev fork Rev 12c, canonical Rev 15) the stamped
# title line fails the bare-equality match, the whole stale board becomes
# "foreign" domain content, and the board duplicates itself as legacy.
W6="$WORK/p6"; mkdir -p "$W6"
printf '작업대 (갱신 09:41)\n\n[라이브]\n- 작업 stale\n' > "$W6/dashboard.md"
# No manifest -> composed_sha None -> the adoption path runs unconditionally.
printf '도메인 카드' | DOGANY_BOT_DATA_DIR="$W6" python3 "$FOOTER" \
  --pin-set domain >/dev/null 2>&1
if grep -q '도메인 카드' "$W6/dashboard.md" \
   && ! grep -q '"legacy"' "$W6/dashboard-sections.json" \
   && ! grep -q '작업 stale' "$W6/dashboard.md"; then
  ok
else
  bad "P6 stamped stale board was re-adopted as legacy (self-duplication)"
fi

# --- P7: concurrent distinct-owner writers all land ------------------------
W7="$WORK/p7"; mkdir -p "$W7"
pids=()
for i in 1 2 3 4 5 6 7 8; do
  printf '동시 카드 %s' "$i" | DOGANY_BOT_DATA_DIR="$W7" \
    python3 "$FOOTER" --pin-set "owner$i" >/dev/null 2>&1 &
  pids+=($!)
done
wait "${pids[@]}" 2>/dev/null
lost=0
for i in 1 2 3 4 5 6 7 8; do
  grep -q "동시 카드 $i" "$W7/dashboard.md" || lost=$((lost + 1))
done
if [[ $lost -eq 0 ]]; then
  ok
else
  bad "P7 concurrent pin-set lost $lost of 8 distinct-owner sections"
fi

echo "test-dgn1020-pin-ownership: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
