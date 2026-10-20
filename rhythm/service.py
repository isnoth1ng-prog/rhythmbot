from __future__ import annotations

import asyncio
from rapidfuzz import fuzz, utils

from .catalog import search_tracks
from .config import MAX_TRACK_SECONDS, MIN_TRACK_SECONDS
from .downloader import download_mp3
from .sources import search_source


def _similarity(left: str, right: str) -> float:
    return float(fuzz.WRatio(left, right, processor=utils.default_process))


def _audio_score(result: dict, track: dict) -> float:
    title = result.get("title") or ""
    uploader = result.get("uploader") or ""
    expected = f"{track.get('artist', '')} - {track.get('title', '')}"
    title_score = _similarity(title, expected)
    title_only_score = _similarity(title, track.get("title", ""))
    artist_score = _similarity(uploader, track.get("artist", "")) if uploader else 0
    score = max(title_score, title_only_score * 0.85 + artist_score * 0.15)
    source_duration = result.get("duration")
    target_duration = track.get("duration")
    if isinstance(source_duration, (int, float)):
        if not MIN_TRACK_SECONDS <= source_duration <= MAX_TRACK_SECONDS:
            return 0
        if isinstance(target_duration, (int, float)) and target_duration > 0:
            difference = abs(source_duration - target_duration)
            if difference > 45:
                score -= min(24, difference / 4)
            elif difference > 20:
                score -= 8
    return score


async def resolve_track(query: str) -> tuple[dict, dict]:
    catalog_tracks = await search_tracks(query)
    if catalog_tracks and catalog_tracks[0]["metadata_score"] >= 52:
        track = catalog_tracks[0]
        # Album is optional, but if present it is part of the clean ID3 metadata.
        track["album"] = track.get("album") or ""
    else:
        # If the catalog cannot confidently identify the query, don't invent metadata.
        raise LookupError("Не смог уверенно определить исполнителя и название через Apple Music/iTunes. Попробуй написать «исполнитель — название».")

    search_query = f'{track["artist"]} {track["title"]} official audio'
    for source in ("youtube", "soundcloud"):
        try:
            results = await asyncio.to_thread(search_source, search_query, source, 5)
        except Exception:
            results = []
        ranked = sorted(
            (( _audio_score(item, track), item) for item in results if item.get("url")),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not ranked:
            continue
        score, candidate = ranked[0]
        if score < 66:
            continue
        try:
            path, info = await download_mp3(candidate["url"])
            # If the extractor gives us a duration, enforce limits again after extraction.
            duration = info.get("duration")
            if isinstance(duration, (int, float)) and not MIN_TRACK_SECONDS <= duration <= MAX_TRACK_SECONDS:
                from .downloader import cleanup
                cleanup(path)
                continue
            return track, {
                "path": path,
                "source": source,
                "source_title": candidate.get("title") or info.get("title") or "",
                "match_score": round(score),
            }
        except Exception:
            continue
    raise LookupError("Не нашёл достаточно точное совпадение или источник не дал скачать аудио. Попробуй другое написание; случайный трек вместо нужного я отправлять не буду.")
