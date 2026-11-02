"""Read the signed-in Kugou web user's complete playlist library.

Only the two read-only cloud-list requests carry an in-memory web token. Public
playback and audio transfers continue to use :class:`KugouClient` without cookies.
The H5 signing format and cookie fields match Kugou's ``infSign.min.js`` and
``getBaseInfo.min.js``; see README for the endpoint evidence and live-test limit.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
from html import unescape
import json
import re
import time
from urllib.parse import unquote

import httpx

from kugou import KugouClient, KugouCollection, KugouError, KugouTrack, _NOOP, _track


_GATEWAY = "https://gateway.kugou.com"
_LISTS_PATH = "/v7/get_all_list"
_TRACKS_PATH = "/v4/get_list_all_file"
# Public H5 request checksum seed, not an account credential or media key.
_H5_SIGN_SEED = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
_POSITIVE_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_GLOBAL_ID = re.compile(r"collection_[0-9]+_[0-9]+_([1-9][0-9]*)_[0-9]+\Z")
_COOKIE_NAMES = {"KuGoo", "kg_mid", "kg_dfid", "KugooID", "userid", "token", "a_id"}


class KugouAuthError(KugouError):
    """The current browser login is absent, expired, or unsuitable for H5."""


@dataclass(frozen=True)
class KugouSession:
    user_id: str
    token: str = field(repr=False)
    mid: str = ""
    dfid: str = "-"
    appid: int = 1014

    def __post_init__(self):
        if (not isinstance(self.user_id, str) or not _POSITIVE_ID.fullmatch(self.user_id)
                or not _safe_value(self.token, maximum=4096)
                or not isinstance(self.mid, str) or (self.mid and not _safe_value(self.mid, maximum=256))
                or not _safe_value(self.dfid, maximum=256)
                or type(self.appid) is not int or self.appid not in {1010, 1014}):
            raise KugouAuthError("酷狗网页登录信息不完整或已失效，请在酷狗主站重新登录后重试")


@dataclass(frozen=True)
class KugouPlaylist:
    id: str
    title: str
    count: int | None = None
    url: str = ""
    list_id: str = ""
    list_type: int = 0


def _safe_value(value, *, maximum: int) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and all(ord(char) > 32 and ord(char) != 127 for char in value))


def session_from_cookies(cookies: list[dict]) -> KugouSession:
    """Extract only H5 session fields; never retain the browser's cookie jar.

    ``KuGoo`` is itself an ampersand-separated cookie containing ``KugooID``,
    ``t`` and ``a_id``. A whole-cookie URL-encoded representation is accepted.
    Host-only cookies from other Kugou applications are deliberately ignored.
    """
    if not isinstance(cookies, list) or len(cookies) > 1000:
        raise KugouAuthError("未收到有效的酷狗网页登录信息，请在酷狗主站登录后重试")
    values: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name, value = cookie.get("name"), cookie.get("value")
        if domain not in {"kugou.com", "www.kugou.com"} or name not in _COOKIE_NAMES:
            continue
        if not isinstance(value, str) or len(value) > 16384:
            raise KugouAuthError("酷狗网页登录信息格式无效，请重新登录")
        expiry = cookie.get("expirationDate")
        if isinstance(expiry, (int, float)) and expiry <= time.time():
            continue
        if name in values and values[name] != value:
            raise KugouAuthError("检测到冲突的酷狗网页登录信息，请在当前浏览器重新登录")
        values[name] = value

    compound: dict[str, str] = {}
    raw = values.get("KuGoo", "")
    # Preserve literal '+' in token values; this is not form-urlencoded data.
    if "=" not in raw:
        raw = unquote(raw)
    for item in raw.split("&"):
        key, separator, value = item.partition("=")
        if separator and key in {"KugooID", "t", "a_id"}:
            if key in compound and compound[key] != unquote(value):
                raise KugouAuthError("酷狗网页登录信息格式无效，请重新登录")
            compound[key] = unquote(value)

    user_id = compound.get("KugooID") or values.get("KugooID") or values.get("userid") or ""
    token = compound.get("t") or values.get("token") or ""
    appid = compound.get("a_id") or values.get("a_id") or "1014"
    if appid not in {"1010", "1014"}:
        raise KugouAuthError("当前酷狗登录信息不属于网页版，请在酷狗主站重新登录")
    return KugouSession(user_id, token, values.get("kg_mid", ""), values.get("kg_dfid") or "-", int(appid))


def _integer(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return int(value) if re.fullmatch(r"[0-9]{1,12}", str(value)) else None


def _first(row: dict, *names, default=""):
    for name in names:
        if row.get(name) is not None and row[name] != "":
            return row[name]
    return default


def _row_id(row: dict) -> str:
    if not isinstance(row, dict):
        raise KugouError("酷狗个人歌单包含无效的记录")
    list_id = str(_first(row, "listid", "list_id"))
    if not _POSITIVE_ID.fullmatch(list_id):
        raise KugouError("酷狗个人歌单缺少有效的云歌单 ID，无法确认完整性")
    # A collected entry has a user-local ID and separate creator/global IDs;
    # never require those namespaces to have the same numeric component.
    return list_id


def _library_track(row: dict, *, position: int) -> KugouTrack:
    if not isinstance(row, dict):
        raise KugouError("酷狗个人歌单包含无效的歌曲记录")
    normalized = {
        "hash": _first(row, "hash", "HASH", "FileHash", "filehash", "Hash"),
        "album_id": _first(row, "album_id", "AlbumID"),
        "mixsongid": _first(row, "mixsongid", "album_audio_id", "add_mixsongid", "MixSongID", "AlbumAudioID"),
        "name": _first(row, "songname", "song_name", "audio_name", "SongName", "name", "filename"),
        "author_name": _first(row, "author_name", "singername", "SingerName"),
        "singerinfo": row.get("singerinfo") if isinstance(row.get("singerinfo"), list) else [],
    }
    milliseconds = _first(row, "timelen", "timelength", "duration_ms", default=None)
    if milliseconds is None:
        seconds = _first(row, "duration", "Duration", default=None)
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            milliseconds = seconds * 1000 if seconds < 10000 else seconds
    normalized["timelen"] = milliseconds
    song_hash = str(normalized["hash"]).upper()
    if not re.fullmatch(r"[A-F0-9]{32}", song_hash):
        # Keep unavailable/cloud-only records in their original position. The
        # existing downloader will reject the empty hash for this one song and
        # continue the rest of the playlist, rather than discarding the list.
        return KugouTrack(id=f"unavailable:{position}", title=unescape(str(normalized["name"] or "不可用歌曲")),
                          artist=unescape(str(normalized["author_name"] or "")))
    track = _track(normalized)
    return KugouTrack(
        id=track.id, hash=track.hash, album_id=track.album_id, title=track.title,
        artist=track.artist, duration=track.duration, mixsongid=track.mixsongid,
        url=f"https://www.kugou.com/song/#hash={track.hash}"
        + (f"&album_id={track.album_id}" if _integer(track.album_id) is not None else ""),
    )


class KugouLibraryClient(KugouClient):
    page_size = 50

    def __init__(self, session: KugouSession, client: httpx.Client | None = None):
        if not isinstance(session, KugouSession):
            raise KugouAuthError("酷狗登录会话已结束，请在酷狗主站重新读取个人歌单")
        super().__init__(client)
        self._session = session

    def _private_json(self, path: str, body: dict, *, check_cancelled=_NOOP) -> dict:
        if path not in {_LISTS_PATH, _TRACKS_PATH}:
            raise KugouError("不支持的酷狗个人歌单请求")
        check_cancelled()
        session = self._session
        timestamp = str(int(time.time() * 1000))
        params = {
            "appid": session.appid, "clienttime": timestamp, "clientver": "20000",
            "dfid": session.dfid, "mid": session.mid or self._mid, "srcappid": "2919",
            "token": session.token, "userid": int(session.user_id), "uuid": timestamp, "plat": 1,
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        material = "".join(f"{key}={params[key]}" for key in sorted(params)) + encoded
        params["signature"] = hashlib.md5((_H5_SIGN_SEED + material + _H5_SIGN_SEED).encode()).hexdigest()
        # A fresh Request avoids inheriting cookies, auth headers or query
        # defaults from an injected client. Only this fixed HTTPS host receives
        # the token, and even same-host redirects are rejected.
        request = httpx.Request("POST", _GATEWAY + path, params=params, content=encoded.encode(), headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.kugou.com/",
            "Content-Type": "application/json", "x-router": "cloudlist.service.kugou.com",
        })
        try:
            response = self.client.send(request, auth=None, follow_redirects=False)
        except httpx.HTTPError:
            # Never chain an exception whose request URL contains the token:
            # the host includes tracebacks in diagnostic messages.
            raise KugouError("酷狗个人歌单连接失败，请检查网络或稍后重试") from None
        with closing(response):
            check_cancelled()
            if response.is_redirect:
                raise KugouError("酷狗个人歌单接口发生重定向，已停止发送登录信息")
            if response.status_code == 401:
                raise KugouAuthError("酷狗登录已失效，请在酷狗主站重新登录后重试")
            if response.status_code != 200:
                raise KugouError(f"酷狗个人歌单接口 HTTP {response.status_code}，请稍后重试")
            try:
                payload = response.json()
            except ValueError:
                raise KugouError("酷狗个人歌单接口未返回有效 JSON，请稍后重试") from None
        if not isinstance(payload, dict):
            raise KugouError("酷狗个人歌单接口返回了无效的数据结构")
        raw_code = payload.get("error_code", payload.get("errcode"))
        code = _integer(raw_code)
        if code == 20010:
            raise KugouAuthError("酷狗登录已失效或未获授权（20010），请在酷狗主站重新登录后重试")
        if str(payload.get("status")) != "1" or (raw_code is not None and str(raw_code) != "0"):
            suffix = f"（错误码 {code}）" if code is not None else ""
            raise KugouError(f"酷狗个人歌单读取失败{suffix}，请重新登录或稍后重试")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise KugouError("酷狗个人歌单接口缺少有效的数据")
        return data

    def _pages(self, path: str, body: dict, *, check_cancelled=_NOOP, expected_count=None):
        consumed = 0
        declared_total = None
        seen_pages: set[str] = set()
        initial_version = None
        total_key = None
        for page in range(1, self.max_pages + 1):
            check_cancelled()
            data = self._private_json(path, {**body, "page": page, "pagesize": self.page_size},
                                      check_cancelled=check_cancelled)
            rows = _first(data, "info", "songs", "lists", "file", default=None)
            if isinstance(rows, dict):
                rows = rows.get("file")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise KugouError("酷狗个人歌单分页缺少有效记录，无法确认完整性")
            if data.get("page") is not None and _integer(data["page"]) != page:
                raise KugouError("酷狗个人歌单返回了错误的分页，未完成全部读取")

            # Service variants differ in whether totals/end flags are exposed.
            # Without either marker, keep requesting pages until an empty page;
            # neither a short page nor the initial library's m_count is a stop.
            current_key = next((key for key in ("total", "total_count", "count") if key in data), None)
            if page == 1:
                total_key = current_key
            elif current_key != total_key:
                raise KugouError("酷狗个人歌单分页总数字段发生变化，无法确认完整性")
            count = _integer(data[current_key]) if current_key else None
            if current_key and count is None:
                raise KugouError("酷狗个人歌单分页总数无效，无法确认完整性")
            if count is not None:
                if ((declared_total is not None and declared_total != count)
                        or (expected_count is not None and expected_count != count)):
                    raise KugouError("酷狗个人歌单在读取期间发生变化，请重新读取")
                declared_total = count
            more_key = next((key for key in ("has_more", "hasmore") if key in data), None)
            more = None
            if more_key:
                value = data[more_key]
                if type(value) not in {int, str, bool} or value not in (0, 1, "0", "1", False, True):
                    raise KugouError("酷狗个人歌单分页结束标记无效")
                more = str(value).lower() in {"1", "true"}
            limit = declared_total if declared_total is not None else expected_count
            version = data.get("total_ver")
            if version is not None:
                if initial_version is None:
                    initial_version = str(version)
                elif str(version) != initial_version:
                    raise KugouError("酷狗个人歌单在读取期间发生变化，请重新读取")

            # Fingerprint full records so malformed pages cannot be hidden by
            # song-hash deduplication. Hashing also avoids retaining raw bodies.
            fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                                     separators=(",", ":")).encode()).hexdigest()
            if rows and fingerprint in seen_pages:
                raise KugouError("酷狗个人歌单接口重复返回同一页，未完成全部读取")
            seen_pages.add(fingerprint)
            consumed += len(rows)
            if limit is not None and consumed > limit:
                raise KugouError("酷狗个人歌单记录数量超过接口总数，无法确认完整性")
            if limit is not None and more is False and consumed != limit:
                raise KugouError("酷狗个人歌单提前结束，未完成全部读取")
            if limit is not None and consumed == limit and more is True:
                raise KugouError("酷狗个人歌单总数与结束标记冲突，无法确认完整性")
            if (declared_total is not None and consumed == declared_total) or more is False:
                yield rows
                return
            if not rows:
                if more is True or (limit is not None and consumed != limit):
                    raise KugouError("酷狗个人歌单提前结束，未完成全部读取")
                yield rows
                return
            yield rows
        raise KugouError("酷狗个人歌单超过分页安全上限，未完成全部读取")

    def list_playlists(self, *, check_cancelled=_NOOP) -> list[KugouPlaylist]:
        session = self._session
        # No local revision exists: 0 requests the complete snapshot, rather
        # than a delta against somebody else's hard-coded baseline version.
        body = {"userid": int(session.user_id), "token": session.token, "total_ver": 0, "type": 2}
        playlists: list[KugouPlaylist] = []
        seen_ids: set[tuple[int, str]] = set()
        for rows in self._pages(_LISTS_PATH, body, check_cancelled=check_cancelled):
            for row in rows:
                check_cancelled()
                # The cloud collection service also returns collected albums.
                # list_ver=2 identifies albums, not personal song playlists.
                if str(row.get("list_ver")) == "2":
                    continue
                list_id = _row_id(row)
                list_type = _integer(_first(row, "type", "list_type", default=0))
                if list_type not in {0, 1}:
                    raise KugouError("酷狗个人歌单返回了不支持的歌单类型")
                entry_key = (list_type, list_id)
                if entry_key in seen_ids:
                    raise KugouError("酷狗个人歌单分页出现重复的歌单，可能在读取期间发生变化")
                seen_ids.add(entry_key)
                global_id = str(row.get("global_collection_id") or "")
                canonical_id = global_id if _GLOBAL_ID.fullmatch(global_id) else list_id
                source_id = str(_first(row, "list_create_gid", "create_gid")) if list_type == 1 else global_id
                if not _GLOBAL_ID.fullmatch(source_id):
                    source_id = global_id
                url = f"https://www.kugou.com/yy/special/single/{source_id}.html" if _GLOBAL_ID.fullmatch(source_id) else ""
                count_value = _first(row, "count", "m_count", default=None)
                count = _integer(count_value)
                if count_value is not None and count is None:
                    raise KugouError("酷狗个人歌单返回了无效的歌曲总数")
                title = unescape(str(row.get("name") or row.get("title") or f"酷狗歌单 {list_id}"))
                playlists.append(KugouPlaylist(canonical_id, title, count, url, list_id, list_type))
        return playlists

    def resolve_playlist(self, playlist: KugouPlaylist, *, check_cancelled=_NOOP) -> KugouCollection:
        if not isinstance(playlist, KugouPlaylist):
            raise KugouError("无效的酷狗个人歌单")
        if playlist.list_type == 1:
            if not playlist.url:
                raise KugouError("酷狗收藏歌单缺少可识别的公开来源地址")
            # The cloud-list endpoint's numeric ID belongs to this account.
            # Saved playlists point to someone else's original list, which the
            # existing complete public parser can read without the web token.
            collection = super().resolve_collection(playlist.url, check_cancelled=check_cancelled)
            return KugouCollection(playlist.id, playlist.title, collection.tracks)
        list_id = playlist.list_id or playlist.id
        match = _GLOBAL_ID.fullmatch(list_id)
        if match:
            list_id = match.group(1)
        if not _POSITIVE_ID.fullmatch(list_id):
            raise KugouError("酷狗个人歌单缺少有效的云歌单 ID")
        session = self._session
        body = {
            "listid": int(list_id), "userid": int(session.user_id), "area_code": 1,
            "show_relate_goods": 0, "allplatform": 1, "show_cover": 1, "type": 0,
            "token": session.token,
        }
        tracks: dict[str, KugouTrack] = {}
        seen_files: set[str] = set()
        position = 0
        for rows in self._pages(_TRACKS_PATH, body, check_cancelled=check_cancelled,
                               expected_count=playlist.count):
            for row in rows:
                check_cancelled()
                file_id = str(_first(row, "fileid", "file_id"))
                if not _POSITIVE_ID.fullmatch(file_id):
                    file_id = ""
                if file_id and file_id in seen_files:
                    raise KugouError("酷狗个人歌单分页出现重复歌曲记录，可能在读取期间发生变化")
                if file_id:
                    seen_files.add(file_id)
                position += 1
                track = _library_track(row, position=position)
                tracks.setdefault(track.id, track)
        return KugouCollection(playlist.id, playlist.title, list(tracks.values()))
