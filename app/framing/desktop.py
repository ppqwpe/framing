"""화면 문서 · 창 · 바로가기 · 창 아이콘 · 시작"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

from . import env
from .env import DependencyBootstrap, OPENCV_PACKAGES, Relauncher
from .util import Console
from .media import FFmpegTool
from .job import app_icon_bytes
from .cli import CommandLine
from .app import DropTarget
from .api import Api
from .server import LocalServer
from .checkup import CrashNote, FfmpegStep, PipStep, SetupWizard


UI_HTML = Path(__file__).with_name('ui.html').read_text(encoding='utf-8')


# ══════════════════════════════════════════════════ 5-3. 띄우기

class UiDocument:
    """화면 HTML 한 장. 창이든 브라우저든 서버가 이걸 내준다."""

    def __init__(self, html: str) -> None:
        self._html = html

    def as_bytes(self) -> bytes:
        return self._html.encode("utf-8")


class DesktopWindow:
    """
    진짜 창. pywebview 가 있어야 한다.

    화면은 **로컬 서버 주소**를 물린다. 예전에는 임시 파일의 file:// 주소를
    물렸는데, 그러면 두 가지가 깨진다.

      · 대조 화면은 iframe 안에 산다. 출처가 없는 문서에서 file:// 을 읽는 것을
        브라우저 엔진이 막아서, 창은 떠도 영상 자리가 까맣게 남는다.
      · 파이썬이 화면으로 소식을 보낼 길이 없다. 예전에는 창에 자바스크립트를
        직접 밀어넣었는데 그건 맥에서 앱을 죽인다. 지금은 화면이 서버에서
        가져가는 한 길뿐이라, 서버가 없으면 아무 소식도 못 받는다.

    그래서 창도 브라우저와 같은 주소를 본다. 길이 하나면 시험도 하나로 끝난다.
    """

    def __init__(self, api: "Api", url: str) -> None:
        self._api = api
        self._url = url

    def open(self) -> None:
        if not DependencyBootstrap("webview", ("pywebview",), announce=False).try_ensure():
            raise RuntimeError("pywebview 를 설치하지 못했습니다")
        import webview
        window = webview.create_window(
            "Framing",
            url=self._url,
            js_api=self._api,
            # 두 칸(왼쪽 파일 · 오른쪽 작업)이 처음부터 나란히 서는 너비로 연다
            width=1360, height=900, min_size=(900, 640),
            background_color="#0f1113")
        self._api.bind(window)          # 창은 파일 고르기 · 끌어놓기 · 닫기에만
        # 끌어다 놓기는 창이 실제로 뜬 뒤에 걸어야 한다.
        # webview.start 에 넘긴 함수가 그 시점에 불린다 (공식 예제와 같은 방식).
        webview.start(self._after_start, window,
                      debug=bool(os.environ.get("FRAMING_DEBUG")))

    def _after_start(self, window) -> None:
        self._api.set_drop_ready(DropTarget(self._api).attach(window))
        WindowIcon().apply()


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
                capture_output=True, text=True)
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


class WindowIcon:
    """
    창과 작업표시줄의 아이콘을 고양이로.

    .bat · .command 로 도는 길은 파이썬이 스크립트를 읽는 것이라, 윈도우는 창
    아이콘으로 **파이썬 인터프리터의 것**을 씁니다. build/icon.ico 를 새로
    구워도 이 길에는 닿지 않습니다 — 그건 패키징한 exe 가 쓰는 파일입니다.

    그래서 아이콘을 실행 파일 안에 글자로 싣고 다니다가, 창이 뜬 뒤에 둘을 합니다.

      1. 프로세스에 제 이름표를 답니다(AppUserModelID). 안 달면 작업표시줄에서
         파이썬으로 묶입니다.
      2. 창 손잡이를 찾아 WM_SETICON 을 보냅니다.

    윈도우에서만 하는 일이고, 무엇 하나 안 되더라도 조용히 넘어갑니다 —
    아이콘 때문에 프로그램이 안 열리면 그게 훨씬 나쁩니다.
    """

    APP_ID = "AlienMeow.Framing"
    TITLE = "Framing"
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
    WAIT_SECONDS, STEP = 6.0, 0.2

    def apply(self) -> None:
        if sys.platform != "win32":
            return
        spot = self._write()
        if spot is not None:
            threading.Thread(target=self._stamp, args=(spot,), daemon=True).start()

    def _write(self) -> Optional[Path]:
        """싣고 다니던 아이콘을 파일로 꺼낸다 — Win32 는 파일 경로만 받는다."""
        blob = app_icon_bytes()
        if blob is None:
            return None
        try:
            spot = Path(tempfile.gettempdir()) / "framing-icon.ico"
            spot.write_bytes(blob)
            return spot
        except Exception:
            return None

    def _stamp(self, spot: Path) -> None:
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(self.APP_ID)
        except Exception:
            pass
        try:
            user32 = ctypes.windll.user32
            handle = user32.LoadImageW(None, str(spot), self.IMAGE_ICON, 0, 0,
                                       self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE)
            if not handle:
                return
            # 창이 실제로 뜨는 시점은 백엔드마다 다르다. 몇 초만 지켜본다.
            waited = 0.0
            while waited < self.WAIT_SECONDS:
                window = user32.FindWindowW(None, self.TITLE)
                if window:
                    for which in (self.ICON_SMALL, self.ICON_BIG):
                        user32.SendMessageW(window, self.WM_SETICON, which, handle)
                    return
                time.sleep(self.STEP)
                waited += self.STEP
        except Exception:
            pass


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
    """어느 방식으로 띄울지 정하고 띄운다. 창이 안 되면 브라우저로."""

    def __init__(self, carried: Sequence[str] = ()) -> None:
        self._api = Api()
        self._api.carry(carried)
        self._ui = UiDocument(UI_HTML)

    def launch(self) -> int:
        """
        서버를 먼저 세우고, 그 주소를 창에 물린다. 창이 안 되면 브라우저로.

        서버는 화면만 내주는 것이 아니다 — 엔진이 뱉는 소식을 담아 두는 큐이자,
        대조 화면이 영상을 읽어 가는 길이다. 그래서 창 모드에도 반드시 있어야 한다.
        """
        server = LocalServer(self._api, self._ui)
        url = server.start()
        try:
            if (os.environ.get("FRAMING_MODE") or "").lower() != "browser":
                try:
                    DesktopWindow(self._api, url).open()
                    return 0
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    print(f"[알림] 창을 띄우지 못했습니다: {exc}")
                    print("[알림] 대신 기본 브라우저로 엽니다. 기능은 똑같습니다.")
            server.open_browser()
            server.wait()
            return 0
        finally:
            server.stop()


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
            PipStep("프로그램 창 pywebview", "webview", ("pywebview",), "약 1MB", optional=True),
            FfmpegStep(FFmpegTool(allow_download=True)),
        ])

    @classmethod
    def run(cls, argv: Optional[Sequence[str]] = None) -> int:
        request = StartupRequest(sys.argv[1:] if argv is None else argv)
        if request.wants_cli:
            return CommandLine(request.args).run()         # 작업 이름이나 옵션이 있으면 터미널 도구로

        # 앱으로 구운 판(.exe · .app)은 필요한 것을 이미 안고 있다. 챙길 것도,
        # 콘솔을 떼려고 자기를 다시 띄울 것도 없다 — 하면 오히려 두 번 뜬다.
        # 앱으로 구운 판과 포터블 판(Framing.bat · 내장 파이썬)은 챙길 것을 이미 다 안고 있다
        if not getattr(sys, "frozen", False) and not os.environ.get("FRAMING_PORTABLE"):
            cls.wizard().ensure()               # 처음이면 여기서 다 챙긴다 (콘솔에 보이게)
            if Relauncher.detach_console():
                return 0                         # 콘솔을 떼고 새로 띄웠으니 여기서 끝
        return FramingApp(request.paths).launch()   # 아이콘 위에 놓은 파일을 들고 창으로


