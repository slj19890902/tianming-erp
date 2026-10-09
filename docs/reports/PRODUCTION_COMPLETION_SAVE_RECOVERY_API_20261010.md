# PRODUCTION-COMPLETION-SAVE-RECOVERY API 候选

候选 c02693f8a678b1a72ed3de285b32633842b7c22c；从正式v602源码6640d5369e2d48ecba36be9aefae1afa9ed449db建立新managed工作树及codex/production-completion-recovery-api-20261010。未push、发布、改版本/模型/迁移或正式业务数据。旧chain-order-audit HEAD9a3a9f33及其服务源保持干净不动，未操作进程。API线执行身份为Codex手机/API代理，沿本会话模型设置未另切换。

## 结果与合同

只修真实single_job保存入库的complete＋actual_input_quantity路径。首次写入前，在原atomic_bom写锁中查父key，processing两处父Command首次INSERT之前调用可选result_builder；默认None与旧调用不变，精确重放先return，不执行builder。证明随父result_json一次INSERT，数据库禁止UPDATE/DELETE触发器保留，测试与隔离服务显式装同款触发器。

专用helper核本次completed job、consumed reservation、原料consume及产出manual_in流水身份/数量/actor；冻结原body、历史actor、原收料/customer/source、requested/original/completed映射、actual input/output、output lot、原位置、真实单位、remaining/continuation。不取当前累计库存当本次产出，不返回成本。新proof全部序列化和flush在commit前原回滚边界内。数量、预占、工艺、BOM、成本、位置资格与审计算法未改。

原200业务字段保持，增加completion_receipt/current_actor_id/proof_status。首回执与同key重放完整JSON相同；非必需动态replayed字段已按根审核删除。旧Command无新proof精确重放仅legacy_trace，不补写旧记录。过程compact签名与原mutate普通JSON签名未统一。

只读POST /api/production/stock-preparation/{receipt_id}/completion-result按原key＋完整body＋认证actor核原持久命令。先核当前来源客户，完成后再核冻结原客户；admin/boss仍沿原全客户规则，不虚构受限boss。身份读取不调用带加工资格的prep.source；保存后位置停用/改名或库存版本变化不改原proof。省略output_kind时，已有命令核对取持久request_json有效值，不查后来job推断。只读no_autoflush，不业务commit；标准权限拒绝安全审计保留。

not_recorded只是当前查询时点，原POST可能在途，不允许清键或隐式执行。expected_actor_id严格正int可选，排除model_dump及旧签名；顶层和原body owner都与当前认证比对。异身份/异body/损坏证明返回保留型中文错误。no-store用于成功与业务/身份/权限异常；框架字段验证422不标安全拒绝。首Rejected只在本single路径已进入事务、锁内父key不存在、业务WIE发生于commit之前且rollback成功时给出；未知后的后来Rejected仍不能使UI清键。

合法nullable保留：customer_id/floor可null，location_name可空，无法确定的output_unit为null而不猜只/片。实际单位与底层stock_unit分列；remaining>0但不足一件时continuation=null合法，未新造任务/预占。精确字段及nullable见CONTRACT.md。

## 证据

修前真实HTTP2项安全红：部分8/20、全量20/20均因旧200缺completion_receipt失败，red.xml 2 failed / 6.46s。原只读6项和UI14项证据保留在上一audit目录；不把它们重复计成新后端扣库Bug。

最终final.xml 29 passed / 59.08s，XML classname精确拆为新test_stock_preparation_completion_recovery_api 25项＋原test_stock_processing_auto 4项。test-nodes.json列全部节点，verification.json记录真实XML与clean。新覆盖部分/全量、readonly零业务commit及库存/Jobs/Command/流水/预占/业务审计零新增、commit真实成功后ack丢失、proof/审计故障全回滚、strict actor、异body/key/嵌套actor、not_recorded、旧默认builder=None/compact签名及append-only UPDATE/DELETE拒绝、位置/库存后来变化、损坏JSON/proof/null、首明确拒绝与已有key冲突、权限/匿名、真实在途not_recorded→原POST完成、冻结工艺/不足一件余料、nullable客户、省略用途持久核对、当前/冻结客户两道门禁。

原4个邻近节点：
- tests/test_stock_processing_auto.py::test_auto_receipt_partial_replay_cancel_and_identity
- tests/test_stock_processing_auto.py::test_partial_audit_failure_rolls_back_and_zero_rejected
- tests/test_stock_processing_auto.py::test_legacy_get_is_read_only_and_process_owns_remainder
- tests/test_stock_processing_auto.py::test_frozen_yield_batch_units_and_continuing_after_product_change

真实HTTP导出export.xml 6 passed / 13.57s，导7组JSON：normal-partial、normal-full、nullable-customer、units-frozen、legacy-trace、not-recorded、committed-ack-loss。每组带完整originalBody/actorId/receiptId和最终sourceCandidate、四文件LF hashes，非手写业务回包。部分投入8产出8、remaining12；全量20产出20、continuation=null。单位例投入8产出48，冻结output_unit只而底层output_stock_unit=sheets/quantity48，明确单位标签差别不宣称价格或单位算法修复。legacy/not_recorded同一个导出节点先后查询，因此7份JSON不等于7测试。

初期20项均失败原因是新facts适配器错误写OperationLog.details_json，模型实为details；修正后20绿，再补最短边界到25绿。api.xml保留该工具错误，不当业务红或新增Bug。邻近最初1 failed/3 passed仅新增动态replayed违背原完整JSON比较；按根批准删除字段后原测试4项全部绿，未改旧测试。合成JWT短key/Pydantic schema exclude警告仅fixture/生成schema元信息；真实业务dump排除owner已有断言，未使用正式密钥。

## 范围与交付

四个允许文件：app/api/stock_preparation.py、app/services/stock_preparation_processing.py（6行callback）、新app/services/stock_preparation_completion_recovery.py、新tests/test_stock_preparation_completion_recovery_api.py。source-fingerprints.json同时列四文件Git blob与LF SHA256。diff检查通过，candidate树干净；Alembic仍单一en1009hp head，无迁移执行。

UI线在自己的工作树消费真实JSON并负责隔离Chrome实际丢ack→刷新→readonly恢复；本API报告不冒称页面/手机/物理验收。专用serve_ui.py由UI独占启停新owned18163端口，每次生成新UUID合成库、真实cookie、20张待加工及原8张body，/__fixture__/ready暴露合成数据和startup源hash，显式拒旧18161/18162。API未启动或关闭旧服务。

group dispose仍为已确认同类剩余风险，后续独立闭环；本轮不扩算法、group、组套、撤销、take注销或全局Axios。正式整合/审查/发布由根串行；完成证明不等于管理员现场或物理加工验收。

补充导出：committed-ack-loss.json已增加真实提交前sourceRow、真实GET workspace pending响应及history trace；export-ack.xml 1 passed / 3.89s，原7份JSON源SHA未变。UI反馈Chrome启动被策略阻断，根批准实际HTML/mixin消费真实HTTP档案替代本轮自动视觉链；不重试、不操作旧或中间服务，视觉/Cookie验收仍标pending。
