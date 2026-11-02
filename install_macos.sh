#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo '未检测到 python3。请先安装 Python 3.11+（建议 python.org 官方安装包）。'
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit('需要 Python 3.11 或更新版本')
try:
    import tkinter
except ImportError:
    sys.exit('Python 缺少 tkinter。请安装带 Tcl/Tk 的 Python 或 Homebrew 对应 python-tk。')
PY
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo '请先安装 FFmpeg：brew install ffmpeg'
  exit 1
fi
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
printf '\n安装成功！运行 ./start_macos.sh 或双击 启动BiliAlbum.command\n'
