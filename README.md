# Rhythm

Telegram music bot focused on accurate song matching and clean audio metadata.

## Search and metadata

- Apple/iTunes provides canonical artist, track title, album, duration, and cover art when a confident catalog match exists.
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
