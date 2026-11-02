"""Known-empty personal playlists are valid, not failed playlist reads.

All account, response and media fixtures are synthetic. The user confirmed an
empty playlist; they did not provide its raw track-API response.
"""
from pathlib import Path
import json
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))
from kugou import KugouError, KugouMedia
from kugou_library import KugouAuthError, KugouLibraryClient, KugouPlaylist, KugouSession
from kugou_queue import download_kugou

SESSION = KugouSession("123", "synthetic-empty-playlist-token")
TRACKS_PATH = "/v4/get_list_all_file"


def envelope(data, **extra):
    return {"status": 1, "error_code": 0, "data": data, **extra}


class EmptyTrackTests(unittest.TestCase):
    def client(self, data, *, payload=None):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.path, TRACKS_PATH)
            self.assertEqual(json.loads(request.content)["page"], 1)
            return httpx.Response(200, json=payload if payload is not None else envelope(data))
        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        return KugouLibraryClient(SESSION, http), calls

    def resolve(self, data, expected=0):
        client, calls = self.client(data)
        collection = client.resolve_playlist(KugouPlaylist("1", "Empty", expected, list_id="1"))
        self.assertEqual(collection.tracks, [])
        self.assertEqual(len(calls), 1)
        return collection

    def test_confirmed_zero_accepts_explicit_null_info(self):
        self.resolve({"info": None})

    def test_response_zero_accepts_null_without_preview_count(self):
        for key in ("count", "total", "total_count"):
            for value in (0, "0"):
                with self.subTest(key=key, value=value):
                    self.resolve({"info": None, key: value}, expected=None)

    def test_confirmed_zero_accepts_explicit_nested_null_file(self):
        self.resolve({"info": {"file": None}, "count": 0})

    def test_explicit_empty_array_keeps_working(self):
        self.resolve({"info": [], "count": 0})

    def test_unknown_or_nonempty_playlist_cannot_default_to_empty(self):
        for expected in (None, 1, 17):
            for data in ({"info": None}, {"info": {"file": None}}):
                client, _ = self.client(data)
                with self.subTest(expected=expected, data=data), self.assertRaises(KugouError):
                    client.resolve_playlist(KugouPlaylist("1", "Not verified empty", expected))

    def test_missing_and_malformed_fields_do_not_become_empty(self):
        for data in ({}, {"info": {}}, {"info": ""}, {"info": False}, {"info": 0},
                     {"info": "null"}, {"info": [None]}, {"info": {"file": ""}},
                     {"info": None, "items": None}, {"info": None, "items": ""}):
            client, _ = self.client({**data, "count": 0})
            with self.subTest(data=data), self.assertRaises(KugouError):
                client.resolve_playlist(KugouPlaylist("1", "Empty", 0))

    def test_null_still_rejects_conflicting_totals_page_and_more(self):
        for data in ({"info": None, "count": 1},
                     {"info": None, "total": 0, "count": 1},
                     {"info": None, "count": "bad"},
                     {"info": None, "count": False},
                     {"info": None, "page": 2},
                     {"info": None, "has_more": 1},
                     {"info": None, "has_more": 1.0}):
            client, _ = self.client(data)
            with self.subTest(data=data), self.assertRaises(KugouError):
                client.resolve_playlist(KugouPlaylist("1", "Empty", 0))

    def test_positive_preview_count_cannot_be_replaced_by_zero(self):
        client, _ = self.client({"info": None, "count": 0})
        with self.assertRaises(KugouError):
            client.resolve_playlist(KugouPlaylist("1", "Changed", 17))

    def test_nonempty_alternate_list_is_not_hidden_by_null(self):
        song = {"hash": "A" * 32, "filename": "Fixture", "fileid": 1}
        client, _ = self.client({"info": None, "items": [song], "count": 1})
        result = client.resolve_playlist(KugouPlaylist("1", "One", 1))
        self.assertEqual(len(result.tracks), 1)
        client, _ = self.client({"info": None, "items": [song], "count": 0})
        with self.assertRaises(KugouError):
            client.resolve_playlist(KugouPlaylist("1", "Empty", 0))

    def test_auth_and_account_checks_still_precede_empty_result(self):
        client, _ = self.client({}, payload=envelope({"info": None, "count": 0}, status=0, error_code=20010))
        with self.assertRaises(KugouAuthError):
            client.resolve_playlist(KugouPlaylist("1", "Empty", 0))
        client, _ = self.client({"info": None, "count": 0, "userid": 456})
        with self.assertRaises(KugouError):
            client.resolve_playlist(KugouPlaylist("1", "Empty", 0))

    def test_cancelled_empty_playlist_does_not_return_success(self):
        client, calls = self.client({"info": None, "count": 0})
        class Cancelled(Exception):
            pass
        def cancel():
            if calls:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            client.resolve_playlist(KugouPlaylist("1", "Empty", 0), check_cancelled=cancel)
        self.assertEqual(len(calls), 1)

    def test_mixed_queue_counts_empty_playlist_as_success_not_a_failure(self):
        # Real library parser + real queue, synthetic HTTP and media. A small
        # host facade keeps this regression independent of yt-dlp imports.
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.path, TRACKS_PATH)
            body = json.loads(request.content)
            if body["listid"] == 1:
                return httpx.Response(200, json=envelope({"info": None, "count": 0}))
            rows = [{"hash": f"{n:032X}", "filename": f"Fixture {n}", "fileid": n}
                    for n in range(1, 18)]
            return httpx.Response(200, json=envelope({"info": rows, "count": 17}))
        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        client = KugouLibraryClient(SESSION, http)
        def media(track, **_):
            if int(track.hash, 16) > 8:
                raise KugouError("synthetic unavailable track")
            return KugouMedia("https://example.invalid/fixture.mp3", track.title)
        def download(media, destination, **_):
            destination.write_bytes(b"synthetic-media-not-real-audio")
            return destination
        client.resolve_track = Mock(side_effect=media)
        client.download = Mock(side_effect=download)
        with tempfile.TemporaryDirectory() as tmp:
            task = SimpleNamespace(id="task", url="https://www.kugou.com/", mode="audio",
                                   kugou_confirmed=True, kugou_user_id=SESSION.user_id)
            manager = SimpleNamespace(download_root=Path(tmp), cancelled=set(), lock=threading.RLock(),
                                      emit=lambda: None, _kugou_sessions={"task": SESSION},
                                      _kugou_plans={"task": [KugouPlaylist("1", "Empty", 0),
                                                            KugouPlaylist("2", "Favorites", 17)]})
            host = SimpleNamespace(now_iso=lambda: "fixture-time", HostLogger=lambda: Mock())
            with patch.dict(sys.modules, {"host_legacy": host}):
                with self.assertRaises(RuntimeError) as caught:
                    download_kugou(manager, task, type("Cancelled", (Exception,), {}), client=client)
            self.assertIn("9/17", str(caught.exception))
            self.assertNotIn("张歌单读取失败", str(caught.exception))
            self.assertTrue((Path(tmp) / "audio/kugou/用户 123/Empty [1]").is_dir())
            self.assertEqual(task.item_count, 17)
            self.assertEqual(task.playlist_count, 2)
            self.assertEqual(len(list(Path(tmp).rglob("*.mp3"))), 8)
            self.assertEqual(client.resolve_track.call_count, 17)
            self.assertEqual(client.download.call_count, 8)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
