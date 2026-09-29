# P0-15 全链扫描适配（2026-09-29）

- 工作树：`D:/tm-worktrees/p0-15-scanner-round-20260929`，分支 `codex/p0-15-scanner-round-20260929`，基线 `03554df0`。
- 范围：只读扫描器、其复用的只读状态投影及匿名合成回归；没有 migration、交易写服务修改、历史修复、正式库或网页操作。
- 变更：完工批次沿 `InventoryLotTransfer` 图读取。关闭的原批次只有在来源、客户/产品身份、来源/目标批次与库位、五类库存余额流水连续、移库版本递增、单父图结构、逐边分解及全图终态数量都一致时，才被认定为合法移库继承；整批同批次换库、部分/两级拆批、后续消耗及 split 完工只按 `stock_quantity` 核对。只有实际属于该根图的 `source_type=transfer` 批次可从重复完工输出中排除，独立伪标签仍报错。`manual_in` 可在完工来源身份精确时省略辅助订单字段，冲突字段仍不通过。送货分配以冻结需求分母精确相加；`accept_over` 的库存口径仍为实际发货量，100 发货/101 签收单列 review，完成行不再因早退漏报。持久 `delivered` 与等待回单、待对账、待开票、待收款、完成等后续业务投影视为相容，早期阻塞仍报状态差异。
- 共享状态投影：`order_business_status.py` 只把 Product 查询改为无阈值批量预取，消除 20 条订单时的逐行 lazy SELECT；没有改变任何状态推导、数量或业务门禁。`delivered` 相容后续财务阶段仅是本扫描器的告警比较规则。
- 红灯：审计目录 `D:/ERP-AUDIT/20260929-comprehensive/p0-15-sol-review` 在修复前稳定复现叶子目标批次库位不符、移库流水偷改 consumed、100 发货/101 签收 review 被完成行早退吞掉；查询诊断确认 1 单 25、20 单 44 个 SELECT 的 19 条增长全部来自逐明细 Product lazy load。未改动 `03554df0` 同样复现 25/44，证明原性能失败是基线真实问题。
- 验证：隔离 runner 完整 `tests/test_p0_15_incomplete_order_chain.py` 为 27 通过，性能用例已恢复为固定查询族；覆盖合法/伪造移库、关闭无移库、错流水、活动坏边、叶子错库位、非移动余额篡改、终态总量不平、双来源合并、独立伪 transfer、部分/两级/同 ID 整批移位、后续消耗、split 完工、冻结分母及 accept_over。`tests/test_p0_order_business_status.py` 11 通过；真实仓库服务用例 `test_reserved_transfer_copies_packaging_and_preserves_reserved_totals` 通过。`test_audit_order_projection_optimization.py` 另有 3 个估值夹具失败，未改基线同样失败，原因是合成入库缺成本报价，不归因本轮；其余 2 项通过。隔离器只允许审计临时目录写入；TestClient 适配器仅放行本机 loopback 自管 socket，继续阻断外网及子进程。
- 工具限制：当前虚拟环境没有 `ruff`，已用 `py_compile`、`git diff --check` 与上述回归代替；不把缺少静态工具写成通过。
- 状态：修复及隔离验证完成，尚未正式发布；实际业务历史候选仍需只读人工定性，最终差异需独立复审。
