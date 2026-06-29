# Business Rules

## Company Info

- Company info is an admin-maintained system setting.
- Only admins can view or edit it.
- Blank company names are invalid.

## Delivery Print

- Delivery print uses company sender information from the backend.
- If sender info is missing, the UI should fall back to the existing display values.

## Common Box Board Recommendation

- ERP 常用箱的长、宽、高、报料尺寸和压线尺寸统一使用整数毫米。
- A1/0201 普通开槽箱采购报料推荐：报料长 = `2 × (L + W) + 30`，报料宽 = `W + H + 5`。
- 其中 30mm 是整体舌头/搭口，5mm 是宽度方向经验放量；不得替换为报价面积公式。
- 人工选择“压线”后，推荐上摇盖、高、下摇盖为 `round(W/2)、H、round(W/2)`。
- 推荐值允许人工修改；人工修改后不得自动覆盖，只有点击“重新推荐”才强制刷新。
- 只有 A1/0201 使用上述公式。其他箱型没有确认公式时必须提示人工填写。
- 历史非标准箱型值必须保留，不做批量整理。

## Data Safety

- This release does not alter historical orders.
- This release does not modify `legacy_*` tables.
- This release does not run any migration against the formal database.
