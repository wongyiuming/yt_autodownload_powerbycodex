"""Opt-in single-song playback through the user's own Kugou web session.

Credentials live only in the queue's memory and this client. They are sent to
one fixed HTTPS endpoint, never to page metadata, CDN downloads or redirects.
A refusal remains a refusal: no paid-media unlocks or authorization fallback.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import re
from urllib.parse import quote, unquote, urlsplit

import httpx

from kugou import KugouClient, KugouError, KugouMedia, KugouTrack, _HASH, _NOOP
from kugou_library import KugouAuthError, KugouSession

_ENDPOINT = "https://wwwapi.kugou.com/yy/index.php"


def _code(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return int(value) if re.fullmatch(r"-?[0-9]{1,8}", str(value)) else None


class KugouSongClient(KugouClient):
    """An authenticated playback path, deliberately not enabled for libraries."""

    def __init__(self, session: KugouSession, client: httpx.Client | None = None):
        if not isinstance(session, KugouSession):
            raise KugouAuthError("酷狗单曲缺少完整登录会话，请重新读取当前歌曲页")
        super().__init__(client)
        self._session = session

    def close(self) -> None:
        self._session = None
        super().close()

    def resolve_track(self, track: KugouTrack, *, check_cancelled=_NOOP) -> KugouMedia:
        if not isinstance(track, KugouTrack) or not _HASH.fullmatch(track.hash):
            raise KugouError("酷狗单曲缺少有效的歌曲标识，尚未请求播放接口")
        session = self._session
        if not isinstance(session, KugouSession):
            raise KugouAuthError("酷狗单曲登录会话已结束，请重新读取当前歌曲页")
        check_cancelled()
        params = {
            "r": "play/getdata", "hash": track.hash,
            "album_id": track.album_id or "0", "album_audio_id": track.mixsongid or "0",
            "appid": session.appid, "platid": 4,
            "mid": session.mid or self._mid, "dfid": session.dfid,
            "userid": session.user_id, "token": session.token,
        }
        # Reconstruct only SDK-supported fields; never copy the whole cookie jar.
        cookie = (f"KuGoo=KugooID={session.user_id}&t={quote(session.token, safe='')}&a_id={session.appid}; "
                  f"kg_mid={quote(params['mid'], safe='')}; kg_dfid={quote(session.dfid, safe='')}")
        request = httpx.Request("GET", _ENDPOINT, params=params, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.kugou.com/",
            "Accept": "application/json", "Cookie": cookie,
        })
        try:
            response = self.client.send(request, auth=None, follow_redirects=False)
        except httpx.HTTPError as error:
            category = ("超时" if isinstance(error, httpx.TimeoutException) else
                        "代理连接失败" if isinstance(error, httpx.ProxyError) else "HTTP 传输失败")
            # httpx exceptions can contain the credential-bearing request URL.
            raise KugouError(f"酷狗网页登录态播放接口{category}；不是账号付费状态的判断") from None
        with closing(response):
            check_cancelled()
            if response.headers.get("ssa-code"):
                raise KugouAuthError("酷狗网页登录态播放接口要求安全验证，请在官网完成验证")
            if response.is_redirect:
                raise KugouAuthError("酷狗网页登录态播放接口发生重定向；已停止转发登录凭据")
            if response.status_code == 401:
                raise KugouAuthError("酷狗网页登录态播放接口拒绝当前会话（HTTP 401），请重新登录后重试")
            if response.status_code != 200:
                raise KugouError(f"酷狗网页登录态播放接口 HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError:
                raise KugouError("酷狗网页登录态播放接口未返回有效 JSON") from None
        if not isinstance(payload, dict):
            raise KugouError("酷狗网页登录态播放接口返回结构无效")
        codes = [_code(payload[key]) for key in ("err_code", "error_code", "errcode") if key in payload]
        if any(code is None for code in codes):
            raise KugouError("酷狗网页登录态播放接口业务码格式无效")
        code = next((value for value in codes if value), 0)
        if code == 20010:
            raise KugouAuthError("酷狗网页登录态播放接口拒绝当前会话（业务码 20010），请重新读取登录态")
        if code or ("status" in payload and str(payload["status"]) != "1"):
            # Only bounded numeric codes, never an upstream message or body.
            raise KugouError(f"酷狗网页登录态播放接口返回业务错误（业务码 {code}）；不推断具体付费或版权原因")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise KugouError("酷狗网页登录态播放接口缺少歌曲数据")
        if "userid" in data and str(data["userid"]) != session.user_id:
            raise KugouAuthError("酷狗网页登录态播放接口返回的账号与当前任务不一致")
        url = data.get("play_url")
        if not isinstance(url, str) or not url:
            raise KugouError(f"酷狗网页登录态播放接口未提供完整音频地址（业务码 {code}）；不推断具体付费或版权原因")
        # A malformed response must not turn the credential-bearing API URL
        # into a CDN request, nor smuggle the token in a different URL field.
        decoded_url = unquote(unquote(url))
        parsed = urlsplit(url)
        if session.token in decoded_url or (parsed.hostname == "wwwapi.kugou.com"
                                           and parsed.path == "/yy/index.php"):
            raise KugouError("酷狗播放地址含账号凭据或指向认证接口，已拒绝转发")
        # _media still rejects preview fragments, encrypted formats and invalid
        # URLs. No endpoint fallback follows an authenticated refusal.
        media = self._media(payload, track, mobile=False)
        return replace(media, title=track.title or track.hash)
