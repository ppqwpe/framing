"""오류 · 작업 하나만 돌게 하는 문 · 엔진 소식 다리"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock


# Standalone hardened derivative; original generated sources were not supplied.


class FramingError(Exception):
    """작업 중 발생한, 이 파일 하나만 실패시키면 되는 오류."""


class OperationGate:
    """Serializes background jobs and synchronous file mutations."""
    def __init__(self):
        self._lock = Lock()

    def claim(self):
        return self._lock.acquire(blocking=False)

    def release(self):
        self._lock.release()

    @contextmanager
    def exclusive(self):
        if not self.claim():
            raise FramingError('실행 중인 작업이 끝난 뒤 변경하세요.')
        try:
            yield
        finally:
            self.release()


class EngineBridge:
    """Routes engine events to the current caller without global method replacement."""
    _sink = ContextVar('framing_event_sink', default=None)

    def __init__(self, emit):
        self._emit = emit

    def __enter__(self):
        self._token = self._sink.set(self._emit)
        return self

    def __exit__(self, *exc):
        self._sink.reset(self._token)

    @classmethod
    def emit(cls, kind, data):
        sink = cls._sink.get()
        if sink is None:
            return False
        sink(kind, data)
        return True


