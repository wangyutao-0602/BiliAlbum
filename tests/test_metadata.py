"""Official extraction fallback must retain collection data and exact P identity."""
import io
import json
import queue
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from yt_dlp.networking import Response

from bilialbum.api import BiliAPI
from bilialbum.gui import BiliAlbumApp
from bilialbum.metadata import embedded_info, extract_view
from bilialbum.models import BiliAlbumError, NetworkFailure, Episode
from bilialbum.resolver import Resolver
from bilialbum.urltools import parse_url
from test_resolver import FakeAPI, view, season_items

BV = 'BV0000000000'
URL = f'https://www.bilibili.com/video/{BV}/?p=3'


class PageYDL:
    data = None
    info = None
    requested = []
    options = None
    play = None

    def __init__(self, options):
        PageYDL.options = options

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def urlopen(self, url):
        PageYDL.requested.append(url)
        if isinstance(PageYDL.data, Exception):
            raise PageYDL.data
        raw = ('window.__INITIAL_STATE__=' + json.dumps({'videoData': PageYDL.data})).encode()
        if PageYDL.play:
            raw += (';window.__playinfo__=' + json.dumps(PageYDL.play)).encode()
        return Response(io.BytesIO(raw), url, {'Content-Type': 'text/html'})

    def extract_info(self, url, download=False):
        assert download is False
        with self.urlopen(url) as response:
            # Reading the captured page must still work for the official extractor.
            assert b'window.__INITIAL_STATE__=' in response.read()
        return dict(PageYDL.info)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        PageYDL.data = view(3)
        PageYDL.data['aid'] = 42
        PageYDL.info = {'id': BV + '_p3', 'title': 'P3', 'duration': 80,
                        'formats': [{'format_id': 'v', 'height': 720, 'vcodec': 'h264', 'acodec': 'aac'}]}
        PageYDL.requested = []
        PageYDL.play = None

    def test_one_public_page_is_shared_with_extractor_and_preserves_pages(self):
        with patch('yt_dlp.YoutubeDL', PageYDL):
            data, info = extract_view(parse_url(URL), direct=True)
        self.assertEqual(data['pages'][2]['cid'], 300)
        self.assertEqual(info['id'], BV + '_p3')
        self.assertEqual(PageYDL.requested, [URL])
        self.assertEqual(PageYDL.options['proxy'], '')
        self.assertTrue(PageYDL.options['skip_download'])
        self.assertNotIn('cookiefile', PageYDL.options)

    def test_av_identity_and_requested_page_are_preserved(self):
        with patch('yt_dlp.YoutubeDL', PageYDL):
            data, info = extract_view(parse_url('https://www.bilibili.com/video/av42?p=3'))
        self.assertEqual(data['aid'], 42)
        self.assertEqual(info['id'], BV + '_p3')

    def test_official_extractor_wrong_p_still_fails(self):
        PageYDL.info['id'] = BV + '_p1'
        with patch('yt_dlp.YoutubeDL', PageYDL), self.assertRaisesRegex(BiliAlbumError, '分集校验失败'):
            extract_view(parse_url(URL))

    def test_missing_page_list_cannot_be_silently_treated_as_single_video(self):
        PageYDL.data.pop('pages')
        with patch('yt_dlp.YoutubeDL', PageYDL), self.assertRaisesRegex(BiliAlbumError, '完整分 P'):
            extract_view(parse_url(URL))

    def test_official_webpage_412_stops_after_one_request(self):
        PageYDL.data = HTTPError(URL, 412, '', {}, None)
        with patch('yt_dlp.YoutubeDL', PageYDL), self.assertRaises(NetworkFailure) as caught:
            extract_view(parse_url(URL))
        self.assertEqual(caught.exception.status, 412)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(PageYDL.requested), 1)

    def test_fallback_preserves_collection_sorting_p3_and_preflight_info(self):
        PageYDL.data['ugc_season'] = {'id': 88, 'title': '合集', 'ep_count': 3,
                                     'sections': [{'episodes': season_items()}]}
        api = FakeAPI(batches={1: BiliAlbumError('受限')})
        original_get = api.get

        def get(route, params):
            if route == '/x/web-interface/view':
                raise NetworkFailure('视频信息 API → 公开网页', 'server_denied', 'HTTP 412', status=412, retryable=False)
            return original_get(route, params)

        with patch.object(api, 'get', side_effect=get), patch('yt_dlp.YoutubeDL', PageYDL):
            result = Resolver(api).resolve(URL)
        self.assertEqual(result.kind, 'season')
        self.assertEqual([ep.title for ep in result.episodes], ['最早', '中间', '最晚'])
        ep = result.episodes[result.selected_number - 1]
        self.assertEqual((ep.page, ep.cid, ep.duration), (3, 300, 80))
        self.assertEqual(result.initial_info['id'], BV + '_p3')
        with patch('bilialbum.gui.Resolver') as resolver, patch('bilialbum.gui.probe_episode') as probe:
            resolver.return_value.resolve.return_value = result
            fake = SimpleNamespace(events=queue.Queue())
            BiliAlbumApp._resolve_worker(fake, URL)
        probe.assert_not_called()
        fake.events.get_nowait()
        self.assertEqual(fake.events.get_nowait(), ('quality', [720]))

    def test_proxy_error_does_not_try_an_alternative_client(self):
        error = NetworkFailure('代理配置', 'invalid_proxy', '无效', retryable=False)
        with patch.object(BiliAPI, 'get', side_effect=error), patch('bilialbum.resolver.extract_view') as fallback:
            with self.assertRaises(NetworkFailure):
                Resolver(BiliAPI()).resolve(URL)
        fallback.assert_not_called()

    def test_default_resolver_uses_public_page_without_waiting_for_legacy_api(self):
        with patch('bilialbum.resolver.extract_view', return_value=(view(3), PageYDL.info)) as page, patch.object(BiliAPI, 'get', side_effect=AssertionError('legacy view API must not be called')):
            result = Resolver().resolve(URL)
        self.assertEqual(result.episodes[result.selected_number - 1].page, 3)
        page.assert_called_once()


PLAY = {'code': 0, 'data': {'timelength': 80000, 'cid': 300, 'dash': {
    'video': [{'id': 64, 'base_url': 'https://media.invalid/video.m4s', 'codecs': 'avc1.64001f',
               'height': 720, 'width': 1280, 'bandwidth': 800000}],
    'audio': [{'id': 30280, 'baseUrl': 'https://media.invalid/audio.m4s', 'codecs': 'mp4a.40.2',
               'bandwidth': 128000}]}}}


class EmbeddedPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.data = view(3)
        self.data['cid'] = 300
        self.play = copy.deepcopy(PLAY)

    def parse(self):
        raw = ('<script>window.__playinfo__=\n' + json.dumps(self.play) + ';</script>').encode()
        return embedded_info(raw, self.data, parse_url(URL))

    def test_dash_base_url_spellings_and_exact_p3_are_supported(self):
        info = self.parse()
        self.assertEqual((info['id'], info['cid'], info['duration']), (BV + '_p3', 300, 80))
        from bilialbum.quality import choose_format
        choice = choose_format(info, '720', False)
        self.assertEqual(choice.selector, 'embedded-video-0+embedded-audio-0')

    def test_embedded_p1_for_p3_is_never_used(self):
        self.data['cid'] = 100
        self.assertIsNone(self.parse())
        self.data['cid'] = 300
        self.play['data']['cid'] = 100
        self.assertIsNone(self.parse())

    def test_missing_cid_preview_paid_or_drm_is_not_accepted(self):
        for mutation in ('cid', 'preview', 'paid', 'drm'):
            self.setUp()
            if mutation == 'cid':
                self.data.pop('cid')
            elif mutation == 'preview':
                self.play['data']['timelength'] = 10000
            elif mutation == 'paid':
                self.data['is_upower_exclusive'] = True
            else:
                self.play['data']['dash']['video'][0]['has_drm'] = True
            with self.subTest(mutation=mutation):
                self.assertIsNone(self.parse())

    def test_valid_embedded_data_avoids_playback_api_and_reextraction(self):
        PageYDL.data, PageYDL.play = self.data, self.play
        PageYDL.requested = []
        try:
            with patch('yt_dlp.YoutubeDL', PageYDL), patch.object(PageYDL, 'extract_info', side_effect=AssertionError('no additional API extraction')):
                data, info = extract_view(parse_url(URL))
            self.assertEqual(PageYDL.requested, [URL])
            self.assertTrue(info['_bilialbum_embedded'])
            self.assertEqual(len(data['pages']), 3)
        finally:
            PageYDL.play = None

    def test_playback_412_uses_only_embedded_fallback_and_keeps_identity_check(self):
        from bilialbum.downloader import probe_episode
        error = NetworkFailure('yt-dlp 播放信息', 'server_denied', 'HTTP 412', status=412, retryable=False)
        with patch('bilialbum.downloader.extract_single', side_effect=error), patch('bilialbum.metadata.extract_view', return_value=(self.data, self.parse())) as fallback:
            info = probe_episode(Episode(1, 'P3', BV, 3, cid=300))
        self.assertEqual(info['cid'], 300)
        self.assertTrue(fallback.call_args.kwargs['embedded_only'])

    def test_changed_cid_is_rejected_before_download(self):
        from bilialbum.downloader import validate_episode_info
        with self.assertRaisesRegex(BiliAlbumError, 'CID'):
            validate_episode_info(self.parse(), Episode(1, 'P3', BV, 3, cid=999))

    def test_real_official_extractor_reuses_page_buffer_when_dash_is_absent(self):
        from yt_dlp import YoutubeDL
        data = dict(self.data, aid=42)
        requests = []

        def transport(_ydl, request):
            url = request if isinstance(request, str) else request.url
            requests.append(url)
            path = urlsplit(url).path
            if path.startswith('/video/'):
                raw = ('<script>window.__INITIAL_STATE__=' + json.dumps({'videoData': data}) + ';</script>').encode()
                return Response(io.BytesIO(raw), url, {'Content-Type': 'text/html; charset=utf-8'})
            payloads = {
                '/x/player/pagelist': {'code': 0, 'data': data['pages']},
                '/x/web-interface/nav': {'code': 0, 'data': {'wbi_img': {
                    'img_url': 'https://image.invalid/' + 'a' * 32 + '.png',
                    'sub_url': 'https://image.invalid/' + 'b' * 32 + '.png'}}},
                '/x/player/wbi/playurl': self.play,
                '/x/player/wbi/v2': {'code': 0, 'data': {'view_points': []}},
            }
            if path not in payloads:
                raise AssertionError(f'Unexpected request: {path}')
            return Response(io.BytesIO(json.dumps(payloads[path]).encode()), url, {'Content-Type': 'application/json'})

        with patch.object(YoutubeDL, 'urlopen', transport):
            result, info = extract_view(parse_url(URL), direct=True)
        self.assertEqual(info['id'], BV + '_p3')
        self.assertEqual(result['pages'][2]['cid'], 300)
        self.assertTrue(info['formats'])
        self.assertEqual(sum('/video/' in url for url in requests), 1)
