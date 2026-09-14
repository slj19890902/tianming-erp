import {useEffect, useRef, useState} from "react";
import "./warehouseGoods.css";
import {goodsSearchText,matchesGoodsSearch,loadGoodsPinyin,type GoodsPinyin} from "./goodsSearch.mjs";

type Facts = {scope:"general"|"customers"; customer_ids:number[]; product_ids:number[];
  material_code:string; verified_material_id:number|null;
  processing:"raw"|"cut"|"die_cut"|"creased"|"printed"; mold_tool_id:number|null; note:string};
type Options = {customers:{id:number;name:string;full_name:string;code:string}[]; products:{id:number;customer_id:number;name:string;code:string;mold_tool_id:number|null}[];
  materials:{id:number;code:string;supplier:string;layer_count:number;is_white_face:boolean}[]; molds:{id:number;name:string;search_text?:string}[]};
const initialFacts=(raw:boolean):Facts=>({scope:"general",customer_ids:[],product_ids:[],material_code:"",verified_material_id:null,
  processing:raw?"raw":"cut",mold_tool_id:null,note:""});
async function api(path:string,init?:RequestInit) {
  const response=await fetch(`/api/warehouse/goods${path}`,{credentials:"same-origin",cache:"no-store",...init});
  const data=await response.json();
  if(!response.ok)throw new Error(typeof data.detail==="string"?data.detail:Array.isArray(data.detail)?data.detail.map((e:{msg:string})=>e.msg).join("；"):"操作失败，请刷新核对");
  return data;
}
function FactsEditor({facts,setFacts,options,raw,flute,canPrice,length,width,quantity}:{facts:Facts;setFacts:(f:Facts)=>void;options:Options;raw:boolean;flute:string;canPrice:boolean;length:string;width:string;quantity:string}) {
  const [customerSearch,setCustomerSearch]=useState(""),[productSearch,setProductSearch]=useState(""),[moldSearch,setMoldSearch]=useState("");
  const [runtime,setRuntime]=useState<GoodsPinyin|null>(null),[materialOpen,setMaterialOpen]=useState(false),[quote,setQuote]=useState("");
  useEffect(()=>{let alive=true;void loadGoodsPinyin().then(r=>{if(alive)setRuntime(r);});return()=>{alive=false};},[]);
  useEffect(()=>{let alive=true;setQuote("");if(!facts.verified_material_id||!canPrice)return;
    const timer=setTimeout(()=>void api(`/material-price?material_id=${facts.verified_material_id}&flute_type=${encodeURIComponent(flute)}&length_mm=${Number(length)||0}&width_mm=${Number(width)||0}&quantity=${Number(quantity)||0}`).then(d=>{if(alive)setQuote(`当前报价 ${d.square_price} ${d.unit||"元/㎡"} · ${d.tax_included?"含税":"未税"}${d.unit_price ? ` · ${d.unit_price}元/张` : ""}${d.total_price ? ` · 合计${d.total_price}元` : ""}`);}).catch(e=>{if(alive)setQuote(e.message);}),200);return()=>{alive=false;clearTimeout(timer)};
  },[facts.verified_material_id,flute,canPrice,length,width,quantity]);
  const search=(text:string,query:string)=>matchesGoodsSearch(goodsSearchText(text,runtime),query);
  const update=(p:Partial<Facts>)=>setFacts({...facts,...p});
  const toggle=(ids:number[],id:number)=>ids.includes(id)?ids.filter(v=>v!==id):[...ids,id];
  const products=options.products.filter(p=>(facts.scope==="general"||facts.customer_ids.includes(p.customer_id))&&
    (!facts.mold_tool_id||p.mold_tool_id===facts.mold_tool_id)&&`${p.code} ${p.name} ${options.customers.find(c=>c.id===p.customer_id)?.name}`.toLowerCase().includes(productSearch.toLowerCase()));
  return <div className="goods-fields">
    <label>适用范围<select value={facts.scope} onChange={e=>update({scope:e.target.value as Facts["scope"],customer_ids:[],product_ids:[]})}><option value="general">通用（所有客户）</option><option value="customers">指定一家或多家客户</option></select></label>
    {facts.scope==="customers"&&<div><input placeholder="客户名称、拼音或首字母" value={customerSearch} onChange={e=>setCustomerSearch(e.target.value)}/><div className="goods-checks">{options.customers.filter(c=>search(`${c.name} ${c.full_name} ${c.code}`,customerSearch)).map(c=><label key={c.id}><input type="checkbox" checked={facts.customer_ids.includes(c.id)} onChange={()=>{const ids=toggle(facts.customer_ids,c.id);update({customer_ids:ids,product_ids:facts.product_ids.filter(id=>ids.includes(options.products.find(p=>p.id===id)?.customer_id||0))});}}/><span>{c.name}</span></label>)}</div></div>}
    {!raw&&<label>已经完成的加工<select value={facts.processing} onChange={e=>update({processing:e.target.value as Facts["processing"]})}><option value="cut">裁切 / 衬板净片</option><option value="die_cut">模切，待印刷或后加工</option><option value="creased">已压线</option><option value="printed">已印刷，待后加工</option></select></label>}
    {!raw&&<label>使用模具（可选）<input aria-label="筛选模具" placeholder="输入模具编号、名称或拼音筛选" value={moldSearch} onChange={e=>setMoldSearch(e.target.value)}/><select value={facts.mold_tool_id||""} onChange={e=>update({mold_tool_id:Number(e.target.value)||null,product_ids:[]})}><option value="">未指定模具，按逐款用途确认</option>{options.molds.filter(m=>m.id===facts.mold_tool_id||search(m.search_text||m.name,moldSearch)).map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select></label>}
    <div className="goods-material-picker"><label>材质代码<input value={facts.material_code} maxLength={100} placeholder="输入代码，选择供应商对应材质" onFocus={()=>setMaterialOpen(true)} onChange={e=>{update({material_code:e.target.value,verified_material_id:null});setMaterialOpen(true);}}/></label>
      {materialOpen&&<div className="goods-material-results">{options.materials.filter(m=>search(`${m.code} ${m.supplier}`,facts.material_code)).slice(0,30).map(m=><button type="button" key={m.id} onClick={()=>{update({verified_material_id:m.id,material_code:m.code});setMaterialOpen(false);}}><b>{m.code}</b><span>{m.supplier}{m.is_white_face?" · 白色":""}</span></button>)}<button type="button" className="goods-picker-close" onClick={()=>setMaterialOpen(false)}>收起</button></div>}
      {facts.verified_material_id&&<small>{options.materials.find(m=>m.id===facts.verified_material_id)?.supplier} {canPrice&&<>· {quote||"正在读取报价…"}</>}</small>}
    </div>
    <details><summary>确认可用产品（已选 {facts.product_ids.length} 款）</summary><p>模切、压线、已印刷片料必须逐款确认；未选产品的通用净片仍须符合物理规格。</p><input placeholder="客户、存货编码、产品名称" value={productSearch} onChange={e=>setProductSearch(e.target.value)}/><div className="goods-checks">{products.slice(0,80).map(p=><label key={p.id}><input type="checkbox" checked={facts.product_ids.includes(p.id)} onChange={()=>update({product_ids:toggle(facts.product_ids,p.id)})}/><span>{options.customers.find(c=>c.id===p.customer_id)?.name} · {p.code}<small>{p.name}</small></span></label>)}</div><small>符合 {products.length} 款，显示前80款；输入客户或编码可缩小范围。</small>{facts.product_ids.length>0&&<div className="goods-checks">{facts.product_ids.map(id=><label key={id}><input type="checkbox" checked onChange={()=>update({product_ids:facts.product_ids.filter(v=>v!==id)})}/>{options.products.find(p=>p.id===id)?.code||id} · 已选</label>)}</div>}</details>
    <label>说明<textarea maxLength={1000} value={facts.note} onChange={e=>update({note:e.target.value})}/></label>
  </div>;
}

export function WarehouseGoods({lotId,locationId,layoutVersion,raw=false,canSave,onSaved,onBusyChange}:{lotId?:number;locationId?:number;layoutVersion?:number;raw?:boolean;canSave:boolean;onSaved:()=>Promise<unknown>;onBusyChange?:(busy:boolean)=>void}) {
  const [facts,setFacts]=useState<Facts>(initialFacts(raw)),[options,setOptions]=useState<Options|null>(null),[version,setVersion]=useState(0),[editable,setEditable]=useState(true);
  const [message,setMessage]=useState(""),[busy,setBusy]=useState(false),[physical,setPhysical]=useState("");
  const [name,setName]=useState(""),[length,setLength]=useState(""),[width,setWidth]=useState(""),[quantity,setQuantity]=useState("");
  const [layer,setLayer]=useState(3),[flute,setFlute]=useState("B"),[stockDate,setStockDate]=useState(new Date().toLocaleDateString("en-CA",{timeZone:"Asia/Shanghai"}));
  const [pieces,setPieces]=useState("1"),[yieldPerSheet,setYield]=useState("1"),[component,setComponent]=useState("whole"),[source,setSource]=useState("existing_stocktake");
  const [creaseType,setCreaseType]=useState(""),[creaseLeft,setCreaseLeft]=useState(""),[creaseMiddle,setCreaseMiddle]=useState(""),[creaseRight,setCreaseRight]=useState("");
  const pending=useRef<{signature:string;key:string}|null>(null),saving=useRef(false);
  useEffect(()=>{let alive=true;void Promise.all([api("/options"),lotId?api(`/${lotId}`):Promise.resolve(null)]).then(([o,d])=>{if(!alive)return;setOptions(o);if(d){setFacts(d.facts);setFlute(d.physical.flute);setVersion(d.version);setEditable(d.editable);setPhysical(`${d.physical.name||"片料"} · ${d.physical.length}×${d.physical.width} · ${d.physical.flute}楞 · 原入库材质 ${d.physical.material}${d.physical.settlement_unit_price ? ` · 入库结算单价 ${d.physical.settlement_unit_price}元/张` : ""}`);}}).catch(e=>{if(alive)setMessage(e.message);});return()=>{alive=false};},[lotId]);
  const save=async()=>{
    if(saving.current||!canSave||!options)return;
    const payload=lotId?{facts,expected_version:version}:{facts,location_id:locationId,expected_layout_version:layoutVersion,
      quantity:Number(quantity),stock_date:stockDate,internal_name:name,board_length_mm:Number(length),board_width_mm:Number(width),layer_count:layer,flute_type:flute,
      pieces_per_box:Number(pieces),stock_yield_per_sheet:Number(yieldPerSheet),component_type:component,source_kind:source,
      crease_type:creaseType||null,crease_left_mm:creaseLeft?Number(creaseLeft):null,crease_middle_mm:creaseMiddle?Number(creaseMiddle):null,crease_right_mm:creaseRight?Number(creaseRight):null};
    const signature=JSON.stringify(payload);
    if(pending.current?.signature!==signature)pending.current={signature,key:`goods-${Date.now()}-${Math.random().toString(36).slice(2)}`};
    saving.current=true;setBusy(true);onBusyChange?.(true);setMessage("");
    try{const result=await api(lotId?`/${lotId}`:"/sheet-entry",{method:lotId?"PUT":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...payload,idempotency_key:pending.current.key})});
      setVersion(result.version);setFacts(result.facts);setMessage(lotId?"适用资料已保存，库存数量未改变":"货物已入库");pending.current=null;
      if(!lotId){setQuantity("");setName("");}
      try{await onSaved();}catch{setMessage("已保存；地图刷新失败，请刷新核对后再操作");setEditable(false);}
    }catch(e){setMessage(e instanceof Error?e.message:"保存失败");}finally{saving.current=false;setBusy(false);onBusyChange?.(false);}
  };
  return <section className="warehouse-goods"><h4>{lotId?"适用客户与加工资料":raw?"新增原材料（张）":"新增半成品（张）"}</h4>
    {physical&&<p>{physical}</p>}
    {options&&<fieldset disabled={busy||!canSave||!editable}>
      {!lotId&&<div className="goods-fields"><label>货物名称<input value={name} maxLength={200} onChange={e=>setName(e.target.value)} placeholder={raw?"例如 B楞纸板":"例如 通用衬板 / 模切待印刷片"}/></label>
        <div className="goods-pair"><label>纸板长（mm）<input type="number" inputMode="numeric" step={1} min={1} value={length} onChange={e=>setLength(e.target.value)}/></label><label>纸板宽（mm）<input type="number" inputMode="numeric" step={1} min={1} value={width} onChange={e=>setWidth(e.target.value)}/></label></div>
        <div className="goods-pair"><label>层数<select value={layer} onChange={e=>{const n=Number(e.target.value);setLayer(n);setFlute(n===3?"B":n===5?"AB":"AAA");}}>{[3,5,7].map(n=><option key={n} value={n}>{n}层</option>)}</select></label><label>楞型<select value={flute} onChange={e=>setFlute(e.target.value)}>{(layer===3?["A","B","E"]:layer===5?["AB","BE"]:["AAA","ABC"]).map(v=><option key={v}>{v}</option>)}</select></label></div>
        <div className="goods-pair"><label>实际数量（张）<input type="number" inputMode="numeric" step={1} min={1} value={quantity} onChange={e=>setQuantity(e.target.value)}/></label><label>库存日期<input type="date" value={stockDate} onChange={e=>setStockDate(e.target.value)}/></label></div>
        <details><summary>片数换算与来源</summary><label>组件<select value={component} onChange={e=>setComponent(e.target.value)}><option value="whole">整片</option><option value="cover">盖片</option><option value="base">底片</option></select></label><label>每箱所需片数<input type="number" min={1} value={pieces} onChange={e=>setPieces(e.target.value)}/></label><label>每库存张产出片数<input type="number" min={1} value={yieldPerSheet} onChange={e=>setYield(e.target.value)}/></label><label>来源<select value={source} onChange={e=>setSource(e.target.value)}><option value="existing_stocktake">本厂现场盘点发现</option><option value="partner_transfer">合作纸箱厂搬入</option></select></label></details>
      </div>}
      <FactsEditor facts={facts} setFacts={setFacts} options={options} raw={lotId?facts.processing==="raw":raw} flute={flute} canPrice={canSave&&!lotId} length={length} width={width} quantity={quantity}/>
      {!lotId&&facts.processing==="creased"&&<div className="goods-fields"><label>压线类型<input value={creaseType} onChange={e=>setCreaseType(e.target.value)} placeholder="按实际压线类型填写"/></label>{[["左段",creaseLeft,setCreaseLeft],["中段",creaseMiddle,setCreaseMiddle],["右段",creaseRight,setCreaseRight]].map(([label,value,setter])=><label key={String(label)}>{String(label)}（mm）<input type="number" min={0} value={String(value)} onChange={e=>(setter as (s:string)=>void)(e.target.value)}/></label>)}</div>}
      {canSave&&<button type="button" className="twin-primary-action" onClick={()=>void save()}>{busy?"保存中…":lotId?"保存适用资料":"确认增加库存"}</button>}
    </fieldset>}
    {!options&&!message&&<p>正在读取可选资料…</p>}{!editable&&<p>批次已预占或不可用，请解除相关预占后再维护用途。</p>}{message&&<p role="status">{message}</p>}
  </section>;
}
