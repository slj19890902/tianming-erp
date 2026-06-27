# Phase 1 执行状态：送货单状态流闭环

分支：`feature/phase1-delivery-statusflow`（从 `factory-current-baseline` 切出）
状态：独立评审发现并修复并发问题，待提交/合并授权
最近更新：2026-06-27

## 1. 范围与状态机（用户 2026-06-27 拍板）

- 未保存表单：前端草稿，不入库。
- 保存后：`pending`，可编辑、可删除。
- 确认发货：`dispatched`。
- 取消发货：恢复为 `pending`，回滚已送数量并重算订单状态，清除发货人/发货时间/打印状态。
- 已签收、已对账：由回单/对账关联推导，**不**写入送货状态。
- **本阶段不新增 `draft/cancelled` 数据库状态** → 维持 `pending/dispatched` 两态。
- 有回单禁止取消；已对账优先提示禁止；本阶段不开放反审核。
- 所有写操作事务化并记录操作日志。

## 2. 是否需要数据库迁移

**否。** 本次未改任何 ORM 模型/表结构。`sales_deliveries.status` 仍为 `pending/dispatched`，
现有 `CheckConstraint("status IN ('pending','dispatched')")` 已覆盖。
因此 Phase 1 不产生 alembic 迁移，正式库结构无需变更，原计划的「Step 6 主库结构迁移授权」可省去。

## 3. 后端实现

改动文件：`app/api/deliveries.py`、`app/api/finance.py`。

- 新增 `DeliveryUpdate` 请求模型（明细非空、不可重复）。
- 抽出共享助手 `_collect_delivery_lines()`：创建与编辑共用的明细校验（客户归属、
  已收料、未结案、剩余量、超送警告），消除重复，`create_delivery` 已重构复用。
- `PUT /api/deliveries/{id}`（编辑，admin/sales）：仅 `pending` 可编辑；保留送货单号；
  先通过条件写锁定 `pending` 状态，再整体替换明细并重算 `total_quantity`；
  不改动订单已送数量；事务 + 审计 `UPDATE`。
- `DELETE /api/deliveries/{id}`（删除，admin/sales）：仅 `pending` 可删除；
  先通过条件写锁定 `pending` 状态，避免与确认发货同时成功；级联删除明细；
  不改动订单已送数量；事务 + 审计 `DELETE`。
- `PUT /api/deliveries/{id}/cancel`（取消发货，admin/sales）：
  - 门禁顺序：① 已进入对账 → 409「已进入对账，必须先反审核…」；② 已有 confirmed 回单 → 409「已有回单…」。
  - 并发防护：先用条件 UPDATE 抢占 `dispatched → pending` 并取得写锁，再检查门禁；
    门禁失败则整笔 rollback，状态仍为 `dispatched`。
  - 回滚：逐条 `delivered_quantity -= line.delivered_quantity`，带守卫 `delivered_quantity >= line` 防负/防重复回滚。
  - 重算订单状态（复用 `_refresh_order_status`）；事务 + 审计 `CANCEL_DISPATCH`。
- `POST /api/finance/return_receipts`：在读取送货单和写回单前，对同一
  `dispatched` 送货单执行条件写锁定，保证“取消发货”和“确认回单”不能同时成功。
- `PUT /api/deliveries/{id}/printed`：改为带 `status='dispatched'` 条件的原子更新，
  避免取消发货后又写回打印状态。

## 4. 前端最小入口（static/index.html，维护模式）

按架构决策，旧单页仅加必需按钮（完整 UI 留待 tm_frontend 迁移）：

- 送货列表 `pending` 行：新增「编辑」「删除」。
- 送货列表 `dispatched` 行：新增「取消发货」（红色，二次确认）。
- 「编辑」复用新增送货单模态（预填明细/数量/备注/车号/日期，编辑态锁定客户）。
- 明细备注可查看和修改，编辑日期或车号时不会被清空。
- 新增方法 `editDelivery / deleteDelivery / cancelDelivery`，提交分支区分 PUT/POST。
- JS 语法 `node --check` 通过。

## 5. 测试（tests/test_phase1_delivery_statusflow.py，19 例）

覆盖：编辑（成功/发货后拒绝/角色）、删除（成功不动数量/发货后拒绝/角色）、
取消发货（回滚+订单状态/重复取消阻断/事务原子性/回单门禁/对账门禁/角色）、
编辑与发货并发、删除与发货并发、取消与回单并发、审计日志、
前端操作入口和明细备注保留。

- Phase 1 + 送货/财务 + 前端回归：**93 passed**（带 `ERP_DATABASE_PATH` 重定向）。
- 三组关键并发场景各连续执行 30 轮，均未出现双成功、数量错账或明细漂移。
- 独立评审前的全量测试：**721 passed, 14 failed, 31 skipped**（带重定向）。
- 独立修复后的最终全量测试：**726 passed, 14 failed, 31 skipped**（391s，带重定向）；
  新增测试全部通过，失败集合与修复前一致。
- 14 个失败**全部与送货无关且为既有环境问题**，已证明非本次回归：
  - 8 × `test_phase18_pdf_training` + 3 × `test_phase19_tianhua_ocr`：
    测试启动的子进程继承了隔离用 `ERP_DATABASE_PATH`，但该临时库未执行完整迁移，
    因而缺少 `users.display_name`、PDF 训练表和约束；不是送货代码回归。
  - `test_start_script_uses_complete_backend_entrypoint` + `test_phase13_deployment...`：项目根 `.env` 文件不存在。
  - `test_phase1_compatibility_backend_no_longer_defaults_to_test_database`：被测试环境的 `ERP_DATABASE_PATH` 重定向带偏（读 `phase1_postgres.database` 常量）。
  - **硬证明**：`git stash` 暂存本次全部改动（分支无提交，stash 后即基线内容），用**完全相同的 env** 重跑这 14 个 → 同样 `14 failed, 60 passed`。即基线本身这 14 个就失败，与送货状态流无关。

## 6. 副本演练（211MB 真实数据副本，scratchpad/sandbox）

`rehearse_sandbox.py` 在真实数据副本上：

- 受控明细：创建 → 编辑（保号、改量、不动订单数量）→ 删除；
- 创建 → 发货（数量+12）→ 取消（精确回滚到 0、状态回 pending、清发货时间）；
- 真实送货单 #1（已对账）取消 → 被门禁 409 拦截；
- 演练后副本 `integrity_check=ok`、`foreign_key_check=0`。
- 结果：**ALL PASS (15/15)**。

## 7. 主库只读保护与一处重要说明

- 全程目标：正式库只读。基线见 `PHASE1_BASELINE_20260627.md`。
- **发现并已处置**：测试/演练中导入 `app.core.database` 时，其模块级全局 `engine`
  默认绑定正式库路径，且 `connect` 监听器执行 `PRAGMA journal_mode=DELETE`。
  正式库原为 WAL 模式，首次连接触发 WAL 合并，**正式库文件字节被重写一次**
  （SHA-256 `02d06b…0fad0` → `669010f7…e991a`，文件大小不变）。
- **逐表核对确认逻辑数据零变化**：所有关键表行数、`integrity_check=ok`、
  `foreign_key_check=0`、`alembic_version=s57m1p9q6r39` 均与基线完全一致；
  `operation_logs` 仍为 172（演练审计写入副本，未入正式库）。属良性物理改写。
- **已建立纪律**：此后所有测试/演练均设 `ERP_DATABASE_PATH` 重定向到临时库；
  设置后重跑 44 例，正式库 SHA-256 保持 `669010f7…e991a` 不再变化（STABLE）。
- 当前正式库新基线 SHA-256：`669010f771fe5b71a7cc4e7671722226a64fb620d607160530423ab40b7e991a`。

## 8. 回滚方式

- 代码：本阶段全部改动在分支 `feature/phase1-delivery-statusflow`，
  `git checkout factory-current-baseline` 即可回退；尚未合并、未推送。
- 数据库：未做任何迁移，无需回滚。
- 正式库：未发生逻辑写入；如需位级一致可从既有备份恢复（数据等价，无业务必要）。

## 9. 验收清单（对照执行计划阶段 1）

| 验收点 | 结果 |
|---|---|
| 待发货可编辑，不改送货单号 | ✅ 测试 + 演练 |
| 待发货可删除，不动订单已送数量 | ✅ 测试 + 演练 |
| 发货只累计一次 | ✅ 既有 phase7 回归 |
| 取消发货精确扣回数量 | ✅ 测试 + 演练 |
| 取消后订单状态正确 | ✅ 测试（partially/pending_delivery） |
| 有回单/对账返回明确中文错误 | ✅ 测试 + 真实数据演练 |
| 操作日志完整（UPDATE/DELETE/CANCEL_DISPATCH） | ✅ 测试 |
| 删除与发货不能同时成功 | ✅ 并发测试，30 轮稳定 |
| 取消与确认回单不能同时成功 | ✅ 并发测试，30 轮稳定 |
| 重复操作被阻断 | ✅ 测试（重复取消 409） |
| 事务原子性（部分失败整体回滚） | ✅ 测试 |

## 10. 待用户决定 / 下一阶段

- 请独立验收：分支代码、`tests/test_phase1_delivery_statusflow.py`、本状态文件、演练结论。
- 验收通过后是否合并到 `factory-current-baseline`（需用户授权 commit/merge）。
- 随后进入 Phase 2（年龄友好 UI + 离线资源，落在 tm_frontend，首迁送货管理）。
