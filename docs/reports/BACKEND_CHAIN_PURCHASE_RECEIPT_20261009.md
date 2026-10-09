# 报料与收料后端审查修复候选

状态：开发验证通过，尚未正式发布；交主管整合发布。正式网页、打印和现场操作待管理员验收。

- 任务：BACKEND-CHAIN-AUDIT-FIX-20261009 的报料、收料子任务。
- 起点：正式 v0.22.592 / 8e50a807b41fbf97032dc24c950346f885e6af16。
- 候选：147dc5c0db1ef25b2069d6bd8bc886f96571ddc1，分支 codex/chain-purchase-receipt-audit。
- 树：D:\.codex\worktrees\chain-purchase-receipt-audit\纸箱厂erp软件搭建。
- 无迁移，唯一 Alembic head 为 em1009bs。未发布、未 push、未读写正式业务库。测试全部使用合成临时数据库。

## 已确认问题与修复

|编号|触发与修前证据|影响|修复|
|---|---|---|---|
|P02|旧报料编辑接口对 cancelled 订单和强制结档行仍返回200，800×200被改成900×250|终止业务仍改写采购需求|锁定订单后重新读取状态，阻止终止、结档及已有送货的原报料编辑|
|P03|旧采购部分实收40/100，material_status仍pending，编辑接口返回200并改变尺寸|首次收料后的合同和后续生产可能漂移|查询实际有效收料，部分收料也冻结原报料；保留原用途冻结门禁|
|R01|外购收料首次成功后，把当前客户范围改为空，同键重放返回200及原收料明细|失去客户权限后仍可读取收料|读取采购来源并先核验当前客户范围，再允许幂等返回|
|R02|补库100先收60；第二次省略数量按剩余40入库，再原样重试返回409|员工无法确认已成功的补齐收料，可能误另建请求|用该笔原始累计数还原当时待收40，保持同键同载荷返回原事实|
|R03|同批提交sr1和sr01，各10，旧接口返回成功2条，实收20|同一明细别名绕过批量去重|按来源前缀和数值ID判重，第二条失败，实收仅10；其他有效行保持原部分成功合同|
|R04|已撤销的单条收料仍被同键重试作为成功返回；真实冻结收料批量入库后撤销，再重试仍回放旧posted/入库成功缓存|界面告知已入库但正式事实已撤销|单条检查单头及明细状态；批量缓存返回前核验实际有效明细，已撤销/缺失明确拒绝并给出历史核对及新请求指引|
|R05|有成本权限时收料成功后撤销cost.view，同批次重试仍返回order_cost、sheet_cost等缓存|权限回收后泄露成本|缓存重放再次按当前成本权限去除成本字段，保留数量和位置|

P02、P03为两类报料缺陷；R01～R05为五类收料缺陷，合计7类。已撤销单头/明细以及取消/结档等参数用例不是重复计数的独立Bug。

## 报料10项审查矩阵

|编号|审查点|结果与证据|
|---|---|---|
|P01|客户范围与跨客户用途|原保护有效。customer_scope_blocks_preview_and_finalize_before_master_validation通过；用途分配器要求同一客户|
|P02|取消、结档及送货后的旧编辑|确认问题并修复；test_chain_purchase_guards的取消和结档参数由200变409且尺寸保持不变|
|P03|部分收料后合同冻结|确认问题并修复；test_chain_purchase_guards的received_partially参数由200变409|
|P04|采购数量正整数、非法值及用途守恒|用途分配器拒绝bool、非有限、负数和非整数；gold_samples和tamper_or_stale系列通过|
|P05|重复物理来源与重复取整|seen_source_keys、来源键唯一约束存在；same_purchase_spec_cannot_be_split_into_two_lines_to_round_twice通过|
|P06|成品、纸片、采购张数及开料换算|gold_samples、merges_two_tens_before_single_ceiling、cutting_helper_recomputes_from_immutable_base_without_accumulation通过|
|P07|预览过期/提交篡改|tamper_or_stale_rejects_without_any_formal_mutation通过，未写采购或用途事实|
|P08|同键重放/异载荷/异操作者|pure_idempotency_guard_binds_hash_and_actor与idempotency_replay_binds_payload_hash_and_actor通过|
|P09|价格主档变化是否追改正式采购|confirmed_price_snapshot_does_not_follow_later_price_change通过|
|P10|采购、用途、审计及提交失败原子性|audit_or_commit_failure_rolls_back_purchase_and_purpose_together两参数通过|

## 收料10项审查矩阵

|编号|审查点|结果与证据|
|---|---|---|
|R01|幂等重放时当前客户权限|确认问题并修复；新API回归200变403；customer_scope_is_checked_before_price_or_purpose_details_leak也通过|
|R02|分批默认补齐与相同请求重试|确认问题并修复；60+默认40=100，仅两条事实，原样重试成功且无重复|
|R03|数组内同一来源的文本别名|确认问题并修复；sr1/sr01仅一条成功、一条重复提示|
|R04|撤销后单条及缓存批次重试|确认问题并修复；单头/明细状态两参数，以及真实收料→撤销→批次重试均拒绝；原撤销动作自身同键重放仍通过|
|R05|重放缓存的当前成本权限|确认问题并修复；撤销cost.view后重试移除成本字段，数量仍40|
|R06|分批、超收、离散单位与多行事务|external_packaging_receiving中partial_then_complete、partial_order_target_closure和multi_line_atomic三项通过；补库原短收/超收规则未改变|
|R07|收料数量到成品/备库的单位换算|one_sheet_two_forms_500_finished_and_only_50_reserve_sheets通过|
|R08|冻结材质尺寸与单位价格成本守恒|square_meter_price_uses_frozen_dimensions_and_cost_conserves通过|
|R09|无可用正式位置、审计异常时整体回滚|unavailable_required_location_fails_entire_receipt_without_fallback及audit_failure_rolls_back_receipt_allocation_completion_and_inventory通过|
|R10|撤销前后续预占使用与幂等资格|reversal_blocks_when_reserve_lot_is_used_by_a_later_order、receive_idempotency_binds_payload_and_actor_without_double_counting、frozen_receipt_revert_replays_same_actor_payload_and_key通过|

## 修复策略与验证结果

集中修复入口合同：旧报料编辑与收料共享同一订单行写锁；幂等重试保留原事实，但每次仍检查当下权限和有效状态；批量按真实数值身份去重。没有修改历史库存、单据或价格，也没有重新计算正式数据。

修前证据：chain-receipt-before.xml 为5条失败（4类收料）；chain-purchase-before.xml 为3条失败（2类报料）；chain-batch-access-before.xml 为2条失败（批量撤销与成本缓存）。这些文件仅保留最终正确构造场景的失败结果。

修后共40个不同定向用例通过：
- chain-receipt-after.xml：18通过，包含原8条新缺陷参数回归和10条相邻正常路径/换算。
- chain-receipt-risk-matrix.xml：18通过（冻结、权限、篡改、成本、事务、撤销等）。
- chain-receipt-final.xml：12通过，包含最终10条新回归与2条既有幂等；其中8条与第一批重合。
- git diff --check通过；迁移head唯一，未增加迁移。

产物目录：D:\.codex\visualizations\2026\10\06\01a10fa4-7953-7483-aa7f-d36784031133。
测试警告仅沿用合成fixture短JWT测试密钥提示，未涉及正式密钥。

文件：app/api/incoming.py；app/api/requisition.py；app/services/external_packaging_receiving.py；app/services/incoming_receipts.py；tests/test_chain_purchase_guards.py；tests/test_chain_receipt_guards.py。

## 边界与人工验收

本轮确认7类问题，未将每环节10项检查声称为10个Bug。未做正式库存或历史单据修复；不证明全部并发组合、多级BOM及各终端无问题。本次数字别名去重覆盖同一来源前缀的不同数值表示；不同路由前缀指向同一物理采购来源的兼容别名仍依赖原后端来源校验，后续宜统一发布唯一收料行ID。

主管发布后人工核对：①分批补库按剩余数量补齐后重试，确认不新增库存；②收料撤销后旧页面重试，应说明已撤销并引导查询历史；③旧部分实收采购不能编辑原尺寸或数量。正式屏幕与实体操作未经管理员验收。
