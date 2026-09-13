# DESKTOPMARGIN001：桌面首页客户送货材料毛利

## 范围

- 在桌面首页接入同源 `GET /api/dashboard/customer-delivery-margin` 的只读看板。
- 仅展示当前用户可见且接口返回的销售、冻结材料成本、毛利、覆盖度、缺口和参考补充；前端不推算缺口成本或改写金额。
- 支持本月默认日期、客户搜索、日期/客户筛选、分页客户表、销售/成本柱状图和每日毛利率趋势。
- 保留旧待办及旧回单/对账 KPI，并明确其原口径身份。

## 不变量与安全边界

- 管理员/老板且同时具备 `dashboard.view`、`finance.view`、`cost.view` 才显示并请求该模块；403、失败、离开、返回、退出或取消请求均不得保留上一客户的敏感数据。
- 数字展示直接使用 API；未知值显示“待补”，负毛利保留负号，销售额为 0 时毛利率显示“—”。
- 客户搜索只调用 `/api/master/customers?keyword=...&page_size=50&include_inactive=true`，不把报表客户分页当全集。
- 请求支持 generation/AbortController 防竞态；无写入、无正式页面自动点击、无后端/手机/版本/业务事实变更。

## 允许文件

- 初始：本任务卡、新 `static/ui/desktop-delivery-margin.js`、新 `static/ui/desktop-delivery-margin.css`、专属测试。
- 待主会明确释放首页模板所有权后，才可改 `static/index.html` 接入模块。

## 验证

- Node mock 异步生命周期：竞态响应、Abort/leave、403/失败清敏感数据、客户搜索和展示缺口语义。
- Vue 模板/静态差异测试及定向 Python 测试；完成后记录独立 NAS 任务回执。
