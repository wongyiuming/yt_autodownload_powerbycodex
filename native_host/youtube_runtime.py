"""Safe YouTube access: capture yt-dlp errors and avoid repeated broken client requests."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import subprocess
from pathlib import Path

import yt_dlp
from yt_dlp.version import __version__ as YT_DLP_VERSION

RELOAD_MARKER = "the page needs to be reloaded"
ALT_PLAYER_CLIENTS = ("web_safari", "web_embedded")


class NonRetryableYoutubeError(RuntimeError):
    """The failing YouTube extractor request has exhausted safe fallbacks."""


class _RecordingLogger:
    """Forward yt-dlp logs while retaining the specific cause of a failed attempt."""

    def __init__(self, downstream):
        self.downstream = downstream
        self.diagnostics: list[str] = []

    def _forward(self, level: str, message: str) -> None:
        value = str(message)
        if level in {"warning", "error"}:
            self.diagnostics.append(value[-2000:])
            self.diagnostics = self.diagnostics[-8:]
        if self.downstream:
            getattr(self.downstream, level)(value)

    def debug(self, message: str) -> None:
        self._forward("debug", message)

    def info(self, message: str) -> None:
        self._forward("info", message)

    def warning(self, message: str) -> None:
        self._forward("warning", message)

    def error(self, message: str) -> None:
        self._forward("error", message)


def youtube_options(node_path: str | None, cookie_path: Path, *, use_cookies: bool = False) -> dict:
    """Use the bundled EJS solver with Node; GitHub is a backup distribution."""
    options: dict = {"no_plugins": True}
    if use_cookies and cookie_path.is_file():
        options["cookiefile"] = str(cookie_path)
    if node_path:
        options.update({
            "js_runtimes": {"node": {"path": node_path}},
            "remote_components": {"ejs:github"},
        })
    return options


def _is_reload_failure(exc: Exception, logger: _RecordingLogger) -> bool:
    combined = str(exc) + "\n" + "\n".join(logger.diagnostics)
    return RELOAD_MARKER in combined.lower()


def _run_once(operation, active: dict):
    recorder = _RecordingLogger(active.get("logger"))
    run_options = dict(active)
    run_options["logger"] = recorder
    try:
        return True, operation(run_options), None, recorder
    except Exception as exc:
        return False, None, exc, recorder


def _try_without_cookies(operation, options: dict):
    """Try original request, then anonymous, then an alternate public web client.

    Only the specific reload error triggers fallback. Other download failures
    are raised unchanged. At most three attempts are made, never an unbounded loop.
    """
    active = dict(options)
    alternate_client_tried = False
    logger = options.get("logger")
    for _ in range(3):
        ok, result, error, recorder = _run_once(operation, active)
        if ok:
            if alternate_client_tried and logger:
                logger.info("YouTube 备用播放器客户端解析成功。")
            return result
        assert error is not None
        if not _is_reload_failure(error, recorder):
            raise error

        if active.get("cookiefile"):
            if logger:
                logger.warning(
                    "YouTube 登录 Cookie 请求出现 reload 错误；"
                    "尝试不携带 Cookie 下载公开资源。"
                )
            active = dict(active)
            active.pop("cookiefile", None)
            continue

        if not alternate_client_tried:
            if logger:
                logger.warning(
                    "YouTube 匿名请求仍出现 reload 错误；"
                    "尝试 web_safari / web_embedded 备用客户端。"
                )
            alternate_client_tried = True
            active = dict(active)
            args = {k: dict(v) for k, v in active.get("extractor_args", {}).items()}
            args.setdefault("youtube", {})["player_client"] = list(ALT_PLAYER_CLIENTS)
            active["extractor_args"] = args
            continue

        raise NonRetryableYoutubeError(
            "YouTube 页面仍要求重新加载；已尝试 Cookie/匿名请求及备用客户端。"
            "请先核实 Host/yt-dlp 版本并使用 --probe-url 获取诊断。"
        ) from error

    raise NonRetryableYoutubeError("YouTube 解析失败，全部安全回退路径已用尽。")


def download_with_fallback(options: dict, url: str) -> None:
    def execute(active: dict) -> None:
        with yt_dlp.YoutubeDL(active) as ydl:
            code = ydl.download([url])
            if code:
                # Some downloaders return an error code instead of raising.
                # The recording logger preserves the underlying yt-dlp error
                # so that the caller can decide whether a fallback is useful.
                raise RuntimeError(f"yt-dlp 返回错误码 {code}")
    _try_without_cookies(execute, options)


def extract_with_fallback(options: dict, url: str):
    def execute(active: dict):
        with yt_dlp.YoutubeDL(active) as ydl:
            return ydl.extract_info(url, download=False)
    return _try_without_cookies(execute, options)


def node_version(node_path: str | None) -> str:
    if not node_path:
        return ""
    try:
        result = subprocess.run(
            [node_path, "--version"], capture_output=True, text=True,
            check=True, timeout=4,
        )
        value = result.stdout.strip()
        return value if re.fullmatch(r"v\d+\.\d+\.\d+", value) else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def runtime_diagnostics(node_path: str | None) -> dict:
    node = node_version(node_path)
    major = int(node[1:].split(".", 1)[0]) if node else 0
    try:
        ejs_version = importlib.metadata.version("yt-dlp-ejs")
    except importlib.metadata.PackageNotFoundError:
        ejs_version = ""
    return {
        "host_version": "1.2.3",
        "yt_dlp_version": YT_DLP_VERSION,
        "ejs_available": importlib.util.find_spec("yt_dlp_ejs") is not None,
        "ejs_version": ejs_version,
        "node_version": node,
        "node_compatible": major >= 22,
    }
