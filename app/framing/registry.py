"""작업 목록 — 이름(과 예전 이름)으로 작업 클래스 찾기"""
from __future__ import annotations


from .job import Job
from .cuts import CutJob
from .jobs_extract import ExtractJob
from .jobs_reverse import ReverseJob
from .jobs_retime import RetimeJob
from .jobs_convert import ConvertJob
from .jobs_compare import CompareJob
from .jobs_rename import RenameJob
from .jobs_plate import PlateHoldJob, PlateRevertJob, PlateRifeJob
from .jobs_review import AuditJob, ContactSheetJob


JOB_CLASSES: tuple[type[Job], ...] = (
    ExtractJob, RetimeJob, ReverseJob, ConvertJob, CompareJob, RenameJob, AuditJob, ContactSheetJob, CutJob,
    PlateRifeJob, PlateRevertJob, PlateHoldJob)
JOB_REGISTRY: dict[str, type[Job]] = {}
for _cls in JOB_CLASSES:
    JOB_REGISTRY[_cls.KEY] = _cls
    for _alias in _cls.ALIASES:                 # 예전 이름도 계속 받아준다
        JOB_REGISTRY.setdefault(_alias, _cls)


