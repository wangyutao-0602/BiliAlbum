import queue
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bilialbum.gui import BiliAlbumApp
from bilialbum.models import Collection, Episode, NetworkFailure

BV = 'BV0000000000'


class GUITests(unittest.TestCase):
    def test_download_denial_keeps_stage_and_retry_guidance(self):
        error = NetworkFailure('yt-dlp 媒体流', 'server_denied', 'yt-dlp 媒体流失败：HTTP 412。',
                               status=412, retryable=False)
        fake = SimpleNamespace(events=queue.Queue(), manager=MagicMock())
        fake.manager.run.side_effect = error
        BiliAlbumApp._download_worker(fake, None, [], 'auto', True)
        kind, message = fake.events.get_nowait()
        self.assertEqual(kind, 'download_error')
        self.assertIn('媒体流', message)
        self.assertIn('停止自动重试', message)

    def test_all_failed_downloads_are_not_reported_as_completed(self):
        fake = MagicMock()
        fake.events = queue.Queue()
        fake.events.put(('done', (0, 2)))
        fake.events.put(('download_end',))
        with patch('bilialbum.gui.messagebox.showwarning') as warning, patch('bilialbum.gui.messagebox.showinfo') as info:
            BiliAlbumApp._poll(fake)
        fake.state.set.assert_called_with('下载失败')
        warning.assert_called_once()
        info.assert_not_called()

    def test_resolution_error_goes_to_ui_without_quality_requests(self):
        fake = SimpleNamespace(events=queue.Queue())
        error = NetworkFailure('视频信息 API → 公开网页', 'server_denied', 'API HTTP 412；网页 HTTP 412', status=412, retryable=False)
        with patch('bilialbum.gui.Resolver') as resolver, patch('bilialbum.gui.probe_episode') as probe:
            resolver.return_value.resolve.side_effect = error
            BiliAlbumApp._resolve_worker(fake, f'https://www.bilibili.com/video/{BV}')
        kind, message = fake.events.get_nowait()
        self.assertEqual(kind, 'resolve_error')
        self.assertIn('停止自动重试', message)
        self.assertIn('当前链接暂不能开始下载', message)
        self.assertEqual(fake.events.get_nowait(), ('resolve_end',))
        probe.assert_not_called()

    def test_error_clears_stale_collection_and_disables_download(self):
        fake = MagicMock()
        fake.events = queue.Queue()
        fake.events.put(('resolve_error', 'API HTTP 412；网页 HTTP 412'))
        fake.events.put(('resolve_end',))
        with patch('bilialbum.gui.messagebox.showerror') as popup:
            BiliAlbumApp._poll(fake)
        self.assertIsNone(fake.collection)
        fake.start_button.configure.assert_called_with(state='disabled')
        fake.parse_button.configure.assert_called_with(state='normal')
        popup.assert_called_once_with('解析失败', 'API HTTP 412；网页 HTTP 412')

    def test_worker_probes_only_selected_p3(self):
        episodes = [Episode(i, str(i), BV, i) for i in range(1, 4)]
        col = Collection('test', 'multip', episodes, 3, 'P order', '')
        fake = SimpleNamespace(events=queue.Queue())
        with patch('bilialbum.gui.Resolver') as resolver, patch('bilialbum.gui.probe_episode', return_value={'formats': []}) as probe:
            resolver.return_value.resolve.return_value = col
            BiliAlbumApp._resolve_worker(fake, 'test')
        probe.assert_called_once_with(episodes[2], direct=False)
        self.assertEqual(fake.events.get_nowait(), ('resolved', col))

    def test_apply_result_selects_p3_and_keeps_all_custom_enabled(self):
        episodes = [Episode(i, str(i), BV, i) for i in range(1, 4)]
        col = Collection('test', 'multip', episodes, 3, 'P order', '')
        fake = MagicMock()
        BiliAlbumApp._apply_result(fake, col)
        fake.table.selection_set.assert_called_once_with('3')
        fake.all_radio.configure.assert_called_once_with(state='normal')
        fake.custom_radio.configure.assert_called_once_with(state='normal')
