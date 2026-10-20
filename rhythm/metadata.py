from __future__ import annotations

import asyncio
from io import BytesIO

import aiohttp
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TPE2, TRCK, ID3, ID3NoHeaderError
from PIL import Image


async def _cover_bytes(url: str) -> bytes | None:
    if not url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.read()
        image = Image.open(BytesIO(data)).convert("RGB")
        image.thumbnail((600, 600))
        out = BytesIO()
        image.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()
    except Exception:
        return None


async def tag_mp3(path: str, track: dict) -> None:
    cover = await _cover_bytes(track.get("artwork_url", ""))
    def write_tags() -> None:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("TIT2")
        tags.delall("TPE1")
        tags.delall("TPE2")
        tags.delall("TALB")
        tags.delall("TRCK")
        tags.delall("APIC")
        tags.add(TIT2(encoding=3, text=track["title"]))
        tags.add(TPE1(encoding=3, text=track["artist"]))
        tags.add(TPE2(encoding=3, text=track["artist"]))
        tags.add(TALB(encoding=3, text=track.get("album") or ""))
        tags.add(TRCK(encoding=3, text=""))
        if cover:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=3)
    await asyncio.to_thread(write_tags)
