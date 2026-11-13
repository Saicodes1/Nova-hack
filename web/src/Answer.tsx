import {useEffect, useRef, useState} from 'react';
import {AlertTriangle, ArrowDownToLine, BellRing, Check, Cloud, CloudOff, Loader2, Minus, Satellite, Send, ShieldCheck, Square, X} from 'lucide-react';
import ComparisonDownload from './ComparisonDownload';
import {ask, follow, openAnalysis, stageNames, stopWatch, watchFromAnswer} from './actions';
import {useStation, type Tab} from './state';
import {api, bytes, center, cloudTone, day, duration, intents, moment, number, rates, shift, today, type Health, type Mode,
  type OrbitPass, type Overpass, type Packet, type Validation, type Watch} from './types';

const tabs:Tab[] = ['Answer','Evidence','Downlink','Next passes'];

function Confidence({packet}:{packet:Packet}) {
  const level=packet.trust.level.toLowerCase();
  return <span className={'confidence '+level} title={'Confidence score '+packet.trust.score+' out of 100'}>
    <ShieldCheck size={13}/>{packet.trust.level} confidence<b>{packet.trust.score}</b></span>;
}

/** The headline of an answer: one number, or one plain reason there is none. */
function Hero({packet}:{packet:Packet}) {
  const b=packet.scenes.find(s=>s.role==='B');
  if (packet.status==='ok'&&packet.stats) return <div className="hero">
    <div className="big">{number(packet.stats.changed_ha,packet.stats.changed_ha<100?1:0)}<small>hectares</small></div>
    <p>of {intents[packet.intent].short} · {number(packet.stats.changed_pct)}% of the area · {packet.stats.n_polygons} {packet.stats.n_polygons===1?'patch':'patches'}</p>
  </div>;
  if (packet.status==='refused_cloudy') return <div className="hero refuse">
    <div className="big"><Cloud size={34}/>Too cloudy to answer</div>
    <p>{b?'On '+day(b.datetime)+', '+number(b.aoi_cloud_pct,0)+'% of the area was under cloud. ':''}The satellite will not guess.</p>
  </div>;
  if (packet.status==='no_scenes') return <div className="hero refuse">
    <div className="big"><CloudOff size={34}/>No image for these dates</div><p>Sentinel-2 has no usable overpass of this area near the dates you chose.</p></div>;
  return <div className="hero none"><div className="big"><Minus size={34}/>No change found</div>
    <p>Nothing above the {number((packet.trust.thresholds as {trust?:{min_change_ha?:number}})?.trust?.min_change_ha??0.5)} ha detection floor.</p></div>;
}

/** After a cloudy refusal: the real clear passes close to the date that was asked for. */
function ClearPasses({packet}:{packet:Packet}) {
  const s=useStation();
  const [rows,setRows]=useState<Overpass[]|null>(null);
  const b=packet.scenes.find(scene=>scene.role==='B');
  const a=packet.scenes.find(scene=>scene.role==='A');
  const asked=b?.datetime.slice(0,10)||s.dateB;
  useEffect(()=>{
    if (!asked) return;
    const controller=new AbortController();
    const end=shift(asked,22)>today()?today():shift(asked,22);
    api<Overpass[]>('/passes',{aoi:packet.aoi,start:shift(asked,-10),end},controller.signal).then(setRows).catch(()=>setRows([]));
    return ()=>controller.abort();
  },[packet.analysis_id]);
  const clear=(rows??[]).filter(row=>row.cloud_pct<20&&row.date!==asked&&(!a||row.date>a.datetime.slice(0,10)));
  if (rows===null) return <p className="hint"><Loader2 size={12} className="spin"/> Looking for clear overpasses nearby…</p>;
  if (!clear.length) return <p className="hint">No clear overpass within three weeks. Open "Next passes" for the forecast.</p>;
  return <div className="retry"><span className="label">Clear overpasses nearby</span>
    {clear.slice(0,4).map(row=><button key={row.date} onClick={()=>{s.set({dateB:row.date,exactB:true,presetId:null});ask();}}>
      <i className={cloudTone(row.cloud_pct)}/>{day(row.date)}<small>{number(row.cloud_pct,0)}% cloud</small><span>Ask again with this day</span></button>)}
  </div>;
}

function Scenes({packet,ids}:{packet:Packet;ids?:boolean}) {
  return <div className="scenes">{packet.scenes.map(scene=>{
    const shot=packet.thumbnails?.[scene.role.toLowerCase()];
    return <article key={scene.role}>
      {shot&&<img src={'data:image/webp;base64,'+shot} alt=""/>}
      <div><span className="label">{scene.role==='A'?'Before':'After'}</span>
        <b title={moment(scene.datetime)+' local time'}>{day(scene.datetime)}</b>
        <small><i className={cloudTone(scene.aoi_cloud_pct)}/>{number(scene.aoi_cloud_pct,0)}% cloud
          {scene.days_from_requested>0&&<> · asked for {day(scene.requested_date,false)}</>}</small>
        {ids&&<>{scene.item_ids.map(id=><code key={id}>{id}</code>)}<small>{scene.source}</small></>}
      </div></article>;
  })}</div>;
}

function Chat({packet}:{packet:Packet}) {
  const s=useStation();
  const [chat,setChat]=useState<{role:string;text:string}[]>([]);
  const [message,setMessage]=useState('');
  const [waiting,setWaiting]=useState(false);
  async function send(event:React.FormEvent) {
    event.preventDefault();
    const text=message.trim();
    if (!text||waiting) return;
    setChat(rows=>[...rows,{role:'You',text}]);setMessage('');setWaiting(true);
    try {
      const result=await api<{reply:string;new_analysis_id?:string}>('/chat',{analysis_id:packet.analysis_id,message:text,language:s.language});
      setChat(rows=>[...rows,{role:'Satellite',text:result.reply}]);
      if (result.new_analysis_id) follow(result.new_analysis_id);
    } catch(error) {setChat(rows=>[...rows,{role:'Satellite',text:(error as Error).message}]);}
    finally {setWaiting(false);}
  }
  return <section className="chat"><span className="label">Ask a follow-up</span>
    {chat.map((row,i)=><p key={i} className={row.role==='You'?'me':''}>{row.text}</p>)}
    {waiting&&<p className="hint"><Loader2 size={12} className="spin"/> Checking the evidence…</p>}
    <form onSubmit={send}><input value={message} maxLength={2000} placeholder="Why is the confidence not higher?" onChange={e=>setMessage(e.target.value)} aria-label="Follow-up question"/>
      <button aria-label="Send" disabled={waiting||!message.trim()}><Send size={15}/></button></form>
  </section>;
}

function AnswerTab({packet,health,compact}:{packet:Packet;health:Health|null;compact?:boolean}) {
  const s=useStation();
  const result=s.result!;
  const synthetic=packet.scenes.some(scene=>scene.source==='synthetic');
  const writer=health?.node_info?.verdict_writer;
  const low=packet.trust.level==='Low';
  return <div dir={result.plan.language==='ar'?'rtl':'ltr'}>
    <Hero packet={packet}/>
    <Confidence packet={packet}/>
    {synthetic&&<span className="confidence synthetic">Made-up sample imagery</span>}
    {result.explanation.headline!==packet.onboard_verdict&&<h3>{result.explanation.headline}</h3>}
    {result.explanation.body!==result.explanation.headline&&<p className="body">{result.explanation.body}</p>}
    {packet.status==='refused_cloudy'&&!synthetic&&<ClearPasses packet={packet}/>}
    {packet.onboard_verdict&&<blockquote><span className="label"><Satellite size={12}/> Said onboard{writer?.startsWith('ollama')?' · written by '+writer.replace('ollama:','')+' on the satellite':''}</span>{packet.onboard_verdict}</blockquote>}
    <Scenes packet={packet}/>
    {!compact&&<section className={'caveats'+(low?' low':'')}><span className="label"><AlertTriangle size={12}/> What could make this wrong</span>
      <ul>{result.explanation.caveats.map(c=><li key={c}>{c}</li>)}</ul></section>}
    {!compact&&<div className="actions">
      {!synthetic&&<button onClick={watchFromAnswer}><BellRing size={15}/> Keep watching this area</button>}
      {packet.image_bounds.length===4&&<ComparisonDownload key={packet.analysis_id} packet={packet}/>}
    </div>}
    {!compact&&health?.openai.explainer&&!synthetic&&<Chat key={packet.analysis_id} packet={packet}/>}
    <p className="credit">{packet.attribution.join(' · ')}</p>
  </div>;
}

function EvidenceTab({packet}:{packet:Packet}) {
  const s=useStation();
  const [check,setCheck]=useState<Validation|null>(null);
  const [reference,setReference]=useState('');
  useEffect(()=>{
    setCheck(null);
    if (!s.presetId) return;
    api<{presets:{id:string;validation?:string}[]}>('/demo-cases').then(({presets})=>{
      const name=presets.find(p=>p.id===s.presetId)?.validation;
      if (name) {setReference(name);return api<Validation>('/validation/'+name).then(setCheck);}
    }).catch(()=>{});
  },[s.presetId,packet.analysis_id]);
  const stats=packet.stats;
  return <>
    {stats&&<dl className="metrics">
      <div><dt>Changed</dt><dd>{number(stats.changed_ha,2)}<small>ha</small></dd></div>
      <div><dt>Share of area</dt><dd>{number(stats.changed_pct)}<small>%</small></dd></div>
      <div><dt>Area clear in both images</dt><dd>{number(stats.valid_overlap_pct)}<small>%</small></dd></div>
      <div><dt>Largest patch</dt><dd>{number(stats.largest_polygon_ha,1)}<small>ha</small></dd></div>
    </dl>}
    {check&&<section><h4>Checked against an independent map</h4>
      <p className="body">Brazil's space agency INPE maps Amazon deforestation by hand every year (PRODES). Compared with its 2020–2024 polygons for this area:</p>
      <dl className="metrics">
        <div><dt>Recall</dt><dd>{number(check.recall*100,0)}<small>%</small></dd></div>
        <div><dt>Precision</dt><dd>{number(check.precision*100,0)}<small>%</small></dd></div>
      </dl>
      <ul className="plain">
        <li>We found {number(check.recall*100,0)}% of the {number(check.prodes_ha,0)} ha INPE mapped as newly cleared.</li>
        <li>Only {number(check.precision*100,0)}% of our area matches theirs, because {number(check.false_positive_on_earlier_prodes_share*100,0)}% of the rest lies on land INPE had already marked as cleared before 2020. INPE never reports the same land twice, while we report any vegetation loss.</li>
        <li>This is one 25 km² site, not a general accuracy figure.</li>
      </ul>
      <p className="credit">{reference.replace(/_/g,' ')} · {check.source.layer} · fetched {check.source.fetched}</p></section>}
    {stats&&stats.from_to.length>0&&<section><h4>What this land was in 2021</h4>
      {stats.from_to.map(row=><div className="bar" key={row.from_class}><span>{row.from_class}</span><b>{number(row.ha,1)} ha</b>
        <i style={{width:Math.max(2,100*row.ha/stats.changed_ha)+'%'}}/></div>)}</section>}
    <section><h4>Confidence <Confidence packet={packet}/></h4>
      <div className="score"><i style={{width:packet.trust.score+'%'}} className={packet.trust.level.toLowerCase()}/></div>
      <ul className="plain">{packet.trust.caveats.map(c=><li key={c}>{c}</li>)}</ul>
      {stats&&stats.artefact_ha>0&&<p className="hint">{number(stats.artefact_ha,1)} ha of the change sits at cloud edges and is outlined in amber on the map.</p>}
      {packet.intent==='urban_growth'&&stats&&<p className="hint">Detector: {packet.trust.flags.includes('fresunet_assisted')?'FresUNet model plus index rules':'index rules'}.</p>}
      <details><summary>Rules and thresholds applied</summary><pre>{JSON.stringify({flags:packet.trust.flags,thresholds:packet.trust.thresholds},null,2)}</pre></details>
    </section>
    <section><h4>Images used</h4><Scenes packet={packet} ids/></section>
    {s.stages.length>0&&<section><h4>What the satellite did</h4><Timeline/></section>}
    <div className="actions"><a download={'answer-'+packet.analysis_id+'.json'} href={'data:application/json;charset=utf-8,'+encodeURIComponent(JSON.stringify(packet,null,2))}>
      <ArrowDownToLine size={15}/> Download the answer packet</a></div>
    <p className="credit">{packet.attribution.join(' · ')}</p>
  </>;
}

function DownlinkTab({packet}:{packet:Packet}) {
  const {mode,busy,set}=useStation();
  const link=packet.link;
  if (!link) return <p className="empty">No downlink was recorded for this answer.</p>;
  const raw=link.raw_equivalent_bytes;
  return <>
    <div className="hero"><div className="big">{bytes(link.packet_bytes)}<small>sent down</small></div>
      {raw!==null&&<p>instead of {bytes(raw)} of raw imagery · {number(raw/link.packet_bytes,0)}× smaller</p>}</div>
    {raw!==null&&<div className="sizes"><div><span>Answer packet</span><i style={{width:Math.max(0.6,100*link.packet_bytes/raw)+'%'}}/></div>
      <div><span>Raw images for both dates</span><i className="raw"/></div></div>}
    <section><h4>How long that takes over a real radio link</h4>
      <table><thead><tr><th>Link</th><th>Answer</th><th>Raw images</th></tr></thead><tbody>
        {(['uhf-9k6','sband-2m'] as Mode[]).map(key=><tr key={key}><td>{rates[key].label}<small>{rates[key].detail}</small></td>
          <td>{duration(link.packet_bytes*8/rates[key].bps)}</td><td>{raw===null?'—':duration(raw*8/rates[key].bps)}</td></tr>)}
      </tbody></table>
      <p className="hint">The full-size images on the map are fetched for display only. They are not part of the downlink.</p></section>
    <section><h4>Link used for the next question</h4>
      <div className="chips small">{(Object.keys(rates) as Mode[]).map(key=><button key={key} aria-pressed={mode===key} disabled={busy} onClick={()=>set({mode:key})}>
        {rates[key].label}<small>{rates[key].detail}</small></button>)}</div></section>
  </>;
}

function PassesTab({packet}:{packet:Packet}) {
  const [rows,setRows]=useState<OrbitPass[]|null>(null);
  const [note,setNote]=useState('');
  useEffect(()=>{
    const controller=new AbortController();
    const [lon,lat]=center(packet.aoi);
    api<OrbitPass[]>('/orbit/passes?lat='+lat+'&lon='+lon,undefined,controller.signal).then(setRows)
      .catch(error=>{if(!controller.signal.aborted){setRows([]);setNote(error.message);}});
    return ()=>controller.abort();
  },[packet.analysis_id]);
  const next=rows?.find(row=>row.cloud_pct!==null&&row.cloud_pct<20);
  if (rows===null) return <p className="empty"><Loader2 size={14} className="spin"/> Predicting the next overpasses…</p>;
  return <>
    {next ? <div className="hero"><div className="big">{new Date(next.time_utc).toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short'})}<small>next clear view</small></div>
      <p>{next.satellite} · {new Date(next.time_utc).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})} your time · {number(next.cloud_pct!,0)}% cloud forecast</p></div>
      : <p className="empty">{note||'No clear overpass is forecast in the next ten days.'}</p>}
    {rows.length>0&&<table><thead><tr><th>Satellite</th><th>When</th><th>Cloud forecast</th></tr></thead><tbody>
      {rows.map(row=><tr key={row.satellite+row.time_utc}><td>{row.satellite}</td><td title={row.time_utc}>{moment(row.time_utc)}</td>
        <td>{row.cloud_pct===null?'—':<><i className={cloudTone(row.cloud_pct)}/>{number(row.cloud_pct,0)}%</>}</td></tr>)}</tbody></table>}
    <p className="hint">Predicted from public orbit data (CelesTrak) and a weather forecast (Open-Meteo). Real acquisitions can differ.</p>
  </>;
}

function Timeline() {
  const stages=useStation(state=>state.stages);
  return <ol className="timeline">{stages.filter(row=>row.stage!=='done').map(row=><li key={row.stage} className={row.status}>
    <i>{row.status==='done'?<Check size={11}/>:row.status==='started'?<Loader2 size={11} className="spin"/>:<X size={11}/>}</i>
    <span>{stageNames[row.stage]||row.stage}{row.stage==='queued'&&row.status==='started'&&row.detail.ahead?<small> · {row.detail.ahead} ahead of you</small>:null}{row.stage==='cloud_check'&&row.detail.valid_overlap_pct!==undefined&&<small> · {number(row.detail.valid_overlap_pct,0)}% clear</small>}
      {row.stage==='downlink'&&row.detail.packet_bytes&&<small> · {bytes(row.detail.packet_bytes)}</small>}</span>
    <em>{row.replayed?'replayed · ':''}{row.ms!==null?(row.ms>=1000?number(row.ms/1000,1)+' s':number(row.ms,0)+' ms'):''}</em></li>)}</ol>;
}

const ORDER=['uplink','scene_select_a','scene_select_b','load','cloud_check','indices','change','landcover','trust','render','verdict','packet','downlink','explain'];
const hints:Record<string,string> = {
  queued:'Other questions are ahead of yours. The satellite answers one at a time.',
  uplink:'Your question is on its way up as a short command.',
  scene_select_a:'Looking through the real overpasses for the clearest "before" image.',
  scene_select_b:'Looking through the real overpasses for the clearest "after" image.',
  load:'Fetching both images. For a place it has not seen before this is the slow step and can take up to a minute.',
  cloud_check:'Checking how much of the area is hidden by cloud.',
  indices:'Turning the image bands into vegetation, water and built-up measures.',
  change:'Comparing the two dates pixel by pixel.',
  landcover:'Looking up what the land was before.',
  trust:'Scoring how far the result can be trusted.',
  render:'Drawing the before and after pictures.',
  verdict:'The onboard model is writing its one-sentence verdict.',
  packet:'Packing the answer for the radio link.',
  downlink:'The answer is coming down.',
  explain:'Writing the explanation.',
};

/** What the satellite is doing right now, how far along it is, and for how long. */
function Progress() {
  const s=useStation();
  const [seconds,setSeconds]=useState(0);
  useEffect(()=>{
    const start=Date.now();
    const timer=window.setInterval(()=>setSeconds(Math.round((Date.now()-start)/1000)),1000);
    return ()=>clearInterval(timer);
  },[]);
  const last=s.stages.at(-1);
  const queued=last?.stage==='queued'&&last.status==='started';
  const step=last ? ORDER.indexOf(last.stage)+(last.status==='done'?1:0.5) : 0.3;
  const share=Math.max(4,Math.min(97,100*Math.max(step,0.3)/ORDER.length));
  const ahead=queued ? last!.detail.ahead??0 : 0;
  return <div className="progress" role="status" aria-live="polite">
    <div className="radar"><i/><i/><i/><Satellite size={22}/></div>
    <h3>{queued ? (ahead>0?ahead+(ahead===1?' question is':' questions are')+' ahead of yours':'You are next') : last ? stageNames[last.stage] : 'Reading your question'}</h3>
    <p className="body">{last ? hints[last.stage]??'' : 'Working out the place, the two dates and what to look for.'}</p>
    <div className={'meter'+(queued?' waiting':'')}><i style={{width:share+'%'}}/></div>
    <div className="meter-foot"><span>{queued?'Waiting':'Step '+Math.max(1,Math.ceil(step))+' of '+ORDER.length}</span>
      <span>{seconds<60?seconds+' s':Math.floor(seconds/60)+' min '+(seconds%60)+' s'}</span></div>
    <p className="hint">{seconds>45 ? 'Still working. A place the satellite has not seen before can take over a minute.' : 'Usually 5 to 40 seconds. Keep this page open.'}</p>
  </div>;
}

const outcomes:Record<string,{label:string;icon:React.ReactNode}> = {
  alert:{label:'Alert',icon:<BellRing size={13}/>},quiet:{label:'Nothing to report',icon:<Minus size={13}/>},
  cloudy:{label:'Too cloudy',icon:<Cloud size={13}/>},no_scene:{label:'No image',icon:<CloudOff size={13}/>},
  error:{label:'Failed',icon:<X size={13}/>},processing:{label:'Satellite is looking…',icon:<Loader2 size={13} className="spin"/>},
};
const statusText:Record<Watch['status'],string> = {starting:'Finding a clear reference image…',running:'Replaying overpasses…',
  armed:'Waiting for the next overpass',done:'Finished',stopped:'Stopped',failed:'Could not start'};

function WatchView({watch,health}:{watch:Watch;health:Health|null}) {
  const s=useStation();
  const list=useRef<HTMLOListElement>(null);
  const live=['starting','running','armed'].includes(watch.status);
  const totals=watch.totals;
  const selected=s.result&&watch.entries.find(e=>e.date===s.watchRow)?.analysis_id===s.result.packet.analysis_id ? s.result.packet : null;
  useEffect(()=>{if(watch.status==='running') list.current?.lastElementChild?.scrollIntoView({block:'nearest',behavior:'smooth'});},[watch.entries.length]);
  return <>
    <div className="watch-head">
      <span className={'pill '+watch.status}>{live&&<i/>}{statusText[watch.status]}</span>
      {live&&<button className="link" onClick={stopWatch}><Square size={11}/> Stop</button>}
      <h3>{watch.question||'Watching for '+intents[watch.intent].short}</h3>
      <p className="body">{watch.aoi.name||'Selected area'} · alert at {number(watch.floor_ha)} ha of {intents[watch.intent].short}
        {watch.baseline&&<> · compared with {day(watch.baseline.date)}</>}</p>
      {watch.message&&<div className="alert">{watch.message}</div>}
    </div>
    {totals.passes>0&&<div className="watch-totals">
      <div><b>{totals.passes}</b><span>overpasses</span></div>
      <div className="alert-count"><b>{totals.alerts}</b><span>{totals.alerts===1?'alert':'alerts'}</span></div>
      <div><b>{bytes(totals.sent_bytes)}</b><span>sent down</span></div>
      <div><b>{totals.raw_bytes?bytes(totals.raw_bytes):'—'}</b><span>if sent raw</span></div>
    </div>}
    <ol className="watch-list" ref={list}>{watch.entries.map(entry=><li key={entry.date} className={'w-'+entry.outcome+(s.watchRow===entry.date?' on':'')}>
      <button disabled={!entry.analysis_id} onClick={()=>{s.set({watchRow:entry.date});if(entry.analysis_id) openAnalysis(entry.analysis_id);}}>
        <i>{outcomes[entry.outcome].icon}</i>
        <span><b>{day(entry.date)}</b>{outcomes[entry.outcome].label}{entry.outcome==='alert'&&entry.changed_ha!==null&&<> · {number(entry.changed_ha,0)} ha</>}
          <small>{entry.verdict}</small></span>
        <em>{entry.packet_bytes!==null?bytes(entry.packet_bytes):''}</em>
      </button></li>)}</ol>
    {watch.status==='armed'&&<p className="hint">Caught up to today. New overpasses are checked automatically every 30 minutes.</p>}
    {selected&&selected.report==='full'&&<div className="watch-detail"><span className="label">Report for {day(s.watchRow)}</span>
      <AnswerTab packet={selected} health={health} compact/></div>}
    {selected&&selected.report==='quiet'&&<div className="watch-detail"><span className="label">{day(s.watchRow)}</span>
      <p className="body">{selected.onboard_verdict}</p><p className="hint">No images were sent down for this pass, so there is nothing to show on the map.</p></div>}
  </>;
}

export {Hero, Confidence, Timeline, Progress};

export default function Answer({health}:{health:Health|null}) {
  const s=useStation();
  const packet=s.result?.packet;
  if (s.watch) return <aside className="panel answer" aria-label="Standing watch">
    <header><span className="label"><BellRing size={12}/> Standing watch</span>
      <button className="close" aria-label="Close" onClick={()=>{stopWatch();s.reset();}}><X size={15}/></button></header>
    <div className="scroll"><WatchView watch={s.watch} health={health}/></div>
  </aside>;
  if (!packet&&!s.busy) return null;
  return <aside className="panel answer" aria-label="Answer">
    {!packet ? <>
      <header><span className="label"><Satellite size={12}/> Satellite at work</span></header>
      <div className="scroll"><Progress/><Timeline/></div>
    </> : <>
      <header><nav role="tablist">{tabs.map(tab=><button key={tab} role="tab" aria-selected={s.tab===tab} onClick={()=>s.set({tab})}>{tab}</button>)}</nav>
        <button className="close" aria-label="Close" onClick={()=>s.reset()}><X size={15}/></button></header>
      <div className="scroll">
        {s.tab==='Answer'&&<AnswerTab packet={packet} health={health}/>}
        {s.tab==='Evidence'&&<EvidenceTab packet={packet}/>}
        {s.tab==='Downlink'&&<DownlinkTab packet={packet}/>}
        {s.tab==='Next passes'&&<PassesTab packet={packet}/>}
      </div>
    </>}
  </aside>;
}
