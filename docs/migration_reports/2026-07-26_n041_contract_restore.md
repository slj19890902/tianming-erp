# N041 客户合同能力恢复迁移报告

## 基线与目标

- 代码基线：`7792f6f246a1ef8428beebebd0d8075e91ee9f4c`
- 原已验收实现：`9cce9fa21a6c6eecc8275326485dcaf1b49071e8`
- 起始 revision：`co71v8x9z60`
- 目标 revision：`cp72v8x9z61`
- `cp72.down_revision = co71v8x9z60`
- Alembic head：唯一 `cp72v8x9z61`

原迁移 `df62v8x9z51` 依赖旧 `ce61`，未直接复制到当前迁移链。

## 隔离副本

- 只读源：
  `D:\tm-uat\composite-order-overrides-20260725\carton_erp_uat.sqlite3`
- 源 SHA-256：
  `BB472EA228C7279DA1224FA4BBD80B10DE72F112FEBBB82F6DECC69FE45B74FA`
- 演练再次复制件：
  `D:\tm-uat\n041-contract-restore-20260726\carton_erp_cp72_migration_rehearsal.sqlite3`
- 最终大小：`1,589,248` 字节
- 最终 SHA-256：
  `62CF6E1A55340342002EA9CB8421694FA41259D542A22F1487DADF91171D56C3`

## 往返结果

| 阶段 | revision | integrity_check | foreign_key_check |
|---|---|---|---:|
| 复制前 | `co71v8x9z60` | `ok` | 0 |
| 第一次升级 | `cp72v8x9z61` | `ok` | 0 |
| 安全降级 | `co71v8x9z60` | `ok` | 0 |
| 第二次升级 | `cp72v8x9z61` | `ok` | 0 |

## Fail-closed 探针

在 `cp72` 再次复制件中插入一条匿名合同草稿后执行降级：

- 降级返回非零并提示合同事实存在；
- `alembic_version` 保持 `cp72v8x9z61`；
- 合同事实仍为 1 条；
- `integrity_check=ok`；
- 外键异常 0。

自动迁移回归还分别模拟：

- 合同草稿删除后只剩 `contract_daily_sequences` 编号事实；
- 合同草稿删除后只剩 `operation_logs.entity_type='customer_contract'` 审计事实。

两种情况均在任何 DDL 前拒绝降级，并保持 `cp72v8x9z61`、完整性 `ok`、外键异常 0。

## 数据边界

- 未连接、迁移或写入工厂正式数据库。
- 所有迁移与匿名测试事实仅存在于 `D:\tm-uat` 隔离复制件。
- 正式发布仍须由工厂 Codex 重新备份、验证并在正式备份副本演练后取得授权。
