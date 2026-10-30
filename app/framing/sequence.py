"""시퀀스 입력 · 결과 폴더 · 프레임레이트 · 시퀀스→영상 · GIF"""
from __future__ import annotations

import json
import math
import os
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path

from .errors import FramingError
from .options import JobResult
from .env import cv2
from .util import Console, FrameRate, ImageIO, human, unique_path
from .media import collect_images
from .frames import ColorProfile
from .job import Job


"""Reusable sequence input and file delivery for media conversion."""


def bounded_number(value, label, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FramingError(f'{label}: 숫자를 입력하세요.')
    if not math.isfinite(number) or not low <= number <= high:
        raise FramingError(f'{label}: {low:g}~{high:g} 범위로 입력하세요.')
    return number


class OutputFolder:
    """Each execution owns a fresh folder and a completion record."""
    def __init__(self, source, root, suffix, record):
        self.path = unique_path((Path(root) if root else source.parent) / (source.stem + suffix))
        self.path.mkdir(parents=True, exist_ok=False)
        self.record = self.path / record
        self.data = {'status': 'incomplete', 'source': str(source)}
        self.write()

    def write(self, **changes):
        self.data.update(changes)
        self.record.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')


class SequenceInput(AbstractContextManager):
    """Ordered image files with an explicit demuxer time base and frame count."""
    def __init__(self, source, fps, abort):
        self.images = collect_images(source)
        if not self.images:
            raise FramingError('이미지 시퀀스 폴더를 넣어 주세요.')
        self.rate = FrameRate.from_value(bounded_number(fps, '시퀀스 FPS', 1, 240))
        self.fps = self.rate.value
        self.duration = len(self.images) / self.fps
        self.first = None
        for path in self.images:
            abort()
            frame = ImageIO.read(path, cv2.IMREAD_UNCHANGED)
            if frame is None:
                raise FramingError(f'이미지를 읽지 못했습니다: {path.name}')
            if self.first is None:
                self.first = frame
            elif frame.shape != self.first.shape or frame.dtype != self.first.dtype:
                raise FramingError(f'시퀀스의 크기·채널·비트 깊이를 맞춰 주세요: {path.name}')

    @staticmethod
    def text(images, fps):
        rate = FrameRate.from_value(fps)
        def entry(path):
            quoted = path.resolve().as_posix().replace("'", "'\\''")
            return f"file '{quoted}'\noption framerate {rate.text}\n"
        # The per-file framerate prevents the image demuxer's default 25 fps
        # from rounding timestamps and repeating/skipping sequence images.
        return ('ffconcat version 1.0\n' +
                ''.join(entry(p) + f'duration {1/fps:.12f}\n' for p in images) + entry(images[-1]))

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='framing_sequence_')
        listing = Path(self.temp.name) / 'frames.ffconcat'
        listing.write_text(self.text(self.images, self.fps), encoding='utf-8')
        self.args = ['-f', 'concat', '-safe', '0', '-i', str(listing)]
        return self

    def __exit__(self, *exc):
        self.temp.cleanup()


class SequenceMovieJob(Job):
    def run(self, target):
        self._ff.ensure()
        with SequenceInput(target, self._opt.fps, self._abort_if_asked) as source:
            prores = self._opt.prores_movie
            alpha = source.first.ndim == 3 and source.first.shape[2] == 4
            height, width = source.first.shape[:2]
            destination = self._output_file(target, '_movie', '.mov' if prores else '.mp4')
            if prores:
                codec = ['-c:v', 'prores_ks', '-profile:v', '4444' if alpha else '3',
                         '-pix_fmt', 'yuva444p10le' if alpha else 'yuv422p10le',
                         '-video_track_timescale', str(source.rate.numerator)]
            else:
                codec = ['-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-pix_fmt',
                         'yuv444p' if width % 2 or height % 2 else 'yuv420p', '-movflags', '+faststart']
                if alpha:
                    Console.info('MP4에는 알파가 저장되지 않습니다. 알파가 필요하면 ProRes를 선택하세요.')
            with tempfile.TemporaryDirectory(prefix='framing_movie_', dir=destination.parent) as temp:
                movie = Path(temp) / destination.name
                code = self._ff.run(['-y', *source.args, '-vf', f'fps={source.rate.text},' + ColorProfile().to_yuv_filter(),
                                     *codec, '-frames:v', str(len(source.images)), '-an', str(movie)],
                                    total_seconds=source.duration)
                self._abort_if_asked()
                if code or not movie.is_file():
                    raise FramingError(f'시퀀스 영상 저장 실패 (ffmpeg {code})')
                os.replace(movie, destination)
            return JobResult(target, ok=True, output_dir=destination.parent, outputs=[destination],
                             size_out=destination.stat().st_size,
                             message=f'{len(source.images)}장 → {source.duration:.3f}초 · {source.fps:g}fps · {destination.name}')


class GifJob(Job):
    """Two-pass palette generation keeps the whole video out of RAM."""
    def run(self, target):
        self._ff.ensure()
        self.rate = bounded_number(self._opt.gif_fps, 'GIF FPS', 1, 50)
        self.width = int(bounded_number(self._opt.gif_width, 'GIF 가로 크기', 16, 4096))
        if target.is_dir():
            with SequenceInput(target, self._opt.fps, self._abort_if_asked) as source:
                return self.render(target, source.args, source.duration)
        info = self._ff.probe(target)
        if not info or not info.vcodec or info.duration <= 0:
            raise FramingError('GIF로 바꿀 영상의 길이를 읽지 못했습니다.')
        return self.render(target, ['-i', str(target)], info.duration)

    def render(self, target, inputs, duration):
        destination = self._output_file(target, '_animated', '.gif')
        filters = (f'trim=duration={duration:.12f},setpts=PTS-STARTPTS,fps={self.rate:g},'
                   f"scale=w='min(iw,{self.width})':h=-1:flags=lanczos,setsar=1")
        with tempfile.TemporaryDirectory(prefix='framing_gif_', dir=destination.parent) as temp:
            palette, movie = Path(temp) / 'palette.png', Path(temp) / 'result.gif'
            Console.info('GIF 1/2 · 전체 영상의 색상표를 만듭니다')
            code = self._ff.run(['-y', *inputs, '-vf', filters + ',palettegen', '-frames:v', '1', str(palette)],
                                total_seconds=duration)
            self._abort_if_asked()
            if code or not palette.is_file():
                raise FramingError(f'GIF 색상표 생성 실패 (ffmpeg {code})')
            Console.info('GIF 2/2 · 애니메이션을 저장합니다')
            graph = f'[0:v]{filters}[v];[v][1:v]paletteuse=dither=sierra2_4a[out]'
            code = self._ff.run(['-y', *inputs, '-i', str(palette), '-filter_complex', graph,
                                 '-map', '[out]', '-an', '-loop', '0' if self._opt.gif_loop else '-1',
                                 '-t', f'{duration:.12f}', str(movie)], total_seconds=duration)
            self._abort_if_asked()
            if code or not movie.is_file():
                raise FramingError(f'GIF 저장 실패 (ffmpeg {code})')
            os.replace(movie, destination)
        return JobResult(target, ok=True, output_dir=destination.parent, outputs=[destination],
                         size_out=destination.stat().st_size,
                         message=f'GIF · {self.rate:g}fps · 최대 {self.width}px · {human(destination.stat().st_size)}',
                         next_paths=[])

