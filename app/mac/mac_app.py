"""Cocoa/WKWebView desktop shell; no external server or browser fallback."""
import json, multiprocessing, os, sys, traceback, uuid
from pathlib import Path
from framing import core
from mac_runtime import MacPaths, AppLog, NativeDialogs, ChildProcesses, error_alert

class BundledFFmpeg(core.FFmpegTool):
    def __init__(self,paths):super().__init__(False);self.paths=paths
    def find(self,name):return self.paths.ffmpeg(name)

class MacApi(core.Api):
    def __init__(self,paths):
        super().__init__();self._paths=paths
        self._ff=BundledFFmpeg(paths)
        self.PREFS_FILE=paths.support/'preferences.json'
        self.KEYWORD_FILE=paths.support/'keywords.json'
    def prefs(self):return {'lang':'kr',**super().prefs()}
    def pick_files(self,job_key=''):
        try:return NativeDialogs(self._window).files(self._accepts(job_key)[0])
        except Exception as e:
            self.notify('error','파일 선택 창을 열지 못했습니다: '+str(e));return []
    def pick_folder(self):
        try:return NativeDialogs(self._window).folder()
        except Exception as e:
            self.notify('error','폴더 선택 창을 열지 못했습니다: '+str(e));return []
    def project_save(self,data):
        try:
            document=core.ProjectDocument.dumps(data)
            picked=NativeDialogs(self._window).save('Framing.framing.json')
            if not picked:return {'ok':True,'cancelled':True}
            destination=Path(picked[0])
            if not destination.name.endswith('.framing.json'):destination=destination.with_name(destination.name+'.framing.json')
            temporary=destination.with_name('.'+destination.name+'.'+uuid.uuid4().hex+'.tmp')
            try:temporary.write_text(document,encoding='utf-8');temporary.replace(destination)
            finally:temporary.unlink(missing_ok=True)
            return {'ok':True,'path':str(destination)}
        except Exception as e:return {'ok':False,'error':str(e)}
    def save_report(self,text):
        try:
            path=self._paths.logs/'진단.txt';path.write_text(str(text),encoding='utf-8')
            return {'ok':True,'path':str(path)}
        except OSError as e:return {'ok':False,'error':str(e)}
    def _build_options(self,raw):
        options=super()._build_options(raw)
        if options.retime_mode=='rife':
            runner=core.RifeRunner(options.rife_program)
            if not runner.ready:raise core.FramingError('Mac용 RIFE 실행 파일과 모델이 필요합니다. FFmpeg · 모션을 선택해 주세요.')
            options.rife_program=runner._program
        return options

class MacDocument(core.UiDocument):
    def __init__(self):
        html=core.UI_HTML
        # Do not offer the Windows-only bundled GPU engine on a default Mac build.
        if not core.RifeRunner().ready:
            old="['rife', 'RIFE · AI / GPU'], "
            if old not in html:raise RuntimeError('Mac 보간 메뉴를 확인해야 합니다.')
            html=html.replace(old,'')
        super().__init__(html)

class MacApplication:
    def __init__(self,paths):
        self.paths=paths;self.api=MacApi(paths);self.children=ChildProcesses();self.server=None
    def launch(self):
        import webview
        for name in ('ffmpeg','ffprobe'):self.paths.ffmpeg(name)
        self.children.install()
        self.server=core.LocalServer(self.api,MacDocument())
        try:
            url=self.server.start()
            window=webview.create_window('Framing',url,js_api=self.api,width=1360,height=900,
                min_size=(900,640),background_color='#0f1113',confirm_close=True)
            self.api.bind(window)
            webview.start(self.after_start,window,gui='cocoa',debug=False,
                private_mode=False,storage_path=str(self.paths.cache))
        finally:
            self.api._stop.set();self.api._ff.abort();self.children.close()
            if self.api._worker:self.api._worker.join(timeout=5)
            core.Scratch.clear()
            if self.server:self.server.stop()
        return 0
    def after_start(self,window):self.api.set_drop_ready(core.DropTarget(self.api).attach(window))

def configure(paths):
    paths.prepare()
    core.HERE=paths.resources
    core.FFMPEG_HOME=paths.resources/'_ffmpeg'
    core.BUILD='2026.10.12-mac.1'
    core.CrashNote.SPOT=paths.logs/'오류.txt'

def main():
    if sys.platform!='darwin':
        print('Mac 앱은 macOS에서 빌드·실행하세요. 이 파일은 Windows 실행 파일이 아닙니다.')
        return 2
    paths=MacPaths.current();configure(paths)
    if getattr(sys,'frozen',False):sys.stdout=sys.stderr=AppLog(paths.logs)
    if len(sys.argv)==3 and sys.argv[1]=='--self-test':
        from mac_diagnostics import run
        return run(paths,Path(sys.argv[2]))
    try:return MacApplication(paths).launch()
    except Exception:
        details=traceback.format_exc();(paths.logs/'오류.txt').write_text(details,encoding='utf-8')
        error_alert('오류 기록: '+str(paths.logs/'오류.txt')+'\n\n'+details.splitlines()[-1]);return 1

if __name__=='__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
