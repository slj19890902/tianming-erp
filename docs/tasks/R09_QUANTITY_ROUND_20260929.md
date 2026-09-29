# R09 待补送双数量闭环（2026-09-29）

## 范围

修复持续待补送列表与分配预览把客户需求数量、预占客户信用和仓库实物数量直接比较的问题。沿用订单冻结的客户/实物换算、普通送货库存资格和原发货/取消事务，不新增 migration，不回写历史记录，不放宽客户、状态、报料阶段、预占或库存身份门禁。

## 红灯证据

审计匿名夹具建立冻结合同“客户 1000 对实物 500”。普通送货 `_delivery_remaining_quantity` 返回 1000，修复前 `delivery_backlogs.physical_ready` 返回 500。隔离复现结果为 `1 failed, 1 passed`，失败断言 `assert 500 == 1000`。

## 实现合同

- 每条 `finished_order` 预占按 `requirement_quantity_denominator` 读取精确客户信用；同时受该预占剩余实物和批次实际 `quantity_reserved` 封顶。
- 多批信用与实物先分别精确合计，再按订单冻结比例取可组成的完整客户单位；不四舍五入。
- 继续排除取消预占、冻结/非活动批次、暂存占用、异客户、异产品、BOM 主体批次及组件预占。
- 自由成品仍调用原 `finished_inventory_candidates`，因此既有报料阶段门禁不变；只把通过门禁的实物数按冻结比例投影为客户单位。
- 列表和预览数量继续以客户单位作为可填写/可发货上限，并新增 `customer_unit`、`physical_unit`、`ready_physical_quantity`、`available_finished_physical_quantity` 供页面明确双数量。
- 实际发货、待补送核销、取消恢复与打印继续走原送货事务。测试证明客户送货 400 对应实物 200，打印客户行 400、实际货物行 200；取消后待补送恢复 1000、实物可补恢复 500。

## 隔离验证

测试使用 `D:/ERP-AUDIT/20260929-comprehensive/run_isolated_tests.py` 审计钩子，并把源码指向本工作树。运行时清除 `ERP_*`、`TM_ERP_*` 及外部 API 密钥；数据库、附件、备份、日志和临时文件全部位于每轮新建的 `synthetic-tests/<time>`；socket、子进程及边界外写入被拒绝。

- 新增 `tests/test_r09_quantity_round.py`：1:2、2:1、精确分母、多批合计、列表/预览、实际发货、取消与打印，4 passed。
- `tests/test_package_d_r09.py`、新 R09 测试、`tests/test_composite_component_delivery_quantities.py` 及审计复现用例合跑：14 passed。
- 较宽回归曾合跑 37 例；其中 34 passed，2 个既有 `test_direct_external_finished.py` 用例因测试请求使用旧 `lines` 字段收到 400（期望 409），与本次改动文件和路径无关；另 1 个 R09 monkeypatch 兼容失败已在实现中修复并由上述 14 例通过确认。

## 未覆盖与交接

- 审计报告中“释放预占后自由成品为 0”的样例仍受既有报料阶段门禁阻止；该样例尚未确认为合法业务路径，本轮没有放宽门禁。
- `static/index.html` 由主线程单独串行修改：列表和预览应显示客户单位，并用新增字段并列显示实物数量/单位；输入值和 `max` 继续使用客户单位。
- 未运行正式页面、实体打印、真实手机或正式数据库验收；未迁移、未发布、未推送、未写 NAS。
