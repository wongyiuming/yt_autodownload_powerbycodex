from __future__ import annotations

import json
import re
import sys

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
    for obj in walk(data):
        shelf = obj.get("shelfRenderer") if isinstance(obj, dict) else None
        if not isinstance(shelf, dict):
            continue
        title = text_of(shelf.get("title")).strip().casefold()
        if title in {"专辑和单曲", "專輯和單曲", "albums & singles", "albums and singles"}:
            return shelf
    return {}


def continuation_token(node) -> str:
    for obj in walk(node):
        command = obj.get("continuationCommand") if isinstance(obj, dict) else None
        if isinstance(command, dict) and command.get("token"):
            return str(command["token"])
    return ""


def grid_playlists(node) -> list[tuple[str, str]]:
    found = []
    for obj in walk(node):
        renderer = obj.get("gridPlaylistRenderer") if isinstance(obj, dict) else None
        if not isinstance(renderer, dict):
            continue
        title = text_of(renderer.get("title")).strip()
        playlist_id = str(renderer.get("playlistId") or "").strip()
        if title and playlist_id:
            found.append((playlist_id, title))
    return found


def run_case(label: str, params: dict[str, str], hl: str, gl: str) -> None:
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30.0, http2=True) as client:
        response = client.get(URL, params=params)
        response.raise_for_status()
        html = response.text
        data = extract_json_object(html, "ytInitialData")
        if not data:
            print(f"CASE {label}: NO_INITIAL_DATA")
            return
        shelf = find_album_shelf(data)
        if not shelf:
            print(f"CASE {label}: NO_SHELF")
            return
        token = continuation_token(shelf.get("endpoint") or {})
        api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        client_version = version_match.group(1) if version_match else "2.20260925.00.00"
        if not token or not api_key_match:
            print(f"CASE {label}: NO_TOKEN_OR_KEY")
            return

        albums: dict[str, str] = {}
        page = 0
        seen = set()
        while token and token not in seen and page < 10:
            page += 1
            seen.add(token)
            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": client_version,
                        "hl": hl,
                        "gl": gl,
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
            page_items = grid_playlists(body)
            for pid, title in page_items:
                albums.setdefault(pid, title)
            print(f"CASE {label}: page={page} items={len(page_items)} unique={len(albums)} next={bool(continuation_token(body))}")
            token = continuation_token(body)
        print(f"CASE {label}: TOTAL={len(albums)}")
        if len(albums) >= 38:
            for index, (pid, title) in enumerate(albums.items(), 1):
                print(f"CASE {label} ALBUM {index:02d} {pid} :: {title}")


cases = [
    ("default", {}, "en", "US"),
    ("US", {"hl": "zh-CN", "gl": "US"}, "zh-CN", "US"),
    ("HK", {"hl": "zh-HK", "gl": "HK"}, "zh-HK", "HK"),
    ("TW", {"hl": "zh-TW", "gl": "TW"}, "zh-TW", "TW"),
    ("SG", {"hl": "zh-CN", "gl": "SG"}, "zh-CN", "SG"),
    ("GB", {"hl": "en-GB", "gl": "GB"}, "en-GB", "GB"),
]
for case in cases:
    try:
        run_case(*case)
    except Exception as exc:
        print(f"CASE {case[0]}: ERROR {type(exc).__name__}: {exc}")
