# P0-A 启动、发布、迁移与 UAT 隔离操作手册

## 1. 本轮安全边界

- 普通启动只做配置、数据库可读性、`integrity_check`、外键和 Alembic `current == code head` 检查。
- 普通启动绝不执行 `alembic upgrade`；revision 不一致时保持停机并给出明确错误。
- 旧 `scripts/admin/update_erp.ps1` 已 fail-closed 停用，不再自动拉代码、迁移或重启。
- 正式迁移只允许走 `scripts/admin/release_erp.ps1` 的 Prepare / Apply 两阶段流程。
- 家庭 UAT 只允许独立 SQLite 副本、`18000-19999` 独立端口、loopback 和 `ERP_ENVIRONMENT=test`。
- 本轮没有新增 Alembic revision，也不包含 N081。

## 2. 正式传输模式

正式环境必须显式选择或接受默认模式：

- `https_proxy`：默认且推荐。后端只监听 loopback，外部通过受信 loopback 反向代理使用 HTTPS；Cookie 使用 `Secure`，启用 HTTPS 跳转和 HSTS。
- `lan_http`：仅供已核实的工厂受控私网直连。允许 `0.0.0.0` 或私网 IP 监听，但来源、服务 URL 和端口必须是显式私网值，禁止配置受信代理；仍启用可信 Host、精确 CORS、会话 Cookie 的 `HttpOnly + SameSite=Lax`、带 Cookie 写请求的 Origin/CSRF 门禁及其他安全响应头。

`lan_http` 的通信和 Cookie 不加密，只能用于没有路由器端口映射、没有公网暴露、客户端受控且 Windows 防火墙把 TCP 8000 限定在工厂私网的场景。向日葵只用于控制工厂电脑，不应把 ERP 端口暴露给互联网；以后需要跨网络直接访问 ERP 时，必须切换 VPN/HTTPS 方案。

2026-07-23 工厂只读核验确认：主机 `PC-20250926DZYH`，私网地址 `192.168.3.80/24`，ERP 当前监听 `0.0.0.0:8000`，没有 80/443 代理证据。因此本次正式候选采用显式 `lan_http`，不再把“尚无 HTTPS 代理”本身视为阻断；发布前仍必须只读核对防火墙入站范围，并按本手册完成备份、演练、授权和 UAT。

## 3. 工厂正式配置与普通启动

工厂当前网络对应的 `.env` 非敏感配置为：

```ini
ERP_ENVIRONMENT=production
ERP_PRODUCTION_TRANSPORT=lan_http
ERP_DATABASE_PATH=data/carton_erp.sqlite3
ERP_BIND_HOST=0.0.0.0
ERP_PORT=8000
ERP_WORKERS=1
ERP_ALLOWED_ORIGINS=http://192.168.3.80:8000
ERP_TRUSTED_HOSTS=192.168.3.80,PC-20250926DZYH,127.0.0.1,localhost
ERP_TRUSTED_PROXY_IPS=
ERP_HEALTH_URL=http://192.168.3.80:8000/api/health
ERP_BROWSER_URL=http://192.168.3.80:8000/
ERP_SECRET_KEY_FILE=data/session_secret.key
```

会话密钥文件必须在切换前已存在且长度至少 32；不得把内容打印到终端或提交 Git。若工厂 IP、网段或端口变化，必须同步更新 Origin、Host、URL 和防火墙范围，不得临时改为通配符。

防火墙门禁：发布前先只读列出所有命中 Python/8000 的入站规则；正式规则只允许 Private 配置文件和 `192.168.3.0/24` 访问 TCP 8000。发现公网、Any 或更宽网段规则时先停止发布，单独确认如何收窄，不能仅新增一条窄规则后保留旧的宽规则。

正式配置完成后仍使用原入口：

```powershell
.\scripts\windows\start_erp.ps1
```

启动日志为 `logs\erp_startup.log`。看到 revision 不匹配时，不得手工改 `alembic_version`，也不得临时恢复自动 `upgrade head`；应进入第 5 节发布流程。

## 4. 家庭 UAT

先复制一份经过验证、revision 与候选代码一致的 UAT 数据库，再运行：

```powershell
.\scripts\windows\start_erp_uat.ps1 `
  -DatabasePath "D:\tm-uat\p0a\carton_erp_uat.sqlite3" `
  -Port 18080 `
  -PythonPath "D:\纸箱厂erp软件搭建\.venv\Scripts\python.exe"
```

`PythonPath` 可省略；省略时要求当前 UAT 工作树自带 `.venv`。显式指定时只复用已安装依赖的 Python 运行时，应用代码仍来自当前 UAT 工作树，数据库和端口仍保持隔离。

门禁会同时拒绝当前工作树默认库和已确认的工厂绝对正式库、端口 8000、非 `18000-19999` 端口、缺失副本、完整性异常或 revision 不匹配。该入口不会迁移 UAT 副本。

## 5. 正式发布与迁移

### 5.1 Prepare：停服、备份、隔离演练

只有已审查 SHA 已经位于干净的 `factory-current-baseline` 工作区时才运行：

```powershell
.\scripts\admin\release_erp.ps1 -Prepare `
  -PreviousCodeSha "<更新前正式版本40位SHA>" `
  -ExpectedCodeSha "<已批准的40位SHA>" `
  -ExpectedRevision "<已批准的Alembic revision>"
```

Prepare 固定执行：生产配置与路径核验 → 验证更新前 SHA 是目标 SHA 的祖先 → 验证
更新前代码唯一 Alembic head 与正式库 current 完全一致 → 精确识别并停止 ERP →
SQLite Backup API 备份 → 备份哈希、完整性、外键、revision、核心表计数核验 →
从备份创建隔离演练副本 → 隔离副本精确迁移到指定 revision → 再次核验。

报告写入 `docs\migration_reports\release_runtime_*.json`，备份写入 `data\backups`，演练副本写入 `data\release_rehearsals`。这些运行产物不进入 Git。Prepare 完成后正式库尚未迁移，ERP 保持停服，并打印与本次证据绑定的授权口令。

P0-5A 起报告升级为签名 schema v3，额外记录更新前/目标代码 SHA、更新前/目标
Alembic head、脱敏配置与依赖指纹。授权口令使用随机值，报告只保存口令哈希，
不保存可重放的明文口令。

### 5.2 人工复核

至少核对：

- `code_sha` 和 `expected_revision` 是本次批准值；
- `previous_code_sha` 是更新前正式版本，且 `previous_code_revision` 等于迁移前
  正式库 revision；
- 正式库路径正确；
- source、backup、rehearsal 的 `integrity_check=ok`、外键异常 0；
- backup/rehearsal 路径及 SHA-256 已记录；
- 演练后 revision 正确；
- 核心业务表计数没有变化。

任何一项不符都停止，不运行 Apply。

### 5.3 Apply：再次授权后才写正式库

```powershell
.\scripts\admin\release_erp.ps1 -Apply `
  -PlanPath "<Prepare 输出的完整报告路径>" `
  -ApprovalToken "<Prepare 输出的 APPLY-... 口令>"
```

Apply 会再次确认服务已停止、代码 SHA/head 未变化、正式库路径/hash/revision/核心表计数未变化，然后才精确迁移到报告中的 revision。迁移、完整性、外键、revision 和计数通过后，在仍停服时计算全应用表逻辑指纹。服务健康启动后再次确认数据库文件/WAL 与停服现场完全一致，才把报告标为 `completed`；如果启动窗口出现任何变化或证据写入失败，会重新停止刚启动的 ERP。

发布完成后原子更新：

```text
data\release_state\latest_completed_release.json
```

逻辑指纹按稳定顺序覆盖全部应用表内容，能识别订单数量或库位 UPDATE，也不会因
单纯 WAL checkpoint 误判为业务变化。它的基准时点固定在启动前，不能把启动后的
业务写入重新吸收到“无变化”基线。

## 6. 失败与恢复原则

- 任一步失败立即停止；迁移失败或启动失败时服务不得自动带病启动。
- 保留报告、正式库现场、已验证备份和演练副本，不覆盖或删除。
- 不通过修改 `alembic_version`、替换原始 BAK、跳过外键/完整性检查来“继续”。
- 是否恢复备份、重新发布或修复代码必须作为独立人工决策；执行恢复时继续遵守停服、现场备份、哈希、完整性、revision 和健康检查门禁。

## 7. P0-5A 离线故障检查

完成至少一次签名 schema v3 正式发布后，可在 ERP 网页无法打开时双击：

```text
scripts\windows\erp_fault_check.bat
```

也可在 PowerShell 运行：

```powershell
.\scripts\admin\rollback_erp.ps1
```

该入口严格只读：不停止 ERP、不切换 Git、不迁移或恢复数据库。它只会显示：

- 当前/上一代码版本；
- 发布完成后数据库是否变化及变化表；
- 更新前备份是否仍可验证；
- `具备完整回退申请条件` 或 `当前不能安全回退`。

发布报告和最近发布指针都必须先通过本机受保护会话密钥派生的 HMAC 签名验证。
手工修改报告后重算普通文件哈希不能恢复可信状态；会话密钥缺失或轮换时也会
fail-closed，要求转人工离线核验。运行配置指纹同时覆盖当前 Python 环境实际安装
包版本与安装记录，不只检查 requirements 文件。

P0-5A 不执行真正回退。数据库只要发生变化，即使 revision 相同也一律保持阻断，
不会接受发布报告内可被手工填写的“兼容”声明。仅代码回退必须等 P0-5B 建立绑定
当前数据指纹、可验签且不可伪造的隔离兼容证据链后再评估。正式环境网页
`/api/system/backups/restore` 已禁用，不能绕过离线门禁在线覆盖数据库。

## 8. P0-5B 阶段 1：上一版本隔离兼容演练

P0-5A 人工验收通过后，P0-5B 仍分成两个独立授权阶段。阶段 1 只验证上一版本能否
在独立目录和数据库副本上安全启动，不执行正式切换或正式数据库恢复。

由工厂 Codex 在正式项目目录运行：

```powershell
.\scripts\admin\rollback_runtime.ps1 -TechnicalDetails
```

脚本自动从已验签的最近发布指针读取上一版本，不接受人工填写 SHA、revision、原因
或兼容声明。它会：

1. 在项目目录之外建立唯一、全新的隔离输出目录；
2. 用 `git archive` 解包签名发布报告绑定的上一版本，不切换当前 checkout；
3. 核对上一版本与当前版本的依赖清单；内容变化时阻断，只有 CRLF/LF 差异不会
   被误判；
4. 数据未变化时，从已验证更新前备份再次复制演练库；数据已变化但 revision 与
   上一版本相同时，从当前数据库通过 SQLite Backup API 创建演练副本；
5. 只在 `127.0.0.1` 的 18000--19999 非正式端口启动上一版本；
6. 核对 Alembic current、`integrity_check`、外键、核心业务表和
   `/api/health = {"ok": true}`，并确认监听 PID 属于本次启动的隔离进程树；
7. 比较启动前后演练数据库的全表逻辑指纹，并确认当前数据库、正式分支、HEAD、
   工作区和旧代码运行目录均未变化；
8. 分别用 `rollback-runtime-manifest-v1` 与 `rollback-compatibility-v1`
   签发用途隔离的 HMAC 证据。

以下任一情况都会安全阻断：指针/报告/签名被修改、备份损坏、旧 SHA 或 revision
不一致、依赖真正变化、端口 8000 或非 loopback、目录逃逸或覆盖、旧程序启动时
写入演练库/代码目录、健康响应来自其他进程、隔离进程树或端口没有完全退出、
当前数据库或正式 Git 在演练期间变化。旧程序子进程使用最小环境白名单和 Python
隔离模式，不继承 `PYTHONPATH`、`PYTHONHOME`、`ALEMBIC_CONFIG` 或
`UVICORN_*` 注入变量。

阶段 1 仍然不会停止 8000 服务，不会运行 Alembic upgrade/downgrade，不会执行
`git switch/reset/stash`，不会替换或覆盖正式数据库。只有阶段 1 另行完成人工
验收后，才能开发和授权阶段 2 的活动运行指针、现场备份、一次确认切换及失败自动
恢复；阶段 2 也必须让旧代码只接触新的候选数据库副本。阶段 1 报告明确写入
`execution_eligible=false`、随机证据编号和 24 小时有效期，绝不能直接作为正式
切换授权；阶段 2 必须停服后重新验签并复核现场。

## 9. P0-5B 阶段 2：活动运行目录切换与一次自动恢复

阶段 2 只提供离线命令行入口，不提供网页“一键回退”。它不会切换或重置正式
Git checkout，也不会让旧代码直接连接或覆盖原正式数据库。工厂真实执行前仍须
取得本次回退的单独授权。

### 9.1 Prepare：不停服准备

先取得仍在有效期内的阶段 1 兼容报告，再运行：

```powershell
.\scripts\admin\rollback_execute.ps1 -Prepare `
  -CompatibilityReport "<阶段1 compatibility_report.json 完整路径>"
```

Prepare 会重新验签发布、阶段 1、代码、依赖和数据库证据；创建“回退前当前现场”
SQLite 备份、候选数据库、独立旧版本活动目录、签名切换计划和短时一次口令。完整
数据库回退还会显示第二个数据库授权口令及可能丢失数据的时间范围。

Prepare 不停止 ERP、不创建活动指针、不修改正式 checkout，也不写原正式数据库。
旧代码以后只会使用本轮新建的候选 SQLite。所有输出固定放在正式 checkout 外的
`tm-rollback-runtimes` 新目录中。

### 9.2 人工核对

至少核对：

- 模式是“仅代码回退”还是“完整数据库回退”；
- 当前/上一代码 SHA、Alembic revision 与阶段 1 证据一致；
- 现场备份和候选数据库均为新路径，完整性为 `ok`、外键异常为 0；
- 完整回退的数据时间范围已经明确；
- 正式服务仍在运行，活动指针尚未创建；
- 负责人一次口令没有写进计划或文档。

### 9.3 Apply：一次授权后切换

```powershell
.\scripts\admin\rollback_execute.ps1 -Apply `
  -PlanPath "<Prepare 输出的 rollback_switch_plan.json 完整路径>" `
  -ApprovalToken "<ROLLBACK-...>"
```

完整数据库回退还必须增加：

```powershell
-DatabaseApprovalToken "<DATABASE-...>"
```

Apply 的固定顺序为：

1. 停服前重新验签并原子消费一次口令；
2. 当前正式 ERP 继续运行时，让目标旧版本只在 loopback 维护端口预热；
3. 预热使用本轮独立数据库、会话密钥、上传临时目录和备份目录，预热前后核对
   正式私有上传目录及会话密钥完全未变；
4. 精确核对单 worker、进程创建时间、命令行、运行目录、Python 路径和监听 PID，
   再停止当前 ERP；
5. 原子写入受签名保护的活动运行指针；
6. 消费“预开放自动恢复许可”，写入正式开放意向和持久开放证据；
7. 按活动指针启动目标版本，复核唯一监听、精确健康响应、数据库身份、
   revision、完整性和外键，再签发最终结果。

启动脚本只接受固定的控制器参数列表，不接受额外 `-c`、重复参数或任意脚本
注入。活动目录、控制文件和可变文件副本中的 symlink/reparse point、路径逃逸、
数据库替换或未知监听者都会 fail-closed。

### 9.4 失败边界

- 正式开放许可尚未消费时，目标失败只允许一次受控恢复：撤销目标指针、恢复
  当前运行目录并启动当前版本。
- 自动恢复也失败时保持停机，签发
  `stopped_manual_recovery_required`，不循环重启。
- 一旦预开放许可已消费，即使正式开放意向文件还未来得及写入，也永久禁止自动
  restore/revoke；目标指针和候选数据库必须保留，签发
  `manual_target_retained` 后转人工处理，避免丢失开放后的业务写入。
- 原正式 PID 已退出但正式端口被未知 PID 抢占时，不误停未知进程；签发
  `stopped_before_activation_manual_recovery_required`，记录已停 PID 和观察到
  的监听 PID，保持未激活现场等待人工处理。

### 9.5 与日常发布的互锁

`release_erp.ps1` 和直接运行的 `release_erp.py` 在 Prepare、Apply 前都会检查
固定活动指针及持久激活声明。只要存在活动指针、未撤销声明、正式开放证据、
损坏文件、未知条目或 reparse point，普通发布就会阻断。只有“声明 + 匹配且已
验签的撤销记录、无正式开放证据”才允许回到普通发布流程。

阶段 2 不运行 Alembic upgrade/downgrade，不新增 migration，不执行
`git switch/reset/checkout/stash`，也不自动删除计划、备份、候选数据库、
活动目录或签名证据。
