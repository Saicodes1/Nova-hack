import {useEffect, useState} from 'react';
import {ArrowRight, MapPin, Satellite, Thermometer, Zap} from 'lucide-react';
import {Confidence, Hero, Progress, Timeline} from './Answer';
import {applyPreset, ask} from './actions';
import {useStation} from './state';
import {api, bytes, day, type Demo, type Health, type Telemetry} from './types';

const examples = [
  'Did Dubai Marina lose vegetation between March 2022 and March 2024?',
  'Did Jebel Ali flood after the April 2024 storm?',
  'How much did Dubai South build up from March 2019 to March 2025?',
];

/** The page behind the QR code: one question box, one answer, built for a phone. */
export default function Mobile() {
  const s=useStation();
  const [presets,setPresets]=useState<Demo[]>([]);
  const [telemetry,setTelemetry]=useState<Telemetry|null>(null);
  const [health,setHealth]=useState<Health|null>(null);
  const [swipe,setSwipe]=useState(50);
  useEffect(()=>{
    s.set({source:'phone',tracks:false});
    api<{presets?:Demo[]}>('/demo-cases').then(data=>setPresets(data.presets??[])).catch(()=>{});
    api<Health>('/health').then(setHealth).catch(()=>{});
    const poll=()=>api<Telemetry>('/telemetry').then(setTelemetry).catch(()=>setTelemetry(null));
    poll();
    const timer=window.setInterval(poll,2500);
    return ()=>clearInterval(timer);
  },[]);
  const packet=s.result?.packet;
  const jetson=health?.node_info?.machine==='aarch64';
  const writer=health?.node_info?.verdict_writer;
  const images=packet&&packet.image_bounds.length===4 ? '/api/analyses/'+packet.analysis_id+'/images/' : '';
  const [a,b]=['A','B'].map(role=>packet?.scenes.find(scene=>scene.role===role));
  function again() {s.reset();s.set({question:'',aoi:null,dateA:'',dateB:'',intent:'',demoId:null,presetId:null,exactA:false,exactB:false});window.scrollTo(0,0);}
  function submit(event:React.FormEvent) {
    event.preventDefault();
    // A typed question is planned from its own words, not from a previous area.
    s.set({aoi:null,dateA:'',dateB:'',intent:'',demoId:null,presetId:null,exactA:false,exactB:false});
    ask();
  }

  return <div className="mobile">
    <header>
      <div className="brand"><span className="mark"><i/></span><div><h1>ASK THE <b>SATELLITE</b></h1></div></div>
      <div className={'m-node '+(telemetry?(telemetry.state==='idle'?'on':'busy'):'off')}><i/>
        <span>{telemetry ? (jetson&&telemetry.power_source!=='simulated'?'Jetson Orin Nano':'Simulated satellite node')+' · '+(telemetry.state==='idle'?'ready':'working') : 'Satellite not reachable'}</span>
        {telemetry?.power_w!=null&&<span><Zap size={12}/>{telemetry.power_w.toFixed(1)} W{telemetry.power_source==='simulated'?' (simulated)':''}</span>}
        {telemetry?.temp_c!=null&&<span><Thermometer size={12}/>{telemetry.temp_c.toFixed(0)} °C</span>}
      </div>
    </header>

    {!packet&&!s.busy&&<main>
      <h2>Ask what changed on Earth.</h2>
      <p className="body">You are talking to the small computer on our table. It compares two real satellite images and sends back a short, checked answer.</p>
      <form onSubmit={submit}>
        <textarea value={s.question} rows={3} maxLength={2000} placeholder="Name a place, two dates and what you want to know."
          onChange={e=>s.set({question:e.target.value})}/>
        {s.error&&<div className="alert" role="alert">{s.error}</div>}
        <button className="primary" disabled={!s.question.trim()}>{s.error?'Ask again':'Ask the satellite'} <ArrowRight size={16}/></button>
      </form>
      <span className="label">Or tap one</span>
      {presets.map(preset=><button key={preset.id} className="case" onClick={()=>applyPreset(preset)}>
        <span>{preset.title}<small>{preset.question}</small></span></button>)}
      {examples.map(text=><button key={text} className="case" onClick={()=>{s.set({question:text,aoi:null,dateA:'',dateB:'',intent:'',presetId:null});ask();}}>
        <span>{text}</span></button>)}
    </main>}

    {!packet&&s.busy&&<main>
      <p className="asked">{s.question}</p>
      <Progress/>
      <Timeline/>
    </main>}

    {packet&&s.result&&<main dir={s.result.plan.language==='ar'?'rtl':'ltr'}>
      <p className="asked">{s.question}</p>
      <p className="m-area"><MapPin size={13}/> {packet.aoi.name||'Selected area'}</p>
      {packet.aoi.name?.startsWith('Centre of')&&<p className="hint">That region is far larger than one analysis, so this covers a 5 km square at its centre. Name a town or district for a specific spot.</p>}
      <Hero packet={packet}/>
      <Confidence packet={packet}/>
      <p className="body">{s.result.explanation.body}</p>
      {images&&<div className="m-compare">
        <img src={images+'a'} alt={'Before, '+day(a?.datetime)}/>
        <div style={{clipPath:'inset(0 0 0 '+swipe+'%)'}}><img src={images+'b'} alt={'After, '+day(b?.datetime)}/><img src={images+'change'} alt=""/></div>
        <i style={{left:swipe+'%'}}/>
        <span className="tag-a">{day(a?.datetime)}</span><span className="tag-b">{day(b?.datetime)}</span>
        <input type="range" min={0} max={100} value={swipe} onChange={e=>setSwipe(+e.target.value)} aria-label="Drag to compare before and after"/>
      </div>}
      {images&&<p className="hint">Drag across the image. Left is before, right is after, with the change highlighted.</p>}
      {packet.onboard_verdict&&<blockquote><span className="label"><Satellite size={12}/> Said onboard{writer?.startsWith('ollama')?' · '+writer.replace('ollama:','')+' running on the satellite':''}</span>{packet.onboard_verdict}</blockquote>}
      {packet.link&&<p className="m-link"><b>{bytes(packet.link.packet_bytes)}</b> came down the radio link
        {packet.link.raw_equivalent_bytes?<> instead of <b>{bytes(packet.link.raw_equivalent_bytes)}</b> of raw images</>:null}.</p>}
      <section className="caveats"><span className="label">What could make this wrong</span>
        <ul>{s.result.explanation.caveats.map(c=><li key={c}>{c}</li>)}</ul></section>
      <p className="credit">{packet.attribution.join(' · ')}</p>
      <button className="primary" onClick={again}>Ask something else <ArrowRight size={16}/></button>
    </main>}
  </div>;
}
