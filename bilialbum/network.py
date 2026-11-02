"""Shared, credential-free network diagnostics; never changes system settings."""
from __future__ import annotations

import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .models import NetworkFailure

DENIED = {403, 412, 429}
SAFE_QUERY = {'bvid', 'aid', 'p', 'cid', 'mid', 'season_id', 'series_id',
              'page_num', 'page_size', 'pn', 'ps', 'jsonp', 'platform', 'qn', 'fnval'}
SAFE_PATHS = {'/x/web-interface/view', '/x/web-interface/nav', '/x/player/pagelist',
              '/x/player/wbi/playurl', '/x/player/wbi/v2', '/x/player/v2',
              '/x/web-interface/wbi/view/detail', '/x/polymer/web-space/seasons_archives_list',
              '/x/series/archives'}


def safe_url(value: str) -> str:
    """Only retain public video identifiers and numeric pagination parameters."""
    try:
        parts = urllib.parse.urlsplit(str(value))
        host = parts.hostname or ''
        port = parts.port
        netloc = host + (f':{port}' if port else '')
        # Unknown redirected paths can contain tokens; only expose Bilibili public routes.
        public_host = host == 'bilibili.com' or host.endswith('.bilibili.com')
        public_path = parts.path in SAFE_PATHS or re.fullmatch(r'/video/(?:BV[A-Za-z0-9]{10}|av\d+)/?', parts.path)
        path = parts.path if public_host and public_path else '/'
        query = []
        if public_host and public_path:
            patterns = {'bvid': r'BV[A-Za-z0-9]{10}', 'jsonp': r'jsonp', 'platform': r'web'}
            query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
                     if k in SAFE_QUERY and re.fullmatch(patterns.get(k, r'[0-9]{1,20}'), v)]
        return urllib.parse.urlunsplit((parts.scheme, netloc, path,
                                        urllib.parse.urlencode(query), ''))
    except ValueError:
        return '<invalid-url>'


def proxy_summary(proxies: dict) -> dict:
    result = {}
    for key, value in proxies.items():
        if key == 'no':
            result[key] = {'configured': bool(value), 'entries': len(str(value).split(','))}
            continue
        if value in ('', '__noproxy__'):
            result[key] = {'disabled': True}
            continue
        try:
            parsed = urllib.parse.urlsplit(str(value))
            port = parsed.port
            result[key] = {'scheme': parsed.scheme, 'host': '<redacted>' if parsed.hostname else None,
                           'loopback': parsed.hostname in ('localhost', '127.0.0.1', '::1'),
                           'port': port, 'credentials_present': bool(parsed.username or parsed.password)}
        except ValueError:
            result[key] = {'invalid': True}
    return result


def validate_proxies(proxies: dict | None = None) -> None:
    proxies = urllib.request.getproxies() if proxies is None else proxies
    for key in ('http', 'https', 'all'):
        value = proxies.get(key)
        if not value or value == '__noproxy__':
            continue
        try:
            parsed = urllib.parse.urlsplit(value)
            if (parsed.scheme not in ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h')
                    or not parsed.hostname or parsed.port == 0 or parsed.path not in ('', '/')
                    or parsed.query or parsed.fragment):
                raise ValueError()
            parsed.port
        except ValueError:
            raise NetworkFailure('代理配置', 'invalid_proxy',
                                 f'{key.upper()} 代理格式无效；请核对启动程序的代理配置（地址已隐藏）。',
                                 retryable=False) from None


def exception_details(exc: BaseException) -> tuple[int | None, str]:
    seen = set()
    pending = [exc]
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        status = getattr(current, 'status', None) or getattr(current, 'code', None)
        if isinstance(status, int) and 100 <= status < 600:
            return status, 'server_denied' if status in DENIED else 'http_error'
        if isinstance(current, socket.gaierror):
            return None, 'dns'
        if isinstance(current, ssl.SSLError):
            return None, 'tls'
        if isinstance(current, (TimeoutError, socket.timeout)):
            return None, 'timeout'
        for attr in ('reason', 'cause', '__cause__', '__context__'):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
        info = getattr(current, 'exc_info', None)
        if isinstance(info, tuple) and len(info) > 1 and isinstance(info[1], BaseException):
            pending.append(info[1])
    # yt-dlp sometimes only preserves the HTTP status in DownloadError's text.
    match = re.search(r'HTTP (?:Error |Status )?(403|412|429)\b', str(exc))
    return (int(match[1]), 'server_denied') if match else (None, 'network_or_extractor')


def network_failure(stage: str, exc: BaseException) -> NetworkFailure:
    status, category = exception_details(exc)
    detail = f'HTTP {status}' if status else {
        'dns': 'DNS 解析失败', 'tls': 'TLS 证书或连接失败', 'timeout': '连接超时',
    }.get(category, type(exc).__name__)
    return NetworkFailure(stage, category, f'{stage}失败：{detail}。', status=status,
                          retryable=category not in ('server_denied', 'tls'))


def gui_error_message(exc: BaseException, *, downloading=False) -> str:
    message = str(exc)
    if isinstance(exc, NetworkFailure):
        if exc.category == 'server_denied':
            message += ('\n服务器拒绝访问，已停止自动重试。请在浏览器核实页面可访问，'
                        '并比较系统代理与终端代理；稍后再试。')
        elif exc.category == 'invalid_proxy':
            message += '\n程序没有更改系统代理。'
        message += ('\n当前下载已停止。' if downloading else '\n当前链接暂不能开始下载。')
        message += '其他正常可访问的视频仍可解析；已有 MP4 文件可继续使用。'
    return message
