import {useEffect,useState} from 'react';

declare global {
  interface Window {
    TMEntryProduct: {open(options:{productId:number;stockStage?:string}):Promise<unknown>;preview(id:number,stage?:string):Promise<string>};
  }
}

export function EntryProductButton({productId,stockStage,onSaved}:{productId:number;stockStage:string;onSaved:()=>void}){
  const [message,setMessage]=useState(''),[busy,setBusy]=useState(false),[revision,setRevision]=useState(0);
  useEffect(()=>{
    let current=true;setMessage('正在核对入库成本…');
    window.TMEntryProduct.preview(productId,stockStage).then(text=>{if(current)setMessage(text);}).catch(error=>{if(current)setMessage(error.message);});
    return()=>{current=false;};
  },[productId,stockStage,revision]);
  return <div><button type="button" disabled={busy} onClick={async()=>{
    if(busy)return;setBusy(true);
    try{const saved=await window.TMEntryProduct.open({productId,stockStage});if(saved){setRevision(v=>v+1);onSaved();}}
    catch(error){setMessage((error as Error).message);}finally{setBusy(false);}
  }}>编辑常用箱材质 / 规格</button><p aria-live="polite">{message}</p></div>;
}
