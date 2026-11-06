"""영상 · 시퀀스 읽기, ffprobe 정보, FFmpeg 실행"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence
from typing import TYPE_CHECKING

from . import env
from .errors import FramingError
from .env import IMAGE_EXTENSIONS, cv2
from .util import AsciiPathAlias, Console, ImageIO, Progress, SEEK_BACKOFF, TAIL_SCAN, human, is_interactive

if TYPE_CHECKING:   # 타입 표기에만 쓰는 이름
    from .env import np


class VideoSource:
    """영상을 열고 원하는 인덱스의 프레임을 안전하게 꺼내준다 (임의 접근용)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._capture: Optional["cv2.VideoCapture"] = None
        self._alias = AsciiPathAlias(path)
        self._active_path: Optional[Path] = None
        self._frame_count: Optional[int] = None

    def __enter__(self) -> "VideoSource":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open(self) -> None:
        active = self._path
        capture = cv2.VideoCapture(str(active))
        if not capture.isOpened():
            capture.release()
            active = self._alias.create()
            capture = cv2.VideoCapture(str(active))
        if not capture.isOpened():
            capture.release()
            self._alias.cleanup()
            raise FramingError(f"영상을 열 수 없습니다: {self._path.name}")
        self._capture = capture
        self._active_path = active

    def reopen(self) -> "cv2.VideoCapture":
        if self._capture is not None:
            self._capture.release()
        capture = cv2.VideoCapture(str(self._active_path or self._path))
        if not capture.isOpened():
            capture.release()
            raise FramingError(f"영상을 다시 열 수 없습니다: {self._path.name}")
        self._capture = capture
        return capture

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._alias.cleanup()

    def _require(self) -> "cv2.VideoCapture":
        if self._capture is None:
            raise FramingError("영상이 열려 있지 않습니다.")
        return self._capture

    @property
    def fps(self) -> float:
        return float(self._require().get(cv2.CAP_PROP_FPS) or 0.0)

    @property
    def resolution(self) -> tuple[int, int]:
        capture = self._require()
        return (int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0))

    @property
    def frame_count(self) -> int:
        if self._frame_count is None:
            reported = int(self._require().get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            self._frame_count = reported if reported > 0 else self._count_by_scan()
        return self._frame_count

    def _count_by_scan(self) -> int:
        capture = self._require()
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        total = 0
        while capture.grab():
            total += 1
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        return total

    def read_at(self, target: int) -> tuple[Optional["np.ndarray"], int]:
        capture = self._require()
        target = max(0, target)
        best: tuple[Optional["np.ndarray"], int] = (None, -1)
        for backoff in (SEEK_BACKOFF, SEEK_BACKOFF * 20, None):
            if backoff is None:
                capture = self.reopen()
                position = 0
            else:
                anchor = max(0, target - backoff)
                capture.set(cv2.CAP_PROP_POS_FRAMES, anchor)
                position = int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0)
                if position > target and anchor > 0:
                    continue
            frame, index = self._decode_forward(capture, target, position)
            if frame is not None:
                if index >= target:
                    return frame, index
                best = (frame, index)
        return best

    @staticmethod
    def _decode_forward(capture: "cv2.VideoCapture", target: int,
                        position: int) -> tuple[Optional["np.ndarray"], int]:
        frame: Optional["np.ndarray"] = None
        index = -1
        for _ in range(max(1, target - position) + 16):
            ok, decoded = capture.read()
            if not ok:
                break
            frame = decoded
            index = max(0, int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0) - 1)
            if index >= target:
                break
        return frame, index

    def read_last(self) -> tuple[Optional["np.ndarray"], int]:
        capture = self._require()
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, self.frame_count - TAIL_SCAN))
        frame, index = self._drain(capture)
        if frame is not None:
            return frame, index
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        return self._drain(capture)

    @staticmethod
    def _drain(capture: "cv2.VideoCapture") -> tuple[Optional["np.ndarray"], int]:
        frame: Optional["np.ndarray"] = None
        index = -1
        while True:
            ok, decoded = capture.read()
            if not ok:
                break
            frame = decoded
            index = max(0, int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0) - 1)
        return frame, index


class FrameFeed(ABC):
    """프레임을 순서대로 흘려주는 공급원. 영상이든 시퀀스 폴더든 같은 얼굴."""

    @property
    @abstractmethod
    def count(self) -> int: ...

    @property
    def fps(self) -> float:
        return 0.0

    @abstractmethod
    def frames(self, reverse: bool = False) -> Iterator["np.ndarray"]: ...

    def close(self) -> None:
        pass

    def __enter__(self) -> "FrameFeed":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ImageSequenceFeed(FrameFeed):
    def __init__(self, paths: Sequence[Path], preserve: bool = False) -> None:
        self._paths = list(paths)
        self._preserve = preserve

    @property
    def count(self) -> int:
        return len(self._paths)

    @property
    def paths(self) -> list[Path]:
        return list(self._paths)

    def frames(self, reverse: bool = False) -> Iterator["np.ndarray"]:
        signature = None
        for path in (reversed(self._paths) if reverse else self._paths):
            image = ImageIO.read(path, cv2.IMREAD_UNCHANGED if self._preserve else cv2.IMREAD_COLOR)
            if image is None:
                raise FramingError(f"이미지를 읽지 못했습니다: {path.name}")
            if self._preserve:
                current = (image.shape, image.dtype)
                if signature is not None and current != signature:
                    raise FramingError(f'시퀀스의 크기·채널·비트 깊이를 맞춰 주세요: {path.name}')
                signature = current
            yield image


class VideoFrameFeed(FrameFeed):
    """영상을 앞에서부터 순차 디코딩한다. 역순은 인덱스를 뒤집어 저장해 해결한다."""

    def __init__(self, path: Path) -> None:
        self._source = VideoSource(path)
        self._source.open()

    @property
    def count(self) -> int:
        return self._source.frame_count

    @property
    def fps(self) -> float:
        return self._source.fps or 24.0

    def frames(self, reverse: bool = False) -> Iterator["np.ndarray"]:
        capture = self._source.reopen()
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            yield frame

    def close(self) -> None:
        self._source.close()


def collect_images(folder: Path) -> list[Path]:
    found = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    return sorted(found, key=lambda p: [int(s) if s.isdigit() else s.casefold()
                                       for s in re.split(r'(\d+)', p.name)])


def open_feed(target: Path, preserve: bool = False) -> FrameFeed:
    if target.is_dir():
        images = collect_images(target)
        if not images:
            raise FramingError(f"이미지가 없습니다: {target.name}")
        return ImageSequenceFeed(images, preserve=preserve)
    return VideoFrameFeed(target)


# ══════════════════════════════════════════════════ ffmpeg

@dataclass
class ProbeInfo:
    vcodec: str = ""
    acodec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    video_duration: float = 0.0
    alpha: bool = False
    pix_fmt: str = ""
    color_range: str = ""          # 'tv' | 'pc' | '' — 영상이 스스로 적어 둔 밝기 범위
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""          # 매트릭스. bt709 / bt470bg / smpte170m …
    reported_frames: int = 0       # Prefer the video stream's count over duration estimates.
    timecode: str = ""             # 시작 타임코드 (01:00:00:00). 없으면 빈칸
    audio_bits: int = 0            # 소리의 비트 깊이 (pcm 24bit 면 24)

    def timecode_args(self) -> list[str]:
        """
        결과 영상에 원본 시작 타임코드를 그대로 단다.

        리타임 · 플레이트 · 홀드는 길이가 바뀌어도 시작점은 원본과 같다. 타임코드가 빠지면
        편집 · 컨폼에서 이 플레이트가 어디서 왔는지 다시 찾아야 한다.
        """
        return ["-timecode", self.timecode] if self.timecode else []

    @property
    def color_metadata(self) -> dict:
        """Declared stream tags, never guessed from dimensions or codec."""
        def clean(value):
            return value if value and value not in ('unknown', 'unspecified', 'reserved', 'N/A') else ''
        tags = {key: clean(getattr(self, 'color_' + key))
                for key in ('space', 'primaries', 'transfer', 'range')}
        labels = {'bt709': 'Rec.709', 'bt2020': 'Rec.2020', 'smpte432': 'Display P3',
                  'smpte2084': 'PQ (ST 2084)', 'arib-std-b67': 'HLG',
                  'iec61966-2-1': 'sRGB', 'tv': 'Limited', 'pc': 'Full'}
        shown = {key: labels.get(value, value) if value else '미지정' for key, value in tags.items()}
        return {**tags, 'pixelFormat': self.pix_fmt, 'declared': any(tags.values()),
                'summary': f"색역 {shown['primaries']} · 전달 {shown['transfer']} · 매트릭스 {shown['space']} · 범위 {shown['range']}",
                'labels': shown}

    @property
    def frames(self) -> int:
        if self.reported_frames > 0:
            return self.reported_frames
        duration = self.video_duration or self.duration
        if not math.isfinite(duration) or not math.isfinite(self.fps) or duration <= 0 or self.fps <= 0:
            return 0
        return max(1, int(round(duration * self.fps)))


class OutputColorTags:
    """
    출력 쪽 색 표기(-color_primaries · -color_trc · -colorspace · -color_range)를 setparams 필터로도 건다.

    ffmpeg 9 는 그림 파일(PNG 등)이나 필터를 거쳐 온 프레임에 이미 "모름" 이 적혀 있으면
    출력 옵션으로 준 primaries · transfer 를 무시하고 매트릭스 · 범위만 남긴다. 재 보니
    리타임 · 플레이트 · 시퀀스→영상 결과가 전부 bt709 색역 · 전달 표기를 잃고 있었다.
    프레임 자체에 setparams 로 적으면 인코더가 그대로 받는다.
    """

    FLAGS = {"-color_primaries": "color_primaries", "-color_trc": "color_trc",
             "-colorspace": "colorspace", "-color_range": "range"}
    SETPARAMS_NAME = {"rgb": "gbr"}         # 인코더 이름 → setparams 이름

    @classmethod
    def fold(cls, args: Sequence[str]) -> list[str]:
        args = list(args)
        if "-filter_complex" in args or not args:
            return args
        last_input = max((i for i, a in enumerate(args) if a == "-i"), default=-1)
        tail = args[last_input + 2:] if last_input >= 0 else args
        for i, a in enumerate(tail[:-1]):
            if a in ("-c:v", "-vcodec", "-c") and tail[i + 1] == "copy":
                return args
        values = {}
        for i, a in enumerate(tail[:-1]):
            if a in cls.FLAGS:
                value = cls.SETPARAMS_NAME.get(tail[i + 1], tail[i + 1])
                if not (a == "-colorspace" and value == "gbr"):
                    values[cls.FLAGS[a]] = value
        if not values:
            return args
        stamp = "setparams=" + ":".join(f"{k}={v}" for k, v in values.items())
        start = last_input + 2 if last_input >= 0 else 0
        for i in range(len(args) - 2, start - 1, -1):
            if args[i] in ("-vf", "-filter:v"):
                args[i + 1] = f"{args[i + 1]},{stamp}"
                return args
        return args[:-1] + ["-vf", stamp, args[-1]]


class LocalStaging:
    """
    네트워크 드라이브로 나가는 영상은 로컬 임시 폴더에서 다 만든 뒤 옮긴다.

    MOV · MP4 는 끝까지 쓴 다음 앞으로 돌아가 헤더를 고쳐 쓴다. RaiDrive(SFTP) 같은 마운트는 이런
    되돌아 쓰기와, 다른 프로그램이 반쯤 쓴 파일을 읽는 일에 약하다. 로컬에서 끝내고 완성본만 옮긴다.
    (11/06 "284MB 에서 멈춤" 의 원인은 이게 아니라 stdin 이었다 — _dispatch 의 stdin 주석 참고.)
    번호 붙은 시퀀스(%04d)는 한 장씩 차례로 쓰기만 해서 그대로 둔다.
    """

    def __init__(self, args: Sequence[str]) -> None:
        self.args = list(args)
        self.destination: Optional[Path] = None
        self.temporary: Optional[Path] = None
        out = self.args[-1] if self.args else ""
        if out and "%" not in out and not out.startswith("-") and out not in ("-", "pipe:1") \
                and self.remote(Path(out)):
            self.destination = Path(out)
            folder = Path(tempfile.mkdtemp(prefix="framing_stage_"))
            self.temporary = folder / self.destination.name
            self.args[-1] = str(self.temporary)

    @staticmethod
    def remote(path: Path) -> bool:
        text = str(path)
        if text.startswith(("\\\\", "//")):
            return True
        if sys.platform != "win32":
            return False
        drive = os.path.splitdrive(os.path.abspath(text))[0]
        if not drive:
            return False
        try:
            import ctypes
            kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive + "\\"))
            return kind == 4                      # DRIVE_REMOTE
        except Exception:
            return False

    @property
    def active(self) -> bool:
        return self.destination is not None

    def blocked(self) -> bool:
        """-n (덮어쓰지 않음) 인데 이미 있으면 ffmpeg 처럼 실패한다."""
        return self.active and "-n" in self.args and self.destination.exists()

    def finish(self, code: int) -> int:
        if not self.active:
            return code
        try:
            if code == 0 and self.temporary.exists():
                Console.info(f"네트워크 드라이브라 로컬에서 만든 뒤 옮깁니다 → {self.destination.name}")
                shutil.move(str(self.temporary), str(self.destination))
        except OSError as exc:
            Console.warn(f"결과를 옮기지 못했습니다: {exc}")
            code = 1
        finally:
            shutil.rmtree(self.temporary.parent, ignore_errors=True)
        return code


class FFmpegTool:
    """ffmpeg / ffprobe 를 찾고, 없으면 (윈도우에 한해) 내려받아 옆에 둔다."""

    DOWNLOADS = (
        ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
         "gyan.dev (ffmpeg.org 공식 링크)"),
        ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
         "ffmpeg-master-latest-win64-gpl.zip", "github.com/BtbN/FFmpeg-Builds"),
    )

    def __init__(self, allow_download: bool = True) -> None:
        self._allow_download = allow_download
        self._cache: dict[str, Optional[str]] = {}
        self._active: Optional[subprocess.Popen] = None   # 중단 버튼이 이걸 붙잡는다

    def abort(self) -> bool:
        """돌고 있는 ffmpeg 을 끊는다. 끊을 게 있었으면 True."""
        process = self._active
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                return True
            except Exception:
                return False
        return False

    # --- 탐색 -------------------------------------------------------------

    def find(self, name: str) -> Optional[str]:
        if name in self._cache:
            return self._cache[name]
        found = self._search(name)
        self._cache[name] = found
        return found

    @staticmethod
    def _search(name: str) -> Optional[str]:
        for ext in (".exe", ""):
            candidate = env.FFMPEG_HOME / (name + ext)
            if candidate.is_file():
                return str(candidate)
        found = shutil.which(name)
        if found:
            return found
        guesses = [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
                   r"C:\ProgramData\chocolatey\bin",
                   os.path.expanduser(r"~\scoop\shims")]
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            guesses += glob.glob(os.path.join(local, "Microsoft", "WinGet",
                                              "Packages", "*FFmpeg*", "*", "bin"))
            guesses += glob.glob(os.path.join(local, "Microsoft", "WinGet",
                                              "Packages", "*FFmpeg*", "bin"))
        for folder in guesses:
            for ext in (".exe", ""):
                candidate = Path(folder) / (name + ext)
                if candidate.is_file():
                    return str(candidate)
        return None

    def ensure(self) -> str:
        """ffmpeg 경로를 돌려주거나, 없으면 설명과 함께 예외를 던진다."""
        found = self.find("ffmpeg")
        if found:
            return found
        if self._allow_download:
            found = self._install()
            self._cache["ffmpeg"] = found
            self._cache.pop("ffprobe", None)
        if found:
            return found
        raise FramingError(
            "ffmpeg 이 없습니다. 이 작업은 ffmpeg 없이는 못 합니다.\n"
            "         윈도우 : winget install Gyan.FFmpeg\n"
            "         macOS  : brew install ffmpeg\n"
            "         우분투 : sudo apt install ffmpeg")

    def ensure_probe(self) -> str:
        """
        ffprobe 경로.

        예전에는 부르는 쪽이 ffmpeg 경로에 ``.replace('ffmpeg', 'ffprobe')`` 를
        걸었다. 리눅스의 ``/usr/bin/ffmpeg`` 에서는 맞아떨어졌지만 윈도우의
        ``...\\.framing\\ffmpeg\\bin\\ffmpeg.exe`` 에서는 **폴더 이름까지 같이**
        바뀌어 없는 경로가 됐다 — WinError 2. str.replace 는 몇 번째인지
        가리지 않는다.

        그래서 찾기는 찾는 쪽(find)에만 둔다. 이름을 손으로 만들지 않는다.
        """
        self.ensure()                       # ffmpeg 이 없으면 여기서 설명과 함께 멈춘다
        found = self.find("ffprobe")
        if found:
            return found
        raise FramingError(
            "ffprobe 를 찾지 못했습니다. ffmpeg 은 있는데 ffprobe 만 없는 상태입니다 — "
            "대개 ffmpeg 을 폴더째 받지 않고 실행 파일 하나만 옮겼을 때 이렇게 됩니다.")

    @property
    def platform_hint(self) -> str:
        return {"darwin": "brew install ffmpeg",
                "win32": "winget install Gyan.FFmpeg"}.get(
                    sys.platform, "sudo apt install ffmpeg")

    @property
    def ffprobe(self) -> Optional[str]:
        return self.find("ffprobe")

    # --- 내려받기 ---------------------------------------------------------

    def _install(self) -> Optional[str]:
        if sys.platform == "darwin":
            return self._install_mac()
        if not sys.platform.startswith("win"):
            Console.warn("ffmpeg 이 없습니다.")
            Console.info("우분투 · 데비안 : sudo apt install ffmpeg")
            Console.info("페도라        : sudo dnf install ffmpeg")
            return None

        Console.warn("ffmpeg 이 없어 내려받습니다. 약 80MB, 최초 1회.")
        Console.info(f"설치 위치: {env.FFMPEG_HOME}  (이 폴더만 지우면 원상복구)")
        # Concurrent launches must not share or delete each other's download.
        archive = Path(tempfile.gettempdir()) / f"framing_ffmpeg_{uuid.uuid4().hex}.zip"
        for url, who in self.DOWNLOADS:
            Console.step(f"{who} 에서 받는 중")
            try:
                size = self._fetch(url, archive)
                if size < 1_000_000:
                    raise FramingError(f"{human(size)} 밖에 못 받았습니다")
                self._unpack(archive, env.FFMPEG_HOME)
                target = env.FFMPEG_HOME / "ffmpeg.exe"
                if not target.is_file():
                    raise FramingError("압축 안에 ffmpeg.exe 가 없습니다")
                Console.ok(f"준비됨: {target}")
                return str(target)
            except Exception as exc:
                Console.fail(f"실패: {exc}")
            finally:
                if archive.exists():
                    try:
                        archive.unlink()
                    except OSError:
                        pass
        Console.warn("두 곳 모두 실패했습니다. 회사망·프록시 차단일 수 있습니다.")
        Console.info("winget install Gyan.FFmpeg 를 쓰거나,")
        Console.info(f"직접 받은 ffmpeg.exe / ffprobe.exe 를 {env.FFMPEG_HOME} 에 넣어 주세요.")
        return None

    def _install_mac(self) -> Optional[str]:
        """
        맥에서는 Homebrew 가 사실상 표준이라 그쪽에 맡긴다.

        윈도우처럼 직접 내려받지 않는 이유가 있다. 애플 실리콘과 인텔은 다른 바이너리를
        써야 하고, 내려받은 실행 파일에는 격리 딱지가 붙어 macOS 가 실행을 막는다.
        그걸 코드로 우회하는 건 사용자 몰래 보안 설정을 건드리는 일이라 하지 않는다.
        """
        Console.warn("ffmpeg 이 없습니다. 이 작업에는 ffmpeg 이 필요합니다.")
        if shutil.which("brew"):
            Console.info("Homebrew 가 있습니다. 설치를 시작합니다 (몇 분 걸립니다)")
            subprocess.run(["brew", "install", "ffmpeg"], check=False, stdin=subprocess.DEVNULL)
            found = self._search("ffmpeg")
            if found:
                Console.ok(f"준비됨: {found}")
                return found
            Console.fail("설치가 끝났는데도 못 찾았습니다. 터미널을 새로 열고 다시 실행해 보세요.")
            return None

        Console.info("Homebrew 가 없습니다. 터미널에 아래 두 줄을 차례로 붙여넣으세요.")
        Console.info("")
        Console.info('  /bin/bash -c "$(curl -fsSL '
                     'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
        Console.info("  brew install ffmpeg")
        Console.info("")
        Console.info("끝나면 이 창을 닫고 Framing 을 다시 열면 됩니다.")
        return None

    @staticmethod
    def _fetch(url: str, destination: Path) -> int:
        import urllib.request
        request = urllib.request.Request(url, headers={"User-Agent": "framing"})
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            got, last = 0, 0.0
            with open(destination, "wb") as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if now - last > 0.25:
                        last = now
                        if total:
                            pct = got * 100.0 / total
                            bars = "#" * int(pct / 2.5)
                            line = f"       [{bars:<40}] {pct:5.1f}%  {human(got)} / {human(total)}"
                        else:
                            line = f"       {human(got)}"
                        sys.stdout.write("\r" + line.ljust(78))
                        sys.stdout.flush()
        sys.stdout.write("\r" + f"       [{'#' * 40}] 100.0%  {human(got)}".ljust(78) + "\n")
        return got

    @staticmethod
    def _unpack(archive: Path, destination: Path) -> None:
        import zipfile
        wanted = ("ffmpeg.exe", "ffprobe.exe")
        found: dict[str, str] = {}
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                base = os.path.basename(member).lower()
                if base in wanted and base not in found:
                    found[base] = member
            missing = [w for w in wanted if w not in found]
            if missing:
                raise FramingError("압축 안에 " + ", ".join(missing) + " 이 없습니다")
            destination.mkdir(parents=True, exist_ok=True)
            for base, member in found.items():
                out = destination / base
                with zf.open(member) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(out, 0o755)

    # --- 실행 -------------------------------------------------------------

    def probe(self, path: Path) -> Optional[ProbeInfo]:
        tool = self.ffprobe
        if not tool:
            return None
        try:
            result = subprocess.run(
                [tool, "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL)
            data = json.loads(result.stdout or "{}")
        except Exception:
            return None

        info = ProbeInfo()
        for stream in data.get("streams", []):
            kind = stream.get("codec_type")
            if kind == "video" and not info.vcodec:
                info.vcodec = stream.get("codec_name") or ""
                try:
                    info.reported_frames = max(0, int(stream.get('nb_frames') or 0))
                except (TypeError, ValueError):
                    pass
                try:
                    info.video_duration = float(stream.get('duration') or 0)
                except (TypeError, ValueError):
                    pass
                info.width = int(stream.get("width") or 0)
                info.height = int(stream.get("height") or 0)
                pix = stream.get("pix_fmt") or ""
                info.pix_fmt = pix
                info.alpha = pix.startswith(("yuva", "rgba", "bgra", "argb", "abgr",
                                             "gbrap", "ya"))
                tidy = lambda key: (stream.get(key) or "").strip().lower()
                info.color_range = tidy("color_range")
                info.color_primaries = tidy("color_primaries")
                info.color_transfer = tidy("color_transfer")
                info.color_space = tidy("color_space")
                rate = stream.get("r_frame_rate") or "0/1"
                try:
                    num, den = rate.split("/")
                    info.fps = float(num) / float(den) if float(den) else 0.0
                except Exception:
                    pass
            elif kind == "audio" and not info.acodec:
                info.acodec = stream.get("codec_name") or ""
                info.audio_bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
            # 시작 타임코드는 tmcd 트랙 · 영상 스트림 · 포맷 어디에든 적힐 수 있다
            info.timecode = info.timecode or (stream.get("tags") or {}).get("timecode", "")
        info.timecode = info.timecode or (data.get("format", {}).get("tags") or {}).get("timecode", "")
        try:
            info.duration = float(data.get("format", {}).get("duration") or 0)
        except Exception:
            pass
        return info

    def why_failed(self) -> str:
        """마지막으로 조용히 돌린 ffmpeg 이 남긴 말. 실패했을 때만 쓸모가 있다."""
        return (getattr(self, "_last_error", "") or "").strip()

    def run(self, args: Sequence[str], quiet: bool = False,
            total_seconds: float = 0.0) -> int:
        """
        total_seconds 를 주면 ffmpeg 에게 진행 상황을 받아 몇 %인지 알려준다.
        긴 인코딩에서 화면이 멈춘 것처럼 보이지 않게 하려는 것.
        """
        binary = self.ensure()
        staging = LocalStaging(OutputColorTags.fold(args))
        if staging.blocked():
            self._last_error = f"이미 있습니다: {staging.destination}"
            return 1
        return staging.finish(self._dispatch(binary, staging.args, quiet, total_seconds))

    def _dispatch(self, binary: str, args: Sequence[str], quiet: bool,
                  total_seconds: float) -> int:
        if total_seconds and total_seconds > 0:
            return self._run_with_progress(binary, args, quiet, total_seconds)
        if quiet:
            # 조용히 돌리더라도 하는 말은 받아 둔다. 아무 말 없이 실패하면
            # 왜 안 되는지 아무도 알 수 없다 — 실패했을 때만 꺼내 보여준다.
            return self._run_quiet(binary, args)

        # 진행 표시는 터미널에서만 의미가 있다. 파이프로 넘길 땐 로그만 더럽힌다.
        show_stats = is_interactive()
        command = [binary, "-hide_banner", "-loglevel", "error",
                   "-stats" if show_stats else "-nostats"]
        command += list(args)
        # stdin 은 넘겨주지 않는다 (아래 두 곳도 같다). Electron 창이 엔진에 물린 파이프를 ffmpeg 이
        # 물려받으면, 엔진의 감시 스레드가 그 파이프를 읽는 동안 ffmpeg 이 마무리 단계에서 멈춘다
        # (재 봄: MOV 목차를 못 써서 누크에서 1프레임 ERROR). 로컬 · 네트워크 드라이브를 가리지 않았다.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        self._active = process
        try:
            process.wait()
        finally:
            self._active = None
        completed = process
        if show_stats:
            width = shutil.get_terminal_size((120, 24)).columns
            sys.stdout.write("\r" + " " * max(80, width) + "\r")   # 진행 줄 지우기
            sys.stdout.flush()
        return completed.returncode

    def _run_quiet(self, binary: str, args: Sequence[str]) -> int:
        """화면은 조용히, 오류 메시지는 손에 쥔 채로."""
        command = [binary, "-hide_banner", "-loglevel", "error", "-nostats", *args]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True,
                                   errors="replace")
        self._active = process
        try:
            _out, err = process.communicate()
        finally:
            self._active = None
        lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
        self._last_error = " / ".join(lines[-3:])
        return process.returncode

    def _run_with_progress(self, binary: str, args: Sequence[str], quiet: bool,
                           total_seconds: float) -> int:
        command = [binary, "-hide_banner", "-loglevel", "error", "-nostats",
                   "-progress", "pipe:1", *args]
        # A pipe for stderr can fill while we read progress from stdout, deadlocking
        # the encoder. A temporary file drains diagnostics without a second thread.
        self._last_error = ""
        with tempfile.TemporaryFile(mode="w+b") as diagnostics:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
                                       encoding="utf-8", errors="replace", bufsize=1,
                                       stderr=diagnostics)
            self._active = process
            total_us = total_seconds * 1_000_000
            try:
                for line in process.stdout:
                    key, _, value = line.strip().partition("=")
                    if key not in ("out_time_us", "out_time_ms"):
                        continue
                    try:
                        done_us = int(value)
                    except ValueError:
                        continue
                    Progress.span(min(1.0, done_us / total_us) if total_us else 0.0)
                process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                if process.stdout:
                    process.stdout.close()
                self._active = None
            # Keep only a bounded tail, even when a damaged input floods stderr.
            diagnostics.seek(0, os.SEEK_END)
            diagnostics.seek(max(0, diagnostics.tell() - 16384))
            lines = [line.strip() for line in diagnostics.read().decode("utf-8", "replace").splitlines() if line.strip()]
            self._last_error = " / ".join(lines[-3:])
            if process.returncode == 0:
                Progress.span(1.0)
            elif not quiet and self._last_error:
                Console.warn(self._last_error)
            return process.returncode


# ══════════════════════════════════════════════════ 작업 공통


# ══════════════════════════════════════════════════ 1. 프레임 3장


