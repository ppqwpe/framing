// Browser-local processing. No uploads or network calls in this module.
export function safeName(name){const n=String(name).replace(/[\x00-\x1f<>:"/\\|?*]/g,'_').replace(/[ .]+$/,'');if(!n||n==='.'||n==='..')throw Error('파일 이름을 입력하세요.');return n}
export function renamePlan(files,o){
 const used=new Set();return files.map((f,i)=>{const dot=f.name.lastIndexOf('.'),stem=dot>0?f.name.slice(0,dot):f.name,ext=dot>0?f.name.slice(dot):'';let n;
 if(o.style==='number'){if(!o.name?.trim()||!Number.isInteger(o.start)||o.start<0||!Number.isInteger(o.width)||o.width<1||o.width>8)throw Error('이름과 번호 설정을 확인하세요.');n=o.name+'_'+String(o.start+i).padStart(o.width,'0')}
 else if(o.style==='replace'){if(!o.find)throw Error('찾을 글자를 입력하세요.');n=stem.split(o.find).join(o.replace)}else n=o.prefix+stem+o.suffix;
 n=safeName(n+ext);const key=n.normalize('NFC').toLowerCase();if(used.has(key))throw Error('바꾼 이름이 서로 겹칩니다: '+n);used.add(key);return {name:n,blob:f.file||f};});
}
export function route(tool,o,force=false){return !force&&(['rename','contact'].includes(tool)||tool==='compare'&&o.format!=='video'||tool==='extract'&&o.pick!=='all'&&['png','jpg'].includes(o.frame_format))?'local':'server'}
const crcTable=Uint32Array.from({length:256},(_,i)=>{for(let k=0;k<8;k++)i=(i&1)?0xedb88320^(i>>>1):i>>>1;return i>>>0});
export function crc32(bytes){let crc=0xffffffff;for(const b of bytes)crc=crcTable[(crc^b)&255]^(crc>>>8);return (crc^0xffffffff)>>>0}
export async function zipStored(items,signal){
 if(items.reduce((n,x)=>n+x.blob.size,0)>256*1024**2)throw Error('ZIP은 256MB까지 묶습니다. 파일을 개별 다운로드해 주세요.');
 const blocks=[],entries=[];let offset=0;
 for(const item of items){signal?.throwIfAborted();const name=new TextEncoder().encode(safeName(item.name)),data=new Uint8Array(await item.blob.arrayBuffer()),crc=crc32(data);
  const local=new Uint8Array(30+name.length),v=new DataView(local.buffer);v.setUint32(0,0x04034b50,true);v.setUint16(4,20,true);v.setUint16(6,0x800,true);v.setUint16(12,33,true);v.setUint32(14,crc,true);v.setUint32(18,data.length,true);v.setUint32(22,data.length,true);v.setUint16(26,name.length,true);local.set(name,30);
  const central=new Uint8Array(46+name.length),c=new DataView(central.buffer);c.setUint32(0,0x02014b50,true);c.setUint16(4,20,true);c.setUint16(6,20,true);c.setUint16(8,0x800,true);c.setUint16(14,33,true);c.setUint32(16,crc,true);c.setUint32(20,data.length,true);c.setUint32(24,data.length,true);c.setUint16(28,name.length,true);c.setUint32(42,offset,true);central.set(name,46);blocks.push(local,data);entries.push(central);offset+=local.length+data.length;
 }
 const size=entries.reduce((n,e)=>n+e.length,0),end=new Uint8Array(22),v=new DataView(end.buffer);v.setUint32(0,0x06054b50,true);v.setUint16(8,items.length,true);v.setUint16(10,items.length,true);v.setUint32(12,size,true);v.setUint32(16,offset,true);return new Blob([...blocks,...entries,end],{type:'application/zip'});
}
export async function loadMedia(file,signal){
 const image=/\.(png|jpe?g|webp|gif)$/i.test(file.name),el=document.createElement(image?'img':'video'),url=URL.createObjectURL(file);el.src=url;
 if(!image){el.preload='auto';el.muted=true;el.playsInline=true}
 try{await new Promise((resolve,reject)=>{const event=image?'load':'loadeddata';let timer;
  const clean=()=>{clearTimeout(timer);el.removeEventListener(event,ok);el.removeEventListener('error',bad);signal?.removeEventListener('abort',abort)};
  const ok=()=>{clean();resolve()},bad=()=>{clean();reject(Error(file.name+' 형식을 브라우저에서 읽지 못했습니다. 서버 처리를 선택하세요.'))},abort=()=>{clean();reject(new DOMException('취소했습니다.','AbortError'))};
  el.addEventListener(event,ok,{once:true});el.addEventListener('error',bad,{once:true});signal?.addEventListener('abort',abort,{once:true});timer=setTimeout(bad,20000);if(signal?.aborted)abort();else if(image?el.complete&&el.naturalWidth:el.readyState>=2)ok();
 });
 const width=image?el.naturalWidth:el.videoWidth,height=image?el.naturalHeight:el.videoHeight;if(!width||!height||width*height>3840*2160)throw Error('브라우저 처리는 4K 이하 파일을 사용하세요.');
 return {el,image,width,height,duration:image?0:el.duration,close(){if(!image)el.pause();el.removeAttribute('src');if(!image)el.load();URL.revokeObjectURL(url)}};
 }catch(e){if(!image)el.pause();el.removeAttribute('src');if(!image)el.load();URL.revokeObjectURL(url);throw e}
}
export async function seek(media,time,signal){
 signal?.throwIfAborted();if(media.image)return;
 const target=Math.max(0,Math.min(Number(time)||0,Math.max(0,media.duration-.05)));if(Math.abs(media.el.currentTime-target)<.001&&media.el.readyState>=2)return;
 await new Promise((resolve,reject)=>{let timer;const clean=()=>{clearTimeout(timer);media.el.removeEventListener('seeked',ok);signal?.removeEventListener('abort',abort)},ok=()=>{clean();resolve()},abort=()=>{clean();reject(new DOMException('취소했습니다.','AbortError'))};media.el.addEventListener('seeked',ok,{once:true});signal?.addEventListener('abort',abort,{once:true});timer=setTimeout(()=>{clean();reject(Error('프레임을 읽는 시간이 초과됐습니다.'))},15000);media.el.currentTime=target;});
}
export function frameCanvas(width,height){const c=document.createElement('canvas');c.width=width;c.height=height;return c}
function fit(ctx,m,x,y,w,h){const scale=Math.min(w/m.width,h/m.height),dw=m.width*scale,dh=m.height*scale;ctx.drawImage(m.el,x+(w-dw)/2,y+(h-dh)/2,dw,dh)}
export function compose(a,b,o,time=0){const w=Math.min(a.width,1920),h=Math.round(a.height*w/a.width),c=frameCanvas(o.mode==='side'?w*2:w,h),ctx=c.getContext('2d');ctx.fillStyle='#080a09';ctx.fillRect(0,0,c.width,h);
 if(o.mode==='side'){fit(ctx,a,0,0,w,h);fit(ctx,b,w,0,w,h)}else if(o.mode==='b'){fit(ctx,b,0,0,w,h)}else {fit(ctx,a,0,0,w,h);if(o.mode==='wipe'){ctx.save();ctx.beginPath();ctx.rect(w*o.split,0,w*(1-o.split),h);ctx.clip();fit(ctx,b,0,0,w,h);ctx.restore()}if(o.mode==='overlay'){ctx.globalAlpha=o.mix;fit(ctx,b,0,0,w,h);ctx.globalAlpha=1}if(o.mode==='flicker'&&Math.floor(time*2)%2)fit(ctx,b,0,0,w,h)}return c}
export function blobCanvas(canvas,format='png'){return new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(Error('이미지를 저장하지 못했습니다.')),format==='jpg'?'image/jpeg':'image/png',.93))}
export function captureTimes(duration,o){
 if(!Number.isFinite(duration)||duration<=0)throw Error('영상 길이를 읽지 못했습니다.');
 if(o.pick==='at'){const values=String(o.at).trim().split(/[\s,]+/).map(s=>s==='first'?0:s==='middle'?duration/2:s==='last'?Math.max(0,duration-.05):Number(s));if(values.length>100||values.some(t=>!Number.isFinite(t)||t<0||t>duration))throw Error('위치는 first middle last 또는 영상 길이 안의 초 단위 숫자로 입력하세요.');return values}
 if(!Number.isInteger(o.count)||o.count<1||o.count>100)throw Error('이미지 수는 1~100으로 입력하세요.');return Array.from({length:o.count},(_,i)=>(i+.5)*duration/o.count);
}
export async function localJob(tool,files,o,signal,progress){
 if(!files.length)throw Error('파일을 선택하세요.');if(tool==='rename')return renamePlan(files,o);
 const results=[];let outputBytes=0;const add=x=>{outputBytes+=x.blob.size;if(outputBytes>256*1024**2)throw Error('이미지 결과가 256MB를 넘었습니다. 뽑는 수를 줄여주세요.');results.push(x)};
 if(tool==='compare'){
  if(!Number.isFinite(o.time)||o.time<0||![o.split,o.mix].every(x=>Number.isFinite(x)&&x>=0&&x<=1))throw Error('시간은 0 이상, 비율은 0~1로 입력하세요.');
  if(files.length!==2)throw Error('대조할 파일 두 개를 선택하세요.');const a=await loadMedia(files[0].file,signal);let b;
  try{b=await loadMedia(files[1].file,signal);if(a.image!==b.image)throw Error('영상끼리 또는 이미지끼리 선택하세요.');await seek(a,o.time,signal);await seek(b,o.time,signal);const modes=o.views==='all'||o.format==='frames'?['side','wipe','overlay','flicker','a','b']:[o.mode];for(const mode of modes){signal.throwIfAborted();add({name:'SHOTKIT_'+mode+'.png',blob:await blobCanvas(compose(a,b,{...o,mode},o.time))})}}finally{a.close();b?.close()}return results;
 }
 for(let i=0;i<files.length;i++){
  signal.throwIfAborted();const f=files[i],m=await loadMedia(f.file,signal);
  try{const stem=f.name.replace(/\.[^.]+$/,'');
   if(tool==='contact'){
    const c=frameCanvas(960,600),ctx=c.getContext('2d');ctx.fillStyle='#141815';ctx.fillRect(0,0,c.width,c.height);ctx.fillStyle='#eee';ctx.font='20px sans-serif';ctx.fillText(f.name,18,30,920);
    for(let n=0;n<6;n++){await seek(m,m.duration*(n+.5)/6,signal);const x=(n%3)*320,y=50+Math.floor(n/3)*270;fit(ctx,m,x,y,320,235);ctx.fillStyle='#eee';ctx.font='14px sans-serif';ctx.fillText(m.image?'이미지':(m.duration*(n+.5)/6).toFixed(2)+'초',x+12,y+257)}add({name:`${i+1}_${stem}_콘택트시트.png`,blob:await blobCanvas(c)});
   }else{if(m.image)throw Error('프레임을 뽑을 영상 파일을 선택하세요.');const times=captureTimes(m.duration,o);if(times.length*files.length>100)throw Error('한 번에 최대 100장까지 뽑을 수 있습니다.');for(let n=0;n<times.length;n++){await seek(m,times[n],signal);const c=frameCanvas(m.width,m.height);c.getContext('2d').drawImage(m.el,0,0);add({name:`${i+1}_${stem}_${String(n+1).padStart(3,'0')}.${o.frame_format}`,blob:await blobCanvas(c,o.frame_format)});progress(`${f.name} · ${n+1}/${times.length}장`)}}
  }finally{m.close()}
 }return results;
}
