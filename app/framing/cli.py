"""터미널 도구 — 작업 계획 · 명령줄 · 대상 모으기"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import env
from .options import JobResult, Options
from .env import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .util import Console, is_interactive, normalize_path
from .media import FFmpegTool, collect_images
from .job import Job
from .jobs_extract import DELIVERY_BY_KEY
from .jobs_retime import QUALITY_PRESETS
from .jobs_convert import CONVERT_TARGETS
from .registry import JOB_CLASSES, JOB_REGISTRY


class WorkPlan:
    """
    이 작업이 한 번에 무엇을 받는지 정한다 — 하나씩인가, 둘씩 짝지어서인가.

    예전에는 이 판단이 CLI 쪽에만 있었다. 그래서 창에서 Compare 를 돌리면
    파일 두 개를 하나씩 따로 넘겨 버렸고, 매번 "두 개가 필요합니다" 만 나왔다.
    같은 판단을 두 군데 두면 반드시 한쪽이 뒤처진다. 그래서 여기 하나만 둔다.
    """

    def __init__(self, job_class: type[Job], targets: Sequence[Path]) -> None:
        self._job_class = job_class
        self._targets = list(targets)

    @property
    def paired(self) -> bool:
        return bool(self._job_class.PAIRED)

    @property
    def grouped(self) -> bool:
        return bool(getattr(self._job_class, "GROUPED", False))

    @property
    def units(self) -> list[tuple[Path, ...]]:
        if self.grouped:
            return [tuple(self._targets)] if self._targets else []
        if not self.paired:
            return [(target,) for target in self._targets]
        return [(self._targets[i], self._targets[i + 1])
                for i in range(0, len(self._targets) - 1, 2)]

    @property
    def shortfall(self) -> str:
        """시작조차 못 하는 경우의 이유. 빈 문자열이면 갈 수 있다."""
        if self.paired and len(self._targets) < 2:
            return ("파일 두 개가 필요합니다. A 와 B 를 같이 넣어 주세요 "
                    "(둘을 한꺼번에 끌어다 놓거나, 폴더째 넣어도 됩니다).")
        return ""

    @property
    def leftover(self) -> Optional[Path]:
        """짝이 안 맞아 남는 것. 조용히 버리지 말고 알려줘야 한다."""
        if self.paired and len(self._targets) % 2:
            return self._targets[-1]
        return None

    @staticmethod
    def label(unit: Sequence[Path]) -> str:
        if len(unit) > 2:
            return f"{unit[0].name} 외 {len(unit) - 1}개"
        return "  ↔  ".join(path.name for path in unit)

    @staticmethod
    def run(job: Job, unit: Sequence[Path]) -> JobResult:
        if getattr(type(job), "GROUPED", False):
            return job.run_batch(list(unit))
        return job.run_pair(*unit) if len(unit) == 2 else job.run(unit[0])


class CommandLine:
    """
    터미널에 친 한 줄을 실행 가능한 요청으로 옮긴다.

    argv → 작업 이름 → 파싱된 인자 → Options → Application. 이 다섯 걸음이
    모듈에 흩어져 있으면 하나를 고칠 때 나머지 넷을 찾아다녀야 한다.
    여기 한 자리에 모아 둔다.
    """

    HELP_WORDS = ("help", "explain", "도움말", "?")
    # 예전 이름으로 부르면 목적지까지 정해진 걸로 친다. mp4 라고 쳤는데
    # "어디로 보낼까요" 를 다시 묻는 건 되묻기가 아니라 훼방이다.
    ALIAS = {"mp4": "mp4", "to-mp4": "mp4",
                     "mov": "mov", "to-mov": "mov",
                     "prores": "nuke", "nuke": "nuke",
                     "shrink": "small", "compress": "small", "small": "small"}

    def __init__(self, argv: Optional[Sequence[str]] = None) -> None:
        self._raw = list(argv) if argv is not None else sys.argv[1:]

    # --- 읽기 -------------------------------------------------------------

    @staticmethod
    def job_for(name: str) -> Optional[type[Job]]:
        return JOB_REGISTRY.get(name.strip().lower())

    @staticmethod
    def parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog=env.SELF.name,
            description="영상 작업 도구 — 뽑기 · 리타임 · 역재생 · 변환 · 대조. "
                        "인자 없이 열면 창이 뜹니다.",
            epilog="작업: " + " / ".join(c.KEY for c in JOB_CLASSES),
            formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("paths", nargs="*", help="영상 · 이미지 · 시퀀스 폴더")
        pl = parser.add_argument_group('plate (플레이트 늘리기 · RIFE)')
        pl.add_argument('--rife', dest='rife_program', default='',
                        help='RIFE 실행 파일 경로. 주면 가운데 단계까지 한 번에 갑니다')
        pl.add_argument('--no-snap-plate', dest='plate_no_snap', action='store_true',
                        help='원본 프레임을 잡으려고 0.25프레임 당기는 것을 끔')
        pl.add_argument('--hold-where', dest='hold_where',
                        choices=('tail', 'head', 'both'), default='tail',
                        help='hold: 어느 쪽 프레임을 붙잡을지 (기본 tail = 뒤)')
        pl.add_argument('--drop', dest='drop_frames', default='',
                        help='revert: 메모 없이 뺄 자리 (tail:20 · head:20 · 8,21,34 · 240-265)')
        pl.add_argument('--note-file', dest='stretch_note', default='',
                        help='revert: 메모 경로 (.txt 리타임메모 · .json 늘림메모). 비우면 영상 옆과 영상 안에서 찾습니다')
        dl = parser.add_argument_group("delivery (납품)")
        dl.add_argument('--shot', dest='delivery_shot', default='',
                        help='delivery: 샷코드. 비우면 원본 이름에서 물려받습니다 (A_0002C007_org_v001 → A_0002C007)')
        dl.add_argument('--task', dest='delivery_task', default='',
                        help='delivery: 테스크 이름 (기본 ai)')
        dl.add_argument('--version', dest='delivery_version', type=int, default=0,
                        help='delivery: 버전 번호. 비우면 안 쓰인 다음 번호로 갑니다')
        parser.add_argument('--freeze-seconds', type=float, default=0.5,
                            help='audit: 정지 후보 최소 길이(초), 0.1~10')
        ex = parser.add_argument_group("extract (뽑기)")
        ex.add_argument("--format", choices=("png", "jpg", "both", "exr", "dpx"),
                        default=None, help="출력 형식. exr · dpx 는 --at all 일 때")
        ex.add_argument("--at", default=None,
                        help="뽑을 지점: 0,50%%,1:23,last … / all 이면 전부")
        ex.add_argument("--count", type=int, default=0, help="균등하게 N장")
        ex.add_argument("--every", type=int, default=0, help="N프레임마다 한 장")
        ex.add_argument("--levels", choices=("auto", "keep", "full"), default=None,
                        help="밝기 범위: auto=틀린 표기만 바로잡음 (기본)")
        ex.add_argument("--jpeg-quality", type=int, default=95, help="JPEG 품질 1~100")
        rt = parser.add_argument_group("retime (리타임)")
        rt.add_argument("--frames", dest="target_frames", type=int, default=0,
                        help="목표 프레임 수")
        rt.add_argument("--seconds", dest="target_seconds", type=float, default=0.0,
                        help="목표 길이(초)")
        rt.add_argument("--speed", type=float, default=0.0,
                        help="배속. 0.5 = 절반 속도, 2 = 두 배 빠르게")
        rt.add_argument("--retime-mode", choices=("auto", "dup", "hold", "blend", "motion"),
                        default="auto", help="없는 프레임 채우는 법 (hold = 마지막 프레임 붙잡기)")
        rt.add_argument("--no-snap", action="store_true", help="매트 라벨색 복원 끄기")
        rt.add_argument("--note", dest="retime_note", action="store_true",
                        help="손댄 자리를 메모장(.txt)으로도 결과 옆에 남김")
        rt.add_argument("--result", dest="delivery",
                        choices=tuple(DELIVERY_BY_KEY), default="auto",
                        help="리타임 · 역재생 결과를 어떤 모습으로 (기본 auto = 영상 하나)")
        cv = parser.add_argument_group("convert (변환)")
        # 목적지를 여기 또 적어 두면 엔진에 하나 늘었을 때 명령줄만 모르는 상태가 된다.
        cv.add_argument("--to", dest="convert_to",
                        choices=tuple(t.KEY for t in CONVERT_TARGETS),
                        default=None, help="어디로 보낼지")
        cv.add_argument("--image-format", choices=("png", "jpg", "webp", "exr", "dpx"), default=None,
                        help="이미지를 어떤 형식으로")
        cv.add_argument("--target-fps", type=float, default=24,
                        help="fps: 목표 프레임레이트 (24 · 23.976 · 25 · 29.97 …)")
        cv.add_argument("--fps-mode", choices=("conform", "dup", "blend", "motion"), default="conform",
                        help="fps: conform=프레임 1:1(길이 바뀜) · dup/blend/motion=길이 유지")
        cv.add_argument("--prores-movie", action="store_true",
                        help="시퀀스를 누크용 ProRes 로 묶음")
        cv.add_argument("--level", choices=tuple(QUALITY_PRESETS), default=None,
                        help="용량 줄이기 강도")
        cv.add_argument("--h265", action="store_true", help="용량 줄이기에 HEVC 사용")
        cv.add_argument("--max-height", type=int, default=0, help="세로 해상도 상한")
        cv.add_argument("--force-encode", action="store_true",
                        help="상자만 바꿔도 될 때도 다시 인코딩")
        cv.add_argument('--gif-fps', type=float, default=12)
        cv.add_argument('--gif-width', type=int, default=640)
        cv.add_argument('--gif-once', action='store_true', help='GIF를 한 번만 재생')
        cuts = parser.add_argument_group('cuts (컷 분할)')
        cuts.add_argument('--cut-mode', choices=('auto', 'interval', 'manual'), default='auto')
        cuts.add_argument('--cut-threshold', type=float, default=10)
        cuts.add_argument('--cut-min-seconds', type=float, default=0.5)
        cuts.add_argument('--cut-interval', type=float, default=5)
        cuts.add_argument('--cut-times', default='', help='예: 3.5,00:07.2')
        rn = parser.add_argument_group("rename (이름 바꾸기)")
        rn.add_argument("--pattern", dest="rename_pattern", default=None,
                        help="새 이름 패턴. {n} 자리에 번호 (기본 장면_{n})")
        rn.add_argument("--start", dest="rename_start", type=int, default=1,
                        help="시작 번호")
        rn.add_argument("--docker", dest="rename_docker", action="store_true",
                        help="조용한_수달 처럼 무작위 두 단어 이름으로")
        rn.add_argument("--name-lang", dest="rename_lang", choices=("kr", "en"),
                        default="kr", help="도커식 단어: kr=한글 두 단어, en=도커 원본")
        rn.add_argument("--find", dest="rename_find", default="",
                        help="이름에서 찾을 말 (쉼표로 여러 개)")
        rn.add_argument("--replace", dest="rename_replace", default="",
                        help="찾은 자리에 넣을 말. 비우면 지웁니다")
        rn.add_argument("--prefix", dest="rename_prefix", default="",
                        help="앞에 붙일 말 (bg · ch · plate …)")
        rn.add_argument("--suffix", dest="rename_suffix", default="",
                        help="뒤에 붙일 말")
        rn.add_argument("--cut-head", dest="rename_cut_head", type=int, default=0,
                        help="앞에서 잘라낼 글자 수")
        rn.add_argument("--cut-tail", dest="rename_cut_tail", type=int, default=0,
                        help="뒤에서 잘라낼 글자 수")
        cp = parser.add_argument_group("compare (대조)")
        cp.add_argument("--view", choices=("wipe", "overlay", "flicker", "side"),
                        default=None, help="어느 보기로 열지")
        cp.add_argument("--keep", dest="keep_compare", action="store_true",
                        help="결과를 원본 옆에 파일로 남김")
        parser.add_argument("--out", type=Path, default=None, help="결과를 모아둘 폴더")
        parser.add_argument("--fps", type=float, default=24, help="시퀀스를 묶을 때 프레임레이트")
        parser.add_argument("--no-mp4", action="store_true", help="확인용 mp4 를 만들지 않음")
        parser.add_argument("--overwrite", action="store_true", help="같은 이름이 있으면 덮어씀")
        parser.add_argument("--top-only", action="store_true", help="하위 폴더는 훑지 않음")
        parser.add_argument("--no-download", action="store_true", help="ffmpeg 자동 내려받기 끔")
        parser.add_argument("--no-pause", action="store_true", help="끝나도 엔터 대기 없음")
        return parser

    @property
    def wants_help(self) -> bool:
        return bool(self._raw) and self._raw[0].strip().lower() in self.HELP_WORDS

    @classmethod
    def split(cls, argv: list[str]) -> tuple[Optional[str], list[str]]:
        """첫 인자가 작업 이름이면 떼어낸다. 별칭도 받는다."""
        if argv and cls.job_for(argv[0]) is not None:
            return argv[0].strip().lower(), argv[1:]
        return None, argv

    # --- 옮기기 -----------------------------------------------------------

    @staticmethod
    def _rename_mode(args: argparse.Namespace) -> str:
        """무엇을 적었는지 보고 방식을 고른다 — 방식 이름까지 외우게 하지 않는다."""
        if args.rename_docker:
            return "docker"
        if args.rename_find:
            return "find" if args.rename_replace else "strip"
        if args.rename_prefix or args.rename_suffix or args.rename_cut_head or args.rename_cut_tail:
            return "affix"
        return "pattern"

    @classmethod
    def options(cls, args: argparse.Namespace) -> Options:
        return Options(
            frame_format=args.format or "both",
            jpeg_quality=args.jpeg_quality,
            at=args.at or "",
            count=args.count,
            every=args.every,
            levels=args.levels or "auto",
            out_dir=args.out,
            fps=args.fps,
            make_mp4=not args.no_mp4,
            h265=args.h265,
            convert_to=args.convert_to or "mp4",
            image_format=args.image_format or "png",
            target_fps=args.target_fps, fps_mode=args.fps_mode,
            prores_movie=args.prores_movie,
            gif_fps=args.gif_fps, gif_width=args.gif_width, gif_loop=not args.gif_once,
            cut_mode=args.cut_mode, cut_threshold=args.cut_threshold,
            cut_min_seconds=args.cut_min_seconds, cut_interval=args.cut_interval, cut_times=args.cut_times,
            view=args.view or "wipe",
            audit_freeze_seconds=args.freeze_seconds,
            rife_program=args.rife_program,
            plate_no_snap=args.plate_no_snap,
            stretch_note=args.stretch_note,
            drop_frames=args.drop_frames,
            hold_where=args.hold_where,
            delivery_shot=args.delivery_shot,
            delivery_task=args.delivery_task,
            delivery_version=args.delivery_version,
            rename_mode=cls._rename_mode(args),
            rename_pattern=args.rename_pattern or "장면_{n}",
            rename_start=args.rename_start,
            rename_lang=args.rename_lang,
            rename_find=args.rename_find,
            rename_replace=args.rename_replace,
            rename_prefix=args.rename_prefix,
            rename_suffix=args.rename_suffix,
            rename_cut_head=args.rename_cut_head,
            rename_cut_tail=args.rename_cut_tail,
            keep_compare=args.keep_compare,
            target_frames=args.target_frames,
            target_seconds=args.target_seconds,
            speed=args.speed,
            retime_mode=args.retime_mode or "auto",
            no_snap=args.no_snap,
            retime_note=args.retime_note,
            delivery=args.delivery or "auto",
            level=args.level or "medium",
            max_height=args.max_height,
            force_encode=args.force_encode,
            overwrite=args.overwrite,
            recursive=not args.top_only,
        )

    @classmethod
    def apply_alias(cls, name: str, options: Options) -> None:
        where = cls.ALIAS.get((name or "").strip().lower())
        if where:
            options.convert_to = where

    # --- 돌리기 -----------------------------------------------------------

    SHORTCUT_WORDS = ("shortcut", "바로가기")

    def run(self) -> int:
        from .app import Application   # 순환 임포트를 피해 여기서
        from .desktop import DesktopShortcut   # 순환 임포트를 피해 여기서
        raw = self._raw
        # "help" 라고 쳤는데 "찾을 수 없습니다: help" 가 나오면 안 된다.
        if self.wants_help:
            self.parser().print_help()
            return 0
        if raw and raw[0].lower() in self.SHORTCUT_WORDS:
            return DesktopShortcut().make()
        job_key, rest = self.split(raw)
        args = self.parser().parse_args(rest)
        options = self.options(args)
        if job_key:
            self.apply_alias(job_key, options)
        ff = FFmpegTool(allow_download=not args.no_download)

        app = Application(options, ff, open_result=not args.no_pause)
        try:
            code = app.run(job_key, args.paths)
        except KeyboardInterrupt:
            print("\n  중단했습니다. 이미 끝난 파일은 남아 있습니다.")
            code = 130

        if not args.no_pause and is_interactive():
            try:
                input("\n  엔터를 누르면 종료합니다...")
            except EOFError:
                pass
        return code


# 예전 이름으로 부르면 목적지까지 정해진 걸로 친다. mp4 라고 쳤는데
# "어디로 보낼까요" 를 다시 묻는 건 되묻기가 아니라 훼방이다.


# ══════════════════════════════════════════════════ 대상 수집

class TargetCollector:
    """입력 경로를 작업이 받을 수 있는 대상 목록으로 펼친다."""

    OUTPUT_MARKS = ("_conv", "_small", "_nuke", "_reversed", "_retimed",
                    "_keyframes", "_sequence", "_compare")

    def __init__(self, job: type[Job], recursive: bool = True) -> None:
        self._job = job
        self._recursive = recursive

    def collect(self, inputs: Iterable[str | Path]) -> list[Path]:
        named: list[Path] = []
        scanned: list[Path] = []
        for raw in inputs:
            path = normalize_path(raw)
            if path.is_file():
                if self._takes(path):
                    named.append(path)
                else:
                    Console.warn(f"다룰 수 없는 파일이라 건너뜁니다: {path.name}")
            elif path.is_dir():
                if self._job.ACCEPTS_FOLDER and collect_images(path):
                    named.append(path)          # 시퀀스 폴더 자체가 대상
                else:
                    scanned += self._scan(path)
            else:
                Console.warn(f"찾을 수 없습니다: {path}")
        merged = self._dedupe(named) + [p for p in self._dedupe(scanned)
                                        if p not in named]
        return merged

    def _scan(self, folder: Path) -> list[Path]:
        pattern = "**/*" if self._recursive else "*"
        found = sorted(p for p in folder.glob(pattern)
                       if p.is_file() and self._takes(p))
        fresh = [p for p in found if not self._looks_like_output(p)]
        skipped = len(found) - len(fresh)
        if skipped:
            Console.info(f"이전 결과물로 보이는 {skipped}개는 건드리지 않았습니다")
        return fresh

    def _takes(self, path: Path) -> bool:
        suffix = path.suffix.lower()
        if self._job.KEY == "rename": return True
        if suffix in VIDEO_EXTENSIONS:
            return True
        return bool(self._job.ACCEPTS_IMAGE and suffix in IMAGE_EXTENSIONS)

    @classmethod
    def _looks_like_output(cls, path: Path) -> bool:
        return path.stem.endswith(cls.OUTPUT_MARKS)

    @staticmethod
    def _dedupe(paths: Sequence[Path]) -> list[Path]:
        seen: set[Path] = set()
        unique: list[Path] = []
        for path in paths:
            key = path.resolve()
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique
