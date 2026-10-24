# Rhythm

Telegram music bot with Apple/iTunes metadata, strict title/artist matching, and multiple audio-source fallbacks.

## Runtime

The Docker image starts the current implementation with `python -m rhythm` (not the legacy `main.py` engine).

Search metadata is taken from Apple/iTunes where available. Audio-source fallback order is SoundCloud, YouTube Music, then YouTube, so YouTube is not the first download dependency. YouTube Music downloads may still be subject to YouTube's restrictions; SoundCloud availability and licensing vary by track.

## Environment

- `BOT_TOKEN` — required Telegram bot token.
- `YT_PROXY` — optional HTTP(S) proxy for yt-dlp sources.
- `YOUTUBE_COOKIES_B64` — optional base64-encoded Netscape cookie file; keep it in Railway Variables only, never commit it.
- `MIN_TRACK_SECONDS` — minimum audio duration (default 60).
- `MAX_TRACK_SECONDS` — maximum audio duration (default 900).

## Deployment

Deploy using the repository Dockerfile. Railway's service-level Start Command, if explicitly overridden in the dashboard, must be cleared or set to `python -m rhythm` so it does not launch the legacy engine.
