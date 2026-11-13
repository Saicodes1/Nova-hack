import {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Cpu, Info, QrCode, Radio, Smartphone, Thermometer, X, Zap} from 'lucide-react';
import QRCode from 'qrcode';
import '@fontsource-variable/inter';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import Globe from './Globe';
import Composer from './Composer';
import Answer from './Answer';
import Mobile from './Mobile';
import {follow} from './actions';
import {useStation} from './state';
import {api, rates, type Demo, type Health, type Recent, type Telemetry, type Tracks, type WatchPreset} from './types';
import './style.css';

const states:Record<string,string> = {idle:'Standing by',uplink:'Receiving command',processing:'Analysing onboard',downlink:'Sending answer'};

function App() {
  const station=useStation();
  const [demos,setDemos]=useState<Demo[]>([]);
  const [presets,setPresets]=useState<Demo[]>([]);
  const [watches,setWatches]=useState<WatchPreset[]>([]);
  const [telemetry,setTelemetry]=useState<Telemetry|null>(null);
  const [online,setOnline]=useState<boolean|null>(null);
  const [health,setHealth]=useState<Health|null>(null);
  const [tracks,setTracks]=useState<{data:Tracks;time:number}|null>(null);
  const [sources,setSources]=useState(false);
  const [qr,setQr]=useState('');
  const [qrBig,setQrBig]=useState(false);
  const [room,setRoom]=useState<Recent[]>([]);
  const [audience,setAudience]=useState(true);
  const shown=useRef(new Set<string>());
  const first=useRef(true);
  const link=(health?.public_url||window.location.origin)+'/?ask';
  const local=/\/\/(localhost|127\.)/.test(link);

  useEffect(()=>{
    QRCode.toDataURL(link,{margin:1,width:640,color:{dark:'#05070c',light:'#ffffff'}}).then(setQr).catch(()=>{});
  },[link]);

  // Audience mode: when someone in the room asks from their phone and this
  // screen is free, show their question and its answer here.
  useEffect(()=>{
    const tick=()=>api<Recent[]>('/recent').then(rows=>{
      const phone=rows.filter(row=>row.source==='phone');
      setRoom(phone.slice(0,4));
      if (first.current) {first.current=false;phone.forEach(row=>shown.current.add(row.analysis_id));return;}
      const fresh=phone.find(row=>!shown.current.has(row.analysis_id));
      const now=useStation.getState();
      if (fresh && audience && !now.busy && !now.watch) {
        shown.current.add(fresh.analysis_id);
        now.set({question:fresh.question||'A question from the room',presetId:null,demoId:null,task:'ask',dateA:'',dateB:''});
        follow(fresh.analysis_id);
        setQrBig(false);
      }
    }).catch(()=>{});
    tick();
    const timer=window.setInterval(tick,2000);
    return ()=>clearInterval(timer);
  },[audience]);


  useEffect(()=>{
    const controller=new AbortController();
    api<{cases:Demo[];presets?:Demo[];watches?:WatchPreset[]}>('/demo-cases',undefined,controller.signal).then(data=>{
      setDemos(data.cases);setPresets(data.presets??[]);setWatches(data.watches??[]);
    }).catch(()=>{if(!controller.signal.aborted) station.set({error:'The ground station server is not answering. Start it, then reload.'});});
    api<Health>('/health',undefined,controller.signal).then(setHealth).catch(()=>{});
    let first=true;
    function poll(){
      api<Telemetry>('/telemetry',undefined,controller.signal).then(data=>{
        setTelemetry(data);setOnline(true);
        if (first) {first=false;if (data.link_mode in rates) station.set({mode:data.link_mode});}
      }).catch(()=>{if(!controller.signal.aborted) setOnline(false)});
    }
    poll();
    const interval=window.setInterval(poll,2000);
    return ()=>{controller.abort();clearInterval(interval);};
  },[]);

  useEffect(()=>{
    if (!station.tracks||tracks) return;
    const controller=new AbortController();
    api<Tracks>('/orbit/tracks',undefined,controller.signal).then(data=>setTracks({data,time:Date.now()})).catch(()=>{});
    return ()=>controller.abort();
  },[station.tracks]);

  const info=health?.node_info;
  const jetson=info?.machine==='aarch64';
  const rightOpen=Boolean(station.watch||station.result||station.busy);
  const sim=telemetry?.power_source==='simulated';
  const working=online&&telemetry&&telemetry.state!=='idle';
  return <div className={'app'+(rightOpen?' has-answer':'')}>
    <Globe trackData={tracks?.data??null} trackTime={tracks?.time??0} rightOpen={rightOpen}/>
    <div className="scrim" aria-hidden="true"/>
    <div className="frame" aria-hidden="true"/>
    <header className="hud">
      <div className="brand"><span className="mark"><i/></span><div><h1>ASK THE <b>SATELLITE</b></h1><p>EARTH OBSERVATION · SENTINEL-2</p></div></div>
      <div className="telemetry" aria-label="Satellite node status">
        <span className={'node '+(online?(working?'busy':'on'):'off')}><i/>
          <span><small>{online===false?'NODE':sim?'SIMULATED NODE':jetson?'JETSON ORIN NANO':telemetry?.mode==='remote'?'REMOTE NODE':'SIMULATED NODE'}</small>
            {online===null?'Connecting':online?states[telemetry?.state??'idle']:'Not reachable'}</span></span>
        {telemetry?.power_w!=null&&<span title={sim?'Simulated value, modelled on the Jetson measurements. Not a live reading.':'Power drawn by the satellite computer, measured live'}><Zap size={13}/><span><small>{sim?'POWER · SIMULATED':'POWER'}</small>{telemetry.power_w.toFixed(1)} W</span></span>}
        {telemetry?.temp_c!=null&&<span title={sim?'Simulated value. Not a live reading.':'Processor temperature, measured live'}><Thermometer size={13}/><span><small>{sim?'TEMP · SIMULATED':'TEMP'}</small>{telemetry.temp_c.toFixed(0)} °C</span></span>}
        {telemetry&&online&&<span title="Memory in use on the satellite computer"><Cpu size={13}/><span><small>MEMORY</small>{(telemetry.mem_used_mb/1024).toFixed(1)} GB</span></span>}
        <span title="Radio link used for the answer"><Radio size={13}/><span><small>LINK</small>{station.mode==='demo-fast'?'Instant':rates[station.mode].label+' '+rates[station.mode].detail}</span></span>
      </div>
    </header>
    <Composer demos={demos} presets={presets} watches={watches}/>
    <Answer health={health}/>
    {!rightOpen&&!station.aoi&&<div className="welcome"><h2>Ask what changed on Earth.</h2>
      <p>Pick a place and two dates. An AI satellite compares real Sentinel-2 images and sends back a short, checked answer.</p>
      {qr&&<button className="qr-card" onClick={()=>setQrBig(true)}><img src={qr} alt="QR code to ask from your phone"/>
        <span><b>Ask it from your phone</b><small>Scan, type a question, and watch the answer arrive here too.</small></span></button>}</div>}
    <button className="qr-button" onClick={()=>setQrBig(true)} title="Show the QR code for phones"><QrCode size={13}/> Phone QR</button>
    {room.length>0&&!rightOpen&&<div className="room"><span className="label"><Smartphone size={12}/> From the room</span>
      {room.map(row=><button key={row.analysis_id} onClick={()=>{station.set({question:row.question});follow(row.analysis_id);}}>{row.question||row.place||'Question'}</button>)}</div>}
    {qrBig&&<div className="qr-stage" role="dialog" aria-label="QR code for phones" onClick={()=>setQrBig(false)}>
      <div onClick={e=>e.stopPropagation()}>
        <button className="close" aria-label="Close" onClick={()=>setQrBig(false)}><X size={18}/></button>
        <span className="label">Talk to the satellite yourself</span>
        <h2>Scan and ask it anything.</h2>
        {qr&&<img src={qr} alt="QR code"/>}
        <code>{link}</code>
        {local&&<p className="hint bad">This address only works on this laptop. Set PUBLIC_URL to an address phones can reach, then restart the ground station.</p>}
        <label className="switch"><input type="checkbox" checked={audience} onChange={e=>setAudience(e.target.checked)}/> Show the room's questions on this screen</label>
      </div></div>}
    <button className="sources-button" onClick={()=>setSources(!sources)} aria-expanded={sources}><Info size={13}/> Data sources</button>
    {sources&&<div className="panel sources" role="dialog" aria-label="Data sources">
      <p>Globe imagery: Sentinel-2 cloudless 2020 by EOX IT Services GmbH (contains modified Copernicus Sentinel data 2020).</p>
      <p>Analysis imagery: Copernicus Sentinel-2 L2A via Element 84 Earth Search and Microsoft Planetary Computer.</p>
      <p>Land cover: © ESA WorldCover project 2021. Place names and borders: © OpenStreetMap contributors, OpenFreeMap.</p>
      <p>Weather and place search: Open-Meteo. Orbits: CelesTrak.</p>
      <p>This is an emulated satellite: {jetson?'a Jetson Orin Nano':'a computer'} replaying real, free Sentinel-2 data.</p>
    </div>}
  </div>;
}

const phone=new URLSearchParams(window.location.search).has('ask') || (window.innerWidth<700 && !new URLSearchParams(window.location.search).has('desk'));
createRoot(document.getElementById('root')!).render(phone?<Mobile/>:<App/>);
