# ERP Project State

## Current Baseline

- Branch: `factory-current-baseline`
- Latest merge commit: `6913749`
- Feature branch merged: `feature/v0203-company-info`
- App version: `v0.20.6`
- Version name: `回单撤销与对账单修复版`

## What v0.20.6 Fixed

- 首页现在能一打开就看到待报料、待入库、待送货、待回单、待对账和未结清这些待办。
- 首页多了一个简单清楚的提醒区，管理员一进系统就知道先处理什么。
- 首页加载失败时会显示中文提示，不会直接空白。
- 下面还保留了今天和本月的简单业务摘要，方便快速看一眼整体情况。

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
