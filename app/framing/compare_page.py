"""대조 화면 HTML"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import quote

from .i18n import MessageCatalog


class ComparePage:
    """
    브라우저에서 직접 끌어 보는 대조 뷰어를 만든다.

    영상으로 구운 대조는 정해진 속도로만 지나간다. 정작 보고 싶은 건
    "이 부분에서 경계를 천천히 왔다 갔다" 인데 그게 안 된다.
    그래서 바를 손으로 끄는 페이지를 같이 만든다. 두 파일 옆에 두면 바로 열린다.
    """

    NAME = "대조.html"

    # 엔진 쪽 이름과 화면 쪽 이름이 다르다. 여기서 한 번만 맞춰 둔다.
    MODES = {"wipe": "wipe", "overlay": "fade", "flicker": "flip", "side": "side"}

    @classmethod
    def engine_key(cls, shown: str) -> str:
        """
        화면이 말하는 이름을 엔진 이름으로 되돌린다.

        이 되돌리는 길이 없어서 오래 틀려 있었다. 대조 창에서 겹치기(fade)나
        번갈아(flip)를 보다가 "mp4 로 저장" 을 누르면, 엔진은 그런 이름을 몰라
        조용히 첫 번째 보기(반반 바)를 구웠다. 보고 있던 것과 다른 게 나왔다.
        """
        for key, name in cls.MODES.items():
            if name == shown:
                return key
        return shown          # 이미 엔진 이름이면 그대로

    HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Framing 대조 — __TITLE__</title><style>
:root{--bg:#0b0d0f;--fg:#e8eaed;--dim:#8b949e;--line:#242a31;--accent:#e0a458}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,
 "Segoe UI","Malgun Gothic",sans-serif;display:flex;flex-direction:column;min-height:100vh}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;
 gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:14px;margin:0;letter-spacing:1px}
.tag{color:var(--dim);font-size:12px}
.modes{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap;align-items:center}
button{background:#0d0f11;border:1px solid var(--line);color:var(--dim);
 border-radius:999px;padding:5px 13px;font-size:12.5px;cursor:pointer;font-family:inherit}
button.on{border-color:var(--accent);color:var(--accent);background:#1d1a15}
/* 고른 보기에 딸린 조절칸. 필요할 때만 나타난다 — 늘 떠 있으면 머리띠만 복잡해진다. */
.sub{display:none;align-items:center;gap:7px;color:var(--dim);font-size:12px;
 padding:0 4px 0 12px;border-left:1px solid var(--line);margin-left:4px}
.sub.show{display:flex}
.sub button{padding:4px 11px;font-size:12px}
input[type=range]{width:130px;accent-color:var(--accent)}
main{flex:1;display:flex;align-items:center;justify-content:center;padding:18px;
 min-height:0;overflow:hidden}
#stage{transform-origin:center center}
#stage{position:relative;line-height:0;border:0;outline:1px solid var(--line);border-radius:0;
 overflow:hidden;cursor:ew-resize;background:#000;touch-action:none}
#base,#top{position:absolute;top:0;left:0;width:100%;height:100%;overflow:hidden;pointer-events:none;background:#000}
#base>*,#top>*{position:absolute;max-width:none;max-height:none;object-fit:contain;object-position:center;background:#000;user-select:none;pointer-events:none}
#bar{position:absolute;top:0;bottom:0;width:2px;background:var(--accent);
 pointer-events:none;box-shadow:0 0 8px rgba(224,164,88,.6)}
#bar::after{content:"";position:absolute;top:50%;left:50%;width:26px;height:26px;
 margin:-13px 0 0 -13px;border:2px solid var(--accent);border-radius:50%;background:rgba(11,13,15,.7)}
body.side #stage{cursor:default}
body.side #base{width:50%}
body.side #top{left:50%;width:50%;opacity:1!important}
body.solo #stage{cursor:default}
/* 재생 막대 — 화면 아래에 하나만 두고 A · B 를 함께 움직인다 */
#tape{display:flex;align-items:center;gap:12px;padding:9px 18px;
 border-top:1px solid var(--line)}
#tape[hidden]{display:none}
#tape button{width:38px;text-align:center;padding:5px 0}
#seek{flex:1;width:auto;accent-color:var(--accent)}
#time{color:var(--dim);font:12px ui-monospace,monospace;min-width:160px;text-align:right}
footer{padding:11px 18px;border-top:1px solid var(--line);color:var(--dim);font-size:12px;
 display:flex;gap:16px;flex-wrap:wrap}
kbd{background:#0d0f11;border:1px solid var(--line);border-radius:4px;padding:1px 6px;
 font-family:ui-monospace,monospace;font-size:11px;color:var(--fg)}
.name{color:var(--accent)}
[hidden]{display:none!important}
#pan{flex:1;min-height:180px;touch-action:none}
.stage-label{position:absolute;top:8px;z-index:2;background:#080b0fbb;border:1px solid #79828c;padding:7px 9px;line-height:1;font-weight:bold;pointer-events:none}
#leftLabel{left:8px}#rightLabel{right:8px}
#stage>video,#stage>img,#top>*{user-select:none;pointer-events:none}
#tape{flex-wrap:wrap}#tape button{width:auto;padding:5px 9px}
#stage{flex-shrink:0;box-sizing:content-box}
button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.region-box{position:absolute;pointer-events:none;border:1px solid #63d4e8;z-index:3;box-shadow:0 0 0 1px #0008}
#regionFields{display:flex;align-items:center;gap:8px;padding:7px 18px;flex-wrap:wrap;border-bottom:1px solid var(--line);color:var(--dim);font-size:12px}
#tape select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:4px}
#regionFields input{width:72px;background:var(--bg);border:1px solid var(--line);border-radius:5px;color:var(--fg);padding:4px}
#stage.select-region{cursor:crosshair}#regionHint{color:#63d4e8}
</style></head><body>
<header>
  <h1>Framing 대조</h1>
  <span class="tag">A <span class="name" translate="no">__A__</span> &nbsp;·&nbsp; B <span class="name" translate="no">__B__</span></span>
  <div class="modes">
    <button data-mode="wipe">반반 바</button>
    <button data-mode="fade">겹치기</button>
    <button data-mode="flip">번갈아</button>
    <button data-mode="side">나란히</button>
    <button data-mode="a">A만</button><button data-mode="b">B만</button>
    <button id="regionPick">영역 선택</button><button id="regionOnly" disabled>영역만 보기</button><button id="regionReset" hidden>영역 해제</button><button id="swap">A ↔ B</button><button id="fit">화면 맞춤</button><button id="actual">1:1</button>
    <span class="sub" id="splitWrap">경계
      <input type="range" id="split" aria-label="대조 경계" min="0" max="100" value="50" step="1">
      <span id="splitNum">50%</span><button id="center">중앙</button></span>
    <span class="sub" id="mixWrap">섞기
      <input type="range" id="mix" aria-label="B 혼합 비율" min="0" max="100" value="50">
      <span id="mixNum">50%</span></span>

  </div>
</header>
<div id="regionFields" hidden>
  <span id="regionHint">A 기준 픽셀 · 화면에서 끌어 선택</span>
  <label>X <input id="regionX" type="number" min="0" step="1" value="0"></label>
  <label>Y <input id="regionY" type="number" min="0" step="1" value="0"></label>
  <label>너비 <input id="regionW" type="number" min="1" step="1"></label>
  <label>높이 <input id="regionH" type="number" min="1" step="1"></label>
</div>
<main id="pan"><div id="stage" tabindex="0" aria-label="대조 화면" data-w="__W__" data-h="__H__" data-fps="__FPS__"><div id="base">__BOTTOM__</div><div id="top">__TOP__</div><div id="bar"></div><span id="leftLabel" class="stage-label">A</span><span id="rightLabel" class="stage-label">B</span></div></main>
<div id="tape" hidden>
  <button id="play" title="재생 · 멈춤 (Space)">▶</button>
  <button id="backFrame" aria-label="이전 프레임">−1f</button><button id="nextFrame" aria-label="다음 프레임">+1f</button>
  <input type="range" aria-label="재생 위치" id="seek" min="0" max="1000" value="0" step="1">
  <label>소리 <select id="audioSource" aria-label="대조 소리 · 저장에도 적용">
    <option value="a">A 소리</option><option value="b">B 소리</option><option value="none">소리 끄기</option>
  </select></label>
  <label>음량 <input id="audioVolume" aria-label="대조 음량" type="range" min="0" max="100" value="100" step="5" style="width:80px"></label>
  <span id="audioHint" role="status"></span>
  <span id="time">0:00 / 0:00</span>
</div>
<footer>
  <span>바를 끌거나 <kbd>←</kbd> <kbd>→</kbd> 로 경계를 옮깁니다 (<kbd>Shift</kbd> 로 미세 조정)</span>
  <span><kbd>Space</kbd> 재생 · 멈춤</span>
  <span><kbd>1</kbd>~<kbd>4</kbd> 보기 · <kbd>A</kbd> <kbd>B</kbd> 한쪽만 · <kbd>S</kbd> 맞바꾸기</span>
  <span>휠 확대 · 오른쪽 끌어 이동 · <kbd>,</kbd> <kbd>.</kbd> 한 프레임씩 · <kbd>Esc</kbd> 닫기</span>
  <span id="pos">50%</span>
  <span id="zoomNum"></span>
</footer>
<script>
__LOCALIZATION__
(() => {
  let language = /* @viewer-language */ "kr";
  const t = text => Messages.text(text, language);
  Messages.protect([...document.querySelectorAll('.name')].map(node => node.textContent));
  const setLanguage = value => {
    const previous = language;
    language = value === 'en' ? 'en' : 'kr';
    Messages.refresh(document.documentElement, language, previous);
    document.documentElement.lang = language === 'kr' ? 'ko' : 'en';
    document.title = t('Framing 대조') + ' — ' + [...document.querySelectorAll('.name')].map(node => node.textContent).join(' ↔ ');
  };
  setLanguage(language);
  addEventListener('message', event => {
    if (event.source === parent && event.data?.framing === 'language') setLanguage(event.data.lang);
  });
  class CompareRegionSelector {
    constructor(viewer) {
      this.viewer=viewer;this.value=null;this.editing=false;this.only=false;this.drag=null;
      this.boxes=[0,1].map(()=>{const n=document.createElement('div');n.className='region-box';n.hidden=true;viewer.stage.append(n);return n;});
      viewer.node('regionPick').onclick=()=>{this.editing=!this.editing;if(this.editing)this.only=false;this.paint();viewer.fit();viewer.publish();};
      viewer.node('regionOnly').onclick=()=>{if(!this.value)return;this.only=!this.only;this.editing=false;viewer.zoom=1;viewer.actualSize=false;viewer.panX=viewer.panY=0;this.paint();viewer.fit();viewer.publish();};
      viewer.node('regionReset').onclick=()=>{this.value=null;this.editing=false;this.only=false;this.paint();viewer.fit();viewer.publish();};
      ['X','Y','W','H'].forEach(key=>viewer.node('region'+key).onchange=()=>{
        const [w,h]=viewer.size(),read=k=>Number(viewer.node('region'+k).value);
        const x=Math.max(0,Math.min(w-1,Math.round(read('X')||0))),y=Math.max(0,Math.min(h-1,Math.round(read('Y')||0)));
        this.value={x,y,width:Math.max(1,Math.min(w-x,Math.round(read('W')||1))),height:Math.max(1,Math.min(h-y,Math.round(read('H')||1)))};
        this.paint();viewer.fit();viewer.publish();
      });
      const stage=viewer.stage;
      stage.addEventListener('pointerdown',e=>{
        if(!this.editing||e.button!==0||this.drag)return;
        e.preventDefault();e.stopImmediatePropagation();stage.setPointerCapture(e.pointerId);
        const box=stage.getBoundingClientRect(),wide=viewer.mode==='side'?2:1;
        const pane=wide===2&&e.clientX>=box.left+box.width/2?1:0;
        this.drag={id:e.pointerId,pane,start:this.point(e,pane)};this.move(e);
      });
      stage.addEventListener('pointermove',e=>{if(this.drag?.id===e.pointerId){e.stopImmediatePropagation();this.move(e);}});
      const end=e=>{if(this.drag?.id===e.pointerId){this.drag=null;if(stage.hasPointerCapture(e.pointerId))stage.releasePointerCapture(e.pointerId);viewer.publish();}};
      ['pointerup','pointercancel','lostpointercapture'].forEach(k=>stage.addEventListener(k,end));
    }
    point(e,pane) {
      const v=this.viewer,b=v.stage.getBoundingClientRect(),[w,h]=v.size(),pw=b.width/(v.mode==='side'?2:1);
      return [Math.max(0,Math.min(w,Math.round((e.clientX-b.left-pane*pw)/pw*w))),Math.max(0,Math.min(h,Math.round((e.clientY-b.top)/b.height*h)))];
    }
    move(e) {
      const [w,h]=this.viewer.size(),a=this.drag.start,b=this.point(e,this.drag.pane);
      const x=Math.min(w-1,Math.min(a[0],b[0])),y=Math.min(h-1,Math.min(a[1],b[1]));
      this.value={x,y,width:Math.max(1,Math.max(a[0],b[0])-x),height:Math.max(1,Math.max(a[1],b[1])-y)};
      this.paint();this.viewer.publish();
    }
    paint() {
      const v=this.viewer,[w,h]=v.size(),r=this.value,wide=v.mode==='side'?2:1;
      v.stage.classList.toggle('select-region',this.editing);
      v.node('regionPick').classList.toggle('on',this.editing);v.node('regionPick').setAttribute('aria-pressed',String(this.editing));
      v.node('regionFields').hidden=!this.editing&&!r;v.node('regionReset').hidden=!this.editing&&!r;v.node('regionReset').disabled=!r;
      this.boxes.forEach((box,i)=>{box.hidden=!r||i>=wide||this.only;if(r)Object.assign(box.style,{left:((r.x/w+i)/wide*100)+'%',top:r.y/h*100+'%',width:r.width/w/wide*100+'%',height:r.height/h*100+'%'});});
      v.node('regionOnly').disabled=!r;v.node('regionOnly').textContent=t(this.only?'전체 보기로':'영역만 보기');v.node('regionOnly').classList.toggle('on',this.only);v.node('regionOnly').setAttribute('aria-pressed',String(this.only));
      const values=r||{x:0,y:0,width:w,height:h};
      Object.entries({X:'x',Y:'y',W:'width',H:'height'}).forEach(([key,field])=>{v.node('region'+key).value=values[field];});
    }
  }
  class CompareAudio {
    constructor(viewer) {
      this.viewer=viewer;this.media=viewer.media;
      this.available=__AUDIO__;
      this.select=viewer.node('audioSource');this.volume=viewer.node('audioVolume');
      this.select.value=this.available[0]?'a':this.available[1]?'b':'none';
      ['a','b'].forEach((key,i)=>this.select.querySelector(`[value="${key}"]`).disabled=!this.available[i]);
      this.select.onchange=()=>{this.apply();viewer.publish();};
      this.volume.oninput=()=>{this.apply();viewer.publish();};
      this.apply();
    }
    get source(){return this.select.value;}
    get level(){return Number(this.volume.value)/100;}
    apply(){
      this.media.forEach((media,i)=>{media.muted=this.source!==['a','b'][i];media.volume=this.level;});
      this.viewer.node('audioHint').textContent=t(this.available.some(Boolean)?'선택한 소리로 MP4 저장':'두 파일에 오디오가 없습니다');
      this.volume.disabled=this.source==='none';
    }
  }
  class CompareViewer {
    constructor() {
      this.stage = document.getElementById('stage');
      this.top = document.getElementById('top');
      this.bar = document.getElementById('bar');
      this.mode = 'wipe'; this.split = .5; this.swapped = false;
      this.zoom = 1; this.panX = this.panY = 0; this.fitted = 1;
      this.actualSize = false;
      this.drag = this.pan = null; this.flipTimer = null; this.showB = true;
      this.fps = +this.stage.dataset.fps || 24;
      this.region = new CompareRegionSelector(this);
      this.audio = new CompareAudio(this);
      this.bind(); this.setMode('__START__'); this.fit(); this.tape();
    }
    get media() { return [...this.stage.querySelectorAll('video')]; }
    get leader() { return this.media[0]; }
    node(id) { return document.getElementById(id); }
    clamp(value) { return Math.max(0, Math.min(1, value)); }
    frameAt(time) { return Math.floor(time*this.fps+1e-3); }
    size() {
      const one=this.stage.querySelector('video,img');
      return [+this.stage.dataset.w||one.videoWidth||one.naturalWidth||640,+this.stage.dataset.h||one.videoHeight||one.naturalHeight||360];
    }
    snapshot() { return {framing:'mode',mode:this.mode,split:this.split,mix:+this.node('mix').value/100,
      swapped:this.swapped,time:this.leader?.currentTime||0,showB:this.showB,region:this.region.value,regionOnly:this.region.only,
      audioSource:this.audio.source,audioVolume:this.audio.level}; }
    publish() { parent.postMessage(this.snapshot(), '*'); }
    displayArea() {
      const [width,height]=this.size();
      return this.region.only&&this.region.value?this.region.value:{x:0,y:0,width,height};
    }
    fit() {
      const [w,h]=this.size(),r=this.displayArea(),wide=this.mode==='side'?2:1;
      const available=Math.max(100,innerHeight-this.node('pan').offsetTop-130);
      const scale=Math.min((innerWidth-36)/(r.width*wide),available/r.height);
      this.stage.style.width=(r.width*wide*scale)+'px';
      this.stage.style.height=(r.height*scale)+'px';
      this.stage.querySelectorAll('video,img').forEach(media=>Object.assign(media.style,{
        width:(w*scale)+'px',height:(h*scale)+'px',left:(-r.x*scale)+'px',top:(-r.y*scale)+'px'}));
      this.fitted=scale;if(this.actualSize)this.zoom=1/scale;this.applyZoom();this.paint();
    }
    applyZoom() {
      this.stage.style.transform=`translate(${this.panX}px,${this.panY}px) scale(${this.zoom})`;
      this.node('zoomNum').textContent=Math.round(this.zoom*this.fitted*100)+'%';
    }
    paint() {
      let clip=0;
      const area=this.displayArea(),boundary=(this.split*this.size()[0]-area.x)/area.width;
      if(this.mode==='wipe')clip=this.clamp(boundary)*100;
      if(this.mode==='a')clip=this.swapped?0:100;
      if(this.mode==='b')clip=this.swapped?100:0;
      if(this.mode==='flip')clip=this.showB?0:100;
      this.top.style.clipPath=`inset(0 0 0 ${clip}%)`;
      this.top.style.opacity=this.mode==='fade'?+this.node('mix').value/100:1;
      this.bar.hidden=this.mode!=='wipe'||boundary<0||boundary>1;this.bar.style.left=this.clamp(boundary)*100+'%';
      const percent=Math.round(this.clamp(boundary)*100);
      this.node('split').value=percent;
      this.node('splitNum').textContent=percent+'%'+(this.region.only?' · X '+Math.round(this.split*this.size()[0]):'');
      this.node('mixNum').textContent=this.node('mix').value+'%';
      this.node('pos').textContent=this.mode==='wipe'?t(`왼쪽 ${this.swapped?'B':'A'} ${percent}% · 오른쪽 ${this.swapped?'A':'B'}`):this.mode==='side'?t(`왼쪽 ${this.swapped?'B':'A'} | 오른쪽 ${this.swapped?'A':'B'}`):'';
      this.node('leftLabel').textContent=this.swapped?'B':'A';
      this.node('rightLabel').textContent=this.swapped?'A':'B';
      this.node('leftLabel').hidden=this.node('rightLabel').hidden=!['wipe','side'].includes(this.mode);
      this.region.paint();
    }
    setMode(mode) {
      this.mode=['wipe','fade','flip','side','a','b'].includes(mode)?mode:'wipe';
      clearInterval(this.flipTimer); this.flipTimer=null;
      document.body.className=this.mode==='side'?'side':['a','b'].includes(this.mode)?'solo':'';
      this.node('mixWrap').classList.toggle('show',this.mode==='fade');
      this.node('splitWrap').classList.toggle('show',this.mode==='wipe');
      document.querySelectorAll('[data-mode]').forEach(b=>{const on=b.dataset.mode===this.mode;b.classList.toggle('on',on);b.setAttribute('aria-pressed',String(on));});
      if(this.mode==='flip')this.flipTimer=setInterval(()=>{this.showB=!this.showB;this.paint();this.publish();},260);
      this.fit(); this.publish();
    }
    setSplit(value) { const [w]=this.size();this.split=Math.round(this.clamp(value)*w)/w;this.paint();this.publish(); }
    moveSplit(event) {
      const box=this.stage.getBoundingClientRect();
      const area=this.displayArea();
      if(box.width)this.setSplit((area.x+this.clamp((event.clientX-box.left)/box.width)*area.width)/this.size()[0]);
    }
    swap() {
      const base=this.node('base'),low=base.firstElementChild,high=this.top.firstElementChild;
      base.append(high);this.top.append(low);
      this.swapped=!this.swapped; this.fit();this.publish();
    }
    duration() {
      const values=this.media.map(m=>m.duration).filter(v=>Number.isFinite(v)&&v>0);
      return values.length?Math.min(...values):0;
    }
    seek(at) {
      const last=Math.max(0,(Math.ceil(this.duration()*this.fps-Math.max(1e-6,this.fps*1e-6))-1)/this.fps);
      const value=Math.max(0,Math.min(at,last));
      this.media.forEach(m=>{if(m.readyState)m.currentTime=value;});this.tape();this.publish();
    }
    pause() { this.media.forEach(m=>m.pause()); }
    toggle() {
      if(!this.leader)return;
      if(!this.leader.paused)this.pause();
      else {
        if(this.leader.currentTime>=this.duration()-1/this.fps-.00001)this.seek(0);
        Promise.all(this.media.map(m=>m.play())).catch(()=>{this.pause();this.node('pos').textContent=t('재생하지 못했습니다. 파일 형식을 확인하세요.');});
      }
    }
    step(dir) { if(this.leader){this.pause();this.seek((this.frameAt(this.leader.currentTime)+dir)/this.fps);} }
    tape() {
      this.node('tape').hidden=!this.leader;
      if(!this.leader)return;
      const time=this.leader.currentTime,duration=this.duration();
      this.node('seek').value=duration?time/duration*1000:0;
      this.node('time').textContent=`${time.toFixed(2)} / ${duration.toFixed(2)}s · #${this.frameAt(time)+1}`;
      this.node('play').textContent=this.leader.paused?'▶':'❚❚';this.publish();
    }
    bind() {
      const stage=this.stage,pan=this.node('pan');
      stage.addEventListener('pointerdown',e=>{
        if(e.button!==0||this.mode!=='wipe'||this.drag!==null)return;
        e.preventDefault();stage.focus({preventScroll:true});this.drag=e.pointerId;stage.setPointerCapture(e.pointerId);this.moveSplit(e);
      });
      stage.addEventListener('pointermove',e=>{if(this.drag===e.pointerId)this.moveSplit(e);});
      const end=e=>{if(this.drag===e.pointerId){this.drag=null;if(stage.hasPointerCapture(e.pointerId))stage.releasePointerCapture(e.pointerId);}};
      ['pointerup','pointercancel','lostpointercapture'].forEach(event=>stage.addEventListener(event,end));
      pan.addEventListener('pointerdown',e=>{
        if(![1,2].includes(e.button)||this.pan)return;
        e.preventDefault();this.pan={id:e.pointerId,x:e.clientX-this.panX,y:e.clientY-this.panY};pan.setPointerCapture(e.pointerId);
      });
      pan.addEventListener('pointermove',e=>{if(this.pan?.id===e.pointerId){this.panX=e.clientX-this.pan.x;this.panY=e.clientY-this.pan.y;this.applyZoom();}});
      const endPan=e=>{if(this.pan?.id===e.pointerId){this.pan=null;if(pan.hasPointerCapture(e.pointerId))pan.releasePointerCapture(e.pointerId);}};
      ['pointerup','pointercancel','lostpointercapture'].forEach(event=>pan.addEventListener(event,endPan));
      pan.addEventListener('contextmenu',e=>e.preventDefault());
      pan.addEventListener('wheel',e=>{
        e.preventDefault();const before=this.zoom;this.actualSize=false;
        this.zoom=Math.min(16,Math.max(.2,before*(e.deltaY<0?1.15:1/1.15)));
        const box=stage.getBoundingClientRect(),ratio=this.zoom/before-1;
        this.panX-=(e.clientX-box.left-box.width/2)*ratio;this.panY-=(e.clientY-box.top-box.height/2)*ratio;this.applyZoom();
      },{passive:false});
      document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>this.setMode(b.dataset.mode));
      this.node('split').oninput=e=>{const r=this.displayArea();this.setSplit((r.x+(+e.target.value/100)*r.width)/this.size()[0]);};
      this.node('center').onclick=()=>{const r=this.displayArea();this.setSplit((r.x+r.width/2)/this.size()[0]);};
      this.node('mix').oninput=()=>{this.paint();this.publish();};
      this.node('swap').onclick=()=>this.swap();
      this.node('fit').onclick=()=>{this.actualSize=false;this.zoom=1;this.panX=this.panY=0;this.fit();};
      this.node('actual').onclick=()=>{this.actualSize=true;this.zoom=1/this.fitted;this.panX=this.panY=0;this.applyZoom();};
      this.node('play').onclick=()=>this.toggle();
      this.node('backFrame').onclick=()=>this.step(-1);this.node('nextFrame').onclick=()=>this.step(1);
      this.node('seek').oninput=e=>this.seek(this.duration()*+e.target.value/1000);
      this.media.forEach(m=>{
        m.removeAttribute('loop');
        ['loadedmetadata','loadeddata'].forEach(k=>m.addEventListener(k,()=>{this.fit();this.tape();}));
        ['timeupdate','play','pause','seeked'].forEach(k=>m.addEventListener(k,()=>this.tape()));
        m.addEventListener('timeupdate',()=>{
          if(m!==this.leader)return;
          if(m.currentTime>=this.duration()-.005&&!m.paused){this.pause();this.seek(this.duration());}
          this.media.slice(1).forEach(other=>{if(other.readyState&&!other.seeking&&Math.abs(other.currentTime-m.currentTime)>1/this.fps)other.currentTime=m.currentTime;});
        });
        m.addEventListener('ended',()=>{this.pause();this.seek(this.duration());});
      });
      stage.querySelectorAll('img').forEach(img=>img.addEventListener('load',()=>this.fit()));
      document.addEventListener('keydown',e=>{
        if(e.key==='Escape'){parent.postMessage('framing:close-viewer','*');return;}
        if(e.ctrlKey||e.metaKey||e.altKey||e.target.closest('input,select,textarea,button,[contenteditable="true"]'))return;
        const key=e.key.toLowerCase();
        if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();this.setMode('wipe');this.setSplit(this.split+(e.key==='ArrowLeft'?-1:1)*(e.shiftKey?.01:.05)*this.displayArea().width/this.size()[0]);}
        else if(key===' '){e.preventDefault();this.toggle();}
        else if(key>='1'&&key<='4')this.setMode(['wipe','fade','flip','side'][+key-1]);
        else if(key==='a'||key==='b')this.setMode(key);
        else if(key==='s')this.swap();
        else if(key===','||key==='.')this.step(key===','?-1:1);
        else if(key==='0')this.node('fit').click();else if(key==='9')this.node('actual').click();
      });
      addEventListener('message',async e=>{
        if(e.source!==parent||e.data?.framing!=='capture-state')return;
        // Whole-video exports do not depend on the browser decoder being ready.
        if(e.data.format!=='frame'){
          this.pause();parent.postMessage({...this.snapshot(),framing:'captured-state',request:e.data.request},'*');return;
        }
        this.pause();clearInterval(this.flipTimer);this.flipTimer=null;
        if(this.leader)this.seek(this.frameAt(this.leader.currentTime)/this.fps);
        const request=e.data.request;
        try {
          await Promise.all(this.media.map(m=>new Promise((resolve,reject)=>{
            if(!m.seeking&&m.readyState>=2){resolve();return;}
            const done=()=>{if(!m.seeking&&m.readyState>=2){clearTimeout(timer);m.removeEventListener('seeked',done);m.removeEventListener('loadeddata',done);resolve();}};
            const timer=setTimeout(()=>{m.removeEventListener('seeked',done);m.removeEventListener('loadeddata',done);reject(new Error(t('프레임을 아직 읽는 중입니다. 잠시 후 다시 저장하세요.')));},5000);
            m.addEventListener('seeked',done);m.addEventListener('loadeddata',done);
          })));
          parent.postMessage({...this.snapshot(),framing:'captured-state',request},'*');
        } catch(error){parent.postMessage({framing:'captured-state',request,error:error.message},'*');}
      });
      addEventListener('resize',()=>this.fit());
      addEventListener('blur',()=>{this.drag=null;this.pan=null;});
      addEventListener('pagehide',()=>{clearInterval(this.flipTimer);this.pause();});
    }
  }
  new CompareViewer();
})();
</script></body></html>"""

    def __init__(self, a_name: str, b_name: str, video: bool,
                 size: tuple[int, int] = (0, 0), start: str = "wipe",
                 fps: float = 24.0, language: str = 'kr', audio=(False, False)) -> None:
        self._a = a_name
        self._b = b_name
        self._video = video
        self._size = size
        self._start = self.MODES.get(start, "wipe")
        self._fps = fps if fps and fps > 0 else 24.0
        self._language = 'en' if language == 'en' else 'kr'
        self._audio = [bool(value) for value in audio]

    def _tag(self, name: str, controls: bool = False) -> str:
        if self._video:
            # 브라우저가 붙여 주는 재생 막대는 쓰지 않는다. 아래층에만 달면
            # 위층에 가려 오른쪽 반만 보이고, 두 영상을 같이 몰지도 못한다.
            # 대신 화면 아래에 우리 것을 하나 두고 둘을 함께 움직인다.
            return (f'<video src="{name}" loop muted playsinline '
                    f'preload="auto"></video>')
        return f'<img src="{name}" alt="">'

    def render(self, a_src: str = "", b_src: str = "") -> str:
        """
        뷰어 한 장. 파일로 저장할 때는 옆에 둔 파일 이름을, 앱 안에서 띄울 때는
        앱이 정해 준 주소를 넣는다. 같은 화면을 두 번 만들지 않으려는 것이다.
        """
        values = {'LOCALIZATION': MessageCatalog.script(), 'TITLE': html.escape(f'{self._a} ↔ {self._b}'),
                  'A': html.escape(self._a), 'B': html.escape(self._b),
                  'W': str(self._size[0] or 0), 'H': str(self._size[1] or 0),
                  'START': self._start, 'FPS': f'{self._fps:.12g}', 'AUDIO': json.dumps(self._audio),
                  'BOTTOM': self._tag(html.escape(a_src or quote(self._a), quote=True)),
                  'TOP': self._tag(html.escape(b_src or quote(self._b), quote=True))}
        template = self.HTML.replace('/* @viewer-language */ "kr"',
                                     '/* @viewer-language */ "' + self._language + '"')
        return re.sub(r'__(LOCALIZATION|TITLE|A|B|W|H|START|FPS|AUDIO|BOTTOM|TOP)__',
                      lambda match: values[match.group(1)], template)


    def write(self, folder: Path) -> Path:
        destination = folder / self.NAME
        destination.write_text(self.render(), encoding="utf-8")
        return destination


