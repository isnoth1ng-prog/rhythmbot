from __future__ import annotations

import base64
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
YT_PROXY = os.getenv("YT_PROXY", "").strip() or None
YOUTUBE_COOKIES_B64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
MIN_TRACK_SECONDS = max(1, int(os.getenv("MIN_TRACK_SECONDS", "60")))
MAX_TRACK_SECONDS = max(MIN_TRACK_SECONDS, int(os.getenv("MAX_TRACK_SECONDS", "900")))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/rhythm"))
COOKIE_FILE = DOWNLOAD_DIR / "youtube-cookies.txt"


def prepare_runtime() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if YOUTUBE_COOKIES_B64:
        try:
            decoded = base64.b64decode(YOUTUBE_COOKIES_B64, validate=True)
            COOKIE_FILE.write_bytes(decoded)
            COOKIE_FILE.chmod(0o600)
        except Exception as exc:
            raise RuntimeError("YOUTUBE_COOKIES_B64 is not valid base64") from exc
