"""Album-aware channel downloads and migration for YT AutoDownload Queue."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
from urllib.parse import parse_qs, urlsplit

import yt_dlp

APP_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "YTAutodownload"
COOKIE_PATH = APP_DIR / "youtube-cookies.txt"
LOG_PATH = APP_DIR / "host.log"
JOURNAL_PATH = APP_DIR / "organization-journal.jsonl"
CATALOG_DIR = APP_DIR / "catalogs"
MEDIA_SUFFIXES = {".mp3", ".mp4", ".m4a", ".webm", ".opus", ".aac", ".flac", ".mkv", ".mov"}
VIDEO_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\](?=\.[^.]+$)")
INVALID_WINDOWS_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_component(value: str, fallback: str = "未命名", limit: int = 96) -> str:
    text = INVALID_WINDOWS_CHARS_RE.sub("_", (value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .") or fallback
    return text[:limit].rstrip(" .") or fallback


def clean_channel_title(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"\s+-\s+(Releases|发行|發行)$", "", text, flags=re.I)
    text = re.sub(r"\s+-\s+Topic$", "", text, flags=re.I)
    return text.strip() or "频道"


def video_id_from_entry(entry: dict) -> str | None:
    candidate = str(entry.get("id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        return candidate
    for key in ("url", "webpage_url", "original_url"):
        raw = entry.get(key)
        if not isinstance(raw, str):
            continue
        try:
            parsed = urlsplit(raw)
            if parsed.netloc.endswith("youtu.be"):
                value = next((p for p in parsed.path.split("/") if p), "")
            else:
                value = (parse_qs(parsed.query).get("v") or [""])[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
                return value
        except Exception:
            pass
    return None


def album_url_from_entry(entry: dict) -> str | None:
    for key in ("webpage_url", "original_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    item_id = str(entry.get("id") or "")
    if item_id.startswith("MP"):
        return f"https://music.youtube.com/browse/{item_id}"
    if item_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", item_id):
        return f"https://www.youtube.com/playlist?list={item_id}"
    return None


@dataclass(frozen=True)
class AlbumTrack:
    video_id: str
    title: str
    index: int


@dataclass
class AlbumRelease:
    title: str
    folder: str
    url: str
    playlist_id: str = ""
    tracks: list[AlbumTrack] = field(default_factory=list)


@dataclass(frozen=True)
class Membership:
    album_folder: str
    track_title: str
    track_index: int


@dataclass
class ChannelCatalog:
    source_url: str
    channel_title: str
    channel_folder: str
    releases_url: str
    albums: list[AlbumRelease] = field(default_factory=list)
    upload_ids: list[str] = field(default_factory=list)

    def memberships(self) -> dict[str, list[Membership]]:
        result: dict[str, list[Membership]] = {}
        for album in self.albums:
            for track in album.tracks:
                result.setdefault(track.video_id, []).append(Membership(
                    album.folder, track.title, track.index))
        return result


class AlbumLogger:
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


class AlbumOrganizer:
    def __init__(self, manager, cancel_error):
        self.m = manager
        self.cancel_error = cancel_error
        self.log = AlbumLogger()

    def repair_channel(self, raw_url: str, cookies: list[dict]) -> dict:
        from host_legacy import classify_url
        kind, url = classify_url(raw_url)
        if kind != "channel":
            raise ValueError("整理功能只用于公开频道 URL")
        self.m._write_cookies(cookies)
        catalog = self.discover(url)
        if not catalog.albums:
            raise RuntimeError("该频道没有提取到“专辑和单曲”结构；未移动任何文件")
        results = {mode: self.organize(catalog, mode) for mode in ("audio", "video")}
        return {
            "catalog": {
                "channel": catalog.channel_title,
                "albums": len(catalog.albums),
                "tracks": sum(len(album.tracks) for album in catalog.albums),
            },
            "results": results,
        }

    def download_channel(self, task) -> None:
        self._task_text(task, "正在读取频道的专辑和单曲结构…")
        catalog = self.discover(task.url)
        root = self._mode_root(task.mode)
        archive = APP_DIR / f"archive-{task.mode}.txt"
        if not catalog.albums:
            out = root / catalog.channel_folder / "_其他上传" / "%(title)s [%(id)s].%(ext)s"
            self._download(task, task.url, str(out), archive, "其他上传")
            return

        before = self.organize(catalog, task.mode)
        self.log.info(f"pre-organize {catalog.channel_title}: {json.dumps(before, ensure_ascii=False)}")
        total = len(catalog.albums)
        for number, album in enumerate(catalog.albums, 1):
            self._check_cancel(task)
            with self.m.lock:
                task.current_title = f"专辑 {number}/{total}：{album.title}"
                task.item_index = number
                task.item_count = total + 1
                task.progress = (number - 1) / max(1, total + 1) * 100
                task.updated_at = now_iso()
            self.m.emit(force=True)
            out = root / catalog.channel_folder / album.folder / "%(playlist_index)02d - %(title)s [%(id)s].%(ext)s"
            self._download(task, album.url, str(out), archive, album.title)

        self._check_cancel(task)
        self._task_text(task, "正在补充频道中不属于专辑的其他上传…")
        out = root / catalog.channel_folder / "_其他上传" / "%(title)s [%(id)s].%(ext)s"
        self._download(task, task.url, str(out), archive, "其他上传")
        after = self.organize(catalog, task.mode)
        self.log.info(f"post-organize {catalog.channel_title}: {json.dumps(after, ensure_ascii=False)}")

    def discover(self, channel_url: str) -> ChannelCatalog:
        releases_url = f"{channel_url.rstrip('/')}/releases"
        channel_title = "频道"
        albums: list[AlbumRelease] = []
        try:
            releases = self._flat(releases_url)
            channel_title = clean_channel_title(str(
                releases.get("channel") or releases.get("uploader") or releases.get("title") or "频道"))
            used_folders: dict[str, str] = {}
            seen: set[str] = set()
            for entry in (releases.get("entries") or []):
                if not isinstance(entry, dict):
                    continue
                url = album_url_from_entry(entry)
                if not url:
                    continue
                key = str(entry.get("id") or url)
                if key in seen:
                    continue
                seen.add(key)
                info = self._flat(url)
                title = str(entry.get("title") or info.get("title") or key or "未命名专辑").strip()
                playlist_id = str(info.get("id") or entry.get("id") or "")
                folder = safe_component(title, "未命名专辑")
                old_id = used_folders.get(folder.casefold())
                if old_id is not None and old_id != playlist_id:
                    suffix = (playlist_id or hashlib.sha1(url.encode()).hexdigest())[-8:]
                    folder = safe_component(f"{title} [{suffix}]")
                used_folders[folder.casefold()] = playlist_id
                tracks, track_seen = [], set()
                for index, track in enumerate(info.get("entries") or [], 1):
                    if not isinstance(track, dict):
                        continue
                    vid = video_id_from_entry(track)
                    if not vid or vid in track_seen:
                        continue
                    track_seen.add(vid)
                    tracks.append(AlbumTrack(
                        vid, str(track.get("title") or vid).strip(),
                        int(track.get("playlist_index") or index)))
                if tracks:
                    albums.append(AlbumRelease(
                        title, folder, str(info.get("webpage_url") or url), playlist_id, tracks))
        except Exception as exc:
            self.log.warning(f"releases discovery failed {releases_url}: {exc}")

        upload_ids: list[str] = []
        try:
            root_info = self._flat(channel_url)
            title = str(root_info.get("channel") or root_info.get("uploader") or root_info.get("title") or "").strip()
            if title:
                channel_title = clean_channel_title(title)
            upload_ids = sorted(self._collect_video_ids(root_info))
        except Exception as exc:
            self.log.warning(f"channel uploads discovery failed {channel_url}: {exc}")

        catalog = ChannelCatalog(
            channel_url, channel_title, safe_component(channel_title, "频道"),
            releases_url, albums, upload_ids)
        self._save_catalog(catalog)
        self.log.info(
            f"catalog {channel_title}: {len(albums)} releases, "
            f"{sum(len(a.tracks) for a in albums)} memberships, {len(upload_ids)} uploads")
        return catalog

    def organize(self, catalog: ChannelCatalog, mode: str) -> dict:
        root = self._mode_root(mode)
        memberships = catalog.memberships()
        stats = {"moved": 0, "linked": 0, "deduped": 0, "conflicts": 0, "matched": 0, "other_moved": 0}
        if not memberships and not catalog.upload_ids:
            return stats

        files: dict[str, list[Path]] = {}
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
                match = VIDEO_ID_RE.search(path.name)
                if match:
                    files.setdefault(match.group(1), []).append(path)

        channel_root = root / catalog.channel_folder
        for vid, member_list in memberships.items():
            candidates = files.get(vid) or []
            if not candidates:
                continue
            stats["matched"] += 1
            source = next((p for p in candidates if self._under(p, channel_root)), candidates[0])
            ext, primary = source.suffix.lower(), None
            for number, member in enumerate(member_list):
                target = channel_root / member.album_folder / self._album_name(member, vid, ext)
                if number == 0:
                    primary, result = self._move_or_reuse(source, target, vid)
                else:
                    if primary is None or not primary.exists():
                        primary = self._find(root, vid)
                    if primary is None:
                        break
                    result = self._link_or_copy(primary, target, vid)
                stats[result] += 1
            if primary and primary.exists():
                for duplicate in candidates:
                    if duplicate == primary or not duplicate.exists():
                        continue
                    if self._same_content(primary, duplicate):
                        self._journal("dedupe-delete", duplicate, primary, vid)
                        duplicate.unlink(missing_ok=True)
                        stats["deduped"] += 1

        other_root = channel_root / "_其他上传"
        for vid in set(catalog.upload_ids) - set(memberships):
            for source in list(files.get(vid) or []):
                if not source.exists() or self._under(source, other_root):
                    continue
                _target, result = self._move_or_reuse(source, other_root / source.name, vid)
                stats[result] += 1
                if result == "moved":
                    stats["other_moved"] += 1
        self._remove_empty(root)
        return stats

    def _download(self, task, url: str, outtmpl: str, archive: Path, context: str) -> None:
        def hook(status: dict) -> None:
            self._check_cancel(task)
            info = status.get("info_dict") or {}
            title = str(info.get("title") or task.title or task.url)
            with self.m.lock:
                task.current_title = f"{context} / {title}" if context else title
                task.updated_at = now_iso()
                if status.get("status") == "downloading":
                    task.speed = self.m._format_speed(status.get("speed"))
                    task.eta = self.m._format_eta(status.get("eta"))
            self.m.emit()

        options = self._base_options()
        options.update({
            "outtmpl": outtmpl, "noplaylist": False, "ignoreerrors": False,
            "continuedl": True, "retries": 10, "fragment_retries": 10,
            "windowsfilenames": True, "trim_file_name": 150,
            "download_archive": str(archive), "progress_hooks": [hook],
            "ffmpeg_location": self.m.ffmpeg_path, "concurrent_fragment_downloads": 4,
        })
        if task.mode == "audio":
            options.update({
                "format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}],
            })
        else:
            options.update({"format": "bv*+ba/b", "merge_output_format": "mp4"})
        with yt_dlp.YoutubeDL(options) as ydl:
            code = ydl.download([url])
            if code:
                raise RuntimeError(f"yt-dlp 返回错误码 {code}")

    def _flat(self, url: str) -> dict:
        options = self._base_options()
        options.update({"extract_flat": "in_playlist", "skip_download": True, "ignoreerrors": True})
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        return info if isinstance(info, dict) else {}

    def _base_options(self) -> dict:
        options = {"quiet": True, "no_warnings": False, "logger": self.log}
        if COOKIE_PATH.is_file():
            options["cookiefile"] = str(COOKIE_PATH)
        if self.m.node_path:
            options.update({
                "no_plugins": True, "remote_components": {"ejs:github"},
                "js_runtimes": {"node": {"path": self.m.node_path}},
            })
        return options

    def _mode_root(self, mode: str) -> Path:
        root = self.m.download_root / ("audio" if mode == "audio" else "video")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _task_text(self, task, text: str) -> None:
        with self.m.lock:
            task.current_title, task.updated_at = text, now_iso()
        self.m.emit(force=True)

    def _check_cancel(self, task) -> None:
        if task.id in self.m.cancelled:
            raise self.cancel_error("用户取消任务")

    @staticmethod
    def _collect_video_ids(info: dict) -> set[str]:
        found, stack = set(), [info]
        while stack:
            current = stack.pop()
            if not isinstance(current, dict):
                continue
            vid = video_id_from_entry(current)
            if vid:
                found.add(vid)
            stack.extend(entry for entry in (current.get("entries") or []) if isinstance(entry, dict))
        return found

    def _save_catalog(self, catalog: ChannelCatalog) -> None:
        try:
            CATALOG_DIR.mkdir(parents=True, exist_ok=True)
            identity = hashlib.sha1(catalog.source_url.encode()).hexdigest()[:12]
            data = {"generated_at": now_iso(), **asdict(catalog)}
            (CATALOG_DIR / f"{identity}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log.warning(f"could not save catalog: {exc}")

    @staticmethod
    def _album_name(member: Membership, vid: str, ext: str) -> str:
        return f"{member.track_index:02d} - {safe_component(member.track_title, vid, 120)} [{vid}]{ext}"

    @staticmethod
    def _under(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except (ValueError, OSError):
            return False

    @staticmethod
    def _same_path(a: Path, b: Path) -> bool:
        try:
            return a.resolve() == b.resolve()
        except OSError:
            return str(a).casefold() == str(b).casefold()

    @staticmethod
    def _same_content(a: Path, b: Path) -> bool:
        try:
            if a.stat().st_size != b.stat().st_size:
                return False
            with a.open("rb") as left, b.open("rb") as right:
                while True:
                    x, y = left.read(1024 * 1024), right.read(1024 * 1024)
                    if x != y:
                        return False
                    if not x:
                        return True
        except OSError:
            return False

    def _move_or_reuse(self, source: Path, target: Path, vid: str) -> tuple[Path, str]:
        target.parent.mkdir(parents=True, exist_ok=True)
        if self._same_path(source, target):
            return target, "deduped"
        if target.exists():
            if self._same_content(source, target):
                self._journal("dedupe-delete", source, target, vid)
                source.unlink(missing_ok=True)
                return target, "deduped"
            self._journal("conflict", source, target, vid)
            return source, "conflicts"
        self._journal("move", source, target, vid)
        shutil.move(str(source), str(target))
        return target, "moved"

    def _link_or_copy(self, source: Path, target: Path, vid: str) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if self._same_content(source, target):
                return "deduped"
            self._journal("conflict", source, target, vid)
            return "conflicts"
        try:
            os.link(source, target)
            self._journal("hardlink", source, target, vid)
        except OSError:
            shutil.copy2(source, target)
            self._journal("copy", source, target, vid)
        return "linked"

    @staticmethod
    def _find(root: Path, vid: str) -> Path | None:
        marker = f"[{vid}]"
        return next((p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES and marker in p.stem), None)

    def _journal(self, action: str, source: Path, target: Path, vid: str) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            record = {"at": now_iso(), "action": action, "video_id": vid, "source": str(source), "target": str(target)}
            with JOURNAL_PATH.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _remove_empty(root: Path) -> None:
        try:
            dirs = sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
            for directory in dirs:
                try:
                    directory.rmdir()
                except OSError:
                    pass
        except OSError:
            pass


def install_album_support(queue_manager_cls, cancelled_error_cls) -> None:
    if getattr(queue_manager_cls, "_album_support_installed", False):
        return
    original_download = queue_manager_cls._download
    original_host_info = queue_manager_cls.host_info

    def host_info(self):
        info = original_host_info(self)
        info.update({"version": "1.1.0", "album_organization": True})
        return info

    def download(self, task):
        if task.kind == "channel":
            return AlbumOrganizer(self, cancelled_error_cls).download_channel(task)
        return original_download(self, task)

    def repair_channel(self, raw_url, cookies):
        return AlbumOrganizer(self, cancelled_error_cls).repair_channel(raw_url, cookies)

    queue_manager_cls.host_info = host_info
    queue_manager_cls._download = download
    queue_manager_cls.repair_channel = repair_channel
    queue_manager_cls._album_support_installed = True
