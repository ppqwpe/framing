import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac')); sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from framing import core
from mac_app import MacApi, MacApplication, MacDocument
from mac_runtime import MacPaths, NativeDialogs, ChildProcesses

class MacContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='Mac contract ')
        self.root=Path(self.temp.name)
        self.paths=MacPaths(self.root/'사용자',self.root/'앱 리소스')
        self.paths.prepare()
    def tearDown(self):self.temp.cleanup()
    def media_tools(self):
        folder=self.paths.resources/'_ffmpeg';folder.mkdir(parents=True)
        for name in ('ffmpeg','ffprobe'):
            p=folder/name;p.write_bytes(b'test');p.chmod(0o755)
    def test_app_storage_is_outside_bundle(self):
        self.assertTrue(self.paths.support.is_dir())
        self.assertFalse(self.paths.support.is_relative_to(self.paths.resources))
        api=MacApi(self.paths);self.assertEqual(api.prefs()['lang'],'kr')
        api.prefs_save({'lang':'en'});self.assertEqual(MacApi(self.paths).prefs()['lang'],'en')
    def test_ffmpeg_cannot_fall_back_to_host(self):
        with self.assertRaises(RuntimeError):self.paths.ffmpeg('ffmpeg')
        self.media_tools();self.assertTrue(Path(self.paths.ffmpeg('ffprobe')).is_file())
    def test_native_picker_paths_cancel_and_filter(self):
        window=Mock();view=SimpleNamespace(FileDialog=SimpleNamespace(OPEN=1,FOLDER=2,SAVE=3))
        with patch.dict(sys.modules,{'webview':view}):
            window.create_file_dialog.return_value=('/Users/유저/한 글.mov','/Volumes/素材/A.mp4')
            self.assertEqual(len(NativeDialogs(window).files({'.mov','.mp4'})),2)
            self.assertIn(';',window.create_file_dialog.call_args.kwargs['file_types'][0])
            window.create_file_dialog.return_value=None
            self.assertEqual(NativeDialogs(window).folder(),[])
            window.create_file_dialog.return_value='/Users/유저/작업.json'
            self.assertEqual(NativeDialogs(window).save('작업.json'),['/Users/유저/작업.json'])
    def test_picker_failure_is_reported_without_tk(self):
        api=MacApi(self.paths);api.notify=Mock()
        with patch('mac_app.NativeDialogs.files',side_effect=RuntimeError('picker failed')):
            self.assertEqual(api.pick_files(),[])
        api.notify.assert_called_once()
    def test_project_save_and_cancel(self):
        api=MacApi(self.paths);target=self.root/'작업 저장'
        with patch('mac_app.NativeDialogs.save',return_value=[str(target)]),patch.object(core.ProjectDocument,'dumps',return_value='{"test":true}'):
            saved=api.project_save({});self.assertTrue(saved['ok'])
            self.assertEqual(json.loads(Path(saved['path']).read_text()),{'test':True})
        with patch('mac_app.NativeDialogs.save',return_value=[]),patch.object(core.ProjectDocument,'dumps',return_value='{}'):
            self.assertTrue(api.project_save({})['cancelled'])
    def test_missing_rife_not_offered(self):
        with patch.object(core.RifeRunner,'ready',new_callable=__import__('unittest').mock.PropertyMock,return_value=False):
            self.assertNotIn("['rife', 'RIFE · AI / GPU']",MacDocument().as_bytes().decode())
            with self.assertRaises(core.ShotkitError):MacApi(self.paths)._build_options({'retimeMode':'rife'})
    def test_cocoa_window_and_cleanup_no_browser_fallback(self):
        self.media_tools();view=Mock();server=Mock();server.start.return_value='http://127.0.0.1:45678/'
        with patch.dict(sys.modules,{'webview':view}),patch.object(core,'LocalServer',return_value=server):
            self.assertEqual(MacApplication(self.paths).launch(),0)
        self.assertEqual(view.start.call_args.kwargs['gui'],'cocoa')
        server.stop.assert_called_once();server.open_browser.assert_not_called()
    def test_cocoa_failure_still_stops_internal_server(self):
        self.media_tools();view=Mock();view.start.side_effect=RuntimeError('Cocoa failed');server=Mock()
        with patch.dict(sys.modules,{'webview':view}),patch.object(core,'LocalServer',return_value=server):
            with self.assertRaises(RuntimeError):MacApplication(self.paths).launch()
        server.stop.assert_called_once();server.open_browser.assert_not_called()
    def test_process_shutdown_only_owns_registered_processes(self):
        supervisor=ChildProcesses();process=Mock();process.poll.return_value=None;supervisor.items=[process]
        supervisor.close();process.terminate.assert_called_once();process.wait.assert_called_once()
    def test_finder_reveal_uses_argument_list(self):
        folder=Path('/Users/유저/샷 폴더');selected=folder/'a; echo bad.mp4'
        with patch.object(core.subprocess,'run') as run:
            core.MacReveal().plans(folder,selected)[0]()
        self.assertEqual(run.call_args.args[0],['open','-R',str(selected)])

if __name__=='__main__':unittest.main()
