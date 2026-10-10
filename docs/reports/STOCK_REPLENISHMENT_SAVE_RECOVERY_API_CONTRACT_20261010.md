# 补库主保存与只读恢复合同 v1（待实施）

2026-10-10；基于 v611 / aa70b997 与只读根 fbcd3dff。此文是下一实施合同，不表示新 API 已存在。现状真实 HTTP 在同目录 plain.json、external.json、physical-adjacent.json、employee.json、precommit.json。

## 1. 范围及证明含义

主写仍为 `POST /api/requisition/stock-replenishment/orders`，正常及精确重放仍 201。现有业务响应字段保持，追加 `save_receipt` 与 `current_actor_id`；原保存状态与当前收料/作废状态分列。原键重复不自动打印。新增只读 `POST /api/requisition/stock-replenishment/save-results/{idempotency_key}/resolve`，任何分支 `Cache-Control: no-store`，无业务 flush/commit/审计写；现有权限拒绝安全审计照常。

仅主保存 CBR/CBW 来源 namespace：suffix 为 SHA256(UTF-8原key) 前20位大写十六进制；CBR=customer_request、CBW=stock_warning，不依赖当天日期。匹配 full actor + normalized payload hash，direct external 另核持久 batch 的完整原key与 source_order_id 一致。不接受 SW 自动预警 plan_hash、RAW reviewed_hash、physical quantity_contract hash 冒充主请求 hash；不补旧 NULL hash。

这是“完整原请求与现存持久来源事实匹配”的证明，**不是服务器曾冻结首响应 JSON**，也不是当前采购/库存尚未变化的证明。request echo 来自本次调用者提供的原body，经旧模型规范化且持久hash严格匹配后返回，不声称数据库保存了原body JSON。

physical 专用 `POST /stock-replenishment/orders/{id}/external-purchase` 仍独立二段确认，本卡只相邻保护，不自动重发。员工 `POST /api/business-approvals` 是申请 pending；applied 结果中的业务来源通常仍 draft，不将员工申请回执当正式采购完成。

## 2. 请求及旧 v611 hash 兼容

主写原body不改，只增可选 `expected_actor_id`：严格正整数，bool/字符串均拒绝；缺失兼容旧客户端。恢复请求：

```json
{"expected_actor_id":1,"original_request":{"idempotency_key":"原key","source_type":"customer_request","items":["完整原行"]}}
```

resolve 的 expected_actor_id 必填且等于本次认证 user.id；original_request 内若有 expected_actor_id 也须相等。路径key与原body经既有trim规范化的key严格相同。历史 created_by 必须是本次认证actor；expected字段本身不证明历史身份。账号不匹配409并 Preserve、Actor-Mismatch，绝不 Rejected。

hash 严格沿用：`canonical_purchase_purpose_hash({actor_id:user.id,payload:old_model_dump(mode='json',exclude_none=False)})`。expected_actor_id 与任何新增传输/readback元字段明确排除；不得使 v611 已成功请求 hash 改变。旧hash算法含 idempotency_key。规范 JSON 为 ensure_ascii=False、sort_keys=True、compact separators、UTF-8 SHA256。列表保留原顺序（重复同产品不同请求行合法，不按产品去重）。

原body完整保留，不由当前草稿重新生成。规范回显仅旧模型字段，extra字段按既有 Pydantic extra-ignore，不以 extra 写入hash。顶层固定字段：replenishment_plan=null、replenishment_plans=[]、source_type=manual_history、idempotency_key=null、supplier_name=null、customer_id=null、remark=null、stock_now=false、items。有效主保存仍按旧业务仅 customer_request/stock_warning、key非空、stock_now=false；模型默认不是新增业务许可。

每行固定字段（未设为null，明确默认另列）：stock_policy_id、procurement_mode、target_inventory_type、product_id、reference_product_id、customer_id、material_id、product_code、product_name、internal_name、material_code、layer_count、flute_type、report_length_mm、report_width_mm、crease_type、crease_left_mm、crease_middle_mm、crease_right_mm、sheet_type=raw_board、component_type=whole、pieces_per_box=1、stock_yield_per_sheet=1、quantity、location_id、historical_workbook、historical_sheet、historical_row、historical_search_text、remark、external_purchase_quantity、external_purchase_unit、external_order_quantity_basis、external_purchase_quantity_basis。target_inventory_type/quantity必填；整数和尺寸继续旧模型转换，不为shape验证新增null限制。

既有validator：source_type trim/lower；key trim/空转null；flute trim/upper；毛→毛片、净→净料；净料→net_sheet、毛片→raw_board，毛片/净料/其他清除残留压线段；压线要求三段并→creased_sheet。尺寸沿 SheetDimension 现规则。Decimal 经过 model_dump(json) 为字符串，**不擅自把 `"11"` 与 `"11.000000"` 视作相同旧hash**；hash canonical 不会重写已经是字符串的Decimal。UI对原body逐字段按这些旧模型规则规范化比较，不宽泛忽略未知 receipt 字段。

## 3. 回包

首写/精确重放附加：`current_actor_id: positive int`，`save_receipt: Receipt|null`。所有完整回执构造/校验在提交前同一回滚边界完成；提交后只发送plain响应，减少已commit却序列化失败。原业务库存/审批/采购算法不变。

resolve 200：

```json
{"status":"completed","current_actor_id":1,"idempotency_key":"原key","request_match":true,"save_receipt":{},"current":{},"trace":null}
```

`status` 仅 completed/not_recorded/trace。completed 要求全部证明成立；not_recorded 时 request_match=false、save_receipt/current/trace=null，只表示查询时点未见记录，可能原POST仍在途，不能清key或换key。trace 有安全原单定位但证明不完整：request_match可为true（已匹配完整hash）或false（旧无hash），save_receipt=null，current可非敏感摘要；原请求继续保留，不重建。异payload/hash冲突409+Preserve，不伪装not_recorded。

Receipt 固定字段：

- `schema_version:1`；`idempotency_key:string`；`actor_id:positive int`；`request_hash:64 lowercase hex`；`request:完整旧规范化body`；`request_match:true`。
- `outcome: "draft_saved" | "direct_external_purchase_confirmed"`。普通来源即使后来confirmed/stocked/voided，原outcome仍draft_saved。
- `source:{id,order_number,source_type,created_by,created_at,customer_id,supplier_name,remark}`。身份ID须存在且自洽；customer/supplier/remark允许模型合法nullable，缺失原必要身份不能用当前主档补。order_number必须符合上述namespace。
- `lines:[]` 按原请求行顺序，每行 `request_index:0-based int`、`source_item_id:positive int`、`requested_quantity:positive int`、`saved_quantity:positive int`、`stock_policy_id/product_id/reference_product_id/customer_id/material_id`（模型允许nullable）、`target_inventory_type/procurement_route`、原持久code/name/material/dimensions/crease/sheet/component/pieces_per_box/stock_yield_per_sheet/remark`。这些直接读取来源列，不重新prepare，不引用可变当前产品单位。逐项真实转换对应，不硬要求 product_id=原body（半成品可为null且reference为原产品）。缺行/多行/身份漂移/SET NULL导致原匹配无法证明时trace或scope拒绝。
- `external_purchase:null|{batch_id,idempotency_key,source_order_id,purchase_order_ids,lines:[]}`。direct外购才非null；lines按 source_item_id 对应，含 purchase_order_id、purchase_item_id、external_product_id_snapshot、purchase_quantity、purchase_unit、order_quantity_basis、purchase_quantity_basis、converted_source_quantity。Decimal用数据库有效十进制字符串；不回当前主档价格/比例。`saved_quantity=converted_source_quantity=floor(purchase_quantity*order_quantity_basis/purchase_quantity_basis)`；请求quantity是建议量，override时不硬等实存量。外购必须核源line/purchase item/batch所有关联一致。
- 不声明已冻结商品工艺单位；purchase_unit来自持久采购行，来源数量是来源列本来的计量，展示不得凭当前产品名伪造原单位。成本/金额不进入证明的必需字段。

`current` 固定为 `{source_id,status,stocked_quantity,items:[{source_item_id,stocked_quantity,receipt_progress}],external_purchase_status:null|摘要}`；这些是当前事实，不参与原成功匹配，收料/取消/作废不改变原完成证明。trace固定 `{source_id,order_number,url}`，url为本客户可访问的原来源GET/实际报料历史入口，不要求trace GET成功才确认已有完整证明。

## 4. 当前权限、客户与资格

本轮保守不扩恢复阅读权限：plain仍 requisition.execute；direct external仍当前execute+admin+成本查看，所有普通/IntegrityError/inner replay均保 v611 门禁。撤权403+Preserve，禁止返回成本/不存在假象；更细 view-only 恢复留后续独立权限合同。员工申请仍其当前申请/查看/所有权合同。

readonly scope 独立按持久 order/item customer 与仍存在关联对象客户身份核验（含跨客户与客户范围），不复用 `_stock_replenishment_item_customer_id` 当前active/fresh门禁。不因产品/预警/供应商停用、当前价格变化或原单收料/作废重新跑新建资格。有关联缺失/客户迁移或不一致且无法证明范围时trace/拒绝，不以当前master补原事实。新建资格只用于真正新单；续发POST按既有执行门禁，但已成功同key不重prepare当前报价。

## 5. 首次明确拒绝出口（实施必须证明阶段）

默认所有无法确定结果：`X-Stock-Replenishment-Preserve:1` + no-store。仅当前actor/key/规范原request绑定、同写锁已查原key确无记录、从未越过commit、业务事务完整rollback成功的fresh失败，可返回结构化 `{detail,save_result:{status:"not_saved",current_actor_id,idempotency_key,request_hash}}` 与 `X-Stock-Replenishment-Rejected:1`，**移除Preserve**。原单存在后的异载荷/权限冲突、commit可能完成/回执构造失败、回滚不确定均不得not_saved。不能仅凭4xx/5xx打标。

Pydantic 422发生handler前，可说明本次输入尚未进入handler，但不证明同key历史不存在；不用通用422释放键。UI本地仍fresh、从未unknown时，可依据请求校验错误允许更正草稿；若已建立待发原记录，仅在收到上述完整绑定not_saved后释放/更正。如希望handler前schema422也释放，必须新增专用校验阶段协议/请求身份完整核验，不能用全局异常器臆造hash。当前可先在首发前本地检验必填类型，未标422保留并提供核对操作。

**曾unknown记录，即使之后not_saved/Rejected/401/403/409，也永远保留原key/body**。not_saved仅证明本次fresh事务，不能证明之前在途POST不会执行；本卡无永久终止/注销协议。UI同actor+独立key保存原完整body，pending阻止同源新保存；账号切换冻结原记录；有限等待后仍unknown；只读核对不隐式POST。

## 6. 下一实施最短验证

v611旧hash跨版精确重放；普通多行/同产品重复行索引映射；direct外购override冻结floor；已保存后收料/void/inactive且scope用户可核对；CBR/CBW与SW/RAW/physical隔离；旧null/缺行/SET NULL不猜；actor/pathkey/body冲突；fresh安全rollback独占Rejected对照commit丢ackPreserve；只读成功/业务冲突无DML；审批applied重放零执行。当前5项仅现状观察，不称上述新合同验收已通过。
