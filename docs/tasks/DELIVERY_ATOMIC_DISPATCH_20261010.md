# DELIVERY-ATOMIC-DISPATCH-20261010

状态：API、UI 与助手实现已整合，定向验证通过，候选 v0.22.617 正在最终独审；尚未正式发布。本卡不把设计或负面观察当修复。用户持续升级/修复发布授权继续有效，不另索重复发布批准；具体数据及发布门禁保留。

目标：员工确认的整次送货要么完整提交一次，要么全部未提交；断网后能只读核对原结果，取消后的迟到原请求不能重新扣库，其他人改过数量的旧页面不能直接发后来新数量。正常明确重新发货、物理片/客户数量、BOM、共享库存、拿货、修订、打印及后续回单/对账阻断保持。

基线：审查575e与原正式v614相关源等价。实际正式v615/5b726452cac0709259e74b520b95e3364f7ddf05已合入fabb7c45，根审查与方案7f9c3561，工作分支codex/delivery-atomic-dispatch-20261010。正式包c9b4c3a524779beb1a92a9c7a2e6e84854f15b306c6f73ec6b38b1f5d83a84c2，revision eo1010pi，远端已到5b726452；实施前/发布前重新核对。保留v615现场查询及图纸全部附件，不覆盖主工作区或旧候选。

实施期间正式再次更新至v616/b11162d3665206d3bea0dd28d55afb3def96c1cd，包508038e684c4ec979492cec28f76fc950bf98baf635095a2913a8570b931afbf；远端e10da8d5479c77d580d9fca6010e5f62fe7f393d为其发布回执。根d0139e31已完整合入。送货API/models/helper与v615等价；保留新增补库模具打印、首页提醒组数和index中home-workbench.js的5e31af5338b09535引用。发布版本按最终正式基线顺延，不覆盖其他任务成果。

启动：CODEX_START→NAS AI_START→本卡→ORDER_FLOW→总需求4/6/7/10/11/12/16/17及章程3～9。证据为docs/reports/DELIVERY_SAVE_RECOVERY_AUDIT_20261010.md和artifact delivery-save-recovery-audit的真实HTTP/实际方法；不得扩为全仓重构。

## 实现边界

1. 原子发货命令：强制expected_actor_id、idempotency_key、expected_version及员工已确认的完整单据/拿货行快照。只读预览提供冻结客户和物理数量，发现版本/数量变化须员工重新核对；不静默换成后台最新值。
2. 同写事务内先判原key精确重放，再沿Order→Delivery→OrderItem既有锁序核版本和原快照。必要的拿货apply抽私有commit=False，随货prepare与最终dispatch同父事务。保存完整原结果后才commit；故障全回滚。原结果与当前状态分开，原key在取消后仅返回旧完成事实，不重新扣库。
3. 原记录使用FinanceIdempotencyRecord的真实delivery dispatch新action，原actor/key/hash/resource/response自洽；损坏或缺证据只给追溯，不能补造成功。只读resolver无DML，当前权限及原/当前客户范围都核对。首次明确未执行与先前unknown分开，不能把任意4xx/500当全局未执行。
4. 外部旧无body写请求明确拒绝并提示刷新。普通发货/取消变化使旧version失效；私有已发货revision整体原事务及版本语义保留。不得给旧测试全局自动添body来掩盖旧端点行为。
5. UI发出前持久保存完整原命令，独立账号/请求记录，紧凑显示“查原结果”“按原内容继续”“查看原单”。unknown跨刷新重开保留；空/部分/坏2xx不成功；有限等待及账号/窗口代际防晚回包。主完成与打印登记/列表失败分开，恢复成功不自动重打。
6. 独立读取/执行兼容能力delivery_dispatch_v1必须在新写协议开放前激活。助手update/start/rollback/fallback/backup/restore全部核签名能力，不因same revision越过，也不让迁移分支绕过；实际安装助手须先含新门禁并退出旧GUI。无新记录但在途请求也须受保护。采用shared/data/delivery-dispatch-contract.json持久哨兵、state索引、加密备份metadata中的原内容/哈希。停服冷备验证后、新程序启动前原子激活；restore在promote前核metadata/哨兵/数据库action/签名能力并恢复索引。受管API从固定共享路径核哨兵，缺环境变量不能让真实受管库放行；非受管合成库独立。数据库action仅作遗失标记兜底。无DDL，不声称阻止管理员人为直接运行旧二进制，不能以别的capability冒支持。

快照边界经独审确认：仅绑定已确认Delivery全行/版本/冻结双数量与BOM、已显式unordered allocation及拿货任务完整实际来源；普通order未预选批次明确allocation_mode=on_dispatch，保留原同锁自动多货位扣减，不复制未来分配算法、不发明lot_version或因无关库存变化挡送货。receipt记录本次真正消费流水。随货prepare同样绑定已确认需求和冻结身份，库位选择沿原算法、同事务提交或回滚。

草稿create/update恢复本身另列下一最小闭环；当前不得宣称保存弹窗全部已修。现有保存后发货入口须使用已核对持久单ID和版本，不能把草稿坏回包当已确认发货依据。原historical/revision/customer-po的保存算法不在本卡重写。

### 原请求结束出口（独审发现的发布可用性闭环）

全部错误保留原请求虽能避免重复扣库，但版本已变化的原请求无法继续；不得只让员工联系管理员而没有操作出口。新增显式“结束本次确认，重新核对”close写动作，携带完整原command及原actor/key，与execute同Order→Delivery锁序，锁后重新读ledger。若已completed返回原完成结果；若已closed返回原结束凭据；仅原key不存在时写同action的closed原凭据及审计，一次提交，原业务数量/状态不变。不得重新要求原version等当前、pending或库存资格，仍核actor、完整请求身份、原及当前客户权限和受管激活门禁。全局key唯一竞争须回滚后精确重读，不能覆盖终态。

closed证明须绑定原actor/key/hash/resource/完整原request和closed_at，不伪装dispatch_receipt。execute锁前/锁后遇有效closed只返回原closed，迟到请求不再扣库；resolver支持只读closed，not_recorded不构成结束证据。UI在发close前持久化closing意图，超时/刷新只查原结果或重发原close，不能再执行原dispatch；严格核验并持久closed凭据后才解除同单pending，新发仍读新快照并由员工确认。迟到错误不能覆盖completed/closed终态。当前候选尚未发布，delivery_dispatch_v1同时包含completed/closed，旧中间候选不作为可回退兼容版本。

最短新增真实交错：execute先赢再close仍completed；close先赢再迟到execute无库存变化；close提交后丢回包可只读恢复closed，重新核对后新key正常发货。close提交失败/权限失败保原待核对，不按任意4xx清键。不新增取消送货或草稿恢复范围。

## 并行与唯一文件所有权

本卡明确授权两个执行代理和一个独审并行。执行者从本卡最终提交建立自己的新codex分支，保留旧候选；各自既有独立worktree可在确认clean且无运行引用后复用。

- /root/mobile_drawings_api：app/api/deliveries.py、新app/services/delivery_dispatch_commands.py、新tests/test_delivery_dispatch_commands.py和tests/test_delivery_dispatch_revision_compat.py；必要的定向既有送货测试更新先报根列文件，不能全局改conftest或通用自动补body。只写artifact/delivery-atomic-dispatch/api。
- /root/mobile_drawings_ui：static/index.html、新static/ui/delivery-dispatch-recovery.js、新tests/ui/delivery_dispatch_recovery.test.cjs及实际方法/DOM定向测试；tests/fixtures/delivery-dispatch-recovery/只保存最小真实合成HTTP JSON及生成来源/SHA，不存认证Cookie/token/header，不手造或补写成功证明。只写artifact/delivery-atomic-dispatch/ui。保留v615 index原资源hash及其他模块。
- 根：desktop_assistant/manager.py、build.py、server_entry.py、onboarding.py，新desktop_assistant/delivery_dispatch_contract.py，tests/desktop_assistant/test_delivery_dispatch_reader.py，任务/报告/版本及发布资产。API调用新模块require_managed_dispatch_activation(database_path, control=None)，传真实db.get_bind().url.database，模块自动核TM_ERP_CONTROL/真实共享路径；失败ValueError转503禁止写，彼此不互改。
- /root/order_recovery_review：只写artifact/delivery-atomic-dispatch/review，独立检查真实反例→修复、数量/权限/回滚及helper恢复，发现缺陷交owner；不改候选源码。

## 最短验收与发布

真实合成HTTP覆盖原请求重放、异body/actor/customer scope、旧版本、取消后原key不扣/旧版本新key拒绝/明确新版本正常发；前置apply/prepare/dispatch各故障全回滚，原结果commit后丢回包可只读恢复；普通/无订单双数量及受控revision。UI用真实API包跑实际方法，覆盖坏ack、unknown持久化、同来源待核对、迟到跨账号/窗口、打印/列表辅助失败。helper真实签名包、激活前后、损坏/伪能力、零record窗口、真实加密backup→空目录restore及失败不promote。

不跑全仓或无关长测试。禁止IAB、正式自动点击、新Chrome/额外服务、旧PID终止、正式业务补数及Git push。root差异检查/源绑定/唯一head后独审；正式安装助手和应用须同候选通过验证，实时CAS、新冷备实际Manager.restore、原业务字段和原附件保持、完整性/外键、健康/资源/权限核对，回退门禁不能省略。无DDL时不运行无关迁移；如确需DDL先另卡完整读取三份迁移文档并执行全部门禁。

每阶段NAS独立回执，管理员1～3步现场验收待反馈；持续Goal active。局域网一次瞬时失联已独立复查自行恢复，未执行修复、不计本卡成果。

本卡artifact根：D:/.codex/visualizations/2026/10/10/delivery-atomic-dispatch。API在自己的artifact先定版API_CONTRACT.md并向UI/根通知；字段修正须同步，UI不自行发明成功proof。独审只读建议与根方案以当前本卡为实施范围。

独审补充最低边界：本轮发布适用已有受管安装升级及完整备份空目录恢复，不产全新首次接入安装器。新cap包首次接入须在停止原服务前明确拒绝并提示已接入用更新、新机器用验证恢复包；遗留pending同样保持未完成，不自动激活或启动。为此根独占onboarding.py最小门禁和同文件定向测试。取消当前只推进版本，不声称其自身已具原请求恢复或exactly-once；后续单独审。
