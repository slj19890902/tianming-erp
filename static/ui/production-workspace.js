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
   data(){return this.$parent?{}:{stockPrepDialog:null,stockPrepPendingCount:0,productionPendingSource:'stock',stockDialogSequence:0,stockLocationMap:null};},
   methods:{
    stockOperationKey(){const bytes=new Uint8Array(16);global.crypto.getRandomValues(bytes);return 'group-'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');},
    async openStockLocationMap(){const d=this.stockPrepDialog;if(!d||d.loading||!this.canAdmin||!this.hasPermission('warehouse.view'))return;d.error='';
     try{if(!await this.ensureProductionLocations({force:true})||this.stockPrepDialog!==d)return;
      const token=this.stockOperationKey();const params=new URLSearchParams({embedded:'1',readonly:'1',source:'production-location-picker',tab:'map',floor:'3F',mode:'lookup',view:'2d',picker_token:token});
      this.stockLocationMap={token,dialog:d,auth:this.authGeneration,url:'/warehouse.html?'+params};
     }catch(e){d.error=this.errorMessage(e);}
    },
    closeStockLocationMap(){this.stockLocationMap=null;},
    async acceptStockLocation(event){const picker=this.stockLocationMap;
     if(!picker||event.origin!==global.location.origin||event.source!==this.$refs.stockLocationFrame?.contentWindow||event.data?.type!=='erp-production-location'||event.data.token!==picker.token||picker.auth!==this.authGeneration||picker.dialog!==this.stockPrepDialog||!this.canAdmin||!this.hasPermission('warehouse.view'))return;
     const id=Number(event.data.location_id);if(!Number.isInteger(id)||id<=0)return;
     const d=this.stockPrepDialog;d.error='';
     if(!await this.ensureProductionLocations({force:true})){d.error='货位核对失败，请重试';return;}
     if(this.stockLocationMap!==picker||this.stockPrepDialog!==d||picker.auth!==this.authGeneration)return;
     const location=this.productionLocations.find(l=>Number(l.id)===id);
     if(!location){d.error='此位置当前不可用于成品存放，请重新选位';this.closeStockLocationMap();return;}
     d.location=Number(location.id);this.closeStockLocationMap();
    },
    async selectStockProductionTab(tab){this.productionTab=tab;this.stockPrepState=tab==='pending'?'pending':tab==='stock'?'stock':'';await this.loadStockPreparation(1);if(tab==='pending'){await this.loadProductionPage(1);this.productionPendingSource=this.stockPrepPendingCount?'stock':this.productionPendingTotal?'orders':'stock';}},
    async loadStockWorkspace(page=1){const sequence=++this.stockPrepSequence,auth=this.authGeneration;this.stockPrepBusy=true;this.stockPrepRows=[];this.stockPrepError='';this.stockPrepPage=page;
     try {const {data}=await axios.get('/api/production/stock-preparation',{params:{workspace:true,q:this.stockPrepQuery,state:this.stockPrepState,page,page_size:this.screenPageSize(12)}});
      if(sequence!==this.stockPrepSequence||auth!==this.authGeneration)return;
      this.stockPrepRows=data.items;this.stockPrepTotal=data.total;this.stockPrepCounts=data.counts;this.stockPrepPendingCount=data.workspace_pending_count||0;
     }catch(e){if(sequence===this.stockPrepSequence&&auth===this.authGeneration)this.stockPrepError=this.errorMessage(e);}finally{if(sequence===this.stockPrepSequence)this.stockPrepBusy=false;}
    },
    stockEntryName(row){return row.entry_type==='kit'?row.plan.recipe:['group_job','group_stock'].includes(row.entry_type)?row.task.group.recipe:row;},
    stockEntryStatus(row){if(row.entry_type==='group_stock')return '子件配套库存';if(row.physical>0&&row.jobs?.some(j=>j.status==='completed'))return '生产余料';if(row.source_kind==='legacy_stock')return '已入库·备库库存';return row.entry_type==='kit'?'整组待安排':row.entry_type==='group_job'||row.entry_type==='single_job'?'待生产':this.stockPrepLabels()[row.status];},
    stockOutputQuantity(row){return (row.jobs||[]).filter(j=>!row.grouped_output_hidden||!j.product.preparation_group).reduce((sum,j)=>sum+Number(j.output_remaining||0),0);},
    stockOutputLocations(row){return [...new Set((row.jobs||[]).filter(j=>j.output_remaining>0&&(!row.grouped_output_hidden||!j.product.preparation_group)).map(j=>j.output_location).filter(Boolean))].join(' / ');},
    async openStockDialog(row){this.stockPrepError='';this.stockPrepDialog={row,loading:true,error:'',sets:row.plan?.available_sets||1,location:null,preview:null,job:row.job?{...row.job,_actual:row.job.expected_output}:null,quantity:row.available||0,view:'action',page:1};
     const dialog=this.stockPrepDialog;
     try {await this.ensureProductionLocations();if(this.stockPrepDialog!==dialog)return;
      if(row.entry_type==='kit')await this.previewStockGroup();
      if(['group_job','group_stock'].includes(row.entry_type)){dialog.location=row.task.group.planned_location.id;dialog.jobs=row.task.jobs.map(j=>({...j,_actual:j.expected_output}));}
      if(row.entry_type==='single_job')dialog.location=row.job.product.planned_location?.id||null;
     }catch(e){dialog.error=this.errorMessage(e);}finally{dialog.loading=false;this.$nextTick(()=>document.querySelector('.stock-production-dialog .toolbar button')?.focus());}
    },
    async previewStockGroup(){const d=this.stockPrepDialog;if(!d||!Number.isInteger(Number(d.sets))||d.sets<=0)return;const sequence=++this.stockDialogSequence;d.loading=true;d.error='';d.preview=null;
     try {const {data}=await axios.get('/api/production/stock-preparation/groups/'+d.row.plan.recipe.parent_id+'/preview',{params:{sets:d.sets}});if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.preview=data;}
     catch(e){if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.error=this.errorMessage(e);}finally{if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.loading=false;}
    },
    async saveStockDialog(action){const d=this.stockPrepDialog;if(!d||d.loading)return;d.error='';if(this.stockPrepBusy){d.error='列表正在刷新，请稍后重试';return;}
     if(action!=='cancel'&&!action.startsWith('keep_')&&!d.location){d.error='请选择成品位置';return;}
     if(d.row.entry_type!=='kit'&&d.row.entry_type!=='group_job'){
      const row=d.row;row._quantity=Number(d.quantity);row._location=d.location;const job=d.job?{...d.job,_location:d.location}:null;
      const saved=await this.stockPrepAction(row,action,job);
      if(this.stockPrepError)d.error=this.stockPrepError;else if(saved&&this.stockPrepDialog===d)this.stockPrepDialog=null;return;
     }
     if(action==='plan'&&(!d.preview||d.preview.sets!==Number(d.sets)||d.preview.shortages.length)){d.error='请先预览有效套数并补齐子件';return;}
     if(action==='cancel'&&!window.confirm('取消整组生产安排，并释放所有子件材料？'))return;
     const group=d.row.task?.group;
     const payload={action,parent_id:group?.recipe.parent_id||d.row.plan.recipe.parent_id,sets:Number(d.sets),basis_hash:d.preview?.basis_hash||'',group_key:group?.key||'',location_id:d.location,layout_version:this.productionLocations.find(l=>l.id===d.location)?.layout_version??null,confirm_overproduction:false,
      jobs:(d.jobs||[]).map(j=>({job_id:j.id,job_version:j.version,lot_version:j.lot_version,actual_output:Number(j._actual)}))};
     if(action==='complete'&&payload.jobs.some(j=>!Number.isInteger(j.actual_output)||j.actual_output<=0)){d.error='请逐款填写实际产出正整数';return;}
     if(action==='complete'&&d.jobs.some(j=>Number(j._actual)>j.expected_output)){if(!window.confirm('有子件超过理论产出，确认已核对实际数量？'))return;payload.confirm_overproduction=true;}
     const auth=this.authGeneration;d.loading=true;
     try{const signature=JSON.stringify(payload);if(d.attempt?.signature!==signature)d.attempt={signature,key:this.stockOperationKey()};payload.operation_key=d.attempt.key;await axios.post('/api/production/stock-preparation/group-actions',payload);if(auth!==this.authGeneration)return;if(this.stockPrepDialog===d)this.stockPrepDialog=null;await this.loadStockPreparation(this.stockPrepPage);this.showToast(action==='plan'?'整组已转待生产':action==='complete'?'整组已入库，子件分别留账':'整组安排已取消');}catch(e){if(auth===this.authGeneration)d.error=this.errorMessage(e);}finally{d.loading=false;}
    },
   },
  });
 }
 global.ERPProductionWorkspace={install};
})(typeof window==='undefined'?globalThis:window);
