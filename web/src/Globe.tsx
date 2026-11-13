import {useEffect, useRef, useState} from 'react';
import maplibregl, {LngLatBounds, Map as MapLibre, Marker, Popup} from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import {Globe2, Layers2, Minus, Orbit, Plus} from 'lucide-react';
import type {Feature, FeatureCollection, Geometry} from 'geojson';
import {day, number, type AOI, type Point, type Tracks} from './types';
import {useStation} from './state';

const empty:FeatureCollection = {type:'FeatureCollection',features:[]};
const feature = (geometry:Geometry):Feature => ({type:'Feature',geometry,properties:{}});
const patchColor:maplibregl.ExpressionSpecification = ['case',['==',['get','artefact'],true],'#ffb454','#3ee0ff'];
const HOME = {center:[38,22] as Point, zoom:2.05};
const sidePad = () => window.innerWidth>1100 ? {left:400,right:0,top:40,bottom:0} : {left:0,right:0,top:0,bottom:0};
const LABEL_STYLE = 'https://tiles.openfreemap.org/styles/dark';

/** Satellite-imagery globe. The imagery itself is a cloud-free Sentinel-2 mosaic. */
function baseStyle():maplibregl.StyleSpecification {
  return {
    version:8,
    projection:{type:'globe'},
    sources:{imagery:{type:'raster',tileSize:256,maxzoom:14,
      tiles:['https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg']}},
    layers:[
      {id:'space',type:'background',paint:{'background-color':'#05070c'}},
      {id:'imagery',type:'raster',source:'imagery',paint:{'raster-fade-duration':200,'raster-saturation':['interpolate',['linear'],['zoom'],4,-0.1,9,-0.45],
        'raster-brightness-max':['interpolate',['linear'],['zoom'],4,0.95,9,0.42]}},
    ],
    sky:{'atmosphere-blend':['interpolate',['linear'],['zoom'],0,1,4,0.8,7,0]},
  } as maplibregl.StyleSpecification;
}

function patchLayers(map:MapLibre, filled:boolean) {
  if (map.getSource('detected')) return;
  map.addSource('detected',{type:'geojson',data:empty});
  map.addLayer({id:'detected-hit',source:'detected',type:'fill',paint:{'fill-color':patchColor,'fill-opacity':filled ? .28 : 0}});
  map.addLayer({id:'detected-casing',source:'detected',type:'line',paint:{'line-color':'#04080d','line-width':4}});
  map.addLayer({id:'detected-outline',source:'detected',type:'line',paint:{'line-color':patchColor,'line-width':1.8}});
}

/** A 5 km square around a clicked point. */
export function dropBox(lon:number, lat:number):AOI {
  const dy = 2.5/110.57, dx = 2.5/(111.32*Math.cos(lat*Math.PI/180));
  return {geometry:{type:'Polygon',coordinates:[[[lon-dx,lat-dy],[lon+dx,lat-dy],[lon+dx,lat+dy],[lon-dx,lat+dy],[lon-dx,lat-dy]]]}};
}

export default function Globe({trackData, trackTime, rightOpen}:{trackData:Tracks|null; trackTime:number; rightOpen:boolean}) {
  const station = useStation();
  const baseEl = useRef<HTMLDivElement>(null);
  const afterEl = useRef<HTMLDivElement>(null);
  const viewEl = useRef<HTMLDivElement>(null);
  const coordEl = useRef<HTMLSpanElement>(null);
  const maps = useRef<{base:MapLibre;after:MapLibre}|null>(null);
  const markers = useRef<Record<string,Marker>>({});
  const idle = useRef(true);
  const [ready, setReady] = useState(false);
  const [canvasError,setCanvasError] = useState(false);
  const [points,setPoints] = useState<Point[]>([]);
  const latest = useRef({points,station,rightOpen});
  latest.current={points,station,rightOpen};
  const packet=station.result?.packet;
  const rendered=station.stages.find(row=>row.stage==='render'&&row.status==='done')?.detail;
  const bounds=packet ? packet.image_bounds : rendered?.image_bounds;
  const polygonData=packet ? packet.change_geojson : rendered?.change_geojson;
  const comparison=Boolean(bounds?.length===4 && station.currentId);
  const previewId=packet ? station.currentId : rendered?.artifact_id ?? station.currentId;
  const preview = previewId ? '/api/analyses/'+previewId+'/images/' : '';
  const draw=station.draw;

  function select(aoi:AOI) {
    station.set({aoi,demoId:null,presetId:null,draw:null,exactA:false,exactB:false});
    station.reset();
    setPoints([]);
  }

  useEffect(()=>{
    if (!baseEl.current || !afterEl.current) return;
    let base:MapLibre|undefined, after:MapLibre|undefined;
    try {
      base = new MapLibre({container:baseEl.current,...HOME,attributionControl:false,style:baseStyle(),maxPitch:60,minZoom:1});
      after = new MapLibre({container:afterEl.current,...HOME,interactive:false,attributionControl:false,
        style:{version:8,projection:{type:'globe'},sources:{},layers:[]} as maplibregl.StyleSpecification});
    } catch {
      base?.remove(); after?.remove(); setCanvasError(true); return;
    }
    const a=base,b=after;
    let alive=true, frame=0, loaded=0;
    const controller=new AbortController();
    const sync=()=>b.jumpTo({center:a.getCenter(),zoom:a.getZoom(),bearing:a.getBearing(),pitch:a.getPitch(),padding:a.getPadding()});
    function layers() {
      if (++loaded<2) return;
      for (const key of ['area','draft','tracks']) a.addSource(key,{type:'geojson',data:empty});
      a.addLayer({id:'track-line',source:'tracks',type:'line',paint:{'line-color':'#8fb4ff','line-width':1.2,'line-opacity':.55,'line-dasharray':[2,3]}});
      a.addLayer({id:'area-fill',source:'area',type:'fill',paint:{'fill-color':'#3ee0ff','fill-opacity':.06}});
      a.addLayer({id:'area-glow',source:'area',type:'line',paint:{'line-color':'#3ee0ff','line-width':7,'line-opacity':.18,'line-blur':4}});
      a.addLayer({id:'area-line',source:'area',type:'line',paint:{'line-color':'#3ee0ff','line-width':1.6}});
      patchLayers(a,false);
      a.addLayer({id:'draft-line',source:'draft',type:'line',paint:{'line-color':'#3ee0ff','line-width':2,'line-dasharray':[2,2]}});
      a.addLayer({id:'draft-point',source:'draft',type:'circle',filter:['==',['geometry-type'],'Point'],
        paint:{'circle-radius':4,'circle-color':'#3ee0ff','circle-stroke-color':'#04080d','circle-stroke-width':2}});
      patchLayers(b,true);
      setReady(true);
      // Place names and borders on top of the imagery, if the label service answers.
      fetch(LABEL_STYLE,{signal:controller.signal}).then(r=>r.ok?r.json():Promise.reject()).then(style=>{
        if (!alive) return;
        const [name,source]=Object.entries(style.sources).find(([,s])=>(s as {type:string}).type==='vector') ?? [];
        if (!name) return;
        a.setGlyphs(style.glyphs);
        a.addSource(name,source as maplibregl.SourceSpecification);
        for (const layer of style.layers) {
          if (layer.source!==name) continue;
          if (layer.type==='line' && layer['source-layer']==='boundary') {
            a.addLayer({...layer,paint:{'line-color':'rgba(255,255,255,0.28)','line-width':0.7}},'track-line');
          } else if (layer.type==='symbol' && layer['source-layer']==='place' && !/state|other|village|suburb|continent/.test(layer.id)) {
            a.addLayer({...layer,minzoom:Math.max(layer.minzoom??0,2.8),maxzoom:11,
              layout:{...layer.layout,'text-field':['coalesce',['get','name_en'],['get','name:latin'],['get','name']],'text-transform':'none'},
              paint:{'text-color':'rgba(240,246,252,0.86)','text-halo-color':'rgba(4,8,13,0.85)','text-halo-width':1.3}},'track-line');
          }
        }
      }).catch(()=>{});
    }
    a.setPadding(sidePad()); b.setPadding(sidePad());
    a.on('load',layers); b.on('load',layers);
    a.on('move',sync); a.on('resize',sync);
    const wake=()=>{idle.current=false;};
    for (const name of ['mousedown','touchstart','wheel'] as const) a.getCanvas().addEventListener(name,wake,{passive:true});
    function spin() {
      if (idle.current && !a.isMoving()) {
        const c=a.getCenter();
        a.jumpTo({center:[c.lng+0.035,c.lat]});
      }
      frame=requestAnimationFrame(spin);
    }
    frame=requestAnimationFrame(spin);
    a.on('mousemove',event=>{
      const {lng,lat}=event.lngLat.wrap();
      if (coordEl.current) coordEl.current.textContent=
        Math.abs(lat).toFixed(4)+'° '+(lat>=0?'N':'S')+'   '+Math.abs(lng).toFixed(4)+'° '+(lng>=0?'E':'W');
    });
    a.on('click',(event)=>{
      const {points,station}=latest.current;
      const mode=station.draw;
      const point=event.lngLat.wrap().toArray() as Point;
      if (mode && !station.busy) {
        if (mode==='drop') select(dropBox(point[0],point[1]));
        else if (mode==='box' && points.length===1) {
          const [x,y]=points[0], [u,v]=point;
          if (x===u || y===v) return;
          select({geometry:{type:'Polygon',coordinates:[[[x,y],[u,y],[u,v],[x,v],[x,y]]]}});
        } else setPoints([...points,point]);
        return;
      }
      if (!station.changes || !a.getLayer('detected-hit')) return;
      const item=a.queryRenderedFeatures(event.point,{layers:['detected-hit']})[0];
      if (item) {
        const info=item.properties;
        new Popup({closeButton:false,className:'patch-popup'}).setLngLat(event.lngLat).setHTML(
          '<b>'+number(info.ha,2)+' ha</b> changed here'+(info.artefact?'<br><em>At a cloud edge. May be an artefact.</em>':'')).addTo(a);
      }
    });
    maps.current={base:a,after:b};
    return ()=>{alive=false;cancelAnimationFrame(frame);controller.abort();
      Object.values(markers.current).forEach(m=>m.remove());markers.current={};a.remove();b.remove();maps.current=null;};
  },[]);

  // Fly to the selected area, keeping it clear of the side panels.
  useEffect(()=>{
    const base=maps.current?.base;
    if (!ready || !base) return;
    base.getSource<maplibregl.GeoJSONSource>('area')?.setData(station.aoi?.geometry ?? empty);
    if (!station.aoi) return;
    idle.current=false;
    const box=new LngLatBounds();
    station.aoi.geometry.coordinates[0].forEach(point=>box.extend(point as Point));
    // Keep the area in the free space between the two side panels. The camera
    // is worked out for the final padding, then flown to in one smooth move.
    const wide=base.getContainer().clientWidth>1100;
    const none={top:0,bottom:0,left:0,right:0};
    const before=base.getPadding();
    base.setPadding(none);
    const camera=base.cameraForBounds(box,{maxZoom:14.5,
      padding:{top:120,bottom:100,left:(wide?400:0)+50,right:(wide&&rightOpen?440:0)+50}});
    base.setPadding(before);
    if (camera) base.flyTo({...camera,padding:none,duration:base.getZoom()<5?3200:1400,essential:true});
  },[ready,station.aoi,rightOpen]);

  useEffect(()=>{
    const base=maps.current?.base;
    if (!ready || !base) return;
    if (draw) idle.current=false;
    base.getCanvas().style.cursor=draw?'crosshair':'';
    const features=points.map(point=>feature({type:'Point',coordinates:point}));
    if (points.length>1) features.push(feature({type:'LineString',coordinates:points}));
    base.getSource<maplibregl.GeoJSONSource>('draft')?.setData({type:'FeatureCollection',features});
  },[ready,draw,points]);
  useEffect(()=>{if(!draw) setPoints([]);},[draw]);

  // Before image on the base map, after image on the clipped map above it.
  useEffect(()=>{
    if (!ready || !maps.current) return;
    const {base,after}=maps.current;
    function image(map:MapLibre,key:string,name:string,show:boolean) {
      if (map.getLayer(key)) map.removeLayer(key);
      if (map.getSource(key)) map.removeSource(key);
      if (!show || bounds?.length!==4 || !preview) return;
      map.addSource(key,{type:'image',url:preview+name,coordinates:[bounds[0],bounds[1],bounds[2],bounds[3]]});
      map.addLayer({id:key,type:'raster',source:key,paint:{'raster-fade-duration':0}},
        map.getLayer('area-fill')?'area-fill':map.getLayer('detected-hit')?'detected-hit':undefined);
    }
    image(base,'before','a',comparison);
    image(after,'after','b',comparison);
    image(after,'change-preview','change',comparison&&station.changes&&!polygonData&&!packet);
    for (const map of [base,after]) {
      map.getSource<maplibregl.GeoJSONSource>('detected')?.setData(comparison && station.changes && polygonData ? polygonData : empty);
    }
  },[ready,bounds,comparison,preview,station.changes,polygonData,packet]);

  // Sentinel-2 ground tracks, with each satellite placed where it is right now.
  useEffect(()=>{
    const base=maps.current?.base;
    if (!ready || !base) return;
    const show=station.tracks && trackData;
    base.getSource<maplibregl.GeoJSONSource>('tracks')?.setData(show ? {type:'FeatureCollection',
      features:Object.values(trackData).map(coordinates=>feature({type:'MultiLineString',coordinates}))} : empty);
    function place() {
      if (!show || !base) {Object.values(markers.current).forEach(m=>m.remove());markers.current={};return;}
      const minutes=(Date.now()-trackTime)/60000;
      for (const [name,segments] of Object.entries(trackData)) {
        const flat=segments.flat();
        const i=Math.min(Math.floor(minutes),flat.length-2), t=Math.min(1,minutes-i);
        if (i<0||!flat[i+1]) continue;
        const [x0,y0]=flat[i],[x1,y1]=flat[i+1];
        const position:Point=Math.abs(x1-x0)>180?flat[i]:[x0+(x1-x0)*t,y0+(y1-y0)*t];
        if (!markers.current[name]) {
          const element=document.createElement('div');
          element.className='sat-marker';
          element.innerHTML='<i></i><span>'+name.replace(/sentinel-?/i,'S').toUpperCase()+'</span>';
          markers.current[name]=new Marker({element,opacityWhenCovered:'0'}).setLngLat(position).addTo(base);
        } else markers.current[name].setLngLat(position);
      }
    }
    place();
    const timer=window.setInterval(place,5000);
    return ()=>clearInterval(timer);
  },[ready,station.tracks,trackData,trackTime]);

  function swipeFrom(event:React.PointerEvent) {
    const box=viewEl.current!.getBoundingClientRect();
    const move=(e:PointerEvent)=>station.set({swipe:Math.max(2,Math.min(98,(e.clientX-box.left)/box.width*100))});
    const stop=()=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',stop);};
    window.addEventListener('pointermove',move);window.addEventListener('pointerup',stop);
    event.preventDefault();
  }
  function home() {
    const base=maps.current?.base;
    if (!base) return;
    base.flyTo({...HOME,bearing:0,pitch:0,padding:sidePad(),duration:2200});
  }
  const sceneA=packet?.scenes.find(s=>s.role==='A');
  const sceneB=packet?.scenes.find(s=>s.role==='B');
  const zoom=(delta:number)=>{idle.current=false;maps.current?.base.easeTo({zoom:maps.current.base.getZoom()+delta,duration:300});};

  return <div className="globe" ref={viewEl}>
    <div className="stars"/>
    <div ref={baseEl} className="map-layer"/>
    <div ref={afterEl} className="map-layer after-layer" style={{clipPath:comparison?'inset(0 0 0 '+station.swipe+'%)':'inset(0 0 0 100%)'}}/>
    {canvasError&&<div className="canvas-error">{comparison ? <>
      <img src={preview+'a'} alt="Before"/><img src={preview+'b'} alt="After" style={{clipPath:'inset(0 0 0 '+station.swipe+'%)'}}/>
    </> : 'This browser cannot draw the 3D globe (WebGL is off). Results still appear in the panel.'}</div>}
    {comparison&&<>
      <div className="swipe" style={{left:station.swipe+'%'}} onPointerDown={swipeFrom} role="slider" aria-label="Drag to compare before and after"
        aria-valuenow={Math.round(station.swipe)} aria-valuemin={0} aria-valuemax={100} tabIndex={0}
        onKeyDown={e=>{if(e.key==='ArrowLeft')station.set({swipe:Math.max(2,station.swipe-4)});if(e.key==='ArrowRight')station.set({swipe:Math.min(98,station.swipe+4)});}}>
        <span>‹ ›</span>
      </div>
      <div className="swipe-tag before" style={{right:(100-station.swipe)+'%'}}><small>BEFORE</small>{day(sceneA?.datetime || station.dateA)}</div>
      <div className="swipe-tag after" style={{left:station.swipe+'%'}}><small>AFTER</small>{day(sceneB?.datetime || station.dateB)}</div>
    </>}
    {draw&&<div className="draw-hint">
      <b>{draw==='drop'?'Click anywhere to place a 5 km box':draw==='box'?(points.length?'Now click the opposite corner':'Click one corner of the area'):'Click each corner of the area'}</b>
      {draw==='polygon'&&<button disabled={points.length<3} onClick={()=>select({geometry:{type:'Polygon',coordinates:[[...points,points[0]]]}})}>Finish</button>}
      <button onClick={()=>station.set({draw:null})}>Cancel</button>
    </div>}
    <div className="map-tools">
      {comparison&&<button aria-pressed={station.changes} title="Show or hide the change outlines" onClick={()=>station.set({changes:!station.changes})}><Layers2 size={16}/><span>Change</span></button>}
      <button aria-pressed={station.tracks} title="Show Sentinel-2 orbits" onClick={()=>station.set({tracks:!station.tracks})}><Orbit size={16}/><span>Orbits</span></button>
      <button title="Back to the whole globe" onClick={home}><Globe2 size={16}/></button>
      <button title="Zoom in" onClick={()=>zoom(1)}><Plus size={16}/></button>
      <button title="Zoom out" onClick={()=>zoom(-1)}><Minus size={16}/></button>
    </div>
    <div className="coords"><span ref={coordEl}>Move over the globe</span></div>
  </div>;
}
