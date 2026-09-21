# 合并报料总量抵扣（2026-09-21）

任务：REQUISITION_GROUP_TOTAL_20260921。技术发布完成：v0.22.472，待管理员人工验收。

## 已实现
- 整组一次确认同尺寸、同材质、同楞型合格库存；跨货位/栈板合计可用量去重展示，批次清单只读。订单先录先分，批次按FIFO，同批余量可连续供给多个订单。
- 后台在一个事务内预先验证所有来源、物理资格、维度、数量和版本，原子分配与审计；并发变化失败回滚，同键同载荷回放，不重复预占。
- 明确通用库存的customer签名差异不再错误排除绿色整组入口；未知归属不等于明确通用。纸色、层数、加工、客户权限、已印刷款号限制保留。
- 未印刷模切料仍需已有模具/形状兼容或确认适用事实；不能凭外廓尺寸推断工艺。异尺寸仍主动查询黄色确认，本次不扩展分切方案执行。
- 源订单全额覆盖后，剩余采购行按稳定规格身份恢复备注和额外备库输入。没有备库的全额覆盖不生成零采购。

## 定向测试
命令：D:/纸箱厂erp软件搭建/.venv/Scripts/python.exe -m pytest -q tests/test_requisition_group_pool.py tests/test_requisition_deduction_ui.py tests/test_semi_finished_inventory_frontend.py tests/test_p1_09c_105_requisition_inventory_action_guard.py tests/test_p0_37_vue_template_browser_safety.py -k 'not pending_requisition_explains_semi_deduction_and_purchase_shortage and not pdf_direct_save_carries_the_same_reservation_plan'
结果：62 passed, 2 deselected, 46 warnings in 60.00s。警告为夹具旧短JWT密钥，不涉及正式密钥。
两项排除是v471基线已有旧文本/代码位置断言失败，已在干净v471 worktree独立复现（2 failed）：pending_requisition_explains_semi_deduction_and_purchase_shortage、pdf_direct_save_carries_the_same_reservation_plan；本轮没有修改订单/PDF流程，也不把它们写为通过。
新增失败测试先见3 failed/1 passed，修复后包含共享批次、缺口、版本冲突、异楞/异尺寸/异材质/印刷专用/客户/未知归属伪造、审计故障回滚、已确认空白模切跨款、并发重试及前端一次请求行为。
node tests/check_index_vue_template.cjs通过；git diff --check通过；alembic heads为唯一du0920，无迁移。

## 真实隔离验证
1. 全ERP独立开发实例18122，数据库D:/tm-uat/group-total-20260921-browser/semi-order-b1.sqlite3，合成客户/订单/库存。Chrome登录后选择224+112两款；界面汇总336片、库存300张；点击一次整组抵扣后，待报料剩36张，继续保存生成一张36张采购单。未对正式页面自动点击。
2. 正式库通过SQLite在线备份形成独立副本D:/tm-uat/group-total-20260921/formal-snapshot.sqlite3，仅在副本写预占。截图对应8款需求1881片，批次885/886/887/888分别525/420/540/534张，合计2019张；一次分配1881片，余138张，缺口0。原正式库存未抵扣。
证据D:/tm-uat/group-total-20260921/screenshot-sample-result.json及final-tests.txt。

## 边界与未验证
- 有额外备库且库存将整组全额覆盖时，继续保护原输入，提示先转独立补库草稿；本轮未实现该备库自动转单，不会静默丢弃备库。
- 当前绿色总量路径复用既有物理张/需求片换算与预占，不新增跨订单共享一张多出片原板的执行模型；这类分切方案不冒充本轮同尺寸1:1样本验收。
- 未运行全仓库测试、实体打印或真实手机；正式业务页面待管理员人工验收。未改写历史订单/库存/成本，无正式数据修正或迁移。

## 三步人工验收
1. 正式发布后Ctrl+F5打开报料，勾选同规格待报料款并点合并报料，检查库存合计和绿色整组按钮。
2. 点击一次整组一键抵扣，无需逐款逐批操作；有缺口仅保留缺口采购，全覆盖不产生零采购。
3. 按需展开只读批次和位置核对追溯；已印刷库存仍受存货编码限制。不要为验收在正式库新建模拟业务。

## 正式发布回执

分支：codex/requisition-group-total-20260921；代码提交：82c7e16ad16505a84850af0e75bc9d7312108de0，已推送并快进factory-current-baseline。
签名包：a59a0ff536c92ff15e66d7a10c55c7228fe27ff57ff27abaf7d6569bc13b0638
正式运行目录：D:/TianmingERP/releases/a59a0ff536c92ff15e66d7a10c55c7228fe27ff57ff27abaf7d6569bc13b0638
正式数据库：D:/TianmingERP/shared/data/carton_erp.sqlite3，未迁移、未覆盖、未人工改写业务事实。
助手Manager.update已完成签名核验、NAS完整备份解密和散列校验、切换及重启；前版v0.22.471仍保留。
备份：Z:\sata1-18015598002\BoxERP\backups\20260921-101950-e21f0a4d.tmbackup
备份SHA256：06e9a8091ce06beef8018b614015b37fc64953ee7d03d082cf45d1d6711b8985
三个地址健康均200/ok=true；正式静态HTML与签名清单一致；正式完整Vue模板编译通过；数据库integrity=ok，外键错误0，revision=du0920。
原归档工作区无关改动保持原样。未执行正式浏览器点击验收，未将技术通过记作人工通过。
