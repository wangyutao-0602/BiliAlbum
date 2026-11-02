from __future__ import annotations
import re
from .models import Episode, BiliAlbumError


def parse_episodes(expr: str, count: int) -> list[int]:
    """1,3,5-8 => [1,3,5,6,7,8]. Invalid ranges fail atomically."""
    expr = expr.translate(str.maketrans({'，': ',', '－': '-', '—': '-', '–': '-'})).replace(' ', '')
    if not expr:
        raise BiliAlbumError('请填写集数，例如 1,3,5-8。')
    values: set[int] = set()
    for item in expr.split(','):
        if re.fullmatch(r'\d+', item):
            left = right = int(item)
        elif re.fullmatch(r'\d+-\d+', item):
            left, right = map(int, item.split('-'))
        else:
            raise BiliAlbumError(f'集数格式错误：{item!r}。示例：1,3,5-8')
        if left < 1 or right < left or right > count:
            raise BiliAlbumError(f'集数 {item} 超出有效范围 1–{count} 或区间顺序错误。')
        values.update(range(left, right + 1))
    return sorted(values)


def select_episodes(episodes: list[Episode], mode: str, single: int, custom: str) -> list[Episode]:
    if mode == 'all':
        wanted = list(range(1, len(episodes) + 1))
    elif mode == 'single':
        wanted = parse_episodes(str(single), len(episodes))
    elif mode == 'custom':
        wanted = parse_episodes(custom, len(episodes))
    else:
        raise BiliAlbumError('未知下载模式。')
    return [episodes[i - 1] for i in wanted]
