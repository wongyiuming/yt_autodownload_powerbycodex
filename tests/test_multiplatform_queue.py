"""Cross-platform queue regressions, including real persisted download records."""

import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))

import host_legacy
import runtime_enhancements


YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
BILIBILI = "https://space.bilibili.com/123/favlist?fid=456"
KUGOU = "https://www.kugou.com/yy/special/single/123.html"
YT_COOKIE = {"domain": ".youtube.com", "name": "SID", "value": "youtube-example"}
BILI_COOKIE = {"domain": ".bilibili.com", "name": "SESSDATA", "value": "bili-example"}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.m = host_legacy.QueueManager.__new__(host_legacy.QueueManager)
        self.m.tasks, self.m.cancelled = [], set()
        self.m.lock = threading.RLock()
        self.m.wake = Mock()
        self.m.emit = Mock()
        self.m.download_root = self.root / "media"
        self.m.node_path, self.m.ffmpeg_path = "node", "ffmpeg"
        runtime_enhancements.install_runtime_enhancements(host_legacy.QueueManager, host_legacy.CancelledError)
        for name, value in {"COOKIE_PATH": self.root / "youtube-cookies.txt",
                            "BILIBILI_COOKIE_PATH": self.root / "bilibili-cookies.txt",
                            "APP_DIR": self.root, "STATE_PATH": self.root / "state.json"}.items():
            p = patch.object(host_legacy, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_bilibili_cookie_is_separate_and_cannot_accept_youtube_cookie(self):
        youtube = self.m.enqueue(YOUTUBE, "audio", "", [YT_COOKIE], True)
        bili = self.m.enqueue(BILIBILI, "video", "", [YT_COOKIE, BILI_COOKIE], True)
        self.assertTrue(youtube.use_cookies)
        self.assertTrue(bili.use_cookies)
        self.assertEqual(bili.platform, "bilibili")
        self.assertEqual(bili.kind, "favorites")
        self.assertIn("youtube-example", host_legacy.COOKIE_PATH.read_text())
        self.assertNotIn("bili-example", host_legacy.COOKIE_PATH.read_text())
        self.assertIn("bili-example", host_legacy.BILIBILI_COOKIE_PATH.read_text())
        self.assertNotIn("youtube-example", host_legacy.BILIBILI_COOKIE_PATH.read_text())
        task = self.m.enqueue(BILIBILI, "audio", "", [YT_COOKIE], True)
        self.assertFalse(task.use_cookies)

    def test_kugou_rejects_video_and_never_saves_cookies(self):
        with self.assertRaisesRegex(ValueError, "仅支持音频"):
            self.m.enqueue(KUGOU, "video", "", [], False)
        task = self.m.enqueue(KUGOU, "audio", "", [YT_COOKIE, BILI_COOKIE], True)
        self.assertFalse(task.use_cookies)
        self.assertFalse(host_legacy.COOKIE_PATH.exists())
        self.assertFalse(host_legacy.BILIBILI_COOKIE_PATH.exists())

    def test_retry_does_not_mutate_credentials_of_running_task(self):
        task = self.m.enqueue(BILIBILI, "audio", "", [], False)
        task.status = "downloading"
        with self.assertRaisesRegex(ValueError, "只能重试"):
            self.m.retry(task.id, [BILI_COOKIE], True)
        self.assertFalse(task.use_cookies)
        self.assertFalse(host_legacy.BILIBILI_COOKIE_PATH.exists())

    def test_retry_uses_url_and_keeps_other_platform_cookie(self):
        self.m.enqueue(YOUTUBE, "audio", "", [YT_COOKIE], True)
        task = self.m.enqueue(BILIBILI, "audio", "", [], False)
        task.status = "error"
        task.platform = "youtube"
        self.m.retry(task.id, [YT_COOKIE, BILI_COOKIE], True)
        self.assertEqual(task.platform, "bilibili")
        self.assertTrue(task.use_cookies)
        self.assertEqual(task.status, "queued")
        self.assertIn("youtube-example", host_legacy.COOKIE_PATH.read_text())
        self.assertIn("bili-example", host_legacy.BILIBILI_COOKIE_PATH.read_text())

    def test_bilibili_uses_yt_dlp_without_youtube_fallback_and_has_own_archive(self):
        task = self.m.enqueue(BILIBILI, "audio", "", [BILI_COOKIE], True)
        fake = Mock()
        fake.download.return_value = 0
        with patch.object(host_legacy.yt_dlp, "YoutubeDL") as constructor, \
             patch.object(runtime_enhancements, "download_with_fallback") as youtube, \
             patch.object(runtime_enhancements, "runtime_diagnostics", return_value={"yt_dlp_version": "test"}):
            constructor.return_value.__enter__.return_value = fake
            self.m._download(task)
        youtube.assert_not_called()
        fake.download.assert_called_once_with(["https://www.bilibili.com/medialist/detail/ml456"])
        options = constructor.call_args.args[0]
        self.assertEqual(options["cookiefile"], str(host_legacy.BILIBILI_COOKIE_PATH))
        self.assertFalse(options["noplaylist"])
        self.assertNotIn("js_runtimes", options)
        self.assertNotIn("remote_components", options)
        self.assertNotIn("extractor_args", options)
        self.assertEqual(Path(options["download_archive"]).parent, self.m.download_root / "audio" / "bilibili")
        self.assertEqual(options["postprocessors"][0]["preferredcodec"], "mp3")

    def test_anon_bilibili_ignores_cached_cookie_files(self):
        host_legacy.COOKIE_PATH.write_text("youtube")
        host_legacy.BILIBILI_COOKIE_PATH.write_text("bilibili")
        task = self.m.enqueue(BILIBILI, "video", "", [], False)
        with patch.object(host_legacy.yt_dlp, "YoutubeDL") as constructor, \
             patch.object(runtime_enhancements, "runtime_diagnostics", return_value={"yt_dlp_version": "test"}):
            constructor.return_value.__enter__.return_value.download.return_value = 0
            self.m._download(task)
        options = constructor.call_args.args[0]
        self.assertNotIn("cookiefile", options)
        self.assertEqual(options["merge_output_format"], "mp4")

    def test_youtube_still_uses_existing_fallback_and_archive(self):
        task = self.m.enqueue(YOUTUBE, "audio", "", [YT_COOKIE], True)
        with patch.object(runtime_enhancements, "download_with_fallback") as youtube, \
             patch.object(runtime_enhancements, "runtime_diagnostics", return_value={"yt_dlp_version": "test"}):
            self.m._download(task)
        options, url = youtube.call_args.args
        self.assertEqual(url, YOUTUBE)
        self.assertEqual(options["cookiefile"], str(host_legacy.COOKIE_PATH))
        self.assertEqual(Path(options["download_archive"]).parent, self.m.download_root / "audio")

    def test_restore_old_state_and_new_platform_tasks(self):
        host_legacy.STATE_PATH.write_text(json.dumps({"tasks": [
            {"id": "old", "url": YOUTUBE, "kind": "video", "mode": "audio", "status": "done"},
            {"id": "new", "url": BILIBILI, "kind": "favorites", "mode": "video", "status": "downloading"},
        ]}))
        self.m._load()
        self.assertEqual([(t.platform, t.status) for t in self.m.tasks], [("youtube", "done"), ("bilibili", "queued")])

    def test_one_obsolete_url_does_not_erase_the_restored_queue(self):
        host_legacy.STATE_PATH.write_text(json.dumps({"tasks": [
            {"id": "valid", "url": YOUTUBE, "kind": "video", "mode": "audio", "status": "queued"},
            {"id": "obsolete", "url": "https://www.youtube.com/watch?v=a/b", "kind": "video", "mode": "audio", "status": "error"},
            {"id": "next", "url": BILIBILI, "kind": "favorites", "mode": "audio", "status": "queued"},
        ]}))
        self.m._load()
        self.assertEqual([t.status for t in self.m.tasks], ["queued", "error", "queued"])
        self.assertIn("历史任务", self.m.tasks[1].error)


class KugouQueueTests(unittest.TestCase):
    setUp = QueueTests.setUp

    def client(self, tracks=2):
        client = Mock()
        client.resolve_collection.return_value = SimpleNamespace(
            id="123", title="Fixture", tracks=[SimpleNamespace(id=f"hash{i}", title=f"Song {i}") for i in range(tracks)])
        client.resolve_track.side_effect = lambda track, **kw: SimpleNamespace(title=track.title, ext="wav")
        client.download.side_effect = lambda media, path, **kw: path.write_bytes(b"fixture")
        return client

    def convert(self, ffmpeg, source, target, check):
        check()
        target.write_bytes(b"ID3fixture-result")

    def test_http_queue_archives_only_completed_files_and_redownloads_missing_file(self):
        import kugou_queue
        task = self.m.enqueue(KUGOU, "audio", "", [], False)
        client = self.client()
        with patch.object(kugou_queue, "convert_to_mp3", side_effect=self.convert), \
             patch.object(host_legacy.yt_dlp, "YoutubeDL", side_effect=AssertionError("Kugou must not use yt-dlp")):
            kugou_queue.download_kugou(self.m, task, host_legacy.CancelledError, client=client)
            self.assertEqual(client.download.call_count, 2)
            kugou_queue.download_kugou(self.m, task, host_legacy.CancelledError, client=client)
            self.assertEqual(client.download.call_count, 2, "existing MP3s are not downloaded twice")
            root = self.m.download_root / "audio" / "kugou"
            archive = json.loads((root / "archive-audio.json").read_text())
            (root / archive["hash0"]).unlink()
            kugou_queue.download_kugou(self.m, task, host_legacy.CancelledError, client=client)
            self.assertEqual(client.download.call_count, 3, "a removed output is eligible again")
        self.assertEqual(task.item_count, 2)
        self.assertEqual(task.progress, 100)

    def test_failed_track_does_not_block_later_tracks_or_enter_archive(self):
        import kugou_queue
        task = self.m.enqueue(KUGOU, "audio", "", [], False)
        client = self.client()
        client.resolve_track.side_effect = [RuntimeError("requires permission"), SimpleNamespace(title="Song 1", ext="wav")]
        with patch.object(kugou_queue, "convert_to_mp3", side_effect=self.convert):
            with self.assertRaisesRegex(RuntimeError, "1/2 首失败"):
                kugou_queue.download_kugou(self.m, task, host_legacy.CancelledError, client=client)
            archive = json.loads((self.m.download_root / "audio" / "kugou" / "archive-audio.json").read_text())
            self.assertEqual(set(archive), {"hash1"})
            client.resolve_track.side_effect = lambda track, **kw: SimpleNamespace(title=track.title, ext="wav")
            kugou_queue.download_kugou(self.m, task, host_legacy.CancelledError, client=client)
            self.assertEqual(client.download.call_count, 2, "retry processes only the previously failed track")

    def test_cancel_during_conversion_cleans_work_files_and_does_not_archive(self):
        import kugou_queue
        task = self.m.enqueue(KUGOU, "audio", "", [], False)
        with patch.object(kugou_queue, "convert_to_mp3", side_effect=host_legacy.CancelledError("cancel")):
            with self.assertRaises(host_legacy.CancelledError):
                kugou_queue.download_kugou(self.m, task, host_legacy.CancelledError, client=self.client())
        root = self.m.download_root / "audio" / "kugou"
        self.assertFalse((root / "archive-audio.json").exists())
        self.assertEqual(list(root.rglob("*.mp3")), [])
        self.assertEqual(list(root.rglob(".kugou-*")), [])

    def test_mp3_is_saved_unchanged_without_ffmpeg(self):
        import kugou_queue
        task = self.m.enqueue(KUGOU, "audio", "", [], False)
        client = self.client(tracks=1)
        client.resolve_track.side_effect = None
        client.resolve_track.return_value = SimpleNamespace(title="Original MP3", ext="mp3")
        original = b"ID3-original-audio-bytes"
        client.download.side_effect = lambda media, path, **kw: path.write_bytes(original)
        with patch.object(kugou_queue, "convert_to_mp3", side_effect=AssertionError("MP3 must not be re-encoded")):
            kugou_queue.download_kugou(self.m, task, host_legacy.CancelledError, client=client)
        files = list(self.m.download_root.rglob("*.mp3"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), original)

    def test_kugou_dispatch_does_not_use_youtube_or_yt_dlp(self):
        import kugou_queue
        task = self.m.enqueue(KUGOU, "audio", "", [], False)
        with patch.object(kugou_queue, "download_kugou") as download, \
             patch.object(runtime_enhancements, "download_with_fallback") as youtube, \
             patch.object(host_legacy.yt_dlp, "YoutubeDL") as ydl:
            self.m._download(task)
        download.assert_called_once_with(self.m, task, host_legacy.CancelledError)
        youtube.assert_not_called()
        ydl.assert_not_called()

    def test_cancelling_conversion_reaps_ffmpeg(self):
        import kugou_queue
        process = Mock()
        process.poll.return_value = None
        check = Mock(side_effect=[None, host_legacy.CancelledError("cancel")])
        with patch.object(kugou_queue.subprocess, "Popen", return_value=process):
            with self.assertRaises(host_legacy.CancelledError):
                kugou_queue.convert_to_mp3("ffmpeg", self.root / "source.wav", self.root / "out.mp3", check)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)
        self.assertNotIn(process, kugou_queue._active_processes)


if __name__ == "__main__":
    unittest.main()
