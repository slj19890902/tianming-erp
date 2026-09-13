# MOBILEMARGIN001 手机首页送货材料毛利（2026-09-13）

## 目标

- 在 `static/mobile_erp.html` 首页为管理员/老板增加只读“送货材料毛利”可视化。
- 使用阶段3固定的 `GET /api/dashboard/customer-delivery-margin` 契约；不修改后端、桌面入口、版本或迁移。

## 门禁与业务边界

- 仅当 `state.shell?.delivery_margin_allowed === true` 时显示模块并请求接口；不把 `management_summary_allowed` 当作成本权限。
- 管理员/老板且具备 `dashboard.view`、`finance.view`、`cost.view` 由后端 allowlist 决定；员工不请求成本接口。
- 明确标记送货材料毛利，不称净利；未知售价、税口径、单位、成本保持缺口，不按零补猜。
- 保留原现场入口、财务待办与既有手机导航权限。

## 交互范围

- 客户、日期筛选；日期默认北京时间本月 1 日至今天；返回保留筛选。
- 金额卡、销售额与材料成本柱图、毛利率每日趋势、分页客户表与缺口展开。
- 请求支持 abort、generation 过期保护和切页保护；窄屏不横向溢出。

## 文件所有权

- `static/mobile_erp.html`
- `static/ui/mobile-delivery-margin.js`
- `static/ui/mobile-delivery-margin.css`
- 专属 Node/静态契约测试

## 验证与状态

- 使用匿名 fixture 做 Node/静态定向测试，不使用真实数据、不执行正式页面自动点击或 IAB。
- 本分支基于实时 `origin/factory-current-baseline` 005a5a9f/v390；提交但不推送，待 Astra 复核与主负责人整合。
