---
name: youtube-digest
display_name: 유튜브 요약
description: >
  Fires when __USER_LABEL__ drops a YouTube link (or asks to summarize / digest a YouTube
  video). Pulls the video's transcript and metadata, then reports a Korean
  digest -- what the video says, key points, takeaways. Triggers (KO): a bare
  youtube.com/ or youtu.be/ URL, "이 영상 요약해줘", "유튜브 요약", "이거 무슨 내용이야",
  "영상 내용 정리해줘", "이 링크 소화해서 알려줘", "핵심만 뽑아줘", "전문 줘"(full transcript).
  Triggers (EN): "summarize this video", "what's this youtube about", "digest
  this link", "tldr this video", "give me the transcript". Handles BOTH the
  short digest (default) AND the full-transcript request. Output = Korean digest
  message; full transcript delivered as a file via send_file on request.
---

# youtube-digest -- YouTube link -> Korean digest

Take YouTube URL. Get transcript + metadata. Report digest in Korean.

## trigger
- __USER_LABEL__ pastes a YouTube URL (youtube.com/watch, youtu.be/, /shorts/, /live/).
- __USER_LABEL__ asks to summarize / digest / "무슨 내용" a YouTube video.
- __USER_LABEL__ asks for the 전문 / full transcript of a video.

## tools
- `yt_fetch.sh <url> [outfile]` -- pulls metadata + clean plain-text transcript.
  - prints META lines: TITLE / CHANNEL / UPLOAD / DURATION / SUBLANG / OUTFILE.
  - writes transcript to outfile (default /tmp/yt_transcript.txt).
  - exit 3 + `NO_SUBTITLES=1` -> video has no usable captions.
- yt-dlp already installed (/opt/homebrew/bin/yt-dlp).

## steps
1. get URL from __USER_LABEL__ message. strip tracking junk is not needed; pass as-is in quotes.
2. run:
   `.claude/skills/youtube-digest/yt_fetch.sh "<url>" /tmp/yt_digest.txt`
   read the META lines from stdout (title, channel, upload date, duration, sublang).
3. handle no-subtitle case:
   - if exit 3 / NO_SUBTITLES -> tell __USER_LABEL__ the video has no captions, cannot digest
     from transcript. offer nothing fake. stop.
4. read transcript file. decide effort by size:
   - short/medium (< ~40k chars): read + summarize inline yourself.
   - long (>= ~40k chars, or long lecture): delegate to a subagent (model=sonnet)
     -- "long transcript, sonnet for summarize, cheap + good at wrangling".
     per delegation-visibility rule: when the subagent finishes, report result to __USER_LABEL__ FIRST,
     as lead message, before any follow-up.
5. build digest (Korean). do NOT dump raw transcript. structure:
   - one header line: 채널 / 제목 / 길이 / 업로드일 (only fields that exist).
   - 핵심 내용: crisp bullets. concrete numbers, claims, conclusions kept.
   - if useful: one-line 전반 메시지 / takeaway at the end.
   - keep it tight; no per-step narration.
6. offer full transcript only if __USER_LABEL__ wants it (do not auto-send). on request,
   deliver the transcript file via a standalone `send_file::` line.

## notes
- upload date comes as YYYYMMDD -> render human ("2026-07-16" or "7월 16일").
- SUBLANG tells caption source (ko manual / ko auto-translate / en / other).
  auto-captions can be rough -> summarize meaning, do not quote verbatim as exact.
- facts with exact values (price/number/spec) inside the video are the SPEAKER's
  claim, not verified truth. attribute to the video, don't assert as fact.
- multiple links in one message -> process each, digest each separately.
- model routing: inline summarize = you; long-transcript summarize = subagent sonnet.

## tier
- BEST-EFFORT. description auto-fire on YouTube links. no hook.
