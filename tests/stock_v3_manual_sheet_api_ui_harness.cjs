const fs=require('node:fs'),ui=require('../static/ui/manual-sheet-deduction.js');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{
  const targets=ui.orderTargets(input.order_item),target=targets.find(row=>!row.bom_snapshot_id && row.component_type==='whole');
  if(!target)throw Error('Saved internally reported order has no whole-sheet entry');
  let submitted=null;
  global.axios={get:async()=>({data:input.preview}),post:async(path,payload)=>{submitted={path,payload};return {data:{message:'request captured'}};}};
  const state={snapshotId:target.bom_snapshot_id || 0,orderItemId:target.order_item_id,componentType:target.component_type,
    actorId:1,...ui.component.data(),...ui.component.methods,$emit(){}};
  Object.defineProperty(state,'identity',{get(){return ui.component.computed.identity.call(this)}});
  await state.open();
  const row=state.rows.find(row=>row.lot_id===input.lot_id);
  if(!row)throw Error('Actual API stock lot is missing from manual UI');
  row.inputQuantity=String(input.quantity);await state.reserve(row);
  if(!submitted)throw Error('UI did not create an explicit-quantity API request');
  const reported=ui.reportedTargets(input.document_line);
  process.stdout.write(JSON.stringify({submitted,targets,reported}));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
