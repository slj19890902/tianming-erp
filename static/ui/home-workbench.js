(function (global) {
  'use strict';
  const text = value => String(value ?? '').trim().toLocaleLowerCase();
  const fields = ['customer_label','customer_name','product_code','product_name','customer_po','order_number','message'];
  function matches(row, customer, query) {
    return (!customer || Number(row.customer_id) === Number(customer) || (row.customer_ids || []).includes(Number(customer)))
      && (!text(query) || fields.some(key => text(row[key]).includes(text(query))));
  }
  function stockState(row) {
    if (row.pending_request_ids?.length) return 'approval';
    const needed = Number(row.is_virtual_composite_parent || row.procurement_mode === 'external_purchase'
      ? row.suggested_new_requisition_finished_quantity : row.suggested_new_requisition_sheet_quantity);
    if (needed > 0) return row.draft_ready ? 'new' : 'missing';
    if (row.replenishment_state === 'board_preparation_ready') return 'prepare';
    if (row.replenishment_state === 'already_ordered') return 'incoming';
    return row.draft_ready ? 'covered' : 'missing';
  }
  const actionable = row => ['new','missing','prepare'].includes(stockState(row));
  function customerGroups(rows) {
    const groups=new Map();
    for(const row of rows){
      const id=Number(row.customer_id);if(!groups.has(id))groups.set(id,{id,label:row.customer_label||row.customer_name,rows:[],action:0,arranged:0,approval:0});
      const group=groups.get(id);group.rows.push(row);
      group[stockState(row)==='approval'?'approval':actionable(row)?'action':'arranged']++;
    }
    return [...groups.values()].sort((a,b)=>b.action-a.action||b.rows.length-a.rows.length||a.id-b.id);
  }
  function fairTasks(rows) {
    const tiers=new Map();
    for(const row of rows){const rank=({overdue:0,today:1,approval:2})[row.urgency]??3;if(!tiers.has(rank))tiers.set(rank,new Map());const groups=tiers.get(rank),key=row.customer_id;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);}
    const result=[];for(const [,groups] of [...tiers].sort((a,b)=>a[0]-b[0])){let more=true;while(more){more=false;for(const group of groups.values()){if(group.length){result.push(group.shift());more=true;}}}}return result;
  }
  function attentionCategory(row){const s=row.attention_state||'active';return s==='hidden'?'hidden':s==='waiting_customer'?'customer':['stock_review','cancel_review','verify'].includes(s)?'review':s==='active'?'active':'deferred';}
  function pageRows(rows, requested, size) {
    const pages = Math.max(1, Math.ceil(rows.length / size));
    const page = Math.min(Math.max(1, Number(requested) || 1), pages);
    return {rows:rows.slice((page-1)*size, page*size), page, pages, total:rows.length};
  }
  function stageCards(cards, tasks, filtered) {
    return cards.filter(c => c.key !== 'warehouse_capacity').map(card => {
      if (!filtered) return card;
      const rows = tasks.filter(t => t.key === card.key);
      return {...card, count:new Set(rows.map(t=>t.settlement_identity|| (card.count_unit==='客户'?t.customer_id:t.id))).size,
        ...(card.amount!==undefined?{amount:rows.reduce((s,r)=>s+Number(r.amount||0),0)}:{})};
    });
  }
  const mixin = {
    data() { return this.$parent ? {} : {
      homeCustomer:'', homeQuery:'', homeStockTab:'action', homeTaskMode:'all',
      homeStockPage:1, homeTaskPage:1, homeExpandedPolicy:null, homeAnalyticsOpen:false,
      homeEntry:null, homeActionBusy:false, homeViewportHeight:global.innerHeight || 1080,
      homeStockCustomer:null, homeSearchSummary:false, homeAttentionTab:'active', homePreferenceForm:null,
      homePreferenceBusy:false, homePreferenceError:'', homeAdvice:null, homeAdviceBusy:false,
    }; },
    computed: {
      homeData() { return this.overview?.workbench || {tasks:[],customers:[]}; },
      homeCanStock() { return this.hasPermission('warehouse.view') && this.hasPermission('requisition.view'); },
      homeFilteredTasks() { return (this.homeData.tasks || []).filter(r => matches(r,this.homeCustomer,this.homeQuery)); },
      homeWarnings() { return (this.overview?.low_stock_warnings || []).filter(r => matches(r,this.homeCustomer,this.homeQuery)); },
      homeStockCounts() { const action=this.homeWarnings.filter(actionable).length; return {action,arranged:this.homeWarnings.length-action,all:this.homeWarnings.length}; },
      homeStockRows() { return this.homeWarnings.filter(r => this.homeStockTab==='all' || (this.homeStockTab==='action' ? actionable(r) : !actionable(r))); },
      homePageSize() { return this.homeViewportHeight<850 ? 2 : this.isLargeUi ? 3 : 4; },
      homeStockGroups() {return customerGroups(this.homeStockRows);},
      homeStockDetailCustomer() {return Number(this.homeStockCustomer||this.homeCustomer)||(this.homeQuery&&!this.homeSearchSummary&&this.homeStockGroups.length===1?this.homeStockGroups[0].id:null);},
      homeStockGroupPage() {return pageRows(this.homeStockGroups,this.homeStockPage,this.homePageSize);},
      homeStocksPage() { return pageRows(this.homeStockRows.filter(r=>Number(r.customer_id)===this.homeStockDetailCustomer),this.homeStockPage,this.homePageSize); },
      homeTodayOrderIds() { return new Set(this.homeFilteredTasks.filter(r => r.order_id && r.due_date===this.homeData.today).map(r=>r.order_id)); },
      homeTaskRows() { const rows=this.homeFilteredTasks.filter(r => (this.homeAttentionTab==='all'||attentionCategory(r)===this.homeAttentionTab)&&(this.homeTaskMode==='all'
        || (this.homeTaskMode==='today' && r.order_id && this.homeTodayOrderIds.has(r.order_id))
        || (this.homeTaskMode==='approval' && r.key==='approval')
        || (this.homeTaskMode==='receipt' && r.key==='pending_receipt')
        || r.key===this.homeTaskMode));return this.homeTaskMode==='all'?fairTasks(rows):rows; },
      homeAttentionCounts() {const counts={active:0,customer:0,deferred:0,review:0,hidden:0,all:this.homeFilteredTasks.length};for(const r of this.homeFilteredTasks)counts[attentionCategory(r)]++;return counts;},
      homeTasksPage() { return pageRows(this.homeTaskRows,this.homeTaskPage,this.homePageSize); },
      homeStages() { return stageCards(this.dashboardCards || [],this.homeFilteredTasks,!!this.homeCustomer || !!this.homeQuery); },
      homeReceiptCount() { return this.homeFilteredTasks.filter(t=>t.key==='pending_receipt').length; },
      homeApprovalCount() { return this.homeFilteredTasks.filter(t=>t.key==='approval').length; },
      homeTaskLabel() { return ({all:'优先处理',today:'今日交期事项',approval:this.homeData.can_review?'待我审批':'我的申请',receipt:'待回单'})[this.homeTaskMode] || this.homeStages.find(c=>c.key===this.homeTaskMode)?.title || '优先处理'; },
    },
    mounted() {if(this.$parent)return;this._homeResize=()=>{this.homeViewportHeight=global.innerHeight;};global.addEventListener('resize',this._homeResize);},
    beforeUnmount() {if(this._homeResize)global.removeEventListener('resize',this._homeResize);},
    watch: {
      homeCustomer() { this.homeResetPages(); }, homeQuery() { this.homeResetPages(); },
      homeStockTab() { this.homeStockPage=1; this.homeExpandedPolicy=null; },
      homeTaskMode() { this.homeTaskPage=1; },
      homeAttentionTab(){this.homeTaskPage=1;this.homePreferenceForm=null;},
      authGeneration() { if(this.$parent)return;this.homeCustomer='';this.homeQuery='';this.homeEntry=null;this.homeResetPages();this.homeAnalyticsOpen=false;this.homeAttentionTab='active';this.homePreferenceError='';this._homeReminderAttempt=null;this._homeAdviceRequest=null; },
      'user.id'(id) { if(this.$parent||!id)return;try{const saved=JSON.parse(sessionStorage.getItem(`erp-home-filter:${id}`)||'null');if(saved){this.homeCustomer=saved.customer||'';this.homeQuery=saved.query||'';sessionStorage.removeItem(`erp-home-filter:${id}`);}}catch(_){} },
      homeAnalyticsOpen(value) { this.dashboardDetailsVisible=value;this.$nextTick(()=>this.syncDesktopDeliveryMargin()); },
    },
    methods: {
      homeResetPages() { this.homeStockPage=1;this.homeTaskPage=1;this.homeExpandedPolicy=null;this.homeStockCustomer=null;this.homeSearchSummary=false;this.homeAdvice=null;this.homePreferenceForm=null; },
      homeChooseStockCustomer(id){this.homeStockCustomer=id;this.homeSearchSummary=!id;this.homeStockPage=1;this.homeExpandedPolicy=null;this.homeAdvice=null;},
      async homeLoadAdvice(row){
        const request={actor:this.user?.id,generation:this.authGeneration,policy:row.policy_id};
        this._homeAdviceRequest=request;this.homeAdviceBusy=true;this.homeAdvice={policy_id:row.policy_id};
        try{const response=await global.axios.get(`/api/dashboard/stock-advice/${Number(row.policy_id)}`);
          if(this._homeAdviceRequest===request&&request.actor===this.user?.id&&request.generation===this.authGeneration)this.homeAdvice=response.data;
        }catch(error){if(this._homeAdviceRequest===request)this.homeAdvice={policy_id:row.policy_id,error:typeof error.response?.data?.detail==='string'?error.response.data.detail:'分析依据暂未加载，请重试'};}
        finally{if(this._homeAdviceRequest===request)this.homeAdviceBusy=false;}
      },
      homeDateAfter(days){const d=new Date(`${this.homeData.today}T12:00:00`);d.setDate(d.getDate()+days);return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;},
      homeEditReminder(row){this.homePreferenceError='';this.homePreferenceForm={task_id:row.id,reason:row.attention_reason||'later',remind_on:this.homeDateAfter(3),scope:row.attention_state==='hidden'?'personal':this.homeData.can_manage_team_reminders?'team':'personal'};},
      homeReminderEffect(){return this.homeData.attention_reasons?.find(r=>r.code===this.homePreferenceForm?.reason)?.effect||'';},
      async homeSaveReminder(row,action){
        if(this.homePreferenceBusy||this.overviewError)return;
        const form=this.homePreferenceForm||{},reason=this.homeData.attention_reasons?.find(r=>r.code===form.reason);
        const scope=action==='hidden'?'personal':action==='active'?row.attention_scope:form.scope;
        const payload={task_id:row.id,source_hash:row.source_hash,scope,state:action||reason?.state,
          reason:action==='active'?'':form.reason,remind_on:action?null:form.remind_on,
          expected_version:row.attention_versions?.[scope]||0};
        const signature=JSON.stringify(payload);
        if(!this._homeReminderAttempt||this._homeReminderAttempt.signature!==signature)this._homeReminderAttempt={signature,key:global.crypto?.randomUUID?.()||`home-${Date.now()}-${Math.random().toString(36).slice(2)}`};
        this.homePreferenceBusy=true;this.homePreferenceError='';
        const actor=this.user?.id,generation=this.authGeneration;
        try{await global.axios.post('/api/dashboard/task-preferences',{...payload,idempotency_key:this._homeReminderAttempt.key});
          if(actor!==this.user?.id||generation!==this.authGeneration)return;
          this._homeReminderAttempt=null;this.homePreferenceForm=null;await this.loadOverview();
        }catch(error){if(actor===this.user?.id&&generation===this.authGeneration)this.homePreferenceError=typeof error.response?.data?.detail==='string'?error.response.data.detail:'提醒未保存，请重试';}
        finally{this.homePreferenceBusy=false;}
      },
      homeClearFilters() { this.homeCustomer='';this.homeQuery='';this.homeTaskMode='all';this.homeResetPages(); },
      homeRemember() { try{sessionStorage.setItem(`erp-home-filter:${this.user?.id}`,JSON.stringify({customer:this.homeCustomer,query:this.homeQuery}));}catch(_){} },
      homeState:stockState,
      homeNumber(value) { return value===null||value===undefined ? '待核' : Number(value).toLocaleString('zh-CN',{maximumFractionDigits:2}); },
      homeStockUnit(row) { return row.is_virtual_composite_parent ? '套' : row.unit_label || '单位待核'; },
      homeStockHeading(row) {
        return ({approval:'报料申请待审批',prepare:'已有备料，待生产',incoming:'已报料，待到货',missing:'补库资料待完善',covered:'现有备料已覆盖'})[stockState(row)]
          || `建议补至 ${this.homeNumber(row.target_quantity)}${this.homeStockUnit(row)}`;
      },
      homeStockDetail(row) {
        const state=stockState(row);
        if(state==='missing') return `缺：${(row.missing_fields || []).join('、') || '请核对常用箱'}`;
        if(state==='approval') return '已提交申请';
        if(state==='prepare') return `可覆盖 ${this.homeNumber(row.customer_board_preparation_auto_cover_capacity)}${this.homeStockUnit(row)}`;
        if(state==='incoming') return `在途纸板 ${this.homeNumber(row.incoming_board_preparation_sheet_quantity)}张`;
        if(state==='new') return `尚需补 ${this.homeNumber(row.suggested_new_requisition_finished_quantity)}${this.homeStockUnit(row)}`;
        return '查看库存与备料';
      },
      homeStockAction(row) {
        const state=stockState(row);
        if(state==='approval') return '查看申请';
        if(state==='missing') return this.hasPermission('products.edit') ? '补充资料' : this.canSubmitBusinessRequest ? '申请修改' : '查看缺项';
        if(state==='prepare') return ['admin','boss','workshop'].includes(this.user?.role) ? '安排生产' : '查看备料';
        if(state==='incoming') return '查看来料';
        if(state==='covered') return '查看库存';
        if(this.canSubmitBusinessRequest && !this.canRequisition) return '申请报料';
        if(this.canRequisition && (row.procurement_mode!=='external_purchase'||(this.canAdmin&&this.canViewCosts))) return row.procurement_mode==='external_purchase'?'生成采购草稿':'生成报料草稿';
        return '查看库存';
      },
      async homeOpenStock(row) {
        if(this.homeActionBusy || this.overviewError)return;
        this.homeActionBusy=true;
        try {
          const state=stockState(row);this.homeRemember();
          if(state==='approval') return this.homeOpenApproval(row.pending_request_ids[0]);
          if(state==='missing') {
            if(this.canSubmitBusinessRequest&&!this.hasPermission('products.edit')) {window.location.href=`/static/business-approvals.html?customer=${Number(row.customer_id)}&action=product_update&product=${Number(row.product_id)}&q=${encodeURIComponent(row.product_code || row.product_name)}`;return;}
            if(!this.hasPermission('products.view')) {this.homeExpandedPolicy=row.policy_id;return;}
            this.filters.productCustomer=row.customer_id;this.filters.productKeyword=row.product_code||row.product_name;
            this.homeEntry={target:'products',customer_label:row.customer_label,product_code:row.product_code};this.pages.products=1;
            this.invalidatePageCache('products');return await this.go('products');
          }
          if(state==='prepare' && ['admin','boss','workshop'].includes(this.user?.role)) {
            this.productionTab='preparation';this.stockPrepState='arrange';this.stockPrepQuery=row.product_code;
            this.homeEntry={target:'production',customer_label:row.customer_label,product_code:row.product_code};
            this.invalidatePageCache('production');return await this.go('production');
          }
          if(state==='incoming') {
            this.requisitionTab='reported';this.reportedFilters.customer_id=row.customer_id;this.reportedFilters.product_code=row.product_code;
            this.homeEntry={target:'requisition',customer_label:row.customer_label,product_code:row.product_code};
            this.pages.requisitionReported=1;this.invalidatePageCache('requisition');return await this.go('requisition');
          }
          if(state==='new' && (this.canRequisition || this.canSubmitBusinessRequest)
            && (this.canSubmitBusinessRequest || row.procurement_mode!=='external_purchase'||(this.canAdmin&&this.canViewCosts))) return await this.openLowStockReplenishment(row);
          this.homeEntry={target:'warehouse',customer_label:row.customer_label,product_code:row.product_code};
          return await this.openLowStockLocations(row);
        } finally {this.homeActionBusy=false;}
      },
      homeOpenApproval(id) { this.homeRemember();window.location.href=`/static/business-approvals.html?${id?'request='+Number(id):''}`; },
      async homeOpenTask(row) {
        if(this.homeActionBusy||this.overviewError)return;
        this.homeActionBusy=true;
        try {
          this.homeEntry={...row};
          if(row.key==='approval') return this.homeOpenApproval(row.request_id);
          if(row.key==='pending_production') {
            this.productionTab='pending';this.productionPendingSource='orders';this.productionQuery=row.customer_po||row.order_number||row.product_code;
            this.pages.productionPending=1;this.invalidatePageCache('production');return await this.go('production');
          }
          if(row.key==='pending_material'||row.key==='pending_incoming') {
            const target=row.target;
            if(target==='requisition'){this.requisitionWorkspace='board';this.requisitionTab='pending';this.requisitionSupplierFilter='';this.pages.requisitionPending=1;}
            else{this.incomingWorkspace='board';this.incomingTab='pending';this.pages.incomingPending=1;}
            this.invalidatePageCache(target);return await this.go(target);
          }
          if(row.key==='pending_delivery') {
            if(this.canSubmitBusinessRequest&&!this.hasPermission('deliveries.execute')) {
              this.homeRemember();window.location.href=`/static/business-approvals.html?customer=${Number(row.customer_id)}&action=delivery_create`;return;
            }
            if(this.hasPermission('deliveries.execute')) {
              await this.go('deliveries');await this.openDelivery(row.customer_id);
              if(this.modal?.type==='delivery'&&Number(this.deliveryForm.customer_id)===Number(row.customer_id)) {
                this.deliveryBatchPicker.keyword=row.product_code||row.customer_po||'';this.deliveryBatchPicker.visible=true;await this.loadDeliveryBatchItems(1);
              }
              return;
            }
          }
          const result=await this.openDashboardTarget(row);
          if(row.key==='pending_delivery' && !this.hasPermission('deliveries.execute')) {
            this.pendingDeliveryItems=this.pendingDeliveryItems.filter(r=>Number(r.customer_id)===Number(row.customer_id));
            this.deliveryCustomerCandidates=this.deliveryCustomerCandidates.filter(r=>Number(r.customer_id || r.id)===Number(row.customer_id));
          }
          return result;
        } finally {this.homeActionBusy=false;}
      },
      async homeReturn() {this.homeEntry=null;await this.go('dashboard');},
      async homeClearEntry() {
        const target=this.homeEntry?.target;this.homeEntry=null;
        if(target==='products'){this.filters.productCustomer='';this.filters.productKeyword='';}
        if(target==='production'){this.productionQuery='';this.stockPrepQuery='';}
        if(target==='requisition'){this.reportedFilters.customer_id='';this.reportedFilters.product_code='';}
        if(target==='deliveries'){this.deliveryListFilters.customer_id='';this.deliveryListFilters.keyword='';}
        if(target==='finance')this.financeFilters.customer_id='';
        this.invalidatePageCache(this.activePage);await this.loadPage(this.activePage,{force:true});
      },
      homeSelectMetric(mode) {this.homeTaskMode=mode;this.homeAttentionTab='all';this.homeTaskPage=1;},
    },
  };
  const template = `
  <section class="home-workbench" aria-label="今日工作台" :aria-busy="vm.overviewLoading">
    <header class="home-heading"><div><h1>今日工作台</h1><p>{{ vm.homeData.today || '' }}<span v-if="vm.overview.as_of"> · 更新于 {{ vm.formatDateTime(vm.overview.as_of) }}</span></p></div>
      <div class="home-filters"><select v-model="vm.homeCustomer" aria-label="首页客户筛选"><option value="">全部客户</option><option v-for="c in vm.homeData.customers" :key="c.id" :value="c.id">{{c.label}}</option></select>
        <input v-model.trim="vm.homeQuery" type="search" aria-label="首页查找" placeholder="客户 / 存货编码 / 订单号" />
        <button v-if="vm.homeCustomer || vm.homeQuery" class="home-link" @click="vm.homeClearFilters">清空</button>
        <button v-if="vm.canCreateOrders" class="home-button primary" @click="vm.openOrderPdfImport">导入订单</button>
      </div>
    </header>
    <div v-if="vm.overviewError" class="home-error" role="alert">{{vm.overview.as_of?'更新失败，以下保留上次数据':'首页读取失败'}}：{{vm.overviewError}} <button :disabled="vm.overviewLoading" @click="vm.loadOverview">重新加载</button></div>
    <div v-if="vm.overviewLoading" class="home-loading" role="status">正在更新首页…</div>
    <template v-if="vm.overview.as_of">
      <nav class="home-metrics" aria-label="首页重点事项">
        <button v-if="vm.homeCanStock" @click="vm.homeStockTab='action';vm.homeStockPage=1"><span>库存需处理</span><strong class="amber">{{vm.homeStockCounts.action}}<small>款</small></strong></button>
        <button v-if="vm.hasPermission('orders.view') || vm.hasPermission('deliveries.view')" @click="vm.homeSelectMetric('today')"><span>今日交期事项</span><strong>{{vm.homeTodayOrderIds.size}}<small>单</small></strong></button>
        <button v-if="vm.homeData.can_view_approvals" @click="vm.homeSelectMetric('approval')"><span>{{vm.homeData.can_review?'待我审批':'我的申请'}}</span><strong class="blue">{{vm.homeApprovalCount}}<small>项</small></strong></button>
        <button v-if="vm.hasPermission('deliveries.view')" @click="vm.homeSelectMetric('receipt')"><span>待回单</span><strong>{{vm.homeReceiptCount}}<small>张</small></strong></button>
      </nav>
      <div class="home-columns" :class="{'home-no-stock':!vm.homeCanStock}">
        <section v-if="vm.homeCanStock" class="home-panel home-stock" aria-labelledby="home-stock-title">
          <div class="home-panel-heading"><h2 id="home-stock-title">库存预警 <span class="home-badge amber">{{vm.homeStockCounts.action}}款待处理</span></h2><button class="home-link" @click="vm.homeStockTab='all'">全部预警</button></div>
          <nav class="home-tabs" aria-label="库存预警范围"><button v-for="(label,key) in {action:'需处理',arranged:'已安排',all:'全部'}" :key="key" :class="{active:vm.homeStockTab===key}" :aria-pressed="vm.homeStockTab===key" @click="vm.homeStockTab=key">{{label}} {{vm.homeStockCounts[key]}}</button></nav>
          <template v-if="!vm.homeStockDetailCustomer">
            <div class="home-customer-head"><span>客户</span><span>需处理</span><span>已安排</span><span>待审批</span><span></span></div>
            <article v-for="g in vm.homeStockGroupPage.rows" :key="g.id" class="home-customer-row">
              <div><strong>{{g.label}}</strong><small>{{g.rows.length}} 款预警</small></div><strong class="amber">{{g.action}}</strong><span>{{g.arranged}}</span><span>{{g.approval}}</span><button class="home-button" @click="vm.homeChooseStockCustomer(g.id)">查看产品 ›</button>
            </article>
            <div v-if="!vm.homeStockGroupPage.total" class="home-empty">当前范围暂无库存预警</div>
            <footer class="home-pagination"><span>共 {{vm.homeStockGroupPage.total}} 家客户 · {{vm.homeStockGroupPage.page}} / {{vm.homeStockGroupPage.pages}}</span><div><button :disabled="vm.homeStockGroupPage.page<=1" @click="vm.homeStockPage--">上一页</button><button :disabled="vm.homeStockGroupPage.page>=vm.homeStockGroupPage.pages" @click="vm.homeStockPage++">下一页</button></div></footer>
          </template>
          <template v-else>
          <div class="home-stock-back"><button v-if="!vm.homeCustomer" class="home-link" @click="vm.homeChooseStockCustomer(null)">‹ 客户汇总</button><strong>{{vm.homeStockGroups.find(g=>g.id===vm.homeStockDetailCustomer)?.label}}</strong></div>
          <div class="home-stock-head"><span>存货编码 / 产品</span><span>库存 / 预警线</span><span>补库状态</span><span>操作</span></div>
          <div v-if="!vm.homeStocksPage.total" class="home-empty">{{vm.homeCustomer || vm.homeQuery?'当前筛选下暂无库存预警':vm.homeStockTab==='action'?'暂无需要处理的库存预警':'暂无此类库存预警'}}</div>
          <article v-for="r in vm.homeStocksPage.rows" :key="r.policy_id" class="home-stock-item">
            <div class="home-stock-row"><div class="home-product"><span class="home-customer" :title="r.customer_name">{{r.customer_label || r.customer_name}}</span><strong>{{r.product_code || '无编码'}}</strong><span>{{r.product_name}}</span></div>
              <div class="home-stock-qty"><div><strong>{{vm.homeNumber(r.available_quantity)}}</strong><span> / {{vm.homeNumber(r.warning_quantity)}} {{vm.homeStockUnit(r)}}</span></div><small>{{r.is_virtual_composite_parent?'含可配套组件':'实存'}}<template v-if="r.allocatable_available_quantity!==null && r.allocatable_available_quantity!==undefined"> · 可用 {{vm.homeNumber(r.allocatable_available_quantity)}}</template></small></div>
              <div class="home-stock-status"><strong>{{vm.homeStockHeading(r)}}</strong><span :class="vm.homeState(r)==='new'||vm.homeState(r)==='missing'?'amber':'muted'">{{vm.homeStockDetail(r)}}</span><small v-if="vm.homeState(r)==='new' && !r.is_virtual_composite_parent && r.procurement_mode!=='external_purchase'">新报纸板 {{vm.homeNumber(r.suggested_new_requisition_sheet_quantity)}}张</small></div>
              <div class="home-row-actions"><button class="home-button" :class="{primary:vm.homeState(r)==='new'}" :disabled="vm.homeActionBusy || !!vm.overviewError" @click="vm.homeOpenStock(r)">{{vm.homeStockAction(r)}}</button><button class="home-link" :aria-expanded="vm.homeExpandedPolicy===r.policy_id" @click="vm.homeExpandedPolicy=vm.homeExpandedPolicy===r.policy_id?null:r.policy_id">{{vm.homeExpandedPolicy===r.policy_id?'收起明细':'库存明细'}}</button></div>
            </div>
            <div v-if="vm.homeExpandedPolicy===r.policy_id" class="home-stock-expanded">
              <span>订单占用 {{vm.homeNumber(r.reserved_quantity)}} {{vm.homeStockUnit(r)}}</span><span>备料 {{vm.homeNumber(r.customer_board_preparation_available_sheet_quantity)}} 张</span><span>在途 {{vm.homeNumber(r.incoming_board_preparation_sheet_quantity)}} 张</span>
              <span v-if="r.is_virtual_composite_parent">已成套 {{vm.homeNumber(r.assembled_quantity)}} · 未组装可配 {{vm.homeNumber(r.unassembled_available_set_quantity)}} 套</span>
              <span v-if="r.same_spec_warning_count">另有{{r.same_spec_warning_count}}款同规格预警，备料不重复计入</span>
              <span v-if="r.material_code">{{r.material_code}} · {{r.report_length_mm}} × {{r.report_width_mm}} mm</span>
              <button class="home-link" @click="vm.homeEntry={target:'warehouse',customer_label:r.customer_label,product_code:r.product_code};vm.openLowStockLocations(r)">查看库存位置</button>
              <button v-if="vm.canRequisition || vm.canEditStockAlerts" class="home-link" @click="vm.openProductStockPolicy(r)">修改预警</button>
              <button class="home-link" :disabled="vm.homeAdviceBusy" @click="vm.homeLoadAdvice(r)">补库分析依据</button>
              <div v-if="vm.homeAdvice?.policy_id===r.policy_id" class="home-stock-advice">
                <span v-if="vm.homeAdviceBusy">正在核对历史记录…</span><span v-else-if="vm.homeAdvice.error" role="alert">{{vm.homeAdvice.error}}</span>
                <template v-else><strong>规则参考 · {{vm.homeAdvice.period_start}} — {{vm.homeAdvice.as_of}}</strong>
                  <p>近90天送货 {{vm.homeAdvice.shipment_count_90}} 次 · {{vm.homeNumber(vm.homeAdvice.shipment_quantity_90)}} {{vm.homeAdvice.unit}}；近180天 {{vm.homeAdvice.shipment_count_180}} 次</p>
                  <p>报料→到货 {{vm.homeNumber(vm.homeAdvice.procurement_days)}} 天（{{vm.homeAdvice.procurement_samples}}例） · 到货→完工 {{vm.homeNumber(vm.homeAdvice.production_days)}} 天（{{vm.homeAdvice.production_samples}}例，中位数）</p>
                  <details><summary>依据与建议</summary><p v-for="reason in vm.homeAdvice.reasons">{{reason}}</p><p>{{vm.homeAdvice.location_advice}}</p><p v-for="s in vm.homeAdvice.shipments">{{s.date}} · {{s.delivery_number}} · {{vm.homeNumber(s.quantity)}} {{vm.homeAdvice.unit}}</p></details>
                </template>
              </div>
            </div>
          </article>
          <footer class="home-pagination"><span>共{{vm.homeStocksPage.total}}款 · {{vm.homeStocksPage.page}} / {{vm.homeStocksPage.pages}}</span><div><button :disabled="vm.homeStocksPage.page<=1" @click="vm.homeStockPage=vm.homeStocksPage.page-1">上一页</button><button :disabled="vm.homeStocksPage.page>=vm.homeStocksPage.pages" @click="vm.homeStockPage=vm.homeStocksPage.page+1">下一页</button></div></footer>
          </template>
        </section>
        <section class="home-panel home-tasks" aria-labelledby="home-tasks-title">
          <div class="home-panel-heading"><h2 id="home-tasks-title">{{vm.homeTaskLabel}}</h2><button class="home-link" @click="vm.homeTaskMode='all';vm.homeAttentionTab='active';vm.homeTaskPage=1">优先处理</button></div>
          <nav class="home-attention-tabs" aria-label="提醒分类"><button v-for="(label,key) in {active:'现在处理',customer:'待客户',deferred:'已延后',review:'待处置',hidden:'已隐藏',all:'全部'}" :key="key" :class="{active:vm.homeAttentionTab===key}" @click="vm.homeAttentionTab=key">{{label}} <span>{{vm.homeAttentionCounts[key]}}</span></button></nav>
          <div v-if="vm.homePreferenceError" class="home-error" role="alert">{{vm.homePreferenceError}}</div>
          <div v-if="!vm.homeTasksPage.total" class="home-empty">{{vm.homeQuery || vm.homeCustomer?'当前筛选下暂无待办':'当前没有此类待办'}}</div>
          <article v-for="r in vm.homeTasksPage.rows" :key="r.id" class="home-task">
            <div class="home-task-meta"><span class="home-badge" :class="r.urgency">{{r.urgency_label}}</span><span v-if="r.type!==r.urgency_label">{{r.type}}</span><time v-if="r.due_date && r.urgency==='normal'">交期 {{r.due_date}}</time></div>
            <div class="home-task-main"><div><strong :title="r.customer_name">{{r.customer_label}}<template v-if="r.product_code"> · {{r.product_code}}</template></strong><span v-if="r.product_name">{{r.product_name}}</span><p>{{r.message}}</p><small v-if="r.customer_po || r.order_number">{{r.customer_po?'客户单号':'ERP订单'}} {{r.customer_po || r.order_number}}</small><small v-if="r.attention_label" class="home-attention-note">{{r.attention_label}}<template v-if="r.remind_on"> · {{r.remind_on}}提醒</template></small><small v-else-if="r.resurface_reason" class="blue">{{r.resurface_reason}}</small></div><div class="home-task-actions"><button class="home-button" :disabled="vm.homeActionBusy || !!vm.overviewError" @click="vm.homeOpenTask(r)">{{r.action_text}}</button><button class="home-link" :disabled="vm.homePreferenceBusy || !!vm.overviewError" @click="vm.homeEditReminder(r)">更多</button><button v-if="r.attention_state!=='active' && (r.attention_scope==='personal'||vm.homeData.can_manage_team_reminders)" class="home-link" :disabled="vm.homePreferenceBusy || !!vm.overviewError" @click="vm.homeSaveReminder(r,'active')">恢复提醒</button></div></div>
            <div v-if="vm.homePreferenceForm?.task_id===r.id" class="home-reminder-form">
              <label>原因<select v-model="vm.homePreferenceForm.reason"><option v-for="reason in vm.homeData.attention_reasons" :key="reason.code" :value="reason.code">{{reason.label}}</option></select></label>
              <label>提醒日期<input type="date" v-model="vm.homePreferenceForm.remind_on" :min="vm.homeDateAfter(1)" :max="vm.homeDateAfter(365)" /></label>
              <div class="home-reminder-shortcuts"><button v-for="n in [1,3,7]" class="home-link" @click="vm.homePreferenceForm.remind_on=vm.homeDateAfter(n)">{{n===1?'明天':n+'天后'}}</button></div>
              <label v-if="vm.homeData.can_manage_team_reminders">范围<select v-model="vm.homePreferenceForm.scope"><option value="team">团队提醒</option><option value="personal">仅对我</option></select></label>
              <small>{{vm.homeReminderEffect()}}</small>
              <div class="home-reminder-buttons"><button class="home-button primary" :disabled="vm.homePreferenceBusy" @click="vm.homeSaveReminder(r)">{{vm.homePreferenceBusy?'保存中…':'保存安排'}}</button><button class="home-button" :disabled="vm.homePreferenceBusy" @click="vm.homeSaveReminder(r,'hidden')">仅对我隐藏</button><button class="home-link" :disabled="vm.homePreferenceBusy" @click="vm.homePreferenceForm=null">取消</button></div>
            </div>
          </article>
          <footer class="home-pagination"><span>共{{vm.homeTasksPage.total}}项 · {{vm.homeTasksPage.page}} / {{vm.homeTasksPage.pages}}</span><div><button :disabled="vm.homeTasksPage.page<=1" @click="vm.homeTaskPage=vm.homeTasksPage.page-1">上一页</button><button :disabled="vm.homeTasksPage.page>=vm.homeTasksPage.pages" @click="vm.homeTaskPage=vm.homeTasksPage.page+1">下一页</button></div></footer>
        </section>
      </div>
      <section class="home-panel home-progress"><div class="home-panel-heading"><h2>业务进度</h2><button v-if="vm.canViewDeliveryMargin" class="home-link" :aria-expanded="vm.homeAnalyticsOpen" @click="vm.homeAnalyticsOpen=!vm.homeAnalyticsOpen">{{vm.homeAnalyticsOpen?'收起财务分析':'财务分析'}}</button></div><nav aria-label="业务进度"><button v-for="c in vm.homeStages" :key="c.key" :class="{active:vm.homeTaskMode===c.key}" @click="vm.homeSelectMetric(c.key)">{{c.title}} <strong>{{c.count}}</strong><small>{{c.count_unit}}</small><span v-if="c.key==='pending_payment'" class="home-stage-amount">¥{{vm.homeNumber(c.amount)}}</span></button><button v-if="vm.dashboardRequisitionHoldSummary.total" @click="vm.openWaitingRequisitionFromDashboard">等候报料 <strong>{{vm.dashboardRequisitionHoldSummary.total}}</strong>条</button></nav></section>
      <div v-if="vm.deliveryBacklogs.items.length || vm.deliveryBacklogs.error" class="home-backlog"><button class="home-link" @click="vm.openDeliveryBacklogs">待补送与历史</button><span>{{vm.deliveryBacklogs.error || '有待补送事项，请核对交付安排'}}</span></div>
    </template>
  </section>`;
  function install(app) {
    const methods=app._component.methods;
    for(const [method,target] of [['requisitionPendingRequestParams','requisition'],['incomingPendingRequestParams','incoming']]) {
      const original=methods[method];
      methods[method]=function(...args) {const params=original.apply(this,args);if(this.homeEntry?.target===target && this.homeEntry.source_item_id)params.home_item_id=this.homeEntry.source_item_id;return params;};
    }
    app.mixin(mixin);
    app.component('home-workbench',{computed:{vm(){return this.$root;}},template});
  }
  const exported={install,mixin,template,matches,stockState,actionable,pageRows,stageCards,customerGroups,fairTasks,attentionCategory};
  if(typeof module!=='undefined'&&module.exports)module.exports=exported;
  global.ERPHomeWorkbench=exported;
})(typeof window==='undefined'?globalThis:window);
