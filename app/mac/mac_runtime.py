"""macOS services. Imports Cocoa only when an actual Mac window is needed."""
from dataclasses import dataclass
from pathlib import Path
import logging, os, subprocess, sys, threading
from logging.handlers import RotatingFileHandler

@dataclass(frozen=True)
class MacPaths:
    home: Path
    resources: Path

    @classmethod
    def current(cls):
        return cls(Path.home(),Path(getattr(sys,'_MEIPASS',Path(__file__).parent)))

    @property
    def support(self):return self.home/'Library'/'Application Support'/'Framing'
    @property
    def logs(self):return self.home/'Library'/'Logs'/'Framing'
    @property
    def cache(self):return self.home/'Library'/'Caches'/'Framing'

    def prepare(self):
        for p in (self.support,self.logs,self.cache):p.mkdir(parents=True,exist_ok=True)

    def ffmpeg(self,name):
        path=self.resources/'_ffmpeg'/name
        if not path.is_file() or not os.access(path,os.X_OK):
            raise RuntimeError(f'앱 안의 {name} 실행 파일이 없습니다. Mac에서 앱을 다시 빌드해 주세요.')
        return str(path)

class AppLog:
    def __init__(self,folder):
        self.logger=logging.getLogger('framing.mac')
        self.logger.setLevel(logging.INFO)
        handler=RotatingFileHandler(folder/'app.log',maxBytes=4_000_000,backupCount=2,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        self.logger.addHandler(handler)
    def write(self,text):
        if text.strip():self.logger.info(text.rstrip())
        return len(text)
    def flush(self):
        for handler in self.logger.handlers:handler.flush()
    def isatty(self):return False

class NativeDialogs:
    def __init__(self,window):self.window=window
    @staticmethod
    def paths(value):
        if not value:return []
        return [str(v) for v in ([value] if isinstance(value,(str,Path)) else value)]
    def files(self,extensions):
        import webview
        pattern=';'.join('*'+e for e in sorted(extensions))
        return self.paths(self.window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=True,
            file_types=(f'영상 및 이미지 ({pattern})','모든 파일 (*.*)')))
    def folder(self):
        import webview
        return self.paths(self.window.create_file_dialog(webview.FileDialog.FOLDER))
    def save(self,name):
        import webview
        return self.paths(self.window.create_file_dialog(webview.FileDialog.SAVE,save_filename=name,
            file_types=('Framing 작업 (*.json)',)))

class ChildProcesses:
    """Track only the media processes owned by this application."""
    def __init__(self):self.items=[];self.lock=threading.Lock();self.original=None
    def install(self):
        self.original=subprocess.Popen;owner=self
        class TrackedProcess(owner.original):
            def __init__(self,args,*pos,**kw):
                super().__init__(args,*pos,**kw)
                first=args[0] if isinstance(args,(list,tuple)) and args else ''
                if Path(str(first)).stem in ('ffmpeg','ffprobe','rife-ncnn-vulkan'):
                    with owner.lock:
                        owner.items=[p for p in owner.items if p.poll() is None]
                        owner.items.append(self)
        subprocess.Popen=TrackedProcess
    def close(self):
        with self.lock:items=list(self.items)
        for p in items:
            if p.poll() is None:
                try:p.terminate()
                except ProcessLookupError:pass
        for p in items:
            try:p.wait(timeout=3)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
        if self.original:subprocess.Popen=self.original

def error_alert(message):
    # All callers are on the Python/Cocoa main thread.
    if sys.platform=='darwin':
        from AppKit import NSAlert
        alert=NSAlert.alloc().init()
        alert.setMessageText_('Framing을 열지 못했습니다')
        alert.setInformativeText_(str(message))
        alert.addButtonWithTitle_('확인')
        alert.runModal()
    else:print(message,file=sys.stderr)
