# 首页工作台与统一产品搜索

用户已于 2026-10-10 回复“实施”，范围为已确认的首页整体评估方案及搜索补充方案。

基线：正式 v0.22.603 / 5b167953446d07d9b77718e05061099803932cfb。独立候选工作树 home-product-workbench-20261010；发布前重新核对正式基线。保留其他任务改动。

目标：客户队列与宽工作清单、完整授权库存汇总、今日安排与历史积压分开；首页和手机共用找产品/片料找用途、生产资料/图纸/BOM/库存位置/订单、现有报料及审批入口。

业务不变量：只查询客户权限内数据；采购成本和利润不进入搜索；实存/可用/占用及单位分开；实际选料使用原有匹配门禁；无证据不认定片料归属；停用/临时仅现货产品不报料；保留提醒、版本、事务、审计、幂等门禁。此任务不补正正式库存、模具、报价或档案。

按 NAS《Codex模型分工与审核规则》老板授权并行，唯一写入负责人：
- backend：app/api/product_workbench.py、app/services/product_workbench.py、app/main.py、tests/test_product_workbench.py。
- home_frontend：static/ui/home-workbench.js、static/ui/home-workbench.css、tests/ui/home_workbench.test.cjs。
- 主管：static/ui/product-workbench.js/css、static/index.html、static/mobile_erp.html、共享 UI 测试、集成和发布记录。
所有成员只在此独立候选内工作，不提交/部署业务数据。组件集成约定：首页模板包含 <product-workbench :vm="vm"></product-workbench>；组件 emit/search 状态通过 vm.productWorkbenchOpen 控制工作区；注册由主管实现。

验证：权限与零库存搜索、精确/逆向尺寸与加工状态、资料卡字段/BOM、客户汇总筛选、提醒保留、前端请求乱序与布局检查；无 DDL 计划。隔离副本验证，不操作正式页面。正式发布仍需备份、迁移链、静态资源完整性与只读健康核对；管理员人工验收另计。
