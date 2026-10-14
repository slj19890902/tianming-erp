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
  const componentNames = {whole:'整片',cover:'盖片',base:'底片'};
  function physicalComponents(value, bom=false) {
    const settings=value.sheet_cutting_settings_snapshot;
    if(settings && [2,3].includes(settings.schema_version)) {
      if(settings.whole && !settings.cover && !settings.base)return ['whole'];
      if(settings.cover && settings.base && !settings.whole)return ['cover','base'];
      return [];
    }
    const style=String(value.snapshot_component_box_style || value.box_style || '').trim().toUpperCase().replace(/\s+/g,'');
    const standalone=['BOTTOM_BASE','TOP_COVER','独立底','独立天盖','TDGD天地盖底','TDGG天地盖盖','天盖','底','模切盖模切盖'];
    if(standalone.includes(style))return ['whole'];
    if(style==='A3_SET' || /A3|天地盖/.test(style))return ['cover','base'];
    if(!bom && Number(value.snapshot_base_report_length_mm)>0 && Number(value.snapshot_base_report_width_mm)>0)return ['cover','base'];
    return ['whole'];
  }
  function orderTargets(item) {
    const itemId=Number(item.id || item.order_item_id || item.item_id);
    if(!itemId)return [];
    const children=item.bom_components || [],targets=[];
    if(!children.length || (!children.some(child=>Number(child.component_product_id)===Number(item.product_id))
        && Number(item.snapshot_report_length_mm)>0 && Number(item.snapshot_report_width_mm)>0)) {
      for(const part of physicalComponents(item))targets.push({key:`order:${itemId}:${part}`,order_item_id:itemId,
        component_type:part,label:`主件 · ${componentNames[part]}`});
    }
    for(const child of children) {
      const snapshotId=Number(child.id || child.bom_snapshot_id || child.snapshot_id);
      if(!snapshotId)continue;
      for(const part of physicalComponents(child,true))targets.push({key:`bom:${snapshotId}:${part}`,order_item_id:itemId,
        bom_snapshot_id:snapshotId,component_type:part,label:`${child.product_name || child.snapshot_component_product_name || child.product_code || '子件'} · ${componentNames[part]}`});
    }
    return targets;
  }
  function reportedTargets(line) {
    const itemId=Number(line.order_item_id);
    if(!itemId)return [];
    const bom=String(line.source_key || '').match(/^component:([1-9]\d*):(whole|cover|base)$/);
    if(bom)return [{key:`bom:${bom[1]}:${bom[2]}`,order_item_id:itemId,bom_snapshot_id:Number(bom[1]),
      component_type:bom[2],label:`${line.product_name || '子件'} · ${componentNames[bom[2]]}`}];
    if(line.source_key && String(line.source_key).startsWith('component:'))return [];
    const source=String(line.source_key || '').match(/^order_item:([1-9]\d*):(whole|cover|base)$/);
    if(source && Number(source[1])!==itemId)return [];
    // Legacy ordinary requisitions encode these exact suffixes in the frozen
    // source name; this mirrors _requisition_item_component on the server.
    const part=line.component_type || source?.[2] ||
      (String(line.product_name || '').endsWith('-底')?'base':String(line.product_name || '').endsWith('-盖')?'cover':'whole');
    if(!componentNames[part])return [];
    return [{key:`order:${itemId}:${part}`,order_item_id:itemId,component_type:part,label:componentNames[part]}];
  }
  function refreshDraftGroup(line) {
    const children=line._merged_component_lines || [];
    const stockExtra=Number(line.stock_purpose_sheet_qty || 0),remark=line.remark;
    for(const key of ['required_piece_qty','remaining_required_piece_qty','semi_finished_reserved_piece_qty','finished_inventory_reserved_qty']) {
      line[key]=children.reduce((sum,child)=>sum+Number(child[key] || 0),0);
    }
    line._minimum_requisition_qty=children.reduce((sum,child)=>sum+Number(child._minimum_requisition_qty ?? child.order_purpose_sheet_qty ?? 0),0);
    line._authoritative_order_purpose_sheet_qty=line._minimum_requisition_qty;
    line.order_purpose_sheet_qty=line._minimum_requisition_qty;
    line.stock_purpose_sheet_qty=stockExtra;line.purchase_total_sheet_qty=line.order_purpose_sheet_qty+stockExtra;
    line.requisition_qty=line.purchase_total_sheet_qty;line.remark=remark;
    line._manual_sheet_refresh_error=children.some(child=>child._manual_sheet_refresh_error);
  }
  const component = {
    props:{actorId:{type:Number,default:0},snapshotId:{type:Number,default:0},orderItemId:{type:Number,default:0}, componentType:{type:String,default:'whole'},disabled:{type:Boolean,default:false}},
    emits:['reserved','operation-state'],
    data() {return {opened:false,loading:false,busy:false,error:'',message:'',preview:null,rows:[],uncertain:false};},
    created() {this.uncertain=pending.has(this.identity);},
    computed:{identity() {return `${this.actorId || 0}:${this.snapshotId?'bom':'order'}:${this.snapshotId || this.orderItemId}:${this.componentType}`;}},
    watch:{identity() {this.loading=false;this.busy=false;this.opened=false;this.preview=null;this.rows=[];this.error='';this.message='';this.uncertain=pending.has(this.identity);}},
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
        this.loading=true;const identity=this.identity;
        try {
          const {data}=await root.axios.get(this.endpoint()+'/manual-inventory', {params:{component_type:this.componentType}});
          if(this.identity!==identity)return;
          this.preview=data;this.rows=(data.items || []).map(row=>({...row,inputQuantity:''}));this.uncertain=false;
        } catch(error) {if(this.identity===identity)this.error=this.errorText(error);}
        finally {if(this.identity===identity)this.loading=false;}
      },
      errorText(error) {const detail=error.response?.data?.detail;return typeof detail==='string'?detail:(detail?.message || error.message || '库存操作失败');},
      async reserve(row) {
        if (this.busy || this.loading || (this.disabled && !this.uncertain)) return;
        const identity=this.identity;
        let held=pending.get(identity);
        if (!held) {
          const quantity=chosenQuantity(row);
          if (!row.selectable || !quantity) {this.error='请填写不超过可用库存及需求上限的正整数张数';return;}
          held={preview:this.preview,rows:this.rows,payload:{component_type:this.componentType,
            lot_id:row.lot_id,stock_quantity:quantity,expected_version:row.version,
            expected_order_item_revision:this.preview.expected_order_item_revision,
            review_hash:row.review_hash,warning_acknowledged_codes:row.warning_codes || [],idempotency_key:operationKey()}};
          pending.set(identity,held);
        }
        this.busy=true;this.error='';this.message='';
        this.$emit('operation-state',{identity,blocked:true});
        try {
          const {data}=await root.axios.post(this.endpoint()+'/manual-reserve',held.payload);
          pending.delete(identity);
          if(this.identity!==identity)return;
          this.uncertain=false;
          this.message=data.message;this.preview={...this.preview,...data};
          // A successful write remains successful even when its refresh fails.
          this.rows=[];this.$emit('reserved',data);
          try {const {data:fresh}=await root.axios.get(this.endpoint()+'/manual-inventory',{params:{component_type:this.componentType}});
            if(this.identity!==identity)return;
            this.preview=fresh;this.rows=(fresh.items || []).map(item=>({...item,inputQuantity:''}));this.$emit('reserved',fresh);
          } catch(error) {if(this.identity===identity)this.error='抵扣已成功；库存列表刷新失败，请点击重新查看，不要重复抵扣。';}
        } catch(error) {
          const status=error.response?.status;
          if(status && status<500)pending.delete(identity);
          if(this.identity!==identity)return;
          if (status && status<500) {pending.delete(identity);this.uncertain=false;this.rows=[];this.error=this.errorText(error)+'；请重新查看库存。';}
          else {this.uncertain=true;this.error='抵扣结果暂未确认。请点击“核对上次抵扣结果”，保留原批次和数量。';}
        } finally {if(this.identity===identity)this.busy=false;this.$emit('operation-state',{identity,blocked:pending.has(identity)});}
      }
    },
    template:`<section class="manual-sheet-deduction" style="margin-top:6px">
      <button type="button" class="btn small" :disabled="busy || (disabled && !uncertain)" @click="open">{{uncertain?'查看上次抵扣':'使用库存片料'}}</button>
      <div v-if="opened" style="border:1px solid #d1d5db;padding:10px;margin-top:6px;min-width:260px;max-width:480px;white-space:normal">
        <strong>选择片料，自己填写本次张数</strong>
        <div v-if="preview" class="muted">{{preview.product_code}} · {{preview.board_length_mm}}×{{preview.board_width_mm}} mm · {{preview.material_code}}<br>已承诺 {{preview.committed_piece_qty || 0}} 片 · 未合并仍需 {{preview.remaining_required_piece_qty}} 片<br>库存已安排 {{preview.semi_finished_reserved_piece_qty || 0}} 片 · 每只用 {{preview.pieces_per_box}} 片</div>
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
  root.TMManualSheetDeduction={component,chosenQuantity,pending,physicalComponents,orderTargets,reportedTargets,refreshDraftGroup};
  if (typeof module !== 'undefined') module.exports=root.TMManualSheetDeduction;
})(typeof window !== 'undefined'?window:globalThis);
