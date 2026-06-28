# ERP Project State

## Current Baseline

- Branch: `factory-current-baseline`
- Latest merge commit: `6913749`
- Feature branch merged: `feature/v0203-company-info`
- App version: `v0.20.5`
- Version name: `回单撤销与对账单修复版`

## What v0.20.5 Fixed

- 送货单确认回单后，可以撤销回单，误点后能退回已发货状态。
- 月结对账单现在可以切换到有对账资格的客户，不会只盯住一个客户。
- 对账单新增了查看、编辑、取消按钮，遇到开票或收款记录会明确提示原因。
- 收款核销不再要求填写收款账户，直接输入金额即可。

## What v0.20.3 Added

- Company info maintenance in system settings
- Admin-only read/write API for company config
- Delivery print sender fields sourced from company config
- Statement export company header alignment

## Verified Behavior

- Admin can read and save company info
- Non-admin cannot read company info
- Blank company name is rejected
- Delivery print page shows company name, address, and phone from backend data
- Formal database was not modified by the merge

## Notes

- No history migration was performed in this merge.
- No `legacy_*` tables were changed.
- Existing ERP structure and workflows remain intact.
