"""화면 문서 · 바로가기 · 시작 (브라우저 또는 Electron 창)"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from . import env
from .env import OPENCV_PACKAGES, Relauncher
from .util import Console
from .media import FFmpegTool
from .job import app_icon_bytes
from .cli import CommandLine
from .api import Api
from .server import LocalServer
from .checkup import CrashNote, FfmpegStep, PipStep, SetupWizard


UI_HTML = Path(__file__).with_name('ui.html').read_text(encoding='utf-8')


# ══════════════════════════════════════════════════ 5-3. 띄우기

class UiDocument:
    """화면 HTML 한 장. 브라우저든 Electron 창이든 로컬 서버가 이걸 내준다."""

    def __init__(self, html: str) -> None:
        self._html = html

    def as_bytes(self) -> bytes:
        return self._html.encode("utf-8")


class DesktopShortcut:
    """
    바탕화면 바로가기를 만든다. 아이콘은 고양이.

    **.bat 파일 자체는 아이콘을 못 바꿉니다.** 윈도우가 .bat · .cmd 에는
    정해진 시스템 아이콘을 씁니다 — 파일 안에 무엇을 넣어도 바뀌지 않습니다.
    파일에 제 아이콘을 다는 건 .exe 와 바로가기(.lnk) 뿐입니다.

    그래서 바로가기를 만듭니다. 그 바로가기에는 아이콘을 달 수 있고, 작업
    표시줄에 고정하면 거기에도 같은 아이콘이 붙습니다.

        Framing.bat shortcut

    맥은 사정이 다릅니다 — .command 는 파인더에서 아이콘을 바꿀 수 있지만
    (Finder 정보 창에 붙여넣기) 명령으로 하려면 별도 도구가 필요해서, 여기서는
    아이콘 파일만 꺼내 두고 자리를 알려 줍니다.
    """

    NAME = "Framing"

    def make(self) -> int:
        icon = self._icon()
        if icon is None:
            Console.warn("아이콘을 꺼내지 못했습니다.")
            return 1
        if sys.platform != "win32":
            Console.info(f"아이콘  {icon}")
            Console.info("맥에서는 파인더에서 Framing.command 정보 창을 열고 "
                         "왼쪽 위 아이콘에 이 파일을 붙여넣으면 바뀝니다.")
            return 0
        return self._windows(icon)

    def _icon(self) -> Optional[Path]:
        """아이콘을 곁에 꺼내 둔다. 바로가기는 이 파일을 계속 가리킨다."""
        blob = app_icon_bytes()
        if blob is None:
            return None
        try:
            home = Path.home() / ".framing"
            home.mkdir(parents=True, exist_ok=True)
            spot = home / "framing.ico"
            spot.write_bytes(blob)
            return spot
        except Exception as exc:
            Console.warn(f"아이콘을 쓰지 못했습니다: {exc}")
            return None

    def _windows(self, icon: Path) -> int:
        launcher = Path(env.SELF).resolve()
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            desktop = Path.home()
        link = desktop / f"{self.NAME}.lnk"
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
            "$s.TargetPath = '{target}';"
            "$s.WorkingDirectory = '{folder}';"
            "$s.IconLocation = '{icon}';"
            "$s.Description = 'Framing';"
            "$s.Save()"
        ).format(**{key: str(value).replace("'", "''") for key, value in
                    dict(link=link, target=launcher, folder=launcher.parent, icon=icon).items()})
        try:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, stdin=subprocess.DEVNULL)
        except OSError as exc:
            Console.warn(f"powershell 을 부르지 못했습니다: {exc}")
            return 1
        if done.returncode != 0 or not link.exists():
            Console.warn(f"바로가기를 만들지 못했습니다: {(done.stderr or '').strip()[:200]}")
            return 1
        Console.info(f"바로가기  {link}")
        Console.info("이걸 더블클릭해서 여세요. 작업표시줄에 고정하면 거기에도 "
                     "같은 아이콘이 붙습니다.")
        return 0


class StartupRequest:
    """실행할 때 받은 인자를 읽어, 터미널로 갈지 창으로 갈지 정한다."""

    HELP_WORDS = ("explain", "help", "?")

    def __init__(self, args: Sequence[str]) -> None:
        self._args = [str(a) for a in args]

    @property
    def args(self) -> list[str]:
        return list(self._args)

    @property
    def paths(self) -> list[str]:
        """전부 실제로 있는 경로일 때만 목록으로 본다 (아이콘 위에 끌어다 놓은 경우)."""
        if not self._args:
            return []
        found = [a for a in self._args if Path(a).expanduser().exists()]
        return found if len(found) == len(self._args) else []

    @property
    def wants_cli(self) -> bool:
        if not self._args:
            return False
        first = self._args[0].strip().lower()
        if first in self.HELP_WORDS or CommandLine.job_for(first) is not None:
            return True
        if any(a.startswith("-") for a in self._args):
            return True
        return not self.paths          # 경로도 아니면 터미널이 이유를 알려준다


class FramingApp:
    """엔진 서버를 세우고 화면을 연다 — 기본은 브라우저, Electron 창이 부르면 주소만 알려 준다."""

    def __init__(self, carried: Sequence[str] = ()) -> None:
        self._api = Api()
        self._api.carry(carried)
        self._ui = UiDocument(UI_HTML)

    def launch(self) -> int:
        """
        서버는 화면만 내주는 것이 아니다 — 엔진이 뱉는 소식을 담아 두는 큐이자,
        대조 화면이 영상을 읽어 가는 길이다. 브라우저든 Electron 창이든 같은 주소를 본다.
        """
        server = LocalServer(self._api, self._ui)
        url = server.start()
        mode = (os.environ.get("FRAMING_MODE") or "").lower()
        try:
            if mode == "electron":
                return self._serve_electron(url, server)
            server.open_browser()
            server.wait()
            return 0
        finally:
            server.stop()

    def _serve_electron(self, url: str, server: LocalServer) -> int:
        """
        Electron 창(app/electron)이 이 프로세스를 띄운다. 주소는 한 줄로 알려 주고,
        창 쪽이 stdin 을 닫으면(창을 닫았거나 Electron 이 죽었으면) 같이 끝난다.
        """
        pipe = self._take_stdin()

        def watch() -> None:
            try:
                while pipe.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            self._api.quit()

        threading.Thread(target=watch, daemon=True).start()
        print(f"FRAMING_URL={url}", flush=True)
        server.wait()
        return 0

    @staticmethod
    def _take_stdin():
        """
        Electron 이 물린 stdin 파이프를 감시 스레드만 쓰는 사본으로 옮기고, 표준 입력은 NUL 로 바꾼다.

        그대로 두면 엔진이 띄우는 ffmpeg · ffprobe · RIFE 가 모두 그 파이프를 물려받는다. 감시 스레드가
        그 파이프를 읽고 있는 동안 ffmpeg 이 마무리 단계에서 멈췄다 (11/06: MOV 목차를 못 써서 누크에서
        1프레임 ERROR). 사본(os.dup)은 자식에게 넘어가지 않고, 자식은 NUL 을 받는다.
        """
        pipe = os.fdopen(os.dup(0), "rb", buffering=0)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        sys.stdin = open(os.devnull, "r")
        if sys.platform == "win32":
            try:
                import ctypes
                import msvcrt
                ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))   # STD_INPUT_HANDLE
            except Exception:
                pass
        return pipe


def main(argv: Optional[Sequence[str]] = None) -> int:
    """넘어지면 조용히 사라지지 않는다 — 무슨 일이었는지 쪽지를 남긴다."""
    # Redirected Windows terminals may default to CP949. Help and diagnostics
    # contain Unicode symbols and filenames; they must not crash while printing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                pass
    try:
        return Startup.run(argv)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        spot = CrashNote.write(exc)
        print("\n[!] Framing 을 시작하지 못했습니다.")
        print(f"    {type(exc).__name__}: {exc}")
        if spot is not None:
            print(f"\n    무슨 일이 있었는지 여기 적어 두었습니다:\n    {spot}")
            print("    이 파일을 보내 주시면 원인을 찾을 수 있습니다.")
        return 1


class Startup:
    """
    켜질 때 어디로 갈지 고른다 — 터미널이냐 창이냐.

    작업 이름이나 옵션이 붙어 있으면 터미널 도구로, 아이콘을 눌러 들어왔으면
    창으로 간다. 앱으로 구운 판은 챙길 것이 이미 안에 있어서 준비 단계를
    건너뛴다 — 안 그러면 창이 두 번 뜬다.
    """

    @staticmethod
    def wizard() -> SetupWizard:
        """처음 쓰는 사람이 챙겨야 할 것들. 순서대로."""
        return SetupWizard([
            PipStep("영상 처리 opencv", "cv2", OPENCV_PACKAGES, "약 60MB"),
            FfmpegStep(FFmpegTool(allow_download=True)),
        ])

    @classmethod
    def run(cls, argv: Optional[Sequence[str]] = None) -> int:
        request = StartupRequest(sys.argv[1:] if argv is None else argv)
        if request.wants_cli:
            return CommandLine(request.args).run()         # 작업 이름이나 옵션이 있으면 터미널 도구로

        # 앱으로 구운 판(.exe · .app)과 포터블 판(Framing.bat · 내장 파이썬)은 필요한 것을 이미 안고 있다.
        # 챙길 것도, 콘솔을 떼려고 자기를 다시 띄울 것도 없다 — 하면 오히려 두 번 뜬다.
        if not getattr(sys, "frozen", False) and not os.environ.get("FRAMING_PORTABLE"):
            cls.wizard().ensure()               # 처음이면 여기서 다 챙긴다 (콘솔에 보이게)
            if Relauncher.detach_console():
                return 0                         # 콘솔을 떼고 새로 띄웠으니 여기서 끝
        return FramingApp(request.paths).launch()   # 아이콘 위에 놓은 파일을 들고 창으로


