"""대조 작업 · 보기 방식 · 소리 · 재생용 사본"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
from typing import TYPE_CHECKING

from .errors import FramingError
from .options import JobResult
from .env import VIDEO_EXTENSIONS
from .util import Console, FrameRate, Scratch, hms, link_or_copy, open_externally
from .media import ProbeInfo
from .frames import ColorProfile, TransferCurve
from .job import Job
from .compare_page import ComparePage

if TYPE_CHECKING:   # 타입 표기에만 쓰는 이름
    from .media import FFmpegTool


class CompareView(ABC):
    """
    두 소재를 나란히 놓고 보는 한 가지 방법.

    "어디가 달라졌나"는 눈으로 왔다 갔다 하며 찾는 게 제일 부정확하다.
    사람 눈은 두 그림을 번갈아 볼 때 위치를 기억하지 못한다. 그래서 도구가
    차이를 한 화면에 겹쳐 줘야 한다.
    """

    KEY = ""
    TITLE = ""
    BLURB = ""

    @abstractmethod
    def chain(self, seconds: float = 0.0) -> str:
        """[a][b] 두 입력을 받아 [v] 하나를 내놓는 filter_complex.

        seconds 는 소재 길이 — 시간에 따라 움직이는 보기가 이걸 참고한다."""


class OverlayView(CompareView):
    KEY = "overlay"
    TITLE = "겹치기"
    BLURB = "포개서 봅니다. 섞는 정도를 손잡이로 조절합니다 — 위치가 밀렸는지 볼 때"

    MIX = 0.5                   # 창에서는 손으로 조절한다. 이건 구워 낼 때의 기본값

    def chain(self, seconds: float = 0.0) -> str:
        return f"[a][b]blend=all_opacity={self.MIX:.2f}:shortest=1[v]"


class WipeView(CompareView):
    KEY = "wipe"
    TITLE = "반반 바"
    BLURB = "경계선이 좌우로 오가며 A 와 B 를 번갈아 덮습니다"

    SECONDS = 4.0               # 한 번 왕복하는 데 걸리는 시간

    def chain(self, seconds: float = 0.0) -> str:
        """
        경계가 왼쪽 끝과 오른쪽 끝 사이를 계속 오간다. 멈춰 있으면 경계 근처만 보게 된다.

        crop 으로 잘라 붙이는 방법은 쓸 수 없다. crop 의 폭은 필터를 세울 때
        한 번만 계산되고 그 뒤로는 안 변해서, 시간이 흘러도 경계가 그대로다.
        픽셀 단위로 어느 쪽을 쓸지 고르는 blend 식이라야 실제로 움직인다.

        짧은 소재는 한 번 왕복하는 시간도 같이 줄인다. 1초짜리에 4초 왕복을 쓰면
        경계가 반만 지나가고 끝나서, 정작 반대쪽은 못 보고 만다.
        """
        period = self.SECONDS
        if seconds and seconds > 0.2:
            period = max(1.0, min(self.SECONDS, seconds))
        wave = f"0.5+0.5*sin(2*PI*T/{period:.2f})"
        return f"[a][b]blend=all_expr='if(lt(X,W*({wave})),A,B)':shortest=1[v]"


class FlickerView(CompareView):
    KEY = "flicker"
    TITLE = "번갈아"
    BLURB = "A 와 B 를 빠르게 번갈아 보여줍니다. 미세한 차이는 이게 제일 잘 보입니다"

    EVERY = 6                   # 프레임 단위. 24fps 에서 초당 네 번 바뀐다

    def chain(self, seconds: float = 0.0) -> str:
        return "[a][b]blend=all_expr='if(eq(mod(floor(T/0.26),2),0),B,A)':shortest=1[v]"


class SideBySideView(CompareView):
    KEY = "side"
    TITLE = "나란히"
    BLURB = "왼쪽 A, 오른쪽 B. 전체 인상을 비교할 때 씁니다"

    def chain(self, seconds: float = 0.0) -> str:
        return "[a][b]hstack=inputs=2:shortest=1[v]"


COMPARE_VIEWS: tuple[CompareView, ...] = (
    WipeView(), OverlayView(), FlickerView(), SideBySideView())
COMPARE_BY_KEY = {v.KEY: v for v in COMPARE_VIEWS}


class ConfiguredCompareView(CompareView):
    """An export snapshot of the viewer controls; registry defaults stay immutable."""
    def __init__(self, base, state):
        import math
        self.base = base
        self.KEY, self.TITLE = base.KEY, base.TITLE
        self.mode = state.get('mode', base.KEY)
        def fraction(key):
            value = float(state.get(key, .5))
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise FramingError('대조 비율은 0~1이어야 합니다.')
            return value
        self.split, self.mix = fraction('split'), fraction('mix')
        self.swapped = bool(state.get('swapped'))
        self.show_b = bool(state.get('showB', True))
        self.snapshot = False

    def chain(self, seconds=0.0):
        if self.mode in ('a', 'b'):
            first = (self.mode == 'a') != self.swapped
            return '[a]null[v];[b]nullsink' if first else '[b]null[v];[a]nullsink'
        if self.KEY == 'wipe':
            return f"[a][b]blend=all_expr='if(lt(X,round(W*{self.split:.9f})),A,B)':shortest=1[v]"
        if self.KEY == 'overlay':
            return f"[a][b]blend=all_expr='A*{1-self.mix:.6f}+B*{self.mix:.6f}':shortest=1[v]"
        if self.KEY == 'flicker' and self.snapshot:
            return '[b]null[v];[a]nullsink' if self.show_b else '[a]null[v];[b]nullsink'
        return self.base.chain(seconds)


class CompareAudioPolicy:
    """Select one original audio track independently from the visual A/B order."""
    def __init__(self, ff, first, second, state=None):
        state = state or {}
        self.source = state.get('audioSource', 'auto')
        if self.source not in ('auto', 'a', 'b', 'none'):
            raise FramingError('대조 소리 선택이 올바르지 않습니다.')
        try:
            self.volume = float(state.get('audioVolume', 1))
        except (TypeError, ValueError):
            raise FramingError('대조 음량이 올바르지 않습니다.')
        if not math.isfinite(self.volume) or not 0 <= self.volume <= 1:
            raise FramingError('대조 음량은 0~100%로 설정하세요.')
        available = [bool((info := ff.probe(p)) and info.acodec) for p in (first, second)]
        if self.source == 'auto':
            self.source = 'a' if available[0] else 'b' if available[1] else 'none'
        elif self.source != 'none' and not available['ab'.index(self.source)]:
            raise FramingError(f'{self.source.upper()} 파일에 오디오가 없습니다. 다른 소리 또는 소리 끄기를 선택하세요.')

    def arguments(self, swapped=False, seconds=0):
        if self.source == 'none' or self.volume == 0:
            return ['-an']
        index = 'ab'.index(self.source) ^ bool(swapped)
        # Audio follows the shared timeline; pad short audio to the final video length.
        filters = f'aresample=async=1:first_pts=0,volume={self.volume:.8g}'
        if seconds > 0:
            filters += f',apad,atrim=duration={seconds:.12g}'
        return ['-map', f'{index}:a:0', '-af', filters,
                '-c:a', 'aac', '-b:a', '192k', '-ac', '2']


class CompareBurner:
    """
    보고 있는 대조를 파일 한 장으로 굽는다 — 영상이면 mp4, 그림이면 png.

    창에서 "mp4 로 저장" 을 눌러도, --keep 으로 남길 때도 전부 여기로 온다.
    굽는 자리는 한 곳이어야 창에서 본 것과 파일이 어긋나지 않는다.
    """

    def __init__(self, ff: "FFmpegTool") -> None:
        self._ff = ff

    def name_for(self, first: Path, view: CompareView, video: bool,
                 folder: Optional[Path] = None) -> Path:
        """원본 옆에, 무엇을 어떻게 비교한 것인지 이름만 봐도 알게."""
        where = folder or first.parent
        ext = ".mp4" if video else ".png"
        base = f"대조_{view.KEY}_{first.stem}"
        destination = where / (base + ext)
        turn = 2
        while destination.exists():                 # 있던 것을 덮지 않는다
            destination = where / f"{base}_{turn}{ext}"
            turn += 1
        return destination

    def burn(self, first: Path, second: Path, view: CompareView, destination: Path,
             size: tuple[int, int] = (0, 0), seconds: float = 0.0,
             video: bool = True, fps: float = 24.0, region=None,
             output: str = '', frame=None, audio=None, swapped=False) -> Optional[Path]:
        import math
        width, height = size
        output = output or ('video' if video else 'frame')
        fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
               if width and height else "setsar=1")
        timeline = f"setpts=PTS-STARTPTS,fps={FrameRate.from_value(fps).text}:round=up:eof_action=pass," if video else ''
        # RGB planes keep a wipe boundary on the exact pixel, without chroma subsampling.
        chain = f"[0:v]{timeline}{fit},format=gbrp[a];[1:v]{timeline}{fit},format=gbrp[b];"
        if isinstance(view, ConfiguredCompareView):
            view.snapshot = output == 'frame'
        chain += view.chain(seconds if video else 0.0).replace('[v]', '[composite]')
        current = 'composite'
        if region:
            x, y, rw, rh = region
            if view.KEY == 'side' and getattr(view, 'mode', '') not in ('a', 'b'):
                chain += (f';[{current}]split[cl][cr];[cl]crop={rw}:{rh}:{x}:{y}:exact=1[l];'
                          f'[cr]crop={rw}:{rh}:{width+x}:{y}:exact=1[r];[l][r]hstack[region]')
            else:
                chain += f';[{current}]crop={rw}:{rh}:{x}:{y}:exact=1[region]'
            width, height = rw, rh
            current = 'region'
        if output == 'frame' and video:
            chain += f";[{current}]select=eq(n\\,{int(frame or 0)})[picked]"
            current = 'picked'
        # H.264 4:2:0 is playable in browsers, including odd-sized selections.
        final_filter = 'pad=ceil(iw/2)*2:ceil(ih/2)*2' if output == 'video' else 'null'
        chain += f';[{current}]{final_filter}[v]'
        frames = FrameRate.frame_count(seconds, fps) if video and seconds > 0 else None
        if output == 'video':
            pixel = 'yuv420p'
            tail = ['-c:v', 'libx264', '-crf', '14', '-preset', 'medium',
                    '-pix_fmt', pixel, '-movflags', '+faststart']
            audio = audio or CompareAudioPolicy(self._ff, first, second)
            duration = frames / fps if frames else seconds
            tail += audio.arguments(swapped=swapped, seconds=duration)
            if frames:
                # Trim the video filter instead of -frames:v, which can end audio early.
                chain = chain.replace('[v]', f'[untrimmed];[untrimmed]trim=end_frame={frames}[v]')
                tail += ['-t', f'{duration:.12g}']
        else:
            tail = ['-c:v', 'png', '-pix_fmt', 'rgb24', '-fps_mode', 'passthrough']
            if output == 'frame': tail += ['-frames:v', '1', '-update', '1']
            elif frames: tail += ['-frames:v', str(frames)]
            if output == 'frames': tail += ['-start_number', '1']
        destination.parent.mkdir(parents=True, exist_ok=True)
        code = self._ff.run(["-y", "-i", str(first), "-i", str(second),
                             "-filter_complex", chain, "-map", "[v]",
                             *tail, str(destination)],
                            total_seconds=(seconds if video else 0.0))
        exists = any(destination.parent.glob('frame.*.png')) if output == 'frames' else destination.exists()
        if code != 0 or not exists:
            return None
        return destination


class PlayableProxy:
    """
    브라우저가 열 수 있는 형태로 바꿔 준다.

    <video> 가 실제로 여는 건 mp4(h264 8bit) 와 webm 정도다.
    ProRes · DNxHD · HEVC · 10bit · .mov 상자는 화면이 그냥 까맣게 나온다.
    작업용 소재는 대부분 그런 것들이라, 원본을 그대로 넘기면 대조 자체가 안 된다.
    그래서 볼 수 있는 형태로 한 벌 만들어 두고, 그걸 본다.

    이건 어디까지나 보기용이다. --keep 으로 굽는 대조 영상은 원본을 쓴다.
    """

    SAFE_CODEC = frozenset({"h264", "vp8", "vp9", "av1"})
    SAFE_BOX = frozenset({".mp4", ".m4v", ".webm"})
    SAFE_IMAGE = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
    TALL = 1440                 # 이보다 크면 보기용으로만 줄인다

    def __init__(self, ff: "FFmpegTool", folder: Path, cancelled=None) -> None:
        self._ff = ff
        self._folder = folder
        self._cancelled = cancelled or (lambda: False)

    def _check_cancelled(self):
        if self._cancelled():
            raise FramingError("중단했습니다.")

    @classmethod
    def why(cls, path: Path, info: Optional[ProbeInfo], video: bool) -> str:
        """바꿔야 하는 이유. 빈 문자열이면 그대로 써도 된다."""
        suffix = path.suffix.lower()
        if not video:
            return "" if suffix in cls.SAFE_IMAGE else f"{suffix.lstrip('.').upper()} 는 브라우저가 못 엽니다"
        if suffix not in cls.SAFE_BOX:
            return f"{suffix} 상자는 브라우저가 못 엽니다"
        if info and info.vcodec and info.vcodec not in cls.SAFE_CODEC:
            return f"{info.vcodec} 는 브라우저가 못 엽니다"
        if info and ColorProfile.from_probe(info).bits > 8:
            return "10bit 는 브라우저가 못 엽니다"
        allowed_audio = {'opus', 'vorbis'} if suffix == '.webm' else {'aac', 'mp3'}
        if info and info.acodec and info.acodec not in allowed_audio:
            return f"{info.acodec} 오디오를 재생용으로 바꿉니다"
        if info and info.pix_fmt and info.pix_fmt not in ('yuv420p', 'yuvj420p'):
            return "영상 픽셀 형식을 재생용으로 바꿉니다"
        return ""

    def make(self, path: Path, info: Optional[ProbeInfo], video: bool,
             name: str) -> Optional[Path]:
        """
        볼 수 있는 한 벌.

        한 번 해 보고 안 되면 포기하지 않는다. 작업용 소재는 별게 다 있어서
        (홀수 크기, 이상한 프레임레이트, x264 가 빠진 ffmpeg 빌드…) 첫 판에
        실패하는 일이 실제로 생긴다. 그때 그냥 원본을 넘기면 까만 화면이 나오고,
        사람은 프로그램이 고장 났다고 생각한다. 그래서 순서대로 몇 번 더 해 본다.
        """
        seconds = info.duration if info else 0.0
        for order, plan in enumerate(self._plans(path, info, video, name), 1):
            self._check_cancelled()
            out, args = plan
            if out.exists():
                try:
                    out.unlink()
                except OSError:
                    pass
            code = self._ff.run(args, quiet=True, total_seconds=seconds)
            self._check_cancelled()
            if code == 0 and out.exists() and out.stat().st_size > 1024:
                if order > 1:
                    Console.info(f"{path.name} 은 {order}번째 방법으로 바꿨습니다")
                return out
        return None

    def _plans(self, path: Path, info: Optional[ProbeInfo], video: bool,
               name: str) -> list[tuple[Path, list[str]]]:
        """해 볼 방법들. 위에서부터 하나씩, 되는 게 나올 때까지."""
        if not video:
            png = self._folder / f"{name}.png"
            plain = [
                (png, ["-y", "-i", str(path), "-frames:v", "1",
                       "-pix_fmt", "rgb24", str(png)]),
                (png, ["-y", "-i", str(path), "-vf", "format=rgb24",
                       "-frames:v", "1", str(png)]),
            ]
            if path.suffix.lower() != ".exr":
                return plain
            # EXR 은 선형이라 그냥 뽑으면 어둡게 나온다 — sRGB 곡선을 씌워 본다 (누크 뷰어 sRGB 와 같은 그림)
            return [(png, ["-y", "-i", str(path), "-vf", TransferCurve.filter("to_srgb"),
                           "-frames:v", "1", "-pix_fmt", "rgb24", str(png)])] + plain

        mp4 = self._folder / f"{name}.mp4"
        webm = self._folder / f"{name}.webm"
        shrink = f"scale=-2:{self.TALL}," if info and info.height > self.TALL else ""
        even = "scale=trunc(iw/2)*2:trunc(ih/2)*2,"     # 홀수 크기는 x264 가 거른다
        return [
            (mp4, ["-y", "-i", str(path), "-vf", f"{shrink}format=yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-movflags", "+faststart", "-map", "0:v:0", "-map", "0:a:0?", "-c:a", "aac", "-ac", "2", str(mp4)]),
            # 홀수 크기 · 이상한 타임스탬프까지 다림질해서 다시
            (mp4, ["-y", "-fflags", "+genpts", "-i", str(path),
                   "-vf", f"{shrink}{even}format=yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
                   "-movflags", "+faststart", "-map", "0:v:0", "-map", "0:a:0?", "-c:a", "aac", "-ac", "2", str(mp4)]),
            # x264 가 없는 빌드도 있다. webm 은 브라우저가 똑같이 잘 연다
            (webm, ["-y", "-i", str(path), "-vf", f"{shrink}{even}format=yuv420p",
                    "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34",
                    "-deadline", "realtime", "-cpu-used", "8", "-map", "0:v:0", "-map", "0:a:0?", "-c:a", "libopus", "-ac", "2", str(webm)]),
        ]


class CompareJob(Job):
    """두 파일을 나란히 놓고 무엇이 달라졌는지 본다."""

    KEY = "compare"
    ALIASES = ("diff", "ab")
    TITLE = "Compare"
    HINT = "두 영상 · 이미지를 나란히 놓고 달라진 곳 찾기"
    NOTE = """
    파일 두 개를 넣으세요. 먼저 넣은 것이 A, 나중이 B 입니다.
    (폴더를 넣으면 그 안의 앞의 두 개를 씁니다.)

    실행하면 대조 창이 바로 열립니다. 인코딩을 기다릴 것도, 파일이 쌓일 것도 없습니다.

    창 안에서 네 가지를 오갈 수 있습니다  (숫자키 1~4)
      1 반반 바   경계선을 잡고 좌우로 끌면 A 와 B 가 갈립니다
      2 겹치기    포개서 봅니다. 섞기 손잡이로 0~100% 조절 — 위치가 밀렸는지 볼 때
      3 번갈아    빠르게 교대합니다. 미세한 차이는 이게 제일 잘 보입니다
      4 나란히    왼쪽 A, 오른쪽 B. 여기 안에 "둘 다 · A만 · B만" 이 같이 있습니다

      바 끌기 · ← → 로 미세 조정 (Shift 를 누르면 더 잘게)
      A · B 키로 한쪽만 · Space 재생·멈춤
      영상이면 두 쪽이 같은 지점을 보도록 재생 위치를 계속 맞춰 둡니다.

    --view 로 어느 보기로 열지 정합니다 (wipe · overlay · flicker · side).

    남는 것 없이 봅니다
      대조는 결과물이 아니라 확인하는 행위입니다. 볼 때마다 원본 폴더에 파일이 쌓이면
      정작 필요한 파일이 묻힙니다. 그래서 임시 자리에 만들고, Framing 을 닫을 때 지웁니다.
      남겨서 남에게 보내야 하면 --keep 을 붙이세요. 그때는 원본 옆에 폴더를 만들고
      고른 보기로 구운 대조 영상도 같이 넣어 줍니다.

    브라우저가 못 여는 형식이면 알아서 바꿔 놓고 봅니다
      창의 재생기가 실제로 여는 건 mp4(h264 8bit) 와 webm 정도입니다.
      ProRes · DNxHD · HEVC · 10bit · .mov 상자는 아무 말 없이 까만 화면만 나옵니다.
      그래서 미리 알아보고, 못 여는 형식이면 볼 수 있는 한 벌을 만들어 그걸 겁니다.
      창 위쪽에 "보기용으로 변환함" 이라고 붙습니다 — 원본 픽셀이 아니라는 뜻입니다.
      원본은 손대지 않고, --keep 으로 굽는 대조 영상도 원본에서 만듭니다.
      1440p 가 넘으면 보기용만 그 크기로 줄입니다. 줄였다고 알려줍니다.

    알아둘 것
      · 해상도가 다르면 A 기준 캔버스에 비율을 유지해 맞추고 검은 여백을 넣습니다.
      · 길이가 다르면 짧은 쪽에서 끝납니다.
      · 색 규격이 서로 다르면 그것부터 알려줍니다. 그게 차이의 원인일 때가 많습니다."""
    PAIRED = True
    NEEDS_FFMPEG = True
    ACCEPTS_FOLDER = True
    ACCEPTS_IMAGE = True

    def run_pair(self, first: Path, second: Path) -> JobResult:
        result = JobResult(target=first)
        keeping = bool(self._opt.keep_compare)
        folder = (self._output_dir(first, "_compare") if keeping
                  else Scratch.folder("compare"))
        view = COMPARE_BY_KEY.get(self._opt.view or "wipe", COMPARE_VIEWS[0])
        left, right = self._probe_both(first, second)
        video = self._is_video(first, left)
        if video != self._is_video(second, right):
            raise FramingError('대조는 영상 두 개 또는 이미지 두 개를 선택하세요.')
        durations = [p.duration for p in (left, right) if p and p.duration > 0]
        seconds = min(durations) if durations else 0.0

        Console.info(f"보기  {view.TITLE} — {view.BLURB}")
        size = (left.width, left.height) if left else (0, 0)
        page = ComparePage(first.name, second.name, video, size, start=view.KEY,
                           fps=(left.fps if left and left.fps else 24.0), language=self._opt.language,
                           audio=(bool(left and left.acodec), bool(right and right.acodec)))

        # 보기용을 못 만들어도 창은 띄운다. 바깥 브라우저로 내보내 봐야 거기서도
        # 똑같이 까맣게 나올 뿐이고, 창 안에 있어야 "mp4 로 저장" 을 바로 누를 수
        # 있다 — 굽는 건 원본에서 하니 이 형식도 제대로 나온다.
        first_src, second_src, note = self._watchable(
            first, second, left, right, video, folder) or (first, second, "")

        self._abort_if_asked()
        # 앱이 있으면 창 안에서 바로 보여준다. 브라우저를 새로 띄우고 파일을
        # 남기는 건, 앱이 없어서 달리 보여줄 방법이 없을 때의 대비책이다.
        shown = False
        if self.show_inline is not None:
            shown = bool(self.show_inline(page.render("__A_SRC__", "__B_SRC__"),
                                          {"a": str(first_src), "b": str(second_src),
                                           # 구울 때는 보기용이 아니라 원본을 쓴다
                                           "sourceA": str(first), "sourceB": str(second),
                                           "width": size[0], "height": size[1],
                                           "seconds": seconds, "fps": (left.fps if left and left.fps else 24.0),
                                           "video": video, "note": note,
                                           "title": f"{first.name} ↔ {second.name}"}))
        if shown:
            Console.ok("창 안에 대조를 띄웠습니다")

        made: list[Path] = []
        if keeping or not shown:
            # 파일로 남기는 뷰어에도 볼 수 있는 쪽을 건다 — 원본을 걸면
            # 브라우저에서 열어 봐야 까만 화면이라 남기는 뜻이 없다
            made.append(self._page(first_src, second_src, folder, video, size, view,
                                   names=(first.name, second.name)))
        if keeping:
            self._abort_if_asked()
            movie = self._burn(first, second, folder, view, left, right, video)
            if movie:
                made.append(movie)

        result.outputs = made
        result.output_dir = folder if keeping else None
        result.ok = True
        result.message = f"A {first.name}  ↔  B {second.name}  ·  {view.TITLE}"

        if keeping:
            Console.info(f"파일로도 남겼습니다: {folder}")
        elif not shown:
            if open_externally(made[0]):
                Console.ok("대조 창을 열었습니다")
            else:
                Console.info(f"뷰어를 직접 여세요: {made[0]}")
            Console.info("잠깐 보는 용도라 임시 자리에 만들었습니다 "
                         "— Framing 을 닫으면 지워집니다")
        return result

    def run(self, target: Path) -> JobResult:       # 짝이 안 맞을 때만 여기로 온다
        raise FramingError("Compare 는 파일 두 개가 필요합니다. A 와 B 를 같이 넣어 주세요.")

    def _watchable(self, first: Path, second: Path, left: Optional[ProbeInfo],
                   right: Optional[ProbeInfo], video: bool,
                   folder: Path) -> Optional[tuple[Path, Path, str]]:
        """
        창에 걸 수 있는 한 쌍을 만든다.

        ProRes 나 .mov 를 그대로 걸면 재생기가 아무 말 없이 까만 화면만 보여준다.
        무엇이 잘못됐는지 알 길이 없는 게 제일 나쁘다. 그래서 미리 알아보고,
        못 여는 형식이면 볼 수 있는 한 벌을 만들어 그걸 건다.

        한쪽만 실패해도 나머지 한쪽은 살린다. 예전에는 둘 중 하나만 실패해도
        통째로 포기하고 바깥 브라우저로 넘겼는데, 거기서도 어차피 까맣게 나왔다.
        """
        reasons = [PlayableProxy.why(first, left, video),
                   PlayableProxy.why(second, right, video)]
        if not any(reasons):
            return first, second, ""

        Console.step(f"{reasons[0] or reasons[1]} — 볼 수 있는 형태로 바꾸는 중")
        maker = PlayableProxy(self._ff, folder, cancelled=self.should_stop)
        made: list[Path] = []
        stuck: list[str] = []
        for path, info, name, why in ((first, left, "viewA", reasons[0]),
                                      (second, right, "viewB", reasons[1])):
            if not why:                      # 이 쪽은 그대로 걸어도 된다
                made.append(path)
                continue
            proxy = maker.make(path, info, video, name)
            if proxy is None:
                Console.fail(f"{path.name} 은 창에서 볼 수 있는 형태로 바꾸지 못했습니다")
                said = self._ff.why_failed()
                if said:                     # ffmpeg 이 남긴 말을 그대로 — 원인을 알 수 있게
                    Console.info(f"ffmpeg: {said}")
                made.append(path)            # 원본이라도 건다 — 나머지 한쪽은 보인다
                stuck.append(path.name)
            else:
                made.append(proxy)

        if stuck:
            Console.info("그쪽은 까맣게 나옵니다. 'mp4 로 저장' 은 원본으로 굽기 때문에 "
                         "이 형식도 제대로 나옵니다")
            return made[0], made[1], f"{', '.join(stuck)} 은 창에서 못 엽니다 — mp4 로 저장을 쓰세요"

        shrunk = bool(left and left.height > PlayableProxy.TALL)
        note = "보기용으로 변환함" + (f" · {PlayableProxy.TALL}p 로 줄임" if shrunk else "")
        Console.info(f"{note} (원본은 손대지 않았습니다)")
        return made[0], made[1], note

    # --- 살펴보기 ---------------------------------------------------------

    def _probe_both(self, first: Path, second: Path
                    ) -> tuple[Optional[ProbeInfo], Optional[ProbeInfo]]:
        left = self._ff.probe(first)
        right = self._ff.probe(second)
        if left and right:
            Console.info(f"A  {left.width}x{left.height}  {left.fps:.2f}fps  "
                         f"{hms(left.duration)}  {left.vcodec or '이미지'}")
            Console.info(f"B  {right.width}x{right.height}  {right.fps:.2f}fps  "
                         f"{hms(right.duration)}  {right.vcodec or '이미지'}")
            if (left.width, left.height) != (right.width, right.height):
                Console.warn(f"해상도가 다릅니다 — B 를 {left.width}x{left.height} 로 "
                             "비율을 유지해 검은 여백으로 맞춥니다")
            self._compare_color(first, second, left, right)
        return left, right

    def _compare_color(self, first: Path, second: Path,
                       left: ProbeInfo, right: ProbeInfo) -> None:
        one = self._color(first, left)
        two = self._color(second, right)
        if one.summary == two.summary:
            return
        Console.warn("두 파일의 색 규격이 다릅니다 — 차이의 원인이 여기일 수 있습니다")
        Console.info(f"     A  {one.summary}")
        Console.info(f"     B  {two.summary}")

    @staticmethod
    def _is_video(path: Path, info: Optional[ProbeInfo]) -> bool:
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            return True
        return bool(info and info.duration > 0.2)

    # --- 만들기 -----------------------------------------------------------

    def _page(self, first: Path, second: Path, folder: Path, video: bool,
              size: tuple[int, int], view: CompareView,
              names: tuple[str, str] = ()) -> Path:
        """뷰어 옆에 두 파일을 둔다. 상대 경로가 제일 안 깨진다 (링크로 두면 즉시 끝난다)."""
        a = link_or_copy(first, folder / f"A{first.suffix.lower()}")
        b = link_or_copy(second, folder / f"B{second.suffix.lower()}")
        # 화면에 쓰는 이름은 원본 이름이라야 무엇을 보고 있는지 안다.
        # 거는 파일은 A · B 로 두되(상대 경로가 제일 안 깨진다) 이름표는 원본으로.
        show = names if names else (a.name, b.name)
        page = ComparePage(show[0], show[1], video, size, start=view.KEY,
                           fps=(self._ff.probe(first).fps or 24.0) if video else 24.0, language=self._opt.language,
                           audio=tuple(bool((info := self._ff.probe(p)) and info.acodec) for p in (first, second)) if video else (False, False))
        destination = folder / ComparePage.NAME
        destination.write_text(page.render(a.name, b.name), encoding="utf-8")
        return destination

    def _burn(self, first: Path, second: Path, folder: Path, view: CompareView,
              left: Optional[ProbeInfo], right: Optional[ProbeInfo],
              video: bool) -> Optional[Path]:
        burner = CompareBurner(self._ff)
        ext = ".mp4" if video else ".png"
        made = burner.burn(first, second, view, folder / f"대조_{view.KEY}{ext}",
                           size=(left.width if left else 0, left.height if left else 0),
                           seconds=min((p.duration for p in (left, right) if p and p.duration > 0), default=0.0),
                           fps=(left.fps if left and left.fps else 24.0), video=video)
        if made is None:
            Console.warn("대조 영상은 만들지 못했습니다 — 뷰어는 그대로 씁니다")
        return made


