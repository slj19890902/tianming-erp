import {useEffect, useRef, useState} from "react";
import "./warehouseGoods.css";
import {goodsSearchText,matchesGoodsSearch,loadGoodsPinyin,type GoodsPinyin} from "./goodsSearch.mjs";

type Facts = {display_name?:string; source_customer_name?:string|null; source_customer_id?:number|null; scope:"general"|"customers"; customer_ids:number[]; product_ids:number[];
  face_paper?:"white"|"kraft"|"unknown"; dimension_source?:"unknown"|"tape"|"label"; crease_product_id?:number|null;
  material_code:string; verified_material_id:number|null;
  processing:"raw"|"cut"|"die_cut"|"creased"|"printed"|"dedicated_component"; mold_tool_id:number|null; note:string; cut_trim_mm?:number; cut_kerf_mm?:number; blank_unprinted?:boolean};
type Options = {customers:{id:number;name:string;full_name:string;code:string}[]; products:{id:number;customer_id:number;name:string;code:string;mold_tool_id:number|null;is_liner:boolean;is_a1:boolean;crease_values:(number|null)[];report_width_mm:number|null;flute_type:string}[];
  suppliers:{id:number;name:string}[];
  materials:{id:number;code:string;supplier:string;layer_count:number;is_white_face:boolean}[]; molds:{id:number;name:string;search_text?:string}[]};
const initialFacts=(raw:boolean):Facts=>({scope:"general",customer_ids:[],product_ids:[],material_code:"",verified_material_id:null,
  processing:raw?"raw":"cut",mold_tool_id:null,note:"",dimension_source:"tape"});
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
  const products=options.products.filter(p=>(facts.processing!=="die_cut"||!p.is_liner)&&(facts.processing!=="creased"||p.is_a1)&&(facts.scope==="general"||facts.customer_ids.includes(p.customer_id))&&
    (!facts.mold_tool_id||p.mold_tool_id===facts.mold_tool_id)&&`${p.code} ${p.name} ${options.customers.find(c=>c.id===p.customer_id)?.name}`.toLowerCase().includes(productSearch.toLowerCase()));
  return <div className="goods-fields">
    <label>适用范围<select value={facts.scope} onChange={e=>update({scope:e.target.value as Facts["scope"],customer_ids:[],product_ids:[]})}><option value="general">通用（所有客户）</option><option value="customers">指定一家或多家客户</option></select></label>
    {facts.scope==="customers"&&<div><input placeholder="客户名称、拼音或首字母" value={customerSearch} onChange={e=>setCustomerSearch(e.target.value)}/><div className="goods-checks">{options.customers.filter(c=>search(`${c.name} ${c.full_name} ${c.code}`,customerSearch)).map(c=><label key={c.id}><input type="checkbox" checked={facts.customer_ids.includes(c.id)} onChange={()=>{const ids=toggle(facts.customer_ids,c.id);update({customer_ids:ids,product_ids:facts.product_ids.filter(id=>ids.includes(options.products.find(p=>p.id===id)?.customer_id||0))});}}/><span>{c.name}</span></label>)}</div></div>}
    {!raw&&<label>已经完成的加工<select value={facts.processing} onChange={e=>update({processing:e.target.value as Facts["processing"],product_ids:[],mold_tool_id:null,crease_product_id:null})}><option value="cut">矩形净片（可继续模切/压线）</option><option value="die_cut">已模切成形</option><option value="creased">已压线（优先A1箱）</option><option value="printed">已印刷，待后加工</option>{facts.processing==="dedicated_component"&&<option value="dedicated_component">专用盖/底：已压线开槽，待打钉</option>}</select></label>}
    <label>尺寸来源<select value={facts.dimension_source||"unknown"} onChange={e=>update({dimension_source:e.target.value as Facts["dimension_source"]})}><option value="tape">卷尺测量（±10mm待核）</option><option value="label">标签尺寸（精确）</option><option value="unknown">旧记录 / 不确定</option></select></label>
    {facts.processing==="die_cut"&&<label><input type="checkbox" checked={!!facts.blank_unprinted} onChange={e=>update({blank_unprinted:e.target.checked})}/>确认空白未印刷（同版模具匹配）</label>}
    {["raw","cut"].includes(facts.processing)&&<details><summary>裁切余量（mm）</summary><label>每边修边<input type="number" min={0} max={500} step="0.1" value={facts.cut_trim_mm||0} onChange={e=>update({cut_trim_mm:Number(e.target.value)})}/></label><label>刀缝<input type="number" min={0} max={100} step="0.1" value={facts.cut_kerf_mm||0} onChange={e=>update({cut_kerf_mm:Number(e.target.value)})}/></label><small>0表示按净尺寸理论排料；请按现场实际设置。</small></details>}
    {!raw&&<label>使用模具（可选）<input aria-label="筛选模具" placeholder="输入模具编号、名称或拼音筛选" value={moldSearch} onChange={e=>setMoldSearch(e.target.value)}/><select value={facts.mold_tool_id||""} onChange={e=>update({mold_tool_id:Number(e.target.value)||null,product_ids:[]})}><option value="">未指定模具，按逐款用途确认</option>{options.molds.filter(m=>m.id===facts.mold_tool_id||search(m.search_text||m.name,moldSearch)).map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select></label>}
    <div className="goods-material-picker"><label>{flute==="NONE"?"原纸品种 / 克重":"材质代码"}<input value={facts.material_code} maxLength={100} placeholder={flute==="NONE"?"例如 灰底白板250克":"输入代码，选择供应商对应材质"} onFocus={()=>setMaterialOpen(true)} onChange={e=>{update({material_code:e.target.value,verified_material_id:null});setMaterialOpen(true);}}/></label>
      {materialOpen&&flute!=="NONE"&&<div className="goods-material-results">{options.materials.filter(m=>search(`${m.code} ${m.supplier}`,facts.material_code)).slice(0,30).map(m=><button type="button" key={m.id} onClick={()=>{update({verified_material_id:m.id,material_code:m.code});setMaterialOpen(false);}}><b>{m.code}</b><span>{m.supplier}{m.is_white_face?" · 白色":""}</span></button>)}<button type="button" className="goods-picker-close" onClick={()=>setMaterialOpen(false)}>收起</button></div>}
      {facts.verified_material_id&&<small>{options.materials.find(m=>m.id===facts.verified_material_id)?.supplier} {canPrice&&<>· {quote||"正在读取报价…"}</>}</small>}
    </div>
    <details><summary>确认可用产品（已选 {facts.product_ids.length} 款）</summary><p>模切/印刷需确认用途；压线核对三段尺寸；净片核对尺寸与纸质。</p><input placeholder="客户、存货编码、产品名称" value={productSearch} onChange={e=>setProductSearch(e.target.value)}/><div className="goods-checks">{products.slice(0,80).map(p=><label key={p.id}><input type="checkbox" checked={facts.product_ids.includes(p.id)} onChange={()=>update({product_ids:toggle(facts.product_ids,p.id)})}/><span>{options.customers.find(c=>c.id===p.customer_id)?.name} · {p.code}<small>{p.name}</small></span></label>)}</div><small>符合 {products.length} 款，显示前80款；输入客户或编码可缩小范围。</small>{facts.product_ids.length>0&&<div className="goods-checks">{facts.product_ids.map(id=><label key={id}><input type="checkbox" checked onChange={()=>update({product_ids:facts.product_ids.filter(v=>v!==id)})}/>{options.products.find(p=>p.id===id)?.code||id} · 已选</label>)}</div>}</details>
    <label>说明<textarea maxLength={1000} value={facts.note} onChange={e=>update({note:e.target.value})}/></label>
  </div>;
}

export function WarehouseGoods({lotId,locationId,layoutVersion,raw=false,canSave,onSaved,onBusyChange}:{lotId?:number;locationId?:number;layoutVersion?:number;raw?:boolean;canSave:boolean;onSaved:()=>Promise<unknown>;onBusyChange?:(busy:boolean)=>void}) {
  const [facts,setFacts]=useState<Facts>(initialFacts(raw)),[options,setOptions]=useState<Options|null>(null),[version,setVersion]=useState(0),[editable,setEditable]=useState(true);
  const [finished,setFinished]=useState(false),[finishedSearch,setFinishedSearch]=useState("");
  const [message,setMessage]=useState(""),[busy,setBusy]=useState(false),[physical,setPhysical]=useState("");
  const [name,setName]=useState(""),[length,setLength]=useState(""),[width,setWidth]=useState(""),[quantity,setQuantity]=useState("");
  const [layer,setLayer]=useState(3),[flute,setFlute]=useState("B"),[stockDate,setStockDate]=useState(new Date().toLocaleDateString("en-CA",{timeZone:"Asia/Shanghai"}));
  const [supplierId,setSupplierId]=useState(""),[sheetCost,setSheetCost]=useState("");
  const [pieces,setPieces]=useState("1"),[yieldPerSheet,setYield]=useState("1"),[component,setComponent]=useState("whole"),[source,setSource]=useState("existing_stocktake");
  const [creaseSearch,setCreaseSearch]=useState(""),[creaseLeft,setCreaseLeft]=useState(""),[creaseMiddle,setCreaseMiddle]=useState(""),[creaseRight,setCreaseRight]=useState("");
  const [correctionReason,setCorrectionReason]=useState(""),[correctionQuantity,setCorrectionQuantity]=useState(""),[syncProduct,setSyncProduct]=useState(""),[impact,setImpact]=useState<{fingerprint:string;old_product_name:string;new_product_name:string;historical_order_lines:number}|null>(null);
  const pending=useRef<{signature:string;key:string}|null>(null),saving=useRef(false);
  useEffect(()=>{let alive=true;void Promise.all([api("/options"),lotId?api(`/${lotId}`):Promise.resolve(null)]).then(([o,d])=>{if(!alive)return;setOptions(o);if(d){setFacts({...d.facts,display_name:d.facts.display_name||d.physical.name||""});setFlute(d.physical.flute);setVersion(d.version);setFinished(d.inventory_type==="finished");setEditable(d.editable);setPhysical(`${d.physical.name||"片料"} · ${d.physical.length}×${d.physical.width} · ${d.physical.flute==="NONE"?"无楞":`${d.physical.flute}楞`} · 原入库材质 ${d.physical.material}${d.physical.settlement_unit_price ? ` · 入库结算单价 ${d.physical.settlement_unit_price}元/张` : ""}`);}}).catch(e=>{if(alive)setMessage(e.message);});return()=>{alive=false};},[lotId]);
  useEffect(()=>setImpact(null),[facts,correctionReason,correctionQuantity,syncProduct]);
  const correctionPayload=()=>({facts,expected_version:version,correction_reason:correctionReason,correction_quantity:correctionQuantity?Number(correctionQuantity):null,sync_product_id:syncProduct?Number(syncProduct):null,impact_fingerprint:impact?.fingerprint||null});
  const preview=async()=>{try{const result=await api(`/${lotId}/correction-preview`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...correctionPayload(),idempotency_key:"correction-preview"})});setImpact(result);setMessage("");}catch(e){setMessage(e instanceof Error?e.message:"预览失败");}};
  const save=async()=>{
    if(saving.current||!canSave||!options)return;
    if(lotId&&!correctionReason.trim()){setMessage("请填写资料修正原因");return;}
    if(lotId&&syncProduct&&!impact){setMessage("请先预览同步常用箱的影响");return;}
    const payload=lotId?correctionPayload():{facts,location_id:locationId,expected_layout_version:layoutVersion,
      quantity:Number(quantity),stock_date:stockDate,internal_name:name,board_length_mm:Number(length),board_width_mm:Number(width),layer_count:layer,flute_type:flute,
      ...(layer===1?{supplier_id:Number(supplierId),sheet_unit_cost:sheetCost}:{}),
      pieces_per_box:Number(pieces),stock_yield_per_sheet:Number(yieldPerSheet),component_type:component,source_kind:source,
      crease_type:facts.processing==="creased"?"压线":null,crease_left_mm:facts.processing==="creased"&&creaseLeft?Number(creaseLeft):null,crease_middle_mm:facts.processing==="creased"&&creaseMiddle?Number(creaseMiddle):null,crease_right_mm:facts.processing==="creased"&&creaseRight?Number(creaseRight):null};
    const signature=JSON.stringify(payload);
    if(pending.current?.signature!==signature)pending.current={signature,key:`goods-${Date.now()}-${Math.random().toString(36).slice(2)}`};
    saving.current=true;setBusy(true);onBusyChange?.(true);setMessage("");
    try{const result=await api(lotId?`/${lotId}`:"/sheet-entry",{method:lotId?"PUT":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...payload,idempotency_key:pending.current.key})});
      setVersion(result.version);setFacts(result.facts);setMessage(lotId?"适用资料已保存，库存数量未改变":"货物已入库");pending.current=null;
      if(lotId&&result.lot_id!==lotId){setEditable(false);setMessage("已在原位置拆分新批次并修正资料；总数量不变，请重新选择新批次继续维护。");}
      if(!lotId){setQuantity("");setName("");}
      try{await onSaved();}catch{setMessage("已保存；地图刷新失败，请刷新核对后再操作");setEditable(false);}
    }catch(e){setMessage(e instanceof Error?e.message:"保存失败");}finally{saving.current=false;setBusy(false);onBusyChange?.(false);}
  };
  return <section className="warehouse-goods"><h4>{lotId?"修正库存资料":raw?"新增原材料（张）":"新增半成品（张）"}</h4>
    {physical&&<p>{physical}</p>}
    {lotId&&<p>原始来源客户：{facts.source_customer_name||options?.customers.find(c=>c.id===facts.source_customer_id)?.name||"未登记"}（保留原记录）</p>}
    {options&&<fieldset disabled={busy||!canSave||!editable}>
      {lotId&&<div className="goods-fields"><label>当前库存名称<input maxLength={200} value={facts.display_name||""} onChange={e=>setFacts({...facts,display_name:e.target.value})}/></label><label>修正原因<input maxLength={500} value={correctionReason} onChange={e=>setCorrectionReason(e.target.value)}/></label><label>仅修正部分数量（留空为全部可用数量）<input type="number" min={1} step={1} value={correctionQuantity} onChange={e=>setCorrectionQuantity(e.target.value)}/></label><p>原来源客户和历史记录保留；部分修正会在原位置拆分批次，不调整库存总量。</p><label>同步更新常用箱<select value={syncProduct} onChange={e=>setSyncProduct(e.target.value)}><option value="">不修改常用箱（默认）</option>{options.products.filter(p=>facts.product_ids.includes(p.id)).map(p=><option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></label>{syncProduct&&<><button type="button" onClick={()=>void preview()}>预览常用箱同步影响</button>{impact&&<p>{impact.old_product_name} → {impact.new_product_name}；只改以后使用的主档名称，{impact.historical_order_lines}条历史订单快照保持不变。</p>}</>}</div>}
      {!lotId&&<div className="goods-fields"><label>货物名称<input value={name} maxLength={200} onChange={e=>setName(e.target.value)} placeholder={raw?"例如 B楞纸板":"例如 通用衬板 / 模切待印刷片"}/></label>
        <div className="goods-pair"><label>纸板长（mm）<input type="number" inputMode="numeric" step={1} min={1} value={length} onChange={e=>setLength(e.target.value)}/></label><label>纸板宽（mm）<input type="number" inputMode="numeric" step={1} min={1} value={width} onChange={e=>setWidth(e.target.value)}/></label></div>
        <div className="goods-pair"><label>纸张类型<select value={layer} onChange={e=>{const n=Number(e.target.value);setLayer(n);setFlute(n===1?"NONE":n===3?"B":n===5?"AB":"AAA");setFacts({...facts,verified_material_id:null,face_paper:n===1?"white":"kraft"});}}>{[1,3,5,7].map(n=><option key={n} value={n}>{n===1?"单层原纸 / 白卡":`${n}层瓦楞纸板`}</option>)}</select></label><label>楞型<select value={flute} disabled={layer===1} onChange={e=>setFlute(e.target.value)}>{(layer===1?["NONE"]:layer===3?["A","B","E"]:layer===5?["AB","BE"]:["AAA","ABC"]).map(v=><option key={v} value={v}>{v==="NONE"?"无楞":v}</option>)}</select></label></div>
        {layer===1&&<><div className="goods-pair"><label>原纸供应商<select value={supplierId} onChange={e=>setSupplierId(e.target.value)}><option value="">请选择供应商</option>{options.suppliers.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label><label>含税到库单价（元/张）<input type="number" inputMode="decimal" min="0.0001" step="0.0001" value={sheetCost} onChange={e=>setSheetCost(e.target.value)}/></label></div><small>按张登记库存并固定本批成本；不重复生成采购应付。</small></>}
        <div className="goods-pair"><label>实际数量（张）<input type="number" inputMode="numeric" step={1} min={1} value={quantity} onChange={e=>setQuantity(e.target.value)}/></label><label>库存日期<input type="date" value={stockDate} onChange={e=>setStockDate(e.target.value)}/></label></div>
        <details><summary>片数换算与来源</summary><label>组件<select value={component} onChange={e=>setComponent(e.target.value)}><option value="whole">整片</option><option value="cover">盖片</option><option value="base">底片</option></select></label><label>每箱所需片数<input type="number" min={1} value={pieces} onChange={e=>setPieces(e.target.value)}/></label><label>每库存张产出片数<input type="number" min={1} value={yieldPerSheet} onChange={e=>setYield(e.target.value)}/></label><label>来源<select value={source} onChange={e=>setSource(e.target.value)}><option value="existing_stocktake">本厂现场盘点发现</option><option value="partner_transfer">合作纸箱厂搬入</option></select></label></details>
      </div>}
      {finished?<div className="goods-fields">
        <label>库存用途<select value={facts.scope} onChange={e=>setFacts({...facts,scope:e.target.value as Facts["scope"],customer_ids:e.target.value==="general"?[]:facts.customer_ids})}><option value="general">通用</option><option value="customers">指定客户</option></select></label>
        {facts.scope==="customers"&&<label>适用客户<select value={facts.customer_ids[0]||""} onChange={e=>setFacts({...facts,customer_ids:[Number(e.target.value)],product_ids:[]})}><option value="">请选择</option>{options.customers.map(c=><option key={c.id} value={c.id}>{c.name}</option>)}</select></label>}
        <label>对应实际产品<input placeholder="按编码或名称筛选" value={finishedSearch} onChange={e=>setFinishedSearch(e.target.value)}/><select value={facts.product_ids[0]||""} onChange={e=>setFacts({...facts,product_ids:[Number(e.target.value)]})}><option value="">请选择</option>{options.products.filter(p=>(facts.scope==="general"||facts.customer_ids.includes(p.customer_id))&&(facts.product_ids.includes(p.id)||`${p.code} ${p.name}`.toLowerCase().includes(finishedSearch.toLowerCase()))).filter((p,i)=>i<80||facts.product_ids.includes(p.id)).map(p=><option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></label>
        <small>更换客户产品须有完全相同的冻结实物规格和工艺；不会更改成品尺寸、数量或成本。</small>
        <label>盘点备注<textarea maxLength={1000} value={facts.note} onChange={e=>setFacts({...facts,note:e.target.value})}/></label>
      </div>:<FactsEditor facts={facts} setFacts={setFacts} options={options} raw={lotId?facts.processing==="raw":raw} flute={flute} canPrice={canSave&&!lotId} length={length} width={width} quantity={quantity}/>}
      {!lotId&&facts.processing==="creased"&&<div className="goods-fields"><label>取用常用箱压线<input placeholder="客户、编码或名称" value={creaseSearch} onChange={e=>setCreaseSearch(e.target.value)}/><select value={facts.crease_product_id||""} onChange={e=>{const id=Number(e.target.value)||null;const p=options.products.find(p=>p.id===id);setFacts({...facts,crease_product_id:id});if(p){setCreaseLeft(String(p.crease_values[0]));setCreaseMiddle(String(p.crease_values[1]));setCreaseRight(String(p.crease_values[2]));}}}><option value="">选择模板（或下方实测）</option>{options.products.filter(p=>p.is_a1&&p.flute_type===flute&&p.crease_values.every(v=>v&&v>0)&&(facts.scope==="general"||facts.customer_ids.includes(p.customer_id))&&`${p.code} ${p.name} ${options.customers.find(c=>c.id===p.customer_id)?.name}`.toLowerCase().includes(creaseSearch.toLowerCase())).slice(0,80).map(p=><option key={p.id} value={p.id}>{options.customers.find(c=>c.id===p.customer_id)?.name} · {p.code} · {p.crease_values.join("+")}</option>)}</select></label><div className="goods-pair">{[["左翼",creaseLeft,setCreaseLeft],["中段高度",creaseMiddle,setCreaseMiddle],["右翼",creaseRight,setCreaseRight]].map(([label,value,setter])=><label key={String(label)}>{String(label)}（mm）<input type="number" inputMode="numeric" min={1} step={1} value={String(value)} onChange={e=>(setter as (s:string)=>void)(e.target.value)}/></label>)}</div><small>合计 {Number(creaseLeft)+Number(creaseMiddle)+Number(creaseRight)}mm / 板宽 {width||"—"}mm · {facts.dimension_source==="label"?"应相等":"测量差≤10mm"}</small></div>}
      {canSave&&<button type="button" className="twin-primary-action" onClick={()=>void save()}>{busy?"保存中…":lotId?"保存资料修正":"确认增加库存"}</button>}
    </fieldset>}
    {!options&&!message&&<p>正在读取可选资料…</p>}{!editable&&<p>批次已预占或不可用，请解除相关预占后再维护用途。</p>}{message&&<p role="status">{message}</p>}
  </section>;
}
