(function(root){
  "use strict";
  const PREFIX="tianming:stocktake-review:v1:";
  const DEFAULT_REJECTION="驳回库存盘点单（系统记录）";
  const positive=value=>Number.isSafeInteger(value)&&value>0;
  const integer=value=>Number.isSafeInteger(value)&&value>=0;
  const text=value=>typeof value==="string"&&value.trim().length>0;
  const time=value=>text(value)&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value)&&Number.isFinite(Date.parse(value));
  const copy=value=>JSON.parse(JSON.stringify(value));
  const sorted=value=>Array.isArray(value)?value.map(sorted):value&&typeof value==="object"?Object.fromEntries(Object.keys(value).sort().map(key=>[key,sorted(value[key])])):value;
  const same=(left,right)=>JSON.stringify(sorted(left))===JSON.stringify(sorted(right));
  function reason(action,body){const value=typeof body.reason==="string"?body.reason.trim():"";return value||(action==="reject"?DEFAULT_REJECTION:null)}
  function validRecord(row){return row&&row.version===1&&positive(row.ownerId)&&positive(row.orderId)&&["approve","reject"].includes(row.action)&&row.body&&text(row.body.idempotency_key)&&row.body.expected_actor_id===row.ownerId&&["unknown","confirmed"].includes(row.state)}
  function validReceipt(order,row){
    if(!validRecord(row)||!order||order.id!==row.orderId||!text(order.order_number)||!Array.isArray(order.items)||!Array.isArray(order.reviews))return false;
    if(!positive(order.version)||!positive(order.location_id)||order.lot_count!==order.items.length||!integer(order.snapshot_on_hand)||!integer(order.counted_quantity)||!Number.isSafeInteger(order.difference_quantity))return false;
    const target=row.action==="approve"?"approved":"rejected",review=order.matched_review;
    if(order.status!==target||order.request_action!==row.action||order.request_idempotency_key!==row.body.idempotency_key||order.request_reason!==reason(row.action,row.body)||order.current_actor_id!==row.ownerId)return false;
    if(!review||!positive(review.id)||!positive(review.sequence)||review.action!==row.action||review.from_status!=="submitted"||review.to_status!==target||review.idempotency_key!==row.body.idempotency_key||review.reason!==reason(row.action,row.body)||review.reviewed_by!==row.ownerId||order.reviewed_by!==review.reviewed_by||!time(review.reviewed_at)||order.reviewed_at!==review.reviewed_at||order.review_note!==review.reason)return false;
    const actual=order.reviews.find(item=>item.id===review.id);
    if(!actual||!same({...actual,idempotency_key:review.idempotency_key},review)||!review.details||typeof review.details!=="object")return false;
    const lots=new Set();
    for(const item of order.items){
      if(!positive(item.id)||!positive(item.inventory_lot_id)||lots.has(item.inventory_lot_id)||!positive(item.lot_version_snapshot)||!integer(item.quantity_available_snapshot)||!integer(item.quantity_reserved_snapshot)||!integer(item.counted_quantity)||!Number.isSafeInteger(item.difference_quantity)||item.quantity_on_hand_snapshot!==item.quantity_available_snapshot+item.quantity_reserved_snapshot||item.difference_quantity!==item.counted_quantity-item.quantity_on_hand_snapshot)return false;
      lots.add(item.inventory_lot_id);
    }
    if(order.snapshot_on_hand!==order.items.reduce((sum,item)=>sum+item.quantity_on_hand_snapshot,0)||order.counted_quantity!==order.items.reduce((sum,item)=>sum+item.counted_quantity,0)||order.difference_quantity!==order.items.reduce((sum,item)=>sum+item.difference_quantity,0))return false;
    if(row.action==="approve"){
      const adjustments=review.details.adjustments;
      if(!Array.isArray(adjustments)||adjustments.length!==order.items.length)return false;
      const seen=new Set();
      for(const change of adjustments){
        const item=order.items.find(item=>item.inventory_lot_id===change.inventory_lot_id);
        if(!item||seen.has(change.inventory_lot_id)||!["before_available","reserved","counted_quantity","after_available"].every(field=>integer(change[field]))||!Number.isSafeInteger(change.delta)||change.counted_quantity!==item.counted_quantity||change.after_available+change.reserved!==item.counted_quantity||change.delta!==change.after_available-change.before_available||change.movement_id!==item.adjustment_movement_id||(change.delta===0?change.movement_id!==null:!positive(change.movement_id)))return false;
        seen.add(change.inventory_lot_id);
      }
    }else if(review.details.order_id!==row.orderId||review.details.idempotency_key!==row.body.idempotency_key||review.details.reason!==review.reason||review.details.order_number!==order.order_number)return false;
    return true;
  }
  function create({storage,actor,request,newKey}){
    const busy=new Set();
    const confirmedLocal=new Map();
    const key=row=>`${PREFIX}${row.ownerId}:${row.orderId}:${encodeURIComponent(row.body.idempotency_key)}`;
    const save=row=>{storage.setItem(key(row),JSON.stringify(row));if(storage.getItem(key(row))!==JSON.stringify(row))throw new Error("审核请求未能可靠保存，请勿提交")};
    function entries(orderId=null){
      const id=actor();if(!positive(id))throw new Error("账号尚未加载，请重新登录后核对");
      const rows=[];
      for(let index=0;index<storage.length;index++){
        const entryKey=storage.key(index);if(!entryKey||!entryKey.startsWith(`${PREFIX}${id}:`))continue;
        if(orderId!==null&&!entryKey.startsWith(`${PREFIX}${id}:${orderId}:`))continue;
        let row;try{row=JSON.parse(storage.getItem(entryKey))}catch{throw new Error("待核对审核记录损坏，请保留并联系管理员")}
        if(!validRecord(row)||row.ownerId!==id||entryKey!==key(row))throw new Error("待核对审核记录不完整，请保留并联系管理员");
        rows.push(copy(confirmedLocal.get(entryKey)||row));
      }
      return rows;
    }
    const forOrder=id=>entries(id);
    function lookup(row){
      try{
        if(!validRecord(row)||actor()!==row.ownerId)return {status:"other_actor",row};
        const raw=storage.getItem(key(row));if(raw===null)return {status:"other_actor",row};
        const saved=JSON.parse(raw);
        if(!validRecord(saved)||key(saved)!==key(row)||saved.action!==row.action||!same(saved.body,row.body))return {status:"unknown",row,message:"原审核请求与当前记录不一致，请保留并核对；未发送"};
        return {status:"active",row:copy(confirmedLocal.get(key(row))||saved)};
      }catch{return {status:"unknown",row,message:"待核对记录读取失败，原请求保持；请重试读取"}}
    }
    function finish(row,receipt){
      const stored=lookup(row);if(stored.status!=="active")return stored;row=stored.row;
      if(!validReceipt(receipt,row))return {status:"unknown",row,message:"审核回执不完整，原请求已保留，请核对结果"};
      const confirmed={...row,state:"confirmed",receipt:copy(receipt)};
      confirmedLocal.set(key(row),confirmed);
      try{save(confirmed)}catch{return {status:"confirmed",row:confirmed,receipt,message:"审核已完成，但完成记录保存失败；请继续核对原请求"}}
      try{storage.removeItem(key(row));if(storage.getItem(key(row))!==null)throw new Error("未清理")}catch{return {status:"confirmed",row:confirmed,receipt,message:"审核已完成，待核对记录清理失败；请重试清理"}}
      confirmedLocal.delete(key(row));
      return {status:"completed",row:confirmed,receipt};
    }
    async function operate(row,mode){
      const stored=lookup(row);if(stored.status!=="active")return stored;row=stored.row;
      if(busy.has(key(row)))return {status:"busy",row};
      const confirmed=confirmedLocal.get(key(row))||(row.state==="confirmed"?row:null);
      if(confirmed)return finish(confirmed,confirmed.receipt);
      busy.add(key(row));
      try{
        const data=await request(mode==="resolve"?`/api/warehouse/stocktakes/${row.orderId}/review-result`:`/api/warehouse/stocktakes/${row.orderId}/${row.action}`,{method:"POST",body:JSON.stringify(mode==="resolve"?{action:row.action,body:row.body}:row.body)});
        const latest=lookup(row);if(latest.status!=="active")return latest;
        if(mode==="resolve"){
          if(!data||data.current_actor_id!==row.ownerId||data.request_action!==row.action||data.request_idempotency_key!==row.body.idempotency_key||data.request_reason!==reason(row.action,row.body)||!time(data.observed_at))return {status:"unknown",row,message:"核对回执不完整，原请求已保留"};
          if(data.status==="not_found"&&data.order===null&&data.matched_review===null)return {status:"not_found",row,message:"当前未查到这笔审核记录；原请求仍保留，可明确选择按原请求继续"};
          if(data.status!=="found"||!data.order||!same(data.matched_review,data.order.matched_review))return {status:"unknown",row,message:"核对结果不完整，原请求已保留"};
          return finish(row,data.order);
        }
        return finish(row,data);
      }catch(error){const latest=lookup(row);return latest.status!=="active"?latest:{status:"unknown",row,message:`审核结果待核对：${error.message||"网络异常"}；原请求已保留`}}
      finally{busy.delete(key(row))}
    }
    function prepare(orderId,action,body={},display={}){
      if(!positive(orderId)||!["approve","reject"].includes(action))throw new Error("审核对象无效");
      const ownerId=actor();if(!positive(ownerId))throw new Error("账号尚未加载");
      if(forOrder(orderId).length)throw new Error("该盘点单有待核对审核，请先核对原请求");
      const row={version:1,ownerId,orderId,action,body:{...copy(body),idempotency_key:newKey(),expected_actor_id:ownerId},display:{orderNumber:typeof display.orderNumber==="string"?display.orderNumber:"",locationName:typeof display.locationName==="string"?display.locationName:""},state:"unknown",createdAt:new Date().toISOString()};
      save(row);return copy(row);
    }
    return {entries,forOrder,prepare,send:row=>operate(row,"write"),resolve:row=>operate(row,"resolve"),continue:row=>operate(row,"write"),busy:row=>busy.has(key(row))};
  }
  const exported={create,validReceipt,reason};
  if(typeof module!=="undefined"&&module.exports)module.exports=exported;
  root.StocktakeReviewRecovery=exported;
})(typeof globalThis!=="undefined"?globalThis:this);
