# 生产“保存入库”原请求恢复：独立方案复核

日期：2026-10-10。状态：只读方案复核完成；本报告未实施、提交或发布修复，也未进行正式页面或现场加工验收。

结论：同意以单任务 complete（带 actual_input_quantity）为下一最小修复闭环。现有同原键重放、事务回滚和数量门禁有效；需要补齐持久原请求、准确保存证明和只读核对。group dispose 同类风险另卡，不能据此扩到组套、撤销、process 或全部生产算法。

## 范围与证据

已按 CODEX_START → NAS AI_START → production-completion-result-audit/TASK.md → ORDER_FLOW 与总需求相关章节、执行章程相关小节读取。随后核对根 production-completion-save-recovery-fix/TASK.md；本子任务只有 artifact/NAS 写权限。

实际入口：index.html 单任务“保存入库” → production-workspace.js 的 saveStockDialog('complete') → stockPrepAction → POST /api/production/stock-preparation/{receipt_id}/actions。实际请求明确携带原 job、版本、投入、产出、semi/finished、位置与布局版本、超产确认和 operation_key。

复用 API 6 项真实合成 HTTP/数据库观察（14.005 秒）及 UI 14 项实际方法观察，均为修前行为证据，不能称修复通过。本独立线没有重复宽测。已独立重读 API、processing、mutate、模型、事务及源解析代码，核对证据 XML、指纹与基线。verification.json 保存本轮独立差异核验。

- API 工作树 9a3a9f33dfc9e5194017729898f5c4e963c7e904；UI 工作树 0b3189d0dcc38a7ccbec5237234f72d681b84df4。均只读、干净，服务与进程未动。
- 所选后端文件和 production-workspace.js 与 v601 来源 108de0681c73fc213f94dd123d077eec395a8186、v602 来源 6640d5369e2d48ecba36be9aefae1afa9ed449db 一致。index 整文件在 v602 因订单恢复改变；仅生产方法片段相同，不能称整文件一致。
- 实际证据位于 ../production-completion-result-audit/api/ 与 ui/。formal v602 发布由根负责，本报告不重新声称独立完成正式发布核验。

确认缺陷为 UI 两个主族（坏 2xx 误报成功；未知原请求遗失）及局部生产通道的迟到 401 注销新账号。当前 API 不认识 expected_actor_id 是新合同必须补足的意图绑定，不另计权限旁路。没有发现同原键二次扣料、审计失败留下部分产出或普通权限被绕过。

partial 8/20 真实结果：请求 job1，取消 job1 的旧预占；job2 完成 8，job3 继续 12，源消耗 8、预占 12、产出 8。原键重放零新增。真正刷新到 job3 后用新版本、新键再加工 8 是另一合法批次，不能当作“原键重复二扣”。因此恢复保护必须覆盖同 receipt/source 的 continuation，不能只锁原 job1。

## 最小持久证明

复用 StockPreparationCommand 的原 operation_key 主键、receipt_item_id、request_json、result_json、actor_id。只对本轮支持路径的新鲜父 Command 增加 completion_receipt。首回执、精确重放与只读查询使用同一冻结证明；不修改旧记录，不从以后库存或任务补齐缺失的历史证明。

建议合同如下；最终 URL、字段命名和枚举由 API 明确发根与 UI 后统一，不能三方各自猜测。

| 证明部分 | 最低事实与验证 |
| --- | --- |
| 身份 | schema、原 operation_key、历史 actor_id、receipt_item_id；当前认证 actor 单放响应包络，不能覆盖历史操作者 |
| 原请求 | 保留现业务归一化 request；digest 如有，取原 request_json 字节。覆盖 action、原键、lot/job/output 版本、投入、产出、用途、地址、布局及超产确认；expected_actor 排除在业务签名外 |
| 原来源 | 冻结来源类型和相关主键、原 customer_id、源 lot_id；备库订单项与订单剩余片料两类来源不可混同 |
| 本次投入 | requested job_id/version、original job_id、本次实际投入数量与台账单位；核对这次完成对应预占消耗/流水，不能用源累计 consumed |
| 本次产出 | completed job_id、完成时版本、本次 actual_output、output lot_id、output_kind、产品冻结身份、台账单位及员工显示单位；不能用该 lot 后来的 available/physical 总数 |
| 原位置 | 本次输出的实际 location_id、请求布局版本及冻结可读位置；后来移货或改货位名称不能改写原结果 |
| 后续任务 | 实际 continuation job_id（可空）、创建时数量/版本、remaining_input_quantity。全量明确 original=requested；部分 completed 可以与 requested 不同 |

remaining > 0 但因理论产出不足而 continuation 为空是当前算法允许的事实；不能在证明层补造继续任务或宣称全部剩余量已经预占。单位来自本次真实台账与冻结物理身份：已加工子件的物理“片”和底层 lot 计量可能不同，应分列保留，不以当前产品主数据重命名历史。无需返回成本、金额等字段。

首写时使用同一事务中真实完成 job/output/reservation/movement 的事实做一致性核验，字段不齐或矛盾应整个事务回滚。proof 的序列化和验证必须在 commit 前完成；commit 后仅返回已准备的普通值，避免再查 mutable ORM 拼回执。

## 两个必须落实的实现门槛

1. **证明必须在父 Command 首次 INSERT 前装配。** alembic/versions/sprep0912_stock_preparation.py:35–36 对 stock_preparation_commands 建有 BEFORE UPDATE/DELETE 拒绝触发器。stock_preparation_processing.py 的全量和部分分支目前都在添加父 Command 后立即 flush。即使仍在原事务中，API 等 process 返回后 UPDATE 父 result_json 也会违反正式触发器。必须由 processing 两个新鲜父记录插入点在同写锁内调用专用构建逻辑，再首次插入完整 result_json；已有原键返回分支绝不调用构建器。不能删除/规避触发器或改迁移。

2. **只读结果查询不能复用写资格校验作为读取前置。** prep.source/_source 包含 has_raw_plan、purpose posted、来源当前工艺资格等写门禁。原加工完成后来源可能变化，不能因此把已持久原结果伪装为未找到。新 resolver 需只读分离“来源身份/当前客户范围核对”和“当前是否允许继续加工”。身份缺失、来源冲突或权限变化可拒绝并保留待核对状态；不能跳过范围检查，也不能要求当前库存/状态仍满足原 POST 的写资格。

已将两项通知根/API；根已采纳并补任务卡，增加 processing 两个插入点的最小白名单，并要求合成测试启用同款 append-only trigger。常见 create_all 测试库未必包含正式迁移触发器，单靠原夹具绿不足证明正确。

## 旧请求、只读恢复与权限

- expected_actor_id 使用严格正整数、可选以兼容旧客户端；在执行前核 authenticated user。排除旧 model_dump/业务签名，绝不改旧 JSON：process 是 compact encode；legacy mutate 是普通 json.dumps 的带空格格式。不能统一编码或补新默认字段破坏旧键重放。
- 只读核对带路径 receipt、原 key、完整 original_request、expected_actor。先核当前角色（保留 admin/boss）、原 owner、receipt、原 body 与持久 Command；未授权不能泄漏结果。现在 admin/boss 均全客户，不伪造受限 boss 测试；仍保留冻结客户与当前来源客户双范围检查以防合同将来改变。
- 新完整 proof 与原输入逐项对应才 completed。后来的库存移动、继续加工、任务历史投影不能覆盖当时事实；当前状态需要展示时另列为当前追溯信息。
- 历史 Command 无新 proof：只显示安全 legacy_trace 和其原 result 中确实保存的 job/continuation 等字段，另可显示原 request 记载的提交内容。不能把请求宣称为已产出事实，不能因为后来的当前 job 恰好 completed 就升级完整证明。旧 result 缺 original_job_id、位置、产出时不得补造；该缺证不应妨碍合法原记录追溯入口。
- 原键冲突、异 actor/body、损坏 proof、权限撤销均保留原待核对记录。不用 HTTP 409/500 判定未执行；真实 commit 后丢回执已证明 500 可能保存成功。
- not_recorded 仅查询时点未见；原 POST 可能仍在途。no-store，零业务 flush/commit；标准拒绝审计维持。查询不自动 POST、不删除原键、不换键。明确“按原内容继续”才重发完全相同 body/key，角色或来源资格变化可能拒绝，仍应保留并可追溯。
- 旧缺 body 记录仅安全追溯/管理员核对；不能用当前任务填补原 body。这里不引入终结 marker、接管账号或注销请求能力。

## 页面最小行为

原 POST 发出前，逐请求持久保存不可变 endpoint、action、key、完整 body、owner、receipt/source 及可读摘要，写后读回验证失败则零 POST。未知后编辑/关闭/重开/刷新仍保留原请求。按账号隔离、每键独立；同 receipt/source 的继续任务也被原 unknown 约束，其他收料项照常操作。跨标签存储本身不是原子锁，不能夸大跨设备防重保证。

空、坏、部分、错数量/位置/账号/映射回执一律待核对；不能只检查 2xx 或非空 JSON。卡片显示“原投入、原产出、原货位、原任务”，按钮“查原结果”“查看加工记录”；查不到说明未证明取消，明确继续才用原内容原键发送。不得自动恢复成当前默认 100 或新的 continuation 请求。

现有迟到成功/迟到 GET 和列表失败保护保持。single 完成使用局部独立网络通道，在共享 401 副作用前核账号/代数，避免旧 401 注销新账号；当前账号真实失效仍执行现登录规则，不改全局 Axios。有限超时退出忙态但保留 unknown。实际关闭按钮与提交忙态一致。

只有完整证明才能显示成功和清理自身 pending。列表刷新失败仍显示本次已保存，不再重发；缓存清理失败保留 confirmed 状态，不能还原成可执行待加工。成功记录入口不能马上被刷新掉而丢失可读结果。

## 实施验收与剩余范围

根新卡已覆盖主要门禁，以上两处纠偏已采纳；目前没有方案层新增阻断。后续仍必须独立核精确候选与真实 API JSON/实际 UI 消费，不能以此计划替代修复验收。

最短集合：带正式同款触发器的全量/部分首次证明、原键重放零新增；新旧 compact/spaced 签名字节；commit 前 proof/审计故障全回滚、commit 后丢回执只读恢复；查询未见时原请求随后成功；后续源/目标变化不改冻结 proof；当前角色/owner/范围；旧无 proof 安全追溯。UI 验证坏回执未知、首发存储失败零 POST、刷新/重开原内容、同 receipt continuation 受阻/另一 receipt 可用、未知后 409 保留、迟到 401/成功/GET、成功后列表和缓存失败；至少一条真实合成 HTTP 保存丢回执→刷新→只读恢复跨合同验证。

不重复计已有绿保护为 Bug；不宣称现场进行了第二次物理加工。group dispose、手机未知请求永久终结仍未在本卡修复。没有新增数据库模型/迁移，没有正式数据写入或自动页面操作。管理员实际页面和物理入库验收待后续发布后进行。

交付：本 PLAN.md、verification.json、verify_sources.py（只读核验脚本）及 NAS 独立回执。无源码提交，本轮仅方案。
