import {create} from 'zustand';
import type {AOI, Intent, Language, Mode, Result, Stage} from './types';
export type Tab = 'Ask'|'Evidence'|'Orbit'|'Link';
type State = {
  aoi:AOI|null; dateA:string; dateB:string; intent:Intent|''; language:Language; question:string;
  demoId:string|null; mode:Mode; tab:Tab; stages:Stage[]; result:Result|null; currentId:string|null;
  busy:boolean; error:string; swipe:number; changes:boolean; tracks:boolean;
  selectedPatch:number|null; focus:[number,number,number,number]|null; patchOpacity:number;
  set:(patch:Partial<State>)=>void; stage:(event:Stage)=>void;
};
export const useStation = create<State>(set => ({
  aoi:null,dateA:'',dateB:'',intent:'',language:'en',question:'',demoId:null,mode:'demo-fast',
  tab:'Ask',stages:[],result:null,currentId:null,busy:false,error:'',swipe:50,changes:true,tracks:false,selectedPatch:null,focus:null,patchOpacity:0.4,
  set:patch=>set(patch),
  stage:event=>set(state=>({stages:[...state.stages.filter(row=>row.stage!==event.stage),event]})),
}));
