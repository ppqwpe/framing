"""변환 작업과 변환 대상"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import Optional

from .errors import FramingError
from .naming import DeliverySlate
from .options import JobResult
from .env import IMAGE_EXTENSIONS
from .util import Console, FrameRate, hms, human
from .media import ProbeInfo
from .frames import ColorProfile, PixelPlan, TransferCurve
from .job import Job
from .sequence import GifJob, SequenceInput, SequenceMovieJob
from .jobs_extract import ExtractJob
from .jobs_retime import BoxTags, MOV_AUDIO_OK, MOV_VIDEO_OK, MP4_AUDIO_OK, MP4_VIDEO_OK, QUALITY_PRESETS


class TranscodeJob(Job):
    """ffmpeg 로 컨테이너를 바꾸거나 다시 인코딩하는 작업들의 공통 뼈대."""

    NEEDS_FFMPEG = True

    @abstractmethod
    def _plan(self, target: Path, info: Optional[ProbeInfo]) -> tuple[list[str], str, str, str]:
        """(ffmpeg 인자, 확장자, 파일명 접미사, 사람이 읽을 설명)"""

    def _named(self, target: Path, suffix: str, ext: str) -> Path:
        """결과 파일 이름을 정하는 자리. 기본은 '원래이름 + 접미사'."""
        return self._output_file(target, suffix, ext)

    def run(self, target: Path) -> JobResult:
        result = JobResult(target=target, size_in=target.stat().st_size)
        info = self._ff.probe(target)
        profile = self._color(target, info)
        if info:
            Console.info(f"{human(result.size_in)}  {info.width}x{info.height}  "
                         f"{info.fps:.2f}fps  {hms(info.duration)}  "
                         f"{info.vcodec or '영상없음'} / {info.acodec or '소리없음'}")
            self._announce_color(profile)
            if info.alpha and self._drops_alpha():
                Console.warn("알파 채널이 있는데 이 형식은 못 담습니다. 투명도가 사라집니다. "
                             "투명도를 지키려면 누크용 마스터로 보내세요.")
        else:
            Console.info(f"{human(result.size_in)}   (내용을 못 읽었습니다)")

        args, ext, suffix, why = self._plan(target, info)
        args, why = self._apply_downscale(args, info, why)
        args = self._stamp(args, profile)

        # 같은 상자에 그대로 옮겨 담을 뿐이면 굳이 파일을 만들지 않는다.
        # 다만 꼬리표를 고쳐야 하는 경우는 다르다 — 그건 픽셀은 그대로여도
        # "맥에서 안 열리던 것이 열리게" 되는 일이라, 만들 값어치가 있다.
        needs_tag = BoxTags.for_copy(info.vcodec if info else "", ext)
        if (args[:2] == ["-c", "copy"] and target.suffix.lower() == ext
                and not self._opt.max_height and not profile.retagged
                and not needs_tag):
            result.message = "이미 그 형식이고 다시 인코딩할 것도 없어 건너뜁니다 (용량을 줄이려면 compress)"
            result.ok = True
            result.size_out = result.size_in
            return result

        destination = self._named(target, suffix, ext)
        Console.info(f"할 일  {why}")
        Console.info(f"출력  {destination}")

        code = self._ff.run(["-y" if self._opt.overwrite else "-n",
                             "-i", str(target), *args, str(destination)],
                            total_seconds=(info.duration if info else 0.0))
        if code != 0 or not destination.exists():
            raise FramingError(f"ffmpeg 실패 (종료코드 {code})")

        result.outputs = [destination]
        result.output_dir = destination.parent
        result.size_out = destination.stat().st_size
        result.ok = True
        if args[:2] == ["-c", "copy"]:
            result.message = f"{human(result.size_in)} → {human(result.size_out)}  (영상 데이터 그대로)"
        else:
            pct = (1 - result.size_out / result.size_in) * 100 if result.size_in else 0
            word = "작아짐" if pct > 0 else "커짐"
            result.message = (f"{human(result.size_in)} → {human(result.size_out)}  "
                              f"{abs(pct):.1f}% {word}")
            if pct < -5:
                if self._opt.convert_to == "nuke":
                    result.message += "  (ProRes 는 편집용이라 커지는 게 정상입니다)"
                elif self._opt.convert_to == "small":
                    result.message += ("  (원본이 이미 알뜰하게 인코딩돼 있습니다 "
                                       "— 세게 줄이거나 그냥 원본을 쓰세요)")
        return result

    def _drops_alpha(self) -> bool:
        """이 목적지가 투명도를 버리는가. 버릴 거면 미리 말해야 한다."""
        return (self._opt.convert_to or "mp4") not in ("nuke", "fps")     # fps 는 알파 소스를 4444 로 담는다

    @staticmethod
    def _stamp(args: list[str], profile: ColorProfile) -> list[str]:
        """
        결과물에 색 규격을 도장 찍는다.

        ffmpeg 이 알아서 옮겨줄 때도 있지만, 태그가 없거나 틀린 소스에서는 그냥 빈칸이
        된다. 빈칸이면 다음 도구가 추측하고, 추측이 어긋나는 순간 색이 밀린다.
        재포장(-c copy)일 때도 찍는다 — 픽셀은 그대로고 표기만 바로잡히는 셈이라 공짜다.
        """
        tags = profile.tag_args()
        if not tags:
            return args
        stamped = list(args) + tags
        if "-bsf:v" not in stamped:
            stamped += profile.range_bsf(args)
        return stamped

    def _apply_downscale(self, args: list[str], info: Optional[ProbeInfo],
                         why: str) -> tuple[list[str], str]:
        height = self._opt.max_height
        if not height or not info or info.height <= height:
            return args, why
        if args[:1] == ["-c"]:
            return args, why + "   (컨테이너만 바꿀 땐 해상도를 못 줄입니다)"
        if "-vf" in args:                   # 곡선 필터가 이미 있으면 이어 붙인다 (-vf 가 둘이면 뒤엣것만 산다)
            at = args.index("-vf")
            merged = list(args)
            merged[at + 1] = f"{args[at + 1]},scale=-2:{height}"
            return merged, why + f", {height}p 로 축소"
        return ["-vf", f"scale=-2:{height}"] + args, why + f", {height}p 로 축소"

    @staticmethod
    def _audio_args(info: Optional[ProbeInfo], preset: dict) -> list[str]:
        has_audio = bool(info and info.acodec)
        return ["-c:a", "aac", "-b:a", preset["audio"]] if has_audio else ["-an"]


class ConvertTarget(ABC):
    """
    무엇으로 바꿀 것인가. 목적지마다 규칙이 다르니 각자 자기 규칙만 안다.

    예전에는 To MP4 · To MOV · Shrink 로 창이 셋이었다. 하는 일이 거의 같은데
    창만 갈라져 있어서, 쓰는 사람이 "mov 로 갈까 압축할까"를 고르려면
    창을 옮겨 다녀야 했다. 목적지는 선택지이지 별개의 작업이 아니다.
    """

    @staticmethod
    def pcm(info: Optional[ProbeInfo]) -> list[str]:
        """
        mov 에 담을 소리. 원본이 16bit pcm 이면 16bit, 그 밖(24bit 녹음 · aac 등)은 24bit.
        예전에는 늘 16bit 로 내려서 24bit 현장음이 깎였다.
        """
        if not (info and info.acodec):
            return ["-an"]
        if info.acodec == "pcm_s16le" or (info.acodec.startswith("pcm") and 0 < info.audio_bits <= 16):
            return ["-c:a", "pcm_s16le"]
        return ["-c:a", "pcm_s24le"]

    KEY = ""
    TITLE = ""
    HINT = ""
    EXT = ".mp4"
    SUFFIX = "_conv"
    KIND = "video"              # 이 목적지가 받는 것: video · image · sequence

    @abstractmethod
    def plan(self, job: "ConvertJob", target: Path,
             info: Optional[ProbeInfo], profile: ColorProfile
             ) -> tuple[list[str], str, str, str]: ...

    @staticmethod
    def _preset(job: "ConvertJob") -> dict:
        return QUALITY_PRESETS[job._opt.level]


class Mp4Target(ConvertTarget):
    KEY = "mp4"
    TITLE = "MP4 (배포·업로드)"
    HINT = "가장 잘 열리는 형식. 대개 다시 인코딩하지 않고 상자만 갈아끼웁니다"
    EXT = ".mp4"

    def plan(self, job, target, info, profile):
        preset = self._preset(job)
        vcodec = info.vcodec if info else ""
        acodec = info.acodec if info else ""
        if (vcodec in MP4_VIDEO_OK and (not acodec or acodec in MP4_AUDIO_OK)
                and not job._opt.force_encode):
            tag = BoxTags.for_copy(vcodec, self.EXT)
            return (["-c", "copy", *tag, "-movflags", "+faststart"],
                    self.EXT, self.SUFFIX,
                    f"상자만 교체 — {vcodec} 영상은 손대지 않습니다"
                    + ("  (애플이 열도록 꼬리표만 hvc1 로)" if tag else ""))
        pix, note = PixelPlan.for_x26x(profile, keep_depth=False)
        if note:
            Console.info(f"계조  {note}")
        args = ["-c:v", "libx264", "-crf", str(preset["crf264"]),
                "-preset", preset["preset"], *pix,
                "-movflags", "+faststart"] + job._audio_args(info, preset)
        why = (f"{vcodec or '?'} 는 mp4 에 못 들어가서 h264 로 다시 인코딩"
               if vcodec not in MP4_VIDEO_OK else "요청대로 h264 로 다시 인코딩")
        return args, self.EXT, self.SUFFIX, why


class MovTarget(ConvertTarget):
    KEY = "mov"
    TITLE = "MOV (상자만 교체)"
    HINT = "확장자만 .mov 로. 안에 든 코덱도 화질도 그대로입니다"
    EXT = ".mov"

    def plan(self, job, target, info, profile):
        preset = self._preset(job)
        vcodec = info.vcodec if info else ""
        acodec = info.acodec if info else ""
        if (vcodec in MOV_VIDEO_OK and (not acodec or acodec in MOV_AUDIO_OK)
                and not job._opt.force_encode):
            tag = BoxTags.for_copy(vcodec, self.EXT)
            return (["-c", "copy", *tag], self.EXT, self.SUFFIX,
                    f"다시 담기만 — {vcodec} 을 비트 그대로 .mov 에 넣습니다 "
                    f"(안에 든 건 여전히 {vcodec} 입니다)"
                    + ("  · 애플이 열도록 꼬리표만 hvc1 로" if tag else ""))
        pix, note = PixelPlan.for_x26x(profile, keep_depth=True)
        if note:
            Console.info(f"계조  {note}")
        args = ["-c:v", "libx264", "-crf", str(preset["crf264"]),
                "-preset", preset["preset"], *pix] + job._audio_args(info, preset)
        return args, self.EXT, self.SUFFIX, f"{vcodec or '?'} 는 mov 에 못 들어가서 h264 로 다시 인코딩"


class NukeTarget(ConvertTarget):
    KEY = "nuke"
    TITLE = "누크용 마스터 (ProRes)"
    HINT = "Nuke · After Effects · Resolve 가 물고 늘어지지 않는 편집용 코덱"
    EXT = ".mov"
    SUFFIX = "_nuke"

    def plan(self, job, target, info, profile):
        args, why = PixelPlan.for_prores(profile)
        args += self.pcm(info)
        return args, self.EXT, self.SUFFIX, f"{why} 로 다시 인코딩 — 누크가 바로 읽습니다"


class DeliveryTarget(ConvertTarget):
    """
    납품 규약 한 벌을 통째로 건다 — MOV / ProRes 4444 XQ / 1920×1080 / 24fps.

    규약은 네 줄인데 손으로 맞추면 네 군데서 틀립니다. 특히 해상도와 fps 는
    "맞겠지" 하고 넘어갔다가 상대 쪽에서 반려됩니다. 그래서 여기서는 재 보고,
    안 맞으면 맞추면서 **무엇을 건드렸는지 말합니다.** 조용히 고치면 다음에
    또 같은 소스를 넣습니다.

    파일 이름은 `DeliverySlate` 가 규약(`샷코드_테스크_v###`)대로 짓습니다.
    """

    KEY = "delivery"
    TITLE = "납품 (ProRes 4444 XQ)"
    HINT = "MOV · 4444 XQ · 1920×1080 · 24fps · 샷코드_테스크_v###"
    EXT = ".mov"
    SUFFIX = "_delivery"          # 이름은 슬레이트가 짓는다. 여기는 쓰이지 않는다

    WIDTH, HEIGHT, FPS = 1920, 1080, 24.0
    PROFILE = "5"                 # prores_ks: 0 proxy · 1 lt · 2 std · 3 hq · 4 4444 · 5 4444xq

    def _frame(self, info: Optional[ProbeInfo]) -> tuple[str, str]:
        """규약 해상도로 맞춘다. 비율은 절대 안 늘린다 — 모자라면 레터박스."""
        if info and info.width == self.WIDTH and info.height == self.HEIGHT:
            return "", ""
        if info and info.width and info.height:
            Console.warn(f"{info.width}x{info.height} 를 {self.WIDTH}x{self.HEIGHT} 로 맞춥니다 — "
                         f"비율이 다르면 위아래(또는 좌우)에 검은 띠가 생깁니다")
        return (f"scale={self.WIDTH}:{self.HEIGHT}:force_original_aspect_ratio=decrease"
                f":flags=lanczos,pad={self.WIDTH}:{self.HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
                f"{self.WIDTH}x{self.HEIGHT} 로 맞춤")

    CONFORM_WITHIN = 0.002        # 23.976 ↔ 24 처럼 0.1% 차이는 프레임 1:1 로 다시 읽는다

    def _rate(self, info: Optional[ProbeInfo]) -> tuple[str, list[str], str]:
        """
        24fps 로 못 박는다. (영상 필터, 소리 필터 인자, 설명)

        23.976 은 컨폼한다 — 프레임을 하나도 버리거나 겹치지 않고 24 로 다시 읽는다
        (0.1% 빨라지고 소리도 같이 맞춘다). 예전에는 -r 24 로 41초마다 한 장씩 겹쳐 채워서,
        납품본과 플레이트의 프레임 번호가 어긋났다.
        그 밖의 fps(25 · 30 …)는 프레임을 버리거나 겹쳐야 해서 크게 말한다.
        """
        fps = info.fps if info and info.fps else 0.0
        if fps and abs(fps - self.FPS) < 0.01:
            return "", [], ""
        if fps and abs(fps - self.FPS) / self.FPS < self.CONFORM_WITHIN:
            Console.info(f"원본 {fps:.3f}fps 를 프레임 1:1 로 24fps 컨폼합니다 (프레임 수 그대로)")
            tempo = self.FPS / fps
            return (f"setpts=N/({self.FPS:g}*TB)", ["-af", f"atempo={tempo:.6f}"],
                    f"{fps:.3f} → 24fps 컨폼 (프레임 1:1)")
        if fps:
            Console.warn(f"원본이 {fps:.3f}fps 입니다. 24fps 로 내보내면 프레임을 버리거나 "
                         f"겹쳐 채웁니다 — 길이를 제대로 맞추려면 리타임을 먼저 쓰세요")
        return "", [], "24fps 로 고정"

    def plan(self, job, target, info, profile):
        alpha = bool(profile.has_alpha)
        pix = ["-pix_fmt", "yuva444p10le", "-alpha_bits", "16"] if alpha else \
              ["-pix_fmt", "yuv444p10le"]
        frame, framed = self._frame(info)
        conform, tempo, rated = self._rate(info)
        sound = self.pcm(info)
        if sound != ["-an"]:
            sound += tempo
        chain = ",".join(f for f in (conform, frame) if f)
        args = ["-c:v", "prores_ks", "-profile:v", self.PROFILE, "-vendor", "apl0",
                *pix, *(["-vf", chain] if chain else []), "-r", "24", *sound]
        said = " · ".join(x for x in ("ProRes 4444 XQ", framed, rated,
                                      "알파 유지" if alpha else "") if x)
        return args, self.EXT, self.SUFFIX, said


class SmallTarget(ConvertTarget):
    KEY = "small"
    TITLE = "용량 줄이기"
    HINT = "반드시 다시 인코딩합니다. 화질이 조금 깎입니다"
    EXT = ".mp4"
    SUFFIX = "_small"

    def plan(self, job, target, info, profile):
        preset = self._preset(job)
        ext = target.suffix.lower() if target.suffix.lower() in (".mp4", ".mov") else ".mp4"
        pix, note = PixelPlan.for_x26x(profile, keep_depth=False)
        if note:
            Console.info(f"계조  {note}")
        if job._opt.h265:
            args = ["-c:v", "libx265", "-crf", str(preset["crf265"]),
                    "-preset", preset["preset"], "-tag:v", "hvc1", *pix]
            why = f"h265 crf {preset['crf265']} — {preset['note']}"
        else:
            args = ["-c:v", "libx264", "-crf", str(preset["crf264"]),
                    "-preset", preset["preset"], *pix]
            why = f"h264 crf {preset['crf264']} — {preset['note']}"
        if ext == ".mp4":
            args += ["-movflags", "+faststart"]
        args += job._audio_args(info, preset)
        return args, ext, self.SUFFIX, why


class ImageTarget(ConvertTarget):
    """이미지 한 장씩 형식만 바꾼다. 영상 목적지와 규칙이 아예 다르다."""

    KEY = "image"
    TITLE = "이미지 형식 바꾸기"
    HINT = "PNG · JPG · WEBP 로. 크기도 같이 줄일 수 있습니다"
    EXT = ".png"
    SUFFIX = "_conv"
    KIND = "image"

    ENCODERS = {
        "png": ([], ".png"),
        "jpg": (["-q:v", "2"], ".jpg"),
        "webp": (["-quality", "92"], ".webp"),
        "exr": (["-c:v", "exr", "-compression", "zip16", "-format", "half"], ".exr"),
        "dpx": (["-c:v", "dpx"], ".dpx"),
    }

    def plan(self, job, target, info, profile):
        want = (job._opt.image_format or "png").lower()
        if want not in self.ENCODERS:
            raise FramingError(f'지원하지 않는 이미지 형식입니다: {want}')
        extra, ext = self.ENCODERS[want]
        pixels = (["-pix_fmt", "gbrapf32le" if profile.has_alpha else "gbrpf32le"]
                  if want == 'exr' else ["-pix_fmt", "yuvj444p"] if want == 'jpg' else [])
        curve, said = self.curve(target.suffix.lower(), want)
        if said:
            Console.info(f"곡선  {said}")
        chain = ColorProfile.join(TransferCurve.filter(curve, profile.has_alpha) if curve else "")
        args = ["-frames:v", "1", *chain, *extra, *pixels]
        return args, ext, self.SUFFIX, f"{target.suffix.lstrip('.').upper()} → {want.upper()}"

    @staticmethod
    def curve(source: str, want: str) -> tuple[str, str]:
        """
        EXR 은 선형, PNG · JPG · WEBP 는 sRGB, DPX 는 영상 코드값(rec709)이다.
        형식만 바꾸고 곡선을 안 바꾸면 EXR → PNG 가 새까맣게, PNG → EXR 이 허옇게 나온다.
        """
        if source == ".exr" and want != "exr":
            return (("to_rec709", "선형 → Rec.709 (DPX)") if want == "dpx"
                    else ("to_srgb", "선형 → sRGB"))
        if source != ".exr" and want == "exr":
            return (("from_rec709", "Rec.709 → 선형") if source == ".dpx"
                    else ("from_srgb", "sRGB → 선형"))
        return "", ""


class FpsTarget(ConvertTarget):
    """
    프레임레이트 바꾸기. 방법이 넷이고, 무엇을 지키느냐가 다르다.

      conform  프레임 1:1. 장 수 그대로, 재생 속도만 바뀐다 (25 → 24 면 4% 느려짐).
               소리는 같은 비율로 늘리고 줄인다 (음높이 유지). VFX 플레이트의 기본.
      dup      길이 그대로. 모자라면 앞 프레임을 한 번 더, 남으면 버린다. 픽셀은 안 섞인다.
      blend    길이 그대로. 이웃 프레임을 비율대로 섞는다 (잔상).
      motion   길이 그대로. 움직임을 추정해 새 프레임을 그린다. 찢어질 수 있다.

    23.976 · 29.97 · 59.94 는 1000/1001 분수로 정확히 맞춘다 (2997/125 같은 근사가 아니라).
    코덱 · 계조 · 알파 · 색 표기 · 시작 타임코드는 원본을 따른다.
    """

    KEY = "fps"
    TITLE = "프레임레이트 바꾸기"
    HINT = "컨폼(프레임 1:1) · 복제 · 겹치기 · 모션 보간"
    EXT = ".mov"
    SUFFIX = "_fps"
    MODES = ("conform", "dup", "blend", "motion")
    NTSC = {23.976: "24000/1001", 29.97: "30000/1001", 47.952: "48000/1001",
            59.94: "60000/1001", 119.88: "120000/1001"}

    @classmethod
    def rate(cls, value) -> tuple[str, float]:
        """('24000/1001', 23.976…) — 영상에 적을 분수와 계산용 소수."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise FramingError("목표 FPS 를 숫자로 입력하세요 (예: 24 · 23.976 · 25).")
        if not 1 <= number <= 240:
            raise FramingError("목표 FPS 는 1~240 사이로 입력하세요.")
        for near, text in cls.NTSC.items():
            if abs(number - near) < 0.006:
                top, bottom = text.split("/")
                return text, int(top) / int(bottom)
        exact = FrameRate.from_value(number)
        return exact.text, exact.numerator / exact.denominator

    @staticmethod
    def tempo(ratio: float) -> str:
        """atempo 는 한 번에 0.5~2 배만 받는다. 넘으면 나눠서 건다."""
        steps = []
        while ratio > 2.0:
            steps.append("atempo=2.0"); ratio /= 2.0
        while ratio < 0.5:
            steps.append("atempo=0.5"); ratio /= 0.5
        steps.append(f"atempo={ratio:.6f}")
        return ",".join(steps)

    def plan(self, job, target, info, profile):
        mode = (job._opt.fps_mode or "conform").lower()
        if mode not in self.MODES:
            raise FramingError(f"알 수 없는 FPS 변환 방법입니다: {mode} ({' · '.join(self.MODES)})")
        text, value = self.rate(job._opt.target_fps)
        source = info.fps if info and info.fps else 0.0
        if not source:
            raise FramingError("원본 FPS 를 읽지 못했습니다.")
        if abs(source - value) < 1e-3:
            Console.info(f"원본이 이미 {value:.3f}fps 입니다 — 코덱만 원본 규격으로 다시 담습니다")

        video_filter, tempo, said = self._motion(mode, text, value, source, info)
        video, ext, coded = self._codec(target, profile)
        sound = self._sound(info, ext, tempo)
        # 장 수를 못박는다. 컨폼은 원본과 같게, 길이 유지는 원본 길이 × 목표 FPS 로.
        # (minterpolate 는 꼬리 몇 장을 못 그리고 끝난다 — 재 보니 94장이어야 할 것이 91장)
        frames = info.frames if info else 0
        expected = frames if mode == "conform" else round(frames * value / source)
        # 못박기는 모션 보간에만 — 나머지는 이미 정확하고, 못박으면 소리 꼬리가 몇 ms 잘린다
        count = ["-frames:v", str(expected)] if expected > 0 and mode == "motion" else []
        args = [*(["-vf", video_filter] if video_filter else []), "-r", text, *count,
                *video, *sound, *(info.timecode_args() if info else [])]
        if ext == ".mp4":
            args += ["-movflags", "+faststart"]
        return args, ext, f"_{self._label(value)}fps", f"{source:.3f} → {value:.3f}fps · {said} · {coded}"

    @staticmethod
    def _label(value: float) -> str:
        return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", "p")

    def _motion(self, mode, text, value, source, info):
        frames = info.frames if info else 0
        if mode == "conform":
            seconds = frames / value if frames else 0
            Console.info(f"컨폼  프레임 {frames}장 그대로 · 길이 {info.duration:.3f}초 → {seconds:.3f}초"
                         if frames else "컨폼  프레임 그대로, 재생 속도만 바뀝니다")
            return f"setpts=N/({text}*TB)", self.tempo(value / source), "컨폼 (프레임 1:1)"
        Console.info(f"길이 유지  약 {frames}장 → 약 {round(frames * value / source)}장")
        if mode == "dup":
            return f"fps={text}", "", "복제 (길이 유지)"
        if mode == "blend":
            return f"framerate=fps={text}", "", "겹치기 (길이 유지)"
        Console.warn("모션 보간은 빠른 움직임 · 가림에서 찢어질 수 있습니다 — 결과를 꼭 보세요")
        # 끝 프레임을 몇 장 복제해 두어야 마지막 구간까지 그린다 (넘친 몫은 -frames:v 가 자른다)
        return (f"tpad=stop_mode=clone:stop=4,minterpolate=fps={text}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
                "", "모션 보간 (길이 유지)")

    @staticmethod
    def _codec(target, profile):
        """편집용 소스(ProRes · 10bit · 알파)는 ProRes 로, 8bit 배포용은 같은 상자의 h264 로."""
        if profile.deep:
            args, why = PixelPlan.for_prores(profile)
            return args, ".mov", why
        pix, _note = PixelPlan.for_x26x(profile, keep_depth=True)
        ext = ".mov" if target.suffix.lower() == ".mov" else ".mp4"
        return ["-c:v", "libx264", "-crf", "12", "-preset", "slow", *pix], ext, "h264 (crf 12)"

    def _sound(self, info, ext, tempo):
        if not (info and info.acodec):
            return ["-an"]
        coded = self.pcm(info) if ext == ".mov" else ["-c:a", "aac", "-b:a", "256k"]
        return coded + (["-af", tempo] if tempo else [])


class GifTarget(ConvertTarget):
    KEY = 'gif'
    TITLE = 'GIF 애니메이션'
    HINT = '영상 또는 이미지 시퀀스를 움직이는 GIF로'
    KIND = 'motion'
    def plan(self, job, target, info, profile):
        return [], '.gif', '_animated', self.TITLE


class FramesTarget(ConvertTarget):
    KEY = 'frames'
    TITLE = '영상 → 이미지 시퀀스'
    HINT = '원본의 모든 프레임을 번호 붙은 이미지로'
    def plan(self, job, target, info, profile):
        return [], '', '_sequence', self.TITLE


class SequenceTarget(ConvertTarget):
    """번호 붙은 이미지 폴더를 영상 한 편으로 묶는다. Sequence 의 반대 방향."""

    KEY = "movie"
    TITLE = "시퀀스 → 영상"
    HINT = "번호 붙은 이미지 폴더를 mp4 · 누크용 마스터로 묶습니다"
    EXT = ".mp4"
    SUFFIX = "_movie"
    KIND = "sequence"

    def plan(self, job, target, info, profile):
        return [], self.EXT, self.SUFFIX, "시퀀스를 영상으로"


CONVERT_TARGETS: tuple[ConvertTarget, ...] = (
    Mp4Target(), MovTarget(), NukeTarget(), DeliveryTarget(), SmallTarget(), FpsTarget(),
    ImageTarget(), SequenceTarget(), GifTarget(), FramesTarget())
CONVERT_BY_KEY = {t.KEY: t for t in CONVERT_TARGETS}


class ConvertJob(TranscodeJob):
    KEY = "convert"
    ALIASES = ("mp4", "to-mp4", "mov", "to-mov", "prores", "nuke",
               "shrink", "compress", "small")
    TITLE = "Convert"
    HINT = "영상 · 이미지 · 시퀀스를 원하는 형식으로"
    ACCEPTS_IMAGE = True
    ACCEPTS_FOLDER = True
    NOTE = """
    영상 · 이미지 · 번호 붙은 시퀀스 폴더를 다 받습니다.
    넣은 것과 안 맞는 목적지를 골라 두면 맞는 쪽으로 알아서 옮기고 알려줍니다.

    이미지를 넣으면
      PNG · JPG · WEBP 로 바꿉니다. --max-height 로 크기도 같이 줄일 수 있습니다.
      (재보니 같은 그림이 PNG 153KB → JPG 24KB → WEBP 18KB 였습니다.)

    시퀀스 폴더를 넣으면
      영상 한 편으로 묶습니다. Sequence 의 반대 방향입니다.
      --prores-movie 를 붙이면 누크용 ProRes 로 묶고, 알파가 있으면 4444 로 갑니다.
      번호가 1부터 시작하지 않거나 중간이 비어 있어도 됩니다 —
      패턴을 짜 맞추지 않고 파일 목록을 그대로 먹입니다.

    영상을 넣으면, 어디로 보낼지 하나만 고르면 됩니다.

      MP4 (배포·업로드)
        가장 잘 열리는 형식입니다. 안에 든 코덱이 mp4 가 받는 것(h264 / hevc /
        mpeg4 / av1)이면 다시 인코딩하지 않고 상자만 갈아끼웁니다. 몇 초 걸리고
        화질 손실은 없습니다. 영상 데이터가 비트 단위로 그대로 옮겨갑니다.

      MOV (상자만 교체)
        확장자만 .mov 가 됩니다. h264 짜리를 .mov 로 바꿔도 안에 든 건 여전히
        h264 입니다. 화질이 좋아지지도, 편집이 수월해지지도 않습니다.
        편집용 마스터가 필요하면 아래를 쓰세요.

      누크용 마스터 (ProRes)
        Nuke · After Effects · Resolve 가 스크러빙할 때 버벅이지 않는 코덱입니다.
          · 용량이 10~30배 커집니다. 고장이 아니라 원래 그렇습니다.
          · 스크러빙 속도와 세대 손실 내성을 용량으로 사는 코덱입니다.
          · 배포·업로드용으로 쓰면 안 됩니다.
          · h264 를 ProRes 로 바꿔도 없던 화질이 생기지는 않습니다.
            이미 뭉개진 8bit 계조를 10bit 상자에 옮겨 담을 뿐입니다.
            다시 인코딩할 거라면 원본 렌더에서 바로 뽑는 게 맞습니다.
          · 알파가 있으면 알아서 ProRes 4444 로 갑니다. 422 HQ 는 알파를 못 담습니다.

      용량 줄이기
        반드시 다시 인코딩합니다. 되돌릴 수 없는 화질 손실이 생깁니다.
        원본은 그대로 두고 새 파일을 만드니, 마음에 안 들면 지우면 됩니다.
        이미 잘 압축된 파일은 오히려 커질 수 있습니다. 그러면 커졌다고 알려줍니다.

        가장 크게 줄이는 건 화질 단계가 아니라 해상도입니다. 4K → 1080p 면
        픽셀 수가 1/4 이 됩니다. 화질을 만지기 전에 "이걸 정말 4K 로 봐야 하나"를
        먼저 따져보세요.

        h265 는 같은 화질에 20~30% 더 작지만 인코딩이 2~4배 느리고,
        옛 기기나 일부 편집 프로그램에서 안 열립니다.

    공통
      · 알파 채널은 mp4 에 못 들어갑니다. 투명도가 필요하면 누크용 마스터나
        PNG 시퀀스를 쓰세요.
      · 소스의 색 규격(bt709 / 제한범위 등)을 결과에 그대로 적어 둡니다.
        안 적어 두면 다음 도구가 추측하고, 그 추측이 어긋나면 색이 밀립니다.
      · 10bit · 4:2:2 소스는 배포용으로 갈 때 8bit 로 내려갑니다. 내려간다고
        알려줍니다. 계조를 지키려면 누크용 마스터로 가세요.
"""

    @property
    def destination(self) -> ConvertTarget:
        return CONVERT_BY_KEY.get(self._opt.convert_to or "mp4", CONVERT_TARGETS[0])

    @staticmethod
    def kind_of(target: Path) -> str:
        """넣은 것이 무엇인가. 목적지를 고르는 기준이 된다."""
        if target.is_dir():
            return "sequence"
        return "image" if target.suffix.lower() in IMAGE_EXTENSIONS else "video"

    def _settle(self, target: Path) -> ConvertTarget:
        """
        고른 목적지가 넣은 것과 안 맞으면 맞는 쪽으로 데려간다.

        이미지를 넣어 놓고 "MP4 (배포)" 가 눌려 있다고 해서 거절하는 건
        도움이 안 된다. 넣은 것이 무엇인지는 우리가 이미 아니까,
        맞는 목적지로 옮기고 옮겼다고 말해 주면 된다.
        """
        kind = self.kind_of(target)
        where = self.destination
        if where.KIND == kind or (where.KEY == "gif" and kind in ("video", "sequence")):
            return where
        fallback = {"image": "image", "sequence": "movie", "video": "mp4"}[kind]
        moved = CONVERT_BY_KEY[fallback]
        Console.info(f"넣으신 것이 {'폴더(시퀀스)' if kind == 'sequence' else kind} 라"
                     f" 목적지를 '{moved.TITLE}' 로 바꿨습니다")
        return moved

    def run(self, target: Path) -> JobResult:
        where = self._settle(target)
        options = replace(self._opt, convert_to=where.KEY)
        if where.KEY in ('movie', 'gif', 'frames'):
            if where.KEY == 'frames':
                fmt = options.image_format if options.image_format in ExtractJob.SEQUENCE_ENCODERS else 'png'
                worker = ExtractJob(replace(options, at='all', count=0, every=0, frame_format=fmt), self._ff)
            else:
                worker = (GifJob if where.KEY == 'gif' else SequenceMovieJob)(options, self._ff)
            worker.should_stop = self.should_stop
            return worker.run(target)
        previous = self._opt
        self._opt = options
        try:
            return super().run(target)
        finally:
            self._opt = previous

    def _plan(self, target: Path, info: Optional[ProbeInfo]):
        where = self.destination
        Console.info(f"목적지  {where.TITLE}")
        return where.plan(self, target, info, self._color(target, info))

    def _named(self, target: Path, suffix: str, ext: str) -> Path:
        """납품만 규약대로 짓고, 나머지는 예전처럼 '원래이름 + 접미사'."""
        if self.destination.KEY != 'delivery':
            return super()._named(target, suffix, ext)
        slate = DeliverySlate.decide(target.stem, self._opt.delivery_shot,
                                     self._opt.delivery_task, self._opt.delivery_version)
        spot = slate.free_in(self._opt.out_dir or target.parent, ext,
                             overwrite=self._opt.overwrite)
        Console.info(f'이름  {spot.name}   (샷코드_테스크_v###)')
        return spot

    @staticmethod
    def _concat_list(images, fps):
        return SequenceInput.text(images, fps)


