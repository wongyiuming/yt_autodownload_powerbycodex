"""Queue/auth persistence regression tests (no Chrome or network)."""

from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native_host"))

import host_legacy
import runtime_enhancements


class QueueCookieTests(unittest.TestCase):
    def setUp(self):
        self.m = host_legacy.QueueManager.__new__(host_legacy.QueueManager)
        self.m.tasks = []
        self.m.lock = threading.RLock()
        self.m.wake = Mock()
        self.m.emit = Mock()
        self.m._write_cookies = Mock()
        # Install the same retry method used by production.
        runtime_enhancements.install_runtime_enhancements(host_legacy.QueueManager, host_legacy.CancelledError)

    def test_public_enqueue_does_not_write_or_use_cookies(self):
        task = self.m.enqueue("https://www.youtube.com/watch?v=S0pt6PEApy0", "audio", "", [], False)
        self.assertFalse(task.use_cookies)
        self.m._write_cookies.assert_not_called()

    def test_cookie_opt_in_only_when_available(self):
        cookies = [{"domain": ".youtube.com", "name": "SID", "value": "example"}]
        task = self.m.enqueue("https://www.youtube.com/watch?v=S0pt6PEApy0", "audio", "", cookies, True)
        self.assertTrue(task.use_cookies)
        self.m._write_cookies.assert_called_once_with(cookies)

    def test_retry_changes_cookie_mode(self):
        task = self.m.enqueue("https://www.youtube.com/watch?v=S0pt6PEApy0", "audio", "", [], False)
        task.status = "error"
        cookies = [{"domain": ".youtube.com", "name": "SID", "value": "example"}]
        self.m.retry(task.id, cookies, True)
        self.assertTrue(task.use_cookies)
        self.assertEqual(task.status, "queued")
        self.m._write_cookies.assert_called_once_with(cookies)


if __name__ == "__main__":
    unittest.main()
