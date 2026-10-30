"""뽑을 지점 · 작업 바탕 클래스 · 앱 아이콘"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from typing import TYPE_CHECKING

from .errors import FramingError
from .options import JobResult, Options
from .env import IMAGE_EXTENSIONS
from .util import Console, FrameNumber, ImageIO, unique_path
from .media import FFmpegTool, ProbeInfo, VideoSource
from .frames import ColorProfile, LevelInspector

if TYPE_CHECKING:   # 타입 표기에만 쓰는 이름
    from .env import np


@dataclass(frozen=True)
class FramePick:
    """뽑을 지점 하나. label 이 파일 이름에 들어간다."""
    label: str
    index: int
    prefer_tail: bool = False        # 마지막 프레임이면 별도 경로로 잡는다


class PickSpec:
    """
    "어디를 뽑을까" 를 적은 문자열을 실제 프레임 번호로 옮긴다.

    쓸 수 있는 표기 (쉼표나 공백으로 구분)
      first / middle / last      처음 · 중간 · 마지막
      1  120  999                프레임 번호 (1부터 — 사람이 세는 번호)
      -1  -30                    뒤에서부터. -1 이 마지막
      25%  50%                   길이 기준 백분율
      3s  1:23  0:05.5           시간. 콜론이나 s 가 있어야 시간으로 본다
    """

    ALIASES = {
        'first': 'first', 'start': 'first', '처음': 'first', '첫': 'first',
        'middle': 'middle', 'mid': 'middle', '중간': 'middle',
        'last': 'last', 'end': 'last', '끝': 'last', '마지막': 'last',
    }
    DEFAULT_TEXT = 'first,middle,last'
    MAX_PICKS = 200                  # 이보다 많으면 Sequence 를 쓰는 게 맞다

    def __init__(self, text: str = '', every: int = 0, count: int = 0) -> None:
        self._text = (text or '').strip()
        self._every = max(0, int(every or 0))
        self._count = max(0, int(count or 0))

    @property
    def is_default(self) -> bool:
        return not self._text and not self._every and not self._count

    def summary(self) -> str:
        if self._text:
            return self._text
        if self._every:
            return f'매 {self._every}프레임'
        if self._count:
            return f'균등 {self._count}장'
        return self.DEFAULT_TEXT

    # --- 풀어내기 ---------------------------------------------------------

    def resolve(self, total: int, fps: float) -> list[FramePick]:
        if total <= 0:
            raise FramingError('프레임 수를 알 수 없습니다.')
        raw = self._raw_picks(total, fps)

        seen: dict[int, FramePick] = {}
        for pick in raw:
            index = max(0, min(total - 1, pick.index))
            if index != pick.index:
                Console.warn(f'{pick.label} 는 범위를 벗어나 {FrameNumber.show(index)} 로 맞췄습니다')
            fixed = FramePick(pick.label, index, pick.prefer_tail or index == total - 1)
            seen.setdefault(index, fixed)

        picks = [seen[i] for i in sorted(seen)]
        if len(picks) > self.MAX_PICKS:
            Console.warn(f'{len(picks)}장은 너무 많아 앞에서 {self.MAX_PICKS}장만 뽑습니다. '
                         '전부 필요하면 Sequence 를 쓰세요.')
            picks = picks[:self.MAX_PICKS]
        if not picks:
            raise FramingError('뽑을 지점이 하나도 없습니다.')
        return picks

    def _raw_picks(self, total: int, fps: float) -> list[FramePick]:
        if self._text:
            return [self._parse(token, total, fps)
                    for token in re.split(r'[,\s]+', self._text) if token]
        if self._every:
            return [FramePick(f'f{i:05d}', i) for i in range(0, total, self._every)]
        if self._count:
            return self._even_spread(self._count, total)
        return [self._parse(t, total, fps) for t in self.DEFAULT_TEXT.split(',')]

    @staticmethod
    def _even_spread(count: int, total: int) -> list[FramePick]:
        if count <= 1:
            return [FramePick('middle', total // 2)]
        step = (total - 1) / (count - 1)
        return [FramePick(f'f{round(i * step):05d}', round(i * step)) for i in range(count)]

    def _parse(self, token: str, total: int, fps: float) -> FramePick:
        raw = token.strip().lower()
        if not raw:
            raise FramingError('빈 항목이 있습니다.')

        alias = self.ALIASES.get(raw)
        if alias == 'first':
            return FramePick('first', 0)
        if alias == 'middle':
            return FramePick('middle', total // 2)
        if alias == 'last':
            return FramePick('last', total - 1, True)

        if raw.endswith('%'):
            try:
                pct = float(raw[:-1])
            except ValueError:
                raise FramingError(f'백분율을 못 읽었습니다: {token}')
            index = round((total - 1) * pct / 100)
            return FramePick(f'p{int(round(pct)):03d}', index)

        seconds = self._as_seconds(raw)
        if seconds is not None:
            if fps <= 0:
                raise FramingError('프레임레이트를 몰라 시간으로 지정할 수 없습니다.')
            return FramePick(f't{raw.replace(":", "-").replace(".", "_")}',
                             round(seconds * fps))

        try:
            number = int(raw)
        except ValueError:
            raise FramingError(
                f'무슨 뜻인지 모르겠습니다: {token}\n'
                '         first / middle / last, 프레임 번호, 25%, 1:23 같은 식으로 적으세요.')
        if number < 0:
            index = total + number
            return FramePick(f'f{max(0, index):05d}', index, number == -1)
        # 사람이 적는 번호는 1부터. 파일 이름에도 적은 그대로 남긴다 —
        # "8번을 뽑았는데 파일은 f00007" 이면 그 자리에서 헷갈린다.
        if number < FrameNumber.BASE:
            raise FramingError(
                f'프레임 번호는 {FrameNumber.BASE}부터입니다: {token}\n'
                '         뒤에서부터 세려면 -1 처럼 음수로 적으세요.')
        return FramePick(f'f{number:05d}', FrameNumber.read(number))

    @staticmethod
    def _as_seconds(raw: str) -> Optional[float]:
        """1:23, 0:05.5, 12s 를 초로. 시간 표기가 아니면 None."""
        if raw.endswith('s') and raw[:-1].replace('.', '', 1).isdigit():
            return float(raw[:-1])
        if ':' not in raw:
            return None
        parts = raw.split(':')
        if len(parts) > 3:
            return None
        try:
            values = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for value in values:
            seconds = seconds * 60 + value
        return seconds


class Job(ABC):
    """작업 하나. 이름·설명·입력 종류·필요 도구를 스스로 안다."""

    KEY = ""
    TITLE = ""
    HINT = ""
    NOTE = ""                        # 고르기 전에 알아둘 것 (메뉴에서 보여줌)
    ALIASES: tuple[str, ...] = ()
    NEEDS_FFMPEG = False
    ACCEPTS_FOLDER = False           # 시퀀스 폴더를 입력으로 받는가
    ACCEPTS_IMAGE = False            # 낱장 이미지도 대상이 되는가 (Compare)
    PAIRED = False                   # 한 번에 두 개를 짝지어 받는가 (Compare)
    GROUPED = False                  # 전부를 한 번에 받는가 (Rename — 번호가 전체를 관통한다)

    def __init__(self, options: Options, ff: FFmpegTool) -> None:
        self._opt = options
        self._ff = ff
        self._colors: dict[str, ColorProfile] = {}    # 소스마다 한 번만 판단한다
        # 앱에서 중단 버튼을 누르면 이걸 갈아끼운다. CLI 에서는 늘 False.
        self.should_stop: Callable[[], bool] = lambda: False
        # 앱이 있으면 결과를 창 안에서 보여준다. 없으면 None — 그때만 밖으로 연다.
        self.show_inline: Optional[Callable[[str, dict], bool]] = None

    # --- 색 규격 ------------------------------------------------------------

    def _color(self, target: Path, info: Optional[ProbeInfo] = None,
               sample: Optional["np.ndarray"] = None) -> ColorProfile:
        """
        이 소스의 색 규격. 작업마다 다시 판단하지 않는다 — 그러면 서로 어긋난다.

        표기를 그대로 믿되, 실제 픽셀이 표기와 어긋나면 픽셀 쪽을 믿는다.
        (전체범위라고 적어 놓고 16~235 만 쓰는 파일이 실제로 흔하다.)
        """
        key = str(target)
        if key in self._colors:
            return self._colors[key]
        if info is None and self._ff.find("ffprobe"):
            info = self._ff.probe(target)
        profile = ColorProfile.from_probe(info)
        if (self._opt.levels or "auto").lower() != "keep":
            if sample is None and profile.crange == "pc" and not target.is_dir():
                sample = self._peek(target)     # 의심스러울 때만 한 장 열어 본다
            if sample is not None:
                profile = profile.corrected(LevelInspector.read(sample, profile.crange))
        self._colors[key] = profile
        return profile

    @staticmethod
    def _peek(target: Path) -> Optional["np.ndarray"]:
        """첫 그림 한 장. 표기가 'pc' 인 파일에서만 부른다 — 그 외엔 볼 이유가 없다."""
        try:
            if target.suffix.lower() in IMAGE_EXTENSIONS:
                return ImageIO.read(target)
            with VideoSource(target) as source:
                frame, _index = source.read_at(0)
                return frame
        except Exception:
            return None

    def _announce_color(self, profile: ColorProfile) -> None:
        Console.info(f"색 규격  {profile.summary}")
        if profile.retagged:
            Console.warn("전체범위라고 적혀 있는데 실제로는 눌려 있습니다 "
                         "— 제한범위로 바로잡아 읽고, 결과에도 그렇게 적어 둡니다")

    def _abort_if_asked(self) -> None:
        if self.should_stop():
            raise FramingError('중단했습니다.')

    @abstractmethod
    def run(self, target: Path) -> JobResult: ...

    # --- 도우미 -----------------------------------------------------------

    def _output_dir(self, target: Path, suffix: str) -> Path:
        base = target if target.is_dir() else target.with_suffix("")
        folder = self._opt.out_dir or Path(str(base) + suffix)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _output_file(self, target: Path, suffix: str, ext: str) -> Path:
        folder = self._opt.out_dir or target.parent
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{target.stem}{suffix}{ext}"
        if dest.resolve() == target.resolve():
            dest = folder / f"{target.stem}{suffix}_1{ext}"
        return dest if self._opt.overwrite else unique_path(dest)


# 창·바로가기 아이콘은 app/icon/framing.ico (tools/make_logo.py 가 만든다)
APP_ICON = Path(__file__).resolve().parent.parent / "icon" / "framing.ico"


def app_icon_bytes() -> Optional[bytes]:
    """창·바로가기에 쓸 아이콘(ico). 없으면 None — 아이콘은 안 건드린다."""
    try:
        return APP_ICON.read_bytes() if APP_ICON.is_file() else None
    except OSError:
        return None

