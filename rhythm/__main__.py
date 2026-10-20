from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile

from .config import BOT_TOKEN, DOWNLOAD_DIR, prepare_runtime
from .downloader import cleanup
from .metadata import tag_mp3
from .service import resolve_track

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("rhythm")
dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Йоу, это Rhythm 🎧\n\n"
        "Отправь название трека или «исполнитель — название». "
        "Я сверю метаданные, проверю совпадение и попробую найти аудио.\n\n"
        "Для точного результата лучше указывать исполнителя."
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Как искать:\n"
        "• исполнитель — название\n"
        "• название трека исполнитель\n\n"
        "Rhythm сверяет метаданные через Apple/iTunes, затем ищет аудио в YouTube "
        "и при необходимости пробует SoundCloud. Доступность треков не гарантируется."
    )


@dp.message(F.text)
async def search_and_send(message: Message) -> None:
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return
    if len(query) > 180:
        await message.answer("Слишком длинный запрос. Напиши название покороче.")
        return

    status = await message.answer("🔎 Сверяю название и ищу подходящее аудио…")
    path = None
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        track, result = await resolve_track(query)
        path = result["path"]
        await tag_mp3(path, track)
        size = Path(path).stat().st_size
        if size > 49 * 1024 * 1024:
            raise RuntimeError("Файл слишком большой для отправки через этот бот.")
        caption = f'{track["artist"]} — {track["title"]}\nИсточник: {result["source"].capitalize()}'
        await status.delete()
        await message.answer_audio(
            audio=FSInputFile(path, filename=f'{track["artist"]} - {track["title"]}.mp3'),
            title=track["title"][:64],
            performer=track["artist"][:64],
            caption=caption[:1024],
        )
    except LookupError as exc:
        await status.edit_text(str(exc))
    except Exception:
        log.exception("Track request failed")
        try:
            await status.edit_text("Не получилось обработать этот запрос. Попробуй ещё раз чуть позже.")
        except Exception:
            pass
    finally:
        cleanup(path)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to Railway Variables.")
    prepare_runtime()
    bot = Bot(BOT_TOKEN)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        # Remove temporary cookie file on graceful shutdown.
        cookie_file = DOWNLOAD_DIR / "youtube-cookies.txt"
        cookie_file.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
