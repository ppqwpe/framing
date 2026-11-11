"""리타임 묶음 · 플레이트 · RIFE · 늘림 메모 · hold · 되돌리기"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import env
from .errors import FramingError
from .env import cv2
from .util import Console, FrameRate, ImageIO
from .retime import RetimeTimeline
from .media import FFmpegTool, LocalStaging, OutputColorTags


"""A durable retime package with complete frames and per-frame provenance."""


def rebind(ffmpeg: str, args: Sequence[str], source: str) -> subprocess.CompletedProcess:
    """
    플레이트를 되묶는 ffmpeg 한 번. 색 표기는 프레임에도 찍고(OutputColorTags),
    원본의 시작 타임코드를 그대로 단다 — 늘려도 줄여도 시작점은 원본과 같다.
    """
    info = FFmpegTool().probe(Path(source)) if source and Path(source).exists() else None
    stamped = OutputColorTags.fold(list(args))
    if info and info.timecode:
        stamped = stamped[:-1] + info.timecode_args() + stamped[-1:]
    staging = LocalStaging(stamped)               # 네트워크 드라이브면 로컬에서 만든 뒤 옮긴다
    done = subprocess.run([ffmpeg, *staging.args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    done.returncode = staging.finish(done.returncode)
    return done


class RetimeBundle:
    def __init__(self, folder, source):
        self.folder, self.source = Path(folder), Path(source)
        self.frames = self.folder / 'frames'
        self.interpolated = self.folder / 'interpolated'
        self.manifest = self.folder / 'retime.json'
        self.frames.mkdir(parents=True, exist_ok=True)
        self.write({'status': 'incomplete', 'source': str(self.source)})

    def write(self, data):
        self.manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def finish(self, report, frames, movie, abort):
        entries, copies = [], []
        engine = report.payload()['engine']
        for index, (path, changed) in enumerate(zip(frames, report.marks())):
            abort()
            kind = ('repeated' if engine == 'dup' else 'interpolated') if changed else 'source'
            entry = {'frame': index + 1, 'time': index / report.plan.fps,
                     'sourcePosition': report.at(index), 'kind': kind,
                     'file': path.relative_to(self.folder).as_posix()}
            if kind == 'interpolated':
                self.interpolated.mkdir(exist_ok=True)
                copied = self.interpolated / path.name
                shutil.copy2(path, copied)
                copies.append(copied)
                entry['interpolatedFile'] = copied.relative_to(self.folder).as_posix()
            entries.append(entry)
        self.write({'status': 'complete', 'source': str(self.source),
                    'movie': movie.relative_to(self.folder).as_posix(),
                    'fps': report.plan.fps, 'engine': report.payload()['engine'],
                    'sourceFrames': report.plan.source, 'outputFrames': len(frames),
                    'frameNumbering': 'Files start at 1; sourcePosition starts at 0',
                    'frames': entries})
        return copies + [self.manifest]

"""
플레이트를 지키면서 RIFE 로 사이를 채우는 왕복.

짧은 mov 플레이트를 늘려야 하는데, 늘리는 순간 플레이트가 죽는 게 문제였다.
프레임을 통째로 RIFE 에 넣으면 원본까지 8bit 로 한 번 다녀오기 때문이다.

그래서 **원본 프레임은 RIFE 를 거치지 않는다.** 사이만 채운다.

    plate/     원본 N장 · 소스 계조 그대로 (16bit)   ← 플레이트. 손대지 않는다
    for_rife/  같은 N장의 8bit 복사본                ← RIFE 에게만 주는 것
    rife/      RIFE 가 돌려준 촘촘한 격자             ← 8bit
    → 결과      원본 자리는 plate/ 에서, 새 자리만 rife/ 에서

정수배(2·4·8)로만 뽑는 이유도 같다. 배수 M 으로 뽑으면 촘촘한 격자의 M 칸마다
원본이 **정확히** 놓인다. 임의 비율로 한 번에 늘리면 원본이 격자 위에 안 떨어져서
전부 새로 그려진다 — 24→48 을 그냥 시키면 48장 중 46장이 새 그림이었다.
"""


# ══════════════════════════════════════════════════ 플레이트가 무엇인가

@dataclass(frozen=True)
class PlateSpec:
    """되돌릴 때 필요한 플레이트의 모든 것. 여기 없는 값은 지어내지 않는다."""

    source: str
    frames: int
    fps: float
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    codec: str = ""
    primaries: str = ""
    transfer: str = ""
    matrix: str = ""
    crange: str = ""

    @property
    def deep(self) -> bool:
        """8bit 4:2:0 위에 있는가 — 그렇다면 16bit PNG 로 떠야 한다."""
        fmt = self.pix_fmt.lower()
        return any(mark in fmt for mark in ("10", "12", "14", "16", "444", "422"))

    @property
    def read_pix_fmt(self) -> str:
        return "rgb48le" if self.deep else "rgb24"

    def tag_args(self) -> list[str]:
        """소스가 적어 둔 것만 도장 찍는다 — 없는 걸 지어내면 색이 밀린다."""
        pairs = (("-color_primaries", self.primaries), ("-color_trc", self.transfer),
                 ("-colorspace", self.matrix), ("-color_range", self.crange))
        return [bit for flag, value in pairs if value for bit in (flag, value)]

    @classmethod
    def read(cls, probe: str, source: Path) -> "PlateSpec":
        """ffprobe 한 줄짜리 출력에서 뽑는다."""
        got = dict(line.split("=", 1) for line in probe.strip().splitlines() if "=" in line)
        vague = {"", "unknown", "unspecified", "reserved", "N/A", "n/a"}
        clean = lambda key: "" if got.get(key, "") in vague else got.get(key, "")
        rate = got.get("r_frame_rate", "24/1")
        try:
            top, bottom = rate.split("/")
            fps = float(top) / float(bottom or 1)
        except (ValueError, ZeroDivisionError):
            fps = 24.0
        return cls(source=str(source), frames=int(got.get("nb_read_frames") or 0),
                   fps=fps or 24.0,
                   width=int(got.get("width") or 0), height=int(got.get("height") or 0),
                   pix_fmt=clean("pix_fmt"), codec=clean("codec_name"),
                   primaries=clean("color_primaries"), transfer=clean("color_transfer"),
                   matrix=clean("color_space"), crange=clean("color_range"))


# ══════════════════════════════════════════════════ 어느 칸을 고를 것인가

@dataclass(frozen=True)
class Pick:
    """결과 프레임 한 장이 어디서 오는가."""

    out_index: int                      # 결과 몇 번째 (0부터)
    dense_index: int                    # 촘촘한 격자의 몇 번째
    source_index: Optional[int]         # 원본 그 자체면 원본 번호, 아니면 None
    snapped: bool = False               # 원본을 잡으려고 살짝 당겼는가

    @property
    def from_plate(self) -> bool:
        return self.source_index is not None


class DenseGrid:
    """
    RIFE 를 몇 배로 돌리고, 그 격자에서 어느 칸을 고를지.

    배수 M 으로 뽑으면 격자 칸 d 는 원본 위치 d/M 이다. 그래서 d 가 M 의 배수면
    그 칸은 **원본 그 자체**다 — RIFE 결과가 아니라 plate/ 에서 꺼내 쓰면 된다.

    목표 장수가 격자에 딱 안 떨어질 때는 가장 가까운 칸을 고른다. 격자가 촘촘해서
    (M=4 면 원본 한 장을 넷으로 나눈 것) 밀리는 양이 0.25프레임을 넘지 않는다.
    임의 비율로 한 번에 늘릴 때 생기던 눈금 어긋남이 여기서 사라진다.
    """

    CHOICES = (2, 4, 8)                 # RIFE 가 흔히 지원하는 배수
    SNAP_WITHIN = 0.25                  # 이만큼(원본 프레임 단위) 안이면 원본으로 당긴다

    def __init__(self, source_frames: int, target_frames: int,
                 multiplier: Optional[int] = None, snap: bool = True, preserve: bool = True) -> None:
        if source_frames < 2:
            raise FramingError("플레이트가 두 장은 되어야 사이를 채울 수 있습니다.")
        if target_frames < 2:
            raise FramingError("목표 장수가 너무 적습니다.")
        self.source_frames = source_frames
        self.target_frames = target_frames
        self.snap = snap
        self.preserve = preserve
        self.multiplier = multiplier or self._decide(source_frames, target_frames)

    @classmethod
    def _decide(cls, source_frames: int, target_frames: int) -> int:
        """목표를 담을 수 있는 가장 작은 정수배. 크게 뽑을수록 느리고 커진다."""
        if target_frames <= source_frames:
            return 1                     # 줄이거나 같으면 RIFE 가 필요 없다
        for mult in cls.CHOICES:
            if mult * (source_frames - 1) + 1 >= target_frames:
                return mult
        return cls.CHOICES[-1]

    @property
    def needs_rife(self) -> bool:
        return self.multiplier > 1

    @property
    def dense_count(self) -> int:
        return self.multiplier * (self.source_frames - 1) + 1

    def picks(self) -> list[Pick]:
        """
        결과 프레임마다 격자 어느 칸에서 가져올지.

        원본을 잡으려고 당길(snap) 때 **옆 칸과 같아지면 안 된다.** 처음 짠 것이
        그 함정에 빠졌다 — 24→90 에서 당긴 자리가 다음 칸과 겹쳐 더블이 22장
        생겼고, "원본 그대로 46장" 이라고 말하는데 원본은 24장뿐이었다.
        늘리기가 더블을 만드는 바로 그 병이다. 그래서 칸 번호가 반드시
        **앞보다 커지게** 두고, 못 지키면 당기지 않는다.
        """
        mult, last = self.multiplier, self.dense_count - 1
        if self.preserve and self.target_frames >= self.source_frames:
            timeline = RetimeTimeline(self.source_frames, self.target_frames)
            out = []
            for slot in range(self.target_frames):
                source = timeline.origins.get(slot)
                if source is not None:
                    dense = source * mult
                else:
                    exact = timeline.at(slot)
                    interval = int(exact)
                    dense = min((interval + 1) * mult - 1,
                                max(interval * mult + 1, round(exact * mult)))
                out.append(Pick(slot, dense, source, False))
            return out
        span = self.target_frames - 1
        exacts = [t * last / span if span else 0.0 for t in range(self.target_frames)]
        natural = [min(last, max(0, int(round(e)))) for e in exacts]

        chosen = list(natural)
        if self.snap and mult > 1:
            for t, exact in enumerate(exacts):
                if chosen[t] % mult == 0:
                    continue                        # 이미 원본 위에 있다
                near = int(round(exact / mult)) * mult
                if not 0 <= near <= last:
                    continue
                if abs(near - exact) / mult > self.SNAP_WITHIN:
                    continue                        # 너무 많이 당겨야 한다
                behind = chosen[t - 1] if t else -1
                ahead = natural[t + 1] if t + 1 < len(natural) else last + 1
                if near <= behind or near >= ahead:
                    continue                        # 당기면 옆과 같아진다 = 더블
                chosen[t] = near

        out: list[Pick] = []
        for t, dense in enumerate(chosen):
            source = dense // mult if dense % mult == 0 else None
            out.append(Pick(t, dense, source, dense != natural[t]))
        return out

    @property
    def unavoidable_doubles(self) -> int:
        """
        격자보다 많은 장수를 달라고 하면 산수로 겹칠 수밖에 없는 장수.

        늘려 놓고 다시 줄일 때 더블이 왜 생기냐는 물음이 여기서도 똑같이 나온다.
        돌리기 전에 세어서 말해 준다.
        """
        dense = [p.dense_index for p in self.picks()]
        return sum(1 for a, b in zip(dense, dense[1:]) if a == b)

    def summary(self, picks: Optional[Sequence[Pick]] = None) -> str:
        picks = picks if picks is not None else self.picks()
        kept = sum(1 for p in picks if p.from_plate)
        doubles = sum(1 for a, b in zip(picks, picks[1:])
                      if a.dense_index == b.dense_index)
        tail = f" · 겹치는 자리 {doubles}장" if doubles else ""
        if not self.needs_rife:
            return (f"{self.source_frames} → {self.target_frames}프레임 · "
                    f"RIFE 없이 원본에서 고르기만 합니다{tail}")
        return (f"{self.source_frames} → {self.target_frames}프레임 · "
                f"RIFE {self.multiplier}배({self.dense_count}장)로 뽑아 골라냅니다 · "
                f"원본 그대로 {kept}장 · 새로 채우는 자리 {len(picks) - kept}장{tail}")


# ══════════════════════════════════════════════════ 1단계 · 내보내기

class PlateExport:
    """
    플레이트를 두 벌로 떠 놓는다 — 지킬 것과, RIFE 에게 줄 것.

    plate/ 는 소스 계조 그대로(10bit 이면 16bit PNG). for_rife/ 는 8bit 복사본이다.
    RIFE 는 8bit 밖에 못 먹는데, 그걸 원본에까지 적용하면 플레이트가 죽는다.
    그래서 처음부터 갈라 둔다.
    """

    FOLDER = "_rife"
    MANIFEST = "rife.json"

    def __init__(self, source: Path, spec: PlateSpec, grid: DenseGrid,
                 ffmpeg: str = "ffmpeg", abort: Optional[Callable[[], None]] = None) -> None:
        self._source = Path(source)
        self._spec = spec
        self._grid = grid
        self._ffmpeg = ffmpeg
        self._abort = abort or (lambda: None)

    def run(self, folder: Path) -> Path:
        folder = Path(folder)
        plate, for_rife = folder / "plate", folder / "for_rife"
        for spot in (plate, for_rife):
            spot.mkdir(parents=True, exist_ok=True)
        self._abort()
        self._explode(plate, self._spec.read_pix_fmt)
        made = sorted(plate.glob("*.png"))
        if not made:
            raise FramingError("플레이트에서 프레임을 뽑지 못했습니다.")
        Console.info(f"플레이트  {len(made)}장 · {self._spec.pix_fmt or '알 수 없음'} "
                     f"→ {'16bit' if self._spec.deep else '8bit'} PNG")
        if self._grid.needs_rife:
            self._abort()
            self._explode(for_rife, "rgb24")
            Console.info(f"RIFE 에게  {len(sorted(for_rife.glob('*.png')))}장 · 8bit 복사본")
        else:
            shutil.rmtree(for_rife, ignore_errors=True)
        self._write_manifest(folder, made)
        return folder

    def _explode(self, spot: Path, pix_fmt: str) -> None:
        done = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-i", str(self._source),
             "-pix_fmt", pix_fmt, "-start_number", "0", str(spot / "%06d.png")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise FramingError(f"프레임을 뽑지 못했습니다: {done.stderr.strip()[:200]}")

    def _write_manifest(self, folder: Path, plate_files: Sequence[Path]) -> None:
        picks = self._grid.picks()
        data = {
            "tool": "framing-plate-rife", "version": 1,
            "status": "awaiting-rife" if self._grid.needs_rife else "ready",
            "plate": asdict(self._spec),
            "grid": {"multiplier": self._grid.multiplier,
                     "denseCount": self._grid.dense_count,
                     "targetFrames": self._grid.target_frames,
                     "snap": self._grid.snap, "preserveOriginals": self._grid.preserve},
            "frameNumbering": "모든 번호는 0부터. 파일 이름도 000000 부터.",
            # 원본이 바뀌지 않았다는 걸 나중에 증명할 수 있게 지문을 남긴다
            "plateDigest": self._digest(plate_files),
            "picks": [{"out": p.out_index, "dense": p.dense_index,
                       "source": p.source_index, "snapped": p.snapped} for p in picks],
        }
        (folder / self.MANIFEST).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _digest(files: Sequence[Path]) -> str:
        """플레이트 전체의 지문 하나. 중간에 바뀌면 가져올 때 걸린다."""
        pot = hashlib.sha256()
        for path in files:
            pot.update(path.name.encode())
            pot.update(hashlib.sha256(path.read_bytes()).digest())
        return pot.hexdigest()[:32]


# ══════════════════════════════════════════════════ 2단계 · RIFE

@dataclass(frozen=True)
class RifeCommandPlan:
    """ncnn maps output i to input i*N/output_count, so request N*multiplier."""
    source_frames: int
    multiplier: int

    def __post_init__(self):
        if self.source_frames < 2 or self.multiplier not in (2, 4, 8):
            raise FramingError('RIFE 입력은 2장 이상, 배수는 2·4·8이어야 합니다.')

    @property
    def output_count(self):
        return self.source_frames * self.multiplier

    @property
    def dense_count(self):
        return (self.source_frames - 1) * self.multiplier + 1


class RifeRunner:
    """Adapt the external ncnn CLI, validate its model and await cancellable output."""

    def __init__(self, program=None, abort=None):
        bundled = env.RIFE_HOME / ('rife-ncnn-vulkan' if sys.platform == 'darwin' else 'rife-ncnn-vulkan.exe')
        self._program = str(program) if program else str(bundled) if bundled.is_file() else '' 
        self._abort = abort or (lambda: None)

    @property
    def ready(self):
        return (bool(self._program) and Path(self._program).is_file()
                and (sys.platform != 'darwin' or (not self._program.lower().endswith('.exe')
                     and os.access(self._program, os.X_OK))))

    def _model(self):
        if not self.ready:
            return Path('rife-v4.6')
        root = Path(self._program).resolve().parent
        for name in ('rife-v4.6', 'rife-v4'):
            for parent in (root, root / 'models'):
                folder = parent / name
                if folder.is_dir() and any(folder.glob('*.param')) and any(folder.glob('*.bin')):
                    return folder
        raise FramingError('RIFE 실행 파일 옆에 rife-v4.6 또는 rife-v4 모델 폴더가 필요합니다. 공식 배포본의 모델을 함께 두세요.')

    def command(self, source, out, multiplier):
        plan = RifeCommandPlan(len(list(Path(source).glob('*.png'))), multiplier)
        return [str(Path(self._program).resolve()) if self.ready else 'rife-ncnn-vulkan',
                '-i', str(Path(source).resolve()), '-o', str(Path(out).resolve()),
                '-m', str(self._model()), '-n', str(plan.output_count), '-f', '%08d.png']

    def run(self, source, out, multiplier):
        plan = RifeCommandPlan(len(list(Path(source).glob('*.png'))), multiplier)
        command = self.command(source, out, multiplier)
        out.mkdir(parents=True, exist_ok=True)
        if any(out.iterdir()):
            raise FramingError('RIFE 출력 폴더가 비어 있지 않습니다. 이전 결과와 섞이지 않도록 새 폴더를 사용하세요.')
        if not self.ready:
            Console.warn('RIFE 실행 파일이 등록되지 않았습니다 — v4 모델로 아래 명령을 실행하세요')
            Console.info(subprocess.list2cmdline(command) if os.name == 'nt' else __import__('shlex').join(command))
            return False
        self._abort()
        process = subprocess.Popen(command, cwd=str(Path(self._program).resolve().parent),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
        try:
            while True:
                self._abort()
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.terminate()
            try: process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
            raise
        if process.returncode:
            raise FramingError(f'RIFE 가 실패했습니다: {stderr.strip()[:300]}')
        files = sorted(out.glob('*.png'))
        if len(files) != plan.output_count:
            raise FramingError(f'RIFE 출력 수가 다릅니다: 예상 {plan.output_count}, 실제 {len(files)}')
        for frame in files:
            self._abort()
            if ImageIO.read(frame, cv2.IMREAD_UNCHANGED) is None:
                raise FramingError(f'RIFE 출력 이미지를 읽을 수 없습니다: {frame.name}')
        return True


class PlateAssemble:
    """
    고른 칸을 모아 결과를 만든다. 원본 자리는 plate/ 에서, 새 자리만 RIFE 결과에서.

    만들기 **전에** 먼저 따진다. 장수가 안 맞거나 플레이트가 바뀌었으면 아무것도
    만들지 않고 무엇이 어긋났는지 말한다 — 어긋난 채로 한 편을 구워 놓으면
    그게 맞는지 틀린지 나중에 알 방법이 없다.
    """

    def __init__(self, folder: Path, ffmpeg: str = "ffmpeg",
                 abort: Optional[Callable[[], None]] = None) -> None:
        self.folder = Path(folder)
        spot = self.folder / PlateExport.MANIFEST
        if not spot.exists():
            raise FramingError(f"{PlateExport.MANIFEST} 가 없습니다 — 먼저 내보내기를 하세요.")
        self.data = json.loads(spot.read_text(encoding="utf-8"))
        self.spec = PlateSpec(**self.data["plate"])
        self.grid = self.data["grid"]
        self.picks = [Pick(p["out"], p["dense"], p["source"], p.get("snapped", False))
                      for p in self.data["picks"]]
        self._ffmpeg = ffmpeg
        self._abort = abort or (lambda: None)

    # --- 따지기 -----------------------------------------------------------

    def complaints(self, rife_dir: Optional[Path] = None) -> list[str]:
        """만들기 전에 걸리는 것들. 빈 목록이면 진행해도 된다."""
        found: list[str] = []
        plate = sorted((self.folder / "plate").glob("*.png"))
        if len(plate) != self.spec.frames and self.spec.frames:
            found.append(f"플레이트가 {self.spec.frames}장이어야 하는데 {len(plate)}장입니다")
        if plate and PlateExport._digest(plate) != self.data.get("plateDigest"):
            found.append("플레이트 프레임이 내보낸 뒤에 바뀌었습니다 (지문 불일치)")
        if self.grid["multiplier"] > 1:
            spot = Path(rife_dir) if rife_dir else self.folder / "rife"
            if not spot.exists():
                found.append(f"RIFE 결과 폴더가 없습니다: {spot}")
            else:
                got = len(sorted(spot.glob("*.png")))
                want = self.grid["denseCount"]
                maximum = self.spec.frames * self.grid['multiplier']
                if not want <= got <= maximum:
                    found.append(f"RIFE 결과가 {want}~{maximum}장이어야 하는데 {got}장입니다 "
                                 f"— 배수({self.grid['multiplier']}x)가 맞는지 확인하세요")
        return found

    # --- 만들기 -----------------------------------------------------------

    def build(self, rife_dir: Optional[Path] = None,
              out_dir: Optional[Path] = None) -> tuple[Path, dict]:
        problems = self.complaints(rife_dir)
        if problems:
            raise FramingError("가져올 수 없습니다 — " + " / ".join(problems))
        plate = sorted((self.folder / "plate").glob("*.png"))
        dense = sorted(Path(rife_dir or self.folder / "rife").glob("*.png")) \
            if self.grid["multiplier"] > 1 else []
        spot = Path(out_dir or self.folder / "result")
        spot.mkdir(parents=True, exist_ok=True)
        for old in spot.glob("*.png"):
            old.unlink()

        kept = made = 0
        for pick in self.picks:
            self._abort()
            if pick.from_plate and pick.source_index < len(plate):
                shutil.copy2(plate[pick.source_index], spot / f"{pick.out_index:06d}.png")
                kept += 1
            else:
                if pick.dense_index >= len(dense):
                    raise FramingError(f"RIFE 결과에 {pick.dense_index}번 칸이 없습니다.")
                shutil.copy2(dense[pick.dense_index], spot / f"{pick.out_index:06d}.png")
                made += 1
        report = {"frames": len(self.picks), "fromPlate": kept, "fromRife": made,
                  "snapped": sum(1 for p in self.picks if p.snapped),
                  "multiplier": self.grid["multiplier"]}
        return spot, report

    def note(self, out_frames: int) -> "StretchNote":
        """되돌릴 때 쓸 쪽지. 출력 프레임마다 원본 몇 번인지(보간이면 -1)."""
        origins = [p.source_index if p.from_plate else -1 for p in self.picks]
        return StretchNote(self.spec, out_frames, origins)

    def mux(self, frames_dir: Path, destination: Path,
            note: Optional["StretchNote"] = None) -> Path:
        """플레이트와 같은 코덱·계조·색 표기로 되묶는다. 쪽지는 파일 안에 심는다."""
        done = rebind(self._ffmpeg,
            ["-v", "error", "-y", "-framerate", FrameRate.from_value(self.spec.fps).text,
             "-start_number", "0", "-i", str(Path(frames_dir) / "%06d.png"),
             *encoder_for(self.spec), *self.spec.tag_args(),
             *(note.embed_args() if note else []), "-an", str(destination)], self.spec.source)
        if done.returncode != 0 or not Path(destination).exists():
            raise FramingError(f"되묶기에 실패했습니다: {done.stderr.strip()[:200]}")
        if note:
            note.write(Path(destination))
        return Path(destination)


def encoder_for(spec: PlateSpec) -> list[str]:
    """소스 계조를 안 깎는 코덱. 10bit 이면 ProRes, 아니면 h264."""
    if spec.deep:
        return ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0",
                "-pix_fmt", "yuv422p10le"]
    return ["-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p"]


# ══════════════════════════════════════════════════ 되돌리기

class StretchNote:
    """
    늘린 영상을 다시 원래 길이로 돌리기 위한 쪽지.

    영상 옆에 파일로 두고, **영상 안에도 심는다.** 쪽지 파일은 옮기다 잃어버리고
    영상만 남는 일이 실제로 흔하다. 둘 중 하나만 있어도 되돌아가게 한다.

    담는 건 하나다 — 출력 프레임마다 그게 원본 몇 번이었는지. 보간이면 -1.
    되돌리기는 그래서 계산이 아니라 **고르기**다. -1 인 자리를 빼면 끝이다.
    """

    SUFFIX = ".늘림메모.json"
    TAG = "FRAMING_STRETCH="
    OLD_TAGS = ("SHOTKIT_STRETCH=",)          # SHOTKIT 시절 영상도 되돌릴 수 있게
    VERSION = 1

    def __init__(self, spec: PlateSpec, out_frames: int,
                 origins: Sequence[int],
                 added: Optional[Sequence[int]] = None) -> None:
        self.spec = spec
        self.out_frames = out_frames
        self.origins = list(origins)
        # 늘리면서 **생긴** 자리. 보간이면 origins 가 -1 이라 저절로 갈리지만,
        # 홀드는 원본을 한 번 더 쓴 것이라 origins 만으로는 못 가른다.
        # (끝 프레임을 22번 더 쓴 자리도 origins 로는 '원본 23번' 이다.)
        self._added = (list(added) if added is not None
                       else [i for i, origin in enumerate(self.origins) if origin < 0])

    # --- 무엇을 되돌릴 수 있나 ---------------------------------------------

    @property
    def kept(self) -> list[int]:
        """출력에서 남길 자리 — 늘리면서 생긴 자리가 아닌 칸들."""
        drop = set(self._added)
        return [i for i in range(len(self.origins)) if i not in drop]

    @property
    def added(self) -> int:
        return len(self._added)

    @property
    def restores_to(self) -> int:
        return len(self.kept)

    @property
    def restoration_problem(self) -> str:
        # Explicit drop lists use source.frames == out_frames and do not claim
        # that an earlier source timeline can be reconstructed.
        if self.spec.frames != self.out_frames:
            retained = [self.origins[i] for i in self.kept]
            if retained != list(range(self.spec.frames)):
                return (f'원본 {self.spec.frames}장의 순서가 모두 남아 있지 않습니다 '
                        f'(남길 자리 {len(retained)}장). 원본 길이 복원에는 원본 영상이 필요합니다.')
        return ''

    def summary(self) -> str:
        if self.restoration_problem:
            return f'{self.spec.frames} → {self.out_frames}프레임 · 원본 길이 복원 불가'
        return (f"{self.spec.frames} → {self.out_frames}프레임 · "
                f"늘린 자리 {self.added}장 · 되돌리면 {self.restores_to}장")

    # --- 적고 읽기 ---------------------------------------------------------

    def as_dict(self) -> dict:
        return {"tool": "framing-stretch", "version": self.VERSION,
                "source": asdict(self.spec), "outFrames": self.out_frames,
                "addedFrames": self.added, "restoresTo": self.restores_to,
                "canRestoreSource": not bool(self.restoration_problem),
                "restorationProblem": self.restoration_problem,
                "frameNumbering": "0부터. origins[i] = 출력 i번이 원본 몇 번인가, "
                                  "-1이면 보간. addedAt = 늘리면서 생긴 자리",
                "origins": self.origins, "addedAt": self._added}

    @classmethod
    def from_dict(cls, data: dict) -> "StretchNote":
        if data.get("version") != cls.VERSION:
            raise FramingError(f"모르는 메모 형식입니다 (version={data.get('version')}).")
        spec = PlateSpec(**data["source"])
        return cls(spec, int(data["outFrames"]), data["origins"], data.get("addedAt"))

    def beside(self, movie: Path) -> Path:
        return Path(movie).with_suffix(Path(movie).suffix + self.SUFFIX)

    def write(self, movie: Path) -> Path:
        spot = self.beside(movie)
        spot.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return spot

    def embed_args(self) -> list[str]:
        """영상 안에 심을 ffmpeg 인자. 쪽지를 잃어버려도 여기서 나온다."""
        packed = json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))
        return ["-metadata", f"comment={self.TAG}{packed}"]

    # --- 찾아오기 ---------------------------------------------------------

    @classmethod
    def find(cls, movie: Path, explicit: Optional[Path] = None,
             ffprobe: str = "ffprobe") -> "StretchNote":
        """쪽지 → 영상 속 → 없으면 왜 없는지 말한다. 순서가 곧 믿는 순서다."""
        movie = Path(movie)
        if explicit:
            return cls.from_dict(json.loads(Path(explicit).read_text(encoding="utf-8")))
        sidecar = movie.with_suffix(movie.suffix + cls.SUFFIX)
        if sidecar.exists():
            return cls.from_dict(json.loads(sidecar.read_text(encoding="utf-8")))
        packed = cls._embedded(movie, ffprobe)
        if packed:
            return cls.from_dict(json.loads(packed))
        raise FramingError(
            f"늘림 메모를 찾지 못했습니다. {sidecar.name} 를 영상 옆에 두거나 "
            "--note 로 경로를 주세요.")

    @staticmethod
    def _embedded(movie: Path, ffprobe: str) -> str:
        said = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format_tags=comment",
             "-of", "default=nw=1:nk=1", str(movie)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL).stdout.strip()
        for tag in (StretchNote.TAG, *StretchNote.OLD_TAGS):
            if said.startswith(tag):
                return said[len(tag):]
        return ""


class RetimeNoteText:
    """
    리타임 메모장(`_리타임메모.txt`)에서 새로 생긴 자리를 다시 읽는다.

    되돌리기의 정상 입력은 JSON 쪽지다. 그런데 사람이 손에 들고 있는 건 보통
    이 메모장이다 — 결과 옆에 떨어지고, 열어 보면 번호가 적혀 있다. 그걸 창에
    끌어다 놓으면 되게 한다.

    읽는 규칙은 하나다. **숫자와 쉼표만 있는 줄이 프레임 번호다.** 다른 줄에도
    숫자는 있지만(`길이 246 → 265프레임`) 거기엔 글자가 섞여 있다. 그래서 말이
    한국어든 영어든 같은 규칙으로 읽힌다.
    """

    SUFFIXES = (".txt",)
    ONLY_NUMBERS = re.compile(r"^[\s\d,]+$")

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @classmethod
    def handles(cls, path: Path) -> bool:
        return Path(path).suffix.lower() in cls.SUFFIXES

    def frames(self) -> list[int]:
        """적혀 있는 번호 — 사람이 세는 1부터, 정렬해서 중복 없이."""
        try:
            said = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FramingError(f"메모장을 읽지 못했습니다: {exc}")
        picked: set[int] = set()
        for line in said.splitlines():
            if line.strip() and self.ONLY_NUMBERS.match(line):
                picked.update(int(n) for n in re.findall(r"\d+", line))
        if not picked:
            raise FramingError(
                f"{self._path.name} 에서 프레임 번호를 찾지 못했습니다 — "
                "리타임 메모장이 맞는지 확인해 주세요.")
        return sorted(picked)

    def note(self, spec: PlateSpec, total: int) -> StretchNote:
        """쪽지 모양으로 옮긴다 — 되돌리는 쪽은 출처를 따지지 않는다."""
        spots = self.frames()
        if spots[-1] > total:
            raise FramingError(
                f"메모는 {spots[-1]}번까지 적혀 있는데 넣은 영상은 {total}프레임입니다 — "
                "메모와 다른 영상인 것 같습니다.")
        if len(spots) >= total:
            raise FramingError("전부 빼면 남는 프레임이 없습니다.")
        text = self._path.read_text(encoding='utf-8')
        header = re.search(r'(?:FRAMING|SHOTKIT)_TIMELINE source=(\d+) output=(\d+) restorable=(\d+)', text)
        legacy = re.search(r'(?:길이|Length)\s+(\d+)\s*→\s*(\d+)', text)
        if header or legacy:
            values = header or legacy
            source_count, output_count = int(values[1]), int(values[2])
            if output_count != total or total - len(spots) != source_count:
                raise FramingError('이 메모로 원본 길이를 복원할 수 없습니다. 원본 프레임이 모두 남아 있는지 확인하세요.')
        drop = {n - 1 for n in spots}
        origins = [-1 if i in drop else i for i in range(total)]
        return StretchNote(spec, total, origins, sorted(drop))


class DropSpec:
    """
    메모 없이 "어디를 뺄지" 를 직접 말하는 방법.

    늘릴 때 메모를 안 남겼거나 남에게 받은 영상이면 되돌릴 근거가 없다.
    그때 쓰는 차선책이다 — 메모가 있으면 그쪽이 언제나 옳다. 여기서 고른
    자리는 사람이 말한 것이지 늘릴 때 실제로 생긴 자리가 아니다.

      tail:20    뒤에서 20장
      head:20    앞에서 20장
      8,21,34    그 번호들 (사람이 세는 1부터)
      10-20      구간
    """

    WHERE = ("tail", "head")

    def __init__(self, said: str) -> None:
        self._said = (said or "").strip()

    def __bool__(self) -> bool:
        return bool(self._said)

    def targets(self, total: int) -> list[int]:
        """뺄 자리를 0부터 센 번호로 돌려준다."""
        said = self._said
        if not said:
            raise FramingError("뺄 자리를 말해 주세요 — tail:20 · head:20 · 8,21,34")
        for where in self.WHERE:
            if said.lower().startswith(where + ":"):
                return self._edge(where, said.split(":", 1)[1], total)
        return self._listed(said, total)

    @staticmethod
    def _edge(where: str, count: str, total: int) -> list[int]:
        try:
            many = int(count.strip())
        except ValueError:
            raise FramingError(f"{where}: 뒤에는 장수를 적어 주세요 (예: {where}:20)")
        if many <= 0 or many >= total:
            raise FramingError(f"{many}장은 뺄 수 없습니다 — 영상이 {total}프레임입니다")
        return list(range(total - many, total)) if where == "tail" else list(range(many))

    @staticmethod
    def _listed(said: str, total: int) -> list[int]:
        picked: set[int] = set()
        for piece in said.replace(" ", "").split(","):
            if not piece:
                continue
            first, _, last = piece.partition("-")
            try:
                low = int(first)
                high = int(last) if last else low
            except ValueError:
                raise FramingError(f"프레임 번호로 읽을 수 없습니다: {piece}")
            if low < 1 or high > total or low > high:
                raise FramingError(f"{piece} 는 1~{total} 범위를 벗어납니다")
            picked.update(range(low - 1, high))       # 사람은 1부터, 안에서는 0부터
        if not picked:
            raise FramingError("뺄 자리가 하나도 없습니다")
        if len(picked) >= total:
            raise FramingError("전부 빼면 남는 프레임이 없습니다")
        return sorted(picked)

    def note(self, spec: PlateSpec, total: int) -> StretchNote:
        """메모 모양으로 옮긴다 — 되돌리는 쪽은 출처를 따지지 않는다."""
        drop = set(self.targets(total))
        origins = [-1 if i in drop else i for i in range(total)]
        return StretchNote(spec, total, origins, sorted(drop))


class HoldPlan:
    """
    보간 없이 **끝 프레임을 붙잡아** 길이를 늘린다.

    새로 그리는 픽셀이 하나도 없다. 모든 프레임이 원본이라 플레이트가 100% 산다 —
    RIFE 를 거치는 자리조차 없고, 찢어지거나 녹을 걱정도 없다.
    끝이 거의 멈춰 있는 컷이나, 타임라인 길이만 맞추면 되는 자리에서는 이게 제일 깨끗하다.

    대신 붙잡은 자리는 그냥 멈춰 있다. 움직임이 살아 있는 꼬리에 쓰면 툭 서 버린다.
    """

    WHERE = ("tail", "head", "both")
    LABEL = {"tail": "뒤", "head": "앞", "both": "앞뒤"}

    def __init__(self, source_frames: int, target_frames: int,
                 where: str = "tail") -> None:
        if source_frames < 1:
            raise FramingError("붙잡을 프레임이 없습니다.")
        where = (where or "tail").lower()
        if where not in self.WHERE:
            raise FramingError(f"붙잡을 자리를 확인하세요 ({' · '.join(self.WHERE)}).")
        if target_frames < source_frames:
            raise FramingError(
                f"홀드는 늘리기만 합니다 — {source_frames}장을 {target_frames}장으로 "
                "줄이려면 리타임을 쓰세요.")
        self.source_frames = source_frames
        self.target_frames = target_frames
        self.where = where

    @property
    def extra(self) -> int:
        return self.target_frames - self.source_frames

    @property
    def shares(self) -> tuple[int, int]:
        """(앞에 붙일 장수, 뒤에 붙일 장수)."""
        if self.where == "head":
            return self.extra, 0
        if self.where == "tail":
            return 0, self.extra
        front = self.extra // 2
        return front, self.extra - front

    def origins(self) -> list[int]:
        """출력 프레임마다 원본 몇 번을 쓸 것인가."""
        front, back = self.shares
        last = self.source_frames - 1
        return ([0] * front + list(range(self.source_frames)) + [last] * back)

    def added(self) -> list[int]:
        """늘리면서 생긴 자리 — 되돌릴 때 여기만 빠진다."""
        front, back = self.shares
        tail_from = front + self.source_frames
        return list(range(front)) + list(range(tail_from, tail_from + back))

    def summary(self) -> str:
        front, back = self.shares
        spots = " · ".join(
            f"{name} {count}장" for name, count in
            (("앞", front), ("뒤", back)) if count)
        return (f"{self.source_frames} → {self.target_frames}프레임 · "
                f"{self.LABEL[self.where]} 홀드 ({spots or '붙일 것 없음'}) · "
                "새로 그리는 픽셀 없음")


class HoldBuilder:
    """홀드로 늘린 결과를 만든다. 프레임을 고르는 일뿐이라 섞이는 자리가 없다."""

    def __init__(self, source: Path, spec: PlateSpec, plan: HoldPlan,
                 ffmpeg: str = "ffmpeg",
                 abort: Optional[Callable[[], None]] = None) -> None:
        self._source = Path(source)
        self._spec = spec
        self._plan = plan
        self._ffmpeg = ffmpeg
        self._abort = abort or (lambda: None)

    def run(self, destination: Path, workspace: Path) -> tuple[Path, StretchNote]:
        workspace = Path(workspace)
        spread, picked = workspace / "plate", workspace / "out"
        for spot in (spread, picked):
            spot.mkdir(parents=True, exist_ok=True)
        self._abort()
        done = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-i", str(self._source),
             "-pix_fmt", self._spec.read_pix_fmt, "-start_number", "0",
             str(spread / "%06d.png")], capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise FramingError(f"프레임을 뽑지 못했습니다: {done.stderr.strip()[:200]}")
        plate = sorted(spread.glob("*.png"))
        if len(plate) < self._plan.source_frames:
            raise FramingError(f"프레임이 모자랍니다 — {self._plan.source_frames}장이어야 "
                               f"하는데 {len(plate)}장을 읽었습니다.")
        origins = self._plan.origins()
        for order, source_index in enumerate(origins):
            self._abort()
            shutil.copy2(plate[source_index], picked / f"{order:06d}.png")
        note = StretchNote(self._spec, len(origins), origins, self._plan.added())
        made = rebind(self._ffmpeg,
            ["-v", "error", "-y", "-framerate", FrameRate.from_value(self._spec.fps).text,
             "-start_number", "0", "-i", str(picked / "%06d.png"),
             *encoder_for(self._spec), *self._spec.tag_args(),
             *note.embed_args(), "-an", str(destination)], self._spec.source)
        if made.returncode != 0 or not Path(destination).exists():
            raise FramingError(f"되묶기에 실패했습니다: {made.stderr.strip()[:200]}")
        note.write(Path(destination))
        return Path(destination), note


class PlateRevert:
    """
    늘린 영상에서 **보간으로 넣은 자리만** 빼서 원래 길이로 되돌린다.

    계산이 아니라 고르기다 — 쪽지가 "출력 3번은 원본 1번" 이라고 적어 뒀으니,
    원본이었던 칸만 순서대로 모으면 된다. 어느 프레임을 버릴지 다시 추정하지
    않는다. 추정하면 늘릴 때와 뺄 때가 어긋나고, 그건 아무도 못 알아챈다.
    """

    def __init__(self, movie: Path, note: StretchNote, ffmpeg: str = "ffmpeg",
                 abort: Optional[Callable[[], None]] = None) -> None:
        self.movie = Path(movie)
        self.note = note
        self._ffmpeg = ffmpeg
        self._abort = abort or (lambda: None)

    def complaints(self, actual_frames: int) -> list[str]:
        found: list[str] = []
        if actual_frames != self.note.out_frames:
            found.append(f"메모는 {self.note.out_frames}프레임짜리를 기다리는데 "
                         f"넣은 영상은 {actual_frames}프레임입니다 "
                         "— 다른 영상이거나 잘린 것 같습니다")
        if not self.note.kept:
            found.append("메모에 원본 자리가 하나도 없습니다")
        if self.note.restoration_problem:
            found.append(self.note.restoration_problem)
        return found

    def run(self, destination: Path, workspace: Path) -> dict:
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        spread = workspace / "all"
        spread.mkdir(exist_ok=True)
        self._abort()
        done = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-i", str(self.movie),
             "-pix_fmt", self.note.spec.read_pix_fmt, "-start_number", "0",
             str(spread / "%06d.png")], capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise FramingError(f"프레임을 뽑지 못했습니다: {done.stderr.strip()[:200]}")
        frames = sorted(spread.glob("*.png"))
        problems = self.complaints(len(frames))
        if problems:
            raise FramingError("되돌릴 수 없습니다 — " + " / ".join(problems))

        picked = workspace / "picked"
        picked.mkdir(exist_ok=True)
        for order, out_index in enumerate(self.note.kept):
            self._abort()
            shutil.copy2(frames[out_index], picked / f"{order:06d}.png")
        spec = self.note.spec
        made = rebind(self._ffmpeg,
            ["-v", "error", "-y", "-framerate", FrameRate.from_value(spec.fps).text,
             "-start_number", "0", "-i", str(picked / "%06d.png"),
             *encoder_for(spec), *spec.tag_args(), "-an", str(destination)], spec.source)
        if made.returncode != 0 or not Path(destination).exists():
            raise FramingError(f"되묶기에 실패했습니다: {made.stderr.strip()[:200]}")
        return {"from": self.note.out_frames, "to": len(self.note.kept),
                "removed": self.note.added}

