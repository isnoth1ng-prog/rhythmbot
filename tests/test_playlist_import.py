import unittest

from rhythm.playlist import is_music_url, _track_from_entry


class PlaylistUrlTests(unittest.TestCase):
    def test_accepts_supported_music_services(self):
        for url in (
            "https://music.yandex.ru/users/example/playlists/123",
            "https://music.apple.com/us/playlist/example/pl.123",
            "https://open.spotify.com/playlist/123",
            "https://www.youtube.com/playlist?list=123",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_music_url(url))

    def test_rejects_untrusted_hosts_and_non_urls(self):
        for value in (
            "hello there",
            "https://example.com/playlist/123",
            "https://open.spotify.com.evil.example/playlist/123",
            "file:///etc/passwd",
            "",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_music_url(value))

    def test_extracts_artist_title_from_entry(self):
        self.assertEqual(
            _track_from_entry({"title": "Artist — Song"}),
            {"title": "Song", "artist": "Artist", "duration": None, "source_url": ""},
        )

    def test_prefers_structured_artist_and_title(self):
        self.assertEqual(
            _track_from_entry({"title": "Song", "artist": "Artist", "duration": 123}),
            {"title": "Song", "artist": "Artist", "duration": 123, "source_url": ""},
        )


if __name__ == "__main__":
    unittest.main()
