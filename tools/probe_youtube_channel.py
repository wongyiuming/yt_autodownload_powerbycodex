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


def continuation_token(node) -> str:
    for obj in walk(node):
        command = obj.get("continuationCommand") if isinstance(obj, dict) else None
        if isinstance(command, dict) and command.get("token"):
            return str(command["token"])
    return ""


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
    token = continuation_token(shelf.get("endpoint") or {})
    print(f"INITIAL albums={len(albums)} continuation={bool(token)}")
    for playlist_id, title in list(albums.items())[:12]:
        print(f"ALBUM {playlist_id} :: {title}")

    api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    if token and not api_key_match:
        raise SystemExit("NO_INNERTUBE_API_KEY")
    client_version = version_match.group(1) if version_match else "2.20260925.00.00"

    seen_tokens = set()
    page = 0
    while token and token not in seen_tokens and page < 20:
        page += 1
        seen_tokens.add(token)
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
        before = len(albums)
        collect_albums(more, albums)
        token = continuation_token(more)
        print(f"CONT page={page} new={len(albums)-before} total={len(albums)} next={bool(token)}")

    print(f"TOTAL_ALBUMS={len(albums)}")
    for playlist_id, title in albums.items():
        print(f"FINAL {playlist_id} :: {title}")

    expected = {"石頭記", "TM+M DECADE(NCE)", "Shen Jing"}
    found = set(albums.values())
    missing = expected - found
    if missing:
        raise SystemExit(f"MISSING_EXPECTED={sorted(missing)}")
    if len(albums) < 12:
        raise SystemExit(f"TOO_FEW_ALBUMS={len(albums)}")
