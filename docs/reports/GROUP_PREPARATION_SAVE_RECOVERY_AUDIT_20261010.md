# GROUP-PREPARATION-SAVE-RECOVERY 只读 API 审查

本轮完成只读审查，未实现或发布group修复。固定导入树HEAD c02693f8a678b1a72ed3de285b32633842b7c22c，Git clean；未checkout、改源码/仓库测试/版本/模型/迁移、push或操作引用服务/PID。仅本artifact/api合成探针和报告写入；与single候选分开。正式对照采用卡固定v602源码6640d5369e2d48ecba36be9aefae1afa9ed449db；不把正在串行发布的v603或当前编辑UI当作本轮已发布源。

source-fingerprints.json记录17个相关完整文件、API GroupAction模型/post_group_action/history_trace三个片段；均与6640d536 LF精确等价。整个stock_preparation.py已因single修复变化，报告只称上述group片段等价。未变ORDER_FLOW、总需求7/14/17与章程3～9复用并核等价。

## 实际入口与现行事务

正式HTML index4912的group_job“保存入库”明确调用saveStockDialog('dispose')；group_stock按钮会按去向改为store_outputs/assemble，不是本次主对象。无按钮的group complete不作主缺陷。saveStockDialog的group分支提交POST /api/production/stock-preparation/group-actions，payload包含dispose/disposition/parent_id/sets/basis_hash/group_key、每个job的ID/版本/原lot版本/实际产出/output_version/位置布局、顶层位置布局/超产确认和operation_key。真实JS安装后的方法生成payload与真实HTTP档案semi-full.json逐字段一致。

post_group_action沿RoleChecker(admin,boss)，先核当前parent客户和对应组的每个receipt客户，再atomic_bom。mutate_group分派dispose；dispose先查父Command，原actor及encode(payload)完全匹配才返回持久result。新写要求完整无重复的组成员，逐子件调用原complete：消耗每个job已计划投入，记录真实子件产出，默认semi分存；finished去向会在同一事务随后调用assemble，按原冻结配比消耗子件并产生真实成套输出。父Command在所有子件/可选组套之后首次INSERT+flush；API commit后直接返回已构造plain result，没有commit后再查生成回执的代码。

GroupAction.model_dump提供旧默认字段；API仅移除jobs中lot_id=null，随后compact encode(sort_keys=True,ensure_ascii=False,separators=(',',':'))保留jobs数组原顺序。签名含完整业务默认及key；没有expected_actor_id字段，未知owner提示被忽略。不能统一成single/mutate普通JSON或重新排序数组破坏旧键。Command actor/receipt/request/result持久且sprep0912触发器禁止UPDATE/DELETE；所有探针都显式装同款触发器，真实UPDATE/DELETE均拒绝。

semi父结果只有action=dispose、group_key、disposition、job_ids；finished额外带冻结assembly result（recipe/sets/inputs/输出lot等）。它没有完整本次子件投入、产出lot/位置/单位、当前认证身份或严格原请求回显，不能当作新完整完成证明。现GET history/{group_key}（原plan key）可查看当前组；history/{本次dispose operation_key}404。没有原dispose key＋完整body的专用readonly resolver。当前history可看原组累计/当前信息，不能用它冒充某次operation_key执行证明。

## 八个合成真实HTTP场景

最终api.xml 8 passed / 20.36s，test-nodes.json列精确节点。最初7项18.28s全部通过；只追加第8个合法nullable来源场景，未扩其它动作或仓库套件。全部用当前schema和真实采购/收料/计划/保存API；legacy fixture仅关闭新收料自动plan来建立仍合法的既有group任务，不涉及退役旧数据抽取层、正式库或旧系统。

1. semi全量产出：计划5套、子件投入15/20；实际产出15/20。改当前BOM配比为99后仍按冻结3/4完成。原key同body完整JSON一致，库存/Job/Command/流水/预占/业务审计零新增；异body同key409，新key旧版本409，事实不变。history组key200，保存key404。
2. semi少于理论产出：同样消耗全部已计划投入15/20，实际产出14/19，组任务均完成。这里“部分”指少于理论的实际产出，不是single分批实际投入，没有剩余continuation；原key重放零新增。
3. finished全量：先真实子件产出15/20，再按冻结3/4消耗并产生5套输出；同键零新增、旧版本/异载荷受保护。子件生产与组套事实分列，不能将35片与5套混计。
4. 第二child complete业务审计故障：HTTP500，已进入事务的第一child和后续所有库存/Job/子父Command/流水/预占/业务审计均回到提交前，恢复故障后同body200。
5. Session.commit真实成功后注入OperationalError：HTTP500，但父Command及两子件产出已持久；原key+原body POST重放200零新增。真实pending workspace已无该group_job；组history200，本次保存keyhistory404。committed-ack-loss.json同时保留持久Command、原body、真实GET与重放。错误状态不证明未执行。
6. body额外expected_actor_id=1，真实cookie为允许执行的boss2：200且Command.actor_id=2，request_json无该未知字段。admin1同key409；撤boss权限为sales/selected无客户后写403、history403，匿名写401；业务事实不变。admin/boss按原规则全客户，不伪造受限boss。owner提示当前未声明，不单独计权限Bug，作为新恢复合同必要首写保护。
7. 首child正在事务内、父Command尚未提交时，真实GET组history404且workspace仍可见旧pending group_job；放行原POST后200，history200。查询时点未见不能证明原请求终止；重开旧组再换key仍受已测job/lot CAS保护，不称原请求二扣。
8. 已有group的StockReplenishmentOrderItem.customer_id为nullable，保留原frozen recipe customer身份；真实semi dispose仍200、重放零新增，产出profile customer_ids=[null]。未来proof应分列冻结recipe客户和每个原receipt客户nullable，不为证明新加正整数门槛。Product.customer_id非空，不能把parent客户也随便改null或伪造不存在的合法场景。

各场景有同名真实JSON。底层semi lot/unit及input consume movement为sheets，而实际已加工子件的profile.quantity_unit=pieces；产品/组套可读标签按冻结basis/recipe而定。现行集团完整产出及组套各自事实不能强令等于当前lot余量。模型nullable/实际来源如实记录，不猜单位、不扩大本轮算法。

## 三个实际函数观察与问题分类

frontend_entry.cjs加载真实production-workspace，调用实际openStockDialog/saveStockDialog，使用semi-full.json真实workspace作为数据及合成传输adapter，3项观察通过（frontend-entry.json）；这是实际控制器测试，不是Chrome/DOM、cookie或现场验收。

- group_job实际dispose payload与真实HTTP档案一致，空200 data=null仍toast“半成品已分存”并关框：确认不完整2xx误成功。
- unknown后编辑一款产出，会按新signature换operation_key；关闭重开同组会丢d.attempt，恢复原理论产出并换key：确认未知原body/key只保留内存，缺持久恢复。默认数量来自真实组数组，不猜排序。
- 第一版Node观察误把第一款固定认作15，实际数组首款为20，硬编码14断言失败；改为真实job ID对应原数量后通过。此为探针顺序错误，不当业务Bug。HTTP没有故障适配器错误。

这是上一single审查已明确保留的同类group剩余风险，不重复登记新的原key重复扣料Bug。现有原key/事务/CAS门禁有效，未发现本轮数量事务破坏或权限绕过。缺完整证明和原key只读入口，是同一未知结果恢复闭环需要补齐的能力；当前仍沿共享Axios的迟到认证副作用属相关剩余风险，本次未执行新的迟到401探针，不冒称已复测或已修。

## 下一最小方案（只建议，未实施）

限定group_job action=dispose；默认semi及明确finished两条现有去向都需按真实结果说明，绝不扩所有group动作。

稳定pending锁身份用原group_key＋原请求actor；每个持久记录键再包含独立operation_key/token，不能只actor/group覆盖。group半成品14/19仍是同一组全部计划投入的已完成事实；不发明部分投入continuation。相同group的dispose及紧接该组输出的新去向保存必须先核原unknown，其他无关group可继续；不能按parent/product全局锁住所有材料，也不能冒称localStorage跨设备原子锁。

父Command.result_json追加group_completion_receipt必须在dispose最后父Command首次INSERT之前完整构造，不能API取得已flush返回后UPDATE。最小可选result_builder默认None，旧重放return先于builder；只为实际dispose新写装配。group dispatcher最多传该可选参数，不动complete/assemble/store/revert算法。所有子件完成和finished组套后的proof校验/序列化在原atomic_bom内；任何异常整体回滚，append-only保护不禁用。

新冻结proof至少：schema/key/历史actor/规范化完整原GroupAction；原group_key/plan key、parent和冻结recipe身份（保留原名字/单位nullable）；每个原job/receipt/source lot及原客户nullable、本次投入、实际产出、consume reservation/movement、首次产出manual_in movement/lot/原位置与用途、物理label和ledger单位分别保存。semi明确无已组套事实；finished另列本次实际组套sets、冻结配比、输入consume movement和成套manual_in输出lot/原位置/真实数量单位。不能拿已被组套消耗后的子件当前余量证明原产出，也不能拿当前BOM重新计算历史。

拟readonly POST /api/production/stock-preparation/group-completion-result，请求operation_key＋完整original_request＋严格expected_actor_id；URL/字段须下一卡先定合同。先标准认证角色、当前parent/每原receipt客户，再冻结proof原客户范围；不跑当前active/pending/posted/库存版本等新写资格。持久原actor/key/action/group/parent/完整body及proof一致才completed，当前认证actor另列。no_autoflush/零业务commit，安全拒绝审计保留，no-store。只查询当前未见为not_recorded，不隐式POST、清键或换键。

现有旧Command原键精确查询能证明：该actor以该规范业务body/保存key留下原dispose结果，job_ids/可选assembly来自持久原result；它不能单凭小result证明全部原子件输出lot/来源/customer/位置单位已达到新proof完整合同。历史无新proof只返回legacy_trace＋原result/安全history组入口，不从当前Job/Lot/BOM补造completed；坏JSON/异body/异actor冲突保留。精确原POST重放仍兼容旧compact签名字节及jobs数组顺序。

GroupAction新增可选strict positive expected_actor_id，从旧业务dump/签名排除；首写与resolver均核本次认证账号及原body owner，不把expected字段当历史授权。错actor保留；首明确拒绝如实现，需与single同样真实事务阶段/锁内父key不存在/rollback成功保护，未知后任何错误不解锁。

UI首发前可靠存原完整body/key/actor/group和可读parent/子件摘要，写后读回不可靠则零POST；坏/空2xx、unknown及迟到请求保留。读取与“按原内容继续”分开，confirmed后列表/清缓存失败不再加工。后续group局部独立请求通道与生命周期门禁复用single已审模式，不改全局Axios、不用新key探测。旧缺body仅可读trace，不拿当前默认子件数量重建。

下一最小允许文件建议：app/api/stock_preparation.py、新group recovery helper、stock_preparation_disposition.py父INSERT前可选装配点、stock_preparation_groups.py仅传callback；UI实际production-workspace group分支和新独立组件；专用API/UI测试。新卡决定精确allowlist，不预先写任何代码/迁移或数据库终结标记。验收复用本8场景并加精确proof/readonly/坏2xx持久原请求/同组阻断/异组可用/生命周期与真实回执联测，正式现场另由管理员确认。
