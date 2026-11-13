import type { FeatureCollection, Geometry, Polygon } from 'geojson';
export type Point = [number, number];
export type AOI = { geometry: Polygon; name?: string };
export type Intent = 'water_gain'|'water_loss'|'veg_gain'|'veg_loss'|'urban_growth'|'burn'|'visible_change';
export type Language = 'en'|'ar'|'hi';
export type Mode = 'demo-fast'|'sband-2m'|'uhf-9k6';
export const intents: Record<Intent, string> = {
  water_gain: 'Surface water gain', water_loss: 'Surface water loss',
  veg_gain: 'Vegetation gain', veg_loss: 'Vegetation loss', urban_growth: 'Urban growth', burn: 'Burn-related change',
  visible_change: 'Visible change · FresUNet',
};
export const intentColor: Record<Intent, string> = {
  water_gain:'#38bdf8', water_loss:'#fbbf24', veg_gain:'#4ade80', veg_loss:'#f87171',
  urban_growth:'#22d3ee', burn:'#fb7185', visible_change:'#e879f9',
};
export const intentNoun: Record<Intent, string> = {
  water_gain:'new water', water_loss:'lost water', veg_gain:'new vegetation', veg_loss:'lost vegetation',
  urban_growth:'new built-up land', burn:'burned land', visible_change:'visible change',
};
export type Bounds = [number,number,number,number];
export function featureBounds(geometry:Geometry):Bounds {
  const xs:number[]=[], ys:number[]=[];
  const walk=(value:unknown)=>{
    if (Array.isArray(value)&&typeof value[0]==='number') {xs.push(value[0] as number);ys.push(value[1] as number);}
    else if (Array.isArray(value)) value.forEach(walk);
  };
  if ('coordinates' in geometry) walk(geometry.coordinates);
  return [Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)];
}
export type Demo = {id:string; title:string; aoi:AOI; date_a:string; date_b:string; intent:Intent; question:string; synthetic:boolean};
export type Scene = {role:string; item_ids:string[]; datetime:string; requested_date:string; aoi_cloud_pct:number; source:string};
export type Packet = {
  analysis_id:string; aoi:AOI; intent:Intent; status:string; scenes:Scene[]; image_bounds:Point[];
  change_geojson:FeatureCollection|null; attribution:string[];
  trust:{score:number; level:string; flags:string[]; caveats:string[]; thresholds:Record<string,unknown>};
  stats:{changed_ha:number;changed_pct:number;valid_overlap_pct:number;aoi_ha:number;n_polygons:number;
    largest_polygon_ha:number;artefact_ha:number;from_to:{from_class:string;ha:number}[]}|null;
  link:{mode:string;packet_bytes:number;raw_equivalent_bytes:number|null;saved_pct:number|null;
    simulated_seconds:number;raw_simulated_seconds:number|null}|null;
};
export type Result = {packet:Packet;explanation:{headline:string;body:string;caveats:string[]};plan:{language:Language}};
export type Stage = {stage:string;status:string;ms:number|null;replayed:boolean;detail:{message?:string;artifact_id?:string;image_bounds?:Point[];change_geojson?:FeatureCollection}};
export type Pass = {satellite:string;time_utc:string;cross_track_km:number;cloud_pct:number|null};
export type Place = {name:string;country:string;lat:number;lon:number;aoi:AOI};
export type Telemetry = {state:string;mode:string;link_mode:Mode;mem_used_mb:number;power_w:number|null;temp_c:number|null};
export type Tracks = Record<string,Point[][]>;

export async function api<T>(path:string, body?:unknown, signal?:AbortSignal):Promise<T> {
  const response = await fetch('/api'+path, body ? {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal,
  } : {signal});
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(typeof problem.detail === 'string' ? problem.detail : 'The request could not be completed.');
  }
  return response.json();
}

export const number = (value:number, digits=1) => new Intl.NumberFormat(undefined,{maximumFractionDigits:digits}).format(value);
export const moment = (utc:string) => new Date(utc).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});
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
