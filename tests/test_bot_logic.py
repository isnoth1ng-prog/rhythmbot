import unittest

from aiogram.types import InlineQueryResultCachedAudio

from rhythm.__main__ import _track_key
from rhythm.service import _audio_score


class InlineResultTests(unittest.TestCase):
    def test_cached_audio_result_uses_supported_fields(self):
        result = InlineQueryResultCachedAudio(
            id="rh-test-1",
            audio_file_id="telegram-file-id",
        )
        self.assertEqual(result.type, "audio")
        self.assertEqual(result.audio_file_id, "telegram-file-id")

    def test_track_key_is_normalized(self):
        self.assertEqual(
            _track_key({"artist": "  Макулатура ", "title": " В огне "}),
            "ytmusic-v1|макулатура|в огне",
        )

    def test_audio_score_prefers_matching_title_and_artist(self):
        track = {"artist": "Макулатура", "title": "В огне", "duration": 210}
        matching = {
            "title": "Макулатура - В огне",
            "uploader": "Макулатура",
            "duration": 211,
        }
        unrelated = {
            "title": "Совершенно другая песня",
            "uploader": "Другой исполнитель",
            "duration": 211,
        }
        self.assertGreater(_audio_score(matching, track), _audio_score(unrelated, track))

    def test_audio_score_rejects_sped_up_version(self):
        track = {"artist": "Макулатура", "title": "В огне", "duration": 210}
        sped_up = {
            "title": "Макулатура - В огне (speed up)",
            "uploader": "Макулатура",
            "duration": 170,
        }
        self.assertEqual(_audio_score(sped_up, track), 0.0)

    def test_audio_score_rejects_duration_mismatch(self):
        track = {"artist": "Макулатура", "title": "В огне", "duration": 210}
        short_edit = {
            "title": "Макулатура - В огне",
            "uploader": "Макулатура",
            "duration": 165,
        }
        self.assertEqual(_audio_score(short_edit, track), 0.0)

    def test_audio_score_rejects_duration_outside_limits(self):
        track = {"artist": "Макулатура", "title": "В огне", "duration": 210}
        too_short = {
            "title": "Макулатура - В огне",
            "uploader": "Макулатура",
            "duration": 20,
        }
        self.assertEqual(_audio_score(too_short, track), 0.0)


if __name__ == "__main__":
    unittest.main()


class SearchMergeTests(unittest.TestCase):
    def test_search_options_merges_catalog_and_youtube_music(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from rhythm.service import search_options

        catalog = [{
            "artist": "Макулатура", "title": "В огне", "album": "Реприза",
            "duration": 210, "artwork_url": "https://example.test/cover.jpg",
            "track_url": "https://music.apple.com/track/1", "metadata_score": 98,
            "metadata_source": "Apple/iTunes",
        }]
        source = [
            {"source": "youtube_music", "url": "https://music.youtube.com/watch?v=abc",
             "title": "В огне", "duration": 211, "uploader": "Макулатура"},
            {"source": "youtube_music", "url": "https://music.youtube.com/watch?v=def",
             "title": "Другой трек", "duration": 190, "uploader": "Другой исполнитель"},
        ]
        with patch("rhythm.service.search_tracks", new=AsyncMock(return_value=catalog)), \
             patch("rhythm.service.search_source", return_value=source) as source_search:
            results = asyncio.run(search_options("Макулатура — В огне"))

        source_search.assert_called_once()
        self.assertTrue(any(item.get("audio_source") == "youtube_music" for item in results))
        exact = next(item for item in results if item["artist"] == "Макулатура" and item["title"] == "В огне")
        self.assertEqual(exact["metadata_source"], "Apple/iTunes")
        self.assertEqual(exact["track_url"], "https://music.youtube.com/watch?v=abc")
        self.assertEqual(len([item for item in results if item["title"] == "В огне"]), 1)

    def test_search_options_does_not_require_empty_catalog_to_use_youtube_music(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from rhythm.service import search_options

        source = [{
            "source": "youtube_music", "url": "https://music.youtube.com/watch?v=abc",
            "title": "В огне", "duration": 211, "uploader": "Макулатура",
        }]
        with patch("rhythm.service.search_tracks", new=AsyncMock(return_value=[])), \
             patch("rhythm.service.search_source", return_value=source):
            results = asyncio.run(search_options("Макулатура — В огне"))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["artist"], "Макулатура")
        self.assertEqual(results[0]["title"], "В огне")
        self.assertEqual(results[0]["metadata_source"], "YouTube Music")
