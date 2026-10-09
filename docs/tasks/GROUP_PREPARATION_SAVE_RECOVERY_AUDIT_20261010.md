# GROUP-PREPARATION-SAVE-RECOVERY-AUDIT-20261010

持续优化 Goal 的下一独立只读审查。当前 single_job 修复已固定 API c02693f8，UI 最终验证中，根尚未发布 v603。正式仍须实时读取，最近核对 v602/6640d536/en1009hp。允许 API 审查与当前 UI 收尾并行，仅 artifact 写入，无仓库源码修改。每个写文件仍只有一个负责人。

启动复用未变 CODEX_START→NAS AI_START→本卡→ORDER_FLOW→总需求7/14/17及章程3～9。目标只调查实际 group_job 对话框的保存入库/去向保存调用；此前真实路径是 saveStockDialog→dispose→POST /api/production/stock-preparation/group-actions，不能把按钮名当作后端 complete action。先核真实页面和 API 调用、冻结BOM/任务/输出身份及响应。

仅使用已释放但不得切分支/改源的 production-save-recovery-api-20261010 工作树 c02693f8 作导入来源，先比较本审查涉及的 group action/helper 与正式 6640d536 等价。该树仍有合成服务引用，禁止修改、checkout 或关进程。真实 UI 当前在另一工作树编辑中，不将中间 UI当已发布；API审查可先独立建立真实HTTP合同。探针只写 D:/.codex/visualizations/2026/10/10/group-preparation-save-recovery-audit/api，测试用项目安全 conftest 合成库和真实不可变触发器，绝不接正式业务库。

最短检查：真实全部/部分 group dispose 保存、同key同body重放零新增、同key异body及新key旧版本拒绝、commit前故障完整回滚、commit真实成功后ack丢失与当前可追溯能力、actor/权限/客户及冻结来源；说明 group command 是否有append-only触发器，真实签名字节与结果结构。确认老资料合法nullable和单位/子件输出，不改算法，不猜角色权限。每项区分新缺陷、现有保护、探针错误及无法证明的场景。

最多先6～8个高信息量HTTP场景，不扩全仓或其它plan/complete/assemble/revert。输出REPORT.md、XML、精确源码指纹、关键真实JSON、风险和最小修复方案，以及NAS独立回执。此卡不授权实现新group恢复合同、不新增迁移、不建永久终结标记、不做正式写入/发布、不push。待审查和当前single交付后根另定最小修复卡；不得和当前single修复混包。
