# 历史报料材质正式库清洗运行手册

本手册只允许在工厂主机 `PC-20250926DZYH` 上执行，目标数据库只能是：

`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

规范结果：

- 三层：`3 位字母数字/A|B|E`，例如 `A6A/B`。
- 五层：`5 位字母数字/AB|BE`，例如 `K618A/AB`。
- 七层：只保留已经合法的 `7 位字母数字/AAA|ABC`；非法七层记录必须人工审核。
- `K618A/B楞`、`K618A/AB/BE楞` 等值只有在关联常用箱或逐行人工审批能唯一证明楞型时才允许修改，禁止按多数值猜测。

## 一、执行前门禁

由工厂 Codex 逐项核对并保存终端输出：

```powershell
$cleanupRoot = 'D:\tm-worktrees\erp-historical-material-cleanup-20260722'
$formalDb = 'D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3'
$reportRoot = 'D:\纸箱厂erp软件搭建\docs\migration_reports\historical-material-cleanup'
$backupRoot = 'D:\纸箱厂erp软件搭建\backups\historical-material-cleanup'

if ($env:COMPUTERNAME -ne 'PC-20250926DZYH') { throw '不是工厂正式主机，禁止继续' }
if (-not (Test-Path -LiteralPath $formalDb -PathType Leaf)) { throw '正式数据库不存在，禁止继续' }
Get-Item -LiteralPath $formalDb | Format-List FullName,Length,LastWriteTime
Get-FileHash -Algorithm SHA256 -LiteralPath $formalDb
```

在隔离 worktree 检出 PR 分支，不切换正式运行目录的分支。确认工具所在提交来自 `factory-current-baseline@c5cffa2123973b3f30b74be7f6dc198d1f070210` 之后，再停止 ERP。停止后必须满足：

```powershell
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw '8000 端口仍在监听，ERP 尚未停止，禁止继续'
}
```

不要删除任何 `-wal`、`-shm` 或 `-journal` 文件。若脚本报告存在 sidecar 或数据库被占用，保持停机并排查原因。

## 二、只读 dry-run

先处理当前权威表：

```powershell
python -X utf8 "$cleanupRoot\scripts\admin\normalize_historical_requisition_materials.py" `
  --database $formalDb `
  --output-dir "$reportRoot\purchase-dry-run" `
  --table historical_purchase_entries
```

如果正式库中仍存在旧表，再单独处理旧表：

```powershell
python -X utf8 "$cleanupRoot\scripts\admin\normalize_historical_requisition_materials.py" `
  --database $formalDb `
  --output-dir "$reportRoot\legacy-map-dry-run" `
  --table historical_requisition_maps
```

每次 dry-run 后必须核对：

- JSON/Markdown 中 `integrity_check=ok`、`foreign_key_errors=0`。
- dry-run 前后数据库 SHA-256、大小、mtime 完全不变。
- `*_REVIEW.csv` 中每一行都有来源、产品和旧材质上下文。
- 任何无法唯一确定 AB/BE 或 A/B/E 的记录保持 `pending`，不得直接 Apply。

## 三、人工审批

复制本轮 `*_REVIEW.csv` 为独立审批文件。每一行至少填写：

- `approved_material_code`：符合规范的唯一结果。
- `review_status`：精确填写 `approved`。
- `reviewer`：真实审核人。
- `reviewed_at`：真实审核时间。
- `authority_override`：只有人工确认的历史事实与当前常用箱冲突时才填写 `approved/manual`。

不得修改 `table_name`、`row_id` 或 `expected_material_code`。审批后重新执行同一张表的 dry-run，并增加：

```powershell
--approved-mapping '审批 CSV 的绝对路径'
```

只有新的 JSON 同时满足 `needs_review=0`，且计划数量、逐行旧值和新值已经人工签字确认，才允许申请正式 Apply。

## 四、正式 Apply

每张表分开执行；每次都重新读取当时的正式库 SHA-256：

```powershell
$expectedSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $formalDb).Hash

python -X utf8 "$cleanupRoot\scripts\admin\normalize_historical_requisition_materials.py" `
  --database $formalDb `
  --output-dir "$reportRoot\purchase-apply" `
  --table historical_purchase_entries `
  --approved-mapping '审批 CSV 的绝对路径' `
  --apply `
  --confirm-apply APPLY_HISTORICAL_MATERIAL_NORMALIZATION `
  --confirm-service-stopped ERP_STOPPED_AND_DATABASE_UNLOCKED `
  --expected-sha256 $expectedSha `
  --backup-dir $backupRoot
```

旧表如存在，必须在第一张表验收完成后重新计算 SHA-256，再使用 `--table historical_requisition_maps` 独立执行。不要复用上一张表的 SHA-256、报告或审批文件。

## 五、验收与失败处置

Apply 后先保持 ERP 停止，再执行不带审批文件的同表 dry-run。验收条件：

- `planned_updates=0`。
- `needs_review=0`。
- `integrity_check=ok`。
- `foreign_key_errors=0`。
- Apply 报告中的备份路径、备份 SHA-256、执行前后 SHA-256 均存在。
- 报告目录不存在本轮残留的 `.pending` 文件、`INVALIDATED_APPLY_REPORT` 或 `.INVALIDATED.json`。
- 随机抽查三层、五层以及事故样本 `BC14C/A -> BC14C/AB` 的证据链。
- 使用 `c5cffa2` 修复后的 PDF 保存流程回归事故订单，确认不再出现历史楞型错误提示。

若出现 `EMERGENCY_RESTORE_FAILED`、`EMERGENCY_REPORT_CLEANUP_FAILED`、任何 `HISTORICAL_MATERIAL_NORMALIZATION_FAILED` 报告、`INVALIDATED_APPLY_REPORT`、任何 `.INVALIDATED.json`、残留事务 sidecar，或成功报告与数据库 SHA-256 不一致：

1. 禁止启动 ERP。
2. 禁止手工删除、覆盖或重命名正式库、恢复临时文件、备份和 sidecar。
3. 保存终端输出及全部报告路径。
4. 不得把残留 `HISTORICAL_MATERIAL_NORMALIZATION_APPLY_*` 文件当作成功证据；先检查其 JSON `status/valid` 和同名 `.INVALIDATED.json`。
5. 由另一名 Codex/维护人员只读复核后再决定恢复。

只有两张实际存在的历史表全部满足验收条件，才可恢复 ERP 服务并完成人工页面验收。
