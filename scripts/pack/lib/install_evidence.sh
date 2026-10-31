#!/bin/bash
# install_evidence.sh -- install completion/source evidence contract
# (DGN-1456 / DGN-1460, design: files/reports/evidence-axis-20260915.md).
#
# THE CONTRACT. The installer is the only writer; install (requires_kit
# satisfaction) and update (plan/apply) are the consumers, and both read the
# SAME receipt through the SAME reader. The ledger line in .instance.conf
# (DOGANY_PACKS=id@version) is a PROJECTION for compatibility -- the receipt
# is the authoritative record of "this source's delivery finished and the
# payload is still there".
#
# Files (under <root>/config/packs/):
#   <id>.receipt.inprogress   durable marker written BEFORE the first byte of
#                             payload mutates the instance. Its mere presence
#                             means an install run started and never issued
#                             delivery-complete -- readers refuse to treat the
#                             pack as installed while it exists. NOT removed
#                             by any trap: surviving a kill -9 is the point
#                             (the DGN-1460 orphan-stamp shape).
#   <id>.receipt              issued ONLY after every delivery step ran and
#                             the expected-anchor list (fixed BEFORE delivery
#                             started -- never a list of "copies that
#                             happened to succeed") re-verified against the
#                             instance. Atomic rename; replaces the previous
#                             generation.
#
# Receipt format: plain key=value lines (shell/py-parseable, no jq):
#   schema=1
#   pack=<id>
#   version=<RECORD_VERSION -- preserves the DGN-1469 --source-tag record>
#   class=<kit|pack|legacy>
#   exec=<pid>-<timestamp>
#   status=<in-progress|delivery-complete>
#   source=<tag:<ref> | untagged-local:<reason>>   (optional -- DGN-1489: how
#                             the installed version is tag-backed. Absent for
#                             out-of-scope lanes (non-git fixture sources);
#                             readers ignore unknown keys, so this line is
#                             additive)
#   anchor_count=<N>
#   anchor=<instance-relative path>     (exactly N lines)
#
# Reader verdicts (report §4.5 -- the consumption contract):
#   VERIFIED     rc 0  receipt complete, version matches ledger, every anchor
#                      physically present
#   MISMATCH     rc 3  contradiction: version drift, anchor gone, or a ledger
#                      claim with NO physical evidence at all (the DGN-1456
#                      main-agent ghost: DOGANY_PACKS entry + zero payload)
#   INCOMPLETE   rc 4  an in-progress marker exists -- an install started and
#                      never completed; any older receipt is masked (report
#                      §4.4.2)
#   UNKNOWN      rc 5  no receipt, but a pre-receipt legacy install trace
#                      exists (<id>.skills or <id>.files -- the two files the
#                      two installer generations write NEAR THE END of
#                      delivery). Cannot verify, cannot condemn.
#   CHECK_ERROR  rc 6  the checker itself failed: bad args, unreadable dir,
#                      malformed/truncated receipt, bad enum
#
# .requires_framework and .complete are DELIBERATELY not evidence: the former
# is a mid-run compat cache (the DGN-1460 orphan itself), the latter marks the
# data-dependent completion_verb, not delivery (report §2.1/§2.3).
#
# rc alone is NOT the contract: the reader prints exactly one structured
# EVIDENCE line and the rc must AGREE with the printed verdict. The hardened
# consumer (evidence_consume) validates line shape, verdict allow-list and
# rc<->verdict agreement, and converts ANY disagreement -- empty output,
# garbage token, rc/token mismatch, reader crash -- into CHECK_ERROR. A broken
# checker therefore reads as a failed check, never as a pass (report §5
# "검사기 고장" counterexamples).

_IEV_SELF="${BASH_SOURCE[0]}"

_iev_rc_for() {
  case "$1" in
    VERIFIED) echo 0 ;;
    MISMATCH) echo 3 ;;
    INCOMPLETE) echo 4 ;;
    UNKNOWN) echo 5 ;;
    *) echo 6 ;;
  esac
}

# evidence_begin <root> <pack_id> <version> <class> <anchors-newline-list> [source]
# Writes the in-progress marker atomically. Call BEFORE the first payload
# write. Returns nonzero (no partial file left) on any failure. [source] is
# the DGN-1489 tag-backing record (tag:<ref> / untagged-local:<reason>);
# empty = no source line (out-of-scope lanes stay byte-identical).
evidence_begin() {
  local root="$1" pid="$2" ver="$3" cls="$4" anchors="${5:-}" src="${6:-}"
  local dir="$root/config/packs" tmp n=0 a
  [[ -n "$root" && -n "$pid" && -n "$ver" ]] || return 1
  mkdir -p "$dir" || return 1
  tmp="$(mktemp "$dir/.$pid.receipt.tmp.XXXXXX")" || return 1
  {
    echo "schema=1"
    echo "pack=$pid"
    echo "version=$ver"
    echo "class=$cls"
    echo "exec=$$-$(date +%Y%m%dT%H%M%S)"
    echo "status=in-progress"
    if [[ -n "$src" ]]; then echo "source=$src"; fi
  } > "$tmp" || { rm -f "$tmp"; return 1; }
  while IFS= read -r a; do
    [[ -n "$a" ]] || continue
    echo "anchor=$a" >> "$tmp" || { rm -f "$tmp"; return 1; }
    n=$((n + 1))
  done <<< "$anchors"
  # anchor_count is explicit so a truncated receipt (fewer anchor lines than
  # declared) is detectable -- an empty list is a DECLARED empty list, not a
  # generation failure (report §5, the empty-NM3-list counterexample).
  echo "anchor_count=$n" >> "$tmp" || { rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$dir/$pid.receipt.inprogress" || { rm -f "$tmp"; return 1; }
  return 0
}

# evidence_complete <root> <pack_id>
# Re-verifies every expected anchor against the instance, then promotes the
# in-progress marker to the delivery-complete receipt (atomic) and removes
# the marker. Prints its findings; nonzero return = completion REFUSED and
# the in-progress marker stays (readers keep seeing INCOMPLETE).
evidence_complete() {
  local root="$1" pid="$2"
  local dir="$root/config/packs" inprog tmp missing=0 a
  inprog="$dir/$pid.receipt.inprogress"
  if [[ ! -f "$inprog" ]]; then
    echo "install evidence: no in-progress marker at $inprog -- cannot issue delivery-complete for a run that never declared itself"
    return 1
  fi
  while IFS= read -r a; do
    case "$a" in anchor=*) a="${a#anchor=}" ;; *) continue ;; esac
    # -e resolves symlinks: a link whose target is gone is a missing anchor.
    if [[ ! -e "$root/$a" ]]; then
      echo "install evidence: expected anchor MISSING after delivery: $a"
      missing=$((missing + 1))
    fi
  done < "$inprog"
  if [[ "$missing" -gt 0 ]]; then
    echo "install evidence: $missing expected anchor(s) missing -- delivery-complete REFUSED (in-progress marker kept)"
    return 1
  fi
  tmp="$(mktemp "$dir/.$pid.receipt.tmp.XXXXXX")" || return 1
  sed 's/^status=in-progress$/status=delivery-complete/' "$inprog" > "$tmp" \
    || { rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$dir/$pid.receipt" || { rm -f "$tmp"; return 1; }
  rm -f "$inprog"
  echo "install evidence: delivery-complete receipt issued (config/packs/$pid.receipt)"
  return 0
}

# evidence_read <root> <pack_id> [ledger_version]
# Prints exactly one "EVIDENCE schema=1 pack=<id> verdict=<V> detail=<...>"
# line; returns the verdict's mapped rc. Precondition at every call site: the
# ledger (or a dependency declaration) CLAIMS this pack is installed here.
evidence_read() {
  local root="$1" pid="$2" ledger_ver="${3:-}"
  local dir receipt inprog verdict="" detail=""
  local r_schema="" r_pack="" r_ver="" r_status="" r_count="" n_anchor=0 miss=0 k v a

  _iev_emit() {
    echo "EVIDENCE schema=1 pack=$pid verdict=$1 detail=$2"
    return "$(_iev_rc_for "$1")"
  }

  if [[ -z "$root" || -z "$pid" ]]; then
    _iev_emit CHECK_ERROR "bad arguments (root='$root' pack='$pid')"; return
  fi
  if [[ ! -d "$root" ]]; then
    _iev_emit CHECK_ERROR "instance root not a directory: $root"; return
  fi
  dir="$root/config/packs"
  receipt="$dir/$pid.receipt"
  inprog="$dir/$pid.receipt.inprogress"

  if [[ -e "$inprog" ]]; then
    v="$(sed -n 's/^exec=//p' "$inprog" 2>/dev/null | head -n1)"
    _iev_emit INCOMPLETE "in-progress marker present (exec=${v:-unreadable}) -- an install run started and never issued delivery-complete; any prior receipt is masked"; return
  fi

  if [[ -e "$receipt" ]]; then
    if [[ ! -r "$receipt" ]]; then
      _iev_emit CHECK_ERROR "receipt unreadable: $receipt"; return
    fi
    while IFS= read -r k; do
      case "$k" in
        schema=*) r_schema="${k#schema=}" ;;
        pack=*) r_pack="${k#pack=}" ;;
        version=*) r_ver="${k#version=}" ;;
        status=*) r_status="${k#status=}" ;;
        anchor_count=*) r_count="${k#anchor_count=}" ;;
        anchor=*)
          a="${k#anchor=}"
          n_anchor=$((n_anchor + 1))
          if [[ ! -e "$root/$a" ]]; then
            miss=$((miss + 1))
            detail="${detail:+$detail; }anchor gone: $a"
          fi
          ;;
      esac
    done < "$receipt"
    if [[ "$r_schema" != "1" ]]; then
      _iev_emit CHECK_ERROR "receipt schema '$r_schema' not supported"; return
    fi
    if [[ "$r_pack" != "$pid" || -z "$r_ver" ]]; then
      _iev_emit CHECK_ERROR "receipt malformed (pack='$r_pack' version='$r_ver')"; return
    fi
    if [[ "$r_status" != "delivery-complete" ]]; then
      _iev_emit CHECK_ERROR "receipt status '$r_status' is not a valid completed-receipt state"; return
    fi
    if ! [[ "$r_count" =~ ^[0-9]+$ ]] || [[ "$r_count" -ne "$n_anchor" ]]; then
      _iev_emit CHECK_ERROR "receipt truncated: anchor_count=$r_count but $n_anchor anchor line(s)"; return
    fi
    if [[ -n "$ledger_ver" && "$ledger_ver" != "$r_ver" ]]; then
      _iev_emit MISMATCH "ledger claims $pid@$ledger_ver but the receipt records $r_ver"; return
    fi
    if [[ "$miss" -gt 0 ]]; then
      _iev_emit MISMATCH "$miss of $n_anchor delivered anchor(s) no longer present ($detail)"; return
    fi
    _iev_emit VERIFIED "receipt $pid@$r_ver, anchors $n_anchor/$n_anchor present"; return
  fi

  # No receipt. A pre-receipt install left one of the two end-of-delivery
  # traces; anything else claiming "installed" is the DGN-1456 ghost shape.
  if [[ -e "$dir/$pid.skills" || -e "$dir/$pid.files" ]]; then
    _iev_emit UNKNOWN "no receipt; pre-receipt legacy install trace present -- cannot verify delivery"; return
  fi
  _iev_emit MISMATCH "no receipt and no legacy install trace -- the ledger claims an install this instance has no physical evidence of (ghost entry)"; return
}

# evidence_consume <root> <pack_id> [ledger_version]
# The hardened consumer plan/apply/installer share. Runs the reader as a
# SUBPROCESS (so a reader crash is observable, not fatal to the caller) and
# validates output shape + verdict allow-list + rc<->verdict agreement.
# Echoes "<VERDICT>|<detail>" and always returns 0 -- callers branch on the
# verdict token. DOGANY_EVIDENCE_LIB overrides the reader executable (test
# fault-injection seam, same pattern as DOGANY_APPLY_INSTALLER).
evidence_consume() {
  local root="$1" pid="$2" ledger_ver="${3:-}"
  local lib="${DOGANY_EVIDENCE_LIB:-$_IEV_SELF}" out rc=0 verdict detail want_rc
  out="$(bash "$lib" read --root "$root" --pack "$pid" ${ledger_ver:+--ledger-version "$ledger_ver"} 2>/dev/null)" || rc=$?
  if [[ "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" != "1" ]]; then
    echo "CHECK_ERROR|reader output is not exactly one line (rc=$rc)"; return 0
  fi
  case "$out" in
    "EVIDENCE schema=1 pack=$pid verdict="*" detail="*) : ;;
    *) echo "CHECK_ERROR|reader output malformed (rc=$rc): ${out:-<empty>}"; return 0 ;;
  esac
  verdict="${out#*verdict=}"; verdict="${verdict%% *}"
  detail="${out#* detail=}"
  case "$verdict" in
    VERIFIED|MISMATCH|INCOMPLETE|UNKNOWN|CHECK_ERROR) : ;;
    *) echo "CHECK_ERROR|reader verdict '$verdict' not in the allow-list (rc=$rc)"; return 0 ;;
  esac
  want_rc="$(_iev_rc_for "$verdict")"
  if [[ "$rc" -ne "$want_rc" ]]; then
    echo "CHECK_ERROR|reader rc=$rc disagrees with verdict $verdict (expected rc=$want_rc)"; return 0
  fi
  echo "$verdict|$detail"
  return 0
}

# ---- CLI dispatch (only when executed, not sourced) -------------------------
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  set -uo pipefail
  cmd="${1:-}"; shift || true
  case "$cmd" in
    read)
      ROOT="" PID="" LVER=""
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --root) ROOT="${2:-}"; shift 2 ;;
          --pack) PID="${2:-}"; shift 2 ;;
          --ledger-version) LVER="${2:-}"; shift 2 ;;
          *) echo "EVIDENCE schema=1 pack=${PID:-?} verdict=CHECK_ERROR detail=unknown argument '$1'"; exit 6 ;;
        esac
      done
      evidence_read "$ROOT" "$PID" "$LVER"
      exit $?
      ;;
    *)
      echo "usage: install_evidence.sh read --root <instance-root> --pack <id> [--ledger-version <v>]" >&2
      exit 6
      ;;
  esac
fi
