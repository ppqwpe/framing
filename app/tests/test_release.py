"""Release compatibility and boundary checks. Run next to Framing.bat and its baseline JSON."""
from pathlib import Path
import dataclasses,json,sys,types,unittest,tempfile,subprocess,shutil,re,os
HERE=Path(__file__).resolve().parent
APP=Path(__file__).resolve().parents[1]/'framing'/'core.py'
source=APP.read_text(encoding='utf-8')
m=types.ModuleType('framing_release_test');m.__file__=str(APP);sys.modules[m.__name__]=m
exec(compile(source,str(APP),'exec'),m.__dict__)
baseline=json.loads((HERE/'compat_baseline.json').read_text(encoding='utf-8'))
class ReleaseTests(unittest.TestCase):
 @unittest.skipUnless(os.name=='nt','Windows batch launcher')
 def test_batch_help_and_error_codes(self):
  for flag,expected in [('--help',0),('--invalid-option-for-test',2)]:
   result=subprocess.run(['cmd','/d','/c',str(APP),flag],capture_output=True,encoding='utf-8',errors='replace',timeout=30)
   self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
   self.assertNotIn('시작하지 못했습니다',result.stdout)
 @unittest.skipUnless(os.name=='nt','Windows python fallback')
 def test_batch_python_without_py_launcher(self):
  env=os.environ.copy();env['PATH']=str(Path(sys.executable).parent)+';'+os.environ['SystemRoot']+'/System32'
  result=subprocess.run(['cmd','/d','/c',str(APP),'--help'],capture_output=True,encoding='utf-8',errors='replace',env=env,timeout=30)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertIn('usage:',result.stdout.lower())
 def test_regular_video_preserves_explicit_material_choice(self):
  import numpy as np
  samples=[np.full((100,100,3),(0,0,255),np.uint8)]*5
  job=m.RetimeJob(m.Options(no_snap=True),m.FFmpegTool(False))
  snapper,matte=job._read_material(samples)
  self.assertIsNone(snapper);self.assertFalse(matte)
  plan=m.RetimePlan.decide(5,24,frames=8)
  engine=job._pick_engine(plan,matte,snapper,Path(tempfile.gettempdir()),samples[0])
  self.assertEqual(engine.KEY,'motion')
 def test_original_option_defaults_preserved(self):
  self.assertEqual(dataclasses.asdict(m.Options()),baseline['defaults'])
 def test_original_jobs_and_aliases_preserved(self):
  actual={k:{'key':v.KEY,'aliases':list(v.ALIASES),'paired':v.PAIRED,'folder':v.ACCEPTS_FOLDER} for k,v in m.JOB_REGISTRY.items()}
  self.assertEqual(actual,baseline['jobs'])
 def test_original_conversion_targets_preserved(self):
  self.assertEqual([v.KEY for v in m.CONVERT_TARGETS],baseline['targets'])
 def test_high_and_low_source_rates(self):
  for fps in [0.5,480,1000,24000/1001]:
   self.assertAlmostEqual(m.FrameRate.from_value(fps).value,fps)
 def test_probe_uses_video_count_and_duration(self):
  info=m.ProbeInfo(fps=480,duration=0.009,video_duration=0.008333,reported_frames=4)
  self.assertEqual(info.frames,4)
  info.reported_frames=0;self.assertEqual(info.frames,4)
  info.fps=24;info.duration=10;info.video_duration=1
  self.assertEqual(info.frames,24)
 def test_high_and_low_fps_mp4_output(self):
  ff=m.FFmpegTool(False)
  if not ff.find('ffmpeg') or not ff.find('ffprobe'):self.skipTest('FFmpeg required')
  import numpy as np
  with tempfile.TemporaryDirectory() as name:
   for fps in [0.5,480,24000/1001]:
    folder=Path(name)/str(fps);folder.mkdir()
    for i in range(1,5):m.ImageIO.write(folder/f'out.{i:04d}.png',np.full((16,16,3),i*40,np.uint8))
    job=m.RetimeJob(m.Options(),ff)
    movie=job._mux(folder,m.RetimePlan(4,4,fps),4,m.ColorProfile(),m.Delivery.pick('both'),folder/'input.mp4')
    self.assertIsNotNone(movie)
    info=ff.probe(movie);self.assertEqual(info.frames,4);self.assertAlmostEqual(info.fps,fps,places=6)
 def test_invalid_source_rates(self):
  for fps in [0,-1,None,float('nan'),float('inf'),'bad']:
   with self.assertRaises(m.FramingError):m.FrameRate.from_value(fps)
 def test_sequence_limit_preserved(self):
  import numpy as np
  with tempfile.TemporaryDirectory() as name:
   folder=Path(name);m.ImageIO.write(folder/'frame0001.png',np.zeros((16,16,3),np.uint8))
   for fps in [0.5,480]:
    with self.assertRaises(m.FramingError):m.SequenceInput(folder,fps,lambda:None)
 def test_restore_capability_serialized(self):
  note=m.StretchNote(m.PlateSpec(source='x',frames=3,fps=24),5,[0,-1,-1,-1,2],[1,2,3])
  self.assertFalse(note.as_dict()['canRestoreSource'])
  self.assertTrue(note.as_dict()['restorationProblem'])
  self.assertTrue(m.StretchNote.from_dict(note.as_dict()).restoration_problem)
 def test_legacy_stretch_note_loads(self):
  note=m.StretchNote(m.PlateSpec(source='x',frames=3,fps=24),5,[0,1,2,2,2],[3,4]).as_dict()
  note.pop('canRestoreSource');note.pop('restorationProblem')
  self.assertEqual(m.StretchNote.from_dict(note).kept,[0,1,2])
 def test_api_exports_cover_ui(self):
  calls=set(re.findall(r'api\(\)\.(\w+)\(',m.UI_HTML))
  self.assertFalse(calls-m.ApiDispatcher.METHODS,calls-m.ApiDispatcher.METHODS)
 def test_primary_javascript_parses(self):
  if not shutil.which('node'):self.skipTest('Node.js required for UI syntax check')
  scripts=re.findall(r'<script[^>]*>(.*?)</script>',m.UI_HTML,re.S)
  self.assertTrue(scripts)
  with tempfile.TemporaryDirectory() as name:
   for i,script in enumerate(scripts):
    p=Path(name)/f'ui{i}.js';p.write_text(script,encoding='utf-8')
    result=subprocess.run(['node','--check',str(p)],capture_output=True,text=True,encoding='utf-8')
    self.assertEqual(result.returncode,0,result.stderr)
 def test_result_preview_selects_media_only_and_movie_first(self):
  if not shutil.which('node'):self.skipTest('Node.js required for preview selection check')
  cls='class ResultMedia {'+m.UI_HTML.split('class ResultMedia {',1)[1].split('class ReviewPanel {',1)[0]
  code="const assert=require('node:assert/strict');\n"+cls+'''
const items=ResultMedia.items({files:['out.0001.png','out.0002.png','note.json'],nextFiles:['frames','movie.mp4','note.txt'],retime:{movie:'movie.mp4'}});
assert.deepEqual(items.map(x=>x.path),['movie.mp4','out.0001.png','out.0002.png']);
assert.deepEqual(ResultMedia.items({files:['inspection.json']}),[]);
assert.deepEqual(ResultMedia.items({files:['OUTPUT.MOV']}),[{path:'OUTPUT.MOV'}]);
'''
  with tempfile.TemporaryDirectory() as name:
   p=Path(name)/'preview.js';p.write_text(code,encoding='utf-8')
   result=subprocess.run(['node',str(p)],capture_output=True,text=True,encoding='utf-8')
   self.assertEqual(result.returncode,0,result.stderr)
 def test_ui_input_and_length_behavior(self):
  if not shutil.which('node'):self.skipTest('Node.js required for UI rules check')
  classes=m.UI_HTML.split('class RetimeWorkflow {',1)[1].split('const CARDS =',1)[0]
  code="const assert=require('node:assert/strict'); const t=x=>x;\nclass RetimeWorkflow {"+classes+'''
assert.equal(OptionInput.read(''), '');
assert.equal(OptionInput.read('-2'), -2);
for(const value of ['',null,NaN,Infinity,-1,0,2.5,6001])assert.ok(OptionInput.problem('targetFrames',value));
assert.equal(OptionInput.problem('targetFrames',200),'');
assert.equal(OptionInput.problem('maxHeight',0),'');
assert.ok(OptionInput.problem('gifFps',51));
assert.equal(OptionInput.problem('fps',23.976),'');
assert.ok(OptionInput.validate({key:'retime',options:{lengthMode:'frames',targetFrames:''}},{}));
assert.equal(OptionInput.validate({key:'retime',options:{retimeWay:'undo',revertBy:'note',targetFrames:''}},{}),null);
assert.ok(OptionInput.validate({key:'extract',options:{pickMode:'at',at:''}},{}));
assert.ok(OptionInput.validate({key:'retime',options:{retimeWay:'undo',revertBy:'manual',dropFrames:''}},{}));
assert.ok(OptionInput.validate({key:'cuts',options:{cutMode:'manual',cutTimes:''}},{}));
assert.equal(OptionInput.validate({key:'extract',options:{pickMode:'default',count:''}},{}),null);
for(const [value,expected] of [[2.5,2],[3.5,4],[4.4999999,4],[4.5000001,5]])assert.equal(LengthChange.roundFrames(value),expected);
const row={v:{frames:176,fps:23.976,fpsExact:24000/1001}};
assert.equal(new LengthChange([row],{lengthMode:'seconds',targetSeconds:1001/48}).target,500);
assert.equal(new LengthChange([{v:{frames:5,fps:24}}],{lengthMode:'speed',speed:2}).target,2);
const mixed=new LengthChange([row,{v:{frames:176,fps:24}}],{lengthMode:'seconds',targetSeconds:10});
assert.equal(mixed.known,false);
console.log('UI boundary assertions passed');
'''
  with tempfile.TemporaryDirectory() as name:
   p=Path(name)/'rules.js';p.write_text(code,encoding='utf-8')
   result=subprocess.run(['node',str(p)],capture_output=True,text=True,encoding='utf-8')
   self.assertEqual(result.returncode,0,result.stderr)
if __name__=='__main__':unittest.main(verbosity=2)
