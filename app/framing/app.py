"""작업 실행기 · 폴더 열기 · 끌어놓기"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence
from typing import TYPE_CHECKING

from . import env
from .errors import FramingError
from .options import JobResult, Options
from .util import Console, human, is_interactive
from .media import FFmpegTool
from .job import Job
from .registry import JOB_REGISTRY
from .cli import TargetCollector, WorkPlan

if TYPE_CHECKING:   # 타입 표기에만 쓰는 이름
    from .api import Api


class Application:
    def __init__(self, options: Options, ff: FFmpegTool, open_result: bool = True) -> None:
        self._opt = options
        self._ff = ff
        # --no-pause 는 스크립트에서 부른다는 뜻이다. 그때 결과 폴더를 띄우면 돌릴 때마다 창이 쌓인다.
        self._open_result = open_result
        # 앱에서 중단 버튼을 누르면 이걸 갈아끼운다. CLI 에서는 늘 False.
        self.should_stop: Callable[[], bool] = lambda: False

    def _abort_if_asked(self) -> None:
        if self.should_stop():
            raise FramingError('중단했습니다.')

    def run(self, job_key: Optional[str], inputs: Sequence[str]) -> int:
        Console.banner("Framing — 영상 작업 도구 모음")

        raw_inputs = list(inputs)
        if not raw_inputs:
            Console.warn("처리할 대상이 없습니다.")
            Console.info(f"{env.SELF.name} keyframes 영상.mp4   처럼 쓰거나,")
            Console.info("그냥 더블클릭하면 창이 뜹니다.")
            return 1

        if job_key is None:
            job_key = "extract"            # 아이콘에 끌어다 놓으면 창이 뜨니, 여기 오면 CLI 다

        job_class = JOB_REGISTRY[job_key]  # split_job 이 검증해 둔 키
        job = job_class(self._opt, self._ff)

        targets = TargetCollector(job_class, self._opt.recursive).collect(raw_inputs)
        if not targets:
            Console.warn("처리할 대상을 찾지 못했습니다.")
            return 1

        if job_class.NEEDS_FFMPEG:
            try:
                self._ff.ensure()
            except FramingError as exc:
                Console.warn(str(exc))
                return 1

        plan = WorkPlan(job_class, targets)
        if plan.shortfall:
            Console.warn(plan.shortfall)
            return 1
        if plan.leftover is not None:
            Console.warn(f"{plan.leftover.name} 은 짝이 없어 건너뜁니다")

        units = plan.units
        Console.banner(f"{job_class.TITLE}  ·  {len(units)}"
                       f"{'쌍' if plan.paired else '개'}")
        results = [self._run_one(job, unit, i, len(units))
                   for i, unit in enumerate(units, 1)]
        return self._report(results)

    def _run_one(self, job: Job, unit: Sequence[Path], order: int, total: int) -> JobResult:
        target = unit[0]
        Console.section(f"[{order}/{total}] {WorkPlan.label(unit)}")
        try:
            result = WorkPlan.run(job, unit)
        except FramingError as exc:
            Console.fail(str(exc))
            return JobResult(target=target, ok=False, message=str(exc))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            Console.fail(message)
            return JobResult(target=target, ok=False, message=message)

        if result.message:
            Console.ok(result.message)
        if result.output_dir:
            Console.info(f"→ {result.output_dir}")
        return result

    def _report(self, results: Sequence[JobResult]) -> int:
        failed = [r for r in results if not r.ok]
        Console.banner(f"완료 — 성공 {len(results) - len(failed)}개 / 실패 {len(failed)}개")
        for result in failed:
            Console.fail(f"{result.target.name}: {result.message or '결과 없음'}")

        total_in = sum(r.size_in for r in results if r.ok and r.size_in)
        total_out = sum(r.size_out for r in results if r.ok and r.size_out)
        if total_in and total_out:
            delta = (1 - total_out / total_in) * 100
            verb = "줄었습니다" if delta >= 0 else "늘었습니다"
            Console.info(f"합계 용량  {human(total_in)} → {human(total_out)}"
                         f"  ({abs(delta):.1f}% {verb})")
        Console.info("원본은 건드리지 않았습니다.")

        first = next((r.output_dir for r in results if r.ok and r.output_dir), None)
        if first and self._open_result and is_interactive():
            self._open_folder(first)
        return 0 if not failed else 1

    @staticmethod
    def _open_folder(folder: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))       # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)], check=False)
        except Exception:
            pass


# ══════════════════════════════════════════════════ CLI


# ══════════════════════════════════════════════════ 5. 창구

# ══════════════════════════════════════════════════ 바깥 세상과의 접점

class Reveal:
    """
    폴더 하나를 파일 탐색기에 띄우는 방법. 운영체제마다 방법이 다르니
    하위 클래스가 자기 방법만 안다. 고르는 건 pick() 이 한다.

    방법은 여러 개를 순서대로 들고 있다가 되는 것이 나올 때까지 시도한다.
    """

    QUIET = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}

    @staticmethod
    def pick() -> "Reveal":
        if sys.platform.startswith('win'):
            return WindowsReveal()
        if sys.platform == 'darwin':
            return MacReveal()
        return LinuxReveal()

    def plans(self, folder: Path, highlight: Optional[Path]) -> list:
        raise NotImplementedError


class WindowsReveal(Reveal):
    """
    윈도우 탐색기. 여기 함정이 하나 있었다.

    subprocess 에 목록으로 주면 파이썬이 띄어쓰기 있는 인자를 통째로 따옴표로
    감싼다. 그러면 explorer.exe 가 받는 건 `"/select,C:\\내 폴더\\a.mp4"` 라서
    /select 를 스위치로 못 알아보고, 경로도 아니니 그냥 기본 폴더(바탕화면 ·
    내 문서)를 연다. "폴더 열기를 눌렀는데 바탕화면이 뜬다" 가 이것이었다.

    그래서 윈도우에서는 명령줄을 문자열로 직접 만들어 넘긴다. 문자열로 주면
    파이썬이 손대지 않고 그대로 전달한다. 경로 구분자도 역슬래시로 맞춘다 —
    탐색기는 슬래시로 된 경로에서 /select 를 무시한다.
    """

    @staticmethod
    def _win_path(path: Path) -> str:
        return os.path.normpath(str(path)).replace('/', '\\')

    def plans(self, folder: Path, highlight: Optional[Path]) -> list:
        here = self._win_path(folder)
        made = []
        if highlight is not None:
            picked = self._win_path(highlight)
            made.append(lambda: subprocess.Popen(
                f'explorer.exe /select,"{picked}"', **self.QUIET))
        made.append(lambda: subprocess.Popen(
            f'explorer.exe "{here}"', **self.QUIET))
        made.append(lambda: os.startfile(here))    # type: ignore[attr-defined]
        return made


class MacReveal(Reveal):
    """파인더. -R 은 파일을 짚어서 보여준다."""

    def plans(self, folder: Path, highlight: Optional[Path]) -> list:
        made = []
        if highlight is not None:
            made.append(lambda: subprocess.run(['open', '-R', str(highlight)],
                                               check=True, **self.QUIET))
        made.append(lambda: subprocess.run(['open', str(folder)],
                                           check=True, **self.QUIET))
        return made


class LinuxReveal(Reveal):
    """리눅스는 파일 관리자가 제각각이라 흔한 것부터 차례로 본다."""

    TRIES = ('xdg-open', 'nautilus', 'thunar', 'dolphin', 'nemo')

    def plans(self, folder: Path, highlight: Optional[Path]) -> list:
        return [(lambda name=name: subprocess.run([name, str(folder)],
                                                  check=True, **self.QUIET))
                for name in self.TRIES]


class FolderOpener:
    """
    결과 폴더를 파일 탐색기로 연다. 그것만 한다.

    두 가지가 중요하다.
      · 창이 다른 창 뒤로 숨지 않고 맨 앞으로 나와야 한다
      · explorer.exe 는 성공해도 0 이 아닌 값을 돌려준다 — 종료코드를 믿으면 안 된다
    """

    def __init__(self, path: str, select: str = "",
                 how: Optional[Reveal] = None) -> None:
        self._raw = path
        self._select = select
        self._how = how or Reveal.pick()

    def open(self) -> dict:
        folder = self._folder()
        if folder is None:
            return {'ok': False, 'path': str(self._raw),
                    'error': f'그런 경로가 없습니다: {self._raw}'}
        target = self._highlight(folder)
        troubles = []
        for attempt in self._how.plans(folder, target):
            try:
                attempt()
                return {'ok': True, 'path': str(folder)}
            except Exception as exc:
                troubles.append(f'{type(exc).__name__}: {exc}')
                continue
        why = ' / '.join(troubles[:2]) if troubles else '방법이 하나도 없습니다'
        return {'ok': False, 'path': str(folder),
                'error': f'폴더를 열지 못했습니다 ({why}). 경로를 직접 열어 주세요.'}

    def _folder(self) -> Optional[Path]:
        if not self._raw:
            return None
        target = Path(self._raw).expanduser()
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        if not target.exists():
            return None
        return target if target.is_dir() else target.parent

    def _highlight(self, folder: Path) -> Optional[Path]:
        """골라서 보여줄 파일 하나. 있으면 탐색기가 확실히 앞으로 나온다."""
        if self._select:
            picked = Path(self._select).expanduser()
            if not picked.is_absolute():
                picked = (Path.cwd() / picked).resolve()
            if picked.exists():
                return picked
        try:
            for child in sorted(folder.iterdir()):
                if child.is_file():
                    return child
        except OSError:
            pass
        return None




# ══════════════════════════════════════════════════ 엔진 다리


# ══════════════════════════════════════════════════ 화면이 부르는 함수들

