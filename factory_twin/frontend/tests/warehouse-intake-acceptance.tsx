import React, {useMemo, useState} from "react";
import {createRoot} from "react-dom/client";
import {EditorCanvas} from "../src/EditorCanvas";
import {WarehouseRackElevation} from "../src/WarehouseTwinApp";
import {warehouseIntakePaints, rackIntakePaint} from "../src/warehouseIntakeColors.mjs";
import type {SelectedEntity} from "../src/types";
import "../src/styles.css";
import "../src/warehouseTwin.css";
const noop = () => {};
const layers = {structures:false,equipment:false,racks:true,pallets:true,zones:false,aisles:false,noGo:false,customStructures:false,labels:false,production:false};
const ages = [0,31,91,181,365,null,null];
const rack:any = {id:"fixture-rack",layout_id:"fixture",name:"A1",rack_code:"internal-fixture",x_mm:10000,y_mm:6000,z_mm:0,width_mm:11000,depth_mm:800,height_mm:1800,levels:1,bays:7,cargo_rows:7,level_cell_counts:[7],level_heights_mm:[],rotation_deg:0,color:"#334155",source:"manual",status:"confirmed",is_locked:false,version:1};
const locations:any[] = ages.map((age,index) => ({location_id:index+1,location_code:`fixture-${index+1}`,location_name:`A1第1层${index+1}格`,floor_code:"TEST",map_rack_id:rack.id,level_no:1,slot_no:index+1,storage_type:"rack",address_kind:"rack_slot",is_active:true,position_status:"mapped",map_position:{version:1},loose_items:index===6 ? [] : [{lot_id:index+1,product_id:index+1,inventory_code:`匿名${index+1}`,product_name:"样本产品",customer_name:"测试",quantity:10*(index+1),available_quantity:10*(index+1),unit:"pieces",intake_age_days:age,intake_identity_key:`sample-${index+1}`}],pallet:null}));
function App() {
  const [selected,setSelected] = useState<SelectedEntity>({kind:"pallet",id:"erp-location-1"});
  const [focused,setFocused] = useState(false);
  const selectedItems = focused ? locations[0].loose_items : null;
  const paints = useMemo(() => warehouseIntakePaints(locations,selectedItems),[focused]);
  const rackPaint = rackIntakePaint(rack.id,locations,paints);
  const layout:any = {id:"fixture",name:"隔离地图",floor_code:"TEST",bounds_mm:{min_x:0,min_y:0,max_x:20000,max_y:10000},structures:[],placements:[],features:[],violations:[],warnings:[],rule_defaults:{},racks:[{...rack,intake_color:rackPaint.color,intake_unknown:rackPaint.unknown}],
    pallets:ages.map((_,index) => ({id:`erp-location-${index+1}`,layout_id:"fixture",pallet_code:`fixture-${index}`,name:`样本${index+1}`,zone_id:"fixture",zone_code:"fixture",x_mm:3000+index*2200,y_mm:3000,z_mm:0,width_mm:1200,depth_mm:1000,height_mm:150,rotation_deg:0,color:paints[index+1].color,intake_color:paints[index+1].color,intake_unknown:paints[index+1].unknown,intake_dimmed:paints[index+1].dimmed,visual_status:index===6 ? "empty" : "waiting",visual_kind:"physical_pallet",is_logical_anchor:false,is_simulated:false,version:1,snapped:true}))};
  return <main style={{height:"100vh",display:"flex",flexDirection:"column",padding:8,gap:8}}><div style={{display:"flex",gap:12,alignItems:"center"}}><b>隔离匿名样本 · 无 API / 数据库写入</b><button onClick={() => setFocused(value=>!value)}>{focused ? "取消产品选择" : "只看匿名1"}</button><button onClick={()=>setSelected(null)}>取消选中</button><span>左至右：0 / 31 / 91 / 181 / 365 / 日期待核 / 空位</span></div>
    <div style={{height:"52vh",display:"flex",minHeight:380}}><EditorCanvas layout={layout} assets={[]} viewMode="2d" cameraPreset="fit" viewResetToken={0} selected={selected} layers={layers} visualTheme="warehouse" readOnly showIntakeLegend highlightedPalletIds={focused ? ["erp-location-1"] : ["erp-location-3"]} productQuantityLabels={focused ? {"erp-location-1":"实存 10片"} : {}} allowPalletSelection palletSnapEnabled={false} palletSnapThresholdMm={0} drawMode={null} drawPoints={[]} measureMode={false} measurePoints={[]} onSelect={setSelected} onMoveEquipment={noop} onMoveRack={noop} onMovePallet={noop} onMoveFeature={noop} onDropAsset={noop} onDropRack={noop} onDropPallet={noop} onDrawPoint={noop} onMeasurePoint={noop}/></div>
    <div style={{flex:1,minHeight:0,overflow:"auto"}}><WarehouseRackElevation rack={rack} locations={locations} unboundLocationCount={0} canChooseProducts={false} intakePaints={paints} selectedProductLotIds={selectedItems?.map((item:any)=>item.lot_id)??null} productQuantityLabels={focused ? {"erp-location-1":"实存 10片"} : {}} highlightedLotIds={focused ? [1] : [3]} selectedLocationId={selected?.kind==="pallet" ? Number(selected.id.replace("erp-location-","")) : undefined} rackIndex={0} rackCount={1} onPrevious={noop} onNext={noop} onSelectLocation={id=>setSelected({kind:"pallet",id:`erp-location-${id}`})} onChooseEmptyLocation={noop} onSelectLot={(id)=>setSelected({kind:"pallet",id:`erp-location-${id}`})} onRefocus={noop} onClose={noop}/></div>
  </main>;
}
createRoot(document.getElementById("root")!).render(<App/>);
