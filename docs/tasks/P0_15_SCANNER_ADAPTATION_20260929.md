# P0-15 全链扫描适配（2026-09-29）

- 工作树：`D:/tm-worktrees/p0-15-scanner-round-20260929`，分支 `codex/p0-15-scanner-round-20260929`，基线 `03554df0`。
- 范围：只读扫描器、其复用的只读状态投影及匿名合成回归；没有 migration、交易写服务修改、历史修复、正式库或网页操作。
- 变更：完工批次沿 `InventoryLotTransfer` 图读取。关闭的原批次只有在来源、客户/产品身份、来源/目标批次与库位、五类库存余额流水连续、移库版本递增、单父图结构和逐边分解一致时，才被认定为合法移库继承。根批次按真实写入来源冻结数量：`production_completion` 核对 `actual_output_quantity`，`production_surplus` 核对 `stock_quantity`；因此当前 direct 实物入库的 `stock_quantity=0` 不再产生零数量误报，历史 split 的库存部分仍只核对 stock。终态数量允许有逐笔、五余额连续且数量相符的盘点 `adjust`，未知异动仍使图无效。叶批次后续库位变化只接受唯一当前托盘、确认时间位于 transfer 之后、起止库位连续、版本逐次加一且终点一致的托盘移动证据；证据缺口降为 review，明确矛盾仍为 error。只有实际属于已核验主图的 `source_type=transfer` 批次可从重复完工输出中排除，独立伪标签仍报错。`manual_in` 可在完工来源身份精确时省略辅助订单字段，冲突字段仍不通过。送货分配以冻结需求分母精确相加；`accept_over` 的库存口径仍为实际发货量，100 发货/101 签收单列 review，完成行不再因早退漏报。持久 `delivered` 与等待回单、待对账、待开票、待收款、完成等后续业务投影视为相容，早期阻塞仍报状态差异。
- 共享状态投影：`order_business_status.py` 只把 Product 查询改为无阈值批量预取，消除 20 条订单时的逐行 lazy SELECT；没有改变任何状态推导、数量或业务门禁。`delivered` 相容后续财务阶段仅是本扫描器的告警比较规则。
- 红灯：审计目录 `D:/ERP-AUDIT/20260929-comprehensive/p0-15-sol-review` 在修复前稳定复现叶子目标批次库位不符、移库流水偷改 consumed、100 发货/101 签收 review 被完成行早退吞掉；查询诊断确认 1 单 25、20 单 44 个 SELECT 的 19 条增长全部来自逐明细 Product lazy load。未改动 `03554df0` 同样复现 25/44，证明原性能失败是基线真实问题。第二轮只读快照复扫又复现 direct 完工 `stock_quantity=0` 被当成期望零、合法盘点差异被粗终态等式拒绝、已确认托盘移位未进入证据图；新增两个脱敏用例修复前均同时报活动批次缺失、重复输出和图无效。
- 验证：隔离 runner 完整 `tests/test_p0_15_incomplete_order_chain.py` 为 31 通过；覆盖 direct 实物入库、split surplus、合法/伪造移库、关闭无移库、错流水、活动坏边、叶子错库位、合法/伪造盘点、托盘移动时间与版本缺口三态、非移动余额篡改、双来源合并、独立伪 transfer、部分/两级/同 ID 整批移位、后续消耗、冻结分母及 accept_over。查询诊断为 1 单 25、20 单 25 个 SELECT。只读一致性副本复扫由原 74 项（21 error、53 review）变为 55 项（2 条 error 告警、53 review）；202、349、385 三条指定链和十条 direct 零数量误报均消失，保留的 2 条 error 都是原完工投入超过有效收料告警，仍待业务核对，不能称作已确认业务错误。`tests/test_p0_order_business_status.py` 11 通过；真实仓库服务用例 `test_reserved_transfer_copies_packaging_and_preserves_reserved_totals` 通过。集成侧已补齐报价夹具并确认 `test_audit_order_projection_optimization.py` 5 通过。隔离器只允许审计临时目录写入；TestClient 适配器仅放行本机 loopback 自管 socket，继续阻断外网及子进程。
- 工具限制：当前虚拟环境没有 `ruff`，已用 `py_compile`、`git diff --check` 与上述回归代替；不把缺少静态工具写成通过。
- 状态：本轮 bounded 修复及隔离验证完成，尚未正式发布；55 项扫描提示仍只是 2 条 error 告警和 53 条 review，需按业务证据继续定性，不能据此自动修历史数据。

## 最终集成与独立接受

Sol最终提交8a75bde72a6bfa15046a68fc7abc002eab72dfac，集成f0d03b82；Terra独立13+3通过，root最终组合52通过。最终只读复扫证据round-tests/075758114333，55提示（2 error级告警、53 review），哈希/sidecar不变，未打开或修复正式库。状态：隔离开发与技术验收通过，待人工验收/待上线。
