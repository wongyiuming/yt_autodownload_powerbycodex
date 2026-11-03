"""Allowlisted, credential-free diagnostics for the personal-library pipeline.

Never serialize str(error), request URLs, upstream messages/bodies, headers,
user IDs or device identifiers. A diagnostic contains only local codes and
bounded numeric protocol metadata. It can safely be displayed in the popup.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kugou import KugouError


class Failure(str, Enum):
    TIMEOUT = "NETWORK_TIMEOUT"
    PROXY = "NETWORK_PROXY"
    CONNECT = "NETWORK_CONNECT"
    TLS = "NETWORK_TLS"
    DNS = "NETWORK_DNS"
    TRANSPORT = "NETWORK_TRANSPORT"
    HTTP = "HTTP_ERROR"
    REDIRECT = "REDIRECT_BLOCKED"
    AUTH = "SESSION_REJECTED"
    API = "API_REJECTED"
    VERIFY = "VERIFICATION_REQUIRED"
    JSON = "INVALID_JSON"
    SCHEMA = "INVALID_RESPONSE"
    PAGE = "PAGINATION_INVALID"
    CHANGED = "PAGINATION_CHANGED"
    REPEAT = "PAGINATION_REPEATED"
    LIMIT = "PAGINATION_LIMIT"
    ROW = "PLAYLIST_RECORD_INVALID"
    INTERNAL = "INTERNAL_ERROR"


_MESSAGES = {
    Failure.TIMEOUT: "本机 Host 请求酷狗超时；浏览器与 Host 的代理路径可能不同",
    Failure.PROXY: "本机 Host 的代理连接失败；请检查 Host 使用的代理",
    Failure.CONNECT: "本机 Host 无法建立连接（DNS、TLS 或连接失败），不是未登录的判断",
    Failure.TLS: "本机 Host 的 TLS 证书校验或握手失败；请检查证书链，不要关闭证书校验",
    Failure.DNS: "本机 Host 无法解析酷狗接口域名，请检查 DNS 或代理配置",
    Failure.TRANSPORT: "本机 Host 与酷狗的 HTTP 传输失败",
    Failure.HTTP: "酷狗接口返回非成功 HTTP 状态",
    Failure.REDIRECT: "酷狗接口发生重定向，已停止发送登录信息",
    Failure.AUTH: "当前网页登录会话未通过酷狗歌单接口验证；不代表网页没有登录",
    Failure.API: "酷狗接口返回业务错误，请保留下面的错误码用于排查",
    Failure.VERIFY: "酷狗接口要求安全验证，请在官网处理；程序不会绕过验证",
    Failure.JSON: "酷狗接口未返回有效 JSON（可能是网关或代理页面）",
    Failure.SCHEMA: "酷狗接口返回的数据结构与当前适配不一致",
    Failure.PAGE: "酷狗分页数据不一致，无法确认已读取全部歌单",
    Failure.CHANGED: "歌单数量或版本在读取期间发生变化，请重新读取",
    Failure.REPEAT: "酷狗接口重复返回同一页，未完成全部读取",
    Failure.LIMIT: "酷狗歌单超过分页安全上限，未完成全部读取",
    Failure.ROW: "酷狗歌单记录字段与当前适配不一致",
    Failure.INTERNAL: "歌单读取发生程序异常；请反馈此诊断，不要发送 Cookie 或 token",
}
_PATHS = {"playlist_list": "/v7/get_all_list", "playlist_tracks": "/v4/get_list_all_file"}
_CHECKS = {"rows", "total", "page", "end", "version", "duplicate", "list_id", "list_type", "track_count", "user_id", "body"}
_SHAPE_KEYS = {"data", "info", "songs", "lists", "list", "items", "file", "total", "total_count", "count", "list_count", "collect_count", "album_count", "has_more", "hasmore", "page", "total_ver"}
_TYPES = {"object", "array", "string", "number", "boolean", "null"}


def _bounded(value, low, high):
    return value if type(value) is int and low <= value <= high else None


@dataclass(frozen=True)
class Diagnostic:
    code: Failure
    stage: str = "preview"
    page: int | None = None
    http_status: int | None = None
    api_code: int | None = None
    check: str = ""
    shape: tuple[tuple[str, str], ...] = ()
    exception_type: str = ""
    module: str = ""
    line: int | None = None

    def public(self) -> dict:
        code = self.code if type(self.code) is Failure else Failure.INTERNAL
        stage = self.stage if type(self.stage) is str and self.stage in {*_PATHS, "preview"} else "preview"
        result = {"code": code.value, "stage": stage}
        if stage in _PATHS:
            result["endpoint"] = _PATHS[stage]
        for key, value, low, high in (("page", self.page, 1, 1000),
                                     ("http_status", self.http_status, 100, 599),
                                     ("api_code", self.api_code, -99999999, 99999999)):
            value = _bounded(value, low, high)
            if value is not None:
                result[key] = value
        if type(self.check) is str and self.check in _CHECKS:
            result["check"] = self.check
        if type(self.shape) is tuple:
            shape = {}
            for pair in self.shape[:len(_SHAPE_KEYS)]:
                if type(pair) is tuple and len(pair) == 2:
                    key, value = pair
                    if type(key) is str and key in _SHAPE_KEYS and type(value) is str and value in _TYPES:
                        shape[key] = value
            if shape:
                result["shape"] = shape
        if type(self.exception_type) is str and self.exception_type in {"TypeError", "ValueError", "AttributeError", "KeyError", "RuntimeError", "OSError", "ImportError", "ModuleNotFoundError", "UnknownError"}:
            result["exception_type"] = self.exception_type
        if type(self.module) is str and self.module in {"kugou_library.py", "kugou_preview.py", "kugou_plan.py", "kugou.py", "kugou_diagnostics.py"}:
            result["module"] = self.module
            line = _bounded(self.line, 1, 100000)
            if line is not None:
                result["line"] = line
        return result

    def message(self) -> str:
        data = Diagnostic.public(self)
        message = _MESSAGES[Failure(data["code"])]
        details = [data["code"], data["stage"]]
        for key, label in (("page", "页"), ("http_status", "HTTP"), ("api_code", "业务码"), ("check", "检查项")):
            if key in data:
                details.append(f"{label}={data[key]}")
        return message + " [" + ", ".join(details) + "]"


def response_shape(data) -> tuple[tuple[str, str], ...]:
    """Record known field *types*, never contents or unknown field names."""
    def kind(value):
        return {dict: "object", list: "array", str: "string", int: "number", float: "number", bool: "boolean", type(None): "null"}.get(type(value))
    if type(data) is not dict:
        return (("data", kind(data)),) if kind(data) else ()
    return tuple((key, kind(data[key])) for key in sorted(_SHAPE_KEYS) if key in data and kind(data[key]))


class KugouDiagnosticError(KugouError):
    def __init__(self, message: str, diagnostic: Diagnostic):
        super().__init__(message)
        self.diagnostic = diagnostic


def public_diagnostic(error: Exception) -> Diagnostic | None:
    """Only accept our exact data type; never call an exception's formatter."""
    diagnostic = getattr(error, "diagnostic", None) if isinstance(error, KugouError) else None
    if type(diagnostic) is Diagnostic:
        return diagnostic
    return None


def internal_diagnostic(error: Exception) -> Diagnostic:
    """Expose only a fixed exception class and our source location, no locals."""
    allowed = {TypeError, ValueError, AttributeError, KeyError, RuntimeError, OSError, ImportError, ModuleNotFoundError}
    name = type(error).__name__ if type(error) in allowed else "UnknownError"
    module, line = "", None
    tb = error.__traceback__
    while tb is not None:
        file = tb.tb_frame.f_code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if file in {"kugou_library.py", "kugou_preview.py", "kugou_plan.py", "kugou.py", "kugou_diagnostics.py"}:
            module, line = file, tb.tb_lineno
        tb = tb.tb_next
    return Diagnostic(Failure.INTERNAL, exception_type=name, module=module, line=line)
