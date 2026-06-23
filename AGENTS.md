# 天明 ERP 项目级执行规则

1. 每次开始任务前，先读取 `docs/CODEX_HANDOFF.md`。
2. 涉及迁移时，同时读取：
   - `docs/MIGRATION_PLAN.md`
   - `docs/MIGRATION_RUNBOOK.md`
   - `docs/MIGRATION_CHECKLIST.md`
3. 每次迁移前必须先备份数据库并验证备份。
4. 每次只做一个最小可验证闭环。
5. 禁止无备份写入数据库。
6. 禁止直接覆盖生产库、主沙盘或原始 BAK。
7. 禁止把 `erp.db` 当作 4 万历史订单来源。
8. `BoxDB20_REPRO` 只作为隔离权威源使用，先只读核对。
9. 第一步只能做 SQL Server → `legacy_ruida_*` 的 dry-run 差异统计。
10. 未经明确授权，禁止刷新 `legacy_ruida_*`。
11. 未经 100 条测试迁移验收，禁止全量写入正式业务表。
12. 长日志和大规模差异清单写入 `docs/migration_reports/`，不要塞入聊天。
13. 每轮完成后更新 `docs/CODEX_HANDOFF.md`。
14. 回复只提供：
    - 摘要
    - 验证结果
    - 是否写入数据
    - 下一步建议

