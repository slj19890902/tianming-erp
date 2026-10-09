# DESKTOP-STOCKTAKE-REVIEW-RECOVERY-20261010

持续Goal的下一独立后端/桌面闭环。真实正式仍v599源0de5439d582185bf9856ffe9bb22931f4bdd9b95、包fa67ef4adbe14b033e6d4f97865fff5f3fc4d1954c68d801da2081ffa4d1b589，en1009hp。沿用已验证真实正式祖先；远端指针ec60eb1e落后，不据此退回。原主目录无关改动不动，不push，不修正式业务数据。

入口：CODEX_START→NAS AI_START→本卡→WAREHOUSE→总需求9.1/14/15、执行章程3～9，已读未变内容复用。源证据为D:/.codex/visualizations/2026/10/10/stocktake-review-audit：真实HTTP10项、旧相邻4项、实际桌面函数8组探针；源b094564e。根已读probe.xml及原代码，首次正常审核200的reviews为空/reviewed_by_name为null，回执读取故障时commit已成功但HTTP500；commit本身失败409确实回滚，commit成功后确认丢失409则已持久，必须分开记录。同key/body精确重放200无新增。桌面空/部分200会清key并报成功；漂移分支清键和换单也会丢原请求。不能声称正式历史已发生同样故障。

方案与范围：
1. approve/reject在flush后、commit前构造完整plain回执；定向过期reviews/reviewer后重读，保留现有业务算法、审核幂等、库存/预占/审计事务和回滚。完整构造失败全部回滚；commit本身未知仍由核对处理。返回原请求action/key/规范化reason、current_actor_id及真实审核事实，历史reviewer与当前认证分开。
2. review请求增加可选严格正整数expected_actor_id，写前核对当前认证，不进入历史幂等签名，旧客户端省略兼容。沿现行warehouse.stocktake.review权限及全部客户范围，不增加或删除业务权限门禁。
3. 独立只读review-result端点，以order_id/action/原key/原reason复用resolve_review_replay匹配持久审核。明确found/not_found及完整匹配review事实，不autoflush、不业务commit。approve的details没有原key，不能靠客户端猜；显式返回精确匹配的审核id/历史reviewer和原动作。reject默认reason与原写入合同一致。mobile submit/confirm resolve原合同不扩改。GET盘点单仍可查当前状态，但不能冒充原请求匹配成功。
4. 桌面提交前按账号、单据、原action/key/body保存待核对记录，存储失败不发送。完整回执校验通过才成功，空/坏/部分2xx和任何不确定响应保留原记录。未知同一盘点单暂停新审核/改动作，其他盘点单可继续；换单、关窗、重载、401/403/409/422都不能清早先未知记录。提供直接可点的“核对审核结果”；not_found不证明永不执行，明确继续只发送原key/body。已确认成功但清记录失败维持confirmed，防重复处理。
5. 审核成功与列表刷新失败分开提示，刷新只GET。多页独立键不互相覆盖；切账号或晚响应不接管旧记录、不关闭其他新打开的明细。只读定位模式仍禁止写入/审核，普通有权限账号遵循现行权限。正常审核只保留一次必要确认，取消不生成记录/请求；不加重复确认或新审批层。

写入负责人：/root/mobile_drawings_api完成原只读报告后，从本卡提交新建codex/desktop-stocktake-review-recovery-20261010，复用自己干净chain-order-audit工作树。独占app/api/stocktake.py、static/warehouse.html、必要新static/ui/stocktake-review-recovery.js、专用tests/test_stocktake_review_recovery_api.py及tests/stocktake_review_recovery.cjs/Python包装；不要改app/services/stocktake.py或移动端恢复模块/页面。若确需服务修改先给根证据。根独占卡/版本/汇总/最终审核/串行正式发布。

本卡明确允许与MOBILE-FIELD-NAVIGATION的UI线并行，运行文件互不交叉；两个最小候选分别复核，串行发布，不同时操作正式服务。API执行者先把接口和页面恢复合同发根复核，之后实现完整闭环，不仅修API就宣称桌面弱网已解决。

验证最短组：先红后绿approve/reject正常首回执及原key重放、异载荷冲突、reviewer身份/权限/客户范围、拒绝默认reason、提交前回执错误回滚、commit后结果未知readonly找回、not_found零写入、盘亏预占/差额流水守恒。真实API回执导出交前端消费；桌面实际函数/组件坏回执、双击、取消、切单/关窗/重载、切账号晚响应、原请求冻结、存储异常、成功仅列表刷新失败。原mobile43项不全重跑，选共享接口直接受影响最短保护；源码解析、差异、唯一head。隔离Chrome界面检查可用，禁止IAB/正式点击。所有测试合成隔离数据。

完成提交候选及NAS独立回执后释放文件，由根执行实际正式基线复核、签名、冷备独立恢复、323表及附件保持、完整性/外键、健康/相关只读资源；无迁移、无正式数量或历史补写。安全终止未知请求持久合同仍未完成，不以删本地键冒充解除；管理员人工验收另记。Goal继续active。

候选后独立视觉复核：手机UI执行者完成并释放MOBILE-FIELD-NAVIGATION文件后，可只读候选6bb7de74及后续5215c5fa，在专属Chrome/合成数据环境核对实际桌面页面。不编辑桌面运行文件、不操作正式端、不扩大测试矩阵；重点原单号/货位/审核结果卡可见、核对原结果、同单暂停别单继续、一次确认及成功后列表刷新失败。API执行者提供合成库创建/启动入口，根做最终判定。5215c5fa另修已真实复现的迟到详情GET覆盖另一单/复开关闭窗口，保留红绿证据。
