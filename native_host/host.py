"""Entry point for YT AutoDownload Queue native host."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback

from album_support import install_album_support
from host_legacy import CancelledError, HostLogger, NativeWriter, QueueManager, read_message

install_album_support(QueueManager, CancelledError)


def main() -> int:
    writer = NativeWriter()
    manager = QueueManager(writer)
    writer.send({"event": "hello", "host": manager.host_info(), "queue": manager.snapshot()})
    while True:
        try:
            message = read_message()
            if message is None:
                return 0
            request_id = message.get("id")
            action = message.get("action")
            try:
                response = {"ok": True, "reply_to": request_id}
                if action in {"hello", "list"}:
                    response.update({"host": manager.host_info(), "queue": manager.snapshot()})
                elif action == "enqueue":
                    manager.enqueue(
                        message.get("url", ""),
                        message.get("mode", "video"),
                        message.get("title", ""),
                        message.get("cookies") or [],
                    )
                    response["queue"] = manager.snapshot()
                elif action == "repair_channel":
                    response["repair"] = manager.repair_channel(
                        message.get("url", ""), message.get("cookies") or [])
                elif action == "cancel":
                    manager.cancel(message.get("task_id", ""))
                    response["queue"] = manager.snapshot()
                elif action == "remove":
                    manager.remove(message.get("task_id", ""))
                    response["queue"] = manager.snapshot()
                elif action == "clear_finished":
                    manager.clear_finished()
                    response["queue"] = manager.snapshot()
                elif action == "open_folder":
                    mode = "audio" if message.get("mode") == "audio" else "video"
                    path = manager.download_root / mode
                    path.mkdir(parents=True, exist_ok=True)
                    if sys.platform == "win32":
                        os.startfile(path)  # type: ignore[attr-defined]
                    else:
                        subprocess.Popen(["xdg-open", str(path)])
                else:
                    raise ValueError(f"未知操作：{action}")
                writer.send(response)
            except Exception as exc:
                HostLogger().error(f"request {action}: {exc}\n{traceback.format_exc(limit=8)}")
                writer.send({"ok": False, "reply_to": request_id, "error": str(exc)})
        except Exception as exc:
            try:
                writer.send({"event": "host_error", "error": str(exc), "trace": traceback.format_exc(limit=3)})
            except Exception:
                return 2


if __name__ == "__main__":
    raise SystemExit(main())
