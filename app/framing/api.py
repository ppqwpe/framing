"""화면이 부르는 Api"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import env
from .errors import EngineBridge, FramingError, OperationGate
from .naming import BatchNamePlan, DeliverySlate, NamePlan, RenameHistory
from .options import Options, UiOptions
from .env import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .util import Scratch
from .media import FFmpegTool, VideoSource, collect_images
from .i18n import MessageCatalog
from .job import PickSpec
from .jobs_extract import DELIVERY_CHOICES
from .jobs_compare import PlayableProxy
from .jobs_rename import ProjectDocument
from .jobs_plate import UiJobSelection
from .registry import JOB_CLASSES
from .compare_export import CompareExporter
from .cli import TargetCollector, WorkPlan
from .app import FolderOpener


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
            'build': env.BUILD,
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
        from .checkup import Checkup   # 순환 임포트를 피해 여기서
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

