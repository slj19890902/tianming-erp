# 历史已收已送闭合批：提交能力的最小模型设计

本文件为候选设计，不是已实现API。当前只读预览始终返回can_submit=false。9867/227缺真实历史采购单价，用户明确仅保留订单与报料，不允许当前价暂估。

## 为什么普通模型不足

`ProductionCompletion`的direct类型可不指定location/lot，适合表示过去已直接送完且当前无货的产出事实。但`IncomingReceiptPurposeAllocation`约束finished_output_qty_delta>0必须同时关联completion和lot。普通收料又在当前实际地堆或货架形成存货，不能把历史已消耗数量穿过今天的空间占用后再倒填送货日期。

只用OperationLog保存JSON不足：缺少来源级唯一约束、显式外键及旧程序读取门禁，普通撤回仍可能对历史量创建当前返库。因此不能把日志方案当无迁移闭环。

## 两张追加事实表与最小迁移范围

1. `historical_closed_batches`：ID；order_id、supplier_order_id、customer_id；idempotency_key唯一；request_hash、reviewed_hash；完整前值JSON及来源合同JSON；收料业务日、生产业务日、送货业务日；各日期的precision/day及is_inferred/basis；received_at/created_at语义说明；原送货单号；操作者、确认理由、created_at（当前UTC）；产生的receipt_id和delivery_id（唯一且RESTRICT）。本版order_id及supplier_order_id各唯一，避免同批不同幂等键重复承接。
2. `historical_closed_batch_lines`：batch_id、order_item_id、supplier_item_id、purpose_snapshot_id、receipt_item_id、completion_id、delivery_item_id，均适当唯一并RESTRICT；实收张数、实际成品数、已送成品数、单位/产出换算冻结；零期末余量；原采购材料/供应商/尺寸；历史单价、price_unit、currency、tax_included/tax_rate及价格来源文件hash/页/行、确认人、确认时间；未税/税额/含税总成本，精度/公式版本；采购和销售证据hash/行号。

数据库触发器拒绝UPDATE/DELETE，表内约束数量为正、单位明确、零余量、价格>0、hash长度、日期序列。SQLite/PostgreSQL行为一致。更正必须另建专用追加修订，首版不开放。非空禁止有损降级。源单/票据/完工均RESTRICT，不依赖应用层约定保护来源。

复用IncomingReceipt/Item保存可见的正式历史来料，复用ProductionCompletion保存实际历史产出、direct且lot/location为空。历史批表提供特有来源与成本合同，不能谎称普通receipt_auto当前形成库存。需为完成来源新增清楚的historical_backfill分类或统一来源投影，不能让界面显示为今天在仓生产。

采购价格不能走当前主档价格自动冻结。若复用SupplierReceiptPriceFact，必须增加明确`historical_evidence`来源及历史批行关联和相应约束；现有native/historical_master_adoption不能冒名复用。保留现有税费和单位约束，冻结“此时登记的旧合同”，不将当前Material.version冒充过去版本。收料为历史业务日，created_at为现在；未知具体时分秒只保留日期精度，不宣称历史操作时间。

## 未来提交事务

1. 权限：活动管理员/被明确授予历史修复权限者，同时满足收料、送货、采购价格确认、客户范围。预览与提交使用相同授权判断。不能仅信前端隐藏按钮。
2. 用真实外层事务锁住Order→SupplierOrder→全部来源明细，重读完整预览、无下游集合和相同客户PO/原送货号查重。CAS在采购明细版本及受影响订单明细原状态/数量上执行；摘要覆盖全部持久值和源合同。别的会话已经收料、预占、改价、改数量或加来源，整批拒绝。
3. 同键同请求先读既有历史批返回；同键异请求、同订单/来源换键重复、跨客户重放拒绝。先保护权限再读回，禁止幂等成为越权读取入口。
4. 只在真实历史价齐全且完整同批实收=产出=已送、无BOM/双片/模数换算/余料时写入：历史收料及历史价格来源、历史实际产出、原日期已发货Delivery/Items、独立历史批及审计。订单采购数量保持，订单实际收料/已送累计只增加一次；所有关联在同事务完成。
5. 不创建当前InventoryLot/InventoryReservation/InventoryMovement/Pallet/SpaceOccupancy；也不借现有lot消耗。过去的流入/产出/流出及净0由独立追加台账表达。不得使用普通发货API消耗今天库存。
6. 客户签字未知：不代签、不创建confirmed回单；未来通过既有回单及对账流程确认实际单据。原文件送货依据可供开待签回单，但推算日期不能变成真实确认人/签字。用户未授权本次开票或收款时不产生票款。
7. 任一步失败（含最后审计/记录幂等）回滚全部事实。无自动重试变更请求或换幂等键。返回全链ID、源合同、期间与净库存0证明。

## 必须同时完成的读取/回退合同

- 收料历史/采购内部trace识别历史批行，展示原业务日期和当前录入时间，显示无当前存货，采购用途已履行而不是“尚待收料”。
- 生产任务同一订单只有一个，已完工数量由历史completion体现；库存/货位视图不得把direct-null completion变成未知位置待上架或待入库。
- 送货详情、订单完成状态、月度履约统计按实际送货日；没有lot不应退回到按当前预计成本估算。
- 材料成本追溯将历史批行作为独立actual source，按实际送货期间确认一次；供应商对账候选只从历史收料和真实旧价格合同取一次，不由前端再补一笔应付。客户回单/对账只产生一次应收。
- 普通收料/生产撤销、发货撤回/修订/取消、订单回退与采购取消遇到该历史批拒绝，提示需专用历史更正。不允许对从未进入当前仓库的历史量做返库。现有旧单行为完全保持。
- 有历史批事实后旧程序回退读取须阻断；不能让旧版本按普通来源缺失重新自动收料/生产/发货。

## 实施与测试边界

约两张表、一个迁移（及价格origin约束适配）、一个领域事务服务、API、历史读取/成本/撤销适配，共约10–14生产文件和3–5测试文件，UI由主任务另整合。当前候选仅实现独立只读资格预览，无迁移、无提交、未注册生产app。

完整提交必须覆盖：实际隔离SQLite重复提交/同键异值/并发两会话、来源版本变化、未知或当前参考价拒绝、日期推算标识、晚期失败全回滚、普通链无变化、当前全库存及空间逐表净0、采购计划不变、供应商和客户财务均不重复、旧程序/普通撤销门禁。预览测试不能替代这些提交验证。
