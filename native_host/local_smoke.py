"""A local-only real yt-dlp + FFmpeg/MP3 smoke test for the frozen EXE."""

from __future__ import annotations

import functools
import math
import struct
import tempfile
import threading
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import imageio_ffmpeg

from youtube_runtime import download_with_fallback


class _QuietLocalHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args) -> None:
        pass


def _write_tone(source: Path) -> None:
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        samples = (int(14000 * math.sin(2 * math.pi * 440 * index / 16000)) for index in range(8000))
        audio.writeframes(struct.pack("<" + "h" * 8000, *samples))


def smoke_local_mp3() -> dict:
    """Download a self-generated local WAV and convert it using the real app path."""
    with tempfile.TemporaryDirectory(prefix="yt-ad-smoke-") as directory:
        root = Path(directory)
        source = root / "fixture.wav"
        _write_tone(source)

        handler = functools.partial(_QuietLocalHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/fixture.wav"
            output = root / "smoke.%(ext)s"
            options = {
                "quiet": True, "no_warnings": False,
                "noplaylist": True,
                "outtmpl": str(output),
                "format": "bestaudio/best",
                "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                }],
            }
            download_with_fallback(options, url)
            mp3 = root / "smoke.mp3"
            if not mp3.is_file() or mp3.stat().st_size < 100:
                raise RuntimeError("yt-dlp returned success but output MP3 is missing.")
            return {"ok": True, "mp3_bytes": mp3.stat().st_size}
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)


def smoke_local_kugou() -> dict:
    """Exercise the frozen HTTP downloader, FFmpeg and archive using local audio.

    Only source metadata is a fixture; bytes travel through a real local HTTP
    server and the same Kugou download/MP3/queue path used in production.
    """
    import httpx
    from types import SimpleNamespace
    from host_legacy import CancelledError, QueueManager, Task
    from kugou import KugouClient, KugouCollection, KugouMedia, KugouTrack
    from kugou_queue import download_kugou

    with tempfile.TemporaryDirectory(prefix="kugou-smoke-") as directory:
        root = Path(directory)
        source = root / "fixture.wav"
        _write_tone(source)
        handler = functools.partial(_QuietLocalHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        transfers = []

        class FixtureClient(KugouClient):
            def resolve_collection(self, url, **_):
                return KugouCollection("local", "Local HTTP audio", [KugouTrack("local", title="Local fixture")])

            def resolve_track(self, track, **_):
                transfers.append(track.id)
                return KugouMedia(f"http://127.0.0.1:{server.server_port}/fixture.wav",
                                  "Local fixture", "wav", 0.5, source.stat().st_size)

        try:
            manager = SimpleNamespace(download_root=root / "output", lock=threading.RLock(),
                                      cancelled=set(), emit=lambda **_: None,
                                      ffmpeg_path=imageio_ffmpeg.get_ffmpeg_exe(),
                                      _format_speed=QueueManager._format_speed,
                                      _format_eta=QueueManager._format_eta)
            task = Task("local", "https://www.kugou.com/yy/special/single/1.html", "playlist", "audio", platform="kugou")
            with httpx.Client(trust_env=False) as http_client:
                client = FixtureClient(http_client)
                download_kugou(manager, task, CancelledError, client=client)
                download_kugou(manager, task, CancelledError, client=client)
            files = list(manager.download_root.rglob("*.mp3"))
            if len(files) != 1 or files[0].stat().st_size < 100 or transfers != ["local"]:
                raise RuntimeError("HTTP/FFmpeg/MP3 或酷狗下载去重验证失败")
            return {"ok": True, "backend": "httpx", "mp3_bytes": files[0].stat().st_size,
                    "transfers": len(transfers), "deduped": True}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
