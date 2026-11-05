#!/bin/bash
# Framing for Mac. Double-click in Finder: opens the Framing window (app/window/Framing.app),
# or the same UI in the browser when the window is not there. With arguments it is the command-line tool:
#   ./Framing.command extract shot.mov --at 1,50%,last
# First run makes app/.venv-mac (numpy, OpenCV, Pillow), and clears the download quarantine and signs
# Framing.app for this Mac (it has no Apple developer signature). FFmpeg comes from Homebrew.
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
WINDOW=app/window/Framing.app
if [ $# -eq 0 ] && [ -d "$WINDOW" ]; then
  if [ ! -f "$VENV/.window-signed" ]; then
    echo "First run: preparing the Framing window for this Mac..."
    xattr -cr . 2>/dev/null || true
    codesign --force --deep --sign - "$WINDOW"
    touch "$VENV/.window-signed"
  fi
  open "$WINDOW"
  exit 0
fi
export FRAMING_LAUNCHER="$PWD/Framing.command" FRAMING_PORTABLE=1
exec "$VENV/bin/python" -u -X utf8 app/main.py "$@"
