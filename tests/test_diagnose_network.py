import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from subprocess import TimeoutExpired

import diagnose_network as diag

BV = 'BV0000000000'
URL = f'https://www.bilibili.com/video/{BV}/'
API = 'https://api.bilibili.com/x/web-interface/view?bvid=' + BV
DATA = {'bvid': BV, 'aid': 1, 'title': 'test', 'pages': [{'page': 1, 'cid': 10}]}


class Response(io.BytesIO):
    status = 200
    def geturl(self):
        return URL


class DiagnosticTests(unittest.TestCase):
    def check(self, body=None, failure=None, kind='api'):
        opener = SimpleNamespace(open=unittest.mock.Mock())
        if failure:
            opener.open.side_effect = failure
        else:
            opener.open.return_value = Response(body)
        with patch('urllib.request.build_opener', return_value=opener):
            row = diag.check_http(API if kind == 'api' else URL, {'bvid': BV}, kind)
        self.assertEqual(opener.open.call_count, 1)
        return row

    def test_api_200_must_contain_valid_data(self):
        row = self.check(json.dumps({'code': 0, 'data': DATA}).encode())
        self.assertTrue(row['valid_data'])
        self.assertEqual(row['status'], 200)
        for obj in ({'code': -412}, {'code': 0, 'data': []}, {'code': 0, 'data': {'bvid': 'wrong'}}):
            self.assertFalse(self.check(json.dumps(obj).encode())['valid_data'])

    def test_api_and_web_412_are_independent_no_fallback_or_retry(self):
        for kind in ('api', 'webpage'):
            row = self.check(failure=HTTPError(URL, 412, '', {}, None), kind=kind)
            self.assertEqual(row['status'], 412)
            self.assertFalse(row['valid_data'])
            self.assertEqual(row['failure_stage'], 'HTTP 请求')

    def test_web_200_must_contain_matching_initial_state(self):
        body = ('window.__INITIAL_STATE__=\n  ' + json.dumps({'videoData': DATA})).encode()
        self.assertTrue(self.check(body, kind='webpage')['valid_data'])
        self.assertFalse(self.check(b'<html>challenge</html>', kind='webpage')['valid_data'])

    def test_network_exception_recorded_without_raw_message(self):
        row = self.check(failure=URLError('http://name:secret@private.example'))
        self.assertEqual(row['exception'], 'URLError')
        self.assertNotIn('secret', json.dumps(row))

    def test_redirects_are_reported_and_sensitive_query_removed(self):
        def make_opener(recorder):
            recorder.hops.append({'status': 302, 'url': URL})
            return SimpleNamespace(open=lambda *a, **k: Response(json.dumps({'code': 0, 'data': DATA}).encode()))
        with patch('urllib.request.build_opener', side_effect=make_opener):
            row = diag.check_http(API, {'bvid': BV}, 'api')
        self.assertTrue(row['redirected'])
        self.assertEqual(row['redirects'][0]['status'], 302)

    def test_yt_metadata_success_and_failure(self):
        info = {'id': BV + '_p3', 'title': 'P3', 'formats': [{'format_id': 'v'}]}
        with patch('diagnose_network.extract_single', return_value=info):
            row = diag.check_yt_dlp(URL + '?p=3')
        self.assertTrue(row['valid_data'])
        with patch('diagnose_network.extract_single', side_effect=ValueError('secret')):
            row = diag.check_yt_dlp(URL)
        self.assertFalse(row['valid_data'])
        self.assertNotIn('secret', str(row))

    def test_subprocess_timeout_is_bounded(self):
        with patch('subprocess.run', side_effect=TimeoutExpired('command', 60)) as proc:
            row = diag.run_yt_dlp(URL)
        self.assertFalse(row['valid_data'])
        self.assertEqual(proc.call_args.kwargs['timeout'], 60)

    def test_settings_only_issues_no_http_requests(self):
        with patch('diagnose_network.environment_report', return_value={}), patch('diagnose_network.check_http') as http, patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(diag.main(['--settings-only']), 0)
        http.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue())['checks'], {})

    def test_failed_check_returns_nonzero_and_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'diag.json'
            with patch('diagnose_network.environment_report', return_value={}), patch('diagnose_network.check_http', return_value={'valid_data': False}), patch('sys.stdout', new_callable=io.StringIO):
                code = diag.main([URL, '--output', str(output)])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(output.read_text())['checks']['api']['valid_data'])

class DirectComparisonTests(unittest.TestCase):
    def test_direct_comparison_does_not_change_global_proxy_environment(self):
        import os
        before = dict(os.environ)
        response = Response(json.dumps({'code': 0, 'data': DATA}).encode())
        with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=lambda *a, **k: response)) as build:
            result = diag.check_http(API, {'bvid': BV}, 'api', direct=True)
        self.assertTrue(result['valid_data'])
        self.assertEqual(result['network_mode'], 'explicit_direct')
        self.assertEqual(build.call_args.args[1].proxies, {})
        self.assertEqual(dict(os.environ), before)

class ProxyEnvironmentTests(unittest.TestCase):
    def report_for(self, proxies, *, direct=False, bypass=False):
        with patch('urllib.request.getproxies', return_value=proxies), patch('urllib.request.proxy_bypass', return_value=bypass), patch('diagnose_network.shutil.which', return_value=None):
            return diag.environment_report(direct=direct)

    def test_only_all_proxy_exposes_urllib_ytdlp_route_difference(self):
        report = self.report_for({'all': 'http://alice:secret@localhost:8080'})
        route = report['effective_https_routes']['api.bilibili.com']
        self.assertTrue(route['urllib']['direct'])
        self.assertFalse(route['same_selected_proxy'])
        self.assertNotIn('secret', json.dumps(report))
        self.assertNotIn('alice', json.dumps(report))

    def test_only_http_proxy_exposes_ytdlp_https_fallback(self):
        report = self.report_for({'http': 'http://localhost:8080'})
        self.assertFalse(report['yt_dlp_same_proxy_table'])
        self.assertFalse(report['effective_https_routes']['api.bilibili.com']['same_selected_proxy'])

    def test_direct_mode_disables_both_clients_even_if_system_proxy_present(self):
        report = self.report_for({'https': 'http://localhost:8080'}, direct=True)
        self.assertEqual(report['urllib_effective_proxies'], {})
        self.assertEqual(report['yt_dlp_proxies'], {'all': {'disabled': True}})
        route = report['effective_https_routes']['api.bilibili.com']
        self.assertTrue(route['urllib']['direct'])
        self.assertTrue(route['yt_dlp']['direct'])
        self.assertTrue(route['same_selected_proxy'])

    def test_no_proxy_bypass_is_respected_and_value_redacted(self):
        report = self.report_for({'https': 'http://localhost:8080', 'no': 'bilibili.com'}, bypass=True)
        route = report['effective_https_routes']['api.bilibili.com']
        self.assertTrue(route['urllib']['direct'])
        self.assertTrue(route['yt_dlp']['direct'])
        self.assertEqual(report['urllib_proxies']['no'], {'configured': True, 'entries': 1})

class DiagnosticExitTests(unittest.TestCase):
    def test_invalid_proxy_settings_only_returns_failure(self):
        with patch('diagnose_network.environment_report', return_value={'proxy_validation': 'invalid_proxy'}), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(diag.main(['--settings-only']), 1)
            self.assertEqual(diag.main(['--settings-only', '--direct']), 0)

    def test_duplicate_direct_modes_rejected_before_network(self):
        with patch('sys.stderr', new_callable=io.StringIO), patch('diagnose_network.check_http') as http:
            with self.assertRaises(SystemExit):
                diag.main([URL, '--direct', '--compare-direct'])
        http.assert_not_called()

    def test_yt_http_200_wrong_identity_is_metadata_validation_failure(self):
        info = {'id': BV + '_p1', 'title': 'wrong page', 'formats': [{'format_id': 'v'}]}
        with patch('diagnose_network.extract_single', return_value=info):
            row = diag.check_yt_dlp(URL + '?p=3')
        self.assertFalse(row['valid_data'])
        self.assertEqual(row['failure_stage'], 'yt-dlp 元数据校验')
        self.assertEqual(row['category'], 'invalid_metadata')
