import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from bilialbum.downloader import DownloadManager, probe_episode
from bilialbum.extraction import RequestTrace, extract_single
from bilialbum.models import BiliAlbumError, NetworkFailure, Episode, Collection

BV = 'BV0000000000'
URL = f'https://www.bilibili.com/video/{BV}?p=3'


class FakeYDL:
    options = None
    requested = []
    info = {'id': BV + '_p3', 'title': 'third', 'formats': [{'format_id': 'v'}]}
    failure = None
    target = URL

    def __init__(self, options):
        FakeYDL.options = options

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def urlopen(self, req):
        FakeYDL.requested.append(req.url)
        if FakeYDL.failure:
            raise FakeYDL.failure
        return SimpleNamespace(status=200, url=req.url)

    def extract_info(self, url, download):
        assert download is False
        self.urlopen(SimpleNamespace(url=FakeYDL.target, method='GET'))
        return FakeYDL.info.copy()


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        FakeYDL.failure = None
        FakeYDL.target = URL
        FakeYDL.requested = []

    def test_success_single_p3_public_api_no_download_no_retry_no_cache(self):
        trace = RequestTrace()
        info = extract_single(FakeYDL, URL, trace=trace)
        self.assertEqual(info['id'], BV + '_p3')
        self.assertEqual(trace.records[0]['status'], 200)
        self.assertFalse(FakeYDL.options['cachedir'])
        self.assertTrue(FakeYDL.options['noplaylist'])
        self.assertEqual(FakeYDL.options['extractor_retries'], 0)
        self.assertNotIn('cookiefile', FakeYDL.options)
        self.assertNotIn('cookiesfrombrowser', FakeYDL.options)

    def test_failure_records_actual_playinfo_stage_and_stops_repeat(self):
        FakeYDL.target = 'https://api.bilibili.com/x/player/wbi/playurl?cid=30&w_rid=secret'
        FakeYDL.failure = HTTPError(FakeYDL.target, 412, '', {}, None)
        trace = RequestTrace()
        with self.assertRaises(NetworkFailure) as caught:
            extract_single(FakeYDL, URL, trace=trace)
        self.assertEqual(caught.exception.status, 412)
        self.assertIn('播放信息', caught.exception.stage)
        self.assertNotIn('secret', str(trace.records))
        with self.assertRaises(NetworkFailure):
            extract_single(FakeYDL, URL, trace=trace)
        self.assertEqual(len(FakeYDL.requested), 1)

    def test_trace_request_budget(self):
        trace = RequestTrace(limit=1)
        extract_single(FakeYDL, URL, trace=trace)
        with self.assertRaises(NetworkFailure):
            extract_single(FakeYDL, URL, trace=trace)
        self.assertEqual(len(FakeYDL.requested), 1)

    def test_probe_identity_still_rejects_p1(self):
        with patch('bilialbum.downloader._import_ydl', return_value=FakeYDL), patch.object(FakeYDL, 'info', {'id': BV + '_p1'}):
            with self.assertRaisesRegex(BiliAlbumError, '分集校验失败'):
                probe_episode(Episode(1, 'P3', BV, 3))

    def test_412_does_not_retry_download_or_continue_entire_collection(self):
        denied = NetworkFailure('yt-dlp 公开视频网页', 'server_denied', 'HTTP 412', status=412, retryable=False)
        episodes = [Episode(1, 'one', BV), Episode(2, 'two', BV, 2)]
        col = Collection('test', 'multip', episodes, 1, '', URL)
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(Path(tmp))
            with patch('bilialbum.downloader.ensure_ffmpeg'), patch.object(manager, '_attempt', side_effect=denied) as attempted:
                with self.assertRaises(NetworkFailure):
                    manager.run(col, episodes, 'auto', True)
                self.assertEqual(attempted.call_count, 1)

class DirectExtractionTests(unittest.TestCase):
    def test_probe_direct_mode_passes_official_proxy_disable_option(self):
        FakeYDL.failure = None
        FakeYDL.target = URL
        with patch('bilialbum.downloader._import_ydl', return_value=FakeYDL):
            info = probe_episode(Episode(1, 'P3', BV, 3), direct=True)
        self.assertEqual(info['id'], BV + '_p3')
        self.assertEqual(FakeYDL.options['proxy'], '')

class FailureAttributionTests(unittest.TestCase):
    def test_earlier_nonfatal_412_does_not_mask_later_extractor_error(self):
        trace = RequestTrace()
        trace.records.append({'exception': 'HTTPError', 'status': 412,
                              'category': 'server_denied', 'stage': '分 P 列表'})
        failure = trace.failure(ValueError('unrelated invalid data'))
        self.assertIsNone(failure.status)
        self.assertEqual(failure.stage, 'yt-dlp 元数据提取')
        self.assertEqual(failure.category, 'network_or_extractor')

    def test_production_trace_has_no_diagnostic_request_limit(self):
        trace = RequestTrace()
        self.assertIsNone(trace.limit)

class MediaStageTests(unittest.TestCase):
    def test_media_cdn_failure_is_not_labeled_webpage_failure(self):
        from bilialbum.extraction import request_stage
        self.assertEqual(request_stage('https://upos-sz-mirror.bilivideo.com/path/video.m4s?token=secret'), '媒体流')
