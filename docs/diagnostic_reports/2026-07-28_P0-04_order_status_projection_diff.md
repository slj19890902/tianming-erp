# P0-04 订单持久化状态与统一业务投影只读差异报告

## 1. 核验边界

- 核验日期：2026-07-28
- 代码基线：`85ce933b9b436c7e2dbec549c31c31da2056b381`
- 隔离数据库：
  `D:\tm-uat\p0-04-order-status-unified-20260728\carton_erp_uat.sqlite3`
- 隔离副本原始 SHA-256：
  `241D16773C50E0C36C4A11E06A1B4022E8F6137558907E9D04A0512FFC0E8A0C`
- Alembic revision：`cr74v8x9z63`
- 数据库以 SQLite `mode=ro` 打开；本轮统计没有写入、迁移或修正任何状态。

## 2. 汇总

- 非瑞达历史订单：39 张
- 订单明细：207 条
- 持久化主状态与统一投影不同：31 张
- 统一投影本体耗时：约 0.0105 秒
- 加载订单与完成全部投影共执行固定 14 条 SQL；没有逐订单或逐明细 N+1。

| 持久化主状态 | 派生主状态 | 订单数 | 订单号 |
|---|---|---:|---|
| `delivered` | `completed` | 4 | TM20260630002、TM20260701002、TM20260707002、TM20260710001 |
| `delivered` | `pending_invoice` | 1 | TM20260701003 |
| `partially_delivered` | `pending_incoming` | 1 | TM20260713002 |
| `partially_delivered` | `pending_production` | 11 | TM20260627003、TM20260629004、TM20260630003、TM20260702007、TM20260706002、TM20260704002、TM20260707003、TM20260708001、TM20260709005、TM20260713001、TM20260714002 |
| `pending_delivery` | `pending_production` | 11 | TM20260629003、TM20260701001、TM20260702001、TM20260702002、TM20260702006、TM20260703001、TM20260703002、TM20260714001、TM20260714003、TM20260715002、TM20260716001 |
| `pending_production` | `pending_incoming` | 2 | TM20260602001、TM20260624015 |
| `pending_production` | `pending_material` | 1 | TM20260708003 |

差异集中证明：旧 `sales_orders.status` 是粗粒度兼容快照，不能继续作为页面唯一
真相。P0-04 只读派生真实单据事实，不批量回写旧字段。

## 3. 指定订单 TM20260624015

| 订单号 | 明细系统单号 | 页面序号 | 持久化主状态 | 派生明细状态 | 最小证据 | 差异原因 |
|---|---|---:|---|---|---|---|
| TM20260624015 | TM20260624015-001 | 1 | `pending_production` | `pending_production` | `actual_incoming_received` | 已实收，尚无生产完成事实 |
| TM20260624015 | TM20260624015-002 | 2 | `pending_production` | `pending_incoming` | `confirmed_supplier_requisition` | 有正式报料，尚无有效实收事实 |
| TM20260624015 | TM20260624015-003 | 3 | `pending_production` | `pending_production` | `actual_incoming_received` | 已实收，尚无生产完成事实 |

主状态按三条明细中的最早阻塞阶段聚合为 `pending_incoming`（待收料），不会再由
兄弟明细、遍历顺序或旧主状态覆盖。列表只显示窄序号；完整明细系统单号仅在只读
详情中展示。

## 4. 结论

1. 现有真实单据字段足以完成第一阶段统一投影，无需新增迁移。
2. 历史差异只通过用户可见投影纠正，不修改正式业务数据。
3. 后续若要清理 `sales_orders.status`，必须另立迁移任务并重新审批。
