# 000120有限组套更正候选与真实阻断（Sol）

范围：唯一新服务 app/services/emergency_kit_correction_20261014.py、专用合成测试和 managed probe；没有修改既有业务文件、模型或DDL。根授权方案为订单10552、原下盖666/上盖1421各50、原位1918、成套3863输出50。开发代码可以审阅，但实际数据不满足活跃订单前提，不能作为可直接正式执行的更正入口。

## 真实订单阻断

指定原始隔离基线10:49的全部customer136/product3863订单只找到一行：item10552，order9782/TM20261013001，qty50、delivered0、force_closed=false，created2026-10-13 03:54:45；订单status=cancelled。compiled graph一份，snapshot40父3863/41下盖3496/42上盖3787，schema5，配方1+1。没有另一条未取消的新明细可替换target。

全行及compiled资料证据：`/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/stock-governance-sol/000120-actual-orders-readonly.json`。原assemble_subkit_inventory同样明确拒绝cancelled/closed，不能仅删新服务guard成功。按根最新指令保留门禁、不复活取消订单、不让旧取消订单承载新发货。用户确认现存上下盖实际用了50套，不等于授权复活该订单。必须由根决定合法新目标或另一个可审计的有限历史物理组套合同，然后重新冻结范围并演练；本候选不自动改target。

真实 managed 任务 task-fdd39545536e47cd9f8cc01c11aad100 使用既有run_task从唯一基线新建owned child，preview即409“订单身份、数量、送货或结束状态已变化”，没有拆批、身份确认或组套。完整rollback，前后逻辑指纹1213045942c012c3fae9d3c6c42635c7f4719b4d3fefae2762cda5e45efaab2b；原baseline SHA仍ab607153accc3950c5c0f14e4584b559226e813e8243e5140cdb32e5c753b4b4。证据同目录 emergency-kit-managed.json。失败现场按run_task规则保留，没有新建另一个无所有权副本，没有绕过地图、fixed shelf或结束状态。此结果不是实际成功演练。

## 候选合同与调用

`preview(db, actor=admin, order_item_id=10552, quantity=50, target_location_id=1918)` 只读。校验客户136、父3863、被冻结的1+1配方、当前三个产品basis与订单expected完全相等；原批皆active/finished/boxes/原位1918、原可用210且无占用/消费/报损/报废；下盖666为v1且原basis=NULL，上盖1421完整basis与订单相等。异常有效预占、其他待送引用、实际fixed-shelf集货均拒绝。preview返回版本、来源成本、原basis、当前/冻结basis与hash、确定数量及不确定项。

旧1027/lot666/50的planned分配允许出现在preview并明确展示。execute要求外层先退休此旧目标待送行并flush，之后在同一事务重新preview取hash；服务不退休该行、不改原allocation87的planned/consumed/restored，不修改拿货状态或送货。根专用外层负责此步骤，不能调用只接受current行的通用替换函数去再次退休。

`execute(db, actor=admin, preview_hash=..., operation_key=..., evidence=用户确认原文, physical_match_confirmed=True)` 使用atomic_bom，锁订单、重查幂等、核hash并CAS原批/产品；沿既有split_for_correction在同库位从666拆50，child identity_preview/confirm_identity仅确认child50，剩余666的160及NULL basis不变。再沿assemble_order_inventory从child50和1421取50，expected_outputs仅3863:50。没有新matcher、没有额外工艺或虚构移位，没有commit。权限、客户范围与幂等是内部服务前置。

后置核验两份真实BomAssemblyInput及数量/成本、666剩160且消费0/basisNULL、1421剩160且消费50/原basis不变、child可用0/消费50/仅此child有确认basis、原两批材质/成本/位置不变、新父件50/完整冻结basis/订单和来源绑定；来源参考成本总和与新父件成本一致，并调用既有require_inherited_entry_cost新入库guard。不将参考成本提升为现金采购成本。

完整幂等回执使用现有FinanceIdempotencyRecord：唯一key、request_hash、actor、action、order scope，TEXT保存完整request+receipt+preview，flush无commit。OperationLog仅摘要含finance_receipt_id、hash、assembly/input/output及数量。重放不读取可能截断的OperationLog JSON；坏Finance回执409，不能再次拆批。Finance行、split、identity确认、组套、日志和外层业务均随失败/rollback撤销。

## 开发验证

专用合成27通过：仅child50确认及剩160历史不变、同key重放、不同内容/管理员拒绝、OperationLog只剩receipt id仍能重放、坏完整回执不重复、新订单目标客户错误拒绝、49/51/布尔数量/错订单/错货位、来源版本/当前master/价格/订单量/原basis变化拒绝、有效预占/消费/报损/报废、assembly后和最终audit异常原子回滚、6种结束状态不复活、caller rollback无服务commit。fixture为本任务独立临时合成SQLite，没有复制实际数据。

与实物组件成本和父BOM成本相邻合计46通过（6.64秒）：

```text
/Users/kimiyf/ERP-Migration/venv/bin/python -m pytest -q --tb=short \
 tests/test_emergency_kit_correction_20261014.py \
 tests/test_stock_v3_components_20261014.py tests/test_bom_entry_cost.py \
 --basetemp=/tmp/stock-v3-kit-correction-final-pytest
```

合成fixture初始缺真实name、订单snapshot name、last_movement以及BOM父交付参数的setup错误已经修正，未当业务红例。真实canceled订单是唯一managed失败证据，不能通过改fixture替代。此处未宣布正式发布、数据更正或真实成功；git diff --check通过，未push、未访问正式DB、未控制服务、未做NAS大复制。
