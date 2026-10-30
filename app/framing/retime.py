"""리타임 계획 · 타임라인 · 메모 · 보간 엔진"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .errors import FramingError
from .env import cv2, np
from .util import Console, FrameRate, ImageIO
from .i18n import MessageCatalog
from .frames import LabelSnapper


@dataclass(frozen=True)
class RetimePlan:
    """몇 프레임을 몇 프레임으로 만들 것인가. 요청 방식이 뭐였든 여기로 모인다."""

    source: int
    target: int
    fps: float
    requested: int = 0          # 사용자가 실제로 말한 숫자 (잘라내기 전)

    MAX_FRAMES = 20000          # 계산 자체가 터지지 않게 두는 상한
    HARD_CAP = 6000             # 24fps 로 4분. 그보다 길면 리타임할 물건이 아니다
    STRETCH_CAP = 20.0          # 원본의 20배 넘게 늘리는 건 오타로 본다

    @classmethod
    def decide(cls, source: int, fps: float, frames: int = 0,
               speed: float = 0.0, seconds: float = 0.0) -> "RetimePlan":
        """프레임 수 · 배속 · 초 중 무엇으로 말하든 같은 계획으로 옮긴다."""
        fps = fps if fps and fps > 0 else 24.0
        if frames > 0:
            target = frames
        elif seconds > 0:
            target = int(round(seconds * fps))
        elif speed > 0:
            target = int(round(source / speed))   # 2배속 = 절반 길이
        else:
            target = source
        asked = target
        target = max(1, min(cls.MAX_FRAMES, target))
        return cls(source=max(1, source), target=target, fps=fps, requested=asked)

    @property
    def speed(self) -> float:
        return self.source / self.target

    @property
    def slowing(self) -> bool:
        return self.target > self.source

    @property
    def unchanged(self) -> bool:
        return self.target == self.source

    @property
    def summary(self) -> str:
        return (f"{self.source}프레임 ({self.source / self.fps:.2f}초) → "
                f"{self.target}프레임 ({self.target / self.fps:.2f}초)  ·  "
                f"{self.speed:.3f}배속 ({'느리게' if self.slowing else '빠르게'})")

    @property
    def refusal(self) -> str:
        """
        감당 못 할 요청은 조용히 잘라내지 말고 거절한다.

        999999 를 20000 으로 몰래 줄여 놓고 20분 동안 디스크를 채우는 게
        제일 나쁜 응답이다. 오타는 오타라고 말해줘야 고칠 수 있다.
        """
        if self.target <= self.HARD_CAP:
            return ""
        asked = self.requested or self.target
        return (f"{asked}프레임({asked / self.fps:.0f}초)은 한 번에 하기엔 깁니다 "
                f"— {self.HARD_CAP}프레임({self.HARD_CAP / self.fps:.0f}초)까지만 합니다.\n"
                "         --frames · --seconds · --speed 를 같이 주지는 않았는지, "
                "자릿수가 맞는지 확인해 보세요.")

    @property
    def suspicious(self) -> str:
        """거절할 정도는 아니지만 오타처럼 보이는 것. 막지는 않고 말만 한다."""
        if self.target <= self.source * self.STRETCH_CAP:
            return ""
        return (f"{self.source}프레임을 {self.target}프레임으로 "
                f"({self.target / self.source:.0f}배) 늘립니다 — 오타가 아닌지 확인하세요")


class SourceFrames:
    """Consecutive identity of decoded pixels, used only for the retime plan.

    Similar thumbnails cannot prove that two original frames are duplicates.
    Hold duration and noise-tolerant detection belong to FrameAudit.
    """
    def __init__(self, frames: Sequence["np.ndarray"]) -> None:
        self._origin = self._trace(frames)

    @property
    def origin(self) -> list[int]:
        return self._origin

    @property
    def distinct(self) -> int:
        """Number of consecutive groups, not a count of globally unique images."""
        return len(set(self._origin))

    @property
    def repeated(self) -> int:
        return len(self._origin) - self.distinct

    def shortfall(self, target: int) -> int:
        return max(0, target - self.distinct)

    @staticmethod
    def _trace(frames: Sequence["np.ndarray"]) -> list[int]:
        if not len(frames): return []
        out = [0]
        for previous, current in zip(frames, frames[1:]):
            same = (previous.shape == current.shape and previous.dtype == current.dtype
                    and np.array_equal(previous, current))
            out.append(out[-1] if same else out[-1] + 1)
        return out


class RetimeTimeline:
    """One frame layout shared by rendering, RIFE selection and undo records.

    Stretching assigns every source frame a unique output slot. Intermediate
    slots use a fractional position between those anchors. Shortening retains
    uniform resampling and cannot promise restoration of discarded frames.
    """
    def __init__(self, source, target):
        self.source, self.target = source, target
        self.anchors = ([round(i * (target - 1) / (source - 1)) for i in range(source)]
                        if source > 1 and target >= source else [0])
        self.origins = {slot: i for i, slot in enumerate(self.anchors)}
        self.positions = self._positions()

    def _positions(self):
        if self.target <= 1 or self.source <= 1:
            return [0.0] * self.target
        if self.target < self.source:
            return [i * (self.source - 1) / (self.target - 1) for i in range(self.target)]
        values = [0.0] * self.target
        for i, (start, end) in enumerate(zip(self.anchors, self.anchors[1:])):
            for slot in range(start, end):
                values[slot] = i + (slot - start) / (end - start)
        values[-1] = float(self.source - 1)
        return values

    def at(self, position):
        return self.positions[position]


class RetimeReport:
    """
    결과의 어디가 원본이고 어디가 새로 만든 자리인지.

    리타임을 돌리고 나면 "그래서 어디를 건드린 건데?" 를 알 수 없었다. 길이만
    알려 줘 봐야, 문제가 생겼을 때 어느 프레임을 봐야 하는지 알 길이 없다.
    자리 계산은 엔진과 똑같은 식(_source_index)을 쓰므로 실제와 어긋나지 않는다.
    """

    NEAR = 1e-6                 # 이만큼이면 원본 자리에 정확히 떨어진 것으로 본다

    # 방식마다 "손댄 자리" 의 뜻이 달라서 부르는 이름도 다르다
    LABELS = {
        "dup":  {"kept": "새 원본 위치", "made": "프레임 재사용"},
        "hold": {"kept": "원본 그대로", "made": "끝 홀드"},
        "":     {"kept": "원본 위치", "made": "보간 위치"},
    }

    def __init__(self, plan: "RetimePlan", engine_key: str = "",
                 origin: Optional[Sequence[int]] = None) -> None:
        self._plan = plan
        self._engine = engine_key
        self._timeline = RetimeTimeline(plan.source, plan.target)
        # origin 이 있으면 번호가 아니라 **그림**으로 센다. 없으면 예전처럼 번호로.
        self._origin = list(origin) if origin else []

    def at(self, position: int) -> float:
        if self._plan.target <= 1:
            return 0.0
        if self._engine == "hold":
            # 앞은 원본을 1:1 로 쓰고, 넘어가면 마지막 프레임에 머문다
            return float(min(position, self._plan.source - 1))
        if self._engine in ("motion", "blend") and self._plan.slowing:
            return self._timeline.at(position)
        return position * (self._plan.source - 1) / (self._plan.target - 1)

    def added(self) -> list[bool]:
        """
        결과 프레임마다 True = **늘리면서 생긴** 자리. 되돌리기가 쓰는 값이다.

        `marks()` 와 헷갈리기 쉬운데 묻는 것이 다르다.

          marks()  "결과에서 더블로 보이는 자리는 어디인가"   — 보고용
          added()  "늘리면서 새로 끼워 넣은 자리는 어디인가"  — 되돌리기용

        둘이 갈리는 경우가 실제로 있었다. **원본에 이미 똑같은 두 장이 붙어
        있으면** marks() 는 그 자리도 더블로 센다(그림으로 세니까 맞는 말이다).
        그런데 그 자리는 늘리면서 생긴 게 아니다. 246 → 265 는 19장이 늘었는데
        원본 1·2번이 같은 그림이라 marks() 가 20장을 표시했고, 그 메모로
        되돌리니 **원본 한 장이 같이 빠져서 245장**이 나왔다.

        그래서 여기서는 그림을 보지 않는다. 가리키는 원본 번호가 앞과 같은
        자리, 그것만 늘리면서 생긴 자리다.
        """
        if self._engine == "hold":
            edge = min(self._plan.source, self._plan.target)
            return [position >= edge for position in range(self._plan.target)]
        if self._engine != "dup":
            return self.marks()          # 겹치기·보간은 새로 그린 자리가 곧 생긴 자리
        out, previous = [], None
        for position in range(self._plan.target):
            index = min(self._plan.source - 1, int(round(self.at(position))))
            out.append(index == previous)
            previous = index
        return out

    def marks(self) -> list[bool]:
        """
        결과 프레임마다 True = 손댄 자리.

        무엇이 "손댄 자리" 인지는 방식마다 다르다.
          복제        이웃과 같은 그림을 한 번 더 쓴 자리 (새로 그린 픽셀은 없다)
          겹치기·보간 원본 자리에 정확히 떨어지지 않아 새로 만들어야 했던 자리

        **되돌리기는 이 값을 쓰면 안 된다** — added() 를 쓴다. 위 설명 참고.
        """
        out: list[bool] = []
        if self._plan.source == 1 and self._plan.slowing:
            return [i > 0 for i in range(self._plan.target)]
        if self._engine == "hold":
            # 원본이 끝난 지점부터가 붙잡은 자리. 새로 그린 픽셀은 하나도 없다
            edge = min(self._plan.source, self._plan.target)
            return [position >= edge for position in range(self._plan.target)]
        if self._engine == "dup":
            previous = None
            for position in range(self._plan.target):
                index = min(self._plan.source - 1, int(round(self.at(position))))
                # 번호가 달라도 그림이 같으면 그건 더블이다 — 한 번 늘렸던
                # 영상을 다시 줄일 때 여기서 조용히 틀렸다
                picture = self._origin[index] if index < len(self._origin) else index
                out.append(picture == previous)
                previous = picture
            return out
        for position in range(self._plan.target):
            exact = self.at(position)
            out.append(abs(exact - round(exact)) > self.NEAR)
        return out

    @property
    def made(self) -> int:
        return sum(self.marks())

    @property
    def kept(self) -> int:
        return self._plan.target - self.made

    def already_double(self) -> int:
        """원본에 이미 같은 그림으로 붙어 있던 장수. 늘리기가 만든 것이 아니다."""
        return sum(self.marks()) - sum(self.added())

    def where(self, limit: int = 6) -> str:
        """
        늘리면서 생긴 자리를 사람 말로. 규칙적이면 규칙으로, 아니면 번호 몇 개로.

        메모·되돌리기와 같은 값(added)을 말한다. 화면이 20장이라 하고 메모가
        19장이라 적으면, 둘 중 어느 쪽이 맞는지 알 방법이 없다.
        """
        marks = self.added()
        spots = [i for i, fresh in enumerate(marks) if fresh]
        if not spots:
            return "새로 만든 프레임 없음 — 원본 프레임만 골라 썼습니다"
        if self._engine == "hold":
            return (f"{spots[0] + 1}번째부터 끝까지 {len(spots)}장 "
                    "— 마지막 프레임을 붙잡았습니다")
        if len(spots) >= len(marks) * 0.9:
            # 원본 사이를 다시 잘게 나누면 처음·끝 말고는 성한 자리가 없다.
            # 그게 정상이지만, 모르고 보면 고장으로 읽힌다.
            return (f"처음과 끝을 뺀 거의 전부 ({len(spots)}/{len(marks)}장) — "
                    f"원본 {self._plan.source}장 사이를 {self._plan.target}장으로 다시 "
                    "나누느라 눈금이 안 맞습니다")
        gaps = {b - a for a, b in zip(spots, spots[1:])}
        if len(gaps) == 1 and spots[0] < 4:
            step = gaps.pop()
            first = spots[0] + 1                   # 사람이 세는 번호는 1부터
            return f"{first}번째부터 {step}장마다 (총 {len(spots)}장)"
        head = ", ".join(str(i + 1) for i in spots[:limit])
        return f"{head}{' …' if len(spots) > limit else ''} 번째 (총 {len(spots)}장)"

    def strip(self, width: int = 48) -> str:
        """한 줄 그림. ▉ 는 원본 그대로, ░ 는 새로 만든 자리."""
        marks = self.marks()
        if not marks:
            return ""
        size = min(width, len(marks))
        cells = []
        for slot in range(size):
            start = slot * len(marks) // size
            end = max(start + 1, (slot + 1) * len(marks) // size)
            cells.append("░" if any(marks[start:end]) else "▉")
        return "".join(cells)

    @property
    def plan(self) -> "RetimePlan":
        return self._plan

    def payload(self) -> dict:
        """화면이 띠를 그릴 수 있게 넘기는 값."""
        return {"source": self._plan.source, "target": self._plan.target,
                "kept": self.kept, "made": self.made, "engine": self._engine,
                "evidence": "render-plan",
                "labels": self.LABELS.get(self._engine, self.LABELS[""]),
                "note": "프레임 배치 기록입니다. 보간 위치·재사용 횟수는 홀드 검출 결과가 아닙니다.",
                "speed": round(self._plan.speed, 4),
                "marks": self.marks()[:2000], "where": self.where()}


class RetimeNote:
    """
    같은 이야기를, 결과 옆에 놓고 가는 쪽지.

    지도는 화면에 뜨지만 창을 닫으면 사라진다. 그런데 리타임한 컷은 며칠 뒤에
    다시 열어 본다 — "이거 몇 배속이었지", "어디가 새로 그린 자리였지". 그때
    물어볼 데가 없었다. 그래서 원하면 결과 옆에 메모장 한 장을 놓는다.

    세는 일은 RetimeReport 가 이미 다 해 놨다. 여기는 그걸 사람이 읽는 글로
    옮기기만 한다 — 숫자를 다시 세면 지도와 쪽지가 어긋난다.
    """

    SUFFIX = "_리타임메모.txt"
    PER_LINE = 12               # 한 줄에 번호 열두 개 — 창에 붙여넣기 좋은 폭

    def __init__(self, report: RetimeReport, source: Path,
                 engine_title: str = "") -> None:
        self._report = report
        self._source = Path(source)
        self._title = engine_title or report.payload().get("engine", "")

    def spot(self, folder: Path) -> Path:
        return Path(folder) / (self._source.stem + self.SUFFIX)

    def text(self) -> str:
        """
        짧게. 며칠 뒤에 열어 보는 사람이 알고 싶은 건 하나다 — **어디가 새로
        생긴 자리인가.**

        예전에는 배속·띠그림·주의문까지 열댓 줄을 적었다. 정작 번호는 60개에서
        잘랐다. 읽을 것은 많고 쓸 것은 잘려 있었다. 지금은 거꾸로다: 설명을
        줄이고 번호를 **한 장도 빠짐없이** 적는다. 그래야 이 메모장을 되돌리기에
        그대로 끌어다 놓을 수 있다 (RetimeNoteText 가 이걸 다시 읽는다).
        """
        plan = self._report.plan
        made = self._report.payload()['labels']['made']
        spots = self.frames()
        lines = [
            "Framing 리타임 메모",
            "",
            f"원본   {self._source.name}",
            f"적은 때 {time.strftime('%Y-%m-%d %H:%M')}",
            f"길이   {plan.source} → {plan.target}프레임",
            f"방식   {self._title}",
            f"FRAMING_TIMELINE source={plan.source} output={plan.target} restorable={int(plan.target-len(spots)==plan.source)}",
            "",
        ]
        if not spots:
            lines.append("새로 생긴 프레임 없음 — 원본 프레임만 골라 썼습니다")
            return "\n".join(lines) + "\n"
        if plan.target - len(spots) != plan.source:
            lines.append('원본 프레임이 모두 남아 있지 않아 이 메모로 원본 길이를 복원할 수 없습니다.')
        lines.append(f"{made} {len(spots)}장 · 프레임 번호 (결과 기준, 1부터)")
        lines += self._number_lines(spots)
        return "\n".join(lines) + "\n"

    def frames(self) -> list[int]:
        """
        늘리면서 생긴 자리 — 사람이 세는 1부터.

        `marks()` 가 아니라 `added()` 다. 이 메모장은 읽는 종이가 아니라
        되돌리기에 도로 넣는 종이라서, 한 장이라도 더 적히면 원본이 깎인다.
        """
        return [i + 1 for i, fresh in enumerate(self._report.added()) if fresh]

    @classmethod
    def _number_lines(cls, spots: Sequence[int]) -> list[str]:
        """번호만 있는 줄로 적는다 — 되돌리기가 이 줄만 골라 읽는다."""
        return [", ".join(str(n) for n in spots[at:at + cls.PER_LINE])
                for at in range(0, len(spots), cls.PER_LINE)]

    def write(self, folder: Path, language='kr') -> Optional[Path]:
        """못 적어도 리타임을 실패로 만들지 않는다 — 쪽지는 곁다리다."""
        spot = self.spot(folder) if language != 'en' else Path(folder) / (self._source.stem + '_retime-notes.txt')
        try:
            spot.parent.mkdir(parents=True, exist_ok=True)
            spot.write_text(MessageCatalog().text(self.text(), language), encoding="utf-8")
        except OSError as exc:
            Console.warn(f"메모를 적지 못했습니다: {exc}")
            return None
        return spot


class RetimeEngine(ABC):
    """
    프레임 수를 바꾸는 한 가지 방법.

    셋 다 결과 장수는 정확히 같다. 다른 건 "없는 프레임을 어떻게 채우는가" 뿐이다.
    """

    KEY = ""
    TITLE = ""
    BLURB = ""
    NEEDS_FFMPEG = False

    def __init__(self, plan: RetimePlan, snapper: Optional[LabelSnapper] = None,
                 abort: Optional[Callable[[], None]] = None) -> None:
        self._plan = plan
        self._timeline = RetimeTimeline(plan.source, plan.target)
        self._snapper = snapper
        self._abort = abort or (lambda: None)

    @abstractmethod
    def render(self, frames: Sequence["np.ndarray"],
               write: Callable[[int, "np.ndarray"], None]) -> int: ...

    def _finish(self, frame: "np.ndarray") -> "np.ndarray":
        return self._snapper.apply(frame) if self._snapper is not None else frame

    def _source_index(self, position: int) -> float:
        """결과 몇 번째 프레임이 원본의 몇 번째 자리에 해당하는가 (소수점 포함)."""
        if self._plan.target <= 1:
            return 0.0
        if self.KEY in ('motion', 'blend') and self._plan.slowing:
            return self._timeline.at(position)
        return position * (self._plan.source - 1) / (self._plan.target - 1)


class DuplicateEngine(RetimeEngine):
    """가장 가까운 원본 프레임을 그대로 쓴다. 픽셀 연산이 없어 매트가 100% 남는다."""

    KEY = "dup"
    TITLE = "복제"
    BLURB = "원본 프레임을 그대로 골라 씁니다. 색이 절대 섞이지 않습니다"

    def render(self, frames, write) -> int:
        for position in range(self._plan.target):
            self._abort()
            index = min(len(frames) - 1, int(round(self._source_index(position))))
            write(position, frames[index])      # 라벨 복원조차 필요 없다 — 원본이니까
        return self._plan.target


class TailHoldEngine(RetimeEngine):
    """
    앞은 원본을 1:1 로 쓰고, 모자란 뒤는 마지막 프레임을 붙잡는다.

    꼬리가 이미 멈춰 있거나 타임라인 길이만 맞추면 되는 자리에서는 이쪽이
    제일 깨끗하다. 새로 그리는 픽셀이 하나도 없고, 원본 구간의 타이밍이
    한 프레임도 밀리지 않는다. 복제는 늘린 만큼을 전 구간에 흩뿌리지만
    홀드는 꼬리 한 군데에만 모아 둔다.

    줄이는 일에는 쓸 수 없다 — 붙잡을 자리가 없다.
    """

    KEY = "hold"
    TITLE = "끝 홀드"
    BLURB = "원본을 1:1 로 쓰고 모자란 길이만 마지막 프레임으로 채웁니다"

    def _source_index(self, position: int) -> float:
        return float(min(position, self._plan.source - 1))

    def render(self, frames, write) -> int:
        last = len(frames) - 1
        for position in range(self._plan.target):
            self._abort()
            write(position, frames[min(position, last)])
        return self._plan.target


class BlendEngine(RetimeEngine):
    """이웃한 두 프레임을 섞는다. 부드럽지만 겹쳐 보인다 (디졸브)."""

    KEY = "blend"
    TITLE = "겹치기"
    BLURB = "앞뒤 프레임을 비율대로 섞습니다. 잔상이 남지만 실패하지 않습니다"

    def render(self, frames, write) -> int:
        for position in range(self._plan.target):
            self._abort()
            exact = self._source_index(position)
            first = int(np.floor(exact))
            second = min(len(frames) - 1, first + 1)
            weight = float(exact - first)
            if weight < 1e-6 or first == second:
                write(position, frames[first])
                continue
            mixed = cv2.addWeighted(frames[first], 1.0 - weight,
                                    frames[second], weight, 0.0)
            write(position, self._finish(mixed))
        return self._plan.target


class MotionEngine(RetimeEngine):
    """
    ffmpeg 의 모션 보간. 물체가 어디로 갔는지 추정해 없던 프레임을 그린다.

    잘 되면 제일 매끄럽고, 안 되면 물체가 녹거나 찢어진다.
    빠른 움직임 · 가림 · 모션블러에 약하다. 그래서 결과를 꼭 보고 판단해야 한다.
    """

    KEY = "motion"
    TITLE = "모션 보간"
    BLURB = "움직임을 추정해 없던 프레임을 그립니다. 제일 부드럽지만 찢어질 수 있습니다"
    NEEDS_FFMPEG = True

    FILTER = "minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"

    @staticmethod
    def supports(frame) -> bool:
        # A 16-bit PNG output cannot recover depth lost inside minterpolate.
        # Grayscale must also retain its channel layout throughout a sequence.
        return frame.dtype == np.uint8 and frame.ndim == 3 and frame.shape[2] in (3, 4)

    @staticmethod
    def _still(deep: bool, alpha: bool) -> str:
        """소스가 가진 것을 중간 그림에서 잃지 않게. 여기서 깎이면 되돌릴 수 없다."""
        if alpha:
            return "rgba64be" if deep else "rgba"
        return "rgb48be" if deep else "rgb24"

    def __init__(self, plan, snapper=None, abort=None, ff=None, workspace=None):
        super().__init__(plan, snapper, abort)
        self._ff = ff
        self._workspace = workspace

    def render(self, frames, write) -> int:
        if len(frames) < 2 or self._plan.target <= 1 or self._plan.unchanged:
            for position in range(self._plan.target):
                self._abort()
                write(position, frames[int(round(self._source_index(position)))])
            return self._plan.target
        if not self.supports(frames[0]):
            raise FramingError('모션 보간은 8비트 컬러 입력을 지원합니다. 계조와 채널을 유지하려면 겹치기를 사용하세요.')
        stage = self._workspace / "_in"
        stage.mkdir(parents=True, exist_ok=True)
        first = frames[0]
        deep = first.dtype == np.uint16
        alpha = first.ndim == 3 and first.shape[2] == 4
        if alpha:
            Console.info("알파는 보간해도 가장자리가 무릅니다 — 결과를 꼭 확인하세요")
        for number, frame in enumerate(frames, 1):
            self._abort()
            if not ImageIO.write(stage / f"s.{number:06d}.png", frame):
                raise FramingError('보간 입력 프레임을 저장하지 못했습니다.')
        made = self._interpolate(stage, deep, alpha)
        if not made:
            raise FramingError("모션 보간에 실패했습니다.")
        written = 0
        for position in range(self._plan.target):
            self._abort()
            exact = self._source_index(position)
            if abs(exact - round(exact)) < 1e-6:
                write(position, frames[int(round(exact))])
                written += 1
                continue
            if position < len(made):
                frame = ImageIO.read(made[position], cv2.IMREAD_UNCHANGED)
            else:
                frame = None
            if frame is None:
                raise FramingError(f'보간 프레임 {position + 1}을 만들지 못했습니다. 다시 시도해 주세요.')
            else:
                write(position, self._finish(frame))
            written += 1
        return written

    def _interpolate(self, stage: Path, deep: bool, alpha: bool) -> list[Path]:
        out = self._workspace / "_interp"
        out.mkdir(parents=True, exist_ok=True)
        fps = self._plan.fps
        # 먼저 시간을 늘렸다 줄인 뒤, 그 위에서 목표 fps 로 다시 그린다.
        ratio = (self._plan.target - 1) / max(1, self._plan.source - 1)
        # Fine timebase avoids rounding fractional source positions to the input FPS.
        # Tail context lets minterpolate emit the final interval instead of dropping it.
        stretch = f"settb=AVTB,setpts=PTS*{ratio:.12f}"
        if self._plan.slowing:
            # ffmpeg round() rounds positive halves upward; implement ties-to-even
            # with integer modulo so input anchors match the shared layout exactly.
            numerator = f'N*{self._plan.target - 1}'
            denominator = self._plan.source - 1
            base = f'floor(({numerator})/{denominator})'
            remainder = f'mod({numerator},{denominator})'
            expression = f'{base}+if(gt(2*{remainder},{denominator}),1,if(eq(2*{remainder},{denominator}),mod({base},2),0))'
            stretch = f"settb=AVTB,setpts='({expression})/({FrameRate.from_value(fps).text})/TB'"
        chain = (f"{stretch},tpad=stop_mode=clone:stop_duration={max(2/fps, 2*ratio/fps):.9f},"
                 f"{self.FILTER.format(fps=FrameRate.from_value(fps).text)}")
        code = self._ff.run(["-y", "-framerate", FrameRate.from_value(fps).text,
                             "-start_number", "1", "-i", str(stage / "s.%06d.png"),
                             "-vf", chain, "-frames:v", str(self._plan.target),
                             "-c:v", "png", "-pix_fmt", self._still(deep, alpha),
                             str(out / "i.%06d.png")], quiet=True)
        made = sorted(out.glob("i.*.png"))
        if code != 0:
            return []
        return made


