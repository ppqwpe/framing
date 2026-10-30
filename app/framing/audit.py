"""샷 검사 — 정지 구간 · 후보 · 시퀀스 목록"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import FramingError
from .env import cv2, np
from .util import FrameNumber
from .media import collect_images


"""Validated comparison exports from original media and a frozen viewer state."""


"""Streaming media checks with bounded memory and explicit candidate intervals."""


@dataclass
class AuditFinding:
    kind: str
    first: int
    last: int
    message: str
    path: str = ''

    def as_dict(self, fps: float) -> dict:
        """
        내보내는 번호는 **사람이 세는 1부터**다.

        안에서는 리스트 자리(0부터)로 다루다가 여기서 한 번만 옮긴다. 예전에는
        검사만 0부터라서, 옆자리에서 "33·34번이 겹친다" 고 하면 그게 누구 번호인지
        되물어야 했다.
        """
        count = self.last - self.first + 1
        return {'kind': self.kind,
                'first': FrameNumber.show(self.first), 'last': FrameNumber.show(self.last),
                'seconds': self.first / fps if fps else 0,
                'endSeconds': (self.last + 1) / fps if fps else 0,
                'duration': count / fps if fps else 0, 'frameCount': count,
                'message': self.message, 'path': self.path}


class HoldDetector:
    """Compare color, local motion and cumulative drift against a fixed anchor."""
    PRESETS = {'strict': (0.2, 1.25, 3, 0.0005),
               'balanced': (0.75, 3.0, 8, 0.002),
               'tolerant': (1.5, 5.0, 12, 0.004)}

    def __init__(self, sensitivity):
        if sensitivity not in self.PRESETS:
            raise FramingError('홀드 검출 기준을 확인하세요.')
        self.thresholds = self.PRESETS[sensitivity]
        self.anchor = self.previous = None

    @staticmethod
    def sample(frame):
        if frame.ndim == 2: frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        small = cv2.resize(frame[:, :, :3], (320, 180), interpolation=cv2.INTER_AREA).astype(np.float32)
        if frame.dtype == np.uint16: small /= 257.0
        # A small blur suppresses codec noise; tile and pixel checks retain local changes.
        return cv2.GaussianBlur(small, (3, 3), 0)

    def similar(self, a, b):
        delta = np.max(np.abs(a - b), axis=2)
        average, local, pixel, fraction = self.thresholds
        if float(delta.mean()) > average: return False
        tiles = delta.reshape(9, 20, 16, 20).mean(axis=(1, 3))
        return float(tiles.max()) <= local and float(np.mean(delta > pixel)) <= fraction

    def feed(self, sample):
        if self.previous is None:
            self.previous = self.anchor = sample
            return False
        adjacent = self.similar(sample, self.previous)
        held = adjacent and self.similar(sample, self.anchor)
        if not held:
            self.anchor = sample
        self.previous = sample
        return held


class FrameAudit:
    def __init__(self, fps: float, freeze_seconds: float = 0.5, sensitivity='balanced') -> None:
        try:
            fps, seconds = float(fps), float(freeze_seconds)
        except (ValueError, TypeError):
            raise FramingError('검사 FPS와 최소 길이는 숫자로 입력하세요.')
        if not math.isfinite(fps) or not math.isfinite(seconds) or not 0.1 <= seconds <= 10:
            raise FramingError('홀드 최소 길이는 0.1~10초로 입력하세요.')
        self.fps = fps if fps > 0 else 24.0
        self.minimum = max(2, math.ceil(seconds * self.fps))
        self.sensitivity = sensitivity
        self.detector = HoldDetector(sensitivity)
        self.findings = []
        self.previous_hash = None
        self.shape = None
        self.runs = {'black': None, 'freeze': None, 'duplicate': None}
        self.count = 0
        self.truncated = False

    def add(self, kind, first, last, message, path='') -> None:
        if len(self.findings) < 500:
            self.findings.append(AuditFinding(kind, first, last, message, path))
        else: self.truncated = True

    def _run(self, kind: str, active: bool, index: int) -> None:
        first = self.runs[kind]
        if active and first is None:
            self.runs[kind] = max(0, index - (kind != 'black'))
        elif not active and first is not None:
            self._close(kind, index - 1)

    def _close(self, kind: str, last: int) -> None:
        first = self.runs[kind]
        self.runs[kind] = None
        if first is None: return
        minimum = self.minimum if kind == 'freeze' else (2 if kind == 'duplicate' else 1)
        if last - first + 1 >= minimum:
            labels = {'black': '검은 프레임 후보', 'freeze': '홀드 프레임 후보', 'duplicate': '동일 프레임 반복'}
            self.add(kind, first, last, f'{labels[kind]} · {FrameNumber.show(first)}–{FrameNumber.show(last)} 프레임')

    def feed(self, frame, path='') -> None:
        index = self.count
        if self.shape is None: self.shape = frame.shape[:2]
        elif frame.shape[:2] != self.shape:
            self.add('resolution', index, index, f'해상도 다름 · {frame.shape[1]}×{frame.shape[0]}', path)
        sample = self.detector.sample(frame)
        # Hash the decoded pixels, not a thumbnail: a tiny moving object is not a duplicate.
        fingerprint = (frame.shape, str(frame.dtype), hashlib.blake2b(np.ascontiguousarray(frame).data, digest_size=16).digest())
        self._run('black', float(np.mean(np.max(sample, axis=2) <= 12)) >= 0.98, index)
        held = self.detector.feed(sample)
        self._run('freeze', held, index)
        self._run('duplicate', fingerprint == self.previous_hash, index)
        self.previous_hash = fingerprint
        self.count += 1

    def finish(self) -> list:
        for kind in self.runs: self._close(kind, self.count - 1)
        self.findings.sort(key=lambda item: (item.first, item.kind))
        return self.findings


class SequenceInventory:
    """Numeric ordering and missing ranges, without inventing missing frames."""

    def __init__(self, folder: Path) -> None:
        self.paths = collect_images(folder)
        self.findings = []
        groups = {}
        for path in self.paths:
            match = re.match(r'^(.*?)(\d+)$', path.stem)
            if match:
                groups.setdefault((match[1], path.suffix.lower()), []).append((int(match[2]), path))
        if len(groups) == 1 and sum(len(g) for g in groups.values()) == len(self.paths):
            self.paths = [p for _n, p in sorted(next(iter(groups.values())))]
        positions = {path: index for index, path in enumerate(self.paths)}
        for entries in groups.values():
            ordered = sorted(entries)
            for (previous, _), (number, path) in zip(ordered, ordered[1:]):
                if number > previous + 1:
                    self.findings.append(AuditFinding('missing', positions[path], positions[path],
                        f'시퀀스 번호 누락 · {previous + 1}–{number - 1}', str(path)))
                elif number == previous:
                    self.findings.append(AuditFinding('number', positions[path], positions[path],
                        f'시퀀스 번호 중복 · {number}', str(path)))


