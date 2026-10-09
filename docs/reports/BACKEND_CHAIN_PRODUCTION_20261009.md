# 生产后端审查修复 2026-10-09

状态：开发验证通过，尚未由本子任务正式发布；交主管整合发布，正式页面待管理员人工验收。

任务：BACKEND-CHAIN-AUDIT-FIX-20261009。以正式 v0.22.592 / 8e50a807b41fbf97032dc24c950346f885e6af16 建立独立 worktree，远端 factory-current-baseline 当前仍为 ec60eb1e，未拿旧远端覆盖正式。分支 codex/chain-production-audit-20261009；提交 93c6d12beb07856f278d382378fb8cdf65526650。

## 确认问题和修复

1. P1：原料已全部加工后，原货位停用、未放置或原栈板已退役，撤销完工仍返回成功并恢复原料。修前接口三个场景均错误返回200。现在在完整事务中先认领原位置，检查位置实际可用及栈板仍为原有效容器；不满足返回409，原料、产出、任务、流水和命令均不改变。正式收料可用的原料暂存区例外仍由原收料来源类型和ID限定，未扩大通用入库权限。
2. P2：独立加工子件实际组套后，冻结配方没有 customer_name，打开含备库的完工历史出现500。修前接口可复现500；现在保留已有冻结名称，缺字段时按记录中的真实 customer_id 只读获取显示名称，不写回历史命令。组套历史、数量、客户均可读取。

仅改 app/services/stock_preparation_history.py；新增 tests/test_chain_production_audit.py。没有更改数量算法、客户权限、冻结BOM、成本、单位、正式库存或历史事实。

## 十类审查矩阵

| 检查类 | 当前结论 | 本轮证据 |
|---|---|---|
| 重复完工、同键异载荷 | 所测场景正常，无新增缺陷 | test_stock_preparation_flow::test_partial_production_replay_and_stock_conservation；test_n029_production_service::test_idempotency_conflict_cas_and_atomic_rollback |
| 任务/批次版本、取消与完整回滚 | 所测场景正常 | test_stock_preparation_flow::test_cancel_stale_overproduction_and_atomic_failure |
| 材料预占、实际投入、数量守恒 | 所测场景正常；新撤销位置漏洞见问题1 | test_auto_receipt_partial_replay_cancel_and_identity；新增失效原位置3场景 |
| 分批投入、余料继续加工与超收 | 所测场景正常 | test_auto_receipt_partial_replay_cancel_and_identity；test_overreceipt_is_visible_raw_and_auto_plan_stays_within_purchase |
| 模数/分切投入成本、双拼折算 | 所测场景正常 | test_sheet_cut_production::test_completion_uses_frozen_physical_input_and_inherits_cost；test_double_splice_sixty_pieces_complete_and_deliver_as_thirty_boxes |
| 组套按真实冻结配比耗料 | 所测场景正常 | test_finished_disposition_consumes_children_and_retains_source_trace；缺子件成本原子回滚用例 |
| 已用/预占/送货下游后的撤销边界 | 原位置恢复有缺陷已修；其他所测下游门禁正常 | test_reversal_atomic_failure_and_downstream_block；test_production_reversal_is_admin_only_and_downstream_change_is_atomic |
| 加工任务冻结身份、当前主档变更 | 所测场景正常 | test_frozen_yield_batch_units_and_continuing_after_product_change |
| 客户范围、管理员/车间权限 | 所测场景正常 | test_scope_and_admin_only；test_group_history_and_safe_reversal_keep_source_facts 的空客户范围 |
| 共用BOM、完工历史和来源追溯 | 共用组套正常；新独立组套历史500已修 | test_real_preparation_auto_enrollment_cross_customer_stock_assembly；test_independent_assembled_bom_history_is_readable |

额外核对旧A06：v592已包含 4c9b8410，completion_input_audit.py 的 receipt-source-units-v2 已按冻结用途、组件、来源和数量单位分析；不再以最后批计划量截断所有累计投入。3项诊断测试通过。本轮未重复修改，也未据扫描更改库存。

## 验证和边界

- 新场景及旧完工历史8通过；十类业务定向回归14通过；A06诊断3通过，共25通过、0失败、0跳过。
- 新测试修前：历史500；停用/未放置/退役栈板撤销均错误200；修后：历史200、非法撤销409，检查库存及命令保持原值。
- 编译、git diff --check通过；Alembic唯一head em1009bs；没有migration或version改动。
- 测试全部使用合成隔离SQLite；未写、复制覆盖或修正正式业务数据库；未运行正式页面自动点击、IAB或推送Git。
- 仅证明已测试场景，不证明ERP全部流程没有Bug。异常旧批次、真实物理位置和管理员实际页面仍需现场核对。
- 证据位于本回执同目录：before.xml、before-pallet-valid.xml、after.xml、risk-matrix.xml、diagnostic.xml。
- 建议管理员整合发布后：①打开独立组套后的完工历史，核对客户/数量；②确认失效原位置提示先核对位置，不直接恢复原料。真实业务不要为测试而停用货位或撤销有效完工。
- 真实Token用量环境未提供，无法获取，不估算。

## 主管复核后补充

追加提交 20cda697135445fffdc1fc9d53d0331566ee7d00：在原材料位置集合排序前显式阻止缺失来源或空货位。正常数据库该字段为非空外键，但异常旧来源投影混合空值和整数时也必须业务阻断。独立注入缺位置投影验证修前500、修后409且整组无写入；同时正常整组撤销再次通过。新增1个场景，本生产任务总计26个不同测试场景通过，不把这一防御修订计为第三个正式业务Bug。

根送货提交1b0c694e独立复核：新增扩展普通编辑幂等时，PO继承或实际日期的后续变化会阻止旧请求重放。tests/test_chain_delivery_review.py（写入主管工作树，文件交主管提交）2个测试在1b0c694e原模块内存加载下均以409失败；主管当前修订后均以200重放原响应且不覆盖当前状态。未改主管API/前端文件。此项为本次候选扩展边界，未计入v592原有Bug。证据review-original.xml与review-before.xml（后者在主管已修后的工作区运行，实际为2通过）。
