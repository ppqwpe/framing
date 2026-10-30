"""검사 · 콘택트시트 작업"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import FramingError
from .options import JobResult
from .env import DependencyBootstrap, IMAGE_EXTENSIONS, cv2, np
from .util import FrameNumber, ImageIO, Progress, unique_path
from .media import ImageSequenceFeed, ProbeInfo, VideoSource, open_feed
from .i18n import MessageCatalog
from .job import Job
from .audit import FrameAudit, SequenceInventory


class AuditReport:
    """Serialize findings once, resolving source paths in internal index space."""

    def __init__(self, source, audit, inventory=None):
        self.source, self.audit, self.inventory = source, audit, inventory

    def as_dict(self):
        audit = self.audit
        findings = []
        for finding in audit.finish():
            row = finding.as_dict(audit.fps)
            if self.inventory and not row['path']:
                row['path'] = str(self.inventory.paths[min(finding.first, len(self.inventory.paths) - 1)])
            findings.append(row)
        return {'source': str(self.source), 'fps': audit.fps, 'frames': audit.count,
                'candidateOnly': True, 'truncated': audit.truncated,
                'settings': {'minimumSeconds': audit.minimum / audit.fps,
                             'sensitivity': audit.sensitivity, 'method': 'anchor-color-tiles-v2',
                             'frameNumberBase': FrameNumber.BASE}, 'findings': findings}


class AuditJob(Job):
    KEY = 'audit'
    TITLE = '샷 검사'
    HINT = '검은·정지·중복 후보와 시퀀스 누락 확인'
    ACCEPTS_FOLDER = True
    NOTE = '원본을 수정하지 않고 검토할 후보를 찾습니다. 프레임 번호는 1부터입니다.'

    def run(self, target: Path) -> JobResult:
        inventory = SequenceInventory(target) if target.is_dir() else None
        feed = ImageSequenceFeed(inventory.paths) if inventory else open_feed(target)
        with feed:
            audit = FrameAudit((self._opt.fps if inventory else feed.fps) or 24,
                               self._opt.audit_freeze_seconds, self._opt.audit_sensitivity)
            if inventory:
                for finding in inventory.findings:
                    audit.add(finding.kind, finding.first, finding.last, finding.message, finding.path)
            expected = feed.count
            for index, frame in enumerate(feed.frames()):
                self._abort_if_asked()
                audit.feed(frame, str(inventory.paths[index]) if inventory else '')
                if index % 48 == 0: Progress.tick(index + 1, max(expected, index + 1))
            if not audit.count: raise FramingError('읽을 수 있는 프레임이 없습니다.')
            if not inventory and expected > audit.count + 1:
                audit.add('decode', audit.count - 1, audit.count - 1, f'일찍 디코딩이 끝났습니다 · 예상 {expected}, 읽음 {audit.count}')
            findings = audit.finish()
        data = AuditReport(target, audit, inventory).as_dict()
        self._abort_if_asked()
        destination = self._output_file(target, '_inspection', '.json')
        destination.write_text(json.dumps(MessageCatalog().report(data, self._opt.language), ensure_ascii=False, indent=2), encoding='utf-8')
        result = JobResult(target, ok=True, message=f'{audit.count}프레임 검사 · 검토 후보 {len(findings)}곳',
                           output_dir=destination.parent, outputs=[destination])
        result.inspection = data
        result.next_paths = []
        return result


class ContactSheetRenderer:
    """A readable, paginated first/middle/last-frame sheet with Unicode labels."""

    def __init__(self, language='kr') -> None:
        self.language = language
        self.messages = MessageCatalog()
        DependencyBootstrap('PIL', ('Pillow',)).ensure()
        from PIL import Image, ImageDraw, ImageFont, ImageOps
        self.Image, self.Draw, self.Font, self.Ops = Image, ImageDraw, ImageFont, ImageOps

    def font(self, size):
        fonts = [Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/malgun.ttf',
                 Path('/System/Library/Fonts/AppleSDGothicNeo.ttc'),
                 Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
                 Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
        for font in fonts:
            if font.is_file(): return self.Font.truetype(str(font), size)
        return self.Font.load_default()

    def image(self, frame):
        if frame.dtype == np.uint16: frame = (frame / 257).astype(np.uint8)
        if frame.ndim == 2: return self.Image.fromarray(frame).convert('RGB')
        return self.Image.fromarray(cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2RGB))

    def render(self, rows, destination: Path, page: int) -> None:
        canvas = self.Image.new('RGB', (1280, 80 + len(rows) * 290), '#11151b')
        draw = self.Draw.Draw(canvas)
        draw.text((24, 18), f'Framing  /  CONTACT SHEET  /  {page}', font=self.font(26), fill='#eebc72')
        for index, row in enumerate(rows):
            top = 80 + index * 290
            label = row['name']
            while draw.textlength(label, font=self.font(21)) > 1210: label = label[:-2] + '…'
            draw.text((24, top), label, font=self.font(21), fill='white')
            draw.text((24, top + 28), self.messages.text(row['info'], self.language), font=self.font(15), fill='#bac6d5')
            color_line = self.messages.text(row['color']['summary'], self.language)
            while draw.textlength(color_line, font=self.font(13)) > 1210: color_line = color_line[:-2] + '…'
            draw.text((24, top + 47), color_line, font=self.font(13), fill='#bac6d5')
            for slot, (frame, caption) in enumerate(row['frames']):
                left = 24 + slot * 416
                thumb = self.Ops.contain(self.image(frame), (400, 176))
                canvas.paste(thumb, (left + (400 - thumb.width) // 2, top + 70 + (176 - thumb.height) // 2))
                while draw.textlength(caption, font=self.font(15)) > 396: caption = caption[:-2] + '…'
                draw.text((left, top + 250), caption, font=self.font(15), fill='#bac6d5')
        canvas.save(destination, format='PNG')


class ContactSheetJob(Job):
    KEY = 'contact'
    TITLE = '샷 정보 · 콘택트시트'
    HINT = '첫·중간·끝 프레임을 한 장으로'
    GROUPED = True
    ACCEPTS_FOLDER = True
    ACCEPTS_IMAGE = True
    NOTE = '선택한 파일을 6개씩 한 페이지로 묶습니다. PNG 미리보기이며 원본 화질 검증용은 아닙니다.'

    def run(self, target: Path) -> JobResult:
        return self.run_batch([target])

    @staticmethod
    def _thumbnail(frame):
        if frame is None: return None
        height, width = frame.shape[:2]
        scale = min(1.0, 400 / width, 186 / height)
        return cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))),
                          interpolation=cv2.INTER_AREA) if scale < 1 else frame

    def _row(self, target: Path) -> dict:
        if target.is_dir():
            paths = SequenceInventory(target).paths
            if not paths: raise FramingError(f'{target.name}: 이미지가 없습니다.')
            indices = [0, (len(paths) - 1) // 2, len(paths) - 1]
            frames = [(self._thumbnail(ImageIO.read(paths[i])), f'#{FrameNumber.show(i)} · {paths[i].name}') for i in indices]
            sample = ImageIO.read(paths[0]); height, width = sample.shape[:2]
            info = f'{width} × {height} · {len(paths)} frames · sequence'
        elif target.suffix.lower() in IMAGE_EXTENSIONS:
            frames = [(self._thumbnail(ImageIO.read(target)), 'STILL')]
            sample = ImageIO.read(target); height, width = sample.shape[:2]
            info = f'{width} × {height} · Still image'
        else:
            with VideoSource(target) as source:
                total, fps = source.frame_count, source.fps or 24.0
                indices = [0, max(0, (total - 1) // 2), max(0, total - 1)]
                frames = []
                for i in indices:
                    self._abort_if_asked()
                    frame, actual = source.read_at(i)
                    frames.append((self._thumbnail(frame), f'#{FrameNumber.show(actual)} · {actual / fps:.2f}s'))
                width, height = source.resolution
                info = f'{width} × {height} · {fps:.3f} fps · {total / fps:.2f}s · {total} frames'
        if any(frame is None for frame, _ in frames):
            raise FramingError(f'{target.name}: 미리보기 프레임을 읽지 못했습니다.')
        probe_target = paths[0] if target.is_dir() else target
        probe = self._ff.probe(probe_target) if self._ff and self._ff.find('ffprobe') else None
        color = (probe or ProbeInfo()).color_metadata
        return {'name': target.name, 'info': info, 'frames': frames, 'color': color}

    def run_batch(self, targets: list[Path]) -> JobResult:
        if not targets: raise FramingError('콘택트시트로 볼 파일을 고르세요.')
        renderer = ContactSheetRenderer(self._opt.language)
        folder = self._output_dir(targets[0], '_contact')
        made, rows, shots, inspections = [], [], [], []
        try:
            for index, target in enumerate(targets):
                self._abort_if_asked()
                shot = self._row(target)
                rows.append(shot)
                shots.append({'name': shot['name'], 'path': str(target), 'info': shot['info'], 'color': shot['color']})
                if self._opt.contact_audit and (target.is_dir() or target.suffix.lower() not in IMAGE_EXTENSIONS):
                    audit = AuditJob(self._opt, self._ff)
                    audit.should_stop = self.should_stop
                    inspected = audit.run(target)
                    inspections.append(inspected.inspection)
                Progress.tick(index + 1, len(targets))
                if len(rows) == 6 or index == len(targets) - 1:
                    destination = unique_path(folder / f'contact_{len(made) + 1:03d}.png')
                    made.append(destination)
                    renderer.render(rows, destination, len(made))
                    rows = []
        except BaseException:
            for path in made: path.unlink(missing_ok=True)
            raise
        result = JobResult(targets[0], ok=True, message=f'{len(targets)}개 샷 · {len(made)}페이지',
                           output_dir=folder, outputs=made)
        result.shots, result.inspections = shots, inspections
        result.previews = [str(path) for path in made]
        result.next_paths = []
        return result


