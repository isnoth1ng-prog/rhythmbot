from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sqlite3
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup,
    InlineKeyboardButton, InlineQuery, InlineQueryResultArticle, InlineQueryResultCachedAudio, InputTextMessageContent,
)
from .config import BOT_TOKEN, DOWNLOAD_DIR, prepare_runtime
from .downloader import cleanup
from .metadata import tag_mp3
from .service import resolve_track, search_options

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("rhythm")
dp = Dispatcher()
_sessions = {}
_ids = itertools.count(1)
_inline_file_ids: dict[str, str] = {}
PAGE_SIZE = 5
INLINE_RESULTS = 5
INLINE_CACHE_CHANNEL_ID = os.getenv("INLINE_CACHE_CHANNEL_ID", "").strip()
_INLINE_CACHE_DB = None


def _keyboard(sid: int, page: int) -> InlineKeyboardMarkup:
    session = _sessions[sid]
    tracks = session["tracks"]
    start = page * PAGE_SIZE
    rows = []
    for idx, track in enumerate(tracks[start:start + PAGE_SIZE], start=start):
        label = f'{track.get("artist", "Неизвестный исполнитель")} — {track.get("title", "Без названия")}'
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"rh:pick:{sid}:{idx}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹ Назад", callback_data=f"rh:page:{sid}:{page-1}"))
    if start + PAGE_SIZE < len(tracks):
        nav.append(InlineKeyboardButton(text="Дальше ›", callback_data=f"rh:page:{sid}:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _track_key(track: dict) -> str:
    return f'ytmusic-v1|{track.get("artist", "").strip().casefold()}|{track.get("title", "").strip().casefold()}'


def _cache_init() -> None:
    global _INLINE_CACHE_DB
    cache_root = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or DOWNLOAD_DIR)
    cache_root.mkdir(parents=True, exist_ok=True)
    _INLINE_CACHE_DB = cache_root / "inline_cache.sqlite3"
    with sqlite3.connect(_INLINE_CACHE_DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS audio_cache (track_key TEXT PRIMARY KEY, file_id TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT (unixepoch()))")


def _cache_get(key: str) -> str | None:
    if _INLINE_CACHE_DB is None:
        return None
    with sqlite3.connect(_INLINE_CACHE_DB, timeout=2) as db:
        row = db.execute("SELECT file_id FROM audio_cache WHERE track_key = ?", (key,)).fetchone()
    return row[0] if row else None


def _cache_set(key: str, file_id: str) -> None:
    if _INLINE_CACHE_DB is None:
        return
    with sqlite3.connect(_INLINE_CACHE_DB, timeout=2) as db:
        db.execute("INSERT INTO audio_cache(track_key, file_id, updated_at) VALUES (?, ?, unixepoch()) ON CONFLICT(track_key) DO UPDATE SET file_id=excluded.file_id, updated_at=excluded.updated_at", (key, file_id))


async def _store_inline_audio(bot: Bot, track: dict, file_id: str) -> str:
    """Store audio in the private cache channel so its Telegram file_id persists."""
    if not INLINE_CACHE_CHANNEL_ID:
        log.error("Inline cache save skipped: INLINE_CACHE_CHANNEL_ID is not configured")
        return file_id
    log.info("Inline cache save started: key=%s", _track_key(track))
    cached = await bot.send_audio(
        chat_id=INLINE_CACHE_CHANNEL_ID,
        audio=file_id,
        title=str(track.get("title") or "Без названия")[:64],
        performer=str(track.get("artist") or "Неизвестный исполнитель")[:64],
    )
    if not cached.audio:
        raise RuntimeError("Telegram did not return audio after storing it in cache channel")
    log.info("Inline cache channel save succeeded: key=%s", _track_key(track))
    return cached.audio.file_id


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Rhythm — поиск музыки в Telegram.")


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer("Напиши исполнителя или «исполнитель — название», выбери вариант — отправлю MP3 с обложкой. Инлайн: @имя_бота запрос.")


@dp.message(F.text)
async def search_and_list(message: Message) -> None:
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return
    if len(query) > 180:
        await message.answer("Слишком длинный запрос.")
        return
    status = await message.answer("Ищу…")
    try:
        tracks = (await search_options(query))[:15]
        if not tracks:
            await status.edit_text("Ничего подходящего не нашёл. Попробуй «исполнитель — название».")
            return
        sid = next(_ids)
        _sessions[sid] = {"tracks": tracks, "owner": message.from_user.id if message.from_user else 0}
        await status.edit_text(f"Нашёл {len(tracks)} вариантов. Выбери нужный:", reply_markup=_keyboard(sid, 0))
    except Exception:
        log.exception("Search failed for query=%r", query)
        await status.edit_text("Не получилось выполнить поиск. Попробуй позже.")


@dp.callback_query(F.data.startswith("rh:page:"))
async def change_page(callback: CallbackQuery) -> None:
    try:
        _, _, raw_sid, raw_page = callback.data.split(":")
        sid, page = int(raw_sid), int(raw_page)
        session = _sessions.get(sid)
        if not session or callback.from_user.id != session["owner"]:
            await callback.answer("Список устарел. Отправь запрос ещё раз.", show_alert=True)
            return
        page = max(0, min(page, (len(session["tracks"]) - 1) // PAGE_SIZE))
        await callback.message.edit_reply_markup(reply_markup=_keyboard(sid, page))
        await callback.answer()
    except Exception:
        log.exception("Page change failed")
        await callback.answer("Не удалось открыть страницу.", show_alert=True)


async def _download_tagged(query: str) -> tuple[dict, str]:
    track, result = await resolve_track(query)
    path = result["path"]
    await tag_mp3(path, track)
    if Path(path).stat().st_size > 49 * 1024 * 1024:
        cleanup(path)
        raise RuntimeError("Файл слишком большой для отправки через Telegram.")
    return track, path


@dp.callback_query(F.data.startswith("rh:pick:"))
async def choose_track(callback: CallbackQuery) -> None:
    path = None
    try:
        _, _, raw_sid, raw_idx = callback.data.split(":")
        sid, idx = int(raw_sid), int(raw_idx)
        session = _sessions.get(sid)
        if not session or callback.from_user.id != session["owner"] or idx >= len(session["tracks"]):
            await callback.answer("Список устарел. Отправь запрос ещё раз.", show_alert=True)
            return
        selected = session["tracks"][idx]
        query = f'{selected.get("artist", "")} — {selected.get("title", "")}'
        log.info("Track selection started: query=%r track_key=%s channel_configured=%s", query, _track_key(selected), bool(INLINE_CACHE_CHANNEL_ID))
        await callback.answer("Готовлю MP3…")
        await callback.message.edit_text("⬇️ Готовлю…")
        await callback.bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
        track, path = await _download_tagged(query)
        try:
            sent = await callback.message.answer_audio(
                audio=FSInputFile(path, filename=f'{track["artist"]} - {track["title"]}.mp3'),
                title=track["title"][:64],
                performer=track["artist"][:64],
            )
            if sent.audio:
                try:
                    cached_file_id = await _store_inline_audio(callback.bot, track, sent.audio.file_id)
                except Exception:
                    log.exception("Could not store track in inline cache channel")
                    cached_file_id = sent.audio.file_id
                _inline_file_ids[_track_key(track)] = cached_file_id
                _cache_set(_track_key(track), cached_file_id)
                _cache_set(_track_key(selected), cached_file_id)
                log.info("Inline cache DB saved: track_key=%s selected_key=%s", _track_key(track), _track_key(selected))
        finally:
            cleanup(path)
        _sessions.pop(sid, None)
        await callback.message.delete()
    except LookupError as exc:
        try:
            await callback.message.edit_text(str(exc))
        except Exception:
            pass
    except Exception:
        log.exception("Selected track failed")
        try:
            await callback.message.edit_text("Не получилось скачать этот вариант. Попробуй выбрать другой.")
        except Exception:
            pass

@dp.inline_query()
async def inline_search(inline_query: InlineQuery) -> None:
    """Return playable cached audio; prepare the top uncached result on demand."""
    query = (inline_query.query or "").strip()
    if len(query) < 2:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    try:
        tracks = await asyncio.wait_for(search_options(query), timeout=4.0)
        tracks = tracks[:INLINE_RESULTS]
        if not tracks:
            await inline_query.answer([], cache_time=1, is_personal=True)
            return

        # First check all search results against the cache.
        cached_results = []
        uncached_tracks = []
        for idx, track in enumerate(tracks):
            file_id = _cache_get(_track_key(track))
            if file_id:
                cached_results.append(InlineQueryResultCachedAudio(
                    id=f"rh-a-{idx}",
                    audio_file_id=file_id,
                ))
            else:
                uncached_tracks.append(track)

        # If nothing is cached yet, prepare the best-ranked result directly
        # from inline search. Limit this to one download to avoid Telegram's
        # inline-answer deadline being exceeded.
        if not cached_results and uncached_tracks and INLINE_CACHE_CHANNEL_ID:
            track = uncached_tracks[0]
            query_to_download = f'{track.get("artist", "")} — {track.get("title", "")}'
            path = None
            try:
                prepared_track, path = await asyncio.wait_for(
                    _download_tagged(query_to_download), timeout=6.0
                )
                sent = await asyncio.wait_for(
                    inline_query.bot.send_audio(
                        chat_id=INLINE_CACHE_CHANNEL_ID,
                        audio=FSInputFile(path, filename=f'{prepared_track["artist"]} - {prepared_track["title"]}.mp3'),
                        title=str(prepared_track.get("title") or "Без названия")[:64],
                        performer=str(prepared_track.get("artist") or "Неизвестный исполнитель")[:64],
                    ),
                    timeout=3.0,
                )
                if not sent.audio:
                    raise RuntimeError("Telegram did not return audio from cache channel")
                file_id = sent.audio.file_id
                _cache_set(_track_key(prepared_track), file_id)
                _cache_set(_track_key(track), file_id)
                log.info("Inline cache prepared from inline query: query=%r key=%s", query, _track_key(prepared_track))
                cached_results.append(InlineQueryResultCachedAudio(
                    id="rh-inline-prepared",
                    audio_file_id=file_id,
                ))
            except Exception:
                log.exception("Could not prepare uncached inline result for query=%r", query)
            finally:
                if path:
                    cleanup(path)

        await inline_query.answer(cached_results, cache_time=1, is_personal=True)
        log.info("Inline search query=%r results=%d cached=%d", query, len(cached_results), len(cached_results))
    except asyncio.TimeoutError:
        log.warning("Inline search timed out for query=%r", query)
        try:
            await inline_query.answer([], cache_time=1, is_personal=True)
        except Exception:
            pass
    except Exception:
        log.exception("Inline search failed for query=%r", query)
        try:
            await inline_query.answer([], cache_time=1, is_personal=True)
        except Exception:
            pass

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to Railway Variables.")
    prepare_runtime()
    _cache_init()
    log.info("Inline cache diagnostics: channel_configured=%s cache_db=%s", bool(INLINE_CACHE_CHANNEL_ID), _INLINE_CACHE_DB)
    global bot_for_inline
    bot = Bot(BOT_TOKEN)
    bot_for_inline = bot
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        (DOWNLOAD_DIR / "youtube-cookies.txt").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
