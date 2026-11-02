import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from bilialbum.media import convert, verify_output, probe
from bilialbum.models import BiliAlbumError


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / 'src.mkv'
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=24:d=1',
               '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'mpeg4', '-q:v', '8',
               '-c:a', 'pcm_s16le', '-shortest', str(self.input)]
        subprocess.run(cmd, check=True, capture_output=True)

    def test_transcode_h264_aac(self):
        dest = self.root / 'out.mp4'
        values = []
        convert(self.input, dest, threading.Event(), values.append)
        self.assertGreater(len(values), 0)
        verify_output(dest, 1)
        info = probe(dest)
        self.assertIn('h264', [s['codec_name'] for s in info['streams']])
        self.assertIn('aac', [s['codec_name'] for s in info['streams']])

    def test_duration_rejects_truncation(self):
        dest = self.root / 'out.mp4'
        convert(self.input, dest, threading.Event(), lambda f: None)
        with self.assertRaises(BiliAlbumError):
            verify_output(dest, 70)

    def test_malformed_duration_is_a_media_error(self):
        data = {'format': {'format_name': 'mp4', 'duration': 'N/A'},
                'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'pix_fmt': 'yuv420p'}]}
        with patch('bilialbum.media.probe', return_value=data):
            with self.assertRaisesRegex(BiliAlbumError, '时长无效'):
                verify_output(self.input)


if __name__ == '__main__':
    unittest.main()

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class SilentMediaTests(unittest.TestCase):
    def test_silent_video_is_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / 'video.mkv'
            out = Path(root) / 'out.mp4'
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=s=160x90:d=1:r=24', '-c:v', 'mpeg4', str(src)],
                           check=True, capture_output=True)
            convert(src, out, threading.Event(), lambda f: None)
            verify_output(out, 1)
