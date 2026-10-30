"""소식 버스 · 로컬 서버"""
from __future__ import annotations

import json
import re
import threading
from typing import TYPE_CHECKING

from .errors import FramingError

if TYPE_CHECKING:   # 타입 표기에만 쓰는 이름
    from .api import Api
    from .desktop import UiDocument


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


