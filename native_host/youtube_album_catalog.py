"""Read YouTube's browser-visible 'Albums & singles' shelf."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time

import httpx

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}
ALBUM_SHELF_TITLES = {"专辑和单曲", "專輯和單曲", "albums & singles", "albums and singles"}


@dataclass(frozen=True)
class AlbumCard:
    title: str
    playlist_id: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/playlist?list={self.playlist_id}"


def _extract_json_object(text: str, marker: str) -> dict:
    pos = text.find(marker)
    if pos < 0:
        return {}
    pos = text.find("{", pos + len(marker))
    if pos < 0:
        return {}
    depth = 0
    in_string = False
    escaped = False
    for i in range(pos, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[pos:i + 1])
                    return value if isinstance(value, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


def _text(value) -> str:
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"]
    if isinstance(value.get("content"), str):
        return value["content"]
    return "".join(
        str(item.get("text") or "")
        for item in value.get("runs") or []
        if isinstance(item, dict)
    )


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _channel_title(data: dict) -> str:
    for obj in _walk(data):
        renderer = obj.get("channelMetadataRenderer") if isinstance(obj, dict) else None
        if isinstance(renderer, dict) and renderer.get("title"):
            return str(renderer["title"]).strip()
    return ""


def _playlist_id_from_node(node: dict) -> str:
    """Find a playlist id in one renderer without confusing channel browse ids for albums."""
    direct = str(node.get("playlistId") or "").strip()
    if direct:
        return direct

    for obj in _walk(node):
        if not isinstance(obj, dict):
            continue
        watch = obj.get("watchEndpoint")
        if isinstance(watch, dict):
            playlist_id = str(watch.get("playlistId") or "").strip()
            if playlist_id:
                return playlist_id
        browse = obj.get("browseEndpoint")
        if isinstance(browse, dict):
            browse_id = str(browse.get("browseId") or "").strip()
            # Playlist browse ids are exposed as VL<playlist id> in current channel grids.
            if browse_id.startswith("VL") and len(browse_id) > 2:
                return browse_id[2:]
    return ""


def _album_from_lockup(lockup: dict) -> AlbumCard | None:
    if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_ALBUM":
        return None
    metadata = ((lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {})
    title = _text(metadata.get("title")).strip()
    playlist_id = _playlist_id_from_node(lockup) or str(lockup.get("contentId") or "").strip()
    if not title or not playlist_id:
        return None
    return AlbumCard(title=title, playlist_id=playlist_id)


def _album_from_grid_playlist(renderer: dict) -> AlbumCard | None:
    """Parse the expanded album dialog returned by the shelf continuation.

    The channel home shelf uses lockupViewModel for the first visible cards. Opening the
    browser's full "Albums & singles" panel returns gridPlaylistRenderer objects instead.
    Ignoring this renderer was the reason only the first 12 albums were ever classified.
    """
    title = _text(renderer.get("title")).strip()
    playlist_id = _playlist_id_from_node(renderer)
    if not title or not playlist_id:
        return None
    return AlbumCard(title=title, playlist_id=playlist_id)


def _collect_albums(node, albums: dict[str, AlbumCard]) -> None:
    for obj in _walk(node):
        if not isinstance(obj, dict):
            continue

        lockup = obj.get("lockupViewModel")
        if isinstance(lockup, dict):
            card = _album_from_lockup(lockup)
            if card:
                albums.setdefault(card.playlist_id, card)

        grid = obj.get("gridPlaylistRenderer")
        if isinstance(grid, dict):
            card = _album_from_grid_playlist(grid)
            if card:
                albums.setdefault(card.playlist_id, card)


def _continuation_token(node) -> str:
    for obj in _walk(node):
        command = obj.get("continuationCommand") if isinstance(obj, dict) else None
        if isinstance(command, dict) and command.get("token"):
            return str(command["token"])
    return ""


def _find_album_shelf(data: dict) -> dict:
    fallback = {}
    for obj in _walk(data):
        shelf = obj.get("shelfRenderer") if isinstance(obj, dict) else None
        if not isinstance(shelf, dict):
            continue
        title = _text(shelf.get("title")).strip().casefold()
        items = ((shelf.get("content") or {}).get("horizontalListRenderer") or {}).get("items") or []
        has_album = any(
            isinstance(item, dict)
            and (item.get("lockupViewModel") or {}).get("contentType") == "LOCKUP_CONTENT_TYPE_ALBUM"
            for item in items
        )
        if title in ALBUM_SHELF_TITLES:
            return shelf
        if has_album:
            fallback = shelf
    return fallback


def _get_channel_page(client: httpx.Client, channel_url: str, *, attempts: int = 3) -> httpx.Response:
    """Fetch the channel page with a small retry for transient/challenge-shaped responses."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(channel_url, params={"hl": "zh-CN", "gl": "US"})
            response.raise_for_status()
            if _extract_json_object(response.text, "ytInitialData"):
                return response
            last_error = RuntimeError("YouTube 页面未找到 ytInitialData")
        except Exception as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(float(attempt))
    raise RuntimeError(str(last_error or "读取 YouTube 频道页面失败"))


def fetch_channel_album_cards(channel_url: str, *, max_continuations: int = 20) -> tuple[str, list[AlbumCard], bool]:
    """Return (channel_title, albums, album_shelf_found) from the public channel page.

    The data source is the same ytInitialData / youtubei browse payload rendered by the
    browser. The first shelf cards are lockupViewModel objects; opening the full album
    panel returns gridPlaylistRenderer objects and may continue onto additional pages.
    A network/parsing failure raises instead of silently classifying music as
    '_其他上传'. Channels that genuinely have no album shelf return an empty list.
    """
    channel_url = channel_url.rstrip("/")
    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=30.0, http2=True) as client:
        response = _get_channel_page(client, channel_url)
        html = response.text
        data = _extract_json_object(html, "ytInitialData")
        if not data:
            raise RuntimeError("YouTube 页面未找到 ytInitialData")

        title = _channel_title(data)
        shelf = _find_album_shelf(data)
        if not shelf:
            return title, [], False

        albums: dict[str, AlbumCard] = {}
        _collect_albums(shelf, albums)
        if not albums:
            raise RuntimeError("已找到“专辑和单曲”区域，但没有解析到专辑卡片")

        token = _continuation_token(shelf.get("endpoint") or {})
        api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        client_version = version_match.group(1) if version_match else "2.20260925.00.00"
        if token and not api_key_match:
            raise RuntimeError("YouTube 页面存在专辑续页，但未找到 INNERTUBE_API_KEY")

        seen_tokens: set[str] = set()
        pages = 0
        while token and token not in seen_tokens and pages < max_continuations:
            pages += 1
            seen_tokens.add(token)
            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": client_version,
                        "hl": "zh-CN",
                        "gl": "US",
                    }
                },
                "continuation": token,
            }
            follow = client.post(
                f"https://www.youtube.com/youtubei/v1/browse?key={api_key_match.group(1)}",
                json=payload,
                headers={
                    "Origin": "https://www.youtube.com",
                    "Referer": str(response.url),
                    "X-Youtube-Client-Name": "1",
                    "X-Youtube-Client-Version": client_version,
                },
            )
            follow.raise_for_status()
            body = follow.json()
            _collect_albums(body, albums)
            token = _continuation_token(body)

        if token and pages >= max_continuations:
            raise RuntimeError(f"专辑分页超过安全上限 {max_continuations}，拒绝返回不完整结果")

        return title, list(albums.values()), True
