import {useEffect,useRef,useState} from 'react';
import './warehouseGoods.css';

type Material={id:number;code:string;supplier:string};
type Preview={fingerprint:string;unit_cost:string;evidence:{report_length_mm:number;report_width_mm:number;pieces_per_box:number}};
type Props={locationId:number;layoutVersion:number;canSave:boolean;onSaved:()=>Promise<unknown>;onBusyChange?:(busy:boolean)=>void};
const priceUrl='/?page=products&subpage=materials';
async function request(path:string,body?:unknown){
  const r=await fetch('/api/warehouse/goods'+path,{credentials:'same-origin',cache:'no-store',
    ...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
  const data=await r.json();
  if(!r.ok){const e=new Error(typeof data.detail==='string'?data.detail:data.detail?.message||'请检查必填资料及正整数尺寸') as Error&{status:number};e.status=r.status;throw e;}
  return data;
}

export function UnassignedFinishedEntry({locationId,layoutVersion,canSave,onSaved,onBusyChange}:Props){
  const [materials,setMaterials]=useState<Material[]>([]),[query,setQuery]=useState(''),[storageKey,setStorageKey]=useState('');
  const [form,setForm]=useState({name:'现场盘点纸箱',length_mm:'',width_mm:'',height_mm:'',box_style:'A1',
    report_length_mm:'',report_width_mm:'',splice_mode:'single',flap_mm:'30',material_id:'',quantity:'',
    material_confidence:'estimated',dimension_source:'tape',stock_date:new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'}),note:''});
  const [preview,setPreview]=useState<Preview|null>(null),[busy,setBusy]=useState(false),[message,setMessage]=useState(''),[saved,setSaved]=useState(false);
  const [uncertain,setUncertain]=useState(false), attempt=useRef<Record<string,unknown>|null>(null),lock=useRef(false);
  const load=async()=>{const o=await request('/options');setMaterials(o.materials);return o;};
  useEffect(()=>{let alive=true;void request('/options').then(o=>{if(!alive)return;setMaterials(o.materials);
    const key=`unassigned-entry:${o.actor_id}:${locationId}`;setStorageKey(key);
    try{const old=JSON.parse(sessionStorage.getItem(key)||'null');if(old?.form)setForm(old.form);
      if(old?.attempt){attempt.current=old.attempt;setUncertain(true);setMessage('上次保存结果未确认，请重试原请求，避免重复入库。');}}
    catch{/* Invalid local draft is not a stock fact. */}
  }).catch(e=>{if(alive)setMessage(e.message);});return()=>{alive=false;};},[locationId]);
  useEffect(()=>{if(storageKey&&!saved)try{sessionStorage.setItem(storageKey,JSON.stringify({form,attempt:attempt.current}));}catch{/* in-page retry survives */}},[form,storageKey,saved,uncertain]);
  const change=(key:keyof typeof form,value:string)=>{setForm({...form,[key]:value});setPreview(null);};
  const payload=()=>({...form,location_id:locationId,expected_layout_version:layoutVersion,
    ...Object.fromEntries(['length_mm','width_mm','height_mm','flap_mm','material_id','quantity'].map(k=>[k,Number(form[k as keyof typeof form])])),
    report_length_mm:form.box_style==='A1'?null:Number(form.report_length_mm)||null,
    report_width_mm:form.box_style==='A1'?null:Number(form.report_width_mm)||null});
  const run=async(save:boolean)=>{
    if(lock.current||!canSave||saved)return;lock.current=true;setBusy(true);onBusyChange?.(true);
    try{
      if(!save){const result=await request('/finished-unassigned/preview',{...payload(),idempotency_key:'preview-unassigned'});setPreview(result);setMessage('已计算，请核对实物数量及参考成本后入库。');return;}
      if(!attempt.current){if(!preview)throw new Error('请先计算参考成本');attempt.current={...payload(),fingerprint:preview.fingerprint,idempotency_key:window.crypto?.randomUUID?.()||`unassigned-${Date.now()}-${Math.random().toString(36).slice(2)}`};}
      try{if(storageKey)sessionStorage.setItem(storageKey,JSON.stringify({form,attempt:attempt.current}));}catch{/* keep the original request in memory */}
      await request('/finished-unassigned',attempt.current);setSaved(true);setUncertain(false);attempt.current=null;
      try{if(storageKey)sessionStorage.removeItem(storageKey);}catch{/* already saved; never repeat */}
      setMessage('客户待认领成品已入库，成本依据已冻结。');
      try{await onSaved();}catch{setMessage('已入库；列表刷新失败，请刷新查看，不要再次新增。');}
    }catch(e){const error=e as Error&{status?:number};setMessage(error.message);
      if(save){const unknown=!error.status||error.status>=500;setUncertain(unknown);
        if(!unknown){attempt.current=null;setPreview(null);try{if(storageKey)sessionStorage.setItem(storageKey,JSON.stringify({form}));}catch{/* retain form in memory */}}
        else setMessage('保存结果暂未确认。点击重试同一笔，不要另外新增；系统会防重复。');}}
    finally{lock.current=false;setBusy(false);onBusyChange?.(false);}
  };
  return <section className="warehouse-goods"><h4>客户待认领成品（只）</h4>
    <p>先搜索现有库存，确认是尚未登记的新实物。只在空库位新增；客户未核实前不用于订单发货。估算材质按人工选择记录，不代表采购事实。</p>
    <fieldset disabled={!canSave||busy||uncertain||saved}><div className="goods-fields">
      <label>货物名称<input value={form.name} maxLength={200} onChange={e=>change('name',e.target.value)}/></label>
      <div className="goods-pair">{(['length_mm','width_mm','height_mm'] as const).map((k,i)=><label key={k}>实测{['长','宽','高'][i]}（mm）<input type="number" inputMode="numeric" min={1} value={form[k]} onChange={e=>change(k,e.target.value)}/></label>)}</div>
      <label>箱型<select value={form.box_style} onChange={e=>change('box_style',e.target.value)}><option>A1</option><option>其他</option></select></label>
      <label>拼片<select value={form.splice_mode} onChange={e=>change('splice_mode',e.target.value)}><option value="single">单片</option><option value="double">双片</option></select></label>
      {form.box_style==='A1'?<label>搭舌（mm）<input type="number" min={1} value={form.flap_mm} onChange={e=>change('flap_mm',e.target.value)}/><small>默认30mm，可按实物修改；仅用于本批参考计价。</small></label>:
        <div className="goods-pair">{(['report_length_mm','report_width_mm'] as const).map((k,i)=><label key={k}>实际报料{['长','宽'][i]}（mm）<input type="number" min={1} value={form[k]} onChange={e=>change(k,e.target.value)}/></label>)}</div>}
      <label>尺寸来源<select value={form.dimension_source} onChange={e=>change('dimension_source',e.target.value)}><option value="tape">卷尺实测</option><option value="label">标签标注</option></select></label>
      <label>供应商材质<input placeholder="筛选代码或供应商" value={query} onChange={e=>setQuery(e.target.value)}/><select value={form.material_id} onChange={e=>change('material_id',e.target.value)}><option value="">请选择</option>{materials.filter(m=>m.id===Number(form.material_id)||`${m.code} ${m.supplier}`.toLowerCase().includes(query.toLowerCase())).map(m=><option key={m.id} value={m.id}>{m.supplier} · {m.code}</option>)}</select></label>
      <label>材质依据<select value={form.material_confidence} onChange={e=>change('material_confidence',e.target.value)}><option value="estimated">人工估算材质</option><option value="confirmed">已核实材质</option></select></label>
      <a href={priceUrl} target="_blank" rel="noopener">补充供应商材质 / 报价（保留本页）</a><button type="button" onClick={()=>{setPreview(null);void load().then(()=>setMessage('资料已刷新，请重新计算参考成本')).catch(e=>setMessage(e.message));}}>补完后刷新资料</button>
      <label>数量（只）<input type="number" inputMode="numeric" min={1} value={form.quantity} onChange={e=>change('quantity',e.target.value)}/></label>
      <label>库存日期<input type="date" value={form.stock_date} onChange={e=>change('stock_date',e.target.value)}/></label>
      <label>说明<input value={form.note} maxLength={1000} onChange={e=>change('note',e.target.value)}/></label>
    </div><button type="button" onClick={()=>void run(false)}>计算参考成本</button></fieldset>
    {preview&&<p>报料 {preview.evidence.report_length_mm} × {preview.evidence.report_width_mm} mm × {preview.evidence.pieces_per_box} 片/只；参考材料成本 ¥{preview.unit_cost}/只（含税），不含人工。</p>}
    <button type="button" className="twin-primary-action" disabled={busy||saved||!canSave||(!preview&&!uncertain)} onClick={()=>void run(true)}>{busy?'处理中…':uncertain?'重试原入库请求':'确认增加库存'}</button>
    <p role="status">{message}</p>
  </section>;
}
