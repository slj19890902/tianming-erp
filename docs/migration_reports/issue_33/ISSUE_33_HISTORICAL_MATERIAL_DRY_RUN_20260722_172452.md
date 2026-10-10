# Issue #33 第一阶段：历史材质只读 Dry-run

- 模式：只读；工具不提供 Apply 参数。
- 数据库前后 SHA-256：`7863f9d926aa10fae8274cdff8d2e983a20b310aea8f67468afcb212ae366dbb` / `7863f9d926aa10fae8274cdff8d2e983a20b310aea8f67468afcb212ae366dbb`（一致）。
- 数据库前后大小：`219447296` / `219447296`（一致）。
- 数据库前后 mtime UTC：`2026-07-22T07:42:50.091878+00:00` / `2026-07-22T07:42:50.091878+00:00`（一致）。
- `integrity_check`：`ok`；外键错误：`0`。

## 两张历史表摘要

- `historical_requisition_maps`：2313 条；2313 条未关联产品。
  - 需人工 REVIEW：721 条；原因：{'suffix_can_be_reviewed': 663, 'compound_or_unparseable': 58}。
  - 表为原始档案，没有精确层数/楞型字段；本阶段不自动规范或回填。
- `material_requisition_items`：175 条；按关联订单快照校验异常 0 条。
  - 当前业务快照全部已符合代码长度与楞型规则。

## 样本证据

- 原始档案当前精确匹配 `BC14C/A`：0 条；`K618A/B`：0 条。
- 两个字符串在当前原始档案中均为 0；历史快照现保留规范码并由订单快照给出精确层数/楞型。
- 规范快照样本：[{'id': 3, 'material_snapshot': 'K618A', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21301022'}, {'id': 10, 'material_snapshot': 'BC14C', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21302001'}, {'id': 61, 'material_snapshot': 'BC14C', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21302006'}, {'id': 67, 'material_snapshot': 'BC14C', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21302006'}, {'id': 86, 'material_snapshot': 'BC14C', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21302006'}, {'id': 88, 'material_snapshot': 'BC14C', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21302006'}, {'id': 117, 'material_snapshot': 'K618A', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21301022'}, {'id': 166, 'material_snapshot': 'K618A', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21301022'}, {'id': 170, 'material_snapshot': 'BC14C', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21302001'}, {'id': 174, 'material_snapshot': 'BC14C', 'layer_count': 5, 'flute_type': 'AB', 'product_code_snapshot': '21302066'}]

## 人工复核文件

- `D:\tm-worktrees\erp-issue-33-history-dryrun-20260722\docs\migration_reports\issue_33\ISSUE_33_HISTORICAL_MATERIAL_REVIEW_20260722_172452.csv`
- 所有行均为 `review_status=pending`，`suggested_action=review_only_no_apply`。
