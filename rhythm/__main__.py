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
    InlineKeyboardButton, InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
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

@dp.inline_query()
async def inline_search(inline_query: InlineQuery) -> None:
    """Search inline quickly and return selectable tracks, never pre-download audio."""
    query = (inline_query.query or "").strip()
    if len(query) < 2:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    try:
        tracks = await asyncio.wait_for(search_options(query), timeout=4.0)
        tracks = tracks[:15]
        if not tracks:
            await inline_query.answer([], cache_time=2, is_personal=True)
            return

        sid = next(_ids)
        _sessions[sid] = {
            "tracks": tracks,
            "owner": inline_query.from_user.id,
        }
        results = []
        for idx, track in enumerate(tracks):
            artist = str(track.get("artist") or "Неизвестный исполнитель")
            title = str(track.get("title") or "Без названия")
            album = str(track.get("album") or "").strip()
            duration = track.get("duration")
            details = []
            if album:
                details.append(album)
            if isinstance(duration, (int, float)) and duration > 0:
                details.append(f"{int(duration) // 60}:{int(duration) % 60:02d}")
            results.append(InlineQueryResultArticle(
                id=f"rh-{sid}-{idx}"[:64],
                title=f"{artist} — {title}"[:128],
                description=" · ".join(details)[:255] or "Нажми, чтобы выбрать трек и скачать MP3",
                input_message_content=InputTextMessageContent(
                    message_text=f"Rhythm\n{artist} — {title}\nНажми «Скачать MP3», чтобы загрузить трек."
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="Скачать MP3",
                        callback_data=f"rh:pick:{sid}:{idx}",
                    )
                ]]),
            ))

        await inline_query.answer(results, cache_time=2, is_personal=True)
        log.info("Inline search query=%r results=%d session=%d", query, len(results), sid)
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
