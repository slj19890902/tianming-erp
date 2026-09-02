# P1-147 仓库历史坐标归一受控复演手册

## 1. 本轮边界

- 候选权威方案：**已发布的实测区域几何 + 当前用户可见的百分比货位布局**。
- P1-147 固定为 **3F 专项**。脚本必须如实报告全仓输入数量，但只有数据库楼层码为 `3F` 的已发布货位可以进入差异审计和候选执行集。
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
  --output-dir "docs\migration_reports\P1-147-current-copy" `
  --scope-floor 3F
```

审计以 SQLite `mode=ro` + `query_only` 打开数据库，先执行 `integrity_check` 和 `foreign_key_check`，再输出：

- `p1_147_coordinate_audit.csv`：逐位置的楼层、区域、货位、当前毫米坐标、用户可见中心、候选左下角、偏差、批次、栈板和库存数量；
- `p1_147_rehearsal_candidates.csv`：只含 3F 轴对齐、不越界且超过门槛的受控复演候选；
- `p1_147_field_samples.csv`：A/B/D/E 抽样、全部旋转位置、全部不越界非矩形位置，以及全部几何阻断位置；
- `p1_147_out_of_scope_findings.csv`：1F/4F 等范围外问题，只作独立闭环证据，绝不进入 P1-147 执行集；
- `p1_147_zone_geometry.csv`：区域几何分类；
- `p1_147_coordinate_correction_plan.json`：全仓输入分母、3F 专项分母、逐行候选状态、源数据库/3F 地图哈希和逐行 CAS 事实；
- `p1_147_summary.md`：数量汇总和写入阻断。

计划 schema v2 将货位分为四类：`rehearsal_candidate`、`field_confirmation_required`、`geometry_blocked`、`unchanged`。只有第一类可被 rehearse/apply/rollback 更新。旧 schema v1 计划因统计作用域不明确而永久拒绝执行。

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
  "scope_floor_code": "3F",
  "approved_candidate_count": 240,
  "candidate_location_ids_sha256": "从计划 execution_candidate 原样复制",
  "confirmed_geometry_classes": [
    "axis_aligned_rectangle"
  ],
  "approved_by": "老板姓名",
  "approved_at": "2026-09-02T20:00:00+08:00"
}
```

审计计划存在 `hard_apply_blockers` 时，批准文件不能绕过；必须重新导出或修复输入事实后重新审计。

## 5. 零持久写复演

复演会在一个事务中临时解除且随后恢复 `warehouse_ground_layout_slots` 的一条已核验 UPDATE 保护触发器，逐行执行 CAS、写入审计、检查保护表摘要、完整性和外键，最后强制回滚整个事务。正式规划的不可变触发器始终保留，规划 version/fingerprint 不会被修改。

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

- 所有 3F 坐标、数据库文件 SHA 和操作日志回到复演前状态；
- 地堆货位 UPDATE 保护触发器仍存在且定义未变，正式规划不可变触发器从未解除；
- `inventory_lots`、栈板/占用表、楼层、区域、策略、`warehouse_locations`、`floor3_location_layouts` 和正式规划的逐表逻辑 SHA 未变；
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

脚本在提交前自动创建并逐字节核验数据库备份，按 `slot + location + layout + area + plan version/fingerprint + policy + 3F map revision/hash` 做 CAS；任一行失败则整批回滚。只更新候选行的 `x_mm/y_mm`，不修改 plan version/fingerprint。每个改动位置写一条 `operation_logs`，批次号由计划哈希和方向确定，重复执行返回 `idempotent_replay`。

隔离副本验收通过也不构成正式库写入授权。正式库必须另行备份、停服、复核路径并取得老板明确批准。

## 7. 回退

### 7.1 逻辑回退

对已经受控提交的隔离副本，可用同一计划将每一行 CAS 回原坐标；回退保留原修正审计并新增逐行回退审计，正式规划 version/fingerprint 始终不变：

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

没有经批准的持久修正副本时，不做“修正后 Chrome 验收”；静态前端坐标换算回归和零持久复演通过不能冒充现场验收。

## 9. 2026-09-02 scope v2 隔离复演记录

- 已验签输入：数据库 SHA-256 `2f9c26c6d9af061e42790dd098e66dc2c2daf97286f3fd6b787c702711dd0577`，正式地图 SHA-256 `8a9b9c86a83a3fd563a9bb4771a8ccd4dc3a8823b05b5c28dafe1f1e055fac69`。
- 全仓输入 342 / 23；3F 专项 308 / 20；差异 306、门槛内 2。
- 当前执行集 240（占用 217）；不越界非矩形 22（占用 0）等待现场确认；几何阻断 44（占用 14）被排除。
- 范围外只读发现：`F1/1F` 别名 10（占用 10），4F plan revision 落后 24（占用 0）。
- 240 行零持久复演连续两次通过，事务回滚后数据库 SHA 不变；CAS 冲突副本整批拒绝，坐标和操作日志不变；完整性 `ok`、外键异常 0。
- 未生成批准文件，未执行持久 apply/rollback，未进行 Chrome 修正后验收。
- 本次输入在导出时是最新正式快照；之后工厂正式库已由 `jl70v8x9z59` 升至 `jm71v8x9z60`。发布回执证明该迁移未自动改写仓库、库存或地图事实，但任何持久隔离 UAT、Chrome 修正验收或正式迁移仍须取得 `jm71v8x9z60` 之后的新同批数据库/地图快照并重新生成计划。
- 同日另有“P1-147 送货取消”正式发布回执，存在任务号复用。正式批准前必须由老板确定唯一任务号；批准文件中的 `task` 不得在编号冲突未解除时直接复用。

## 10. 2026-09-02 老板持续授权

老板于 23:26 明确回复“明确授权 无需另外授权”。执行口径如下：

1. 候选权威固定为“已发布实测区域几何 + 当前用户可见百分比货位布局”，差异门槛固定为 `0.5 mm`。
2. 当前授权执行集上限为 240 个 3F、轴对齐且不越界的候选；最终数量和 location ID 集仍须由最新快照重新审计并通过 CAS，不能因上限授权而多写一行。
3. 最新正式副本、同批正式地图、现场抽样事实、备份、计划哈希、CAS、逐行审计、事务、完整性、外键和回退均通过后，可直接完成持久隔离 UAT、Chrome 修正验收，并由工厂端正式执行，不再向老板重复请求授权；家庭端仍不得写或替换工厂正式库。
4. 计划专属 `--approval` JSON 是把本授权机械绑定到最新 plan SHA、候选数量、location ID 哈希和现场回执的执行清单，不构成新的审批；执行端可以在门禁满足后生成。
5. 当前授权不包含 22 个非矩形现场确认位置、44 个几何阻断位置、1F `F1/1F` 或 4F revision 问题；这些行继续 fail-closed。
6. 授权不能替代尚未发生的现场抽样，也不能使用 20:41 / `jl70v8x9z59` 旧快照直接修改后来已升至 `jm71v8x9z60` 的正式库。
