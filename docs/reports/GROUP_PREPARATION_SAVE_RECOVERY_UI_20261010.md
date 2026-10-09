# 组套加工保存结果恢复：UI候选回执

日期：2026-10-10。任务：GROUP-PREPARATION-SAVE-RECOVERY-FIX-20261010。

最终UI候选：`e578e90506fbcd654a6b2341fed75d447dcc8003`（运行文件前置 `1e6289cd8ea378753d75be42b11c168f9f9b39a2`，后续仅两个测试文件），分支 `codex/group-completion-recovery-ui-20261010`，工作树干净。起点为正式v603源码 `5b167953446d07d9b77718e05061099803932cfb`。API合同和实际HTTP来源为固定候选 `4d6808e705b4155b998a5a2978e98c5fc4eee1d0`；本回执不表示已经发布。

## 修改及员工操作

实际入口为生产备库 `group_job` 的保存入库，经 `saveStockDialog` → `saveStockGroupCompletion` → `sendStockGroupCompletion` → `POST /api/production/stock-preparation/group-actions`，动作 dispose。普通保存不增加确认步骤。首次业务POST之前，按账号与独立原operation_key持久保存完整原body、group_key、父产品及每job原投入/产出/位置摘要，并读回核验；保存或读回失败零业务POST。

空/坏/部分2xx、超时及不具备提交前拒绝证明的异常均保留原请求，显示待核对。关闭重开或刷新后恢复原内容，按账号+group_key冻结；同组后续group_stock的store_outputs/assemble在真正POST前也重新读取并检查。异组仍可操作，不以父产品或单一子件全局阻断。只读“查原结果”不自动续写；仅not_recorded的时点观察后，员工明确点“按原内容继续”才发送原key及完整原body。无永久注销或假零流水。

仅完整proof核验当前/历史actor、key、group、完整规范请求、数组顺序、冻结配比及逐job来源、版本、数量、位置、产出lot和流水后才确认。按job ID映射，合法多receipt同产品、多job以及assembly非零inputs子集均支持。semi少产14/19仍显示原全投入15/20，无单项continuation；finished子件加工与成套入库分列，余片明示“本次完成后余片”，不当成当前库存。nullable来源客户/单位/楼层与空货位名称不臆补；未知物理单位显示单位待核对，货位缺名保留ID辅助核对。

成功proof、列表刷新和缓存清理分开处理；成功卡保留可读加工记录及只读刷新。旧无proof只提供原记录追溯并保留请求，不冒充完整成功。history仍依赖现有当前来源写资格，读取失败只显示追溯暂不可用和重试，不撤销confirmed、不重发业务POST。

局部group请求通道继承现有认证必要默认值、携带cookie，并设60秒有限等待；不修改全局Axios。迟到401/成功核当前账号及生命周期，普通当前401仍走现有退出规则。单项模块保持基线字节一致。

## 最小安全红与最终验证

修前实际页面方法7组：6组安全红、1组原payload绿色；复现4种坏2xx误成功关框，以及未知后改数/关闭重开导致原内存请求丢失。它们不构成原key重复扣减的证明。

最终实际HTML/mixin＋模块定向回归 **57/57**：见 `final-group.json` 与 `final-group.xml`。单项既有回归 **49/49**（1e6289cd结果复用；所有运行JS/HTML与单项模块字节未变，本次未重复运行）：`final-single.json/xml`。Python包装 **1 passed（0.57s）**：`final-pytest.xml`。`git diff --check`通过，最终提交只有五个白名单文件。测试覆盖原body持久/重入、双storage控制器不覆盖、同组group_stock写前守卫/异组可用、未见显式继续、首次Rejected可更正与旧unknown后Rejected仍冻结、真实和迟到401、选位/地图链、超时、列表/清理失败、旧A不覆盖B、history失败保成功。

10个真实HTTP档案完整嵌入CJS（业务原文未手改），对应 `../api/http/*.json`。逐字段去掉archivePath/archiveSha256元信息后与源档案完全一致、原始SHA及API来源一致，**10/10**：`http-embedding-check.json`。原request模型缺省值、lot_id:null排除和UI原生成body以规范请求核验；首写持久化后重试始终完整原body，不重新组装。

真实postcommit500档案 `committed-ack-loss.json` 是同一隔离库finished2套：实际写成功但丢ack→新页面从真实workspaceAfter GET看到group_stock→同组后续写被挡→只读resolve确认→读取可读history，**总新业务POST为原首次1次**。该原文包含原sourceRow/workspaceBefore、500、resolve、workspaceAfter、history/replay。数量事务事实由API实际HTTP验证支持，页面模拟不能替代真实库事务验收。

项目实际Vue编译器编译完整恢复组件和两处实际HTML入口，非DOM renderer触发核对/继续/追溯/只读刷新及busy禁用；Vue.reactive实际保存/追溯路径验证响应式可见状态。纯UTF8 SHA覆盖ASCII、中文及JSON字符串引号，无LAN crypto.subtle依赖。

## 纠正记录及边界

- 初始“缓存清理失败后异组可用”夹具错误复用A操作key，已改独立B key；它是测试夹具问题，不计ERP缺陷。
- 根复核发现嵌入archivePath反斜杠被re.sub替换解释；已改正斜杠及函数replacement，并重跑55+49及原文9/9，未改业务JSON。
- 跨页localStorage不是原子锁，不宣称阻止所有跨设备/并发新写；独立原key记录不互相覆盖，已读到同组待核对时保守阻断。异常记录/整体读取失败提供可读错误和重新读取，不静默删除。
- not_recorded不是永久未执行，无法安全结束的请求仍保留原key/body并给原请求和管理员核对依据；本卡没有注销机制。旧proof缺失不能提升为完整确认。
- 本轮按任务卡不再尝试Chrome启动或停止被拒的旧进程；无新Chrome PID/服务进程、无截图。浏览器DOM/Cookie/实际localStorage、窄屏与管理员现场验收仍pending，Vue非DOM及页面函数验证不冒充现场通过。
- 旧18161/18162/18163、PID15248和被其引用的旧工作树未操作；无正式数据操作、版本、迁移、push或发布。

## 文件、指纹和交接

仅修改 `static/index.html`、`static/ui/production-workspace.js`，新增 `static/ui/stock-preparation-group-recovery.js`、`tests/stock_preparation_group_recovery.cjs`、`tests/test_stock_preparation_group_recovery_ui.py`。script引用使用相应文件内容hash。单项恢复模块和全局Axios不改。

`final-source-fingerprints.json`记基线、最终UI SHA、API SHA/源码指纹、五文件LF SHA、单项模块LF SHA及十HTTP档案原始SHA；提交字节与已测文件LF hash逐项相同。五文件已释放，由根串行整合/独立复核/发布。

独立复核最短命令：`node tests/stock_preparation_group_recovery.cjs --filter "真实freshRejected|真实多receipt|真实commit丢ack|实际Vue响应式|实际Vue编译"`。完整脚本可同目录直接运行，专用pytest包装会执行完整57组。

## 独立复核补齐：实际首次安全拒绝

根及独立审查在初候选1e6289cd后发现候选API fresh错误同时带Rejected/Preserve，和已定安全拒绝出口矛盾，UI按保守优先一直保持unknown。这是候选跨合同缺口；不计为正式v603原业务缺陷。UI业务判定不放宽，API后续4d6808e7仅修fresh安全拒绝移除Preserve。

最终API10个HTTP节点重新生成（包括原9包和fresh-rejected），完整原文已更新CJS，来源统一最终4d6808e7并逐字段和原始SHA核对10/10。旧3bc包由API保存在http-3bc42151-before，UI原55/49与初报告保存在before-safe-rejection-*，没有覆盖抹除旧观察。

新增两例调用实际HTML/mixin的openStockDialog/saveStockDialog/sendStockGroupCompletion：

1. 首次真实409仅Rejected/no-store、beforeCounts=afterRejectionCounts且只读not_recorded，未产生事实。页面保留弹窗并解除原新请求记录，可更正原数量，busy退出且不显示成功。准确原body通过规范字段比较发送一致。
2. 同原body先遇坏回执成为unknown，后只读not_recorded，员工显式继续遇同真实Rejected仍保留原key/body、同组锁及核对入口，不凭后来拒绝抹去先前未知；无自动POST。

fresh原文故意lot_version+100（102对当前2）触发真实版本门禁。sourceRow是当前真实GET。页面入口测试只在其clone上投影原request版本，明确为“版本不匹配的合成页面投影”；未将它冒充真实陈旧GET或库存事实。真实接口零DML/回滚事实由API档案counts支持，页面函数证据仅证明错误出口/未知状态行为。

最终test-only后续提交e578e905，group57/57与pytest包装1通过。static/index.html、production-workspace.js、新group模块及原single模块与1e提交逐字节一致；仅两测试更新，无额外UI业务改动。指纹同时保留runtimeUiCandidate与单项原49复用依据。
