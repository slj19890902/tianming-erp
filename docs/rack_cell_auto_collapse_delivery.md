# 货架点选货位自动收起 — v0.22.350 发布回执

2026-09-11 15:36:32 技术发布完成；开发验证通过，正式 ERP 人工验收待完成。

任务：RACK-CELL-AUTO-COLLAPSE-20260911。基线 d44ba00e / v349；独立分支 codex/rack-cell-auto-collapse-20260911，代码 e2caa6906367e5182c6585720f8198dc19896e84，已推送候选并快进正式 factory-current-baseline。

## 结果和边界

点击正视图的格数或格内非按钮内容，选中该格正式 location_id，自动关闭正视图并聚焦右侧货位信息。空格可只读查看，添加权限仍独立校验。保留查货/移货/盘点模式、移货来源和页面草稿。产品编码查看标签、查看明细展开批次、打印及添加按钮保留各自操作，不因冒泡错误关闭。

只修改 WarehouseTwinApp.tsx、对应 rackEmptyCellClick 测试、版本、构建入口/新 bundle 及文档；不修改业务 API、权限、地图/草稿、库存数量、绑定或成本。无新迁移，唯一 head 仍为 rz10v8x9z74。不使用家庭模拟布局，不写入正式业务事实。

## 验证

- 先复现：3 个新增用例失败、7 个原用例通过。修改后 rackEmptyCellClick + rackFront 共 13 项通过；涵盖唯一身份、空/有货货位、只读查看、添加权限、无意外按钮冒泡、标签/批次交互及原格数布局。
- TypeScript 类型检查、Vite 构建和差异检查通过；未跑无关后端或全量测试。
- 正式 /warehouse.html 返回 200、Cache-Control: no-store，并引用 warehouseTwin-B9odmguF.js；所有引用资源存在。新 bundle 的线上与磁盘 SHA-256 一致：7D09F78C7D6182381273DCE2EC29BEBDBA23A9301F1F9C0AE700A286F6F58E37。
- localhost:18000、192.168.3.80:8000 和正式 HTTPS 外网 /api/health 均回读 200 / ok=true。启动时外网健康短暂失败，后续复检已恢复；未更改代理配置。
- Chrome 发现成功但创建验收页超时 30 秒且会话重置，未改用 IAB，不声称真实页面点击通过。现场最短验收：刷新仓库→打开货架正视图→点击格数，检查直接出现对应货位信息；再核对产品编码、批次明细各自功能。

## 发布和回滚证据

正式报告：docs/migration_reports/release_runtime_20260911_153545.json，completed，版本 v0.22.350，code SHA e2caa690，head rz10v8x9z74。

备份：data/backups/carton_erp_before_release_20260911_153548.sqlite3。隔离演练：data/release_rehearsals/carton_erp_release_rehearsal_20260911_153548.sqlite3。两者 SHA-256 均为 104481b1c74a4578052f833a6dc3054d677648956dc21ebab1fc416f2661ed66；完整性 ok、外键违规 0，核心计数一致。沿用现有 head，仅 no-op upgrade，未执行历史降级或旧导入。

正式库发布前后 SHA-256 均为 c6ecd34670816cb5c5c56b8d27c649778678118259624298c5d2bf3798c055c6，完整性 ok、外键违规 0、核心计数未变。回滚以 v349/d44ba00e 代码和上述已验证备份为恢复点；本轮未执行回滚，任何恢复仍需保留新现场并核对差异，不能覆盖发布后业务。

NAS 独立回执：04_开发记录/任务回执/20260911_1536_货架点选货位自动收起_v350.md。实际 token 用量环境未提供，不估算。
