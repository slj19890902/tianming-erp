# 独立组套保存恢复 API 候选

2026-10-10；GROUP-ASSEMBLY-SAVE-RECOVERY-FIX-20261010。候选已提交，未发布；根另行整合、独立审查与发布。

候选 f767ebf874ed672ebb99b51cfd9b39529414dfac；基线为正式 v604 / 75129569ed5ff0bf454d075471c9b4fd902b070f / en1009hp。管理树 group-save-recovery-api-20261010，分支 codex/group-assembly-recovery-api-20261010，旧4d分支保留。提交后工作树干净，仅授权4运行文件和新API测试，无版本/迁移/模型/数量算法变更，无push、服务或浏览器操作。正式/NAS状态引用根已验证发布事实，本代理没有正式业务访问。

启动沿 CODEX_START、NAS AI_START、新任务卡、ORDER_FLOW/主需求7/14/17及章程3～9；未变上下文按卡复用。根方案与独立 audit/review/PLAN 全文已读。探针仅合成tmp_path库，runner显式加载安全conftest，--noconftest避免重复监听；不复制正式库。

## 最终变化

原 group_stock→assemble 保留完整原签名与业务门禁，新增严格可选actor匹配、冻结 assembly_completion_receipt 和只读 POST group-assembly-result。每job的receipt/原料来源/可空客户/已加工output lot形成完整成员集合；inputs仅本次真实非零consume。余片从本次流水before/after取得，take0从同锁结束余额冻结；不倒算初加工入库量，不声称当前库存或可选共享登记结果。

disposition.assemble仅增加默认None可选builder，在最终父Command原首次INSERT前调用；groups.mutate_group独立assembly_result_builder只向顶层assemble转发。dispose内部原assemble不传新builder，已发布single/dispose合同保持。原输入证据→输出/成本/空栈板→父Command/审计/flush→后置enroll→commit顺序不变；append-only UPDATE/DELETE触发器始终开启。

写响应在commit前完成严格证明校验和序列化；原键精确重放整个JSON相同，零新增。原账号及签名不符保留请求。只读恢复同时核当前权限、原任务/receipt归属及冻结客户；另核持久输入证据和本次consume/manual_in流水ID/key/actor/数量/单位/前后余额，避免损坏证明冒称完成。不跑来源posted、状态、当前BOM/余额/位置新写资格，也不从当前业务数据重造历史证明。历史默认builder=None的合法记录仅legacy_trace，不回填或UPDATE。

fresh业务错误只有同锁原父key不存在、commit未开始且rollback成功时独占Assembly-Rejected，绝无Preserve。锁前认证/角色仍沿共享Group-Preserve/no-store，FastAPI422可能无专属头，任何状态码本身不授权删键；已unknown请求即使后来Rejected也保留。CONTRACT.md包含精确包络、默认值、nullable、算法忽略字段和摘要边界。

## 实际验证

- red.xml：3项真实正常HTTP均200却无完整证明，3 failed/9.46s；这是原保存结果合同缺口的红例，不是数量或幂等失败。
- api-final.xml：24 passed/61.37s。正常2/5套，多receipt同产品3jobs/2非零inputs，两次部分组套，旧合法disposition=semi/actual_output=0及ignored字段原签名，真正提交后ack丢失，只读零业务DML/commit，在途未见父Command随后原POST成功，当前及历史actor/权限/客户门禁，损坏证明回滚，旧无proof、append-only触发器，以及freshRejected与已存在key冲突区分。
- adjacent.xml：5 passed/14.46s，节点为group dispose首写semi/finished两项、原group_reserve_complete_and_frozen_bom一项、single首写amount8/20两项。test-nodes.json保留XML的真实classname及参数，不估算拆分。
- git diff --check通过。最终源码/测试LF与raw SHA在source-fingerprints.json；仅允许5文件提交。

实际边界：多来源第二次2套，consume前后为(9,6,3)/(12,8,4)，总余片7；已耗尽第三job take0余额0，GET的output_locations=[]，仍必须在成员全集。页面全集身份锚点是job/receipt/product/request版本；仅有位置投影时正向附加核对，不要求未投影成员猜lot。默认真实台账/显示单位不同：子件sheets/只，成套boxes/套，原单位逐一保留；不凭标签转换数量。来源客户null与目标空名称真实保存成功；后来源撤销、job取消、位置floor=null仍恢复原冻结证明与相同原键回执。

真正commit后抛OperationalError返回500+Preserve；库存/父Command已存在，readonly completed原证明，SQL监听无INSERT/UPDATE/DELETE，不commit，不新增流水；精确POST重放零新增。后置enroll注入在父Command与proof已flush后抛错，全核心库存/job/reservation/Command/movement及业务审计回滚。成本门禁因第二成员缺继承成本拒绝，第一成员已消费的事务也完整回滚，Rejected与Preserve互斥。

## 失败分类与未覆盖界限

green-interim.xml曾16 passed/1 failed：nullable夹具删除算法必需的physical_basis导致500；已修夹具保留实物身份，仅改合法客户null/空位置名。此为测试准备错误，不登记新业务Bug。lock-drift-red.xml名称为阶段名，实际1 passed/4.10s：锁前真实Job.version漂移返回409，未复现旧值覆盖。新增write身份查询移到atomic内首业务读取，避免依赖弱identity-map偶然释放；仅预防结构调整，不计新增Bug或称安全红。旧警告为合成账号JWT短key/Pydantic已有exclude元数据警告，未影响原签名验证。

已启用实际同款Command拒UPDATE/DELETE触发器。没有启用共享成品策略的成功合成例，本证明不承诺共用登记状态；其原后置顺序保留及失败回滚已有实证。历史GET仍可能被当前source写资格拦住；专用readonly不依赖该GET，历史读失败不能撤销confirmed。库级无跨设备注销/接管能力；not_recorded仅时点事实。浏览器DOM/Cookie/localStorage、多设备及管理员实际操作验收待根/UI单列，本轮未启动或重试浏览器/进程动作。

## 真实HTTP交付

http目录13包：normal-2、normal-5、normal-1-multi、second-partial-depleted-member、ignored-fields-compatible、committed-ack-loss、inflight-not-recorded、legacy-no-proof、nullable-changed-eligibility、fresh-rejected、rollback-builder、rollback-enroll、rollback-cost。全部原API HTTP，包含原body/actor/sourceRow/真实workspaceBefore和After/history、首写/readonly及可用重放；没有手造成功proof。http-manifest.json为最终文件SHA。

最终api-final执行使用提交前完全相同的源码字节，commit未修改运行内容；每包透明记录此生成时序、finalCandidate及源LF SHA。fresh-rejected原body是人为陈旧output_version夹具，与当前sourceRow不同，禁止冒称当前GET原生陈旧。旧候选审查与初始红档保留，不以最终标签伪改其事实。UI已获最终合同、精确SHA和全部真实包，可做实际HTML/mixin消费；根独立审者已收到并启动。

运行入口：使用仓库.venv Python执行本目录run_tests.py，工作目录为候选tree，参数 tests/test_stock_preparation_assembly_recovery_api.py --noconftest -q --junitxml=本artifact/api-final.xml；HTTP导出设置 ASSEMBLY_RECOVERY_EXPORT_DIR=本目录/http。候选/回执不代表正式发布或持续Goal完成。
