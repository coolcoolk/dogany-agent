#!/bin/bash
# life-engine.sh -- resolve the life-data engine's file stem (DGN-1238).
#
# The engine has shipped under two stems: `lifekit` (original) and `kit_life`
# (pack rename, 2026-08-28). Live fleets exist on BOTH coordinates at once,
# so framework code must never hardcode one stem again: it resolves the stem
# per instance, and any future rename can be absorbed by extending
# LIFE_ENGINE_STEMS (or pinning LIFE_ENGINE=) instead of editing N call
# sites. Python twin: routines/lib/life_engine.py -- keep the two resolution
# contracts identical.
#
# Resolution order (life_engine_stem):
#   1. explicit pin: config/agent.conf LIFE_ENGINE=<stem>. Only the pinned
#      stem is considered; a pin whose files are missing resolves to nothing
#      (the configured-but-missing case -- see life_engine_warn_missing).
#   2. probe every known stem against the files the caller needs.
#   3. nothing found -> empty output. The caller MUST keep its pre-existing
#      "kit absent" path, so a kitless mint behaves exactly as before.
#
# set -e safety: every function always returns 0 (empty output = unresolved)
# except life_engine_configured, whose return CODE is its answer and whose
# body cannot fail under set -euo pipefail. Read-only, except the throttle
# stamp/log written by life_engine_warn_missing.

LIFE_ENGINE_STEMS="kit_life lifekit"

# _life_engine_conf_get <file> <key>
# First KEY= line's value, whitespace- and quote-stripped (same normalization
# as routines/lib/conf_reader.py). Never sources the file. Missing file/key
# -> empty output, rc 0.
_life_engine_conf_get() {
  local _lec_line _lec_val
  [ -f "${1:-}" ] || { printf ''; return 0; }
  _lec_line="$(grep -m1 "^${2}=" "$1" 2>/dev/null || true)"
  [ -n "$_lec_line" ] || { printf ''; return 0; }
  _lec_val="${_lec_line#*=}"
  printf '%s' "$_lec_val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
  return 0
}

# life_engine_stem <agent_root> [ext ...]
# Print the resolved stem, or nothing. Each ext names a file the caller
# needs: `conf` means config/<stem>.conf, anything else database/<stem>.<ext>
# (existence check only -- callers keep their own -x / content gates).
# No exts -> ANY engine surface (database/<stem>.{py,db,sh} or the conf)
# resolves the stem.
life_engine_stem() {
  local _les_root="${1:-}" _les_pin _les_stems _les_s _les_e _les_f _les_ok
  shift 2>/dev/null || true
  [ -n "$_les_root" ] || { printf ''; return 0; }
  _les_pin="$(_life_engine_conf_get "$_les_root/config/agent.conf" LIFE_ENGINE)"
  if [ -n "$_les_pin" ]; then _les_stems="$_les_pin"; else _les_stems="$LIFE_ENGINE_STEMS"; fi
  # $(printf ...) is field-split in every sh family (a bare $var is NOT split
  # under zsh) -- keep this form so the loop sees one stem per iteration.
  for _les_s in $(printf '%s' "$_les_stems"); do
    if [ "$#" -eq 0 ]; then
      for _les_e in py db sh; do
        if [ -f "$_les_root/database/$_les_s.$_les_e" ]; then printf '%s' "$_les_s"; return 0; fi
      done
      if [ -f "$_les_root/config/$_les_s.conf" ]; then printf '%s' "$_les_s"; return 0; fi
    else
      _les_ok=1
      for _les_e in "$@"; do
        if [ "$_les_e" = "conf" ]; then _les_f="$_les_root/config/$_les_s.conf"
        else _les_f="$_les_root/database/$_les_s.$_les_e"; fi
        [ -f "$_les_f" ] || { _les_ok=0; break; }
      done
      if [ "$_les_ok" = "1" ]; then printf '%s' "$_les_s"; return 0; fi
    fi
  done
  printf ''
  return 0
}

# life_engine_conf_path <agent_root>
# Print the existing kit conf path (pin honored), or nothing.
life_engine_conf_path() {
  local _lecp_s
  _lecp_s="$(life_engine_stem "${1:-}" conf)"
  [ -n "$_lecp_s" ] && printf '%s' "${1}/config/$_lecp_s.conf"
  return 0
}

# life_engine_configured <agent_root>
# rc 0 iff the instance SAYS it has the life engine: LIFE_ENGINE= pinned,
# KIT= names a life kit, or a known kit conf carries LIFEKIT=on. This is what
# separates "kit absent (normal no-op)" from "kit configured but engine files
# not found" -- the silent-failure class DGN-1238 exists to close.
life_engine_configured() {
  local _lecf_root="${1:-}" _lecf_v _lecf_s
  [ -n "$_lecf_root" ] || return 1
  _lecf_v="$(_life_engine_conf_get "$_lecf_root/config/agent.conf" LIFE_ENGINE)"
  [ -n "$_lecf_v" ] && return 0
  _lecf_v="$(_life_engine_conf_get "$_lecf_root/config/agent.conf" KIT | tr '[:upper:]' '[:lower:]')"
  case "$_lecf_v" in lifekit|kit_life) return 0 ;; esac
  for _lecf_s in $(printf '%s' "$LIFE_ENGINE_STEMS"); do
    _lecf_v="$(_life_engine_conf_get "$_lecf_root/config/$_lecf_s.conf" LIFEKIT)"
    [ "$_lecf_v" = "on" ] && return 0
  done
  return 1
}

# life_engine_warn_missing <agent_root> <caller>
# Non-blocking (never exits non-zero, never blocks the caller) warning for
# the configured-but-not-found case. Throttled to once per day: one stderr
# line + one line appended to .telegram_bot/life-engine.warn.log.
life_engine_warn_missing() {
  local _lew_root="${1:-}" _lew_caller="${2:-unknown}" _lew_dir _lew_stamp _lew_today
  [ -n "$_lew_root" ] || return 0
  _lew_dir="$_lew_root/.telegram_bot"
  _lew_stamp="$_lew_dir/life-engine.warn.stamp"
  _lew_today="$(date '+%Y-%m-%d' 2>/dev/null || true)"
  if [ "$(cat "$_lew_stamp" 2>/dev/null || true)" = "$_lew_today" ]; then return 0; fi
  echo "[life-engine] configured but engine files not found under known stems ($LIFE_ENGINE_STEMS) in $_lew_root -- caller=$_lew_caller (DGN-1238)" >&2 || true
  mkdir -p "$_lew_dir" 2>/dev/null || return 0
  printf '%s' "$_lew_today" > "$_lew_stamp" 2>/dev/null || true
  printf '%s %s: life engine configured but files not found (stems: %s)\n' \
    "$_lew_today" "$_lew_caller" "$LIFE_ENGINE_STEMS" \
    >> "$_lew_dir/life-engine.warn.log" 2>/dev/null || true
  return 0
}
