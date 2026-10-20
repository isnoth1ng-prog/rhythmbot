# Rhythm Telegram Bot

A clean, modular Telegram audio bot. Track metadata is resolved through Apple's public iTunes Search API; audio discovery and retrieval are isolated behind source adapters.

## Architecture

- `rhythm/catalog.py` — Apple/iTunes metadata lookup and normalization.
- `rhythm/sources.py` — YouTube and SoundCloud search adapters via yt-dlp.
- `rhythm/downloader.py` — bounded download/conversion and temporary-file handling.
- `rhythm/metadata.py` — MP3 ID3 tags and cover embedding.
- `rhythm/service.py` — matching/scoring pipeline and source fallback.
- `rhythm/__main__.py` — Telegram bot entry point.

The bot rejects tracks shorter than 60 seconds or longer than 15 minutes, and refuses weak metadata/audio matches rather than blindly returning the first result. Search and download run off the event loop.

## Local run

1. Install Python 3.11+ and FFmpeg.
2. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
3. Install dependencies: `pip install -r requirements.txt`.
4. Run: `python -m rhythm`.

## Railway

Create a Railway service from this GitHub repository and deploy using the included Dockerfile. Add `BOT_TOKEN` in Variables. `YT_PROXY` and `YOUTUBE_COOKIES_B64` are optional. Keep real cookies only in Railway Variables; never commit them.

## Important

Source availability and platform anti-bot restrictions can change. This bot does not guarantee every track is available. Use it only for audio you are authorized to download and redistribute, and follow the source platforms' terms.
