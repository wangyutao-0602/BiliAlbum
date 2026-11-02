import unittest
from bilialbum.models import Episode, BiliAlbumError
from bilialbum.selection import parse_episodes, select_episodes


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.eps = [Episode(i, f'{i}', f'BV{i:010d}') for i in range(1, 13)]

    def test_range_and_duplicates(self):
        self.assertEqual(parse_episodes('1,3,5-8,5', 12), [1, 3, 5, 6, 7, 8])

    def test_chinese_punctuation(self):
        self.assertEqual(parse_episodes('1，3，5—7', 12), [1, 3, 5, 6, 7])

    def test_bad_range(self):
        for expr in ('', '0', '13', '4-2', '1,foo', '1,,3', '1-99999'):
            with self.subTest(expr=expr), self.assertRaises(BiliAlbumError):
                parse_episodes(expr, 12)

    def test_all_single_custom(self):
        self.assertEqual(len(select_episodes(self.eps, 'all', 9, '')), 12)
        self.assertEqual([e.number for e in select_episodes(self.eps, 'single', 3, '')], [3])
        self.assertEqual([e.number for e in select_episodes(self.eps, 'custom', 3, '8,1,5-6')], [1, 5, 6, 8])


if __name__ == '__main__':
    unittest.main()
