# bridge

An original Telegram <-> Claude bridge built directly on the official
[`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/).

It exposes a Claude Code session over a Telegram bot: send a text (or voice)
message, the bot drives a persistent SDK streaming client scoped to a project
directory, streams the reply back as a live-updating message, and turns
numbered decision menus into tappable inline buttons.

## Highlights

- Persistent per-user streaming SDK client (one in-flight query, queued input).
- Live draft-message streaming with code-fence-aware final splitting.
- `[[OPTIONS]]` marker -> inline buttons, with a conservative Haiku auto-classifier.
- `send_file::` marker for explicit, opt-in file delivery (in-root auto, out-of-root confirmed).
- Per-turn timeout with partial-output preservation, silent auto-resume, and a one-tap continue button.
- Local (offline) voice input via faster-whisper + ffmpeg. Input only; no TTS.
- 24h auto-new-session, cross-process resume guard, allowlist + stale-message drop.

## Run

```
pip install -r requirements.txt
python -m bridge --path /path/to/project
```

Configuration lives in `<project>/.telegram_bot/.env` (see `.env.example`).

This is an original implementation. It is not a fork; behavior was reimplemented
against the public SDK. Licensed MIT.

## Machine voice defaults

Copy `voice.conf.example` to `~/.dogany/voice.conf` and edit its `model`,
`language`, and `vocabulary` keys. This uses the same machine root as
`~/.dogany/memory-queue`. The example is a seed, never an automatically applied
configuration. Settings are read at bridge startup.

The instance `.telegram_bot/.env` wins over inherited environment variables,
which win over machine model/language defaults. Machine defaults precede the
shared/package fallback files. Existing instance `LOCAL_WHISPER_MODEL` pins
must be removed to inherit a machine model change.

Without `WHISPER_LANGUAGE` or a machine `language`, speech language derives
from `LOCALE`, then `AGENT_LANG`, then `en`; region suffixes are removed.
An explicit empty language or `auto` enables language detection. Speech locale
is read before the UI's narrower locale normalization.

`WHISPER_VOCABULARY` adds comma/newline-separated phrases to machine
`vocabulary`. Duplicates are removed in order; spaces within phrases survive.
The transcriber inspects the installed API and uses `hotwords` when available,
otherwise `initial_prompt`. These are recognition hints, not guaranteed spelling.

Behind the explicit phrases comes a derived vocabulary read from
`.telegram_bot/voice-vocab.json` (schema 1, `terms` in priority order). The
bridge core only reads that file; an absent or malformed file adds nothing.
The whole list is deduplicated case-insensitively and cut to the recognizer's
prompt budget (`max_length // 2 - 1` tokens, 223 for Whisper), counted with the
loaded model's tokenizer, or with UTF-8 bytes when no tokenizer is exposed.
The cut is computed once per transcriber, not per message.

`faster-whisper` is required even for core-only installs. Mint verifies its
import; updates with an existing bridge venv fail before version stamping if
its import is broken or missing. Models still load on first transcription;
installing the dependency does not download a model.
