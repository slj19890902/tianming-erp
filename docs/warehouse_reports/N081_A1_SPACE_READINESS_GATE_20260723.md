# N081-A1 库位空间放置就绪门禁

日期：2026-07-23

基线：`origin/factory-current-baseline@7d8dc5e87a25b7c2632ef76ff4eb052f1dc4e0a6`

分支：`codex/n081-a-space-readiness-gate-20260723`

## 1. 目标

本闭环只解决“库位是否已经具备可入库、可放栈板、可盘点的明确物理位置”。

- `placed`：楼层、区域、存储方式完整；三楼库位还必须存在平面图布局。
- `unplaced`：库位记录可以保留和继续补资料，但不能承载新增正式库存、当前栈板或盘点单。
- 不改变正式库存批次是订单抵扣唯一事实来源的规则。
- 不创建新的库存余额体系，不开始 N081-B1 文件导入或正式盘点入账。

## 2. 迁移

Revision：`ch64v8x9z53`

Down revision：`cg63v8x9z52`

迁移行为：

1. 为 `warehouse_locations` 增加非空 `placement_status`。
2. 完整既有库位回填为 `placed`，其余回填为 `unplaced`。
3. 保留 `C1-R12/C1-R13` 原有 ID，将其补为三楼 C1 固定地面位。
4. C1 右侧从 11 格等比例扩展到 13 格，不覆盖人工修改过的布局。
5. 阻止正式活动/冻结批次和当前栈板引用未放置或停用库位。
6. C1-R12/R13 一旦产生库存、栈板或人工布局，禁止破坏性降级。

## 3. 页面与服务

- 全部库位台账新增“存储方式”选择。
- 未放置库位显示“未放置，禁止入库”。
- 成品、半成品入库的区域/库位候选不显示未放置库位。
- 库存服务对绕过前端的未放置库位请求返回 409。
- N035 盘点列表不显示未放置库位；按 ID 查询或提交盘点同样返回 409。
- N081 只读报告新增 `locations_by_placement` 和 `location_unplaced`，并忽略停用历史库位的主数据缺口。

## 4. 隔离数据库验证

源副本：

`D:\tm-uat\n081_floor3_placement_20260723_125337\carton_erp_uat.sqlite3`

人工验收副本：

`D:\tm-uat\n081_space_readiness_20260723_145244\carton_erp_uat.sqlite3`

源副本再次复制前后的 SHA-256：

`8B475C78A793BA5C5C130CDE8D19BF22922A4B40B70BB521398C12DC733836AC`

升级后：

- Alembic：`ch64v8x9z53`
- `quick_check=ok`
- `foreign_key_check=0`
- `placed=398`
- `unplaced=2`
- C1-R12/R13：`floor=3`、`area=C1`、`storage=ground`、`source_version=V11`
- C1 右侧布局：13 格
- SF-TEMP：`unplaced`

只读报告扫描前后大小和 SHA-256 一致，`database_written=false`。

## 5. 人工 UAT

地址：`http://127.0.0.1:18084/warehouse.html`

账号：`codex_uat`

密码：`123456`

建议确认：

1. “库位管理 → 三楼平面图 → C1 区”可以看到 C1-R12 和 C1-R13。
2. 点击 C1-R12/C1-R13，空位详情仍可正常使用“绑定货物”。
3. “全部库位台账”中 SF-TEMP 显示“未放置，禁止入库”。
4. 新增/编辑库位时能选择地面位、货架位、临时位；资料留空时为未放置。
5. “入成品仓”的库位候选包含 C1-R12/C1-R13，但不包含 SF-TEMP 或 3D00001。

## 6. 自动验证

- 定向测试：`81 passed`
- Python 编译：通过
- JavaScript 语法：通过
- Alembic head：仅 `ch64v8x9z53`
- `git diff --check`：通过
- 浏览器 Console：无 error/warn

人工验收已通过；允许形成独立提交并推送独立 `codex/` 分支，但仍禁止直接部署到工厂正式目录。
