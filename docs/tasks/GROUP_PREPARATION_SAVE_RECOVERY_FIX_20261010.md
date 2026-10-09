# GROUP-PREPARATION-SAVE-RECOVERY-FIX-20261010

持续Goal下一最小修复闭环。根已完整读group audit/api/REPORT.md和独立group review/PLAN.md，复用8真实HTTP＋3实际页面方法：group_job保存入库→dispose/group-actions，确认空2xx误成功关框、unknown原key/body仅内存且改数/关重开会丢失；缺原保存key只读查询。原同key幂等、版本、数量事务及权限有效，不虚构原键二扣。共享Axios迟到身份副作用按必要恢复链补测。不凑Bug数、不混入single已发布结果。

正式及NAS现在已v0.22.603，源码5b167953446d07d9b77718e05061099803932cfb，包b3801ecce4ceda35770869dabd771d01c7d0cff2d8b7ce6747106a5a16af6b4e，en1009hp无迁移；新冷备恢复、323表/附件保持、健康/资源已通过，现场验收pending。根分支codex/group-completion-recovery-release-20261010；两个新managed树均从5b167953正式源码创建注册。开始先核干净HEAD和版本，建各自codex/分支；旧API/UI树仍被合成服务引用，不可切换或修改。本会话无Git push，不改正式业务历史。

启动顺序CODEX_START→NAS AI_START→本卡→ORDER_FLOW→总需求7/14/17及章程3～9。已读未变上下文复用，新增只读审查报告/独立方案必须读。本卡允许API/UI按互斥文件并行、先由API确定CONTRACT.md再接完整成功证明；根串行整合和正式发布。

## 最小合同及不变量

1. 只修group_job action=dispose，包括现有semi分存和明确finished成套去向。不扩plan/complete/assemble/store_outputs/revert的业务算法。group_stock同group后续入口只增加未知结果锁。单项加工v603整个49场景功能须保持。
2. 每job消耗全部已计划投入；semi实际产出14/19仍全部投入15/20，没有single分批投入continuation。finished先有子件生产，再按冻结配比实际组套，可能少套/超产留下余片，assembly.inputs仅take>0成员；不能要求所有job都被组套消耗或全部产出清零。同子件可多个receipt/job，按ID映射，不按数组位置/每产品一job假设，不混加片数与套数，不更改数量算法。
3. 新鲜dispose父Command.result_json增加不可变group_completion_receipt，必须在dispose末尾父首次INSERT前同原atomic_bom锁内构造/校验/完整序列化。允许dispose增加默认None的result_builder，仅新父INSERT前调用；dispatcher仅向dispose转发。旧重放先return不跑builder。保持sprep0912不可UPDATE/DELETE触发器，不能API拿已flush结果再UPDATE。builder/flush/审计失败和所有子件/finished组套整体回滚。
4. proof冻结原key/历史actor/规范化完整原GroupAction、group_key及parent/冻结recipe身份；逐job/receipt/source lot/原来源客户nullable、原版本、实际计划投入/产出、消耗reservation/movement、首次manual_in产出lot/movement/一次台账量与单位、原位置/布局及物理标签。semi明确无assembly；finished另列本次assembly key/sets、冻结配比、真实非零输入consume与成套manual_in输出/位置/单位。不能把当前lot余量当本次产量，不能用当前BOM或重复乘模数/开料。不给成本字段，不补造空标签/楼层/单位；recipe客户与每receipt nullable客户分列，保留原合法业务门禁。
5. GroupAction新增optional严格正整数expected_actor_id，排除旧model_dump/签名；当前认证与原body owner均核。原规范化只去掉jobs内lot_id=null，保留全部默认和原jobs数组顺序，compact JSON字节不改。首回包与同key重放整个JSON相同，不新增动态replayed字段。原key异action/actor/body冲突不当未查到。
6. API先定只读group-completion-result实际URL/包络/证明合同给UI和根。仅完整原key/body/actor/parent/group与持久proof相符才completed；current_actor单列。readonly独立解析当前parent/每原receipt身份权限，并核原冻结recipe/来源客户；不调用当前active/BOM/pending/posted等写资格，不用当前组成员重造旧结果。业务no_autoflush/零flush/commit，标准拒绝安全审计保持，no-store。not_recorded只是时点未见，不清键/隐式执行。旧无proof只legacy_trace＋安全组history入口，不从当前job/lot补proof，坏证据保留待核对。
7. 首Rejected如提供，需同写锁内无父key、进入事务且commit前业务失败、rollback成功；未知后收到任何后续错误仍保留。禁止永久结束marker、新migration、改旧事实和接管别人的记录。
8. UI首POST前可靠保存并读回actor＋独立operation_key、原endpoint/完整不可变body、group_key及parent/逐子件原数量位置摘要；失败零POST。按同actor原group_key锁unknown，不按parent/product全局锁；同组变成group_stock后的store_outputs/assemble入口也先核原unknown，异组可用。原请求数组顺序和job映射保留；不以新默认子件数量重建旧请求。跨页存储非原子锁，不夸大跨设备保证。
9. 恢复卡不依赖列表empty/error，主要为查原结果和可读加工记录；not_recorded后才允许明确按原内容继续，严格原key/body。真实在途关闭/忙态一致，旧账号/旧窗口迟到401/成功不影响新用户；局部有限超时通道覆盖本group保存/恢复/选位/地图GET和其后刷新，当前401保留原失效规则。不改全局Axios，不重复普通确认；实际超产仅原有一次。
10. 确认成功与列表/缓存清理失败分别显示。semi逐款投入、产出及各自位置；finished子件加工与成套入库分列，留余片时不称全部组装。普通页面保持简洁，原请求技术标识放详情，数量/位置/异常和动作直接显示。成功卡保留可读history入口，不能导航到原始JSON。

## 文件所有权

- API /root/mobile_drawings_api：D:/.codex/worktrees/group-save-recovery-api-20261010/纸箱厂erp软件搭建。独占app/api/stock_preparation.py（仅group入口/新resolver/GroupAction字段，不改single合同）、新app/services/stock_preparation_group_recovery.py、app/services/stock_preparation_disposition.py（仅父INSERT前可选builder）、app/services/stock_preparation_groups.py（仅可选参数转发dispose）、新tests/test_stock_preparation_group_recovery_api.py。
- UI /root/mobile_drawings_ui：D:/.codex/worktrees/group-save-recovery-ui-20261010/纸箱厂erp软件搭建。独占static/index.html局部script/恢复卡入口、static/ui/production-workspace.js实际group保存/同组入口锁和生命周期、新static/ui/stock-preparation-group-recovery.js、必要同名CSS、新tests/stock_preparation_group_recovery.cjs及test_stock_preparation_group_recovery_ui.py。保留single恢复模块不改、全局Axios不改。
- 根：卡/文档/版本、独立审核、整合与发布串行。独立review只写artifact，不写上述源码。同一文件单写入负责人。

## 验证和交付

先建安全红，实施后覆盖真实semi全量/少产、finished完整/少套余片、同子件多receipt/job（后二者此前仅源码推导，本卡须定向实测）、原key完全重放、异body/身份/旧版本、旧lot_id=null与数组顺序签名、不可改trigger、builder/审计前全回滚、commit真成功后丢ack、readonly原来源/位置/BOM后来改变、nullable/ledger与物理单位、旧proof缺失trace、在途未见后原POST完成。API导精确最终SHA真实HTTP原文供实际UI方法消费；UI实测坏2xx、刷新/关闭重开、同组group_stock锁/异组、旧账号401/成功、列表/清缓存失败和原body只读核对零新业务POST。按共享风险保留最短single邻近保护，UI单项49组在最终候选复用运行一次即可，不扩全仓。

本轮不再尝试绕过已被自动审批拒绝的Chrome启动/旧进程停止，旧18161/18162/18163与工作树保留。技术验收采用实际HTML/mixin＋最终真实HTTP＋项目已有真实Vue编译/非DOM事件验证；浏览器DOM/Cookie/存储、窄屏及管理员现场仍pending，不冒称截图通过。不自动点击正式页面，不用IAB，不安装无关依赖。每线提交精确候选、源指纹、失败/纠正记录、REPORT和NAS独立回执，不push或自行发布。根完成独立复核、签名、验证冷备、正式事实保持、健康/资源后单独发版。
