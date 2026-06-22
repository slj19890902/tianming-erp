# 备份保留策略与常规备份清理报告

执行时间：2026-06-20 19:44:30

## 本轮边界

- 本轮是否执行历史迁移：否
- 本轮是否修改历史订单：否
- 本轮是否修改主库：否
- 未删除主库
- 未删除 `data/sandboxes`
- 未删除迁移报告
- 未删除受保护迁移备份

## 新增能力

### 1. 共享备份保留策略

新增：

- `app/core/backup_retention.py`

规则：

- 默认只保留最近 5 个常规 `.sqlite3` 备份
- 保护备份不纳入这 5 个名额
- 保护关键词：
  - `FINAL`
  - `ARCHIVE`
  - `MIGRATION`
  - `tianhua_batch`
  - `tianhua_sales_apply`
  - `legacy_refresh`

### 2. 管理脚本

新增：

- `scripts/admin/manage_backups.py`

支持：

- `list`
- `cleanup`

默认行为：

- 默认不删除
- 只有显式 `--apply` 才删除

### 3. 自动清理接入

已接入：

- `app/core/database.py` 中的 `backup_to_nas()`
- `scripts/admin/final_password_handoff.py` 中的本地备份函数

效果：

- 新常规备份成功后，自动尝试把常规备份收敛到最近 5 个
- 新备份失败时，不会触发清理
- 清理失败不会回滚新备份，但会在结果中返回错误

## Dry-run 结果

执行命令：

```powershell
.\.venv\Scripts\python.exe .\scripts\admin\manage_backups.py cleanup --backup-dir .\data\backups --keep 5
```

Dry-run 统计：

- 当前 `.sqlite3` 备份总数：44
- 常规备份数量：33
- 保护备份数量：11
- 非 `.sqlite3` / 非备份目录项：8（均为目录，不参与删除）
- Dry-run 计划删除常规备份：28
- Dry-run 预计释放空间：5,996,277,760 bytes（5718.50 MB）

Dry-run 明确确认：

- 所有保护备份均不在删除清单内
- 未发现需要人工判断的 `.sqlite3` 无法分类文件

## Apply 结果

执行命令：

```powershell
.\.venv\Scripts\python.exe .\scripts\admin\manage_backups.py cleanup --backup-dir .\data\backups --keep 5 --apply
```

实际结果：

- 是否执行 apply 清理：是
- 实际删除文件数量：28
- 实际释放空间：5,996,277,760 bytes（5718.50 MB）
- 清理后 `.sqlite3` 备份总数：16
- 清理后常规备份数量：5
- 清理后保护备份数量：11
- 清理后常规备份是否 ≤ 5：是
- 保护备份是否全部保留：是

当前保留的 5 个常规备份：

1. `carton_erp_20260620_172155_035693_before_account_rbac_hardening.sqlite3`
2. `carton_erp_20260620_172137_848773_before_account_rbac_hardening.sqlite3`
3. `carton_erp_20260620_172103_638288_before_account_rbac_hardening.sqlite3`
4. `carton_erp_20260620_171932_344878_before_account_rbac_hardening.sqlite3`
5. `carton_erp_before_onsite_password_handoff_20260620_165210.sqlite3`

当前保留的 11 个保护备份：

- `carton_erp_FINAL_AFTER_TIANHUA_MIGRATION_20260620_145234.sqlite3`
- `carton_erp_before_tianhua_batch_5_apply_20260620_134925.sqlite3`
- `carton_erp_before_tianhua_batch_4_apply_20260620_130056.sqlite3`
- `carton_erp_before_tianhua_batch_3_apply_20260620_085704.sqlite3`
- `carton_erp_before_tianhua_batch_2_apply_20260620_074551.sqlite3`
- `carton_erp_before_tianhua_sales_apply_20260619_202414.sqlite3`
- `carton_erp_before_tianhua_batch_1_apply_20260619_203207.sqlite3`
- `carton_erp_before_tianhua_sales_apply_20260619_202543.sqlite3`
- `carton_erp_before_legacy_refresh_20260619_085956.sqlite3`
- `carton_erp_before_legacy_refresh_20260619_090738.sqlite3`
- `carton_erp_before_legacy_refresh_apply_20260619_090705.sqlite3`

## 主库状态

正式数据库：

`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

只读校验：

- 主库仍存在：是
- `integrity_check = ok`
- `sales_orders = 15593`
- `sales_order_items = 15658`
- 历史订单数（旧前缀原始值）= 15589

## 测试结果

已通过：

```text
15 passed
```

覆盖点包括：

- 默认 `keep = 5`
- dry-run 不删除文件
- apply 只删除超额常规备份
- 保护备份不会被删除
- 保护关键词识别
- 新备份成功后的自动清理
- 新备份失败时不清理旧备份
- 主库不在清理范围内

## 结论

1. 常规备份已收敛到最近 5 个。
2. 关键迁移节点与最终归档备份全部保留。
3. 主库未被修改，完整性正常。
4. 以后所有走共享备份入口的新常规备份，会自动执行同样的保留策略。

## 后续建议

1. 保护备份不要自动删；如果后续真要删除，必须人工确认。
2. 日常管理员只需要关心“最近 5 个常规备份 + 所有关键迁移备份”。
3. 如需扩展为“日备/周备/月备”多层策略，再单独设计，不要直接在当前规则上混改。
