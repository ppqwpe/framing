"""대조 결과 내보내기"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .errors import FramingError
from .util import FrameRate, unique_path
from .compare_page import ComparePage
from .jobs_compare import COMPARE_BY_KEY, CompareAudioPolicy, CompareBurner, ConfiguredCompareView


@dataclass(frozen=True)
class CompareRegion:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def read(cls, raw, size):
        if raw is None:
            return None
        values = [raw.get(key) for key in ('x', 'y', 'width', 'height')]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) or int(v) != v for v in values):
            raise FramingError('선택 영역은 정수 픽셀 좌표여야 합니다.')
        x, y, width, height = map(int, values)
        if x < 0 or y < 0 or width < 1 or height < 1 or x + width > size[0] or y + height > size[1]:
            raise FramingError('선택 영역이 A 기준 화면을 벗어났습니다.')
        return cls(x, y, width, height)

    def tuple(self):
        return self.x, self.y, self.width, self.height


class CompareExporter:
    FORMATS = ('frame', 'video', 'frames', 'both')
    MODES = ('wipe', 'overlay', 'flicker', 'side', 'a', 'b')

    def __init__(self, ff, facts, state, options=None, cancelled=None, progress=None):
        self.ff, self.facts = ff, dict(facts)
        self.cancelled = cancelled or (lambda: False)
        self.progress = progress or (lambda data: None)
        self.state, self.options = dict(state or {}), dict(options or {})
        self.first, self.second = Path(facts['a']), Path(facts['b'])
        self.video = bool(facts['video'])
        self.size = (int(facts['width']), int(facts['height']))
        if min(self.size) < 1:
            raise FramingError('대조 해상도를 확인할 수 없습니다.')
        self.fps = float(facts.get('fps') or 24)
        self.seconds = float(facts.get('seconds') or 0)
        if not math.isfinite(self.fps) or not 0 < self.fps <= 1000 or not math.isfinite(self.seconds) or self.seconds < 0:
            raise FramingError('대조 시간 정보를 확인할 수 없습니다.')
        self.format = self.options.get('format') or ('video' if self.video else 'frame')
        if self.format not in self.FORMATS or (not self.video and self.format != 'frame'):
            raise FramingError('이 소재에서 지원하지 않는 저장 형식입니다.')
        if self.options.get('area', 'selection') not in ('selection', 'full') or self.options.get('views', 'current') not in ('current', 'all'):
            raise FramingError('저장할 보기 또는 영역 설정이 올바르지 않습니다.')
        if self.video and self.seconds <= 0:
            raise FramingError('영상 길이를 확인할 수 없습니다.')
        self.count = FrameRate.frame_count(self.seconds, self.fps) if self.video else 1
        at = float(self.state.get('time') or 0)
        if not math.isfinite(at) or at < 0:
            raise FramingError('재생 위치가 올바르지 않습니다.')
        # Browsers can quantize currentTime to microseconds after an exact frame seek.
        self.frame = min(self.count - 1, max(0, math.floor(at * self.fps + 1e-3)))
        self.region = CompareRegion.read(self.state.get('region') if self.options.get('area', 'selection') == 'selection' else None, self.size)
        mode = ComparePage.engine_key(self.state.get('mode', 'wipe'))
        if mode not in self.MODES:
            raise FramingError('알 수 없는 대조 보기입니다.')
        self.modes = self.MODES if self.options.get('views') == 'all' else (mode,)
        self.audio = CompareAudioPolicy(ff, self.first, self.second, self.state) if self.video else None
        # Validate ratios before creating any output directory.
        ConfiguredCompareView(COMPARE_BY_KEY.get(mode, COMPARE_BY_KEY['wipe']), self.state)

    def save(self):
        folder = unique_path(self.first.parent / f'{self.first.stem}_compare_export')
        folder.mkdir(parents=True, exist_ok=False)
        files = []
        metadata = {'status': 'incomplete', 'sourceA': str(self.first), 'sourceB': str(self.second),
                    'canvas': list(self.size), 'fps': self.fps, 'duration': self.seconds,
                    'frameIndex': self.frame, 'frameNumbering': 'PNG starts at 1; frameIndex starts at 0',
                    'region': self.region.tuple() if self.region else None,
                    'state': self.state, 'format': self.format, 'views': self.modes,
                    'audioSource': self.audio.source if self.audio else 'none',
                    'audioVolume': self.audio.volume if self.audio else 0,
                    'videoPadding': 'Odd dimensions padded at right/bottom to even for H.264 playback',
                    'alignment': 'A canvas; aspect-preserving fit with centered black padding',
                    'files': []}
        manifest = folder / 'comparison.json'
        def record(status):
            metadata.update(status=status, files=[p.relative_to(folder).as_posix() for p in files])
            manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        record('incomplete')
        try:
            units = len(self.modes) * (2 if self.format == 'both' else 1)
            done = 0
            for mode in self.modes:
                state = dict(self.state, mode=mode)
                view = ConfiguredCompareView(COMPARE_BY_KEY.get(mode, COMPARE_BY_KEY['wipe']), state)
                first, second = (self.second, self.first) if view.swapped else (self.first, self.second)
                common = dict(size=self.size, seconds=self.seconds, video=self.video, fps=self.fps,
                              region=self.region.tuple() if self.region else None, audio=self.audio, swapped=view.swapped)
                kinds = ('video', 'frames') if self.format == 'both' else (self.format,)
                for kind in kinds:
                    if self.cancelled():
                        raise FramingError(f'저장을 중단했습니다. 완료된 파일은 {folder}에 있습니다.')
                    self.progress({'done': done, 'total': units, 'mode': mode, 'format': kind})
                    destination = folder / (f'{mode}.mp4' if kind == 'video' else
                                           f'{mode}_frame_{self.frame + 1:06d}.png' if kind == 'frame' else f'{mode}_frames/frame.%06d.png')
                    made = CompareBurner(self.ff).burn(first, second, view, destination, **common,
                                output=kind, frame=self.frame if kind == 'frame' else None)
                    if self.cancelled():
                        raise FramingError(f'저장을 중단했습니다. 완료된 파일은 {folder}에 있습니다.')
                    if made is None:
                        raise FramingError(f'{mode} 저장에 실패했습니다. {self.ff.why_failed()} 완료된 파일은 {folder}에 남아 있습니다.')
                    outputs = sorted(destination.parent.glob('frame.*.png')) if kind == 'frames' else [made]
                    files.extend(outputs)
                    if kind == 'frames' and len(outputs) != self.count:
                        raise FramingError(f'프레임 수가 다릅니다: 예상 {self.count}, 저장 {len(outputs)}. {folder}')
                    record('incomplete')
                    done += 1
                    self.progress({'done': done, 'total': units, 'mode': mode, 'format': kind})
            record('complete')
        except Exception:
            record('incomplete')
            raise
        return folder, files, manifest


