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
