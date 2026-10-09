# GROUP-ASSEMBLY-SAVE-RECOVERY-FIX-20261010

2026-10-10，承接已完成的9个真实HTTP和10个实际UI观察审查。根方案见 docs/reports/GROUP_ASSEMBLY_SAVE_RECOVERY_PLAN_20261010.md；独立审者已明确“无架构阻断，可按最小卡实施”，完整PLAN随本任务证据归档。只修已加工子件 group_stock / action=assemble 保存结果恢复。持续目标仍active，不宣称全系统无Bug。

## 当前基线与授权

正式及NAS v0.22.604，源75129569ed5ff0bf454d075471c9b4fd902b070f，包077c2cde0a2d54932987baa2a960aa5934f66dfb17194b273bd21051e9fa6081，唯一head en1009hp，无本任务迁移。根88ea6aab只在正式之上追加文档；origin/factory-current-baseline仍已核验的旧祖先ec60eb1e1e6100c7e0b1157733eaa0af8713488a，不回退到该旧指针。最终发布前再次实时核验。

老板已授权审查、优化、修复、提交发布；根完成定向测试、独立复核、签名、标准Manager新冷备独立恢复和原事实保持门禁后可发布，目标版本暂定v605，若基线变动先协调。会话不Git push。不改正式历史业务数据，不重写数量、成本、货位或历史证明。不调用正式浏览器、不用IAB、不重试此前被自动审批拒绝的Chrome/旧PID操作。

沿 CODEX_START → NAS AI_START → 本卡 → ORDER_FLOW → 总需求7/14/17、章程3～9启动；已读未变部分复用。发布沿三迁移文档和NAS数据边界，旧抽取入口仍退役。一个最小闭环，保留无关改动。

## 文件负责人及工作树

本卡明确允许API、UI、独立审查并行，每文件唯一写入人。当前group API/UI管理树已确认干净、所有前台探针结束且无关联存活运行进程；可复用，从正式751分别新建下列分支，旧分支保留。不得编辑/切换早先有旧服务引用的production-save或chain树。

- API /root/mobile_drawings_api：D:/.codex/worktrees/group-save-recovery-api-20261010/纸箱厂erp软件搭建，新分支codex/group-assembly-recovery-api-20261010。仅 app/api/stock_preparation.py、app/services/stock_preparation_assembly_recovery.py（新）、app/services/stock_preparation_disposition.py、app/services/stock_preparation_groups.py、tests/test_stock_preparation_assembly_recovery_api.py（新）。先交精确CONTRACT.md给UI/审者，后交真实HTTP包。artifact独占 group-assembly-save-recovery-fix/api。
- UI /root/mobile_drawings_ui：D:/.codex/worktrees/group-save-recovery-ui-20261010/纸箱厂erp软件搭建，新分支codex/group-assembly-recovery-ui-20261010。仅 static/index.html、static/ui/production-workspace.js、static/ui/stock-preparation-assembly-recovery.js（新）、tests/stock_preparation_assembly_recovery.cjs、tests/test_stock_preparation_assembly_recovery_ui.py；必要时专用CSS。相邻group/single旧CJS只允许加载新组件的测试环境适配，不删除或弱化原用例。artifact独占 group-assembly-save-recovery-fix/ui。
- 独立审者 /root/order_recovery_review：只读上述源码，自己的artifact/review独立探针/报告；不改候选源码。
- 根：审查报告、任务卡、版本、整合和发布门禁；不与API/UI抢文件。

## 不变量与实现合同

1. 后端专用只读原组套结果入口，使用原operation_key+完整原GroupAction+当前认证owner。当前权限/原来源归属与冻结customer都核验；不能依赖当前可用量、来源posted/active或当前BOM写入资格证明旧保存。
2. 原完整业务签名保持默认字段、原jobs数组顺序及lot_id:null排除；expected_actor_id严格可选、排除旧业务签名。原assemble忽略的disposition/actual_output/原料lot_version/行目标仍签名，不新增这些字段等同成套事实的限制。页面新请求总绑定actor。
3. 仅assemble默认兼容可选builder，独立assembly_result_builder转发；在最终Command原来的首次INSERT位置前冻结，绝不UPDATE。保留输入证据→成套输出/成本/栈板→最终Command/审计→enroll_completed_bom→commit的原顺序和事务。dispose内嵌assemble不传新builder，已发布结果合同保持。
4. receipt保留完整规范原请求、历史actor、冻结父子配比、原来源/位置/单位、完整members和实际非零inputs、成套lot/流水与实际套数。每个成员按job/lot定位，支持同产品多job和take0。余片取本次consume前后余额，take0取同锁结束余额，不用最初加工量倒算。证明不含成本、不声称共用登记结果。
5. fresh写仍保留原来源/状态/数量/版本/成本/位置/权限和审计门禁，在原写锁内校验；原key同body重放零新增。全部响应检查序列化在commit前；首次/重放写JSON一致。fresh确已回滚且未commit才可独占Rejected、去掉Preserve；未知历史永不因后来Rejected丢失。
6. readonly查询无业务DML/commit；原key不存在仅时点not_recorded。旧无充分证明legacy_trace，不从当前主档/库存拼造旧证明，不回填历史。错body/actor/身份/证明完整性拒绝且保留请求。
7. UI首POST前可靠独立存储actor/key/fullbody，可读套数/来源/目标摘要；存储失败零POST。同组assemble unknown与dispose unknown互通，只锁同组group_stock后续写，异组可用；开窗和实际POST都复核。空列表或列表错误仍能看恢复卡。
8. 完整回执才确认；未知先只读核对，员工明确继续只发原key/body。bounded局部认证通道覆盖本次保存、核对及其刷新/追溯；迟到旧401不影响新账号，当前401仍生效。保留busy/epoch门禁。
9. 成功、列表/历史失败、本机清理失败分别呈现；余片标“本次组套后余片”，不冒充实时库存。旧历史读失败给管理员依据，不删除确认。普通保存不加确认，无永久注销/跨账号接管。

## 必须交付的最短证据

合成tmp_path库和既有安全conftest runner，不复制正式库；新API测试覆盖真实正常/部分/多来源和两次组套、旧忽略字段合法兼容、同键重放/异body/旧版本、身份/权限、append-only、故障回滚及真正commit后丢ack、在途not_recorded和只读零业务写；对已发布single/dispose有确切相邻回归。不扩大无关全仓测试。

UI消费API实际HTTP原文，验证实际HTML→方法原payload，坏2xx、原内容生命周期、storage失败、同组锁和他组可用、actor迟到、成功后刷新失败、legacy/not_recorded具体操作、原单位和多来源余片。记录真实源SHA/HTTP文件SHA，不用自造成功JSON冒充联调。API早交合同，UI不得猜字段。独立审者至少执行自己的高价值边界探针及必要真实Vue模板技术检查；浏览器DOM/Cookie/localStorage和管理员现场验收仍单列pending。

候选提交、差异检查、NAS独立回执后交根；根按正式751精确整合，核文件范围/旧合同/迁移唯一头/签名资产/新NAS冷备恢复/原业务表和附件/健康和资源，再发布NAS。发布前始终写候选，发布后“技术发布完成，待管理员人工验收”。
