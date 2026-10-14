# 本次监护升级中断的有限恢复（2026-10-14）

状态：隔离开发验证；未发布、未访问正式保护目录、未操作正式服务、认证、业务数据库或 NAS。根协调者明确授权补齐本次发布必需的 split 恢复缺口；不扩展通用部署或回滚框架。

独立 worktree：`/Users/kimiyf/ERP-Migration/monitor-recovery-v3-20261014`，分支 `codex/monitor-recovery-v3-20261014`，起点 `3ce4cf1b`。旧 dispatch 测试工作区的三份未提交业务文件保留。随后仅为构建依赖 cherry 根的 `192e4859`（本工作区对应 `84a35001`）；它不是本子任务新增补丁，根整合时不要重复带入。

已重读 AGENTS/CODEX_START、PLATFORM、当前总任务卡、维护/activation 任务卡及相关章程和总需求条目；完整迁移三文档在本连续监护任务前段已读。按根指令不等待 NAS、不重新读取正式现场，回执先保存在本地。

## 已复现的问题

原 `mac_system_upgrade.apply` 先写 NEW plist，再写 disabled NEW control。plist 的实际 `os.replace` 成功后，父目录 fsync 失败，或后续 control CAS 失败，都可能留下 NEW plist + disabled OLD control。原 `fail_closed` 关闭 gate/launchd 持久开关，不回写 plist；旧 `mac_system_service.make_plan/validate` 对两个不一致的包均拒绝，所以无法直接 enable。

首个红例运行原 prepare/apply，真实写出各阶段证据、停止 state、旧监护退出观察及 plist CAS，再注入 control CAS 故障。确认两个旧路由均拒绝后，新恢复模块不存在，测试失败。实现后该例及真实 plist 目录 fsync 故障例均完成恢复。没有手工拼装“成功”原失败记录。

## 精确实现范围

- 新 `mac_system_upgrade_recovery.py` / `_contract.py`，只接受已完成 owner-current-mac activation 的原 factory 升级。没有 CLI 或可传任意路径、包、业务根目录的参数。
- 新模块加入原 monitor `CODE_FILES` 闭合 root pack。`boot.FILES` 不变，普通 service/publish/manage 继续使用各自原 basic pack。根先用外部固定 SHA loader 认证整个 pack，再导入；运行时自己的 hash 复验不替代外部授权。
- 新签名能力 `system_upgrade_split_recovery_v1` 必须为 exact int 1，新签名包必须包含两个恢复模块；其 `git_sha` 必须等于闭合 root pack 的 `source`。恢复计划也固定该 source。原 upgrade plan 的 code manifest SHA 必须仍等于该 pack，因此本入口只适用于由本次新 pack 生成的计划，不追认旧 v629/home 计划或任意历史失败。
- 原 worker、原 upgrade contract、manager、local updater、stream 模块均未改。唯一原 upgrade 改动是 CODE_FILES 增加两项。继续借原 UID worker 的 verify 阶段：完整原 owner 配置认证、新旧签名树、原 current、精确 disabled NEW tuple、只读 inherited FD、两把原锁与 canary 都保留。
- 按根追加要求，build 的 reader cap 增加 `physical_kit_correction_20261014_v1: 1`，原 source completeness 同时要求 reader contract、实际 kit 服务、source valuation、entry guard 四文件；没有 DDL 或新 activation。reader/Manager 其他实现由根/Sol负责。

## 准入及事务

恢复 make_plan 只读原 `system-upgrade-<attempt>` 九份精确证据：plan/prepared/pending/old-control/old.plist/stop/old-exited/old-service-processes/failed。目录和文件使用原 no-follow FD、权限、ACL、inode、原始字节检查；不接受缺失、额外、已 verified 或被链接的旧记录。逐项复验原计划摘要、完整 stop 回执、原监护进程身份/退出观察、日志 inode/权限、原 failed 的确认位。恢复计划固定九份原 bytes 的各自 SHA。

每次准入、prepare 和执行边界继续固定原 activation/owner publication bytes、无 owner FAILED、context/publisher/registry、完整 state SHA。state 必须等于原计划 stopped SHA，current 仍为原 current；本模块无任何 state 写调用。

唯一准入 tuple 是 NEW plist + disabled OLD control。持有原 publication、operation、service-supervisor 锁，要求 launchd label 确认 absent；对专用 UID 的真实/有效 UID 进程集合稳定采样，并独立排除旧、新解释器残留。没有把 enabled=false 当作无进程证明。

执行只 CAS control 到 disabled NEW；原 plist 保持，原失败目录完全只读。调用原 signed verify worker，在返回并复验后释放 monitor 锁，CAS NEW enabled、enable/bootstrap、释放 operation 锁，验收 fresh 新监护身份、时间、factory/manual-stop 状态和 canary。成功仍 `erp_started=false`，之后由协调者决定继续本地代码更新或管理 start。

新恢复有独立 plan/prepared/pending/verify/verified/failed 记录。原失败证据不会删除、覆盖或改称成功。恢复发生异常时沿用精确 fail_closed；包括 rename 后错误、锁退出复验、receipt 写入或 verified 后最后复验失败。失败保留所有现场，`do_not_retry=true`；未知 control 不覆盖，持续 I/O 失败不能保证 gate/label 两确认位。verified 文件独立存在不能代表成功，必须正常返回且没有新 failed。

## 既有一致 tuple 路由

若失败后仍精确 OLD/OLD 或 NEW/NEW，新的 split 执行器拒绝，不扩展状态集合。协调者使用实际 tuple 对应包 K 的既有 `mac_system_service.make_plan('enable', K)` → 原始规范 bytes/SHA → `apply`。若 label 仍注册，必须先同 K 的新鲜 disable plan/apply，正常 stop/bootout 确认 absent，再新鲜 enable。

该路由条件包括原 current/完整停止事实、manual_stop=true、没有 ERP 进程、原签名/权限/worker(check)/canary 全部通过；原 activation 与失败记录保留。enable 的原返回值 `health_verified=false`，不能据此省略监护及 ERP 健康核验。随后可以用已批准新版管理包 `manage(new_package, 'start')` 启动 state 中的原 current；它不是切 current 的入口。旧 monitor 即便可运行原 ERP，也不能说已兼容新 SMB。

若 unknown tuple、activation/原恢复事实变化、原证据不全、进程身份不明、持续 I/O 或 canary 不通过，保留现场，由协调者处理；没有“一定自愈”的承诺。

## 调用顺序与停服边界

1. 在线完成最终干净源码构建、已有身份签名、最终包/manifest SHA、Mac 信任授权和 root publication。新 basic/monitor 两套 root pack 分别按自己的闭合集合生成，不混用。SMB 升级 probe/CAS 在在线阶段先完成。
2. 在线冻结原 current/state/activation/gate/registry 后，使用最终 monitor pack 的原 make_plan/prepare。此步骤不是 worker verify，不能排除之后的 rename/进程/I/O 风险。
3. 原 monitor apply 正常完成才继续 local_code_update；如明确原 split，收集原 failed 的固定九份字节，新恢复 make_plan(attempt_id) → 批次批准其规范 bytes SHA → prepare(raw, sha) → apply(recovery_id, sha)。只在新恢复正常完成且新监护健康后继续。不能重跑原 apply 或自动覆盖未知状态。
4. monitor 后重新固定停止状态 SHA，再走原专用 local_code_update entry，最后新版管理 start。只有一次必须的完整本地备份；不调用会重复 NAS 冷备的 Manager.rollback。local update state rename 后失败需先确认实际 current，不能假定未切换。
5. 保持本次合法自动备份 pause，直到维护结束；manual_stop 时 timer 跳过 NAS，但 start 后清 manual_stop，解除 pause 可能立即触发 overdue 全备份。root 已报告原 pause 有效，本子任务未读取正式文件。

在线 `stage_release` 可早发现坏包，但现有 update 强制 `verify_existing=True`，仍完整解包并验证；不能把此项从停服时间中扣掉。既有 87/90 秒 local update 合成记录目标是小 fixture，不代表最终完整包加 monitor/recovery 的真实窗口。此补丁的合成测试耗时同样不作为生产停服承诺。

## 验证

原监护 root/contract/worker/activation 四模块 **524 项通过**（3.69 秒），未改原测试文件。新增恢复事务先 1 项失败，首版 72 项通过，补充模块来源、已有一致 tuple 路由、真实 plist fsync 故障后为 **79 项**。新恢复签名组合 **6 项通过**（7.80 秒）。

最终组合为新增恢复事务/签名组合、原 owner worker、原 system service、新 build 门禁、原 build/current-Mac-build/SMB-build 八模块，**230 项全部通过，28.86 秒**；XML 为 `/Users/kimiyf/ERP-Migration/evidence/monitor-recovery-v3-integrated.xml`。前述专项次数包含在最终组合中，不累加宣称。另有原四监护模块 524 项与此最终组合不重叠。

basetemp 分别为 evidence 下 `monitor-recovery-v3-pytest`、`monitor-recovery-v3-original-pytest`、`monitor-recovery-v3-signed-pytest`、`monitor-recovery-v3-final-pytest` 和 `monitor-recovery-v3-integrated-pytest`，未使用默认共享目录。提交前 `git diff --check` 通过。

签名组合使用真实 Ed25519 owner 签名、新旧包与完整 published.resolve、runtime.configuration、原 stop/verify worker；OS UID/launchd/进程观察、canary 实机接口和既有 mounted proof 是明确适配器，并禁止 start、backup、restore、业务 SQLite、NAS、运营凭据和真实 launchctl 路径。没有正式操作或新增服务。
