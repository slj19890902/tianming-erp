# 产品前缀候选人工复核填写说明

本说明适用于：

`PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_20260619_150757.csv`

本文件只记录人工判断，不会自动修改产品、订单或任何数据库。

## 填写原则

每次只修改以下人工字段：

- `review_status`
- `review_decision`
- `approved_product_id`
- `reviewed_by`
- `reviewed_at`
- `review_note`

不要修改客户、原始款号、候选产品、影响数量和金额等来源字段。

## 决定填写规则

### 1. 确认候选产品正确

- `review_status = approved`
- `review_decision = approve_prefix_match`
- `approved_product_id = matched_product_id`
- 填写 `reviewed_by`、`reviewed_at`

### 2. 模具费、制版费、运费、加工费等费用项

- `review_status = rejected`
- `review_decision = reject_fee_item`
- `approved_product_id` 必须留空
- 填写 `reviewed_by`、`reviewed_at`

### 3. 暂时无法判断

- `review_status = needs_check`
- `review_decision = manual_check_required`
- `approved_product_id` 留空
- 在 `review_note` 说明待核对内容

### 4. 产品不存在、后续需要建产品

- `review_status = rejected`
- `review_decision = create_product_later`
- `approved_product_id` 留空
- 在 `review_note` 写明缺少的产品资料

### 5. 候选产品明显错误

- `review_status = rejected`
- `review_decision = reject_wrong_product`
- `approved_product_id` 留空
- 在 `review_note` 说明错误原因

## 强制约束

1. `review_decision` 只允许：
   - `approve_prefix_match`
   - `reject_fee_item`
   - `manual_check_required`
   - `create_product_later`
   - `reject_wrong_product`
2. `approved_product_id` 必须是当前主库中已存在且属于同一客户的 `products.id`。
3. 不允许手写不存在的产品 ID。
4. 不允许把费用项映射为普通纸箱产品。
5. 如人工改判为不同于 `matched_product_id` 的现有产品，必须在 `review_note` 中明确说明原因。
6. 空白决定只能保持 `review_status = pending`，不得进入迁移。
7. 填写决定后必须填写审核人和审核时间，建议时间格式为 `YYYY-MM-DD HH:MM:SS`。
8. 不得产生重复的 `customer_id + legacy_style_no_raw`。

## 校验命令

```powershell
python .\scripts\migration\validate_product_prefix_review.py `
  --sqlite-path .\data\carton_erp.sqlite3 `
  --review-csv .\docs\migration_reports\PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_20260619_150757.csv `
  --output-dir .\docs\migration_reports
```

校验通过只代表复核文件格式和审批规则通过，不代表已应用映射，也不授权写入数据库。
