import {useEffect, useRef, useState} from 'react';
import maplibregl, {LngLatBounds, Map as MapLibre, NavigationControl, Popup} from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import {Crosshair, Layers2, MapPin, Pentagon, Search, Square, X} from 'lucide-react';
import type {Feature, FeatureCollection, Geometry} from 'geojson';
import {api, areaKm, intentColor, intents, number, type AOI, type Place, type Point, type Tracks} from './types';
import {useStation} from './state';

const empty:FeatureCollection = {type:'FeatureCollection',features:[]};
const feature = (geometry:Geometry):Feature => ({type:'Feature',geometry,properties:{}});
const formatDate = (value?:string) => value ? new Date(value.slice(0,10)+'T12:00:00').toLocaleDateString(undefined,
  {month:'short',day:'numeric',year:'numeric'}) : '';
const styleUrl = 'https://tiles.openfreemap.org/styles/dark';
const fallbackColor='#22d3ee';
const artefactColor='#9aa4b2';

function patchLayers(map:MapLibre, after:boolean) {
  if (map.getSource('detected')) return;
  map.addSource('detected',{type:'geojson',data:empty});
  map.addSource('selected',{type:'geojson',data:empty});
  const color:maplibregl.ExpressionSpecification=['case',['==',['get','artefact'],true],artefactColor,fallbackColor];
  map.addLayer({id:'detected-hit',source:'detected',type:'fill',
    paint:{'fill-color':color,'fill-opacity':after ? .4 : 0}});
  map.addLayer({id:'detected-casing',source:'detected',type:'line',
    paint:{'line-color':'#05090f','line-width':after ? 3.5 : 3,'line-opacity':.85}});
  map.addLayer({id:'detected-outline',source:'detected',type:'line',
    paint:{'line-color':color,'line-width':after ? 1.8 : 1.5,
      ...(after ? {} : {'line-dasharray':[2,1.5]})}});
  map.addLayer({id:'selected-line',source:'selected',type:'line',
    paint:{'line-color':'#ffffff','line-width':3}});
}

function stylePatches(map:MapLibre, after:boolean, color:string, opacity:number) {
  if (!map.getLayer('detected-hit')) return;
  const paint:maplibregl.ExpressionSpecification=['case',['==',['get','artefact'],true],artefactColor,color];
  map.setPaintProperty('detected-hit','fill-color',paint);
  map.setPaintProperty('detected-hit','fill-opacity',after ? opacity : 0);
  map.setPaintProperty('detected-outline','line-color',paint);
}

type Draw = 'box'|'polygon'|null;

export default function MapPanel({trackData}:{trackData:Tracks|null}) {
  const station = useStation();
  const baseEl = useRef<HTMLDivElement>(null);
  const afterEl = useRef<HTMLDivElement>(null);
  const maps = useRef<{base:MapLibre;after:MapLibre}|null>(null);
  const [ready, setReady] = useState(false);
  const [basemap, setBasemap] = useState(true);
  const [canvasError,setCanvasError] = useState(false);
  const [draw,setDraw] = useState<Draw>(null);
  const [points,setPoints] = useState<Point[]>([]);
  const [search,setSearch] = useState('');
  const [places,setPlaces] = useState<Place[]>([]);
  const [searchNote,setSearchNote] = useState('');
  const [searching,setSearching] = useState(false);
  const latest = useRef({draw,points,station});
  latest.current={draw,points,station};
  const packet=station.result?.packet;
  const rendered=station.stages.find(row=>row.stage==='render'&&row.status==='done')?.detail;
  const bounds=packet?.image_bounds ?? rendered?.image_bounds;
  const polygonData=packet?.change_geojson ?? rendered?.change_geojson;
  const patchCount=polygonData?.features.length ?? 0;
  const hasArtefacts=polygonData?.features.some(feature=>feature.properties?.artefact);
  const comparison=Boolean(bounds?.length && station.currentId);
  const previewId=packet ? station.currentId : rendered?.artifact_id ?? station.currentId;
  const preview = previewId ? '/api/analyses/'+previewId+'/images/' : '';

  function select(aoi:AOI) {
    station.set({aoi,demoId:null,error:'',result:null,stages:[],currentId:null});
    setPlaces([]);
    setSearchNote('');
  }

  useEffect(()=>{
    if (!baseEl.current || !afterEl.current) return;
    let base:MapLibre|undefined, after:MapLibre|undefined;
    try {
      base = new MapLibre({container:baseEl.current,center:[55.22,24.93],zoom:10,attributionControl:false,
        style:{version:8,sources:{},layers:[{id:'background',type:'background',paint:{'background-color':'#161b22'}}]}});
      after = new MapLibre({container:afterEl.current,center:[55.22,24.93],zoom:10,interactive:false,attributionControl:false,
        style:{version:8,sources:{},layers:[]}});
    } catch {
      base?.remove(); after?.remove(); setCanvasError(true); return;
    }
    const a=base,b=after;
    let alive=true;
    let baseStyleReady=false, afterStyleReady=false;
    const controller=new AbortController();
    a.addControl(new NavigationControl({showCompass:false}),'bottom-right');
    function sync() {
      b.jumpTo({center:a.getCenter(),zoom:a.getZoom(),bearing:a.getBearing(),pitch:a.getPitch()});
    }
    function layers() {
      if (!baseStyleReady || !afterStyleReady) return;
      if (!a.getSource('area')) {
        for (const key of ['area','draft','tracks']) a.addSource(key,{type:'geojson',data:empty});
        a.addLayer({id:'area-fill',source:'area',type:'fill',paint:{'fill-color':'#58a6ff','fill-opacity':.10}});
        a.addLayer({id:'area-line',source:'area',type:'line',paint:{'line-color':'#79c0ff','line-width':2}});
        a.addLayer({id:'track-line',source:'tracks',type:'line',paint:{'line-color':'#c2a1ff','line-width':2,'line-dasharray':[3,2]}});
        patchLayers(a,false);
        a.addLayer({id:'draft-line',source:'draft',type:'line',paint:{'line-color':'#79c0ff','line-width':3,'line-dasharray':[2,2]}});
        a.addLayer({id:'draft-point',source:'draft',type:'circle',filter:['==',['geometry-type'],'Point'],
          paint:{'circle-radius':4,'circle-color':'#79c0ff'}});
      }
      patchLayers(b,true);
      setReady(true);
    }
    a.on('style.load',()=>{baseStyleReady=true;layers();});
    b.on('style.load',()=>{afterStyleReady=true;layers();});
    a.on('move',sync); a.on('resize',sync);
    a.on('click',(event)=>{
      const {draw,points,station}=latest.current;
      if (draw && !station.busy) {
        const point=event.lngLat.wrap().toArray() as Point;
        if (draw==='box' && points.length===1) {
          const [x,y]=points[0], [u,v]=point;
          if (x===u || y===v) return;
          select({geometry:{type:'Polygon',coordinates:[[[x,y],[u,y],[u,v],[x,v],[x,y]]]}});
          setDraw(null); setPoints([]);
        } else {
          setPoints([...points,point]);
        }
        return;
      }
      if (!station.changes || !a.getLayer('detected-hit')) return;
      const item=a.queryRenderedFeatures(event.point,{layers:['detected-hit']})[0];
      if (item) {
        const info=item.properties;
        const side=event.point.x / a.getContainer().clientWidth * 100 < station.swipe ? 'Before' : 'After';
        new Popup().setLngLat(event.lngLat).setText(
          side+' view · '+number(info.ha,2)+' ha patch'+(info.artefact?' · possible cloud-edge artefact':'')).addTo(a);
        const features=latest.current.station.result?.packet.change_geojson?.features ?? [];
        const index=features.findIndex(f=>JSON.stringify(f.geometry)===JSON.stringify(item.geometry));
        if (index>=0) latest.current.station.set({selectedPatch:index});
      }
    });
    maps.current={base:a,after:b};
    fetch(styleUrl,{signal:controller.signal}).then(r=>{if(!r.ok) throw Error();return r.json()}).then(style=>{
      if(alive) {
        baseStyleReady=false;
        setReady(false);
        a.setStyle(style,{diff:false});
      }
    }).catch(()=>{if(alive) setBasemap(false)});
    return ()=>{alive=false;controller.abort();a.remove();b.remove();maps.current=null;};
  },[]);

  useEffect(()=>{
    const base=maps.current?.base;
    if (!ready || !base) return;
    base.getSource<maplibregl.GeoJSONSource>('area')?.setData(station.aoi?.geometry ?? empty);
    if (station.aoi && !draw) {
      const bounds=new LngLatBounds();
      station.aoi.geometry.coordinates[0].forEach(point=>bounds.extend(point as Point));
      base.fitBounds(bounds,{padding:65,maxZoom:15,duration:0});
    }
  },[ready,station.aoi]);

  useEffect(()=>{
    const base=maps.current?.base;
    if (!ready || !base) return;
    base.getCanvas().style.cursor=draw?'crosshair':'';
    const features=points.map(point=>feature({type:'Point',coordinates:point}));
    if (points.length>1) features.push(feature({type:'LineString',coordinates:points}));
    base.getSource<maplibregl.GeoJSONSource>('draft')?.setData({type:'FeatureCollection',features});
  },[ready,draw,points]);

  useEffect(()=>{
    if (!ready || !maps.current) return;
    const {base,after}=maps.current;
    function image(map:MapLibre,key:string,name:string,show:boolean) {
      if (map.getLayer(key)) map.removeLayer(key);
      if (map.getSource(key)) map.removeSource(key);
      if (!show || bounds?.length!==4 || !preview) return;
      map.addSource(key,{type:'image',url:preview+name,coordinates:[bounds[0],bounds[1],bounds[2],bounds[3]]});
      map.addLayer({id:key,type:'raster',source:key,paint:{'raster-fade-duration':0}},
        map.getLayer('area-line')?'area-line':map.getLayer('detected-hit')?'detected-hit':undefined);
    }
    image(base,'before','a',comparison);
    image(after,'after','b',comparison);
    image(after,'change-preview','change',comparison&&station.changes&&!polygonData&&!packet);
    for (const map of [base,after]) {
      map.getSource<maplibregl.GeoJSONSource>('detected')?.setData(comparison && station.changes && polygonData ? polygonData : empty);
    }
  },[ready,bounds,comparison,preview,station.changes,polygonData,packet]);

  const color=packet ? intentColor[packet.intent] : fallbackColor;
  useEffect(()=>{
    if (!ready || !maps.current) return;
    stylePatches(maps.current.base,false,color,station.patchOpacity);
    stylePatches(maps.current.after,true,color,station.patchOpacity);
  },[ready,color,station.patchOpacity,polygonData]);

  useEffect(()=>{
    if (!ready || !maps.current) return;
    const chosen=station.selectedPatch!==null ? polygonData?.features[station.selectedPatch] : undefined;
    for (const map of [maps.current.base,maps.current.after])
      map.getSource<maplibregl.GeoJSONSource>('selected')?.setData(chosen ? {type:'FeatureCollection',features:[chosen]} : empty);
  },[ready,station.selectedPatch,polygonData]);

  useEffect(()=>{
    const base=maps.current?.base;
    if (!ready || !base || !station.focus) return;
    const [w,s,e,n]=station.focus;
    base.fitBounds([[w,s],[e,n]],{padding:120,maxZoom:15,duration:600});
  },[ready,station.focus]);

  useEffect(()=>{
    const base=maps.current?.base;
    if (!ready || !base || bounds?.length!==4 || !packet) return;
    const box=new LngLatBounds();
    bounds.forEach(point=>box.extend(point as Point));
    base.fitBounds(box,{padding:40,maxZoom:16,duration:0});
  },[ready,packet?.analysis_id]);

  useEffect(()=>{
    if (!ready) return;
    maps.current?.base.getSource<maplibregl.GeoJSONSource>('tracks')?.setData(
      station.tracks && trackData ? {type:'FeatureCollection',features:Object.values(trackData).map(
        coordinates=>feature({type:'MultiLineString',coordinates}))} : empty);
  },[ready,station.tracks,trackData]);

  async function find(event:React.FormEvent) {
    event.preventDefault();
    if (search.trim().length<2) return;
    setSearching(true); setPlaces([]); setSearchNote('');
    try {
      const result=await api<Place[]>('/geocode?q='+encodeURIComponent(search.trim()));
      setPlaces(result);
      if (!result.length) setSearchNote('No matching places.');
    } catch(error) {setSearchNote((error as Error).message);}
    finally {setSearching(false);}
  }

  function toggle(mode:Draw) {setPoints([]);setDraw(draw===mode?null:mode);}
  function finish() {
    if (points.length<3) return;
    select({geometry:{type:'Polygon',coordinates:[[...points,points[0]]]}});
    setDraw(null);setPoints([]);
  }
  const aoi=station.aoi;
  const sceneA=packet?.scenes.find(s=>s.role==='A');
  const sceneB=packet?.scenes.find(s=>s.role==='B');

  return <section className="map-panel" aria-label="Area selection and satellite comparison">
    <div className="map-view">
      <div ref={baseEl} className="map-layer"/>
      <div ref={afterEl} className="map-layer after-layer" style={{clipPath:comparison?'inset(0 0 0 '+station.swipe+'%)':'inset(0 0 0 100%)'}}/>
      {canvasError&&comparison&&<div className="image-fallback">
        <img src={preview+'a'} alt="Before scene"/>
        <div className="image-fallback" style={{clipPath:'inset(0 0 0 '+station.swipe+'%)'}}>
          <img src={preview+'b'} alt="After scene"/>
          {station.changes&&<img src={preview+'change'} alt="Detected change overlay"/>}
        </div>
      </div>}
      {canvasError&&!comparison&&<div className="canvas-error">WebGL is unavailable. Try a demo to compare the images.</div>}
      <div className="map-actions">
        <form className="map-search" onSubmit={find}>
          <Search size={17}/><input aria-label="Search a place" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search for a place"/>
          <button type="submit" disabled={searching||search.trim().length<2}>{searching?'Searching':'Search'}</button>
        </form>
        <div className="draw-tools">
          <button title="Draw rectangle" aria-label="Draw rectangle" aria-pressed={draw==='box'} onClick={()=>toggle('box')} disabled={station.busy||canvasError}><Square size={17}/></button>
          <button title="Draw polygon" aria-label="Draw polygon" aria-pressed={draw==='polygon'} onClick={()=>toggle('polygon')} disabled={station.busy||canvasError}><Pentagon size={17}/></button>
          <button title="Clear selected area" aria-label="Clear selected area" disabled={!aoi||station.busy} onClick={()=>{setDraw(null);setPoints([]);station.set({aoi:null,demoId:null,result:null,stages:[],currentId:null});}}><X size={17}/></button>
        </div>
      </div>
      {(places.length>0||searchNote)&&<div className="search-results">
        {places.map(place=><button disabled={station.busy} key={place.lat+','+place.lon} onClick={()=>select(place.aoi)}>
          <MapPin size={16}/><span>{place.name}<small>{place.country}</small></span><small>Use 5 km box</small>
        </button>)}
        {searchNote&&<div className="notice">{searchNote}</div>}
      </div>}
      {draw&&<div className="draw-message">
        {draw==='box'?'Click two opposite corners.':'Click the polygon corners.'}
        {draw==='polygon'&&<button disabled={points.length<3} onClick={finish}>Finish area</button>}
        <button onClick={()=>{setDraw(null);setPoints([]);}}>Cancel</button>
      </div>}
      {comparison&&<div className="compare-ui">
        <span className="date-tag before-date"><b>Before</b> {formatDate(sceneA?.datetime || station.dateA)}</span>
        <span className="date-tag after-date"><b>After</b> {formatDate(sceneB?.datetime || station.dateB)}</span>
        <div className="swipe-line" style={{left:station.swipe+'%'}}><span>‹ ›</span></div>
        <input type="range" className="swipe-input" min="0" max="100" value={station.swipe} onChange={e=>station.set({swipe:+e.target.value})} aria-label="Before and after swipe"/>
      </div>}
      <div className="map-bottom">
        {comparison&&<button aria-pressed={station.changes} onClick={()=>station.set({changes:!station.changes})}><Layers2 size={16}/> Change overlay</button>}
        {!basemap&&<span className="map-warning">Basemap unavailable</span>}
      </div>
    </div>
    {comparison&&station.changes&&packet&&<div className="patch-legend" aria-label="Change overlay legend">
      {patchCount ? <>
        <strong>{number(patchCount,0)} {patchCount===1?'patch':'patches'} · {intents[packet.intent]}</strong>
        <span><i className="patch-key filled" style={{borderColor:color,background:color+'66'}}/> After · detected area</span>
        {!canvasError&&<span><i className="patch-key dashed" style={{borderColor:color}}/> Before · same outline</span>}
        {hasArtefacts&&<span><i className="patch-key uncertain"/> Possible cloud-edge artefact</span>}
        <label className="opacity-control">Fill
          <input type="range" min="0" max="0.8" step="0.05" value={station.patchOpacity}
            onChange={e=>station.set({patchOpacity:+e.target.value})} aria-label="Patch fill opacity"/></label>
      </> : <span>{packet.status==='no_change'?'No patches above the detection threshold.':'No change overlay available.'}</span>}
    </div>}
    <div className="map-status">
      <span><Crosshair size={16}/>{aoi ? <>{aoi.name||'Drawn area'} <span className={areaKm(aoi)>50.1?'warning':''}>· {number(areaKm(aoi))} km²</span></> : 'No area selected'}</span>
      <span>{packet?.scenes.some(scene=>scene.source==='synthetic') || station.demoId ? 'Synthetic demo' : 'Sentinel-2'}</span>
    </div>
  </section>;
}
