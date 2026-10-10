# MOBILE-PRODUCT-WORKBENCH-RELIABILITY-20261010

2026-10-10 持续优化目标下一最小闭环：只读审查首页/手机共用的产品搜索与资料卡，重点图纸展开、产品/页签切换、地图返回、断网/迟到响应及客户权限。先复现、形成方案，再按既有授权修复有证据的缺陷；本卡初始阶段不修改运行源码。

正式已核验 v0.22.606，源4b66fc4c104cf49bc48b065c20b5aeed17f9403a，包f863ced8beebe2968263f409af2c8beb6a7fe58c6d4bf8c56ff3e4a8b83c912d；根01567c4c仅多发布文档。根分支codex/mobile-product-workbench-reliability-20261010。远端6c206e33为已合并的v605首页工作台发布回执，不回退到此旧指针。发布前必须重新核验。

启动顺序 CODEX_START→NAS AI_START→本卡→PLATFORM→总需求2/3/5/14/15/18/19和章程3～9；只读定位跨模块接口时精确读取对应契约。用户持续优化及普通 validated 发布授权保持，无Git push，无正式数据修正/迁移。此前Chrome启动及旧PID停止审批被拒绝，不重试或换工具绕过；不IAB，不正式自动点击。

本卡明确允许两个只读子任务并行以缩短审查；所有仓库文件由根独占写。双方只读根集成树 D:/.codex/worktrees/factory-reliability-20261009/纸箱厂erp软件搭建；探针、报告仅写自己artifact子目录，不能改工作树或运行正式业务接口。不得碰旧服务或其锁定工作树，合成库沿安全conftest，无新服务器/浏览器。

- API /root/mobile_drawings_api：api目录。精读 app/api/product_workbench.py、app/services/product_workbench.py、相关授权/缩略图调用，实际合成HTTP复现产品/批次身份与客户隔离、停用/缺档、来源和输入边界的高风险项。不要因缺乏覆盖就登记Bug，不重做已有正常检查凑数。复用既有tests/test_product_workbench.py夹具，保存精确源和HTTP证据。只读报告+最小修复建议。
- UI /root/mobile_drawings_ui：ui目录。实际static/mobile_erp.html、product-workbench.js/css、desktop入口精确片段；复用tests/ui内linkedom/VM实际实现，审查缩略图重试、跨产品/账号迟到响应、地图返回、查询/页签在途交互、窄屏相关结构。离线非DOM不是浏览器验收；不可声称屏幕通过。报告真实复现和最小修复建议。
- 根：确认本卡及实际新正式基线，跨接口核验、独立审查、定义后续实施allowlist。正式已有功能不得因修复丢失，保留数量/状态/权限/幂等/版本/事务/审计和历史冻结。

审查不改库存、材料匹配核心、BOM、报料审批、价格或历史资料；需要业务规则变化先独立列出，不夹带。发现问题分类真实缺陷/既有正常门禁/待现场核验。每轮写NAS独立回执，不能只重复状态。持续Goal active。
