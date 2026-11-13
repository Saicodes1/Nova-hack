import {useStation} from './state';
import {api, areaKm, shift, type AOI, type Demo, type Intent, type Result, type Stage, type Watch, type WatchPreset} from './types';

export const stageNames: Record<string,string> = {queued:'Waiting for the satellite',uplink:'Command sent up',scene_select_a:'Find the before image',scene_select_b:'Find the after image',
  load:'Load the image bands',cloud_check:'Check for cloud',indices:'Compute indices',change:'Detect change',landcover:'Look up land cover',
  trust:'Score confidence',render:'Render images',verdict:'Write onboard verdict',packet:'Pack the answer',downlink:'Answer sent down',
  explain:'Write the explanation',done:'Done',error:'Failed'};

let stream:EventSource|null = null;
let watchTimer = 0;
const store = () => useStation.getState();

/** Follow a running analysis: stage events first, then the finished answer. */
export function follow(id:string, keepWatch=false) {
  store().set({currentId:id,busy:true,error:'',stages:[],result:null,tab:'Answer',...(keepWatch?{}:{watch:null,watchRow:null})});
  stream?.close();
  const source = new EventSource('/api/analyses/'+id+'/events');
  stream = source;
  for (const key of Object.keys(stageNames)) {
    source.addEventListener(key,event=>{
      if (!(event instanceof MessageEvent)) {
        if (key==='error'&&stream===source) {
          source.close();store().set({busy:false,error:'Lost the progress stream. Ask again to retry.'});
        }
        return;
      }
      const row=JSON.parse(event.data) as Stage;
      store().stage(row);
      if (key==='error') {source.close();store().set({busy:false,error:row.detail.message||'The analysis failed.'});}
      if (key==='done') {
        source.close();
        api<Result>('/analyses/'+id).then(result=>{
          if (stream!==source) return;
          const [a,b]=['A','B'].map(role=>result.packet.scenes.find(s=>s.role===role));
          const now=store();
          store().set({result,aoi:result.packet.aoi,busy:false,intent:result.packet.intent,
            dateA:now.dateA||a?.requested_date||'',dateB:now.dateB||b?.requested_date||''});
        }).catch(error=>store().set({busy:false,error:error.message}));
      }
    });
  }
}

/** Show a finished analysis, for example one pass of a watch. */
export async function openAnalysis(id:string) {
  stream?.close();stream=null;
  try {
    const result=await api<Result>('/analyses/'+id);
    store().set({result,currentId:id,stages:[],busy:false,error:'',swipe:50});
  } catch(error) {store().set({error:(error as Error).message});}
}

export function missing() {
  const s=store();
  const gaps:string[]=[];
  if (s.task==='watch') {
    if (!s.aoi) gaps.push('an area');
    if (!s.intent) gaps.push('what to look for');
    if (!s.dateA) gaps.push('a reference date');
    if (!s.watchFrom) gaps.push('a start date');
    return gaps;
  }
  if (s.question.trim()) return gaps;
  if (!s.aoi) gaps.push('an area');
  if (!s.dateA) gaps.push('a before date');
  if (!s.dateB) gaps.push('an after date');
  if (!s.intent) gaps.push('what to look for');
  return gaps;
}

export async function ask() {
  const s=store();
  if (s.busy) return;
  if (s.aoi&&areaKm(s.aoi)>50.1) {s.set({error:'That area is too large. Keep it under 50 km².'});return;}
  if (s.dateA&&s.dateB&&s.dateA>=s.dateB) {s.set({error:'The before date must come before the after date.'});return;}
  stopWatchPolling();
  s.set({busy:true,error:'',stages:[],result:null,currentId:null,watch:null,watchRow:null});
  try {
    const reply=await api<{analysis_id?:string;clarification?:string;aoi?:AOI;date_a?:string;date_b?:string;intent?:Intent}>('/ask',{
      question:s.question,aoi:s.aoi,date_a:s.dateA||null,date_b:s.dateB||null,intent:s.intent||null,
      language:s.language,demo_id:s.demoId,link_mode:s.mode,exact_a:s.exactA,exact_b:s.exactB,source:s.source,
    });
    if (reply.analysis_id) {
      if (reply.aoi) s.set({aoi:reply.aoi,dateA:reply.date_a??s.dateA,dateB:reply.date_b??s.dateB,intent:reply.intent??s.intent});
      follow(reply.analysis_id);
    } else s.set({error:reply.clarification||'Add a place, two dates and what to look for.',busy:false});
  } catch(error) {s.set({error:(error as Error).message,busy:false});}
}

function stopWatchPolling() {window.clearInterval(watchTimer);watchTimer=0;}

/** Poll a watch while it runs, and put each new alert on the map as it arrives. */
export function pollWatch(id:string) {
  stopWatchPolling();
  let shown='';
  async function tick() {
    try {
      const watch=await api<Watch>('/watches/'+id);
      if (store().watch?.id!==id) {stopWatchPolling();return;}
      store().set({watch});
      const alert=[...watch.entries].reverse().find(e=>e.outcome==='alert'&&e.analysis_id);
      if (alert && alert.date!==shown && (watch.status==='running'||!shown)) {
        shown=alert.date;
        store().set({watchRow:alert.date});
        openAnalysis(alert.analysis_id!);
      }
      if (!['starting','running','armed'].includes(watch.status)) stopWatchPolling();
    } catch {/* keep polling; the ground station may be briefly busy */}
  }
  tick();
  watchTimer=window.setInterval(tick,1000);
}

export async function startWatch() {
  const s=store();
  if (!s.aoi||!s.intent||!s.dateA||!s.watchFrom) return;
  if (s.dateA>=s.watchFrom) {s.set({error:'The reference date must come before the first watched day.'});return;}
  stream?.close();stream=null;
  s.set({error:'',stages:[],result:null,currentId:null,busy:false,watch:null,watchRow:null});
  try {
    const watch=await api<Watch>('/watches',{aoi:s.aoi,intent:s.intent,question:s.question,baseline_date:s.dateA,
      start_date:s.watchFrom,end_date:s.watchUntil||null,floor_ha:s.floorHa,link_mode:s.mode,language:s.language});
    s.set({watch});
    pollWatch(watch.id);
  } catch(error) {s.set({error:(error as Error).message});}
}

export async function stopWatch() {
  const watch=store().watch;
  if (!watch) return;
  try {store().set({watch:await api<Watch>('/watches/'+watch.id+'/stop',{})});} catch {/* already stopped */}
}

export function applyPreset(demo:Demo, run=true) {
  store().set({task:'ask',demoId:demo.synthetic?demo.id:null,presetId:demo.synthetic?null:demo.id,question:demo.question,aoi:demo.aoi,
    dateA:demo.date_a,dateB:demo.date_b,exactA:Boolean(demo.exact),exactB:Boolean(demo.exact),intent:demo.intent,draw:null});
  store().reset();
  if (run) ask();
}

export function applyWatchPreset(preset:WatchPreset, run=true) {
  store().set({task:'watch',demoId:null,presetId:preset.id,question:preset.question,aoi:preset.aoi,dateA:preset.baseline_date,
    watchFrom:preset.start_date,watchUntil:preset.end_date??'',floorHa:preset.floor_ha,intent:preset.intent,draw:null,exactA:false,exactB:false});
  store().reset();
  if (run) startWatch();
}

/** Turn the current answer into a standing watch on the same area. */
export function watchFromAnswer() {
  const s=store();
  const a=s.result?.packet.scenes.find(scene=>scene.role==='A');
  const reference=a?.datetime.slice(0,10)||s.dateA;
  s.set({task:'watch',dateA:reference,watchFrom:shift(reference,1),watchUntil:'',demoId:null,
    question:s.question||'Tell me when this changes.'});
}
