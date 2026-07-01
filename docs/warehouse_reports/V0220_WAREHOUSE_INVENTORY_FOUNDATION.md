# v0.22.0 仓库库存基础实施报告

日期：2026-07-01

## 完成范围

- 库位主数据：成品、半成品、共用；支持新增、编辑、启用和停用。
- 成品库存：按客户和常用箱人工入库，固化客户、编码、名称、箱型、尺寸、材质和楞型快照。
- 半成品库存：按实际物理张/片、实际长宽、层数、楞型和片料类型人工入库，可记录供应商、归属客户、压线和开料说明。
- 库存操作：冻结、解冻、调整、报损、报废、管理员转通用成品。
- 库存流水：记录每次操作前后可用、预占、消耗、报损和报废数量，支持幂等键及版本冲突校验。
- 库龄提醒：1年、18个月、2年三级提示。
- 独立页面：`/warehouse.html`。

## 明确未实施

- 未接订单成品库存抵扣。
- 未接报料半成品库存抵扣。
- 未提供库存匹配候选、预占、释放和消耗接口。
- 未自动处理一开几换算。
- 未允许半成品尺寸旋转匹配。
- 未让来料入库自动形成半成品库存。
- 未改变订单、报料、入库、送货、对账的既有数量口径。
- 未迁移或批量修改历史库存、订单和报料数据。

## 数据库变更

- Alembic revision：`c20u7v8w9x19`
- 新增表：
  - `warehouse_locations`
  - `inventory_lots`
  - `finished_goods_inventory_details`
  - `semi_finished_inventory_details`
  - `inventory_reservations`
  - `inventory_movements`
- 正式库迁移前备份：
  `data/backups/carton_erp_20260701_161542_BEFORE_V0220_WAREHOUSE_FOUNDATION.sqlite3`
- 备份 SHA-256：
  `73DBD3142CECCC0C3601624DA0A5689DF6EE983AF1110B84B6D5E51306BC5E49`
- 迁移后：`integrity_check=ok`，`foreign_key_check=0`。
- 新库存表初始为0行，未写入模拟或历史库存。

## 历史数据核验

迁移前后以下行数一致：

- 客户：131
- 常用箱：3319
- 订单：10
- 订单明细：59
- 报料单：21
- 报料明细：57
- 送货单及明细：0
- 对账单：0
- 发票：0

## 测试

- `tests/test_warehouse_inventory_foundation.py`：12 passed
- `tests/test_phase10_frontend.py tests/test_factory_update_script.py tests/test_phase5_requisition_wms.py`：24 passed
- 临时空库完整 Alembic 升级到 head 成功，六张库存表齐全，完整性和外键检查通过。
- 全量：891 passed、8 skipped、12 failed。失败项均不涉及本轮库存文件：
  - 旧前端文案与旧材质楞型规则断言；
  - 已确认使用 `0.0.0.0` 的 LAN 启动脚本与旧 `127.0.0.1` 断言；
  - 依赖当前正式库历史内容的旧断言；
  - 本机天华截图 OCR 样本断言；
  - 历史采购排练测试对客户排序和产品分布的固定假设。
- 新页面 JavaScript 语法检查通过；新增路由完整，未暴露候选、预占、释放或消耗接口。浏览器连接能正常进入 ERP 登录页；因当前浏览器无登录会话，未在自动化中写入正式库存数据。

说明：项目 `.venv` 当前未安装 pytest，测试使用本机
`C:\Users\Administrator\AppData\Local\Programs\Python\Python310\python.exe`
执行；正式迁移使用项目 `.venv` 执行。
