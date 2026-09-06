# 天明 ERP Codex 强制入口

本仓库的唯一有效业务总需求为：

`docs/TIANMING_ERP_MASTER_REQUIREMENTS.md`

开始任何分析、开发、测试、迁移、发布或交接前，必须依次读取：

1. `docs/CODEX_START.md`；
2. NAS `Z:\sata1-18015598002\天明ERP知识库\AI_START.md`；
3. 当前任务卡；
4. `docs/CODEX_START.md` 路由出的一个模块上下文，以及总需求中的相关章节；
5. 当前任务需要的 `docs/CODEX_EXECUTION_CHARTER.md` 小节。

不得把 `docs/CODEX_HANDOFF.md`、完整能力台账、原始聊天备份或本总需求全文作为每个子任务的固定上下文。只按任务编号、业务对象、错误文字、API、模型、提交或 migration 精确检索。

## 不可变边界

- 每次只做一个最小可验证闭环；已有无关改动必须保留。
- 家庭端只在正式数据库隔离副本上验证；禁止写、覆盖或替换工厂正式数据库。
- 未备份并验证前禁止正式迁移；已退役的旧系统抽取层和迁移入口不得重新创建或刷新。
- `BoxDB20_REPRO` 只作隔离权威源；`erp.db` 不是历史订单正式来源。
- 未获授权不得合并正式分支、执行正式迁移、停服或重启。家庭候选的提交与推送按当前任务授权执行。
- 修复不能删除权限、客户范围、数量、状态、幂等、事务、版本、审计或数据库隔离门禁。
- 默认单代理；只有老板或任务卡明确要求并行时才使用多代理，每个文件只能有一个写入负责人。
- 禁止使用内置浏览器 IAB 做验证，禁止调用任何 `browser.tabs.finalize(...)` 或等效批量清理；需要网页验收时使用 Chrome，Chrome 不可用则如实记录未验收。

涉及迁移时还必须完整读取：

- `docs/MIGRATION_PLAN.md`
- `docs/MIGRATION_RUNBOOK.md`
- `docs/MIGRATION_CHECKLIST.md`

每轮结束必须运行与风险相称的测试和差异检查，并写 NAS 独立任务回执。只有正式基线、迁移链、跨端紧急事实或长期启动规则变化时，才给 `docs/CODEX_HANDOFF.md` 增加简短索引。

