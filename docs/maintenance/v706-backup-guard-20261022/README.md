# 2026-10-22 维护修正留档

这是已审查的本机维护代码及定向测试留档，不属于业务包升级，不应从普通用户目录作为 root 运行。真实安装须沿现有系统授权入口，将固定摘要候选复制到 root 所有且不可由普通用户写入的目录后执行。不得跳过摘要、原文件身份或维护锁校验。

- 安装目标：`/Library/TianmingERP-LocalMaintenance/native.py`。
- 前版 SHA256：`ca3ce9c4b75f634eb286ca91caaaceefc461580c2567840a7665d295c76fdbc6`。
- 本版 SHA256：`2366c320799006465093224f6e86a8b5d1083cd5e34e66d54083a5f48340e9eb`。
- 已安装文件完整性白名单 `assets.json` 随受保护事务同步；策略新增150GB备份保留空间、全部完整备份保护及三份故障前在线点摘要。200GB清理目标保留。
- `native.patch` 用于审查；`native.py` 与 `reviewed.json` 保存精确候选，`test_closure.py` 是纯隔离假数据测试，不读写正式库。
- 依赖现有受保护 `storage_policy.py`、`file_points.py` 及仓库 `desktop_assistant`。原证据目录内运行13项通过；后续有真实风险再运行，不重复全量测试。

实际执行回执位于 `/Library/TianmingERP-MaintenanceClosure-20261022`；阶段和结果见 `events.jsonl`、`result.json`。实际恢复证明必须 `paired-restore.json.verified=true`，不能只看快照生成成功。恢复调度以两个原 LaunchDaemon 的实际状态为准。

所有完整 `.tmbackup` 禁止由自动清理删除；历史四份缺失保持事故记录，不从保留摘要中抹去，不拿新快照补造。依赖不全时保守保留程序原包。新在线点按原滚动规则及固定保护摘要处理，原件对象只可在所有保留清单都无引用时回收。
