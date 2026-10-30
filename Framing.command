#!/bin/bash
# Framing for Mac, run from source - double-click in Finder (or: ./Framing.command extract shot.mov --at 1,50%,last).
# First run makes app/.venv-mac and installs numpy, OpenCV and Pillow. FFmpeg comes from Homebrew.
# For a real Mac app (Framing.app) use app/mac/Mac에서_빌드.command instead.
set -euo pipefail
cd "$(dirname "$0")"
if [ "$(uname -s)" != "Darwin" ]; then echo "Run this on a Mac."; exit 1; fi
for brewpath in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [ -x "$brewpath" ]; then eval "$("$brewpath" shellenv)"; break; fi
done
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "Framing needs FFmpeg. Install Homebrew (https://brew.sh), then run: brew install ffmpeg"
  read -r -p "Press Enter to close"; exit 1
fi
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Framing needs Python 3.10 or newer. Install it with: brew install python@3.12"
  read -r -p "Press Enter to close"; exit 1
fi
VENV=app/.venv-mac
if [ ! -x "$VENV/bin/python" ]; then
  echo "First run: setting up Framing (about a minute)..."
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r app/requirements.txt
fi
export FRAMING_LAUNCHER="$PWD/Framing.command" FRAMING_PORTABLE=1
exec "$VENV/bin/python" -u -X utf8 app/main.py "$@"
