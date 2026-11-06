"""실행 환경 — 판 정보 · 실행 파일 자리 · 묶어 둔 도구 자리 · 다시 띄우기 · 자동 설치 · cv2/numpy"""
from __future__ import annotations

import importlib
import os
import site
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional, Sequence


BUILD = "1.0"          # 어느 판을 쓰고 있는지 화면에서 보이게
SELF = Path(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


class Relauncher:
    """이 파일을 다시 띄우는 방법들. 첫 줄이 배치라서 언제나 -x 가 필요하다."""

    DETACHED = "FRAMING_DETACHED"

    @staticmethod
    def restart() -> None:
        """설치한 것을 반영해 처음부터 다시."""
        os.execv(sys.executable, [sys.executable, "-x", str(SELF), *sys.argv[1:]])

    @classmethod
    def detach_console(cls) -> bool:
        """
        윈도우에서 검은 콘솔을 떼어낸다. 떼어냈으면 True — 부른 쪽은 그냥 끝내면 된다.
        준비가 다 끝난 뒤에 부르는 것이 중요하다. 설치 과정은 콘솔에 보여야 하니까.
        """
        if not sys.platform.startswith("win"):
            return False
        if os.environ.get(cls.DETACHED) == "1":
            return False
        current = Path(sys.executable)
        if current.name.lower().startswith("pythonw"):
            return False
        quiet = current.with_name("pythonw.exe")
        if not quiet.is_file():
            return False
        try:
            subprocess.Popen(
                [str(quiet), "-x", str(SELF), *sys.argv[1:]],
                env=dict(os.environ, **{cls.DETACHED: "1"}),
                close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), stdin=subprocess.DEVNULL)
            return True
        except Exception:
            return False


# ══════════════════════════════════════════════════ 1~4. 엔진

MIN_PYTHON = (3, 8)
OPENCV_PACKAGES = ("opencv-python-headless", "opencv-python")
BOOTSTRAP_FLAG = "FRAMING_BOOTSTRAPPED"

HERE = Path(__file__).resolve().parent
# 앱에 묶어 둔 도구 자리. 실행 껍데기(main.py)가 환경변수로 알려 주면 그쪽을 본다
FFMPEG_HOME = Path(os.environ.get("FRAMING_FFMPEG_HOME") or HERE / "_ffmpeg")
RIFE_HOME = Path(os.environ.get("FRAMING_RIFE_HOME") or HERE / "_rife")

VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2v", ".ts", ".m2ts", ".mts", ".mxf", ".ogv", ".3gp",
})
IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".dpx", ".bmp", ".tga",
    ".webp",
})

BAR = "─" * 62


# ══════════════════════════════════════════════════ 자동 설치

class DependencyBootstrap:
    """필요한 파이썬 패키지가 없으면 pip 로 직접 설치하고 임포트까지 마친다."""

    def __init__(self, module_name: str, packages: Sequence[str],
                 announce: bool = True) -> None:
        self._module_name = module_name
        self._packages = tuple(packages)
        self._announce = announce

    def ensure(self) -> ModuleType:
        module = self._import()
        if module is not None:
            return module

        if getattr(sys, "frozen", False):
            raise SystemExit(f"앱 구성요소 {self._module_name}를 읽지 못했습니다. ZIP을 다시 풀어 주세요.")

        if self._announce:
            print(f"[준비] '{self._packages[0]}' 가 없어 자동으로 설치합니다. 최초 1회만 걸립니다.")
        self._ensure_pip()

        for package in self._packages:
            if not self._install(package):
                continue
            self._refresh_search_path()
            module = self._import()
            if module is not None:
                if self._announce:
                    print("[준비] 설치 완료.\n")
                return module
            return self._restart_once()

        raise SystemExit(self._failure_message())

    def try_ensure(self) -> bool:
        """실패해도 프로그램을 끝내지 않는다. 대비책이 있는 쪽에서 쓴다."""
        try:
            self.ensure()
            return True
        except SystemExit:
            return False

    def _import(self) -> Optional[ModuleType]:
        importlib.invalidate_caches()
        try:
            return importlib.import_module(self._module_name)
        except ImportError:
            return None

    @staticmethod
    def _ensure_pip() -> None:
        try:
            importlib.import_module("pip")
        except ImportError:
            try:
                import ensurepip
                ensurepip.bootstrap()
            except Exception:
                pass

    @staticmethod
    def _install(package: str) -> bool:
        # Respect externally managed environments; never force past their safeguards.
        variants = ([], ["--user"])
        for extra in variants:
            print(f"       pip install {package} {' '.join(extra)}".rstrip())
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "--disable-pip-version-check", package, *extra], stdin=subprocess.DEVNULL)
            except OSError:
                return False
            if completed.returncode == 0:
                return True
        return False

    @staticmethod
    def _refresh_search_path() -> None:
        try:
            user_site = site.getusersitepackages()
        except Exception:
            return
        if isinstance(user_site, str) and user_site not in sys.path:
            sys.path.append(user_site)

    @staticmethod
    def relaunch_command() -> list[str]:
        """
        나를 다시 여는 명령.

        한 파일짜리 실행본은 첫 줄이 파이썬이 아니다.
        .bat 은 배치 명령이라 python -x 로 건너뛰어야 하고,
        .command 는 첫 줄이 #!/bin/sh 주석이라 그냥 열어도 된다.
        이걸 구분하지 않으면 설치 직후 다시 시작하는 순간 죽는다.
        """
        if getattr(sys, "frozen", False):
            return [sys.executable]
        me = Path(__file__).resolve()
        skip = ["-x"] if me.suffix.lower() == ".bat" else []
        return [sys.executable, *skip, str(me)]

    def _restart_once(self) -> ModuleType:
        if os.environ.get(BOOTSTRAP_FLAG) != "1":
            os.environ[BOOTSTRAP_FLAG] = "1"
            print("[준비] 설치 반영을 위해 다시 시작합니다.\n")
            command = self.relaunch_command()
            os.execv(command[0], [*command, *sys.argv[1:]])
        raise SystemExit(self._failure_message())

    def _failure_message(self) -> str:
        return (
            f"\n'{self._packages[0]}' 설치에 실패했습니다.\n"
            "인터넷 연결을 확인하고 아래 명령을 직접 실행해 주세요.\n"
            f"  {sys.executable} -m pip install {self._packages[0]}\n"
        )


def require_python() -> None:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"파이썬 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상이 필요합니다. "
            f"현재 {sys.version.split()[0]}\n"
            "https://www.python.org/downloads/ 에서 설치해 주세요.\n")


require_python()

os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

cv2 = DependencyBootstrap("cv2", OPENCV_PACKAGES).ensure()
np = DependencyBootstrap("numpy", ("numpy",)).ensure()


# ══════════════════════════════════════════════════ 공용 유틸

