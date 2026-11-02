import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from bilialbum.models import NetworkFailure
from bilialbum.network import (safe_url, proxy_summary, validate_proxies,
                               exception_details, network_failure, gui_error_message)


class NetworkTests(unittest.TestCase):
    def test_redact_url_credentials_tracking_and_signatures(self):
        result = safe_url('https://name:secret@api.bilibili.com/x/player/wbi/playurl?bvid=BV0000000000&cid=12&w_rid=secret&access_token=secret&SESSDATA=secret#secret')
        self.assertEqual(result, 'https://api.bilibili.com/x/player/wbi/playurl?bvid=BV0000000000&cid=12')
        self.assertEqual(safe_url('https://other.example/secret/token?access_token=secret'), 'https://other.example/')

    def test_proxy_summary_hides_credentials_host_and_no_proxy_domains(self):
        summary = str(proxy_summary({'https': 'http://alice:secret@private.example:8080',
                                     'no': 'private.example,localhost', 'http': 'http://host:bad'}))
        for forbidden in ('alice', 'secret', 'private.example'):
            self.assertNotIn(forbidden, summary)
        self.assertIn('invalid', summary)

    def test_invalid_proxies_fail_before_request_and_hide_values(self):
        for value in ('localhost:8080', 'http://alice:secret@host:bad',
                      'http://host:99999', 'ftp://host:80', 'http://host:0',
                      'http://host/path?token=secret'):
            with self.subTest(value=value):
                with self.assertRaises(NetworkFailure) as caught:
                    validate_proxies({'https': value})
                self.assertEqual(caught.exception.category, 'invalid_proxy')
                self.assertNotIn('secret', str(caught.exception))

    def test_valid_proxies_do_not_mutate_configuration(self):
        proxies = {'https': 'http://name:secret@localhost:8080', 'no': '*.local'}
        before = dict(proxies)
        validate_proxies(proxies)
        self.assertEqual(proxies, before)

    def test_nested_dns_tls_timeout_http_are_distinct(self):
        cases = [(URLError(socket.gaierror(-2, 'private')), (None, 'dns')),
                 (URLError(ssl.SSLError('private')), (None, 'tls')),
                 (URLError(TimeoutError()), (None, 'timeout')),
                 (HTTPError('https://example.com', 412, 'blocked', {}, None), (412, 'server_denied'))]
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(exception_details(exc), expected)

    def test_blocked_gui_message_has_stage_and_no_false_success(self):
        error = network_failure('yt-dlp 播放信息', HTTPError('https://example.com', 412, '', {}, None))
        text = gui_error_message(error)
        self.assertIn('播放信息', text)
        self.assertIn('停止自动重试', text)
        self.assertIn('其他正常可访问的视频', text)
        self.assertFalse(error.retryable)

    def test_api_invalid_proxy_makes_no_requests(self):
        from bilialbum.api import BiliAPI
        with patch('urllib.request.getproxies', return_value={'https': 'http://host:invalid'}), patch('urllib.request.urlopen') as opened:
            with self.assertRaises(NetworkFailure):
                BiliAPI().get('/x/web-interface/view', {'bvid': 'BV0000000000'})
        opened.assert_not_called()

class AppDirectTests(unittest.TestCase):
    def test_api_direct_uses_local_opener_without_global_changes(self):
        import io
        import json
        import os
        from types import SimpleNamespace
        from bilialbum.api import BiliAPI
        before = dict(os.environ)
        data = io.BytesIO(json.dumps({'code': 0, 'data': {'bvid': 'BV0000000000'}}).encode())
        with patch('urllib.request.getproxies', return_value={'https': 'http://invalid:bad'}), patch('urllib.request.build_opener', return_value=SimpleNamespace(open=lambda *a, **k: data)) as built, patch('urllib.request.urlopen') as global_open:
            result = BiliAPI(direct=True).get('/x/web-interface/view', {'bvid': 'BV0000000000'})
        self.assertEqual(result['bvid'], 'BV0000000000')
        self.assertEqual(built.call_args.args[0].proxies, {})
        global_open.assert_not_called()
        self.assertEqual(dict(os.environ), before)

    def test_resolver_direct_propagates_to_shortlink_and_api(self):
        from bilialbum.resolver import Resolver
        from bilialbum.urltools import ParsedURL
        resolver = Resolver(direct=True)
        self.assertTrue(resolver.api.direct)
        with patch('bilialbum.resolver.normalize_url', return_value=ParsedURL('test', video_id='BV0000000000')) as norm, patch('bilialbum.resolver.extract_view', return_value=({'bvid': 'BV0000000000'}, None)) as extracted:
            resolver.resolve('test')
        norm.assert_called_once_with('test', direct=True)
        self.assertTrue(extracted.call_args.kwargs['direct'])

    def test_ydl_direct_uses_supported_proxy_option(self):
        from bilialbum.extraction import metadata_options
        self.assertEqual(metadata_options(direct=True)['proxy'], '')
        self.assertNotIn('proxy', metadata_options())

    def test_shortlink_direct_uses_empty_proxy_handler(self):
        from unittest.mock import MagicMock
        from bilialbum.urltools import normalize_url
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.geturl.return_value = 'https://www.bilibili.com/video/BV0000000000?p=3'
        with patch('urllib.request.build_opener', return_value=opener) as built:
            result = normalize_url('https://b23.tv/example', direct=True)
        self.assertEqual(result.page, 3)
        self.assertEqual(built.call_args.args[1].proxies, {})

class URLRedactionTests(unittest.TestCase):
    def test_invalid_identifier_values_and_unknown_paths_are_hidden(self):
        result = safe_url('https://www.bilibili.com/video/BV0000000000?p=secret&bvid=secret&access_token=secret')
        self.assertNotIn('secret', result)
        self.assertEqual(safe_url('https://www.bilibili.com/challenge/secret?cid=123'), 'https://www.bilibili.com/')
