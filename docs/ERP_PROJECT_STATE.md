# ERP Project State

## Current Baseline

- Branch: `factory-current-baseline`
- Latest merge commit: `6913749`
- Feature branch merged: `feature/v0203-company-info`
- App version: `v0.20.7`
- Version name: `首页待对账汇总与一键启动版`

## What v0.20.7 Fixed

- 首页把“待对账”提醒按客户和月份合并了，同一个客户同一个月只显示一条。
- 首页现在更像一个“今天要先处理什么”的看板，不会把同一客户的月结提醒重复列很多次。
- 新增了一键启动方式，双击就能自动升级、启动 ERP 并打开网页。
- 新增了停止、桌面快捷方式和开机自启脚本，普通操作员更容易直接使用。

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
