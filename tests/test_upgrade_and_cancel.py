"""Offline regressions for cancellation and the upgrade queue-start barrier."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))
import host
import host_legacy
from upgrade_guard import wait_for_upgrade


class UpgradeAndCancelTests(unittest.TestCase):
    def manager(self):
        m = host.QueueManager.__new__(host.QueueManager)
        m.tasks = [host_legacy.Task("active", "https://www.youtube.com/watch?v=test", "video", "video")]
        m.lock = threading.RLock()
        m.cancelled = set()
        m.emit = Mock()
        return m

    def test_queued_cancel_never_becomes_runnable(self):
        m = self.manager()
        m.cancel("active")
        self.assertEqual(m.tasks[0].status, "cancelled")
        self.assertFalse(m.cancelled)
        m.emit.assert_called_once_with(force=True)

    def test_active_cancel_reaches_worker_and_is_not_retried(self):
        m = self.manager()
        attempts = []

        class StopWorker(Exception):
            pass

        def download(task):
            attempts.append(task.id)
            self.assertEqual(task.status, "downloading")
            m.cancel(task.id)
            self.assertIn(task.id, m.cancelled)
            raise host.CancelledError("cancelled by progress hook")

        def emit(**kwargs):
            if m.tasks[0].status == "cancelled":
                raise StopWorker()

        m._download = download
        m.emit.side_effect = emit
        with self.assertRaises(StopWorker):
            m._worker_loop()
        self.assertEqual(attempts, ["active"])
        self.assertEqual(m.tasks[0].status, "cancelled")
        self.assertFalse(m.cancelled)

    def test_marker_waits_until_cleanup_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "upgrade-in-progress"
            marker.touch()

            def complete_cleanup(_delay):
                self.assertTrue(marker.exists())
                marker.unlink()

            with patch("upgrade_guard.time.sleep", side_effect=complete_cleanup) as sleep:
                wait_for_upgrade(root)
            sleep.assert_called_once()

    def test_failed_or_interrupted_upgrade_never_constructs_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "upgrade-in-progress"
            marker.write_text("installer was interrupted")
            with patch.object(host, "APP_DIR", root), patch.object(host, "QueueManager") as manager, \
                 patch.object(sys, "argv", ["host"]), \
                 patch("upgrade_guard.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    host.main()
                manager.assert_not_called()
            self.assertTrue(marker.exists())

    def test_diagnostics_can_recover_install_while_marker_exists(self):
        with patch.object(sys, "argv", ["host", "--diagnostics"]), \
             patch.object(host, "wait_for_upgrade") as gate, \
             patch.object(host, "runtime_diagnostics", return_value={}), \
             patch("builtins.print"):
            self.assertEqual(host.main(), 0)
            gate.assert_not_called()

    def test_interrupted_task_restores_only_after_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            m = self.manager()
            m.tasks[0].status = "downloading"
            from dataclasses import asdict
            state.write_text(json.dumps({"tasks": [asdict(m.tasks[0])]}))
            marker = root / "upgrade-in-progress"
            marker.touch()
            with patch("upgrade_guard.time.sleep", side_effect=lambda _: marker.unlink()):
                wait_for_upgrade(root)
            m.tasks = []
            with patch.object(host_legacy, "APP_DIR", root), patch.object(host_legacy, "STATE_PATH", state):
                m._load()
            self.assertEqual(m.tasks[0].status, "queued")


if __name__ == "__main__":
    unittest.main()
