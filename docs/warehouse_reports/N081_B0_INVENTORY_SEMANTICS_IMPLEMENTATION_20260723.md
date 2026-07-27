# N081-B0 库存语义实施与隔离迁移报告

日期：2026-07-23

开发分支：`codex/n081-b0-inventory-semantics-20260723`

开发起点：
`codex/p0-production-surplus-rebase-20260723@f11d616f419b0bbf9ea47bfa68b41cfd9aedb53e`

## 1. 完成范围

- 冻结既有 `source_type`、成品/半成品、客户专用/通用库存口径。
- 新增 `stock_date_accuracy=exact/estimated/unknown` 和日期原文。
- 历史库存迁移为 `unknown`，不把旧日期擅自认定为精确事实。
- 手工入库明确记录精确日期；估算/不明日期可由后续 N081-B1 显式传入。
- 库存列表和库存洞察对不明日期返回 `age_days=null`，不生成精确超期判断。
- 所有 SQL 与 Python 内存 FIFO 路径均把未知技术日期排在精确/估算日期之后。
- 只有修改日期或显式勾选确认，才会把既有技术日期记为精确日期；该意图进入
  幂等指纹、库存流水和审计明细。
- 本阶段没有创建盘点导入批次，也没有提供正式入账入口。

## 2. 迁移

迁移链：

```text
ch64v8x9z53
→ ci65v8x9z54
→ cj66v8x9z55
```

`cj66` 使用 SQLite 原地 `ADD COLUMN`，不批量重建
`inventory_lots`。降级只在全部记录仍是迁移回填的
`unknown + 原文为空` 时允许；一旦形成精确/估算/原文事实即 fail-closed。

## 3. 隔离副本演练

只读源副本：

```text
D:\tm-uat\p0_production_surplus_rebase_20260723\source_verified\
carton_erp_ch64_source_verified.sqlite3
```

- 大小：`219,447,296` 字节
- SHA-256：
  `89EF26FD9782506CAFD1BDDD5D832096AD330686DE1E8741A9CAF5473621A816`
- 初始 revision：`ch64v8x9z53`
- `integrity_check=ok`
- 外键异常：`0`

迁移工作副本：

```text
D:\tm-uat\n081_b0_inventory_semantics_20260723\
carton_erp_ch64_to_cj66_rehearsal.sqlite3
```

该文件先逐字节复制并复核 SHA-256 相同，随后完成：

```text
ch64 → ci65 → cj66 → ci65 → cj66
```

最终结果：

- revision：`cj66v8x9z55`
- `integrity_check=ok`
- `quick_check=ok`
- 外键异常：`0`
- 当前迁移定义的 insert/update 两个日期来源门禁触发器均存在
- 110 条既有库存：全部 `stock_date_accuracy=unknown`
- 核心业务表行数全部不变
- 库存汇总不变：
  - 可用：`30,983`
  - 预占：`1,039`
  - 已消耗：`0`
  - 损坏：`0`
  - 报废：`100`
- 演练副本最终 SHA-256：
  `A60437AA761300CB9DF006E27B66B5790C26039AFC7F2BA49EF0435DDF61B6D4`
- 只读源副本 SHA-256 复核仍未变化。

## 4. 自动验证

定向语义、迁移、前端、库存洞察、仓库基础和成本权限回归：

```text
56 passed
```

另通过：

- 成品库存候选接口集成测试：`1 passed`
- 库存抵扣、生产和送货相邻回归：`129 passed, 1 failed`；唯一失败在未修改
  的 `f11d616` 基线上原样复现，为既有超送测试夹具漂移，不是 B0 回归
- Python 编译
- 内联 JavaScript 语法
- Alembic 单 head / 线性父 revision
- `git diff --check`

旧 `tests/test_floor3_locations_frontend.py` 仍有与本轮无关的历史断言漂移；
B0 自身新增的日期空值和精确日期前端契约测试均通过。该历史测试不得通过
降低现有已验收页面功能来“修绿”。

## 5. 数据边界

- 未连接、迁移或写入工厂正式数据库。
- 未修改工厂正式目录或 `origin/main`。
- 仅迁移 `D:\tm-uat` 内由已核验源副本再次复制得到的工作副本。
- B1 dry-run 完成前不得创建正式库存；B2 只允许隔离小范围试盘。
