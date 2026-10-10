(function (root) {
  'use strict';
  const normalized = lines => (lines || []).map((line, index) => ({
    client_line_id: String(index), product_id: Number(line.product_id || line.matched_product_id || 0),
    quantity: Number(line.quantity || 0)
  })).filter(line => line.product_id > 0);
  const summary = {
    props: ['customerId', 'lines'],
    data: () => ({rows: [], error: '', busy: false, request: 0, timer: null}),
    computed: {signature() { return JSON.stringify([Number(this.customerId), normalized(this.lines)]); }},
    watch: {signature: {immediate: true, handler() {
      clearTimeout(this.timer); ++this.request; this.rows = []; this.error = '';
      this.busy = Boolean(this.customerId && normalized(this.lines).length);
      this.timer = setTimeout(() => this.refresh(), 300);
    }}},
    beforeUnmount() {clearTimeout(this.timer); ++this.request;},
    methods: {reference(row) {return this.lines?.[Number(row.client_line_id)]?.source_stock_reference;}, async refresh() {
      const request = ++this.request, customer_id = Number(this.customerId), items = normalized(this.lines);
      this.rows = []; this.error = '';
      if (!customer_id || !items.length) {this.busy = false; return;}
      if (items.some(i => !Number.isSafeInteger(i.quantity) || i.quantity < 0)) {
        this.error = '请输入非负整数需求数量'; this.busy = false; return;
      }
      this.busy = true;
      try {
        const {data} = await axios.post('/api/orders/demand-stock-preview', {customer_id, items});
        if (request === this.request) this.rows = data.items || [];
      } catch (error) {
        if (request === this.request) this.error = String(error.response?.data?.detail || '库存读取失败，请重试；不能按零库存处理');
      } finally {if (request === this.request) this.busy = false;}
    }},
    template: `<aside class="demand-stock-summary" aria-label="本批需求库存与真实货位">
      <div class="toolbar-group"><strong>库存 / 预计结余 / 实际货位</strong><button class="btn small" type="button" :disabled="busy" @click="refresh">刷新</button></div>
      <small>只读试算，不扣库。结余按可用库存扣除本批需求；保存时仍需核对。</small>
      <div v-if="busy" role="status">正在核对库存…</div><div v-if="error" class="notice danger">{{error}}</div>
      <div v-for="row in rows" :key="row.client_line_id" class="demand-stock-row">
        <strong>{{row.product_code}}</strong> · 本行 {{row.quantity}} {{row.unit}}
        <span :class="['status',row.status==='enough'?'green':'orange']">{{({enough:'充足',shortage:'缺货',empty:'用完',low:'低于预警',review:'待核对'})[row.status]}}</span>
        <div v-if="row.status!=='review'">现存 {{row.on_hand}} / 占用 {{row.reserved}} / 可用 {{row.available}} {{row.physical_unit}}</div>
        <div v-if="row.status!=='review'"><b>本批后剩余 {{row.batch_remaining}} {{row.unit}}</b><span v-if="row.projected_remaining!==row.batch_remaining">（本行后 {{row.projected_remaining}}）</span><span v-if="row.batch_remaining<0"> · 需补 {{-row.batch_remaining}}</span></div>
        <div v-if="reference(row) && reference(row).on_hand!==row.on_hand" class="notice">原单库存 {{reference(row).on_hand}}，与 ERP 现存 {{row.on_hand}} 不同，请核对统计时点与单位；不覆盖库存。</div>
        <div v-if="row.message" class="notice">{{row.message}}</div>
        <div v-for="loc in row.locations" :key="loc.lot_id" class="demand-location"><b>{{loc.location_name}}</b><br>现存 {{loc.on_hand}} · 可用 {{loc.available}} {{row.physical_unit}}<small> · 批次 {{loc.lot_number}}</small></div>
        <div v-if="row.status!=='review' && !row.locations.length" class="muted">未查到该产品可核实的成品库存位置</div>
      </div><div v-if="!busy&&!error&&!rows.length" class="muted">选择常用箱或匹配存货编码后显示。</div>
    </aside>`
  };
  const importer = {
    props: ['customers'], emits: ['parsed'],
    data: () => ({customerId: '', text: '', file: null, busy: false, error: '', request: 0}),
    beforeUnmount() {++this.request;},
    methods: {async preview() {
      const request = ++this.request; this.error = ''; this.busy = true;
      try {
        const form = new FormData();
        if (this.file) form.append('file', this.file); else form.append('text', this.text);
        const {data} = await axios.post('/api/orders/weekly-demand-preview?customer_id='+encodeURIComponent(this.customerId), form);
        if (request === this.request) this.$emit('parsed', data);
      } catch(error) {if (request === this.request) this.error = String(error.response?.data?.detail || '识别失败，请检查文件或粘贴需求文本');}
      finally {if (request === this.request) this.busy = false;}
    }},
    template: `<details class="weekly-demand-import"><summary>周需求单导入（图片 / PDF / Excel / 粘贴）</summary>
      <p>左侧“位置、存货编码、需求数量”作为订单；右侧库存仅作原单参考。导入后需核对客户、编码、数量和交期。</p>
      <label>客户 <select class="select" v-model="customerId" :disabled="busy"><option value="">请选择客户</option><option v-for="c in customers" :key="c.id" :value="c.id">{{c.name}}</option></select></label>
      <input class="input" type="file" :disabled="busy" accept=".jpg,.jpeg,.png,.pdf,.xls,.xlsx" @change="file=$event.target.files[0]||null;text=''">
      <textarea v-if="!file" class="textarea" :disabled="busy" v-model="text" rows="4" placeholder="每行：位置 存货编码 需求数量，例如 A5-2 80010340 30"></textarea>
      <button class="btn primary" type="button" :disabled="busy||!customerId||(!file&&!text.trim())" @click="preview">{{busy?'正在识别…':'识别并进入订单核对'}}</button>
      <button v-if="file" class="btn" type="button" :disabled="busy" @click="file=null">改用粘贴</button><div v-if="error" class="notice danger">{{error}}</div>
    </details>`
  };
  const mixin = {methods: {
    commonBoxDemandLines() {
      const existing = (this.orderForm.items || []).filter(row => row.product_id).map(row => ({product_id: row.product_id, quantity: row.quantity}));
      for (const selection of Object.values(this.orderCommonBoxPicker.selected || {})) {
        const row = existing.find(row => Number(row.product_id)===Number(selection.product.id));
        if (row) row.quantity = selection.quantity; else existing.push({product_id:selection.product.id, quantity:selection.quantity});
      }
      for (const product of this.orderCommonBoxPicker.items || []) {
        if (!existing.some(row => Number(row.product_id)===Number(product.id))) existing.push({product_id:product.id, quantity:0});
      }
      return existing;
    },
    async acceptWeeklyDemand(draft) {
      this.orderImportDrafts = [this.prepareImportDraftReminderState({...draft, confirmed:false,
        _save_status:'idle', _save_message:'', items:(draft.items||[]).map(row=>this.preparePdfImportItem(row))})];
      this.orderImportBatch.status = 'recognized';
      await this.loadOrderImportReminders();
      if (this.modal?.type !== 'orderPdfImport') return;
      await Promise.all(this.orderImportDrafts[0].items.filter(i=>i.matched_product_id).map(i=>this.loadOrderLineInventory(i,draft.matched_customer_id)));
    }
  }};
  root.TMDemandStock = {summary, importer, mixin, normalized};
  if (typeof module !== 'undefined') module.exports = root.TMDemandStock;
})(typeof window !== 'undefined' ? window : globalThis);
