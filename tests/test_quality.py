import unittest
from bilialbum.models import BiliAlbumError, Episode
from bilialbum.quality import choose_format, available_heights
from bilialbum.downloader import assert_identity, safe_name


INFO = {'formats': [
    {'format_id': 'v1080', 'height': 1080, 'vcodec': 'av01.0', 'acodec': 'none', 'tbr': 1500},
    {'format_id': 'v720', 'height': 720, 'vcodec': 'avc1.42', 'acodec': 'none', 'tbr': 900},
    {'format_id': 'v2160', 'height': 2160, 'vcodec': 'av01.0', 'acodec': 'none', 'format_note': 'VIP only'},
    {'format_id': 'audio', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 128},
]}


class QualityTests(unittest.TestCase):
    def test_available_heights(self):
        self.assertEqual(available_heights(INFO), [1080, 720])

    def test_auto(self):
        choice = choose_format(INFO, 'auto', False)
        self.assertEqual(choice.height, 1080)
        self.assertEqual(choice.selector, 'v1080+audio')

    def test_target(self):
        self.assertEqual(choose_format(INFO, '720', False).height, 720)

    def test_fallback(self):
        self.assertEqual(choose_format(INFO, '1440', True).height, 1080)

    def test_no_fallback(self):
        with self.assertRaises(BiliAlbumError):
            choose_format(INFO, '1440', False)

    def test_no_drm(self):
        data = {'formats': [{'format_id': 'v', 'height': 1080, 'vcodec': 'h264', 'acodec': 'aac', 'has_drm': True}]}
        with self.assertRaises(BiliAlbumError):
            choose_format(data, 'auto', True)

    def test_dont_download_wrong_p(self):
        ep = Episode(3, 'test', 'BV0000000000', page=3)
        with self.assertRaises(BiliAlbumError):
            assert_identity({'id': 'BV0000000000_p1'}, ep)
        assert_identity({'id': 'BV0000000000_p3'}, ep)

    def test_name_sanitized(self):
        self.assertEqual(safe_name('  a/b:c * ?  '), 'a_b_c _ _')


if __name__ == '__main__':
    unittest.main()
