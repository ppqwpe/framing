#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
trap 'echo; echo "실패했습니다. 위 오류를 확인하세요."; read -r -p "Enter로 닫기"' ERR
if [ "$(uname -s)" != "Darwin" ]; then echo "Mac에서 실행하세요."; exit 1; fi
if ! command -v brew >/dev/null 2>&1; then
  for brewpath in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$brewpath" ]; then eval "$("$brewpath" shellenv)"; break; fi
  done
fi
if ! command -v brew >/dev/null 2>&1; then echo "빌드용 Mac에 Homebrew가 필요합니다: https://brew.sh"; exit 1; fi
PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
if [ ! -x "$PYTHON" ] || ! command -v ffmpeg >/dev/null 2>&1; then
  echo "먼저 터미널에서 brew install python@3.12 ffmpeg 를 실행하세요."; exit 1
fi
"$PYTHON" -m venv .venv-mac
.venv-mac/bin/python -m pip install -r requirements-mac.txt
.venv-mac/bin/python build_mac.py "$@"
open dist
echo "빌드가 끝났습니다. Enter로 닫으세요."
read -r
