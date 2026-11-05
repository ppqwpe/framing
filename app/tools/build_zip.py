"""배포 zip 만들기 — 받은 사람이 풀어서 바로 쓰게

  app\\python\\python.exe app\\tools\\build_zip.py            → 이 폴더 옆에 Framing_1.0_windows.zip
  app\\python\\python.exe app\\tools\\build_zip.py D:\\x.zip  → 원하는 곳에
  app\\python\\python.exe app\\tools\\build_zip.py --mac      → Framing_1.0_mac_arm64.zip · Framing_1.0_mac_x64.zip
  app\\python\\python.exe app\\tools\\build_zip.py --all      → 셋 다

zip 안의 맨 위 폴더 이름은 Framing. 쓰다가 생긴 것(app/work, 결과, __pycache__)과 깃 폴더, 개발용 Electron
(app/electron/node_modules)은 넣지 않는다.

Windows: 파이썬 · 라이브러리 · ffmpeg · RIFE(app/python, app/lib, app/ffmpeg, app/rife)는 깃에는 없지만 zip 에는
  꼭 들어가야 하므로, 이 폴더에 있어야 만든다. 창 버전(app/window, app/electron 에서 pnpm run build)이 있으면 같이 넣는다.
Mac: 소스 + Framing.command + app/window/Framing.app. Framing.app 은 app/electron 에서 pnpm run build:mac 으로 받아 둔
  Electron 원본 zip 의 항목을 그대로(심볼릭 링크 · 실행 권한 포함) 옮겨 담으며 이름 · 아이콘 · 앱 코드만 바꿔 만든다.
  Windows 는 관리자 권한 없이 심볼릭 링크를 못 만들어서, 디스크에 풀지 않고 zip 에서 zip 으로 옮긴다.
  서명은 Mac 에서 Framing.command 가 처음 열릴 때 그 Mac 용으로 한다 (Apple 개발자 서명이 없으므로).
"""
from __future__ import annotations

import plistlib
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "app"
VERSION = "1.0"


class ReleaseZip:
    """Framing 폴더를 zip 하나로. 무엇을 빼고 무엇을 꼭 넣을지는 판마다 다르다."""

    SKIP = {ROOT / "results", APP / "work", ROOT / ".git", APP / ".venv-mac",
            APP / "web" / "data", APP / "mac" / "build", APP / "mac" / "dist",
            APP / "electron" / "node_modules", APP / "window-mac"}
    SKIP_NAMES = {"__pycache__", ".gitignore", ".gitattributes", ".env"}
    EXECUTABLE = {"Framing.command"}            # Mac 에서 더블클릭으로 열리게 실행 권한을 얹는 파일

    def __init__(self, dest: Path) -> None:
        self.dest = dest

    def needed(self) -> tuple[Path, ...]:
        return ()

    def skipped(self) -> set[Path]:
        return set(self.SKIP)

    def files(self) -> list[Path]:
        skip = self.skipped()
        out = []
        for p in sorted(ROOT.rglob("*")):
            if any(p == s or s in p.parents for s in skip) or any(part in self.SKIP_NAMES for part in p.parts):
                continue
            if p.is_file() and not (p.parent == ROOT and p.suffix == ".zip"):   # 맨 위의 옛 배포 zip 만 뺀다 (python312.zip 은 꼭 필요)
                out.append(p)
        return out

    def check(self) -> None:
        missing = [str(p) for p in self.needed() if not p.exists()]
        if missing:
            raise SystemExit(f"{self.dest.name} 를 만들 수 없습니다 — 없는 것: " + ", ".join(missing))

    def extra(self, z: zipfile.ZipFile) -> int:
        """판마다 덧붙일 것. 넣은 항목 수를 돌려준다."""
        return 0

    def build(self) -> tuple[int, float]:
        self.check()
        tmp = self.dest.with_suffix(".tmp")
        items = self.files()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in items:
                self._write(z, p)
            count = len(items) + self.extra(z)
        tmp.replace(self.dest)
        return count, self.dest.stat().st_size / 1e6

    def _write(self, z: zipfile.ZipFile, p: Path) -> None:
        mode = 0o755 if p.name in self.EXECUTABLE else 0o644
        self.add_file(z, p, (Path("Framing") / p.relative_to(ROOT)).as_posix(), mode)

    @staticmethod
    def add_file(z: zipfile.ZipFile, src: Path, name: str, mode: int = 0o644) -> None:
        info = zipfile.ZipInfo.from_file(src, name)
        info.create_system = 3                                   # 유닉스 권한을 Mac 이 읽게
        info.external_attr = (stat.S_IFREG | mode) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        with open(src, "rb") as source, z.open(info, "w") as dst:
            while chunk := source.read(1 << 20):
                dst.write(chunk)


class WindowsZip(ReleaseZip):
    """내장 파이썬 · ffmpeg · RIFE 를 안은 판. Framing.bat(브라우저) · Framing_App.bat(창)."""

    def needed(self) -> tuple[Path, ...]:
        return (APP / "python" / "python.exe", APP / "lib",
                APP / "ffmpeg" / "ffmpeg.exe", APP / "ffmpeg" / "ffprobe.exe",
                APP / "rife" / "rife-ncnn-vulkan.exe")

    def check(self) -> None:
        super().check()
        if not (APP / "window" / "Framing.exe").exists():
            print("[알림] app/window/Framing.exe 가 없어 창 버전은 빠집니다 (app/electron 에서 pnpm run build)")


class MacZip(ReleaseZip):
    """소스 + Framing.command + Framing.app (Electron 원본에서 옮겨 담음). Windows 전용 런타임은 뺀다."""

    BUNDLE = "Framing/app/window/Framing.app"
    KEEP_LPROJ = {"en.lproj", "ko.lproj", "Base.lproj"}
    APP_CODE = ("package.json", "main.js", "preload.js")
    DROP = {"Contents/Resources/default_app.asar", "Contents/Resources/electron.icns"}

    def __init__(self, dest: Path, arch: str) -> None:
        super().__init__(dest)
        self.arch = arch
        self.runtime = APP / "window-mac" / f"electron-darwin-{arch}.zip"

    def needed(self) -> tuple[Path, ...]:
        return (self.runtime, APP / "icon" / "framing.icns")

    def skipped(self) -> set[Path]:
        return super().skipped() | {APP / "python", APP / "lib", APP / "ffmpeg", APP / "rife", APP / "window",
                                     ROOT / "Framing.bat", ROOT / "Framing_App.bat"}

    def extra(self, z: zipfile.ZipFile) -> int:
        count = 0
        with zipfile.ZipFile(self.runtime) as electron:
            for info in electron.infolist():
                target = self.place(info.filename)
                if target is None:
                    continue
                data = electron.read(info)
                if target == f"{self.BUNDLE}/Contents/Info.plist":
                    data = self.plist(data)
                copy = zipfile.ZipInfo(target, info.date_time)
                copy.create_system = 3
                copy.external_attr = info.external_attr                # 실행 권한 · 심볼릭 링크 그대로
                copy.compress_type = zipfile.ZIP_STORED if stat.S_ISLNK(info.external_attr >> 16) else zipfile.ZIP_DEFLATED
                z.writestr(copy, data)
                count += 1
        resources = f"{self.BUNDLE}/Contents/Resources"
        self.add_file(z, APP / "icon" / "framing.icns", f"{resources}/framing.icns")
        for name in self.APP_CODE:
            self.add_file(z, APP / "electron" / name, f"{resources}/app/{name}")
        return count + 1 + len(self.APP_CODE)

    def place(self, name: str) -> str | None:
        """Electron.app/... → Framing/app/window/Framing.app/...  (기본 앱 · 기본 아이콘 · 안 쓰는 언어는 버린다)"""
        if not name.startswith("Electron.app/"):
            return None
        rest = name[len("Electron.app/"):]
        if rest in self.DROP:
            return None
        if any(p.endswith(".lproj") and p not in self.KEEP_LPROJ for p in rest.rstrip("/").split("/")):
            return None
        return f"{self.BUNDLE}/{rest}"

    @staticmethod
    def plist(data: bytes) -> bytes:
        info = plistlib.loads(data)
        info.update({"CFBundleName": "Framing", "CFBundleDisplayName": "Framing",
                     "CFBundleIdentifier": "com.alienmeow.framing", "CFBundleIconFile": "framing.icns",
                     "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
                     "LSApplicationCategoryType": "public.app-category.video"})
        info.pop("ElectronAsarIntegrity", None)            # default_app.asar 를 뺐으니 그 지문도 뺀다
        for key in ("NSBluetoothAlwaysUsageDescription", "NSBluetoothPeripheralUsageDescription",
                    "NSCameraUsageDescription", "NSMicrophoneUsageDescription"):
            info.pop(key, None)                             # Framing 은 블루투스 · 카메라 · 마이크를 쓰지 않는다
        return plistlib.dumps(info)


class Releases:
    """명령줄로 고른 판들."""

    @staticmethod
    def chosen(argv: list[str]) -> list[ReleaseZip]:
        where = Path(next((a for a in argv if not a.startswith("--")), ROOT.parent))
        windows = WindowsZip(where if where.suffix == ".zip" else where / f"Framing_{VERSION}_windows.zip")
        folder = where.parent if where.suffix == ".zip" else where
        macs = [MacZip(folder / f"Framing_{VERSION}_mac_{arch}.zip", arch) for arch in ("arm64", "x64")]
        if "--all" in argv:
            return [windows, *macs]
        return macs if "--mac" in argv else [windows]


if __name__ == "__main__":
    for release in Releases.chosen(sys.argv[1:]):
        n, mb = release.build()
        print(f"{release.dest} · 파일 {n}개 · {mb:.0f}MB")
