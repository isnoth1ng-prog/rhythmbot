from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from yt_dlp import YoutubeDL

from .config import DOWNLOAD_DIR, MAX_TRACK_SECONDS, MIN_TRACK_SECONDS, COOKIE_FILE, YT_PROXY


def _download(url: str) -> tuple[str, dict]:
    stem = uuid.uuid4().hex
    output_template = str(DOWNLOAD_DIR / f"{stem}.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 1,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
    }
    if YT_PROXY:
        opts["proxy"] = YT_PROXY
    if COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = info.get("duration")
        if not isinstance(duration, (int, float)) or not MIN_TRACK_SECONDS <= duration <= MAX_TRACK_SECONDS:
            raise ValueError(f"Длительность должна быть от {MIN_TRACK_SECONDS // 60} до {MAX_TRACK_SECONDS // 60} минут.")
        ydl.download([url])
    path = DOWNLOAD_DIR / f"{stem}.mp3"
    if not path.exists():
        matches = list(DOWNLOAD_DIR.glob(f"{stem}.*"))
        if not matches:
            raise RuntimeError("Источник не создал MP3-файл")
        path = matches[0]
    return str(path), info


async def download_mp3(url: str) -> tuple[str, dict]:
    return await asyncio.to_thread(_download, url)


def cleanup(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
