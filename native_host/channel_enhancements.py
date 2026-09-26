"""Patch album_support to use the channel page's browser-visible album shelf."""

from __future__ import annotations

import json
import re

import album_support
from youtube_album_catalog import fetch_channel_album_cards


def _clean_channel_title(value: str) -> str:
    text = album_support.clean_channel_title(value)
    text = re.sub(r"\s+-\s+(主题|主題)$", "", text, flags=re.I)
    text = re.sub(r"\s+-\s+YouTube$", "", text, flags=re.I)
    return text.strip() or "频道"


def install_channel_enhancements() -> None:
    if getattr(album_support.AlbumOrganizer, "_http_album_catalog_installed", False):
        return

    def discover(self, channel_url: str):
        browser_title, cards, shelf_found = fetch_channel_album_cards(channel_url)
        channel_title = _clean_channel_title(browser_title or "频道")
        albums = []
        used_folders: dict[str, str] = {}

        for card in cards:
            playlist_id = card.playlist_id
            url = card.url
            title = card.title.strip() or playlist_id
            folder = album_support.safe_component(title, "未命名专辑")
            old_id = used_folders.get(folder.casefold())
            if old_id is not None and old_id != playlist_id:
                folder = album_support.safe_component(f"{title} [{playlist_id[-8:]}]")
            used_folders[folder.casefold()] = playlist_id

            info = self._flat(url)
            tracks = []
            seen_tracks: set[str] = set()
            for index, track in enumerate(info.get("entries") or [], 1):
                if not isinstance(track, dict):
                    continue
                video_id = album_support.video_id_from_entry(track)
                if not video_id or video_id in seen_tracks:
                    continue
                seen_tracks.add(video_id)
                tracks.append(album_support.AlbumTrack(
                    video_id=video_id,
                    title=str(track.get("title") or video_id).strip(),
                    index=int(track.get("playlist_index") or index),
                ))

            if not tracks:
                self.log.warning(f"album track metadata empty: {title} ({playlist_id})")
            albums.append(album_support.AlbumRelease(
                title=title,
                folder=folder,
                url=str(info.get("webpage_url") or url),
                playlist_id=playlist_id,
                tracks=tracks,
            ))

        upload_ids: list[str] = []
        try:
            root_info = self._flat(channel_url)
            root_title = str(
                root_info.get("channel") or root_info.get("uploader") or root_info.get("title") or ""
            ).strip()
            if root_title:
                channel_title = _clean_channel_title(root_title)
            upload_ids = sorted(self._collect_video_ids(root_info))
        except Exception as exc:
            self.log.warning(f"channel uploads discovery failed {channel_url}: {exc}")

        if shelf_found and not albums:
            raise RuntimeError("已识别“专辑和单曲”区域，但没有得到可用专辑")

        catalog = album_support.ChannelCatalog(
            source_url=channel_url,
            channel_title=channel_title,
            channel_folder=album_support.safe_component(channel_title, "频道"),
            releases_url=f"{channel_url}#albums-and-singles",
            albums=albums,
            upload_ids=upload_ids,
        )
        self._save_catalog(catalog)
        self.log.info(
            f"browser catalog {channel_title}: {len(albums)} releases, "
            f"{sum(len(item.tracks) for item in albums)} memberships, {len(upload_ids)} uploads"
        )
        return catalog

    def download_channel(self, task) -> None:
        self._task_text(task, "正在读取频道页面的“专辑和单曲”…")
        catalog = self.discover(task.url)
        root = self._mode_root(task.mode)
        archive = root / f"archive-{task.mode}.txt"
        channel_root = root / catalog.channel_folder
        channel_root.mkdir(parents=True, exist_ok=True)

        for album in catalog.albums:
            (channel_root / album.folder).mkdir(parents=True, exist_ok=True)
        other_root = channel_root / "_其他上传"
        other_root.mkdir(parents=True, exist_ok=True)

        if not catalog.albums:
            out = other_root / "%(title)s [%(id)s].%(ext)s"
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
                task.updated_at = album_support.now_iso()
            self.m.emit(force=True)
            out = channel_root / album.folder / "%(playlist_index)02d - %(title)s [%(id)s].%(ext)s"
            self._download(task, album.url, str(out), archive, album.title)

        self._check_cancel(task)
        self._task_text(task, "正在补充频道中不属于专辑的其他上传…")
        out = other_root / "%(title)s [%(id)s].%(ext)s"
        self._download(task, task.url, str(out), archive, "其他上传")
        after = self.organize(catalog, task.mode)
        self.log.info(f"post-organize {catalog.channel_title}: {json.dumps(after, ensure_ascii=False)}")

    album_support.AlbumOrganizer.discover = discover
    album_support.AlbumOrganizer.download_channel = download_channel
    album_support.AlbumOrganizer._http_album_catalog_installed = True
