"""Run from inside the frozen .app; failure blocks packaging."""
import json, platform, tempfile, traceback
from pathlib import Path
from framing import core
from mac_app import BundledFFmpeg

def run(paths,destination,check_cocoa=True):
    report={'platform':platform.platform(),'architecture':platform.machine(),'checks':{}}
    try:
        if check_cocoa:
            import webview,webview.platforms.cocoa,AppKit,WebKit
        report['checks']['cocoa_import']=check_cocoa
        ff=BundledFFmpeg(paths)
        with tempfile.TemporaryDirectory(prefix='Framing 한글 검사 ') as folder:
            root=Path(folder);movie=root/'테스트 영상.mp4'
            assert ff.run(['-y','-f','lavfi','-i','testsrc2=size=128x72:rate=24:duration=0.5',
                '-f','lavfi','-i','sine=frequency=440:duration=0.5','-c:v','libx264','-pix_fmt','yuv420p',
                '-c:a','aac','-shortest',str(movie)],quiet=True)==0,ff.why_failed()
            info=ff.probe(movie);assert info.frames==12 and info.acodec=='aac'
            report['checks']['bundled_ffmpeg_audio_unicode']=True
            facts=dict(a=str(movie),b=str(movie),width=128,height=72,fps=24,seconds=.5,video=True)
            _,files,_=core.CompareExporter(ff,facts,{'mode':'side','audioSource':'b'},{'format':'video'}).save()
            out=next(p for p in files if p.suffix=='.mp4');info=ff.probe(out)
            assert info.frames==12 and info.acodec=='aac' and info.width==256
            report['checks']['compare_video_audio']=True
            for mode in ('dup','motion'):
                destination_dir=root/mode;destination_dir.mkdir()
                opt=core.Options(retime_mode=mode,target_frames=18,delivery='both',retime_note=True,no_snap=True,out_dir=destination_dir)
                result=core.UiJobSelection.resolve('retime',opt)(opt,ff).run(movie)
                assert result.ok,result.message
                out=next(p for p in result.outputs if p.suffix=='.mp4')
                note=next(p for p in result.outputs if p.suffix=='.txt')
                assert ff.probe(out).frames==18
                undo=root/('undo_'+mode);undo.mkdir()
                restored=core.PlateRevertJob(core.Options(stretch_note=str(note),out_dir=undo),ff).run(out)
                assert restored.ok and ff.probe(restored.outputs[0]).frames==12
                report['checks'][mode+'_18_to_12_restore']=True
            for tool,extra in [('extract',{'count':3}),('cuts',{'cut_mode':'interval','cut_interval':.25}),
                               ('reverse',{}),('audit',{}),('convert',{'convert_to':'gif'})]:
                dest=root/tool;dest.mkdir()
                opt=core.Options(out_dir=dest,**extra)
                result=core.UiJobSelection.resolve(tool,opt)(opt,ff).run(movie)
                assert result.ok,result.message
                assert result.outputs and all(p.exists() for p in result.outputs)
                report['checks'][tool]=True
            contact=root/'contact';contact.mkdir()
            result=core.ContactSheetJob(core.Options(out_dir=contact),ff).run_batch([movie])
            assert result.ok,result.message
            report['checks']['contact']=True
            new=movie.with_name('새 이름.mp4');history=core.RenameHistory()
            history.apply([(movie,new)]);assert new.exists();history.undo();assert movie.exists()
            report['checks']['rename_undo']=True
        report['ok']=True
    except Exception:report.update(ok=False,error=traceback.format_exc())
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['ok'] else 1
