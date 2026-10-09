# 手机取用未知请求安全结束：独立只读方案复核

结论：本轮没有确认新增业务 Bug。现有精确重放、结果核对与未知请求保留有真实验证；缺少的是安全结束出口。拟议终结能力存在程序回退硬门禁缺口，根已决定本轮只交付方案，**不实施、不启用 close**。本结论不阻塞已独立完成的 v602 订单恢复发布。

## 本轮证据与边界

已按 CODEX_START、NAS AI_START、新 TASK、ORDER_FLOW 及仓库接口上下文、总需求9.1/15、执行章程3～9读取。只读固定 `chain-order-audit` 工作树 HEAD `9a3a9f33dfc9e5194017729898f5c4e963c7e904`；7个目标文件实际LF指纹与 API 审查报告一致且仍等于正式108de068对应文件。结束时工作树干净、git diff --check通过。没有修改仓库、提交/push、正式接口POST、正式DB写入、迁移、网页操作或进程清理。

复核 API 审查 `mobile-take-pending-close-audit/REPORT.md`、4个真实HTTP探针结果（4 passed/9.610秒）及5个实际controller边界（5 passed），没有重新跑大组或把这些作者测试冒充独立新测试。实际代码核对 `app/api/mobile_stock_use.py`、`static/ui/mobile-dimension-stock.js`、库存/货物变更模型、客户权限辅助与助手发布兼容实现。

核查时正式 state/current 已为 v0.22.602、包 `9420d69b7eefa3622db0a7dada1bf98bde2ca858ec8dbb932def3b6726396839`。仅只读核对该包 manifest；包内 `desktop_assistant/manager.py` 与审查工作树源逐字LF一致。本报告不替根宣布 NAS 发布或人工验收完成。

## 最小业务范围

仅对本账号已持久的 v2、完整原body、ownerId及expected_actor_id与当前认证一致的未知请求提供“结束这笔待确认请求”。v1无可信owner、缺body/键、损坏记录全部继续只读核对和管理员处理，不增加当前账号接管旧请求的权限路径。当前绿色取消仍只关闭取用框，不等同服务端终结。

sessionStorage只覆盖同标签页刷新，不能发现另设备原请求，不能宣称跨设备原子锁。服务端在原take从未到达时没有原owner登记；expected_actor只证明当前账号声明匹配认证，不能证明未见键过去属于谁。最小方案可冻结首次已授权终结的actor/body，依赖高熵原key、完整本地v2和当前执行/客户权限；若要求先验历史owner证明，需另加服务器签名/登记，不在当前小闭环内。

## 事务与结果合同

1. close与take必须在任何原key/批次决策前取得同一SQLite `BEGIN IMMEDIATE` 写锁，且共享原key事实检查。take先拿锁并提交，则close只回精确原consume；close先拿锁并写持久marker后，迟到take在同锁中检查marker，永久拒绝该键。not_recorded、409、版本变化、浏览器取消均不能代替marker。
2. 精确已成功结果优先，不能生成终结记录或反向冲销。不同actor或异body占用原key：close/take返回Preserve冲突，不给Rejected、不清缓存；原resolve已有按当前客户权限展示历史操作者的能力保持原合同，不改成执行/接管授权。异常发现成功流水与closed同时存在时保留冲突人工核对，不能静默选择一个。
3. close仅写marker和同事务业务审计，不改InventoryLot数量、版本、时间、预占、栈板或库存流水。当前版本过期/货位停用/库存不足不阻止零库存变化的终结；仍必须有warehouse.execute及当前批次客户范围。marker重放同时核终结时冻结客户与当前批次范围；冻结字段应诚实称“终结时客户事实”，不能冒充从未登记的原请求历史归属。公共无客户库存沿现有仅无限客户权限规则。
4. 相同close重放回同closed证明；异actor、异lot、异原body、损坏marker或既有命名空间冲突一律Preserve。marker写入/审计/commit任一步失败全部回滚；commit已成功但回执丢失，后续resolve能读closed。未知close仍保留原body/key；只有完整匹配的closed或精确completed可结束本地待确认状态。

closed回执应有独立schema、status、原raw key/lot、终结actor与current_actor、完整规范化原body或可精确核对的证明、closed_id/时间和终结时客户事实；没有伪movement_id。UI须单独验证closed，不能复用“取用成功”流程。缓存清理失败仍保留终结事实，禁止再次take；确认后刷新最新库存，员工显式开始新操作才生成新key。界面只表示“当前库未见该键成功流水且该键今后被拒绝”，不宣称实体未拿货或历史从未发生。

## WarehouseGoodsMutation复用可行性及限制

现表有唯一 `idempotency_key`（100）、request_hash（64）、lot_id及response_json，可容纳专用marker，但没有原生全局consume/marker跨表排他约束。若后续选择复用，建议内部键为 `mobile-take-close-v1:` + SHA256(raw take key)，仅按raw key派生，不按actor或lot分槽，否则换actor/lot可能绕开原take的全局key占用。原take键仍是 `mobile-take:<raw>`；规范化签名必须沿 `_take_signature` 包含actor、lot、数量、用途、库存/货位版本，并独立核expected_actor。

其他货物API接收任意80/100字符key，没有真正保留的命名空间；必须用专属schema/action标识、域分离request_hash及raw key/lot/actor完整字段复核。发现已存在非marker行不能覆盖或误当closed。唯一冲突应回Preserve。禁止调用 `warehouse_goods.record()`：它会改主档资料且自行commit；需专用helper直接写marker，按 `append_audit_event` 同事务提交。复用表仅减少DDL，不等于已有终结语义或回退安全。

## 正式启用的阻断门槛

源码及当前正式包证明现有部署门禁**不会识别该新语义**：

- `desktop_assistant/manager.py:62` 的 compatible仅检查硬编码订单库存/报价/共享库存等capability，然后在约109行对同revision直接返回True。新marker放现有表不改变revision，旧程序会获准启动/回退。
- 当前 `build.py:150` 签名reader_capabilities没有 mobile_take_close；仅添加一个新manifest字段也无效，因为既有Manager未消费它。
- `schema_contract.py:217` 静态契约核必要表/列，无法发现现有JSON里的新终结事实。schema_authority只管跨revision兼容，不能补足同revision语义门禁。
- Manager.update/rollback/start及失败自动回退依赖compatible。先发识别底座再启用close只能照顾一次直接previous，不能保证任意旧版本、旧助手或恢复流程都理解marker。

因此不能仅凭文档宣称“永久结束”。下一实施须单列发布契约任务：安装助手对已激活终结事实执行签名reader硬门禁，覆盖同revision更新/显式回退/启动/启动失败回退/备份与恢复，且禁止回退到忽略marker的代码。可参考现有order_inventory_activation模式，但现成模式没有新能力的可配置开关，须真正实现并验证。若承诺旧代码绕开助手直连数据库也不能再执行，则需DB级不可重取用约束/迁移，不属于当前无迁移小修。

## 后续最短验收要求

独立契约门禁完成后才进入功能实施卡。最短验收为：take先锁/close先锁两个真实并发顺序；已成功不终结、异actor/异body拒绝；marker和审计故障回滚、提交后丢ack恢复；旧/当前客户范围和无execute账号；过期版本终结但库存/预占/版本零变化；完整closed校验及迟到账号/存储失败不误清；实际安装助手拒绝所有未声明正确能力的同revision回退并保留数据库。原take数量守恒和BOM/预占约束保留，v1/缺body不自动结束。

本轮状态为“只读方案复核完成，发现未来正式启用门槛，终结接口/marker/UI均尚未实现”。不新增Bug计数，不标记正式或人工验收通过。
