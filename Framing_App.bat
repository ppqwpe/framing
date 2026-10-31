@echo off
rem Framing in its own window (Electron). Framing.bat opens the same UI in the browser.
set "FRAMING_ELECTRON=%~dp0app\electron\node_modules\electron\dist\electron.exe"
if not exist "%~dp0app\python\python.exe" (
  echo app\python is missing. Copy app\python, app\lib, app\ffmpeg and app\rife from the release zip.
  pause
  exit /b 1
)
if not exist "%FRAMING_ELECTRON%" (
  echo Electron is missing. Run "pnpm install" in app\electron.
  pause
  exit /b 1
)
start "" "%FRAMING_ELECTRON%" "%~dp0app\electron"
