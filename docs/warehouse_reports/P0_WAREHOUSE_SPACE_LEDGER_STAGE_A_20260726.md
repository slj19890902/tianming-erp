# P0 通用楼层、区域、库位台账：阶段 A 实施报告

## 范围

- 只实现楼层、区域、容量、库位台账和建设进度。
- 新库位固定为“待布局”，没有真实平面图时不生成任何示意布局。
- 不实施阶段 B 的三楼兼容映射、阶段 C 的全业务区域选择或阶段 D 的逐区启用。
- 不修改三楼既有正式库位编号、库存、栈板、预占、移动和流水事实。

## 分支与依赖

- 分支：`codex/p0-floor-area-location-ledger-20260726`
- 工厂正式起点：`7792f6f246a1ef8428beebebd0d8075e91ee9f4c`
- 直接开发基线：已人工验收 N041 候选头
  `4421a9564fc139d549e01a64de552620b8cd39fe`
- 发布顺序：先发布 N041 的 `cp72v8x9z61`，再发布本候选的
  `cq73v8x9z62`。

## 数据库

- 新 revision：`cq73v8x9z62`
- `down_revision`：`cp72v8x9z61`
- 新表：`warehouse_floors`、`warehouse_areas`
- 不向新表预置或猜测现场楼层、区域、容量和库位。
- 不修改 `warehouse_locations` 既有行和正式 `location_id`。
- 存在楼层或区域事实时 downgrade fail-closed。

隔离副本已完成：

`co71v8x9z60 → cp72v8x9z61 → cq73v8x9z62 → co71v8x9z60 → cp72v8x9z61 → cq73v8x9z62`

各阶段 `integrity_check=ok`、`foreign_key_check=0`。

## 开发侧 UAT 证据

隔离地址：`http://127.0.0.1:18108/`。

开发侧在隔离副本建立：

- 楼层：`1F-UAT`
- 区域：`U1`，规划库位 6，规划栈板 4
- 库位：`1F-U1-L01`

结果：

- 区域进度为已录 1、已布局 0、待布局 1、占用栈板 0。
- 新库位显示“待布局，禁止入库”。
- `U1` 不出现在正式成品库存区域候选和三楼平面图区域候选。
- 浏览器 Console 无 error/warn。
- 浏览器 UAT 前后 `inventory_lots`、`inventory_pallets`、
  `inventory_reservations`、`inventory_movements` 逐表哈希一致。

## 自动验证

- P0 台账、迁移与库位 API 专项：`37 passed`
- 仓库、生产、盘点、N081/N041 迁移扩大回归：`103 passed`
- Python 编译：通过
- 前端内联 JavaScript 语法检查：通过
- Alembic：唯一 head `cq73v8x9z62`
- `git diff --check`：通过

## 待老板人工 UAT

本报告不代表老板验收通过。回家后统一验证：

1. 楼层和区域可用折叠表单快速建立。
2. 区域进度数字符合规划值和已录库位数。
3. 新建库位只能选已登记的楼层、区域，并固定显示“待布局”。
4. 待布局库位不会出现在正式入库、移位、拿货或三楼平面图候选。
5. 三楼现有库存和货位操作保持不变。
