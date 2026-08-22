# P1-89 回单对账归属月份隔离迁移往返报告

## 身份与边界

- 开发基线：`1caa6061b35ac378f1fdbd629198268f0d1c7dc2`（`v0.22.162`）。
- 旧唯一 Alembic head：`de39v8x9z28`。
- 新唯一 Alembic head：`df40v8x9z29`。
- 数据库：pytest 临时目录内匿名 SQLite；没有连接、复制、迁移或写入工厂正式数据库。

## 演练数据

隔离库只建立一组匿名事实：1 个客户、1 张有效送货单、1 条历史回单和 1 张历史对账单。历史回单日期为 `2026-06-14`，显式对账归属月份为 `NULL`；对账金额为 `280.80`。

## 往返步骤与结果

自动测试 `tests/test_p1_89_reconciliation_month_migration.py` 完成：

1. 在新模型空库建立匿名事实并 stamp 到 `df40v8x9z29`；
2. 降级到 `de39v8x9z28`，形成旧结构夹具；
3. 执行 `de39v8x9z28 → df40v8x9z29 → de39v8x9z28 → df40v8x9z29`；
4. 每次升级不为历史回单猜测或批量回填月份；
5. 最终回单数量、实际回单日期、对账单数量和对账金额均保持不变；
6. `PRAGMA integrity_check = ok`；
7. `PRAGMA foreign_key_check` 返回 0 条；
8. 最终 `alembic_version = df40v8x9z29`；
9. 另建隔离库写入显式月份事实后，降级按设计抛出 `RuntimeError`，不会静默丢失新事实。

执行结果：`2 passed in 16.10s`。

## 结论

P1-89 migration 满足历史空值兼容、旧新旧新往返、单 head、数量金额不变、完整性和有新事实时 fail-closed 的开发门禁。该结论只证明家庭隔离验证通过，不授权工厂正式迁移；工厂端仍须先备份并审计正式回单/对账来源，再在正式库副本重复演练并单独申请 Apply 授权。
