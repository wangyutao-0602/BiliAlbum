"""Select only formats the anonymous yt-dlp extractor actually exposes."""
from __future__ import annotations
from .models import FormatChoice, BiliAlbumError

QUALITY_CHOICES = [
    ('自动：最高可用免费画质', 'auto'),
    ('2160P（若可用）', '2160'),
    ('1440P（若可用）', '1440'),
    ('1080P（若可用）', '1080'),
    ('720P（若可用）', '720'),
    ('480P（若可用）', '480'),
    ('360P（若可用）', '360'),
]


def _allowed(f: dict) -> bool:
    if f.get('has_drm') is True or f.get('drm') is True:
        return False
    note = str(f.get('format_note') or '').lower()
    return not any(word in note for word in ('premium only', 'vip only', 'drm protected'))


def _video(f: dict) -> bool:
    return f.get('vcodec') not in (None, 'none') and bool(f.get('format_id'))


def _audio(f: dict) -> bool:
    return f.get('acodec') not in (None, 'none') and bool(f.get('format_id'))


def available_heights(info: dict) -> list[int]:
    return sorted({int(f['height']) for f in (info.get('formats') or [])
                   if _allowed(f) and _video(f) and f.get('height') and int(f['height']) > 0}, reverse=True)


def choose_format(info: dict, requested: str, fallback: bool) -> FormatChoice:
    formats = [f for f in (info.get('formats') or []) if isinstance(f, dict) and _allowed(f)]
    videos = [f for f in formats if _video(f) and f.get('height')]
    if not videos:
        raise BiliAlbumError('没有检测到当前访客可用的视频画质（视频可能需要登录、付费或已下架）。')
    if requested != 'auto':
        try:
            target = int(requested)
        except ValueError as exc:
            raise BiliAlbumError('无效画质选择。') from exc
        exact = [f for f in videos if int(f['height']) == target]
        lower = [f for f in videos if int(f['height']) <= target]
        candidates = exact or (lower if fallback else [])
        if not candidates:
            available = ', '.join(f'{v}P' for v in available_heights(info))
            raise BiliAlbumError(f'本集没有 {target}P 画质。可用：{available}；请开启降级或换画质。')
    else:
        candidates = videos

    def rank(f):
        return (int(f.get('height') or 0), float(f.get('fps') or 0),
                float(f.get('tbr') or f.get('vbr') or 0),
                1 if 'avc' in str(f.get('vcodec') or '').lower() else 0)

    videos = sorted(candidates, key=rank, reverse=True)
    audios = sorted((f for f in formats if _audio(f) and not _video(f)),
                    key=lambda f: (float(f.get('abr') or f.get('tbr') or 0),
                                   1 if 'mp4a' in str(f.get('acodec') or '') else 0), reverse=True)
    for v in videos:
        if _audio(v):
            return FormatChoice(str(v['format_id']), int(v['height']), f'{v["height"]}P（音视频一体）')
        if audios:
            return FormatChoice(f'{v["format_id"]}+{audios[0]["format_id"]}', int(v['height']),
                                f'{v["height"]}P + 音频')
    raise BiliAlbumError('找到了视频画面，但没有可用音轨，拒绝输出意外无声的视频。')
