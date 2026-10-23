"""Kugou queue integration: HTTP media transfer, then local FFmpeg conversion.

The Kugou path never invokes yt-dlp. Only fully written MP3s enter its archive.
"""

from __future__ import annotations

import atexit
import json
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from kugou import KugouClient
from platform_urls import classify_source


_process_lock = threading.RLock()
_active_processes = set()
_exiting = False


def _stop_process(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _shutdown_transcoders() -> None:
    # Native Messaging EOF ends the main thread while the queue is a daemon.
    # Its finally blocks are not guaranteed to run; reap our own FFmpeg here.
    global _exiting
    with _process_lock:
        _exiting = True
        for process in list(_active_processes):
            try:
                _stop_process(process)
            except (OSError, subprocess.SubprocessError):
                pass


atexit.register(_shutdown_transcoders)


def safe_name(value: str, fallback: str = "未命名", limit: int = 80) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" .")[:limit].rstrip(" .") or fallback
    if value.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(
        f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
    )}:
        value = "_" + value
    return value


def convert_to_mp3(ffmpeg: str, source: Path, target: Path, check_cancelled) -> None:
    """Poll FFmpeg so cancelling also stops conversion and reaps its process."""
    check_cancelled()
    # Keep diagnostics out of Native Messaging stdout. A file avoids pipe
    # backpressure without keeping unbounded FFmpeg output in memory.
    with tempfile.TemporaryFile() as errors:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
        with _process_lock:
            if _exiting:
                raise RuntimeError("Host 正在退出，停止酷狗音频转换")
            process = subprocess.Popen(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(source), "-vn", "-c:a", "libmp3lame", "-q:a", "0", str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, **kwargs,
            )
            _active_processes.add(process)
        try:
            while process.poll() is None:
                check_cancelled()
                time.sleep(0.1)
            check_cancelled()
            if process.returncode:
                errors.seek(0)
                detail = errors.read(4000).decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"酷狗音频转换失败：{detail or process.returncode}")
        finally:
            with _process_lock:
                _stop_process(process)
                _active_processes.discard(process)
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("酷狗音频转换未生成有效的 MP3 文件")


def _existing_file(root: Path, relative: str | None) -> Path | None:
    if not isinstance(relative, str):
        return None
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return path if path.is_file() and path.stat().st_size > 0 else None


def download_kugou(manager, task, cancelled_error_cls, *, client=None) -> None:
    """Run one collection as a single persisted queue task, with per-track dedupe."""
    if task.mode != "audio":
        raise ValueError("酷狗音乐仅支持音频下载，请选择 MP3")
    root = manager.download_root / "audio" / "kugou"
    root.mkdir(parents=True, exist_ok=True)
    archive_path = root / "archive-audio.json"
    if archive_path.is_file():
        try:
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError("酷狗下载记录无法读取，请检查 archive-audio.json 后重试") from exc
        if not isinstance(archive, dict):
            raise RuntimeError("酷狗下载记录格式无效：archive-audio.json")
    else:
        archive = {}

    def check_cancelled():
        if task.id in manager.cancelled:
            raise cancelled_error_cls("用户取消任务")

    def update(**values):
        from host_legacy import now_iso
        with manager.lock:
            for key, value in values.items():
                setattr(task, key, value)
            task.updated_at = now_iso()
        manager.emit()

    check_cancelled()
    update(current_title="正在读取酷狗歌单…", speed="", eta="")
    owned_client = client is None
    active_client = client or KugouClient()
    try:
        collection = active_client.resolve_collection(task.url, check_cancelled=check_cancelled)
        if not collection.tracks:
            raise RuntimeError("酷狗歌单没有可下载歌曲")
        update(item_count=len(collection.tracks), item_index=0)
        folder = root / ("_单曲" if classify_source(task.url).kind == "song" else
                         f"{safe_name(collection.title)} [{safe_name(collection.id, limit=60)}]")
        folder.mkdir(parents=True, exist_ok=True)

        failures = []
        for index, track in enumerate(collection.tracks, 1):
            check_cancelled()
            update(item_index=index, current_title=track.title or track.id, speed="", eta="",
                   progress=(index - 1) / len(collection.tracks) * 100)
            if _existing_file(root, archive.get(track.id)):
                update(progress=index / len(collection.tracks) * 100)
                continue

            def progress(status):
                check_cancelled()
                total = status.get("total_bytes") or 0
                fraction = min(1.0, status.get("downloaded_bytes", 0) / total) if total else 0.0
                # Reserve the last 5% of a track for conversion and atomic publish.
                update(progress=((index - 1) + fraction * 0.95) / len(collection.tracks) * 100,
                       speed=manager._format_speed(status.get("speed")),
                       eta=manager._format_eta(status.get("eta")))

            try:
                media = active_client.resolve_track(track, check_cancelled=check_cancelled)
                title = media.title or track.title or track.id
                update(current_title=title)
                filename = f"{index:03d} - {safe_name(title, limit=100)} [{safe_name(track.id, limit=40)}].mp3"
                target = folder / filename
                # Work in the destination filesystem, so the final rename is atomic.
                with tempfile.TemporaryDirectory(prefix=".kugou-", dir=folder) as temporary:
                    work = Path(temporary)
                    source = work / ("source." + safe_name(media.ext, "bin", 8))
                    active_client.download(media, source, progress=progress, check_cancelled=check_cancelled)
                    check_cancelled()
                    update(current_title=f"正在转换 MP3：{title}", speed="", eta="")
                    converted = work / "converted.mp3"
                    convert_to_mp3(manager.ffmpeg_path, source, converted, check_cancelled)
                    check_cancelled()
                    converted.replace(target)
                archive[track.id] = target.relative_to(root).as_posix()
                temporary_archive = archive_path.with_suffix(".tmp")
                temporary_archive.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary_archive.replace(archive_path)
                update(progress=index / len(collection.tracks) * 100)
            except cancelled_error_cls:
                raise
            except Exception as exc:
                # One unavailable track must not prevent the rest of a public
                # collection from being processed; failed tracks stay retryable.
                from host_legacy import HostLogger
                detail = f"{track.title or track.id}：{exc}"
                HostLogger().warning(f"Kugou track failed {track.id}: {exc}")
                failures.append(detail)
        check_cancelled()
        if failures:
            update(progress=(len(collection.tracks) - len(failures)) / len(collection.tracks) * 100)
            raise RuntimeError(
                f"酷狗歌单有 {len(failures)}/{len(collection.tracks)} 首失败；已成功条目保留，重试会跳过。"
                + "；".join(failures[:2])[:350]
            )
    finally:
        if owned_client:
            active_client.close()
