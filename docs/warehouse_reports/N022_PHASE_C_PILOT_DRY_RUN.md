# N022 Phase C.1 模具位置现场试盘只读 dry-run

## 边界

本工具仅供现场试盘前核对 `mold_tools.rack_location`。不提供 `apply`、迁移、API 或数据写回能力；不会读取、创建或映射三楼成品 `warehouse_locations`。数据库路径必须通过 `--database` 显式提供，脚本使用 SQLite `mode=ro` 后立即执行 `PRAGMA query_only=ON`。符号链接数据库路径会被拒绝。

请只将工具指向已确认隔离的副本。即使脚本只读，也不要把 JSON/CSV 输出写入正式库目录或以数据库文件作为输出路径。输出目标为符号链接，或路径包含 `live`、`prod`、`production`、`正式库`（以及当前正式 ERP 根目录）时会被拒绝。

## 使用

```powershell
python scripts/audit/mold_location_pilot.py `
  --database D:\tm-worktrees\uat-data\isolated_n022.sqlite3 `
  --json docs\warehouse_reports\n022_mold_location_dry_run.json `
  --csv docs\warehouse_reports\n022_mold_location_dry_run.csv
```

命令标准输出为稳定排序的汇总 JSON；可选 JSON 包含逐模具记录、差异和汇总，CSV 仅包含待复核差异。不会在数据库中创建临时表、写入日志或执行 `VACUUM`。

## 检查口径

- 合法平放：`3F-M-R02-L2-D03-P08`。
- 合法重型竖放：`3F-M-R01-L1-V-P12`。
- 空位置、旧自由文本和以 `3F-M` 开头但不符合规则的编码分别列为差异。
- 同一启用模具位置出现多个模具时列为 `duplicate_location_occupancy`。
- 同一模具编号重复列为 `duplicate_mold_code`；停用模具列为 `inactive_mold`，均只供现场复核。

位置比较会去除首尾空白并统一为大写；报告按固定字段和排序生成，重复执行的 JSON/CSV 内容可比较。

## 现场结论规则

先由现场人员复核差异，再由后续获授权的流程决定是否修正主数据。本 Phase C.1 只产出候选差异，不得把 `3F-M` 模具位置新增、更新或映射至三楼成品 `warehouse_locations`。
