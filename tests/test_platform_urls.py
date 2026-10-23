"""Source-page regressions, shared with the popup's JavaScript classifier."""

from dataclasses import asdict
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native_host"))

from platform_urls import classify_source
from yt_dlp.extractor.bilibili import (
    BiliBiliIE,
    BilibiliCollectionListIE,
    BilibiliFavoritesListIE,
    BilibiliPlaylistIE,
    BilibiliSeriesListIE,
)


CASES = json.loads((ROOT / "tests/fixtures/platform_urls.json").read_text(encoding="utf-8"))


class PlatformUrlTests(unittest.TestCase):
    def test_supported_sources_and_canonical_urls(self):
        for case in CASES["valid"]:
            with self.subTest(url=case["raw"]):
                actual = classify_source(case["raw"])
                self.assertEqual(asdict(actual), {key: case[key] for key in ("platform", "kind", "url")})
                self.assertEqual(classify_source(actual.url), actual)

    def test_reject_unsupported_and_misleading_sources(self):
        for raw in CASES["invalid"]:
            with self.subTest(url=raw), self.assertRaises(ValueError):
                classify_source(raw)

    def test_canonical_bilibili_urls_match_installed_ytdlp_extractors(self):
        """Catch support claims the pinned yt-dlp cannot actually dispatch."""
        extractors = [BiliBiliIE, BilibiliCollectionListIE, BilibiliFavoritesListIE,
                      BilibiliPlaylistIE, BilibiliSeriesListIE]
        for case in CASES["valid"]:
            if case["platform"] != "bilibili" or case["kind"] == "shortlink":
                continue
            with self.subTest(url=case["url"]):
                matched = [ie for ie in extractors if ie.suitable(case["url"])]
                self.assertEqual(len(matched), 1, matched)
                if case["kind"] == "favorites":
                    self.assertIs(matched[0], BilibiliFavoritesListIE)


if __name__ == "__main__":
    unittest.main()
