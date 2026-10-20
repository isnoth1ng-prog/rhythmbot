from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from yt_dlp import YoutubeDL

from .config import COOKIE_FILE, YT_PROXY

_POOL = ThreadPoolExecutor(max_workers=2)


def _options() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
    }
    if YT_PROXY:
        opts["proxy"] = YT_PROXY
    if COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
    return opts


def search_source(query: str, source: str, limit: int = 5) -> list[dict]:
    prefix = "ytsearch" if source == "youtube" else "scsearch"
    opts = _options()
    opts["extract_flat"] = "in_playlist"
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    results = []
    for entry in entries:
        if not entry:
            continue
        url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
        if not url and entry.get("id"):
            if source == "youtube":
                url = f"https://www.youtube.com/watch?v={entry['id']}"
        results.append({
            "source": source,
            "url": url,
            "title": entry.get("title") or "",
            "duration": entry.get("duration"),
            "uploader": entry.get("uploader") or entry.get("channel") or "",
        })
    return results
