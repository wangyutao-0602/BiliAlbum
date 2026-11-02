"""Use yt-dlp's public single-video API with bounded requests and stage tracing."""
from __future__ import annotations
import urllib.parse

from .models import BiliAlbumError, NetworkFailure
from .network import DENIED, exception_details, safe_url, validate_proxies


def request_stage(url: str) -> str:
    if '/x/player/pagelist' in url:
        return '分 P 列表'
    if 'playurl' in url:
        return '播放信息'
    if '/x/web-interface/nav' in url:
        return '公开签名参数'
    if '/x/player/' in url:
        return '章节或字幕信息'
    if '/x/' in url:
        return '元数据 API'
    host = urllib.parse.urlsplit(url).hostname or ''
    if host.endswith('.bilivideo.com') or host.endswith('.bilivideo.cn'):
        return '媒体流'
    if host == 'bilibili.com' or host.endswith('.bilibili.com'):
        return '公开视频网页'
    return '网络请求'


def metadata_options(*, direct=False) -> dict:
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
            'skip_download': True, 'extract_flat': False, 'ignoreerrors': False,
            'writesubtitles': False, 'getcomments': False, 'cachedir': False,
            'retries': 0, 'extractor_retries': 0, 'fragment_retries': 0,
            'socket_timeout': 12}
    if direct:
        opts['proxy'] = ''  # Official yt-dlp option: ignore system/environment proxies.
    return opts


class SilentLogger:
    # Raw yt-dlp messages can contain proxy credentials or signed media URLs.
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class RequestTrace:
    def __init__(self, limit=None):
        self.records = []
        self.limit = limit
        self.denied = set()

    def ydl_class(self, base):
        trace = self

        class TracedYDL(base):
            def urlopen(self, req):
                url = req if isinstance(req, str) else getattr(req, 'url', None) or req.full_url
                stage = request_stage(url)
                safe = safe_url(url)
                if safe in trace.denied or (trace.limit is not None and len(trace.records) >= trace.limit):
                    raise NetworkFailure(stage, 'request_budget',
                                         f'yt-dlp {stage}：已停止重复的被拒请求或超过诊断请求上限。',
                                         retryable=False)
                row = {'target': safe, 'method': getattr(req, 'method', 'GET'),
                       'stage': stage, 'status': None, 'exception': None,
                       'final_url': safe, 'redirected': None}
                trace.records.append(row)
                try:
                    response = super().urlopen(req)
                    row.update(status=response.status, final_url=safe_url(response.url),
                               redirected=response.url != url)
                    return response
                except Exception as exc:
                    status, category = exception_details(exc)
                    response = getattr(exc, 'response', None)
                    final = getattr(response, 'url', None)
                    row.update(status=status, exception=type(exc).__name__, category=category,
                               final_url=safe_url(final) if final else safe,
                               redirected=(final != url) if final else None)
                    if status in DENIED:
                        trace.denied.add(safe)
                    raise

        return TracedYDL

    def failure(self, exc) -> NetworkFailure:
        status, category = exception_details(exc)
        # A nonfatal request may have failed earlier. Only attribute the final
        # exception to it when the actual cause/status matches.
        failed = [row for row in self.records if row.get('exception') and
                  ((status is not None and row.get('status') == status) or
                   (status is None and category != 'network_or_extractor' and
                    row.get('category') == category))]
        row = failed[-1] if failed else {}
        stage = 'yt-dlp ' + row.get('stage', '元数据提取')
        detail = f'HTTP {status}' if status else category
        return NetworkFailure(stage, category, f'{stage}失败：{detail}。', status=status,
                              retryable=category not in ('server_denied', 'tls', 'request_budget'))


def extract_single(base, url, *, trace=None, direct=False):
    if not direct:
        validate_proxies()
    trace = trace or RequestTrace()
    opts = metadata_options(direct=direct)
    opts['logger'] = SilentLogger()
    try:
        with trace.ydl_class(base)(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except BiliAlbumError:
        raise
    except Exception as exc:
        raise trace.failure(exc) from exc
