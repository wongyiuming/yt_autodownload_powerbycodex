"""Kugou queue integration: HTTP media transfer, then local FFmpeg conversion.

The Kugou path never invokes yt-dlp. Only fully written MP3s enter its archive.
"""

from __future__ import annotations

import atexit
import filecmp
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from kugou import KugouClient, KugouError
from kugou_library import KugouAuthError, KugouLibraryClient
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


def _materialize(source: Path, target: Path, check_cancelled) -> None:
    """Reuse a complete file without overwriting an unrelated destination."""
    check_cancelled()
    if target.is_symlink():
        raise RuntimeError("歌曲目标是符号链接，已保留原链接，请处理后重试")
    if target.exists():
        if target.is_file() and (source.samefile(target) or filecmp.cmp(source, target, shallow=False)):
            return
        raise RuntimeError("歌曲目标已存在且内容不同，已保留原文件，请处理冲突后重试")
    try:
        os.link(source, target)
        return
    except FileExistsError:
        # A file may have appeared since the existence check.
        return _materialize(source, target, check_cancelled)
    except OSError:
        pass
    created = False
    try:
        # Cross-volume or non-NTFS destinations may not support hard links.
        # Exclusive creation also protects against a concurrent user file.
        with target.open("xb") as output:
            created = True
            with source.open("rb") as incoming:
                while chunk := incoming.read(1024 * 1024):
                    check_cancelled()
                    output.write(chunk)
                check_cancelled()
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def download_kugou(manager, task, cancelled_error_cls, *, client=None) -> None:
    """Run a public collection or an account library as one persisted task."""
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
    source = classify_source(task.url)
    personal_library = source.kind == "library"
    update(current_title="正在读取已确认歌单的歌曲…" if personal_library else "正在读取酷狗歌单…",
           speed="", eta="", progress=0, item_count=0, item_index=0,
           playlist_count=0, playlist_index=0)
    owned_client = client is None
    if personal_library:
        if not task.kugou_confirmed or task.id not in getattr(manager, "_kugou_plans", {}):
            raise ValueError("此任务没有已确认的歌单集合；请重新读取歌单 URL 并确认下载")
        playlists = manager._kugou_plans[task.id]
        session = getattr(manager, "_kugou_sessions", {}).get(task.id)
        if not session or session.user_id != task.kugou_user_id:
            raise KugouAuthError("酷狗个人歌单登录会话已失效；请在原账号登录后从扩展点击重试")
        active_client = client or KugouLibraryClient(session)
    elif source.kind == "song" and task.use_cookies:
        from kugou_playback import KugouSongClient
        session = getattr(manager, "_kugou_sessions", {}).get(task.id)
        if not session or session.user_id != task.kugou_user_id:
            raise KugouAuthError("酷狗单曲登录会话已结束；请在原歌曲页面以原账号重试")
        active_client = client or KugouSongClient(session)
    else:
        active_client = client or KugouClient()
    try:
        collections = []
        playlist_failures = []
        if personal_library:
            # Never re-enumerate the account here or on retry: the download
            # scope is exactly the snapshot displayed before confirmation.
            update(playlist_count=len(playlists))
            for playlist_index, playlist in enumerate(playlists, 1):
                check_cancelled()
                update(playlist_index=playlist_index,
                       current_title=f"读取歌单 {playlist_index}/{len(playlists)}：{playlist.title}")
                try:
                    collection = active_client.resolve_playlist(playlist, check_cancelled=check_cancelled)
                    collections.append((playlist_index, collection))
                except (cancelled_error_cls, KugouAuthError):
                    raise
                except KugouError as exc:
                    playlist_failures.append(f"{playlist.title}：{exc}")
        else:
            collection = active_client.resolve_collection(task.url, check_cancelled=check_cancelled)
            if not collection.tracks:
                raise RuntimeError("酷狗歌单没有可下载歌曲")
            collections.append((1, collection))

        item_count = sum(len(collection.tracks) for _, collection in collections)
        update(item_count=item_count, item_index=0, playlist_index=0)
        account_root = root / f"用户 {safe_name(task.kugou_user_id, limit=24)}" if personal_library else root
        account_root.mkdir(parents=True, exist_ok=True)
        failures = []
        index = 0
        for playlist_index, collection in collections:
            check_cancelled()
            folder = account_root / ("_单曲" if source.kind == "song" else
                                      f"{safe_name(collection.title)} [{safe_name(collection.id, limit=60)}]")
            folder.mkdir(parents=True, exist_ok=True)
            if personal_library:
                update(playlist_index=playlist_index, current_title=collection.title)
            for track_index, track in enumerate(collection.tracks, 1):
                index += 1
                check_cancelled()
                title = track.title or track.id
                display_title = f"{collection.title} · {title}" if personal_library else title
                update(item_index=index, current_title=display_title, speed="", eta="",
                       progress=(index - 1) / item_count * 100)

                def progress(status):
                    check_cancelled()
                    total = status.get("total_bytes") or 0
                    fraction = min(1.0, status.get("downloaded_bytes", 0) / total) if total else 0.0
                    update(progress=((index - 1) + fraction * 0.95) / item_count * 100,
                           speed=manager._format_speed(status.get("speed")),
                           eta=manager._format_eta(status.get("eta")))

                try:
                    existing = _existing_file(root, archive.get(track.id))
                    # Public collections retain their existing archive behavior.
                    # An account library also materializes every playlist's copy.
                    if existing and not personal_library and source.kind != "song":
                        update(progress=index / item_count * 100)
                        continue
                    filename = f"{track_index:03d} - {safe_name(title, limit=100)} [{safe_name(track.id, limit=40)}].mp3"
                    target = folder / filename
                    if existing:
                        update(current_title=f"复用已下载文件：{display_title}")
                        _materialize(existing, target, check_cancelled)
                    else:
                        media = active_client.resolve_track(track, check_cancelled=check_cancelled)
                        if not personal_library:
                            # Keep public single-song filenames based on playback metadata.
                            title = media.title or title
                            target = folder / f"{track_index:03d} - {safe_name(title, limit=100)} [{safe_name(track.id, limit=40)}].mp3"
                        with tempfile.TemporaryDirectory(prefix=".kugou-", dir=folder) as temporary:
                            work = Path(temporary)
                            downloaded = work / ("source." + safe_name(media.ext, "bin", 8))
                            active_client.download(media, downloaded, progress=progress, check_cancelled=check_cancelled)
                            check_cancelled()
                            if media.ext.lower() == "mp3":
                                update(current_title=f"正在保存 MP3：{display_title}", speed="", eta="")
                                converted = downloaded
                            else:
                                update(current_title=f"正在转换 MP3：{display_title}", speed="", eta="")
                                converted = work / "converted.mp3"
                                convert_to_mp3(manager.ffmpeg_path, downloaded, converted, check_cancelled)
                            _materialize(converted, target, check_cancelled)
                        archive[track.id] = target.relative_to(root).as_posix()
                        temporary_archive = archive_path.with_suffix(".tmp")
                        temporary_archive.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
                        temporary_archive.replace(archive_path)
                    update(progress=index / item_count * 100)
                except (cancelled_error_cls, KugouAuthError):
                    raise
                except Exception as exc:
                    from host_legacy import HostLogger
                    detail = f"{display_title}：{exc}"
                    HostLogger().warning(f"Kugou track failed {track.id}: {exc}")
                    failures.append(detail)
        check_cancelled()
        if failures or playlist_failures:
            percent = (item_count - len(failures)) / item_count * 100 if item_count else 0
            update(progress=min(percent, 99) if playlist_failures else percent)
            parts = []
            if playlist_failures:
                parts.append(f"{len(playlist_failures)}/{len(playlists)} 张歌单读取失败")
            if failures:
                parts.append(f"{len(failures)}/{item_count} 首失败")
            raise RuntimeError("酷狗歌单有 " + "，".join(parts)
                               + "；已成功条目保留，重试会跳过。"
                               + "；".join((playlist_failures + failures)[:2])[:350])
        if personal_library:
            update(progress=100, playlist_index=len(playlists),
                   current_title=f"个人歌单完成：{len(playlists)} 张歌单，{item_count} 首歌曲")
    finally:
        if owned_client:
            active_client.close()
