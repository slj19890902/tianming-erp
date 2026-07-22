# 历史材质派生规范化（家里 UAT）

此工具只从 `historical_requisition_maps` 读取原始历史文本，并生成独立 CSV。
它永远不会更新原表的 `material_code`，也不会猜测缺失的楞型。

## 可自动派生的结果

仅当原文本已经同时含有合法的基码和楞型时，工具才生成 `format_safe` 结果，
例如 `BC14C-AB楞` → `BC14C/AB`。这只是清理分隔符、空白和后缀。

以下情况一律没有派生材质值：

- 五层 `K618A/B楞`：`pending_invalid_layer_flute`；
- 复合 `K618A/AB/BE楞`：`pending_ambiguous`；
- 只有 `K618A`：`pending_missing_flute`。

## 家里电脑运行方式

先使用工厂生成的 SQLite Backup API 副本，确认副本 SHA-256 与交接值一致；不得连接正式库。

```powershell
py -3.10 -X utf8 scripts\admin\derive_historical_material_normalizations.py `
  --database D:\tm-uat\factory_latest\carton_erp_uat.sqlite3 `
  --output-dir D:\tm-uat\factory_latest\historical-material-reports
```

输出 JSON 会记录数据库前后 SHA-256、大小、mtime、完整性、外键检查和各状态数量；
前后指纹不一致时工具拒绝发布结果。

该工具是家里 UAT 的安全第一步。若以后需要让 ERP 页面使用派生结果，必须另行设计
追加式结果表、迁移、隔离库演练和正式发布授权；禁止把 CSV 的值回写到原始档案列。
