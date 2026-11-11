"""Unified retime routing, compatibility and preflight boundaries."""
from pathlib import Path
import sys,types,unittest,tempfile,subprocess,shutil
from unittest.mock import patch
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from framing import core as m

class UnifiedRetimeTests(unittest.TestCase):
 def test_legacy_windows_console_does_not_abort_job(self):
  import io
  data=io.BytesIO();stream=io.TextIOWrapper(data,encoding='cp949')
  with patch.object(sys,'stdout',stream):m.Console.info('완료 — RIFE')
  stream.flush();self.assertIn(b'RIFE',data.getvalue())
 def spec(self):return m.PlateSpec(source='test.mp4',frames=176,fps=24000/1001)
 def test_rife_resolves_to_desktop_strategy(self):
  self.assertIs(m.UiJobSelection.resolve('retime',m.Options(retime_mode='rife')),m.RifeRetimeJob)
 def test_regular_and_undo_routes_preserved(self):
  for mode in ['auto','motion','dup','blend','hold']:
   self.assertIs(m.UiJobSelection.resolve('retime',m.Options(retime_mode=mode)),m.RetimeJob)
  self.assertIs(m.UiJobSelection.resolve('revert',m.Options(retime_mode='rife')),m.PlateRevertJob)
 def test_legacy_plate_remains_available(self):
  self.assertIs(m.UiJobSelection.resolve('plate',m.Options()),m.PlateRifeJob)
 def test_unknown_job_rejected(self):
  with self.assertRaises(m.FramingError):m.UiJobSelection.resolve('invalid',m.Options())
 def test_equivalent_length_controls(self):
  for values in [dict(target_frames=352),dict(target_seconds=352/(24000/1001)),dict(speed=.5)]:
   with patch.object(m.RifeRunner,'ready',new_callable=__import__('unittest.mock',fromlist=['PropertyMock']).PropertyMock,return_value=True),patch.object(m.RifeRunner,'_model'):
    self.assertEqual(m.RifeRetimeJob(m.Options(**values),None)._wanted(self.spec()),352)
 def test_too_large_and_too_short_rejected(self):
  for values in [dict(target_frames=6001),dict(target_frames=1),dict(target_seconds=.001),dict(speed=1000),dict(target_seconds=100000)]:
   with self.assertRaises(m.FramingError):m.RifeRetimeJob(m.Options(**values),None)._wanted(self.spec())
 def test_missing_gpu_tool_does_not_report_export_success(self):
  with patch.object(m.RifeRunner,'ready',new_callable=__import__('unittest.mock',fromlist=['PropertyMock']).PropertyMock,return_value=False):
   with self.assertRaisesRegex(m.FramingError,'FFmpeg'):m.RifeRetimeJob(m.Options(target_frames=200),None)._wanted(self.spec())
 def test_shrink_does_not_require_gpu(self):
  with patch.object(m.RifeRunner,'_model',side_effect=AssertionError('Must not invoke GPU')):
   self.assertEqual(m.RifeRetimeJob(m.Options(target_frames=88),None)._wanted(self.spec()),88)
 def test_sequence_rejected_before_processing(self):
  with tempfile.TemporaryDirectory() as name:
   with self.assertRaisesRegex(m.FramingError,'영상 파일'):m.RifeRetimeJob(m.Options(),None).run(Path(name))
 def test_alpha_not_silently_removed(self):
  with tempfile.TemporaryDirectory() as name:
   path=Path(name)/'alpha.mov';path.touch()
   ff=types.SimpleNamespace(probe=lambda p:types.SimpleNamespace(alpha=True))
   with self.assertRaisesRegex(m.FramingError,'알파'):m.RifeRetimeJob(m.Options(),ff).run(path)
 def test_pipeline_passes_only_result_movie(self):
  with tempfile.TemporaryDirectory() as name:
   path=Path(name)/'input.mp4';path.touch();movie=Path(name)/'out.mov'
   ff=types.SimpleNamespace(probe=lambda p:types.SimpleNamespace(alpha=False))
   result=m.JobResult(path,ok=True,outputs=[movie,Path(name)/'rife.json',Path(name)/'note.txt'])
   with patch.object(m.PlateRifeJob,'run',return_value=result):
    self.assertEqual(m.RifeRetimeJob(m.Options(),ff).run(path).next_paths,[str(movie)])
 def test_ui_migration_and_selection(self):
  if not shutil.which('node'):self.skipTest('Node.js required')
  classes='class RetimeWorkflow {'+m.UI_HTML.split('class RetimeWorkflow {',1)[1].split('const CARDS =',1)[0]
  recipe='class Recipe {'+m.UI_HTML.split('class Recipe {',1)[1].split('class ',1)[0]
  code="const assert=require('node:assert/strict');const t=x=>x;\n"+classes+recipe+'''
const defaults={retimeWay:'stretch',retimeMode:'auto',lengthMode:'seconds',targetFrames:264,targetSeconds:11,delivery:'bundle'};
const legacy={key:'plate',options:{targetFrames:200,lengthMode:'seconds',rifeProgram:'custom.exe',plateNoSnap:true}};
const migrated=RetimeWorkflow.restore(legacy,defaults);
assert.equal(migrated.key,'retime');assert.equal(migrated.options.retimeMode,'rife');assert.equal(migrated.options.lengthMode,'frames');
assert.equal(migrated.options.targetFrames,200);assert.equal(legacy.key,'plate');
assert.deepEqual(RetimeWorkflow.restore(migrated,defaults),migrated);
assert.equal(OptionInput.validate(migrated,{video:1}),null);
assert.ok(OptionInput.validate(migrated,{video:1,sequence:1}));
assert.ok(OptionInput.validate({...migrated,options:{...migrated.options,targetFrames:1}},{video:1}));
assert.equal(RetimeWorkflow.isRife({...migrated.options,retimeWay:'undo'}),false);
const recipe=new Recipe(defaults);recipe.steps=[{key:'retime',options:{retimeMode:'motion'}},migrated];recipe.editing=1;
assert.equal(recipe.optionsFor('retime').noSnap,true);
assert.equal(RetimeWorkflow.restore({key:'retime',options:{noSnap:false}},{...defaults,noSnap:true}).options.noSnap,false);
assert.match(RetimeWorkflow.description('auto',{noSnap:true}),/원본 색/);
recipe.dropAt(1);assert.equal(recipe.steps.length,1);assert.equal(recipe.steps[0].options.retimeMode,'motion');
assert.match(RetimeWorkflow.description('motion'),/CPU/);assert.match(RetimeWorkflow.description('rife'),/GPU/);
'''
  # Only one visible retime card; RIFE is a method inside it.
  cards=m.UI_HTML.split('const CARDS =',1)[1].split('];',1)[0]
  self.assertNotIn("key: 'plate'",cards);self.assertEqual(cards.count("key: 'retime'"),1)
  with tempfile.TemporaryDirectory() as name:
   script=Path(name)/'ui.js';script.write_text(code,encoding='utf-8')
   run=subprocess.run(['node',str(script)],capture_output=True,text=True,encoding='utf-8')
   self.assertEqual(run.returncode,0,run.stderr)

if __name__=='__main__':unittest.main(verbosity=2)
