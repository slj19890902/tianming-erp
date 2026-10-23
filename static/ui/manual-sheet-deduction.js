(function (root) {
  'use strict';
  // Preserve an uncertain operation when its parent list/draft is reopened.
  const pending = new Map();
  function pendingForActor(actorId) {
    return [...pending.entries()].filter(([identity])=>identity.startsWith(`${Number(actorId)||0}:`));
  }
  async function reconcileForActor(actorId) {
    const entries=pendingForActor(actorId);
    if(entries.some(([,held])=>held.inFlight))return {complete:false,busy:true,outcomes:[]};
    const outcomes=[];
    for(const [,held] of entries)outcomes.push(await held.resume());
    return {complete:pendingForActor(actorId).length===0,busy:false,outcomes};
  }
  const differenceNames = {customer:'通用／客户归属', board_length_mm:'长度', board_width_mm:'宽度',
    material_code:'材质', flute:'楞型', component:'组件', pieces_per_box:'每只耗片数', stock_yield_per_sheet:'每张产出'};
  function chosenQuantity(row) {
    const quantity = Number(row.inputQuantity);
    if (!Number.isSafeInteger(quantity) || quantity <= 0 || quantity > (row.actual_max_stock_quantity ?? row.max_stock_quantity)) return 0;
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
    props:{actorId:{type:Number,default:0},snapshotId:{type:Number,default:0},orderItemId:{type:Number,default:0}, componentType:{type:String,default:'whole'},disabled:{type:Boolean,default:false},operationReporter:{type:Function,default:null},allowRequisition:{type:Boolean,default:false}},
    emits:['reserved','operation-state','requisition'],
    data() {return {opened:false,loading:false,busy:false,error:'',message:'',preview:null,rows:[],uncertain:false,matchMode:'auto',readSequence:0,mapLocation:null,useScrap:false,scrapQuantity:''};},
    created() {this.uncertain=pending.has(this.identity);},
    beforeUnmount() {this.disposed=true;this.readSequence++;},
    computed:{identity() {return `${this.actorId || 0}:${this.snapshotId?'bom':'order'}:${this.snapshotId || this.orderItemId}:${this.componentType}`;}},
    watch:{identity() {this.readSequence++;this.loading=false;this.busy=false;this.opened=false;this.preview=null;this.rows=[];this.error='';this.message='';this.mapLocation=null;this.useScrap=false;this.scrapQuantity='';this.uncertain=pending.has(this.identity);}},
    methods:{
      reportOperation(state) {
        // Vue drops emitted events after unmount; the owner must still settle
        // its lock when a successful reservation removes this pending row.
        if(this.operationReporter)this.operationReporter(state);
        else this.$emit('operation-state',state);
      },
      hold(payload,batch=false,adjustment=false) {
        const identity=this.identity;
        pending.set(identity,{preview:this.preview,rows:this.rows,payload,matchMode:this.matchMode,batch,
          endpoint:this.endpoint()+(adjustment?'/manual-adjust':batch?'/manual-reserve-batch':'/manual-reserve'),adjustment,inFlight:false,
          resume:()=>this.resumeHeld(identity)});
      },
      locationName(row) {return row.warehouse_location?.location_name || row.warehouse_location?.name || row.warehouse_location?.location_code || '位置待核对';},
      openMap(row) {
        const id=Number(row.warehouse_location?.id);
        if(this.busy || !Number.isSafeInteger(id) || id<=0)return;
        this.mapReturnFocus=root.document?.activeElement;
        this.mapLocation={name:this.locationName(row),url:'/warehouse.html?'+new URLSearchParams({
          embedded:'1',readonly:'1',tab:'map',mode:'lookup',view:'2d',location_id:String(id),
          lot_id:String(row.lot_id),floor:String(row.warehouse_location?.warehouse_floor || '')})};
        this.$nextTick?.(()=>this.$refs?.mapBack?.focus());
      },
      closeMap() {
        this.mapLocation=null;
        this.$nextTick?.(()=>{if(this.mapReturnFocus?.isConnected)this.mapReturnFocus.focus();});
      },
      wasteText(row) {
        return row.waste_basis==='rectangular_nesting'
          ? `每张预计裁余 ${row.waste_area_cm2} cm²（${row.waste_percent}%）`
          : '每张裁余待核算（无可验证排版）';
      },
      scrapCount() {
        if(!this.useScrap)return 0;
        const n=Number(this.scrapQuantity);
        return Number.isSafeInteger(n) && n>0 ? n : 0;
      },
      selectScrap() {
        if(this.useScrap)this.scrapQuantity=Math.floor(this.totals().uncovered/Math.max(Number(this.preview?.pieces_per_box || 1),1));
      },
      endpoint() {return `/api/warehouse/semi-finished/${this.snapshotId ? 'bom-components/'+this.snapshotId : 'order-items/'+this.orderItemId}`;},
      quantity:chosenQuantity,
      differences(row) {return (row.signature_differences || []).filter(name=>!['board_length_mm','board_width_mm'].includes(name)).map(name=>differenceNames[name] || name).join('、');},
      credit(row) {return Math.min(chosenQuantity(row) * Number(row.stock_yield_per_sheet), this.preview?.remaining_required_piece_qty || 0);},
      selection() {return this.rows.filter(row=>row.selected);},
      totals() {
        const selected=this.selection(),need=Number(this.preview?.remaining_required_piece_qty || 0);
        const pieces=selected.reduce((sum,row)=>sum+chosenQuantity(row)*Number(row.stock_yield_per_sheet),0);
        const stock=selected.reduce((sum,row)=>sum+chosenQuantity(row),0);
        let remaining=need,extra=false;
        const valid=selected.length>0 && selected.every(row=>{
          const quantity=chosenQuantity(row),output=Number(row.stock_yield_per_sheet);
          const credit=Math.min(quantity*output,remaining);remaining-=credit;
          const over=Math.ceil(credit/output)<quantity;
          extra=extra || over;
          return row.selectable && quantity>0 && output>0 && credit>0 &&
            (!over || Number(row.actual_max_stock_quantity)>Number(row.max_stock_quantity));
        });
        const perBox=Math.max(Number(this.preview?.pieces_per_box || 1),1);
        const uncovered=Math.max(need-pieces,0),scrap=this.scrapCount()*perBox;
        const scrapValid=!this.useScrap || (this.preview?.scrap_completion_allowed && scrap>0 && scrap<=uncovered);
        return {stock,pieces,credit:Math.min(pieces,need),remaining:Math.max(uncovered-scrap,0),uncovered,
          boxes:Math.floor(pieces/perBox),excess:Math.max(pieces-need,0),extra,valid:valid && scrapValid};
      },
      choose(row) {
        if(this.busy || this.uncertain)return;
        if(row.selected && !chosenQuantity(row)) {
          const other=this.selection().filter(item=>item!==row).reduce((sum,item)=>sum+chosenQuantity(item)*Number(item.stock_yield_per_sheet),0);
          const need=Math.max(Number(this.preview?.remaining_required_piece_qty || 0)-other,0);
          row.inputQuantity=Math.min(row.max_stock_quantity,Math.ceil(need/Number(row.stock_yield_per_sheet))) || '';
        }
      },
      yieldText(row) {
        const output=Number(row.stock_yield_per_sheet),perBox=Math.max(Number(this.preview?.pieces_per_box || 1),1);
        if(row.cut_plan){const plan=row.cut_plan;return `分切${Number(plan.rows||1)*Number(plan.columns||1)}份 · ${plan.blank_yield||1}模 · 每张出${output}片`;}
        if(this.preview?.is_bom || this.componentType!=='whole')return `一开${output} · 每片出${output}片部件`;
        const unit=this.preview?.output_unit || '只';
        return perBox===1 ? `一开${output} · 每片可做${output}${unit}` : `一开${output} · 每${perBox}片箱片做1${unit}`;
      },
      async open() {
        if(this.busy || (this.disabled && !this.uncertain))return;
        this.opened=true;this.error='';this.message='';
        this.focusDialog();
        const held=pending.get(this.identity);this.uncertain=Boolean(held);
        if(held) {this.preview=held.preview;this.rows=held.rows;this.matchMode=held.matchMode || 'exact';this.uncertain=true;return;}
        await this.load('auto');
      },
      async load(mode='auto') {
        if(this.busy || this.uncertain)return;
        if(mode==='swapped' && (!this.preview?.can_further_match || this.matchMode!=='exact' || this.rows.length))return;
        this.loading=true;this.error='';this.rows=[];this.matchMode=mode;
        this.useScrap=false;this.scrapQuantity='';
        const identity=this.identity,sequence=++this.readSequence;
        try {
          const {data}=await root.axios.get(this.endpoint()+'/manual-inventory',{params:{component_type:this.componentType,match_mode:mode},timeout:15000});
          if(this.identity!==identity || sequence!==this.readSequence)return;
          this.preview=data;this.rows=(data.items || []).map(row=>({...row,inputQuantity:'',selected:false}));this.uncertain=false;
          this.$nextTick?.(()=>this.$refs?.dialog?.focus());
        } catch(error) {if(this.identity===identity && sequence===this.readSequence){this.preview=null;this.error=this.errorText(error);}}
        finally {if(this.identity===identity && sequence===this.readSequence)this.loading=false;}
      },
      focusDialog() {
        this.returnFocus=root.document?.activeElement;
        this.$nextTick?.(()=>this.$refs?.dialog?.focus());
      },
      close() {
        if(this.busy)return;
        if(this.mapLocation){this.closeMap();return;}
        this.opened=false;this.readSequence++;this.loading=false;
        this.$nextTick?.(()=>{if(this.returnFocus?.isConnected)this.returnFocus.focus();});
      },
      keydown(event) {
        if(event.key==='Escape') {event.preventDefault();event.stopPropagation();this.close();return;}
        if(event.key!=='Tab')return;
        const panel=this.mapLocation?this.$refs?.mapDialog:this.$refs?.dialog;
        const controls=Array.from(panel?.querySelectorAll('button:not([disabled]),input:not([disabled]),summary,[href]') || []).filter(el=>el.getClientRects().length);
        const first=controls[0],last=controls.at(-1),active=root.document?.activeElement;
        if(!first) {event.preventDefault();panel?.focus();}
        else if(event.shiftKey && (active===first || active===panel)) {event.preventDefault();last.focus();}
        else if(!event.shiftKey && (active===last || active===panel)) {event.preventDefault();first.focus();}
      },
      errorText(error) {const detail=error.response?.data?.detail;return typeof detail==='string'?detail:(detail?.message || error.message || '库存操作失败');},
      adjustmentChanges() {
        return (this.preview?.existing_reservations || []).filter(row=>Number(row.keep_stock_quantity)!==Number(row.remaining_stock_quantity));
      },
      adjustmentValid() {
        const changes=this.adjustmentChanges();
        return changes.length>0 && changes.every(row=>row.can_adjust && row.keep_stock_quantity!=='' &&
          Number.isSafeInteger(Number(row.keep_stock_quantity)) && Number(row.keep_stock_quantity)>=0 &&
          Number(row.keep_stock_quantity)<Number(row.remaining_stock_quantity));
      },
      async saveAdjustment() {
        if(this.busy || this.loading || this.disabled || this.uncertain || !this.adjustmentValid())return;
        if(root.confirm && !root.confirm('确认减少未加工片料安排？减少部分退回可用库存，差额重新进入待报料；已采购和已完工记录保持原事实。'))return;
        const payload={component_type:this.componentType,expected_adjustment_revision:this.preview.adjustment_revision,
          idempotency_key:operationKey(),items:this.adjustmentChanges().map(row=>({
            reservation_id:row.reservation_id,keep_stock_quantity:Number(row.keep_stock_quantity)}))};
        this.hold(payload,false,true);
        await this.submitHeld();
      },
      goRequisition() {
        if(!this.allowRequisition || this.busy || this.loading || this.uncertain || this.disabled ||
          this.adjustmentChanges().length || this.selection().length || !(this.preview?.remaining_required_piece_qty>0))return;
        this.close();this.$emit('requisition',this.preview);
      },
      async confirmSelection() {
        if(this.busy || this.loading || this.disabled || this.uncertain || this.adjustmentChanges().length || !this.totals().valid)return;
        const payload={component_type:this.componentType,match_mode:this.matchMode,
          expected_order_item_revision:this.preview.expected_order_item_revision,idempotency_key:operationKey(),
          items:this.selection().map(row=>({lot_id:row.lot_id,stock_quantity:chosenQuantity(row),expected_version:row.version,
            review_hash:row.review_hash,warning_acknowledged_codes:row.warning_codes || []}))};
        if(this.totals().extra)payload.produce_extra_stock=true;
        if(this.useScrap) {
          const now=new Date(),stockDate=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
          const identity=this.identity,quantity=this.scrapCount();
          this.busy=true;this.error='';this.reportOperation({identity,blocked:true});
          try {
            const {data}=await root.axios.post('/api/production/scrap-production/preview',{
              action:'finish_now',product_id:this.preview.product_id,order_item_id:this.orderItemId,
              quantity,stock_date:stockDate,location_id:null,variant_id:null,remarks:'库料短缺，已用废料补足',
              preview_hash:'',operation_key:'sheet-scrap-preview'},{timeout:15000});
            if(this.disposed || this.identity!==identity)return;
            payload.scrap_completion={quantity,stock_date:stockDate,preview_hash:data.preview_hash};
          } catch(error) {this.error=this.errorText(error);return;}
          finally {if(this.identity===identity)this.busy=false;this.reportOperation({identity,blocked:false});}
        }
        this.hold(payload,true);
        await this.submitHeld();
      },
      async reserve(row) {
        if(this.busy || this.loading || (this.disabled && !this.uncertain))return;
        if(!pending.has(this.identity)) {
          const quantity=chosenQuantity(row);
          if(!row.selectable || !quantity) {this.error='请填写不超过可用库存及需求上限的正整数片数';return;}
          this.hold({component_type:this.componentType,
            match_mode:this.matchMode,lot_id:row.lot_id,stock_quantity:quantity,expected_version:row.version,
            expected_order_item_revision:this.preview.expected_order_item_revision,review_hash:row.review_hash,
            warning_acknowledged_codes:row.warning_codes || [],idempotency_key:operationKey()});
        }
        await this.submitHeld();
      },
      async submitHeld() {
        return this.resumeHeld(this.identity);
      },
      async resumeHeld(identity) {
        const held=pending.get(identity);
        if(!held) {
          if(!this.disposed && this.identity===identity){this.uncertain=false;await this.load('auto');}
          return {outcome:'settled'};
        }
        if(held.inFlight || this.busy)return {outcome:'busy'};
        held.inFlight=true;
        this.busy=true;this.error='';this.message='';this.reportOperation({identity,blocked:true});
        let committed=false;
        try {
          const {data}=await root.axios.post(held.endpoint,held.payload,{timeout:45000});
          committed=true;
          pending.delete(identity);
          this.reportOperation({identity,blocked:false});
          if(this.disposed || this.identity!==identity)return {outcome:'success'};
          this.uncertain=false;this.message=data.message;this.preview={...this.preview,...data};this.rows=[];this.opened=Boolean(held.adjustment);
          this.$nextTick?.(()=>{if(held.adjustment)this.$refs?.dialog?.focus();else if(this.returnFocus?.isConnected)this.returnFocus.focus();});
          this.$emit('reserved',data);
          try {
            const {data:fresh}=await root.axios.get(this.endpoint()+'/manual-inventory',{params:{component_type:this.componentType,match_mode:'auto'},timeout:15000});
            if(this.disposed || this.identity!==identity)return {outcome:'success'};
            this.preview=fresh;this.rows=(fresh.items || []).map(item=>({...item,inputQuantity:'',selected:false}));this.matchMode='auto';this.$emit('reserved',fresh);
          } catch(error) {if(this.identity===identity){this.error=held.adjustment?'用料调整已保存；读取最新用料失败，请重新匹配后继续。':'抵扣已成功；库存列表刷新失败，请点击重新查看，不要重复抵扣。';if(held.adjustment)this.preview=null;}}
          return {outcome:'success'};
        } catch(error) {
          if(committed) {
            if(!this.disposed && this.identity===identity)this.error=held.adjustment?'用料调整已保存；页面刷新失败，请重新查看。':'抵扣已成功；页面刷新失败，请重新查看库存，不要重复抵扣。';
            return {outcome:'success'};
          }
          const status=error.response?.status;
          if(status && status<500)pending.delete(identity);
          if(!this.disposed && this.identity===identity) {
            if(status && status<500) {this.uncertain=false;this.rows=[];this.preview=null;this.error=this.errorText(error)+'；请重新查看库存。';}
            else {this.uncertain=true;this.error=held.adjustment?'用料调整结果暂未确认，请核对原请求。':'抵扣结果暂未确认，请核对上次抵扣结果。';}
          }
          return {outcome:status && status<500?'rejected':'unknown',message:this.errorText(error)};
        } finally {held.inFlight=false;if(this.identity===identity)this.busy=false;this.reportOperation({identity,blocked:pending.has(identity)});}
      }
    },
    template:`<section class="manual-sheet-deduction">
      <button ref="trigger" type="button" class="btn small" :disabled="busy || (disabled && !uncertain)" @click="open">{{uncertain?'核对上次片料操作':allowRequisition?'查看 / 调整片料':'使用库存片料'}}</button>
      <teleport to="body">
        <div v-if="opened" class="manual-sheet-backdrop" @click.self="close" @keydown="keydown">
          <section v-show="!mapLocation" ref="dialog" class="manual-sheet-dialog" role="dialog" aria-modal="true" :aria-labelledby="'sheet-title-'+identity" tabindex="-1">
            <header class="manual-sheet-head"><h3 :id="'sheet-title-'+identity">使用库存片料<span v-if="componentType!=='whole'"> · {{componentType==='cover'?'盖片':'底片'}}</span></h3><button class="btn small" type="button" aria-label="关闭使用库存片料" :disabled="busy" @click="close">关闭</button></header>
            <div class="manual-sheet-body" :aria-busy="loading || busy">
              <div v-if="preview" class="manual-sheet-demand"><strong>{{preview.product_code}} · {{preview.product_name}}</strong><span>所需箱片 {{preview.board_length_mm}} × {{preview.board_width_mm}} mm · {{preview.flute_type==='NONE'?'卡纸':preview.flute_type+' 瓦'}} · {{preview.material_code}}</span><b>待用箱片 {{preview.remaining_required_piece_qty}} 片</b></div>
              <section v-if="preview?.existing_reservations?.length && !uncertain" class="manual-sheet-existing">
                <strong>已安排片料</strong>
                <div v-for="reservation in preview.existing_reservations" :key="reservation.reservation_id" class="manual-sheet-existing-row">
                  <div>{{reservation.board_length_mm}} × {{reservation.board_width_mm}} · {{reservation.material_code}}<small>{{reservation.location_name}}</small></div>
                  <label>保留采用 <input class="input" type="number" min="0" step="1" :max="reservation.remaining_stock_quantity" v-model="reservation.keep_stock_quantity" :disabled="busy || loading || disabled || !reservation.can_adjust" :aria-label="'保留采用张数 '+reservation.reservation_id"> 张</label>
                  <small v-if="reservation.blocked_reason">{{reservation.blocked_reason}}</small>
                </div>
                <p>减少的张数退回可用库存，缺口重新报料；增加采用量请在下方选择可用片料。</p>
                <button class="btn" type="button" :disabled="busy || loading || disabled || !adjustmentValid()" @click="saveAdjustment">保存用料调整</button>
              </section>
              <p v-if="matchMode==='swapped'" class="manual-sheet-swap">长宽交换匹配 · 请核对本次使用方向</p>
              <p v-if="loading" role="status">正在查询匹配片料…</p>
              <p v-if="error" class="status orange" role="alert">{{error}}</p>
              <template v-if="uncertain"><p>原批次和片数已保留，核对后再继续操作。</p><button class="btn primary" type="button" :disabled="busy" @click="submitHeld">{{busy?'正在核对…':'核对上次抵扣结果'}}</button></template>
              <div v-else-if="rows.length" class="manual-sheet-table-wrap"><table class="manual-sheet-table"><thead><tr><th>选择</th><th>库存片料尺寸／材质</th><th>可用数量</th><th>库位</th><th>每张产出</th><th>本次使用</th></tr></thead><tbody>
                <tr v-for="row in rows" :key="row.lot_id" :class="{'manual-sheet-selected':row.selected}">
                  <td><input type="checkbox" v-model="row.selected" :aria-label="'选择 '+row.board_length_mm+' × '+row.board_width_mm+'，'+(row.warehouse_location?.location_name || row.warehouse_location?.name || '位置待核对')" :disabled="busy || !row.selectable || !row.max_stock_quantity" @change="choose(row)"></td>
                  <td><strong>{{row.board_length_mm}} × {{row.board_width_mm}} mm</strong><div>{{row.material_code}} / {{row.flute_type}} · {{row.recommendation_label || '同尺寸'}}</div><details v-if="differences(row) || row.warning_messages?.length" class="manual-sheet-details" :class="{'manual-sheet-review':!!differences(row)}"><summary>{{differences(row)?'需核对：'+differences(row):'查看详情'}}</summary><div>{{row.internal_name || '库存片料'}} · {{row.material_code}} / {{row.flute_type}}</div><p v-for="(warning,index) in row.warning_messages" :key="index">{{warning}}</p></details></td>
                  <td><b>{{row.available_stock_quantity}}</b> 张</td><td><button v-if="row.warehouse_location?.id" type="button" class="manual-sheet-location" :disabled="busy" :title="'在地图定位：'+locationName(row)" @click="openMap(row)">{{locationName(row)}}</button><span v-else>位置待核对</span></td><td>{{yieldText(row)}}<div class="manual-sheet-score" :class="'manual-sheet-score-'+(row.match_color || 'unknown')">{{row.utilization_percent==null?'匹配度待核算':'利用率 '+row.utilization_percent+'%'}}</div><div class="manual-sheet-waste">{{wasteText(row)}}</div></td>
                  <td><label><input class="input" type="number" min="1" step="1" :max="row.actual_max_stock_quantity ?? row.max_stock_quantity" v-model="row.inputQuantity" :disabled="busy || !row.selected" :aria-label="'本次使用张数，最多 '+(row.actual_max_stock_quantity ?? row.max_stock_quantity)+' 张'"> 张</label><small class="manual-sheet-waste">本单建议 {{row.max_stock_quantity}} 张</small></td>
                </tr>
              </tbody></table></div>
              <div v-else-if="!loading && preview && !uncertain" class="manual-sheet-empty">
                <p v-if="!preview.remaining_required_piece_qty">当前用料需求已覆盖，无需再报料。</p><p v-else>{{matchMode==='swapped'?'暂无长宽交换后可用的同楞型片料。':'暂无符合材质楞型及加工用途的可用片料（已查询同尺寸、大张裁切和交换方向）。'}}</p>
                <button v-if="matchMode==='exact' && preview.can_further_match" class="btn" type="button" :disabled="busy" @click="load('swapped')">进一步匹配（交换长宽）</button>
              </div>
              <p v-if="rows.length" class="manual-sheet-explain">同尺寸优先，其次大张裁切、交换方向；组内按利用率推荐。裁余是矩形排版估算，含边料，不含模切内部挖空；可再利用的边料不等于报废。</p>
              <details v-if="preview?.scrap_completion_allowed && (useScrap || (selection().length && totals().uncovered>0)) && !uncertain" class="manual-sheet-special"><summary>少几张，用废料补足（特殊情况）</summary><label><input type="checkbox" v-model="useScrap" :disabled="busy" @change="selectScrap"> 缺少部分已用废料加工完成</label><label v-if="useScrap">合格成品 <input class="input" type="number" min="1" step="1" v-model="scrapQuantity" :disabled="busy" :max="Math.floor(totals().uncovered / Math.max(preview.pieces_per_box,1))"> {{preview.output_unit || '只'}}</label><p>仅填写已经做好的数量；沿用保存货位入库并用于本单。废料材料成本为0，人工沿现有工序计算。尚未完成时不勾选，保留真实缺口。</p></details>
            </div>
            <footer class="manual-sheet-foot"><div v-if="selection().length && !uncertain" class="manual-sheet-total"><strong>本次使用 {{totals().stock}} 张</strong><span>{{preview?.is_bom || componentType!=='whole'?'可出 '+totals().pieces+' 片部件':'可做 '+totals().boxes+' '+(preview?.output_unit || '只')}}</span><b :class="{'manual-sheet-covered':totals().remaining===0}">{{totals().remaining?'剩余 '+totals().remaining+' 片仍需安排':'抵扣后无需报料'}}</b><span v-if="totals().valid && totals().excess">完工后预计余 {{totals().excess}} 片入库存，订单数量不变</span><span v-if="!totals().valid" class="manual-sheet-invalid">请填写可用库存内的正整数；缺口覆盖后，无需再选其他批次。</span></div>
              <div class="manual-sheet-buttons"><button v-if="!uncertain" class="btn" type="button" :disabled="busy || loading" @click="load('auto')">重新匹配</button><button v-if="allowRequisition && preview?.remaining_required_piece_qty>0 && !uncertain" class="btn" type="button" :disabled="busy || loading || disabled || adjustmentChanges().length>0 || selection().length>0" @click="goRequisition">去待报料补差额</button><button class="btn" type="button" :disabled="busy" @click="close">取消</button><button class="btn primary" type="button" :disabled="busy || loading || uncertain || disabled || adjustmentChanges().length>0 || !totals().valid" @click="confirmSelection">{{busy?'正在保存…':'确认使用并抵扣'}}</button></div>
            </footer>
          </section>
          <section v-if="mapLocation" ref="mapDialog" class="manual-sheet-dialog manual-sheet-map" role="dialog" aria-modal="true" aria-label="片料货位定位" tabindex="-1">
            <header class="manual-sheet-head"><h3>{{mapLocation.name}}</h3><button ref="mapBack" class="btn primary" type="button" @click="closeMap">返回片料选择</button></header>
            <iframe :src="mapLocation.url" title="现有仓库地图：片料货位" referrerpolicy="same-origin"></iframe>
          </section>
        </div>
      </teleport>
    </section>`
  };
  root.TMManualSheetDeduction={component,chosenQuantity,pending,pendingForActor,reconcileForActor,physicalComponents,orderTargets,reportedTargets,refreshDraftGroup};
  if (typeof module !== 'undefined') module.exports=root.TMManualSheetDeduction;
})(typeof window !== 'undefined'?window:globalThis);
