"""점검 · 오류 쪽지 · 처음 쓰는 사람용 설치 마법사"""
from __future__ import annotations

import datetime
import importlib
import subprocess
import sys
import traceback
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Sequence
from typing import TYPE_CHECKING

from .env import DependencyBootstrap
from .util import Scratch

if TYPE_CHECKING:   # 타입 표기에만 쓰는 이름
    from .media import FFmpegTool
    from .api import Api


class Checkup:
    """
    지금 무엇이 어떻게 돌고 있는지 한 장으로 모은다.

    "오류가 나요" 만 가지고는 아무것도 알 수 없다. 어느 판인지, 파이썬은 뭔지,
    ffmpeg 은 있는지, 창으로 열렸는지 브라우저로 열렸는지 — 물어보면 서로 피곤하다.
    화면에서 한 번 누르면 이게 모여 나오게 한다.

    파이썬이 아는 것만 모은다. 화면이 아는 것(무슨 파일을 넣었는지, 기록,
    자바스크립트 오류)은 화면 쪽 Doctor 가 붙여서 한 장으로 만든다.
    """

    def __init__(self, api: "Api") -> None:
        self._api = api

    def facts(self) -> dict:
        return {
            '때': f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
            '판': self._machine(),
            '파이썬': f"{sys.version.split()[0]}  ({sys.executable})",
            '열린 방식': self._how(),
            'ffmpeg': self._ffmpeg(),
            '설정 폴더': str(Path.home() / '.framing'),
            '임시 자리': f"{Scratch.holding()}개",
            '못 보낸 것': self._api.trouble or '없음',
        }

    @staticmethod
    def _machine() -> str:
        try:
            import platform
            return f"{platform.platform()}  ({platform.machine()})"
        except Exception:
            return sys.platform

    def _how(self) -> str:
        where = '창' if self._api._window is not None else '브라우저'
        return f"{where}  ·  화면은 {'서버' if self._api._served else '파일'} 에서"

    def _ffmpeg(self) -> str:
        spot = self._api._ff.find('ffmpeg') or ''
        if not spot:
            return '없음 (처음 쓸 때 받아 옵니다)'
        try:
            first = subprocess.run([spot, '-version'], capture_output=True,
                                   text=True, timeout=10).stdout.splitlines()
            return f"{spot}\n              {first[0] if first else ''}"
        except Exception:
            return spot


class CrashNote:
    """
    시작하다 넘어졌을 때 남기는 쪽지.

    창 모드에는 열어 볼 콘솔이 없고, 윈도우에서 배치로 열면 넘어지는 순간
    검은 창이 그대로 닫힌다. 쓰는 사람 눈에는 "눌렀는데 아무 일도 안 일어남"
    으로만 보인다 — 물어봐도 답할 말이 없다.

    그래서 무슨 일이 있었는지 파일 하나로 남긴다. 그 파일만 보내 주면 된다.
    """

    SPOT = Path.home() / '.framing' / '오류.txt'

    @classmethod
    def write(cls, exc: BaseException) -> Optional[Path]:
        try:
            cls.SPOT.parent.mkdir(parents=True, exist_ok=True)
            cls.SPOT.write_text(
                "Framing 이 시작하다 멈췄습니다.\n"
                "이 파일을 그대로 보내 주시면 원인을 찾을 수 있습니다.\n\n"
                f"때  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"판  {sys.platform}  ·  python {sys.version.split()[0]}\n"
                f"곳  {sys.argv[0]}\n\n"
                + "".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)),
                encoding='utf-8')
            return cls.SPOT
        except Exception:
            return None

# ══════════════════════════════════════════════════ 창


# ══════════════════════════════════════════════════ 첫 실행 준비

class SetupStep(ABC):
    """처음 쓸 때 챙겨야 하는 것 하나. 각자 자기가 준비됐는지, 어떻게 챙기는지 안다."""

    def __init__(self, title: str, size_hint: str = "") -> None:
        self.title = title
        self.size_hint = size_hint

    @property
    def label(self) -> str:
        return f"{self.title} ({self.size_hint})" if self.size_hint else self.title

    @abstractmethod
    def is_ready(self) -> bool: ...

    @abstractmethod
    def install(self) -> bool: ...

    @property
    def optional(self) -> bool:
        """없어도 프로그램은 뜬다. 그 기능만 못 쓴다."""
        return False


class PipStep(SetupStep):
    """pip 로 챙기는 파이썬 꾸러미."""

    def __init__(self, title: str, module: str, packages: Sequence[str],
                 size_hint: str = "", optional: bool = False) -> None:
        super().__init__(title, size_hint)
        self._module = module
        self._packages = tuple(packages)
        self._optional = optional

    @property
    def optional(self) -> bool:
        return self._optional

    def is_ready(self) -> bool:
        try:
            importlib.invalidate_caches()
            importlib.import_module(self._module)
            return True
        except ImportError:
            return False

    def install(self) -> bool:
        # 마법사가 이미 무엇을 챙기는지 말했으니 안내는 겹치지 않게 끈다.
        return DependencyBootstrap(self._module, self._packages, announce=False).try_ensure()


class FfmpegStep(SetupStep):
    """영상 인코더. 없으면 찾아보고, 없으면 내려받는다."""

    HINTS = {"win32": "약 80MB, 알아서 내려받습니다",
             "darwin": "Homebrew 로 설치합니다"}

    def __init__(self, tool: "FFmpegTool") -> None:
        # 어떻게 챙기는지가 운영체제마다 다르다. 무엇을 기다려야 하는지 미리 말해 준다.
        super().__init__("인코더 ffmpeg",
                         self.HINTS.get(sys.platform, "패키지 관리자로 설치합니다"))
        self._tool = tool

    def is_ready(self) -> bool:
        return bool(self._tool.find("ffmpeg") and self._tool.find("ffprobe"))

    def install(self) -> bool:
        try:
            self._tool.ensure()
            return self.is_ready()
        except Exception:
            return False

    @property
    def optional(self) -> bool:
        return True          # 없어도 프레임 뽑기는 된다


class SetupWizard:
    """
    처음 쓰는 사람이 아무것도 안 해도 되게, 필요한 것을 순서대로 챙긴다.
    무엇을 하고 있는지 계속 보여준다 — 멈춘 것처럼 보이는 게 제일 나쁘다.
    """

    MARKER = Path.home() / ".framing" / "ready.txt"
    WIDTH = 60

    def __init__(self, steps: Sequence[SetupStep]) -> None:
        self._steps = list(steps)

    # --- public ----------------------------------------------------------

    def ensure(self) -> None:
        first_time = not self.MARKER.exists()
        missing = [s for s in self._steps if not s.is_ready()]
        if not missing:
            self._remember()
            return

        # 한 번 겪어 본 사람에게는 조용히. 꼭 필요한 게 빠졌을 때만 다시 나선다.
        if not first_time:
            missing = [s for s in missing if not s.optional]
            if not missing:
                return

        self._banner(len(missing))
        failed = []
        for order, step in enumerate(missing, 1):
            self._line(order, len(missing), step)
            if step.install():
                self._done("완료")
            elif step.optional:
                self._done("건너뜀 — 나중에 필요할 때 다시 시도합니다")
            else:
                self._done("실패")
                failed.append(step)

        if failed:
            self._trouble(failed)
        else:
            print("\n  준비 끝. 창을 띄웁니다.\n")
            self._remember()

    # --- internal --------------------------------------------------------

    def _banner(self, count: int) -> None:
        bar = "─" * self.WIDTH
        print(f"\n{bar}\n  Framing — 처음 실행 준비\n{bar}")
        print(f"  필요한 것 {count}가지를 알아서 챙깁니다. 처음 한 번만 걸립니다.")
        print("  인터넷 사정에 따라 몇 분 걸릴 수 있습니다. 그냥 두세요.\n")

    @staticmethod
    def _line(order: int, total: int, step: SetupStep) -> None:
        print(f"  [{order}/{total}] {step.label}")
        print("        챙기는 중...", flush=True)

    @staticmethod
    def _done(text: str) -> None:
        print(f"        → {text}\n", flush=True)

    def _trouble(self, failed: Sequence[SetupStep]) -> None:
        print("\n  다음을 챙기지 못했습니다:")
        for step in failed:
            print(f"    · {step.title}")
        print("\n  인터넷 연결이나 회사망 차단을 확인해 주세요.")
        print(f"  직접 챙기려면:  {sys.executable} -m pip install opencv-python-headless pywebview")
        if sys.platform == "darwin":
            print("  맥에서 권한 때문에 막히면 앞에 --user 를 붙여 보세요.")
        print()
        try:
            input("  엔터를 누르면 그래도 실행해 봅니다...")
        except EOFError:
            pass

    def _remember(self) -> None:
        try:
            self.MARKER.parent.mkdir(parents=True, exist_ok=True)
            self.MARKER.write_text("ok\n", encoding="utf-8")
        except OSError:
            pass                       # 기록 못 해도 상관없다. 다음에 다시 확인할 뿐


# ══════════════════════════════════════════════════ 6. 화면


# 화면 HTML 은 같은 폴더의 ui.html (CSS · JS 포함 한 파일)
