# 手工片料UI串行交接（Sol → 根）

本子任务未修改static/index.html。已有可独立采用组件static/ui/manual-sheet-deduction.js，hash 8d35263a6974。可从10be6028仅提取static/index.html的diff作为5个基础入口及父保存互斥方法参考；不能整包合并旧commit。

## 接口与事件

普通订单GET/POST：/api/warehouse/semi-finished/order-items/{order_item_id}/manual-inventory与manual-reserve；BOM对应bom-components/{snapshot_id}。GET params component_type=whole/cover/base，纯读取不生成需求。

组件：`<manual-sheet-deduction :actor-id="Number(user?.id || 0)" :order-item-id="Number(真实订单明细ID)" :component-type="真实冻结组件" :disabled="manualSheetDeductionDisabled()" @operation-state="setManualSheetOperation($event)" @reserved="refreshManualBomSheetDeduction(对应行,$event)"></manual-sheet-deduction>`；BOM将order-item-id替换snapshot-id。不能用存货编码定位，不按编码合并父子产品。

GET响应包含required_piece_quantity、remaining_required_piece_qty（扣承诺后的手工可用缺口）、committed_piece_qty、库存覆盖、每只耗片数、尺寸/材质、候选实际可用/预占版本、max_stock_quantity与review_hash。POST真实用户stock_quantity、review_hash、库存version、订单revision、确认全部warning_codes、唯一idempotency_key。成功响应有internal_requisition_changes（前后qty/version/status）、committed_piece_qty及剩余缺口、reservation_ids；重复请求原回执replayed=true。填数不得默认全部库存。

## 必须连接的实际使用入口

1. 已保存普通订单详情：不依赖待报料列表是否返回该明细；只要客户权限+warehouse.reserve，入口可查询真实阶段，不能被requisition_status“已报料”或笼统can_requisition隐藏。已采购对应部分仍展示承诺量/0上限，GET返回真实阶段；结档/完工显示后端明确409。
2. BOM已保存订单：逐实体子件和whole/cover/base键连接快照ID，A有库存可独立用，不能按整父件已有报料锁全部。
3. 原patch的待报料普通行、普通草稿、BOM pending子件、供应商草稿来源子件、内部BOM草稿5入口。需去除`source.can_requisition`对手工入口的笼统门禁；它只控制创建采购，不代表已内部登记的片料可用性。
4. 内部已登记报料详情也应按真实order_item_id（或BomSource snapshot_id）开同组件；不要求删单重录，不改变收料入口。

## 根需处理的更新逻辑

- script引用/static/ui/manual-sheet-deduction.js?v=8d35263a6974，并register app.component("manual-sheet-deduction",TMManualSheetDeduction.component)。
- data.manualSheetOperations={}；setManualSheetOperation按identity保存blocked；requisitionInventoryActionBlocked并入Object.values(...).some(Boolean)，保存期间相互禁用。
- reserved后刷新订单详情与内部报料明细；普通供应商草稿调用既有refreshSupplierRequisitionDraftAfterInventoryReservation；BOM重新POST bom-sheet-cutting/preview更新目的数量/fingerprint并保留合法remark与stock_purpose_sheet_qty。刷新失败标记_manual_sheet_refresh_error，requisitionBatchLinePayload须拒绝旧草稿保存。采用100后若order_purpose=0且无备库量，应去掉该采购行，不造0张采购；合法额外备库量独立保留。
- 显示“已承诺 X片 / 未合并仍需 Y片 / 当前可用张数 / 本次采用张数”，避免把尚未报料与已采购混为同一状态。
- helper遇超时保留原载荷；核对按钮重试原key，不换key。不确定时父草稿提交禁用；成功后刷新失败不得再写一次。

## 验证交接

独立helper Node6项已通过（操作数量/重试/刷新失败/旧预览拒绝/互斥/账号隔离）。tests/manual_sheet_deduction_20261013.cjs中的2个index整合测试待根连接后运行：Vue模板编译/原5入口，以及BOM目的预览刷新保备注额外备库；若新增订单详情入口后测试数应按新UI真实数量更新，不能继续硬编码5。最终需要实际既有普通订单+内部报料未合并入口检验，不能只依赖新草稿可显示。
