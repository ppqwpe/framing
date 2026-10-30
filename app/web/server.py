"""Single-process, password-protected Framing web service."""
import asyncio, hashlib, hmac, json, os, secrets, shutil, signal, subprocess, sys, time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from domain import filename, options

BASE=Path(__file__).resolve().parent
DATA=Path(os.environ.get('FRAMING_DATA', BASE/'data')).resolve()
PASSWORD=os.environ.get('FRAMING_ACCESS_PASSWORD','')
SECURE=os.environ.get('FRAMING_HTTPS','0')=='1'
LIMIT=512*1024*1024
sessions={}; jobs={}; attempts={}; gate=asyncio.Semaphore(1)
def rife_ready():
    p=Path(os.environ.get('FRAMING_RIFE_PROGRAM',''))
    return p.is_file() and any((p.parent/n).is_dir() for n in ['rife-v4.6','rife-v4'])
def stop(p):
    if p and p.poll() is None:
        if os.name=='nt':subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],capture_output=True)
        else:os.killpg(p.pid,signal.SIGKILL)
async def cleanup():
    while True:
        await asyncio.sleep(60)
        for key,s in list(sessions.items()):
            if time.time()-s['seen']>86400 and not any(j['owner']==key and j['status'] in ['queued','running'] for j in jobs.values()):
                sessions.pop(key,None);shutil.rmtree(s['root'],ignore_errors=True)
                for jid in [i for i,j in jobs.items() if j['owner']==key]:jobs.pop(jid,None)
        live={s['root'] for s in sessions.values()}
        for p in DATA.iterdir():
            if p.is_dir() and p not in live and time.time()-p.stat().st_mtime>86400:shutil.rmtree(p,ignore_errors=True)
        for ip,times in list(attempts.items()):
            if not times or time.time()-times[-1]>300:attempts.pop(ip,None)
@asynccontextmanager
async def lifespan(app):
    if len(PASSWORD)<12:raise RuntimeError('FRAMING_ACCESS_PASSWORD must contain at least 12 characters.')
    DATA.mkdir(parents=True,exist_ok=True)
    # Sessions are intentionally not resumed after a restart. Remove expired data only.
    for p in DATA.iterdir():
        if p.is_dir() and time.time()-p.stat().st_mtime>86400:shutil.rmtree(p)
    task=asyncio.create_task(cleanup())
    yield
    task.cancel()
    for j in jobs.values():stop(j.get('process'))
app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
@app.middleware('http')
async def headers(req,call_next):
    if req.method not in ['GET','HEAD']:
        origin=req.headers.get('origin')
        if origin and origin.rstrip('/')!=str(req.base_url).rstrip('/'):
            return JSONResponse({'detail':'다른 사이트에서 보낸 요청입니다.'},403)
    res=await call_next(req)
    res.headers['X-Content-Type-Options']='nosniff'
    res.headers['Referrer-Policy']='no-referrer'
    res.headers['Cache-Control']='no-store'
    res.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    return res
def session(req,write=False):
    key=req.cookies.get('framing');s=sessions.get(key)
    if not s:raise HTTPException(401,'접속 비밀번호를 입력하세요.')
    if write and not hmac.compare_digest(req.headers.get('x-csrf-token',''),s['csrf']):raise HTTPException(403,'새로고침 후 다시 시도하세요.')
    s['seen']=time.time();return key,s
async def body_json(req):
    data=bytearray()
    async for chunk in req.stream():
        data.extend(chunk)
        if len(data)>16384:raise HTTPException(413,'요청이 너무 큽니다.')
    try:
        value=json.loads(data)
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,TypeError):raise HTTPException(400,'요청 내용을 확인해 주세요.')
@app.post('/api/login')
async def login(req:Request):
    ip=req.client.host; now=time.time();a=[t for t in attempts.get(ip,[]) if now-t<300]
    attempts[ip]=a
    if len(a)>=10:raise HTTPException(429,'5분 후 다시 시도하세요.')
    if int(req.headers.get('content-length','0'))>4096:raise HTTPException(413)
    body=await body_json(req)
    if not hmac.compare_digest(str(body.get('password','')),PASSWORD):
        a.append(now);raise HTTPException(401,'비밀번호가 맞지 않습니다.')
    if len(sessions)>=100:raise HTTPException(503,'작업 공간이 가득 찼습니다. 잠시 후 접속하세요.')
    key=secrets.token_hex(32);root=DATA/key;root.mkdir()
    sessions[key]={'root':root,'csrf':secrets.token_hex(32),'seen':now,'files':{},'uploading':False}
    res=JSONResponse({'ok':True});res.set_cookie('framing',key,httponly=True,secure=SECURE,samesite='strict',max_age=86400);return res
@app.get('/api/session')
async def bootstrap(req:Request):
    key,s=session(req)
    return {'csrf':s['csrf'],'files':list(s['files'].values()),'rife':rife_ready(),'jobs':[{'id':j['id'],'tool':j.get('tool',''),'status':j['status']} for j in jobs.values() if j['owner']==key]}
@app.post('/api/files')
async def upload(req:Request,name:str):
    _,s=session(req,True)
    try:name=filename(name)
    except ValueError as e:raise HTTPException(400,str(e))
    if s['uploading']:raise HTTPException(409,'현재 업로드가 끝난 뒤 다시 올려주세요.')
    if len(s['files'])>=20 or sum(f['size'] for f in s['files'].values())>=2*1024**3:raise HTTPException(413,'한 작업 공간에는 최대 20개·2GB까지 올릴 수 있습니다.')
    if shutil.disk_usage(DATA).free<3*1024**3:raise HTTPException(507,'서버 저장 공간이 부족합니다.')
    ident=secrets.token_hex(16);p=s['root']/ident;p.mkdir();p=p/name;size=0;s['uploading']=True
    try:
        with p.open('wb') as f:
            async for chunk in req.stream():
                size+=len(chunk)
                if size>LIMIT or size+sum(v['size'] for v in s['files'].values())>2*1024**3:raise HTTPException(413,'파일은 512MB, 전체는 2GB 이하여야 합니다.')
                f.write(chunk)
        if not size:raise HTTPException(400,'빈 파일입니다.')
        item={'id':ident,'name':name,'size':size};s['files'][ident]=item;return item
    except BaseException:
        p.unlink(missing_ok=True);raise
    finally:s['uploading']=False
@app.get('/api/files/{fid}')
async def source(fid:str,req:Request):
    _,s=session(req);f=s['files'].get(fid)
    if not f:raise HTTPException(404)
    return FileResponse(s['root']/fid/f['name'],filename=f['name'],content_disposition_type='inline' if Path(f['name']).suffix.lower() in ['.mp4','.webm','.png','.jpg','.jpeg'] else 'attachment')
@app.delete('/api/files/{fid}')
async def remove_file(fid:str,req:Request):
    key,s=session(req,True)
    if any(j['owner']==key and j['status'] in ['queued','running'] for j in jobs.values()):raise HTTPException(409,'작업이 끝난 뒤 지워주세요.')
    f=s['files'].pop(fid,None)
    if not f:raise HTTPException(404)
    (s['root']/fid/f['name']).unlink(missing_ok=True)
    return {'ok':True}
async def run(j):
    async with gate:
        if j['status']=='cancelled':return
        j['status']='running';j['started']=time.time()
        root=j['root']
        try:
            with (root/'worker.log').open('wb') as log:
                p=subprocess.Popen([sys.executable,str(BASE/'worker.py'),str(root/'request.json')],stdout=log,stderr=log,start_new_session=os.name!='nt')
                j['process']=p
                while p.poll() is None:
                    await asyncio.sleep(1)
                    if shutil.disk_usage(DATA).free<512*1024**2 or time.time()-j['started']>1800 or sum(x.stat().st_size for x in root.rglob('*') if x.is_file())>8*1024**3:
                        stop(p);raise ValueError('작업 제한(30분 또는 8GB)을 넘었습니다. 더 짧은 영상으로 나누어 주세요.')
            if j['status']=='cancelled':return
            if p.returncode:
                message=(root/'error.txt').read_text(encoding='utf-8') if (root/'error.txt').exists() else '처리를 완료하지 못했습니다.'
                raise ValueError(message.replace(str(root),'[작업]')[:500])
            result=json.loads((root/'result.json').read_text(encoding='utf-8'))
            j['results']=[{'id':str(i),'name':name,'size':(root/'output'/name).stat().st_size} for i,name in enumerate(result['files'])]
            j['status']='done'
        except Exception as e:j.update(status='failed',error=str(e))
        finally:
            stop(j.get('process'));j.pop('process',None)
@app.post('/api/jobs')
async def submit(req:Request):
    key,s=session(req,True);b=await body_json(req)
    if sum(j['status'] in ['queued','running'] for j in jobs.values())>=8:raise HTTPException(429,'대기 작업이 많습니다. 잠시 후 실행하세요.')
    if any(j['owner']==key and j['status'] in ['queued','running'] for j in jobs.values()):raise HTTPException(409,'현재 작업을 완료하거나 취소해 주세요.')
    try:
        selected=b['files']
        if not isinstance(selected,list) or not 1<=len(selected)<=20 or len(set(selected))!=len(selected):raise ValueError('파일을 선택하세요.')
        files=[s['files'][i] for i in selected]
        opt=options(b['tool'],b.get('options',{}),rife_ready())
    except (ValueError,KeyError,TypeError) as e:raise HTTPException(400,str(e))
    ident=secrets.token_hex(16);root=s['root']/('job_'+ident);root.mkdir()
    (root/'request.json').write_text(json.dumps({'tool':b['tool'],'options':opt,'files':[dict(name=f['name'],path=str(s['root']/f['id']/f['name'])) for f in files]},ensure_ascii=False),encoding='utf-8')
    if len([j for j in jobs.values() if j['owner']==key])>=40:raise HTTPException(429,'작업 공간당 40회까지 실행할 수 있습니다. 새로 접속하세요.')
    j={'id':ident,'owner':key,'tool':b['tool'],'root':root,'status':'queued','results':[]};jobs[ident]=j;asyncio.create_task(run(j));return {'id':ident}
def own_job(jid,req,write=False):
    key,_=session(req,write);j=jobs.get(jid)
    if not j or j['owner']!=key:raise HTTPException(404)
    return j
@app.get('/api/jobs/{jid}')
async def status(jid:str,req:Request):
    j=own_job(jid,req);return {k:v for k,v in j.items() if k in ['id','status','results','error','started']}
@app.post('/api/jobs/{jid}/cancel')
async def cancel(jid:str,req:Request):
    j=own_job(jid,req,True)
    if j['status'] in ['queued','running']:j['status']='cancelled';stop(j.get('process'))
    return {'ok':True}
@app.get('/api/jobs/{jid}/results/{rid}')
async def result(jid:str,rid:str,req:Request):
    j=own_job(jid,req)
    f=next((f for f in j['results'] if f['id']==rid),None)
    if not f:raise HTTPException(404)
    p=(j['root']/'output'/f['name']).resolve()
    if not p.is_relative_to((j['root']/'output').resolve()):raise HTTPException(404)
    return FileResponse(p,filename=p.name)
@app.get('/')
async def index():return FileResponse(BASE/'static'/'index.html')
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
