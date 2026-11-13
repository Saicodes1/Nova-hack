import {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity, ArrowDownToLine, ArrowRight, CircleHelp, Clock3, Database, Layers3, Radio, Satellite, Send, ShieldCheck} from 'lucide-react';
import MapPanel from './MapPanel';
import ComparisonDownload from './ComparisonDownload';
import {useStation, type Tab} from './state';
import {api, areaKm, center, featureBounds, intentColor, intentNoun, intents, moment, number, type Demo, type Intent, type Language,
  type Mode, type Packet, type Pass, type Place, type Result, type Stage, type Telemetry, type Tracks} from './types';
import './style.css';

const tabs = [{name:'Ask',icon:CircleHelp},{name:'Evidence',icon:Layers3},{name:'Orbit',icon:Activity},{name:'Link',icon:Radio}] as const;
const stages: Record<string,string> = {uplink:'Uplink',scene_select_a:'Find before scene',scene_select_b:'Find after scene',
  load:'Load bands',cloud_check:'Check cloud cover',indices:'Calculate indices',change:'Detect change',landcover:'Add land-cover context',
  trust:'Score confidence',render:'Render imagery',verdict:'Build verdict',packet:'Assemble packet',downlink:'Downlink',explain:'Write answer',done:'Complete',error:'Analysis failed'};
const rates: Record<Mode,number> = {'demo-fast':2_000_000,'sband-2m':2_000_000,'uhf-9k6':9600};
const bytes=(size:number)=>size>=1_000_000?number(size/1_000_000,2)+' MB':size>=1000?number(size/1000,2)+' kB':number(size,0)+' B';

function App() {
  const station=useStation();
  const {set,stage}=station;
  const [demos,setDemos]=useState<Demo[]>([]);
  const [telemetry,setTelemetry]=useState<Telemetry|null>(null);
  const [online,setOnline]=useState<boolean|null>(null);
  const [openai,setOpenai]=useState<{planner:boolean;explainer:boolean}|null>(null);
  const [offline,setOffline]=useState(false);
  const [demoError,setDemoError]=useState('');
  const [passRows,setPassRows]=useState<Pass[]|null>(null);
  const [orbitNote,setOrbitNote]=useState('');
  const [trackData,setTrackData]=useState<Tracks|null>(null);
  const [trackNote,setTrackNote]=useState('');
  const [chat,setChat]=useState<{role:string;text:string}[]>([]);
  const [message,setMessage]=useState('');
  const [chatting,setChatting]=useState(false);
  const stream=useRef<EventSource|null>(null);
  const fetchedMode=useRef(false);
  const chatVersion=useRef(0);

  function applyDemo(demo:Demo) {
    set({demoId:demo.id,demoSynthetic:demo.synthetic,question:demo.question,aoi:demo.aoi,dateA:demo.date_a,dateB:demo.date_b,
      intent:demo.intent,error:'',result:null,stages:[],currentId:null,swipe:50});
  }

  useEffect(()=>{
    const controller=new AbortController();
    api<{cases:Demo[]}>('/demo-cases',undefined,controller.signal).then(({cases})=>{
      setDemos(cases);
      const current=useStation.getState();
      if (cases[0]&&!current.question&&!current.aoi) applyDemo(cases[0]);
    }).catch(()=>{if(!controller.signal.aborted) setDemoError('Demo cases are unavailable. Check the ground server.');});
    api<{openai:{planner:boolean;explainer:boolean};offline?:boolean}>('/health',undefined,controller.signal).then(data=>{setOpenai(data.openai);setOffline(Boolean(data.offline));}).catch(()=>{});
    function poll(){
      api<Telemetry>('/telemetry',undefined,controller.signal).then(data=>{
        setTelemetry(data);setOnline(true);
        if (!fetchedMode.current) {
          fetchedMode.current=true;
          if (data.link_mode in rates) set({mode:data.link_mode});
        }
      }).catch(()=>{if(!controller.signal.aborted) setOnline(false)});
    }
    poll();
    const interval=window.setInterval(poll,2000);
    return ()=>{controller.abort();clearInterval(interval);stream.current?.close();};
  },[]);

  useEffect(()=>{
    if (station.tab!=='Orbit'||!station.aoi) return;
    const controller=new AbortController();
    const [lon,lat]=center(station.aoi);
    setPassRows(null);setOrbitNote('Loading predicted passes…');
    api<Pass[]>('/orbit/passes?lat='+lat+'&lon='+lon,undefined,controller.signal).then(data=>{
      setPassRows(data);setOrbitNote('');
    }).catch(error=>{if(!controller.signal.aborted)setOrbitNote(error.message)});
    return ()=>controller.abort();
  },[station.tab,station.aoi]);

  useEffect(()=>{
    if (!station.tracks) return;
    const controller=new AbortController();
    setTrackNote('Loading ground tracks…');
    api<Tracks>('/orbit/tracks',undefined,controller.signal).then(data=>{setTrackData(data);setTrackNote('')})
      .catch(error=>{if(!controller.signal.aborted)setTrackNote(error.message)});
    return ()=>controller.abort();
  },[station.tracks]);

  function follow(id:string) {
    set({currentId:id,busy:true,error:'',stages:[],result:null,selectedPatch:null,focus:null});
    stream.current?.close();
    const source = new EventSource('/api/analyses/'+id+'/events');
    stream.current=source;
    for (const key of Object.keys(stages)) {
      source.addEventListener(key,event=>{
        if (!(event instanceof MessageEvent)) {
          if (key==='error'&&stream.current===source) {
            source.close();set({busy:false,error:'The progress stream disconnected. Run the analysis again to recover.'});
          }
          return;
        }
        const row=JSON.parse(event.data) as Stage;
        stage(row);
        if (key==='error') {
          source.close();set({busy:false,error:row.detail.message||'Analysis failed.'});
        }
        if (key==='done') {
          source.close();
          api<Result>('/analyses/'+id).then(result=>{
            if (stream.current===source) set({result,aoi:result.packet.aoi,busy:false});
          }).catch(error=>set({busy:false,error:error.message}));
        }
      });
    }
  }

  async function submit(event:React.FormEvent) {
    event.preventDefault();
    if (station.busy) return;
    if (station.aoi&&areaKm(station.aoi)>50.1) {set({error:'Select an area of 50 km² or less.'});return;}
    if (station.dateA&&station.dateB&&station.dateA>=station.dateB) {set({error:'The before date must precede the after date.'});return;}
    set({busy:true,error:''});
    try {
      const reply=await api<{analysis_id?:string;clarification?:string}>('/ask',{
        question:station.question,aoi:station.aoi,date_a:station.dateA||null,date_b:station.dateB||null,
        intent:station.intent||null,language:station.language,demo_id:station.demoId,link_mode:station.mode,
      });
      if (reply.analysis_id) {
        chatVersion.current++;
        setChat([]);setMessage('');setChatting(false);follow(reply.analysis_id);
      } else {
        set({error:reply.clarification||'Add more details to the question.',busy:false});
      }
    } catch(error) {set({error:(error as Error).message,busy:false});}
  }

  async function send(event:React.FormEvent) {
    event.preventDefault();
    const text=message.trim(), version=chatVersion.current;
    if (!station.currentId||!text||chatting) return;
    setChat(rows=>[...rows,{role:'You',text}]);setMessage('');setChatting(true);
    try {
      const result=await api<{reply:string;new_analysis_id?:string}>('/chat',{
        analysis_id:station.currentId,message:text,language:station.language,
      });
      if (version!==chatVersion.current) return;
      setChat(rows=>[...rows,{role:'Satellite',text:result.reply}]);
      if (result.new_analysis_id) follow(result.new_analysis_id);
    } catch(error) {
      if (version===chatVersion.current) setChat(rows=>[...rows,{role:'Satellite',text:(error as Error).message}]);
    } finally {if (version===chatVersion.current) setChatting(false);}
  }

  const packet=station.result?.packet;
  const analysisSource=packet?.scenes.some(scene=>scene.source==='synthetic');
  const active=station.stages.at(-1);
  const next=passRows?.find(row=>row.cloud_pct!==null&&row.cloud_pct<20);
  const completeForm=Boolean(station.aoi&&station.dateA&&station.dateB&&station.intent);
  return <div className="app">
    <header className="topbar">
      <div className="identity"><span className="logo"><Satellite size={23}/></span><div><h1>Ask the Satellite</h1><p>Sentinel-2 Ground Station</p></div></div>
      <div className="telemetry">
        {offline&&<span className="node-state">Offline demo</span>}
        <span className="node-state"><i className={online?'dot online':'dot'}/>{online ? (telemetry?.mode==='remote'?'Remote node':'Local node') : online===false?'Node unavailable':'Connecting'}{online&&telemetry?.state!=='idle'?' · '+telemetry?.state:''}</span>
        <span><Radio size={15}/> {station.mode==='uhf-9k6'?'UHF':'S-band'} · {station.mode==='uhf-9k6'?'9.6 kbps':'2 Mbps'}</span>
        {telemetry?.power_w!=null&&<span>{telemetry.power_w} W</span>}
        {telemetry?.temp_c!=null&&<span>{telemetry.temp_c} °C</span>}
      </div>
    </header>
    <main className="workspace">
      <div className="main-side">
        <div className="map-heading"><div><h2>Observation area</h2><p>{packet?.aoi.name || station.aoi?.name || (station.aoi ? 'Drawn area · '+number(areaKm(station.aoi))+' km²' : 'Search, draw a rectangle or a polygon')}</p></div>
          <span className="source-tag"><Satellite size={16}/>{(station.demoId&&station.demoSynthetic)||analysisSource?'Synthetic demonstration':'Sentinel-2 imagery'}</span>
        </div>
        <MapPanel trackData={trackData}/>
      </div>
      <aside className="side">
        <nav aria-label="Analysis panels" className="tabs">
          {tabs.map(({name,icon:Icon})=><button key={name} aria-current={station.tab===name?'page':undefined}
            onClick={()=>set({tab:name as Tab})}><Icon size={16}/>{name}</button>)}
        </nav>
        <div className="side-content">
          {station.tab==='Ask'&&<>
            <div className="section-heading"><h2>Ask the satellite</h2><p>Pick an area and two dates, then ask what changed.</p></div>
            {!!station.stages.length&&<Pipeline rows={station.stages} busy={station.busy}/>}
            {station.error&&<div className="alert" role="alert">{station.error}</div>}
            {station.result&&packet&&<>
              <article className={'answer '+(packet.trust.level==='Low'?'low':'')} dir={station.result.plan.language==='ar'?'rtl':'ltr'}>
                <div className="answer-label"><ShieldCheck size={16}/> Answer <Confidence level={packet.trust.level}/></div>
                {packet.trust.level==='Low'&&station.result.explanation.caveats[0]&&<p className="warning">{station.result.explanation.caveats[0]}</p>}
                <h3>{station.result.explanation.headline}</h3>
                <StatChips packet={packet}/><p>{station.result.explanation.body}</p>
                <ul>{station.result.explanation.caveats.slice(packet.trust.level==='Low'?1:0).map(c=><li key={c}>{c}</li>)}</ul>
                <div className="answer-scenes">{packet.scenes.map(scene=><p key={scene.role}><b>{scene.role==='A'?'Before':'After'}</b> <span title={scene.datetime}>{moment(scene.datetime)}</span> · {number(scene.aoi_cloud_pct)}% cloud<br/><code>{scene.item_ids.join(', ')}</code></p>)}</div>
                {packet.status==='refused_cloudy'&&!analysisSource&&<button onClick={()=>set({tab:'Orbit'})}>Check predicted passes <ArrowRight size={15}/></button>}
                <ComparisonDownload key={packet.analysis_id} packet={packet}/>
              </article>
              <PatchList packet={packet}/>
              {openai?.explainer&&!analysisSource&&<section className="follow-up">
                <h3>Follow-up question</h3>
                {chat.map((entry,i)=><p className="chat-row" key={i}><b>{entry.role}</b> {entry.text}</p>)}
                <form onSubmit={send}><input value={message} onChange={e=>setMessage(e.target.value)} aria-label="Follow-up question" maxLength={2000} placeholder="Ask about this analysis"/>
                  <button aria-label="Send follow-up" disabled={chatting||!message.trim()||station.busy}><Send size={17}/></button></form>
                {chatting&&<p className="field-note" role="status">Looking up evidence…</p>}
              </section>}
            </>}
            {station.result&&!station.busy ? <details className="query">
              <summary>New question or different dates</summary>
            <div className="case-select">
              <label htmlFor="case">Data source</label>
              <select id="case" value={station.demoId||''} disabled={station.busy} onChange={e=>{
                const selected=demos.find(d=>d.id===e.target.value);
                if(selected)applyDemo(selected);else set({demoId:null,error:''});
              }}>
                <option value="">Live Sentinel-2</option>
                {demos.map(demo=><option key={demo.id} value={demo.id}>Demo · {demo.title}</option>)}
              </select>
              {demoError&&<small role="status">{demoError}</small>}
            </div>
            <form onSubmit={submit} className="ask-form">
              <label>Question<textarea value={station.question} maxLength={2000} placeholder="Did surface water expand after the storm?"
                onChange={e=>set({question:e.target.value,demoId:null})} disabled={station.busy}/></label>
              <div className="field-grid">
                <label>Before<input type="date" value={station.dateA} onChange={e=>set({dateA:e.target.value,demoId:null})} disabled={station.busy}/></label>
                <label>After<input type="date" value={station.dateB} onChange={e=>set({dateB:e.target.value,demoId:null})} disabled={station.busy}/></label>
              </div>
              <div className="field-grid intent-grid">
                <label>Analysis type<select value={station.intent} disabled={station.busy} onChange={e=>set({intent:e.target.value as Intent|'',demoId:null})}>
                  <option value="">Infer from question</option>{Object.entries(intents).map(([value,label])=><option key={value} value={value}>{label}</option>)}
                </select></label>
                <label>Language<select value={station.language} disabled={station.busy} onChange={e=>set({language:e.target.value as Language})}>
                  <option value="en">English</option><option value="ar">العربية</option><option value="hi">हिन्दी</option>
                </select></label>
              </div>
              {station.demoId&&station.demoSynthetic&&<p className="field-note">This demo uses synthetic imagery.</p>}
              {!station.demoId&&openai&&!openai.planner&&!completeForm&&<p className="field-note">Select an area, two dates, and an analysis type to run without OpenAI.</p>}
              <button className="primary" disabled={station.busy||(!station.question.trim()&&!completeForm)}><Satellite size={17}/>
                {station.busy?(active?stages[active.stage]:'Planning…'):station.demoId?'Run demo':'Ask the satellite'}<ArrowRight size={16}/></button>
            </form>
            </details> : <div className="query-open">
            <div className="case-select">
              <label htmlFor="case">Data source</label>
              <select id="case" value={station.demoId||''} disabled={station.busy} onChange={e=>{
                const selected=demos.find(d=>d.id===e.target.value);
                if(selected)applyDemo(selected);else set({demoId:null,error:''});
              }}>
                <option value="">Live Sentinel-2</option>
                {demos.map(demo=><option key={demo.id} value={demo.id}>Demo · {demo.title}</option>)}
              </select>
              {demoError&&<small role="status">{demoError}</small>}
            </div>
            <form onSubmit={submit} className="ask-form">
              <label>Question<textarea value={station.question} maxLength={2000} placeholder="Did surface water expand after the storm?"
                onChange={e=>set({question:e.target.value,demoId:null})} disabled={station.busy}/></label>
              <div className="field-grid">
                <label>Before<input type="date" value={station.dateA} onChange={e=>set({dateA:e.target.value,demoId:null})} disabled={station.busy}/></label>
                <label>After<input type="date" value={station.dateB} onChange={e=>set({dateB:e.target.value,demoId:null})} disabled={station.busy}/></label>
              </div>
              <div className="field-grid intent-grid">
                <label>Analysis type<select value={station.intent} disabled={station.busy} onChange={e=>set({intent:e.target.value as Intent|'',demoId:null})}>
                  <option value="">Infer from question</option>{Object.entries(intents).map(([value,label])=><option key={value} value={value}>{label}</option>)}
                </select></label>
                <label>Language<select value={station.language} disabled={station.busy} onChange={e=>set({language:e.target.value as Language})}>
                  <option value="en">English</option><option value="ar">العربية</option><option value="hi">हिन्दी</option>
                </select></label>
              </div>
              {station.demoId&&station.demoSynthetic&&<p className="field-note">This demo uses synthetic imagery.</p>}
              {!station.demoId&&openai&&!openai.planner&&!completeForm&&<p className="field-note">Select an area, two dates, and an analysis type to run without OpenAI.</p>}
              <button className="primary" disabled={station.busy||(!station.question.trim()&&!completeForm)}><Satellite size={17}/>
                {station.busy?(active?stages[active.stage]:'Planning…'):station.demoId?'Run demo':'Ask the satellite'}<ArrowRight size={16}/></button>
            </form>
            </div>}
            {!!station.stages.length&&<details className="timeline" open={station.busy||undefined}>
              <summary><Clock3 size={16}/><span>Stage details</span><small>{active ? stages[active.stage] : ''}</small></summary>
              <ol>{station.stages.map(row=><li key={row.stage}><span className={'stage-dot '+row.status}/><span>{stages[row.stage]||row.stage}</span>
                {row.replayed&&<small>Replayed</small>}<span>{row.ms!==null?number(row.ms,0)+' ms':row.status}</span></li>)}</ol>
            </details>}
          </>}
          {station.tab==='Evidence'&&<Evidence packet={packet}/>}
          {station.tab==='Orbit'&&<>
            <div className="section-heading"><h2>Predicted passes</h2><p>Overpass geometry from CelesTrak. Cloud forecast from Open-Meteo.</p></div>
            <label className="check"><input type="checkbox" checked={station.tracks} onChange={e=>set({tracks:e.target.checked})}/> Show orbital ground tracks</label>
            {station.tracks&&trackNote&&<p className="field-note">{trackNote}</p>}
            {next&&<div className="notice clear-pass"><b>Next predicted clear pass</b><p>{next.satellite} · <span title={next.time_utc}>{moment(next.time_utc)}</span> · {next.cloud_pct}% cloud</p></div>}
            {!station.aoi?<p className="empty">Select an observation area to see upcoming passes.</p>:orbitNote?<p className="empty" role="status">{orbitNote}</p>:
              passRows?.length?<table><thead><tr><th>Satellite / local time</th><th>Offset</th><th>Cloud</th></tr></thead><tbody>{passRows.map(row=>
                <tr key={row.satellite+row.time_utc}><td>{row.satellite}<small title={row.time_utc}>{moment(row.time_utc)}</small></td>
                  <td>{number(row.cross_track_km)} km</td><td>{row.cloud_pct===null?'—':number(row.cloud_pct)+'%'}</td></tr>)}</tbody></table>:
                <p className="empty">No predicted daytime passes in the forecast window.</p>}
          </>}
          {station.tab==='Link'&&<LinkPanel packet={packet}/>}
        </div>
      </aside>
    </main>
    <footer><span>Basemap © OpenStreetMap contributors, OpenFreeMap</span><span>Weather and geocoding by Open-Meteo</span><span>Orbital elements by CelesTrak</span>
      {packet?.attribution.map(item=><span key={item}>{item}</span>)}</footer>
  </div>;
}

function Confidence({level}:{level:string}) {return <span className={'confidence '+level.toLowerCase()}>{level} confidence</span>}

function Evidence({packet}:{packet?:Packet}) {
  return <>
    <div className="section-heading"><h2>Evidence</h2><p>Change measurements, confidence, and image provenance.</p></div>
    {!packet?<p className="empty">Run an analysis to view its evidence.</p>:<>
      {packet.stats&&<>
        <dl className="metrics"><div><dt>Detected change</dt><dd>{number(packet.stats.changed_ha,2)} ha</dd></div>
          <div><dt>Share of clear coverage</dt><dd>{number(packet.stats.changed_pct)}%</dd></div>
          <div><dt>Clear overlap</dt><dd>{number(packet.stats.valid_overlap_pct)}%</dd></div>
          <div><dt>Detected patches</dt><dd>{number(packet.stats.n_polygons,0)}</dd></div></dl>
        <PatchList packet={packet}/>
        {packet.stats.from_to.length>0&&<section className="evidence-section"><h3>Previous land cover</h3>
          {packet.stats.from_to.map(row=><div className="bar-row" key={row.from_class}>
            <span>{row.from_class}</span><b>{number(row.ha,2)} ha</b>
            <progress value={row.ha} max={packet.stats!.changed_ha} aria-label={row.from_class}/>
          </div>)}
        </section>}
      </>}
      <section className="evidence-section"><h3>Confidence <Confidence level={packet.trust.level}/></h3>
        {packet.intent==='urban_growth'&&packet.stats&&<p className="field-note">Detector · {packet.trust.flags.includes('fresunet_assisted')?'Built-up evidence + FresUNet':'Built-up evidence (colour + texture)'}</p>}
        {packet.intent==='visible_change'&&packet.stats&&<p className="field-note">Detector · FresUNet</p>}
        <div className="score"><progress value={packet.trust.score} max={100} aria-label="Confidence score"/><b>{packet.trust.score}/100</b></div>
        <ul>{packet.trust.caveats.map(caveat=><li key={caveat}>{caveat}</li>)}</ul>
        <details><summary>Applied thresholds and flags</summary><pre>{JSON.stringify({flags:packet.trust.flags,thresholds:packet.trust.thresholds},null,2)}</pre></details>
      </section>
      <section className="evidence-section"><h3>Acquisition scenes</h3>
        {packet.scenes.length===0&&<p>No usable scenes were returned.</p>}
        {packet.scenes.map(scene=><article className="scene" key={scene.role}><h4>{scene.role==='A'?'Before':'After'} <span>{number(scene.aoi_cloud_pct)}% cloud</span></h4>
          <p title={scene.datetime}>{moment(scene.datetime)}</p><p>Requested {scene.requested_date} · {scene.source}</p>
          {scene.item_ids.map(id=><code key={id}>{id}</code>)}</article>)}
      </section>
      <a className="download" download={'answer-'+packet.analysis_id+'.json'} href={'data:application/json;charset=utf-8,'+encodeURIComponent(JSON.stringify(packet,null,2))}>
        <ArrowDownToLine size={16}/> Download answer packet
      </a>
      <ComparisonDownload key={packet.analysis_id} packet={packet}/>
    </>}
  </>;
}

function LinkPanel({packet}:{packet?:Packet}) {
  const {mode,busy,set}=useStation();
  const link=packet?.link;
  const rate=rates[mode];
  return <>
    <div className="section-heading"><h2>Downlink</h2><p>Compare the answer packet with the full imagery at the selected bitrate.</p></div>
    <label>Link mode<select value={mode} disabled={busy} onChange={e=>set({mode:e.target.value as Mode})}>
      <option value="demo-fast">Demo fast · 2 Mbps</option><option value="sband-2m">S-band · 2 Mbps</option><option value="uhf-9k6">UHF · 9.6 kbps</option>
    </select></label>
    {!link?<p className="empty">Run an analysis to measure its answer packet.</p>:<>
      <dl className="metrics link-metrics"><div><dt><Database size={15}/> Answer packet</dt><dd>{bytes(link.packet_bytes)}</dd></div>
        <div><dt>Full scene assets</dt><dd>{link.raw_equivalent_bytes===null?'Unavailable':bytes(link.raw_equivalent_bytes)}</dd></div>
        <div><dt>Packet transfer estimate</dt><dd>{number(link.packet_bytes*8/rate,3)} s</dd></div>
        <div><dt>Full scene transfer estimate</dt><dd>{link.raw_equivalent_bytes===null?'Unavailable':number(link.raw_equivalent_bytes*8/rate,2)+' s'}</dd></div>
        {link.saved_pct!==null&&<div><dt>Reduction</dt><dd>{number(link.saved_pct,3)}%</dd></div>}</dl>
      <p className="field-note">This analysis ran with {link.mode}. The selector also previews other bitrates.</p>
    </>}
  </>;
}


const phases=[
  {key:'ground',label:'Ground',detail:'Plan',stages:['uplink']},
  {key:'onboard',label:'Satellite',detail:'Onboard analysis',stages:['scene_select_a','scene_select_b','load','cloud_check','indices','change','landcover','trust','render','verdict','packet']},
  {key:'downlink',label:'Downlink',detail:'Answer packet',stages:['downlink']},
  {key:'answer',label:'Answer',detail:'Explain',stages:['explain','done']},
];

function Pipeline({rows,busy}:{rows:Stage[];busy:boolean}) {
  const failed=rows.some(row=>row.stage==='error'||row.status==='failed');
  return <ol className="pipeline" aria-label="Analysis pipeline">
    {phases.map(phase=>{
      const mine=rows.filter(row=>phase.stages.includes(row.stage));
      const done=phase.key==='onboard' ? mine.some(row=>row.stage==='packet'&&row.status==='done')
        : mine.length>0 && mine.every(row=>row.status==='done');
      const refused=mine.some(row=>row.status==='refused');
      const active=!done && mine.length>0 && busy;
      const ms=mine.reduce((sum,row)=>sum+(row.status==='done'&&row.ms?row.ms:0),0);
      const state=failed&&!done&&mine.length?'failed':refused?'refused':done?'done':active?'active':'idle';
      return <li key={phase.key} className={'phase '+state}>
        <span className="phase-dot"/><b>{phase.label}</b>
        <small>{state==='done'&&ms?number(ms/1000,1)+' s':state==='active'?'working…':state==='refused'?'too cloudy':phase.detail}</small>
      </li>;
    })}
  </ol>;
}

function StatChips({packet}:{packet:Packet}) {
  const stats=packet.stats;
  if (!stats) return null;
  return <div className="chips">
    <span className="chip strong" style={{borderColor:intentColor[packet.intent]}}>
      <i style={{background:intentColor[packet.intent]}}/>{number(stats.changed_ha,1)} ha {intentNoun[packet.intent]}</span>
    <span className="chip">{number(stats.changed_pct)}% of clear area</span>
    <span className="chip">{number(stats.n_polygons,0)} {stats.n_polygons===1?'patch':'patches'}</span>
  </div>;
}

function PatchList({packet}:{packet:Packet}) {
  const {selectedPatch,set}=useStation();
  const features=packet.change_geojson?.features ?? [];
  if (!features.length) return null;
  const order=features.map((feature,index)=>({index,ha:Number(feature.properties?.ha ?? 0),artefact:Boolean(feature.properties?.artefact)}))
    .sort((a,b)=>b.ha-a.ha).slice(0,8);
  const largest=order[0]?.ha || 1;
  return <section className="patch-list">
    <h3>Largest patches <small>click to zoom</small></h3>
    <ul>{order.map((row,rank)=><li key={row.index}>
      <button aria-pressed={selectedPatch===row.index} onClick={()=>set({selectedPatch:row.index,focus:featureBounds(features[row.index].geometry)})}>
        <span className="rank">{rank+1}</span>
        <span className="bar"><i style={{width:(100*row.ha/largest)+'%',background:row.artefact?'#9aa4b2':intentColor[packet.intent]}}/></span>
        <b>{number(row.ha,2)} ha</b>{row.artefact&&<small>edge</small>}
      </button></li>)}</ul>
  </section>;
}

createRoot(document.getElementById('root')!).render(<App/>);
