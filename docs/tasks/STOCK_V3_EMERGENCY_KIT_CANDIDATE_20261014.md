# 000120既有成品实物组套有限更正（Sol，最终无订单合同）

用户选择保留订单9782/TM20261013001取消状态，按实际上下盖各50组成的50套更正原无订单送货。本服务不创建、复活或使用订单10552，不写送货、拿货、采购或生产表；原下盖666余160的NULL历史basis不补写。原订单候选已整体替换，仅此合同可以调用。

## 固定事实与接口

唯一范围：客户136、下盖3496/批666、上盖3787/批1421、父套3863、数量50、原实物库位1918。初始两来源均available210且reserved/consumed/damaged/scrapped为0，下盖v1/basisNULL，上盖basis等于此次冻结的当前真实实物basis；父套冻结配方必须为上下盖各1。异常预占、其他有效待送引用、实际集货、客户/身份/位置/版本/成本变化均拒绝。下盖实际来源参考单价11.4627，上盖当前入库参考单价1.3640：573.1350+68.2000=641.3350，每套12.8267，CNY、actual=false，不重算旧历史成本。

```python
proposal = preview(db, actor=admin, product_id=3863, quantity=50,
                   target_location_id=1918, lower_lot_id=666, upper_lot_id=1421)
receipt = execute(db, actor=admin, preview_hash=proposal['preview_hash'],
                  operation_key='由根外层冻结的唯一标识', evidence='用户确认原文',
                  physical_match_confirmed=True)
```

返回output_lot_id、split_lot_id、source_record_id、product_id=3863、quantity=50、location_id=1918、reference_total_cost='641.3350'、reference_unit_cost='12.8267'和replayed。活动管理员及客户权限必需。服务不commit；外层事务同时负责送货行替换，晚期失败全部回滚。

preview允许展示唯一旧1027/lot666/50的planned引用。execute之前，根外层必须在同事务先退休1027并flush，再重新preview冻结hash；原allocation87 planned/consumed/restored及拿货历史保留。不能用旧hash执行，服务不替外层退休记录。

## 真实投入与可追溯来源

沿既有split_for_correction原位拆666的50，只对child执行identity_preview/confirm_identity并比对此次冻结basis；666剩160仍basisNULL。使用CAS版本与真实consume流水投入child50、1421的50，均不关联订单或送货，不以adjust伪造消耗。沿manual_finished_in在1918生成父套50，source_type=transfer，明确source_ref_type和cost_snapshot_source=`physical_kit_correction_20261014`，ref指向同一FinanceIdempotencyRecord。

Finance先flush取得id，最终同一事务保存完整request、preview、输入lot/版本/before-after/真实movement、typed拆批、冻结父子basis及成本、输出入库movement和完整receipt；OperationLog只写短摘要与Finance id。相同key/actor/payload重放并验证完整来源，内容/管理员变化或损坏Finance均拒绝，不依赖可能截断的OperationLog。

entry_source_valuation仅增加该明确来源分支，委托validate_source_cost。核查两路真实consume及数量、实际split、父套1+1 child basis hash、原入库流水、Finance/hash/输出绑定、CNY成本守恒。原输出以及合法移库/拆分后裔均可读取冻结成本；通过typed转移链回到唯一原输出，允许正常qty/version/location变化。不以未来current master覆盖历史，不要求已送或已移库批永远等于初始50/v1/1918。跨伪源、循环、缺链、错误consume/hash/basis或输出总量不守恒拒绝。

core/inventory_entry_guard仅新增该typed ref的after_flush跟踪，以及`frozen_assembly_spec`明确规格分支。该字段只有完整Finance来源验证成功才返回，包含两个真实成品子spec/basis；guard逐项匹配父assembly的数量和basis hash。没有伪装assembled-entry-v1纸板规格。合成ReceiptPriceGuardSession实际commit验证成功；伪造Finance或缺子spec的commit 422且rollback恢复原库存。

## 验证与边界

专用41测试和组件/父BOM相邻19测试共60通过：固定50范围、权限、子批确认及剩160历史、版本/master/cost/basis变化、幂等完整回执、真实consume来源、合法拆分/移库、未来master变化、伪源/循环/缺链/数量异常、晚期错误、正式session commit门禁、独立无订单dispatch→cancel只恢复父套50并可重复cancel。后者不恢复已投入上下盖，不实现逆组套。

```text
/Users/kimiyf/ERP-Migration/venv/bin/python -m pytest -q --tb=short \
 tests/test_emergency_kit_correction_20261014.py tests/test_stock_v3_components_20261014.py \
 tests/test_bom_entry_cost.py --basetemp=/tmp/stock-v3-physical-kit-guard-pytest
```

真实演练通过既有run_task从唯一原始隔离基线创建owned child，禁止直接修改baseline。runner默认seed地图曾在manual_finished_in被409拒绝；此失败完整回滚，没有放宽门禁。随后只将已核实真实runtime证据原bytes写入owned child运行路径，成功执行全部合同及幂等重放，并在第二case最终audit之后注入异常，核查全部库存及Finance回滚。最终还用真实ReceiptPriceGuardSession跟踪并明确validate_entries核验新父套。每case全库逻辑指纹回到起点、原baseline SHA始终ab607153accc3950c5c0f14e4584b559226e813e8243e5140cdb32e5c753b4b4；完整证据`stock-governance-sol/physical-kit-managed.json`及managed task receipts/stdout。

run_task自动回收尝试被既有“无法确认 ERP/测试进程已退出”检查阻止，按规则保留owned副本，未绕过保护。首次使用venv符号链接启动runner在准备阶段被trusted Python guard拒绝，已改用canonical Python执行，未将准备失败当业务失败。没有正式访问/修改、服务控制、push、NAS复制。根在整合路径独立完成正式送货154的局部替换、父套销售价税冻结和整单外层回滚审计；本服务自身没有触碰正式154，合成dispatch使用独立测试单。


最终受管任务：`task-8f95f190d0ef45c29f2755fa9e00d708`，returncode=0；成功case明确transaction_entry_guard_passed=true，第二case为late_audit_fault成功回滚。每case全库指纹`b8fa090151cc1c4d87c428d50cd719ea03c46468aa88e8fbd50bf2fb58d00d53`一致。临时输出1432/下盖child1431仅属演练，不能作为正式id。真实runtime文件SHA为`cef9dfd43e8ef2b4d94abdd43c50e631d548e5f02a447041c996709832e1ab1b`。

额外库存计价/旧无订单API回归44例，30通过、14在旧API测试未提交送货快照时被既有dispatch 409门禁拒绝。使用本分支HEAD原guard/cost模块代码重跑代表例，同样409“请刷新页面，先核对送货快照后再确认发货”；证据`stock-governance-sol/unordered-legacy-baseline-test.txt`。没有为旧测试撤掉新送货快照门禁，本次独立服务级dispatch/cancel实际通过。该额外套件未声称全绿，根整单验证使用新合法快照合同。

## 独立复审P2：typed派生数量必须绑定真实移库流水

Astra复审发现：父套50原位拆20后，仅将InventoryLotTransfer.quantity/available_quantity改19，实际child20/origin30及两条location_transfer流水仍20，旧校验错误保留12.8267成本。新增专用测试先复现该失败，未写正式数据。

最小补充仅有限服务：每条派生typed边按已有goods-correction或location-transfer精确幂等键查实际移出/移入流水，验证movement lot/operator/unit/type/quantity、无其他订单/送货/反向用途、五类余额非负及逐项移出/移入delta守恒。原位correction还核同位、无reserved及其可完整重算的已存request_hash；普通partial move使用其实际键和独立可证字段，合法整批selfmove要求只有source流水且五余额不变、全部可用/预占数量相符。任何同源派生损坏，同时拒绝原输出及后裔成本。

InventoryMovement没有历史location/version列：本修复不按movement条数推算lot.version，不发明缺失位置证据、不强制当前qty/version/location等于出生值。typed正版本与source_version_after=before+1保持；真实资料版本变化、正常split、partial move、full selfmove均兼容，普通move即使operation key以correction:开头也不会误判来源。

专用54及组件/BOM相邻19合计73通过。新增13例覆盖typed20→19、缺source/target流水、错source/target余额、错lot、correction版本/hash/位置/key、正常partial/selfmove以及两种真实流水损坏。原无订单dispatch/cancel、真实session commit guard、完整Finance/late fault测试继续通过。本轮只改有限service、专用tests及本报告，其他业务文件未动，git diff --check通过。真实外层双更正的提交/回滚审计由根独立管理，此P2提交未触碰已批准原baseline及正式库存。
