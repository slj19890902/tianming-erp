# CUSTOMER-STATEMENTS-FRONTEND-20261022

本任务仅处理三项阻碍旧单补录的前端问题：供应商纸板要求未显示压线三段；已有空印刷主档被无触碰保存成无印刷；普通已发货单实际日期更正入口不可达。

代码基线先核v711/7236cf0a，再以公开发布包及完成回执核实正式v712/901e356f；六文件修复已安全整合到准确v712，本地代码提交036ab33f。现有PDF人工识别、原单事实核对和保存保护完整保留。具体正式基线必须在下一次候选构建前重新读取，不能以本文件的历史状态替代实时核验。

## 修复边界

- 压线类型为压线时显示主料/底料已有三段；末段0不是空值，缺段提示待填写。毛净独立选择，supplier_edge_type后端合同不变。
- 原有print_content为null/空时，编辑器显示未确认，仅修改其他字段仍保留未知值。显式选无印刷/色数/挂板及新建默认行为保留。
- 送货更多菜单恢复现有actual-date接口入口，双权限、pending/dispatched状态、忙碌及明细编辑保护一致。无需取消回单；后端财务锁、日期下限、客户范围、CAS、幂等、审计不变。
- 不修改任何后端、数据库、正式业务事实或舌头默认逻辑；不取消财务/库存锁；不替代已开票历史履约的独立更正能力。

## 候选文件与验证

static/index.html以及tests/test_sheet_cutting_frontend.py、tests/ui/supplier_sheet_default.browser.cjs、tests/test_product_unknown_printing_frontend.py、tests/test_p1_137b_dispatched_delivery_frontend.py、tests/test_delivery_actual_date_entry.py。

整合到v712后23项受影响定向测试通过，包含真实前端方法和完整保存payload、确认回单入口、临时SQLite开票后拒绝及原事实不变、既有CAS/幂等/对账锁。PDF人工核对UI回归、完整Vue/内联JavaScript编译、git diff检查通过；Alembic唯一head ez1022hc，无迁移。旧布局测试7项失配此前已在未修改基线复现，未改无关断言。浏览器断言文件已更新，但本轮未运行正式页面自动点击验收。

## 构建与授权边界

允许本地提交、构建及隔离验证/回滚准备，允许最终编号确定后的必要app/version.py元数据更新。当前阶段不正式停服、不发布、不调用系统认证。并行手机任务已占用v713；另有正式升级待串行完成，最终候选编号和基线尚未指定，不冒用或预占编号。

本任务证据目录为`/Users/kimiyf/ERP-Migration/evidence/customer-statements-20261022`。release-prep生成器沿实际v712使用的固定摘要Manager流程，应用成员allowlist仅static/index.html和app/version.py；保留旧包、依赖/能力/schema一致、无删除、候选身份、系统身份、原维护锁、完整本地备份、真实Mac正常/失败恢复及原生收尾门禁。不会从脏工作树打包。当前config.pending.json缺少最终commit/version，必须拒绝准入。禁止重跑旧事务或直接覆盖正式index.html。

最终完整包、签名请求、生成驱动及摘要冻结只有在准确基线和递增版本确定后才能完成。真实签名包的原生隔离恢复检查依赖合法系统认证后的只读捕获；本阶段不会把单元测试或预览编译称作已完成签名包原生恢复演练。

发布后管理员最短人工核验：压线主料/底料及0显示；旧空印刷仅改其他字段保存仍未知；未对账送货菜单可更正日期，已有财务链仍拒绝。未获人工确认前，不标记正式验收通过。
