# P1-147 仓库历史坐标归一受控复演手册

## 1. 本轮边界

- 候选权威方案：**已发布的实测区域几何 + 当前用户可见的百分比货位布局**。
- `floor3_location_layouts` 决定用户当前看见的货位中心；`warehouse_ground_layout_slots.x_mm/y_mm` 是地堆标准矩形左下角。
- 本任务只生成审计、现场抽样清单和受控修正候选；未取得现场抽样与老板确认前，不执行正式库修正。
- 不发布、丢弃或改写 `data/layout_drafts/twin_layout_v1.draft.json`，也不改写正式地图 JSON。
- 不修改库存数量、批次、栈板、占用关系、货位身份、地址版本或业务状态。

## 2. 输入门禁

必须同时取得同一次工厂导出的：

1. P0-35 / `v0.22.237` 以后最新正式数据库的隔离副本；
2. 该次正式运行态地图 `data/layout_runtime/twin_layout_v1.json`；
3. 导出清单中的数据库大小、SHA-256、Alembic revision、Git SHA；
4. 副本不得带有 `-wal` 或 `-shm` 文件，路径必须明确包含 `UAT`、`copy`、`replica`、`snapshot`、`rehearsal` 或 `P1-147` 隔离标记。

禁止把仓库根目录的 `data/carton_erp.sqlite3` 传给复演或修正命令。

## 3. 只读审计

```powershell
python scripts/admin/p1_147_warehouse_coordinate_normalization.py audit `
  --database "D:\P1-147-UAT\factory_snapshot.sqlite3" `
  --published-map "D:\P1-147-UAT\twin_layout_v1.json" `
  --output-dir "docs\migration_reports\P1-147-current-copy"
```

审计以 SQLite `mode=ro` + `query_only` 打开数据库，先执行 `integrity_check` 和 `foreign_key_check`，再输出：

- `p1_147_coordinate_audit.csv`：逐位置的楼层、区域、货位、当前毫米坐标、用户可见中心、候选左下角、偏差、批次、栈板和库存数量；
- `p1_147_field_samples.csv`：A/B/D/E、旋转区域和非矩形区域的分组抽样；
- `p1_147_zone_geometry.csv`：区域几何分类；
- `p1_147_coordinate_correction_plan.json`：源数据库/地图 SHA、逐行 CAS 事实和按 plan 重算的指纹；
- `p1_147_summary.md`：数量汇总和写入阻断。

当前代码基线自带的三楼正式地图 revision 为 `3994317ae14a7f18`，静态分类为 24 个轴对齐矩形、0 个旋转矩形、16 个非矩形。现场结论必须以工厂导出的运行态正式地图重新生成；不得用代码树静态数量替代。

## 4. 现场抽样与老板决策

现场至少核对：

- A、B、D、E 每个区域族的最大偏差位、边界位和有库存位；
- 每一个旋转区域（若运行态地图存在）；
- 每一种非矩形区域；
- 地图中心点、地面标识、系统货位名称、实际栈板/批次是否是同一位置。

老板需要在独立 JSON 中确认权威方案和已核对的几何类别。示例（不是批准）：

```json
{
  "task": "P1-147",
  "plan_sha256": "从审计输出原样复制",
  "authority": "published_measured_geometry_plus_visible_percent_layout",
  "field_sampling_status": "confirmed",
  "confirmed_geometry_classes": [
    "axis_aligned_rectangle",
    "non_rectangular"
  ],
  "approved_by": "老板姓名",
  "approved_at": "2026-09-02T20:00:00+08:00"
}
```

审计计划存在 `hard_apply_blockers` 时，批准文件不能绕过；必须重新导出或修复输入事实后重新审计。

## 5. 零持久写复演

复演会在一个事务中临时解除且随后恢复两条已核验的 UPDATE 保护触发器，逐行执行 CAS、写入审计、检查保护表摘要、完整性和外键，最后强制回滚整个事务。

```powershell
python scripts/admin/p1_147_warehouse_coordinate_normalization.py rehearse `
  --database "D:\P1-147-UAT\factory_snapshot.sqlite3" `
  --published-map "D:\P1-147-UAT\twin_layout_v1.json" `
  --plan "docs\migration_reports\P1-147-current-copy\p1_147_coordinate_correction_plan.json" `
  --actor-user-id 1 `
  --confirm-isolated-copy `
  --token "APPLY-P1-147-ISOLATED-COPY"
```

验收结果必须是 `rehearsed_and_rolled_back`，并再次确认：

- 坐标与 plan 指纹回到复演前状态；
- 两条保护触发器仍存在且定义未变；
- `inventory_lots`、`inventory_pallets`、`inventory_pallet_items`、占用表、`warehouse_locations`、`floor3_location_layouts` 的逐表逻辑 SHA 未变；
- `integrity_check=ok`、外键违规为 0。

## 6. 未来受控修正（本轮不执行）

只有老板完成第 4 节确认并另行批准正式迁移后，才允许工厂端 Codex 在再次复制出的隔离副本执行：

```powershell
python scripts/admin/p1_147_warehouse_coordinate_normalization.py apply `
  --database "D:\P1-147-UAT\factory_snapshot.sqlite3" `
  --published-map "D:\P1-147-UAT\twin_layout_v1.json" `
  --plan "docs\migration_reports\P1-147-current-copy\p1_147_coordinate_correction_plan.json" `
  --approval "D:\P1-147-UAT\P1-147-owner-approval.json" `
  --actor-user-id 1 `
  --backup-dir "D:\P1-147-UAT\backups" `
  --confirm-isolated-copy `
  --token "APPLY-P1-147-ISOLATED-COPY"
```

脚本在提交前自动创建并逐字节核验数据库备份，按 `slot + plan + layout + policy + map revision` 做 CAS；任一行失败则整批回滚。每个改动位置写一条 `operation_logs`，批次号由计划哈希和方向确定，重复执行返回 `idempotent_replay`。

隔离副本验收通过也不构成正式库写入授权。正式库必须另行备份、停服、复核路径并取得老板明确批准。

## 7. 回退

### 7.1 逻辑回退

对已经受控提交的隔离副本，可用同一计划将每一行 CAS 回原坐标；回退保留原修正审计并新增逐行回退审计，plan 版本继续递增：

```powershell
python scripts/admin/p1_147_warehouse_coordinate_normalization.py rollback `
  --database "D:\P1-147-UAT\factory_snapshot.sqlite3" `
  --published-map "D:\P1-147-UAT\twin_layout_v1.json" `
  --plan "docs\migration_reports\P1-147-current-copy\p1_147_coordinate_correction_plan.json" `
  --actor-user-id 1 `
  --backup-dir "D:\P1-147-UAT\rollback-backups" `
  --confirm-isolated-copy `
  --token "ROLLBACK-P1-147-ISOLATED-COPY"
```

### 7.2 完整文件回退

若逻辑回退无法满足验收，停止应用服务并由工厂端 Codex核对三个绝对路径：当前库、脚本生成的 `before_p1_147` 备份、恢复后的新副本。先对备份运行完整性/外键检查并复核 SHA，再恢复到一个新的临时文件，验证通过后才以可恢复方式替换目标隔离库。不得覆盖原始 BAK、主沙盘或仍在运行的数据库；正式库文件替换需要老板再次明确批准。

## 8. 前端地图验收

自动回归至少运行：

```powershell
python -m pytest `
  tests/scripts/test_p1_147_warehouse_coordinate_normalization.py `
  tests/test_p1_147_warehouse_coordinate_frontend_contract.py -q
```

Chrome 人工验收只连接隔离 UAT：三楼地图分别抽查 A/B/D/E、有库存位、非矩形区域及任何旋转区域，核对货位中心、名称、栈板、批次、数量和详情一致；刷新后位置不跳动。不得使用内置浏览器，也不得发布或放弃现存三楼地图草稿。
