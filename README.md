# Rhythm

Telegram music bot focused on accurate song matching and clean audio metadata.

## Search and metadata

- Apple/iTunes and Deezer provide independent structured artist, track title, album, duration, ISRC (when available), and cover-art metadata. Apple/iTunes is preferred when both catalogs identify the same recording.
- YouTube Music is searched separately for structured song results and is used as the audio source.
- Catalog and YouTube Music results are merged and de-duplicated instead of stopping when either source returns results.
- Search ranking compares artist and title independently; explicit artist/title queries preserve the user's spelling when catalog metadata is not confident.
- Obvious alternate edits (speed-up, slowed, nightcore, remix, mashup, bass boost) and large duration mismatches are rejected during audio resolution.
- MP3 tags are written from the selected metadata; source video/channel labels are not used as the preferred artist/title when catalog data is available.

The project uses public/unofficial source interfaces, which can change or be rate-limited. A high matching score is a heuristic, not a guarantee that every recording is the exact master. Please respect copyright and each service's terms.

## Runtime

The Docker image starts the current implementation with `python -m rhythm` (not the legacy `main.py` engine).

## Environment

- `BOT_TOKEN` — required Telegram bot token.
- `YT_PROXY` — optional HTTP(S) proxy for yt-dlp.
- `YOUTUBE_COOKIES_B64` — optional base64-encoded Netscape cookie file; keep it in Railway Variables only, never commit it.
- `MIN_TRACK_SECONDS` — minimum audio duration (default 60).
- `MAX_TRACK_SECONDS` — maximum audio duration (default 900).

## Deployment

Deploy using the repository Dockerfile. Railway's service-level Start Command, if explicitly overridden in the dashboard, must be cleared or set to `python -m rhythm` so it does not launch the legacy engine.


## Playlist import (experimental)

Send a public playlist URL to the Telegram bot to attempt importing its track list. The bot processes entries sequentially, searches available audio sources for each recording, sends successful matches as Telegram audio, and reports failures at the end. Jobs are queued with a small concurrency limit to avoid overwhelming the service.

Supported URL domains are recognized for Yandex Music, Apple Music, Spotify, and YouTube. **Recognition does not guarantee that a provider's playlist can be read**: this first implementation relies on yt-dlp extractors, and some providers may require a dedicated API adapter, credentials, or a public playlist. It does not bypass DRM or subscription restrictions. Very large playlists can take a long time and may encounter Telegram/source rate limits.
