@echo off
rem Framing in its own window. Framing.bat opens the same UI in the browser.
rem Release zips carry app\window\Framing.exe; a clone without it uses Electron from app\electron.
if not exist "%~dp0app\python\python.exe" (
  echo app\python is missing. Copy app\python, app\lib, app\ffmpeg and app\rife from the release zip.
  pause
  exit /b 1
)
if exist "%~dp0app\window\Framing.exe" (
  start "" "%~dp0app\window\Framing.exe"
  exit /b 0
)
set "FRAMING_ELECTRON=%~dp0app\electron\node_modules\electron\dist\electron.exe"
if not exist "%FRAMING_ELECTRON%" (
  echo The Framing window is not built. Run "pnpm install" and "pnpm run build" in app\electron.
  pause
  exit /b 1
)
start "" "%FRAMING_ELECTRON%" "%~dp0app\electron"
