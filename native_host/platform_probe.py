"""Read-only, credential-free diagnostics for every supported platform."""

from __future__ import annotations

import os
from pathlib import Path

from host_legacy import HostLogger
from platform_urls import classify_source
from youtube_runtime import extract_with_fallback, youtube_options


def probe_source(raw_url: str, node_path: str | None) -> dict:
    source = classify_source(raw_url)
    if source.platform == "kugou":
        from kugou import KugouClient
        with KugouClient() as client:
            collection = client.resolve_collection(source.url)
            result = {"platform": "kugou", "backend": "httpx", "id": collection.id,
                      "title": collection.title, "item_count": len(collection.tracks)}
            if source.kind == "song":
                media = client.resolve_track(collection.tracks[0])
                result.update({"media_available": True, "format_count": 1, "title": media.title})
            return result

    options = {
        "quiet": True, "no_warnings": False, "skip_download": True,
        "noplaylist": source.kind == "video" and source.platform == "youtube",
        "verbose": True, "logger": HostLogger(), "socket_timeout": 20,
        "no_plugins": True,
    }
    if source.kind != "video":
        options["extract_flat"] = "in_playlist"
    if source.platform == "youtube":
        options.update(youtube_options(node_path, Path(os.devnull)))
        info = extract_with_fallback(options, source.url)
    else:
        import yt_dlp
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source.url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("平台未返回有效的来源信息")
    result = {"platform": source.platform, "backend": "yt-dlp", "id": info.get("id"),
              "title": info.get("title"), "format_count": len(info.get("formats") or [])}
    if "entries" in info:
        result["item_count"] = len(list(info["entries"] or []))
    return result
