import {create} from 'zustand';
import type {AOI, Intent, Language, Mode, Result, Stage, Watch} from './types';

export type Tab = 'Answer'|'Evidence'|'Downlink'|'Next passes';
export type Task = 'ask'|'watch';
type State = {
  // what is being asked
  task:Task; aoi:AOI|null; dateA:string; dateB:string; exactA:boolean; exactB:boolean;
  intent:Intent|''; language:Language; question:string; demoId:string|null; presetId:string|null; mode:Mode;
  // standing watch settings
  watchFrom:string; watchUntil:string; floorHa:number; source:'desk'|'phone';
  // what came back
  tab:Tab; stages:Stage[]; result:Result|null; currentId:string|null; busy:boolean; error:string;
  watch:Watch|null; watchRow:string|null;
  // map
  swipe:number; changes:boolean; tracks:boolean; draw:'drop'|'box'|'polygon'|null;
  set:(patch:Partial<State>)=>void; stage:(event:Stage)=>void; reset:()=>void;
};
const cleared = {stages:[],result:null,currentId:null,busy:false,error:'',watch:null,watchRow:null,swipe:50};
export const useStation = create<State>(set => ({
  task:'ask',aoi:null,dateA:'',dateB:'',exactA:false,exactB:false,intent:'',language:'en',question:'',
  demoId:null,presetId:null,mode:'demo-fast',watchFrom:'',watchUntil:'',floorHa:10,source:'desk',
  tab:'Answer',...cleared,changes:true,tracks:true,draw:null,
  set:patch=>set(patch),
  stage:event=>set(state=>({stages:[...state.stages.filter(row=>row.stage!==event.stage),event]})),
  reset:()=>set({...cleared,tab:'Answer'}),
}));
