<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { request, type Product } from '../api/client'
import { useAuthStore } from '../stores/auth'

type Member = { product_id:number; customer_name:string; code:string; name:string; unit:string; spec:string; material:string; mold_label:string; process:string; role_label?:string; enabled?:boolean }
type Lot = {lot_id:number;number:string;product_id:number;available:number;reserved:number;location:string;shared:boolean;eligible:boolean;reason:string|null}
type Group = {group_id:number;version:number;enabled:boolean;auto_enroll:boolean;identity_issue:string|null;evidence:string;products:Member[];lots:Lot[];batch_count:number;pending_count:number}
type SharedProduct = Product & { customer_id:number; code?:string; name?:string; group_id:number|null; drawing_number?:string|null; spec:string; material:string; role_label:string; reason?:string|null }
type CustomerChoice = {customer_id:number;customer_name:string;missing:boolean;products:SharedProduct[]}
const productResults = ref<SharedProduct[]>([])
const customerChoices = ref<CustomerChoice[]>([])
const sourceProduct = ref<SharedProduct|null>(null)
const editingMembers = ref(false)
const customerKeyword = ref('')
const disabledProducts = ref<number[]>([])
const auth = useAuthStore()
const prefix = '/api/warehouse/finished/shared-stock'
const groups = ref<Group[]>([])
const keyword = ref('')
const busy = ref(false)
const error = ref('')
const current = ref<Group|null>(null)
const creating = ref(false)
const selectedProducts = ref<SharedProduct[]>([])
const lots = ref<Lot[]>([])
const selectedLots = ref<number[]>([])
const enabled = ref(true)
const automatic = ref(false)
const preview = ref<{products:Member[];lot_count:number;hash:string;action:string}|null>(null)
const evidence = ref('')
const acknowledged = ref(false)
let pending: {url:string;args:Record<string,unknown>;operation_key:string}|null = null
async function run(task:()=>Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await task() } catch (e) { error.value = e instanceof Error ? e.message : '操作失败，请刷新核对' }
  finally { busy.value = false }
}
async function loadGroups() {
  const [g,p] = await Promise.all([
    request<{items:Group[]}>(prefix + '/groups?keyword=' + encodeURIComponent(keyword.value)),
    request<{items:SharedProduct[]}>(prefix + '/products?keyword=' + encodeURIComponent(keyword.value)),
  ])
  groups.value=g.items;productResults.value=p.items
}
async function openGroup(id:number) {
  await run(async()=>{
    customerSequence++;customerChoices.value=[];sourceProduct.value=null
    current.value = await request<Group>(prefix + '/groups/' + id)
    creating.value = false; editingMembers.value=false; selectedLots.value = []; lots.value = current.value.lots
    enabled.value = current.value.enabled; automatic.value = current.value.auto_enroll
  })
}
function newGroup() {
  current.value = null; creating.value = true; editingMembers.value=false; sourceProduct.value=null; customerChoices.value=[]; selectedProducts.value = []
  lots.value = []; selectedLots.value = []; error.value = ''
}
let customerSequence = 0
async function loadCustomers() {
  if(!sourceProduct.value)return
  const sequence=++customerSequence
  const sourceId=sourceProduct.value.id
  const value=await request<{items:CustomerChoice[]}>(prefix+'/products/'+sourceId+'/customers?keyword='+encodeURIComponent(customerKeyword.value))
  if(sequence===customerSequence && sourceProduct.value?.id===sourceId)customerChoices.value=value.items
}
async function startSharing(product:SharedProduct) {
  if(product.group_id){await openGroup(product.group_id);await editMembers(product);return}
  newGroup();automatic.value=true;sourceProduct.value=product;selectedProducts.value=[product]
  await run(async()=>{
    lots.value=(await request<{items:Lot[]}>(prefix+'/product-lots/'+product.id)).items
    selectedLots.value=lots.value.filter(l=>l.eligible).map(l=>l.lot_id)
    await loadCustomers()
  })
}
async function editMembers(product?:SharedProduct) {
  if(!current.value)return
  editingMembers.value=true;customerKeyword.value=''
  selectedProducts.value=current.value.products.map(p=>({...p,id:p.product_id,product_code:p.code,product_name:p.name,customer_id:0,is_active:true,group_id:current.value!.group_id}) as SharedProduct)
  disabledProducts.value=current.value.products.filter(p=>p.enabled===false).map(p=>p.product_id)
  await run(async()=>{
    const products=(await request<{items:SharedProduct[]}>(prefix+'/products?keyword='+encodeURIComponent(current.value!.products[0].code))).items
    // Resolve by stable product ID, never the first same-code identity.
    sourceProduct.value=product || products.find(p=>p.id===current.value!.products[0].product_id) || null
    if(!sourceProduct.value)throw new Error('原产品当前不可用，请核对资料后再追加客户；仍可在下方暂停共用')
    for(const row of selectedProducts.value){const actual=products.find(p=>p.id===row.id);if(actual)row.customer_id=actual.customer_id}
    await loadCustomers()
  })
}
function customerSelected(customer:CustomerChoice) {
  return selectedProducts.value.find(p=>customer.products.some(x=>x.id===p.id))?.id
}
async function chooseCustomer(customer:CustomerChoice,id?:number) {
  const previous=selectedProducts.value.find(p=>customer.products.some(x=>x.id===p.id))
  if(previous && current.value?.products.some(p=>p.product_id===previous.id))return
  if(previous)removeProduct(previous.id)
  const candidate=customer.products.find(p=>p.id===id)
  if(!candidate)return
  if(selectedProducts.value.some(p=>p.id===candidate.id))return
  await run(async()=>{
    selectedProducts.value.push({...candidate,product_code:candidate.code||candidate.product_code,product_name:candidate.name||candidate.product_name} as SharedProduct)
    if(creating.value){const rows=(await request<{items:Lot[]}>(prefix+'/product-lots/'+candidate.id)).items;lots.value.push(...rows);selectedLots.value.push(...rows.filter(l=>l.eligible).map(l=>l.lot_id))}
  })
}
function removeProduct(id:number) {
  selectedProducts.value = selectedProducts.value.filter(p=>p.id!==id)
  const removed = new Set(lots.value.filter(l=>l.product_id===id).map(l=>l.lot_id))
  lots.value = lots.value.filter(l=>l.product_id!==id)
  selectedLots.value = selectedLots.value.filter(id=>!removed.has(id))
}
function productLabel(id:number) {
  const member = current.value?.products.find(p=>p.product_id===id)
  if(member) return member.customer_name + ' · ' + member.code
  const product = selectedProducts.value.find(p=>p.id===id)
  return product ? product.customer_name + ' · ' + product.product_code : String(id)
}
async function prepare(action:'create'|'add_lots'|'configure'|'members') {
  await run(async()=>{
    const args:Record<string,unknown> = action==='create'
      ? {product_ids:selectedProducts.value.map(p=>p.id),lot_ids:[...selectedLots.value],auto_enroll:automatic.value}
      : {action,expected_version:current.value!.version,
          ...(action==='add_lots'?{lot_ids:[...selectedLots.value]}:action==='members'?{product_ids:selectedProducts.value.map(p=>p.id),disabled_product_ids:[...disabledProducts.value]}:{enabled:enabled.value,auto_enroll:automatic.value})}
    const base = action==='create'?prefix:prefix+'/groups/'+current.value!.group_id
    const value = await request<any>(base+'/preview',{method:'POST',body:JSON.stringify(args)})
    const source = action==='create'?value:value.value
    const products = source?.products.map((p:any)=>{
      const b = JSON.parse(p.identity_json).basis
      return {product_id:p.product_id,customer_name:current.value?.products.find(x=>x.product_id===p.product_id)?.customer_name || selectedProducts.value.find(x=>x.id===p.product_id)?.customer_name,
        code:p.code,name:p.name,unit:b.unit,spec:b.spec,material:b.material,mold_id:b.mold_tool_id,process:b.production_process,enabled:action==='members'?!disabledProducts.value.includes(p.product_id):true}
    }) || current.value!.products
    const reviewProducts=products.map((p:any)=>({...p,enabled:action==='members'?!disabledProducts.value.includes(p.product_id):p.enabled}))
    preview.value = {products:reviewProducts,lot_count:source?.lots.length||0,hash:value.preview_hash,action}
    pending={url:base+'/confirm',args,operation_key:'shared-'+(globalThis.crypto.randomUUID?.() || Date.now()+'-'+Math.random().toString(36).slice(2))}
    evidence.value='';acknowledged.value=false
  })
}
async function save() {
  if (!preview.value || !pending || !evidence.value.trim() || !acknowledged.value) return
  await run(async()=>{
    const result = await request<{group_id:number}>(pending!.url,{method:'POST',body:JSON.stringify({
      ...pending!.args,preview_hash:preview.value!.hash,operation_key:pending!.operation_key,
      evidence:evidence.value.trim(),physical_match_confirmed:true,
    })})
    preview.value=null;pending=null
    current.value=await request<Group>(prefix+'/groups/'+result.group_id)
    editingMembers.value=false;customerChoices.value=[];sourceProduct.value=null
    creating.value=false;lots.value=current.value.lots;selectedLots.value=[]
    enabled.value=current.value.enabled;automatic.value=current.value.auto_enroll
    await loadGroups();ElMessage.success('共用设置已保存并回读确认')
  })
}
onMounted(()=>{if(auth.user?.role==='admin') void run(loadGroups)})
</script>

<template>
  <section class="shared-page">
    <div class="heading"><div><h1>库存共用</h1><p>确认可以互换的成品或 BOM 零件，多个客户或部门从同一货位取货，共用一份可用余额；先成功预占的订单先占用。</p></div><router-link to="/master-data">返回基础资料</router-link></div>
    <el-alert v-if="auth.user?.role!=='admin'" title="库存共用设置由管理员维护。" type="info" :closable="false" />
    <template v-else>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <div class="toolbar"><el-input v-model="keyword" placeholder="输入存货编码，查产品并设置共用" clearable @keyup.enter="run(loadGroups)" /><el-button :disabled="busy" @click="run(loadGroups)">查询 / 刷新</el-button></div>
      <el-table v-if="keyword.trim()" :data="productResults" border empty-text="没有匹配的有效产品，请检查编码或名称">
        <el-table-column label="客户 / 部门" prop="customer_name" min-width="180"/>
        <el-table-column label="存货编码" prop="product_code" width="130"/>
        <el-table-column label="名称" prop="product_name" min-width="150"/>
        <el-table-column label="图号" prop="drawing_number" min-width="140"/><el-table-column label="类型" prop="role_label" width="110"/>
        <el-table-column label="规格" prop="spec" min-width="160"/>
        <el-table-column label="材质" prop="material" width="100"/>
        <el-table-column label="操作" width="150"><template #default="{row}"><el-button type="primary" link :disabled="busy" @click="startSharing(row)">{{row.group_id?'管理共用客户':'设置共用'}}</el-button></template></el-table-column>
      </el-table>
      <h2>已设置的共用产品</h2>
      <el-table :data="groups" border v-loading="busy" empty-text="暂无共用产品，先在上方搜索存货编码">
        <el-table-column label="客户与产品" min-width="330"><template #default="{row}"><div v-for="p in row.products" :key="p.product_id">{{p.customer_name}} · {{p.code}} · {{p.name}}</div></template></el-table-column>
        <el-table-column label="状态" width="100"><template #default="{row}">{{row.enabled?'共用中':'已暂停'}}</template></el-table-column>
        <el-table-column label="共用批次" prop="batch_count" width="100"/>
        <el-table-column label="待核实批次" prop="pending_count" width="110"/>
        <el-table-column label="新入库" width="130"><template #default="{row}">{{row.auto_enroll?'符合条件自动加入':'逐批确认'}}</template></el-table-column>
        <el-table-column label="操作" width="120"><template #default="{row}"><el-button link type="primary" :disabled="busy" @click="openGroup(row.group_id)">查看 / 维护</el-button></template></el-table-column>
      </el-table>
      <section v-if="current || creating" class="detail">
        <h2>{{creating?'设置共用客户':'共用产品 · '+current!.products.map(p=>p.code).join(' / ')}}</h2>
        <section v-if="creating || editingMembers">
          <p>选择可取用此款实物的客户或部门。系统按同码查找各客户产品；同码的整套、子件及不同印刷不能混选。送货抬头和各客户价格继续按原单据取值。</p>
          <div class="selected" v-for="p in selectedProducts" :key="p.id">
            <span>{{p.customer_name}} · {{p.product_code}} · {{p.product_name}}</span>
            <el-checkbox v-if="editingMembers && current?.products.some(x=>x.product_id===p.id)" :model-value="!disabledProducts.includes(p.id)" :disabled="busy" @change="(value:any)=>{disabledProducts=value?disabledProducts.filter(id=>id!==p.id):[...disabledProducts,p.id]}">允许新增共用预占</el-checkbox>
            <el-button v-else-if="p.id!==sourceProduct?.id" link :disabled="busy" @click="removeProduct(p.id)">移除</el-button>
          </div>
          <div class="toolbar"><el-input v-model="customerKeyword" placeholder="搜索客户或部门名称" clearable @keyup.enter="run(loadCustomers)"/><el-button :disabled="busy" @click="run(loadCustomers)">查找客户</el-button></div>
          <el-table :data="customerChoices" border empty-text="未找到可见客户">
            <el-table-column label="客户 / 部门" prop="customer_name" min-width="180"/>
            <el-table-column label="同码产品 / 实物身份" min-width="350"><template #default="{row}">
              <span v-if="row.missing" class="note">缺少此存货编码的产品资料，补齐后再选；不会自动创建产品。</span>
              <el-select v-else :model-value="customerSelected(row)" :disabled="busy || row.products.some((p:any)=>current?.products.some(x=>x.product_id===p.id)) || row.products.some((p:any)=>p.id===sourceProduct?.id)" clearable placeholder="选择对应产品" style="width:100%" @change="(id:number)=>chooseCustomer(row,id)">
                <el-option v-for="p in row.products" :key="p.id" :value="p.id" :disabled="!!p.reason" :label="p.role_label+' · '+p.name+' · '+p.spec+(p.reason?' · '+p.reason:'')"/>
              </el-select>
              <div v-for="p in row.products.filter((p:any)=>p.reason)" :key="p.id" class="note">{{p.role_label}}：{{p.reason}}</div>
            </template></el-table-column>
          </el-table>
          <p class="note">列表最多显示100家，可输入客户名称缩小范围。已有客户通过取消“允许新增共用预占”停用；原身份和已预占订单保留。</p>
          <el-checkbox v-if="creating" v-model="automatic" :disabled="busy">符合条件的新入库自动加入共用</el-checkbox>
          <el-button v-if="editingMembers" type="primary" :disabled="busy" @click="prepare('members')">核对并保存共用客户</el-button>
        </section>
        <template v-if="!creating">

          <el-alert v-if="current!.identity_issue" type="warning" :closable="false" :title="current!.identity_issue" />
          <el-table :data="current!.products" border>
            <el-table-column label="类型" prop="role_label" width="100"/>
            <el-table-column label="客户" prop="customer_name" min-width="190"/><el-table-column label="新预占" width="100"><template #default="{row}">{{row.enabled===false?'已暂停':'允许'}}</template></el-table-column><el-table-column label="编码" prop="code" width="120"/><el-table-column label="名称" prop="name" min-width="120"/>
            <el-table-column label="规格" prop="spec" min-width="150"/><el-table-column label="材质" prop="material" width="100"/><el-table-column label="单位" prop="unit" width="65"/><el-table-column label="共用模具" prop="mold_label" min-width="120"/>
          </el-table>
          <el-button :disabled="busy" @click="editMembers()">追加 / 停用共用客户</el-button>
          <div class="settings"><el-checkbox v-model="enabled" :disabled="busy">启用库存共用</el-checkbox><el-checkbox v-model="automatic" :disabled="busy">符合条件的新入库自动加入</el-checkbox><el-button :disabled="busy" @click="prepare('configure')">预览设置调整</el-button></div>
          <p class="note">符合条件的新成品、已完成加工的 BOM 零件及实际组装成套库存可自动加入。订单预占保留；历史、资料变化、未加工纸板或外购批次需另行核实。整套仅在实际组装后入库，不会因设置共用而增加。暂停后停止新的共用，已有预占仍按原单完成或撤销。</p>
        </template>
        <h3>库存批次</h3>
        <el-table :data="lots" border empty-text="当前没有成品库存，可以先建立共用关联">
          <el-table-column width="60" label="选择"><template #default="{row}"><input type="checkbox" v-model="selectedLots" :value="row.lot_id" :disabled="busy || !row.eligible" :aria-label="'选择批次 '+row.number" /></template></el-table-column>
          <el-table-column label="批次 / 原归属" min-width="190"><template #default="{row}">{{row.number}}<div class="note">{{productLabel(row.product_id)}}</div></template></el-table-column>
          <el-table-column label="货位" prop="location" min-width="190"/><el-table-column label="可用" prop="available" width="85"/><el-table-column label="已预占" prop="reserved" width="85"/>
          <el-table-column label="共用情况" min-width="220"><template #default="{row}">{{row.shared?'已加入本组':row.reason || '可选择加入'}}</template></el-table-column>
        </el-table>
        <div class="toolbar"><el-button type="primary" :disabled="busy || (creating?selectedProducts.length<2:selectedLots.length===0 || !!current?.identity_issue)" @click="prepare(creating?'create':'add_lots')">{{creating?'预览并建立共用组':'预览追加所选批次'}}</el-button><span class="note">库存仍保留原归属、成本和历史，数量只记一份。</span></div>
      </section>
      <el-dialog :model-value="!!preview" title="核对共用操作" width="min(920px, 94vw)" :close-on-click-modal="false" :before-close="(done:()=>void)=>{if(!busy){preview=null;pending=null;done()}}">
        <template v-if="preview">
          <el-alert :closable="false" type="info" :title="preview.action==='members' ? '将保存所列客户的共用状态；已有预占保留，新加入客户不会复制库存' : preview.action==='configure' ? ('将'+(enabled?'启用':'暂停')+'共用；后续合格新入库'+(automatic?'自动加入':'逐批确认')) : ('本次'+(preview.action==='create'?'建立共用组':'追加库存')+'，选择 '+preview.lot_count+' 个批次')" />
          <el-table :data="preview.products" border><el-table-column v-if="preview.action==='members'" label="新预占" width="100"><template #default="{row}">{{row.enabled===false?'暂停':'允许'}}</template></el-table-column><el-table-column label="客户" prop="customer_name" min-width="180"/><el-table-column label="编码" prop="code" width="115"/><el-table-column label="名称" prop="name"/><el-table-column label="规格" prop="spec" min-width="135"/><el-table-column label="材质" prop="material" width="90"/><el-table-column label="工艺" prop="process" min-width="130"/></el-table>
          <el-input v-model="evidence" type="textarea" :maxlength="1000" :disabled="busy" placeholder="记录现场核实或调整依据，例如：两客户确认无专用印刷，实物规格一致" class="evidence"/>
          <el-checkbox v-model="acknowledged" :disabled="busy">我已核对所列产品、实物批次和本次共用设置</el-checkbox>
          <el-alert v-if="error" :title="error" type="error" :closable="false"/>
        </template>
        <template #footer><el-button :disabled="busy" @click="preview=null;pending=null">取消</el-button><el-button type="primary" :loading="busy" :disabled="!acknowledged || !evidence.trim()" @click="save">确认保存</el-button></template>
      </el-dialog>
    </template>
  </section>
</template>

<style scoped>
.shared-page{padding:22px;max-width:1500px;margin:auto}.heading{display:flex;justify-content:space-between;align-items:center}.heading h1{margin:0;font-size:24px}.heading p,.note{color:#64748b;font-size:13px;line-height:1.6}.toolbar,.settings{display:flex;gap:12px;align-items:center;margin:18px 0;flex-wrap:wrap}.toolbar .el-input{max-width:350px}.detail{margin-top:28px;padding:22px;background:white;border:1px solid #dce3ed;border-radius:8px}.selected{display:flex;justify-content:space-between;margin:10px 0;padding:8px;background:#f1f5f9}.evidence{margin:18px 0}.el-table{margin-top:12px}h2{font-size:19px}h3{font-size:16px}
</style>
