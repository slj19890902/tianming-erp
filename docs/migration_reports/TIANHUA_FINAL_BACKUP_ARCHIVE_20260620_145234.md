# 天华超净迁移后最终备份归档报告

## 1. 归档信息

- 归档时间：2026-06-20 14:52:34
- 本轮是否执行迁移 / apply / 删除 / 更新：否
- 正式数据库路径：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 最终备份路径：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_FINAL_AFTER_TIANHUA_MIGRATION_20260620_145234.sqlite3`
- 只读验收报告路径：`D:\纸箱厂erp软件搭建\docs\migration_reports\TIANHUA_FINAL_READONLY_ACCEPTANCE_20260620_142359.md`

## 2. 正式库归档前核对

- `sales_orders`：15,593
- `sales_order_items`：15,658
- `RUIDA-` 订单数：15,589
- 迁移台账订单数：15,589
- 迁移台账明细数：15,651
- `PRAGMA integrity_check`：`ok`
- `PRAGMA foreign_key_check`：0

结论：正式库状态与最终只读验收一致。

## 3. 最终备份文件

- 文件存在：是
- 文件大小：217,415,680 字节
- 修改时间：2026-06-20 14:53:22
- 备份方式：SQLite backup API

说明：本轮未覆盖旧备份，也未删除任何既有备份文件。

## 4. 备份后校验

### 4.1 备份库可用性

- 备份库可正常打开：是
- `PRAGMA integrity_check`：`ok`
- `PRAGMA foreign_key_check`：0

### 4.2 正式库 / 备份库数量对比

| 项目 | 正式库 | 备份库 |
| --- | ---: | ---: |
| `sales_orders` | 15,593 | 15,593 |
| `sales_order_items` | 15,658 | 15,658 |
| `RUIDA-` 订单数 | 15,589 | 15,589 |
| 迁移台账订单数 | 15,589 | 15,589 |
| 迁移台账明细数 | 15,651 | 15,651 |

结论：正式库与最终备份库核心数量完全一致。

## 5. SHA256 记录

- 正式库 SHA256：`162450A396E1CC4A439618084501E915D181201BA8C3849D0836C0AF50ECDCAE`
- 备份库 SHA256：`8A68F8856A26A236BFB284B28C82537A148000B408FB3982EAEE1F9FE47F33A0`

说明：

1. 两个 SHA256 已完整记录。
2. 正式库与备份库 SHA256 不同，但这不代表数据异常。
3. 备份使用 SQLite backup API，从运行中的正式库生成一致性快照；在页面布局、空闲页、文件级物理结构不完全相同的情况下，文件哈希可以不同。
4. 本次已通过数量、`integrity_check`、`foreign_key_check`、备份库可打开等方式确认逻辑数据一致。

## 6. rejected 行处理结论

人工审核文件中 rejected 共 2 行：

1. 第 32 行：`21301021 / 中性内箱26*45THH10`
2. 第 157 行：`21302061 / 满衬板24*36`

两行均为：

- `review_decision = create_product_later`
- `approved_product_id` 为空

结合最终只读验收结果，以上 2 个 rejected raw 款号均未进入正式明细；费用项也未进入正式明细。

## 7. 是否发现异常

未发现业务数据异常。

补充说明：

- 正式库在计算文件级 SHA256 时被其他进程占用，PowerShell `Get-FileHash` 无法直接读取，因此改用 Python 顺序读取完成哈希计算。
- 该情况不影响备份结果，也不影响数据库只读校验。

## 8. 结论与后续建议

结论：最终备份归档完成，备份文件有效，正式库与备份库核心数量一致，完整性检查正常，rejected 行处理结果正确。

后续建议：

1. 进入日常使用前的人工抽查。
2. 整理管理员、财务、文员、只读账号的密码与权限。
3. 整理操作规范、备份频率和恢复演练说明。
