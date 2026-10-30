"""Framing 엔진의 창구 — 모든 이름을 한곳에서 꺼내 쓴다 (from framing import core; core.FFmpegTool ...)

실제 코드는 같은 폴더의 모듈들에 있다. 밖에서 바꿔 끼우는 실행 환경 값(SELF · BUILD · FFMPEG_HOME · RIFE_HOME)은
core 가 아니라 framing.env 에 넣어야 다른 모듈이 본다.
"""
from __future__ import annotations

from . import errors, naming, options, env, util, media, i18n, frames  # noqa: F401
from . import retime, job, audit, compare_page, plate, sequence, cuts, jobs_extract  # noqa: F401
from . import jobs_reverse, jobs_retime, jobs_convert, jobs_compare, jobs_rename, jobs_plate, jobs_review, registry  # noqa: F401
from . import compare_export, cli, app, api, server, checkup, desktop  # noqa: F401

_MODULES = (errors, naming, options, env, util, media, i18n, frames, retime, job, audit, compare_page, plate, sequence, cuts, jobs_extract, jobs_reverse, jobs_retime, jobs_convert, jobs_compare, jobs_rename, jobs_plate, jobs_review, registry, compare_export, cli, app, api, server, checkup, desktop)
for _module in _MODULES:
    globals().update({k: v for k, v in vars(_module).items() if not k.startswith('__')})
del _module
