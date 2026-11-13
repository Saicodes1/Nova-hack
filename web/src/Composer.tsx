import {useEffect, useRef, useState} from 'react';
import {ArrowRight, BellRing, Check, Cloud, CloudOff, MapPin, MousePointerClick, Pencil, Pentagon, Search, Square, X} from 'lucide-react';
import {applyPreset, applyWatchPreset, ask, missing, startWatch, stageNames} from './actions';
import {useStation} from './state';
import {api, areaKm, cloudTone, day, intents, number, shift, today, type AOI, type Demo, type Intent, type Language,
  type Overpass, type Place, type WatchPreset} from './types';

const passCache = new Map<string,Overpass[]>();

/** Real overpasses around a date, each with the cloud cover over the chosen area. */
function PassStrip({aoi,date,picked,from,until,onPick}:{aoi:AOI;date:string;picked:boolean;from?:string;until?:string;onPick:(date:string)=>void}) {
  const [rows,setRows]=useState<Overpass[]|null>(null);
  const [note,setNote]=useState('');
  const start=shift(date,-22), end=shift(date,22)>today()?today():shift(date,22);
  const key=JSON.stringify(aoi.geometry.coordinates)+start+end;
  useEffect(()=>{
    if (start>end) {setRows([]);return;}
    if (passCache.has(key)) {setRows(passCache.get(key)!);setNote('');return;}
    const controller=new AbortController();
    setRows(null);setNote('');
    const timer=window.setTimeout(()=>{
      api<Overpass[]>('/passes',{aoi,start,end},controller.signal).then(data=>{passCache.set(key,data);setRows(data);})
        .catch(error=>{if(!controller.signal.aborted){setRows([]);setNote(error.message);}});
    },350);
    return ()=>{clearTimeout(timer);controller.abort();};
  },[key]);
  if (rows===null) return <div className="passes loading"><i/><span>Reading cloud cover for each overpass…</span></div>;
  const shown=rows.filter(row=>(!from||row.date>from)&&(!until||row.date<until));
  if (!shown.length) return <p className="hint">{note||'No Sentinel-2 overpass found within three weeks of this date.'}</p>;
  return <div className="passes" role="listbox" aria-label="Overpasses near this date">
    {shown.map(row=><button type="button" role="option" key={row.date} aria-selected={picked&&row.date===date}
      className={cloudTone(row.cloud_pct)} title={row.platform+' · '+number(row.cloud_pct)+'% of the area under cloud'} onClick={()=>onPick(row.date)}>
      <span>{day(row.date,false)}</span><i/><small>{number(row.cloud_pct,0)}%</small>
    </button>)}
  </div>;
}

function Step({n,title,value,open,done,onToggle,children}:{n:number;title:string;value:React.ReactNode;open:boolean;done:boolean;
  onToggle:()=>void;children:React.ReactNode}) {
  return <section className={'step'+(open?' open':'')+(done?' done':'')}>
    <button type="button" className="step-head" onClick={onToggle} aria-expanded={open}>
      <span className="step-n">{done?<Check size={13}/>:n}</span>
      <span className="step-title">{title}</span>
      <span className="step-value">{value}</span>
      <Pencil size={13} className="step-edit"/>
    </button>
    {open&&<div className="step-body">{children}</div>}
  </section>;
}

export default function Composer({demos,presets,watches}:{demos:Demo[];presets:Demo[];watches:WatchPreset[]}) {
  const s=useStation();
  const {set}=s;
  const [open,setOpen]=useState<number|null>(null);
  const [search,setSearch]=useState('');
  const [places,setPlaces]=useState<Place[]>([]);
  const [searchNote,setSearchNote]=useState('');
  const [searching,setSearching]=useState(false);
  const question=useRef<HTMLTextAreaElement>(null);
  const [casesOpen,setCasesOpen]=useState(true);
  const started=Boolean(s.busy||s.result||s.watch);
  useEffect(()=>{if(started) setCasesOpen(false);},[started]);
  const watching=s.task==='watch';
  const gaps=missing();
  const active=s.stages.at(-1);
  const locked=s.busy;
  const edit=(patch:Parameters<typeof set>[0])=>set({...patch,demoId:null,presetId:null});
  const toggle=(n:number)=>setOpen(open===n?null:n);

  useEffect(()=>{if(s.draw) setOpen(null);},[s.draw]);

  async function find(event:React.FormEvent) {
    event.preventDefault();
    if (search.trim().length<2) return;
    setSearching(true);setPlaces([]);setSearchNote('');
    try {
      const result=await api<Place[]>('/geocode?q='+encodeURIComponent(search.trim()));
      setPlaces(result);
      if (!result.length) setSearchNote('No place found with that name.');
    } catch(error) {setSearchNote((error as Error).message);}
    finally {setSearching(false);}
  }
  function choose(place:Place) {
    edit({aoi:{...place.aoi,name:[place.name,place.country].filter(Boolean).join(', ')},exactA:false,exactB:false});
    s.reset();setPlaces([]);setSearch('');setOpen(2);
  }
  function submit(event:React.FormEvent) {
    event.preventDefault();
    setOpen(null);
    if (watching) startWatch(); else ask();
  }

  const where=s.aoi ? <><span>{s.aoi.name||'Drawn area'}</span><small className={areaKm(s.aoi)>50.1?'bad':''}> · {number(areaKm(s.aoi))} km²</small></> : <em>Choose a place</em>;
  const when=watching
    ? (s.dateA&&s.watchFrom ? <><span>{day(s.watchFrom,!s.watchUntil)} <ArrowRight size={12}/> {s.watchUntil?day(s.watchUntil):'ongoing'}</span></> : <em>Choose the period</em>)
    : (s.dateA||s.dateB ? <>{day(s.dateA)||'?'} <ArrowRight size={12}/> {day(s.dateB)||'?'}</> : <em>Choose two dates</em>);
  const what=s.intent ? intents[s.intent].label : <em>{watching?'Choose a change':'Decided from your question'}</em>;

  return <aside className="panel composer" aria-label="Mission">
    <div className="task-switch" role="tablist">
      <button role="tab" aria-selected={!watching} disabled={locked} onClick={()=>set({task:'ask'})}>Ask once</button>
      <button role="tab" aria-selected={watching} disabled={locked} onClick={()=>set({task:'watch',watchFrom:s.watchFrom||(s.dateA?shift(s.dateA,1):'')})}><BellRing size={13}/> Keep watching</button>
    </div>
    <form onSubmit={submit}>
      <div className="scroll">
        <label className="question">
          <span className="label">{watching?'What should the satellite tell you about?':'Your question'}</span>
          <textarea ref={question} value={s.question} rows={2} maxLength={2000} disabled={locked}
            placeholder={watching?'Tell me if new water appears here.':'Did Jebel Ali flood after the April 2024 storm?'}
            onChange={e=>edit({question:e.target.value})}
            onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();e.currentTarget.form?.requestSubmit();}}}/>
        </label>
        {!watching&&<p className="hint">Name a place and two dates and the satellite works out the rest, or set them below.</p>}
        {watching&&<p className="hint">The satellite checks every overpass and only sends data down when the change is large enough.</p>}

        <details className="cases" open={casesOpen} onToggle={e=>setCasesOpen(e.currentTarget.open)}>
          <summary className="label">Try a real case</summary>
          <div className="case-grid">
          {watches.map(preset=><button type="button" className={'case watch'+(s.presetId===preset.id?' on':'')} key={preset.id} disabled={locked} onClick={()=>applyWatchPreset(preset)}>
            <BellRing size={15}/><span>{preset.title}<small>Standing watch · the satellite checks every overpass and only reports real change</small></span></button>)}
          {presets.map(preset=><button type="button" className={'case'+(s.presetId===preset.id?' on':'')} key={preset.id} disabled={locked} onClick={()=>applyPreset(preset)}>
            {preset.exact?<Cloud size={15}/>:<CloudOff size={15}/>}<span>{preset.title}<small>{intents[preset.intent].label} · {preset.name}</small></span></button>)}
          </div>
          {demos.length>0&&<details><summary>Offline samples (made-up imagery)</summary>
            {demos.map(demo=><button type="button" className={'case'+(s.demoId===demo.id?' on':'')} key={demo.id} disabled={locked} onClick={()=>applyPreset(demo)}>
              <span>{demo.title}<small>Works with no internet · not real data</small></span></button>)}
          </details>}
        </details>
        <span className="label own">{watching?'Set up the watch':'Or set it up yourself'}</span>
        <Step n={1} title="Where" value={where} open={open===1} done={Boolean(s.aoi)} onToggle={()=>toggle(1)}>
          <div className="search">
            <Search size={15}/><input aria-label="Search a place" value={search} placeholder="Search a city, district or landmark"
              onChange={e=>setSearch(e.target.value)} onKeyDown={e=>{if(e.key==='Enter') find(e);}}/>
            <button type="button" disabled={searching||search.trim().length<2} onClick={find}>{searching?'…':'Find'}</button>
          </div>
          {places.map(place=><button type="button" className="place" key={place.lat+','+place.lon} onClick={()=>choose(place)}>
            <MapPin size={14}/><span>{place.name}<small>{place.country}</small></span><small>5 km box</small></button>)}
          {searchNote&&<p className="hint">{searchNote}</p>}
          <div className="draw-row">
            <button type="button" aria-pressed={s.draw==='drop'} onClick={()=>set({draw:s.draw==='drop'?null:'drop'})}><MousePointerClick size={14}/> Click the globe</button>
            <button type="button" aria-pressed={s.draw==='box'} onClick={()=>set({draw:s.draw==='box'?null:'box'})}><Square size={14}/> Draw a box</button>
            <button type="button" aria-pressed={s.draw==='polygon'} onClick={()=>set({draw:s.draw==='polygon'?null:'polygon'})}><Pentagon size={14}/> Shape</button>
          </div>
          {s.aoi&&<button type="button" className="link" onClick={()=>{edit({aoi:null});s.reset();}}><X size={12}/> Clear the area</button>}
        </Step>

        <Step n={2} title="When" value={when} open={open===2} done={watching?Boolean(s.dateA&&s.watchFrom):Boolean(s.dateA&&s.dateB)} onToggle={()=>toggle(2)}>
          {watching ? <>
            <label className="date"><span className="label">Compare against (a clear day before)</span>
              <input type="date" value={s.dateA} max={today()} onChange={e=>edit({dateA:e.target.value})}/></label>
            {s.aoi&&s.dateA&&<PassStrip aoi={s.aoi} date={s.dateA} picked onPick={date=>edit({dateA:date,watchFrom:s.watchFrom>date?s.watchFrom:shift(date,1)})}/>}
            <div className="pair">
              <label className="date"><span className="label">Watch from</span><input type="date" value={s.watchFrom} max={today()} onChange={e=>edit({watchFrom:e.target.value})}/></label>
              <label className="date"><span className="label">Until (optional)</span><input type="date" value={s.watchUntil} onChange={e=>edit({watchUntil:e.target.value})}/></label>
            </div>
            <p className="hint">Past dates replay the real overpasses in a few seconds each. Leave "until" empty to keep watching new passes.</p>
            <label className="date"><span className="label">Alert me when the change is at least</span>
              <span className="unit"><input type="number" min={0.5} max={5000} step={0.5} value={s.floorHa} onChange={e=>set({floorHa:Math.max(0.5,+e.target.value||0.5)})}/> hectares</span></label>
          </> : <>
            {(['A','B'] as const).map(role=>{
              const value=role==='A'?s.dateA:s.dateB, exact=role==='A'?s.exactA:s.exactB;
              return <div className="when" key={role}>
                <label className="date"><span className="label">{role==='A'?'Before':'After'}</span>
                  <input type="date" value={value} max={today()} onChange={e=>edit(role==='A'?{dateA:e.target.value,exactA:false}:{dateB:e.target.value,exactB:false})}/></label>
                {s.aoi&&value ? <PassStrip aoi={s.aoi} date={value} picked={exact} from={role==='B'?s.dateA:undefined} until={role==='A'?s.dateB:undefined}
                  onPick={date=>edit(role==='A'?{dateA:date,exactA:true}:{dateB:date,exactB:true})}/> : null}
                {s.aoi&&value&&<p className="hint">{exact ? <><Check size={11}/> Using the overpass of {day(value)} exactly.</>
                  : 'Each dot is a real overpass. Pick one, or leave it and the satellite takes the clearest one nearby.'}</p>}
              </div>;
            })}
            {!s.aoi&&<p className="hint">Choose a place first to see which days the satellite actually passed over, and how cloudy each was.</p>}
            <p className="legend"><i className="clear"/> clear <i className="partial"/> some cloud <i className="cloudy"/> cloudy</p>
          </>}
        </Step>

        <Step n={3} title="Look for" value={what} open={open===3} done={Boolean(s.intent)} onToggle={()=>toggle(3)}>
          <div className="chips">
            {(Object.keys(intents) as Intent[]).filter(key=>key!=='visible_change').map(key=>
              <button type="button" key={key} aria-pressed={s.intent===key} title={intents[key].hint}
                onClick={()=>{edit({intent:s.intent===key&&!watching?'':key});}}>{intents[key].label}<small>{intents[key].hint}</small></button>)}
          </div>
        </Step>

      </div>

      <footer>
        {s.error&&<div className="alert" role="alert">{s.error}</div>}
        {gaps.length>0&&gaps.length<(watching?4:4)&&!s.error&&<p className="hint">Still needed: {gaps.join(', ')}.</p>}
        <div className="send">
          <select aria-label="Answer language" value={s.language} disabled={locked} onChange={e=>set({language:e.target.value as Language})}>
            <option value="en">EN</option><option value="ar">عربي</option><option value="hi">हिन्दी</option>
          </select>
          <button className="primary" disabled={locked||gaps.length>0}>
            {s.busy ? (active?stageNames[active.stage]:'Reading your question')+'…' : watching ? 'Start watching' : 'Ask the satellite'}
            {!s.busy&&<ArrowRight size={16}/>}
          </button>
        </div>
      </footer>
    </form>
  </aside>;
}
