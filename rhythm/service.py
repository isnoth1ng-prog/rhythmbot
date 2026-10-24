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
    trusted = 2 if source in {"apple/itunes", "apple", "itunes"} else 1 if source == "deezer" else 0
    return trusted, float(track.get("metadata_score") or 0)


async def search_options(query: str) -> list[dict]:
    """Return a broad, ranked list while preferring verified catalog metadata.

    Apple/iTunes supplies display metadata; YouTube Music supplies additional
    song candidates. Results are merged instead of abandoning source search as
    soon as the catalog returns a few items.
    """
    query = (query or "").strip()
    if not query:
        return []

    requested = _split_track_query(query)
    artist_query = requested is None and _artist_query(query)
    # Start Apple/iTunes and YouTube Music requests together so inline search
    # does not wait for one remote catalog before even starting the next.
    source_query = f"{requested[0]} {requested[1]}" if requested else query
    source_limit = 25 if artist_query else 20
    source_task = asyncio.create_task(
        asyncio.to_thread(search_source, source_query, "youtube_music", source_limit)
    )

    catalog_tracks: list[dict] = []
    try:
        catalog_tracks = await search_tracks(query, limit=40 if artist_query else 25)
    except Exception:
        log.exception("Apple/iTunes search failed for %r", query)

    catalog_ranked: list[dict] = []
    for candidate in catalog_tracks:
        artist = str(candidate.get("artist") or "")
        title = str(candidate.get("title") or "")
        if requested:
            artist_score = _similarity(requested[0], artist)
            title_score = _similarity(requested[1], title)
            if artist_score < 66 or title_score < 70:
                continue
            item = dict(candidate)
            item["match_score"] = title_score * 0.62 + artist_score * 0.38
        elif artist_query:
            artist_score = _similarity(query, artist)
            if artist_score < 58:
                continue
            item = dict(candidate)
            item["match_score"] = artist_score * 0.75 + float(candidate.get("metadata_score") or 0) * 0.25
        else:
            score = float(candidate.get("metadata_score") or 0)
            if score < 52:
                continue
            item = dict(candidate)
            item["match_score"] = score
        catalog_ranked.append(item)

    catalog_ranked.sort(
        key=lambda item: (float(item.get("match_score") or 0), float(item.get("metadata_score") or 0)),
        reverse=True,
    )

    source_ranked: list[dict] = []
    try:
        # This request was started above, in parallel with Apple/iTunes.
        source_results = await source_task
    except Exception:
        log.exception("YouTube Music search failed for %r", source_query)
        source_results = []

    for source_item in source_results:
        source_title = _clean(str(source_item.get("title") or ""))
        source_artist = _clean(str(source_item.get("uploader") or ""))
        if not source_title or not source_artist:
            continue

        if requested:
            title_score = _similarity(requested[1], source_title)
            artist_score = _similarity(requested[0], source_artist)
            if title_score < 68 or artist_score < 62:
                continue
            source_score = title_score * 0.62 + artist_score * 0.38
        elif artist_query:
            artist_score = _similarity(query, source_artist)
            if artist_score < 55:
                continue
            source_score = artist_score * 0.75 + _similarity(source_title, query) * 0.25
        else:
            title_score = _similarity(query, source_title)
            combined_score = _similarity(f"{source_artist} {source_title}", query)
            query_words = {
                token.casefold() for token in re.findall(r"[^\W_]+", query, flags=re.UNICODE)
                if len(token) > 1
            }
            result_words = {
                token.casefold() for token in re.findall(
                    r"[^\W_]+", f"{source_artist} {source_title}", flags=re.UNICODE
                ) if len(token) > 1
            }
            coverage = len(query_words & result_words) / max(1, len(query_words))
            if max(title_score, combined_score * 0.92) < 62 or coverage < 0.5:
                continue
            source_score = max(title_score, combined_score * 0.92)

        # If catalog metadata describes this same recording, use it for display
        # and tags; keep the YouTube Music URL as the audio source.
        best_catalog = None
        best_pair_score = 0.0
        for catalog in catalog_tracks:
            artist_score = _similarity(source_artist, str(catalog.get("artist") or ""))
            title_score = _similarity(source_title, str(catalog.get("title") or ""))
            pair_score = title_score * 0.62 + artist_score * 0.38
            if artist_score >= 68 and title_score >= 72 and pair_score > best_pair_score:
                best_catalog, best_pair_score = catalog, pair_score

        if best_catalog:
            item = dict(best_catalog)
            item["track_url"] = source_item.get("url") or item.get("track_url") or ""
            item["audio_source"] = "youtube_music"
            item["match_score"] = max(source_score, best_pair_score)
        else:
            # For explicit artist/title requests, preserve the user's spelling
            # rather than copying noisy source text into ID3 tags.
            if requested:
                display_artist, display_title = requested
            else:
                display_artist, display_title = source_artist, source_title
            item = {
                "title": display_title,
                "artist": display_artist,
                "album": "",
                "duration": source_item.get("duration"),
                "artwork_url": "",
                "track_url": source_item.get("url") or "",
                "metadata_score": 0,
                "metadata_source": "YouTube Music",
                "audio_source": "youtube_music",
                "match_score": source_score,
            }
        source_ranked.append(item)

    # Merge catalog and YouTube Music results. De-duplicate by normalized
    # artist/title while keeping the best catalog-backed representation.
    merged: dict[tuple[str, str], dict] = {}
    def normalized(value: str) -> str:
        return re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)

    for item in catalog_ranked + sorted(source_ranked, key=lambda value: float(value.get("match_score") or 0), reverse=True):
        artist = _clean(str(item.get("artist") or ""))
        title = _clean(str(item.get("title") or ""))
        if not artist or not title:
            continue
        key = (normalized(artist), normalized(title))
        existing = merged.get(key)
        if existing is None:
            item["artist"], item["title"] = artist, title
            merged[key] = item
        else:
            # Catalog links are useful metadata links, but audio resolution must
            # retain the YouTube Music URL even when the catalog has a store URL.
            if item.get("audio_source") == "youtube_music" and item.get("track_url"):
                existing["track_url"] = item["track_url"]
                existing["audio_source"] = "youtube_music"
            elif not existing.get("track_url") and item.get("track_url"):
                existing["track_url"] = item["track_url"]
                existing["audio_source"] = item.get("audio_source")
            if _metadata_priority(item) > _metadata_priority(existing):
                source_url = existing.get("track_url") or item.get("track_url")
                audio_source = existing.get("audio_source") or item.get("audio_source")
                existing.update(item)
                existing["track_url"] = source_url
                if audio_source:
                    existing["audio_source"] = audio_source

    results = list(merged.values())
    results.sort(
        key=lambda item: (
            float(item.get("match_score", item.get("metadata_score", 0)) or 0),
            float(item.get("metadata_score") or 0),
        ),
        reverse=True,
    )
    # Return up to 15 meaningful choices; exact artist/title searches should
    # not be flooded by loosely related tracks.
    return results[:15]

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
    for source in ("youtube_music", "soundcloud", "youtube"):
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
        message = "YouTube Music не ответил на поиск. Проверь логи Railway: там будет точная причина."
    elif any("скачивание не удалось" in item for item in diagnostics):
        message = "YouTube Music нашёл результат, но не удалось скачать аудио. Проверь логи Railway — причина записана там."
    else:
        message = "В YouTube Music не удалось найти достаточно точное совпадение. Попробуй запрос «исполнитель — название»."
    raise LookupError(message)
