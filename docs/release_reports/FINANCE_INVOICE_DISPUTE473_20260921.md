# 开票缺项提示与对账异议更正 v0.22.473

状态：技术发布完成，待管理员人工验收。
任务：FINANCE-INVOICE-DISPUTE-20260921；分支 codex/finance-invoice-dispute-20260921。
代码 SHA：f429f16564437ec77b344abddc31aa280f124ac5；从正式 v472 继承，无迁移，唯一 head du0920。

## 问题和结果
正式库只读核对：客户5“苏州天华超净科技股份有限公司”的销方已保存且确认，客户税务档案仍pending，默认开票项目规则未保存。客户122“苏州天华新能源科技股份有限公司”未建开票档案。前端丢弃后端missing_items，所以无法看到真正缺项。
现在显示具体缺项、正确保存状态和首次设置顺序，保存失败保留输入。未代填、代确认任何客户税务或项目规则。
客户异议支持仅撤销确认、本期改单价、选定明细跨期，其他明细保留原月。老板明确实际签收也有误，因此数量走现有回单更正流程，同事务同步重算对账，保留原冻结单价/税率。原短收/超收处理、后续补送、库存退回/重耗、回单/对账版本、幂等、权限客户范围、收款开票和审计门禁保留。已经分属多张对账单的同一回单不直接更正，须先处理相关账单。

## 验证
- 先复现旧实现只改单价返回422；新实现本月改价、实际回单更正、重算、重新确认和生成新开票任务通过，旧任务自动作废。
- 最终定向命令：pytest -q tests/test_finance_dispute_corrections.py tests/test_phase8_finance.py::test_receipt_can_be_edited_before_statement_but_not_after tests/test_phase8_finance.py::test_old_receipt_cannot_change_after_released_balance_is_dispatched tests/test_p1_15b_unordered_finished_delivery.py::test_receipt_edit_reconsumes_previous_return_before_applying_new_shortage tests/test_p1_09c_39_return_receipt_action_guards.py：19 passed。
- 同轮开票前端4项通过；既有争议、已开票、已收款门禁定向通过。新增Node缺项显示、同月改价提交、回单路由检查通过。
- Vue完整模板和内联JS编译、git diff --check、唯一迁移head通过。正式部署目录再次完整编译通过。
- 首次试跑P1-130全文件有1项既有应付账龄用例受固定2026-08日期影响失败，与本任务无关；后续缩小到相关争议测试。没有全仓库回归，没有正式页面自动点击。
- 三个健康入口均200/ok；LAN正式HTML逐字节等于签名包，SHA256 22a185ce796b956f9aef888ee04e5d2c0111e5c721f0645702a1673b24e6c0a3。未登录财务与客户税务接口返回401。数据库integrity_check=ok、FK=0。

## 发布和数据边界
通过Manager.update完成签名文件验证、停服完整备份、解密回读与NAS散列校验、启动新服务。正式库发布前后database_info一致，未执行迁移或业务写入。
发布包：a33c0a972f5bd6203081ec790b0669562e1fe0fa571fbc658e7a259cc0864a8f
回滚程序包：a59a0ff536c92ff15e66d7a10c55c7228fe27ff57ff27abaf7d6569bc13b0638
备份：Z:\sata1-18015598002\BoxERP\backups\20260921-103323-8ba82857.tmbackup
备份SHA256：cc245981328b68d5573bdae1328d15f820b728a1d9350307670f17585d75cea8
NAS发布目录latest已更新v0.22.473。机器证据：D:/tm-build473-finance-dispute-20260921/formal-check.json 和 acceptance-readonly.json。
主工作区已有文档及plans改动保留，未回写或覆盖主工作区。

## 管理员验收
1. Ctrl+F5刷新，确认客户全称；先核对并保存已确认默认项目规则，再保存已确认税务资料，尝试生成真实开票任务；失败时能看到具体缺项。
2. 对真实异议账单填原因，改单价或点“更正回单数量”，按真实签收选择短收/超收处理；保存后核对本期金额，重新导出并确认。
3. 仅勾选需下月对账的明细移出，其余本月重新确认和开票；已有开票/收款应仍阻止直接更改。

当前没有管理员人工验收确认。真实token用量不可获取，未估算。
