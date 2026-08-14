# P1-11D0 原料片料数据门槛只读审计

- 门槛状态：`blocked`
- 数据库写入：`false`
- 连接方式：`sqlite_mode_ro_query_only`
- 数据库 revision：`cr74v8x9z63`
- 期望 revision：`mm21v8x9z10`

## 数据结论

- 正式 raw_board：0 条正式 raw_board。
- 没有正式 raw_board 分母，因此完整率不是 0%，而是不可计算。
- 可用客户专用片料：0 条。
- 可用通用片料：0 条。
- 未关联正式 lot 的历史半成品栈板行：22 条，数量 12811 张。

## 门槛检查

- PASS：`database_hash_unchanged`
- PASS：`query_only_no_changes`
- PASS：`integrity_check_ok`
- PASS：`foreign_key_check_ok`
- BLOCKED：`revision_matches_current`
- BLOCKED：`has_formal_raw_board_denominator`
- BLOCKED：`has_ready_customer_specific_lot`
- BLOCKED：`has_ready_general_lot`
- BLOCKED：`historical_unlinked_semi_items_resolved`

## 边界

本报告只含聚合数字、匿名编号和原因代码；不包含批次号、客户、产品、货位或原始业务 ID。
匿名测试夹具只验证审计器，不构成真实数据门槛通过证据。
D0 不接订单冻结需求，因此不计算尺寸/楞型等订单兼容性；D1～D3 未获授权。
