"""작업 결과 · 작업 옵션 · 화면 옵션"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional


@dataclass
class JobResult:
    target: Path
    ok: bool = False
    message: str = ""
    output_dir: Optional[Path] = None
    outputs: list[Path] = field(default_factory=list)
    size_in: int = 0
    size_out: int = 0
    inspection: Optional[dict] = None
    previews: list[str] = field(default_factory=list)
    shots: list[dict] = field(default_factory=list)
    inspections: list[dict] = field(default_factory=list)
    next_paths: Optional[list[str]] = None
    rename_map: Optional[list] = None
    retime_map: Optional[dict] = None
    cuts: list[dict] = field(default_factory=list)

    def as_event(self, name: str) -> dict:
        return {'name': name, 'message': self.message,
                'dir': str(self.output_dir) if self.output_dir else '',
                'files': [str(p) for p in self.outputs[:12]], 'fileCount': len(self.outputs),
                'sizeIn': self.size_in, 'sizeOut': self.size_out,
                'renamed': self.rename_map, 'retime': self.retime_map,
                'inspection': self.inspection, 'previews': self.previews,
                'shots': self.shots, 'inspections': self.inspections, 'nextFiles': self.next_paths,
                'cuts': self.cuts}


@dataclass
class Options:
    """CLI 와 메뉴가 공유하는 설정 묶음."""
    language: str = 'kr'               # Output reports; independent of filename language
    frame_format: str = "both"          # keyframes: png | jpg | both
    jpeg_quality: int = 95
    at: str = ""                        # keyframes: 어디를 뽑을지
    every: int = 0                      # keyframes: 매 N프레임
    count: int = 0                      # keyframes: 균등하게 N장
    levels: str = "auto"                # keyframes: auto | keep | full (밝기 범위)
    out_dir: Optional[Path] = None
    fps: float = 24
    make_mp4: bool = True
    convert_to: str = "mp4"             # convert: mp4 | mov | nuke | small | image | movie
    image_format: str = "png"           # convert: 이미지 목적지 형식
    prores_movie: bool = False          # convert: 시퀀스를 누크용으로 묶을지
    gif_fps: float = 12
    gif_width: int = 640
    gif_loop: bool = True
    cut_mode: str = 'auto'
    cut_threshold: float = 10
    cut_min_seconds: float = 0.5
    cut_interval: float = 5
    cut_times: str = ''
    view: str = "wipe"                  # compare: wipe | overlay | flicker | side
    rename_rules: list[dict] = field(default_factory=list)
    audit_freeze_seconds: float = 0.5
    audit_sensitivity: str = "balanced"
    rife_program: str = ""              # plate: RIFE 실행 파일 (없으면 명령만 알려준다)
    plate_no_snap: bool = False         # plate: 원본을 잡으려고 당기는 것을 끔
    stretch_note: str = ""              # revert: 늘림 메모 경로 (비우면 알아서 찾음)
    drop_frames: str = ""               # revert: 메모 없이 뺄 자리 (tail:20 · 8,21,34)
    hold_where: str = "tail"            # hold: tail | head | both
    contact_audit: bool = False
    rename_mode: str = "pattern"        # rename: pattern | docker
    rename_pattern: str = "장면_{n}"     # rename: {n} 자리에 번호
    rename_start: int = 1               # rename: 시작 번호
    rename_lang: str = "kr"             # rename 도커식: kr(한글 두 단어) | en(도커 원본)
    rename_find: str = ""               # rename: 찾을 말 (쉼표로 여러 개)
    rename_replace: str = ""            # rename: 바꿀 말 (비우면 지움)
    rename_prefix: str = ""             # rename: 앞에 붙일 말 (bg · ch · plate …)
    rename_suffix: str = ""             # rename: 뒤에 붙일 말
    rename_cut_head: int = 0            # rename: 앞에서 잘라낼 글자 수
    rename_cut_tail: int = 0            # rename: 뒤에서 잘라낼 글자 수
    keep_compare: bool = False          # compare: 결과를 원본 폴더에 남길지
    h265: bool = False
    target_frames: int = 0              # retime: 목표 프레임 수
    target_seconds: float = 0.0         # retime: 목표 길이(초)
    speed: float = 0.0                  # retime: 배속 (0.5 = 절반 속도)
    retime_mode: str = "auto"           # retime: auto | dup | hold | blend | motion
    no_snap: bool = False               # retime: 라벨 색 복원 끄기
    retime_note: bool = False           # retime: 손댄 자리를 메모장으로도 남길지
    delivery: str = "auto"              # retime · reverse: 결과를 어떤 모습으로
                                        #   auto | mp4 | mov | seq | both
    delivery_shot: str = ""            # delivery: 샷코드 (비우면 원본 이름에서 물려받음)
    delivery_task: str = ""            # delivery: 테스크 이름 (비우면 ai)
    delivery_version: int = 0          # delivery: 버전 (0 이면 비어 있는 다음 번호)
    level: str = "medium"
    max_height: int = 0
    force_encode: bool = False
    overwrite: bool = False
    recursive: bool = True


class UiOptions:
    """
    화면이 보낸 설정 뭉치를 Options 로 옮긴다.

    예전에는 여기에 이름 대조표가 손으로 적혀 있었다 (``'cutTimes':
    'cut_times'`` 를 마흔 줄). 그래서 Options 에 항목을 하나 더해도 화면이
    보낸 값이 조용히 버려졌다 — 아무 데서도 오류가 나지 않아 알아채는 데
    오래 걸렸다. 이름 규칙은 늘 같았으므로(``cutTimes`` ↔ ``cut_times``)
    대조표 대신 규칙을 쓴다. 항목이 늘면 저절로 따라온다.

    받은 값의 꼴도 여기서 맞춘다. 화면의 숫자 칸은 빈칸일 때 문자열을
    보내는데, 그걸 그대로 두면 한참 뒤 ffmpeg 인자에서 터진다.
    """

    SKIP = ('out_dir',)          # 경로는 부르는 쪽이 따로 다룬다

    def __init__(self, raw: Optional[dict] = None) -> None:
        self._raw = dict(raw or {})

    @staticmethod
    def snake(name: str) -> str:
        """``cutMinSeconds`` → ``cut_min_seconds``"""
        out = []
        for letter in name:
            if letter.isupper():
                out.append('_')
            out.append(letter.lower())
        return ''.join(out)

    # 이 파일은 annotations 를 미루므로 spec.type 이 글자다. 글자로 본다.
    SHAPES = {'bool': bool, 'int': int, 'float': float, 'str': str}

    @classmethod
    def _shape(cls, value, spec):
        """선언한 자료형으로 맞춘다. 못 맞추면 버린다 (기본값이 남는다)."""
        want = cls.SHAPES.get(str(spec.type).strip())
        if want is None:                      # Optional[Path] · list[dict] — 그대로
            return value
        if want is bool:
            return value if isinstance(value, bool) else str(value).lower() in ('1', 'true', 'on')
        if want is str:
            return value if isinstance(value, str) else str(value)
        try:
            return want(value)
        except (TypeError, ValueError):
            return None

    def fill(self, options: 'Options') -> 'Options':
        specs = {f.name: f for f in fields(options)}
        for js_name, value in self._raw.items():
            spec = specs.get(self.snake(js_name))
            if value is None or spec is None or spec.name in self.SKIP:
                continue
            shaped = self._shape(value, spec)
            if shaped is not None:
                setattr(options, spec.name, shaped)
        out = self._raw.get('outDir')
        options.out_dir = Path(out) if out else None
        return options

