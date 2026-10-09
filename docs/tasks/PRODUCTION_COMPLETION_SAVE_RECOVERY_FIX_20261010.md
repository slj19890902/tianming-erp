# PRODUCTION-COMPLETION-SAVE-RECOVERY-FIX-20261010

持续 Goal 的一个最小修复闭环。根已阅读 production-completion-result-audit/api/REPORT.md、ui/REPORT.md及独立方案要点；6项真实HTTP和14项实际页面观察确认：①single加工完成的空/坏2xx误报成功并关框；②未知请求不持久，编辑/关重开/刷新会丢原key/body，原20投入甚至恢复默认100；③局部生产请求的迟到401可先经共享拦截器注销新账号。原key重放不重复消耗；旧body换key被CAS挡住，但刷新后真正continuation新任务/newkey可合法再次加工，不能称原key二扣。首写actor字段原API未声明，不单独计权限Bug，作为恢复合同必要身份保护补齐。

当前正式及NAS已v0.22.602，源码6640d5369e2d48ecba36be9aefae1afa9ed449db，包9420d69b7eefa3622db0a7dada1bf98bde2ca858ec8dbb932def3b6726396839，en1009hp，无待执行迁移。本会话不push，不改正式业务历史。根保留当前文档分支；两个新managed worktree均从真实正式源码创建，开始先核干净HEAD并建codex/任务分支，旧API/UI树及仍被服务引用的源不能切换或修改。

启动顺序 CODEX_START→NAS AI_START→本卡→ORDER_FLOW→总需求7/14/17及执行章程3～9。已读未变部分复用。主路径严格为single_job“保存入库”→saveStockDialog('complete')→stockPrepAction→POST /api/production/stock-preparation/{receipt_id}/actions，action complete且actual_input_quantity已提供。group_job实际dispose/group-actions是已确认同类剩余风险，单列下一闭环；本卡不顺手改所有group/组套/撤销/plan/process算法或全局Axios。

## 授权方案和不变量

1. 新鲜single完成的父StockPreparationCommand.result_json在原事务中附不可变completion_receipt，首回执/原key重放/只读核对使用同一冻结证明。必须在与实际加工相同atomic_bom写锁内辨别已存在父key；历史重放不得用当前job/lot补写证明。独立复核发现sprep0912为Command安装拒绝UPDATE/DELETE触发器，故proof必须随父Command首次INSERT写入，不能在API收到已flush结果后更新命令，即便仍在同一事务。授权process增加可选result_builder=None，仅两处新父Command首次INSERT前调用专用builder；旧重放先return，默认None保留其它旧调用。proof构造、完整序列化/flush在commit前同一原子回滚边界，不改加工数量、冻结工艺、BOM、预占、位置资格、成本、来源或审计算法。
2. 完整证明至少覆盖schema、operation_key、历史actor、当前认证actor分列、receipt/source/customer、规范化原body、requested/original/completed job映射、本次实际投入和产出、输出lot/用途/单位及原位置、continuation及remaining。部分加工的完成job与原job允许按真实事务映射不同，不能拿当前累计库存当本次产出。只持久冻结本次可证事实，不虚构单位或财务事实；不返回成本。
3. expected_actor_id严格正整数、可选以兼容旧客户端，首写和只读核对前与认证比较；排除旧业务model_dump/签名。保留process compact encode与旧mutate普通JSON字节，不能统一编码破坏旧key。错actor/异body/已有key冲突保留未知；原权限及客户范围门禁全部保留。
4. 新增局部只读原请求核对接口，最终URL/JSON先由API明确发UI与根，不能各自猜字段。仅在原actor、receipt、key、完整原body及持久proof一致时completed；冻客户与当前来源均核当前范围，现admin/boss全客户规则不伪造受限角色测试。只读身份/客户校验不能复用prep.source的posted/purpose/has_raw_plan等写资格前置，已保存后来源状态改变仍可查原证明，不因此放宽任何写资格。只读不flush/commit业务，标准拒绝审计保留，结果no-store。not_recorded仅表示本次未查到，可能原请求仍在途，不允许自动续写、清键或换键。旧无新proof仅安全trace明确缺证，不从当前job/库存重建原产出；损坏/异身份保护保留。
5. UI首发前可靠保存当前actor、receipt/source、原endpoint/key/完整不可变body及可读摘要，写后读回失败禁止发出。每原请求独立存储键，按actor隔离；同receipt未知时包括新的continuation任务都禁止另建完成请求，其他无关receipt可操作。跨页存储不是原子锁，不夸大跨设备保证。
6. 查询与明确“按原内容继续”分开；后者严格原body/key，无隐式新POST。原结果未知后任何409/后续Rejected仍保留；首发从未未知且后台明确证实提交前回滚、无已完成原key时才可更正，若实现此协议必须以阶段/完整rollback证据保护，不能凭状态码推断。缺原body的旧记录不补造、不自动发新键，给可读追溯依据。
7. 当前页面刷新或重新打开仍能看到原待核对记录。实际在途提交忙态与关闭按钮一致；关闭/切账号/切单后迟到结果不能改变另一窗口、清别人的记录。生产局部独立请求通道避免共享迟到401副作用，并在当前身份确实过期时沿原登录失效规则处理；有限超时只退出忙态、保留未知，无自动重发。
8. 完整证明后显示本次数量/位置及加工记录入口，持久成功与列表刷新失败分开；缓存清理失败仍保留confirmed防重。不因成功卡被立即刷新掉而再丢可读结果。不能添加无需的普通保存确认；实际超产原确认保持一次。没有注销/tombstone或数据库迁移，本卡不启用手机未知请求终结功能。

## 文件所有权和协作

明确允许API/UI双线并行，先定合同后接线：
- /root/mobile_drawings_api：D:/.codex/worktrees/production-save-recovery-api-20261010/纸箱厂erp软件搭建。独占app/api/stock_preparation.py、新app/services/stock_preparation_completion_recovery.py、tests/test_stock_preparation_completion_recovery_api.py；另精确批准app/services/stock_preparation_processing.py增加默认None的可选builder和两处父Command INSERT前装配调用，仅此，不改加工算法。
- /root/mobile_drawings_ui：D:/.codex/worktrees/production-save-recovery-ui-20261010/纸箱厂erp软件搭建。独占static/index.html相关script引入、single加工动作/结果入口局部；static/ui/production-workspace.js必要调用；新static/ui/stock-preparation-completion-recovery.js及必要同名CSS；tests/stock_preparation_completion_recovery.cjs、tests/test_stock_preparation_completion_recovery_ui.py。不改后端、订单恢复组件或共享Axios。
- 根：计划、版本、整合、独立复核和正式发布串行。独立审查只写artifact。单文件单写入人。

## 最短验收与交付

先保留安全红再绿。真实部分8/20与全量20/20、精确同key零新增、错body/错actor/权限；真实commit后丢ack仍可readonly完整恢复；proof/审计前失败全部回滚；合成测试明确安装sprep0912同款append-only触发器，证明无需UPDATE已有命令且门禁保持；inflight未见后原POST完成；历史无proof只trace；源/目的后改不篡改冻结proof；原旧签名字节重放。UI空/截断/错量/错地址/错身份都未知；首写存储失败零POST；真实关闭/重开/刷新恢复原内容、同receipt新continuation受阻及另一receipt可用；未知后409、迟到401/成功/GET、确认成功后列表或清缓存失败、只读查询零业务POST。新接口真实HTTP导出供实际UI消费，至少一条合成隔离Chrome真实保存丢回执→刷新→只读恢复；不以手写API回包代替跨合同验证。

不跑全仓，不自动点击正式页面，不用IAB，不触碰已被自动审批拒绝的旧进程。新增隔离服务仅自身owned端口/路径记录，不关闭用户Chrome。测试、源指纹、失败与纠错事实、NAS独立回执齐全。每条线独立候选提交不push，定向验证通过后释放文件，根核精确候选、备份恢复、业务事实保持、健康/资源后发布。group dispose及手机终结门禁等未完成项须保留，不能宣布全系统已无Bug。

## 2026-10-10 实施中兼容和验证边界补充

相邻旧回归要求首次保存与同键重放完整 JSON 相同；新增 write.replayed 的 false/true 会破坏此合同。根批准删除这个非必要新增字段，保留旧测试及整个回包一致性，其他完成证明不变。只读省略 output_kind 的旧请求根据持久 Command.request_json 的有效值核对，不读取后来 job 推断原事实。空客户、楼层、位置显示名或物理单位沿旧合法合同保留空值，不能为证明新造业务门禁。

本轮新独立 Chrome 启动被自动审批以 blocked by policy 拒绝，无详细理由、无 Chrome PID，未执行，也不换工具绕过；CUA 连接失败。截图及浏览器视觉审查停止并记录未完成。根依据独立复核意见，将本轮技术验收严格收窄为最终源码下的实际 HTML/mixin 入口、真实 HTTP 完成及查询原文、页面状态重建共用持久缓存模拟刷新，断言零新增业务 POST、原 key/body/receipt/actor、continuation 锁、nullable 和历史追溯。它可以证明代码和接口恢复闭环，不能证明浏览器 DOM 可见性、Cookie/存储实际环境或现场易用性；这些仍待管理员人工验收。不得将 Chrome 写成通过，权限、事务、幂等和正式发布全部安全门禁保持。
