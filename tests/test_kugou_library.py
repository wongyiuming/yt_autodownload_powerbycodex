"""Session isolation and complete-pagination tests for Kugou's H5 library."""

import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
import unittest
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))

from kugou import KugouError, KugouTrack
from kugou_library import (
    KugouAuthError, KugouLibraryClient, KugouPlaylist, KugouSession, session_from_cookies,
)


TOKEN = "test-session-never-written-to-status"
HASH_A, HASH_B, HASH_C = "A" * 32, "B" * 32, "C" * 32


def cookie(name, value, **extra):
    return {"name": name, "value": value, "domain": ".kugou.com", "path": "/", **extra}


def playlist_row(list_id, count=1, **extra):
    return {"listid": list_id, "global_collection_id": f"collection_3_123_{list_id}_0",
            "name": f"List {list_id}", "m_count": count, "list_ver": 1, **extra}


def track_row(song_hash, **extra):
    return {"hash": song_hash, "filename": "Artist - Song", "album_id": 9,
            "timelen": 180000, **extra}


def page(rows, count, **extra):
    return {"status": 1, "data": {"info": rows, "count": count, **extra}}


class Cancelled(Exception):
    pass


class TestKugouSession(unittest.TestCase):
    def test_extracts_plain_and_encoded_compound_web_cookie(self):
        raw = f"KugooID=123&t={TOKEN}&a_id=1014&NickName=%u9F9F"
        for value in (raw, quote(raw, safe="")):
            with self.subTest(encoded=value != raw):
                session = session_from_cookies([
                    cookie("KuGoo", value), cookie("kg_mid", "device-mid"), cookie("kg_dfid", "device-dfid"),
                    cookie("unrelated", "not retained"),
                ])
                self.assertEqual(session.user_id, "123")
                self.assertEqual(session.token, TOKEN)
                self.assertEqual(session.mid, "device-mid")
                self.assertEqual(session.dfid, "device-dfid")
                self.assertNotIn(TOKEN, repr(session))
                self.assertNotIn("not retained", repr(session))

    def test_no_login_expired_or_wrong_domain_requires_login(self):
        value = f"KugooID=123&t={TOKEN}&a_id=1014"
        cases = [[], [cookie("KuGoo", value, domain="kugou.com.evil.example")],
                 [cookie("KuGoo", value, domain="fanxing.kugou.com")],
                 [cookie("KuGoo", value, expirationDate=time.time() - 5)],
                 [cookie("KuGoo", "KugooID=123&a_id=1014")],
                 [cookie("KuGoo", f"KugooID=123&t={TOKEN}&a_id=1005")]]
        for cookies in cases:
            with self.subTest(cookies=len(cookies)), self.assertRaises(KugouAuthError) as error:
                session_from_cookies(cookies)
            self.assertNotIn(TOKEN, str(error.exception))

    def test_conflicting_sessions_are_not_combined(self):
        with self.assertRaisesRegex(KugouAuthError, "冲突"):
            session_from_cookies([
                cookie("KuGoo", "KugooID=123&t=first&a_id=1014"),
                cookie("KuGoo", "KugooID=456&t=second&a_id=1014", domain="www.kugou.com"),
            ])

    def test_invalid_session_values_are_rejected_without_echo(self):
        for user_id, token in (("0", TOKEN), ("../123", TOKEN), ("123", ""), ("123", "secret\r\nheader")):
            with self.subTest(user_id=user_id), self.assertRaises(KugouAuthError) as error:
                KugouSession(user_id, token)
            self.assertNotIn(TOKEN, str(error.exception))


class TestKugouLibrary(unittest.TestCase):
    def client(self, handler, **http_options):
        http = httpx.Client(transport=httpx.MockTransport(handler), **http_options)
        self.addCleanup(http.close)
        client = KugouLibraryClient(KugouSession("123", TOKEN, "web-mid", "web-dfid"), http)
        client.page_size = 2
        return client

    def test_all_playlist_pages_are_signed_and_builtins_retained(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(str(request.url).split("?", 1)[0], "https://gateway.kugou.com/v7/get_all_list")
            self.assertEqual(request.headers["x-router"], "cloudlist.service.kugou.com")
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("unrelated", request.url.params)
            params = dict(request.url.params)
            signature = params.pop("signature")
            material = "".join(f"{key}={params[key]}" for key in sorted(params)) + request.content.decode()
            seed = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
            self.assertEqual(signature, hashlib.md5((seed + material + seed).encode()).hexdigest())
            body = json.loads(request.content)
            self.assertEqual(body["userid"], 123)
            self.assertEqual(body["token"], TOKEN)
            self.assertEqual(body["type"], 2)
            self.assertEqual(body["total_ver"], 0)
            self.assertEqual(params["appid"], "1014")
            self.assertEqual(params["mid"], "web-mid")
            number = body["page"]
            rows = ([playlist_row(1, name="我喜欢", is_def=2), playlist_row(2, name="默认收藏", is_def=1)]
                    if number == 1 else [playlist_row(3, name="Created &amp; collected")])
            return httpx.Response(200, json=page(rows, 3, page=number, total_ver=1001))

        client = self.client(handler, cookies={"KuGoo": "must-not-be-forwarded"},
                             headers={"Authorization": "must-not-be-forwarded"}, params={"unrelated": "secret"},
                             auth=("unused", "unused"))
        result = client.list_playlists()
        self.assertEqual([p.title for p in result], ["我喜欢", "默认收藏", "Created & collected"])
        self.assertEqual([p.list_id for p in result], ["1", "2", "3"])
        self.assertEqual(result[0].url, "https://www.kugou.com/yy/special/single/collection_3_123_1_0.html")
        self.assertEqual(len(requests), 2)

    def test_songs_are_paged_and_song_hash_duplicates_do_not_truncate_pages(self):
        seen = []
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(request.url.path, "/v4/get_list_all_file")
            self.assertEqual(body["listid"], 7)
            self.assertEqual(body["userid"], 123)
            self.assertEqual(body["allplatform"], 1)
            seen.append(body["page"])
            rows = ([track_row(HASH_A, fileid=1), track_row(HASH_B, fileid=2)]
                    if body["page"] == 1 else [track_row(HASH_B, fileid=3), track_row(HASH_C, fileid=4)])
            return httpx.Response(200, json=page(rows, 4, page=body["page"]))
        collection = self.client(handler).resolve_playlist(KugouPlaylist("collection_3_123_7_0", "All songs", 4))
        self.assertEqual([track.id for track in collection.tracks], [HASH_A, HASH_B, HASH_C])
        self.assertEqual(collection.tracks[0].duration, 180)
        self.assertEqual(collection.tracks[0].url, f"https://www.kugou.com/song/#hash={HASH_A}&album_id=9")
        self.assertEqual(seen, [1, 2])

    def test_public_playback_on_library_client_does_not_send_session(self):
        def handler(request):
            self.assertEqual(request.url.host, "m.kugou.com")
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("token", request.url.params)
            self.assertNotIn(TOKEN, str(request.url))
            return httpx.Response(200, json={"status": 1, "url": "https://fs.kugou.com/song.mp3",
                                            "extName": "mp3", "timeLength": 180})
        media = self.client(handler).resolve_track(KugouTrack(HASH_A, hash=HASH_A))
        self.assertEqual(media.ext, "mp3")

    def test_collected_list_uses_original_public_source_without_session(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.path == "/v7/get_all_list":
                return httpx.Response(200, json=page([playlist_row(3, type=1,
                    global_collection_id="collection_3_999_11_0",
                    list_create_gid="collection_3_777_8_0")], 1))
            self.assertNotIn(TOKEN, str(request.url))
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("token", request.url.params)
            if request.url.host == "www.kugou.com":
                self.assertEqual(request.url.path, "/yy/special/single/collection_3_777_8_0.html")
                return httpx.Response(200, text='var specialInfo={"id":999,"name":"Original"};')
            self.assertEqual(request.url.host, "gatewayretry.kugou.com")
            self.assertEqual(request.url.params["userid"], "0")
            self.assertEqual(request.url.params["specialid"], "999")
            return httpx.Response(200, json=page([track_row(HASH_A)], 1))
        client = self.client(handler)
        playlist = client.list_playlists()[0]
        self.assertEqual(playlist.list_id, "3")
        self.assertEqual(playlist.list_type, 1)
        collection = client.resolve_playlist(playlist)
        self.assertEqual([t.hash for t in collection.tracks], [HASH_A])
        self.assertFalse(any(request.url.path == "/v4/get_list_all_file" for request in calls))

    def test_missing_hash_is_retained_and_later_valid_songs_are_available(self):
        rows = [{"filename": "Removed song", "fileid": 0}, track_row(HASH_A, fileid=2),
                {"filename": "Cloud-only file", "fileid": 0}]
        client = self.client(lambda _: httpx.Response(200, json=page(rows, 3)))
        result = client.resolve_playlist(KugouPlaylist("7", "Mixed", 3))
        self.assertEqual([t.id for t in result.tracks], ["unavailable:1", HASH_A, "unavailable:3"])
        self.assertEqual(result.tracks[0].title, "Removed song")
        self.assertEqual(result.tracks[1].hash, HASH_A)
        with self.assertRaisesRegex(KugouError, "缺少有效"):
            client.resolve_track(result.tracks[0])

    def test_empty_library_is_a_valid_complete_result(self):
        self.assertEqual(self.client(lambda _: httpx.Response(200, json=page([], 0))).list_playlists(), [])

    def test_float_more_flag_cannot_silently_truncate_library(self):
        for value in (1.0, 0.0):
            with self.subTest(value=value):
                client = self.client(lambda _: httpx.Response(200, json={
                    "status": 1, "data": {"info": [playlist_row(1)], "has_more": value},
                }))
                with self.assertRaisesRegex(KugouError, "结束标记无效"):
                    client.list_playlists()

    def test_albums_are_not_misreported_as_personal_playlists(self):
        result = self.client(lambda _: httpx.Response(200, json=page([
            playlist_row(1), playlist_row(2, list_ver=2),
        ], 2))).list_playlists()
        self.assertEqual([p.list_id for p in result], ["1"])

    def test_explicit_more_flag_supports_response_without_total(self):
        calls = []
        def handler(request):
            number = json.loads(request.content)["page"]
            calls.append(number)
            return httpx.Response(200, json={"status": 1, "data": {
                "info": [playlist_row(number)], "page": number, "has_more": int(number < 3),
            }})
        self.assertEqual(len(self.client(handler).list_playlists()), 3)
        self.assertEqual(calls, [1, 2, 3])

    def test_without_total_short_pages_continue_until_an_empty_page(self):
        calls = []
        def handler(request):
            number = json.loads(request.content)["page"]
            calls.append(number)
            rows = [playlist_row(number)] if number < 3 else []
            # Terminal empty pages may omit the revision sent on data pages.
            data = {"info": rows, **({"total_ver": 42} if rows else {})}
            return httpx.Response(200, json={"status": 1, "data": data})
        self.assertEqual(len(self.client(handler).list_playlists()), 2)
        self.assertEqual(calls, [1, 2, 3])

    def test_song_list_count_without_current_total_requires_an_empty_page(self):
        calls = []
        def handler(request):
            number = json.loads(request.content)["page"]
            calls.append(number)
            rows = [track_row(HASH_A)] if number == 1 else []
            return httpx.Response(200, json={"status": 1, "data": {"info": rows}})
        collection = self.client(handler).resolve_playlist(KugouPlaylist("1", "Song", 1))
        self.assertEqual(len(collection.tracks), 1)
        self.assertEqual(calls, [1, 2])

    def test_repeated_page_and_overlap_are_errors(self):
        repeated = self.client(lambda _: httpx.Response(200, json=page([playlist_row(1)], 3)))
        with self.assertRaisesRegex(KugouError, "重复返回"):
            repeated.list_playlists()
        def overlap(request):
            number = json.loads(request.content)["page"]
            return httpx.Response(200, json=page([playlist_row(number), playlist_row(number + 1)], 4))
        with self.assertRaisesRegex(KugouError, "重复的歌单"):
            self.client(overlap).list_playlists()

    def test_early_end_wrong_page_overrun_and_conflicting_markers_fail(self):
        cases = [
            page([], 3), page([playlist_row(1)], 1, page=2),
            page([playlist_row(1), playlist_row(2)], 1),
            page([playlist_row(1)], 3, has_more=0),
            page([playlist_row(1)], 1, has_more=1),
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(KugouError):
                self.client(lambda _, payload=payload: httpx.Response(200, json=payload)).list_playlists()

    def test_mutating_count_or_version_requires_retry(self):
        for field in ("count", "total_ver"):
            def handler(request):
                number = json.loads(request.content)["page"]
                data = {"info": [playlist_row(number)], "count": 3, "total_ver": 20}
                data[field] = (3 if field == "count" else 20) + number - 1
                return httpx.Response(200, json={"status": 1, "data": data})
            with self.subTest(field=field), self.assertRaisesRegex(KugouError, "读取期间发生变化"):
                self.client(handler).list_playlists()

    def test_song_count_changed_since_library_enumeration_is_not_silent(self):
        client = self.client(lambda _: httpx.Response(200, json=page([track_row(HASH_A)], 1)))
        with self.assertRaisesRegex(KugouError, "读取期间发生变化"):
            client.resolve_playlist(KugouPlaylist("1", "Changed", 2))

    def test_duplicate_file_id_cannot_hide_a_missing_song(self):
        def handler(request):
            number = json.loads(request.content)["page"]
            return httpx.Response(200, json=page([track_row(HASH_A if number == 1 else HASH_B, fileid=1)], 2))
        with self.assertRaisesRegex(KugouError, "重复歌曲记录"):
            self.client(handler).resolve_playlist(KugouPlaylist("1", "Overlap", 2))

    def test_pagination_safety_cap_reports_incomplete(self):
        client = self.client(lambda _: httpx.Response(200, json=page([playlist_row(1)], 3)))
        client.max_pages = 1
        with self.assertRaisesRegex(KugouError, "安全上限"):
            client.list_playlists()

    def test_cancellation_stops_before_second_request(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=page([playlist_row(1)], 2))
        def check():
            if calls:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            self.client(handler).list_playlists(check_cancelled=check)
        self.assertEqual(len(calls), 1)

    def test_redirects_never_receive_a_second_authenticated_request(self):
        for target in ("https://attacker.example/", "https://gateway.kugou.com/other"):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(307, headers={"Location": target})
            with self.subTest(target=target), self.assertRaisesRegex(KugouError, "重定向"):
                self.client(handler, follow_redirects=True).list_playlists()
            self.assertEqual(len(calls), 1)

    def test_auth_failure_is_distinct_and_never_echoes_body(self):
        for response in (httpx.Response(401, text=TOKEN), httpx.Response(200, json={
                "status": 0, "error_code": 20010, "errmsg": TOKEN})):
            with self.assertRaises(KugouAuthError) as caught:
                self.client(lambda _: response).list_playlists()
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_http_exception_traceback_does_not_include_token_url(self):
        def handler(request):
            raise httpx.ConnectError(f"Could not connect: {request.url}", request=request)
        try:
            self.client(handler).list_playlists()
        except KugouError:
            trace = traceback.format_exc()
        else:
            self.fail("Expected sanitized connection error")
        self.assertNotIn(TOKEN, trace)
        self.assertNotIn("signature=", trace)

    def test_non_json_and_service_errors_do_not_echo_credentials(self):
        for response in (httpx.Response(200, text=TOKEN), httpx.Response(500, text=TOKEN),
                         httpx.Response(200, json={"status": 0, "error_code": 900, "errmsg": TOKEN})):
            with self.assertRaises(KugouError) as caught:
                self.client(lambda _: response).list_playlists()
            self.assertNotIn(TOKEN, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
