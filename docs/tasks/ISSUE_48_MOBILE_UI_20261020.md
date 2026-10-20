# Issue 48 手机版六屏界面改版

任务来源：https://github.com/slj19890902/tianming-erp/issues/48
分支：frontend-v2；基线8028f148，用户明确只在该分支开发，不改后端、不碰生产。
范围：按docs/erp-mobile-ui-design.html实现首页、现场查询、产品详情、认纸板结果、仓库地图、收料。绿色轻量界面、短码、48px触控、真实现有API、权限与原业务动作保持。
验证：npm run build，隔离Chrome六屏及查询/展开/地图/扫码释放/权限/收料请求保护；不连接正式服务。完成后提交与同步远端，在Issue回帖改动及自测结果。

用户确认：保留既有后端权限，财务无法读取的经营数值显示“—”；不扩大权限、不改后端。
完成记录：见 `docs/tasks/ISSUE_48_MOBILE_UI_RESULT_20261020.md`。
