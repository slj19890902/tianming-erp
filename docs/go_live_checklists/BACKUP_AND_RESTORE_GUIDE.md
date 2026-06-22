# 备份与恢复操作说明

更新时间：2026-06-20

正式库：

`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

当前最终归档备份：

`D:\纸箱厂erp软件搭建\data\backups\carton_erp_FINAL_AFTER_TIANHUA_MIGRATION_20260620_145234.sqlite3`

## 一、每天什么时候备份

建议：

1. 每天下班前备份 1 次
2. 如果当天录单很多，中午再补 1 次
3. 做大改前先手工备份 1 次

## 二、每周如何保留

- 最近 7 天每天 1 份
- 每周额外保留 1 份周备份

## 三、每月如何归档

- 保留月末最后一天备份
- 当月有重要调整时，再多留 1 份关键节点备份

## 四、建议命名规则

`carton_erp_用途_YYYYMMDD_HHMMSS.sqlite3`

示例：

- `carton_erp_daily_20260620_180000.sqlite3`
- `carton_erp_weekly_20260621_180000.sqlite3`
- `carton_erp_month_end_20260630_180000.sqlite3`

## 五、出现误删/误改时怎么恢复

先做 4 件事：

1. 先停止 ERP 使用
2. 不要继续录新单
3. 先保留当前出问题的库
4. 选最接近出问题前的备份

## 六、恢复前必须先做什么

1. 复制当前正式库，保留现场
2. 确认要恢复的备份时间点
3. 确认恢复后会丢失哪段新数据
4. 恢复前通知使用人员先停操作

## 七、哪些文件绝对不能删除

1. `data/carton_erp.sqlite3`
2. `data/backups` 下正式备份
3. `carton_erp_FINAL_AFTER_TIANHUA_MIGRATION_20260620_145234.sqlite3`
4. `.env`
5. `data/session_secret.key`

## 八、怎么判断恢复成功

恢复后至少检查：

- 能正常打开系统
- 能正常登录
- 订单列表能打开
- 客户列表能打开
- 最近几张关键订单还能查到
- 页面没有明显报错

## 九、什么时候联系技术人员

出现以下情况就联系：

1. 系统打不开
2. 登录失败
3. 数据库文件打不开
4. 恢复后订单数量明显不对
5. 不确定该用哪份备份

## 十、现在的备份保留规则

从 2026-06-20 起，系统常规备份默认只保留最近 5 个。

说明：

1. 这里只清理“常规备份”。
2. 关键迁移备份、最终归档备份、历史审计备份不会自动删除。
3. 下列关键词命中的备份默认受保护：
   - `FINAL`
   - `ARCHIVE`
   - `MIGRATION`
   - `tianhua_batch`
   - `tianhua_sales_apply`
   - `legacy_refresh`

## 十一、手工查看和清理备份

只读查看：

```powershell
.\.venv\Scripts\python.exe .\scripts\admin\manage_backups.py list --backup-dir .\data\backups --keep 5
```

先看 dry-run：

```powershell
.\.venv\Scripts\python.exe .\scripts\admin\manage_backups.py cleanup --backup-dir .\data\backups --keep 5
```

确认后才真正清理：

```powershell
.\.venv\Scripts\python.exe .\scripts\admin\manage_backups.py cleanup --backup-dir .\data\backups --keep 5 --apply
```

注意：

- 不要手工删除保护备份。
- 真要删除保护备份，必须人工确认后单独处理。
