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
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
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


def walk_paths(node, path="$"):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk_paths(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_paths(value, f"{path}[{index}]")


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


def album_from_lockup(lockup: dict) -> tuple[str, str] | None:
    if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_ALBUM":
        return None
    meta = ((lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {})
    title = text_of(meta.get("title")).strip()
    playlist_id = str(lockup.get("contentId") or "").strip()
    playback = (((lockup.get("itemPlayback") or {}).get("inlinePlayerData") or {}).get("onSelect") or {}).get("innertubeCommand") or {}
    playlist_id = str((playback.get("watchEndpoint") or {}).get("playlistId") or playlist_id).strip()
    if not title or not playlist_id:
        return None
    return title, playlist_id


def collect_albums(node, albums: dict[str, str]) -> None:
    for obj in walk(node):
        lockup = obj.get("lockupViewModel") if isinstance(obj, dict) else None
        if not isinstance(lockup, dict):
            continue
        item = album_from_lockup(lockup)
        if item:
            title, playlist_id = item
            albums.setdefault(playlist_id, title)


def continuation_tokens(node) -> list[tuple[str, str]]:
    found = []
    for path, obj in walk_paths(node):
        command = obj.get("continuationCommand") if isinstance(obj, dict) else None
        if isinstance(command, dict) and command.get("token"):
            found.append((f"{path}.continuationCommand", str(command["token"])))
    return found


def browse_endpoints(node) -> list[tuple[str, dict]]:
    found = []
    for path, obj in walk_paths(node):
        endpoint = obj.get("browseEndpoint") if isinstance(obj, dict) else None
        if isinstance(endpoint, dict):
            found.append((f"{path}.browseEndpoint", endpoint))
    return found


def renderer_counter(node) -> Counter:
    counter = Counter()
    for _path, obj in walk_paths(node):
        if not isinstance(obj, dict):
            continue
        for key in obj:
            if key.endswith("Renderer") or key.endswith("ViewModel") or key.endswith("Action"):
                counter[key] += 1
    return counter


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

    albums: dict[str, str] = {}
    collect_albums(shelf, albums)
    print(f"INITIAL albums={len(albums)}")
    print("SHELF_KEYS", sorted(shelf.keys()))

    tokens = continuation_tokens(shelf)
    print(f"SHELF_TOKENS={len(tokens)}")
    for path, token in tokens:
        print(f"TOKEN_PATH {path} token_prefix={token[:80]}")

    endpoints = browse_endpoints(shelf)
    print(f"SHELF_BROWSE_ENDPOINTS={len(endpoints)}")
    for path, endpoint in endpoints:
        print(f"BROWSE_PATH {path} {json.dumps(endpoint, ensure_ascii=False, separators=(',', ':'))}")

    horizontal = ((shelf.get("content") or {}).get("horizontalListRenderer") or {})
    print("HORIZONTAL_KEYS", sorted(horizontal.keys()))
    print("ENDPOINT_JSON", json.dumps(shelf.get("endpoint") or {}, ensure_ascii=False, separators=(",", ":"))[:12000])

    for playlist_id, title in list(albums.items())[:12]:
        print(f"ALBUM {playlist_id} :: {title}")

    api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    client_version = version_match.group(1) if version_match else "2.20260925.00.00"

    if not api_key_match:
        raise SystemExit("NO_INNERTUBE_API_KEY")

    # Exercise every distinct continuation found on the shelf so we can see which one
    # actually expands the album collection. This is diagnostic only.
    seen_token_values = set()
    for token_no, (token_path, first_token) in enumerate(tokens, 1):
        if first_token in seen_token_values:
            continue
        seen_token_values.add(first_token)
        token = first_token
        local_albums = dict(albums)
        seen_pages = set()
        page = 0
        print(f"TRY_TOKEN {token_no} path={token_path}")
        while token and token not in seen_pages and page < 5:
            page += 1
            seen_pages.add(token)
            api_url = f"https://www.youtube.com/youtubei/v1/browse?key={api_key_match.group(1)}"
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
            headers = {
                "Origin": "https://www.youtube.com",
                "Referer": str(response.url),
                "X-Youtube-Client-Name": "1",
                "X-Youtube-Client-Version": client_version,
            }
            follow = client.post(api_url, json=payload, headers=headers)
            follow.raise_for_status()
            more = follow.json()
            before = len(local_albums)
            collect_albums(more, local_albums)
            next_tokens = continuation_tokens(more)
            print(
                f"TOKEN_RESULT token={token_no} page={page} new={len(local_albums)-before} "
                f"total={len(local_albums)} next_tokens={len(next_tokens)} top={sorted(more.keys())}"
            )
            if page == 1:
                print("RENDERERS", json.dumps(renderer_counter(more).most_common(30), ensure_ascii=False))
                for next_path, next_value in next_tokens[:10]:
                    print(f"NEXT_TOKEN_PATH {next_path} token_prefix={next_value[:80]}")
                for browse_path, browse in browse_endpoints(more)[:20]:
                    print(f"FOLLOW_BROWSE_PATH {browse_path} {json.dumps(browse, ensure_ascii=False, separators=(',', ':'))}")
            token = next_tokens[0][1] if next_tokens else ""

        print(f"TRY_TOKEN_DONE token={token_no} total={len(local_albums)}")

    # Current expected truth for this specific regression channel.
    print(f"VISIBLE_FIRST_PAGE={len(albums)} EXPECTED_TOTAL=40")
