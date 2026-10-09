# 整组加工保存恢复：独立候选复核

日期：2026-10-10。结论：**最终候选独立技术复核通过，无剩余发布阻断。** 发现的一处候选 API/UI P2 头标记冲突已由负责人修正并完成真实回包复验；不计作正式 v603 的既有 Bug。此文不代表已发布或管理员现场验收。

## 精确候选与范围

API 最终：`4d6808e705b4155b998a5a2978e98c5fc4eee1d0`，前置 `3bc42151605033c81ae41c2c639adfd59364fbce`，目录 `D:/.codex/worktrees/group-save-recovery-api-20261010/纸箱厂erp软件搭建`。先独立核 3bc HEAD、tree clean、5 个授权文件指纹和 `git diff --check`；再核 4d 只对 API 安全首拒绝分支增加 1 行移除 Preserve，以及专用真实 HTTP 测试。三个 recovery/disposition/groups 服务未变。共享 API 中 7 个 single 节点 AST 与正式基线 `5b167953` 完全相等。未修改任何仓库、测试、正式数据或进程，没有 push、浏览器/IAB、正式页面操作。

UI 最终：`e578e90506fbcd654a6b2341fed75d447dcc8003`，运行文件前置 `1e6289cd8ea378753d75be42b11c168f9f9b39a2`，目录 `D:/.codex/worktrees/group-save-recovery-ui-20261010/纸箱厂erp软件搭建`。e578 仅更新两份测试，三个运行文件和 single 模块与 1e 字节相等；最终 tree clean、5 文件 LF 指纹匹配、diff-check 通过。源/机器证据均绑定这两个最终 SHA。

只执行本目录的安全 synthetic conftest runner；测试数据库来自 tmp_path，不复制整库，不触碰正式库。机器结果、精确源/证据 SHA256 见 `verification.json`。前一轮合同预审见 `CONTRACT-REVIEW.md`，不将方案等同于实际验收。

## 后端独立结论

修正后未发现 API 剩余阻断。原完整请求签名与 actor 门禁、同锁 fresh 判定、父 Command 首次 INSERT proof、不可改触发器、原 key 重放和只读恢复符合已定合同。数量/组套业务算法未调整；其他 group 动作只保留原转发，single 七处 AST 不变。

`independent-api.xml`：**2 passed，6.073 秒，0 skipped / failure / error**，权威运行来源为 3bc。下列两节点对应的保存/只读服务在 4d 字节不变；没有为改标签重跑或篡改原真实包来源：

1. **多来源、少套、原签名和不可改流水。** 真实收料拆为两次产生 3 job / 3 receipt / 2 product，以倒序原 jobs 保存 finished 1 套。组套仅取 2 job，另 1 job 完全未参与组套，完成后余片合计 28。逐 job 核原料 consume、首次 manual_in、实际 output lot；逐 assembly input 核真实 consume movement/key/数量；按冻结配比汇总，而非按数组或单产品唯一 job。保存 SQL 不含 Command UPDATE/DELETE；另显式尝试两种禁改 SQL 均被实际 trigger 拒绝。原 payload 去掉 expected_actor_id 和 lot_id:null 后，原 key 重放整个 JSON 仍相等；反转原数组的查询为 409，原内容恢复 200，业务事实不增。
2. **可空资料与后续变化不覆盖原保存。** 来源 customer=null、冻结显示单位缺失、位置名称为空的真实 semi 首写 200；投入 15/20，产出 14/19，assembly=null。保存后只在合成库改变 job/receipt 状态、当前输出余量和位置名称/楼层，再用原 body 查询。禁用 Session.commit 和当前写资格 source；逐 SQL 捕获为零 INSERT/UPDATE/DELETE/REPLACE，恢复 200 no-store，proof 与首写完全相同，全部业务事实保持。此节点没有让未配置楼层的新加工绕过旧选位门禁；它检验的是保存后当前楼层变化不覆盖原位置。

真实独立包：`independent-multisource-real-http.json`、`independent-nullable-real-http.json`，均带上述 sourceCandidate。前者 `command_update_delete_attempts=0` 指自动保存期间的 SQL；显式触发器检验另尝试 UPDATE 与 DELETE，均拒绝，不应误解为整个探针没有任何拒绝尝试。

## 复用作者证据的准确口径

逐项核 XML、节点清单、9 个 HTTP 最终包的 SHA 标签和源指纹：`api-final.xml` 28 节点（24 新 group + 4 single）；`ack-and-group-adjacent.xml` 3 次执行，其中 ack-loss 是同节点改 finished2/余片的后一次权威证据，另 2 个为既有 group 邻近保护。因此作者最终 **30 唯一节点 / 31 绿执行**，不合计成 31 个独立场景。

复用作者实际前提交回滚、真实提交后丢 ack、在途 not_recorded 后原提交成功、权限/原当前客户范围、旧无 proof legacy_trace、不完整证据拒绝等覆盖。未声称本轮逐项独立重跑它们。最后 `committed-ack-loss.json` 是同一个合成数据库 finished2 首回 500 → resolver 200 → 真实 stock workspace 含 assembled_stock 和 group_stock，能用于前端同组未知锁的实际链路，早期 finished5 包不冒充此证据。

## 独立发现并修复的 P2

3bc `_group_headers()` 总带 Preserve=1，安全 fresh 回滚分支又加 Rejected=1；UI 1e 的解除条件是 fresh、从未 unknown、Rejected 且不 Preserve。因此首个真实版本校验失败虽然明确没有保存，仍被 UI 保留成 unknown，只读 not_recorded 后只能原内容重试，无法更正旧版本。这是具体跨合同 P2，并非原 key 二扣或权限问题。根批准负责人修 API 唯一 safe-fresh 分支移除 Preserve；UI 保守 unknown 规则不放宽。

独立旧观察 `independent-rejected-api.xml` 1 passed / 3.47 秒只表示“确实复现双头、零业务增量、查询未见”，不是通过预期用户体验；原 JSON 与原探针另存 before-fix。最终 4d 同节点 `independent-rejected-fixed-api.xml` **1 passed / 7.36 秒**：409 只有 Rejected/no-store、没有 Preserve，仍零业务增量、查询 not_recorded。实际错误版本是合成请求人为加 99，未声称真实界面取得过该版本。

负责人对 4d 重导原 9 包加 fresh-rejected 共 10 包：`final-sha-http-export.xml` 10 passed；不是简单改 sourceCandidate 标签。fresh 包另覆盖同 key 更正成功、已存在 key 冲突仍 Preserve。旧 3bc 包保留在 `http-3bc42151-before`。新增 fresh 节点后作者总唯一节点是 31，重导/复跑不叠加成新场景。

## 最终 UI 与跨合同独立验证

`probe_candidate_ui.cjs` 对精确 e578 实际加载 index inline、production-workspace 和两恢复模块，调用真实已安装方法；`independent-ui.json` **3 项通过**：

- 两份独立 API HTTP 原文分别经首回完整证明校验、真实 `loadStockPreparation` 重新读取记录、`resolveStockGroupCompletion` 只读核对。每例 1 次 resolver、**0 次业务 POST**，原 body 不重建，确认后缓存清理。随后真实 `viewStockGroupTrace` 方法收到 409 时，confirmed/proof 保留，未触发业务写入。multi 为 3 job / 2 product / 2 inputs；nullable 来源客户、物理标签与空位置接受。此两包 API 来源仍明确为 3bc，不冒称重导自 4d。
- 这两包的浏览器恢复摘要是按合成原计划明确声明的测试元数据，不含 captured sourceRow；因此它们验证实际恢复方法的消费兼容，**不声称同库首次页面保存链**。完整首次保存到新页面的链路另由下一条最终 4d 实际 sourceRow/workspaceAfter 包支持。
- 项目真实 Vue **3.5.40** 编译并以自定义非 DOM host 渲染 **6 个模板**：新增组恢复组件、原 single 组件、两个实际组入口、实际 preparation section、实际 data-panel。验证空列表/错误仍显示组卡、五类事件绑定、未查到前隐藏继续、忙态禁用、空标签/位置回退、成套数与本次余片分层；**0 warning / 0 error**。缺表达式的负语法控制正确拒绝。

另对作者最终 e578 脚本独立只运行最短 **4/4**（`independent-ui-selected.json`）：首阶段 Rejected/unknown 后区别；最终 4d 真实 freshRejected 首拒绝可更正；同真实包在先前 unknown 后仍保留原 body；真实 finished2 丢 ack → 同库 workspaceAfter group_stock → store_outputs/assemble 均被未知锁挡住 → 只读确认 → 可读加工记录。该链总业务 POST 只有原首次 1 次，没有追加。fresh 页面仅对当前真实 sourceRow 的 clone 投影故意错误版本，不宣称历史实际 GET。

作者最终 group57、pytest 包装1证据已核，single49按 1e 运行文件不变复用；本独立任务不重跑全部。最终内嵌 10 HTTP 包逐字段、原始 SHA、archivePath 和 sourceCandidate 均与 API4d 档案相符。运行 JavaScript 无新的放宽错误条件或财务字段。

## 已知边界与过程记录

旧 history/completed_groups 仍使用来源当前写资格，所以来源状态后变可能令追溯 GET 失败；不能因此否认独立 resolver 的持久证明。冻结 remaining_stock_quantity 是“本次完成后余片”，不是实时库存。此两项已给 UI，并须在最终页面函数验证保持 confirmed、不清原请求、不发业务 POST。

一次只读检查脚本漏设 read_text UTF-8，读取 HTTP JSON 时发生 GBK 解码错误；随后明确 UTF-8 完成读取和验证，不涉及业务请求/应用失败。独立两节点首次有效执行全部通过。测试警告为既有合成 conftest 的短测试密钥和 Pydantic 字段元信息提示；已实证 expected_actor 不进持久签名，没有据警告修改权限或生产配置。

自有 Vue 探针首次把部分 document 提前注入浏览器 bundle，导致探针的 `createElement` host 缺失；之前两个实际方法用例已完成。仅把最小 v-model document 钩子延后至 Vue 加载后，与既有非 DOM 模式一致，未改应用或伪造 DOM。纠正后完整 3 项重跑通过；`independent-ui-initial-failure.json` 保留原因，不计 ERP 缺陷。

最终报告和 `verification.json` 写 NAS 独立回执并校验一致。真实浏览器 DOM/Cookie/storage、窄屏及管理员现场验收仍 pending，禁止将非 DOM 检查称为 Chrome 验收。正式发布、安全备份和根集成由根负责，本子任务未发布。只有上述明确范围已完成；group 其它动作算法的独立恢复、永久安全结束和跨设备全局锁均不在本闭环内。
