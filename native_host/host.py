"""Entry point for YT AutoDownload Queue native host."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import traceback
from pathlib import Path

from album_support import install_album_support
from archive_location import install_archive_location
from channel_enhancements import install_channel_enhancements
from host_legacy import CancelledError, HostLogger, NativeWriter, QueueManager, read_message
from runtime_enhancements import install_runtime_enhancements, install_version_info
from youtube_runtime import runtime_diagnostics, extract_with_fallback, youtube_options

# Base downloader/retry first; channel album logic wraps it afterwards.
install_runtime_enhancements(QueueManager, CancelledError)
install_channel_enhancements()
install_album_support(QueueManager, CancelledError)
# Preserve v0.1.2 archive migration semantics for existing installations.
install_archive_location(QueueManager)
install_version_info(QueueManager)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--diagnostics":
        print(json.dumps(runtime_diagnostics(QueueManager._find_node()), ensure_ascii=False))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--probe-url":
        if len(sys.argv) < 3:
            print("Usage: yt_autodownload_host.exe --probe-url <youtube-url>", file=sys.stderr)
            return 2
        # A public diagnostic must not access user credentials.
        # It extracts metadata only: no download or postprocessing.
        node_path = QueueManager._find_node()
        options = {
            "quiet": True, "no_warnings": False, "skip_download": True,
            "noplaylist": True, "verbose": True, "logger": HostLogger(),
        }
        options.update(youtube_options(node_path, Path(os.devnull)))
        try:
            info = extract_with_fallback(options, sys.argv[2])
            print(json.dumps({
                "ok": True,
                "id": info.get("id"),
                "format_count": len(info.get("formats") or []),
                **runtime_diagnostics(node_path),
            }, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(json.dumps({
                "ok": False,
                "error": str(exc),
                **runtime_diagnostics(node_path),
            }, ensure_ascii=False), file=sys.stderr)
            return 1
    HostLogger().info(
        "HOST_START " +
        json.dumps(runtime_diagnostics(QueueManager._find_node()), ensure_ascii=False)
    )
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
                elif action == "retry":
                    manager.retry(message.get("task_id", ""), message.get("cookies"))
                    response["queue"] = manager.snapshot()
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
