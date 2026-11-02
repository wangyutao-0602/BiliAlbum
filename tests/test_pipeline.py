"""Offline end-to-end exercise: fake yt-dlp writes a local synthesized movie."""
from __future__ import annotations
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from bilialbum.models import Collection, Episode
from bilialbum.downloader import DownloadManager, probe_episode
from bilialbum.media import verify_output

INFO = {'id': 'BV0000000000_p3', 'duration': 1.0, 'formats': [
    {'format_id': 'v720', 'height': 720, 'vcodec': 'avc1.4d', 'acodec': 'none', 'tbr': 700},
    {'format_id': 'a', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 80},
]}


class FakeYDL:
    sample_file = None
    called = []

    def __init__(self, options):
        self.options = options
        self.__class__.called.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def extract_info(self, url, download):
        if download:
            dest = Path(self.options['outtmpl'].replace('%(ext)s', 'mkv'))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(FakeYDL.sample_file, dest)
            for hook in self.options.get('progress_hooks', []):
                hook({'status': 'downloading', 'downloaded_bytes': 100, 'total_bytes': 200})
                hook({'status': 'finished', 'filename': str(dest)})
        return dict(INFO)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class PipelineTests(unittest.TestCase):
    def test_fresh_embedded_formats_use_real_ytdlp_processing_without_reextraction(self):
        from yt_dlp import YoutubeDL
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / 'sample.mkv'
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=s=160x90:d=1:r=24', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=1', '-c:v', 'mpeg4',
                            '-c:a', 'pcm_s16le', '-shortest', str(sample)], check=True, capture_output=True)
            info = {'id': 'BV0000000000_p3', 'title': 'P3', 'duration': 1,
                    'extractor': 'BiliBili', 'extractor_key': 'BiliBili',
                    'formats': [{'format_id': 'embedded', 'url': 'https://media.invalid/test.mp4',
                                 'ext': 'mp4', 'height': 720, 'vcodec': 'h264', 'acodec': 'aac'}],
                    '_bilialbum_embedded': True, '_bilialbum_received_at': time.monotonic()}
            ep = Episode(1, 'P3', 'BV0000000000', 3, duration=1)
            col = Collection('test', 'single', [ep], 1, '', ep.url, initial_info=info)
            manager = DownloadManager(root / 'export')

            def write_media(_ydl, processed):
                self.assertEqual(processed['id'], ep.bvid + '_p3')
                temp_dir = next((root / 'export' / '.bilialbum-temp').iterdir())
                shutil.copy2(sample, temp_dir / 'source.mkv')

            with patch('bilialbum.downloader.probe_episode', side_effect=AssertionError('must reuse fresh page formats')), patch.object(YoutubeDL, 'extract_info', side_effect=AssertionError('must not reextract')), patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('must not make metadata requests')), patch.object(YoutubeDL, 'process_info', write_media):
                self.assertEqual(manager.run(col, [ep], '720', False), (1, 0))
            verify_output(root / 'export' / 'P3.mp4', 1)
            self.assertIsNone(col.initial_info)

    def test_truncated_source_is_removed_before_next_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            FakeYDL.sample_file = root / 'fake-source.mkv'
            FakeYDL.called = []
            ep = Episode(1, 'P3', 'BV0000000000', page=3, duration=70)
            col = Collection('test', 'single', [ep], 1, '', ep.url)
            manager = DownloadManager(root / 'export')

            def sample(duration):
                subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                                f'testsrc2=s=160x90:d={duration}:r=2', '-f', 'lavfi', '-i',
                                f'sine=frequency=440:duration={duration}', '-c:v', 'mpeg4',
                                '-c:a', 'pcm_s16le', '-shortest', str(FakeYDL.sample_file)],
                               check=True, capture_output=True)

            sample(1)
            with patch('bilialbum.downloader._import_ydl', return_value=FakeYDL), patch.dict(INFO, duration=70):
                self.assertEqual(manager.run(col, [ep], '720', True), (0, 1))
                self.assertEqual(list((root / 'export').rglob('source.complete.json')), [])
                self.assertEqual(list((root / 'export').rglob('*.mp4')), [])
                sample(70)
                self.assertEqual(manager.run(col, [ep], '720', True), (1, 0))
                downloads = [x for x in FakeYDL.called if 'outtmpl' in x]
                self.assertEqual(len(downloads), 2)
                verify_output(root / 'export' / 'P3.mp4', 70)

    def test_download_then_skip_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            FakeYDL.sample_file = root / 'fake-source.mkv'
            FakeYDL.called = []
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=s=320x180:d=1:r=24', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=1', '-c:v', 'mpeg4',
                            '-c:a', 'pcm_s16le', '-shortest', str(FakeYDL.sample_file)],
                           check=True, capture_output=True)
            ep = Episode(3, '第三讲', 'BV0000000000', page=3, duration=1)
            collection = Collection('课程合集', 'multip', [Episode(1, '一', ep.bvid),
                         Episode(2, '二', ep.bvid, 2), ep], 3, 'P 排序', 'https://...')
            logs = []
            manager = DownloadManager(root / 'export', lambda stage, msg, pct: logs.append((stage, msg, pct)))
            with patch('bilialbum.downloader._import_ydl', return_value=FakeYDL):
                self.assertEqual(manager.run(collection, [ep], '720', True), (1, 0))
                results = list((root / 'export' / '课程合集').glob('*.mp4'))
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].name.startswith('003 -'))
                verify_output(results[0], 1)
                initial_downloads = len([x for x in FakeYDL.called if 'outtmpl' in x])
                self.assertEqual(manager.run(collection, [ep], '720', True), (1, 0))
                later_downloads = len([x for x in FakeYDL.called if 'outtmpl' in x])
                self.assertEqual(initial_downloads, later_downloads)
            self.assertTrue(any(st == '跳过' for st, _, _ in logs))
            history = json.loads((root / 'export' / '.bilialbum-history.json').read_text())
            self.assertIn(f'{ep.key}|720|1', history)

    def test_identity_rejects_wrong_p_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong = dict(INFO, id='BV0000000000_p1')
            ep = Episode(1, 'P3', 'BV0000000000', 3)
            with patch('bilialbum.downloader._import_ydl', return_value=FakeYDL):
                with patch.object(FakeYDL, 'extract_info', return_value=wrong):
                    from bilialbum.models import BiliAlbumError
                    with self.assertRaisesRegex(BiliAlbumError, '分集校验失败'):
                        probe_episode(ep)
