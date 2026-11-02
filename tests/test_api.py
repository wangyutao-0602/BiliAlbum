import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from bilialbum.api import BiliAPI
from bilialbum.models import BiliAlbumError
from bilialbum.resolver import Resolver

BV = 'BV0000000000'


def response_412(url='https://api.bilibili.com/x/web-interface/view'):
    return HTTPError(url, 412, 'Precondition Failed', {}, None)


class PublicViewFallbackTests(unittest.TestCase):
    def test_api_412_public_page_returns_requested_p3(self):
        initial = {'videoData': {
            'bvid': BV, 'title': '测试', 'pubdate': 1589601697,
            'pages': [{'page': i, 'part': f'第{i}集', 'cid': i * 10}
                      for i in range(1, 4)],
        }}
        html = ('<script>window.__INITIAL_STATE__=' +
                json.dumps(initial, ensure_ascii=False) + ';</script>').encode()
        with patch('urllib.request.urlopen', side_effect=[response_412(), io.BytesIO(html)]) as mocked:
            result = Resolver(BiliAPI()).resolve(f'https://www.bilibili.com/video/{BV}?p=3')
        self.assertEqual(result.selected_number, 3)
        self.assertEqual(result.episodes[2].cid, 30)
        self.assertEqual(mocked.call_count, 2)

    def test_page_also_412_stops_without_retry(self):
        with patch('urllib.request.urlopen', side_effect=[response_412(), response_412('https://www.bilibili.com/video/')]) as mocked:
            with self.assertRaisesRegex(BiliAlbumError, '又收到 HTTP 412'):
                BiliAPI().get('/x/web-interface/view', {'bvid': BV})
        self.assertEqual(mocked.call_count, 2)

    def test_no_initial_state_reports_block(self):
        with patch('urllib.request.urlopen', side_effect=[response_412(), io.BytesIO(b'<html>Access challenge</html>')]):
            with self.assertRaisesRegex(BiliAlbumError, '没有可用的初始视频数据'):
                BiliAPI().get('/x/web-interface/view', {'bvid': BV})

    def test_wrong_bvid_prevents_wrong_download(self):
        html = ('window.__INITIAL_STATE__=' + json.dumps({
            'videoData': {'bvid': 'BV1000000001', 'pages': []}
        })).encode()
        with patch('urllib.request.urlopen', side_effect=[response_412(), io.BytesIO(html)]):
            with self.assertRaisesRegex(BiliAlbumError, 'BV 号与请求不一致'):
                BiliAPI().get('/x/web-interface/view', {'bvid': BV})

    def test_normal_api_has_no_fallback(self):
        resp = {'code': 0, 'data': {'bvid': BV}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(resp).encode())) as mocked:
            result = BiliAPI().get('/x/web-interface/view', {'bvid': BV})
        self.assertEqual(result['bvid'], BV)
        self.assertEqual(mocked.call_count, 1)

    def test_412_non_view_endpoint_no_fallback(self):
        with patch('urllib.request.urlopen', side_effect=response_412()) as mocked:
            with self.assertRaisesRegex(BiliAlbumError, 'HTTP 412'):
                BiliAPI().get('/x/series/archives', {'mid': '1'})
        self.assertEqual(mocked.call_count, 1)


if __name__ == '__main__':
    unittest.main()

class PayloadIntegrityTests(unittest.TestCase):
    def test_api_200_wrong_id_is_rejected(self):
        payload = {'code': 0, 'data': {'bvid': 'BV1000000001'}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaisesRegex(BiliAlbumError, '标识与请求不一致'):
                BiliAPI().get('/x/web-interface/view', {'bvid': BV})

    def test_api_200_business_412_is_not_success(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"code": -412}')) as opened:
            with self.assertRaises(BiliAlbumError) as caught:
                BiliAPI().get('/x/web-interface/view', {'bvid': BV})
        self.assertEqual(caught.exception.category, 'server_denied')
        self.assertEqual(opened.call_count, 1)

    def test_api_200_invalid_data_is_not_success(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"code": 0, "data": []}')):
            with self.assertRaisesRegex(BiliAlbumError, '有效的元数据'):
                BiliAPI().get('/x/web-interface/view', {'bvid': BV})

    def test_public_page_whitespace_and_av_identity(self):
        raw = ('window.__INITIAL_STATE__ = \n\t' + json.dumps({'videoData': {'bvid': BV, 'aid': 42}})).encode()
        result = BiliAPI.parse_public_page(raw, {'aid': '42'})
        self.assertEqual(result['bvid'], BV)
        with self.assertRaisesRegex(BiliAlbumError, 'av 号与请求不一致'):
            BiliAPI.parse_public_page(raw, {'aid': '43'})
