"""Sequential, cancellable downloads using yt-dlp's supported Python API."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import threading
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from .media import convert, ensure_ffmpeg, verify_output, probe
from .models import Episode, Collection, BiliAlbumError, Cancelled
from .quality import choose_format
from .extraction import extract_single, SilentLogger, RequestTrace
from .models import NetworkFailure
from .network import network_failure

EventCallback = Callable[[str, str, float], None]


def safe_name(text: str, limit: int = 95) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(text))
    text = re.sub(r'\s+', ' ', text).strip(' .')
    text = text[:limit].rstrip(' .')
    while len(text.encode('utf-8')) > 180:
        text = text[:-1]
    return text or '未命名'


def _import_ydl():
    try:
        from yt_dlp import YoutubeDL
        return YoutubeDL
    except ImportError as exc:
        raise BiliAlbumError('yt-dlp 尚未安装。请先执行安装脚本。') from exc


def assert_identity(info: dict, ep: Episode):
    vid = str(info.get('id') or '')
    # For multi-P, the extractor usually emits BVxxxx_p3. A _p1 suffix
    # is optional for single-page videos. A requested P>1 MUST match.
    if not (vid == ep.bvid and ep.page == 1) and vid != f'{ep.bvid}_p{ep.page}':
        raise BiliAlbumError(f'分集校验失败：网址需要 {ep.key}，实际解析到 {vid!r}。已阻止错误下载。')
    if ep.cid is not None and info.get('cid') is not None and str(ep.cid) != str(info['cid']):
        raise BiliAlbumError('分集 CID 与解析时不一致，已阻止下载可能被替换的视频。')


def probe_episode(ep: Episode, *, direct=False):
    YoutubeDL = _import_ydl()
    try:
        info = extract_single(YoutubeDL, ep.url, direct=direct)
    except NetworkFailure as exc:
        if exc.status != 412 or exc.stage not in ('yt-dlp 播放信息', 'yt-dlp 章节或字幕信息', 'yt-dlp 分 P 列表'):
            raise
        # A public page may already contain usable DASH despite a later API 412.
        # Only inspect that data; never repeat the rejected playback API here.
        from .metadata import extract_view
        from .urltools import parse_url
        try:
            _, info = extract_view(parse_url(ep.url), direct=direct, embedded_only=True)
        except BiliAlbumError:
            raise exc from None
    validate_episode_info(info, ep)
    return info


def validate_episode_info(info, ep):
    if not isinstance(info, dict) or info.get('_type') in ('playlist', 'multi_video'):
        raise BiliAlbumError('预期单集却解析到了播放列表，已停止下载。')
    assert_identity(info, ep)
    availability = str(info.get('availability') or '').lower()
    if availability in ('premium_only', 'subscriber_only', 'needs_auth', 'private'):
        raise BiliAlbumError('该视频标记为需要会员或登录，匿名模式不会下载。')
    duration = info.get('duration')
    if duration is not None:
        try:
            duration = float(duration)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise BiliAlbumError('提取器返回的分集时长无效。') from None
    if ep.duration and duration:
        if duration < float(ep.duration) * .90 and float(ep.duration) - duration > 6:
            raise BiliAlbumError('当前匿名模式可能只能获得视频试看片段，已拒绝下载。')


def _unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    for i in range(2, 10000):
        candidate = target.with_name(f'{target.stem} ({i}){target.suffix}')
        if not candidate.exists():
            return candidate
    raise BiliAlbumError('输出目录中重名文件过多。')


def _read_history(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_history(path: Path, content: dict):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _source_file(temp_dir: Path) -> Path | None:
    candidates = [p for p in temp_dir.glob('source.*') if p.is_file() and
                  p.stem == 'source' and
                  p.suffix.lower() in ('.mkv', '.mp4', '.webm', '.flv', '.mov') and p.stat().st_size > 1024]
    # Prefer the one that was last completed (skip partial extensions).
    for path in sorted(candidates, key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            obj = probe(path)
            types = {s.get('codec_type') for s in obj.get('streams', [])}
            if (obj.get('format') or {}).get('duration') and {'video', 'audio'} <= types:
                return path
        except BiliAlbumError:
            continue
    return None


def _completed_source(temp_dir, ep, selector):
    """Only reuse a source after a completed, identity-checked download/merge."""
    try:
        marker = json.loads((temp_dir / 'source.complete.json').read_text())
        if marker.get('key') != ep.key or marker.get('cid') != ep.cid or marker.get('selector') != selector:
            return None
        source = _source_file(temp_dir)
        if source and marker.get('file') == source.name and marker.get('size') == source.stat().st_size:
            return source
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _discard_source(temp_dir: Path):
    """Invalidate rejected media and its completion marker before another attempt."""
    for path in temp_dir.glob('source.*'):
        if path.is_file():
            path.unlink()


class DownloadManager:
    def __init__(self, output: Path, on_event: EventCallback | None = None, *, direct=False):
        self.output = Path(output).expanduser()
        self.on_event = on_event or (lambda stage, message, progress: None)
        self.cancel = threading.Event()
        self.direct = direct

    def emit(self, stage: str, message: str, progress: float = 0):
        self.on_event(stage, message, min(max(progress, 0), 1))

    def run(self, collection: Collection, episodes: list[Episode], quality: str, fallback: bool) -> tuple[int, int]:
        ensure_ffmpeg()
        self.output.mkdir(parents=True, exist_ok=True)
        dest_dir = self.output / safe_name(collection.title) if collection.is_collection else self.output
        dest_dir.mkdir(parents=True, exist_ok=True)
        history_file = self.output / '.bilialbum-history.json'
        history = _read_history(history_file)
        successful = 0
        failed = 0
        for index, ep in enumerate(episodes, 1):
            if self.cancel.is_set():
                raise Cancelled('任务已取消。')
            self.emit('开始', f'[{index}/{len(episodes)}] {ep.title}', 0)
            try:
                if self._one(ep, collection, quality, fallback, dest_dir, history, history_file):
                    successful += 1
                else:
                    successful += 1  # already present and validated
            except Cancelled:
                raise
            except NetworkFailure as exc:
                self.emit('失败', f'{ep.title}：{exc}', 0)
                if not exc.retryable:
                    raise
                failed += 1
            except Exception as exc:
                failed += 1
                self.emit('失败', f'[{index}/{len(episodes)}] {ep.title}: {exc}', 0)
            self.emit('总体', f'已处理 {index}/{len(episodes)}；成功/已存在 {successful}，失败 {failed}', index / len(episodes))
        return successful, failed

    def _one(self, ep, collection, quality, fallback, dest_dir, history, history_file):
        key = f'{ep.key}|{quality}|{int(fallback)}'
        old = history.get(key) or {}
        if isinstance(old.get('path'), str) and (ep.cid is None or old.get('cid') == ep.cid):
            path = Path(old['path'])
            if path.exists():
                try:
                    verify_output(path, ep.duration)
                    self.emit('跳过', f'已存在且验证通过：{path.name}', 1)
                    return False
                except BiliAlbumError:
                    pass

        last_err = None
        for attempt in range(1, 4):
            if self.cancel.is_set():
                raise Cancelled('任务已取消。')
            try:
                return self._attempt(ep, collection, quality, fallback, dest_dir, history, history_file, key)
            except Cancelled:
                raise
            except Exception as exc:
                last_err = exc
                if not isinstance(exc, NetworkFailure) or not exc.retryable:
                    raise
                if attempt >= 3:
                    break
                self.emit('重试', f'{ep.title} 第 {attempt} 次失败：{exc}；准备重试', 0)
                if self.cancel.wait(min(2 ** attempt, 8)):
                    raise Cancelled('任务已取消。')
        raise BiliAlbumError(f'连续 3 次失败：{last_err}')

    def _attempt(self, ep, collection, quality, fallback, dest_dir, history, history_file, key):
        self.emit('解析', f'检测画质：{ep.title}', 0.04)
        initial = collection.initial_info
        if (initial and initial.get('_bilialbum_embedded')
                and initial.get('id') == f'{ep.bvid}_p{ep.page}'
                and 0 <= time.monotonic() - initial.get('_bilialbum_received_at', 0) < 120):
            # Consume fresh page formats once. A later retry obtains new URLs.
            info = initial
            collection.initial_info = None
            validate_episode_info(info, ep)
        else:
            info = probe_episode(ep, direct=self.direct)
        choice = choose_format(info, quality, fallback)
        self.emit('画质', f'{ep.title}：实际下载 {choice.description}', 0.07)
        # A new cache namespace excludes unverified source files from older versions.
        tmp_id = hashlib.sha256(f'validated-v2|{key}|{ep.cid}|{choice.selector}'.encode()).hexdigest()[:18]
        tmp_dir = self.output / '.bilialbum-temp' / tmp_id
        tmp_dir.mkdir(parents=True, exist_ok=True)
        source = _completed_source(tmp_dir, ep, choice.selector)
        if source is None:
            YoutubeDL = _import_ydl()
            def match_filter(candidate, *, incomplete=False):
                if incomplete is not True:
                    validate_episode_info(candidate, ep)
                return None

            def hook(d):
                if self.cancel.is_set():
                    raise Cancelled('用户取消下载。')
                if d.get('status') == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    done = d.get('downloaded_bytes') or 0
                    frac = done / total if total else 0
                    speed = d.get('speed') or 0
                    self.emit('下载', f'{ep.title} · {frac:.0%} · {speed / 1024 / 1024:.2f} MiB/s', 0.08 + frac * .68)
                elif d.get('status') == 'finished':
                    self.emit('合并', f'{ep.title}：已下载媒体片段', .77)
            opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
                    'ignoreerrors': False, 'format': choice.selector,
                    'outtmpl': str(tmp_dir / 'source.%(ext)s'),
                    'merge_output_format': 'mkv', 'continuedl': True,
                    'overwrites': False, 'retries': 0, 'fragment_retries': 0,
                    'extractor_retries': 0, 'cachedir': False,
                    'logger': SilentLogger(),
                    'match_filter': match_filter,
                    'skip_unavailable_fragments': False, 'socket_timeout': 20,
                    'progress_hooks': [hook], 'getcomments': False,
                    'writesubtitles': False, 'writethumbnail': False}
            if self.direct:
                opts['proxy'] = ''
            trace = RequestTrace()
            try:
                with trace.ydl_class(YoutubeDL)(opts) as ydl:
                    if info.get('_bilialbum_embedded'):
                        # Process the verified public formats without asking the
                        # failing playurl API for the same data a second time.
                        prepared = {k: v for k, v in info.items() if not k.startswith('_bilialbum_')}
                        final_info = ydl.process_ie_result(prepared, download=True)
                    else:
                        final_info = ydl.extract_info(ep.url, download=True)
                    if not isinstance(final_info, dict):
                        raise BiliAlbumError('下载结果元数据异常。')
                    validate_episode_info(final_info, ep)
            except Cancelled:
                raise
            except NetworkFailure:
                raise
            except BiliAlbumError:
                # A rejected identity must never leave reusable completed media.
                _discard_source(tmp_dir)
                raise
            except Exception as exc:
                if self.cancel.is_set():
                    raise Cancelled('用户取消了媒体下载。') from exc
                if trace.records:
                    raise trace.failure(exc) from exc
                raise network_failure('媒体下载或合并', exc) from exc
            source = _source_file(tmp_dir)
            if source is None:
                _discard_source(tmp_dir)
                raise BiliAlbumError('媒体下载后找不到可读取的源文件。')
            (tmp_dir / 'source.complete.json').write_text(json.dumps({
                'key': ep.key, 'cid': ep.cid, 'selector': choice.selector,
                'file': source.name, 'size': source.stat().st_size}), encoding='utf-8')

        digits = max(3, len(str(len(collection.episodes))))
        name = (f'{ep.number:0{digits}d} - {safe_name(ep.title)}' if collection.is_collection
                else safe_name(ep.title))
        final = _unique_path(dest_dir / (name + '.mp4'))
        working = tmp_dir / 'output.processing.mp4'
        self.emit('转换', f'转换为 H.264/AAC：{ep.title}', .80)
        convert(source, working, self.cancel, lambda f: self.emit('转换', f'{ep.title} · 转换 {f:.0%}', .80 + f * .16))
        self.emit('验证', f'验证媒体文件：{ep.title}', .97)
        try:
            verify_output(working, ep.duration or info.get('duration'))
        except BiliAlbumError:
            # A decodable but truncated source must not poison all future attempts.
            _discard_source(tmp_dir)
            working.unlink(missing_ok=True)
            raise
        if self.cancel.is_set():
            raise Cancelled('用户取消了任务。')
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(working, final)
        history[key] = {'path': str(final.resolve()), 'bvid': ep.bvid, 'page': ep.page,
                        'title': ep.title, 'cid': ep.cid, 'published': ep.published,
                        'height': choice.height, 'quality_option': quality,
                        'source_url': ep.url, 'finished_at': datetime.now().astimezone().isoformat()}
        try:
            _save_history(history_file, history)
        except OSError as exc:
            self.emit('提醒', f'下载成功，但无法写入历史记录：{exc}', 1)
        try:
            shutil.rmtree(tmp_dir)
        except OSError:
            pass
        self.emit('完成', f'✓ {final.name}（{choice.height}P）', 1)
        return True
