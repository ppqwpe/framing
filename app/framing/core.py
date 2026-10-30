# -*- coding: utf-8 -*-
"""
Framing — 영상 작업 도구 모음
=============================
파일 하나. 더블클릭하면 창이 뜬다. 창에 파일을 놓으면 할 수 있는 일이 나온다.

  뽑기     원하는 지점 몇 장, 또는 전부를 시퀀스로 (PNG/JPG/EXR/DPX)
  리타임   길이 · 배속 바꾸기 (프레임 보간, 매트 자동 보호)
  역재생   영상 · 시퀀스를 거꾸로
  변환     MP4 · 누크용 마스터 · 용량 줄이기 · 이미지 · 시퀀스 묶기
  대조     두 영상 · 이미지를 나란히 놓고 달라진 곳 찾기

터미널에서 쓰고 싶으면 인자를 주면 된다.
  윈도우    Framing.bat extract shot.mov --at 1,50%%,last
  맥·리눅스  ./Framing.command compare before.mov after.mov

준비물은 파이썬 3.8 이상뿐이다. opencv · pywebview · ffmpeg 는 처음 한 번 알아서 챙긴다.
(맥에서 ffmpeg 은 Homebrew 로 챙긴다. brew 가 없으면 설치 방법을 알려준다.)

같은 알맹이를 두 벌로 낸다.
  Framing.bat      첫 줄이 윈도우 배치 명령. 파이썬은 -x 로 그 줄을 건너뛴다
  Framing.command  첫 줄이 셸 스크립트. 파이썬 쪽에서는 그냥 문서열이라 그대로 읽힌다

구성
----
  1. 채비      Relauncher · DependencyBootstrap · FFmpegTool
  2. 바탕      Console · ImageIO · 경로 도구
  3. 미디어    VideoSource · FrameFeed · PickSpec · ColorProfile
  4. 작업      Job 과 여섯 자식
  5. 창구      Menu · CLI · EngineBridge · Api · LocalServer · DesktopWindow
  6. 화면      UI_HTML
"""

from __future__ import annotations
# Standalone hardened derivative; original generated sources were not supplied.



from contextvars import ContextVar
from threading import Lock
from contextlib import contextmanager



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



import os
import re
import uuid
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence


@dataclass
class RenameEntry:
    """One file's original, final and current locations during a rename."""

    original: Path
    destination: Path
    temporary: Optional[Path] = None
    current: Path = field(init=False)

    def __post_init__(self) -> None:
        self.current = self.original

    def move_to(self, destination: Path) -> None:
        # Never deliberately replace an existing file, including broken links.
        if os.path.lexists(destination):
            raise FileExistsError(f"{destination} 이 이미 있습니다")
        self.current.rename(destination)
        self.current = destination


class RenameTransaction:
    """Validate the whole plan, apply it, and recover from a failed move.

    Recovery is best effort: a locked file can also block restoration. In that
    case the error reports the surviving paths rather than claiming success.
    """

    def __init__(self, mapping: Sequence[tuple[Path, Path]]) -> None:
        self._entries = [RenameEntry(Path(old).absolute(), Path(new).absolute())
                         for old, new in mapping]

    def _validate(self) -> None:
        originals = [entry.original for entry in self._entries]
        destinations = [entry.destination for entry in self._entries]
        if len(set(originals)) != len(originals):
            raise FramingError("같은 파일이 두 번 들어 있습니다 — 아무것도 바꾸지 않았습니다.")
        if len(set(destinations)) != len(destinations):
            raise FramingError("새 이름끼리 겹칩니다 — 아무것도 바꾸지 않았습니다.")
        sources = set(originals)
        for entry in self._entries:
            if not entry.original.is_file():
                raise FramingError(f"{entry.original} 파일이 없습니다 — 아무것도 바꾸지 않았습니다.")
            if entry.original.parent != entry.destination.parent:
                raise FramingError("이름 변경은 같은 폴더 안에서만 할 수 있습니다.")
            if os.path.lexists(entry.destination) and entry.destination not in sources:
                raise FramingError(f"{entry.destination.name} 이 이미 있습니다 — 아무것도 바꾸지 않았습니다.")

    def _restore(self, entries: Sequence[RenameEntry]) -> list[RenameEntry]:
        # First vacate final names. This also restores A -> B -> A swaps safely.
        for entry in reversed(entries):
            if str(entry.current) not in (str(entry.original), str(entry.temporary)):
                try:
                    entry.move_to(entry.temporary)
                except OSError:
                    pass
        for entry in reversed(entries):
            if str(entry.current) == str(entry.temporary):
                try:
                    entry.move_to(entry.original)
                except OSError:
                    pass
        return [entry for entry in entries if str(entry.current) != str(entry.original)]

    def execute(self) -> list[tuple[Path, Path]]:
        self._validate()
        entries = [entry for entry in self._entries
                   if str(entry.original) != str(entry.destination)]
        try:
            for entry in entries:
                entry.temporary = entry.original.with_name(f"_rn{uuid.uuid4().hex}{entry.original.suffix}")
                entry.move_to(entry.temporary)
            for entry in entries:
                entry.move_to(entry.destination)
        except (Exception, KeyboardInterrupt) as exc:
            unresolved = self._restore(entries)
            if unresolved:
                locations = "\n".join(f"{entry.current} → {entry.original}" for entry in unresolved)
                raise FramingError(f"이름 변경과 일부 복구에 실패했습니다: {exc}\n"
                                   f"남아 있는 파일 → 원래 이름:\n{locations}") from exc
            raise FramingError(f"이름 변경에 실패해 원래 이름으로 복구했습니다: {exc}") from exc
        return [(entry.original, entry.destination) for entry in entries]


class NamePlan:
    """
    이름을 어떻게 바꿀지에 대한 계획. 세우는 것과 실행을 나눈다 —
    디스크를 건드리기 전에 전부 보여주고, 부딪히면 하나도 안 바꾼다.
    """

    # 도커가 컨테이너에 붙이는 식의 기억하기 쉬운 두 단어 이름.
    # AI 가 뱉은 dreamina-2026…-n2vns 같은 이름은 사람이 못 외운다.
    MOODS = ("조용한", "달리는", "붉은", "푸른", "새벽의", "한낮의", "노을진", "비오는",
             "눈내린", "안개낀", "빛나는", "느린", "빠른", "작은", "커다란", "낡은",
             "새로운", "숨은", "열린", "먼", "가까운", "따뜻한", "차가운", "깊은")
    THINGS = ("수달", "등대", "골목", "터널", "광장", "옥상", "정원", "부두",
              "계단", "창문", "다리", "숲길", "간판", "분수", "벤치", "노면",
              "지붕", "담장", "화분", "표지판", "가로등", "우체통", "철로", "냇가")

    # 진짜 도커 목록 — moby/moby internal/namesgenerator 의 형용사 108 × 인물 236.
    # 도커와 같은 골격(형용사_인물, 소문자, 밑줄)이고, 도커가 절대 만들지 않는
    # boring_wozniak 도 똑같이 건너뛴다.
    DOCKER_LEFT = ()      # 아래에서 채운다 — 목록이 길어서 클래스 뒤에 붙인다
    DOCKER_RIGHT = ()

    def __init__(self, targets: Sequence[Path], mode: str, pattern: str,
                 start: int, lang: str = "kr", find: str = "", replace: str = "",
                 prefix: str = "", suffix: str = "", cut_head: int = 0,
                 cut_tail: int = 0) -> None:
        self._targets = list(targets)
        self._mode = (mode or "pattern").lower()
        self._pattern = (pattern or "").strip() or "장면_{n}"
        self._start = max(0, int(1 if start is None else start))
        self._lang = (lang or "kr").lower()
        self._find = find or ""
        self._replace = replace or ""
        self._prefix = prefix or ""
        self._suffix = suffix or ""
        self._cut_head = max(0, int(cut_head or 0))
        self._cut_tail = max(0, int(cut_tail or 0))

    def mapping(self) -> list[tuple[Path, Path]]:
        """[(지금 이름, 새 이름)]. 여기까지는 디스크를 건드리지 않는다."""
        if self._mode == "docker":
            return self._docker_names()
        if self._mode in ("find", "strip"):
            return self._edit_names(strip_only=(self._mode == "strip"))
        if self._mode == "affix":
            return self._affix_names()
        return self._pattern_names()

    # ── 이미 붙어 있는 이름을 고쳐 쓰는 길 ────────────────────────
    #    AI 가 뱉은 dreamina-2026-08-26-cgt-… 에서 쓸데없는 토막만 걷어내고
    #    bg · ch · plate 같은 표시를 붙이는 일이 실제로 제일 많다.

    def _edit_names(self, strip_only: bool) -> list[tuple[Path, Path]]:
        """찾아 바꾸기 / 지우기. 찾을 말은 쉼표로 여러 개."""
        wanted = [w for w in (p.strip() for p in self._find.split(",")) if w]
        if not wanted:
            raise FramingError("무엇을 찾을지 적어 주세요.")
        swap = "" if strip_only else self._replace
        out = []
        for path in self._targets:
            stem = path.stem
            for word in wanted:
                stem = stem.replace(word, swap)
            stem = self._tidy(stem) or path.stem      # 통째로 지워지면 원래대로
            out.append((path, path.with_name(self._clean(stem) + path.suffix.lower())))
        return out

    def _affix_names(self) -> list[tuple[Path, Path]]:
        """앞뒤에 붙이고, 원하면 앞뒤에서 몇 글자 잘라낸다."""
        head, tail = self._prefix.strip("_ "), self._suffix.strip("_ ")
        out = []
        for path in self._targets:
            stem = path.stem
            if self._cut_head:
                stem = stem[self._cut_head:]
            if self._cut_tail:
                stem = stem[: -self._cut_tail] if self._cut_tail < len(stem) else ""
            stem = self._tidy(stem)
            # 두 번 눌러도 bg_bg_ 가 되지 않게 — 이미 붙어 있으면 그냥 둔다
            parts = [
                "" if (head and stem.startswith(head + "_")) else head,
                stem,
                "" if (tail and stem.endswith("_" + tail)) else tail,
            ]
            stem = "_".join(p for p in parts if p) or path.stem
            out.append((path, path.with_name(self._clean(stem) + path.suffix.lower())))
        return out

    @staticmethod
    def _tidy(stem: str) -> str:
        """토막을 걷어내고 남은 밑줄·붙임표가 겹치거나 끝에 매달린 것을 정리."""
        for _ in range(3):
            for pair in ("__", "--", "_-", "-_", " _", "_ "):
                stem = stem.replace(pair, "_" if "_" in pair else "-")
        return stem.strip("_- ")

    def _pattern_names(self) -> list[tuple[Path, Path]]:
        pattern = self._pattern
        if "{n}" not in pattern:
            pattern += "_{n}"
        last = self._start + len(self._targets) - 1
        digits = max(2, len(str(last)))
        out = []
        for order, path in enumerate(self._targets, self._start):
            stem = pattern.replace("{n}", str(order).zfill(digits))
            out.append((path, path.with_name(self._clean(stem) + path.suffix.lower())))
        return out

    def _docker_names(self) -> list[tuple[Path, Path]]:
        seen: set[str] = set()
        out = []
        for path in self._targets:
            stem = self.one_name(self._lang, path.parent, path.suffix.lower(), seen)
            seen.add(stem)
            out.append((path, path.with_name(stem + path.suffix.lower())))
        return out

    @classmethod
    def one_name(cls, lang: str, folder: Path, suffix: str,
                 avoid: set[str] | None = None) -> str:
        """도커식 이름 하나 — 그 폴더에서 부딪히지 않는 것으로."""
        import random
        avoid = avoid or set()
        left, right = ((cls.DOCKER_LEFT, cls.DOCKER_RIGHT)
                       if (lang or "").lower() == "en" else (cls.MOODS, cls.THINGS))
        for _try in range(200):
            stem = f"{random.choice(left)}_{random.choice(right)}"
            if stem == "boring_wozniak":        # 도커도 안 만든다 (RIP Steve Jobs)
                continue
            if stem not in avoid and not (folder / (stem + suffix)).exists():
                return stem
        raise FramingError("겹치지 않는 이름을 만들지 못했습니다. 패턴 방식을 쓰세요.")

    @classmethod
    def rename_single(cls, old: Path, wanted: str, lang: str = "kr") -> Path:
        """파일 하나만 안전하게. wanted 가 비면 도커식 무작위 이름을 쓴다."""
        if not old.exists():
            raise FramingError(f"{old.name} 이 없습니다 — 이미 옮겨졌거나 지워졌습니다.")
        suffix = old.suffix.lower()
        if wanted:
            stem = wanted.strip()
            if stem.lower().endswith(suffix) and suffix:
                stem = stem[: -len(suffix)]
            stem = cls._clean(stem)
        else:
            stem = cls.one_name(lang, old.parent, suffix)
        new = old.with_name(stem + suffix)
        RenameTransaction([(old, new)]).execute()
        return new

    @staticmethod
    def _clean(stem: str) -> str:
        """파일 이름에 못 들어가는 글자만 걷어낸다. 나머지는 쓰는 사람의 뜻이다."""
        bad = '\\/:*?"<>|'
        return "".join(ch for ch in stem if ch not in bad).strip() or "이름없음"


@dataclass
class RenameCandidate:
    source: Path
    stem: str
    extension: str

    @property
    def name(self) -> str:
        return self.stem + self.extension


class BatchNamePlan:
    """DarkNamer-style cumulative edits; computing a draft never renames files."""

    KINDS = {'replace', 'prefix', 'suffix', 'clear', 'trim_left', 'trim_right',
             'number', 'keep_digits', 'remove_digits', 'lower', 'upper', 'parent', 'extension'}

    def __init__(self, paths: Sequence[Path], rules: Sequence[dict]) -> None:
        if len(rules) > 100:
            raise FramingError('변경 규칙은 100개까지 쌓을 수 있습니다.')
        self.rows = [RenameCandidate(Path(p).absolute(), Path(p).stem, Path(p).suffix) for p in paths]
        for rule in rules:
            self._apply(rule)

    @staticmethod
    def integer(value, default: int, maximum: int = 1000000000) -> int:
        try:
            raw = default if value is None else value
            if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
                raise ValueError('not an integer')
            number = int(raw)
        except (TypeError, ValueError, OverflowError):
            raise FramingError('번호와 글자 수는 정수로 적어 주세요.')
        if not 0 <= number <= maximum:
            raise FramingError(f'숫자는 0~{maximum} 사이로 적어 주세요.')
        return number

    def _apply(self, rule: dict) -> None:
        if not isinstance(rule, dict) or rule.get('kind') not in self.KINDS:
            raise FramingError('알 수 없는 이름 변경 규칙입니다.')
        kind = rule['kind']
        text = str(rule.get('text', ''))
        if len(text) > 255:
            raise FramingError('추가할 글자가 너무 깁니다.')
        for index, row in enumerate(self.rows):
            if kind == 'replace':
                find = str(rule.get('find', ''))
                if not find:
                    raise FramingError('찾을 문자열을 적어 주세요.')
                row.stem = row.stem.replace(find, text)
            elif kind == 'prefix': row.stem = text + row.stem
            elif kind == 'suffix': row.stem += text
            elif kind == 'clear': row.stem = ''
            elif kind == 'trim_left': row.stem = row.stem[self.integer(rule.get('count'), 1, 255):]
            elif kind == 'trim_right':
                count = self.integer(rule.get('count'), 1, 255)
                if count: row.stem = row.stem[:-count]
            elif kind == 'number':
                start = self.integer(rule.get('start'), 1)
                step = self.integer(rule.get('step'), 1, 1000000)
                width = self.integer(rule.get('width'), 3, 12)
                number = str(start + index * step).zfill(width)
                position = rule.get('position', 'suffix')
                if position == 'prefix': row.stem = number + row.stem
                elif position == 'suffix': row.stem += number
                elif position == 'replace': row.stem = number
                else: raise FramingError('번호 위치를 확인하세요.')
            elif kind == 'keep_digits': row.stem = ''.join(c for c in row.stem if c.isdecimal())
            elif kind == 'remove_digits': row.stem = ''.join(c for c in row.stem if not c.isdecimal())
            elif kind == 'lower': row.stem = row.stem.lower()
            elif kind == 'upper': row.stem = row.stem.upper()
            elif kind == 'parent': row.stem = row.source.parent.name
            elif kind == 'extension': row.extension = ('.' + text.lstrip('.')) if text else ''

    @staticmethod
    def name_error(name: str) -> str:
        if not name or name in ('.', '..') or not name.strip('.'):
            return '이름이 비어 있습니다'
        if any(ord(c) < 32 or c in '\\/:*?"<>|' for c in name):
            return '이름에 사용할 수 없는 문자가 있습니다'
        if name.endswith((' ', '.')):
            return '이름 끝에 공백이나 점을 사용할 수 없습니다'
        if len(name.encode('utf-16-le')) > 510:
            return '파일 이름이 너무 깁니다'
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                    *(f'LPT{i}' for i in range(1, 10))}
        if name.split('.')[0].upper() in reserved:
            return '운영체제가 예약한 이름입니다'
        return ''

    def preview(self) -> list[dict]:
        sources = {row.source for row in self.rows}
        names: dict[Path, int] = {}
        for row in self.rows:
            if not self.name_error(row.name):
                dest = row.source.with_name(row.name)
                names[dest] = names.get(dest, 0) + 1
        result = []
        for row in self.rows:
            error = ('이름이 비어 있습니다' if not row.stem else self.name_error(row.name))
            dest = row.source.with_name(row.name) if not error else None
            if not row.source.is_file(): error = '원본 파일을 찾을 수 없습니다'
            elif dest is not None and names[dest] > 1: error = '다른 줄과 새 이름이 겹칩니다'
            elif dest is not None and os.path.lexists(dest) and dest not in sources:
                error = '같은 이름의 파일이 이미 있습니다'
            result.append({'path': str(row.source), 'original': row.source.name, 'name': row.name,
                           'destination': str(dest) if dest else '', 'error': error,
                           'changed': row.source.name != row.name,
                           'stamp': [str(value) for value in RenameHistory.stamp(row.source)] if row.source.is_file() else None})
        return result

    def mapping(self) -> list[tuple[Path, Path]]:
        rows = self.preview()
        failures = [f"{row['original']}: {row['error']}" for row in rows if row['error']]
        if failures:
            raise FramingError('\n'.join(failures[:20]))
        return [(Path(row['path']), Path(row['destination'])) for row in rows]


class RenameHistory:
    """A single committed batch, with identity checks before an explicit undo."""

    def __init__(self) -> None:
        self._undo = []
        self._lock = threading.Lock()

    @staticmethod
    def stamp(path: Path) -> tuple:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def apply(self, pairs: Sequence[tuple[Path, Path]], expected: Optional[dict] = None) -> list:
        with self._lock:
            for old, new in pairs:
                if expected is not None and tuple(map(str, expected.get(str(old)) or ())) != tuple(map(str, self.stamp(old))):
                    raise FramingError(f'{old.name} 파일이 미리보기 이후 바뀌었습니다. 다시 확인하세요.')
                error = BatchNamePlan.name_error(new.name)
                if error: raise FramingError(error)
            done = RenameTransaction(pairs).execute()
            self._undo = [(new, old, self.stamp(new)) for old, new in done]
            return [(str(old), str(new)) for old, new in done]

    def undo(self) -> list:
        with self._lock:
            if not self._undo: raise FramingError('되돌릴 이름 변경이 없습니다.')
            for current, original, stamp in self._undo:
                if not current.is_file() or self.stamp(current) != stamp:
                    raise FramingError(f'{current.name} 파일이 바뀌었습니다. 되돌리기를 중단했습니다.')
            done = RenameTransaction([(current, original) for current, original, _ in self._undo]).execute()
            self._undo = []
            return [(str(old), str(new)) for old, new in done]


NamePlan.DOCKER_LEFT = (
    "admiring", "adoring", "affectionate", "agitated", "amazing", "angry",
    "awesome", "beautiful", "blissful", "bold", "boring", "brave", "busy",
    "charming", "clever", "compassionate", "competent", "condescending",
    "confident", "cool", "cranky", "crazy", "dazzling", "determined",
    "distracted", "dreamy", "eager", "ecstatic", "elastic", "elated", "elegant",
    "eloquent", "epic", "exciting", "fervent", "festive", "flamboyant", "focused",
    "friendly", "frosty", "funny", "gallant", "gifted", "goofy", "gracious",
    "great", "happy", "hardcore", "heuristic", "hopeful", "hungry", "infallible",
    "inspiring", "intelligent", "interesting", "jolly", "jovial", "keen", "kind",
    "laughing", "loving", "lucid", "magical", "modest", "musing", "mystifying",
    "naughty", "nervous", "nice", "nifty", "nostalgic", "objective", "optimistic",
    "peaceful", "pedantic", "pensive", "practical", "priceless", "quirky",
    "quizzical", "recursing", "relaxed", "reverent", "romantic", "sad", "serene",
    "sharp", "silly", "sleepy", "stoic", "strange", "stupefied", "suspicious",
    "sweet", "tender", "thirsty", "trusting", "unruffled", "upbeat", "vibrant",
    "vigilant", "vigorous", "wizardly", "wonderful", "xenodochial", "youthful",
    "zealous", "zen")
NamePlan.DOCKER_RIGHT = (
    "agnesi", "albattani", "allen", "almeida", "antonelli", "archimedes",
    "ardinghelli", "aryabhata", "austin", "babbage", "banach", "banzai",
    "bardeen", "bartik", "bassi", "beaver", "bell", "benz", "bhabha", "bhaskara",
    "black", "blackburn", "blackwell", "bohr", "booth", "borg", "bose", "bouman",
    "boyd", "brahmagupta", "brattain", "brown", "buck", "burnell", "cannon",
    "carson", "cartwright", "carver", "cerf", "chandrasekhar", "chaplygin",
    "chatelet", "chatterjee", "chaum", "chebyshev", "clarke", "cohen", "colden",
    "cori", "cray", "curie", "curran", "darwin", "davinci", "dewdney", "dhawan",
    "diffie", "dijkstra", "dirac", "driscoll", "dubinsky", "easley", "edison",
    "einstein", "elbakyan", "elgamal", "elion", "ellis", "engelbart", "euclid",
    "euler", "faraday", "feistel", "fermat", "fermi", "feynman", "franklin",
    "gagarin", "galileo", "galois", "ganguly", "gates", "gauss", "germain",
    "goldberg", "goldstine", "goldwasser", "golick", "goodall", "gould",
    "greider", "grothendieck", "haibt", "hamilton", "haslett", "hawking",
    "heisenberg", "hellman", "hermann", "herschel", "hertz", "heyrovsky",
    "hodgkin", "hofstadter", "hoover", "hopper", "hugle", "hypatia", "ishizaka",
    "jackson", "jang", "jemison", "jennings", "jepsen", "johnson", "joliot",
    "jones", "kalam", "kapitsa", "kare", "keldysh", "keller", "kepler", "khayyam",
    "khorana", "kilby", "kirch", "knuth", "kowalevski", "lalande", "lamarr",
    "lamport", "leakey", "leavitt", "lederberg", "lehmann", "lewin", "lichterman",
    "liskov", "lovelace", "lumiere", "mahavira", "margulis", "matsumoto",
    "maxwell", "mayer", "mccarthy", "mcclintock", "mclaren", "mclean", "mcnulty",
    "meitner", "mendel", "mendeleev", "meninsky", "merkle", "mestorf",
    "mirzakhani", "montalcini", "moore", "morse", "moser", "murdock", "napier",
    "nash", "neumann", "newton", "nightingale", "nobel", "noether", "northcutt",
    "noyce", "panini", "pare", "pascal", "pasteur", "payne", "perlman", "pike",
    "poincare", "poitras", "proskuriakova", "ptolemy", "raman", "ramanujan",
    "rhodes", "ride", "ritchie", "robinson", "roentgen", "rosalind", "rubin",
    "saha", "sammet", "sanderson", "satoshi", "shamir", "shannon", "shaw",
    "shirley", "shockley", "shtern", "sinoussi", "snyder", "solomon", "spence",
    "stonebraker", "sutherland", "swanson", "swartz", "swirles", "taussig",
    "tesla", "tharp", "thompson", "torvalds", "tu", "turing", "varahamihira",
    "vaughan", "villani", "visvesvaraya", "volhard", "wescoff", "wilbur", "wiles",
    "williams", "williamson", "wilson", "wing", "wozniak", "wright", "wu",
    "yalow", "yonath", "zhukovsky")




class DeliverySlate:
    """
    납품 파일 이름 — `샷코드_테스크_v###`.

        A_0002C007_ai_v001.mov

    이름을 손으로 적으면 반드시 틀립니다. 오타 하나, 버전 하나 겹치면 상대
    쪽에서 어느 게 최신인지 못 고르고, 그걸 알아채는 건 보통 다음 날입니다.
    그래서 셋을 규칙으로 못 박습니다.

      샷코드   원본 파일 이름에서 그대로 물려받습니다 (`A_0002C007_org_v001`
               → `A_0002C007`). 다른 샷 이름이 섞여 들어갈 자리가 없습니다.
      테스크   짧은 말 하나. AFX 결과물이면 보통 `ai`.
      버전     같은 이름이 이미 있으면 다음 번호로 올립니다. 덮어쓰지 않습니다.
    """

    #  A_0002C007_org_v001  ·  SH010_comp_v012  — 뒤에서부터 읽는다
    READ = re.compile(r'^(?P<shot>.+?)_(?P<task>[^_]+)_v(?P<version>\d+)$', re.IGNORECASE)
    SAFE = re.compile(r'[^0-9A-Za-z가-힣._-]+')
    DIGITS = 3
    DEFAULT_TASK = 'ai'

    def __init__(self, shot: str, task: str = DEFAULT_TASK, version: int = 1) -> None:
        self.shot = self._clean(shot)
        self.task = self._clean(task) or self.DEFAULT_TASK
        self.version = max(1, int(version or 1))
        if not self.shot:
            raise FramingError('샷코드가 없습니다 — 원본 이름에서 찾지 못했으니 직접 넣어 주세요.')

    @classmethod
    def _clean(cls, said: str) -> str:
        return cls.SAFE.sub('', str(said or '').strip()).strip('._-')

    @classmethod
    def read(cls, stem: str) -> Optional['DeliverySlate']:
        """이미 규약대로 생긴 이름이면 셋으로 갈라 읽는다."""
        found = cls.READ.match(str(stem or '').strip())
        if not found:
            return None
        return cls(found['shot'], found['task'], int(found['version']))

    @classmethod
    def shot_of(cls, stem: str) -> str:
        """
        원본 이름에서 샷코드만 뽑는다.

        규약대로 생겼으면 그 앞부분, 아니면 이름 전체를 샷코드로 본다 —
        `A_0002C007.mov` 하나만 받았을 때도 돌아가야 한다.
        """
        said = cls.read(stem)
        return said.shot if said else cls._clean(stem)

    @classmethod
    def decide(cls, source_stem: str, shot: str = '', task: str = '',
               version: int = 0) -> 'DeliverySlate':
        """말해 준 것을 먼저 쓰고, 안 말한 것은 원본 이름에서 물려받는다."""
        return cls(shot or cls.shot_of(source_stem), task or cls.DEFAULT_TASK,
                   version or 1)

    def name(self, ext: str = '.mov') -> str:
        return f'{self.shot}_{self.task}_v{self.version:0{self.DIGITS}d}{ext}'

    def bump(self) -> 'DeliverySlate':
        return DeliverySlate(self.shot, self.task, self.version + 1)

    def free_in(self, folder: Path, ext: str = '.mov', *, overwrite: bool = False) -> Path:
        """
        아직 안 쓰인 버전을 찾아 그 경로를 돌려준다.

        버전을 직접 말했는데 그 번호가 이미 있으면 조용히 올립니다 —
        납품본을 덮어쓰는 건 되돌릴 수 없는 사고입니다.
        """
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        slate = self
        if overwrite:
            return folder / slate.name(ext)
        for _ in range(999):
            spot = folder / slate.name(ext)
            if not spot.exists():
                return spot
            slate = slate.bump()
        raise FramingError('버전 번호가 999를 넘었습니다.')


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

import argparse
import base64
import datetime
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence



import atexit
import importlib
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import ModuleType
from typing import Optional, Sequence

BUILD = "1.0"          # 어느 판을 쓰고 있는지 화면에서 보이게
SELF = Path(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


class Relauncher:
    """이 파일을 다시 띄우는 방법들. 첫 줄이 배치라서 언제나 -x 가 필요하다."""

    DETACHED = "FRAMING_DETACHED"

    @staticmethod
    def restart() -> None:
        """설치한 것을 반영해 처음부터 다시."""
        os.execv(sys.executable, [sys.executable, "-x", str(SELF), *sys.argv[1:]])

    @classmethod
    def detach_console(cls) -> bool:
        """
        윈도우에서 검은 콘솔을 떼어낸다. 떼어냈으면 True — 부른 쪽은 그냥 끝내면 된다.
        준비가 다 끝난 뒤에 부르는 것이 중요하다. 설치 과정은 콘솔에 보여야 하니까.
        """
        if not sys.platform.startswith("win"):
            return False
        if os.environ.get(cls.DETACHED) == "1":
            return False
        current = Path(sys.executable)
        if current.name.lower().startswith("pythonw"):
            return False
        quiet = current.with_name("pythonw.exe")
        if not quiet.is_file():
            return False
        try:
            subprocess.Popen(
                [str(quiet), "-x", str(SELF), *sys.argv[1:]],
                env=dict(os.environ, **{cls.DETACHED: "1"}),
                close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            return True
        except Exception:
            return False


# ══════════════════════════════════════════════════ 1~4. 엔진

MIN_PYTHON = (3, 8)
OPENCV_PACKAGES = ("opencv-python-headless", "opencv-python")
BOOTSTRAP_FLAG = "FRAMING_BOOTSTRAPPED"

HERE = Path(__file__).resolve().parent
# 앱에 묶어 둔 도구 자리. 실행 껍데기(main.py · Mac 앱)가 환경변수로 알려 주면 그쪽을 본다
FFMPEG_HOME = Path(os.environ.get("FRAMING_FFMPEG_HOME") or HERE / "_ffmpeg")
RIFE_HOME = Path(os.environ.get("FRAMING_RIFE_HOME") or HERE / "_rife")

VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2v", ".ts", ".m2ts", ".mts", ".mxf", ".ogv", ".3gp",
})
IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".dpx", ".bmp", ".tga",
    ".webp",
})

BAR = "─" * 62


# ══════════════════════════════════════════════════ 자동 설치

class DependencyBootstrap:
    """필요한 파이썬 패키지가 없으면 pip 로 직접 설치하고 임포트까지 마친다."""

    def __init__(self, module_name: str, packages: Sequence[str],
                 announce: bool = True) -> None:
        self._module_name = module_name
        self._packages = tuple(packages)
        self._announce = announce

    def ensure(self) -> ModuleType:
        module = self._import()
        if module is not None:
            return module

        if getattr(sys, "frozen", False):
            raise SystemExit(f"앱 구성요소 {self._module_name}를 읽지 못했습니다. ZIP을 다시 풀어 주세요.")

        if self._announce:
            print(f"[준비] '{self._packages[0]}' 가 없어 자동으로 설치합니다. 최초 1회만 걸립니다.")
        self._ensure_pip()

        for package in self._packages:
            if not self._install(package):
                continue
            self._refresh_search_path()
            module = self._import()
            if module is not None:
                if self._announce:
                    print("[준비] 설치 완료.\n")
                return module
            return self._restart_once()

        raise SystemExit(self._failure_message())

    def try_ensure(self) -> bool:
        """실패해도 프로그램을 끝내지 않는다. 대비책이 있는 쪽에서 쓴다."""
        try:
            self.ensure()
            return True
        except SystemExit:
            return False

    def _import(self) -> Optional[ModuleType]:
        importlib.invalidate_caches()
        try:
            return importlib.import_module(self._module_name)
        except ImportError:
            return None

    @staticmethod
    def _ensure_pip() -> None:
        try:
            importlib.import_module("pip")
        except ImportError:
            try:
                import ensurepip
                ensurepip.bootstrap()
            except Exception:
                pass

    @staticmethod
    def _install(package: str) -> bool:
        # Respect externally managed environments; never force past their safeguards.
        variants = ([], ["--user"])
        for extra in variants:
            print(f"       pip install {package} {' '.join(extra)}".rstrip())
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "--disable-pip-version-check", package, *extra])
            except OSError:
                return False
            if completed.returncode == 0:
                return True
        return False

    @staticmethod
    def _refresh_search_path() -> None:
        try:
            user_site = site.getusersitepackages()
        except Exception:
            return
        if isinstance(user_site, str) and user_site not in sys.path:
            sys.path.append(user_site)

    @staticmethod
    def relaunch_command() -> list[str]:
        """
        나를 다시 여는 명령.

        한 파일짜리 실행본은 첫 줄이 파이썬이 아니다.
        .bat 은 배치 명령이라 python -x 로 건너뛰어야 하고,
        .command 는 첫 줄이 #!/bin/sh 주석이라 그냥 열어도 된다.
        이걸 구분하지 않으면 설치 직후 다시 시작하는 순간 죽는다.
        """
        if getattr(sys, "frozen", False):
            return [sys.executable]
        me = Path(__file__).resolve()
        skip = ["-x"] if me.suffix.lower() == ".bat" else []
        return [sys.executable, *skip, str(me)]

    def _restart_once(self) -> ModuleType:
        if os.environ.get(BOOTSTRAP_FLAG) != "1":
            os.environ[BOOTSTRAP_FLAG] = "1"
            print("[준비] 설치 반영을 위해 다시 시작합니다.\n")
            command = self.relaunch_command()
            os.execv(command[0], [*command, *sys.argv[1:]])
        raise SystemExit(self._failure_message())

    def _failure_message(self) -> str:
        return (
            f"\n'{self._packages[0]}' 설치에 실패했습니다.\n"
            "인터넷 연결을 확인하고 아래 명령을 직접 실행해 주세요.\n"
            f"  {sys.executable} -m pip install {self._packages[0]}\n"
        )


def require_python() -> None:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"파이썬 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상이 필요합니다. "
            f"현재 {sys.version.split()[0]}\n"
            "https://www.python.org/downloads/ 에서 설치해 주세요.\n")


require_python()

os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

cv2 = DependencyBootstrap("cv2", OPENCV_PACKAGES).ensure()
np = DependencyBootstrap("numpy", ("numpy",)).ensure()


# ══════════════════════════════════════════════════ 공용 유틸

class Console:
    """Console formatting with optional context-local event delivery."""
    @staticmethod
    def _write(kind, text, formatted):
        if not EngineBridge.emit('log', {'kind': kind, 'text': str(text)}):
            try:
                print(formatted)
            except UnicodeEncodeError:
                encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
                print(str(formatted).encode(encoding, errors='backslashreplace').decode(encoding))

    @staticmethod
    def banner(text): Console._write('head', text, f"\n{BAR}\n  {text}\n{BAR}")
    @staticmethod
    def section(text): Console._write('head', text, f"\n  {text}")
    @staticmethod
    def info(text): Console._write('info', text, f"     {text}")
    @staticmethod
    def step(text): Console._write('info', text, f"     · {text}")
    @staticmethod
    def warn(text): Console._write('warn', text, f"     [!] {text}")
    @staticmethod
    def ok(text): Console._write('ok', text, f"     ✓ {text}")
    @staticmethod
    def fail(text): Console._write('err', text, f"     ✗ {text}")


def is_interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def normalize_path(raw: str | Path) -> Path:
    """따옴표와 터미널 드래그로 붙는 역슬래시 이스케이프를 걷어낸다."""
    text = str(raw).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    path = Path(text).expanduser()
    if os.name != "nt" and not path.exists() and "\\" in text:
        unescaped = Path(re.sub(r"\\(.)", r"\1", text)).expanduser()
        if unescaped.exists():
            return unescaped
    return path


def display_pad(text: str, width: int) -> str:
    """한글은 터미널에서 두 칸을 먹는다. 그걸 감안해 자리를 맞춘다."""
    import unicodedata
    used = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)
    return text + " " * max(1, width - used)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def hms(seconds: float) -> str:
    if not seconds:
        return "?"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def unique_path(path: Path) -> Path:
    """이미 있으면 _1, _2 를 붙여 새 이름을 만든다."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    index = 1
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


class ImageIO:
    """한글 경로에서도 안전하게 읽고 쓴다 (OpenCV 는 비ASCII 경로에 약하다)."""

    @staticmethod
    def read(path: Path, flags: int = cv2.IMREAD_COLOR) -> Optional["np.ndarray"]:
        try:
            buffer = np.frombuffer(path.read_bytes(), dtype=np.uint8)
        except OSError:
            return None
        if not buffer.size:
            return None
        try:
            return cv2.imdecode(buffer, flags)
        except cv2.error:
            return None

    @staticmethod
    def write(path: Path, image: "np.ndarray", params: Sequence[int] = ()) -> bool:
        ok, buffer = cv2.imencode(path.suffix, image, list(params))
        if not ok:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.tobytes())
        return True


class AsciiPathAlias:
    """Windows OpenCV 가 한글 경로를 못 여는 경우를 위한 임시 ASCII 경로."""

    def __init__(self, source: Path) -> None:
        self._source = source
        self._alias: Optional[Path] = None

    def create(self) -> Path:
        name = f"_tmp_{uuid.uuid4().hex[:12]}{self._source.suffix}"
        for candidate in (self._source.parent / name, Path(tempfile.gettempdir()) / name):
            try:
                os.link(self._source, candidate)
                self._alias = candidate
                return candidate
            except OSError:
                continue
        candidate = Path(tempfile.gettempdir()) / name
        shutil.copy2(self._source, candidate)
        self._alias = candidate
        return candidate

    def cleanup(self) -> None:
        if self._alias is not None:
            try:
                self._alias.unlink()
            except OSError:
                pass
            self._alias = None


class Scratch:
    """
    잠깐 보고 버릴 결과를 담는 자리.

    대조는 결과물이 아니라 확인하는 행위다. 볼 때마다 원본 폴더에 파일이 쌓이면
    정작 필요한 파일이 묻힌다. 그래서 임시 자리에 만들고, 프로그램이 닫힐 때 지운다.
    """

    _folders: list[Path] = []

    @classmethod
    def folder(cls, tag: str) -> Path:
        made = Path(tempfile.mkdtemp(prefix=f"framing_{tag}_"))
        cls._folders.append(made)
        return made

    @classmethod
    def clear(cls) -> None:
        while cls._folders:
            shutil.rmtree(cls._folders.pop(), ignore_errors=True)

    @classmethod
    def holding(cls) -> int:
        return len(cls._folders)


atexit.register(Scratch.clear)


def open_externally(path: Path) -> bool:
    """기본 프로그램으로 연다. 안 되면 조용히 False — 경로는 어차피 로그에 남는다."""
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))                    # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], **quiet)
        else:
            subprocess.Popen(["xdg-open", str(path)], **quiet)
        return True
    except Exception:
        return False


def link_or_copy(source: Path, destination: Path) -> Path:
    """
    같은 디스크면 하드링크로 즉시 끝난다. 28MB 짜리를 두 번 복사하고 앉아 있을 이유가 없다.
    임시 폴더가 다른 드라이브에 있으면 링크가 안 걸리니 그때만 복사한다.

    같은 자리에 두 번 만들 수 있어야 한다. 지난번에 걸어 둔 링크가 남아 있으면
    그건 원본과 같은 파일이라, 그대로 복사하려 들면 "같은 파일입니다" 로 멈춘다.
    (--keep 으로 같은 폴더에 두 번 대조하면 그래서 터졌다.)
    """
    if destination.exists():
        try:
            if destination.samefile(source):
                return destination            # 이미 같은 파일 — 할 일이 없다
        except OSError:
            pass
        try:
            destination.unlink()
        except OSError:
            pass
    try:
        os.link(source, destination)
        return destination
    except OSError:
        shutil.copyfile(source, destination)
        return destination




# ══════════════════════════════════════════════════ 프레임 소스

SEEK_BACKOFF = 48
TAIL_SCAN = 90



class Progress:
    @classmethod
    def tick(cls, done: int, total: int, every: int = 24) -> None:
        if EngineBridge.emit('progress', {'done': done, 'total': total}): return
        if total and (done % every == 0 or done == total):
            print(f"       {done}/{total}", end="\r" if done != total else "\n", flush=True)

    @classmethod
    def span(cls, fraction: float) -> None:
        """0~1 사이 진행률. ffmpeg 처럼 프레임 수를 모르는 작업이 쓴다."""
        fraction = max(0.0, min(1.0, fraction))
        if EngineBridge.emit('progress', {'done': fraction, 'total': 1}): return
        pct = int(fraction * 100)
        print(f"       {pct}%", end="\r" if pct < 100 else "\n", flush=True)



class FrameNumber:
    """
    프레임 번호를 사람에게 보여 줄 때 거치는 단 하나의 자리.

    **사람이 읽거나 적는 번호는 전부 1부터다.** 안에서 세는 번호(리스트 자리)는
    0부터이고, 그 둘이 만나는 곳이 여기다.

    왜 못 박았는가 — 예전에는 도구마다 달랐다. 뽑기와 검사와 컷 분할은 0부터,
    리타임 메모와 되돌리기는 1부터였다. 그래서 "33번 34번이 겹친다" 는 말을
    듣고도 그게 어느 쪽 번호인지 되물어야 했고, 한 칸 어긋난 채로 지나간 적도
    있다. 누크도 1부터 세고, 납품 규약의 240프레임도 1~240 이다. 사람 쪽에
    맞춘다.

    안에 담아 두는 기계용 기록(늘림메모의 origins 같은 것)은 0부터 그대로다 —
    되돌리기가 그걸로 고르기 때문이다. 대신 그런 기록은 자기 base 를 적어 둔다.
    """

    BASE = 1

    @classmethod
    def show(cls, index: int) -> int:
        """안에서 세던 번호(0부터) → 사람이 보는 번호(1부터)."""
        return int(index) + cls.BASE

    @classmethod
    def read(cls, said: int) -> int:
        """사람이 적은 번호(1부터) → 안에서 세는 번호(0부터)."""
        return int(said) - cls.BASE

    @classmethod
    def label(cls) -> str:
        return "1부터"

    @classmethod
    def listing(cls, indexes) -> list[int]:
        return [cls.show(i) for i in indexes]





import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence


class VideoSource:
    """영상을 열고 원하는 인덱스의 프레임을 안전하게 꺼내준다 (임의 접근용)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._capture: Optional["cv2.VideoCapture"] = None
        self._alias = AsciiPathAlias(path)
        self._active_path: Optional[Path] = None
        self._frame_count: Optional[int] = None

    def __enter__(self) -> "VideoSource":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open(self) -> None:
        active = self._path
        capture = cv2.VideoCapture(str(active))
        if not capture.isOpened():
            capture.release()
            active = self._alias.create()
            capture = cv2.VideoCapture(str(active))
        if not capture.isOpened():
            capture.release()
            self._alias.cleanup()
            raise FramingError(f"영상을 열 수 없습니다: {self._path.name}")
        self._capture = capture
        self._active_path = active

    def reopen(self) -> "cv2.VideoCapture":
        if self._capture is not None:
            self._capture.release()
        capture = cv2.VideoCapture(str(self._active_path or self._path))
        if not capture.isOpened():
            capture.release()
            raise FramingError(f"영상을 다시 열 수 없습니다: {self._path.name}")
        self._capture = capture
        return capture

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._alias.cleanup()

    def _require(self) -> "cv2.VideoCapture":
        if self._capture is None:
            raise FramingError("영상이 열려 있지 않습니다.")
        return self._capture

    @property
    def fps(self) -> float:
        return float(self._require().get(cv2.CAP_PROP_FPS) or 0.0)

    @property
    def resolution(self) -> tuple[int, int]:
        capture = self._require()
        return (int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0))

    @property
    def frame_count(self) -> int:
        if self._frame_count is None:
            reported = int(self._require().get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            self._frame_count = reported if reported > 0 else self._count_by_scan()
        return self._frame_count

    def _count_by_scan(self) -> int:
        capture = self._require()
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        total = 0
        while capture.grab():
            total += 1
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        return total

    def read_at(self, target: int) -> tuple[Optional["np.ndarray"], int]:
        capture = self._require()
        target = max(0, target)
        best: tuple[Optional["np.ndarray"], int] = (None, -1)
        for backoff in (SEEK_BACKOFF, SEEK_BACKOFF * 20, None):
            if backoff is None:
                capture = self.reopen()
                position = 0
            else:
                anchor = max(0, target - backoff)
                capture.set(cv2.CAP_PROP_POS_FRAMES, anchor)
                position = int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0)
                if position > target and anchor > 0:
                    continue
            frame, index = self._decode_forward(capture, target, position)
            if frame is not None:
                if index >= target:
                    return frame, index
                best = (frame, index)
        return best

    @staticmethod
    def _decode_forward(capture: "cv2.VideoCapture", target: int,
                        position: int) -> tuple[Optional["np.ndarray"], int]:
        frame: Optional["np.ndarray"] = None
        index = -1
        for _ in range(max(1, target - position) + 16):
            ok, decoded = capture.read()
            if not ok:
                break
            frame = decoded
            index = max(0, int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0) - 1)
            if index >= target:
                break
        return frame, index

    def read_last(self) -> tuple[Optional["np.ndarray"], int]:
        capture = self._require()
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, self.frame_count - TAIL_SCAN))
        frame, index = self._drain(capture)
        if frame is not None:
            return frame, index
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        return self._drain(capture)

    @staticmethod
    def _drain(capture: "cv2.VideoCapture") -> tuple[Optional["np.ndarray"], int]:
        frame: Optional["np.ndarray"] = None
        index = -1
        while True:
            ok, decoded = capture.read()
            if not ok:
                break
            frame = decoded
            index = max(0, int(capture.get(cv2.CAP_PROP_POS_FRAMES) or 0) - 1)
        return frame, index


class FrameFeed(ABC):
    """프레임을 순서대로 흘려주는 공급원. 영상이든 시퀀스 폴더든 같은 얼굴."""

    @property
    @abstractmethod
    def count(self) -> int: ...

    @property
    def fps(self) -> float:
        return 0.0

    @abstractmethod
    def frames(self, reverse: bool = False) -> Iterator["np.ndarray"]: ...

    def close(self) -> None:
        pass

    def __enter__(self) -> "FrameFeed":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ImageSequenceFeed(FrameFeed):
    def __init__(self, paths: Sequence[Path], preserve: bool = False) -> None:
        self._paths = list(paths)
        self._preserve = preserve

    @property
    def count(self) -> int:
        return len(self._paths)

    @property
    def paths(self) -> list[Path]:
        return list(self._paths)

    def frames(self, reverse: bool = False) -> Iterator["np.ndarray"]:
        signature = None
        for path in (reversed(self._paths) if reverse else self._paths):
            image = ImageIO.read(path, cv2.IMREAD_UNCHANGED if self._preserve else cv2.IMREAD_COLOR)
            if image is None:
                raise FramingError(f"이미지를 읽지 못했습니다: {path.name}")
            if self._preserve:
                current = (image.shape, image.dtype)
                if signature is not None and current != signature:
                    raise FramingError(f'시퀀스의 크기·채널·비트 깊이를 맞춰 주세요: {path.name}')
                signature = current
            yield image


class VideoFrameFeed(FrameFeed):
    """영상을 앞에서부터 순차 디코딩한다. 역순은 인덱스를 뒤집어 저장해 해결한다."""

    def __init__(self, path: Path) -> None:
        self._source = VideoSource(path)
        self._source.open()

    @property
    def count(self) -> int:
        return self._source.frame_count

    @property
    def fps(self) -> float:
        return self._source.fps or 24.0

    def frames(self, reverse: bool = False) -> Iterator["np.ndarray"]:
        capture = self._source.reopen()
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            yield frame

    def close(self) -> None:
        self._source.close()


def collect_images(folder: Path) -> list[Path]:
    found = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    return sorted(found, key=lambda p: [int(s) if s.isdigit() else s.casefold()
                                       for s in re.split(r'(\d+)', p.name)])


def open_feed(target: Path, preserve: bool = False) -> FrameFeed:
    if target.is_dir():
        images = collect_images(target)
        if not images:
            raise FramingError(f"이미지가 없습니다: {target.name}")
        return ImageSequenceFeed(images, preserve=preserve)
    return VideoFrameFeed(target)


# ══════════════════════════════════════════════════ ffmpeg

@dataclass
class ProbeInfo:
    vcodec: str = ""
    acodec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    video_duration: float = 0.0
    alpha: bool = False
    pix_fmt: str = ""
    color_range: str = ""          # 'tv' | 'pc' | '' — 영상이 스스로 적어 둔 밝기 범위
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""          # 매트릭스. bt709 / bt470bg / smpte170m …
    reported_frames: int = 0       # Prefer the video stream's count over duration estimates.

    @property
    def color_metadata(self) -> dict:
        """Declared stream tags, never guessed from dimensions or codec."""
        def clean(value):
            return value if value and value not in ('unknown', 'unspecified', 'reserved', 'N/A') else ''
        tags = {key: clean(getattr(self, 'color_' + key))
                for key in ('space', 'primaries', 'transfer', 'range')}
        labels = {'bt709': 'Rec.709', 'bt2020': 'Rec.2020', 'smpte432': 'Display P3',
                  'smpte2084': 'PQ (ST 2084)', 'arib-std-b67': 'HLG',
                  'iec61966-2-1': 'sRGB', 'tv': 'Limited', 'pc': 'Full'}
        shown = {key: labels.get(value, value) if value else '미지정' for key, value in tags.items()}
        return {**tags, 'pixelFormat': self.pix_fmt, 'declared': any(tags.values()),
                'summary': f"색역 {shown['primaries']} · 전달 {shown['transfer']} · 매트릭스 {shown['space']} · 범위 {shown['range']}",
                'labels': shown}

    @property
    def frames(self) -> int:
        if self.reported_frames > 0:
            return self.reported_frames
        duration = self.video_duration or self.duration
        if not math.isfinite(duration) or not math.isfinite(self.fps) or duration <= 0 or self.fps <= 0:
            return 0
        return max(1, int(round(duration * self.fps)))


class FFmpegTool:
    """ffmpeg / ffprobe 를 찾고, 없으면 (윈도우에 한해) 내려받아 옆에 둔다."""

    DOWNLOADS = (
        ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
         "gyan.dev (ffmpeg.org 공식 링크)"),
        ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
         "ffmpeg-master-latest-win64-gpl.zip", "github.com/BtbN/FFmpeg-Builds"),
    )

    def __init__(self, allow_download: bool = True) -> None:
        self._allow_download = allow_download
        self._cache: dict[str, Optional[str]] = {}
        self._active: Optional[subprocess.Popen] = None   # 중단 버튼이 이걸 붙잡는다

    def abort(self) -> bool:
        """돌고 있는 ffmpeg 을 끊는다. 끊을 게 있었으면 True."""
        process = self._active
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                return True
            except Exception:
                return False
        return False

    # --- 탐색 -------------------------------------------------------------

    def find(self, name: str) -> Optional[str]:
        if name in self._cache:
            return self._cache[name]
        found = self._search(name)
        self._cache[name] = found
        return found

    @staticmethod
    def _search(name: str) -> Optional[str]:
        for ext in (".exe", ""):
            candidate = FFMPEG_HOME / (name + ext)
            if candidate.is_file():
                return str(candidate)
        found = shutil.which(name)
        if found:
            return found
        guesses = [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
                   r"C:\ProgramData\chocolatey\bin",
                   os.path.expanduser(r"~\scoop\shims")]
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            guesses += glob.glob(os.path.join(local, "Microsoft", "WinGet",
                                              "Packages", "*FFmpeg*", "*", "bin"))
            guesses += glob.glob(os.path.join(local, "Microsoft", "WinGet",
                                              "Packages", "*FFmpeg*", "bin"))
        for folder in guesses:
            for ext in (".exe", ""):
                candidate = Path(folder) / (name + ext)
                if candidate.is_file():
                    return str(candidate)
        return None

    def ensure(self) -> str:
        """ffmpeg 경로를 돌려주거나, 없으면 설명과 함께 예외를 던진다."""
        found = self.find("ffmpeg")
        if found:
            return found
        if self._allow_download:
            found = self._install()
            self._cache["ffmpeg"] = found
            self._cache.pop("ffprobe", None)
        if found:
            return found
        raise FramingError(
            "ffmpeg 이 없습니다. 이 작업은 ffmpeg 없이는 못 합니다.\n"
            "         윈도우 : winget install Gyan.FFmpeg\n"
            "         macOS  : brew install ffmpeg\n"
            "         우분투 : sudo apt install ffmpeg")

    def ensure_probe(self) -> str:
        """
        ffprobe 경로.

        예전에는 부르는 쪽이 ffmpeg 경로에 ``.replace('ffmpeg', 'ffprobe')`` 를
        걸었다. 리눅스의 ``/usr/bin/ffmpeg`` 에서는 맞아떨어졌지만 윈도우의
        ``...\\.framing\\ffmpeg\\bin\\ffmpeg.exe`` 에서는 **폴더 이름까지 같이**
        바뀌어 없는 경로가 됐다 — WinError 2. str.replace 는 몇 번째인지
        가리지 않는다.

        그래서 찾기는 찾는 쪽(find)에만 둔다. 이름을 손으로 만들지 않는다.
        """
        self.ensure()                       # ffmpeg 이 없으면 여기서 설명과 함께 멈춘다
        found = self.find("ffprobe")
        if found:
            return found
        raise FramingError(
            "ffprobe 를 찾지 못했습니다. ffmpeg 은 있는데 ffprobe 만 없는 상태입니다 — "
            "대개 ffmpeg 을 폴더째 받지 않고 실행 파일 하나만 옮겼을 때 이렇게 됩니다.")

    @property
    def platform_hint(self) -> str:
        return {"darwin": "brew install ffmpeg",
                "win32": "winget install Gyan.FFmpeg"}.get(
                    sys.platform, "sudo apt install ffmpeg")

    @property
    def ffprobe(self) -> Optional[str]:
        return self.find("ffprobe")

    # --- 내려받기 ---------------------------------------------------------

    def _install(self) -> Optional[str]:
        if sys.platform == "darwin":
            return self._install_mac()
        if not sys.platform.startswith("win"):
            Console.warn("ffmpeg 이 없습니다.")
            Console.info("우분투 · 데비안 : sudo apt install ffmpeg")
            Console.info("페도라        : sudo dnf install ffmpeg")
            return None

        Console.warn("ffmpeg 이 없어 내려받습니다. 약 80MB, 최초 1회.")
        Console.info(f"설치 위치: {FFMPEG_HOME}  (이 폴더만 지우면 원상복구)")
        # Concurrent launches must not share or delete each other's download.
        archive = Path(tempfile.gettempdir()) / f"framing_ffmpeg_{uuid.uuid4().hex}.zip"
        for url, who in self.DOWNLOADS:
            Console.step(f"{who} 에서 받는 중")
            try:
                size = self._fetch(url, archive)
                if size < 1_000_000:
                    raise FramingError(f"{human(size)} 밖에 못 받았습니다")
                self._unpack(archive, FFMPEG_HOME)
                target = FFMPEG_HOME / "ffmpeg.exe"
                if not target.is_file():
                    raise FramingError("압축 안에 ffmpeg.exe 가 없습니다")
                Console.ok(f"준비됨: {target}")
                return str(target)
            except Exception as exc:
                Console.fail(f"실패: {exc}")
            finally:
                if archive.exists():
                    try:
                        archive.unlink()
                    except OSError:
                        pass
        Console.warn("두 곳 모두 실패했습니다. 회사망·프록시 차단일 수 있습니다.")
        Console.info("winget install Gyan.FFmpeg 를 쓰거나,")
        Console.info(f"직접 받은 ffmpeg.exe / ffprobe.exe 를 {FFMPEG_HOME} 에 넣어 주세요.")
        return None

    def _install_mac(self) -> Optional[str]:
        """
        맥에서는 Homebrew 가 사실상 표준이라 그쪽에 맡긴다.

        윈도우처럼 직접 내려받지 않는 이유가 있다. 애플 실리콘과 인텔은 다른 바이너리를
        써야 하고, 내려받은 실행 파일에는 격리 딱지가 붙어 macOS 가 실행을 막는다.
        그걸 코드로 우회하는 건 사용자 몰래 보안 설정을 건드리는 일이라 하지 않는다.
        """
        Console.warn("ffmpeg 이 없습니다. 이 작업에는 ffmpeg 이 필요합니다.")
        if shutil.which("brew"):
            Console.info("Homebrew 가 있습니다. 설치를 시작합니다 (몇 분 걸립니다)")
            subprocess.run(["brew", "install", "ffmpeg"], check=False)
            found = self._search("ffmpeg")
            if found:
                Console.ok(f"준비됨: {found}")
                return found
            Console.fail("설치가 끝났는데도 못 찾았습니다. 터미널을 새로 열고 다시 실행해 보세요.")
            return None

        Console.info("Homebrew 가 없습니다. 터미널에 아래 두 줄을 차례로 붙여넣으세요.")
        Console.info("")
        Console.info('  /bin/bash -c "$(curl -fsSL '
                     'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
        Console.info("  brew install ffmpeg")
        Console.info("")
        Console.info("끝나면 이 창을 닫고 Framing 을 다시 열면 됩니다.")
        return None

    @staticmethod
    def _fetch(url: str, destination: Path) -> int:
        import urllib.request
        request = urllib.request.Request(url, headers={"User-Agent": "framing"})
        with urllib.request.urlopen(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            got, last = 0, 0.0
            with open(destination, "wb") as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if now - last > 0.25:
                        last = now
                        if total:
                            pct = got * 100.0 / total
                            bars = "#" * int(pct / 2.5)
                            line = f"       [{bars:<40}] {pct:5.1f}%  {human(got)} / {human(total)}"
                        else:
                            line = f"       {human(got)}"
                        sys.stdout.write("\r" + line.ljust(78))
                        sys.stdout.flush()
        sys.stdout.write("\r" + f"       [{'#' * 40}] 100.0%  {human(got)}".ljust(78) + "\n")
        return got

    @staticmethod
    def _unpack(archive: Path, destination: Path) -> None:
        import zipfile
        wanted = ("ffmpeg.exe", "ffprobe.exe")
        found: dict[str, str] = {}
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                base = os.path.basename(member).lower()
                if base in wanted and base not in found:
                    found[base] = member
            missing = [w for w in wanted if w not in found]
            if missing:
                raise FramingError("압축 안에 " + ", ".join(missing) + " 이 없습니다")
            destination.mkdir(parents=True, exist_ok=True)
            for base, member in found.items():
                out = destination / base
                with zf.open(member) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(out, 0o755)

    # --- 실행 -------------------------------------------------------------

    def probe(self, path: Path) -> Optional[ProbeInfo]:
        tool = self.ffprobe
        if not tool:
            return None
        try:
            result = subprocess.run(
                [tool, "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            data = json.loads(result.stdout or "{}")
        except Exception:
            return None

        info = ProbeInfo()
        for stream in data.get("streams", []):
            kind = stream.get("codec_type")
            if kind == "video" and not info.vcodec:
                info.vcodec = stream.get("codec_name") or ""
                try:
                    info.reported_frames = max(0, int(stream.get('nb_frames') or 0))
                except (TypeError, ValueError):
                    pass
                try:
                    info.video_duration = float(stream.get('duration') or 0)
                except (TypeError, ValueError):
                    pass
                info.width = int(stream.get("width") or 0)
                info.height = int(stream.get("height") or 0)
                pix = stream.get("pix_fmt") or ""
                info.pix_fmt = pix
                info.alpha = pix.startswith(("yuva", "rgba", "bgra", "argb", "abgr",
                                             "gbrap", "ya"))
                tidy = lambda key: (stream.get(key) or "").strip().lower()
                info.color_range = tidy("color_range")
                info.color_primaries = tidy("color_primaries")
                info.color_transfer = tidy("color_transfer")
                info.color_space = tidy("color_space")
                rate = stream.get("r_frame_rate") or "0/1"
                try:
                    num, den = rate.split("/")
                    info.fps = float(num) / float(den) if float(den) else 0.0
                except Exception:
                    pass
            elif kind == "audio" and not info.acodec:
                info.acodec = stream.get("codec_name") or ""
        try:
            info.duration = float(data.get("format", {}).get("duration") or 0)
        except Exception:
            pass
        return info

    def why_failed(self) -> str:
        """마지막으로 조용히 돌린 ffmpeg 이 남긴 말. 실패했을 때만 쓸모가 있다."""
        return (getattr(self, "_last_error", "") or "").strip()

    def run(self, args: Sequence[str], quiet: bool = False,
            total_seconds: float = 0.0) -> int:
        """
        total_seconds 를 주면 ffmpeg 에게 진행 상황을 받아 몇 %인지 알려준다.
        긴 인코딩에서 화면이 멈춘 것처럼 보이지 않게 하려는 것.
        """
        binary = self.ensure()
        if total_seconds and total_seconds > 0:
            return self._run_with_progress(binary, args, quiet, total_seconds)
        if quiet:
            # 조용히 돌리더라도 하는 말은 받아 둔다. 아무 말 없이 실패하면
            # 왜 안 되는지 아무도 알 수 없다 — 실패했을 때만 꺼내 보여준다.
            return self._run_quiet(binary, args)

        # 진행 표시는 터미널에서만 의미가 있다. 파이프로 넘길 땐 로그만 더럽힌다.
        show_stats = is_interactive()
        command = [binary, "-hide_banner", "-loglevel", "error",
                   "-stats" if show_stats else "-nostats"]
        command += list(args)
        process = subprocess.Popen(command)
        self._active = process
        try:
            process.wait()
        finally:
            self._active = None
        completed = process
        if show_stats:
            width = shutil.get_terminal_size((120, 24)).columns
            sys.stdout.write("\r" + " " * max(80, width) + "\r")   # 진행 줄 지우기
            sys.stdout.flush()
        return completed.returncode

    def _run_quiet(self, binary: str, args: Sequence[str]) -> int:
        """화면은 조용히, 오류 메시지는 손에 쥔 채로."""
        command = [binary, "-hide_banner", "-loglevel", "error", "-nostats", *args]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True,
                                   errors="replace")
        self._active = process
        try:
            _out, err = process.communicate()
        finally:
            self._active = None
        lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
        self._last_error = " / ".join(lines[-3:])
        return process.returncode

    def _run_with_progress(self, binary: str, args: Sequence[str], quiet: bool,
                           total_seconds: float) -> int:
        command = [binary, "-hide_banner", "-loglevel", "error", "-nostats",
                   "-progress", "pipe:1", *args]
        # A pipe for stderr can fill while we read progress from stdout, deadlocking
        # the encoder. A temporary file drains diagnostics without a second thread.
        self._last_error = ""
        with tempfile.TemporaryFile(mode="w+b") as diagnostics:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True,
                                       encoding="utf-8", errors="replace", bufsize=1,
                                       stderr=diagnostics)
            self._active = process
            total_us = total_seconds * 1_000_000
            try:
                for line in process.stdout:
                    key, _, value = line.strip().partition("=")
                    if key not in ("out_time_us", "out_time_ms"):
                        continue
                    try:
                        done_us = int(value)
                    except ValueError:
                        continue
                    Progress.span(min(1.0, done_us / total_us) if total_us else 0.0)
                process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                if process.stdout:
                    process.stdout.close()
                self._active = None
            # Keep only a bounded tail, even when a damaged input floods stderr.
            diagnostics.seek(0, os.SEEK_END)
            diagnostics.seek(max(0, diagnostics.tell() - 16384))
            lines = [line.strip() for line in diagnostics.read().decode("utf-8", "replace").splitlines() if line.strip()]
            self._last_error = " / ".join(lines[-3:])
            if process.returncode == 0:
                Progress.span(1.0)
            elif not quiet and self._last_error:
                Console.warn(self._last_error)
            return process.returncode



# ══════════════════════════════════════════════════ 작업 공통






# ══════════════════════════════════════════════════ 1. 프레임 3장


from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional







"""One catalog shared by the application, standalone viewer and release bundle."""
from pathlib import Path
import json
import re

LOCALE_DATA = {'이미지 시퀀스 · GIF · MP4 · ProRes': 'Image sequence · GIF · MP4 · ProRes', '입력에 맞는 형식을 아래에서 고르세요.': 'Choose an output format below.', '영상 ↔ 시퀀스 · GIF · 파일 형식': 'Video ↔ sequence · GIF · formats', '변환 · GIF · 시퀀스 · 컷 분할': 'Convert · GIF · sequences · split cuts', '변환할 형식': 'Convert to', '영상 → 이미지 시퀀스': 'Video → image sequence', '시퀀스 → 영상': 'Sequence → video', 'GIF 애니메이션': 'Animated GIF', 'GIF FPS': 'GIF FPS', 'GIF 가로 크기': 'GIF width', '반복 재생': 'Loop', 'GIF 미리보기': 'Preview GIF', '시퀀스 형식': 'Sequence format', 'ProRes로 저장': 'Save as ProRes', '시퀀스 FPS': 'Sequence FPS', '컷 분할': 'Split cuts', '장면 · 간격 · 시간으로 나누기': 'Split by scene, interval or time', '나누는 방법': 'Split method', '장면 자동 감지': 'Detect scenes', '일정 간격': 'Fixed interval', '시간 직접 지정': 'Enter times', '감지할 컷': 'Scene sensitivity', '적게': 'Fewer', '표준': 'Standard', '많이': 'A lot', '최소 컷 길이(초)': 'Minimum cut length (s)', '분할 간격(초)': 'Split interval (s)', '나눌 시간': 'Split times', '컷': 'Cut', '원본 프레임 (1부터)': 'Source frames (from 1)', '구간(초)': 'Range (s)', '전체 길이를 GIF로 저장합니다. GIF에는 소리가 없고 색상 수가 제한됩니다.': 'Save the full duration as GIF. GIF has no audio and uses a limited palette.', '모든 프레임을 1번부터 저장합니다. 매번 새 폴더를 만들며 용량이 커질 수 있습니다.': 'Save every frame, numbered from 1, in a new folder. Image sequences can be large.', '끄면 MP4 · 켜면 ProRes, 알파가 있으면 4444': 'Off: MP4. On: ProRes, using 4444 for alpha.', '원본 시퀀스의 FPS를 입력하세요. 23.976 등 소수도 가능합니다.': 'Enter the source sequence FPS. Fractional rates such as 23.976 are supported.', '이미지 폴더 안의 파일을 번호 순서로 연결합니다. 빠진 번호는 건너뛰며 프레임을 추가하지 않습니다.': 'Use an image folder. Files are joined in numeric order; missing numbers do not add frames.', '이보다 짧은 자동 감지 컷은 합칩니다.': 'Merge shorter automatically detected cuts.', '자동 감지는 장면 변화 후보입니다. 빠른 움직임·플래시도 잡힐 수 있어 결과를 확인하세요.': 'Scene detection finds candidates. Fast motion or flashes can also trigger a cut; review the results.', '마지막 컷은 남은 길이만큼 저장합니다.': 'The final cut contains the remaining duration.', '쉼표로 구분합니다. 지정 시간 이상인 첫 프레임부터 다음 컷이 시작됩니다.': 'Separate times with commas. Each cut starts at the first frame at or after the requested time.', '컷별 MP4와 CSV·JSON 목록을 새 폴더에 저장합니다. 영상은 다시 인코딩하며 첫 오디오 트랙도 함께 나눕니다.': 'Save MP4 clips and CSV/JSON records in a new folder. Video is re-encoded with the first audio track.', '새 이름 · 번호': 'New name & numbers', '글자 찾아 바꾸기': 'Find & replace', '앞뒤에 덧붙이기': 'Add before / after', '이름의 일부를 바꾸거나 지워요': 'Replace or remove part of a name', '기존 이름을 살려서 정리해요': 'Keep the existing name', '파일 이름 정리': 'Tidy up file names', '어떻게 바꿀지 고르고, 미리본 뒤 적용하세요.': 'Choose an edit, preview it, then apply.', '이름 편집': 'Edit names', '새 이름': 'New name', '예: 가을_도시': 'e.g. autumn_city', '숫자 형식': 'Number format', '찾을 글자': 'Find text', '바꿀 글자': 'Replace with', '비워두면 찾은 글자를 지워요': 'Leave empty to remove the text', '앞에 붙일 글자': 'Add before', '뒤에 붙일 글자': 'Add after', '미리보기 반영': 'Update preview', '추가할 글자를 입력하세요.': 'Enter some text to add.', '확장자는 그대로 유지합니다. 실제 변경은 아래에서 적용하세요.': 'Extensions stay the same. Apply the changes below.', '더 많은 편집': 'More edits', '직전 편집 취소': 'Undo last edit', '처음 이름으로': 'Reset preview', '변경 되돌리기': 'Undo rename', '이렇게 바뀝니다': 'Preview changes', '개 선택': 'selected', '보이는 파일 모두 선택': 'Select all visible files', '선택 안 됨': 'Not selected', '표시할 파일이 없습니다. 검색어나 선택을 확인하세요.': 'No visible files. Check your search or selection.', '개 선택 파일이 검색으로 숨겨져 있습니다. 이 파일들도 변경에 포함됩니다.': 'selected files are hidden by your search. They are included in this rename.', '입력한 내용을 미리보기에 반영하세요.': 'Update the preview to use your edits.', '미리보기를 확인하고 있어요…': 'Checking the preview…', '개 이름을 확인해 주세요.': 'names need attention.', '개 이름을 변경할 준비가 됐어요.': 'names are ready to change.', '아직 바뀌는 이름이 없습니다. 위에서 편집해 보세요.': 'No changes yet. Choose an edit above.', '적용하기 전에는 파일 이름이 바뀌지 않습니다.': 'Your files keep their names until you apply.', '이름 변경하기': 'Apply rename', '오른쪽에서 차례로 실행하면 반영됩니다.': 'Run the steps on the right to apply.', '이름을 편집하고 미리보기를 확인하세요.': 'Edit the names and check the preview.', '샷 검사': 'Shot inspection', '콘택트시트': 'Contact sheet', '샷 살펴보기': 'Review shots', '검사 · 콘택트시트 · 대조': 'Inspect · contact sheet · compare', '살펴볼 파일을 넣으세요': 'Add files to review', '검토할 프레임 찾기': 'Find frames to review', '샷을 한 장으로 보기': 'See shots on one sheet', '작업 저장': 'Save project', '작업 열기': 'Open project', '일괄 이름 변경': 'Batch rename', '문자열 교체': 'Replace text', '앞에 추가': 'Add prefix', '뒤에 추가': 'Add suffix', '이름 지우기': 'Clear name', '앞에서 지우기': 'Trim start', '뒤에서 지우기': 'Trim end', '번호 붙이기': 'Add numbers', '숫자만 남기기': 'Keep digits', '숫자 지우기': 'Remove digits', '소문자로': 'Lowercase', '대문자로': 'Uppercase', '폴더명으로': 'Use folder name', '확장자 변경': 'Change extension', '찾을 문자열': 'Find text', '바꿀 문자열': 'Replace with', '새 확장자': 'New extension', '추가할 글자': 'Text to add', '글자 수': 'Character count', '시작 번호': 'Start at', '자릿수': 'Digits', '증가량': 'Increment', '번호 위치': 'Number position', '이름 뒤': 'After name', '이름 앞': 'Before name', '번호만': 'Numbers only', '미리보기에 추가': 'Add to preview', '규칙 지우기': 'Remove rule', '한 단계 취소': 'Undo draft step', '변경 초기화': 'Reset draft', '적용 되돌리기': 'Undo rename', '미리보기 계산 중…': 'Preparing preview…', '충돌': 'Conflicts', '현재 이름': 'Current name', '바꿀 이름': 'New name', '상태': 'Status', '변경 예정': 'Will change', '유지': 'Keep', '변경 중…': 'Renaming…', '변경 적용': 'Apply changes', '파일': 'File', '개 되돌림': 'files restored', '개 이름 변경': 'files renamed', '작업을 저장했습니다.': 'Project saved.', '경로를 확인하세요': 'Check file paths', '작업을 불러왔습니다. 실행할 준비가 되면 실행을 누르세요.': 'Project loaded. Press Run when ready.', '검토 후보': 'Review candidates', '1부터 시작하는 프레임 번호': 'Frame numbers start at 1', '시트 보기': 'View sheet', '정지 후보 최소 길이(초)': 'Minimum freeze (seconds)', '규칙을 쌓고 왼쪽 미리보기를 확인한 뒤 변경 적용을 누르세요.': 'Stack rules, review the names on the left, then apply.', '확장자 이름만 바뀝니다. 영상 형식 변환은 변환 도구를 쓰세요.': 'Only the extension changes. Use Convert to change the media format.', '적용 전에는 실제 파일 이름이 바뀌지 않습니다.': 'File names stay unchanged until you apply.', '정적인 장면도 후보가 될 수 있습니다. 눌러서 직접 확인하세요.': 'Static scenes may also be flagged. Click to inspect.', '설정한 기준에서 검토 후보를 찾지 못했습니다.': 'No candidates found at these thresholds.', '후보가 많아 앞의 500개만 표시합니다.': 'Showing the first 500 candidates.', '검은 프레임·정지·중복 후보와 시퀀스 번호 누락을 검사합니다. 원본은 바꾸지 않습니다.': 'Find black, frozen or duplicate candidates and missing sequence numbers. Originals stay unchanged.', '첫·중간·끝 프레임과 파일 정보를 PNG로 만듭니다. 6개 샷마다 한 페이지입니다.': 'Export first, middle and last frames with file details to PNG. Six shots per page.', 'Framing': 'Framing', '처음 화면': 'Home', '종료': 'Quit', '설명 보기': 'Guide', '무엇을 할까요': 'What are we doing?', '← 하던 작업으로 돌아가기': '← Back to what I was doing', '← 다른 일 고르기': '← Pick something else', '파일을 여기에 놓으세요': 'Drop your files here', '파일 고르기': 'Choose files', '폴더 고르기': 'Choose a folder', '경로 직접 붙여넣기': 'Paste paths instead', '이 경로 쓰기': 'Use these paths', '영상 다루기': 'Work on video', '뽑고 · 늘이고 · 뒤집고 · 바꾸기': 'Grab · retime · reverse · convert', '영상 · 시퀀스 폴더를 넣으세요': 'Drop videos or a sequence folder', '두 개 비교하기': 'Compare two', '전후를 겹쳐 보고 달라진 곳 찾기': 'Lay before and after on top of each other', '비교할 두 개를 넣으세요 — 먼저 넣은 것이 A': 'Drop exactly two — the first one is A', '이름 정리하기': 'Tidy names', '외계어 파일명을 읽히는 이름으로': 'Turn machine gibberish into names you can read', '이름을 정리할 파일들을 넣으세요': 'Drop the files you want renamed', '넣기': 'Add', '도구 고르기': 'Pick a tool', '실행': 'Run', '결과': 'Result', '뽑기': 'Grab', '원하는 지점 몇 장, 또는 전부를 시퀀스로': 'A few frames where you want them, or the whole thing as a sequence', '리타임': 'Retime', '길이 · 배속 바꾸기. 매트는 알아서 보호': 'Change length or speed. Mattes are protected automatically', '역재생': 'Reverse', '거꾸로. 시퀀스는 픽셀 손대지 않음': 'Backwards. Sequences keep their pixels untouched', '변환': 'Convert', 'MP4 · 누크용 · 용량 줄이기 · 이미지 · 묶기': 'MP4 · Nuke master · shrink · images · stitch', '대조': 'Compare', '두 개를 나란히 놓고 달라진 곳 찾기': 'Put two side by side and find what moved', '이름 바꾸기': 'Rename', 'AI 가 뱉은 외계어 이름을 읽히는 이름으로': 'Turn the gibberish an AI spat out into readable names', '이름': 'Name', '종류': 'Kind', '크기': 'Size', '정보': 'About', '영상': 'video', '이미지': 'image', '시퀀스': 'sequence', '없음': 'missing', '전부 고르기': 'Select all', '눌러서 이 순서로 세우기': 'Click to sort by this', '끌어서 순서 바꾸기': 'Drag to reorder', '눌러서 이름 고치기': 'Click to edit the name', '알파': 'alpha', '이름으로 거르기': 'Filter by name', '+ 더 넣기': '+ Add more', '폴더 넣기': 'Add a folder', '모두 비우기': 'Clear list', '옮겼거나 지워진 파일을 목록에서 뺍니다': 'Drop files that were moved or deleted', '여기에 파일 · 폴더를 끌어다 놓으면 목록에 더해집니다  ·  누르면 골라서 넣기': 'Drop files or folders here to add them  ·  or click to browse', '여기에 놓으면 목록에서 뺍니다': 'Drop here to take it off the list', '이 이름으로 바꿉니다': 'Rename to this', '다른 이름으로 다시 굴리기': 'Roll another name', '이름 고치기': 'Edit the name', '도커식 이름 굴려 보기 — 확인을 눌러야 바뀝니다': 'Roll a docker-style name — nothing changes until you confirm', '폴더에서 보기': 'Show in folder', '목록에서 빼기': 'Remove selected', '도커식 이름 굴리기': 'Roll docker names', '이 파일들만 남기기': 'Keep only these', '고른 것 풀기': 'Clear the selection', '확인': 'Confirm', '취소': 'Cancel', '다시': 'Again', '파일을 살펴보지 못했습니다': "Couldn't look at the files", '어디를': 'Where', '첫 · 중간 · 끝': 'First · middle · last', '균등하게 N장': 'N evenly spaced', 'N마다': 'Every N', '직접 찍기': 'Pick by hand', '전부 (시퀀스)': 'Everything (sequence)', '몇 장': 'How many', '간격(프레임)': 'Every (frames)', '지점': 'Points', '형식': 'Format', 'PNG만': 'PNG only', 'JPG만': 'JPG only', '무손실 시퀀스는 원본의 수십~수백 배 용량입니다': 'A lossless sequence runs tens to hundreds of times the source size', '밝기 범위': 'Levels', '자동': 'Auto', '펴기': 'Expand', '그대로': 'Leave it', '검정이 들뜨거나 흰색이 탁하면 "펴기"': 'If blacks look lifted or whites look muddy, use Expand', '길이를': 'Length', '몇 초로': 'Target duration', '몇 프레임으로': 'Target frames', '몇 배속으로': 'Playback speed', '초': 'seconds', '프레임': 'frames', '배속': 'speed', '0.5 = 절반 속도 / 2 = 두 배 빠르게': '0.5 = half speed / 2 = twice as fast', '채우는 법': 'Frame method', '알아서': 'Auto', '복제': 'Nearest frame', '모션보간': 'Motion interpolation', '매트는 복제로, 실사를 늘릴 땐 모션보간으로 갑니다': 'Auto uses nearest frames for mattes and motion to slow live action.', '메모 남기기': 'Leave a note', '보간한 자리를 적은 메모장(.txt)을 결과 옆에 놓습니다': 'Drops a .txt next to the result listing every frame it had to make', '받을 모습': 'Output', '확인용 영상도': 'A check video too', '8bit 는 mp4, 10bit·알파는 ProRes mov': '8bit → mp4, 10bit or alpha → ProRes mov', '어디서나 열림 · 8bit': 'Opens anywhere · 8bit', '누크용 마스터 · 계조와 알파 유지': 'Nuke master · keeps depth and alpha', 'PNG 시퀀스': 'PNG sequence', '프레임 파일만 · 영상 없음': 'Frames only · no video', '시퀀스 + MP4': 'Sequence + MP4', '프레임 파일과 확인용 mp4 둘 다': 'Frame files plus a check mp4', '시퀀스를 영상으로 묶을 때': 'Used when stitching a sequence into video', '영상을': 'Video to', 'MP4 (배포)': 'MP4 (share)', '누크용 마스터': 'Nuke master', '용량 줄이기': 'Shrink', 'MOV (상자만)': 'MOV (rewrap)', '강도': 'How hard', '조금': 'A little', '보통': 'Normal', '해상도': 'Height', '20~30% 더 작지만 느리고 호환성이 떨어집니다': '20~30% smaller, but slower and less compatible', 'ProRes 422 HQ 로. 알파가 있으면 알아서 4444 로 갑니다. ': 'ProRes 422 HQ, or 4444 automatically if there is alpha. ', '용량이 10~30배 커지는 게 정상입니다.': 'Growing 10~30× is normal, not a bug.', '이미지를': 'Images to', '세로 상한(px)': 'Max height (px)', '0 이면 원본 크기 그대로': '0 keeps the original size', '누크용으로 묶기': 'Stitch for Nuke', '켜면 ProRes. 알파가 있으면 4444': 'ProRes when on; 4444 if there is alpha', '번호 붙은 이미지 폴더는 영상 한 편으로 묶습니다.': 'A numbered image folder becomes one video.', '방식': 'How', '패턴 번호': 'Numbered pattern', '앞뒤 붙이기': 'Add prefix/suffix', '찾아 바꾸기': 'Replace', '지우기': 'Strip', '도커식 무작위': 'Docker-style', '이미 붙은 이름을 고칠 거면 앞뒤 붙이기 · 찾아 바꾸기 · 지우기': 'To fix names that already exist, use prefix/suffix, find-replace or strip', '단어': 'Words', '한글 두 단어': 'Korean pair', '도커 원본 (영문)': 'Docker original', '영문은 도커가 컨테이너에 쓰는 그 목록 그대로 — sleepy_turing 식': 'The English list is docker’s own — sleepy_turing and friends', '패턴': 'Pattern', '{n} 자리에 번호가 들어갑니다': 'The number goes where {n} is', '앞에': 'Before', '이름 앞에 붙습니다': 'Goes in front of the name', '뒤에': 'After', '이름 뒤에 붙습니다': 'Goes after the name', '앞 몇 글자 자르기': 'Cut from front', '뒤 몇 글자 자르기': 'Cut from back', '찾을 말': 'Find', '쉼표로 여러 개를 한 번에': 'Comma-separate to do several at once', '바꿀 말': 'Replace with', '찾은 말을 지우고, 남은 밑줄 · 붙임표는 알아서 정리합니다.': 'Removes what it finds and tidies the leftover underscores and dashes.', '조각 내기': 'Split the name', '표에서 파일을 골라야 이름을 조각낼 수 있습니다': 'Tick a file in the table and its name shows up here in pieces', '이 이름에는 - _ . 로 끊긴 자리가 없습니다': 'Nothing to split — this name has no - _ . in it', '조각을 누르고 아래 단추를 고르세요': 'Tap the pieces, then pick a button', '앞 이름': 'Previous name', '다음 이름': 'Next name', '고른 것 지우기': 'Drop picked', '고른 조각을 이름에서 뺍니다': 'Removes the pieces you picked', '고른 것만 남기기': 'Keep picked only', '고른 조각 말고 나머지를 전부 뺍니다': 'Removes everything you did not pick', '앞부터 여기까지': 'Head up to here', '이름 처음부터 마지막으로 고른 조각까지 뺍니다': 'Removes from the start through the last piece you picked', '여기부터 끝까지': 'From here to the end', '처음 고른 조각부터 이름 끝까지 뺍니다': 'Removes from the first piece you picked through the end', '자주 쓰는 말': 'Words you use a lot', '이 말 지우기': 'Remove this word', '+ 새 말': '+ new word', '아직 바뀌는 이름이 없습니다.': 'Nothing would change yet.', '지금 이름': 'Now', '바뀔 이름': 'Becomes', '바뀌는 것만': 'Only changes', '전부 보기': 'Show all', '한 번에 바꾸기': 'Rename all', '되돌리기': 'Undo', '방금 바꾼 이름을 되돌립니다': 'Put the last batch of names back', '이 줄은 그대로입니다': 'stays as it is', '보기': 'View', '반반 바': 'Wipe', '겹치기': 'Blend', '번갈아': 'Flicker', '나란히': 'Side by side', '창이 뜬 뒤에도 숫자키 1~4 로 바꿉니다': 'Keys 1~4 switch views once it opens', '파일로 남기기': 'Keep a file', '끄면 임시로 보고 Framing 닫을 때 지웁니다': 'Off means it is temporary and goes away when Framing closes', '먼저 넣은 것이 A, 나중이 B 입니다.': 'The first one is A, the second is B.', '이 안에서 봅니다. 파일은 남지 않습니다.': 'You watch it here. No file is left behind.', '크게': 'Bigger', '목록 보기': 'Show list', '파일 목록을 잠시 접고 크게 봅니다': 'Folds the file list away for a bigger view', '닫기 (Esc)': 'Close (Esc)', '굽는 중…': 'Rendering…', '대조를 굽지 못했습니다': "Couldn't render the comparison", '대조 화면을 띄우지 못했습니다': "Couldn't show the comparison", '무언가 잘못됐다면': 'If something went wrong', '지금 상태를 한 장으로 모읍니다 — 무슨 파일을 넣었는지, 무엇을 하려 했는지, 어디서 걸렸는지.': 'Collect the current state in one report: inputs, intended operation and where a problem occurred.', '진단서 만들기': 'Make a report', '파일로 저장': 'Save to a file', '모으는 중…': 'Gathering…', '복사했습니다 — 그대로 붙여넣어 주세요': 'Copied — just paste it', '아래 글을 그대로 복사해 주세요': 'Copy the text below', '저장했습니다': 'Saved', '저장하지 못했습니다': "Couldn't save", '다시 눌러 보고, 그래도 안 되면 알려 주세요.': 'Try running it again — if it still fails, let me know.', '새 창에서 열기': 'Open in a new window', '중단': 'Stop', '이어서': 'Next', '재료로만': 'Just add them', '도구는 고르지 않고 목록에만 넣습니다': 'Adds them to the list without picking a tool', '만든 것 전부': 'Everything made', '전부 재료로': 'Use all of it', '폴더 열기': 'Open folder', '원본 그대로': 'Straight from the source', '손댄 자리': 'Touched', '창으로 못 보냄': "Couldn't reach the screen", '알 수 없는 이유': 'no reason given', '알 수 없는 오류': 'Unknown error', '받지 못한 응답': 'Unanswered promise', '복사': 'Copy', '닫기': 'Close', '시작': 'Start', '화면': 'screen', '도구': 'Tools', '자세한 설명 열고 닫기': 'Show or hide the details', '파일을 먼저 넣으면, 누르는 즉시 그 도구로 갑니다.': 'Add files first and a click takes you straight to that tool.', '누르면 바로 그 도구가 선택됩니다.': 'Clicking picks that tool right away.', '대조 창 단축키': 'Compare shortcuts', '휠  확대 · 오른쪽 끌기  이동 · , .  한 프레임씩 · Esc  닫기': 'Wheel  zoom · right-drag  pan · , .  frame step · Esc  close', '먼저 파일을 넣어 주세요 — 서랍을 닫고 끌어다 놓으면 됩니다': 'Add files first — close this drawer and drop them in', '대조는 정확히 두 개를 넣었을 때만 됩니다': 'Compare needs exactly two files', '지금 넣은 파일로는 쓸 수 없는 도구입니다': "This tool doesn't fit what you've added", '폴더 고르는 중…': 'Opening the folder picker…', '파일 고르는 중…': 'Opening the file picker…', '경로 확인하는 중…': 'Checking the paths…', '넣는 중…': 'adding…', '파일 대화상자를 열지 못했습니다. "경로 직접 붙여넣기" 를 쓰세요.': 'Could not open the file dialog. Use "Paste paths instead".', '경로를 읽지 못했습니다. 오른쪽 아래 오류 줄을 봐 주세요.': 'Could not read those paths. See the error line at the bottom right.', '브라우저 모드에서는 끌어다 놓은 파일의 경로를 알 수 없습니다. ': 'In browser mode we cannot see the path of a dropped file. ', '"경로 직접 붙여넣기" 를 쓰세요.': 'Use "Paste paths instead".', '이름을 만들지 못했습니다': "Couldn't make a name", '바꾸지 못했습니다': "Couldn't rename", '있음': 'yes', '없음': 'no', ' · 창에 끌어다 놓기 됨': ' · drop onto the window works', '켬': 'on', '끔': 'off', '영상 한 편': 'one video', '처음': 'first', '중간': 'middle', '마지막': 'last', '고른 지점만 뽑습니다. 프레임 번호 · 백분율 · 시간으로도 찍고,': 'Grabs only the points you name — by frame, percent or timecode,', '"전부" 를 고르면 통째로 시퀀스가 됩니다 (PNG · JPG · EXR · DPX)': 'or pick "Everything" and the whole clip becomes a sequence (PNG · JPG · EXR · DPX)', 'JPG 는 4:4:4 · 10bit 는 16bit 로 · 틀린 밝기 표기는 바로잡음': 'JPG at 4:4:4 · 10bit lands as 16bit · wrong level tags get fixed', '원본 24프레임': 'source 24 frames', '결과 48프레임': 'result 48 frames', '점선 = 원본에 없던 프레임': 'dashed = frames the source never had', '복제는 옆 칸을 그대로 쓰고,': 'Nearest frame reuses the source.', '모션보간은 새로 그립니다': 'Motion creates an intermediate image.', '매트 · 뎁스 패스는 무조건 복제 — 라벨색을 섞으면 뜻이 사라집니다': 'Auto uses nearest frames for mattes and depth; mixing label colors changes their meaning.', '원본': 'source', '순서만 뒤집습니다': 'Only the order flips', '시퀀스면 파일을 역순으로 복사 — 픽셀은 손도 안 댑니다': 'A sequence is copied back to front — pixels are never touched', '영상이면 10bit · 알파는 ProRes 로 받아': 'Video with 10bit or alpha lands as ProRes', '계조와 투명도를 지킵니다': 'so depth and transparency survive', '영상을 넣으면': 'Drop a video', 'MP4  배포 · 업로드': 'MP4  share · upload', '상자만 교체 · 손실 0': 'rewrap only · zero loss', 'MOV  상자만 교체': 'MOV  rewrap only', '안에 든 코덱 그대로': 'keeps the codec inside', '재인코딩 · 스크러빙이 빨라짐': 're-encode · scrubs faster', '재인코딩 · 화질 조금 깎임': 're-encode · slightly softer', '이미지를 넣으면': 'Drop images', 'PNG · JPG · WEBP · 크기 줄이기': 'PNG · JPG · WEBP · resize', '시퀀스 폴더를 넣으면': 'Drop a sequence folder', '영상 한 편으로 묶습니다': 'becomes one video', '막대는 결과 용량. 누크용이 10~30배 커지는 건 고장이 아니라 정상입니다': 'Bars are output size. A Nuke master running 10~30x bigger is normal, not a fault', '경계를 손으로 끌어 A 와 B 를 가릅니다. 영상 · 이미지 둘 다 됩니다': 'Drag the seam to split A from B. Works for video and stills', 'ProRes · HEVC 처럼 창이 못 여는 형식은 알아서 바꿔 놓고 봅니다': 'Formats the window cannot open (ProRes, HEVC) get converted first', '창이 뜬 뒤에도': 'Once it opens,', '숫자키 1~4 로': 'number keys 1~4', '바로 바꿉니다': 'switch the view', '바뀜': 'Changing', '손댄 것 지우기': 'Clear my edits', '손으로': 'by hand', '눌러서 직접 고치기': 'Click to type your own', '실행하면 무작위 두 단어가 붙습니다': 'Two random words on Run', '확인을 눌러야 바뀝니다': 'nothing changes until you confirm', '바꾸는 중…': 'renaming…', '되돌리는 중…': 'putting names back…', '이름을 바꿨습니다': 'renamed', '끌어다 놓은 것 읽는 중…': 'reading what you dropped…', '파일 넣기': 'add files', '지금은 안 됨': 'not now', '로딩중…': 'Loading…', '내보내면 대조 화면만 나옵니다 — 원본은 그대로입니다': 'Export gives you the comparison only — your originals stay as they are', '지금 보고 있는 대조 그대로 한 편을 굽습니다. A · B 원본은 따로 나오지 않습니다.': 'Renders exactly what you are watching as one file. A and B are not exported separately.', '나오는 것은 대조 한 편뿐입니다. A · B 원본은 건드리지 않고, 따로 뽑히지도 않습니다.': 'You get one comparison file. A and B are never touched, and never come out on their own.', '프레임을 그림으로': 'Frames out as stills', '길이 · 배속 바꾸기': 'Change length or speed', '거꾸로 돌리기': 'Play it backwards', '형식 · 용량 바꾸기': 'Change format or size', '둘을 겹쳐 보기': 'Lay two on top of each other', '뽑기 · 리타임 · 역재생 · 변환': 'Grab · retime · reverse · convert', '영상을 넣으세요': 'Drop a video', '전후를 겹쳐 보기': 'Before and after, overlaid', '두 개를 넣으세요': 'Drop exactly two', '이름 한 번에 정리': 'Rename in one go', '파일을 넣으세요': 'Drop your files', '원하는 지점만 뽑거나 통째로 시퀀스로. 10bit 은 16bit 로 받습니다.': 'Grab the points you want, or the whole clip as a sequence. 10bit lands as 16bit.', '길이나 배속을 바꾸고, 어디를 건드렸는지 지도로 알려줍니다.': 'Change length or speed, and see a map of what got touched.', '순서만 뒤집습니다. 시퀀스는 픽셀을 손대지 않습니다.': 'Only the order flips. Sequences keep their pixels.', '형식을 바꾸거나 용량을 줄입니다. 상자만 바꿔도 되면 재인코딩하지 않습니다.': 'Change the format or shrink it. No re-encode when a rewrap will do.', '둘을 겹쳐 보고, 보고 있는 그대로 한 편으로 굽습니다.': 'Overlay two and render exactly what you see as one file.', '이름을 눌러 직접 고치거나, 번호 · 찾아 바꾸기 · 주사위로 한꺼번에.': 'Click a name to type your own, or use Number · Replace · Random on all of them.', '이렇게': 'How', '번호': 'Number', '주사위': 'Random', '표에서 이름을 눌러 직접 고쳐도 됩니다': 'You can also click a name and type your own', '{n} 자리에 번호': 'the number goes at {n}', '비우면 지웁니다': 'empty = delete it', '한글': 'Korean', '영문': 'English', '조용한_수달 / sleepy_turing': 'quiet_otter / sleepy_turing', '앞뒤': 'Around', '비워 두면 안 붙습니다': 'leave empty to skip', '담은 것만': 'Picked only', '담은 것만 이 도구로': 'Run this tool on the picked ones', '풀기': 'Deselect', '고른 것이 없습니다': 'Nothing selected', '줄을 눌러 고르면 도구가 켜집니다': 'click a row to select — tools switch on', '표에서 파일을 고르세요': 'Select files in the table first', '전부 풀기': 'Deselect all', '먼저 파일을 고르세요': 'Select files first', '두 개를 골라야 합니다': 'Needs exactly two', '지금': 'now', '고른 것으로는 못 합니다': "Doesn't fit what you picked", '먼저 고르기': 'select first', '대조는 정확히 두 개를 골랐을 때만 됩니다': 'Compare needs exactly two selected', '고른 파일로는 쓸 수 없는 도구입니다': "This tool doesn't fit the files you picked", '앞 작업 뒤에 이어 붙이기': 'Chain this after the current step', '만든 사람  외계냥 (Alien Meow)': 'Made by Alien Meow', '영상 만들다 매번 하던 노가다가 지겨워서 만들었습니다. 잘 쓰이면 좋겠습니다.': 'Built to simplify repetitive video production tasks. We hope it helps your workflow.', '붙이기': 'Add around', '비워 두면 아무것도 안 붙습니다': 'leave both empty and nothing is added', '예': 'Yes', '차례': 'Order', '차례로 실행': 'Run in order', '이 단계 설정 고치기': 'Edit this step', '이 단계 빼기': 'Take this step out', '앞 단계가 만든 것이 다음 단계의 재료가 됩니다': 'What each step makes becomes the next step’s material', '앞 단계가 아무것도 만들지 않아 여기서 멈춥니다.': 'The step before made nothing, so we stop here.', '한 번 더 누르면 빠집니다': 'click again to take it out', '한 줄에 하나씩': 'one per line', 'Framing 을 닫았습니다.': 'Framing is closed.', '살펴보는 중…': 'reading…', '없는 파일': 'Missing', '치우기': '— clear them', '목록에서 뺐습니다': 'taken off the list', '이름을 바꿉니다': 'will be renamed', '무작위 두 단어로': 'two random words', '무작위 두 단어 이름이 붙습니다': 'Everyone gets a random two-word name', '대상': 'targets', '완료': 'Done', '중단됨': 'Stopped', '성공': 'ok', '실패': 'failed', '끝난 것': 'finished', '한 번에 바꿉니다': 'renamed in one go.', '각각 돌립니다': 'run one by one.', '저장': 'Save', '폴더를 열지 못했습니다': "Couldn't open the folder", '파일을 직접 열었습니다 — Framing 으로 실행해야 동작합니다.': 'You opened the file directly — run it through Framing to make it work.', '왼쪽에서 도구를 고르세요': 'Pick a tool on the left', '왼쪽 표에서 파일을 고르세요': 'Select files in the table on the left', '고른 것만 돌아갑니다. 고르면 쓸 수 있는 도구가 켜집니다.': 'Only what you select runs. Selecting switches on the tools that fit.', '설정 · 진행 · 결과 · 대조 화면이 모두 이 칸에 나옵니다.': 'Options, progress, results and the comparison all show up here.', '홀드 검출 기준을 확인하세요.': 'Check the hold detection threshold.', '홀드 최소 길이는 0.1~10초로 입력하세요.': 'Minimum hold duration must be between 0.1 and 10 seconds.', '검은 프레임 후보': 'Possible black frames', '홀드 프레임 후보': 'Possible hold frames', '동일 프레임 반복': 'Identical repeated frames', '{0} · {1}–{2} 프레임': '{0} · frames {1}–{2}', '검사 FPS와 최소 길이는 숫자로 입력하세요.': 'Enter numeric values for inspection FPS and minimum duration.', '해상도 다름 · {0}×{1}': 'Resolution mismatch · {0}×{1}', '시퀀스 번호 누락 · {0}–{1}': 'Missing sequence numbers · {0}–{1}', '시퀀스 번호 중복 · {0}': 'Duplicate sequence number · {0}', '선택 영역은 정수 픽셀 좌표여야 합니다.': 'The selected region must use integer pixel coordinates.', '선택 영역이 A 기준 화면을 벗어났습니다.': 'The selected region extends outside the A reference canvas.', '대조 해상도를 확인할 수 없습니다.': 'Unable to determine the comparison resolution.', '대조 시간 정보를 확인할 수 없습니다.': 'Unable to determine the comparison timing.', '이 소재에서 지원하지 않는 저장 형식입니다.': 'This output format is not supported for this input.', '저장할 보기 또는 영역 설정이 올바르지 않습니다.': 'Invalid view or region settings.', '영상 길이를 확인할 수 없습니다.': 'Unable to determine the video duration.', '재생 위치가 올바르지 않습니다.': 'Invalid playback position.', '알 수 없는 대조 보기입니다.': 'Unknown comparison view.', '저장을 중단했습니다. 완료된 파일은 {0}에 있습니다.': 'Export stopped. Completed files are in {0}.', '{0} 저장에 실패했습니다. 완료된 파일은 {1}에 남아 있습니다.': 'Failed to save {0}. Completed files remain in {1}.', '프레임 수가 다릅니다: 예상 {0}, 저장 {1}. {2}': 'Frame count mismatch: expected {0}, saved {1}. {2}', '장면_{n}': 'shot_{n}', '장면 전환 · 일정 간격 · 지정 시간으로 나누기': 'Split by scene changes, fixed intervals or specified times', '자동 감지는 장면 변화 후보입니다. 컷별 MP4와 프레임 목록을 새 폴더에 저장합니다.': 'Automatic detection suggests scene boundaries. Saves each cut as an MP4 with a frame manifest in a new folder.', '프레임 시간과 장면 변화를 분석합니다': 'Analyzing frame timestamps and scene changes', '{0}컷 저장 · 원본의 모든 프레임을 한 번씩 사용합니다': 'Saving {0} cuts · every source frame is used exactly once', '영상의 프레임 시간을 읽지 못했습니다.': 'Unable to read frame timestamps.', '프레임 시간이 중복되거나 역전된 영상입니다. MP4로 변환한 뒤 다시 시도하세요.': 'Frame timestamps are duplicated or out of order. Convert to MP4 and try again.', '검사할 프레임이 없습니다.': 'No frames to inspect.', '장면 감지 기준': 'Scene detection threshold', '최소 컷 길이': 'Minimum cut duration', '한 번에 최대 500컷까지 저장합니다. 기준이나 간격을 조절하세요.': 'A maximum of 500 cuts can be saved at once. Adjust the threshold or interval.', '컷을 나눌 영상 정보를 읽지 못했습니다.': 'Unable to read the video information for splitting.', '{0}프레임 → {1}컷 · MP4 + 컷 목록': '{0} frames → {1} cuts · MP4 + cut manifest', '장면 분석 실패 (ffmpeg {0})': 'Scene analysis failed (ffmpeg {0})', '컷 시간 형식이 잘못되었습니다: {0}. 예: 3.5, 00:07.2': 'Invalid cut time: {0}. Example: 3.5, 00:07.2', '분할 간격': 'Split interval', '컷 저장이 완료되지 않았습니다 (ffmpeg {0}). 미완료 폴더: {1}': 'Cut export did not finish (ffmpeg {0}). Incomplete folder: {1}', '한 번에 최대 500컷까지 저장합니다. 간격을 늘려 주세요.': 'A maximum of 500 cuts can be saved at once. Increase the interval.', '모르는 컷 분할 방식입니다.': 'Unknown cut splitting method.', '나눌 시간을 입력하세요. 예: 3.5, 00:07.2': 'Enter cut times. Example: 3.5, 00:07.2', '컷 시간은 0초보다 크고 영상 길이 {0}초보다 작아야 합니다.': 'Cut times must be greater than 0 and less than the video duration of {0} seconds.', '실행 중인 작업이 끝난 뒤 변경하세요.': 'Wait for the current job to finish before making changes.', "\n'{0}' 설치에 실패했습니다.\n인터넷 연결을 확인하고 아래 명령을 직접 실행해 주세요.\n  {1} -m pip install {2}\n": "\nFailed to install '{0}'.\nCheck your internet connection and run this command manually:\n  {1} -m pip install {2}\n", '파이썬 {0}.{1} 이상이 필요합니다. 현재 {2}\nhttps://www.python.org/downloads/ 에서 설치해 주세요.\n': 'Python {0}.{1} or later is required. Current version: {2}\nInstall it from https://www.python.org/downloads/.\n', "[준비] '{0}' 가 없어 자동으로 설치합니다. 최초 1회만 걸립니다.": "[Setup] Installing missing dependency '{0}'. This is needed only once.", '[준비] 설치 반영을 위해 다시 시작합니다.\n': '[Setup] Restarting to load the installed dependency.\n', '[준비] 설치 완료.\n': '[Setup] Installation complete.\n', '영상 + 프레임 폴더': 'Video + frame folders', '영상·전체 PNG·새로 보간된 PNG·프레임 기록을 새 폴더에 저장': 'Save the video, all PNG frames, interpolated PNG frames and frame manifest in a new folder', '영상 · 시퀀스를 거꾸로': 'Reverse videos and image sequences', '길이 · 배속 바꾸기 (프레임 보간)': 'Change duration or speed (frame interpolation)', 'MP4 (배포·업로드)': 'MP4 (sharing and upload)', '가장 잘 열리는 형식. 대개 다시 인코딩하지 않고 상자만 갈아끼웁니다': 'A widely supported format. Usually repackages the container without re-encoding.', 'MOV (상자만 교체)': 'MOV (repackage only)', '확장자만 .mov 로. 안에 든 코덱도 화질도 그대로입니다': 'Changes the container to .mov while keeping the codec and quality.', '누크용 마스터 (ProRes)': 'Editing master (ProRes)', 'Nuke · After Effects · Resolve 가 물고 늘어지지 않는 편집용 코덱': 'An editing codec for Nuke, After Effects and Resolve', '반드시 다시 인코딩합니다. 화질이 조금 깎입니다': 'Always re-encodes. Some quality loss is expected.', '이미지 형식 바꾸기': 'Convert image format', 'PNG · JPG · WEBP 로. 크기도 같이 줄일 수 있습니다': 'PNG, JPG or WEBP, with optional resizing', '영상 또는 이미지 시퀀스를 움직이는 GIF로': 'Turn a video or image sequence into an animated GIF', '원본의 모든 프레임을 번호 붙은 이미지로': 'Save every source frame as a numbered image', '번호 붙은 이미지 폴더를 mp4 · 누크용 마스터로 묶습니다': 'Combine a numbered image folder into MP4 or a ProRes editing master', '영상 · 이미지 · 시퀀스를 원하는 형식으로': 'Convert videos, images and sequences to the format you need', '포개서 봅니다. 섞는 정도를 손잡이로 조절합니다 — 위치가 밀렸는지 볼 때': 'Overlay both inputs and adjust the blend to check alignment', '경계선이 좌우로 오가며 A 와 B 를 번갈아 덮습니다': 'The divider moves across A and B to reveal each input', 'A 와 B 를 빠르게 번갈아 보여줍니다. 미세한 차이는 이게 제일 잘 보입니다': 'Alternate A and B rapidly to spot small differences', '왼쪽 A, 오른쪽 B. 전체 인상을 비교할 때 씁니다': 'A on the left, B on the right, for comparing the full composition', '두 영상 · 이미지를 나란히 놓고 달라진 곳 찾기': 'Compare two videos or images to find changes', '문자열·번호·확장자를 미리 보고 한 번에 변경': 'Preview and batch edit text, numbering and extensions', '\n    AI 영상 이름은 dreamina-2026…-n2vns 처럼 사람이 못 읽습니다. 여기서 정리하세요.\n\n      패턴    장면_{n}  →  장면_01.mp4, 장면_02.mp4 …  ({n} 자리에 번호)\n      도커식  조용한_수달.mp4 처럼 기억하기 쉬운 두 단어를 무작위로\n\n    실행하기 전에 무엇이 무엇으로 바뀌는지 전부 보여주고,\n    하나라도 부딪히면 아무것도 바꾸지 않습니다. 넣은 순서대로 번호가 붙습니다.': '\n    Clean up long, generated filenames before sharing.\n\n      Pattern: shot_{n} → shot_01.mp4, shot_02.mp4 … ({n} is the number)\n      Random words: a memorable two-word name.\n\n    Preview every change before applying it. If any name conflicts, nothing is renamed.\n    Numbers follow the order in the list.', '검은·정지·중복 후보와 시퀀스 누락 확인': 'Find black, held or repeated frames and missing sequence numbers', '원본을 수정하지 않고 검토할 후보를 찾습니다. 프레임 번호는 0부터입니다.': 'Suggests frames to review without modifying the source. Frame numbers start at 1.', '샷 정보 · 콘택트시트': 'Shot info · contact sheet', '첫·중간·끝 프레임을 한 장으로': 'First, middle and last frames on one sheet', '선택한 파일을 6개씩 한 페이지로 묶습니다. PNG 미리보기이며 원본 화질 검증용은 아닙니다.': 'Groups six files per page. PNG previews are for review, not source quality verification.', '  ·  {0}장, {1}': '  ·  {0} frames, {1}', '{0}x{1}, {2}fps, 총 {3}프레임  ·  {4}': '{0}x{1}, {2}fps, {3} frames total · {4}', '거의 그대로, 조금만 줄어듦': 'Nearly unchanged, with a small size reduction', '보통 이걸 씀': 'Recommended for most uses', '눈에 띄게 물러지지만 많이 줄어듦': 'Noticeably softer, with a larger size reduction', '{0} → {1}프레임  ·  {2}배속  ·  {3}': '{0} → {1} frames · {2}× speed · {3}', '{0}x{1}, {2}fps, {3}, 약 {4}프레임 → {5}': '{0}x{1}, {2}fps, {3}, about {4} frames → {5}', '{0}지점: {1} … {2}': '{0} positions: {1} … {2}', ' · 알파 유지': ' · alpha preserved', '원본 깊이로 뽑았습니다 ({0} PNG{1})': 'Extracted at source depth ({0} PNG{1})', '색 범위  {0}': 'Color range  {0}', ' (10bit·알파는 여기서 8bit 로 내려앉습니다)': ' (10-bit and alpha are reduced to 8-bit here)', '프레임만': 'Frames only', '{0}프레임 역순 복사 (픽셀 그대로)': 'Copying {0} frames in reverse order (unchanged pixels)', '{0}프레임 역순': '{0} frames reversed', '묶기  {0}': 'Encoding  {0}', '할 일  거꾸로 · {0}': 'Plan  reverse · {0}', '{0}프레임 역순 시퀀스': 'Reversed sequence of {0} frames', '계획  {0}': 'Plan  {0}', '방식  {0} — {1}': 'Method  {0} — {1}', '배치  {0} {1}장 · {2} {3}장': 'Mapping  {0}: {1} frames · {2}: {3} frames', '자리  {0}': 'Positions  {0}', '원본 깊이({0}bit{1})로 읽는 중': 'Reading at source depth ({0}-bit{1})', '결과가 대략 {0} 나옵니다 ({1}장 PNG) — 디스크 여유를 확인하세요': 'Estimated output size: {0} ({1} PNG frames). Check available disk space.', '프레임을 전부 올리는 데 약 {0} 가 필요합니다 — 리타임은 앞뒤를 같이 봐야 해서 어쩔 수 없습니다': 'Loading all frames requires about {0} of memory; retiming needs neighboring frames.', '메모리가 모자라면 Sequence 로 나눠 뽑고 구간별로 돌리세요': 'If memory is limited, extract a sequence and process it in sections.', '소재  매트 · 뎁스 패스 (색상 집중도 {0}%)': 'Material  matte / depth pass (color concentration {0}%)', '라벨  {0}': 'Labels  {0}', '고름  줄이는 중이라 복제 — 버리는 작업이라 새로 그릴 게 없습니다': 'Auto  nearest frame for shortening; no new frames are needed', '{0}프레임은 {1}fps 로 딱 떨어지지 않습니다 ({2}초) — 맞추려면 {3}': '{0} frames do not divide evenly at {1}fps ({2}s). For an exact duration, use {3}.', '이미 그 형식이고 다시 인코딩할 것도 없어 건너뜁니다 (용량을 줄이려면 compress)': 'Already in this format with no re-encoding needed; skipped. Use Compress to reduce size.', '할 일  {0}': 'Plan  {0}', '출력  {0}': 'Output  {0}', '{0} → {1}  (영상 데이터 그대로)': '{0} → {1} (video data unchanged)', '{0} 는 mp4 에 못 들어가서 h264 로 다시 인코딩': '{0} is not supported in MP4; re-encoding as H.264', '요청대로 h264 로 다시 인코딩': 'Re-encoding as H.264 as requested', '{0} 는 mov 에 못 들어가서 h264 로 다시 인코딩': '{0} is not supported in MOV; re-encoding as H.264', '{0} 로 다시 인코딩 — 누크가 바로 읽습니다': 'Re-encoding as {0} for editing in Nuke', '시퀀스를 영상으로': 'Image sequence to video', "넣으신 것이 {0} 라 목적지를 '{1}' 로 바꿨습니다": "Input is {0}; destination changed to '{1}'", '목적지  {0}': 'Destination  {0}', '{0} 상자는 브라우저가 못 엽니다': 'The browser cannot open the {0} container', '{0} 는 브라우저가 못 엽니다': 'The browser cannot decode {0}', '10bit 는 브라우저가 못 엽니다': 'The browser cannot decode this 10-bit video', '보기  {0} — {1}': 'View  {0} — {1}', 'Compare 는 파일 두 개가 필요합니다. A 와 B 를 같이 넣어 주세요.': 'Compare requires two files. Add both A and B.', '{0} — 볼 수 있는 형태로 바꾸는 중': '{0} — preparing a playable preview', '보기용으로 변환함': 'Converted for preview', '{0} (원본은 손대지 않았습니다)': '{0} (source unchanged)', '두 파일의 색 규격이 다릅니다 — 차이의 원인이 여기일 수 있습니다': 'The files have different color metadata, which may explain visible differences.', '{0}개 이름 바꿈': 'Renamed {0} files', '바뀔 이름이 없습니다': 'No names would change', '전부': 'All', '전체 프레임 저장 미완료 (ffmpeg {0}). 폴더: {1}': 'Full frame export did not finish (ffmpeg {0}). Folder: {1}', '원본 깊이로 다시 뽑기에 실패해 8bit 로 갑니다': 'Source-depth extraction failed; falling back to 8-bit', '이미 0~255 를 쓰고 있어 펼 것이 없습니다': 'Already uses the full 0–255 range; no expansion needed', '표기가 틀린 밝기 범위를 펴서 저장합니다': 'Correcting the mislabeled luma range before saving', '검정이 들뜨고 흰색이 탁해 보이면 --levels full 로 다시 뽑아 보세요': 'If blacks look lifted and whites look dull, try extracting with --levels full.', '프레임을 하나도 읽지 못했습니다.': 'Unable to read any frames.', '프레임 디코딩에 모두 실패했습니다.': 'All frame decoding attempts failed.', '{0} — 소스 계조를 그대로 지킵니다': '{0} — preserving source depth', '영상 + 프레임 폴더는 리타임용입니다. 역재생은 시퀀스 + MP4를 선택하세요.': 'Video + frame folders is for retiming. For reverse playback, choose Sequence + MP4.', '이미지가 없습니다.': 'No images found.', '{0} 짜리라 프레임을 경유합니다 (메모리 안전, 소리는 빠집니다)': 'Processing {0} through intermediate frames to limit memory use; audio is omitted.', '역재생 파일이 만들어지지 않았습니다.': 'The reversed video was not created.', '길이가 그대로입니다 — 프레임을 다시 번호만 매겨 내보냅니다': 'Duration is unchanged; exporting the frames with new numbering', '결과 프레임이 만들어지지 않았습니다.': 'No output frames were created.', '원본 확인  디코딩 픽셀이 완전히 같은 인접 프레임 쌍 {0}개 — 정지 구간의 길이는 샷 검사에서 확인하세요': 'Source check  {0} adjacent frame pairs have identical decoded pixels. Use Shot inspection to measure hold duration.', '배치 계획  {0}개 위치에서 앞 프레임을 재사용합니다 — 홀드 검출 결과가 아닙니다': 'Mapping plan  {0} positions reuse the preceding frame. This is not a hold detection result.', '메모  {0}': 'Note  {0}', '원본 깊이로 읽지 못해 8bit 로 갑니다': 'Unable to read at source depth; falling back to 8-bit', '소재  색 복원 끔 (--no-snap)': 'Material  label color restoration disabled (--no-snap)', '소재  실사 · 페인팅 (색상 집중도 {0}%) — 색은 건드리지 않습니다': 'Material  live action / painting (color concentration {0}%) — colors are unchanged', '{0} 은 ffmpeg 이 있어야 합니다 — 겹치기로 갑니다': '{0} requires FFmpeg; falling back to blending', '고름  매트라서 복제 — 라벨색을 섞으면 그 픽셀의 뜻이 사라집니다': 'Auto  nearest frame for mattes; blending would change label values', '고름  ffmpeg 이 없어 겹치기': 'Auto  blending because FFmpeg is unavailable', ' 또는 ': ' or ', '{0}   (내용을 못 읽었습니다)': '{0} (unable to read details)', 'ffmpeg 실패 (종료코드 {0})': 'FFmpeg failed (exit code {0})', '작아짐': 'smaller', '커짐': 'larger', ', {0}p 로 축소': ', scaled down to {0}p', '계조  {0}': 'Bit depth  {0}', '대조는 영상 두 개 또는 이미지 두 개를 선택하세요.': 'Select two videos or two images to compare.', '창 안에 대조를 띄웠습니다': 'Comparison opened in the app', '파일로도 남겼습니다: {0}': 'Also saved to: {0}', "그쪽은 까맣게 나옵니다. 'mp4 로 저장' 은 원본으로 굽기 때문에 이 형식도 제대로 나옵니다": 'That input will appear black. MP4 export reads the source and can still render this format.', '{0} 은 창에서 못 엽니다 — mp4 로 저장을 쓰세요': '{0} cannot be opened in the viewer. Use MP4 export.', ' · {0}p 로 줄임': ' · scaled down to {0}p', '대조 영상은 만들지 못했습니다 — 뷰어는 그대로 씁니다': 'Unable to create the comparison video. The viewer is still available.', '이름을 바꿀 파일을 고르세요.': 'Select files to rename.', '지원하는 Framing 작업 파일이 아닙니다.': 'This is not a supported Framing project file.', '파일 목록이 올바르지 않습니다.': 'Invalid file list.', '선택 목록이 올바르지 않습니다.': 'Invalid selection list.', '작업 단계가 올바르지 않습니다.': 'Invalid workflow steps.', '검사·콘택트시트·대조는 단독 작업으로 저장하세요.': 'Save inspection, contact sheet or comparison as a single-step project.', '작업 파일이 너무 큽니다 (최대 2MB).': 'Project file is too large (maximum 2MB).', '{0}프레임 검사 · 검토 후보 {1}곳': 'Inspected {0} frames · {1} review candidates', '{0}: 미리보기 프레임을 읽지 못했습니다.': '{0}: unable to read a preview frame.', '콘택트시트로 볼 파일을 고르세요.': 'Select files for the contact sheet.', '{0}개 샷 · {1}페이지': '{0} shots · {1} pages', '읽는 단계에서 이미 폈습니다': 'Range was already expanded during decoding', '눌린 그림을 0~255 로 폅니다 (--levels full)': 'Expanding luma to 0–255 (--levels full)', '{0} ({1}) 는 읽지 못해 건너뜁니다': 'Unable to read {0} ({1}); skipped', '영상을 만들지 못했습니다. ffmpeg 을 확인해 주세요.': 'Unable to create the video. Check FFmpeg.', '한 번에 뒤집기 실패 — 프레임을 경유해 다시 시도합니다': 'Direct reverse failed; retrying through intermediate frames', '프레임 분해에 실패했습니다.': 'Unable to extract intermediate frames.', '고름  실사를 늘리는 중이라 모션 보간 — 없던 프레임이 있어야 매끄럽습니다': 'Auto  motion interpolation for slowing live action; new frames improve smoothness', '저장 실패: {0}': 'Save failed: {0}', '알파 채널이 있는데 이 형식은 못 담습니다. 투명도가 사라집니다. 투명도를 지키려면 누크용 마스터로 보내세요.': 'This input has alpha, which the output format cannot store. Choose a ProRes master to preserve transparency.', '  (ProRes 는 편집용이라 커지는 게 정상입니다)': ' (ProRes is an editing format, so a larger file is expected)', '   (컨테이너만 바꿀 땐 해상도를 못 줄입니다)': ' (container-only conversion cannot resize the video)', '상자만 교체 — {0} 영상은 손대지 않습니다': 'Container-only conversion — {0} video is unchanged', '다시 담기만 — {0} 을 비트 그대로 .mov 에 넣습니다 (안에 든 건 여전히 {1} 입니다)': 'Repackaging {0} into .mov without changing its bits (codec remains {1})', '대조 비율은 0~1이어야 합니다.': 'Comparison ratio must be between 0 and 1.', '잠깐 보는 용도라 임시 자리에 만들었습니다 — Framing 을 닫으면 지워집니다': 'Saved to a temporary preview folder; removed when Framing closes', '{0} 은 창에서 볼 수 있는 형태로 바꾸지 못했습니다': 'Unable to convert {0} to a playable preview', '해상도가 다릅니다 — B 를 {0}x{1} 로 비율을 유지해 검은 여백으로 맞춥니다': 'Resolution mismatch — fitting B into {0}x{1} with black padding and preserved aspect ratio', '작업 설정을 읽을 수 없습니다.': 'Unable to read workflow settings.', '알 수 없는 작업 단계입니다.': 'Unknown workflow step.', '작업 파일을 읽을 수 없습니다.': 'Unable to read the project file.', '읽을 수 있는 프레임이 없습니다.': 'No readable frames found.', '일찍 디코딩이 끝났습니다 · 예상 {0}, 읽음 {1}': 'Decoding ended early · expected {0}, read {1}', '{0}: 이미지가 없습니다.': '{0}: no images found.', '영상 저장에 실패했습니다. 프레임은 {0}에 보관되어 있습니다.': 'Video export failed. Frames are preserved in {0}.', ' +알파': ' +alpha', '  (원본이 이미 알뜰하게 인코딩돼 있습니다 — 세게 줄이거나 그냥 원본을 쓰세요)': ' (the source is already efficiently encoded; use stronger compression or keep the original)', '  (애플이 열도록 꼬리표만 hvc1 로)': ' (tag changed to hvc1 for Apple compatibility)', '  · 애플이 열도록 꼬리표만 hvc1 로': ' · tag changed to hvc1 for Apple compatibility', '폴더(시퀀스)': 'Folder (sequence)', '{0} 은 {1}번째 방법으로 바꿨습니다': 'Converted {0} using method {1}', '대조 창을 열었습니다': 'Comparison window opened', '뷰어를 직접 여세요: {0}': 'Open the viewer manually: {0}', 'Framing 작업': 'Framing project', '영상없음': 'no video', '소리없음': 'no audio', '색 규격  {0}': 'Color metadata  {0}', '전체범위라고 적혀 있는데 실제로는 눌려 있습니다 — 제한범위로 바로잡아 읽고, 결과에도 그렇게 적어 둡니다': 'Tagged as full range but appears limited; decoding and output metadata will use limited range.', '중단했습니다.': 'Stopped.', '색역 {0} · 전달 {1} · 매트릭스 {2} · 범위 {3}': 'Primaries {0} · transfer {1} · matrix {2} · range {3}', 'gyan.dev (ffmpeg.org 공식 링크)': 'gyan.dev (linked by ffmpeg.org)', 'ffmpeg 이 없습니다. 이 작업은 ffmpeg 없이는 못 합니다.\n         윈도우 : winget install Gyan.FFmpeg\n         macOS  : brew install ffmpeg\n         우분투 : sudo apt install ffmpeg': 'FFmpeg is required. Install it using winget install Gyan.FFmpeg (Windows), brew install ffmpeg (macOS), or sudo apt install ffmpeg (Ubuntu/Debian).', 'ffmpeg 이 없어 내려받습니다. 약 80MB, 최초 1회.': 'FFmpeg is missing. Downloading about 80MB; needed only once.', '설치 위치: {0}  (이 폴더만 지우면 원상복구)': 'Install location: {0} (remove this folder to uninstall)', '두 곳 모두 실패했습니다. 회사망·프록시 차단일 수 있습니다.': 'Both download sources failed. A company network or proxy may be blocking them.', 'winget install Gyan.FFmpeg 를 쓰거나,': 'Run winget install Gyan.FFmpeg, or', '직접 받은 ffmpeg.exe / ffprobe.exe 를 {0} 에 넣어 주세요.': 'place ffmpeg.exe and ffprobe.exe in {0} manually.', 'ffmpeg 이 없습니다. 이 작업에는 ffmpeg 이 필요합니다.': 'FFmpeg is missing and is required for this operation.', 'Homebrew 가 없습니다. 터미널에 아래 두 줄을 차례로 붙여넣으세요.': 'Homebrew is missing. Paste the following two lines into Terminal in order.', '끝나면 이 창을 닫고 Framing 을 다시 열면 됩니다.': 'When finished, close this window and reopen Framing.', '영상을 열 수 없습니다: {0}': 'Unable to open video: {0}', '영상을 다시 열 수 없습니다: {0}': 'Unable to reopen video: {0}', '영상이 열려 있지 않습니다.': 'No video is open.', '이미지가 없습니다: {0}': 'No images found: {0}', '미지정': 'Unspecified', 'ffmpeg 이 없습니다.': 'FFmpeg is missing.', '우분투 · 데비안 : sudo apt install ffmpeg': 'Ubuntu / Debian: sudo apt install ffmpeg', '페도라        : sudo dnf install ffmpeg': 'Fedora: sudo dnf install ffmpeg', '{0} 에서 받는 중': 'Downloading from {0}', 'Homebrew 가 있습니다. 설치를 시작합니다 (몇 분 걸립니다)': 'Homebrew is available. Starting installation; this may take a few minutes.', '설치가 끝났는데도 못 찾았습니다. 터미널을 새로 열고 다시 실행해 보세요.': 'Installation finished, but FFmpeg was not found. Open a new terminal and try again.', '이미지를 읽지 못했습니다: {0}': 'Unable to read image: {0}', '준비됨: {0}': 'Ready: {0}', '{0} 밖에 못 받았습니다': 'Only {0} was downloaded', '압축 안에 ffmpeg.exe 가 없습니다': 'The archive does not contain ffmpeg.exe', '실패: {0}': 'Failed: {0}', ' 이 없습니다': ' is missing', '압축 안에 ': 'Inside the archive: ', '{0}: {1}~{2} 범위로 입력하세요.': '{0}: enter a value between {1} and {2}.', '{0}: 숫자를 입력하세요.': '{0}: enter a number.', '이미지 시퀀스 폴더를 넣어 주세요.': 'Add a folder containing an image sequence.', 'GIF로 바꿀 영상의 길이를 읽지 못했습니다.': 'Unable to read the video duration for GIF conversion.', 'GIF 1/2 · 전체 영상의 색상표를 만듭니다': 'GIF 1/2 · building a palette from the full video', 'GIF 2/2 · 애니메이션을 저장합니다': 'GIF 2/2 · saving the animation', 'GIF · {0}fps · 최대 {1}px · {2}': 'GIF · {0}fps · maximum {1}px · {2}', '{0}장 → {1}초 · {2}fps · {3}': '{0} images → {1}s · {2}fps · {3}', 'GIF 색상표 생성 실패 (ffmpeg {0})': 'GIF palette generation failed (ffmpeg {0})', 'GIF 저장 실패 (ffmpeg {0})': 'GIF export failed (ffmpeg {0})', '시퀀스의 크기·채널·비트 깊이를 맞춰 주세요: {0}': 'Sequence images must have matching dimensions, channels and bit depth: {0}', 'MP4에는 알파가 저장되지 않습니다. 알파가 필요하면 ProRes를 선택하세요.': 'MP4 does not store alpha. Choose ProRes to preserve transparency.', '시퀀스 영상 저장 실패 (ffmpeg {0})': 'Sequence video export failed (ffmpeg {0})', '겹치지 않는 이름을 만들지 못했습니다. 패턴 방식을 쓰세요.': 'Unable to generate a unique name. Use a naming pattern.', '이름없음': 'Untitled', '이름이 비어 있습니다': 'The name is empty', '이름에 사용할 수 없는 문자가 있습니다': 'The name contains invalid characters', '이름 끝에 공백이나 점을 사용할 수 없습니다': 'A name cannot end with a space or period', '파일 이름이 너무 깁니다': 'The filename is too long', '운영체제가 예약한 이름입니다': 'This name is reserved by the operating system', '{0} 이 이미 있습니다': '{0} already exists', '같은 파일이 두 번 들어 있습니다 — 아무것도 바꾸지 않았습니다.': 'The same file appears twice. Nothing was renamed.', '새 이름끼리 겹칩니다 — 아무것도 바꾸지 않았습니다.': 'New names conflict with each other. Nothing was renamed.', '무엇을 찾을지 적어 주세요.': 'Enter text to find.', '{0} 이 없습니다 — 이미 옮겨졌거나 지워졌습니다.': '{0} no longer exists; it was moved or deleted.', '변경 규칙은 100개까지 쌓을 수 있습니다.': 'A maximum of 100 rename rules is allowed.', '숫자는 0~{0} 사이로 적어 주세요.': 'Enter a number between 0 and {0}.', '알 수 없는 이름 변경 규칙입니다.': 'Unknown rename rule.', '추가할 글자가 너무 깁니다.': 'The text to add is too long.', '원본 파일을 찾을 수 없습니다': 'Source file not found', '{0} 파일이 없습니다 — 아무것도 바꾸지 않았습니다.': '{0} is missing. Nothing was renamed.', '이름 변경은 같은 폴더 안에서만 할 수 있습니다.': 'Files can only be renamed within the same folder.', '{0} 이 이미 있습니다 — 아무것도 바꾸지 않았습니다.': '{0} already exists. Nothing was renamed.', '이름 변경에 실패해 원래 이름으로 복구했습니다: {0}': 'Rename failed; original names were restored: {0}', '번호와 글자 수는 정수로 적어 주세요.': 'Use whole numbers for numbering and character counts.', '다른 줄과 새 이름이 겹칩니다': 'This new name conflicts with another row', '되돌릴 이름 변경이 없습니다.': 'No rename operation to undo.', '이름 변경과 일부 복구에 실패했습니다: {0}\n남아 있는 파일 → 원래 이름:\n{1}': 'Rename and part of the rollback failed: {0}\nRemaining files → original names:\n{1}', '찾을 문자열을 적어 주세요.': 'Enter the search text.', '같은 이름의 파일이 이미 있습니다': 'A file with this name already exists', '{0} 파일이 미리보기 이후 바뀌었습니다. 다시 확인하세요.': '{0} changed after the preview. Review it again.', '{0} 파일이 바뀌었습니다. 되돌리기를 중단했습니다.': '{0} has changed. Undo was stopped.', '번호 위치를 확인하세요.': 'Check the numbering position.', '원본 프레임을 그대로 골라 씁니다. 색이 절대 섞이지 않습니다': 'Selects original frames without blending their pixels', '앞뒤 프레임을 비율대로 섞습니다. 잔상이 남지만 실패하지 않습니다': 'Blends neighboring frames by their relative position; ghosting may occur', '모션 보간': 'Motion interpolation', '움직임을 추정해 없던 프레임을 그립니다. 제일 부드럽지만 찢어질 수 있습니다': 'Estimates motion to create intermediate frames; smooth results may contain distortions', '밝기 {0}~{1} (영상 표기: {2})': 'Luma {0}–{1} (video tag: {2})', '빨강': 'red', '{0}프레임 ({1}초) → {2}프레임 ({3}초)  ·  {4}배속 ({5})': '{0} frames ({1}s) → {2} frames ({3}s) · {4}× speed ({5})', '{0}프레임({1}초)은 한 번에 하기엔 깁니다 — {2}프레임({3}초)까지만 합니다.\n         --frames · --seconds · --speed 를 같이 주지는 않았는지, 자릿수가 맞는지 확인해 보세요.': '{0} frames ({1}s) exceeds the per-job limit of {2} frames ({3}s).\nCheck --frames, --seconds and --speed for conflicting options or typing errors.', '{0}프레임을 {1}프레임으로 ({2}배) 늘립니다 — 오타가 아닌지 확인하세요': 'Expanding {0} frames to {1} frames ({2}×). Check this is intentional.', '{0}{1} 번째 (총 {2}장)': '{0}{1} (total {2} frames)', '첫': 'first', '끝': 'last', '안 적힘': 'unspecified', '규격 안 적힘': 'metadata unspecified', '{0}bit {1} 유지': '{0}-bit {1} preserved', '노랑': 'yellow', '초록': 'green', '청록': 'cyan', '파랑': 'blue', '마젠타': 'magenta', '무채색(뎁스)만 — 색이 끼면 회색으로 되돌립니다': 'Grayscale depth only; remove unintended color casts', '새로 만든 프레임 없음 — 원본 프레임만 골라 썼습니다': 'No new frames — only source frames were selected', '처음과 끝을 뺀 거의 전부 ({0}/{1}장) — 원본 {2}장 사이를 {3}장으로 다시 나누느라 눈금이 안 맞습니다': 'Almost all except the first and last ({0}/{1} frames); resampling {2} source frames into {3} positions rarely lands on an original position', '{0}번째부터 {1}장마다 (총 {2}장)': 'Every {1} frames, starting at {0} (total {2} frames)', '프레임 배치 기록입니다. 보간 위치·재사용 횟수는 홀드 검출 결과가 아닙니다.': 'This is a frame mapping record. Interpolation positions and reuse counts are not hold detection results.', 'Framing 리타임 메모': 'Framing retiming note', '원본   {0}': 'Source  {0}', '적은 때 {0}': 'Created  {0}', '길이   {0}프레임 ({1}초) → {2}프레임 ({3}초)': 'Duration  {0} frames ({1}s) → {2} frames ({3}s)', '배속   {0}배 ({1})': 'Speed  {0}× ({1})', '방식   {0}': 'Method  {0}', '{0}  {1}장': '{0}  {1} frames', '자리   {0}': 'Positions  {0}', '{0} 프레임 번호 (결과 기준, 1부터)': '{0} frame numbers (output, starting at 1)', '없음 — 원본 프레임만 골라 썼습니다': 'None — only source frames were selected', ' … (총 {0}장)': ' … (total {0} frames)', '매 {0}프레임': 'Every {0} frames', '균등 {0}장': '{0} evenly spaced frames', '{0} 저장 실패: {1}': 'Failed to save {0}: {1}', 'ProRes 4444 (알파 유지 — 422 HQ 는 알파를 못 담습니다)': 'ProRes 4444 (preserves alpha; 422 HQ cannot store alpha)', '띠     {0}': 'Bands  {0}', '알파는 보간해도 가장자리가 무릅니다 — 결과를 꼭 확인하세요': 'Alpha edges may soften during interpolation; review the result.', '모션 보간에 실패했습니다.': 'Motion interpolation failed.', '프레임 수를 알 수 없습니다.': 'Unable to determine the frame count.', '{0}장은 너무 많아 앞에서 {1}장만 뽑습니다. 전부 필요하면 Sequence 를 쓰세요.': '{0} frames exceeds the limit; extracting the first {1}. Use Sequence to extract all frames.', '뽑을 지점이 하나도 없습니다.': 'No extraction positions selected.', '빈 항목이 있습니다.': 'An entry is empty.', '제한범위': 'limited range', '전체범위': 'full range', '{0}bit {1} → 8bit 420 (배포용). 계조를 지키려면 누크용 마스터로': '{0}-bit {1} → 8-bit 420 (delivery). Choose a ProRes master to preserve depth.', '느리게': 'slower', '빠르게': 'faster', '새 원본 위치': 'New source positions', '원본 위치': 'Source positions', '프레임 재사용': 'Reused frames', '보간 위치': 'Interpolated positions', '메모를 적지 못했습니다: {0}': 'Unable to write notes: {0}', '보간 입력 프레임을 저장하지 못했습니다.': 'Unable to save interpolation input frames.', '보간 프레임 {0}을 만들지 못했습니다. 다시 시도해 주세요.': 'Unable to create interpolated frame {0}. Please try again.', '{0} 는 범위를 벗어나 {1} 로 맞췄습니다': '{0} is out of range; clamped to {1}', '프레임레이트를 몰라 시간으로 지정할 수 없습니다.': 'Cannot use time positions because the frame rate is unknown.', '무슨 뜻인지 모르겠습니다: {0}\n         first / middle / last, 프레임 번호, 25%, 1:23 같은 식으로 적으세요.': 'Unrecognized position: {0}\nUse first / middle / last, a frame number, 25%, or a time such as 1:23.', '백분율을 못 읽었습니다: {0}': 'Unable to parse percentage: {0}', '도움말': 'Help', 'extract (뽑기)': 'extract', 'retime (리타임)': 'retime', 'convert (변환)': 'convert', 'cuts (컷 분할)': 'cuts', 'rename (이름 바꾸기)': 'rename', 'compare (대조)': 'compare', 'Framing 대조': 'Framing compare', '나란히 놓고 봅니다. 전체 구도를 견줄 때': 'Side by side for comparing the full composition', '왼쪽만': 'A only', '오른쪽만': 'B only', '맞바꾸기': 'Swap', '한 프레임씩': 'Step one frame', '재생': 'Play', '멈춤': 'Pause', '되감기': 'Rewind', '맞춤': 'Fit', '확대': 'Zoom', '{0}  ·  화면은 {1} 에서': '{0} · UI at {1}', '약 80MB, 알아서 내려받습니다': 'Downloads automatically, about 80MB', 'Homebrew 로 설치합니다': 'Install using Homebrew', '파일 두 개가 필요합니다. A 와 B 를 같이 넣어 주세요 (둘을 한꺼번에 끌어다 놓거나, 폴더째 넣어도 됩니다).': 'Two files are required. Add both A and B together, or add a folder containing them.', '{0} 외 {1}개': '{0} and {1} more', 'Framing — 영상 작업 도구 모음': 'Framing — video tools', '완료 — 성공 {0}개 / 실패 {1}개': 'Done — {0} succeeded / {1} failed', '원본은 건드리지 않았습니다.': 'Source files were left unchanged.', '영상 작업 도구 — 뽑기 · 리타임 · 역재생 · 변환 · 대조. 인자 없이 열면 창이 뜹니다.': 'Video tools: extract, retime, reverse, convert and compare. Open without arguments to launch the UI.', '영상 · 이미지 · 시퀀스 폴더': 'Videos, images and sequence folders', 'audit: 정지 후보 최소 길이(초), 0.1~10': 'audit: minimum hold duration in seconds, 0.1–10', '출력 형식. exr · dpx 는 --at all 일 때': 'Output format; EXR and DPX require --at all', '뽑을 지점: 0,50%%,1:23,last … / all 이면 전부': 'Positions to extract: 0,50%%,1:23,last … / all for every frame', 'N프레임마다 한 장': 'Extract one image every N frames', '밝기 범위: auto=틀린 표기만 바로잡음 (기본)': 'Luma range: auto corrects incorrect tags only (default)', 'JPEG 품질 1~100': 'JPEG quality, 1–100', '목표 프레임 수': 'Target frame count', '목표 길이(초)': 'Target duration in seconds', '배속. 0.5 = 절반 속도, 2 = 두 배 빠르게': 'Speed: 0.5 = half speed, 2 = twice as fast', '없는 프레임 채우는 법': 'How to fill intermediate frames', '매트 라벨색 복원 끄기': 'Disable matte label color restoration', '손댄 자리를 메모장(.txt)으로도 결과 옆에 남김': 'Save a text note alongside the output', '리타임 · 역재생 결과를 어떤 모습으로 (기본 auto = 영상 하나)': 'Retiming / reverse output type (default auto = one video)', '어디로 보낼지': 'Output destination format', '이미지를 어떤 형식으로': 'Output image format', '시퀀스를 누크용 ProRes 로 묶음': 'Combine sequence into a ProRes editing master', '용량 줄이기 강도': 'Compression strength', '용량 줄이기에 HEVC 사용': 'Use HEVC for compression', '세로 해상도 상한': 'Maximum vertical resolution', '상자만 바꿔도 될 때도 다시 인코딩': 'Re-encode even when container-only conversion is possible', 'GIF를 한 번만 재생': 'Play the GIF only once', '예: 3.5,00:07.2': 'Example: 3.5,00:07.2', '새 이름 패턴. {n} 자리에 번호 (기본 장면_{n})': 'New name pattern; {n} is the number (default shot_{n})', '조용한_수달 처럼 무작위 두 단어 이름으로': 'Use a random two-word name', '도커식 단어: kr=한글 두 단어, en=도커 원본': 'Random-name language: kr=Korean words, en=English words', '이름에서 찾을 말 (쉼표로 여러 개)': 'Text to find in names (separate multiple terms with commas)', '찾은 자리에 넣을 말. 비우면 지웁니다': 'Replacement text; leave empty to remove matches', '앞에 붙일 말 (bg · ch · plate …)': 'Prefix (bg, ch, plate, etc.)', '뒤에 붙일 말': 'Suffix', '앞에서 잘라낼 글자 수': 'Number of characters to remove from the start', '뒤에서 잘라낼 글자 수': 'Number of characters to remove from the end', '어느 보기로 열지': 'Initial comparison view', '결과를 원본 옆에 파일로 남김': 'Save output beside the source', '결과를 모아둘 폴더': 'Output folder', '시퀀스를 묶을 때 프레임레이트': 'Frame rate for image sequences', '확인용 mp4 를 만들지 않음': 'Do not create a preview MP4', '같은 이름이 있으면 덮어씀': 'Overwrite existing files with the same name', '하위 폴더는 훑지 않음': 'Do not scan subfolders', 'ffmpeg 자동 내려받기 끔': 'Disable automatic FFmpeg download', '끝나도 엔터 대기 없음': 'Do not wait for Enter on completion', '방법이 하나도 없습니다': 'No methods available', '폴더를 열지 못했습니다 ({0}). 경로를 직접 열어 주세요.': 'Unable to open folder ({0}). Open the path manually.', '영상 · 이미지': 'Videos and images', '\nFraming 이 브라우저에서 열렸습니다: {0}': '\nFraming opened in your browser: {0}', "끝내려면 화면 오른쪽 위의 '종료' 를 누르거나 이 창에서 Ctrl+C.": 'To quit, use Quit in the top right or press Ctrl+C in this terminal.', '종료합니다.': 'Quitting.', '때': 'Time', '판': 'Build', '파이썬': 'Python', '열린 방식': 'Launch mode', '설정 폴더': 'Settings folder', '임시 자리': 'Temporary folder', '못 보낸 것': 'Undelivered events', '{0}개': '{0} files', '창': 'Window', '브라우저': 'Browser', '없음 (처음 쓸 때 받아 옵니다)': 'Not installed (downloaded on first use)', '인코더 ffmpeg': 'FFmpeg encoder', '\n{0}\n  Framing — 처음 실행 준비\n{1}': '\n{0}\n  Framing — first-run setup\n{1}', '  필요한 것 {0}가지를 알아서 챙깁니다. 처음 한 번만 걸립니다.': '  Installing {0} dependencies. This is needed only once.', '  인터넷 사정에 따라 몇 분 걸릴 수 있습니다. 그냥 두세요.\n': '  This may take a few minutes depending on your internet connection.\n', '        챙기는 중...': '        Installing...', '\n  다음을 챙기지 못했습니다:': '\n  Unable to install:', '\n  인터넷 연결이나 회사망 차단을 확인해 주세요.': '\n  Check your internet connection or company network restrictions.', '  직접 챙기려면:  {0} -m pip install opencv-python-headless pywebview': '  To install manually: {0} -m pip install opencv-python-headless pywebview', '이전 결과물로 보이는 {0}개는 건드리지 않았습니다': 'Skipped {0} files that appear to be previous outputs', '처리할 대상이 없습니다.': 'No inputs to process.', '{0} keyframes 영상.mp4   처럼 쓰거나,': 'Use {0} keyframes video.mp4, or', '그냥 더블클릭하면 창이 뜹니다.': 'double-click to open the app.', '처리할 대상을 찾지 못했습니다.': 'No supported inputs found.', '{0} 은 짝이 없어 건너뜁니다': 'Skipping {0}: no matching comparison partner', '줄었습니다': 'smaller', '늘었습니다': 'larger', '합계 용량  {0} → {1}  ({2}% {3})': 'Total size  {0} → {1} ({2}% {3})', '작업: ': 'Job: ', '\n  중단했습니다. 이미 끝난 파일은 남아 있습니다.': '\n  Stopped. Completed files have been kept.', '\n  엔터를 누르면 종료합니다...': '\n  Press Enter to close...', '그런 경로가 없습니다: {0}': 'Path does not exist: {0}', '끌어다 놓은 파일의 경로를 읽지 못했습니다. 왼쪽 "경로 직접 붙여넣기" 를 쓰세요.': 'Unable to read the dropped file path. Use Paste paths on the left.', '먼저 영상을 고르세요.': 'Select a video first.', '이미 실행 중입니다.': 'A job is already running.', '패키지 관리자로 설치합니다': 'Install using a package manager', '\n  준비 끝. 창을 띄웁니다.\n': '\n  Ready. Opening the app.\n', '  맥에서 권한 때문에 막히면 앞에 --user 를 붙여 보세요.': '  On macOS, try adding --user if installation is blocked by permissions.', '  엔터를 누르면 그래도 실행해 봅니다...': '  Press Enter to try starting anyway...', 'pywebview 를 설치하지 못했습니다': 'Unable to install pywebview', '영상 처리 opencv': 'OpenCV video processing', '약 60MB': 'About 60MB', '프로그램 창 pywebview': 'pywebview app window', '약 1MB': 'About 1MB', '\n[!] Framing 을 시작하지 못했습니다.': '\n[!] Framing failed to start.', '이름 변경 목록이 올바르지 않습니다.': 'Invalid rename list.', '바꿀 이름이 없습니다.': 'No names to change.', '미리 볼 파일을 찾을 수 없습니다.': 'Preview file not found.', '영상이나 이미지 파일을 고르세요.': 'Select a video or image file.', '바꾸지 못했습니다: {0}': 'Unable to rename: {0}', '먼저 대조를 실행하세요.': 'Run a comparison first.', '원본 파일을 찾지 못했습니다.': 'Source file not found.', '대조 내보내기 — 원본에서 저장 중': 'Comparison export — rendering from the source', '대조 저장 · {0}개': 'Saved {0} comparison outputs', '모르는 작업입니다: {0}': 'Unknown job: {0}', '서버': 'Server', 'Framing 이 시작하다 멈췄습니다.\n이 파일을 그대로 보내 주시면 원인을 찾을 수 있습니다.\n\n때  {0}\n판  {1}  ·  python {2}\n곳  {3}\n\n': 'Framing stopped during startup.\nShare this report to help diagnose the cause.\n\nTime  {0}\nBuild  {1} · Python {2}\nLocation  {3}\n\n', '\n    무슨 일이 있었는지 여기 적어 두었습니다:\n    {0}': '\n    Error details were saved to:\n    {0}', '    이 파일을 보내 주시면 원인을 찾을 수 있습니다.': '    Share this report to help diagnose the cause.', '다룰 수 없는 파일이라 건너뜁니다: {0}': 'Skipping unsupported file: {0}', '찾을 수 없습니다: {0}': 'Not found: {0}', '쌍': 'pairs', '개': 'files', '보기용 파일을 만들지 못했습니다.': 'Unable to create a preview file.', '건너뜀 — 나중에 필요할 때 다시 시도합니다': 'Skipped; will retry when needed', '결과 없음': 'No output', '없는 기능입니다: {0}': 'Unknown feature: {0}', '[알림] 창을 띄우지 못했습니다: {0}': '[Notice] Unable to open the app window: {0}', '[알림] 대신 기본 브라우저로 엽니다. 기능은 똑같습니다.': '[Notice] Opening the same app in your default browser.', '요청을 읽지 못했습니다.': 'Unable to read the request.', '모든 파일 (*.*)': 'All files (*.*)', '{0}: 중단했습니다.': '{0}: stopped.', '고르기': 'Choose', '색역': 'Primaries', '미리보기': 'Preview', '선택 파일 미리보기 (Space)': 'Preview selected file (Space)', '이 파일 하나만 리타임': 'Retime this file only', '개 샷': ' shots', '페이지': 'pages', '1~50 · 기본 12': '1–50 · default 12', '이미지 형식': 'Image format', '홀드 최소 길이(초)': 'Minimum hold duration (s)', '0.1~10초 · 이 길이 이상 이어지는 구간만 표시합니다.': '0.1–10 seconds · show only holds at least this long.', '검출 세부 설정': 'Detection settings', '홀드 검출 기준': 'Hold detection threshold', '엄격': 'Strict', '노이즈 허용': 'Allow noise', '압축 노이즈가 있는 영상은 노이즈 허용을 사용하세요. 느린 움직임도 후보가 될 수 있습니다.': 'Use Allow noise for compressed footage. Slow movement may also be flagged.', '파일 정보·컬러스페이스와 첫·중간·끝 프레임을 함께 확인합니다.': 'Review file details, color metadata, and first, middle and last frames together.', '프레임 검사 함께': 'Include frame inspection', '검은 프레임·정지·중복 후보도 찾습니다. 전체 프레임을 읽으므로 시간이 더 걸립니다.': 'Also find possible black, held and repeated frames. This reads every frame and takes longer.', '대조 창에서 현재 프레임·전체 영상·전체 PNG를 저장할 수 있습니다. 영역 선택과 A/B 개별 추출도 지원합니다.': 'Export the current frame, full video or all PNG frames from the comparison viewer. You can select a region or export A and B separately.', '작업을 시작하지 못했습니다.': 'Unable to start the job.', '프레임 배치 기록이며 홀드 검출 결과가 아닙니다.': 'Frame mapping record; this is not a hold detection result.', '보간 영상 보기': 'Preview retimed video', '원본과 대조': 'Compare with source', '전체 프레임 폴더': 'All frames folder', '보간된 프레임만': 'Interpolated frames only', '1~4  보기 방식 · A / B  한쪽만 · S  맞바꾸기\n': '1–4  view mode · A / B  one input · S  swap\n', '← →  경계 옮기기 (Shift 미세) · Space  재생·멈춤\n': '← →  move divider (Shift for fine steps) · Space  play/pause\n', '지금 상태를 한 장으로 모읍니다 — ': 'Collect the current state in one report: ', '무슨 파일을 넣었는지, 무엇을 하려 했는지, ': 'inputs, intended operation, and ', '어디서 걸렸는지.': 'where a problem occurred.', '영상 만들다 매번 하던 노가다가 지겨워서 만들었습니다. ': 'Built to simplify repetitive video production tasks. ', '잘 쓰이면 좋겠습니다.': 'We hope it helps your workflow.', '저장 형식': 'Export format', '현재 프레임 PNG': 'Current frame PNG', '전체 영상 MP4': 'Full video MP4', '전체 PNG 시퀀스': 'Full PNG sequence', '영상 + 전체 프레임': 'Video + all frames', '현재 화면 PNG': 'Current view PNG', '저장할 보기': 'Views to export', '지금 보기': 'Current view', '모든 보기 · A/B 포함': 'All views, including A/B', '저장 영역': 'Export region', '선택 영역 · 없으면 전체': 'Selected region, or full frame', '전체 화면': 'Full frame', '영역 선택으로 필요한 부분만 지정할 수 있습니다.': 'Select a region to export only the area you need.', '저장 폴더 열기': 'Open output folder', '저장 중단': 'Stop export', '중단 요청 중…': 'Requesting stop…', '추출 / 저장': 'Export / Save', '선택한 보기와 영역을 원본에서 저장합니다. 확대·이동과 화면 위 안내선은 저장하지 않습니다.': 'Renders the selected view and region from the source. Viewer zoom, pan and guides are not included.', 'A만': 'A only', 'B만': 'B only', '현재 프레임': 'Current frame', '저장 중…': 'Saving…', '대조 화면의 응답이 없습니다. 다시 실행해 주세요.': 'The comparison viewer is not responding. Run the comparison again.', '프레임을 확인하고 저장합니다…': 'Checking the frame and saving…', '대조를 저장하지 못했습니다.': 'Unable to export the comparison.', '저장 완료': 'Export complete', '정보 확인 · 시트 만들기 · 프레임 검사': 'File info · contact sheets · frame inspection', '샷 정보 · 콘택트시트 · 대조': 'Shot info · contact sheets · compare', '컷별 MP4와 시작·끝 프레임 목록을 새 폴더에 저장합니다.': 'Save each cut as an MP4 with a start/end frame manifest in a new folder.', '파일 미리보기': 'File preview', '미리보기를 불러오는 중…': 'Loading preview…', '파일 정보': 'File info', '← 이전': '← Previous', '이전 파일 또는 후보': 'Previous file or candidate', '이전 (←)': 'Previous (←)', '다음 →': 'Next →', '다음 파일 또는 후보': 'Next file or candidate', '다음 (→)': 'Next (→)', '← → 이전·다음 · Esc 닫기': '← → previous/next · Esc close', '미리보기 실패': 'Preview failed', '이 영상은 브라우저에서 재생하지 못했습니다.': 'The browser could not play this video.', '구간 시작': 'Start of range', '구간 끝': 'End of range', '이미지를 읽지 못했습니다.': 'Unable to read the image.', '다시 시도': 'Retry', '프레임레이트': 'Frame rate', '길이': 'Duration', '코덱': 'Codec', '오디오 코덱': 'Audio codec', '파일 크기': 'File size', '작업 저장 실패': 'Unable to save project', '작업 파일은 2MB까지 열 수 있습니다.': 'Project files must be 2MB or smaller.', '작업 열기 실패': 'Unable to open project', '미리보기를 만들지 못했습니다.': 'Unable to create preview.', '예: final_': 'Example: final_', '예: _v02': 'Example: _v02', '추가 편집 · 필요할 때 펼치기': 'More editing options', '목록 정렬': 'Sort list', '선택 목록 제거': 'Remove selected', '변경 전 → 변경 후': 'Before → After', '이름 미리보기 필터': 'Filter rename preview', '전체 보기': 'Show all', '확인 필요': 'Needs review', '개 선택 파일이 필터로 숨겨져 있습니다. 선택 상태와 변경 대상은 유지됩니다.': ' selected files are hidden by the filter. They remain selected for renaming.', '이름을 바꾸지 못했습니다.': 'Unable to rename files.', '샷': 'Shot', '파일 정보 · 컬러스페이스': 'File info · color metadata', '컬러스페이스 · 미지정': 'Color metadata · unspecified', '프레임 검사': 'Frame inspection', '홀드 최소 길이': 'Minimum hold duration', '홀드': 'Hold', '완전히 동일한 인접 프레임 구간': 'Ranges with identical adjacent frames', '홀드 후보': 'Possible holds', '합계': 'Total', '전체': 'All', '동일 프레임': 'Identical frames', '검은 프레임': 'Black frames', '누락·기타': 'Missing / other', '검사 후보 필터': 'Filter review candidates', '확인 →': 'Review →', '컬러스페이스 정보': 'Color metadata', '파일에 기록된 값': 'Values recorded in the file', '색역 / Primaries': 'Primaries', '전달 함수 / Transfer': 'Transfer function', '매트릭스 / Space': 'Matrix / color space', '밝기 범위 / Range': 'Luma range', '픽셀 형식': 'Pixel format', '\n    어디를 뽑을지만 정하면 됩니다.\n      첫 · 중간 · 마지막 (기본) / 균등하게 N장 / N프레임마다 / 직접 찍기 / 전부\n\n    직접 찍기 (--at, 쉼표로 여러 개)\n      first middle last · 프레임 번호(0부터) · -1(끝에서부터) · 25% · 1:23 · 3s\n      all 이라고 쓰면 전부 — 시퀀스로 풉니다\n\n    몇 장만 뽑을 때는 PNG · JPG(4:4:4) 로, 전부 풀 때는 PNG · JPG · EXR · DPX 로.\n    무손실 시퀀스는 원본 mp4 의 수십~수백 배가 됩니다. 1분 1080p ≈ 5~15GB.\n\n    색이 달라 보이지 않게 하는 일 세 가지는 몇 장이든 전부든 똑같이 합니다.\n      · JPG 는 색을 줄이지 않는 4:4:4 로\n      · 10bit · 알파 소스는 원본 깊이 그대로 (16bit PNG, 알파 유지)\n      · 밝기 범위 표기가 틀린 영상은 읽는 단계에서 바로잡음 (--levels auto|full|keep)\n\n    화면과 여전히 다르면 보는 프로그램의 감마 차이입니다 (영상 2.4 vs sRGB 2.2).\n    그건 파일을 고쳐서 맞추면 안 됩니다 — 다른 모든 도구와 어긋나게 됩니다.': 'Choose where to extract frames: first, middle, last, a frame number, a percentage or a timestamp. Use All frames to save every decoded frame as a numbered image sequence. PNG is lossless; JPG is smaller. EXR and DPX are available for full sequences. Source files stay unchanged.', '어디를 뽑을지만 정하면 됩니다.\n      첫 · 중간 · 마지막 (기본) / 균등하게 N장 / N프레임마다 / 직접 찍기 / 전부\n\n    직접 찍기 (--at, 쉼표로 여러 개)\n      first middle last · 프레임 번호(0부터) · -1(끝에서부터) · 25% · 1:23 · 3s\n      all 이라고 쓰면 전부 — 시퀀스로 풉니다\n\n    몇 장만 뽑을 때는 PNG · JPG(4:4:4) 로, 전부 풀 때는 PNG · JPG · EXR · DPX 로.\n    무손실 시퀀스는 원본 mp4 의 수십~수백 배가 됩니다. 1분 1080p ≈ 5~15GB.\n\n    색이 달라 보이지 않게 하는 일 세 가지는 몇 장이든 전부든 똑같이 합니다.\n      · JPG 는 색을 줄이지 않는 4:4:4 로\n      · 10bit · 알파 소스는 원본 깊이 그대로 (16bit PNG, 알파 유지)\n      · 밝기 범위 표기가 틀린 영상은 읽는 단계에서 바로잡음 (--levels auto|full|keep)\n\n    화면과 여전히 다르면 보는 프로그램의 감마 차이입니다 (영상 2.4 vs sRGB 2.2).\n    그건 파일을 고쳐서 맞추면 안 됩니다 — 다른 모든 도구와 어긋나게 됩니다.': 'Choose where to extract frames: first, middle, last, a frame number, a percentage or a timestamp. Use All frames to save every decoded frame as a numbered image sequence. PNG is lossless; JPG is smaller. EXR and DPX are available for full sequences. Source files stay unchanged.', '\n    무엇으로 받을지 고를 수 있습니다  (--result)\n      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov\n      mp4    어디서나 열리는 h264 하나\n      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다\n      seq    PNG 시퀀스만. 영상은 만들지 않습니다\n      both   시퀀스 + 확인용 영상 둘 다\n\n    auto 가 소스를 보고 정하는 기준\n      8bit 4:2:0        h264 mp4 (소리도 같이 뒤집습니다)\n      10bit · 4:2:2     ProRes 422 HQ mov — 계조를 안 깎으려고\n      알파가 있으면     ProRes 4444 mov — 투명도를 지키려고\n    무엇이 들어오든 h264 로 내보내면 순서 바꾸려다 원본 계조와 알파를 죽이게 됩니다.\n\n    시퀀스 폴더를 넣고 seq · both 로 받으면 파일을 역순으로 복사만 합니다 —\n    픽셀은 손대지 않습니다.\n\n    소스의 색 규격(bt709 / bt601 · 제한범위 / 전체범위)은 결과에 그대로 찍어 둡니다.\n    안 찍어 두면 다음 도구가 추측하고, 그 추측이 어긋나는 게 색이 밀리는 진짜 원인입니다.\n\n\n    30초가 넘는 영상은 ffmpeg 의 reverse 필터가 전체를 램에 올려서 위험하므로,\n    프레임을 경유하는 방식으로 자동 전환합니다. 이때 소리는 빠집니다.': 'Reverse a video or an image sequence. Auto saves a video; Sequence saves numbered frames. Sequence + MP4 saves both. Audio is omitted when processing through intermediate frames. Review the result before using it in an edit.', '무엇으로 받을지 고를 수 있습니다  (--result)\n      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov\n      mp4    어디서나 열리는 h264 하나\n      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다\n      seq    PNG 시퀀스만. 영상은 만들지 않습니다\n      both   시퀀스 + 확인용 영상 둘 다\n\n    auto 가 소스를 보고 정하는 기준\n      8bit 4:2:0        h264 mp4 (소리도 같이 뒤집습니다)\n      10bit · 4:2:2     ProRes 422 HQ mov — 계조를 안 깎으려고\n      알파가 있으면     ProRes 4444 mov — 투명도를 지키려고\n    무엇이 들어오든 h264 로 내보내면 순서 바꾸려다 원본 계조와 알파를 죽이게 됩니다.\n\n    시퀀스 폴더를 넣고 seq · both 로 받으면 파일을 역순으로 복사만 합니다 —\n    픽셀은 손대지 않습니다.\n\n    소스의 색 규격(bt709 / bt601 · 제한범위 / 전체범위)은 결과에 그대로 찍어 둡니다.\n    안 찍어 두면 다음 도구가 추측하고, 그 추측이 어긋나는 게 색이 밀리는 진짜 원인입니다.\n\n\n    30초가 넘는 영상은 ffmpeg 의 reverse 필터가 전체를 램에 올려서 위험하므로,\n    프레임을 경유하는 방식으로 자동 전환합니다. 이때 소리는 빠집니다.': 'Reverse a video or an image sequence. Auto saves a video; Sequence saves numbered frames. Sequence + MP4 saves both. Audio is omitted when processing through intermediate frames. Review the result before using it in an edit.', '\n    길이를 말하는 방법이 셋입니다. 편한 걸 쓰세요. 어느 쪽이든 같은 곳으로 갑니다.\n      --frames 264     결과를 264프레임으로\n      --seconds 11     결과를 11초로 (fps 를 곱해서 계산)\n      --speed 0.5      절반 속도 = 두 배 길이. 2 면 두 배 빠르게\n\n    없는 프레임을 무엇으로 채울까  (--retime-mode)\n      auto     소재를 보고 알아서 고릅니다 (기본). 무엇을 골랐는지 매번 알려줍니다\n      dup      가장 가까운 원본 프레임을 그대로. 픽셀 연산이 0 이라 매트가 100% 남습니다\n      motion   움직임을 추정해 없던 그림을 그립니다. 제일 부드럽고, 가끔 찢어집니다\n               (ffmpeg 이 없으면 앞뒤를 섞는 방식으로 알아서 물러섭니다)\n\n    auto 가 고르는 기준\n      매트 · 뎁스 패스면          → dup.    라벨색을 뭉개면 그 픽셀의 뜻이 사라집니다\n      실사인데 느리게 (늘리기)     → motion. 슬로모션은 없던 프레임이 있어야 매끄럽습니다\n      실사인데 빠르게 (줄이기)     → dup.    버리는 작업이라 새로 그릴 게 없습니다\n\n    매트 패스를 알아서 알아봅니다\n      매트는 몇 개의 라벨색으로만 칠해져 색상이 좁게 몰립니다 (실측 83~99%).\n      실사·페인팅은 넓게 퍼집니다 (21~28%). 이 차이로 갈라냅니다.\n      매트로 판별되면 쓰인 라벨색을 찾아 보여주고, 보간 후 섞인 픽셀을\n      가장 가까운 라벨로 되돌립니다. 명도는 손대지 않습니다 — 그건 뎁스니까요.\n      실사인데 매트로 잘못 본 것 같으면 --no-snap 으로 색 복원을 끄세요.\n\n    무엇으로 받을지 고를 수 있습니다  (--result)\n      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov — 영상 하나만 나옵니다\n      mp4    어디서나 열리는 h264 하나\n      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다\n      seq    PNG 시퀀스만. 영상은 만들지 않습니다\n      both   시퀀스 + 확인용 영상 둘 다\n      bundle 영상·전체 프레임·새로 보간된 프레임·기록을 새 폴더에 저장\n    새 UI 리타임 작업은 bundle이 기본입니다. CLI는 auto를 유지합니다.\n\n    알아둘 것\n      · 24fps 의 배수로 맞추면 편집에서 어긋나지 않습니다. 아니면 알려줍니다.\n      · motion 은 결과를 꼭 눈으로 보세요. 빠른 움직임 · 가림 · 모션블러에 약합니다.\n      · 소리는 빠집니다. 길이가 바뀌면 소리도 같이 늘려야 하는데,\n        그건 리타임이 아니라 다른 작업입니다.\n      · --note 를 주면 어디를 손댔는지 메모장(.txt) 한 장을 결과 옆에 놓습니다.\n      · 영상 소스의 색 규격(bt709 / 제한범위 등)은 결과 mp4 에 그대로 찍어 둡니다.': 'Change duration or speed while keeping the output FPS. Nearest frame reuses original pixels. Blend mixes neighboring images and may cause ghosting. Motion estimates movement with FFmpeg minterpolate (MCI, bidirectional estimation and adaptive overlapping blocks); it is not a neural AI model. It may distort fast motion, occlusions or scene cuts. Auto chooses nearest frame for mattes or shortening, and motion for slowing live action; without FFmpeg it falls back to blending.\n\nExample: three frames A–B–C retimed to five become A–intermediate–B–intermediate–C. The first and last frames are preserved. Other output positions do not always coincide with an original frame. Mapping marks describe sampling, not detected holds.\n\nChoose Video + frame folders to save the movie, every PNG frame, interpolated PNG frames and a mapping manifest together. Retimed exports do not contain source audio. 16-bit and grayscale inputs use Blend instead of Motion to preserve depth and channel layout; the actual method is recorded in the result.', '길이를 말하는 방법이 셋입니다. 편한 걸 쓰세요. 어느 쪽이든 같은 곳으로 갑니다.\n      --frames 264     결과를 264프레임으로\n      --seconds 11     결과를 11초로 (fps 를 곱해서 계산)\n      --speed 0.5      절반 속도 = 두 배 길이. 2 면 두 배 빠르게\n\n    없는 프레임을 무엇으로 채울까  (--retime-mode)\n      auto     소재를 보고 알아서 고릅니다 (기본). 무엇을 골랐는지 매번 알려줍니다\n      dup      가장 가까운 원본 프레임을 그대로. 픽셀 연산이 0 이라 매트가 100% 남습니다\n      motion   움직임을 추정해 없던 그림을 그립니다. 제일 부드럽고, 가끔 찢어집니다\n               (ffmpeg 이 없으면 앞뒤를 섞는 방식으로 알아서 물러섭니다)\n\n    auto 가 고르는 기준\n      매트 · 뎁스 패스면          → dup.    라벨색을 뭉개면 그 픽셀의 뜻이 사라집니다\n      실사인데 느리게 (늘리기)     → motion. 슬로모션은 없던 프레임이 있어야 매끄럽습니다\n      실사인데 빠르게 (줄이기)     → dup.    버리는 작업이라 새로 그릴 게 없습니다\n\n    매트 패스를 알아서 알아봅니다\n      매트는 몇 개의 라벨색으로만 칠해져 색상이 좁게 몰립니다 (실측 83~99%).\n      실사·페인팅은 넓게 퍼집니다 (21~28%). 이 차이로 갈라냅니다.\n      매트로 판별되면 쓰인 라벨색을 찾아 보여주고, 보간 후 섞인 픽셀을\n      가장 가까운 라벨로 되돌립니다. 명도는 손대지 않습니다 — 그건 뎁스니까요.\n      실사인데 매트로 잘못 본 것 같으면 --no-snap 으로 색 복원을 끄세요.\n\n    무엇으로 받을지 고를 수 있습니다  (--result)\n      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov — 영상 하나만 나옵니다\n      mp4    어디서나 열리는 h264 하나\n      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다\n      seq    PNG 시퀀스만. 영상은 만들지 않습니다\n      both   시퀀스 + 확인용 영상 둘 다\n      bundle 영상·전체 프레임·새로 보간된 프레임·기록을 새 폴더에 저장\n    새 UI 리타임 작업은 bundle이 기본입니다. CLI는 auto를 유지합니다.\n\n    알아둘 것\n      · 24fps 의 배수로 맞추면 편집에서 어긋나지 않습니다. 아니면 알려줍니다.\n      · motion 은 결과를 꼭 눈으로 보세요. 빠른 움직임 · 가림 · 모션블러에 약합니다.\n      · 소리는 빠집니다. 길이가 바뀌면 소리도 같이 늘려야 하는데,\n        그건 리타임이 아니라 다른 작업입니다.\n      · --note 를 주면 어디를 손댔는지 메모장(.txt) 한 장을 결과 옆에 놓습니다.\n      · 영상 소스의 색 규격(bt709 / 제한범위 등)은 결과 mp4 에 그대로 찍어 둡니다.': 'Change duration or speed while keeping the output FPS. Nearest frame reuses original pixels. Blend mixes neighboring images and may cause ghosting. Motion estimates movement with FFmpeg minterpolate (MCI, bidirectional estimation and adaptive overlapping blocks); it is not a neural AI model. It may distort fast motion, occlusions or scene cuts. Auto chooses nearest frame for mattes or shortening, and motion for slowing live action; without FFmpeg it falls back to blending.\n\nExample: three frames A–B–C retimed to five become A–intermediate–B–intermediate–C. The first and last frames are preserved. Other output positions do not always coincide with an original frame. Mapping marks describe sampling, not detected holds.\n\nChoose Video + frame folders to save the movie, every PNG frame, interpolated PNG frames and a mapping manifest together. Retimed exports do not contain source audio. 16-bit and grayscale inputs use Blend instead of Motion to preserve depth and channel layout; the actual method is recorded in the result.', '\n    영상 · 이미지 · 번호 붙은 시퀀스 폴더를 다 받습니다.\n    넣은 것과 안 맞는 목적지를 골라 두면 맞는 쪽으로 알아서 옮기고 알려줍니다.\n\n    이미지를 넣으면\n      PNG · JPG · WEBP 로 바꿉니다. --max-height 로 크기도 같이 줄일 수 있습니다.\n      (재보니 같은 그림이 PNG 153KB → JPG 24KB → WEBP 18KB 였습니다.)\n\n    시퀀스 폴더를 넣으면\n      영상 한 편으로 묶습니다. Sequence 의 반대 방향입니다.\n      --prores-movie 를 붙이면 누크용 ProRes 로 묶고, 알파가 있으면 4444 로 갑니다.\n      번호가 1부터 시작하지 않거나 중간이 비어 있어도 됩니다 —\n      패턴을 짜 맞추지 않고 파일 목록을 그대로 먹입니다.\n\n    영상을 넣으면, 어디로 보낼지 하나만 고르면 됩니다.\n\n      MP4 (배포·업로드)\n        가장 잘 열리는 형식입니다. 안에 든 코덱이 mp4 가 받는 것(h264 / hevc /\n        mpeg4 / av1)이면 다시 인코딩하지 않고 상자만 갈아끼웁니다. 몇 초 걸리고\n        화질 손실은 없습니다. 영상 데이터가 비트 단위로 그대로 옮겨갑니다.\n\n      MOV (상자만 교체)\n        확장자만 .mov 가 됩니다. h264 짜리를 .mov 로 바꿔도 안에 든 건 여전히\n        h264 입니다. 화질이 좋아지지도, 편집이 수월해지지도 않습니다.\n        편집용 마스터가 필요하면 아래를 쓰세요.\n\n      누크용 마스터 (ProRes)\n        Nuke · After Effects · Resolve 가 스크러빙할 때 버벅이지 않는 코덱입니다.\n          · 용량이 10~30배 커집니다. 고장이 아니라 원래 그렇습니다.\n          · 스크러빙 속도와 세대 손실 내성을 용량으로 사는 코덱입니다.\n          · 배포·업로드용으로 쓰면 안 됩니다.\n          · h264 를 ProRes 로 바꿔도 없던 화질이 생기지는 않습니다.\n            이미 뭉개진 8bit 계조를 10bit 상자에 옮겨 담을 뿐입니다.\n            다시 인코딩할 거라면 원본 렌더에서 바로 뽑는 게 맞습니다.\n          · 알파가 있으면 알아서 ProRes 4444 로 갑니다. 422 HQ 는 알파를 못 담습니다.\n\n      용량 줄이기\n        반드시 다시 인코딩합니다. 되돌릴 수 없는 화질 손실이 생깁니다.\n        원본은 그대로 두고 새 파일을 만드니, 마음에 안 들면 지우면 됩니다.\n        이미 잘 압축된 파일은 오히려 커질 수 있습니다. 그러면 커졌다고 알려줍니다.\n\n        가장 크게 줄이는 건 화질 단계가 아니라 해상도입니다. 4K → 1080p 면\n        픽셀 수가 1/4 이 됩니다. 화질을 만지기 전에 "이걸 정말 4K 로 봐야 하나"를\n        먼저 따져보세요.\n\n        h265 는 같은 화질에 20~30% 더 작지만 인코딩이 2~4배 느리고,\n        옛 기기나 일부 편집 프로그램에서 안 열립니다.\n\n    공통\n      · 알파 채널은 mp4 에 못 들어갑니다. 투명도가 필요하면 누크용 마스터나\n        PNG 시퀀스를 쓰세요.\n      · 소스의 색 규격(bt709 / 제한범위 등)을 결과에 그대로 적어 둡니다.\n        안 적어 두면 다음 도구가 추측하고, 그 추측이 어긋나면 색이 밀립니다.\n      · 10bit · 4:2:2 소스는 배포용으로 갈 때 8bit 로 내려갑니다. 내려간다고\n        알려줍니다. 계조를 지키려면 누크용 마스터로 가세요.\n': 'Image sequence → video: put numbered images in one folder (for example shot_0001.png, shot_0002.png), add that folder, choose Sequence → video, set Sequence FPS and run. Images are naturally sorted by filename. At 24 FPS, 240 images make 10 seconds. The images must have matching dimensions, channels and bit depth. This plays existing images in order; it does not invent movement from a single photograph.\n\nChoose GIF for an animated preview with FPS, width and loop controls. Video → image sequence saves every decoded frame in a new folder. MP4 is for sharing; ProRes is for editing and alpha. MOV can repackage compatible streams without re-encoding. Compression re-encodes and can lose quality.', '영상 · 이미지 · 번호 붙은 시퀀스 폴더를 다 받습니다.\n    넣은 것과 안 맞는 목적지를 골라 두면 맞는 쪽으로 알아서 옮기고 알려줍니다.\n\n    이미지를 넣으면\n      PNG · JPG · WEBP 로 바꿉니다. --max-height 로 크기도 같이 줄일 수 있습니다.\n      (재보니 같은 그림이 PNG 153KB → JPG 24KB → WEBP 18KB 였습니다.)\n\n    시퀀스 폴더를 넣으면\n      영상 한 편으로 묶습니다. Sequence 의 반대 방향입니다.\n      --prores-movie 를 붙이면 누크용 ProRes 로 묶고, 알파가 있으면 4444 로 갑니다.\n      번호가 1부터 시작하지 않거나 중간이 비어 있어도 됩니다 —\n      패턴을 짜 맞추지 않고 파일 목록을 그대로 먹입니다.\n\n    영상을 넣으면, 어디로 보낼지 하나만 고르면 됩니다.\n\n      MP4 (배포·업로드)\n        가장 잘 열리는 형식입니다. 안에 든 코덱이 mp4 가 받는 것(h264 / hevc /\n        mpeg4 / av1)이면 다시 인코딩하지 않고 상자만 갈아끼웁니다. 몇 초 걸리고\n        화질 손실은 없습니다. 영상 데이터가 비트 단위로 그대로 옮겨갑니다.\n\n      MOV (상자만 교체)\n        확장자만 .mov 가 됩니다. h264 짜리를 .mov 로 바꿔도 안에 든 건 여전히\n        h264 입니다. 화질이 좋아지지도, 편집이 수월해지지도 않습니다.\n        편집용 마스터가 필요하면 아래를 쓰세요.\n\n      누크용 마스터 (ProRes)\n        Nuke · After Effects · Resolve 가 스크러빙할 때 버벅이지 않는 코덱입니다.\n          · 용량이 10~30배 커집니다. 고장이 아니라 원래 그렇습니다.\n          · 스크러빙 속도와 세대 손실 내성을 용량으로 사는 코덱입니다.\n          · 배포·업로드용으로 쓰면 안 됩니다.\n          · h264 를 ProRes 로 바꿔도 없던 화질이 생기지는 않습니다.\n            이미 뭉개진 8bit 계조를 10bit 상자에 옮겨 담을 뿐입니다.\n            다시 인코딩할 거라면 원본 렌더에서 바로 뽑는 게 맞습니다.\n          · 알파가 있으면 알아서 ProRes 4444 로 갑니다. 422 HQ 는 알파를 못 담습니다.\n\n      용량 줄이기\n        반드시 다시 인코딩합니다. 되돌릴 수 없는 화질 손실이 생깁니다.\n        원본은 그대로 두고 새 파일을 만드니, 마음에 안 들면 지우면 됩니다.\n        이미 잘 압축된 파일은 오히려 커질 수 있습니다. 그러면 커졌다고 알려줍니다.\n\n        가장 크게 줄이는 건 화질 단계가 아니라 해상도입니다. 4K → 1080p 면\n        픽셀 수가 1/4 이 됩니다. 화질을 만지기 전에 "이걸 정말 4K 로 봐야 하나"를\n        먼저 따져보세요.\n\n        h265 는 같은 화질에 20~30% 더 작지만 인코딩이 2~4배 느리고,\n        옛 기기나 일부 편집 프로그램에서 안 열립니다.\n\n    공통\n      · 알파 채널은 mp4 에 못 들어갑니다. 투명도가 필요하면 누크용 마스터나\n        PNG 시퀀스를 쓰세요.\n      · 소스의 색 규격(bt709 / 제한범위 등)을 결과에 그대로 적어 둡니다.\n        안 적어 두면 다음 도구가 추측하고, 그 추측이 어긋나면 색이 밀립니다.\n      · 10bit · 4:2:2 소스는 배포용으로 갈 때 8bit 로 내려갑니다. 내려간다고\n        알려줍니다. 계조를 지키려면 누크용 마스터로 가세요.': 'Image sequence → video: put numbered images in one folder (for example shot_0001.png, shot_0002.png), add that folder, choose Sequence → video, set Sequence FPS and run. Images are naturally sorted by filename. At 24 FPS, 240 images make 10 seconds. The images must have matching dimensions, channels and bit depth. This plays existing images in order; it does not invent movement from a single photograph.\n\nChoose GIF for an animated preview with FPS, width and loop controls. Video → image sequence saves every decoded frame in a new folder. MP4 is for sharing; ProRes is for editing and alpha. MOV can repackage compatible streams without re-encoding. Compression re-encodes and can lose quality.', '\n    파일 두 개를 넣으세요. 먼저 넣은 것이 A, 나중이 B 입니다.\n    (폴더를 넣으면 그 안의 앞의 두 개를 씁니다.)\n\n    실행하면 대조 창이 바로 열립니다. 인코딩을 기다릴 것도, 파일이 쌓일 것도 없습니다.\n\n    창 안에서 네 가지를 오갈 수 있습니다  (숫자키 1~4)\n      1 반반 바   경계선을 잡고 좌우로 끌면 A 와 B 가 갈립니다\n      2 겹치기    포개서 봅니다. 섞기 손잡이로 0~100% 조절 — 위치가 밀렸는지 볼 때\n      3 번갈아    빠르게 교대합니다. 미세한 차이는 이게 제일 잘 보입니다\n      4 나란히    왼쪽 A, 오른쪽 B. 여기 안에 "둘 다 · A만 · B만" 이 같이 있습니다\n\n      바 끌기 · ← → 로 미세 조정 (Shift 를 누르면 더 잘게)\n      A · B 키로 한쪽만 · Space 재생·멈춤\n      영상이면 두 쪽이 같은 지점을 보도록 재생 위치를 계속 맞춰 둡니다.\n\n    --view 로 어느 보기로 열지 정합니다 (wipe · overlay · flicker · side).\n\n    남는 것 없이 봅니다\n      대조는 결과물이 아니라 확인하는 행위입니다. 볼 때마다 원본 폴더에 파일이 쌓이면\n      정작 필요한 파일이 묻힙니다. 그래서 임시 자리에 만들고, Framing 을 닫을 때 지웁니다.\n      남겨서 남에게 보내야 하면 --keep 을 붙이세요. 그때는 원본 옆에 폴더를 만들고\n      고른 보기로 구운 대조 영상도 같이 넣어 줍니다.\n\n    브라우저가 못 여는 형식이면 알아서 바꿔 놓고 봅니다\n      창의 재생기가 실제로 여는 건 mp4(h264 8bit) 와 webm 정도입니다.\n      ProRes · DNxHD · HEVC · 10bit · .mov 상자는 아무 말 없이 까만 화면만 나옵니다.\n      그래서 미리 알아보고, 못 여는 형식이면 볼 수 있는 한 벌을 만들어 그걸 겁니다.\n      창 위쪽에 "보기용으로 변환함" 이라고 붙습니다 — 원본 픽셀이 아니라는 뜻입니다.\n      원본은 손대지 않고, --keep 으로 굽는 대조 영상도 원본에서 만듭니다.\n      1440p 가 넘으면 보기용만 그 크기로 줄입니다. 줄였다고 알려줍니다.\n\n    알아둘 것\n      · 해상도가 다르면 A 기준 캔버스에 비율을 유지해 맞추고 검은 여백을 넣습니다.\n      · 길이가 다르면 짧은 쪽에서 끝납니다.\n      · 색 규격이 서로 다르면 그것부터 알려줍니다. 그게 차이의 원인일 때가 많습니다.': 'Add two videos or two images as A and B. Use Wipe, Overlay, Flicker or Side by side, or view one input. The timeline controls both inputs; comparison ends at the shorter duration. B is fitted to the A canvas with its aspect ratio preserved.\n\nSelect a region by dragging or by entering pixel coordinates. Region only zooms the comparison to that crop. Export the current PNG, full video, full PNG sequence or both video and frames. Export the current view or all views including A/B. Exports render from the source and omit the viewer guides, zoom and pan.', '파일 두 개를 넣으세요. 먼저 넣은 것이 A, 나중이 B 입니다.\n    (폴더를 넣으면 그 안의 앞의 두 개를 씁니다.)\n\n    실행하면 대조 창이 바로 열립니다. 인코딩을 기다릴 것도, 파일이 쌓일 것도 없습니다.\n\n    창 안에서 네 가지를 오갈 수 있습니다  (숫자키 1~4)\n      1 반반 바   경계선을 잡고 좌우로 끌면 A 와 B 가 갈립니다\n      2 겹치기    포개서 봅니다. 섞기 손잡이로 0~100% 조절 — 위치가 밀렸는지 볼 때\n      3 번갈아    빠르게 교대합니다. 미세한 차이는 이게 제일 잘 보입니다\n      4 나란히    왼쪽 A, 오른쪽 B. 여기 안에 "둘 다 · A만 · B만" 이 같이 있습니다\n\n      바 끌기 · ← → 로 미세 조정 (Shift 를 누르면 더 잘게)\n      A · B 키로 한쪽만 · Space 재생·멈춤\n      영상이면 두 쪽이 같은 지점을 보도록 재생 위치를 계속 맞춰 둡니다.\n\n    --view 로 어느 보기로 열지 정합니다 (wipe · overlay · flicker · side).\n\n    남는 것 없이 봅니다\n      대조는 결과물이 아니라 확인하는 행위입니다. 볼 때마다 원본 폴더에 파일이 쌓이면\n      정작 필요한 파일이 묻힙니다. 그래서 임시 자리에 만들고, Framing 을 닫을 때 지웁니다.\n      남겨서 남에게 보내야 하면 --keep 을 붙이세요. 그때는 원본 옆에 폴더를 만들고\n      고른 보기로 구운 대조 영상도 같이 넣어 줍니다.\n\n    브라우저가 못 여는 형식이면 알아서 바꿔 놓고 봅니다\n      창의 재생기가 실제로 여는 건 mp4(h264 8bit) 와 webm 정도입니다.\n      ProRes · DNxHD · HEVC · 10bit · .mov 상자는 아무 말 없이 까만 화면만 나옵니다.\n      그래서 미리 알아보고, 못 여는 형식이면 볼 수 있는 한 벌을 만들어 그걸 겁니다.\n      창 위쪽에 "보기용으로 변환함" 이라고 붙습니다 — 원본 픽셀이 아니라는 뜻입니다.\n      원본은 손대지 않고, --keep 으로 굽는 대조 영상도 원본에서 만듭니다.\n      1440p 가 넘으면 보기용만 그 크기로 줄입니다. 줄였다고 알려줍니다.\n\n    알아둘 것\n      · 해상도가 다르면 A 기준 캔버스에 비율을 유지해 맞추고 검은 여백을 넣습니다.\n      · 길이가 다르면 짧은 쪽에서 끝납니다.\n      · 색 규격이 서로 다르면 그것부터 알려줍니다. 그게 차이의 원인일 때가 많습니다.': 'Add two videos or two images as A and B. Use Wipe, Overlay, Flicker or Side by side, or view one input. The timeline controls both inputs; comparison ends at the shorter duration. B is fitted to the A canvas with its aspect ratio preserved.\n\nSelect a region by dragging or by entering pixel coordinates. Region only zooms the comparison to that crop. Export the current PNG, full video, full PNG sequence or both video and frames. Export the current view or all views including A/B. Exports render from the source and omit the viewer guides, zoom and pan.', '영역 선택': 'Select region', '영역만 보기': 'Region only', '영역 해제': 'Clear region', '화면 맞춤': 'Fit to window', '경계': 'Divider', '대조 경계': 'Comparison divider', '중앙': 'Center', '섞기': 'Blend', 'B 혼합 비율': 'B blend amount', 'A 기준 픽셀 · 화면에서 끌어 선택': 'Pixels on the A canvas · drag to select', '너비': 'Width', '높이': 'Height', '대조 화면': 'Comparison viewer', '재생 · 멈춤 (Space)': 'Play / pause (Space)', '이전 프레임': 'Previous frame', '다음 프레임': 'Next frame', '재생 위치': 'Playback position', '바를 끌거나 ': 'Drag the divider or use ', ' 로 경계를 옮깁니다 (': ' to move it (', ' 로 미세 조정)': ' for fine steps)', ' 재생 · 멈춤': ' play / pause', ' 보기 · ': ' views · ', ' 한쪽만 · ': ' one input · ', ' 맞바꾸기': ' swap', '휠 확대 · 오른쪽 끌어 이동 · ': 'Wheel to zoom · right-drag to pan · ', ' 한 프레임씩 · ': ' frame step · ', ' 닫기': ' close', '프레임을 아직 읽는 중입니다. 잠시 후 다시 저장하세요.': 'The frame is still loading. Wait a moment and export again.', '{0} — 대상 {1}개': '{0} — {1} inputs', '{0} — 대상 {1}쌍': '{0} — {1} pairs', '중단됨 — 끝난 것 {0}개 / 실패 {1}개': 'Stopped — {0} completed / {1} failed', '보간은 어떻게 하나요?': 'How does interpolation work?', '시퀀스를 영상으로 만드는 방법': 'How to turn a sequence into a video', '번호 순서의 이미지를 한 폴더에 넣고 그 폴더를 추가하세요. FPS는 1초에 재생할 이미지 수입니다. 240장 ÷ 24 FPS = 10초입니다.': 'Put numbered images in one folder and add that folder. FPS is the number of images played each second. 240 images ÷ 24 FPS = 10 seconds.', '사진 한 장에 움직임을 만드는 기능이 아니라, 준비한 이미지들을 순서대로 묶는 기능입니다.': 'This plays your existing images in order; it does not create motion from a single photo.', '모션은 FFmpeg로 앞뒤 프레임의 움직임을 추정해 중간 이미지를 만듭니다. 복제는 원본을 다시 쓰고, 겹치기는 앞뒤 이미지를 섞습니다.': 'Motion uses FFmpeg to estimate movement between neighboring frames and create intermediate images. Nearest frame reuses originals; Blend mixes neighboring images.', '예: A → B → C를 5장으로 늘리면 A → 중간 → B → 중간 → C. 빠른 움직임·가려지는 물체·컷 경계에서는 결과를 확인하세요. 오디오는 저장하지 않습니다.': 'Example: expanding A → B → C to five frames gives A → intermediate → B → intermediate → C. Review fast motion, occlusions and scene cuts. Audio is not exported.', '    무엇으로 받을지 고를 수 있습니다  (--result)\n      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov\n      mp4    어디서나 열리는 h264 하나\n      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다\n      seq    PNG 시퀀스만. 영상은 만들지 않습니다\n      both   시퀀스 + 확인용 영상 둘 다\n\n    auto 가 소스를 보고 정하는 기준\n      8bit 4:2:0        h264 mp4 (소리도 같이 뒤집습니다)\n      10bit · 4:2:2     ProRes 422 HQ mov — 계조를 안 깎으려고\n      알파가 있으면     ProRes 4444 mov — 투명도를 지키려고\n    무엇이 들어오든 h264 로 내보내면 순서 바꾸려다 원본 계조와 알파를 죽이게 됩니다.\n\n    시퀀스 폴더를 넣고 seq · both 로 받으면 파일을 역순으로 복사만 합니다 —\n    픽셀은 손대지 않습니다.\n\n    소스의 색 규격(bt709 / bt601 · 제한범위 / 전체범위)은 결과에 그대로 찍어 둡니다.\n    안 찍어 두면 다음 도구가 추측하고, 그 추측이 어긋나는 게 색이 밀리는 진짜 원인입니다.\n\n\n    30초가 넘는 영상은 ffmpeg 의 reverse 필터가 전체를 램에 올려서 위험하므로,\n    프레임을 경유하는 방식으로 자동 전환합니다. 이때 소리는 빠집니다.': 'Reverse a video or an image sequence. Auto saves a video; Sequence saves numbered frames. Sequence + MP4 saves both. Audio is omitted when processing through intermediate frames. Review the result before using it in an edit.', '    길이를 말하는 방법이 셋입니다. 편한 걸 쓰세요. 어느 쪽이든 같은 곳으로 갑니다.\n      --frames 264     결과를 264프레임으로\n      --seconds 11     결과를 11초로 (fps 를 곱해서 계산)\n      --speed 0.5      절반 속도 = 두 배 길이. 2 면 두 배 빠르게\n\n    없는 프레임을 무엇으로 채울까  (--retime-mode)\n      auto     소재를 보고 알아서 고릅니다 (기본). 무엇을 골랐는지 매번 알려줍니다\n      dup      가장 가까운 원본 프레임을 그대로. 픽셀 연산이 0 이라 매트가 100% 남습니다\n      motion   움직임을 추정해 없던 그림을 그립니다. 제일 부드럽고, 가끔 찢어집니다\n               (ffmpeg 이 없으면 앞뒤를 섞는 방식으로 알아서 물러섭니다)\n\n    auto 가 고르는 기준\n      매트 · 뎁스 패스면          → dup.    라벨색을 뭉개면 그 픽셀의 뜻이 사라집니다\n      실사인데 느리게 (늘리기)     → motion. 슬로모션은 없던 프레임이 있어야 매끄럽습니다\n      실사인데 빠르게 (줄이기)     → dup.    버리는 작업이라 새로 그릴 게 없습니다\n\n    매트 패스를 알아서 알아봅니다\n      매트는 몇 개의 라벨색으로만 칠해져 색상이 좁게 몰립니다 (실측 83~99%).\n      실사·페인팅은 넓게 퍼집니다 (21~28%). 이 차이로 갈라냅니다.\n      매트로 판별되면 쓰인 라벨색을 찾아 보여주고, 보간 후 섞인 픽셀을\n      가장 가까운 라벨로 되돌립니다. 명도는 손대지 않습니다 — 그건 뎁스니까요.\n      실사인데 매트로 잘못 본 것 같으면 --no-snap 으로 색 복원을 끄세요.\n\n    무엇으로 받을지 고를 수 있습니다  (--result)\n      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov — 영상 하나만 나옵니다\n      mp4    어디서나 열리는 h264 하나\n      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다\n      seq    PNG 시퀀스만. 영상은 만들지 않습니다\n      both   시퀀스 + 확인용 영상 둘 다\n      bundle 영상·전체 프레임·새로 보간된 프레임·기록을 새 폴더에 저장\n    새 UI 리타임 작업은 bundle이 기본입니다. CLI는 auto를 유지합니다.\n\n    알아둘 것\n      · 24fps 의 배수로 맞추면 편집에서 어긋나지 않습니다. 아니면 알려줍니다.\n      · motion 은 결과를 꼭 눈으로 보세요. 빠른 움직임 · 가림 · 모션블러에 약합니다.\n      · 소리는 빠집니다. 길이가 바뀌면 소리도 같이 늘려야 하는데,\n        그건 리타임이 아니라 다른 작업입니다.\n      · --note 를 주면 어디를 손댔는지 메모장(.txt) 한 장을 결과 옆에 놓습니다.\n      · 영상 소스의 색 규격(bt709 / 제한범위 등)은 결과 mp4 에 그대로 찍어 둡니다.': 'Change duration or speed while keeping the output FPS. Nearest frame reuses original pixels. Blend mixes neighboring images and may cause ghosting. Motion estimates movement with FFmpeg minterpolate (MCI, bidirectional estimation and adaptive overlapping blocks); it is not a neural AI model. It may distort fast motion, occlusions or scene cuts. Auto chooses nearest frame for mattes or shortening, and motion for slowing live action; without FFmpeg it falls back to blending.\n\nExample: three frames A–B–C retimed to five become A–intermediate–B–intermediate–C. The first and last frames are preserved. Other output positions do not always coincide with an original frame. Mapping marks describe sampling, not detected holds.\n\nChoose Video + frame folders to save the movie, every PNG frame, interpolated PNG frames and a mapping manifest together. Retimed exports do not contain source audio. 16-bit and grayscale inputs use Blend instead of Motion to preserve depth and channel layout; the actual method is recorded in the result.', '    영상 · 이미지 · 번호 붙은 시퀀스 폴더를 다 받습니다.\n    넣은 것과 안 맞는 목적지를 골라 두면 맞는 쪽으로 알아서 옮기고 알려줍니다.\n\n    이미지를 넣으면\n      PNG · JPG · WEBP 로 바꿉니다. --max-height 로 크기도 같이 줄일 수 있습니다.\n      (재보니 같은 그림이 PNG 153KB → JPG 24KB → WEBP 18KB 였습니다.)\n\n    시퀀스 폴더를 넣으면\n      영상 한 편으로 묶습니다. Sequence 의 반대 방향입니다.\n      --prores-movie 를 붙이면 누크용 ProRes 로 묶고, 알파가 있으면 4444 로 갑니다.\n      번호가 1부터 시작하지 않거나 중간이 비어 있어도 됩니다 —\n      패턴을 짜 맞추지 않고 파일 목록을 그대로 먹입니다.\n\n    영상을 넣으면, 어디로 보낼지 하나만 고르면 됩니다.\n\n      MP4 (배포·업로드)\n        가장 잘 열리는 형식입니다. 안에 든 코덱이 mp4 가 받는 것(h264 / hevc /\n        mpeg4 / av1)이면 다시 인코딩하지 않고 상자만 갈아끼웁니다. 몇 초 걸리고\n        화질 손실은 없습니다. 영상 데이터가 비트 단위로 그대로 옮겨갑니다.\n\n      MOV (상자만 교체)\n        확장자만 .mov 가 됩니다. h264 짜리를 .mov 로 바꿔도 안에 든 건 여전히\n        h264 입니다. 화질이 좋아지지도, 편집이 수월해지지도 않습니다.\n        편집용 마스터가 필요하면 아래를 쓰세요.\n\n      누크용 마스터 (ProRes)\n        Nuke · After Effects · Resolve 가 스크러빙할 때 버벅이지 않는 코덱입니다.\n          · 용량이 10~30배 커집니다. 고장이 아니라 원래 그렇습니다.\n          · 스크러빙 속도와 세대 손실 내성을 용량으로 사는 코덱입니다.\n          · 배포·업로드용으로 쓰면 안 됩니다.\n          · h264 를 ProRes 로 바꿔도 없던 화질이 생기지는 않습니다.\n            이미 뭉개진 8bit 계조를 10bit 상자에 옮겨 담을 뿐입니다.\n            다시 인코딩할 거라면 원본 렌더에서 바로 뽑는 게 맞습니다.\n          · 알파가 있으면 알아서 ProRes 4444 로 갑니다. 422 HQ 는 알파를 못 담습니다.\n\n      용량 줄이기\n        반드시 다시 인코딩합니다. 되돌릴 수 없는 화질 손실이 생깁니다.\n        원본은 그대로 두고 새 파일을 만드니, 마음에 안 들면 지우면 됩니다.\n        이미 잘 압축된 파일은 오히려 커질 수 있습니다. 그러면 커졌다고 알려줍니다.\n\n        가장 크게 줄이는 건 화질 단계가 아니라 해상도입니다. 4K → 1080p 면\n        픽셀 수가 1/4 이 됩니다. 화질을 만지기 전에 "이걸 정말 4K 로 봐야 하나"를\n        먼저 따져보세요.\n\n        h265 는 같은 화질에 20~30% 더 작지만 인코딩이 2~4배 느리고,\n        옛 기기나 일부 편집 프로그램에서 안 열립니다.\n\n    공통\n      · 알파 채널은 mp4 에 못 들어갑니다. 투명도가 필요하면 누크용 마스터나\n        PNG 시퀀스를 쓰세요.\n      · 소스의 색 규격(bt709 / 제한범위 등)을 결과에 그대로 적어 둡니다.\n        안 적어 두면 다음 도구가 추측하고, 그 추측이 어긋나면 색이 밀립니다.\n      · 10bit · 4:2:2 소스는 배포용으로 갈 때 8bit 로 내려갑니다. 내려간다고\n        알려줍니다. 계조를 지키려면 누크용 마스터로 가세요.': 'Image sequence → video: put numbered images in one folder (for example shot_0001.png, shot_0002.png), add that folder, choose Sequence → video, set Sequence FPS and run. Images are naturally sorted by filename. At 24 FPS, 240 images make 10 seconds. The images must have matching dimensions, channels and bit depth. This plays existing images in order; it does not invent movement from a single photograph.\n\nChoose GIF for an animated preview with FPS, width and loop controls. Video → image sequence saves every decoded frame in a new folder. MP4 is for sharing; ProRes is for editing and alpha. MOV can repackage compatible streams without re-encoding. Compression re-encodes and can lose quality.', '    파일 두 개를 넣으세요. 먼저 넣은 것이 A, 나중이 B 입니다.\n    (폴더를 넣으면 그 안의 앞의 두 개를 씁니다.)\n\n    실행하면 대조 창이 바로 열립니다. 인코딩을 기다릴 것도, 파일이 쌓일 것도 없습니다.\n\n    창 안에서 네 가지를 오갈 수 있습니다  (숫자키 1~4)\n      1 반반 바   경계선을 잡고 좌우로 끌면 A 와 B 가 갈립니다\n      2 겹치기    포개서 봅니다. 섞기 손잡이로 0~100% 조절 — 위치가 밀렸는지 볼 때\n      3 번갈아    빠르게 교대합니다. 미세한 차이는 이게 제일 잘 보입니다\n      4 나란히    왼쪽 A, 오른쪽 B. 여기 안에 "둘 다 · A만 · B만" 이 같이 있습니다\n\n      바 끌기 · ← → 로 미세 조정 (Shift 를 누르면 더 잘게)\n      A · B 키로 한쪽만 · Space 재생·멈춤\n      영상이면 두 쪽이 같은 지점을 보도록 재생 위치를 계속 맞춰 둡니다.\n\n    --view 로 어느 보기로 열지 정합니다 (wipe · overlay · flicker · side).\n\n    남는 것 없이 봅니다\n      대조는 결과물이 아니라 확인하는 행위입니다. 볼 때마다 원본 폴더에 파일이 쌓이면\n      정작 필요한 파일이 묻힙니다. 그래서 임시 자리에 만들고, Framing 을 닫을 때 지웁니다.\n      남겨서 남에게 보내야 하면 --keep 을 붙이세요. 그때는 원본 옆에 폴더를 만들고\n      고른 보기로 구운 대조 영상도 같이 넣어 줍니다.\n\n    브라우저가 못 여는 형식이면 알아서 바꿔 놓고 봅니다\n      창의 재생기가 실제로 여는 건 mp4(h264 8bit) 와 webm 정도입니다.\n      ProRes · DNxHD · HEVC · 10bit · .mov 상자는 아무 말 없이 까만 화면만 나옵니다.\n      그래서 미리 알아보고, 못 여는 형식이면 볼 수 있는 한 벌을 만들어 그걸 겁니다.\n      창 위쪽에 "보기용으로 변환함" 이라고 붙습니다 — 원본 픽셀이 아니라는 뜻입니다.\n      원본은 손대지 않고, --keep 으로 굽는 대조 영상도 원본에서 만듭니다.\n      1440p 가 넘으면 보기용만 그 크기로 줄입니다. 줄였다고 알려줍니다.\n\n    알아둘 것\n      · 해상도가 다르면 A 기준 캔버스에 비율을 유지해 맞추고 검은 여백을 넣습니다.\n      · 길이가 다르면 짧은 쪽에서 끝납니다.\n      · 색 규격이 서로 다르면 그것부터 알려줍니다. 그게 차이의 원인일 때가 많습니다.': 'Add two videos or two images as A and B. Use Wipe, Overlay, Flicker or Side by side, or view one input. The timeline controls both inputs; comparison ends at the shorter duration. B is fitted to the A canvas with its aspect ratio preserved.\n\nSelect a region by dragging or by entering pixel coordinates. Region only zooms the comparison to that crop. Export the current PNG, full video, full PNG sequence or both video and frames. Export the current view or all views including A/B. Exports render from the source and omit the viewer guides, zoom and pan.', '    AI 영상 이름은 dreamina-2026…-n2vns 처럼 사람이 못 읽습니다. 여기서 정리하세요.\n\n      패턴    장면_{n}  →  장면_01.mp4, 장면_02.mp4 …  ({n} 자리에 번호)\n      도커식  조용한_수달.mp4 처럼 기억하기 쉬운 두 단어를 무작위로\n\n    실행하기 전에 무엇이 무엇으로 바뀌는지 전부 보여주고,\n    하나라도 부딪히면 아무것도 바꾸지 않습니다. 넣은 순서대로 번호가 붙습니다.': '\n    Clean up long, generated filenames before sharing.\n\n      Pattern: shot_{n} → shot_01.mp4, shot_02.mp4 … ({n} is the number)\n      Random words: a memorable two-word name.\n\n    Preview every change before applying it. If any name conflicts, nothing is renamed.\n    Numbers follow the order in the list.', '전체 보기로': 'Full view', '왼쪽 {0} {1}% · 오른쪽 {2}': 'Left {0} {1}% · right {2}', '왼쪽 {0} | 오른쪽 {1}': 'Left {0} | right {1}', '재생하지 못했습니다. 파일 형식을 확인하세요.': 'Playback failed. Check the file format.', '── 화면 ──': '── Interface ──', '── 넣은 파일 ──': '── Inputs ──', '── 마지막 기록 ──': '── Recent log ──', '── 오류 ──': '── Errors ──', '말': 'Language', '주소': 'URL', '고른 것 없음': 'Nothing selected', '고른 것': 'Selection', '도는 중': 'Running', '아니오': 'No', '물어보지 못했습니다 — {0}': 'Unable to query the backend — {0}', '{0}() 을 창이 내주지 않습니다 (pywebview 판이 낮을 수 있습니다)': '{0}() is unavailable in this window (pywebview may be outdated)', '예시': 'Example', '모션 보간은 8비트 컬러 입력을 지원합니다. 계조와 채널을 유지하려면 겹치기를 사용하세요.': 'Motion interpolation supports 8-bit color inputs. Use Blend to preserve bit depth and channel layout.', '계조와 채널을 유지하기 위해 모션 보간 대신 겹치기를 사용합니다. 16비트·그레이스케일 입력은 프레임을 혼합합니다.': 'Using Blend instead of Motion to preserve bit depth and channel layout. 16-bit and grayscale inputs use frame blending.', '예시 · 원본 3프레임': 'Example · 3 source frames', '예시 · 결과 5프레임': 'Example · 5 output frames', 'Framing 진단서': 'Framing diagnostic report', '       (▉ {0} · ░ {1})': '       (▉ {0} · ░ {1})', '      {0}   (▉ {1} · ░ {2})': '{0}   (▉ {1} · ░ {2})', '{0} → {1}프레임  ·  {2}배속  ·  {3}  ·  {4}': '{0} → {1} frames · {2}× speed · {3} · {4}', '플레이트 늘리기 (RIFE)': 'Stretch plate (RIFE)', '원본 계조를 지키면서 사이만 RIFE 로 채움': 'Keeps the source depth and lets RIFE fill only the gaps', '    두 번에 나눠 돌립니다.\n      framing plate 컷.mov --frames 96        내보내기 (폴더가 생깁니다)\n      framing plate 컷.mov_rife               가져오기 (RIFE 결과를 넣고 다시)\n\n    RIFE 실행 파일을 --rife 로 주면 가운데 단계까지 한 번에 갑니다.\n    정수배(2·4·8)로 뽑은 뒤 목표 장수만 골라냅니다 — 그래야 원본이 격자 위에\n    정확히 놓여서 그대로 통과합니다. 임의 비율로 한 번에 늘리면 원본이 거의\n    안 남습니다. 알파는 다루지 않습니다.': '    Runs in two passes.\n      framing plate shot.mov --frames 96      export (a folder appears)\n      framing plate shot.mov_rife             import (put RIFE output back, run again)\n\n    Pass --rife with the RIFE executable to do the middle step too.\n    It renders an integer multiple (2·4·8) and then selects the target count, so\n    original frames land exactly on the grid and pass through untouched. Stretching\n    straight to an arbitrary ratio leaves almost no originals. Alpha is not handled.', '플레이트  {0}프레임 · {1}fps · {2} · {3}': 'Plate  {0} frames · {1}fps · {2} · {3}', '모음  원본 그대로 {0}장 · RIFE 로 채운 자리 {1}장': 'Assembled  {0} frames straight from the plate · {1} filled by RIFE', '되묶기  {0} · 색 표기 그대로': 'Re-wrapped  {0} · colour tags carried over', 'RIFE 실행 파일이 등록되지 않았습니다 — 아래 명령을 직접 돌리세요': 'No RIFE executable registered — run this command yourself', 'RIFE  {0}배로 돌립니다': 'RIFE  running at {0}x', '가져오지 못했습니다 — 위 내용을 확인하세요.': 'Nothing was imported — see the lines above.', '플레이트가 두 장은 되어야 사이를 채울 수 있습니다.': 'A plate needs at least two frames before anything can be filled in.', '몇 장으로 늘릴지 정해 주세요 (--frames · --seconds · --speed).': 'Say how many frames you want (--frames · --seconds · --speed).', '알파는 다루지 않습니다': 'Alpha is not handled', '늘린 것 되돌리기': 'Undo a stretch', '늘림 메모를 보고 보간 프레임만 빼냄': 'Reads the stretch note and removes only the interpolated frames', '메모  {0} · {1}': 'Note  {0} · {1}', '{0} → {1}프레임 · 늘린 자리 {2}장 · 되돌리면 {3}장': '{0} → {1} frames · {2} added · reverts to {3}', '늘림 메모를 찾지 못했습니다. {0} 를 영상 옆에 두거나 --note 로 경로를 주세요.': 'No stretch note found. Put {0} beside the movie or pass --note.', '되돌릴 수 없습니다 — {0}': 'Cannot revert — {0}', '메모에 원본 자리가 하나도 없습니다': 'The note lists no original frames', '끝 프레임 붙잡기': 'Hold the end frame', '보간 없이 앞뒤 프레임을 붙잡아 길이만 늘림': 'Extends the length by freezing the first or last frame — no interpolation', '{0} → {1}프레임 · {2} 홀드 ({3}) · 새로 그리는 픽셀 없음': '{0} → {1} frames · held at the {2} ({3}) · no pixel drawn', '뒤': 'end', '앞': 'start', '붙일 것 없음': 'nothing to add', '홀드는 늘리기만 합니다 — {0}장을 {1}장으로 줄이려면 리타임을 쓰세요.': 'Hold only lengthens — to go from {0} to {1} frames, use retime.', '붙잡을 자리를 확인하세요 ({0}).': 'Check which end to hold ({0}).', '붙잡을 프레임이 없습니다.': 'There is no frame to hold.', '플레이트  {0}프레임 · {1}fps · {2}': 'Plate  {0} frames · {1}fps · {2}', '{0} → {1}프레임 · {2} 홀드 {3}장  ·  {4}': '{0} → {1} frames · {2} hold of {3}  ·  {4}', '프레임을 읽지 못했습니다.': 'No frames could be read.', '뺀 자리  늘리면서 생긴 {0}장': 'Removed  {0} frames that the stretch added', '{0} → {1}프레임 · 늘린 자리 {2}장 뺌  ·  {3}': '{0} → {1} frames · removed {2} added frames  ·  {3}', '끝 홀드': 'Hold last frame', '원본을 1:1 로 쓰고 모자란 길이만 마지막 프레임으로 채웁니다': 'Keeps every source frame 1:1 and fills the remaining length by holding the last frame.', '끝 홀드는 늘릴 때만 씁니다 — 복제로 갑니다': 'Hold last frame only applies when lengthening — using nearest frame instead.', '뺄 자리를': 'Frames to remove', '직접 지정': 'Pick them myself', '메모와 영상이 맞고 원본 프레임이 모두 남아 있어야 복원할 수 있습니다': 'When a note exists it is always the right answer', '뺄 자리': 'Frames to remove', '메모 파일': 'Note file', 'tail:20 = 뒤에서 20장 · head:20 = 앞에서 20장 · 8,21,34 = 그 번호들(1부터) · 240-265 = 그 구간': 'tail:20 = last 20 · head:20 = first 20 · 8,21,34 = those numbers (from 1) · 240-265 = that range', '끝을 붙잡아 늘린 것을 되돌릴 때': 'Undoing a hold-the-last-frame stretch', '샷 검사로 정지 구간을 확인할 수 있지만, 원래 정지한 장면과 추가된 홀드는 직접 구분해야 합니다.': 'If you do not know how many were added, run "Review shot" first to find the frozen stretch.', '메모가 왜 필요한가요?': 'Why does it need a note?', '길이가 그대로입니다': 'length is unchanged', '장 늘어납니다': ' longer', '장 줄어듭니다': ' shorter', '줄일 때는 원본 프레임을 고르게 솎아 냅니다. 새로 그리는 프레임은 없습니다.': 'Shortening drops source frames evenly across the clip. Nothing new is drawn.', '끝 홀드는 앞을 1:1 로 두고 모자란 길이만 마지막 프레임으로 채웁니다 — 앞 구간이 한 프레임도 밀리지 않습니다.': 'Hold last frame keeps the head 1:1 and fills only the missing length with the last frame, so nothing in the head shifts.', '예시 · 늘려 둔 8프레임': 'Example · 8 stretched frames', '예시 · 되돌린 5프레임': 'Example · 5 frames after undo', '점선 = 늘리면서 생긴 자리': 'dashed = frames the stretch added', '메모에 어디였는지 적혀 있으면': 'When the note records where', '그 자리만 빼냅니다 — 다시': 'it removes exactly those — it', '추측하지 않습니다': 'does not guess again', '메모가 없으면 뺄 자리를 직접 말할 수 있습니다 (뒤에서 N장 · 번호 · 구간)': 'With no note you can name the frames yourself (last N · numbers · a range)', '여기서 줄이면 무엇이 다른가요?': 'What is different about shortening here?', '메모와 상관없이 전 구간에서 고르게 솎아 냅니다. 그래서 남은 프레임이 원본의 몇 번이었는지가 어긋납니다.': 'It ignores the note and thins the whole clip evenly, so the remaining frames no longer map back to the source.', '메모가 있고 원본 프레임이 모두 남아 있을 때만 원본 길이로 되돌릴 수 있습니다.': 'A note can restore the original length only when every source frame remains.', '무엇을': 'What to do', '늘리기': 'Make it longer', '줄이기': 'Make it shorter', '늘리기 · 줄이기 · 늘린 것 되돌리기': 'Longer · shorter · undo a stretch', '늘렸던 것을 되돌리는 중이라면 위에서 "늘린 것 되돌리기" 를 고르세요 — 늘리면서 생긴 자리를 그 자리로 빼냅니다.': 'Undoing a stretch? Pick "Undo a stretch" above — it removes exactly the frames the stretch added.', '메모대로': 'From the note', '메모를 창에 끌어다 놓거나 찾아보기를 누르세요': 'Drop the note onto the window, or press Browse', '찾아보기': 'Browse', '비우기': 'Clear', '목록에 있는 메모': 'Notes in your list', '메모': 'Note', '메모장(.txt)이나 늘림메모(.json)를 창에 끌어다 놓으면 이 칸에 들어옵니다.': 'Drop a retiming note (.txt) or a stretch note (.json) onto the window and it lands in this field.', '비워 두면 영상 옆의 늘림메모를 먼저 보고, 없으면 영상 안에 심어 둔 것을 읽습니다.': 'Left blank, it looks for the stretch note beside the video, then the copy embedded in the video itself.', '메모는 .txt 또는 .json 입니다.': 'A note is a .txt or .json file.', '파일 대화상자를 열지 못했습니다.': 'Could not open the file dialog.', '되돌리기는 다시 계산하는 것이 아니라 고르는 것입니다. 메모에 적힌 자리를 그대로 빼냅니다.': 'Undoing is picking, not recalculating. It removes exactly the positions the note records.', '다시 추정하면 늘릴 때와 뺄 때가 어긋나는데, 그건 아무도 못 알아챕니다. 넣은 영상의 프레임 수가 메모와 맞지 않으면 아무것도 만들지 않고 멈춥니다.': 'Guessing again would put the stretch and the removal out of step, and nobody would notice. If the video does not match what the note expects, it stops without writing anything.', '"끝 홀드" 로 늘렸다면 붙잡힌 자리는 전부 꼬리에 있습니다. 245장을 265장으로 늘렸다면 tail:20 입니다.': 'If you stretched with "Hold last frame", every held frame sits at the tail. 245 stretched to 265 means tail:20.', '새로 생긴 프레임 없음 — 원본 프레임만 골라 썼습니다': 'No new frames — only source frames were used', '    늘릴 때 남긴 메모를 보고 보간으로 넣은 자리만 뺍니다.\n\n      framing revert 컷_01_plate.mov                        옆의 메모를 알아서 찾습니다\n      framing revert 컷_01.mov --note-file 컷_01_리타임메모.txt\n      framing revert 컷_01.mov --note-file 늘림메모.json\n\n    리타임 메모장(.txt)을 줘도 됩니다 — 거기 적힌 프레임 번호를 뺍니다.\n    메모를 잃어버려도 영상 안에 심어 둔 것이 있으면 되돌아갑니다.\n    넣은 영상의 프레임 수가 메모와 다르면 (자르거나 다른 영상이면)\n    아무것도 만들지 않고 멈춥니다.\n\n    메모가 아예 없으면 어디를 뺄지 직접 말할 수 있습니다.\n\n      framing revert 컷.mov --drop tail:20        뒤에서 20장\n      framing revert 컷.mov --drop head:20        앞에서 20장\n      framing revert 컷.mov --drop 8,21,34        그 번호들 (1부터)\n      framing revert 컷.mov --drop 240-265        구간\n\n    메모와 영상이 맞아도 원본 프레임이 모두 남아 있어야 원본 길이로 복원됩니다.': '    Reads the note left by the stretch and removes only the frames it added.\n\n      framing revert cut_01_plate.mov                       finds the note beside it\n      framing revert cut_01.mov --note-file cut_01_retime-notes.txt\n      framing revert cut_01.mov --note-file stretch-note.json\n\n    A retiming note (.txt) works too — it removes the frame numbers written there.\n    Even if the note file is lost, the copy embedded in the video gets you back.\n    If the video does not have the frame count the note expects (trimmed, or a\n    different video), nothing is written and it stops.\n\n    With no note at all, you can name the frames to remove yourself.\n\n      framing revert cut.mov --drop tail:20        last 20\n      framing revert cut.mov --drop head:20        first 20\n      framing revert cut.mov --drop 8,21,34        those numbers (from 1)\n      framing revert cut.mov --drop 240-265        that range\n\n    Naming them yourself is the fallback — when a note exists it is always right.', '      framing hold 컷_01.mov --frames 46            뒤를 붙잡습니다 (기본)\n      framing hold 컷_01.mov --seconds 2 --hold-where head\n      framing hold 컷_01.mov --frames 46 --hold-where both\n\n    새로 그리는 픽셀이 없어서 플레이트가 그대로 남습니다. 대신 붙잡은 자리는\n    멈춰 있으니, 꼬리가 움직이는 컷이면 RIFE 쪽(plate)을 쓰세요.\n    늘림 메모를 남기므로 framing revert 로 되돌아갑니다.': '      framing hold cut_01.mov --frames 46            holds the tail (default)\n      framing hold cut_01.mov --seconds 2 --hold-where head\n      framing hold cut_01.mov --frames 46 --hold-where both\n\n    No pixel is redrawn, so the plate survives intact. The held stretch simply\n    stops, so for a cut whose tail is still moving use the RIFE route (plate).\n    It leaves a stretch note, so framing revert takes you back.', '길이   {0} → {1}프레임': 'Length  {0} → {1} frames', '{0} {1}장 · 프레임 번호 (결과 기준, 1부터)': '{0} {1} frames · frame numbers (output, starting at 1)', '납품 (ProRes 4444 XQ)': 'Delivery (ProRes 4444 XQ)', '납품 (4444 XQ)': 'Delivery (4444 XQ)', 'MOV · 4444 XQ · 1920×1080 · 24fps · 샷코드_테스크_v###': 'MOV · 4444 XQ · 1920×1080 · 24fps · shot_task_v###', 'MOV · ProRes 4444 XQ · 1920×1080 · 24fps 로 고정합니다. 규격이 다르면 맞추면서 알려 드립니다.': 'Locked to MOV · ProRes 4444 XQ · 1920×1080 · 24fps. Anything off spec is conformed, and it tells you what it changed.', '샷코드': 'Shot code', '테스크': 'Task', '버전': 'Version', '비우면 원본 이름에서 물려받습니다': 'Leave blank to inherit it from the source name', 'A_0002C007_org_v001 → A_0002C007': 'A_0002C007_org_v001 → A_0002C007', '비우면 안 쓰인 다음 번호': 'Leave blank for the next unused number', '나올 이름': 'Resulting name', '(첫 파일 기준)': '(for the first file)', '규격이 안 맞으면 어떻게 되나요?': 'What if the source is off spec?', '해상도가 다르면 비율을 지킨 채 1920×1080 안에 넣고 남는 자리를 검게 채웁니다. 늘려서 찌그러뜨리지 않습니다.': 'A different resolution is fitted inside 1920×1080 with its aspect kept and the rest padded black. Nothing is stretched.', 'fps 가 24 가 아니면 프레임을 버리거나 겹쳐 채웁니다. 길이를 제대로 맞추려면 리타임을 먼저 쓰세요.': 'At any rate other than 24fps, frames are dropped or duplicated. To change the length properly, run Retime first.', 'ffmpeg 는 4444 XQ 를 10bit 입력으로 받아 12bit 로 적어 냅니다. 원본이 12bit 이상이면 그 단계에서 한 번 내려갑니다 — 12bit 원본을 그대로 지켜야 하면 누크 mov64 로 내세요.': 'ffmpeg takes 10-bit input for 4444 XQ and writes it out declared as 12-bit. A source deeper than 10-bit loses that step — to keep a true 12-bit master, write it from Nuke mov64 instead.', 'delivery: 샷코드. 비우면 원본 이름에서 물려받습니다 (A_0002C007_org_v001 → A_0002C007)': 'delivery: shot code. Blank inherits it from the source name (A_0002C007_org_v001 → A_0002C007)', 'delivery: 테스크 이름 (기본 ai)': 'delivery: task name (default ai)', 'delivery: 버전 번호. 비우면 안 쓰인 다음 번호로 갑니다': 'delivery: version number. Blank picks the next unused one', '파일을 창에 드래그 앤 드롭 하세요. 또는 아래에서 할 일을 고르세요.': 'Drag and drop your files onto this window, or pick what to do below.', '원본을 수정하지 않고 검토할 후보를 찾습니다. 프레임 번호는 1부터입니다.': 'Finds candidates for review without modifying the source. Frame numbers start at 1.', '    어디를 뽑을지만 정하면 됩니다.\n      첫 · 중간 · 마지막 (기본) / 균등하게 N장 / N프레임마다 / 직접 찍기 / 전부\n\n    직접 찍기 (--at, 쉼표로 여러 개)\n      first middle last · 프레임 번호(1부터) · -1(끝에서부터) · 25% · 1:23 · 3s\n      all 이라고 쓰면 전부 — 시퀀스로 풉니다\n\n    몇 장만 뽑을 때는 PNG · JPG(4:4:4) 로, 전부 풀 때는 PNG · JPG · EXR · DPX 로.\n    무손실 시퀀스는 원본 mp4 의 수십~수백 배가 됩니다. 1분 1080p ≈ 5~15GB.\n\n    색이 달라 보이지 않게 하는 일 세 가지는 몇 장이든 전부든 똑같이 합니다.\n      · JPG 는 색을 줄이지 않는 4:4:4 로\n      · 10bit · 알파 소스는 원본 깊이 그대로 (16bit PNG, 알파 유지)\n      · 밝기 범위 표기가 틀린 영상은 읽는 단계에서 바로잡음 (--levels auto|full|keep)\n\n    화면과 여전히 다르면 보는 프로그램의 감마 차이입니다 (영상 2.4 vs sRGB 2.2).\n    그건 파일을 고쳐서 맞추면 안 됩니다 — 다른 모든 도구와 어긋나게 됩니다.': 'Choose where to extract frames: first, middle, last, a frame number, a percentage or a timestamp. Use All frames to save every decoded frame as a numbered image sequence. PNG is lossless; JPG is smaller. EXR and DPX are available for full sequences. Source files stay unchanged.'}
LOCALE_SCRIPT = '\'use strict\';\n\n/** Translates complete messages. Captured filenames are never word-replaced. */\nclass MessageCatalog {\n  constructor(entries) {\n    this.entries = entries;\n    this.reverse = Object.fromEntries(Object.entries(entries).map(([key, value]) => [value, key]));\n    this.templates = this.compile(entries);\n    this.backTemplates = this.compile(this.reverse);\n    this.protected = new Set();\n    this.nodes = new WeakMap();\n  }\n  compile(entries) {\n    const escape = text => text.replace(/[.*+?^${}()|[\\]\\\\]/g, \'\\\\$&\');\n    return Object.entries(entries).filter(([key]) => /\\{\\d+\\}/.test(key)).map(([key, value]) => {\n      const ids = [], parts = key.split(/(\\{\\d+\\})/g);\n      const pattern = parts.map((part, index) => {\n        if (!/^\\{\\d+\\}$/.test(part)) return escape(part);\n        ids.push(part);\n        // Counts cannot absorb the preceding label\'s words (e.g. "source positions 2").\n        const numeric = /^(?:프레임|장|개|곳|컷|번째|초|배속|배|%|fps|bit|p\\b|\\s+(?:frames|files|shots|pages|cuts)\\b)/.test(parts[index + 1] || \'\');\n        return numeric ? \'([-+]?\\\\d+(?:[.,]\\\\d+)*)\' : \'([\\\\s\\\\S]*?)\';\n      }).join(\'\');\n      return { pattern: new RegExp(\'^\' + pattern + \'$\'), value, ids,\n        weight: key.replace(/\\{\\d+\\}/g, \'\').length };\n    }).sort((a, b) => b.weight - a.weight);\n  }\n  protect(values) {\n    for (const value of values.filter(Boolean)) {\n      this.protected.add(value);\n      this.protected.add(value.split(/[\\\\/]/).pop());\n    }\n  }\n  text(value, lang = \'en\', from = \'kr\', depth = 0, declared = false) {\n    if (value == null || lang === from) return value;\n    const text = String(value);\n    if ((!declared && this.protected.has(text)) || depth > 5) return text;\n    const entries = lang === \'en\' ? this.entries : this.reverse;\n    if (Object.hasOwn(entries, text)) return entries[text];\n    // Preserve surrounding whitespace in text nodes and console messages.\n    if (text.trim() !== text && Object.hasOwn(entries, text.trim())) {\n      return text.replace(text.trim(), entries[text.trim()]);\n    }\n    for (const template of lang === \'en\' ? this.templates : this.backTemplates) {\n      const match = text.match(template.pattern);\n      if (!match) continue;\n      const values = new Map(template.ids.map((id, index) => [id, match[index + 1]]));\n      return template.value.replace(/\\{\\d+\\}/g, id => {\n        const captured = values.get(id) ?? id;\n        // File paths and names are user data, even when their words match labels.\n        if (/[\\\\/]/.test(captured) || /\\.[a-z0-9]{2,8}$/i.test(captured)) return captured;\n        return this.text(captured, lang, from, depth + 1);\n      });\n    }\n    // Legacy summaries are composed from complete messages with separators.\n    // Do not replace arbitrary words within a filename or an unknown error.\n    if (/\\n| · | — |, /.test(text)) {\n      return text.split(text.includes(\'\\n\') ? /(\\n)/ : /( · | — |, )/).map(part =>\n        this.textPart(part, lang, from, depth)).join(\'\');\n    }\n    return text;\n  }\n  textPart(part, lang, from, depth) {\n    return /^(\\n| · | — |, )$/.test(part) ? part : this.text(part, lang, from, depth + 1);\n  }\n  /** Refresh persistent result/viewer regions without restarting their media. */\n  refresh(root, lang, from = \'kr\') {\n    if (!root) return;\n    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);\n    const update = (owner, key, value, write) => {\n      const records = this.nodes.get(owner) || new Map();\n      const prior = records.get(key);\n      const source = prior?.rendered === value ? prior.source : this.text(value, \'kr\', from, 0, true);\n      const rendered = this.text(source, lang, \'kr\', 0, true);\n      records.set(key, { source, rendered }); this.nodes.set(owner, records);\n      if (rendered !== value) write(rendered);\n    };\n    while (walker.nextNode()) {\n      const node = walker.currentNode;\n      if (node.parentElement?.closest(\'script,style,textarea,[translate="no"]\')) continue;\n      update(node, \'text\', node.nodeValue, value => node.nodeValue = value);\n    }\n    for (const node of [root, ...root.querySelectorAll(\'[title],[aria-label],[placeholder]\')]) {\n      if (!node.getAttribute || node.closest(\'[translate="no"]\')) continue;\n      for (const key of [\'title\', \'aria-label\', \'placeholder\']) {\n        if (node.hasAttribute(key)) update(node, key, node.getAttribute(key), value => node.setAttribute(key, value));\n      }\n    }\n  }\n}\nconst EN = /* @locale-data */ {};\nconst Messages = new MessageCatalog(EN);\n'


LOCALE_DATA.update({'길이 · 배속 변경': 'Change length / speed', '샷 검사 · 콘택트시트 · 대조': 'Shot inspection · contact sheet · compare', '목표 길이에 따라 늘리거나 줄입니다. 리타임 결과에는 오디오가 저장되지 않습니다.': 'The target length determines whether the clip becomes longer or shorter. Retime output has no audio.', '메모가 있고 원본 프레임이 모두 남아 있을 때만 원본 길이로 되돌릴 수 있습니다.': 'A note can restore the original length only when every source frame remains.', '메모와 영상이 맞고 원본 프레임이 모두 남아 있어야 복원할 수 있습니다': 'Restoration requires a matching note and every original frame.', '새 버전에서 늘린 영상은 추가 프레임만 빼서 원본 길이로 되돌립니다. 이전 버전의 복원 불가 메모나 줄인 영상은 원본으로 다시 작업하세요.': 'Motion and blend may omit original frames, limiting restoration. Use the original video if restoration is unavailable.', '전체 프레임을 저장하므로 용량이 크게 늘 수 있습니다. 영상만 필요하면 알아서 또는 MP4를 선택하세요.': 'Saving every frame can use substantial disk space. Choose Auto or MP4 if you only need a video.', '길이 또는 FPS가 다른 파일은 각 파일의 정보로 따로 계산합니다.': 'Files with different lengths or frame rates are calculated individually.', '숫자를 입력하세요.': 'Enter a number.', '정수로 입력하세요.': 'Enter a whole number.', '범위 안의 숫자를 입력하세요.': 'Enter a number within this range.', '뽑을 지점을 입력하세요. 예: 1, 50%, last': 'Enter frame positions, e.g. 1, 50%, last.', '뺄 프레임을 입력하세요. 예: tail:20': 'Enter frames to remove, e.g. tail:20.', '샷 검사로 정지 구간을 확인할 수 있지만, 원래 정지한 장면과 추가된 홀드는 직접 구분해야 합니다.': 'Inspection can find still sections. Review whether they are original still scenes or added holds.'})

LOCALE_DATA.update({'소재 판별': 'Material detection', '자동 판별': 'Detect automatically', '일반 영상 · 색 유지': 'Regular video · preserve colors', '색이 제한된 실사는 매트로 오인될 수 있습니다. 일반 영상 · 색 유지를 고르면 색 복원을 끄고 원래 색으로 보간합니다.': 'Footage with a limited palette can be mistaken for a matte. Regular video disables label-color snapping and interpolates the original colors.', '프레임 처리': 'Frame processing', '줄일 때 알아서·복제는 원본 프레임을 골라 씁니다. 겹치기·모션보간을 선택하면 중간 이미지를 계산합니다.': 'When shortening, Auto and Duplicate select source frames. Blend and Motion calculate intermediate images.'})

LOCALE_DATA.update({'결과 미리보기': 'Preview result', '일부 결과만 미리보기로 표시합니다. 전체 파일은 폴더 열기에서 확인하세요.': 'Preview shows a subset of the results. Open the folder to see all files.'})

LOCALE_DATA.update({'소리': 'Audio', 'A 소리': 'Audio A', 'B 소리': 'Audio B', '소리 끄기': 'Mute', '음량': 'Volume', '대조 소리 · 저장에도 적용': 'Comparison audio · also used for export', '대조 음량': 'Comparison volume', '선택한 소리로 MP4 저장': 'MP4 uses the selected audio', '두 파일에 오디오가 없습니다': 'Neither file contains audio', '영상 저장': 'Save video', '이미지 저장': 'Save image', '저장 결과 보기': 'Preview saved result'})

LOCALE_DATA.update({'RIFE GPU 보간': 'RIFE GPU interpolation', 'AI로 중간 프레임 만들기': 'Create intermediate frames with AI', '목표 프레임 수': 'Target frame count', '포함된 RIFE 모델을 GPU로 실행합니다. 원본에서 고른 프레임은 그대로 쓰고, 새 프레임을 채워 MOV로 저장합니다.': 'Run bundled RIFE on your GPU, retain selected original frames and export MOV.', '원본·보간 PNG도 함께 저장하므로 저장 공간이 많이 필요합니다. 소리와 알파는 저장하지 않습니다.': 'Original and interpolated PNG frames need significant disk space. Audio and alpha are not included.', 'RIFE는 Vulkan 지원 GPU와 드라이버가 필요합니다. 결과가 완료되면 결과 미리보기에서 확인하세요.': 'RIFE requires a Vulkan-capable GPU and driver. Use Preview result when complete.'})

class MessageCatalog:
    def __init__(self, entries=None):
        self.entries = LOCALE_DATA if entries is None else entries
        self.templates = []
        for key, value in self.entries.items():
            ids = re.findall(r'\{\d+\}', key)
            if ids:
                parts = re.split(r'(\{\d+\})', key)
                chunks = []
                for index, part in enumerate(parts):
                    if not re.fullmatch(r'\{\d+\}', part):
                        chunks.append(re.escape(part)); continue
                    numeric = re.match(r'^(?:프레임|장|개|곳|컷|번째|초|배속|배|%|fps|bit|p\b)', parts[index + 1] if index + 1 < len(parts) else '')
                    chunks.append(r'([-+]?\d+(?:[.,]\d+)*)' if numeric else r'(.*?)')
                pattern = ''.join(chunks)
                self.templates.append((len(re.sub(r'\{\d+\}', '', key)), re.compile('^' + pattern + '$', re.S), value, ids))
        self.templates.sort(key=lambda row: row[0], reverse=True)

    def text(self, value, lang='en', depth=0):
        if lang != 'en' or not isinstance(value, str) or depth > 5:
            return value
        if value in self.entries:
            return self.entries[value]
        if value.strip() != value and value.strip() in self.entries:
            return value.replace(value.strip(), self.entries[value.strip()])
        for _, pattern, translated, ids in self.templates:
            match = pattern.fullmatch(value)
            if match:
                params = dict(zip(ids, match.groups()))
                return re.sub(r'\{\d+\}', lambda m: self._parameter(params.get(m.group(), m.group()), lang, depth), translated)
        # Resolve each complete line before splitting its internal separators.
        parts = re.split(r'(\n)' if '\n' in value else r'( · | — |, )', value)
        return ''.join(self.text(part, lang, depth + 1) if i % 2 == 0 and len(parts) > 1 else part for i, part in enumerate(parts))

    def _parameter(self, value, lang, depth):
        if '/' in value or '\\' in value or re.search(r'\.[a-zA-Z0-9]{2,8}$', value):
            return value
        return self.text(value, lang, depth + 1)

    def report(self, data, lang='en', field=''):
        """Translate declared report copy; keep paths, identifiers and user data."""
        if isinstance(data, list):
            return [self.report(value, lang, field) for value in data]
        if isinstance(data, dict):
            return {key: self.report(value, lang, 'labels' if field == 'labels' else key)
                    for key, value in data.items()}
        return self.text(data, lang) if field in {'message', 'summary', 'info', 'note', 'where', 'labels'} else data

    @staticmethod
    def script():
        # Escape HTML delimiters so catalog content cannot terminate a script.
        data = json.dumps(LOCALE_DATA, ensure_ascii=False).replace('<', '\\u003c')
        return LOCALE_SCRIPT.replace('/* @locale-data */ {}', data)

import re
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional, Sequence



class FrameWriter(ABC):
    @property
    @abstractmethod
    def extension(self) -> str: ...

    @property
    def params(self) -> Sequence[int]:
        return ()

    def write(self, frame: "np.ndarray", stem: Path) -> Path:
        destination = stem.with_name(stem.name + self.extension)
        if not ImageIO.write(destination, frame, self.params):
            raise FramingError(f"{self.extension} 저장 실패: {destination.name}")
        return destination


class PngWriter(FrameWriter):
    def __init__(self, compression: int = 3) -> None:
        self._compression = max(0, min(9, compression))

    @property
    def extension(self) -> str:
        return ".png"

    @property
    def params(self) -> Sequence[int]:
        return (cv2.IMWRITE_PNG_COMPRESSION, self._compression)


class JpegWriter(FrameWriter):
    """
    JPEG 는 기본이 4:2:0 이라 색 정보를 가로세로 절반으로 줄여 저장한다.
    단풍처럼 채도 높은 것이 잔가지로 얽힌 그림에서는 경계 색이 눈에 띄게 밀린다
    (재보니 경계에서 최대 51단계). 그래서 색을 줄이지 않는 4:4:4 로 저장한다.

    JPEG 는 8bit·알파 없음이 전부다. 그보다 큰 그림이 들어오면 여기서 줄여야 한다.
    """

    def __init__(self, quality: int = 95) -> None:
        self._quality = max(1, min(100, quality))

    @property
    def extension(self) -> str:
        return ".jpg"

    @property
    def params(self) -> Sequence[int]:
        return (cv2.IMWRITE_JPEG_QUALITY, self._quality,
                cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444)

    def write(self, frame: "np.ndarray", stem: Path) -> Path:
        if frame.dtype == np.uint16:
            frame = (frame.astype(np.float32) / 257.0).round().clip(0, 255).astype(np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = frame[..., :3]          # JPEG 은 투명도를 못 담는다
        return super().write(frame, stem)


def build_writers(fmt: str, quality: int) -> list[FrameWriter]:
    return {"png": [PngWriter()],
            "jpg": [JpegWriter(quality)],
            "both": [PngWriter(), JpegWriter(quality)]}[fmt]


@dataclass(frozen=True)
class ColorReading:
    """뽑은 그림이 실제로 쓰는 밝기 범위와, 영상이 스스로 말한 범위."""

    low: int
    high: int
    declared: str = ""          # 'tv'(제한) | 'pc'(전체) | '' (안 적혀 있음)

    LIMITED_LOW = 16
    LIMITED_HIGH = 235

    @property
    def looks_squeezed(self) -> bool:
        """
        눌린 그림의 확실한 자국만 잡는다 — 검정이 딱 16 에, 흰색이 딱 235 에 서 있는 것.

        "16~237 사이면 눌린 것" 같은 헐렁한 기준을 쓰면 그냥 어두운 밤 장면까지
        눌렸다고 잘못 읽어서 멀쩡한 그림을 들어 올린다. 그건 고치는 게 아니라 망치는 것이다.
        """
        black_sits_at_16 = self.LIMITED_LOW - 2 <= self.low <= self.LIMITED_LOW + 2
        white_sits_at_235 = self.LIMITED_HIGH - 3 <= self.high <= self.LIMITED_HIGH + 3
        has_real_range = (self.high - self.low) >= 100
        return black_sits_at_16 and white_sits_at_235 and has_real_range

    @property
    def mismatched(self) -> bool:
        """전체 범위라고 적어 놓고 실제로는 눌려 있는 경우 — 태그가 틀린 것이다."""
        return self.declared == "pc" and self.looks_squeezed

    @property
    def summary(self) -> str:
        said = {"tv": "제한범위", "pc": "전체범위"}.get(self.declared, "안 적힘")
        return f"밝기 {self.low}~{self.high} (영상 표기: {said})"


class LevelInspector:
    """그림이 16~235 로 눌려 있는지 재기만 한다."""

    @staticmethod
    def read(frame: "np.ndarray", declared: str = "") -> ColorReading:
        """
        채널별 최댓값이 아니라 밝기(루마)로 잰다.
        채도 높은 빨강은 한 채널만 250 을 넘길 수 있어서, 채널 최댓값으로 재면
        눌린 그림을 안 눌렸다고 잘못 읽는다.
        """
        if frame.ndim == 3:
            gray = cv2.cvtColor(frame[..., :3], cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
        scale = 257.0 if gray.dtype == np.uint16 else 1.0   # 16bit 도 0~255 자로 잰다
        low = int(np.percentile(gray, 0.1) / scale)
        high = int(np.percentile(gray, 99.9) / scale)
        return ColorReading(low=low, high=high, declared=(declared or "").strip().lower())


class LevelExpander:
    """눌린 그림(16~235)을 0~255 로 편다. 검정이 들뜨고 흰색이 탁한 것을 되돌린다."""

    LOW = 16.0
    HIGH = 235.0

    @classmethod
    def apply(cls, frame: "np.ndarray") -> "np.ndarray":
        scale = 255.0 / (cls.HIGH - cls.LOW)
        stretched = (frame.astype(np.float32) - cls.LOW) * scale
        return np.clip(stretched, 0, 255).astype(np.uint8)


@dataclass(frozen=True)
class ColorProfile:
    """
    소스가 어떤 색 규격인지에 대한 단 하나의 판단.

    한 파일을 두고 작업마다 다르게 읽으면 왔다 갔다 할 때마다 색이 밀린다.
    그래서 소스마다 이걸 한 번만 정하고, 모든 작업이 같은 걸 본다.
      · 읽을 때  : read_filter() — 표기가 틀렸으면 여기서 바로잡아 읽는다
      · 저장할 때 : tag_args()   — 읽은 그대로를 출력에 도장 찍는다
    도장을 안 찍으면 다음 도구가 제멋대로 추측하고, 그게 색이 밀리는 진짜 원인이다.
    """

    primaries: str = ""
    transfer: str = ""
    matrix: str = ""
    crange: str = ""            # 'tv' | 'pc'
    pix_fmt: str = ""
    retagged: bool = False      # 소스 표기가 틀려서 우리가 바로잡았다
    from_video: bool = False    # 이 RGB 가 영상을 디코딩해서 나온 것인가

    VAGUE = frozenset({"", "unknown", "unspecified", "reserved", "n/a"})
    ASSUMED_RANGE = "tv"        # 영상은 안 적혀 있으면 제한범위가 관례다

    # 규격이 안 적힌 영상을 ffmpeg · OpenCV 가 **실제로** 펴는 기준.
    # swscale 의 기본값은 해상도와 상관없이 늘 bt601 이다. 재서 확인했다 —
    # 1080p 무표기 파일도 bt601 로 펴진다.
    DECODER_MATRIX = "bt601"

    # --- 만들기 -----------------------------------------------------------

    @classmethod
    def from_probe(cls, info: Optional[ProbeInfo]) -> "ColorProfile":
        if info is None:
            return cls()
        clean = lambda v: "" if (v or "").lower() in cls.VAGUE else (v or "").lower()
        return cls(primaries=clean(info.color_primaries),
                   transfer=clean(info.color_transfer),
                   matrix=clean(info.color_space),
                   crange=clean(info.color_range),
                   pix_fmt=(info.pix_fmt or "").lower(),
                   from_video=True)

    def corrected(self, reading: Optional["ColorReading"]) -> "ColorProfile":
        """실제 픽셀이 표기와 어긋나면 픽셀 쪽을 믿는다. 표기는 사람이 틀리게 넣는다."""
        if reading is None or not reading.mismatched:
            return self
        return replace(self, crange="tv", retagged=True)

    # --- 소스가 무엇을 가지고 있나 -------------------------------------------

    @property
    def bits(self) -> int:
        if self.pix_fmt.startswith(('rgb48', 'bgr48', 'rgba64', 'bgra64')):
            return 16
        for depth in (16, 14, 12, 10):
            if f"p{depth}" in self.pix_fmt or f"{depth}le" in self.pix_fmt \
               or f"{depth}be" in self.pix_fmt:
                return depth
        return 8

    @classmethod
    def from_frame(cls, frame) -> "ColorProfile":
        alpha = frame.ndim == 3 and frame.shape[2] == 4
        deep = frame.dtype == np.uint16
        if frame.ndim == 2:
            pixel = 'gray16le' if deep else 'gray'
        else:
            pixel = ('bgra64le' if alpha else 'bgr48le') if deep else ('bgra' if alpha else 'bgr24')
        return cls(pix_fmt=pixel)

    @property
    def chroma(self) -> str:
        for kind in ("444", "422", "420", "411", "410"):
            if kind in self.pix_fmt:
                return kind
        if self.pix_fmt.startswith(("rgb", "bgr", "gbr", "argb", "abgr", "rgba", "bgra")):
            return "444"
        return "420"

    @property
    def has_alpha(self) -> bool:
        return self.pix_fmt.startswith(("yuva", "rgba", "bgra", "argb", "abgr",
                                        "gbrap", "ya"))

    @property
    def deep(self) -> bool:
        """8bit 4:2:0 보다 위에 있는가 — 그렇다면 함부로 내리면 안 된다."""
        return self.bits > 8 or self.chroma in ("422", "444") or self.has_alpha

    @property
    def effective_range(self) -> str:
        return self.crange or self.ASSUMED_RANGE

    # --- ffmpeg 에게 하는 말 -------------------------------------------------

    # ffprobe 가 읽어 주는 이름과 인코더가 받아 주는 이름이 늘 같지는 않다.
    # 예를 들어 RGB 소스의 매트릭스를 ffprobe 는 gbr 이라 부르는데 인코더는 rgb 만 안다.
    # 아는 값만 찍고 모르는 값은 버린다 — 못 알아듣는 이름을 넘기면 인코딩 자체가 죽는다.
    LEGAL_MATRIX = frozenset({
        "rgb", "bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco",
        "bt2020nc", "bt2020c", "smpte2085", "chroma-derived-nc",
        "chroma-derived-c", "ictcp"})
    LEGAL_PRIMARIES = frozenset({
        "bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film",
        "bt2020", "smpte428", "smpte431", "smpte432", "jedec-p22"})
    LEGAL_TRANSFER = frozenset({
        "bt709", "gamma22", "gamma28", "smpte170m", "smpte240m", "linear",
        "log100", "log316", "iec61966-2-4", "bt1361e", "iec61966-2-1",
        "bt2020-10", "bt2020-12", "smpte2084", "smpte428", "arib-std-b67"})
    RENAME = {"gbr": "rgb", "bt470bg": "bt470bg", "smpte428_1": "smpte428",
              "iec61966_2_1": "iec61966-2-1", "iec61966_2_4": "iec61966-2-4",
              "bt2020_ncl": "bt2020nc", "bt2020_cl": "bt2020c"}

    SWS_MATRIX = {"bt709": "bt709", "bt470bg": "bt601", "bt601": "bt601",
                  "smpte170m": "smpte170m", "smpte240m": "smpte240m",
                  "bt2020nc": "bt2020", "bt2020_ncl": "bt2020", "fcc": "fcc"}

    def read_filter(self) -> str:
        """
        영상을 그림(RGB)으로 펼 때 붙이는 변환식.

        표기가 틀린 파일은 -color_range 로는 안 고쳐진다. 그 값이 이미 픽셀 형식 이름에
        박혀 있어서(yuvj420p 같은) 바깥에서 하는 말을 무시하기 때문이다.
        실제로 듣는 건 scale 필터의 in_range 하나뿐이다 — 재보고 알아낸 것.

        영상→영상 재인코딩에는 필요 없다. 그쪽은 픽셀을 그대로 두고 표기만 고치면 된다.
        """
        if not self.retagged:
            return ""
        return "scale=in_range=tv:out_range=pc:flags=full_chroma_int+accurate_rnd"

    @staticmethod
    def join(*filters: str) -> list[str]:
        """비어 있지 않은 필터만 이어 붙여 -vf 인자로 만든다."""
        chain = ",".join(f for f in filters if f)
        return ["-vf", chain] if chain else []

    def range_bsf(self, encoder_args: Sequence[str]) -> list[str]:
        """
        h264 · h265 는 -color_range 만으로는 밝기 범위를 파일에 안 적을 때가 많다.
        (다른 색 정보가 하나도 없으면 그 칸 자체를 통째로 생략한다.)
        그러면 다음 도구가 또 추측하게 된다. 비트스트림에 직접 못박아 둔다.
        """
        if not self.crange:
            return []
        codec = next((a for a in encoder_args if a in ("libx264", "libx265")), "")
        name = {"libx264": "h264_metadata", "libx265": "hevc_metadata"}.get(codec)
        if not name:
            return []                       # ProRes 는 이 칸이 아예 없다. 관례상 제한범위
        flag = 1 if self.crange == "pc" else 0
        return ["-bsf:v", f"{name}=video_full_range_flag={flag}"]

    @property
    def read_matrix(self) -> str:
        """
        이 RGB 를 만들어 낸 매트릭스 (swscale 이름).

        여기서 크게 데었다. 규격이 **안 적힌** 영상을 디코더는 bt601 로 펴는데,
        되돌릴 때는 bt709 로 접고 있었다. 명암은 그대로인데 주황과 하늘색만
        눈에 띄게 밀린다 — 1080p 무표기 파일에서 픽셀의 99% 가 어긋났다.

        적혀 있으면 적힌 대로. 안 적혀 있으면 **디코더가 실제로 쓴 기준**으로.
        영상에서 온 게 아니라 진짜 그림 파일이면 되돌릴 원본이 없으니 bt709.
        """
        if self.matrix:
            return self.SWS_MATRIX.get(self.matrix, "bt709")
        return self.DECODER_MATRIX if self.from_video else "bt709"

    def to_yuv_filter(self) -> str:
        """
        RGB(이미지)를 다시 영상으로 돌릴 때 쓸 변환식.

        이걸 안 적어 주면 ffmpeg 은 RGB→YUV 를 제 기본값으로 계산해 놓고
        결과에는 딴소리를 적는다. 그 한 줄 차이로 살색과 하늘색이 밀린다.
        """
        return (f"scale=out_color_matrix={self.read_matrix}"
                f":out_range={self.effective_range}"
                ":flags=full_chroma_int+accurate_rnd")

    def tag_args(self) -> list[str]:
        """출력에 색 규격을 명시한다. 아는 것만 찍는다 — 모르는 걸 지어내지 않는다."""
        args: list[str] = []
        for flag, value, legal in (
                ("-color_primaries", self.primaries, self.LEGAL_PRIMARIES),
                ("-color_trc", self.transfer, self.LEGAL_TRANSFER),
                ("-colorspace", self.matrix, self.LEGAL_MATRIX),
                ("-color_range", self.crange, frozenset({"tv", "pc"}))):
            clean = self.RENAME.get(value, value)
            if clean and clean in legal:
                args += [flag, clean]
        return args

    def stamp_args(self) -> list[str]:
        """
        결과에 찍을 도장.

        소스가 적어 둔 게 있으면 그대로 옮긴다. 하나도 없으면 —
          · 영상을 편 프레임이면 **아무것도 안 찍는다.** 소스는 무표기인데
            결과에만 도장을 찍으면, 규격을 짐작하는 재생기에서 둘이 서로
            다른 그림이 된다. 안 적힌 것을 지어내지 않는 게 맞다.
          · 진짜 그림 파일이면 되돌릴 원본이 없으니 bt709 로 못박아 준다.
        """
        args = self.tag_args()
        if args or self.from_video:
            return args
        return ["-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709", "-color_range", "tv"]

    # --- 사람에게 하는 말 ---------------------------------------------------

    @property
    def summary(self) -> str:
        said = {"tv": "제한범위", "pc": "전체범위"}.get(self.crange, "안 적힘")
        depth = f"{self.bits}bit {self.chroma}" + (" +알파" if self.has_alpha else "")
        space = self.matrix or self.primaries or "규격 안 적힘"
        return f"{depth} · {space} · {said}"


class PixelPlan:
    """
    소스가 가진 계조·채도·알파를 깎지 않는 픽셀 형식을 고른다.

    yuv420p 로 못박아 두면 10bit 도, 4:2:2 도, 알파도 전부 조용히 사라진다.
    사라진 걸 알아채는 건 한참 뒤 합성할 때다.
    """

    LADDER = {
        8:  {"420": "yuv420p", "422": "yuv422p", "444": "yuv444p"},
        10: {"420": "yuv420p10le", "422": "yuv422p10le", "444": "yuv444p10le"},
        12: {"420": "yuv420p12le", "422": "yuv422p12le", "444": "yuv444p12le"},
    }
    SAFE = "yuv420p"            # 어디서나 열리는 최소공배수

    @classmethod
    def for_x26x(cls, profile: ColorProfile, keep_depth: bool) -> tuple[list[str], str]:
        """(픽셀 형식 인자, 사람이 읽을 한 줄). keep_depth 가 꺼져 있으면 배포용 8bit."""
        if not keep_depth:
            if profile.deep:
                return (["-pix_fmt", cls.SAFE],
                        f"{profile.bits}bit {profile.chroma} → 8bit 420 (배포용). "
                        f"계조를 지키려면 누크용 마스터로")
            return ["-pix_fmt", cls.SAFE], ""
        bits = 12 if profile.bits >= 12 else (10 if profile.bits > 8 else 8)
        chroma = profile.chroma if profile.chroma in ("420", "422", "444") else "420"
        name = cls.LADDER[bits][chroma]
        note = "" if name == cls.SAFE else f"{bits}bit {chroma} 유지"
        return ["-pix_fmt", name], note

    @classmethod
    def for_prores(cls, profile: ColorProfile) -> tuple[list[str], str]:
        """알파가 있으면 4444, 없으면 422 HQ. 알파를 422 에 넣으면 그냥 버려진다."""
        if profile.has_alpha:
            return (["-c:v", "prores_ks", "-profile:v", "4444", "-vendor", "apl0",
                     "-pix_fmt", "yuva444p10le", "-alpha_bits", "16"],
                    "ProRes 4444 (알파 유지 — 422 HQ 는 알파를 못 담습니다)")
        return (["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0",
                 "-pix_fmt", "yuv422p10le"],
                "ProRes 422 HQ")

    @classmethod
    def still_args(cls, profile: ColorProfile) -> list[str]:
        """정지 이미지로 뽑을 때. 8bit 소스는 8bit 로, 그 위는 16bit 로 받는다."""
        if profile.has_alpha:
            return ["-pix_fmt", "rgba64be" if profile.bits > 8 else "rgba"]
        return ["-pix_fmt", "rgb48be" if profile.bits > 8 else "rgb24"]


class PreciseGrab:
    """
    10bit·알파 소스에서 프레임을 뽑는 길.

    OpenCV 는 무엇을 넣든 8bit BGR 로 돌려준다. 빠르고 몇 번째 프레임인지 세기도 쉬워서
    보통은 그걸로 충분하다. 그런데 10bit ProRes 를 그렇게 뽑으면 계조가 여기서 깎이고
    알파는 아예 사라진다. 그런 소스만 ffmpeg 에게 직접 시킨다.

    번호는 이미 OpenCV 로 세어 두고 여기서는 그 번호만 다시 뽑는다.
    한 번의 디코딩으로 필요한 프레임을 전부 건진다 — 한 장씩 다시 열면 열 배 느리다.
    """

    def __init__(self, ff: "FFmpegTool", profile: ColorProfile) -> None:
        self._ff = ff
        self._profile = profile

    def available(self) -> bool:
        return bool(self._ff.find("ffmpeg"))

    def fetch(self, target: Path, indices: Sequence[int]) -> dict[int, "np.ndarray"]:
        """{프레임번호: 그림}. 실패하면 빈 사전 — 부르는 쪽이 OpenCV 로 되돌아간다."""
        wanted = sorted({max(0, int(i)) for i in indices})
        if not wanted:
            return {}
        picks = "+".join(rf"eq(n\,{i})" for i in wanted)
        workspace = Path(tempfile.mkdtemp(prefix="framing_grab_"))
        try:
            code = self._ff.run(
                ["-y", "-i", str(target),
                 *ColorProfile.join(f"select='{picks}'", self._profile.read_filter()),
                 "-fps_mode", "passthrough",
                 "-c:v", "png", *PixelPlan.still_args(self._profile), "-an",
                 str(workspace / "g.%05d.png")], quiet=True)
            made = sorted(workspace.glob("g.*.png"))
            if len(made) != len(wanted):
                # 몇 번째가 몇 번인지 확신할 수 없으면 하나도 쓰지 않는다.
                # 엉뚱한 프레임을 맞다고 내미는 것보다 원래 길로 되돌아가는 게 낫다.
                return {}
            if code != 0 and not made:
                return {}
            grabbed: dict[int, "np.ndarray"] = {}
            for number, path in zip(wanted, made):
                image = ImageIO.read(path, cv2.IMREAD_UNCHANGED)
                if image is not None:
                    grabbed[number] = image
            return grabbed
        except Exception:
            return {}                       # 어떤 이유로든 실패하면 조용히 원래 길로
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


@dataclass(frozen=True)
class HueLabel:
    """매트 패스에서 쓰인 라벨 색 하나. 색상과 채도로만 정의된다 — 밝기는 뎁스다."""

    hue: float                  # 0~360
    sat: float                  # 0~1

    NAMES = ((30, "빨강"), (70, "노랑"), (165, "초록"), (200, "청록"),
             (260, "파랑"), (330, "마젠타"), (360, "빨강"))

    @property
    def name(self) -> str:
        for edge, korean in self.NAMES:
            if self.hue < edge:
                return korean
        return "빨강"

    def __str__(self) -> str:
        return f"{self.name} (H {self.hue:.0f}° · S {self.sat * 100:.0f}%)"


class HsvBridge:
    """
    BGR 과 HSV 를 오간다. OpenCV 의 float 변환을 쓴다 — 왕복 오차가 0 이다.
    (uint8 로 돌리면 색상이 0~179 로 눌려서 라벨을 구분하기에 너무 거칠다.)
    """

    @staticmethod
    def to_hsv(bgr: "np.ndarray") -> "np.ndarray":
        top = 65535.0 if bgr.dtype == np.uint16 else 255.0
        return cv2.cvtColor(bgr[..., :3].astype(np.float32) / top, cv2.COLOR_BGR2HSV)

    @staticmethod
    def to_bgr(hsv: "np.ndarray", like: "np.ndarray") -> "np.ndarray":
        top = 65535.0 if like.dtype == np.uint16 else 255.0
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR) * top
        return np.clip(bgr, 0, top).astype(like.dtype)


class MatteReader:
    """
    소재가 매트·뎁스 패스인지 실사·페인팅인지 읽어내고, 쓰인 라벨 색을 찾아낸다.

    매트는 몇 개의 라벨색으로만 칠해져 색상이 좁은 봉우리에 몰린다.
    실사는 색상이 넓게 퍼진다. 이 차이 하나로 갈린다 — 실측 매트 83~99%, 실사 21~28%.
    잘못 판별하면 실사 소재의 색을 라벨로 뭉개 버리므로, 애매하면 실사로 본다.
    """

    BINS = 72                   # 5도 간격
    COLORED = 0.10              # 이보다 채도가 낮으면 무채색 = 뎁스
    MATTE_LINE = 0.55           # 상위 3봉우리가 이 비율을 넘으면 매트
    MIN_PIXELS = 500
    MIN_SHARE = 0.03            # 전체의 3% 미만인 색은 라벨로 치지 않는다
    NEIGHBOR = 2                # 인접 ±2 bin 은 같은 라벨로 묶는다

    def __init__(self, samples: Sequence["np.ndarray"]) -> None:
        self._hues, self._sats = self._gather(samples)

    @classmethod
    def _gather(cls, samples: Sequence["np.ndarray"]):
        hues, sats = [], []
        for frame in samples:
            if frame is None or frame.size == 0:
                continue
            small = frame
            if frame.shape[1] > 640:            # 판별에 원본 해상도는 필요 없다
                scale = 640.0 / frame.shape[1]
                small = cv2.resize(frame, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_AREA)
            hsv = HsvBridge.to_hsv(small)
            mask = hsv[..., 1] >= cls.COLORED
            if mask.any():
                hues.append(hsv[..., 0][mask])
                sats.append(hsv[..., 1][mask])
        if not hues:
            return np.empty(0, np.float32), np.empty(0, np.float32)
        return np.concatenate(hues), np.concatenate(sats)

    @property
    def concentration(self) -> float:
        if len(self._hues) < self.MIN_PIXELS:
            return 1.0                          # 색이 거의 없다 = 순수 뎁스
        hist, _ = np.histogram(self._hues, bins=self.BINS, range=(0, 360))
        return float(np.sort(hist)[-3:].sum() / max(1, hist.sum()))

    @property
    def is_matte(self) -> bool:
        if len(self._hues) < self.MIN_PIXELS:
            return True
        return self.concentration > self.MATTE_LINE

    def legend(self) -> list[HueLabel]:
        """쓰인 라벨색 목록. 무채색뿐이면 빈 목록 — 그것도 유효한 답이다."""
        if len(self._hues) < self.MIN_PIXELS:
            return []
        hist, edges = np.histogram(self._hues, bins=self.BINS, range=(0, 360))
        total = max(1, hist.sum())
        labels: list[HueLabel] = []
        used = np.zeros(self.BINS, bool)
        for slot in np.argsort(-hist):
            if hist[slot] / total < self.MIN_SHARE or used[slot]:
                continue
            near = [(slot + k) % self.BINS
                    for k in range(-self.NEIGHBOR, self.NEIGHBOR + 1)]
            if any(used[n] for n in near):
                continue
            for n in near:
                used[n] = True
            picked = self._within(edges[slot] - 12.5, edges[slot] + 17.5)
            if picked.sum() < 100:
                continue
            labels.append(HueLabel(hue=self._circular_mean(self._hues[picked]),
                                   sat=float(np.median(self._sats[picked]))))
        labels.sort(key=lambda label: -label.sat)
        return labels

    def _within(self, low: float, high: float) -> "np.ndarray":
        hues = self._hues
        if low < 0 or high > 360:               # 0도를 넘어가는 구간
            return (hues >= low % 360) | (hues < high % 360)
        return (hues >= low) & (hues < high)

    @staticmethod
    def _circular_mean(hues: "np.ndarray") -> float:
        """0도 근처를 평균 내면 180도가 나온다. 원 위에서 평균해야 한다."""
        radians = np.deg2rad(hues)
        angle = np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())
        return float(np.rad2deg(angle) % 360)


class LabelSnapper:
    """
    보간이 만들어 낸 중간색을 가장 가까운 라벨로 되돌린다.

    매트에서 빨강과 파랑 사이의 보라색 픽셀은 "반쯤 빨강"이 아니라 오류다.
    라벨은 뜻이지 색이 아니라서, 섞이는 순간 그 픽셀의 의미가 사라진다.
    명도(V)는 손대지 않는다 — 그건 뎁스이고, 뎁스는 섞여도 뜻이 유지된다.
    """

    BAND = 35.0                 # 이 각도 안쪽이면 그 라벨로 본다

    def __init__(self, legend: Sequence[HueLabel], band: float = BAND) -> None:
        self._legend = list(legend)
        self._band = band

    @property
    def describes(self) -> str:
        if not self._legend:
            return "무채색(뎁스)만 — 색이 끼면 회색으로 되돌립니다"
        return " · ".join(str(label) for label in self._legend)

    def apply(self, frame: "np.ndarray") -> "np.ndarray":
        alpha = frame[..., 3:] if frame.ndim == 3 and frame.shape[2] == 4 else None
        hsv = HsvBridge.to_hsv(frame)
        hue, sat, value = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        out_hue = np.zeros_like(hue)
        out_sat = np.zeros_like(sat)
        claimed = np.zeros(hue.shape, bool)
        for label in self._legend:
            distance = np.abs((hue - label.hue + 180) % 360 - 180)
            hit = (distance <= self._band) & (sat >= label.sat * 0.5) & ~claimed
            out_hue[hit] = label.hue
            out_sat[hit] = label.sat
            claimed |= hit
        out_sat[~claimed] = 0.0                 # 라벨이 아니면 순수 무채색
        fixed = HsvBridge.to_bgr(np.stack([out_hue, out_sat, value], -1), frame)
        if alpha is not None:
            fixed = np.concatenate([fixed, alpha], axis=2)
        return fixed


@dataclass(frozen=True)
class RetimePlan:
    """몇 프레임을 몇 프레임으로 만들 것인가. 요청 방식이 뭐였든 여기로 모인다."""

    source: int
    target: int
    fps: float
    requested: int = 0          # 사용자가 실제로 말한 숫자 (잘라내기 전)

    MAX_FRAMES = 20000          # 계산 자체가 터지지 않게 두는 상한
    HARD_CAP = 6000             # 24fps 로 4분. 그보다 길면 리타임할 물건이 아니다
    STRETCH_CAP = 20.0          # 원본의 20배 넘게 늘리는 건 오타로 본다

    @classmethod
    def decide(cls, source: int, fps: float, frames: int = 0,
               speed: float = 0.0, seconds: float = 0.0) -> "RetimePlan":
        """프레임 수 · 배속 · 초 중 무엇으로 말하든 같은 계획으로 옮긴다."""
        fps = fps if fps and fps > 0 else 24.0
        if frames > 0:
            target = frames
        elif seconds > 0:
            target = int(round(seconds * fps))
        elif speed > 0:
            target = int(round(source / speed))   # 2배속 = 절반 길이
        else:
            target = source
        asked = target
        target = max(1, min(cls.MAX_FRAMES, target))
        return cls(source=max(1, source), target=target, fps=fps, requested=asked)

    @property
    def speed(self) -> float:
        return self.source / self.target

    @property
    def slowing(self) -> bool:
        return self.target > self.source

    @property
    def unchanged(self) -> bool:
        return self.target == self.source

    @property
    def summary(self) -> str:
        return (f"{self.source}프레임 ({self.source / self.fps:.2f}초) → "
                f"{self.target}프레임 ({self.target / self.fps:.2f}초)  ·  "
                f"{self.speed:.3f}배속 ({'느리게' if self.slowing else '빠르게'})")

    @property
    def refusal(self) -> str:
        """
        감당 못 할 요청은 조용히 잘라내지 말고 거절한다.

        999999 를 20000 으로 몰래 줄여 놓고 20분 동안 디스크를 채우는 게
        제일 나쁜 응답이다. 오타는 오타라고 말해줘야 고칠 수 있다.
        """
        if self.target <= self.HARD_CAP:
            return ""
        asked = self.requested or self.target
        return (f"{asked}프레임({asked / self.fps:.0f}초)은 한 번에 하기엔 깁니다 "
                f"— {self.HARD_CAP}프레임({self.HARD_CAP / self.fps:.0f}초)까지만 합니다.\n"
                "         --frames · --seconds · --speed 를 같이 주지는 않았는지, "
                "자릿수가 맞는지 확인해 보세요.")

    @property
    def suspicious(self) -> str:
        """거절할 정도는 아니지만 오타처럼 보이는 것. 막지는 않고 말만 한다."""
        if self.target <= self.source * self.STRETCH_CAP:
            return ""
        return (f"{self.source}프레임을 {self.target}프레임으로 "
                f"({self.target / self.source:.0f}배) 늘립니다 — 오타가 아닌지 확인하세요")


class SourceFrames:
    """Consecutive identity of decoded pixels, used only for the retime plan.

    Similar thumbnails cannot prove that two original frames are duplicates.
    Hold duration and noise-tolerant detection belong to FrameAudit.
    """
    def __init__(self, frames: Sequence["np.ndarray"]) -> None:
        self._origin = self._trace(frames)

    @property
    def origin(self) -> list[int]:
        return self._origin

    @property
    def distinct(self) -> int:
        """Number of consecutive groups, not a count of globally unique images."""
        return len(set(self._origin))

    @property
    def repeated(self) -> int:
        return len(self._origin) - self.distinct

    def shortfall(self, target: int) -> int:
        return max(0, target - self.distinct)

    @staticmethod
    def _trace(frames: Sequence["np.ndarray"]) -> list[int]:
        if not len(frames): return []
        out = [0]
        for previous, current in zip(frames, frames[1:]):
            same = (previous.shape == current.shape and previous.dtype == current.dtype
                    and np.array_equal(previous, current))
            out.append(out[-1] if same else out[-1] + 1)
        return out


class RetimeTimeline:
    """One frame layout shared by rendering, RIFE selection and undo records.

    Stretching assigns every source frame a unique output slot. Intermediate
    slots use a fractional position between those anchors. Shortening retains
    uniform resampling and cannot promise restoration of discarded frames.
    """
    def __init__(self, source, target):
        self.source, self.target = source, target
        self.anchors = ([round(i * (target - 1) / (source - 1)) for i in range(source)]
                        if source > 1 and target >= source else [0])
        self.origins = {slot: i for i, slot in enumerate(self.anchors)}
        self.positions = self._positions()

    def _positions(self):
        if self.target <= 1 or self.source <= 1:
            return [0.0] * self.target
        if self.target < self.source:
            return [i * (self.source - 1) / (self.target - 1) for i in range(self.target)]
        values = [0.0] * self.target
        for i, (start, end) in enumerate(zip(self.anchors, self.anchors[1:])):
            for slot in range(start, end):
                values[slot] = i + (slot - start) / (end - start)
        values[-1] = float(self.source - 1)
        return values

    def at(self, position):
        return self.positions[position]


class RetimeReport:
    """
    결과의 어디가 원본이고 어디가 새로 만든 자리인지.

    리타임을 돌리고 나면 "그래서 어디를 건드린 건데?" 를 알 수 없었다. 길이만
    알려 줘 봐야, 문제가 생겼을 때 어느 프레임을 봐야 하는지 알 길이 없다.
    자리 계산은 엔진과 똑같은 식(_source_index)을 쓰므로 실제와 어긋나지 않는다.
    """

    NEAR = 1e-6                 # 이만큼이면 원본 자리에 정확히 떨어진 것으로 본다

    # 방식마다 "손댄 자리" 의 뜻이 달라서 부르는 이름도 다르다
    LABELS = {
        "dup":  {"kept": "새 원본 위치", "made": "프레임 재사용"},
        "hold": {"kept": "원본 그대로", "made": "끝 홀드"},
        "":     {"kept": "원본 위치", "made": "보간 위치"},
    }

    def __init__(self, plan: "RetimePlan", engine_key: str = "",
                 origin: Optional[Sequence[int]] = None) -> None:
        self._plan = plan
        self._engine = engine_key
        self._timeline = RetimeTimeline(plan.source, plan.target)
        # origin 이 있으면 번호가 아니라 **그림**으로 센다. 없으면 예전처럼 번호로.
        self._origin = list(origin) if origin else []

    def at(self, position: int) -> float:
        if self._plan.target <= 1:
            return 0.0
        if self._engine == "hold":
            # 앞은 원본을 1:1 로 쓰고, 넘어가면 마지막 프레임에 머문다
            return float(min(position, self._plan.source - 1))
        if self._engine in ("motion", "blend") and self._plan.slowing:
            return self._timeline.at(position)
        return position * (self._plan.source - 1) / (self._plan.target - 1)

    def added(self) -> list[bool]:
        """
        결과 프레임마다 True = **늘리면서 생긴** 자리. 되돌리기가 쓰는 값이다.

        `marks()` 와 헷갈리기 쉬운데 묻는 것이 다르다.

          marks()  "결과에서 더블로 보이는 자리는 어디인가"   — 보고용
          added()  "늘리면서 새로 끼워 넣은 자리는 어디인가"  — 되돌리기용

        둘이 갈리는 경우가 실제로 있었다. **원본에 이미 똑같은 두 장이 붙어
        있으면** marks() 는 그 자리도 더블로 센다(그림으로 세니까 맞는 말이다).
        그런데 그 자리는 늘리면서 생긴 게 아니다. 246 → 265 는 19장이 늘었는데
        원본 1·2번이 같은 그림이라 marks() 가 20장을 표시했고, 그 메모로
        되돌리니 **원본 한 장이 같이 빠져서 245장**이 나왔다.

        그래서 여기서는 그림을 보지 않는다. 가리키는 원본 번호가 앞과 같은
        자리, 그것만 늘리면서 생긴 자리다.
        """
        if self._engine == "hold":
            edge = min(self._plan.source, self._plan.target)
            return [position >= edge for position in range(self._plan.target)]
        if self._engine != "dup":
            return self.marks()          # 겹치기·보간은 새로 그린 자리가 곧 생긴 자리
        out, previous = [], None
        for position in range(self._plan.target):
            index = min(self._plan.source - 1, int(round(self.at(position))))
            out.append(index == previous)
            previous = index
        return out

    def marks(self) -> list[bool]:
        """
        결과 프레임마다 True = 손댄 자리.

        무엇이 "손댄 자리" 인지는 방식마다 다르다.
          복제        이웃과 같은 그림을 한 번 더 쓴 자리 (새로 그린 픽셀은 없다)
          겹치기·보간 원본 자리에 정확히 떨어지지 않아 새로 만들어야 했던 자리

        **되돌리기는 이 값을 쓰면 안 된다** — added() 를 쓴다. 위 설명 참고.
        """
        out: list[bool] = []
        if self._plan.source == 1 and self._plan.slowing:
            return [i > 0 for i in range(self._plan.target)]
        if self._engine == "hold":
            # 원본이 끝난 지점부터가 붙잡은 자리. 새로 그린 픽셀은 하나도 없다
            edge = min(self._plan.source, self._plan.target)
            return [position >= edge for position in range(self._plan.target)]
        if self._engine == "dup":
            previous = None
            for position in range(self._plan.target):
                index = min(self._plan.source - 1, int(round(self.at(position))))
                # 번호가 달라도 그림이 같으면 그건 더블이다 — 한 번 늘렸던
                # 영상을 다시 줄일 때 여기서 조용히 틀렸다
                picture = self._origin[index] if index < len(self._origin) else index
                out.append(picture == previous)
                previous = picture
            return out
        for position in range(self._plan.target):
            exact = self.at(position)
            out.append(abs(exact - round(exact)) > self.NEAR)
        return out

    @property
    def made(self) -> int:
        return sum(self.marks())

    @property
    def kept(self) -> int:
        return self._plan.target - self.made

    def already_double(self) -> int:
        """원본에 이미 같은 그림으로 붙어 있던 장수. 늘리기가 만든 것이 아니다."""
        return sum(self.marks()) - sum(self.added())

    def where(self, limit: int = 6) -> str:
        """
        늘리면서 생긴 자리를 사람 말로. 규칙적이면 규칙으로, 아니면 번호 몇 개로.

        메모·되돌리기와 같은 값(added)을 말한다. 화면이 20장이라 하고 메모가
        19장이라 적으면, 둘 중 어느 쪽이 맞는지 알 방법이 없다.
        """
        marks = self.added()
        spots = [i for i, fresh in enumerate(marks) if fresh]
        if not spots:
            return "새로 만든 프레임 없음 — 원본 프레임만 골라 썼습니다"
        if self._engine == "hold":
            return (f"{spots[0] + 1}번째부터 끝까지 {len(spots)}장 "
                    "— 마지막 프레임을 붙잡았습니다")
        if len(spots) >= len(marks) * 0.9:
            # 원본 사이를 다시 잘게 나누면 처음·끝 말고는 성한 자리가 없다.
            # 그게 정상이지만, 모르고 보면 고장으로 읽힌다.
            return (f"처음과 끝을 뺀 거의 전부 ({len(spots)}/{len(marks)}장) — "
                    f"원본 {self._plan.source}장 사이를 {self._plan.target}장으로 다시 "
                    "나누느라 눈금이 안 맞습니다")
        gaps = {b - a for a, b in zip(spots, spots[1:])}
        if len(gaps) == 1 and spots[0] < 4:
            step = gaps.pop()
            first = spots[0] + 1                   # 사람이 세는 번호는 1부터
            return f"{first}번째부터 {step}장마다 (총 {len(spots)}장)"
        head = ", ".join(str(i + 1) for i in spots[:limit])
        return f"{head}{' …' if len(spots) > limit else ''} 번째 (총 {len(spots)}장)"

    def strip(self, width: int = 48) -> str:
        """한 줄 그림. ▉ 는 원본 그대로, ░ 는 새로 만든 자리."""
        marks = self.marks()
        if not marks:
            return ""
        size = min(width, len(marks))
        cells = []
        for slot in range(size):
            start = slot * len(marks) // size
            end = max(start + 1, (slot + 1) * len(marks) // size)
            cells.append("░" if any(marks[start:end]) else "▉")
        return "".join(cells)

    @property
    def plan(self) -> "RetimePlan":
        return self._plan

    def payload(self) -> dict:
        """화면이 띠를 그릴 수 있게 넘기는 값."""
        return {"source": self._plan.source, "target": self._plan.target,
                "kept": self.kept, "made": self.made, "engine": self._engine,
                "evidence": "render-plan",
                "labels": self.LABELS.get(self._engine, self.LABELS[""]),
                "note": "프레임 배치 기록입니다. 보간 위치·재사용 횟수는 홀드 검출 결과가 아닙니다.",
                "speed": round(self._plan.speed, 4),
                "marks": self.marks()[:2000], "where": self.where()}


class RetimeNote:
    """
    같은 이야기를, 결과 옆에 놓고 가는 쪽지.

    지도는 화면에 뜨지만 창을 닫으면 사라진다. 그런데 리타임한 컷은 며칠 뒤에
    다시 열어 본다 — "이거 몇 배속이었지", "어디가 새로 그린 자리였지". 그때
    물어볼 데가 없었다. 그래서 원하면 결과 옆에 메모장 한 장을 놓는다.

    세는 일은 RetimeReport 가 이미 다 해 놨다. 여기는 그걸 사람이 읽는 글로
    옮기기만 한다 — 숫자를 다시 세면 지도와 쪽지가 어긋난다.
    """

    SUFFIX = "_리타임메모.txt"
    PER_LINE = 12               # 한 줄에 번호 열두 개 — 창에 붙여넣기 좋은 폭

    def __init__(self, report: RetimeReport, source: Path,
                 engine_title: str = "") -> None:
        self._report = report
        self._source = Path(source)
        self._title = engine_title or report.payload().get("engine", "")

    def spot(self, folder: Path) -> Path:
        return Path(folder) / (self._source.stem + self.SUFFIX)

    def text(self) -> str:
        """
        짧게. 며칠 뒤에 열어 보는 사람이 알고 싶은 건 하나다 — **어디가 새로
        생긴 자리인가.**

        예전에는 배속·띠그림·주의문까지 열댓 줄을 적었다. 정작 번호는 60개에서
        잘랐다. 읽을 것은 많고 쓸 것은 잘려 있었다. 지금은 거꾸로다: 설명을
        줄이고 번호를 **한 장도 빠짐없이** 적는다. 그래야 이 메모장을 되돌리기에
        그대로 끌어다 놓을 수 있다 (RetimeNoteText 가 이걸 다시 읽는다).
        """
        plan = self._report.plan
        made = self._report.payload()['labels']['made']
        spots = self.frames()
        lines = [
            "Framing 리타임 메모",
            "",
            f"원본   {self._source.name}",
            f"적은 때 {time.strftime('%Y-%m-%d %H:%M')}",
            f"길이   {plan.source} → {plan.target}프레임",
            f"방식   {self._title}",
            f"FRAMING_TIMELINE source={plan.source} output={plan.target} restorable={int(plan.target-len(spots)==plan.source)}",
            "",
        ]
        if not spots:
            lines.append("새로 생긴 프레임 없음 — 원본 프레임만 골라 썼습니다")
            return "\n".join(lines) + "\n"
        if plan.target - len(spots) != plan.source:
            lines.append('원본 프레임이 모두 남아 있지 않아 이 메모로 원본 길이를 복원할 수 없습니다.')
        lines.append(f"{made} {len(spots)}장 · 프레임 번호 (결과 기준, 1부터)")
        lines += self._number_lines(spots)
        return "\n".join(lines) + "\n"

    def frames(self) -> list[int]:
        """
        늘리면서 생긴 자리 — 사람이 세는 1부터.

        `marks()` 가 아니라 `added()` 다. 이 메모장은 읽는 종이가 아니라
        되돌리기에 도로 넣는 종이라서, 한 장이라도 더 적히면 원본이 깎인다.
        """
        return [i + 1 for i, fresh in enumerate(self._report.added()) if fresh]

    @classmethod
    def _number_lines(cls, spots: Sequence[int]) -> list[str]:
        """번호만 있는 줄로 적는다 — 되돌리기가 이 줄만 골라 읽는다."""
        return [", ".join(str(n) for n in spots[at:at + cls.PER_LINE])
                for at in range(0, len(spots), cls.PER_LINE)]

    def write(self, folder: Path, language='kr') -> Optional[Path]:
        """못 적어도 리타임을 실패로 만들지 않는다 — 쪽지는 곁다리다."""
        spot = self.spot(folder) if language != 'en' else Path(folder) / (self._source.stem + '_retime-notes.txt')
        try:
            spot.parent.mkdir(parents=True, exist_ok=True)
            spot.write_text(MessageCatalog().text(self.text(), language), encoding="utf-8")
        except OSError as exc:
            Console.warn(f"메모를 적지 못했습니다: {exc}")
            return None
        return spot


class RetimeEngine(ABC):
    """
    프레임 수를 바꾸는 한 가지 방법.

    셋 다 결과 장수는 정확히 같다. 다른 건 "없는 프레임을 어떻게 채우는가" 뿐이다.
    """

    KEY = ""
    TITLE = ""
    BLURB = ""
    NEEDS_FFMPEG = False

    def __init__(self, plan: RetimePlan, snapper: Optional[LabelSnapper] = None,
                 abort: Optional[Callable[[], None]] = None) -> None:
        self._plan = plan
        self._timeline = RetimeTimeline(plan.source, plan.target)
        self._snapper = snapper
        self._abort = abort or (lambda: None)

    @abstractmethod
    def render(self, frames: Sequence["np.ndarray"],
               write: Callable[[int, "np.ndarray"], None]) -> int: ...

    def _finish(self, frame: "np.ndarray") -> "np.ndarray":
        return self._snapper.apply(frame) if self._snapper is not None else frame

    def _source_index(self, position: int) -> float:
        """결과 몇 번째 프레임이 원본의 몇 번째 자리에 해당하는가 (소수점 포함)."""
        if self._plan.target <= 1:
            return 0.0
        if self.KEY in ('motion', 'blend') and self._plan.slowing:
            return self._timeline.at(position)
        return position * (self._plan.source - 1) / (self._plan.target - 1)


class DuplicateEngine(RetimeEngine):
    """가장 가까운 원본 프레임을 그대로 쓴다. 픽셀 연산이 없어 매트가 100% 남는다."""

    KEY = "dup"
    TITLE = "복제"
    BLURB = "원본 프레임을 그대로 골라 씁니다. 색이 절대 섞이지 않습니다"

    def render(self, frames, write) -> int:
        for position in range(self._plan.target):
            self._abort()
            index = min(len(frames) - 1, int(round(self._source_index(position))))
            write(position, frames[index])      # 라벨 복원조차 필요 없다 — 원본이니까
        return self._plan.target


class TailHoldEngine(RetimeEngine):
    """
    앞은 원본을 1:1 로 쓰고, 모자란 뒤는 마지막 프레임을 붙잡는다.

    꼬리가 이미 멈춰 있거나 타임라인 길이만 맞추면 되는 자리에서는 이쪽이
    제일 깨끗하다. 새로 그리는 픽셀이 하나도 없고, 원본 구간의 타이밍이
    한 프레임도 밀리지 않는다. 복제는 늘린 만큼을 전 구간에 흩뿌리지만
    홀드는 꼬리 한 군데에만 모아 둔다.

    줄이는 일에는 쓸 수 없다 — 붙잡을 자리가 없다.
    """

    KEY = "hold"
    TITLE = "끝 홀드"
    BLURB = "원본을 1:1 로 쓰고 모자란 길이만 마지막 프레임으로 채웁니다"

    def _source_index(self, position: int) -> float:
        return float(min(position, self._plan.source - 1))

    def render(self, frames, write) -> int:
        last = len(frames) - 1
        for position in range(self._plan.target):
            self._abort()
            write(position, frames[min(position, last)])
        return self._plan.target


class BlendEngine(RetimeEngine):
    """이웃한 두 프레임을 섞는다. 부드럽지만 겹쳐 보인다 (디졸브)."""

    KEY = "blend"
    TITLE = "겹치기"
    BLURB = "앞뒤 프레임을 비율대로 섞습니다. 잔상이 남지만 실패하지 않습니다"

    def render(self, frames, write) -> int:
        for position in range(self._plan.target):
            self._abort()
            exact = self._source_index(position)
            first = int(np.floor(exact))
            second = min(len(frames) - 1, first + 1)
            weight = float(exact - first)
            if weight < 1e-6 or first == second:
                write(position, frames[first])
                continue
            mixed = cv2.addWeighted(frames[first], 1.0 - weight,
                                    frames[second], weight, 0.0)
            write(position, self._finish(mixed))
        return self._plan.target


class MotionEngine(RetimeEngine):
    """
    ffmpeg 의 모션 보간. 물체가 어디로 갔는지 추정해 없던 프레임을 그린다.

    잘 되면 제일 매끄럽고, 안 되면 물체가 녹거나 찢어진다.
    빠른 움직임 · 가림 · 모션블러에 약하다. 그래서 결과를 꼭 보고 판단해야 한다.
    """

    KEY = "motion"
    TITLE = "모션 보간"
    BLURB = "움직임을 추정해 없던 프레임을 그립니다. 제일 부드럽지만 찢어질 수 있습니다"
    NEEDS_FFMPEG = True

    FILTER = "minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"

    @staticmethod
    def supports(frame) -> bool:
        # A 16-bit PNG output cannot recover depth lost inside minterpolate.
        # Grayscale must also retain its channel layout throughout a sequence.
        return frame.dtype == np.uint8 and frame.ndim == 3 and frame.shape[2] in (3, 4)

    @staticmethod
    def _still(deep: bool, alpha: bool) -> str:
        """소스가 가진 것을 중간 그림에서 잃지 않게. 여기서 깎이면 되돌릴 수 없다."""
        if alpha:
            return "rgba64be" if deep else "rgba"
        return "rgb48be" if deep else "rgb24"

    def __init__(self, plan, snapper=None, abort=None, ff=None, workspace=None):
        super().__init__(plan, snapper, abort)
        self._ff = ff
        self._workspace = workspace

    def render(self, frames, write) -> int:
        if len(frames) < 2 or self._plan.target <= 1 or self._plan.unchanged:
            for position in range(self._plan.target):
                self._abort()
                write(position, frames[int(round(self._source_index(position)))])
            return self._plan.target
        if not self.supports(frames[0]):
            raise FramingError('모션 보간은 8비트 컬러 입력을 지원합니다. 계조와 채널을 유지하려면 겹치기를 사용하세요.')
        stage = self._workspace / "_in"
        stage.mkdir(parents=True, exist_ok=True)
        first = frames[0]
        deep = first.dtype == np.uint16
        alpha = first.ndim == 3 and first.shape[2] == 4
        if alpha:
            Console.info("알파는 보간해도 가장자리가 무릅니다 — 결과를 꼭 확인하세요")
        for number, frame in enumerate(frames, 1):
            self._abort()
            if not ImageIO.write(stage / f"s.{number:06d}.png", frame):
                raise FramingError('보간 입력 프레임을 저장하지 못했습니다.')
        made = self._interpolate(stage, deep, alpha)
        if not made:
            raise FramingError("모션 보간에 실패했습니다.")
        written = 0
        for position in range(self._plan.target):
            self._abort()
            exact = self._source_index(position)
            if abs(exact - round(exact)) < 1e-6:
                write(position, frames[int(round(exact))])
                written += 1
                continue
            if position < len(made):
                frame = ImageIO.read(made[position], cv2.IMREAD_UNCHANGED)
            else:
                frame = None
            if frame is None:
                raise FramingError(f'보간 프레임 {position + 1}을 만들지 못했습니다. 다시 시도해 주세요.')
            else:
                write(position, self._finish(frame))
            written += 1
        return written

    def _interpolate(self, stage: Path, deep: bool, alpha: bool) -> list[Path]:
        out = self._workspace / "_interp"
        out.mkdir(parents=True, exist_ok=True)
        fps = self._plan.fps
        # 먼저 시간을 늘렸다 줄인 뒤, 그 위에서 목표 fps 로 다시 그린다.
        ratio = (self._plan.target - 1) / max(1, self._plan.source - 1)
        # Fine timebase avoids rounding fractional source positions to the input FPS.
        # Tail context lets minterpolate emit the final interval instead of dropping it.
        stretch = f"settb=AVTB,setpts=PTS*{ratio:.12f}"
        if self._plan.slowing:
            # ffmpeg round() rounds positive halves upward; implement ties-to-even
            # with integer modulo so input anchors match the shared layout exactly.
            numerator = f'N*{self._plan.target - 1}'
            denominator = self._plan.source - 1
            base = f'floor(({numerator})/{denominator})'
            remainder = f'mod({numerator},{denominator})'
            expression = f'{base}+if(gt(2*{remainder},{denominator}),1,if(eq(2*{remainder},{denominator}),mod({base},2),0))'
            stretch = f"settb=AVTB,setpts='({expression})/({FrameRate.from_value(fps).text})/TB'"
        chain = (f"{stretch},tpad=stop_mode=clone:stop_duration={max(2/fps, 2*ratio/fps):.9f},"
                 f"{self.FILTER.format(fps=FrameRate.from_value(fps).text)}")
        code = self._ff.run(["-y", "-framerate", FrameRate.from_value(fps).text,
                             "-start_number", "1", "-i", str(stage / "s.%06d.png"),
                             "-vf", chain, "-frames:v", str(self._plan.target),
                             "-c:v", "png", "-pix_fmt", self._still(deep, alpha),
                             str(out / "i.%06d.png")], quiet=True)
        made = sorted(out.glob("i.*.png"))
        if code != 0:
            return []
        return made


@dataclass(frozen=True)
class FramePick:
    """뽑을 지점 하나. label 이 파일 이름에 들어간다."""
    label: str
    index: int
    prefer_tail: bool = False        # 마지막 프레임이면 별도 경로로 잡는다


class PickSpec:
    """
    "어디를 뽑을까" 를 적은 문자열을 실제 프레임 번호로 옮긴다.

    쓸 수 있는 표기 (쉼표나 공백으로 구분)
      first / middle / last      처음 · 중간 · 마지막
      1  120  999                프레임 번호 (1부터 — 사람이 세는 번호)
      -1  -30                    뒤에서부터. -1 이 마지막
      25%  50%                   길이 기준 백분율
      3s  1:23  0:05.5           시간. 콜론이나 s 가 있어야 시간으로 본다
    """

    ALIASES = {
        'first': 'first', 'start': 'first', '처음': 'first', '첫': 'first',
        'middle': 'middle', 'mid': 'middle', '중간': 'middle',
        'last': 'last', 'end': 'last', '끝': 'last', '마지막': 'last',
    }
    DEFAULT_TEXT = 'first,middle,last'
    MAX_PICKS = 200                  # 이보다 많으면 Sequence 를 쓰는 게 맞다

    def __init__(self, text: str = '', every: int = 0, count: int = 0) -> None:
        self._text = (text or '').strip()
        self._every = max(0, int(every or 0))
        self._count = max(0, int(count or 0))

    @property
    def is_default(self) -> bool:
        return not self._text and not self._every and not self._count

    def summary(self) -> str:
        if self._text:
            return self._text
        if self._every:
            return f'매 {self._every}프레임'
        if self._count:
            return f'균등 {self._count}장'
        return self.DEFAULT_TEXT

    # --- 풀어내기 ---------------------------------------------------------

    def resolve(self, total: int, fps: float) -> list[FramePick]:
        if total <= 0:
            raise FramingError('프레임 수를 알 수 없습니다.')
        raw = self._raw_picks(total, fps)

        seen: dict[int, FramePick] = {}
        for pick in raw:
            index = max(0, min(total - 1, pick.index))
            if index != pick.index:
                Console.warn(f'{pick.label} 는 범위를 벗어나 {FrameNumber.show(index)} 로 맞췄습니다')
            fixed = FramePick(pick.label, index, pick.prefer_tail or index == total - 1)
            seen.setdefault(index, fixed)

        picks = [seen[i] for i in sorted(seen)]
        if len(picks) > self.MAX_PICKS:
            Console.warn(f'{len(picks)}장은 너무 많아 앞에서 {self.MAX_PICKS}장만 뽑습니다. '
                         '전부 필요하면 Sequence 를 쓰세요.')
            picks = picks[:self.MAX_PICKS]
        if not picks:
            raise FramingError('뽑을 지점이 하나도 없습니다.')
        return picks

    def _raw_picks(self, total: int, fps: float) -> list[FramePick]:
        if self._text:
            return [self._parse(token, total, fps)
                    for token in re.split(r'[,\s]+', self._text) if token]
        if self._every:
            return [FramePick(f'f{i:05d}', i) for i in range(0, total, self._every)]
        if self._count:
            return self._even_spread(self._count, total)
        return [self._parse(t, total, fps) for t in self.DEFAULT_TEXT.split(',')]

    @staticmethod
    def _even_spread(count: int, total: int) -> list[FramePick]:
        if count <= 1:
            return [FramePick('middle', total // 2)]
        step = (total - 1) / (count - 1)
        return [FramePick(f'f{round(i * step):05d}', round(i * step)) for i in range(count)]

    def _parse(self, token: str, total: int, fps: float) -> FramePick:
        raw = token.strip().lower()
        if not raw:
            raise FramingError('빈 항목이 있습니다.')

        alias = self.ALIASES.get(raw)
        if alias == 'first':
            return FramePick('first', 0)
        if alias == 'middle':
            return FramePick('middle', total // 2)
        if alias == 'last':
            return FramePick('last', total - 1, True)

        if raw.endswith('%'):
            try:
                pct = float(raw[:-1])
            except ValueError:
                raise FramingError(f'백분율을 못 읽었습니다: {token}')
            index = round((total - 1) * pct / 100)
            return FramePick(f'p{int(round(pct)):03d}', index)

        seconds = self._as_seconds(raw)
        if seconds is not None:
            if fps <= 0:
                raise FramingError('프레임레이트를 몰라 시간으로 지정할 수 없습니다.')
            return FramePick(f't{raw.replace(":", "-").replace(".", "_")}',
                             round(seconds * fps))

        try:
            number = int(raw)
        except ValueError:
            raise FramingError(
                f'무슨 뜻인지 모르겠습니다: {token}\n'
                '         first / middle / last, 프레임 번호, 25%, 1:23 같은 식으로 적으세요.')
        if number < 0:
            index = total + number
            return FramePick(f'f{max(0, index):05d}', index, number == -1)
        # 사람이 적는 번호는 1부터. 파일 이름에도 적은 그대로 남긴다 —
        # "8번을 뽑았는데 파일은 f00007" 이면 그 자리에서 헷갈린다.
        if number < FrameNumber.BASE:
            raise FramingError(
                f'프레임 번호는 {FrameNumber.BASE}부터입니다: {token}\n'
                '         뒤에서부터 세려면 -1 처럼 음수로 적으세요.')
        return FramePick(f'f{number:05d}', FrameNumber.read(number))

    @staticmethod
    def _as_seconds(raw: str) -> Optional[float]:
        """1:23, 0:05.5, 12s 를 초로. 시간 표기가 아니면 None."""
        if raw.endswith('s') and raw[:-1].replace('.', '', 1).isdigit():
            return float(raw[:-1])
        if ':' not in raw:
            return None
        parts = raw.split(':')
        if len(parts) > 3:
            return None
        try:
            values = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for value in values:
            seconds = seconds * 60 + value
        return seconds




class Job(ABC):
    """작업 하나. 이름·설명·입력 종류·필요 도구를 스스로 안다."""

    KEY = ""
    TITLE = ""
    HINT = ""
    NOTE = ""                        # 고르기 전에 알아둘 것 (메뉴에서 보여줌)
    ALIASES: tuple[str, ...] = ()
    NEEDS_FFMPEG = False
    ACCEPTS_FOLDER = False           # 시퀀스 폴더를 입력으로 받는가
    ACCEPTS_IMAGE = False            # 낱장 이미지도 대상이 되는가 (Compare)
    PAIRED = False                   # 한 번에 두 개를 짝지어 받는가 (Compare)
    GROUPED = False                  # 전부를 한 번에 받는가 (Rename — 번호가 전체를 관통한다)

    def __init__(self, options: Options, ff: FFmpegTool) -> None:
        self._opt = options
        self._ff = ff
        self._colors: dict[str, ColorProfile] = {}    # 소스마다 한 번만 판단한다
        # 앱에서 중단 버튼을 누르면 이걸 갈아끼운다. CLI 에서는 늘 False.
        self.should_stop: Callable[[], bool] = lambda: False
        # 앱이 있으면 결과를 창 안에서 보여준다. 없으면 None — 그때만 밖으로 연다.
        self.show_inline: Optional[Callable[[str, dict], bool]] = None

    # --- 색 규격 ------------------------------------------------------------

    def _color(self, target: Path, info: Optional[ProbeInfo] = None,
               sample: Optional["np.ndarray"] = None) -> ColorProfile:
        """
        이 소스의 색 규격. 작업마다 다시 판단하지 않는다 — 그러면 서로 어긋난다.

        표기를 그대로 믿되, 실제 픽셀이 표기와 어긋나면 픽셀 쪽을 믿는다.
        (전체범위라고 적어 놓고 16~235 만 쓰는 파일이 실제로 흔하다.)
        """
        key = str(target)
        if key in self._colors:
            return self._colors[key]
        if info is None and self._ff.find("ffprobe"):
            info = self._ff.probe(target)
        profile = ColorProfile.from_probe(info)
        if (self._opt.levels or "auto").lower() != "keep":
            if sample is None and profile.crange == "pc" and not target.is_dir():
                sample = self._peek(target)     # 의심스러울 때만 한 장 열어 본다
            if sample is not None:
                profile = profile.corrected(LevelInspector.read(sample, profile.crange))
        self._colors[key] = profile
        return profile

    @staticmethod
    def _peek(target: Path) -> Optional["np.ndarray"]:
        """첫 그림 한 장. 표기가 'pc' 인 파일에서만 부른다 — 그 외엔 볼 이유가 없다."""
        try:
            if target.suffix.lower() in IMAGE_EXTENSIONS:
                return ImageIO.read(target)
            with VideoSource(target) as source:
                frame, _index = source.read_at(0)
                return frame
        except Exception:
            return None

    def _announce_color(self, profile: ColorProfile) -> None:
        Console.info(f"색 규격  {profile.summary}")
        if profile.retagged:
            Console.warn("전체범위라고 적혀 있는데 실제로는 눌려 있습니다 "
                         "— 제한범위로 바로잡아 읽고, 결과에도 그렇게 적어 둡니다")

    def _abort_if_asked(self) -> None:
        if self.should_stop():
            raise FramingError('중단했습니다.')

    @abstractmethod
    def run(self, target: Path) -> JobResult: ...

    # --- 도우미 -----------------------------------------------------------

    def _output_dir(self, target: Path, suffix: str) -> Path:
        base = target if target.is_dir() else target.with_suffix("")
        folder = self._opt.out_dir or Path(str(base) + suffix)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _output_file(self, target: Path, suffix: str, ext: str) -> Path:
        folder = self._opt.out_dir or target.parent
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{target.stem}{suffix}{ext}"
        if dest.resolve() == target.resolve():
            dest = folder / f"{target.stem}{suffix}_1{ext}"
        return dest if self._opt.overwrite else unique_path(dest)




# 창·바로가기 아이콘은 app/icon/framing.ico (tools/make_logo.py 가 만든다)
APP_ICON = Path(__file__).resolve().parent.parent / "icon" / "framing.ico"


def app_icon_bytes() -> Optional[bytes]:
    """창·바로가기에 쓸 아이콘(ico). 없으면 None — 아이콘은 안 건드린다."""
    try:
        return APP_ICON.read_bytes() if APP_ICON.is_file() else None
    except OSError:
        return None

"""Validated comparison exports from original media and a frozen viewer state."""
from dataclasses import dataclass
from pathlib import Path
import json
import math






"""Streaming media checks with bounded memory and explicit candidate intervals."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import math
import re





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


from pathlib import Path

import html
import re
from urllib.parse import quote

class ComparePage:
    """
    브라우저에서 직접 끌어 보는 대조 뷰어를 만든다.

    영상으로 구운 대조는 정해진 속도로만 지나간다. 정작 보고 싶은 건
    "이 부분에서 경계를 천천히 왔다 갔다" 인데 그게 안 된다.
    그래서 바를 손으로 끄는 페이지를 같이 만든다. 두 파일 옆에 두면 바로 열린다.
    """

    NAME = "대조.html"

    # 엔진 쪽 이름과 화면 쪽 이름이 다르다. 여기서 한 번만 맞춰 둔다.
    MODES = {"wipe": "wipe", "overlay": "fade", "flicker": "flip", "side": "side"}

    @classmethod
    def engine_key(cls, shown: str) -> str:
        """
        화면이 말하는 이름을 엔진 이름으로 되돌린다.

        이 되돌리는 길이 없어서 오래 틀려 있었다. 대조 창에서 겹치기(fade)나
        번갈아(flip)를 보다가 "mp4 로 저장" 을 누르면, 엔진은 그런 이름을 몰라
        조용히 첫 번째 보기(반반 바)를 구웠다. 보고 있던 것과 다른 게 나왔다.
        """
        for key, name in cls.MODES.items():
            if name == shown:
                return key
        return shown          # 이미 엔진 이름이면 그대로

    HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Framing 대조 — __TITLE__</title><style>
:root{--bg:#0b0d0f;--fg:#e8eaed;--dim:#8b949e;--line:#242a31;--accent:#e0a458}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,
 "Segoe UI","Malgun Gothic",sans-serif;display:flex;flex-direction:column;min-height:100vh}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;
 gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:14px;margin:0;letter-spacing:1px}
.tag{color:var(--dim);font-size:12px}
.modes{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap;align-items:center}
button{background:#0d0f11;border:1px solid var(--line);color:var(--dim);
 border-radius:999px;padding:5px 13px;font-size:12.5px;cursor:pointer;font-family:inherit}
button.on{border-color:var(--accent);color:var(--accent);background:#1d1a15}
/* 고른 보기에 딸린 조절칸. 필요할 때만 나타난다 — 늘 떠 있으면 머리띠만 복잡해진다. */
.sub{display:none;align-items:center;gap:7px;color:var(--dim);font-size:12px;
 padding:0 4px 0 12px;border-left:1px solid var(--line);margin-left:4px}
.sub.show{display:flex}
.sub button{padding:4px 11px;font-size:12px}
input[type=range]{width:130px;accent-color:var(--accent)}
main{flex:1;display:flex;align-items:center;justify-content:center;padding:18px;
 min-height:0;overflow:hidden}
#stage{transform-origin:center center}
#stage{position:relative;line-height:0;border:0;outline:1px solid var(--line);border-radius:0;
 overflow:hidden;cursor:ew-resize;background:#000;touch-action:none}
#base,#top{position:absolute;top:0;left:0;width:100%;height:100%;overflow:hidden;pointer-events:none;background:#000}
#base>*,#top>*{position:absolute;max-width:none;max-height:none;object-fit:contain;object-position:center;background:#000;user-select:none;pointer-events:none}
#bar{position:absolute;top:0;bottom:0;width:2px;background:var(--accent);
 pointer-events:none;box-shadow:0 0 8px rgba(224,164,88,.6)}
#bar::after{content:"";position:absolute;top:50%;left:50%;width:26px;height:26px;
 margin:-13px 0 0 -13px;border:2px solid var(--accent);border-radius:50%;background:rgba(11,13,15,.7)}
body.side #stage{cursor:default}
body.side #base{width:50%}
body.side #top{left:50%;width:50%;opacity:1!important}
body.solo #stage{cursor:default}
/* 재생 막대 — 화면 아래에 하나만 두고 A · B 를 함께 움직인다 */
#tape{display:flex;align-items:center;gap:12px;padding:9px 18px;
 border-top:1px solid var(--line)}
#tape[hidden]{display:none}
#tape button{width:38px;text-align:center;padding:5px 0}
#seek{flex:1;width:auto;accent-color:var(--accent)}
#time{color:var(--dim);font:12px ui-monospace,monospace;min-width:160px;text-align:right}
footer{padding:11px 18px;border-top:1px solid var(--line);color:var(--dim);font-size:12px;
 display:flex;gap:16px;flex-wrap:wrap}
kbd{background:#0d0f11;border:1px solid var(--line);border-radius:4px;padding:1px 6px;
 font-family:ui-monospace,monospace;font-size:11px;color:var(--fg)}
.name{color:var(--accent)}
[hidden]{display:none!important}
#pan{flex:1;min-height:180px;touch-action:none}
.stage-label{position:absolute;top:8px;z-index:2;background:#080b0fbb;border:1px solid #79828c;padding:7px 9px;line-height:1;font-weight:bold;pointer-events:none}
#leftLabel{left:8px}#rightLabel{right:8px}
#stage>video,#stage>img,#top>*{user-select:none;pointer-events:none}
#tape{flex-wrap:wrap}#tape button{width:auto;padding:5px 9px}
#stage{flex-shrink:0;box-sizing:content-box}
button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.region-box{position:absolute;pointer-events:none;border:1px solid #63d4e8;z-index:3;box-shadow:0 0 0 1px #0008}
#regionFields{display:flex;align-items:center;gap:8px;padding:7px 18px;flex-wrap:wrap;border-bottom:1px solid var(--line);color:var(--dim);font-size:12px}
#tape select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:4px}
#regionFields input{width:72px;background:var(--bg);border:1px solid var(--line);border-radius:5px;color:var(--fg);padding:4px}
#stage.select-region{cursor:crosshair}#regionHint{color:#63d4e8}
</style></head><body>
<header>
  <h1>Framing 대조</h1>
  <span class="tag">A <span class="name" translate="no">__A__</span> &nbsp;·&nbsp; B <span class="name" translate="no">__B__</span></span>
  <div class="modes">
    <button data-mode="wipe">반반 바</button>
    <button data-mode="fade">겹치기</button>
    <button data-mode="flip">번갈아</button>
    <button data-mode="side">나란히</button>
    <button data-mode="a">A만</button><button data-mode="b">B만</button>
    <button id="regionPick">영역 선택</button><button id="regionOnly" disabled>영역만 보기</button><button id="regionReset" hidden>영역 해제</button><button id="swap">A ↔ B</button><button id="fit">화면 맞춤</button><button id="actual">1:1</button>
    <span class="sub" id="splitWrap">경계
      <input type="range" id="split" aria-label="대조 경계" min="0" max="100" value="50" step="1">
      <span id="splitNum">50%</span><button id="center">중앙</button></span>
    <span class="sub" id="mixWrap">섞기
      <input type="range" id="mix" aria-label="B 혼합 비율" min="0" max="100" value="50">
      <span id="mixNum">50%</span></span>

  </div>
</header>
<div id="regionFields" hidden>
  <span id="regionHint">A 기준 픽셀 · 화면에서 끌어 선택</span>
  <label>X <input id="regionX" type="number" min="0" step="1" value="0"></label>
  <label>Y <input id="regionY" type="number" min="0" step="1" value="0"></label>
  <label>너비 <input id="regionW" type="number" min="1" step="1"></label>
  <label>높이 <input id="regionH" type="number" min="1" step="1"></label>
</div>
<main id="pan"><div id="stage" tabindex="0" aria-label="대조 화면" data-w="__W__" data-h="__H__" data-fps="__FPS__"><div id="base">__BOTTOM__</div><div id="top">__TOP__</div><div id="bar"></div><span id="leftLabel" class="stage-label">A</span><span id="rightLabel" class="stage-label">B</span></div></main>
<div id="tape" hidden>
  <button id="play" title="재생 · 멈춤 (Space)">▶</button>
  <button id="backFrame" aria-label="이전 프레임">−1f</button><button id="nextFrame" aria-label="다음 프레임">+1f</button>
  <input type="range" aria-label="재생 위치" id="seek" min="0" max="1000" value="0" step="1">
  <label>소리 <select id="audioSource" aria-label="대조 소리 · 저장에도 적용">
    <option value="a">A 소리</option><option value="b">B 소리</option><option value="none">소리 끄기</option>
  </select></label>
  <label>음량 <input id="audioVolume" aria-label="대조 음량" type="range" min="0" max="100" value="100" step="5" style="width:80px"></label>
  <span id="audioHint" role="status"></span>
  <span id="time">0:00 / 0:00</span>
</div>
<footer>
  <span>바를 끌거나 <kbd>←</kbd> <kbd>→</kbd> 로 경계를 옮깁니다 (<kbd>Shift</kbd> 로 미세 조정)</span>
  <span><kbd>Space</kbd> 재생 · 멈춤</span>
  <span><kbd>1</kbd>~<kbd>4</kbd> 보기 · <kbd>A</kbd> <kbd>B</kbd> 한쪽만 · <kbd>S</kbd> 맞바꾸기</span>
  <span>휠 확대 · 오른쪽 끌어 이동 · <kbd>,</kbd> <kbd>.</kbd> 한 프레임씩 · <kbd>Esc</kbd> 닫기</span>
  <span id="pos">50%</span>
  <span id="zoomNum"></span>
</footer>
<script>
__LOCALIZATION__
(() => {
  let language = /* @viewer-language */ "kr";
  const t = text => Messages.text(text, language);
  Messages.protect([...document.querySelectorAll('.name')].map(node => node.textContent));
  const setLanguage = value => {
    const previous = language;
    language = value === 'en' ? 'en' : 'kr';
    Messages.refresh(document.documentElement, language, previous);
    document.documentElement.lang = language === 'kr' ? 'ko' : 'en';
    document.title = t('Framing 대조') + ' — ' + [...document.querySelectorAll('.name')].map(node => node.textContent).join(' ↔ ');
  };
  setLanguage(language);
  addEventListener('message', event => {
    if (event.source === parent && event.data?.framing === 'language') setLanguage(event.data.lang);
  });
  class CompareRegionSelector {
    constructor(viewer) {
      this.viewer=viewer;this.value=null;this.editing=false;this.only=false;this.drag=null;
      this.boxes=[0,1].map(()=>{const n=document.createElement('div');n.className='region-box';n.hidden=true;viewer.stage.append(n);return n;});
      viewer.node('regionPick').onclick=()=>{this.editing=!this.editing;if(this.editing)this.only=false;this.paint();viewer.fit();viewer.publish();};
      viewer.node('regionOnly').onclick=()=>{if(!this.value)return;this.only=!this.only;this.editing=false;viewer.zoom=1;viewer.actualSize=false;viewer.panX=viewer.panY=0;this.paint();viewer.fit();viewer.publish();};
      viewer.node('regionReset').onclick=()=>{this.value=null;this.editing=false;this.only=false;this.paint();viewer.fit();viewer.publish();};
      ['X','Y','W','H'].forEach(key=>viewer.node('region'+key).onchange=()=>{
        const [w,h]=viewer.size(),read=k=>Number(viewer.node('region'+k).value);
        const x=Math.max(0,Math.min(w-1,Math.round(read('X')||0))),y=Math.max(0,Math.min(h-1,Math.round(read('Y')||0)));
        this.value={x,y,width:Math.max(1,Math.min(w-x,Math.round(read('W')||1))),height:Math.max(1,Math.min(h-y,Math.round(read('H')||1)))};
        this.paint();viewer.fit();viewer.publish();
      });
      const stage=viewer.stage;
      stage.addEventListener('pointerdown',e=>{
        if(!this.editing||e.button!==0||this.drag)return;
        e.preventDefault();e.stopImmediatePropagation();stage.setPointerCapture(e.pointerId);
        const box=stage.getBoundingClientRect(),wide=viewer.mode==='side'?2:1;
        const pane=wide===2&&e.clientX>=box.left+box.width/2?1:0;
        this.drag={id:e.pointerId,pane,start:this.point(e,pane)};this.move(e);
      });
      stage.addEventListener('pointermove',e=>{if(this.drag?.id===e.pointerId){e.stopImmediatePropagation();this.move(e);}});
      const end=e=>{if(this.drag?.id===e.pointerId){this.drag=null;if(stage.hasPointerCapture(e.pointerId))stage.releasePointerCapture(e.pointerId);viewer.publish();}};
      ['pointerup','pointercancel','lostpointercapture'].forEach(k=>stage.addEventListener(k,end));
    }
    point(e,pane) {
      const v=this.viewer,b=v.stage.getBoundingClientRect(),[w,h]=v.size(),pw=b.width/(v.mode==='side'?2:1);
      return [Math.max(0,Math.min(w,Math.round((e.clientX-b.left-pane*pw)/pw*w))),Math.max(0,Math.min(h,Math.round((e.clientY-b.top)/b.height*h)))];
    }
    move(e) {
      const [w,h]=this.viewer.size(),a=this.drag.start,b=this.point(e,this.drag.pane);
      const x=Math.min(w-1,Math.min(a[0],b[0])),y=Math.min(h-1,Math.min(a[1],b[1]));
      this.value={x,y,width:Math.max(1,Math.max(a[0],b[0])-x),height:Math.max(1,Math.max(a[1],b[1])-y)};
      this.paint();this.viewer.publish();
    }
    paint() {
      const v=this.viewer,[w,h]=v.size(),r=this.value,wide=v.mode==='side'?2:1;
      v.stage.classList.toggle('select-region',this.editing);
      v.node('regionPick').classList.toggle('on',this.editing);v.node('regionPick').setAttribute('aria-pressed',String(this.editing));
      v.node('regionFields').hidden=!this.editing&&!r;v.node('regionReset').hidden=!this.editing&&!r;v.node('regionReset').disabled=!r;
      this.boxes.forEach((box,i)=>{box.hidden=!r||i>=wide||this.only;if(r)Object.assign(box.style,{left:((r.x/w+i)/wide*100)+'%',top:r.y/h*100+'%',width:r.width/w/wide*100+'%',height:r.height/h*100+'%'});});
      v.node('regionOnly').disabled=!r;v.node('regionOnly').textContent=t(this.only?'전체 보기로':'영역만 보기');v.node('regionOnly').classList.toggle('on',this.only);v.node('regionOnly').setAttribute('aria-pressed',String(this.only));
      const values=r||{x:0,y:0,width:w,height:h};
      Object.entries({X:'x',Y:'y',W:'width',H:'height'}).forEach(([key,field])=>{v.node('region'+key).value=values[field];});
    }
  }
  class CompareAudio {
    constructor(viewer) {
      this.viewer=viewer;this.media=viewer.media;
      this.available=__AUDIO__;
      this.select=viewer.node('audioSource');this.volume=viewer.node('audioVolume');
      this.select.value=this.available[0]?'a':this.available[1]?'b':'none';
      ['a','b'].forEach((key,i)=>this.select.querySelector(`[value="${key}"]`).disabled=!this.available[i]);
      this.select.onchange=()=>{this.apply();viewer.publish();};
      this.volume.oninput=()=>{this.apply();viewer.publish();};
      this.apply();
    }
    get source(){return this.select.value;}
    get level(){return Number(this.volume.value)/100;}
    apply(){
      this.media.forEach((media,i)=>{media.muted=this.source!==['a','b'][i];media.volume=this.level;});
      this.viewer.node('audioHint').textContent=t(this.available.some(Boolean)?'선택한 소리로 MP4 저장':'두 파일에 오디오가 없습니다');
      this.volume.disabled=this.source==='none';
    }
  }
  class CompareViewer {
    constructor() {
      this.stage = document.getElementById('stage');
      this.top = document.getElementById('top');
      this.bar = document.getElementById('bar');
      this.mode = 'wipe'; this.split = .5; this.swapped = false;
      this.zoom = 1; this.panX = this.panY = 0; this.fitted = 1;
      this.actualSize = false;
      this.drag = this.pan = null; this.flipTimer = null; this.showB = true;
      this.fps = +this.stage.dataset.fps || 24;
      this.region = new CompareRegionSelector(this);
      this.audio = new CompareAudio(this);
      this.bind(); this.setMode('__START__'); this.fit(); this.tape();
    }
    get media() { return [...this.stage.querySelectorAll('video')]; }
    get leader() { return this.media[0]; }
    node(id) { return document.getElementById(id); }
    clamp(value) { return Math.max(0, Math.min(1, value)); }
    frameAt(time) { return Math.floor(time*this.fps+1e-3); }
    size() {
      const one=this.stage.querySelector('video,img');
      return [+this.stage.dataset.w||one.videoWidth||one.naturalWidth||640,+this.stage.dataset.h||one.videoHeight||one.naturalHeight||360];
    }
    snapshot() { return {framing:'mode',mode:this.mode,split:this.split,mix:+this.node('mix').value/100,
      swapped:this.swapped,time:this.leader?.currentTime||0,showB:this.showB,region:this.region.value,regionOnly:this.region.only,
      audioSource:this.audio.source,audioVolume:this.audio.level}; }
    publish() { parent.postMessage(this.snapshot(), '*'); }
    displayArea() {
      const [width,height]=this.size();
      return this.region.only&&this.region.value?this.region.value:{x:0,y:0,width,height};
    }
    fit() {
      const [w,h]=this.size(),r=this.displayArea(),wide=this.mode==='side'?2:1;
      const available=Math.max(100,innerHeight-this.node('pan').offsetTop-130);
      const scale=Math.min((innerWidth-36)/(r.width*wide),available/r.height);
      this.stage.style.width=(r.width*wide*scale)+'px';
      this.stage.style.height=(r.height*scale)+'px';
      this.stage.querySelectorAll('video,img').forEach(media=>Object.assign(media.style,{
        width:(w*scale)+'px',height:(h*scale)+'px',left:(-r.x*scale)+'px',top:(-r.y*scale)+'px'}));
      this.fitted=scale;if(this.actualSize)this.zoom=1/scale;this.applyZoom();this.paint();
    }
    applyZoom() {
      this.stage.style.transform=`translate(${this.panX}px,${this.panY}px) scale(${this.zoom})`;
      this.node('zoomNum').textContent=Math.round(this.zoom*this.fitted*100)+'%';
    }
    paint() {
      let clip=0;
      const area=this.displayArea(),boundary=(this.split*this.size()[0]-area.x)/area.width;
      if(this.mode==='wipe')clip=this.clamp(boundary)*100;
      if(this.mode==='a')clip=this.swapped?0:100;
      if(this.mode==='b')clip=this.swapped?100:0;
      if(this.mode==='flip')clip=this.showB?0:100;
      this.top.style.clipPath=`inset(0 0 0 ${clip}%)`;
      this.top.style.opacity=this.mode==='fade'?+this.node('mix').value/100:1;
      this.bar.hidden=this.mode!=='wipe'||boundary<0||boundary>1;this.bar.style.left=this.clamp(boundary)*100+'%';
      const percent=Math.round(this.clamp(boundary)*100);
      this.node('split').value=percent;
      this.node('splitNum').textContent=percent+'%'+(this.region.only?' · X '+Math.round(this.split*this.size()[0]):'');
      this.node('mixNum').textContent=this.node('mix').value+'%';
      this.node('pos').textContent=this.mode==='wipe'?t(`왼쪽 ${this.swapped?'B':'A'} ${percent}% · 오른쪽 ${this.swapped?'A':'B'}`):this.mode==='side'?t(`왼쪽 ${this.swapped?'B':'A'} | 오른쪽 ${this.swapped?'A':'B'}`):'';
      this.node('leftLabel').textContent=this.swapped?'B':'A';
      this.node('rightLabel').textContent=this.swapped?'A':'B';
      this.node('leftLabel').hidden=this.node('rightLabel').hidden=!['wipe','side'].includes(this.mode);
      this.region.paint();
    }
    setMode(mode) {
      this.mode=['wipe','fade','flip','side','a','b'].includes(mode)?mode:'wipe';
      clearInterval(this.flipTimer); this.flipTimer=null;
      document.body.className=this.mode==='side'?'side':['a','b'].includes(this.mode)?'solo':'';
      this.node('mixWrap').classList.toggle('show',this.mode==='fade');
      this.node('splitWrap').classList.toggle('show',this.mode==='wipe');
      document.querySelectorAll('[data-mode]').forEach(b=>{const on=b.dataset.mode===this.mode;b.classList.toggle('on',on);b.setAttribute('aria-pressed',String(on));});
      if(this.mode==='flip')this.flipTimer=setInterval(()=>{this.showB=!this.showB;this.paint();this.publish();},260);
      this.fit(); this.publish();
    }
    setSplit(value) { const [w]=this.size();this.split=Math.round(this.clamp(value)*w)/w;this.paint();this.publish(); }
    moveSplit(event) {
      const box=this.stage.getBoundingClientRect();
      const area=this.displayArea();
      if(box.width)this.setSplit((area.x+this.clamp((event.clientX-box.left)/box.width)*area.width)/this.size()[0]);
    }
    swap() {
      const base=this.node('base'),low=base.firstElementChild,high=this.top.firstElementChild;
      base.append(high);this.top.append(low);
      this.swapped=!this.swapped; this.fit();this.publish();
    }
    duration() {
      const values=this.media.map(m=>m.duration).filter(v=>Number.isFinite(v)&&v>0);
      return values.length?Math.min(...values):0;
    }
    seek(at) {
      const last=Math.max(0,(Math.ceil(this.duration()*this.fps-Math.max(1e-6,this.fps*1e-6))-1)/this.fps);
      const value=Math.max(0,Math.min(at,last));
      this.media.forEach(m=>{if(m.readyState)m.currentTime=value;});this.tape();this.publish();
    }
    pause() { this.media.forEach(m=>m.pause()); }
    toggle() {
      if(!this.leader)return;
      if(!this.leader.paused)this.pause();
      else {
        if(this.leader.currentTime>=this.duration()-1/this.fps-.00001)this.seek(0);
        Promise.all(this.media.map(m=>m.play())).catch(()=>{this.pause();this.node('pos').textContent=t('재생하지 못했습니다. 파일 형식을 확인하세요.');});
      }
    }
    step(dir) { if(this.leader){this.pause();this.seek((this.frameAt(this.leader.currentTime)+dir)/this.fps);} }
    tape() {
      this.node('tape').hidden=!this.leader;
      if(!this.leader)return;
      const time=this.leader.currentTime,duration=this.duration();
      this.node('seek').value=duration?time/duration*1000:0;
      this.node('time').textContent=`${time.toFixed(2)} / ${duration.toFixed(2)}s · #${this.frameAt(time)+1}`;
      this.node('play').textContent=this.leader.paused?'▶':'❚❚';this.publish();
    }
    bind() {
      const stage=this.stage,pan=this.node('pan');
      stage.addEventListener('pointerdown',e=>{
        if(e.button!==0||this.mode!=='wipe'||this.drag!==null)return;
        e.preventDefault();stage.focus({preventScroll:true});this.drag=e.pointerId;stage.setPointerCapture(e.pointerId);this.moveSplit(e);
      });
      stage.addEventListener('pointermove',e=>{if(this.drag===e.pointerId)this.moveSplit(e);});
      const end=e=>{if(this.drag===e.pointerId){this.drag=null;if(stage.hasPointerCapture(e.pointerId))stage.releasePointerCapture(e.pointerId);}};
      ['pointerup','pointercancel','lostpointercapture'].forEach(event=>stage.addEventListener(event,end));
      pan.addEventListener('pointerdown',e=>{
        if(![1,2].includes(e.button)||this.pan)return;
        e.preventDefault();this.pan={id:e.pointerId,x:e.clientX-this.panX,y:e.clientY-this.panY};pan.setPointerCapture(e.pointerId);
      });
      pan.addEventListener('pointermove',e=>{if(this.pan?.id===e.pointerId){this.panX=e.clientX-this.pan.x;this.panY=e.clientY-this.pan.y;this.applyZoom();}});
      const endPan=e=>{if(this.pan?.id===e.pointerId){this.pan=null;if(pan.hasPointerCapture(e.pointerId))pan.releasePointerCapture(e.pointerId);}};
      ['pointerup','pointercancel','lostpointercapture'].forEach(event=>pan.addEventListener(event,endPan));
      pan.addEventListener('contextmenu',e=>e.preventDefault());
      pan.addEventListener('wheel',e=>{
        e.preventDefault();const before=this.zoom;this.actualSize=false;
        this.zoom=Math.min(16,Math.max(.2,before*(e.deltaY<0?1.15:1/1.15)));
        const box=stage.getBoundingClientRect(),ratio=this.zoom/before-1;
        this.panX-=(e.clientX-box.left-box.width/2)*ratio;this.panY-=(e.clientY-box.top-box.height/2)*ratio;this.applyZoom();
      },{passive:false});
      document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>this.setMode(b.dataset.mode));
      this.node('split').oninput=e=>{const r=this.displayArea();this.setSplit((r.x+(+e.target.value/100)*r.width)/this.size()[0]);};
      this.node('center').onclick=()=>{const r=this.displayArea();this.setSplit((r.x+r.width/2)/this.size()[0]);};
      this.node('mix').oninput=()=>{this.paint();this.publish();};
      this.node('swap').onclick=()=>this.swap();
      this.node('fit').onclick=()=>{this.actualSize=false;this.zoom=1;this.panX=this.panY=0;this.fit();};
      this.node('actual').onclick=()=>{this.actualSize=true;this.zoom=1/this.fitted;this.panX=this.panY=0;this.applyZoom();};
      this.node('play').onclick=()=>this.toggle();
      this.node('backFrame').onclick=()=>this.step(-1);this.node('nextFrame').onclick=()=>this.step(1);
      this.node('seek').oninput=e=>this.seek(this.duration()*+e.target.value/1000);
      this.media.forEach(m=>{
        m.removeAttribute('loop');
        ['loadedmetadata','loadeddata'].forEach(k=>m.addEventListener(k,()=>{this.fit();this.tape();}));
        ['timeupdate','play','pause','seeked'].forEach(k=>m.addEventListener(k,()=>this.tape()));
        m.addEventListener('timeupdate',()=>{
          if(m!==this.leader)return;
          if(m.currentTime>=this.duration()-.005&&!m.paused){this.pause();this.seek(this.duration());}
          this.media.slice(1).forEach(other=>{if(other.readyState&&!other.seeking&&Math.abs(other.currentTime-m.currentTime)>1/this.fps)other.currentTime=m.currentTime;});
        });
        m.addEventListener('ended',()=>{this.pause();this.seek(this.duration());});
      });
      stage.querySelectorAll('img').forEach(img=>img.addEventListener('load',()=>this.fit()));
      document.addEventListener('keydown',e=>{
        if(e.key==='Escape'){parent.postMessage('framing:close-viewer','*');return;}
        if(e.ctrlKey||e.metaKey||e.altKey||e.target.closest('input,select,textarea,button,[contenteditable="true"]'))return;
        const key=e.key.toLowerCase();
        if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();this.setMode('wipe');this.setSplit(this.split+(e.key==='ArrowLeft'?-1:1)*(e.shiftKey?.01:.05)*this.displayArea().width/this.size()[0]);}
        else if(key===' '){e.preventDefault();this.toggle();}
        else if(key>='1'&&key<='4')this.setMode(['wipe','fade','flip','side'][+key-1]);
        else if(key==='a'||key==='b')this.setMode(key);
        else if(key==='s')this.swap();
        else if(key===','||key==='.')this.step(key===','?-1:1);
        else if(key==='0')this.node('fit').click();else if(key==='9')this.node('actual').click();
      });
      addEventListener('message',async e=>{
        if(e.source!==parent||e.data?.framing!=='capture-state')return;
        // Whole-video exports do not depend on the browser decoder being ready.
        if(e.data.format!=='frame'){
          this.pause();parent.postMessage({...this.snapshot(),framing:'captured-state',request:e.data.request},'*');return;
        }
        this.pause();clearInterval(this.flipTimer);this.flipTimer=null;
        if(this.leader)this.seek(this.frameAt(this.leader.currentTime)/this.fps);
        const request=e.data.request;
        try {
          await Promise.all(this.media.map(m=>new Promise((resolve,reject)=>{
            if(!m.seeking&&m.readyState>=2){resolve();return;}
            const done=()=>{if(!m.seeking&&m.readyState>=2){clearTimeout(timer);m.removeEventListener('seeked',done);m.removeEventListener('loadeddata',done);resolve();}};
            const timer=setTimeout(()=>{m.removeEventListener('seeked',done);m.removeEventListener('loadeddata',done);reject(new Error(t('프레임을 아직 읽는 중입니다. 잠시 후 다시 저장하세요.')));},5000);
            m.addEventListener('seeked',done);m.addEventListener('loadeddata',done);
          })));
          parent.postMessage({...this.snapshot(),framing:'captured-state',request},'*');
        } catch(error){parent.postMessage({framing:'captured-state',request,error:error.message},'*');}
      });
      addEventListener('resize',()=>this.fit());
      addEventListener('blur',()=>{this.drag=null;this.pan=null;});
      addEventListener('pagehide',()=>{clearInterval(this.flipTimer);this.pause();});
    }
  }
  new CompareViewer();
})();
</script></body></html>"""

    def __init__(self, a_name: str, b_name: str, video: bool,
                 size: tuple[int, int] = (0, 0), start: str = "wipe",
                 fps: float = 24.0, language: str = 'kr', audio=(False, False)) -> None:
        self._a = a_name
        self._b = b_name
        self._video = video
        self._size = size
        self._start = self.MODES.get(start, "wipe")
        self._fps = fps if fps and fps > 0 else 24.0
        self._language = 'en' if language == 'en' else 'kr'
        self._audio = [bool(value) for value in audio]

    def _tag(self, name: str, controls: bool = False) -> str:
        if self._video:
            # 브라우저가 붙여 주는 재생 막대는 쓰지 않는다. 아래층에만 달면
            # 위층에 가려 오른쪽 반만 보이고, 두 영상을 같이 몰지도 못한다.
            # 대신 화면 아래에 우리 것을 하나 두고 둘을 함께 움직인다.
            return (f'<video src="{name}" loop muted playsinline '
                    f'preload="auto"></video>')
        return f'<img src="{name}" alt="">'

    def render(self, a_src: str = "", b_src: str = "") -> str:
        """
        뷰어 한 장. 파일로 저장할 때는 옆에 둔 파일 이름을, 앱 안에서 띄울 때는
        앱이 정해 준 주소를 넣는다. 같은 화면을 두 번 만들지 않으려는 것이다.
        """
        values = {'LOCALIZATION': MessageCatalog.script(), 'TITLE': html.escape(f'{self._a} ↔ {self._b}'),
                  'A': html.escape(self._a), 'B': html.escape(self._b),
                  'W': str(self._size[0] or 0), 'H': str(self._size[1] or 0),
                  'START': self._start, 'FPS': f'{self._fps:.12g}', 'AUDIO': json.dumps(self._audio),
                  'BOTTOM': self._tag(html.escape(a_src or quote(self._a), quote=True)),
                  'TOP': self._tag(html.escape(b_src or quote(self._b), quote=True))}
        template = self.HTML.replace('/* @viewer-language */ "kr"',
                                     '/* @viewer-language */ "' + self._language + '"')
        return re.sub(r'__(LOCALIZATION|TITLE|A|B|W|H|START|FPS|AUDIO|BOTTOM|TOP)__',
                      lambda match: values[match.group(1)], template)



    def write(self, folder: Path) -> Path:
        destination = folder / self.NAME
        destination.write_text(self.render(), encoding="utf-8")
        return destination


"""A durable retime package with complete frames and per-frame provenance."""
from pathlib import Path
import json
import shutil


class RetimeBundle:
    def __init__(self, folder, source):
        self.folder, self.source = Path(folder), Path(source)
        self.frames = self.folder / 'frames'
        self.interpolated = self.folder / 'interpolated'
        self.manifest = self.folder / 'retime.json'
        self.frames.mkdir(parents=True, exist_ok=True)
        self.write({'status': 'incomplete', 'source': str(self.source)})

    def write(self, data):
        self.manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def finish(self, report, frames, movie, abort):
        entries, copies = [], []
        engine = report.payload()['engine']
        for index, (path, changed) in enumerate(zip(frames, report.marks())):
            abort()
            kind = ('repeated' if engine == 'dup' else 'interpolated') if changed else 'source'
            entry = {'frame': index + 1, 'time': index / report.plan.fps,
                     'sourcePosition': report.at(index), 'kind': kind,
                     'file': path.relative_to(self.folder).as_posix()}
            if kind == 'interpolated':
                self.interpolated.mkdir(exist_ok=True)
                copied = self.interpolated / path.name
                shutil.copy2(path, copied)
                copies.append(copied)
                entry['interpolatedFile'] = copied.relative_to(self.folder).as_posix()
            entries.append(entry)
        self.write({'status': 'complete', 'source': str(self.source),
                    'movie': movie.relative_to(self.folder).as_posix(),
                    'fps': report.plan.fps, 'engine': report.payload()['engine'],
                    'sourceFrames': report.plan.source, 'outputFrames': len(frames),
                    'frameNumbering': 'Files start at 1; sourcePosition starts at 0',
                    'frames': entries})
        return copies + [self.manifest]

"""
플레이트를 지키면서 RIFE 로 사이를 채우는 왕복.

짧은 mov 플레이트를 늘려야 하는데, 늘리는 순간 플레이트가 죽는 게 문제였다.
프레임을 통째로 RIFE 에 넣으면 원본까지 8bit 로 한 번 다녀오기 때문이다.

그래서 **원본 프레임은 RIFE 를 거치지 않는다.** 사이만 채운다.

    plate/     원본 N장 · 소스 계조 그대로 (16bit)   ← 플레이트. 손대지 않는다
    for_rife/  같은 N장의 8bit 복사본                ← RIFE 에게만 주는 것
    rife/      RIFE 가 돌려준 촘촘한 격자             ← 8bit
    → 결과      원본 자리는 plate/ 에서, 새 자리만 rife/ 에서

정수배(2·4·8)로만 뽑는 이유도 같다. 배수 M 으로 뽑으면 촘촘한 격자의 M 칸마다
원본이 **정확히** 놓인다. 임의 비율로 한 번에 늘리면 원본이 격자 위에 안 떨어져서
전부 새로 그려진다 — 24→48 을 그냥 시키면 48장 중 46장이 새 그림이었다.
"""


from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional, Sequence
import hashlib
import json
import re
import shutil
import subprocess





# ══════════════════════════════════════════════════ 플레이트가 무엇인가

@dataclass(frozen=True)
class PlateSpec:
    """되돌릴 때 필요한 플레이트의 모든 것. 여기 없는 값은 지어내지 않는다."""

    source: str
    frames: int
    fps: float
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    codec: str = ""
    primaries: str = ""
    transfer: str = ""
    matrix: str = ""
    crange: str = ""

    @property
    def deep(self) -> bool:
        """8bit 4:2:0 위에 있는가 — 그렇다면 16bit PNG 로 떠야 한다."""
        fmt = self.pix_fmt.lower()
        return any(mark in fmt for mark in ("10", "12", "14", "16", "444", "422"))

    @property
    def read_pix_fmt(self) -> str:
        return "rgb48le" if self.deep else "rgb24"

    def tag_args(self) -> list[str]:
        """소스가 적어 둔 것만 도장 찍는다 — 없는 걸 지어내면 색이 밀린다."""
        pairs = (("-color_primaries", self.primaries), ("-color_trc", self.transfer),
                 ("-colorspace", self.matrix), ("-color_range", self.crange))
        return [bit for flag, value in pairs if value for bit in (flag, value)]

    @classmethod
    def read(cls, probe: str, source: Path) -> "PlateSpec":
        """ffprobe 한 줄짜리 출력에서 뽑는다."""
        got = dict(line.split("=", 1) for line in probe.strip().splitlines() if "=" in line)
        vague = {"", "unknown", "unspecified", "reserved", "N/A", "n/a"}
        clean = lambda key: "" if got.get(key, "") in vague else got.get(key, "")
        rate = got.get("r_frame_rate", "24/1")
        try:
            top, bottom = rate.split("/")
            fps = float(top) / float(bottom or 1)
        except (ValueError, ZeroDivisionError):
            fps = 24.0
        return cls(source=str(source), frames=int(got.get("nb_read_frames") or 0),
                   fps=fps or 24.0,
                   width=int(got.get("width") or 0), height=int(got.get("height") or 0),
                   pix_fmt=clean("pix_fmt"), codec=clean("codec_name"),
                   primaries=clean("color_primaries"), transfer=clean("color_transfer"),
                   matrix=clean("color_space"), crange=clean("color_range"))


# ══════════════════════════════════════════════════ 어느 칸을 고를 것인가

@dataclass(frozen=True)
class Pick:
    """결과 프레임 한 장이 어디서 오는가."""

    out_index: int                      # 결과 몇 번째 (0부터)
    dense_index: int                    # 촘촘한 격자의 몇 번째
    source_index: Optional[int]         # 원본 그 자체면 원본 번호, 아니면 None
    snapped: bool = False               # 원본을 잡으려고 살짝 당겼는가

    @property
    def from_plate(self) -> bool:
        return self.source_index is not None


class DenseGrid:
    """
    RIFE 를 몇 배로 돌리고, 그 격자에서 어느 칸을 고를지.

    배수 M 으로 뽑으면 격자 칸 d 는 원본 위치 d/M 이다. 그래서 d 가 M 의 배수면
    그 칸은 **원본 그 자체**다 — RIFE 결과가 아니라 plate/ 에서 꺼내 쓰면 된다.

    목표 장수가 격자에 딱 안 떨어질 때는 가장 가까운 칸을 고른다. 격자가 촘촘해서
    (M=4 면 원본 한 장을 넷으로 나눈 것) 밀리는 양이 0.25프레임을 넘지 않는다.
    임의 비율로 한 번에 늘릴 때 생기던 눈금 어긋남이 여기서 사라진다.
    """

    CHOICES = (2, 4, 8)                 # RIFE 가 흔히 지원하는 배수
    SNAP_WITHIN = 0.25                  # 이만큼(원본 프레임 단위) 안이면 원본으로 당긴다

    def __init__(self, source_frames: int, target_frames: int,
                 multiplier: Optional[int] = None, snap: bool = True, preserve: bool = True) -> None:
        if source_frames < 2:
            raise FramingError("플레이트가 두 장은 되어야 사이를 채울 수 있습니다.")
        if target_frames < 2:
            raise FramingError("목표 장수가 너무 적습니다.")
        self.source_frames = source_frames
        self.target_frames = target_frames
        self.snap = snap
        self.preserve = preserve
        self.multiplier = multiplier or self._decide(source_frames, target_frames)

    @classmethod
    def _decide(cls, source_frames: int, target_frames: int) -> int:
        """목표를 담을 수 있는 가장 작은 정수배. 크게 뽑을수록 느리고 커진다."""
        if target_frames <= source_frames:
            return 1                     # 줄이거나 같으면 RIFE 가 필요 없다
        for mult in cls.CHOICES:
            if mult * (source_frames - 1) + 1 >= target_frames:
                return mult
        return cls.CHOICES[-1]

    @property
    def needs_rife(self) -> bool:
        return self.multiplier > 1

    @property
    def dense_count(self) -> int:
        return self.multiplier * (self.source_frames - 1) + 1

    def picks(self) -> list[Pick]:
        """
        결과 프레임마다 격자 어느 칸에서 가져올지.

        원본을 잡으려고 당길(snap) 때 **옆 칸과 같아지면 안 된다.** 처음 짠 것이
        그 함정에 빠졌다 — 24→90 에서 당긴 자리가 다음 칸과 겹쳐 더블이 22장
        생겼고, "원본 그대로 46장" 이라고 말하는데 원본은 24장뿐이었다.
        늘리기가 더블을 만드는 바로 그 병이다. 그래서 칸 번호가 반드시
        **앞보다 커지게** 두고, 못 지키면 당기지 않는다.
        """
        mult, last = self.multiplier, self.dense_count - 1
        if self.preserve and self.target_frames >= self.source_frames:
            timeline = RetimeTimeline(self.source_frames, self.target_frames)
            out = []
            for slot in range(self.target_frames):
                source = timeline.origins.get(slot)
                if source is not None:
                    dense = source * mult
                else:
                    exact = timeline.at(slot)
                    interval = int(exact)
                    dense = min((interval + 1) * mult - 1,
                                max(interval * mult + 1, round(exact * mult)))
                out.append(Pick(slot, dense, source, False))
            return out
        span = self.target_frames - 1
        exacts = [t * last / span if span else 0.0 for t in range(self.target_frames)]
        natural = [min(last, max(0, int(round(e)))) for e in exacts]

        chosen = list(natural)
        if self.snap and mult > 1:
            for t, exact in enumerate(exacts):
                if chosen[t] % mult == 0:
                    continue                        # 이미 원본 위에 있다
                near = int(round(exact / mult)) * mult
                if not 0 <= near <= last:
                    continue
                if abs(near - exact) / mult > self.SNAP_WITHIN:
                    continue                        # 너무 많이 당겨야 한다
                behind = chosen[t - 1] if t else -1
                ahead = natural[t + 1] if t + 1 < len(natural) else last + 1
                if near <= behind or near >= ahead:
                    continue                        # 당기면 옆과 같아진다 = 더블
                chosen[t] = near

        out: list[Pick] = []
        for t, dense in enumerate(chosen):
            source = dense // mult if dense % mult == 0 else None
            out.append(Pick(t, dense, source, dense != natural[t]))
        return out

    @property
    def unavoidable_doubles(self) -> int:
        """
        격자보다 많은 장수를 달라고 하면 산수로 겹칠 수밖에 없는 장수.

        늘려 놓고 다시 줄일 때 더블이 왜 생기냐는 물음이 여기서도 똑같이 나온다.
        돌리기 전에 세어서 말해 준다.
        """
        dense = [p.dense_index for p in self.picks()]
        return sum(1 for a, b in zip(dense, dense[1:]) if a == b)

    def summary(self, picks: Optional[Sequence[Pick]] = None) -> str:
        picks = picks if picks is not None else self.picks()
        kept = sum(1 for p in picks if p.from_plate)
        doubles = sum(1 for a, b in zip(picks, picks[1:])
                      if a.dense_index == b.dense_index)
        tail = f" · 겹치는 자리 {doubles}장" if doubles else ""
        if not self.needs_rife:
            return (f"{self.source_frames} → {self.target_frames}프레임 · "
                    f"RIFE 없이 원본에서 고르기만 합니다{tail}")
        return (f"{self.source_frames} → {self.target_frames}프레임 · "
                f"RIFE {self.multiplier}배({self.dense_count}장)로 뽑아 골라냅니다 · "
                f"원본 그대로 {kept}장 · 새로 채우는 자리 {len(picks) - kept}장{tail}")


# ══════════════════════════════════════════════════ 1단계 · 내보내기

class PlateExport:
    """
    플레이트를 두 벌로 떠 놓는다 — 지킬 것과, RIFE 에게 줄 것.

    plate/ 는 소스 계조 그대로(10bit 이면 16bit PNG). for_rife/ 는 8bit 복사본이다.
    RIFE 는 8bit 밖에 못 먹는데, 그걸 원본에까지 적용하면 플레이트가 죽는다.
    그래서 처음부터 갈라 둔다.
    """

    FOLDER = "_rife"
    MANIFEST = "rife.json"

    def __init__(self, source: Path, spec: PlateSpec, grid: DenseGrid,
                 ffmpeg: str = "ffmpeg", abort: Optional[Callable[[], None]] = None) -> None:
        self._source = Path(source)
        self._spec = spec
        self._grid = grid
        self._ffmpeg = ffmpeg
        self._abort = abort or (lambda: None)

    def run(self, folder: Path) -> Path:
        folder = Path(folder)
        plate, for_rife = folder / "plate", folder / "for_rife"
        for spot in (plate, for_rife):
            spot.mkdir(parents=True, exist_ok=True)
        self._abort()
        self._explode(plate, self._spec.read_pix_fmt)
        made = sorted(plate.glob("*.png"))
        if not made:
            raise FramingError("플레이트에서 프레임을 뽑지 못했습니다.")
        Console.info(f"플레이트  {len(made)}장 · {self._spec.pix_fmt or '알 수 없음'} "
                     f"→ {'16bit' if self._spec.deep else '8bit'} PNG")
        if self._grid.needs_rife:
            self._abort()
            self._explode(for_rife, "rgb24")
            Console.info(f"RIFE 에게  {len(sorted(for_rife.glob('*.png')))}장 · 8bit 복사본")
        else:
            shutil.rmtree(for_rife, ignore_errors=True)
        self._write_manifest(folder, made)
        return folder

    def _explode(self, spot: Path, pix_fmt: str) -> None:
        done = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-i", str(self._source),
             "-pix_fmt", pix_fmt, "-start_number", "0", str(spot / "%06d.png")],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0:
            raise FramingError(f"프레임을 뽑지 못했습니다: {done.stderr.strip()[:200]}")

    def _write_manifest(self, folder: Path, plate_files: Sequence[Path]) -> None:
        picks = self._grid.picks()
        data = {
            "tool": "framing-plate-rife", "version": 1,
            "status": "awaiting-rife" if self._grid.needs_rife else "ready",
            "plate": asdict(self._spec),
            "grid": {"multiplier": self._grid.multiplier,
                     "denseCount": self._grid.dense_count,
                     "targetFrames": self._grid.target_frames,
                     "snap": self._grid.snap, "preserveOriginals": self._grid.preserve},
            "frameNumbering": "모든 번호는 0부터. 파일 이름도 000000 부터.",
            # 원본이 바뀌지 않았다는 걸 나중에 증명할 수 있게 지문을 남긴다
            "plateDigest": self._digest(plate_files),
            "picks": [{"out": p.out_index, "dense": p.dense_index,
                       "source": p.source_index, "snapped": p.snapped} for p in picks],
        }
        (folder / self.MANIFEST).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _digest(files: Sequence[Path]) -> str:
        """플레이트 전체의 지문 하나. 중간에 바뀌면 가져올 때 걸린다."""
        pot = hashlib.sha256()
        for path in files:
            pot.update(path.name.encode())
            pot.update(hashlib.sha256(path.read_bytes()).digest())
        return pot.hexdigest()[:32]


# ══════════════════════════════════════════════════ 2단계 · RIFE

@dataclass(frozen=True)
class RifeCommandPlan:
    """ncnn maps output i to input i*N/output_count, so request N*multiplier."""
    source_frames: int
    multiplier: int

    def __post_init__(self):
        if self.source_frames < 2 or self.multiplier not in (2, 4, 8):
            raise FramingError('RIFE 입력은 2장 이상, 배수는 2·4·8이어야 합니다.')

    @property
    def output_count(self):
        return self.source_frames * self.multiplier

    @property
    def dense_count(self):
        return (self.source_frames - 1) * self.multiplier + 1


class RifeRunner:
    """Adapt the external ncnn CLI, validate its model and await cancellable output."""

    def __init__(self, program=None, abort=None):
        bundled = RIFE_HOME / ('rife-ncnn-vulkan' if sys.platform == 'darwin' else 'rife-ncnn-vulkan.exe')
        self._program = str(program) if program else str(bundled) if bundled.is_file() else '' 
        self._abort = abort or (lambda: None)

    @property
    def ready(self):
        return (bool(self._program) and Path(self._program).is_file()
                and (sys.platform != 'darwin' or (not self._program.lower().endswith('.exe')
                     and os.access(self._program, os.X_OK))))

    def _model(self):
        if not self.ready:
            return Path('rife-v4.6')
        root = Path(self._program).resolve().parent
        for name in ('rife-v4.6', 'rife-v4'):
            for parent in (root, root / 'models'):
                folder = parent / name
                if folder.is_dir() and any(folder.glob('*.param')) and any(folder.glob('*.bin')):
                    return folder
        raise FramingError('RIFE 실행 파일 옆에 rife-v4.6 또는 rife-v4 모델 폴더가 필요합니다. 공식 배포본의 모델을 함께 두세요.')

    def command(self, source, out, multiplier):
        plan = RifeCommandPlan(len(list(Path(source).glob('*.png'))), multiplier)
        return [str(Path(self._program).resolve()) if self.ready else 'rife-ncnn-vulkan',
                '-i', str(Path(source).resolve()), '-o', str(Path(out).resolve()),
                '-m', str(self._model()), '-n', str(plan.output_count), '-f', '%08d.png']

    def run(self, source, out, multiplier):
        plan = RifeCommandPlan(len(list(Path(source).glob('*.png'))), multiplier)
        command = self.command(source, out, multiplier)
        out.mkdir(parents=True, exist_ok=True)
        if any(out.iterdir()):
            raise FramingError('RIFE 출력 폴더가 비어 있지 않습니다. 이전 결과와 섞이지 않도록 새 폴더를 사용하세요.')
        if not self.ready:
            Console.warn('RIFE 실행 파일이 등록되지 않았습니다 — v4 모델로 아래 명령을 실행하세요')
            Console.info(subprocess.list2cmdline(command) if os.name == 'nt' else __import__('shlex').join(command))
            return False
        self._abort()
        process = subprocess.Popen(command, cwd=str(Path(self._program).resolve().parent),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', errors='replace')
        try:
            while True:
                self._abort()
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.terminate()
            try: process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
            raise
        if process.returncode:
            raise FramingError(f'RIFE 가 실패했습니다: {stderr.strip()[:300]}')
        files = sorted(out.glob('*.png'))
        if len(files) != plan.output_count:
            raise FramingError(f'RIFE 출력 수가 다릅니다: 예상 {plan.output_count}, 실제 {len(files)}')
        for frame in files:
            self._abort()
            if ImageIO.read(frame, cv2.IMREAD_UNCHANGED) is None:
                raise FramingError(f'RIFE 출력 이미지를 읽을 수 없습니다: {frame.name}')
        return True


class PlateAssemble:
    """
    고른 칸을 모아 결과를 만든다. 원본 자리는 plate/ 에서, 새 자리만 RIFE 결과에서.

    만들기 **전에** 먼저 따진다. 장수가 안 맞거나 플레이트가 바뀌었으면 아무것도
    만들지 않고 무엇이 어긋났는지 말한다 — 어긋난 채로 한 편을 구워 놓으면
    그게 맞는지 틀린지 나중에 알 방법이 없다.
    """

    def __init__(self, folder: Path, ffmpeg: str = "ffmpeg",
                 abort: Optional[Callable[[], None]] = None) -> None:
        self.folder = Path(folder)
        spot = self.folder / PlateExport.MANIFEST
        if not spot.exists():
            raise FramingError(f"{PlateExport.MANIFEST} 가 없습니다 — 먼저 내보내기를 하세요.")
        self.data = json.loads(spot.read_text(encoding="utf-8"))
        self.spec = PlateSpec(**self.data["plate"])
        self.grid = self.data["grid"]
        self.picks = [Pick(p["out"], p["dense"], p["source"], p.get("snapped", False))
                      for p in self.data["picks"]]
        self._ffmpeg = ffmpeg
        self._abort = abort or (lambda: None)

    # --- 따지기 -----------------------------------------------------------

    def complaints(self, rife_dir: Optional[Path] = None) -> list[str]:
        """만들기 전에 걸리는 것들. 빈 목록이면 진행해도 된다."""
        found: list[str] = []
        plate = sorted((self.folder / "plate").glob("*.png"))
        if len(plate) != self.spec.frames and self.spec.frames:
            found.append(f"플레이트가 {self.spec.frames}장이어야 하는데 {len(plate)}장입니다")
        if plate and PlateExport._digest(plate) != self.data.get("plateDigest"):
            found.append("플레이트 프레임이 내보낸 뒤에 바뀌었습니다 (지문 불일치)")
        if self.grid["multiplier"] > 1:
            spot = Path(rife_dir) if rife_dir else self.folder / "rife"
            if not spot.exists():
                found.append(f"RIFE 결과 폴더가 없습니다: {spot}")
            else:
                got = len(sorted(spot.glob("*.png")))
                want = self.grid["denseCount"]
                maximum = self.spec.frames * self.grid['multiplier']
                if not want <= got <= maximum:
                    found.append(f"RIFE 결과가 {want}~{maximum}장이어야 하는데 {got}장입니다 "
                                 f"— 배수({self.grid['multiplier']}x)가 맞는지 확인하세요")
        return found

    # --- 만들기 -----------------------------------------------------------

    def build(self, rife_dir: Optional[Path] = None,
              out_dir: Optional[Path] = None) -> tuple[Path, dict]:
        problems = self.complaints(rife_dir)
        if problems:
            raise FramingError("가져올 수 없습니다 — " + " / ".join(problems))
        plate = sorted((self.folder / "plate").glob("*.png"))
        dense = sorted(Path(rife_dir or self.folder / "rife").glob("*.png")) \
            if self.grid["multiplier"] > 1 else []
        spot = Path(out_dir or self.folder / "result")
        spot.mkdir(parents=True, exist_ok=True)
        for old in spot.glob("*.png"):
            old.unlink()

        kept = made = 0
        for pick in self.picks:
            self._abort()
            if pick.from_plate and pick.source_index < len(plate):
                shutil.copy2(plate[pick.source_index], spot / f"{pick.out_index:06d}.png")
                kept += 1
            else:
                if pick.dense_index >= len(dense):
                    raise FramingError(f"RIFE 결과에 {pick.dense_index}번 칸이 없습니다.")
                shutil.copy2(dense[pick.dense_index], spot / f"{pick.out_index:06d}.png")
                made += 1
        report = {"frames": len(self.picks), "fromPlate": kept, "fromRife": made,
                  "snapped": sum(1 for p in self.picks if p.snapped),
                  "multiplier": self.grid["multiplier"]}
        return spot, report

    def note(self, out_frames: int) -> "StretchNote":
        """되돌릴 때 쓸 쪽지. 출력 프레임마다 원본 몇 번인지(보간이면 -1)."""
        origins = [p.source_index if p.from_plate else -1 for p in self.picks]
        return StretchNote(self.spec, out_frames, origins)

    def mux(self, frames_dir: Path, destination: Path,
            note: Optional["StretchNote"] = None) -> Path:
        """플레이트와 같은 코덱·계조·색 표기로 되묶는다. 쪽지는 파일 안에 심는다."""
        frames = sorted(Path(frames_dir).glob("*.png"))
        done = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-framerate", FrameRate.from_value(self.spec.fps).text,
             "-start_number", "0", "-i", str(Path(frames_dir) / "%06d.png"),
             *encoder_for(self.spec), *self.spec.tag_args(),
             *(note.embed_args() if note else []), "-an", str(destination)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0 or not Path(destination).exists():
            raise FramingError(f"되묶기에 실패했습니다: {done.stderr.strip()[:200]}")
        if note:
            note.write(Path(destination))
        return Path(destination)


def encoder_for(spec: PlateSpec) -> list[str]:
    """소스 계조를 안 깎는 코덱. 10bit 이면 ProRes, 아니면 h264."""
    if spec.deep:
        return ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0",
                "-pix_fmt", "yuv422p10le"]
    return ["-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p"]


# ══════════════════════════════════════════════════ 되돌리기

class StretchNote:
    """
    늘린 영상을 다시 원래 길이로 돌리기 위한 쪽지.

    영상 옆에 파일로 두고, **영상 안에도 심는다.** 쪽지 파일은 옮기다 잃어버리고
    영상만 남는 일이 실제로 흔하다. 둘 중 하나만 있어도 되돌아가게 한다.

    담는 건 하나다 — 출력 프레임마다 그게 원본 몇 번이었는지. 보간이면 -1.
    되돌리기는 그래서 계산이 아니라 **고르기**다. -1 인 자리를 빼면 끝이다.
    """

    SUFFIX = ".늘림메모.json"
    TAG = "FRAMING_STRETCH="
    OLD_TAGS = ("SHOTKIT_STRETCH=",)          # SHOTKIT 시절 영상도 되돌릴 수 있게
    VERSION = 1

    def __init__(self, spec: PlateSpec, out_frames: int,
                 origins: Sequence[int],
                 added: Optional[Sequence[int]] = None) -> None:
        self.spec = spec
        self.out_frames = out_frames
        self.origins = list(origins)
        # 늘리면서 **생긴** 자리. 보간이면 origins 가 -1 이라 저절로 갈리지만,
        # 홀드는 원본을 한 번 더 쓴 것이라 origins 만으로는 못 가른다.
        # (끝 프레임을 22번 더 쓴 자리도 origins 로는 '원본 23번' 이다.)
        self._added = (list(added) if added is not None
                       else [i for i, origin in enumerate(self.origins) if origin < 0])

    # --- 무엇을 되돌릴 수 있나 ---------------------------------------------

    @property
    def kept(self) -> list[int]:
        """출력에서 남길 자리 — 늘리면서 생긴 자리가 아닌 칸들."""
        drop = set(self._added)
        return [i for i in range(len(self.origins)) if i not in drop]

    @property
    def added(self) -> int:
        return len(self._added)

    @property
    def restores_to(self) -> int:
        return len(self.kept)

    @property
    def restoration_problem(self) -> str:
        # Explicit drop lists use source.frames == out_frames and do not claim
        # that an earlier source timeline can be reconstructed.
        if self.spec.frames != self.out_frames:
            retained = [self.origins[i] for i in self.kept]
            if retained != list(range(self.spec.frames)):
                return (f'원본 {self.spec.frames}장의 순서가 모두 남아 있지 않습니다 '
                        f'(남길 자리 {len(retained)}장). 원본 길이 복원에는 원본 영상이 필요합니다.')
        return ''

    def summary(self) -> str:
        if self.restoration_problem:
            return f'{self.spec.frames} → {self.out_frames}프레임 · 원본 길이 복원 불가'
        return (f"{self.spec.frames} → {self.out_frames}프레임 · "
                f"늘린 자리 {self.added}장 · 되돌리면 {self.restores_to}장")

    # --- 적고 읽기 ---------------------------------------------------------

    def as_dict(self) -> dict:
        return {"tool": "framing-stretch", "version": self.VERSION,
                "source": asdict(self.spec), "outFrames": self.out_frames,
                "addedFrames": self.added, "restoresTo": self.restores_to,
                "canRestoreSource": not bool(self.restoration_problem),
                "restorationProblem": self.restoration_problem,
                "frameNumbering": "0부터. origins[i] = 출력 i번이 원본 몇 번인가, "
                                  "-1이면 보간. addedAt = 늘리면서 생긴 자리",
                "origins": self.origins, "addedAt": self._added}

    @classmethod
    def from_dict(cls, data: dict) -> "StretchNote":
        if data.get("version") != cls.VERSION:
            raise FramingError(f"모르는 메모 형식입니다 (version={data.get('version')}).")
        spec = PlateSpec(**data["source"])
        return cls(spec, int(data["outFrames"]), data["origins"], data.get("addedAt"))

    def beside(self, movie: Path) -> Path:
        return Path(movie).with_suffix(Path(movie).suffix + self.SUFFIX)

    def write(self, movie: Path) -> Path:
        spot = self.beside(movie)
        spot.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return spot

    def embed_args(self) -> list[str]:
        """영상 안에 심을 ffmpeg 인자. 쪽지를 잃어버려도 여기서 나온다."""
        packed = json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))
        return ["-metadata", f"comment={self.TAG}{packed}"]

    # --- 찾아오기 ---------------------------------------------------------

    @classmethod
    def find(cls, movie: Path, explicit: Optional[Path] = None,
             ffprobe: str = "ffprobe") -> "StretchNote":
        """쪽지 → 영상 속 → 없으면 왜 없는지 말한다. 순서가 곧 믿는 순서다."""
        movie = Path(movie)
        if explicit:
            return cls.from_dict(json.loads(Path(explicit).read_text(encoding="utf-8")))
        sidecar = movie.with_suffix(movie.suffix + cls.SUFFIX)
        if sidecar.exists():
            return cls.from_dict(json.loads(sidecar.read_text(encoding="utf-8")))
        packed = cls._embedded(movie, ffprobe)
        if packed:
            return cls.from_dict(json.loads(packed))
        raise FramingError(
            f"늘림 메모를 찾지 못했습니다. {sidecar.name} 를 영상 옆에 두거나 "
            "--note 로 경로를 주세요.")

    @staticmethod
    def _embedded(movie: Path, ffprobe: str) -> str:
        said = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format_tags=comment",
             "-of", "default=nw=1:nk=1", str(movie)],
            capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
        for tag in (StretchNote.TAG, *StretchNote.OLD_TAGS):
            if said.startswith(tag):
                return said[len(tag):]
        return ""


class RetimeNoteText:
    """
    리타임 메모장(`_리타임메모.txt`)에서 새로 생긴 자리를 다시 읽는다.

    되돌리기의 정상 입력은 JSON 쪽지다. 그런데 사람이 손에 들고 있는 건 보통
    이 메모장이다 — 결과 옆에 떨어지고, 열어 보면 번호가 적혀 있다. 그걸 창에
    끌어다 놓으면 되게 한다.

    읽는 규칙은 하나다. **숫자와 쉼표만 있는 줄이 프레임 번호다.** 다른 줄에도
    숫자는 있지만(`길이 246 → 265프레임`) 거기엔 글자가 섞여 있다. 그래서 말이
    한국어든 영어든 같은 규칙으로 읽힌다.
    """

    SUFFIXES = (".txt",)
    ONLY_NUMBERS = re.compile(r"^[\s\d,]+$")

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @classmethod
    def handles(cls, path: Path) -> bool:
        return Path(path).suffix.lower() in cls.SUFFIXES

    def frames(self) -> list[int]:
        """적혀 있는 번호 — 사람이 세는 1부터, 정렬해서 중복 없이."""
        try:
            said = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FramingError(f"메모장을 읽지 못했습니다: {exc}")
        picked: set[int] = set()
        for line in said.splitlines():
            if line.strip() and self.ONLY_NUMBERS.match(line):
                picked.update(int(n) for n in re.findall(r"\d+", line))
        if not picked:
            raise FramingError(
                f"{self._path.name} 에서 프레임 번호를 찾지 못했습니다 — "
                "리타임 메모장이 맞는지 확인해 주세요.")
        return sorted(picked)

    def note(self, spec: PlateSpec, total: int) -> StretchNote:
        """쪽지 모양으로 옮긴다 — 되돌리는 쪽은 출처를 따지지 않는다."""
        spots = self.frames()
        if spots[-1] > total:
            raise FramingError(
                f"메모는 {spots[-1]}번까지 적혀 있는데 넣은 영상은 {total}프레임입니다 — "
                "메모와 다른 영상인 것 같습니다.")
        if len(spots) >= total:
            raise FramingError("전부 빼면 남는 프레임이 없습니다.")
        text = self._path.read_text(encoding='utf-8')
        header = re.search(r'(?:FRAMING|SHOTKIT)_TIMELINE source=(\d+) output=(\d+) restorable=(\d+)', text)
        legacy = re.search(r'(?:길이|Length)\s+(\d+)\s*→\s*(\d+)', text)
        if header or legacy:
            values = header or legacy
            source_count, output_count = int(values[1]), int(values[2])
            if output_count != total or total - len(spots) != source_count:
                raise FramingError('이 메모로 원본 길이를 복원할 수 없습니다. 원본 프레임이 모두 남아 있는지 확인하세요.')
        drop = {n - 1 for n in spots}
        origins = [-1 if i in drop else i for i in range(total)]
        return StretchNote(spec, total, origins, sorted(drop))


class DropSpec:
    """
    메모 없이 "어디를 뺄지" 를 직접 말하는 방법.

    늘릴 때 메모를 안 남겼거나 남에게 받은 영상이면 되돌릴 근거가 없다.
    그때 쓰는 차선책이다 — 메모가 있으면 그쪽이 언제나 옳다. 여기서 고른
    자리는 사람이 말한 것이지 늘릴 때 실제로 생긴 자리가 아니다.

      tail:20    뒤에서 20장
      head:20    앞에서 20장
      8,21,34    그 번호들 (사람이 세는 1부터)
      10-20      구간
    """

    WHERE = ("tail", "head")

    def __init__(self, said: str) -> None:
        self._said = (said or "").strip()

    def __bool__(self) -> bool:
        return bool(self._said)

    def targets(self, total: int) -> list[int]:
        """뺄 자리를 0부터 센 번호로 돌려준다."""
        said = self._said
        if not said:
            raise FramingError("뺄 자리를 말해 주세요 — tail:20 · head:20 · 8,21,34")
        for where in self.WHERE:
            if said.lower().startswith(where + ":"):
                return self._edge(where, said.split(":", 1)[1], total)
        return self._listed(said, total)

    @staticmethod
    def _edge(where: str, count: str, total: int) -> list[int]:
        try:
            many = int(count.strip())
        except ValueError:
            raise FramingError(f"{where}: 뒤에는 장수를 적어 주세요 (예: {where}:20)")
        if many <= 0 or many >= total:
            raise FramingError(f"{many}장은 뺄 수 없습니다 — 영상이 {total}프레임입니다")
        return list(range(total - many, total)) if where == "tail" else list(range(many))

    @staticmethod
    def _listed(said: str, total: int) -> list[int]:
        picked: set[int] = set()
        for piece in said.replace(" ", "").split(","):
            if not piece:
                continue
            first, _, last = piece.partition("-")
            try:
                low = int(first)
                high = int(last) if last else low
            except ValueError:
                raise FramingError(f"프레임 번호로 읽을 수 없습니다: {piece}")
            if low < 1 or high > total or low > high:
                raise FramingError(f"{piece} 는 1~{total} 범위를 벗어납니다")
            picked.update(range(low - 1, high))       # 사람은 1부터, 안에서는 0부터
        if not picked:
            raise FramingError("뺄 자리가 하나도 없습니다")
        if len(picked) >= total:
            raise FramingError("전부 빼면 남는 프레임이 없습니다")
        return sorted(picked)

    def note(self, spec: PlateSpec, total: int) -> StretchNote:
        """메모 모양으로 옮긴다 — 되돌리는 쪽은 출처를 따지지 않는다."""
        drop = set(self.targets(total))
        origins = [-1 if i in drop else i for i in range(total)]
        return StretchNote(spec, total, origins, sorted(drop))


class HoldPlan:
    """
    보간 없이 **끝 프레임을 붙잡아** 길이를 늘린다.

    새로 그리는 픽셀이 하나도 없다. 모든 프레임이 원본이라 플레이트가 100% 산다 —
    RIFE 를 거치는 자리조차 없고, 찢어지거나 녹을 걱정도 없다.
    끝이 거의 멈춰 있는 컷이나, 타임라인 길이만 맞추면 되는 자리에서는 이게 제일 깨끗하다.

    대신 붙잡은 자리는 그냥 멈춰 있다. 움직임이 살아 있는 꼬리에 쓰면 툭 서 버린다.
    """

    WHERE = ("tail", "head", "both")
    LABEL = {"tail": "뒤", "head": "앞", "both": "앞뒤"}

    def __init__(self, source_frames: int, target_frames: int,
                 where: str = "tail") -> None:
        if source_frames < 1:
            raise FramingError("붙잡을 프레임이 없습니다.")
        where = (where or "tail").lower()
        if where not in self.WHERE:
            raise FramingError(f"붙잡을 자리를 확인하세요 ({' · '.join(self.WHERE)}).")
        if target_frames < source_frames:
            raise FramingError(
                f"홀드는 늘리기만 합니다 — {source_frames}장을 {target_frames}장으로 "
                "줄이려면 리타임을 쓰세요.")
        self.source_frames = source_frames
        self.target_frames = target_frames
        self.where = where

    @property
    def extra(self) -> int:
        return self.target_frames - self.source_frames

    @property
    def shares(self) -> tuple[int, int]:
        """(앞에 붙일 장수, 뒤에 붙일 장수)."""
        if self.where == "head":
            return self.extra, 0
        if self.where == "tail":
            return 0, self.extra
        front = self.extra // 2
        return front, self.extra - front

    def origins(self) -> list[int]:
        """출력 프레임마다 원본 몇 번을 쓸 것인가."""
        front, back = self.shares
        last = self.source_frames - 1
        return ([0] * front + list(range(self.source_frames)) + [last] * back)

    def added(self) -> list[int]:
        """늘리면서 생긴 자리 — 되돌릴 때 여기만 빠진다."""
        front, back = self.shares
        tail_from = front + self.source_frames
        return list(range(front)) + list(range(tail_from, tail_from + back))

    def summary(self) -> str:
        front, back = self.shares
        spots = " · ".join(
            f"{name} {count}장" for name, count in
            (("앞", front), ("뒤", back)) if count)
        return (f"{self.source_frames} → {self.target_frames}프레임 · "
                f"{self.LABEL[self.where]} 홀드 ({spots or '붙일 것 없음'}) · "
                "새로 그리는 픽셀 없음")


class HoldBuilder:
    """홀드로 늘린 결과를 만든다. 프레임을 고르는 일뿐이라 섞이는 자리가 없다."""

    def __init__(self, source: Path, spec: PlateSpec, plan: HoldPlan,
                 ffmpeg: str = "ffmpeg",
                 abort: Optional[Callable[[], None]] = None) -> None:
        self._source = Path(source)
        self._spec = spec
        self._plan = plan
        self._ffmpeg = ffmpeg
        self._abort = abort or (lambda: None)

    def run(self, destination: Path, workspace: Path) -> tuple[Path, StretchNote]:
        workspace = Path(workspace)
        spread, picked = workspace / "plate", workspace / "out"
        for spot in (spread, picked):
            spot.mkdir(parents=True, exist_ok=True)
        self._abort()
        done = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-i", str(self._source),
             "-pix_fmt", self._spec.read_pix_fmt, "-start_number", "0",
             str(spread / "%06d.png")], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0:
            raise FramingError(f"프레임을 뽑지 못했습니다: {done.stderr.strip()[:200]}")
        plate = sorted(spread.glob("*.png"))
        if len(plate) < self._plan.source_frames:
            raise FramingError(f"프레임이 모자랍니다 — {self._plan.source_frames}장이어야 "
                               f"하는데 {len(plate)}장을 읽었습니다.")
        origins = self._plan.origins()
        for order, source_index in enumerate(origins):
            self._abort()
            shutil.copy2(plate[source_index], picked / f"{order:06d}.png")
        note = StretchNote(self._spec, len(origins), origins, self._plan.added())
        made = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-framerate", FrameRate.from_value(self._spec.fps).text,
             "-start_number", "0", "-i", str(picked / "%06d.png"),
             *encoder_for(self._spec), *self._spec.tag_args(),
             *note.embed_args(), "-an", str(destination)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if made.returncode != 0 or not Path(destination).exists():
            raise FramingError(f"되묶기에 실패했습니다: {made.stderr.strip()[:200]}")
        note.write(Path(destination))
        return Path(destination), note


class PlateRevert:
    """
    늘린 영상에서 **보간으로 넣은 자리만** 빼서 원래 길이로 되돌린다.

    계산이 아니라 고르기다 — 쪽지가 "출력 3번은 원본 1번" 이라고 적어 뒀으니,
    원본이었던 칸만 순서대로 모으면 된다. 어느 프레임을 버릴지 다시 추정하지
    않는다. 추정하면 늘릴 때와 뺄 때가 어긋나고, 그건 아무도 못 알아챈다.
    """

    def __init__(self, movie: Path, note: StretchNote, ffmpeg: str = "ffmpeg",
                 abort: Optional[Callable[[], None]] = None) -> None:
        self.movie = Path(movie)
        self.note = note
        self._ffmpeg = ffmpeg
        self._abort = abort or (lambda: None)

    def complaints(self, actual_frames: int) -> list[str]:
        found: list[str] = []
        if actual_frames != self.note.out_frames:
            found.append(f"메모는 {self.note.out_frames}프레임짜리를 기다리는데 "
                         f"넣은 영상은 {actual_frames}프레임입니다 "
                         "— 다른 영상이거나 잘린 것 같습니다")
        if not self.note.kept:
            found.append("메모에 원본 자리가 하나도 없습니다")
        if self.note.restoration_problem:
            found.append(self.note.restoration_problem)
        return found

    def run(self, destination: Path, workspace: Path) -> dict:
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        spread = workspace / "all"
        spread.mkdir(exist_ok=True)
        self._abort()
        done = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-i", str(self.movie),
             "-pix_fmt", self.note.spec.read_pix_fmt, "-start_number", "0",
             str(spread / "%06d.png")], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0:
            raise FramingError(f"프레임을 뽑지 못했습니다: {done.stderr.strip()[:200]}")
        frames = sorted(spread.glob("*.png"))
        problems = self.complaints(len(frames))
        if problems:
            raise FramingError("되돌릴 수 없습니다 — " + " / ".join(problems))

        picked = workspace / "picked"
        picked.mkdir(exist_ok=True)
        for order, out_index in enumerate(self.note.kept):
            self._abort()
            shutil.copy2(frames[out_index], picked / f"{order:06d}.png")
        spec = self.note.spec
        made = subprocess.run(
            [self._ffmpeg, "-v", "error", "-y", "-framerate", FrameRate.from_value(spec.fps).text,
             "-start_number", "0", "-i", str(picked / "%06d.png"),
             *encoder_for(spec), *spec.tag_args(), "-an", str(destination)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if made.returncode != 0 or not Path(destination).exists():
            raise FramingError(f"되묶기에 실패했습니다: {made.stderr.strip()[:200]}")
        return {"from": self.note.out_frames, "to": len(self.note.kept),
                "removed": self.note.added}

import subprocess
"""Reusable sequence input and file delivery for media conversion."""
from pathlib import Path
from contextlib import AbstractContextManager
import json
import math
import os
import tempfile








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


@dataclass(frozen=True)
class FrameRate:
    """Bound a numeric FPS to a rational time base accepted by media containers."""
    numerator: int
    denominator: int

    @classmethod
    def from_value(cls, value):
        from fractions import Fraction
        try:
            number = float(value)
        except (ValueError, TypeError, OverflowError):
            raise FramingError('프레임레이트는 0보다 큰 유한한 숫자여야 합니다.')
        if not math.isfinite(number) or number <= 0:
            raise FramingError('프레임레이트는 0보다 큰 유한한 숫자여야 합니다.')
        fraction = Fraction(str(number)).limit_denominator(1000000)
        if not 0 < fraction.numerator <= 2147483647 or fraction.denominator > 2147483647:
            raise FramingError('미디어에 저장할 수 없는 프레임레이트입니다.')
        return cls(fraction.numerator, fraction.denominator)

    @staticmethod
    def frame_count(seconds, fps):
        # ffprobe stream durations are rounded to microseconds. Do not invent
        # a final frame because 176 is reported as 176.000008 at 24000/1001 fps.
        if not math.isfinite(seconds) or not math.isfinite(fps) or seconds < 0 or fps <= 0:
            raise FramingError('프레임 수를 계산할 시간 정보가 올바르지 않습니다.')
        return max(1, math.ceil(seconds * fps - max(1e-6, fps * 1e-6)))

    @property
    def text(self):
        return f'{self.numerator}/{self.denominator}'

    @property
    def value(self):
        return self.numerator / self.denominator


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

"""Scene analysis, frame boundary planning, and one-pass clip export."""
from pathlib import Path
from bisect import bisect_left
import csv
import math
import re
import tempfile







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



import json
import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Sequence





class ExtractJob(Job):
    """영상에서 그림을 꺼낸다 — 몇 장이든, 전부든.

    예전에는 '몇 장'(Key Frames)과 '전부'(Sequence)가 딴 작업이었다.
    쓰는 사람 눈에는 같은 일의 양 끝일 뿐이라 하나로 합쳤다. 어디를 뽑을지만 다르다.
    """

    KEY = "extract"
    ALIASES = ("keyframes", "frames", "key", "sequence", "seq")
    TITLE = "뽑기"
    HINT = "원하는 지점 몇 장, 또는 전부를 시퀀스로"
    NOTE = """
    어디를 뽑을지만 정하면 됩니다.
      첫 · 중간 · 마지막 (기본) / 균등하게 N장 / N프레임마다 / 직접 찍기 / 전부

    직접 찍기 (--at, 쉼표로 여러 개)
      first middle last · 프레임 번호(1부터) · -1(끝에서부터) · 25% · 1:23 · 3s
      all 이라고 쓰면 전부 — 시퀀스로 풉니다

    몇 장만 뽑을 때는 PNG · JPG(4:4:4) 로, 전부 풀 때는 PNG · JPG · EXR · DPX 로.
    무손실 시퀀스는 원본 mp4 의 수십~수백 배가 됩니다. 1분 1080p ≈ 5~15GB.

    색이 달라 보이지 않게 하는 일 세 가지는 몇 장이든 전부든 똑같이 합니다.
      · JPG 는 색을 줄이지 않는 4:4:4 로
      · 10bit · 알파 소스는 원본 깊이 그대로 (16bit PNG, 알파 유지)
      · 밝기 범위 표기가 틀린 영상은 읽는 단계에서 바로잡음 (--levels auto|full|keep)

    화면과 여전히 다르면 보는 프로그램의 감마 차이입니다 (영상 2.4 vs sRGB 2.2).
    그건 파일을 고쳐서 맞추면 안 됩니다 — 다른 모든 도구와 어긋나게 됩니다."""

    SEQUENCE_ENCODERS = {
        "png": ["-c:v", "png"],
        "jpg": ["-c:v", "mjpeg", "-q:v", "2"],
        "exr": ["-c:v", "exr", "-compression", "zip16", "-format", "half",
                "-pix_fmt", "gbrpf32le"],
        "dpx": ["-c:v", "dpx"],
    }

    @property
    def wants_all(self) -> bool:
        return (self._opt.at or "").strip().lower() in ("all", "전부")

    def run(self, target: Path) -> JobResult:
        if self.wants_all:
            return self._extract_all(target)
        return self._extract_picks(target)

    # --- 전부 (시퀀스) ------------------------------------------------------

    def _extract_all(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        self._ff.ensure()
        fmt = self._opt.frame_format
        if fmt not in self.SEQUENCE_ENCODERS:
            fmt = "png"                     # both 는 몇 장 뽑을 때의 이야기다
        bundle = OutputFolder(target, self._opt.out_dir, '_sequence', 'sequence.json')
        folder = bundle.path
        pattern = folder / f"{target.stem}.%04d.{fmt}"
        info = self._ff.probe(target)
        profile = self._color(target, info)
        if info:
            result.message = (f"{info.width}x{info.height}, {info.fps:.3f}fps, "
                              f"{hms(info.duration)}, 약 {info.frames}프레임 → {fmt.upper()}")
            self._announce_color(profile)

        code = self._ff.run(["-y", "-i", str(target),
                             *ColorProfile.join(profile.read_filter()),
                             *self.SEQUENCE_ENCODERS[fmt], '-fps_mode', 'passthrough',
                             '-start_number', '1', "-an", str(pattern)],
                            total_seconds=info.duration if info else 0.0)
        made = sorted(p for p in folder.iterdir() if p.suffix.lower() == f".{fmt}")
        self._abort_if_asked()
        if code != 0 or not made:
            raise FramingError(f"전체 프레임 저장 미완료 (ffmpeg {code}). 폴더: {folder}")
        bundle.write(status='complete', frames=len(made), fps=info.fps if info else 0,
                     format=fmt, fileNumberBase=1, sourceFrameBase=0,
                     timing='One file per decoded frame. VFR frame intervals are not represented by sequence FPS.')
        result.outputs = made
        result.output_dir = folder
        result.next_paths = [str(folder)]
        result.ok = bool(made)
        result.message += f"  ·  {len(made)}장, {human(sum(p.stat().st_size for p in made))}"
        return result

    # --- 몇 장 --------------------------------------------------------------

    @property
    def spec(self) -> PickSpec:
        return PickSpec(self._opt.at, self._opt.every, self._opt.count)

    def _extract_picks(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        fmt = self._opt.frame_format
        if fmt not in ("png", "jpg", "both"):
            fmt = "png"                     # exr · dpx 는 전부 풀 때의 형식이다
        writers = build_writers(fmt, self._opt.jpeg_quality)
        spec = self.spec
        with VideoSource(target) as source:
            picks = spec.resolve(source.frame_count, source.fps)
            grabbed = self._grab(source, picks)
            width, height, fps, total = (*source.resolution, source.fps,
                                         source.frame_count)
        folder = self._output_dir(target, "_keyframes")

        profile = self._color(target, sample=grabbed[0][1])
        self._announce_color(profile)
        grabbed, exact = self._upgrade(target, grabbed, profile)
        fix = self._level_plan(profile, grabbed[0][1], already_fixed=exact)

        for order, (pick, frame, index) in enumerate(grabbed, 1):
            if fix:
                frame = LevelExpander.apply(frame)
            stem = folder / f"{target.stem}_{order:02d}_{pick.label}"
            result.outputs += [w.write(frame, stem) for w in writers]
        marks = " / ".join(f"{p.label}={FrameNumber.show(i)}" for p, _f, i in grabbed)
        if len(marks) > 90:
            marks = f"{len(grabbed)}지점: {grabbed[0][2]} … {grabbed[-1][2]}"
        result.message = (f"{width}x{height}, {fps:.3f}fps, "
                          f"총 {total}프레임  ·  {marks}")
        result.output_dir = folder
        result.ok = bool(result.outputs)
        return result

    def _upgrade(self, target: Path, grabbed: list, profile: ColorProfile) -> tuple[list, bool]:
        """
        소스가 8bit 4:2:0 보다 위에 있으면 ffmpeg 으로 다시 뽑는다.

        번호는 이미 세어 두었으니 그대로 쓰고, 그림만 원본 깊이로 바꿔 담는다.
        실패하면 방금 OpenCV 로 뽑은 걸 그대로 쓴다 — 못 뽑는 것보다 낫다.
        """
        if not profile.deep and not profile.retagged:
            return grabbed, False
        grabber = PreciseGrab(self._ff, profile)
        if not grabber.available():
            return grabbed, False
        deep = grabber.fetch(target, [index for _p, _f, index in grabbed])
        if not deep:
            Console.info("원본 깊이로 다시 뽑기에 실패해 8bit 로 갑니다")
            return grabbed, False
        upgraded = [(pick, deep.get(index, frame), index)
                    for pick, frame, index in grabbed]
        depth = "16bit" if upgraded[0][1].dtype == np.uint16 else "8bit"
        alpha = " · 알파 유지" if upgraded[0][1].ndim == 3 and upgraded[0][1].shape[2] == 4 else ""
        Console.info(f"원본 깊이로 뽑았습니다 ({depth} PNG{alpha})")
        return upgraded, True

    def _level_plan(self, profile: ColorProfile, sample: "np.ndarray",
                    already_fixed: bool = False) -> bool:
        """
        뽑은 그림이 16~235 로 눌려 있는지 보고, 펼지 말지 정한다.

        표기가 틀린 경우는 ColorProfile 이 이미 알고 있다. ffmpeg 으로 뽑았다면
        읽는 단계에서 바로잡혔으니 여기서 또 건드리면 두 번 펴는 셈이 된다.
        그 외에는 값을 건드리지 않는다 — 다른 도구와 결과가 같아야 하니까.
        """
        mode = (self._opt.levels or "auto").lower()
        if mode == "keep":
            return False

        reading = LevelInspector.read(sample, profile.crange)
        Console.info(f"색 범위  {reading.summary}")

        if mode == "full":
            if already_fixed:
                Console.info("읽는 단계에서 이미 폈습니다")
                return False
            if reading.looks_squeezed:
                Console.info("눌린 그림을 0~255 로 폅니다 (--levels full)")
                return True
            Console.info("이미 0~255 를 쓰고 있어 펼 것이 없습니다")
            return False

        if already_fixed:
            return False
        if profile.retagged:
            Console.info("표기가 틀린 밝기 범위를 펴서 저장합니다")
            return True
        if reading.looks_squeezed:
            Console.info("검정이 들뜨고 흰색이 탁해 보이면 --levels full 로 다시 뽑아 보세요")
        return False

    def _grab(self, source: VideoSource,
              picks: Sequence[FramePick]) -> list[tuple[FramePick, "np.ndarray", int]]:
        """지정된 지점들을 순서대로 읽는다. 앞에서 뒤로 가야 탐색이 싸다."""
        if source.frame_count <= 0:
            raise FramingError("프레임을 하나도 읽지 못했습니다.")
        cache: dict[int, "np.ndarray"] = {}
        grabbed: list[tuple[FramePick, "np.ndarray", int]] = []

        for order, pick in enumerate(picks, 1):
            self._abort_if_asked()
            Progress.tick(order, len(picks), every=1 if len(picks) < 24 else 24)
            if pick.prefer_tail:
                frame, index = self._fetch_last(source, cache)
            else:
                frame, index = self._fetch(source, pick.index, cache)
            if frame is None:
                Console.warn(f"{pick.label} ({FrameNumber.show(pick.index)}) 는 읽지 못해 건너뜁니다")
                continue
            grabbed.append((pick, frame, index))

        if not grabbed:
            raise FramingError("프레임 디코딩에 모두 실패했습니다.")
        return grabbed

    @staticmethod
    def _fetch(source: VideoSource, target: int,
               cache: dict[int, "np.ndarray"]) -> tuple[Optional["np.ndarray"], int]:
        target = max(0, target)
        if target in cache:
            return cache[target], target
        frame, index = source.read_at(target)
        if frame is not None:
            cache[index] = frame
        return frame, index

    @staticmethod
    def _fetch_last(source: VideoSource,
                    cache: dict[int, "np.ndarray"]) -> tuple[Optional["np.ndarray"], int]:
        frame, index = source.read_last()
        if frame is not None:
            cache[index] = frame
            return frame, index
        if cache:
            index = max(cache)
            return cache[index], index
        return None, -1


# ══════════════════════════════════════════════════ 결과를 어떤 모습으로

class Delivery(ABC):
    """
    리타임·역재생이 결과를 어떤 모습으로 내놓을지.

    예전에는 이걸 아무도 고를 수 없었다. 리타임은 무조건 PNG 시퀀스 폴더에
    확인용 mp4 를 얹었고, 역재생은 소스를 보고 혼자 정했다. 영상 하나만
    필요한 사람에게 144장짜리 폴더를 안기는 건 친절이 아니다.

    그래서 "어떤 모습으로 받을지" 를 여기 한 곳에 모으고, 종류마다 클래스를
    하나씩 둔다. 작업 쪽에는 if 가 없다 — 무엇으로 받을지는 이쪽이 안다.
    """

    KEY = ""
    TITLE = ""
    HINT = ""
    keeps_frames = False        # 프레임 파일을 결과로 남기는가
    makes_video = True          # 영상 파일을 만드는가

    @abstractmethod
    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        """(ffmpeg 인코더 인자, 확장자, 사람이 읽을 한 줄)"""

    @staticmethod
    def h264() -> tuple[list[str], str, str]:
        return (["-c:v", "libx264", "-crf", "12", "-preset", "slow",
                 "-pix_fmt", "yuv420p"], ".mp4", "h264 mp4")

    @staticmethod
    def prores(profile: ColorProfile) -> tuple[list[str], str, str]:
        args, why = PixelPlan.for_prores(profile)
        return args, ".mov", f"{why} mov"

    @staticmethod
    def needs_yuv(encoder: Sequence[str]) -> bool:
        """ProRes 4444 로 갈 때는 RGB→YUV 변환식을 끼우면 알파가 그 자리에서 죽는다."""
        return "yuva444p10le" not in list(encoder)

    @classmethod
    def pick(cls, key: str) -> "Delivery":
        return DELIVERY_BY_KEY.get((key or "auto").strip().lower(), AutoDelivery)()


class AutoDelivery(Delivery):
    """소스를 보고 정한다. 8bit 4:2:0 이면 mp4, 그 위면 계조를 지키려고 ProRes."""

    KEY = "auto"
    TITLE = "알아서"
    HINT = "8bit 는 mp4, 10bit·알파는 ProRes mov"

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        if profile.deep:
            args, ext, why = self.prores(profile)
            return args, ext, f"{why} — 소스 계조를 그대로 지킵니다"
        return self.h264()


class Mp4Delivery(Delivery):
    """어디서나 열리는 h264 mp4 하나. 배포·확인용."""

    KEY = "mp4"
    TITLE = "MP4"
    HINT = "어디서나 열림 · 8bit"

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        args, ext, why = self.h264()
        if profile.deep:
            why += " (10bit·알파는 여기서 8bit 로 내려앉습니다)"
        return args, ext, why


class ProResDelivery(Delivery):
    """누크·다빈치로 바로 가는 편집용 마스터."""

    KEY = "mov"
    TITLE = "ProRes MOV"
    HINT = "누크용 마스터 · 계조와 알파 유지"

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        return self.prores(profile)


class SequenceDelivery(Delivery):
    """프레임 파일만. 영상은 만들지 않는다."""

    KEY = "seq"
    TITLE = "PNG 시퀀스"
    HINT = "프레임 파일만 · 영상 없음"
    keeps_frames = True
    makes_video = False

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        return [], "", "프레임만"


class SequenceAndVideoDelivery(SequenceDelivery):
    """프레임도 남기고 확인용 영상도 만든다 — 예전 리타임이 하던 그것."""

    KEY = "both"
    TITLE = "시퀀스 + MP4"
    HINT = "프레임 파일과 확인용 mp4 둘 다"
    makes_video = True

    def encode(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        return self.h264()


class BundleDelivery(AutoDelivery):
    KEY = "bundle"
    TITLE = "영상 + 프레임 폴더"
    HINT = "영상·전체 PNG·새로 보간된 PNG·프레임 기록을 새 폴더에 저장"
    keeps_frames = True


DELIVERY_CHOICES: tuple[type[Delivery], ...] = (
    AutoDelivery, Mp4Delivery, ProResDelivery,
    SequenceDelivery, SequenceAndVideoDelivery, BundleDelivery)
DELIVERY_BY_KEY = {cls.KEY: cls for cls in DELIVERY_CHOICES}


# ══════════════════════════════════════════════════ 3. 역재생

class ReverseJob(Job):
    """영상이나 시퀀스를 거꾸로. 픽셀은 건드리지 않는다."""

    KEY = "reverse"
    ALIASES = ("rev",)
    TITLE = "Reverse"
    HINT = "영상 · 시퀀스를 거꾸로"
    NOTE = """
    무엇으로 받을지 고를 수 있습니다  (--result)
      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov
      mp4    어디서나 열리는 h264 하나
      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다
      seq    PNG 시퀀스만. 영상은 만들지 않습니다
      both   시퀀스 + 확인용 영상 둘 다

    auto 가 소스를 보고 정하는 기준
      8bit 4:2:0        h264 mp4 (소리도 같이 뒤집습니다)
      10bit · 4:2:2     ProRes 422 HQ mov — 계조를 안 깎으려고
      알파가 있으면     ProRes 4444 mov — 투명도를 지키려고
    무엇이 들어오든 h264 로 내보내면 순서 바꾸려다 원본 계조와 알파를 죽이게 됩니다.

    시퀀스 폴더를 넣고 seq · both 로 받으면 파일을 역순으로 복사만 합니다 —
    픽셀은 손대지 않습니다.

    소스의 색 규격(bt709 / bt601 · 제한범위 / 전체범위)은 결과에 그대로 찍어 둡니다.
    안 찍어 두면 다음 도구가 추측하고, 그 추측이 어긋나는 게 색이 밀리는 진짜 원인입니다.


    30초가 넘는 영상은 ffmpeg 의 reverse 필터가 전체를 램에 올려서 위험하므로,
    프레임을 경유하는 방식으로 자동 전환합니다. 이때 소리는 빠집니다."""
    ACCEPTS_FOLDER = True
    RAM_SAFE_SECONDS = 30.0     # 이보다 길면 ffmpeg reverse 필터 대신 프레임 경유

    def run(self, target: Path) -> JobResult:
        if self._opt.delivery == 'bundle':
            raise FramingError('영상 + 프레임 폴더는 리타임용입니다. 역재생은 시퀀스 + MP4를 선택하세요.')
        if target.is_dir():
            return self._reverse_sequence(target)
        return self._reverse_video(target)

    # --- 시퀀스 -----------------------------------------------------------

    def _reverse_sequence(self, target: Path) -> JobResult:
        """
        시퀀스는 파일 순서만 뒤집는다 — 픽셀은 손대지 않는다.

        받는 모습이 시퀀스가 아니면(mp4 · mov 하나만 원하면) 뒤집은 프레임을
        임시 자리에 두었다가 영상만 남긴다. 안 쓸 폴더를 원본 옆에 두지 않는다.
        """
        images = collect_images(target)
        if not images:
            raise FramingError("이미지가 없습니다.")
        how = Delivery.pick(self._opt.delivery)
        if how.keeps_frames:
            return self._reverse_into(target, images, how,
                                      self._output_dir(target, "_reversed"), keep=True)
        stage = Path(tempfile.mkdtemp(prefix="framing_revseq_"))
        try:
            return self._reverse_into(target, images, how, stage, keep=False)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _reverse_into(self, target: Path, images: Sequence[Path], how: Delivery,
                      folder: Path, keep: bool) -> JobResult:
        result = JobResult(target=target)
        ext = images[0].suffix
        total = len(images)
        for i, source in enumerate(reversed(images), 1):
            self._abort_if_asked()
            shutil.copyfile(source, folder / f"rev.{i:04d}{ext}")
            Progress.tick(i, total)
        if keep:
            result.outputs = sorted(folder.glob(f"rev.*{ext}"))
            result.output_dir = folder
            result.message = f"{total}프레임 역순 복사 (픽셀 그대로)"
        else:
            result.output_dir = target.parent
            result.message = f"{total}프레임 역순"
        made = self._mux(folder, f"rev.%04d{ext}", total, how, target)
        if made:
            result.outputs.append(made)
            result.message += f"  ·  {made.name}"
        elif not keep:
            raise FramingError("영상을 만들지 못했습니다. ffmpeg 을 확인해 주세요.")
        result.ok = True
        return result

    def _mux(self, folder: Path, pattern: str, total: int,
             how: Delivery, target: Path,
             profile: Optional[ColorProfile] = None) -> Optional[Path]:
        """
        뒤집힌 프레임들을 영상 하나로 묶는다.

        묶은 것을 어디에 둘지는 프레임을 남기느냐가 정한다. 남기면 그 폴더 안에
        확인용으로, 안 남기면 원본 옆에 결과물로.

        profile 은 **이 프레임들이 어디서 왔는지**다. 영상을 펴서 나온 프레임이면
        그 소스의 규격을 넘겨야 한다 — 안 넘기면 편 기준과 접는 기준이 달라져
        주황·하늘색이 밀린다. 진짜 그림 파일이면 되돌릴 원본이 없으니 빈 프로필.
        """
        if not how.makes_video or not self._ff.find("ffmpeg"):
            return None
        if how.keeps_frames and not self._opt.make_mp4:
            return None
        profile = profile if profile is not None else ColorProfile()
        encoder, ext, why = how.encode(profile)
        if not ext:
            return None
        Console.info(f"묶기  {why}")
        made = (folder / f"reversed_{total}f{ext}" if how.keeps_frames
                else self._output_file(target, "_reversed", ext))
        shape = ["-vf", profile.to_yuv_filter()] if Delivery.needs_yuv(encoder) else []
        code = self._ff.run(["-y", "-framerate", str(self._opt.fps),
                             "-i", str(folder / pattern), *shape, *encoder,
                             *profile.stamp_args(), "-an", str(made)], quiet=True)
        return made if code == 0 and made.exists() else None

    # --- 영상 -------------------------------------------------------------

    def _encoder_for(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        """
        (인코더 인자, 확장자, 설명)

        Reverse 는 순서만 바꾸는 작업이다. 그런데 늘 8bit 4:2:0 h264 로 뱉으면
        10bit 도 4:2:2 도 알파도 여기서 조용히 죽는다. 순서 바꾸려다 원본을 깎는 셈이다.
        무엇으로 받을지는 Delivery 가 안다 — 기본(알아서)이면 소스를 보고 정한다.
        """
        how = Delivery.pick(self._opt.delivery)
        args, ext, why = how.encode(profile)
        if ext == ".mov":
            args = list(args) + ["-an"]    # ProRes 에는 소리를 싣지 않는다
        return list(args), ext, why

    def _reverse_video(self, target: Path) -> JobResult:
        if Delivery.pick(self._opt.delivery).keeps_frames:
            return self._reverse_video_to_frames(target)
        result = JobResult(target=target, size_in=target.stat().st_size)
        self._ff.ensure()
        info = self._ff.probe(target)
        profile = self._color(target, info)
        self._announce_color(profile)
        duration = info.duration if info else 0.0
        encoder, ext, why = self._encoder_for(profile)
        Console.info(f"할 일  거꾸로 · {why}")
        destination = self._output_file(target, "_reversed", ext)
        keeps_audio = ext == ".mp4"

        if duration and duration > self.RAM_SAFE_SECONDS:
            Console.step(f"{hms(duration)} 짜리라 프레임을 경유합니다 (메모리 안전, 소리는 빠집니다)")
            self._reverse_via_frames(target, destination, info, profile, encoder)
        else:
            audio = ["-af", "areverse"] if keeps_audio else ["-an"]
            code = self._ff.run(
                ["-y", "-i", str(target), "-vf", "reverse",
                 *audio, *encoder, *profile.tag_args(),
                 *profile.range_bsf(encoder), str(destination)])
            if code != 0 or not destination.exists():
                Console.step("한 번에 뒤집기 실패 — 프레임을 경유해 다시 시도합니다")
                self._reverse_via_frames(target, destination, info, profile, encoder)

        if not destination.exists():
            raise FramingError("역재생 파일이 만들어지지 않았습니다.")
        result.outputs = [destination]
        result.output_dir = destination.parent
        result.size_out = destination.stat().st_size
        result.ok = True
        result.message = f"{hms(duration)} → {destination.name}"
        return result

    def _reverse_video_to_frames(self, target: Path) -> JobResult:
        """
        영상을 거꾸로 된 PNG 시퀀스로. 누크로 바로 들고 갈 때 쓴다.

        중간 그림은 소스 계조에 맞춰 받는다 — 10bit 을 8bit 로 거치면
        여기서 계조가 죽고 되돌릴 방법이 없다.
        """
        result = JobResult(target=target, size_in=target.stat().st_size)
        self._ff.ensure()
        info = self._ff.probe(target)
        profile = self._color(target, info)
        self._announce_color(profile)
        folder = self._output_dir(target, "_reversed")
        workspace = Path(tempfile.mkdtemp(prefix="framing_revf_"))
        try:
            code = self._ff.run(["-y", "-i", str(target),
                                 *ColorProfile.join(profile.read_filter()),
                                 "-c:v", "png", *PixelPlan.still_args(profile), "-an",
                                 str(workspace / "f.%06d.png")])
            frames = sorted(workspace.glob("f.*.png"))
            if code != 0 and not frames:
                raise FramingError("프레임 분해에 실패했습니다.")
            total = len(frames)
            for index, source in enumerate(reversed(frames), 1):
                self._abort_if_asked()
                shutil.move(str(source), str(folder / f"rev.{index:04d}.png"))
                Progress.tick(index, total)
            result.outputs = sorted(folder.glob("rev.*.png"))
            result.output_dir = folder
            result.ok = True
            result.message = f"{total}프레임 역순 시퀀스"
            made = self._mux(folder, "rev.%04d.png", total,
                             Delivery.pick(self._opt.delivery), target, profile)
            if made:
                result.outputs.append(made)
                result.message += f"  ·  {made.name}"
            return result
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _reverse_via_frames(self, target: Path, destination: Path,
                            info: Optional[ProbeInfo], profile: ColorProfile,
                            encoder: Sequence[str]) -> None:
        """
        긴 영상용. ffmpeg 의 reverse 필터는 전체를 메모리에 올려서 위험하다.

        중간 그림은 소스에 맞춰 받는다 — 10bit 소스를 8bit PNG 로 거치면
        여기서 계조가 죽고, 되돌릴 방법이 없다.
        """
        fps = (info.fps if info and info.fps else float(self._opt.fps)) or 24.0
        still = PixelPlan.still_args(profile)
        workspace = Path(tempfile.mkdtemp(prefix="framing_rev_"))
        try:
            code = self._ff.run(["-y", "-i", str(target),
                                 *ColorProfile.join(profile.read_filter()),
                                 "-c:v", "png", *still, "-an",
                                 str(workspace / "f.%06d.png")])
            frames = sorted(workspace.glob("f.*.png"))
            if code != 0 and not frames:
                raise FramingError("프레임 분해에 실패했습니다.")
            for index, source in enumerate(reversed(frames), 1):
                source.rename(workspace / f"r.{index:06d}.png")
            self._ff.run(["-y", "-framerate", FrameRate.from_value(fps).text,
                          "-i", str(workspace / "r.%06d.png"),
                          "-vf", profile.to_yuv_filter(), *encoder,
                          *profile.tag_args(), *profile.range_bsf(encoder),
                          "-an", str(destination)])
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


# ══════════════════════════════════════════════════ 5~7. 변환 · 압축

QUALITY_PRESETS = {
    "light":  {"crf264": 20, "crf265": 24, "preset": "medium",
               "audio": "192k", "note": "거의 그대로, 조금만 줄어듦"},
    "medium": {"crf264": 23, "crf265": 28, "preset": "medium",
               "audio": "160k", "note": "보통 이걸 씀"},
    "strong": {"crf264": 28, "crf265": 32, "preset": "slow",
               "audio": "128k", "note": "눈에 띄게 물러지지만 많이 줄어듦"},
}

class BoxTags:
    """
    상자에 담을 때 붙여야 하는 코덱 꼬리표.

    HEVC 를 픽셀 그대로 옮겨 담을 때가 문제다. ffmpeg 은 원본이 달고 있던
    꼬리표(hev1)를 그대로 두는데, 애플의 재생기 — 퀵타임 · 미리보기 · 사진 ·
    파인더 미리보기 — 는 hev1 을 열지 못한다. 파일을 두 번 눌러 보면
    "이 파일 형식은 열 수 없습니다" 가 뜬다. 같은 픽셀인데 꼬리표 때문이다.

    hvc1 로 바꿔 달면 열린다. 픽셀은 한 비트도 건드리지 않는다 — 상자 안쪽에
    적힌 이름표만 바꾸는 것이라, 재인코딩하지 않는다는 약속도 그대로다.
    """

    # 코덱 → 애플이 여는 꼬리표. 지금은 HEVC 하나뿐이지만 자리는 열어 둔다.
    APPLE_SAFE = {"hevc": "hvc1"}
    BOXES = (".mp4", ".mov", ".m4v")

    @classmethod
    def for_copy(cls, vcodec: str, ext: str) -> list:
        """그대로 옮겨 담을 때 덧붙일 인자. 붙일 게 없으면 빈 목록."""
        if (ext or "").lower() not in cls.BOXES:
            return []
        tag = cls.APPLE_SAFE.get((vcodec or "").lower())
        return ["-tag:v", tag] if tag else []


MP4_VIDEO_OK = ("h264", "hevc", "mpeg4", "av1")
MP4_AUDIO_OK = ("aac", "mp3", "ac3", "alac")
MOV_VIDEO_OK = ("h264", "hevc", "prores", "dnxhd", "mjpeg", "mpeg4",
                "qtrle", "png", "rawvideo")
MOV_AUDIO_OK = ("aac", "pcm_s16le", "pcm_s24le", "alac", "mp3")


class RetimeEngineFactory:
    """Choose and construct an engine; jobs only coordinate input and output."""

    def __init__(self, engines, ff):
        self._engines = {engine.KEY: engine for engine in engines}
        self._ff = ff

    def create(self, mode, plan, matte, snapper, abort, workspace, sample=None):
        chosen = (mode or 'auto').lower()
        if chosen == 'auto':
            chosen = 'dup' if matte or not plan.slowing else 'motion'
            Console.info(f'고름  {chosen} — 매트 보호와 결과 길이에 따라 선택했습니다')
        if chosen not in self._engines:
            raise FramingError(f'알 수 없는 리타임 방식입니다: {chosen}')
        engine = self._engines[chosen]
        if engine is TailHoldEngine and not plan.slowing:
            Console.warn('끝 홀드는 늘릴 때만 씁니다 — 복제로 갑니다')
            engine = DuplicateEngine
        if engine.NEEDS_FFMPEG and not self._ff.find('ffmpeg'):
            Console.warn('ffmpeg 이 없어 겹치기로 갑니다')
            engine = BlendEngine
        if engine is MotionEngine and sample is not None and not engine.supports(sample):
            Console.warn('계조와 채널을 유지하기 위해 겹치기를 사용합니다')
            engine = BlendEngine
        if engine is MotionEngine:
            return engine(plan, snapper, abort, ff=self._ff, workspace=workspace)
        return engine(plan, snapper, abort)


class RetimeJob(Job):
    """영상·시퀀스를 원하는 길이로 늘리거나 줄인다."""

    KEY = "retime"
    ALIASES = ("speed", "stretch")
    TITLE = "Retime"
    HINT = "길이 · 배속 바꾸기 (프레임 보간)"
    NOTE = """
    길이를 말하는 방법이 셋입니다. 편한 걸 쓰세요. 어느 쪽이든 같은 곳으로 갑니다.
      --frames 264     결과를 264프레임으로
      --seconds 11     결과를 11초로 (fps 를 곱해서 계산)
      --speed 0.5      절반 속도 = 두 배 길이. 2 면 두 배 빠르게

    없는 프레임을 무엇으로 채울까  (--retime-mode)
      auto     소재를 보고 알아서 고릅니다 (기본). 무엇을 골랐는지 매번 알려줍니다
      dup      가장 가까운 원본 프레임을 그대로. 픽셀 연산이 0 이라 매트가 100% 남습니다
      motion   움직임을 추정해 없던 그림을 그립니다. 제일 부드럽고, 가끔 찢어집니다
               (ffmpeg 이 없으면 앞뒤를 섞는 방식으로 알아서 물러섭니다)

    auto 가 고르는 기준
      매트 · 뎁스 패스면          → dup.    라벨색을 뭉개면 그 픽셀의 뜻이 사라집니다
      실사인데 느리게 (늘리기)     → motion. 슬로모션은 없던 프레임이 있어야 매끄럽습니다
      실사인데 빠르게 (줄이기)     → dup.    버리는 작업이라 새로 그릴 게 없습니다

    매트 패스를 알아서 알아봅니다
      매트는 몇 개의 라벨색으로만 칠해져 색상이 좁게 몰립니다 (실측 83~99%).
      실사·페인팅은 넓게 퍼집니다 (21~28%). 이 차이로 갈라냅니다.
      매트로 판별되면 쓰인 라벨색을 찾아 보여주고, 보간 후 섞인 픽셀을
      가장 가까운 라벨로 되돌립니다. 명도는 손대지 않습니다 — 그건 뎁스니까요.
      실사인데 매트로 잘못 본 것 같으면 --no-snap 으로 색 복원을 끄세요.

    무엇으로 받을지 고를 수 있습니다  (--result)
      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov — 영상 하나만 나옵니다
      mp4    어디서나 열리는 h264 하나
      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다
      seq    PNG 시퀀스만. 영상은 만들지 않습니다
      both   시퀀스 + 확인용 영상 둘 다
      bundle 영상·전체 프레임·새로 보간된 프레임·기록을 새 폴더에 저장
    새 UI 리타임 작업은 bundle이 기본입니다. CLI는 auto를 유지합니다.

    알아둘 것
      · 24fps 의 배수로 맞추면 편집에서 어긋나지 않습니다. 아니면 알려줍니다.
      · motion 은 결과를 꼭 눈으로 보세요. 빠른 움직임 · 가림 · 모션블러에 약합니다.
      · 소리는 빠집니다. 길이가 바뀌면 소리도 같이 늘려야 하는데,
        그건 리타임이 아니라 다른 작업입니다.
      · --note 를 주면 어디를 손댔는지 메모장(.txt) 한 장을 결과 옆에 놓습니다.
      · 영상 소스의 색 규격(bt709 / 제한범위 등)은 결과 mp4 에 그대로 찍어 둡니다."""
    ACCEPTS_FOLDER = True

    ENGINES = (DuplicateEngine, TailHoldEngine, BlendEngine, MotionEngine)

    def run(self, target: Path) -> JobResult:
        """
        프레임을 어디에 그릴지부터 정한다.

        예전에는 무조건 원본 옆에 `_retimed` 폴더를 만들고 PNG 를 전부 깔았다.
        영상 하나만 필요한 사람에게 144장짜리 폴더를 안기는 셈이었다.
        이제 시퀀스로 받겠다고 한 경우에만 폴더가 생기고, 아니면 임시 자리에
        그렸다가 영상만 남기고 치운다.
        """
        result = JobResult(target=target)
        how = Delivery.pick(self._opt.delivery)
        keep = how.keeps_frames
        folder = (self._retime_folder(target) if keep
                  else Path(tempfile.mkdtemp(prefix="framing_retime_out_")))
        workspace = Path(tempfile.mkdtemp(prefix="framing_retime_"))
        try:
            return self._work(target, folder, workspace, result, how)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            if not keep:
                shutil.rmtree(folder, ignore_errors=True)

    def _retime_folder(self, target: Path) -> Path:
        name = target.name if target.is_dir() else target.stem
        folder = unique_path((self._opt.out_dir or target.parent) / f'{name}_retimed')
        folder.mkdir(parents=True, exist_ok=False)
        return folder

    def _work(self, target: Path, folder: Path, workspace: Path,
              result: JobResult, how: "Delivery") -> JobResult:
        bundle = RetimeBundle(folder, target) if how.KEY == 'bundle' else None
        frame_folder = bundle.frames if bundle else folder
        frames, fps, profile = self._load(target, workspace)
        plan = RetimePlan.decide(len(frames), fps,
                                 frames=self._opt.target_frames,
                                 speed=self._opt.speed,
                                 seconds=self._opt.target_seconds)
        if plan.refusal:
            raise FramingError(plan.refusal)      # 못 할 일은 계획부터 말하지 않는다
        Console.info(f"계획  {plan.summary}")
        if plan.suspicious:
            Console.warn(plan.suspicious)
        self._warn_disk(frames[0], plan)

        snapper, matte = self._read_material(frames)
        engine = self._pick_engine(plan, matte, snapper, workspace, frames[0])
        Console.info(f"방식  {engine.TITLE} — {engine.BLURB}")
        material = SourceFrames(frames)
        self._warn_repeats(material, plan, engine)
        if plan.unchanged:
            Console.info("길이가 그대로입니다 — 프레임을 다시 번호만 매겨 내보냅니다")

        made = self._render(engine, frames, frame_folder, plan)
        if not made:
            raise FramingError("결과 프레임이 만들어지지 않았습니다.")
        result.outputs = list(made) if how.keeps_frames else []
        result.output_dir = folder if how.keeps_frames else target.parent
        result.ok = True
        result.message = (f"{plan.source} → {len(made)}프레임  ·  "
                          f"{plan.speed:.3f}배속  ·  {engine.TITLE}")

        # 어디를 건드렸는지 말해 준다. 길이만 알려 주면 문제가 생겼을 때
        # 어느 프레임을 봐야 하는지 알 수가 없다.
        report = RetimeReport(plan, engine.KEY, material.origin)
        result.retime_map = report.payload()
        labels = result.retime_map['labels']
        kept, made_label = labels['kept'], labels['made']
        Console.info(f"배치  {kept} {report.kept}장 · {made_label} {report.made}장")
        Console.info(result.retime_map['note'])
        Console.info(f"자리  {report.where()}")
        if report.strip():
            Console.info(f"      {report.strip()}   (▉ {kept} · ░ {made_label})")
        # 원본이 이미 들고 있던 정지 구간. 늘리기가 만든 게 아니라서 되돌릴 때도
        # 안 빠진다 — 말해 주지 않으면 "왜 더블이 남아 있지" 가 된다.
        doubled = report.already_double()
        if doubled:
            Console.warn(f"원본에 이미 같은 그림이 {doubled}장 붙어 있습니다 — "
                         f"늘리면서 생긴 것이 아니라 되돌려도 그대로 남습니다")

        stretch = self._stretch_note(target, plan, report, profile)
        movie = self._mux(frame_folder, plan, len(made), profile, how, target,
                          stretch.embed_args() if stretch else ())
        if movie and stretch:
            # 되돌릴 근거는 **영상 안에** 심는다. 옆에 json 을 따로 떨구면 결과
            # 폴더에 메모가 둘(.txt · .json)이 되고, 어느 게 진짜인지 알 수 없다.
            # 사람이 읽고 다시 넣을 종이는 _리타임메모.txt 하나로 족하다.
            Console.info(f"되돌릴 근거  영상 안에 심었습니다 · {stretch.summary()}")
            if stretch.restoration_problem:
                Console.warn(stretch.restoration_problem)
            else:
                Console.info(f"되돌리기  framing revert \"{movie.name}\"")
        if movie:
            result.outputs.append(movie)
            result.message += f"  ·  {movie.name}"
        elif how.makes_video and (not how.keeps_frames or how.KEY == 'bundle' or self._opt.make_mp4):
            if how.keeps_frames:
                raise FramingError(f'영상 저장에 실패했습니다. 프레임은 {folder}에 보관되어 있습니다.')
            raise FramingError("영상을 만들지 못했습니다. ffmpeg 을 확인해 주세요.")
        if bundle:
            result.outputs += bundle.finish(report, made, movie, self._abort_if_asked)
            result.next_paths = [str(movie)]
            result.retime_map.update(sourcePath=str(target), movie=str(movie),
                                     sourceVideo=target.is_file(),
                                     framesDir=str(bundle.frames),
                                     interpolatedDir=str(bundle.interpolated) if bundle.interpolated.exists() else '')
        self._leave_note(report, target, movie, result)
        return result

    def _warn_repeats(self, material: SourceFrames, plan: RetimePlan,
                      engine: RetimeEngine) -> None:
        """원본 픽셀의 완전 일치와 복제 배치 계획을 구분해 알린다."""
        if material.repeated:
            Console.info(f"원본 확인  디코딩 픽셀이 완전히 같은 인접 프레임 쌍 {material.repeated}개 "
                         "— 정지 구간의 길이는 샷 검사에서 확인하세요")
        if engine.KEY != "dup":
            return
        repeats = RetimeReport(plan, engine.KEY, material.origin).made
        if repeats:
            Console.info(f"배치 계획  {repeats}개 위치에서 앞 프레임을 재사용합니다 "
                         "— 홀드 검출 결과가 아닙니다")

    def _leave_note(self, report: "RetimeReport", target: Path,
                    movie: Optional[Path], result: JobResult) -> None:
        """
        메모장 한 장. 결과 옆에 남는 종이는 이것 하나뿐이다.

        늘렸으면 묻지 않고 남긴다 — 되돌리려면 근거가 있어야 하고, 없는 걸
        나중에 만들 수는 없다. 줄이거나 길이가 그대로면 되돌릴 것이 없으니,
        "메모 남기기" 를 켠 사람에게만 준다.
        """
        stretched = any(report.added())
        if not stretched and not self._opt.retime_note:
            return
        where = movie.parent if movie else (result.output_dir or target.parent)
        spot = RetimeNote(report, target, self._engine_title(report)).write(where, self._opt.language)
        if spot:
            result.outputs.append(spot)
            Console.info(f"메모  {spot.name}")

    def _engine_title(self, report: "RetimeReport") -> str:
        key = report.payload().get("engine", "")
        found = next((e for e in self.ENGINES if e.KEY == key), None)
        return found.TITLE if found else key

    # --- 읽기 -------------------------------------------------------------

    def _load(self, target: Path,
              workspace: Path) -> tuple[list["np.ndarray"], float, ColorProfile]:
        """
        프레임을 전부 메모리에 올린다. 리타임은 앞뒤를 같이 봐야 해서 흘려보낼 수 없다.
        대신 올리기 전에 얼마나 먹을지 재서, 위험하면 미리 말한다.
        """
        profile = ColorProfile()
        if not target.is_dir():
            profile = self._color(target)
            self._announce_color(profile)
            if profile.deep and self._ff.find("ffmpeg"):
                deep = self._load_deep(target, profile, workspace)
                if deep:
                    fps = self._source_fps(target)
                    return deep, fps, profile
        with open_feed(target, preserve=True) as feed:
            total, fps = feed.count, (feed.fps or float(self._opt.fps))
            frames = []
            for index, frame in enumerate(feed.frames(), 1):
                self._abort_if_asked()
                if index == 1:
                    self._warn_memory(frame, total)
                frames.append(frame)
                Progress.tick(index, total)
        if not frames:
            raise FramingError("프레임을 하나도 읽지 못했습니다.")
        if target.is_dir():
            sampled = ColorProfile.from_frame(frames[0])
            if sampled.bits > 8 or sampled.has_alpha:
                profile = sampled
        return frames, fps, profile

    def _source_fps(self, target: Path) -> float:
        info = self._ff.probe(target) if self._ff.find("ffprobe") else None
        return (info.fps if info and info.fps else float(self._opt.fps)) or 24.0

    def _load_deep(self, target: Path, profile: ColorProfile,
                   workspace: Path) -> list["np.ndarray"]:
        """
        10bit · 알파 소스를 원본 깊이 그대로 읽는다.

        OpenCV 로 읽으면 여기서 8bit 로 깎여서, 리타임만 하려다 계조를 잃는다.
        Key Frames 와 같은 판단을 쓴다 — 같은 영상이면 어느 작업을 거치든 같아야 한다.
        """
        stage = workspace / "_deep"
        stage.mkdir(parents=True, exist_ok=True)
        Console.step(f"원본 깊이({profile.bits}bit"
                     f"{' +알파' if profile.has_alpha else ''})로 읽는 중")
        code = self._ff.run(["-y", "-i", str(target),
                             *ColorProfile.join(profile.read_filter()),
                             "-c:v", "png", *PixelPlan.still_args(profile), "-an",
                             str(stage / "d.%06d.png")], quiet=True)
        made = sorted(stage.glob("d.*.png"))
        if code != 0 and not made:
            Console.info("원본 깊이로 읽지 못해 8bit 로 갑니다")
            return []
        frames: list["np.ndarray"] = []
        for index, file in enumerate(made, 1):
            self._abort_if_asked()
            if index == 1:
                probe = ImageIO.read(file, cv2.IMREAD_UNCHANGED)
                if probe is None:
                    return []
                self._warn_memory(probe, len(made))
                frames.append(probe)
                continue
            image = ImageIO.read(file, cv2.IMREAD_UNCHANGED)
            if image is None:
                return []
            frames.append(image)
            Progress.tick(index, len(made))
        return frames

    BULKY = 2 * 1024 ** 3       # 결과가 이보다 크면 시작 전에 말한다
    HEAVY = 3 * 1024 ** 3       # 이 이상 먹을 것 같으면 미리 말한다

    @classmethod
    def _warn_disk(cls, first: "np.ndarray", plan: RetimePlan) -> None:
        """PNG 는 대충 원본 바이트의 절반쯤으로 눌린다. 자릿수만 맞으면 충분하다."""
        rough = int(first.nbytes * 0.5) * plan.target
        if rough < cls.BULKY:
            return
        Console.warn(f"결과가 대략 {human(rough)} 나옵니다 ({plan.target}장 PNG) "
                     "— 디스크 여유를 확인하세요")

    @classmethod
    def _warn_memory(cls, first: "np.ndarray", total: int) -> None:
        """
        멈춘 것처럼 보이다가 죽는 게 제일 나쁘다. 들어가기 전에 얼마나 먹는지 말한다.
        (리타임은 앞뒤 프레임을 같이 봐야 해서 흘려보낼 수 없다.)
        """
        need = int(first.nbytes) * max(1, total)
        if need < cls.HEAVY:
            return
        Console.warn(f"프레임을 전부 올리는 데 약 {human(need)} 가 필요합니다 "
                     "— 리타임은 앞뒤를 같이 봐야 해서 어쩔 수 없습니다")
        Console.info("메모리가 모자라면 Sequence 로 나눠 뽑고 구간별로 돌리세요")

    def _read_material(self, frames: Sequence["np.ndarray"]
                       ) -> tuple[Optional[LabelSnapper], bool]:
        """매트인지 실사인지 읽고, 매트면 라벨 목록을 만든다."""
        if self._opt.no_snap:
            Console.info("소재  색 복원 끔 (--no-snap)")
            return None, False
        if frames[0].ndim == 2:
            return None, True
        step = max(1, len(frames) // 5)
        reader = MatteReader(frames[::step][:5])
        share = reader.concentration * 100
        if not reader.is_matte:
            Console.info(f"소재  실사 · 페인팅 (색상 집중도 {share:.0f}%) "
                         "— 색은 건드리지 않습니다")
            return None, False
        snapper = LabelSnapper(reader.legend())
        Console.info(f"소재  매트 · 뎁스 패스 (색상 집중도 {share:.0f}%)")
        Console.info(f"라벨  {snapper.describes}")
        return snapper, True

    # --- 고르기 -----------------------------------------------------------

    def _pick_engine(self, plan: RetimePlan, matte: bool,
                     snapper: Optional[LabelSnapper], workspace: Path, sample=None) -> RetimeEngine:
        return RetimeEngineFactory(self.ENGINES, self._ff).create(
            self._opt.retime_mode, plan, matte, snapper,
            self._abort_if_asked, workspace, sample)

    # --- 쓰기 -------------------------------------------------------------

    def _render(self, engine: RetimeEngine, frames: Sequence["np.ndarray"],
                folder: Path, plan: RetimePlan) -> list[Path]:
        made: list[Path] = []

        def write(position: int, frame: "np.ndarray") -> None:
            destination = folder / f"out.{position + 1:04d}.png"
            if not ImageIO.write(destination, frame):
                raise FramingError(f"저장 실패: {destination.name}")
            made.append(destination)
            Progress.tick(position + 1, plan.target)

        engine.render(frames, write)
        return sorted(set(made))

    def _stretch_note(self, target: Path, plan: RetimePlan,
                      report: RetimeReport, profile: ColorProfile):
        """
        늘린 자리를 되돌릴 수 있게 적어 둔다.

        여기서 적지 않으면 되돌릴 때 "어디를 뺄지" 를 다시 추정해야 하고,
        추정은 늘릴 때와 어긋난다. 줄이는 작업에는 되돌릴 것이 없다.
        """
        if not plan.slowing:
            return None
        # marks() 가 아니라 added() 다. marks() 는 "결과에서 더블로 보이는 자리"
        # 라서 원본에 원래 있던 정지 구간까지 센다. 그걸로 되돌리면 원본이 깎인다.
        added = report.added()
        engine = report.payload().get("engine", "")
        origins = [(int(round(report.at(position)))
                    if engine in ("dup", "hold") or not added[position] else -1)
                   for position in range(plan.target)]
        spec = PlateSpec(source=str(target), frames=plan.source, fps=plan.fps,
                         pix_fmt=profile.pix_fmt or "")
        return StretchNote(spec, plan.target, origins,
                           [i for i, made in enumerate(added) if made])

    def _mux(self, folder: Path, plan: RetimePlan, count: int,
             profile: ColorProfile, how: "Delivery", target: Path,
             extra: Sequence[str] = ()) -> Optional[Path]:
        """
        그려 둔 프레임을 영상 하나로 묶는다.

        프레임을 남기는 경우에만 그 폴더 안에 확인용으로 넣는다. 아니면
        원본 옆에 결과물로 — 그게 사람들이 기대하는 자리다.
        """
        if not how.makes_video or not self._ff.find("ffmpeg"):
            return None
        if how.keeps_frames and how.KEY != 'bundle' and not self._opt.make_mp4:
            return None
        encoder, ext, why = how.encode(profile)
        if not ext:
            return None
        Console.info(f"묶기  {why}")
        movie_folder = folder.parent if how.KEY == 'bundle' else folder
        made = (movie_folder / f"retimed_{count}f{ext}" if how.keeps_frames
                else self._output_file(target, "_retimed", ext))
        # 규격이 안 적힌 소스도 profile 이 스스로 안다 (read_matrix) —
        # 예전엔 여기서 빈 프로필로 갈아치웠고, 그게 색이 밀리던 자리였다.
        shape = (["-vf", profile.to_yuv_filter()]
                 if Delivery.needs_yuv(encoder) else [])
        code = self._ff.run(["-y", "-framerate", FrameRate.from_value(plan.fps).text,
                             "-i", str(folder / "out.%04d.png"), *shape,
                             *encoder, *profile.tag_args(), *extra, "-an",
                             str(made)], quiet=True)
        return made if code == 0 and made.exists() else None


class TranscodeJob(Job):
    """ffmpeg 로 컨테이너를 바꾸거나 다시 인코딩하는 작업들의 공통 뼈대."""

    NEEDS_FFMPEG = True

    @abstractmethod
    def _plan(self, target: Path, info: Optional[ProbeInfo]) -> tuple[list[str], str, str, str]:
        """(ffmpeg 인자, 확장자, 파일명 접미사, 사람이 읽을 설명)"""

    def _named(self, target: Path, suffix: str, ext: str) -> Path:
        """결과 파일 이름을 정하는 자리. 기본은 '원래이름 + 접미사'."""
        return self._output_file(target, suffix, ext)

    def run(self, target: Path) -> JobResult:
        result = JobResult(target=target, size_in=target.stat().st_size)
        info = self._ff.probe(target)
        profile = self._color(target, info)
        if info:
            Console.info(f"{human(result.size_in)}  {info.width}x{info.height}  "
                         f"{info.fps:.2f}fps  {hms(info.duration)}  "
                         f"{info.vcodec or '영상없음'} / {info.acodec or '소리없음'}")
            self._announce_color(profile)
            if info.alpha and self._drops_alpha():
                Console.warn("알파 채널이 있는데 이 형식은 못 담습니다. 투명도가 사라집니다. "
                             "투명도를 지키려면 누크용 마스터로 보내세요.")
        else:
            Console.info(f"{human(result.size_in)}   (내용을 못 읽었습니다)")

        args, ext, suffix, why = self._plan(target, info)
        args, why = self._apply_downscale(args, info, why)
        args = self._stamp(args, profile)

        # 같은 상자에 그대로 옮겨 담을 뿐이면 굳이 파일을 만들지 않는다.
        # 다만 꼬리표를 고쳐야 하는 경우는 다르다 — 그건 픽셀은 그대로여도
        # "맥에서 안 열리던 것이 열리게" 되는 일이라, 만들 값어치가 있다.
        needs_tag = BoxTags.for_copy(info.vcodec if info else "", ext)
        if (args[:2] == ["-c", "copy"] and target.suffix.lower() == ext
                and not self._opt.max_height and not profile.retagged
                and not needs_tag):
            result.message = "이미 그 형식이고 다시 인코딩할 것도 없어 건너뜁니다 (용량을 줄이려면 compress)"
            result.ok = True
            result.size_out = result.size_in
            return result

        destination = self._named(target, suffix, ext)
        Console.info(f"할 일  {why}")
        Console.info(f"출력  {destination}")

        code = self._ff.run(["-y" if self._opt.overwrite else "-n",
                             "-i", str(target), *args, str(destination)],
                            total_seconds=(info.duration if info else 0.0))
        if code != 0 or not destination.exists():
            raise FramingError(f"ffmpeg 실패 (종료코드 {code})")

        result.outputs = [destination]
        result.output_dir = destination.parent
        result.size_out = destination.stat().st_size
        result.ok = True
        if args[:2] == ["-c", "copy"]:
            result.message = f"{human(result.size_in)} → {human(result.size_out)}  (영상 데이터 그대로)"
        else:
            pct = (1 - result.size_out / result.size_in) * 100 if result.size_in else 0
            word = "작아짐" if pct > 0 else "커짐"
            result.message = (f"{human(result.size_in)} → {human(result.size_out)}  "
                              f"{abs(pct):.1f}% {word}")
            if pct < -5:
                if self._opt.convert_to == "nuke":
                    result.message += "  (ProRes 는 편집용이라 커지는 게 정상입니다)"
                elif self._opt.convert_to == "small":
                    result.message += ("  (원본이 이미 알뜰하게 인코딩돼 있습니다 "
                                       "— 세게 줄이거나 그냥 원본을 쓰세요)")
        return result

    def _drops_alpha(self) -> bool:
        """이 목적지가 투명도를 버리는가. 버릴 거면 미리 말해야 한다."""
        return (self._opt.convert_to or "mp4") != "nuke"

    @staticmethod
    def _stamp(args: list[str], profile: ColorProfile) -> list[str]:
        """
        결과물에 색 규격을 도장 찍는다.

        ffmpeg 이 알아서 옮겨줄 때도 있지만, 태그가 없거나 틀린 소스에서는 그냥 빈칸이
        된다. 빈칸이면 다음 도구가 추측하고, 추측이 어긋나는 순간 색이 밀린다.
        재포장(-c copy)일 때도 찍는다 — 픽셀은 그대로고 표기만 바로잡히는 셈이라 공짜다.
        """
        tags = profile.tag_args()
        if not tags:
            return args
        stamped = list(args) + tags
        if "-bsf:v" not in stamped:
            stamped += profile.range_bsf(args)
        return stamped

    def _apply_downscale(self, args: list[str], info: Optional[ProbeInfo],
                         why: str) -> tuple[list[str], str]:
        height = self._opt.max_height
        if not height or not info or info.height <= height:
            return args, why
        if args[:1] == ["-c"]:
            return args, why + "   (컨테이너만 바꿀 땐 해상도를 못 줄입니다)"
        return ["-vf", f"scale=-2:{height}"] + args, why + f", {height}p 로 축소"

    @staticmethod
    def _audio_args(info: Optional[ProbeInfo], preset: dict) -> list[str]:
        has_audio = bool(info and info.acodec)
        return ["-c:a", "aac", "-b:a", preset["audio"]] if has_audio else ["-an"]


class ConvertTarget(ABC):
    """
    무엇으로 바꿀 것인가. 목적지마다 규칙이 다르니 각자 자기 규칙만 안다.

    예전에는 To MP4 · To MOV · Shrink 로 창이 셋이었다. 하는 일이 거의 같은데
    창만 갈라져 있어서, 쓰는 사람이 "mov 로 갈까 압축할까"를 고르려면
    창을 옮겨 다녀야 했다. 목적지는 선택지이지 별개의 작업이 아니다.
    """

    KEY = ""
    TITLE = ""
    HINT = ""
    EXT = ".mp4"
    SUFFIX = "_conv"
    KIND = "video"              # 이 목적지가 받는 것: video · image · sequence

    @abstractmethod
    def plan(self, job: "ConvertJob", target: Path,
             info: Optional[ProbeInfo], profile: ColorProfile
             ) -> tuple[list[str], str, str, str]: ...

    @staticmethod
    def _preset(job: "ConvertJob") -> dict:
        return QUALITY_PRESETS[job._opt.level]


class Mp4Target(ConvertTarget):
    KEY = "mp4"
    TITLE = "MP4 (배포·업로드)"
    HINT = "가장 잘 열리는 형식. 대개 다시 인코딩하지 않고 상자만 갈아끼웁니다"
    EXT = ".mp4"

    def plan(self, job, target, info, profile):
        preset = self._preset(job)
        vcodec = info.vcodec if info else ""
        acodec = info.acodec if info else ""
        if (vcodec in MP4_VIDEO_OK and (not acodec or acodec in MP4_AUDIO_OK)
                and not job._opt.force_encode):
            tag = BoxTags.for_copy(vcodec, self.EXT)
            return (["-c", "copy", *tag, "-movflags", "+faststart"],
                    self.EXT, self.SUFFIX,
                    f"상자만 교체 — {vcodec} 영상은 손대지 않습니다"
                    + ("  (애플이 열도록 꼬리표만 hvc1 로)" if tag else ""))
        pix, note = PixelPlan.for_x26x(profile, keep_depth=False)
        if note:
            Console.info(f"계조  {note}")
        args = ["-c:v", "libx264", "-crf", str(preset["crf264"]),
                "-preset", preset["preset"], *pix,
                "-movflags", "+faststart"] + job._audio_args(info, preset)
        why = (f"{vcodec or '?'} 는 mp4 에 못 들어가서 h264 로 다시 인코딩"
               if vcodec not in MP4_VIDEO_OK else "요청대로 h264 로 다시 인코딩")
        return args, self.EXT, self.SUFFIX, why


class MovTarget(ConvertTarget):
    KEY = "mov"
    TITLE = "MOV (상자만 교체)"
    HINT = "확장자만 .mov 로. 안에 든 코덱도 화질도 그대로입니다"
    EXT = ".mov"

    def plan(self, job, target, info, profile):
        preset = self._preset(job)
        vcodec = info.vcodec if info else ""
        acodec = info.acodec if info else ""
        if (vcodec in MOV_VIDEO_OK and (not acodec or acodec in MOV_AUDIO_OK)
                and not job._opt.force_encode):
            tag = BoxTags.for_copy(vcodec, self.EXT)
            return (["-c", "copy", *tag], self.EXT, self.SUFFIX,
                    f"다시 담기만 — {vcodec} 을 비트 그대로 .mov 에 넣습니다 "
                    f"(안에 든 건 여전히 {vcodec} 입니다)"
                    + ("  · 애플이 열도록 꼬리표만 hvc1 로" if tag else ""))
        pix, note = PixelPlan.for_x26x(profile, keep_depth=True)
        if note:
            Console.info(f"계조  {note}")
        args = ["-c:v", "libx264", "-crf", str(preset["crf264"]),
                "-preset", preset["preset"], *pix] + job._audio_args(info, preset)
        return args, self.EXT, self.SUFFIX, f"{vcodec or '?'} 는 mov 에 못 들어가서 h264 로 다시 인코딩"


class NukeTarget(ConvertTarget):
    KEY = "nuke"
    TITLE = "누크용 마스터 (ProRes)"
    HINT = "Nuke · After Effects · Resolve 가 물고 늘어지지 않는 편집용 코덱"
    EXT = ".mov"
    SUFFIX = "_nuke"

    def plan(self, job, target, info, profile):
        acodec = info.acodec if info else ""
        args, why = PixelPlan.for_prores(profile)
        args += ["-c:a", "pcm_s16le"] if acodec else ["-an"]
        return args, self.EXT, self.SUFFIX, f"{why} 로 다시 인코딩 — 누크가 바로 읽습니다"


class DeliveryTarget(ConvertTarget):
    """
    납품 규약 한 벌을 통째로 건다 — MOV / ProRes 4444 XQ / 1920×1080 / 24fps.

    규약은 네 줄인데 손으로 맞추면 네 군데서 틀립니다. 특히 해상도와 fps 는
    "맞겠지" 하고 넘어갔다가 상대 쪽에서 반려됩니다. 그래서 여기서는 재 보고,
    안 맞으면 맞추면서 **무엇을 건드렸는지 말합니다.** 조용히 고치면 다음에
    또 같은 소스를 넣습니다.

    파일 이름은 `DeliverySlate` 가 규약(`샷코드_테스크_v###`)대로 짓습니다.
    """

    KEY = "delivery"
    TITLE = "납품 (ProRes 4444 XQ)"
    HINT = "MOV · 4444 XQ · 1920×1080 · 24fps · 샷코드_테스크_v###"
    EXT = ".mov"
    SUFFIX = "_delivery"          # 이름은 슬레이트가 짓는다. 여기는 쓰이지 않는다

    WIDTH, HEIGHT, FPS = 1920, 1080, 24.0
    PROFILE = "5"                 # prores_ks: 0 proxy · 1 lt · 2 std · 3 hq · 4 4444 · 5 4444xq

    def _frame(self, info: Optional[ProbeInfo]) -> tuple[list[str], str]:
        """규약 해상도로 맞춘다. 비율은 절대 안 늘린다 — 모자라면 레터박스."""
        if info and info.width == self.WIDTH and info.height == self.HEIGHT:
            return [], ""
        if info and info.width and info.height:
            Console.warn(f"{info.width}x{info.height} 를 {self.WIDTH}x{self.HEIGHT} 로 맞춥니다 — "
                         f"비율이 다르면 위아래(또는 좌우)에 검은 띠가 생깁니다")
        return (["-vf", f"scale={self.WIDTH}:{self.HEIGHT}:force_original_aspect_ratio=decrease"
                        f":flags=lanczos,pad={self.WIDTH}:{self.HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
                        f"setsar=1"],
                f"{self.WIDTH}x{self.HEIGHT} 로 맞춤")

    def _rate(self, info: Optional[ProbeInfo]) -> tuple[list[str], str]:
        """
        24fps 로 못 박는다.

        이미 24 면 아무 일도 안 일어납니다. 아니면 ffmpeg 가 프레임을 버리거나
        겹쳐 채우는데, 그건 리타임이지 변환이 아닙니다. 그래서 크게 말합니다 —
        길이를 제대로 바꿀 거면 리타임으로 가야 합니다.
        """
        fps = info.fps if info and info.fps else 0.0
        if fps and abs(fps - self.FPS) < 0.01:
            return ["-r", "24"], ""
        if fps:
            Console.warn(f"원본이 {fps:.3f}fps 입니다. 24fps 로 내보내면 프레임을 버리거나 "
                         f"겹쳐 채웁니다 — 길이를 제대로 맞추려면 리타임을 먼저 쓰세요")
        return ["-r", "24"], "24fps 로 고정"

    def plan(self, job, target, info, profile):
        alpha = bool(profile.has_alpha)
        pix = ["-pix_fmt", "yuva444p10le", "-alpha_bits", "16"] if alpha else \
              ["-pix_fmt", "yuv444p10le"]
        frame, framed = self._frame(info)
        rate, rated = self._rate(info)
        sound = ["-c:a", "pcm_s16le"] if (info and info.acodec) else ["-an"]
        args = ["-c:v", "prores_ks", "-profile:v", self.PROFILE, "-vendor", "apl0",
                *pix, *frame, *rate, *sound]
        said = " · ".join(x for x in ("ProRes 4444 XQ", framed, rated,
                                      "알파 유지" if alpha else "") if x)
        return args, self.EXT, self.SUFFIX, said


class SmallTarget(ConvertTarget):
    KEY = "small"
    TITLE = "용량 줄이기"
    HINT = "반드시 다시 인코딩합니다. 화질이 조금 깎입니다"
    EXT = ".mp4"
    SUFFIX = "_small"

    def plan(self, job, target, info, profile):
        preset = self._preset(job)
        ext = target.suffix.lower() if target.suffix.lower() in (".mp4", ".mov") else ".mp4"
        pix, note = PixelPlan.for_x26x(profile, keep_depth=False)
        if note:
            Console.info(f"계조  {note}")
        if job._opt.h265:
            args = ["-c:v", "libx265", "-crf", str(preset["crf265"]),
                    "-preset", preset["preset"], "-tag:v", "hvc1", *pix]
            why = f"h265 crf {preset['crf265']} — {preset['note']}"
        else:
            args = ["-c:v", "libx264", "-crf", str(preset["crf264"]),
                    "-preset", preset["preset"], *pix]
            why = f"h264 crf {preset['crf264']} — {preset['note']}"
        if ext == ".mp4":
            args += ["-movflags", "+faststart"]
        args += job._audio_args(info, preset)
        return args, ext, self.SUFFIX, why


class ImageTarget(ConvertTarget):
    """이미지 한 장씩 형식만 바꾼다. 영상 목적지와 규칙이 아예 다르다."""

    KEY = "image"
    TITLE = "이미지 형식 바꾸기"
    HINT = "PNG · JPG · WEBP 로. 크기도 같이 줄일 수 있습니다"
    EXT = ".png"
    SUFFIX = "_conv"
    KIND = "image"

    ENCODERS = {
        "png": ([], ".png"),
        "jpg": (["-q:v", "2"], ".jpg"),
        "webp": (["-quality", "92"], ".webp"),
        "exr": (["-c:v", "exr", "-compression", "zip16", "-format", "half"], ".exr"),
        "dpx": (["-c:v", "dpx"], ".dpx"),
    }

    def plan(self, job, target, info, profile):
        want = (job._opt.image_format or "png").lower()
        if want not in self.ENCODERS:
            raise FramingError(f'지원하지 않는 이미지 형식입니다: {want}')
        extra, ext = self.ENCODERS[want]
        pixels = (["-pix_fmt", "gbrapf32le" if profile.has_alpha else "gbrpf32le"]
                  if want == 'exr' else ["-pix_fmt", "yuvj444p"] if want == 'jpg' else [])
        args = ["-frames:v", "1", *extra, *pixels]
        return args, ext, self.SUFFIX, f"{target.suffix.lstrip('.').upper()} → {want.upper()}"


class GifTarget(ConvertTarget):
    KEY = 'gif'
    TITLE = 'GIF 애니메이션'
    HINT = '영상 또는 이미지 시퀀스를 움직이는 GIF로'
    KIND = 'motion'
    def plan(self, job, target, info, profile):
        return [], '.gif', '_animated', self.TITLE


class FramesTarget(ConvertTarget):
    KEY = 'frames'
    TITLE = '영상 → 이미지 시퀀스'
    HINT = '원본의 모든 프레임을 번호 붙은 이미지로'
    def plan(self, job, target, info, profile):
        return [], '', '_sequence', self.TITLE


class SequenceTarget(ConvertTarget):
    """번호 붙은 이미지 폴더를 영상 한 편으로 묶는다. Sequence 의 반대 방향."""

    KEY = "movie"
    TITLE = "시퀀스 → 영상"
    HINT = "번호 붙은 이미지 폴더를 mp4 · 누크용 마스터로 묶습니다"
    EXT = ".mp4"
    SUFFIX = "_movie"
    KIND = "sequence"

    def plan(self, job, target, info, profile):
        return [], self.EXT, self.SUFFIX, "시퀀스를 영상으로"


CONVERT_TARGETS: tuple[ConvertTarget, ...] = (
    Mp4Target(), MovTarget(), NukeTarget(), DeliveryTarget(), SmallTarget(),
    ImageTarget(), SequenceTarget(), GifTarget(), FramesTarget())
CONVERT_BY_KEY = {t.KEY: t for t in CONVERT_TARGETS}


class ConvertJob(TranscodeJob):
    KEY = "convert"
    ALIASES = ("mp4", "to-mp4", "mov", "to-mov", "prores", "nuke",
               "shrink", "compress", "small")
    TITLE = "Convert"
    HINT = "영상 · 이미지 · 시퀀스를 원하는 형식으로"
    ACCEPTS_IMAGE = True
    ACCEPTS_FOLDER = True
    NOTE = """
    영상 · 이미지 · 번호 붙은 시퀀스 폴더를 다 받습니다.
    넣은 것과 안 맞는 목적지를 골라 두면 맞는 쪽으로 알아서 옮기고 알려줍니다.

    이미지를 넣으면
      PNG · JPG · WEBP 로 바꿉니다. --max-height 로 크기도 같이 줄일 수 있습니다.
      (재보니 같은 그림이 PNG 153KB → JPG 24KB → WEBP 18KB 였습니다.)

    시퀀스 폴더를 넣으면
      영상 한 편으로 묶습니다. Sequence 의 반대 방향입니다.
      --prores-movie 를 붙이면 누크용 ProRes 로 묶고, 알파가 있으면 4444 로 갑니다.
      번호가 1부터 시작하지 않거나 중간이 비어 있어도 됩니다 —
      패턴을 짜 맞추지 않고 파일 목록을 그대로 먹입니다.

    영상을 넣으면, 어디로 보낼지 하나만 고르면 됩니다.

      MP4 (배포·업로드)
        가장 잘 열리는 형식입니다. 안에 든 코덱이 mp4 가 받는 것(h264 / hevc /
        mpeg4 / av1)이면 다시 인코딩하지 않고 상자만 갈아끼웁니다. 몇 초 걸리고
        화질 손실은 없습니다. 영상 데이터가 비트 단위로 그대로 옮겨갑니다.

      MOV (상자만 교체)
        확장자만 .mov 가 됩니다. h264 짜리를 .mov 로 바꿔도 안에 든 건 여전히
        h264 입니다. 화질이 좋아지지도, 편집이 수월해지지도 않습니다.
        편집용 마스터가 필요하면 아래를 쓰세요.

      누크용 마스터 (ProRes)
        Nuke · After Effects · Resolve 가 스크러빙할 때 버벅이지 않는 코덱입니다.
          · 용량이 10~30배 커집니다. 고장이 아니라 원래 그렇습니다.
          · 스크러빙 속도와 세대 손실 내성을 용량으로 사는 코덱입니다.
          · 배포·업로드용으로 쓰면 안 됩니다.
          · h264 를 ProRes 로 바꿔도 없던 화질이 생기지는 않습니다.
            이미 뭉개진 8bit 계조를 10bit 상자에 옮겨 담을 뿐입니다.
            다시 인코딩할 거라면 원본 렌더에서 바로 뽑는 게 맞습니다.
          · 알파가 있으면 알아서 ProRes 4444 로 갑니다. 422 HQ 는 알파를 못 담습니다.

      용량 줄이기
        반드시 다시 인코딩합니다. 되돌릴 수 없는 화질 손실이 생깁니다.
        원본은 그대로 두고 새 파일을 만드니, 마음에 안 들면 지우면 됩니다.
        이미 잘 압축된 파일은 오히려 커질 수 있습니다. 그러면 커졌다고 알려줍니다.

        가장 크게 줄이는 건 화질 단계가 아니라 해상도입니다. 4K → 1080p 면
        픽셀 수가 1/4 이 됩니다. 화질을 만지기 전에 "이걸 정말 4K 로 봐야 하나"를
        먼저 따져보세요.

        h265 는 같은 화질에 20~30% 더 작지만 인코딩이 2~4배 느리고,
        옛 기기나 일부 편집 프로그램에서 안 열립니다.

    공통
      · 알파 채널은 mp4 에 못 들어갑니다. 투명도가 필요하면 누크용 마스터나
        PNG 시퀀스를 쓰세요.
      · 소스의 색 규격(bt709 / 제한범위 등)을 결과에 그대로 적어 둡니다.
        안 적어 두면 다음 도구가 추측하고, 그 추측이 어긋나면 색이 밀립니다.
      · 10bit · 4:2:2 소스는 배포용으로 갈 때 8bit 로 내려갑니다. 내려간다고
        알려줍니다. 계조를 지키려면 누크용 마스터로 가세요.
"""

    @property
    def destination(self) -> ConvertTarget:
        return CONVERT_BY_KEY.get(self._opt.convert_to or "mp4", CONVERT_TARGETS[0])

    @staticmethod
    def kind_of(target: Path) -> str:
        """넣은 것이 무엇인가. 목적지를 고르는 기준이 된다."""
        if target.is_dir():
            return "sequence"
        return "image" if target.suffix.lower() in IMAGE_EXTENSIONS else "video"

    def _settle(self, target: Path) -> ConvertTarget:
        """
        고른 목적지가 넣은 것과 안 맞으면 맞는 쪽으로 데려간다.

        이미지를 넣어 놓고 "MP4 (배포)" 가 눌려 있다고 해서 거절하는 건
        도움이 안 된다. 넣은 것이 무엇인지는 우리가 이미 아니까,
        맞는 목적지로 옮기고 옮겼다고 말해 주면 된다.
        """
        kind = self.kind_of(target)
        where = self.destination
        if where.KIND == kind or (where.KEY == "gif" and kind in ("video", "sequence")):
            return where
        fallback = {"image": "image", "sequence": "movie", "video": "mp4"}[kind]
        moved = CONVERT_BY_KEY[fallback]
        Console.info(f"넣으신 것이 {'폴더(시퀀스)' if kind == 'sequence' else kind} 라"
                     f" 목적지를 '{moved.TITLE}' 로 바꿨습니다")
        return moved

    def run(self, target: Path) -> JobResult:
        where = self._settle(target)
        options = replace(self._opt, convert_to=where.KEY)
        if where.KEY in ('movie', 'gif', 'frames'):
            if where.KEY == 'frames':
                fmt = options.image_format if options.image_format in ExtractJob.SEQUENCE_ENCODERS else 'png'
                worker = ExtractJob(replace(options, at='all', count=0, every=0, frame_format=fmt), self._ff)
            else:
                worker = (GifJob if where.KEY == 'gif' else SequenceMovieJob)(options, self._ff)
            worker.should_stop = self.should_stop
            return worker.run(target)
        previous = self._opt
        self._opt = options
        try:
            return super().run(target)
        finally:
            self._opt = previous

    def _plan(self, target: Path, info: Optional[ProbeInfo]):
        where = self.destination
        Console.info(f"목적지  {where.TITLE}")
        return where.plan(self, target, info, self._color(target, info))

    def _named(self, target: Path, suffix: str, ext: str) -> Path:
        """납품만 규약대로 짓고, 나머지는 예전처럼 '원래이름 + 접미사'."""
        if self.destination.KEY != 'delivery':
            return super()._named(target, suffix, ext)
        slate = DeliverySlate.decide(target.stem, self._opt.delivery_shot,
                                     self._opt.delivery_task, self._opt.delivery_version)
        spot = slate.free_in(self._opt.out_dir or target.parent, ext,
                             overwrite=self._opt.overwrite)
        Console.info(f'이름  {spot.name}   (샷코드_테스크_v###)')
        return spot

    @staticmethod
    def _concat_list(images, fps):
        return SequenceInput.text(images, fps)


class CompareView(ABC):
    """
    두 소재를 나란히 놓고 보는 한 가지 방법.

    "어디가 달라졌나"는 눈으로 왔다 갔다 하며 찾는 게 제일 부정확하다.
    사람 눈은 두 그림을 번갈아 볼 때 위치를 기억하지 못한다. 그래서 도구가
    차이를 한 화면에 겹쳐 줘야 한다.
    """

    KEY = ""
    TITLE = ""
    BLURB = ""

    @abstractmethod
    def chain(self, seconds: float = 0.0) -> str:
        """[a][b] 두 입력을 받아 [v] 하나를 내놓는 filter_complex.

        seconds 는 소재 길이 — 시간에 따라 움직이는 보기가 이걸 참고한다."""


class OverlayView(CompareView):
    KEY = "overlay"
    TITLE = "겹치기"
    BLURB = "포개서 봅니다. 섞는 정도를 손잡이로 조절합니다 — 위치가 밀렸는지 볼 때"

    MIX = 0.5                   # 창에서는 손으로 조절한다. 이건 구워 낼 때의 기본값

    def chain(self, seconds: float = 0.0) -> str:
        return f"[a][b]blend=all_opacity={self.MIX:.2f}:shortest=1[v]"


class WipeView(CompareView):
    KEY = "wipe"
    TITLE = "반반 바"
    BLURB = "경계선이 좌우로 오가며 A 와 B 를 번갈아 덮습니다"

    SECONDS = 4.0               # 한 번 왕복하는 데 걸리는 시간

    def chain(self, seconds: float = 0.0) -> str:
        """
        경계가 왼쪽 끝과 오른쪽 끝 사이를 계속 오간다. 멈춰 있으면 경계 근처만 보게 된다.

        crop 으로 잘라 붙이는 방법은 쓸 수 없다. crop 의 폭은 필터를 세울 때
        한 번만 계산되고 그 뒤로는 안 변해서, 시간이 흘러도 경계가 그대로다.
        픽셀 단위로 어느 쪽을 쓸지 고르는 blend 식이라야 실제로 움직인다.

        짧은 소재는 한 번 왕복하는 시간도 같이 줄인다. 1초짜리에 4초 왕복을 쓰면
        경계가 반만 지나가고 끝나서, 정작 반대쪽은 못 보고 만다.
        """
        period = self.SECONDS
        if seconds and seconds > 0.2:
            period = max(1.0, min(self.SECONDS, seconds))
        wave = f"0.5+0.5*sin(2*PI*T/{period:.2f})"
        return f"[a][b]blend=all_expr='if(lt(X,W*({wave})),A,B)':shortest=1[v]"



class FlickerView(CompareView):
    KEY = "flicker"
    TITLE = "번갈아"
    BLURB = "A 와 B 를 빠르게 번갈아 보여줍니다. 미세한 차이는 이게 제일 잘 보입니다"

    EVERY = 6                   # 프레임 단위. 24fps 에서 초당 네 번 바뀐다

    def chain(self, seconds: float = 0.0) -> str:
        return "[a][b]blend=all_expr='if(eq(mod(floor(T/0.26),2),0),B,A)':shortest=1[v]"


class SideBySideView(CompareView):
    KEY = "side"
    TITLE = "나란히"
    BLURB = "왼쪽 A, 오른쪽 B. 전체 인상을 비교할 때 씁니다"

    def chain(self, seconds: float = 0.0) -> str:
        return "[a][b]hstack=inputs=2:shortest=1[v]"


COMPARE_VIEWS: tuple[CompareView, ...] = (
    WipeView(), OverlayView(), FlickerView(), SideBySideView())
COMPARE_BY_KEY = {v.KEY: v for v in COMPARE_VIEWS}


class ConfiguredCompareView(CompareView):
    """An export snapshot of the viewer controls; registry defaults stay immutable."""
    def __init__(self, base, state):
        import math
        self.base = base
        self.KEY, self.TITLE = base.KEY, base.TITLE
        self.mode = state.get('mode', base.KEY)
        def fraction(key):
            value = float(state.get(key, .5))
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise FramingError('대조 비율은 0~1이어야 합니다.')
            return value
        self.split, self.mix = fraction('split'), fraction('mix')
        self.swapped = bool(state.get('swapped'))
        self.show_b = bool(state.get('showB', True))
        self.snapshot = False

    def chain(self, seconds=0.0):
        if self.mode in ('a', 'b'):
            first = (self.mode == 'a') != self.swapped
            return '[a]null[v];[b]nullsink' if first else '[b]null[v];[a]nullsink'
        if self.KEY == 'wipe':
            return f"[a][b]blend=all_expr='if(lt(X,round(W*{self.split:.9f})),A,B)':shortest=1[v]"
        if self.KEY == 'overlay':
            return f"[a][b]blend=all_expr='A*{1-self.mix:.6f}+B*{self.mix:.6f}':shortest=1[v]"
        if self.KEY == 'flicker' and self.snapshot:
            return '[b]null[v];[a]nullsink' if self.show_b else '[a]null[v];[b]nullsink'
        return self.base.chain(seconds)


class CompareAudioPolicy:
    """Select one original audio track independently from the visual A/B order."""
    def __init__(self, ff, first, second, state=None):
        state = state or {}
        self.source = state.get('audioSource', 'auto')
        if self.source not in ('auto', 'a', 'b', 'none'):
            raise FramingError('대조 소리 선택이 올바르지 않습니다.')
        try:
            self.volume = float(state.get('audioVolume', 1))
        except (TypeError, ValueError):
            raise FramingError('대조 음량이 올바르지 않습니다.')
        if not math.isfinite(self.volume) or not 0 <= self.volume <= 1:
            raise FramingError('대조 음량은 0~100%로 설정하세요.')
        available = [bool((info := ff.probe(p)) and info.acodec) for p in (first, second)]
        if self.source == 'auto':
            self.source = 'a' if available[0] else 'b' if available[1] else 'none'
        elif self.source != 'none' and not available['ab'.index(self.source)]:
            raise FramingError(f'{self.source.upper()} 파일에 오디오가 없습니다. 다른 소리 또는 소리 끄기를 선택하세요.')

    def arguments(self, swapped=False, seconds=0):
        if self.source == 'none' or self.volume == 0:
            return ['-an']
        index = 'ab'.index(self.source) ^ bool(swapped)
        # Audio follows the shared timeline; pad short audio to the final video length.
        filters = f'aresample=async=1:first_pts=0,volume={self.volume:.8g}'
        if seconds > 0:
            filters += f',apad,atrim=duration={seconds:.12g}'
        return ['-map', f'{index}:a:0', '-af', filters,
                '-c:a', 'aac', '-b:a', '192k', '-ac', '2']


class CompareBurner:
    """
    보고 있는 대조를 파일 한 장으로 굽는다 — 영상이면 mp4, 그림이면 png.

    창에서 "mp4 로 저장" 을 눌러도, --keep 으로 남길 때도 전부 여기로 온다.
    굽는 자리는 한 곳이어야 창에서 본 것과 파일이 어긋나지 않는다.
    """

    def __init__(self, ff: "FFmpegTool") -> None:
        self._ff = ff

    def name_for(self, first: Path, view: CompareView, video: bool,
                 folder: Optional[Path] = None) -> Path:
        """원본 옆에, 무엇을 어떻게 비교한 것인지 이름만 봐도 알게."""
        where = folder or first.parent
        ext = ".mp4" if video else ".png"
        base = f"대조_{view.KEY}_{first.stem}"
        destination = where / (base + ext)
        turn = 2
        while destination.exists():                 # 있던 것을 덮지 않는다
            destination = where / f"{base}_{turn}{ext}"
            turn += 1
        return destination

    def burn(self, first: Path, second: Path, view: CompareView, destination: Path,
             size: tuple[int, int] = (0, 0), seconds: float = 0.0,
             video: bool = True, fps: float = 24.0, region=None,
             output: str = '', frame=None, audio=None, swapped=False) -> Optional[Path]:
        import math
        width, height = size
        output = output or ('video' if video else 'frame')
        fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
               if width and height else "setsar=1")
        timeline = f"setpts=PTS-STARTPTS,fps={FrameRate.from_value(fps).text}:round=up:eof_action=pass," if video else ''
        # RGB planes keep a wipe boundary on the exact pixel, without chroma subsampling.
        chain = f"[0:v]{timeline}{fit},format=gbrp[a];[1:v]{timeline}{fit},format=gbrp[b];"
        if isinstance(view, ConfiguredCompareView):
            view.snapshot = output == 'frame'
        chain += view.chain(seconds if video else 0.0).replace('[v]', '[composite]')
        current = 'composite'
        if region:
            x, y, rw, rh = region
            if view.KEY == 'side' and getattr(view, 'mode', '') not in ('a', 'b'):
                chain += (f';[{current}]split[cl][cr];[cl]crop={rw}:{rh}:{x}:{y}:exact=1[l];'
                          f'[cr]crop={rw}:{rh}:{width+x}:{y}:exact=1[r];[l][r]hstack[region]')
            else:
                chain += f';[{current}]crop={rw}:{rh}:{x}:{y}:exact=1[region]'
            width, height = rw, rh
            current = 'region'
        if output == 'frame' and video:
            chain += f";[{current}]select=eq(n\\,{int(frame or 0)})[picked]"
            current = 'picked'
        # H.264 4:2:0 is playable in browsers, including odd-sized selections.
        final_filter = 'pad=ceil(iw/2)*2:ceil(ih/2)*2' if output == 'video' else 'null'
        chain += f';[{current}]{final_filter}[v]'
        frames = FrameRate.frame_count(seconds, fps) if video and seconds > 0 else None
        if output == 'video':
            pixel = 'yuv420p'
            tail = ['-c:v', 'libx264', '-crf', '14', '-preset', 'medium',
                    '-pix_fmt', pixel, '-movflags', '+faststart']
            audio = audio or CompareAudioPolicy(self._ff, first, second)
            duration = frames / fps if frames else seconds
            tail += audio.arguments(swapped=swapped, seconds=duration)
            if frames:
                # Trim the video filter instead of -frames:v, which can end audio early.
                chain = chain.replace('[v]', f'[untrimmed];[untrimmed]trim=end_frame={frames}[v]')
                tail += ['-t', f'{duration:.12g}']
        else:
            tail = ['-c:v', 'png', '-pix_fmt', 'rgb24', '-fps_mode', 'passthrough']
            if output == 'frame': tail += ['-frames:v', '1', '-update', '1']
            elif frames: tail += ['-frames:v', str(frames)]
            if output == 'frames': tail += ['-start_number', '1']
        destination.parent.mkdir(parents=True, exist_ok=True)
        code = self._ff.run(["-y", "-i", str(first), "-i", str(second),
                             "-filter_complex", chain, "-map", "[v]",
                             *tail, str(destination)],
                            total_seconds=(seconds if video else 0.0))
        exists = any(destination.parent.glob('frame.*.png')) if output == 'frames' else destination.exists()
        if code != 0 or not exists:
            return None
        return destination




class PlayableProxy:
    """
    브라우저가 열 수 있는 형태로 바꿔 준다.

    <video> 가 실제로 여는 건 mp4(h264 8bit) 와 webm 정도다.
    ProRes · DNxHD · HEVC · 10bit · .mov 상자는 화면이 그냥 까맣게 나온다.
    작업용 소재는 대부분 그런 것들이라, 원본을 그대로 넘기면 대조 자체가 안 된다.
    그래서 볼 수 있는 형태로 한 벌 만들어 두고, 그걸 본다.

    이건 어디까지나 보기용이다. --keep 으로 굽는 대조 영상은 원본을 쓴다.
    """

    SAFE_CODEC = frozenset({"h264", "vp8", "vp9", "av1"})
    SAFE_BOX = frozenset({".mp4", ".m4v", ".webm"})
    SAFE_IMAGE = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
    TALL = 1440                 # 이보다 크면 보기용으로만 줄인다

    def __init__(self, ff: "FFmpegTool", folder: Path, cancelled=None) -> None:
        self._ff = ff
        self._folder = folder
        self._cancelled = cancelled or (lambda: False)

    def _check_cancelled(self):
        if self._cancelled():
            raise FramingError("중단했습니다.")

    @classmethod
    def why(cls, path: Path, info: Optional[ProbeInfo], video: bool) -> str:
        """바꿔야 하는 이유. 빈 문자열이면 그대로 써도 된다."""
        suffix = path.suffix.lower()
        if not video:
            return "" if suffix in cls.SAFE_IMAGE else f"{suffix.lstrip('.').upper()} 는 브라우저가 못 엽니다"
        if suffix not in cls.SAFE_BOX:
            return f"{suffix} 상자는 브라우저가 못 엽니다"
        if info and info.vcodec and info.vcodec not in cls.SAFE_CODEC:
            return f"{info.vcodec} 는 브라우저가 못 엽니다"
        if info and ColorProfile.from_probe(info).bits > 8:
            return "10bit 는 브라우저가 못 엽니다"
        allowed_audio = {'opus', 'vorbis'} if suffix == '.webm' else {'aac', 'mp3'}
        if info and info.acodec and info.acodec not in allowed_audio:
            return f"{info.acodec} 오디오를 재생용으로 바꿉니다"
        if info and info.pix_fmt and info.pix_fmt not in ('yuv420p', 'yuvj420p'):
            return "영상 픽셀 형식을 재생용으로 바꿉니다"
        return ""

    def make(self, path: Path, info: Optional[ProbeInfo], video: bool,
             name: str) -> Optional[Path]:
        """
        볼 수 있는 한 벌.

        한 번 해 보고 안 되면 포기하지 않는다. 작업용 소재는 별게 다 있어서
        (홀수 크기, 이상한 프레임레이트, x264 가 빠진 ffmpeg 빌드…) 첫 판에
        실패하는 일이 실제로 생긴다. 그때 그냥 원본을 넘기면 까만 화면이 나오고,
        사람은 프로그램이 고장 났다고 생각한다. 그래서 순서대로 몇 번 더 해 본다.
        """
        seconds = info.duration if info else 0.0
        for order, plan in enumerate(self._plans(path, info, video, name), 1):
            self._check_cancelled()
            out, args = plan
            if out.exists():
                try:
                    out.unlink()
                except OSError:
                    pass
            code = self._ff.run(args, quiet=True, total_seconds=seconds)
            self._check_cancelled()
            if code == 0 and out.exists() and out.stat().st_size > 1024:
                if order > 1:
                    Console.info(f"{path.name} 은 {order}번째 방법으로 바꿨습니다")
                return out
        return None

    def _plans(self, path: Path, info: Optional[ProbeInfo], video: bool,
               name: str) -> list[tuple[Path, list[str]]]:
        """해 볼 방법들. 위에서부터 하나씩, 되는 게 나올 때까지."""
        if not video:
            png = self._folder / f"{name}.png"
            return [
                (png, ["-y", "-i", str(path), "-frames:v", "1",
                       "-pix_fmt", "rgb24", str(png)]),
                # EXR · HDR 처럼 선형으로 담긴 그림은 그냥 뽑으면 새까맣게 나온다
                (png, ["-y", "-apply_trc", "iec61966_2_1", "-i", str(path),
                       "-frames:v", "1", "-pix_fmt", "rgb24", str(png)]),
                (png, ["-y", "-i", str(path), "-vf", "format=rgb24",
                       "-frames:v", "1", str(png)]),
            ]

        mp4 = self._folder / f"{name}.mp4"
        webm = self._folder / f"{name}.webm"
        shrink = f"scale=-2:{self.TALL}," if info and info.height > self.TALL else ""
        even = "scale=trunc(iw/2)*2:trunc(ih/2)*2,"     # 홀수 크기는 x264 가 거른다
        return [
            (mp4, ["-y", "-i", str(path), "-vf", f"{shrink}format=yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-movflags", "+faststart", "-map", "0:v:0", "-map", "0:a:0?", "-c:a", "aac", "-ac", "2", str(mp4)]),
            # 홀수 크기 · 이상한 타임스탬프까지 다림질해서 다시
            (mp4, ["-y", "-fflags", "+genpts", "-i", str(path),
                   "-vf", f"{shrink}{even}format=yuv420p",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
                   "-movflags", "+faststart", "-map", "0:v:0", "-map", "0:a:0?", "-c:a", "aac", "-ac", "2", str(mp4)]),
            # x264 가 없는 빌드도 있다. webm 은 브라우저가 똑같이 잘 연다
            (webm, ["-y", "-i", str(path), "-vf", f"{shrink}{even}format=yuv420p",
                    "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34",
                    "-deadline", "realtime", "-cpu-used", "8", "-map", "0:v:0", "-map", "0:a:0?", "-c:a", "libopus", "-ac", "2", str(webm)]),
        ]


class CompareJob(Job):
    """두 파일을 나란히 놓고 무엇이 달라졌는지 본다."""

    KEY = "compare"
    ALIASES = ("diff", "ab")
    TITLE = "Compare"
    HINT = "두 영상 · 이미지를 나란히 놓고 달라진 곳 찾기"
    NOTE = """
    파일 두 개를 넣으세요. 먼저 넣은 것이 A, 나중이 B 입니다.
    (폴더를 넣으면 그 안의 앞의 두 개를 씁니다.)

    실행하면 대조 창이 바로 열립니다. 인코딩을 기다릴 것도, 파일이 쌓일 것도 없습니다.

    창 안에서 네 가지를 오갈 수 있습니다  (숫자키 1~4)
      1 반반 바   경계선을 잡고 좌우로 끌면 A 와 B 가 갈립니다
      2 겹치기    포개서 봅니다. 섞기 손잡이로 0~100% 조절 — 위치가 밀렸는지 볼 때
      3 번갈아    빠르게 교대합니다. 미세한 차이는 이게 제일 잘 보입니다
      4 나란히    왼쪽 A, 오른쪽 B. 여기 안에 "둘 다 · A만 · B만" 이 같이 있습니다

      바 끌기 · ← → 로 미세 조정 (Shift 를 누르면 더 잘게)
      A · B 키로 한쪽만 · Space 재생·멈춤
      영상이면 두 쪽이 같은 지점을 보도록 재생 위치를 계속 맞춰 둡니다.

    --view 로 어느 보기로 열지 정합니다 (wipe · overlay · flicker · side).

    남는 것 없이 봅니다
      대조는 결과물이 아니라 확인하는 행위입니다. 볼 때마다 원본 폴더에 파일이 쌓이면
      정작 필요한 파일이 묻힙니다. 그래서 임시 자리에 만들고, Framing 을 닫을 때 지웁니다.
      남겨서 남에게 보내야 하면 --keep 을 붙이세요. 그때는 원본 옆에 폴더를 만들고
      고른 보기로 구운 대조 영상도 같이 넣어 줍니다.

    브라우저가 못 여는 형식이면 알아서 바꿔 놓고 봅니다
      창의 재생기가 실제로 여는 건 mp4(h264 8bit) 와 webm 정도입니다.
      ProRes · DNxHD · HEVC · 10bit · .mov 상자는 아무 말 없이 까만 화면만 나옵니다.
      그래서 미리 알아보고, 못 여는 형식이면 볼 수 있는 한 벌을 만들어 그걸 겁니다.
      창 위쪽에 "보기용으로 변환함" 이라고 붙습니다 — 원본 픽셀이 아니라는 뜻입니다.
      원본은 손대지 않고, --keep 으로 굽는 대조 영상도 원본에서 만듭니다.
      1440p 가 넘으면 보기용만 그 크기로 줄입니다. 줄였다고 알려줍니다.

    알아둘 것
      · 해상도가 다르면 A 기준 캔버스에 비율을 유지해 맞추고 검은 여백을 넣습니다.
      · 길이가 다르면 짧은 쪽에서 끝납니다.
      · 색 규격이 서로 다르면 그것부터 알려줍니다. 그게 차이의 원인일 때가 많습니다."""
    PAIRED = True
    NEEDS_FFMPEG = True
    ACCEPTS_FOLDER = True
    ACCEPTS_IMAGE = True

    def run_pair(self, first: Path, second: Path) -> JobResult:
        result = JobResult(target=first)
        keeping = bool(self._opt.keep_compare)
        folder = (self._output_dir(first, "_compare") if keeping
                  else Scratch.folder("compare"))
        view = COMPARE_BY_KEY.get(self._opt.view or "wipe", COMPARE_VIEWS[0])
        left, right = self._probe_both(first, second)
        video = self._is_video(first, left)
        if video != self._is_video(second, right):
            raise FramingError('대조는 영상 두 개 또는 이미지 두 개를 선택하세요.')
        durations = [p.duration for p in (left, right) if p and p.duration > 0]
        seconds = min(durations) if durations else 0.0

        Console.info(f"보기  {view.TITLE} — {view.BLURB}")
        size = (left.width, left.height) if left else (0, 0)
        page = ComparePage(first.name, second.name, video, size, start=view.KEY,
                           fps=(left.fps if left and left.fps else 24.0), language=self._opt.language,
                           audio=(bool(left and left.acodec), bool(right and right.acodec)))

        # 보기용을 못 만들어도 창은 띄운다. 바깥 브라우저로 내보내 봐야 거기서도
        # 똑같이 까맣게 나올 뿐이고, 창 안에 있어야 "mp4 로 저장" 을 바로 누를 수
        # 있다 — 굽는 건 원본에서 하니 이 형식도 제대로 나온다.
        first_src, second_src, note = self._watchable(
            first, second, left, right, video, folder) or (first, second, "")

        self._abort_if_asked()
        # 앱이 있으면 창 안에서 바로 보여준다. 브라우저를 새로 띄우고 파일을
        # 남기는 건, 앱이 없어서 달리 보여줄 방법이 없을 때의 대비책이다.
        shown = False
        if self.show_inline is not None:
            shown = bool(self.show_inline(page.render("__A_SRC__", "__B_SRC__"),
                                          {"a": str(first_src), "b": str(second_src),
                                           # 구울 때는 보기용이 아니라 원본을 쓴다
                                           "sourceA": str(first), "sourceB": str(second),
                                           "width": size[0], "height": size[1],
                                           "seconds": seconds, "fps": (left.fps if left and left.fps else 24.0),
                                           "video": video, "note": note,
                                           "title": f"{first.name} ↔ {second.name}"}))
        if shown:
            Console.ok("창 안에 대조를 띄웠습니다")

        made: list[Path] = []
        if keeping or not shown:
            # 파일로 남기는 뷰어에도 볼 수 있는 쪽을 건다 — 원본을 걸면
            # 브라우저에서 열어 봐야 까만 화면이라 남기는 뜻이 없다
            made.append(self._page(first_src, second_src, folder, video, size, view,
                                   names=(first.name, second.name)))
        if keeping:
            self._abort_if_asked()
            movie = self._burn(first, second, folder, view, left, right, video)
            if movie:
                made.append(movie)

        result.outputs = made
        result.output_dir = folder if keeping else None
        result.ok = True
        result.message = f"A {first.name}  ↔  B {second.name}  ·  {view.TITLE}"

        if keeping:
            Console.info(f"파일로도 남겼습니다: {folder}")
        elif not shown:
            if open_externally(made[0]):
                Console.ok("대조 창을 열었습니다")
            else:
                Console.info(f"뷰어를 직접 여세요: {made[0]}")
            Console.info("잠깐 보는 용도라 임시 자리에 만들었습니다 "
                         "— Framing 을 닫으면 지워집니다")
        return result

    def run(self, target: Path) -> JobResult:       # 짝이 안 맞을 때만 여기로 온다
        raise FramingError("Compare 는 파일 두 개가 필요합니다. A 와 B 를 같이 넣어 주세요.")

    def _watchable(self, first: Path, second: Path, left: Optional[ProbeInfo],
                   right: Optional[ProbeInfo], video: bool,
                   folder: Path) -> Optional[tuple[Path, Path, str]]:
        """
        창에 걸 수 있는 한 쌍을 만든다.

        ProRes 나 .mov 를 그대로 걸면 재생기가 아무 말 없이 까만 화면만 보여준다.
        무엇이 잘못됐는지 알 길이 없는 게 제일 나쁘다. 그래서 미리 알아보고,
        못 여는 형식이면 볼 수 있는 한 벌을 만들어 그걸 건다.

        한쪽만 실패해도 나머지 한쪽은 살린다. 예전에는 둘 중 하나만 실패해도
        통째로 포기하고 바깥 브라우저로 넘겼는데, 거기서도 어차피 까맣게 나왔다.
        """
        reasons = [PlayableProxy.why(first, left, video),
                   PlayableProxy.why(second, right, video)]
        if not any(reasons):
            return first, second, ""

        Console.step(f"{reasons[0] or reasons[1]} — 볼 수 있는 형태로 바꾸는 중")
        maker = PlayableProxy(self._ff, folder, cancelled=self.should_stop)
        made: list[Path] = []
        stuck: list[str] = []
        for path, info, name, why in ((first, left, "viewA", reasons[0]),
                                      (second, right, "viewB", reasons[1])):
            if not why:                      # 이 쪽은 그대로 걸어도 된다
                made.append(path)
                continue
            proxy = maker.make(path, info, video, name)
            if proxy is None:
                Console.fail(f"{path.name} 은 창에서 볼 수 있는 형태로 바꾸지 못했습니다")
                said = self._ff.why_failed()
                if said:                     # ffmpeg 이 남긴 말을 그대로 — 원인을 알 수 있게
                    Console.info(f"ffmpeg: {said}")
                made.append(path)            # 원본이라도 건다 — 나머지 한쪽은 보인다
                stuck.append(path.name)
            else:
                made.append(proxy)

        if stuck:
            Console.info("그쪽은 까맣게 나옵니다. 'mp4 로 저장' 은 원본으로 굽기 때문에 "
                         "이 형식도 제대로 나옵니다")
            return made[0], made[1], f"{', '.join(stuck)} 은 창에서 못 엽니다 — mp4 로 저장을 쓰세요"

        shrunk = bool(left and left.height > PlayableProxy.TALL)
        note = "보기용으로 변환함" + (f" · {PlayableProxy.TALL}p 로 줄임" if shrunk else "")
        Console.info(f"{note} (원본은 손대지 않았습니다)")
        return made[0], made[1], note

    # --- 살펴보기 ---------------------------------------------------------

    def _probe_both(self, first: Path, second: Path
                    ) -> tuple[Optional[ProbeInfo], Optional[ProbeInfo]]:
        left = self._ff.probe(first)
        right = self._ff.probe(second)
        if left and right:
            Console.info(f"A  {left.width}x{left.height}  {left.fps:.2f}fps  "
                         f"{hms(left.duration)}  {left.vcodec or '이미지'}")
            Console.info(f"B  {right.width}x{right.height}  {right.fps:.2f}fps  "
                         f"{hms(right.duration)}  {right.vcodec or '이미지'}")
            if (left.width, left.height) != (right.width, right.height):
                Console.warn(f"해상도가 다릅니다 — B 를 {left.width}x{left.height} 로 "
                             "비율을 유지해 검은 여백으로 맞춥니다")
            self._compare_color(first, second, left, right)
        return left, right

    def _compare_color(self, first: Path, second: Path,
                       left: ProbeInfo, right: ProbeInfo) -> None:
        one = self._color(first, left)
        two = self._color(second, right)
        if one.summary == two.summary:
            return
        Console.warn("두 파일의 색 규격이 다릅니다 — 차이의 원인이 여기일 수 있습니다")
        Console.info(f"     A  {one.summary}")
        Console.info(f"     B  {two.summary}")

    @staticmethod
    def _is_video(path: Path, info: Optional[ProbeInfo]) -> bool:
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            return True
        return bool(info and info.duration > 0.2)

    # --- 만들기 -----------------------------------------------------------

    def _page(self, first: Path, second: Path, folder: Path, video: bool,
              size: tuple[int, int], view: CompareView,
              names: tuple[str, str] = ()) -> Path:
        """뷰어 옆에 두 파일을 둔다. 상대 경로가 제일 안 깨진다 (링크로 두면 즉시 끝난다)."""
        a = link_or_copy(first, folder / f"A{first.suffix.lower()}")
        b = link_or_copy(second, folder / f"B{second.suffix.lower()}")
        # 화면에 쓰는 이름은 원본 이름이라야 무엇을 보고 있는지 안다.
        # 거는 파일은 A · B 로 두되(상대 경로가 제일 안 깨진다) 이름표는 원본으로.
        show = names if names else (a.name, b.name)
        page = ComparePage(show[0], show[1], video, size, start=view.KEY,
                           fps=(self._ff.probe(first).fps or 24.0) if video else 24.0, language=self._opt.language,
                           audio=tuple(bool((info := self._ff.probe(p)) and info.acodec) for p in (first, second)) if video else (False, False))
        destination = folder / ComparePage.NAME
        destination.write_text(page.render(a.name, b.name), encoding="utf-8")
        return destination

    def _burn(self, first: Path, second: Path, folder: Path, view: CompareView,
              left: Optional[ProbeInfo], right: Optional[ProbeInfo],
              video: bool) -> Optional[Path]:
        burner = CompareBurner(self._ff)
        ext = ".mp4" if video else ".png"
        made = burner.burn(first, second, view, folder / f"대조_{view.KEY}{ext}",
                           size=(left.width if left else 0, left.height if left else 0),
                           seconds=min((p.duration for p in (left, right) if p and p.duration > 0), default=0.0),
                           fps=(left.fps if left and left.fps else 24.0), video=video)
        if made is None:
            Console.warn("대조 영상은 만들지 못했습니다 — 뷰어는 그대로 씁니다")
        return made








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




class PlateRifeJob(Job):
    """
    짧은 mov 플레이트를 RIFE 로 늘리되, 플레이트는 손대지 않는다.

    한 번에 다 하지 않고 두 번에 나눠 부른다 — 가운데에 RIFE 가 끼기 때문이다.
      1) 내보내기 : plate/(원본 계조) · for_rife/(8bit 복사본) · rife.json
      2) RIFE     : 등록된 실행 파일이 있으면 자동, 없으면 명령을 적어 주고 멈춤
      3) 가져오기 : 폴더를 다시 넣으면 원본 자리는 plate/ 에서, 새 자리만 RIFE 에서

    원본 프레임이 RIFE 를 거치지 않는 게 핵심이다. 그래서 10bit 이 산다.
    """

    KEY = 'plate'
    ALIASES = ('rife', 'plate-rife')
    TITLE = '플레이트 늘리기 (RIFE)'
    HINT = '원본 계조를 지키면서 사이만 RIFE 로 채움'
    NEEDS_FFMPEG = True
    ACCEPTS_FOLDER = True
    NOTE = """
    두 번에 나눠 돌립니다.
      framing plate 컷.mov --frames 96        내보내기 (폴더가 생깁니다)
      framing plate 컷.mov_rife               가져오기 (RIFE 결과를 넣고 다시)

    RIFE 실행 파일을 --rife 로 주면 가운데 단계까지 한 번에 갑니다.
    정수배(2·4·8)로 뽑은 뒤 목표 장수만 골라냅니다 — 그래야 원본이 격자 위에
    정확히 놓여서 그대로 통과합니다. 임의 비율로 한 번에 늘리면 원본이 거의
    안 남습니다. 알파는 다루지 않습니다."""

    def run(self, target: Path) -> JobResult:
        if target.is_dir() and (target / PlateExport.MANIFEST).exists():
            return self._bring_back(target)
        return self._send_out(target)

    # --- 1단계 -----------------------------------------------------------

    def _send_out(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        self._ff.ensure()
        spec = self._read_plate(target)
        wanted = self._wanted(spec)
        grid = DenseGrid(spec.frames, wanted, snap=not self._opt.plate_no_snap)
        Console.info(f'계획  {grid.summary()}')
        short = grid.unavoidable_doubles
        if short:
            # 격자보다 많이 달라고 하면 산수로 겹친다. 돌리기 전에 말한다.
            Console.warn(f'{wanted}장은 RIFE {grid.multiplier}배 격자'
                         f'({grid.dense_count}장)보다 많습니다 '
                         f'— {short}장은 같은 그림이 한 번 더 나옵니다')
        folder = unique_path((self._opt.out_dir or target.parent) / (target.stem + PlateExport.FOLDER))
        PlateExport(target, spec, grid, self._ff.ensure(),
                    self._abort_if_asked).run(folder)

        if grid.needs_rife:
            runner = RifeRunner(self._opt.rife_program, self._abort_if_asked)
            ran = runner.run(folder / 'for_rife', folder / 'rife', grid.multiplier)
            if ran:
                return self._bring_back(folder)
            Console.info(f'다음  RIFE 결과를 {folder / "rife"} 에 넣고 다시 부르세요')
            Console.info(f'      framing plate "{folder}"')
        else:
            return self._bring_back(folder)
        result.outputs = [folder / PlateExport.MANIFEST]
        result.output_dir = folder
        result.ok = True
        result.message = f'내보냄 · {grid.summary()}'
        return result

    def _read_plate(self, target: Path) -> PlateSpec:
        spec = PlateSpec.read(_probe_plate(self._ff.ensure_probe(), target), target)
        if spec.frames < 2:
            raise FramingError('플레이트가 두 장은 되어야 사이를 채울 수 있습니다.')
        Console.info(f'플레이트  {spec.frames}프레임 · {spec.fps:g}fps · '
                     f'{spec.pix_fmt or "규격 안 적힘"} · {spec.codec or "?"}')
        return spec

    def _wanted(self, spec: PlateSpec) -> int:
        return _wanted_frames(self._opt, spec)

    # --- 3단계 -----------------------------------------------------------

    def _bring_back(self, folder: Path) -> JobResult:
        result = JobResult(target=folder)
        assemble = PlateAssemble(folder, self._ff.ensure(), self._abort_if_asked)
        problems = assemble.complaints()
        if problems:
            for line in problems:
                Console.warn(line)
            raise FramingError('가져오지 못했습니다 — 위 내용을 확인하세요.')
        frames, report = assemble.build()
        Console.info(f'모음  원본 그대로 {report["fromPlate"]}장 · '
                     f'RIFE 로 채운 자리 {report["fromRife"]}장'
                     + (f' · 원본을 잡으려고 당긴 자리 {report["snapped"]}장'
                        if report['snapped'] else ''))
        # 되돌릴 수 있게 쪽지를 남긴다 — 파일로도 두고 영상 안에도 심는다
        note = assemble.note(report['frames'])
        movie = assemble.mux(frames, folder / f'{Path(assemble.spec.source).stem}_plate.mov',
                             note)
        Console.info(f'되묶기  {assemble.spec.pix_fmt or "소스 그대로"} · 색 표기 그대로')
        Console.info(f'메모  {note.beside(movie).name} · {note.summary()}')
        Console.info(f'되돌리기  framing revert "{movie.name}"')
        result.outputs = [movie, note.beside(movie), folder / PlateExport.MANIFEST]
        result.output_dir = folder
        result.ok = True
        result.message = (f'{report["frames"]}프레임 · 원본 {report["fromPlate"]}장 + '
                          f'RIFE {report["fromRife"]}장  ·  {movie.name}')
        return result


class RifeRetimeJob(PlateRifeJob):
    """Desktop adapter: validate first, then reuse the tested RIFE pipeline."""

    TITLE = '리타임 · RIFE'

    def _wanted(self, spec):
        plan = RetimePlan.decide(spec.frames, spec.fps, frames=self._opt.target_frames,
                                 seconds=self._opt.target_seconds, speed=self._opt.speed)
        if plan.refusal:
            raise FramingError(plan.refusal)
        if plan.target < 2:
            raise FramingError('RIFE 결과는 2프레임 이상으로 정해 주세요.')
        if plan.slowing:
            runner = RifeRunner(self._opt.rife_program, self._abort_if_asked)
            if not runner.ready:
                raise FramingError('RIFE 구성요소가 없습니다. 앱 압축을 모두 풀거나 보간 방식에서 FFmpeg를 선택하세요.')
            runner._model()
        return plan.target

    def run(self, target):
        if not target.is_file() or target.suffix.lower() not in VIDEO_EXTENSIONS:
            raise FramingError('RIFE는 영상 파일을 사용합니다. 이미지 시퀀스는 먼저 영상으로 변환하거나 FFmpeg를 선택하세요.')
        if self._ff.probe(target).alpha:
            raise FramingError('RIFE는 알파 영상을 지원하지 않습니다. 투명도를 유지하려면 복제 또는 겹치기를 선택하세요.')
        result = super().run(target)
        result.next_paths = [str(p) for p in result.outputs if p.suffix.lower() in VIDEO_EXTENSIONS]
        return result


class UiJobSelection:
    """Resolve one visible retime tool to its execution strategy."""

    @staticmethod
    def resolve(key, options):
        if key == 'retime' and options.retime_mode == 'rife':
            return RifeRetimeJob
        found = JOB_REGISTRY.get(key)
        if found is None:
            raise FramingError(f'모르는 작업입니다: {key}')
        return found


class PlateRevertJob(Job):
    """
    늘린 영상에서 보간 자리만 빼서 원래 길이로 되돌린다.

    쪽지에 "출력 3번은 원본 1번" 이 적혀 있으니 되돌리기는 계산이 아니라
    **고르기**다. 어느 프레임을 버릴지 다시 추정하지 않는다 — 추정하면 늘릴
    때와 뺄 때가 어긋나고, 그건 아무도 못 알아챈다.

    쪽지는 영상 옆 파일에서 찾고, 없으면 영상 안에 심어 둔 것에서 찾는다.
    """

    KEY = 'revert'
    ALIASES = ('unstretch', 'shrink-back')
    TITLE = '늘린 것 되돌리기'
    HINT = '늘림 메모를 보고 보간 프레임만 빼냄'
    NEEDS_FFMPEG = True
    NOTE = """
    늘릴 때 남긴 메모를 보고 보간으로 넣은 자리만 뺍니다.

      framing revert 컷_01_plate.mov                        옆의 메모를 알아서 찾습니다
      framing revert 컷_01.mov --note-file 컷_01_리타임메모.txt
      framing revert 컷_01.mov --note-file 늘림메모.json

    리타임 메모장(.txt)을 줘도 됩니다 — 거기 적힌 프레임 번호를 뺍니다.
    메모를 잃어버려도 영상 안에 심어 둔 것이 있으면 되돌아갑니다.
    넣은 영상의 프레임 수가 메모와 다르면 (자르거나 다른 영상이면)
    아무것도 만들지 않고 멈춥니다.

    메모가 아예 없으면 어디를 뺄지 직접 말할 수 있습니다.

      framing revert 컷.mov --drop tail:20        뒤에서 20장
      framing revert 컷.mov --drop head:20        앞에서 20장
      framing revert 컷.mov --drop 8,21,34        그 번호들 (1부터)
      framing revert 컷.mov --drop 240-265        구간

    메모와 영상이 맞아도 원본 프레임이 모두 남아 있어야 원본 길이로 복원됩니다."""

    def _given_note(self) -> Optional[Path]:
        """
        "메모 파일" 칸에 들어온 것. 경로여야 하는데 **메모의 내용을 붙여넣는**
        일이 실제로 있었다. 그러면 예전에는 그 긴 글을 파일 이름으로 알고
        찾다가 FileNotFoundError 로 죽었다 — 무엇이 잘못됐는지 알 수가 없다.
        """
        said = (self._opt.stretch_note or '').strip().strip('"').strip("'")
        if not said:
            return None
        spot = Path(said)
        if spot.is_file():
            return spot
        if len(said) > 200 or '\n' in said:
            raise FramingError('메모 파일 칸에는 파일을 넣어 주세요 — 메모의 내용이 아니라 '
                               '메모장 파일(.txt)이나 늘림메모(.json)를 끌어다 놓으면 됩니다.')
        raise FramingError(f'메모 파일을 찾지 못했습니다: {said}')

    def _note(self, target: Path, ffprobe: str) -> StretchNote:
        """
        뺄 자리를 누가 아는가 — 세 갈래다.

          1. 리타임 메모장(.txt) 을 직접 줬다          → 거기 적힌 번호
          2. 아무 말도 없다                            → 늘림메모(.json) 를 찾아본다
          3. "뒤에서 20장" 처럼 직접 말했다            → 그 자리 (차선책)
        """
        given = self._given_note()
        asked = DropSpec(self._opt.drop_frames)
        if given is not None and RetimeNoteText.handles(given):
            spec = self._plate(target, ffprobe)
            Console.info(f'메모장  {given.name}')
            return RetimeNoteText(given).note(spec, spec.frames)
        if not asked:
            return StretchNote.find(target, given, ffprobe)
        spec = self._plate(target, ffprobe)
        Console.warn('직접 지정한 자리를 뺍니다 — 늘릴 때 기록한 자리가 아닙니다')
        return asked.note(spec, spec.frames)

    def _plate(self, target: Path, ffprobe: str) -> PlateSpec:
        spec = PlateSpec.read(_probe_plate(ffprobe, target), target)
        if spec.frames < 2:
            raise FramingError('프레임 수를 읽지 못했습니다 — 뺄 자리를 셀 수가 없습니다.')
        return spec

    def run(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        ffmpeg = self._ff.ensure()
        ffprobe = self._ff.ensure_probe()
        note = self._note(target, ffprobe)
        if note.restoration_problem:
            raise FramingError(note.restoration_problem)
        Console.info(f'메모  {note.summary()}')
        revert = PlateRevert(target, note, ffmpeg, self._abort_if_asked)
        destination = self._output_file(target, '_원본길이', target.suffix or '.mov')
        workspace = Path(tempfile.mkdtemp(prefix='framing_revert_'))
        try:
            report = revert.run(destination, workspace)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        Console.info(f'뺀 자리  늘리면서 생긴 {report["removed"]}장')
        result.outputs = [destination]
        result.output_dir = destination.parent
        result.ok = True
        result.message = (f'{report["from"]} → {report["to"]}프레임 · '
                          f'늘린 자리 {report["removed"]}장 뺌  ·  {destination.name}')
        return result


class PlateHoldJob(Job):
    """
    끝(또는 앞) 프레임을 붙잡아 길이를 늘린다. 보간을 아예 안 쓴다.

    짧은 컷을 늘릴 때 늘 보간이 답인 건 아니다. 꼬리가 거의 멈춰 있거나
    타임라인 길이만 맞추면 되는 자리에서는, 끝 프레임을 붙잡는 편이 깨끗하다 —
    **새로 그리는 픽셀이 하나도 없어서** 플레이트가 100% 산다. 찢어질 일도 없다.

    대신 붙잡은 자리는 그냥 멈춘다. 움직임이 살아 있는 꼬리에 쓰면 툭 서 버린다.
    늘림 메모를 똑같이 남기므로 `framing revert` 로 그대로 되돌아간다.
    """

    KEY = 'hold'
    ALIASES = ('freeze', '홀드')
    TITLE = '끝 프레임 붙잡기'
    HINT = '보간 없이 앞뒤 프레임을 붙잡아 길이만 늘림'
    NEEDS_FFMPEG = True
    NOTE = """
      framing hold 컷_01.mov --frames 46            뒤를 붙잡습니다 (기본)
      framing hold 컷_01.mov --seconds 2 --hold-where head
      framing hold 컷_01.mov --frames 46 --hold-where both

    새로 그리는 픽셀이 없어서 플레이트가 그대로 남습니다. 대신 붙잡은 자리는
    멈춰 있으니, 꼬리가 움직이는 컷이면 RIFE 쪽(plate)을 쓰세요.
    늘림 메모를 남기므로 framing revert 로 되돌아갑니다."""

    def run(self, target: Path) -> JobResult:
        result = JobResult(target=target)
        ffmpeg = self._ff.ensure()
        spec = PlateSpec.read(_probe_plate(self._ff.ensure_probe(), target), target)
        if spec.frames < 1:
            raise FramingError('프레임을 읽지 못했습니다.')
        Console.info(f'플레이트  {spec.frames}프레임 · {spec.fps:g}fps · '
                     f'{spec.pix_fmt or "규격 안 적힘"}')
        wanted = _wanted_frames(self._opt, spec)
        plan = HoldPlan(spec.frames, wanted, self._opt.hold_where)
        Console.info(f'계획  {plan.summary()}')
        destination = self._output_file(target, '_hold', target.suffix or '.mov')
        workspace = Path(tempfile.mkdtemp(prefix='framing_hold_'))
        try:
            movie, note = HoldBuilder(target, spec, plan, ffmpeg,
                                      self._abort_if_asked).run(destination, workspace)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        Console.info(f'메모  {note.beside(movie).name} · {note.summary()}')
        Console.info(f'되돌리기  framing revert "{movie.name}"')
        result.outputs = [movie, note.beside(movie)]
        result.output_dir = movie.parent
        result.ok = True
        result.message = (f'{spec.frames} → {wanted}프레임 · '
                          f'{HoldPlan.LABEL[plan.where]} 홀드 {plan.extra}장  ·  {movie.name}')
        return result


def _probe_plate(ffprobe: str, target: Path) -> str:
    """
    플레이트 규격 한 덩어리. 늘리기와 붙잡기가 같은 눈으로 본다.

    받는 건 **ffprobe 경로**다. 예전에는 ffmpeg 경로를 받아 여기서 이름을
    바꿨는데, 윈도우처럼 폴더 이름에도 'ffmpeg' 이 들어가면 그것까지 바뀌어
    없는 경로가 됐다.
    """
    try:
        return subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0',
             '-count_frames', '-show_entries',
             'stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_read_frames,'
             'color_primaries,color_transfer,color_space,color_range',
             '-of', 'default=nw=1', str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    except OSError as exc:
        # 날것의 FileNotFoundError 가 그대로 올라가면 "메모 파일이 없나?" 하고
        # 엉뚱한 데를 보게 된다. 못 찾은 것이 무엇인지 이름을 적어 준다.
        raise FramingError(f'ffprobe 를 실행하지 못했습니다 ({ffprobe}) — {exc}')


def _wanted_frames(opt, spec: PlateSpec) -> int:
    """몇 장으로 늘릴지 — 프레임 · 초 · 배속 중 무엇으로 말했든 여기로 모은다."""
    if opt.target_frames > 0:
        return int(opt.target_frames)
    if opt.target_seconds > 0:
        return max(2, int(round(opt.target_seconds * spec.fps)))
    if opt.speed > 0:
        return max(2, int(round(spec.frames / opt.speed)))
    raise FramingError('몇 장으로 늘릴지 정해 주세요 (--frames · --seconds · --speed).')


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


JOB_CLASSES: tuple[type[Job], ...] = (
    ExtractJob, RetimeJob, ReverseJob, ConvertJob, CompareJob, RenameJob, AuditJob, ContactSheetJob, CutJob,
    PlateRifeJob, PlateRevertJob, PlateHoldJob)
JOB_REGISTRY: dict[str, type[Job]] = {}
for _cls in JOB_CLASSES:
    JOB_REGISTRY[_cls.KEY] = _cls
    for _alias in _cls.ALIASES:                 # 예전 이름도 계속 받아준다
        JOB_REGISTRY.setdefault(_alias, _cls)





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



class WorkPlan:
    """
    이 작업이 한 번에 무엇을 받는지 정한다 — 하나씩인가, 둘씩 짝지어서인가.

    예전에는 이 판단이 CLI 쪽에만 있었다. 그래서 창에서 Compare 를 돌리면
    파일 두 개를 하나씩 따로 넘겨 버렸고, 매번 "두 개가 필요합니다" 만 나왔다.
    같은 판단을 두 군데 두면 반드시 한쪽이 뒤처진다. 그래서 여기 하나만 둔다.
    """

    def __init__(self, job_class: type[Job], targets: Sequence[Path]) -> None:
        self._job_class = job_class
        self._targets = list(targets)

    @property
    def paired(self) -> bool:
        return bool(self._job_class.PAIRED)

    @property
    def grouped(self) -> bool:
        return bool(getattr(self._job_class, "GROUPED", False))

    @property
    def units(self) -> list[tuple[Path, ...]]:
        if self.grouped:
            return [tuple(self._targets)] if self._targets else []
        if not self.paired:
            return [(target,) for target in self._targets]
        return [(self._targets[i], self._targets[i + 1])
                for i in range(0, len(self._targets) - 1, 2)]

    @property
    def shortfall(self) -> str:
        """시작조차 못 하는 경우의 이유. 빈 문자열이면 갈 수 있다."""
        if self.paired and len(self._targets) < 2:
            return ("파일 두 개가 필요합니다. A 와 B 를 같이 넣어 주세요 "
                    "(둘을 한꺼번에 끌어다 놓거나, 폴더째 넣어도 됩니다).")
        return ""

    @property
    def leftover(self) -> Optional[Path]:
        """짝이 안 맞아 남는 것. 조용히 버리지 말고 알려줘야 한다."""
        if self.paired and len(self._targets) % 2:
            return self._targets[-1]
        return None

    @staticmethod
    def label(unit: Sequence[Path]) -> str:
        if len(unit) > 2:
            return f"{unit[0].name} 외 {len(unit) - 1}개"
        return "  ↔  ".join(path.name for path in unit)

    @staticmethod
    def run(job: Job, unit: Sequence[Path]) -> JobResult:
        if getattr(type(job), "GROUPED", False):
            return job.run_batch(list(unit))
        return job.run_pair(*unit) if len(unit) == 2 else job.run(unit[0])


class CommandLine:
    """
    터미널에 친 한 줄을 실행 가능한 요청으로 옮긴다.

    argv → 작업 이름 → 파싱된 인자 → Options → Application. 이 다섯 걸음이
    모듈에 흩어져 있으면 하나를 고칠 때 나머지 넷을 찾아다녀야 한다.
    여기 한 자리에 모아 둔다.
    """

    HELP_WORDS = ("help", "explain", "도움말", "?")
    # 예전 이름으로 부르면 목적지까지 정해진 걸로 친다. mp4 라고 쳤는데
    # "어디로 보낼까요" 를 다시 묻는 건 되묻기가 아니라 훼방이다.
    ALIAS = {"mp4": "mp4", "to-mp4": "mp4",
                     "mov": "mov", "to-mov": "mov",
                     "prores": "nuke", "nuke": "nuke",
                     "shrink": "small", "compress": "small", "small": "small"}

    def __init__(self, argv: Optional[Sequence[str]] = None) -> None:
        self._raw = list(argv) if argv is not None else sys.argv[1:]

    # --- 읽기 -------------------------------------------------------------

    @staticmethod
    def job_for(name: str) -> Optional[type[Job]]:
        return JOB_REGISTRY.get(name.strip().lower())

    @staticmethod
    def parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog=SELF.name if "SELF" in globals() else "framing",
            description="영상 작업 도구 — 뽑기 · 리타임 · 역재생 · 변환 · 대조. "
                        "인자 없이 열면 창이 뜹니다.",
            epilog="작업: " + " / ".join(c.KEY for c in JOB_CLASSES),
            formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("paths", nargs="*", help="영상 · 이미지 · 시퀀스 폴더")
        pl = parser.add_argument_group('plate (플레이트 늘리기 · RIFE)')
        pl.add_argument('--rife', dest='rife_program', default='',
                        help='RIFE 실행 파일 경로. 주면 가운데 단계까지 한 번에 갑니다')
        pl.add_argument('--no-snap-plate', dest='plate_no_snap', action='store_true',
                        help='원본 프레임을 잡으려고 0.25프레임 당기는 것을 끔')
        pl.add_argument('--hold-where', dest='hold_where',
                        choices=('tail', 'head', 'both'), default='tail',
                        help='hold: 어느 쪽 프레임을 붙잡을지 (기본 tail = 뒤)')
        pl.add_argument('--drop', dest='drop_frames', default='',
                        help='revert: 메모 없이 뺄 자리 (tail:20 · head:20 · 8,21,34 · 240-265)')
        pl.add_argument('--note-file', dest='stretch_note', default='',
                        help='revert: 메모 경로 (.txt 리타임메모 · .json 늘림메모). 비우면 영상 옆과 영상 안에서 찾습니다')
        dl = parser.add_argument_group("delivery (납품)")
        dl.add_argument('--shot', dest='delivery_shot', default='',
                        help='delivery: 샷코드. 비우면 원본 이름에서 물려받습니다 (A_0002C007_org_v001 → A_0002C007)')
        dl.add_argument('--task', dest='delivery_task', default='',
                        help='delivery: 테스크 이름 (기본 ai)')
        dl.add_argument('--version', dest='delivery_version', type=int, default=0,
                        help='delivery: 버전 번호. 비우면 안 쓰인 다음 번호로 갑니다')
        parser.add_argument('--freeze-seconds', type=float, default=0.5,
                            help='audit: 정지 후보 최소 길이(초), 0.1~10')
        ex = parser.add_argument_group("extract (뽑기)")
        ex.add_argument("--format", choices=("png", "jpg", "both", "exr", "dpx"),
                        default=None, help="출력 형식. exr · dpx 는 --at all 일 때")
        ex.add_argument("--at", default=None,
                        help="뽑을 지점: 0,50%%,1:23,last … / all 이면 전부")
        ex.add_argument("--count", type=int, default=0, help="균등하게 N장")
        ex.add_argument("--every", type=int, default=0, help="N프레임마다 한 장")
        ex.add_argument("--levels", choices=("auto", "keep", "full"), default=None,
                        help="밝기 범위: auto=틀린 표기만 바로잡음 (기본)")
        ex.add_argument("--jpeg-quality", type=int, default=95, help="JPEG 품질 1~100")
        rt = parser.add_argument_group("retime (리타임)")
        rt.add_argument("--frames", dest="target_frames", type=int, default=0,
                        help="목표 프레임 수")
        rt.add_argument("--seconds", dest="target_seconds", type=float, default=0.0,
                        help="목표 길이(초)")
        rt.add_argument("--speed", type=float, default=0.0,
                        help="배속. 0.5 = 절반 속도, 2 = 두 배 빠르게")
        rt.add_argument("--retime-mode", choices=("auto", "dup", "hold", "blend", "motion"),
                        default="auto", help="없는 프레임 채우는 법 (hold = 마지막 프레임 붙잡기)")
        rt.add_argument("--no-snap", action="store_true", help="매트 라벨색 복원 끄기")
        rt.add_argument("--note", dest="retime_note", action="store_true",
                        help="손댄 자리를 메모장(.txt)으로도 결과 옆에 남김")
        rt.add_argument("--result", dest="delivery",
                        choices=tuple(DELIVERY_BY_KEY), default="auto",
                        help="리타임 · 역재생 결과를 어떤 모습으로 (기본 auto = 영상 하나)")
        cv = parser.add_argument_group("convert (변환)")
        # 목적지를 여기 또 적어 두면 엔진에 하나 늘었을 때 명령줄만 모르는 상태가 된다.
        cv.add_argument("--to", dest="convert_to",
                        choices=tuple(t.KEY for t in CONVERT_TARGETS),
                        default=None, help="어디로 보낼지")
        cv.add_argument("--image-format", choices=("png", "jpg", "webp", "exr", "dpx"), default=None,
                        help="이미지를 어떤 형식으로")
        cv.add_argument("--prores-movie", action="store_true",
                        help="시퀀스를 누크용 ProRes 로 묶음")
        cv.add_argument("--level", choices=tuple(QUALITY_PRESETS), default=None,
                        help="용량 줄이기 강도")
        cv.add_argument("--h265", action="store_true", help="용량 줄이기에 HEVC 사용")
        cv.add_argument("--max-height", type=int, default=0, help="세로 해상도 상한")
        cv.add_argument("--force-encode", action="store_true",
                        help="상자만 바꿔도 될 때도 다시 인코딩")
        cv.add_argument('--gif-fps', type=float, default=12)
        cv.add_argument('--gif-width', type=int, default=640)
        cv.add_argument('--gif-once', action='store_true', help='GIF를 한 번만 재생')
        cuts = parser.add_argument_group('cuts (컷 분할)')
        cuts.add_argument('--cut-mode', choices=('auto', 'interval', 'manual'), default='auto')
        cuts.add_argument('--cut-threshold', type=float, default=10)
        cuts.add_argument('--cut-min-seconds', type=float, default=0.5)
        cuts.add_argument('--cut-interval', type=float, default=5)
        cuts.add_argument('--cut-times', default='', help='예: 3.5,00:07.2')
        rn = parser.add_argument_group("rename (이름 바꾸기)")
        rn.add_argument("--pattern", dest="rename_pattern", default=None,
                        help="새 이름 패턴. {n} 자리에 번호 (기본 장면_{n})")
        rn.add_argument("--start", dest="rename_start", type=int, default=1,
                        help="시작 번호")
        rn.add_argument("--docker", dest="rename_docker", action="store_true",
                        help="조용한_수달 처럼 무작위 두 단어 이름으로")
        rn.add_argument("--name-lang", dest="rename_lang", choices=("kr", "en"),
                        default="kr", help="도커식 단어: kr=한글 두 단어, en=도커 원본")
        rn.add_argument("--find", dest="rename_find", default="",
                        help="이름에서 찾을 말 (쉼표로 여러 개)")
        rn.add_argument("--replace", dest="rename_replace", default="",
                        help="찾은 자리에 넣을 말. 비우면 지웁니다")
        rn.add_argument("--prefix", dest="rename_prefix", default="",
                        help="앞에 붙일 말 (bg · ch · plate …)")
        rn.add_argument("--suffix", dest="rename_suffix", default="",
                        help="뒤에 붙일 말")
        rn.add_argument("--cut-head", dest="rename_cut_head", type=int, default=0,
                        help="앞에서 잘라낼 글자 수")
        rn.add_argument("--cut-tail", dest="rename_cut_tail", type=int, default=0,
                        help="뒤에서 잘라낼 글자 수")
        cp = parser.add_argument_group("compare (대조)")
        cp.add_argument("--view", choices=("wipe", "overlay", "flicker", "side"),
                        default=None, help="어느 보기로 열지")
        cp.add_argument("--keep", dest="keep_compare", action="store_true",
                        help="결과를 원본 옆에 파일로 남김")
        parser.add_argument("--out", type=Path, default=None, help="결과를 모아둘 폴더")
        parser.add_argument("--fps", type=float, default=24, help="시퀀스를 묶을 때 프레임레이트")
        parser.add_argument("--no-mp4", action="store_true", help="확인용 mp4 를 만들지 않음")
        parser.add_argument("--overwrite", action="store_true", help="같은 이름이 있으면 덮어씀")
        parser.add_argument("--top-only", action="store_true", help="하위 폴더는 훑지 않음")
        parser.add_argument("--no-download", action="store_true", help="ffmpeg 자동 내려받기 끔")
        parser.add_argument("--no-pause", action="store_true", help="끝나도 엔터 대기 없음")
        return parser

    @property
    def wants_help(self) -> bool:
        return bool(self._raw) and self._raw[0].strip().lower() in self.HELP_WORDS

    @classmethod
    def split(cls, argv: list[str]) -> tuple[Optional[str], list[str]]:
        """첫 인자가 작업 이름이면 떼어낸다. 별칭도 받는다."""
        if argv and cls.job_for(argv[0]) is not None:
            return argv[0].strip().lower(), argv[1:]
        return None, argv

    # --- 옮기기 -----------------------------------------------------------

    @staticmethod
    def _rename_mode(args: argparse.Namespace) -> str:
        """무엇을 적었는지 보고 방식을 고른다 — 방식 이름까지 외우게 하지 않는다."""
        if args.rename_docker:
            return "docker"
        if args.rename_find:
            return "find" if args.rename_replace else "strip"
        if args.rename_prefix or args.rename_suffix or args.rename_cut_head or args.rename_cut_tail:
            return "affix"
        return "pattern"

    @classmethod
    def options(cls, args: argparse.Namespace) -> Options:
        return Options(
            frame_format=args.format or "both",
            jpeg_quality=args.jpeg_quality,
            at=args.at or "",
            count=args.count,
            every=args.every,
            levels=args.levels or "auto",
            out_dir=args.out,
            fps=args.fps,
            make_mp4=not args.no_mp4,
            h265=args.h265,
            convert_to=args.convert_to or "mp4",
            image_format=args.image_format or "png",
            prores_movie=args.prores_movie,
            gif_fps=args.gif_fps, gif_width=args.gif_width, gif_loop=not args.gif_once,
            cut_mode=args.cut_mode, cut_threshold=args.cut_threshold,
            cut_min_seconds=args.cut_min_seconds, cut_interval=args.cut_interval, cut_times=args.cut_times,
            view=args.view or "wipe",
            audit_freeze_seconds=args.freeze_seconds,
            rife_program=args.rife_program,
            plate_no_snap=args.plate_no_snap,
            stretch_note=args.stretch_note,
            drop_frames=args.drop_frames,
            hold_where=args.hold_where,
            delivery_shot=args.delivery_shot,
            delivery_task=args.delivery_task,
            delivery_version=args.delivery_version,
            rename_mode=cls._rename_mode(args),
            rename_pattern=args.rename_pattern or "장면_{n}",
            rename_start=args.rename_start,
            rename_lang=args.rename_lang,
            rename_find=args.rename_find,
            rename_replace=args.rename_replace,
            rename_prefix=args.rename_prefix,
            rename_suffix=args.rename_suffix,
            rename_cut_head=args.rename_cut_head,
            rename_cut_tail=args.rename_cut_tail,
            keep_compare=args.keep_compare,
            target_frames=args.target_frames,
            target_seconds=args.target_seconds,
            speed=args.speed,
            retime_mode=args.retime_mode or "auto",
            no_snap=args.no_snap,
            retime_note=args.retime_note,
            delivery=args.delivery or "auto",
            level=args.level or "medium",
            max_height=args.max_height,
            force_encode=args.force_encode,
            overwrite=args.overwrite,
            recursive=not args.top_only,
        )

    @classmethod
    def apply_alias(cls, name: str, options: Options) -> None:
        where = cls.ALIAS.get((name or "").strip().lower())
        if where:
            options.convert_to = where

    # --- 돌리기 -----------------------------------------------------------

    SHORTCUT_WORDS = ("shortcut", "바로가기")

    def run(self) -> int:
        raw = self._raw
        # "help" 라고 쳤는데 "찾을 수 없습니다: help" 가 나오면 안 된다.
        if self.wants_help:
            self.parser().print_help()
            return 0
        if raw and raw[0].lower() in self.SHORTCUT_WORDS:
            return DesktopShortcut().make()
        job_key, rest = self.split(raw)
        args = self.parser().parse_args(rest)
        options = self.options(args)
        if job_key:
            self.apply_alias(job_key, options)
        ff = FFmpegTool(allow_download=not args.no_download)

        app = Application(options, ff)
        try:
            code = app.run(job_key, args.paths)
        except KeyboardInterrupt:
            print("\n  중단했습니다. 이미 끝난 파일은 남아 있습니다.")
            code = 130

        if not args.no_pause and is_interactive():
            try:
                input("\n  엔터를 누르면 종료합니다...")
            except EOFError:
                pass
        return code




# 예전 이름으로 부르면 목적지까지 정해진 걸로 친다. mp4 라고 쳤는데
# "어디로 보낼까요" 를 다시 묻는 건 되묻기가 아니라 훼방이다.






# ══════════════════════════════════════════════════ 대상 수집

class TargetCollector:
    """입력 경로를 작업이 받을 수 있는 대상 목록으로 펼친다."""

    OUTPUT_MARKS = ("_conv", "_small", "_nuke", "_reversed", "_retimed",
                    "_keyframes", "_sequence", "_compare")

    def __init__(self, job: type[Job], recursive: bool = True) -> None:
        self._job = job
        self._recursive = recursive

    def collect(self, inputs: Iterable[str | Path]) -> list[Path]:
        named: list[Path] = []
        scanned: list[Path] = []
        for raw in inputs:
            path = normalize_path(raw)
            if path.is_file():
                if self._takes(path):
                    named.append(path)
                else:
                    Console.warn(f"다룰 수 없는 파일이라 건너뜁니다: {path.name}")
            elif path.is_dir():
                if self._job.ACCEPTS_FOLDER and collect_images(path):
                    named.append(path)          # 시퀀스 폴더 자체가 대상
                else:
                    scanned += self._scan(path)
            else:
                Console.warn(f"찾을 수 없습니다: {path}")
        merged = self._dedupe(named) + [p for p in self._dedupe(scanned)
                                        if p not in named]
        return merged

    def _scan(self, folder: Path) -> list[Path]:
        pattern = "**/*" if self._recursive else "*"
        found = sorted(p for p in folder.glob(pattern)
                       if p.is_file() and self._takes(p))
        fresh = [p for p in found if not self._looks_like_output(p)]
        skipped = len(found) - len(fresh)
        if skipped:
            Console.info(f"이전 결과물로 보이는 {skipped}개는 건드리지 않았습니다")
        return fresh

    def _takes(self, path: Path) -> bool:
        suffix = path.suffix.lower()
        if self._job.KEY == "rename": return True
        if suffix in VIDEO_EXTENSIONS:
            return True
        return bool(self._job.ACCEPTS_IMAGE and suffix in IMAGE_EXTENSIONS)

    @classmethod
    def _looks_like_output(cls, path: Path) -> bool:
        return path.stem.endswith(cls.OUTPUT_MARKS)

    @staticmethod
    def _dedupe(paths: Sequence[Path]) -> list[Path]:
        seen: set[Path] = set()
        unique: list[Path] = []
        for path in paths:
            key = path.resolve()
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique


# ══════════════════════════════════════════════════ 실행

class Application:
    def __init__(self, options: Options, ff: FFmpegTool) -> None:
        self._opt = options
        self._ff = ff
        # 앱에서 중단 버튼을 누르면 이걸 갈아끼운다. CLI 에서는 늘 False.
        self.should_stop: Callable[[], bool] = lambda: False

    def _abort_if_asked(self) -> None:
        if self.should_stop():
            raise FramingError('중단했습니다.')

    def run(self, job_key: Optional[str], inputs: Sequence[str]) -> int:
        Console.banner("Framing — 영상 작업 도구 모음")

        raw_inputs = list(inputs)
        if not raw_inputs:
            Console.warn("처리할 대상이 없습니다.")
            Console.info(f"{SELF.name} keyframes 영상.mp4   처럼 쓰거나,")
            Console.info("그냥 더블클릭하면 창이 뜹니다.")
            return 1

        if job_key is None:
            job_key = "extract"            # 아이콘에 끌어다 놓으면 창이 뜨니, 여기 오면 CLI 다

        job_class = JOB_REGISTRY[job_key]  # split_job 이 검증해 둔 키
        job = job_class(self._opt, self._ff)

        targets = TargetCollector(job_class, self._opt.recursive).collect(raw_inputs)
        if not targets:
            Console.warn("처리할 대상을 찾지 못했습니다.")
            return 1

        if job_class.NEEDS_FFMPEG:
            try:
                self._ff.ensure()
            except FramingError as exc:
                Console.warn(str(exc))
                return 1

        plan = WorkPlan(job_class, targets)
        if plan.shortfall:
            Console.warn(plan.shortfall)
            return 1
        if plan.leftover is not None:
            Console.warn(f"{plan.leftover.name} 은 짝이 없어 건너뜁니다")

        units = plan.units
        Console.banner(f"{job_class.TITLE}  ·  {len(units)}"
                       f"{'쌍' if plan.paired else '개'}")
        results = [self._run_one(job, unit, i, len(units))
                   for i, unit in enumerate(units, 1)]
        return self._report(results)

    def _run_one(self, job: Job, unit: Sequence[Path], order: int, total: int) -> JobResult:
        target = unit[0]
        Console.section(f"[{order}/{total}] {WorkPlan.label(unit)}")
        try:
            result = WorkPlan.run(job, unit)
        except FramingError as exc:
            Console.fail(str(exc))
            return JobResult(target=target, ok=False, message=str(exc))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            Console.fail(message)
            return JobResult(target=target, ok=False, message=message)

        if result.message:
            Console.ok(result.message)
        if result.output_dir:
            Console.info(f"→ {result.output_dir}")
        return result

    def _report(self, results: Sequence[JobResult]) -> int:
        failed = [r for r in results if not r.ok]
        Console.banner(f"완료 — 성공 {len(results) - len(failed)}개 / 실패 {len(failed)}개")
        for result in failed:
            Console.fail(f"{result.target.name}: {result.message or '결과 없음'}")

        total_in = sum(r.size_in for r in results if r.ok and r.size_in)
        total_out = sum(r.size_out for r in results if r.ok and r.size_out)
        if total_in and total_out:
            delta = (1 - total_out / total_in) * 100
            verb = "줄었습니다" if delta >= 0 else "늘었습니다"
            Console.info(f"합계 용량  {human(total_in)} → {human(total_out)}"
                         f"  ({abs(delta):.1f}% {verb})")
        Console.info("원본은 건드리지 않았습니다.")

        first = next((r.output_dir for r in results if r.ok and r.output_dir), None)
        if first and is_interactive():
            self._open_folder(first)
        return 0 if not failed else 1

    @staticmethod
    def _open_folder(folder: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))       # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)], check=False)
        except Exception:
            pass


# ══════════════════════════════════════════════════ CLI














# ══════════════════════════════════════════════════ 5. 창구

# ══════════════════════════════════════════════════ 바깥 세상과의 접점

class Reveal:
    """
    폴더 하나를 파일 탐색기에 띄우는 방법. 운영체제마다 방법이 다르니
    하위 클래스가 자기 방법만 안다. 고르는 건 pick() 이 한다.

    방법은 여러 개를 순서대로 들고 있다가 되는 것이 나올 때까지 시도한다.
    """

    QUIET = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}

    @staticmethod
    def pick() -> "Reveal":
        if sys.platform.startswith('win'):
            return WindowsReveal()
        if sys.platform == 'darwin':
            return MacReveal()
        return LinuxReveal()

    def plans(self, folder: Path, highlight: Optional[Path]) -> list:
        raise NotImplementedError


class WindowsReveal(Reveal):
    """
    윈도우 탐색기. 여기 함정이 하나 있었다.

    subprocess 에 목록으로 주면 파이썬이 띄어쓰기 있는 인자를 통째로 따옴표로
    감싼다. 그러면 explorer.exe 가 받는 건 `"/select,C:\\내 폴더\\a.mp4"` 라서
    /select 를 스위치로 못 알아보고, 경로도 아니니 그냥 기본 폴더(바탕화면 ·
    내 문서)를 연다. "폴더 열기를 눌렀는데 바탕화면이 뜬다" 가 이것이었다.

    그래서 윈도우에서는 명령줄을 문자열로 직접 만들어 넘긴다. 문자열로 주면
    파이썬이 손대지 않고 그대로 전달한다. 경로 구분자도 역슬래시로 맞춘다 —
    탐색기는 슬래시로 된 경로에서 /select 를 무시한다.
    """

    @staticmethod
    def _win_path(path: Path) -> str:
        return os.path.normpath(str(path)).replace('/', '\\')

    def plans(self, folder: Path, highlight: Optional[Path]) -> list:
        here = self._win_path(folder)
        made = []
        if highlight is not None:
            picked = self._win_path(highlight)
            made.append(lambda: subprocess.Popen(
                f'explorer.exe /select,"{picked}"', **self.QUIET))
        made.append(lambda: subprocess.Popen(
            f'explorer.exe "{here}"', **self.QUIET))
        made.append(lambda: os.startfile(here))    # type: ignore[attr-defined]
        return made


class MacReveal(Reveal):
    """파인더. -R 은 파일을 짚어서 보여준다."""

    def plans(self, folder: Path, highlight: Optional[Path]) -> list:
        made = []
        if highlight is not None:
            made.append(lambda: subprocess.run(['open', '-R', str(highlight)],
                                               check=True, **self.QUIET))
        made.append(lambda: subprocess.run(['open', str(folder)],
                                           check=True, **self.QUIET))
        return made


class LinuxReveal(Reveal):
    """리눅스는 파일 관리자가 제각각이라 흔한 것부터 차례로 본다."""

    TRIES = ('xdg-open', 'nautilus', 'thunar', 'dolphin', 'nemo')

    def plans(self, folder: Path, highlight: Optional[Path]) -> list:
        return [(lambda name=name: subprocess.run([name, str(folder)],
                                                  check=True, **self.QUIET))
                for name in self.TRIES]


class FolderOpener:
    """
    결과 폴더를 파일 탐색기로 연다. 그것만 한다.

    두 가지가 중요하다.
      · 창이 다른 창 뒤로 숨지 않고 맨 앞으로 나와야 한다
      · explorer.exe 는 성공해도 0 이 아닌 값을 돌려준다 — 종료코드를 믿으면 안 된다
    """

    def __init__(self, path: str, select: str = "",
                 how: Optional[Reveal] = None) -> None:
        self._raw = path
        self._select = select
        self._how = how or Reveal.pick()

    def open(self) -> dict:
        folder = self._folder()
        if folder is None:
            return {'ok': False, 'path': str(self._raw),
                    'error': f'그런 경로가 없습니다: {self._raw}'}
        target = self._highlight(folder)
        troubles = []
        for attempt in self._how.plans(folder, target):
            try:
                attempt()
                return {'ok': True, 'path': str(folder)}
            except Exception as exc:
                troubles.append(f'{type(exc).__name__}: {exc}')
                continue
        why = ' / '.join(troubles[:2]) if troubles else '방법이 하나도 없습니다'
        return {'ok': False, 'path': str(folder),
                'error': f'폴더를 열지 못했습니다 ({why}). 경로를 직접 열어 주세요.'}

    def _folder(self) -> Optional[Path]:
        if not self._raw:
            return None
        target = Path(self._raw).expanduser()
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        if not target.exists():
            return None
        return target if target.is_dir() else target.parent

    def _highlight(self, folder: Path) -> Optional[Path]:
        """골라서 보여줄 파일 하나. 있으면 탐색기가 확실히 앞으로 나온다."""
        if self._select:
            picked = Path(self._select).expanduser()
            if not picked.is_absolute():
                picked = (Path.cwd() / picked).resolve()
            if picked.exists():
                return picked
        try:
            for child in sorted(folder.iterdir()):
                if child.is_file():
                    return child
        except OSError:
            pass
        return None


class DropTarget:
    """
    창 위로 파일을 끌어다 놓으면 진짜 경로를 받아 화면에 넘긴다.

    브라우저는 끌어다 놓은 파일의 경로를 자바스크립트에 알려주지 않는다.
    pywebview 가 파이썬 쪽 이벤트에 pywebviewFullPath 를 얹어 주므로 여기서 받는다.

    반드시 webview.start() 가 창을 띄운 뒤에 걸어야 한다.
    창이 만들어지기 전에는 window.dom 이 아직 없어서 조용히 실패한다 —
    예전에 끌어다 놓기가 안 먹던 이유가 이것이었다.
    """

    def __init__(self, api: "Api") -> None:
        self._api = api

    def attach(self, window) -> bool:
        try:
            from webview.dom import DOMEventHandler
        except Exception:
            return False
        try:
            events = window.dom.document.events
            # dragstart 는 건드리지 않는다. 여기에 preventDefault 를 걸면
            # 표 안에서 줄을 끌어 순서를 바꾸는 것까지 같이 죽는다 —
            # 창에서만 끌기가 안 먹던 이유가 이것이었다. 우리가 막아야 할 것은
            # "창 밖에서 들어온 파일을 웹뷰가 그냥 열어 버리는 것" 뿐이다.
            events.dragenter += DOMEventHandler(self._swallow, True, False)
            events.dragover += DOMEventHandler(self._swallow, True, False, debounce=500)
            events.drop += DOMEventHandler(self._on_drop, True, False)
            return True
        except Exception:
            return False

    @staticmethod
    def _swallow(_event) -> None:
        """기본 동작(파일을 창에 그냥 띄우기)을 막기만 한다."""

    def _on_drop(self, event) -> None:
        paths = self._paths_in(event)
        if paths:
            self._api.dropped(paths)
        else:
            self._api.notify('warn', '끌어다 놓은 파일의 경로를 읽지 못했습니다. '
                                     '왼쪽 "경로 직접 붙여넣기" 를 쓰세요.')

    @staticmethod
    def _paths_in(event) -> list:
        try:
            files = (event or {}).get('dataTransfer', {}).get('files') or []
        except Exception:
            return []
        found = []
        for item in files:
            if not isinstance(item, dict):
                continue
            full = item.get('pywebviewFullPath') or item.get('path')
            if full:
                found.append(str(full))
        return found


# 창으로 자바스크립트를 직접 밀어넣던 WindowChannel 은 없앴다.
#
# WKWebView 의 evaluateJavaScript 는 **메인 스레드에서만** 부를 수 있다.
# 그런데 작업은 일꾼 스레드에서 돌고, 기록 한 줄마다 화면으로 밀어넣었다.
# 윈도우(EdgeChromium)는 알아서 넘겨 줘서 멀쩡했는데, 맥에서는 그 자리에서
# 프로세스가 죽는다 — "Python 응용 프로그램이 예기치 않게 종료되었습니다".
#
# 지금은 창 모드도 화면을 로컬 서버에서 받아 오니, 이벤트도 같은 길로 보낸다.
# 파이썬은 큐에 넣기만 하고 화면이 가져간다. 스레드를 넘나드는 호출이 없다.


# ══════════════════════════════════════════════════ 엔진 다리



# ══════════════════════════════════════════════════ 화면이 부르는 함수들

class Api:
    """pywebview 가 화면에 노출하는 함수 묶음. 여기가 앱의 바깥 경계다."""

    def __init__(self) -> None:
        self._window = None
        self._emitter: Optional[Callable[[str, dict], None]] = None
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ff = FFmpegTool(allow_download=True)
        self._served = False                     # 화면을 로컬 서버에서 받아 오는가
        self._trouble: Optional[Callable[[], str]] = None
        self._stops: list[Callable[[], None]] = []   # 끝낼 때 차례로 부를 것들
        self._startup_paths: list[str] = []
        self._tickets: dict[str, Path] = {}      # 창 안 뷰어에 내준 파일 표
        self._pages: dict[str, str] = {}         # 서버가 내줄 대조 화면
        self._last_compare: dict = {}            # 지금 보고 있는 대조 짝 (내보내기용)
        self._drop_ready = False
        self._rename_history = RenameHistory()
        self._operations = OperationGate()

    def set_drop_ready(self, ready: bool) -> None:
        """창이 뜬 뒤 실제로 끌어다 놓기를 걸 수 있었는지 화면에 알려준다."""
        self._drop_ready = bool(ready)
        self._emit('capability', {'canDrop': self._drop_ready})

    def carry(self, paths: Sequence[str]) -> None:
        """Framing 위에 끌어다 놓고 시작한 경우, 그 파일들을 화면에 넘겨준다."""
        self._startup_paths = [str(p) for p in paths]

    def notify(self, kind: str, text: str) -> None:
        self._emit('log', {'kind': kind, 'text': text})

    def dropped(self, paths: Sequence[str]) -> None:
        """창 위로 끌어다 놓았을 때. 진짜 경로를 화면으로 보낸다."""
        cleaned = [str(p) for p in paths if p]
        if cleaned:
            self._emit('dropped', {'paths': cleaned})

    def bind(self, window) -> None:
        """
        창 모드. **창만** 잡는다 — 파일 고르기 창, 끌어다 놓기, 닫기에 쓴다.

        이벤트는 여기로 보내지 않는다. 화면으로 밀어넣는 일은 메인 스레드에서만
        할 수 있는데 작업은 일꾼 스레드에서 돌기 때문이다. 맥에서는 그걸 어기면
        경고가 아니라 프로세스가 죽는다. 이벤트는 서버 큐로 간다.
        """
        self._window = window
        # 종료를 누르면 창이 실제로 닫혀야 한다. 예전에는 여기를 안 걸어 둬서
        # "종료했습니다" 안내만 남고 빈 창이 그대로 떠 있었다.
        self._stops.append(self._close_window)

    def _close_window(self) -> None:
        try:
            self._window.destroy()
        except Exception:
            pass

    def bind_server(self, stop: Callable[[], None]) -> None:
        """화면을 로컬 서버에서 내준다 — 창 모드에서도 그렇다.

        미디어 주소가 여기에 걸린다. 자세한 이유는 _media_url 에 적어 두었다.
        """
        self._served = True
        self._stops.append(stop)

    def bind_emitter(self, emitter: Callable[[str, dict], None],
                     trouble: Optional[Callable[[], str]] = None) -> None:
        """이벤트를 큐에 쌓아 두고 화면이 가져가게 한다. 창 모드도 이 길이다."""
        self._emitter = emitter
        self._trouble = trouble

    # --- 화면으로 밀어넣기 ---------------------------------------------

    @property
    def trouble(self) -> str:
        """화면으로 보내다 마지막으로 걸린 것. 없으면 빈 글자."""
        return self._trouble() if self._trouble is not None else ''

    def _emit(self, kind: str, payload: dict) -> bool:
        """화면까지 갔으면 True. 대조처럼 '갔는지' 가 중요한 것도 있다."""
        if self._emitter is None:
            return False
        try:
            return self._emitter(kind, payload) is not False
        except Exception:
            return False

    # --- 화면이 부르는 것들 ---------------------------------------------

    def describe(self) -> dict:
        """작업 목록과 설명. 화면은 이걸 받아서 메뉴를 그린다."""
        jobs = []
        for cls in JOB_CLASSES:
            jobs.append({
                'key': cls.KEY,
                'title': cls.TITLE,
                'hint': cls.HINT,
                'note': (cls.NOTE or '').strip('\n'),
                'acceptsFolder': cls.ACCEPTS_FOLDER,
                'acceptsImage': getattr(cls, 'ACCEPTS_IMAGE', False),
                'needsFfmpeg': cls.NEEDS_FFMPEG,
                'paired': cls.PAIRED,
            })
        return {
            'jobs': jobs,
            # 결과를 어떤 모습으로 받을지 — 화면이 이 목록으로 단추를 만든다
            'deliveries': [{'key': cls.KEY, 'title': cls.TITLE, 'hint': cls.HINT}
                           for cls in DELIVERY_CHOICES],
            'ffmpeg': self._ff.find('ffmpeg') or '',
            'python': sys.version.split()[0],
            'build': BUILD,
            'startupPaths': self._startup_paths,
            'canDrop': self._drop_ready,
            'trouble': self.trouble,          # 창으로 못 밀어넣은 것이 있었나
            'lang': self.prefs().get('lang', ''),   # 지난번에 고른 화면 말
        }

    def _accepts(self, job_key: str = '') -> tuple[set, str]:
        """무엇이든 받는다 — 파일이 먼저 오고, 무엇을 할지는 그다음에 정하는 화면이다."""
        return (set(VIDEO_EXTENSIONS) | set(IMAGE_EXTENSIONS),
                '영상 · 이미지')

    def pick_files(self, job_key: str = '') -> list:
        wanted, label = self._accepts(job_key)
        language = self.prefs().get('lang', 'en')
        label = MessageCatalog().text(label, language)
        exts = ' '.join('*' + e for e in sorted(wanted))
        if self._window is not None:
            try:
                import webview
                return list(self._window.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=True,
                    file_types=(f'{label} ({exts})', MessageCatalog().text('모든 파일 (*.*)', language))) or [])
            except Exception:
                pass
        return self._tk_dialog('files')

    def pick_folder(self) -> list:
        if self._window is not None:
            try:
                import webview
                return list(self._window.create_file_dialog(webview.FOLDER_DIALOG) or [])
            except Exception:
                pass
        return self._tk_dialog('folder')

    @staticmethod
    def _tk_dialog(kind: str) -> list:
        """창이 없을 때(브라우저 모드) 쓰는 기본 파일 대화상자."""
        try:
            import tkinter
            from tkinter import filedialog
        except ImportError:
            return []
        root = tkinter.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        try:
            if kind == 'folder':
                picked = filedialog.askdirectory()
                return [picked] if picked else []
            picked = filedialog.askopenfilenames()
            return list(picked or [])
        finally:
            root.destroy()

    def add_paths(self, text: str) -> dict:
        """직접 붙여넣은 경로. 대화상자가 막힌 환경의 탈출구."""
        found = []
        for line in (text or '').splitlines():
            cleaned = line.strip().strip('"').strip("'")
            if not cleaned:
                continue
            path = Path(cleaned).expanduser()
            found.append(str(path))
        return {'paths': found}

    def inspect(self, paths: list, job_key: str = '') -> dict:
        """고른 것들이 무엇인지 훑어본다. 실행 전에 화면에 보여주려고."""
        wanted, _label = self._accepts(job_key)
        videos, sequences, images, missing = [], [], [], []
        for raw in paths:
            path = Path(raw)
            if not path.exists():
                missing.append(str(path))
                continue
            if path.is_dir():
                if job_key == 'rename':
                    for child in sorted(p for p in path.rglob('*') if p.is_file()):
                        images.append({'path': str(child), 'name': child.name, 'dir': str(child.parent),
                                       'size': child.stat().st_size, 'other': True})
                    continue
                inside = collect_images(path)
                inner = [p for p in path.rglob('*')
                         if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
                if inside:
                    sequences.append({'path': str(path), 'name': path.name,
                                      'dir': str(path.parent), 'frames': len(inside)})
                if inner:
                    videos += [self._video_facts(p) for p in sorted(inner)[:200]]
                continue
            suffix = path.suffix.lower()
            if suffix in VIDEO_EXTENSIONS:
                videos.append(self._video_facts(path))
            elif suffix in wanted:
                images.append(self._image_facts(path))
            else:
                images.append({'path': str(path), 'name': path.name, 'dir': str(path.parent),
                               'size': path.stat().st_size, 'other': True})
        return {'videos': videos, 'sequences': sequences,
                'images': images, 'missing': missing}

    def _image_facts(self, path: Path) -> dict:
        facts = {'path': str(path), 'name': path.name, 'dir': str(path.parent),
                 'size': path.stat().st_size, 'width': 0, 'height': 0}
        info = self._ff.probe(path) if self._ff.find('ffprobe') else None
        if info:
            facts.update({'width': info.width, 'height': info.height, 'color': info.color_metadata})
        return facts

    def _video_facts(self, path: Path) -> dict:
        facts = {'path': str(path), 'name': path.name, 'dir': str(path.parent),
                 'size': path.stat().st_size,
                 'width': 0, 'height': 0, 'fps': 0.0, 'duration': 0.0, 'frames': 0,
                 'vcodec': '', 'acodec': '', 'alpha': False}
        info = self._ff.probe(path) if self._ff.find('ffprobe') else None
        if info:
            # frames 는 리타임 화면이 "늘리는 중인가 줄이는 중인가" 를 판단하는 값이다.
            facts.update({'width': info.width, 'height': info.height, 'fps': round(info.fps, 3), 'fpsExact': info.fps,
                          'duration': round(info.duration, 3), 'frames': int(info.frames or 0),
                          'vcodec': info.vcodec,
                          'acodec': info.acodec, 'alpha': info.alpha, 'color': info.color_metadata})
        return facts

    def delivery_name(self, path: str, options: dict) -> dict:
        """
        납품 파일 이름을 미리 보여준다 — 만들지는 않는다.

        규칙은 엔진(DeliverySlate)에만 둡니다. 화면이 똑같은 규칙을 따로 들고
        있으면 둘이 갈라지고, 그때 화면에 보인 이름과 실제로 나온 파일이
        달라집니다. 그게 제일 알아채기 어려운 사고입니다.
        """
        target = Path(path) if path else None
        if target is None:
            return {'ok': False, 'error': '먼저 영상을 고르세요.'}
        opt = UiOptions(options).fill(Options())
        try:
            slate = DeliverySlate.decide(target.stem, opt.delivery_shot,
                                         opt.delivery_task, opt.delivery_version)
        except FramingError as exc:
            return {'ok': False, 'error': str(exc)}
        folder = opt.out_dir or (target.parent if target.exists() else None)
        spot = (slate.free_in(folder, '.mov', overwrite=opt.overwrite)
                if folder and folder.is_dir() else None)
        return {'ok': True, 'name': spot.name if spot else slate.name('.mov'),
                'shot': slate.shot, 'task': slate.task, 'version': slate.version}

    def resolve_picks(self, path: str, options: dict) -> dict:
        """실행 전에 '어디를 뽑는지' 미리 보여주려고 계산만 해 본다."""
        target = Path(path) if path else None
        if target is None or not target.exists():
            return {'ok': False, 'error': '먼저 영상을 고르세요.'}

        info = self._ff.probe(target) if self._ff.find('ffprobe') else None
        fps = (info.fps if info and info.fps else 0.0) or float(options.get('fps') or 24)
        total = info.frames if info and info.frames else 0
        if total <= 0:
            try:
                with VideoSource(target) as source:
                    total, fps = source.frame_count, (source.fps or fps)
            except Exception as exc:
                return {'ok': False, 'error': str(exc)}

        spec = PickSpec(options.get('at') or '',
                               options.get('every') or 0,
                               options.get('count') or 0)
        warnings: list[str] = []
        try:
            with EngineBridge(lambda kind, data: warnings.append(data['text']) if data.get('kind') == 'warn' else None):
                picks = spec.resolve(total, fps)
        except FramingError as exc:
            return {'ok': False, 'error': str(exc)}

        return {
            'ok': True, 'total': total, 'fps': round(fps, 3),
            'warnings': warnings,
            'picks': [{'label': p.label, 'index': p.index,
                       'time': round(p.index / fps, 3) if fps else 0.0} for p in picks],
        }

    def run(self, job_key: str, paths: list, options: dict) -> dict:
        if not self._operations.claim():
            return {'ok': False, 'error': '이미 실행 중입니다.'}
        self._stop.clear()
        try:
            self._worker = threading.Thread(target=self._run_reserved,
                args=(job_key, paths, options or {}), daemon=True)
            self._worker.start()
        except Exception:
            self._operations.release()
            raise
        return {'ok': True}

    def _run_reserved(self, job_key, paths, options):
        completion = {'ok': 0, 'fail': 1, 'cancelled': False, 'dirs': []}
        try:
            completion = self._run_blocking(job_key, paths, options, emit_done=False)
        finally:
            self._operations.release()
            self._emit('done', completion)

    def cancel(self) -> dict:
        self._stop.set()
        killed = self._ff.abort()          # 돌고 있는 ffmpeg 이 있으면 즉시 끊는다
        return {'ok': True, 'killedFfmpeg': killed}

    # --- 이름에 자주 쓰는 말들 (bg · ch · plate …) ------------------------
    #     사람마다 쓰는 말이 다르니 고를 수 있게 두고, 고른 것은 남긴다.

    KEYWORD_FILE = Path.home() / '.framing' / 'keywords.json'
    PREFS_FILE = Path.home() / '.framing' / 'prefs.json'

    def prefs(self) -> dict:
        """다음에 열어도 그대로여야 하는 것들 (지금은 언어 하나)."""
        try:
            saved = json.loads(self.PREFS_FILE.read_text(encoding='utf-8'))
            if isinstance(saved, dict):
                return saved
        except Exception:
            pass
        return {}

    def prefs_save(self, data: dict) -> dict:
        """통째로 덮어쓰지 않고 얹는다 — 화면은 자기가 아는 것만 보내면 된다."""
        merged = self.prefs()
        for key, value in (data or {}).items():
            merged[str(key)[:32]] = value
        try:
            self.PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.PREFS_FILE.write_text(
                json.dumps(merged, ensure_ascii=False), encoding='utf-8')
            return {'ok': True, 'prefs': merged}
        except OSError as exc:
            return {'ok': False, 'error': str(exc), 'prefs': merged}
    KEYWORD_DEFAULT = ['bg', 'ch', 'plate', 'fx', 'comp', 'ref', 'mask', 'matte',
                       'cam', 'lut', 'final', 'wip', 'v01']

    def keywords(self) -> dict:
        """등록해 둔 말들. 없으면 흔히 쓰는 것으로 시작한다."""
        try:
            saved = json.loads(self.KEYWORD_FILE.read_text(encoding='utf-8'))
            if isinstance(saved, list) and saved:
                return {'words': [str(w) for w in saved][:60]}
        except Exception:
            pass
        return {'words': list(self.KEYWORD_DEFAULT)}

    def keywords_save(self, words: list) -> dict:
        """말 목록을 통째로 저장. 화면에서 더하고 빼면 그때마다 부른다."""
        clean, seen = [], set()
        for raw in (words or []):
            word = str(raw).strip()
            if word and word.lower() not in seen:
                seen.add(word.lower())
                clean.append(word[:24])
        try:
            self.KEYWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.KEYWORD_FILE.write_text(
                json.dumps(clean[:60], ensure_ascii=False), encoding='utf-8')
            return {'ok': True, 'words': clean[:60]}
        except OSError as exc:
            return {'ok': False, 'error': str(exc), 'words': clean[:60]}

    def propose_name(self, path: str, lang: str = 'kr') -> dict:
        """
        도커식 이름 하나를 제안만 한다. 디스크는 건드리지 않는다.

        주사위를 누르자마자 파일 이름이 바뀌어 버리면 되돌릴 방법이 없다.
        먼저 보여주고, 사람이 확인을 누를 때 바꾼다.
        """
        target = Path(path)
        try:
            stem = NamePlan.one_name(lang or 'kr', target.parent,
                                            target.suffix.lower())
            return {'ok': True, 'name': stem + target.suffix.lower(), 'stem': stem}
        except FramingError as exc:
            return {'ok': False, 'error': str(exc)}

    def rename_preview(self, paths: list, rules: list) -> dict:
        try:
            if not isinstance(paths, list) or not isinstance(rules, list):
                raise FramingError('이름 변경 목록이 올바르지 않습니다.')
            rows = BatchNamePlan([Path(p) for p in paths], rules).preview()
            return {'ok': True, 'rows': rows}
        except (FramingError, OSError, TypeError, ValueError) as exc:
            return {'ok': False, 'error': str(exc)}

    def rename_apply(self, rows: list) -> dict:
        try:
            if not isinstance(rows, list) or not rows: raise FramingError('바꿀 이름이 없습니다.')
            pairs = [(Path(row['path']), Path(row['destination'])) for row in rows]
            expected = {row['path']: row.get('stamp') for row in rows} if any('stamp' in row for row in rows) else None
            with self._operations.exclusive():
                renamed = self._rename_history.apply(pairs, expected)
            return {'ok': True, 'renamed': renamed}
        except (FramingError, OSError, TypeError, ValueError, KeyError) as exc:
            return {'ok': False, 'error': str(exc)}

    def rename_undo(self) -> dict:
        try:
            with self._operations.exclusive():
                return {'ok': True, 'renamed': self._rename_history.undo()}
        except (FramingError, OSError) as exc:
            return {'ok': False, 'error': str(exc)}

    def project_save(self, data: dict) -> dict:
        try:
            document = ProjectDocument.dumps(data)
            if self._window is not None:
                import webview
                choice = self._window.create_file_dialog(webview.SAVE_DIALOG,
                    save_filename='Framing.framing.json', file_types=('Framing (*.framing.json)',))
                if not choice: return {'ok': True, 'cancelled': True}
                destination = Path(choice[0] if isinstance(choice, (tuple, list)) else choice)
                if not destination.name.endswith('.framing.json'):
                    destination = destination.with_name(destination.name + '.framing.json')
                temporary = destination.with_name('.' + destination.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_text(document, encoding='utf-8')
                    temporary.replace(destination)
                finally:
                    if temporary.exists(): temporary.unlink()
                return {'ok': True, 'path': str(destination)}
            return {'ok': True, 'document': document, 'filename': 'Framing.framing.json'}
        except (FramingError, OSError, TypeError, ValueError) as exc:
            return {'ok': False, 'error': str(exc)}

    def project_import(self, text: str) -> dict:
        try:
            document = ProjectDocument.loads(text)
            return {'ok': True, 'project': document,
                    'missing': [p for p in document['paths'] if not Path(p).exists()]}
        except (FramingError, OSError) as exc:
            return {'ok': False, 'error': str(exc)}

    def preview_media(self, path: str) -> dict:
        try:
            target = Path(path).absolute()
            if not target.is_file(): raise FramingError('미리 볼 파일을 찾을 수 없습니다.')
            video = target.suffix.lower() in VIDEO_EXTENSIONS
            if not video and target.suffix.lower() not in IMAGE_EXTENSIONS | {'.gif'}:
                raise FramingError('영상이나 이미지 파일을 고르세요.')
            info = self._ff.probe(target) if self._ff.find('ffprobe') else None
            if PlayableProxy.why(target, info, video):
                with self._operations.exclusive():
                    self._stop.clear()
                    folder = Scratch.folder('preview')
                    proxy = PlayableProxy(self._ff, folder, cancelled=self._stop.is_set).make(target, info, video, 'preview')
                if proxy is None: raise FramingError('보기용 파일을 만들지 못했습니다.')
                target = proxy
            return {'ok': True, 'url': self._media_url(str(target)), 'video': video,
                    'info': {'width': info.width, 'height': info.height, 'fps': info.fps if video else 0,
                             'duration': info.duration if video else 0, 'vcodec': info.vcodec, 'acodec': info.acodec,
                             'color': info.color_metadata} if info else {}}
        except (FramingError, OSError) as exc:
            return {'ok': False, 'error': str(exc)}


    def rename_one(self, path: str, wanted: str = '', lang: str = 'kr') -> dict:
        """목록의 파일 하나만 그 자리에서. wanted 가 비면 도커식 무작위 이름."""
        try:
            with self._operations.exclusive():
                new = NamePlan.rename_single(Path(path), wanted or '', lang or 'kr')
            return {'ok': True, 'old': path, 'path': str(new), 'name': new.name}
        except FramingError as exc:
            return {'ok': False, 'error': str(exc)}
        except OSError as exc:
            return {'ok': False, 'error': f'바꾸지 못했습니다: {exc}'}

    # --- 창 안에서 대조 보여주기 -----------------------------------------

    def _media_url(self, path: str) -> str:
        """
        이 화면이 읽을 수 있는 주소로 바꾼다.

        서버가 있으면 무조건 서버로 흘려보낸다 — 창 모드에서도.

        예전에는 창 모드일 때 file:// 주소를 그대로 넘겼다. 화면이 file:// 이니
        읽히겠거니 했는데, 대조 화면은 iframe 의 srcdoc 안에서 산다. srcdoc 문서는
        출처가 비어 있어서(about:srcdoc) 크로미움·WebView2 가 그 안에서 file:// 을
        읽는 것을 막는다. 그래서 창은 떠도 영상 자리가 까맣게 남았다.
        그때 <video> 가 남기는 것은 error.code 4 — "이 src 는 못 연다" 다.

        경로를 그대로 주소에 싣지 않고 표를 끊어 주는 이유는, 열어 준 적 없는 파일을
        주소만 바꿔 넣어 읽어 가지 못하게 하려는 것이다.
        """
        real = Path(path)
        if not self._served and self._window is not None:
            return real.as_uri()          # 서버 없이 띄운 창 (옛 길)
        token = uuid.uuid4().hex[:16]
        self._tickets[token] = real
        return f'/media/{token}{real.suffix.lower()}'

    def ticket(self, token: str) -> Optional[Path]:
        return self._tickets.get(token)

    def page(self, token: str) -> Optional[str]:
        """서버가 내줄 대조 화면. 없으면 None."""
        return self._pages.get(token)

    def _park(self, html: str) -> str:
        """
        대조 화면을 서버에 세워 두고 주소를 돌려준다.

        예전에는 이 15KB 짜리 글을 통째로 화면으로 밀어 넣어 srcdoc 에 붙였다.
        두 가지가 나빴다. 창 모드에서는 그 큰 글이 evaluate_js 한 줄에 실려
        가는데 도중에 조용히 흘리면 아무 일도 안 일어난 것처럼 보였고,
        srcdoc 문서는 출처가 없어서 그 안에서 영상 파일을 읽지 못했다.
        주소 하나만 보내면 둘 다 없어진다 — 화면은 그냥 그 주소를 연다.

        지난 것은 몇 개만 남긴다. 창을 닫을 때까지 계속 쌓일 이유가 없다.
        """
        token = uuid.uuid4().hex[:16]
        self._pages[token] = html
        for old in list(self._pages)[:-8]:
            self._pages.pop(old, None)
        return f'/compare/{token}.html'

    def show_compare(self, html: str, facts: dict) -> bool:
        """엔진이 만든 뷰어를 창 안으로 넘긴다. 넘겼으면 True."""
        if self._emitter is None:
            return False
        try:
            page = (html.replace('__A_SRC__', self._media_url(facts['a']))
                        .replace('__B_SRC__', self._media_url(facts['b'])))
            language = 'kr' if self.prefs().get('lang') == 'kr' else 'en'
            page = page.replace('/* @viewer-language */ \"kr\"', json.dumps(language))
        except Exception:
            return False
        # 지금 보고 있는 짝을 기억해 둔다 — "mp4 로 저장" 은 이걸로 굽는다.
        # 굽는 재료는 보기용 사본이 아니라 원본이어야 한다.
        self._last_compare = {
            'a': facts.get('sourceA') or facts.get('a', ''),
            'b': facts.get('sourceB') or facts.get('b', ''),
            'width': facts.get('width', 0), 'height': facts.get('height', 0),
            'seconds': facts.get('seconds', 0.0), 'video': bool(facts.get('video')),
            'fps': facts.get('fps', 24.0),
        }
        # 화면까지 갔는지 확인하고 대답한다. 못 갔는데 갔다고 하면 아무것도
        # 안 보이는 채로 끝난다 — 그럴 땐 파일로 만들어 여는 길이라도 남겨야 한다.
        return self._emit('viewer', {
            'url': self._park(page), 'title': facts.get('title', '대조'),
            'note': facts.get('note', ''),
            'canExport': bool(self._last_compare['a']),
            'exportKind': 'mp4' if facts.get('video') else 'png'})

    def export_compare(self, view: str = 'wipe', state: Optional[dict] = None,
                       options: Optional[dict] = None) -> dict:
        if not self._operations.claim():
            return {'ok': False, 'error': '이미 실행 중입니다.'}
        try:
            last = dict(getattr(self, '_last_compare', None) or {})
            if not last.get('a'):
                raise FramingError('먼저 대조를 실행하세요.')
            if not all(Path(last[key]).is_file() for key in ('a', 'b')):
                raise FramingError('원본 파일을 찾지 못했습니다.')
            snapshot = dict(state or {})
            snapshot['mode'] = snapshot.get('mode') or view
            self._stop.clear()
            exporter = CompareExporter(self._ff, last, snapshot, options,
                                       cancelled=self._stop.is_set,
                                       progress=lambda data: self._emit('compare-export-progress', data))
            self._emit('log', {'kind': 'head', 'text': '대조 내보내기 — 원본에서 저장 중'})
            folder, files, manifest = exporter.save()
            self._emit('result', {'name': folder.name, 'message': f'대조 저장 · {len(files)}개',
                                  'dir': str(folder), 'files': [str(p) for p in files[:12]],
                                  'fileCount': len(files), 'nextFiles': []})
            return {'ok': True, 'path': str(files[0]), 'dir': str(folder),
                    'name': folder.name, 'fileCount': len(files), 'manifest': str(manifest)}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}
        finally:
            self._operations.release()

    def quit(self) -> dict:
        """브라우저 모드에는 닫을 창이 없으니 화면에서 끌 수 있게 한다."""
        self._stop.set()
        self._ff.abort()
        Scratch.clear()      # 대조하려고 만든 임시 파일을 두고 가지 않는다
        if self._stops:
            threading.Timer(0.3, self._closeAll).start()
        return {'ok': True}

    def _closeAll(self) -> None:
        """걸어 둔 것을 나중에 건 것부터 닫는다 — 창 먼저, 서버 나중."""
        for stop in reversed(self._stops):
            try:
                stop()
            except Exception:
                pass

    def checkup(self) -> dict:
        """지금 상태 한 장. 화면이 자기 것을 붙여 완성한다."""
        return Checkup(self).facts()

    def save_report(self, text: str) -> dict:
        """화면이 만든 진단서를 파일로. 그 파일만 보내면 된다."""
        spot = Path.home() / '.framing' / '진단.txt'
        try:
            spot.parent.mkdir(parents=True, exist_ok=True)
            spot.write_text(str(text or ''), encoding='utf-8')
        except Exception as exc:
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        return {'ok': True, 'path': str(spot)}

    def reveal(self, path: str, select: str = "") -> dict:
        """결과 폴더를 탐색기 · 파인더로 연다. 안 되면 왜 안 되는지 돌려준다."""
        return FolderOpener(path, select).open()

    # --- 실제 작업 -------------------------------------------------------

    def _build_options(self, raw: dict):
        options = UiOptions(raw).fill(Options())
        options.language = 'kr' if self.prefs().get('lang') == 'kr' else 'en'
        return options

    def _run_blocking(self, job_key: str, paths: list, raw_options: dict, emit_done=True) -> dict:
        started = {'ok': 0, 'fail': 0, 'dirs': []}
        try:
            options = self._build_options(raw_options)
            job_class = UiJobSelection.resolve(job_key, options)
            targets = TargetCollector(job_class, options.recursive).collect(paths)
            if not targets:
                raise FramingError('처리할 대상을 찾지 못했습니다.')

            # 하나씩 돌릴지 둘씩 짝지어 돌릴지는 엔진이 정한다.
            # 여기서 따로 판단하면 창과 명령줄이 어긋난다 — 실제로 그래서 Compare 가 안 돌았다.
            plan = WorkPlan(job_class, targets)
            if plan.shortfall:
                raise FramingError(plan.shortfall)
            if plan.leftover is not None:
                self._emit('log', {'kind': 'warn',
                                   'text': f'{plan.leftover.name} 은 짝이 없어 건너뜁니다'})
            units = plan.units

            if job_class.NEEDS_FFMPEG:
                self._ff.ensure()

            self._emit('start', {'job': job_class.TITLE, 'count': len(units),
                                 'unit': '쌍' if plan.paired else '개'})
            job = job_class(options, self._ff)
            job.should_stop = self._stop.is_set        # 프레임 루프에서도 중단이 먹게
            job.show_inline = self.show_compare        # 결과를 창 안에서 보여준다

            with EngineBridge(self._emit):
                for order, unit in enumerate(units, 1):
                    target = unit[0]
                    if self._stop.is_set():
                        self._emit('log', {'kind': 'warn', 'text': '중단했습니다.'})
                        break
                    self._emit('item', {'order': order, 'total': len(units),
                                        'name': WorkPlan.label(unit)})
                    try:
                        result = WorkPlan.run(job, unit)
                    except FramingError as exc:
                        if self._stop.is_set():
                            self._emit('log', {'kind': 'warn',
                                               'text': f'{WorkPlan.label(unit)}: 중단했습니다.'})
                            break
                        started['fail'] += 1
                        self._emit('log', {'kind': 'err', 'text': f'{WorkPlan.label(unit)}: {exc}'})
                        continue
                    except Exception as exc:
                        if self._stop.is_set():
                            self._emit('log', {'kind': 'warn',
                                               'text': f'{WorkPlan.label(unit)}: 중단했습니다.'})
                            break
                        started['fail'] += 1
                        self._emit('log', {'kind': 'err',
                                           'text': f'{WorkPlan.label(unit)}: {type(exc).__name__}: {exc}'})
                        continue
                    if result.ok:
                        started['ok'] += 1
                        if result.output_dir:
                            started['dirs'].append(str(result.output_dir))
                        self._emit('result', result.as_event(WorkPlan.label(unit)))
                    else:
                        started['fail'] += 1
                        self._emit('log', {'kind': 'err',
                                           'text': f'{WorkPlan.label(unit)}: {result.message or "결과 없음"}'})
        except Exception as exc:
            started['fail'] += 1
            self._emit('log', {'kind': 'err', 'text': str(exc)})
            if os.environ.get('FRAMING_DEBUG'):
                self._emit('log', {'kind': 'err', 'text': traceback.format_exc()})
        finally:
            completion = {'ok': started['ok'], 'fail': started['fail'],
                          'cancelled': self._stop.is_set(), 'dirs': sorted(set(started['dirs']))}
            if emit_done: self._emit('done', completion)
        return completion



# ══════════════════════════════════════════════════ 브라우저 모드

class EventBus:
    """엔진이 뱉는 이벤트를 쌓아 두고, 화면이 번호를 들고 와서 가져간다."""

    def __init__(self, keep: int = 4000) -> None:
        self._events: list[dict] = []
        self._keep = keep
        self._lock = threading.Lock()
        self.last_error = ""

    def push(self, kind: str, payload: dict) -> bool:
        body = self._plain(kind, payload)
        with self._lock:
            self._events.append({'seq': len(self._events), 'kind': kind,
                                 'payload': body})
            if len(self._events) > self._keep * 2:
                # 오래된 건 버리되 번호는 유지되도록 앞을 자른 만큼 표시해 둔다
                self._events = self._events[-self._keep:]
        return True                    # 큐에 넣었으면 화면이 반드시 가져간다

    def trouble(self) -> str:
        return self.last_error

    def _plain(self, kind: str, payload: dict) -> dict:
        """
        화면까지 갈 수 있는 모양으로 미리 바꿔 둔다.

        내보낼 때 바꾸면 늦다. Path 하나가 섞여 있으면 그 응답 전체가 터지고,
        그 판에 쌓인 다른 소식까지 같이 못 간다. 여기서 걸러 두면 못 옮기는
        값 하나만 글자가 되고 나머지는 멀쩡히 간다.
        """
        try:
            return json.loads(json.dumps(payload, ensure_ascii=False,
                                         default=str))
        except Exception as exc:
            self.last_error = f"{kind}: {type(exc).__name__}: {exc}"
        kept = {}
        for key, value in (payload or {}).items():
            try:
                kept[key] = json.loads(json.dumps(value, ensure_ascii=False,
                                                  default=str))
            except Exception:
                kept[key] = repr(value)[:200]
        return kept

    @property
    def head(self) -> int:
        with self._lock:
            return (self._events[-1]['seq'] + 1) if self._events else 0

    def since(self, after: int) -> tuple[list, int]:
        """after 가 음수면 지난 것은 건너뛰고 지금 위치만 알려준다.

        새로고침했을 때 예전 결과가 되살아나던 원인이 여기 있었다.
        """
        if after < 0:
            return [], self.head
        with self._lock:
            fresh = [e for e in self._events if e['seq'] >= after]
            nxt = (self._events[-1]['seq'] + 1) if self._events else after
            return fresh, nxt


MEDIA_TYPES = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
    '.mkv': 'video/x-matroska', '.webm': 'video/webm', '.avi': 'video/x-msvideo',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
    '.tif': 'image/tiff', '.tiff': 'image/tiff',
}


class LocalRequestPolicy:
    """Validate browser origin and a per-launch secret at the HTTP boundary."""

    MAX_BODY = 4 * 1024 * 1024

    def __init__(self):
        import secrets
        self.token = secrets.token_urlsafe(32)
        self.origin = ''

    def allows(self, headers, authenticated=False):
        import secrets
        from urllib.parse import urlsplit
        if headers.get('Host') != urlsplit(self.origin).netloc:
            return False
        origin = headers.get('Origin')
        if origin and origin != self.origin:
            return False
        if headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
            return False
        return not authenticated or secrets.compare_digest(
            headers.get('X-Framing-Token', ''), self.token)

    def body_length(self, headers):
        if headers.get('Transfer-Encoding'):
            raise FramingError('청크 요청은 지원하지 않습니다')
        try:
            length = int(headers.get('Content-Length', '0'))
        except (ValueError, TypeError):
            raise FramingError('잘못된 요청 길이입니다')
        if not 0 < length <= self.MAX_BODY:
            raise FramingError('요청 크기가 허용 범위를 벗어났습니다')
        return length

    def document(self, ui):
        injection = '<script>window.__framingToken=' + json.dumps(self.token) + ';</script>'
        document = ui.as_bytes()
        marker = b'<script'
        if marker not in document:
            raise FramingError('UI document has no script')
        return document.replace(marker, injection.encode('utf-8') + marker, 1)


class ApiDispatcher:
    """Only explicitly exported UI actions may cross the HTTP boundary."""

    METHODS = frozenset(('describe', 'inspect', 'pick_files', 'pick_folder',
        'resolve_picks', 'delivery_name', 'run', 'cancel', 'reveal', 'add_paths',
        'rename_one', 'propose_name', 'keywords', 'keywords_save',
        'export_compare', 'prefs', 'prefs_save', 'quit', 'checkup', 'save_report',
        'rename_preview', 'rename_apply', 'rename_undo', 'project_save',
        'project_import', 'preview_media'))

    def __init__(self, api):
        self._api = api

    def invoke(self, payload):
        import inspect
        if not isinstance(payload, dict):
            raise FramingError('요청은 객체여야 합니다')
        name, args = payload.get('method'), payload.get('args', [])
        if not isinstance(name, str) or name not in self.METHODS:
            raise FramingError('허용되지 않은 기능입니다')
        if not isinstance(args, list):
            raise FramingError('인자는 목록이어야 합니다')
        method = getattr(self._api, name)
        try:
            inspect.signature(method).bind(*args)
        except TypeError:
            raise FramingError('기능에 맞지 않는 인자입니다')
        return method(*args)


class HttpByteRange:
    """Parse one RFC-style byte range, including suffix and open-ended ranges."""

    @staticmethod
    def parse(value, total):
        if not value:
            return 0, total - 1, False
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
        if not match or total <= 0:
            raise ValueError('invalid range')
        first, last = match.groups()
        if not first:
            if not last or int(last) <= 0:
                raise ValueError('invalid suffix')
            return max(0, total - int(last)), total - 1, True
        start = int(first)
        end = min(int(last), total - 1) if last else total - 1
        if start >= total or end < start:
            raise ValueError('unsatisfiable range')
        return start, end, True


class LocalServer:
    """
    창을 못 띄우는 환경을 위한 대비책. 127.0.0.1 에만 열고, 기본 브라우저로 띄운다.
    화면은 창 모드와 같은 framing_ui.html 이고, 파이썬도 같은 Api 를 쓴다.
    """

    def __init__(self, api: "Api", ui: "UiDocument") -> None:
        self._api = api
        self._ui = ui
        self._bus = EventBus()
        api.bind_emitter(self._bus.push, self._bus.trouble)
        api.bind_server(self.stop)
        self._httpd = None
        self.url = ''
        self._done = threading.Event()

    def start(self) -> str:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        api, bus, ui = self._api, self._bus, self._ui
        policy, dispatcher = LocalRequestPolicy(), ApiDispatcher(api)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):        # 콘솔을 조용히
                pass

            def _send(self, code: int, body: bytes, mime: str) -> None:
                self.send_response(code)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def _json(self, payload: dict, code: int = 200) -> None:
                self._send(code, json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                           'application/json; charset=utf-8')

            def do_GET(self):
                if not policy.allows(self.headers, self.path.startswith('/events')):
                    self.close_connection = True
                    self._json({'error': 'forbidden'}, 403)
                    return
                if self.path in ('/', '/index.html'):
                    self._send(200, policy.document(ui), 'text/html; charset=utf-8')
                    return
                if self.path.startswith('/media/'):
                    self._media(self.path[len('/media/'):])
                    return
                if self.path.startswith('/compare/'):
                    token = self.path[len('/compare/'):].split('.')[0]
                    html = api.page(token)
                    if html is None:
                        self._json({'error': 'not found'}, 404)
                        return
                    self._send(200, html.encode('utf-8'),
                               'text/html; charset=utf-8')
                    return
                if self.path.startswith('/events'):
                    from urllib.parse import urlparse, parse_qs
                    query = parse_qs(urlparse(self.path).query)
                    try:
                        after = int((query.get('after') or ['0'])[0])
                    except ValueError:
                        self._json({'error': 'invalid event cursor'}, 400)
                        return
                    events, nxt = bus.since(after)
                    self._json({'events': events, 'next': nxt})
                    return
                self._json({'error': 'not found'}, 404)

            def _media(self, name: str) -> None:
                """
                창 안 뷰어에 파일을 흘려보낸다. 표를 끊어 준 것만 내준다.

                Range 를 받아 줘야 영상 재생 막대를 끌 수 있다. 통째로만 주면
                브라우저가 탐색을 포기하고, 긴 영상은 아예 재생이 안 되기도 한다.
                """
                token = name.split('.')[0]
                real = api.ticket(token)
                if real is None or not real.is_file():
                    self._json({'error': 'not found'}, 404)
                    return
                mime = MEDIA_TYPES.get(real.suffix.lower(), 'application/octet-stream')
                total = real.stat().st_size
                try:
                    start, end, partial = HttpByteRange.parse(self.headers.get('Range', ''), total)
                except ValueError:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{total}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                if partial:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
                self.end_headers()
                try:
                    with open(real, 'rb') as handle:
                        handle.seek(start)
                        left = length
                        while left > 0:
                            chunk = handle.read(min(262144, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass                      # 보는 사람이 창을 닫았을 뿐이다

            def do_POST(self):
                if self.path != '/call':
                    self._json({'ok': False, 'error': 'not found'}, 404)
                    return
                if not policy.allows(self.headers, authenticated=True):
                    self.close_connection = True
                    self._json({'ok': False, 'error': 'forbidden'}, 403)
                    return
                if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    self.close_connection = True
                    self._json({'ok': False, 'error': 'JSON required'}, 415)
                    return
                try:
                    length = policy.body_length(self.headers)
                    self.connection.settimeout(15)
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise FramingError('요청이 완성되지 않았습니다')
                    body = json.loads(raw)
                    value = dispatcher.invoke(body)
                    self._json({'ok': True, 'value': value})
                except (FramingError, ValueError, TypeError, TimeoutError) as exc:
                    self.close_connection = True
                    self._json({'ok': False, 'error': str(exc)}, 400)
                except Exception as exc:
                    self._json({'ok': False, 'error': f'{type(exc).__name__}: {exc}'})

        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        port = self._httpd.server_address[1]
        self.url = f'http://127.0.0.1:{port}/'
        policy.origin = self.url.rstrip('/')
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self.url

    def open_browser(self) -> None:
        import webbrowser
        webbrowser.open(self.url)

    def stop(self) -> None:
        self._done.set()
        if self._httpd is not None:
            threading.Thread(target=self._httpd.shutdown, daemon=True).start()

    def wait(self) -> None:
        print(f"\nFraming 이 브라우저에서 열렸습니다: {self.url}")
        print("끝내려면 화면 오른쪽 위의 '종료' 를 누르거나 이 창에서 Ctrl+C.")
        try:
            while not self._done.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        print("종료합니다.")



class Checkup:
    """
    지금 무엇이 어떻게 돌고 있는지 한 장으로 모은다.

    "오류가 나요" 만 가지고는 아무것도 알 수 없다. 어느 판인지, 파이썬은 뭔지,
    ffmpeg 은 있는지, 창으로 열렸는지 브라우저로 열렸는지 — 물어보면 서로 피곤하다.
    화면에서 한 번 누르면 이게 모여 나오게 한다.

    파이썬이 아는 것만 모은다. 화면이 아는 것(무슨 파일을 넣었는지, 기록,
    자바스크립트 오류)은 화면 쪽 Doctor 가 붙여서 한 장으로 만든다.
    """

    def __init__(self, api: "Api") -> None:
        self._api = api

    def facts(self) -> dict:
        return {
            '때': f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
            '판': self._machine(),
            '파이썬': f"{sys.version.split()[0]}  ({sys.executable})",
            '열린 방식': self._how(),
            'ffmpeg': self._ffmpeg(),
            '설정 폴더': str(Path.home() / '.framing'),
            '임시 자리': f"{Scratch.holding()}개",
            '못 보낸 것': self._api.trouble or '없음',
        }

    @staticmethod
    def _machine() -> str:
        try:
            import platform
            return f"{platform.platform()}  ({platform.machine()})"
        except Exception:
            return sys.platform

    def _how(self) -> str:
        where = '창' if self._api._window is not None else '브라우저'
        return f"{where}  ·  화면은 {'서버' if self._api._served else '파일'} 에서"

    def _ffmpeg(self) -> str:
        spot = self._api._ff.find('ffmpeg') or ''
        if not spot:
            return '없음 (처음 쓸 때 받아 옵니다)'
        try:
            first = subprocess.run([spot, '-version'], capture_output=True,
                                   text=True, timeout=10).stdout.splitlines()
            return f"{spot}\n              {first[0] if first else ''}"
        except Exception:
            return spot


class CrashNote:
    """
    시작하다 넘어졌을 때 남기는 쪽지.

    창 모드에는 열어 볼 콘솔이 없고, 윈도우에서 배치로 열면 넘어지는 순간
    검은 창이 그대로 닫힌다. 쓰는 사람 눈에는 "눌렀는데 아무 일도 안 일어남"
    으로만 보인다 — 물어봐도 답할 말이 없다.

    그래서 무슨 일이 있었는지 파일 하나로 남긴다. 그 파일만 보내 주면 된다.
    """

    SPOT = Path.home() / '.framing' / '오류.txt'

    @classmethod
    def write(cls, exc: BaseException) -> Optional[Path]:
        try:
            cls.SPOT.parent.mkdir(parents=True, exist_ok=True)
            cls.SPOT.write_text(
                "Framing 이 시작하다 멈췄습니다.\n"
                "이 파일을 그대로 보내 주시면 원인을 찾을 수 있습니다.\n\n"
                f"때  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"판  {sys.platform}  ·  python {sys.version.split()[0]}\n"
                f"곳  {sys.argv[0]}\n\n"
                + "".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)),
                encoding='utf-8')
            return cls.SPOT
        except Exception:
            return None

# ══════════════════════════════════════════════════ 창


# ══════════════════════════════════════════════════ 첫 실행 준비

class SetupStep(ABC):
    """처음 쓸 때 챙겨야 하는 것 하나. 각자 자기가 준비됐는지, 어떻게 챙기는지 안다."""

    def __init__(self, title: str, size_hint: str = "") -> None:
        self.title = title
        self.size_hint = size_hint

    @property
    def label(self) -> str:
        return f"{self.title} ({self.size_hint})" if self.size_hint else self.title

    @abstractmethod
    def is_ready(self) -> bool: ...

    @abstractmethod
    def install(self) -> bool: ...

    @property
    def optional(self) -> bool:
        """없어도 프로그램은 뜬다. 그 기능만 못 쓴다."""
        return False


class PipStep(SetupStep):
    """pip 로 챙기는 파이썬 꾸러미."""

    def __init__(self, title: str, module: str, packages: Sequence[str],
                 size_hint: str = "", optional: bool = False) -> None:
        super().__init__(title, size_hint)
        self._module = module
        self._packages = tuple(packages)
        self._optional = optional

    @property
    def optional(self) -> bool:
        return self._optional

    def is_ready(self) -> bool:
        try:
            importlib.invalidate_caches()
            importlib.import_module(self._module)
            return True
        except ImportError:
            return False

    def install(self) -> bool:
        # 마법사가 이미 무엇을 챙기는지 말했으니 안내는 겹치지 않게 끈다.
        return DependencyBootstrap(self._module, self._packages, announce=False).try_ensure()


class FfmpegStep(SetupStep):
    """영상 인코더. 없으면 찾아보고, 없으면 내려받는다."""

    HINTS = {"win32": "약 80MB, 알아서 내려받습니다",
             "darwin": "Homebrew 로 설치합니다"}

    def __init__(self, tool: "FFmpegTool") -> None:
        # 어떻게 챙기는지가 운영체제마다 다르다. 무엇을 기다려야 하는지 미리 말해 준다.
        super().__init__("인코더 ffmpeg",
                         self.HINTS.get(sys.platform, "패키지 관리자로 설치합니다"))
        self._tool = tool

    def is_ready(self) -> bool:
        return bool(self._tool.find("ffmpeg") and self._tool.find("ffprobe"))

    def install(self) -> bool:
        try:
            self._tool.ensure()
            return self.is_ready()
        except Exception:
            return False

    @property
    def optional(self) -> bool:
        return True          # 없어도 프레임 뽑기는 된다


class SetupWizard:
    """
    처음 쓰는 사람이 아무것도 안 해도 되게, 필요한 것을 순서대로 챙긴다.
    무엇을 하고 있는지 계속 보여준다 — 멈춘 것처럼 보이는 게 제일 나쁘다.
    """

    MARKER = Path.home() / ".framing" / "ready.txt"
    WIDTH = 60

    def __init__(self, steps: Sequence[SetupStep]) -> None:
        self._steps = list(steps)

    # --- public ----------------------------------------------------------

    def ensure(self) -> None:
        first_time = not self.MARKER.exists()
        missing = [s for s in self._steps if not s.is_ready()]
        if not missing:
            self._remember()
            return

        # 한 번 겪어 본 사람에게는 조용히. 꼭 필요한 게 빠졌을 때만 다시 나선다.
        if not first_time:
            missing = [s for s in missing if not s.optional]
            if not missing:
                return

        self._banner(len(missing))
        failed = []
        for order, step in enumerate(missing, 1):
            self._line(order, len(missing), step)
            if step.install():
                self._done("완료")
            elif step.optional:
                self._done("건너뜀 — 나중에 필요할 때 다시 시도합니다")
            else:
                self._done("실패")
                failed.append(step)

        if failed:
            self._trouble(failed)
        else:
            print("\n  준비 끝. 창을 띄웁니다.\n")
            self._remember()

    # --- internal --------------------------------------------------------

    def _banner(self, count: int) -> None:
        bar = "─" * self.WIDTH
        print(f"\n{bar}\n  Framing — 처음 실행 준비\n{bar}")
        print(f"  필요한 것 {count}가지를 알아서 챙깁니다. 처음 한 번만 걸립니다.")
        print("  인터넷 사정에 따라 몇 분 걸릴 수 있습니다. 그냥 두세요.\n")

    @staticmethod
    def _line(order: int, total: int, step: SetupStep) -> None:
        print(f"  [{order}/{total}] {step.label}")
        print("        챙기는 중...", flush=True)

    @staticmethod
    def _done(text: str) -> None:
        print(f"        → {text}\n", flush=True)

    def _trouble(self, failed: Sequence[SetupStep]) -> None:
        print("\n  다음을 챙기지 못했습니다:")
        for step in failed:
            print(f"    · {step.title}")
        print("\n  인터넷 연결이나 회사망 차단을 확인해 주세요.")
        print(f"  직접 챙기려면:  {sys.executable} -m pip install opencv-python-headless pywebview")
        if sys.platform == "darwin":
            print("  맥에서 권한 때문에 막히면 앞에 --user 를 붙여 보세요.")
        print()
        try:
            input("  엔터를 누르면 그래도 실행해 봅니다...")
        except EOFError:
            pass

    def _remember(self) -> None:
        try:
            self.MARKER.parent.mkdir(parents=True, exist_ok=True)
            self.MARKER.write_text("ok\n", encoding="utf-8")
        except OSError:
            pass                       # 기록 못 해도 상관없다. 다음에 다시 확인할 뿐


# ══════════════════════════════════════════════════ 6. 화면


# 화면 HTML 은 같은 폴더의 ui.html (CSS · JS 포함 한 파일)
UI_HTML = Path(__file__).with_name('ui.html').read_text(encoding='utf-8')




# ══════════════════════════════════════════════════ 5-3. 띄우기

class UiDocument:
    """화면 HTML 한 장. 창이든 브라우저든 서버가 이걸 내준다."""

    def __init__(self, html: str) -> None:
        self._html = html

    def as_bytes(self) -> bytes:
        return self._html.encode("utf-8")


class DesktopWindow:
    """
    진짜 창. pywebview 가 있어야 한다.

    화면은 **로컬 서버 주소**를 물린다. 예전에는 임시 파일의 file:// 주소를
    물렸는데, 그러면 두 가지가 깨진다.

      · 대조 화면은 iframe 안에 산다. 출처가 없는 문서에서 file:// 을 읽는 것을
        브라우저 엔진이 막아서, 창은 떠도 영상 자리가 까맣게 남는다.
      · 파이썬이 화면으로 소식을 보낼 길이 없다. 예전에는 창에 자바스크립트를
        직접 밀어넣었는데 그건 맥에서 앱을 죽인다. 지금은 화면이 서버에서
        가져가는 한 길뿐이라, 서버가 없으면 아무 소식도 못 받는다.

    그래서 창도 브라우저와 같은 주소를 본다. 길이 하나면 시험도 하나로 끝난다.
    """

    def __init__(self, api: "Api", url: str) -> None:
        self._api = api
        self._url = url

    def open(self) -> None:
        if not DependencyBootstrap("webview", ("pywebview",), announce=False).try_ensure():
            raise RuntimeError("pywebview 를 설치하지 못했습니다")
        import webview
        window = webview.create_window(
            "Framing",
            url=self._url,
            js_api=self._api,
            # 두 칸(왼쪽 파일 · 오른쪽 작업)이 처음부터 나란히 서는 너비로 연다
            width=1360, height=900, min_size=(900, 640),
            background_color="#0f1113")
        self._api.bind(window)          # 창은 파일 고르기 · 끌어놓기 · 닫기에만
        # 끌어다 놓기는 창이 실제로 뜬 뒤에 걸어야 한다.
        # webview.start 에 넘긴 함수가 그 시점에 불린다 (공식 예제와 같은 방식).
        webview.start(self._after_start, window,
                      debug=bool(os.environ.get("FRAMING_DEBUG")))

    def _after_start(self, window) -> None:
        self._api.set_drop_ready(DropTarget(self._api).attach(window))
        WindowIcon().apply()


class DesktopShortcut:
    """
    바탕화면 바로가기를 만든다. 아이콘은 고양이.

    **.bat 파일 자체는 아이콘을 못 바꿉니다.** 윈도우가 .bat · .cmd 에는
    정해진 시스템 아이콘을 씁니다 — 파일 안에 무엇을 넣어도 바뀌지 않습니다.
    파일에 제 아이콘을 다는 건 .exe 와 바로가기(.lnk) 뿐입니다.

    그래서 바로가기를 만듭니다. 그 바로가기에는 아이콘을 달 수 있고, 작업
    표시줄에 고정하면 거기에도 같은 아이콘이 붙습니다.

        Framing.bat shortcut

    맥은 사정이 다릅니다 — .command 는 파인더에서 아이콘을 바꿀 수 있지만
    (Finder 정보 창에 붙여넣기) 명령으로 하려면 별도 도구가 필요해서, 여기서는
    아이콘 파일만 꺼내 두고 자리를 알려 줍니다.
    """

    NAME = "Framing"

    def make(self) -> int:
        icon = self._icon()
        if icon is None:
            Console.warn("아이콘을 꺼내지 못했습니다.")
            return 1
        if sys.platform != "win32":
            Console.info(f"아이콘  {icon}")
            Console.info("맥에서는 파인더에서 Framing.command 정보 창을 열고 "
                         "왼쪽 위 아이콘에 이 파일을 붙여넣으면 바뀝니다.")
            return 0
        return self._windows(icon)

    def _icon(self) -> Optional[Path]:
        """아이콘을 곁에 꺼내 둔다. 바로가기는 이 파일을 계속 가리킨다."""
        blob = app_icon_bytes()
        if blob is None:
            return None
        try:
            home = Path.home() / ".framing"
            home.mkdir(parents=True, exist_ok=True)
            spot = home / "framing.ico"
            spot.write_bytes(blob)
            return spot
        except Exception as exc:
            Console.warn(f"아이콘을 쓰지 못했습니다: {exc}")
            return None

    def _windows(self, icon: Path) -> int:
        launcher = Path(SELF).resolve()
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            desktop = Path.home()
        link = desktop / f"{self.NAME}.lnk"
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
            "$s.TargetPath = '{target}';"
            "$s.WorkingDirectory = '{folder}';"
            "$s.IconLocation = '{icon}';"
            "$s.Description = 'Framing';"
            "$s.Save()"
        ).format(**{key: str(value).replace("'", "''") for key, value in
                    dict(link=link, target=launcher, folder=launcher.parent, icon=icon).items()})
        try:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True)
        except OSError as exc:
            Console.warn(f"powershell 을 부르지 못했습니다: {exc}")
            return 1
        if done.returncode != 0 or not link.exists():
            Console.warn(f"바로가기를 만들지 못했습니다: {(done.stderr or '').strip()[:200]}")
            return 1
        Console.info(f"바로가기  {link}")
        Console.info("이걸 더블클릭해서 여세요. 작업표시줄에 고정하면 거기에도 "
                     "같은 아이콘이 붙습니다.")
        return 0


class WindowIcon:
    """
    창과 작업표시줄의 아이콘을 고양이로.

    .bat · .command 로 도는 길은 파이썬이 스크립트를 읽는 것이라, 윈도우는 창
    아이콘으로 **파이썬 인터프리터의 것**을 씁니다. build/icon.ico 를 새로
    구워도 이 길에는 닿지 않습니다 — 그건 패키징한 exe 가 쓰는 파일입니다.

    그래서 아이콘을 실행 파일 안에 글자로 싣고 다니다가, 창이 뜬 뒤에 둘을 합니다.

      1. 프로세스에 제 이름표를 답니다(AppUserModelID). 안 달면 작업표시줄에서
         파이썬으로 묶입니다.
      2. 창 손잡이를 찾아 WM_SETICON 을 보냅니다.

    윈도우에서만 하는 일이고, 무엇 하나 안 되더라도 조용히 넘어갑니다 —
    아이콘 때문에 프로그램이 안 열리면 그게 훨씬 나쁩니다.
    """

    APP_ID = "AlienMeow.Framing"
    TITLE = "Framing"
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
    WAIT_SECONDS, STEP = 6.0, 0.2

    def apply(self) -> None:
        if sys.platform != "win32":
            return
        spot = self._write()
        if spot is not None:
            threading.Thread(target=self._stamp, args=(spot,), daemon=True).start()

    def _write(self) -> Optional[Path]:
        """싣고 다니던 아이콘을 파일로 꺼낸다 — Win32 는 파일 경로만 받는다."""
        blob = app_icon_bytes()
        if blob is None:
            return None
        try:
            spot = Path(tempfile.gettempdir()) / "framing-icon.ico"
            spot.write_bytes(blob)
            return spot
        except Exception:
            return None

    def _stamp(self, spot: Path) -> None:
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(self.APP_ID)
        except Exception:
            pass
        try:
            user32 = ctypes.windll.user32
            handle = user32.LoadImageW(None, str(spot), self.IMAGE_ICON, 0, 0,
                                       self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE)
            if not handle:
                return
            # 창이 실제로 뜨는 시점은 백엔드마다 다르다. 몇 초만 지켜본다.
            waited = 0.0
            while waited < self.WAIT_SECONDS:
                window = user32.FindWindowW(None, self.TITLE)
                if window:
                    for which in (self.ICON_SMALL, self.ICON_BIG):
                        user32.SendMessageW(window, self.WM_SETICON, which, handle)
                    return
                time.sleep(self.STEP)
                waited += self.STEP
        except Exception:
            pass


class StartupRequest:
    """실행할 때 받은 인자를 읽어, 터미널로 갈지 창으로 갈지 정한다."""

    HELP_WORDS = ("explain", "help", "?")

    def __init__(self, args: Sequence[str]) -> None:
        self._args = [str(a) for a in args]

    @property
    def args(self) -> list[str]:
        return list(self._args)

    @property
    def paths(self) -> list[str]:
        """전부 실제로 있는 경로일 때만 목록으로 본다 (아이콘 위에 끌어다 놓은 경우)."""
        if not self._args:
            return []
        found = [a for a in self._args if Path(a).expanduser().exists()]
        return found if len(found) == len(self._args) else []

    @property
    def wants_cli(self) -> bool:
        if not self._args:
            return False
        first = self._args[0].strip().lower()
        if first in self.HELP_WORDS or CommandLine.job_for(first) is not None:
            return True
        if any(a.startswith("-") for a in self._args):
            return True
        return not self.paths          # 경로도 아니면 터미널이 이유를 알려준다


class FramingApp:
    """어느 방식으로 띄울지 정하고 띄운다. 창이 안 되면 브라우저로."""

    def __init__(self, carried: Sequence[str] = ()) -> None:
        self._api = Api()
        self._api.carry(carried)
        self._ui = UiDocument(UI_HTML)

    def launch(self) -> int:
        """
        서버를 먼저 세우고, 그 주소를 창에 물린다. 창이 안 되면 브라우저로.

        서버는 화면만 내주는 것이 아니다 — 엔진이 뱉는 소식을 담아 두는 큐이자,
        대조 화면이 영상을 읽어 가는 길이다. 그래서 창 모드에도 반드시 있어야 한다.
        """
        server = LocalServer(self._api, self._ui)
        url = server.start()
        try:
            if (os.environ.get("FRAMING_MODE") or "").lower() != "browser":
                try:
                    DesktopWindow(self._api, url).open()
                    return 0
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    print(f"[알림] 창을 띄우지 못했습니다: {exc}")
                    print("[알림] 대신 기본 브라우저로 엽니다. 기능은 똑같습니다.")
            server.open_browser()
            server.wait()
            return 0
        finally:
            server.stop()




def main(argv: Optional[Sequence[str]] = None) -> int:
    """넘어지면 조용히 사라지지 않는다 — 무슨 일이었는지 쪽지를 남긴다."""
    # Redirected Windows terminals may default to CP949. Help and diagnostics
    # contain Unicode symbols and filenames; they must not crash while printing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                pass
    try:
        return Startup.run(argv)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        spot = CrashNote.write(exc)
        print("\n[!] Framing 을 시작하지 못했습니다.")
        print(f"    {type(exc).__name__}: {exc}")
        if spot is not None:
            print(f"\n    무슨 일이 있었는지 여기 적어 두었습니다:\n    {spot}")
            print("    이 파일을 보내 주시면 원인을 찾을 수 있습니다.")
        return 1


class Startup:
    """
    켜질 때 어디로 갈지 고른다 — 터미널이냐 창이냐.

    작업 이름이나 옵션이 붙어 있으면 터미널 도구로, 아이콘을 눌러 들어왔으면
    창으로 간다. 앱으로 구운 판은 챙길 것이 이미 안에 있어서 준비 단계를
    건너뛴다 — 안 그러면 창이 두 번 뜬다.
    """

    @staticmethod
    def wizard() -> SetupWizard:
        """처음 쓰는 사람이 챙겨야 할 것들. 순서대로."""
        return SetupWizard([
            PipStep("영상 처리 opencv", "cv2", OPENCV_PACKAGES, "약 60MB"),
            PipStep("프로그램 창 pywebview", "webview", ("pywebview",), "약 1MB", optional=True),
            FfmpegStep(FFmpegTool(allow_download=True)),
        ])

    @classmethod
    def run(cls, argv: Optional[Sequence[str]] = None) -> int:
        request = StartupRequest(sys.argv[1:] if argv is None else argv)
        if request.wants_cli:
            return CommandLine(request.args).run()         # 작업 이름이나 옵션이 있으면 터미널 도구로

        # 앱으로 구운 판(.exe · .app)은 필요한 것을 이미 안고 있다. 챙길 것도,
        # 콘솔을 떼려고 자기를 다시 띄울 것도 없다 — 하면 오히려 두 번 뜬다.
        # 앱으로 구운 판과 포터블 판(Framing.bat · 내장 파이썬)은 챙길 것을 이미 다 안고 있다
        if not getattr(sys, "frozen", False) and not os.environ.get("FRAMING_PORTABLE"):
            cls.wizard().ensure()               # 처음이면 여기서 다 챙긴다 (콘솔에 보이게)
            if Relauncher.detach_console():
                return 0                         # 콘솔을 떼고 새로 띄웠으니 여기서 끝
        return FramingApp(request.paths).launch()   # 아이콘 위에 놓은 파일을 들고 창으로


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
        raise SystemExit(0)
