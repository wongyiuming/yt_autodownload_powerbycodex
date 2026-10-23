"""Cookie-free Kugou collection parsing and HTTP audio transfers.

The website exposes only the first 30 playlist entries. Resolve its public
``specialInfo.id`` and page the official collection endpoint instead; never
silently call that preview a complete playlist. Audio URLs are obtained from
the ordinary public playback endpoints. No yt-dlp or browser runtime is used.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlsplit
import uuid

import httpx

from platform_urls import classify_source


class KugouError(RuntimeError):
    """An unavailable resource or an incomplete/unsupported Kugou response."""


@dataclass(frozen=True)
class KugouTrack:
    id: str
    hash: str = ""
    album_id: str = ""
    title: str = ""
    artist: str = ""
    duration: float | None = None
    mixsongid: str = ""
    url: str = ""


@dataclass(frozen=True)
class KugouCollection:
    id: str
    title: str
    tracks: list[KugouTrack]


@dataclass(frozen=True)
class KugouMedia:
    url: str
    title: str
    ext: str = "mp3"
    duration: float | None = None
    filesize: int | None = None


_NOOP = lambda: None
_HASH = re.compile(r"[A-Fa-f0-9]{32}\Z")
_AUDIO_EXTS = {"mp3", "m4a", "mp4", "aac", "ogg", "opus", "flac", "wav"}
_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.kugou.com/"}
# Public Android client request format, also used by musicdl's Kugou provider.
# This is an API request checksum, not an account credential or a media key.
_PLAYLIST_SIGN_SEED = "OIlwieks28dk2k092lksi2UIkp"
_PLAYLIST_API = "https://gatewayretry.kugou.com/v2/get_other_list_file"


def _positive_number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if 0 < result < float("inf") else None


def _js_json(document: str, name: str):
    """Read a JSON literal after an assignment, without executing JavaScript."""
    match = re.search(r"\b(?:var|let|const)\s+" + re.escape(name) + r"\s*=\s*", document)
    if not match:
        raise KugouError(f"酷狗页面未提供 {name}，页面可能已失效或当前网络无法访问")
    try:
        value, _ = json.JSONDecoder().raw_decode(document[match.end():])
        return value
    except (ValueError, TypeError) as exc:
        raise KugouError(f"酷狗页面的 {name} 格式已变化") from exc


def _track(row: dict) -> KugouTrack:
    if not isinstance(row, dict):
        raise KugouError("酷狗歌单包含无效的歌曲记录")
    song_hash = str(row.get("hash") or row.get("HASH") or row.get("FileHash") or "").upper()
    mixsongid = str(row.get("mixsongid") or row.get("album_audio_id") or row.get("add_mixsongid") or "")
    if not _HASH.fullmatch(song_hash):
        raise KugouError("酷狗歌曲缺少有效的文件 hash，无法可靠下载")
    artist = str(row.get("author_name") or row.get("singername") or "")
    if not artist:
        artist = "、".join(str(s.get("name") or "") for s in row.get("singerinfo", []) if isinstance(s, dict))
    title = str(row.get("songname") or row.get("song_name") or row.get("audio_name") or row.get("name") or row.get("filename") or song_hash)
    if artist and not title.startswith(artist + " - "):
        title = f"{artist} - {title}"
    milliseconds = _positive_number(row.get("timelen") or row.get("timelength") or row.get("duration"))
    return KugouTrack(
        id=song_hash, hash=song_hash, album_id=str(row.get("album_id") or ""),
        title=unescape(title), artist=unescape(artist),
        duration=milliseconds / 1000 if milliseconds else None,
        mixsongid=mixsongid, url=str(row.get("song_url") or ""),
    )


class KugouClient:
    """Synchronous HTTP client; injected httpx clients make transport testable."""

    page_size = 100
    max_pages = 1000

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=httpx.Timeout(20, connect=10))
        self._owned = client is None
        self._mid = uuid.uuid4().hex

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        if self._owned:
            self.client.close()

    def _open(self, url: str, *, params=None, headers=None, stream=False,
              metadata=True, check_cancelled=_NOOP) -> httpx.Response:
        # Handle redirects explicitly: requests to metadata must stay on Kugou,
        # and cookies must not leak even when an injected client has a cookie jar.
        for _ in range(6):
            check_cancelled()
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise KugouError("酷狗返回了无效的 HTTP 地址")
            host = parsed.hostname.lower()
            if metadata and not (host == "kugou.com" or host.endswith(".kugou.com")):
                raise KugouError("酷狗元数据请求被重定向到非酷狗站点")
            request = self.client.build_request("GET", url, params=params,
                                                headers={**_BROWSER_HEADERS, **(headers or {})})
            request.headers.pop("cookie", None)
            try:
                response = self.client.send(request, stream=stream, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise KugouError(f"酷狗 HTTP 连接失败（{host}）：{type(exc).__name__}") from exc
            if response.is_redirect:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise KugouError("酷狗返回了缺少地址的重定向")
                url, params = urljoin(str(response.url), location), None
                continue
            if response.is_error:
                response.close()
                raise KugouError(f"酷狗 HTTP {response.status_code}（{host}），请检查网络或稍后重试")
            try:
                check_cancelled()
            except BaseException:
                response.close()
                raise
            return response
        raise KugouError("酷狗 HTTP 重定向次数过多")

    def _json(self, url: str, *, params=None, headers=None, check_cancelled=_NOOP) -> dict:
        with closing(self._open(url, params=params, headers=headers, check_cancelled=check_cancelled)) as response:
            try:
                payload = response.json()
            except ValueError as exc:
                raise KugouError("酷狗接口没有返回 JSON，可能遇到访问限制") from exc
        if not isinstance(payload, dict):
            raise KugouError("酷狗接口返回了无效的数据结构")
        return payload

    def resolve_collection(self, url: str, *, check_cancelled=_NOOP) -> KugouCollection:
        source = classify_source(url)
        if source.platform != "kugou":
            raise KugouError("该链接不是受支持的酷狗页面")
        check_cancelled()
        if source.kind == "song":
            if "/mixsong/" in source.url:
                with closing(self._open(source.url, check_cancelled=check_cancelled)) as response:
                    rows = _js_json(response.text, "dataFromSmarty")
                if not isinstance(rows, list) or len(rows) != 1:
                    raise KugouError("酷狗单曲页面没有返回一首有效歌曲")
                track = _track(rows[0])
            else:
                query = parse_qs(urlsplit(source.url).fragment)
                song_hash = query["hash"][0]
                track = KugouTrack(id=song_hash, hash=song_hash, album_id=(query.get("album_id") or [""])[0])
            return KugouCollection(track.id, track.title or "酷狗单曲", [track])

        collection_id = urlsplit(source.url).path.rsplit("/", 1)[-1].removesuffix(".html")
        with closing(self._open(source.url, check_cancelled=check_cancelled)) as response:
            info = _js_json(response.text, "specialInfo")
        if not isinstance(info, dict) or not re.fullmatch(r"[1-9][0-9]*", str(info.get("id") or "")):
            raise KugouError("酷狗页面没有提供有效的公开歌单 ID")
        playlist_id = str(info["id"])
        title = unescape(str(info.get("name") or f"酷狗歌单 {playlist_id}"))
        tracks: dict[str, KugouTrack] = {}
        seen_pages = set()
        consumed = 0
        expected_count = None
        for page in range(1, self.max_pages + 1):
            check_cancelled()
            params = {
                "specialid": playlist_id, "need_sort": 1, "module": "CloudMusic",
                "clientver": 11239, "pagesize": self.page_size, "specalidpgc": playlist_id,
                "userid": 0, "page": page, "type": 0, "area_code": 1, "appid": 1005,
            }
            material = "".join(sorted(f"{key}={value}" for key, value in params.items()))
            params["signature"] = hashlib.md5((_PLAYLIST_SIGN_SEED + material + _PLAYLIST_SIGN_SEED).encode()).hexdigest()
            payload = self._json(_PLAYLIST_API, params=params, headers={
                "User-Agent": "Android9-AndroidPhone-11239-18-0-playlist-wifi",
                "x-router": "pubsongscdn.kugou.com", "mid": self._mid,
                "dfid": "-", "clienttime": str(int(time.time())),
            }, check_cancelled=check_cancelled)
            data = payload.get("data")
            if str(payload.get("status")) != "1" or not isinstance(data, dict):
                detail = str(payload.get("errmsg") or payload.get("error") or payload.get("error_code") or "访问受限")[:160]
                raise KugouError(f"酷狗完整歌单读取失败：{detail}；不会把网页前 30 首当作完整歌单")
            rows = data.get("info")
            count = data.get("count")
            if not isinstance(rows, list) or not re.fullmatch(r"[0-9]+", str(count)):
                raise KugouError("酷狗歌单分页缺少曲目或总数，无法确认完整性")
            count = int(count)
            if expected_count is not None and expected_count != count:
                raise KugouError("酷狗歌单在读取期间发生变化，请重新添加或重试")
            expected_count = count
            if data.get("page") is not None and str(data["page"]) != str(page):
                raise KugouError("酷狗接口返回了错误的分页，未完成整个歌单")
            page_tracks = [_track(row) for row in rows]
            fingerprint = tuple(track.id for track in page_tracks)
            if fingerprint in seen_pages:
                raise KugouError("酷狗接口重复返回同一页，未完成整个歌单")
            seen_pages.add(fingerprint)
            for track in page_tracks:
                tracks.setdefault(track.id, track)
            consumed += len(rows)
            if consumed > count:
                raise KugouError("酷狗歌单曲目数量超过接口总数，无法确认完整性")
            if consumed == count:
                return KugouCollection(collection_id, title, list(tracks.values()))
            if not rows:
                raise KugouError(f"酷狗歌单提前结束（{consumed}/{count}），请重试")
        raise KugouError("酷狗歌单超过分页安全上限，未完成整个歌单")

    @staticmethod
    def _media(payload: dict, track: KugouTrack, *, mobile: bool) -> KugouMedia:
        data = payload if mobile else payload.get("data")
        if not isinstance(data, dict):
            raise KugouError("酷狗播放接口没有返回歌曲数据")
        url = data.get("url" if mobile else "play_url")
        if not isinstance(url, str) or not url:
            detail = str(data.get("error") or payload.get("error") or payload.get("err_code") or "登录、会员、版权或地区限制")[:160]
            raise KugouError(f"酷狗没有提供可播放的音频直链：{detail}")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise KugouError("酷狗没有提供有效的 HTTP 音频直链")
        ext = str(data.get("extName" if mobile else "extname") or Path(parsed.path).suffix.lstrip(".")).lower()
        if ext not in _AUDIO_EXTS:
            raise KugouError("酷狗返回的资源不是支持的明文音频文件")
        duration = _positive_number(data.get("timeLength" if mobile else "timelength"))
        if duration and not mobile:
            duration /= 1000
        full_duration = track.duration
        extra = data.get("extra") or {}
        if not full_duration and isinstance(extra, dict):
            milliseconds = _positive_number(extra.get("128timelength") or extra.get("320timelength"))
            full_duration = milliseconds / 1000 if milliseconds else None
        is_preview = any(str(data.get(key, "")).lower() in {"1", "true"} for key in
                         ("is_free_part", "isFreePart", "is_trial", "isTrial", "trial"))
        if is_preview or (duration and full_duration and duration + 3 < full_duration * 0.95):
            raise KugouError("酷狗只返回了试听片段，不能当作完整歌曲下载")
        filesize = _positive_number(data.get("fileSize" if mobile else "filesize"))
        title = str(data.get("fileName" if mobile else "audio_name") or track.title or track.id)
        return KugouMedia(url, unescape(title), ext, duration or full_duration, int(filesize) if filesize else None)

    def resolve_track(self, track: KugouTrack, *, check_cancelled=_NOOP) -> KugouMedia:
        if not _HASH.fullmatch(track.hash):
            raise KugouError("酷狗歌曲缺少有效的 hash")
        attempts = [
            ("https://m.kugou.com/app/i/getSongInfo.php", {"cmd": "playInfo", "hash": track.hash}, True),
            ("https://wwwapi.kugou.com/yy/index.php", {
                "r": "play/getdata", "hash": track.hash, "album_id": track.album_id or "0", "mid": self._mid,
            }, False),
        ]
        failures = []
        for url, params, mobile in attempts:
            check_cancelled()
            try:
                payload = self._json(url, params=params, check_cancelled=check_cancelled)
                return self._media(payload, track, mobile=mobile)
            except KugouError as exc:
                failures.append(str(exc))
        raise KugouError("；".join(dict.fromkeys(failures)))

    def download(self, media: KugouMedia, destination: Path, *,
                 progress: Callable[[dict], None] | None = None, check_cancelled=_NOOP) -> Path:
        """Stream a media URL to a temporary file and atomically publish it."""
        destination = Path(destination)
        if destination.exists():
            raise KugouError("酷狗下载目标已经存在，拒绝覆盖")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        started, downloaded = time.monotonic(), 0
        try:
            with closing(self._open(media.url, headers={"Accept-Encoding": "identity"}, stream=True,
                                    metadata=False, check_cancelled=check_cancelled)) as response:
                if response.status_code != 200:
                    raise KugouError("酷狗音频响应不是完整文件")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type.startswith("text/") or content_type in {"application/json", "application/xml"}:
                    raise KugouError("酷狗音频地址返回了错误页面，而不是音频")
                length = _positive_number(response.headers.get("content-length"))
                total = int(length) if length and response.headers.get("content-encoding", "identity") == "identity" else None
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        check_cancelled()
                        output.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(time.monotonic() - started, 0.001)
                        speed = downloaded / elapsed
                        if progress:
                            progress({"status": "downloading", "downloaded_bytes": downloaded,
                                      "total_bytes": total, "speed": speed,
                                      "eta": max(0, (total - downloaded) / speed) if total else None})
                check_cancelled()
                # Content-Length describes the HTTP representation; the playback
                # API's filesize describes the decoded media file. iter_bytes()
                # decodes HTTP content encoding, so these are separate checks.
                # A valid-but-short MP3 can still be decoded by FFmpeg, making
                # comparison with the playback metadata necessary before archive.
                if (not downloaded or (total is not None and downloaded != total)
                        or (media.filesize is not None and downloaded != media.filesize)):
                    raise KugouError("酷狗音频下载不完整，已丢弃临时文件")
            if destination.exists():
                raise KugouError("酷狗下载目标已经存在，拒绝覆盖")
            temporary.replace(destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        if progress:
            progress({"status": "finished", "downloaded_bytes": downloaded,
                      "total_bytes": downloaded, "speed": None, "eta": 0})
        return destination
