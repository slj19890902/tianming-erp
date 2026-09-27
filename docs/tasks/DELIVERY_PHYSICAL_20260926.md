# 送货双数量、历史校正及模具登记

授权：老板2026-09-26要求按补充方案顺序实施并发布。单代理；每个子闭环独立验证与发布。正式业务更正限已核实清单。2026-09-27老板已完成最新货位盘点并授权继续，当前实物1890片；此前“暂不盘点”和3303理论方案已被后续事实取代。原审计保留溯源，不可作为执行计划。

## 顺序及当前状态

1. 打印HTTP兼容与失败重试：v499技术发布完成，资源/健康/完整性门禁通过，待管理员人工验收。详见DELIVERY_PRINT_HTTP_20260926发布回执。
2. 客户/实物双数量：v504技术发布完成，覆盖订单、备库直送、快照、实物拿货/扣库、预占和财务客户口径；回归及当前实物批次启用见后文。
3. 历史库存：旧3303/+2021/-1700方案已永久停用。管理员9张已审核盘点净-1092后现1890片；7个现存批次已新增零数量单位确认流水，余额、成本、旧单和旧流水不变。不是逐笔历史少扣已全部查明或已补扣。旧单撤销/退回仍须核实真实实物事件。
4. 出库后补库提示：v504技术发布完成，按实际扣库后阈值提示，确认复用待报料；待补/在途覆盖、幂等、权限及灰卡原料路线已定向验证，正式人工验收待反馈。
5. 模具：原231新增及2既有名称更新完成。老板已确认24停用款存在实体、YL共用模具在R04；本轮23停用款补建并关联且保留停用，共用模具161保留一个本体并登记R04。研光80011946与光洋已有242同码同名，尚待确认共用还是各一块，仅剩此1款身份待答。v507已修复停用产品实体模具标签兼容并保留数字排序，实际标签打印待人工验收。

全局完成要求：隔离数量验收全部通过、实际发布、只读健康/资源/数据库检查、可追溯数据回执及管理员最短验收入口。不能以打印子任务完成宣告总目标完成。

## 打印子闭环证据

正式起点v497，Git 30e73dc7，运行包44358c59，数据库dz0922。本次不迁移、不写业务数据。
复用主页面兼容幂等键算法供打印页及管理员设计页加载；打印失败保留相同请求键，同文档并发合并、变更文档/价显独立键、成功后下一次打印新键。只POST原customer-print-events接口。
验证：verify_delivery_print_retry.cjs通过；隔离Chrome实际HTTP下secure=false、randomUUID=undefined，确认打印1次、登记1次、无报错；17项客户资料/模板/加载恢复测试通过。恢复测试补齐浏览器事件及CustomerDeliveryPrint.controls桩，不修改业务预期。
截图和JSON：D:/.codex/workspace_artifacts/delivery_physical_20260926/print。

发布门禁发现并修补：v498新operation-key.js未加app/main.py静态路由，正式资源检查404，因此未宣告交付、未发布NAS latest。v499添加原静态路由循环，新增真实create_app的TestClient测试通过（不用仅挂业务router的模板fixture）；不以拦截资源的Chrome测试替代服务路由验证。发布最终状态另见回执。

## 下一闭环已定位的接口（不可漏改）

- app/api/deliveries.py：DeliveryLineCreate、_validate_delivery_source_contract、_collect_unordered_finished_lines、创建/编辑/已发货修订、_pick_unordered_location_plan、拿货应用picked_quantity回写delivered_quantity、响应及actual_goods_rows。现有计划量/拿货量常被直接当客户量，必须逐入口拆分。
- app/services/unordered_finished_delivery.py：发货planned总量校验、取消、回单短收/编辑重扣必须同一实物快照。不能只改发货校验。
- app/services/external_packaging_receiving.py：备库实收quantity被折为converted_quantity，再作为actual_inventory_quantity；计划进度仍可按客户单位，但真实入库必须保留采购实物及奇数余片。
- app/services/direct_external_finished.py：订单纯外购也先折客户量入库，预占credited与reserved同量；必须与备库一致修复。保留旧已收识别和未入仓历史门禁。
- app/services/semi_finished_inventory.py：consume_delivery_item_inventory将consumed_stock作为客户累计；app/services/warehouse_inventory.py consume_finished_reservation将stock同时写credited_requirement。已有两种字段应复用，不能另建库存账。匹配、预占、撤销、订单可送量需要检查同一比例。
- app/models/delivery.py缺独立数量换算快照；库存FinishedGoodsInventoryDetail已有physical_basis_json，预占已有reserved_stock_quantity/credited_requirement_quantity。新增字段/校正台账若需migration应单独演练，不修改历史快照。
- app/services/stock_replenishment.py receive_replenishment_item支持计划量与actual_inventory_quantity，但目前约束实际量>=计划量；通用比例、单次零完整单位实收、价格守恒须测试。
- 17:59新鲜只读副本before-dual-quantity.sqlite3完整性ok/FK0/head dz0922，00006仍2982。旧收料子批次639–644每批原300，645原221；现余644=100、645=221。搬入656–661各300、662=861。完整迁移血缘、费用来源仍须在执行候选中核验，不凭本记录直接写库。
- 原始采购批次部分成本3.94、部分NULL，搬入批次7.71；不可把所有现有单位成本机械除2，须核对采购冻结总金额和既有成本补充事件。

## 双数量候选进度（2026-09-26，本轮未发布）

已在隔离worktree继续实现：备库直送分配总量按冻结实物量校验；编辑草稿沿用本单最新换算快照；发货重验批次实物口径；短收退库按客户差额换算实物；拿货任务original_quantity使用实物，位置行单位采用冻结实物单位；拿货回写逆换算客户数量并更新快照，不能整除立即拒绝，草稿版本校验同步使用实物数量。

新增tests/test_delivery_physical_quantities.py，最终6 passed（15.91s）；覆盖200片送100/50、主档改为1:3后编辑仍用原1:2、重复确认/取消、199片只送99余1、两次各50、短收10退20及撤销回单重扣、手机拿200实物/部分100回写客户50/拒绝99实物。git diff --check通过。测试通过真实隔离API与库存服务，未用正式页面自动点击。

测试初次因旧P1-15B fixture缺入库尺寸/成本失败；本测试显式补了仅测试用材质报价与报料尺寸，先建立已验证库存，再设置外购身份。该fixture只证明发货链，不证明外购收料或成本换算完成。未修改正式数据、未执行migration、未提交/发布此候选。

待接续：1. 响应actual_goods_lines及纸质拿货/前端输入分配实物；2. 入库保持真实片数及余片，单位成本守恒；3. 普通订单预占/消费双字段；4. 单位快照全入口错误转换、旧任务兼容和迁移隔离往返；5. 历史逐批次校正、预警及模具。不要把6项通过当完整交付。新增ea0926迁移尚未演练，正式仍不能接入这些未闭合数量规则。

## 数量显示和前端录入候选进度（2026-09-26，未发布）

本轮已改API客户/实物投影：送货明细及打印查询读取quantity_contract_json，客户print.items.quantity和unit保持客户口径，actual_goods_lines按冻结实物数量/单位。响应带quantity_contract供重新打开草稿使用。备库候选同时返回实际库存、完整可送客户数、换算口径及历史批次待核原因；未核实旧批次不能误认为新实物库存。

前端选择批次输入客户数量后转换为物理分配，保持客户合计；保存校验比较换算实物量，打开草稿及行内编辑沿用数量快照。界面文案改客户送货数量/可送客户数量，并显示仓库应拿实物。修改草稿的客户单位从旧数量快照保留，不从新主档重取。

验证：tests/test_delivery_physical_quantities.py最终6 passed（16.47s），额外断言候选200实物对应100客户、打印客户100只/实物200片。scripts/validation/verify_delivery_quantity_input.cjs PASS，执行真实前端方法验证100→200、50→100、同款多批合并、1:1混合、序列化重开不重复换算及199对应99，并编译全部内联JS检查语法。git diff --check通过。尚无实际Chrome页面视觉验收，不把方法测试称页面已验收。

下一接点：external_packaging_receiving.py备库分支只在converted_quantity>0时入仓，actual_inventory_quantity传converted_quantity，导致余片遗漏；stock_replenishment.receive_replenishment_item尚无外购实收来源校验/实物口径标识/采购实物单位成本覆盖。direct_external_finished.py同样按converted_finished_quantity入仓并以ratio倍单位成本冻结，预占stock和credited还未拆分。tests/test_p1_140_external_stock_replenishment.py已存在4043实收测试，但旧断言库存2021必须按新业务改成4043且保留计划进度2021/2000及余片1；零完整单位的分批收料测试也要改为每片实物入账。普通订单预占消费需一并贯通。未写上述入库服务，避免把本轮显示修复误记成入库已完成。

正式数据库、历史单据、模具均未修改。本轮无发布，ea0926迁移仍未演练。总目标未完成。

## 备库实收入库候选进度（2026-09-26，未发布）

本轮实现external_packaging_receiving备库分支按每次received_quantity入库，包括converted_finished_quantity=0的余片；converted仍只推进完整客户单位计划。stock_replenishment校验外购实收来源、采购与补库产品对应、入库实物等于实收；不同计量不再机械要求实物>=计划进度。新增external_physical_receipt.py冻结原采购含税货款/采购实物数量为片成本，绑定实收/采购/客户/产品/入库总量，记录实物数量口径；不按新主档售价估值，不翻倍总成本。warehouse_display_units识别实物单位；inventory_entry_guard识别关联实收的冻结采购规格，保留成本和规格校验。

测试：更新test_p1_140_external_stock_replenishment旧数量预期，4043实收库存4043/计划完成2000/完整折算2021/余1、成本8.5每片，原始实收不可变触发器、两次各1片生成两个批次合计2且进度1、重复收料不重复入账。非迁移12项通过。新增test_external_physical_delivery_flow真实API串起采购补库→实收200→无订单送客户100→扣200至0，确认无客户订单、实际库存流水200并主动验证入库成本门禁。最终合跑上述两文件及test_delivery_physical_quantities：19 passed, 1 deselected（42.74s）。排除的是旧专项迁移测试；新ea0926迁移尚未演练，不能宣称迁移门禁通过。git diff --check通过。

普通订单入库尚未改：direct_external_finished仍converted入仓/倍数成本；production_workflow._reserve_component_completion_lot仍reserved_stock=credited_requirement。semi_finished_inventory consume/reverse及coverage仍把stock当客户，warehouse_inventory consume/reverse/release也同步加减两字段。下轮需统一修复并测试。

跨批次余片需正确设计：订单比例1:3，先收2再收4，应实物库存6、可交客户2；不能只对各批独立floor得到1，也不能通过只把单批reserved/credited比值当真正比例。模型已有reservation_group_key、cut_plan_json及整数credited字段（允许0），可复用分组冻结比例/批次偏移记录整组对应，保留批次真实片数和来源成本，不另建库存账。multilevel_bom_external_reversal._reverse_output当前用converted校验并拒绝零整件有库存，必须同步按显式实物快照兼容旧记录；不简单用当前主档翻历史库存。

当前未执行任何正式数据校正、模具登记、迁移或发布。历史4043报料与4042实收差1依旧待核，不因新收料规则自动补历史1片。

## 普通订单双数量候选进度（2026-09-26，未发布，有1项未通过）

已改direct_external_finished.post按实收物理片数入仓，旧external_legacy_stock调用_post_quantity保留physical_entry=False旧口径，不重解释既有旧库存确认入口。新收料成本复用external_physical_receipt冻结采购片成本。新增order_basis只读订单冻结比例和销售/采购单位；build_order_delivery_snapshot新增可选customer_quantity，普通送货创建/编辑调用时保存quantity_contract_json；ensure历史补缺调用不加数量快照，避免历史默写。

production_workflow._reserve_component_completion_lot新增credited_quantity，支持stock=2000/credited=1000。warehouse_inventory消费、逆转、释放按对应的客户量更新requirement字段；semi_finished_inventory主成品coverage和累计已送使用requirement量，消耗/逆转客户目标转换为stock。已验证普通1:1和1:2整批收料1000/2000、预占客户1000、发货实物2000、部分取消客户200恢复400及订单余量200。低级测试也检验新送货数量快照；普通订单端到端API/打印/手机仍需补验，不把服务测试冒充全部入口完成。

multilevel_bom_external_reversal明确区分新实物成本快照quantity_basis=physical和旧converted口径；新直接外购撤销按received_physical_quantity校验并冲回实物，旧记录保留原规则。1:1收1000、1:2收2000、1:2只收1片尚无整客户单位三种撤销/重复撤销测试通过，原实收凭证保留。

最终合跑test_direct_external_finished、test_external_legacy_stock、test_external_physical_delivery_flow、test_delivery_physical_quantities、test_p1_140_external_stock_replenishment -k not migration：31 passed, 1 failed, 1 deselected，79.20秒。失败已明确：test_split_receipt_carries_fraction_and_uses_frozen_ratio，比例1:3先收2再收4，库存合计6正确，但逐批floor预占仅3，应6并支持客户2。测试已改为新正确实物/可送标准，没有跳过或降低验收。git diff --check通过。

下一步必须先解决跨批次分数客户单位的预占、出库和反向链，再进行正式发布。现有Reservation的credited/consumed_requirement/released_requirement为Integer，DeliveryInventoryAllocation也Integer且约束credited>0；不能给跨批分配塞0信用而绕过数量门禁，更不能每批floor或把库存合并伪造新来源。可研究在原双数量表上扩展内部需求精度并冻结精确比例（Fraction用于总量计算，避免1/3有限小数累计误差），客户打印仍整数；不要另建数量账。若改字段需完整迁移门禁，当前ea0926只新增送货快照，尚未演练，不足以支持此扩展。

还需处理：普通订单拿货original_quantity已经用实物，但_pick_location_plan旧算法仍需对应校正；订单actual_goods_lines仍需由冻结数量覆盖；创建订单从既有physical批次预占、超送预占、短收退回及取消链需全回归。历史校正、预警、模具尚未执行。本轮无正式数据写入、无迁移/发布；工作区未提交，不能直接上线。

## 跨批次精确可送量闭环（2026-09-26，未发布）
本轮修复remaining_finished_order_credit_by_item_ids：按订单项与requirement_quantity_denominator分组汇总分子，用Fraction累加，最后一次取完整客户单位，避免新分子直接被读成客户量。旧分母1保持原语义，取消和组件过滤保留。
新增/扩展test_split_receipt_carries_fraction_and_uses_frozen_ratio两组：比例1:3分收2/4片，比例1:2分收1/1/1/1片；首批不足整单位可送0；主档改1:7仍沿用订单冻结比例；完整发货消耗6/4片，部分取消客户1恢复3/2片，可送恢复1。
验证：test_direct_external_finished全文件11 passed（31.36s）；加入首批可送0断言后split两例2 passed（9.08s）。P1-81定向回归2 passed/1 failed：double_splice_receipt及receipt_auto_composite_finished_stock通过；receipt_auto_reserves_only_order_quantity_not_already_covered在manual_finished_in准备测试库存时被既有成本门禁拒绝，缺供应商平方价/价格单位及天地盖单片尺寸，尚未进入收料与新投影。未放宽门禁或跳过该用例，需补有效测试事实并重新验证。git diff --check通过（仅换行提示）。
当前仍未提交、迁移、发布或修改正式库。还需普通订单API/手机/打印投影、超送及其他requirement分母读取回归、迁移完整演练；历史校正、预警、模具未交付。不能将本轮通过称全部完成。

## 普通订单部分收料与客户打印接口（2026-09-26，未发布）
修复P1-81既有回归fixture：独立人工库存补充测试专用平方价材质及展开尺寸，原采购冻结实收价格保持不变；不放宽成本/尺寸门禁。receipt_auto_reserves_only_order_quantity_not_already_covered重新验证1 passed（5.99s）。
新增普通订单真实API测试，先收200片，客户送100。首次发现收料已入仓仍被全单外购收齐门禁409拒绝；现仅对eligible+managed的纯直接外购且无额外必需bound组件允许按已收库存部分开单。仍走原客户、强结、可送量及超量权限校验，测试101客户单位被409拒绝。详情与客户打印actual_goods_lines使用新单冻结实物快照，保留order_item_id；客户打印quantity维持100、实物200。
验证：test_direct_external_finished及test_delivery_physical_quantities合跑18 passed（46.55s），覆盖普通订单创建/打印、收料/撤销、跨批次及备库直送。git diff --check通过，仅换行提示。无正式数据库写入、迁移或发布，候选仍未提交。
下一入口已定位：_pick_item_location_plan把新item.original_quantity实物量传给要求客户量的_inventory_sources_for_order_item；后者finished_current/finished_need及planned_stock用stock代替requirement，必须同源修复；另有批量投影约4137行的同类分配不能漏。跨批次requirement可能分数，JSON表示应明确分子分母，不能直接序列化Fraction或逐批int导致丢失。当前手机/纸质拿货普通订单未验收，不宣称完整发布。超送、迁移、历史校正、预警、模具仍待完成。

## 普通订单拿货与部分拿货回写闭环（2026-09-26，未发布）
_inventory_sources_for_order_item和_delivery_list_standard_inventory_sources共用finished_pick_plan，按客户需求扣除精确已消耗信用，逐批换回真实拿货片数；allocation信用读分母。JSON边界保留numeric显示量并附requirement_numerator/denominator，库存计算不使用浮点显示值。covered_requirement_quantity也按分母显示。
_pick_item_location_plan使用送货冻结客户数量请求库存分配，original_quantity保持实物，已覆盖量按物理片数汇总，单位使用冻结实物单位；不再把实物计划数量当客户量再次换算。旧无快照单保持原路径。纸质拿货页已核对读取location_lines.pick_quantity，尚未做本轮Chrome视觉验收。
真实API测试覆盖比例1:2收200送100，以及1:3跨批收2+4送2：详情、分页列表分配合计200/6且精确客户信用100/2；客户打印100/2，实物200/6；拿货任务同片数/原库存来源，无伪造缺货行。部分拿货100/3，提交并apply回客户50/1，重新打印正确；实际dispatch后消耗100/3，剩余预占100/3。
验证：初轮普通外购+备库直送19 passed（49.06s）；新增列表、部分拿货回写和实际发货断言后两组API最终2 passed（9.43s）；旧分页查询量回归1 passed（4.78s）；git diff --check通过。无正式库写入、migration或发布，候选未提交。
剩余：超送分配/历史allocation读取/创建订单从既有physical库存预占等分母消费者继续审查；ea0926迁移完整演练、实际打印视觉与旧模板回归；历史校正需审计清单确认，不擅自覆盖余额；预警和三客户模具仍待交付。目标保持完整。

## 超量送货实物预占服务（2026-09-26，未发布）
reserve_finished_surplus_for_delivery对eligible纯外购读取订单冻结比例，客户quantity转换physical_quantity后预占；分子take*customer_basis及分母physical_basis保存在原reservation。重放按冻结客户信用比较，不再把reserved_stock数值直接与客户输入比较。添加受限来源：只有eligible且source_type=purchase_reserve/source_ref_type=direct_external_receipt的真实批次可候选，仍校验客户、SKU、非通用、物理口径身份、状态、CAS，生产余货旧1:1保持。
semi_finished_inventory的正向/反向current_surplus累计改为requirement_amount精确和，避免新分子被当客户已送量。数量配置损坏转换为WarehouseInventoryError409。
新测试用真实2000片外购实收，经现有release服务释放原订单预占，再单独验证余货预占服务100客户=200片、重放同reservation、101同key拒绝；consume后库存1800可用/200消耗，reverse+release恢复2000。此为服务测试，不冒充完整外购超送页面链。
验证：旧P0-32超量收料送货全文件与普通外购合跑25 passed（69.95s）；追加实际consume/reverse/release断言后的余货专项1 passed（5.80s）。git diff --check通过（仅换行提示）。无正式库写入、迁移、发布，候选未提交。
下一步已知未闭合：纯外购_delivery_remaining_quantity目前min订单余额/预占，不包含真实余货，外购超送API尚不能触达新余货服务；_inventory_sources_for_order_item的surplus规划仍按stock=credit，候选来源也需同源验证；普通reserve_finished_inventory入口仍quantity同时作stock与credit且physical_basis_json字符串比较，需区分数量标记和真实规格身份。不得只开放超送数值而漏实物候选/权限确认。迁移、视觉、历史校正、预警、模具仍待交付。

## 已预占余货分配与已发货列表（2026-09-26，未发布）
finished_pick_plan增加reservation_type参数，原finished_order默认行为不变；_inventory_sources_for_order_item已预占余货按客户信用累计和冻结比例规划实物，不再stock=credit。新增实际余货service测试断言预占客户100时分配实物200、信用100。
_delivery_list_standard_inventory_sources纳入finished_surplus_delivery，按成品源呈现，草稿已预占余货用同一精确计划，已发货展示实际allocation。修复旧列表漏超送来源问题。新增旧P0-32已发货列表断言：订单600实送602，来源合计602，均为finished，随后取消恢复原可用/预占。
验证：余货专项1 passed（6.14s）；602拿货/发货/列表/取消1 passed（6.89s）；列表查询量+普通外购全文件15 passed（41.80s）。git diff --check通过。未修改正式库、未migration、未发布，候选未提交。
仍未完成：未预占余货来源查询和外购可送数量API还需统一真实实物来源，普通库存抵扣的双数量与身份比较仍需修；ea0926迁移尚未演练，历史校正、预警、模具仍待。不得称外购超送完整页面已打通；本轮仅已预占分配和列表闭环。

## ea0926 留存审计副本迁移演练（2026-09-26，未发布）
完整读取迁移计划、运行手册、检查清单（分段补齐截断输出）；退役legacy抽取/历史batch命令未执行。新增scripts/validation/rehearse_delivery_quantity_migration.py：源限离线workspace_artifacts且必须匹配显式SHA，输出必须新目录；仅复制品以expected_database_path执行真正Alembic upgrade/downgrade/upgrade，不stamp，不连接正式库。
源before-dual-quantity.sqlite3 SHA256 b296156a317a4ded043296c0454f4d23d94f664a59cfead8375b4c62c7ffc3af；原revision dz0922。演练唯一head ea0926，304张原业务表原字段逐行哈希、1026个索引/触发器对象保持，integrity ok/FK0。旧送货快照NULL，两张旧需求分母1；0/-1/NULL拒绝。三份独立复制品分别加入新快照、分母2、分母3，降级出现明确“禁止有损降级”，拒绝前后文件字节哈希不变。源哈希保持。
首次run1完成后加强拒绝原因断言、逐表证据和日志持久化，run2复验通过。完整证据D:/.codex/workspace_artifacts/delivery_physical_20260926/migration-ea0926-run2/evidence.json，原字段hash及schema对象original-facts.json，三份guard日志及三次迁移日志。源为留存审计时点，不冒充最新正式备份；发布前仍须新鲜正式备份复验。
三份迁移文档增加ea0926候选状态。git diff --check通过。无正式迁移、无历史校正、无服务重启/发布，候选未提交。接续普通库存抵扣/未预占余货完整接口、打印视觉、预警、历史校正审核和三客户模具。目标未完成。

## 发货后库存预警读取与阈值边界（2026-09-26，未发布）
现有stock_policy_dict及常用箱快捷摘要从available < warning改为<=，快捷摘要同时检查policy.active。新增四个边界用例：80库存配79/80/81阈值以及停用，完整与快捷投影一致。7项阈值、权限/客户范围、快捷设置、库存统计测试通过（17.27s）。
新增GET /api/deliveries/{id}/stock-warnings：复用_delivery_for_user和deliveries.view，额外warehouse.view；服务delivery_stock_warning_rows仅对dispatched且非历史补录，按关联库存consume/reverse_consume流水净消耗>0的成品SKU，筛本客户启用成品策略。只返回SKU/名称/余额/阈值/目标，不返回价格或其他客户策略。GET不写补库单或库存。
真实备库直送API验证：200实物开客户50，草稿无提醒，真实扣100后余额100等于阈值100返回1条；重复GET一致、待报料数量不变；取消后无提醒且库存恢复200。专项1 passed（6.05s），git diff --check通过。未修改正式数据/策略/历史库存，未发布。
关键接续：现有create_stock_replenishment_order的external分支调用create_external_stock_replenishment_purchase，直接创建confirmed补库单与采购批次，不符合用户“确认提示后只进入待报料”。必须复用StockReplenishmentOrder/Item的draft状态及procurement_route_snapshot=external_packaging接通待报料和后续正式采购，不能把弹窗确认直接绑定现有external确认采购入口。纸板分支原本status=draft可复用。阈值00006单位仍待确认，不改数值、不根据当前比例擅自翻倍。新读取接口尚未接前端弹窗，也未完成确认生成草稿/去重，不宣称预警功能交付。
其他未完成数量入口、历史校正审核、模具和正式发布保持原任务范围。

## 发货预警外购在途实物口径（2026-09-26，未发布）
本轮重验上一轮仅复核方案未推进代码，继续在隔离候选推进。新增external_stock_incoming_physical_quantity读取同客户同产品有效外购采购数量，减active_receipt_item过滤后的真实实收；排除采购撤销和作废补库单，逐采购行超收归零。不读旧补库计划quantity-stocked_quantity客户单位，不用当前主档比例重算，不逐批舍弃余片。
发货stock-warnings对纯外购非复合/非coated_board增加incoming_physical_quantity和suggested_physical_quantity=max(target-available-incoming,0)。后端保持实际消耗、客户范围、warehouse.view权限门禁；数据非整数返回明确409而非截断。既有全局stock_policy_dict及旧外购直接确认采购入口仍保留旧语义，本轮只修改发货后实物预警契约；后续必须统一新草稿路线，不能把本轮称全部补库完成。
测试：test_p1_140_external_stock_replenishment及test_delivery_physical_quantities -k not migration，19 passed/1 deselected，41.14s。覆盖采购8000实物、收1剩7999/收2剩7998、主档1:2改1:7冻结在途8000、取消采购0，以及发货200扣100余额100建议实物100。排除旧专项migration测试，因为本轮无schema变动；既有ea0926演练证据保留。git diff --check通过（换行提示）。
未修改正式库、历史流水、预警阈值、模具，未发布。下一步：现有StockReplenishmentOrder/Item增加冻结实物需求快照（nullable兼容旧customer-plan），外购待报料draft后续采购/实收进度闭环；既有external确认入口会直接建采购，不能直接接弹窗。新草稿必须保留奇数需求，不得向下取整或默默翻倍。还需完成普通库存抵扣/未预占超送路径、历史校正审核、打印视觉、模具及正式发布。

## 外购预警实物草稿与快照迁移（2026-09-26，未发布）
新增stock_warning_drafts.py，在原StockReplenishmentOrder/Item保存draft和实物quantity，不另建模块。新增item.quantity_contract_json冻结客户/产品、单位/比例、实物数、delivery/policy来源；旧NULL保持原客户计划语义。按dispatch+policy固定单号重放，策略更新锁序列化；新会话和主档修改后仍返回原草稿。此服务尚未接公开API，调用方未来必须执行权限、真实净扣库和实时建议量复核，跨送货的既有待补覆盖仍需接通。
unified_procurement.pending_stock_rows将有快照的外购草稿明确标procurement_mode=external_purchase、quantity_basis=physical和采购片数/单位；snapshot指纹纳入新快照，旧NULL指纹不变。原纸板attach拒绝external门禁保留。后续采购入口及实收进度尚未接，不能把待报料投影称完整可采购交付。
测试test_stock_warning_physical_drafts：实际派发200片后生成1片草稿，status draft无confirmed；新会话主档1:2改1:7/推荐改20，重放仍1片原比例同单；客户订单、采购批次、库存流水计数均未增加；待报料投影显示1片外购。1 passed，6s。首轮测试错误使用不存在User.is_superuser，改用fixture明确admin账号后通过，未变业务权限。
扩展回归：stock_replenishment_flow +新草稿+P1-140 -k not migration：20 passed，24 failed，1 deselected，79.45s。失败集中旧flow请求缺少idempotency_key，被400拦截；git show HEAD:app/api/requisition.py确认该强制防重复门禁原已存在。未删除门禁或跳过失败，不宣称全回归通过；具体失败仍需按真实当前契约调整旧测试并复验（还可能有后续错误）。
ea0926未发布迁移新增nullable补库快照及非NULL降级拒绝。run3真实upgrade/downgrade/upgrade：304原业务表、1026索引/触发器不变，原数据快照NULL，4种有数据拒绝降级且文件字节不变，integrity ok/FK0。源audit副本哈希不变，正式库未动。三份迁移文档已更新run3证据；脚本新增补库字段保护检验。
下一步优先：外购草稿正式采购复用原流程，允许1片不足整客户单位但不改客户单位；冻结比例用于采购与实收，进度按该草稿physical语义，旧NULL按旧customer语义。接确认API/弹窗、pending覆盖与幂等审计，再完成旧测试契约回归。历史校正审计确认、普通库存抵扣/未预占超送、模具/打印验收及完整发布仍未完成。无正式写库、migration、发布，候选未提交。

## 外购物理草稿转采购及逐片实收（2026-09-26，未发布）
将外购采购持久化抽为_post_external_stock_purchase，旧create_external_stock_replenishment_purchase复用，不重复建模块。prepare_external_stock_purchase新增physical_contract，仅新物理草稿允许不足1完整客户单位的采购，使用冻结比例校验身份/单位，仍按当前有效供应商候选、价格、MOQ和包装倍数门禁。
confirm_external_warning_draft复用既有草稿/明细，校验数量快照哈希、客户、纯外购适用性，CAS draft→confirmed；单号派生固定幂等键，已生成采购重放不再生成。新POST /api/requisition/stock-replenishment/orders/{order_id}/external-purchase要求requisition.execute、admin+cost.view、客户范围和expected_request_hash。首次确认append_audit_event与采购同事务，重放不重复审计。详情返回request_hash及quantity_contract供后续前端使用。
external_packaging_receiving新快照进度按实物实收，旧NULL仍按converted客户计划；实际入库始终真实收到片数。待报料pending_stock_rows排除已有效采购的外购明细，避免采购后仍能重复选入。保存的客户/实物比例不被主档后改1:7覆盖。
验证：新草稿+旧P1-140非迁移合跑15 passed/1 deselected，32.62s；随后加强真实管理员确认API、旧hash409、重复POST created=false、响应快照字段及哈希一致性校验，新草稿3 passed，10.95s。覆盖1片完整采购/入库/stocked；3片分收1+2→partially_stocked/stocked，各次重复实收不加库存；同一草稿只1采购批次；采购后不再出待报料。该服务测试中草稿来源配送使用SimpleNamespace（不冒充发货弹窗全链）；前一测试真实发货创建草稿单独验证。git diff --check通过。
尚需接通：发货预警确认API（权限、真实净消耗、策略锁后重算、已待补覆盖）及前端弹窗；待报料外购确认按钮接新接口与价格预览；跨送货去重/作废及外购实收反向链回归；普通库存抵扣和未预占超送；旧补库测试24项失败按当前幂等契约修正并复验；历史校正审核、模具和打印验收与正式发布。本轮无正式库、migration、服务或版本变更。候选未提交，目标未完成。

## 发货预警确认草稿API及待补覆盖（2026-09-26，未发布）
新增POST /api/deliveries/{id}/stock-warnings/confirm，payload.items包含policy_id/physical_quantity（严格正整数）。复用deliveries.execute、送货客户范围，额外warehouse.view/requisition.execute；锁定仍dispatched送货单、按id顺序锁启用本客户策略，再expire/reload读取实际净consume预警及当前缺口。未发货/取消/历史补录、缺口变化、重复策略选择或同产品重复策略均拒绝；整批事务生成draft和审计，失败回滚。按delivery+policy派生原单号重放，不重复建单，重放数量不符409。
新pending_physical_warning_quantity汇总同客户同SKU待报料实物快照；发货预警建议=max(target-available-incoming-pending,0)，不再让下一次送货重复覆盖已有待报料。已采购转confirmed后从pending扣除，由真实外购在途覆盖接续。physical_demand_contract改显式条件校验，避免python优化关闭assert绕过业务检查。
真实API测试：库存200，两次客户50各扣100；第一次target201/available100生成101片待报料，重复请求同单created=false，GETpending101/suggestion0；第二次库存0建议100，只新建100；合计两张draft，无采购批次，库存仍0。未发货与过期数量请求拒绝不建单；历史重复产品策略整批409，原两草稿保持。
验证：newdraft+delivery_physical_quantities 11 passed 27.63s；显式快照校验后draft4 passed13.20s；追加同产品重复策略后定向1 passed5.89s。git diff --check通过。未修改正式库或部署。
重要范围：此confirm目前只接纯外购实物需求，普通纸板/复合产品缺suggested_physical_quantity会明确409，不能冒充所有产品预警已完成。尚未绑定前端弹窗，避免把未完整普通报料链投放正式页面。下一步复用stock_policy_replenishment_draft（14794）及_build_replenishment_item构造普通paperboard草稿，处理产成品需求与报料片数、多组件及已有待补覆盖，统一确认语义后再绑定dispatchDelivery finally（实际dispatch成功后，即使printed登记/刷新失败也提醒；补打不创建）。待报料外购按钮还需接既有新external-purchase接口及价格预览。旧补库24失败测试、普通抵扣/未预占超送、历史校正审核、模具/打印/发布均未完成。

## 普通纸板及复合待报料覆盖（2026-09-26，未发布）
customer_board_preparation_coverage新增独立pending_board_preparation_sheet_quantity、pending_board_preparation_auto_cover_capacity、pending_auto_cover_piece_quantity。仅统计本客户、本reference_product、原冻结规格签名一致、paperboard、stock_warning、request_hash非空的draft；不将草稿当库存/在途，不跨兼容SKU重复抵扣。作废立即退出覆盖。
stock_policy_dict普通报料及virtual_composite_replenishment_demand_plan按真实待报料片数扣需求后再除每箱片数/实际开料出数取整；复合父件按必需组件分别覆盖，完整套数取最短组件。无需新报料且已有草稿时state=pending_requisition，与已采购already_ordered分离。
验证12 passed21.74s：五种每箱片数/开料出数、双拼余片、BOM子件、普通待报料1张从282需求减为281（库存仍80、在途0），作废回282；虚拟及组装复合父件两种既有API路径，草稿375+500=875，pending875/incoming0/新建议0；进入既有统一供应商采购后原回归通过。git diff --check需本轮收尾确认。
本轮没有把发货confirm扩展至普通报料，尚需复用stock_policy_replenishment_draft（包含多组件）及_build_replenishment_item构建普通draft，API提交应校验整个建议方案指纹，不能仅比较成品数，避免规格/组件变化后误确认。普通草稿quantity为报料张数（衬板finished另核），不写外购physical quantity_contract；来源绑定使用确定性送货+策略order_number及审计。前端补库弹窗/外购待报料确认按钮未接，pending_requisition可补更明确显示；旧24项失败及其他未完成范围继续保持。未修改正式库存、历史、模具或发布。

## 普通报料确认及发货补库弹窗（2026-09-26，未发布）
paperboard_warning_plan复用stock_policy_replenishment_draft和StockReplenishmentItemPayload，完整规格/组件/数量序列化指纹；create_paperboard_warning_draft复用_build_replenishment_item，原StockReplenishmentOrder/Item draft保存纸板行，外购quantity_contract不写入普通行。GET送货预警对有报料操作权限用户返回plan_hash/proposed_items，缺资料返回blocked_reason；POST同策略锁后重算整份plan_hash，不仅比较成品数，多组件原子建单并审计。重放普通草稿按hash、外购按实物数量；旧已建单来源保持。
真实普通API测试：库存200送50剩150，目标250、一开二，建议50张。修改主档报料宽180→190后旧hash409；刷新后建1张待报料含50张/宽190，重复同草稿；已有草稿后proposed_items为空。seed_physical测试helper增加external=False以构造真实普通库存，生产代码未因此放宽门禁。
static/index.html新增showDeliveryStockWarnings，在dispatchDelivery finally且dispatchSucceeded时调用，因此printed登记或列表刷新失败也提醒，发货失败/补打不自动建单。显示所有触发款号、名称、库存/阈值；确认前显示外购实物数量/单位或普通报料张数；取消无POST；缺资料单列，已有覆盖不重复建单；无报料权限只提示交报料人员。POST保存后显示待报料单号。未进行正式自动页面点击。
验证：newdraft+delivery_physical_quantities 12 passed29.64s；verify_delivery_quantity_input.cjs编译全inlineJS并双数量方法通过；新verify_delivery_stock_warning.cjs执行真实前端方法，取消、外购+纸板混合请求、已有覆盖、提醒请求失败、printed失败仍提醒、dispatch失败不提醒/恢复按钮状态全通过。首轮JS测试缺normalizeDeliveryPickTask桩，补准确空任务桩后通过，未改业务流规避。git diff --check通过。
边界：复合多组件复用既有方案生成和前轮复合采购回归，本轮真实发货confirm专项只验证普通纸板；还需补复合发货与权限/并发/撤销回归。衬板finished草稿待补覆盖、coated_board特殊路线需核查。尚需待报料列表外购确认按钮及价格预览、Chrome隔离视觉；旧24失败测试按幂等契约修正；普通抵扣/未预占超送、历史校正审核、模具及打印兼容等范围未完成。正式数据、服务、migration和版本未变，未发布，候选未提交。

## 待报料外购按钮与价格预览校验（2026-09-26，未发布）
新增GET /api/requisition/stock-replenishment/orders/{id}/external-purchase-preview，沿用requisition.execute/admin/cost.view/客户范围；返回供应商、实物数量/单位、规格、单价、税模式/税率/税额/总金额及草稿hash、quotehash。prepare_warning_purchase复用现有候选、价格、MOQ/包装倍数校验，quotehash覆盖冻结需求、供应商身份/名称/版本、产品候选版本、价格整行及计算结果。POST要求expected_quote_hash并即时复算，变化409；首次采购batch.request_fingerprint保存quotehash，重放匹配原采购而不读后改主档。原老外购确认入口保留。
待报料表外购物理行新增管理员“预览并确认外购”按钮，其余角色显示待管理员；禁止混入纸板勾选采购。显示实际采购单位、外购实物需求和“外购，无需生产”，不再把外购数量硬标张或需生产。方法confirmStockExternalDraft预览→确认→POST两个hash→刷新；取消无POST、忙状态防连击、采购后刷新失败明确已采购。未进行Chrome正式点击。
测试真实API两组1片/3片分收：预览后修改供应商名称，旧quotehash409；刷新后按原冻结1:2采购并逐片入库，重复确认只1批次；草稿旧hash409。综合newdraft+旧P1-140非迁移17 passed/1 deselected38.08s。前端verify_delivery_stock_warning.cjs增加采购取消、不越权、准确两个hash、采购一次/刷新及忙状态恢复；所有方法测试通过。verify_delivery_quantity_input.cjs全部内联JS语法与原双数量方法通过；git diff --check通过。
本轮只验证新报价确认/前端方法，未完成浏览器视觉及打印证据。剩余：补库权限/并发/取消与复合发货端到端、衬板finished待补覆盖/coated_board路线；旧补库24失败测试修复；普通库存抵扣/未预占外购超送；历史校正清单确认与批次成本；模具创建及旧打印模板兼容；提交/正式发布安全门禁及管理员验收。未写正式数据库、未migration或重启发布，候选仍未提交。

## 补库旧回归与统一采购收料状态（2026-09-26，未发布）
本轮复查上轮测试无存活进程后重新验证。旧stock_replenishment_flow测试按当前幂等与真实采购前置契约更新：草稿不直接收料，通过from-pending-selection生成有效供应商采购单，保留库存/审计/重复提交及作废门禁。并发草稿fixture复制真实request_hash及明细，不放松生产比较。
修复真实问题：统一采购reported-documents原固定待入库，现supplier_incoming_statuses按有效posted收料、typed source link和短收结清聚合为待入库/部分入库/已入库/已作废。仅一条聚合SQL，匹配多个来源身份的同一事实不重复累计；保持单据状态及权限过滤。incoming_receipts._stock_target先查单据生命周期再查有效采购，作废收货返回明确状态提示。
扩展分页回归发现原逐补库单receipt_progress_map引发查询随历史增长，改为一次批量进度查询；去掉循环内SimpleNamespace导入，修复只有复合记录时报UnboundLocalError。采购来源快照并入既有明细outerjoin（supplier_item_id唯一），避免独立查询，不放宽16次SQL预算。
验证过程：补库全文件最初30通过1错误提示失败；修正后原31通过。新增部分收料/短收结清及禁止普通撤销两组后，合跑33补库+7分页为39通过1查询预算失败；最终合并来源快照后分页7 passed20.26s，受最后修改影响的补库4项再验4 passed29 deselected10.18s。新增测试确认普通撤销客户专用库存409，状态保持已入库，不冒充撤销成功测试。git diff --check通过（仅换行提示）。
分支codex/delivery-physical-quantity-20260926，HEAD 0d735add0efec2e6bf6607600e65787151ee3582，候选未提交。无正式库写入、历史校正、migration、服务重启或发布。未浏览器点击验收。
接续未完成：普通已有实物库存抵扣与未预占外购超送；预警权限/并发及复合真实发货、衬板待补覆盖/coated_board；打印视觉与管理员模板；历史校正清单确认和成本批次链；三客户模具；正式发布门禁与人工验收。目标仍未完成，不把当前回归通过当全量交付。

## 已有实物成品抵扣普通订单（2026-09-26，未发布）
修复reserve_finished_inventory把quantity同时当客户和实物的问题：eligible纯外购使用订单冻结比例，输入客户数量转physical_for，库存CAS、预占及reserve流水均按实物；需求保存分子/分母，取消精确还原；重放校验客户信用、订单、批次及类型，异数量同key409。非外购保持1:1。
新增finished_stock_identity.matches_stock_identity只排除独立quantity_basis元数据，仍逐字段比较冻结SKU规格；require_physical_stock独立校验非1:1库存的实物账标记。候选与写入共用，历史未核批次不能按新比例抵扣。
finished_stock_customer_capacity按订单快照求完整客户单位并向下取整。订单reservation_plan的库存上限接此服务；warehouse finished/candidates保留实物quantity_available并增加customer_quantity_available。订单明细抵扣页标明实物可用/抵扣客户数量，默认和max使用客户可抵量。
真实API验证：实际外购收料释放原预占后新建同产品订单，库存200输入客户100，1:1预占100、1:2预占200；200片拒绝客户101；199片只允许客户99预占198余1，拒绝客户100；重复不重占、同key异数量拒绝，取消恢复原库存，流水记录实物量。测试路由fixture补挂真实warehouse router，未绕过权限。
验证：direct_external_finished全文件16 passed45.18s；新增奇数参数后专项3 passed12.36s。finished_goods_inventory_reservation初18失败均在manual_finished_in成本门禁（旧fixture无材质价格/展开尺寸），仅补测试已报价材质及尺寸后19 passed22.90s。前端验证脚本执行真实loadFinishedInventoryCandidates，200/100、199/99、普通50、余1可抵0默认正确；全inlineJS语法及原送货输入测试通过。git diff --check通过。
本轮仅隔离开发，无正式库修改、历史校正、迁移、服务重启或发布，候选未提交。
接续：新建/导入订单库存候选前端仍可能用实物当客户拟用量，订单保存已加正确上限但需完整页面回归；requisition._safe_late_finished_inventory_candidates及_preview_from_facts、18386分配仍按lot.quantity_available计算，需要统一，不能声称所有入口已闭合。未预占外购超送、移库分拆分母消费者仍需检查。预警其余路线/并发权限、模具、旧打印及发布原范围保留。

## 实物预占部分移库的分数信用守恒（2026-09-26，未发布）
本轮检查待报料late finished入口时，同时追查预占分母消费者，发现_transfer_finished_lot_location拆分直接released_requirement += take/new credited=take，导致1:2或1:3批次移动1片后子批信用错误为1客户单位。先新增真实收料/真实移库测试复现：两组均失败，实际Fraction(1,1)对比预期1/2、1/3。
修复拆分使用reservation_requirement_numerator(reservation,take)，原预占released保存对应分子，子预占继承requirement_quantity_denominator；保留产品身份、位置版本、幂等、成本及原移动流水，未改正式数据。
测试：真实外购收4/6片（客户2），部分移库1片，源释放及子信用为1/2、1/3，合计客户信用仍2；重放同目标不重复拆；拆分后发货客户2消耗4/6片；取消全部恢复4/6预占及客户2。和原复合BOM组装前/后移库成本及覆盖回归共4 passed28 deselected16.94s。git diff --check通过。
下一步仍需处理：_late_finished_inventory_preview_from_facts和批量预览context、one-click分配仍用实物量作为客户量；跨奇数批次不能逐批floor而丢客户信用，需预览与写入统一精确规划后再开通。新建/导入订单页面拟用数量、未预占外购超送、预警剩余路线/并发权限、历史校正审核、模具和打印发布尚未完成。
候选仍未提交、未发布，无正式库写入/迁移/重启。本轮未执行正式浏览器自动验收。目标保持完整，先修数据守恒底层不是取消上层范围。

## 预占详情双数量显示（2026-09-26，未发布）
本轮追查late finished发现明确路由事实：requisition.pending base_query在9576排除supply_mode_snapshot=external_purchase；_ensure_pending_order_item_for_supplier_order只接受未报料/已报料，不接受外购包材待确认。故此前把纸板待报料late finished当作纯外购必经入口不准确，不应为双数量扩展纸板采购路由。新建/导入订单的成品拟用、已有订单成品抵扣才是实际受影响入口；跨奇数批次整体预占仍需在该入口统一，不逐批floor丢数。
确认并修复warehouse._reservation_dict直接返回内部需求分子的问题：旧公共credited/consumed/released_requirement_quantity现按分母转显示数值，另保留每字段_numerator和requirement_quantity_denominator供精确追溯；None旧值保持None，实物字段不变。订单明细预占表同时显示预占实物、抵扣客户数量，分数用分子/分母避免小数误认为整客户单位。
验证新增真实warehouse预约响应断言：1:1/1:2/199片余1，客户抵扣100/99、实物200/198及分母正确；真实移库子批1片对应1/2或1/3投影，后续发货与取消仍守恒。5 passed14 deselected17.74s。verify_delivery_quantity_input.cjs前端真实方法、原双数量及inlineJS编译通过，git diff --check通过。未修改纸板late finished或扩大采购资格。
无正式库写入、历史校正、migration、重启/发布。候选未提交。下一步优先新建/导入库存计划跨批次与客户口径完整链、未预占外购超送；预警余下边界、打印模板/管理员、模具、历史审计确认和发布原范围均保留。

## 新建订单草稿预览客户实物口径（2026-09-26，未发布）
真实API失败复现：/orders/inventory-draft-preview对1:2库存200返回finished_planned_quantity200，对199返回199；应分别100/99。修复产品草稿比例解析、候选实物账标记校验，候选可抵客户量转换；used_stock_by_lot始终累计实物，计划显示客户量，不会让后一行重复采用前一行已分配库存。请求选中的历史未核批次409，候选排除；权限、客户、确认、版本预检保持。
验证：扩充已有真实外购→释放→新单API用例，1:1与1:2/199预览通过3 passed12.43s。新增actual order reservation_plan保存：199片两草稿行共用同批预览[99,0]，真实保存订单客户数量200不改，库存预占198余1、客户信用99，1 passed6.03s。原PDF预览读写隔离、共享库存、权限/客户范围、重复行ID4 passed9.78s。git diff --check通过。
仍有明确未闭合：当前plan每批requested_qty整数客户量，跨两奇数批（如1+1片对应1客户）单批floor可能漏算，需统一整组物理分配与精确信用，再复用预览/保存/前端，不得把本轮单批验证宣称跨批完成。前端新建/导入候选拟用显示及订单自定义比例草稿契约亦需核对。普通外购未预占超送、预警剩余边界、打印模板/管理员、三客户模具、历史校正审核及正式发布原范围不变。
本轮无正式库写入、迁移、重启或发布，候选仍未提交；NAS独立回执同步，不写全局交接。

## 新建订单跨零散批次整组抵扣（2026-09-26，未发布）
真实失败复现：两个实际外购收料批次各1片，释放原预占后新订单选择两批，1:2草稿计划返回[0,0]而应第一行1第二行0。新增delivery_quantities.finished_reservation_plan：所选每批请求客户上限转物理容量，重复lot不重复计容量；先按整组实物确定完整客户量再FIFO分配，每批信用Fraction，计划总客户量整数；不足完整组的余片留存。
/orders/inventory-draft-preview及_apply_order_reservation_plans共用规划。预览used_stock_by_lot按实际分配物理片数累计；保存使用订单冻结比例（纯外购）及既有版本/客户/确认/事务门禁，每批reserve写精确信用。reserve_finished_inventory内部接受规划产生Fraction，通过physical_for_fractional_credit验证能转换为正整数实物；外部API schema仍整数客户输入，不允许用小数凑单。数量契约错误转409。
验证真实API参数[199]→客户99预占198余1；[1,1]→客户1跨批预占2；[1,2]→客户1预占2余1，第二草稿行不重复采用，保存订单总数量200保持。专项3 passed11.12s；直接外购全文件22+原PDF预览4+原成品抵扣19合跑45 passed90.49s。git diff --check通过。
新发现接续：direct_external_finished.managed目前只按本订单采购收料关联判断；新订单采用其他订单释放的实物库存后，可能被_external_packaging_received拦住送货。必须以已验证实物标记及本订单实际预占为依据接通，不允许当前主档重解释历史库存，也不能因一个新批次就把混合旧口径全部放开。需新增真实新订单采用库存→创建送货→拿货→扣库→取消测试。前端新建/导入候选仍需同源客户口径/跨批显示；剩余超送、预警、打印、模具、历史校正审核与发布保持。
候选未提交、未正式发布；无正式库、历史、migration、重启修改。未执行正式浏览器自动验收。

## 新订单采用已核实实物库存后送货闭环（2026-09-26，未发布）
新增真实新订单采用库存→送货测试，3组均先复现409外购包材尚未收齐。direct_external_finished.managed保留原本订单收料路径，并为eligible纯外购增加已采用库存判断：必须有本订单有效finished_order预占，所有未完全释放预占的批次均具显式physical标记，客户/产品/单位与订单冻结比例匹配，专用且信用分子分母与实物量严格相符。任何一批未核实则不进入新实物路径；require_physical_stock新增require_marker关键字，1:1也可强制标记。批量outerjoin加载预占、批次及finished_detail，避免逐批get。
验证真实API完整链：[199]、[1,1]、[1,2]实收，释放原订单后新建计划订单，预览/预占正确；去掉一批标记managed false恢复后true；新订单开送货客户99/1，print客户数不变/实物198/2；拿货任务及各库位合计实物一致；picked/submit、真实dispatch扣实物，重复dispatch及补打不重复扣；cancel及重复cancel恢复原预占，余1保持。专项3 passed12.76s。direct_external_finished22+delivery_physical_quantities7合跑29 passed78.05s；最后批量加载调整后专项3 passed13.17s。git diff --check通过。
本轮未做页面视觉/实体打印；不宣称所有入口完成。接续前端新建/导入库存候选拟用口径、未预占外购超送；预警衬板/特殊路线和权限并发；旧模板/管理员和三客户打印视觉；模具和历史校正审核；正式发布门禁与管理员验收。候选未提交、未发布，无正式库、历史、migration或服务修改。

## 新建及导入订单页面库存双数量（2026-09-26，未发布）
finished_product_candidates新增customer_quantity_available和quantity_contract（两种单位/比例），保留quantity_available实物口径，候选排除非1:1未核实批次，沿用客户/仓库权限。
前端reallocateDraftInventorySequentially的成品分支先汇总selected实物，再按完整客户组分配各批实物与分数信用，跨行usedStockByLot记实物，同批去重；半成品分支保留。新增finishedInventoryAllocatedCustomerQuantity按实物总和统一换算，避免逐批浮点求和影响整客户覆盖；buildReservationPlan向后端提交每批完整客户组上限（ceil到customer_basis），后台仍整组分配实际片数，内部份额不作为小数客户输入提交。不同候选比例或损坏规则标stale拒绝保存。
PDF库存表改成品实物拟用/可用且显示冻结physical_unit，pdfFinishedUse汇总stock_quantity；手工订单候选/已安排也显示实物和单位，不再硬编码个。pdf-workspace.js引用更新缓存版本20260926-physical-quantity。
验证：候选真实API 3 passed12.69s（1:1、1:2、199余1）；verify_delivery_quantity_input.cjs执行真实前端方法，200→100、199→99、1+1、1+2、共享批次、2客户:3实物组、payload整数组及PDF实物显示通过；inlineJS和外部pdf-workspace语法通过。原semi_finished_inventory_frontend初28通过2旧文本断言失败，按已有补库片/个分类和保存行号||=持久化更新断言，30 passed1.69s。git diff --check通过。
本轮未Chrome视觉验收，候选未提交/发布，无正式库写入、迁移、重启。剩余未预占外购超送，预警衬板/coated_board与权限并发，旧打印模板/管理员视觉与三客户Excel一致性，模具、历史校正审核及完整发布门禁。前端自定义订单比例草稿覆盖仍需核对；本轮使用产品默认basis，后端保存使用订单冻结basis。

## 衬板待报料与在途覆盖（2026-09-26，未发布）
真实失败复现：test_liner_stock_warning_can_create_draft_and_receive_as_finished创建paperboard/finished衬板9片，pending覆盖返回0。原因coverage只查semi_finished且finished明细缺纸板签名字段。新增严格同客户、同product、明确paperboard路线的liner finished覆盖；成品按数量直接覆盖成品需求，内部piece计量乘当前pieces_per_box用于同口径相减，不乘开料出数。普通semi_finished继续原冻结规格签名，外购finished不进入此分支。外购在途旧汇总排除明确paperboard路线避免重复抵扣。
验收扩展真实API：创建库存策略target9，待报料pending9/incoming0，实际策略建议新增成品/纸板均0；作废状态退出覆盖；采购后pending0/incoming9且策略建议仍0；实收9后incoming0、实际finished库存9及F34库位原断言保持。合跑stock_replenishment_flow与stock_warning_physical_drafts：37通过1测试状态拼写错误失败；修正为模型合法voided并把新增策略helper移入正确测试后，受改动2项通过（6.41s）。其余37无代码再变，不重复全跑。存在既有JWT/SQLAlchemy警告，未隐去。
另核实orders._validated_external_purchase_ratio：常用箱是唯一可写比例源，旧订单提交字段不能覆盖，保存冻结主档；此前任务卡提出订单自定义覆盖疑点排除，不另建订单比例配置。
本轮正式数据库、历史数量、migration、服务、版本均未变，候选未提交发布。剩余外购未预占超送、预警其他边界与权限并发、旧模板/管理员和三客户打印视觉、模具批次、历史校正预览确认及发布仍未完成。上一轮主要方案复述，本轮恢复实际修复和失败转通过证据。

## 发货补库权限、事务与并发门禁验证（2026-09-26，未发布）
新增真实API测试9项：缺warehouse.view/requisition.execute/deliveries.execute、selected客户范围无授权均403且无草稿；取消发货/停用策略409且无草稿，取消恢复200实物。注入create_delivery_warning_draft审计失败，事务关闭后无草稿，再次有效请求仅创建一次。两个独立TestClient/线程/数据库会话并发：同送货确认均200且created一真一假；两张已发货单争同策略当前201片缺口，仅一200另一409；最终均一草稿总201片。
验证命令pytest tests/test_stock_warning_physical_drafts.py -k "gates or audit_failure or concurrent"：9 passed，5 deselected，24.46s。git diff --check通过。本轮只增加测试和文档，未改运行代码、正式数据库、迁移、服务或版本。既有候选仍未提交发布，不将门禁通过宣称功能全部交付。
读码发现需保留的路线事实：coated_board实际为灰底白板（卡纸），并非覆膜板；总需求169行要求保留原料路线。delivery预警排除此类直接外购实物分支，但paperboard_warning_plan遇原external_stock_draft仍明确阻止，特殊原料待报料方案仍未完成，不能为通过而按直接成品套用。另复核HEAD中_delivery_remaining_quantity对direct managed本就min订单余额/预占；此前提出的外购未预占超送是基线已有边界，后续需区分保持既有规则与新开超送入口，不能把原无订单库存直送误认为必须在订单上放宽超送。未在本轮改变此规则。
接续：优先收口特殊路线、旧模板/管理员及三客户打印视觉、模具候选操作和历史校正清单。历史正式校正仍需用户确认；发布门禁和管理员人工验收原范围保留。目标未完成。

## 旧模板及三客户隔离Chrome视觉复验（2026-09-26，未发布）
复核既有LEGACY_PRINT_FONT_20260922任务与v490发布证据，现候选已含旧像素/pt修复；没有为此重复改业务代码。以原TH-20260922-003冻结delivery.json运行verify_legacy_delivery_typography.cjs，当前Chrome真实渲染18行/总数2759/2页，company21px、标题18px、表格13px、meta12px、地址11px；PCS单行17px，全部内容位于安全区。管理员设计页公司/地址/表格字号与打印一致，自定义1.1倍率有效，重绘不累乘。截图已查看，客户标签无此前拆行，顺序公司/标题/地址正确。
新增可复跑scripts/validation/verify_customer_delivery_layout.cjs，复用旧审查脚本但改候选相对路径、参数化输出、NODE_PATH依赖和禁止所有网络请求。从当前preset_layout生成yke/kew/yl配置，以37行合成验收数据跑价格显隐6场景均PASS，241×139.5 PDF及全页截图已生成并查看。超长用途800重复串分45页，重组原文一致、数量只计一次400。修正样张金额合计为37×214.64=7941.68并明确整单只数。样张是版式测试数据，不是正式出货或真实Excel逐笔核对证据。
输出D:/.codex/workspace_artifacts/delivery_physical_20260926/legacy-print-current及customer-print-current。
管理员草稿/发布/客户覆盖/历史快照/回滚与旧模板测试合跑16通过1旧CSS文本断言失败；当前实际13px已由Chrome验证，调整test_p1_77_delivery_print_safe_layout三条过时文本断言以认可默认倍率1的calc像素写法，该文件6通过0.41s。git diff --check通过。没有删除权限或改字号预期。
灰底白卡路线读码确认：模型只允许finished/semi_finished，external_stock_draft与_post_external_stock_purchase固定finished；普通订单卡纸有明确原料例外，不能直接复用finished备库。此特殊路线未实现且没有擅自绕过。本轮未正式写库、历史校正、迁移、服务重启或发布。剩余原目标含特殊补库路线、双数量整合、历史逐批校正方案确认、模具登记和正式发布安全门禁；实体打印和正式页面须管理员人工验收。

## 三客户模具最新只读预览及名称兼容（2026-09-26，未发布）
实际正式库mode=ro/query_only/BEGIN查询YG137/GY138/YL136，新增scripts/audit/customer_diecut_molds.py只读plan，无apply模式，包含产品/模具版本、当前绑定、共享关系、稳定创建幂等键、目标1F-M-R04及源事实hash；客户存货编码重复、停用、共享模具不自动新建。R04属于mold_location固定靠墙区域，不是warehouse_locations行，不新增库位。
失败复现3种真实主档产品名TRD-NϵP、T1K-08,-16(I/O)、KCM，KCX经正式mold create返回422。mold_identity仅中文简写新增希腊字母及中英文逗号允许，标签名称规则不变；长度、脚本标记/控制字符拒绝保持。真实API保存原文、R04、相同key重放同ID；多客户、权限、原身份回归合跑13 passed26.07s。只读planner另1 passed0.67s，验证不写、确定性、范围、停用、版本hash变化及共享本体。
修复后只读预览230 create_and_bind、2 reuse_bound、27 review，源hash ae4c42288460b1a1bd624228418098e4a09c4bf489b857c527f8d1a664cc0b45；不同于最初224候选，不能沿旧数量执行。预览D:/.codex/workspace_artifacts/delivery_physical_20260926/molds/current-plan.json及登记预览.md。该清单是最新候选，不表示已完成登记；执行前仍需复核版本、未关联旧模具同名碰撞和绑定业务接口，并在隔离正式副本批量演练创建/绑定/重放，再执行正式授权范围。现有共享模具161关联两个YL产品不拆分；2个单独已有模具名称与产品名不同，后续更新应保留历史与版本审计。
本轮未写正式数据、未登记模具、未发布/迁移/重启。修复待候选整合发布。git diff --check通过。旧26/31条粗略待核清单以本轮逐项原因作为后续核对起点，不能把待核当删除范围。完整目标仍未完成。

## 三客户模具正式副本批量演练（2026-09-26，未发布）
新增scripts/validation/rehearse_customer_diecut_molds.py：源库以mode=ro/query_only打开，仅SQLite backup到全新workspace_artifacts目录，目标不能已有、不能正式安装目录；只在新副本调用既有create_mold_tool和bind_mold_products，保持管理员、身份、位置、幂等、版本、审计门禁，不直接SQL插模具。无迁移/密码修改/正式写入。
实际演练230款：创建→绑定→新Session重复create同ID/idempotent_replay→重复bind bound_count0。完成230/230，重做plan变232 reuse_bound、27 review、0 create。副本integrity ok/FK0。逐表全字段hash对比，305张业务表中仅master_data_object_versions、mold_master_mutations、mold_tool_customers、mold_tools、operation_logs、products变化，299张不变；库存、采购、订单和历史送货均未变。证据molds/rehearsal-1/evidence.json及molds-isolated.sqlite3。该工具没有正式apply模式，演练成功不能当已登记。
planner补未关联旧模具编号/名称含相同SKU的碰撞警戒，只列待核不靠名字推断身份。新单测验证碰撞、只读、共享、版本指纹通过1 passed0.68s；真实只读复查230候选无此碰撞，hash保持ae4c42288460b1a1bd624228418098e4a09c4bf489b857c527f8d1a664cc0b45。演练完成后增加预期变更表白名单断言，已有evidence名单符合，无理由重复整批。
27待核明确分类：24停用/删除、1活跃重复编码、2产品共享模具161。共享模具当前1F-M-R01-L2-G04，旧标签Z.001.000012/014，实际绑定SKU Z.001.000012与Z.001.000114，不能直接把同一本体复制两次或静默更正014/114。两款独立已有模具在R04但中文简写是原加工名称，需独立版本化更新计划。暂不将现有实际R01位置伪造为已搬R04。
本轮无正式写库、发布/服务变化、历史校正或实体标签打印。git diff --check通过，候选未提交。下一步复核重复编码及共享标签差异，准备受控正式登记批次/发布，同时收口双数量整合与历史校正候选；完整目标未完成。

## 外盒内衬编码区分及模具第二次演练（2026-09-26，未发布）
只读核实YL Z.001.000096重复内部product_code：3585为模切D版ECU新版纸盒，customer_material_code=Z.001.000096；3776为粘贴内衬，customer_material_code=Z.001.000096内衬。并非两条同存货编码模切产品。planner按warehouse既有存货编码语义customer_material_code优先/缺省product_code，重复判断和标签匹配同步采用该值，不合并主档，不给非模切内衬建模具。新回归验证同内部编码不同存货编码不冲突，真正同存货编码仍待核。1 passed0.68s。
当前plan231新增、2复用、26待核，hash fb5e2169f4166f19f10213f545036535c324fe13fab3aa60095edb82fbbce323。全新rehearsal-2正式只读backup副本，231创建/绑定/新会话重放全部通过；另经原update_mold_tool版本/审计/幂等接口将R04两个单独模具中文简写更新为产品名称：163纸箱扣手→B07总成 外箱；85正面单片模切→自制风机包装箱1148*906*814。重复更新同idempotency返回原结果，未新建本体。原客户关联、位置、备注保留。
副本305表hash对比仍仅6张模具/产品/版本/审计表变化，299张不变；integrity ok/FK0；下一次plan233复用/26待核/0新建。证据molds/rehearsal-2/evidence.json。完整正式执行工具尚未提供，本轮不擅自正式写入。
已异步询问老板：YL Z.001.000012与Z.001.000114共用模具当前是否仍在R01-L2-G04或已搬R04，未收到答复前不登记物理移位。旧标签012/014与绑定114存在差异，保留待核、不靠名称另造模具。余24停用资料保持原状。当前仅这项位置事实待答，不阻塞其他开发或构成总体blocked。
无正式数据库写入、迁移、重启、发布或标签打印；候选仍未提交，整体未完成。git diff --check通过。下一步优先双数量整合与历史逐批校正预览/正式发布门禁，模具候选随发布执行且需刷新正式基线和已验证备份。

## 双数量整合回归及历史逐批预览（2026-09-26，未发布）
整合12测试文件覆盖订单实收/送货、备库直送真实采购入库链、成品预占、补库草稿、旧外购与超量收料、模板管理员/针式布局、模具名称：144 passed267.39s；JUnit D:/.codex/workspace_artifacts/delivery_physical_20260926/integration-current.xml。补充receipt_purpose_flow、common_box_low_stock_alert、semi_finished_inventory_frontend三文件102通过2失败183.10s：失败均旧断言，生产拿货来源已加12项原料事实字段，按真实库位和固定材质/规格/数量补齐严格dict断言；前端函数分隔原只匹配单参数签名，现在双参数造成截入后续调价confirm，改按函数名左括号界定。定向两项2 passed7.54s，无运行代码改动。JS两个验证脚本PASS（双数量输入/跨批/共享库存/保存重开/纸质拿货与预警确认取消/权限/采购预览/打印失败后提醒）。git diff --check通过。
只读正式库新鲜事务审计时间2026-09-26T14:51:19Z：YL136/3807/Z.004.000006身份核实，账面可用2982/预占0/净消耗1700。6送货中3有效600/600/500，3作废净0；消费3500逆转1800；无新调整类型。实收7=4042片/旧转入2021，采购14冻结单价1.97、金额7962.74。收料源批次442→639至645追溯同receipt7；批次639—643每批补记300/补扣300净0，644原100补记300补扣200→200，645原221补记221→442。入库校正合计2021、出库-1700；搬入656—662保持2661；理论3303。只读预览含逐笔有效/作废、批次版本、流水ID、源hash及原始证据，未更新任何余额/客户历史数量/成本。
输出history-current/batch-correction-preview.json与逐批次校正预览.md。源hash d84e3a13e3e4db7fdb5ec11f3763f666fd1c7ce49a0754f4f5c306a09fc64023。4043报料额外1片不自动作为实收；原搬入成本7.71不除2；NULL旧成本不自动覆盖。此为校正审核预览，还缺校正流水执行/历史取消链/幂等与原子演练，不能以算式成立当作可直接正式执行。
本轮无正式写库、发布、迁移、重启或模具登记。用户共享模具实际位置问题仍待答，但不阻塞独立推进。下一步优先历史校正台账与隔离演练、特殊补库路线收口及发布门禁。目标未完成，候选仍未提交。

## 2661搬入与4043报料后续方案
本轮只读重核正式库存2982/预占0/净消耗1700，输出history-current/下一步实际处理方案.md。2661搬入只计一次，4043报料与4042实收差1不自动入账；入库+2021和出库-1700分开，理论3303，不盘点。历史内部ledger候选字段customer_id修为owner_customer_id，AST与diff通过，功能测试及取消/成本/原子链未完成，不暴露正式执行入口。无正式写库、迁移或发布；NAS独立回执20260926_2661搬入4043报料后续实际方案。

## 历史校正台账与撤销链隔离回归（未发布）
新增内部历史校正台账服务historical_quantity_ledger：客户/产品/原流水身份、有效管理员、事件整数量范围、完整余额及版本、预占异常品、非负库存、已取消或存在冲回源拒绝、逐原流水固定幂等键及异载荷拒绝。入库与出库分别新增流水，旧流水和客户送货数量不变。无正式API/CLI执行入口。

两项真实失败复现并修复：
1. 隔离旧单客户100、旧扣100，入库补记200、出库补扣100后取消发货，原返回300可用/100消耗，应400/0。取消及短收冲回现在读取原consume关联校正流水计算实物；旧allocation和reversal_quantity仍保持旧口径，真实库存流水记实物。撤销回单以原冲回流水实物再扣，校验身份/数量，沿用后来出库禁止撤回与栈板投影门禁。
2. SQLite SELECT未实际BEGIN时先SAVEPOINT/RELEASE会提前提交，调用者rollback无法撤回校正。新增测试失败复现后，校正批次在没有真实外层事务时BEGIN IMMEDIATE，已有事务不重启，保存点内错误及调用者后续rollback均能回滚。

## 验证
历史校正12项+未来双数量7项：19 passed42.40s，证据history-current/ledger-regression.xml。
旧无订单送货全文件：26 passed66.46s，证据history-current/unordered-regression.xml。
12项覆盖分开校正、跨会话重复执行、旧源与客户数量不改、异载荷拒绝、第二事件失败后调用者commit也无部分入账、停用管理员及错误身份拒绝、取消完整恢复及重复取消不重复恢复、90/99客户实收对应20/2实物回库、编辑原回单重放两个周期、取消源拒绝、非法事件、外层rollback。
旧测试此前缺少现行成本尺寸/材质/供应商资料导致26项在seed失败，补齐隔离测试资料；价格Numeric(18,6)已有，三处字符串四位比较改Decimal精确数值比较。未放宽生产门禁。既有JWT/依赖弃用warnings仍在。
git diff --check通过。

## 下一步及边界
还需完整历史计划的真实隔离副本批次演练：严格源指纹及入库移库血缘、全批次计划完整性、物理单位与批次实物标记原子激活、成本按真实receipt证据守恒、搬入2661不重复且不重算成本，应用后的业务查询与未来送货接续。当前内部台账不能直接当正式校正执行器。历史客户100等商业数量保持，未据主档改写历史。
按已审计方案入库+2021、出库-1700、2661搬入不变，3303为理论，4043/4042差1待核。本轮不盘点，未写正式数据库、未迁移、未重启、未发布。正式历史校正仍须老板针对最终证据确认。
其他待完成：特殊补库路线、模具受控正式登记及共享位置待核、候选提交整合和发布门禁、管理员验收。目标保持未完成。

## 2026-09-26 正式v503整合与历史整批校正演练（候选未发布）
实际发现正式已v0.22.503，运行源码0acfdaaff06c63d4dce6df31d606f022294f6d02，包c5975088，数据库ea0926是管理员回退/开票归档，非本任务数量迁移。最新fetch确认origin/factory-current-baseline=4d4926b0。先保存候选68a8206b，再仅在任务分支合入该正式基线；三个迁移文档保留双方记录，其余代码自动整合。双数量迁移从未发布ea0926重命名eb0926dq，前驱正式ea0926，保留正式迁移文件，不stamp或修改正式库。
新增只允许workspace_artifacts新目录的rehearse_yl_quantity_correction.py：正式源mode=ro backup，严格原批次和流水全集逐字段核对、客户/产品/实收/有效与作废送货身份，源变则拒绝。13笔校正与14批次物理标记/成本及审计同一事务；重复跨会话执行0新增。库存内部boxes/sheets分类保持，展示片来自物理标记，不能把lot.unit直接写片（真实约束拒绝已修）。校正流水明确物理单位片。
演练history-rehearsal-4通过：2982+2021-1700=3303；净消耗3400；搬入2661及7.71成本保持；实收来源7批按采购1.97每片合计7962.74，NULL原成本从原凭证补齐且审计保留原值，不改历史客户单和结算。仅inventory_lots/inventory_movements/finished_goods_inventory_details/operation_logs四表变化，301张其余表哈希不变，integrity ok/FK0。证据evidence.json。完整正式执行器仍未暴露，当前仅隔离演练。
真实已校正副本另复制future-flow.sqlite3，通过实际FastAPI备库直送：YL/00006客户输入50，客户打印50、实际拿货100，批次645从442扣至342，重复确认仍342，取消回442。仅隔离app覆盖DB/当前管理员依赖，未改账号密码；证据future-flow-evidence.json。
新链ea0926→eb0926dq→ea0926→eb0926dq演练通过：新鲜只读源SHA256 3c0ee2acaff7fc39cb6beaf5b6fea003bf3aba81665be799246af37fc1240cc7；304原业务表字段hash、1029索引/触发器不变；4类新事实拒绝有损降级且文件hash不变，integrity ok/FK0。证据migration-eb0926dq-run1/evidence.json。此只读副本不是正式部署停服备份。
整合回归49 passed129.20s：历史校正12、未来双数量7、外购送货23、正式移库撤销5、全仓产品数量2。后补校正流水片单位断言1 passed5.74s；两个前端验证脚本PASS，含自动合并后inline语法。git diff检查通过。
已异步请求老板针对最终历史校正及凭证成本统一结果确认，未收到答复不正式执行；此确认来自老板历史数据边界，不是程序发布二次审批。共享模具实际位置确认仍待答。接续特殊补库路线、正式模具登记准备、发布门禁及管理员验收；目标未完成，本轮无正式迁移、业务写入、停服或发布。

## 灰底白卡预警报料收口与v504发布候选
复现灰底白卡发货预警无proposed_items：stock_policy_replenishment_draft把所有external_purchase都路由为直接外购成品。按既有规则排除coated_board及真实组合，继续原纸板/BOM路线，资料来自常用箱，不造供应商或采购事实。普通/灰卡两项冻结计划、资料变化409、重复确认一单及待报料覆盖测试通过2 passed8.63s；预警+补库+旧外购61 passed124.60s。灰卡草稿为paperboard/semi_finished、层数1/NONE，按开料规则报原料。
准备v0.22.504，当前正式仍v503/ea0926；候选唯一eb0926dq，已有304表/1029对象升降升证据，完整性/FK与新事实拒降保护通过。历史校正批准和共享模具现场位置仍待回复；程序升级不执行这两项未确认历史事实。正式签名包、Manager时点备份、迁移和健康检查尚未执行。

## 2026-09-27 v504技术发布完成
Manager已部署代码bfad73a4、包67bc4d38，正式eb0926dq，304原业务表事实不变；NAS时点备份20260927-000255-6f557716已解密及哈希验证。三入口健康/引用静态资源/匿名权限核对通过，NAS更新源已同步v0.22.504。详见release_reports/DELIVERY_PHYSICAL_20260926.md及release504/formal-release-result.json。正式页面未自动点击，管理员验收待完成。历史校正仍未批准/执行，不能把程序发布当历史库存已修正。
模具新鲜只读计划与已演练指纹一致：231创建、2复用、26待核，准备整批事务失败回滚验证后按授权登记，共享物理位置待确认。

## 2026-09-27 模具正式登记完成
231新增（YG123/GY105/YL3）＋2已有R04模具名称更新已正式提交；在用资料明确233款，剩24停用款和2共享模具位置待确认。新正式计划reuse_bound233/review26，指纹bcd1698121ea9a8e09d8296d2ecbbc17627e2d358cd53d135fc5c4019c058eb1。
整批API事务在隔离副本验证故障全部回滚、重试拒绝旧指纹且零新增；正式只改6张授权模具关联/版本审计表，299其他表不变，并逐字段核对非目标产品/模具。NAS时点备份20260927-002914-7f9c942c.tmbackup，完整性/FK与三入口健康通过。首轮失败备份未触发业务写入，清理本任务重复临时文件及归档隔离副本后重新备份通过才登记；没有删除更新器迁移目录。证据molds/formal-registration/result.json、atomic-rehearsal-2/evidence.json；操作脚本在同目录，辅助脚本SHA256 4aa554b2899be30725062003a5d624a91a54d16dc79d1caf7cf9281417db086b。
正式历史库存仍未校正，待老板批准2982+2021-1700=3303方案；暂不盘点。已询问24停用款是否仍需登记，尚未得到回复；共享模具实际位置问题亦未回复。程序/模具的管理员人工验收未代填通过。任务整体仍未完成。

## 2026-09-27 老板更新实物1890及模具打印排序
老板确认当前实物1890片，先前盘点入库已包括报料数量，之后已出两批。此事实推翻把2661搬入和4042报料实收完全独立累加的前提；此前3303方案暂停，禁止执行旧+2021/-1700计划。需明确1890截止时点、所指两批送货及原盘点日期/数量，重建盘点前后事件；当前2982与1890净差1092仅为待核差额，不直接写余额或全部归为换算差异。
本轮最小代码闭环：模具批量标签默认按标签名称/存货编码的数字自然顺序打印（2在10之前、00006在00012之前）；保留名称/货位/选择顺序选项、纸张版式、客户权限和原打印任务。现状页面默认选择顺序，根因不是缺少数字比较能力，而是未默认按编号。历史库存本轮只读，打印排序独立验证发布。

老板再次明确1890片截至9月27日最新，过去只盘点一次、采购一次，早期合作厂搬入量不清楚。不再追问无法回忆的历史数量。账面2982分布在9个三楼D4货位，已询问现1890的实际存放分布；差额1092待核，禁止任意摊到批次或沿用3303。
v0.22.505已正式部署，源码c8f4a0fa、包7a49038b，数据库eb0926dq无迁移，304表业务事实不变。NAS备份20260927-084636-43f23ed9已校验，健康及正式标签HTML哈希/默认数字排序通过。实际JS函数验证通过、相邻打印恢复6 passed；未做正式自动点击。详见release_reports/MOLD_LABEL_NUMBER_20260927.md。旧计划JSON已superseded且旧审核表标停用，不会执行旧+2021/-1700方案。当前1890校正未入账，待货位事实及具体方案确认。

## 2026-09-27 最新货位盘点后实物单位正式启用
老板确认最新00006货位盘点已完成、可以继续。新鲜只读核实9张管理员已审核盘点33—41、4次移库，盘点净-1092，正式已为1890片/预占0；不再补扣，不执行旧3303方案。7个有库存批次656/657/659/985/986/987/988：6批280、1批210，全部原实物单位标记为空。

按本次继续授权，在独立正式副本验证只补实物单位quantity_basis标记，数量、货位、成本、历史单据和流水不变。第二批失败整库回滚、重放无变化；真实送货API客户100/打印100/拿货200，草稿1890、发货1690、重复确认409不再扣、取消1890。301其他表hash不变，完整性ok/FK0。

正式Manager备份20260927-094849-43e230a0.tmbackup校验通过后整批事务登记7条零数量流水3174—3180，逐字段核对全部库存余额/成本不变及旧流水不变；服务恢复、三入口健康通过。运行仍v505/eb0926dq，无发布或迁移。远端7b923861是另一分类候选，本轮不合入或代发布。正式页面和实体打印待管理员验收，既有共享/停用模具事实待核。盘点前历史送货未重写换算或取消规则，不能把当前单位启用当历史逐笔补扣已完成。

完整结果及证据见release_reports/YL00006_COUNT_20260927.md、NAS独立回执20260927_YL00006盘点后实物单位启用。当前库存单位启用闭环完成；旧历史补扣计划永久保持superseded。

## 2026-09-27 停用产品实体模具与共用模具R04
老板补齐现场事实后恢复任务。23款无身份冲突的停用产品通过原模具创建API及已有主数据版本/CAS/审计服务登记，产品不启用、不改工艺；YL模具161按实物确认移到R04，保留原本体及两款绑定。原同码冲突3660/3594（研光/光洋80011946 APS4）已询问共用或各一块，未答前不复制或合并。正式数据批次故障/重放隔离验证、备份和298其他表不变通过。

发现停用产品的实体模具标签被active产品过滤器拒绝，补充仅标签读取可采用正式客户身份下未删除的既有停用产品绑定，不放宽生产选款或普通绑定。5项专测及客户范围/已确认分类整合41项通过；共用模具两个完整品名过长，按共同产品名称作简写，两个完整编码与各自完整主档名称保留。24个模具两纸型48次隔离投影和正式只读校验通过，00006库存仍1890。实体打印未代验收。

候选合入并发已正式部署v506及其回执；本轮v507/adc02b97已正式部署，无迁移，head eb0926dq。新备份20260927-113451-5bda2870校验通过、原304张业务表程序切换前后事实核对，另独立审计模具161中文简写；三入口健康/静态/匿名401通过。完整回执release_reports/MOLD_INACTIVE_R04_20260927.md，NAS同名独立目录。剩余仅80011946身份确认及管理员实际打印反馈，未将整体目标标完成。

