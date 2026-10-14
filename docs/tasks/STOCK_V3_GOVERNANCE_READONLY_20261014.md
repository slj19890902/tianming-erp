# 691批现存库存只读治理与实际链核验（Sol）

本阶段仅分析既有证据和指定 run_task 隔离基线。源为10:49:20抓取的 current-business-readonly.json，1430条库存行与原始隔离副本逐字段一致；副本 SHA256 为 ab607153accc3950c5c0f14e4584b559226e813e8243e5140cdb32e5c753b4b4，每轮 mode=ro + query_only 读取前后相同。未打开正式路径，未写基线、未启用严格过滤、未运行服务。

逐批证据输出：`/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/stock-governance-sol/all-live-stock-evidence.json`（约8MB，691批每批原basis、数量单位、位置、typed源链、订单/生产/来料/盘点流水、出生时master部分版本、成本、候选与缺字段）及同目录 all-live-stock-ledger.txt、summary.json。审计工具为 scripts/audit/stock_v3_governance_20261014.py。此工具固定唯一批准的只读基线，不连接环境的正式DB。

|分类|批数|处理边界|
|---|---:|---|
|缺完整依据待核|165|未找到可唯一复制的完整typed源basis或冻结生产图；不由当前主档造完整历史|
|保留真实或尚未证明等价的历史差异|193|逐字段差异及旧冻结订单匹配单独列出；不自动判坏库存|
|仅工艺文字集合相同的规范化预览候选|25|只做预览，保留原文；任意工艺顺序不能通用排序|
|已有精确/已确认共享/完整组套依据|164|117精确+46共享+1组套；仍需实际交易测试，不能据此宣称全部订单适用|
|坏当前BOM|1|747，父3774；格挡3823 is_active=0，配方4衬板+3格挡+4护角|
|片料已保存原事实|132|原料/加工/已完成输出区分；未逐个做消费测试|
|片料资料或用途需核|11|7模切用途未确认+3无新profile+1输出当前身份不匹配；缺profile本身不是不可用证明|

691=548成品+143片料，不相加不同单位数量。原165缺、218差分类完全复现；218拆为25格式候选与193需保留差异。181仅production_process差，25只是字符串集合等同；shared服务的 `_process_key` 只规范化明确工序集合，并非支持任意顺序无损。当前产品候选和数量合同仍返回486批，含152缺依据、211差异，另17有可用量未返回。旧未发布strict函数按只读方式抽出比较，仅返回123（76精确+46共享+1组套），会隐藏363批。本阶段没有合并或启用它。

413 typed转移中204源批=目标批，是原批移动位置；209才是跨批追溯。不能把204条误判为循环。所有现存链无缺源、循环、多根或入库基本身份冲突。165缺依据中94来源transfer，不表示移库丢资料：追源后源批也没有完整basis；45有出生时master部分版本、120没有当时版本，旧快照还缺die_cut_path/production_notes/sheet_cutting_settings。部分历史数据可作为确认上下文，不能说“唯一完整恢复已可执行”。自动完整历史basis恢复预览0批；25格式预览和45部分历史证据应保留待核，不丢已知客户、编码、尺寸、材质、楞型字段。

143片料选明确product绑定或当前同矩形目标共33pair，通过实际候选及硬授权服务只读核验：25普通授权通过。680→3494通常因X616X/8INI8不同拒绝，reviewed授权通过。1099绑定3586（父件）材质代码空，不能假造子件；1358→3367硬授权通过但候选未返回；1359→3390候选和授权通过但direct=false；1427→3562可显示候选，但加工身份未匹配拒绝。故“3无profile全部不可用”“候选返回即已完成可抵扣”均不成立。33pair真实预占/释放在新的managed child另行验证，不修改基线事实。

现有冻结图可匹配7个live批次：682/683/747→10281；918→10353/10469；998→10546；1421→10552；1430→10546/10592。这是冻结身份比较，不保证订单仍开放、余量、成本、组套或送货事务可成功。

## 三个重点来源

666经 transfer154从608拆出210，608原盘点manual_in1936，库位移位2074/2075，源和目标basis均NULL。原独立底450×450×210/VSNIV/AB与订单10552所需一致，当时product3496 v7明确独立底、一片、单拼、只，production_process空；最新订单所需“无需结合”。成本11.4627来自已批准同昌套价14.58按参考材料权重分摊，成本详情还保留原4.4431和890/845旧双组件权重。这不是当前890一片新估价的依据，不能因组件计算已修就回写这笔冻结历史成本。

680有原人工入200张（movement2101）、原批移位200（3597）、ST-20261006盘点加4（3598），合计账204张，现无预占/消耗；原1980×1376、X616X、AB5、profile客户136、成本6.96/㎡×2.72448=18.9624/张。1430是10月14盘点manual_in4442新增50只，完整冻结8INI8双拼成品basis，按5/㎡×2.72448×2=27.2448/只，无原料680生产输入外键。用户确认100张原680做50只，若根批准以原库存参考成本继承：投入参考1896.24、现成品参考1362.24、差534.00、单位37.9248；这是有限更正预览而非现金成本或既有生产事实，不在本阶段执行。

000204采购链206→BomSource21(snapshot46)→materialPurpose247→ProcurementSourceLink31→supplier623/order209（09:01 confirmed100）真实存在，order direct字段为空不能说无采购。用户确认未真正发供应商、授权撤销错误100张，撤销和历史消耗/成品来源修正由根单独执行；本阶段保留现状证据。

## 666→10552用途确认差距

用户明确50套取自已登记上下盖，是此次50套用途的证据，不等于确认全部旧库存对所有新单可用。上盖唯一live1421(pid3787)basis与订单10552完整一致；下盖唯一live666(pid3496)basis缺失。订单冻结下盖basis包含：独立底450×450×210、VSNIV、AB5、单拼、pieces1、unit只、净料、无需结合、assembly空、无模具；还含旧base压线210/425/210及base_crease_type压线，这些是保留字段，不应作为独立底又有一片base的证明。原v7工艺空与冻结订单无需结合必须按此实际用途确认记载，不能无依据视为全量文字等价。

既有共享API无法承载本次同客户单一订单用途：`/finished/shared-stock/preview`要求2至20产品，每客户只允许一个，单[3496]真实服务调用409；绕过人数入口直接 `_preview_lots` 对666也409“缺少已确认完成状态、客户、零件或成套冻结配方”。confirm要求 product_ids、lot_ids、preview_hash(64hex)、operation_key(1..100)、evidence(1..1000)、physical_match_confirmed=true、活动admin及客户权限。内部维护补充仅接受已存在basis的spec/production_process，不能补整张NULL依据。历史BOM漏模具API仅支持有完整preparation_assembly来源的整套，666不属此类。

通用physical-identity确认API虽然可填NULL，预览读取当前master，要求preview_hash、operation_key、physical_match_confirmed=true/admin，且lot未占用/未消耗；它不是订单10552和数量50的有限兼容证明，不宜把用户“此次已取用50套”扩大为全210下盖完整新品身份确认。建议根记录订单10552、客户136、snapshot41下盖/42上盖（40为父3863）、下盖666/上盖1421、各50、原basis哈希（下盖NULL）、原lot/version/cost/数量、冻结expected basis/hash及用户确认原文的有限更正事实；保留原basis。需要的不是再猜一张历史basis，而是能表达本订单既成用途、幂等审计与原库存扣账的有限更正机制。本报告不创建此能力。

## managed child交易验证范围（根已确认）

用现有scripts/uat/run_task.py从唯一基线再建owned child，端口仅占配置值不启动服务；每pair创建测试新订单并冻结真实产品资料。读候选和资格后调用reserve_semi_finished_inventory，核验片/张守恒、同key重放、不同数量和旧version拒绝、release、剩余需求及采购张数恢复；业务拒绝记录后继续，不改资料转绿。每case显式outer BEGIN、所有操作无commit、最终rollback并核验相关表指纹；全场前后完整逻辑DB指纹、schema及baseline SHA一致。成功由run_task回收copies、保留结果；断言失败保留现场。成品164、历史用途登记、组套和送货留下一阶段，不冒充本次33pair已覆盖。任何需DDL或缺的新更正能力由根串行协调。

根随后已在managed child复现000204取消误采购→reviewed采用100成功→生产预览50/参考成本1896.24→consume阶段又拒材质差异；消费未继承reserve确认。这是实际闭环阻断，根独占修消费段。本33pair仅核预占/释放/缺口，不以本报告的reviewed资格通过证明实际消费已通过。

## 33pair实际子副本结果

工具 scripts/audit/run_stock_v3_pair_audit_20261014.py 通过既有run_task，使用真实canonical Python和明确venv site依赖，运行 scripts/audit/stock_v3_pair_rollback_20261014.py。最终任务 task-9dfd699c4c48492a822ffc3caaa29961 returncode=0，33pair完成、24预占/释放全链通过、9业务拒绝；source仍为唯一原始隔离副本。

逐pair JSON/TXT：证据目录 managed-pair-results.json、managed-pair-results.txt，含实际候选/警告/可选性、资格、预占和释放后的原生库存张数、需求片数与采购张数、同key重放/不同数量/过期version拒绝、逐case表指纹、原材质及成本不改、需求表回滚。新需求payload无效的1099不创建需求或预占；其422字段为board_length_mm、board_width_mm、material_code、flute_type。

拒绝9pair：664→3826/3827/3828、680→3494、734→941、735→2617均为原材质与目标不同；1099→3586为绑定父件缺真实需求字段；1358→3367为board_length_mm/stock_yield_per_sheet差异，需要已确认override，不能由脚本假造；1427→3562仍加工用途未知。1359→3390实际候选selectable=true、direct=false，成功采用1张→片需求10降9→释放恢复10，与缺新profile就不能用的推断不同。

24成功pair实际保留需求，采购量按冻结供应商出数进位；验证不混加张、片、只或套。预占不消费，释放恢复原可用/占用，原库存身份、材质、历史成本和位置逐字段相同（仅当前事务version/时间正常变化，最终rollback全部恢复）。一成功pair在reserve流水后注入异常，nested事务回滚后的批次字段和需求等于注入前；全场outer事务每case rollback。

子副本全库逻辑指纹前后 ecad0c5799ab3bada63f4e4b0cef9b6f48cffff944befc0c2d76405ad564f565，schema一致；baseline仍 ab607153accc3950c5c0f14e4584b559226e813e8243e5140cdb32e5c753b4b4。未运行HTTP或后台服务，未commit子副本业务事务。

成功任务的既有自动回收尝试被desktop_assistant.cleanup.assert_idle阻止，原因“无法确认 ERP/测试进程已退出”，来自macOS应用进程psutil AccessDenied。copies retained，不能写“已回收”；已交根协调现有清理/权限边界，没有修改、绕过guard或换rm工具。三次先前harness失败也按规则保留：task-3d160a7d2e9b4c0e87c9fa4aa4f98784（venv symlink拒绝、尚无child启动）、task-48550b3ada174ec78cb1d1566938fac4（新订单customer-document隔离review缺失）、task-f0c26edebf7f484b984c8c393017d138（1099无效payload未经边界校验导致TypeError）。后两轮和最终轮均已经完整rollback并验证全库一致。修正仅在测试脚本加入隔离empty review及真实SemiRequirementPayload验证，无业务服务代码变更。empty review只服务仓库测试，不作为客户单据来源抽取验收。

pending-evidence-previews.json保留25工艺规范化预览和45历史部分上下文，共70，不是70完整身份修复，完整自动恢复0。此阶段审计文件均未触发正式动作；git diff --check通过。
