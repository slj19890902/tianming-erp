# YG-20261015-001 客户类别遗漏修复

用户最初称图号遗漏，随后明确：类别没有体现在送货单明细；以80012130为例，要求立即解决。2026-10-16用户明确授权将已验证修复发送至维护聊天，统一发布并执行定点更正。

## 核实结果

隔离来源：/Library/TianmingERP-Closure-v644-20261016/latest-owned/source.sqlite3。
送货155，YG-20261015-001，customer137，pending/version2，总量8550。
当前明细1101/order_item10610/product3564，编码80012130，30片，图号7942511正确；类别NULL，原始资料存在B/BT冲突。
常用箱3564/version8已明确类别BT（2026-10-15 10:10:13数据库时间），送货创建时间2026-10-15 10:01:08。旧订单及送货冻结空类别没有随后更新。
同单其他当前行未发现“冻结类别缺失而当前明确类别可补”的情况。
另有customer138同编码产品3509，不能按编码跨客户取值。

## 变更与不变量

- 新送货从订单继承资料时，只有类别NULL且产品身份相符、当前主档有明确类别才补齐；记录product_id/version依据。不猜取冲突旧资料，已有值和显式空字符串保持。
- 既有送货修订继续优先沿用上次送货快照，不随主档漂移。
- 明细API与桌面明细展示送货冻结客户类别；冲突留空显示待核对，不把生产箱型代作客户类别。
- 定点execute服务仅更正155/1101/3564：NULL→BT，version2→3；保留图号7942511、数量30、全部其他单据/订单/库存/金额；审计与幂等同事务。调用者须先验证当前备份，再提交。
- 明确边界：未改migration，eq1015sa唯一head；未改正式库，尚未部署。

## 验证

证据目录：/Users/kimiyf/ERP-Migration/evidence/delivery-category-20261016。
定向用例验证新建HTTP、明细、打印、修订冻结、显式空值、错误身份、前导零及取消旧覆盖字段；JS语法通过。
真实副本脚本verify_correction.py验证权限拒绝、旧版本拒绝、审计故障回滚、外层回滚、提交、幂等重放、明细/打印均BT；所有表摘要比较只允许sales_deliveries/sales_delivery_items/operation_logs/finance_idempotency_records变化。完整性ok。
受管回执：/Users/kimiyf/ERP-Migration/managed-delivery-category-20261016/task-81bcb9efd91f45ae96f1334d2135a8c8/result.json。测试成功，清理器因无法证明其他ERP进程退出而保留约229MB副本，不强删。
旁支旧测试test_five_decimal_sale_survives_delivery_receipt_and_statement仍使用已过期直接dispatch调用；test_delivery_inline_edit_frontend的Vue测试桩缺app.use。均在未改本次代码的维护基线同样失败；不借机改无关测试/业务。Node需明确加入PATH。

## 正式交接

从origin/factory-current-baseline建立独立分支后，快进至实际v644源码cf9888f4，避免旧远端v625代码覆盖。分支codex/delivery-drawing-20261016。
由维护聊天统一纳入下一发布；发布前重新核实正式155/1101/3564状态，严格命中才执行，任何变化停止重审，不放松条件。
调用app.services.delivery_category_correction_20261016.execute(db, actor=活动管理员)，调用者负责备份验证和commit。该服务不是自动migration、启动钩子或通用批量回写入口。
验收：展开YG-20261015-001明细，80012130客户类别BT；打印预览同样BT、图号7942511、数量30。
