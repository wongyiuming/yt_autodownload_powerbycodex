"""Transport/response failures reach the popup without hiding behind 'network'.

No personal credentials or live external requests are used. The signature
vector is calculated independently by Node from the published H5 algorithm.
"""
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import time
import traceback
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_host'))
from kugou import KugouError
from kugou_diagnostics import Diagnostic, Failure, public_diagnostic
from kugou_library import KugouLibraryClient, KugouSession
from kugou_preview import KugouPreviewStore, KugouPreviewError

TOKEN = 'fixture-secret+not-for-logs'
SESSION = KugouSession('123', TOKEN, 'fixture-mid', 'fixture-dfid')


def row(n):
    return {'listid': n, 'type': 0, 'name': f'List {n}', 'm_count': 1, 'list_ver': 1}


class DiagnosticTests(unittest.TestCase):
    def client(self, handler):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        return KugouLibraryClient(SESSION, http)

    def preview(self, client):
        store = KugouPreviewStore(client_factory=lambda _: client)
        self.addCleanup(store.close)
        record = store.start(SESSION)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = store.get(record['preview_id'])
            if record['status'] != 'loading':
                return store, record
            time.sleep(.002)
        self.fail('Preview worker did not finish')

    def assert_failure(self, client, code, **fields):
        store, record = self.preview(client)
        self.assertEqual(record['status'], 'error')
        self.assertEqual(record['diagnostic']['code'], code)
        for key, value in fields.items():
            self.assertEqual(record['diagnostic'][key], value)
        self.assertEqual(record['playlists'], [])
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn('signature=', serialized)
        self.assertNotIn('errmsg', serialized)
        with self.assertRaises(KugouPreviewError):
            store.consume(record['preview_id'], SESSION.user_id)
        return record

    def test_http_status_and_path_survive_preview_without_body_or_token(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                self.assert_failure(self.client(lambda _, s=status: httpx.Response(s, text=TOKEN)),
                                    'HTTP_ERROR', http_status=status, page=1,
                                    endpoint='/v7/get_all_list')

    def test_business_rejection_is_not_a_network_failure(self):
        for code in (900, -2, '1234'):
            with self.subTest(code=code):
                response = httpx.Response(200, json={'status': 0, 'error_code': code, 'errmsg': TOKEN})
                self.assert_failure(self.client(lambda _: response), 'API_REJECTED',
                                    http_status=200, api_code=int(code))

    def test_auth_rejection_is_not_a_claim_that_webpage_is_logged_out(self):
        for response in (httpx.Response(401, text=TOKEN),
                         httpx.Response(200, json={'status': 0, 'error_code': 20010, 'msg': TOKEN})):
            self.assert_failure(self.client(lambda _: response), 'SESSION_REJECTED')

    def test_invalid_json_and_schema_have_distinct_diagnostics(self):
        cases = [(httpx.Response(200, text=TOKEN), 'INVALID_JSON'),
                 (httpx.Response(200, json=[]), 'INVALID_RESPONSE'),
                 (httpx.Response(200, json={'status': 1, 'data': {'info': TOKEN}}), 'INVALID_RESPONSE')]
        for response, code in cases:
            with self.subTest(code=code):
                self.assert_failure(self.client(lambda _: response), code)

    def test_safety_challenge_is_reported_not_bypassed(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(403, headers={'ssa-code': TOKEN}, text=TOKEN)
        self.assert_failure(self.client(handler), 'VERIFICATION_REQUIRED')
        self.assertEqual(len(calls), 1)

    def test_redirect_does_not_forward_credentials(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': 'https://example.invalid/?token=' + TOKEN})
        self.assert_failure(self.client(handler), 'REDIRECT_BLOCKED')
        self.assertEqual(len(calls), 1)

    def test_transport_errors_are_classified_without_exception_strings(self):
        cases = [(httpx.ReadTimeout, 'NETWORK_TIMEOUT'), (httpx.ProxyError, 'NETWORK_PROXY'),
                 (httpx.ConnectError, 'NETWORK_CONNECT'), (httpx.ReadError, 'NETWORK_TRANSPORT')]
        for cls, code in cases:
            def handler(request):
                raise cls('secret request: ' + str(request.url), request=request)
            with self.subTest(code=code):
                client = self.client(handler)
                self.assert_failure(client, code)
                try:
                    client.list_playlists()
                except KugouError:
                    trace = traceback.format_exc()
                self.assertNotIn(TOKEN, trace)
                self.assertNotIn('signature=', trace)

    def test_dns_and_tls_chain_types_are_safe_without_exception_messages(self):
        for cause, code in ((socket.gaierror('secret ' + TOKEN), 'NETWORK_DNS'),
                            (ssl.SSLError('secret ' + TOKEN), 'NETWORK_TLS')):
            def handler(request):
                raise httpx.ConnectError('secret ' + str(request.url)) from cause
            with self.subTest(code=code):
                self.assert_failure(self.client(handler), code)

    def test_second_page_failure_keeps_page_and_does_not_publish_partial_result(self):
        def handler(request):
            number = json.loads(request.content)['page']
            if number == 1:
                return httpx.Response(200, json={'status': 1, 'data': {'info': [row(1)], 'count': 2}})
            return httpx.Response(502, text=TOKEN)
        self.assert_failure(self.client(handler), 'HTTP_ERROR', page=2, http_status=502)

    def test_unknown_exceptions_expose_only_fixed_type_and_no_message(self):
        class BrokenClient:
            def list_playlists(self, **_):
                raise TypeError('request https://example.invalid/?token=' + TOKEN)
            def close(self):
                pass
        record = self.assert_failure(BrokenClient(), 'INTERNAL_ERROR', exception_type='TypeError')
        self.assertNotIn('example.invalid', json.dumps(record))

    def test_shape_is_types_only_and_unknown_keys_are_omitted(self):
        client = self.client(lambda _: httpx.Response(200, json={
            'status': 1, 'data': {'info': TOKEN, TOKEN: TOKEN, 'count': 'bad', 'list_count': 99}}))
        record = self.assert_failure(client, 'INVALID_RESPONSE', check='rows')
        self.assertEqual(record['diagnostic']['shape'], {'info': 'string', 'count': 'string', 'list_count': 'number'})

    def test_category_counters_are_not_added_to_make_a_false_total(self):
        calls = []
        def handler(request):
            n = json.loads(request.content)['page']
            calls.append(n)
            return httpx.Response(200, json={'status': 1, 'data': {
                'list_count': 166, 'collect_count': 47, 'album_count': 0,
                'info': [row(n)] if n < 3 else [], 'total_ver': 4316}})
        store, result = self.preview(self.client(handler))
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['playlist_count'], 2)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(len(store.consume(result['preview_id'], '123')), 2)

    def test_alternate_row_container_is_supported_without_treating_missing_as_empty(self):
        for key in ('info', 'songs', 'lists', 'list', 'items', 'file'):
            with self.subTest(key=key):
                client = self.client(lambda _: httpx.Response(200, json={'status': 1, 'data': {key: [row(1)], 'count': 1}}))
                self.assertEqual(len(client.list_playlists()), 1)
        self.assert_failure(self.client(lambda _: httpx.Response(200, json={'status': 1, 'data': {}})),
                            'INVALID_RESPONSE', check='rows')

    def test_response_account_mismatch_is_not_a_ready_preview(self):
        self.assert_failure(self.client(lambda _: httpx.Response(200, json={
            'status': 1, 'data': {'userid': 456, 'info': [], 'count': 0}})),
            'INVALID_RESPONSE', check='user_id')

    def test_malformed_diagnostic_fields_cannot_reflect_secret_strings(self):
        d = Diagnostic(TOKEN, TOKEN, True, 999999, TOKEN, TOKEN,
                       ((TOKEN, TOKEN), ('info', TOKEN)), TOKEN, TOKEN, True)
        self.assertEqual(d.public(), {'code': 'INTERNAL_ERROR', 'stage': 'preview'})
        self.assertNotIn(TOKEN, d.message())

    @unittest.skipUnless(shutil.which('node'), 'Node required for independent JavaScript parity')
    def test_h5_request_signature_matches_independent_node_algorithm(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, json={'status': 1, 'data': {'info': [], 'count': 0}})
        with patch('kugou_library.time.time', return_value=1804134600):
            self.client(handler).list_playlists()
        request = captured[0]
        params = dict(request.url.params)
        signature = params.pop('signature')
        js = """const crypto = require('crypto'); const fs = require('fs');
const {params,body} = JSON.parse(fs.readFileSync(0,'utf8'));
const s='NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt';
const pairs=Object.keys(params).map(k=>`${k}=${params[k]}`).sort().join('');
process.stdout.write(crypto.createHash('md5').update(s+pairs+body+s).digest('hex'));"""
        actual = subprocess.run(['node', '-e', js], input=json.dumps({'params': params, 'body': request.content.decode()}),
                                text=True, encoding='utf-8', capture_output=True, check=True, timeout=10).stdout
        self.assertEqual(signature, actual)
        self.assertEqual(params['appid'], '1014')
        self.assertEqual(params['clienttime'], '1804134600000')
        self.assertNotIn('cookie', request.headers)
        self.assertNotIn('authorization', request.headers)

    @unittest.skipUnless(shutil.which('node'), 'Node required for popup regression')
    def test_popup_diagnostic_and_error_status_regressions(self):
        subprocess.run(['node', str(ROOT / 'tests/test_kugou_diagnostics_ui.js')],
                       cwd=ROOT, check=True, timeout=10, capture_output=True)


if __name__ == '__main__':
    unittest.main()
