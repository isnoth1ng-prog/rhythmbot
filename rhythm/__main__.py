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
    InlineKeyboardButton, InlineQuery, InlineQueryResultCachedAudio,
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
    return f'{track.get("artist", "").strip().casefold()}|{track.get("title", "").strip().casefold()}'


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


@dp.inline_query()
async def inline_search(inline_query: InlineQuery) -> None:
    query = (inline_query.query or "").strip()
    if len(query) < 2:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    # Telegram expects inline answers quickly. Never serially prepare five downloads
    # inside one inline request: that can exceed Telegram's answer deadline.
    results = []
    seen_ids = set()
    try:
        # First, search the existing file_id cache without waiting for external APIs.
        normalized_query = query.casefold()
        if _INLINE_CACHE_DB is not None:
            with sqlite3.connect(_INLINE_CACHE_DB, timeout=1) as db:
                rows = db.execute("SELECT track_key, file_id FROM audio_cache ORDER BY updated_at DESC LIMIT 500").fetchall()
            tokens = [token for token in normalized_query.replace("—", " ").replace("–", " ").replace("-", " ").replace("|", " ").split() if len(token) > 1]
            for key, file_id in rows:
                if tokens and all(token in key for token in tokens) and file_id not in seen_ids:
                    results.append(InlineQueryResultCachedAudio(
                        id=f"rh-cache-{abs(hash(key))}"[:64],
                        audio_file_id=file_id,
                    ))
                    seen_ids.add(file_id)
                    if len(results) >= INLINE_RESULTS:
                        break
        log.info("Inline cache query=%r rows=%d results=%d", query, len(rows) if _INLINE_CACHE_DB is not None else 0, len(results))
        if results:
            await inline_query.answer(results, cache_time=2, is_personal=True)
            log.info("Inline answer sent from cache query=%r results=%d", query, len(results))
            return

        # Cold cache: use a strict overall budget and prepare only the top match.
        # A multi-track sequential download loop makes Telegram return an empty list.
        tracks = (await asyncio.wait_for(search_options(query), timeout=4.5))[:5]
        for index, candidate in enumerate(tracks):
            key = _track_key(candidate)
            file_id = _inline_file_ids.get(key) or _cache_get(key)
            if file_id:
                if file_id not in seen_ids:
                    seen_ids.add(file_id)
                    results.append(InlineQueryResultCachedAudio(
                        id=f"rh-{index}-{abs(hash(key))}"[:64],
                        audio_file_id=file_id,
                    ))
                continue

            # Only one download attempt, bounded so we can answer before Telegram times out.
            query_for_track = f'{candidate.get("artist", "")} — {candidate.get("title", "")}'
            path = None
            temporary_message = None
            try:
                track, path = await asyncio.wait_for(_download_tagged(query_for_track), timeout=4.0)
                temporary_message = await inline_query.bot.send_audio(
                    chat_id=inline_query.from_user.id,
                    audio=FSInputFile(path, filename=f'{track["artist"]} - {track["title"]}.mp3'),
                    title=track["title"][:64],
                    performer=track["artist"][:64],
                    disable_notification=True,
                )
                if temporary_message.audio:
                    file_id = temporary_message.audio.file_id
                    _inline_file_ids[key] = file_id
                    _inline_file_ids[_track_key(track)] = file_id
                    _cache_set(key, file_id)
                    _cache_set(_track_key(track), file_id)
                    results.append(InlineQueryResultCachedAudio(
                        id=f"rh-{index}-{abs(hash(key))}"[:64],
                        audio_file_id=file_id,
                    ))
            except asyncio.TimeoutError:
                log.warning("Inline cold-cache preparation exceeded time budget for %r", query_for_track)
            except Exception:
                log.exception("Could not prepare inline audio for %r", query_for_track)
            finally:
                cleanup(path)
                if temporary_message:
                    try:
                        await inline_query.bot.delete_message(
                            chat_id=inline_query.from_user.id,
                            message_id=temporary_message.message_id,
                        )
                    except Exception:
                        log.debug("Could not delete temporary inline upload", exc_info=True)
            # Never start another download in the same inline query.
            break

        await inline_query.answer(results, cache_time=1, is_personal=True)
        log.info("Inline answer sent after cold-cache attempt query=%r results=%d", query, len(results))
    except asyncio.TimeoutError:
        log.warning("Inline search timed out for query=%r", query)
        try:
            await inline_query.answer(results, cache_time=1, is_personal=True)
        except Exception:
            pass
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
    bot = Bot(BOT_TOKEN)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        (DOWNLOAD_DIR / "youtube-cookies.txt").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
