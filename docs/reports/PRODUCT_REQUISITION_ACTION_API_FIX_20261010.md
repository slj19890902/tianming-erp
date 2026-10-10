# 产品申请报料动作：后端最小修复候选

候选：8c79504e0df337fa6ece5487927bd926ee00414e；父任务卡2b18eef03e885b187eeb98e678ea405447d806df；正式基线v607/ef952a8d60d60683b167d2890c471d37a686aa42。managed group-save-recovery-api树clean，旧分支保留。仅改requisition.py、external_packaging_stock_replenishment.py、根额外批准的incoming_receipts.py最小路由重排、新专项测试。无迁移/版本/push/正式数据/浏览器/服务/PID操作。

## 结果与边界

修复外购普通/IntegrityError/服务内部重放的当前权限、客户范围、完整actor+归一化body校验。新hash只在真实首次Order INSERT赋，成功重放不补hash、不新增采购/库存/创建审计。新建同事务最小创建audit含order/batch/actor/hash/key摘要；不是完整敏感body或回执证明。审计失败真实HTTP500、order/hash/purchase/audit与库存事实全部回滚。Service漏proof与无效proof均零写。

fresh参考产品停用/删除保护已覆盖；旧成功精确重放按冻结事实跨日及主档变化仍201。无参考通用板仍合法。旧NULL外购仅安全409＋原采购单号/历史入口，不猜历史owner/body、不改旧hash。

新hash兼容检查发现并修复三处来源分类：直接外购的原print、reported候选及完整列表不能消失；严格排除整单任何实物quantity_contract行，保留原physical-warning合同。通用incoming原外购专用拒绝先判。没有修改unified_procurement或数量匹配核心：pending已有外购purchase关联排除，取消原来源voided，attach_stock_sources仍拒外购，coverage已有paperboard限定。gold formal607/current实际HTTP均证明原来源GET/打印、两种reported查询、外购history/采购打印可读；外购收料成功，已收拒取消409，未收取消200且不误入纸板pending。

尚未修复：提交后回执丢失/前端原key持久恢复（另卡）；API不新增readonly resolve。旧generic void对历史NULL外购可能缺外购撤销专用门禁只是静态相邻疑点，未实证、未修改。本轮不宣称全流程弱网恢复完成。

## 测试与真实证据

- red-fix.xml：2条真实HTTP安全红（异remark原key201；停用参考产品fresh201）。
- final-targeted.xml：20 passed，47.14s = 新专项7＋p1_140真实11（只排migration与已证实旧静态节点）＋审批1＋金样本1。
- physical-adjacent.xml：1 passed，5.73s，原实物预警外购采购/收料相邻节点。
- export-fixed.xml：4 passed，10.26s，冻结candidate实际HTTP另导normal/replay、身份/异body、旧NULL及fresh产品边界，位于fix-http/；非手写业务响应。
- gold-formal607.xml：1 passed，3.99s；gold-formal607.json实际加载正式原requisition/external服务原文、其他未改代码的隔离进程。gold-candidate.json来自本候选，明确sourceCandidate；无正式库。
- run_fix.py为安全conftest runner；finalize_fix.py产生4文件raw/LF指纹、精确节点/结果hash。运行源与commit LF相同。

## 保留失败与基线对照（不冒称全绿）

first-green-audit-assumption.xml：4pass1fail。专用测试曾把标准权限拒绝新增security audit误算成业务零新增失败（operation_logs 5→6）；修正专用断言为业务事实严格不变、创建audit恰1，未删除权限拒绝审计。fix-green.xml的5pass是当时中间阶段，不是最终7专项结论。

adjacent.xml：5pass4fail，9.41s。formal607-77-baseline.xml实际执行正式INDEX原文及相同未改旧测试：3pass4fail，0.75s；不是仅比字节。四相同节点：
1. test_replenishment_save_freezes_payload_and_commits_before_auxiliary_work：ValueError，旧静态断言找不到Promise.allSettled。
2. test_same_replenishment_draft_is_single_flight_and_payload_is_frozen。
3. test_successful_post_survives_print_and_refresh_failures。
4. test_validation_failure_keeps_draft_but_unknown_result_closes_it。
后三条均旧_method_body以saveSupplierRequisitionDraft作边界截入beginSupplierSheetCuttingEdit等下一方法，AsyncFunction报SyntaxError Unexpected token '{'；未改旧测试或当前save方法，列为旧测试维护边界，不据此新增业务Bug。

before-required-proof-targeted.xml：16pass2fail/1deselect40.82s。其中通用incoming专用code丢失是本轮真实兼容回归，已最小修复并原断言通过；另一旧test_external_stock_warning_frontend_uses_purchase_units_and_history_actions因“建议外购备库”原静态文字缺失。formal607-external-baseline.xml同节点实际正式服务1pass1fail3.73s：incoming原baseline通过，旧静态baseline也失败；不能两者统归豁免。

required-proof-fixture-missing-name.xml：18pass1fail/2deselect47.31s。新历史NULL INSERT夹具漏product_name_snapshot的NOT NULL字段，补真实产品冻结名/码，未改业务门禁；最终通过。gold-candidate-fixture-missing.xml初金样本缺F1待送区，收料409是正常门禁；按既有测试补合成合法位置后gold通过。旧失败XML均保留，未为重现中间工具错误重跑。

## 交接

根负责独立审查/整合/正式发布；本候选源码释放不再修改。NAS独立FIX回执与本报告逐字一致，manifest含报告/合同/源指纹/XML/HTTP文件hash。实际触控/管理员工厂操作不由API测试代替。
