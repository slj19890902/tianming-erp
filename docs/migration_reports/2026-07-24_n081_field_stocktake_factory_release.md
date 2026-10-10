# 2026-07-24 N081 两人现场盘点简化工厂正式发布

## 摘要

- 正式候选：
  `1b318efed68c77fa39ff50935e79d09dd0ba8961`。
- 候选线性继承已发布的
  `f7680f1cadb730751d5baa458f9743ed11776ba8`。
- 用户明确授权正式迁移：
  `cp72v8x9z61 → cj66v8x9z55 → ck67v8x9z56 → cm69v8x9z58 → cn70v8x9z59`。
- 使用 P0-A Prepare / Apply 门禁完成停服、备份、隔离演练、正式迁移、
  完整性复检和 ERP 重启。
- 运行时 JSON：
  `docs/migration_reports/release_runtime_20260724_201213.json`。

## 备份与隔离演练

- Prepare 前正式库 revision=`cp72v8x9z61`。
- Prepare 前正式库 SHA-256：
  `F95032C8B1820688D274C784A8BB3030B49FC5E71E0BE074AD74443B984C564E`。
- SQLite Backup API 备份：
  `D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_release_20260724_201214.sqlite3`。
- 备份 SHA-256：
  `D8FFA99ED9BA0237AAAA7A232E67F71E39863FD6CA423962F5CFA0DBACEBB851`。
- 备份 revision=`cp72v8x9z61`、`integrity_check=ok`、外键异常 0。
- 隔离演练副本：
  `D:\纸箱厂erp软件搭建\data\release_rehearsals\carton_erp_release_rehearsal_20260724_201214.sqlite3`。
- 演练完成 revision=`cn70v8x9z59`、`integrity_check=ok`、外键异常 0。
- 演练 SHA-256：
  `0BFDD1DB58BA0D806068D4CAB2300DCAF8AD794AE9BD4D12F420245F1B2D419C`。
- 源库、备份、演练副本的 15 张核心业务表计数完全一致。

## 正式 Apply 与重启

- 正式迁移后 revision=`cn70v8x9z59`。
- 正式迁移后 SHA-256：
  `D89D0C0B64250FA96F34A8183AF7AA2D86354B1F7977E31ED50D2C97DDDF486F`。
- `integrity_check=ok`，外键异常 0。
- 15 张核心业务表计数迁移前后完全一致，包括：
  `sales_orders=50`、`sales_order_items=229`、
  `material_requisitions=43`、`material_requisition_items=175`。
- ERP 已恢复监听 `0.0.0.0:8000`；
  `GET /api/health` 返回 200 和 `{"ok":true}`。
- 启动日志未发现 `ERROR`、`Traceback` 或 `Exception`。

## N081 只读验证

- `GET /warehouse.html` 返回 200。
- 未登录访问盘点模板、现场盘点表和盘点批次接口均返回 401，登录门禁正常。
- 页面包含简化后的“首次盘点入库”三步流程：
  导出当前成品库存盘点表、上传并自动检查、一次“确认盘点入库”。
- 页面明确允许大概位置留空或填写自由文字，并将未知位置标记为“位置待确认”。
- 最终确认受权限、检查指纹、错误行归零、版本和幂等门禁保护。
- 发布后只读检查时，
  `inventory_onboarding_batches`、`inventory_onboarding_lines`、
  `inventory_onboarding_postings` 均为 0 条。
- 验证过程没有上传盘点表、创建批次、执行 dry-run、submit 或 post。
- 工厂 `.venv` 未安装 `pytest`；家庭候选的 UAT/回归记录为 `184 passed`，
  本机 Python `compileall` 通过。

## 是否写入数据

- 已写入 `cp72 → cn70` 数据库结构迁移。
- 未新增、删除或修改 15 张核心业务表记录。
- 未创建盘点批次、盘点明细或盘点入账记录。
