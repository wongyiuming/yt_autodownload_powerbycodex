"""Read the signed-in Kugou web user's complete playlist library.

Only the two read-only cloud-list requests carry an in-memory web token. Public
playback and audio transfers continue to use :class:`KugouClient` without cookies.
The H5 signing format and cookie fields match Kugou's ``infSign.min.js`` and
``getBaseInfo.min.js``; see README for the endpoint evidence and live-test limit.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field, replace
import hashlib
from html import unescape
import json
import re
import socket
import ssl
import time
from urllib.parse import unquote

import httpx

from kugou import KugouClient, KugouCollection, KugouError, KugouTrack, _NOOP, _track
from kugou_diagnostics import Diagnostic, Failure, KugouDiagnosticError, response_shape


_GATEWAY = "https://gateway.kugou.com"
_LISTS_PATH = "/v7/get_all_list"
_TRACKS_PATH = "/v4/get_list_all_file"
# Public H5 request checksum seed, not an account credential or media key.
_H5_SIGN_SEED = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
_POSITIVE_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_GLOBAL_ID = re.compile(r"collection_[0-9]+_[0-9]+_([1-9][0-9]*)_[0-9]+\Z")
_COOKIE_NAMES = {"KuGoo", "kg_mid", "kg_dfid", "KugooID", "userid", "token", "t", "a_id", "appid"}


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
    ``t`` and ``a_id``. The extension can instead send the minimal separate
    userid/token/appid fields returned by the official page's getBaseInfo.
    Each candidate must provide its own complete userid/token pair: never join
    half an old compound cookie to a token from a different login or partition.
    """
    if not isinstance(cookies, list) or len(cookies) > 1000:
        raise KugouAuthError("未收到有效的酷狗网页登录信息，请在酷狗主站登录后重试")
    values: dict[str, str] = {}
    scopes: set[tuple[str, str]] = set()
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
        if name not in {"kg_mid", "kg_dfid"}:
            scopes.add((str(cookie.get("storeId") or ""),
                        json.dumps(cookie.get("partitionKey"), sort_keys=True, separators=(",", ":"))))
        if name in values and values[name] != value:
            raise KugouAuthError("检测到冲突的酷狗网页登录信息，请在当前浏览器重新登录")
        values[name] = value
    if len(scopes) > 1:
        raise KugouAuthError("酷狗登录字段来自不同浏览器会话或 Cookie 分区，请在当前标签页重新读取")

    compound: dict[str, str] = {}
    raw = values.get("KuGoo", "")
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    # Preserve literal '+' in token values; this is not form-urlencoded data.
    for _ in range(2):
        if "=" in raw:
            break
        raw = unquote(raw)
    for item in raw.split("&"):
        key, separator, value = item.partition("=")
        if separator and key in {"KugooID", "userid", "t", "token", "a_id", "appid"}:
            if key in compound and compound[key] != unquote(value):
                raise KugouAuthError("酷狗网页登录信息格式无效，请重新登录")
            compound[key] = unquote(value)

    def coherent_value(fields, names, default=""):
        alternatives = {fields[name] for name in names if fields.get(name)}
        if len(alternatives) > 1:
            raise KugouAuthError("酷狗登录字段相互冲突，请在当前标签页重新读取")
        return next(iter(alternatives), default)

    candidates: list[KugouSession] = []
    for fields in (compound, values):
        user_id = coherent_value(fields, ("KugooID", "userid"))
        token = coherent_value(fields, ("t", "token"))
        if not user_id or not token:
            continue
        appid = coherent_value(fields, ("a_id", "appid"), "1014")
        if appid not in {"1010", "1014"}:
            raise KugouAuthError("当前酷狗登录信息不属于网页版，请在酷狗主站重新登录")
        candidates.append(KugouSession(user_id, token, values.get("kg_mid", ""),
                                       values.get("kg_dfid") or "-", int(appid)))
    if not candidates:
        raise KugouAuthError("未收到完整的酷狗用户 ID 和登录令牌，请在当前酷狗标签页重新读取会话")
    if any(candidate.user_id != candidates[0].user_id or candidate.token != candidates[0].token
           for candidate in candidates[1:]):
        raise KugouAuthError("检测到冲突的酷狗网页登录信息，请在当前浏览器重新登录")
    return candidates[0]


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
        self._diagnostic = Diagnostic(Failure.INTERNAL)

    def _failure(self, message, code, check="", *, auth=False):
        diagnostic = replace(self._diagnostic, code=code, check=check)
        if auth:
            error = KugouAuthError(message)
            error.diagnostic = diagnostic
            return error
        return KugouDiagnosticError(message, diagnostic)

    def _private_json(self, path: str, body: dict, *, check_cancelled=_NOOP) -> dict:
        if path not in {_LISTS_PATH, _TRACKS_PATH}:
            raise KugouError("不支持的酷狗个人歌单请求")
        check_cancelled()
        stage = "playlist_list" if path == _LISTS_PATH else "playlist_tracks"
        self._diagnostic = Diagnostic(Failure.INTERNAL, stage, _integer(body.get("page")))
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.kugou.com/", "Accept": "application/json",
            "Content-Type": "application/json", "x-router": "cloudlist.service.kugou.com",
        })
        try:
            response = self.client.send(request, auth=None, follow_redirects=False)
        except httpx.HTTPError as error:
            # Classify by type, not str(error): request URLs can contain tokens.
            code = (Failure.TIMEOUT if isinstance(error, httpx.TimeoutException) else
                    Failure.PROXY if isinstance(error, httpx.ProxyError) else
                    Failure.CONNECT if isinstance(error, httpx.ConnectError) else Failure.TRANSPORT)
            if code == Failure.CONNECT:
                cause = error
                for _ in range(8):
                    if isinstance(cause, ssl.SSLError):
                        code = Failure.TLS
                        break
                    if isinstance(cause, socket.gaierror):
                        code = Failure.DNS
                        break
                    cause = cause.__cause__ or cause.__context__
                    if cause is None:
                        break
            raise self._failure("酷狗个人歌单连接失败，请检查网络或稍后重试", code) from None
        with closing(response):
            check_cancelled()
            self._diagnostic = replace(self._diagnostic, http_status=response.status_code)
            if response.headers.get("ssa-code"):
                raise self._failure("酷狗接口要求安全验证，请在官网处理", Failure.VERIFY)
            if response.is_redirect:
                raise self._failure("酷狗个人歌单接口发生重定向，已停止发送登录信息", Failure.REDIRECT)
            if response.status_code == 401:
                raise self._failure("酷狗会话未通过接口验证", Failure.AUTH, auth=True)
            if response.status_code != 200:
                raise self._failure(f"酷狗个人歌单接口 HTTP {response.status_code}，请稍后重试", Failure.HTTP)
            try:
                payload = response.json()
            except ValueError:
                raise self._failure("酷狗个人歌单接口未返回有效 JSON，请稍后重试", Failure.JSON) from None
        if not isinstance(payload, dict):
            raise self._failure("酷狗个人歌单接口返回了无效的数据结构", Failure.SCHEMA, "body")
        raw_code = payload.get("error_code", payload.get("errcode"))
        code = int(raw_code) if type(raw_code) in {int, str} and re.fullmatch(r"-?[0-9]{1,8}", str(raw_code)) else None
        self._diagnostic = replace(self._diagnostic, api_code=code, shape=response_shape(payload.get("data")))
        if code == 20010:
            raise self._failure("酷狗登录会话未获接口授权（20010），请在酷狗主站重新登录后重试", Failure.AUTH, auth=True)
        if str(payload.get("status")) != "1" or (raw_code is not None and str(raw_code) != "0"):
            suffix = f"（错误码 {code}）" if code is not None else ""
            raise self._failure(f"酷狗个人歌单读取失败{suffix}，请重新登录或稍后重试", Failure.API)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise self._failure("酷狗个人歌单接口缺少有效的数据", Failure.SCHEMA, "body")
        if "userid" in data and str(data["userid"]) != session.user_id:
            raise self._failure("酷狗接口返回的账号与当前会话不同", Failure.SCHEMA, "user_id")
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
            rows = _first(data, "info", "songs", "lists", "list", "items", "file", default=None)
            if isinstance(rows, dict):
                rows = rows.get("file")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise self._failure("酷狗个人歌单分页缺少有效记录，无法确认完整性", Failure.SCHEMA, "rows")
            if data.get("page") is not None and _integer(data["page"]) != page:
                raise self._failure("酷狗个人歌单返回了错误的分页，未完成全部读取", Failure.PAGE, "page")

            # Service variants differ in whether totals/end flags are exposed.
            # Without either marker, keep requesting pages until an empty page;
            # neither a short page nor the initial library's m_count is a stop.
            current_key = next((key for key in ("total", "total_count", "count") if key in data), None)
            # list_count/collect_count/album_count are category metadata, not
            # an authoritative total for info. Do not add them or stop on them.
            if page == 1:
                total_key = current_key
            elif current_key != total_key:
                raise self._failure("酷狗个人歌单分页总数字段发生变化，无法确认完整性", Failure.PAGE, "total")
            count = _integer(data[current_key]) if current_key else None
            if current_key and count is None:
                raise self._failure("酷狗个人歌单分页总数无效，无法确认完整性", Failure.PAGE, "total")
            if count is not None:
                if ((declared_total is not None and declared_total != count)
                        or (expected_count is not None and expected_count != count)):
                    raise self._failure("酷狗个人歌单在读取期间发生变化，请重新读取", Failure.CHANGED, "version")
                declared_total = count
            more_key = next((key for key in ("has_more", "hasmore") if key in data), None)
            more = None
            if more_key:
                value = data[more_key]
                if type(value) not in {int, str, bool} or value not in (0, 1, "0", "1", False, True):
                    raise self._failure("酷狗个人歌单分页结束标记无效", Failure.PAGE, "end")
                more = str(value).lower() in {"1", "true"}
            limit = declared_total if declared_total is not None else expected_count
            version = data.get("total_ver")
            if version is not None:
                if initial_version is None:
                    initial_version = str(version)
                elif str(version) != initial_version:
                    raise self._failure("酷狗个人歌单在读取期间发生变化，请重新读取", Failure.CHANGED, "version")

            # Fingerprint full records so malformed pages cannot be hidden by
            # song-hash deduplication. Hashing also avoids retaining raw bodies.
            fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                                     separators=(",", ":")).encode()).hexdigest()
            if rows and fingerprint in seen_pages:
                raise self._failure("酷狗个人歌单接口重复返回同一页，未完成全部读取", Failure.REPEAT, "duplicate")
            seen_pages.add(fingerprint)
            consumed += len(rows)
            if limit is not None and consumed > limit:
                raise self._failure("酷狗个人歌单记录数量超过接口总数，无法确认完整性", Failure.PAGE, "total")
            if limit is not None and more is False and consumed != limit:
                raise self._failure("酷狗个人歌单提前结束，未完成全部读取", Failure.PAGE, "end")
            if limit is not None and consumed == limit and more is True:
                raise self._failure("酷狗个人歌单总数与结束标记冲突，无法确认完整性", Failure.PAGE, "end")
            if (declared_total is not None and consumed == declared_total) or more is False:
                yield rows
                return
            if not rows:
                if more is True or (limit is not None and consumed != limit):
                    raise self._failure("酷狗个人歌单提前结束，未完成全部读取", Failure.PAGE, "end")
                yield rows
                return
            yield rows
        raise self._failure("酷狗个人歌单超过分页安全上限，未完成全部读取", Failure.LIMIT, "page")

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
                try:
                    list_id = _row_id(row)
                except KugouError:
                    raise self._failure("酷狗个人歌单缺少有效的云歌单 ID，无法确认完整性", Failure.ROW, "list_id") from None
                list_type = _integer(_first(row, "type", "list_type", default=0))
                if list_type not in {0, 1}:
                    raise self._failure("酷狗个人歌单返回了不支持的歌单类型", Failure.ROW, "list_type")
                entry_key = (list_type, list_id)
                if entry_key in seen_ids:
                    raise self._failure("酷狗个人歌单分页出现重复的歌单，可能在读取期间发生变化", Failure.REPEAT, "duplicate")
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
                    raise self._failure("酷狗个人歌单返回了无效的歌曲总数", Failure.ROW, "track_count")
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
                    raise self._failure("酷狗个人歌单分页出现重复歌曲记录，可能在读取期间发生变化", Failure.REPEAT, "duplicate")
                if file_id:
                    seen_files.add(file_id)
                position += 1
                track = _library_track(row, position=position)
                tracks.setdefault(track.id, track)
        return KugouCollection(playlist.id, playlist.title, list(tracks.values()))
