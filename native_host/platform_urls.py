"""Recognize supported source pages before handing them to a downloader.

Keep this normalization in sync with extension/platform_urls.js. Both runtimes
exercise the same fixtures so the popup cannot offer an unsupported source.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit


@dataclass(frozen=True)
class Source:
    platform: str
    kind: str
    url: str


_NUMBER = re.compile(r"[1-9][0-9]*\Z")
_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_BILIBILI_VIDEO = re.compile(r"(?i:BV)[A-Za-z0-9]{10}|(?i:av)[1-9][0-9]*")
_KUGOU_COLLECTION = re.compile(r"(?:[1-9][0-9]*|collection_(?:[0-9]+_){3}[0-9]+)\Z")


def _first(query: dict, name: str) -> str:
    return (query.get(name) or [""])[0]


def classify_source(raw: str) -> Source:
    """Return a canonical, platform-specific URL or raise ``ValueError``.

An explicit Bilibili ``p=`` remains a single part. A favorites/list URL is a
whole collection even when the page's query includes a currently playing bvid.
"""
    if not isinstance(raw, str):
        raise ValueError("URL 必须是字符串")
    value = raw.strip()
    if not value or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("URL 无效，请粘贴完整的 http/https 页面地址")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("仅支持 http/https URL")
    # Do not let browser URL coercion, userinfo, or custom ports change the host.
    if parsed.username is not None or parsed.password is not None or "%" in parsed.netloc:
        raise ValueError("URL 不允许包含账号、密码或编码域名")
    if parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
        raise ValueError("不支持自定义端口")
    if re.search(r"(?:^|/)(?:\.|%2e){1,2}(?=/|$)", parsed.path, re.I):
        raise ValueError("URL 路径无效")
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query, keep_blank_values=True)

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parts[0] if parts else ""
        if _YOUTUBE_ID.fullmatch(video_id):
            return Source("youtube", "video", f"https://www.youtube.com/watch?v={video_id}")
        raise ValueError("YouTube 短链接缺少视频 ID")

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        list_id = _first(query, "list")
        if path in {"/playlist", "/watch"} and _YOUTUBE_ID.fullmatch(list_id):
            return Source("youtube", "playlist", f"https://www.youtube.com/playlist?list={list_id}")
        video_id = _first(query, "v")
        if path == "/watch" and _YOUTUBE_ID.fullmatch(video_id):
            return Source("youtube", "video", f"https://www.youtube.com/watch?v={video_id}")
        if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"} and _YOUTUBE_ID.fullmatch(parts[1]):
            return Source("youtube", "video", f"https://www.youtube.com/watch?v={parts[1]}")
        root = parts[:1] if parts and parts[0].startswith("@") and len(parts[0]) > 1 else None
        if len(parts) >= 2 and parts[0] in {"channel", "c", "user"}:
            root = parts[:2]
        if root:
            return Source("youtube", "channel", "https://www.youtube.com/" + quote("/".join(root), safe="/@%"))
        raise ValueError("请打开 YouTube 视频、播放列表或公开频道")

    if host == "b23.tv":
        if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9]+", parts[0]):
            return Source("bilibili", "shortlink", f"https://b23.tv/{parts[0]}")
        raise ValueError("bilibili 短链接缺少有效 ID")

    if host == "space.bilibili.com":
        if len(parts) >= 2 and _NUMBER.fullmatch(parts[0]):
            mid = parts[0]
            fid, sid = _first(query, "fid"), _first(query, "sid")
            if parts[1:] == ["favlist"] and _NUMBER.fullmatch(fid):
                return Source("bilibili", "favorites", f"https://www.bilibili.com/medialist/detail/ml{fid}")
            if len(parts) == 3 and parts[1] == "lists" and _NUMBER.fullmatch(parts[2]):
                list_type = _first(query, "type") or "season"
                if list_type in {"season", "series"}:
                    return Source("bilibili", "collection", f"https://space.bilibili.com/{mid}/lists/{parts[2]}?type={list_type}")
            if parts[1:] in (["channel", "collectiondetail"], ["channel", "seriesdetail"]) and _NUMBER.fullmatch(sid):
                list_type = "season" if parts[2] == "collectiondetail" else "series"
                return Source("bilibili", "collection", f"https://space.bilibili.com/{mid}/lists/{sid}?type={list_type}")
        raise ValueError("请打开具体的 bilibili 收藏夹（含 fid）或合集页面")

    if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        favorites = re.fullmatch(r"/(?:medialist/(?:detail|play)|list)/ml([1-9][0-9]*)", path)
        if favorites:
            return Source("bilibili", "favorites", f"https://www.bilibili.com/medialist/detail/ml{favorites[1]}")
        if len(parts) == 2 and parts[0] == "list" and _NUMBER.fullmatch(parts[1]) and _NUMBER.fullmatch(_first(query, "sid")):
            # This player URL supplies its own list metadata; keep sid and drop
            # the current item/cursor so downloading always starts at the list.
            # Preserve otype when supplied; the page can use it to distinguish
            # collection types whose numeric IDs overlap.
            params = {"sid": _first(query, "sid")}
            if "otype" in query:
                if not re.fullmatch(r"[0-9]+", _first(query, "otype")):
                    raise ValueError("bilibili 集合类型无效")
                params["otype"] = _first(query, "otype")
            return Source("bilibili", "collection", f"https://www.bilibili.com/list/{parts[1]}?{urlencode(params)}")
        if len(parts) == 3 and parts[:2] == ["medialist", "play"] and _NUMBER.fullmatch(parts[2]):
            sid = _first(query, "business_id")
            business = _first(query, "business")
            if _NUMBER.fullmatch(sid) and business in {"space_series", "space_collection"}:
                list_type = "series" if business == "space_series" else "season"
                return Source("bilibili", "collection", f"https://space.bilibili.com/{parts[2]}/lists/{sid}?type={list_type}")
        if len(parts) == 2 and parts[0] == "video" and _BILIBILI_VIDEO.fullmatch(parts[1]):
            video_id = ("BV" if parts[1][:2].lower() == "bv" else "av") + parts[1][2:]
            part = _first(query, "p")
            if "p" in query and not _NUMBER.fullmatch(part):
                raise ValueError("bilibili 分 P 编号必须是正整数")
            return Source("bilibili", "video", f"https://www.bilibili.com/video/{video_id}" + (f"?p={part}" if part else ""))
        raise ValueError("请打开 bilibili 视频、收藏夹或合集页面")

    if host in {"kugou.com", "www.kugou.com", "m.kugou.com"}:
        mixsong = re.fullmatch(r"/mixsong/([A-Za-z0-9]+)\.html", path)
        if mixsong:
            return Source("kugou", "song", f"https://www.kugou.com/mixsong/{mixsong[1]}.html")
        special = re.fullmatch(r"/yy/special/single/([^/]+)\.html", path)
        mobile = re.fullmatch(r"/plist/list/([1-9][0-9]*)", path) if host == "m.kugou.com" else None
        collection_id = special[1] if special else mobile[1] if mobile else ""
        if _KUGOU_COLLECTION.fullmatch(collection_id):
            return Source("kugou", "playlist", f"https://www.kugou.com/yy/special/single/{collection_id}.html")
        if path == "/song" or (path == "/yy/index.php" and _first(query, "r") == "play/getdata"):
            song_query = {**query, **parse_qs(parsed.fragment, keep_blank_values=True)}
            song_hash = _first(song_query, "hash")
            album_id = _first(song_query, "album_id")
            if re.fullmatch(r"[A-Fa-f0-9]{32}", song_hash):
                if album_id and not re.fullmatch(r"[0-9]+", album_id):
                    raise ValueError("酷狗 album_id 必须是数字")
                params = {"hash": song_hash.upper()}
                if album_id:
                    params["album_id"] = album_id
                return Source("kugou", "song", "https://www.kugou.com/song/#" + urlencode(params))
        raise ValueError("请打开酷狗歌单，或包含 hash 的歌曲播放页")

    raise ValueError("当前支持 YouTube、bilibili 和酷狗音乐的指定视频、歌曲与集合页面")
