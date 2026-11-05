"""컷 분할"""
from __future__ import annotations

import csv
import math
import re
import tempfile
from bisect import bisect_left
from pathlib import Path

from .errors import FramingError
from .options import JobResult
from .util import Console, FrameNumber
from .job import Job
from .sequence import OutputFolder, bounded_number


"""Scene analysis, frame boundary planning, and one-pass clip export."""


class StartTimecode:
    """
    원본 시작 타임코드에 프레임 수를 더한다 (non-drop).

    컷마다 원본에서 몇 번째 프레임에서 시작했는지가 타임코드로 남아야 컨폼할 때 제자리를 찾는다.
    예전에는 모든 컷이 원본 시작 타임코드(10:20:30:12)를 그대로 달고 나왔다.
    드롭프레임(;)은 계산 규칙이 달라 건드리지 않는다.
    """

    def __init__(self, text: str, fps: float) -> None:
        self.text, self.base = text or "", max(1, int(round(fps or 24)))
        parts = self.text.split(":")
        self.valid = len(parts) == 4 and ";" not in self.text and all(p.isdigit() for p in parts)
        if self.valid:
            h, m, s, f = map(int, parts)
            self.frames = ((h * 60 + m) * 60 + s) * self.base + f

    def plus(self, frames: int) -> str:
        if not self.valid:
            return ""
        total = self.frames + frames
        f, total = total % self.base, total // self.base
        s, total = total % 60, total // 60
        m, h = total % 60, (total // 60) % 24
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


class CutTimeline:
    def __init__(self, times, scores, duration):
        if not times or any(not math.isfinite(t) for t in times):
            raise FramingError('영상의 프레임 시간을 읽지 못했습니다.')
        if any(b <= a for a, b in zip(times, times[1:])):
            raise FramingError('프레임 시간이 중복되거나 역전된 영상입니다. MP4로 변환한 뒤 다시 시도하세요.')
        self.times, self.scores = times, scores
        self.duration = duration

    @classmethod
    def read(cls, target, ff, info, abort):
        with tempfile.TemporaryDirectory(prefix='framing_cuts_') as temp:
            record = Path(temp) / 'frames.txt'
            quoted = record.as_posix().replace(':', '\\:').replace("'", "'\\''")
            # scdet writes a score for EVERY frame. Timestamps survive scaling.
            vf = ("setpts=PTS-STARTPTS,scale=w='min(iw,320)':h=-1,format=rgb24,"
                  f"scdet=threshold=10,metadata=mode=print:file='{quoted}'")
            code = ff.run(['-y', '-i', str(target), '-map', '0:v:0', '-vf', vf,
                           '-an', '-f', 'null', '-'], total_seconds=info.duration)
            abort()
            if code or not record.is_file():
                raise FramingError(f'장면 분석 실패 (ffmpeg {code})')
            times, scores = [], []
            for line in record.read_text(encoding='utf-8').splitlines():
                if line.startswith('frame:'):
                    match = re.search(r'pts_time:([-+\d.eE]+)', line)
                    if match:
                        times.append(float(match[1])); scores.append(0.0)
                elif line.startswith('lavfi.scd.score=') and scores:
                    scores[-1] = float(line.partition('=')[2])
        if not times:
            raise FramingError('검사할 프레임이 없습니다.')
        step = times[-1] - times[-2] if len(times) > 1 else 1 / (info.fps or 24)
        end = info.video_duration if info.video_duration > times[-1] else times[-1] + step
        return cls(times, scores, end)

    @staticmethod
    def seconds(text):
        try:
            parts = str(text).strip().split(':')
            if not 1 <= len(parts) <= 3:
                raise ValueError()
            values = [float(p) for p in parts]
            if any(not math.isfinite(v) or v < 0 for v in values):
                raise ValueError()
            if len(parts) > 1 and any(v >= 60 for v in values[1:]):
                raise ValueError()
            return sum(value * 60 ** i for i, value in enumerate(reversed(values)))
        except (TypeError, ValueError):
            raise FramingError(f'컷 시간 형식이 잘못되었습니다: {text}. 예: 3.5, 00:07.2')

    def boundaries(self, options):
        count = len(self.times)
        mode = options.cut_mode
        if mode == 'auto':
            threshold = bounded_number(options.cut_threshold, '장면 감지 기준', 1, 100)
            minimum = bounded_number(options.cut_min_seconds, '최소 컷 길이', 0.1, 600)
            points, last = [], 0.0
            for index in range(1, count):
                at = self.times[index]
                if self.scores[index] >= threshold and at - last >= minimum - 1e-6 and self.duration - at >= minimum - 1e-6:
                    points.append(index); last = at
        else:
            if mode == 'interval':
                interval = bounded_number(options.cut_interval, '분할 간격', 0.1, 86400)
                if self.duration / interval > 501:
                    raise FramingError('한 번에 최대 500컷까지 저장합니다. 간격을 늘려 주세요.')
                requested = [i * interval for i in range(1, math.ceil(self.duration / interval))]
            elif mode == 'manual':
                tokens = [p for p in re.split(r'[,;\s]+', str(options.cut_times).strip()) if p]
                if not tokens:
                    raise FramingError('나눌 시간을 입력하세요. 예: 3.5, 00:07.2')
                requested = [self.seconds(token) for token in tokens]
                if any(at <= 0 or at >= self.duration for at in requested):
                    raise FramingError(f'컷 시간은 0초보다 크고 영상 길이 {self.duration:.3f}초보다 작아야 합니다.')
            else:
                raise FramingError('모르는 컷 분할 방식입니다.')
            points = [bisect_left(self.times, at - 1e-6) for at in requested]
        points = sorted({p for p in points if 0 < p < count})
        if len(points) >= 500:
            raise FramingError('한 번에 최대 500컷까지 저장합니다. 기준이나 간격을 조절하세요.')
        return [0, *points, count]

    def entries(self, boundaries, folder):
        cuts = []
        for index, (first, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
            start_time = self.times[first]
            end_time = self.times[end] if end < len(self.times) else self.duration
            # 번호는 사람이 세는 1부터. 도구마다 다르면 그게 사고의 출발점이다.
            cuts.append({'index': index,
                         'first': FrameNumber.show(first), 'last': FrameNumber.show(end - 1),
                         'frames': end - first,
                         'startSeconds': start_time, 'endSeconds': end_time,
                         'duration': end_time - start_time,
                         'file': str(folder / f'cut_{index:04d}.mp4')})
        return cuts


class CutJob(Job):
    def _stamp_timecodes(self, cuts):
        """세그먼트는 원본 시작 타임코드를 그대로 복사한다. 컷마다 제 시작 타임코드로 다시 단다 (재인코딩 없음)."""
        for cut in cuts:
            stamp = cut.get('timecode')
            if not stamp:
                continue
            movie = Path(cut['file'])
            temp = movie.with_name(movie.stem + '_tc' + movie.suffix)
            code = self._ff.run(['-y', '-i', str(movie), '-map', '0:v', '-map', '0:a?', '-c', 'copy',
                                 '-timecode', stamp, '-movflags', '+faststart', str(temp)], quiet=True)
            if code == 0 and temp.is_file() and temp.stat().st_size > 0:
                temp.replace(movie)
            else:
                temp.unlink(missing_ok=True)
                Console.warn(f'{movie.name} 의 타임코드를 {stamp} 로 바꾸지 못했습니다')

    KEY = 'cuts'
    TITLE = '컷 분할'
    HINT = '장면 전환 · 일정 간격 · 지정 시간으로 나누기'
    ALIASES = ('split', 'scenes')
    NEEDS_FFMPEG = True
    NOTE = '자동 감지는 장면 변화 후보입니다. 컷별 MP4와 프레임 목록을 새 폴더에 저장합니다.'

    def run(self, target):
        self._ff.ensure()
        info = self._ff.probe(target)
        if not info or not info.vcodec or info.duration <= 0:
            raise FramingError('컷을 나눌 영상 정보를 읽지 못했습니다.')
        Console.info('프레임 시간과 장면 변화를 분석합니다')
        timeline = CutTimeline.read(target, self._ff, info, self._abort_if_asked)
        boundaries = timeline.boundaries(self._opt)
        bundle = OutputFolder(target, self._opt.out_dir, '_cuts', 'cuts.json')
        cuts = timeline.entries(boundaries, bundle.path)
        start = StartTimecode(info.timecode, info.fps)
        if start.valid:
            for cut, first in zip(cuts, boundaries):
                cut['timecode'] = start.plus(first)
        bundle.write(mode=self._opt.cut_mode, threshold=self._opt.cut_threshold,
                     minimumSeconds=self._opt.cut_min_seconds, intervalSeconds=self._opt.cut_interval,
                     requestedTimes=self._opt.cut_times, frameNumberBase=FrameNumber.BASE,
                     sourceFrames=len(timeline.times), duration=timeline.duration, cuts=cuts)
        Console.info(f'{len(cuts)}컷 저장 · 원본의 모든 프레임을 한 번씩 사용합니다')
        try:
            marks = boundaries[1:-1]
            args = ['-y', '-i', str(target), '-map', '0:v:0', '-map', '0:a:0?',
                    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt',
                    'yuv444p' if info.width % 2 or info.height % 2 else 'yuv420p',
                    '-fps_mode', 'passthrough', '-bf', '0', '-sc_threshold', '0', '-c:a', 'aac', '-b:a', '192k']
            if marks:
                args += ['-force_key_frames', 'expr:' + '+'.join(f'eq(n,{n})' for n in marks)]
            args += ['-f', 'segment', '-segment_start_number', '1', '-reset_timestamps', '1',
                     '-segment_format', 'mp4', '-segment_format_options', 'movflags=+faststart']
            # An empty boundary list is one whole clip, not the muxer's default 2s segments.
            args += ['-segment_frames', ','.join(map(str, marks))] if marks else ['-segment_time', str(timeline.duration + 1)]
            args.append(str(bundle.path / 'cut_%04d.mp4'))
            code = self._ff.run(args, total_seconds=timeline.duration)
            self._abort_if_asked()
            movies = [Path(cut['file']) for cut in cuts]
            if code or len(list(bundle.path.glob('cut_*.mp4'))) != len(movies) or not all(p.is_file() and p.stat().st_size > 0 for p in movies):
                raise FramingError(f'컷 저장이 완료되지 않았습니다 (ffmpeg {code}). 미완료 폴더: {bundle.path}')
            self._stamp_timecodes(cuts)
            listing = bundle.path / 'cuts.csv'
            with listing.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(cuts[0]))
                writer.writeheader(); writer.writerows(cuts)
            bundle.write(status='complete')
        except Exception as exc:
            bundle.write(error=str(exc))
            raise
        return JobResult(target, ok=True, output_dir=bundle.path, outputs=movies + [listing, bundle.record],
                         next_paths=[str(p) for p in movies], cuts=cuts,
                         message=f'{len(timeline.times)}프레임 → {len(cuts)}컷 · MP4 + 컷 목록')


