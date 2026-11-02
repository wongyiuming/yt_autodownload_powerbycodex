"""Regressions for a successful get_all_list terminal page with info=null.

Only field types came from the user's redacted diagnostic. All identities,
counts, titles, versions and response envelopes below are synthetic fixtures.
"""

from pathlib import Path
import json
import sys
import time
import unittest
from unittest.mock import Mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))

from kugou import KugouError
from kugou_library import KugouAuthError, KugouLibraryClient, KugouPlaylist, KugouSession
from kugou_preview import KugouPreviewError, KugouPreviewStore


TOKEN = "synthetic-null-page-token-not-a-user-credential"
LISTS_PATH = "/v7/get_all_list"


def row(number, **extra):
    return {"listid": number, "type": 0, "list_ver": 1, "m_count": 3,
            "name": f"Playlist {number}",
            "global_collection_id": f"collection_3_123_{number}_0", **extra}


def page(rows, **extra):
    return {"status": 1, "error_code": 0, "data": {
        "info": rows, "list_count": 17, "collect_count": 5,
        "album_count": 0, "total_ver": 42, **extra,
    }}


class NullPlaylistPageTests(unittest.TestCase):
    def client(self, responses):
        requests = []

        def handler(request):
            requests.append(request)
            number = json.loads(request.content)["page"]
            if number > len(responses):
                self.fail("Requested another page after the terminal response")
            return httpx.Response(200, json=responses[number - 1])

        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        client = KugouLibraryClient(KugouSession("123", TOKEN), http)
        return client, requests

    def test_reported_second_page_null_preserves_first_page_playlists(self):
        client, requests = self.client([page([row(1), row(2, type=1)]), page(None)])
        playlists = client.list_playlists()
        self.assertEqual([p.list_id for p in playlists], ["1", "2"])
        self.assertEqual([p.list_type for p in playlists], [0, 1])
        self.assertEqual([p.url for p in playlists], [
            "https://www.kugou.com/yy/special/single/collection_3_123_1_0.html",
            "https://www.kugou.com/yy/special/single/collection_3_123_2_0.html",
        ])
        self.assertEqual([json.loads(r.content)["page"] for r in requests], [1, 2])
        self.assertTrue(all(r.url.path == LISTS_PATH for r in requests))
        self.assertTrue(all(json.loads(r.content)["total_ver"] == 0 for r in requests))

    def test_full_pages_continue_until_explicit_null_terminal_page(self):
        client, requests = self.client([
            page([row(1), row(2)]), page([row(3), row(4)]), page(None),
        ])
        client.page_size = 2
        self.assertEqual([p.list_id for p in client.list_playlists()], ["1", "2", "3", "4"])
        self.assertEqual(len(requests), 3)

    def test_short_page_does_not_skip_a_later_nonempty_page(self):
        client, requests = self.client([page([row(1)]), page([row(2)]), page(None)])
        self.assertEqual(len(client.list_playlists()), 2)
        self.assertEqual(len(requests), 3)

    def test_empty_library_can_return_explicit_null_on_first_page(self):
        client, requests = self.client([page(None, list_count=0, collect_count=0, album_count=0)])
        self.assertEqual(client.list_playlists(), [])
        self.assertEqual(len(requests), 1)

    def test_null_is_not_a_blanket_default_for_missing_or_malformed_fields(self):
        for value in ({}, {"info": {}}, {"info": ""}, {"info": False}, {"info": 0},
                      {"info": "null"}, {"info": [None]}, {"info": {"file": None}},
                      {"list": None}, {"info": None, "items": ""}):
            with self.subTest(value=value):
                client, _ = self.client([{"status": 1, "error_code": 0, "data": value}])
                with self.assertRaises(KugouError) as caught:
                    client.list_playlists()
                self.assertEqual(caught.exception.diagnostic.public()["check"], "rows")

    def test_nonempty_alternate_array_is_not_hidden_by_null_info(self):
        client, requests = self.client([page(None, items=[row(1)]), page(None)])
        self.assertEqual([p.list_id for p in client.list_playlists()], ["1"])
        self.assertEqual(len(requests), 2)

    def test_null_with_more_flag_is_still_incomplete(self):
        for key in ("has_more", "hasmore"):
            with self.subTest(key=key):
                client, _ = self.client([page([row(1)]), page(None, **{key: 1})])
                with self.assertRaises(KugouError) as caught:
                    client.list_playlists()
                diagnostic = caught.exception.diagnostic.public()
                self.assertEqual((diagnostic["code"], diagnostic["check"], diagnostic["page"]),
                                 ("PAGINATION_INVALID", "end", 2))
                self.assertEqual(diagnostic["shape"]["info"], "null")

    def test_null_before_declared_total_is_not_a_success(self):
        for key in ("count", "total", "total_count"):
            with self.subTest(key=key):
                client, _ = self.client([
                    page([row(1)], **{key: 3}), page(None, **{key: 3}),
                ])
                with self.assertRaises(KugouError) as caught:
                    client.list_playlists()
                self.assertEqual(caught.exception.diagnostic.public()["check"], "end")

    def test_first_null_with_positive_declared_total_is_not_an_empty_library(self):
        client, _ = self.client([page(None, count=1)])
        with self.assertRaises(KugouError) as caught:
            client.list_playlists()
        self.assertEqual(caught.exception.diagnostic.public()["check"], "end")

    def test_null_still_checks_page_revision_and_total_contract(self):
        cases = [
            (page([row(1)]), page(None, page=1), "PAGINATION_INVALID", "page"),
            (page([row(1)]), page(None, total_ver=43), "PAGINATION_CHANGED", "version"),
            (page([row(1)], count=3), page(None), "PAGINATION_INVALID", "total"),
            (page([row(1)]), page(None, has_more=1.0), "PAGINATION_INVALID", "end"),
        ]
        for first, last, code, check in cases:
            with self.subTest(check=check, code=code):
                client, _ = self.client([first, last])
                with self.assertRaises(KugouError) as caught:
                    client.list_playlists()
                diagnostic = caught.exception.diagnostic.public()
                self.assertEqual((diagnostic["code"], diagnostic["check"]), (code, check))

    def test_null_does_not_hide_business_or_authentication_failure(self):
        for code, error_type in ((900, KugouError), (20010, KugouAuthError)):
            with self.subTest(code=code):
                failed = {**page(None), "status": 0, "error_code": code, "errmsg": TOKEN}
                client, _ = self.client([page([row(1)]), failed])
                with self.assertRaises(error_type) as caught:
                    client.list_playlists()
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertEqual(caught.exception.diagnostic.public()["api_code"], code)

    def test_unknown_track_count_null_still_requires_rows(self):
        client, requests = self.client([page(None)])
        with self.assertRaises(KugouError) as caught:
            client.resolve_playlist(KugouPlaylist("1", "Songs", None, list_id="1"))
        self.assertEqual(requests[0].url.path, "/v4/get_list_all_file")
        self.assertEqual(caught.exception.diagnostic.public()["check"], "rows")

    def test_cancellation_and_page_cap_still_prevent_completion(self):
        client, requests = self.client([page([row(1)]), page(None)])
        class Cancelled(Exception):
            pass
        def check():
            if requests:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            client.list_playlists(check_cancelled=check)
        self.assertEqual(len(requests), 1)
        client, _ = self.client([page([row(1)])])
        client.max_pages = 1
        with self.assertRaises(KugouError) as caught:
            client.list_playlists()
        self.assertEqual(caught.exception.diagnostic.public()["code"], "PAGINATION_LIMIT")

    def test_repeated_page_cannot_be_hidden_by_a_later_null(self):
        client, requests = self.client([page([row(1)]), page([row(1)]), page(None)])
        with self.assertRaises(KugouError) as caught:
            client.list_playlists()
        self.assertEqual(caught.exception.diagnostic.public()["code"], "PAGINATION_REPEATED")
        self.assertEqual(len(requests), 2)

    def preview(self, responses):
        client, requests = self.client(responses)
        for name in ("resolve_playlist", "resolve_track", "download"):
            setattr(client, name, Mock(side_effect=AssertionError("Preview must remain read-only")))
        store = KugouPreviewStore(client_factory=lambda session: client)
        self.addCleanup(store.close)
        snapshot = store.start(KugouSession("123", TOKEN))
        deadline = time.monotonic() + 3
        while snapshot["status"] == "loading" and time.monotonic() < deadline:
            time.sleep(0.005)
            snapshot = store.get(snapshot["preview_id"])
        self.assertNotEqual(snapshot["status"], "loading")
        for name in ("resolve_playlist", "resolve_track", "download"):
            getattr(client, name).assert_not_called()
        self.assertTrue(all(r.url.path == LISTS_PATH for r in requests))
        self.assertNotIn(TOKEN, json.dumps(snapshot))
        return store, snapshot

    def test_preview_keeps_urls_and_requires_explicit_same_account_consumption(self):
        store, result = self.preview([page([row(1), row(2)]), page(None)])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["playlist_count"], 2)
        self.assertEqual([p["list_id"] for p in result["playlists"]], ["1", "2"])
        self.assertTrue(all(p["url"] for p in result["playlists"]))
        with self.assertRaises(KugouPreviewError):
            store.consume(result["preview_id"], "456")
        self.assertEqual(store.get(result["preview_id"])["status"], "ready")
        confirmed = store.consume(result["preview_id"], "123")
        self.assertEqual([p.list_id for p in confirmed], ["1", "2"])
        with self.assertRaises(KugouPreviewError):
            store.consume(result["preview_id"], "123")

    def test_conflicting_null_page_does_not_expose_partial_ready_preview(self):
        store, result = self.preview([page([row(1)]), page(None, has_more=1)])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["playlists"], [])
        self.assertEqual(result["diagnostic"]["code"], "PAGINATION_INVALID")
        with self.assertRaises(KugouPreviewError):
            store.consume(result["preview_id"], "123")


if __name__ == "__main__":
    unittest.main()
