# Astra 第三阶段整改再验收交接（历史入口）

**当前结论优先：7af3ee7d 的下述完成结论经 Astra 再验收撤回。** 之后的实际整改、P08 下一阶段与最终复验入口为 [06_当前整改与下一阶段交付](06_当前整改与下一阶段交付.md)，以该文和机器证据索引为准。旧归档通过不能替代最终归档校验，旧 v479 不是实时运行版本。

## 原交接历史（非当前完成依据）

结论：F1—F3 技术整改已完成，候选分支待 Astra 再验收。本轮只新增测试、离线清单工具和交接文档；没有应用运行代码、迁移或正式数据变更，因此 v0.22.479 不重新发布。

起始正式基线为 `4975711847560f8def9e99c0ecbc65d40c1b60a1`；整合前刷新为 `ae3d1b4f782003d9a2a7a63150c5be955579c438`，本分支已无冲突 rebase。候选 HEAD：以 Git 实时读取为准；运行包仍应与历史发布源码 `b007abd41fd73666d77789b250b448cc41a8cec1`、v0.22.479 区分记录，不能由本轮文档提交冒充。

F1：`tests/test_phase3_scale_reliability.py::test_sqlite_delete_journal_lock_wait_positive_control` 使用新文件 SQLite、两个独立连接、DELETE journal 与 5000ms busy timeout。`BEGIN EXCLUSIVE` 的受控释放使第二连接实际等待约 219ms 后提交；普通无竞争读取只记录请求、SQL 和应用互斥锁时间，SQLite 忙等待在 Python DB-API 中明确标为未直接可观测而非零。

F2：每个 100/1000/5000 参数档创建独立数据库，分别记录总量、筛选量和全局投影量。合成夹具含采购、收料、生产、库存预留和部分发货四类关系，且明确不代表正常业务 API 主链。冷、热、提交后、受控读写重叠与真实终态管理状态改变均有定向断言。最终相关 nodeid 4 项通过；P04 文件完整集 6 项通过。证据根：`D:\tm-worktrees\erp-phase3-evidence\P07\p07-f1-f2-inventory-states-20260921-01`。

F3：`scripts/qa/verify_phase3_manifest.py` 使用“文件字节 CRLF 规范化为 LF 后 SHA-256”的跨机器规则；`MANIFEST_ASTRA_REWORK.json` 排除自身，分组列出代码、测试、文档与报告摘要。当前树及 Git ZIP 归档均已通过；归档证据在 `D:\tm-worktrees\erp-phase3-evidence\P07\p07-manifest-zip-archive-20260921-03`。历史 11 项差异是换行字节口径，不是篡改。P02 已改为两个精确参数化 nodeid。

不需要管理员页面验收：没有可见前端变化，也没有浏览器自动化。真实 PDF、实纸、手机和备用电脑恢复仍是独立现场事项，未标为通过。本轮尚未写 NAS 回执；候选推送后应在 NAS `04_开发记录\任务回执` 写入同名独立回执。
