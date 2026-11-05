"""RIFE 플레이트 · 되돌리기 · hold 작업"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .errors import FramingError
from .options import JobResult
from .env import VIDEO_EXTENSIONS
from .util import Console, unique_path
from .retime import RetimePlan
from .job import Job
from .plate import DenseGrid, DropSpec, HoldBuilder, HoldPlan, PlateAssemble, PlateExport, PlateRevert, PlateSpec, RetimeNoteText, RifeRunner, StretchNote


class PlateRifeJob(Job):
    """
    짧은 mov 플레이트를 RIFE 로 늘리되, 플레이트는 손대지 않는다.

    한 번에 다 하지 않고 두 번에 나눠 부른다 — 가운데에 RIFE 가 끼기 때문이다.
      1) 내보내기 : plate/(원본 계조) · for_rife/(8bit 복사본) · rife.json
      2) RIFE     : 등록된 실행 파일이 있으면 자동, 없으면 명령을 적어 주고 멈춤
      3) 가져오기 : 폴더를 다시 넣으면 원본 자리는 plate/ 에서, 새 자리만 RIFE 에서

    원본 프레임이 RIFE 를 거치지 않는 게 핵심이다. 그래서 10bit 이 산다.
    """

    KEY = 'plate'
    ALIASES = ('rife', 'plate-rife')
    TITLE = '플레이트 늘리기 (RIFE)'
    HINT = '원본 계조를 지키면서 사이만 RIFE 로 채움'
    NEEDS_FFMPEG = True
    ACCEPTS_FOLDER = True
    NOTE = """
    두 번에 나눠 돌립니다.
      framing plate 컷.mov --frames 96        내보내기 (폴더가 생깁니다)
      framing plate 컷.mov_rife               가져오기 (RIFE 결과를 넣고 다시)

    RIFE 실행 파일을 --rife 로 주면 가운데 단계까지 한 번에 갑니다.
    정수배(2·4·8)로 뽑은 뒤 목표 장수만 골라냅니다 — 그래야 원본이 격자 위에
    정확히 놓여서 그대로 통과합니다. 임의 비율로 한 번에 늘리면 원본이 거의
    안 남습니다. 알파는 다루지 않습니다."""

    def run(self, target: Path) -> JobResult:
        if target.is_dir() and (target / PlateExport.MANIFEST).exists():
            return self._bring_back(target)
        return self._send_out(target)

    # --- 1단계 -----------------------------------------------------------

    def _send_out(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        self._ff.ensure()
        spec = self._read_plate(target)
        wanted = self._wanted(spec)
        grid = DenseGrid(spec.frames, wanted, snap=not self._opt.plate_no_snap)
        Console.info(f'계획  {grid.summary()}')
        short = grid.unavoidable_doubles
        if short:
            # 격자보다 많이 달라고 하면 산수로 겹친다. 돌리기 전에 말한다.
            Console.warn(f'{wanted}장은 RIFE {grid.multiplier}배 격자'
                         f'({grid.dense_count}장)보다 많습니다 '
                         f'— {short}장은 같은 그림이 한 번 더 나옵니다')
        folder = unique_path((self._opt.out_dir or target.parent) / (target.stem + PlateExport.FOLDER))
        PlateExport(target, spec, grid, self._ff.ensure(),
                    self._abort_if_asked).run(folder)

        if grid.needs_rife:
            runner = RifeRunner(self._opt.rife_program, self._abort_if_asked)
            ran = runner.run(folder / 'for_rife', folder / 'rife', grid.multiplier)
            if ran:
                return self._bring_back(folder)
            Console.info(f'다음  RIFE 결과를 {folder / "rife"} 에 넣고 다시 부르세요')
            Console.info(f'      framing plate "{folder}"')
        else:
            return self._bring_back(folder)
        result.outputs = [folder / PlateExport.MANIFEST]
        result.output_dir = folder
        result.ok = True
        result.message = f'내보냄 · {grid.summary()}'
        return result

    def _read_plate(self, target: Path) -> PlateSpec:
        spec = PlateSpec.read(_probe_plate(self._ff.ensure_probe(), target), target)
        if spec.frames < 2:
            raise FramingError('플레이트가 두 장은 되어야 사이를 채울 수 있습니다.')
        if spec.pix_fmt.lower().startswith(('yuva', 'rgba', 'bgra', 'argb', 'abgr', 'gbrap', 'ya')):
            # 예전에는 4444 알파 소스를 422 HQ 로 되묶으며 알파를 말없이 버렸다
            raise FramingError('RIFE 플레이트는 알파를 담지 못합니다. 투명도를 유지하려면 '
                               '리타임에서 복제 또는 겹치기를 쓰세요.')
        Console.info(f'플레이트  {spec.frames}프레임 · {spec.fps:g}fps · '
                     f'{spec.pix_fmt or "규격 안 적힘"} · {spec.codec or "?"}')
        return spec

    def _wanted(self, spec: PlateSpec) -> int:
        return _wanted_frames(self._opt, spec)

    # --- 3단계 -----------------------------------------------------------

    def _bring_back(self, folder: Path) -> JobResult:
        result = JobResult(target=folder)
        assemble = PlateAssemble(folder, self._ff.ensure(), self._abort_if_asked)
        problems = assemble.complaints()
        if problems:
            for line in problems:
                Console.warn(line)
            raise FramingError('가져오지 못했습니다 — 위 내용을 확인하세요.')
        frames, report = assemble.build()
        Console.info(f'모음  원본 그대로 {report["fromPlate"]}장 · '
                     f'RIFE 로 채운 자리 {report["fromRife"]}장'
                     + (f' · 원본을 잡으려고 당긴 자리 {report["snapped"]}장'
                        if report['snapped'] else ''))
        # 되돌릴 수 있게 쪽지를 남긴다 — 파일로도 두고 영상 안에도 심는다
        note = assemble.note(report['frames'])
        movie = assemble.mux(frames, folder / f'{Path(assemble.spec.source).stem}_plate.mov',
                             note)
        Console.info(f'되묶기  {assemble.spec.pix_fmt or "소스 그대로"} · 색 표기 그대로')
        Console.info(f'메모  {note.beside(movie).name} · {note.summary()}')
        Console.info(f'되돌리기  framing revert "{movie.name}"')
        result.outputs = [movie, note.beside(movie), folder / PlateExport.MANIFEST]
        result.output_dir = folder
        result.ok = True
        result.message = (f'{report["frames"]}프레임 · 원본 {report["fromPlate"]}장 + '
                          f'RIFE {report["fromRife"]}장  ·  {movie.name}')
        return result


class RifeRetimeJob(PlateRifeJob):
    """Desktop adapter: validate first, then reuse the tested RIFE pipeline."""

    TITLE = '리타임 · RIFE'

    def _wanted(self, spec):
        plan = RetimePlan.decide(spec.frames, spec.fps, frames=self._opt.target_frames,
                                 seconds=self._opt.target_seconds, speed=self._opt.speed)
        if plan.refusal:
            raise FramingError(plan.refusal)
        if plan.target < 2:
            raise FramingError('RIFE 결과는 2프레임 이상으로 정해 주세요.')
        if plan.slowing:
            runner = RifeRunner(self._opt.rife_program, self._abort_if_asked)
            if not runner.ready:
                raise FramingError('RIFE 구성요소가 없습니다. 앱 압축을 모두 풀거나 보간 방식에서 FFmpeg를 선택하세요.')
            runner._model()
        return plan.target

    def run(self, target):
        if not target.is_file() or target.suffix.lower() not in VIDEO_EXTENSIONS:
            raise FramingError('RIFE는 영상 파일을 사용합니다. 이미지 시퀀스는 먼저 영상으로 변환하거나 FFmpeg를 선택하세요.')
        if self._ff.probe(target).alpha:
            raise FramingError('RIFE는 알파 영상을 지원하지 않습니다. 투명도를 유지하려면 복제 또는 겹치기를 선택하세요.')
        result = super().run(target)
        result.next_paths = [str(p) for p in result.outputs if p.suffix.lower() in VIDEO_EXTENSIONS]
        return result


class UiJobSelection:
    """Resolve one visible retime tool to its execution strategy."""

    @staticmethod
    def resolve(key, options):
        from .registry import JOB_REGISTRY   # 순환 임포트를 피해 여기서
        if key == 'retime' and options.retime_mode == 'rife':
            return RifeRetimeJob
        found = JOB_REGISTRY.get(key)
        if found is None:
            raise FramingError(f'모르는 작업입니다: {key}')
        return found


class PlateRevertJob(Job):
    """
    늘린 영상에서 보간 자리만 빼서 원래 길이로 되돌린다.

    쪽지에 "출력 3번은 원본 1번" 이 적혀 있으니 되돌리기는 계산이 아니라
    **고르기**다. 어느 프레임을 버릴지 다시 추정하지 않는다 — 추정하면 늘릴
    때와 뺄 때가 어긋나고, 그건 아무도 못 알아챈다.

    쪽지는 영상 옆 파일에서 찾고, 없으면 영상 안에 심어 둔 것에서 찾는다.
    """

    KEY = 'revert'
    ALIASES = ('unstretch', 'shrink-back')
    TITLE = '늘린 것 되돌리기'
    HINT = '늘림 메모를 보고 보간 프레임만 빼냄'
    NEEDS_FFMPEG = True
    NOTE = """
    늘릴 때 남긴 메모를 보고 보간으로 넣은 자리만 뺍니다.

      framing revert 컷_01_plate.mov                        옆의 메모를 알아서 찾습니다
      framing revert 컷_01.mov --note-file 컷_01_리타임메모.txt
      framing revert 컷_01.mov --note-file 늘림메모.json

    리타임 메모장(.txt)을 줘도 됩니다 — 거기 적힌 프레임 번호를 뺍니다.
    메모를 잃어버려도 영상 안에 심어 둔 것이 있으면 되돌아갑니다.
    넣은 영상의 프레임 수가 메모와 다르면 (자르거나 다른 영상이면)
    아무것도 만들지 않고 멈춥니다.

    메모가 아예 없으면 어디를 뺄지 직접 말할 수 있습니다.

      framing revert 컷.mov --drop tail:20        뒤에서 20장
      framing revert 컷.mov --drop head:20        앞에서 20장
      framing revert 컷.mov --drop 8,21,34        그 번호들 (1부터)
      framing revert 컷.mov --drop 240-265        구간

    메모와 영상이 맞아도 원본 프레임이 모두 남아 있어야 원본 길이로 복원됩니다."""

    def _given_note(self) -> Optional[Path]:
        """
        "메모 파일" 칸에 들어온 것. 경로여야 하는데 **메모의 내용을 붙여넣는**
        일이 실제로 있었다. 그러면 예전에는 그 긴 글을 파일 이름으로 알고
        찾다가 FileNotFoundError 로 죽었다 — 무엇이 잘못됐는지 알 수가 없다.
        """
        said = (self._opt.stretch_note or '').strip().strip('"').strip("'")
        if not said:
            return None
        spot = Path(said)
        if spot.is_file():
            return spot
        if len(said) > 200 or '\n' in said:
            raise FramingError('메모 파일 칸에는 파일을 넣어 주세요 — 메모의 내용이 아니라 '
                               '메모장 파일(.txt)이나 늘림메모(.json)를 끌어다 놓으면 됩니다.')
        raise FramingError(f'메모 파일을 찾지 못했습니다: {said}')

    def _note(self, target: Path, ffprobe: str) -> StretchNote:
        """
        뺄 자리를 누가 아는가 — 세 갈래다.

          1. 리타임 메모장(.txt) 을 직접 줬다          → 거기 적힌 번호
          2. 아무 말도 없다                            → 늘림메모(.json) 를 찾아본다
          3. "뒤에서 20장" 처럼 직접 말했다            → 그 자리 (차선책)
        """
        given = self._given_note()
        asked = DropSpec(self._opt.drop_frames)
        if given is not None and RetimeNoteText.handles(given):
            spec = self._plate(target, ffprobe)
            Console.info(f'메모장  {given.name}')
            return RetimeNoteText(given).note(spec, spec.frames)
        if not asked:
            return StretchNote.find(target, given, ffprobe)
        spec = self._plate(target, ffprobe)
        Console.warn('직접 지정한 자리를 뺍니다 — 늘릴 때 기록한 자리가 아닙니다')
        return asked.note(spec, spec.frames)

    def _plate(self, target: Path, ffprobe: str) -> PlateSpec:
        spec = PlateSpec.read(_probe_plate(ffprobe, target), target)
        if spec.frames < 2:
            raise FramingError('프레임 수를 읽지 못했습니다 — 뺄 자리를 셀 수가 없습니다.')
        return spec

    def run(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        ffmpeg = self._ff.ensure()
        ffprobe = self._ff.ensure_probe()
        note = self._note(target, ffprobe)
        if note.restoration_problem:
            raise FramingError(note.restoration_problem)
        Console.info(f'메모  {note.summary()}')
        revert = PlateRevert(target, note, ffmpeg, self._abort_if_asked)
        destination = self._output_file(target, '_원본길이', target.suffix or '.mov')
        workspace = Path(tempfile.mkdtemp(prefix='framing_revert_'))
        try:
            report = revert.run(destination, workspace)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        Console.info(f'뺀 자리  늘리면서 생긴 {report["removed"]}장')
        result.outputs = [destination]
        result.output_dir = destination.parent
        result.ok = True
        result.message = (f'{report["from"]} → {report["to"]}프레임 · '
                          f'늘린 자리 {report["removed"]}장 뺌  ·  {destination.name}')
        return result


class PlateHoldJob(Job):
    """
    끝(또는 앞) 프레임을 붙잡아 길이를 늘린다. 보간을 아예 안 쓴다.

    짧은 컷을 늘릴 때 늘 보간이 답인 건 아니다. 꼬리가 거의 멈춰 있거나
    타임라인 길이만 맞추면 되는 자리에서는, 끝 프레임을 붙잡는 편이 깨끗하다 —
    **새로 그리는 픽셀이 하나도 없어서** 플레이트가 100% 산다. 찢어질 일도 없다.

    대신 붙잡은 자리는 그냥 멈춘다. 움직임이 살아 있는 꼬리에 쓰면 툭 서 버린다.
    늘림 메모를 똑같이 남기므로 `framing revert` 로 그대로 되돌아간다.
    """

    KEY = 'hold'
    ALIASES = ('freeze', '홀드')
    TITLE = '끝 프레임 붙잡기'
    HINT = '보간 없이 앞뒤 프레임을 붙잡아 길이만 늘림'
    NEEDS_FFMPEG = True
    NOTE = """
      framing hold 컷_01.mov --frames 46            뒤를 붙잡습니다 (기본)
      framing hold 컷_01.mov --seconds 2 --hold-where head
      framing hold 컷_01.mov --frames 46 --hold-where both

    새로 그리는 픽셀이 없어서 플레이트가 그대로 남습니다. 대신 붙잡은 자리는
    멈춰 있으니, 꼬리가 움직이는 컷이면 RIFE 쪽(plate)을 쓰세요.
    늘림 메모를 남기므로 framing revert 로 되돌아갑니다."""

    def run(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        ffmpeg = self._ff.ensure()
        spec = PlateSpec.read(_probe_plate(self._ff.ensure_probe(), target), target)
        if spec.frames < 1:
            raise FramingError('프레임을 읽지 못했습니다.')
        Console.info(f'플레이트  {spec.frames}프레임 · {spec.fps:g}fps · '
                     f'{spec.pix_fmt or "규격 안 적힘"}')
        wanted = _wanted_frames(self._opt, spec)
        plan = HoldPlan(spec.frames, wanted, self._opt.hold_where)
        Console.info(f'계획  {plan.summary()}')
        destination = self._output_file(target, '_hold', target.suffix or '.mov')
        workspace = Path(tempfile.mkdtemp(prefix='framing_hold_'))
        try:
            movie, note = HoldBuilder(target, spec, plan, ffmpeg,
                                      self._abort_if_asked).run(destination, workspace)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        Console.info(f'메모  {note.beside(movie).name} · {note.summary()}')
        Console.info(f'되돌리기  framing revert "{movie.name}"')
        result.outputs = [movie, note.beside(movie)]
        result.output_dir = movie.parent
        result.ok = True
        result.message = (f'{spec.frames} → {wanted}프레임 · '
                          f'{HoldPlan.LABEL[plan.where]} 홀드 {plan.extra}장  ·  {movie.name}')
        return result


def _probe_plate(ffprobe: str, target: Path) -> str:
    """
    플레이트 규격 한 덩어리. 늘리기와 붙잡기가 같은 눈으로 본다.

    받는 건 **ffprobe 경로**다. 예전에는 ffmpeg 경로를 받아 여기서 이름을
    바꿨는데, 윈도우처럼 폴더 이름에도 'ffmpeg' 이 들어가면 그것까지 바뀌어
    없는 경로가 됐다.
    """
    try:
        return subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0',
             '-count_frames', '-show_entries',
             'stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_read_frames,'
             'color_primaries,color_transfer,color_space,color_range',
             '-of', 'default=nw=1', str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    except OSError as exc:
        # 날것의 FileNotFoundError 가 그대로 올라가면 "메모 파일이 없나?" 하고
        # 엉뚱한 데를 보게 된다. 못 찾은 것이 무엇인지 이름을 적어 준다.
        raise FramingError(f'ffprobe 를 실행하지 못했습니다 ({ffprobe}) — {exc}')


def _wanted_frames(opt, spec: PlateSpec) -> int:
    """몇 장으로 늘릴지 — 프레임 · 초 · 배속 중 무엇으로 말했든 여기로 모은다."""
    if opt.target_frames > 0:
        return int(opt.target_frames)
    if opt.target_seconds > 0:
        return max(2, int(round(opt.target_seconds * spec.fps)))
    if opt.speed > 0:
        return max(2, int(round(spec.frames / opt.speed)))
    raise FramingError('몇 장으로 늘릴지 정해 주세요 (--frames · --seconds · --speed).')


