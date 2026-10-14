# INCOMING-PREFLIGHT-20261014

用户范围：修复 sr44/45/46 等补库来料可勾选但批量提交才提示 RAW 地图版本不一致的问题。

成功条件：半成品补库列表调用实际收料使用的同一只读库位解析器；不可用时显示明确原因、禁止勾选；恢复发布后刷新可用。列表不占位、不修图、不写数据库。提交仍执行原门禁。

文件负责人：根代理，仅 app/api/incoming.py、新专项测试、本任务卡。供应商采购与撤销由独立 material-flow 工作区负责；正式图、000204、库存搜索及统一发布由 ERP_PLAN_V3 聊天负责。

保留数量、用途、单位、客户过滤、状态、幂等、事务、价格冻结及历史事实。只补 target_inventory_type=semi_finished 的库位预检，不改变成品补库规则、不修正式数据、不停服。

基线：实际发布源码 61e5fafba43ec9fe27e0381d0dbab59d76eb96a5；origin/factory-current-baseline 仍旧且远端检查超时，不冒称已同步。工作区 codex/incoming-preflight-20261014。隔离验证使用现有 managed-current-copy 派生受管副本；不写正式数据库。

回归：不可用库位的失败先复现；客户范围、分批实收和正常库位可选；多行只读解析一次；既有冻结用途状态投影不覆盖执行阻断原因。无迁移。

## 2026-10-14 开发验证

- 新用例修改前因缺 receipt_execution_ready 失败；修改后专项、checkbox 实际 JS、选中用途库位、首页客户范围共 8 passed。
- 旧 test_projection_queries_are_bounded_and_read_only 在修改前的 61e5 incoming.py 基线也失败（SQL 禁表断言），本任务不扩大修复；其余相关用例通过。
- 真实 10:49 受管隔离副本 sr44/sr45/sr46 均显示地图发布版本阻断，0 SQL 写入，保持数量/来源。结果：evidence/material-map-audit-20261014/incoming-preflight-result.json（工作区外）。
- 验证 runner 使用现有无符号链接 python-inventory-uat，地图证据复制到自有 UAT 目录。首次两次 runner 因 Python 符号链接、地图路径隔离门禁而失败，未修改安全门禁；修正调用后通过。成功副本清理器因无法确认进程退出而保留，未绕过强删。
- compileall、git diff --check 通过；唯一 Alembic head ep1010md，无迁移。
- 开发验证通过，尚未正式发布。仅由 ERP_PLAN_V3 负责人统一整合并发布。
