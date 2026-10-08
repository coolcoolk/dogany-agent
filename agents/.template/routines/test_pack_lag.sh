#!/bin/bash
# test-lane: fast
# test_pack_lag.sh -- DGN-1493 / DGN-1417: routines/pack-lag.sh acceptance.
#
# Every case runs against a hermetic fixture under one mktemp dir: a fake
# instance root (.instance.conf + config/packs/) and a real throwaway git
# repo carrying pack/<id>/v* tags. No live instance, no network, no push.
#
# The cases are the ticket's acceptance criteria, one test each:
#   V1  BEHIND is detected from INSIDE the instance, with no canonical repo
#       anywhere in the picture (DGN-1493 criterion 1)
#   V2  CURRENT is clean and rc 0
#   V3  a pre-release held on the dev channel is CURRENT, not broken
#       (DGN-1493 criterion 2)
#   V4  a pre-release ranked ABOVE the stable channel head is
#       PRERELEASE-AHEAD and rc 0 -- dogfooding is normal
#   V5  SILENCE REPRODUCTION: a BEHIND pack MUST raise an alert naming the
#       pack. This is the guard for "a lag that passes with no alert is the
#       defect" (DGN-1493 criterion 3). Mutating the notify call away must
#       turn this case RED -- that is the mutation check.
#   V6  a stable install ranked above the source head is DRIFT (rc 1), and
#       is worded differently from the pre-release case
#   V7  the receipt beats the ledger: a -dev.1 receipt over a stable ledger
#       version is measured as the pre-release (DGN-1469 / DGN-1489)
#   V8  an install whose coordinate was never published reads UNTAGGED, rc 1
#   V9  a framework-only instance (no DOGANY_PACKS) prints an explicit
#       0-pack statement and exits 0 -- never a silent nothing
#   V10 a pack mounted but absent from the registry is UNREGISTERED, rc 1
#       (cannot measure != clean)
#   V11 a duplicate registry key is rc 2, never a quiet first-line-wins
#   V12 a declared pin is honoured instead of the channel head
#   V13 estate host (DGN-1417 round 3): BEHIND leaves the update-notice
#       request and raises NO separate push (absorbed, no double notice)
#   V14 estate host, BEHIND + an evidence problem: the request for the lag,
#       the push for the evidence problem only
#   V15 DGN-1747: the BEHIND push is the owner-approved first-person copy --
#       agent display name from routines/lib/agent_name.py, no pack id /
#       version / English internals in the headline, the per-pack lines only
#       inside the folded detail block
#   V16 DGN-1808: the scheduled (notify) run that measured BEHIND and left
#       the owner's update offer exits 0, and through the REAL cron-guard.sh
#       produces no "작업이 실패했어요" push; --no-notify (lint) keeps rc 1
#   V17 DGN-1808: cannot-measure (rc 2) through cron-guard still alarms
#   V18 DGN-1808 r2 (dec-220): an evidence problem only (nothing behind)
#       goes to the OPERATOR sink only (owner sink empty) and exits 0 when
#       delivered; an UNdelivered finding (no push.sh) exits 1 so
#       cron-guard's alarm carries it -- never silent
#   V19 dec-220 through the REAL push.sh (stubbed curl, two bot tokens):
#       operator resolved -> sent with the operator token only; operator
#       unresolved -> no send at all, the log-only line printed, rc 0, and
#       under the real cron-guard no "작업이 실패했어요" push either
#   V20 dec-220: BEHIND + an evidence problem on a non-estate host -- the
#       owner's BEHIND push carries the BEHIND line only, the evidence line
#       goes to the operator sink
#
# Exit contract (DGN-1808): LRC is the --no-notify (lint) rc and keeps
# 1 for any BEHIND/DRIFT/UNTAGGED/broken; RC is the notify (scheduled) rc,
# 0 once every finding reached its audience (evidence with no operator:
# logged by design). SINK = owner-bound bodies, SINK.operator = operator-only.
set -u

WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUT="$WT/routines/pack-lag.sh"
# Mutation hook: point the runner at a mutated copy to prove the tests go red.
SUT="${PACKLAG_SUT:-$SUT}"

PASS=0; FAIL=0
say() { printf '%s\n' "$*"; }
ok()  { PASS=$((PASS+1)); say "  ok: $*"; }
bad() { FAIL=$((FAIL+1)); say "  FAIL: $*"; }

SB="$(mktemp -d /tmp/dgn1493-packlag.XXXXXX)"
trap 'rm -rf "$SB"' EXIT

# --- fixture builders -------------------------------------------------------
mk_repo() { # mk_repo <dir> <tag...>
  local d="$1"; shift
  mkdir -p "$d"; git -C "$d" init -q .
  git -C "$d" -c user.email=t@t -c user.name=t commit -q --allow-empty -m seed
  local t; for t in "$@"; do git -C "$d" tag "$t"; done
}

mk_inst() { # mk_inst <root> <DOGANY_PACKS value or -> [extra conf lines...]
  local root="$1" packs="$2"; shift 2
  mkdir -p "$root/routines" "$root/config/packs"
  {
    echo "DOGANY_AGENT_NAME=fixture"
    echo "DOGANY_AGENT_LABEL=fixture"
    [[ "$packs" == "-" ]] || echo "DOGANY_PACKS=$packs"
    local l; for l in "$@"; do echo "$l"; done
  } > "$root/.instance.conf"
  cp "$SUT" "$root/routines/pack-lag.sh"
}

mk_receipt() { # mk_receipt <root> <id> <version> <source-tag or ->
  local root="$1" pid="$2" ver="$3" src="$4"
  {
    echo "schema=1"; echo "pack=$pid"; echo "version=$ver"
    echo "class=kit"; echo "exec=1-x"; echo "status=delivery-complete"
    [[ "$src" == "-" ]] || echo "source=tag:$src"
    echo "anchor_count=0"
  } > "$root/config/packs/$pid.receipt"
}

mk_reg() { # mk_reg <home> <lines...>
  local home="$1"; shift
  mkdir -p "$home"; printf '%s\n' "$@" > "$home/pack-sources.conf"
}

# run <case> <instance-root> <shared-home> -- sets OUT / RC / SINK / LRC
# LRC = the --no-notify (lint) rc, measured first (it leaves no request/push).
run() {
  local root="$2" home="$3"
  SINK="$SB/$1.sink"; : > "$SINK"; : > "$SINK.operator"
  LRC=0
  DOGANY_SHARED_HOME="$home" DOGANY_PACKLAG_SINK="$SINK" \
    bash "$root/routines/pack-lag.sh" --no-notify >/dev/null 2>&1 || LRC=$?
  [[ ! -s "$SINK" && ! -s "$SINK.operator" ]] || bad "$1 --no-notify pushed: $(cat "$SINK" "$SINK.operator")"
  RC=0
  OUT="$(DOGANY_SHARED_HOME="$home" DOGANY_PACKLAG_SINK="$SINK" \
         bash "$root/routines/pack-lag.sh" 2>&1)" || RC=$?
}

has()  { printf '%s' "$OUT" | grep -q "$1"; }

# --- V1 / V5: BEHIND + the silence reproduction -----------------------------
C="$SB/v1"; mk_repo "$C/repo" pack/dev/v1.3.0 pack/dev/v1.3.1
mk_reg "$C/home" "dev.repo=$C/repo" "dev.channel=stable"
mk_inst "$C/inst" "dev@1.3.0"
mk_receipt "$C/inst" dev 1.3.0 pack/dev/v1.3.0
run v1 "$C/inst" "$C/home"
if has 'BEHIND' && [[ "$LRC" -eq 1 ]]; then ok "V1 BEHIND detected inside the instance (lint rc 1)"; else bad "V1 BEHIND not detected (lrc=$LRC): $OUT"; fi
if [[ "$RC" -eq 0 ]]; then ok "V1 scheduled run whose push was delivered exits 0 (DGN-1808)"; else bad "V1 delivered BEHIND still rc=$RC"; fi
if has '1 cut(s) back'; then ok "V1 distance is reported, not just a flag"; else bad "V1 distance missing"; fi
if [[ -s "$SINK" ]] && grep -q 'dev: BEHIND' "$SINK"; then ok "V5 silence reproduction: BEHIND raised an alert naming the pack"; else bad "V5 SILENT LAG -- no alert raised for a BEHIND pack (sink: $(cat "$SINK"))"; fi

# --- V2: CURRENT ------------------------------------------------------------
C="$SB/v2"; mk_repo "$C/repo" pack/dev/v1.3.0 pack/dev/v1.3.1
mk_reg "$C/home" "dev.repo=$C/repo" "dev.channel=stable"
mk_inst "$C/inst" "dev@1.3.1"; mk_receipt "$C/inst" dev 1.3.1 pack/dev/v1.3.1
run v2 "$C/inst" "$C/home"
if has 'CURRENT' && [[ "$RC" -eq 0 ]]; then ok "V2 CURRENT is clean (rc 0)"; else bad "V2 not clean (rc=$RC): $OUT"; fi
if [[ ! -s "$SINK" ]]; then ok "V2 a clean run raises no alert"; else bad "V2 clean run alerted anyway"; fi

# --- V3: pre-release on the dev channel is CURRENT --------------------------
C="$SB/v3"; mk_repo "$C/repo" pack/ph/v0.11.0-dev.1 pack/ph/v0.11.5-dev.1
mk_reg "$C/home" "ph.repo=$C/repo" "ph.channel=dev"
mk_inst "$C/inst" "ph@0.11.5-dev.1"; mk_receipt "$C/inst" ph 0.11.5-dev.1 pack/ph/v0.11.5-dev.1
run v3 "$C/inst" "$C/home"
if has 'CURRENT' && [[ "$RC" -eq 0 ]]; then ok "V3 pre-release on dev channel is NORMAL (rc 0)"; else bad "V3 pre-release judged broken (rc=$RC): $OUT"; fi

# --- V4: pre-release ahead of the stable head -------------------------------
C="$SB/v4"; mk_repo "$C/repo" pack/kit/v2.20.3 pack/kit/v2.21.0-dev.2
mk_reg "$C/home" "kit.repo=$C/repo" "kit.channel=stable"
mk_inst "$C/inst" "kit@2.21.0-dev.2"; mk_receipt "$C/inst" kit 2.21.0-dev.2 pack/kit/v2.21.0-dev.2
run v4 "$C/inst" "$C/home"
if has 'PRERELEASE-AHEAD' && [[ "$RC" -eq 0 ]]; then ok "V4 pre-release above the stable head is NORMAL, not BROKEN (rc 0)"; else bad "V4 dogfooding judged broken (rc=$RC): $OUT"; fi

# --- V6: stable above the source head is DRIFT ------------------------------
C="$SB/v6"; mk_repo "$C/repo" pack/kit/v2.20.3 pack/kit/v2.21.0
mk_reg "$C/home" "kit.repo=$C/repo" "kit.channel=stable"
mk_inst "$C/inst" "kit@2.21.0"; mk_receipt "$C/inst" kit 2.21.0 pack/kit/v2.21.0
run v6 "$C/inst" "$C/home"
if has 'CURRENT' && [[ "$RC" -eq 0 ]]; then ok "V6 control: newest stable is CURRENT"; else bad "V6 control failed (rc=$RC): $OUT"; fi
# now make the source head go backwards (tag removed at source = drift shape)
git -C "$C/repo" tag -d pack/kit/v2.21.0 >/dev/null 2>&1
run v6b "$C/inst" "$C/home"
if has 'UNTAGGED' && [[ "$LRC" -eq 1 ]]; then ok "V6 a vanished source tag is UNTAGGED, not clean (lint rc 1)"; else bad "V6 vanished tag not caught (lrc=$LRC): $OUT"; fi

# --- V7: the receipt beats the ledger ---------------------------------------
C="$SB/v7"; mk_repo "$C/repo" pack/ph/v0.11.0-dev.1 pack/ph/v0.11.5-dev.1
mk_reg "$C/home" "ph.repo=$C/repo" "ph.channel=dev"
# ledger says the stable 0.11.0 (the DGN-1469 defect shape); receipt has truth
mk_inst "$C/inst" "ph@0.11.0"; mk_receipt "$C/inst" ph 0.11.0-dev.1 pack/ph/v0.11.0-dev.1
run v7 "$C/inst" "$C/home"
if has 'v0.11.0-dev.1' && has 'receipt'; then ok "V7 the receipt coordinate wins over the ledger projection"; else bad "V7 ledger used instead of receipt: $OUT"; fi
if has 'BEHIND' && [[ "$LRC" -eq 1 ]]; then ok "V7 and the pre-release is still ranked (BEHIND 0.11.5-dev.1)"; else bad "V7 pre-release not ranked (lrc=$LRC)"; fi

# --- V8: never-published coordinate -----------------------------------------
C="$SB/v8"; mk_repo "$C/repo" pack/kit/v2.20.3
mk_reg "$C/home" "kit.repo=$C/repo" "kit.channel=stable"
mk_inst "$C/inst" "kit@2.20.1"; mk_receipt "$C/inst" kit 2.20.1 -
run v8 "$C/inst" "$C/home"
if has 'UNTAGGED' && [[ "$LRC" -eq 1 ]]; then ok "V8 an untagged install is UNTAGGED, lint rc 1 (DGN-1489 shape)"; else bad "V8 untagged install passed (lrc=$LRC): $OUT"; fi

# --- V9: framework-only instance --------------------------------------------
C="$SB/v9"; mk_reg "$C/home" "# empty registry"
mk_inst "$C/inst" "-"
run v9 "$C/inst" "$C/home"
if has 'examined 0 pack' && has 'framework-only' && [[ "$RC" -eq 0 ]]; then ok "V9 fw-only instance states 0 packs explicitly (rc 0)"; else bad "V9 fw-only instance silent (rc=$RC): $OUT"; fi

# --- V10: mounted but unregistered ------------------------------------------
C="$SB/v10"; mk_reg "$C/home" "# empty registry"
mk_inst "$C/inst" "kit@2.20.3"
run v10 "$C/inst" "$C/home"
if has 'UNREGISTERED' && [[ "$LRC" -eq 1 ]]; then ok "V10 unmeasurable pack is reported, not skipped (lint rc 1)"; else bad "V10 unregistered pack skipped (lrc=$LRC): $OUT"; fi
if grep -q 'UNREGISTERED' "$SINK.operator"; then ok "V10 the unmeasurable case also alerts (the operator)"; else bad "V10 unmeasurable pack alerted nobody"; fi
if [[ ! -s "$SINK" ]]; then ok "V10 and not the owner (dec-220)"; else bad "V10 owner got an evidence push: $(cat "$SINK")"; fi

# --- V11: duplicate registry key --------------------------------------------
C="$SB/v11"; mk_repo "$C/repo" pack/kit/v1.0.0
mk_reg "$C/home" "kit.repo=$C/repo" "kit.repo=$C/repo" "kit.channel=stable"
mk_inst "$C/inst" "kit@1.0.0"
run v11 "$C/inst" "$C/home"
if has 'duplicate key' && [[ "$RC" -eq 2 && "$LRC" -eq 2 ]]; then ok "V11 duplicate registry key is rc 2, never first-line-wins"; else bad "V11 duplicate key tolerated (rc=$RC): $OUT"; fi

# --- V12: declared pin wins over the channel head ---------------------------
C="$SB/v12"; mk_repo "$C/repo" pack/kit/v1.0.0 pack/kit/v1.1.0
mk_reg "$C/home" "kit.repo=$C/repo" "kit.channel=stable"
mk_inst "$C/inst" "kit@1.0.0" "DOGANY_PACK_PINS=kit=pack/kit/v1.0.0@abc1234"
mk_receipt "$C/inst" kit 1.0.0 pack/kit/v1.0.0
run v12 "$C/inst" "$C/home"
if has 'PINNED-OK' && [[ "$RC" -eq 0 ]]; then ok "V12 a declared pin is honoured, not silently overtaken by the channel"; else bad "V12 pin ignored (rc=$RC): $OUT"; fi

# --- V13 / V14: estate host -- BEHIND is absorbed into the update notice ----
C="$SB/v13"; mk_repo "$C/repo" pack/dev/v1.3.0 pack/dev/v1.3.1
mkdir -p "$C/canon/scripts/pack"; : > "$C/canon/scripts/pack/update_surface.py"
mk_reg "$C/home" "dev.repo=$C/repo" "dev.channel=stable"
mk_inst "$C/inst" "dev@1.3.0" "DOGANY_REPO_ROOT=$C/canon"
mk_receipt "$C/inst" dev 1.3.0 pack/dev/v1.3.0
run v13 "$C/inst" "$C/home"
REQ="$C/inst/.telegram_bot/state/update-offer-request"
if has 'BEHIND' && [[ "$LRC" -eq 1 ]]; then ok "V13 measurement unchanged: BEHIND printed, lint rc 1"; else bad "V13 measurement changed (lrc=$LRC): $OUT"; fi
if [[ "$RC" -eq 0 ]]; then ok "V13 offer raised -> the scheduled run exits 0 (DGN-1808)"; else bad "V13 offer raised but rc=$RC"; fi
if [[ -f "$REQ" ]] && grep -q '"source": "pack-lag"' "$REQ"; then ok "V13 BEHIND leaves the update-notice request"; else bad "V13 no update-notice request"; fi
if [[ ! -s "$SINK" ]]; then ok "V13 no separate pack-lag push (absorbed, no double notification)"; else bad "V13 pushed anyway: $(cat "$SINK")"; fi

C="$SB/v14"; mk_repo "$C/repo" pack/dev/v1.3.0 pack/dev/v1.3.1
mk_repo "$C/repo2" pack/kit/v2.20.3
mkdir -p "$C/canon/scripts/pack"; : > "$C/canon/scripts/pack/update_surface.py"
mk_reg "$C/home" "dev.repo=$C/repo" "dev.channel=stable" "kit.repo=$C/repo2" "kit.channel=stable"
mk_inst "$C/inst" "dev@1.3.0,kit@2.20.1" "DOGANY_REPO_ROOT=$C/canon"
mk_receipt "$C/inst" dev 1.3.0 pack/dev/v1.3.0
mk_receipt "$C/inst" kit 2.20.1 -
run v14 "$C/inst" "$C/home"
if [[ -f "$C/inst/.telegram_bot/state/update-offer-request" ]]; then ok "V14 the lag goes to the update notice"; else bad "V14 no request for the lag"; fi
if grep -q 'kit: UNTAGGED' "$SINK.operator" && ! grep -q 'BEHIND' "$SINK.operator" && grep -q '1 pack evidence problem' "$SINK.operator"; then ok "V14 the operator push carries only the evidence problem"; else bad "V14 operator push content wrong: $(cat "$SINK.operator")"; fi
if [[ ! -s "$SINK" ]]; then ok "V14 the owner gets no push at all (lag -> offer, evidence -> operator)"; else bad "V14 owner pushed: $(cat "$SINK")"; fi

# --- V15: DGN-1747 approved copy for the BEHIND push ------------------------
C="$SB/v15"; mk_repo "$C/repo" pack/dev/v1.3.0 pack/dev/v1.3.1
mk_reg "$C/home" "dev.repo=$C/repo" "dev.channel=stable"
mk_inst "$C/inst" "dev@1.3.0"
mk_receipt "$C/inst" dev 1.3.0 pack/dev/v1.3.0
mkdir -p "$C/inst/routines/lib" "$C/inst/identity"
cp "$WT/routines/lib/agent_name.py" "$C/inst/routines/lib/agent_name.py"
printf '# AGENT\n- Name: **김아그**\n' > "$C/inst/identity/hot.custom.agent.md"
SINK="$SB/v15.sink"; : > "$SINK"; RC=0
OUT="$(DOGANY_LANG=ko DOGANY_SHARED_HOME="$C/home" DOGANY_PACKLAG_SINK="$SINK" \
       bash "$C/inst/routines/pack-lag.sh" 2>&1)" || RC=$?
V15_HEAD="$(head -n 2 "$SINK")"
V15_WANT="📦 김아그의 팩 1개가 최신 버전보다 뒤처져 있어요.
/update로 올릴 수 있어요."
if [[ "$V15_HEAD" == "$V15_WANT" ]]; then ok "V15 headline is the approved copy verbatim, agent named via agent_name.py"; else bad "V15 headline wrong: $V15_HEAD"; fi
if ! printf '%s' "$V15_HEAD" | sed 's#/update##' | grep -q '[A-Za-z]' \
   && ! printf '%s' "$V15_HEAD" | grep -q 'DGN-\|[0-9]\.[0-9]'; then
  ok "V15 headline carries no pack id / version / ticket / English internal"
else
  bad "V15 headline leaks internals: $V15_HEAD"
fi
if sed -n 3p "$SINK" | grep -q '^>! ' && grep -q '^> dev: BEHIND' "$SINK"; then ok "V15 per-pack detail rides the folded block"; else bad "V15 detail not folded: $(cat "$SINK")"; fi

# --- V16-V18: DGN-1808 -- a delivered finding is not a routine failure -------
# The real cron-guard.sh runs in the fixture's routines/ with a stub push.sh
# (cron-guard resolves $SCRIPT_DIR/push.sh) and a private dedup dir.
GUARD_SRC="$WT/routines/cron-guard.sh"
mk_guard() { # mk_guard <inst-root>: real cron-guard + recording push stub
  local r="$1/routines"
  sed -e "s|DEDUP_DIR=\"/tmp/dogany-cron-guard\"|DEDUP_DIR=\"$1/dedup\"|" \
    "$GUARD_SRC" > "$r/cron-guard.sh"
  cat > "$r/push.sh" <<STUB
#!/bin/bash
printf 'PUSH %s\n' "\$*" >> "$1/push.calls"
STUB
  chmod +x "$r/push.sh"; : > "$1/push.calls"
}
# guard <case> <inst-root> <shared-home> -- GRC = cron-guard's rc
guard() {
  local root="$2" home="$3"
  SINK="$SB/$1.sink"; : > "$SINK"; : > "$SINK.operator"; GRC=0
  DOGANY_SHARED_HOME="$home" DOGANY_PACKLAG_SINK="$SINK" \
    bash "$root/routines/cron-guard.sh" --label "com.test.$1.pack-lag-0317" \
      --friendly-name pack-lag --log "$root/pl.stdout.log" \
      -- /bin/bash "$root/routines/pack-lag.sh" > "$SB/$1.gout" 2>&1 || GRC=$?
}
failpush() { grep -q '실패\|failed\|FAILED' "$1/push.calls"; }

C="$SB/v16"; mk_repo "$C/repo" pack/dev/v1.3.0 pack/dev/v1.3.1
mkdir -p "$C/canon/scripts/pack"; : > "$C/canon/scripts/pack/update_surface.py"
mk_reg "$C/home" "dev.repo=$C/repo" "dev.channel=stable"
mk_inst "$C/inst" "dev@1.3.0" "DOGANY_REPO_ROOT=$C/canon"
mk_receipt "$C/inst" dev 1.3.0 pack/dev/v1.3.0
mk_guard "$C/inst"
guard v16 "$C/inst" "$C/home"
if [[ "$GRC" -eq 0 ]]; then ok "V16 behind + offer under cron-guard ends rc 0"; else bad "V16 cron-guard rc=$GRC: $(cat "$SB/v16.gout")"; fi
if [[ -f "$C/inst/.telegram_bot/state/update-offer-request" ]]; then ok "V16 the owner update offer was still raised"; else bad "V16 no update offer request"; fi
if [[ ! -s "$C/inst/push.calls" ]] && ! grep -q 'ROUTINE FAILED' "$SB/v16.gout"; then ok "V16 no cron-guard failure push (the 03:17 '작업이 실패했어요' shape)"; else bad "V16 failure alarm fired: $(cat "$C/inst/push.calls")"; fi
if [[ ! -s "$SINK" ]]; then ok "V16 and no separate pack-lag push"; else bad "V16 pack-lag pushed: $(cat "$SINK")"; fi
run v16l "$C/inst" "$C/home"
if [[ "$LRC" -eq 1 ]]; then ok "V16 lint path unchanged: --no-notify still rc 1 on BEHIND"; else bad "V16 lint rc=$LRC"; fi

C="$SB/v17"; mk_repo "$C/repo" pack/kit/v1.0.0
mk_reg "$C/home" "kit.repo=$C/repo" "kit.repo=$C/repo" "kit.channel=stable"
mk_inst "$C/inst" "kit@1.0.0"; mk_guard "$C/inst"
guard v17 "$C/inst" "$C/home"
if [[ "$GRC" -eq 2 ]] && failpush "$C/inst"; then ok "V17 cannot-measure (rc 2) still raises the cron-guard alarm"; else bad "V17 cannot-measure not alarmed (grc=$GRC): $(cat "$C/inst/push.calls")"; fi

C="$SB/v18"; mk_repo "$C/repo" pack/kit/v2.20.3
mk_reg "$C/home" "kit.repo=$C/repo" "kit.channel=stable"
mk_inst "$C/inst" "kit@2.20.1"; mk_receipt "$C/inst" kit 2.20.1 -
mk_guard "$C/inst"
guard v18 "$C/inst" "$C/home"
if [[ "$GRC" -eq 0 ]] && grep -q 'kit: UNTAGGED' "$SINK.operator" && ! failpush "$C/inst"; then
  ok "V18 evidence-only finding reaches the operator, delivered -> rc 0, no failure-worded push"
else bad "V18 grc=$GRC sink=$(cat "$SINK.operator") push=$(cat "$C/inst/push.calls")"; fi
if [[ ! -s "$SINK" ]] && ! grep -q '팩 지연' "$SINK.operator"; then ok "V18 owner sink empty, the old '[팩 지연]' owner copy is gone (dec-220)"; else bad "V18 owner-bound evidence push: $(cat "$SINK")"; fi
# undelivered: no sink seam, no push.sh in the instance -> rc 1 (cron-guard carries it)
rm -f "$C/inst/routines/push.sh"; RC=0
DOGANY_SHARED_HOME="$C/home" bash "$C/inst/routines/pack-lag.sh" >/dev/null 2>&1 || RC=$?
if [[ "$RC" -eq 1 ]]; then ok "V18 an undelivered finding keeps rc 1 (never silent)"; else bad "V18 undelivered finding rc=$RC"; fi

# --- V19: dec-220 through the REAL push.sh ---------------------------------
# Real push.sh + bridge sanitizer linked into the fixture, curl stubbed; the
# bot token in the sendMessage URL tells WHO a message went to.
mk_realpush() { # mk_realpush <inst-root>
  local r="$1" e name
  for e in "$WT/routines"/* "$WT/bridge"; do
    name="$(basename "$e")"
    case "$name" in pack-lag.sh|__pycache__|terminal-state-ledger.py) continue ;; esac
    [[ "$name" == bridge ]] && { ln -s "$e" "$r/bridge"; continue; }
    ln -s "$e" "$r/routines/$name"
  done
  mkdir -p "$r/.telegram_bot" "$r/bin"
  printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-ownertoken\nALLOWED_USER_IDS=111\n' > "$r/.telegram_bot/.env"
  printf 'TELEGRAM_BOT_TOKEN=TEST-ONLY-operatortoken\nALLOWED_USER_IDS=999\n' > "$r/steward.env"
  cat > "$r/bin/curl" <<'CURL_EOF'
#!/bin/bash
OUT=""; ARGS=("$@")
for ((i=0;i<${#ARGS[@]};i++)); do [[ "${ARGS[$i]}" == "-o" ]] && OUT="${ARGS[$((i+1))]}"; done
printf '%s\n' "$*" >> "$CURL_LOG"
[[ -n "$OUT" ]] && echo '{"ok":true,"result":{"message_id":1}}' > "$OUT"
printf '200'
CURL_EOF
  chmod +x "$r/bin/curl"
}
sends() { grep -o 'botTEST-ONLY-[a-z]*token/send[A-Za-z]*' "$1" 2>/dev/null | sort | uniq -c | tr -s ' ' ; }
C="$SB/v19"; mk_repo "$C/repo" pack/kit/v2.20.3
mk_reg "$C/home" "kit.repo=$C/repo" "kit.channel=stable"
mk_inst "$C/inst" "kit@2.20.1"; mk_receipt "$C/inst" kit 2.20.1 -
mk_realpush "$C/inst"
CL="$SB/v19.curl"
# a: operator resolved
printf 'STEWARD_ENV_FILE=%s\n' "$C/inst/steward.env" > "$C/inst/config/agent.conf"
: > "$CL"; RC=0
OUT="$(PATH="$C/inst/bin:$PATH" CURL_LOG="$CL" PUSH_GUARD_OVERRIDE=send-anyway DOGANY_SHARED_HOME="$C/home" \
       bash "$C/inst/routines/pack-lag.sh" 2>&1)" || RC=$?
if [[ "$RC" -eq 0 ]] && grep -q 'TEST-ONLY-operatortoken/sendMessage' "$CL" && grep -q 'UNTAGGED' "$CL"; then
  ok "V19a operator resolved -> the evidence finding is sent with the operator token (rc 0)"
else bad "V19a rc=$RC sends=$(sends "$CL") out=$OUT"; fi
if ! grep -q 'TEST-ONLY-ownertoken' "$CL"; then ok "V19a the owner token is never used"; else bad "V19a owner got it: $(sends "$CL")"; fi
# b: operator unresolved (a user install) -> log only, nothing sent
: > "$C/inst/config/agent.conf"
: > "$CL"; RC=0
OUT="$(PATH="$C/inst/bin:$PATH" CURL_LOG="$CL" PUSH_GUARD_OVERRIDE=send-anyway DOGANY_SHARED_HOME="$C/home" \
       bash "$C/inst/routines/pack-lag.sh" 2>&1)" || RC=$?
if [[ "$RC" -eq 0 ]]; then ok "V19b operator unresolved -> rc 0 (logged by design, not a failure)"; else bad "V19b rc=$RC: $OUT"; fi
if ! grep -q 'send' "$CL"; then ok "V19b no push at all -- no owner fallback"; else bad "V19b something was sent: $(sends "$CL")"; fi
if has 'logged only: no operator' && has 'kit: \?UNTAGGED\|UNTAGGED'; then ok "V19b the log-only line and the verdict are in the run log"; else bad "V19b log line missing: $OUT"; fi
# c: same, under the real cron-guard with the real push.sh
sed -e "s|DEDUP_DIR=\"/tmp/dogany-cron-guard\"|DEDUP_DIR=\"$C/inst/dedup\"|" \
  "$GUARD_SRC" > "$C/inst/routines/cron-guard.sh.real"
rm -f "$C/inst/routines/cron-guard.sh"; mv "$C/inst/routines/cron-guard.sh.real" "$C/inst/routines/cron-guard.sh"
: > "$CL"; GRC=0
PATH="$C/inst/bin:$PATH" CURL_LOG="$CL" PUSH_GUARD_OVERRIDE=send-anyway DOGANY_SHARED_HOME="$C/home" \
  bash "$C/inst/routines/cron-guard.sh" --label "com.test.v19.pack-lag-0317" \
    --friendly-name pack-lag --log "$C/inst/pl.stdout.log" \
    -- /bin/bash "$C/inst/routines/pack-lag.sh" > "$SB/v19.gout" 2>&1 || GRC=$?
if [[ "$GRC" -eq 0 ]] && ! grep -q 'send' "$CL" && ! grep -q 'ROUTINE FAILED' "$SB/v19.gout"; then
  ok "V19c under cron-guard: rc 0, no cron-guard failure push, nothing reached the owner"
else bad "V19c grc=$GRC sends=$(sends "$CL") gout=$(tail -5 "$SB/v19.gout")"; fi

# --- V20: BEHIND + evidence on a non-estate host ----------------------------
C="$SB/v20"; mk_repo "$C/repo" pack/dev/v1.3.0 pack/dev/v1.3.1
mk_repo "$C/repo2" pack/kit/v2.20.3
mk_reg "$C/home" "dev.repo=$C/repo" "dev.channel=stable" "kit.repo=$C/repo2" "kit.channel=stable"
mk_inst "$C/inst" "dev@1.3.0,kit@2.20.1"
mk_receipt "$C/inst" dev 1.3.0 pack/dev/v1.3.0
mk_receipt "$C/inst" kit 2.20.1 -
run v20 "$C/inst" "$C/home"
if [[ "$RC" -eq 0 ]] && grep -q '^> dev: BEHIND' "$SINK" && ! grep -q 'UNTAGGED' "$SINK"; then ok "V20 the owner's BEHIND push carries the BEHIND line only"; else bad "V20 owner push wrong (rc=$RC): $(cat "$SINK")"; fi
if grep -q 'kit: UNTAGGED' "$SINK.operator" && ! grep -q 'BEHIND' "$SINK.operator"; then ok "V20 the evidence line goes to the operator only"; else bad "V20 operator push wrong: $(cat "$SINK.operator")"; fi

say ""
say "pack-lag selftest: $PASS pass, $FAIL fail"
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
