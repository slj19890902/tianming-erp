# PRODUCT-REQUISITION-ACTION-RELIABILITY-20261010

持续优化下一最小闭环：只读审核产品工作台的“现有订单/主动备库/申请报料”动作及手机入口，检验重复点击、网络结果未知、切换产品/账号和权限变化时是否发生重复需求或结果串页。先复现并给最小方案；本卡初始阶段不授权运行源码改动，不为9点回顾跳过门禁。

2026-10-10 08:49正式只读核验v607，源ef952a8d60d60683b167d2890c471d37a686aa42，包e576e9d0eb176ef619938e3ec603871b35476e0785eb6faa4d12cee18ddfb5c5，en1009hp。根24b7fcbc仅多发布回执。根将从此完整基线另建codex/product-requisition-action-reliability-20261010，保留全部前序修复。远端旧6c206e33已并入，不从旧指针回退；后续发布仍实时CAS。

按CODEX_START→NAS AI_START→本卡→PLATFORM→总需求2/3/4/5/14/15/18/19及章程3～9；报料接口只补读主需求6/7相关合同。普通validated发布授权延续，但本阶段只读审查/合成复现；无Git push，无正式业务数据写入/迁移，不启动新Chrome或服务，不终止旧PID，不IAB，不正式自动点击。此前审批拒绝不重试或换工具绕过。

本卡明确允许两个只读子任务并行，根独占所有仓库文件。两代理只读根树D:/.codex/worktrees/factory-reliability-20261009/纸箱厂erp软件搭建；探针仅写各自artifact子目录。现有API/UI候选树仍保留，不修改或切分支。全部API验证沿安全conftest、tmp合成库；禁止把正式ERP_DATABASE_PATH带入pytest。

- /root/mobile_drawings_api：artifact/api。精确定位product-workbench actions触发的既有补库申请/主动备库请求API、查询及幂等；实际HTTP验证当前范围/客户/启用/数量门禁、同键重放或既有去重、断网前后能否查回同一请求。区别只打开弹窗/读接口与真实业务写；先报路由/合同，最多做3～5个高价值场景，不扫全库凑缺陷。
- /root/mobile_drawings_ui：artifact/ui。执行实际product-workbench.js action方法和desktop/mobile onAction，复用已有linkedom/VM；区分申请报料写入、打开既有订单/备库弹窗。核重复点击、成功/错误/空回执、超时、切产品/账号后续callback及返回。实际业务请求只使用合成替身；既有14包可作资料fixture但不得冒充本次写入HTTP证据。不给只读候选贴已修复标签。
- 根：固定新任务卡，核最新正式及源、主规则，整合实际复现/正常门禁/未知项目，给后续最小allowlist。独立审者仅在明确需要时接入，不扩大盲测。

保留订单与备库用途分流、审批、客户范围、数量/单位、权限、版本、幂等、事务、审计和历史事实。不能通过去掉确认/权限/幂等来简化报料，不修改数量或制造申请/订单。只有实际复现才计Bug，既有正常去重和被拒请求是正常保护。当前阶段输出源码/请求证据、最小修复建议、NAS独立回执；Goal继续active。

## 2026-10-10 09:01 实施阶段：入口身份与保存门禁

只读探针已经完成，依据老板持续修复并发布授权推进以下最小闭环，初始只读限制仅对本节allowlist解除。方案见 `docs/reports/PRODUCT_REQUISITION_ACTION_RELIABILITY_PLAN_20261010.md`。不要求凑Bug数量，不将保存未知恢复半实现计入本轮完成。

- API负责人 /root/mobile_drawings_api：只写 `app/api/requisition.py`、`app/services/external_packaging_stock_replenishment.py` 和本轮新增 `tests/test_product_requisition_action_reliability.py`；原测试若确需契约变更先向根说明。在已保留的group-save-recovery-api树确认clean、无运行引用后，从本实施卡根提交创建 `codex/product-requisition-action-api-20261010`，不得在37361旧候选上继续。负责外购原key当前权限、完整actor+normalized payload哈希、新单INSERT原子保存、旧null不造证明、完整异常重放路径，以及普通新单引用停用/删除产品资格。既有成功重放继续按原冻结事实，不以当前主档资格拒绝；不修改迁移。
- UI负责人 /root/mobile_drawings_ui：只写 `static/ui/product-workbench.js`、`static/index.html` 的本卡相关方法、`static/mobile_erp.html` 仅资源引用，以及新增 `tests/ui/product-requisition-action-reliability.test.cjs`。在已保留的group-save-recovery-ui树确认clean、无运行引用后，从同一根提交创建 `codex/product-requisition-action-ui-20261010`。负责动作轮次/账号/页面/原form身份、有限等待和迟到回调失效。合法go造成组件卸载不得阻断正常跳转；取消/切页不能继续开旧窗或写新草稿。普通开窗和保存不增加确认。
- 独立审核 /root/order_recovery_review：只写本artifact/review，审核两候选最终SHA及3～5个高价值反例。批准申请、跨日/主档变更重放、权限/数量/原采购数和原字段不变、UI真实handoff都需有当前证据。不能以作者测试全绿代替独立结论。
- 根：独占文档、任务卡、版本、合并及正式发布；只在定向回归、独立审核、差异/唯一head通过后按标准Manager冷备/恢复/事实核对/健康/静态门禁发布。无Git push、无正式业务修正、无新服务/浏览器或PID终止。当前正式仍v607；版本和正式CAS发布前重查。

下一独立闭环保留：补库保存坏ack与unknown的完整原请求持久化、同账号只读结果查询、原key精确重放；员工审批页恢复另核实际脚本。现有UI发现明确记为未修，不把上述入口修复冒充保存恢复完成。
