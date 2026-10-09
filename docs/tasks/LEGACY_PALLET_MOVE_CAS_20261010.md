# LEGACY-PALLET-MOVE-CAS-20261010

持续Goal下一闭环。前一Goal轮为实际进展：v596和v597已提交发布，根19项整合通过，正式/NAS当前v597源29881dba93d5da9afde596e857569a823c038768，包35d5403ae182f6b40692d2c84b6179d78019bdf90e543c33bcccda67bdc131a4，revision en1009hp。当前文档HEAD cf671dc3干净，远端factory-current-baseline实时仍ec60eb1e，不能用落后远端覆盖真实正式祖先。沿本会话普通发布授权和不push边界推进，正式历史数据不改。

本卡固定旧整板移位版本回退修复：根真实HTTP合成夹具在move目标claim前插入真实reserve+release，lot版本1→3，pallet仍1，旧move成功写回lot版本2；实际数量10/0保持。正常对照1→2。相关真实位置触发器已安装，完整成本/身份门禁保留。证据D:/.codex/visualizations/2026/10/10/mobile-old-move-race/REPORT.md及NAS 20261010-WAREHOUSE-LEGACY-MOVE-RACE-READONLY.md。没有证据证明正式历史已发生该异常或数量被覆盖。

采纳NAS 20261010-LEGACY-PALLET-MOVE-CAS-PLAN.md的最小方案：在任何写前记录source原位置、pallet版本和完整成员/关联lot快照；保留原target floor/projection→location/layout/空位首次写锁顺序。目标claim后先做同key已完成精确重放，再对source和全部关联lot作完整CAS，局部刷新source/成员/lot与所涉位置、核集合和资格，最后进入原savepoint及释放源占位/改位置/版本/流水流程。漂移409且整笔move回滚，独立预占/释放事实保留。

必须保护：已有预占仍可正常移位；无lot旧快照/混合成员、允许从非操作source移出的合同、源/目标rack禁止、legacy与published空位规则、目标layout token、secondary大占位、容量、客户权限、成本/数量/冻结事实、同key精确重放/异载荷、事务/审计/ground occupancy与slot回滚、成品编辑继续更新和批量移动整体回滚。相同已完成请求在目标已被自己占用时仍须重放。所有新409在原异常/重放边界内。

实现选择优先floor3内move专用薄helper或明确保持默认行为的局部刷新参数；不能给全局_linked_inventory_lots加populate_existing、不能无条件expire_all或先flush抹掉/提前写内部caller待写事实。若需要改动既有merge helper，必须先给主管精确差异理由并证明v597默认行为不变；不要为抽象重构扩大范围。

主模块WAREHOUSE，读CODEX_START→NAS AI_START→本卡→WAREHOUSE及总需求9.1/9.3，关联6.4/7/17，执行章程3至9。上一轮相关读取可复用，未变文档不重复全量加载。无新迁移，不修改正式数量/位置/历史流水、权限、附件或旧系统。旧未知手机取用安全结束只作另一任务设计，本卡不实施。

分工：后端执行者/root/mobile_drawings_api复用干净chain-order-audit工作树，从本卡提交新建codex/legacy-pallet-move-api-20261010，独占app/services/floor3_locations.py与新tests/test_legacy_pallet_move_cas.py；其他服务/API只读和回归。主管独占本卡、版本/汇总、根整合/独立复核和串行发布。最多两个执行子任务并行，第二执行者可只读评估手机端下一项，文件不能多人写。原主目录无关修改保持。

先建立至少reserve、reserve+release真实HTTP失败回归，再改代码。最短覆盖正常/已有预占/历史无lot、成员与位置漂移、target竞争/布局/停用、精确重放及同key等待窗口、各阶段故障和源ground占位/slots回滚、当前权限/客户、无关Session待写事实、实际内部caller合约。相邻回归选原move正常/竞争/重复、published位置门禁、批量正常/整体回滚及成品编辑一项；v597合并新测试和最有风险的一项batch保护保留。合成隔离库，单组少于10分钟，不跑全仓；未覆盖处如实列示。

通过候选提交后由根独立审查并按实际缺口追加验证。源码、版本、唯一head、签名、实时基线、冷备独立恢复、全部业务表/附件保持、完整性/FK、健康和相关只读核验齐全后发布。正式页面不自动点击、不用IAB，给管理员最短现场入口，未收到反馈不写人工通过。NAS独立回执记源SHA/证据/未完成项；总Goal保持active。
