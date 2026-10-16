"""Offline tests: no YouTube credentials or network are needed."""

import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))

from yt_dlp.utils import DownloadError

import host_legacy
import youtube_runtime


class FakeYDL:
    attempts = []
    failures = []

    def __init__(self, options):
        self.options = options
        self.attempts.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def download(self, urls):
        if self.failures:
            error = self.failures.pop(0)
            if isinstance(error, tuple):
                code, message = error
                if message:
                    self.options["logger"].error(message)
                return code
            if error:
                raise DownloadError(error)
        return 0

    def extract_info(self, url, download=False):
        if self.failures:
            error = self.failures.pop(0)
            if error:
                raise DownloadError(error)
        return {"id": "video-id"}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        FakeYDL.attempts = []
        FakeYDL.failures = []
        self.patch = patch.object(youtube_runtime.yt_dlp, "YoutubeDL", FakeYDL)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_retry_reload_without_cookies(self):
        FakeYDL.failures = ["The page needs to be reloaded.", None]
        youtube_runtime.download_with_fallback({"cookiefile": "cookies.txt"}, "https://youtube.com/watch?v=x")
        self.assertEqual(len(FakeYDL.attempts), 2)
        self.assertEqual(FakeYDL.attempts[0]["cookiefile"], "cookies.txt")
        self.assertNotIn("cookiefile", FakeYDL.attempts[1])

    def test_extractor_uses_same_fallback(self):
        FakeYDL.failures = ["The page needs to be reloaded.", None]
        result = youtube_runtime.extract_with_fallback({"cookiefile": "cookies.txt"}, "url")
        self.assertEqual(result["id"], "video-id")
        self.assertNotIn("cookiefile", FakeYDL.attempts[1])

    def test_other_errors_not_retried_without_cookies(self):
        FakeYDL.failures = ["HTTP 403 Forbidden"]
        with self.assertRaises(DownloadError):
            youtube_runtime.download_with_fallback({"cookiefile": "cookies.txt"}, "url")
        self.assertEqual(len(FakeYDL.attempts), 1)

    def test_anonymous_reload_uses_alternate_player_once(self):
        FakeYDL.failures = ["The page needs to be reloaded.", None]
        youtube_runtime.download_with_fallback({}, "url")
        self.assertEqual(len(FakeYDL.attempts), 2)
        self.assertEqual(
            FakeYDL.attempts[1]["extractor_args"]["youtube"]["player_client"],
            ["web_safari", "web_embedded"],
        )

    def test_reload_return_code_uses_recorded_error(self):
        FakeYDL.failures = [
            (1, "ERROR: [youtube] The page needs to be reloaded."),
            None,
        ]
        youtube_runtime.download_with_fallback({"cookiefile": "cookies.txt"}, "url")
        self.assertEqual(len(FakeYDL.attempts), 2)
        self.assertNotIn("cookiefile", FakeYDL.attempts[1])

    def test_reload_fallback_has_three_attempts_max(self):
        FakeYDL.failures = ["The page needs to be reloaded."] * 3
        with self.assertRaises(youtube_runtime.NonRetryableYoutubeError):
            youtube_runtime.download_with_fallback({"cookiefile": "cookies.txt"}, "url")
        self.assertEqual(len(FakeYDL.attempts), 3)

    def test_double_reload_gives_actionable_error(self):
        FakeYDL.failures = ["The page needs to be reloaded."] * 3
        with self.assertRaises(youtube_runtime.NonRetryableYoutubeError):
            youtube_runtime.download_with_fallback({"cookiefile": "cookies.txt"}, "url")
        self.assertEqual(len(FakeYDL.attempts), 3)

    def test_cookies_are_opt_in_like_yt_dlp_cli(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "youtube-cookies.txt"
            path.write_text("# Netscape HTTP Cookie File\n")
            self.assertNotIn("cookiefile", youtube_runtime.youtube_options(None, path))
            self.assertEqual(youtube_runtime.youtube_options(None, path, use_cookies=True)["cookiefile"], str(path))

    def test_options_use_node_and_local_solver(self):
        options = youtube_runtime.youtube_options("C:/Node/node.exe", Path("/absent.txt"))
        self.assertEqual(options["js_runtimes"]["node"]["path"], "C:/Node/node.exe")
        self.assertIn("ejs:github", options["remote_components"])

    def test_old_cookie_file_is_cleared(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "youtube-cookies.txt"
            path.write_text("stale")
            with patch.object(host_legacy, "COOKIE_PATH", path):
                host_legacy.QueueManager._write_cookies(None, [])
            self.assertFalse(path.exists())

    def test_cookie_rejects_unrelated_domains(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "youtube-cookies.txt"
            with patch.object(host_legacy, "COOKIE_PATH", path):
                host_legacy.QueueManager._write_cookies(None, [{
                    "domain": ".youtube.com.evil.example", "name": "SID", "value": "bad",
                }])
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
