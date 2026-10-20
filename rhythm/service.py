from __future__ import annotations

import asyncio
import logging
import re

from rapidfuzz import fuzz, utils

from .catalog import search_tracks
from .config import MAX_TRACK_SECONDS, MIN_TRACK_SECONDS
from .downloader import download_mp3, cleanup
from .sources import search_source

log = logging.getLogger("rhythm.service")


def _clean(value: str) -> str:
    value = re.sub(r"\\s*[-–—|]?\\s*Topic\\s*$", "", value or "", flags=re.IGNORECASE)
    return value.strip(" -–—|")


def _artist_query(query: str) -> bool:
    return not any(sep in query for sep in (" — ", " – ", " - ", " | ")) and len(query.split()) <= 4


def _similarity(left: str, right: str) -> float:
    return float(fuzz.WRatio(left, right, processor=utils.default_process))


def _audio_score(result: dict, track: dict, query: str = "") -> float:
    title = result.get("title") or ""
    uploader = result.get("uploader") or ""
    if track.get("artist") and track.get("title"):
        expected = f'{track["artist"]} - {track["title"]}'
        title_score = _similarity(title, expected)
        title_only_score = _similarity(title, track["title"])
        artist_score = _similarity(uploader, track["artist"]) if uploader else 0.0
        score = max(title_score, title_only_score * 0.85 + artist_score * 0.15)
    else:
        # Apple/iTunes is optional: score source results against the user's own query.
        score = _similarity(title, query)
        if uploader:
            score = max(score, _similarity(f"{uploader} - {title}", query))

    source_duration = result.get("duration")
    target_duration = track.get("duration")
    if isinstance(source_duration, (int, float)):
        if not MIN_TRACK_SECONDS <= source_duration <= MAX_TRACK_SECONDS:
            return 0.0
        if isinstance(target_duration, (int, float)) and target_duration > 0:
            difference = abs(source_duration - target_duration)
            if difference > 45:
                score -= min(24, difference / 4)
            elif difference > 20:
                score -= 8
    return score


def _fallback_metadata(query: str, candidate: dict) -> dict:
    source_title = _clean((candidate.get("title") or query).strip())
    artist = _clean((candidate.get("uploader") or "").strip())
    title = source_title
    for separator in (" — ", " – ", " - ", " | "):
        if separator in source_title:
            left, right = source_title.split(separator, 1)
            if left.strip() and right.strip():
                artist, title = left.strip(), right.strip()
                break
    # If the query itself was supplied as "artist — title", prefer that clean split.
    for separator in (" — ", " – ", " - "):
        if separator in query:
            left, right = query.split(separator, 1)
            if left.strip() and right.strip():
                artist, title = left.strip(), right.strip()
                break
    return {
        "title": title or query,
        "artist": artist or "Неизвестный исполнитель",
        "album": "",
        "duration": candidate.get("duration"),
        "artwork_url": "",
        "track_url": candidate.get("url", ""),
        "metadata_score": 0,
        "metadata_source": "audio source fallback",
    }


async def search_options(query: str) -> list[dict]:
    # Artist-only queries get a wider, but capped, selection list.
    artist_query = _artist_query(query)
    try:
        tracks = await search_tracks(query, limit=25 if artist_query else 10)
    except Exception:
        log.exception("Apple/iTunes search failed for %r", query)
        tracks = []
    if tracks:
        if artist_query:
            tracks.sort(key=lambda t: _similarity(query, t.get("artist", "")) * 0.65 + t.get("metadata_score", 0) * 0.35, reverse=True)
            tracks = [t for t in tracks if _similarity(query, t.get("artist", "")) >= 45]
        else:
            tracks = [t for t in tracks if t.get("metadata_score", 0) >= 45]
        if tracks:
            return tracks[:15]

    # Apple is optional; when it has no useful match, offer source results instead.
    fallback, seen = [], set()
    for source in ("youtube", "soundcloud"):
        try:
            results = await asyncio.to_thread(search_source, query, source, 12 if artist_query else 6)
        except Exception:
            log.exception("%s search failed for %r", source, query)
            continue
        for item in results:
            title = _clean(item.get("title") or "")
            artist = _clean(item.get("uploader") or "")
            score = max(_similarity(title, query), _similarity(f"{artist} - {title}", query) if artist else 0)
            key = (title.casefold(), artist.casefold())
            if score < 48 or key in seen:
                continue
            seen.add(key)
            fallback.append({"title": title or query, "artist": artist or "Неизвестный исполнитель",
                "album": "", "duration": item.get("duration"), "artwork_url": "",
                "track_url": item.get("url", ""), "metadata_score": score, "metadata_source": source})
    fallback.sort(key=lambda t: t["metadata_score"], reverse=True)
    return fallback[:15]


async def resolve_track(query: str) -> tuple[dict, dict]:
    track = None
    try:
        catalog_tracks = await search_tracks(query)
        if catalog_tracks:
            best = catalog_tracks[0]
            if best.get("metadata_score", 0) >= 38:
                track = best
                track["album"] = track.get("album") or ""
            else:
                log.info("Apple/iTunes match too weak for query=%r score=%s; trying audio sources directly",
                         query, best.get("metadata_score"))
        else:
            log.info("Apple/iTunes returned no results for query=%r; trying audio sources directly", query)
    except Exception:
        # A metadata provider outage must not prevent source search.
        log.exception("Apple/iTunes catalog request failed for query=%r; falling back to audio sources", query)

    # Search using catalog-normalized artist/title when available; otherwise use the raw user query.
    search_query = f'{track["artist"]} {track["title"]}' if track else query
    diagnostics: list[str] = []
    for source in ("youtube", "soundcloud"):
        try:
            results = await asyncio.to_thread(search_source, search_query, source, 8)
            log.info("%s search query=%r returned %d candidates", source, search_query, len(results))
        except Exception as exc:
            log.warning("%s search failed: %s", source, exc, exc_info=True)
            diagnostics.append(f"{source}: ошибка поиска ({type(exc).__name__})")
            continue

        ranked = sorted(
            ((_audio_score(item, track or {}, query), item) for item in results if item.get("url")),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not ranked:
            diagnostics.append(f"{source}: нет результатов с рабочей ссылкой")
            continue

        log.info("%s top candidates: %s", source,
                 [(round(score), item.get("title", "")[:90]) for score, item in ranked[:5]])
        eligible = [(score, item) for score, item in ranked if score >= 50]
        if not eligible:
            diagnostics.append(f"{source}: совпадения слишком слабые")
            continue

        for score, candidate in eligible[:4]:
            try:
                path, info = await download_mp3(candidate["url"])
                duration = info.get("duration")
                if isinstance(duration, (int, float)) and not MIN_TRACK_SECONDS <= duration <= MAX_TRACK_SECONDS:
                    cleanup(path)
                    diagnostics.append(f"{source}: длительность вне лимита")
                    continue
                resolved_track = track or _fallback_metadata(query, {**candidate, **info})
                resolved_track["artist"] = _clean(resolved_track.get("artist", ""))
                resolved_track["title"] = _clean(resolved_track.get("title", ""))
                # Keep catalog duration when available, but fill gaps from the actual source.
                if not resolved_track.get("duration"):
                    resolved_track["duration"] = duration
                return resolved_track, {
                    "path": path,
                    "source": source,
                    "source_title": candidate.get("title") or info.get("title") or "",
                    "match_score": round(score),
                }
            except Exception as exc:
                log.warning("Download candidate failed source=%s score=%s title=%r error=%s",
                            source, round(score), candidate.get("title"), exc, exc_info=True)
                diagnostics.append(f"{source}: скачивание не удалось ({type(exc).__name__})")

    log.error("No track found for query=%r; diagnostics=%s", query, diagnostics)
    if diagnostics and all("ошибка поиска" in item for item in diagnostics):
        message = "YouTube и SoundCloud не ответили на поиск. Проверь логи Railway: там будет точная причина."
    elif any("скачивание не удалось" in item for item in diagnostics):
        message = "Результаты поиска есть, но источники не дали скачать аудио. Проверь логи Railway — причина записана там."
    else:
        message = "Не удалось найти достаточно точное совпадение. Попробуй запрос «исполнитель — название»."
    raise LookupError(message)
