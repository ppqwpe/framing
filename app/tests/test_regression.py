from pathlib import Path
import sys,tempfile
ROOT=Path(tempfile.gettempdir())/'framing_regression'
(ROOT/'work').mkdir(parents=True,exist_ok=True)
def load_app():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from framing import core as module
    return module

import unittest,tempfile
import numpy as np
m=load_app()
class RegressionTests(unittest.TestCase):
 def test_black_freeze_duplicate(self):
  audit=m.FrameAudit(24,.5)
  frames=[np.full((180,320,3),v,np.uint8) for v in range(40,140,10)]
  frames += [np.full((180,320,3),180,np.uint8)]*12
  frames += [np.zeros((180,320,3),np.uint8)]*4
  frames += [np.full((180,320,3),v,np.uint8) for v in range(50,150,10)]
  for f in frames:audit.feed(f)
  rows=m.AuditReport('fixture',audit).as_dict()['findings']
  self.assertTrue(any(x['kind']=='freeze' and (x['first'],x['last'])==(11,22) for x in rows))
  self.assertTrue(any(x['kind']=='black' and (x['first'],x['last'])==(23,26) for x in rows))
  self.assertTrue(any(x['kind']=='duplicate' and (x['first'],x['last'])==(11,22) for x in rows))
  for row in rows:self.assertIn(f"{row['first']}–{row['last']}",row['message'])
 def test_audit_path_matches_first(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   folder=Path(temp)
   for i in range(4):m.ImageIO.write(folder/f'frame{i+1:04}.png',np.zeros((180,320,3),np.uint8))
   result=m.AuditJob(m.Options(out_dir=folder/'report'),m.FFmpegTool(False)).run(folder)
   rows=result.inspection['findings']
   self.assertTrue(rows)
   for row in rows:self.assertEqual(row['path'],str(folder/'frame0001.png'))
 def test_sequence_missing(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   folder=Path(temp)
   for n in [1,2,5]:m.ImageIO.write(folder/f'a{n:04}.png',np.full((20,20,3),n*30,np.uint8))
   inventory=m.SequenceInventory(folder)
   self.assertEqual(len(inventory.findings),1);self.assertIn('3–4',inventory.findings[0].message)
 def test_tiny_change_not_duplicate(self):
  a=np.ones((180,320,3),np.uint8)*100;b=a.copy();b[50,50]=200
  audit=m.FrameAudit(24,.5);audit.feed(a);audit.feed(b)
  self.assertFalse(any(x.kind=='duplicate' for x in audit.finish()))
 def test_rename_swap_and_undo(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   a,b=Path(temp)/'a.txt',Path(temp)/'b.txt';a.write_text('A');b.write_text('B')
   history=m.RenameHistory();history.apply([(a,b),(b,a)])
   self.assertEqual((a.read_text(),b.read_text()),('B','A'));history.undo()
   self.assertEqual((a.read_text(),b.read_text()),('A','B'))
 def test_rename_stale_preview(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   a,b=Path(temp)/'a.txt',Path(temp)/'b.txt';a.write_text('A')
   expected={str(a):m.RenameHistory.stamp(a)};a.write_text('CHANGED')
   with self.assertRaises(m.FramingError):m.RenameHistory().apply([(a,b)],expected)
   self.assertTrue(a.exists());self.assertFalse(b.exists())
 def test_project_roundtrip(self):
  data={'format':'framing-project','version':1,'paths':[str(ROOT/'work/media/proxy.mp4')],'steps':[{'key':'audit','options':{}}]}
  self.assertEqual(m.ProjectDocument.loads(m.ProjectDocument.dumps(data))['steps'],data['steps'])
 def test_rife_cli_grid(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   folder=Path(temp)
   for n in range(5):(folder/f'{n:06}.png').write_bytes(b'test')
   for multiplier in (2,4,8):
    command=m.RifeRunner().command(folder,folder/'out',multiplier)
    self.assertEqual(int(command[command.index('-n')+1]),5*multiplier)
    self.assertEqual(command[command.index('-m')+1],'rife-v4.6')
    grid=m.DenseGrid(5,7,multiplier=multiplier,snap=False)
    self.assertEqual(grid.dense_count,(5-1)*multiplier+1)
 def test_cancel_before_render(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   source=Path(temp)/'frames';source.mkdir()
   m.ImageIO.write(source/'f0001.png',np.zeros((32,32,3),np.uint8))
   m.ImageIO.write(source/'f0002.png',np.ones((32,32,3),np.uint8)*100)
   job=m.RetimeJob(m.Options(out_dir=Path(temp)/'result',target_frames=4),m.FFmpegTool(False))
   job.should_stop=lambda:True
   with self.assertRaisesRegex(m.FramingError,'중단'):job.run(source)
 def test_corrupt_image(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   path=Path(temp)/'bad.png';path.write_bytes(b'')
   self.assertIsNone(m.ImageIO.read(path))
class TimingTests(unittest.TestCase):
 def test_fractional_rate_roundtrip(self):
  rate=m.FrameRate.from_value(24000/1001)
  self.assertEqual(rate.text,'24000/1001')
  self.assertEqual(m.FrameRate.frame_count(7.340667,rate.value),176)
 def test_partial_frame_kept(self):
  self.assertEqual(m.FrameRate.frame_count(1.01,24),25)
 def test_invalid_timing(self):
  for value in [float('nan'),float('inf'),-1]:
   with self.assertRaises(m.FramingError):m.FrameRate.frame_count(value,24)
 def test_real_image_encoder(self):
  from types import SimpleNamespace
  for fmt in ['exr','dpx']:
   args,ext,_,_=m.ImageTarget().plan(SimpleNamespace(_opt=m.Options(image_format=fmt)),Path('x.png'),None,m.ColorProfile())
   self.assertEqual(ext,'.'+fmt);self.assertIn(fmt,args)
 def test_unknown_image_format_rejected(self):
  from types import SimpleNamespace
  with self.assertRaises(m.FramingError):m.ImageTarget().plan(SimpleNamespace(_opt=m.Options(image_format='bad')),Path('x.png'),None,m.ColorProfile())
 def test_rife_model_missing(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   exe=Path(temp)/'rife.exe';exe.write_bytes(b'fake')
   with self.assertRaises(m.FramingError):m.RifeRunner(str(exe))._model()

class RestoreTests(unittest.TestCase):
 def test_motion_stretch_preserves_all_source_frames(self):
  plan=m.RetimePlan.decide(176,24000/1001,frames=200);report=m.RetimeReport(plan,'motion')
  job=m.RetimeJob(m.Options(),m.FFmpegTool(False));note=job._stretch_note(Path('x.mp4'),plan,report,m.ColorProfile())
  self.assertFalse(note.restoration_problem)
  self.assertFalse(m.PlateRevert(Path('x.mp4'),note).complaints(200))
  self.assertEqual(note.added,24)
 def test_hold_can_restore(self):
  spec=m.PlateSpec(source='x',frames=3,fps=24)
  note=m.StretchNote(spec,5,[0,1,2,2,2],[3,4]);self.assertFalse(note.restoration_problem)
 def test_generated_text_restores_all_source_frames(self):
  plan=m.RetimePlan.decide(176,24,frames=200);report=m.RetimeReport(plan,'motion')
  with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
   path=Path(temp)/'note.txt';path.write_text(m.RetimeNote(report,Path('x.mp4')).text(),encoding='utf-8')
   note=m.RetimeNoteText(path).note(m.PlateSpec(source='x',frames=200,fps=24),200)
   self.assertEqual(len(note.kept),176)
   self.assertEqual(note.added,24)
 def test_explicit_drop_supported(self):
  spec=m.PlateSpec(source='x',frames=5,fps=24)
  note=m.DropSpec('tail:2').note(spec,5);self.assertFalse(note.restoration_problem);self.assertEqual(note.kept,[0,1,2])

if __name__=='__main__':unittest.main(verbosity=2)
