# NAS_RETENTION_TWO_20261014

状态：限定候选开发；未部署、未执行 NAS 删除。

## 当前交付结论（覆盖以下早期阶段记录）

当前已实现独立 `nas_backup_rotation.py` 的具体 Mac SMB stream 认证读取/紧凑恢复证明/按需活动采集/持久增量2+1索引，以及 `nas_backup_rotation_stream.py` 的仅固定目录单退役包同FileId删除和启动后既有密文传输函数。前一阶段“未实现 adapter/索引”的描述仅记录当时状态，不能用于判断最新候选。**当前仍未接入正式 manager/worker，自动两份尚未生效**；不支持未知 legacy 合同或任意 NAS/Windows，不展开新历史审批协议。

只读证据来自统一负责人已经 OS 认证采集的 `evidence/erp-plan-v3-20261014/map-preflight-20261014-034856/read-report.json`，本代理仅读该导出，未访问受保护目录。实际是 `BoxERP/mac-server-backups` SMB stream，现有库的 `mac_smb_stream_contract.validate` 与 `mac_smb_stream_receipt.validate` 对导出结构验证通过；这不把导出本身变成凭据。仅有一份可识别 receipt，SHA `b38146ec456ff0f462ff316c7649bc6643876d429ba63bac9444e6f392a246e9`，对应密文 `b18fb6fc02fa44f3f2ad51b4c124528f5169a8d86f3014158ae94e756ab3fc12`、1,715,972,385字节。没有第二份认证回执或两份完整独立恢复证明；缺旧 archive-contract 不走挂载缓存兜底。bootstrap不足继续保留原包，不在营业时段追加冷备。

## 最小正式接入顺序（manager文件由统一负责人独占）

1. **生成前门禁**：原有 `manager.lock` 内、ERP仍运行且任何新完整包尚未创建前，调用 `pre_backup_gate(MacStreamReceiptAdapter(manager))`。retired、staged、未接管的last_backup_stream、未登记活动均阻断新备份但不停止ERP。首次确需建立第二业务点时，必须先有真实首包恢复证明，显式传 `bootstrap_pointer`；门禁把首包和唯一任务槽CAS写进bootstrap字段后才一次允许，重入只恢复该固定任务，不会把随后B/C逐次解释成“第二份”。已有同schema升级只沿统一owner已批准的**一次**本地冷备，不为本模块另造第二次停服/备份。
2. **原worker生命周期**：在已有短锁内调用 `adapter.record_job_locked(job)`，或锁外调用 `record_job(job)`；生成/上传/恢复/演练的原worker只登记自己的实际job及 `active → completed/failed_closed`，不得替其他任务猜闭合。字段为 `job_id,kind,status,backup_path,backup_sha256,receipt`，名字只允许当前代码产生的裸UUID、smb-stream-/local-code-update-/mounted-checkpoint-/import-＋UUID。已有未登记staging必须凭原任务所有权/进程/恢复引用实际核对后接管；不能人工塞空表。collector实际受控FD读取登记和staging一级名字，未登记、未知、活跃upload或state变化均拒绝。该生命周期hook尚未安装，因此登记缺口现在明确阻断。
3. **ERP启动后复用既有密文**：短停服本地完整密文＋独立还原 → 切换newcurrent → 确认ERP恢复运行后，批准的后台stream worker调用 `transfer_existing_ciphertext(upload_archive,readback_archive,source_fd,download_fd,filename,size,cipher_sha,attempt_id)`。直接复用已有Archive.upload/download，两次批准连接独立，FD原件不由函数关闭；不读运行库、不重新pack，不调用stopped包装。随后 `authenticate_existing_readback(download_path,raw_zip,password,local_proof)` 绑定原cipher/raw/manifest SHA、DBsummary与业务source_time；本地proof必须来自owner真实受保护的短停服恢复报告，不是用户JSON成功标志。owner沿现有签名worker的receipt发布/CAS状态段保存真实新receipt及last_backup_stream，不调用 `archive_and_publish_stopped/build_and_verify_stopped` 重造包。
4. **真实独立恢复报告**：同一回下载交现有隔离 `Manager.restore(expected_backup_sha256=...)`；仅用本任务隔离副本，不启动生产。之后调用 `capture_verified_recovery(restored,receipt,receipt_sha,download_path,raw_zip)`，它逐ZIP entry核原文件哈希、从恢复root核不可变shared/地图、DB实际完整性/外键/计数、实际恢复的签名packages与凭据结果；不假设payload/shared仍存在。`adapter.publish_recovery(pointer,report)` 写固定owner/权限受控记录，同报告一致重放，不同拒绝。报告仅保存清单digest/count、地图摘要、DB/签名/凭据元数据及真实SHA/时点，<64KiB，不放宽已有1MiB reader。
5. **索引轮换**：新receipt先 `stage_candidate(adapter,pointer)`，真实验证通过后 `advance(adapter,pointer)`；第一次用两份真实证据 `advance(adapter,bootstrap=(first,second))`，必须包含持久固定首包。失败原两份不变，staged现场不被抹掉；CAS成功把唯一过期包加入retired。retired未清或staged失败必须先恢复该工作，生成前门禁拒绝第四份。失败临时载荷的回收仍须原worker的真实closure/精确清单，不在证据缺失时擅自回收。
6. **单包清理**：批准的原stream worker持已认证Archive调用 `execute_next(adapter,archive)`。保留两包句柄禁止write/delete，退役包用share=0同一FileId读取、对receipt目录/文件index及SHA/size核对，再短manager锁内重新按需采集当前/活动/演练引用、比较index CAS并SET_INFO/CLOSE确认远端缺失。最多一个过期包，不扫NAS、不删目录/资料/程序外部依赖；完整包已包含所需依赖，未提供独立外部依赖清理资格。网络读取在锁外、ERP不停服。删除或CAS失败队列保留；若崩溃发生在远端已删之后，下轮认证同目录缺失才幂等ack。collector最终锁内重新读取真实登记并生成当前时间证据，不复用hash前60秒旧报告。

现有validator绑定**当前**control/package，升级后不同批准链的旧receipt会明确拒绝，**不会被判成不重要后删除**。统一owner须在改批准记录前保存可核验的历史批准链并交受控核验，或由新批准worker对原密文重新独立认证并关联保留原证明；本候选不自行发明历史审批协议、换合同、覆盖旧receipt或重打运行库。该情况须报警并保留队列/原包，不能静默积累或继续产生未索引备份。

当前专用增量路径只读取索引两份＋新的一份（或两份＋唯一退役包），实际远端验证约3个包字节，无历史1.4TB全量检查。大范围离线历史清理仍只使用下面早期的离线apply候选，不挂普通备份。

最终定向验证：既有维护解释器 `/Users/kimiyf/ERP-Migration/maintenance-venv/bin/python`（含已安装smbprotocol/cryptography，无安装/联网），以下命令 **63 passed，2.42秒**；Astra另外独立复核原17项、stream16项及bootstrap窄复核3项通过。`git diff --check`通过。真实恢复3项由独立helper的单测试文件整合，验证共享目录rename后的报告成功、附件篡改拒绝、2048个极小文件使原manifest>1MiB而报告<64KiB。其他测试涵盖真实SMB wire结构同FileId删除/缺失确认、引用/身份/清单变化阻断、删除后CAS失败幂等回收队列、真实owner/FD读取与既有validator、发布报告重放、按需collector与未知job拒绝、既有cipher FD上传/独立回读认证、首次bootstrap首包/槽CAS及A→B→C重入拒绝。

```text
/Users/kimiyf/ERP-Migration/maintenance-venv/bin/python -m pytest --import-mode=importlib --confcutdir=tests/desktop_assistant tests/desktop_assistant/test_nas_full_backup_retention.py tests/desktop_assistant/test_nas_backup_rotation.py tests/desktop_assistant/test_nas_backup_rotation_stream.py tests/desktop_assistant/test_nas_rotation_real_restore.py -q
```

本候选没有正式接入/发布，也没有NAS实际删除、认证、服务/网络控制或正式数据库读写。生产记录读取/发布及远端函数只在合成fixture或读取已经授权导出的结构上验证，未在真实批准会话调用。注册、短锁、真实报告、receipt发布和原签名worker的调用边界仍须统一owner按上面六处hook串行接入；不能把新schema文件出现当保留已启用。

## 范围和不变量

老板要求 NAS 全局只保留最新独立核验通过及上一独立核验通过的两份完整可恢复版本，连同必要的恢复说明、签名、程序与密钥恢复依赖。验证中的第三份暂存；失败不轮换。不是每个目录留两份，也不复用仅处理根目录 sqlite3 的旧五份保留器。

工作区 `/Users/kimiyf/ERP-Migration/nas-retention-two-20261014`，分支 `codex/nas-retention-two-20261014`；主任务已核实正式业务源 `61e5fafb`。远端记录旧、fetch 超时，不声称远端同步。仅写独立 `desktop_assistant/nas_full_backup_retention.py`、专用脚本、测试及本任务文档；manager、监护、激活、升级函数由主任务协调。

正式运行库保持本地。禁止正式数据库访问、服务控制、认证、发布、push、新大副本及本轮 NAS 删除。正式资料、图纸、知识库、密钥和 current/previous/schema_authority/migration_target 及恢复来源程序依赖必须保护。路径越界、链接、未知文件、文件变化、活动上传及活动引用均拒绝。

## 证据及现有契约

来源 `/Users/kimiyf/ERP-Migration/evidence/material-map-audit-20261014` 的 `NAS容量与两份备份清理方案.txt`、`nas-sizes.json`、`nas-top-level.json`。733.74 GB 是 BoxERP 子目录逻辑字节，非物理空间，不包括快照/回收站和根独立业务文件。`nas-sizes.json.large` 只有 1073 个大文件的 path/bytes/mtime 子集，不能当完整逐文件执行清单。不得无谓重扫 59280 文件。

已读 AGENTS、CODEX_START、NAS AI_START、PLATFORM、总需求相关章节与执行章程 1–9 必要条款。知识库回执本轮受写入 allowlist 限制，先记本地，主任务串行回写。

`manager._backup_inputs` 核对数据库结构/完整性、附件、签名 release、结构权威及恢复来源；`_build_backup_payload` 生成 `tianming.recovery.v1`，含 shared、release/compatibility/原始程序包、credentials.tmencrypted、来源与业务激活契约，随后加密/解密校验。`_backup_at_location` 在 NAS `tianming-backup-work/<UUID>` 暂存，pending→final，Mac 经 mac_backup_storage.after_write 独立直连回读再发布本地 control/backup-receipts，最后写 last_backup。Windows 本地回读不足以证明极空间队列落盘。现有 receipt 不是完整独立恢复演练证明。`_backup_locked` 当前直到上传结束才恢复服务；本模块不更改该公共流程，不把保留与长停服绑定。

当前挂载为极空间 cache NFS；172.16.3.96:445 依赖蒲公英 utun6，LAN 候选 192.168.3.96 不通。挂载缓存回读不能替代独立远端证明，不停蒲公英、不改网络。

## 成功标准

独立纯规划器、无默认删除 CLI、可信串行执行适配边界；合成小文件测试验证两份全局保留及 fail-closed 行为。既有元数据生成缺验证项与逐文件审查预览，所有对象保持 blocked，允许删除量为 0。独立远端/恢复核验及生产接入由主任务与现有发布聊天统一串行办理。

## 接入位置（建议，未修改）

1. 现有备份 worker 完成完整恢复验证及独立远端验证后，将证据交给保留 authority adapter；不要仅凭 last_backup_at/verified 布尔值调用。
2. 适配器复用现有维护/上传锁，核实固定 NAS 合同、真实存储身份、全部活动备份/恢复/演练引用、程序状态角色、上传队列，持有排他文件写入租约。
3. 用完整逐文件 inventory 生成规划；在业务已恢复后的后台阶段串行执行。现有 mac_smb_stream_work 不负责保留，保持其边界。
4. 生产 adapter 尚未建立前，专用脚本仅生成证据预览，不提供 apply。当前 backup/monitor/upgrade 接入补丁须主任务统一安排；不得扩大成第二套备份生成或迁移框架。

## 可接入的准确契约与凭据来源

- `desktop_assistant/manager.py:664` 的 `publish_receipt` 完成后只有远端存储证明，不在此处直接轮换。`Manager._backup_locked` 的 finally/start 恢复业务后，由统一负责人接入后台串行验证/保留阶段；不能把 apply 加入 `_build_backup_payload` 或停服上传区段。
- 原 Mac 路径：`mac_backup_storage.publish_receipt` 在固定 `/Library/TianmingERP/instance/control/backup-receipts/<stamp>.json` 及 `<stamp>.mac-storage.json` 发布 owner/权限受控的本地 receipt 与 readback sidecar；`after_write/readback/_validate_proof` 负责固定合同和独立专用进程远端字节核对。`verified_at` 明确只查历史本地记录，不查询当前远端可用性，不足以作为当前 apply 的 gate。
- 新 SMB stream 路径：`mac_smb_stream_target.archive_stopped:117` 调用 `mac_smb_stream_receipt.archive_and_publish_stopped`；receipt 的 contract/context/package、upload/download、SHA/bytes/raw_sha、file/directory identity 已有验证；`mac_smb_stream_backup.build_and_verify_stopped` 会验证独立连接的回下载、解密后的 raw_sha。复用这些认证入口，不重新相信拷贝来的 JSON 字段。此证明仍不能替代完整独立恢复演练。
- 恢复依据须另用 `Manager.restore(expected_backup_sha256=...)` 在指定隔离任务根执行后，保存外置报告，绑定备份 SHA、认证清单业务 created 时点、DB结构/完整性/外键、全部附件与地图、签名 release/compatibility/来源程序、credential restore，证明全部恢复覆盖及依赖。已有 `scripts/uat/verify_managed_recovery.py` 只生成合成 Windows 数据，不认证这里的历史 NAS 包，不能冒用其 `recovery-runtime-evidence.json` 为两候选的证明。`mac_service_restore.run` 是固定服务恢复入口，本轮不调用，也不借它越界做生产恢复。
- 生产 authority adapter 必须实时读取已批准实例的 state current/previous/schema_authority/migration_target、restore_provenance.STATE_KEYS、last_backup/last_backup_stream、migration_backup；核对 control/process.json、上传 worker/队列与所有演练登记，扩展到签名兼容回退依赖，并认证固定备份合同和资料/图纸/知识库/密钥路径。文件目录本身存在不证明没有引用。模块采用全部角色显式声明，空角色由适配器证明不存在；旧完整包仍被引用时整个轮换 blocked。
- `Authority.freeze()` 必须持现有维护锁及排除所有 NAS 写入者的租约、认证完整逐文件清单与以上证明；`recheck()` 每次删除前后重新核对合同/挂载/上传/引用。当前没有实现或注册生产 Authority。模块仅提供最终规划/逐文件执行函数，缺此适配器不得 apply；POSIX dir-FD 安全能力不足时拒绝，无 Windows 不安全降级。

**执行器仅限离线历史清理**：apply_plan 先逐文件全字节预检，再在每次 unlink 前重新全字节读取。若691.97GB大文件子集全部经过独立审核获准，需约1.384TB读取，加上独立保留包验证和完整清单补齐成本；不能放在普通备份成功的同步/停服路径，也不能当后台每次全NAS扫描。排他租约阻止备份/归档写入，ERP业务运行库仍本地，不应停业务。实际执行须统一负责人另行安排限量历史清单、持续租约和合适维护时间。

普通自动轮换的最小接入建议：复用既有 `control/backup-receipts` 或 `last_backup_stream` 的认证凭据，维护一份绑定固定合同的增量索引，只追踪两份认证恢复版本＋新增的一份暂存及依赖。新备份上传和独立验证在业务恢复后完成；成功后持短索引锁重新核对当前/回退/结构权威引用，用CAS固定最新与上一业务点。旧两份在验证失败时原样保留；成功轮换的清理工作单只含唯一失效旧包及已失去引用的依赖，按该索引定向验证/回收，避免全量NAS枚举和长期持全写入锁。首次历史治理使用这里的离线规划/受控执行候选；本轮未实现/接入增量索引或定时轮换，不增加第二套生成/恢复框架。

## 预览、验证与候选交付

预览文件 `docs/tasks/NAS_RETENTION_TWO_20261014_PREVIEW.json` 可由以下命令重建，仅访问原先保存的本地证据：

```text
python3 scripts/admin/nas_retention_evidence_preview.py --evidence-dir /Users/kimiyf/ERP-Migration/evidence/material-map-audit-20261014 --output docs/tasks/NAS_RETENTION_TWO_20261014_PREVIEW.json
python3 -m unittest discover -s tests/desktop_assistant -p test_nas_full_backup_retention.py -v
```

24 个定向合成测试通过（Python 3，0.057 秒），不载入 ERP 数据库/服务或根 tests/conftest。覆盖两份全局排序、同包多处不算两版本、独立恢复及依赖缺失、活动上传/旧包引用、未知或不完整清单、密钥/图纸/依赖保护、越界/链接/硬链接、修改/替换、计划篡改、全部预检后才删除、部分失败停止并精确回报。首次运行发现 macOS /var 临时路径别名会被无链接路径门禁拒绝，测试改用已解析的真实 /private/var 合成根，未放宽生产路径门禁。

原统计范围 733,743,459,411 字节；1073 个大文件子集 691,968,541,511 字节，清单逐项记录相对路径、bytes、mtime、证据类别、retain_blocked 和理由。可删除字节严格为 **0**。子集中 295 个加密备份名称候选 307,862,444,861 字节，60 个备份工作载荷候选 41,735,571,042 字节，8 个未完成载荷候选 20,725,012,464 字节，352 个程序分发候选 175,824,783,832 字节；其余为未分类归档与未知载荷，全部缺分类/活动引用/认证证明。各数是互斥元数据子集，不能当整体可释放量或物理空间验收。backups 根 286 个 tmbackup 的293,432,888,407字节和247个 pending 的6,032,431,636字节另有原始直接文件聚合证据，与子集不能相加。

名称候选组：`mac-server-backups/20261014-070840-4086632c.tmbackup`（1,715,972,385 字节）及 `backups/mac-mini/mounted-{315db29fe6724c939b02a8ae66781448,79d0e560e6b044bda9a69119abe1a576}/20261013-192734-8a47a6b9.tmbackup`（每处1,715,765,217字节）。后者同名同大小仍不证明同 SHA；两组均未被认可为应保留版本。NAS 最新存在性由主任务另外核查；本代理没有读取这些大包、认证或实际恢复。

本轮只是可审查的最小规划器/受控执行候选，**自动保留尚未接入、生产问题尚未宣称修复**。没有写正式数据、改服务/网络、push、发布或删除 NAS。独立 Astra 复审、真实远端/完整恢复证明、生产 adapter、串行实际清理和 NAS 物理空间验收仍由主任务办理。NAS 回执待主任务统一回写。真实 token 用量不可获取。

## Astra 复审 P1 修正

复审发现引用保护使用大小写敏感比较，而清单去重使用 casefold，在大小写不敏感的 Mac/SMB 上可能把 current 的大小写别名误列为删除。已将保护路径、目录祖先及清单重复检查统一使用保守的 Unicode NFC＋casefold 比较键；真实文件身份、SHA 和执行路径仍保持原始精确值，不把别名改写后交给 unlink。保守比较可能多保留文件，不开放当前/回退/知识库保护窗口。

新增 current/previous 包、knowledge 目录大小写别名，NFC/NFD 组合字符知识库别名及清单重复别名测试；活动旧备份引用测试也改为大小写别名。修正前29个测试中6个明确失败，复现误删及漏阻断；修正后29个定向合成测试全部通过。本修正仍仅候选模块/测试/任务文档，没有生产接入或NAS实际删除。
