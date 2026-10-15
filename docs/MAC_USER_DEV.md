# Mac 普通用户开发维护

适用范围：天明 ERP 的本地开发、依赖安装、构建、测试、预览和开发服务重启。正式发布、正式数据库、签名、系统服务、系统钥匙串和 `/Library/TianmingERP` 不在本工作流内。

## 固定工作区

本次隔离工作树：`/Users/kimiyf/ERP-Migration/mac-user-dev-20261015`，从本机 `origin/factory-current-baseline` 建立。它与 `/Users/kimiyf/ERP-Migration/repository`、其他候选工作树以及运行中的正式系统分离。若以后目标目录不可写，从当时的正式 Git 基线另建用户目录工作树并保留原有修改；不要 `sudo chown` 正式目录。

工具和缓存位于当前用户可写的 `/Users/kimiyf/ERP-Migration/tools`。Python 3.12.15 与 uv 0.13.0 已位于该目录；Node 22.23.3 取自 nodejs.org 官方 macOS arm64 归档，按官方 SHASUMS256 校验后解压到 `tools/node-official`。不改系统 Python、全局 npm、Homebrew 或 shell 登录配置。每个工作树使用自己的 `.venv`；npm 依赖在 `frontend-v2/node_modules`；数据库、密钥文件、备份、上传、日志和前端构建结果在本工作树的 `.local-dev`。这两个目录均被 Git 忽略。

## 无人值守命令

从上述工作树运行下列命令。所有子命令关闭标准输入，对安装、构建、测试和启动均设超时；失败查看 `.local-dev/logs`，不要改用提权重试。

```sh
/usr/bin/python3 scripts/mac_user_dev.py setup
/usr/bin/python3 scripts/mac_user_dev.py build
/usr/bin/python3 scripts/mac_user_dev.py test
/usr/bin/python3 scripts/mac_user_dev.py start
/usr/bin/python3 scripts/mac_user_dev.py preview
/usr/bin/python3 scripts/mac_user_dev.py restart
/usr/bin/python3 scripts/mac_user_dev.py status
/usr/bin/python3 scripts/mac_user_dev.py stop
```

`setup` 使用项目独立 `.venv` 安装 `requirements-dev.txt`，用官方 Node 的 npm 对锁文件执行 `npm ci`。`build` 对后端编译、前端类型检查及 Vite 构建；构建产物放 `.local-dev/frontend-dist`，不会覆盖已跟踪的正式资源。`test` 执行后端静态资源定向测试与可独立运行的前端 Node 测试。`start` 先只在 `.local-dev/data/dev.sqlite3` 初始化空开发库，再在 `127.0.0.1:18380` 启动隔离后端并验证 `/api/health`；`preview` 在 `127.0.0.1:15380/frontend-v2/` 启动构建预览并验证 HTTP 200。`restart` 仅对本工作树记录且通过命令行身份核验的后端 PID 发 SIGTERM，再启动新实例。`stop` 同样只处理这两个本地进程。

脚本清除继承的 `ERP_*` 环境变量，显式指定 development 模式、独立数据库、备份和上传目录；若工作树含 `.env` 就拒绝启动，以免继承正式配置。端口始终绑定 loopback，不转发到正式服务。

## 密码来源与边界

2026-10-15 的只读检查显示本用户为 UID 501，`/Library/TianmingERP` 属 root，正式 Python 进程以 UID 499 运行。近期 `/Users/kimiyf/ERP-Migration/evidence/mac-cleanup-20261014/emergency-resume-v3.applescript` 等脚本使用 `do shell script ... with administrator privileges`，其 Python 操作 `/Library/TianmingERP/instance`、`fchown(...,499,499)` 和正式服务控制；另外旧管理员包使用 `/usr/bin/sudo /bin/sh`。这两类命令会要求 macOS 管理员授权；2026-10-15 `sudo -n -v` 返回 1，且已执行无提示的 `sudo -k -n`，本工作流不依赖 sudo 缓存。系统钥匙串/签名授权请求是另一个门禁，Codex 工具审批又是另一层；本地开发流程不访问这些凭据或操作。

正式系统的升级、服务重启、系统身份/钥匙串设置和正式数据库操作若仍要求管理员授权，应记录准确命令、影响并停在正式环节。不能把本地开发服务的免密重启描述成正式服务已免密或已发布。

## 2026-10-15 实机验收

已在该工作树修改维护代码、无密码安装完整 `requirements-dev.txt` 与前端锁定依赖、完成后端编译及前端类型检查/Vite 构建。后端定向测试 `3 passed`；前端独立测试 `96 passed`。隔离后端 `/api/health` 返回 `{"ok":true}`，前端预览 HTTP 200；后端以 UID 501 从 PID 33496 重启为 33537，重启后健康检查通过。无提示复核 `sudo -n -v` 返回 1，说明上述步骤未使用管理员缓存。工作树外的正式服务仍以 UID 499 运行，未停启或修改。

前端全量旧测试本次尝试结果为 101 项中 99 通过、2 失败，另有 `ordersWorkspaceState.test.cjs` 要求外部冻结旧源码，不能虚构基线。两项失败分别来自 `orderDrawingWorkflow.test.cjs` 的旧 `withRequestDeadline` 测试替身和 `workspaceTabActivation.test.cjs` 的过期源码正则；它们属于测试夹具与当前基线不匹配，未列入上述 96 项独立测试。不要把本次定向测试表述为前端全量通过。
