from pathlib import Path
import sys,types,unittest,tempfile,subprocess
from unittest.mock import patch
APP=Path(__file__).resolve().parents[1]/'framing'/'core.py'
m=types.ModuleType('hardening_test');m.__file__=str(APP);sys.modules[m.__name__]=m
exec(compile(APP.read_text(encoding='utf-8'),str(APP),'exec'),m.__dict__)

class HardeningTests(unittest.TestCase):
 def test_cancel_before_proxy_does_not_encode(self):
  ff=types.SimpleNamespace(run=lambda *a,**k:self.fail('started encoding after cancel'))
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(m.ShotkitError):m.PlayableProxy(ff,Path(tmp),cancelled=lambda:True).make(Path('a.mov'),None,True,'view')
 def test_cancel_during_proxy_does_not_retry_or_return_success(self):
  for code in [0,1]:
   with self.subTest(code=code),tempfile.TemporaryDirectory() as tmp:
    stopped=[False];calls=[]
    def run(args,**kwargs):
     calls.append(args);Path(args[-1]).write_bytes(b'x'*2048);stopped[0]=True;return code
    with self.assertRaises(m.ShotkitError):m.PlayableProxy(types.SimpleNamespace(run=run),Path(tmp),cancelled=lambda:stopped[0]).make(Path('a.mov'),None,True,'view')
    self.assertEqual(len(calls),1)
 def test_normal_proxy_failure_still_retries(self):
  with tempfile.TemporaryDirectory() as tmp:
   calls=[]
   def run(args,**kwargs):
    calls.append(args)
    if len(calls)==2:Path(args[-1]).write_bytes(b'x'*2048);return 0
    return 1
   result=m.PlayableProxy(types.SimpleNamespace(run=run),Path(tmp)).make(Path('a.mov'),None,True,'view')
   self.assertIsNotNone(result);self.assertEqual(len(calls),2)
 def test_no_viewer_published_after_cancel(self):
  job=m.CompareJob(m.Options(),m.FFmpegTool(False));stopped=[False];job.should_stop=lambda:stopped[0]
  job.show_inline=lambda *args:self.fail('published after cancel')
  def watch(*args):stopped[0]=True;return Path('a.mp4'),Path('b.mp4'),''
  info=m.ProbeInfo(vcodec='h264',width=96,height=64,fps=24,duration=1)
  with tempfile.TemporaryDirectory() as tmp,patch.object(m.Scratch,'folder',return_value=Path(tmp)),patch.object(job,'_probe_both',return_value=(info,info)),patch.object(job,'_watchable',side_effect=watch):
   with self.assertRaises(m.ShotkitError):job.run_pair(Path('a.mp4'),Path('b.mp4'))
 def test_large_error_output_does_not_block_and_preserves_tail(self):
  # Real child process writes much more than the OS pipe capacity to stderr.
  real_popen=subprocess.Popen;children=[]
  def launch(_command,**kwargs):
   proc=real_popen([sys.executable,'-c',"import sys;sys.stderr.write('x'*2000000+'\\nFINAL ENCODER ERROR\\n');sys.stderr.flush();print('out_time_us=100000',flush=True);sys.exit(9)"],**kwargs)
   children.append(proc);return proc
  ff=m.FFmpegTool(False);ff._last_error='stale';progress=[]
  with patch.object(m.subprocess,'Popen',side_effect=launch),patch.object(m.Progress,'span',side_effect=progress.append):
   self.assertEqual(ff._run_with_progress('encoder',[],True,1),9)
  self.assertIn('FINAL ENCODER ERROR',ff.why_failed());self.assertNotIn('stale',ff.why_failed());self.assertLess(len(ff.why_failed()),16450)
  self.assertNotIn(1.0,progress);self.assertIsNone(ff._active);self.assertTrue(all(p.poll() is not None for p in children))
 def test_real_ffmpeg_error_is_captured_for_normal_export(self):
  ff=m.FFmpegTool(False)
  code=ff.run(['-i','__SHOTKIT_MISSING_INPUT_FOR_TEST__.mp4','-f','null','-'],total_seconds=1)
  self.assertNotEqual(code,0);self.assertIn('__SHOTKIT_MISSING_INPUT_FOR_TEST__',ff.why_failed())

if __name__=='__main__':unittest.main(verbosity=2)
