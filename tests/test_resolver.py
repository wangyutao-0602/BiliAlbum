import unittest
from bilialbum.models import BiliAlbumError
from bilialbum.resolver import Resolver


class FakeAPI:
    def __init__(self, view=None, batches=None, series=None):
        self.view = view or {}
        self.batches = batches or {}
        self.series = series or {}
        self.calls = []

    def get(self, route, params):
        self.calls.append((route, params))
        if route == '/x/web-interface/view':
            return self.view
        if route.endswith('/seasons_archives_list'):
            value = self.batches.get(int(params['page_num']))
            if isinstance(value, Exception):
                raise value
            return value if value is not None else {'archives': [], 'page': {'total': 0}}
        if route == '/x/series/archives':
            return self.series.get(int(params['pn'])) or {'archives': [], 'page': {}}
        raise AssertionError(route)


def view(pages=3):
    return {'bvid': 'BV0000000000', 'title': '物语', 'pubdate': 1589601697,
            'owner': {'mid': 42},
            'pages': [{'page': i, 'part': f'part{i}', 'cid': i * 100, 'duration': 80}
                      for i in range(1, pages + 1)]}


def season_items():
    return [
        {'bvid': 'BV1000000001', 'title': '中间', 'pubdate': 300, 'duration': 30},
        {'bvid': 'BV1000000002', 'title': '最晚', 'pubdate': 400, 'duration': 30},
        {'bvid': 'BV0000000000', 'title': '最早', 'pubdate': 200, 'duration': 30},
    ]


class ResolverTests(unittest.TestCase):
    def test_multip_exact_p3(self):
        res = Resolver(FakeAPI(view())).resolve('https://www.bilibili.com/video/BV0000000000?p=3')
        self.assertEqual(res.kind, 'multip')
        self.assertEqual(res.selected_number, 3)
        self.assertEqual(res.episodes[2].cid, 300)
        self.assertEqual(res.episodes[2].page, 3)

    def test_does_not_fall_back_p1(self):
        with self.assertRaises(BiliAlbumError):
            Resolver(FakeAPI(view())).resolve('https://www.bilibili.com/video/BV0000000000?p=9')

    def test_single(self):
        res = Resolver(FakeAPI(view(1))).resolve('https://www.bilibili.com/video/BV0000000000')
        self.assertEqual(len(res.episodes), 1)
        self.assertEqual(res.kind, 'single')

    def test_season_sort_ascending_anchor(self):
        item = view(1)
        item['ugc_season'] = {'id': 88, 'title': '物理课'}
        api = FakeAPI(item, batches={1: {'archives': season_items(), 'page': {'total': 3}}})
        res = Resolver(api).resolve('https://www.bilibili.com/video/BV0000000000')
        self.assertEqual(res.kind, 'season')
        self.assertEqual([e.title for e in res.episodes], ['最早', '中间', '最晚'])
        self.assertEqual(res.selected_number, 1)
        self.assertEqual(res.title, '物理课')

    def test_season_multip_p3_anchor_kept(self):
        item = view(3)
        item['ugc_season'] = {'id': 88, 'title': '物理课'}
        res = Resolver(FakeAPI(item, {1: {'archives': season_items(), 'page': {'total': 3}}})).resolve(
            'https://www.bilibili.com/video/BV0000000000?p=3')
        self.assertEqual(res.episodes[res.selected_number - 1].page, 3)

    def test_missing_date_keeps_platform_order(self):
        item = view(1)
        item['ugc_season'] = {'id': 88, 'title': '课'}
        seq = season_items()
        del seq[1]['pubdate']
        res = Resolver(FakeAPI(item, {1: {'archives': seq, 'page': {'total': 3}}})).resolve(
            'https://www.bilibili.com/video/BV0000000000')
        self.assertEqual([e.title for e in res.episodes], ['中间', '最晚', '最早'])
        self.assertIn('缺失', res.order_note)
        self.assertEqual(res.selected_number, 3)

    def test_paginated_season_complete(self):
        items = season_items()
        api = FakeAPI(batches={1: {'archives': items[:2], 'page': {'total': 3}},
                               2: {'archives': items[2:], 'page': {'total': 3}}})
        result = Resolver(api).resolve('https://space.bilibili.com/42/lists/88?type=season')
        self.assertEqual(len(result.episodes), 3)
        self.assertEqual(len([call for call in api.calls if 'seasons_archives_list' in call[0]]), 2)

    def test_partial_season_blocked(self):
        items = season_items()
        api = FakeAPI(batches={1: {'archives': items[:1], 'page': {'total': 3}},
                               2: {'archives': [], 'page': {'total': 3}}})
        with self.assertRaisesRegex(BiliAlbumError, '只读到'):
            Resolver(api).resolve('https://space.bilibili.com/42/lists/88?type=season')

    def test_series(self):
        api = FakeAPI(series={1: {'archives': season_items(), 'page': {'total': 3}}})
        result = Resolver(api).resolve('https://space.bilibili.com/42/lists/55?type=series')
        self.assertEqual(result.kind, 'series')
        self.assertEqual([x.title for x in result.episodes], ['最早', '中间', '最晚'])


if __name__ == '__main__':
    unittest.main()

class SeasonIntegrityTests(unittest.TestCase):
    def test_duplicate_bvid_refused(self):
        records = season_items()
        records[2]['bvid'] = records[0]['bvid']
        api = FakeAPI(batches={1: {'archives': records, 'page': {'total': 3}}})
        with self.assertRaisesRegex(BiliAlbumError, '重复'):
            Resolver(api).resolve('https://space.bilibili.com/42/lists/88?type=season')

    def test_no_proof_embedded_complete_refused(self):
        item = view(1)
        item['ugc_season'] = {'id': 88, 'title': '课', 'sections': [
            {'episodes': [{'arc': {'bvid': 'BV0000000000'}, 'title': '课程'}]}
        ]}
        api = FakeAPI(item, batches={1: BiliAlbumError('403')})
        with self.assertRaisesRegex(BiliAlbumError, '无法证明'):
            Resolver(api).resolve('https://www.bilibili.com/video/BV0000000000')

class AdditionalIntegrityTests(unittest.TestCase):
    def test_complete_embedded_season_survives_pagination_denial(self):
        item = view(3)
        item['ugc_season'] = {'id': 88, 'title': '课', 'ep_count': 3,
                              'sections': [{'episodes': season_items()}]}
        api = FakeAPI(item, batches={1: BiliAlbumError('HTTP 412')})
        result = Resolver(api).resolve('https://www.bilibili.com/video/BV0000000000?p=3')
        self.assertEqual(result.kind, 'season')
        selected = result.episodes[result.selected_number - 1]
        self.assertEqual((selected.page, selected.cid, selected.duration), (3, 300, 80))
        from bilialbum.selection import select_episodes
        chosen = select_episodes(result.episodes, 'custom', 1, '3,1')
        self.assertEqual([ep.title for ep in chosen], ['最早', '最晚'])
        self.assertEqual([ep.number for ep in chosen], [1, 3])

    def test_duplicate_embedded_season_not_accepted_as_complete(self):
        item = view(1)
        records = season_items()
        records[1] = records[0]
        item['ugc_season'] = {'id': 88, 'ep_count': 3, 'sections': [{'episodes': records}]}
        with self.assertRaisesRegex(BiliAlbumError, '重复'):
            Resolver(FakeAPI(item, batches={1: BiliAlbumError('HTTP 412')})).resolve('https://www.bilibili.com/video/BV0000000000')

    def test_single_record_labeled_p3_never_becomes_p1(self):
        item = view(1)
        item['pages'][0]['page'] = 3
        result = Resolver(FakeAPI(item)).resolve('https://www.bilibili.com/video/BV0000000000?p=3')
        self.assertEqual(result.episodes[0].page, 3)

    def test_duplicate_pages_rejected_before_selection(self):
        item = view(3)
        item['pages'][2]['page'] = 1
        with self.assertRaisesRegex(BiliAlbumError, '编号重复'):
            Resolver(FakeAPI(item)).resolve('https://www.bilibili.com/video/BV0000000000?p=3')

class InvalidPageNumberTests(unittest.TestCase):
    def test_zero_page_number_is_not_replaced_with_p3(self):
        item = view(3)
        item['pages'][2]['page'] = 0
        with self.assertRaisesRegex(BiliAlbumError, '编号重复或无效'):
            Resolver(FakeAPI(item)).resolve('https://www.bilibili.com/video/BV0000000000?p=3')
