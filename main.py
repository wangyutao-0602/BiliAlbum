#!/usr/bin/env python3
"""Run without arguments for GUI, or pass --url for headless CLI."""
from __future__ import annotations
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='BiliAlbum - public Bilibili video exporter')
    parser.add_argument('--url', help='Bilibili BV/av video URL or space lists URL')
    parser.add_argument('--list', action='store_true', help='Only print ordered episode list')
    parser.add_argument('--direct', action='store_true', help='Ignore proxies in this process only; does not change system/VPN settings')
    parser.add_argument('--mode', choices=('single', 'all', 'custom'), default='single')
    parser.add_argument('--episode', type=int, help='Episode number for --mode single')
    parser.add_argument('--episodes', default='', help='Examples: 1,3,5-8')
    parser.add_argument('--quality', choices=('auto', '2160', '1440', '1080', '720', '480', '360'), default='auto')
    parser.add_argument('--no-fallback', action='store_true')
    parser.add_argument('--output', default=str(Path.home() / 'Downloads' / 'Bilibili'))
    args = parser.parse_args()
    if args.list and not args.url:
        parser.error('--list requires --url')
    if not args.url:
        from bilialbum.gui import start_gui
        start_gui(direct=args.direct)
        return 0
    from bilialbum.resolver import Resolver
    from bilialbum.selection import select_episodes
    from bilialbum.downloader import DownloadManager
    from bilialbum.models import BiliAlbumError, Cancelled
    try:
        col = Resolver(direct=args.direct).resolve(args.url)
        print(f'{col.title}\n{col.kind}, {len(col.episodes)} 集 | {col.order_note}')
        for ep in col.episodes:
            mark = '*' if ep.number == col.selected_number else ' '
            print(f'{mark} {ep.number:03d} {ep.title} | {ep.key}')
        if args.list:
            return 0
        selected = col.selected_number if args.episode is None else args.episode
        items = select_episodes(col.episodes, args.mode, selected, args.episodes)
        print(f'准备下载 {len(items)} 集 | 画质 {args.quality} | 输出到 {args.output}')
        def progress(stage, message, percent):
            if stage in ('下载', '转换'):
                print(f'\r[{stage}] {percent:5.0%} {message[:90]:90}', end='', flush=True)
            else:
                print(f'\n[{stage}] {message}')
        manager = DownloadManager(Path(args.output), progress, direct=args.direct)
        try:
            done, failures = manager.run(col, items, args.quality, not args.no_fallback)
        except KeyboardInterrupt:
            manager.cancel.set()
            print('\n用户中断。')
            return 130
        print(f'\n完成：成功/已存在 {done}，失败 {failures}')
        return 0 if failures == 0 else 2
    except (BiliAlbumError, Cancelled) as exc:
        print(f'错误：{exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
