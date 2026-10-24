from __future__ import annotations

from yt_dlp import YoutubeDL
from ytmusicapi import YTMusic

from .config import COOKIE_FILE, YT_PROXY


def _options() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 1,
        "ignoreerrors": True,
    }
    if YT_PROXY:
        opts["proxy"] = YT_PROXY
    if COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
    return opts


def search_source(query: str, source: str, limit: int = 8) -> list[dict]:
    if source == "youtube_music":
        # YouTube Music returns structured song/artist metadata instead of
        # noisy arbitrary video titles. Public catalogue search needs no login.
        music = YTMusic()
        entries = music.search(query, filter="songs", limit=limit)
        results: list[dict] = []
        for entry in entries:
            video_id = entry.get("videoId")
            if not video_id:
                continue
            artists = entry.get("artists") or []
            artist = ", ".join(
                item.get("name", "").strip() for item in artists if item.get("name")
            )
            results.append({
                "source": source,
                "url": f"https://music.youtube.com/watch?v={video_id}",
                "title": entry.get("title") or "",
                "duration": entry.get("duration_seconds"),
                "uploader": artist,
            })
        return results
    elif source == "youtube":
        search_term = f"ytsearch{limit}:{query}"
    elif source == "soundcloud":
        search_term = f"scsearch{limit}:{query}"
    else:
        raise ValueError(f"Unsupported audio source: {source}")

    with YoutubeDL(_options()) as ydl:
        info = ydl.extract_info(search_term, download=False)

    results: list[dict] = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        entry_id = entry.get("id")
        url = entry.get("webpage_url") or entry.get("original_url")
        if source == "youtube":
            if entry_id:
                url = f"https://www.youtube.com/watch?v={entry_id}"
            elif url and not url.startswith(("https://", "http://")):
                continue
        else:
            # SoundCloud flat search results should expose a canonical URL.
            if not url:
                url = entry.get("url")
            if url and not url.startswith(("https://", "http://")):
                continue

        if not url:
            continue
        results.append({
            "source": source,
            "url": url,
            "title": entry.get("title") or "",
            "duration": entry.get("duration"),
            "uploader": entry.get("uploader") or entry.get("channel") or "",
        })
    return results
