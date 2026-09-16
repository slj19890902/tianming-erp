import { WarehouseGoods } from "./WarehouseGoods";
import { useEffect, useRef, useState } from "react";
import "./materialCandidates.css";

interface Candidate { product_id:number; customer_name:string; inventory_code:string; product_name:string; box_style:string;
  exact_dimension_match:boolean; near_dimension_match:boolean; dimension_basis:string; score:number; selectable:boolean; length_mm:number; width_mm:number; flute_type:string; warnings:string[] }
interface Result { version:number; source:string; dimension_notice:string; items:Candidate[]; saved:Candidate[]; editable:boolean; saved_at:string|null }
async function request(lotId:number, init?:RequestInit):Promise<Result> {
  const response = await fetch(`/api/warehouse/lots/${lotId}/material-candidates`, {credentials:"same-origin", cache:"no-store", ...init});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "匹配读取失败，请重试");
  return data;
}
export function MaterialCandidates({lotId, canSave, onSaved}:{lotId:number;canSave:boolean;onSaved:()=>Promise<unknown>}) {
  const [search,setSearch] = useState("");
  const [boxStyle,setBoxStyle] = useState("");
  const [open,setOpen] = useState(false), [more,setMore] = useState(false);
  const [showCuttable,setShowCuttable] = useState(false);
  const [data,setData] = useState<Result|null>(null), [selected,setSelected] = useState<number[]>([]);
  const [busy,setBusy] = useState(false), [message,setMessage] = useState("");
  const alive = useRef(true), pending = useRef<{signature:string;key:string}|null>(null);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false};},[]);
  const accept = (result:Result)=>{setData(result);setSelected(result.saved.map(item=>item.product_id));};
  const load = async()=>{
    setBusy(true);setMessage("");
    try {const result=await request(lotId);if(alive.current) {accept(result);pending.current=null;}}
    catch(error){if(alive.current)setMessage(error instanceof Error?error.message:"读取失败");}
    finally{if(alive.current)setBusy(false);}
  };
  const save = async()=>{
    if(!data || busy)return;
    const payload={product_ids:selected,expected_version:data.version};
    const signature=JSON.stringify(payload);
    if(pending.current?.signature!==signature)pending.current={signature,key:`material-${lotId}-${Date.now()}-${Math.random().toString(36).slice(2)}`};
    setBusy(true);setMessage("");
    try {
      const result=await request(lotId,{method:"PUT",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({...payload,idempotency_key:pending.current.key})});
      if(!alive.current)return;
      accept(result);pending.current=null;setMessage("候选用途已保存");
      try {await onSaved();}catch{if(alive.current)setMessage("候选用途已保存，地图刷新失败，请刷新页面");}
    }catch(error){if(alive.current)setMessage(error instanceof Error?error.message:"保存失败");}
    finally{if(alive.current)setBusy(false);}
  };
  const stale=data?.saved.filter(item=>!data.items.some(current=>current.product_id===item.product_id && current.selectable)) || [];
  const filtered=data?.items.filter(item=>(showCuttable||item.near_dimension_match)&&(!boxStyle||item.box_style===boxStyle)&&`${item.customer_name} ${item.inventory_code} ${item.product_name}`.toLowerCase().includes(search.toLowerCase()))||[];
  return <section className="twin-material-candidates" aria-label="材料候选用途">
    <button type="button" className="twin-detail-toggle" aria-expanded={open} onClick={()=>{setOpen(!open);if(!open&&!data)void load();}}>
      {open?"收起匹配":"匹配产品 / 查看候选用途"}</button>
    {open&&<>
      {data&&<><b>匹配产品</b><small>{data.source}</small>
        <p className="material-match-note">{data.dimension_notice} 同楞型、同层数、不旋转。默认显示长宽各相差不超过10%的产品；模切形状、压线及印刷仍需核对，保存候选不等于预占库存。</p>
        <b>尺寸完全一致 {data.items.filter(item=>item.exact_dimension_match).length} 款 · 共 {data.items.length} 款</b>
        <button type="button" className="twin-detail-toggle" aria-pressed={showCuttable} onClick={()=>{setShowCuttable(!showCuttable);setMore(false);}}>{showCuttable?"只看尺寸相近":"展开其他可裁切项（非近尺寸推荐）"}</button>
        <select aria-label="匹配箱型" value={boxStyle} onChange={e=>{setBoxStyle(e.target.value);setMore(false);}}><option value="">全部箱型</option>{[...new Set(data.items.map(item=>item.box_style))].sort().map(style=><option key={style} value={style}>{style}</option>)}</select>
        <input aria-label="筛选客户或常用箱" value={search} onChange={e=>{setSearch(e.target.value);setMore(false);}} placeholder="查找客户、存货编码、产品名称"/>
        {(more?filtered:filtered.slice(0,5)).map(item=><label className="material-match-row" key={item.product_id}>
          <input type="checkbox" aria-label={`候选用途 ${item.inventory_code}`} checked={selected.includes(item.product_id)}
            disabled={busy||!canSave||!data.editable||(!item.selectable&&!selected.includes(item.product_id))}
            onChange={event=>setSelected(current=>event.target.checked?[...current,item.product_id]:current.filter(id=>id!==item.product_id))}/>
          <span><span className="material-match-heading"><b>{item.customer_name}</b><strong>{item.exact_dimension_match?"尺寸完全一致":`${item.near_dimension_match?"尺寸相近":"其他裁切项"} · 利用率 ${item.score}%`}</strong></span>
            <b>{item.inventory_code}</b><span>{item.product_name}</span>
            <small>{item.length_mm}×{item.width_mm} mm · {item.flute_type}楞 · {item.dimension_basis}</small>
            <small className="material-match-warning">{item.warnings.join(" · ")}</small></span>
        </label>)}
        {!filtered.length&&<p>没有符合当前筛选的产品；请核对箱型和毫米尺寸，不会用单位不明的旧尺寸凑匹配。</p>}
        {filtered.length>5&&<button type="button" className="twin-detail-toggle" aria-expanded={more} onClick={()=>setMore(!more)}>{more?"收起":"更多"}（筛选后 {filtered.length} 款）</button>}
        {stale.length>0&&<details><summary>已保存、现需重新核对（{stale.length}）</summary>{stale.map(item=><label className="material-match-row" key={item.product_id}>
          <input type="checkbox" checked={selected.includes(item.product_id)} disabled={busy||!canSave||!data.editable}
            onChange={()=>setSelected(current=>current.filter(id=>id!==item.product_id))}/><span>{item.customer_name} · {item.inventory_code} · {item.product_name}<small>匹配资料已变化，请取消勾选后重新保存</small></span></label>)}</details>}
        <div className="material-match-actions"><span>已选 {selected.length} 款</span>
          {canSave&&<button type="button" disabled={busy||!data.editable} onClick={()=>void save()}>{busy?"处理中…":"保存候选用途"}</button>}</div>
        {data.saved_at&&<small>已保存 {data.saved.length} 款 · {data.saved_at.replace("T"," ").slice(0,16)}</small>}
      </>}
      {canSave&&<details><summary>人工维护适用客户、材质和加工资料</summary><WarehouseGoods key={lotId} lotId={lotId} canSave={canSave} onSaved={async()=>{await onSaved();await load();}} /></details>}
      {busy&&!data&&<p role="status">正在匹配…</p>}
      {message&&<p role="status">{message}</p>}
      <button type="button" className="twin-detail-toggle secondary" disabled={busy} onClick={()=>void load()}>重新读取匹配与已保存用途</button>
    </>}
  </section>;
}
