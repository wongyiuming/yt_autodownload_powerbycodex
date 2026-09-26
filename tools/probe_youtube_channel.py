from __future__ import annotations

import json
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


def walk(node, path="root"):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, f"{path}[{i}]")


def find_shelves(data: dict):
    for path, obj in walk(data):
        shelf = obj.get("shelfRenderer") if isinstance(obj, dict) else None
        if isinstance(shelf, dict):
            yield path + ".shelfRenderer", shelf


def summarize_lockup(item: dict) -> dict:
    lockup = item.get("lockupViewModel") or {}
    meta = ((lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {})
    title = text_of(meta.get("title"))
    content_id = str(lockup.get("contentId") or "")
    playback = (((lockup.get("itemPlayback") or {}).get("inlinePlayerData") or {}).get("onSelect") or {}).get("innertubeCommand") or {}
    watch = playback.get("watchEndpoint") or {}
    nav = lockup.get("rendererContext") or {}
    return {
        "title": title,
        "contentId": content_id,
        "contentType": lockup.get("contentType"),
        "playlistId": watch.get("playlistId") or content_id,
        "videoId": watch.get("videoId") or "",
        "metadataKeys": list(meta.keys()),
        "rendererContextKeys": list(nav.keys()),
    }


with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30.0, http2=True) as client:
    response = client.get(URL, params={"hl": "zh-CN", "gl": "US"})
    print(f"FETCH {URL} -> {response.status_code} {len(response.text)} bytes final={response.url}")
    data = extract_json_object(response.text, "ytInitialData")
    print(f"ytInitialData keys={list(data)[:20]}")
    if not data:
        raise SystemExit("NO_INITIAL_DATA")

    types = Counter()
    for _path, obj in walk(data):
        for key in obj:
            if key.endswith("Renderer") or key.endswith("ViewModel"):
                types[key] += 1
    print("TOP_RENDERERS")
    for name, count in types.most_common(25):
        print(f"  {count:4d} {name}")

    shelves = list(find_shelves(data))
    print(f"SHELVES {len(shelves)}")
    for shelf_no, (path, shelf) in enumerate(shelves, 1):
        title = text_of(shelf.get("title"))
        endpoint = shelf.get("endpoint") or {}
        print("SHELF", json.dumps({
            "no": shelf_no,
            "path": path,
            "title": title,
            "endpoint": endpoint,
            "keys": list(shelf.keys()),
        }, ensure_ascii=False))
        items = ((shelf.get("content") or {}).get("horizontalListRenderer") or {}).get("items") or []
        print(f"SHELF_ITEMS {len(items)}")
        for i, item in enumerate(items, 1):
            if isinstance(item, dict) and "lockupViewModel" in item:
                print("ALBUM", i, json.dumps(summarize_lockup(item), ensure_ascii=False))

    print("BROWSE_ENDPOINTS_WITH_PARAMS")
    seen = set()
    for path, obj in walk(data):
        browse = obj.get("browseEndpoint") if isinstance(obj, dict) else None
        if not isinstance(browse, dict) or not browse.get("params"):
            continue
        key = (str(browse.get("browseId") or ""), str(browse.get("params") or ""))
        if key in seen:
            continue
        seen.add(key)
        command_url = (((obj.get("commandMetadata") or {}).get("webCommandMetadata") or {}).get("url") or "")
        print("BROWSE", json.dumps({"path": path, "browseId": key[0], "params": key[1], "url": command_url}, ensure_ascii=False))
