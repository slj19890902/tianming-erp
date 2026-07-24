# 组合 BOM 订单专用组件数量与报料作废验证报告

## 范围

- 基线：`f97bb5014986422155c975c323a6121aab7a6204`
- 分支：`codex/bom-requisition-demand-hotfix-20260724`
- 迁移：`ci65v8x9z54 → co71v8x9z60`
- 只使用匿名隔离 SQLite；未连接或写入工厂正式数据库。

## 业务结果

1. 常用箱/BOM 模板继续保持每套一个组件。
2. 订单主项保持 3000 套；客户要求少送组件时，本订单组件可独立改为 2700 个。
3. 待报料同时展示父件与组件：
   - 父件：3000 个，470×600，一开一，3000 张。
   - 组件：2700 个，575×550，一开二，1350 张。
4. 订单专用调整不修改常用箱、BOM 模板、其他订单或历史报料。
5. 新组合报料来源显示为“组合 BOM 报料单”，不再误显示“旧报料单”。
6. 未入库组合报料可整批作废；历史主单和明细保留，父件与组件自动回到待报料。
7. 重复提交与重复作废均幂等；已有入库事实时拒绝直接作废。

## 自动验证

- 订单/BOM/报料/前端/迁移及相邻扩大回归：
  `136 passed, 1 deselected`。
- 最终幂等加固后的订单与报料回归：`77 passed`。
- 旧组件单兼容、整批作废与前端专项：`9 passed`。
- 单独的内联 JavaScript 语法检查：`1 passed`。
- Python 编译：通过。
- Alembic：唯一 head=`co71v8x9z60`。
- `git diff --check`：通过。
- 排除的测试是基线已有的移动入库 PDF 原始路径断言，不在本候选修改范围。

## 隔离迁移证据

- 迁移副本：
  `D:\tm-uat\erp-bom-requisition-demand-hotfix-20260724\migration-rehearsal.sqlite3`
- 迁移前备份：
  `D:\tm-uat\erp-bom-requisition-demand-hotfix-20260724\migration-rehearsal-ci65-backup.sqlite3`
- 迁移前备份 SHA-256：
  `2FE72AD9ED58FA3496F2566F74D5FDE1D4318A32E329213783618AD2E4E1BF47`
- 升级结果：`co71v8x9z60`
- `integrity_check=ok`
- `foreign_key_check=0`

## 浏览器 UAT 证据

- 数据库：
  `D:\tm-uat\erp-bom-requisition-demand-hotfix-20260724\browser-uat.sqlite3`
- 匿名测试账号：`admin` / `123456`
- 已完成生成、来源识别、整批作废和返回待报料的页面闭环。
- 最终保留 1 张状态为“已取消”的组合报料主单、2 条状态为“已取消”的报料明细。
- 浏览器 Console 无 error/warn。
- 截图：
  - `D:\tm-uat\erp-bom-requisition-demand-hotfix-20260724\screenshots\bom-pending-after-void.png`
  - `D:\tm-uat\erp-bom-requisition-demand-hotfix-20260724\screenshots\bom-reported-voided-history.png`

## 工厂发布边界

- 家庭侧不得更新 `origin/factory-current-baseline` 或 `origin/main`。
- 工厂端必须先核对候选 SHA 和祖先关系。
- 正式库必须先创建并验证一致性备份。
- 先在备份副本演练 `ci65 → co71`，再取得明确发布授权。
- 正式迁移后需要重启 ERP，并复核 revision、完整性、外键、健康接口和组合报料页面。
