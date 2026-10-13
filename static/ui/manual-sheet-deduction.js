(function (root) {
  'use strict';
  // Preserve an uncertain operation when its parent list/draft is reopened.
  const pending = new Map();
  const differenceNames = {customer:'通用／客户归属', board_length_mm:'长度', board_width_mm:'宽度',
    material_code:'材质', flute:'楞型', component:'组件', pieces_per_box:'每只耗片数', stock_yield_per_sheet:'每张产出'};
  function chosenQuantity(row) {
    const quantity = Number(row.inputQuantity);
    if (!Number.isSafeInteger(quantity) || quantity <= 0 || quantity > row.max_stock_quantity) return 0;
    return quantity;
  }
  function operationKey() {
    return 'manual-sheet-' + (root.crypto?.randomUUID?.() || Date.now().toString(36) + '-' + Math.random().toString(36).slice(2));
  }
  const component = {
    props:{actorId:{type:Number,default:0},snapshotId:{type:Number,default:0},orderItemId:{type:Number,default:0}, componentType:{type:String,default:'whole'},disabled:{type:Boolean,default:false}},
    emits:['reserved','operation-state'],
    data() {return {opened:false,loading:false,busy:false,error:'',message:'',preview:null,rows:[],uncertain:false};},
    computed:{identity() {return `${this.actorId || 0}:${this.snapshotId?'bom':'order'}:${this.snapshotId || this.orderItemId}:${this.componentType}`;}},
    methods:{
      endpoint() {return `/api/warehouse/semi-finished/${this.snapshotId ? 'bom-components/'+this.snapshotId : 'order-items/'+this.orderItemId}`;},
      quantity:chosenQuantity,
      differences(row) {return (row.signature_differences || []).map(name=>differenceNames[name] || name).join('、');},
      credit(row) {return Math.min(chosenQuantity(row) * Number(row.stock_yield_per_sheet), this.preview?.remaining_required_piece_qty || 0);},
      async open() {
        if (this.busy || (this.disabled && !this.uncertain)) return;
        this.opened=true;this.error='';this.message='';
        const held=pending.get(this.identity);
        if (held) {this.preview=held.preview;this.rows=held.rows;this.uncertain=true;return;}
        this.loading=true;
        try {
          const {data}=await root.axios.get(this.endpoint()+'/manual-inventory', {params:{component_type:this.componentType}});
          this.preview=data;this.rows=(data.items || []).map(row=>({...row,inputQuantity:''}));this.uncertain=false;
        } catch(error) {this.error=this.errorText(error);}
        finally {this.loading=false;}
      },
      errorText(error) {const detail=error.response?.data?.detail;return typeof detail==='string'?detail:(detail?.message || error.message || '库存操作失败');},
      async reserve(row) {
        if (this.busy || this.loading || (this.disabled && !this.uncertain)) return;
        let held=pending.get(this.identity);
        if (!held) {
          const quantity=chosenQuantity(row);
          if (!row.selectable || !quantity) {this.error='请填写不超过可用库存及需求上限的正整数张数';return;}
          held={preview:this.preview,rows:this.rows,payload:{component_type:this.componentType,
            lot_id:row.lot_id,stock_quantity:quantity,expected_version:row.version,
            expected_order_item_revision:this.preview.expected_order_item_revision,
            review_hash:row.review_hash,warning_acknowledged_codes:row.warning_codes || [],idempotency_key:operationKey()}};
          pending.set(this.identity,held);
        }
        this.busy=true;this.error='';this.message='';
        this.$emit('operation-state',{identity:this.identity,blocked:true});
        try {
          const {data}=await root.axios.post(this.endpoint()+'/manual-reserve',held.payload);
          pending.delete(this.identity);this.uncertain=false;
          this.message=data.message;this.preview={...this.preview,...data};
          // A successful write remains successful even when its refresh fails.
          this.rows=[];this.$emit('reserved',data);
          try {const {data:fresh}=await root.axios.get(this.endpoint()+'/manual-inventory',{params:{component_type:this.componentType}});
            this.preview=fresh;this.rows=(fresh.items || []).map(item=>({...item,inputQuantity:''}));this.$emit('reserved',fresh);
          } catch(error) {this.error='抵扣已成功；库存列表刷新失败，请点击重新查看，不要重复抵扣。';}
        } catch(error) {
          const status=error.response?.status;
          if (status && status<500) {pending.delete(this.identity);this.uncertain=false;this.rows=[];this.error=this.errorText(error)+'；请重新查看库存。';}
          else {this.uncertain=true;this.error='抵扣结果暂未确认。请点击“核对上次抵扣结果”，保留原批次和数量。';}
        } finally {this.busy=false;this.$emit('operation-state',{identity:this.identity,blocked:this.uncertain});}
      }
    },
    template:`<section class="manual-sheet-deduction" style="margin-top:6px">
      <button type="button" class="btn small" :disabled="busy || (disabled && !uncertain)" @click="open">{{uncertain?'查看上次抵扣':'使用库存片料'}}</button>
      <div v-if="opened" style="border:1px solid #d1d5db;padding:10px;margin-top:6px;min-width:260px;max-width:480px;white-space:normal">
        <strong>选择片料，自己填写本次张数</strong>
        <div v-if="preview" class="muted">{{preview.product_code}} · {{preview.board_length_mm}}×{{preview.board_width_mm}} mm · {{preview.material_code}}<br>剩余 {{preview.remaining_required_piece_qty}} 片 · 每只用 {{preview.pieces_per_box}} 片</div>
        <div v-if="loading">正在查询库存…</div>
        <div v-if="message" class="status green" style="white-space:normal">{{message}}</div>
        <div v-if="error" class="status orange" style="white-space:normal">{{error}}</div>
        <button v-if="uncertain" type="button" class="btn small" :disabled="busy" @click="reserve(null)">{{busy?'正在核对…':'核对上次抵扣结果'}}</button>
        <article v-for="row in rows" :key="row.lot_id" style="border-top:1px solid #e5e7eb;margin-top:8px;padding-top:8px">
          <b>{{row.internal_name || '库存片料'}} · {{row.board_length_mm}}×{{row.board_width_mm}} mm</b>
          <div>{{row.material_code}} / {{row.flute_type}} · 可用 {{row.available_stock_quantity}} 张</div>
          <div>{{row.warehouse_location?.location_name || row.warehouse_location?.name || '位置待核对'}}</div>
          <div v-if="differences(row)" class="status orange" style="white-space:normal">需核对：{{differences(row)}}</div>
          <div v-for="(warning,index) in row.warning_messages" :key="index" class="muted">{{warning}}</div>
          <div v-if="row.cut_plan" class="muted">分切后 {{row.cut_plan.target_length_mm}}×{{row.cut_plan.target_width_mm}} mm，每张抵 {{row.stock_yield_per_sheet}} 片</div>
          <div v-if="row.selectable && row.max_stock_quantity>0">
            <label>本次使用 <input class="input compact-input" type="number" min="1" step="1" :max="row.max_stock_quantity" v-model="row.inputQuantity" :disabled="busy || uncertain" style="width:90px"> 张</label>
            <span class="muted">最多 {{row.max_stock_quantity}} 张；抵扣 {{credit(row)}} 片</span>
            <button type="button" class="btn small success" :disabled="busy || uncertain || disabled || !quantity(row)" @click="reserve(row)">确认抵扣</button>
            <small class="muted" style="display:block">确认即采用本批次并确认上方差异；只预占填写的张数。</small>
          </div>
          <div v-else class="muted">{{row.selectable?'当前需求已覆盖，无需再抵扣':'当前批次不能直接采用，请先核对库存用途或加工资料。'}}</div>
        </article>
        <div v-if="!loading && preview && !rows.length && !uncertain" class="muted">没有可列出的片料；请核对库存用途、楞型、面纸和可用量。</div>
        <button type="button" class="btn small" :disabled="busy || uncertain" @click="open">重新查看</button>
        <button type="button" class="btn small" :disabled="busy || uncertain" @click="opened=false">收起</button>
      </div>
    </section>`
  };
  root.TMManualSheetDeduction={component,chosenQuantity,pending};
  if (typeof module !== 'undefined') module.exports=root.TMManualSheetDeduction;
})(typeof window !== 'undefined'?window:globalThis);
