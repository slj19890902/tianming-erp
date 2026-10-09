# 生产备库加工完成：只读页面结果审查

日期：2026-10-10。状态：只读审查完成，尚未实施恢复修复或正式页面验收。

本轮确认两个生产保存缺陷族：坏2xx被当作成功；未知请求仅保存在可变页面对象中，关闭、改数或刷新后丢失原键和原内容。另确认共享网络拦截器的迟到401能注销新账号。已有同内容原键重试、迟到成功/列表GET保护和写成功后列表失败提示继续成立，不重复计为缺陷。

## 当前源码与边界

- 只读工作树：`D:/.codex/worktrees/chain-production-audit-20261009/纸箱厂erp软件搭建`，HEAD `0b3189d0dcc38a7ccbec5237234f72d681b84df4`，本轮未修改源码、仓库测试、分支、版本或服务。
- `production-workspace.js`、index内生产方法和加工入库弹窗分别按LF规范化哈希，与HEAD、v601来源 `108de0681c73fc213f94dd123d077eec395a8186`、根 `6640d5369e2d48ecba36be9aefae1afa9ed449db` 一致。详见 `source-fingerprints.json`。
- 执行实际完整inline Vue methods和实际 `ERPProductionWorkspace` mixin。关闭事件从真实HTML按钮提取；重新打开走 `openStockDialog`；刷新走 `loadStockPreparation`→`loadStockWorkspace`。
- 本线只合成HTTP响应/故障和任务行，不模拟后台库存算法。没有Chrome截图、正式页面点击、正式数据写入，也没有停用或更改保留的隔离服务。

## 真实主入口和原请求

`static/index.html:4982` 加工入库按钮→`openStockDialog(row)`。single_job弹窗 `index.html:4913` 保存入库→`saveStockDialog('complete')`（`static/ui/production-workspace.js:142`）→`stockPrepAction`（index约20516）→`POST /api/production/stock-preparation/{receipt_item_id}/actions`。

原body包含 `action:'complete'`、`lot_version`、`quantity:0`、`job_id`、`job_version`、`actual_output`、`output_kind`、`output_version`、`location_id`、`layout_version`、`confirm_overproduction`、`actual_input_quantity`、`operation_key`。实际投入来自弹窗 `inputQuantity`，产出来自 `job._actual`，地址来自弹窗 `location`，输出用途默认为 `semi`。页面的物理加工动作不能从这份软件请求自动推断。

相邻group_job按钮实际是 `saveStockDialog('dispose')`→`/api/production/stock-preparation/group-actions`，默认半成品分存。页面没有group complete按钮，本轮没有把直接调用该API当员工主入口。

## 复现和已保护行为

证据：`probe.cjs`、`results.json`、`results.xml`。运行 `node probe.cjs`，14/14观察断言通过；这些断言证明现有行为/缺陷可复现，不是修复后的回归通过。

| 证据节点 | 实际结果 | 判断 |
|---|---|---|
| single-complete-bad-2xx 的null、{}、空字符串、无效JSON四组 | 不读取响应业务内容，关闭弹窗并报“已保存，本次库存已更新” | 缺陷族1：不完整回执误报成功 |
| single-unknown-unchanged-explicit-retry-same-key | 保留当前弹窗且不改数时，显式再点保存使用相同key/body | 现有窄范围幂等保护，不是跨刷新恢复 |
| single-unknown-edit-mints-new-key | 未知后改投入/产出20→21，新键发新body | 缺陷族2：未知原请求未冻结 |
| single-unknown-actual-close-open-resets-original-input | 实际关闭事件后实际重新打开，投入/产出从原20恢复默认100，再保存换键 | 同缺陷族2，非额外计数 |
| single-unknown-refresh-current-list-loses-key-and-body | 新页面实际GET列表后打开任务，无原key/body，同20内容也换键 | 同缺陷族2；合成GET返回待加工行不证明原动作未成功 |
| single-inflight-actual-close-enabled-and-original-request-not-durable | single仅全局busy，弹窗loading仍false；真实关闭按钮可关闭；丢响应后无持久恢复 | 同缺陷族2，在途也可失去可见核对入口 |
| single-full-ack-list-get-failure-retains-success-message | 合成HTTP成功后GET失败，仍给“已保存；列表刷新失败…不要再次建单” | 已有保存/刷新分离正确；本节点的 `{ok:true}` 仅HTTP成功替身，不是完整业务证明 |
| single-late-post-after-actor-change-ignored | 旧成功回到新账号后，无关闭/成功提示/GET/缓存写入 | authGeneration业务保护有效；不从辅助busy状态推断新缺陷 |
| single-account-change-during-list-refresh-late-get-no-new-dom | 迟到GET不覆盖B数据，不报A成功 | 列表自身authGeneration有效 |
| single-late-401-actual-shared-interceptor-signs-out-new-actor | 旧401先触发实际Axios拦截器及实际mounted账号失效回调，user B变null、generation再次加1 | 共享通道边界：局部业务guard执行太晚 |
| adjacent-group-dispose-empty-2xx-success | 空2xx关闭并报“半成品已分存” | 缺陷族1的同family相邻对照 |

401探针的finance/page/modal清理辅助函数是no-op；`user=null`、账号代数变化和登录提示由实际账号回调执行，Axios响应拦截器亦为实际源代码。它不证明正式用户当前遭遇过该问题。

## 与真实后台事实对齐

API独立合成真实HTTP/数据库证据位于同级 `../api/`，6项通过，见其 `api.xml`；本线只读取结果，不重新实施事务。

- `normal-8.json` 首次成功实际为200：`action:'complete'`、`job_id`、`completed_job_id`、`continuation_job_id`、`remaining_input_quantity`，部分投入另有 `original_job_id`。现回包没有原key、投入/产出、用途、地址、输出lot或当前认证账号证明。不能只加“非空JSON”便声称完整原请求已核实。
- `committed-ack-loss.json` 首次投入8提交后丢回执500，已消耗/产出8；精确原键200重放零新增。旧任务改body/新键被409挡住。真正重读后的续加工任务、新版本、新键再投入8可合法200，总消耗16、待加工4。结论是未知恢复丢失会让员工误把下一批当上一笔重试；不是原键重复二扣，也不能推断现场物理加工了第二次。
- 原operation key并不是当前history路径的精确核对入口；该路径按job/group追溯，不能用列表状态替代原请求证明。
- `actor-permission.json` 实际认证boss2、附加 `expected_actor_id:1` 仍首次200，Command记actor2，因为当前模型忽略该未支持字段。普通权限/客户范围拒绝仍成立，不能描述为越权。UI晚回保护不阻止服务器按变化后的Cookie执行。

## 下一最小修复建议（方案，未约定新API字段）

1. 首POST前保存不可变原endpoint/action/key/body/owner及产品、来源、投入、产出、单位、用途、目标货位。每原请求独立键，按账号隔离，保存失败不发送。未知后禁止改原内容或生成新键；可关闭离开但重新打开/刷新必须列原待核对记录。只约束关联原加工任务/来源，避免一笔未知锁全账号。
2. 后端以现有Command持久request/result和真实库存/任务事实生成完整保存证明，并提供权限、客户范围、签名和账号门禁下的精确只读核对。先共同固定合同再实施；现有job/history仅作追溯入口。部分投入必须核原job与完成子job/续job映射，不能硬要求完成job等于原job。
3. 只有完整匹配当前身份、原key、冻结原body及持久完成事实才展示成功并清自己的记录。空/坏/部分2xx、超时或此前未知后的409均保留。未找到只说明本次未见，不等于取消；显式“按原内容继续”才发送同key/body。首次明确提交前拒绝若需允许更正，必须由服务器证明回滚且无已完成原key，不能凭409推断。
4. 成功信息与列表刷新分开，保留目前单任务已有安全提示，并补可见完成记录/只读查看入口。成功后缓存删除失败保留confirmed，不能重新加工；刷新按钮仅GET。
5. 首写强制当前actor意图并在服务端核验。该生产局部通道在共享401/403副作用前核当前epoch/账号，有限等待后退出忙态但保留未知；不自动重发、不全局重写网络层。真实在途关闭禁用与提交忙态应一致。

最简员工文字：“这笔加工结果待核对：原投入20张，产出20只，半成品入三楼B2。先查结果。”按钮“查原结果”“查看加工记录”；完整证明后“已入库：20只，三楼B2”；not_found后“本次未查到原请求，未证明取消”，显示原内容和明确“按原内容继续”。没有安全结束证明时保留记录并提供原账号、来源、任务、原键和时间供管理员核对，不提供一键删除。

必要定向回归：合法全量/部分投入完整回执；空/截断/错身份/错地址/错数量；首写前存储失败；未知后改数、真实关闭/重开、刷新、多标签不覆盖；同键重放零新增；续任务不得冒充原请求；未知后409保留；切账号迟到成功/401/GET；首写actor错配零写；写成功GET失败及缓存删除失败；只读resolve零业务DML及当前客户权限。相邻group dispose先确认同合同可覆盖，避免本卡扩大到所有生产写入口。

## 交付核验

本轮没有代码提交。结束再次核HEAD及工作树干净；服务源及原进程保持不变。此报告和NAS独立回执仅交付只读证据/方案，正式员工及物理操作验收仍未完成。
