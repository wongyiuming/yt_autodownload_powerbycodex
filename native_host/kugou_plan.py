"""Validate the non-secret playlist snapshot that a user has confirmed."""

from __future__ import annotations

from dataclasses import asdict
import re

from kugou_library import KugouPlaylist
from platform_urls import classify_source


MAX_PLAYLISTS = 10000
_ID = re.compile(r"(?:[1-9][0-9]{0,19}|collection_[0-9]+_[0-9]+_[1-9][0-9]*_[0-9]+)\Z")
_LIST_ID = re.compile(r"[1-9][0-9]{0,19}\Z")


def playlist_plan(records) -> tuple[KugouPlaylist, ...]:
    """Accept only playlist descriptors, never arbitrary URLs or credentials."""
    if not isinstance(records, (list, tuple)) or len(records) > MAX_PLAYLISTS:
        raise ValueError("已确认的酷狗歌单集合无效，请重新读取歌单 URL 并确认")
    result = []
    seen = set()
    for record in records:
        if isinstance(record, KugouPlaylist):
            row = asdict(record)
        elif isinstance(record, dict):
            row = record
        else:
            raise ValueError("已确认的酷狗歌单记录无效，请重新读取并确认")
        identifier = row.get("id")
        list_id = row.get("list_id")
        title = row.get("title")
        url = row.get("url", "")
        count = row.get("count")
        list_type = row.get("list_type", 0)
        if (not isinstance(identifier, str) or len(identifier) > 160 or not _ID.fullmatch(identifier)
                or not isinstance(list_id, str) or not _LIST_ID.fullmatch(list_id)
                or not isinstance(title, str) or not title or len(title) > 256
                or not isinstance(url, str) or len(url) > 1024
                or (count is not None and (type(count) is not int or not 0 <= count <= 10**9))
                or type(list_type) is not int or list_type not in {0, 1}):
            raise ValueError("已确认的酷狗歌单字段无效，请重新读取并确认")
        if url:
            source = classify_source(url)
            if source.platform != "kugou" or source.kind != "playlist" or source.url != url:
                raise ValueError("歌单预览包含无效的公开 URL，请重新读取并确认")
        key = (list_type, list_id)
        if key in seen:
            raise ValueError("已确认的酷狗歌单集合包含重复记录，请重新读取并确认")
        seen.add(key)
        result.append(KugouPlaylist(identifier, title, count, url, list_id, list_type))
    return tuple(result)


def serialize_plan(records) -> list[dict]:
    return [asdict(playlist) for playlist in playlist_plan(records)]
