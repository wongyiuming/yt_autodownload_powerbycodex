"""Runtime queue fixes layered onto the original native host."""

from __future__ import annotations

import time

import yt_dlp

import host_legacy

AUTO_RETRY_COUNT = 2
AUTO_RETRY_DELAYS = (3, 8)


def install_runtime_enhancements(queue_manager_cls, cancelled_error_cls) -> None:
    """Install media-local archives, task-level retries, and manual retry."""
    if getattr(queue_manager_cls, "_runtime_enhancements_installed", False):
        return

    def retry(self, task_id: str) -> None:
        with self.lock:
            task = self._task(task_id)
            if task.status not in {"error", "cancelled"}:
                raise ValueError("只能重试失败或已取消的任务")
            self.cancelled.discard(task_id)
            task.status = "queued"
            task.progress = 0.0
            task.speed = ""
            task.eta = ""
            task.current_title = ""
            task.item_index = 0
            task.item_count = 0
            task.error = ""
            task.updated_at = host_legacy.now_iso()
        self.emit(force=True)
        self.wake.set()

    def worker_loop(self) -> None:
        while True:
            task = None
            with self.lock:
                task = next((item for item in self.tasks if item.status == "queued"), None)
                if task:
                    task.status = "downloading"
                    task.error = ""
                    task.updated_at = host_legacy.now_iso()
            if not task:
                self.wake.wait(1.0)
                self.wake.clear()
                continue

            self.emit(force=True)
            completed = False
            cancelled = False
            final_error = ""

            for attempt in range(AUTO_RETRY_COUNT + 1):
                try:
                    if attempt:
                        with self.lock:
                            task.error = ""
                            task.current_title = f"自动重试 {attempt}/{AUTO_RETRY_COUNT}…"
                            task.speed = ""
                            task.eta = ""
                            task.updated_at = host_legacy.now_iso()
                        self.emit(force=True)
                    self._download(task)
                    completed = True
                    break
                except cancelled_error_cls:
                    cancelled = True
                    break
                except Exception as exc:
                    final_error = str(exc).strip()[-500:]
                    if attempt >= AUTO_RETRY_COUNT:
                        break
                    delay = AUTO_RETRY_DELAYS[min(attempt, len(AUTO_RETRY_DELAYS) - 1)]
                    with self.lock:
                        task.error = final_error
                        task.current_title = f"失败，{delay} 秒后自动重试 {attempt + 1}/{AUTO_RETRY_COUNT}"
                        task.speed = ""
                        task.eta = ""
                        task.updated_at = host_legacy.now_iso()
                    self.emit(force=True)
                    for _ in range(delay * 10):
                        if task.id in self.cancelled:
                            cancelled = True
                            break
                        time.sleep(0.1)
                    if cancelled:
                        break

            with self.lock:
                if cancelled or task.id in self.cancelled:
                    task.status = "cancelled"
                    task.error = ""
                    self.cancelled.discard(task.id)
                elif completed:
                    task.status = "done"
                    task.progress = 100.0
                    task.error = ""
                else:
                    task.status = "error"
                    task.error = final_error or "任务失败"
                task.updated_at = host_legacy.now_iso()
            self.emit(force=True)

    def base_download(self, task) -> None:
        output_dir = self.download_root / ("audio" if task.mode == "audio" else "video")
        output_dir.mkdir(parents=True, exist_ok=True)
        archive = output_dir / f"archive-{task.mode}.txt"
        outtmpl = str(output_dir / "%(playlist_title,channel,uploader|Default)s" / "%(title)s [%(id)s].%(ext)s")

        def hook(status: dict) -> None:
            if task.id in self.cancelled:
                raise cancelled_error_cls("用户取消任务")
            info = status.get("info_dict") or {}
            with self.lock:
                task.current_title = str(info.get("title") or task.current_title or task.title)
                task.item_index = int(info.get("playlist_index") or task.item_index or 0)
                task.item_count = int(info.get("playlist_count") or task.item_count or 0)
                task.updated_at = host_legacy.now_iso()
                if status.get("status") == "downloading":
                    total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                    downloaded = status.get("downloaded_bytes") or 0
                    if total:
                        item_percent = downloaded / total * 100
                        if task.item_count and task.item_index:
                            task.progress = ((task.item_index - 1) + item_percent / 100) / task.item_count * 100
                        else:
                            task.progress = item_percent
                    task.speed = self._format_speed(status.get("speed"))
                    task.eta = self._format_eta(status.get("eta"))
                elif status.get("status") == "finished" and task.item_count and task.item_index:
                    task.progress = task.item_index / task.item_count * 100
            self.emit()

        options = {
            "outtmpl": outtmpl,
            "noplaylist": False,
            "ignoreerrors": False,
            "continuedl": True,
            "retries": 10,
            "fragment_retries": 10,
            "windowsfilenames": True,
            "trim_file_name": 180,
            "download_archive": str(archive),
            "progress_hooks": [hook],
            "ffmpeg_location": self.ffmpeg_path,
            "quiet": True,
            "no_warnings": False,
            "concurrent_fragment_downloads": 4,
            "logger": host_legacy.HostLogger(),
        }
        if host_legacy.COOKIE_PATH.is_file():
            options["cookiefile"] = str(host_legacy.COOKIE_PATH)
        if self.node_path:
            options.update({
                "no_plugins": True,
                "remote_components": {"ejs:github"},
                "js_runtimes": {"node": {"path": self.node_path}},
            })
        if task.mode == "audio":
            options.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                }],
            })
        else:
            options.update({"format": "bv*+ba/b", "merge_output_format": "mp4"})

        with yt_dlp.YoutubeDL(options) as ydl:
            code = ydl.download([task.url])
            if code:
                raise RuntimeError(f"yt-dlp 返回错误码 {code}")

    queue_manager_cls.retry = retry
    queue_manager_cls._worker_loop = worker_loop
    queue_manager_cls._download = base_download
    queue_manager_cls._runtime_enhancements_installed = True


def install_version_info(queue_manager_cls) -> None:
    """Advertise capabilities after all other monkey patches have been installed."""
    original = queue_manager_cls.host_info

    def host_info(self):
        info = original(self)
        info.update({
            "version": "1.2.0",
            "album_organization": True,
            "auto_retry_count": AUTO_RETRY_COUNT,
            "manual_retry": True,
        })
        return info

    queue_manager_cls.host_info = host_info
