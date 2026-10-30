"""Hosted SHOTKIT contracts. No browser-supplied filesystem paths cross this layer."""
from __future__ import annotations
import math, re
from pathlib import Path

VIDEO={'.mp4','.mov','.mkv','.webm','.m4v','.avi'}
IMAGE={'.png','.jpg','.jpeg','.webp','.tif','.tiff','.exr','.dpx'}
NOTES={'.txt','.json'}
TOOLS={'compare','extract','retime','revert','cuts','reverse','audit','contact','convert','rename'}

def filename(value):
    name=str(value).replace('\\','/').split('/')[-1]
    name=re.sub(r'[\x00-\x1f<>:"|?*]', '_', name).strip(' .')[:150]
    if not name or name.startswith('.') or name.rsplit('.',1)[0].upper() in {'CON','PRN','AUX','NUL','COM1','LPT1'}:
        raise ValueError('파일 이름을 확인해 주세요.')
    if Path(name).suffix.lower() not in VIDEO|IMAGE|NOTES:
        raise ValueError('영상·이미지 또는 SHOTKIT 리타임 메모를 올려 주세요.')
    return name

def options(tool, raw, rife_ready=False):
    if tool not in TOOLS or not isinstance(raw,dict):raise ValueError('지원하지 않는 작업입니다.')
    result={}
    def choice(key, default, values):
        value=raw.get(key,default)
        if value not in values:raise ValueError(f'{key}: 선택값을 확인해 주세요.')
        result[key]=value
    def number(key,default,lo,hi,integer=False):
        value=raw.get(key,default)
        if isinstance(value,bool):raise ValueError('숫자를 입력해 주세요.')
        try:value=float(value)
        except (TypeError,ValueError):raise ValueError('숫자를 입력해 주세요.')
        if not math.isfinite(value) or not lo<=value<=hi or (integer and int(value)!=value):
            raise ValueError(f'{key}: {lo}~{hi} 범위로 입력해 주세요.')
        result[key]=int(value) if integer else value
    def text(key,default='',length=200):
        value=raw.get(key,default)
        if not isinstance(value,str) or len(value)>length or '\x00' in value:raise ValueError('입력한 글자를 확인해 주세요.')
        result[key]=value
    if tool=='compare':
        choice('mode','side',['wipe','overlay','flicker','side','a','b'])
        choice('audio','a',['a','b','none']);choice('format','video',['video','frame','frames'])
        choice('views','current',['current','all']);number('time',0,0,120)
        number('split',.5,0,1);number('mix',.5,0,1)
    elif tool=='extract':
        choice('pick','count',['count','all','at']);number('count',5,1,100,True)
        choice('frame_format','png',['png','jpg','exr','dpx']);text('at','first middle last')
        if result['pick']!='all' and result['frame_format'] in ['exr','dpx']:raise ValueError('EXR·DPX는 전체 프레임 추출에서 선택하세요.')
    elif tool=='retime':
        choice('retime_mode','motion',['motion','rife','dup','blend','hold'])
        number('target_frames',264,1,1200,True)
        if result['retime_mode']=='rife' and not rife_ready:raise ValueError('이 서버에는 RIFE GPU가 준비되지 않았습니다. 다른 보간을 선택하세요.')
    elif tool=='cuts':
        choice('cut_mode','interval',['interval','auto','manual']);number('cut_interval',3,.25,120)
        number('cut_threshold',10,1,100);text('cut_times','3,6')
    elif tool=='convert':
        choice('convert_to','mp4',['mp4','mov','small','gif','image','movie'])
        choice('image_format','png',['png','jpg','webp','exr','dpx']);number('fps',24,1,120)
        number('gif_width',640,64,1280,True);number('gif_fps',12,1,30,True)
    elif tool=='rename':
        choice('style','number',['number','replace','affix']);text('name','SHOT',80)
        text('find');text('replace');text('prefix','',80);text('suffix','',80)
        number('start',1,0,999999,True);number('width',3,1,8,True)
        for key in ['name','prefix','suffix','replace']:
            if any(c in result[key] for c in '/\\<>:"|?*'):raise ValueError('이름에는 경로 기호를 쓸 수 없습니다.')
        if result['style']=='number' and not result['name'].strip():raise ValueError('새 이름을 입력하세요.')
        if result['style']=='replace' and not result['find']:raise ValueError('찾을 글자를 입력하세요.')
    elif tool=='audit':number('audit_freeze_seconds',.5,.1,30)
    unknown=set(raw)-set(result)
    if unknown:raise ValueError('허용되지 않은 설정입니다: '+', '.join(sorted(unknown)))
    return result

def rename_rules(o):
    if o['style']=='number':return [{'kind':'clear'},{'kind':'prefix','text':o['name']+'_'},{'kind':'number','start':o['start'],'width':o['width'],'step':1,'position':'suffix'}]
    if o['style']=='replace':return [{'kind':'replace','find':o['find'],'text':o['replace']}]
    return [{'kind':'prefix','text':o['prefix']},{'kind':'suffix','text':o['suffix']}]
