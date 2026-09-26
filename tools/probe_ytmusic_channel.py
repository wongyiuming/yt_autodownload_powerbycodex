from __future__ import annotations

import sys

from ytmusicapi import YTMusic

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CHANNEL_ID = "UC5QL_gYA1VzTkcIjwD4cxfA"
yt = YTMusic()
artist = yt.get_artist(CHANNEL_ID)
print(f"ARTIST name={artist.get('name')!r} channelId={artist.get('channelId')!r}")

all_items: dict[str, tuple[str, str]] = {}
for section_name in ("albums", "singles"):
    section = artist.get(section_name) or {}
    initial = section.get("results") or []
    print(
        f"SECTION {section_name}: initial={len(initial)} "
        f"browseId={section.get('browseId')!r} params={bool(section.get('params'))}"
    )
    items = list(initial)
    if section.get("browseId") and section.get("params"):
        items = yt.get_artist_albums(section["browseId"], section["params"], limit=None)
    print(f"SECTION {section_name}: full={len(items)}")
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        browse_id = str(item.get("browseId") or "").strip()
        audio_playlist_id = str(item.get("audioPlaylistId") or "").strip()
        key = audio_playlist_id or browse_id or title
        if key:
            all_items.setdefault(key, (title, audio_playlist_id or browse_id))
        print(
            f"{section_name.upper()} title={title!r} browseId={browse_id!r} "
            f"audioPlaylistId={audio_playlist_id!r} year={item.get('year')!r}"
        )

print(f"UNIQUE_RELEASES={len(all_items)}")
for index, (_key, (title, release_id)) in enumerate(all_items.items(), 1):
    print(f"RELEASE {index:02d} {release_id} :: {title}")
