"""One isolated process per job; desktop UI and local-file RPC are never exposed."""
import json, os, shutil, sys, traceback, zipfile
from pathlib import Path
import shotkit_core as core
from domain import IMAGE, NOTES, VIDEO, rename_rules

def execute(request_path):
    request_path=Path(request_path).resolve(); root=request_path.parent
    request=json.loads(request_path.read_text(encoding='utf-8'))
    output=root/'output'; output.mkdir(exist_ok=True)
    inputs=root/'inputs';inputs.mkdir(exist_ok=True)
    selected=[]
    for i,entry in enumerate(request['files']):
        folder=inputs/str(i);folder.mkdir(exist_ok=True)
        target=folder/entry['name'];shutil.copy2(entry['path'],target);selected.append(target)
    ff=core.FFmpegTool(False);tool=request['tool'];o=request['options']; deliver=[]
    media=[p for p in selected if p.suffix.lower() not in NOTES]
    for p in media:
        # Reject playlists disguised as videos before any decoder sees them.
        with p.open('rb') as source:header=source.read(32)
        if p.suffix.lower() in VIDEO:
            valid=(header[4:8] in [b'ftyp',b'moov',b'mdat',b'wide',b'free'] or header[:4]==b'\x1aE\xdf\xa3' or (header[:4]==b'RIFF' and header[8:12]==b'AVI '))
            if not valid:raise ValueError('지원하는 영상 컨테이너가 아닙니다: '+p.name)
        else:
            valid=(header.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'II*\x00',b'MM\x00*',b'II+\x00',b'MM\x00+',b'\x76\x2f\x31\x01',b'SDPX',b'XPDS')) or (header[:4]==b'RIFF' and header[8:12]==b'WEBP'))
            if not valid:raise ValueError('지원하는 이미지 형식이 아닙니다: '+p.name)
        info=ff.probe(p)
        if not info or not info.width or not info.height:raise ValueError('읽을 수 없는 영상·이미지입니다: '+p.name)
        if info.width*info.height>3840*2160 or (p.suffix.lower() in VIDEO and (info.duration>120 or info.frames>3600)):
            raise ValueError('웹 버전은 4K 이하·2분 이하·3600프레임 이하의 소재를 지원합니다.')
    if tool=='rename':
        for p in selected:
            if (output/p.name).exists():raise ValueError('같은 이름의 파일이 있습니다. 하나씩 선택해 주세요.')
            shutil.copy2(p,output/p.name)
        plan=core.BatchNamePlan([output/p.name for p in selected],rename_rules(o));rows=plan.preview()
        history=core.RenameHistory();history.apply([(Path(r['path']),Path(r['destination'])) for r in rows if r['path']!=r['destination']])
        (output/'이름변경.json').write_text(json.dumps([{'before':Path(r['path']).name,'after':Path(r['destination']).name} for r in rows],ensure_ascii=False,indent=2),encoding='utf-8')
        deliver=list(output.iterdir())
    elif tool=='compare':
        if len(media)!=2:raise ValueError('대조할 영상 두 개 또는 이미지 두 개를 선택하세요.')
        a,b=media;video=a.suffix.lower() in VIDEO
        if video!=(b.suffix.lower() in VIDEO):raise ValueError('영상끼리 또는 이미지끼리 선택하세요.')
        info=ff.probe(a);other=ff.probe(b)
        facts=dict(a=str(a),b=str(b),video=video,width=info.width,height=info.height,fps=info.fps or 24,seconds=min(info.video_duration,other.video_duration) if video else 0)
        state=dict(mode=o['mode'],audioSource=o['audio'],time=o['time'],split=o['split'],mix=o['mix'])
        folder,files,_=core.CompareExporter(ff,facts,state,dict(format=o['format'] if video else 'frame',views=o['views'])).save()
        dest=output/'대조';shutil.move(str(folder),dest);deliver=[dest/p.relative_to(folder) for p in files]
    else:
        opts={k:v for k,v in o.items() if k not in ['pick','count','at']}
        if tool=='extract':
            opts['at']='all' if o['pick']=='all' else o['at'] if o['pick']=='at' else ''
            opts['count']=o['count'] if o['pick']=='count' else 0
        if tool=='retime':opts.update(no_snap=True,retime_note=True,delivery='both',rife_program=os.environ.get('SHOTKIT_RIFE_PROGRAM',''))
        if tool=='reverse':opts['delivery']='mp4'
        if tool=='revert':
            notes=[p for p in selected if p.suffix.lower() in NOTES]
            if len(media)!=1 or len(notes)>1:raise ValueError('리타임 결과 영상 한 개와 메모 한 개를 선택하세요. 내장 메모가 있으면 영상만 선택해도 됩니다.')
            opts['stretch_note']=str(notes[0]) if notes else ''
        if not media:raise ValueError('처리할 영상 또는 이미지를 선택하세요.')
        if tool=='convert' and o['convert_to']=='movie':
            if any(p.suffix.lower() not in IMAGE for p in media):raise ValueError('영상으로 묶을 이미지들을 선택하세요.')
            sequence=inputs/'sequence';sequence.mkdir()
            for i,p in enumerate(sorted(media,key=lambda p:p.name)):
                # Decode once to normalize arbitrary input names and mixed extensions.
                core.ImageIO.write(sequence/f'frame.{i+1:06d}.png',core.ImageIO.read(p,-1))
            media=[sequence]
        if tool in ['retime','revert','cuts','reverse','extract'] and any(not p.is_dir() and p.suffix.lower() not in VIDEO for p in media):
            raise ValueError('이 작업에는 영상 파일을 선택하세요.')
        if tool=='contact':
            result=core.ContactSheetJob(core.Options(out_dir=output),ff).run_batch(media)
            if not result.ok:raise ValueError(result.message)
            deliver+=result.outputs
        else:
            for index,p in enumerate(media):
                dest=output/f'{index+1:02d}_{p.stem}';dest.mkdir()
                opt=core.Options(out_dir=dest,**opts)
                result=core.UiJobSelection.resolve(tool,opt)(opt,ff).run(p)
                if not result.ok:raise ValueError(result.message)
                deliver+=result.outputs
    made=[]
    for p in deliver:
        p=Path(p).resolve()
        if p.is_file() and p.is_relative_to(output.resolve()):made.append(p)
    made=list(dict.fromkeys(made));assert made,'저장된 결과가 없습니다.'
    # Keep job bundles deterministic and free of private absolute source paths.
    for p in output.rglob('*'):
        if p.is_file() and p.suffix.lower() in ['.json','.txt','.csv','.html']:
            s=p.read_text(encoding='utf-8-sig',errors='replace')
            s=s.replace(str(root),'[작업]').replace(str(root).replace('\\','\\\\'),'[작업]')
            p.write_text(s,encoding='utf-8')
    bundle=output/'SHOTKIT_결과.zip'
    with zipfile.ZipFile(bundle,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
        for p in sorted(output.rglob('*')):
            if p.is_file() and p!=bundle:z.write(p,p.relative_to(output).as_posix())
    made.append(bundle)
    (root/'result.json').write_text(json.dumps({'files':[p.relative_to(output).as_posix() for p in made]},ensure_ascii=False),encoding='utf-8')

if __name__=='__main__':
    try:execute(sys.argv[1])
    except Exception as exc:
        traceback.print_exc()
        (Path(sys.argv[1]).parent/'error.txt').write_text(str(exc),encoding='utf-8')
        sys.exit(1)
