"""리타임 작업 · 품질 프리셋 · 컨테이너 태그"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional, Sequence
from typing import TYPE_CHECKING

from .errors import FramingError
from .options import JobResult
from .env import cv2
from .util import Console, FrameRate, ImageIO, Progress, human, unique_path
from .media import open_feed
from .frames import ColorProfile, LabelSnapper, MatteReader, PixelPlan
from .retime import BlendEngine, DuplicateEngine, MotionEngine, RetimeEngine, RetimeNote, RetimePlan, RetimeReport, SourceFrames, TailHoldEngine
from .job import Job
from .plate import PlateSpec, RetimeBundle, StretchNote
from .jobs_extract import Delivery
from .sequence import SequenceOutput

if TYPE_CHECKING:   # 타입 표기에만 쓰는 이름
    from .env import np


QUALITY_PRESETS = {
    "light":  {"crf264": 20, "crf265": 24, "preset": "medium",
               "audio": "192k", "note": "거의 그대로, 조금만 줄어듦"},
    "medium": {"crf264": 23, "crf265": 28, "preset": "medium",
               "audio": "160k", "note": "보통 이걸 씀"},
    "strong": {"crf264": 28, "crf265": 32, "preset": "slow",
               "audio": "128k", "note": "눈에 띄게 물러지지만 많이 줄어듦"},
}

class BoxTags:
    """
    상자에 담을 때 붙여야 하는 코덱 꼬리표.

    HEVC 를 픽셀 그대로 옮겨 담을 때가 문제다. ffmpeg 은 원본이 달고 있던
    꼬리표(hev1)를 그대로 두는데, 애플의 재생기 — 퀵타임 · 미리보기 · 사진 ·
    파인더 미리보기 — 는 hev1 을 열지 못한다. 파일을 두 번 눌러 보면
    "이 파일 형식은 열 수 없습니다" 가 뜬다. 같은 픽셀인데 꼬리표 때문이다.

    hvc1 로 바꿔 달면 열린다. 픽셀은 한 비트도 건드리지 않는다 — 상자 안쪽에
    적힌 이름표만 바꾸는 것이라, 재인코딩하지 않는다는 약속도 그대로다.
    """

    # 코덱 → 애플이 여는 꼬리표. 지금은 HEVC 하나뿐이지만 자리는 열어 둔다.
    APPLE_SAFE = {"hevc": "hvc1"}
    BOXES = (".mp4", ".mov", ".m4v")

    @classmethod
    def for_copy(cls, vcodec: str, ext: str) -> list:
        """그대로 옮겨 담을 때 덧붙일 인자. 붙일 게 없으면 빈 목록."""
        if (ext or "").lower() not in cls.BOXES:
            return []
        tag = cls.APPLE_SAFE.get((vcodec or "").lower())
        return ["-tag:v", tag] if tag else []


MP4_VIDEO_OK = ("h264", "hevc", "mpeg4", "av1")
MP4_AUDIO_OK = ("aac", "mp3", "ac3", "alac")
MOV_VIDEO_OK = ("h264", "hevc", "prores", "dnxhd", "mjpeg", "mpeg4",
                "qtrle", "png", "rawvideo")
MOV_AUDIO_OK = ("aac", "pcm_s16le", "pcm_s24le", "alac", "mp3")


class RetimeEngineFactory:
    """Choose and construct an engine; jobs only coordinate input and output."""

    def __init__(self, engines, ff):
        self._engines = {engine.KEY: engine for engine in engines}
        self._ff = ff

    def create(self, mode, plan, matte, snapper, abort, workspace, sample=None):
        chosen = (mode or 'auto').lower()
        if chosen == 'auto':
            chosen = 'dup' if matte or not plan.slowing else 'motion'
            Console.info(f'고름  {chosen} — 매트 보호와 결과 길이에 따라 선택했습니다')
        if chosen not in self._engines:
            raise FramingError(f'알 수 없는 리타임 방식입니다: {chosen}')
        engine = self._engines[chosen]
        if engine is TailHoldEngine and not plan.slowing:
            Console.warn('끝 홀드는 늘릴 때만 씁니다 — 복제로 갑니다')
            engine = DuplicateEngine
        if engine.NEEDS_FFMPEG and not self._ff.find('ffmpeg'):
            Console.warn('ffmpeg 이 없어 겹치기로 갑니다')
            engine = BlendEngine
        if engine is MotionEngine and sample is not None and not engine.supports(sample):
            Console.warn('계조와 채널을 유지하기 위해 겹치기를 사용합니다')
            engine = BlendEngine
        if engine is MotionEngine:
            return engine(plan, snapper, abort, ff=self._ff, workspace=workspace)
        return engine(plan, snapper, abort)


class RetimeJob(Job):
    """영상·시퀀스를 원하는 길이로 늘리거나 줄인다."""

    KEY = "retime"
    ALIASES = ("speed", "stretch")
    TITLE = "Retime"
    HINT = "길이 · 배속 바꾸기 (프레임 보간)"
    NOTE = """
    길이를 말하는 방법이 셋입니다. 편한 걸 쓰세요. 어느 쪽이든 같은 곳으로 갑니다.
      --frames 264     결과를 264프레임으로
      --seconds 11     결과를 11초로 (fps 를 곱해서 계산)
      --speed 0.5      절반 속도 = 두 배 길이. 2 면 두 배 빠르게

    없는 프레임을 무엇으로 채울까  (--retime-mode)
      auto     소재를 보고 알아서 고릅니다 (기본). 무엇을 골랐는지 매번 알려줍니다
      dup      가장 가까운 원본 프레임을 그대로. 픽셀 연산이 0 이라 매트가 100% 남습니다
      motion   움직임을 추정해 없던 그림을 그립니다. 제일 부드럽고, 가끔 찢어집니다
               (ffmpeg 이 없으면 앞뒤를 섞는 방식으로 알아서 물러섭니다)

    auto 가 고르는 기준
      매트 · 뎁스 패스면          → dup.    라벨색을 뭉개면 그 픽셀의 뜻이 사라집니다
      실사인데 느리게 (늘리기)     → motion. 슬로모션은 없던 프레임이 있어야 매끄럽습니다
      실사인데 빠르게 (줄이기)     → dup.    버리는 작업이라 새로 그릴 게 없습니다

    매트 패스를 알아서 알아봅니다
      매트는 몇 개의 라벨색으로만 칠해져 색상이 좁게 몰립니다 (실측 83~99%).
      실사·페인팅은 넓게 퍼집니다 (21~28%). 이 차이로 갈라냅니다.
      매트로 판별되면 쓰인 라벨색을 찾아 보여주고, 보간 후 섞인 픽셀을
      가장 가까운 라벨로 되돌립니다. 명도는 손대지 않습니다 — 그건 뎁스니까요.
      실사인데 매트로 잘못 본 것 같으면 --no-snap 으로 색 복원을 끄세요.

    무엇으로 받을지 고를 수 있습니다  (--result)
      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov — 영상 하나만 나옵니다
      mp4    어디서나 열리는 h264 하나
      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다
      seq    PNG 시퀀스만. 영상은 만들지 않습니다
      both   시퀀스 + 확인용 영상 둘 다
      bundle 영상·전체 프레임·새로 보간된 프레임·기록을 새 폴더에 저장
    새 UI 리타임 작업은 bundle이 기본입니다. CLI는 auto를 유지합니다.

    알아둘 것
      · 24fps 의 배수로 맞추면 편집에서 어긋나지 않습니다. 아니면 알려줍니다.
      · motion 은 결과를 꼭 눈으로 보세요. 빠른 움직임 · 가림 · 모션블러에 약합니다.
      · 소리는 빠집니다. 길이가 바뀌면 소리도 같이 늘려야 하는데,
        그건 리타임이 아니라 다른 작업입니다.
      · --note 를 주면 어디를 손댔는지 메모장(.txt) 한 장을 결과 옆에 놓습니다.
      · 영상 소스의 색 규격(bt709 / 제한범위 등)은 결과 mp4 에 그대로 찍어 둡니다."""
    ACCEPTS_FOLDER = True

    ENGINES = (DuplicateEngine, TailHoldEngine, BlendEngine, MotionEngine)

    def run(self, target: Path) -> JobResult:
        """
        프레임을 어디에 그릴지부터 정한다.

        예전에는 무조건 원본 옆에 `_retimed` 폴더를 만들고 PNG 를 전부 깔았다.
        영상 하나만 필요한 사람에게 144장짜리 폴더를 안기는 셈이었다.
        이제 시퀀스로 받겠다고 한 경우에만 폴더가 생기고, 아니면 임시 자리에
        그렸다가 영상만 남기고 치운다.
        """
        result = JobResult(target=target)
        key = (self._opt.delivery or "auto").lower()
        self._naming = SequenceOutput.of(target) if target.is_dir() else None
        # 시퀀스를 넣었으면 같은 형식의 시퀀스로 돌려준다 (EXR 은 EXR, DPX 는 DPX)
        how = Delivery.pick("seq" if self._naming and key == "auto" else key)
        keep = how.keeps_frames
        folder = (self._retime_folder(target) if keep
                  else Path(tempfile.mkdtemp(prefix="framing_retime_out_")))
        workspace = Path(tempfile.mkdtemp(prefix="framing_retime_"))
        try:
            return self._work(target, folder, workspace, result, how)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            if not keep:
                shutil.rmtree(folder, ignore_errors=True)

    def _retime_folder(self, target: Path) -> Path:
        name = target.name if target.is_dir() else target.stem
        folder = unique_path((self._opt.out_dir or target.parent) / f'{name}_retimed')
        folder.mkdir(parents=True, exist_ok=False)
        return folder

    def _work(self, target: Path, folder: Path, workspace: Path,
              result: JobResult, how: "Delivery") -> JobResult:
        bundle = RetimeBundle(folder, target) if how.KEY == 'bundle' else None
        frame_folder = bundle.frames if bundle else folder
        frames, fps, profile = self._load(target, workspace)
        plan = RetimePlan.decide(len(frames), fps,
                                 frames=self._opt.target_frames,
                                 speed=self._opt.speed,
                                 seconds=self._opt.target_seconds)
        if plan.refusal:
            raise FramingError(plan.refusal)      # 못 할 일은 계획부터 말하지 않는다
        Console.info(f"계획  {plan.summary}")
        if plan.suspicious:
            Console.warn(plan.suspicious)
        self._warn_disk(frames[0], plan)

        snapper, matte = self._read_material(frames)
        engine = self._pick_engine(plan, matte, snapper, workspace, frames[0])
        Console.info(f"방식  {engine.TITLE} — {engine.BLURB}")
        material = SourceFrames(frames)
        self._warn_repeats(material, plan, engine)
        if plan.unchanged:
            Console.info("길이가 그대로입니다 — 프레임을 다시 번호만 매겨 내보냅니다")

        made = self._render(engine, frames, frame_folder, plan)
        if not made:
            raise FramingError("결과 프레임이 만들어지지 않았습니다.")
        result.outputs = list(made) if how.keeps_frames else []
        result.output_dir = folder if how.keeps_frames else target.parent
        result.ok = True
        result.message = (f"{plan.source} → {len(made)}프레임  ·  "
                          f"{plan.speed:.3f}배속  ·  {engine.TITLE}")

        # 어디를 건드렸는지 말해 준다. 길이만 알려 주면 문제가 생겼을 때
        # 어느 프레임을 봐야 하는지 알 수가 없다.
        report = RetimeReport(plan, engine.KEY, material.origin)
        result.retime_map = report.payload()
        labels = result.retime_map['labels']
        kept, made_label = labels['kept'], labels['made']
        Console.info(f"배치  {kept} {report.kept}장 · {made_label} {report.made}장")
        Console.info(result.retime_map['note'])
        Console.info(f"자리  {report.where()}")
        if report.strip():
            Console.info(f"      {report.strip()}   (▉ {kept} · ░ {made_label})")
        # 원본이 이미 들고 있던 정지 구간. 늘리기가 만든 게 아니라서 되돌릴 때도
        # 안 빠진다 — 말해 주지 않으면 "왜 더블이 남아 있지" 가 된다.
        doubled = report.already_double()
        if doubled:
            Console.warn(f"원본에 이미 같은 그림이 {doubled}장 붙어 있습니다 — "
                         f"늘리면서 생긴 것이 아니라 되돌려도 그대로 남습니다")

        stretch = self._stretch_note(target, plan, report, profile)
        movie = self._mux(frame_folder, plan, len(made), profile, how, target,
                          stretch.embed_args() if stretch else ())
        if movie and stretch:
            # 되돌릴 근거는 **영상 안에** 심는다. 옆에 json 을 따로 떨구면 결과
            # 폴더에 메모가 둘(.txt · .json)이 되고, 어느 게 진짜인지 알 수 없다.
            # 사람이 읽고 다시 넣을 종이는 _리타임메모.txt 하나로 족하다.
            Console.info(f"되돌릴 근거  영상 안에 심었습니다 · {stretch.summary()}")
            if stretch.restoration_problem:
                Console.warn(stretch.restoration_problem)
            else:
                Console.info(f"되돌리기  framing revert \"{movie.name}\"")
        if movie:
            result.outputs.append(movie)
            result.message += f"  ·  {movie.name}"
        elif how.makes_video and (not how.keeps_frames or how.KEY == 'bundle' or self._opt.make_mp4):
            if how.keeps_frames:
                raise FramingError(f'영상 저장에 실패했습니다. 프레임은 {folder}에 보관되어 있습니다.')
            raise FramingError("영상을 만들지 못했습니다. ffmpeg 을 확인해 주세요.")
        if bundle:
            result.outputs += bundle.finish(report, made, movie, self._abort_if_asked)
            result.next_paths = [str(movie)]
            result.retime_map.update(sourcePath=str(target), movie=str(movie),
                                     sourceVideo=target.is_file(),
                                     framesDir=str(bundle.frames),
                                     interpolatedDir=str(bundle.interpolated) if bundle.interpolated.exists() else '')
        self._leave_note(report, target, movie, result)
        return result

    def _warn_repeats(self, material: SourceFrames, plan: RetimePlan,
                      engine: RetimeEngine) -> None:
        """원본 픽셀의 완전 일치와 복제 배치 계획을 구분해 알린다."""
        if material.repeated:
            Console.info(f"원본 확인  디코딩 픽셀이 완전히 같은 인접 프레임 쌍 {material.repeated}개 "
                         "— 정지 구간의 길이는 샷 검사에서 확인하세요")
        if engine.KEY != "dup":
            return
        repeats = RetimeReport(plan, engine.KEY, material.origin).made
        if repeats:
            Console.info(f"배치 계획  {repeats}개 위치에서 앞 프레임을 재사용합니다 "
                         "— 홀드 검출 결과가 아닙니다")

    def _leave_note(self, report: "RetimeReport", target: Path,
                    movie: Optional[Path], result: JobResult) -> None:
        """
        메모장 한 장. 결과 옆에 남는 종이는 이것 하나뿐이다.

        늘렸으면 묻지 않고 남긴다 — 되돌리려면 근거가 있어야 하고, 없는 걸
        나중에 만들 수는 없다. 줄이거나 길이가 그대로면 되돌릴 것이 없으니,
        "메모 남기기" 를 켠 사람에게만 준다.
        """
        stretched = any(report.added())
        if not stretched and not self._opt.retime_note:
            return
        where = movie.parent if movie else (result.output_dir or target.parent)
        spot = RetimeNote(report, target, self._engine_title(report)).write(where, self._opt.language)
        if spot:
            result.outputs.append(spot)
            Console.info(f"메모  {spot.name}")

    def _engine_title(self, report: "RetimeReport") -> str:
        key = report.payload().get("engine", "")
        found = next((e for e in self.ENGINES if e.KEY == key), None)
        return found.TITLE if found else key

    # --- 읽기 -------------------------------------------------------------

    def _load(self, target: Path,
              workspace: Path) -> tuple[list["np.ndarray"], float, ColorProfile]:
        """
        프레임을 전부 메모리에 올린다. 리타임은 앞뒤를 같이 봐야 해서 흘려보낼 수 없다.
        대신 올리기 전에 얼마나 먹을지 재서, 위험하면 미리 말한다.
        """
        profile = ColorProfile()
        if not target.is_dir():
            profile = self._color(target)
            self._announce_color(profile)
            if profile.deep and self._ff.find("ffmpeg"):
                deep = self._load_deep(target, profile, workspace)
                if deep:
                    fps = self._source_fps(target)
                    return deep, fps, profile
        with open_feed(target, preserve=True) as feed:
            total, fps = feed.count, (feed.fps or float(self._opt.fps))
            frames = []
            for index, frame in enumerate(feed.frames(), 1):
                self._abort_if_asked()
                if index == 1:
                    self._warn_memory(frame, total)
                frames.append(frame)
                Progress.tick(index, total)
        if not frames:
            raise FramingError("프레임을 하나도 읽지 못했습니다.")
        if target.is_dir():
            sampled = ColorProfile.from_frame(frames[0])
            if sampled.bits > 8 or sampled.has_alpha:
                profile = sampled
        return frames, fps, profile

    def _source_fps(self, target: Path) -> float:
        info = self._ff.probe(target) if self._ff.find("ffprobe") else None
        return (info.fps if info and info.fps else float(self._opt.fps)) or 24.0

    def _load_deep(self, target: Path, profile: ColorProfile,
                   workspace: Path) -> list["np.ndarray"]:
        """
        10bit · 알파 소스를 원본 깊이 그대로 읽는다.

        OpenCV 로 읽으면 여기서 8bit 로 깎여서, 리타임만 하려다 계조를 잃는다.
        Key Frames 와 같은 판단을 쓴다 — 같은 영상이면 어느 작업을 거치든 같아야 한다.
        """
        stage = workspace / "_deep"
        stage.mkdir(parents=True, exist_ok=True)
        Console.step(f"원본 깊이({profile.bits}bit"
                     f"{' +알파' if profile.has_alpha else ''})로 읽는 중")
        code = self._ff.run(["-y", "-i", str(target),
                             *ColorProfile.join(profile.read_filter()),
                             "-c:v", "png", *PixelPlan.still_args(profile), "-an",
                             str(stage / "d.%06d.png")], quiet=True)
        made = sorted(stage.glob("d.*.png"))
        if code != 0 and not made:
            Console.info("원본 깊이로 읽지 못해 8bit 로 갑니다")
            return []
        frames: list["np.ndarray"] = []
        for index, file in enumerate(made, 1):
            self._abort_if_asked()
            if index == 1:
                probe = ImageIO.read(file, cv2.IMREAD_UNCHANGED)
                if probe is None:
                    return []
                self._warn_memory(probe, len(made))
                frames.append(probe)
                continue
            image = ImageIO.read(file, cv2.IMREAD_UNCHANGED)
            if image is None:
                return []
            frames.append(image)
            Progress.tick(index, len(made))
        return frames

    BULKY = 2 * 1024 ** 3       # 결과가 이보다 크면 시작 전에 말한다
    HEAVY = 3 * 1024 ** 3       # 이 이상 먹을 것 같으면 미리 말한다

    @classmethod
    def _warn_disk(cls, first: "np.ndarray", plan: RetimePlan) -> None:
        """PNG 는 대충 원본 바이트의 절반쯤으로 눌린다. 자릿수만 맞으면 충분하다."""
        rough = int(first.nbytes * 0.5) * plan.target
        if rough < cls.BULKY:
            return
        Console.warn(f"결과가 대략 {human(rough)} 나옵니다 ({plan.target}장 PNG) "
                     "— 디스크 여유를 확인하세요")

    @classmethod
    def _warn_memory(cls, first: "np.ndarray", total: int) -> None:
        """
        멈춘 것처럼 보이다가 죽는 게 제일 나쁘다. 들어가기 전에 얼마나 먹는지 말한다.
        (리타임은 앞뒤 프레임을 같이 봐야 해서 흘려보낼 수 없다.)
        """
        need = int(first.nbytes) * max(1, total)
        if need < cls.HEAVY:
            return
        Console.warn(f"프레임을 전부 올리는 데 약 {human(need)} 가 필요합니다 "
                     "— 리타임은 앞뒤를 같이 봐야 해서 어쩔 수 없습니다")
        Console.info("메모리가 모자라면 Sequence 로 나눠 뽑고 구간별로 돌리세요")

    def _read_material(self, frames: Sequence["np.ndarray"]
                       ) -> tuple[Optional[LabelSnapper], bool]:
        """매트인지 실사인지 읽고, 매트면 라벨 목록을 만든다."""
        if self._opt.no_snap:
            Console.info("소재  색 복원 끔 (--no-snap)")
            return None, False
        if frames[0].ndim == 2:
            return None, True
        step = max(1, len(frames) // 5)
        reader = MatteReader(frames[::step][:5])
        share = reader.concentration * 100
        if not reader.is_matte:
            Console.info(f"소재  실사 · 페인팅 (색상 집중도 {share:.0f}%) "
                         "— 색은 건드리지 않습니다")
            return None, False
        snapper = LabelSnapper(reader.legend())
        Console.info(f"소재  매트 · 뎁스 패스 (색상 집중도 {share:.0f}%)")
        Console.info(f"라벨  {snapper.describes}")
        return snapper, True

    # --- 고르기 -----------------------------------------------------------

    def _pick_engine(self, plan: RetimePlan, matte: bool,
                     snapper: Optional[LabelSnapper], workspace: Path, sample=None) -> RetimeEngine:
        return RetimeEngineFactory(self.ENGINES, self._ff).create(
            self._opt.retime_mode, plan, matte, snapper,
            self._abort_if_asked, workspace, sample)

    # --- 쓰기 -------------------------------------------------------------

    def _render(self, engine: RetimeEngine, frames: Sequence["np.ndarray"],
                folder: Path, plan: RetimePlan) -> list[Path]:
        made: list[Path] = []

        naming = getattr(self, "_naming", None)

        def write(position: int, frame: "np.ndarray") -> None:
            if naming:
                destination = folder / naming.name(position)
                saved = ImageIO.write(destination, naming.ready(frame), dpx_bits=naming.dpx_bits)
            else:
                destination = folder / f"out.{position + 1:04d}.png"
                saved = ImageIO.write(destination, frame)
            if not saved:
                raise FramingError(f"저장 실패: {destination.name}")
            made.append(destination)
            Progress.tick(position + 1, plan.target)

        engine.render(frames, write)
        return sorted(set(made))

    def _stretch_note(self, target: Path, plan: RetimePlan,
                      report: RetimeReport, profile: ColorProfile):
        """
        늘린 자리를 되돌릴 수 있게 적어 둔다.

        여기서 적지 않으면 되돌릴 때 "어디를 뺄지" 를 다시 추정해야 하고,
        추정은 늘릴 때와 어긋난다. 줄이는 작업에는 되돌릴 것이 없다.
        """
        if not plan.slowing:
            return None
        # marks() 가 아니라 added() 다. marks() 는 "결과에서 더블로 보이는 자리"
        # 라서 원본에 원래 있던 정지 구간까지 센다. 그걸로 되돌리면 원본이 깎인다.
        added = report.added()
        engine = report.payload().get("engine", "")
        origins = [(int(round(report.at(position)))
                    if engine in ("dup", "hold") or not added[position] else -1)
                   for position in range(plan.target)]
        spec = PlateSpec(source=str(target), frames=plan.source, fps=plan.fps,
                         pix_fmt=profile.pix_fmt or "")
        return StretchNote(spec, plan.target, origins,
                           [i for i, made in enumerate(added) if made])

    def _mux(self, folder: Path, plan: RetimePlan, count: int,
             profile: ColorProfile, how: "Delivery", target: Path,
             extra: Sequence[str] = ()) -> Optional[Path]:
        """
        그려 둔 프레임을 영상 하나로 묶는다.

        프레임을 남기는 경우에만 그 폴더 안에 확인용으로 넣는다. 아니면
        원본 옆에 결과물로 — 그게 사람들이 기대하는 자리다.
        """
        if not how.makes_video or not self._ff.find("ffmpeg"):
            return None
        if how.keeps_frames and how.KEY != 'bundle' and not self._opt.make_mp4:
            return None
        encoder, ext, why = how.encode(profile)
        if not ext:
            return None
        Console.info(f"묶기  {why}")
        movie_folder = folder.parent if how.KEY == 'bundle' else folder
        made = (movie_folder / f"retimed_{count}f{ext}" if how.keeps_frames
                else self._output_file(target, "_retimed", ext))
        # 규격이 안 적힌 소스도 profile 이 스스로 안다 (read_matrix) —
        # 예전엔 여기서 빈 프로필로 갈아치웠고, 그게 색이 밀리던 자리였다.
        yuv = Delivery.needs_yuv(encoder)
        shape = ["-vf", profile.to_yuv_filter()] if yuv else []
        naming = getattr(self, "_naming", None)
        if naming:
            source = ["-start_number", str(naming.start), "-i", str(folder / naming.pattern())]
            shape = ColorProfile.join(naming.curve(alpha=not yuv),
                                      ColorProfile().to_yuv_filter() if yuv else "")
            tags = ColorProfile().stamp_args()
        else:
            source, tags = ["-i", str(folder / "out.%04d.png")], profile.tag_args()
            info = self._ff.probe(target) if self._ff.find("ffprobe") else None
            if info and info.timecode:
                tags += info.timecode_args()          # 길이는 바뀌어도 시작 타임코드는 원본 그대로
                Console.info(f"타임코드  {info.timecode} 에서 시작 (원본과 같음)")
        code = self._ff.run(["-y", "-framerate", FrameRate.from_value(plan.fps).text,
                             *source, *shape, *encoder, *tags, *extra, "-an",
                             str(made)], quiet=True)
        return made if code == 0 and made.exists() else None


