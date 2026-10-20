from __future__ import annotations

import aiohttp
from rapidfuzz import fuzz, utils

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


def _score(query: str, candidate: str) -> float:
    return float(fuzz.WRatio(query, candidate, processor=utils.default_process))


async def search_tracks(query: str, limit: int = 8) -> list[dict]:
    params = {"term": query, "entity": "song", "media": "music", "limit": limit}
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
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
        label = f"{artist} - {title}"
        tracks.append({
            "title": title,
            "artist": artist,
            "album": (item.get("collectionName") or "").strip(),
            "duration": duration,
            "artwork_url": artwork,
            "track_url": item.get("trackViewUrl"),
            "metadata_score": _score(query, label),
            "metadata_source": "Apple/iTunes",
        })
    tracks.sort(key=lambda t: t["metadata_score"], reverse=True)
    return tracks
