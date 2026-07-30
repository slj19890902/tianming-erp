# Q0-04 / P0-08A 供应商只读审计

> **证据边界：本报告不能证明 2026-07-30 工厂当前正式库。**
> 正式迁移、停用佳丰或发布前，必须在工厂最新正式库的隔离副本上重跑同一脚本。

## 数据源与只读门禁

- 来源标签：`2026-07-28工厂来源隔离副本再复制件；非2026-07-30工厂当前正式库；正式发布前须在工厂最新隔离副本重跑`
- 数据库：`D:\tm-uat\q0-04-supplier-master-20260730\carton_erp_before_q0_04.sqlite3`
- 大小：`219447296` 字节
- 修改时间（UTC）：`2026-07-28T04:29:41.536685+00:00`
- SHA-256：`9C7A90F37D60521DE36723D9BA9B0C36EB7A47EE35C2F1E04AF93AC4016FEC42`
- SQLite：`3.49.1`
- 连接：URI `mode=ro`
- `query_only`：`True`
- 写操作 authorizer：`True`
- 审计前后数据库及 sidecar 未变化：`True`

## 数据库检查

- Alembic：`cr74v8x9z63`
- `integrity_check`：`ok`
- 外键异常：`0`
- 表数量：`143`

## P0-08 新供应商主档迁移状态

- 状态：**P0-08 未迁移**
- `supplier_master_records`：`False`
- `supplier_master_aliases`：`False`
- 新主档记录：`0`
- 新主档别名：`0`

_无记录。_

## 旧系统历史供应商表（只读保留）

> `suppliers` 是旧系统历史供应商表，不是 P0-08 新供应商主档。新迁移必须新建独立表，禁止覆盖、重命名或清空该旧表。

- `suppliers` 表存在：`True`
- 字段：`address, contact_person, created_at, id, is_active, name, payment_note, phone, updated_at`
- 指向该旧表的外键数量：`6`

| ID | 标准名 | 显示名/简称 | 别名 | 业务代码 | 旧系统ID | 启用 |
|---|---|---|---|---|---|---|
| 1 | 苏州佳丰纸业有限公司 | 佳丰 | 佳丰 | JF | 9 | 1 |
| 2 | 固丽纸业(上海)有限公司 | 固丽 | 固丽 | GL | 10 | 1 |
| 3 | 苏州市科能纸品有限公司 | 科能 | 科能 | KN | 12 | 1 |
| 4 | 苏州市金东浩纸业有限公司 | 金东浩 | 金东浩 | JDH | 13 | 1 |
| 5 | 达成包装制品(苏州)有限公司 | 达成 | 达成 | DC | 14 | 1 |
| 6 | 苏州一诺包装有限公司 | 一诺包装 | 一诺包装 | YN | 15 | 1 |
| 7 | 昆山市诺尔特包装材料有限公司 | 诺尔特 | 诺尔特 | NTE | 16 | 1 |
| 8 | 苏州聚晟达电子材料科技有限公司 | 春山包装 | 春山包装 | CS | 17 | 1 |
| 9 | 永丰余纸业（上海）有限公司 | 永丰余 | 永丰余 | YFY | 18 | 1 |
| 10 | 昆山鸣朋纸业有限公司 | 鸣朋 | 鸣朋 | MP | 19 | 1 |
| 11 | 苏州顺厚金属制品有限公司 | 顺厚金属 | 顺厚金属 | SH | 21 | 1 |

### 指向旧 `suppliers` 表的外键

| 引用表 | 引用字段 | 目标字段 | ON UPDATE | ON DELETE |
|---|---|---|---|---|
| accounts_payable | supplier_id | id | NO ACTION | NO ACTION |
| legacy_ruida_order_items | supplier_id | id | NO ACTION | NO ACTION |
| material_reports | supplier_id | id | NO ACTION | NO ACTION |
| orders | supplier_id | id | NO ACTION | NO ACTION |
| payable_payments | supplier_id | id | NO ACTION | NO ACTION |
| product_archives | last_supplier_id | id | NO ACTION | NO ACTION |

## 数据库中可识别的供应商名称

| 类别 | 表 | 字段 | 名称/别名值 | 行数 |
|---|---|---|---|---|
| 订单/历史 | legacy_ruida_order_items | supplier_name | JF   佳丰 | 30125 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | MP   鸣朋 | 927 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | GL   固丽 | 239 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | YN   一诺包装 | 126 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | NTE   诺尔特 | 117 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | JDH   金东浩 | 45 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | DC   达成 | 24 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | KN   科能 | 22 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | CS   春山包装 | 20 |
| 订单/历史 | legacy_ruida_order_items | supplier_name | YFY   永丰余 | 2 |
| 材质 | material_code_mapping_candidates | new_supplier | 佳丰 | 321 |
| 材质 | material_code_mapping_candidates | new_supplier | 鸣朋 | 245 |
| 材质 | material_code_mapping_candidates | new_supplier | 嘉林亿 | 51 |
| 材质 | material_code_mapping_candidates | old_supplier | 佳丰 | 321 |
| 材质 | material_code_mapping_candidates | old_supplier | 鸣朋 | 245 |
| 材质 | material_code_mapping_candidates | old_supplier | 嘉林亿 | 51 |
| 报价/价格 | material_price_adjustment_batches | supplier_name | 苏州佳丰 | 1 |
| 报价/价格 | material_price_adjustment_batches | supplier_name | 苏州嘉林亿 | 1 |
| 报价/价格 | material_price_history | supplier_name | 苏州嘉林亿 | 129 |
| 报价/价格 | material_price_history | supplier_name | 苏州佳丰 | 126 |
| 报料/采购 | material_requisitions | supplier_name | 苏州嘉林亿 | 24 |
| 报料/采购 | material_requisitions | supplier_name | 昆山鸣朋 | 15 |
| 材质 | materials | supplier_name | 苏州嘉林亿 | 137 |
| 材质 | materials | supplier_name | 昆山鸣朋 | 126 |
| 材质 | materials | supplier_name | 苏州佳丰 | 126 |
| 材质 | materials | supplier_name | UAT测试纸板20260728 | 1 |
| 订单/历史 | sales_order_items | snapshot_supplier_name | 昆山鸣朋 | 104 |
| 订单/历史 | sales_order_items | snapshot_supplier_name | 苏州嘉林亿 | 103 |
| 报价/价格 | supplier_flute_price_rules | supplier_name | 昆山鸣朋 | 5 |
| 报价/价格 | supplier_flute_price_rules | supplier_name | 苏州佳丰 | 5 |
| 报价/价格 | supplier_flute_price_rules | supplier_name | 苏州嘉林亿 | 5 |
| 报价/价格 | supplier_material_base_prices | supplier_name | 苏州嘉林亿 | 86 |
| 材质 | supplier_material_rule_configs | supplier_name | 苏州嘉林亿 | 7 |
| 材质 | supplier_material_substitution_rules | supplier_name | 苏州嘉林亿 | 19 |
| 材质 | supplier_paper_codes | supplier_name | 苏州嘉林亿 | 18 |
| 材质 | supplier_paper_codes | supplier_name | 昆山鸣朋 | 12 |
| 材质 | supplier_paper_codes | supplier_name | UAT测试纸板20260728 | 2 |
| 报料/采购 | supplier_requisition_orders | supplier_name | 昆山鸣朋 | 34 |
| 报料/采购 | supplier_requisition_orders | supplier_name | 苏州嘉林亿 | 13 |
| 供应商主档/规则 | suppliers | name | 固丽纸业(上海)有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 昆山市诺尔特包装材料有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 昆山鸣朋纸业有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 永丰余纸业（上海）有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 苏州一诺包装有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 苏州佳丰纸业有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 苏州市科能纸品有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 苏州市金东浩纸业有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 苏州聚晟达电子材料科技有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 苏州顺厚金属制品有限公司 | 1 |
| 供应商主档/规则 | suppliers | name | 达成包装制品(苏州)有限公司 | 1 |

## 目标供应商引用：佳丰

- 匹配的 P0-08 新供应商主档 ID：`[]`
- 匹配的旧 `suppliers` 表 ID：`[1]`
- 匹配的旧来源系统供应商 ID：`[9]`
- 匹配的材质 ID：`[169, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215, 216, 217, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228, 229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239, 240, 241, 242, 243, 244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260, 261, 262, 263, 264, 265, 266, 267, 268, 269, 270, 271, 272, 273, 274, 275, 276, 277, 278, 279, 280, 281, 282, 283, 284, 285, 286, 287, 288, 289, 290, 291, 292, 293, 294]`
- 直接/材质关联命中合计（非去重）：`31159`
- 已知关系链命中合计（非去重）：`0`
- 计数说明：同一业务行可能通过供应商文本和材质关联等多个字段重复命中；正式判断应查看下方逐表逐字段结果。

| 类别 | 表 | 字段 | 依据 | 行数 | 匹配值/ID |
|---|---|---|---|---|---|
| 订单/历史 | legacy_ruida_order_items | supplier_name | supplier_text | 30125 | [{"value": "JF   佳丰", "row_count": 30125}] |
| 材质 | material_code_mapping_candidates | new_supplier | supplier_text | 321 | [{"value": "佳丰", "row_count": 321}] |
| 材质 | material_code_mapping_candidates | old_supplier | supplier_text | 321 | [{"value": "佳丰", "row_count": 321}] |
| 报价/价格 | material_price_adjustment_batches | supplier_name | supplier_text | 1 | [{"value": "苏州佳丰", "row_count": 1}] |
| 报价/价格 | material_price_history | material_id | material_linked_supplier | 126 | {"169": 1, "170": 1, "171": 1, "172": 1, "173": 1, "174": 1, "175": 1, "176": 1, "177": 1, "178": 1, "179": 1, "180": 1, "181": 1, "182": 1, "183": 1, "184": 1, "185": 1, "186": 1, "187": 1, "188": 1, "189": 1, "190": 1, "191": 1, "192": 1, "193": 1, "194": 1, "195": 1, "196": 1, "197": 1, "198": 1, "199": 1, "200": 1, "201": 1, "202": 1, "203": 1, "204": 1, "205": 1, "206": 1, "207": 1, "208": 1, "209": 1, "210": 1, "211": 1, "212": 1, "213": 1, "214": 1, "215": 1, "216": 1, "217": 1, "218": 1, "219": 1, "220": 1, "221": 1, "222": 1, "223": 1, "224": 1, "225": 1, "226": 1, "227": 1, "228": 1, "229": 1, "230": 1, "231": 1, "232": 1, "233": 1, "234": 1, "235": 1, "236": 1, "237": 1, "238": 1, "239": 1, "240": 1, "241": 1, "242": 1, "243": 1, "244": 1, "245": 1, "246": 1, "247": 1, "248": 1, "249": 1, "250": 1, "251": 1, "252": 1, "253": 1, "254": 1, "255": 1, "256": 1, "257": 1, "258": 1, "259": 1, "260": 1, "261": 1, "262": 1, "263": 1, "264": 1, "265": 1, "266": 1, "267": 1, "268": 1, "269": 1, "270": 1, "271": 1, "272": 1, "273": 1, "274": 1, "275": 1, "276": 1, "277": 1, "278": 1, "279": 1, "280": 1, "281": 1, "282": 1, "283": 1, "284": 1, "285": 1, "286": 1, "287": 1, "288": 1, "289": 1, "290": 1, "291": 1, "292": 1, "293": 1, "294": 1} |
| 报价/价格 | material_price_history | supplier_name | supplier_text | 126 | [{"value": "苏州佳丰", "row_count": 126}] |
| 材质 | materials | supplier_name | supplier_text | 126 | [{"value": "苏州佳丰", "row_count": 126}] |
| 审计 | operation_logs | details | audit_text_redacted | 7 | "" |
| 报价/价格 | supplier_flute_price_rules | supplier_name | supplier_text | 5 | [{"value": "苏州佳丰", "row_count": 5}] |
| 供应商主档/规则 | suppliers | name | supplier_text | 1 | [{"value": "苏州佳丰纸业有限公司", "row_count": 1}] |

### 已知关系链检查

| 类别 | 检查 | 行数 |
|---|---|---|
| 来料/收料 | incoming_by_requisition | 0 |
| 来料/收料 | incoming_by_supplier_order | 0 |
| 库存 | inventory_movements_by_requisition | 0 |
| 库存 | inventory_movements_by_supplier_order | 0 |

### 因旧副本缺表/缺字段而跳过

_无记录。_

## 结论边界

- 该结果仅用于 P0-08A 家庭预审和完善工厂重跑清单。
- 不能据此在正式库停用、删除、改名或替换任何供应商。
- 不能把佳丰的材质、价格、报料、来料或库存自动转给胜源/森林阳光。
- `suppliers` 是仍被历史业务外键引用的旧表；P0-08 新迁移只能新建`supplier_master_records` / `supplier_master_aliases`，不得覆盖旧表。
- 工厂端须对最新正式库先制作隔离副本，再用相同命令重跑并保存新的 SHA-256、Alembic、完整性、外键和引用报告。

## 工厂隔离副本重跑示例

```powershell
python scripts\audit\q0_04_supplier_readonly_audit.py `
  --database "<工厂最新正式库的隔离副本.sqlite3>" `
  --source-label "工厂最新正式库隔离副本；填写复制时间和正式版本 SHA" `
  --json-output docs\migration_reports\Q0-04_supplier_audit_factory_copy.json `
  --markdown-output docs\migration_reports\Q0-04_supplier_audit_factory_copy.md
```

## 辅助附注（不属于主证据）

- 项目根目录旧副本 revision=t68n0r1s7u50、大小=222134272、SHA-256=E3A97229B754A60FFA10FF13F56A679C7D27446E12196E4E2DF0E01C76B1F058，仅用于早期脚本冒烟，不作为本报告主证据。
