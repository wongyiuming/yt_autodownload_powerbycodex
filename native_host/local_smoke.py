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


def smoke_local_mp3() -> dict:
    """Download a self-generated local WAV and convert it using the real app path."""
    with tempfile.TemporaryDirectory(prefix="yt-ad-smoke-") as directory:
        root = Path(directory)
        source = root / "fixture.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            samples = (
                int(14000 * math.sin(2 * math.pi * 440 * index / 16000))
                for index in range(8000)
            )
            audio.writeframes(struct.pack("<" + "h" * 8000, *samples))

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
