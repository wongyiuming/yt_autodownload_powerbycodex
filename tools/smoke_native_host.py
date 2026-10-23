"""Native Messaging full-process handshake for Windows CI."""

from __future__ import annotations

import json
import struct
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_native_host.py <host-exe>")
    request = json.dumps({"id": "smoke-hello", "action": "hello"}).encode("utf-8")
    wire = struct.pack("<I", len(request)) + request
    proc = subprocess.run(
        [sys.argv[1]], input=wire, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=35, check=False,
    )
    if proc.returncode:
        raise AssertionError(f"host failed: code={proc.returncode}: {proc.stderr[:500]!r}")
    output = proc.stdout
    messages = []
    while len(output) >= 4:
        length = struct.unpack("<I", output[:4])[0]
        output = output[4:]
        if length > len(output):
            raise AssertionError("Native Messaging response frame truncated")
        messages.append(json.loads(output[:length]))
        output = output[length:]
    if output:
        raise AssertionError(f"Unframed host stdout: {output[:100]!r}")
    hello = next((m for m in messages if m.get("event") == "hello"), None)
    reply = next((m for m in messages if m.get("reply_to") == "smoke-hello"), None)
    if not hello or not reply or not reply.get("ok"):
        raise AssertionError(f"Native Messaging hello failed: {messages!r}")
    if hello["host"]["version"] != "1.3.0" or reply["host"]["version"] != "1.3.0":
        raise AssertionError(f"Wrong runtime version: {messages!r}")
    print(json.dumps({
        "ok": True, "runtime_version": hello["host"]["version"],
        "yt_dlp": hello["host"].get("yt_dlp_version"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
