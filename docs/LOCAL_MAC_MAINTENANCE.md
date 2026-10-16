# Mac日常维护：普通用户工作与受保护维护分开验收

2026-10-16 12:43接续：正式业务v0.22.645已技术发布，源991d8e2d、签名包effc98a3…、PID18242；人工验收待完成。开发钩子已执行，正式更新完成钩子和日志限额已随包部署。本次备份即时清理成功，未触发延迟收尾分支；勿把源码部署或null回执说成该分支已经正式演练。不要把工作区APP_VERSION理解成正式程序身份，也不要覆盖已签名不可变程序。

## 工作区与非交互规则

- 使用 /Users/kimiyf/ERP-Migration/erp-plan-v3-20261014。开发依赖留在 /Users/kimiyf/ERP-Migration/venv；不sudo pip、不改系统目录权限、不用正式库运行测试。
- UAT子进程的解释器必须是原规则支持的实际普通文件，不能把venv的符号链接当作已通过可信解释器检查。本轮使用 /Library/TianmingERP-Rehearsal-v643-20261016/owned/candidate-code/runtime/bin/python3.12，UID501执行，不提权；该当前演练区在人工验收前保留。
- scripts/uat/run_task.py 自动关闭stdin；默认等待1800秒，--timeout-seconds最多7200秒。超时只记录timed_out/124并保留现场，不盲杀迁移或并行重跑。源库、命令结果和小回执保留。
- 地图验证显式传 --layout-source 指向本次捕获的正式runtime地图，避免默认源码种子地图冒充现场。正式只读页面检查不自动点击。
- 原始正式副本内PDF绝对路径可能仍指正式shared，普通用户不可读取时必须停止该依赖。本轮通过官方create_sqlite_copy的隔离transform，仅将8条PDF路径重绑到已复制且SHA匹配的原件，再由run_task创建受管副本；原源库SHA未变。证据脚本在 evidence/storage-map-20261016/verify-real-task-hook.py（完整绝对根见回执）。不得清空样本或绕过UAT路径检查。

## 开发保留机制（当前工作区已生效）

- build.remove_transient_build_trees 在构建成功后删除可重建payload/pyi-work，保留签名/未签名候选及校验记录，并写build-retention.json；不足60GiB告警，不降低正式恢复预算。本轮已实际在v643构建目录执行。
- run_task完成后保留结果/日志；成功且未keep-copy的任务尝试原清理器。失败/超时/人工保留/活动引用不推断关闭。成功副本如暂不能删，则封存元数据身份，下一任务结束时再次审查旧已封存成功副本；不立即重复当前任务失败清理。
- desktop_assistant.task_retention 默认dry-run，--apply显式执行。只接受当前用户拥有、Git外、已登记并封存的task-UUID/copies；完整记录、源引用、路径、权限和原进程检查任一不明即保留。重放无额外删除。30GiB仅是已登记测试副本的软预算，不是已治理整个磁盘的声明。

```sh
cd /Users/kimiyf/ERP-Migration/erp-plan-v3-20261014
/Users/kimiyf/ERP-Migration/venv/bin/python -m desktop_assistant.task_retention --base-dir /Users/kimiyf/ERP-Migration/managed-storage-check-normalized
# 审查该次清单后才加 --apply；不加sudo，不自动唤起授权窗口。
```

## 受保护恢复副本收尾

- 必须绑定唯一原job/verified.json、已发布恢复回执与索引、实际密文SHA、完整manifest/逐文件SHA、库完整性和原程序签名；原操作锁内检查健康当前版及状态未漂移。
- 先落持久意图，再按原remove_owned_tree删除确证的independent-recovery；仅删除成功后解除该条backup_work_retained，业务/发布/迁移状态不制造成功。原密文、正式库、业务原件与程序包不删。
- 本轮固定受保护入口已实际执行并收尾。中断/部分删除恢复日志及after_update集成已经定向验证，并随v645业务包部署；原监督器未替换。本次正式即时清理成功，延迟钩子仍以相同守卫处理以后确有遗留的副本。
- UID499不能读取某些UID501/root应用的argv/env，原进程守卫会保留副本；私有目录权限本身不能证明没有旧打开句柄，不放松守卫。没有常驻受限系统执行通道，不承诺永久自动清理。需要高权限时必须走合法、固定范围、摘要绑定的原系统授权；本轮OS等待1800秒、请求进程1830秒，不重试已证明不可用的sudo免密，不使用SecurityAgent自动化。

## 正式事务的运行身份与恢复

- Apple系统Python负责受保护入口、脚本摘要及完整不可变执行树准入。原清理模块依赖psutil，必须在该完整核验之后使用签名包内的Python运行时；不得向系统Python临时安装依赖或注入用户可写site-packages。本轮首次启动器因缺psutil在停服前退出，修正一次性启动器后实际准入成功；业务签名包未改变。
- 受控高权限准入仅执行确证清单回收和派发，随后退出。在线预检、最终切换及同事务等待控制器统一用正式UID499，不用持久root Python父进程维持切换。
- 发布必须持久记录preflight、stop_requested、process_ready及technical_release_complete。聊天退出不取消正式事务。超时保留状态，不盲杀迁移或并行再发布。
- 更新中的监视使用有界shell curl和小日志tail；不要用跨身份长驻Python监视器给原清理进程守卫制造不透明应用进程。preflight最终记录含完整原件清单，不直接整行输出。
- 无迁移也须证明旧程序能读取新写入文档字段；失败恢复同时绑定库事实与原件变化。新增业务事实后不得自动用旧数据库恢复，更不能只比较revision就启旧版。

## 仍需独立完成

- 正式日志策略已部署为8MiB活动文件＋三份备份，本次实际文件6,353,200字节、在限额内；后台延迟回收钩子已部署，但跨身份不可检查引用仍保留。正式包/历史完整备份的归档耐久性未取得新证明，保留它们。未知失败现场不按mtime或数量强删。
- 不新增NAS备份动作，不更换监督器，不为减少目录数量移动正式数据根。当前业务、监督/迁移/恢复/签名助手依赖可以是多包。
- 已完成旧演练仅回收明确清单；当前v643演练、历史源数据库、地图治理证据与人工验收依赖仍保留。正式地图沿用既有区域和location_id，默认货位不是有货事实。
