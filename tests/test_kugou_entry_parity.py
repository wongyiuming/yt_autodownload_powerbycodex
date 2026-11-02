"""Synthetic regressions for the same song entering through different sources.

No live account, network, payment checks or third-party playback services.
The paid-looking response below deliberately models a missing-ID request,
not an assertion about the meaning of any real Kugou error code.
"""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_host'))
from kugou import KugouClient, KugouError, KugouTrack, _track
from kugou_library import KugouLibraryClient, KugouPlaylist, KugouSession, _library_track
from kugou_queue import download_kugou
from platform_urls import classify_source

HASH = 'A' * 32
PAGE = 'https://www.kugou.com/mixsong/fixture123.html'
SESSION = KugouSession('123', 'synthetic-session-do-not-log')
CANONICAL = f'https://www.kugou.com/song/#hash={HASH}&album_id=12&album_audio_id=34'
BASE_ROW = {'hash': HASH, 'album_id': '12', 'mixsongid': '34', 'songname': 'Fixture',
            'singername': 'Artist', 'timelen': 180000}
WIRE_ROW = {'FileHash': HASH.lower(), 'AlbumID': 12, 'MixSongID': 34,
            'SongName': 'Fixture', 'SingerName': 'Artist', 'Duration': 180}


class Cancelled(Exception):
    pass


def host_facade():
    return SimpleNamespace(now_iso=lambda: 'synthetic-time', HostLogger=lambda: Mock())


class EntryParityTests(unittest.TestCase):
    def client(self, handler, *, library=False):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        return KugouLibraryClient(SESSION, http) if library else KugouClient(http)

    def test_shared_normalization_keeps_identity_and_duration(self):
        expected = _library_track(BASE_ROW, position=1)
        for hash_key in ('hash', 'HASH', 'FileHash', 'filehash', 'Hash'):
            for audio_key in ('mixsongid', 'album_audio_id', 'add_mixsongid', 'MixSongID', 'AlbumAudioID'):
                row = {**WIRE_ROW, hash_key: HASH.lower(), audio_key: 34}
                row.pop('FileHash', None)
                row.pop('MixSongID', None)
                row[hash_key] = HASH.lower()
                row[audio_key] = 34
                with self.subTest(hash=hash_key, audio=audio_key):
                    self.assertEqual(_track(row), expected)
                    self.assertEqual(_library_track(row, position=1), expected)
        self.assertEqual(expected.url, CANONICAL)
        self.assertEqual(expected.duration, 180)

    def test_modern_single_page_uses_same_metadata_as_library(self):
        page = 'var dataFromSmarty = ' + json.dumps([WIRE_ROW]) + ';'
        client = self.client(lambda _: httpx.Response(200, text=page))
        actual = client.resolve_collection(PAGE).tracks[0]
        self.assertEqual(actual, _library_track(BASE_ROW, position=1))

    def test_canonical_url_roundtrip_keeps_audio_id(self):
        client = self.client(lambda _: self.fail('hash URL must not request metadata'))
        variants = [CANONICAL, CANONICAL.replace('album_audio_id', 'mixsongid'),
                    f'https://www.kugou.com/song/?album_audio_id=34#hash={HASH}&album_id=12',
                    f'https://www.kugou.com/yy/index.php?r=play/getdata&hash={HASH}&album_id=12&mixsongid=34']
        for url in variants:
            with self.subTest(url=url):
                self.assertEqual(classify_source(url).url, CANONICAL)
                song = client.resolve_collection(url).tracks[0]
                self.assertEqual((song.hash, song.album_id, song.mixsongid), (HASH, '12', '34'))
                self.assertEqual(classify_source(classify_source(url).url).url, CANONICAL)

    def test_mixsong_link_hints_fill_missing_page_metadata(self):
        row = {"hash": HASH, "name": "Fixture"}
        client = self.client(lambda _: httpx.Response(200, text='var dataFromSmarty = ' + json.dumps([row]) + ';'))
        url = PAGE + '?album_id=12&mixsongid=34&utm_source=ignored'
        canonical = classify_source(url).url
        self.assertEqual(canonical, PAGE + '?album_id=12&album_audio_id=34')
        song = client.resolve_collection(url).tracks[0]
        self.assertEqual((song.hash, song.album_id, song.mixsongid), (HASH, '12', '34'))

    def test_mixsong_link_cannot_override_a_different_recording(self):
        client = self.client(lambda _: httpx.Response(200, text='var dataFromSmarty = ' + json.dumps([BASE_ROW]) + ';'))
        for suffix in ('?hash=' + 'B' * 32, '?album_id=13', '?album_audio_id=35'):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(KugouError, '标识不一致'):
                client.resolve_collection(PAGE + suffix)

    def test_invalid_or_conflicting_audio_ids_never_expand_to_library(self):
        for suffix in ('&album_audio_id=abc', '&album_audio_id=-2', '&album_audio_id=3.4',
                       '&album_audio_id=123456789012345678901', '&album_audio_id=34&mixsongid=35'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                classify_source(f'https://www.kugou.com/song/#hash={HASH}' + suffix)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for cross-runtime URL comparison')
    def test_javascript_python_url_parity_preserves_audio_identifier(self):
        urls = [PAGE + '?album_id=12&mixsongid=34', PAGE + '?album_audio_id=34&mixsongid=35', CANONICAL, CANONICAL.replace('album_audio_id', 'mixsongid'),
                f'https://www.kugou.com/song/?mixsongid=34#hash={HASH}&album_id=12',
                CANONICAL + '&mixsongid=34', CANONICAL + '&mixsongid=35',
                CANONICAL.replace('=34', '=broken'),
                f'https://www.kugou.com/song/#hash={HASH}&album_audio_id=9007199254740993']
        script = "const {classifySourceUrl:c}=require('./extension/platform_urls.js');const fs=require('fs');process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8')).map(c)));"
        result = subprocess.run(['node', '-e', script], input=json.dumps(urls), text=True,
                                capture_output=True, check=True, cwd=ROOT, timeout=10)
        for url, js in zip(urls, json.loads(result.stdout)):
            try:
                py = classify_source(url)
            except ValueError:
                self.assertEqual(js['kind'], 'unknown')
            else:
                self.assertEqual((js['kind'], js['normalizedUrl']), (py.kind, py.url))

    def test_incomplete_identity_cannot_be_reported_as_payment_rejection(self):
        for key in ('AlbumID', 'MixSongID'):
            for value in ('secret-invalid', True, {}, []):
                row = {**WIRE_ROW, key: value}
                with self.subTest(key=key, value=value), self.assertRaisesRegex(KugouError, '标识'):
                    _track(row)

    def test_no_cache_library_and_single_use_identical_playback_parameters(self):
        # Force the web fallback to require the identifier in this synthetic API.
        # This models why "upstream says paid" is not an account diagnosis.
        calls = []
        def handler(request):
            self.assertNotIn('cookie', request.headers)
            self.assertNotIn('token', request.url.params)
            calls.append(request)
            if request.url.host == 'm.kugou.com':
                return httpx.Response(200, json={'status': 0, 'error': '需要付费'})
            self.assertEqual(request.url.params['album_audio_id'], '34')
            self.assertEqual(request.url.params['album_id'], '12')
            self.assertEqual(request.url.params['hash'], HASH)
            return httpx.Response(200, json={'status': 1, 'data': {
                'play_url': 'https://fs.kugou.com/fixture.mp3', 'timelength': 180000,
                'extname': 'mp3', 'filesize': 6, 'audio_name': 'Fixture'}})
        for client, song in [(self.client(handler), _track(WIRE_ROW)),
                             (self.client(handler, library=True), _library_track(BASE_ROW, position=1)),
                             (self.client(handler), self.client(handler).resolve_collection(CANONICAL).tracks[0])]:
            client._mid = 'fixed-device-for-comparison'
            self.assertEqual(client.resolve_track(song).url, 'https://fs.kugou.com/fixture.mp3')
        self.assertEqual(len(calls), 6)
        self.assertEqual([dict(c.url.params) for c in calls[:2]], [dict(c.url.params) for c in calls[2:4]])
        self.assertEqual([dict(c.url.params) for c in calls[:2]], [dict(c.url.params) for c in calls[4:]])

    def test_legacy_hash_only_mobile_success_is_unchanged(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'url': 'https://fs.kugou.com/fixture.mp3', 'extName': 'mp3'})
        client = self.client(handler)
        client.resolve_track(KugouTrack(HASH, hash=HASH))
        self.assertEqual(len(calls), 1)
        self.assertEqual(dict(calls[0].url.params), {'cmd': 'playInfo', 'hash': HASH})

    def test_paid_looking_error_is_labeled_as_endpoint_result_not_account_fact(self):
        def handler(request):
            if request.url.host == 'm.kugou.com':
                return httpx.Response(200, json={'status': 0, 'error': '需要付费'})
            return httpx.Response(200, json={'status': 0, 'err_code': 30020, 'data': {}})
        with self.assertRaises(KugouError) as caught:
            self.client(handler).resolve_track(_track(WIRE_ROW))
        text = str(caught.exception)
        for expected in ('不是当前账号权益结论', '移动端匿名接口', '网页匿名接口', '需要付费', '30020'):
            self.assertIn(expected, text)

    def test_previews_and_encrypted_resources_remain_rejected(self):
        song = _track(BASE_ROW)
        for payload in ({'url': 'https://fs.kugou.com/f.mp3', 'extName': 'mp3', 'is_free_part': 1},
                        {'url': 'https://fs.kugou.com/f.kgm', 'extName': 'kgm'},
                        {'url': 'https://fs.kugou.com/f.mp3', 'extName': 'mp3', 'timeLength': 30}):
            with self.subTest(payload=payload), self.assertRaises(KugouError):
                KugouClient._media(payload, song, mobile=True)

    def task_manager(self, root, url, *, library=False):
        task = SimpleNamespace(id='task', url=url, mode='audio', kugou_confirmed=library,
                               kugou_user_id='123' if library else '')
        manager = SimpleNamespace(download_root=Path(root), lock=threading.RLock(), cancelled=set(),
                                  emit=lambda: None, _format_speed=lambda _: '', _format_eta=lambda _: '',
                                  _kugou_sessions={'task': SESSION},
                                  _kugou_plans={'task': [KugouPlaylist('2', 'Fixture list', 1)]})
        return task, manager

    def test_library_download_then_mixsong_single_reuses_identical_file(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.path == '/v4/get_list_all_file':
                return httpx.Response(200, json={'status': 1, 'error_code': 0,
                                                'data': {'info': [BASE_ROW], 'count': 1}})
            if request.url.path == '/mixsong/fixture123.html':
                return httpx.Response(200, text='var dataFromSmarty = ' + json.dumps([WIRE_ROW]) + ';')
            if request.url.host == 'm.kugou.com':
                return httpx.Response(200, json={'url': 'https://fs.kugou.com/fixture.mp3',
                                                'extName': 'mp3', 'fileSize': 6, 'timeLength': 180})
            self.assertEqual(request.url.host, 'fs.kugou.com')
            return httpx.Response(200, content=b'abcdef', headers={'content-type': 'audio/mpeg'})
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'host_legacy': host_facade()}):
            task, manager = self.task_manager(tmp, 'https://www.kugou.com/', library=True)
            download_kugou(manager, task, Cancelled, client=self.client(handler, library=True))
            first_calls = len(calls)
            self.assertEqual(first_calls, 3)
            song_task, manager = self.task_manager(tmp, PAGE)
            download_kugou(manager, song_task, Cancelled, client=self.client(handler))
            # Single page metadata is needed, but no new playback or media request.
            self.assertEqual(len(calls), first_calls + 1)
            files = list((Path(tmp) / 'audio/kugou/_单曲').glob('*.mp3'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b'abcdef')
            self.assertEqual(song_task.progress, 100)
            self.assertIn('复用', song_task.current_title)

    def test_existing_v143_archive_hash_url_reuses_without_any_http(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'host_legacy': host_facade()}):
            root = Path(tmp) / 'audio/kugou'; root.mkdir(parents=True)
            (root / 'old.mp3').write_bytes(b'existing-complete-file')
            (root / 'archive-audio.json').write_text(json.dumps({HASH: 'old.mp3'}), encoding='utf-8')
            task, manager = self.task_manager(tmp, CANONICAL)
            client = self.client(lambda _: self.fail('valid archived hash must not request any HTTP'))
            download_kugou(manager, task, Cancelled, client=client)
            self.assertEqual(next((root / '_单曲').glob('*.mp3')).read_bytes(), b'existing-complete-file')

    def test_missing_archived_file_does_not_claim_reuse(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'host_legacy': host_facade()}):
            root = Path(tmp) / 'audio/kugou'; root.mkdir(parents=True)
            (root / 'archive-audio.json').write_text(json.dumps({HASH: 'missing.mp3'}), encoding='utf-8')
            task, manager = self.task_manager(tmp, CANONICAL)
            client = self.client(lambda _: httpx.Response(200, json={'status': 0, 'error': 'unavailable'}))
            with self.assertRaises(RuntimeError):
                download_kugou(manager, task, Cancelled, client=client)
            self.assertEqual(list((root / '_单曲').glob('*.mp3')), [])

    def test_different_hash_with_same_title_does_not_reuse_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {'host_legacy': host_facade()}):
            root = Path(tmp) / 'audio/kugou'; root.mkdir(parents=True)
            (root / 'Artist - Fixture.mp3').write_bytes(b'different-recording')
            (root / 'archive-audio.json').write_text(json.dumps({'B' * 32: 'Artist - Fixture.mp3'}), encoding='utf-8')
            task, manager = self.task_manager(tmp, CANONICAL)
            client = self.client(lambda _: httpx.Response(200, json={'status': 0, 'error': 'unavailable'}))
            with self.assertRaises(RuntimeError):
                download_kugou(manager, task, Cancelled, client=client)
            self.assertEqual(list((root / '_单曲').glob('*.mp3')), [])


if __name__ == '__main__':
    unittest.main()
