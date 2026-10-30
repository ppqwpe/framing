"""이름 바꾸기 계획 · 실행 · 되돌리기, 납품 이름표"""
from __future__ import annotations

import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .errors import FramingError


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


