"""Framing 실행 진입점 — Framing.bat(Windows) · Framing.command(Mac) 가 이 파일을 실행한다. 본체는 framing 패키지.

  인자 없이        로컬 서버를 띄우고 브라우저로 화면을 연다
  작업 이름 · 옵션  터미널 도구로 바로 처리 (main.py extract shot.mov --at 1,50%,last)

앱에 묶어 둔 도구(app/ffmpeg · app/rife)가 있으면 그것을 쓰고, 없으면 PATH 의 ffmpeg 을 찾는다.
"""
import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

for name, folder in (("FRAMING_FFMPEG_HOME", "ffmpeg"), ("FRAMING_RIFE_HOME", "rife")):
    if (APP / folder).is_dir():
        os.environ.setdefault(name, str(APP / folder))
if (APP / "python").is_dir() and Path(sys.executable).parent == APP / "python":
    os.environ.setdefault("FRAMING_PORTABLE", "1")     # 내장 파이썬이면 pip 설치 단계를 건너뛴다
os.environ.setdefault("FRAMING_MODE", "browser")       # 그리딩처럼 브라우저로 (창이 필요하면 FRAMING_MODE=window)
os.environ.setdefault("FRAMING_DETACHED", "1")         # 콘솔을 떼지 않는다 — 서버 소식이 보이고 Ctrl+C 로 끝낼 수 있게

from framing import core  # noqa: E402

if os.environ.get("FRAMING_LAUNCHER"):
    core.SELF = Path(os.environ["FRAMING_LAUNCHER"])   # 도움말 이름 · 바탕화면 바로가기가 Framing.bat 을 가리키게

if __name__ == "__main__":
    sys.exit(core.main())
