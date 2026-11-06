"""콘솔 출력 · 경로 · 임시 폴더 · 진행 표시 · 프레임 번호"""
from __future__ import annotations

import atexit
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .errors import EngineBridge, FramingError
from .env import BAR, cv2, np


class Console:
    """Console formatting with optional context-local event delivery."""
    @staticmethod
    def _write(kind, text, formatted):
        if not EngineBridge.emit('log', {'kind': kind, 'text': str(text)}):
            try:
                print(formatted)
            except UnicodeEncodeError:
                encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
                print(str(formatted).encode(encoding, errors='backslashreplace').decode(encoding))

    @staticmethod
    def banner(text): Console._write('head', text, f"\n{BAR}\n  {text}\n{BAR}")
    @staticmethod
    def section(text): Console._write('head', text, f"\n  {text}")
    @staticmethod
    def info(text): Console._write('info', text, f"     {text}")
    @staticmethod
    def step(text): Console._write('info', text, f"     · {text}")
    @staticmethod
    def warn(text): Console._write('warn', text, f"     [!] {text}")
    @staticmethod
    def ok(text): Console._write('ok', text, f"     ✓ {text}")
    @staticmethod
    def fail(text): Console._write('err', text, f"     ✗ {text}")


def is_interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def normalize_path(raw: str | Path) -> Path:
    """따옴표와 터미널 드래그로 붙는 역슬래시 이스케이프를 걷어낸다."""
    text = str(raw).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    path = Path(text).expanduser()
    if os.name != "nt" and not path.exists() and "\\" in text:
        unescaped = Path(re.sub(r"\\(.)", r"\1", text)).expanduser()
        if unescaped.exists():
            return unescaped
    return path


def display_pad(text: str, width: int) -> str:
    """한글은 터미널에서 두 칸을 먹는다. 그걸 감안해 자리를 맞춘다."""
    import unicodedata
    used = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)
    return text + " " * max(1, width - used)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def hms(seconds: float) -> str:
    if not seconds:
        return "?"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def unique_path(path: Path) -> Path:
    """이미 있으면 _1, _2 를 붙여 새 이름을 만든다."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    index = 1
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


class FFImage:
    """
    EXR · DPX 를 ffmpeg 으로 읽고 쓴다. 이 OpenCV(5, headless)는 둘 다 못 연다.

    배열 모양은 OpenCV 와 같게 맞춘다 (BGR · BGRA). 값은 손대지 않는다:
      · EXR → float32. 선형 값 그대로, 1.0 넘는 하이라이트도 그대로
      · DPX → uint16. 10bit 는 16bit 자리로 넓혀 담는다
    """

    FLOAT = {".exr"}

    @staticmethod
    def _tool(name: str) -> Optional[str]:
        from .media import FFmpegTool            # media 가 이 모듈을 읽으므로 여기서
        return FFmpegTool._search(name)

    @classmethod
    def pixel_format(cls, path: Path) -> str:
        probe = cls._tool("ffprobe")
        if not probe:
            return ""
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(path)],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
        return (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else ""

    @classmethod
    def read(cls, path: Path, keep_alpha: bool = True) -> Optional["np.ndarray"]:
        ffmpeg = cls._tool("ffmpeg")
        pix = cls.pixel_format(path)
        if not ffmpeg or not pix:
            return None
        has_alpha = pix.startswith(("gbrap", "rgba", "bgra", "argb", "abgr", "ya", "yuva"))
        alpha = keep_alpha and has_alpha
        floating = path.suffix.lower() in cls.FLOAT
        # half EXR 을 ffmpeg 안에서 float32 로 바꾸면 1.0 위가 잘린다 (ffmpeg 9 에서 재 봄: 2.5 → 1.0).
        # 그래서 half 는 half 그대로 받아 numpy 에서 넓힌다.
        half = floating and pix in ("gbrpf16le", "gbrapf16le")
        if floating:
            want = (pix if half else ("gbrapf32le" if has_alpha else "gbrpf32le"))
        else:
            want = "rgba64le" if alpha else "rgb48le"
        out = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", want, "-"], stdin=subprocess.DEVNULL, capture_output=True)
        size = cls._size(path)
        if out.returncode or not out.stdout or not size:
            return None
        width, height = size
        if floating:
            stored = 4 if has_alpha else 3
            planes = np.frombuffer(out.stdout, np.float16 if half else np.float32)
            planes = planes.reshape(stored, height, width).astype(np.float32)
            g, b, r = planes[0], planes[1], planes[2]
            layers = [b, g, r] + ([planes[3]] if alpha else [])
            return np.ascontiguousarray(np.stack(layers, axis=-1))
        channels = 4 if alpha else 3
        packed = np.frombuffer(out.stdout, np.uint16).reshape(height, width, channels)
        order = [2, 1, 0, 3] if alpha else [2, 1, 0]
        return np.ascontiguousarray(packed[..., order])

    @classmethod
    def _size(cls, path: Path) -> Optional[tuple[int, int]]:
        probe = cls._tool("ffprobe")
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
        try:
            w, h = (out.stdout or "").strip().splitlines()[0].split("x")[:2]
            return int(w), int(h)
        except (ValueError, IndexError):
            return None

    @classmethod
    def write(cls, path: Path, image: "np.ndarray", dpx_bits: int = 0) -> bool:
        ffmpeg = cls._tool("ffmpeg")
        if not ffmpeg or image.ndim != 3 or image.shape[2] not in (3, 4):
            return False
        height, width, channels = image.shape
        alpha = channels == 4
        if path.suffix.lower() in cls.FLOAT:
            data = image.astype(np.float32)
            layers = [data[..., 1], data[..., 0], data[..., 2]] + ([data[..., 3]] if alpha else [])
            raw, src = np.ascontiguousarray(np.stack(layers)).tobytes(), ("gbrapf32le" if alpha else "gbrpf32le")
            codec = ["-c:v", "exr", "-compression", "zip16", "-format", "half"]
        else:
            if image.dtype == np.uint8:
                data, src = image, ("rgba" if alpha else "rgb24")
            else:
                data = (np.clip(image, 0, 1) * 65535).astype(np.uint16) if image.dtype.kind == "f" else image
                src = "rgba64le" if alpha else "rgb48le"
            order = [2, 1, 0, 3] if alpha else [2, 1, 0]
            raw = np.ascontiguousarray(data[..., order]).tobytes()
            codec = ["-c:v", "dpx"]
            if dpx_bits == 10 and not alpha:
                codec += ["-pix_fmt", "gbrp10le"]
        path.parent.mkdir(parents=True, exist_ok=True)
        out = subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", src,
                              "-s", f"{width}x{height}", "-i", "-", *codec, "-frames:v", "1",
                              "-update", "1", str(path)], input=raw, capture_output=True)
        return out.returncode == 0 and path.exists()


class ImageIO:
    """한글 경로에서도 안전하게 읽고 쓴다 (OpenCV 는 비ASCII 경로에 약하다)."""

    VIA_FFMPEG = {".exr", ".dpx"}           # OpenCV 가 못 여는 합성용 형식

    @staticmethod
    def read(path: Path, flags: int = cv2.IMREAD_COLOR) -> Optional["np.ndarray"]:
        if path.suffix.lower() in ImageIO.VIA_FFMPEG:
            image = FFImage.read(path, keep_alpha=flags == cv2.IMREAD_UNCHANGED)
            if image is not None and flags == cv2.IMREAD_COLOR and image.dtype != np.uint8:
                # 색으로만 읽겠다는 쪽(미리보기 · 판별)은 8bit 를 기대한다
                # EXR 은 선형이라 그대로 내리면 새까맣다 — 보기용으로 sRGB 곡선을 씌운다
                if image.dtype.kind == "f":
                    lin = np.clip(image, 0, 1)
                    image = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055) * 255.0
                else:
                    image = image.astype(np.float32) * (255.0 / 65535.0)
                image = np.clip(image + 0.5, 0, 255).astype(np.uint8)
            return image
        try:
            buffer = np.frombuffer(path.read_bytes(), dtype=np.uint8)
        except OSError:
            return None
        if not buffer.size:
            return None
        try:
            return cv2.imdecode(buffer, flags)
        except cv2.error:
            return None

    @staticmethod
    def write(path: Path, image: "np.ndarray", params: Sequence[int] = (), dpx_bits: int = 0) -> bool:
        if path.suffix.lower() in ImageIO.VIA_FFMPEG:
            return FFImage.write(path, image, dpx_bits)
        ok, buffer = cv2.imencode(path.suffix, image, list(params))
        if not ok:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.tobytes())
        return True


class AsciiPathAlias:
    """Windows OpenCV 가 한글 경로를 못 여는 경우를 위한 임시 ASCII 경로."""

    def __init__(self, source: Path) -> None:
        self._source = source
        self._alias: Optional[Path] = None

    def create(self) -> Path:
        name = f"_tmp_{uuid.uuid4().hex[:12]}{self._source.suffix}"
        for candidate in (self._source.parent / name, Path(tempfile.gettempdir()) / name):
            try:
                os.link(self._source, candidate)
                self._alias = candidate
                return candidate
            except OSError:
                continue
        candidate = Path(tempfile.gettempdir()) / name
        shutil.copy2(self._source, candidate)
        self._alias = candidate
        return candidate

    def cleanup(self) -> None:
        if self._alias is not None:
            try:
                self._alias.unlink()
            except OSError:
                pass
            self._alias = None


class Scratch:
    """
    잠깐 보고 버릴 결과를 담는 자리.

    대조는 결과물이 아니라 확인하는 행위다. 볼 때마다 원본 폴더에 파일이 쌓이면
    정작 필요한 파일이 묻힌다. 그래서 임시 자리에 만들고, 프로그램이 닫힐 때 지운다.
    """

    _folders: list[Path] = []

    @classmethod
    def folder(cls, tag: str) -> Path:
        made = Path(tempfile.mkdtemp(prefix=f"framing_{tag}_"))
        cls._folders.append(made)
        return made

    @classmethod
    def clear(cls) -> None:
        while cls._folders:
            shutil.rmtree(cls._folders.pop(), ignore_errors=True)

    @classmethod
    def holding(cls) -> int:
        return len(cls._folders)


atexit.register(Scratch.clear)


def open_externally(path: Path) -> bool:
    """기본 프로그램으로 연다. 안 되면 조용히 False — 경로는 어차피 로그에 남는다."""
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))                    # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], **quiet)
        else:
            subprocess.Popen(["xdg-open", str(path)], **quiet)
        return True
    except Exception:
        return False


def link_or_copy(source: Path, destination: Path) -> Path:
    """
    같은 디스크면 하드링크로 즉시 끝난다. 28MB 짜리를 두 번 복사하고 앉아 있을 이유가 없다.
    임시 폴더가 다른 드라이브에 있으면 링크가 안 걸리니 그때만 복사한다.

    같은 자리에 두 번 만들 수 있어야 한다. 지난번에 걸어 둔 링크가 남아 있으면
    그건 원본과 같은 파일이라, 그대로 복사하려 들면 "같은 파일입니다" 로 멈춘다.
    (--keep 으로 같은 폴더에 두 번 대조하면 그래서 터졌다.)
    """
    if destination.exists():
        try:
            if destination.samefile(source):
                return destination            # 이미 같은 파일 — 할 일이 없다
        except OSError:
            pass
        try:
            destination.unlink()
        except OSError:
            pass
    try:
        os.link(source, destination)
        return destination
    except OSError:
        shutil.copyfile(source, destination)
        return destination


# ══════════════════════════════════════════════════ 프레임 소스

SEEK_BACKOFF = 48
TAIL_SCAN = 90


class Progress:
    @classmethod
    def tick(cls, done: int, total: int, every: int = 24) -> None:
        if EngineBridge.emit('progress', {'done': done, 'total': total}): return
        if total and (done % every == 0 or done == total):
            print(f"       {done}/{total}", end="\r" if done != total else "\n", flush=True)

    @classmethod
    def span(cls, fraction: float) -> None:
        """0~1 사이 진행률. ffmpeg 처럼 프레임 수를 모르는 작업이 쓴다."""
        fraction = max(0.0, min(1.0, fraction))
        if EngineBridge.emit('progress', {'done': fraction, 'total': 1}): return
        pct = int(fraction * 100)
        print(f"       {pct}%", end="\r" if pct < 100 else "\n", flush=True)


class FrameNumber:
    """
    프레임 번호를 사람에게 보여 줄 때 거치는 단 하나의 자리.

    **사람이 읽거나 적는 번호는 전부 1부터다.** 안에서 세는 번호(리스트 자리)는
    0부터이고, 그 둘이 만나는 곳이 여기다.

    왜 못 박았는가 — 예전에는 도구마다 달랐다. 뽑기와 검사와 컷 분할은 0부터,
    리타임 메모와 되돌리기는 1부터였다. 그래서 "33번 34번이 겹친다" 는 말을
    듣고도 그게 어느 쪽 번호인지 되물어야 했고, 한 칸 어긋난 채로 지나간 적도
    있다. 누크도 1부터 세고, 납품 규약의 240프레임도 1~240 이다. 사람 쪽에
    맞춘다.

    안에 담아 두는 기계용 기록(늘림메모의 origins 같은 것)은 0부터 그대로다 —
    되돌리기가 그걸로 고르기 때문이다. 대신 그런 기록은 자기 base 를 적어 둔다.
    """

    BASE = 1

    @classmethod
    def show(cls, index: int) -> int:
        """안에서 세던 번호(0부터) → 사람이 보는 번호(1부터)."""
        return int(index) + cls.BASE

    @classmethod
    def read(cls, said: int) -> int:
        """사람이 적은 번호(1부터) → 안에서 세는 번호(0부터)."""
        return int(said) - cls.BASE

    @classmethod
    def label(cls) -> str:
        return "1부터"

    @classmethod
    def listing(cls, indexes) -> list[int]:
        return [cls.show(i) for i in indexes]


@dataclass(frozen=True)
class FrameRate:
    """Bound a numeric FPS to a rational time base accepted by media containers."""
    numerator: int
    denominator: int

    @classmethod
    def from_value(cls, value):
        from fractions import Fraction
        try:
            number = float(value)
        except (ValueError, TypeError, OverflowError):
            raise FramingError('프레임레이트는 0보다 큰 유한한 숫자여야 합니다.')
        if not math.isfinite(number) or number <= 0:
            raise FramingError('프레임레이트는 0보다 큰 유한한 숫자여야 합니다.')
        fraction = Fraction(str(number)).limit_denominator(1000000)
        if not 0 < fraction.numerator <= 2147483647 or fraction.denominator > 2147483647:
            raise FramingError('미디어에 저장할 수 없는 프레임레이트입니다.')
        return cls(fraction.numerator, fraction.denominator)

    @staticmethod
    def frame_count(seconds, fps):
        # ffprobe stream durations are rounded to microseconds. Do not invent
        # a final frame because 176 is reported as 176.000008 at 24000/1001 fps.
        if not math.isfinite(seconds) or not math.isfinite(fps) or seconds < 0 or fps <= 0:
            raise FramingError('프레임 수를 계산할 시간 정보가 올바르지 않습니다.')
        return max(1, math.ceil(seconds * fps - max(1e-6, fps * 1e-6)))

    @property
    def text(self):
        return f'{self.numerator}/{self.denominator}'

    @property
    def value(self):
        return self.numerator / self.denominator

