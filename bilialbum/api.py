"""Public Bilibili metadata access (no account, cookies or privileged endpoints).

The legacy public view API intermittently responds with HTTP 412.  When that
happens, the only fallback is to inspect initial-state JSON already delivered
in the corresponding *public* webpage.  If the webpage itself denies access,
we stop; this is not a challenge solver or an access-control bypass.
"""
from __future__ import annotations
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from .models import BiliAlbumError
from .models import NetworkFailure
from .network import network_failure, validate_proxies


class BiliAPI:
    BASE = 'https://api.bilibili.com'
    USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 Chrome/128.0 Safari/537.36')
    _INITIAL_STATE = re.compile(r'window\.__INITIAL_STATE__\s*=\s*')

    def __init__(self, *, direct=False):
        self.direct = direct

    def _open(self, request, *, timeout):
        if self.direct:
            return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)

    def get(self, route: str, params: dict) -> dict:
        if not self.direct:
            validate_proxies()
        if not route.startswith('/') or '://' in route:
            raise BiliAlbumError('非法接口路径。')
        url = self.BASE + route + '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            'User-Agent': self.USER_AGENT,
            'Referer': 'https://www.bilibili.com/',
            'Accept': 'application/json, text/plain, */*',
        })
        try:
            with self._open(req, timeout=18) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as err:
            if err.code == 412 and route == '/x/web-interface/view':
                return self._public_view_fallback(params)
            raise network_failure(f'Bilibili 元数据接口 {route}', err) from err
        except (urllib.error.URLError, ValueError, TimeoutError) as err:
            raise network_failure(f'Bilibili 元数据接口 {route}', err) from err
        data = self.parse_api_payload(payload)
        if route == '/x/web-interface/view':
            for key in ('bvid', 'aid'):
                if key in params and str(data.get(key)) != str(params[key]):
                    raise BiliAlbumError('视频 API 返回的标识与请求不一致，已拒绝错误的视频。')
        return data

    @staticmethod
    def parse_api_payload(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise BiliAlbumError('Bilibili 接口返回了无法识别的数据。')
        if payload.get('code') != 0:
            if payload.get('code') in (-352, -412, -403):
                raise NetworkFailure('视频元数据业务响应', 'server_denied',
                                     f'Bilibili 接口拒绝请求 (code={payload.get("code")})。',
                                     status=None, retryable=False)
            raise BiliAlbumError(f'Bilibili 接口拒绝请求：{payload.get("message", "未知原因")} (code={payload.get("code")})。请先确认视频可正常观看。')
        if not isinstance(payload.get('data'), dict):
            raise BiliAlbumError('Bilibili 接口没有返回有效的元数据对象。')
        return payload['data']

    def _public_view_fallback(self, params: dict) -> dict:
        """Read embedded data from the public video page after the legacy API's 412.

        Never solve challenges or retry blocked requests: failure is explicit.
        """
        bvid = params.get('bvid')
        aid = params.get('aid')
        if isinstance(bvid, str) and re.fullmatch(r'BV[A-Za-z0-9]{10}', bvid):
            video_id = bvid
        elif re.fullmatch(r'[0-9]{1,20}', str(aid or '')):
            video_id = 'av' + str(aid)
        else:
            raise BiliAlbumError('视频 API 返回 HTTP 412，且没有可供公共网页解析的有效视频 ID。')

        page_url = f'https://www.bilibili.com/video/{video_id}/'
        req = urllib.request.Request(page_url, headers={
            'User-Agent': self.USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml',
        })
        try:
            with self._open(req, timeout=18) as response:
                raw = response.read(8_000_001)
        except urllib.error.HTTPError as err:
            raise NetworkFailure('视频信息 API → 公开网页',
                'server_denied' if err.code in (403, 412, 429) else 'http_error',
                f'视频信息接口返回 412；尝试读取该视频公开网页时又收到 HTTP {err.code}。'
                '元数据尚未获取，未进入画质探测或下载阶段。',
                status=err.code, retryable=err.code not in (403, 412, 429),
            ) from err
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            raise network_failure('视频信息 API HTTP 412 后的公开网页', err) from err
        return self.parse_public_page(raw, params)

    @classmethod
    def parse_public_page(cls, raw: bytes, params: dict) -> dict:
        bvid, aid = params.get('bvid'), params.get('aid')
        if len(raw) > 8_000_000:
            raise BiliAlbumError('视频公开网页超过解析大小限制。')
        page = raw.decode('utf-8', errors='replace')
        match = cls._INITIAL_STATE.search(page)
        if not match:
            raise BiliAlbumError(
                '视频信息接口返回 412；公开网页没有可用的初始视频数据。'
                '可能存在页面验证或网站结构变化；程序不会跳过访问控制。'
            )
        try:
            start = match.end()
            while start < len(page) and page[start].isspace():
                start += 1
            state, _ = json.JSONDecoder().raw_decode(page, start)
        except (ValueError, TypeError) as err:
            raise BiliAlbumError('视频公开网页中的初始数据格式无法识别。') from err
        if not isinstance(state, dict) or not isinstance(state.get('videoData'), dict):
            raise BiliAlbumError('公开网页没有返回可用的视频元数据。')
        data = state['videoData']
        if bvid is not None and data.get('bvid') != bvid:
            raise BiliAlbumError('公开网页中的 BV 号与请求不一致，已拒绝错误的视频。')
        if aid is not None and str(data.get('aid')) != str(aid):
            raise BiliAlbumError('公开网页中的 av 号与请求不一致，已拒绝错误的视频。')
        if not data.get('bvid'):
            raise BiliAlbumError('公开网页返回的数据没有有效 BV 号。')
        return data
