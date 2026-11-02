"""Independent audit regressions: incomplete collections and unsafe download reuse."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.request import Request

from bilialbum.downloader import DownloadManager, _source_file, _completed_source, _read_history
from bilialbum.models import BiliAlbumError, Episode, Collection
from bilialbum.resolver import Resolver
from bilialbum.urltools import normalize_url, _ShortLinkRedirect
from bilialbum.gui import BiliAlbumApp
from test_resolver import FakeAPI, season_items, view

BV = 'BV0000000000'
URL = f'https://www.bilibili.com/video/{BV}?p=3'
INFO = {'id': BV + '_p3', 'title': 'P3', 'duration': 10, 'formats': [
    {'format_id': 'v', 'height': 720, 'vcodec': 'h264', 'acodec': 'aac'}]}


class CollectionAuditTests(unittest.TestCase):
    def test_series_duplicate_member_does_not_count_as_full(self):
        items = season_items()
        items[2] = items[0]
        api = FakeAPI(series={1: {'archives': items, 'page': {'total': 3}}})
        with self.assertRaisesRegex(BiliAlbumError, '重复'):
            Resolver(api).resolve('https://space.bilibili.com/42/lists/88?type=series')

    def test_changed_total_cannot_silently_hide_missing_member(self):
        items = season_items()
        api = FakeAPI(batches={1: {'archives': items[:1], 'page': {'total': 3}},
                               2: {'archives': items[1:2], 'page': {'total': 2}}})
        with self.assertRaisesRegex(BiliAlbumError, '总数发生变化'):
            Resolver(api).resolve('https://space.bilibili.com/42/lists/88?type=season')

    def test_missing_total_short_page_continues_until_empty_page(self):
        items = season_items()
        api = FakeAPI(batches={1: {'archives': items[:2], 'page': {}},
                               2: {'archives': items[2:], 'page': {}},
                               3: {'archives': [], 'page': {}}})
        result = Resolver(api).resolve('https://space.bilibili.com/42/lists/88?type=season')
        self.assertEqual(len(result.episodes), 3)
        self.assertEqual(len(api.calls), 3)

    def test_collection_bv_total_duration_is_not_used_as_p1_duration(self):
        api = FakeAPI(series={1: {'archives': season_items(), 'page': {'total': 3}}})
        result = Resolver(api).resolve('https://space.bilibili.com/42/lists/88?type=series')
        self.assertTrue(all(ep.duration is None for ep in result.episodes))

    def test_fractional_boolean_or_malformed_page_data_is_rejected(self):
        for value in (3.9, True, '3.0'):
            item = view(3)
            item['pages'][2]['page'] = value
            with self.subTest(value=value), self.assertRaises(BiliAlbumError):
                Resolver(FakeAPI(item)).resolve(URL)
        item = view(1)
        item['pages'] = {}
        with self.assertRaises(BiliAlbumError):
            Resolver(FakeAPI(item)).resolve(URL.replace('?p=3', ''))


class DownloadAuditTests(unittest.TestCase):
    def test_reextraction_rejects_wrong_p_before_media_write_with_real_ytdlp_filter(self):
        from yt_dlp import YoutubeDL
        ep = Episode(1, 'P3', BV, 3, duration=10)
        col = Collection('test', 'single', [ep], 1, '', URL)
        wrong = {'id': BV + '_p1', 'title': 'P1', 'duration': 10,
                 'extractor': 'BiliBili', 'extractor_key': 'BiliBili',
                 'formats': [{'format_id': 'v', 'url': 'https://invalid.example/movie.mp4',
                              'ext': 'mp4', 'height': 720, 'vcodec': 'h264', 'acodec': 'aac'}]}
        class ChangedExtraction(YoutubeDL):
            def extract_info(self, url, download):
                return self.process_ie_result(dict(wrong), download=download)
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(Path(tmp))
            with patch('bilialbum.downloader.probe_episode', return_value=INFO), patch('bilialbum.downloader._import_ydl', return_value=ChangedExtraction), patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('must not fetch media')) as network:
                with self.assertRaisesRegex(BiliAlbumError, '分集校验失败'):
                    manager._attempt(ep, col, '720', True, Path(tmp), {}, Path(tmp)/'history.json', ep.key)
            network.assert_not_called()
            self.assertEqual(list(Path(tmp).rglob('*.mp4')), [])

    def test_permanent_identity_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(Path(tmp))
            ep = Episode(1, 'P3', BV, 3)
            with patch.object(manager, '_attempt', side_effect=BiliAlbumError('分集校验失败')) as attempt:
                with self.assertRaises(BiliAlbumError):
                    manager._one(ep, None, 'auto', True, Path(tmp), {}, Path(tmp)/'history.json')
            self.assertEqual(attempt.call_count, 1)

    def test_partial_video_component_cannot_be_reused_as_merged_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'source.f137.mp4'
            path.write_bytes(b'x'*2048)
            with patch('bilialbum.downloader.probe', return_value={'format': {'duration': 10}, 'streams': [{'codec_type': 'video'}]}):
                self.assertIsNone(_source_file(Path(tmp)))
            path.rename(Path(tmp)/'source.mp4')
            with patch('bilialbum.downloader.probe', return_value={'format': {'duration': 10}, 'streams': [{'codec_type': 'video'}]}):
                self.assertIsNone(_source_file(Path(tmp)))

    def test_unmarked_completed_file_is_not_trusted_after_interrupted_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'source.mkv'
            path.write_bytes(b'x'*2048)
            ep = Episode(1, 'P3', BV, 3)
            with patch('bilialbum.downloader._source_file', return_value=path):
                self.assertIsNone(_completed_source(Path(tmp), ep, 'v+a'))
                (Path(tmp)/'source.complete.json').write_text(json.dumps({'key': ep.key, 'cid': None,
                    'selector': 'v+a', 'file': 'source.mkv', 'size': 2048}))
                self.assertEqual(_completed_source(Path(tmp), ep, 'v+a'), path)
                self.assertIsNone(_completed_source(Path(tmp), Episode(1, 'P1', BV), 'v+a'))

    def test_damaged_history_has_safe_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history.json'
            for value in ([], 'wrong', None, {'bad': 'wrong'}):
                path.write_text(json.dumps(value))
                self.assertEqual(_read_history(path), {})


class UIAndURLAuditTests(unittest.TestCase):
    def test_finished_thread_with_unhandled_results_still_blocks_new_task(self):
        fake = SimpleNamespace(_job_active=True, worker=SimpleNamespace(is_alive=lambda: False))
        self.assertTrue(BiliAlbumApp._busy(fake))

    def test_invalid_publish_time_does_not_stop_ui_event_processing(self):
        fake = MagicMock()
        col = Collection('test', 'single', [Episode(1, 'test', BV, published=10**99)], 1, '', URL)
        BiliAlbumApp._apply_result(fake, col)
        self.assertEqual(fake.table.insert.call_args.kwargs['values'][2], '未知')

    def test_b23_stops_at_valid_location_without_fetching_blocked_landing_page(self):
        requests = []
        def open_short(req, **kwargs):
            requests.append(req.full_url)
            return _ShortLinkRedirect().redirect_request(req, None, 302, '', {}, URL)
        with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=open_short)):
            result = normalize_url('https://b23.tv/example')
        self.assertEqual(result.page, 3)
        self.assertEqual(requests, ['https://b23.tv/example'])

    def test_cli_episode_zero_is_rejected_instead_of_defaulting_to_p3(self):
        import main
        col = Collection('test', 'multip', [Episode(i, str(i), BV, i) for i in range(1,4)], 3, '', URL)
        with patch('sys.argv', ['main.py', '--url', URL, '--episode', '0']), patch('bilialbum.resolver.Resolver') as resolver, patch('bilialbum.downloader.DownloadManager') as manager, patch('sys.stdout', new_callable=io.StringIO):
            resolver.return_value.resolve.return_value = col
            self.assertEqual(main.main(), 1)
        manager.assert_not_called()
