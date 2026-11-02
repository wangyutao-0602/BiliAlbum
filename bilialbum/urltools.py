"""Parse and normalize Bilibili links without following arbitrary hosts."""
from __future__ import annotations
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from .models import BiliAlbumError
from .network import network_failure, validate_proxies

VIDEO = re.compile(r'^/video/(BV[0-9A-Za-z]{10}|av[0-9]{1,20})/?$', re.I | re.ASCII)
SPACE_LIST = re.compile(r'^/([0-9]{1,20})/lists/([0-9]{1,20})/?$', re.I)


@dataclass(frozen=True)
class ParsedURL:
    url: str
    video_id: str | None = None
    page: int = 1
    mid: str | None = None
    list_id: str | None = None
    list_type: str | None = None


def allowed_host(host: str | None) -> bool:
    host = (host or '').lower().rstrip('.')
    return host in ('bilibili.com', 'b23.tv') or host.endswith('.bilibili.com') or host.endswith('.b23.tv')


def _checked_parts(url):
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or not allowed_host(parts.hostname):
            raise BiliAlbumError('只允许 HTTPS 的 Bilibili/b23.tv 网址，不接受其他站点。')
        if parts.username or parts.password or parts.port not in (None, 443):
            raise BiliAlbumError('链接包含不允许的端口或账户信息。')
        return parts
    except ValueError:
        raise BiliAlbumError('网址格式或端口无效。') from None


def _page_parameter(query):
    values = query.get('p', ['1'])
    if len(set(values)) != 1:
        raise BiliAlbumError('网址包含冲突的 p 分集参数。')
    value = values[-1]
    if len(value) > 5 or not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 20000:
        raise BiliAlbumError('网址中的 p 分集参数无效。')
    return int(value)


def parse_url(value: str) -> ParsedURL:
    url = value.strip()
    if not url:
        raise BiliAlbumError('请先粘贴一个 Bilibili 视频网址。')
    if not url.startswith(('https://', 'http://')):
        url = 'https://' + url
    parts = _checked_parts(url)
    matched = VIDEO.fullmatch(parts.path)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if matched:
        page = _page_parameter(query)
        vid = matched.group(1)
        if vid.lower().startswith('bv'):
            vid = 'BV' + vid[2:]
        elif vid.lower().startswith('av'):
            vid = 'av' + vid[2:]
        return ParsedURL(url, video_id=vid, page=page)
    list_match = SPACE_LIST.fullmatch(parts.path)
    if list_match and parts.hostname and parts.hostname.lower() == 'space.bilibili.com':
        typ = query.get('type', ['season'])[0]
        if typ not in ('season', 'series'):
            raise BiliAlbumError('只支持 type=season 或 type=series 的 UP 主列表。')
        return ParsedURL(url, mid=list_match.group(1), list_id=list_match.group(2), list_type=typ)
    raise BiliAlbumError('暂不支持此链接结构。请使用 BV/av 视频链接或 UP 主的 lists 合集链接。')


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _checked_parts(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ResolvedShortLink(Exception):
    def __init__(self, url):
        self.url = url


class _ShortLinkRedirect(_SafeRedirect):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = _checked_parts(newurl)
        if not (parts.hostname or '').lower().rstrip('.').endswith('b23.tv'):
            try:
                parse_url(newurl)
            except BiliAlbumError:
                pass
            else:
                # The Location already resolves to a supported public URL.
                # Do not fetch the landing webpage just to learn its address.
                raise _ResolvedShortLink(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def normalize_url(value: str, *, direct=False) -> ParsedURL:
    """Only b23.tv requires a network redirect; other links stay untouched."""
    url = value.strip()
    if not url.startswith(('https://', 'http://')):
        url = 'https://' + url
    parts = _checked_parts(url)
    if (parts.hostname or '').lower().rstrip('.').endswith('b23.tv'):
        if parts.scheme != 'https':
            raise BiliAlbumError('短网址必须使用 HTTPS。')
        original_query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        requested_page = _page_parameter(original_query) if 'p' in original_query else None
        handlers = [_ShortLinkRedirect(), urllib.request.ProxyHandler({})] if direct else [_ShortLinkRedirect()]
        opener = urllib.request.build_opener(*handlers)
        if not direct:
            validate_proxies()
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (BiliAlbum)'})
        try:
            with opener.open(req, timeout=15) as response:
                url = response.geturl()
        except _ResolvedShortLink as resolved:
            url = resolved.url
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            raise network_failure('b23.tv 短链接重定向', err) from err
        if requested_page is not None:
            final = _checked_parts(url)
            query = [(k, v) for k, v in urllib.parse.parse_qsl(final.query) if k != 'p']
            query.append(('p', str(requested_page)))
            url = urllib.parse.urlunsplit(final._replace(query=urllib.parse.urlencode(query)))
    return parse_url(url)
