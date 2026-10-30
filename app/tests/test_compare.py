"""Real FFmpeg coverage for comparison audio, video and proxy output."""
from pathlib import Path
import sys,types,unittest,tempfile,subprocess,json,shutil
import numpy as np
APP=Path(__file__).resolve().parents[1]/'framing'/'core.py'
m=types.ModuleType('compare_test');m.__file__=str(APP);sys.modules[m.__name__]=m
exec(compile(APP.read_text(encoding='utf-8'),str(APP),'exec'),m.__dict__)

class CompareTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.ff=m.FFmpegTool(False)
  for name,hz in [('a',440),('b',880),('silent',0)]:
   args=['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=96x64:rate=24000/1001:duration=1.001']
   if hz:args+=['-f','lavfi','-i',f'sine=frequency={hz}:sample_rate=48000:duration=0.7']
   args+=['-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(cls.root/f'{name}.mp4')]
   subprocess.run(args,check=True,capture_output=True)
 @classmethod
 def tearDownClass(cls):cls.temp.cleanup()
 def probe(self,path):
  return json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))['streams']
 def sound(self,path):
  raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-map','0:a:0','-ac','1','-ar','48000','-f','f32le','-'])
  samples=np.frombuffer(raw,dtype='<f4');window=samples[4800:24000]
  hz=np.argmax(abs(np.fft.rfft(window)))*48000/len(window)
  return hz,float(np.sqrt(np.mean(window**2))),len(samples)/48000
 def export(self,source='a',swapped=False,mode='wipe',first='a',second='b',volume=1,region=None,format='video'):
  facts=dict(a=str(self.root/f'{first}.mp4'),b=str(self.root/f'{second}.mp4'),video=True,width=96,height=64,fps=24000/1001,seconds=1.001)
  state=dict(mode=mode,audioSource=source,audioVolume=volume,swapped=swapped,region=region)
  return m.CompareExporter(self.ff,facts,state,{'format':format}).save()
 def test_audio_source_survives_visual_swap(self):
  for source,hz in [('a',440),('b',880)]:
   for swapped in [False,True]:
    with self.subTest(source=source,swapped=swapped):
     _,files,_=self.export(source,swapped);freq,rms,seconds=self.sound(files[0])
     self.assertAlmostEqual(freq,hz,delta=3);self.assertGreater(rms,.02);self.assertAlmostEqual(seconds,1.001,delta=.025)
 def test_all_six_modes_keep_video_frames_and_audio(self):
  for mode in m.CompareExporter.MODES:
   with self.subTest(mode=mode):
    _,files,_=self.export(mode=mode);streams=self.probe(files[0]);v=next(s for s in streams if s['codec_type']=='video')
    self.assertEqual(int(v['nb_frames']),24);self.assertEqual(v['pix_fmt'],'yuv420p')
    self.assertAlmostEqual(float(v['duration']),1.001,places=5);self.assertTrue(any(s['codec_type']=='audio' for s in streams))
 def test_auto_falls_back_to_b(self):
  _,files,_=self.export(source='auto',first='silent');self.assertAlmostEqual(self.sound(files[0])[0],880,delta=3)
 def test_no_audio_inputs(self):
  _,files,_=self.export(source='auto',first='silent',second='silent');self.assertFalse(any(s['codec_type']=='audio' for s in self.probe(files[0])))
 def test_mute_is_respected(self):
  for source,level in [('none',1),('a',0)]:
   _,files,_=self.export(source=source,volume=level);self.assertFalse(any(s['codec_type']=='audio' for s in self.probe(files[0])))
 def test_volume_is_exported(self):
  _,full,_=self.export();_,half,_=self.export(volume=.5)
  self.assertAlmostEqual(self.sound(half[0])[1]/self.sound(full[0])[1],.5,delta=.025)
 def test_invalid_or_missing_audio_rejected(self):
  for state in [dict(audioSource='x'),dict(audioVolume=float('nan')),dict(audioVolume=-1),dict(audioSource='a')]:
   with self.assertRaises(m.ShotkitError):m.CompareAudioPolicy(self.ff,self.root/'silent.mp4',self.root/'b.mp4',state)
 def test_odd_selection_is_playable_and_png_exact(self):
  region=dict(x=1,y=1,width=31,height=29)
  _,files,_=self.export(region=region);v=next(s for s in self.probe(files[0]) if s['codec_type']=='video')
  self.assertEqual((v['width'],v['height'],v['pix_fmt']),(32,30,'yuv420p'))
  _,files,_=self.export(region=region,format='frame');info=self.ff.probe(files[0]);self.assertEqual((info.width,info.height),(31,29))
 def test_frames_and_both(self):
  for kind,count in [('frames',24),('both',25)]:
   _,files,manifest=self.export(format=kind);self.assertEqual(len(files),count);self.assertEqual(json.loads(manifest.read_text())['status'],'complete')
 def test_proxy_preserves_sound_in_each_fallback(self):
  maker=m.PlayableProxy(self.ff,self.root)
  for destination,args in maker._plans(self.root/'a.mp4',self.ff.probe(self.root/'a.mp4'),True,'view'):
   self.assertEqual(self.ff.run(args,quiet=True),0,self.ff.why_failed())
   self.assertAlmostEqual(self.sound(destination)[0],440,delta=3)
 def test_proxy_rejects_unplayable_pixel_or_audio_format(self):
  for info in [m.ProbeInfo(vcodec='h264',pix_fmt='yuv444p'),m.ProbeInfo(vcodec='h264',pix_fmt='yuv420p',acodec='ac3')]:
   self.assertTrue(m.PlayableProxy.why(Path('test.mp4'),info,True))
 def test_saved_viewer_embeds_audio_availability(self):
  page=m.ComparePage('a.mp4','b.mp4',True,audio=(False,True)).render()
  self.assertIn('this.available=[false, true]',page);self.assertNotIn('__AUDIO__',page)

if __name__=='__main__':unittest.main(verbosity=2)
