"""Confirmed-library scope, account isolation and on-disk playlist regressions."""

from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))

import host_legacy
import kugou_queue
import runtime_enhancements
from kugou import KugouCollection, KugouError, KugouMedia, KugouTrack
from kugou_library import KugouAuthError, KugouLibraryClient, KugouPlaylist


LIBRARY = "https://www.kugou.com/"
USER_ID = "12345"
TOKEN = "private-fixture-token-must-not-be-persisted"
HASH_A, HASH_B, HASH_C = "A" * 32, "B" * 32, "C" * 32


def cookies(user_id=USER_ID, token=TOKEN):
    return [{"domain": ".kugou.com", "name": "KuGoo", "session": True,
             "value": f"KugooID={user_id}&t={token}&a_id=1014"}]


def track(song_hash, title):
    return KugouTrack(song_hash, hash=song_hash, title=title)


class PersonalLibraryQueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in {
            "APP_DIR": self.root / "state", "STATE_PATH": self.root / "state" / "state.json",
            "LOG_PATH": self.root / "state" / "host.log",
            "COOKIE_PATH": self.root / "state" / "youtube-cookies.txt",
            "BILIBILI_COOKIE_PATH": self.root / "state" / "bilibili-cookies.txt",
        }.items():
            change = patch.object(host_legacy, name, value)
            change.start()
            self.addCleanup(change.stop)
        runtime_enhancements.install_runtime_enhancements(host_legacy.QueueManager, host_legacy.CancelledError)
        self.m = self.manager()

    def manager(self):
        manager = host_legacy.QueueManager.__new__(host_legacy.QueueManager)
        manager.tasks, manager.cancelled, manager._kugou_sessions = [], set(), {}
        manager._kugou_plans = {}
        manager.lock = threading.RLock()
        manager.wake, manager.writer, manager.emit = Mock(), Mock(), Mock()
        manager.download_root = self.root / "media"
        manager.node_path, manager.ffmpeg_path = "node", "ffmpeg"
        manager._last_emit = 0.0
        return manager

    def enqueue(self, user_id=USER_ID, token=TOKEN, *, playlists=()):
        return self.m.enqueue(LIBRARY, "audio", "我的歌单", cookies(user_id, token), True,
                              kugou_playlists=playlists)

    @staticmethod
    def plan(collections):
        return tuple(KugouPlaylist(collection.id, collection.title, len(collection.tracks),
                                   list_id=collection.id) for collection in collections)

    @property
    def audio_root(self):
        return self.m.download_root / "audio" / "kugou"

    def playlist_folder(self, collection):
        return self.audio_root / f"用户 {USER_ID}" / f"{collection.title} [{collection.id}]"

    def client(self, collections, *, playlist_errors=None, track_errors=None):
        playlist_errors = playlist_errors or {}
        track_errors = track_errors or {}
        by_id = {collection.id: collection for collection in collections}
        client = Mock(spec=KugouLibraryClient)
        client.list_playlists.side_effect = AssertionError("Confirmed downloads must never re-enumerate the account")

        def resolve_playlist(playlist, **kwargs):
            kwargs["check_cancelled"]()
            if playlist.id in playlist_errors:
                raise playlist_errors[playlist.id]
            return by_id[playlist.id]

        def resolve_track(item, **kwargs):
            kwargs["check_cancelled"]()
            if item.id in track_errors:
                raise track_errors[item.id]
            return KugouMedia(f"https://media.example/{item.id}.mp3", item.title)

        def download(media, destination, **kwargs):
            kwargs["check_cancelled"]()
            destination.write_bytes(b"ID3" + media.title.encode())
            kwargs["progress"]({"downloaded_bytes": destination.stat().st_size,
                                "total_bytes": destination.stat().st_size})
            return destination

        client.resolve_playlist.side_effect = resolve_playlist
        client.resolve_track.side_effect = resolve_track
        client.download.side_effect = download
        return client

    def run_download(self, task, client):
        try:
            return kugou_queue.download_kugou(self.m, task, host_legacy.CancelledError, client=client)
        finally:
            client.list_playlists.assert_not_called()

    def test_snapshot_and_persisted_state_have_account_but_no_credentials(self):
        plan = (KugouPlaylist("11", "Only in the saved plan", 7,
                              "https://www.kugou.com/yy/special/single/11.html", "11", 0),)
        task = self.enqueue(playlists=plan)
        self.assertEqual(task.kugou_user_id, USER_ID)
        self.assertTrue(task.use_cookies)
        self.assertTrue(task.kugou_confirmed)
        self.assertEqual(task.playlist_count, 1)
        self.assertEqual(self.m._kugou_sessions[task.id].token, TOKEN)
        self.m._save()
        state = host_legacy.STATE_PATH.read_text(encoding="utf-8")
        snapshot = json.dumps(self.m.snapshot())
        self.assertEqual(json.loads(state)["kugou_plans"][task.id], [asdict(plan[0])])
        self.assertNotIn("Only in the saved plan", snapshot)
        self.assertNotIn("special/single/11.html", snapshot)
        self.assertNotIn('"kugou_plans"', snapshot)
        self.assertNotIn('"playlists"', snapshot)
        for serialized in (state, snapshot):
            self.assertIn(USER_ID, serialized)
            self.assertNotIn(TOKEN, serialized)
            self.assertNotIn("KuGoo", serialized)
            self.assertNotIn('"token"', serialized)
            self.assertNotIn('"cookies"', serialized)
        self.assertFalse(host_legacy.COOKIE_PATH.exists())
        self.assertFalse(host_legacy.BILIBILI_COOKIE_PATH.exists())

    def test_restore_blocks_unfinished_library_and_preserves_unrelated_tasks(self):
        queued_plan = (KugouPlaylist("11", "Previously confirmed", 2, "", "11", 0),)
        running_plan = (KugouPlaylist("22", "Another plan", None, "", "22", 1),)
        queued = self.enqueue(playlists=queued_plan)
        running = self.enqueue(playlists=running_plan)
        running.status = "downloading"
        done = self.enqueue()
        done.status = "done"
        public = self.m.enqueue("https://www.youtube.com/watch?v=abc123", "audio", "", [], False)
        public.status = "downloading"
        self.m._save()
        restored = self.manager()
        restored._load()
        tasks = {item.id: item for item in restored.tasks}
        self.assertEqual(tasks[queued.id].status, "error")
        self.assertEqual(tasks[running.id].status, "error")
        self.assertIn("重试", tasks[running.id].error)
        self.assertEqual(tasks[running.id].kugou_user_id, USER_ID)
        self.assertEqual(tasks[done.id].status, "done")
        self.assertEqual(tasks[public.id].status, "queued")
        self.assertEqual(restored._kugou_sessions, {})
        self.assertEqual(restored._kugou_plans[queued.id], queued_plan)
        self.assertEqual(restored._kugou_plans[running.id], running_plan)
        self.assertEqual(restored._kugou_plans[done.id], ())
        self.assertTrue(tasks[queued.id].kugou_confirmed)

    def test_raw_library_enqueue_is_rejected_until_preview_is_confirmed(self):
        with self.assertRaisesRegex(ValueError, "先读取歌单 URL"):
            self.m.enqueue(LIBRARY, "audio", "", cookies(), True)
        self.assertEqual(self.m.tasks, [])
        self.assertEqual(self.m._kugou_sessions, {})
        self.assertEqual(self.m._kugou_plans, {})

    def test_missing_login_fails_before_adding_task(self):
        for supplied, enabled in (([], True), (cookies(), False)):
            with self.subTest(enabled=enabled), self.assertRaises(KugouAuthError):
                self.m.enqueue(LIBRARY, "audio", "", supplied, enabled, kugou_playlists=())
        self.assertEqual(self.m.tasks, [])
        self.assertEqual(self.m._kugou_sessions, {})

    def test_failed_retry_keeps_original_task_and_session_unchanged(self):
        task = self.enqueue()
        task.status, task.error, task.progress = "error", "prior failure", 55
        original = asdict(task)
        session = self.m._kugou_sessions[task.id]
        for supplied, enabled in ((cookies("67890"), True), ([], True), (cookies(), False)):
            with self.subTest(enabled=enabled, present=bool(supplied)), self.assertRaises(KugouAuthError):
                self.m.retry(task.id, supplied, enabled)
            self.assertEqual(asdict(task), original)
            self.assertIs(self.m._kugou_sessions[task.id], session)

    def test_retry_requires_bound_account_and_refreshes_matching_session(self):
        plan = (KugouPlaylist("11", "Confirmed", 2, "", "11", 0),)
        task = self.enqueue(playlists=plan)
        task.status = "error"
        task.kugou_user_id = ""
        with self.assertRaisesRegex(KugouAuthError, "账号标识"):
            self.m.retry(task.id, cookies(), True)
        task.kugou_user_id = USER_ID
        task.playlist_index, task.playlist_count, task.item_count = 2, 3, 10
        self.m._kugou_sessions.clear()
        self.m.retry(task.id, cookies(token="refreshed-fixture-token"), True)
        self.assertEqual(task.status, "queued")
        self.assertEqual(task.kugou_user_id, USER_ID)
        self.assertEqual(self.m._kugou_sessions[task.id].token, "refreshed-fixture-token")
        self.assertEqual((task.playlist_index, task.playlist_count, task.item_count), (0, 1, 0))
        self.assertEqual(self.m._kugou_plans[task.id], plan)

    def test_missing_plan_or_confirmation_cannot_be_retried_or_downloaded(self):
        for missing_plan in (False, True):
            with self.subTest(missing_plan=missing_plan):
                task = self.enqueue()
                task.status = "error"
                self.m._kugou_sessions.pop(task.id)
                if missing_plan:
                    self.m._kugou_plans.pop(task.id)
                else:
                    task.kugou_confirmed = False
                before = asdict(task)
                with self.assertRaisesRegex(ValueError, "已确认的歌单集合"):
                    self.m.retry(task.id, cookies(), True)
                self.assertEqual(asdict(task), before)
                self.assertNotIn(task.id, self.m._kugou_sessions)
                client = self.client([])
                with self.assertRaisesRegex(ValueError, "已确认的歌单集合"):
                    self.run_download(task, client)
                client.resolve_playlist.assert_not_called()

    def test_restored_missing_or_invalid_plan_does_not_enable_unconfirmed_downloads(self):
        task = self.enqueue(playlists=(KugouPlaylist("11", "Confirmed", 2, "", "11", 0),))
        public = self.m.enqueue("https://www.youtube.com/watch?v=abc123", "audio", "", [], False)
        self.m._save()
        original = json.loads(host_legacy.STATE_PATH.read_text(encoding="utf-8"))
        for invalid in (None, [{"id": "11", "list_id": "", "title": "Invalid"}]):
            with self.subTest(invalid=bool(invalid)):
                state = json.loads(json.dumps(original))
                if invalid is None:
                    state["kugou_plans"].pop(task.id)
                else:
                    state["kugou_plans"][task.id] = invalid
                host_legacy.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
                restored = self.manager()
                restored._load()
                tasks = {item.id: item for item in restored.tasks}
                self.assertFalse(tasks[task.id].kugou_confirmed)
                self.assertEqual(tasks[task.id].status, "error")
                self.assertIn("确认", tasks[task.id].error)
                self.assertNotIn(task.id, restored._kugou_plans)
                self.assertEqual(tasks[public.id].status, "queued")

    def test_retry_cannot_change_running_task_credentials(self):
        task = self.enqueue()
        task.status = "downloading"
        original = self.m._kugou_sessions[task.id]
        with self.assertRaisesRegex(ValueError, "只能重试"):
            self.m.retry(task.id, cookies(token="new-token"), True)
        self.assertIs(self.m._kugou_sessions[task.id], original)

    def test_cancel_keeps_plan_for_retry_but_remove_and_clear_drop_it(self):
        cancelled, removed, cleared, pending = [self.enqueue() for _ in range(4)]
        self.m.cancel(cancelled.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertNotIn(cancelled.id, self.m._kugou_sessions)
        self.assertIn(cancelled.id, self.m._kugou_plans)
        self.assertEqual(self.m._kugou_plans[cancelled.id], ())
        removed.status = "error"
        self.m.remove(removed.id)
        self.assertNotIn(removed.id, self.m._kugou_sessions)
        self.assertNotIn(removed.id, self.m._kugou_plans)
        cleared.status = "done"
        self.m.clear_finished()
        self.assertEqual([item.id for item in self.m.tasks], [pending.id])
        self.assertEqual(set(self.m._kugou_sessions), {pending.id})
        self.assertEqual(set(self.m._kugou_plans), {pending.id})

    def test_worker_clears_sessions_and_does_not_auto_retry_auth_errors(self):
        for error, expected in ((None, "done"), (KugouAuthError("登录失效"), "error"),
                                (host_legacy.CancelledError("取消"), "cancelled")):
            with self.subTest(expected=expected):
                self.m = self.manager()
                task = self.enqueue()
                self.m._download = Mock(side_effect=error)
                self.m.wake.wait.side_effect = StopIteration("test worker exit")
                with self.assertRaises(StopIteration):
                    self.m._worker_loop()
                self.assertEqual(task.status, expected)
                self.assertEqual(self.m._kugou_sessions, {})
                self.assertIn(task.id, self.m._kugou_plans, "terminal state keeps the confirmed plan for retry/history")
                self.m._download.assert_called_once_with(task)

    def test_missing_session_fails_before_any_library_request(self):
        task = self.enqueue()
        self.m._kugou_sessions.clear()
        client = self.client([])
        with self.assertRaises(KugouAuthError):
            self.run_download(task, client)
        client.list_playlists.assert_not_called()

    def test_shared_song_completes_both_playlist_folders_with_one_transfer(self):
        shared = track(HASH_A, "Shared")
        collections = [KugouCollection("11", "First", [shared]),
                       KugouCollection("22", "Second", [shared, track(HASH_B, "Unique")])]
        client, task = self.client(collections), self.enqueue(playlists=self.plan(collections))
        self.run_download(task, client)
        self.assertEqual(client.download.call_count, 2)
        self.assertEqual(client.resolve_track.call_count, 2)
        self.assertEqual(len(list(self.playlist_folder(collections[0]).glob("*.mp3"))), 1)
        self.assertEqual(len(list(self.playlist_folder(collections[1]).glob("*.mp3"))), 2)
        shared_files = [file for file in self.audio_root.rglob("*.mp3") if HASH_A in file.name]
        self.assertEqual(len(shared_files), 2)
        self.assertTrue(all(file.read_bytes() == b"ID3Shared" for file in shared_files))
        self.assertEqual((task.item_count, task.playlist_count, task.progress), (3, 2, 100))
        self.run_download(task, client)
        self.assertEqual(client.download.call_count, 2, "retry reuses completed media")
        client.close.assert_not_called()

    def test_hardlink_failure_falls_back_to_copy_for_each_playlist(self):
        shared = track(HASH_A, "Shared")
        collections = [KugouCollection("11", "First", [shared]), KugouCollection("22", "Second", [shared])]
        client, task = self.client(collections), self.enqueue(playlists=self.plan(collections))
        with patch.object(kugou_queue.os, "link", side_effect=OSError("unsupported filesystem")) as link:
            self.run_download(task, client)
        self.assertGreaterEqual(link.call_count, 2)
        self.assertEqual(client.download.call_count, 1)
        files = list(self.audio_root.rglob("*.mp3"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(file.read_bytes() == b"ID3Shared" for file in files))
        self.assertEqual(list(self.audio_root.rglob(".kugou-*")), [])

    def test_conflicting_playlist_file_is_preserved_and_task_fails(self):
        shared = track(HASH_A, "Shared")
        collections = [KugouCollection("11", "First", [shared]), KugouCollection("22", "Second", [shared])]
        target = self.playlist_folder(collections[1]) / f"001 - Shared [{HASH_A}].mp3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"user-file-must-not-be-overwritten")
        client, task = self.client(collections), self.enqueue(playlists=self.plan(collections))
        with self.assertRaisesRegex(RuntimeError, "1/2 首失败"):
            self.run_download(task, client)
        self.assertEqual(target.read_bytes(), b"user-file-must-not-be-overwritten")
        self.assertEqual(client.download.call_count, 1)
        archived = json.loads((self.audio_root / "archive-audio.json").read_text(encoding="utf-8"))
        self.assertEqual((self.audio_root / archived[HASH_A]).read_bytes(), b"ID3Shared")

    def test_empty_library_succeeds_without_media_requests(self):
        task, client = self.enqueue(), self.client([])
        self.run_download(task, client)
        self.assertEqual((task.item_count, task.playlist_count, task.progress), (0, 0, 100))
        self.assertTrue((self.audio_root / f"用户 {USER_ID}").is_dir())
        client.resolve_track.assert_not_called()
        self.assertFalse((self.audio_root / "archive-audio.json").exists())

    def test_empty_playlist_keeps_its_folder(self):
        collection = KugouCollection("11", "Empty", [])
        task, client = self.enqueue(playlists=self.plan([collection])), self.client([collection])
        self.run_download(task, client)
        self.assertTrue(self.playlist_folder(collection).is_dir())
        self.assertEqual((task.item_count, task.playlist_count, task.progress), (0, 1, 100))
        client.download.assert_not_called()

    def test_playlist_and_track_failures_do_not_skip_remaining_music(self):
        collections = [KugouCollection("11", "Unavailable playlist", []),
                       KugouCollection("22", "Mixed", [track(HASH_A, "Unavailable song"), track(HASH_B, "Available")]),
                       KugouCollection("33", "Later", [track(HASH_C, "Last")])]
        client = self.client(collections, playlist_errors={"11": KugouError("playlist denied")},
                             track_errors={HASH_A: KugouError("song denied")})
        task = self.enqueue(playlists=self.plan(collections))
        with self.assertRaisesRegex(RuntimeError, "1/3 张歌单读取失败.*1/3 首失败"):
            self.run_download(task, client)
        self.assertEqual(client.resolve_playlist.call_count, 3)
        self.assertEqual(client.resolve_track.call_count, 3)
        self.assertEqual(client.download.call_count, 2)
        archive = json.loads((self.audio_root / "archive-audio.json").read_text(encoding="utf-8"))
        self.assertEqual(set(archive), {HASH_B, HASH_C})
        self.assertLess(task.progress, 100)

    def test_cancellation_between_playlists_stops_before_media_transfer(self):
        collections = [KugouCollection("11", "First", [track(HASH_A, "A")]),
                       KugouCollection("22", "Second", [track(HASH_B, "B")])]
        task, client = self.enqueue(playlists=self.plan(collections)), self.client(collections)

        def cancel_after_first(playlist, **kwargs):
            self.m.cancelled.add(task.id)
            return collections[0]

        client.resolve_playlist.side_effect = cancel_after_first
        with self.assertRaises(host_legacy.CancelledError):
            self.run_download(task, client)
        self.assertEqual(client.resolve_playlist.call_count, 1)
        client.download.assert_not_called()

    def test_download_and_restored_retry_never_expand_into_unpreviewed_playlists(self):
        confirmed = KugouCollection("11", "Confirmed", [track(HASH_A, "Approved song")])
        added_later = KugouCollection("22", "Added after confirmation", [track(HASH_B, "Unapproved song")])
        plan = self.plan([confirmed])
        task = self.enqueue(playlists=plan)
        client = self.client([confirmed, added_later])
        self.run_download(task, client)
        self.assertEqual([call.args[0].id for call in client.resolve_playlist.call_args_list], ["11"])
        task.status = "error"
        self.m._save()
        restored = self.manager()
        restored._load()
        self.m = restored
        task = restored._task(task.id)
        self.assertEqual(self.m._kugou_plans[task.id], plan)
        self.m.retry(task.id, cookies(token="fresh-token"), True)
        self.run_download(task, client)
        self.assertEqual([call.args[0].id for call in client.resolve_playlist.call_args_list], ["11", "11"])
        client.list_playlists.assert_not_called()
        self.assertEqual(client.download.call_count, 1)
        self.assertEqual((task.playlist_count, task.item_count), (1, 1))
        self.assertFalse(self.playlist_folder(added_later).exists())
        archive = json.loads((self.audio_root / "archive-audio.json").read_text(encoding="utf-8"))
        self.assertEqual(set(archive), {HASH_A})


if __name__ == "__main__":
    unittest.main()
