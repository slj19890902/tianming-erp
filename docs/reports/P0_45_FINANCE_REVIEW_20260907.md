# P0-45 财务链路与自动化梳理（2026-09-07）

建议沿现有财务链补齐“自动准备、异常集中处理、人工确认事实”，优先完成新收料冻结价守门和历史缺口收口。现有月结、业务修订、开票任务、组合付款、固定费用草稿均可复用，无需重建财务模块。

本报告为**静态代码审阅**：读取工作区 `D:/tm-worktrees/erp-p0-45-receipt-price-gate-20260907`，HEAD 为 `315ae1f93beda61e5f09ac39ed26153dd2bd51f2`。下列行号均按该基线；本轮其他执行者正在修改的 P0-45A 收料门禁不在本报告中预报为完成。只写本报告，未运行测试、未启动应用、未连接任何业务数据库、未进行浏览器验证、未发布、未生成或修改业务凭证。测试名表示“已有相应用例”，不表示本轮通过。

## 1. 财务事实图谱

| 链路 | 已有行为与金额来源 | 代码证据（基线行号） |
|---|---|---|
| 纸板实收 → 采购冻结价 | 月结读取有效 `IncomingReceipt/Item`；优先使用 `SupplierReceiptSettlementPriceFact` 的供应商、材质、尺寸、数量、单位、价税和实收日期快照；兼容正常采购 `PurchaseReceiptFact`。内部用途分摊金额不作为应付价格。 | `app/services/supplier_monthly_settlement.py:282`、`:370`、`:406`、`:443` |
| 冻结价 → 应付候选 | 按冻结单位计算每张含税采购成本，再乘实收张数，金额 `ROUND_HALF_UP`；元/㎡通过尺寸换算，不能再对元/张重复乘面积。原生采购兼容分支仍从来源明细读取尺寸和供应商，不能把它等同于全部字段均已迁入新结算事实。 | `app/services/supplier_monthly_settlement.py:115`、`:451`、`:463`、`:531` |
| 外购包材实收 → 应付候选 | 有单独扫描与金额算法，保留采购时的数量单位、计价单位与税合同；纸板完整性检查不应把非纸板补库错误纳入同一报价合同。 | `app/services/supplier_monthly_settlement.py:348`、`:539`、`:560` |
| 候选 → 供应商周期草稿 | 供应商自有结算日，截止日完整结束后才生成；变更结算日时衔接上一实际周期。按供应商、币种、税口径分组并冻结来源哈希。 | `app/services/supplier_monthly_settlement.py:680`、`:1058`、`:1103`、`:1120` |
| 草稿 → 确认应付 | 登记供应商账单日期与金额，差异处理后须与调整后金额一致；确认在同一事务建立 `FinancePayable` 并关联月结，以版本条件更新月结。应付所属日期为周期截止日。 | `app/services/supplier_monthly_settlement.py:1850`、`:1883`、`:1909`；`app/api/supplier_settlements.py:361` |
| 确认应付 → 发票 → 付款 | 供应商发票支持同张发票跨周期分配；付款受“已确认应付余额”和“已登记发票余额”双上限约束。组合付款可同事务使用本供应商余额、承兑和银行金额；全付时同步应付状态。 | `app/services/supplier_monthly_settlement.py:2044`、`:2200`、`:2336`、`:2622` |
| 确认后差额 → 应付/余额 | 追加有前后金额、操作者、版本的调整事实，更新当前应付余额；已付金额超过调减后应付时形成该供应商余额。原实收价格快照及调整记录不能被自动任务覆盖。 | `app/services/supplier_monthly_settlement.py:1684`、`:1786`、`:1800`；`alembic/versions/jo73v8x9z62_p1_149_supplier_settlement_cycles.py:20` |
| 客户回单 → 对账 | 仅纳入已确认且未被重复占用的回单明细；应收数量采用 `actual_received_quantity`。订单销售价或无订单送货冻结价 × 回单数量，按销售价税合同计算并保存对账快照；附加收费另走确认明细。成本快照只影响毛利。 | `app/api/finance.py:5630`、`:5741`、`:5761`、`:5779`、`:5797`、`:5812` |
| 客户对账 → 开票任务/开票登记 | 确认对账后建立带来源版本、幂等键的开票任务和资料快照；支持税务模板导出、登记开票结果和原件。任务来源已变化则拒绝继续。直接发票登记也检查对账确认、业务版本、台账版本和总额上限。 | `app/api/invoice_tasks.py:1270`、`:1306`、`:1557`、`:1830`、`:1878`；`app/api/finance.py:5937` |
| 客户对账 → 收款 | 收款保存独立事实并更新累计已收，支持部分/全额收款、幂等及业务版本/台账版本条件更新；目前允许已确认对账先收款后开票。 | `app/api/finance.py:6028`、`:6051`、`:6062` |
| 固定费用/水电 → 成本草稿 | 工资、年租金分月、固定月供按规则生成成本池草稿；规则×月份去重，只更新仍为草稿的记录，已确认/作废保留。水电合并为每月一笔，确认后只能补付款信息。不能据此宣称已自动产生银行付款或供应商应付。 | `app/api/finance_simplified.py:534`、`:863`、`:893`、`:924`、`:953`、`:1096` |

“预付”需准确命名：现有 `SupplierCreditLot` 仅允许**承兑超额**与**确认后调减**两类来源，可供同供应商后续抵扣；不是任意银行预付台账。银行付款不允许超应付，也没有在本次所读接口中发现收货前银行预付款入口（`app/models/supplier_settlement.py:728`；`app/services/supplier_monthly_settlement.py:2448`）。

## 2. 已有自动化与最小增强方向

| 已有入口 | 当前触发/限制 | 可直接复用的增强 |
|---|---|---|
| `POST /api/finance/supplier-settlements/generate-due` | 页面加载到默认月份、具备财务操作权限时按当前页面状态每天触发一次；也有“补生成到期草稿”按钮。服务按每个供应商取**最近一个闭合周期**，不替换变化草稿。没有在本次查阅的 `app/main.py`、`app/core`、`app/services` 中定位到独立财务调度器。 | 先完成零缺价检查，再由受控服务任务复用同一服务；保存运行日期、范围、幂等结果和异常数。若要补多期，应显式枚举缺失闭合周期，不能把当前接口的名称理解为补齐所有历史。 |
| `POST /api/finance/simple-finance/recurring-generate` | 用户点击按当前选择月份生成费用草稿，规则有效期与月份去重；确认金额保持不动。 | 自动准备新月份草稿，并呈现规则失效、缺成本中心等例外；确认成本与付款仍基于实际业务事实。 |
| 客户待对账查询、对账生成、开票任务 | 已有待对账客户/周期汇总、确认门禁、开票资料完整性校验和冻结导出。 | 可批量准备待核对清单、草稿或开票资料缺口；以回单为准。实际开票结果、收款应有来源凭证，不能由时间经过推定已发生。 |
| 供应商组合付款及余额抵扣 | 已有同供应商范围、票据版本、余额与应付上限、事务审计。 | 自动计算建议抵扣额和付款清单；用户确认票据、付款日期及银行流水后提交原服务。 |

触发证据：`static/index.html:22217`、`:22231`、`:22272`、`:22027`；周期服务：`app/services/supplier_monthly_settlement.py:1278`；接口事务/权限：`app/api/supplier_settlements.py:72`、`:361`、`:495`。

## 3. 已证实的代码缺口与需要定向复现的风险

以下是代码行为证据，**未证实工厂正式数据已受影响**。

1. **P0-45C：存在缺价仍可生成部分月结。** `_scan_paperboard` 把缺价行记为 issue 后跳过；生成服务累积 issue 后继续为其余候选创建草稿。确认服务核对供应商金额，但未重新扫描该周期完整性。因此“生成成功”不能证明“应付完整”。应把周期缺口汇总与“立即处理 N 条”做成一处入口，零缺价断言作为生成/确认前的完整性门禁；已确认单据遇到新差异走追加调整。证据：`app/services/supplier_monthly_settlement.py:370`、`:1106`、`:1128`、`:1850`。
2. **P0-45B：历史预览天然有日期边界。** 现有 `_period_items` 按 `start_utc/end_utc` 筛选，预览跳过已有结算事实和原生采购事实。单周期扫描不能证明全库未解决数为零。应另设覆盖所有有效应付纸板实收的只读完整性扫描，再按明确来源生成可采用计划，保留已有事实幂等跳过。证据：`app/services/supplier_receipt_price_facts.py:590`、`:608`；任务卡第 IV、V 节。未读取正式库，未复核历史数量或金额。
3. **独立高风险候选：通用应付按钮可能与供应商月结脱节。** `_transition_payable` 只按 `FinancePayable` ID、版本和状态修改，可标记已付/作废；该函数未检查 `SupplierMonthlyStatement.finance_payable_id` 反向关联，也未同步月结付款事实。应补“关联月结的应付只能走月结服务”的接口保护和一个定向回归；在复现前不要称正式余额已经错误。证据：`app/api/finance.py:6974`、`:6997`、`:7004`、`:7042`；对照月结联动 `app/services/supplier_monthly_settlement.py:2622`。
4. **独立高风险候选：已收款未开票的客户对账可进入异议重开路径。** 收款接口只要求已确认对账，不要求先开票；`reopen`、`adjust-dispute` 检查已开票/开票任务，却未见 `settled_amount` 门禁。应定向构造“先收款、未开票、再移出对账明细”验证，保证已有收款事实不能因修改来源而失去对应金额；实现可选择阻止或受控款项转移，需明确业务口径。证据：`app/api/finance.py:6074`、`:6330`、`:6357`、`:6508`、`:6536`。
5. **到期日不会自动从供应商结算日推导。** 供应商确认应付设置 `document_date=period_end`，未设置 `due_date`；账龄把空到期日归入“未到期”。若直接自动催付，可能把无付款期限的月结长期列为未到期。需先明确付款账期及起算点。证据：`app/services/supplier_monthly_settlement.py:1883`；`app/api/finance.py:7161`。

需保持的现有保护：草稿重生成新建 N+1，旧单及明细完整保留；已有应付/发票/付款不可重生成。自动周期调用 `replace_changed_drafts=False`，确认后变化仅提示处理。证据：`app/services/supplier_monthly_settlement.py:913`、`:1158`、`:1278`。不能用计划任务更新供应商主档价后批量重算旧单。

## 4. 待确认业务事实（不阻塞 P0-45A）

- 银行预付是否允许收货/收票前发生，允许按采购单还是按供应商挂账；当前供应商付款的“先票后款”限制是否符合全部实际场景。
- 供应商付款期限按月结截止日、账单日期还是收票日计算；未登记期限应显示“未设置”还是采用明确默认值。
- 客户已收款后的异议如何处理：冻结原对账，还是建立带审计的款项转移/退款链；不能只改对账金额。
- 周期无人值守生成需要覆盖哪些历史月份、使用哪个受限服务身份；失败提醒谁。不能靠共享个人账号或页面常开保证运行。

## 5. 已有测试与本轮验证范围

下列是已定位的最短测试候选；本报告执行者**均未运行**。P0-45A 实际新增与执行结果由实施回执单独记录，不以本表代替。

| 保护主题 | 已有测试位置与名称 |
|---|---|
| 历史采用幂等、主档改价不改旧月结 | `tests/test_p0_39_supplier_receipt_price_facts.py:329` `test_explicit_hash_adoption_is_idempotent_and_monthly_lines_use_frozen_fact`（`:420` 修改当前报价后仍断言冻结金额） |
| 补库缺价拒绝 | 同文件 `:473` `test_stock_replenishment_freezes_price_and_missing_price_fails_closed`；它不能单独证明所有收料入口零写入 |
| 周期闭合/修订保留/禁止改已开票付款单 | `tests/test_p1_149_supplier_settlement.py:344` `test_supplier_period_generation_waits_for_the_complete_cutoff_day`；`:384` `test_regenerate_creates_revision_n_plus_one_and_keeps_old_statement_and_lines`；`:555` `test_regenerate_rejects_statement_with_invoice_or_payment` |
| 同供应商余额、承兑、银行同事务 | 同文件 `:609` `test_payment_batch_applies_existing_credit_acceptance_and_bank_atomically`；`:763` `test_payment_batch_rejects_bank_overpayment_and_cross_supplier_credit_without_partial_writes` |
| 确认后追加调整与余额留痕 | 同文件 `:849` `test_paid_statement_adjustments_reallocate_payment_create_credit_and_are_immutable` |
| 固定费用自动草稿不覆盖并发确认 | `tests/test_p1_144_simplified_finance.py:55` `test_recurring_wages_rent_and_vehicle_loan_generate_monthly_drafts`；`:215` `test_recurring_generate_cas_cannot_overwrite_concurrently_confirmed_cost` |
| 客户开票/收款版本、幂等和回滚 | `tests/test_finance_manual_cas_idempotency.py:234` `test_partial_and_full_settlement_increment_version_once_per_fact`；`:469` `test_audit_failure_rolls_back_statement_business_and_idempotency_fact` |
| 客户异议保护已有发票 | `tests/test_p1_130_statement_invoice_finance.py:276` `test_issued_statement_cannot_be_reopened_or_adjusted`；本次未定位到“已收款未开票”的对应守门用例 |

下一步顺序：先交付 P0-45A 同事务冻结与提交门禁；再单独处理 B/C 全库历史缺口和持续完整性；上述通用应付、已收款客户异议风险各用独立定向用例验证后修补；最后复用现有服务增加周期草稿和异常提醒自动化。财务与地图继续独立 worktree、提交和验收。

本轮已提供统一自动回归脚本 `scripts/uat/verify_p0_45_finance.py`（测试数据库、备份及密钥均使用临时隔离目录）与全日期只读 CLI `scripts/admin/check_supplier_receipt_prices.py`（显式指定数据库，以 `mode=ro` 和 `query_only` 打开）；正式历史采用、周期强阻断和无人值守调度仍未执行。
