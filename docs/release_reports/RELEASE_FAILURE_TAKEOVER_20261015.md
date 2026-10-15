# 本次发布失败复盘与接续执行单

后续结果：2026-10-15 16:47 已完成 v0.22.639 正式技术发布，见[正式发布回执](V639_FORMAL_RELEASE_20261015.md)。下文为发布前复盘与当时状态，不替代最新运行事实。

状态：本轮接手完成根因定位与最小源码修复，隔离回归通过；修复尚未签名、尚未正式发布。正式业务继续运行 v0.22.635。本文不把监督程序 v0.22.639 当作业务发布成功。

当前用户已授权本轮必要系统验证、隔离修复、提交和通过验证后的正式发布。单负责人串行；沿用既有数据安全边界，不另启发布流程。早期任务卡的“仅建议/只读/不发布”不覆盖当前授权。

## 1. 真实现场

- 正式业务进程 PID 85385、UID 499，8000 监听；监督进程 PID 35919。最近一次事务 PID 84640 已退出，无该候选迁移进程。
- 业务包 `f3243b30e3b6752877657318fd25f2ffa54c3fbe6371866de004a90520729377`；当前业务 v0.22.635。局域网入口 `http://192.168.3.80:8000/frontend-v2/`，本轮只读 HTTP 200，health `{"ok":true}`。
- 受保护的只读诊断直接读取正式 SQLite：revision `ep1010md`，quick_check=ok，FK 违规 0；组套命令 receipt_item_id 仍 NOT NULL，原有 no_update/no_delete 触发器及收料项索引存在。最新失败没有进入正式迁移。
- 正式业务表数量：sales_orders 234、sales_order_items 1008、inventory_lots 1470、inventory_movements 4633、sales_deliveries 154、sales_delivery_items 1055。须使用 sales_* 表，旧 orders/deliveries 表不是当前销售业务数量依据。
- state.operation=`update`，manual_stop=false，无 backup_work_retained。历史 migration_target/report 字段仍引用旧事务，不能只看字段存在就判定活动迁移。
- 15:08 完整本地备份 `20261015-150848-b493fa40.tmbackup`，1,717,206,065 字节，SHA `093b8478db7ebe38b65407d22afa372f44e52fdf1f90025d6791e8104dad17e7`。原协议记录 restored_verified=true；接手重新读取密文并核对摘要相符，恢复临时目录已不在原位置。此次核对不是重新解密恢复演练。
- state 仍有自动备份任务告警。它与上条具体恢复点的有效证据分开报告，不推断为备份文件损坏，也不宣称定时备份已正常。
- 本轮系统管理员通道成功返回退出码 0，未走 sudo 免密试探，未读取密码。不能由当前成功推断今后永久免密。

直接证据：`/Library/TianmingERP-ReleaseDiagnosis-20261015/business-diagnosis.json`；普通用户可读回执 `/Users/kimiyf/ERP-Migration/evidence/formal-recovery-20261015/diagnosis-business-admin-result.json`。

## 2. 连续失败的实际原因

以下时间为 2026-10-15 中国时间。旧回执仅记录异常类型和栈帧的条目，原始错误文字已丢失；其具体条件由对应已部署代码定位，不伪造原始错误。

| 时间/候选 | 原始证据与具体故障 | 对发布的影响 |
| --- | --- | --- |
| 07:27 / v636 | failed.json: supervisor-transition / TimeoutExpired，subprocess.run 等待超时 | 业务更新混入监督程序切换；子进程执行窗口未与持久事务解耦 |
| 07:52 / v636 | 子命令 rc=1，AttributeError，mac_service_manage.record 第120行；驱动传入 Path，接口要求具有 path 属性的 Lease | 正式驱动接口未在相同身份和实际安装模块上预演 |
| 08:01 / v636 | 子命令 rc=1，local_backup_only.enabled 第60行；Manager 用旧 current 的 manifest 校验候选模块摘要 | 新旧签名主体混用；e9ca69b8、27f53bea 已修正传递候选身份 |
| 08:42 / v637 | 子命令 rc=1，backup_locked 第230行要求旧 receipt.retention.status==one_complete | 旧备份清理未完成阻止后续发布，不能等同新备份无效 |
| 09:30、09:37 / v638 | BrokenPipeError（phase 输出）及续跑 TypeError | 驱动/执行窗口故障，与迁移 SQL 无关；旧报告无原始 TypeError 文字，不猜测参数 |
| 09:45 / v638 | 子命令 rc=1，_reconcile_previous_retention_locked 第192行仍未满足 one_complete | 收尾与清理问题尚未证明解决就再次正式运行 |
| 10:30 / v639 | 子命令 rc=1，_finalize_locked 第200行要求业务 ready 且 manual_stop=false；该驱动却以 manual_stop=true、业务已停为前提进入更新 | 回退启动后的标记和回执前提不一致；收尾异常覆盖原演练异常，根因信息丢失 |
| 14点隔离验证 | eq1015sa SQLite batch 重建丢失两条 stock_preparation_commands 保护触发器 | 实际迁移缺陷；3f561c39 已修复，正式快照升/降/再升保持323张原表事实及原索引/触发器 |
| 15:11 报错、15:15旧版恢复 / 4478f3c6 | 原始 ValueError：`SMB流备份目标未通过核验；保留现场，不改用其他备份位置` | 本地完整备份已完成，演练入口却按遗留 SMB 元数据选路，候选模块路径不等于旧绑定目录，受保护校验拒绝；没有执行正式迁移 |

最近一条完整失败链：

`Manager._update_locked → local_backup_only.backup_locked（成功） → migration.rehearse → _rotation_rehearsal_owner → rotation_enabled → mac_smb_stream_target.prepare（候选路径≠旧绑定目录，拒绝） → start旧版 → 发布本地备份回执 → 退出失败`

真正的组织/验证缺口：此前按单个报错逐层打补丁，用大量单元测试替代“精确签名候选＋UID499＋真实已安装控制记录＋完整更新和回退路径”的端到端演练。可提前发现的错误在停服后才暴露。失败日志还丢失原始异常或被收尾异常覆盖，增加定位成本。

根因材料：`failure-history-admin-result.json`、`failure-history-raw-details.json`（上述 evidence 目录），以及 `/Library/TianmingERP-ReceiptAuto-v639-Final-20261015/release-transaction.stdout`。旧回执文件不修改。

## 3. 本次已落地的最小修复

1. migration.prepare_rehearsal_owner 首先验证受保护 local-only 策略；策略有效时不进入 SMB 分支。策略损坏必须拒绝，不自动换路；未选 local-only 时原 SMB 安全验证保留。
2. Manager 在备份及停服前执行演练路径检查；实际创建演练任务时再次核对，防止预检后变化。预检本身不登记任务、不连接 NAS、不读取凭据。
3. 将15:08新增且已核对的完整备份摘要加入本轮保留名单，避免下一次收尾删除当前已有恢复点。
4. 隔离复现先得到与正式现场相同的 SMB ValueError，再修复。相关34项回归通过；另1项真实签名/密文/独立恢复的合成更新全链通过。合成测试的进程生命周期和迁移子进程为替身，不能宣称真实 Mac 切换/回退已验收。

测试文件：test_migration_rotation_lifecycle.py、test_migration_before_stop.py、test_local_backup_only.py、test_migration.py、test_local_migration_update.py。日志：local-rehearsal-route-tests.log、local-migration-full-route-tests.log。工作区原先干净；未覆盖未知修改。

## 4. 唯一发布负责人接续要求（按顺序，不跳步）

### A. 保持旧版在线，固定修复候选

- 基线是4478f3c6；提交本次最小修复后固定完整 Git SHA。旧 signed SHA `1c200ef9…` 不包含本次修复，不能再次发布它。
- 复用已构建前端和未变化运行时，仅重新打包变更文件、更新清单、签名并校验全部成员。用实际 Mac publisher 公钥验证，不能混用历史 publisher 公钥。记录业务 version、源码 SHA、签名包 SHA，三者不可混用。
- 保留所有当前正式备份（包括新093b…）和旧程序；本轮不运行 NAS 备份、不重装监督程序、不扩展业务需求。

### B. 在停服前证明整条实际路径

- 安装不可变候选后，用受保护入口和实际 UID499 运行只读授权/运行时/路径预检；验证完整驱动可启动并持久回查，实际模块存在、调用参数匹配。不能先停服再调脚本。
- 用现有有所有权的最新正式快照及必要原件，在隔离环境执行**签名候选的实际 rehearsal 路径**，确认 local-only 不触发旧 SMB owner。数据库升级降级及触发器证据仅在迁移源码/起点仍匹配时复用；业务原件只验证过摘要的不能写成完整恢复成功。
- 在隔离数据库上验证候选启动和健康；关闭调度/外部副作用。正式旧版持续服务，禁止两个程序写正式 SQLite。
- 注入候选启动失败，验证与实际迁移后数据库兼容的旧包恢复、审计保留、监督后续能拉起；不得只验证 eligibility 函数。当前 manager 会写 update_failed，而旧 supervisor 对任何含 failed 的 operation 停止拉起，这条风险仍须在受控恢复路径中解决并验证；不能删除人工停服/活动迁移/不兼容保护。
- 只有完整路径通过，才固定一次发布事务。当前代码修复和35项测试不能代替这一门禁。

### C. 一次受控切换与失败收尾

- 管理员入场与脱离聊天的事务驱动在旧版运行时完成；驱动自行持锁、持久记录阶段、原始异常及独立恢复异常。禁止重用硬编码旧 state SHA 的旧 launcher；禁止因输出管道断开终止事务。
- 停服前再次核对候选摘要、当前业务/DB/状态、空间、备份保留名单、恢复执行身份和可回读结果。变更则重新验证受影响项，旧版继续运行。
- 只在最终切换停止写入，完成原协议最终一致恢复点、迁移与完整性/FK/旧事实/触发器/索引检查，再切程序启动；新拆分恢复格式未实证前不取消现有恢复保障。
- 新版启动失败且数据库/业务规则兼容时恢复兼容旧包，保留真实失败结果；状态不明或部分迁移保留现场专项恢复，不恢复旧数据库覆盖新数据，不强杀迁移。
- 健康及核心查询通过才记录技术发布成功，人工验收另列。清理/自动备份告警不得被冒充为业务发布失败或已修复。

## 5. 真实停服与验收边界

最近一次：stop_requested=15:08:33.888613；old_process_stopped=15:08:36.453500；old_start_requested=15:11:09.829569；实际进程启动约15:13:33；process_ready=15:15:00.020598。

- 进程停止至 ready：383.567秒（6分23.567秒）；停服请求至 ready：386.132秒。
- stop至恢复启动请求约153.376秒；恢复启动请求至ready约230.191秒，其中创建业务进程前约143秒、进程启动后约87秒。需对签名/运行环境重复验证及应用启动分段实测，不能把迁移1.4–1.6秒当成停服时长。
- 普通更新≤60秒仍是未达成目标；本轮接手诊断与源码修复未停正式服务，新增停服0秒。
- 当前尚未正式发布本次修复。正常发布与“新版启动失败后恢复旧版并继续监督”的真实环境验收尚未完成。

最多三步人工验收（正式发布完成后再做）：核对页面业务版本；查询一张已有订单与送货记录；按既有业务流程核对本轮收料/组套及货位显示，不在正式库创建测试单据。
