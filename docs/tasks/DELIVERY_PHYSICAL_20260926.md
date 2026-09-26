# 送货双数量、历史校正及模具登记

授权：老板2026-09-26要求按补充方案顺序实施并发布。单代理；每个子闭环独立验证与发布。正式业务更正限已核实清单，4043报料与4042实收差1片不自动入账。暂不盘点，不将理论余额称实盘。原审计与补充方案在NAS对应独立回执。

## 顺序及当前状态

1. 打印HTTP兼容与失败重试：v499技术发布完成，资源/健康/完整性门禁通过，待管理员人工验收。详见DELIVERY_PRINT_HTTP_20260926发布回执。
2. 客户/实物双数量：待实施，必须同时覆盖入库、订单、备库直送、快照、拿货、出库、撤销与财务口径。
3. 历史校正：待新鲜副本逐批次演练。旧审计2982+2021-1700=3303，仅当时理论数；2661独立搬入保留。差1片待实收证据。凭证、流水、成本、幂等及取消链完整后受控执行。
4. 出库后补库提示：待实施，复用现有待报料，重复打印不生成；原阈值单位待核。
5. 模具：待复核并登记，研光119/光洋105缺失候选、YL4复用、31待核；R04，SKU标签、产品名简写，不自动打印。

全局完成要求：隔离数量验收全部通过、实际发布、只读健康/资源/数据库检查、可追溯数据回执及管理员最短验收入口。不能以打印子任务完成宣告总目标完成。

## 打印子闭环证据

正式起点v497，Git 30e73dc7，运行包44358c59，数据库dz0922。本次不迁移、不写业务数据。
复用主页面兼容幂等键算法供打印页及管理员设计页加载；打印失败保留相同请求键，同文档并发合并、变更文档/价显独立键、成功后下一次打印新键。只POST原customer-print-events接口。
验证：verify_delivery_print_retry.cjs通过；隔离Chrome实际HTTP下secure=false、randomUUID=undefined，确认打印1次、登记1次、无报错；17项客户资料/模板/加载恢复测试通过。恢复测试补齐浏览器事件及CustomerDeliveryPrint.controls桩，不修改业务预期。
截图和JSON：D:/.codex/workspace_artifacts/delivery_physical_20260926/print。

发布门禁发现并修补：v498新operation-key.js未加app/main.py静态路由，正式资源检查404，因此未宣告交付、未发布NAS latest。v499添加原静态路由循环，新增真实create_app的TestClient测试通过（不用仅挂业务router的模板fixture）；不以拦截资源的Chrome测试替代服务路由验证。发布最终状态另见回执。

## 下一闭环已定位的接口（不可漏改）

- app/api/deliveries.py：DeliveryLineCreate、_validate_delivery_source_contract、_collect_unordered_finished_lines、创建/编辑/已发货修订、_pick_unordered_location_plan、拿货应用picked_quantity回写delivered_quantity、响应及actual_goods_rows。现有计划量/拿货量常被直接当客户量，必须逐入口拆分。
- app/services/unordered_finished_delivery.py：发货planned总量校验、取消、回单短收/编辑重扣必须同一实物快照。不能只改发货校验。
- app/services/external_packaging_receiving.py：备库实收quantity被折为converted_quantity，再作为actual_inventory_quantity；计划进度仍可按客户单位，但真实入库必须保留采购实物及奇数余片。
- app/services/direct_external_finished.py：订单纯外购也先折客户量入库，预占credited与reserved同量；必须与备库一致修复。保留旧已收识别和未入仓历史门禁。
- app/services/semi_finished_inventory.py：consume_delivery_item_inventory将consumed_stock作为客户累计；app/services/warehouse_inventory.py consume_finished_reservation将stock同时写credited_requirement。已有两种字段应复用，不能另建库存账。匹配、预占、撤销、订单可送量需要检查同一比例。
- app/models/delivery.py缺独立数量换算快照；库存FinishedGoodsInventoryDetail已有physical_basis_json，预占已有reserved_stock_quantity/credited_requirement_quantity。新增字段/校正台账若需migration应单独演练，不修改历史快照。
- app/services/stock_replenishment.py receive_replenishment_item支持计划量与actual_inventory_quantity，但目前约束实际量>=计划量；通用比例、单次零完整单位实收、价格守恒须测试。
- 17:59新鲜只读副本before-dual-quantity.sqlite3完整性ok/FK0/head dz0922，00006仍2982。旧收料子批次639–644每批原300，645原221；现余644=100、645=221。搬入656–661各300、662=861。完整迁移血缘、费用来源仍须在执行候选中核验，不凭本记录直接写库。
- 原始采购批次部分成本3.94、部分NULL，搬入批次7.71；不可把所有现有单位成本机械除2，须核对采购冻结总金额和既有成本补充事件。
