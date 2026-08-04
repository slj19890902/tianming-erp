# 每周工厂真实数据家庭副 ERP 使用说明

本工具用于把工厂正式 ERP 的一致性 SQLite 快照带到家庭电脑，在完全隔离的工作副本中复现和模拟业务。它不是正式发布工具，也不会把家庭测试数据同步回工厂。

## 数据结构

```text
工厂正式库
  └─ SQLite Backup API 一致性导出包
       └─ 家庭 received 只读收到件（永不直接运行）
            └─ 家庭 runs 可写工作副本（所有模拟只写这里）
```

默认家庭目录：

```text
D:\tm-weekly-uat\received\<package_id>\
D:\tm-weekly-uat\runs\<package_id>\
```

## 第一步：工厂导出

在工厂正式目录、`factory-current-baseline` 分支执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\admin\export_weekly_home_uat.ps1
```

也可以直接双击 `scripts\admin\export_weekly_home_uat.bat`。

默认输出到 `D:\tm-weekly-uat-exports`。每个包包含：

- `carton_erp_factory_snapshot.sqlite3`：通过 SQLite Backup API 创建的一致性副本；
- `manifest.json`：代码 SHA、数据库 revision、大小、SHA-256、完整性、外键和核心表计数；
- `manifest.sha256`：manifest 传输完整性校验。

必须传输整个包目录。不要传输 `.env`、`session_secret.key`、Cookie、浏览器资料或正式账号密码。数据库包含真实业务信息，只能使用老板指定的受控 NAS 目录或 BitLocker 加密介质，不得放入 Git、聊天附件或公共云同步目录。

## 第二步：家庭导入并启动

在家庭电脑执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\import_start_weekly_home_uat.ps1 `
  -PackageDir "D:\收到的完整包目录" `
  -Port 18200
```

日常使用可以直接双击 `scripts\windows\import_start_weekly_home_uat.bat`，随后在弹出的窗口中选择完整数据包目录，不需要手工输入命令。

脚本会自动：

1. 校验 manifest 和数据库；
2. `git fetch` 并确认 SHA 属于远端正式基线；
3. 按工厂精确 SHA 建立 detached 独立 worktree；
4. 保存只读收到件并生成可写工作副本；
5. 检查代码 SHA、唯一 Alembic head、数据库 revision、完整性、外键、主机、路径、端口和监听地址；
6. 使用 `test + 127.0.0.1 + 单 worker` 启动家庭副 ERP。

家庭副 ERP 地址为 `http://127.0.0.1:18200/`。测试账号只能在可写工作副本创建或修改；不得使用工厂正式账号密码。

## 第三步：重置本周测试数据

导入成功后会显示 `runtime.json` 路径。需要清除本周全部模拟操作时执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\reset_weekly_home_uat.ps1 `
  -RuntimeFile "D:\tm-weekly-uat\runs\<package_id>\runtime.json" `
  -Port 18200 `
  -PythonPath "D:\纸箱厂erp软件搭建\.venv\Scripts\python.exe"
```

日常使用可以直接双击 `scripts\windows\reset_weekly_home_uat.bat`；脚本会选择最近一次导入的本周副本，精确停止对应 UAT 进程、重置并重新启动。

重置器只会停止 PID 文件所指向且命令行同时匹配当前 UAT worktree 和端口的 Uvicorn 进程；随后用只读收到件覆盖标准命名的工作副本并重新启动。它不会删除或修改 received 原件。

## 固定拒绝条件

以下任一情况都会停止：

- 在工厂主机运行家庭导入、启动或重置；
- 家庭数据库不在 `D:\tm-weekly-uat\runs` 下；
- 使用正式数据库路径、8000 端口、非 `127.0.0.1` 或多 worker；
- 数据库 SHA、manifest SHA、代码 SHA、数据库 revision、唯一代码 head、完整性或外键不符合；
- 数据包提交不属于 `origin/factory-current-baseline`；
- 重置 PID 对应的不是当前家庭 UAT 进程。

普通家庭 UAT 启动不会自动执行 Alembic 迁移。要验证尚未发布的新候选，必须从同一 received 原件另外复制候选工作副本，并按迁移任务书在副本上单独演练，不能覆盖本工具建立的工厂镜像线。
