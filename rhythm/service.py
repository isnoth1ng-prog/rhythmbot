from __future__ import annotations

import asyncio
import logging

from rapidfuzz import fuzz, utils

from .catalog import search_tracks
from .config import MAX_TRACK_SECONDS, MIN_TRACK_SECONDS
from .downloader import download_mp3, cleanup
from .sources import search_source

log = logging.getLogger("rhythm.service")


def _similarity(left: str, right: str) -> float:
    return float(fuzz.WRatio(left, right, processor=utils.default_process))


def _audio_score(result: dict, track: dict) -> float:
    title = result.get("title") or ""
    uploader = result.get("uploader") or ""
    expected = f'{track.get("artist", "")} - {track.get("title", "")}'
    title_score = _similarity(title, expected)
    title_only_score = _similarity(title, track.get("title", ""))
    artist_score = _similarity(uploader, track.get("artist", "")) if uploader else 0.0
    score = max(title_score, title_only_score * 0.85 + artist_score * 0.15)

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


async def resolve_track(query: str) -> tuple[dict, dict]:
    try:
        catalog_tracks = await search_tracks(query)
    except Exception as exc:
        log.exception("Apple/iTunes catalog request failed for query=%r", query)
        raise LookupError(
            "Не удалось связаться с каталогом Apple/iTunes. Попробуй ещё раз чуть позже."
        ) from exc

    if not catalog_tracks:
        raise LookupError(
            "Apple/iTunes не нашёл этот запрос. Напиши «исполнитель — название» "
            "или попробуй оригинальное написание."
        )

    track = catalog_tracks[0]
    if track["metadata_score"] < 38:
        log.info(
            "Low catalog score query=%r top=%r score=%s",
            query, f'{track["artist"]} - {track["title"]}', track["metadata_score"]
        )
        raise LookupError(
            "Не смог уверенно определить трек в Apple/iTunes. Укажи исполнителя "
            "и название через тире."
        )
    track["album"] = track.get("album") or ""

    search_query = f'{track["artist"]} {track["title"]}'
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
            ((_audio_score(item, track), item) for item in results if item.get("url")),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not ranked:
            diagnostics.append(f"{source}: нет результатов с рабочей ссылкой")
            continue

        log.info(
            "%s top candidates: %s",
            source,
            [(round(score), item.get("title", "")[:90]) for score, item in ranked[:5]],
        )
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
                return track, {
                    "path": path,
                    "source": source,
                    "source_title": candidate.get("title") or info.get("title") or "",
                    "match_score": round(score),
                }
            except Exception as exc:
                log.warning(
                    "Download candidate failed source=%s score=%s title=%r error=%s",
                    source, round(score), candidate.get("title"), exc, exc_info=True,
                )
                diagnostics.append(f"{source}: скачивание не удалось ({type(exc).__name__})")

    log.error("No track found for query=%r; diagnostics=%s", query, diagnostics)
    if diagnostics and all("ошибка поиска" in item for item in diagnostics):
        message = "YouTube и SoundCloud не ответили на поиск. Проверь логи Railway: там будет точная причина."
    elif any("скачивание не удалось" in item for item in diagnostics):
        message = "Результаты поиска есть, но источники не дали скачать аудио. Проверь логи Railway — причина записана там."
    else:
        message = "Не удалось найти достаточно точное совпадение. Попробуй запрос «исполнитель — название»."
    raise LookupError(message)
