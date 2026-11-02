"""Official yt-dlp extraction with the same public page retained for collections."""
from __future__ import annotations

import io
import json
import math
import re
import time
from urllib.parse import urlsplit

from .api import BiliAPI
from .extraction import RequestTrace, SilentLogger, extract_single, metadata_options
from .models import BiliAlbumError, Episode
from .network import validate_proxies


def embedded_info(raw: bytes, data: dict, target):
    """Use public DASH data only when the active page CID proves the requested P.

    Page JSON is decoded as a complete object; matching fragments between
    unrelated video/audio keys can silently select another video or break JSON.
    """
    from .downloader import validate_episode_info

    try:
        pages = data.get('pages') or []
        page = next(p for p in pages if isinstance(p, dict) and p.get('page') == target.page)
        cid = page.get('cid')
        # Some pages always embed P1's play info even when ?p=3 is requested.
        # Missing or conflicting CID is a reason to defer to the official extractor.
        if not cid or str(data.get('cid')) != str(cid):
            return None
        rights = data.get('rights') or {}
        if (data.get('is_upower_exclusive') or data.get('is_chargeable_season')
                or any(rights.get(key) for key in ('pay', 'ugc_pay', 'ugc_pay_preview'))):
            return None
        text = raw.decode('utf-8', errors='replace')
        match = re.search(r'window\.__playinfo__\s*=\s*', text)
        if not match:
            return None
        payload, _ = json.JSONDecoder().raw_decode(text, match.end())
        if payload.get('code') != 0:
            return None
        play = payload['data']
        if play.get('cid') is not None and str(play['cid']) != str(cid):
            return None
        duration = float(play['timelength']) / 1000
        if not math.isfinite(duration) or duration <= 0:
            return None
        dash = play['dash']
        formats = []
        for kind in ('video', 'audio'):
            for index, media in enumerate(dash.get(kind) or []):
                if (not isinstance(media, dict) or media.get('has_drm') or media.get('drm')
                        or media.get('drm_tech_type')):
                    continue
                candidates = [media.get('baseUrl'), media.get('base_url')]
                for key in ('backupUrl', 'backup_url'):
                    backups = media.get(key)
                    candidates.extend(backups if isinstance(backups, list) else [])
                url = next((u for u in candidates if isinstance(u, str)
                            and urlsplit(u).scheme in ('http', 'https') and urlsplit(u).hostname), None)
                codec = media.get('codecs')
                if not url or not codec:
                    continue
                fmt = {'format_id': f'embedded-{kind}-{index}', 'url': url,
                       'ext': 'mp4' if kind == 'video' else 'm4a',
                       'vcodec': codec if kind == 'video' else 'none',
                       'acodec': codec if kind == 'audio' else 'none',
                       'tbr': float(media.get('bandwidth') or 0) / 1000}
                if kind == 'video':
                    height = int(media.get('height') or 0)
                    if height <= 0:
                        continue
                    fmt.update(height=height, width=media.get('width'))
                formats.append(fmt)
        if not any(f['vcodec'] != 'none' for f in formats) or not any(f['acodec'] != 'none' for f in formats):
            return None
        info = {'id': f'{data["bvid"]}_p{target.page}',
                'cid': cid,
                'title': str(data.get('title') or data['bvid']),
                'duration': duration, 'formats': formats,
                'http_headers': {'Referer': f'https://www.bilibili.com/video/{data["bvid"]}/?p={target.page}'},
                'extractor': 'BiliBili', 'extractor_key': 'BiliBili',
                'webpage_url': target.url, '_bilialbum_embedded': True,
                '_bilialbum_received_at': time.monotonic()}
        validate_episode_info(info, Episode(1, info['title'], data['bvid'], target.page,
                                           duration=page.get('duration')))
        return info
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration, BiliAlbumError):
        return None


def extract_view(target, *, direct=False, embedded_only=False):
    """Fetch one public page, then use embedded DASH or official extraction.

    Retain the same initial state for all page CIDs and ugc_season. The official
    extractor receives a buffered copy of this page if DASH cannot be verified.
    Neither path rereads the page, loads account cookies, or downloads media.
    """
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.networking import Response
    except ImportError as exc:
        raise BiliAlbumError('yt-dlp 尚未安装，无法使用官方提取器解析。') from exc
    from .downloader import validate_episode_info

    params = ({'bvid': target.video_id} if target.video_id.startswith('BV')
              else {'aid': target.video_id[2:]})
    url = f'https://www.bilibili.com/video/{target.video_id}/?p={target.page}'
    trace = RequestTrace(limit=10)
    if not direct:
        validate_proxies()
    try:
        with trace.ydl_class(YoutubeDL)({**metadata_options(direct=direct), 'logger': SilentLogger()}) as ydl:
            with ydl.urlopen(url) as response:
                raw = response.read(8_000_001)
                final_url, headers, status, reason = response.url, response.headers, response.status, response.reason
    except BiliAlbumError:
        raise
    except Exception as exc:
        raise trace.failure(exc) from exc
    data = BiliAPI.parse_public_page(raw, params)
    pages = data.get('pages')
    if not isinstance(pages, list) or not pages:
        raise BiliAlbumError('yt-dlp 网页缺少完整分 P 数据，已阻止不完整下载。')
    info = embedded_info(raw, data, target)
    if info is not None:
        return data, info
    if embedded_only:
        raise BiliAlbumError('当前网页没有可核对分 P 的免费音视频数据。')

    class MetadataYDL(YoutubeDL):
        page_used = False

        def urlopen(self, request):
            requested = request if isinstance(request, str) else getattr(request, 'url', None) or request.full_url
            if requested == url and not self.page_used:
                self.page_used = True
                return Response(io.BytesIO(raw), final_url, headers, status, reason)
            return super().urlopen(request)

    info = extract_single(MetadataYDL, url, direct=direct, trace=trace)
    validate_episode_info(info, Episode(1, str(data.get('title') or ''), data['bvid'], target.page))
    return data, info
