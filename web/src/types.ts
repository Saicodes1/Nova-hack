import type { FeatureCollection, Polygon } from 'geojson';
export type Point = [number, number];
export type AOI = { geometry: Polygon; name?: string };
export type Intent = 'water_gain'|'water_loss'|'veg_gain'|'veg_loss'|'urban_growth'|'burn'|'visible_change';
export type Language = 'en'|'ar'|'hi';
export type Mode = 'demo-fast'|'sband-2m'|'uhf-9k6';

/** Plain-language names for each kind of change the satellite can look for. */
export const intents: Record<Intent, {label:string; short:string; hint:string}> = {
  water_gain: {label:'New water', short:'new surface water', hint:'Flooding, new lakes, filled reservoirs'},
  water_loss: {label:'Lost water', short:'surface water loss', hint:'Drying lakes, receding floods'},
  veg_loss: {label:'Lost vegetation', short:'vegetation loss', hint:'Deforestation, cleared fields'},
  veg_gain: {label:'New vegetation', short:'vegetation gain', hint:'Regrowth, new crops, greening'},
  urban_growth: {label:'New buildings', short:'built-up growth', hint:'Construction, roads, new districts'},
  burn: {label:'Burn scars', short:'burned area', hint:'Wildfire and burn damage'},
  visible_change: {label:'Anything visible', short:'visible change', hint:'Neural model, needs FresUNet on the node'},
};
export type Demo = {id:string; title:string; name:string; aoi:AOI; date_a:string; date_b:string; intent:Intent;
  question:string; synthetic:boolean; exact?:boolean; validation?:string};
export type WatchPreset = {id:string; title:string; name:string; aoi:AOI; intent:Intent; question:string;
  baseline_date:string; start_date:string; end_date:string|null; floor_ha:number};
export type Scene = {role:string; item_ids:string[]; datetime:string; requested_date:string; aoi_cloud_pct:number;
  days_from_requested:number; source:string};
export type Packet = {
  analysis_id:string; aoi:AOI; intent:Intent; status:'ok'|'no_change'|'refused_cloudy'|'no_scenes'; scenes:Scene[];
  image_bounds:Point[]; change_geojson:FeatureCollection|null; attribution:string[]; onboard_verdict:string;
  report:'full'|'quiet'; timings_ms:Record<string,number>; thumbnails:Record<string,string>;
  trust:{score:number; level:string; flags:string[]; caveats:string[]; thresholds:Record<string,unknown>};
  stats:{changed_ha:number;changed_pct:number;valid_overlap_pct:number;aoi_ha:number;n_polygons:number;
    largest_polygon_ha:number;artefact_ha:number;from_to:{from_class:string;ha:number}[]}|null;
  link:{mode:string;packet_bytes:number;raw_equivalent_bytes:number|null;saved_pct:number|null;
    simulated_seconds:number;raw_simulated_seconds:number|null}|null;
};
export type Result = {packet:Packet;explanation:{headline:string;body:string;caveats:string[]};plan:{language:Language}};
export type Stage = {stage:string;status:string;ms:number|null;replayed:boolean;
  detail:{message?:string;artifact_id?:string;image_bounds?:Point[];change_geojson?:FeatureCollection;
    valid_overlap_pct?:number;scene_ids?:string[];ahead?:number;detector?:string;packet_bytes?:number}};
export type OrbitPass = {satellite:string;time_utc:string;cross_track_km:number;cloud_pct:number|null};
/** A real past overpass of the selected area. */
export type Overpass = {date:string;time_utc:string;platform:string;cloud_pct:number;source:string};
export type Place = {name:string;country:string;lat:number;lon:number;aoi:AOI};
export type Telemetry = {state:string;mode:string;link_mode:Mode;mem_used_mb:number;power_w:number|null;temp_c:number|null;verdict_writer?:string|null;power_source?:string|null};
export type Recent = {analysis_id:string;question:string;place:string|null;intent:Intent;source:'desk'|'phone';asked_at:string};
export type Health = {public_url:string; node_mode:string; planner:string; openai:{planner:boolean;explainer:boolean};
  node_info:{host?:string;machine?:string;verdict_writer?:string;verdict_model_ready?:boolean;power_source?:string}|null};
export type Tracks = Record<string,Point[][]>;
export type WatchEntry = {date:string;time_utc:string;platform:string;
  outcome:'processing'|'alert'|'quiet'|'cloudy'|'no_scene'|'error';analysis_id:string|null;verdict:string;
  changed_ha:number|null;clear_pct:number|null;trust_level:string|null;packet_bytes:number|null;raw_bytes:number|null};
export type Watch = {id:string;aoi:AOI;intent:Intent;question:string;baseline_date:string;start_date:string;
  end_date:string|null;floor_ha:number;status:'starting'|'running'|'armed'|'done'|'stopped'|'failed';message:string;
  baseline:Overpass|null;entries:WatchEntry[];
  totals:{passes:number;alerts:number;quiet:number;cloudy:number;sent_bytes:number;raw_bytes:number|null}};
export type Validation = {detected_ha:number;prodes_ha:number;intersection_ha:number;precision:number;recall:number;
  iou:number;false_positive_ha:number;false_positive_on_earlier_prodes_share:number;missed_prodes_ha:number;
  source:{layer:string;fetched:string}};

export async function api<T>(path:string, body?:unknown, signal?:AbortSignal, method?:string):Promise<T> {
  const response = await fetch('/api'+path, body ? {
    method:method??'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal,
  } : {signal, method});
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(typeof problem.detail === 'string' ? problem.detail : 'The request could not be completed.');
  }
  return response.json();
}

export const number = (value:number, digits=1) => new Intl.NumberFormat('en',{maximumFractionDigits:digits}).format(value);
export const moment = (utc:string) => new Date(utc).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});
/** "12 Apr 2024" from an ISO date or timestamp, without timezone drift. */
export const day = (iso?:string|null, year=true) => iso ? new Date(iso.slice(0,10)+'T12:00:00').toLocaleDateString('en-GB',
  {day:'numeric',month:'short',...(year?{year:'numeric'}:{})}) : '';
export const shift = (iso:string, days:number) => {
  const date = new Date(iso.slice(0,10)+'T12:00:00Z');
  date.setUTCDate(date.getUTCDate()+days);
  return date.toISOString().slice(0,10);
};
export const today = () => new Date().toISOString().slice(0,10);
export const bytes = (size:number) => size>=1e9 ? number(size/1e9,2)+' GB' : size>=1e6 ? number(size/1e6,1)+' MB'
  : size>=1000 ? number(size/1000,1)+' KB' : number(size,0)+' B';
export const duration = (seconds:number) => seconds>=172800 ? number(seconds/86400,1)+' days' : seconds>=7200 ? number(seconds/3600,1)+' hours'
  : seconds>=120 ? number(seconds/60,1)+' min' : seconds>=1 ? number(seconds,1)+' s' : number(seconds*1000,0)+' ms';
export const rates: Record<Mode,{bps:number;label:string;detail:string}> = {
  'demo-fast':{bps:2_000_000,label:'Instant',detail:'no radio delay'},
  'sband-2m':{bps:2_000_000,label:'S-band',detail:'2 Mbit/s'},
  'uhf-9k6':{bps:9600,label:'UHF',detail:'9.6 kbit/s'},
};
export const center = (aoi:AOI):Point => {
  const points = aoi.geometry.coordinates[0].slice(0,-1);
  return [0,1].map(axis => points.reduce((sum, p) => sum+p[axis],0)/points.length) as Point;
};
export function areaKm(aoi:AOI) {
  const points = aoi.geometry.coordinates[0];
  const [lon,lat] = points[0];
  const kmX = 111.32 * Math.cos(lat * Math.PI/180);
  const kmY = 110.57;
  return Math.abs(points.slice(1).reduce((sum,p,i) =>
    sum+(points[i][0]-lon)*(p[1]-lat)-(p[0]-lon)*(points[i][1]-lat),0))*kmX*kmY/2;
}
export const cloudTone = (pct:number) => pct<20 ? 'clear' : pct<60 ? 'partial' : 'cloudy';
