import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MoldRackElevation, WarehouseRackElevation } from '../src/WarehouseTwinApp';
import '../src/warehouseTwin.css';

// Anonymous fixtures only. No login, database, map publication, or inventory writes.
const rack: any = {id:'fixture-rack', rack_code:'样本架', name:'样本架', levels:3, bays:3,
  level_cell_counts:[3,3,3], width_mm:2600, depth_mm:1200, height_mm:2600};
const molds = Array.from({length:80}, (_, i) => ({id:i+1, mold_code:`fixture-${i}`, mold_name:`中性内盒 ${400+i}×300×200`,
  rack_location:'样本架·第2层第1格', product_count:2,
  location_guide:{kind:'storage_grid', level:2, grid:1, prompt:'样本架·第2层第1格'},
  products:[{id:i+1, product_code:`TEST-${i}`, product_name:'中性内盒', customer_name:'样本客户'},
    {id:i+101, product_code:`OTHER-${i}`, product_name:'共用模具产品', customer_name:'另一客户'}]}));
const locations: any[] = Array.from({length:9}, (_, i) => ({location_id:i+1, location_code:`fixture-cell-${i}`, location_name:`样本架·第${Math.floor(i/3)+1}层第${i%3+1}格`,
  map_rack_id:rack.id, level_no:Math.floor(i/3)+1, slot_no:i%3+1, loose_items:i<2 ? [1,2].map(j => ({lot_id:i*10+j,
    product_id:i===0?1:j, customer_id:1, product_name:i===0?'中性纸箱（单品多批次）':`零散余量纸箱 ${j}`,
    specification:'400×300×200 mm', inventory_code:'TEST-ONLY', customer_short_name:'样本客户',
    available_quantity:j*20, reserved_quantity:10, damaged_quantity:0, unit:'boxes', stock_date:'2026-09-08', stock_date_accuracy:'exact'})) : []}));
function App() {
  const [mode,setMode]=useState('mold');
  const [narrow,setNarrow]=useState(false);
  return <main><header style={{padding:12,background:'#fff'}}><strong>隔离视觉验收（匿名样本，不写正式系统）</strong>{' '}
    <button onClick={()=>setMode('mold')}>模具80块</button> <button onClick={()=>setMode('carton')}>纸箱单品与混放</button>{' '}
    <button onClick={()=>setNarrow(!narrow)}>{narrow?'桌面宽度':'窄容器390px'}</button></header>
    <div style={{width:narrow?390:'100%',maxWidth:'100%',margin:'12px auto'}}>
    {mode==='mold'?<MoldRackElevation rack={rack} response={{rack:{...rack,blocked_levels:[]},items:molds,total:80} as any}
      loading={false} error="" canMoveMolds={false} rackIndex={0} rackCount={1} onPrevious={()=>{}} onNext={()=>{}} onClose={()=>{}} onMoldMoved={()=>{}} />:
      <WarehouseRackElevation rack={rack} locations={locations} unboundLocationCount={0} canChooseProducts={false}
        rackIndex={0} rackCount={1} onPrevious={()=>{}} onNext={()=>{}} onClose={()=>{}} onChooseEmptyLocation={()=>{}} />}
    </div></main>;
}
createRoot(document.getElementById('root')!).render(<App/>);
