# 单层原纸入库支持（候选，未正式发布）

## 范围及实现

- 分支：`codex/plain-paper-20260914`；基线：`4435a2ff4fff3c68492555a46809d00a214ab847`，正式版本仍为 v0.22.404。
- 电脑与手机共用 WarehouseGoods 新增“单层原纸 / 白卡”，固定无楞，按张盘点、按张计入库成本。
- 必选启用供应商、品种/克重、长宽、数量、日期及人民币含税到库每张成本；无报价不猜金额，不借用瓦楞材质报价。
- 以既有 sheet ledger 的 `raw_board` 保存原纸，`layer_count=1 / flute_type=NONE`；不创建可送成品，不重复生成采购应付。已模切完工的 TH/22000015 仍按成品入口。本轮不改其主档或历史成本。
- 每批成本独立冻结，含供应商身份/版本、操作人、日期、元/张口径；供应商改名不覆盖旧快照。库存成本仅管理员/老板可见，员工不能执行本入库接口。
- 保留正式 location_id、布局版本、客户权限、幂等、事务、审计；未改变地图或草稿。
- 手机货位页、手机盘点页、桌面正视图统一把 NONE 显示为“无楞”。

## 验证

- `pytest tests/test_plain_paper_inventory.py tests/test_warehouse_goods.py -q`：25 passed。包含单层合法/非法输入、冻结单价、真实供应商、幂等重放/异载荷、版本冲突、停用供应商、员工拒绝与隐藏成本、审计故障全事务回滚、迁移无损和拒绝有损降级。
- TypeScript 编译、Vite 构建成功；shelfDisplay + warehouseInventory 前端测试 65 passed；git diff --check 通过。
- 单一 Alembic head：mr0914，父 mq0912。
- 最新正式库只读 SQLite backup 至 `D:\erp-plain-paper-isolated-20260914.sqlite3`，升级→降级→重新升级全部通过。286 张业务表、98,429 行数据逐表哈希不变；索引/触发器 SQL 不变；每阶段 integrity=ok、FK=0。正式库只以 mode=ro 打开。
- 两项旧回归在未修改的 4435a2ff endpoint 下仍失败，非本次新增：`test_selected_current_price_frozen_as_batch_settlement` 的旧 estimate_basis 断言（金额仍为1.2）；`test_mobile_goods_uses_real_material_snapshot_for_semi_and_raw` 仍期望把材质作为产品编码，与现有分离显示不一致。未删除或放宽断言，后续按原任务处理，不宣称全量测试通过。复核脚本留在隔离工作区 data/audit/plain_paper_baseline_checks.py。
- 未执行正式页面自动点击验收。

资产 SHA-256：

- WarehouseGoods-X3sYe29_.js：FE18C2858C2F2F94F1B91A676C86B74FCE3CED8C630001563AA938D76792E748
- mobileGoods-Cu_ZDJFe.js：F1D0DCDC1A4A8566E7C64197E419DF610A79BEC823884D97C401D2A1EDDE9AB5
- warehouseTwin-Bp1xhdCw.js：1E08D4BF288D5893A713991F9B81E0B6DB58FE7246479E2479B984CE2F6C9F8E

## 未发布原因和恢复条件

正式管理器当前包为 3f18d5e24b780f07deb6ecc0c201968595ddf38757bcafd9f40429ed23101668。D盘可用14,281,916,416字节，现有备份门禁需15,508,892,852字节，尚不含新候选完整构建/暂存。未尝试停服，未降低空间或备份检查，未删除/搬移旧数据及恢复包，未正式迁移、未改正式业务数据、未改 NAS latest feed。

释放足够发布工作空间后，重新核对实时正式基线及另一个 DELIVERY405 候选；本分支未包含它，不能覆盖或称它已上线。再按工厂规范做新时点验证备份、签名构建与迁移包、恢复准备、正式升级和只读健康/静态资源核对。

上线后管理员人工验收：原材料→单层原纸/白卡→选择灰底白板供应商，填写真实原纸品种、尺寸、张数及每张成本；保存后刷新核对货位“无楞/张”和批次成本，员工账户不得看到成本。不得用模拟数量在正式库验收。
