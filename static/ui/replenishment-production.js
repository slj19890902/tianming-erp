(function(root){
  'use strict';
  const pending=new Map(), prefix='erp-replenishment-production-v1:';
  const sourceUrl=id=>`/api/production/stock-replenishment/${id}/production-sources`;
  const scrapUrl='/api/production/scrap-production';
  const clone=value=>JSON.parse(JSON.stringify(value));
  const positive=value=>Number.isSafeInteger(Number(value)) && Number(value)>0;
  const key=()=> 'replenish-'+(root.crypto?.randomUUID?.() || Date.now().toString(36)+'-'+Math.random().toString(36).slice(2));
  function errorText(error){const detail=error.response?.data?.detail;return typeof detail==='string'?detail:detail?.message || error.message || '补库安排读取失败';}
  function hydrate(actor){
    if(!positive(actor))return;
    const storage=root.sessionStorage;
    for(let i=0;i<storage.length;i++){
      const name=storage.key(i);if(!name?.startsWith(prefix+actor+':'))continue;
      const identity=name.slice(prefix.length);if(pending.has(identity))continue;
      const held=JSON.parse(storage.getItem(name));
      if(held.actor!==Number(actor) || !positive(held.source) || identity!==`${actor}:${held.source}` || !['sheet','scrap'].includes(held.kind) || !['plan','finish_now','complete','cancel'].includes(held.payload?.action) || !held.payload.operation_key)throw Error('保存的补库请求不完整，请保留本页并联系管理员核对');
      pending.set(identity,{...held,identity,inFlight:false});
    }
  }
  function pendingForActor(actor){hydrate(Number(actor));return [...pending.entries()].filter(([,held])=>held.actor===Number(actor));}
  function retain(held){root.sessionStorage.setItem(prefix+held.identity,JSON.stringify({actor:held.actor,source:held.source,kind:held.kind,payload:held.payload}));pending.set(held.identity,held);}
  function discard(held){root.sessionStorage.removeItem(prefix+held.identity);pending.delete(held.identity);}
  async function submit(held,isCurrent,report,recover=false){
    if(held.inFlight || !isCurrent())return {outcome:'pending'};
    held.inFlight=true;report?.({identity:`${held.actor}:replenishment:${held.source}`,blocked:true});
    let checkingResult=false;
    try{
      let data;
      if(recover && held.kind==='sheet'){
        checkingResult=true;
        const response=await root.axios.post(sourceUrl(held.source)+'/result',{original_request:held.payload},{timeout:15000});
        if(!isCurrent())return {outcome:'pending'};
        if(response.data.status==='completed')data=response.data.result;
        else if(response.data.status!=='not_found')throw Error('原请求仍未确认，请稍后核对');
        checkingResult=false;
      }
      if(!data){
        if(!isCurrent())return {outcome:'pending'};
        data=(await root.axios.post(held.kind==='sheet'?sourceUrl(held.source):scrapUrl,clone(held.payload),{timeout:30000})).data;
      }
      discard(held);report?.({identity:`${held.actor}:replenishment:${held.source}`,blocked:false});
      return {outcome:'completed',data};
    }catch(error){
      const status=Number(error.response?.status);
      // Authentication failures during recovery do not prove the earlier write failed.
      if(!checkingResult && status>=400 && status<500 && ![401,403,408,429].includes(status)){
        discard(held);report?.({identity:`${held.actor}:replenishment:${held.source}`,blocked:false});
        return {outcome:'rejected',message:errorText(error)};
      }
      return {outcome:'pending',message:errorText(error)};
    }finally{held.inFlight=false;}
  }
  async function reconcileForActor(actor,isCurrent,report){
    const entries=pendingForActor(actor);if(entries.some(([,h])=>h.inFlight))return {complete:false,outcomes:[]};
    const outcomes=[];
    for(const [,held] of entries){if(!isCurrent())break;outcomes.push(await submit(held,isCurrent,report,true));}
    return {complete:pendingForActor(actor).length===0,outcomes};
  }
  const component={
    props:{actorId:Number,authGeneration:Number,sourceId:Number,productLabel:String,disabled:Boolean,canExecute:Boolean,managementOnly:Boolean,operationReporter:Function},
    emits:['changed'],
    data(){return {opened:false,tab:'sheet',loading:false,busy:false,error:'',message:'',detail:null,scrap:null,locations:[],selected:null,quantity:'',scrapQuantity:'',variant:null,locationId:null,remarks:'',uncertain:false,sequence:0};},
    computed:{identity(){return `${this.actorId}:${this.sourceId}`;},context(){return `${this.identity}:${this.authGeneration}`;}},
    created(){this.restore();},beforeUnmount(){this.disposed=true;this.sequence++;},
    watch:{context(){this.sequence++;this.opened=false;this.loading=false;this.busy=false;this.detail=null;this.scrap=null;this.locations=[];this.error='';this.message='';this.restore();}},
    methods:{
      report(state){this.operationReporter?.(state);},
      restore(){try{hydrate(Number(this.actorId));this.uncertain=pending.has(this.identity);if(this.uncertain)this.report({identity:`${this.actorId}:replenishment:${this.sourceId}`,blocked:true});}catch(e){this.error=errorText(e);this.uncertain=true;}},
      current(context){return !this.disposed && this.context===context;},
      async open(tab){if(this.busy || (this.disabled && !this.uncertain))return;if(!positive(this.sourceId)){this.error='补库来源不完整，请刷新原补库单';return;}this.tab=tab;this.opened=true;this.returnFocus=root.document?.activeElement;this.$nextTick(()=>this.$refs.dialog?.focus());this.restore();if(!this.uncertain)await this.load();},
      close(){if(this.busy)return;this.opened=false;this.sequence++;this.loading=false;this.$nextTick(()=>{if(this.returnFocus?.isConnected)this.returnFocus.focus();});},
      keydown(event){
        if(event.key==='Escape'){event.preventDefault();event.stopPropagation();this.close();return;}
        if(event.key!=='Tab')return;
        const panel=this.$refs.dialog,controls=[...(panel?.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary') || [])].filter(el=>el.getClientRects().length),first=controls[0],last=controls.at(-1),active=root.document?.activeElement;
        if(!first){event.preventDefault();panel?.focus();}else if(event.shiftKey && (active===first || active===panel)){event.preventDefault();last.focus();}else if(!event.shiftKey && (active===last || active===panel)){event.preventDefault();first.focus();}
      },
      async load(){
        if(this.busy || this.uncertain || !positive(this.sourceId))return;
        const context=this.context,seq=++this.sequence;this.loading=true;this.error='';this.detail=null;this.scrap=null;this.selected=null;this.quantity='';
        try{
          const {data}=await root.axios.get(sourceUrl(this.sourceId),{timeout:15000});
          if(!this.current(context) || seq!==this.sequence)return;
          this.detail=data;this.locationId=data.storage?.location_id || null;this.variant=data.variants?.[0]?.id || null;this.scrapQuantity=data.remaining_output;
          if(this.canExecute)await this.loadLocations();
        }catch(e){if(this.current(context) && seq===this.sequence)this.error=errorText(e);}
        finally{if(this.current(context) && seq===this.sequence)this.loading=false;}
      },
      async loadLocations(){
        const context=this.context,seq=this.sequence;
        try{const {data}=await root.axios.get('/api/production/stock-preparation/locations',{timeout:15000});if(this.current(context) && seq===this.sequence)this.locations=data.items || [];}
        catch(e){if(this.current(context) && seq===this.sequence)this.error='货位读取失败：'+errorText(e);}
      },
      choose(row){if(this.busy || this.uncertain)return;this.selected=row;this.quantity=Math.min(row.available_quantity,Math.ceil(this.detail.remaining_output*row.pieces_per_box/row.yield_per_sheet));},
      async save(kind,action,job){
        if(this.busy || this.loading || this.uncertain || this.disabled || !this.canExecute || !this.detail)return;
        if(action==='cancel' && !root.confirm('取消这项未完工安排？释放材料并恢复尚需补库的缺口。'))return;
        const context=this.context,source=this.sourceId,identity=this.identity,actor=this.actorId;
        let payload;
        if(kind==='sheet'){
          payload={action,operation_key:key(),source_fingerprint:this.detail.source_fingerprint};
          if(action==='plan'){
            if(!this.selected || !positive(this.quantity) || Number(this.quantity)>this.selected.available_quantity){this.error='请选择片料并填写可用范围内的投入张数';return;}
            Object.assign(payload,{inventory_lot_id:this.selected.inventory_lot_id,lot_version:this.selected.version,quantity:Number(this.quantity)});
          }else{
            Object.assign(payload,{job_id:job.id,job_version:job.version,lot_version:job.lot_version});
            if(action==='complete'){
              if(!positive(job._output)){this.error='请填写本次合格产出数量';return;}
              const location=this.locations.find(row=>row.id===Number(job._location || this.locationId));
              if(!location || !positive(location.layout_version)){this.error='请读取并选择有效的成品实际货位后再完工';return;}
              const actual=Number(job._output),over=actual>Number(job.expected_output);
              if(over && !root.confirm(`实际产出 ${actual} 高于理论 ${job.expected_output}，确认按实际数量入库？`))return;
              Object.assign(payload,{actual_output:actual,lot_version:job.lot_version,location_id:Number(job._location || this.locationId) || null,layout_version:Number(location.layout_version),confirm_overproduction:over});
            }
          }
        }else{
          const now=new Date(),date=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
          payload={action,operation_key:key(),product_id:this.detail.product_id,stock_replenishment_item_id:source,stock_date:job?.stock_date || date};
          if(job)Object.assign(payload,{job_id:job.id,job_version:job.version});
          else{
            if(!positive(this.scrapQuantity) || Number(this.scrapQuantity)>this.detail.remaining_output){this.error='请填写未覆盖范围内的成品数量';return;}
            Object.assign(payload,{quantity:Number(this.scrapQuantity),location_id:this.locationId,variant_id:this.variant,remarks:this.remarks});
          }
        }
        this.busy=true;this.error='';this.message='';this.report({identity:`${actor}:replenishment:${source}`,blocked:true});
        try{
          if(action==='plan' || action==='finish_now'){
            const {data}=await root.axios.post(kind==='sheet'?sourceUrl(source)+'/preview':scrapUrl+'/preview',payload,{timeout:15000});
            if(!this.current(context))return;
            payload.preview_hash=data.preview_hash;
          }
          if(!this.current(context))return;
          const held={actor,source,identity,kind,payload:clone(payload),inFlight:false};retain(held);this.uncertain=true;
          const result=await submit(held,()=>this.current(context),this.report);
          if(this.current(context))await this.settle(result);
        }catch(e){if(this.current(context))this.error=errorText(e);}
        finally{if(this.current(context)){this.busy=false;this.uncertain=pending.has(identity);if(!this.uncertain)this.report({identity:`${actor}:replenishment:${source}`,blocked:false});}else if(!pending.has(identity))this.report({identity:`${actor}:replenishment:${source}`,blocked:false});}
      },
      async recover(){
        const held=pending.get(this.identity);if(!held || this.busy || held.inFlight)return;
        const context=this.context;this.busy=true;this.error='';
        try{const result=await submit(held,()=>this.current(context),this.report,true);if(this.current(context))await this.settle(result);}
        finally{if(this.current(context))this.busy=false;}
      },
      async settle(result){
        this.uncertain=pending.has(this.identity);
        if(result.outcome==='pending'){this.error=(result.message || '原请求仍在处理中')+'；结果未确认，请核对原请求，勿重新提交。';return;}
        this.detail=null;this.selected=null;
        if(result.outcome==='rejected'){this.error=result.message;return;}
        this.message='已保存，正在刷新补库覆盖和生产安排。';this.$emit('changed',result.data);
        this.busy=false;await this.load();if(!this.detail)this.error='操作已保存；'+this.error+'。请刷新列表，不要重复提交。';
      },
      plannedJobs(){return (this.detail?.jobs || []).map(job=>{if(job._output===undefined)job._output=job.expected_output;return job;});},
      statusLabel(status){return {pending:'待生产',planned:'待生产',completed:'已完工',cancelled:'已取消'}[status] || status;},
      pendingJob(job){return ['pending','planned'].includes(job.status);}
    },
    template:`<div class="manual-sheet-deduction replenishment-production">
      <div class="toolbar-group"><template v-if="!managementOnly"><button class="btn small" :disabled="disabled && !uncertain" @click="open('sheet')">使用库存片料</button><button class="btn small" :disabled="disabled && !uncertain" @click="open('scrap')">废料生产</button></template><button class="btn small" :disabled="disabled && !uncertain" @click="open('jobs')">{{uncertain?'核对原请求':'生产安排 / 记录'}}</button></div>
      <div v-if="error && !opened" class="status orange">{{error}}</div>
      <teleport to="body"><div v-if="opened" class="manual-sheet-backdrop" @keydown="keydown" @click.self="close"><section ref="dialog" tabindex="-1" role="dialog" aria-modal="true" aria-label="补库用料与生产" class="manual-sheet-dialog">
        <header class="manual-sheet-head"><h3>补库用料与生产 · {{productLabel || detail?.product_code || ''}}</h3><button class="btn" :disabled="busy" @click="close">关闭</button></header>
        <div class="manual-sheet-body"><div class="toolbar-group"><button v-for="(label,value) in {sheet:'使用库存片料',scrap:'废料生产',jobs:'生产安排 / 记录'}" :class="['btn',{primary:tab===value}]" :disabled="busy || uncertain" @click="tab=value">{{label}}</button><button class="btn" :disabled="busy || uncertain || loading" @click="load">刷新</button></div>
          <p v-if="loading" role="status">正在读取补库需求和材料…</p><p v-if="error" class="notice danger" role="alert">{{error}}</p><p v-if="message" role="status">{{message}}</p><div v-if="uncertain" class="notice warning">原请求及数量已保留。<button class="btn primary" :disabled="busy" @click="recover">{{busy?'正在核对…':'核对原请求'}}</button></div>
          <template v-if="detail"><div class="manual-sheet-demand"><strong>{{detail.product_code}} {{detail.product_name}}</strong><span>补库目标 <b>{{detail.required_output}}</b></span><span>已覆盖 <b>{{detail.covered_output}}</b></span><span>未覆盖 <b>{{detail.remaining_output}}</b></span><span>剩余报料 <b>{{detail.remaining_sheet_quantity}}</b> 张</span></div>
            <p v-if="detail.blocked_reason" class="notice warning">{{detail.blocked_reason}}</p><p v-if="detail.purchase_blocked_reason" class="notice warning">{{detail.purchase_blocked_reason}}</p>
            <template v-if="tab==='sheet'"><p>安排时预占材料；实际完工才扣料并入成品库。</p><div class="manual-sheet-table-wrap"><table class="manual-sheet-table"><thead><tr><th>选择</th><th>材料 / 规格</th><th>可用张数</th><th>每张出片</th><th>位置 / 适用提示</th></tr></thead><tbody><tr v-for="row in detail.candidates" :key="row.inventory_lot_id" :class="{'manual-sheet-selected':selected===row}"><td><input type="radio" :checked="selected===row" :disabled="busy || uncertain || !!detail.blocked_reason || !canExecute" aria-label="选择片料批次" @change="choose(row)"></td><td>{{row.material_code}} / {{row.flute_type}}<br>{{row.board_length_mm}} × {{row.board_width_mm}}</td><td>{{row.available_quantity}}</td><td>{{row.yield_per_sheet}}<div v-if="row.pieces_per_box>1">每件 {{row.pieces_per_box}} 片</div></td><td>{{row.location}}<div>{{row.recommendation_label}}</div><div v-for="warning in row.warning_messages" class="status orange">{{warning}}</div></td></tr></tbody></table></div><p v-if="!detail.candidates?.length">当前没有符合条件的可用片料。</p><div v-if="selected" class="toolbar-group"><label>本次投入 <input class="input" type="number" min="1" :max="selected.available_quantity" v-model.number="quantity" :disabled="busy || uncertain"> 张</label><button class="btn primary" :disabled="busy || uncertain || !!detail.blocked_reason || !canExecute" @click="save('sheet','plan')">采用并安排生产</button></div></template>
            <template v-if="tab==='scrap'"><p>用于未入库存账的可用边料；账内片料请从“使用库存片料”领用。</p><div class="toolbar-group"><label>成品数量 <input class="input" type="number" min="1" :max="detail.remaining_output" v-model.number="scrapQuantity" :disabled="busy || uncertain"></label><label v-if="detail.variants?.length">工艺 <select class="select" v-model="variant" :disabled="busy || uncertain"><option v-for="v in detail.variants" :value="v.id">{{v.name}}</option></select></label></div><p>成品货位：{{detail.storage?.location?.location_name || detail.storage?.location?.location_code || '待选择'}}</p><button class="btn small" :disabled="busy || uncertain" @click="loadLocations">选择实际货位</button><location-picker v-if="locations.length" :locations="locations" v-model="locationId" :disabled="busy || uncertain"/><p><input class="input" v-model="remarks" placeholder="备注（可选）" :disabled="busy || uncertain"></p><div class="toolbar-group"><button class="btn primary" :disabled="busy || uncertain || !!detail.blocked_reason || !canExecute || detail.remaining_output<=0" @click="save('scrap','plan')">安排废料生产</button><button class="btn" :disabled="busy || uncertain || !!detail.blocked_reason || !canExecute || detail.remaining_output<=0" @click="save('scrap','finish_now')">已实际完成，直接入库</button></div></template>
            <template v-if="tab==='jobs'"><section v-for="job in plannedJobs()" :key="'sheet:'+job.id" class="manual-sheet-existing"><strong>库存片料 · {{statusLabel(job.status)}}</strong><p>投入 {{job.input_quantity}} 张 · 理论产出 {{job.expected_output}} · 实际产出 {{job.actual_output || 0}} · {{job.location}}</p><div v-if="pendingJob(job)" class="toolbar-group"><label>本次合格产出 <input class="input" type="number" min="1" v-model.number="job._output" :disabled="busy || uncertain || !canExecute"></label><button class="btn small" :disabled="busy || uncertain" @click="loadLocations">选择成品实际货位</button><location-picker v-if="locations.length" :locations="locations" v-model="job._location" :disabled="busy || uncertain"/><span v-else>{{detail.storage?.location?.location_name || '使用产品已保存货位'}}</span><button class="btn primary" :disabled="busy || uncertain || !canExecute" @click="save('sheet','complete',job)">完成并入库</button><button class="btn" :disabled="busy || uncertain || !canExecute" @click="save('sheet','cancel',job)">取消安排</button></div></section><section v-for="job in detail.scrap_jobs" :key="'scrap:'+job.id" class="manual-sheet-existing"><strong>废料 · {{statusLabel(job.status)}}</strong><p>{{job.quantity}} 成品 · {{job.variant?.name || '常用工艺'}}</p><div v-if="pendingJob(job)" class="toolbar-group"><button class="btn primary" :disabled="busy || uncertain || !canExecute" @click="save('scrap','complete',job)">完成并入库</button><button class="btn" :disabled="busy || uncertain || !canExecute" @click="save('scrap','cancel',job)">取消安排</button></div></section><p v-if="!detail.jobs?.length && !detail.scrap_jobs?.length">尚无生产安排。</p></template>
          </template></div></section></div></teleport></div>`
  };
  const jobsComponent={
    props:{actorId:Number,authGeneration:Number,canExecute:Boolean,disabled:Boolean,operationReporter:Function},emits:['changed'],
    data(){return {rows:[],loading:false,error:'',history:false,sequence:0};},
    mounted(){this.load();},beforeUnmount(){this.disposed=true;this.sequence++;},
    watch:{actorId(){this.rows=[];this.load();},authGeneration(){this.rows=[];this.load();}},
    methods:{async load(){const actor=this.actorId,generation=this.authGeneration,seq=++this.sequence;this.loading=true;this.error='';try{const {data}=await root.axios.get('/api/production/stock-replenishment/production-sources/jobs',{params:{include_completed:this.history},timeout:15000});if(this.disposed || actor!==this.actorId || generation!==this.authGeneration || seq!==this.sequence)return;const rows=new Map();for(const job of data.items || [])rows.set(job.stock_replenishment_item_id,job);for(const [,held] of pendingForActor(actor))if(!rows.has(held.source))rows.set(held.source,{stock_replenishment_item_id:held.source,product_name:'待核对补库安排'});this.rows=[...rows.values()];}catch(e){if(actor===this.actorId && generation===this.authGeneration && seq===this.sequence)this.error=errorText(e);}finally{if(actor===this.actorId && generation===this.authGeneration && seq===this.sequence)this.loading=false;}},async changed(value){this.$emit('changed',value);await this.load();}},
    template:`<section class="panel"><div class="toolbar-group"><strong>补库片料 / 废料生产</strong><label><input type="checkbox" v-model="history" :disabled="loading" @change="load"> 包含已完成记录</label><button class="btn small" :disabled="loading" @click="load">{{loading?'读取中…':'刷新'}}</button></div><p v-if="error" class="notice danger">{{error}}</p><div v-for="row in rows" :key="row.stock_replenishment_item_id" class="manual-sheet-existing-row"><span>{{row.order_number}} · {{row.product_code}} {{row.product_name}}</span><replenishment-production :actor-id="actorId" :auth-generation="authGeneration" :source-id="row.stock_replenishment_item_id" :product-label="row.product_code" :can-execute="canExecute" :disabled="disabled" :operation-reporter="operationReporter" management-only @changed="changed"/></div><p v-if="!loading && !error && !rows.length" class="muted">当前没有{{history?'':'待完成的'}}补库片料或废料生产安排。</p></section>`
  };
  root.TMReplenishmentProduction={component,jobsComponent,pending,pendingForActor,reconcileForActor};
})(window);
