from __future__ import annotations

import asyncio
import base64
import os
import re
from urllib.parse import unquote, urlparse

import requests
from yt_dlp import YoutubeDL

from .config import COOKIE_FILE, YT_PROXY

SUPPORTED_HOSTS = {
    "music.yandex.ru", "music.yandex.com", "music.apple.com",
    "open.spotify.com", "spotify.com", "www.spotify.com",
    "youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be",
}


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
    return host in SUPPORTED_HOSTS


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
    if not artist:
        match = re.match(r"^(.{1,120}?)\s+[—–-]\s+(.{1,180})$", title)
        if match:
            artist, title = match.group(1).strip(), match.group(2).strip()
    if not title:
        return None
    duration = entry.get("duration")
    if duration is None and isinstance(entry.get("duration_ms"), (int, float)):
        duration = round(entry["duration_ms"] / 1000)
    return {
        "title": title,
        "artist": artist,
        "duration": duration,
        "source_url": entry.get("webpage_url") or entry.get("url") or "",
    }


def _yandex_track(entry: dict) -> dict | None:
    title = str(entry.get("title") or "").strip()
    artists = entry.get("artists") or []
    artist = ", ".join(
        str(item.get("name") or "").strip()
        for item in artists if isinstance(item, dict) and item.get("name")
    )
    if not title:
        return None
    duration_ms = entry.get("durationMs")
    return {
        "title": title,
        "artist": artist,
        "duration": round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None,
        "source_url": "",
    }


def _extract_yandex_playlist(url: str) -> dict | None:
    """Read a public Yandex Music playlist through its public playlist handler."""
    parsed = urlparse(url)
    match = re.search(r"/users/([^/]+)/playlists/(\d+)", unquote(parsed.path))
    if not match:
        return None
    owner, kind = match.groups()
    try:
        response = requests.get(
            "https://music.yandex.ru/handlers/playlist.jsx",
            params={"owner": owner, "kinds": kind, "light": "true", "lang": "ru"},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; RhythmPlaylistBot/1.0)",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    playlist = payload.get("playlist") if isinstance(payload, dict) else None
    if not isinstance(playlist, dict):
        return None
    tracks = [
        track for entry in (playlist.get("tracks") or [])
        if isinstance(entry, dict) and (track := _yandex_track(entry))
    ]
    if not tracks:
        return None
    return {"title": str(playlist.get("title") or "Яндекс Музыка"), "tracks": tracks}


def _spotify_credentials() -> tuple[str, str]:
    return (
        os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
        os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
    )


def _extract_spotify_playlist(url: str) -> dict | None:
    """Use Spotify's official Web API when optional client credentials are configured."""
    match = re.search(r"/playlist/([A-Za-z0-9]+)", urlparse(url).path)
    client_id, client_secret = _spotify_credentials()
    if not match or not client_id or not client_secret:
        return None
    playlist_id = match.group(1)
    try:
        token_response = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": "Basic " + base64.b64encode(
                    f"{client_id}:{client_secret}".encode()
                ).decode(),
            },
            timeout=(5, 20),
        )
        token_response.raise_for_status()
        token = token_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        endpoint = f"https://api.spotify.com/v1/playlists/{playlist_id}"
        response = requests.get(
            endpoint, params={"fields": "name,tracks(next,items(track(name,artists(name),duration_ms,external_urls)))"},
            headers=headers, timeout=(5, 20),
        )
        response.raise_for_status()
        payload = response.json()
        tracks = []
        page = payload.get("tracks") or {}
        while True:
            for row in page.get("items") or []:
                item = row.get("track") if isinstance(row, dict) else None
                if not isinstance(item, dict):
                    continue
                track = _track_from_entry({
                    "title": item.get("name"),
                    "artists": item.get("artists") or [],
                    "duration_ms": item.get("duration_ms"),
                    "webpage_url": (item.get("external_urls") or {}).get("spotify", ""),
                })
                if track:
                    tracks.append(track)
            next_url = page.get("next")
            if not next_url:
                break
            next_response = requests.get(next_url, headers=headers, timeout=(5, 20))
            next_response.raise_for_status()
            page = next_response.json()
        if tracks:
            return {"title": str(payload.get("name") or "Spotify playlist"), "tracks": tracks}
    except (requests.RequestException, ValueError, KeyError):
        return None
    return None


def _extract_apple_playlist(url: str) -> dict | None:
    """Use Apple Music's catalog API when a developer token is configured."""
    token = os.getenv("APPLE_MUSIC_DEVELOPER_TOKEN", "").strip()
    parsed = urlparse(url)
    match = re.search(r"/(playlist|album)/[^/]+/(pl\.[A-Za-z0-9.-]+|[0-9]+)", unquote(parsed.path))
    if not token or not match:
        return None
    resource_type, resource_id = match.groups()
    storefront_match = re.search(r"/([a-z]{2})/(?:playlist|album)/", parsed.path, re.IGNORECASE)
    storefront = (storefront_match.group(1) if storefront_match else "us").lower()
    endpoint = f"https://api.music.apple.com/v1/catalog/{storefront}/{resource_type}s/{resource_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(endpoint, headers=headers, timeout=(5, 20))
        response.raise_for_status()
        payload = response.json()
        resource = (payload.get("data") or [{}])[0]
        tracks = []
        next_url = ((resource.get("relationships") or {}).get("tracks") or {}).get("next")
        rows = ((resource.get("relationships") or {}).get("tracks") or {}).get("data") or []
        while True:
            for item in rows:
                attributes = item.get("attributes") or {}
                artists = attributes.get("artistName") or ""
                title = attributes.get("name") or ""
                if title:
                    tracks.append({
                        "title": str(title),
                        "artist": str(artists),
                        "duration": round(attributes["durationInMillis"] / 1000)
                        if isinstance(attributes.get("durationInMillis"), (int, float)) else None,
                        "source_url": (attributes.get("url") or ""),
                    })
            if not next_url:
                break
            next_response = requests.get(next_url, headers=headers, timeout=(5, 20))
            next_response.raise_for_status()
            next_payload = next_response.json()
            rows = next_payload.get("data") or []
            next_url = next_payload.get("next")
        if tracks:
            return {"title": str((resource.get("attributes") or {}).get("name") or "Apple Music playlist"), "tracks": tracks}
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None
    return None


def _extract_playlist(url: str) -> dict:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    # Prefer official/public metadata APIs when they are available.
    provider_result = None
    if host in {"music.yandex.ru", "music.yandex.com"}:
        provider_result = _extract_yandex_playlist(url)
    elif host in {"open.spotify.com", "spotify.com", "www.spotify.com"}:
        provider_result = _extract_spotify_playlist(url)
    elif host == "music.apple.com":
        provider_result = _extract_apple_playlist(url)
    if provider_result:
        return provider_result

    # yt-dlp supports some public playlist and single-track URLs as a fallback.
    try:
        with YoutubeDL(_options()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise PlaylistImportError(
            "Не удалось прочитать эту ссылку. Проверь, что плейлист публичный. "
            "Для Spotify и Apple Music добавь API-токены в Railway Variables; "
            "Яндекс Музыка поддерживается для публичных плейлистов."
        ) from exc
    if not info:
        raise PlaylistImportError(
            "Источник не вернул список композиций. Проверь, что плейлист доступен по ссылке."
        )
    entries = info.get("entries")
    if entries is None:
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
            "Список песен не получен. Spotify и Apple Music требуют API-токены, если публичный экстрактор не сработал."
        )
    return {
        "title": str(info.get("title") or info.get("playlist_title") or "Импортированный плейлист"),
        "tracks": tracks,
    }


async def extract_playlist(url: str) -> dict:
    """Extract playlist metadata without downloading audio; runs network work off-loop."""
    if not is_music_url(url):
        raise PlaylistImportError("Пришли ссылку на плейлист Яндекс Музыки, Apple Music, Spotify или YouTube.")
    return await asyncio.to_thread(_extract_playlist, url)
