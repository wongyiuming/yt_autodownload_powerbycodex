"""First-download authenticated single-song regressions; all data is synthetic."""
import json
from pathlib import Path
import sys
import tempfile
import shutil
import subprocess
import threading
import traceback
import unittest
from unittest.mock import Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native_host"))
from kugou import KugouClient, KugouError, KugouTrack
from kugou_library import KugouAuthError, KugouSession
from kugou_playback import KugouSongClient

TOKEN = "fixture-only-token+&=secret"
SESSION = KugouSession("123", TOKEN, "fixture-mid", "fixture-dfid")
HASH = "A" * 32
TRACK = KugouTrack(HASH, hash=HASH, album_id="456", mixsongid="789", title="Test song", duration=180)
URL = f"https://www.kugou.com/song/#hash={HASH}&album_id=456&album_audio_id=789"
COOKIES = [{"domain": "www.kugou.com", "name": key, "value": value}
           for key, value in (("userid", "123"), ("token", TOKEN), ("a_id", "1014"),
                              ("kg_mid", "fixture-mid"), ("kg_dfid", "fixture-dfid"))]


def playback(**changes):
    data = {"play_url": "https://fs.kugou.com/test.mp3", "extname": "mp3", "timelength": 180000,
            "filesize": 6, "audio_name": "Test song", **changes}
    return {"status": 1, "err_code": 0, "data": data}


class PlaybackTests(unittest.TestCase):
    def client(self, handler, **defaults):
        http = httpx.Client(transport=httpx.MockTransport(handler), **defaults)
        self.addCleanup(http.close)
        return KugouSongClient(SESSION, http)

    def test_authenticated_web_request_has_session_and_complete_song_identity(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url).split('?')[0], "https://wwwapi.kugou.com/yy/index.php")
            for key, value in {"r": "play/getdata", "hash": HASH, "album_id": "456",
                               "album_audio_id": "789", "userid": "123", "token": TOKEN,
                               "appid": "1014", "platid": "4", "mid": "fixture-mid", "dfid": "fixture-dfid"}.items():
                self.assertEqual(request.url.params[key], value)
            self.assertIn("KugooID=123&t=fixture-only-token%2B%26%3Dsecret&a_id=1014", request.headers["cookie"])
            self.assertNotIn("unrelated", request.headers["cookie"])
            return httpx.Response(200, json=playback())
        media = self.client(handler).resolve_track(TRACK)
        self.assertEqual(media.duration, 180)
        self.assertEqual(len(calls), 1)

    def test_media_and_metadata_do_not_inherit_credentials_even_after_set_cookie(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.host == "wwwapi.kugou.com":
                self.assertNotIn("injected", request.url.params)
                self.assertNotIn("authorization", request.headers)
                return httpx.Response(200, json=playback(), headers={"set-cookie": "KuGoo=secret; Domain=.kugou.com"})
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("injected", request.url.params)
            self.assertNotIn(TOKEN, str(request.url))
            return httpx.Response(200, content=b"abcdef", headers={"content-type": "audio/mpeg"})
        client = self.client(handler, cookies={"unrelated": TOKEN}, headers={"authorization": TOKEN},
                             params={"injected": TOKEN}, auth=("user", TOKEN))
        media = client.resolve_track(TRACK)
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)/"song.mp3"
            client.download(media, target)
            self.assertEqual(target.read_bytes(), b"abcdef")
        client._open("https://www.kugou.com/mixsong/test.html").close()
        self.assertEqual(len(calls), 3)

    def test_redirects_and_challenges_do_not_forward_or_fallback(self):
        for response in (httpx.Response(302, headers={"location": "https://evil.invalid/?token=" + TOKEN}),
                         httpx.Response(302, headers={"location": "https://wwwapi.kugou.com/else"}),
                         httpx.Response(200, headers={"ssa-code": TOKEN}, json=playback())):
            calls=[]
            def handler(request):
                calls.append(request)
                return response
            with self.assertRaises(KugouAuthError) as caught:
                self.client(handler).resolve_track(TRACK)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_auth_refusals_are_not_retried_anonymously(self):
        for response in (httpx.Response(401, text=TOKEN), httpx.Response(200, json={
                "status": 0, "err_code": 20010, "error": TOKEN})):
            calls=[]
            def handler(request):
                calls.append(request)
                return response
            with self.assertRaises(KugouAuthError):
                self.client(handler).resolve_track(TRACK)
            self.assertEqual(len(calls), 1)

    def test_business_failure_and_missing_url_never_echo_upstream_text(self):
        for payload in ({"status": 0, "err_code": 30020, "error": TOKEN},
                        playback(play_url="", error="需要付费 " + TOKEN)):
            with self.assertRaises(KugouError) as caught:
                self.client(lambda _: httpx.Response(200, json=payload)).resolve_track(TRACK)
            self.assertIn("网页登录态播放接口", str(caught.exception))
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertNotIn("需要付费", str(caught.exception))

    def test_transport_tracebacks_do_not_contain_token_or_cookie(self):
        def handler(request):
            raise httpx.ReadTimeout(str(request.url) + str(request.headers), request=request)
        try:
            self.client(handler).resolve_track(TRACK)
        except KugouError:
            trace = traceback.format_exc()
        self.assertNotIn(TOKEN, trace)
        self.assertNotIn("token=", trace)
        self.assertIn("超时", trace)

    def test_bad_json_schema_and_numeric_codes_are_safely_reported(self):
        for response in (httpx.Response(200, text=TOKEN), httpx.Response(200, json=[]),
                         httpx.Response(200, json={"status": 1, "err_code": TOKEN}),
                         httpx.Response(200, json={"status": 1, "data": None}),
                         httpx.Response(503, text=TOKEN)):
            with self.assertRaises(KugouError) as caught:
                self.client(lambda _: response).resolve_track(TRACK)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_wrong_response_account_and_token_in_media_url_are_rejected(self):
        for data in ({"userid": "456"}, {"play_url": "https://cdn.example/a.mp3?token=" + TOKEN}):
            with self.assertRaises(KugouError):
                self.client(lambda _: httpx.Response(200, json=playback(**data))).resolve_track(TRACK)

    def test_signed_cdn_token_is_not_confused_with_account_token(self):
        url = "https://fs.kugou.com/test.mp3?token=cdn-delivery-signature"
        media = self.client(lambda _: httpx.Response(200, json=playback(play_url=url))).resolve_track(TRACK)
        self.assertEqual(media.url, url)

    def test_trial_encrypted_and_non_http_media_still_rejected(self):
        for data in ({"is_free_part": 1}, {"timelength": 60000},
                     {"extname": "kgm"}, {"play_url": "file:///secret.mp3"}):
            with self.assertRaises(KugouError):
                self.client(lambda _: httpx.Response(200, json=playback(**data))).resolve_track(TRACK)

    def test_cancel_and_closed_session_cannot_request(self):
        client = self.client(lambda _: self.fail("unexpected request"))
        class Cancelled(Exception): pass
        def check(): raise Cancelled()
        with self.assertRaises(Cancelled):
            client.resolve_track(TRACK, check_cancelled=check)
        client.close()
        with self.assertRaises(KugouAuthError):
            client.resolve_track(TRACK)


class ExtensionSessionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for extension integration")
    def test_actual_background_and_popup_session_flow(self):
        subprocess.run(["node", str(ROOT / "tests/test_kugou_single_session.js")], cwd=ROOT,
                       check=True, capture_output=True, text=True, encoding="utf-8", timeout=15)


class SingleQueueTests(unittest.TestCase):
    def manager(self):
        import host_legacy as legacy
        from runtime_enhancements import install_runtime_enhancements
        install_runtime_enhancements(legacy.QueueManager, legacy.CancelledError)
        manager = legacy.QueueManager.__new__(legacy.QueueManager)
        manager.lock = threading.RLock()
        manager.wake = threading.Event()
        manager.cancelled = set()
        manager.tasks = []
        manager._kugou_sessions = {}
        manager._kugou_plans = {}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        manager.download_root = Path(tmp.name)/"downloads"
        manager.emit = Mock()
        manager.ffmpeg_path = "unused"
        manager._write_cookies = Mock(side_effect=AssertionError("Kugou credentials must not be written"))
        for name, value in {"APP_DIR": Path(tmp.name), "STATE_PATH": Path(tmp.name)/"state.json",
                            "LOG_PATH": Path(tmp.name)/"host.log"}.items():
            patcher=patch.object(legacy, name, value);patcher.start();self.addCleanup(patcher.stop)
        return manager

    def test_enqueue_keeps_session_in_memory_and_never_in_state(self):
        import host_legacy as legacy
        manager=self.manager()
        task=manager.enqueue(URL, "audio", "song", COOKIES, True)
        self.assertTrue(task.use_cookies)
        self.assertEqual(task.kugou_user_id, "123")
        self.assertFalse(task.kugou_confirmed)
        self.assertNotIn(task.id, manager._kugou_plans)
        self.assertEqual(manager._kugou_sessions[task.id].token, TOKEN)
        manager._save()
        self.assertNotIn(TOKEN, legacy.STATE_PATH.read_text())
        self.assertNotIn(TOKEN, json.dumps(manager.snapshot()))
        self.assertNotIn("token", legacy.STATE_PATH.read_text())
        manager._write_cookies.assert_not_called()

    def test_missing_session_fails_before_enqueue_and_opt_out_stays_anonymous(self):
        manager=self.manager()
        with self.assertRaises(KugouAuthError):
            manager.enqueue(URL,"audio","",[],True)
        self.assertEqual(manager.tasks,[])
        task=manager.enqueue(URL,"audio","",COOKIES,False)
        self.assertFalse(task.use_cookies)
        self.assertNotIn(task.id,manager._kugou_sessions)

    def test_first_download_uses_authenticated_client_without_archive(self):
        import host_legacy as legacy
        from kugou_queue import download_kugou
        manager=self.manager()
        task=manager.enqueue(URL,"audio","",COOKIES,True)
        calls=[]
        def handler(request):
            calls.append(request)
            if request.url.host == "wwwapi.kugou.com":
                self.assertEqual(request.url.params.get("token"),TOKEN)
                return httpx.Response(200,json=playback())
            self.assertEqual(request.url.host,"fs.kugou.com")
            self.assertNotIn("cookie",request.headers)
            return httpx.Response(200,content=b"abcdef",headers={"content-type":"audio/mpeg"})
        http=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(http.close)
        real=KugouSongClient(SESSION,http)
        with patch("kugou_playback.KugouSongClient",return_value=real) as factory:
            download_kugou(manager,task,legacy.CancelledError)
        factory.assert_called_once_with(manager._kugou_sessions[task.id])
        self.assertEqual([r.url.host for r in calls],["wwwapi.kugou.com","fs.kugou.com"])
        self.assertEqual(len(list(manager.download_root.rglob("*.mp3"))),1)
        archive=(manager.download_root/"audio/kugou/archive-audio.json").read_text()
        self.assertIn(HASH,archive);self.assertNotIn(TOKEN,archive)

    def test_retry_refreshes_same_account_and_rejects_switch_without_mutation(self):
        manager=self.manager();task=manager.enqueue(URL,"audio","",COOKIES,True);task.status="error"
        wrong=[{**c,"value":"456" if c["name"]=="userid" else c["value"]} for c in COOKIES]
        with self.assertRaises(KugouAuthError):manager.retry(task.id,wrong,True)
        self.assertEqual(task.status,"error");self.assertEqual(manager._kugou_sessions[task.id].token,TOKEN)
        fresh=[{**c,"value":"new-fixture-token" if c["name"]=="token" else c["value"]} for c in COOKIES]
        manager.retry(task.id,fresh,True)
        self.assertEqual(task.status,"queued");self.assertEqual(manager._kugou_sessions[task.id].token,"new-fixture-token")
        with self.assertRaises(ValueError):manager.retry(task.id,wrong,True)
        self.assertEqual(manager._kugou_sessions[task.id].token,"new-fixture-token")
        task.status="error";manager.retry(task.id,[],False)
        self.assertFalse(task.use_cookies);self.assertNotIn(task.id,manager._kugou_sessions)

    def test_old_anonymous_single_can_opt_in_on_retry(self):
        manager=self.manager();task=manager.enqueue(URL,"audio","",[],False);task.status="error"
        manager.retry(task.id,COOKIES,True)
        self.assertTrue(task.use_cookies);self.assertEqual(task.kugou_user_id,"123")

    def test_restore_blocks_unfinished_session_task_instead_of_anonymous_fallback(self):
        manager=self.manager();task=manager.enqueue(URL,"audio","",COOKIES,True);manager._save()
        manager.tasks=[];manager._kugou_sessions={};manager._load()
        self.assertEqual(manager.tasks[0].status,"error")
        self.assertTrue(manager.tasks[0].use_cookies)
        self.assertEqual(manager._kugou_sessions,{})

    def test_cancel_remove_and_clear_drop_session(self):
        manager=self.manager();task=manager.enqueue(URL,"audio","",COOKIES,True)
        manager.cancel(task.id);self.assertNotIn(task.id,manager._kugou_sessions)
        task=manager.enqueue(URL,"audio","",COOKIES,True);task.status="error"
        manager.remove(task.id);self.assertNotIn(task.id,manager._kugou_sessions)
        task=manager.enqueue(URL,"audio","",COOKIES,True);task.status="done"
        manager.clear_finished();self.assertEqual(manager._kugou_sessions,{})

    def test_missing_task_session_cannot_silently_download_anonymously(self):
        import host_legacy as legacy
        from kugou_queue import download_kugou
        manager=self.manager();task=manager.enqueue(URL,"audio","",COOKIES,True)
        manager._kugou_sessions.clear()
        with self.assertRaises(KugouAuthError):download_kugou(manager,task,legacy.CancelledError)

    def test_auth_error_survives_queue_for_nonretryable_worker_handling(self):
        import host_legacy as legacy
        from kugou_queue import download_kugou
        manager=self.manager();task=manager.enqueue(URL,"audio","",COOKIES,True)
        client=Mock();client.resolve_collection.return_value=type('Collection',(),{'id':HASH,'title':'Song','tracks':[TRACK]})()
        client.resolve_track.side_effect=KugouAuthError("session rejected")
        with self.assertRaises(KugouAuthError):download_kugou(manager,task,legacy.CancelledError,client=client)
        client.download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
