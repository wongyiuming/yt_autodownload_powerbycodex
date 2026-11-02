"""Real Native Messaging frames for the read-preview-confirm RPC boundary.

Host imports and monkey patches run in a subprocess. Only the network preview
client and download worker are replaced; the real main loop, queue manager,
state persistence, authentication parsing and preview store remain in use.
"""

import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest


NATIVE = Path(__file__).resolve().parents[1] / "native_host"
USER_ID = "12345"
TOKEN = "rpc-private-fixture-token-never-in-output"
FRESH_TOKEN = "rpc-refreshed-fixture-token-never-in-output"

WRAPPER = r'''
import json, os, sys, time
from pathlib import Path
native, fixture = Path(sys.argv[1]), Path(sys.argv[2])
os.environ["LOCALAPPDATA"] = str(fixture)
sys.path.insert(0, str(native))
sys.argv = [str(native / "host.py")]
import host
import host_legacy
from kugou import KugouError
from kugou_library import KugouPlaylist
from kugou_preview import KugouPreviewStore

def audit(event):
    with (fixture / "audit.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event}) + "\n")

class FixtureClient:
    def __init__(self, session):
        self.session = session
    def list_playlists(self, *, check_cancelled):
        audit("list_playlists")
        if self.session.user_id == "98765":
            raise KugouError("https://example.test/?token=" + self.session.token)
        deadline = time.monotonic() + 20
        while not (fixture / "release-preview").exists():
            check_cancelled()
            if time.monotonic() > deadline:
                raise RuntimeError("fixture release timeout")
            time.sleep(0.01)
        check_cancelled()
        return [
            KugouPlaylist("collection_3_12345_11_0", "预览中已确认的歌单", 3,
                          "https://www.kugou.com/yy/special/single/collection_3_12345_11_0.html", "11", 0),
            KugouPlaylist("22", "Private without public URL", None, "", "22", 0),
        ]
    def resolve_playlist(self, *args, **kwargs):
        audit("resolve_playlist")
        raise AssertionError("Preview must not resolve songs")
    def resolve_track(self, *args, **kwargs):
        audit("resolve_track")
        raise AssertionError("Preview must not request media")
    def download(self, *args, **kwargs):
        audit("download")
        raise AssertionError("Preview must not download")
    def close(self):
        audit("close")

host.QueueManager._worker_loop = lambda self: None
host.QueueManager._find_node = staticmethod(lambda: None)
host_legacy.imageio_ffmpeg.get_ffmpeg_exe = lambda: "fixture-ffmpeg"
host.KugouPreviewStore = lambda: KugouPreviewStore(FixtureClient)
raise SystemExit(host.main())
'''


def cookies(user_id=USER_ID, token=TOKEN):
    return [{"domain": ".kugou.com", "name": "KuGoo", "session": True,
             "value": f"KugooID={user_id}&t={token}&a_id=1014"}]


class NativeConfirmationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stderr_path = self.root / "stderr.txt"
        self.stderr = self.stderr_path.open("wb")
        self.messages = queue.Queue()
        self.received = []
        self.sequence = 0
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", WRAPPER, str(NATIVE), str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            env=env,
        )
        self.reader = threading.Thread(target=self.read_frames, daemon=True)
        self.reader.start()
        self.addCleanup(self.stop_host)
        hello = self.next_message(timeout=15)
        self.assertEqual(hello.get("event"), "hello")
        self.assertEqual(hello["queue"], [])

    def read_frames(self):
        def exact(length):
            result = b""
            while len(result) < length:
                chunk = self.process.stdout.read(length - len(result))
                if not chunk:
                    if not result:
                        return None
                    raise AssertionError("truncated native response")
                result += chunk
            return result

        try:
            while True:
                header = exact(4)
                if header is None:
                    return
                length = struct.unpack("<I", header)[0]
                if not 0 < length <= 1024 * 1024:
                    raise AssertionError("invalid or excessive native response length")
                body = exact(length)
                if body is None:
                    raise AssertionError("native response payload missing")
                self.messages.put(json.loads(body.decode("utf-8")))
        except Exception as error:
            self.messages.put(error)

    def stop_host(self):
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(2)
        if self.process.stdout:
            self.process.stdout.close()
        self.stderr.close()

    def next_message(self, timeout=3):
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty:
            self.fail(f"Native Messaging timed out; exit code {self.process.poll()}")
        if isinstance(message, Exception):
            raise message
        self.received.append(message)
        return message

    def rpc(self, action, *, timeout=3, **payload):
        self.sequence += 1
        identifier = f"test-{self.sequence}"
        wire = json.dumps({"id": identifier, "action": action, **payload}, ensure_ascii=False).encode("utf-8")
        self.process.stdin.write(struct.pack("<I", len(wire)) + wire)
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.next_message(max(0.01, deadline - time.monotonic()))
            if message.get("reply_to") == identifier:
                return message
        self.fail(f"No native response to {action}")

    def wait_status(self, preview_id, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.rpc("kugou_preview_get", preview_id=preview_id)
            self.assertTrue(response["ok"], response)
            preview = response["preview"]
            if preview["status"] == status:
                return preview
            self.assertEqual(preview["status"], "loading", preview)
            time.sleep(0.01)
        self.fail(f"Preview did not become {status}")

    def assert_no_secret_or_media_work(self):
        text = json.dumps(self.received, ensure_ascii=False)
        for path in (self.root / "YTAutodownload" / "host.log", self.root / "YTAutodownload" / "state.json",
                     self.stderr_path):
            if path.exists():
                text += path.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(FRESH_TOKEN, text)
        audit = self.root / "audit.jsonl"
        events = [json.loads(line)["event"] for line in audit.read_text(encoding="utf-8").splitlines()] if audit.exists() else []
        self.assertTrue(set(events) <= {"list_playlists", "close"}, events)
        return events

    def test_preview_is_read_only_responsive_and_confirmed_exactly_once(self):
        raw = self.rpc("enqueue", url="https://www.kugou.com/", mode="audio",
                       cookies=cookies(), use_cookies=True)
        self.assertFalse(raw["ok"])
        self.assertIn("先读取", raw["error"])
        started = self.rpc("kugou_preview_start", cookies=cookies())
        self.assertTrue(started["ok"])
        preview_id = started["preview"]["preview_id"]
        self.assertEqual(started["preview"]["status"], "loading")
        # The fixture intentionally blocks its API call until released. These
        # real RPC replies demonstrate the native loop remains responsive.
        for action in ("hello", "list"):
            response = self.rpc(action, timeout=2)
            self.assertTrue(response["ok"])
            self.assertEqual(response["queue"], [])
        early = self.rpc("kugou_preview_confirm", preview_id=preview_id, cookies=cookies())
        self.assertFalse(early["ok"])
        self.assertEqual(self.rpc("list")["queue"], [])
        (self.root / "release-preview").touch()
        ready = self.wait_status(preview_id, "ready")
        self.assertEqual(ready["playlist_count"], 2)
        self.assertEqual(self.rpc("list")["queue"], [], "ready preview is still read-only")
        wrong = self.rpc("kugou_preview_confirm", preview_id=preview_id, cookies=cookies("67890"))
        self.assertFalse(wrong["ok"])
        self.assertIn("账号不同", wrong["error"])
        self.assertEqual(self.rpc("kugou_preview_get", preview_id=preview_id)["preview"]["status"], "ready")
        confirmed = self.rpc("kugou_preview_confirm", preview_id=preview_id,
                             cookies=cookies(token=FRESH_TOKEN), kugou_playlists=[{"id": "999"}])
        self.assertTrue(confirmed["ok"], confirmed)
        self.assertEqual(len(confirmed["queue"]), 1)
        task_id = confirmed["task_id"]
        self.assertTrue(confirmed["queue"][0]["kugou_confirmed"])
        self.assertEqual(confirmed["queue"][0]["kugou_user_id"], USER_ID)
        self.assertEqual(confirmed["queue"][0]["playlist_count"], 2)
        self.assertEqual(confirmed["queue"][0]["status"], "queued")
        state = json.loads((self.root / "YTAutodownload" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["kugou_plans"][task_id], ready["playlists"], "confirmation uses the displayed plan, not message data")
        repeated = self.rpc("kugou_preview_confirm", preview_id=preview_id, cookies=cookies(token=FRESH_TOKEN))
        self.assertFalse(repeated["ok"])
        self.assertEqual(len(self.rpc("list")["queue"]), 1)
        self.assertNotIn('"playlists"', json.dumps(confirmed["queue"]))
        events = self.assert_no_secret_or_media_work()
        self.assertEqual(events.count("list_playlists"), 1)

    def test_cancel_rpc_prevents_late_confirmation_without_creating_tasks(self):
        started = self.rpc("kugou_preview_start", cookies=cookies())
        preview_id = started["preview"]["preview_id"]
        cancelled = self.rpc("kugou_preview_cancel", preview_id=preview_id)
        self.assertTrue(cancelled["ok"])
        self.assertEqual(cancelled["preview"]["status"], "cancelled")
        (self.root / "release-preview").touch()
        rejected = self.rpc("kugou_preview_confirm", preview_id=preview_id, cookies=cookies())
        self.assertFalse(rejected["ok"])
        self.assertEqual(self.rpc("kugou_preview_get", preview_id=preview_id)["preview"]["status"], "cancelled")
        self.assertEqual(self.rpc("list")["queue"], [])
        self.assert_no_secret_or_media_work()

    def test_bad_auth_and_private_api_errors_never_expose_credentials(self):
        missing = self.rpc("kugou_preview_start", cookies=[])
        self.assertFalse(missing["ok"])
        invalid = self.rpc("kugou_preview_start", cookies=cookies(token=TOKEN + "\n"))
        self.assertFalse(invalid["ok"])
        started = self.rpc("kugou_preview_start", cookies=cookies("98765"))
        self.assertTrue(started["ok"])
        failed = self.wait_status(started["preview"]["preview_id"], "error")
        self.assertTrue(failed["error"])
        self.assertEqual(failed["playlists"], [])
        self.assertEqual(self.rpc("list")["queue"], [])
        rejected = self.rpc("kugou_preview_confirm", preview_id=failed["preview_id"], cookies=cookies("98765"))
        self.assertFalse(rejected["ok"])
        self.assert_no_secret_or_media_work()


if __name__ == "__main__":
    unittest.main()
