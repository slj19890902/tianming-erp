(function(global){
 'use strict';
 function install(app){
  app.mixin({
   data(){return this.$parent?{}:{financeExpensePane:'list',financeProcessingPane:0,financeOverviewChartPane:0,financeReportPane:'year',financeOverviewPane:'todo',workspaceLocalPages:{},workspaceLocalSizes:{}};},
   methods:{
    localPage(rows,key){return Math.min(this.workspaceLocalPages[key]||1,Math.max(1,Math.ceil((rows||[]).length/this.localSize(key))));},
    localSize(key){return this.workspaceLocalSizes[key]|| (this.uiMode==='large'?3:5);},
    localRows(rows,key){const page=this.localPage(rows,key),size=this.localSize(key);return (rows||[]).slice((page-1)*size,page*size);},
    setExpensePane(pane){this.financeExpensePane=pane;if(pane==='processing'&&!this.financeProcessingOpen)this.toggleFinanceProcessingPanel();},
   },
  });
  app.component('local-pager',{
   props:{rows:{type:Array,default:()=>[]},listKey:{type:String,required:true}},
   template:`<pager :page="$root.localPage(rows,listKey)" :total="rows.length" :page-size="$root.localSize(listKey)" @change="$root.workspaceLocalPages[listKey]=$event"></pager>`,
  });
  app.component('workspace-fold',{
   data:()=>({open:false}),props:{title:{type:String,default:'详情'}},
   watch:{open(value){if(value)this.$nextTick(()=>[...document.querySelectorAll('.workspace-dialog-close')].at(-1)?.focus());}},
   updated(){if(this.open)this.$nextTick(()=>this.$root.measureLocalTables());},
   methods:{close(){this.open=false;this.$nextTick(()=>this.$refs.trigger?.focus());}},
   template:`<div class="workspace-fold"><button ref="trigger" class="workspace-fold-trigger" type="button" :aria-expanded="open" @click="open=true"><slot name="summary">{{title}}</slot></button><teleport to="body"><div v-if="open" class="workspace-dialog-mask" @click.self="close" @keydown.esc.stop="close"><section class="workspace-dialog" role="dialog" aria-modal="true" :aria-label="title"><div class="toolbar"><strong>{{title}}</strong><button type="button" class="btn small workspace-dialog-close" @click="close">关闭</button></div><slot></slot></section></div></teleport></div>`,
  });
 }
 global.ERPFinanceWorkspace={install};
})(typeof window==='undefined'?globalThis:window);
