import unittest

from aiogram.types import InlineQueryResultAudio, InlineQueryResultCachedAudio

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

    def test_preview_inline_result_uses_apple_preview_url(self):
        result = InlineQueryResultAudio(
            id="rh-preview-test",
            audio_url="https://audio-ssl.itunes.apple.com/test.m4a",
            title="В огне",
            performer="Макулатура",
            audio_duration=30,
        )
        self.assertEqual(result.type, "audio")
        self.assertEqual(result.audio_url, "https://audio-ssl.itunes.apple.com/test.m4a")
        self.assertEqual(result.audio_duration, 30)

    def test_track_key_is_normalized(self):
        self.assertEqual(
            _track_key({"artist": "  Макулатура ", "title": " В огне "}),
            "макулатура|в огне",
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
