@echo off
rem Framing - double-click to start. Everything it needs is inside the app folder.
rem Keep this file ASCII-only: cmd.exe misreads non-ASCII text in .bat files.
rem With arguments it runs as a command-line tool: Framing.bat extract shot.mov --at 1,50%%,last
setlocal
title Framing
if not exist "%~dp0app\python\python.exe" (
  echo.
  echo   Framing files are missing next to this file.
  echo   If you opened it from inside a zip, extract the whole zip first
  echo   ^(right-click - Extract All^), then run Framing.bat in the extracted folder.
  echo.
  pause
  exit /b 1
)
set "FRAMING_LAUNCHER=%~f0"
"%~dp0app\python\python.exe" -u -X utf8 "%~dp0app\main.py" %*
set "FRAMING_EXIT=%errorlevel%"
if not "%FRAMING_EXIT%"=="0" pause
exit /b %FRAMING_EXIT%
