# 产品前缀候选人工复核与审批校验流程

- 执行时间：2026-06-19 15:08:45 +08:00
- 本轮是否修改主库：否
- 是否修改正式表：否
- 是否修改产品表：否
- 是否应用产品映射：否
- 是否导入正式订单：否

## 创建文件

- 人工复核 worksheet：`docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_20260619_150757.csv`
- 填写说明：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_GUIDE_20260619_150757.md`
- 只读校验脚本：`scripts/migration/validate_product_prefix_review.py`
- 首次校验报告：`docs/migration_reports/PRODUCT_PREFIX_REVIEW_VALIDATION_20260619_150845.md`

## 审批字段说明

| 字段 | 用途 |
|---|---|
| `review_status` | `pending`、`approved`、`rejected`、`needs_check` |
| `review_decision` | 人工决定；只能使用五个允许值 |
| `approved_product_id` | 仅批准映射时填写的现有产品 ID |
| `reviewed_by` | 审核人 |
| `reviewed_at` | 审核时间 |
| `review_note` | 改判、拒绝或特殊例外说明 |

允许的 `review_decision`：

- `approve_prefix_match`
- `reject_fee_item`
- `manual_check_required`
- `create_product_later`
- `reject_wrong_product`

## 校验规则

1. CSV 必须包含全部来源字段和审批字段。
2. `pending` 可保持决定空白；其他状态必须填写决定和审核留痕。
3. 批准映射必须填写现有、未删除、同客户的 `products.id`。
4. 批准产品默认必须等于候选产品；人工改判时必须填写明确说明。
5. 费用关键词默认禁止批准为普通产品。
6. 拒绝费用项不得填写产品 ID。
7. `manual_check_required`、`create_product_later`、`reject_wrong_product` 不计入可迁移统计。
8. `customer_id + legacy_style_no_raw` 不允许重复。
9. 校验脚本使用 SQLite 只读连接和 `PRAGMA query_only=ON`，运行前后核对 SHA-256。

## 当前校验结果

- worksheet 总行数：200
- `pending`：200
- 已批准映射：0
- 可新增可迁移明细：0
- 可新增可迁移订单预估：0
- 仍不可迁移订单预估：30,076
- 审批错误：0
- 校验结果：通过
- 当前尚无批准项，不得用于任何迁移。

## 人工操作步骤

1. 按金额和排名从 worksheet 顶部开始核对候选产品。
2. 只填写六个人工审批字段，不改来源字段。
3. 每批填写后运行只读校验脚本。
4. 校验错误必须全部修正；不得跳过错误行进入迁移。
5. 保存已审核 CSV 的只读归档副本，并记录审核人、时间和批次。

## 风险点

- 前缀唯一命中不等于业务语义必然正确。
- 费用、工装、旧编码和组合品可能与普通产品编码相似。
- 人工改判产品必须防止跨客户引用。
- CSV 可被手工破坏格式，应每次重新运行完整校验。
- 当前影响模拟只基于已批准行和现有主库快照。

## 后续副本试迁移设计

人工完成一小批审批并校验通过后，下一轮只允许：

1. 复制主库为隔离副本。
2. 迁移脚本显式读取已校验的 review CSV。
3. 仅使用 `review_status=approved` 且 `review_decision=approve_prefix_match` 的行。
4. 先限制 20 至 100 张订单，核对订单、明细、产品关联、金额和幂等性。
5. 主库哈希必须保持不变；未经新授权不得写主库正式表。
