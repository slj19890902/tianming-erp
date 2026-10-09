# 组套保存结果恢复：独立只读方案复核

日期：2026-10-10。任务：GROUP-ASSEMBLY-SAVE-RECOVERY-AUDIT-20261010。

结论：可以按本文边界另开最小实施卡。现有数量、成本、版本、事务和原键幂等门禁有效；需要修的是已加工子件另行组套的保存证明、未知结果恢复和当前账号请求隔离。本轮没有实施或发布，不据观察数凑缺陷数。没有发现必须改业务算法、历史数据或数据库结构才能完成本闭环的理由。

## 1. 范围、来源与证据等级

入口固定为 `group_stock` → “已组好整套入库” → `saveStockDialog('assemble')` → `POST /api/production/stock-preparation/group-actions`，action=`assemble`。不扩大 `assemble_stock`、`unassemble`、`store_outputs` 的算法/恢复，也不重做已发布的 single 完工、group dispose 恢复。

审查源码：API `4d6808e705b4155b998a5a2978e98c5fc4eee1d0`；UI `e578e90506fbcd654a6b2341fed75d447dcc8003`。API 报告记录 15 个相关文件、UI 报告记录 3 个运行文件与正式源 `75129569ed5ff0bf454d075471c9b4fd902b070f` 的 LF 字节相等。正式 v0.22.604、en1009hp、发布与备份结果来自根发布记录；本审者没有重新访问正式接口/数据库。

先前入口规则按 CODEX_START → NAS AI_START → 本卡 → ORDER_FLOW → 总需求 7/14/17 → 执行章程 3～9 读取并复用。仅只读源码/既有报告、写本 review artifact 与 NAS；没有修改仓库、迁移、业务 POST、进程/浏览器操作、全库复制或扩大跑组。

证据复用 `../api/REPORT.md`、`../ui/REPORT.md` 与其指纹/原始档案：API 9 个真实合成库 HTTP 节点通过（1+7+追加第二次部分组套 1），UI 10 项实际 HTML/mixin 观察。UI 的 10 项包括有效门禁和故障观察，不是 10 个修复通过，更不是 10 个独立 Bug。本次复核不重复执行作者探针。

## 2. 已确认的事实与缺口

- 实际保存方法不检查 2xx 的 body；null/空对象也关闭窗口并报组套成功。这是一个反馈根因。
- 未知请求只保存在弹窗内存；改数、关闭重开、刷新会丢原 owner/key/body，无法先核清原次保存。这是一个请求生命周期根因，不等于已证明后台重复扣料。
- 实际共享 Axios 保存请求的旧账号迟到 401 会先影响全局登录状态，再到局部 epoch 判断。当前选位 GET 已有局部保护，旧账号迟到成功也已有正确保护，不能泛化为所有请求都坏。
- 后端同 key/完整 body/actor 重放返回原 JSON、零新增；异 body、数组换序和旧版本拒绝有效。原键不可查的接口缺口与 UI 生命周期一起修，不重复计为数量 Bug。
- 真实 commit 后丢 ack 返回 500，但 Command/库存事实已保存；原请求重放 200 零新增。组历史 200/当前 assembled_stock 不等于原键完整证明；原组套 key 的 history 404、dispose resolver 409 均不能当“未执行”。
- 在途请求尚未写入最终 Command 时，另一连接查不到该 key；随后原请求仍成功。not_recorded 只能代表查询时点，不能宣布取消或允许静默换 key。
- 在最终 Command 和业务审计已 flush 后，后置 enroll 异常仍使所有核心事实回滚。此顺序与事务边界必须保留。
- 部分 2 套：投入 6/8，余 9/12；第二次合法 2 套：本次 before=9/12、take=6/8、after=3/4，余片合计 7。用初加工 35 减第二次 14 会错成 21。`second-partial-assembly.json` 是本方案的关键边界证据。

## 3. 写入架构：一个独立回调，保留原事务

建议在现有 `assemble(db,payload,actor)` 增加默认 `None` 的关键字参数 `result_builder`。调用点只能在实际 output_lot_id、placed_at、消耗流水、入库流水及原有空栈板处理完成之后、最终父 Command **首次 INSERT 之前**。回调返回最终结果含冻结证明，接着保持原 `Command INSERT → append_audit_event → flush → enroll_completed_bom → API commit` 顺序。

`mutate_group` 增加独立的 `assembly_result_builder` 转发给明确的 assemble 分支。现有 `result_builder` 继续只服务 dispose；dispose 内部调用的嵌套 assemble 不传新回调，不改变其 v604 响应、原键和签名。禁止在 INSERT/flush 后 UPDATE 结果；sprep0912 已禁止 Command UPDATE/DELETE，触发器是验收条件。

新 assemble API 包装器只对 action=assemble 生效：

1. 校验当前角色、expected_actor 和原父产品/原任务/收料来源的客户归属；该阶段只读身份。
2. 在原 `atomic_bom` 同一写锁里查全局 operation_key 的父 Command。已存在时先精确核 actor/原完整签名，返回保存结果；不得被保存之后的来源 posted/active、当前 BOM、余额或货位可写资格拦住。
3. 仅 fresh 分支保留所有原业务资格，并在同锁内执行：组成员与父产品一致、各 receipt 的 `service.source`、completed job、原 output lot 来源/状态/版本、目标位置/布局、数量、成本继承与 CAS。不得因把读取资格拆开而漏掉任何新写资格。
4. 调用原 assemble 算法及独立 builder。新结果在 commit 前完整校验/序列化；proof 构造失败必须一起回滚，不能先保存业务再返回不完整的“成功”。
5. commit 后只返回已冻结结果；真实 commit 后异常仍按未知处理，readonly resolve 查持久事实。

首次与同 key 精确重放的完整写响应须一致；不加入动态 `replayed` 或当前库存摘要。当前 actor 只可为核对通过的原 actor。共享库存自动登记仍按原 enroll 执行；本 proof 不承诺可选共享登记已成功，故无需借本任务重构共享策略。若后续要显示这种承诺，必须补启用共享策略的真实成功测试，而不能只用静态调用关系证明。

## 4. 建议的最小证明合同

以下是语义要求及推荐字段，端点/字段名由根与 API/UI 最终合同固定；不是已实现接口。建议独立 `assembly_completion_receipt`、独立 schema，避免把 assemble 冒充 dispose 的新加工产出。

| 部分 | 最少冻结内容和核验要求 |
| --- | --- |
| 身份 | schema、原 operation_key、group_key、原 actor_id、父 Command 锚定 receipt、保存时间。与持久 Command/原请求对应。 |
| 原请求 | 原规范化 GroupAction 完整回显（含 jobs 原数组顺序）。沿原 model_dump 默认值、compact/sort-keys JSON 及 jobs[].lot_id=null 删除规则；expected_actor_id 排除旧签名。不得排序 jobs 或删去算法没用到的原字段。 |
| 配比 | 实际使用的冻结 parent/children recipe 身份、版本、per_set、代码/名称/单位；只白名单投影证明所需字段。原 result 的合法辅助字段仍兼容，不因 `available_output` 等历史附加字段把旧结果判损坏。 |
| members | 原任务全集，按 job_id 唯一映射；每个任务的 receipt、产品、原料来源身份与可空客户、已加工 output_lot_id、原 job/output 版本、冻结原位置和单位，以及本次组套结束时该原子件批次可用余额。包含 take=0 的成员。 |
| inputs | 仅实际 take>0 的投入；映射 member/job/output lot，记录本次 consume 流水 ID/key、数量、before_available、after_available、原位置/单位。不能只按 product_id 唯一映射，多 receipt/job 同产品合法。 |
| output | 本次唯一成套 lot/实际入库流水 ID/key、实际 sets、原目标 location/layout、冻结位置名称/楼层、原始台账单位和物理显示单位；与 `prep-kit:` 原键及本次入库事实精确对应。 |
| 原证据 | 与 assembly-inputs 证据及最终父 Command 的关联；只证明本次，不用当前库存/任务列表重拼。 |

数量核验：每个 input `before_available - quantity = after_available`；按产品汇总实际消耗等于冻结 `per_set × 实际 sets`。take=0 成员从同锁下真实 output lot 冻结本次结束余额，不伪造一条零 consume 流水。原任务集合与原 body 集合完整相等；proof 数组可按稳定 job_id 排列，但通过 ID 映射，原 request 数组不得改序。按原本批次出库算法取用，不扩大到被移动后的别的 lot 家族。

余片必须标“本次组套后余片”；以后别人取走、移动或再次组套，不回写这个历史证明，也不把它称实时库存。多种子件数量不能无单位混在一条“套数”里；每个子件分别显示数量/单位，实际成套数量单独显示为套。台账原单位与物理显示单位可不同，不再次乘模数、开料份数或每箱件数。

兼容边界：原 assemble 算法忽略 request.disposition、actual_output、原料 lot_version、逐行目标等字段的一部分，但原签名仍包含它们。不得套用 dispose 的 actual_output>0 校验、强制旧合法 request.disposition=finished，或声称本次重新生产了子件。证明描述真实成套结果，与保留完整原请求并不矛盾。customer/floor/物理单位沿真实可空，location_name 允许合法空字符串；不新增非空门槛或把 null 换成猜测值。

## 5. 只读恢复与旧数据

推荐新增独立 `group-assembly-result` 只读 POST，body 为 operation_key、完整 original_request、expected_actor_id；action 必须 assemble，外层与内层 key/actor 一致。UI 总是发送原持久 owner；后端 expected_actor 可沿旧 API 兼容保持可选，但严格正整数且不进入旧签名。不能根据当前账号接管原记录。

在 `no_autoflush` 下读取，成功/未查到路径无业务 INSERT/UPDATE/DELETE、flush 或 commit，不调用会执行或判断新写资格的 helper。仍保留真实当前角色和客户范围门禁（标准认证失败审计沿原规则）。同时核当前父产品/原 job→receipt→source 身份、原 recipe/source/customer 范围和已保存 proof 身份；不存在、错配或无权时保留原请求，不泄露其他账号/客户结果。

| 结果 | 允许的页面行为 |
| --- | --- |
| completed | 仅新完整证明通过全请求、actor、投入/产出/成员对应核验；显示原套数、目标与本次余片，解开此请求锁。 |
| legacy_trace | 旧父 Command 的 actor、原 request_json 精确相符，旧结果只能给安全追溯摘要和入口。不得读当前 jobs/lots/BOM 拼成 complete，不回填历史 Command。保留未知记录。 |
| not_recorded | 此刻未见原父 Command。保留原 owner/key/body；可明确再查，或员工显式继续同一原请求。不能换 key、自动提交或宣告取消。 |
| conflict / forbidden / malformed / timeout | 保留原请求；错 key、其他 action、actor、数组/字段、证明损坏都不能降为 not_recorded。显示可执行核对说明。 |

旧嵌套 dispose 内部 assemble 请求可能不是完整 GroupAction；本入口不替用户推造其 body，不把这种嵌套记录接管成本次独立组套。旧无完整 body 也不猜 owner/数量；只给现有可读记录/管理员核对出口。

追溯入口应在当前页面显示可读组套/加工记录，失败可重试。原 key history 404 不作未保存证据；组历史 200 也不提升 complete。成功证明存在但当前列表/历史读取失败时，只标“已保存，列表/记录暂未刷新”，不能撤销 confirmed、重发业务或清除原事实。

## 6. 前端持久恢复与同组互锁

新独立 assembly 恢复模块，局部接实际 group_stock 保存分支。首次 POST 前持久化原 owner/key/group、完整不可变 body、可读原套数及来源/目标摘要，并读回核验；失败零 POST。每个原 key 独立记录，当前记录不能覆盖另一未知记录。请求发出后错误/空 2xx/证明不全均保留 unknown，关闭窗口和刷新不解除。

`groupStockLocked` 合并已发布 dispose unknown 与新 assembly unknown，按原 actor+group_key；开窗和提交前都读当前存储。双向互锁包括同组后续子件存放/assemble；其他组可正常操作。缺恢复模块时有关写入口保守禁用；避免两个模块递归调用同一锁函数。恢复卡各自使用所属记录，不先后套用两类 restore 导致原数据被覆盖；有多笔 pending 时逐笔显示，不猜“最新的一笔就覆盖所有”。

恢复卡放在列表 data-panel 的 empty/error 之外；全部组完后没有 group_stock 仍能看到原请求。卡片显示原套数、目标、本次余片/来源，主按钮“查原组套结果”；只有 not_recorded 且员工明确选择继续时，才同 key/fullbody 再发。普通首次保存不加多余确认。

未知请求不会被本功能自动终结；不加 tombstone、当前账号接管、跨设备原子互斥承诺。浏览器存储只是本客户端安全流程，不能宣称服务端永久取消或全局同组锁。没有原 key/body 的另一设备不自动拼接。

局部、有限超时的请求通道处理此次保存/恢复及相应读取；在任何全局登录副作用前隔离旧 epoch/actor 的迟到 401，同时保留当前账号真正无权时的处理。既有 group 选位 GET 保护不回退，不扩大改全局 Axios。成功后列表失败与存储清理失败分开提示：confirmed 原事实保留，清理失败保守留卡，禁止自动新 POST；读取追溯失败也不能撤销 confirmed。

## 7. 明确拒绝与未知不能混淆

只有同写锁下已证明原父 key 不存在、commit 尚未开始、业务异常且完整 rollback 成功，才能发送首次安全 Rejected。此响应必须只有 Rejected 和 no-store，不能同时带 Preserve；此前 group dispose 候选的双头问题不能复制。

锁前身份/权限失败、已存在 key 的冲突、commit 已开始、一般异常、回滚失败均不能给“确定未执行”。更重要的是，客户端只对本次首次请求且此前从未 unknown 的可信安全拒绝开放更正；任何已 unknown 的记录随后收到 Rejected 仍保留原内容并先核对。422/普通 409 不能仅凭状态码清记录。

## 8. 最短实施验收与阻断条件

建议作者每类一个高价值合成场景，独立审者只选最短反例，不复制大组：

1. 新部分/全部组套实际 HTTP 与当前 UI 跨验，完整首写/精确重放 JSON 相等、事实零增；原数组顺序和旧默认/null 签名不变；旧合法 disposition=semi、actual_output=0 等不新增拒绝。
2. 三 job/两产品仅两项非零投入，第三余额正确；连续两次部分组套真实 consume.before/after 及余片 7；冻结配比不受当前 BOM 修改影响。
3. 显式启用 append-only 触发器，证明父 Command 仅 INSERT；在父已 flush、后置 enroll 失败时全事务回滚；commit 后丢 ack 的只读恢复返回原事实且零业务 DML/新 POST。
4. 保存后改变来源资格/当前余额/位置，resolver 仍按原事实；当前 actor/角色/客户权限与冻原身份必须有效；原请求 actor 提示不得改变旧 request_json。
5. nullable 客户/楼层/单位及空位置名、真实物理/台账单位；损坏 proof、旧无 proof、错 key/actor/body、在途未见父记录均不误确认或泄露。
6. 首次真实 Rejected 无 Preserve 可更正，先 unknown 再 Rejected 仍保留；空/坏 2xx、超时、存储失败、刷新后原 group 消失、多 pending、同组 dispose/assembly 双向锁及异组可用。
7. 实际 HTML/mixin 消费最终 HTTP 档案，真实丢 ack→刷新→readonly 完整闭环；真实 Vue 编译/组件注册/恢复卡事件做非 DOM 技术验证。未获得浏览器/现场验收不得标通过，不重试此前被拒的 Chrome/进程动作。

出现以下任一情况应阻断候选发布：旧签名/合法业务被无意收紧；回调在父 INSERT 后修改 Command；enroll 移出事务；从当前余额重建原 proof；第二次组套余片算错；忽略原 actor 或源客户范围；未知可换 key/解锁同组；安全 Rejected 与 Preserve 混发；首/重放 JSON 不同；未验证权限、事务、幂等、备份/发布基本门禁。

## 9. 交付界限

本文是只读架构可行性建议，不是新功能上线证明。根已另开 `group-assembly-save-recovery-fix/TASK.md`（任务卡提交 799532cc）授权下一实施阶段；以该卡的文件所有权和边界为准，不把本轮审查当实施证据。最小文件范围为 assemble API/新 readonly、独立 assembly recovery helper、assemble 回调和 mutate_group 显式转发、新测试；UI 实际 group_stock 分支、新恢复模块/卡片及测试。既有 single/dispose 恢复模块和组套算法保留，邻近回归按风险完成。

正式状态仍为 v604（来源根发布记录）；后续新候选需精确 SHA、真实 HTTP、只读独立复核和正常发布安全门禁。浏览器 DOM、真实 Cookie/storage、多设备、窄屏、管理员/现场操作验收尚待实施候选，不以本次函数探针替代。

本轮一次精确搜索将 atomic_bom 文件名误写成不存在的 bom.py，随后用 rg 定位并读取 bom_transactions.py。artifact 指纹脚本初版按 index.html 后缀定位时遇到多个合法文件并停止，改为严格使用作者指纹中的实际路径后重跑；这两项都是只读定位/证据脚本问题，无源码或业务影响。所有既有审查来源、报告/探针指纹及历史精确 Git blob 核对记录见同目录 verification.json；新卡已允许 owner 复用管理树，当前 HEAD 不冒充旧审查 SHA。
