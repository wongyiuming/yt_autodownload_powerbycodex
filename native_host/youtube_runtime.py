"""Shared yt-dlp runtime options and safe public-video fallback."""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import yt_dlp
from yt_dlp.version import __version__ as YT_DLP_VERSION


class NonRetryableYoutubeError(RuntimeError):
    """A repeated identical retry cannot fix this extraction failure."""


def youtube_options(node_path: str | None, cookie_path: Path) -> dict:
    """Prefer the bundled EJS package; GitHub is only an optional backup."""
    options: dict = {"no_plugins": True}
    if cookie_path.is_file():
        options["cookiefile"] = str(cookie_path)
    if node_path:
        options.update({
            "js_runtimes": {"node": {"path": node_path}},
            "remote_components": {"ejs:github"},
        })
    return options


def _try_without_cookies(operation, options: dict):
    """Only retry reload errors anonymously, keeping normal auth flow intact."""
    try:
        return operation(options)
    except yt_dlp.utils.DownloadError as exc:
        if "The page needs to be reloaded" not in str(exc):
            raise
        if not options.get("cookiefile"):
            raise NonRetryableYoutubeError(
                "YouTube 页面要求重新加载；匿名解析仍失败。请检查 yt-dlp/EJS 版本及网络状态。"
            ) from exc
        logger = options.get("logger")
        if logger:
            logger.warning("YouTube 拒绝了携带 Cookie 的解析请求，尝试匿名访问公开资源。")
        anonymous = dict(options)
        anonymous.pop("cookiefile", None)
        try:
            return operation(anonymous)
        except yt_dlp.utils.DownloadError as anonymous_exc:
            raise NonRetryableYoutubeError(
                "Cookie 和匿名访问均未成功；如为登录专属视频，请检查账号 Cookie；公开视频请检查 yt-dlp/EJS。"
            ) from anonymous_exc


def download_with_fallback(options: dict, url: str) -> None:
    def execute(active: dict) -> None:
        with yt_dlp.YoutubeDL(active) as ydl:
            code = ydl.download([url])
            if code:
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
    return {
        "yt_dlp_version": YT_DLP_VERSION,
        "ejs_available": importlib.util.find_spec("yt_dlp_ejs") is not None,
        "node_version": node,
        "node_compatible": major >= 22,
    }
