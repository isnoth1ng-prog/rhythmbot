from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import DOWNLOAD_DIR, MAX_TRACK_SECONDS, MIN_TRACK_SECONDS, COOKIE_FILE, YT_PROXY


# Railway can be challenged differently depending on the YouTube player client.
# Try a small set of clients before giving up; retain yt-dlp's defaults last.
_PLAYER_CLIENTS = (("android",), ("ios",), ("web_safari",), ())


def _download(url: str) -> tuple[str, dict]:
    stem = uuid.uuid4().hex
    output_template = str(DOWNLOAD_DIR / f"{stem}.%(ext)s")
    last_error: Exception | None = None

    for player_clients in _PLAYER_CLIENTS:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 25,
            "retries": 1,
            "fragment_retries": 1,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
        }
        if player_clients:
            opts["extractor_args"] = {
                "youtube": {"player_client": list(player_clients)}
            }
        if YT_PROXY:
            opts["proxy"] = YT_PROXY
        if COOKIE_FILE.exists():
            opts["cookiefile"] = str(COOKIE_FILE)

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                duration = info.get("duration")
                if (
                    not isinstance(duration, (int, float))
                    or not MIN_TRACK_SECONDS <= duration <= MAX_TRACK_SECONDS
                ):
                    raise ValueError(
                        f"Длительность должна быть от {MIN_TRACK_SECONDS // 60} "
                        f"до {MAX_TRACK_SECONDS // 60} минут."
                    )
                ydl.download([url])

            path = DOWNLOAD_DIR / f"{stem}.mp3"
            if not path.exists():
                matches = list(DOWNLOAD_DIR.glob(f"{stem}.*"))
                if not matches:
                    raise RuntimeError("Источник не создал MP3-файл")
                path = matches[0]
            return str(path), info
        except (DownloadError, OSError) as exc:
            last_error = exc
            # Remove incomplete output before trying another client.
            for partial in DOWNLOAD_DIR.glob(f"{stem}.*"):
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
        except ValueError:
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("Не удалось скачать аудио: все варианты клиента исчерпаны")


async def download_mp3(url: str) -> tuple[str, dict]:
    return await asyncio.to_thread(_download, url)


def cleanup(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
