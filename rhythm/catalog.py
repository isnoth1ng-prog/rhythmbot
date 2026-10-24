from __future__ import annotations

import asyncio

import aiohttp
from rapidfuzz import fuzz, utils

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_SEARCH_URL = "https://api.deezer.com/search/track"


def _score(query: str, candidate: str) -> float:
    return float(fuzz.WRatio(query, candidate, processor=utils.default_process))


async def _search_itunes(session: aiohttp.ClientSession, query: str, limit: int) -> list[dict]:
    params = {"term": query, "entity": "song", "media": "music", "limit": limit}
    async with session.get(ITUNES_SEARCH_URL, params=params) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)

    tracks = []
    for item in payload.get("results", []):
        title = (item.get("trackName") or "").strip()
        artist = (item.get("artistName") or "").strip()
        if not title or not artist:
            continue
        duration_ms = item.get("trackTimeMillis")
        duration = round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None
        artwork = item.get("artworkUrl100") or ""
        if artwork:
            artwork = artwork.replace("100x100bb", "600x600bb")
        tracks.append({
            "title": title,
            "artist": artist,
            "album": (item.get("collectionName") or "").strip(),
            "duration": duration,
            "artwork_url": artwork,
            "track_url": item.get("trackViewUrl") or "",
            "preview_url": item.get("previewUrl") or "",
            "isrc": (item.get("isrc") or "").strip(),
            "metadata_score": _score(query, f"{artist} - {title}"),
            "metadata_source": "Apple/iTunes",
        })
    return tracks


async def _search_deezer(session: aiohttp.ClientSession, query: str, limit: int) -> list[dict]:
    params = {"q": query, "limit": limit}
    async with session.get(DEEZER_SEARCH_URL, params=params) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    if payload.get("error"):
        raise RuntimeError(f"Deezer search returned an API error: {payload['error']}")

    tracks = []
    for item in payload.get("data", []):
        title = (item.get("title") or "").strip()
        artist = str((item.get("artist") or {}).get("name") or "").strip()
        if not title or not artist:
            continue
        album = item.get("album") or {}
        duration = item.get("duration")
        cover = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or ""
        tracks.append({
            "title": title,
            "artist": artist,
            "album": str(album.get("title") or "").strip(),
            "duration": duration if isinstance(duration, (int, float)) else None,
            "artwork_url": cover,
            "track_url": item.get("link") or "",
            "preview_url": item.get("preview") or "",
            "isrc": (item.get("isrc") or "").strip(),
            "metadata_score": _score(query, f"{artist} - {title}"),
            "metadata_source": "Deezer",
        })
    return tracks


async def search_tracks(query: str, limit: int = 8) -> list[dict]:
    """Search two structured music catalogs and merge duplicate recordings.

    Apple/iTunes and Deezer return artist/title/album/duration independently of
    the video source used to resolve audio. Failures are isolated per catalog.
    """
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(
            _search_itunes(session, query, limit),
            _search_deezer(session, query, limit),
            return_exceptions=True,
        )

    tracks: list[dict] = []
    for name, result in zip(("Apple/iTunes", "Deezer"), results):
        if isinstance(result, Exception):
            # A catalog outage should not make the whole search fail.
            import logging
            logging.getLogger("rhythm.catalog").warning(
                "%s metadata search failed for %r: %s", name, query, result
            )
            continue
        tracks.extend(result)

    # Prefer Apple metadata when the same recording is available from both
    # catalogs; use ISRC first, then normalized artist/title as a fallback.
    merged: dict[tuple[str, str], dict] = {}
    for item in tracks:
        isrc = str(item.get("isrc") or "").strip().upper()
        key = ("isrc", isrc) if isrc else (
            "name",
            f"{item['artist'].casefold()}|{item['title'].casefold()}",
        )
        previous = merged.get(key)
        if previous is None:
            merged[key] = item
            continue
        priority = {"Apple/iTunes": 2, "Deezer": 1}
        if priority.get(item.get("metadata_source"), 0) > priority.get(previous.get("metadata_source"), 0):
            merged[key] = item

    tracks = list(merged.values())
    tracks.sort(
        key=lambda track: (
            float(track.get("metadata_score") or 0),
            1 if track.get("metadata_source") == "Apple/iTunes" else 0,
        ),
        reverse=True,
    )
    return tracks[:max(limit, 1)]
