# P0-15 全链扫描适配（2026-09-29）

- 工作树：`D:/tm-worktrees/p0-15-scanner-round-20260929`，分支 `codex/p0-15-scanner-round-20260929`，基线 `03554df0`。
- 范围：只读扫描器及匿名合成回归；没有 migration、业务服务修改、历史修复、正式库或网页操作。
- 变更：完工批次沿 `InventoryLotTransfer` 图读取，只有来源、客户/产品身份、数量分解和 `location_transfer` 流水均一致时，关闭的原批次才被认定为合法移库继承；转移目标不再作为第二条完工输出。`manual_in` 可在完工来源身份精确时省略辅助订单字段，冲突字段仍不通过。送货分配以冻结需求分母精确相加；`accept_over` 保持实际发货库存口径，并单列签收差异为 review。
- 验证：审计隔离 runner 下 7 项通过，覆盖合法/伪造移库、无辅助字段的初始入库、冻结分母、签收超量、客户隔离、既有短收/重放和 CLI 合成副本只读；CLI 对新建合成 SQLite 的 SHA-256 与 sidecar 前后不变。
- 已知基线：`test_audit_query_families_do_not_grow_with_unfinished_order_count` 在未改动 `03554df0` 已复现失败（1 单 25、20 单 44 个 SELECT），未作为本次改动归因或掩盖，需单独性能任务处理。
- 状态：开发验证中，尚未正式发布；实际业务历史候选仍需只读人工定性。
