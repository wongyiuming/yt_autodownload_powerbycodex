from __future__ import annotations

import json
import re
import sys
from collections import Counter

import httpx

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
    return "".join(str(x.get("text") or "") for x in value.get("runs") or [] if isinstance(x, dict))


def walk(node, path="root"):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, f"{path}[{i}]")


with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30.0, http2=True) as client:
    for suffix in ("", "/releases"):
        url = URL.rstrip("/") + suffix
        response = client.get(url, params={"hl": "zh-CN", "gl": "US"})
        print(f"FETCH {url} -> {response.status_code} {len(response.text)} bytes final={response.url}")
        data = extract_json_object(response.text, "ytInitialData")
        print(f"ytInitialData keys={list(data)[:20]}")
        if not data:
            for marker in ("var ytInitialData =", "window[\"ytInitialData\"] =", "ytInitialData ="):
                data = extract_json_object(response.text, marker)
                if data:
                    print(f"found with marker {marker!r}")
                    break
        if not data:
            print("NO_INITIAL_DATA")
            continue

        types = Counter()
        hits = []
        for path, obj in walk(data):
            for key in obj:
                if key.endswith("Renderer") or key.endswith("ViewModel"):
                    types[key] += 1
            playlist_id = obj.get("playlistId")
            browse_id = ((obj.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId")
            watch = (obj.get("navigationEndpoint") or {}).get("watchEndpoint") or {}
            if not playlist_id:
                playlist_id = watch.get("playlistId")
            title = text_of(obj.get("title")) or text_of(obj.get("headline"))
            if playlist_id or (isinstance(browse_id, str) and (browse_id.startswith("VL") or browse_id.startswith("MP"))):
                hits.append((path, title, playlist_id or "", browse_id or ""))

        print("TOP_RENDERERS")
        for name, count in types.most_common(25):
            print(f"  {count:4d} {name}")
        print(f"PLAYLIST/BROWSE HITS {len(hits)}")
        seen = set()
        for hit in hits:
            key = hit[2:] + (hit[1],)
            if key in seen:
                continue
            seen.add(key)
            print("HIT", json.dumps({"path": hit[0], "title": hit[1], "playlistId": hit[2], "browseId": hit[3]}, ensure_ascii=False))
        print("---")
