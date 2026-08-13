# P1-54 正式库结构迁移提前执行事件记录

## 事件结论

2026-08-13 约 11:52（Asia/Shanghai），在已取得 P1-54 正式迁移授权后、但尚未完成规定的停服和发布脚本 Prepare 之前，一次原计划指向临时副本的 Alembic 演练错误连接到了正式库，将正式库结构从 `kk19v8x9z08` 提前升级到 `ll20v8x9z09`。

这是发布顺序违规，必须保留完整证据。不能因为最终目标 revision 与已授权发布一致，就把它记录成正常 Prepare/Apply。

## 根因

演练代码通过 Alembic `Config.set_main_option("sqlalchemy.url", <临时副本>)` 指定副本；但本项目 `alembic/env.py` 会重新读取正式运行配置并覆盖该 URL，导致 `command.upgrade(..., "ll20v8x9z09")` 实际作用于正式库。

Alembic 原始关键输出：

```text
Context impl SQLiteImpl.
Will assume non-transactional DDL.
Running upgrade kk19v8x9z08 -> ll20v8x9z09, append-only external-packaging purchase cancellation facts
```

临时副本随后查询新表返回 `sqlite3.OperationalError: no such table: external_packaging_purchase_cancellations`，由此确认迁移没有落在副本。

## 已确认影响

- 事故前正式库 revision：`kk19v8x9z08`。
- 事故前正式库 SHA-256：`500ebe12c28ec1b0f78386a18251372a5943d8b35d6c010803ca7d8382ba5256`。
- 事故后正式库 revision：`ll20v8x9z09`。
- 事故后正式库 SHA-256：`935f46b50753c2f412edf6849d4ba10b82528b28810724a6c4659131dec112c8`。
- 事故后 `integrity_check=ok`，`foreign_key_check=0`。
- 新取消事实表存在，行数为 0；授权的历史单条修复 INSERT 确定没有执行。
- 核心业务计数保持：订单 103、订单明细 401、外购批次 2、外购采购单 2、外购采购明细 5、外购收料单 0、外购收料明细 0、操作日志 6598。
- 精确现场异常仍为订单 `TM20260813001`（id 9657，`cancelled`）与采购单 `EP-20260813-001`（id 2，`confirmed`），采购明细 1、累计实收 0。

`ll20` migration 本身是 schema-only，只建立表、约束、索引和不可变触发器，没有历史业务 DML；因此当前证据表明业务数据未被此次提前迁移改写。

## 立即处置

1. 发现后暂停所有后续正式发布动作，要求发起演练的审查代理停止工具调用。
2. 独立核验并停止唯一正式 ERP 单 worker（PID 16604）；端口 8000 已停止监听。
3. 在停服状态以 SQLite Backup API 创建事故现场备份：
   `data/backups/carton_erp_20260813_115539_635247_pre_update.sqlite3`。
4. 事故现场备份大小 219,447,296 字节，SHA-256
   `73bbb054b20aafd9c0d8541feb3ecb3399a4a6c1b0610c795f2a6fd12b9b08b0`，`integrity_check=ok`。
5. 未执行降级、手改 `alembic_version`、删除空表或补写历史取消事实。

## 后续发布原则

- 老板已明确授权目标 revision `ll20v8x9z09` 和精确单条历史修复，因此不为恢复流程外观而降级；正式服务保持停机，直到代码、版本元数据、数据库 schema 和发布门禁重新一致。
- 后续仍需在干净的 `factory-current-baseline` 上运行两阶段发布脚本。Prepare 必须重新创建并核验正式备份与隔离演练副本；报告会如实显示 source 已经是 `ll20`。
- 精确历史单条修复必须在发布脚本完成、服务仍受控时使用独立事务执行；必须断言精确身份、零实收、审计证据、`changes()=1`，否则回滚。
- 今后禁止用只设置 `alembic.ini sqlalchemy.url` 的方式演练本项目迁移。数据库副本测试必须显式隔离整个运行配置，并在迁移前后独立核对“实际连接路径”。
