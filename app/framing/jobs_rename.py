"""이름 바꾸기 작업 · 작업 파일"""
from __future__ import annotations

import json
from pathlib import Path

from .errors import FramingError
from .naming import BatchNamePlan, NamePlan, RenameTransaction
from .options import JobResult
from .util import Console
from .job import Job


class RenameJob(Job):
    """AI 가 뱉은 외계어 이름을 사람이 읽는 이름으로.

    번호가 파일 전체를 관통해야 하니(01, 02, …) 하나씩이 아니라 한꺼번에 받는다.
    """

    KEY = "rename"
    ALIASES = ("name",)
    TITLE = "이름 바꾸기"
    HINT = "문자열·번호·확장자를 미리 보고 한 번에 변경"
    NOTE = """
    AI 영상 이름은 dreamina-2026…-n2vns 처럼 사람이 못 읽습니다. 여기서 정리하세요.

      패턴    장면_{n}  →  장면_01.mp4, 장면_02.mp4 …  ({n} 자리에 번호)
      도커식  조용한_수달.mp4 처럼 기억하기 쉬운 두 단어를 무작위로

    실행하기 전에 무엇이 무엇으로 바뀌는지 전부 보여주고,
    하나라도 부딪히면 아무것도 바꾸지 않습니다. 넣은 순서대로 번호가 붙습니다."""
    ACCEPTS_IMAGE = True
    GROUPED = True

    def run(self, target: Path) -> JobResult:            # GROUPED 라 여기로는 안 온다
        return self.run_batch([target])

    def run_batch(self, targets: list[Path]) -> JobResult:
        if not targets:
            raise FramingError("이름을 바꿀 파일을 고르세요.")
        result = JobResult(target=targets[0])
        plan = NamePlan(targets, self._opt.rename_mode,
                        self._opt.rename_pattern, self._opt.rename_start,
                        self._opt.rename_lang,
                        self._opt.rename_find, self._opt.rename_replace,
                        self._opt.rename_prefix, self._opt.rename_suffix,
                        self._opt.rename_cut_head, self._opt.rename_cut_tail).mapping()
        if self._opt.rename_mode == 'batch':
            plan = BatchNamePlan(targets, self._opt.rename_rules).mapping()
        renamed = RenameTransaction(plan).execute()
        for old, new in renamed:
            Console.info(f"{old.name}  →  {new.name}")
        result.outputs = [new for _old, new in renamed]
        result.output_dir = result.outputs[0].parent if result.outputs else None
        result.ok = True
        result.message = f"{len(renamed)}개 이름 바꿈" if renamed else "바뀔 이름이 없습니다"
        result.rename_map = [(str(old), str(new)) for old, new in renamed]
        return result


class ProjectDocument:
    """Versioned, data-only project files. Loading never executes a job."""

    VERSION = 1
    LIMIT = 2 * 1024 * 1024

    @classmethod
    def validate(cls, data: dict) -> dict:
        from .registry import JOB_REGISTRY   # 순환 임포트를 피해 여기서
        if not isinstance(data, dict) or data.get('format') not in ('framing-project', 'shotkit-project') or data.get('version') != cls.VERSION:
            raise FramingError('지원하는 Framing 작업 파일이 아닙니다.')
        try:
            if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')) > cls.LIMIT:
                raise FramingError('작업 파일이 너무 큽니다 (최대 2MB).')
        except (TypeError, ValueError) as exc:
            raise FramingError('작업 설정을 읽을 수 없습니다.') from exc
        paths, selected, steps = data.get('paths', []), data.get('selected', []), data.get('steps', [])
        if not isinstance(paths, list) or len(paths) > 10000 or any(not isinstance(p, str) or not p or '\x00' in p for p in paths):
            raise FramingError('파일 목록이 올바르지 않습니다.')
        if not isinstance(selected, list) or any(not isinstance(p, str) for p in selected):
            raise FramingError('선택 목록이 올바르지 않습니다.')
        if not isinstance(steps, list) or len(steps) > 20:
            raise FramingError('작업 단계가 올바르지 않습니다.')
        for step in steps:
            if not isinstance(step, dict) or step.get('key') not in JOB_REGISTRY or not isinstance(step.get('options'), dict):
                raise FramingError('알 수 없는 작업 단계입니다.')
        solo = {'compare', 'audit', 'contact'}
        if len(steps) > 1 and any(step['key'] in solo for step in steps):
            raise FramingError('검사·콘택트시트·대조는 단독 작업으로 저장하세요.')
        return {'format': 'framing-project', 'version': cls.VERSION,
                'name': str(data.get('name', 'Framing 작업'))[:100],
                'paths': list(dict.fromkeys(paths)), 'selected': [p for p in selected if p in paths],
                'steps': steps, 'editing': max(0, min(int(data.get('editing', 0)), max(0, len(steps) - 1)))}

    @classmethod
    def dumps(cls, data: dict) -> str:
        return json.dumps(cls.validate(data), ensure_ascii=False, indent=2, allow_nan=False)

    @classmethod
    def loads(cls, text: str) -> dict:
        if len(text.encode('utf-8')) > cls.LIMIT:
            raise FramingError('작업 파일이 너무 큽니다 (최대 2MB).')
        try: return cls.validate(json.loads(text))
        except (TypeError, ValueError) as exc: raise FramingError('작업 파일을 읽을 수 없습니다.') from exc


