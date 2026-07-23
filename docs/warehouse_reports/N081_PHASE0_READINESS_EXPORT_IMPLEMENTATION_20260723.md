# N081 Phase 0：盘点准备只读导出与模板

日期：2026-07-23

基线：`origin/factory-current-baseline@030c10150735de1aff9c429200858ee4f4a1bab4`

分支：`codex/n081-phase0-readiness-export-20260723`

## 1. 本阶段交付

- 新增只读导出器：`scripts/audit/n081_inventory_readiness.py`。
- 新增首次盘点 CSV 空模板：
  `docs/warehouse_reports/templates/N081_INITIAL_STOCKTAKE_TEMPLATE.csv`。
- 新增不含真实客户数据的脱敏样例：
  `docs/warehouse_reports/templates/N081_INITIAL_STOCKTAKE_SAMPLE_MASKED.csv`。
- 新增安全与数据质量测试：
  `tests/scripts/test_n081_inventory_readiness.py`。

本阶段不新增 API、页面、Alembic revision 或正式盘点确认能力，不创建、
调整、预占或抵扣任何库存。

## 2. 只读门禁

导出器：

1. 必须显式提供 SQLite 路径和报告输出目录。
2. 数据库通过 `mode=ro` 打开并强制 `PRAGMA query_only=ON`。
3. 没有 `--apply`、导入、修复或删除模式。
4. 扫描前后计算数据库大小和 SHA-256，并报告是否变化。
5. 执行 `PRAGMA quick_check` 和 `PRAGMA foreign_key_check`。
6. 缺少 N081 所需表或字段时拒绝继续。
7. 只把 JSON、CSV、Markdown 写入指定报告目录。

## 3. 工厂只读执行命令

工厂 Codex 必须先确认主机是 `PC-20250926DZYH`，再执行：

```powershell
Set-Location "D:\纸箱厂erp软件搭建"
$n081ReportDir = "D:\tm-readonly-audits\n081-phase0-20260723"
python .\scripts\audit\n081_inventory_readiness.py `
  --database "D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3" `
  --output-dir $n081ReportDir
```

执行时不停止 ERP、不迁移数据库、不创建备份、不修改网络或正式目录。
若扫描期间数据库文件变化，报告只标记为并发业务写入，不能据此认定审计
结果为稳定盘点基线，应选择停写窗口重新只读导出。

## 4. 输出

- `N081_INVENTORY_READINESS.md`：数量和问题摘要。
- `n081_inventory_readiness.json`：完整机器可读证据。
- `n081_locations.csv`：库位、楼层、区域、地图、当前栈板和正式批次数量。
- `n081_readiness_issues.csv`：未定位批次、未正式入库快照、缺失空间主数据等。

输出中的 `active_lot_without_pallet` 只表示正式库存尚未完成现场定位，不代表
库存数量错误；`pallet_snapshot_requires_review` 仍不是正式库存，不能直接
参与订单抵扣。

## 5. 家庭隔离演练

隔离 UAT 副本：

`D:\tm-uat\cutting_mode_visibility_20260723_113848\carton_erp_uat.sqlite3`

结果：

- revision：`cg63v8x9z52`
- `quick_check=ok`
- `foreign_key_check=0`
- 扫描前后大小：`219,447,296` 字节
- 扫描前后 SHA-256：
  `DA70483B5E32F3FCBD7176DAA8A934F04A36D8A4DD09C054467E32C8543E7F81`
- 数据库写入：否
- 定向测试：`2 passed`

家庭副本统计只验证导出器，不代表工厂当前库存事实。工厂 N081-A/B0 设计
必须以工厂主机重新生成的只读报告为准。
