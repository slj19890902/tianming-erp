# Mac 既有正式激活的监护升级闭环（2026-10-14）

本任务从根协调者已提交的 `7bf4af6d` 建立独立分支 `codex/monitor-v3-20261014`。范围只有监护升级 root、worker、公开契约，`build.py` 增加一项能力，以及专项合成测试。没有操作正式实例、业务数据库、服务控制、管理员认证、备份、NAS、页面、发布或 Git push；没有重新签发或执行激活。

已按当前 AGENTS/CODEX_START、PLATFORM、执行章程、总任务卡和维护卡读取任务边界，并完整读取 MIGRATION_PLAN、MIGRATION_RUNBOOK、MIGRATION_CHECKLIST；Mac 迁移计划中既有正式模式/后续批准监护的约束另行核对。根明确要求不等待 NAS，本项回执先保存本地。

## 问题与最小修复

原监护升级只接受尚无 `factory-activation.json` 的家庭实例；root 文件门禁、worker 认证和新监护状态验收分别拒绝已激活实例。应用 current 更新不会替换系统 plist 所指的旧监护，SMB target 协议更新也不能代替监护更新。

原 `tianming.system-upgrade-plan.v1` 的字段、home-only 行为、IPC、锁和事务顺序全部保留。新增精确 `tianming.system-upgrade-preserve-activation-plan.v1`，只增加 `activation_sha256` 与 `owner_publication_sha256` 两个必须为小写 64 位 SHA 的字段。只接受已经完成的 owner-current-mac 激活；不扩展其他激活类型。新签名监护必须声明整数 `1` 的 `system_upgrade_preserve_activation_v1`，仍须具备原 `system_upgrade_worker_v1`、既有正式执行能力和完整签名文件树。

root 只在原 publication lock 内读取并持有 activation 与 owner publication 的受保护 no-follow FD，核对精确类型、当前 context、ready=true、request 摘要、activation 原字节摘要和无 owner FAILED。计划固定两份文件的原始字节 SHA；prepare、apply 初始核对及事务每个边界都保持同一文件身份/原字节。owner request 的摘要仍采用其既有 UTF-8 规范 JSON；升级计划仍采用原 ASCII JSON，activation 本身不重写。完整 owner 签名、本机目标、批准清单、原恢复锚和既有 mounted checkpoint 授权留给专用 UID worker 的原 `mac_factory_runtime.configuration`，root 不引入第三方依赖，`CODE_FILES` 闭合集合无需新增模块。

worker 在真实新旧签名树、实际 UID/组/脚本/解释器、精确 disabled control 和 plist 通过后，持有两份原授权 FD 调用完整 configuration，返回的 activation SHA 必须与计划一致，随后再次检查原字节。stop 与 verify 中每次 recheck 都复验；不调用 activation、server_environment 或启动准入，也不访问业务数据库或运营凭据。`ERP_HOME_REHEARSAL=1` 和原 worker reply 的 `home_rehearsal=true` 继续表示工作进程环境，不表示已激活 ERP 降为家庭配置。

原流程依次关闭启动门禁、停原 ERP、观察原监护 kernel exit 与 launchd label 消失、排除旧解释器残留、持两把原锁 CAS plist/control、借原只读锁验证 canary、启用并 bootstrap 新监护。factory 分支只接受 fresh 新 PID 的 `manually-stopped / configured_mode=factory / home_rehearsal=false`；原 home 分支继续拒绝 factory 状态。任何后续失败沿用 fail_closed，保留未决记录，不回滚或改写 activation/gate，不启动 ERP。

factory 成功结果明确 `factory_activated=true`（既有正式配置）、`activation_preserved=true`、`activation_performed=false`、两份原 SHA 和 `erp_started=false`；原 home 结果形状保持。state 只允许原 `manual_stop` 变为 true，业务 current 和所有原恢复历史保持。

## 验证记录

- 修改产品代码前，首版新增 49 项中 **24 失败、25 通过**。主要正例在旧实现的 plan exact fields 和 activation 文件拒绝处复现，后续事务案例因不能准备 factory 计划而失败。
- 原三份 contract/root/worker 测试文件未改。首次修复后，原套件加首版新增用例 **505 通过**。
- root 专项扩为 **68 通过**：旧 v1 拒绝 activation、新计划 exact fields/坏 SHA/未知类型/错 context/缺 gate/unready/错原 SHA/FAILED/链接/缺能力，stop、verify、bootstrap 三边界原字节与恢复状态/registry 漂移，旧进程身份/退出/残留/CAS 竞争，以及所有原失败阶段的 factory 分支。新监护 home、attention、错误 home flag、canary、旧时间、身份或旧 PID 均不能完成。
- 合成签名组合 **17 通过**：真正调用 `authenticated → metadata + published.resolve → _factory_configuration → mac_factory_runtime.configuration → stop/verify`。Ed25519 owner 签名、两份原签名包与完整树、原恢复和请求/网络/清单均实际核验；保留旧业务 current，签名新监护与 current 不同。系统权限/UID/launchd 和既有 mounted proof 是明确适配器。测试禁止 start、backup、restore、environment、NAS、launchctl、SQLite connect 和运营凭据解密；canary 单独为合成替身。新旧签名树篡改、坏 owner 签名、恢复锚变化、能力缺失/布尔/字符串/其他整数、配置 None/错 SHA、配置核验末尾 gate 变化均拒绝且 state 不动。
- 最终 11 模块组合 **818 通过、1 失败，236.25 秒**，其中全部监护 5 模块 **541 项通过、0 失败/错误**。覆盖上述五模块及 `test_mac_system_service`、`test_mac_factory_runtime`、`test_current_mac_runtime`、`test_owner_activation_runtime`、`test_build`、`test_current_mac_build`；证据为 `/Users/kimiyf/ERP-Migration/evidence/monitor-v3-tests.xml`。后来把错 context 反例加强为同时重算 activation/gate/plan 两个 SHA，确保不是只因陈旧摘要拒绝，该定向用例 **1 通过、67 未选**。这些重叠次数不累加。
- 唯一相邻失败是既有 `test_current_mac_runtime::test_native_history_hash_is_not_a_substitute_for_original_signature`：断言只接受 `InvalidSignature`，错误签名现在会进入既有 Mac publisher 信任回退，原测试未隔离固定安装路径，最终打开受保护 instance 时得到 `PermissionError`。没有访问业务数据库、解密凭据或控制服务；发现后未再直接重跑。该测试与 `mac_factory_runtime.py`、`mac_publisher_trust.py`、`release_request.py` 对基线 `7bf4af6d` 均无差异。用临时 pytest fixture 将全部 trust 路径改为合成目录后，安全重现为同一回退链的 `ValueError`，仍因异常类型断言失败；证据 `monitor-v3-trust-isolated.xml`。这是独立的旧测试隔离/断言缺口，已报协调者，本提交不修改该测试或放宽产品验签。
- 任务专属 basetemp 为 `/Users/kimiyf/ERP-Migration/evidence/monitor-v3-pytest`；单独签名组合使用 `monitor-v3-owner-pytest`，加强 context 与安全旧测试诊断分别使用 `monitor-v3-context-pytest`、`monitor-v3-trust-pytest`。既有恢复 fixture 内部使用独占 TemporaryDirectory 并自行清理，不触及其他执行者 pytest 目录。

只读复审由 recovery_v3_review 完成，产品 diff 与真实签名组合测试未见阻断；其独立复跑首次 505 项通过，随后扩展 68+17 共 85 项通过（23.56 秒），使用不同 basetemp，避免互相清理。新增反例及既有同类测试覆盖完整原约束；合成耗时不作为生产窗口承诺。

## 集成与实际执行边界

需要从合并后的固定源码重建完整 Mac 签名包，以及包含更新 root/contract 字节的独立 SHA 批准 stdlib root pack。原 `CODE_FILES` 列表保持，所有文件仍必须按新字节重新计算并核对；旧计划/旧包不能因本源码变更自动获得 factory 升级权限。此提交没有生成正式计划或签名发布物。

协调者后续应先在线完成候选批准/SMB probe 与 CAS，再生成最终 monitor plan；monitor 事务令 ERP 停止时业务 current 仍为计划中的原包。之后同 schema local code update 必须重新冻结停止后的 state SHA 再切 current，最后由新管理入口启动。不得在批准 monitor plan 后先切业务 current。

完整 monitor 事务会多次核验新旧签名执行树与当前包，停服窗口必须包含这一时间，不能只用 local update 的耗时描述整体。代码测试不替代真实 launchd 身份、系统钥匙串、注销/重启或 FileVault 解锁验收；本轮没有执行这些动作。没有 DDL、库存修复或其他 UI/warehouse 改动。
