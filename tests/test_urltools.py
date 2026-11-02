import unittest
from unittest.mock import patch
from bilialbum.models import BiliAlbumError
from bilialbum.urltools import parse_url, normalize_url


class URLTests(unittest.TestCase):
    def test_url_page_preserved_with_other_params(self):
        r = parse_url('https://www.bilibili.com/video/BV0000000000?spm_id_from=home&p=12&vd_source=test')
        self.assertEqual(r.video_id, 'BV0000000000')
        self.assertEqual(r.page, 12)

    def test_url_first_page(self):
        self.assertEqual(parse_url('https://www.bilibili.com/video/BV0000000000').page, 1)

    def test_av_url(self):
        self.assertEqual(parse_url('https://www.bilibili.com/video/av1234567?p=2').video_id, 'av1234567')

    def test_lists(self):
        r = parse_url('https://space.bilibili.com/1234/lists/5678?type=season')
        self.assertEqual((r.mid, r.list_id, r.list_type), ('1234', '5678', 'season'))

    def test_bad_domains(self):
        for url in ('https://www.bilibili.com.attacker.com/video/BV0000000000',
                    'http://www.bilibili.com/video/BV0000000000',
                    'https://www.bilibili.com:9999/video/BV0000000000',
                    'https://example.com/video/BV0000000000'):
            with self.subTest(url=url), self.assertRaises(BiliAlbumError):
                parse_url(url)

    def test_bad_page(self):
        with self.assertRaises(BiliAlbumError):
            parse_url('https://www.bilibili.com/video/BV0000000000?p=abc')


if __name__ == '__main__':
    unittest.main()

class ShortLinkTests(unittest.TestCase):
    def test_short_link_redirect_preserves_final_p3(self):
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.geturl.return_value = 'https://www.bilibili.com/video/BV0000000000?p=3'
        with patch('urllib.request.build_opener', return_value=opener):
            result = normalize_url('https://b23.tv/example')
        self.assertEqual(result.page, 3)
        self.assertEqual(opener.open.call_count, 1)

    def test_short_link_explicit_p3_survives_redirect_dropping_query(self):
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.geturl.return_value = 'https://www.bilibili.com/video/BV0000000000'
        with patch('urllib.request.build_opener', return_value=opener):
            result = normalize_url('https://b23.tv/example?p=3')
        self.assertEqual(result.page, 3)
        self.assertIn('p=3', result.url)

    def test_short_link_credentials_ports_rejected_before_network(self):
        for value in ('https://name:secret@b23.tv/x', 'https://b23.tv:123/x', 'https://b23.tv:bad/x'):
            with self.subTest(url=value), patch('urllib.request.build_opener') as opener:
                with self.assertRaises(BiliAlbumError):
                    normalize_url(value)
                opener.assert_not_called()

    def test_redirect_forbids_credentials_and_external_hosts(self):
        from bilialbum.urltools import _SafeRedirect
        for target in ('https://attacker.example/x', 'https://name:secret@www.bilibili.com/video/x', 'https://b23.tv:8080/x'):
            with self.subTest(target=target), self.assertRaises(BiliAlbumError):
                _SafeRedirect().redirect_request(None, None, 302, '', {}, target)

    def test_blank_conflicting_unicode_pages_rejected(self):
        for query in ('p=', 'p=3&p=1', 'p=³'):
            with self.subTest(query=query), self.assertRaises(BiliAlbumError):
                parse_url('https://www.bilibili.com/video/BV0000000000?' + query)

    def test_uppercase_av_is_normalized(self):
        self.assertEqual(parse_url('https://www.bilibili.com/video/AV42?p=3').video_id, 'av42')
