"""Source-preserving frame placement and exact undo-note boundaries."""
from pathlib import Path
import sys,types,unittest,tempfile
import numpy as np
APP=Path(__file__).resolve().parents[1]/'framing'/'core.py'
m=types.ModuleType('preservation_test');m.__file__=str(APP);sys.modules[m.__name__]=m
exec(compile(APP.read_text(encoding='utf-8'),str(APP),'exec'),m.__dict__)

class PreservationTests(unittest.TestCase):
 def test_requested_153_to_264_records_111_only(self):
  plan=m.RetimePlan(153,264,24)
  for mode in ['motion','blend','dup','hold']:
   report=m.RetimeReport(plan,mode)
   self.assertEqual(sum(report.added()),111,mode)
   note=m.RetimeJob(m.Options(),m.FFmpegTool(False))._stretch_note(Path('source.mp4'),plan,report,m.ColorProfile())
   self.assertFalse(note.restoration_problem)
   self.assertEqual([note.origins[i] for i in note.kept],list(range(153)))
 def test_rife_153_to_264_keeps_all_originals(self):
  picks=m.DenseGrid(153,264).picks()
  self.assertEqual([p.source_index for p in picks if p.from_plate],list(range(153)))
  self.assertEqual(sum(not p.from_plate for p in picks),111)
  self.assertEqual(len({p.dense_index for p in picks}),264)
 def test_all_small_stretch_ratios(self):
  for source in range(2,27):
   for target in range(source,source*8+1):
    timeline=m.RetimeTimeline(source,target)
    self.assertEqual(len(set(timeline.anchors)),source)
    self.assertEqual([timeline.at(slot) for slot in timeline.anchors],list(range(source)))
    picks=m.DenseGrid(source,target).picks()
    self.assertEqual([p.source_index for p in picks if p.from_plate],list(range(source)))
    self.assertEqual(sum(not p.from_plate for p in picks),target-source)
    self.assertTrue(all(a.dense_index<=b.dense_index for a,b in zip(picks,picks[1:])))
 def test_blend_copies_source_pixels_without_snap(self):
  frames=[np.full((4,4,3),i*7,np.uint8) for i in range(10)]
  plan=m.RetimePlan(10,17,24);out=[]
  # Even when matte correction is active, source anchors must be unchanged.
  snapper=types.SimpleNamespace(apply=lambda x:np.zeros_like(x))
  m.BlendEngine(plan,snapper).render(frames,lambda i,f:out.append(f.copy()))
  for i,slot in enumerate(m.RetimeTimeline(10,17).anchors):self.assertTrue(np.array_equal(frames[i],out[slot]))
 def test_original_adjacent_duplicates_are_not_new_frames(self):
  report=m.RetimeReport(m.RetimePlan(153,264,24),'motion',[0]*153)
  self.assertEqual(sum(report.added()),111)
 def test_one_frame_stretch_note(self):
  report=m.RetimeReport(m.RetimePlan(1,5,24),'blend')
  self.assertEqual(report.added(),[False,True,True,True,True])
 def test_old_nonrestorable_note_remains_rejected(self):
  old=m.StretchNote(m.PlateSpec(source='old',frames=153,fps=24),264,[0]+[-1]*262+[152])
  self.assertTrue(old.restoration_problem)
  with tempfile.TemporaryDirectory() as name:
   note=Path(name)/'old.txt'
   note.write_text('FRAMING_TIMELINE source=153 output=264 restorable=0\n'+', '.join(map(str,range(2,264))),encoding='utf-8')
   with self.assertRaises(m.FramingError):m.RetimeNoteText(note).note(m.PlateSpec(source='old',frames=264,fps=24),264)
 def test_new_text_roundtrip_exact_slot_list(self):
  plan=m.RetimePlan(153,264,24);report=m.RetimeReport(plan,'motion')
  with tempfile.TemporaryDirectory() as name:
   note=Path(name)/'new.txt';note.write_text(m.RetimeNote(report,Path('source.mp4')).text(),encoding='utf-8')
   text=note.read_text(encoding='utf-8');self.assertIn('restorable=1',text)
   parsed=m.RetimeNoteText(note).note(m.PlateSpec(source='source',frames=264,fps=24),264)
   self.assertEqual(parsed.kept,m.RetimeTimeline(153,264).anchors)
 def test_shrinking_does_not_promise_restoration(self):
  plan=m.RetimePlan(153,100,24);report=m.RetimeReport(plan,'motion')
  self.assertEqual(report.at(50),50*152/99)
  self.assertIsNone(m.RetimeJob(m.Options(),None)._stretch_note(Path('x'),plan,report,m.ColorProfile()))

if __name__=='__main__':unittest.main(verbosity=2)
