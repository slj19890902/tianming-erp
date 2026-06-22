BoxERP Windows 生产部署与开机自启指南
=====================================

一、正式切换数据库
------------------
1. 打开 PowerShell 并进入项目目录：
   Set-Location "D:\纸箱厂erp软件搭建"

2. 确认 NAS 可访问：
   Test-Path "Z:\sata1-18015598002\BoxERP\backups"

3. 当前已验证数据库就在 data\carton_erp.sqlite3 时运行：
   .\.venv\Scripts\python.exe scripts\deploy_production.py

4. 以后若有明确的新预览库，必须显式指定：
   .\.venv\Scripts\python.exe scripts\deploy_production.py `
     --source "D:\纸箱厂erp软件搭建\migration-workfiles\phase3_preview.sqlite3"

脚本会校验源库，向 NAS 生成带 _pre_production 后缀的备份，通过
SQLite backup API 写临时库，再次校验后覆盖目标库，并更新项目根目录 .env。
任一步失败都不会用损坏文件覆盖生产库。

二、手动启动与停止
------------------
启动：
   .\start_erp.bat

访问：
- 本机：http://127.0.0.1:8000/
- 局域网：http://服务器局域网IP:8000/

日志：
   D:\纸箱厂erp软件搭建\logs\erp_server.log

手工窗口运行时按 Ctrl+C 停止。SQLite 必须固定一个 Uvicorn worker，
因此 start_erp.bat 使用 --workers 1。

三、任务计划程序开机自启（推荐）
--------------------------------
1. 打开“任务计划程序”，选择“创建任务”，不要选择“创建基本任务”。

2. “常规”页：
- 名称填写 BoxERP。
- 勾选“使用最高权限运行”。
- 若继续使用映射盘 Z:，选择“仅在用户登录时运行”。
- 若要求无人登录也运行，应把 ERP_BACKUP_DIR 改为 NAS 的 UNC 路径，
  例如 \\NAS主机名\共享目录\BoxERP\backups，并使用有 NAS 权限的账号。

3. “触发器”页：
- 新建，开始任务选择“启动时”。
- 建议延迟 1 分钟，等待网络和 NAS 完成连接。

4. “操作”页：
- 程序或脚本：C:\Windows\System32\cmd.exe
- 添加参数：/c ""D:\纸箱厂erp软件搭建\start_erp.bat""
- 起始于：D:\纸箱厂erp软件搭建

5. “条件”页：
- 服务器持续运行时取消“只有在计算机使用交流电源时才启动”。
- 使用 Wi-Fi 时可限制为厂内网络连接可用时启动。

6. “设置”页：
- 失败后每 1 分钟重新启动，尝试 3 次。
- 任务已运行时选择“不启动新实例”。

7. 保存后右键 BoxERP 并选择“运行”，检查：
- http://127.0.0.1:8000/ 能打开。
- logs\erp_server.log 没有 Traceback。
- http://127.0.0.1:8000/docs 返回 404。

任务计划程序后台调用 cmd.exe /c 时不会长期显示黑框。不要把批处理文件
放入 Windows“启动”文件夹。

四、NSSM 后台服务
-----------------
1. 将 nssm.exe 放在固定目录，例如 C:\Tools\nssm\nssm.exe。

2. 以管理员 PowerShell 执行：
   C:\Tools\nssm\nssm.exe install BoxERP

3. NSSM 窗口填写：
- Application path：C:\Windows\System32\cmd.exe
- Startup directory：D:\纸箱厂erp软件搭建
- Arguments：/c "D:\纸箱厂erp软件搭建\start_erp.bat"

4. “Log on”页选择有项目目录和 NAS 权限的 Windows 账号。
   Windows 服务通常看不到用户映射的 Z: 盘，NSSM 模式必须优先使用 UNC。

5. 安装并启动：
   C:\Tools\nssm\nssm.exe set BoxERP Start SERVICE_AUTO_START
   C:\Tools\nssm\nssm.exe start BoxERP

6. 维护命令：
   C:\Tools\nssm\nssm.exe status BoxERP
   C:\Tools\nssm\nssm.exe restart BoxERP
   C:\Tools\nssm\nssm.exe stop BoxERP
   C:\Tools\nssm\nssm.exe remove BoxERP confirm

五、防火墙与局域网
------------------
首次部署时，以管理员 PowerShell 仅放行私有网络：
   New-NetFirewallRule `
     -DisplayName "BoxERP TCP 8000" `
     -Direction Inbound `
     -Action Allow `
     -Protocol TCP `
     -LocalPort 8000 `
     -Profile Private,Domain

使用 ipconfig 查看服务器局域网 IP。.env 中 ERP_ALLOWED_ORIGINS 只允许
localhost、127.0.0.1 和私有局域网 IP，禁止使用 * 或公网地址。

六、生产安全检查
----------------
- ERP_ENVIRONMENT=production 时 /docs、/redoc、/openapi.json 都返回 404。
- FastAPI debug 关闭，不向客户端暴露调试 traceback。
- 数据库运行在本机 data\carton_erp.sqlite3，不直接放在 NAS 上运行。
- NAS 仅保存 SQLite backup API 生成并通过完整性校验的备份。

七、回滚
--------
1. 停止 BoxERP。
2. 找到本次生成的 *_pre_production.sqlite3。
3. 通过系统数据恢复功能或既有 restore API 恢复。
4. 检查 integrity_check、登录和订单链路。
5. 重新启动 BoxERP。
