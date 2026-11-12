import {useState} from 'react';
import {ArrowDownToLine} from 'lucide-react';
import {intents, number, type Packet} from './types';

const background = '#10151d';
const ink = '#e6edf3';

function loadImage(path:string):Promise<HTMLImageElement> {
  return new Promise((resolve,reject)=>{
    const image=new Image();
    image.onload=()=>resolve(image);
    image.onerror=()=>reject(new Error('The analysis image could not be loaded. Try the download again.'));
    image.src=path;
  });
}

function surface(width:number,height:number) {
  const canvas=document.createElement('canvas');
  canvas.width=width;
  canvas.height=height;
  const ctx=canvas.getContext('2d',{willReadFrequently:true});
  if (!ctx) throw new Error('Image export is unavailable in this browser.');
  return {canvas,ctx};
}

function patchLayers(mask:HTMLImageElement) {
  const w=mask.naturalWidth,h=mask.naturalHeight;
  const source=surface(w,h);
  source.ctx.drawImage(mask,0,0);
  const rgba=source.ctx.getImageData(0,0,w,h).data;
  const stroke=source.ctx.createImageData(w,h);
  const shade=source.ctx.createImageData(w,h);
  const visible=(i:number)=>rgba[i*4+3]>0;
  for (let i=0;i<w*h;i++) {
    if (!visible(i)) continue;
    const x=i%w,y=Math.floor(i/w);
    const edge=x<2||x>=w-2||y<2||y>=h-2||
      !visible(i-1)||!visible(i+1)||!visible(i-w)||!visible(i+w)||
      !visible(i-2)||!visible(i+2)||!visible(i-2*w)||!visible(i+2*w);
    const offset=i*4;
    const uncertain=rgba[offset]===120&&rgba[offset+1]===120&&rgba[offset+2]===120;
    const color=uncertain?[255,203,128]:[89,239,241];
    if (edge) stroke.data.set([...color,255],offset);
    shade.data.set([...color,edge?255:95],offset);
  }
  const outline=surface(w,h),filled=surface(w,h);
  outline.ctx.putImageData(stroke,0,0);
  filled.ctx.putImageData(shade,0,0);
  return [outline.canvas,filled.canvas];
}

function wrap(ctx:CanvasRenderingContext2D, text:string, width:number) {
  const lines:string[]=[];
  let line='';
  for (const word of text.split(/\s+/)) {
    const next=line ? line+' '+word : word;
    if (line&&ctx.measureText(next).width>width) {lines.push(line);line=word;}
    else line=next;
  }
  if (line) lines.push(line);
  return lines;
}

async function makeComparison(packet:Packet):Promise<Blob> {
  const base='/api/analyses/'+packet.analysis_id+'/images/';
  const [before,after,mask]=await Promise.all([
    loadImage(base+'a'),loadImage(base+'b'),loadImage(base+'change'),
  ]);
  const scale=Math.min(1,1200/Math.max(before.naturalWidth,before.naturalHeight));
  const w=Math.round(before.naturalWidth*scale),h=Math.round(before.naturalHeight*scale);
  const pad=20,gap=12,bar=34,top=68;
  const panel=Math.max(290,w);
  const width=2*panel+pad*2+gap;
  const patchCount=packet.stats?.n_polygons??0;
  const label=packet.stats
    ? (patchCount?number(patchCount,0)+(patchCount===1?' patch':' patches')+' · '+number(packet.stats.changed_ha,2)+' ha detected':'No patches above the detection threshold')
    : 'Change estimate unavailable for this pair';
  const source=packet.attribution.join(' · ');
  const layout=surface(width,1);
  layout.ctx.font='13px system-ui, sans-serif';
  const credits=wrap(layout.ctx,source,width-2*pad);
  const bodyHeight=top+2*(h+bar)+gap;
  const {canvas,ctx}=surface(width,bodyHeight+52+credits.length*19);
  ctx.fillStyle=background;
  ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.fillStyle=ink;
  ctx.font='bold 21px system-ui, sans-serif';
  ctx.fillText('Ask the Satellite · '+intents[packet.intent],pad,31,width-2*pad);
  ctx.fillStyle='#a3afbf';
  ctx.font='14px system-ui, sans-serif';
  ctx.fillText((packet.aoi.name||'Selected area')+' · '+label,pad,53,width-2*pad);
  const sceneA=packet.scenes.find(scene=>scene.role==='A');
  const sceneB=packet.scenes.find(scene=>scene.role==='B');
  const [outline,filled]=patchLayers(mask);
  const panels=[
    {label:'Before · '+(sceneA?.datetime.slice(0,10)||''),image:before},
    {label:'After · '+(sceneB?.datetime.slice(0,10)||''),image:after},
    {label:'Before · patch footprints',image:before,overlay:outline},
    {label:'After · detected patches',image:after,overlay:filled},
  ];
  panels.forEach(({label,image,overlay},i)=>{
    const x=pad+(i%2)*(panel+gap),y=top+Math.floor(i/2)*(h+bar+gap);
    ctx.fillStyle='#1c2633';
    ctx.fillRect(x,y,panel,h+bar);
    ctx.fillStyle=ink;
    ctx.font='bold 14px system-ui, sans-serif';
    ctx.fillText(label,x+10,y+22,panel-20);
    const imageX=x+Math.floor((panel-w)/2);
    ctx.drawImage(image,imageX,y+bar,w,h);
    if (overlay) ctx.drawImage(overlay,imageX,y+bar,w,h);
  });
  const legendY=bodyHeight+23;
  ctx.font='13px system-ui, sans-serif';
  if (packet.stats?.n_polygons) {
    ctx.fillStyle='#59eff1';ctx.fillRect(pad,legendY-10,10,10);
    ctx.fillStyle=ink;ctx.fillText('Detected footprint',pad+16,legendY);
    if (packet.stats.artefact_ha>0) {
      ctx.fillStyle='#ffcb80';ctx.fillRect(pad+154,legendY-10,10,10);
      ctx.fillStyle=ink;ctx.fillText('Possible cloud-edge artefact',pad+170,legendY);
    }
  } else {
    ctx.fillStyle=ink;ctx.fillText(label,pad,legendY,width-2*pad);
  }
  ctx.fillStyle='#a3afbf';
  credits.forEach((line,i)=>ctx.fillText(line,pad,legendY+21+i*19,width-2*pad));
  return new Promise((resolve,reject)=>canvas.toBlob(
    blob=>blob?resolve(blob):reject(new Error('The comparison could not be saved.')),'image/png'));
}

export default function ComparisonDownload({packet}:{packet:Packet}) {
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  if (packet.image_bounds.length!==4) return null;

  async function download() {
    setBusy(true);setError('');
    try {
      const blob=await makeComparison(packet);
      const url=URL.createObjectURL(blob);
      const link=document.createElement('a');
      link.href=url;
      link.download='satellite-comparison-'+packet.analysis_id+'.png';
      link.click();
      window.setTimeout(()=>URL.revokeObjectURL(url),1000);
    } catch (error) {setError((error as Error).message);}
    finally {setBusy(false);}
  }

  return <div className="comparison-download">
    <button type="button" onClick={download} disabled={busy}><ArrowDownToLine size={16}/>
      {busy?'Preparing PNG…':'Download comparison PNG'}
    </button>
    {error&&<p className="field-note warning" role="alert">{error}</p>}
  </div>;
}
