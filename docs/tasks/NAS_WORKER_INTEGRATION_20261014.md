# Mac SMB 两份远端、一份本地备份接线回执（2026-10-14）

仅源码与合成验证；本代理未访问正式实例、NAS、凭据、认证或服务。已有 stream archive 能力保持旧含义；新目标必须声明精确整数 `nas_backup_rotation_v1=1`，并由签名 manifest 包含以下四个文件的 SHA256：

- `desktop_assistant/nas_backup_rotation_worker.py`
- `desktop_assistant/nas_backup_rotation.py`
- `desktop_assistant/nas_backup_rotation_stream.py`
- `desktop_assistant/nas_full_backup_retention.py`

旧 `archive_smb_stream_v2` 与原 FILES 集合不扩大。升级计划按旧能力验证旧包；目标声明 rotation 时另外核对新能力与四份源码。根维护 build，对应 capability/source 检查不在本提交内。实际绑定仍固定 Mac instance、既有批准 package/runtime、UID、受控 FD、原 Keychain/SMB worker，不新增 CLI、凭据入口、任意路径或 NAS 枚举。

## 管理员单批次的实际入口

以下是固定批准 worker 的内部 Python API，`manager` 为已有固定 `/Library/TianmingERP/instance` Manager，`password` 来自已有受控凭据流程；不接受 caller success JSON。根负责审批、root-pinned 外层、原 monitor paused 记录及真正维护窗口。

1. **升级绑定前且 ERP 在线**：`nas_backup_rotation_worker.capture_first_authority(manager, password)`。新候选本身先经已批准 publication 校验，但现有 SMB binding 可以仍是旧 archive 包。读取旧 authority/receipt 原 bytes，验证现有本地密文 UID/FD/hash，真实解密校验 raw SHA，读取实际 manifest SHA/source_time/database，并实际 Manager.restore → capture_verified_recovery。保护目录保存 `retention-bootstrap-authority.json`，包括旧完整 control/receipt bytes、物理 cipher/source point 与真实恢复证明。旧导出 JSON 不能作为执行授权；不要求不存在的旧 local_code_update proof。
2. **原绑定升级后且 ERP 在线**：`nas_backup_rotation_worker.reauthenticate_first(manager, password)`，或 target 包装 `reauthenticate_bootstrap_first(manager,password,path)`（path 必须既有固定 destination）。对同一个 NAS 物理 filename 新连接下载、真实认证回读/恢复，**不上传第二份同 SHA**。新 receipt 名由完整当前 control SHA 与原物理 receipt 名唯一导出：`reauth-<64hexcontrolSHA>-<originalname>`；旧 immutable receipt 保留。恢复报告名绑定完整 cipherSHA 与 receiptSHA，旧名只接受原 receiptSHA 一致。
3. **仍在线，冷备前**：`nas_backup_rotation_worker.reserve_bootstrap_second(manager)`，或 target 包装 `reserve_local_update_slot(manager,path)`。短锁下核对真实第一份恢复及完整活动引用登记，预约唯一 task_id；重复预约复用同一 slot。
4. **唯一 stopped 窗口**：既有 `local_code_update.update(manager,package,password,state_sha256,approval_check=...)` 由原 caller 持维护锁。它只消费已预约 slot，不在停服后调用要求 running 的 gate。真实冷备 proof 新增 `authenticated_recovery={cipher_sha256,raw_sha256,manifest_sha256,database,source_time}`、backup_filename/job_id/rotation_slot；一份完整 cold cipher，恢复后才进入网络阶段。根的四项业务更正在该 stopped 窗口另执行，本 worker 不调用业务更正。
5. **ERP ready 后**：`nas_backup_rotation_worker.resume_local_update(manager,password)`，或既有 Manager.backup/服务 backup 路由自动恢复待办。复用同一 cold cipher，独立上传/下载 → 真正 authenticate_existing_readback → Manager.restore/capture → stage/advance。重复同任务不 pack、不增量、不新建第四份。普通 Manager.backup、nightly 与原签名服务 backup 已接同流程：短锁 admission/capture 与实际 owner job 生命周期；网络、完整回读/恢复与远端退役在锁外。
6. `runtime.retire_next()` 使用原签名 inherited channel / credential / Archive worker，仅新增固定 action=retire；请求无 filename/path/index/fileFD，目标从保护索引精确确定。每次最多一份明确 expired 远端版本。网络失败保留 queued work，下次先完成同项，不生成新备份。

## 已验证但清理被 UID499 守卫拒绝

原 `remove_owned_tree` 全机进程检查可能在专用 UID 遇到 AccessDenied。本代码未调宽该守卫。此时真实恢复 proof/report 与 protected task 先保留，`recovery_cleanup.status=retained`，同任务在 verified 阶段报 `isolated recovery cleanup guard refused`，ERP 保持 ready；下一次只重试该任务，不增加恢复副本或完整 cipher。

只读 `nas_backup_rotation_worker.retained_recovery_targets(manager)` 返回已成功验证的精确 target/owner/job_id、pointer、实际 proof、完整恢复证明或 reportSHA、exists。目标只可能是 `/Library/TianmingERP/instance/control/rotation-recovery/smb-stream-<taskid>`。根可在同一批准批次对 **返回且 exists 的该目标** 用原 root `remove_owned_tree(Path(target),Path(owner))`。它不修改保护记录，不手填 success。随后：

- 旧 binding 首包重复 `capture_first_authority`；
- 新 binding 第一份重复 `reauthenticate_first`（已发布 alias 也恢复同一 protected task）；
- cold 第二份重复 `resume_local_update`；ordinary/nightly 走原 backup 同任务路由。

目标已真实缺席时继续原 proof，不重复 restore/upload/pack；原 immutable `retained` 审计事实保留。合成故障测试覆盖两阶段首包 root 清理后重放与 ordinary 清理拒绝后恢复。首包未闭合时 active 登记阻断 slot，不能声称两份已完成。

## 一份本地完整备份

只有真实恢复且 advance 成功的 pair 才执行 `_local_retention`。候选仅来自原保护 index/bootstrap/task 的 verified predecessor receipt，不扫描 NAS，也不自动采信本地未知历史。保护 task 先持久化 predecessor 清单并进入 indexed 阶段，完整 hash IO 在短锁外；固定 backups 目录 UID/GID/mode、当前 survivor 的实际 FD/fingerprint/hash、旧 cipher 的实际 FD/fingerprint/hash、实时 active references、state/index CAS 与目录名字在短锁中再核，逐个 unlink/fsync/nlink=0。当前最新完整 cipher 保留；旧 receipt、recovery、原 local verified.json、AUTH、task 等小审计证明保留。

`restored_backup_sha256` / `migration_backup_sha256` 和真实 active restore/rehearsal/path/receipt/hash 引用一律阻断。`update_backup` 仅在原 local slot protected completed task、同源真实 recovery proof、原 staging verified.json 与当前 local_code_update 一一相等、ERP ready 且无未决维护时作为完成审计引用处理；不修改该历史字段、不删除小 proof。未知 backups/cache、硬链接、来源不一致、变更 master state/index 均保留报警；任务停在 indexed，下次只能重试该既有任务，不能每晚新生成。

因此当前受控链长期保持一份本地完整 cipher。未入受控 index 且缺恢复证明的历史本地文件不会被自动删除；根须以真正历史关闭/引用证据做单独有限收口，不能以空登记证明本地只有一份。

## 历史 owner 登记与新 typed reader

旧 staging task 允许格式 `completion-<32hex>` 与 `current-mac-audit-<32hex>`，仅允许登记名称，不声明已完成。根须独立保存原 root 进程/维护锁/完整 state/业务文件引用核验后才调用原 record_job failed_closed。未知未登记 staging 仍拒绝，创建空 registry 不能绕过。没有用原报告 verified 冒充新的 NAS backup 成功。

Manager compatible/start/_backup_inputs/restore payload promote 前接根的 physical_kit_reader_contract。检查实际 package SHA 与签名 manifest，缓存伪 capability 不会授权；新事实无对应 exact reader 时拒绝旧程序读取，未新增 DDL 或 activation。

## 验证

使用 maintenance-venv、合成凭据与小密文/真实签名包、内存 SMB wire peer、真实 Manager.restore/完整回读；未读取正式数据。正常集成主机进程清单明确为合成无引用场景；AccessDenied 专项为实际守卫拒绝并保留现场，不声称专用 UID 已获全机进程访问权。pytest 每 run 唯一 basetemp，位于 evidence/erp-plan-v3-20261014/nas-worker-pytest。

首次完整相邻测试 377 passed（final-v5）。后续新增本地 1 份、七类破坏/引用拒绝、清理失败同任务重放、旧 authority 与新 alias 各经 root 精确清理：专项 37 passed（local-retention-v4）；最终完整相邻 386 passed in 49.37s（final-v6.log）。git diff --check 通过。
