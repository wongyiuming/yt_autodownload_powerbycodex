from __future__ import annotations

import json
import re
import sys
from collections import Counter

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/channel/UC5QL_gYA1VzTkcIjwD4cxfA"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}


def extract_json_object(text: str, marker: str) -> dict:
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
                return json.loads(text[pos:i + 1])
    return {}


def text_of(value) -> str:
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"]
    if isinstance(value.get("content"), str):
        return value["content"]
    return "".join(str(x.get("text") or "") for x in value.get("runs") or [] if isinstance(x, dict))


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def find_album_shelf(data: dict) -> dict:
    fallback = {}
    for obj in walk(data):
        shelf = obj.get("shelfRenderer") if isinstance(obj, dict) else None
        if not isinstance(shelf, dict):
            continue
        title = text_of(shelf.get("title")).strip().casefold()
        items = ((shelf.get("content") or {}).get("horizontalListRenderer") or {}).get("items") or []
        has_album = any(
            isinstance(item, dict)
            and (item.get("lockupViewModel") or {}).get("contentType") == "LOCKUP_CONTENT_TYPE_ALBUM"
            for item in items
        )
        if title in {"专辑和单曲", "專輯和單曲", "albums & singles", "albums and singles"}:
            return shelf
        if has_album:
            fallback = shelf
    return fallback


def continuation_token(node) -> str:
    for obj in walk(node):
        command = obj.get("continuationCommand") if isinstance(obj, dict) else None
        if isinstance(command, dict) and command.get("token"):
            return str(command["token"])
    return ""


def renderer_counts(node) -> Counter:
    result = Counter()
    for obj in walk(node):
        if not isinstance(obj, dict):
            continue
        for key in obj:
            if key.endswith("Renderer") or key.endswith("ViewModel"):
                result[key] += 1
    return result


def endpoint_ids(node: dict) -> tuple[list[str], list[str]]:
    browse_ids = []
    playlist_ids = []
    for obj in walk(node):
        if not isinstance(obj, dict):
            continue
        browse = obj.get("browseEndpoint")
        if isinstance(browse, dict) and browse.get("browseId"):
            browse_ids.append(str(browse["browseId"]))
        watch = obj.get("watchEndpoint")
        if isinstance(watch, dict) and watch.get("playlistId"):
            playlist_ids.append(str(watch["playlistId"]))
    return sorted(set(browse_ids)), sorted(set(playlist_ids))


def print_grid_items(body: dict, page: int) -> None:
    items = []
    for obj in walk(body):
        grid = obj.get("gridRenderer") if isinstance(obj, dict) else None
        if isinstance(grid, dict) and isinstance(grid.get("items"), list):
            items = grid["items"]
            break
    print(f"PAGE {page} GRID_ITEMS={len(items)}")
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            print(f"ITEM {page}.{index:02d} NON_DICT")
            continue
        renderer_keys = [key for key in item if key.endswith("Renderer") or key.endswith("ViewModel")]
        print(f"ITEM {page}.{index:02d} TYPES={renderer_keys}")
        for key in renderer_keys:
            renderer = item.get(key)
            if not isinstance(renderer, dict):
                continue
            title = text_of(renderer.get("title")).strip()
            browse_ids, watch_playlist_ids = endpoint_ids(renderer)
            direct_playlist = str(renderer.get("playlistId") or "")
            video_id = str(renderer.get("videoId") or "")
            content_id = str(renderer.get("contentId") or "")
            print(
                f"  {key} title={title!r} direct_playlist={direct_playlist!r} "
                f"video_id={video_id!r} content_id={content_id!r} "
                f"browse_ids={browse_ids} watch_playlist_ids={watch_playlist_ids}"
            )


with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30.0, http2=True) as client:
    response = client.get(URL, params={"hl": "zh-CN", "gl": "US"})
    response.raise_for_status()
    html = response.text
    data = extract_json_object(html, "ytInitialData")
    if not data:
        raise SystemExit("NO_INITIAL_DATA")
    shelf = find_album_shelf(data)
    if not shelf:
        raise SystemExit("NO_ALBUM_SHELF")

    initial_items = ((shelf.get("content") or {}).get("horizontalListRenderer") or {}).get("items") or []
    print(f"INITIAL_ITEMS={len(initial_items)}")

    token = continuation_token(shelf.get("endpoint") or {})
    if not token:
        raise SystemExit("NO_ALBUM_PANEL_TOKEN")

    api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    if not api_key_match:
        raise SystemExit("NO_INNERTUBE_API_KEY")
    client_version = version_match.group(1) if version_match else "2.20260925.00.00"

    seen = set()
    page = 0
    while token and token not in seen and page < 10:
        page += 1
        seen.add(token)
        follow = client.post(
            f"https://www.youtube.com/youtubei/v1/browse?key={api_key_match.group(1)}",
            json={
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": client_version,
                        "hl": "zh-CN",
                        "gl": "US",
                    }
                },
                "continuation": token,
            },
            headers={
                "Origin": "https://www.youtube.com",
                "Referer": str(response.url),
                "X-Youtube-Client-Name": "1",
                "X-Youtube-Client-Version": client_version,
            },
        )
        follow.raise_for_status()
        body = follow.json()
        print(f"PAGE {page} RENDERERS={json.dumps(renderer_counts(body).most_common(20), ensure_ascii=False)}")
        print_grid_items(body, page)
        token = continuation_token(body)
        print(f"PAGE {page} NEXT={bool(token)}")
