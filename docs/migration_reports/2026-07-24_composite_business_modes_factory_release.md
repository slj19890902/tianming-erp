# 2026-07-24 组合报料业务模式工厂正式发布

## 摘要

- 工厂正式目录按候选
  `f7680f1cadb730751d5baa458f9743ed11776ba8` 快进同步。
- 用户针对现场实际 revision 差异再次明确授权完整迁移链：
  `ci65v8x9z54 → co71v8x9z60 → cp72v8x9z61`。
- 使用 P0-A Prepare / Apply 正式发布门禁完成停服、备份、隔离演练、
  正式迁移、完整性复检和 ERP 重启。
- 运行时 JSON 证据：
  `docs/migration_reports/release_runtime_20260724_194742.json`。

## 备份与演练

- 正式源库：
  `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`。
- Prepare 前 revision：`ci65v8x9z54`。
- Prepare 前 SHA-256：
  `C33ABCE2956F519D4DF7D35A0A077259038B69270879A626C9CFBA0C44D15136`。
- SQLite Backup API 备份：
  `D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_release_20260724_194743.sqlite3`。
- 备份 SHA-256：
  `77692E5C8E155E4FDCC49EA86A6F75452B2A899159AD40941258A6A2BA43B473`。
- 备份 revision=`ci65v8x9z54`、`integrity_check=ok`、外键异常 0。
- 隔离演练副本：
  `D:\纸箱厂erp软件搭建\data\release_rehearsals\carton_erp_release_rehearsal_20260724_194743.sqlite3`。
- 演练完成 revision=`cp72v8x9z61`、`integrity_check=ok`、外键异常 0。
- 演练 SHA-256：
  `779FE8BD14B3F30BA02812AA27EDBC8AF5D484FBDC34BFF3F950FEFB2E24AF25`。
- 源库、备份和演练副本的 15 张核心业务表计数完全一致。

## 正式 Apply 与重启

- 正式迁移后 revision=`cp72v8x9z61`。
- 正式迁移后 SHA-256：
  `F95032C8B1820688D274C784A8BB3030B49FC5E71E0BE074AD74443B984C564E`。
- `integrity_check=ok`，外键异常 0。
- 15 张核心业务表计数在迁移前后完全一致，包括：
  `sales_orders=50`、`sales_order_items=229`、
  `material_requisitions=43`、`material_requisition_items=175`。
- ERP 已恢复监听 `0.0.0.0:8000`；
  `GET /api/health` 返回 200 和 `{"ok": true}`。
- 启动日志未发现 `ERROR`、`Traceback` 或 `Exception`。

## 报料流程只读验证

- `GET /api/requisition/pending` 在未登录状态返回 401，登录门禁正常。
- 正式页面返回 200，并包含以下受控流程契约：
  “合并报料”、“报料明细草稿”、“自动使用可匹配库存”和
  “确认生成正式采购单”。
- 验证过程未点击确认生成、作废、库存抵扣、入库或其他业务写入动作。
- 验证前后正式报料主表/明细保持 `43/175`。
- 工厂 `.venv` 当前未安装 `pytest`，因此没有在正式运行环境重跑 pytest；
  Python `compileall` 已通过。
- 内置浏览器仅能看到登录页；现有 Chrome ERP 标签页的只读接管两次超时，
  未能形成登录后页面点击证据，也未对现场标签页执行写入动作。后续建议由现场
  已登录人员人工打开报料工作台，确认“合并报料”首先进入草稿且不直接入账。

## 是否写入数据

- 已写入数据库结构：正式库从 `ci65v8x9z54` 升级到 `cp72v8x9z61`。
- 未新增、删除或修改核心业务表记录；核心表计数迁移前后完全一致。
- 本轮没有生成新的正式报料单。
