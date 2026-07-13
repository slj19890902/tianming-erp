# N-027 Phase B 库存材料成本快照副本演练

日期：2026-07-13
分支：`feature/inventory-cost-snapshot-n027`
基线提交：`264aac3 feat: add read-only inventory insights dashboard`

## 1. 本阶段边界

- 只建立“当前材料估算”快照，不把它称为实际采购成本、生产成本或现金占用。
- 不修改订单、报料、送货、对账数量，不自动抵扣、少报、清理或报损。
- 正式数据库未迁移、未写入；所有迁移和回填只在隔离副本演练。
- Phase B 当前未提交、未 push，等待人工验收。

## 2. 估算口径

半成品单张估算：

```text
报料长(mm) × 报料宽(mm) ÷ 1,000,000 × 当前有效材料平方报价
```

成品单只估算：

```text
报料面积(m²) × 当前有效材料平方报价 × 每箱片数
```

A3 天地盖按“盖面积 + 底面积”各计算一次，不再额外乘片数。材料报价复用现有楞型加价规则。新建库存批次时保存估算单价、平方价、面积、来源明细和快照时间；以后材料报价变化不会静默改写旧批次快照。

## 3. 新增快照字段

`inventory_lots` 新增：

- `estimated_unit_cost_snapshot`
- `estimated_square_price_snapshot`
- `estimated_cost_area_m2_snapshot`
- `cost_snapshot_source`
- `cost_snapshot_detail_json`
- `cost_snapshot_at`

迁移：`ai36v7w8x9e26`，前置：`af33v7w8x9b23`。

注意：当前其他未集成分支也从 `af33v7w8x9b23` 派生了迁移。后续干净集成时必须显式处理 Alembic 多 head，不能直接把多个 sibling revision 当成线性迁移。

## 4. 数据库副本演练

源副本：

`D:\tm-worktrees\uat-data\carton_erp_inventory_insights_20260712_120310.sqlite3`

Phase B 最终安全演练副本：

`D:\tm-worktrees\uat-data\carton_erp_inventory_cost_snapshot_phase_b_safe_20260713_125615.sqlite3`

可复核指纹：

- 源副本 SHA-256：`6a5af0a67f706af8eaa7911dcd6a724de7f92cfc75201ae5d126fee8a9df89be`
- 最终演练副本 SHA-256：`a87b76443e99dd5e0d1cc19b4c8e9eb3b055b7a78e2125db8191e69b2360be7b`
- 最终 revision：`ai36v7w8x9e26`
- 最终 `PRAGMA integrity_check = ok`
- 最终 `PRAGMA foreign_key_check = 0`

迁移往返：

```text
af33v7w8x9b23
→ ai36v7w8x9e26
→ af33v7w8x9b23
→ ai36v7w8x9e26
```

每个阶段均为：

- `PRAGMA integrity_check = ok`
- `PRAGMA foreign_key_check = 0`
- 六个快照字段在 upgrade 后存在、downgrade 后消失、再次 upgrade 后恢复。

## 5. 回填演练

回填脚本默认使用 SQLite 真正只读连接执行 dry-run；apply 必须显式提供：

```text
--apply --confirm-copy COPY_ONLY --copy-root <批准的副本目录>
```

脚本在 dry-run 和 apply 两种模式下都拒绝文件名为 `carton_erp.sqlite3` 的数据库。apply 还会拒绝副本目录外路径、符号链接、多硬链接和已知 live database，并要求数据库 revision 精确等于 `ai36v7w8x9e26`。

dry-run 结果：

- 首次严格匹配候选 1 个
- 另外 1 个半成品测试批次的供应商快照为损坏的合成值 `N027?????`，因此保持“成本待补”，没有按同码材料静默混价
- 数据库指纹未变化

副本 apply 结果：

- 成品批次 `FG-20260712-0159721706`：面积 1.727200m²，平方价 1.8000，单只估算 3.1090。
- 为恢复原验收样本，只在 UAT 副本中把该合成测试行规范化为材质主档 `N7N / 昆山鸣朋 / material_id=78`；操作前副本备份为 `carton_erp_inventory_cost_snapshot_phase_b_safe_before_uat_normalize_20260713_125615.sqlite3`，源副本和正式库均未修改。
- 半成品批次 `SI-20260712-841B6098FC`：面积 0.960000m²，平方价 2.2800，单张估算 2.1888。
- 完整性 `ok`，外键异常 0。
- 再次 dry-run 候选 0、已存在快照跳过 2，证明回填具备幂等边界。

演练 JSON 证据保存在 `D:\tm-worktrees\uat-data\n027_phase_b_safe_*.json`。dry-run 的 SHA-256、mtime 和 journal mode 均保持不变。

## 6. 看板结果

验收服务：`http://127.0.0.1:18043/`

副本数据结果：

- 已录入批次：3
- 成品可用：20
- 半成品可用：30
- 当前材料估算：`¥127.84`
  - 成品：`¥62.18`
  - 半成品：`¥65.66`
- 有可用量批次：2
- 成本可估算：2
- 成本待补：0
- 成本覆盖：100%
- 实际现金占用：仍显示“成本待补”

## 7. 自动验证

- Phase B、库存、材质报价相关回归：`151 passed`
- 最小专项门禁：`32 passed`
- Alembic 真实往返迁移测试：`1 passed`（已包含在上述回归集合）
- Python 编译通过
- `git diff --check` 通过
- 未运行会写正式库的测试
- 未知/空计价单位不参与平方成本估算；A3 天地盖底片尺寸缺失、为零或不完整时保持“成本待补”，不退回普通箱算法。

## 8. 人工验收闸门

2026-07-13 用户已确认 Phase B 人工验收通过，并批准分组提交；该批准不包含正式库迁移或正式库回填。

人工验收只确认显示口径和安全边界：

1. “当前材料估算”显示 `¥127.84`。
2. “实际现金占用”仍显示“成本待补”。
3. 成本覆盖显示 100%，成本可估算 2、成本待补 0。
4. 行动队列中两条可用库存分别显示约 `¥62.18`、`¥65.66`。
5. 页面没有自动抵扣、自动少报、自动清理、自动报损或一键清理操作。
