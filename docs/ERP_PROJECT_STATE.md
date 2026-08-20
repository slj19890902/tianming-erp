# ERP Project State（历史快照）

> 状态：已停止作为当前版本和能力来源。本文记录早期 v0.20.x 阶段；当前 SHA、版本、migration 和能力必须实时核对。当前业务规则入口为 `docs/TIANMING_ERP_MASTER_REQUIREMENTS.md`，启动入口为 `docs/CODEX_START.md`。

## Current Baseline

- Stable branch: `factory-current-baseline`
- Current delivery branch: `feature/v0208-common-box-edit`
- App version: `v0.20.8`
- Version name: `常用箱编辑优化版`

## What v0.20.8 Fixed

- 常用箱编辑页按五排重新整理，长宽高、报料和压线集中在第二排。
- 长、宽、高、报料长宽和压线尺寸按整数毫米显示，单价继续保留小数。
- A1/0201 普通开槽箱在箱型、长宽高和材质齐全后自动推荐采购报料尺寸：报料长 `2 × (L + W) + 30`，报料宽 `W + H + 5`。
- 人工选择“压线”后自动推荐“上摇盖 / 高 / 下摇盖”为 `round(W/2) / H / round(W/2)`。
- 用户手工修改过报料或压线尺寸后，后续字段变化不会强制覆盖；可点击“重新推荐”主动刷新。
- A3 天地盖、平卡、刀卡、隔板、围套、半开槽箱、全搭盖箱、异形箱和其他已加入箱型列表，暂无可靠公式时提示人工填写。
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

- v0.20.8 原始定向测试 `85 passed`；现场修正版测试结果见最新交接记录
- 常用箱编辑弹窗已改为五排布局
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
