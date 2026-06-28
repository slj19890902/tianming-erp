# ERP Project State

## Current Baseline

- Branch: `factory-current-baseline`
- Latest merge commit: `6913749`
- Feature branch merged: `feature/v0203-company-info`
- App version: `v0.20.3`
- Version name: `公司信息维护版`

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
