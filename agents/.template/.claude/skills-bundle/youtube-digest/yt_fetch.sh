#!/usr/bin/env bash
# yt_fetch.sh -- pull YouTube transcript + metadata as clean plain text.
# usage: yt_fetch.sh <youtube-url> [outfile]
# output: prints META lines to stdout, writes clean transcript to outfile.
# exit 0 = transcript ok; exit 3 = no subtitles found; other = hard error.
set -euo pipefail

URL="${1:-}"
OUT="${2:-/tmp/yt_transcript.txt}"
if [ -z "$URL" ]; then echo "ERR: no url" >&2; exit 2; fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- metadata (title / channel / upload date / duration) ---
# one --print per field; yt-dlp emits them on separate lines in order.
META="$(yt-dlp --no-warnings --skip-download \
  --print '%(title)s' --print '%(channel)s' \
  --print '%(upload_date)s' --print '%(duration_string)s' \
  "$URL" 2>/dev/null || true)"
echo "TITLE=$(printf '%s' "$META" | sed -n '1p')"
echo "CHANNEL=$(printf '%s' "$META" | sed -n '2p')"
echo "UPLOAD=$(printf '%s' "$META" | sed -n '3p')"
echo "DURATION=$(printf '%s' "$META" | sed -n '4p')"

# --- subtitles: prefer manual ko, then manual en, then auto ko, then auto en ---
# grab whatever exists; yt-dlp writes <id>.<lang>.vtt into WORK.
yt-dlp --no-warnings --skip-download \
  --write-subs --write-auto-subs \
  --sub-langs "ko,ko-orig,en,en-orig" --sub-format vtt \
  -o "$WORK/sub.%(ext)s" "$URL" >/dev/null 2>&1 || true

# pick best available vtt by priority.
pick=""
for lang in ko ko-orig en en-orig; do
  f="$WORK/sub.$lang.vtt"
  [ -f "$f" ] && { pick="$f"; echo "SUBLANG=$lang"; break; }
done
# fallback: any vtt at all.
if [ -z "$pick" ]; then
  pick="$(ls "$WORK"/*.vtt 2>/dev/null | head -1 || true)"
  [ -n "$pick" ] && echo "SUBLANG=other"
fi
if [ -z "$pick" ]; then
  echo "NO_SUBTITLES=1"
  exit 3
fi

# --- vtt -> clean plain text ---
# drop headers, timestamp lines, cue tags, blank lines; collapse consecutive dups
# (auto-captions repeat rolling lines). join into flowing text.
python3 - "$pick" "$OUT" <<'PY'
import sys, re
src, out = sys.argv[1], sys.argv[2]
lines = []
with open(src, encoding="utf-8", errors="ignore") as f:
    for raw in f:
        s = raw.rstrip("\n")
        if s.startswith("WEBVTT") or s.startswith("Kind:") or s.startswith("Language:"):
            continue
        if "-->" in s:
            continue
        if re.fullmatch(r"\d+", s.strip()):
            continue
        s = re.sub(r"<[^>]+>", "", s)          # inline cue tags <c> timestamps
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            continue
        if lines and lines[-1] == s:            # dedup exact consecutive
            continue
        lines.append(s)
# second pass: remove lines fully contained in the previous (rolling caption overlap)
clean = []
for s in lines:
    if clean and (s in clean[-1] or clean[-1] in s):
        # keep the longer of the two
        if len(s) > len(clean[-1]):
            clean[-1] = s
        continue
    clean.append(s)
text = " ".join(clean)
with open(out, "w", encoding="utf-8") as f:
    f.write(text + "\n")
print(f"CHARS={len(text)}", file=sys.stderr)
PY

echo "OUTFILE=$OUT"
exit 0
