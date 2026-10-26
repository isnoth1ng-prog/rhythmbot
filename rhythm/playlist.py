from __future__ import annotations

import re
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import COOKIE_FILE, YT_PROXY

SUPPORTED_HOSTS = {
    "music.yandex.ru", "music.yandex.com", "music.apple.com",
    "open.spotify.com", "spotify.com", "www.spotify.com",
    "youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be",
}
PLAYLIST_MARKERS = ("/playlist", "/album", "/browse", "/users/", "/playlists/")


class PlaylistImportError(RuntimeError):
    """Raised when a playlist URL cannot be read by an available extractor."""


def is_music_url(text: str) -> bool:
    value = (text or "").strip()
    if not value or len(value) > 2048:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    if host not in SUPPORTED_HOSTS and not any(host.endswith("." + item) for item in SUPPORTED_HOSTS):
        return False
    return True


def _options() -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 20,
        "retries": 2,
        "playlistend": None,
    }
    if YT_PROXY:
        options["proxy"] = YT_PROXY
    if COOKIE_FILE.exists():
        options["cookiefile"] = str(COOKIE_FILE)
    return options


def _track_from_entry(entry: dict) -> dict | None:
    if not entry:
        return None
    title = str(entry.get("track") or entry.get("title") or "").strip()
    artist = str(entry.get("artist") or "").strip()
    if not artist:
        artists = entry.get("artists") or entry.get("creator") or entry.get("uploader") or ""
        if isinstance(artists, list):
            artist = ", ".join(
                str(item.get("name") if isinstance(item, dict) else item).strip()
                for item in artists if item
            )
        else:
            artist = str(artists).strip()
    # Common extractor titles are "Artist - Track"; preserve titles otherwise.
    if not artist:
        match = re.match(r"^(.{1,120}?)\s+[—–-]\s+(.{1,180})$", title)
        if match:
            artist, title = match.group(1).strip(), match.group(2).strip()
    if not title:
        return None
    return {
        "title": title,
        "artist": artist,
        "duration": entry.get("duration"),
        "source_url": entry.get("webpage_url") or entry.get("url") or "",
    }


def _extract_playlist(url: str) -> dict:
    try:
        with YoutubeDL(_options()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise PlaylistImportError(
            "Не удалось прочитать эту ссылку. Возможно, сервис не отдаёт список треков без авторизации "
            "или для него пока нет доступного публичного адаптера."
        ) from exc

    if not info:
        raise PlaylistImportError(
            "Источник не вернул список композиций. Проверь, что плейлист доступен по ссылке."
        )
    entries = info.get("entries")
    if entries is None:
        # A single track URL is accepted as a one-item import.
        item = _track_from_entry(info)
        entries = [item] if item else []
    tracks = []
    for entry in entries:
        if not entry:
            continue
        item = _track_from_entry(entry)
        if item:
            tracks.append(item)
    if not tracks:
        raise PlaylistImportError(
            "Ссылку распознал, но получить список песен не удалось. Для некоторых сервисов понадобится отдельный адаптер/API."
        )
    return {
        "title": str(info.get("title") or info.get("playlist_title") or "Импортированный плейлист"),
        "tracks": tracks,
    }


async def extract_playlist(url: str) -> dict:
    """Extract playlist metadata without downloading audio; runs network work off-loop."""
    if not is_music_url(url):
        raise PlaylistImportError("Пришли ссылку на плейлист Яндекс Музыки, Apple Music, Spotify или YouTube.")
    return await __import__("asyncio").to_thread(_extract_playlist, url)
