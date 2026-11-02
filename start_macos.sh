#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo '尚未安装依赖。请先运行 ./install_macos.sh'
  exit 1
fi
exec .venv/bin/python main.py "$@"
