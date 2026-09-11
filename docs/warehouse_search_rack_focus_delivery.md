# 查货直达货架层格 — v0.22.353

2026-09-11 16:03:27 技术发布完成；开发验证通过，正式 ERP 人工验收待完成。

- 任务 WAREHOUSE-SEARCH-RACK-FOCUS-20260911；分支 codex/warehouse-search-rack-focus-20260911。开始基线 93ab6c21/v351，保留并接入最新正式 94fb12df/v352 后交付；代码 33b70e6c0b1feffe766d560f01e0667594b241e6 已推送并快进 factory-current-baseline。
- 根因是搜索只定位地图货位点，且打开货架时查货列表被隐藏。现在按已映射正式货位与批次核对货架、层格，打开正视图并滚到目标格；黄色表示该产品所在层格/产品，深色边框和文字标明当前格。同产品多位置可通过原位置列表切换；新产品/地面/文字位置收起旧架。搜索输入不触发方向键切架。原点格收起、添加、标签和批次操作保留。
- 文件：WarehouseTwinApp.tsx、warehouseTwin.css、rackEmptyCellClick.test.mjs、warehouseSearchRack.test.mjs、版本、对应 JS/CSS 与入口及文档。没有业务 API 或 migration 变更，没有修改正式地图、草稿、库存数量、批次位置、权限或成本；未导入模拟布局。
- 新增定位用例先 4 项失败；修复后两个定向测试文件共 16 项通过。涵盖正确层格、同架切产品、同产品多格、同格混放、旧/冲突身份、跨楼层等待、旧架关闭、不覆盖追溯入口、v350 点格收起及独立标签/明细。类型检查、Vite 构建、差异检查通过；未跑无关后端/全量回归。
- Chrome 创建验收页 30 秒超时并重置会话，未使用 IAB；不把单元测试当作真实 Chrome/手机人工验收。

## 发布核对

唯一 head sb11v8x9z76 与 v352 已发布数据库一致；本任务只有现有 head 的 no-op upgrade，不降级，不执行历史导入。报告 docs/migration_reports/release_runtime_20260911_160226.json 状态 completed，版本 v0.22.353。

备份 data/backups/carton_erp_before_release_20260911_160230.sqlite3；隔离副本 data/release_rehearsals/carton_erp_release_rehearsal_20260911_160230.sqlite3。两者 SHA-256 e0634e6e2558ff14559d7522632a0133233f8e2bc63d6b7ce7375e04f76165bb，完整性 ok、FK=0、核心计数一致。正式库发布前后 SHA-256 d8ed7156f862e5956698322e7085be585bcb947f750c827b28f7b33f0f98dbf6 完全一致，完整性 ok、FK=0。回滚准备为 v352/94fb12df 与本次已验证备份，未执行回滚，不能覆盖发布后新业务。

正式 /warehouse.html 200/no-store，已引用新资源，线上和磁盘 SHA-256 一致：

- warehouseTwin-Bf0LfeK0.js：5977EE7015A194C753866FC963C0817B4E7F14C57770256CE7B852E7B18A5DD5。
- warehouseTwin-DxVrEJcV.css：88C767C18A9FCAF448B023F76C461E80D5EF454663874CD1CEE0F305A8EC05B0。

本机 127.0.0.1:18000、工厂 192.168.3.80:8000、正式 HTTPS 外网健康均 200/ok。

现场最短验收：刷新仓库，连续点击搜索结果的两个货架产品（不同架或同架不同格），检查正视图及黄色位置同步切换；再选地面产品应收起正视图；点格仍打开对应货位信息。

NAS：04_开发记录/任务回执/20260911_1603_查货直达货架层格_v353.md。实际 token 用量未提供，不估算。
