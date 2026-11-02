"""Bounded, read-only personal-playlist previews before download confirmation.

Only one daemon worker per store calls ``list_playlists``. Previewing never
resolves songs, requests media, or creates queue tasks. Credentials are removed
from the record as soon as the worker starts, and are not retained while the
user reviews a ready preview.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
import threading
import time
import uuid

from kugou import KugouError
from kugou_library import KugouAuthError, KugouLibraryClient, KugouPlaylist, KugouSession
from kugou_plan import MAX_PLAYLISTS, playlist_plan


_MAX_PLAYLISTS = MAX_PLAYLISTS
_MAX_PAGE_SIZE = 100
_MAX_TITLE = 256
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")


class KugouPreviewError(KugouError):
    """A safe user-facing preview state or input error."""


class _PreviewStopped(Exception):
    pass


@dataclass
class _Preview:
    id: str
    user_id: str
    expires_at: float
    session: KugouSession | None = field(repr=False)
    status: str = "loading"
    started: bool = False
    playlists: tuple[KugouPlaylist, ...] = ()
    playlist_count: int = 0
    known_track_count: int = 0
    unknown_track_count: int = 0
    error: str = ""


def _normalize_playlist(playlist: KugouPlaylist, token: str) -> KugouPlaylist:
    """Bound preview/plan fields without inventing or truncating link targets."""
    if not isinstance(playlist, KugouPlaylist) or not isinstance(playlist.title, str):
        raise KugouPreviewError("酷狗歌单预览数据格式无效，请重新读取")
    # A server must not reflect an account credential into a displayed title.
    title = _CONTROLS.sub(" ", playlist.title.replace(token, "[已隐藏]")).strip()[:_MAX_TITLE]
    normalized = KugouPlaylist(playlist.id, title or "未命名歌单", playlist.count,
                               playlist.url, playlist.list_id, playlist.list_type)
    try:
        # Preview and persisted confirmation use exactly the same ID, URL,
        # count and local-list-ID rules. Do not display a ready snapshot that
        # the confirmation path would reject, or rewrite a supplied URL.
        return playlist_plan([normalized])[0]
    except ValueError:
        raise KugouPreviewError("酷狗歌单预览数据格式无效，请重新读取") from None


class KugouPreviewStore:
    """Keep a small set of expiring previews with atomic one-time consumption."""

    def __init__(self, client_factory=KugouLibraryClient, ttl_seconds=900, max_previews=4):
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or ttl_seconds <= 0
                or type(max_previews) is not int or not 1 <= max_previews <= 32):
            raise ValueError("预览有效期或容量无效")
        self._client_factory = client_factory
        self._ttl_seconds = float(ttl_seconds)
        self._max_previews = max_previews
        self._clock = time.monotonic
        self._condition = threading.Condition(threading.RLock())
        self._records: dict[str, _Preview] = {}
        self._worker: threading.Thread | None = None
        self._closed = False

    def start(self, session: KugouSession) -> dict:
        if not isinstance(session, KugouSession):
            raise KugouPreviewError("请在酷狗主站登录后重新读取歌单")
        with self._condition:
            if self._closed:
                raise KugouPreviewError("歌单预览服务已关闭，请重新连接本机组件")
            self._expire_locked()
            while len(self._records) >= self._max_previews:
                oldest_id = next(iter(self._records))
                self._stop_locked(self._records.pop(oldest_id), "预览已被新的读取请求替换，请重新读取")
            preview_id = uuid.uuid4().hex
            record = _Preview(preview_id, session.user_id, self._clock() + self._ttl_seconds, session)
            self._records[preview_id] = record
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="kugou-playlist-preview", daemon=True)
                self._worker.start()
            self._condition.notify_all()
            # The worker cannot acquire this lock until the initial loading
            # response has been constructed, so fast responses are consistent.
            return self._page_locked(record, 0, 50)

    def get(self, preview_id, offset=0, limit=50) -> dict:
        if type(offset) is not int or not 0 <= offset <= _MAX_PLAYLISTS or type(limit) is not int or limit <= 0:
            raise KugouPreviewError("歌单预览分页参数无效")
        with self._condition:
            self._expire_locked()
            return self._page_locked(self._record_locked(preview_id), offset, min(limit, _MAX_PAGE_SIZE))

    def consume(self, preview_id, user_id) -> tuple[KugouPlaylist, ...]:
        with self._condition:
            self._expire_locked()
            record = self._record_locked(preview_id)
            if not isinstance(user_id, str) or user_id != record.user_id:
                raise KugouPreviewError("当前酷狗账号与预览账号不同，请重新读取歌单")
            if record.status != "ready":
                raise KugouPreviewError("歌单预览尚未完成、已失效或已经确认，请重新读取")
            playlists = record.playlists
            record.playlists = ()
            record.session = None
            record.status = "consumed"
            self._condition.notify_all()
            return playlists

    def cancel(self, preview_id) -> dict:
        with self._condition:
            self._expire_locked()
            record = self._record_locked(preview_id)
            # A cancellation cannot undo a previously accepted confirmation.
            if record.status != "consumed":
                self._stop_locked(record, "已取消歌单读取")
            self._condition.notify_all()
            return self._page_locked(record, 0, 50)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for record in self._records.values():
                if record.status != "consumed":
                    self._stop_locked(record, "歌单预览服务已关闭，请重新读取")
            self._condition.notify_all()

    def _record_locked(self, preview_id) -> _Preview:
        if not isinstance(preview_id, str) or preview_id not in self._records:
            raise KugouPreviewError("歌单预览不存在或已过期，请重新读取")
        return self._records[preview_id]

    @staticmethod
    def _stop_locked(record: _Preview, error: str) -> None:
        record.status = "cancelled"
        record.session = None
        record.playlists = ()
        record.error = error

    def _expire_locked(self) -> None:
        now = self._clock()
        for record in self._records.values():
            if record.status in {"loading", "ready", "error"} and now >= record.expires_at:
                self._stop_locked(record, "歌单预览已过期，请重新读取后确认")

    @staticmethod
    def _page_locked(record: _Preview, offset: int, limit: int) -> dict:
        entries = record.playlists[offset:offset + limit] if record.status == "ready" else ()
        next_offset = offset + len(entries)
        return {
            "preview_id": record.id, "status": record.status, "user_id": record.user_id,
            "playlist_count": record.playlist_count, "known_track_count": record.known_track_count,
            # Number of playlists whose song counts have not been disclosed.
            "unknown_track_count": record.unknown_track_count,
            "playlists": [{"id": entry.id, "title": entry.title, "url": entry.url,
                           "count": entry.count, "list_id": entry.list_id, "list_type": entry.list_type}
                          for entry in entries],
            "offset": offset,
            "next_offset": next_offset if record.status == "ready" and next_offset < record.playlist_count else None,
            "error": record.error,
        }

    def _check_active(self, record: _Preview) -> None:
        with self._condition:
            self._expire_locked()
            if self._closed or self._records.get(record.id) is not record or record.status != "loading":
                raise _PreviewStopped()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._expire_locked()
                if self._closed:
                    return
                record = next((item for item in self._records.values()
                               if item.status == "loading" and not item.started), None)
                if record is None:
                    expirations = [item.expires_at for item in self._records.values()
                                   if item.status in {"loading", "ready", "error"}]
                    timeout = max(0, min(expirations) - self._clock()) if expirations else None
                    self._condition.wait(timeout=timeout)
                    continue
                record.started = True
                session = record.session
                record.session = None
            # Keep credential-bearing locals inside a separate frame. It ends
            # before the worker waits for another preview or its expiry.
            self._read(record, session)
            session = None

    def _read(self, record: _Preview, session: KugouSession | None) -> None:
        client = None
        try:
            self._check_active(record)
            if session is None:
                raise KugouAuthError("缺少登录状态")
            client = self._client_factory(session)
            rows = client.list_playlists(check_cancelled=lambda: self._check_active(record))
            self._check_active(record)
            if not isinstance(rows, (list, tuple)) or len(rows) > _MAX_PLAYLISTS:
                raise KugouPreviewError("歌单预览数量超出支持范围，请检查账号后重新读取")
            normalized = []
            seen = set()
            for row in rows:
                self._check_active(record)
                playlist = _normalize_playlist(row, session.token)
                key = (playlist.list_type, playlist.list_id or playlist.id)
                if key in seen:
                    raise KugouPreviewError("歌单预览包含重复记录，请重新读取")
                seen.add(key)
                normalized.append(playlist)
            playlists = tuple(normalized)
            with self._condition:
                self._check_active(record)
                record.playlists = playlists
                record.playlist_count = len(playlists)
                record.known_track_count = sum(item.count for item in playlists if item.count is not None)
                record.unknown_track_count = sum(item.count is None for item in playlists)
                record.status = "ready"
                record.error = ""
        except _PreviewStopped:
            pass
        except Exception as error:
            # Even supposedly safe API exceptions can originate from injected
            # clients. Never expose exception text, repr, URLs or tracebacks.
            if isinstance(error, KugouAuthError):
                message = "当前网页登录会话未通过酷狗歌单接口验证，请刷新当前页面后重新读取"
            elif isinstance(error, KugouPreviewError):
                message = "酷狗歌单预览数据无效或超出支持范围，请重新读取"
            else:
                message = "酷狗歌单接口请求失败，请检查网络后重新读取"
            with self._condition:
                self._expire_locked()
                if self._records.get(record.id) is record and record.status == "loading" and not self._closed:
                    record.status = "error"
                    record.error = message
                    record.playlists = ()
                    record.session = None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    # Cleanup errors must neither revive a cancelled preview
                    # nor echo an account credential from an HTTP client.
                    pass
