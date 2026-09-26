"""Chrome Native Messaging host for YT AutoDownload Queue."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import imageio_ffmpeg
import yt_dlp

try:
    from opencc import OpenCC
except Exception:  # optional at runtime; downloader still works without filename conversion
    OpenCC = None

HOST_NAME = "com.wongyiuming.yt_autodownload"
APP_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "YTAutodownload"
STATE_PATH = APP_DIR / "state.json"
COOKIE_PATH = APP_DIR / "youtube-cookies.txt"
DEFAULT_DOWNLOAD_ROOT = Path.home() / "Downloads" / "YT-Autodownload"
TERMINAL = {"done", "error", "cancelled"}
LOG_PATH = APP_DIR / "host.log"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def classify_url(raw: str) -> tuple[str, str]:
    value = (raw or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("仅支持 http/https URL")
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]

    if host == "youtu.be":
        video_id = next((part for part in parsed.path.split("/") if part), "")
        if not video_id:
            raise ValueError("YouTube 短链接缺少视频 ID")
        return "video", f"https://www.youtube.com/watch?v={video_id}"

    if host not in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        raise ValueError("当前版本仅接受 YouTube 视频、播放列表或公开频道 URL")

    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    list_id = (query.get("list") or [""])[0]
    if parsed.path == "/playlist" and list_id:
        return "playlist", f"https://www.youtube.com/playlist?{urlencode({'list': list_id})}"
    if parsed.path == "/watch" and list_id:
        return "playlist", f"https://www.youtube.com/playlist?{urlencode({'list': list_id})}"
    if parsed.path == "/watch" and (query.get("v") or [""])[0]:
        video_id = (query.get("v") or [""])[0]
        return "video", f"https://www.youtube.com/watch?v={video_id}"
    if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
        return "video", f"https://www.youtube.com/watch?v={parts[1]}"

    root_parts: list[str] | None = None
    if parts and parts[0].startswith("@"):
        root_parts = [parts[0]]
    elif len(parts) >= 2 and parts[0] in {"channel", "c", "user"}:
        root_parts = parts[:2]
    if root_parts:
        return "channel", f"https://www.youtube.com/{'/'.join(root_parts)}"

    raise ValueError("无法识别为单个视频、视频列表或公开频道")


@dataclass
class Task:
    id: str
    url: str
    kind: str
    mode: str
    title: str = ""
    status: str = "queued"
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    current_title: str = ""
    item_index: int = 0
    item_count: int = 0
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


class HostLogger:
    """Keep yt-dlp diagnostics away from stdout, which belongs to Native Messaging."""

    _lock = threading.Lock()

    def _write(self, level: str, message: str) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            with self._lock, LOG_PATH.open("a", encoding="utf-8") as stream:
                stream.write(f"[{now_iso()}] [{level}] {message}\n")
        except Exception:
            pass

    def debug(self, message: str) -> None:
        if message.startswith("[debug]"):
            self._write("DEBUG", message)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)


class CancelledError(RuntimeError):
    pass


class NativeWriter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def send(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            sys.stdout.buffer.write(struct.pack("<I", len(payload)))
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()


class QueueManager:
    def __init__(self, writer: NativeWriter) -> None:
        self.writer = writer
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.tasks: list[Task] = []
        self.cancelled: set[str] = set()
        self.download_root = DEFAULT_DOWNLOAD_ROOT
        self.node_path = self._find_node()
        self.ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        self._last_emit = 0.0
        self._load()
        self.worker = threading.Thread(target=self._worker_loop, name="download-queue", daemon=True)
        self.worker.start()

    @staticmethod
    def _find_node() -> str | None:
        candidates = []
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir / "node.exe",
            exe_dir / "runtime" / "node.exe",
            exe_dir.parent / "runtime" / "node.exe",
        ])
        if frozen_root := getattr(sys, "_MEIPASS", None):
            candidates.extend([
                Path(frozen_root) / "node.exe",
                Path(frozen_root) / "runtime" / "node.exe",
            ])
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return shutil.which("node") or shutil.which("node.exe")

    def host_info(self) -> dict:
        return {
            "name": HOST_NAME,
            "version": "1.0.0",
            "download_root": str(self.download_root),
            "ffmpeg": self.ffmpeg_path,
            "node": self.node_path or "",
            "python": sys.version.split()[0],
        }

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [asdict(task) for task in self.tasks]

    def _load(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if not STATE_PATH.is_file():
            return
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            configured_root = data.get("download_root")
            if configured_root:
                self.download_root = Path(configured_root)
            for item in data.get("tasks", []):
                task = Task(**{field: item.get(field, getattr(Task("", "", "", ""), field)) for field in Task.__dataclass_fields__})
                if task.status == "downloading":
                    task.status = "queued"
                    task.error = ""
                self.tasks.append(task)
        except Exception:
            self.tasks = []

    def _save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        with self.lock:
            payload = {
                "version": 1,
                "download_root": str(self.download_root),
                "tasks": [asdict(task) for task in self.tasks],
            }
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)

    def emit(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < 0.35:
            return
        self._last_emit = now
        self._save()
        self.writer.send({"event": "queue", "queue": self.snapshot()})

    def enqueue(self, raw_url: str, mode: str, title: str, cookies: list[dict]) -> Task:
        kind, normalized = classify_url(raw_url)
        if mode not in {"video", "audio"}:
            raise ValueError("mode 必须是 video 或 audio")
        self._write_cookies(cookies)
        stamp = now_iso()
        task = Task(
            id=uuid.uuid4().hex[:12],
            url=normalized,
            kind=kind,
            mode=mode,
            title=(title or "").strip(),
            created_at=stamp,
            updated_at=stamp,
        )
        with self.lock:
            self.tasks.append(task)
        self.emit(force=True)
        self.wake.set()
        return task

    def cancel(self, task_id: str) -> None:
        with self.lock:
            task = self._task(task_id)
            if task.status == "queued":
                task.status = "cancelled"
                task.updated_at = now_iso()
            elif task.status == "downloading":
                self.cancelled.add(task_id)
            elif task.status not in TERMINAL:
                task.status = "cancelled"
        self.emit(force=True)

    def remove(self, task_id: str) -> None:
        with self.lock:
            task = self._task(task_id)
            if task.status not in TERMINAL:
                raise ValueError("只能移除已完成、失败或取消的任务")
            self.tasks = [item for item in self.tasks if item.id != task_id]
        self.emit(force=True)

    def clear_finished(self) -> None:
        with self.lock:
            self.tasks = [task for task in self.tasks if task.status not in TERMINAL]
        self.emit(force=True)

    def _task(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise ValueError("任务不存在")

    def _write_cookies(self, cookies: list[dict]) -> None:
        if not cookies:
            return
        lines = ["# Netscape HTTP Cookie File"]
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            domain = str(cookie.get("domain") or "")
            if "youtube.com" not in domain:
                continue
            lines.append("\t".join([
                domain,
                "TRUE" if domain.startswith(".") else "FALSE",
                str(cookie.get("path") or "/"),
                "TRUE" if cookie.get("secure") else "FALSE",
                str(max(0, int(cookie.get("expirationDate") or 0))),
                str(cookie.get("name") or ""),
                str(cookie.get("value") or "").replace("\t", "").replace("\n", ""),
            ]))
        if len(lines) > 1:
            COOKIE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def _worker_loop(self) -> None:
        while True:
            task = None
            with self.lock:
                task = next((item for item in self.tasks if item.status == "queued"), None)
                if task:
                    task.status = "downloading"
                    task.error = ""
                    task.updated_at = now_iso()
            if not task:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            self.emit(force=True)
            try:
                self._download(task)
                with self.lock:
                    if task.id in self.cancelled:
                        task.status = "cancelled"
                        self.cancelled.discard(task.id)
                    else:
                        task.status = "done"
                        task.progress = 100.0
                    task.updated_at = now_iso()
            except CancelledError:
                with self.lock:
                    task.status = "cancelled"
                    task.updated_at = now_iso()
                    self.cancelled.discard(task.id)
            except Exception as exc:
                with self.lock:
                    task.status = "error"
                    task.error = str(exc).strip()[-500:]
                    task.updated_at = now_iso()
            finally:
                self.emit(force=True)

    def _download(self, task: Task) -> None:
        output_dir = self.download_root / ("audio" if task.mode == "audio" else "video")
        output_dir.mkdir(parents=True, exist_ok=True)
        archive = APP_DIR / f"archive-{task.mode}.txt"
        outtmpl = str(output_dir / "%(playlist_title,channel,uploader|Default)s" / "%(title)s [%(id)s].%(ext)s")

        def hook(status: dict) -> None:
            if task.id in self.cancelled:
                raise CancelledError("用户取消任务")
            info = status.get("info_dict") or {}
            with self.lock:
                task.current_title = str(info.get("title") or task.current_title or task.title)
                task.item_index = int(info.get("playlist_index") or task.item_index or 0)
                task.item_count = int(info.get("playlist_count") or task.item_count or 0)
                task.updated_at = now_iso()
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
            "logger": HostLogger(),
        }
        if COOKIE_PATH.is_file():
            options["cookiefile"] = str(COOKIE_PATH)
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
            options.update({
                "format": "bv*+ba/b",
                "merge_output_format": "mp4",
            })

        with yt_dlp.YoutubeDL(options) as ydl:
            code = ydl.download([task.url])
            if code:
                raise RuntimeError(f"yt-dlp 返回错误码 {code}")

    @staticmethod
    def _format_speed(value: float | None) -> str:
        if not value:
            return ""
        number = float(value)
        for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
            if number < 1024 or unit == "GiB/s":
                return f"{number:.1f} {unit}"
            number /= 1024
        return ""

    @staticmethod
    def _format_eta(value: int | float | None) -> str:
        if value is None:
            return ""
        seconds = max(0, int(value))
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def read_message() -> dict | None:
    header = sys.stdin.buffer.read(4)
    if not header or len(header) < 4:
        return None
    size = struct.unpack("<I", header)[0]
    if size <= 0 or size > 64 * 1024 * 1024:
        raise RuntimeError("无效的 Native Messaging 消息长度")
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size:
        return None
    return json.loads(payload.decode("utf-8"))


def main() -> int:
    writer = NativeWriter()
    manager = QueueManager(writer)
    writer.send({"event": "hello", "host": manager.host_info(), "queue": manager.snapshot()})
    while True:
        try:
            message = read_message()
            if message is None:
                # The persistent background Native Messaging port keeps this process alive
                # while Chrome is running. If Chrome closes the pipe, exit; persisted queued
                # state is recovered on the next Chrome start and the archive prevents repeats.
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
                writer.send({"ok": False, "reply_to": request_id, "error": str(exc)})
        except Exception as exc:
            try:
                writer.send({"event": "host_error", "error": str(exc), "trace": traceback.format_exc(limit=3)})
            except Exception:
                return 2


if __name__ == "__main__":
    raise SystemExit(main())
