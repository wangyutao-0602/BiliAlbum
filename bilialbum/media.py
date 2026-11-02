"""Stream-copy when possible, otherwise transcode to Photos-friendly AVC/AAC."""
from __future__ import annotations
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable
from .models import BiliAlbumError, Cancelled


def ensure_ffmpeg():
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise BiliAlbumError(f'找不到 {tool}。请先安装 FFmpeg，并重新启动程序。')


def probe(path: Path) -> dict:
    proc = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                           'format=duration,format_name:stream=index,codec_type,codec_name,pix_fmt,duration',
                           '-of', 'json', str(path)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise BiliAlbumError(f'媒体文件无法读取：{proc.stderr[-600:]}')
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise BiliAlbumError('ffprobe 返回异常 JSON。') from exc


def _streams(info: dict):
    streams = info.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    return video, audio


def verify_output(path: Path, expected_duration: float | None = None):
    if not path.is_file() or path.stat().st_size < 1024:
        raise BiliAlbumError('输出文件不存在或过小，验证失败。')
    info = probe(path)
    if 'mp4' not in (info.get('format') or {}).get('format_name', '') and 'mov' not in (info.get('format') or {}).get('format_name', ''):
        raise BiliAlbumError('输出文件并非 MP4 兼容容器。')
    video, audio = _streams(info)
    if not video or video.get('codec_name') != 'h264' or video.get('pix_fmt') != 'yuv420p':
        raise BiliAlbumError('视频不是 H.264 / yuv420p，无法认定为相册兼容格式。')
    if audio is not None and audio.get('codec_name') != 'aac':
        raise BiliAlbumError('音频编码不是 AAC。')
    duration = (info.get('format') or {}).get('duration')
    try:
        duration = float(duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise BiliAlbumError('输出视频时长无效。') from None
    if expected_duration and abs(duration - float(expected_duration)) > max(6.0, float(expected_duration) * 0.06):
        raise BiliAlbumError('输出文件与元数据时长差异过大，可能下载不完整。')
    # Decode at least one frame, not just inspect container metadata.
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-frames:v', '1',
                             '-f', 'null', '-'], capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise BiliAlbumError(f'视频解码校验失败：{result.stderr[-400:]}')
    return info


def convert(input_path: Path, output_path: Path, cancel, progress: Callable[[float], None]):
    ensure_ffmpeg()
    info = probe(input_path)
    video, audio = _streams(info)
    if not video:
        raise BiliAlbumError('原始媒体不含视频轨道。')
    # Genuine silent sources are valid; omit the audio map when absent.
    video_copy = video.get('codec_name') == 'h264' and video.get('pix_fmt') == 'yuv420p'
    audio_copy = audio is None or audio.get('codec_name') == 'aac'
    duration = float((info.get('format') or {}).get('duration') or 0)

    def run(copy_video, copy_audio):
        if output_path.exists():
            output_path.unlink()
        args = ['ffmpeg', '-y', '-hide_banner', '-nostdin', '-loglevel', 'error',
                '-i', str(input_path), '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn',
                '-c:v', 'copy' if copy_video else 'libx264']
        if not copy_video:
            args += ['-preset', 'medium', '-crf', '19', '-pix_fmt', 'yuv420p']
        args += ['-c:a', 'copy' if copy_audio else 'aac']
        if not copy_audio:
            args += ['-b:a', '192k']
        args += ['-movflags', '+faststart', '-f', 'mp4',
                 '-progress', 'pipe:1', '-nostats', str(output_path)]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace', bufsize=1)
        log_tail: list[str] = []
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if cancel.is_set():
                    proc.terminate()
                    raise Cancelled('用户取消了转码。')
                line = raw.strip()
                log_tail.append(line)
                if len(log_tail) > 18:
                    log_tail.pop(0)
                if line.startswith('out_time=') and duration:
                    value = line.split('=', 1)[1]
                    try:
                        h, m, s = value.split(':')
                        progress(min(0.99, (int(h) * 3600 + int(m) * 60 + float(s)) / duration))
                    except (ValueError, ZeroDivisionError):
                        pass
            ret = proc.wait(timeout=30)
            if ret != 0:
                raise BiliAlbumError('FFmpeg 处理失败：' + '\n'.join(log_tail[-8:]))
            progress(1.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            if proc.stdout:
                proc.stdout.close()

    try:
        try:
            run(video_copy, audio_copy)
        except BiliAlbumError:
            if cancel.is_set() or (not video_copy and not audio_copy):
                raise
            # Stream copy can fail for some unusual containers; re-encode to recover.
            run(False, False)
    except Exception:
        if output_path.exists():
            output_path.unlink()
        raise
