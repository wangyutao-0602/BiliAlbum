"""Real Tk widgets, offline transport fixtures; no Bilibili requests or downloads."""
import json
import time
import unittest.mock as mock
from bilialbum.gui import BiliAlbumApp
from bilialbum.models import NetworkFailure
from bilialbum.resolver import Resolver
from bilialbum.api import BiliAPI

BV = 'BV0000000000'
app = BiliAlbumApp(direct=True)
popups = []
try:
    with mock.patch('bilialbum.gui.Resolver') as resolver, mock.patch('bilialbum.gui.messagebox.showerror', side_effect=lambda *args: popups.append(args)):
        assert app.direct.get() is True
        resolver.return_value.resolve.side_effect = NetworkFailure(
            '视频信息 API → 公开网页', 'server_denied', '视频信息 API HTTP 412；公开网页 HTTP 412。', status=412, retryable=False)
        app.url.set(f'https://www.bilibili.com/video/{BV}/')
        app.resolve()
        deadline = time.monotonic() + 3
        while app.state.get() != '解析失败' and time.monotonic() < deadline:
            app.update()
            time.sleep(.01)
        assert app.state.get() == '解析失败'
        assert str(app.start_button.cget('state')) == 'disabled'
        assert popups and '停止自动重试' in popups[0][1]
        assert app.collection is None
        resolver.assert_called_once_with(direct=True)
    data = {'bvid': BV, 'title': 'Tk P3 验收（离线测试数据）', 'pages': [
        {'page': i, 'part': str(i), 'cid': i * 10} for i in range(1, 4)]}
    # Exercise the real resolver, worker, event queue and widgets with a mocked API transport.
    with mock.patch('bilialbum.resolver.extract_view', return_value=(data, {'formats': []})), mock.patch('bilialbum.gui.probe_episode', return_value={'formats': []}):
        app.url.set(f'https://www.bilibili.com/video/{BV}/?p=3')
        app.resolve()
        deadline = time.monotonic() + 3
        while app.state.get() != '解析完成' and time.monotonic() < deadline:
            app.update()
            time.sleep(.01)
        assert app.state.get() == '解析完成'
        assert app.table.selection() == ('3',)
        assert app.collection.episodes[2].page == 3
        assert str(app.start_button.cget('state')) == 'normal'
        assert str(app.all_radio.cget('state')) == 'normal'
        assert str(app.custom_radio.cget('state')) == 'normal'
    print(json.dumps({'real_tk': True, 'offline_fixtures': True, '412_dialog_and_disabled_download': True,
                      'real_resolver_worker_queue': True, 'selected_p3': True, 'all_custom_enabled': True}))
finally:
    app.destroy()
