# ERP Project State

## Current Baseline

- Stable branch: `factory-current-baseline`
- Current delivery branch: `feature/v0208-common-box-edit`
- App version: `v0.20.8`
- Version name: `常用箱编辑优化版`

## What v0.20.8 Fixed

- 常用箱编辑页按六行重新整理，桌面录入更紧凑。
- 长、宽、高按整数毫米显示，单价继续保留小数。
- 生产工艺支持多选，印刷内容使用清楚的固定选项。
- 无印刷时隐藏图纸区域；有印刷时显示上传入口和版本历史。
- 图纸继续逐版保存，新版本不会覆盖旧版本。
- 没有新增数据库字段，也没有批量修改历史产品。

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

- v0.20.8 定向测试 `85 passed`
- 浏览器实测常用箱编辑弹窗六行布局正常
- 无印刷/有印刷切换会正确隐藏或显示图纸区域
- 浏览器控制台无错误
- 正式库只读核对：`integrity_check=ok`、`foreign_key_check=0`
- Admin can read and save company info
- Non-admin cannot read company info
- Blank company name is rejected
- Delivery print page shows company name, address, and phone from backend data
- Formal database was not modified by the merge

## Notes

- No history migration was performed in this release.
- No `legacy_*` tables were changed.
- Existing ERP structure and workflows remain intact.
