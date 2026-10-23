"""HTTP-transport tests for full Kugou collections and plain audio transfers."""

import json
import gzip
from pathlib import Path
import sys
import tempfile
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))

from kugou import KugouClient, KugouError, KugouMedia, KugouTrack


HASH_A, HASH_B, HASH_C = "A" * 32, "B" * 32, "C" * 32
PLAYLIST_URL = "https://www.kugou.com/yy/special/single/collection_3_42_7_0.html"


def row(song_hash, name="Artist - Song"):
    return {"hash": song_hash, "album_id": 123, "name": name, "timelen": 180000,
            "mixsongid": 456, "singerinfo": [{"name": "Artist"}]}


def playlist_page():
    # The first-page preview deliberately differs from the complete API data.
    return '<script>var data=[{"hash":"' + HASH_C + '"}]; var specialInfo=' + json.dumps({
        "id": 789, "name": "Full list &amp; title",
    }) + ";</script>"


def playback(url="https://fs.kugou.com/song.mp3", **extra):
    return {"status": 1, "url": url, "timeLength": 180, "extName": "mp3",
            "fileName": "Artist - Song", "fileSize": 6, **extra}


class Cancelled(Exception):
    pass


class TestKugou(unittest.TestCase):
    def client(self, handler):
        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport)
        self.addCleanup(http.close)
        return KugouClient(http)

    def test_full_collection_paginates_and_deduplicates_in_order(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.assertNotIn("cookie", request.headers)
            if request.url.host == "www.kugou.com":
                return httpx.Response(200, text=playlist_page(), headers={"Set-Cookie": "unused=1; Domain=.kugou.com"})
            page = int(request.url.params["page"])
            self.assertEqual(request.url.params["specialid"], "789")
            self.assertEqual(request.headers["x-router"], "pubsongscdn.kugou.com")
            self.assertEqual(len(request.url.params["signature"]), 32)
            rows = [row(HASH_A), row(HASH_B)] if page == 1 else [row(HASH_B), row(HASH_C)]
            return httpx.Response(200, json={"status": 1, "data": {"info": rows, "count": 4, "page": page}})

        client = self.client(handler)
        client.page_size = 2
        collection = client.resolve_collection(PLAYLIST_URL)
        self.assertEqual(collection.id, "collection_3_42_7_0")
        self.assertEqual(collection.title, "Full list & title")
        self.assertEqual([t.id for t in collection.tracks], [HASH_A, HASH_B, HASH_C])
        self.assertEqual(collection.tracks[0].title, "Artist - Song")
        self.assertEqual(collection.tracks[0].duration, 180)
        self.assertEqual([r.url.params.get("page") for r in calls], [None, "1", "2"])

    def test_failed_full_api_does_not_succeed_with_web_preview(self):
        def handler(request):
            if request.url.host == "www.kugou.com":
                return httpx.Response(200, text=playlist_page())
            return httpx.Response(200, json={"status": 0, "error_code": 20010, "errmsg": "denied"})
        with self.assertRaisesRegex(KugouError, "不会把网页前 30 首"):
            self.client(handler).resolve_collection(PLAYLIST_URL)

    def test_repeated_page_is_not_a_successful_playlist(self):
        def handler(request):
            if request.url.host == "www.kugou.com":
                return httpx.Response(200, text=playlist_page())
            return httpx.Response(200, json={"status": 1, "data": {"count": 10, "info": [row(HASH_A)]}})
        with self.assertRaisesRegex(KugouError, "重复返回"):
            self.client(handler).resolve_collection(PLAYLIST_URL)

    def test_early_empty_page_is_an_error(self):
        def handler(request):
            if request.url.host == "www.kugou.com":
                return httpx.Response(200, text=playlist_page())
            rows = [row(HASH_A)] if request.url.params["page"] == "1" else []
            return httpx.Response(200, json={"status": 1, "data": {"count": 3, "info": rows}})
        with self.assertRaisesRegex(KugouError, "提前结束"):
            self.client(handler).resolve_collection(PLAYLIST_URL)

    def test_missing_total_does_not_guess_completeness(self):
        def handler(request):
            if request.url.host == "www.kugou.com":
                return httpx.Response(200, text=playlist_page())
            return httpx.Response(200, json={"status": 1, "data": {"info": [row(HASH_A)]}})
        with self.assertRaisesRegex(KugouError, "完整性"):
            self.client(handler).resolve_collection(PLAYLIST_URL)

    def test_more_rows_than_declared_total_is_an_error(self):
        def handler(request):
            if request.url.host == "www.kugou.com":
                return httpx.Response(200, text=playlist_page())
            return httpx.Response(200, json={"status": 1, "data": {
                "info": [row(HASH_A), row(HASH_B)], "count": 1,
            }})
        with self.assertRaisesRegex(KugouError, "超过接口总数"):
            self.client(handler).resolve_collection(PLAYLIST_URL)

    def test_playlist_changes_between_pages_require_retry(self):
        def handler(request):
            if request.url.host == "www.kugou.com":
                return httpx.Response(200, text=playlist_page())
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"status": 1, "data": {"info": [row(HASH_A)], "count": page + 2}})
        with self.assertRaisesRegex(KugouError, "读取期间发生变化"):
            self.client(handler).resolve_collection(PLAYLIST_URL)

    def test_homepage_redirect_cannot_be_mistaken_for_collection(self):
        client = self.client(lambda _: httpx.Response(200, text="<html>首页</html>"))
        with self.assertRaisesRegex(KugouError, "specialInfo"):
            client.resolve_collection(PLAYLIST_URL)

    def test_cancellation_between_pages_propagates(self):
        pages = []
        def handler(request):
            if request.url.host == "www.kugou.com":
                return httpx.Response(200, text=playlist_page())
            pages.append(request.url.params["page"])
            return httpx.Response(200, json={"status": 1, "data": {"info": [row(HASH_A)], "count": 2}})
        def check():
            if pages:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            self.client(handler).resolve_collection(PLAYLIST_URL, check_cancelled=check)
        self.assertEqual(pages, ["1"])

    def test_hash_single_track_needs_no_metadata_request(self):
        client = self.client(lambda _: self.fail("hash classification must not request a page"))
        collection = client.resolve_collection(f"https://www.kugou.com/song/#hash={HASH_A.lower()}&album_id=123")
        self.assertEqual(len(collection.tracks), 1)
        self.assertEqual(collection.tracks[0].id, HASH_A)
        self.assertEqual(collection.tracks[0].album_id, "123")

    def test_modern_mixsong_page_yields_hash_metadata(self):
        text = "var dataFromSmarty = " + json.dumps([{
            "hash": HASH_A, "song_name": "Song", "author_name": "Artist", "album_id": 123,
            "mixsongid": 456, "timelength": 180000,
        }]) + ", playType='search_single';"
        collection = self.client(lambda _: httpx.Response(200, text=text)).resolve_collection(
            "https://www.kugou.com/mixsong/j5ete87.html")
        self.assertEqual(collection.tracks[0].mixsongid, "456")
        self.assertEqual(collection.tracks[0].title, "Artist - Song")

    def test_foreign_entry_and_redirect_rejected(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/data"})
        client = self.client(handler)
        with self.assertRaises(ValueError):
            client.resolve_collection("https://www.kugou.com.evil.example/song/#hash=" + HASH_A)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(KugouError, "非酷狗"):
            client.resolve_collection(PLAYLIST_URL)
        self.assertEqual(len(calls), 1)

    def test_anonymous_playback_and_stream_do_not_send_cookies(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertNotIn("cookie", request.headers)
            if request.url.host == "m.kugou.com":
                return httpx.Response(200, json=playback(), headers={"Set-Cookie": "server=1; Domain=.kugou.com"})
            return httpx.Response(200, content=b"abcdef", headers={"Content-Type": "audio/mpeg"})
        client = self.client(handler)
        client.client.cookies.set("unrelated_session", "not-for-kugou", domain=".kugou.com")
        media = client.resolve_track(KugouTrack(HASH_A, hash=HASH_A))
        events = []
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "song.mp3"
            self.assertEqual(client.download(media, target, progress=events.append), target)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertFalse(target.with_suffix(".mp3.part").exists())
        self.assertEqual(events[-1]["status"], "finished")
        self.assertEqual(events[-1]["downloaded_bytes"], 6)
        self.assertEqual(len(calls), 2)

    def test_playback_fallback_uses_web_endpoint(self):
        def handler(request):
            if request.url.host == "m.kugou.com":
                return httpx.Response(200, json={"status": 0, "url": "", "error": "not found"})
            self.assertEqual(request.url.params["album_id"], "123")
            return httpx.Response(200, json={"status": 1, "data": {
                "play_url": "https://fs.kugou.com/song.flac", "timelength": 180000, "filesize": 600,
                "audio_name": "Artist - Web song",
            }})
        media = self.client(handler).resolve_track(KugouTrack(HASH_A, hash=HASH_A, album_id="123"))
        self.assertEqual(media.ext, "flac")
        self.assertEqual(media.duration, 180)

    def test_paid_or_unavailable_track_is_not_success(self):
        client = self.client(lambda _: httpx.Response(200, json={"status": 0, "url": "", "error": "需要付费"}))
        with self.assertRaisesRegex(KugouError, "需要付费"):
            client.resolve_track(KugouTrack(HASH_A, hash=HASH_A))

    def test_preview_duration_and_explicit_preview_flag_rejected(self):
        for result in (playback(timeLength=60), playback(is_free_part=1)):
            with self.subTest(result=result):
                def handler(request):
                    if request.url.host == "m.kugou.com":
                        return httpx.Response(200, json=result)
                    return httpx.Response(200, json={"status": 0, "data": []})
                with self.assertRaisesRegex(KugouError, "试听片段"):
                    self.client(handler).resolve_track(KugouTrack(HASH_A, hash=HASH_A, duration=180))

    def test_encrypted_or_non_http_media_rejected(self):
        for url in ("https://fs.kugou.com/song.kgm", "file:///etc/passwd", "https://fs.kugou.com/playlist.m3u8"):
            with self.subTest(url=url):
                with self.assertRaises(KugouError):
                    KugouClient._media(playback(url, extName=""), KugouTrack(HASH_A), mobile=True)

    def test_cancel_mid_stream_removes_partial_file(self):
        cancelled = False
        client = self.client(lambda _: httpx.Response(200, content=b"x" * (128 * 1024)))
        def progress(_):
            nonlocal cancelled
            cancelled = True
        def check():
            if cancelled:
                raise Cancelled()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "song.mp3"
            with self.assertRaises(Cancelled):
                client.download(KugouMedia("https://fs.kugou.com/song.mp3", "Song"), target,
                                progress=progress, check_cancelled=check)
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_error_document_is_not_saved_as_audio(self):
        client = self.client(lambda _: httpx.Response(200, text="<html>Login</html>"))
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "song.mp3"
            with self.assertRaisesRegex(KugouError, "错误页面"):
                client.download(KugouMedia("https://fs.kugou.com/song.mp3", "Song"), target)
            self.assertFalse(target.exists())

    def test_truncated_transfer_is_not_published(self):
        client = self.client(lambda _: httpx.Response(200, content=b"short", headers={"Content-Length": "100"}))
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "song.mp3"
            with self.assertRaisesRegex(KugouError, "不完整"):
                client.download(KugouMedia("https://fs.kugou.com/song.mp3", "Song"), target)
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_short_media_with_matching_http_length_is_not_published(self):
        # The server says this short response is complete; the playback API
        # supplies the independent expected size of the complete song.
        client = self.client(lambda _: httpx.Response(200, content=b"short", headers={"Content-Length": "5"}))
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "song.mp3"
            with self.assertRaisesRegex(KugouError, "不完整"):
                client.download(KugouMedia("https://fs.kugou.com/song.mp3", "Song", filesize=100), target)
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_encoded_http_response_compares_decoded_media_size(self):
        decoded = b"valid media bytes"
        encoded = gzip.compress(decoded)
        client = self.client(lambda _: httpx.Response(200, content=encoded, headers={
            "Content-Encoding": "gzip", "Content-Length": str(len(encoded)), "Content-Type": "audio/mpeg",
        }))
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "song.mp3"
            client.download(KugouMedia("https://fs.kugou.com/song.mp3", "Song", filesize=len(decoded)), target)
            self.assertEqual(target.read_bytes(), decoded)

    def test_existing_file_is_never_overwritten(self):
        client = self.client(lambda _: self.fail("existing files must not trigger downloads"))
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "song.mp3"
            target.write_bytes(b"existing")
            with self.assertRaisesRegex(KugouError, "拒绝覆盖"):
                client.download(KugouMedia("https://fs.kugou.com/song.mp3", "Song"), target)
            self.assertEqual(target.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
