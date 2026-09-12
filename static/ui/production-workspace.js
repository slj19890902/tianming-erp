(function(global){
 'use strict';
 function install(app){
  app.component('location-picker',{
   props:{modelValue:[Number,String],locations:{type:Array,default:()=>[]},disabled:Boolean},emits:['update:modelValue'],
   data:()=>({floor:'',major:'',area:''}),
   computed:{
    valid(){return this.locations.filter(l=>l.is_active!==false && l.placement_status!=='unplaced');},
    floors(){return [...new Set(this.valid.map(l=>String(l.warehouse_floor)))].sort((a,b)=>Number(a)-Number(b));},
    majors(){return [...new Set(this.valid.filter(l=>String(l.warehouse_floor)===this.floor).map(l=>this.group(l)))].sort();},
    areas(){return [...new Map(this.valid.filter(l=>String(l.warehouse_floor)===this.floor && this.group(l)===this.major).map(l=>[l.area_code,{code:l.area_code,name:l.area_name||l.area_code}])).values()];},
    places(){return this.valid.filter(l=>String(l.warehouse_floor)===this.floor && this.group(l)===this.major && l.area_code===this.area);},
   },
   methods:{
    group(l){return l.area_group_name || l.major_area_name || String(l.area_code||'').match(/^[A-Za-z]+/)?.[0] || l.area_name || l.area_code || '区域';},
    sync(){const row=this.valid.find(l=>Number(l.id)===Number(this.modelValue));if(row){this.floor=String(row.warehouse_floor);this.major=this.group(row);this.area=row.area_code;}},
    change(level){if(level==='floor'){this.major='';this.area='';if(this.majors.length===1)this.major=this.majors[0];}if(level==='major')this.area='';if(this.areas.length===1)this.area=this.areas[0].code;this.$emit('update:modelValue',null);},
   },
   watch:{modelValue:{immediate:true,handler(){this.sync();}},locations(){this.sync();}},
   template:`<div class="location-picker"><select class="select" v-model="floor" :disabled="disabled" @change="change('floor')"><option value="">楼层</option><option v-for="f in floors" :key="f" :value="f">{{f}} 楼</option></select><select class="select" v-model="major" :disabled="disabled||!floor" @change="change('major')"><option value="">大区</option><option v-for="m in majors" :key="m">{{m}}</option></select><select class="select" v-model="area" :disabled="disabled||!major" @change="$emit('update:modelValue',null)"><option value="">小区 / 货架</option><option v-for="a in areas" :key="a.code" :value="a.code">{{a.name}}</option></select><select class="select" :value="modelValue||''" :disabled="disabled||!area" @change="$emit('update:modelValue',Number($event.target.value)||null)"><option value="">具体货位</option><option v-for="l in places" :key="l.id" :value="l.id">{{l.location_name}}</option></select></div>`,
  });
  app.mixin({
   data(){return this.$parent?{}:{stockPrepDialog:null,stockPrepPendingCount:0,productionPendingSource:'stock',stockDialogSequence:0};},
   methods:{
    async selectStockProductionTab(tab){this.productionTab=tab;this.stockPrepState=tab==='pending'?'pending':tab==='stock'?'stock':'';await this.loadStockPreparation(1);if(tab==='pending'){await this.loadProductionPage(1);this.productionPendingSource=this.stockPrepPendingCount?'stock':this.productionPendingTotal?'orders':'stock';}},
    async loadStockWorkspace(page=1){const sequence=++this.stockPrepSequence,auth=this.authGeneration;this.stockPrepBusy=true;this.stockPrepRows=[];this.stockPrepError='';this.stockPrepPage=page;
     try {const {data}=await axios.get('/api/production/stock-preparation',{params:{workspace:true,q:this.stockPrepQuery,state:this.stockPrepState,page,page_size:this.screenPageSize(12)}});
      if(sequence!==this.stockPrepSequence||auth!==this.authGeneration)return;
      this.stockPrepRows=data.items;this.stockPrepTotal=data.total;this.stockPrepCounts=data.counts;this.stockPrepPendingCount=data.workspace_pending_count||0;
     }catch(e){if(sequence===this.stockPrepSequence&&auth===this.authGeneration)this.stockPrepError=this.errorMessage(e);}finally{if(sequence===this.stockPrepSequence)this.stockPrepBusy=false;}
    },
    stockEntryName(row){return row.entry_type==='kit'?row.plan.recipe:row.entry_type==='group_job'?row.task.group.recipe:row;},
    stockEntryStatus(row){return row.entry_type==='kit'?'整组待安排':row.entry_type==='group_job'||row.entry_type==='single_job'?'待生产':this.stockPrepLabels()[row.status];},
    stockOutputQuantity(row){return (row.jobs||[]).reduce((sum,j)=>sum+Number(j.output_remaining||0),0);},
    stockOutputLocations(row){return [...new Set((row.jobs||[]).filter(j=>j.output_remaining>0).map(j=>j.output_location).filter(Boolean))].join(' / ');},
    async openStockDialog(row){this.stockPrepError='';this.stockPrepDialog={row,loading:true,error:'',sets:row.plan?.available_sets||1,location:null,preview:null,job:row.job?{...row.job,_actual:row.job.expected_output}:null,quantity:row.available||0,view:'action',page:1};
     const dialog=this.stockPrepDialog;
     try {await this.ensureProductionLocations();if(this.stockPrepDialog!==dialog)return;
      if(row.entry_type==='kit')await this.previewStockGroup();
      if(row.entry_type==='group_job'){dialog.location=row.task.group.planned_location.id;dialog.jobs=row.task.jobs.map(j=>({...j,_actual:j.expected_output}));}
      if(row.entry_type==='single_job')dialog.location=row.job.product.planned_location?.id||null;
     }catch(e){dialog.error=this.errorMessage(e);}finally{dialog.loading=false;this.$nextTick(()=>document.querySelector('.stock-production-dialog .toolbar button')?.focus());}
    },
    async previewStockGroup(){const d=this.stockPrepDialog;if(!d||!Number.isInteger(Number(d.sets))||d.sets<=0)return;const sequence=++this.stockDialogSequence;d.loading=true;d.error='';d.preview=null;
     try {const {data}=await axios.get('/api/production/stock-preparation/groups/'+d.row.plan.recipe.parent_id+'/preview',{params:{sets:d.sets}});if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.preview=data;}
     catch(e){if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.error=this.errorMessage(e);}finally{if(this.stockPrepDialog===d&&sequence===this.stockDialogSequence)d.loading=false;}
    },
    async saveStockDialog(action){const d=this.stockPrepDialog;if(!d||d.loading||this.stockPrepBusy)return;d.error='';
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
     const signature=JSON.stringify(payload);if(d.attempt?.signature!==signature)d.attempt={signature,key:'group-'+crypto.randomUUID()};payload.operation_key=d.attempt.key;d.loading=true;
     const auth=this.authGeneration;
     try{await axios.post('/api/production/stock-preparation/group-actions',payload);if(auth!==this.authGeneration)return;if(this.stockPrepDialog===d)this.stockPrepDialog=null;await this.loadStockPreparation(this.stockPrepPage);this.showToast(action==='plan'?'整组已转待生产':action==='complete'?'整组已入库，子件分别留账':'整组安排已取消');}catch(e){if(auth===this.authGeneration)d.error=this.errorMessage(e);}finally{d.loading=false;}
    },
   },
  });
 }
 global.ERPProductionWorkspace={install};
})(typeof window==='undefined'?globalThis:window);
