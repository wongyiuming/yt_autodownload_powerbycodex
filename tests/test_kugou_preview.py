"""Read-only playlist preview lifecycle, pagination and confirmation tests."""

import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))

from kugou import KugouError
from kugou_library import KugouAuthError, KugouLibraryClient, KugouPlaylist, KugouSession
from kugou_plan import playlist_plan, serialize_plan
from kugou_preview import KugouPreviewError, KugouPreviewStore


USER_ID = "12345"
TOKEN = "private-fixture-token-not-for-preview-json"
SESSION = KugouSession(USER_ID, TOKEN)


def playlist(index=1, *, title=None, count=3, url=None):
    identity = str(index)
    return KugouPlaylist(identity, title if title is not None else f"歌单 {index}", count,
                         url if url is not None else f"https://www.kugou.com/yy/special/single/{identity}.html",
                         identity, 0)


class ReadOnlyClient:
    def __init__(self, rows=None, *, release=None, error=None, cooperative=True):
        self.rows = rows if rows is not None else [playlist()]
        self.release = release
        self.error = error
        self.cooperative = cooperative
        self.entered = threading.Event()
        self.closed = threading.Event()
        self.list_calls = 0

    def list_playlists(self, *, check_cancelled):
        self.list_calls += 1
        self.entered.set()
        if self.release is not None:
            deadline = time.monotonic() + 5
            while not self.release.wait(0.01):
                if self.cooperative:
                    check_cancelled()
                if time.monotonic() >= deadline:
                    raise AssertionError("test fixture was not released")
        if self.cooperative:
            check_cancelled()
        if self.error:
            raise self.error
        return self.rows

    def resolve_playlist(self, *_args, **_kwargs):
        raise AssertionError("Preview must never resolve playlist songs")

    def resolve_track(self, *_args, **_kwargs):
        raise AssertionError("Preview must never request media URLs")

    def download(self, *_args, **_kwargs):
        raise AssertionError("Preview must never download media")

    def close(self):
        self.closed.set()


class KugouPreviewTests(unittest.TestCase):
    def store(self, client=None, **kwargs):
        client = client or ReadOnlyClient()
        factory = Mock(return_value=client)
        store = KugouPreviewStore(factory, **kwargs)
        self.addCleanup(store.close)
        if client.release is not None:
            self.addCleanup(client.release.set)
        return store, client, factory

    def wait_status(self, store, preview_id, expected="ready"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            page = store.get(preview_id)
            if page["status"] == expected:
                return page
            if page["status"] not in {"loading", expected}:
                self.fail(f"Unexpected preview status: {page}")
            time.sleep(0.005)
        self.fail(f"Preview did not reach {expected}")

    def test_start_is_nonblocking_and_only_reads_playlist_list(self):
        release = threading.Event()
        store, client, factory = self.store(ReadOnlyClient(release=release))
        before = time.monotonic()
        initial = store.start(SESSION)
        self.assertLess(time.monotonic() - before, 1)
        self.assertEqual(initial["status"], "loading")
        self.assertEqual(initial["playlists"], [])
        self.assertEqual(initial["user_id"], USER_ID)
        self.assertTrue(client.entered.wait(1))
        self.assertEqual(store.get(initial["preview_id"])["status"], "loading")
        release.set()
        ready = self.wait_status(store, initial["preview_id"])
        self.assertEqual(ready["playlist_count"], 1)
        self.assertEqual(client.list_calls, 1)
        factory.assert_called_once_with(SESSION)
        self.assertTrue(client.closed.wait(1))
        self.assertIsNone(store._records[initial["preview_id"]].session)
        self.assertTrue(store._worker.daemon)

    def test_get_paginates_all_playlists_and_reports_known_unknown_counts(self):
        rows = [playlist(index, count=None if index % 3 == 0 else index) for index in range(1, 152)]
        store, client, _factory = self.store(ReadOnlyClient(rows))
        preview_id = store.start(SESSION)["preview_id"]
        first = self.wait_status(store, preview_id)
        self.assertEqual(len(first["playlists"]), 50)
        self.assertEqual(first["playlist_count"], 151)
        self.assertEqual(first["known_track_count"], sum(row.count for row in rows if row.count is not None))
        self.assertEqual(first["unknown_track_count"], 50)
        seen, offset = [], 0
        while offset is not None:
            page = store.get(preview_id, offset=offset, limit=60)
            self.assertLessEqual(len(page["playlists"]), 60)
            seen.extend(item["id"] for item in page["playlists"])
            offset = page["next_offset"]
        self.assertEqual(seen, [str(index) for index in range(1, 152)])
        self.assertEqual(len(store.get(preview_id, limit=10000)["playlists"]), 100)
        self.assertEqual(store.get(preview_id, offset=200)["playlists"], [])
        self.assertIsNone(store.get(preview_id, offset=200)["next_offset"])
        self.assertEqual(client.list_calls, 1, "pagination never repeats API enumeration")

    def test_large_titles_are_bounded_and_preview_messages_do_not_contain_token(self):
        title = TOKEN + "\n" + "歌😀" * 2000
        rows = [playlist(index, title=title) for index in range(1, 201)]
        store, _client, _factory = self.store(ReadOnlyClient(rows))
        initial = store.start(SESSION)
        self.assertNotIn(TOKEN, json.dumps(initial, ensure_ascii=False))
        self.wait_status(store, initial["preview_id"])
        page = store.get(initial["preview_id"], limit=10000)
        encoded = json.dumps(page, ensure_ascii=True).encode("utf-8")
        self.assertLess(len(encoded), 1024 * 1024)
        self.assertNotIn(TOKEN.encode(), encoded)
        self.assertNotIn('"token"', encoded.decode())
        self.assertNotIn('"session"', encoded.decode())
        self.assertEqual(len(page["playlists"]), 100)
        self.assertTrue(all(len(item["title"]) <= 256 for item in page["playlists"]))
        self.assertTrue(all("\n" not in item["title"] for item in page["playlists"]))
        accepted = store.consume(initial["preview_id"], USER_ID)
        self.assertEqual(accepted[0].title, page["playlists"][0]["title"])
        self.assertEqual(accepted[0].url, page["playlists"][0]["url"])

    def test_missing_url_stays_empty_and_consumption_matches_display(self):
        row = playlist(url="", title="  A\nB  ")
        store, _client, _factory = self.store(ReadOnlyClient([row]))
        preview_id = store.start(SESSION)["preview_id"]
        page = self.wait_status(store, preview_id)
        self.assertEqual(page["playlists"][0]["url"], "")
        self.assertEqual(page["playlists"][0]["title"], "A B")
        accepted = store.consume(preview_id, USER_ID)
        self.assertIsInstance(accepted, tuple)
        self.assertEqual(accepted[0].url, "")
        self.assertEqual(accepted[0].title, "A B")
        consumed = store.get(preview_id)
        self.assertEqual(consumed["status"], "consumed")
        self.assertEqual(consumed["playlists"], [])
        self.assertEqual(consumed["playlist_count"], 1)

    def test_real_library_descriptors_survive_preview_confirmation_and_persistence(self):
        rows = [
            {"listid": 7, "type": 0, "name": "Own", "count": 2,
             "global_collection_id": "collection_3_12345_7_0"},
            {"listid": 8, "type": 0, "name": "Private"},
            {"listid": 9, "type": 1, "name": "Collected", "m_count": 11,
             "global_collection_id": "collection_3_12345_9_0",
             "list_create_gid": "collection_3_9988_123_0"},
            {"listid": 10, "type": 1, "name": "Collected local ID", "count": 5,
             "create_gid": "collection_3_7788_456_0"},
            {"listid": 11, "type": 1, "name": "Collected without public URL", "count": 0},
            {"listid": 7, "type": 1, "name": "Same numeric ID, different type", "count": 1,
             "list_create_gid": "collection_3_4455_789_0"},
        ]
        requests = []

        def handle(request):
            requests.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v7/get_all_list")
            return httpx.Response(200, json={"status": 1, "error_code": 0,
                                           "data": {"info": rows, "count": len(rows), "page": 1}})

        http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(http.close)
        store = KugouPreviewStore(lambda session: KugouLibraryClient(session, http))
        self.addCleanup(store.close)
        preview_id = store.start(SESSION)["preview_id"]
        page = self.wait_status(store, preview_id)
        accepted = store.consume(preview_id, USER_ID)
        saved = serialize_plan(accepted)
        self.assertEqual(saved, page["playlists"])
        self.assertEqual(playlist_plan(json.loads(json.dumps(saved))), accepted)
        self.assertEqual(len(requests), 1, "preview only fetches the library index")
        self.assertEqual(accepted[0].id, "collection_3_12345_7_0")
        self.assertEqual(accepted[0].list_id, "7")
        self.assertEqual(accepted[1].url, "")
        self.assertEqual(accepted[2].id, "collection_3_12345_9_0")
        self.assertEqual(accepted[2].list_id, "9")
        self.assertEqual(accepted[2].url, "https://www.kugou.com/yy/special/single/collection_3_9988_123_0.html")
        self.assertEqual(accepted[3].id, "10")
        self.assertEqual(accepted[4].url, "")
        self.assertEqual((accepted[5].list_id, accepted[5].list_type), ("7", 1))

    def test_preview_rejects_descriptors_that_cannot_be_persisted(self):
        for row in (KugouPlaylist("1", "Missing local ID", 1, "", "", 0),
                    playlist(url="https://kugou.com/yy/special/single/1.html")):
            with self.subTest(title=row.title):
                store, _client, _factory = self.store(ReadOnlyClient([row]))
                preview_id = store.start(SESSION)["preview_id"]
                page = self.wait_status(store, preview_id, "error")
                self.assertEqual(page["playlists"], [])
                with self.assertRaises(KugouPreviewError):
                    store.consume(preview_id, USER_ID)

    def test_wrong_account_does_not_consume_ready_preview(self):
        store, _client, _factory = self.store()
        preview_id = store.start(SESSION)["preview_id"]
        self.wait_status(store, preview_id)
        with self.assertRaisesRegex(KugouPreviewError, "账号不同"):
            store.consume(preview_id, "67890")
        self.assertEqual(store.get(preview_id)["status"], "ready")
        self.assertEqual(len(store.consume(preview_id, USER_ID)), 1)
        with self.assertRaises(KugouPreviewError):
            store.consume(preview_id, USER_ID)

    def test_two_simultaneous_confirmations_can_only_consume_once(self):
        store, _client, _factory = self.store()
        preview_id = store.start(SESSION)["preview_id"]
        self.wait_status(store, preview_id)
        barrier = threading.Barrier(3)
        results = []

        def confirm():
            barrier.wait()
            try:
                results.append(store.consume(preview_id, USER_ID))
            except KugouPreviewError:
                results.append("rejected")

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(isinstance(result, tuple) for result in results), 1)
        self.assertEqual(results.count("rejected"), 1)

    def test_loading_cannot_be_confirmed(self):
        release = threading.Event()
        store, _client, _factory = self.store(ReadOnlyClient(release=release))
        preview_id = store.start(SESSION)["preview_id"]
        with self.assertRaises(KugouPreviewError):
            store.consume(preview_id, USER_ID)
        release.set()
        self.wait_status(store, preview_id)
        self.assertEqual(len(store.consume(preview_id, USER_ID)), 1)

    def test_expiry_invalidates_ready_preview_without_retaining_session(self):
        store, _client, _factory = self.store(ttl_seconds=10)
        clock = Mock(return_value=100.0)
        store._clock = clock
        preview_id = store.start(SESSION)["preview_id"]
        self.wait_status(store, preview_id)
        clock.return_value = 110.0
        page = store.get(preview_id)
        self.assertEqual(page["status"], "cancelled")
        self.assertIn("过期", page["error"])
        self.assertEqual(page["playlists"], [])
        self.assertIsNone(store._records[preview_id].session)
        with self.assertRaises(KugouPreviewError):
            store.consume(preview_id, USER_ID)

    def test_cancelled_or_expired_read_cannot_publish_a_late_result(self):
        for expire in (False, True):
            with self.subTest(expire=expire):
                release = threading.Event()
                store, client, _factory = self.store(ReadOnlyClient(release=release, cooperative=False), ttl_seconds=10)
                clock = Mock(return_value=100.0)
                store._clock = clock
                preview_id = store.start(SESSION)["preview_id"]
                self.assertTrue(client.entered.wait(1))
                if expire:
                    clock.return_value = 111.0
                    page = store.get(preview_id)
                else:
                    page = store.cancel(preview_id)
                self.assertEqual(page["status"], "cancelled")
                release.set()
                self.assertTrue(client.closed.wait(1))
                self.assertEqual(store.get(preview_id)["status"], "cancelled")
                self.assertEqual(store.get(preview_id)["playlists"], [])
                with self.assertRaises(KugouPreviewError):
                    store.consume(preview_id, USER_ID)

    def test_cancel_reaches_api_cancellation_callback(self):
        release = threading.Event()
        store, client, _factory = self.store(ReadOnlyClient(release=release))
        preview_id = store.start(SESSION)["preview_id"]
        self.assertTrue(client.entered.wait(1))
        store.cancel(preview_id)
        self.assertTrue(client.closed.wait(1), "worker must stop without waiting for fixture release")
        self.assertEqual(store.get(preview_id)["status"], "cancelled")

    def test_error_is_explicit_and_never_echoes_credentials(self):
        for error in (RuntimeError(TOKEN), KugouError("https://example/?token=" + TOKEN), KugouAuthError(TOKEN)):
            with self.subTest(kind=type(error).__name__):
                store, client, _factory = self.store(ReadOnlyClient(error=error))
                preview_id = store.start(SESSION)["preview_id"]
                page = self.wait_status(store, preview_id, "error")
                self.assertTrue(page["error"])
                self.assertEqual(page["playlists"], [])
                self.assertNotIn(TOKEN, json.dumps(page))
                self.assertIsNone(store._records[preview_id].session)
                self.assertTrue(client.closed.wait(1))
                with self.assertRaises(KugouPreviewError):
                    store.consume(preview_id, USER_ID)

    def test_capacity_evicts_old_previews_without_spawning_unbounded_workers(self):
        release = threading.Event()
        store, client, factory = self.store(ReadOnlyClient(release=release, cooperative=False), max_previews=2)
        original_id = store.start(SESSION)["preview_id"]
        self.assertTrue(client.entered.wait(1))
        worker = store._worker
        new_ids = [store.start(SESSION)["preview_id"] for _ in range(20)]
        self.assertEqual(len(store._records), 2)
        self.assertIs(store._worker, worker)
        self.assertEqual(factory.call_count, 1, "blocked read does not create additional threads")
        with self.assertRaises(KugouPreviewError):
            store.get(original_id)
        release.set()
        self.wait_status(store, new_ids[-1])
        self.assertEqual(set(store._records), set(new_ids[-2:]))

    def test_invalid_or_oversized_data_is_an_error_not_partial_success(self):
        cases = [
            [playlist(url="https://www.kugou.com/yy/special/single/" + "1" * 1100 + ".html")],
            [playlist(url="https://www.kugou.com.evil.example/yy/special/single/1.html")],
            [playlist(count=1_000_000_001)],
            [playlist(count=True)],
            [playlist(), playlist()],
            [playlist()] * 10001,
        ]
        for rows in cases:
            with self.subTest(rows=len(rows)):
                store, _client, _factory = self.store(ReadOnlyClient(rows))
                preview_id = store.start(SESSION)["preview_id"]
                page = self.wait_status(store, preview_id, "error")
                self.assertEqual(page["playlist_count"], 0)
                self.assertEqual(page["playlists"], [])
                self.assertTrue(page["error"])

    def test_real_empty_library_is_ready_and_can_be_confirmed(self):
        store, _client, _factory = self.store(ReadOnlyClient([]))
        preview_id = store.start(SESSION)["preview_id"]
        page = self.wait_status(store, preview_id)
        self.assertEqual((page["playlist_count"], page["known_track_count"], page["unknown_track_count"]), (0, 0, 0))
        self.assertEqual(page["error"], "")
        self.assertEqual(store.consume(preview_id, USER_ID), ())

    def test_pagination_arguments_and_missing_preview_are_safe_errors(self):
        store, _client, _factory = self.store()
        preview_id = store.start(SESSION)["preview_id"]
        self.wait_status(store, preview_id)
        for options in ({"offset": -1}, {"offset": 10001}, {"offset": True}, {"offset": "0"},
                        {"limit": 0}, {"limit": 1.0}, {"limit": False}):
            with self.subTest(options=options), self.assertRaises(KugouPreviewError):
                store.get(preview_id, **options)
        for invalid_id in (None, [], "missing"):
            with self.subTest(identifier=type(invalid_id).__name__), self.assertRaises(KugouPreviewError):
                store.get(invalid_id)

    def test_close_cancels_pending_work_and_prevents_new_reads(self):
        release = threading.Event()
        store, client, _factory = self.store(ReadOnlyClient(release=release))
        preview_id = store.start(SESSION)["preview_id"]
        self.assertTrue(client.entered.wait(1))
        store.close()
        self.assertEqual(store.get(preview_id)["status"], "cancelled")
        with self.assertRaises(KugouPreviewError):
            store.start(SESSION)
        self.assertTrue(client.closed.wait(1))
        store._worker.join(1)
        self.assertFalse(store._worker.is_alive())


if __name__ == "__main__":
    unittest.main()
