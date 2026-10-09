(function(global){
 'use strict';
 function install(app){
  app.component('location-picker',{
   props:{modelValue:[Number,String],locations:{type:Array,default:()=>[]},disabled:Boolean},emits:['update:modelValue'],
   data:()=>({floor:'3',major:'',area:'',rack:'',level:''}),
   computed:{
    valid(){return this.locations.filter(l=>l.is_active!==false && l.placement_status!=='unplaced');},
    floors(){return [...new Set(this.valid.map(l=>String(l.warehouse_floor)))].filter(f=>/^[1-9]\d*$/.test(f)).sort((a,b)=>Number(a)-Number(b));},
    majors(){return [...new Set(this.valid.filter(l=>String(l.warehouse_floor)===this.floor).map(l=>this.group(l)))].sort((a,b)=>a==='其他区域'?1:b==='其他区域'?-1:a.localeCompare(b,'en',{numeric:true}));},
    areas(){return [...new Map(this.valid.filter(l=>String(l.warehouse_floor)===this.floor && this.group(l)===this.major).map(l=>[l.area_id||l.area_code,{code:String(l.area_id||l.area_code),name:this.areaName(l),sort:this.areaSort(l)}])).values()].sort((a,b)=>a.sort.localeCompare(b.sort,'en',{numeric:true})||a.name.localeCompare(b.name,'zh-CN',{numeric:true}));},
    areaPlaces(){return this.valid.filter(l=>String(l.warehouse_floor)===this.floor && this.group(l)===this.major && String(l.area_id||l.area_code)===this.area);},
    racks(){return [...new Map(this.areaPlaces.map(l=>[this.rackKey(l),{id:this.rackKey(l),name:l.storage_type==='rack'?this.rackName(l):'地面货位'}])).values()].sort((a,b)=>a.name.localeCompare(b.name,'zh-CN',{numeric:true}));},
    levels(){return [...new Set(this.areaPlaces.filter(l=>this.rackKey(l)===this.rack).map(l=>l.level_no).filter(Boolean))].sort((a,b)=>a-b);},
    places(){return this.areaPlaces.filter(l=>this.rackKey(l)===this.rack && (this.rack==='ground'||String(l.level_no)===this.level)).sort((a,b)=>(a.slot_no||0)-(b.slot_no||0)||a.location_name.localeCompare(b.location_name,'zh-CN',{numeric:true}));},
   },
   methods:{
    areaName(l){const name=l.area_master_name||l.area_name||'';return /^(EDIT-|L-)/i.test(name)?'区域名称待完善':name||'区域名称待完善';},
    group(l){const name=this.areaName(l).replace(/左区L\d+/g,'左区');return name.match(/(?:^|[^A-Za-z])([A-KM-Z][A-Z]?)(?=[0-9货架区零临时]|$)/i)?.[1]?.[0]?.toUpperCase() || '其他区域';},
    areaSort(l){return this.areaName(l).match(/[A-Z]{1,2}(?:货架?|架|区)?\d+(?:-\d+)?/i)?.[0]?.replace(/货架?|架|区/g,'')||this.areaName(l);},
    rackKey(l){return l.storage_type==='rack'?String(l.map_rack_id||l.rack_code||'rack'):'ground';},
    rackName(l){const name=l.rack_display_name||'';return name && !/^(?:EDIT-|[LR]\d{3})/i.test(name)?name:('货架 '+(l.rack_code||''));},
    sync(){const row=this.valid.find(l=>Number(l.id)===Number(this.modelValue));if(row){this.floor=String(row.warehouse_floor);this.major=this.group(row);this.area=String(row.area_id||row.area_code);this.rack=this.rackKey(row);this.level=String(row.level_no||'');}},
    change(step){const fields=['floor','major','area','rack','level'];for(const field of fields.slice(fields.indexOf(step)+1))this[field]='';if(this.majors.length===1)this.major=this.majors[0];if(this.areas.length===1)this.area=this.areas[0].code;if(this.racks.length===1)this.rack=this.racks[0].id;if(this.levels.length===1)this.level=String(this.levels[0]);this.$emit('update:modelValue',null);},
   },
   watch:{modelValue:{immediate:true,handler(){this.sync();}},locations(){this.sync();}},
   template:`<div class="location-picker"><select aria-label="楼层" class="select" v-model="floor" :disabled="disabled" @change="change('floor')"><option value="">楼层</option><option v-for="f in floors" :key="f" :value="f">{{f}} 楼</option></select><select aria-label="区域大类" class="select" v-model="major" :disabled="disabled||!floor" @change="change('major')"><option value="">区域</option><option v-for="m in majors" :key="m">{{m}}</option></select><select aria-label="子区域" class="select" v-model="area" :disabled="disabled||!major" @change="change('area')"><option value="">子区域</option><option v-for="a in areas" :key="a.code" :value="a.code">{{a.name}}</option></select><select aria-label="货架或地面" class="select" v-model="rack" :disabled="disabled||!area" @change="change('rack')"><option value="">货架 / 地面</option><option v-for="r in racks" :key="r.id" :value="r.id">{{r.name}}</option></select><select v-if="rack&&rack!=='ground'" aria-label="货架层" class="select" v-model="level" :disabled="disabled" @change="change('level')"><option value="">层</option><option v-for="n in levels" :value="String(n)">{{n}} 层</option></select><select aria-label="具体货位" class="select" :value="modelValue||''" :disabled="disabled||!rack||(rack!=='ground'&&!level)" @change="$emit('update:modelValue',Number($event.target.value)||null)"><option value="">{{rack==='ground'?'货位':'格'}}</option><option v-for="l in places" :key="l.id" :value="l.id">{{rack==='ground'?l.location_name:l.slot_no+' 格'}} · {{l.is_empty?'空位':'有货'}}</option></select></div>`,
  });
  app.mixin({
   mounted(){if(!this.$parent)global.addEventListener('message',this.acceptStockLocation);},
   beforeUnmount(){if(!this.$parent)global.removeEventListener('message',this.acceptStockLocation);},
   data(){return this.$parent?{}:{stockPrepDialog:null,stockAssemblyDialog:null,stockLocations:[],stockLocationsLoading:false,stockLocationsError:'',stockPrepPendingCount:0,productionPendingSource:'all',stockDialogSequence:0,stockLocationMap:null};},
   methods:{
    async ensureStockLocations({force=false,completion=false}={}){
     if(this.stockLocationsLoading)return false;if(!force&&this.stockLocations.length)return true;
     const auth=this.authGeneration,actor=this.user?.id;this.stockLocationsLoading=true;this.stockLocationsError='';
     try{const {data}=await (completion?this.stockCompletionHttp():axios).get('/api/production/stock-preparation/locations');if(auth!==this.authGeneration||actor!==this.user?.id)return false;this.stockLocations=data.items||[];return true;}
     catch(e){if(auth===this.authGeneration&&actor===this.user?.id){this.stockLocationsError='货位读取失败，请重试';if(completion)this.stockCompletionAuthError(e,{actor,auth});}return false;}finally{if(auth===this.authGeneration&&actor===this.user?.id)this.stockLocationsLoading=false;}
    },
    stockLastLocation(){try{const id=Number(localStorage.getItem('erp-stock-place:'+this.user?.id));return this.stockLocations.some(l=>l.id===id)?id:null;}catch{return null;}},
    rememberStockLocation(id){if(id)try{localStorage.setItem('erp-stock-place:'+this.user?.id,String(id));}catch{}},
    async reverseStockAssembly(row){
     const a=row.assembly;if(this.user?.role!=='admin'||this.stockPrepBusy)return;
     if(!window.confirm('确认已核对实物，需要撤销这次组装并恢复子件库存？已送货、占用或移动的成套库存不能撤销。'))return;
     const payload={action:'unassemble',parent_id:a.recipe.parent_id,group_key:a.group_key,assembly_key:a.key,output_version:a.version,sources:a.source_versions,confirm_unused:true};
     const signature=JSON.stringify(payload);if(row._undo?.signature!==signature)row._undo={signature,key:this.stockOperationKey()};payload.operation_key=row._undo.key;
     const auth=this.authGeneration;this.stockPrepBusy=true;
     try{await axios.post('/api/production/stock-preparation/group-actions',payload);if(auth!==this.authGeneration)return;this.stockPrepDialog=null;await this.loadProduction();this.showToast('已撤销组装，子件恢复库存');}
     catch(e){if(auth===this.authGeneration)this.showToast(this.errorMessage(e),true);}finally{if(auth===this.authGeneration)this.stockPrepBusy=false;}
    },
    async planStockImmediately(row){
     if(!this.canAdmin||this.stockPrepBusy)return;
     if(row.entry_type!=='kit'){row._quantity=row.available;row._location=null;return this.stockPrepAction(row,'plan');}
     const auth=this.authGeneration;this.stockPrepBusy=true;this.stockPrepError='';
     try{
      const {data:preview}=await axios.get('/api/production/stock-preparation/groups/'+row.plan.recipe.parent_id+'/preview',{params:{sets:row.plan.available_sets}});
      if(auth!==this.authGeneration)return;
      if(preview.shortages.length||!preview.inputs.length)throw new Error('材料已变化，请刷新后重试');
      const payload={action:'plan',parent_id:row.plan.recipe.parent_id,sets:preview.sets,basis_hash:preview.basis_hash};
      const signature=JSON.stringify(payload);if(row._planAttempt?.signature!==signature)row._planAttempt={signature,key:this.stockOperationKey()};
      payload.operation_key=row._planAttempt.key;
      await axios.post('/api/production/stock-preparation/group-actions',payload);
      if(auth!==this.authGeneration)return;
      await this.loadStockPreparation(this.stockPrepPage);this.showToast('已转待生产');
     }catch(e){if(auth===this.authGeneration)this.stockPrepError=this.errorMessage(e);}finally{if(auth===this.authGeneration)this.stockPrepBusy=false;}
    },
    stockOperationKey(){const bytes=new Uint8Array(16);global.crypto.getRandomValues(bytes);return 'group-'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');},
    async openStockLocationMap(target=null){const d=this.stockPrepDialog;if(!d||d.loading||(d.row.entry_type==='single_job'&&this.stockCompletionLocked(d.row))||!this.canAdmin||!this.hasPermission('warehouse.view'))return;d.error='';
     try{if(!await this.ensureStockLocations({force:true,completion:d.row.entry_type==='single_job'})||this.stockPrepDialog!==d)return;
      const token=this.stockOperationKey();const params=new URLSearchParams({embedded:'1',readonly:'1',source:'production-location-picker',tab:'map',floor:'3F',mode:'lookup',view:'2d',picker_token:token});
      this.stockLocationMap={token,target,dialog:d,auth:this.authGeneration,url:'/warehouse.html?'+params};
     }catch(e){d.error=this.errorMessage(e);}
    },
    closeStockLocationMap(){this.stockLocationMap=null;},
    async acceptStockLocation(event){const picker=this.stockLocationMap;
     if(!picker||event.origin!==global.location.origin||event.source!==this.$refs.stockLocationFrame?.contentWindow||event.data?.type!=='erp-production-location'||event.data.token!==picker.token||picker.auth!==this.authGeneration||picker.dialog!==this.stockPrepDialog||!this.canAdmin||!this.hasPermission('warehouse.view'))return;
     const id=Number(event.data.location_id);if(!Number.isInteger(id)||id<=0)return;
     const d=this.stockPrepDialog;d.error='';
     if(!await this.ensureStockLocations({force:true,completion:d.row.entry_type==='single_job'})){if(picker.auth===this.authGeneration&&this.stockPrepDialog===d)d.error='货位核对失败，请重试';return;}
     if(this.stockLocationMap!==picker||this.stockPrepDialog!==d||picker.auth!==this.authGeneration)return;
     const location=this.stockLocations.find(l=>Number(l.id)===id);
     if(!location){d.error='此货位当前不可存放，请重新选位';this.closeStockLocationMap();return;}
     if(picker.target)picker.target._location=Number(location.id);else d.location=Number(location.id);this.closeStockLocationMap();
    },
    async selectStockProductionTab(tab){this.productionTab=tab;this.stockPrepState=tab==='pending'?'pending':tab==='stock'?'materials':'arrange';await this.loadStockPreparation(1);if(tab==='pending'){await this.loadProductionPage(1);if(!['orders','stock','all'].includes(this.productionPendingSource))this.productionPendingSource='all';}},
    async loadStockWorkspace(page=1,{completion=false}={}){this.reloadStockCompletionRecovery();const sequence=++this.stockPrepSequence,auth=this.authGeneration,actor=this.user?.id;this.stockPrepBusy=true;this.stockPrepRows=[];this.stockPrepError='';this.stockPrepPage=page;
     try {const {data}=await (completion?this.stockCompletionHttp():axios).get('/api/production/stock-preparation',{params:{workspace:true,q:this.stockPrepQuery,state:this.stockPrepState,page,page_size:this.screenPageSize(12)}});
      if(sequence!==this.stockPrepSequence||auth!==this.authGeneration)return;
      this.stockPrepRows=data.items;this.stockPrepTotal=data.total;this.stockPrepCounts=data.counts;this.stockPrepPendingCount=data.workspace_pending_count||0;
     }catch(e){if(sequence===this.stockPrepSequence&&auth===this.authGeneration){this.stockPrepError=this.errorMessage(e);if(completion)this.stockCompletionAuthError(e,{actor,auth});}}finally{if(sequence===this.stockPrepSequence&&auth===this.authGeneration)this.stockPrepBusy=false;}
    },
    stockEntryName(row){if(row.entry_type==='assembled_stock')return row.assembly.recipe;return row.entry_type==='kit'?row.plan.recipe:['group_job','group_stock'].includes(row.entry_type)?row.task.group.recipe:row;},
    stockEntryStatus(row){
     if(row.entry_type==='assembled_stock')return '成套成品';
     if(row.entry_type==='group_stock')return row.task.jobs.every(j=>j.output_kind==='semi')?'半成品分存':'子件配套库存';
     if(row.entry_type==='kit')return '整组待安排';
     if(['group_job','single_job'].includes(row.entry_type))return '待生产';
     if(row.physical>0&&row.jobs?.some(j=>j.status==='completed'))return '生产余料';
     if(row.source_kind==='legacy_stock'&&['stock','keep'].includes(row.status))return '已入库·备库库存';
     return this.stockPrepLabels()[row.status];
    },
    stockDestinationUrl(place){return '/warehouse.html?'+new URLSearchParams({readonly:'1',source:'order-context',tab:'map',floor:String(place.floor)+'F',mode:'lookup',view:'2d',inventory_type:place.inventory_type,location_id:String(place.location_id),lot_id:String(place.lot_id)}).toString();},
    stockOutputLabel(row){const jobs=(row.jobs||[]).filter(j=>j.output_remaining>0&&(!row.grouped_output_hidden||!j.product.preparation_group));return jobs.length&&jobs.every(j=>j.output_kind==='semi')?'半成品':'成品';},
    stockOutputQuantity(row){return (row.jobs||[]).filter(j=>!row.grouped_output_hidden||!j.product.preparation_group).reduce((sum,j)=>sum+Number(j.output_remaining||0),0);},
    stockOutputLocations(row){return [...new Set((row.jobs||[]).filter(j=>j.output_remaining>0&&(!row.grouped_output_hidden||!j.product.preparation_group)).map(j=>j.output_location).filter(Boolean))].join(' / ');},
    async openPreparationHistory(row){
     if(this.productionBusy)return;const auth=this.authGeneration;this.productionBusy=true;
     try{const {data}=await axios.get('/api/production/stock-preparation/history/'+encodeURIComponent(row.preparation_key));
      if(auth!==this.authGeneration)return;
      const sources=data.items||[];if(!sources.length)throw new Error('来源记录不存在');
      const trace={...sources[0],entry_type:'receipt',jobs:sources.flatMap(r=>r.jobs||[]),movements:sources.flatMap(r=>r.movements||[]),history:sources.flatMap(r=>r.history||[])};
      const opening=this.openStockDialog(trace),dialog=this.stockPrepDialog;await opening;
      if(auth!==this.authGeneration||this.stockPrepDialog!==dialog)return;
      dialog.view='history';dialog.sources=sources;
     }catch(e){if(auth===this.authGeneration)this.showToast('来源读取失败：'+this.errorMessage(e),true);}finally{if(auth===this.authGeneration)this.productionBusy=false;}
    },
    async reversePreparationHistory(row){
     if(this.productionBusy||this.user?.role!=='admin')return;
     if(!window.confirm('仅用于纠正误报：确认这批产出尚未组装、送货或使用？撤销将整组撤回产出，恢复原投入材料到待安排，并保留全部记录。'))return;
     const payload={confirm_unused:true,jobs:row.reverse_versions};const signature=JSON.stringify(payload);
     if(row._reverseAttempt?.signature!==signature)row._reverseAttempt={signature,key:this.stockOperationKey()};
     payload.operation_key=row._reverseAttempt.key;const auth=this.authGeneration;this.productionBusy=true;
     try{await axios.post('/api/production/stock-preparation/completions/'+encodeURIComponent(row.preparation_key)+'/revert',payload);if(auth!==this.authGeneration)return;await this.loadProductionHistory();this.showToast('备库误报已撤销，投入材料恢复待安排');}
     catch(e){if(auth===this.authGeneration)this.showToast('撤销失败：'+this.errorMessage(e),true);}finally{if(auth===this.authGeneration)this.productionBusy=false;}
    },
    async openStockDialog(row,{completion=false}={}){this.reloadStockCompletionRecovery();this.stockPrepError='';this.stockPrepDialog={row,completionActor:row.entry_type==='single_job'||completion?this.user?.id:null,loading:true,error:'',sets:row.task?.remaining_sets||row.task?.group?.sets||row.plan?.available_sets||1,disposition:row.entry_type==='group_stock'&&row.task?.jobs?.some(j=>j.output_kind!=='semi')?'finished':'semi',keepKind:row.keep||'keep_raw',location:null,preview:null,jobs:[],job:row.job?{...row.job,_actual:row.job.expected_output}:null,inputQuantity:row.job?.input_quantity||row.available||0,actualOutput:row.processing_expected_output||0,quantity:row.available||0,view:'action',page:1,sources:[row]};
     const dialog=this.stockPrepDialog;
     try {await this.ensureStockLocations({completion:completion||row.entry_type==='single_job'});if(this.stockPrepDialog!==dialog)return;dialog.location=this.stockLastLocation();
      if(row.entry_type==='kit')await this.previewStockGroup();
      if(['group_job','group_stock'].includes(row.entry_type)){dialog.location=dialog.location||row.task.group.planned_location?.id||null;dialog.jobs=row.task.jobs.map(j=>({...j,_actual:j.expected_output,_location:null}));}
      if(['single_job','single_output'].includes(row.entry_type))dialog.location=row.job.output_location_id||dialog.location||row.job.product.planned_location?.id||null;
      this.restoreStockCompletionDialog(dialog);
     }catch(e){if(this.stockPrepDialog===dialog)dialog.error=this.errorMessage(e);}finally{dialog.loading=false;if(this.stockPrepDialog===dialog)this.$nextTick(()=>document.querySelector('.stock-production-dialog .toolbar button')?.focus());}
    },
    stockProcessingExpected(row,job,input){const factor=Number(job?.product?.factor??row.processing_yield_per_sheet),pieces=Number(job?.product?.pieces_per_box??row.processing_pieces_per_product);return Number.isInteger(factor)&&factor>0&&Number.isInteger(pieces)&&pieces>0?Math.floor(Number(input)*factor/pieces):null;},
    updateStockProcessingQuantity(){const d=this.stockPrepDialog;if(!d||(d.row.entry_type==='single_job'&&this.stockCompletionLocked(d.row)))return;const count=this.stockProcessingExpected(d.row,d.job,d.inputQuantity);if(count===null)return;if(d.job)d.job._actual=count;else d.actualOutput=count;},
    async openStockAssembly(row){if(!this.canAdmin||!row.parent_product_id)return;const d={row,sets:Math.max(1,Number(row.verified_available_sets||1)),location:null,preview:null,loading:true,saving:false,error:'',sequence:0};this.stockAssemblyDialog=d;const auth=this.authGeneration;try{await this.ensureStockLocations();if(this.stockAssemblyDialog!==d||auth!==this.authGeneration)return;d.location=this.stockLastLocation();await this.previewStockAssembly();}catch(e){if(this.stockAssemblyDialog===d)d.error=this.errorMessage(e);}finally{d.loading=false;}},
    async previewStockAssembly(){const d=this.stockAssemblyDialog;if(!d||d.saving)return;const sequence=++d.sequence,auth=this.authGeneration;d.preview=null;d.error='';if(!Number.isInteger(Number(d.sets))||Number(d.sets)<=0){d.error='请填写实际组套数量';return;}d.loading=true;try{const {data}=await axios.get('/api/production/stock-preparation/assembly/'+d.row.parent_product_id+'/preview',{params:{sets:Number(d.sets)}});if(this.stockAssemblyDialog===d&&sequence===d.sequence&&auth===this.authGeneration)d.preview=data;}catch(e){if(this.stockAssemblyDialog===d&&sequence===d.sequence&&auth===this.authGeneration)d.error=this.errorMessage(e);}finally{if(sequence===d.sequence)d.loading=false;}},
    async saveStockAssembly(){const d=this.stockAssemblyDialog;if(!d||d.loading||d.saving||!this.canAdmin)return;const p=d.preview,location=this.stockLocations.find(l=>l.id===d.location);if(!p?.basis_hash||Number(p.sets)!==Number(d.sets)||p.shortages?.length||!location){d.error='请核对实际组套数量、可用子件及成套货位';return;}const payload={action:'assemble_stock',parent_id:d.row.parent_product_id,sets:Number(d.sets),basis_hash:p.basis_hash,jobs:p.sources.map(s=>({job_id:s.job_id,job_version:s.job_version,output_version:s.output_version,lot_version:s.lot_version||0,lot_id:s.lot_id,quantity:s.quantity})),location_id:d.location,layout_version:location.layout_version};const signature=JSON.stringify(payload);if(d.attempt?.signature!==signature)d.attempt={signature,key:this.stockOperationKey()};payload.operation_key=d.attempt.key;d.saving=true;d.error='';const auth=this.authGeneration;try{await axios.post('/api/production/stock-preparation/group-actions',payload);if(auth!==this.authGeneration)return;if(this.stockAssemblyDialog===d)this.stockAssemblyDialog=null;this.rememberStockLocation(d.location);try{await this.loadPendingAssemblies(this.assemblyPage);if(auth!==this.authGeneration)return;this.showToast(this.assemblyError?'组套已保存；列表刷新失败，请刷新核对，不要重复组套':'组套已入库，余下子件保留原货位',!!this.assemblyError);}catch(e){this.showToast('组套已保存；列表刷新失败，请刷新核对，不要重复组套',true);}}catch(e){if(auth===this.authGeneration)d.error=this.errorMessage(e);}finally{d.saving=false;}},
    async previewStockGroup(){const d=this.stockPrepDialog;if(!d||!Number.isInteger(Number(d.sets))||d.sets<=0)return;const sequence=++this.stockDialogSequence;d.loading=true;d.error='';d.preview=null;
     try {const {data}=await axios.get('/api/production/stock-preparation/groups/'+d.row.plan.recipe.parent_id+'/preview',{params:{sets:d.sets}});if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.preview=data;}
     catch(e){if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.error=this.errorMessage(e);}finally{if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.loading=false;}
    },
    async saveStockDialog(action){const d=this.stockPrepDialog;if(!d||d.loading)return;d.error='';if(this.stockPrepBusy){d.error='列表正在刷新，请稍后重试';return;}
     if((action==='complete'||action==='process'||action==='store_output'||action==='assemble'||(action==='dispose'&&d.disposition==='finished')||action.startsWith('keep_'))&&!d.location){d.error='请选择实际存放货位';return;}
     if(!['kit','group_job','group_stock'].includes(d.row.entry_type)){
       const row=d.row;row._quantity=Number(d.quantity);row._actualInput=Number(d.inputQuantity);row._actualOutput=Number(d.actualOutput);row._location=d.location;row._layoutVersion=this.stockLocations.find(l=>l.id===d.location)?.layout_version;row._outputKind=d.disposition==='semi'?'semi':'finished';row._outputVersion=d.job?.output_version||0;const job=d.job?{...d.job,_location:d.location}:null;
      const saved=await this.stockPrepAction(row,action,job);
       if(saved&&this.stockPrepDialog===d){this.rememberStockLocation(d.location);this.stockPrepDialog=null;}else if(this.stockPrepError)d.error=this.stockPrepError;return;
     }
     if(action==='plan'&&(!d.preview||d.preview.sets!==Number(d.sets)||d.preview.shortages.length)){d.error='请先预览有效套数并补齐子件';return;}
     if(action==='cancel'&&!window.confirm('取消整组生产安排，并释放所有子件材料？'))return;
     const group=d.row.task?.group;
     const payload={action,disposition:d.disposition,parent_id:group?.recipe.parent_id||d.row.plan.recipe.parent_id,sets:Number(d.sets),basis_hash:d.preview?.basis_hash||'',group_key:group?.key||'',location_id:d.location,layout_version:this.stockLocations.find(l=>l.id===d.location)?.layout_version??null,confirm_overproduction:false,
      jobs:(d.jobs||[]).map(j=>({job_id:j.id,job_version:j.version,lot_version:j.lot_version,actual_output:Number(j._actual),output_version:j.output_version||0,location_id:j._location||d.location,layout_version:this.stockLocations.find(l=>l.id===(j._location||d.location))?.layout_version??null}))};
     if(['complete','dispose'].includes(action)&&payload.jobs.some(j=>!Number.isInteger(j.actual_output)||j.actual_output<=0)){d.error='请逐款填写实际产出正整数';return;}
     if(['complete','dispose'].includes(action)&&d.jobs.some(j=>Number(j._actual)>j.expected_output)){if(!window.confirm('有子件超过理论产出，确认已核对实际数量？'))return;payload.confirm_overproduction=true;}
     const auth=this.authGeneration;d.loading=true;
     try{const signature=JSON.stringify(payload);if(d.attempt?.signature!==signature)d.attempt={signature,key:this.stockOperationKey()};payload.operation_key=d.attempt.key;await axios.post('/api/production/stock-preparation/group-actions',payload);if(auth!==this.authGeneration)return;if(this.stockPrepDialog===d)this.stockPrepDialog=null;this.rememberStockLocation(d.location);await this.loadStockPreparation(this.stockPrepPage);this.showToast(action==='plan'?'整组已转待生产':['dispose','assemble'].includes(action)?(d.disposition==='semi'?'半成品已分存':'成套成品已入库'):action==='store_outputs'?'半成品位置已保存':action==='complete'?'已入库':'整组安排已取消');}catch(e){if(auth===this.authGeneration)d.error=this.errorMessage(e);}finally{d.loading=false;}
    },
   },
  });
 }
 global.ERPProductionWorkspace={install};
})(typeof window==='undefined'?globalThis:window);
