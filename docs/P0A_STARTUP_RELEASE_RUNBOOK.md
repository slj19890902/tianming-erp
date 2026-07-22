# P0-A 启动、发布、迁移与 UAT 隔离操作手册

## 1. 本轮安全边界

- 普通启动只做配置、数据库可读性、`integrity_check`、外键和 Alembic `current == code head` 检查。
- 普通启动绝不执行 `alembic upgrade`；revision 不一致时保持停机并给出明确错误。
- 旧 `scripts/admin/update_erp.ps1` 已 fail-closed 停用，不再自动拉代码、迁移或重启。
- 正式迁移只允许走 `scripts/admin/release_erp.ps1` 的 Prepare / Apply 两阶段流程。
- 家庭 UAT 只允许独立 SQLite 副本、`18000-19999` 独立端口、loopback 和 `ERP_ENVIRONMENT=test`。
- 本轮没有新增 Alembic revision，也不包含 N081。

## 2. 当前工厂部署前置阻断

正式启动器现在强制要求：

- `ERP_ENVIRONMENT=production`；
- 数据库必须为当前项目的 `data\carton_erp.sqlite3`；
- 后端只监听 `127.0.0.1` 或 `::1`；
- `ERP_HEALTH_URL`、`ERP_BROWSER_URL` 和允许来源使用已批准的 HTTPS 地址；
- 显式可信 Host、loopback 代理地址、单 worker 和合格会话密钥。

当前工厂仍是 `development + 0.0.0.0:8000 + LAN HTTP`，且没有已确认的 HTTPS 反向代理。因此，本 P0-A 分支只能先做代码审查和隔离 UAT；在网络/证书/反向代理方案单独批准并配置前，不得直接替换工厂启动脚本或重启正式服务。

## 3. 普通正式启动

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
  -ExpectedCodeSha "<已批准的40位SHA>" `
  -ExpectedRevision "<已批准的Alembic revision>"
```

Prepare 固定执行：生产配置与路径核验 → SHA/revision 核验 → 精确识别并停止 ERP → SQLite Backup API 备份 → 备份哈希、完整性、外键、revision、核心表计数核验 → 从备份创建隔离演练副本 → 隔离副本精确迁移到指定 revision → 再次核验。

报告写入 `docs\migration_reports\release_runtime_*.json`，备份写入 `data\backups`，演练副本写入 `data\release_rehearsals`。这些运行产物不进入 Git。Prepare 完成后正式库尚未迁移，ERP 保持停服，并打印与本次证据绑定的授权口令。

### 5.2 人工复核

至少核对：

- `code_sha` 和 `expected_revision` 是本次批准值；
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

Apply 会再次确认服务已停止、代码 SHA/head 未变化、正式库路径/hash/revision/核心表计数未变化，然后才精确迁移到报告中的 revision。迁移、完整性、外键、revision、计数和普通启动健康检查全部通过后才启动服务并把报告标为 `completed`。

## 6. 失败与恢复原则

- 任一步失败立即停止；迁移失败或启动失败时服务不得自动带病启动。
- 保留报告、正式库现场、已验证备份和演练副本，不覆盖或删除。
- 不通过修改 `alembic_version`、替换原始 BAK、跳过外键/完整性检查来“继续”。
- 是否恢复备份、重新发布或修复代码必须作为独立人工决策；执行恢复时继续遵守停服、现场备份、哈希、完整性、revision 和健康检查门禁。
