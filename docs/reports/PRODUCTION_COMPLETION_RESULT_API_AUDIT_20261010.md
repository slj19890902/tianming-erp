# PRODUCTION-COMPLETION-RESULT-AUDIT API 只读报告

审查完成，尚未实现或发布本项修复。本轮主对象已由根根据实际HTML纠正为single_job“保存入库”→saveStockDialog('complete')→stockPrepAction→POST /api/production/stock-preparation/{receipt_id}/actions，实际传actual_input_quantity且默认output_kind=semi。group_job按钮实为dispose→group-actions，仅相邻对照；无按钮的group complete不作主缺陷。

## 源与边界

固定chain-order-audit HEAD 9a3a9f33dfc9e5194017729898f5c4e963c7e904，树干净；没有checkout、改分支/仓库源码/测试/迁移/版本、push、正式数据/服务或进程操作。探针只在本artifact/api，安全conftest创建合成库，未复制正式库。

生产API/services/model、production-workspace及测试夹具与v601 108de068和v602 6640d5369e2d48ecba36be9aefae1afa9ed449db同源。static/index.html整体在v602有订单恢复变化，不能称整文件一致；实际stockPrepAction代码片段与双方精确一致，source-fingerprints.json明确记录此差异。v602发布状态由根确认并更新任务卡，本报告不以未发布订单候选冒充正式。

## 真实请求、事务和持久合同

Action包含action/operation_key/lot_version/quantity/job_id/job_version/actual_output/actual_input_quantity/location_id/layout_version/confirm_overproduction/output_kind/output_version。实际本例body在normal-8.json：complete、job1 v1、lot_version2、投入8、产出8、location7/layout1、semi、quantity0/output_version0/overproduction false。

post_action沿RoleChecker(admin,boss)，先取得receipt及客户权限，再atomic_bom；带actual_input_quantity的complete进入stock_preparation_processing.process。外层SQLite真实事务BEGIN IMMEDIATE＋savepoint，所有取消旧预占、计划本批、原料消耗、子件入库、继续任务、Command与业务审计在同一事务；API在service已构造plain result及flush后才db.commit，再返回result。这里不是盘点的commit后重新查询构造回包问题。真正commit后通信/提交函数抛错仍可500但已保存，不能凭500断言未执行。

父StockPreparationCommand以operation_key主键持久保存receipt_item_id、规范化原request_json、result_json、actor_id、created_at。process用compact encode(dict(payload,receipt_id))比较字符串；原mutate用普通json.dumps(sort_keys,ensure_ascii=False)，不能换统一编码破坏旧请求。父key精确重放先核原body+历史actor，返回已存result，不再消耗/产生输出；当前API仍先做当前receipt来源和权限门禁。不同body或其他actor同key409。

部分8/20的真实200 result：action complete、job_id/completed_job_id=2、original_job_id=1、continuation_job_id=3、remaining_input_quantity12。原job1取消，新批job2完成8，继续job3保留12；源可用0/预占12/消耗8，子件output_lot2在location7、可用8。全部20/20时保留原job1，continuation=null、remaining=0，旧200没有original_job_id。现回包不含本次实际投入/产出、output_lot、位置、receipt/source/customer或current_actor，不能凭这个旧结果完整验证所提交内容。

现GET history/job:{completed_job_id}可查看当前来源/作业，原operation_key作为该history URL返回404；这是历史trace设计，不是原key精确完成查询。没有只读精确Command-result接口。读取当前列表、状态completed或产出总数都不等于本次原key/原body完整证明。

## 六项实际HTTP与数量结论

api.xml最终6 passed、14.01s：部分及全部正常/原key重放两项，提交前complete审计故障回滚，真实commit后ack丢失及新继续批次边界，认证actor/权限，group dispose邻近。

- 正常部分与全部完成，原key重放返回原result；库存、Jobs、Commands、流水、预占和业务审计均零新增。不同payload同key409亦零新增。
- 在产出/消耗已进入事务后，注入complete业务审计失败，HTTP500且所有上述事实与提交前相同，未留下部分产出或子命令；恢复故障后同body可200。数量/事务回滚门禁有效。
- 注入Session.commit实际成功后OperationalError，HTTP500但父Command/本次产出8已持久。精确原key200恢复零新增。旧job旧版本换newkey并改产出9→409，不新增。
- 刷新后真正continuation job3/最新version/newkey，再投入8可200，是另一合法批次：总消耗16、继续预占4、子件共16。证据committed-ack-loss.json。它不是同原key二扣，也不是物理第二次加工证明；展示客户端丢原key并拿当前新任务重建请求的风险，必须先核对原请求，不能宣称CAS保护了所有刷新换键。
- body额外expected_actor_id=1、认证cookie为允许执行的boss2，首200且父Command.actor_id=2，request_json忽略该未知字段；切回admin1同key409。当前Action没有已声明owner字段，此为新恢复合同需要补齐的首写身份保护，不另计权限Bug。页面只忽略迟到响应不能阻止服务以新认证账号执行。
- 当前写入口仅admin/boss，这两角色按deps始终全客户；不虚构“受限boss”测试。撤为sales(selected无客户)后POST403，history trace403，匿名401；业务事实不变，安全拒绝审计仍保留。group dispose默认semi真实首200与同key200零新增，历史收料夹具显式关闭自动计划仅建既有group场景，不代表主single自动加工路径或physical验收。

最初artifact facts适配器误用了不存在stock_quantity，已按模型改reserved_stock_quantity后跑通过；该探针错误不作为业务Bug或修前红证据。

## 确认问题归类

UI线已用实际函数确认两类：空/坏2xx直接成功关框；未知原body/key只存row/dialog内存，编辑、关重开和刷新投影可产生新key。后台实证量门禁、丢ack持久结果与继续批次边界为这两类补充证据，不另计重复扣库Bug。没有发现原key会重复扣料/产出、审计失败残留或权限旁路。缺只读精确查询与完整证明作为这一恢复闭环的必要能力补齐。首写actor绑定纳入该合同；group dispose同类前端风险单列下一闭环，不在本轮扩修全部生产。

## 下一最小合同建议（未实施）

只覆盖上述single complete实际投入路径。新增可选严格正整数expected_actor_id，写前与认证user.id比较，bool/字符串拒绝，排除旧签名/model_dump，保留原业务JSON字节。actor mismatch返回409＋专属Actor-Mismatch/Preserve/no-store，不声明原请求未执行；旧客户端无字段仍按现授权。

优先直接在新鲜父Command.result_json附加不可变completion_receipt，首写和原key重放/只读核对同一持久proof。必须在同atomic_bom写锁下查父key是否已存在，只有本次新写才构造；不可在锁前查不到后将重放结果误当fresh，也不可后来拿当前Job/Lot补原冻结事实。全部proof构造/flush/序列化验证放在commit前同一回滚边界，不改加工算法。

建议proof字段：schema、operation_key、historical actor_id、receipt_item_id/customer_id/source_lot_id、归一化原request（完整Action业务默认及原key，不含expected_actor）、requested_job_id、original_job_id（全量也明确为原requested job，兼容旧result缺该键）、completed_job_id、actual_input_quantity、actual_output、output_kind、output_lot_id、原实际location_id/layout_version、remaining_input_quantity、continuation_job_id（可null）、真实投入/产出单位。身份/ID/数量/位置必须从本次持久事务事实核齐。当前返回current_actor_id另置本次认证字段；可读账号名称允许历史为空，不能代替actor_id。不要附财务成本或用当前总库存冒充原产出。

拟只读POST /api/production/stock-preparation/{receipt_id}/completion-result，请求原operation_key、完整original_request和expected_actor_id；最终URL/字段由新卡审核。保持现行执行角色及客户门禁，不借新接口扩权限。先核当前身份、原body actor（若有）、路径receipt/原key，再精确读取Command与其持久proof；原记录actor/key/receipt/body皆匹配才completed。历史已完成事实与当前认证身份分开。历史未存新proof只给legacy_trace/原历史结果可读入口，不能从当前任务/库存重建“原产出”；损坏/异body/异账号409保留。新proof冻结客户与当前来源客户均核当前范围。只读不flush/commit业务，标准拒绝审计仍保留；not_recorded只是查询时点，原POST可能在途，不许可删key或隐式写。

UI首发前按actor+receipt+具体key持久保存不可变完整body与摘要；未知时阻止同receipt关联继续任务形成新完成动作，其余无关单可操作。坏/不完整回执、网络、后续409、权限变化都保留原请求；只读核对与明确同原body继续发送分开。完成后列表刷新/缓存清理失败不能重发，关闭/切单/账号变更或晚响应不能清另请求。旧缺body仅trace并提示管理员核对，不能拿当前继续job补造旧body。本项不新增注销/tombstone或旧数据迁移。

最小文件建议：app/api/stock_preparation.py、新专用completion-recovery服务（若API足够则不增加共享服务改动）、static/index.html真实stockPrepAction局部、production-workspace.js调用局部、专用API/前端恢复测试；不改数量加工/BOM/成本/组套算法、模型或迁移。只读方案由根另卡审核。

下一定向验收：真实部分/全量首证明、同key恢复零新增；首空/坏2xx与unknown持久保留；真实inflight暂未见后完成；当前/历史actor、权限/客户；完整旧签名字节兼容；历史无proof只trace；旧任务newkey拒绝/真实continuation需先核原unknown；commit前proof或审计故障全回滚；commit后丢ack可查；completed后列表失败不重发；源/目标位置后来变动不更改冻结proof。分清开发测试、隔离适配与管理员现场验收。

独立预审补充：sprep0912 禁止 Command UPDATE/DELETE，未来 proof 必须在父Command首次INSERT前完整装配；API取得已flush结果后不得更新父记录。只读核对仅核持久身份/客户，不重新执行source写资格。根已另卡批准实施，本文保持修前只读证据。
