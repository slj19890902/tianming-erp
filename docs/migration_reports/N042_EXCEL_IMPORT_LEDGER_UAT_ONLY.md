# N042 Excel 导入台账临时迁移说明（仅隔离 UAT）

> **禁止合并当前迁移。** 当前临时 revision 为 `n042tmpv8x9z51`，父节点是
> `ce61v8x9z50`。N041 集成后，必须重新分配 revision，并把
> `down_revision` 线性接到 `df62v8x9z51`，然后重新执行副本升级、降级、再升级演练。

## 目的

- 用 `客户 + 来源类型 + 文件 SHA-256` 唯一标识原始 Excel，不依赖客户 PO。
- 不可变保存规范化来源载荷和逐行快照。
- 用批次唯一约束和请求幂等键唯一约束阻止重复文件、无 PO 重复导入及并发双击。
- 保存正式订单前还要校验操作员、客户、解析版本、来源载荷和最终数量/价格签名。

## 当前边界

- 预览只写不可变来源审计台账，不创建正式订单。
- 正式订单只在签名确认后由原有 `POST /api/orders` 事务创建。
- 未写入、未迁移、未修改正式数据库。
- 当前迁移只能在备份并校验过的隔离 UAT 数据库副本演练。

## 回滚

无任何台账事实时可正常 downgrade；一旦产生批次、来源行或转单事实，迁移会阻止
破坏性 downgrade，必须恢复迁移前完整备份。

## 2026-07-20 隔离副本演练证据

- 演练目录：`D:\tm-worktrees\erp-xinzhen-excel-n042-v2\.tmp\n042_migration_20260720_215647`
- 目标仅为新建的隔离 SQLite，不是正式库或主目录数据库。
- 在执行临时迁移前，先将 `ce61v8x9z50` 状态在线备份为
  `n042_ce61_before_temp_migration.sqlite3`。
- 备份 SHA-256：`20A8BEEFAC4E8EFC9EA84819AFE1BCC311741B36A2441118CBA2CE213C91DC9E`。
- 源库与备份均为 `integrity_check=ok`、数据库对象数 362、revision=`ce61v8x9z50`。
- 已完成 `ce61v8x9z50 -> n042tmpv8x9z51 -> ce61v8x9z50 -> n042tmpv8x9z51`。
- 最终 `integrity_check=ok`、`foreign_key_check=0`；三张台账表和六个禁止
  UPDATE/DELETE 的不可变触发器均存在。
- 专项迁移测试覆盖：唯一来源约束、UPDATE/DELETE 拒绝、有事实时降级拒绝、
  空台账升级/降级/再升级。
