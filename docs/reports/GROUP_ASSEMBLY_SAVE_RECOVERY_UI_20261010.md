# GROUP-ASSEMBLY-SAVE-RECOVERY-UI-20261010

本轮候选只修已加工子件 `group_stock` 的组套保存结果恢复。正式基线为 v604 / `75129569ed5ff0bf454d075471c9b4fd902b070f`，工作树 `D:/.codex/worktrees/group-save-recovery-ui-20261010/纸箱厂erp软件搭建`（实际路径见 source-fingerprints.json），分支 `codex/group-assembly-recovery-ui-20261010`。候选提交见本报告末尾。尚未发布。

## 结果与实际入口

实际入口是 index.html 的组套按钮 → `saveStockDialog('assemble')` → `saveStockAssembleCompletion` → `sendStockAssembleCompletion` → 原 `POST /api/production/stock-preparation/group-actions`。新增独立 assembly 组件，不改已发布 single/dispose 恢复模块。

保存前按原账号和原 operation_key 独立持久保存完整 body、套数、子件编码/名称/原位置及成套目标。读写失败不发送；空、坏、部分成功响应保持未知，不报成功，不换键或清原内容。重新打开和刷新可从原恢复卡查询结果；只在 `not_recorded` 后员工明确点击“按原内容继续”才发同键同 body。未见记录不表示取消，旧证明不足只给可读组套记录与管理员依据，保留请求。

同组 assemble/dispose 未知互锁；同组已打开的弹窗在真正发送前再次核对。他组可独立使用。恢复不按产品合并 job，多来源和原数组顺序保留；两类记录并存时逐笔展示，不猜某笔覆盖弹窗。确认但缓存清理失败仍保留同组保护。

新局部认证通道有 60 秒等待上限，覆盖本次开窗、货位/地图、保存、核对、列表刷新和可读追溯；继承 Axios 默认认证配置，不改全局拦截器。旧账号迟到401或成功不污染新账号，当前401仍触发原登录处理；超时仅退忙并保留未知，不表示撤销服务器执行。A晚成功不关闭同账号正在编辑的B。

完整证明核原账号/key/group、完整规范原请求、冻结配比、job/receipt/product/版本、成套数量/批次/流水、完整 members 与非零 inputs、逐笔余额及证据键。原忽略字段保留签名，不硬等于组套事实；合法 semi/actual_output=0、反序 jobs 兼容。`output_locations` 是余额过滤后的不完整投影，耗尽 take0 成员仍必须在原 body/proof；有投影时只附加正向位置核对。纯 UTF-8 SHA256 不依赖 HTTP LAN 缺失的 crypto.subtle。

成功卡显示“本次组套”“本次取用”“本次组套后余片”，明确是原请求当时事实，不当作当前库存。物理单位 null 显示“单位待核对”，不猜只/张；底层库存单位另列。成功、列表刷新失败、历史读取失败和本机清理失败分开。追溯通过既有可读历史弹窗，不跳原始JSON。来源后来变化使 history GET 失败时，完整确认仍保留，不能降未知或重发。

## 最终验证

- 新实际 HTML/mixin/module 回归：**54 / 54**，`final-assembly.json`、`final-assembly.xml`。
- 已发布 group 相邻：**57 / 57**，`final-adjacent-group.json/xml`；旧CJS仅加载实际新组件适配，原断言和真实HTTP档案未改。
- 已发布 single 相邻：**49 / 49**，`final-adjacent-single.json/xml`；single CJS及两旧恢复模块与正式751逐字LF相同。
- 专用 pytest 包装：**1 passed**，`final-ui-pytest.xml`（包装执行上述实际54场景，不是额外54个独立用例）。
- 最终 API 作者13个实际HTTP包 + 独立审者1个实际HTTP包：**14 / 14 原文等价**，`http-equivalence.json`。去除 archivePath/archiveSha256 元数据后逐字段等于原文件，原始文件 SHA256 和最终 API `f767ebf874ed672ebb99b51cfd9b39529414dfac` 均相符。
- `git diff --check` 通过；资源查询串按当前新组件和 workspace 的 LF SHA256 前12位固定。

上述54场景包含存储失败、双实例不同键不覆盖、改数/关闭重开/刷新、两类同组锁、坏证明6种、在途/忙态、not_recorded显式继续、fresh 独占 Rejected 可更正与先前 unknown 后同头仍冻结、迟到401/成功、当前401、超时、地图/列表晚回、缺组件可读失败、成功后刷新/追溯/清缓存失败，以及实际 Vue reactive 方法调用和真实模板的非DOM渲染/按钮事件/忙态。

### 最短真实HTTP生命周期证据

1. `api/http/committed-ack-loss.json`：最终后端真实 commit 后500，持久事实已完成。同原 body经实际页面保存错误路径保留；新页面读同库 workspace 中 group_stock，原组后续 store_outputs/assemble 被锁；只读 resolve 完整确认后可读 history。实际业务POST计数1，恢复没有新增业务POST。该档案的部分2套后原 group_stock仍存在。
2. `review/independent-third-full-zero-member.json`：独立后端第三次2套全耗尽，真实 sourceRow 含已耗尽 job 的空 output_locations，之后 workspace 没有原 group_stock。页面接收真实首写成功内容时注入“响应未到达”故障（不是另一次服务器真实500），刷新实际空列表后列表外卡仍显示原请求，readonly完成，完整成员保留，业务POST仍1。
3. `second-partial-depleted-member.json`：第二次部分组套 take0 耗尽成员空投影合法，全部原成员保留，本次 before-consumed=remaining，显示本次余额而非最初加工量倒算。
4. `fresh-rejected.json`：真实明确回滚零事实且独占 Rejected；首次请求可修改。先unknown再收到同Rejected不得清 body，继续保留核对入口。

## 证据限制与收尾

本轮遵任务卡不启动或重试 Chrome，不开服务，不触碰旧服务/PID，不执行正式页面或正式数据写入。未取得本轮浏览器截图。实际方法、真实HTTP消费和Vue非DOM技术检查不能代替浏览器 DOM/Cookie/真实 localStorage、屏幕可见性或管理员现场验收，均 **pending**。storage跨页读写非原子锁；每笔独立键保证互不覆盖，恢复后同组保守锁，不声称消除所有同时初发竞态。

未见记录但原版本无法再执行时仍保留原请求供只读/管理员核对，本轮不提供注销/跨账号接管。历史读取沿旧当前来源资格，可能失败；成功证明不撤销。成套/成本/库存算法未改。

本轮没有新进程，无需停用；早先审批拒绝导致保留的18161/18162/18163及PID15248完全不动，未借其他工具绕过。无后端、版本、迁移、正式数据、push、发布或其他任务文件改动。独立审查和根最终整合尚由根负责。

## 最终候选

UI提交 `cd556f07004cf610700bdb8d31e6e43186d361ee`，工作树干净；6个文件均为任务白名单。根和独立审者已收到精确SHA及最短节点运行方式；本报告不代表根最终发布回执。
