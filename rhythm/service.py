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
    """Normalize source titles without changing the trusted catalog metadata."""
    value = (value or "").replace("\u200b", "").replace("\ufeff", "")
    value = re.sub(r"\s*[-–—|]?\s*Topic\s*$", "", value, flags=re.IGNORECASE)
    # Remove common YouTube packaging labels, not meaningful track versions.
    value = re.sub(
        r"\s*[\[(](?:FMV|AMV|MV|M/V|OFFICIAL\s+(?:VIDEO|AUDIO|MUSIC\s+VIDEO)|"
        r"LYRICS?(?:\s+VIDEO)?|MUSIC\s+VIDEO|VISUALIZER|4K|8K|HD|HQ|"
        r"FULL\s+VERSION|AUDIO\s+ONLY|LYRIC\s+VIDEO)[\])]\s*",
        " ", value, flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\s*[-|•]\s*(?:official\s+(?:video|audio)|lyrics?(?:\s+video)?|"
        r"music\s+video|visualizer|4k|8k|hd|hq)\s*$",
        "", value, flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"^[\s™©®|•\-–—]+", "", value)
    return value.strip(" -–—|•\t")


def _artist_query(query: str) -> bool:
    return not any(sep in query for sep in (" — ", " – ", " - ", " | ")) and len(query.split()) <= 4


def _similarity(left: str, right: str) -> float:
    return float(fuzz.WRatio(left, right, processor=utils.default_process))


def _audio_score(result: dict, track: dict, query: str = "") -> float:
    raw_title = result.get("title") or ""
    # Reject explicit alternate edits before fuzzy matching. They can match the
    # same song name while playing at a different speed or using a remix.
    title_folded = raw_title.casefold()
    alternate_markers = (
        "speed up", "speed-up", "speedup", "sped up", "sped-up",
        "slowed", "nightcore", "daycore", "ultra slowed", "super slowed",
        "remix", "rework", "mashup", "8d audio", "bass boost",
        "bass boosted", "extended mix", "extended version", "speed edit",
    )
    if any(marker in title_folded for marker in alternate_markers):
        return 0.0
    title = _clean(raw_title)
    uploader = _clean(result.get("uploader") or "")
    # Search-result titles often contain video labels or decorative prefixes.
    # Score the normalized title, while mildly penalizing noisy uploads.
    noise = bool(re.search(
        r"\b(?:FMV|AMV|OFFICIAL\s+VIDEO|LYRICS?|MUSIC\s+VIDEO|VISUALIZER)\b|[™©®]",
        raw_title, flags=re.IGNORECASE,
    ))
    if track.get("artist") and track.get("title"):
        expected = f'{track["artist"]} - {track["title"]}'
        expected_score = _similarity(title, expected)
        title_only_score = _similarity(title, track["title"])
        artist_score = _similarity(uploader, track["artist"]) if uploader else 0.0

        # Hard title gate: artist/uploader similarity must never rescue a
        # different song. Prefer a source title containing artist + track title.
        if max(expected_score, title_only_score) < 72:
            return 0.0
        # If the title is not an almost exact match, require evidence that the
        # upload belongs to the requested artist.
        artist_in_title = expected_score >= 82
        if artist_score < 45 and not artist_in_title:
            return 0.0

        score = max(expected_score, title_only_score * 0.88)
        if artist_score:
            score = max(score, expected_score * 0.9 + artist_score * 0.1)
    else:
        score = _similarity(title, query)
        if uploader:
            score = max(score, _similarity(f"{uploader} - {title}", query))
    if noise:
        score -= 5

    source_duration = result.get("duration")
    target_duration = track.get("duration")
    if isinstance(source_duration, (int, float)):
        if not MIN_TRACK_SECONDS <= source_duration <= MAX_TRACK_SECONDS:
            return 0.0
        if isinstance(target_duration, (int, float)) and target_duration > 0:
            difference = abs(source_duration - target_duration)
            # A large duration mismatch is a strong sign of a sped-up,
            # shortened, live, or otherwise different edit. Reject it instead
            # of letting title similarity win.
            if difference > max(18, target_duration * 0.08):
                return 0.0
            elif difference > 10:
                score -= 5
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


def _metadata_priority(track: dict) -> tuple[int, float]:
    source = str(track.get("metadata_source") or "").casefold()
    # Trusted catalog metadata always precedes source-derived titles.
    trusted = 1 if source in {"apple/itunes", "apple", "itunes"} else 0
    return trusted, float(track.get("metadata_score") or 0)


async def search_options(query: str) -> list[dict]:
    requested = _split_track_query(query)
    artist_query = requested is None and _artist_query(query)
    try:
        tracks = await search_tracks(query, limit=25 if artist_query else 20 if requested else 10)
    except Exception:
        log.exception("Apple/iTunes search failed for %r", query)
        tracks = []

    catalog_ranked = []
    if requested:
        requested_artist, requested_title = requested
        for candidate in tracks:
            artist_score = _similarity(requested_artist, candidate.get("artist", ""))
            title_score = _similarity(requested_title, candidate.get("title", ""))
            if artist_score >= 68 and title_score >= 72:
                item = dict(candidate)
                item["match_score"] = title_score * 0.6 + artist_score * 0.4
                catalog_ranked.append(item)
    elif tracks:
        if artist_query:
            tracks = [t for t in tracks if _similarity(query, t.get("artist", "")) >= 60]
            tracks.sort(key=lambda t: _similarity(query, t.get("artist", "")) * 0.7 +
                        t.get("metadata_score", 0) * 0.3, reverse=True)
        else:
            tracks = [t for t in tracks if t.get("metadata_score", 0) >= 60]
        catalog_ranked = [dict(t) for t in tracks]

    if catalog_ranked:
        catalog_ranked.sort(key=lambda t: (
            float(t.get("match_score", t.get("metadata_score", 0))),
            float(t.get("metadata_score", 0))
        ), reverse=True)
        # Keep trusted Apple/iTunes results first; source search is only a fallback.
        return catalog_ranked[:15]

    fallback, seen = [], set()
    source_query = f"{requested[0]} {requested[1]}" if requested else query
    threshold = 66 if requested else 58
    # Prefer structured YouTube Music song entries over arbitrary uploads.
    for source in ("youtube_music", "youtube", "soundcloud"):
        try:
            results = await asyncio.to_thread(search_source, source_query, source, 12 if artist_query else 8)
        except Exception:
            log.exception("%s search failed for %r", source, source_query)
            continue
        for item in results:
            raw_title = _clean(item.get("title") or "")
            uploader = _clean(item.get("uploader") or "")
            if requested:
                source_artist, source_title = uploader, raw_title
                for separator in (" — ", " – ", " - ", " | "):
                    if separator in raw_title:
                        left, right = raw_title.split(separator, 1)
                        source_artist, source_title = left.strip(), right.strip()
                        break
                title_score = max(_similarity(source_title, requested[1]), _similarity(raw_title, requested[1]) * 0.8)
                artist_score = max(
                    _similarity(uploader, requested[0]) if uploader else 0,
                    _similarity(source_artist, requested[0]),
                )
                score = title_score * 0.6 + artist_score * 0.4
                if title_score < 70 or artist_score < 65:
                    continue
                display_artist, display_title = requested[0], requested[1]
            else:
                # For a free-text query, score the actual title separately from the uploader.
                # WRatio alone can overrate partial matches, so require strong title coverage too.
                title_score = _similarity(raw_title, query)
                combined_score = _similarity(f"{uploader} {raw_title}", query) if uploader else title_score
                score = max(title_score, combined_score * 0.92)
                query_words = {
                    token.casefold() for token in re.findall(r"[^\W_]+", query, flags=re.UNICODE)
                    if len(token) > 1
                }
                result_words = {
                    token.casefold() for token in re.findall(
                        r"[^\W_]+", f"{uploader} {raw_title}", flags=re.UNICODE
                    ) if len(token) > 1
                }
                coverage = len(query_words & result_words) / max(1, len(query_words))
                # Do not expose loosely related videos just because one word matches.
                if score < max(70, threshold + 10) or coverage < 0.75:
                    continue
                display_artist, display_title = uploader or "Неизвестный исполнитель", raw_title

            key = (
                re.sub(r"[^\\w]+", "", display_title.casefold(), flags=re.UNICODE),
                re.sub(r"[^\\w]+", "", display_artist.casefold(), flags=re.UNICODE),
            )
            if key in seen:
                continue
            seen.add(key)
            fallback.append({
                "title": display_title or query,
                "artist": display_artist,
                "album": "",
                "duration": item.get("duration"),
                "artwork_url": "",
                "track_url": item.get("url", ""),
                "metadata_score": score,
                "metadata_source": source,
            })
    fallback.sort(key=lambda t: t["metadata_score"], reverse=True)
    return fallback[:10 if requested else 15]


def _split_track_query(query: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+(?:—|–|-)\s+|\s+\|\s+", query.strip(), maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip()


async def resolve_track(query: str) -> tuple[dict, dict]:
    track = None
    requested = _split_track_query(query)
    try:
        catalog_tracks = await search_tracks(query, limit=20 if requested else 8)
        if requested:
            requested_artist, requested_title = requested
            ranked = []
            for candidate in catalog_tracks:
                artist_score = _similarity(requested_artist, candidate.get("artist", ""))
                title_score = _similarity(requested_title, candidate.get("title", ""))
                combined = title_score * 0.7 + artist_score * 0.3
                ranked.append((combined, artist_score, title_score, candidate))
            ranked.sort(key=lambda item: item[0], reverse=True)
            if ranked and ranked[0][1] >= 62 and ranked[0][2] >= 72:
                track = dict(ranked[0][3])
                log.info("Accepted catalog metadata for %r: artist_score=%s title_score=%s",
                         query, round(ranked[0][1]), round(ranked[0][2]))
            else:
                track = {
                    "title": requested_title, "artist": requested_artist, "album": "",
                    "duration": None, "artwork_url": "", "track_url": "",
                    "metadata_score": 0, "metadata_source": "explicit query fallback",
                }
                log.info("No confident catalog match for %r; preserving the user's artist/title", query)
        elif catalog_tracks:
            best = catalog_tracks[0]
            # A fuzzy score alone is not enough to replace the user's query:
            # iTunes can return a different song for short/ambiguous phrases.
            # Only trust catalog metadata when the result is a very strong match.
            score = float(best.get("metadata_score", 0) or 0)
            if score >= 82:
                track = dict(best)
                track["album"] = track.get("album") or ""
                log.info("Accepted strong Apple/iTunes match for %r score=%s", query, round(score))
            else:
                log.info("Apple/iTunes match too weak for query=%r score=%s; preserving original query",
                         query, round(score))
        else:
            log.info("Apple/iTunes returned no results for query=%r; trying audio sources directly", query)
    except Exception:
        log.exception("Apple/iTunes catalog request failed for query=%r; falling back to audio sources", query)
        if requested:
            track = {
                "title": requested[1], "artist": requested[0], "album": "",
                "duration": None, "artwork_url": "", "track_url": "",
                "metadata_score": 0, "metadata_source": "explicit query fallback",
            }

    # Search by trusted catalog metadata when available, not by the raw user query.
    search_query = f'{track["artist"]} {track["title"]}' if track else query
    diagnostics: list[str] = []
    for source in ("soundcloud", "youtube_music", "youtube"):
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
        log.info("%s top candidates: %s",
                 source, [(round(score), item.get("title", "")[:90]) for score, item in ranked[:5]])
        eligible = [(score, item) for score, item in ranked if score >= 72]
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
                if not resolved_track.get("duration"):
                    resolved_track["duration"] = duration
                return resolved_track, {
                    "path": path, "source": source,
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
