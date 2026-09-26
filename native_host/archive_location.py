"""Keep yt-dlp download archives beside the media they describe."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil

import album_support
import host_legacy

LEGACY_APP_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "YTAutodownload"


def _media_root(manager, mode: str) -> Path:
    root = manager.download_root / ("audio" if mode == "audio" else "video")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _migrate_archive(media_root: Path, mode: str) -> Path:
    """Move/merge the old app-data archive into its media directory once."""
    target = media_root / f"archive-{mode}.txt"
    legacy = LEGACY_APP_DIR / f"archive-{mode}.txt"
    if not legacy.is_file():
        return target

    try:
        if not target.exists():
            shutil.move(str(legacy), str(target))
            return target

        # Both exist: preserve every unique yt-dlp archive entry, then retire the old file.
        lines: list[str] = []
        seen: set[str] = set()
        for path in (target, legacy):
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")
        legacy.unlink(missing_ok=True)
    except OSError as exc:
        host_legacy.HostLogger().warning(f"archive migration failed {legacy} -> {target}: {exc}")
        # Falling back to the legacy path preserves existing download history.
        return legacy
    return target


@contextmanager
def _archive_scope(media_root: Path):
    """Redirect only the dynamic APP_DIR lookup used by download_archive."""
    old_legacy = host_legacy.APP_DIR
    old_album = album_support.APP_DIR
    host_legacy.APP_DIR = media_root
    album_support.APP_DIR = media_root
    try:
        yield
    finally:
        host_legacy.APP_DIR = old_legacy
        album_support.APP_DIR = old_album


def install_archive_location(queue_manager_cls) -> None:
    if getattr(queue_manager_cls, "_archive_location_installed", False):
        return

    original_download = queue_manager_cls._download
    original_host_info = queue_manager_cls.host_info

    def download(self, task):
        media_root = _media_root(self, task.mode)
        archive = _migrate_archive(media_root, task.mode)

        # Normal case: target is inside the media root. If migration failed and
        # returned the legacy file, keep old behavior rather than losing history.
        if archive.parent == media_root:
            with _archive_scope(media_root):
                return original_download(self, task)
        return original_download(self, task)

    def host_info(self):
        info = original_host_info(self)
        info.update({"version": "1.1.1", "archive_in_media_dir": True})
        return info

    queue_manager_cls._download = download
    queue_manager_cls.host_info = host_info
    queue_manager_cls._archive_location_installed = True
