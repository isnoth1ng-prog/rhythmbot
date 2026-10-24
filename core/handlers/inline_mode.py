import asyncio
import logging
from cachetools import TTLCache
from aiogram import Router, Bot
from aiogram.types import InlineQuery, InlineQueryResultCachedAudio
from aiogram.exceptions import TelegramBadRequest

from ..services.inline_search.fts5_search import search_fts
from ..services.inline_search.rapidfuzz_search import search_rapidfuzz

import core.config as Config

user_throttle = TTLCache(maxsize=1000, ttl=Config.INLINE_SEARCH_THROTTLE_TTL)

CHANNEL_ID = Config.CHANNEL_ID
router = Router()
logger = logging.getLogger(__name__)

# Support searching Russian and Latin spellings of the same artist/title.
_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})
_LATIN_TO_CYRILLIC = {
    "shch": "щ", "yo": "ё", "zh": "ж", "kh": "х", "ts": "ц",
    "ch": "ч", "sh": "ш", "yu": "ю", "ya": "я",
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е",
    "z": "з", "i": "и", "y": "й", "k": "к", "l": "л", "m": "м",
    "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т",
    "u": "у", "f": "ф", "c": "к",
}


def _query_variants(query: str) -> list[str]:
    original = " ".join((query or "").split()).strip()
    if not original:
        return []
    variants = [original, original.translate(_CYRILLIC_TO_LATIN)]
    # Reverse transliteration is intentionally conservative: only use it when
    # the query contains Latin letters and no Cyrillic letters.
    if not any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in original):
        lowered = original.lower()
        converted = lowered
        for latin, cyrillic in sorted(_LATIN_TO_CYRILLIC.items(), key=lambda item: len(item[0]), reverse=True):
            converted = converted.replace(latin, cyrillic)
        variants.append(converted)
    unique = []
    seen = set()
    for value in variants:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


async def combine_search_results(query: str):
    query_variants = _query_variants(query)
    if not query_variants:
        return []

    # Search each spelling in both indexes. This makes "раскол" and "Raskol"
    # find the same cached audio even if its metadata uses only one alphabet.
    search_jobs = []
    for variant in query_variants:
        search_jobs.extend([
            (variant, Config.CHANNEL_DB_PATH),
            (variant, Config.CHAT_DB_PATH),
        ])
    raw_results = await asyncio.gather(*[
        search_fts(variant, db_name, limit=500)
        for variant, db_name in search_jobs
    ])

    fuzzy_jobs = []
    for (variant, db_name), candidates in zip(search_jobs, raw_results):
        fuzzy_jobs.append(search_rapidfuzz(variant, candidates, limit=100, cutoff=25))
    fuzzy_results = await asyncio.gather(*fuzzy_jobs)

    unique_songs = {}
    final_list = []
    for results in fuzzy_results:
        for song in results:
            song_id, file_id, title, performer, is_cached, db_tag, score = song
            unique_key = f"{db_tag}:{song_id}"
            existing = unique_songs.get(unique_key)
            song_data = {
                "song_id": song_id,
                "file_id": file_id,
                "title": title,
                "performer": performer,
                "is_cached": is_cached,
                "db_tag": db_tag,
                "unique_key": unique_key,
                "score": score,
                "db_name": Config.CHANNEL_DB_PATH if db_tag.startswith("channel") else Config.CHAT_DB_PATH,
            }
            if existing is None or score > existing["score"]:
                unique_songs[unique_key] = song_data

    final_list = list(unique_songs.values())
    final_list.sort(
        key=lambda item: (
            item["is_cached"],
            item["score"],
            1 if item["db_name"] == Config.CHAT_DB_PATH else 0,
        ),
        reverse=True,
    )
    return final_list[:50]


@router.inline_query()
async def inline_music_search(inline_query: InlineQuery, bot: Bot):
    user_id = inline_query.from_user.id

    if user_id in user_throttle:
        return
    user_throttle[user_id] = True

    if user_id in Config.BLOCKED_USER_IDS:
        await inline_query.answer([], is_personal=True, cache_time=300)
        return

    text = (inline_query.query or "").strip()
    if not text:
        await inline_query.answer([], is_personal=False, cache_time=5)
        return

    songs = await combine_search_results(text)
    cached_results = []

    for item in songs:
        if not item.get("file_id"):
            continue

        try:
            cached = InlineQueryResultCachedAudio(
                id=str(abs(hash(f"cached:{item['unique_key']}:{item['file_id']}"))),
                audio_file_id=item["file_id"],
                title=item["title"] or "Song",
                performer=item["performer"] or "",
                caption="",
            )
            cached_results.append(cached)
        except Exception as exc:
            logger.warning("Failed to create InlineQueryResultCachedAudio: %s", exc)

    try:
        await inline_query.answer(cached_results[:50], is_personal=False, cache_time=60)
    except TelegramBadRequest as exc:
        logger.error("inline.answer failed: %s", exc)
        await inline_query.answer([], is_personal=False, cache_time=1)
