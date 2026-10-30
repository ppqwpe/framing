"""뽑기 작업 · 결과 형식(Delivery)"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Sequence

from .errors import FramingError
from .options import JobResult
from .env import np
from .util import Console, FrameNumber, Progress, hms, human
from .media import VideoSource
from .frames import ColorProfile, LevelExpander, LevelInspector, PixelPlan, PreciseGrab, build_writers
from .job import FramePick, Job, PickSpec
from .sequence import OutputFolder


class ExtractJob(Job):
    """영상에서 그림을 꺼낸다 — 몇 장이든, 전부든.

    예전에는 '몇 장'(Key Frames)과 '전부'(Sequence)가 딴 작업이었다.
    쓰는 사람 눈에는 같은 일의 양 끝일 뿐이라 하나로 합쳤다. 어디를 뽑을지만 다르다.
    """

    KEY = "extract"
    ALIASES = ("keyframes", "frames", "key", "sequence", "seq")
    TITLE = "뽑기"
    HINT = "원하는 지점 몇 장, 또는 전부를 시퀀스로"
    NOTE = """
    어디를 뽑을지만 정하면 됩니다.
      첫 · 중간 · 마지막 (기본) / 균등하게 N장 / N프레임마다 / 직접 찍기 / 전부

    직접 찍기 (--at, 쉼표로 여러 개)
      first middle last · 프레임 번호(1부터) · -1(끝에서부터) · 25% · 1:23 · 3s
      all 이라고 쓰면 전부 — 시퀀스로 풉니다

    몇 장만 뽑을 때는 PNG · JPG(4:4:4) 로, 전부 풀 때는 PNG · JPG · EXR · DPX 로.
    무손실 시퀀스는 원본 mp4 의 수십~수백 배가 됩니다. 1분 1080p ≈ 5~15GB.

    색이 달라 보이지 않게 하는 일 세 가지는 몇 장이든 전부든 똑같이 합니다.
      · JPG 는 색을 줄이지 않는 4:4:4 로
      · 10bit · 알파 소스는 원본 깊이 그대로 (16bit PNG, 알파 유지)
      · 밝기 범위 표기가 틀린 영상은 읽는 단계에서 바로잡음 (--levels auto|full|keep)

    화면과 여전히 다르면 보는 프로그램의 감마 차이입니다 (영상 2.4 vs sRGB 2.2).
    그건 파일을 고쳐서 맞추면 안 됩니다 — 다른 모든 도구와 어긋나게 됩니다."""

    SEQUENCE_ENCODERS = {
        "png": ["-c:v", "png"],
        "jpg": ["-c:v", "mjpeg", "-q:v", "2"],
        "exr": ["-c:v", "exr", "-compression", "zip16", "-format", "half",
                "-pix_fmt", "gbrpf32le"],
        "dpx": ["-c:v", "dpx"],
    }

    @property
    def wants_all(self) -> bool:
        return (self._opt.at or "").strip().lower() in ("all", "전부")

    def run(self, target: Path) -> JobResult:
        if self.wants_all:
            return self._extract_all(target)
        return self._extract_picks(target)

    # --- 전부 (시퀀스) ------------------------------------------------------

    def _extract_all(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        self._ff.ensure()
        fmt = self._opt.frame_format
        if fmt not in self.SEQUENCE_ENCODERS:
            fmt = "png"                     # both 는 몇 장 뽑을 때의 이야기다
        bundle = OutputFolder(target, self._opt.out_dir, '_sequence', 'sequence.json')
        folder = bundle.path
        pattern = folder / f"{target.stem}.%04d.{fmt}"
        info = self._ff.probe(target)
        profile = self._color(target, info)
        if info:
            result.message = (f"{info.width}x{info.height}, {info.fps:.3f}fps, "
                              f"{hms(info.duration)}, 약 {info.frames}프레임 → {fmt.upper()}")
            self._announce_color(profile)

        code = self._ff.run(["-y", "-i", str(target),
                             *ColorProfile.join(profile.read_filter()),
                             *self.SEQUENCE_ENCODERS[fmt], '-fps_mode', 'passthrough',
                             '-start_number', '1', "-an", str(pattern)],
                            total_seconds=info.duration if info else 0.0)
        made = sorted(p for p in folder.iterdir() if p.suffix.lower() == f".{fmt}")
        self._abort_if_asked()
        if code != 0 or not made:
            raise FramingError(f"전체 프레임 저장 미완료 (ffmpeg {code}). 폴더: {folder}")
        bundle.write(status='complete', frames=len(made), fps=info.fps if info else 0,
                     format=fmt, fileNumberBase=1, sourceFrameBase=0,
                     timing='One file per decoded frame. VFR frame intervals are not represented by sequence FPS.')
        result.outputs = made
        result.output_dir = folder
        result.next_paths = [str(folder)]
        result.ok = bool(made)
        result.message += f"  ·  {len(made)}장, {human(sum(p.stat().st_size for p in made))}"
        return result

    # --- 몇 장 --------------------------------------------------------------

    @property
    def spec(self) -> PickSpec:
        return PickSpec(self._opt.at, self._opt.every, self._opt.count)

    def _extract_picks(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        fmt = self._opt.frame_format
        if fmt not in ("png", "jpg", "both"):
            fmt = "png"                     # exr · dpx 는 전부 풀 때의 형식이다
        writers = build_writers(fmt, self._opt.jpeg_quality)
        spec = self.spec
        with VideoSource(target) as source:
            picks = spec.resolve(source.frame_count, source.fps)
            grabbed = self._grab(source, picks)
            width, height, fps, total = (*source.resolution, source.fps,
                                         source.frame_count)
        folder = self._output_dir(target, "_keyframes")

        profile = self._color(target, sample=grabbed[0][1])
        self._announce_color(profile)
        grabbed, exact = self._upgrade(target, grabbed, profile)
        fix = self._level_plan(profile, grabbed[0][1], already_fixed=exact)

        for order, (pick, frame, index) in enumerate(grabbed, 1):
            if fix:
                frame = LevelExpander.apply(frame)
            stem = folder / f"{target.stem}_{order:02d}_{pick.label}"
            result.outputs += [w.write(frame, stem) for w in writers]
        marks = " / ".join(f"{p.label}={FrameNumber.show(i)}" for p, _f, i in grabbed)
        if len(marks) > 90:
            marks = f"{len(grabbed)}지점: {grabbed[0][2]} … {grabbed[-1][2]}"
        result.message = (f"{width}x{height}, {fps:.3f}fps, "
                          f"총 {total}프레임  ·  {marks}")
        result.output_dir = folder
        result.ok = bool(result.outputs)
        return result

    def _upgrade(self, target: Path, grabbed: list, profile: ColorProfile) -> tuple[list, bool]:
        """
        소스가 8bit 4:2:0 보다 위에 있으면 ffmpeg 으로 다시 뽑는다.

        번호는 이미 세어 두었으니 그대로 쓰고, 그림만 원본 깊이로 바꿔 담는다.
        실패하면 방금 OpenCV 로 뽑은 걸 그대로 쓴다 — 못 뽑는 것보다 낫다.
        """
        if not profile.deep and not profile.retagged:
            return grabbed, False
        grabber = PreciseGrab(self._ff, profile)
        if not grabber.available():
            return grabbed, False
        deep = grabber.fetch(target, [index for _p, _f, index in grabbed])
        if not deep:
            Console.info("원본 깊이로 다시 뽑기에 실패해 8bit 로 갑니다")
            return grabbed, False
        upgraded = [(pick, deep.get(index, frame), index)
                    for pick, frame, index in grabbed]
        depth = "16bit" if upgraded[0][1].dtype == np.uint16 else "8bit"
        alpha = " · 알파 유지" if upgraded[0][1].ndim == 3 and upgraded[0][1].shape[2] == 4 else ""
        Console.info(f"원본 깊이로 뽑았습니다 ({depth} PNG{alpha})")
        return upgraded, True

    def _level_plan(self, profile: ColorProfile, sample: "np.ndarray",
                    already_fixed: bool = False) -> bool:
        """
        뽑은 그림이 16~235 로 눌려 있는지 보고, 펼지 말지 정한다.

        표기가 틀린 경우는 ColorProfile 이 이미 알고 있다. ffmpeg 으로 뽑았다면
        읽는 단계에서 바로잡혔으니 여기서 또 건드리면 두 번 펴는 셈이 된다.
        그 외에는 값을 건드리지 않는다 — 다른 도구와 결과가 같아야 하니까.
        """
        mode = (self._opt.levels or "auto").lower()
        if mode == "keep":
            return False

        reading = LevelInspector.read(sample, profile.crange)
        Console.info(f"색 범위  {reading.summary}")

        if mode == "full":
            if already_fixed:
                Console.info("읽는 단계에서 이미 폈습니다")
                return False
            if reading.looks_squeezed:
                Console.info("눌린 그림을 0~255 로 폅니다 (--levels full)")
                return True
            Console.info("이미 0~255 를 쓰고 있어 펼 것이 없습니다")
            return False

        if already_fixed:
            return False
        if profile.retagged:
            Console.info("표기가 틀린 밝기 범위를 펴서 저장합니다")
            return True
        if reading.looks_squeezed:
            Console.info("검정이 들뜨고 흰색이 탁해 보이면 --levels full 로 다시 뽑아 보세요")
        return False

    def _grab(self, source: VideoSource,
              picks: Sequence[FramePick]) -> list[tuple[FramePick, "np.ndarray", int]]:
        """지정된 지점들을 순서대로 읽는다. 앞에서 뒤로 가야 탐색이 싸다."""
        if source.frame_count <= 0:
            raise FramingError("프레임을 하나도 읽지 못했습니다.")
        cache: dict[int, "np.ndarray"] = {}
        grabbed: list[tuple[FramePick, "np.ndarray", int]] = []

        for order, pick in enumerate(picks, 1):
            self._abort_if_asked()
            Progress.tick(order, len(picks), every=1 if len(picks) < 24 else 24)
            if pick.prefer_tail:
                frame, index = self._fetch_last(source, cache)
            else:
                frame, index = self._fetch(source, pick.index, cache)
            if frame is None:
                Console.warn(f"{pick.label} ({FrameNumber.show(pick.index)}) 는 읽지 못해 건너뜁니다")
                continue
            grabbed.append((pick, frame, index))

        if not grabbed:
            raise FramingError("프레임 디코딩에 모두 실패했습니다.")
        return grabbed

    @staticmethod
    def _fetch(source: VideoSource, target: int,
               cache: dict[int, "np.ndarray"]) -> tuple[Optional["np.ndarray"], int]:
        target = max(0, target)
        if target in cache:
            return cache[target], target
        frame, index = source.read_at(target)
        if frame is not None:
            cache[index] = frame
        return frame, index

    @staticmethod
    def _fetch_last(source: VideoSource,
                    cache: dict[int, "np.ndarray"]) -> tuple[Optional["np.ndarray"], int]:
        frame, index = source.read_last()
        if frame is not None:
            cache[index] = frame
            return frame, index
        if cache:
            index = max(cache)
            return cache[index], index
        return None, -1


# ══════════════════════════════════════════════════ 결과를 어떤 모습으로

class Delivery(ABC):
    """
    리타임·역재생이 결과를 어떤 모습으로 내놓을지.

    예전에는 이걸 아무도 고를 수 없었다. 리타임은 무조건 PNG 시퀀스 폴더에
    확인용 mp4 를 얹었고, 역재생은 소스를 보고 혼자 정했다. 영상 하나만
    필요한 사람에게 144장짜리 폴더를 안기는 건 친절이 아니다.

    그래서 "어떤 모습으로 받을지" 를 여기 한 곳에 모으고, 종류마다 클래스를
    하나씩 둔다. 작업 쪽에는 if 가 없다 — 무엇으로 받을지는 이쪽이 안다.
    """

    KEY = ""
    TITLE = ""
    HINT = ""
    keeps_frames = False        # 프레임 파일을 결과로 남기는가
    makes_video = True          # 영상 파일을 만드는가

    @abstractmethod
    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        """(ffmpeg 인코더 인자, 확장자, 사람이 읽을 한 줄)"""

    @staticmethod
    def h264() -> tuple[list[str], str, str]:
        return (["-c:v", "libx264", "-crf", "12", "-preset", "slow",
                 "-pix_fmt", "yuv420p"], ".mp4", "h264 mp4")

    @staticmethod
    def prores(profile: ColorProfile) -> tuple[list[str], str, str]:
        args, why = PixelPlan.for_prores(profile)
        return args, ".mov", f"{why} mov"

    @staticmethod
    def needs_yuv(encoder: Sequence[str]) -> bool:
        """ProRes 4444 로 갈 때는 RGB→YUV 변환식을 끼우면 알파가 그 자리에서 죽는다."""
        return "yuva444p10le" not in list(encoder)

    @classmethod
    def pick(cls, key: str) -> "Delivery":
        return DELIVERY_BY_KEY.get((key or "auto").strip().lower(), AutoDelivery)()


class AutoDelivery(Delivery):
    """소스를 보고 정한다. 8bit 4:2:0 이면 mp4, 그 위면 계조를 지키려고 ProRes."""

    KEY = "auto"
    TITLE = "알아서"
    HINT = "8bit 는 mp4, 10bit·알파는 ProRes mov"

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        if profile.deep:
            args, ext, why = self.prores(profile)
            return args, ext, f"{why} — 소스 계조를 그대로 지킵니다"
        return self.h264()


class Mp4Delivery(Delivery):
    """어디서나 열리는 h264 mp4 하나. 배포·확인용."""

    KEY = "mp4"
    TITLE = "MP4"
    HINT = "어디서나 열림 · 8bit"

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        args, ext, why = self.h264()
        if profile.deep:
            why += " (10bit·알파는 여기서 8bit 로 내려앉습니다)"
        return args, ext, why


class ProResDelivery(Delivery):
    """누크·다빈치로 바로 가는 편집용 마스터."""

    KEY = "mov"
    TITLE = "ProRes MOV"
    HINT = "누크용 마스터 · 계조와 알파 유지"

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        return self.prores(profile)


class SequenceDelivery(Delivery):
    """프레임 파일만. 영상은 만들지 않는다."""

    KEY = "seq"
    TITLE = "PNG 시퀀스"
    HINT = "프레임 파일만 · 영상 없음"
    keeps_frames = True
    makes_video = False

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        return [], "", "프레임만"


class SequenceAndVideoDelivery(SequenceDelivery):
    """프레임도 남기고 확인용 영상도 만든다 — 예전 리타임이 하던 그것."""

    KEY = "both"
    TITLE = "시퀀스 + MP4"
    HINT = "프레임 파일과 확인용 mp4 둘 다"
    makes_video = True

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        return self.h264()


class BundleDelivery(AutoDelivery):
    KEY = "bundle"
    TITLE = "영상 + 프레임 폴더"
    HINT = "영상·전체 PNG·새로 보간된 PNG·프레임 기록을 새 폴더에 저장"
    keeps_frames = True


DELIVERY_CHOICES: tuple[type[Delivery], ...] = (
    AutoDelivery, Mp4Delivery, ProResDelivery,
    SequenceDelivery, SequenceAndVideoDelivery, BundleDelivery)
DELIVERY_BY_KEY = {cls.KEY: cls for cls in DELIVERY_CHOICES}


# ══════════════════════════════════════════════════ 3. 역재생

