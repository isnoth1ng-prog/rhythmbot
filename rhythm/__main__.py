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
    InlineKeyboardButton, InlineQuery, InlineQueryResultCachedAudio, InlineQueryResultAudio,
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
                _inline_file_ids[_track_key(track)] = sent.audio.file_id
                _cache_set(_track_key(track), sent.audio.file_id)
                _cache_set(_track_key(selected), sent.audio.file_id)
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

async def _warm_inline_cache(query: str, user_id: int) -> None:
    """Prepare one real MP3 after answering the inline query.

    Telegram requires a cached audio file_id for InlineQueryResultCachedAudio.
    Downloads must therefore happen outside the inline answer deadline. The
    user can repeat the same query after preparation finishes.
    """
    path = None
    temporary_message = None
    try:
        tracks = (await asyncio.wait_for(search_options(query), timeout=18.0))[:INLINE_RESULTS]
        if not tracks:
            log.info("Inline warm-up found no candidates for %r", query)
            return

        # Try the best-ranked candidates, but keep the background task bounded.
        for candidate in tracks[:3]:
            key = _track_key(candidate)
            if _cache_get(key):
                continue
            query_for_track = f'{candidate.get("artist", "")} — {candidate.get("title", "")}'
            try:
                track, path = await asyncio.wait_for(_download_tagged(query_for_track), timeout=35.0)
                temporary_message = await asyncio.wait_for(
                    bot_for_inline.send_audio(
                        chat_id=user_id,
                        audio=FSInputFile(path, filename=f'{track["artist"]} - {track["title"]}.mp3'),
                        title=track["title"][:64],
                        performer=track["artist"][:64],
                        disable_notification=True,
                    ),
                    timeout=12.0,
                )
                if temporary_message.audio:
                    file_id = temporary_message.audio.file_id
                    _inline_file_ids[key] = file_id
                    _inline_file_ids[_track_key(track)] = file_id
                    _cache_set(key, file_id)
                    _cache_set(_track_key(track), file_id)
                    log.info("Inline audio cached for query=%r track=%r", query, query_for_track)
                    return
            except asyncio.TimeoutError:
                log.warning("Inline warm-up timed out for %r", query_for_track)
            except Exception:
                log.exception("Inline warm-up failed for %r", query_for_track)
            finally:
                cleanup(path)
                path = None
                if temporary_message:
                    try:
                        await temporary_message.bot.delete_message(
                            chat_id=user_id, message_id=temporary_message.message_id
                        )
                    except Exception:
                        log.debug("Could not delete temporary inline upload", exc_info=True)
                    temporary_message = None
    except asyncio.TimeoutError:
        log.warning("Inline warm-up search timed out for query=%r", query)
    except Exception:
        log.exception("Inline warm-up failed for query=%r", query)


# The bot instance is assigned during startup and reused by background cache jobs.
bot_for_inline: Bot | None = None
_inline_warm_tasks: dict[tuple[int, str], asyncio.Task] = {}


@dp.inline_query()
async def inline_search(inline_query: InlineQuery) -> None:
    query = (inline_query.query or "").strip()
    if len(query) < 2:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    results = []
    seen_ids = set()
    normalized_query = query.casefold()
    try:
        # Search persistent Telegram file_id cache first; this path should be fast.
        if _INLINE_CACHE_DB is not None:
            with sqlite3.connect(_INLINE_CACHE_DB, timeout=1) as db:
                rows = db.execute(
                    "SELECT track_key, file_id FROM audio_cache ORDER BY updated_at DESC LIMIT 500"
                ).fetchall()
            tokens = [
                token for token in normalized_query.replace("—", " ").replace("–", " ")
                .replace("-", " ").replace("|", " ").split() if len(token) > 1
            ]
            for key, file_id in rows:
                if tokens and all(token in key for token in tokens) and file_id not in seen_ids:
                    results.append(InlineQueryResultCachedAudio(
                        id=f"rh-cache-{abs(hash(key))}"[:64],
                        audio_file_id=file_id,
                    ))
                    seen_ids.add(file_id)
                    if len(results) >= INLINE_RESULTS:
                        break

        if not results:
            # Always answer within Telegram's deadline. The first cold query
            # warms the cache in the background; repeating the query returns MP3s.
            await inline_query.answer([], cache_time=1, is_personal=True)
            if bot_for_inline is None:
                return
            task_key = (inline_query.from_user.id, normalized_query)
            task = _inline_warm_tasks.get(task_key)
            if task is None or task.done():
                task = asyncio.create_task(_warm_inline_cache(query, inline_query.from_user.id))
                _inline_warm_tasks[task_key] = task
                task.add_done_callback(lambda _task, k=task_key: _inline_warm_tasks.pop(k, None))
                log.info("Inline cache warm-up scheduled for query=%r", query)
            return

        await inline_query.answer(results, cache_time=2, is_personal=True)
        log.info("Inline answer sent from cache query=%r results=%d", query, len(results))
    except Exception:
        log.exception("Inline search failed for query=%r", query)
        try:
            await inline_query.answer(results, cache_time=1, is_personal=True)
        except Exception:
            pass

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to Railway Variables.")
    prepare_runtime()
    _cache_init()
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
