from __future__ import annotations

from pathlib import Path
import sys
import tempfile

import yt_dlp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native_host"))

from album_support import safe_component  # noqa: E402
from youtube_album_catalog import fetch_channel_album_cards  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

URL = "https://www.youtube.com/channel/UC5QL_gYA1VzTkcIjwD4cxfA"
EXPECTED_ALBUMS = 40

channel_title, cards, shelf_found = fetch_channel_album_cards(URL)
print(f"channel={channel_title!r} shelf={shelf_found} albums={len(cards)} expected={EXPECTED_ALBUMS}")
if not shelf_found:
    raise SystemExit("album shelf classification failed")
if len(cards) != EXPECTED_ALBUMS:
    for index, card in enumerate(cards, 1):
        print(f"ALBUM {index:02d} {card.playlist_id} :: {card.title}")
    raise SystemExit(f"expected {EXPECTED_ALBUMS} albums, got {len(cards)}")

expected_titles = {"石頭記", "TM+M DECADE(NCE)", "Shen Jing"}
actual_titles = {card.title for card in cards}
missing = expected_titles - actual_titles
if missing:
    raise SystemExit(f"missing known album titles: {sorted(missing)}")

with tempfile.TemporaryDirectory(prefix="yt-album-smoke-") as temp:
    root = Path(temp) / safe_component(channel_title, "频道")
    root.mkdir(parents=True)
    for card in cards:
        (root / safe_component(card.title, "未命名专辑")).mkdir(parents=True, exist_ok=True)

    created = [path for path in root.iterdir() if path.is_dir()]
    print(f"created_folders={len(created)}")
    if len(created) != EXPECTED_ALBUMS:
        print("CREATED_NAMES", sorted(path.name for path in created))
        raise SystemExit(f"expected {EXPECTED_ALBUMS} unique album folders, created {len(created)}")

    for title in sorted(expected_titles):
        folder = safe_component(title, "未命名专辑")
        path = root / folder
        print(f"CHECK_FOLDER {path}")
        if not path.is_dir():
            raise SystemExit(f"folder was not created: {folder}")

stone = next(card for card in cards if card.title == "石頭記")
options = {
    "quiet": True,
    "no_warnings": False,
    "extract_flat": "in_playlist",
    "skip_download": True,
    "ignoreerrors": False,
}
with yt_dlp.YoutubeDL(options) as ydl:
    info = ydl.extract_info(stone.url, download=False)
entries = [item for item in (info or {}).get("entries") or [] if isinstance(item, dict)]
print(f"stone_album_playlist={stone.playlist_id} tracks={len(entries)}")
if not entries:
    raise SystemExit("album playlist did not expose track metadata")

print("SMOKE_OK: 40 browser-visible albums -> 40 album folders -> playlist track metadata")
