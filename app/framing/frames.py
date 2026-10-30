"""프레임 저장 · 색 범위 · 정밀 추출 · 매트 · 라벨"""
from __future__ import annotations

import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Sequence
from typing import TYPE_CHECKING

from .errors import FramingError
from .env import cv2, np
from .util import ImageIO
from .media import ProbeInfo

if TYPE_CHECKING:   # 타입 표기에만 쓰는 이름
    from .media import FFmpegTool


class FrameWriter(ABC):
    @property
    @abstractmethod
    def extension(self) -> str: ...

    @property
    def params(self) -> Sequence[int]:
        return ()

    def write(self, frame: "np.ndarray", stem: Path) -> Path:
        destination = stem.with_name(stem.name + self.extension)
        if not ImageIO.write(destination, frame, self.params):
            raise FramingError(f"{self.extension} 저장 실패: {destination.name}")
        return destination


class PngWriter(FrameWriter):
    def __init__(self, compression: int = 3) -> None:
        self._compression = max(0, min(9, compression))

    @property
    def extension(self) -> str:
        return ".png"

    @property
    def params(self) -> Sequence[int]:
        return (cv2.IMWRITE_PNG_COMPRESSION, self._compression)


class JpegWriter(FrameWriter):
    """
    JPEG 는 기본이 4:2:0 이라 색 정보를 가로세로 절반으로 줄여 저장한다.
    단풍처럼 채도 높은 것이 잔가지로 얽힌 그림에서는 경계 색이 눈에 띄게 밀린다
    (재보니 경계에서 최대 51단계). 그래서 색을 줄이지 않는 4:4:4 로 저장한다.

    JPEG 는 8bit·알파 없음이 전부다. 그보다 큰 그림이 들어오면 여기서 줄여야 한다.
    """

    def __init__(self, quality: int = 95) -> None:
        self._quality = max(1, min(100, quality))

    @property
    def extension(self) -> str:
        return ".jpg"

    @property
    def params(self) -> Sequence[int]:
        return (cv2.IMWRITE_JPEG_QUALITY, self._quality,
                cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444)

    def write(self, frame: "np.ndarray", stem: Path) -> Path:
        if frame.dtype == np.uint16:
            frame = (frame.astype(np.float32) / 257.0).round().clip(0, 255).astype(np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = frame[..., :3]          # JPEG 은 투명도를 못 담는다
        return super().write(frame, stem)


def build_writers(fmt: str, quality: int) -> list[FrameWriter]:
    return {"png": [PngWriter()],
            "jpg": [JpegWriter(quality)],
            "both": [PngWriter(), JpegWriter(quality)]}[fmt]


@dataclass(frozen=True)
class ColorReading:
    """뽑은 그림이 실제로 쓰는 밝기 범위와, 영상이 스스로 말한 범위."""

    low: int
    high: int
    declared: str = ""          # 'tv'(제한) | 'pc'(전체) | '' (안 적혀 있음)

    LIMITED_LOW = 16
    LIMITED_HIGH = 235

    @property
    def looks_squeezed(self) -> bool:
        """
        눌린 그림의 확실한 자국만 잡는다 — 검정이 딱 16 에, 흰색이 딱 235 에 서 있는 것.

        "16~237 사이면 눌린 것" 같은 헐렁한 기준을 쓰면 그냥 어두운 밤 장면까지
        눌렸다고 잘못 읽어서 멀쩡한 그림을 들어 올린다. 그건 고치는 게 아니라 망치는 것이다.
        """
        black_sits_at_16 = self.LIMITED_LOW - 2 <= self.low <= self.LIMITED_LOW + 2
        white_sits_at_235 = self.LIMITED_HIGH - 3 <= self.high <= self.LIMITED_HIGH + 3
        has_real_range = (self.high - self.low) >= 100
        return black_sits_at_16 and white_sits_at_235 and has_real_range

    @property
    def mismatched(self) -> bool:
        """전체 범위라고 적어 놓고 실제로는 눌려 있는 경우 — 태그가 틀린 것이다."""
        return self.declared == "pc" and self.looks_squeezed

    @property
    def summary(self) -> str:
        said = {"tv": "제한범위", "pc": "전체범위"}.get(self.declared, "안 적힘")
        return f"밝기 {self.low}~{self.high} (영상 표기: {said})"


class LevelInspector:
    """그림이 16~235 로 눌려 있는지 재기만 한다."""

    @staticmethod
    def read(frame: "np.ndarray", declared: str = "") -> ColorReading:
        """
        채널별 최댓값이 아니라 밝기(루마)로 잰다.
        채도 높은 빨강은 한 채널만 250 을 넘길 수 있어서, 채널 최댓값으로 재면
        눌린 그림을 안 눌렸다고 잘못 읽는다.
        """
        if frame.ndim == 3:
            gray = cv2.cvtColor(frame[..., :3], cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
        scale = 257.0 if gray.dtype == np.uint16 else 1.0   # 16bit 도 0~255 자로 잰다
        low = int(np.percentile(gray, 0.1) / scale)
        high = int(np.percentile(gray, 99.9) / scale)
        return ColorReading(low=low, high=high, declared=(declared or "").strip().lower())


class LevelExpander:
    """눌린 그림(16~235)을 0~255 로 편다. 검정이 들뜨고 흰색이 탁한 것을 되돌린다."""

    LOW = 16.0
    HIGH = 235.0

    @classmethod
    def apply(cls, frame: "np.ndarray") -> "np.ndarray":
        scale = 255.0 / (cls.HIGH - cls.LOW)
        stretched = (frame.astype(np.float32) - cls.LOW) * scale
        return np.clip(stretched, 0, 255).astype(np.uint8)


@dataclass(frozen=True)
class ColorProfile:
    """
    소스가 어떤 색 규격인지에 대한 단 하나의 판단.

    한 파일을 두고 작업마다 다르게 읽으면 왔다 갔다 할 때마다 색이 밀린다.
    그래서 소스마다 이걸 한 번만 정하고, 모든 작업이 같은 걸 본다.
      · 읽을 때  : read_filter() — 표기가 틀렸으면 여기서 바로잡아 읽는다
      · 저장할 때 : tag_args()   — 읽은 그대로를 출력에 도장 찍는다
    도장을 안 찍으면 다음 도구가 제멋대로 추측하고, 그게 색이 밀리는 진짜 원인이다.
    """

    primaries: str = ""
    transfer: str = ""
    matrix: str = ""
    crange: str = ""            # 'tv' | 'pc'
    pix_fmt: str = ""
    retagged: bool = False      # 소스 표기가 틀려서 우리가 바로잡았다
    from_video: bool = False    # 이 RGB 가 영상을 디코딩해서 나온 것인가

    VAGUE = frozenset({"", "unknown", "unspecified", "reserved", "n/a"})
    ASSUMED_RANGE = "tv"        # 영상은 안 적혀 있으면 제한범위가 관례다

    # 규격이 안 적힌 영상을 ffmpeg · OpenCV 가 **실제로** 펴는 기준.
    # swscale 의 기본값은 해상도와 상관없이 늘 bt601 이다. 재서 확인했다 —
    # 1080p 무표기 파일도 bt601 로 펴진다.
    DECODER_MATRIX = "bt601"

    # --- 만들기 -----------------------------------------------------------

    @classmethod
    def from_probe(cls, info: Optional[ProbeInfo]) -> "ColorProfile":
        if info is None:
            return cls()
        clean = lambda v: "" if (v or "").lower() in cls.VAGUE else (v or "").lower()
        return cls(primaries=clean(info.color_primaries),
                   transfer=clean(info.color_transfer),
                   matrix=clean(info.color_space),
                   crange=clean(info.color_range),
                   pix_fmt=(info.pix_fmt or "").lower(),
                   from_video=True)

    def corrected(self, reading: Optional["ColorReading"]) -> "ColorProfile":
        """실제 픽셀이 표기와 어긋나면 픽셀 쪽을 믿는다. 표기는 사람이 틀리게 넣는다."""
        if reading is None or not reading.mismatched:
            return self
        return replace(self, crange="tv", retagged=True)

    # --- 소스가 무엇을 가지고 있나 -------------------------------------------

    @property
    def bits(self) -> int:
        if self.pix_fmt.startswith(('rgb48', 'bgr48', 'rgba64', 'bgra64')):
            return 16
        for depth in (16, 14, 12, 10):
            if f"p{depth}" in self.pix_fmt or f"{depth}le" in self.pix_fmt \
               or f"{depth}be" in self.pix_fmt:
                return depth
        return 8

    @classmethod
    def from_frame(cls, frame) -> "ColorProfile":
        alpha = frame.ndim == 3 and frame.shape[2] == 4
        deep = frame.dtype == np.uint16
        if frame.ndim == 2:
            pixel = 'gray16le' if deep else 'gray'
        else:
            pixel = ('bgra64le' if alpha else 'bgr48le') if deep else ('bgra' if alpha else 'bgr24')
        return cls(pix_fmt=pixel)

    @property
    def chroma(self) -> str:
        for kind in ("444", "422", "420", "411", "410"):
            if kind in self.pix_fmt:
                return kind
        if self.pix_fmt.startswith(("rgb", "bgr", "gbr", "argb", "abgr", "rgba", "bgra")):
            return "444"
        return "420"

    @property
    def has_alpha(self) -> bool:
        return self.pix_fmt.startswith(("yuva", "rgba", "bgra", "argb", "abgr",
                                        "gbrap", "ya"))

    @property
    def deep(self) -> bool:
        """8bit 4:2:0 보다 위에 있는가 — 그렇다면 함부로 내리면 안 된다."""
        return self.bits > 8 or self.chroma in ("422", "444") or self.has_alpha

    @property
    def effective_range(self) -> str:
        return self.crange or self.ASSUMED_RANGE

    # --- ffmpeg 에게 하는 말 -------------------------------------------------

    # ffprobe 가 읽어 주는 이름과 인코더가 받아 주는 이름이 늘 같지는 않다.
    # 예를 들어 RGB 소스의 매트릭스를 ffprobe 는 gbr 이라 부르는데 인코더는 rgb 만 안다.
    # 아는 값만 찍고 모르는 값은 버린다 — 못 알아듣는 이름을 넘기면 인코딩 자체가 죽는다.
    LEGAL_MATRIX = frozenset({
        "rgb", "bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco",
        "bt2020nc", "bt2020c", "smpte2085", "chroma-derived-nc",
        "chroma-derived-c", "ictcp"})
    LEGAL_PRIMARIES = frozenset({
        "bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film",
        "bt2020", "smpte428", "smpte431", "smpte432", "jedec-p22"})
    LEGAL_TRANSFER = frozenset({
        "bt709", "gamma22", "gamma28", "smpte170m", "smpte240m", "linear",
        "log100", "log316", "iec61966-2-4", "bt1361e", "iec61966-2-1",
        "bt2020-10", "bt2020-12", "smpte2084", "smpte428", "arib-std-b67"})
    RENAME = {"gbr": "rgb", "bt470bg": "bt470bg", "smpte428_1": "smpte428",
              "iec61966_2_1": "iec61966-2-1", "iec61966_2_4": "iec61966-2-4",
              "bt2020_ncl": "bt2020nc", "bt2020_cl": "bt2020c"}

    SWS_MATRIX = {"bt709": "bt709", "bt470bg": "bt601", "bt601": "bt601",
                  "smpte170m": "smpte170m", "smpte240m": "smpte240m",
                  "bt2020nc": "bt2020", "bt2020_ncl": "bt2020", "fcc": "fcc"}

    def read_filter(self) -> str:
        """
        영상을 그림(RGB)으로 펼 때 붙이는 변환식.

        표기가 틀린 파일은 -color_range 로는 안 고쳐진다. 그 값이 이미 픽셀 형식 이름에
        박혀 있어서(yuvj420p 같은) 바깥에서 하는 말을 무시하기 때문이다.
        실제로 듣는 건 scale 필터의 in_range 하나뿐이다 — 재보고 알아낸 것.

        영상→영상 재인코딩에는 필요 없다. 그쪽은 픽셀을 그대로 두고 표기만 고치면 된다.
        """
        if not self.retagged:
            return ""
        return "scale=in_range=tv:out_range=pc:flags=full_chroma_int+accurate_rnd"

    @staticmethod
    def join(*filters: str) -> list[str]:
        """비어 있지 않은 필터만 이어 붙여 -vf 인자로 만든다."""
        chain = ",".join(f for f in filters if f)
        return ["-vf", chain] if chain else []

    def range_bsf(self, encoder_args: Sequence[str]) -> list[str]:
        """
        h264 · h265 는 -color_range 만으로는 밝기 범위를 파일에 안 적을 때가 많다.
        (다른 색 정보가 하나도 없으면 그 칸 자체를 통째로 생략한다.)
        그러면 다음 도구가 또 추측하게 된다. 비트스트림에 직접 못박아 둔다.
        """
        if not self.crange:
            return []
        codec = next((a for a in encoder_args if a in ("libx264", "libx265")), "")
        name = {"libx264": "h264_metadata", "libx265": "hevc_metadata"}.get(codec)
        if not name:
            return []                       # ProRes 는 이 칸이 아예 없다. 관례상 제한범위
        flag = 1 if self.crange == "pc" else 0
        return ["-bsf:v", f"{name}=video_full_range_flag={flag}"]

    @property
    def read_matrix(self) -> str:
        """
        이 RGB 를 만들어 낸 매트릭스 (swscale 이름).

        여기서 크게 데었다. 규격이 **안 적힌** 영상을 디코더는 bt601 로 펴는데,
        되돌릴 때는 bt709 로 접고 있었다. 명암은 그대로인데 주황과 하늘색만
        눈에 띄게 밀린다 — 1080p 무표기 파일에서 픽셀의 99% 가 어긋났다.

        적혀 있으면 적힌 대로. 안 적혀 있으면 **디코더가 실제로 쓴 기준**으로.
        영상에서 온 게 아니라 진짜 그림 파일이면 되돌릴 원본이 없으니 bt709.
        """
        if self.matrix:
            return self.SWS_MATRIX.get(self.matrix, "bt709")
        return self.DECODER_MATRIX if self.from_video else "bt709"

    def to_yuv_filter(self) -> str:
        """
        RGB(이미지)를 다시 영상으로 돌릴 때 쓸 변환식.

        이걸 안 적어 주면 ffmpeg 은 RGB→YUV 를 제 기본값으로 계산해 놓고
        결과에는 딴소리를 적는다. 그 한 줄 차이로 살색과 하늘색이 밀린다.
        """
        return (f"scale=out_color_matrix={self.read_matrix}"
                f":out_range={self.effective_range}"
                ":flags=full_chroma_int+accurate_rnd")

    def tag_args(self) -> list[str]:
        """출력에 색 규격을 명시한다. 아는 것만 찍는다 — 모르는 걸 지어내지 않는다."""
        args: list[str] = []
        for flag, value, legal in (
                ("-color_primaries", self.primaries, self.LEGAL_PRIMARIES),
                ("-color_trc", self.transfer, self.LEGAL_TRANSFER),
                ("-colorspace", self.matrix, self.LEGAL_MATRIX),
                ("-color_range", self.crange, frozenset({"tv", "pc"}))):
            clean = self.RENAME.get(value, value)
            if clean and clean in legal:
                args += [flag, clean]
        return args

    def stamp_args(self) -> list[str]:
        """
        결과에 찍을 도장.

        소스가 적어 둔 게 있으면 그대로 옮긴다. 하나도 없으면 —
          · 영상을 편 프레임이면 **아무것도 안 찍는다.** 소스는 무표기인데
            결과에만 도장을 찍으면, 규격을 짐작하는 재생기에서 둘이 서로
            다른 그림이 된다. 안 적힌 것을 지어내지 않는 게 맞다.
          · 진짜 그림 파일이면 되돌릴 원본이 없으니 bt709 로 못박아 준다.
        """
        args = self.tag_args()
        if args or self.from_video:
            return args
        return ["-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709", "-color_range", "tv"]

    # --- 사람에게 하는 말 ---------------------------------------------------

    @property
    def summary(self) -> str:
        said = {"tv": "제한범위", "pc": "전체범위"}.get(self.crange, "안 적힘")
        depth = f"{self.bits}bit {self.chroma}" + (" +알파" if self.has_alpha else "")
        space = self.matrix or self.primaries or "규격 안 적힘"
        return f"{depth} · {space} · {said}"


class PixelPlan:
    """
    소스가 가진 계조·채도·알파를 깎지 않는 픽셀 형식을 고른다.

    yuv420p 로 못박아 두면 10bit 도, 4:2:2 도, 알파도 전부 조용히 사라진다.
    사라진 걸 알아채는 건 한참 뒤 합성할 때다.
    """

    LADDER = {
        8:  {"420": "yuv420p", "422": "yuv422p", "444": "yuv444p"},
        10: {"420": "yuv420p10le", "422": "yuv422p10le", "444": "yuv444p10le"},
        12: {"420": "yuv420p12le", "422": "yuv422p12le", "444": "yuv444p12le"},
    }
    SAFE = "yuv420p"            # 어디서나 열리는 최소공배수

    @classmethod
    def for_x26x(cls, profile: ColorProfile, keep_depth: bool) -> tuple[list[str], str]:
        """(픽셀 형식 인자, 사람이 읽을 한 줄). keep_depth 가 꺼져 있으면 배포용 8bit."""
        if not keep_depth:
            if profile.deep:
                return (["-pix_fmt", cls.SAFE],
                        f"{profile.bits}bit {profile.chroma} → 8bit 420 (배포용). "
                        f"계조를 지키려면 누크용 마스터로")
            return ["-pix_fmt", cls.SAFE], ""
        bits = 12 if profile.bits >= 12 else (10 if profile.bits > 8 else 8)
        chroma = profile.chroma if profile.chroma in ("420", "422", "444") else "420"
        name = cls.LADDER[bits][chroma]
        note = "" if name == cls.SAFE else f"{bits}bit {chroma} 유지"
        return ["-pix_fmt", name], note

    @classmethod
    def for_prores(cls, profile: ColorProfile) -> tuple[list[str], str]:
        """알파가 있으면 4444, 없으면 422 HQ. 알파를 422 에 넣으면 그냥 버려진다."""
        if profile.has_alpha:
            return (["-c:v", "prores_ks", "-profile:v", "4444", "-vendor", "apl0",
                     "-pix_fmt", "yuva444p10le", "-alpha_bits", "16"],
                    "ProRes 4444 (알파 유지 — 422 HQ 는 알파를 못 담습니다)")
        return (["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0",
                 "-pix_fmt", "yuv422p10le"],
                "ProRes 422 HQ")

    @classmethod
    def still_args(cls, profile: ColorProfile) -> list[str]:
        """정지 이미지로 뽑을 때. 8bit 소스는 8bit 로, 그 위는 16bit 로 받는다."""
        if profile.has_alpha:
            return ["-pix_fmt", "rgba64be" if profile.bits > 8 else "rgba"]
        return ["-pix_fmt", "rgb48be" if profile.bits > 8 else "rgb24"]


class PreciseGrab:
    """
    10bit·알파 소스에서 프레임을 뽑는 길.

    OpenCV 는 무엇을 넣든 8bit BGR 로 돌려준다. 빠르고 몇 번째 프레임인지 세기도 쉬워서
    보통은 그걸로 충분하다. 그런데 10bit ProRes 를 그렇게 뽑으면 계조가 여기서 깎이고
    알파는 아예 사라진다. 그런 소스만 ffmpeg 에게 직접 시킨다.

    번호는 이미 OpenCV 로 세어 두고 여기서는 그 번호만 다시 뽑는다.
    한 번의 디코딩으로 필요한 프레임을 전부 건진다 — 한 장씩 다시 열면 열 배 느리다.
    """

    def __init__(self, ff: "FFmpegTool", profile: ColorProfile) -> None:
        self._ff = ff
        self._profile = profile

    def available(self) -> bool:
        return bool(self._ff.find("ffmpeg"))

    def fetch(self, target: Path, indices: Sequence[int]) -> dict[int, "np.ndarray"]:
        """{프레임번호: 그림}. 실패하면 빈 사전 — 부르는 쪽이 OpenCV 로 되돌아간다."""
        wanted = sorted({max(0, int(i)) for i in indices})
        if not wanted:
            return {}
        picks = "+".join(rf"eq(n\,{i})" for i in wanted)
        workspace = Path(tempfile.mkdtemp(prefix="framing_grab_"))
        try:
            code = self._ff.run(
                ["-y", "-i", str(target),
                 *ColorProfile.join(f"select='{picks}'", self._profile.read_filter()),
                 "-fps_mode", "passthrough",
                 "-c:v", "png", *PixelPlan.still_args(self._profile), "-an",
                 str(workspace / "g.%05d.png")], quiet=True)
            made = sorted(workspace.glob("g.*.png"))
            if len(made) != len(wanted):
                # 몇 번째가 몇 번인지 확신할 수 없으면 하나도 쓰지 않는다.
                # 엉뚱한 프레임을 맞다고 내미는 것보다 원래 길로 되돌아가는 게 낫다.
                return {}
            if code != 0 and not made:
                return {}
            grabbed: dict[int, "np.ndarray"] = {}
            for number, path in zip(wanted, made):
                image = ImageIO.read(path, cv2.IMREAD_UNCHANGED)
                if image is not None:
                    grabbed[number] = image
            return grabbed
        except Exception:
            return {}                       # 어떤 이유로든 실패하면 조용히 원래 길로
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


@dataclass(frozen=True)
class HueLabel:
    """매트 패스에서 쓰인 라벨 색 하나. 색상과 채도로만 정의된다 — 밝기는 뎁스다."""

    hue: float                  # 0~360
    sat: float                  # 0~1

    NAMES = ((30, "빨강"), (70, "노랑"), (165, "초록"), (200, "청록"),
             (260, "파랑"), (330, "마젠타"), (360, "빨강"))

    @property
    def name(self) -> str:
        for edge, korean in self.NAMES:
            if self.hue < edge:
                return korean
        return "빨강"

    def __str__(self) -> str:
        return f"{self.name} (H {self.hue:.0f}° · S {self.sat * 100:.0f}%)"


class HsvBridge:
    """
    BGR 과 HSV 를 오간다. OpenCV 의 float 변환을 쓴다 — 왕복 오차가 0 이다.
    (uint8 로 돌리면 색상이 0~179 로 눌려서 라벨을 구분하기에 너무 거칠다.)
    """

    @staticmethod
    def to_hsv(bgr: "np.ndarray") -> "np.ndarray":
        top = 65535.0 if bgr.dtype == np.uint16 else 255.0
        return cv2.cvtColor(bgr[..., :3].astype(np.float32) / top, cv2.COLOR_BGR2HSV)

    @staticmethod
    def to_bgr(hsv: "np.ndarray", like: "np.ndarray") -> "np.ndarray":
        top = 65535.0 if like.dtype == np.uint16 else 255.0
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR) * top
        return np.clip(bgr, 0, top).astype(like.dtype)


class MatteReader:
    """
    소재가 매트·뎁스 패스인지 실사·페인팅인지 읽어내고, 쓰인 라벨 색을 찾아낸다.

    매트는 몇 개의 라벨색으로만 칠해져 색상이 좁은 봉우리에 몰린다.
    실사는 색상이 넓게 퍼진다. 이 차이 하나로 갈린다 — 실측 매트 83~99%, 실사 21~28%.
    잘못 판별하면 실사 소재의 색을 라벨로 뭉개 버리므로, 애매하면 실사로 본다.
    """

    BINS = 72                   # 5도 간격
    COLORED = 0.10              # 이보다 채도가 낮으면 무채색 = 뎁스
    MATTE_LINE = 0.55           # 상위 3봉우리가 이 비율을 넘으면 매트
    MIN_PIXELS = 500
    MIN_SHARE = 0.03            # 전체의 3% 미만인 색은 라벨로 치지 않는다
    NEIGHBOR = 2                # 인접 ±2 bin 은 같은 라벨로 묶는다

    def __init__(self, samples: Sequence["np.ndarray"]) -> None:
        self._hues, self._sats = self._gather(samples)

    @classmethod
    def _gather(cls, samples: Sequence["np.ndarray"]):
        hues, sats = [], []
        for frame in samples:
            if frame is None or frame.size == 0:
                continue
            small = frame
            if frame.shape[1] > 640:            # 판별에 원본 해상도는 필요 없다
                scale = 640.0 / frame.shape[1]
                small = cv2.resize(frame, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_AREA)
            hsv = HsvBridge.to_hsv(small)
            mask = hsv[..., 1] >= cls.COLORED
            if mask.any():
                hues.append(hsv[..., 0][mask])
                sats.append(hsv[..., 1][mask])
        if not hues:
            return np.empty(0, np.float32), np.empty(0, np.float32)
        return np.concatenate(hues), np.concatenate(sats)

    @property
    def concentration(self) -> float:
        if len(self._hues) < self.MIN_PIXELS:
            return 1.0                          # 색이 거의 없다 = 순수 뎁스
        hist, _ = np.histogram(self._hues, bins=self.BINS, range=(0, 360))
        return float(np.sort(hist)[-3:].sum() / max(1, hist.sum()))

    @property
    def is_matte(self) -> bool:
        if len(self._hues) < self.MIN_PIXELS:
            return True
        return self.concentration > self.MATTE_LINE

    def legend(self) -> list[HueLabel]:
        """쓰인 라벨색 목록. 무채색뿐이면 빈 목록 — 그것도 유효한 답이다."""
        if len(self._hues) < self.MIN_PIXELS:
            return []
        hist, edges = np.histogram(self._hues, bins=self.BINS, range=(0, 360))
        total = max(1, hist.sum())
        labels: list[HueLabel] = []
        used = np.zeros(self.BINS, bool)
        for slot in np.argsort(-hist):
            if hist[slot] / total < self.MIN_SHARE or used[slot]:
                continue
            near = [(slot + k) % self.BINS
                    for k in range(-self.NEIGHBOR, self.NEIGHBOR + 1)]
            if any(used[n] for n in near):
                continue
            for n in near:
                used[n] = True
            picked = self._within(edges[slot] - 12.5, edges[slot] + 17.5)
            if picked.sum() < 100:
                continue
            labels.append(HueLabel(hue=self._circular_mean(self._hues[picked]),
                                   sat=float(np.median(self._sats[picked]))))
        labels.sort(key=lambda label: -label.sat)
        return labels

    def _within(self, low: float, high: float) -> "np.ndarray":
        hues = self._hues
        if low < 0 or high > 360:               # 0도를 넘어가는 구간
            return (hues >= low % 360) | (hues < high % 360)
        return (hues >= low) & (hues < high)

    @staticmethod
    def _circular_mean(hues: "np.ndarray") -> float:
        """0도 근처를 평균 내면 180도가 나온다. 원 위에서 평균해야 한다."""
        radians = np.deg2rad(hues)
        angle = np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())
        return float(np.rad2deg(angle) % 360)


class LabelSnapper:
    """
    보간이 만들어 낸 중간색을 가장 가까운 라벨로 되돌린다.

    매트에서 빨강과 파랑 사이의 보라색 픽셀은 "반쯤 빨강"이 아니라 오류다.
    라벨은 뜻이지 색이 아니라서, 섞이는 순간 그 픽셀의 의미가 사라진다.
    명도(V)는 손대지 않는다 — 그건 뎁스이고, 뎁스는 섞여도 뜻이 유지된다.
    """

    BAND = 35.0                 # 이 각도 안쪽이면 그 라벨로 본다

    def __init__(self, legend: Sequence[HueLabel], band: float = BAND) -> None:
        self._legend = list(legend)
        self._band = band

    @property
    def describes(self) -> str:
        if not self._legend:
            return "무채색(뎁스)만 — 색이 끼면 회색으로 되돌립니다"
        return " · ".join(str(label) for label in self._legend)

    def apply(self, frame: "np.ndarray") -> "np.ndarray":
        alpha = frame[..., 3:] if frame.ndim == 3 and frame.shape[2] == 4 else None
        hsv = HsvBridge.to_hsv(frame)
        hue, sat, value = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        out_hue = np.zeros_like(hue)
        out_sat = np.zeros_like(sat)
        claimed = np.zeros(hue.shape, bool)
        for label in self._legend:
            distance = np.abs((hue - label.hue + 180) % 360 - 180)
            hit = (distance <= self._band) & (sat >= label.sat * 0.5) & ~claimed
            out_hue[hit] = label.hue
            out_sat[hit] = label.sat
            claimed |= hit
        out_sat[~claimed] = 0.0                 # 라벨이 아니면 순수 무채색
        fixed = HsvBridge.to_bgr(np.stack([out_hue, out_sat, value], -1), frame)
        if alpha is not None:
            fixed = np.concatenate([fixed, alpha], axis=2)
        return fixed


