(function(global){
 'use strict';
 const PREFIX='erp-stock-completion:v1:';
 const positive=v=>Number.isSafeInteger(v)&&v>0;
 const clone=v=>JSON.parse(JSON.stringify(v));
 const stable=v=>Array.isArray(v)?'['+v.map(stable).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+stable(v[k])).join(',')+'}':JSON.stringify(v);
 function createStore(storage,actorId){
  if(!positive(actorId))throw Error('请先核对当前账号');
  const accountPrefix=PREFIX+actorId+':';
  const keyFor=key=>accountPrefix+encodeURIComponent(key);
  function valid(record,key){
   if(!record||record.schema!=='stock-completion-local-v1'||record.actorId!==actorId||typeof record.key!=='string'||!record.key||keyFor(record.key)!==key||!positive(record.receiptId)||record.endpoint!=='/api/production/stock-preparation/'+record.receiptId+'/actions'||!record.body||record.body.action!=='complete'||record.body.operation_key!==record.key||record.body.expected_actor_id!==actorId||!['prepared','unknown','confirmed'].includes(record.state)||typeof record.hadUnknown!=='boolean'||!record.summary||typeof record.summary!=='object')throw Error('加工恢复记录不完整，请保留现场并核对原记录');
   return record;
  }
  function list(){
   const records=[];
   for(let i=0;i<storage.length;i++){const key=storage.key(i);if(typeof key!=='string'||!key.startsWith(accountPrefix))continue;const value=storage.getItem(key);if(value===null)continue;records.push(valid(JSON.parse(value),key))}
   return records.sort((a,b)=>(a.createdAt||'').localeCompare(b.createdAt||'')||a.key.localeCompare(b.key));
  }
  function write(record){
   const candidate=clone(record),key=keyFor(candidate.key);valid(candidate,key);
   const existing=storage.getItem(key);
   if(existing!==null){const previous=valid(JSON.parse(existing),key);if(previous.receiptId!==candidate.receiptId||previous.endpoint!==candidate.endpoint||stable(previous.body)!==stable(candidate.body))throw Error('原加工请求不一致，已保留原记录');if(previous.state==='confirmed'&&candidate.state!=='confirmed')throw Error('这笔加工已确认，不能再次发送');if(previous.hadUnknown&&!candidate.hadUnknown)throw Error('原结果曾未知，不能更改核对状态')}
   const json=JSON.stringify(candidate);storage.setItem(key,json);
   if(storage.getItem(key)!==json)throw Error('加工请求未可靠保存，未发送；请核对浏览器存储');
   return clone(candidate);
  }
  function remove(record){
   const key=keyFor(record.key);valid(record,key);const existing=storage.getItem(key);if(existing===null)return;
   const current=valid(JSON.parse(existing),key);
   if(stable(current.body)!==stable(record.body)||current.state!=='confirmed'||record.state!=='confirmed')throw Error('原加工记录尚未完整确认，继续保留');
   storage.removeItem(key);if(storage.getItem(key)!==null)throw Error('加工已确认，但本机记录尚未清理');
  }
  function reject(record){
   const key=keyFor(record.key);valid(record,key);const existing=storage.getItem(key);if(existing===null)return;
   const current=valid(JSON.parse(existing),key);
   if(current.state!=='prepared'||current.hadUnknown||record.hadUnknown||stable(current.body)!==stable(record.body))throw Error('原结果曾未知，不能更改原请求');
   storage.removeItem(key);if(storage.getItem(key)!==null)throw Error('原请求尚未解除，请保留并核对');
  }
  return {actorId,list,write,remove,reject,keyFor,blocked:receiptId=>list().some(r=>r.receiptId===receiptId)};
 }
 function normalized(body){return {action:body.action,operation_key:body.operation_key,lot_version:body.lot_version,quantity:body.quantity??0,job_id:body.job_id??null,job_version:body.job_version??null,actual_output:body.actual_output??0,actual_input_quantity:body.actual_input_quantity??null,location_id:body.location_id??null,layout_version:body.layout_version??null,confirm_overproduction:body.confirm_overproduction??false,output_kind:body.output_kind??'finished',output_version:body.output_version??0};}
 function validBody(body){return body?.action==='complete'&&typeof body.operation_key==='string'&&body.operation_key.length>=8&&body.operation_key.length<=70&&positive(body.expected_actor_id)&&positive(body.lot_version)&&body.quantity===0&&positive(body.job_id)&&positive(body.job_version)&&positive(body.actual_input_quantity)&&positive(body.actual_output)&&positive(body.location_id)&&(body.layout_version===null||positive(body.layout_version))&&typeof body.confirm_overproduction==='boolean'&&['semi','finished'].includes(body.output_kind)&&Number.isSafeInteger(body.output_version)&&body.output_version>=0;}
 function identity(data,record){
  if(!positive(data?.current_actor_id))throw Error('加工核对回执不完整，请再次查原结果');
  if(data.current_actor_id!==record.actorId)throw Error('账号已变化，请切回原账号核对；原加工请求已保留');
 }
 function traceUrl(id){return '/api/production/stock-preparation/history/job:'+id;}
 function validateComplete(data,record,resolve=false){
  identity(data,record);const p=data.completion_receipt,body=record.body;
  const bad=()=>{throw Error('加工证明不完整或与原请求不一致，请查原结果；原内容已保留')};
  if(resolve?(data.status!=='completed'||data.operation_key!==record.key):(data.proof_status!=='complete'))bad();
  if(!p||p.schema!==1||p.operation_key!==record.key||p.actor_id!==record.actorId||p.receipt_item_id!==record.receiptId||(p.customer_id!==null&&!positive(p.customer_id))||!positive(p.source_lot_id)||stable(p.request)!==stable(normalized(body)))bad();
  if(p.requested_job_id!==body.job_id||p.original_job_id!==body.job_id||!positive(p.completed_job_id)||p.actual_input_quantity!==body.actual_input_quantity||p.actual_output!==body.actual_output||p.output_kind!==body.output_kind||!positive(p.output_lot_id)||p.location_id!==body.location_id||p.layout_version!==body.layout_version||typeof p.location_name!=='string'||(p.warehouse_floor!==null&&!Number.isSafeInteger(p.warehouse_floor))||p.input_unit!=='张'||(p.output_unit!==null&&typeof p.output_unit!=='string'))bad();
  if(!Number.isSafeInteger(p.remaining_input_quantity)||p.remaining_input_quantity<0||(p.continuation_job_id!==null&&!positive(p.continuation_job_id))||(p.remaining_input_quantity===0&&p.continuation_job_id!==null)||p.continuation_job_id===p.completed_job_id||p.trace_url!==traceUrl(p.completed_job_id))bad();
  if(typeof p.input_stock_unit!=='string'||!p.input_stock_unit.trim()||typeof p.output_stock_unit!=='string'||!p.output_stock_unit.trim()||!positive(p.output_stock_quantity)||!positive(p.consume_movement_id)||!positive(p.output_movement_id)||p.consume_movement_id===p.output_movement_id)bad();
  if(record.summary.customerId&&p.customer_id!==record.summary.customerId)bad();
  const result=resolve?data.result:data;
  if(!result||result.action!=='complete'||result.job_id!==p.completed_job_id)bad();
  if(p.completed_job_id!==p.requested_job_id){if(result.original_job_id!==p.original_job_id||result.completed_job_id!==p.completed_job_id||result.continuation_job_id!==p.continuation_job_id||result.remaining_input_quantity!==p.remaining_input_quantity)bad()}
  else if(p.remaining_input_quantity!==0||p.continuation_job_id!==null)bad();
  if(resolve&&data.trace_url!==p.trace_url)bad();
  return clone(p);
 }
 function validateResolve(data,record){
  identity(data,record);if(data.operation_key!==record.key)throw Error('加工核对标识不一致，请再次查原结果');
  if(data.status==='completed')return {status:'completed',receipt:validateComplete(data,record,true)};
  if(data.status==='not_recorded'&&data.completion_receipt===null&&data.result===null&&data.trace_url===null)return {status:'not_recorded'};
  if(data.status==='legacy_trace'&&data.completion_receipt===null){const id=data.result?.completed_job_id??data.result?.job_id;if(data.trace_url!==null&&(!positive(id)||data.trace_url!==traceUrl(id)))throw Error('历史加工追溯回执不完整，请再次核对');return {status:'legacy_trace',traceUrl:data.trace_url}}
  throw Error('加工核对回执不完整，请再次查原结果');
 }
 function install(app){
 app.component('stock-completion-results',{
  props:{records:{type:Array,default:()=>[]},error:{type:String,default:''},requests:{type:Object,default:()=>({})}},emits:['reload','resolve','continue','view','refresh'],
  template:`<section v-if="records.length||error" class="stock-completion-results" aria-label="原加工保存结果">
   <div v-if="error" class="status red" role="alert">{{error}} <button class="btn small" @click="$emit('reload')">重新读取记录</button></div>
   <article v-for="r in records" :key="r.key" class="stock-source-summary" :aria-busy="!!requests[r.key]">
    <strong>{{r.state==='confirmed'?'加工已确认':'原加工结果待核对'}} · {{r.summary.code}} · {{r.summary.name}}</strong>
    <div>{{r.summary.customerName}} · 原来源 {{r.summary.source}}<span v-if="r.summary.lotNumber"> · {{r.summary.lotNumber}}</span></div>
    <div v-if="r.state==='confirmed'">本次投入 {{r.receipt.actual_input_quantity}} 张 · 入库 {{r.receipt.actual_output}} {{r.receipt.output_unit||'（单位待核对）'}} · {{r.receipt.output_kind==='semi'?'按子件存放':'成品'}} · {{r.receipt.location_name||('货位 #'+r.receipt.location_id+'（名称未填）')}}</div>
    <div v-else>原请求：投入 {{r.body.actual_input_quantity}} 张 · 产出 {{r.body.actual_output}} {{r.summary.outputUnit}} · {{r.body.output_kind==='semi'?'按子件存放':'成品'}} · {{r.summary.location}}</div>
    <div v-if="r.lastError" class="status orange" role="status">{{r.lastError}}</div><div v-if="r.cacheError" class="status orange">{{r.cacheError}}</div><div v-if="r.refreshError" class="status orange">{{r.refreshError}}</div>
    <div class="toolbar-group"><button class="btn small" :disabled="!!requests[r.key]" @click="$emit('resolve',r)">{{requests[r.key]?'正在核对…':r.state==='confirmed'?'只读核对':'查原结果'}}</button><button v-if="r.state==='unknown'&&r.notRecorded" class="btn" :disabled="!!requests[r.key]" @click="$emit('continue',r)">按原内容继续</button><button v-if="r.receipt?.trace_url||r.traceUrl" class="btn small" :disabled="!!requests[r.key]" @click="$emit('view',r)">查看加工记录</button><button v-if="r.state==='confirmed'&&r.refreshError" class="btn small" :disabled="!!requests[r.key]" @click="$emit('refresh',r)">只读刷新列表</button></div>
    <details v-if="r.state!=='confirmed'"><summary>管理员核对依据</summary><div>原账号 {{r.actorId}} · {{r.createdAt}} · 原操作标识 {{r.key}}</div><div>请按原来源批次、产品、投入、产出和目标货位核对加工记录；未证明完成或取消前保留原请求。</div></details>
   </article>
  </section>`
 });app.mixin({
  data(){return this.$parent?{}:{stockCompletionRows:[],stockCompletionError:'',stockCompletionRequests:{},stockCompletionActor:null};},
  watch:{authGeneration(){this.reloadStockCompletionRecovery();},'user.id'(){this.reloadStockCompletionRecovery();}},
  methods:{
   stockCompletionStore(){return createStore(localStorage,this.user?.id);},
   stockCompletionHttp(){return axios.create({withCredentials:true,timeout:60000});},
   stockCompletionCurrent(op){return this.user?.id===op.actor&&this.authGeneration===op.auth;},
   stockCompletionMessage(error){const detail=error?.response?.data?.detail;return typeof detail==='string'?detail:detail?.message||error?.message||'加工结果暂未确认，请查原结果';},
   reloadStockCompletionRecovery(){
    if(!positive(this.user?.id)){if(Object.keys(this.stockCompletionRequests).length)this.stockPrepBusy=false;if(this.stockPrepDialog?.completionActor)this.stockPrepDialog=null;if(positive(this.stockCompletionActor)){this.stockLocations=[];this.stockLocationsLoading=false;this.stockLocationsError='';}this.stockCompletionRows=[];this.stockCompletionError='';this.stockCompletionActor=null;this.stockCompletionRequests={};return;}
    const previousActor=this.stockCompletionActor,same=previousActor===this.user.id,old=same?this.stockCompletionRows:[];this.stockCompletionActor=this.user.id;
    if(!same){if(Object.keys(this.stockCompletionRequests).length)this.stockPrepBusy=false;if(this.stockPrepDialog?.completionActor&&this.stockPrepDialog.completionActor!==this.user.id)this.stockPrepDialog=null;if(positive(previousActor)){this.stockLocations=[];this.stockLocationsLoading=false;this.stockLocationsError='';}this.stockCompletionRequests={};}
    try{const store=this.stockCompletionStore(),rows=store.list().map(r=>{const previous=old.find(p=>p.key===r.key);if(previous?.state==='confirmed')return previous;if(r.state==='prepared'&&!this.stockCompletionRequests[r.key]){r.state='unknown';r.hadUnknown=true;try{store.write(r)}catch(error){r.cacheError=this.stockCompletionMessage(error)}}return Object.assign(previous||{},r)});this.stockCompletionRows=[...rows,...old.filter(r=>r.state==='confirmed'&&r.cacheCleared&&!rows.some(p=>p.key===r.key))];this.stockCompletionError='';}
    catch(error){this.stockCompletionError='恢复记录无法读取：'+this.stockCompletionMessage(error);if(!same)this.stockCompletionRows=[];}
   },
   stockCompletionRecord(row){return this.stockCompletionRows.find(r=>r.receiptId===row?.receipt_item_id&&!r.cacheCleared);},
   stockCompletionLocked(row){return !!this.stockCompletionError||!!this.stockCompletionRecord(row);},
   restoreStockCompletionDialog(dialog){const r=this.stockCompletionRecord(dialog.row);if(!r||dialog.row.entry_type!=='single_job')return;dialog.inputQuantity=r.body.actual_input_quantity;if(dialog.job)dialog.job._actual=r.body.actual_output;dialog.disposition=r.body.output_kind;dialog.location=r.body.location_id;dialog.completionKey=r.key;},
   async saveStockCompletion(row,payload,job){
    this.reloadStockCompletionRecovery();if(this.stockCompletionLocked(row)){this.stockPrepError=this.stockCompletionError||'原加工结果待核对，请先查原结果';return false;}
    const actor=this.user?.id,key=this.stockOperationKey(),body={...clone(payload),operation_key:key,expected_actor_id:actor};
    if(!validBody(body)){this.stockPrepError='任务版本、实际数量或货位信息不完整，请刷新核对后再保存';return false;}
    const product=job?.product||row.job?.product||{},location=this.stockLocations.find(l=>l.id===body.location_id);
    const record={schema:'stock-completion-local-v1',actorId:actor,key,receiptId:row.receipt_item_id,endpoint:'/api/production/stock-preparation/'+row.receipt_item_id+'/actions',body,summary:{code:product.code||row.code||'',name:product.name||row.name||'加工产品',customerName:row.customer_name||'',customerId:row.customer_id||null,source:job?.source_location||row.location||'原来源位置',lotNumber:row.lot_number||'',location:location?.location_name||'原入库货位',inputUnit:'张',outputUnit:job?.output_unit||'单位待核对'},state:'prepared',hadUnknown:false,createdAt:new Date().toISOString()};
    try{const store=this.stockCompletionStore();if(store.blocked(record.receiptId))throw Error('这批来源已有待核对请求，请先查原结果');store.write(record);this.stockCompletionRows.push(record)}catch(error){this.stockPrepError='未发送：'+this.stockCompletionMessage(error);return false;}
    return this.sendStockCompletion(this.stockCompletionRows.find(r=>r.key===key),true);
   },
   stockCompletionAuthError(error,op){if(this.stockCompletionCurrent(op)&&error?.response?.status===401)global.erpAuthRequired?.();},
   async sendStockCompletion(record,fresh=false){
    if(this.stockCompletionRequests[record.key]||record.actorId!==this.user?.id||record.state==='confirmed')return false;
    if(!fresh&&!record.notRecorded){record.lastError='请先查原结果，再明确按原内容继续';return false;}
    const op={actor:this.user.id,auth:this.authGeneration},dialog=this.stockPrepDialog;this.stockCompletionRequests[record.key]=true;this.stockPrepBusy=true;
    if(dialog?.row?.receipt_item_id===record.receiptId){dialog.loading=true;dialog.error='';}record.lastError='';
    try{
     const stored=this.stockCompletionStore().list().find(r=>r.key===record.key);if(!stored||stable(stored.body)!==stable(record.body))throw Error('原请求没有可靠存储，未发送');
     if(!fresh||stored.hadUnknown){record.hadUnknown=true;record.state='unknown';this.stockCompletionStore().write(record)}
     const {data}=await this.stockCompletionHttp().post(record.endpoint,clone(record.body));if(!this.stockCompletionCurrent(op))return false;
     const proof=validateComplete(data,record);await this.completeStockCompletion(record,proof,op,dialog);return true;
    }catch(error){
     if(!this.stockCompletionCurrent(op))return false;
     const headers=error?.response?.headers,rejected=headers?.['x-production-completion-rejected']==='1',preserve=headers?.['x-production-completion-preserve']==='1';
     if(fresh&&!record.hadUnknown&&rejected&&!preserve){try{this.stockCompletionStore().reject(record);this.stockCompletionRows=this.stockCompletionRows.filter(r=>r.key!==record.key);this.stockPrepError=this.stockCompletionMessage(error)+'；本次未保存，可更正后保存';if(dialog&&this.stockPrepDialog===dialog)dialog.error=this.stockPrepError;return false}catch(cacheError){record.cacheError=this.stockCompletionMessage(cacheError)}}
     record.state='unknown';record.hadUnknown=true;record.notRecorded=false;record.lastError=this.stockCompletionMessage(error);try{this.stockCompletionStore().write(record)}catch(cacheError){record.cacheError=this.stockCompletionMessage(cacheError)}
     if(dialog&&this.stockPrepDialog===dialog){dialog.error='加工结果待核对：'+record.lastError;this.restoreStockCompletionDialog(dialog)}this.stockPrepError='加工结果待核对，请查原结果';this.stockCompletionAuthError(error,op);return false;
    }finally{if(this.stockCompletionCurrent(op)){delete this.stockCompletionRequests[record.key];this.stockPrepBusy=false;if(dialog&&this.stockPrepDialog===dialog)dialog.loading=false;}}
   },
   async resolveStockCompletion(record){
    if(this.stockCompletionRequests[record.key]||record.actorId!==this.user?.id)return;
    const op={actor:this.user.id,auth:this.authGeneration},dialog=this.stockPrepDialog;this.stockCompletionRequests[record.key]=true;record.lastError='';record.notRecorded=false;
    if(dialog?.row?.receipt_item_id===record.receiptId)dialog.loading=true;
    try{const {data}=await this.stockCompletionHttp().post('/api/production/stock-preparation/'+record.receiptId+'/completion-result',{operation_key:record.key,original_request:clone(record.body),expected_actor_id:op.actor});if(!this.stockCompletionCurrent(op))return;const checked=validateResolve(data,record);
     if(checked.status==='completed'){await this.completeStockCompletion(record,checked.receipt,op,dialog);return;}
     if(record.state==='confirmed')throw Error('这笔加工已完整确认，请保留成功信息并再次只读核对');
     record.state='unknown';record.hadUnknown=true;record.notRecorded=checked.status==='not_recorded';record.traceUrl=checked.traceUrl||null;record.lastError=checked.status==='not_recorded'?'本次未查到原请求，未证明取消。请核对原内容后决定是否继续。':'原记录可追溯，但缺少完整加工证明；请查看加工记录并联系管理员核对。';this.stockCompletionStore().write(record);
    }catch(error){if(this.stockCompletionCurrent(op)){record.lastError=this.stockCompletionMessage(error);this.stockCompletionAuthError(error,op)}}
    finally{if(this.stockCompletionCurrent(op)){delete this.stockCompletionRequests[record.key];if(dialog&&this.stockPrepDialog===dialog)dialog.loading=false;}}
   },
   async completeStockCompletion(record,proof,op,dialog){
    record.state='confirmed';record.receipt=proof;record.notRecorded=false;record.traceUrl=proof.trace_url;record.lastError='';record.cacheError='';
    try{const store=this.stockCompletionStore();store.write(record);store.remove(record);record.cacheCleared=true}catch(error){record.cacheError='已确认，本机记录未清理：'+this.stockCompletionMessage(error)}
    const belongs=this.stockPrepDialog===dialog&&dialog?.row?.receipt_item_id===record.receiptId;if(belongs){this.rememberStockLocation(record.body.location_id);this.stockPrepDialog=null;}
    if(!this.stockCompletionCurrent(op))return;if(this.stockPrepDialog!==null&&!belongs){record.refreshError='加工已确认，请只读刷新列表核对';return;}await this.refreshStockCompletionList(record,op);if(belongs&&this.stockCompletionCurrent(op))this.showToast('已入库：'+proof.actual_output+' '+(proof.output_unit||'单位待核对')+' · '+(proof.location_name||'原货位名称待核对')+(record.refreshError?'；列表未刷新，请刷新核对':''),!!record.refreshError);
   },
   async refreshStockCompletionList(record,op=null){op=op||{actor:this.user?.id,auth:this.authGeneration};if(record.state!=='confirmed'||record.actorId!==op.actor)return;try{await this.loadStockWorkspace(this.stockPrepPage,{completion:true});if(this.stockCompletionCurrent(op))record.refreshError=this.stockPrepError?'加工已确认，列表未刷新；请只读刷新核对':''}catch(error){if(this.stockCompletionCurrent(op))record.refreshError='加工已确认，列表未刷新；请只读刷新核对'}},
   async viewStockCompletionTrace(record){const url=record.receipt?.trace_url||record.traceUrl;if(!url||this.stockCompletionRequests[record.key])return;const id=Number(url.split('job:')[1]);if(!positive(id)||url!==traceUrl(id))return;const op={actor:this.user?.id,auth:this.authGeneration},dialog=this.stockPrepDialog,page=this.activePage;this.stockCompletionRequests[record.key]=true;
    try{const {data}=await this.stockCompletionHttp().get(url);if(!this.stockCompletionCurrent(op)||this.stockPrepDialog!==dialog||this.activePage!==page)return;const sources=data.items||[];if(!sources.length)throw Error('加工记录暂不可读取，请重试');const trace={...sources[0],entry_type:'receipt',jobs:sources.flatMap(r=>r.jobs||[]),movements:sources.flatMap(r=>r.movements||[]),history:sources.flatMap(r=>r.history||[])};const opening=this.openStockDialog(trace,{completion:true}),opened=this.stockPrepDialog;await opening;if(this.stockCompletionCurrent(op)&&this.stockPrepDialog===opened){this.stockPrepDialog.view='history';this.stockPrepDialog.sources=sources}}
    catch(error){if(this.stockCompletionCurrent(op)){record.lastError='加工记录读取失败：'+this.stockCompletionMessage(error);this.stockCompletionAuthError(error,op)}}finally{if(this.stockCompletionCurrent(op))delete this.stockCompletionRequests[record.key];}
   },
  }
 });}
 global.ERPStockCompletionRecovery={PREFIX,createStore,stable,clone,positive,normalized,validBody,identity,validateComplete,validateResolve,traceUrl,install};
})(typeof window==='undefined'?globalThis:window);
