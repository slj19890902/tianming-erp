# 2026-07-24 组合报料业务模式工厂正式发布

## 摘要

- 工厂正式目录按候选
  `f7680f1cadb730751d5baa458f9743ed11776ba8` 快进同步。
- 用户针对现场实际 revision 差异明确授权完整迁移链：
  `ci65v8x9z54 → co71v8x9z60 → cp72v8x9z61`。
- 使用 P0-A Prepare / Apply 正式发布门禁完成停服、备份、隔离演练、
  正式迁移、完整性复检和 ERP 重启。
- 运行时 JSON 证据：
  `docs/migration_reports/release_runtime_20260724_194742.json`。

## 备份、迁移与验证

- 备份：
  `data/backups/carton_erp_before_release_20260724_194743.sqlite3`。
- 隔离演练：
  `data/release_rehearsals/carton_erp_release_rehearsal_20260724_194743.sqlite3`。
- 备份 revision=`ci65v8x9z54`、`integrity_check=ok`、外键异常 0。
- 演练和正式库最终 revision=`cp72v8x9z61`、
  `integrity_check=ok`、外键异常 0。
- 正式迁移后 SHA-256：
  `F95032C8B1820688D274C784A8BB3030B49FC5E71E0BE074AD74443B984C564E`。
- 15 张核心业务表计数迁移前后完全一致，其中销售订单/明细 `50/229`、
  报料主表/明细 `43/175`。
- ERP 恢复监听 `0.0.0.0:8000`，健康接口返回 200。
- 报料只读验证没有执行确认生成、作废、库存抵扣或入库。

## 是否写入数据

- 已写入数据库结构迁移。
- 未新增、删除或修改核心业务记录。
- 本轮没有生成新的正式报料单。
