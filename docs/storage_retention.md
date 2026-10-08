# 更新与测试副本保留规则

## 更新器

完整更新链成功、服务启动通过后，在同一维护锁内清理。失败更新和未完成迁移不触发。

- 保留 current、previous、schema_authority、migration_target，以及这些版本签名契约直接引用的兼容回退包。
- 仍被进程引用、安装内容修改过、含未知文件/链接或安装包校验失败的旧目录保留。
- 未修改的旧解压目录有本地签名包可重建时删除；shared 联接只解除链接，不遍历业务目录。
- 旧压缩包必须在 NAS 发布目录存在同哈希副本且已完成同步才删除。不额外重复上传所有版本。
- ZSpace/FUSE 挂载的上传队列未清空、挂载状态不可确认或 NAS 不可读时，本地压缩包保留；后续完整更新或“已是当前版本”的检查再次尝试。
- `control/retention-latest.json` 记录删除项目及保留原因。清理失败不触发程序回退，也不改变成功更新结果。

## 自动测试

普通 pytest：成功用例的 tmp_path 自动回收；失败用例保留最近一轮，下一轮替换旧轮次。不要通过任意 `--basetemp` 指向已有资料目录。

需要独立数据库的测试，使用仓库的 Python 环境与绝对命令路径：

```powershell
python scripts/uat/run_task.py --source-database D:\ERP-UAT\approved-source.sqlite3 -- C:\Path\To\python.exe -m pytest tests\test_example.py -q
```

入口只读复制显式来源，把数据库、上传、日志、备份等写根绑定到独立 task UUID。默认根为 `D:\ERP-UAT\managed-tasks`；每个任务保留 `result.json`、stdout.log、stderr.log，成功才删除 `copies`。测试命令必须以前台方式运行；`--keep-copy` 可保留现场。失败、中断或存在引用的副本保留，报告说明原因，不强杀进程。

更新器隔离迁移演练成功后删除 release/shared 临时副本，保留完整 v2 结构证据报告和迁移日志；失败现场保留。中断更新仍可凭 v2 报告完成一致性核验，不需要成功演练副本继续占磁盘。

交互 UAT 的服务退出不等于人工验收通过。历史手工创建、无任务登记的副本及代码工作树不自动猜测归属或删除；这一规则不关闭现有服务，不删除未合并或未提交代码。
